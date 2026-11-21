"""Karaoke .ass from word timings, and the words.json format (shared by the pipeline, the timing slider and
the lyric editor).

words.json, version 2: {"version": 2, "lines": [{"words": [[start, end, "word"], ...], "orig": "原文", "singer": 2}, ...]}
  times in seconds on the video's clock *before* the job's timing offset; "orig" is optional (the original
  Japanese, Korean, Chinese… line: shown above the romaji, or instead of it, depending on the display mode); "singer" is optional: 1, 2 or 3 (both), each sung in its own colour; "heard"
  (optional) is Whisper's transcription of what is sung on a Japanese line, for the AI romaji check.
  Version 1 (older jobs) was just the list of word lists.
"""
import itertools, json, math, os, re

from romanize import SCRIPT_RE

STYLE_FORMAT = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
                "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
                "MarginL, MarginR, MarginV, Encoding")
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
                        "ok": bool(l.get("ok")), "backing": bool(l.get("backing")),
                        "note": l["note"] if isinstance(l.get("note"), dict) and l["note"].get("text") else None})
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
                              **({"heard": l["heard"]} if l.get("heard") else {}),
                              # a note on the line: {"by", "kind", "text", "at"}, signed by the studio (agents.py)
                              **({"note": l["note"]} if isinstance(l.get("note"), dict) and l["note"].get("text") else {})}
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
KANA_RE = re.compile(r"[\u3040-\u30ff]")


LEAD = 2.0    # a line appears up to this long before its first word (when its row is free)
LINGER = 2.0  # and stays up to this long after its last word (when the row isn't needed yet)

# ---- how the lyrics look (settings: config.py, KARAOKE_LYRICS_*) ----
# display modes for songs with an original-script line ("orig"): both = the original above the romanized line,
# original = only the original, romaji = only the romanized line, kana = Japanese written all in hiragana
MODES = ("both", "original", "romaji", "kana")
STYLE_DEFAULTS = {"sweep": True, "countdown": True, "stagger": True, "cards": True, "font": "heavy", "display": "both"}
COUNT_GAP = 4.0      # a line after a gap at least this long (and the first line) gets countdown dots
COUNT_STEP = 0.6     # one dot goes every this many seconds: four dots = the last 2.4 s
INTERLUDE = 8.0      # instrumental gaps longer than this show "♪ seconds left"
H, W_ = 1080, 1920
MX = 110             # left / right margin (the TV's title-safe area is ~5 %)
MB = 52              # bottom margin
ROW_GAP = 28         # between the two rows
SUB_GAP = 6          # between the original line and its romanized line
PITCH = 0.86         # how far apart stacked lines are, as a share of the font size (Noto's line box has room to spare)
# text sizes as pixels per em (the font size in the file is bigger: Noto's line box is 1.448 em)
EM = {"plain": 68, "romaji": 66, "both_main": 58, "both_orig": 58, "original": 70, "kana": 64, "latin_in_mode": 62}


def style_settings():
    """The lyric style settings from config.py (environment / .env), or the defaults."""
    try:
        import config as C
        return {"sweep": C.LYRICS_SWEEP, "countdown": C.LYRICS_COUNTDOWN, "stagger": C.LYRICS_STAGGER,
                "cards": C.LYRICS_CARDS, "font": C.LYRICS_FONT, "display": C.LYRICS_DISPLAY}
    except Exception:
        return dict(STYLE_DEFAULTS)


def display_modes(lines):
    """The display modes this song can use: [] for songs with no original-script lines, kana only for Japanese."""
    lines = normalize(lines)
    if not any(l.get("orig") for l in lines):
        return []
    return list(MODES) if any(KANA_RE.search(l["orig"] or "") for l in lines) else list(MODES[:3])


