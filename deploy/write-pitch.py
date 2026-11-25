#!/usr/bin/env python3
"""Write <base>.pitch.json (the sung melody, for singing scores in Encore: pitch.py) for songs published before the
studio made them on publish, then rewrite catalog.json so it lists them. Each song's pitch is tracked once from its
vocals and its lead vocal (about 20 s a song on the GPU, with the karaoke model splitting the lead; about 20 s on the
CPU for a 4-minute song, without the lead) and cached in its job folder (pitch.f0.npz). The studio does this itself
for songs without an up-to-date pitch file (at startup and hourly when idle); --force redoes every song, e.g. to give
the songs written before it the melody that follows the lead (pitch.lead_merge). Run it when nobody is singing (it loads the server) and when no song is being made, or
with --cpu to leave the GPU alone. The files it writes carry the instrumental's loudness too (§1.1 "music");
deploy/write-music.py adds that to existing pitch files without tracking the pitch again.

    python -I deploy/write-pitch.py --dry                  # which songs would get a pitch file; writes nothing
    python -I deploy/write-pitch.py --limit 5              # the first 5 songs without one (try it on a few first)
    python -I deploy/write-pitch.py                        # every published song without a pitch file
    python -I deploy/write-pitch.py --jobs ID[,ID...]      # these songs (job ids), even if they have one
    python -I deploy/write-pitch.py --jobs DIR             # another jobs folder (like write-catalog.py)
    python -I deploy/write-pitch.py --force                # write them all again
    python -I deploy/write-pitch.py --cpu                  # CPU only
    --library DIR [--folder "Music Videos"]                # a test copy of the library
"""
import json, logging, os, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
import catalog  # noqa: E402
import pitch  # noqa: E402

args = sys.argv[1:]
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("write-pitch")


def opt(name, default):
    return args[args.index(name) + 1] if name in args else default


DRY, FORCE, CPU = "--dry" in args, "--force" in args, "--cpu" in args
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


def bases_of(d, job):
    out = []
    if job.get("published_as") and os.path.isfile(os.path.join(MV, job["published_as"] + ".mp4")):
        out.append(job["published_as"])
    try:
        import videoswap
        out += [m["published_as"] for m in videoswap.entries(d) if m.get("mode") == "version" and m.get("published_as")
                and os.path.isfile(os.path.join(MV, m["published_as"] + ".mp4"))]
    except Exception:
        pass
    return out


todo = []
for jid in sorted(os.listdir(JOBS)):
    d = os.path.join(JOBS, jid)
    if ONLY is not None and jid not in ONLY:
        continue
    try:
        job = json.load(open(os.path.join(d, "job.json"), encoding="utf-8"))
    except Exception:
        continue
    bases = bases_of(d, job)
    if not bases or job.get("status") != "done" or not os.path.exists(os.path.join(d, "vocals.m4a")):
        continue
    vt = os.path.getmtime(os.path.join(d, "vocals.m4a"))
    fresh = all(os.path.exists(os.path.join(MV, b + pitch.EXT)) and os.path.getmtime(os.path.join(MV, b + pitch.EXT)) >= vt for b in bases)
    if fresh and not FORCE and ONLY is None:
        continue
    todo.append((jid, d, job, bases))
if LIMIT:
    todo = todo[:LIMIT]
print(f"{len(todo)} song(s) to do", flush=True)
done = failed = 0
t_all = time.time()
for n, (jid, d, job, bases) in enumerate(todo, 1):
    if DRY:
        print(f"  {jid}: {', '.join(b + pitch.EXT for b in bases)}")
        continue
    t0 = time.time()
    try:
        pitch.write(d, job, MV, bases, device="cpu" if CPU else None)
        done += 1
        print(f"[{n}/{len(todo)}] {jid}: {len(bases)} file(s) in {time.time() - t0:.1f} s", flush=True)
    except Exception as e:
        failed += 1
        print(f"[{n}/{len(todo)}] {jid}: FAILED ({str(e)[:200]})", flush=True)
if not DRY:
    n = catalog.write_catalog(JOBS, LIB, MV, log)
    print(f"wrote pitch files for {done} song(s) ({failed} failed) in {time.time() - t_all:.0f} s; {catalog.CATALOG} lists {n} songs")
