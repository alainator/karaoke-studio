#!/usr/bin/env python3
"""Pick the SUNG reading of kanji words by listening (part of the AI romaji check).

    python -I readings.py <job dir>  < lines.json      -> JSON on stdout: {"changes": [{"i", "old", "new"}], "checked"}

Japanese lyrics often sing a kanji word with a special reading that only the printed furigana shows (未来 sung
"ashita", 運命 "sadame", 空 "kara"), so neither a dictionary nor a text-reading AI can know it. Here the local LLM
proposes whole-line romaji alternatives where a special reading is plausible, and the forced aligner (MMS, which
models sounds, not kanji) scores each alternative against the singer's voice on that line. An alternative wins only
when it fits the vocals clearly better than the current romaji. Tested on Brave Shine: it picked yume, kara, ashita,
sadame, kotoba and yume (理想), matching a human romanization, and kept the dictionary reading where it was right.
"""
import json, os, re, subprocess, sys, unicodedata, urllib.request

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402

SR = 16000
MARGIN = 3.0  # log-likelihood points an alternative must beat the current romaji by
PROMPT = (
    "Japanese song lyrics often sing a kanji word with a special reading (furigana) instead of the dictionary one, e.g. "
    "未来 sung 'ashita' or 'asu', 運命 'sadame', 空 'kara', 理想 'yume', 幻 'yume', 約束 'kotoba', 本気 'maji', 宇宙 'sora', "
    "地球 'hoshi', 永遠 'towa', 瞬間 'toki', 明日 'ashita' or 'asu', 今日 'kyou'. For each line, list the whole-line romaji "
    "versions that could plausibly be SUNG: always include the given romaji first, then up to 3 alternatives where a kanji "
    "word could have a special reading ('heard' is a rough transcription that may hint at it). Change only kanji readings; "
    "keep all other words exactly as given. Lines without such kanji: just the given romaji.\n\n")
SCHEMA = {"type": "object", "required": ["lines"], "properties": {"lines": {"type": "array", "items": {
    "type": "object", "required": ["n", "versions"], "properties": {
        "n": {"type": "integer"}, "versions": {"type": "array", "items": {"type": "string"}}}}}}}

job_dir = os.path.abspath(sys.argv[1])
job = json.load(open(f"{job_dir}/job.json"))
lines = json.load(sys.stdin)
items = [{"n": i, "japanese": l["orig"], "romaji": " ".join(w[2] for w in l["words"]),
          **({"heard": l["heard"]} if l.get("heard") else {})}
         for i, l in enumerate(lines) if l.get("orig") and re.search(r"[一-鿿]", l["orig"])]
if not items or not C.OLLAMA_URL:
    print(json.dumps({"changes": [], "checked": 0})); sys.exit()

body = {"model": C.OLLAMA_MODEL, "prompt": PROMPT + json.dumps(items, ensure_ascii=False), "stream": False, "think": False,
        "format": SCHEMA, "keep_alive": "2m", "options": {"temperature": 0, "num_ctx": 12288, "num_predict": 6000}}
req = urllib.request.Request(f"{C.OLLAMA_URL}/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
versions = {int(x["n"]): x["versions"] for x in json.loads(json.load(urllib.request.urlopen(req, timeout=600))["response"])["lines"]}

wav = os.path.join(C.WORK, f"{job['yt']}-vocals.wav")
src = wav if os.path.exists(wav) else f"{job_dir}/vocals.m4a"
audio = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                                     capture_output=True, check=True).stdout, dtype=np.float32)
import torch, torchaudio  # noqa: E402
dev = "cuda" if torch.cuda.is_available() else "cpu"
fa = torchaudio.pipelines.MMS_FA
model = fa.get_model(with_star=False).to(dev)
tok = fa.get_tokenizer()
norm = lambda x: [w for w in (re.sub(r"[^a-z']", "", unicodedata.normalize("NFKD", w).lower()) for w in x.split()) if w]


def fit(clip, text):
    """How well this romaji fits the sung audio: log-likelihood of the best forced alignment."""
    ws = norm(text)
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
    cand = [v.strip() for v in dict.fromkeys(versions.get(it["n"], [])) if v and v.strip()]
    cand = [v for v in cand if v != it["romaji"] and abs(len(v.split()) - len(it["romaji"].split())) <= 2]
    if not cand:
        continue
    l = lines[it["n"]]
    clip = audio[max(0, int((l["words"][0][0] - 0.3) * SR)):int((l["words"][-1][1] + 0.4) * SR)]
    if len(clip) < SR // 2:
        continue
    base = fit(clip, it["romaji"])
    best, best_fit = None, base + MARGIN
    for v in cand:
        f = fit(clip, v)
        if f > best_fit:
            best, best_fit = v, f
    if best:
        changes.append({"i": it["n"], "old": it["romaji"], "new": best, "gain": round(best_fit - base, 1)})
print(json.dumps({"changes": changes, "checked": len(items)}))
