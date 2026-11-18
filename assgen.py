"""Karaoke .ass from word timings, and the words.json format (shared by the pipeline, the timing slider and
the lyric editor).

words.json, version 2: {"version": 2, "lines": [{"words": [[start, end, "word"], ...], "orig": "原文", "singer": 2}, ...]}
  times in seconds on the video's clock *before* the job's timing offset; "orig" is optional (the original
  Japanese line shown above the romaji); "singer" is optional: 1, 2 or 3 (both), each sung in its own colour; "heard"
  (optional) is Whisper's transcription of what is sung on a Japanese line, for the AI romaji check.
  Version 1 (older jobs) was just the list of word lists.
"""
import json, os, re

from romanize import SCRIPT_RE

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


# backing vocals: smaller and in italics, on their own row above the lyrics
BACK_STYLE = "Style: Back,Arial,56,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,-1,0,0,100,100,0,0,1,3,2,2,60,60,262,1"
BACK_STYLE_DUAL = "Style: Back,Arial,52,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,-1,0,0,100,100,0,0,1,3,2,2,60,60,382,1"


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
                        "members": sorted(set(mem)) or None, "wwho": ww if ww and any(ww) else None,
                        "weak": bool(l.get("weak")), "why": (l.get("why") or None) if l.get("weak") else None,
                        "ok": bool(l.get("ok")), "backing": bool(l.get("backing"))})
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
                              **({"weak": True} if l.get("weak") else {}),
                              **({"why": str(l["why"])[:160]} if l.get("weak") and l.get("why") else {}),
                              **({"ok": True} if l.get("ok") and not l.get("weak") else {}),
                              **({"backing": True} if l.get("backing") else {}),
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


RTL_RE = re.compile("[\u0590-\u05ff\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")


LEAD = 2.0    # a line appears up to this long before its first word (when its row is free)
LINGER = 2.0  # and stays up to this long after its last word (when the row isn't needed yet)


def make_ass(lines, offset=0.0, show_orig=True, video_size=None, cast=None, embed_fonts=True):
    """lines: words.json content (v1 or v2). offset shifts everything (+ = later).
    video_size (w, h): on videos narrower than 16:9 the text is scaled down so lines don't wrap as much."""
    lines = normalize(lines)
    dual = show_orig and any(l["orig"] for l in lines)
    k = 1.0
    if video_size and video_size[0] and video_size[1]:
        k = max(0.7, min(1.0, (video_size[0] / video_size[1]) / (16 / 9)) ** 0.5)
    segs = [[(a + offset, b + offset, w) for a, b, w in l["words"]] for l in lines]
    events, hide, lead_segs = [], [], []
    fonts_section, families = "", {}
    if embed_fonts:
        try:
            from fonts import embed_for
            fonts_section, families = embed_for([l["orig"] or "" for l in lines] + [w for l in lines for _, _, w in l["words"]])
        except Exception:
            pass
    lead_starts = [sg[0][0] for k, sg in enumerate(segs) if not lines[k].get("backing")]
    for i, words in enumerate(segs):
        start, end = words[0][0], words[-1][1]
        back = bool(lines[i].get("backing"))
        if back:  # backing vocals: their own row above the lyrics, shown shortly before they're sung
            show, h = max(0.0, start - 0.8), end + 0.3
        else:     # lead lines take turns on the two rows (backing lines don't count)
            n_ = len(lead_segs)
            show = start - LEAD
            if n_ >= 2:
                show = max(show, hide[n_ - 2] + 0.05)   # the row it reuses must be free
            if n_ >= 1:
                show = max(show, lead_segs[n_ - 1][0][0] - 0.2)  # don't appear before the previous line starts
            show = max(0.0, min(show, start))
            # stay up after the last word for as long as the row is free (the line after next needs it LEAD before
            # it starts): timing that is a little early, or a held last note, still leaves the words there to sing
            free = lead_starts[n_ + 2] - LEAD - 0.05 if n_ + 2 < len(lead_starts) else end + LINGER
            h = max(end + 0.4, min(end + LINGER, free))
            hide.append(h); lead_segs.append(words)
        sung = fill_of(lines[i])
        fills = word_fills(lines[i])
        rtl = bool(RTL_RE.search(" ".join(w for _, _, w in words)))
        parts = [f"{{\\k{round((start - show) * 100)}{'' if sung is None else chr(92) + '1c' + sung}{chr(92) + 'i1' if lines[i].get('backing') else ''}}}"]
        cur = sung
        for j, (a, b, w) in enumerate(words):
            nxt = words[j + 1][0] if j + 1 < len(words) else b
            fc = fills[j]
            change = "" if fc == cur or (fc is None and cur is None) else chr(92) + "1c" + (fc or DEFAULT_FILL)
            cur = fc
            kt = "k" if rtl else "kf"
            parts.append(f"{{{change}\\{kt}{max(1, round((nxt - a) * 100))}}}{clean(w)}{' ' if j + 1 < len(words) else ''}")
        if back:
            events.append(f"Dialogue: 0,{ts(show)},{ts(h)},Back,,0,0,0,,{''.join(parts)}")
            continue
        row = "A" if (len(lead_segs) - 1) % 2 == 0 else "B"
        events.append(f"Dialogue: 0,{ts(show)},{ts(h)},Row{row},,0,0,0,,{''.join(parts)}")
        if dual and lines[i]["orig"]:
            fam = next((f for sc, f in families.items() if SCRIPT_RE[sc].search(lines[i]["orig"])), None)
            events.append(f"Dialogue: 0,{ts(show)},{ts(h)},Orig{row},,0,0,0,,{('{' + chr(92) + 'fn' + fam + '}') if fam else ''}{clean(lines[i]['orig'])}")
    styles = DUAL_STYLES if dual else LATIN_STYLES
    if any(l.get("backing") for l in lines):
        styles += "\n" + (BACK_STYLE_DUAL if dual else BACK_STYLE)
    if cast:  # who's who: each name in its colour, at the top for the first seconds of the song
        styles += "\n" + LEGEND_STYLE
        names = "   ".join(f"{{\\1c{MEMBER_FILL[i]}}}{clean(n)}" for i, n in enumerate(cast[:8]))
        until = max(8.0, min(20.0, (segs[0][0][0] if segs else 8.0)))
        events.insert(0, f"Dialogue: 1,{ts(0)},{ts(until)},Legend,,0,0,0,,{names}")
    out = header(scaled(styles, k)) + "\n".join(events) + "\n"
    if fonts_section:  # libass reads embedded fonts from a [Fonts] section placed before [Events]
        out = out.replace("\n[Events]", "\n" + fonts_section + "\n[Events]", 1)
    return out
