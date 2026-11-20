"""Which piece of the original line each romanized word sings, so the original script (or its kana) can light up as
it's sung. Used by assgen.py for the display modes "original", "kana" and "both".

    spans, method = orig_spans(line["orig"], [w for _, _, w in line["words"]], lang)
        spans: one piece of the original line per word ("" when a word gets none); "".join(spans) == the orig line
        method: "cutlet" (Japanese, regrouped exactly as the pipeline's romaji), "space" (one word per space-separated
        piece: Korean and most other scripts), "char" (one Chinese character per pinyin/jyutping syllable) or
        "proportional" (split by length: hand-made or edited romaji, anything that doesn't line up)
    kana = kana_words(line["orig"], words)  -> one hiragana piece per word (Japanese lines), and the method

Japanese: the pipeline's romaji is cutlet's (use_foreign_spelling off). cutlet makes one token per MeCab word and
glues some of them together (suffixes, punctuation, verb endings), so a romaji word is a run of tokens up to one
that is followed by a space. Re-tokenizing the line the same way gives each word its original text and its reading;
when a word was changed by hand or by the AI reading check (未来 sung "ashita"), its kana comes from the romaji itself.
"""
import functools, re, unicodedata

KANA = re.compile(r"[぀-ヿ]")
HAN = re.compile(r"[一-鿿㐀-䶿]")
HANGUL = re.compile(r"[가-힣]")
JA_CHARS = re.compile(r"[぀-ヿ一-鿿㐀-䶿]")


def kata2hira(s):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _key(w):
    """A word as letters only, for comparing romaji (case, punctuation and macrons ignored)."""
    w = unicodedata.normalize("NFKD", w).lower()
    return re.sub(r"[^a-z0-9]", "", w)


@functools.lru_cache(maxsize=1)
def _cutlet():
    import cutlet
    kat = cutlet.Cutlet()
    kat.use_foreign_spelling = False  # as in pipeline.py
    return kat


def _norm_map(orig):
    """cutlet's normalized text, and for each of its characters the index of the orig character it came from (None
    if per-character normalization doesn't rebuild the same text)."""
    from cutlet.cutlet import normalize_text
    norm = normalize_text(orig)
    out, idx = [], []
    for i, c in enumerate(orig):
        n = normalize_text(c)
        out.append(n)
        idx += [i] * len(n)
    return norm, (idx if "".join(out) == norm else None)


def cutlet_groups(orig):
    """[(orig piece, cutlet romaji, hiragana reading)] per romaji word of the line, exactly as cutlet spaces them;
    None if the line can't be split back onto its original characters."""
    kat = _cutlet()
    norm, idx = _norm_map(orig)
    if idx is None:
        return None
    nodes = list(kat.tagger(norm))
    toks = kat.romaji_tokens(nodes, capitalize=False)
    # where each node starts in the normalized text (surfaces are in order; whitespace sits between them)
    pos, starts = 0, []
    for n in nodes:
        p = norm.find(n.surface, pos)
        if p < 0:
            return None
        starts.append(p); pos = p + len(n.surface)
    groups, cur = [], None
    for k, (n, t) in enumerate(zip(nodes, toks)):
        if cur is None:
            cur = {"start": starts[k], "roma": "", "kana": ""}
        cur["roma"] += t.surface
        cur["kana"] += _reading(n)
        if t.space or k == len(nodes) - 1:
            groups.append(cur); cur = None
    # groups whose romaji is empty (dropped punctuation, symbols) join the group before (or after, at the start)
    merged = []
    for g in groups:
        if not _key(g["roma"]) and merged:
            merged[-1]["roma"] += g["roma"]; merged[-1]["kana"] += g["kana"]
        elif merged and not _key(merged[-1]["roma"]):
            g["start"] = merged[-1]["start"]; g["kana"] = merged[-1]["kana"] + g["kana"]; merged[-1] = g
        else:
            merged.append(g)
    if not merged:
        return None
    cuts = [0] + [idx[g["start"]] for g in merged[1:]] + [len(orig)]
    return [(orig[cuts[i]:cuts[i + 1]], g["roma"], g["kana"]) for i, g in enumerate(merged)]


