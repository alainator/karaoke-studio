#!/usr/bin/env python3
"""Measure the loudness (loudness.py: EBU R128 LUFS and true peak, contract §1 "loudness", so players can even out
song volumes) of songs published before the studio measured it on publish, then rewrite catalog.json so it lists
it. ffmpeg only (no GPU): about 1-2 s a song on the CPU. Nothing in the library is written but catalog.json: each
measurement goes to its job folder (loudness.json). Songs measured already (the same files) are skipped, so it can
be stopped and run again. It doesn't run while a song is being made: it stops (run it again later).

    python -I deploy/write-loudness.py --dry                  # which songs would be measured; writes nothing
    python -I deploy/write-loudness.py --limit 5              # the first 5 not measured yet (try it on a few first)
    python -I deploy/write-loudness.py                        # every published song not measured yet
    python -I deploy/write-loudness.py --jobs ID[,ID...]      # these songs (job ids), measured again
    python -I deploy/write-loudness.py --jobs DIR             # another jobs folder (like write-catalog.py)
    python -I deploy/write-loudness.py --force                # all of them again
    --library DIR [--folder "Music Videos"]                   # a test copy of the library
"""
import json, logging, os, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
import catalog  # noqa: E402
import loudness  # noqa: E402

args = sys.argv[1:]
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("write-loudness")


def opt(name, default):
    return args[args.index(name) + 1] if name in args else default


DRY, FORCE = "--dry" in args, "--force" in args
LIMIT = int(opt("--limit", "0") or 0)
LIB = os.path.abspath(opt("--library", C.LIBRARY))
MV = os.path.join(LIB, opt("--folder", os.path.relpath(C.MV_DIR, C.LIBRARY)))
jobs_arg = opt("--jobs", "")
JOBS, ONLY = C.JOBS, None
if jobs_arg and os.path.isdir(jobs_arg):
    JOBS = os.path.abspath(jobs_arg)
elif jobs_arg:
    ONLY = {x.strip() for x in jobs_arg.split(",") if x.strip()}
print(f"library: {LIB}\nsongs:   {MV}\njobs:    {JOBS}\n{'(dry run: nothing is written)' if DRY else ''}", flush=True)
if not os.path.isdir(MV):
    sys.exit(f"no such folder: {MV}")


def jobs():
    for jid in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        try:
            yield jid, os.path.join(JOBS, jid), json.load(open(os.path.join(JOBS, jid, "job.json"), encoding="utf-8"))
        except (OSError, ValueError):
            continue


def making():
    """The id of a song being made right now (here or on the helper PC), or None."""
    return next((jid for jid, _, job in jobs() if job.get("status") == "running"), None)


def measured(d, base):
    cur = loudness.current(d, MV, base)
    return bool(cur and (cur["mix_lufs"] is not None or not os.path.isfile(os.path.join(MV, base + loudness.VOCALS_EXT))))


todo = []
for jid, d, job in jobs():
    base = job.get("published_as")
    if ONLY is not None and jid not in ONLY:
        continue
    if not base or not os.path.isfile(os.path.join(MV, base + ".mp4")):
        continue
    if ONLY is None and not FORCE and measured(d, base):
        continue
    todo.append((jid, d, base))
if LIMIT:
    todo = todo[:LIMIT]
print(f"{len(todo)} song(s) to measure", flush=True)
done = failed = 0
stopped = None
t_all = time.time()
checked = 0.0
for n, (jid, d, base) in enumerate(todo, 1):
    if DRY:
        print(f"  {jid}: {base}")
        continue
    if time.time() - checked > 15:   # (every 15 s: reading every job takes a moment)
        stopped, checked = making(), time.time()
        if stopped:
            print(f"stopped: a song is being made ({stopped}); run it again when it's done (measured songs are skipped)")
            break
    t0 = time.time()
    state, cur = loudness.update(d, MV, base, force=True)
    if state == "measured":
        done += 1
        mix = f", with its vocals {cur['mix_lufs']} LUFS" if cur and cur.get("mix_lufs") is not None else ""
        print(f"[{n}/{len(todo)}] {base}: {cur['lufs']} LUFS, peak {cur['peak_db']} dBTP{mix} ({time.time() - t0:.1f} s)",
              flush=True)
    else:
        failed += 1
        print(f"[{n}/{len(todo)}] {base}: FAILED (no sound that ffmpeg could measure)", flush=True)
if not DRY:
    msg = f"measured {done} song(s) ({failed} failed) in {time.time() - t_all:.0f} s"
    if done:
        msg += f"; {catalog.CATALOG} lists {catalog.write_catalog(JOBS, LIB, MV, log)} songs"
    print(msg)
    if stopped:
        sys.exit(2)
