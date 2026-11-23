"""Custom videos for a made song: the song keeps its sound and lyric timing and plays over a different picture
(a fan video, a dance cover, someone's own music video).

A custom video only supplies the picture. It is lined up with the song by sound (most home-made videos have the song
playing in them), rendered muted and shifted, scaled to the official video's size (so the lyrics file fits every
version), and given the song's own audio track from out.mp4. Two ways to use one:

  replace:  it becomes the song's video (out.mp4); the official render is kept in videos/official.mp4 so it can be
            restored. One at a time: its source and settings are in videos/replace/.
  version:  a second version with a name ("Mia's version"), in versions/<slug>/out.mp4, published to the library as its
            own entry with the same lyrics.

Each custom video keeps its source file and meta.json: {"name", "mode", "slug", "source": {...}, "start": seconds,
"offset", "trim_start", "confidence", "aligned": "sound" | "manual" | "none", "fill", "lead", "made_at", ...}.
"start" is the authoritative one: where the custom video's first frame plays, in the ORIGINAL (untrimmed) official
video's time. "offset" is the same point in the song's current time (start - the trim's start) at the last render, so
when a trim changes, a re-render puts the picture back in the right place.

No web code here; app.py runs these in a background thread and keeps the job's state.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata

import numpy as np

import config as C

SR, HOP = 16000, 160          # onset envelopes at exactly 100 frames per second
FPS = SR / HOP
MIN_CONFIDENCE = 0.5           # below this, alignment gives no offset (the page offers a nudge instead)
MAX_ANALYSE = 30 * 60          # seconds of a custom video looked at for alignment
MAX_SOURCE_BYTES = 4 << 30     # downloads and uploads
BIN = os.path.dirname(sys.executable)  # yt-dlp lives next to python (as in pipeline.py)
FILLS, LEADS = ("hold", "loop"), ("hold", "black")
VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".3gp", ".mts", ".ts", ".wmv", ".flv", ".mpg", ".mpeg")


# ---------------- paths and state ----------------
def replace_dir(job_dir):
    return os.path.join(job_dir, "videos", "replace")


def official_backup(job_dir):
    return os.path.join(job_dir, "videos", "official.mp4")


def version_dir(job_dir, slug):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", slug or ""):
        raise ValueError("bad version name")
    return os.path.join(job_dir, "versions", slug)


def entry_dir(job_dir, key):
    """key: "replace" or a version's slug."""
    return replace_dir(job_dir) if key == "replace" else version_dir(job_dir, key)


def official_file(job_dir):
    """The official render in the song's current time: the backup while a custom video replaces it, else out.mp4."""
    b = official_backup(job_dir)
    return b if os.path.exists(b) and os.path.exists(os.path.join(replace_dir(job_dir), "meta.json")) else os.path.join(job_dir, "out.mp4")


def entry_video(job_dir, key):
    return os.path.join(job_dir, "out.mp4") if key == "replace" else os.path.join(version_dir(job_dir, key), "out.mp4")


def slugify(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40].strip("-")
    if not s:  # a name in another script (Japanese, Korean...): a short fingerprint keeps names apart
        import hashlib
        s = "v-" + hashlib.sha1((name or "").strip().encode()).hexdigest()[:8]
    return s if s not in ("official", "replace") else "version"


def load_meta(d):
    try:
        return json.load(open(os.path.join(d, "meta.json")))
    except (OSError, ValueError):
        return None


def save_meta(d, meta):
    p = os.path.join(d, "meta.json")
    with open(p + ".tmp", "w") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    os.replace(p + ".tmp", p)


def source_file(d):
    hits = [p for p in glob.glob(os.path.join(d, "source.*")) if not p.endswith((".part", ".tmp", ".ytdl"))]
    if not hits:
        raise RuntimeError("its source video is missing; add it again")
    return hits[0]


def entries(job_dir):
    """Every custom video of the song: the replacement (if any) first, then the versions by name."""
    out = []
    m = load_meta(replace_dir(job_dir))
    if m:
        out.append(m)
    for d in sorted(glob.glob(os.path.join(job_dir, "versions", "*"))):
        m = load_meta(d)
        if m:
            out.append(m)
    return out


