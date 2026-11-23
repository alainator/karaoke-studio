#!/usr/bin/env python3
"""Draw the lyrics of every finished song again with the current lyric style (after changing assgen.py or the
KARAOKE_LYRICS_* settings): lyrics.ass in each song's display mode, plus lyrics.<mode>.ass per display mode.

    python -I deploy/rebuild-lyrics.py             # rebuild every finished song
    python -I deploy/rebuild-lyrics.py --dry       # only count what would change, write nothing
    python -I deploy/rebuild-lyrics.py --mark      # also mark changed published songs as having unpublished changes
                                                   # (run it while nobody is editing songs in the studio)
    python -I deploy/rebuild-lyrics.py --jobs DIR  # another jobs folder (tests)

Words, timing and the library's files are not touched: published songs keep their old lyrics until they're published
again; the ones that changed are listed at the end.
"""
import json, os, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
from assgen import write_lyrics  # noqa: E402

args = sys.argv[1:]
DRY, MARK = "--dry" in args, "--mark" in args
JOBS = args[args.index("--jobs") + 1] if "--jobs" in args else C.JOBS
rebuilt = same = failed = 0
republish = []
t0 = time.time()
for jid in sorted(os.listdir(JOBS)):
    d = os.path.join(JOBS, jid)
    try:
        job = json.load(open(os.path.join(d, "job.json")))
    except Exception:
        continue
    if job.get("status") != "done" or not os.path.exists(os.path.join(d, "words.json")):
        continue
    try:
        changed = write_lyrics(d, job, dry=DRY)
    except Exception as e:
        failed += 1
        print(f"failed: {jid}: {str(e)[:200]}", flush=True)
        continue
    if not changed:
        same += 1
        continue
    rebuilt += 1
    if job.get("published"):
        names = [job.get("published_as") or f"{job.get('artist')} - {job.get('title')}"]
        vd = os.path.join(d, "versions")
        for v in sorted(os.listdir(vd)) if os.path.isdir(vd) else []:  # published custom-video versions share the lyrics
            try:
                m = json.load(open(os.path.join(vd, v, "meta.json")))
            except Exception:
                continue
            if m.get("published_as"):
                names.append(m["published_as"])
        republish.append((jid, names))
        if MARK and not DRY and not job.get("unpublished_changes"):
            job["unpublished_changes"] = True
            with open(os.path.join(d, "job.json.tmp"), "w") as f:
                json.dump(job, f, indent=1)
            os.replace(os.path.join(d, "job.json.tmp"), os.path.join(d, "job.json"))
print(f"{'would rebuild' if DRY else 'rebuilt'} {rebuilt} songs, {same} already up to date, {failed} failed "
      f"({time.time() - t0:.0f} s)")
if republish:
    print(f"\n{len(republish)} published songs {'would need' if DRY else 'need'} publishing again to show the new lyrics in the library"
          + (" (marked in the studio)" if MARK and not DRY else "") + ":")
    for jid, names in republish:
        print(f"  {jid}: " + " | ".join(names))
