#!/usr/bin/env python3
"""Draw the lyrics of every finished song again with the current lyric style (after changing assgen.py or the
KARAOKE_LYRICS_* settings): lyrics.ass in each song's display mode, plus lyrics.<mode>.ass per display mode.

    python -I deploy/rebuild-lyrics.py             # rebuild every finished song
    python -I deploy/rebuild-lyrics.py --dry       # only list what would change, write nothing
    python -I deploy/rebuild-lyrics.py --mark      # also mark changed published songs as having unpublished changes
                                                   # (run it while nobody is editing songs in the studio)
    python -I deploy/rebuild-lyrics.py --chinese   # only Chinese songs (Mandarin, Cantonese), each with how many
                                                   # characters it shows differently (KARAOKE_LYRICS_HANZI / the song
                                                   # page) and its original title before -> after
    python -I deploy/rebuild-lyrics.py --non-latin # only songs with original-script lines (Japanese, Korean,
                                                   # Chinese, Thai, Russian...: the songs with display modes)
    python -I deploy/rebuild-lyrics.py --published # only published songs (combines with the others)
    python -I deploy/rebuild-lyrics.py --jobs DIR  # another jobs folder (tests)

Words, timing and the library's files are not touched: published songs keep their old lyrics until they're published
again. The ones that changed are listed at the end (songs that already had unpublished edits listed apart). A song counts as changed when any of its lyric files does: a
song whose lyrics.ass stays the same but gets a new lyrics.<mode>.ass needs publishing again too, for players that
let the singer pick. No lyric text is printed.
"""
import json, os, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
from assgen import load_lines, write_lyrics  # noqa: E402

try:
    import hanzi as HZ  # noqa: E402
except Exception:
    HZ = None

args = sys.argv[1:]
DRY, MARK = "--dry" in args, "--mark" in args
CHINESE, NON_LATIN, PUBLISHED = "--chinese" in args, "--non-latin" in args, "--published" in args
JOBS = args[args.index("--jobs") + 1] if "--jobs" in args else C.JOBS
rebuilt = same = failed = skipped = 0
republish = []
t0 = time.time()
if CHINESE and (HZ is None or not HZ.available()):
    print("note: the Chinese conversion package (opencc-python-reimplemented) isn't installed: the characters stay as "
          "in the lyrics", flush=True)


def chinese_report(job, lines):
    """(characters shown differently, original title before, after, the song's choice) for a Chinese song."""
    import catalog
    origs = [l.get("orig") for l in lines]
    shown, _ = HZ.show_lines(origs, HZ.song_lang(job), HZ.song_choice(job))
    sp = catalog.split_orig(job.get("title"))
    title = job.get("title_orig") or (sp[1] if sp else None) \
        or (job.get("title") if catalog.has_own_script(job.get("title")) else None)
    return HZ.changed_chars(origs, shown), title, catalog.shown_title_orig(job, title) if title else None, HZ.song_choice(job)


for jid in sorted(os.listdir(JOBS)):
    d = os.path.join(JOBS, jid)
    try:
        job = json.load(open(os.path.join(d, "job.json")))
    except Exception:
        continue
    if job.get("status") != "done" or not os.path.exists(os.path.join(d, "words.json")):
        continue
    if PUBLISHED and not job.get("published"):
        continue
    lang = (job.get("result") or {}).get("lang") or job.get("lang")
    if CHINESE and lang not in ("zh", "yue"):
        continue
    lines = []
    if NON_LATIN or CHINESE:
        try:
            lines = load_lines(os.path.join(d, "words.json"))
        except Exception:
            pass
        if NON_LATIN and not any(l.get("orig") for l in lines):
            skipped += 1
            continue
    files = []
    try:
        write_lyrics(d, job, dry=DRY, report=files)
    except Exception as e:
        failed += 1
        print(f"failed: {jid}: {str(e)[:200]}", flush=True)
        continue
    if CHINESE and HZ is not None:
        n, before, after, choice = chinese_report(job, lines)
        print(f"{jid} [{lang}, {choice}]: {n} characters shown differently; original title: {before or '-'} -> "
              f"{after or '-'}; files {'to write' if DRY else 'written'}: {', '.join(files) or 'none'}", flush=True)
    elif files:
        print(f"{jid}: {'would write' if DRY else 'wrote'} {', '.join(files)}", flush=True)
    if not files:
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
        republish.append((jid, names, bool(job.get("unpublished_changes"))))
        if MARK and not DRY and not job.get("unpublished_changes"):
            job["unpublished_changes"] = True
            with open(os.path.join(d, "job.json.tmp"), "w") as f:
                json.dump(job, f, indent=1)
            os.replace(os.path.join(d, "job.json.tmp"), os.path.join(d, "job.json"))
print(f"{'would rebuild' if DRY else 'rebuilt'} {rebuilt} songs, {same} already up to date, {failed} failed"
      + (f", {skipped} without original-script lines left out" if NON_LATIN else "") + f" ({time.time() - t0:.0f} s)")
if republish:
    print(f"\n{len(republish)} published songs {'would need' if DRY else 'need'} publishing again to show the new lyrics in the library"
          + (" (marked in the studio)" if MARK and not DRY else "") + ":")
    for jid, names, _ in [r for r in republish if not r[2]]:
        print(f"  {jid}: " + " | ".join(names))
    held = [r for r in republish if r[2]]
    if held:
        print(f"\n{len(held)} of them already had unpublished changes (edits nobody has published yet): look at "
              "these before publishing them")
        for jid, names, _ in held:
            print(f"  {jid}: " + " | ".join(names))
