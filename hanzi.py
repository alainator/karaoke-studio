"""Chinese characters as players show them: Traditional (the default), Simplified, or as the lyrics have them.

Lyrics come from LRCLIB and kara.moe written either way (some songs simplified, some traditional, a few mixed); party
guests asked for traditional characters. This only changes what is SHOWN: the hanzi rows of the subtitles, the
published words' "orig" rows and the original title. words.json, the aligner's text and the pinyin / jyutping are
made from the lyrics as they were and stay exactly the same.

    choice = song_choice(job)                       # traditional | simplified | lyrics (the song's, else the Studio's)
    shown, script = show_lines(origs, lang, choice) # one per line (None stays None); script: traditional | simplified | None
    shown_title = show_text(title, lang, choice)

Conversion: OpenCC's dictionaries (opencc-python-reimplemented, Apache-2.0), phrase-aware so 头发 -> 頭髮, 干杯 ->
乾杯. Mandarin -> Taiwan standard characters (s2tw), Cantonese -> Hong Kong (s2hk), Simplified: t2s. Not s2twp: its
phrase step swaps words (消息 -> 訊息, 内存 -> 記憶體), which is not what is sung and no longer matches the pinyin
row (one syllable per character).
Never worse than the lyrics:
- a song (or title) already written in the target script is left exactly as it is (OpenCC would still swap a few
  regional variants: 台 -> 臺, 里 -> 裏, 說 -> 説);
- in a mostly-simplified song, a line that already has traditional-only characters is left as it is;
- a line whose length would change is left as it is (each character lights up with its own syllable);
- only Chinese songs (zh, yue): Japanese kanji are never touched;
- without the package (an old image or helper), nothing changes.
"""
import functools

CHOICES = ("traditional", "simplified", "lyrics")
LABELS = {"traditional": "Traditional", "simplified": "Simplified", "lyrics": "As in the lyrics"}
CHINESE = ("zh", "yue")


@functools.lru_cache(maxsize=8)
def _cc(name):
    try:
        from opencc import OpenCC
        try:
            return OpenCC(name)
        except Exception:
            return OpenCC(name + ".json")  # the official opencc wheel names its configs with .json
    except Exception:
        return None


def available():
    return _cc("s2t") is not None


def _conv(name, text):
    c = _cc(name)
    return c.convert(text) if c and text else text


def _diff(a, b):
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


def written_in(text):
    """traditional | simplified: which way the text is written (more characters only one of the two has), or None
    (nothing that differs: the same in both, or no Chinese at all)."""
    if not text or _cc("s2t") is None:
        return None
    t = _diff(text, _conv("t2s", text))   # characters a simplified text wouldn't have
    s = _diff(text, _conv("s2t", text))   # characters a traditional text wouldn't have
    if t > s:
        return "traditional"
    if s > t:
        return "simplified"
    return None


def config_for(lang, choice):
    """The OpenCC conversion for a Chinese song, or None (not Chinese, or shown as in the lyrics)."""
    if lang not in CHINESE or choice not in ("traditional", "simplified"):
        return None
    if choice == "simplified":
        return "t2s"
    return "s2hk" if lang == "yue" else "s2tw"


def studio_choice():
    try:
        import config as C
        v = (getattr(C, "LYRICS_HANZI", "") or "").strip().lower()
    except Exception:
        v = ""
    return v if v in CHOICES else "traditional"


def song_choice(job):
    """The song's own choice (its page), else the Studio's (KARAOKE_LYRICS_HANZI, Traditional by default)."""
    v = (job or {}).get("hanzi")
    return v if v in CHOICES else studio_choice()


def song_lang(job):
    return ((job or {}).get("result") or {}).get("lang") or (job or {}).get("lang")


def show_lines(origs, lang, choice):
    """The lines as shown: (list like origs, script of what is shown: traditional | simplified | None)."""
    origs = list(origs)
    joined = "\n".join(o for o in origs if o)
    if lang not in CHINESE or not joined:
        return origs, None
    have = written_in(joined)
    name = config_for(lang, choice)
    if name is None or have == choice:
        return origs, have
    out = []
    for o in origs:
        if not o:
            out.append(o)
            continue
        if choice == "traditional" and _diff(o, _conv("t2s", o)):
            out.append(o)  # already has traditional characters (a mixed song): as written
            continue
        n = _conv(name, o)
        out.append(n if len(n) == len(o) else o)
    shown = "\n".join(o for o in out if o)
    return out, (written_in(shown) or choice)


def show_text(text, lang, choice):
    """One text (a title) as shown, judged on its own."""
    return show_lines([text], lang, choice)[0][0] if text else text


def font_lang(lang, script):
    """The language hint fonts.lyric_fonts takes: the traditional-character cut first when traditional is shown."""
    if lang in CHINESE and script == "traditional":
        return "zh-hant"
    if lang in CHINESE and script == "simplified":
        return "zh-hans"
    return lang


def changed_chars(before, after):
    """How many characters differ (for reports)."""
    return sum(_diff(a or "", b or "") for a, b in zip(before, after))