def trim_start(job_dir):
    """Where the song's current files start in the original video: the trim as actually applied to the media
    (result.json, written by trimtools), so a render always matches the files on disk."""
    try:
        return float(((json.load(open(os.path.join(job_dir, "result.json"))).get("trim") or {}).get("start")) or 0.0)
    except (OSError, ValueError, TypeError):
        return 0.0


# ---------------- probing and audio ----------------
def _run(cmd, what):
    r = subprocess.run(cmd, capture_output=True, text=True, env=C.tool_env())
    if r.returncode:
        raise RuntimeError(f"{what} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def probe(path):
    """{"duration", "video": bool, "audio": bool, "width", "height", "fps"} of a media file."""
    out = json.loads(_run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate,avg_frame_rate,duration",
                           "-of", "json", path], "reading the video"))
    st = out.get("streams") or []
    v = next((s for s in st if s.get("codec_type") == "video"), None)
    a = next((s for s in st if s.get("codec_type") == "audio"), None)
    dur = float((out.get("format") or {}).get("duration") or (v or {}).get("duration") or 0)
    return {"duration": dur, "video": v is not None, "audio": a is not None, "width": (v or {}).get("width"),
            "height": (v or {}).get("height"), "fps": (v or {}).get("r_frame_rate") or "30/1",
            "avg_fps": (v or {}).get("avg_frame_rate") or "",
            "audio_duration": float((a or {}).get("duration") or dur or 0)}


def song_length(job_dir):
    """The song's length: its audio track (what is copied into every custom render)."""
    return probe(official_file(job_dir))["audio_duration"]


def _pcm(path, limit=None):
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, *(["-t", str(limit)] if limit else []),
           "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode:
        raise RuntimeError("couldn't read its sound: " + r.stderr.decode("utf-8", "replace").strip()[-200:])
    return np.frombuffer(r.stdout, np.float32)


def reference_audio(job_dir):
    """The song as it is now (instrumental + vocals, current time), mono 16 kHz."""
    inst = _pcm(official_file(job_dir))
    vp = os.path.join(job_dir, "vocals.m4a")
    if os.path.exists(vp):
        voc = _pcm(vp)
        n = max(len(inst), len(voc))
        inst = np.pad(inst, (0, n - len(inst))) + np.pad(voc, (0, n - len(voc)))
    return inst


def envelope(y):
    """Onset strength, ~100 frames/s, with its slow loudness trend removed and scaled to unit variance."""
    import librosa
    e = librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP, n_fft=1024).astype(np.float64)
    if len(e) < 10:
        return e
    k = int(FPS)  # 1 s moving average
    trend = np.convolve(e, np.ones(k) / k, mode="same")
    e = e - trend
    sd = e.std()
    return e / sd if sd > 0 else e


# ---------------- alignment ----------------
def _ncc(r, c, min_overlap):
    """Normalized cross-correlation of envelopes r (song, N) and c (custom, M) at every lag L (custom frame k plays at
    song frame k + L). Returns (lags, scores); lags with too little overlap score -1."""
    from scipy.signal import fftconvolve
    N, M = len(r), len(c)
    corr = fftconvolve(r, c[::-1], mode="full")
    lags = np.arange(len(corr)) - (M - 1)
    cr, cc = np.concatenate([[0.0], np.cumsum(r * r)]), np.concatenate([[0.0], np.cumsum(c * c)])
    r_lo, r_hi = np.maximum(0, lags), np.minimum(N, M + lags)
    c_lo, c_hi = np.maximum(0, -lags), np.minimum(M, N - lags)
    ov = r_hi - r_lo
    ok = ov >= min_overlap
    er = cr[np.clip(r_hi, 0, N)] - cr[np.clip(r_lo, 0, N)]
    ec = cc[np.clip(c_hi, 0, M)] - cc[np.clip(c_lo, 0, M)]
    den = np.sqrt(np.maximum(er * ec, 1e-9))
    score = np.where(ok, corr / den, -1.0)
    return lags, score


def _peak(lags, score):
    i = int(np.argmax(score))
    p = float(score[i])
    frac = 0.0
    if 0 < i < len(score) - 1 and score[i - 1] > -1 and score[i + 1] > -1:  # parabola through the top 3 points
        a, b, c = score[i - 1], score[i], score[i + 1]
        d = a - 2 * b + c
        frac = float(0.5 * (a - c) / d) if d < 0 else 0.0
    return i, p, float(lags[i] + frac) / FPS