class _Fonts:
    """The fonts a subtitle uses: the main (heavy, embedded) font for everything, or the TV's own Arial and Droid
    Sans Fallback when it can't be had."""

    def __init__(self, texts, lang, heavy, embed):
        self.heavy = []
        if heavy and embed:
            try:
                from fonts import lyric_fonts
                self.heavy = lyric_fonts(texts, lang)
            except Exception:
                self.heavy = []
        if self.heavy:
            m = self.heavy[0]
            self.main = self.cjk = m.family
            self.bold = 0 if m.family.lower().endswith(("black", "heavy", "extrabold")) else -1
            self.measure_main = self.measure_cjk = m
            self.others = self.heavy[1:]
        else:
            try:
                from fonts import classic_fonts
                arial, droid = classic_fonts()
            except Exception:
                arial = droid = None
            self.main, self.cjk, self.bold = "Arial", "Droid Sans Fallback", -1
            self.measure_main, self.measure_cjk = arial, droid
            self.others = [x for x in (droid,) if x]

    def size(self, em, cjk=False):
        """ASS font size for this many pixels per em."""
        m = self.measure_cjk if cjk else self.measure_main
        return round(em * (m.height / m.upem if m else 1.15))

    def width(self, text, size, cjk=False):
        m = self.measure_cjk if cjk else self.measure_main
        if m is None:
            return sum((1.0 if ord(c) > 0x2e80 else 0.55) for c in text) * size / 1.15
        return m.width(text, size, self.others)


def _is_cjk_text(t):
    return bool(re.search(r"[\u2e80-\u9fff\uac00-\ud7a3]", t or ""))


def _lines_of(pieces, widths, maxw):
    """Break a row of pieces into 1-3 lines that fit maxw, condensing a little before breaking.
    Returns (list of piece-index lists, scale_x percent)."""
    total = sum(widths)
    n = len(pieces)
    if total <= maxw or n == 1:
        return [list(range(n))], (100 if total <= maxw else max(60, int(maxw / total * 100)))
    if total * 0.82 <= maxw:
        return [list(range(n))], int(maxw / total * 100)
    best = None
    for parts in (2, 3):
        if n < parts:
            break
        # balanced split: the cut points that make the widest line narrowest (lines may only break between pieces)
        cum = [0.0]
        for w in widths:
            cum.append(cum[-1] + w)
        for cuts in itertools.combinations(range(1, n), parts - 1):
            b = (0,) + cuts + (n,)
            wid = max(cum[b[i + 1]] - cum[b[i]] for i in range(parts))
            if best is None or wid < best[0] - 1e-6 or (abs(wid - best[0]) < 1e-6 and parts < len(best[1]) - 1):
                best = (wid, b)
        if best and best[0] * 0.82 <= maxw:
            break
    wid, b = best
    return [list(range(b[i], b[i + 1])) for i in range(len(b) - 1)], (100 if wid <= maxw else max(60, int(maxw / wid * 100)))


def _script_runs(text, families, main):
    """clean(text), with the letters of scripts the main font lacks (Thai, Devanagari…) set in their own embedded font
    and the rest back in the main font."""
    if not families:
        return clean(text)
    out, cur = [], None
    for ch in text:
        fam = next((f for sc, f in families.items() if SCRIPT_RE[sc].match(ch)), None)
        # spaces and marks stay in the run they're in
        if fam != cur and not (fam is None and cur is not None and (ch.isspace() or not ch.isalnum())):
            out.append("{\\fn" + (fam or main) + "}")
            cur = fam
        out.append(clean(ch))
    if cur is not None:
        out.append("{\\fn" + main + "}")
    s = "".join(out)
    return s[len("{\\fn" + main + "}"):] if s.startswith("{\\fn" + main + "}") else s


def _row_text(pieces, groups, lead_cs, kt, base_fill, families=None, main=None):
    """ASS text for one row: the lead-in wait, then each piece with its sweep time and colour, broken into lines."""
    out = [f"{{\\k{lead_cs}}}"]
    cur = base_fill
    for gi, grp in enumerate(groups):
        for pi in grp:
            text, cs, fill = pieces[pi]
            last_in_line = pi == grp[-1]
            if last_in_line and gi + 1 < len(groups):
                text = text.rstrip()
            if not text:  # a word with no piece of its own (the original line split unevenly): just wait
                out.append(f"{{\\k{cs}}}")
                continue
            col = ""
            if fill != cur:
                col = "\\1c" + (fill or DEFAULT_FILL)
                cur = fill
            out.append(f"{{{col}\\{kt}{cs}}}{_script_runs(text, families, main)}")
        if gi + 1 < len(groups):
            out.append("\\N")
    return "".join(out)


