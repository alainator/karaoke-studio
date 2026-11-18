"""Karaoke .ass from word timings, and the words.json format (shared by the pipeline, the timing slider and
the lyric editor).

words.json, version 2: {"version": 2, "lines": [{"words": [[start, end, "word"], ...], "orig": "原文", "singer": 2}, ...]}
  times in seconds on the video's clock *before* the job's timing offset; "orig" is optional (the original
  Japanese line shown above the romaji); "singer" is optional: 1, 2 or 3 (both), each sung in its own colour; "heard"
  (optional) is Whisper's transcription of what is sung on a Japanese line, for the AI romaji check.
  Version 1 (older jobs) was just the list of word lists.
"""
import json, os

STYLE_FORMAT = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
                "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding")
# gold fill sweeping over white, two alternating rows so the next line is readable before it's sung
LATIN_STYLES = """Style: RowA,Arial,72,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,60,60,170,1
Style: RowB,Arial,72,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,60,60,70,1"""
# with the original (Japanese) line above each romaji line: four rows from the bottom up
DUAL_STYLES = """Style: RowA,Arial,66,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,60,60,232,1
Style: RowB,Arial,66,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,60,60,58,1
Style: OrigA,Droid Sans Fallback,52,&H00F0F0F0,&H00F0F0F0,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,2,2,60,60,312,1
Style: OrigB,Droid Sans Fallback,52,&H00F0F0F0,&H00F0F0F0,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,2,2,60,60,138,1"""


# fill colour per singer (ASS &HBBGGRR): 1 = gold (the default), 2 = sky blue, 3 = both = green
SINGER_FILL = {1: "&H00D7FF&", 2: "&HFFB43C&", 3: "&H78E678&"}
# named members ("cast", up to 8, e.g. from Genius section headers): one colour each; lines sung together keep gold
MEMBER_RGB = ["#3cb4ff", "#ff6b6b", "#78e678", "#b98cff", "#ff9a3c", "#ff7ac8", "#3cdcc8", "#c8f050"]
MEMBER_FILL = ["&H%s%s%s&" % (c[5:7], c[3:5], c[1:3]) for c in (x.upper() for x in MEMBER_RGB)]
TOGETHER_FILL = "&H00D7FF&"
LEGEND_STYLE = "Style: Legend,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,8,60,60,36,1"


DEFAULT_FILL = "&H00D7FF&"  # the styles' sung colour (gold), for words with no singer set


def who_of(x):
    """A per-word singer override: {"singer": 1-3} or {"members": [indices]}, or None (the line's own singer)."""
    if not isinstance(x, dict):
        return None
    m = [i for i in (x.get("members") or []) if isinstance(i, int) and 0 <= i < 8]
    if m:
        return {"members": sorted(set(m))}
    return {"singer": x["singer"]} if x.get("singer") in (1, 2, 3) else None


def word_fills(line):
    """The sung colour of each word: a word's own singer (lines shared by two singers: "A whole new world" begun by
    one and finished by the other) or else the line's."""
    base = fill_of(line)
    ww = line.get("wwho") or []
    return [(fill_of(ww[j]) or DEFAULT_FILL) if j < len(ww) and ww[j] else base for j in range(len(line["words"]))]


def fill_of(line):
    m = [i for i in (line.get("members") or []) if isinstance(i, int) and 0 <= i < 8]
    if len(m) == 1:
        return MEMBER_FILL[m[0]]
    if len(m) > 1:
        return TOGETHER_FILL
    return SINGER_FILL.get(line.get("singer"))


def header(styles):
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
{STYLE_FORMAT}
{styles}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def normalize(data):
    """Accept words.json v1 (list of word lists) or v2; return a list of {"words": [...], "orig": str|None}."""
    lines = data["lines"] if isinstance(data, dict) else data
    out = []
    for l in lines:
        if isinstance(l, dict):
            sg = l.get("singer")
            mem = [i for i in (l.get("members") or []) if isinstance(i, int) and 0 <= i < 8]
            words = [list(w) for w in l.get("words", [])]
            ww = l.get("wwho")
            ww = [who_of(x) for x in ww] if isinstance(ww, list) and len(ww) == len(words) else None
            out.append({"words": words, "orig": l.get("orig") or None,
                        "singer": sg if sg in (1, 2, 3) else None, "heard": l.get("heard") or None,
                        "members": sorted(set(mem)) or None, "wwho": ww if ww and any(ww) else None})
        else:
            out.append({"words": [list(w) for w in l], "orig": None, "singer": None, "heard": None, "members": None, "wwho": None})
    return [l for l in out if l["words"]]


