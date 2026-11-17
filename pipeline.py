#!/usr/bin/env python3
"""Make one karaoke song from an official music video (the studio runs this as a subprocess per job).

    python -I pipeline.py <job dir>

Reads <job dir>/job.json {"yt": id, "lrc_id": int, "artist", "title", "lang": "en"} and writes, all inside
the job dir: out.mp4 (video copied + instrumental), vocals.m4a (preview toggle), words.json (word timings),
lyrics.ass, result.json. Downloads and stems are cached in KARAOKE_WORK by YouTube id.
Progress goes to stdout as "@step <name>" / "@info <text>"; failures end with "@error <text>" and exit 1.
Line starts come from LRCLIB's human timing shifted by this video's offset (found with Whisper); words inside
a line come from torchaudio MMS_FA forced alignment on the separated vocal stem.
"""
import json, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from assgen import make_ass, save_lines  # noqa: E402

WORK, MODELS, SEP_MODEL = C.WORK, C.MODELS, C.SEP_MODEL
UA = {"User-Agent": C.USER_AGENT}
BIN = os.path.dirname(sys.executable)
SR = 16000


def say(kind, text=""):
    print(f"@{kind} {text}".rstrip(), flush=True)


def fail(text):
    say("error", text)
    sys.exit(1)


def run(*c):
    r = subprocess.run(c, capture_output=True, text=True)
    if r.returncode:
        fail(f"{os.path.basename(c[0])} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout


job_dir = os.path.abspath(sys.argv[1])
job = json.load(open(f"{job_dir}/job.json"))
yt, lrc_id, lang = job["yt"], job["lrc_id"], job.get("lang") or "en"
source = job.get("lyrics_source") or "lrclib"
if not re.fullmatch(r"[A-Za-z0-9_-]{11}", yt):
    fail("bad YouTube id")
os.makedirs(WORK, exist_ok=True); os.makedirs(MODELS, exist_ok=True)
w = f"{WORK}/{yt}"
t0 = time.time()

# 1. lyrics first: cheap, and the job is pointless without synced lines
say("step", "lyrics")
KANA = re.compile(r"[\u3040-\u30ff]")


def karamoe_lines(kid):
    """Hand-timed romaji from the Karaoke Mugen database: (sung start, text) per line."""
    if not re.fullmatch(r"[0-9a-f-]{36}", str(kid)):
        fail("bad kara.moe id")
    k = json.load(urllib.request.urlopen(urllib.request.Request(f"https://kara.moe/api/karas/{kid}", headers=UA), timeout=30))
    info = next((l for l in k.get("lyrics_infos") or [] if l.get("default")), (k.get("lyrics_infos") or [None])[0])
    if not info:
        fail("this kara.moe entry has no lyrics")
    url = "https://kara.moe/downloads/lyrics/" + urllib.parse.quote(info["filename"])
    text = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read().decode("utf-8-sig")
    out = []
    for l in text.splitlines():
        if not l.startswith("Dialogue:"):
            continue
        f = l.split(",", 9)
        h, m_, sec = f[1].split(":")
        start = int(h) * 3600 + int(m_) * 60 + float(sec)
        body = f[9]
        # leading \k before the first syllable is lead-in time, not singing
        lead, pending = 0.0, None
        for tag, txt in re.findall(r"(\{[^}]*\})|([^{]+)", body):
            if tag:
                ks = re.findall(r"\\[kK][fo]?(\d+)", tag)
                if ks:
                    if pending is not None:
                        lead += pending
                    pending = sum(int(x) for x in ks) / 100
            elif txt.strip():
                break
        words = re.sub(r"\{[^}]*\}", "", body).replace("\\N", " ").strip()
        if words:
            out.append((start + lead, re.sub(r"\s+", " ", words)))
    return sorted(out)


if source == "karamoe":
    synced = karamoe_lines(lrc_id)
else:
    req = urllib.request.Request(f"https://lrclib.net/api/get/{int(lrc_id)}", headers=UA)
    lyr = json.load(urllib.request.urlopen(req, timeout=30))
    synced = []
    for l in (lyr.get("syncedLyrics") or "").splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", l)
        if m and m.group(3).strip():
            synced.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
if len(synced) < 4:
    fail("these lyrics have no line timings; pick a lyrics entry marked 'timed'")
align_text = [t for _, t in synced]   # what Whisper reads (original script)
origs = [None] * len(synced)          # original line shown above the romaji
if any(KANA.search(t) for _, t in synced):
    # Japanese: automatic romaji (cutlet + MeCab), the Japanese line stays on screen above it
    import cutlet  # noqa: E402
    kat = cutlet.Cutlet()
    kat.use_foreign_spelling = False  # the sung reading ("aidoru"), which is also what the aligner hears
    origs = [t for _, t in synced]
    synced = [(t, re.sub(r"\s+", " ", kat.romaji(x)).strip() or x) for t, x in synced]
    lang = "ja"
    say("info", "Japanese lyrics: automatic romaji, Japanese shown above")
elif source == "karamoe":
    lang = "ja"
    align_text = [t for _, t in synced]
# romaji lyrics sometimes carry an English translation in brackets ("Yoru no nioi ni (I'll spend all thirty
# nights)"): drop those, but only in songs whose own lyrics aren't English (in English songs, brackets are
# usually backing vocals)
EN = set("the you i and my me is your it's i'm don't can't of in it love all that this what with be are we they "
         "was for on so just know never can will".split())
_en = lambda x: (lambda t: sum(w in EN for w in t) / len(t) if t else 0.0)(re.findall(r"[a-z']+", x.lower()))
if _en(" ".join(re.sub(r"\([^)]*\)", " ", t) for _, t in synced)) < 0.05:
    def _strip(t):
        out = re.sub(r"\s*\(([^)]*)\)", lambda m: "" if len(m.group(1).split()) >= 3 and _en(m.group(1)) >= 0.2 else m.group(0), t)
        return out.strip() or t
    stripped = [(x, _strip(t)) for x, t in synced]
    if sum(a != b for (_, a), (_, b) in zip(synced, stripped)):
        say("info", f"removed English translations in brackets from {sum(a != b for (_, a), (_, b) in zip(synced, stripped))} lines")
        synced = stripped
        if not any(origs):
            align_text = [t for _, t in synced]
letters = [c for _, t in synced for c in t if c.isalpha()]
if letters and sum(c.isascii() for c in letters) / len(letters) < 0.85:
    fail("these lyrics are in a script the studio can't romanise yet (Japanese and Latin-alphabet songs work)")
say("info", f"{len(synced)} timed lines")

# 2. video (H.264 so PiKaraoke can pass it through untouched)
say("step", "download")
if not os.path.exists(f"{w}.mp4"):
    run(f"{BIN}/yt-dlp", "--js-runtimes", "node", "-q", "--no-playlist", "--no-progress",
        "-f", "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/bv*[vcodec^=avc1]+ba/b[ext=mp4]",
        "--merge-output-format", "mp4", "-o", f"{w}.mp4", "--", f"https://www.youtube.com/watch?v={yt}")
vinfo = run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
            "-of", "csv=p=0", f"{w}.mp4").strip()
