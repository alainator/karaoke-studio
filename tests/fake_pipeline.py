#!/usr/bin/env python3
"""Stand-in for pipeline.py in the tests (no GPU, no downloads). The job's "fake" field picks what it does:
(or "fake:<mode>" at the end of the title, for the helper PC, which gets only the song's fields)
slow (default): starts a child (like yt-dlp/ffmpeg) and both wait 10 minutes; stubborn: the same, but the child
ignores SIGTERM; ok: writes a finished song's files and exits 0; media: the same with real (tiny) media, lines and
heard words; oom: says it ran out of GPU memory and fails; conf: a finished song with its by-ear confidence (job fields
fake_lines, fake_sure (null: couldn't follow), fake_squeezed: lines crammed into too little time, fake_listen /
fake_written: the language listened in and the lyrics' language, for the video check); baddl: the video
can't be downloaded (YouTube's bot check)."""
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
if mode == "media":  # a real (tiny) song: 12 s of video and vocals, three lines, what was heard (trim tests)
    def ff(*a):
        subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)
    ff("-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=12", "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", os.path.join(d, "out.mp4"))
    ff("-f", "lavfi", "-i", "sine=frequency=660:duration=12", "-c:a", "aac", os.path.join(d, "vocals.m4a"))
    lines = [{"words": [[4.0, 4.5, "one"], [4.5, 5.0, "two"]]}, {"words": [[6.0, 7.0, "three"]], "weak": True, "why": "test"},
             {"words": [[8.0, 9.0, "four"]]}]
    json.dump({"version": 2, "lines": lines}, open(os.path.join(d, "words.json"), "w"))
    json.dump({"lang": "en", "words": [[4.0, 4.4, "one"], [6.1, 6.9, "three"], [8.0, 8.8, "four"]]}, open(os.path.join(d, "heard.json"), "w"))
    open(os.path.join(d, "lyrics.ass"), "w").write("x")
    json.dump({"offset": 0, "lines": 3, "duration": 12.0}, open(os.path.join(d, "result.json"), "w"))
    print("@info made (fake media)", flush=True)
    sys.exit(0)
if mode == "baddl":
    print("@step download", flush=True)
    print("@error yt-dlp failed: ERROR: [youtube] xxxxxxxxxxx: Sign in to confirm you're not a bot", flush=True)
    sys.exit(1)
if mode == "conf":
    n, sq = int(job.get("fake_lines", 10)), int(job.get("fake_squeezed", 0))
    sure = job["fake_sure"] if "fake_sure" in job else n
    lines, t = [], 5.0
    for i in range(n):
        step = 0.05 if i < sq else 0.5
        lines.append({"words": [[round(t + k * step, 3), round(t + (k + 1) * step, 3), w] for k, w in enumerate(("la", "da", "na"))]})
        t += 3.0
    for f in ("out.mp4", "vocals.m4a", "lyrics.ass"):
        open(os.path.join(d, f), "wb").write(b"placeholder " * 10)
    json.dump({"version": 2, "lines": lines}, open(os.path.join(d, "words.json"), "w"))
    # fake_listen: the language it listened in (a language set on the song wins, as in pipeline.py); fake_written:
    # the language the lyrics are written in (the video check compares the two)
    listened = job.get("lang") or job.get("fake_listen", "en")
    if job.get("fake_listen_once") and not job.get("lang"):
        listened = job["fake_listen_once"]
    json.dump({"offset": 0, "lines": n, "duration": t + 5, "lang": listened, "confidence": {"lines": n, "check": 0, "sure": sure},
               "video_check": {"lyrics_lang": job.get("fake_written"), "album": {"state": "none"}, "lyrics_end": t - 3.0}},
              open(os.path.join(d, "result.json"), "w"))
    print("@step render", flush=True)
    print("@info made (fake, with confidence)", flush=True)
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