def _reading(node):
    """The hiragana of one MeCab word, as cutlet reads it (kana field), or the word itself (Latin, symbols)."""
    s = node.surface
    if not JA_CHARS.search(s):
        return s
    if KANA.search(s) and not HAN.search(s):  # written in kana: show it as written (は stays は), katakana as hiragana
        return kata2hira(s)
    try:
        k = node.feature.kana
    except Exception:
        k = None
    return kata2hira(k) if k and k != "*" else kata2hira(s)


# ---- romaji -> hiragana (for words changed by hand or by the AI reading check) ----
_pairs = ("a あ i い u う e え o お ka か ki き ku く ke け ko こ sa さ shi し si し su す se せ so そ ta た chi ち ti てぃ "
          "tsu つ tu とぅ te て to と na な ni に nu ぬ ne ね no の ha は hi ひ fu ふ hu ふ he へ ho ほ ma ま mi み mu む "
          "me め mo も ya や yu ゆ yo よ ra ら ri り ru る re れ ro ろ wa わ wo を ga が gi ぎ gu ぐ ge げ go ご za ざ "
          "ji じ zi じ zu ず ze ぜ zo ぞ da だ di でぃ du どぅ dzu づ de で do ど ba ば bi び bu ぶ be べ bo ぼ pa ぱ pi ぴ "
          "pu ぷ pe ぺ po ぽ kya きゃ kyu きゅ kyo きょ sha しゃ shu しゅ sho しょ she しぇ cha ちゃ chu ちゅ cho ちょ "
          "che ちぇ nya にゃ nyu にゅ nyo にょ hya ひゃ hyu ひゅ hyo ひょ mya みゃ myu みゅ myo みょ rya りゃ ryu りゅ "
          "ryo りょ gya ぎゃ gyu ぎゅ gyo ぎょ ja じゃ ju じゅ jo じょ je じぇ jya じゃ jyu じゅ jyo じょ bya びゃ byu びゅ "
          "byo びょ pya ぴゃ pyu ぴゅ pyo ぴょ fa ふぁ fi ふぃ fe ふぇ fo ふぉ va ゔぁ vi ゔぃ vu ゔ ve ゔぇ vo ゔぉ "
          "ye いぇ wi うぃ we うぇ tsa つぁ dyu でゅ tyu てゅ").split()
_R2K = dict(zip(_pairs[::2], _pairs[1::2]))
_MACRON = str.maketrans({"ā": "aa", "ī": "ii", "ū": "uu", "ē": "ee", "ō": "ou", "â": "aa", "î": "ii", "û": "uu", "ê": "ee", "ô": "ou"})


def romaji_to_kana(word):
    """Hepburn romaji -> hiragana; None if the word isn't romaji (an English word)."""
    w = word.lower().translate(_MACRON)
    core = re.sub(r"[^a-z']", "", w)
    if not core:
        return None
    out, i = [], 0
    while i < len(core):
        c = core[i]
        if c == "'":
            i += 1; continue
        if c == "n" and (i + 1 == len(core) or core[i + 1] == "'" or (core[i + 1] not in "aiueoy")):
            out.append("ん"); i += 1; continue
        if i + 1 < len(core) and c == core[i + 1] and c not in "aiueon":  # kk, tt, ss, pp: small tsu
            out.append("っ"); i += 1; continue
        if c == "t" and core[i + 1:i + 3] == "ch":
            out.append("っ"); i += 1; continue
        for L in (3, 2, 1):
            s = core[i:i + L]
            if s in _R2K:
                out.append(_R2K[s]); i += L; break
        else:
            return None
    # keep the word's punctuation around it (a trailing comma, "!")
    m = re.match(r"^([^A-Za-z]*).*?([^A-Za-z']*)$", word.translate(_MACRON))
    return (m.group(1) if m else "") + "".join(out) + (m.group(2) if m else "")


# ---- other ways to split ----
def _space_split(orig, n):
    """One space-separated piece per word (Korean, most other scripts), spaces kept with the piece before."""
    parts = re.findall(r"\S+\s*", orig)
    if len(parts) != n:
        return None
    lead = orig[:len(orig) - len(orig.lstrip())]
    parts[0] = lead + parts[0]
    return parts


