"""Fonts for the lyrics: the heavy lyric font, and fonts for scripts the TV's lyric renderer can't draw on its own.

PiKaraoke's TV page (and the studio's preview) render lyrics with two fonts: Arial (Latin, Cyrillic, Greek, Hebrew,
Arabic) and Droid Sans Fallback (Chinese, Japanese, Korean). For other scripts (Hindi, Thai, Tamil, Bengali,
Georgian…) the subtitle file carries its own font: just the characters the song uses, cut from the free Noto font
for that script (SIL Open Font License), in the subtitle's [Fonts] section. The same renderer reads embedded fonts.

    section, families = embed_for(texts)   # "[Fonts]\\n..." (or ""), {script name: font family to use}

The lyrics themselves are drawn in a heavy font that reads from across the room: Noto Sans CJK Black (SIL Open Font
License; Latin, kana and kanji in the Japanese cut, Hangul in the Korean one, hanzi in the Chinese ones), downloaded
once and embedded per song as a subset of the characters the song uses (tens of kB, not the 5 MB font):

    fl = lyric_fonts(texts, lang)          # [FontInfo] (main font first) or [] if it can't be had
    fl[0].family, fl[0].bold, fl[0].width("text", size)
    embed_fonts(fl, chars)                 # "[Fonts]\\n..." section with their subsets
"""
import functools, io, os, re, urllib.request

from romanize import SCRIPTS, SCRIPT_RE

FONT_DIR = os.environ.get("KARAOKE_FONTS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fonts")
URL = "https://github.com/notofonts/notofonts.github.io/raw/main/fonts/{name}/hinted/ttf/{name}-Regular.ttf"


def family_name(file_name):
    """NotoSansDevanagari -> Noto Sans Devanagari"""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", file_name)


def font_path(name, url=None, file_name=None):
    """The Noto font file for a script, downloaded once (None if it can't be had)."""
    path = os.path.join(FONT_DIR, file_name or f"{name}-Regular.ttf")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    if _failed.get(path, 0) > __import__("time").time() - 600:  # don't retry a failed download on every save
        return None
    try:
        os.makedirs(FONT_DIR, exist_ok=True)
        req = urllib.request.Request(url or URL.format(name=name), headers={"User-Agent": "karaoke-studio"})
        data = urllib.request.urlopen(req, timeout=60).read()
        if len(data) < 1000:
            return None
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        return path
    except Exception:
        _failed[path] = __import__("time").time()
        return None


_failed = {}


def _uuencode(data):
    """The ASS [Fonts] encoding: 3 bytes -> 4 characters (6 bits + 33), lines of 80."""
    out, line = [], []
    for i in range(0, len(data), 3):
        b = data[i:i + 3]
        n = int.from_bytes(b + bytes(3 - len(b)), "big")
        line.append("".join(chr(((n >> s) & 63) + 33) for s in (18, 12, 6, 0))[: len(b) + 1])
    text = "".join(line)
    return [text[i:i + 80] for i in range(0, len(text), 80)]


def embed_for(texts):
    """[Fonts] section with subsets of the Noto fonts the texts need, and {script: family} for those scripts."""
    from fontTools import subset
    from fontTools.ttLib import TTFont
    joined = "".join(texts)
    lines, families = [], {}
    for script, (_rng, _label, noto, _rtl) in SCRIPTS.items():
        if not noto or not SCRIPT_RE[script].search(joined):
            continue
        path = font_path(noto)
        if not path:
            continue
        try:
            font = TTFont(path, recalcTimestamp=False)
            opts = subset.Options()
            opts.layout_features = ["*"]  # shaping (conjuncts, vowel signs) needs every layout feature
            opts.name_IDs = ["*"]
            opts.notdef_outline = True
            sub = subset.Subsetter(opts)
            sub.populate(text="".join(sorted(set(joined))) + " ")
            sub.subset(font)
            buf = io.BytesIO()
            font.save(buf)
        except Exception:
            continue
        lines.append(f"fontname: {noto}_0.ttf")
        lines += _uuencode(buf.getvalue())
        families[script] = family_name(noto)
    return ("[Fonts]\n" + "\n".join(lines) + "\n") if lines else "", families


# ---- the heavy lyric font ----
CJK_URL = "https://github.com/notofonts/noto-cjk/raw/main/Sans/SubsetOTF/{reg}/NotoSans{reg}-{weight}.otf"
LYRIC_WEIGHT = "Black"
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uac00-\ud7a3\uf900-\ufaff\uff00-\uffef]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")


