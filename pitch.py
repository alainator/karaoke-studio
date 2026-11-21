#!/usr/bin/env python3
"""<base>.pitch.json next to a published song (contract §1.1): the lead vocal's melody, for singing scores.

    {"version": 1, "base", "duration", "hop": 0.02, "notes": [[start, end, midi], ...], "range": [low, high]}

Made from the song's separated vocals (vocals.m4a in its job folder, in VIDEO time like the published .ass) in two
steps:
1. frames(): the pitch every 20 ms (CREPE, via torchcrepe), cached in the job folder as pitch.f0.npz until vocals.m4a
   changes (a remake or a trim). The pipeline's own CREPE pass (pipeline.py, held notes) keeps only which frames are
   voiced, so it's computed here: in a separate process (python pitch.py extract JOB_DIR), so the web app never holds
   the GPU. On the GPU (when it has 4 GB free) the "full" model takes about 2 s a song plus 5 s to start; on the CPU
   the "tiny" model takes about 20 s for a 4-minute song.
2. notes(): pure numpy. Voiced frames inside sung words (words.json + the song's timing offset) become notes: cut at
   word boundaries, split where the pitch jumps by more than 0.8 semitone, each note at its median pitch (MIDI, a
   float: 60.0 = middle C). Silence, breaths and anything sung outside the words are gaps. range = the lowest and
   highest notes, ignoring the outermost 2% of singing time (octave slips of the tracker).

Never stops a publish: a song whose pitch can't be made has no pitch file (the catalog says null) and still plays.
"""
import json, os, subprocess, sys, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:  # (gpu.py, when run as a script with -I)
    sys.path.insert(0, HERE)
HOP = 0.02                      # seconds per frame (as the file says)
SR = 16000
EXT = ".pitch.json"
CACHE = "pitch.f0.npz"
VOICED = 0.5                    # CREPE periodicity above this = a sung note
JUMP = 0.8                      # semitones: a new note starts where the pitch moves more than this
MIN_NOTE = 3                    # frames (60 ms): shorter bits are dropped
GPU_MIN_FREE_MIB = 4000


def _vocals(job_dir):
    return os.path.join(job_dir, "vocals.m4a")


def _stamp(path):
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}"


def gpu_free_mib():
    import gpu
    return gpu.free_mib()


def extract(job_dir, device="cpu"):
    """Run CREPE on the job's vocals and write the cache (pitch.f0.npz). Runs in its own process (see frames())."""
    import torch, torchcrepe
    src = _vocals(job_dir)
    stamp = _stamp(src)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    a = torch.from_numpy(np.frombuffer(raw, dtype=np.float32).copy()).unsqueeze(0)
    if device != "cpu":  # a GPU
        model, batch = "full", 1024
    else:
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        model, batch = "tiny", 512
    with torch.no_grad():
        f0, per = torchcrepe.predict(a.to(device), SR, hop_length=int(SR * HOP), fmin=65, fmax=1100, model=model,
                                     batch_size=batch, device=device, return_periodicity=True)
    f0 = f0[0].float().cpu().numpy()
    per = per[0].float().cpu().numpy()
    tmp = os.path.join(job_dir, ".pitch.f0.tmp.npz")
    np.savez_compressed(tmp, f0=f0.astype(np.float32), per=per.astype(np.float16), hop=np.float32(HOP),
                        stamp=np.array(stamp), model=np.array(model))
    os.replace(tmp, os.path.join(job_dir, CACHE))


def _gpu_device():
    """The GPU's torch device name ("cuda" on NVIDIA), without importing torch here (extract runs in its own process)."""
    import gpu
    return os.environ.get(gpu.ENV, "").strip() or "cuda"


def frames(job_dir, timeout=900, log=None, device=None):
    """(f0 in Hz, periodicity) per 20 ms frame of the job's vocals, from the cache or computed now (a subprocess, GPU
    if it has room, else CPU; device "cpu"/"cuda" (or another torch device) forces one). Raises if it can't be made."""
    src = _vocals(job_dir)
    if not os.path.exists(src):
        raise FileNotFoundError("no vocals.m4a")
    cache = os.path.join(job_dir, CACHE)
    stamp = _stamp(src)
    if os.path.exists(cache):
        try:
            z = np.load(cache)
            if str(z["stamp"]) == stamp and abs(float(z["hop"]) - HOP) < 1e-6:
                return z["f0"].astype(np.float64), z["per"].astype(np.float64)
        except Exception:
            pass
    device = device or (_gpu_device() if gpu_free_mib() >= GPU_MIN_FREE_MIB else "cpu")
    t0 = time.time()
    r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "pitch.py"), "extract", job_dir, device],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 and device != "cpu":  # the GPU got busy in between: the CPU does it
        device = "cpu"
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "pitch.py"), "extract", job_dir, device],
                           capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("pitch tracking failed: " + (r.stderr or r.stdout).strip()[-300:])
    if log:
        log.info("pitch: %s tracked on the %s in %.1f s", os.path.basename(job_dir), device.upper(), time.time() - t0)
    z = np.load(cache)
    return z["f0"].astype(np.float64), z["per"].astype(np.float64)


