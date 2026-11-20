"""Romanization of non-Latin lyrics into what is SUNG (Korean, Mandarin), for display and for the word aligner.

Korean: spelled Hangul is first turned into pronounced Hangul with the standard sound-change rules (nasalization,
liquidization, aspiration, palatalization), then romanized (Revised Romanization, korean-romanizer) so the result
follows what is sung: 묻는 -> munneun, 몰라 -> molla, 끝이 -> kkeuchi, 답해 -> dapae. English inside a line is kept.
Measured on Stray Kids "TOP" against a human romanization: 98.8 % of letters before these rules (see README).

Mandarin: pypinyin, one syllable per character (no tone marks), English kept.
"""
import functools, re

HANGUL = re.compile(r"[가-힣]")
HAN = re.compile(r"[一-鿿㐀-䶿]")
KANA = re.compile(r"[぀-ヿ]")

# jamo tables (Unicode syllable = 0xAC00 + (initial*21 + medial)*28 + final)
INI = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
FIN = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ", "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ",
       "ㅇ", "ㅈ", "ㅊ", "ㅋ", "ㅌ", "ㅍ", "ㅎ"]


def _split(ch):
    n = ord(ch) - 0xAC00
    return n // (21 * 28), (n // 28) % 21, n % 28


def _join(i, m, f):
    return chr(0xAC00 + (i * 21 + m) * 28 + f)


def _fi(name):
    return FIN.index(name)


def _ii(name):
    return INI.index(name)


def korean_pronounce(word):
    """Spelled Hangul -> pronounced Hangul (within one word)."""
    s = [list(_split(c)) if HANGUL.match(c) else c for c in word]
    for k in range(len(s) - 1):
        a, b = s[k], s[k + 1]
        if not isinstance(a, list) or not isinstance(b, list):
            continue
        fa, ib = FIN[a[2]], INI[b[0]]
        mid_i = b[1] == 20  # medial ㅣ
        # aspiration: stop + ㅎ, ㅎ + stop
        asp = {"ㄱ": "ㅋ", "ㄷ": "ㅌ", "ㅂ": "ㅍ", "ㅈ": "ㅊ"}
        if ib == "ㅎ" and fa in ("ㄱ", "ㄷ", "ㅂ", "ㅈ", "ㅅ"):
            b[0] = _ii(asp.get("ㄷ" if fa == "ㅅ" else fa)); a[2] = 0; continue
        if fa in ("ㄺ",) and ib == "ㅎ":
            a[2] = _fi("ㄹ"); b[0] = _ii("ㅋ"); continue
        if fa in ("ㅎ", "ㄶ", "ㅀ") and ib in ("ㄱ", "ㄷ", "ㅈ"):
            b[0] = _ii(asp[ib]); a[2] = {"ㅎ": 0, "ㄶ": _fi("ㄴ"), "ㅀ": _fi("ㄹ")}[fa]; continue
        if fa in ("ㅎ", "ㄶ", "ㅀ") and ib == "ㅇ":
            a[2] = {"ㅎ": 0, "ㄶ": _fi("ㄴ"), "ㅀ": _fi("ㄹ")}[fa]; continue
        # palatalization: ㄷ/ㅌ + 이 -> 지/치
        if fa in ("ㄷ", "ㅌ") and ib == "ㅇ" and mid_i:
            a[2] = 0; b[0] = _ii("ㅈ" if fa == "ㄷ" else "ㅊ"); continue
        if fa == "ㄾ" and ib == "ㅇ" and mid_i:
            a[2] = _fi("ㄹ"); b[0] = _ii("ㅊ"); continue
        # nasalization: obstruent final before ㄴ/ㅁ
        if ib in ("ㄴ", "ㅁ"):
            if fa in ("ㄱ", "ㄲ", "ㅋ", "ㄳ", "ㄺ"):
                a[2] = _fi("ㅇ")
            elif fa in ("ㄷ", "ㅅ", "ㅆ", "ㅈ", "ㅊ", "ㅌ", "ㅎ"):
                a[2] = _fi("ㄴ")
            elif fa in ("ㅂ", "ㅍ", "ㅄ", "ㄿ", "ㄼ"):
                a[2] = _fi("ㅁ")
            continue
        # liquidization: ㄴ+ㄹ / ㄹ+ㄴ -> ㄹ+ㄹ
        if (fa == "ㄴ" and ib == "ㄹ") or (fa == "ㄹ" and ib == "ㄴ"):
            a[2] = _fi("ㄹ"); b[0] = _ii("ㄹ"); continue
        # ㄹ after ㅁ/ㅇ (and after obstruents, which then nasalize) -> ㄴ
        if ib == "ㄹ" and fa in ("ㅁ", "ㅇ"):
            b[0] = _ii("ㄴ"); continue
        if ib == "ㄹ" and fa in ("ㄱ", "ㅂ"):
            b[0] = _ii("ㄴ"); a[2] = _fi("ㅇ" if fa == "ㄱ" else "ㅁ"); continue
    return "".join(_join(*c) if isinstance(c, list) else c for c in s)


