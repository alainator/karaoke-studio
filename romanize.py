"""Romanization of non-Latin lyrics into what is SUNG (Korean, Mandarin), for display and for the word aligner.

Korean: spelled Hangul is first turned into pronounced Hangul with the standard sound-change rules (nasalization,
liquidization, aspiration, palatalization), then romanized (Revised Romanization, korean-romanizer) so the result
follows what is sung: 묻는 -> munneun, 몰라 -> molla, 끝이 -> kkeuchi, 답해 -> dapae. English inside a line is kept.
Measured on Stray Kids "TOP" against a human romanization: 98.8 % of letters before these rules (see README).

Mandarin: pypinyin, one syllable per character (no tone marks), English kept.
"""
import functools, re, unicodedata

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


def has_words(text):
    """Is there anything to sing in this lyric line (a letter or a digit, in any script)? Lines of only punctuation
    have none (a lyrics copy that marks a repeated line with a row of ditto quote marks)."""
    return any(c.isalnum() for c in text or "")


def drop_unsung(lines, keyed=None):
    """Lyric lines [(time, text)] without the ones that have nothing to sing (see has_words). keyed: {line index:
    value} for the same lines (hand-made romaji), re-keyed to the kept lines. Returns (lines, keyed, dropped texts)."""
    keep = [k for k, (_, x) in enumerate(lines) if has_words(x)]
    new_index = {k: n for n, k in enumerate(keep)}
    return ([lines[k] for k in keep], {new_index[k]: v for k, v in (keyed or {}).items() if k in new_index},
            [x for _, x in lines if x and not has_words(x)])


def _opens(c):
    return unicodedata.category(c) in ("Ps", "Pi")  # ( （ 【 「 “ ‘ «


def glue_brackets(tokens):
    """Words of a romanized line where brackets and quote marks came out as words of their own ("(", "）"): an opening
    one goes onto the word after it, a closing one onto the word before, so a bracketed phrase reads "(ni hao)" and
    keeps both brackets (the word timing gives a word without letters to the word before it, so a "(" opening the
    line was lost and only its ")" was shown). Other punctuation-only words (commas) are left as they are."""
    out, carry = [], ""
    for tok in tokens:
        if any(c.isalnum() for c in tok):
            out.append(carry + tok); carry = ""
            continue
        if carry:
            carry += tok
            continue
        k = next((i for i, c in enumerate(tok) if _opens(c)), len(tok))
        head, tail = tok[:k], tok[k:]
        if head:
            if out and any(unicodedata.category(c) in ("Pe", "Pf") for c in head):
                out[-1] += head
            else:
                out.append(head)
        carry = tail
    if carry:
        if out:
            out[-1] += carry
        else:
            out.append(carry)
    return out


def romanize_mandarin(line):
    """Pinyin, one syllable per character (no tones); English and other text kept as words."""
    from pypinyin import lazy_pinyin, Style
    out = []
    for tok in re.findall(r"[一-鿿㐀-䶿]+|[^一-鿿㐀-䶿\s]+", line):
        if HAN.match(tok):
            out += lazy_pinyin(tok, style=Style.NORMAL, errors="ignore", v_to_u=False)
        else:
            out.append(tok)
    return " ".join(glue_brackets(out))


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


# Thai: PyThaiNLP's RTGS romanizer (royin) is rule-based and works on one syllable at a time; given a whole word it
# guesses the syllables itself and gets many wrong (จดหมาย "chadahamai" for chotmai), and it leaves Thai vowel signs
# in what it can't read (ก็ "k็", แป็ก "pae็k", เกราะ "keraะ"). So each word is cut into syllables (PyThaiNLP's
# dictionary splitter) and each syllable romanized whole, with what royin gets wrong put right first:
#  - a leading vowel before a two-consonant start (เปลือก plueak, เกลือ kluea, ใพร phrai): royin splits the vowel from
#    its consonants ("pelin"), so the syllable is read without the second consonant, which is put back after the first;
#  - a silent ห before a low consonant behind a leading vowel (เหนือ nuea, not "enue") is left out;
#  - ห starting a syllable before its vowel is sounded (หาด hat, เห็ด het; royin drops it);
#  - mai taikhu ( ็) only shortens the vowel; on its own after a consonant it is a short o (ก็ ko);
#  - anything still in Thai letters is dropped, so the romanized row never shows Thai vowel signs.
TH_CONS = "กขฃคฅฆงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ"
_TH_ANY = re.compile("[\u0e00-\u0e7f]")
_TH_CLUSTER = (re.compile("^([เแโ])([กขคตปผพบฟด])([รลว])(.+)$"), re.compile("^([ใไ])([กขคตปผพบฟด])([รลว])(.*)$"))
_TH_SILENT_HO = re.compile("^([เแโใไ])ห([งญนมยรลว])(.*)$")
_TH_HO = re.compile(f"^[เแโใไ]?ห[^{TH_CONS}]")


def _royin(syllable):
    import importlib
    try:  # royin's own syllable step: its public romanize() splits the text into words again (กาศ -> "kas")
        return importlib.import_module("pythainlp.transliterate.royin")._romanize(syllable)
    except Exception:
        from pythainlp.transliterate import romanize as th_rom
        return th_rom(syllable, engine="royin")


def _thai_syllable(s):
    m = _TH_SILENT_HO.match(s)
    if m:
        s = m.group(1) + m.group(2) + m.group(3)
    m = _TH_CLUSTER[0].match(s) or _TH_CLUSTER[1].match(s)
    if m:
        r = _royin(m.group(1) + m.group(2) + m.group(4))
        k = re.match(r"[^aeiou]*", r).end()
        r = r[:k] + {"ร": "r", "ล": "l", "ว": "w"}[m.group(3)] + r[k:]
    else:
        r = _royin(s)
    if _TH_ANY.search(r) and "\u0e47" in s:
        r = _thai_syllable(re.sub(f"^([{TH_CONS}])\u0e47([\u0e48-\u0e4b]?)$", "\\1อ\\2", s).replace("\u0e47", ""))
    r = _TH_ANY.sub("", r)
    if r and not r.startswith("h") and _TH_HO.match(s):
        r = "h" + r
    return r


def romanize_thai_word(word):
    """One Thai word (as PyThaiNLP's word splitter cut it) in Latin letters, syllable by syllable, as one word."""
    try:
        from pythainlp.tokenize import syllable_tokenize
        out = "".join(_thai_syllable(s) for s in syllable_tokenize(word, engine="dict"))
    except Exception:
        out = ""
    if not out:  # (no syllables found: the word as a whole, as before; a word with nothing sounded stays empty)
        from pythainlp.transliterate import romanize as th_rom
        out = _TH_ANY.sub("", th_rom(word, engine="royin"))
    return out


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
            out = []
            for tok in re.findall(r"\S+", line):
                if SCRIPT_RE["thai"].search(tok):
                    out += [romanize_thai_word(w) for w in word_tokenize(tok, engine="newmm", keep_whitespace=False) if w.strip()]
                else:
                    out.append(tok)
            return " ".join(glue_brackets([w for w in out if w]))
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
    return " ".join(glue_brackets(out))  # (the same words as pinyin: the aligner reads a Cantonese line as pinyin)


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

