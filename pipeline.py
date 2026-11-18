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
import json, os, re, shutil, subprocess, sys, time, unicodedata, urllib.parse, urllib.request

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


album_dur = 0.0  # the album recording's length (LRCLIB gives it): the music video's extra scenes lie outside it
if source == "karamoe":
    synced = karamoe_lines(lrc_id)
else:
    req = urllib.request.Request(f"https://lrclib.net/api/get/{int(lrc_id)}", headers=UA)
    lyr = json.load(urllib.request.urlopen(req, timeout=30))
    album_dur = float(lyr.get("duration") or 0)
    synced = []
    for l in (lyr.get("syncedLyrics") or "").splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", l)
        if m and m.group(3).strip():
            synced.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
if len(synced) < 4:
    fail("these lyrics have no line timings; pick a lyrics entry marked 'timed'")
# Some uploads give every line two or three times at (almost) the same moment: Japanese, romaji, and/or an English
# translation, often marked with arrows (↓ original, → romaji, ↑ translation). Group lines that start within
# 0.15 s, keep the Japanese, use a human romaji if there is one, drop translations.
DECOR = re.compile(r"[\u2190-\u21ff\u2669-\u266f\u2605\u2606]")
CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7a3]")  # Japanese, Chinese, Korean
EN_HINT = set("the you i and my me is your it's i'm don't can't of in it love all that this what with be are we they "
              "was for on so just know never can will if as at to".split())
_english = lambda x: (lambda t: bool(t) and sum(w in EN_HINT for w in t) / len(t) > 0.12)(re.findall(r"[a-z']+", x.lower()))
_clean = lambda x: re.sub(r"\s+", " ", DECOR.sub(" ", x)).strip()
hand_romaji = {}
if any(CJK.search(x) for _, x in synced):
    groups, cur = [], []
    for t, x in synced:
        if cur and t - cur[0][0] >= 0.15:
            groups.append(cur); cur = []
        cur.append((t, x))
    if cur:
        groups.append(cur)
    merged = []
    for g in groups:
        if len(g) == 1:  # a line on its own is sung, even if it's English ("Stay the night")
            merged.append((g[0][0], _clean(g[0][1])))
            continue
        kinds = []
        for t, x in g:
            mark = "jp" if "\u2193" in x else "ro" if "\u2192" in x else "tr" if "\u2191" in x else None
            if mark is None:
                mark = "jp" if CJK.search(x) else "tr" if _english(x) else "ro"
            kinds.append((mark, t, _clean(x)))
        jp = next(((t, x) for m, t, x in kinds if m == "jp" and x), None)
        if jp is None:  # no Japanese in this group: keep its lines as they are (minus translations)
            merged += [(t, x) for m, t, x in kinds if m != "tr" and x]
            continue
        jpos = next(k for k, (m, t, x) in enumerate(kinds) if m == "jp")
        ro = [x for k, (m, t, x) in enumerate(kinds) if m == "ro" and x and not CJK.search(x)]
        after = [x for k, (m, t, x) in enumerate(kinds) if k > jpos and m == "ro" and x and not CJK.search(x)]
        merged.append(jp)
        if after or ro:
            hand_romaji[len(merged) - 1] = (after or ro)[0]
    if len(merged) < len(synced):
        say("info", f"lyrics came with extra copies of each line: merged {len(synced)} into {len(merged)} lines"
                    + (f", using {len(hand_romaji)} hand-made romaji lines" if hand_romaji else ""))
    synced = merged
else:
    synced = [(t, _clean(x)) for t, x in synced]
