#!/usr/bin/env python3
"""Stand-in for pipeline.py in the tests (no GPU, no downloads). The job's "fake" field picks what it does:
(or "fake:<mode>" at the end of the title, for the helper PC, which gets only the song's fields)
slow (default): starts a child (like yt-dlp/ffmpeg) and both wait 10 minutes; stubborn: the same, but the child
ignores SIGTERM; ok: writes a finished song's files and exits 0; oom: says it ran out of GPU memory and fails."""
import json, os, subprocess, sys, time

d = sys.argv[1]
job = json.load(open(os.path.join(d, "job.json")))
mode = job.get("fake") or (job.get("title") or "").partition("fake:")[2] or "slow"  # (the helper gets no "fake" field)
print("@step lyrics", flush=True)
if mode == "ok":
    for f in ("out.mp4", "vocals.m4a", "lyrics.ass"):
        open(os.path.join(d, f), "wb").write(b"x")
    json.dump({"lines": []}, open(os.path.join(d, "words.json"), "w"))
    json.dump({"offset": 0, "lines": 0}, open(os.path.join(d, "result.json"), "w"))
    print("@info made (fake)", flush=True)
    sys.exit(0)
if mode == "oom":
    print("torch.OutOfMemoryError: CUDA out of memory", flush=True)
    sys.exit(1)
ready = os.path.join(d, "child-ready")
code = f"import time; open({ready!r}, 'w').close(); time.sleep(600)"
if mode == "stubborn":  # (it says it's ready only once SIGTERM is ignored, so a quick cancel can't beat it)
    code = f"import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); open({ready!r}, 'w').close(); time.sleep(600)"
child = subprocess.Popen([sys.executable, "-c", code])
while not os.path.exists(ready):
    time.sleep(0.02)
os.remove(ready)
with open(os.path.join(d, "pids.txt"), "w") as f:
    f.write(f"{os.getpid()} {child.pid}\n")
print("@step download", flush=True)
time.sleep(600)
