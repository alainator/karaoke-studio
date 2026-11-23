#!/usr/bin/env python3
"""How well do the by-ear marks find the lines a person had to fix? (Read-only: the yardstick for any change to the
marks, earcheck.py / linecheck.py, before it ships.)

    python -I deploy/measure-checks.py [--all-edited]

Ground truth: songs a person played through and marked "checked by you" (job.json "reviewed") that kept their
automatic version (words.orig.json). On those, a line the person moved (start > 0.5 s), stretched (end > 0.7 s) or
retyped needed fixing, and an untouched line was looked at and found fine. --all-edited also counts songs a person
edited without marking them checked: more songs, but biased (people mostly look only at marked lines, so an untouched
line there may simply not have been looked at), so read those numbers as an upper bound on how good the marks are.
Agents' edits (signed "ks" or kind "agent") never count, since they follow the marks themselves.

Prints, per kind of mark, how many lines it marked and how many of those were fixed, and how many fixed lines had
no mark (what the marks miss), split by whether the line's text occurs again in the song (repeated choruses are
where by-ear matching can pick the wrong copy). Lists what it left out: songs whose line count changed, remakes.

2026-11-22: 218 edited songs, only 1 marked checked. On the edited set the marks caught 105 of 159 moved lines with
544 "not heard, fits poorly" marks of which 17% were fixed; audio-only signals (words starting on silence, evenly
spread words, filler-only lines) almost never fired on our songs, because the aligner already puts words on voice.
"""
import collections, glob, json, os, re, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config as C  # noqa: E402

ALL = "--all-edited" in sys.argv


def lines_of(path):
    """A words file's lines, whatever its format (assgen reads them all)."""
    from assgen import load_lines
    return load_lines(path)


def text(l):
    return " ".join(w[2] for w in l.get("words") or [])


def key(l):
    return re.sub(r"[\W_]+", " ", text(l).lower()).strip()


def kind_of(why):
    w = why or ""
    return ("late" if w.startswith("late") else "early" if w.startswith("early") else
            "not heard, fits poorly" if "not heard" in w else "fits poorly" if "fit" in w else "a guess" if w else "?")


def main():
    stats = collections.defaultdict(lambda: [0, 0])          # mark kind -> [marked, fixed]
    missed = collections.Counter()                            # (repeated?) -> fixed lines with no mark
    fixed_all, songs, left_out = 0, 0, collections.Counter()
    for f in sorted(glob.glob(os.path.join(C.JOBS, "*", "words.orig.json"))):
        d = os.path.dirname(f)
        try:
            job = json.load(open(os.path.join(d, "job.json")))
            a = [l for l in lines_of(f) if not l.get("backing") and l.get("words")]
            b = [l for l in lines_of(os.path.join(d, "words.json")) if not l.get("backing") and l.get("words")]
        except (OSError, ValueError, KeyError, TypeError):
            left_out["unreadable"] += 1
            continue
        who = (job.get("last_edit") or {}).get("kind")
        if who in ("ks", "agent"):
            left_out["edited by an agent"] += 1
            continue
        if not job.get("reviewed") and not ALL:
            left_out["not marked checked (--all-edited counts them)"] += 1
            continue
        if job.get("remade") and os.path.exists(os.path.join(d, "words.before-remake.json")):
            left_out["remade (the automatic version is from another make)"] += 1
            continue
        if len(a) != len(b):
            left_out["line count changed (lines added or deleted)"] += 1
            continue
        songs += 1
        count = collections.Counter(key(l) for l in a)
        for la, lb in zip(a, b):
            fixed = (abs(lb["words"][0][0] - la["words"][0][0]) > 0.5 or abs(lb["words"][-1][1] - la["words"][-1][1]) > 0.7
                     or text(la) != text(lb))
            fixed_all += fixed
            rep = "repeated" if count[key(la)] > 1 else "once"
            if la.get("weak"):
                s = stats[(kind_of(la.get("why")), rep)]
                s[0] += 1
                s[1] += fixed
            elif fixed:
                missed[rep] += 1
    print(f"{songs} songs ({'every edited song: an upper bound' if ALL else 'marked checked by a person'}), {fixed_all} lines fixed")
    for (k, rep), (n, fx) in sorted(stats.items(), key=lambda x: -x[1][0]):
        print(f"  {k:24} {rep:9} marked {n:5}  fixed {fx:4} ({fx / max(1, n):.0%})")
    for rep, n in missed.items():
        print(f"  fixed but not marked     {rep:9} {n:5}")
    if left_out:
        print("left out: " + "; ".join(f"{n} {why}" for why, n in left_out.most_common()))


if __name__ == "__main__":
    main()