def _median(x, k):
    if len(x) < k:
        return x.copy()
    pad = k // 2
    xp = np.pad(x, pad, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(xp, k), axis=1)


def to_midi(f0):
    f0 = np.asarray(f0, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        m = 69.0 + 12.0 * np.log2(f0 / 440.0)
    m[~np.isfinite(m)] = 0.0
    return m


def notes(f0, per, words, hop=HOP):
    """The notes ([[start, end, midi]], video time) and range ([low, high] or None).
    f0: Hz per frame; per: CREPE periodicity per frame; words: [(start, end)] in video time (None: every voiced
    stretch counts, cut only by pitch jumps)."""
    f0, per = np.asarray(f0, dtype=np.float64), np.asarray(per, dtype=np.float64)
    n = len(f0)
    voiced = (_median(per, 3) > VOICED) & (f0 > 0)
    midi = _median(to_midi(f0), 5)
    spans = words if words is not None else [(0.0, n * hop)]
    out = []
    for ws, we in sorted(spans):
        k0, k1 = max(0, int(np.ceil(ws / hop - 1e-6))), min(n, int(np.floor(we / hop + 1e-6)))
        k = k0
        while k < k1:
            if not voiced[k]:
                k += 1
                continue
            s = k
            while k + 1 < k1 and voiced[k + 1] and abs(midi[k + 1] - midi[k]) <= JUMP:
                k += 1
            if k + 1 - s >= MIN_NOTE:
                a, b = max(ws, s * hop), min(we, (k + 1) * hop)
                m = float(np.median(midi[s:k + 1]))
                if out and abs(out[-1][1] - a) < 1e-6 and abs(out[-1][2] - m) < 0.4:  # one note held over a word cut
                    out[-1][1] = round(b, 2)
                elif b - a > 0.01:
                    out.append([round(a, 2), round(b, 2), round(m, 1)])
            k += 1
    if not out:
        return [], None
    ms = np.array([x[2] for x in out])
    w = np.array([x[1] - x[0] for x in out])
    order = np.argsort(ms)
    cw = np.cumsum(w[order]) / w.sum()
    lo = float(ms[order][np.searchsorted(cw, 0.02)])
    hi = float(ms[order][min(len(ms) - 1, np.searchsorted(cw, 0.98))])
    return out, [round(lo, 1), round(hi, 1)]


def word_spans(job_dir, job):
    """Every sung word's (start, end) in video time: words.json plus the song's timing offset."""
    from assgen import load_lines
    off = float(job.get("offset") or 0.0)
    out = []
    for l in load_lines(os.path.join(job_dir, "words.json")):
        for w in l["words"]:
            a, b = float(w[0]) + off, float(w[1]) + off
            if b > a:
                out.append((a, b))
    return out


def doc(base, duration, note_list, rng):
    return {"version": 1, "base": base, "duration": duration, "hop": HOP, "notes": note_list, "range": rng}


def write(job_dir, job, mv_dir, bases, log=None, device=None):
    """Write <base>.pitch.json for the song's published bases (the song and its versions share sound and timing).
    Returns the files written. On failure the old pitch files of these bases are removed (no score beats a wrong one)
    and the error is raised."""
    try:
        f0, per = frames(job_dir, log=log, device=device)
        ns, rng = notes(f0, per, word_spans(job_dir, job))
    except Exception:
        for b in bases:
            try:
                os.remove(os.path.join(mv_dir, b + EXT))
            except OSError:
                pass
        raise
    d = (job.get("result") or {}).get("duration")
    dur = round(float(d), 1) if d else round(len(f0) * HOP, 1)
    out = []
    for b in bases:
        p = os.path.join(mv_dir, b + EXT)
        tmp = os.path.join(mv_dir, f".{b}{EXT}.part")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc(b, dur, ns, rng), f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, p)
        out.append(b + EXT)
    return out


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "extract":
        extract(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "cpu")
    else:
        sys.exit("usage: pitch.py extract JOB_DIR [cuda|cpu|<torch device>]")
