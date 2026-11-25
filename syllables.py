"""Syllable timing: a word sung in pieces ("tra-ge-diiies") lights up piece by piece, the pieces joined (no space).

words.json keeps every word whole; a line may add "syl", a list parallel to "words" (like "wwho"): null for a word
sung in one go, or the word's pieces [[start, end, "tra"], [start, end, "ge"], [start, end, "dies"]] (same clock as
the words). The pieces' texts joined are exactly the word's text, the first piece starts where the word starts and
the last ends where the word ends. Everything that reads words.json without knowing "syl" keeps working on whole
words (word counts, scoring, the timing bars in players, the aligner checks).

Tools that move or stretch words without knowing about pieces (a line moved, stretched, trimmed) leave them a little
off: fit() maps each word's pieces back onto the word's span (exact for any move or stretch), and drops them when
the word's text changed. normalize() and save_lines() in assgen.py do that on every read and write.

In the editor and in ks a split is typed with "|" inside the word: "tra|ge|dies" (the | never shows).

auto(): where a word clearly spans several separate notes of the melody (pitch.json's notes; a held or stretched
syllable), split it into syllables and time each piece to its notes. Latin-script words only, never lines with an
original-script row (Japanese, Chinese, Korean... are timed per character or per romanized word already), never a
word a person split, and only where the notes and the syllables agree (see auto_word).
"""
import re
import unicodedata

SEP = "|"
MIN_PIECE = 0.06       # seconds: a piece shorter than this isn't kept
HELD = 0.5             # seconds: a piece at least this long is "held" (it sweeps even with sweep off)

# ---- the data ----
def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and abs(x) != float("inf")


def even(a, b, texts):
    """Pieces sharing a word's span by letter count (a fresh split, before anyone times it)."""
    w = [max(1, len(t)) for t in texts]
    tot, span, t, out = sum(w), max(0.0, b - a), a, []
    for k, (x, wi) in enumerate(zip(texts, w)):
        e = b if k == len(texts) - 1 else t + span * wi / tot
        out.append([round(t, 3), round(e, 3), x])
        t = e
    return out


def fit(word, pieces):
    """The word's pieces, valid and on the word's span; None when the word isn't split (or the pieces no longer
    belong to it: its text changed)."""
    if not isinstance(pieces, list) or not 2 <= len(pieces) <= 24:
        return None
    a, b, text = float(word[0]), float(word[1]), word[2]
    ps = []
    for p in pieces:
        if not (isinstance(p, (list, tuple)) and len(p) == 3 and _num(p[0]) and _num(p[1]) and isinstance(p[2], str) and p[2]):
            return None
        ps.append([float(p[0]), float(p[1]), p[2]])
    if "".join(p[2] for p in ps) != text:
        return None
    ps.sort(key=lambda p: p[0])
    pa, pb = ps[0][0], max(p[1] for p in ps)
    if pb - pa <= 1e-6 or b - a <= 1e-6:
        return even(a, b, [p[2] for p in ps])
    if abs(pa - a) > 5e-4 or abs(pb - b) > 5e-4:  # the word moved or was stretched: the pieces follow it
        k = (b - a) / (pb - pa)
        ps = [[a + (x - pa) * k, a + (y - pa) * k, t] for x, y, t in ps]
    out = []
    for i, (x, y, t) in enumerate(ps):
        x = a if i == 0 else min(max(x, a), b)
        nxt = ps[i + 1][0] if i + 1 < len(ps) else b
        y = b if i + 1 == len(ps) else min(max(y, x), max(x, min(nxt, b)))
        out.append([round(x, 3), round(y, 3), t])
    return out


def fit_line(words, syl):
    """A line's "syl" list fitted to its words, or None when no word is split."""
    if not isinstance(syl, list) or len(syl) != len(words):
        return None
    out = [fit(w, s) if s else None for w, s in zip(words, syl)]
    return out if any(out) else None


def parse(token):
    """A typed word with | markers -> (the word, its piece texts or None). "tra|ge|dies" -> ("tragedies",
    ["tra", "ge", "dies"]); a | at either end or doubled means nothing."""
    parts = [p for p in token.split(SEP) if p]
    word = "".join(parts)
    return word, (parts if len(parts) >= 2 else None)


def marked(word, pieces):
    """The word as typed: its pieces joined by |."""
    return SEP.join(p[2] for p in pieces) if pieces else word[2]


def line_text(line):
    """A line's text with its split words marked (tra|ge|dies), as the editor and ks show it."""
    syl = line.get("syl") or [None] * len(line["words"])
    return " ".join(marked(w, s) for w, s in zip(line["words"], syl))


