"""Karaoke .ass from word timings, and the words.json format (shared by the pipeline, the timing slider and
the lyric editor).

words.json, version 2: {"version": 2, "lines": [{"words": [[start, end, "word"], ...], "orig": "原文", "singer": 2}, ...]}
  times in seconds on the video's clock *before* the job's timing offset; "orig" is optional (the original
  Japanese line shown above the romaji); "singer" is optional: 1, 2 or 3 (both), each sung in its own colour.
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
            out.append({"words": [list(w) for w in l.get("words", [])], "orig": l.get("orig") or None,
                        "singer": sg if sg in (1, 2, 3) else None})
        else:
            out.append({"words": [list(w) for w in l], "orig": None, "singer": None})
    return [l for l in out if l["words"]]


def load_lines(path):
    return normalize(json.load(open(path)))


def save_lines(path, lines):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"version": 2, "lines": [{"words": l["words"], **({"orig": l["orig"]} if l.get("orig") else {}),
                                            **({"singer": l["singer"]} if l.get("singer") in (1, 2, 3) else {})}
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


def make_ass(lines, offset=0.0, show_orig=True, video_size=None):
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
        sung = SINGER_FILL.get(lines[i].get("singer"))
        parts = [f"{{\\k{round((start - show) * 100)}{'' if sung is None else chr(92) + '1c' + sung}}}"]
        for j, (a, b, w) in enumerate(words):
            nxt = words[j + 1][0] if j + 1 < len(words) else b
            parts.append(f"{{\\kf{max(1, round((nxt - a) * 100))}}}{clean(w)}{' ' if j + 1 < len(words) else ''}")
        row = "A" if i % 2 == 0 else "B"
        events.append(f"Dialogue: 0,{ts(show)},{ts(h)},Row{row},,0,0,0,,{''.join(parts)}")
        if dual and lines[i]["orig"]:
            events.append(f"Dialogue: 0,{ts(show)},{ts(h)},Orig{row},,0,0,0,,{clean(lines[i]['orig'])}")
    return header(scaled(DUAL_STYLES if dual else LATIN_STYLES, k)) + "\n".join(events) + "\n"
