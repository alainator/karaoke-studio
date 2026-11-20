"""<base>.vocals.m4a next to a published song (contract §1): the separated lead vocal, for guide vocals in players
(Encore plays it quietly under the karaoke when a singer wants a little help).

It is the job's vocals.m4a (what the Studio took out of the video's sound), so it is on the song's VIDEO timeline with
the trim already applied (trimtools cuts it with out.mp4), and a version shares it (versions play over the song's own
sound). The pipeline writes it as AAC at 128 kb/s, so it is copied as it is; only a file that isn't AAC, or is much
bigger than that, is made again (AAC, 128 kb/s). A file ffprobe can't read as audio isn't published (null).

Never stops a publish: when the copy fails, or the job has no vocals, the old file of that name goes (a guide that
doesn't match the new video is worse than none) and the catalog says null.
"""
import json, os, shutil, subprocess

EXT = ".vocals.m4a"
SRC = "vocals.m4a"
MAX_KBPS = 160     # above this (or not AAC): made again at 128 kb/s
KBPS = 128


def probe(path):
    """(codec, bit rate in kb/s or None) of the first audio stream, or None if ffprobe can't read the file."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=codec_name,bit_rate:format=bit_rate", "-of", "json", path],
                           capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    st = (d.get("streams") or [None])[0]
    if r.returncode or not st:
        return None
    br = st.get("bit_rate") or (d.get("format") or {}).get("bit_rate")
    try:
        kbps = round(int(br) / 1000)
    except (TypeError, ValueError):
        kbps = None
    return st.get("codec_name"), kbps


def needs_encode(path):
    """True if the file must be made again, False if it can be copied; ValueError if it isn't audio."""
    p = probe(path)
    if p is None:
        raise ValueError("not readable as audio")
    codec, kbps = p
    return codec != "aac" or (kbps is not None and kbps > MAX_KBPS)


def _put(src, dst, encode):
    d, name = os.path.split(dst)
    tmp = os.path.join(d, f".{name}.part.m4a")
    try:
        if encode:
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-map", "0:a:0", "-c:a", "aac", "-b:a", f"{KBPS}k",
                                "-movflags", "+faststart", "-f", "mp4", tmp], capture_output=True, text=True, timeout=600)
            if r.returncode:
                raise RuntimeError("ffmpeg: " + (r.stderr or "").strip()[-200:])
        else:
            shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def remove(mv_dir, bases):
    for b in bases:
        p = os.path.join(mv_dir, b + EXT)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def write(job_dir, mv_dir, bases, log=None):
    """Write <base>.vocals.m4a for each published base (the song's and its versions'). Returns the file names written;
    never raises (on a failure the old files of these names are removed)."""
    bases = [b for b in bases if b]
    if not bases:
        return []
    src = os.path.join(job_dir, SRC)
    try:
        if not os.path.isfile(src) or os.path.getsize(src) == 0:
            raise FileNotFoundError("no vocals.m4a")
        first = os.path.join(mv_dir, bases[0] + EXT)
        _put(src, first, needs_encode(src))
        for b in bases[1:]:  # versions: the same sound
            _put(first, os.path.join(mv_dir, b + EXT), False)
        return [b + EXT for b in bases]
    except Exception as e:
        remove(mv_dir, bases)
        if log:
            log.warning("vocals: no guide vocals for %s (%s)", bases[0], str(e)[:200])
        return []