def _char_split(orig, words):
    """Chinese: one character per pinyin/jyutping syllable; other text (English words) one piece each; punctuation
    and spaces ride with the piece before (as the pipeline made the words)."""
    units = []
    for m in re.finditer(r"[一-鿿㐀-䶿]|[^一-鿿㐀-䶿\s]+|\s+", orig):
        t = m.group(0)
        if units and (t.isspace() or not (HAN.search(t) or re.search(r"[A-Za-z]", t))):
            units[-1] += t
        else:
            units.append(t)
    if units and len(units) > 1 and not (HAN.search(units[0]) or re.search(r"[A-Za-z]", units[0])):
        units[1] = units[0] + units[1]; units.pop(0)
    return units if len(units) == len(words) else None


def _weight(c):
    """Roughly how many romaji letters one character of the original stands for."""
    if c.isspace() or unicodedata.category(c)[0] in "PSZ":
        return 0.0
    if c in "ゃゅょぁぃぅぇぉャュョァィゥェォっッーん":
        return 1.0
    if KANA.search(c):
        return 2.0
    if HAN.search(c):
        return 3.5
    if HANGUL.search(c):
        return 2.5
    return 1.0


def proportional(orig, words):
    """Split the original line's characters across the words in proportion to each word's length."""
    n = len(words)
    if n == 1:
        return [orig]
    lens = [max(1, len(_key(w))) for w in words]
    wts = [_weight(c) for c in orig]
    total_o, total_w = sum(wts) or 1.0, sum(lens)
    cum_o, acc = [0.0], 0.0
    for x in wts:
        acc += x; cum_o.append(acc)
    cuts, acc, last = [0], 0, 0
    for k in range(n - 1):
        acc += lens[k]
        target = acc / total_w * total_o
        # the character boundary closest to the target (never going back), after any weightless characters
        best = min(range(last, len(orig) + 1), key=lambda p: (abs(cum_o[p] - target), -p))
        while best < len(orig) and wts[best] == 0.0:
            best += 1
        cuts.append(best); last = best
    cuts.append(len(orig))
    return [orig[cuts[i]:cuts[i + 1]] for i in range(n)]