def align(job_dir, source, log=None):
    """Find where the custom video sits against the song, by sound.
    Returns {"offset": seconds in the song's current time where the custom video's first frame plays (None when
    unsure), "confidence": 0..1, "score": peak correlation, "drift": seconds the match moves from the first third of
    the song to the last (a different speed or an edit), "reason": why there's no offset (or None)}."""
    info = probe(source)
    if not info["audio"]:
        return {"offset": None, "confidence": 0.0, "score": None, "drift": None, "reason": "it has no sound to line up by"}
    r = envelope(reference_audio(job_dir))
    c = envelope(_pcm(source, MAX_ANALYSE))
    if len(c) < 5 * FPS or float(np.abs(c).sum()) == 0:
        return {"offset": None, "confidence": 0.0, "score": None, "drift": None, "reason": "its sound is too short or silent"}
    N, M = len(r), len(c)
    short = min(N, M)
    min_ov = int(min(short, max(0.5 * short, min(short, 20 * FPS))))
    lags, score = _ncc(r, c, min_ov)
    i, p, off = _peak(lags, score)
    # how far the best match stands out: against the second-best match more than 0.5 s away, and against the
    # spread of all scores (beats repeat, so a wrong match has many near-equal rivals)
    valid = score > -1
    away = valid & (np.abs(lags - lags[i]) > 0.5 * FPS)
    p2 = float(score[away].max()) if away.any() else 0.0
    rest = score[away] if away.any() else score[valid]
    med = float(np.median(rest))
    mad = float(np.median(np.abs(rest - med))) * 1.4826 or 1e-6
    z = (p - med) / mad
    # the same match in each third of the overlap, searched within ±3 s: a home video with the song playing agrees
    # everywhere; a live take, a sped-up edit or an unrelated song doesn't
    L = int(round(off * FPS))
    a0, a1 = max(0, L), min(N, M + L)
    parts = []
    if a1 - a0 >= 30 * FPS:
        step = (a1 - a0) // 3
        for k in range(3):
            s0, s1 = a0 + k * step, a0 + (k + 1) * step
            seg = r[s0:s1]
            w = int(3 * FPS)
            c0, c1 = max(0, s0 - L - w), min(M, s1 - L + w)
            if c1 - c0 < len(seg):
                continue
            ll, sc = _ncc(c[c0:c1], seg, len(seg))  # seg slid along the custom part: lag = custom frame of seg[0]
            j, pj, _ = _peak(ll, sc)
            parts.append({"offset": round(float(s0 - (c0 + ll[j])) / FPS, 3), "score": round(float(pj), 3)})
    drift = round(float(parts[-1]["offset"] - parts[0]["offset"]), 3) if len(parts) >= 2 else None
    agree = sum(1 for q in parts if abs(q["offset"] - off) <= 0.12) if parts else None
    # confidence: a clear, consistent peak. Thresholds from tests (see the report): the same recording scores a peak
    # correlation of ~0.5-0.9 standing far above its rivals; unrelated music ~0.1-0.2 with rivals almost as high.
    c_peak = np.clip((p - 0.15) / 0.25, 0, 1)
    c_margin = np.clip((p - p2) / max(p, 1e-6) / 0.25, 0, 1)
    c_z = np.clip((z - 5) / 10, 0, 1)
    conf = float(c_peak * 0.4 + c_margin * 0.3 + c_z * 0.3)
    if parts:
        conf *= 0.4 + 0.6 * agree / len(parts)
    conf = round(conf, 3)
    res = {"offset": round(off, 3), "confidence": conf, "score": round(p, 3), "second": round(p2, 3), "z": round(float(z), 1),
           "drift": drift, "parts": parts, "reason": None}
    if log:
        log(f"alignment: offset {off:+.3f} s, peak {p:.3f}, next best {p2:.3f}, z {z:.1f}, parts {parts}, confidence {conf:.2f}")
    if conf < MIN_CONFIDENCE:
        res.update(offset=None, best_guess=round(off, 3), reason="its sound doesn't match the song well enough")
    return res


