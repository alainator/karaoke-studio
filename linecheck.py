"""Check line timing by ear: compare where each lyric line sits with where a word-timed transcription heard it.

Works on any finished song, edited or not, in any language the studio romanizes: both sides are reduced to plain
letters (the lyric lines as shown, the heard words romanized the same way) and aligned letter by letter, so Japanese
(no spaces, different word splits) is compared the same way as English.

    off = heard_offsets(lines, heard, lang)   # per line: seconds the singing was heard after (+) or before (-) the
                                              # line's own timing, or None if the line wasn't heard clearly
"""
import functools
import numpy as np
from romanize import sound_key, romanize_korean, romanize_mandarin, romanize_generic, script_of, SCRIPTS, is_rtl


@functools.lru_cache(maxsize=1)
def _cutlet():
    import cutlet
    return cutlet.Cutlet()


def _letters(text, lang=None):
    """Plain a-z letters of a heard word, romanized the way the studio shows that language."""
    if any(ord(c) > 0x2E7F for c in text):
        try:
            if lang == "ja":
                text = _cutlet().romaji(text)
            elif lang == "ko":
                text = romanize_korean(text)
            elif lang in ("zh", "yue"):
                text = romanize_mandarin(text)
        except Exception:
            pass
    elif lang not in (None, "ja", "ko", "zh", "yue"):
        sc = script_of([text])
        if sc in SCRIPTS and not is_rtl(sc):
            text = romanize_generic(text, sc)
    return sound_key(text)


def heard_offsets(lines, heard, lang=None):
    """lines: [{"words": [[start, end, text], ...]}, ...]; heard: [[start, end, word], ...] from the transcription."""
    L, own = [], []   # lyric letters: (line, letter code), and each letter's own time
    for i, l in enumerate(lines):
        for s, e, w in l["words"]:
            k = sound_key(str(w))
            for j, ch in enumerate(k):
                L.append((i, ord(ch))); own.append(s + (e - s) * j / max(1, len(k)))
    H, ht = [], []
    for s, e, w in heard:
        k = _letters(str(w), lang)
        for j, ch in enumerate(k):
            H.append(ord(ch)); ht.append(s + (e - s) * j / max(1, len(k)))
    out = [None] * len(lines)
    if len(L) < 20 or len(H) < 20:
        return out
    la, ha = np.array([c for _, c in L]), np.array(H)
    n, m = len(la), len(ha)
    S = np.zeros(m + 1)
    ptr = np.zeros((n + 1, m + 1), dtype=np.int8)  # 1 diagonal, 2 lyric letter unheard, 3 heard letter skipped
    ix = np.arange(m + 1)
    for i in range(1, n + 1):
        dg = S[:-1] + np.where(ha == la[i - 1], 1.0, -1.0)
        up = S - 0.5
        A = np.empty(m + 1); A[0] = up[0]; A[1:] = np.maximum(dg, up[1:])
        src = np.empty(m + 1, dtype=np.int8); src[0] = 2; src[1:] = np.where(dg >= up[1:], 1, 2)
        rm = np.maximum.accumulate(A + 0.15 * ix) - 0.15 * ix  # skipping heard letters (talking, ad-libs) is cheap
        left = rm > A + 1e-9
        S = np.where(left, rm, A); ptr[i] = np.where(left, 3, src)
    i, j = n, int(np.argmax(S))
    diffs = {}
    while i > 0 and j > 0:
        p = ptr[i, j]
        if p == 1:
            if la[i - 1] == ha[j - 1]:
                diffs.setdefault(L[i - 1][0], []).append(ht[j - 1] - own[i - 1])
            i, j = i - 1, j - 1
        elif p == 2:
            i -= 1
        else:
            j -= 1
    count = {}
    for li, _ in L:
        count[li] = count.get(li, 0) + 1
    for li, d in diffs.items():
        d = np.array(d)
        # heard clearly: most of its letters found, and they agree with each other on when
        if len(d) >= max(4, 0.6 * count[li]) and float(np.percentile(d, 75) - np.percentile(d, 25)) <= 1.5:
            out[li] = float(np.median(d))
    # Whisper's word times run a constant few tenths early or late against the aligner's: measure from the song's
    # own typical offset, so only lines that differ from the rest stand out.
    near = [x for x in out if x is not None and abs(x) < 2.0]
    bias = float(np.median(near)) if len(near) >= 5 else 0.0
    out = [None if x is None else x - bias for x in out]
    # A lone line "heard" far away between neighbours that are fine matched the same words somewhere else
    # (a repeated phrase, talking): that says nothing about this line.
    for i, x in enumerate(out):
        if x is not None and abs(x) > 15.0:
            nb = [out[k] for k in (i - 1, i + 1) if 0 <= k < len(out) and out[k] is not None]
            if nb and all(abs(y) <= 1.5 for y in nb):
                out[i] = None
    return out