synced = [(t, x) for t, x in synced if x]
align_text = [t for _, t in synced]   # what Whisper reads (original script)
origs = [None] * len(synced)          # original line shown above the romaji
from romanize import script_of  # noqa: E402
SCRIPT = script_of([t for _, t in synced])
if any(KANA.search(t) for _, t in synced):
    # Japanese: automatic romaji (cutlet + MeCab), the Japanese line stays on screen above it
    import cutlet  # noqa: E402
    kat = cutlet.Cutlet()
    kat.use_foreign_spelling = False  # the sung reading ("aidoru"), which is also what the aligner hears
    # lines without Japanese (English parts, already-romaji lines) stay as they are and get no line above
    origs = [t if CJK.search(t) else None for _, t in synced]
    synced = [(t, hand_romaji[k] if k in hand_romaji else
               (re.sub(r"\s+", " ", kat.romaji(x)).strip() or x) if CJK.search(x) else x)
              for k, (t, x) in enumerate(synced)]
    lang = "ja"
    auto_n = sum(1 for k in range(len(synced)) if k not in hand_romaji and CJK.search(origs[k] or ""))
    say("info", "Japanese lyrics: " + ("hand-made romaji from the lyrics" if not auto_n else
             f"automatic romaji for {auto_n} lines" + (f", hand-made for {len(hand_romaji)}" if hand_romaji else "")) + ", Japanese shown above")
elif SCRIPT in ("ko", "zh"):
    # Korean: pronounced romanization (sound changes applied); Mandarin: pinyin, one syllable per character.
    # The original line stays on screen above.
    from romanize import romanize_korean, romanize_mandarin
    conv = romanize_korean if SCRIPT == "ko" else romanize_mandarin
    origs = [t if CJK.search(t) else None for _, t in synced]
    synced = [(t, hand_romaji[k] if k in hand_romaji else (re.sub(r"\s+", " ", conv(x)).strip() or x) if CJK.search(x) else x)
              for k, (t, x) in enumerate(synced)]
    lang = SCRIPT
    say("info", ("Korean lyrics: romanized as pronounced" if SCRIPT == "ko" else "Chinese lyrics: pinyin (Mandarin)")
        + ", original shown above")
elif source == "karamoe":
    lang = "ja"
    align_text = [t for _, t in synced]
# romaji lyrics sometimes carry an English translation in brackets ("Yoru no nioi ni (I'll spend all thirty
# nights)"): drop those, but only in songs whose own lyrics aren't English (in English songs, brackets are
# usually backing vocals)
EN = set("the you i and my me is your it's i'm don't can't of in it love all that this what with be are we they "
         "was for on so just know never can will".split())
_en = lambda x: (lambda t: sum(w in EN for w in t) / len(t) if t else 0.0)(re.findall(r"[a-z']+", x.lower()))
ROMAJI_HINT = set("wa wo ni ga kimi boku watashi naka kokoro nai desu dake kara made".split())
_rj = lambda x: (lambda t: sum(w in ROMAJI_HINT for w in t) / len(t) if t else 0.0)(re.findall(r"[a-z']+", x.lower()))
_romanized = SCRIPT != "latin" or _rj(" ".join(t for _, t in synced)) > 0.05  # romaji uploads (not e.g. Spanish)
if _romanized and _en(" ".join(re.sub(r"\([^)]*\)", " ", t) for _, t in synced)) < 0.05:
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
if letters and sum(unicodedata.name(c, "").startswith("LATIN") for c in letters) / len(letters) < 0.85:
    fail("these lyrics are in a script the studio can't romanise yet (Latin-alphabet, Japanese, Korean and Mandarin work)")
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
_txt = " ".join(t for _, t in synced)
_accented = sum(1 for c in _txt if c.isalpha() and not c.isascii()) / max(1, sum(c.isalpha() for c in _txt))
if SCRIPT == "zh" or (SCRIPT == "latin" and (_en(_txt) < 0.05 or _accented > 0.03)):
    # which language is actually sung? (the lyrics' letters can't tell Spanish from Portuguese, or Mandarin from Cantonese)
    try:
        mid = len(audio) // 2
        det = model.transcribe(audio[max(0, mid - 15 * SR):mid + 15 * SR], language=None, vad=True, verbose=None).language
    except Exception:
        det = None
    if SCRIPT == "zh" and det == "yue":
        fail("this sounds like Cantonese; only Mandarin is supported for Chinese songs so far")
    if SCRIPT == "latin" and det and det not in ("ja", "ko", "zh", "yue"):
        lang = det
    say("info", f"sung language: {det or 'unknown'}")
