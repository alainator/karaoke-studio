#!/usr/bin/env python3
"""Rebuild catalog.json (in the library root) and the Name.words.json of every published song, for Encore.
The studio keeps them up to date by itself on publish, version publish, rename and delete; run this once after
deploying, or whenever they look out of date.

    python -I deploy/write-catalog.py --dry                    # say what it would write, write nothing
    python -I deploy/write-catalog.py                          # the configured library (KARAOKE_LIBRARY) and jobs
    python -I deploy/write-catalog.py --library DIR [--jobs DIR] [--folder "Music Videos"]   (a test copy)
    python -I deploy/write-catalog.py --no-words               # only catalog.json

Songs with unpublished changes are listed: their words may be ahead of what was published, so their words file is
only written when it's missing (publishing them again writes it).
"""
import json, logging, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
import catalog  # noqa: E402

args = sys.argv[1:]
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("write-catalog")


def opt(name, default):
    return args[args.index(name) + 1] if name in args else default


DRY, WORDS = "--dry" in args, "--no-words" not in args
LIB = os.path.abspath(opt("--library", C.LIBRARY))
JOBS = os.path.abspath(opt("--jobs", C.JOBS))
MV = os.path.join(LIB, opt("--folder", os.path.relpath(C.MV_DIR, C.LIBRARY)))
print(f"library: {LIB}\nsongs:   {MV}\njobs:    {JOBS}\n{'(dry run: nothing is written)' if DRY else ''}", flush=True)
if not os.path.isdir(MV):
    sys.exit(f"no such folder: {MV}")

written = kept = failed = 0
behind = []
for jid in sorted(os.listdir(JOBS)):
    d = os.path.join(JOBS, jid)
    try:
        job = json.load(open(os.path.join(d, "job.json"), encoding="utf-8"))
    except Exception:
        continue
    bases = []
    if job.get("published_as") and os.path.isfile(os.path.join(MV, job["published_as"] + ".mp4")):
        bases.append(job["published_as"])
    try:
        import videoswap
        bases += [m["published_as"] for m in videoswap.entries(d) if m.get("mode") == "version" and m.get("published_as")
                  and os.path.isfile(os.path.join(MV, m["published_as"] + ".mp4"))]
    except Exception:
        pass
    if not bases:
        continue
    if job.get("unpublished_changes"):
        behind.append(job.get("published_as") or jid)
    if not WORDS or not os.path.exists(os.path.join(d, "words.json")):
        continue
    for base in bases:
        target = os.path.join(MV, base + catalog.WORDS_EXT)
        if job.get("unpublished_changes") and os.path.exists(target):
            kept += 1
            continue
        try:
            if not DRY:
                catalog.write_words(d, job, MV, base)
            written += 1
        except Exception as e:
            failed += 1
            print(f"failed: {base}: {str(e)[:200]}", flush=True)

if DRY:
    n = len(catalog.build(JOBS, LIB, MV, catalog.read_catalog(LIB), log))
    print(f"would write {written} words files and catalog.json with {n} songs")
else:
    n = catalog.write_catalog(JOBS, LIB, MV, log)
    print(f"wrote {written} words files ({kept} kept, {failed} failed) and {catalog.CATALOG} with {n} songs")
if behind:
    print(f"{len(behind)} published songs have unpublished changes (publish them again to update their files):")
    for b in behind:
        print("  " + b)