class FontInfo:
    """A font file with what the subtitle needs: its family name (as libass matches it), whether that face is the
    family's bold, and advance widths for measuring lines."""

    def __init__(self, path):
        from fontTools.ttLib import TTFont
        f = TTFont(path, lazy=True)
        name = f["name"]
        self.path = path
        self.family = name.getDebugName(1)
        self.bold = (name.getDebugName(2) or "").lower() == "bold"
        os2, hhea = f["OS/2"], f["hhea"]
        # libass sizes a font so that its OS/2 win ascent + descent fills the font size
        self.height = (os2.usWinAscent + os2.usWinDescent) or (hhea.ascent - hhea.descent) or f["head"].unitsPerEm
        self.ascent = os2.usWinAscent or hhea.ascent
        self.upem = f["head"].unitsPerEm
        cmap = f.getBestCmap() or {}
        hmtx = f["hmtx"].metrics
        self.adv = {cp: hmtx[g][0] for cp, g in cmap.items() if g in hmtx}
        self.file_name = os.path.basename(path)

    def has(self, ch):
        return ord(ch) in self.adv

    def units(self, ch):
        a = self.adv.get(ord(ch))
        if a is None:
            return None
        return a

    def width(self, text, size, others=()):
        """Width in pixels of text at this ASS font size (characters it lacks are measured in the other fonts, or
        guessed: a full em for CJK, half an em for the rest)."""
        tot = 0.0
        for ch in text:
            a = self.adv.get(ord(ch))
            if a is not None:
                tot += a / self.height
                continue
            for o in others:
                b = o.adv.get(ord(ch))
                if b is not None:
                    tot += b / o.height
                    break
            else:
                tot += (1.0 if _CJK_RE.match(ch) else 0.55) * self.upem / self.height
        return tot * size

    def em(self, size):
        """Pixels per em at this ASS font size."""
        return size * self.upem / self.height


@functools.lru_cache(maxsize=16)
def font_info(path):
    return FontInfo(path)


def cjk_font(reg, weight=None):
    """FontInfo of Noto Sans CJK for a region (JP, KR, SC, TC, HK), downloaded once; None if it can't be had."""
    weight = weight or LYRIC_WEIGHT
    name = f"NotoSans{reg}-{weight}"
    p = font_path(name, CJK_URL.format(reg=reg, weight=weight), f"{name}.otf")
    try:
        return font_info(p) if p else None
    except Exception:
        return None


def lyric_fonts(texts, lang=None):
    """The heavy fonts for these lyrics: the main one first (Japanese cut for Japanese and Latin songs, Korean for
    Korean, Chinese for Chinese), then any others needed for characters the main one lacks. [] if unavailable."""
    joined = "".join(texts)
    if _KANA_RE.search(joined):
        order = ["JP", "KR", "TC", "SC"]
    elif len(_HANGUL_RE.findall(joined)) >= 10:
        order = ["KR", "JP", "TC", "SC"]
    elif _HAN_RE.search(joined):
        order = (["TC", "SC"] if lang in ("yue", "zh-hant") else ["SC", "TC"]) + ["JP", "KR"]
    else:
        order = ["JP"]
    main = cjk_font(order[0])
    if main is None:
        return []
    out = [main]
    need = {ch for ch in joined if _CJK_RE.match(ch) and not main.has(ch)}
    for reg in order[1:]:
        if not need:
            break
        # only fetch another cut when it would draw something the ones we have can't
        f = cjk_font(reg)
        if f is None:
            continue
        got = {ch for ch in need if f.has(ch)}
        if got:
            out.append(f)
            need -= got
    return out


def classic_fonts():
    """The TV's own fonts (Arial; Droid Sans Fallback for CJK), for measuring when the heavy font isn't used."""
    out = []
    for name in ("Arial.ttf", "DroidSansFallback.ttf"):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "octopus", name)
        try:
            out.append(font_info(p) if os.path.exists(p) else None)
        except Exception:
            out.append(None)
    return out


_EMBED_CACHE = {}


def embed_fonts(infos, chars):
    """[Fonts] section lines (no header) with subsets of these fonts for these characters. The same input gives the
    same bytes (no timestamps), so an unchanged song gives an unchanged subtitle file."""
    from fontTools import subset
    from fontTools.ttLib import TTFont
    chars = "".join(sorted(set(chars) | {" "}))
    key = (tuple(i.path for i in infos), chars)
    if key in _EMBED_CACHE:
        return _EMBED_CACHE[key]
    lines = []
    for n, info in enumerate(infos):
        use = "".join(c for c in chars if info.has(c)) if n else chars  # extra fonts: only what they're there for
        if n and not use.strip():
            continue
        try:
            font = TTFont(info.path, recalcTimestamp=False)
            opts = subset.Options()
            opts.layout_features = ["*"]
            opts.name_IDs = ["*"]
            opts.notdef_outline = True
            opts.recalc_timestamp = False
            sub = subset.Subsetter(opts)
            sub.populate(text=use)
            sub.subset(font)
            buf = io.BytesIO()
            font.save(buf)
        except Exception:
            continue
        base, ext = os.path.splitext(info.file_name)
        lines.append(f"fontname: {base}_0{ext}")
        lines += _uuencode(buf.getvalue())
    if len(_EMBED_CACHE) > 32:
        _EMBED_CACHE.clear()
    _EMBED_CACHE[key] = lines
    return lines