def load_lines(path):
    return normalize(json.load(open(path)))


def load_cast(path):
    """Member names (up to 8) stored with the lines, or []."""
    try:
        d = json.load(open(path))
    except Exception:
        return []
    return [str(x)[:40] for x in (d.get("cast") or [])][:8] if isinstance(d, dict) else []


_KEEP = object()


def save_lines(path, lines, cast=_KEEP):
    if cast is _KEEP:
        cast = load_cast(path) if os.path.exists(path) else []
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": 2, **({"cast": list(cast)[:8]} if cast else {}),
                   "lines": [{"words": l["words"], **({"orig": l["orig"]} if l.get("orig") else {}),
                              **({"singer": l["singer"]} if l.get("singer") in (1, 2, 3) else {}),
                              **({"members": l["members"]} if l.get("members") else {}),
                              **({"wwho": l["wwho"]} if l.get("wwho") and len(l["wwho"]) == len(l["words"]) else {}),
                              **({"heard": l["heard"]} if l.get("heard") else {})}
                             for l in lines]}, f, ensure_ascii=False)
    os.replace(tmp, path)


def ts(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def clean(text):
    # braces and backslashes are ASS override syntax
    return text.replace("{", "(").replace("}", ")").replace("\\", "/")


def scaled(styles, k):
    """Shrink font sizes, outlines and vertical margins by k (for videos narrower than 16:9)."""
    if k >= 0.999:
        return styles
    out = []
    for line in styles.splitlines():
        f = line.split(",")
        f[2] = str(round(int(f[2]) * k))                                   # Fontsize
        f[16], f[17] = (str(round(float(x) * k, 1)) for x in (f[16], f[17]))  # Outline, Shadow
        f[21] = str(round(int(f[21]) * k))                                 # MarginV
        out.append(",".join(f))
    return "\n".join(out)


def make_ass(lines, offset=0.0, show_orig=True, video_size=None, cast=None):
    """lines: words.json content (v1 or v2). offset shifts everything (+ = later).
    video_size (w, h): on videos narrower than 16:9 the text is scaled down so lines don't wrap as much."""
    lines = normalize(lines)
    dual = show_orig and any(l["orig"] for l in lines)
    k = 1.0
    if video_size and video_size[0] and video_size[1]:
        k = max(0.7, min(1.0, (video_size[0] / video_size[1]) / (16 / 9)) ** 0.5)
    segs = [[(a + offset, b + offset, w) for a, b, w in l["words"]] for l in lines]
    events, hide = [], []
    for i, words in enumerate(segs):
        start, end = words[0][0], words[-1][1]
        show = start - 1.5
        if i >= 2:
            show = max(show, hide[i - 2] + 0.05)   # the row it reuses must be free
        if i >= 1:
            show = max(show, segs[i - 1][0][0] - 0.2)  # don't appear before the previous line starts
        show = max(0.0, min(show, start))
        h = end + 0.4
        hide.append(h)
        sung = fill_of(lines[i])
        fills = word_fills(lines[i])
        parts = [f"{{\\k{round((start - show) * 100)}{'' if sung is None else chr(92) + '1c' + sung}}}"]
        cur = sung
        for j, (a, b, w) in enumerate(words):
            nxt = words[j + 1][0] if j + 1 < len(words) else b
            fc = fills[j]
            change = "" if fc == cur or (fc is None and cur is None) else chr(92) + "1c" + (fc or DEFAULT_FILL)
            cur = fc
            parts.append(f"{{{change}\\kf{max(1, round((nxt - a) * 100))}}}{clean(w)}{' ' if j + 1 < len(words) else ''}")
        row = "A" if i % 2 == 0 else "B"
        events.append(f"Dialogue: 0,{ts(show)},{ts(h)},Row{row},,0,0,0,,{''.join(parts)}")
        if dual and lines[i]["orig"]:
            events.append(f"Dialogue: 0,{ts(show)},{ts(h)},Orig{row},,0,0,0,,{clean(lines[i]['orig'])}")
    styles = DUAL_STYLES if dual else LATIN_STYLES
    if cast:  # who's who: each name in its colour, at the top for the first seconds of the song
        styles += "\n" + LEGEND_STYLE
        names = "   ".join(f"{{\\1c{MEMBER_FILL[i]}}}{clean(n)}" for i, n in enumerate(cast[:8]))
        until = max(8.0, min(20.0, (segs[0][0][0] if segs else 8.0)))
        events.insert(0, f"Dialogue: 1,{ts(0)},{ts(until)},Legend,,0,0,0,,{names}")
    return header(scaled(styles, k)) + "\n".join(events) + "\n"
