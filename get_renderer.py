#!/usr/bin/env python3
"""Fetch the lyric renderer (JavascriptSubtitlesOctopus) and its fonts into static/octopus/.

    python -I get_renderer.py [pikaraoke version]      (default 1.23.0)

The preview uses SubtitlesOctopus (libass in the browser) and its fonts.
They aren't stored in this repo: this script takes them from PiKaraoke's published wheel on PyPI
(pikaraoke/static/js/subtitles-octopus*, default.woff2, and pikaraoke/static/fonts/*).
"""
import io, json, os, sys, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "static", "octopus")
VERSION = sys.argv[1] if len(sys.argv) > 1 else "1.23.0"
WANT = ("subtitles-octopus.js", "subtitles-octopus-worker.js", "subtitles-octopus-worker.wasm",
        "subtitles-octopus-worker-legacy.js", "default.woff2", "COPYRIGHT")

meta = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/pikaraoke/{VERSION}/json", timeout=30))
wheel = next(u for u in meta["urls"] if u["packagetype"] == "bdist_wheel")
print(f"downloading {wheel['filename']} ({wheel['size'] / 1e6:.0f} MB)...")
data = urllib.request.urlopen(wheel["url"], timeout=300).read()
os.makedirs(DEST, exist_ok=True)
got = []
with zipfile.ZipFile(io.BytesIO(data)) as z:
    for name in z.namelist():
        base = os.path.basename(name)
        if (name.startswith("pikaraoke/static/js/") and base in WANT) or \
           (name.startswith("pikaraoke/static/fonts/") and base.lower().endswith((".ttf", ".otf", ".woff2"))):
            with open(os.path.join(DEST, base), "wb") as f:
                f.write(z.read(name))
            got.append(base)
missing = [w for w in WANT if w not in got and w != "subtitles-octopus-worker-legacy.js"]
print("saved:", ", ".join(sorted(got)))
if missing:
    sys.exit(f"missing from this PiKaraoke version: {', '.join(missing)}")
