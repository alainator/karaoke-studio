"""Trimming a made song: cut the start (and/or the end) off its video, for music videos that open with ads, an intro
skit or a long silent title card, or that run on after the song.

The trim is stored on the job in the ORIGINAL video's time (job["trim"] = {"start": s, "end": e or None}) and the
untrimmed media is kept in <job>/untrimmed/, so a trim can be changed or undone without re-downloading, and a remake
cuts the new render the same way. Line times shift with the cut, so every edit the user made stays where it was sung.
"""
import json
import os
import shutil
import subprocess

import config as C
from assgen import load_cast, load_lines, make_ass, save_lines

MEDIA = ("out.mp4", "vocals.m4a")
LINE_FILES = ("words.json", "words.orig.json", "words.before-check.json", "words.before-remake.json",
              "words.before-remake.auto.json")
KEEP = "untrimmed"


def _ffmpeg(*args):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("ffmpeg: " + (r.stderr or "").strip()[-300:])


def _cut(src, dst, start, end, video):
    span = ["-ss", f"{start:.3f}"] + (["-to", f"{end:.3f}"] if end else [])
    tmp = dst + ".tmp" + os.path.splitext(dst)[1]
    if video:  # re-encoded, so the cut lands on the exact frame (a stream copy can only cut at keyframes)
        _ffmpeg(*span, "-i", src, "-map", "0:v:0", "-map", "0:a:0", *C.VENC, "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", tmp)
    else:
        _ffmpeg(*span, "-i", src, "-c:a", "aac", "-b:a", "128k", tmp)
    os.replace(tmp, dst)


def _duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return float(r.stdout.strip())


def _shift_lines(path, delta, length):
    """Move every word by delta seconds; lines that end up wholly before 0 are dropped, words are kept inside the
    video. Returns how many lines were dropped."""
    if not os.path.exists(path):
        return 0
    lines, cast = load_lines(path), load_cast(path)
    out, dropped = [], 0
    for l in lines:
        words = [[round(a + delta, 3), round(b + delta, 3), w] for a, b, w in l["words"]]
        if words[-1][1] <= 0.05 or words[0][0] >= length:
            dropped += 1
            continue
        words = [[min(max(0.0, a), length), min(max(0.0, b), length), w] for a, b, w in words]
        out.append(dict(l, words=words))
    if out:
        save_lines(path, out, cast)
    return dropped


def _shift_heard(path, delta, length):
    if not os.path.exists(path):
        return
    d = json.load(open(path))
    d["words"] = [[round(s + delta, 3), round(e + delta, 3), w] for s, e, w in d.get("words", [])
                  if 0 <= s + delta and e + delta <= length]
    json.dump(d, open(path, "w"), ensure_ascii=False)


def lines_cut_off(job_dir, old_start, new_start, new_end):
    """Line numbers (1-based) that a trim would cut: sung before the new start or after the new end."""
    lines = load_lines(os.path.join(job_dir, "words.json"))
    bad = []
    for i, l in enumerate(lines, 1):
        a, b = l["words"][0][0] + old_start, l["words"][-1][1] + old_start
        if a < new_start - 0.05 or (new_end and b > new_end + 0.05):
            bad.append(i)
    return bad


def apply_trim(job_dir, new_start, new_end, old_start=0.0, fresh=False, offset=0.0):
    """Cut the song to [new_start, new_end] of the original video (new_end None = to the end).
    old_start: where the current files start in the original video (0 if untrimmed).
    fresh: the media in job_dir was just rendered untrimmed (a remake): it becomes the new untrimmed copy.
    Returns the new duration."""
    keep = os.path.join(job_dir, KEEP)
    os.makedirs(keep, exist_ok=True)
    for m in MEDIA:
        src, kept = os.path.join(job_dir, m), os.path.join(keep, m)
        if fresh or not os.path.exists(kept):
            shutil.copyfile(src, kept)
    full = _duration(os.path.join(keep, "out.mp4"))
    end = new_end if new_end and new_end < full - 0.05 else None
    if new_start <= 0.05 and end is None:  # no trim at all: put the untrimmed media back
        for m in MEDIA:
            shutil.copyfile(os.path.join(keep, m), os.path.join(job_dir, m))
    else:
        for m in MEDIA:
            _cut(os.path.join(keep, m), os.path.join(job_dir, m), new_start, end, video=m.endswith(".mp4"))
    length = (end or full) - new_start
    delta = old_start - new_start
    for f in LINE_FILES:
        if f.startswith("words.before-remake") and fresh:
            continue  # the previous version's lines are already in the trimmed time
        _shift_lines(os.path.join(job_dir, f), delta, length)
    _shift_heard(os.path.join(job_dir, "heard.json"), delta, length)
    for f in ("spectro.png", "spectro.webp"):
        p = os.path.join(job_dir, f)
        if os.path.exists(p):
            os.remove(p)
    rp = os.path.join(job_dir, "result.json")
    r = json.load(open(rp))
    r["duration"] = round(length, 1)
    r["trim"] = {"start": round(new_start, 2), "end": round(end, 2) if end else None, "full": round(full, 1)}
    json.dump(r, open(rp, "w"))
    wp = os.path.join(job_dir, "words.json")
    vs = (r["width"], r["height"]) if r.get("width") else None
    open(os.path.join(job_dir, "lyrics.ass"), "w").write(make_ass(load_lines(wp), offset, video_size=vs, cast=load_cast(wp)))
    return length