def romanize_korean(line):
    from korean_romanizer.romanizer import Romanizer

    def one(m):
        r = Romanizer(korean_pronounce(m.group(0))).romanize()
        return r.replace("lr", "ll")
    return re.sub(r"[가-힣]+", one, line)


def romanize_mandarin(line):
    """Pinyin, one syllable per character (no tones); English and other text kept as words."""
    from pypinyin import lazy_pinyin, Style
    out = []
    for tok in re.findall(r"[一-鿿㐀-䶿]+|[^一-鿿㐀-䶿\s]+", line):
        if HAN.match(tok):
            out += lazy_pinyin(tok, style=Style.NORMAL, errors="ignore", v_to_u=False)
        else:
            out.append(tok)
    return " ".join(out)


# Other scripts: (Unicode ranges, name for people, Noto font family for the TV if Arial lacks it, right-to-left).
# Romanized with uroman (the universal romanizer the MMS aligner was trained with) or PyThaiNLP for Thai, which also
# splits Thai into words; Arabic and Hebrew are shown in their own script instead (their romanization leaves out
# the vowels), with words lighting up right to left.
SCRIPTS = {
    "cyrillic":   ("\u0400-\u052f", "Cyrillic", None, False),
    "greek":      ("\u0370-\u03ff\u1f00-\u1fff", "Greek", None, False),
    "devanagari": ("\u0900-\u097f", "Devanagari (Hindi…)", "NotoSansDevanagari", False),
    "bengali":    ("\u0980-\u09ff", "Bengali", "NotoSansBengali", False),
    "gurmukhi":   ("\u0a00-\u0a7f", "Gurmukhi (Punjabi)", "NotoSansGurmukhi", False),
    "gujarati":   ("\u0a80-\u0aff", "Gujarati", "NotoSansGujarati", False),
    "tamil":      ("\u0b80-\u0bff", "Tamil", "NotoSansTamil", False),
    "telugu":     ("\u0c00-\u0c7f", "Telugu", "NotoSansTelugu", False),
    "kannada":    ("\u0c80-\u0cff", "Kannada", "NotoSansKannada", False),
    "malayalam":  ("\u0d00-\u0d7f", "Malayalam", "NotoSansMalayalam", False),
    "sinhala":    ("\u0d80-\u0dff", "Sinhala", "NotoSansSinhala", False),
    "thai":       ("\u0e00-\u0e7f", "Thai", "NotoSansThai", False),
    "lao":        ("\u0e80-\u0eff", "Lao", "NotoSansLao", False),
    "khmer":      ("\u1780-\u17ff", "Khmer", "NotoSansKhmer", False),
    "myanmar":    ("\u1000-\u109f", "Burmese", "NotoSansMyanmar", False),
    "georgian":   ("\u10a0-\u10ff", "Georgian", "NotoSansGeorgian", False),
    "armenian":   ("\u0530-\u058f", "Armenian", "NotoSansArmenian", False),
    "ethiopic":   ("\u1200-\u137f", "Ethiopic (Amharic…)", "NotoSansEthiopic", False),
    "hebrew":     ("\u0590-\u05ff", "Hebrew", None, True),
    "arabic":     ("\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff", "Arabic script", None, True),
}
SCRIPT_RE = {k: re.compile(f"[{v[0]}]") for k, v in SCRIPTS.items()}
NONLATIN = re.compile("[" + "".join(v[0] for v in SCRIPTS.values()) + "\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf\uac00-\ud7a3]")


