#!/usr/bin/env python3
"""Check crowd-sourced lyrics against what is actually sung (part of the AI lyrics check, any Latin-script song).

    python -I lyricscheck.py <job dir>  < lines.json      -> JSON: {"changes": [{"i", "old", "new", "gain"}], "checked"}

LRCLIB lyrics are typed by people: words get swapped, dropped or added ("I wanna" vs "I want to", a missing "oh").
For lines where Whisper's transcription of the vocals ("heard") clearly differs from the written line, the local
LLM proposes the line as sung; then the forced aligner scores the written and the proposed line against the singer's
voice, and the proposal wins only when it clearly fits better. Whisper mishearing alone can't change a line.
"""
import difflib, json, os, re, subprocess, sys, unicodedata, urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

SR = 16000
MARGIN = 4.0         # log-likelihood points the proposal must beat the written line by
PROMPT = (
    "These are song lyrics typed by fans (they can have wrong, missing or extra words). For each line you get the written "
    "line, its neighbours, and 'heard': a rough automatic transcription of what the singer sings around that line (it can "
    "include a word of the neighbouring lines, and it mishears words that sound alike). Return the line as it is actually "
    "SUNG: where 'heard' has a word the written line lacks, lacks a word the written line has, or has a different word, "
    "propose that change; a separate check listens to the vocals and rejects wrong proposals, so propose plausible "
    "fixes rather than holding back. Don't replace a word with a sound-alike that makes no sense in the song. Do not "
    "take words that belong to the neighbouring lines. Keep the written spelling style (e.g. tryin'), capitalisation "
    "and punctuation. If 'heard' matches the line, return it unchanged.\n\n")
SCHEMA = {"type": "object", "required": ["lines"], "properties": {"lines": {"type": "array", "items": {
    "type": "object", "required": ["n", "sung"], "properties": {"n": {"type": "integer"}, "sung": {"type": "string"}}}}}}
key = lambda x: re.sub(r"[^a-z' ]", "", unicodedata.normalize("NFKD", x.replace("đ", "d").replace("Đ", "D")).lower()).split()
# for spotting differences only: tryin' = trying, gonna = going to, the - in did-didn't
loose = lambda x: [re.sub(r"in'$", "ing", w).strip("'") for w in key(x.replace("-", " "))]

def same(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def trim(heard, before, after):
    """The transcription window catches the end of the previous line and the start of the next one: cut those off."""
    h, pb, na = key(heard), key(before), key(after)
    raw = heard.split()
    if len(raw) != len(h):  # punctuation-only tokens: fall back to the normalised words
        raw = h
    lo, hi = 0, len(h)
    for n in range(min(len(pb), len(h) - 1, 4), 0, -1):  # leading words = the previous line's last words
        if all(same(x, y) for x, y in zip(h[:n], pb[-n:])):
            lo = n; break
    for n in range(min(len(na), hi - lo - 1, 4), 0, -1):  # trailing words = the next line's first words
        if all(same(x, y) for x, y in zip(h[hi - n:hi], na[:n])):
            hi -= n; break
    return " ".join(raw[lo:hi])


job_dir = os.path.abspath(sys.argv[1])
job = json.load(open(f"{job_dir}/job.json"))
lines = json.load(sys.stdin)
text = lambda l: " ".join(w[2] for w in l["words"])
items = []
for i, l in enumerate(lines):
    if l.get("orig") or not l.get("heard"):
        continue
    before, after = text(lines[i - 1]) if i else "", text(lines[i + 1]) if i + 1 < len(lines) else ""
    heard = trim(l["heard"], before, after)
    if heard and loose(text(l)) != loose(heard):
        items.append({"n": i, "written": text(l), "heard": heard, "before": before, "after": after})
if not items or not C.OLLAMA_URL:
    print(json.dumps({"changes": [], "checked": 0})); sys.exit()

body = {"model": C.OLLAMA_MODEL, "prompt": PROMPT + json.dumps(items, ensure_ascii=False), "stream": False, "think": False,
        "format": SCHEMA, "keep_alive": "2m", "options": {"temperature": 0, "num_ctx": 12288, "num_predict": 6000}}
req = urllib.request.Request(f"{C.OLLAMA_URL}/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
sung = {int(x["n"]): str(x["sung"]).strip() for x in json.loads(json.load(urllib.request.urlopen(req, timeout=600))["response"])["lines"]}

wav = os.path.join(C.WORK, f"{job['yt']}-vocals.wav")
src = wav if os.path.exists(wav) else f"{job_dir}/vocals.m4a"
audio = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                                     capture_output=True, check=True).stdout, dtype=np.float32)
import torch, torchaudio  # noqa: E402
dev = "cuda" if torch.cuda.is_available() else "cpu"
fa = torchaudio.pipelines.MMS_FA
model = fa.get_model(with_star=False).to(dev)
tok = fa.get_tokenizer()


def fit(clip, t):
    ws = [w for w in (re.sub(r"[^a-z']", "", w) for w in key(t)) if w]
    if not ws:
        return -1e9
    with torch.inference_mode():
        em, _ = model(torch.from_numpy(clip.copy()).float().unsqueeze(0).to(dev))
        lp = torch.log_softmax(em, dim=-1)
        tg = torch.tensor([c for w in tok(ws) for c in w], dtype=torch.int32, device=dev).unsqueeze(0)
        if tg.size(1) >= lp.size(1):
            return -1e9
        _, sc = torchaudio.functional.forced_align(lp, tg, blank=0)
        return float(sc.sum())


changes = []
for it in items:
    new = re.sub(r"\s+", " ", sung.get(it["n"], "")).strip()
    old = it["written"]

    if not new or key(new) == key(old) or abs(len(new.split()) - len(old.split())) > 3:
        continue
    # don't let it borrow the neighbours' words
    if key(new) and (key(new) == key(it["before"]) or key(new) == key(it["after"])):
        continue
    l = lines[it["n"]]
    clip = audio[max(0, int((l["words"][0][0] - 0.3) * SR)):int((l["words"][-1][1] + 0.4) * SR)]
    if len(clip) < SR // 2:
        continue
    gain = fit(clip, new) - fit(clip, old)

    if gain > MARGIN:
        changes.append({"i": it["n"], "old": old, "new": new, "gain": round(gain, 1)})
print(json.dumps({"changes": changes, "checked": len(items)}))