res = model.align(audio, "\n".join(align_text), language=lang, original_split=True)
wsegs = [[(x.start, x.end) for x in s.words if x.word.strip()] for s in res.segments if s.words]
heard_words = []
listen_ja = lang == "ja" and any(origs) and len(hand_romaji) < 0.8 * sum(1 for o in origs if o)
listen_latin = SCRIPT == "latin" and not any(origs)
if listen_ja or listen_latin:
    # what the singer actually sings. Japanese lyrics often write one word and sing another (幻 sung "yume");
    # crowd-sourced lyrics of any language drop or swap words. The AI checks compare this with the written line.
    try:
        # no voice-activity filter: it drops everything after a long non-singing scene (The Kill's video stopped being
        # transcribed at 224 s); each stretch is transcribed on its own so silence can't make it loop
        tr = model.transcribe(audio, language=lang, word_timestamps=True, vad=False, verbose=None,
                              condition_on_previous_text=False)
        heard_words = [((w.start + w.end) / 2, w.word) for sg in tr.segments for w in sg.words]
        say("info", f"listened to the vocals for the lyrics check ({len(heard_words)} words heard)")
    except Exception as e:
        say("info", f"couldn't transcribe the vocals ({str(e)[:60]})")
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
    """Place every line at LRCLIB time + off (one offset, or one per line) and align its words;
    returns (lines, skipped, fit score)."""
    offs = np.broadcast_to(np.asarray(off, dtype=float), (len(synced),))
    out_lines, skipped, total, per = [], [], 0.0, []
    for i, (t, text) in enumerate(synced):
        per.append(0.0)
        a = max(0.0, t + offs[i] - 0.3)
        b = (synced[i + 1][0] + offs[i + 1] - 0.05) if i + 1 < len(synced) else min(end_all, a + 12)
        if a >= end_all - 0.5:
            skipped.append(text); continue
        orig = text.split()
        b_al = min(b, a + 0.3 + max(3.0, 0.9 * len(orig) + 2.5))
        b_al = max(b_al, a + 0.3 + len(re.sub(r"[^a-z]", "", text.lower())) / 22)  # fast rap: ~22 letters a second max
        clip = audio[int(a * SR):int(b_al * SR)]
        if len(clip) == 0 or float(np.sqrt(np.mean(clip ** 2))) < 0.08 * VOCAL_RMS:
            skipped.append(text); continue
        norm = [re.sub(r"[^a-z']", "", unicodedata.normalize("NFKD", x.replace("đ", "d").replace("Đ", "D")).lower()) for x in orig]
        keep = [k for k, x in enumerate(norm) if x]
        words = []
        try:
            with torch.inference_mode():
                em, _ = fa_model(torch.from_numpy(clip).float().unsqueeze(0).to("cuda"))
            spans = aligner(em[0], tokenizer([norm[k] for k in keep]))
            fps = len(clip) / SR / em.size(1)
            words = [[a + sp[0].start * fps, a + sp[-1].end * fps, orig[k]] for k, sp in zip(keep, spans)]
            per[-1] = float(np.mean([np.mean([x.score for x in sp]) for sp in spans])) if spans else 0.0
            total += per[-1]
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
    return out_lines, skipped, total / len(synced), per


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
out_lines, skipped = tried[pick][0], tried[pick][1]
say("info", "word fit " + ", ".join(f"{n} {cands[n]:+.2f}s = {fit_words[n]:.3f}" for n in tried))
say("info", f"video offset vs album timing {off:+.2f}s ({pick})")

