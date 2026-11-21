#!/usr/bin/env python3
"""Compare autopick.py with the Make page's own choosing (run in node from static/index.html) over saved searches,
and optionally with the picks a batch actually used.

    python -I tools/autopick-check.py SEARCHES.json [MORE_SEARCHES.json ...] [--picks PICKS.json] [--overrides FILE]
                                      [--names FILE] [--show N]

SEARCHES.json: {key: /api/search answer}; a later file's keys replace an earlier one's (re-searches).
PICKS.json: [{"key", "yt", "lrc_id", "lyrics_source", "artist", "title"}] (what was made). --overrides: lines
"key|video id or -|lyrics id or -|options" (picks set by hand); --names: lines "key|artist|title" (names set by hand).
Prints agreement per field and every disagreement by category. Needs node for the browser side (the Studio's image
has it). Song names are printed: keep the output out of the repository.
"""
import json, os, sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
import _jsoracle  # noqa: E402


def lines(path):
    if not path:
        return []
    return [l.rstrip("\n").split("|") for l in open(path, encoding="utf-8") if l.strip() and not l.startswith("#")]


def main(argv):
    args, opts, k = [], {}, 0
    while k < len(argv):
        if argv[k].startswith("--"):
            opts[argv[k][2:]] = argv[k + 1]
            k += 2
        else:
            args.append(argv[k])
            k += 1
    searches = {}
    for p in args:
        searches.update(json.load(open(p, encoding="utf-8")))
    keys = list(searches)
    py = _jsoracle.py_picks([searches[k] for k in keys])
    js = _jsoracle.picks([searches[k] for k in keys])
    show = int(opts.get("show", 20))
    if js is None:
        print("no node: the browser side can't be run")
    else:
        diff = [(k, a, b) for k, a, b in zip(keys, py, js) if a != b]
        print(f"Python vs the browser's code: {len(keys) - len(diff)}/{len(keys)} identical")
        for k, a, b in diff[:show]:
            print("  DIFF", k, {f: (a[f], b[f]) for f in a if a[f] != b[f]})
    if not opts.get("picks"):
        return 0
    final = {p["key"]: p for p in json.load(open(opts["picks"], encoding="utf-8"))}
    over = {o[0]: o for o in lines(opts.get("overrides"))}
    names = {n[0]: n for n in lines(opts.get("names"))}
    mine = dict(zip(keys, py))
    tally = defaultdict(Counter)
    detail = defaultdict(list)
    for key, f in final.items():
        p = mine.get(key)
        if p is None:
            tally["missing"]["no saved search"] += 1
            continue
        o = over.get(key)
        hand_v = bool(o and len(o) > 1 and o[1] not in ("-", ""))
        hand_l = bool(o and len(o) > 2 and o[2] not in ("-", ""))
        want_l = f"{f.get('lyrics_source') or 'lrclib'}:{f.get('lrc_id')}"
        for field, got, want, hand in (("video", p["video"], f.get("yt"), hand_v), ("lyrics", p["lyrics"], want_l, hand_l)):
            if got == want:
                tally[field]["same"] += 1
            elif hand:
                tally[field]["set by hand (overrides)"] += 1
            else:
                tally[field]["DIFFERENT"] += 1
                detail[field].append((key, got, want))
        if hand_v and not hand_l and p["lyrics"] != want_l:  # the lyrics pick for the video chosen by hand
            d = searches[key]
            v = next((x for x in d["videos"] if x["id"] == f.get("yt")), None)
            import autopick
            l = autopick.best_lyrics(d, v)
            got = f"{l['source']}:{l['id']}" if l else None
            tally["lyrics for the hand-picked video"]["same" if got == want_l else "DIFFERENT"] += 1
            if got != want_l:
                detail["lyrics for the hand-picked video"].append((key, got, want_l))
        n = names.get(key)
        opt = (o[3] if o and len(o) > 3 else "") or ""
        hand_n = bool(n) or "artist=" in opt or "title=" in opt
        for field in ("artist", "title"):
            if p[field] == f.get(field):
                tally[field]["same"] += 1
            elif hand_n:
                tally[field]["set by hand (names/overrides)"] += 1
            else:
                tally[field]["DIFFERENT"] += 1
                detail[field].append((key, p[field], f.get(field)))
    print(f"\nagainst the {len(final)} picks used:")
    for field in ("video", "lyrics", "lyrics for the hand-picked video", "artist", "title", "missing"):
        if tally[field]:
            tot = sum(tally[field].values())
            print(f"  {field}: " + ", ".join(f"{c} {n} ({100 * n / tot:.1f}%)" for c, n in tally[field].most_common()))
    for field, rows in detail.items():
        print(f"\n{field}: different, not set by hand ({len(rows)})")
        for r in rows[:show]:
            print("  ", r)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
