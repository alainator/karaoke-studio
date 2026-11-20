#!/usr/bin/env python3
"""Write <base>.vocals.m4a (the guide vocals for Encore: guidevocals.py) for songs published before the studio copied
them on publish, then rewrite catalog.json so it lists them. Each one is a copy of the job's vocals.m4a (about 3-5 MB
a song, AAC 128 kb/s; made again only if it isn't AAC or is much bigger), so it is quick and needs no GPU.

Only songs whose published video is still the job's video get one: a song changed since it was published (a trim, a
remake) has new vocals in its job that don't fit the published video; those are listed as skipped (publish them
again, which writes the right file).

    python -I deploy/write-vocals.py --dry                  # which songs would get a vocals file; writes nothing
    python -I deploy/write-vocals.py --limit 5              # the first 5 songs without one (try it on a few first)
    python -I deploy/write-vocals.py                        # every published song without one
    python -I deploy/write-vocals.py --jobs ID[,ID...]      # these songs (job ids), even if they have one
    python -I deploy/write-vocals.py --jobs DIR             # another jobs folder (like write-catalog.py)
    python -I deploy/write-vocals.py --force                # write them all again
    --library DIR [--folder "Music Videos"]                 # a test copy of the library
"""
import json, logging, os, subprocess, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
import catalog  # noqa: E402
import guidevocals  # noqa: E402

args = sys.argv[1:]
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("write-vocals")


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


def duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def same_video(published, made):
    """The published copy is still the job's video (a byte copy, or the same length: a re-encoded picture)."""
    if not os.path.isfile(made):
        return False
    if os.path.getsize(published) == os.path.getsize(made):
        return True
    a, b = duration(published), duration(made)
    return a is not None and b is not None and abs(a - b) < 0.1


def bases_of(d, job):
    """[(base, the job's video it was published from)]: the song's, then its published versions'."""
    out = []
    if job.get("published_as") and os.path.isfile(os.path.join(MV, job["published_as"] + ".mp4")):
        out.append((job["published_as"], os.path.join(d, "out.mp4")))
    try:
        import videoswap
        out += [(m["published_as"], videoswap.entry_video(d, m["slug"])) for m in videoswap.entries(d)
                if m.get("mode") == "version" and m.get("published_as") and os.path.isfile(os.path.join(MV, m["published_as"] + ".mp4"))]
    except Exception:
        pass
    return out


todo, skipped = [], []
for jid in sorted(os.listdir(JOBS)):
    d = os.path.join(JOBS, jid)
    if ONLY is not None and jid not in ONLY:
        continue
    try:
        job = json.load(open(os.path.join(d, "job.json"), encoding="utf-8"))
    except Exception:
        continue
    pairs = bases_of(d, job)
    src = os.path.join(d, guidevocals.SRC)
    if not pairs or job.get("status") != "done" or not os.path.exists(src):
        continue
    vt = os.path.getmtime(src)
    fresh = all(os.path.exists(os.path.join(MV, b + guidevocals.EXT)) and os.path.getmtime(os.path.join(MV, b + guidevocals.EXT)) >= vt
                for b, _ in pairs)
    if fresh and not FORCE and ONLY is None:
        continue
    ok = [b for b, made in pairs if same_video(os.path.join(MV, b + ".mp4"), made)]
    skipped += [(jid, b) for b, _ in pairs if b not in ok]
    if ok:
        todo.append((jid, d, ok))
if LIMIT:
    todo = todo[:LIMIT]
print(f"{len(todo)} song(s) to do", flush=True)
for jid, b in skipped:
    print(f"  skipped {jid}: '{b}' changed since it was published (publish it again)")
done = failed = 0
t_all = time.time()
for n, (jid, d, bases) in enumerate(todo, 1):
    if DRY:
        print(f"  {jid}: {', '.join(b + guidevocals.EXT for b in bases)}")
        continue
    t0 = time.time()
    files = guidevocals.write(d, MV, bases, log)
    if files:
        done += 1
        size = sum(os.path.getsize(os.path.join(MV, f)) for f in files)
        print(f"[{n}/{len(todo)}] {jid}: {len(files)} file(s), {size / 1e6:.1f} MB in {time.time() - t0:.1f} s", flush=True)
    else:
        failed += 1
        print(f"[{n}/{len(todo)}] {jid}: FAILED (see the warning above)", flush=True)
if not DRY:
    n = catalog.write_catalog(JOBS, LIB, MV, log)
    print(f"wrote vocals files for {done} song(s) ({failed} failed, {len(skipped)} skipped) in {time.time() - t_all:.0f} s; "
          f"{catalog.CATALOG} lists {n} songs")