# Music videos sometimes stop the song for a scene, or cut part of it: from there on, the album timing shifts by
# that much. Score every line at every offset near the chosen one (vocal activity), then find the best piecewise-
# constant path (Viterbi): changing offset costs a penalty and must be a real jump (>= 2 s), each part >= 4 lines.
# The result is kept only if the words then fit the vocals better than with one offset (the same judge as above).
off_first = off_last = off  # the offset at the song's start and end (they differ when the video's timing jumps)
scenes = []  # video times where a part with a later offset starts (the song was stopped for a scene before it)
STEP, JUMP, PENALTY = 0.25, 2.0, 2.0
grid = off + np.arange(-60.0, 60.0 + STEP / 2, STEP)
n_l = len(_starts)
S = np.zeros((n_l, len(grid)))
for i, ti in enumerate(_starts):
    nxt = _starts[i + 1] if i + 1 < n_l else ti + 4
    S[i] = [_mean_act(ti + o, ti + o + min(2.0, nxt - ti)) for o in grid]
    if i == 0 or ti - _starts[i - 1] > 3.5:
        S[i] -= [_mean_act(ti + o - 0.8, ti + o - 0.15) for o in grid]
far = int(round(JUMP / STEP))
dp, back = S[0].copy(), np.zeros((n_l, len(grid)), dtype=int)
G = len(grid); idx = np.arange(G)
for i in range(1, n_l):
    # best earlier state at least `far` bins below / above each state (running max with its position)
    pre_v, pre_i = np.maximum.accumulate(dp), np.zeros(G, dtype=int)
    suf_v, suf_i = np.maximum.accumulate(dp[::-1])[::-1], np.zeros(G, dtype=int)
    for k in range(1, G):
        pre_i[k] = k if dp[k] > pre_v[k - 1] else pre_i[k - 1]
    suf_i[G - 1] = G - 1
    for k in range(G - 2, -1, -1):
        suf_i[k] = k if dp[k] >= suf_v[k + 1] else suf_i[k + 1]
    lo, hi = idx - far, idx + far
    from_lo = np.where(lo >= 0, pre_v[np.clip(lo, 0, G - 1)], -1e9)
    from_hi = np.where(hi < G, suf_v[np.clip(hi, 0, G - 1)], -1e9)
    jump = np.maximum(from_lo, from_hi) - PENALTY
    src = np.where(from_lo >= from_hi, pre_i[np.clip(lo, 0, G - 1)], suf_i[np.clip(hi, 0, G - 1)])
    take = jump > dp
    back[i] = np.where(take, src, idx)
    dp = S[i] + np.maximum(dp, jump)
path = [int(np.argmax(dp))]
for i in range(n_l - 1, 0, -1):
    path.append(int(back[i][path[-1]]))
path = path[::-1]
# candidate offsets: the chosen one plus each part of that path (fine-tuned); now judge them by the words
def _line_act(i, x):
    nxt = _starts[i + 1] if i + 1 < n_l else _starts[i] + 4
    return _mean_act(_starts[i] + x, _starts[i] + x + min(2.0, nxt - _starts[i]))


MAX_C = 8
cands_o = [off]
path_c = []  # the activity path's offsets (coarse: they join after the transcription's)
for g in dict.fromkeys(path):
    o = float(grid[g])
    mine = [i for i, h in enumerate(path) if abs(float(grid[h]) - o) < STEP / 2]  # fine-tune on its own lines
    fine = np.arange(o - 0.3, o + 0.3, 0.02)
    path_c.append(float(fine[int(np.argmax([np.mean([_line_act(i, x) for i in mine]) for x in fine]))]))
# the most direct evidence: find the lyric lines in the free transcription of the vocals (Latin and Japanese songs)
# and see how far each sits from its album time; shifts several lines agree on become candidates too
if heard_words and not any(origs):
    import difflib
    hw = [(t, re.sub(r"[^a-z']", "", x.lower())) for t, x in heard_words]
    hw = [x for x in hw if x[1]]
    hws = [x for _, x in hw]
    pairs = []
    for i, (t, text) in enumerate(synced):
        lw = [x for x in (re.sub(r"[^a-z']", "", y.lower()) for y in text.split()) if x]
        if len(lw) < 3:
            continue
        for j in range(len(hw) - len(lw) + 1):
            if difflib.SequenceMatcher(None, lw, hws[j:j + len(lw)]).ratio() >= 0.75:
                pairs.append((hw[j][0] - 0.2 - t, i))
    # densest shifts first: a 3 s window (lyric timestamps drift within a part), counted in distinct lines;
    # after taking one, drop everything within JUMP of it and look again
    pairs.sort()
    clusters, left = [], list(pairs)
    while left:
        xs = np.array([x for x, _ in left])
        sup = [len({i for x, i in left if abs(x - c) <= 1.5}) for c in xs]
        k = int(np.argmax(sup))
        grp = [x for x, _ in left if abs(x - xs[k]) <= 1.5]
        clusters.append((sup[k], float(np.median(grp))))
        left = [(x, i) for x, i in left if abs(x - clusters[-1][1]) >= JUMP]
        if sup[k] < 3:
            break
    for support, o in sorted(clusters, reverse=True):
        if len(cands_o) >= 5 or support < 3:
            break
        if abs(o - off) <= 120 and all(abs(o - c) >= JUMP for c in cands_o):
            cands_o.append(o)