def set_split(line, j, texts):
    """Split word j of the line into these piece texts (evenly timed; the word's own pieces are kept when the texts
    are the same), or unsplit it (texts None or one piece). The texts joined must be the word."""
    w = line["words"][j]
    syl = list(line.get("syl") or [None] * len(line["words"]))
    if len(syl) != len(line["words"]):
        syl = [None] * len(line["words"])
    if texts and len(texts) >= 2:
        if "".join(texts) != w[2]:
            raise ValueError(f"the pieces make {''.join(texts)!r}, the word is {w[2]!r}")
        old = fit(w, syl[j]) if syl[j] else None
        syl[j] = old if old and [p[2] for p in old] == list(texts) else even(w[0], w[1], list(texts))
    else:
        syl[j] = None
    line["syl"] = syl if any(syl) else None
    return line


def set_piece(line, j, p, start=None, end=None):
    """Retime piece p of word j (the first piece's start is the word's start, the last piece's end the word's end).
    A boundary the piece shares with its neighbour moves for both; nothing can cross a neighbour. Raises ValueError."""
    ws = line["words"]
    pcs = fit(ws[j], (line.get("syl") or [None] * len(ws))[j]) if line.get("syl") else None
    if not pcs:
        raise ValueError(f"word {j + 1} isn't split into syllables (ks syllables SONG N W \"tra|ge|dies\")")
    if not 0 <= p < len(pcs):
        raise ValueError(f"word {j + 1} has pieces 1-{len(pcs)}")
    s = pcs[p][0] if start is None else float(start)
    e = pcs[p][1] if end is None else float(end)
    lo = pcs[p - 1][0] + MIN_PIECE if p > 0 else (ws[j - 1][1] if j > 0 else 0.0)
    hi = pcs[p + 1][1] - MIN_PIECE if p + 1 < len(pcs) else (ws[j + 1][0] if j + 1 < len(ws) else 1e9)
    if not (lo - 1e-3 <= s and e <= hi + 1e-3 and e - s >= MIN_PIECE - 1e-6):
        raise ValueError("the piece has to stay between its neighbours, at least %.2f s long" % MIN_PIECE)
    if p > 0 and abs(pcs[p - 1][1] - pcs[p][0]) < 1e-3:
        pcs[p - 1][1] = round(s, 3)
    elif p > 0:
        pcs[p - 1][1] = round(min(pcs[p - 1][1], s), 3)
    if p + 1 < len(pcs) and abs(pcs[p + 1][0] - pcs[p][1]) < 1e-3:
        pcs[p + 1][0] = round(e, 3)
    elif p + 1 < len(pcs):
        pcs[p + 1][0] = round(max(pcs[p + 1][0], e), 3)
    pcs[p] = [round(s, 3), round(e, 3), pcs[p][2]]
    ws[j] = [pcs[0][0], pcs[-1][1], ws[j][2]]
    syl = list(line["syl"])
    syl[j] = pcs
    line["syl"] = syl
    return line


def ass_parts(word, pieces, nxt):
    """The karaoke parts of one split word for assgen: [(text, centiseconds, kind)], the centiseconds adding up to
    exactly round((nxt - word start) * 100), what the word gets unsplit, so a row above or below it that is per word
    (the original-script row) stays in step. kind: "sweep" (the row's own tag), "held" (a piece HELD or longer: it
    fills while it's held even where the row lights words at once) or "gap" (an empty wait). A piece lasts until the
    next one starts; a pause of 0.2 s or more after it (a breath) becomes its own wait, so a held piece's fill ends
    when its note does."""
    a = word[0]
    total = max(1, round((nxt - a) * 100))
    cs_at = lambda t: min(total, max(0, round((t - a) * 100)))
    out = []
    for i, (x, y, t) in enumerate(pieces):
        stop = pieces[i + 1][0] if i + 1 < len(pieces) else nxt
        c0, c2 = cs_at(x), cs_at(stop)
        c1 = min(cs_at(y), c2)
        kind = "held" if y - x >= HELD else "sweep"
        if c2 - c1 >= 20:
            out += [[t, max(0, c1 - c0), kind], ["", c2 - c1, "gap"]]
        else:
            out.append([t, max(0, c2 - c0), kind])
    out[-1][1] = max(0, out[-1][1] + total - sum(p[1] for p in out))   # (rounding: the parts tile 0..total)
    return [tuple(p) for p in out]


# ---- syllables of a word ----
VOWELS = set("aeiouy")
ONSETS = {"bl", "br", "ch", "cl", "cr", "dr", "fl", "fr", "gl", "gr", "kn", "ph", "pl", "pr", "qu", "sc", "sh", "sk",
          "sl", "sm", "sn", "sp", "st", "sw", "th", "tr", "tw", "wh", "wr", "gu"}