duration = float(run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f"{w}.mp4"))
say("info", f"video {vinfo}, {duration:.0f}s")

# 3. separation (cached)
say("step", "separate")
vocals, inst = f"{WORK}/{yt}-vocals.wav", f"{WORK}/{yt}-inst.wav"
if not (os.path.exists(vocals) and os.path.exists(inst)):
    run("ffmpeg", "-v", "error", "-y", "-i", f"{w}.mp4", "-map", "0:a:0", "-ac", "2", "-ar", "44100", f"{w}.wav")
    from audio_separator.separator import Separator
    sep = Separator(model_file_dir=MODELS, output_dir=WORK, output_format="WAV")
    sep.load_model(SEP_MODEL)
    sep.separate(f"{w}.wav", {"Vocals": f"{yt}-vocals", "Instrumental": f"{yt}-inst"})
    del sep
    os.remove(f"{w}.wav")
import gc, torch  # noqa: E402
gc.collect(); torch.cuda.empty_cache()

# 4. alignment
say("step", "align")
import numpy as np, stable_whisper  # noqa: E402
model = stable_whisper.load_model(C.WHISPER_MODEL, device="cuda")
audio = stable_whisper.audio.load_audio(vocals)  # 16 kHz mono
res = model.align(audio, "\n".join(align_text), language=lang, original_split=True)
wsegs = [[(x.start, x.end) for x in s.words if x.word.strip()] for s in res.segments if s.words]
del model; gc.collect(); torch.cuda.empty_cache()
# Where does the video's singing sit relative to LRCLIB's (album) timing? Candidates:
#  - Whisper: median and most common difference of line starts (repeated choruses can fool the median)
#  - vocal activity: slide all line starts over the vocal stem; best match of "singing right after a line
#    start, silence just before one that follows a pause"
# Each candidate is scored on the vocal stem; Whisper's is kept unless activity is clearly better.
if len(wsegs) == len(synced):
    diffs = np.array([s[0][0] - t for s, (t, _) in zip(wsegs, synced)])