for o in path_c:
    if len(cands_o) < MAX_C and all(abs(o - c) >= JUMP for c in cands_o):
        cands_o.append(o)
# the path above can overlook a section (a coarse signal, one global trade-off): also let every run of 6 lines
# propose the offset that suits it best, when that is clearly better for it than every candidate so far
W = 6
props = []
for a0 in range(0, max(1, n_l - W + 1), 2):
    m = S[a0:a0 + W].mean(0)
    g = int(np.argmax(m))
    have = max(m[int(np.argmin(np.abs(grid - c)))] for c in cands_o)
    props.append((m[g] - have, float(grid[g]), list(range(a0, min(n_l, a0 + W)))))
for gain, o, mine in sorted(props, reverse=True):
    if len(cands_o) >= MAX_C or gain < 0.25:
        break
    if all(abs(o - c) >= JUMP for c in cands_o):
        fine = np.arange(o - 0.3, o + 0.3, 0.02)
        cands_o.append(float(fine[int(np.argmax([np.mean([_line_act(i, x) for i in mine]) for x in fine]))]))
say("info", "timing candidates " + ", ".join(f"{o:+.1f}s" for o in cands_o))
if len(cands_o) > 1:
    runs = {o: (tried[pick] if o == off else align_at(o)) for o in cands_o[:MAX_C]}
    C = list(runs)
    F = np.array([runs[o][3] for o in C]).T  # lines x candidates: word fit of each line at each offset
    if os.environ.get("KARAOKE_DEBUG_FIT"):
        json.dump({"C": C, "F": F.tolist(), "starts": _starts.tolist()}, open(f"{job_dir}/fitdebug.json", "w"))
    # cost of a jump, in this song's own scale: rough vocals give every line a low fit (The Kill ~0.15), clean ones high
    WP = 2.5 * float(np.median(F.max(1)))
    dp2, back2 = F[0].copy(), np.zeros((n_l, len(C)), dtype=int)
    for i in range(1, n_l):
        new, bk = np.zeros(len(C)), np.zeros(len(C), dtype=int)
        for c in range(len(C)):
            best_v, best_k = dp2[c], c
            for k in range(len(C)):
                ok = _starts[i] + C[c] > _starts[i - 1] + C[k] + 0.3  # lines stay in order
                if k != c and ok and dp2[k] - WP > best_v:
                    best_v, best_k = dp2[k] - WP, k
            new[c], bk[c] = F[i, c] + best_v, best_k
        dp2, back2[i] = new, bk
    p2 = [int(np.argmax(dp2))]
    for i in range(n_l - 1, 0, -1):
        p2.append(int(back2[i][p2[-1]]))
    p2 = p2[::-1]
    parts = []
    for i, c in enumerate(p2):
        if parts and p2[i - 1] == c:
            parts[-1][1] = i
        else:
            parts.append([i, i, C[c]])
    if len(parts) > 1 and all(b - a + 1 >= 4 for a, b, _ in parts):
        offs = np.array([C[c] for c in p2])
        pw = align_at(offs)
        desc = " | ".join(f"lines {a + 1}-{b + 1} {o:+.1f}s" for a, b, o in parts)
        if pw[2] > fit_words[pick] + 0.005:
            out_lines, skipped = pw[0], pw[1]
            off_first, off_last = float(offs[0]), float(offs[-1])
            scenes = [_starts[a] + o for k, (a, b, o) in enumerate(parts) if k and o > parts[k - 1][2]]
            say("info", f"the video's timing jumps partway (a scene or a cut): {desc}; word fit {pw[2]:.3f} vs {fit_words[pick]:.3f}")
        else:
            say("info", f"a timing jump looked possible ({desc}) but fit the words no better; kept one offset")



