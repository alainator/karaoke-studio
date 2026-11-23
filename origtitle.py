"""A song's original title in its own script (逆光 for "Gyakkou"), found and checked, for the song page's Original
title (players show it next to the title; no file changes).

Songs from lyrics in Japanese, Korean, Chinese, Cyrillic... are named in Latin letters so anyone can find them, and
the original title goes next to it. Many weren't given one (2026-11-22: 202 Japanese songs had none). It's usually
known somewhere: kara.moe's titles, the LRCLIB entry's track name, the music video's YouTube title (「相思相愛」).
A candidate is only used when it reads back as the song's title (romanized: 季路 -> "kiro" = Kiro), so a film's name
in the video title, or a song whose real title is English (Brave Shine), never gets one.

    find(job, fetch) -> {"orig", "source", "read_as", "match"} or None     fetch(url) -> parsed JSON (or raises)
"""
import re
import unicodedata
from difflib import SequenceMatcher

NATIVE = re.compile(r"[぀-ヿ㐀-鿿豈-﫿가-힣Ѐ-ӿͰ-Ͽ֐-׿"
                    r"؀-ۿऀ-෿฀-๿]")
RUN = re.compile(r"[^\sA-Za-z0-9!-/:-@\[-`{-~【】「」『』（）()［］〈〉《》｜|/／・×✕~〜…“”\"'’]+"
                 r"(?:[ 　・ー〜][^\sA-Za-z0-9!-/:-@\[-`{-~【】「」『』（）()［］〈〉《》｜|/／×✕~〜…“”\"'’]+)*")
MATCH = 0.8


def _flat(s):
    """Latin letters only, lower case, long vowels and particles written one way (Gyakkô = gyakkou = gyakko)."""
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = "".join(c for c in s if "a" <= c <= "z")
    for a, b in (("ou", "o"), ("oo", "o"), ("uu", "u"), ("aa", "a"), ("ei", "e"), ("ii", "i"), ("wo", "o"), ("tsu", "tu"),
                 ("shi", "si"), ("chi", "ti"), ("ji", "zi"), ("fu", "hu")):
        s = s.replace(a, b)
    return s


def read_as(native, lang):
    """The native title in Latin letters, the way the song's language reads it."""
    try:
        if lang == "ja":
            import cutlet
            return cutlet.Cutlet().romaji(native)
        if lang == "ko":
            from romanize import romanize_korean
            return romanize_korean(native)
        if lang in ("zh", "yue"):
            from romanize import romanize_mandarin
            return romanize_mandarin(native)
        from romanize import sound_key
        return sound_key(native)
    except Exception:
        return ""


def match(native, title, lang):
    """How well a native title reads back as the Latin title (0..1)."""
    a, b = _flat(read_as(native, lang)), _flat(re.sub(r"\(.*?\)", "", title or ""))
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


VERSION_NOTE = re.compile(r"\s*[~～\-–(（]\s*[^~～()（）]*\b(?:vers?|version|ver)\.?\s*[~～)）]?\s*$", re.I)


def clean(t):
    """A title as written, without version notes ("~ Movie vers.") or stray punctuation at its ends."""
    return VERSION_NOTE.sub("", (t or "").strip()).strip(" 　・〜~：:、,")   # (ー and 。 can be part of a title)


def candidates(job, fetch):
    """(native text, where it came from), best sources first."""
    out = []
    if job.get("lyrics_source") == "karamoe" and job.get("lrc_id"):
        try:
            t = (fetch(f"https://kara.moe/api/karas/{job['lrc_id']}") or {}).get("titles") or {}
            out += [(v, "kara.moe") for k, v in t.items() if v and NATIVE.search(v)]
        except Exception:
            pass
    if job.get("lyrics_source") == "lrclib" and job.get("lrc_id"):
        try:
            tn = (fetch(f"https://lrclib.net/api/get/{int(job['lrc_id'])}") or {}).get("trackName") or ""
            if NATIVE.search(tn):
                out.append((tn.strip(), "LRCLIB"))
        except Exception:
            pass
    for run in RUN.findall(job.get("video_title") or ""):
        if NATIVE.search(run):
            out.append((run, "the video's title"))
    # wider: kara.moe by title, and the track names of LRCLIB's other copies (still checked by reading them back)
    t, a = job.get("title") or "", job.get("artist") or ""
    try:
        import urllib.parse
        for k in ((fetch("https://kara.moe/api/karas/search?" + urllib.parse.urlencode({"filter": t, "size": 12})) or {})
                  .get("content") or []):
            out += [(v, "kara.moe") for v in ((k.get("titles") or {}).values()) if v and NATIVE.search(v)]
    except Exception:
        pass
    try:
        import urllib.parse
        for x in (fetch("https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": f"{a} {t}"})) or [])[:20]:
            tn = x.get("trackName") or ""
            if NATIVE.search(tn):
                out.append((tn, "LRCLIB"))
    except Exception:
        pass
    out = [(clean(x), src) for x, src in out]
    seen, uniq = set(), []
    for t, src in out:
        if t and t not in seen:
            seen.add(t); uniq.append((t, src))
    return uniq


def find(job, fetch, lang=None):
    """The original title for this song, checked against its title; None if no candidate reads back as it."""
    title = job.get("title") or ""
    if NATIVE.search(title):          # already written in its own script
        return None
    letters = re.sub(r"[^A-Za-z]", "", re.sub(r"\(.*?\)", "", title))
    styled = len(letters) >= 6 and letters.isupper() and " " in title.strip()   # "KABANERI OF THE IRON FORTRESS": the
    # official title is in Latin capitals on purpose; a katakana spelling of it isn't its original
    lang = lang or (job.get("result") or {}).get("lang") or job.get("lang")
    best = None
    for native, src in candidates(job, fetch):
        chars = [c for c in native if not c.isspace() and c not in "!?！？・ー-~～.。、,"]
        if not chars or sum(bool(NATIVE.match(c)) for c in chars) < 0.5 * len(chars):
            continue                  # mostly Latin ("KNOCK OUTッ!"): not an original in another script
        if styled and re.fullmatch(r"[\u30a0-\u30ff\s・ー]+", native):
            continue
        m = match(native, title, lang)
        if m >= MATCH and (not best or m > best["match"]):
            best = {"orig": native, "source": src, "read_as": read_as(native, lang), "match": round(m, 2)}
    return best
