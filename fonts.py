"""Fonts for scripts the TV's lyric renderer can't draw on its own.

PiKaraoke's TV page (and the studio's preview) render lyrics with two fonts: Arial (Latin, Cyrillic, Greek, Hebrew,
Arabic) and Droid Sans Fallback (Chinese, Japanese, Korean). For other scripts (Hindi, Thai, Tamil, Bengali,
Georgian…) the subtitle file carries its own font: just the characters the song uses, cut from the free Noto font
for that script (SIL Open Font License), in the subtitle's [Fonts] section. The same renderer reads embedded fonts.

    section, families = embed_for(texts)   # "[Fonts]\\n..." (or ""), {script name: font family to use}
"""
import io, os, re, urllib.request

from romanize import SCRIPTS, SCRIPT_RE

FONT_DIR = os.environ.get("KARAOKE_FONTS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fonts")
URL = "https://github.com/notofonts/notofonts.github.io/raw/main/fonts/{name}/hinted/ttf/{name}-Regular.ttf"


def family_name(file_name):
    """NotoSansDevanagari -> Noto Sans Devanagari"""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", file_name)


def font_path(name):
    """The Noto font file for a script, downloaded once (None if it can't be had)."""
    path = os.path.join(FONT_DIR, f"{name}-Regular.ttf")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    try:
        os.makedirs(FONT_DIR, exist_ok=True)
        req = urllib.request.Request(URL.format(name=name), headers={"User-Agent": "karaoke-studio"})
        data = urllib.request.urlopen(req, timeout=60).read()
        if len(data) < 1000:
            return None
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
        return path
    except Exception:
        return None


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
            font = TTFont(path)
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