NO_SYL = ("ja", "zh", "yue", "ko")      # timed per character / per romanized word already
_LATIN = re.compile(r"^[A-Za-zÀ-ɏ'’\-]+$")


def _base(ch):
    return unicodedata.normalize("NFD", ch)[0].lower()


def _is_vowel(word, i):
    c = _base(word[i])
    if c == "y":  # y is a consonant before a vowel at the start of a word or a syllable (yes, beyond)
        return not (i + 1 < len(word) and _base(word[i + 1]) in "aeiou")
    return c in VOWELS


def _groups(core, lang):
    """The vowel groups of a word (index ranges), with English silent endings left out."""
    g, i = [], 0
    while i < len(core):
        if _is_vowel(core, i) and core[i].isalpha():
            j = i
            while j + 1 < len(core) and _is_vowel(core, j + 1) and core[j + 1].isalpha() and not _hiatus(core, j, lang):
                j += 1
            g.append((i, j))
            i = j + 1
        else:
            i += 1
    low = core.lower()
    if (lang or "en") == "en" and len(g) >= 2:
        s, e = g[-1]
        last = low[s:e + 1]
        tail = low[e + 1:]
        before = low[s - 1] if s > 0 else ""
        if last == "e" and tail in ("", "s", "d") and not (tail == "" and before == "l" and s >= 2 and low[s - 2] not in VOWELS):
            # silent final e (love, times, loved), but not "-le" after a consonant (lit-tle) or -es/-ed after
            # sounds that add a syllable (fa-ces, wan-ted)
            if not (tail == "s" and before in "sxzcg") and not (tail == "d" and before in "td") and before not in VOWELS:
                g = g[:-1]
    return g


def _hiatus(core, j, lang):
    """Two vowels that are sung apart (an accented vowel next to another in Spanish/Portuguese: dí-a)."""
    a, b = core[j], core[j + 1]
    acc = lambda c: unicodedata.normalize("NFD", c) != c and _base(c) in "iu"
    return lang in ("es", "pt", "it", "ca", "gl") and (acc(a) or acc(b))


def vowel_split(word, lang=None):
    """The word split into syllables by its vowel groups (a heuristic for Latin-script words): pieces whose joined
    text is the word, or [word]."""
    m = re.match(r"^([^\w]*)(.*?)([^\w]*)$", word, re.S)
    lead, core, trail = m.group(1), m.group(2), m.group(3)
    if not core or not _LATIN.match(core):
        return [word]
    g = _groups(core, lang)
    if len(g) < 2:
        return [word]
    cuts = []
    for (s0, e0), (s1, e1) in zip(g, g[1:]):
        cons = core[e0 + 1:s1]
        n = len(cons)
        if n == 0:
            cut = s1
        elif (lang or "en") == "en" and s1 == len(core) - 1 and core[s1:].lower() == "e" and n >= 2 \
                and cons.lower().endswith("l"):
            cut = s1 - 2   # English consonant + le at the end is its own syllable: lit-tle, ta-ble
        elif n == 1:
            cut = s1 - 1
        else:
            two = cons[-2:].lower()
            cut = s1 - 2 if two in ONSETS else s1 - 1
            if n == 2 and cons.lower() in ("ck", "ng", "gh") and (lang or "en") == "en":
                cut = s1   # English keeps these with the syllable before: luck-y, sing-ing
        cuts.append(max(1, min(len(core) - 1, cut)))
    pieces, prev = [], 0
    for c in cuts + [len(core)]:
        if c > prev:
            pieces.append(core[prev:c])
            prev = c
    if len(pieces) < 2:
        return [word]
    pieces[0] = lead + pieces[0]
    pieces[-1] = pieces[-1] + trail
    return pieces


def syllables(word, lang=None):
    """The word's syllables (vowel_split). A hyphenation dictionary (pyphen 0.18, LibreOffice's patterns, with
    one-letter pieces allowed) was tried and left out: it's made for breaking lines, not for singing, and leaves out
    breaks on purpose. Of 25 common English words it gave fewer syllables than are sung for 8 (about, tonight,
    singing, faces, everything, tragedies...); where it agreed, it only moved letters (for|ev|er, heav|en). The melody
    is the second opinion instead: auto_word() splits only where the notes agree with the syllables."""
    return vowel_split(word, lang)


