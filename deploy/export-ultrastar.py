#!/usr/bin/env python3
"""Export published songs as an UltraStar song library (ultrastar.py): one folder per song with song.txt, the
instrumental, the guide vocals, the video and the cover, for UltraStar Deluxe, Vocaluxe, Performous, AllKaraoke...

    python -I deploy/export-ultrastar.py OUT_DIR [--q TEXT] [--copy]

--q: only songs whose artist or title contains TEXT. --copy: copy the media instead of hard-linking it (hard links
only work on the same drive; copies are made anyway when they can't be). Songs without a melody track get freestyle
notes only (shown, not scored).
"""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
import catalog  # noqa: E402
import ultrastar  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out"); ap.add_argument("--q", default=""); ap.add_argument("--copy", action="store_true")
    a = ap.parse_args()
    cat = catalog.read_catalog(C.LIBRARY) or {}
    q = a.q.lower()
    songs = [s for s in cat.get("songs") or [] if not q or q in f"{s.get('artist')} {s.get('title')}".lower()]
    os.makedirs(a.out, exist_ok=True)
    done = missing = 0
    for s in songs:
        try:
            ultrastar.export(C.LIBRARY, s, a.out, link=not a.copy)
            done += 1
        except FileNotFoundError:
            missing += 1
    print(f"{done} songs exported to {a.out}" + (f"; {missing} skipped (their files are missing)" if missing else ""))


if __name__ == "__main__":
    main()
