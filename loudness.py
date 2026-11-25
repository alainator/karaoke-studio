"""The loudness of a published song (contract §1 `loudness`), so players can even out song volumes: a loud music
video after a quiet one shouldn't blast (people turn the TV up for the quiet ones). The Studio MEASURES, players
APPLY a gain at playback; the published files are never re-encoded (a synced player would download them again).

Measured with ffmpeg's ebur128 filter (EBU R128 / ITU-R BS.1770): the integrated loudness in LUFS and the true peak
in dBTP of
- the published video's sound (the instrumental: what plays during karaoke), and
- that sound with the guide vocals (<base>.vocals.m4a) mixed in at full, as players play a song with its vocals
  (Encore's idle music, guide vocals on Full): "mix_lufs", "mix_peak_db" (null without a vocals file).
CPU only, about a second per song (ffmpeg decodes the sound alone, much faster than real time).

The measurement is kept in the job folder (loudness.json, written atomically: never job.json, which the running
studio writes too) with the byte sizes of the files it measured. It counts only while those files are unchanged: a
song published again with another video (a remake, a trim) has none until it is measured again, which publishing
does by itself (app.py) and deploy/write-loudness.py does for the songs published before.

    catalog entry (and each version: versions play over the song's own sound, so they get the song's):
    "loudness": {"v": 1, "lufs": -11.8, "peak_db": -0.4, "mix_lufs": -10.9 | null, "mix_peak_db": 0.2 | null}
                or null (not measured, or silent)
"""
import json, os, re, shutil, subprocess, time

V = 1
FILE = "loudness.json"
FLOOR = -70.0          # ebur128's integrated loudness of silence (everything gated out): no measurement
TIMEOUT = 300
VOCALS_EXT = ".vocals.m4a"   # (guidevocals.EXT)

_I = re.compile(r"^\s*I:\s+(-?inf|nan|-?\d+(?:\.\d+)?)\s+LUFS", re.M)
_PEAK = re.compile(r"^\s*Peak:\s+(-?inf|nan|-?\d+(?:\.\d+)?)\s+dBFS", re.M)


def _num(s):
    try:
        x = float(s)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def parse(stderr):
    """(LUFS, true peak dBTP) from ebur128's summary (the last one printed), or None when it has no loudness."""
    i, p = _I.findall(stderr or ""), _PEAK.findall(stderr or "")
    lufs = _num(i[-1]) if i else None
    if lufs is None or lufs <= FLOOR:
        return None
    return round(lufs, 1), (round(_num(p[-1]), 1) if p and _num(p[-1]) is not None else None)


def _run(inputs, graph):
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-v", "info"]
    for p in inputs:
        cmd += ["-i", p]
    cmd += ["-filter_complex", graph, "-map", "[o]", "-f", "null", "-"]
    if shutil.which("nice"):
        cmd = ["nice", "-n", "10"] + cmd   # (never in the way of the studio itself)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse(r.stderr) if r.returncode == 0 else None


EBU = "ebur128=peak=true:framelog=verbose"   # (framelog=verbose: only the summary at the default log level)
FMT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"


def measure_file(path):
    """(LUFS, true peak) of a file's first audio stream, or None (no sound, silent, unreadable)."""
    return _run([path], f"[0:a:0]{EBU}[o]")


def measure_mix(video, vocals):
    """(LUFS, true peak) of the video's sound with the vocals at full on top (as players mix them), or None."""
    return _run([video, vocals], f"[0:a:0]{FMT}[a];[1:a:0]{FMT}[b];"
                                 f"[a][b]amix=inputs=2:duration=first:normalize=0,{EBU}[o]")


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return None


def measure(mv_dir, base):
    """The job-side record for the published <base>.mp4 (and its vocals), or None when its sound can't be measured."""
    video, vocals = os.path.join(mv_dir, base + ".mp4"), os.path.join(mv_dir, base + VOCALS_EXT)
    size, vsize = _size(video), _size(vocals)
    m = measure_file(video)
    if not m:
        return None
    mix = measure_mix(video, vocals) if vsize else None
    if (size, vsize) != (_size(video), _size(vocals)):   # published again meanwhile: this reading is the old file's
        return None
    return {"v": V, "base": base, "size": size, "vocals_size": vsize if mix else None,
            "lufs": m[0], "peak_db": m[1], "mix_lufs": mix[0] if mix else None, "mix_peak_db": mix[1] if mix else None,
            "at": round(time.time())}


def read(job_dir):
    try:
        d = json.load(open(os.path.join(job_dir, FILE), encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def write(job_dir, rec):
    p = os.path.join(job_dir, FILE)
    tmp = p + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, separators=(",", ":"))
    os.replace(tmp, p)


def current(job_dir, mv_dir, base, rec=None):
    """The catalog's "loudness" of the published <base>, or None: only while the measured files are unchanged (the
    same byte sizes; the mix only while the vocals are)."""
    rec = rec if rec is not None else read(job_dir)
    if not rec or rec.get("v") != V or not isinstance(rec.get("lufs"), (int, float)):
        return None   # ("base" isn't checked: it only says which; a rename keeps the files)
    if rec.get("size") is None or rec.get("size") != _size(os.path.join(mv_dir, base + ".mp4")):
        return None
    mix = rec.get("mix_lufs") is not None and rec.get("vocals_size") == _size(os.path.join(mv_dir, base + VOCALS_EXT))
    return {"v": V, "lufs": rec["lufs"], "peak_db": rec.get("peak_db"),
            "mix_lufs": rec.get("mix_lufs") if mix else None, "mix_peak_db": rec.get("mix_peak_db") if mix else None}


def update(job_dir, mv_dir, base, force=False):
    """Measure the published song unless its measurement is current (force: again anyway). Returns
    ("kept" | "measured" | "failed", the catalog field or None). A failure leaves the old record (it no longer
    matches the files, so the catalog says null)."""
    if not force:
        cur = current(job_dir, mv_dir, base)
        if cur and (cur["mix_lufs"] is not None or not os.path.isfile(os.path.join(mv_dir, base + VOCALS_EXT))):
            return "kept", cur
    rec = measure(mv_dir, base)
    if not rec:
        return "failed", None
    write(job_dir, rec)
    return "measured", current(job_dir, mv_dir, base, rec)
