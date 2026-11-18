#!/usr/bin/env python3
"""Re-encode the videos of songs made before the studio started re-encoding them (run from the studio folder).

    python -I deploy/reencode-videos.py [--dry-run]

Older versions copied YouTube's video stream as downloaded. Chrome-based browsers (Chrome, Brave, Edge) hit a decode
error after jumping backwards in it, and the picture freezes while the sound goes on, which makes the lyrics editor
unusable. This re-encodes each finished song's video (x264, the same settings new songs get), leaving the sound,
lyrics and edits alone. Songs already published in PiKaraoke get the fixed video the next time you publish them.
Takes ~15-40 s a song; songs that are queued or being made are skipped.
"""
import glob, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config as C  # noqa: E402

dry = "--dry-run" in sys.argv


def write_json(path, data):  # atomic: the studio may read the file at any moment
    json.dump(data, open(path + ".tmp", "w"), indent=1)
    os.replace(path + ".tmp", path)


todo = []
for d in sorted(glob.glob(os.path.join(C.JOBS, "*", ""))):
    try:
        job, res = json.load(open(d + "job.json")), json.load(open(d + "result.json"))
    except Exception:
        continue
    if job.get("status") == "done" and os.path.exists(d + "out.mp4") and res.get("encoded") != "x264":
        todo.append((d, job, res))
print(f"{len(todo)} song(s) to re-encode" + (" (dry run)" if dry else ""))
for k, (d, job, res) in enumerate(todo, 1):
    name = f"{job.get('artist')} - {job.get('title')}"
    if dry:
        print(f"  {name}"); continue
    t0 = time.time()
    tmp = d + "out.reenc.mp4"
    r = subprocess.run(["nice", "-n", "10", "ffmpeg", "-v", "error", "-y", "-i", d + "out.mp4", "-map", "0:v:0", "-map", "0:a:0",
                        *C.VENC, "-c:a", "copy", "-movflags", "+faststart", tmp], capture_output=True, text=True)
    if r.returncode or not os.path.exists(tmp):
        print(f"  [{k}/{len(todo)}] {name}: failed ({(r.stderr or '').strip()[-200:]})")
        if os.path.exists(tmp):
            os.remove(tmp)
        continue
    try:  # the job may have been remade or deleted meanwhile
        if json.load(open(d + "job.json")).get("status") != "done":
            os.remove(tmp); print(f"  [{k}/{len(todo)}] {name}: changed meanwhile, skipped"); continue
    except Exception:
        continue
    os.replace(tmp, d + "out.mp4")
    res["encoded"] = "x264"
    write_json(d + "result.json", res)
    job = json.load(open(d + "job.json"))
    job["video_v"] = time.time()  # new address for the video, so browsers don't play a cached old copy
    write_json(d + "job.json", job)
    print(f"  [{k}/{len(todo)}] {name}: done in {time.time() - t0:.0f}s")
