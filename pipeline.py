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
import versions  # noqa: E402

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


def lrc_lines(text):
    """[mm:ss.xx] timed lines of an LRC text."""
    out = []
    for l in (text or "").splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", l.strip())
        if m and m.group(3).strip():
            out.append((int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()))
    return out


def plain_lines(text):
    """Lyrics without timing (pasted, or LRCLIB's plain lyrics): one line each, section headers like [Chorus: X] and
    lyrics-site leftovers dropped. Placeholder times 4 s apart; the real ones come from listening (below)."""
    keep, skip = [], 0
    raw = [l.strip() for l in (text or "").splitlines()]
    for n, l in enumerate(raw):
        if not l:
            continue
        if skip:  # Genius's "You might also like" box: three song titles and their artists, until the next header
            if re.fullmatch(r"\[[^\]]*\]", l):
                skip = 0
            else:
                skip -= 1
                continue
        m = re.search(r"\s*You might also like\s*$", l)
        if m:
            l, skip = l[:m.start()].strip(), 6
        l = re.sub(r"\d*\s*Embed$", "", l).strip()  # Genius glues "123Embed" to the last line
        if not l or re.fullmatch(r"\[[^\]]*\]", l) or re.fullmatch(r"See .* Live|Translations|\d+\s+Contributors?.*", l):
            continue
        if n < 3 and re.search(r"\bLyrics$", l):  # the page title ("You Got Beef? Lyrics")
            continue
        keep.append(l)
    return [(4.0 * k, l) for k, l in enumerate(keep)]


album_dur = 0.0  # the album recording's length (LRCLIB gives it): the music video's extra scenes lie outside it
untimed = False   # lyrics without timestamps: each line is placed by listening
if source == "karamoe":
    synced = karamoe_lines(lrc_id)
elif source == "pasted":
    synced = lrc_lines(job.get("lyrics_text"))
    if len(synced) < 4:
        synced, untimed = plain_lines(job.get("lyrics_text")), True
else:
    req = urllib.request.Request(f"https://lrclib.net/api/get/{int(lrc_id)}", headers=UA)
    lyr = json.load(urllib.request.urlopen(req, timeout=30))
    album_dur = float(lyr.get("duration") or 0)
    synced = lrc_lines(lyr.get("syncedLyrics"))
    if len(synced) < 4 and (lyr.get("plainLyrics") or "").strip():
        synced, untimed = plain_lines(lyr["plainLyrics"]), True


def _wkey(x):
    return re.sub(r"[^a-z0-9']", "", unicodedata.normalize("NFKD", x.replace("đ", "d").replace("Đ", "D")).lower())


def align_words(a, b):
    """Line up two word lists in order (semi-global: extra words at either end are free; a word only in one list
    costs a little). Returns [(index in a, index in b)] for words that match exactly or share their first 3 letters."""
    import numpy as _np
    voc = {w: n for n, w in enumerate(set(a) | set(b))}
    A_ = _np.array([voc[w] for w in a]); B_ = _np.array([voc[w] for w in b])
    pa = _np.array([hash(w[:3]) for w in a]); pb = _np.array([hash(w[:3]) for w in b])
    n, m = len(a), len(b)
    H = _np.zeros(m + 1); ptr = _np.zeros((n + 1, m + 1), dtype=_np.int8)
    for i in range(1, n + 1):
        sim = _np.where(B_ == A_[i - 1], 2.0, _np.where(pb == pa[i - 1], 1.0, -1.0))
        dg = H[:-1] + sim; up = H - 0.4
        A0 = _np.empty(m + 1); A0[0] = up[0]; A0[1:] = _np.maximum(dg, up[1:])
        src = _np.empty(m + 1, dtype=_np.int8); src[0] = 2; src[1:] = _np.where(dg >= up[1:], 1, 2)
        ix = _np.arange(m + 1)
        rmax = _np.maximum.accumulate(A0 + 0.4 * ix) - 0.4 * ix
        left = rmax > A0 + 1e-9
        H = _np.where(left, rmax, A0); ptr[i] = _np.where(left, 3, src)
    i, j, out = n, int(_np.argmax(H)), []
    while i > 0 and j > 0:
        p_ = ptr[i, j]
        if p_ == 1:
            if B_[j - 1] == A_[i - 1] or pb[j - 1] == pa[i - 1]:
                out.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif p_ == 2:
            i -= 1
        else:
            j -= 1
    return out[::-1]


