#!/usr/bin/env python3
"""Add the instrumental's loudness ("music", contract §1.1: RMS dBFS per 0.1 s of the published video's sound, for
singing scores in Encore) to <base>.pitch.json files written before the studio added it on publish. Each file is
patched in place (everything else in it stays as it is); the loudness comes from the published <base>.mp4 next to it
(ffmpeg and numpy: about a second a song, no GPU). catalog.json doesn't change (it lists the pitch files, not what's in
them). Songs whose pitch file is missing need deploy/write-pitch.py, which now writes the loudness too.

    python -I deploy/write-music.py --dry                  # which pitch files would get it; writes nothing
    python -I deploy/write-music.py --limit 5              # the first 5 without it (try it on a few first)
    python -I deploy/write-music.py                        # every pitch file without it
    python -I deploy/write-music.py --force                # all of them again
    --library DIR [--folder "Music Videos"]                # a test copy of the library
"""
import json, os, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
import pitch  # noqa: E402

args = sys.argv[1:]


def opt(name, default):
    return args[args.index(name) + 1] if name in args else default


DRY, FORCE = "--dry" in args, "--force" in args
LIMIT = int(opt("--limit", "0") or 0)
LIB = os.path.abspath(opt("--library", C.LIBRARY))
MV = os.path.join(LIB, opt("--folder", os.path.relpath(C.MV_DIR, C.LIBRARY)))
print(f"library: {LIB}\nsongs:   {MV}\n{'(dry run: nothing is written)' if DRY else ''}", flush=True)
if not os.path.isdir(MV):
    sys.exit(f"no such folder: {MV}")

todo, no_video = [], []
for f in sorted(os.listdir(MV)):
    if not f.endswith(pitch.EXT) or f.startswith("."):
        continue
    base = f[:-len(pitch.EXT)]
    try:
        d = json.load(open(os.path.join(MV, f), encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if "music" in d and not FORCE:
        continue
    if not os.path.isfile(os.path.join(MV, base + ".mp4")):
        no_video.append(base)
        continue
    todo.append((base, d))
if LIMIT:
    todo = todo[:LIMIT]
print(f"{len(todo)} pitch file(s) to do" + (f"; {len(no_video)} without their video (skipped)" if no_video else ""), flush=True)
done = failed = 0
t_all = time.time()
for n, (base, d) in enumerate(todo, 1):
    if DRY:
        print(f"  {base}{pitch.EXT}")
        continue
    t0 = time.time()
    mus = pitch.music(os.path.join(MV, base + ".mp4"))
    if not mus:
        failed += 1
        print(f"[{n}/{len(todo)}] {base}: FAILED (the video's sound couldn't be read)", flush=True)
        continue
    d["music"] = mus
    p = os.path.join(MV, base + pitch.EXT)
    tmp = os.path.join(MV, f".{base}{pitch.EXT}.part")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)
    done += 1
    print(f"[{n}/{len(todo)}] {base}: {len(mus['db'])} values in {time.time() - t0:.1f} s", flush=True)
if not DRY:
    print(f"added the loudness to {done} pitch file(s) ({failed} failed) in {time.time() - t_all:.0f} s")