else:
    n = min(len(wsegs), len(synced), 8)
    diffs = np.array([wsegs[i][0][0] - synced[i][0] for i in range(n)]) if n else np.array([0.0])
    say("info", f"whisper found {len(wsegs)} lines vs {len(synced)}; using the first {n}")
w_median = float(np.median(diffs))
bins = np.round(diffs * 4) / 4
vals, counts = np.unique(bins, return_counts=True)
top = vals[counts == counts.max()]
mode_bin = float(top[np.argmin(np.abs(top - w_median))])
w_mode = float(np.median(diffs[np.abs(diffs - mode_bin) <= 0.5]))

HOP = 0.05
_n = int(HOP * SR)
_r = np.sqrt((audio[: len(audio) // _n * _n].reshape(-1, _n) ** 2).mean(1))
_act = (_r > 0.3 * np.percentile(_r, 90)).astype(float)
_cs = np.concatenate([[0.0], np.cumsum(_act)])
_starts = np.array([t for t, _ in synced])


def _mean_act(a, b):
    a, b = int(round(a / HOP)), int(round(b / HOP))
    if b <= 0 or a >= len(_act):
        return 0.0
    a, b = max(a, 0), min(b, len(_act))
    return (_cs[b] - _cs[a]) / max(1, b - a)


def fit(o):
    sc = 0.0
    for i, ti in enumerate(_starts):
        nxt = _starts[i + 1] if i + 1 < len(_starts) else ti + 4
        sc += _mean_act(ti + o, ti + o + min(2.0, nxt - ti))
        if i == 0 or ti - _starts[i - 1] > 3.5:
            sc -= _mean_act(ti + o - 0.8, ti + o - 0.15)
    return sc / len(_starts)


_span = len(audio) / SR
_grid = np.arange(-min(120.0, _starts[-1]), min(120.0, _span), 0.1)
_g = np.array([fit(o) for o in _grid])
_best = _grid[_g.argmax()]
_fine = np.arange(_best - 0.3, _best + 0.3, 0.02)
e_off = float(_fine[np.argmax([fit(o) for o in _fine])])
cands = {"whisper": w_median, "whisper-mode": w_mode, "activity": e_off}
scores = {k: fit(v) for k, v in cands.items()}
say("info", "offset candidates " + ", ".join(f"{k} {v:+.2f}s (activity fit {scores[k]:.2f})" for k, v in cands.items()))

import torchaudio  # noqa: E402
fa = torchaudio.pipelines.MMS_FA
fa_model = fa.get_model(with_star=False).to("cuda")
tokenizer, aligner = fa.get_tokenizer(), fa.get_aligner()
VOCAL_RMS = float(np.sqrt(np.mean(audio ** 2)))
end_all = len(audio) / SR
import unicodedata  # noqa: E402


def align_at(off):
    """Place every line at LRCLIB time + off and align its words; returns (lines, skipped, fit score)."""
    out_lines, skipped, total = [], [], 0.0
    for i, (t, text) in enumerate(synced):
        a = max(0.0, t + off - 0.3)
        b = (synced[i + 1][0] + off - 0.05) if i + 1 < len(synced) else min(end_all, a + 12)
        if a >= end_all - 0.5:
            skipped.append(text); continue
        orig = text.split()
        b_al = min(b, a + 0.3 + max(3.0, 0.9 * len(orig) + 2.5))
        clip = audio[int(a * SR):int(b_al * SR)]
        if len(clip) == 0 or float(np.sqrt(np.mean(clip ** 2))) < 0.08 * VOCAL_RMS:
            skipped.append(text); continue
        norm = [re.sub(r"[^a-z']", "", unicodedata.normalize("NFKD", x).lower()) for x in orig]
        keep = [k for k, x in enumerate(norm) if x]
        words = []
        try:
            with torch.inference_mode():
                em, _ = fa_model(torch.from_numpy(clip).float().unsqueeze(0).to("cuda"))
            spans = aligner(em[0], tokenizer([norm[k] for k in keep]))
            fps = len(clip) / SR / em.size(1)
            words = [[a + sp[0].start * fps, a + sp[-1].end * fps, orig[k]] for k, sp in zip(keep, spans)]
            total += float(np.mean([np.mean([x.score for x in sp]) for sp in spans])) if spans else 0.0
            # words without letters (e.g. "&") ride along with the previous word
            if len(keep) != len(orig) and words:
                full, wi = [], 0
                for k, x in enumerate(orig):
                    if k in keep:
                        full.append(words[wi]); wi += 1
                    elif full:
                        full[-1][2] += " " + x
                words = full
        except Exception as e:
            say("info", f"line {i + 1}: aligner failed ({str(e)[:80]}); words spread evenly")
        if not words:
            step = max(0.2, (b_al - a - 0.3) / len(orig))
            words = [[a + 0.3 + k * step, a + 0.3 + (k + 1) * step, x] for k, x in enumerate(orig)]
        # the last word is often a held note: end it where the singing actually stops
        s0, s1 = int(words[-1][0] * SR), int(b * SR)
        seg = audio[s0:s1]
        if len(seg) > SR // 10:
            fr = SR // 50
            rms = np.sqrt(np.mean(seg[: len(seg) // fr * fr].reshape(-1, fr) ** 2, axis=1))
            loud = np.nonzero(rms > max(rms.max() * 0.12, 0.2 * VOCAL_RMS, 1e-4))[0]
            if len(loud):
                end = words[-1][0] + (loud[-1] + 1) * fr / SR
                words[-1][1] = max(words[-1][1], min(end, words[-1][1] + 5.0))
        out_lines.append({"words": [[round(x, 3), round(y, 3), z] for x, y, z in words], "orig": origs[i]})
    return out_lines, skipped, total / len(synced)


# judge each offset candidate by how well the words actually fit the vocals (skipped lines count as 0)
tried = {}
for name in ("whisper", "whisper-mode", "activity"):
    v = cands[name]
    if any(abs(v - cands[n]) < 0.25 for n in tried):
        continue
    tried[name] = align_at(v)
fit_words = {n: r[2] for n, r in tried.items()}
best = max(fit_words.values())
pick = next(n for n in tried if fit_words[n] >= best - 0.01)  # near-tie: keep the earlier (Whisper) candidate
off = cands[pick]
out_lines, skipped, _ = tried[pick]
say("info", "word fit " + ", ".join(f"{n} {cands[n]:+.2f}s = {fit_words[n]:.3f}" for n in tried))
say("info", f"video offset vs album timing {off:+.2f}s ({pick})")
del fa_model; gc.collect(); torch.cuda.empty_cache()
save_lines(f"{job_dir}/words.json", out_lines)
vw, vh = (int(x) for x in vinfo.split(",")[1:3])
open(f"{job_dir}/lyrics.ass", "w").write(make_ass(out_lines, video_size=(vw, vh)))
say("info", f"{len(out_lines)} lines aligned" + (f", {len(skipped)} not sung in this video" if skipped else ""))

# 5. render: video copied + instrumental; vocals separately for the preview toggle
say("step", "render")
run("ffmpeg", "-v", "error", "-y", "-i", f"{w}.mp4", "-i", inst, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
    "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", f"{job_dir}/out.tmp.mp4")
os.replace(f"{job_dir}/out.tmp.mp4", f"{job_dir}/out.mp4")
run("ffmpeg", "-v", "error", "-y", "-i", vocals, "-c:a", "aac", "-b:a", "128k", f"{job_dir}/vocals.tmp.m4a")
os.replace(f"{job_dir}/vocals.tmp.m4a", f"{job_dir}/vocals.m4a")
json.dump({"offset": round(off, 2), "lines": len(out_lines), "skipped": skipped, "video": vinfo, "width": vw, "height": vh,
           "lang": lang, "romaji": "automatic" if any(origs) else ("kara.moe" if source == "karamoe" else None),
           "duration": round(duration, 1), "seconds": round(time.time() - t0)}, open(f"{job_dir}/result.json", "w"))
say("step", "done")