def borrow_timing(lines):
    """Lyrics without timing (pasted, or LRCLIB's plain lyrics): if LRCLIB has timed lyrics for the same song, line the
    words up with the timed version's words and give each line the time where its first words fall — your words,
    their timing, whatever the line breaks (lyrics sites merge and split lines differently). Lines with nothing
    matched go between their neighbours. Returns (timed lines, album length, share of words matched) or None."""
    try:
        q = urllib.parse.urlencode({"track_name": job.get("title") or "", "artist_name": job.get("artist") or ""})
        hits = json.load(urllib.request.urlopen(urllib.request.Request(f"https://lrclib.net/api/search?{q}", headers=UA), timeout=30))
    except Exception:
        return None
    pw = [(li, k, _wkey(w)) for li, (_, x) in enumerate(lines) for k, w in enumerate(x.split()) if _wkey(w)]
    if len(pw) < 10:
        return None
    best = None
    for h in hits[:15]:
        lrc = lrc_lines(h.get("syncedLyrics"))
        if len(lrc) < 4:
            continue
        lw = [(j, k, _wkey(w)) for j, (_, x) in enumerate(lrc) for k, w in enumerate(x.split()) if _wkey(w)]
        pairs = align_words([w for _, _, w in pw], [w for _, _, w in lw])
        if not best or len(pairs) > len(best[2]):
            best = (lrc, lw, pairs, float(h.get("duration") or 0))
    if not best or len(best[2]) < 0.5 * len(pw):
        return None
    lrc, lw, pairs, dur = best
    est = {}  # pasted word -> its time in the timed version (spread evenly inside the timed line)
    for pi, li in pairs:
        j, k, _ = lw[li]
        t0 = lrc[j][0]; t1 = lrc[j + 1][0] if j + 1 < len(lrc) else t0 + 4.0
        est[pi] = t0 + min(6.0, max(0.5, t1 - t0)) * k / max(1, len(lrc[j][1].split()))
    times = [None] * len(lines)
    for pi, (li, k, _) in enumerate(pw):
        if pi in est and times[li] is None:
            times[li] = max(0.0, est[pi] - 0.3 * k)  # its first matched word, minus the words sung before it
    known = [i for i, t in enumerate(times) if t is not None]
    for i in range(len(times)):
        if times[i] is None:
            lo = max([k for k in known if k < i], default=None); hi = min([k for k in known if k > i], default=None)
            times[i] = (times[lo] + (times[hi] - times[lo]) * (i - lo) / (hi - lo) if lo is not None and hi is not None
                        else times[lo] + 3.0 * (i - lo) if lo is not None else max(0.0, times[hi] - 3.0 * (hi - i)))
    for i in range(1, len(times)):
        times[i] = max(times[i], times[i - 1] + 0.3)
    return [(t, x) for t, (_, x) in zip(times, lines)], dur, len(pairs) / len(pw)


if untimed and len(synced) >= 4:
    got = borrow_timing(synced)
    if got:
        synced, album_dur, share = got
        untimed = False
        say("info", f"your lyrics with LRCLIB's timing: {share:.0%} of the words matched a timed version")
if len(synced) < 4:
    fail("these lyrics are empty or too short (fewer than 4 lines)")
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
say("info", f"{len(synced)} lines" + (" (no timing: they'll be placed by listening)" if untimed else " with timing"))

# Backing vocals in brackets, sung over or between the lead: a line that is all brackets ("(What are you waiting for?
# Bury me, bury me)") or a bracketed phrase of 3+ words inside a line comes out of the lead lines before timing, so
# the lead is timed on its own words; each phrase is placed by itself afterwards, free to overlap the lead.
# Short ad-libs ("(yeah)", "(oh)") stay in their line. Lines with an original-script line above are left alone.
backs = []  # {"lead": index of its lead line in synced, "text": "(...)", "whole": the whole lyric line, "t": its time}
_ks, _ka, _ko = [], [], []
for (t_, x_), al_, og_ in zip(synced, align_text, origs):
    w_ = x_.split()
    if og_ is None and len(w_) >= 2 and x_.count("(") == 1 and w_[0].startswith("(") \
            and w_[-1].rstrip(",.!?;:'\"").endswith(")") and _ks:
        backs.append({"lead": len(_ks) - 1, "text": x_, "whole": True, "t": t_})
        continue
    if og_ is None:
        long_g = [g_ for g_ in re.findall(r"\([^()]*\)", x_) if len(g_.split()) >= 3]
        lead_ = x_
        for g_ in long_g:
            lead_ = lead_.replace(g_, " ")
        lead_ = re.sub(r"\s+", " ", lead_).strip(" ,")
        if long_g and len(lead_.split()) >= 2:
            backs += [{"lead": len(_ks), "text": g_, "whole": False, "t": t_} for g_ in long_g]
            x_ = al_ = lead_
    _ks.append((t_, x_)); _ka.append(al_); _ko.append(og_)