def _lev(a, b):
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _align(wkeys, gkeys):
    """Line up romaji words with cutlet's words when they differ (hand-made or edited romaji: "nanimo" for cutlet's
    "nan mo", "aishitai kara" for "aishitaikara"): blocks of up to 3 words against up to 3 groups (or none), by
    spelling. [(word indices, group indices)], or None when they don't match well enough."""
    n, m = len(wkeys), len(gkeys)
    INF = float("inf")
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    back = [[None] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if dp[i][j] == INF:
                continue
            for a in range(0, 4):
                for b in range(0, 4):
                    if (a == 0 and b == 0) or i + a > n or j + b > m:
                        continue
                    wk, gk = "".join(wkeys[i:i + a]), "".join(gkeys[j:j + b])
                    c = dp[i][j] + _lev(wk, gk) + 0.5 * max(0, a + b - 2) + (0.5 if a == 0 or b == 0 else 0)
                    if c < dp[i + a][j + b]:
                        dp[i + a][j + b] = c; back[i + a][j + b] = (a, b)
    total = max(len("".join(wkeys)), len("".join(gkeys)), 1)
    if dp[n][m] > 0.35 * total:
        return None
    blocks, i, j = [], n, m
    while i or j:
        a, b = back[i][j]
        blocks.append((list(range(i - a, i)), list(range(j - b, j))))
        i, j = i - a, j - b
    return blocks[::-1]


def ja_map(orig, words):
    """Japanese line: (pieces of orig, hiragana pieces, method), one of each per word."""
    try:
        g = cutlet_groups(orig)
    except Exception:
        g = None
    wkeys = [_key(w) for w in words]
    blocks = _align(wkeys, [_key(r) for _, r, _ in g]) if g else None
    if not blocks:
        spans = proportional(orig, words)
        kana = []
        for w, piece in zip(words, spans):
            k = romaji_to_kana(w) if JA_CHARS.search(piece) or not re.search(r"[A-Za-z]", piece) else None
            kana.append(k if k else " " + w + " ")
        kana = [re.sub(r"\s+", " ", x) for x in kana]
        kana[0] = kana[0].lstrip()
        return spans, kana, "proportional"
    spans, kana = [""] * len(words), [""] * len(words)
    carry_s = carry_k = ""  # text of groups nobody sings, before the first word
    last = None
    one_to_one = True
    for wi, gi in blocks:
        piece = "".join(g[k][0] for k in gi)
        gkana = "".join(g[k][2] for k in gi)
        tail = piece[len(piece.rstrip()):]
        if len(wi) != 1 or len(gi) != 1:
            one_to_one = False
        if not wi:  # groups with no word: their text goes with the word before (or after, at the start)
            if last is None:
                carry_s += piece; carry_k += gkana
            else:
                spans[last] += piece
            continue
        if len(wi) == 1:
            w = words[wi[0]]
            if len(gi) == 1 and _key(g[gi[0]][1]) == wkeys[wi[0]] or not JA_CHARS.search(piece):
                k = gkana + tail
            else:  # the reading was changed (by hand or by the AI check): show what is sung
                k = (romaji_to_kana(w) or gkana) + tail
            parts, kparts = [piece], [k]
        else:
            parts = proportional(piece, [words[x] for x in wi]) if piece else [""] * len(wi)
            kparts = [(romaji_to_kana(words[x]) or p.strip()) + p[len(p.rstrip()):] for x, p in zip(wi, parts)]
        for x, p, k in zip(wi, parts, kparts):
            spans[x], kana[x] = p, k
        if carry_s:
            spans[wi[0]] = carry_s + spans[wi[0]]; carry_s = ""
        last = wi[-1]
    if carry_s:  # nothing sung at all: the text goes to the first word
        spans[0] = carry_s + spans[0]
    return spans, kana, ("cutlet" if one_to_one else "aligned")


def _thai_split(orig, n):
    """Thai: words split the way the pipeline romanized them (PyThaiNLP), other pieces by spaces."""
    try:
        from pythainlp.tokenize import word_tokenize
    except Exception:
        return None
    from romanize import SCRIPT_RE
    parts = []
    for m in re.finditer(r"\S+\s*", orig):
        tok = m.group(0); core = tok.rstrip()
        if SCRIPT_RE["thai"].search(core):
            sub = [w for w in word_tokenize(core, engine="newmm", keep_whitespace=False) if w.strip()]
            if "".join(sub) != core:
                return None
            sub[-1] += tok[len(core):]
            parts += sub
        else:
            parts.append(tok)
    if len(parts) != n:
        return None
    parts[0] = orig[:len(orig) - len(orig.lstrip())] + parts[0]
    return parts


def orig_spans(orig, words, japanese=False):
    """(pieces of orig, one per word, joining back to orig), method."""
    if not orig or not words:
        return None, None
    if japanese and JA_CHARS.search(orig) and not (HANGUL.search(orig) and not KANA.search(orig)):
        spans, _, how = ja_map(orig, words)
        if how != "proportional":
            return spans, how
    sp = _space_split(orig, len(words)) if re.search(r"\s", orig.strip()) else None
    if sp:
        return sp, "space"
    if HAN.search(orig) and not KANA.search(orig):
        ch = _char_split(orig, words)
        if ch:
            return ch, "char"
    if re.search(r"[\u0e00-\u0e7f]", orig):
        th = _thai_split(orig, len(words))
        if th:
            return th, "thai"
    return proportional(orig, words), "proportional"


def kana_words(orig, words):
    """One hiragana piece per word of a Japanese line, and how it was found."""
    if not JA_CHARS.search(orig):
        return [w + " " for w in words], "latin"
    _, kana, how = ja_map(orig, words)
    return kana, how


@functools.lru_cache(maxsize=8192)
def line_maps(orig, words, japanese):
    """orig_spans and (for Japanese songs) kana_words of one line, cached (a song's subtitles are drawn once per
    display mode, and again on every save). words: a tuple. Returns (spans, method, kana or None, kana method)."""
    words = list(words)
    spans, how = orig_spans(orig, words, japanese)
    kana, khow = kana_words(orig, words) if japanese else (None, None)
    return spans, how, kana, khow