def _fa_words(a, b, texts):
    """Align words (each must have letters) in audio [a, b]: (start, end) per word and its mean token score."""
    clip = audio[int(a * SR):int(b * SR)]
    with torch.inference_mode():
        em, _ = fa_model(torch.from_numpy(clip).float().unsqueeze(0).to("cuda"))
    spans = aligner(em[0], tokenizer([_nz(t) for t in texts]))
    fps = len(clip) / SR / em.size(1)
    return [(a + sp[0].start * fps, a + sp[-1].end * fps) for sp in spans], [float(np.mean([x.score for x in sp])) for sp in spans]


_nz = lambda x: re.sub(r"[^a-z']", "", unicodedata.normalize("NFKD", x.replace("đ", "d").replace("Đ", "D")).lower())
# Lines are aligned one by one inside the slot LRCLIB's timestamps give them, so one wrong timestamp squeezes a line
# ("we'll carry on, we'll carry on" crammed into the time before a too-early next line). Re-check every pair of
# touching lines aligned together, the boundary free to move; keep the joint timing when it clearly fits the vocals
# better. Tested: Welcome to the Black Parade's choruses fixed, wrong moves on other songs score lower and are refused.
moved = 0
for i in range(len(out_lines) - 1):
    A, B = out_lines[i]["words"], out_lines[i + 1]["words"]
    if B[0][0] - A[-1][1] > 0.6 or any(not _nz(wd[2]) for wd in A + B):
        continue
    try:
        _, sa = _fa_words(A[0][0] - 0.15, A[-1][1] + 0.15, [wd[2] for wd in A])
        _, sb = _fa_words(B[0][0] - 0.15, B[-1][1] + 0.15, [wd[2] for wd in B])
        tj, sj = _fa_words(A[0][0] - 0.15, B[-1][1] + 0.15, [wd[2] for wd in A + B])
    except Exception:
        continue
    if abs(tj[len(A)][0] - B[0][0]) >= 0.5 and np.mean(sj) - np.mean(sa + sb) >= 0.09:
        held = B[-1][1]  # the second line's held last note (measured earlier) isn't affected by the boundary
        for wd, (x, y) in zip(A + B, tj):
            wd[0], wd[1] = round(x, 3), round(y, 3)
        B[-1][1] = max(B[-1][1], held)
        moved += 1
if moved:
    say("info", f"{moved} line boundar{'y' if moved == 1 else 'ies'} moved where the lyrics' timestamps squeezed a line")
del fa_model; gc.collect(); torch.cuda.empty_cache()
if heard_words:
    for l in out_lines:
        if l.get("orig") or listen_latin:
            s0, s1 = l["words"][0][0] - 0.3, l["words"][-1][1] + 0.3
            ws = [w for t, w in heard_words if s0 <= t <= s1]
            text = "".join(ws) if l.get("orig") else " ".join(w.strip() for w in ws)
            l["heard"] = re.sub(r"\s+", " ", text).strip()[:200] or None
save_lines(f"{job_dir}/words.json", out_lines)
vw, vh = (int(x) for x in vinfo.split(",")[1:3])
open(f"{job_dir}/lyrics.ass", "w").write(make_ass(out_lines, video_size=(vw, vh)))
say("info", f"{len(out_lines)} lines aligned" + (f", {len(skipped)} not sung in this video" if skipped else ""))