if backs:
    synced, align_text, origs = _ks, _ka, _ko
    say("info", f"{len(backs)} backing-vocal phrase{'s' if len(backs) != 1 else ''} in brackets: timed separately from the lead")

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
# Meanwhile, in its own process (CPU and network, so it runs alongside Whisper on the GPU): find the album recording
# and map the video's timeline onto it — skits, scenes and cuts found by matching the music (albumsync.py).
album_proc = None
if source != "karamoe" and not untimed and len(synced) >= 4:
    try:
        album_proc = subprocess.Popen([sys.executable, "-I", os.path.join(os.path.dirname(os.path.abspath(__file__)), "albumsync.py"),
                                       f"{w}.mp4", job.get("artist") or "", job.get("title") or "", str(album_dur or 0),
                                       str(synced[-1][0]), WORK, f"{BIN}/yt-dlp", yt],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=C.tool_env())
    except Exception:
        album_proc = None
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
# Mostly-English songs with a Japanese/Korean/Chinese chorus (Fear, and Loathing in Las Vegas: 48 English lines, 5
# Japanese): Whisper listens in English. Told "Japanese", it hears nothing in the English verses, so they got no help
# from listening and drifted. The native lines keep their romaji and their original line on screen.
mixed = lang in ("ja", "ko", "zh") and any(origs) and sum(1 for o in origs if o) < 0.4 * len(origs)
wlang = "en" if mixed else lang
if mixed:
    say("info", f"mostly English with {sum(1 for o in origs if o)} {dict(ja='Japanese', ko='Korean', zh='Chinese')[lang]} "
                f"lines: listening in English")