# ---- automatic splits from the melody ----
MIN_WORD = 0.45        # seconds: shorter words aren't split (they're over before a split could show)
AUTO_PIECE = 0.12      # seconds: an automatic piece at least this long, the last one at least AUTO_LAST (a short
AUTO_LAST = 0.15       # last note after a held one is the held vowel's fall-off, or the word sits off its singing)
MIN_NOTE = 0.08        # seconds: shorter bits of melody are slides, not notes
SAME = 1.0             # semitones: neighbouring notes closer than this (and touching) are one note
TOUCH = 0.06           # seconds


def _notes_in(a, b, notes):
    """The melody's separate notes inside [a, b]: [[start, end, midi]], clipped, small slides merged away."""
    ns = [[max(a, x), min(b, y), m] for x, y, m in notes if y > a and x < b and min(b, y) - max(a, x) > 0.02]
    out = []
    for n in ns:
        if out and n[0] - out[-1][1] <= TOUCH and abs(n[2] - out[-1][2]) < SAME:
            out[-1][1] = n[1]
            continue
        out.append(n)
    return [n for n in out if n[1] - n[0] >= MIN_NOTE]


def auto_word(word, notes, lang=None):
    """The pieces for one word from the melody's notes (same clock), or None when it isn't clearly sung in pieces:
    - the word lasts MIN_WORD or more and has 2+ syllables (syllables());
    - it has 2+ separate notes, and either one note per syllable, or more notes than syllables with the extra ones
      at the end (the last syllable held over several notes: a melisma), or fewer notes with the last one held
      (HELD or longer) and the earlier syllables sharing the notes before it;
    - no piece comes out shorter than AUTO_PIECE (the last one AUTO_LAST), and one is held (HELD or longer: what
      splitting is for).
    Measured on 451 published songs' melodies (2026-11-24): without that last rule 9100 words split (7.6 %), mostly
    quick two-note words that sweep right anyway; with it and the piece floors (AUTO_PIECE, AUTO_LAST) 5069 (4.2 %,
    about 11 a song), the held and stretched ones. Songs made here use the pipeline's own melody (CREPE tiny, 10 ms):
    on 4 songs it split 44 words where the full model split 50, 41 of them the same."""
    a, b, text = word
    if b - a < MIN_WORD:
        return None
    syl = syllables(text, lang)
    if len(syl) < 2:
        return None
    ns = _notes_in(a, b, notes)
    S, N = len(syl), len(ns)
    if N < 2:
        return None
    if N >= S:
        starts = [n[0] for n in ns[:S]]
        if N > S and ns[-1][1] - ns[S - 1][0] < HELD:
            return None   # extra notes that aren't a held last syllable: not sure which syllable is where
        bounds = [a] + [max(ns[k - 1][1], starts[k] - 0.04) for k in range(1, S)]
    else:
        last = ns[-1]
        if last[1] - last[0] < HELD or last[1] - last[0] < 0.4 * (b - a):
            return None
        # the last syllable on the held note, the others share what comes before it, by letters
        head = even(a, max(a, last[0] - 0.04), syl[:-1])
        bounds = [p[0] for p in head] + [max(head[-1][0] + MIN_PIECE, last[0] - 0.04)]
    pieces = []
    for k, t in enumerate(syl):
        x = bounds[k]
        y = bounds[k + 1] if k + 1 < S else b
        pieces.append([round(x, 3), round(y, 3), t])
    if any(p[1] - p[0] < AUTO_PIECE - 1e-6 for p in pieces) or pieces[-1][1] - pieces[-1][0] < AUTO_LAST - 1e-6:
        return None
    if max(p[1] - p[0] for p in pieces) < HELD:
        return None   # no held syllable: a quick word over two notes sweeps right as it is
    return pieces


def can_split(line, lang):
    """Lines auto() may split: not Japanese, Chinese or Korean songs, not lines with an original-script row."""
    return lang not in NO_SYL and not line.get("orig")


def auto(lines, notes, offset=0.0, lang=None):
    """Split the words that are clearly sung in pieces (auto_word), in place. lines: words.json lines (their clock);
    notes: the melody [[start, end, midi]] in video time (= line time + offset). Words a person split already are
    never touched. Returns how many words were split."""
    notes = sorted(notes or [])
    n = 0
    for l in lines:
        if not can_split(l, lang):
            continue
        words = l["words"]
        syl = list(l.get("syl") or [None] * len(words))
        if len(syl) != len(words):
            syl = [None] * len(words)
        for j, w in enumerate(words):
            if syl[j]:
                continue
            p = auto_word([w[0] + offset, w[1] + offset, w[2]], notes, lang)
            if p:
                syl[j] = [[round(x - offset, 3), round(y - offset, 3), t] for x, y, t in p]
                syl[j][0][0], syl[j][-1][1] = w[0], w[1]
                n += 1
        l["syl"] = syl if any(syl) else None
    return n