# ---------------- getting the source ----------------
def fetch(url, dest_dir):
    """Download a YouTube (or other yt-dlp) link into dest_dir/source.<ext>, as the pipeline downloads (H.264 up to
    1080p when there is one; any other format otherwise, since it's re-encoded anyway)."""
    if not re.match(r"https?://[^\s/]+", url or ""):
        raise ValueError("give a web link (https://...)")
    os.makedirs(dest_dir, exist_ok=True)
    for p in glob.glob(os.path.join(dest_dir, "source.*")):
        os.remove(p)
    r = subprocess.run([f"{BIN}/yt-dlp", "--js-runtimes", "node", "-q", "--no-playlist", "--no-progress",
                        "--max-filesize", str(MAX_SOURCE_BYTES),
                        "-f", "bv*[vcodec^=avc1][height<=1080]+ba[ext=m4a]/bv*[vcodec^=avc1]+ba/b[ext=mp4]/bv*[height<=1080]+ba/b",
                        "--merge-output-format", "mp4/mkv", "-o", os.path.join(dest_dir, "source.%(ext)s"), "--", url],
                       capture_output=True, text=True, env=C.tool_env())
    if r.returncode:
        raise RuntimeError("couldn't download it: " + (r.stderr or r.stdout).strip().splitlines()[-1][-200:] if (r.stderr or r.stdout).strip() else "couldn't download it")
    return source_file(dest_dir)


def check_source(path):
    info = probe(path)
    if not info["video"]:
        raise RuntimeError("that file has no picture")
    if info["duration"] < 3:
        raise RuntimeError("that video is too short")
    return info


