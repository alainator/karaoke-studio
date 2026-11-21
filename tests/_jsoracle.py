"""Run the Make page's own choosing code (static/index.html) in node, for comparing with autopick.py.

The functions are cut out of index.html as they are (no copy kept here): usable, bestLyrics, the default-video
choice in renderResults, tidyName, titleCase, namesFromVideo, autoNames. picks(searches) -> [{"video": id or None,
"lyrics": "source:id" or None, "artist", "title", "names_from"}], or None when there's no node."""
import json, os, re, shutil, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(os.path.dirname(HERE), "static", "index.html")


def _block(src, prefix):
    """From prefix up to the brace that closes the first '{' after it."""
    start = src.index(prefix)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise ValueError("unbalanced")


def _line(src, prefix):
    i = src.index(prefix)
    return src[i:src.index("\n", i)]


def script():
    src = open(INDEX, encoding="utf-8").read()
    parts = [_line(src, "const usable = "), _line(src, "const lkey = "),
             _block(src, "function bestLyrics("), _line(src, "const NATIVE = "), _line(src, "const JUNK = "),
             _block(src, "function tidyName("), _line(src, "const titleCase = "), _block(src, "function namesFromVideo("),
             _block(src, "function autoNames(")]
    rr = _block(src, "function renderResults(")
    m = re.search(r"if \(!pick\.video\) \{\n(.*?)\n  \}\n", rr, re.S)  # the default video
    video = m.group(1)
    return "\n".join(parts) + """
let pick = {};
function choose(d) {
  pick = {video: null, lyr: null, data: d};
""" + video + """
  pick.lyr = bestLyrics(d, pick.video);
  const n = autoNames(d);
  return {video: pick.video ? pick.video.id : null, lyrics: pick.lyr ? lkey(pick.lyr) : null, artist: n.artist, title: n.title, names_from: n.from};
}
let input = "";
process.stdin.on("data", c => input += c);
process.stdin.on("end", () => { process.stdout.write(JSON.stringify(JSON.parse(input).map(choose))); });
"""


def node():
    return shutil.which("node")


def picks(searches):
    exe = node()
    if not exe:
        return None
    r = subprocess.run([exe, "-e", script()], input=json.dumps(searches), capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(r.stderr[-2000:])
    return json.loads(r.stdout)


def py_picks(searches):
    import autopick
    out = []
    for d in searches:
        p = autopick.choose(d)
        out.append({"video": p["video"]["id"] if p["video"] else None,
                    "lyrics": f"{p['lyrics']['source']}:{p['lyrics']['id']}" if p["lyrics"] else None,
                    "artist": p["artist"], "title": p["title"], "names_from": p["names_from"]})
    return out