def make_ass(lines, offset=0.0, show_orig=True, video_size=None, cast=None, embed_fonts=True, title=None, artist=None,
             mode=None, style=None, lang=None):
    """lines: words.json content (v1 or v2). offset shifts everything (+ = later).
    video_size (w, h): on videos narrower than 16:9 the text is scaled down so lines don't wrap as much.
    title/artist: shown during a long intro. mode: both | original | romaji | kana (songs with original-script lines;
    default both, or romaji when show_orig is False). style: overrides of the settings (see STYLE_DEFAULTS).
    lang: the song's language (Chinese songs: "yue" picks the traditional-character font)."""
    lines = normalize(lines)
    st = dict(STYLE_DEFAULTS); st.update(style_settings()); st.update(style or {})
    modes = display_modes(lines)
    mode = mode or ("both" if show_orig else "romaji")
    if not modes:
        mode = "plain"
    elif mode not in modes:
        mode = "both" if mode != "romaji" else "romaji"
    japanese = "kana" in modes
    k = ax = 1.0
    if video_size and video_size[0] and video_size[1]:
        k = max(0.7, min(1.0, (video_size[0] / video_size[1]) / (16 / 9)) ** 0.5)
        # libass spreads the 1920 x-units over the video's width but keeps letters their shape: on a video narrower than
        # 16:9 every letter takes this many times more x-units (fewer on a wider one)
        ax = (16 / 9) / (video_size[0] / video_size[1])
    segs = [[(a + offset, b + offset, w) for a, b, w in l["words"]] for l in lines]

    # what each line shows: the original line split per word (and its kana), for the modes that light it up
    from lyricmap import line_maps
    spans, kanas = [None] * len(lines), [None] * len(lines)
    for i, l in enumerate(lines):
        if l["orig"]:
            spans[i], _, kanas[i], _ = line_maps(l["orig"], tuple(w for _, _, w in l["words"]), japanese)

    # fonts: the heavy lyric font (subset of every character any mode of this song shows), plus Noto fonts for
    # scripts it lacks (Devanagari, Thai…)
    all_text = [l["orig"] or "" for l in lines] + [w for l in lines for _, _, w in l["words"]] + \
               ["".join(x) for x in kanas if x] + [title or "", artist or "", "♪●0123456789 "] + list(cast or [])
    F = _Fonts(all_text, lang, st.get("font", "heavy") != "classic", embed_fonts)
    font_lines, families = [], {}
    if embed_fonts:
        try:
            from fonts import embed_for, embed_fonts as embed_heavy
            sec, families = embed_for(all_text)
            if F.heavy:
                font_lines += embed_heavy(F.heavy, "".join(all_text))
            if sec:
                font_lines += sec.split("\n", 1)[1].rstrip("\n").split("\n")
        except Exception:
            pass

    # sizes per row kind
    if mode == "plain":
        em_main, em_top, em_solo = EM["plain"], None, EM["plain"]
    elif mode == "romaji":
        em_main, em_top, em_solo = EM["romaji"], None, EM["romaji"]
    elif mode == "both":
        em_main, em_top, em_solo = EM["both_main"], EM["both_orig"], EM["both_main"]
    else:  # original / kana: the line in that script; lines that have no original (English parts) as they are
        em_main, em_top, em_solo = EM[mode], None, EM["latin_in_mode"]
    sz = lambda em, cjk=False: max(10, round(F.size(em, cjk) * k))
    width = lambda text, size, cjk=False: F.width(text, size, cjk) * ax  # in x-units
    maxw = W_ - 2 * MX
    sweep = "kf" if st.get("sweep", True) else "k"
    stagger = bool(st.get("stagger", True))

    def row_pieces(i, kind, words, fills):
        """[(text, centiseconds, fill)] per word for one row of line i: kind words | orig | kana."""
        out = []
        for j, (a, b, w) in enumerate(words):
            nxt = words[j + 1][0] if j + 1 < len(words) else b
            cs = max(1, round((nxt - a) * 100))
            t = w + (" " if j + 1 < len(words) else "") if kind == "words" else (spans[i][j] if kind == "orig" else kanas[i][j])
            out.append([t, cs, fills[j]])
        if kind != "words" and out:
            out[-1][0] = out[-1][0].rstrip()
        return out

    def rows_for(i):
        """The rows line i shows, top to bottom: [(kind, pieces, size, cjk, family override)]."""
        l, words = lines[i], segs[i]
        fills = word_fills(l)
        has = bool(l["orig"])
        fam = None  # (letters of scripts the main font lacks get their font in _script_runs)
        cjk_o = _is_cjk_text(l["orig"])
        if mode in ("plain", "romaji") or not has:
            return [("words", row_pieces(i, "words", words, fills), sz(em_solo), False, None)]
        if mode == "both":
            return [("orig", row_pieces(i, "orig", words, fills), sz(em_top, cjk_o), cjk_o, fam),
                    ("words", row_pieces(i, "words", words, fills), sz(em_main), False, None)]
        if mode == "kana" and kanas[i]:
            return [("kana", row_pieces(i, "kana", words, fills), sz(em_main, True), True, None)]
        return [("orig", row_pieces(i, "orig", words, fills), sz(em_main, cjk_o), cjk_o, fam)]

    def layout(rows):
        """Each row broken into lines that fit, with its width (of its first line) and height."""
        out = []
        for kind, pieces, size, cjk, fam in rows:
            widths = [width(p[0], size, cjk) for p in pieces]
            groups, sx = _lines_of(pieces, widths, maxw)
            first_w = sum(widths[x] for x in groups[0]) * sx / 100
            if len(groups) > 1:  # a broken line's last piece loses its trailing space
                first_w -= width(pieces[groups[0][-1]][0][len(pieces[groups[0][-1]][0].rstrip()):], size, cjk) * sx / 100
            out.append({"kind": kind, "pieces": pieces, "size": size, "cjk": cjk, "fam": fam, "groups": groups,
                        "sx": sx, "first_w": first_w, "h": size * PITCH * len(groups)})
        return out

    # ---- timing: when each line shows, and the countdown dots before lines that come after a gap ----
    lead_idx = [i for i in range(len(lines)) if not lines[i].get("backing")]
    lead_starts = [segs[i][0][0] for i in lead_idx]
    dots = {}      # lead number -> number of dots
    lead_of = []   # lead number -> how long before its first word it may appear
    run_end = None
    for n_, i in enumerate(lead_idx):
        start = segs[i][0][0]
        gap = start - (run_end if run_end is not None else 0.0)
        nd = 0
        if st.get("countdown", True) and (run_end is None or gap >= COUNT_GAP):
            room = start - (run_end + 0.6 if run_end is not None else 0.25)
            nd = max(0, min(4, int(room // COUNT_STEP)))
        dots[n_] = nd
        lead_of.append(max(LEAD, nd * COUNT_STEP + 0.6) if nd else LEAD)
        run_end = max(run_end or 0.0, segs[i][-1][1])

    events = []
    hide, shows, blocks = [], [], []
    for n_, i in enumerate(lead_idx):
        words = segs[i]
        start, end = words[0][0], words[-1][1]
        show = start - lead_of[n_]
        if n_ >= 2:
            show = max(show, hide[n_ - 2] + 0.05)   # the row it reuses must be free
        if n_ >= 1:
            show = max(show, lead_starts[n_ - 1] - 0.2)  # don't appear before the previous line starts
        show = max(0.0, min(show, start))
        # stay up after the last word for as long as the row is free (the line after next needs it before it
        # starts): timing that is a little early, or a held last note, still leaves the words there to sing
        free = lead_starts[n_ + 2] - lead_of[n_ + 2] - 0.05 if n_ + 2 < len(lead_starts) else end + LINGER
        h = max(end + 0.4, min(end + LINGER, free))
        hide.append(h); shows.append(show)
        blocks.append(layout(rows_for(i)))

    # ---- placement: row A (top, left) and row B (bottom, right), each line a block of its rows ----
    def block_h(b):
        return sum(r["h"] for r in b) + SUB_GAP * k * (len(b) - 1)
    # the height of a one-line block in this mode (the top row sits above it unless a taller bottom line is up)
    if mode == "both":
        std = (sz(em_top, True) + sz(em_main)) * PITCH + SUB_GAP * k
    elif mode in ("original", "kana"):
        std = sz(em_main, True) * PITCH
    else:
        std = sz(em_main) * PITCH
    bottom_b = H - MB * k
    tops = {}
    for n_, i in enumerate(lead_idx):
        b = blocks[n_]
        row = "A" if n_ % 2 == 0 else "B"
        if row == "B":
            y = bottom_b
        else:  # above the tallest bottom-row line that is on screen with it
            hb = std
            for m in (n_ - 1, n_ + 1):
                if 0 <= m < len(lead_idx) and shows[m] < hide[n_] and shows[n_] < hide[m]:
                    hb = max(hb, block_h(blocks[m]))
            y = bottom_b - hb - ROW_GAP * k
        if stagger:
            an, x = (1, MX) if row == "A" else (3, W_ - MX)
        else:
            an, x = 2, W_ // 2
        start, show = segs[i][0][0], shows[n_]
        lead_cs = round((start - show) * 100)
        rtl = bool(RTL_RE.search(" ".join(w for _, _, w in segs[i])))
        kt = "k" if rtl else sweep
        base_fill = fill_of(lines[i])
        yy = y
        for r in reversed(b):  # bottom row first, then upwards
            style = "Main" if r["kind"] == "words" else ("Orig" if r["kind"] == "orig" else "Kana")
            fx = "" if r["sx"] == 100 else "\\fscx%d" % r["sx"]
            fc = "" if base_fill is None else "\\1c" + base_fill
            fn = "\\fn" + r["fam"] if r["fam"] else ""
            head = f"{{\\an{an}\\pos({x},{round(yy)}){fx}\\fs{r['size']}{fc}{fn}}}"
            events.append((show, f"Dialogue: 0,{ts(show)},{ts(hide[n_])},{style},,0,0,0,,{head}"
                                 f"{_row_text(r['pieces'], r['groups'], lead_cs, kt, base_fill, families, F.main)}"))
            yy -= r["h"] + SUB_GAP * k
        top = yy + SUB_GAP * k
        tops[n_] = (top, show, hide[n_])
        # countdown dots, just above where the line starts, one going every COUNT_STEP until its first word
        nd = min(dots[n_], int((start - show) // COUNT_STEP + 1e-6))
        if nd:
            tr = b[0]
            left = x if an == 1 else x - tr["first_w"] if an == 3 else x - tr["first_w"] / 2
            # where the line starts: its left end, or its right end for Arabic and Hebrew
            dan, dx = (3, left + tr["first_w"]) if rtl else (1, left)
            col = base_fill or DEFAULT_FILL
            for q in range(nd):
                t0 = start - (nd - q) * COUNT_STEP
                t1 = t0 + COUNT_STEP
                events.append((t0, f"Dialogue: 1,{ts(t0)},{ts(t1)},Dots,,0,0,0,,{{\\an{dan}\\pos({round(dx)},{round(top + 0.02 * sz(40))})"
                                   f"\\1c{col}}}{'●' * (nd - q)}"))

    # ---- backing vocals: their own row above the lyrics, shown shortly before they're sung ----
    def free_top(t0, t1):
        y = bottom_b - std - ROW_GAP * k - std
        for top, s0, s1 in tops.values():
            if s0 < t1 and t0 < s1:
                y = min(y, top)
        return y
    for i, l in enumerate(lines):
        if not l.get("backing"):
            continue
        words = segs[i]
        start, end = words[0][0], words[-1][1]
        show, h = max(0.0, start - 0.8), end + 0.3
        fills = word_fills(l)
        pieces = row_pieces(i, "words", words, fills)
        size = sz(round(em_solo * 0.8))
        widths = [width(p[0], size) for p in pieces]
        groups, sx = _lines_of(pieces, widths, maxw)
        y = free_top(show, h) - ROW_GAP * k * 0.5
        base_fill = fill_of(l)
        fx = "" if sx == 100 else "\\fscx%d" % sx
        fc = "" if base_fill is None else "\\1c" + base_fill
        head = f"{{\\an2\\pos({W_ // 2},{round(y)}){fx}\\fs{size}{fc}\\i1}}"
        rtl = bool(RTL_RE.search(" ".join(w for _, _, w in words)))
        events.append((show, f"Dialogue: 0,{ts(show)},{ts(h)},Back,,0,0,0,,{head}"
                             f"{_row_text(pieces, groups, round((start - show) * 100), 'k' if rtl else sweep, base_fill, families, F.main)}"))

    # ---- title card in the intro, and "♪ seconds left" in long instrumental parts ----
    if st.get("cards", True) and lead_idx:
        back_spans = [(segs[i][0][0] - 0.8, segs[i][-1][1] + 0.3) for i in range(len(lines)) if lines[i].get("backing")]
        busy = lambda t0, t1: any(a < t1 and t0 < b_ for a, b_ in back_spans)
        card_y = bottom_b - std * 0.55
        note_size = sz(46)

        def countdown(t0, t1, until):
            if t1 - t0 < 2.0 or busy(t0, t1):
                return
            t = t0
            while t < t1 - 0.05:
                left = math.ceil(until - t - 1e-6)
                nxt = min(t1, until - (left - 1))
                if nxt - t >= 0.05:
                    events.append((t, f"Dialogue: 1,{ts(t)},{ts(nxt)},Note,,0,0,0,,{{\\an2\\pos({W_ // 2},{round(card_y)})"
                                      f"\\fs{note_size}}}♪ {left}"))
                t = nxt
        first_show = shows[0]
        intro_end = 0.0
        if (title or artist) and first_show >= 4.5 and not busy(0.0, first_show):
            c0, c1 = 0.4, min(first_show - 0.5, 0.4 + 9.0)
            tsize, asize = sz(80), sz(50)
            parts = []
            for txt, size in ((title, tsize), (artist, asize)):
                if not txt:
                    continue
                w = width(txt, size, _is_cjk_text(txt))
                sx = 100 if w <= maxw else max(55, int(maxw / w * 100))
                parts.append(f"{{\\fs{size}\\fscx{sx}}}{_script_runs(txt, families, F.main)}")
            events.append((c0, f"Dialogue: 2,{ts(c0)},{ts(c1)},Card,,0,0,0,,{{\\an5\\pos({W_ // 2},{round(H * 0.42)})\\fad(500,500)}}"
                               + "\\N".join(parts)))
            intro_end = c1
        if first_show - intro_end >= INTERLUDE * 0.5 + 1.0:
            countdown(intro_end + (0.3 if intro_end else 1.0), first_show - 0.3, lead_starts[0])
        run = None
        for n_ in range(1, len(lead_idx)):
            i_prev = lead_idx[n_ - 1]
            run = max(run or 0.0, segs[i_prev][-1][1])
            gap = lead_starts[n_] - run
            if gap > INTERLUDE:
                t0 = max(hide[m] for m in range(n_)) + 0.25
                countdown(t0, shows[n_] - 0.25, lead_starts[n_])

    # ---- styles ----
    def style(name, size, primary="&H0000D7FF", secondary="&H00FFFFFF", outline=None, shadow=2, italic=0, font=None):
        ol = outline if outline is not None else max(2.0, round(size * 0.068, 1))
        return (f"Style: {name},{font or F.main},{size},{primary},{secondary},&H00000000,&H80000000,{F.bold},{italic},0,0,"
                f"100,100,0,0,1,{round(ol * 1, 1)},{round(shadow * k, 1)},7,0,0,0,1")
    s_main = sz(em_main if mode not in ("original", "kana") else em_solo)
    styles = [style("Main", s_main), style("Orig", sz(em_top or em_main, True), font=F.cjk),
              style("Kana", sz(em_main, True), font=F.cjk),
              style("Back", sz(round(em_solo * 0.8)), italic=-1),
              style("Dots", sz(40), primary="&H0000D7FF", secondary="&H0000D7FF"),
              style("Note", sz(46), primary="&H00FFFFFF", secondary="&H00FFFFFF"),
              style("Card", sz(80), primary="&H00FFFFFF", secondary="&H00FFFFFF")]
    if cast:  # who's who: each name in its colour, at the top for the first seconds of the song
        styles.append(scaled(LEGEND_STYLE.replace(",Arial,", f",{F.main},"), k))
        names = "   ".join(f"{{\\1c{MEMBER_FILL[i]}}}{clean(n)}" for i, n in enumerate(cast[:8]))
        until = max(8.0, min(20.0, (segs[0][0][0] if segs else 8.0)))
        events.insert(0, (0.0, f"Dialogue: 1,{ts(0)},{ts(until)},Legend,,0,0,0,,{names}"))
    events.sort(key=lambda e: e[0])
    out = header("\n".join(styles)) + "\n".join(e for _, e in events) + "\n"
    if font_lines:  # libass reads embedded fonts from a [Fonts] section placed before [Events]
        out = out.replace("\n[Events]", "\n[Fonts]\n" + "\n".join(font_lines) + "\n\n[Events]", 1)
    return out


# ---- the song's subtitle files ----
def video_size_of(job):
    r = (job or {}).get("result") or {}
    if r.get("width"):
        return (r["width"], r["height"])
    m = re.match(r"[^,]*,(\d+),(\d+)", r.get("video") or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def job_display(job, lines=None, modes=None):
    """The display mode a song's lyrics.ass uses: the song's own choice, else the studio's default."""
    modes = display_modes(lines) if modes is None else modes
    want = (job or {}).get("lyrics_display") or style_settings().get("display") or "both"
    return want if want in modes else ("both" if modes else None)


def variant_name(mode):
    return f"lyrics.{mode}.ass"


def write_lyrics(job_dir, job=None, lines=None, cast=None, offset=None, style=None, dry=False):
    """Write the song's lyrics.ass (in its display mode) and one lyrics.<mode>.ass per display mode the song has
    (for players that let the singer pick). Returns True if lyrics.ass changed. dry: only tell, write nothing."""
    if job is None:
        job = json.load(open(os.path.join(job_dir, "job.json")))
    wp = os.path.join(job_dir, "words.json")
    if lines is None:
        lines = load_lines(wp)
    if cast is None:
        cast = load_cast(wp) if os.path.exists(wp) else []
    lines = normalize(lines)
    off = job.get("offset", 0.0) if offset is None else offset
    kw = dict(offset=off or 0.0, video_size=video_size_of(job), cast=cast or None, title=job.get("title"),
              artist=job.get("artist"), style=style, lang=((job.get("result") or {}).get("lang") or job.get("lang")))
    modes = display_modes(lines)
    default = job_display(job, lines, modes)
    texts = {m: make_ass(lines, mode=m, **kw) for m in modes}
    main = texts[default] if modes else make_ass(lines, **kw)
    p = os.path.join(job_dir, "lyrics.ass")
    try:
        same = open(p, encoding="utf-8").read() == main
    except OSError:
        same = False
    if dry:
        return not same

    def put(path, text):
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(path + ".tmp", path)
    if not same:
        put(p, main)
    for m in MODES:
        vp = os.path.join(job_dir, variant_name(m))
        if m in texts:
            try:
                if open(vp, encoding="utf-8").read() == texts[m]:
                    continue
            except OSError:
                pass
            put(vp, texts[m])
        elif os.path.exists(vp):
            os.remove(vp)
    return not same