res = model.align(audio, "\n".join(align_text), language=wlang, original_split=True)
wsegs = [[(x.start, x.end) for x in s.words if x.word.strip()] for s in res.segments if s.words]
heard_words = []
listen_ja = lang == "ja" and not mixed and any(origs) and len(hand_romaji) < 0.8 * sum(1 for o in origs if o)
listen_latin = (SCRIPT == "latin" and not any(origs)) or mixed
if listen_ja or listen_latin:
    # what the singer actually sings. Japanese lyrics often write one word and sing another (幻 sung "yume");
    # crowd-sourced lyrics of any language drop or swap words. The AI checks compare this with the written line.
    try:
        # no voice-activity filter: it drops everything after a long non-singing scene (The Kill's video stopped being
        # transcribed at 224 s); each stretch is transcribed on its own so silence can't make it loop
        tr = model.transcribe(audio, language=wlang, word_timestamps=True, vad=False, verbose=None,
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
if untimed:
    # no timestamps: Whisper's placement of each line on the vocals becomes the line's time (offset ~0); everything
    # after (word timing, the transcription check, timing jumps) works from there as for timed lyrics
    if len(wsegs) == len(synced):
        times = [sg[0][0] for sg in wsegs]
    else:  # shouldn't happen (one segment per line); spread the lines over the sung part instead
        t_a = wsegs[0][0][0] if wsegs else 0.0
        t_b = wsegs[-1][-1][1] if wsegs else len(audio) / SR
        times = [t_a + (t_b - t_a) * k / len(synced) for k in range(len(synced))]
    for k in range(1, len(times)):  # keep lines in order
        times[k] = max(times[k], times[k - 1] + 0.3)
    synced = [(tk, x) for tk, (_, x) in zip(times, synced)]
    say("info", f"the lyrics have no timing: placed {len(synced)} lines by listening")
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


def fit_offs(offs):
    """fit(), with one offset per line (for a timing that changes along the song)."""
    sc = 0.0
    for i, ti in enumerate(_starts):
        o_ = offs[i]
        nxt = _starts[i + 1] if i + 1 < len(_starts) else ti + 4
        sc += _mean_act(ti + o_, ti + o_ + min(2.0, nxt - ti))
        if i == 0 or ti - _starts[i - 1] > 3.5:
            sc -= _mean_act(ti + o_ - 0.8, ti + o_ - 0.15)
    return sc / len(_starts)


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


def align_at(off, only=None):
    """Place every line at LRCLIB time + off (one offset, or one per line) and align its words;
    returns (lines, skipped, fit score)."""
    offs = np.broadcast_to(np.asarray(off, dtype=float), (len(synced),))
    out_lines, skipped, total, per = [], [], 0.0, []
    for i, (t, text) in enumerate(synced):
        per.append(0.0)
        if only is not None and i not in only:  # (a quick look at some lines only, for fine-tuning a part)
            continue
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
        out_lines.append({"words": [[round(x, 3), round(y, 3), z] for x, y, z in words], "orig": origs[i], "fit": per[-1], "_i": i})
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
cur_fit = fit_words[pick]  # word fit of the timing in use; later methods must beat it
scenes = []  # video times where a part with a later offset starts (the song was stopped for a scene before it)
talk_times = []  # video times of heard words that aren't lyrics (set by the transcription matching below)

# The album map (if one was found): each line goes where the album says, plus one small shift for how LRCLIB's
# timestamps sit against the album recording (they are often a second or so off it). Used unless its words fit the
# vocals clearly worse than the timing found above; then the timing-jump search below isn't needed.
album_used = False
if album_proc is not None:
    try:
        _aout, _ = album_proc.communicate(timeout=300)
    except Exception:
        album_proc.kill(); _aout = ""
    _segs = None
    for _ln in (_aout or "").splitlines():
        if _ln.startswith("@say "):
            say("info", _ln[5:])
        elif _ln.startswith("@segs "):
            _segs = json.loads(_ln[6:])
    if _segs:
        import albumsync  # noqa: E402
        _aoffs = lambda c_: np.array([c_ + albumsync.seg_off(_segs, t_ + c_) for t_, _ in synced])
        _byact = sorted(((fit_offs(_aoffs(c_)), c_) for c_ in np.arange(-3.0, 3.01, 0.25)), reverse=True)[:3]
        _abest = None
        for _, c_ in _byact:
            _ar = align_at(_aoffs(c_))
            if not _abest or _ar[2] > _abest[0][2]:
                _abest = (_ar, float(c_))
        _ar, _c = _abest
        # lines near a part boundary (a scene or cut): try both parts' offsets, keep the one the words fit better
        _bounds = [s_["a0"] for s_ in _segs[1:]]
        _o = _aoffs(_c)
        _fixed = 0
        for i_, (t_, _) in enumerate(synced):
            near_ = [b_ for b_ in _bounds if abs(t_ + _c - b_) < 4.0]
            if not near_:
                continue
            k_ = next(k for k, s_ in enumerate(_segs) if abs(s_["a0"] - near_[0]) < 1e-6)
            alts_ = {_c + _segs[k_ - 1]["off"], _c + _segs[k_]["off"]}
            fits_ = {}
            for o_ in alts_:
                tr_ = _o.copy(); tr_[i_] = o_
                fits_[o_] = align_at(tr_, only={i_})[3][i_]
            o_best = max(fits_, key=fits_.get)
            if abs(o_best - _o[i_]) > 0.01:
                _o[i_] = o_best; _fixed += 1
        if _fixed:
            _ar = align_at(_o)
            say("info", f"album map: {_fixed} line{'s' if _fixed != 1 else ''} next to a scene or cut moved to the other side")
        _aoffs_final = _o
        say("info", f"album map: lyrics sit {_c:+.2f}s off the album; word fit {_ar[2]:.3f} vs {cur_fit:.3f}")
        if _ar[2] >= cur_fit - 0.01:
            _o = _aoffs_final
            out_lines, skipped, cur_fit = _ar[0], _ar[1], _ar[2]
            off_first, off_last = float(_o[0]), float(_o[-1])
            album_dur = float(_segs[-1]["a1"])  # the real album length (LRCLIB's can be wrong)
            scenes = [s_["a0"] + s_["off"] + _c for k_, s_ in enumerate(_segs) if k_ and s_["off"] > _segs[k_ - 1]["off"] + 1.0]
            album_used = True
            say("info", "using the album map for the timing" + (f" ({len(_segs)} parts: " + ", ".join(f"{s_['off'] + _c:+.1f}s" for s_ in _segs) + ")" if len(_segs) > 1 else ""))
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
if heard_words and (not any(origs) or mixed):
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
    CO = list(runs)
    F = np.array([runs[o][3] for o in CO]).T  # lines x candidates: word fit of each line at each offset
    if os.environ.get("KARAOKE_DEBUG_FIT"):
        json.dump({"CO": CO, "F": F.tolist(), "starts": _starts.tolist()}, open(f"{job_dir}/fitdebug.json", "w"))
    # cost of a jump, in this song's own scale: rough vocals give every line a low fit (The Kill ~0.15), clean ones high
    WP = 2.5 * float(np.median(F.max(1)))
    dp2, back2 = F[0].copy(), np.zeros((n_l, len(CO)), dtype=int)
    for i in range(1, n_l):
        new, bk = np.zeros(len(CO)), np.zeros(len(CO), dtype=int)
        for c in range(len(CO)):
            best_v, best_k = dp2[c], c
            for k in range(len(CO)):
                ok = _starts[i] + CO[c] > _starts[i - 1] + CO[k] + 0.3  # lines stay in order
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
            parts.append([i, i, CO[c]])
    if len(parts) > 1 and all(b - a + 1 >= 4 for a, b, _ in parts) and not album_used:
        offs = np.array([CO[c] for c in p2])
        # fine-tune each part's shift by its own lines (the candidates can be a few seconds off: The Kill's last part
        # needs +88 s, the nearest candidate was +92): within ±5 s, 1 s steps, then 0.25 s around the best
        for k_, (a_, b_, o_) in enumerate(parts):
            idx_ = set(range(a_, b_ + 1))
            def _pf(x_):
                tr_ = offs.copy(); tr_[a_:b_ + 1] = x_
                pp_ = align_at(tr_, only=idx_)[3]
                return float(np.mean([pp_[q_] for q_ in idx_]))
            coarse_ = {o_ + d_: _pf(o_ + d_) for d_ in np.arange(-5.0, 5.01, 1.0)}
            c0_ = max(coarse_, key=coarse_.get)
            fine_ = {c0_ + d_: _pf(c0_ + d_) for d_ in np.arange(-0.75, 0.76, 0.25)}
            f0_ = max(fine_, key=fine_.get)
            if fine_[f0_] > coarse_.get(o_, -1) + 0.005:
                offs[a_:b_ + 1] = f0_
                parts[k_][2] = float(f0_)
        pw = align_at(offs)
        desc = " | ".join(f"lines {a + 1}-{b + 1} {o:+.1f}s" for a, b, o in parts)
        if pw[2] > fit_words[pick] + 0.005:
            out_lines, skipped, cur_fit = pw[0], pw[1], pw[2]
            off_first, off_last = float(offs[0]), float(offs[-1])
            scenes = [_starts[a] + o for k, (a, b, o) in enumerate(parts) if k and o > parts[k - 1][2]]
            say("info", f"the video's timing jumps partway (a scene or a cut): {desc}; word fit {pw[2]:.3f} vs {fit_words[pick]:.3f}")
        else:
            say("info", f"a timing jump looked possible ({desc}) but fit the words no better; kept one offset")


# Transcription first: line the lyrics up with what Whisper heard, word by word and in order (like aligning two
# texts). Heard words that match no lyric are talking (a skit, a scene) or mishearings and are skipped cheaply; each
# lyric line that is clearly found gets an anchor "sung here". Lines Whisper missed take their neighbours' shift.
# The words are then timed inside those windows, and the result is kept only if it fits the vocals better than the
# timing found so far.
def _tw(x):
    return re.sub(r"[^a-z']", "", unicodedata.normalize("NFKD", x.replace("đ", "d").replace("Đ", "D")).lower())


if heard_words and (not any(origs) or mixed) and len(synced) >= 4:
    _hw = [(t, _tw(x)) for t, x in heard_words]
    _hw = [x for x in _hw if x[1]]
    _lw = [(_i, k, _tw(y)) for _i, (t, text) in enumerate(synced) if not origs[_i] for k, y in enumerate(text.split()) if _tw(y)]
    if len(_hw) >= 10 and len(_lw) >= 10:
        _voc = {w: _n for _n, w in enumerate({x for _, _, x in _lw} | {x for _, x in _hw})}
        _la = np.array([_voc[x] for _, _, x in _lw]); _ha = np.array([_voc[x] for _, x in _hw])
        _lp = np.array([hash(x[:3]) for _, _, x in _lw]); _hp = np.array([hash(x[:3]) for _, x in _hw])
        _MATCH, _NEAR, _MISS, _GAP_L, _GAP_H = 2.0, 1.0, -1.0, -0.4, -0.2  # skipping heard words (talking) is cheap
        _n, _m = len(_lw), len(_hw)
        _H = np.zeros(_m + 1)  # semi-global: heard words before the first lyric cost nothing
        _ptr = np.zeros((_n + 1, _m + 1), dtype=np.int8)  # 1 diagonal, 2 lyric word unheard, 3 heard word skipped
        for _i in range(1, _n + 1):
            _sim = np.where(_ha == _la[_i - 1], _MATCH, np.where(_hp == _lp[_i - 1], _NEAR, _MISS))
            _dg = _H[:-1] + _sim
            _up = _H + _GAP_L
            _A = np.empty(_m + 1); _A[0] = _up[0]; _A[1:] = np.maximum(_dg, _up[1:])
            _src = np.empty(_m + 1, dtype=np.int8); _src[0] = 2; _src[1:] = np.where(_dg >= _up[1:], 1, 2)
            # left moves (skipping heard words) via a running max: best over k<=j of A[k] + _GAP_H*(j-k)
            _ix = np.arange(_m + 1)
            _rmax = np.maximum.accumulate(_A - _GAP_H * _ix) + _GAP_H * _ix
            _lft = _rmax > _A + 1e-9
            _H = np.where(_lft, _rmax, _A)
            _ptr[_i] = np.where(_lft, 3, _src)
        # trace back from the best end (heard words after the last lyric cost nothing)
        _i, _j = _n, int(np.argmax(_H))
        _pairs = []
        while _i > 0 and _j > 0:
            _p_ = _ptr[_i, _j]
            if _p_ == 1:
                if _ha[_j - 1] == _la[_i - 1] or _hp[_j - 1] == _lp[_i - 1]:
                    _pairs.append((_i - 1, _j - 1))
                _i, _j = _i - 1, _j - 1
            elif _p_ == 2:
                _i -= 1
            else:
                _j -= 1
        # heard words that matched no lyric: talking (a skit, an intro, a scene) or mishearings; where several of
        # them fall between lyric lines, the render keeps the video's own sound there
        # (only words that appear nowhere in the lyrics: a repeated chorus the lyrics left out is still singing)
        _matched = {_hj for _, _hj in _pairs}
        _vocab = {x for _, _, x in _lw} | {_tw(y) for bk in backs for y in bk["text"].split()}
        talk_times = [_hw[_hj][0] for _hj in range(len(_hw)) if _hj not in _matched and _hw[_hj][1] not in _vocab]
        _found = {}
        for _li, _hj in _pairs:
            _found.setdefault(_lw[_li][0], []).append((_lw[_li][1], _hw[_hj][0]))
        _nw = {}
        for i_, k_, _ in _lw:
            _nw[i_] = _nw.get(i_, 0) + 1
        _shift = np.full(n_l, np.nan)
        for i_, _got in _found.items():
            if len(_got) >= max(2, 0.4 * _nw[i_]):
                _t_i = synced[i_][0]
                # a line starts no later than its earliest heard word (words can be held for seconds: "Loooook in my
                # eyes"), so anchor on that word, allowing a quick 0.3 s for each unheard word before it
                _shift[i_] = float(min(_th - 0.15 - _kk * 0.3 for _kk, _th in _got)) - _t_i
        # an anchor that disagrees by > 2 s with both neighbours, which agree with each other, is a stray match
        for i_ in range(1, n_l - 1):
            a_, b_, c_ = _shift[i_ - 1], _shift[i_], _shift[i_ + 1]
            if not np.isnan(a_) and not np.isnan(b_) and not np.isnan(c_) and abs(a_ - c_) < 1.0 and abs(b_ - a_) > 2.0:
                _shift[i_] = np.nan
        _anch = int(np.sum(~np.isnan(_shift)))
        _raw = _shift.copy()  # each heard line's own shift (NaN: not heard clearly), before filling in the rest
        if _anch >= max(4, 0.3 * n_l):
            _known = np.where(~np.isnan(_shift))[0]
            for i_ in range(n_l):  # lines Whisper missed: the shift of the nearest anchored line (in album time)
                if np.isnan(_shift[i_]):
                    _shift[i_] = _shift[_known[np.argmin(np.abs(_starts[_known] - _starts[i_]))]]
            for i_ in range(1, n_l):  # lines stay in order
                if _starts[i_] + _shift[i_] < _starts[i_ - 1] + _shift[i_ - 1] + 0.3:
                    _shift[i_] = _shift[i_ - 1]
            _used = {_hj for _, _hj in _pairs}
            _talk = sum(1 for j_ in range(len(_hw)) if j_ not in _used)
            _ta = align_at(_shift)
            say("info", f"transcription first: {_anch} of {n_l} lines found in what was heard, {_talk} heard words "
                        f"aren't lyrics (talking or mishearings); word fit {_ta[2]:.3f} vs {cur_fit:.3f}")
            if os.environ.get("KARAOKE_DEBUG_FIT"):
                save_lines(f"{job_dir}/words.transcription.json", _ta[0])
            if _ta[2] > cur_fit + 0.005:
                out_lines, skipped, cur_fit = _ta[0], _ta[1], _ta[2]
                off_first, off_last = float(_shift[0]), float(_shift[-1])
                scenes = [_starts[i_] + _shift[i_] for i_ in range(1, n_l) if _shift[i_] - _shift[i_ - 1] >= 2.0]
                say("info", "using the transcription's timing")
            else:
                # Transcription veto. Word fit can't see a line put on the wrong repeat of a chorus (the words fit there
                # too) or on the wrong side of a jump; the ORDER of the heard words can. Where heard lines agree on a
                # shift more than 3 s away from the timing in use, that timing is wrong there (Dance, Dance: its lyrics
                # entry jumps +44 → +52 → +67 s; the last part had been put at +44.7 s, 22 s early).
                _cur = {l["_i"]: l["words"][0][0] - synced[l["_i"]][0] for l in out_lines if l.get("_i") is not None}
                _new = np.array([_cur.get(i_, np.nan) for i_ in range(n_l)])
                _bad = [i_ for i_ in range(n_l) if not np.isnan(_raw[i_]) and i_ in _cur and abs(_cur[i_] - _raw[i_]) > 3.0]
                _runs, _run = [], []
                for i_ in _bad:  # runs of such lines (a gap of up to 2 lines allowed)
                    if _run and i_ - _run[-1] > 3:
                        _runs.append(_run); _run = []
                    _run.append(i_)
                if _run:
                    _runs.append(_run)
                _vfix = 0
                if os.environ.get("KARAOKE_DEBUG_FIT"):
                    json.dump({"raw": [None if np.isnan(x) else round(float(x), 2) for x in _raw],
                               "cur": [None if np.isnan(x) else round(float(x), 2) for x in _new], "bad": _bad, "runs": _runs},
                              open(f"{job_dir}/vetodebug.json", "w"))
                # a stretch can hold more than one shift (Dance, Dance's lyrics entry jumps again near the end: +67 s,
                # then +75 s): split it wherever the heard shift moves by more than 1.5 s, and judge each part alone
                _subs = []
                for r_ in _runs:
                    cur_ = [r_[0]]
                    for k_ in r_[1:]:
                        if abs(_raw[k_] - float(np.median(_raw[cur_]))) > 1.5 and len(cur_) >= 2 and \
                                (r_.index(k_) + 1 < len(r_) and abs(_raw[r_[r_.index(k_) + 1]] - _raw[k_]) <= 1.5):
                            _subs.append(cur_); cur_ = []
                        cur_.append(k_)
                    _subs.append(cur_)
                _runs = _subs
                for r_ in _runs:
                    v_ = _raw[r_]
                    med_ = float(np.median(v_))
                    core_ = [k_ for k_ in r_ if abs(_raw[k_] - med_) <= 1.5]  # one odd line mustn't void the stretch
                    if len(core_) >= 4 and len(core_) >= 0.7 * len(r_):
                        end_ = r_[-1]
                        while end_ + 1 < n_l and np.isnan(_raw[end_ + 1]):  # unheard lines after it follow it
                            end_ += 1
                        for k_ in range(r_[0], end_ + 1):
                            _new[k_] = _raw[k_] if k_ in core_ else med_
                        _vfix += end_ - r_[0] + 1
                    elif len(r_) <= 3:  # a line or a few at a part boundary: take the neighbour's shift if that's what it heard
                        for k_ in r_:
                            for nb_ in (k_ - 1, k_ + 1):
                                if 0 <= nb_ < n_l and nb_ not in r_ and not np.isnan(_new[nb_]) and abs(_new[nb_] - _raw[k_]) < 1.5:
                                    _new[k_] = _new[nb_]; _vfix += 1
                                    break
                            else:
                                # a lone line whose own timestamp is off (The Kill's last line, 4 s late): move it to where
                                # it's clearly heard (4+ words matched in order), if that keeps the lines in order
                                at_ = _starts[k_] + _raw[k_]
                                prev_ = _starts[k_ - 1] + _new[k_ - 1] if k_ > 0 and not np.isnan(_new[k_ - 1]) else -1e9
                                next_ = _starts[k_ + 1] + _new[k_ + 1] if k_ + 1 < n_l and not np.isnan(_new[k_ + 1]) else 1e9
                                if len(_found.get(k_, [])) >= 4 and prev_ + 0.3 < at_ < next_ - 0.3:
                                    _new[k_] = _raw[k_]; _vfix += 1
                if _vfix:
                    _kn = np.where(~np.isnan(_new))[0]
                    for i_ in range(n_l):  # lines not placed now: their nearest neighbour's shift
                        if np.isnan(_new[i_]) and len(_kn):
                            _new[i_] = _new[_kn[np.argmin(np.abs(_kn - i_))]]
                    for i_ in range(1, n_l):  # lines stay in order
                        if _starts[i_] + _new[i_] < _starts[i_ - 1] + _new[i_ - 1] + 0.3:
                            _new[i_] = _new[i_ - 1]
                    _tv = align_at(_new)
                    say("info", f"transcription veto: {_vfix} line{'s' if _vfix != 1 else ''} heard more than 3 s from where "
                                f"they were placed; word fit {_tv[2]:.3f} vs {cur_fit:.3f}")
                    if _tv[2] >= cur_fit - 0.03:
                        out_lines, skipped, cur_fit = _tv[0], _tv[1], _tv[2]
                        off_first, off_last = float(_new[0]), float(_new[-1])
                        scenes = [_starts[i_] + _new[i_] for i_ in range(1, n_l) if _new[i_] - _new[i_ - 1] >= 2.0]
                        say("info", "moved them to where they were heard")


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

# place the backing phrases: slide a window the phrase's length over the stretch around its lead line and keep the
# position where the words fit the vocals best (aligning over the whole stretch smeared a phrase across 9 s)
_placed_b = 0
if backs:
    _by_i = {l["_i"]: l for l in out_lines if l.get("_i") is not None}
    _lead_st = sorted(l["words"][0][0] for l in out_lines)
    for bk in backs:
        ld = _by_i.get(bk["lead"])
        words_b = bk["text"].split()
        if not ld or any(not _nz(w_) for w_ in words_b):
            continue
        ls_, le_ = ld["words"][0][0], ld["words"][-1][1]
        nxt_ = next((t_ for t_ in _lead_st if t_ > ls_ + 0.2), le_ + 4.0)
        lk_, bk_ = " ".join(_nz(w_[2]) for w_ in ld["words"]), " ".join(_nz(w_) for w_ in words_b)
        if len(words_b) >= 2 and bk_ in lk_:  # an echo of the lead's own words ("They were born (they were born)"):
            ra = le_ - 0.4                      # it answers after the lead, so don't let it land on the lead itself
            rb = max(ra + 0.45 * len(words_b) + 3.5, nxt_ + 1.0)
        elif bk["whole"]:  # its own lyric line, after the lead: from its own time (shifted like the lead) on
            ra = max(ls_, bk["t"] + (ls_ - synced[bk["lead"]][0]) - 1.0)
            rb = max(ra + 3.0, nxt_ + 3.0)
        else:              # inside the lead line: sung with it or right after
            ra, rb = ls_ - 1.0, max(le_ + 1.0, nxt_)
        ra, rb = max(0.0, ra), min(rb, ra + 15.0, len(audio) / SR)
        span_ = min(rb - ra, 0.45 * len(words_b) + 1.2)
        best_ = None
        st_ = ra
        while st_ + span_ <= rb + 1e-6:
            try:
                tb_, sc_ = _fa_words(st_, st_ + span_, words_b)
                m_ = float(np.mean(sc_))
                if not best_ or m_ > best_[0]:
                    best_ = (m_, tb_)
            except Exception:
                pass
            st_ += 0.25
        if best_:
            out_lines.append({"words": [[round(x_, 3), round(y_, 3), w_] for w_, (x_, y_) in zip(words_b, best_[1])],
                              "orig": None, "backing": True, "singer": ld.get("singer"), "members": ld.get("members")})
            _placed_b += 1
    out_lines.sort(key=lambda l: l["words"][0][0])
    if _placed_b:
        say("info", f"placed {_placed_b} backing-vocal phrase{'s' if _placed_b != 1 else ''} by ear (they may overlap the lead)")
del fa_model; gc.collect(); torch.cuda.empty_cache()
if heard_words:
    for l in out_lines:
        if (l.get("orig") and listen_ja) or (not l.get("orig") and listen_latin):
            s0, s1 = l["words"][0][0] - 0.3, l["words"][-1][1] + 0.3
            ws = [w for t, w in heard_words if s0 <= t <= s1]
            text = "".join(ws) if l.get("orig") else " ".join(w.strip() for w in ws)
            l["heard"] = re.sub(r"\s+", " ", text).strip()[:200] or None
# lines that fit the vocals far worse than the rest of the song are probably misplaced: the editor points them out
_fits = [l.get("fit", 0.0) for l in out_lines if l.get("fit")]
if _fits:
    _med = float(np.median(_fits))
    for l in out_lines:
        l["weak"] = bool(l.get("fit") is not None and l["fit"] < 0.5 * _med)
    if any(l["weak"] for l in out_lines):
        say("info", f"{sum(l['weak'] for l in out_lines)} lines to check: their words fit the singing much worse than the rest")
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
# Talking anywhere outside the sung lines (an intro before the first line, a skit between lines, an outro): clusters
# of heard words that appear nowhere in the lyrics (3+ words, under 2.5 s apart) keep the video's own sound around
# them, unless they overlap the singing. Works for every song, including pasted lyrics, which have no album timing.
if os.environ.get("KARAOKE_DEBUG_FIT"):
    say("info", f"debug talk words: {len(talk_times)}: " + " ".join(f"{t_:.1f}" for t_ in sorted(talk_times)[:40]))
if len(talk_times) >= 3 and out_lines:
    _sp = sorted((l["words"][0][0], l["words"][-1][1]) for l in out_lines)
    _cl = []
    for t_ in sorted(talk_times):
        if _cl and t_ - _cl[-1][-1] <= 2.5:
            _cl[-1].append(t_)
        else:
            _cl.append([t_])
    _win = []
    for c_ in _cl:
        if len(c_) < 3:
            continue
        a_, b_ = c_[0] - 0.5, c_[-1] + 0.8
        # a talking stretch running into a sung line (often just a misheard word in it) stops before that line
        inside = [(ls, le) for ls, le in _sp if min(b_, le) - max(a_, ls) > 0.3]
        if inside:
            if inside[0][0] - 0.2 - a_ < 2.0:
                continue  # talking over the singing: can't keep the original there
            b_ = inside[0][0] - 0.2
        if b_ <= _sp[0][0]:
            a_ = -FADE  # talking before the first sung line: an intro; keep the video's sound from the start
        # join with the previous talking stretch when no lyric line lies between them
        if _win and a_ - _win[-1][1] < 6.0 and not any(_win[-1][1] < ls < a_ for ls, _ in _sp):
            _win[-1] = (_win[-1][0], b_)
        else:
            _win.append((a_, b_))
    for a_, b_ in _win:
        a_, b_ = max(-FADE, a_), min(duration, b_)
        if b_ - a_ >= 2.0 and not any(x <= a_ and b_ <= y for x, y in mids):
            mids.append((a_, b_))
if pre or post or mids:
    terms = ([f"clip(({pre:.2f}-t)/{FADE},0,1)"] if pre else []) + ([f"clip((t-{post:.2f})/{FADE},0,1)"] if post else []) + \
            [f"min(clip((t-{a:.2f})/{FADE},0,1),clip(({b:.2f}-t)/{FADE},0,1))" for a, b in mids]
    g = terms[0]
    for x in terms[1:]:
        g = f"max({g},{x})"
    say("info", "kept the video's own sound " + ", ".join(x for x in (
        f"for its first {pre:.0f}s (before the song)" if pre else "",
        *[f"at {max(0, a):.0f}-{b:.0f}s (talking or a scene)" for a, b in mids],
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
           "duration": round(duration, 1), "seconds": round(time.time() - t0), "encoded": "x264",
           "pipeline": versions.VERSION}, open(f"{job_dir}/result.json", "w"))
say("step", "done")