# ---------------- rendering ----------------
def render(job_dir, source, offset, out_path, fill="hold", lead="hold"):
    """The custom video, muted, with its first frame at `offset` seconds of the song (current time): before that, its
    first frame held (lead="hold") or black ("black"); if it ends before the song, its last frame held (fill="hold")
    or played again from the start ("loop"). Scaled to the official video's size (letterboxed), at its frame rate,
    cut to the song's length, with the song's audio track copied. Written to out_path atomically."""
    off = official_file(job_dir)
    o = probe(off)
    W, H = o["width"], o["height"]
    length = o["audio_duration"] or o["duration"]
    src = probe(source)
    if not src["video"]:
        raise RuntimeError("the source has no picture")
    if offset >= length - 1:
        raise RuntimeError("that start is after the end of the song")
    if offset <= -(src["duration"] - 1) and fill != "loop":
        raise RuntimeError("that start skips past the end of the video")
    pre = []
    if fill == "loop":
        pre += ["-stream_loop", "-1"]
    if offset < 0:
        pre += ["-ss", f"{-offset:.3f}"]
    chain = [f"fps={_rate(src['avg_fps'], o['fps'])}", f"scale={W}:{H}:force_original_aspect_ratio=decrease:force_divisible_by=2",
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black", "setsar=1", "format=yuv420p"]
    if offset > 0:
        chain.append(f"tpad=start_duration={offset:.3f}:start_mode={'clone' if lead == 'hold' else 'add'}:color=black")
    if fill != "loop":
        chain.append(f"tpad=stop_mode=clone:stop_duration={length + 5:.0f}")
    tmp = out_path + ".tmp.mp4"
    _run(["ffmpeg", "-v", "error", "-nostdin", "-y", *pre, "-i", source, "-i", off,
          "-filter_complex", f"[0:v:0]{','.join(chain)}[v]", "-map", "[v]", "-map", "1:a:0",
          *C.VENC, "-c:a", "copy", "-t", f"{length:.3f}", "-movflags", "+faststart", tmp], "making the video")
    os.replace(tmp, out_path)
    return length


def _rate(src_rate, official_rate):
    """The custom video's own (average) frame rate, made constant and kept between 10 and 60 fps; the official's
    when it can't be read (a phone's 60 fps video isn't dropped to a music video's 24)."""
    try:
        a, _, b = str(src_rate).partition("/")
        r = float(a) / float(b or 1)
        if 10 <= r <= 61:
            return src_rate
        if r > 61:
            return "60"
    except (ValueError, ZeroDivisionError):
        pass
    return official_rate


# ---------------- the whole steps (run by app.py in a background thread) ----------------
def add(job_dir, mode, name, source_path, source_info, offset=None, fill="hold", lead="hold", progress=None):
    """Add a custom video from a downloaded/uploaded file (moved into the entry's folder): align it (unless `offset`,
    in the song's current time, is given), render it, and write its meta.json. Returns the meta."""
    progress = progress or (lambda *a: None)
    if mode not in ("replace", "version"):
        raise ValueError("mode: replace or version")
    fill, lead = (fill if fill in FILLS else "hold"), (lead if lead in LEADS else "hold")
    if mode == "version":
        name = (name or "").strip()[:60]
        if not name:
            raise ValueError("give the version a name")
        slug = slugify(name)
        d = version_dir(job_dir, slug)
    else:
        slug, d = "replace", replace_dir(job_dir)
    info = check_source(source_path)
    staging = d + ".new"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    ext = os.path.splitext(source_path)[1].lower()
    src = os.path.join(staging, "source" + (ext if ext in VIDEO_EXTS else ".mp4"))
    shutil.move(source_path, src)
    try:
        meta = {"name": name if mode == "version" else None, "mode": mode, "slug": slug, "source": dict(source_info),
                "source_duration": round(info["duration"], 2), "has_audio": info["audio"], "fill": fill, "lead": lead}
        if offset is None:
            progress("align", "lining it up with the song by sound")
            a = align(job_dir, src)
            meta.update(confidence=a["confidence"], score=a["score"], drift=a["drift"], reason=a["reason"],
                        best_guess=a.get("best_guess"))
            if a["offset"] is None:
                offset, meta["aligned"] = 0.0, "none"
            else:
                offset, meta["aligned"] = a["offset"], "sound"
        else:
            meta.update(aligned="manual", confidence=None)
        ts = trim_start(job_dir)
        meta.update(start=round(offset + ts, 3), offset=round(offset, 3), trim_start=ts)
        progress("render", "making the video")
        out = os.path.join(staging, "render.mp4")
        render(job_dir, src, offset, out, fill, lead)
        meta.update(made_at=time.time(), stale=False, error=None)
        save_meta(staging, meta)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # put it in place: a version's folder, or the replacement over out.mp4 (official kept the first time)
    if mode == "replace":
        if not os.path.exists(os.path.join(d, "meta.json")):  # nothing replaces it yet: out.mp4 is the official render
            os.makedirs(os.path.dirname(official_backup(job_dir)), exist_ok=True)
            shutil.copyfile(os.path.join(job_dir, "out.mp4"), official_backup(job_dir) + ".tmp")
            os.replace(official_backup(job_dir) + ".tmp", official_backup(job_dir))
        os.replace(os.path.join(staging, "render.mp4"), os.path.join(job_dir, "out.mp4"))
    else:
        os.replace(os.path.join(staging, "render.mp4"), os.path.join(staging, "out.mp4"))
    shutil.rmtree(d, ignore_errors=True)
    os.replace(staging, d)
    return meta


def rerender(job_dir, key, offset=None, fill=None, lead=None, realign=False, progress=None):
    """Render a custom video again: with a new offset (song's current time; the nudge), new fill/lead options, after
    a trim or remake (offset None: from its stored original-video start), or lined up by sound again (realign)."""
    progress = progress or (lambda *a: None)
    d = entry_dir(job_dir, key)
    meta = load_meta(d)
    if not meta:
        raise RuntimeError("no such video")
    src = source_file(d)
    ts = trim_start(job_dir)
    if fill in FILLS:
        meta["fill"] = fill
    if lead in LEADS:
        meta["lead"] = lead
    if offset is not None:
        meta.update(aligned="manual" if meta.get("aligned") != "sound" else "sound+nudge")
        off = float(offset)
    elif realign:
        progress("align", "lining it up with the song by sound")
        a = align(job_dir, src)
        meta.update(confidence=a["confidence"], score=a["score"], drift=a["drift"], reason=a["reason"],
                    best_guess=a.get("best_guess"), aligned="sound" if a["offset"] is not None else "none")
        off = a["offset"] if a["offset"] is not None else 0.0
    else:
        off = float(meta.get("start") or 0.0) - ts
    progress("render", "making the video")
    target = entry_video(job_dir, key)
    if key == "replace" and not os.path.exists(official_backup(job_dir)):
        raise RuntimeError("the official video's copy is missing; restore it by remaking the song")
    render(job_dir, src, off, target, meta.get("fill", "hold"), meta.get("lead", "hold"))
    meta.update(start=round(off + ts, 3), offset=round(off, 3), trim_start=ts, made_at=time.time(), stale=False,
                error=None, realign=False)
    if meta.get("published"):
        meta["unpublished_changes"] = True
    save_meta(d, meta)
    return meta


def restore_official(job_dir):
    """Put the official video back as out.mp4 and drop the replacement (its source too)."""
    b = official_backup(job_dir)
    if not os.path.exists(os.path.join(replace_dir(job_dir), "meta.json")):
        raise RuntimeError("the official video is already the song's video")
    if not os.path.exists(b):
        raise RuntimeError("the official video's copy is missing; remake the song to get it back")
    os.replace(b, os.path.join(job_dir, "out.mp4"))
    shutil.rmtree(replace_dir(job_dir), ignore_errors=True)


def remove_version(job_dir, slug):
    d = version_dir(job_dir, slug)
    if not os.path.isdir(d):
        raise RuntimeError("no such version")
    shutil.rmtree(d)


def put_official_back(job_dir):
    """Before a trim: out.mp4 must be the official render, because trimtools keeps (and later cuts from) a copy of
    whatever out.mp4 is when a song is first trimmed."""
    if os.path.exists(os.path.join(replace_dir(job_dir), "meta.json")) and os.path.exists(official_backup(job_dir)):
        shutil.copyfile(official_backup(job_dir), os.path.join(job_dir, "out.mp4") + ".tmp")
        os.replace(os.path.join(job_dir, "out.mp4") + ".tmp", os.path.join(job_dir, "out.mp4"))


def official_changed(job_dir, realign=False):
    """out.mp4 was just made again from the official video (a trim or a remake): it becomes the official copy, and
    every custom video is marked stale (to re-render; realign=True when the official video itself is a different one,
    so the stored starts no longer mean anything). Returns the keys to re-render."""
    keys = []
    for m in entries(job_dir):
        key = m["slug"] if m["mode"] == "version" else "replace"
        m.update(stale=True, realign=bool(realign or m.get("realign")))
        save_meta(entry_dir(job_dir, key), m)
        keys.append(key)
    if "replace" in keys:
        os.makedirs(os.path.dirname(official_backup(job_dir)), exist_ok=True)
        shutil.copyfile(os.path.join(job_dir, "out.mp4"), official_backup(job_dir) + ".tmp")
        os.replace(official_backup(job_dir) + ".tmp", official_backup(job_dir))
    return keys


def adopt(old_job_dir, new_job_dir):
    """"Change video or lyrics" made a new song in place of an old one: carry its custom videos over (they'll be lined
    up again by sound, since the official video is a different one). Returns how many."""
    n = 0
    od = replace_dir(old_job_dir)
    if load_meta(od):
        os.makedirs(os.path.dirname(replace_dir(new_job_dir)), exist_ok=True)
        shutil.rmtree(replace_dir(new_job_dir), ignore_errors=True)
        shutil.move(od, replace_dir(new_job_dir))
        n += 1
    for d in glob.glob(os.path.join(old_job_dir, "versions", "*")):
        if load_meta(d):
            os.makedirs(os.path.join(new_job_dir, "versions"), exist_ok=True)
            dst = os.path.join(new_job_dir, "versions", os.path.basename(d))
            if not os.path.exists(dst):
                shutil.move(d, dst)
                n += 1
    return n


def public(meta, job_dir):
    """What the page and ks see of one custom video."""
    key = meta["slug"] if meta["mode"] == "version" else "replace"
    return {k: meta.get(k) for k in ("name", "mode", "slug", "source", "start", "offset", "trim_start", "confidence",
                                     "aligned", "drift", "reason", "best_guess", "fill", "lead", "made_at", "stale",
                                     "error", "published", "published_as", "unpublished_changes", "source_duration",
                                     "has_audio")} | {"key": key}