def script_of(texts):
    """'ja' if any kana, else 'ko' if Hangul, else 'zh' if Han characters, else another script from SCRIPTS that
    has at least 10 letters (the most used one), else 'latin'. texts: the lyric lines."""
    t = " ".join(texts)
    if KANA.search(t):
        return "ja"
    if len(HANGUL.findall(t)) >= 10:
        return "ko"
    if len(HAN.findall(t)) >= 10:
        return "zh"
    counts = {k: len(r.findall(t)) for k, r in SCRIPT_RE.items()}
    best = max(counts, key=counts.get)
    if counts[best] < 10:
        return "latin"
    if SCRIPTS[best][3]:
        # a song in Latin letters with a line or two in Arabic or Hebrew letters (Queen's "Bismillah") is still a
        # Latin-script song: those lines are shown as written, and the song isn't listened to as Arabic
        lines = [x for x in texts if x.strip()]
        rtl = sum(1 for x in lines if SCRIPT_RE[best].search(x))
        latin = sum(1 for x in lines if re.search(r"[A-Za-z]", x) and not SCRIPT_RE[best].search(x))
        if rtl < 0.4 * len(lines) and latin > 0.5 * len(lines):
            return "latin"
    return best


def is_rtl(script):
    return bool(SCRIPTS.get(script, (0, 0, 0, False))[3])


_UROMAN = None


def _uroman():
    global _UROMAN
    if _UROMAN is None:
        import uroman
        _UROMAN = uroman.Uroman()
    return _UROMAN


def romanize_generic(line, script):
    """A line in another script → Latin letters, word by word (Latin words kept). Thai is split into words first."""
    if script == "thai":
        try:
            from pythainlp.tokenize import word_tokenize
            from pythainlp.transliterate import romanize as th_rom
            out = []
            for tok in re.findall(r"\S+", line):
                if SCRIPT_RE["thai"].search(tok):
                    out += [th_rom(w, engine="royin") for w in word_tokenize(tok, engine="newmm", keep_whitespace=False) if w.strip()]
                else:
                    out.append(tok)
            return " ".join(w for w in out if w)
        except Exception:
            pass
    u = _uroman()
    return " ".join(u.romanize_string(tok) if NONLATIN.search(tok) else tok for tok in line.split())


def romanize_cantonese(line):
    """Jyutping without tone numbers, one syllable per character (Cantonese songs); other text kept."""
    import ToJyutping
    out = []
    for tok in re.findall(r"[一-鿿㐀-䶿]+|[^一-鿿㐀-䶿\s]+", line):
        if HAN.match(tok):
            out += [re.sub(r"\d", "", j) if j else ch for ch, j in ToJyutping.get_jyutping_list(tok)]
        else:
            out.append(tok)
    return " ".join(out)


@functools.lru_cache(maxsize=65536)
def sound_key(text):
    """What a word sounds like in plain Latin letters (a-z and '), for the aligner and for comparing lyrics with what
    Whisper heard in any script: other scripts go through uroman first."""
    import unicodedata
    if NONLATIN.search(text):
        try:
            text = _uroman().romanize_string(text)
        except Exception:
            pass
    text = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D")).lower()
    return re.sub(r"[^a-z']", "", text)


# Runes (Elder and Younger Futhark, Anglo-Saxon): some lyrics write whole lines in runes (Ado's "Tot Musica" spells
# its chant this way). Each rune becomes the Latin letter(s) it stands for, so the line can be sung and timed.
RUNES = dict(zip("ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛈᛉᛊᛏᛒᛖᛗᛚᛞᛟᚬᚴᚼᚽᚿᛅᛆᛋᛌᛐᛑᛘᛙᛦᛧᚭᚮᚯᚩᚪᚣᛣᛤ",
                 "fuþarkgwhnijpzstbemldookhhnaasstdmmrroooaayk k"))
RUNES.update({"ᚩ": "o", "ᚦ": "th", "ᛜ": "ng", "ᛝ": "ng", "ᛇ": "ei", "ᚫ": "ae", "ᛠ": "ea", "ᛡ": "io", "ᛢ": "kw", "ᛥ": "st", "ᛤ": "k", "ᛣ": "k"})
RUNE_RE = re.compile("[\u16A0-\u16F8]")


def derune(text):
    """Runes in a line, written in Latin letters (other text untouched)."""
    if not RUNE_RE.search(text):
        return text
    return RUNE_RE.sub(lambda m: RUNES.get(m.group(0), ""), text)