# 5. render: video copied + instrumental; vocals separately for the preview toggle
say("step", "render")
# Music videos often add scenes before or after the song (a skit, dialogue). The album recording starts at `off` in
# the video and lasts album_dur; outside it, keep the video's own sound (with its talking) instead of the
# instrumental, crossfading over a second. Only when the extra part is clear: >3 s, and no lyric line inside it.
# The video is re-encoded, not copied: YouTube's H.264 as downloaded plays in ffmpeg, but Chrome-based browsers
# (Chrome, Brave, Edge) hit a decode error after jumping backwards in it and the picture freezes for good (The Kill:
# frozen at 148 s on the first jump). x264 with a keyframe every 2 s fixes that, makes jumping fast, and costs
# ~15 s for a 5-minute 1080p video.
VENC = C.VENC
FADE = 1.0
first_t = min(l["words"][0][0] for l in out_lines)
last_t = max(l["words"][-1][1] for l in out_lines)
pre = off_first - 0.2 if 3.0 < off_first < first_t - 0.5 else None
post = off_last + album_dur + 0.2 if album_dur and last_t + 0.5 < off_last + album_dur < duration - 3.0 else None
# scenes inside the song: the silence-of-the-song between the last word before a jump and the first word after it
mids = []
for v in scenes:
    s0 = max([l["words"][-1][1] for l in out_lines if l["words"][0][0] < v - 0.3], default=None)
    e0 = min([l["words"][0][0] for l in out_lines if l["words"][0][0] >= v - 0.3], default=None)
    if s0 is not None and e0 is not None and e0 - s0 >= 3.0:
        mids.append((s0 + 0.3, e0 - 0.3))
if pre or post or mids:
    terms = ([f"clip(({pre:.2f}-t)/{FADE},0,1)"] if pre else []) + ([f"clip((t-{post:.2f})/{FADE},0,1)"] if post else []) + \
            [f"min(clip((t-{a:.2f})/{FADE},0,1),clip(({b:.2f}-t)/{FADE},0,1))" for a, b in mids]
    g = terms[0]
    for x in terms[1:]:
        g = f"max({g},{x})"
    say("info", "kept the video's own sound " + ", ".join(x for x in (
        f"for its first {pre:.0f}s (before the song)" if pre else "",
        *[f"at {a:.0f}-{b:.0f}s (a scene inside the song)" for a, b in mids],
        f"after {post:.0f}s (after the song)" if post else "") if x))
    mix = (f"[0:a:0]aformat=sample_rates=44100:channel_layouts=stereo,volume='{g}':eval=frame[o];"
           f"[1:a:0]aformat=sample_rates=44100:channel_layouts=stereo,volume='1-{g}':eval=frame[i];"
           "[o][i]amix=inputs=2:duration=shortest:normalize=0[a]")
    run("ffmpeg", "-v", "error", "-y", "-i", f"{w}.mp4", "-i", inst, "-filter_complex", mix, "-map", "0:v:0", "-map", "[a]",
        *VENC, "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", f"{job_dir}/out.tmp.mp4")
else:
    run("ffmpeg", "-v", "error", "-y", "-i", f"{w}.mp4", "-i", inst, "-map", "0:v:0", "-map", "1:a:0", *VENC,
        "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", f"{job_dir}/out.tmp.mp4")
os.replace(f"{job_dir}/out.tmp.mp4", f"{job_dir}/out.mp4")
run("ffmpeg", "-v", "error", "-y", "-i", vocals, "-c:a", "aac", "-b:a", "128k", f"{job_dir}/vocals.tmp.m4a")
os.replace(f"{job_dir}/vocals.tmp.m4a", f"{job_dir}/vocals.m4a")
json.dump({"offset": round(off, 2), "lines": len(out_lines), "skipped": skipped, "video": vinfo, "width": vw, "height": vh,
           "lang": lang, "romaji": ("hand" if len(hand_romaji) >= 0.8 * sum(1 for o in origs if o) else "automatic") if any(origs)
           else ("kara.moe" if source == "karamoe" else None),
           "duration": round(duration, 1), "seconds": round(time.time() - t0), "encoded": "x264"}, open(f"{job_dir}/result.json", "w"))
say("step", "done")
