"""Romanization of non-Latin lyrics into what is SUNG (Korean, Mandarin), for display and for the word aligner.

Korean: spelled Hangul is first turned into pronounced Hangul with the standard sound-change rules (nasalization,
liquidization, aspiration, palatalization), then romanized (Revised Romanization, korean-romanizer) so the result
follows what is sung: 묻는 -> munneun, 몰라 -> molla, 끝이 -> kkeuchi, 답해 -> dapae. English inside a line is kept.
Measured on Stray Kids "TOP" against a human romanization: 98.8 % of letters before these rules (see README).

Mandarin: pypinyin, one syllable per character (no tone marks), English kept.
"""
import re

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


def script_of(texts):
    """'ja' if any kana, else 'ko' if Hangul, else 'zh' if Han characters, else 'latin'."""
    t = " ".join(texts)
    if KANA.search(t):
        return "ja"
    if len(HANGUL.findall(t)) >= 10:
        return "ko"
    if len(HAN.findall(t)) >= 10:
        return "zh"
    return "latin"
