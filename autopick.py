"""Pick a song's music video, lyrics and names from a search (/api/search's answer), the way the Make page does.

    from autopick import choose
    p = choose(search_result)   # {"video", "lyrics", "artist", "title", "names_from"} (video/lyrics may be None)

A port of the browser's choosing in static/index.html (`usable`, the default video in `renderResults`, `bestLyrics`,
`tidyName`, `namesFromVideo`, `autoNames`, `titleCase`), used where nobody is at the page: party auto-make (§2.2b of
contract/ENCORE-STUDIO.md). The browser keeps its own copy (it re-picks as the user clicks); tests/test_autopick.py
runs both over the same searches and fails if they disagree, so a change to one must go into the other.

JavaScript details kept on purpose: the sort is a stable sort on the same keys (null counts as 0 in arithmetic);
`\\b`, `\\d` and case-insensitive matching are ASCII-only (no `u` flag); `\\s` and trim() are JavaScript's whitespace;
string slicing counts UTF-16 code units.
"""
import re

# JavaScript's \s (and what String.prototype.trim removes)
_WS_CHARS = "\t\n\v\f\r                  　﻿"
WS = "[" + re.escape(_WS_CHARS) + "]"
_A = re.ASCII
NATIVE = re.compile(r"[぀-ヿ一-鿿가-힣]")
JP_TITLE = re.compile(r"[぀-ヿ一-鿿]")
JUNK = re.compile(WS + r"*[(\[（【][^)\]）】]*(remaster|explicit|clean|radio edit|album version|single version|official|music video|"
                  r"lyric|audio|visuali[sz]er|\bhd\b|\b4k\b|m/v|\bmv\b|\bpv\b|full ver|prod\.|produced by)[^)\]）】]*[)\]）】]",
                  _A | re.I)
REMASTER_TAIL = re.compile(WS + r"+[-–]" + WS + r"+(\d{4}" + WS + r"+)?remaster(ed)?[^\n\r  ]*\Z", _A | re.I)
MV_TAIL = re.compile(WS + r"+(official" + WS + r"+)?(music" + WS + r"+video|m/v|mv)\Z", _A | re.I)
SPACES = re.compile(WS + "{2,}")
DOUBLED = re.compile(WS + r"*[;|]" + WS + "*")
SHOUTED = re.compile(r"[A-Z]{3,}" + WS + r"+[A-Z]{3,}", _A)
CAP = re.compile(r"(^|[" + re.escape(_WS_CHARS) + r'(\-"])([a-z])', _A)
QUOTED = re.compile(r"^([^\n\r  ]+?)" + WS + r"*[「\"“『]([^\n\r  ]+?)[」\"”』]")
PAREN = re.compile(r"\(([^)]+)\)")
PAREN_ANY = re.compile(r"\([^)]*\)")
DASHED = re.compile(r"^([^\n\r  ]+?)" + WS + r"+[-–—]" + WS + r"+([^\n\r  ]+)\Z")
BAR = re.compile(WS + r"+[|｜]" + WS + "+")
LEAD_SEP = re.compile(r"^" + WS + r"*[-–—/|:]" + WS + "*")


def js_trim(s):
    return s.strip(_WS_CHARS)


def _u16(s):
    return len(s.encode("utf-16-le")) // 2


def _slice16(s, start):
    """s.slice(start) with start counted in UTF-16 code units."""
    b = s.encode("utf-16-le", "surrogatepass")[2 * start:]
    return b.decode("utf-16-le", "surrogatepass")


def num(x):
    """A value as JavaScript arithmetic sees it (null/false -> 0, true -> 1)."""
    if x is None or x is False:
        return 0
    if x is True:
        return 1
    return x


def usable(l):
    return bool(l) and (l.get("source") == "pasted" or bool((l.get("timed") or l.get("plain"))
                                                            and (l.get("latin") or l.get("japanese") or l.get("native"))
                                                            and not l.get("mismatch")))


def pick_video(d):
    """The video the Make page starts on: an unflagged one whose length matches some usable lyrics (skips short or TV
    versions), from the artist's channel first."""
    lyr = d.get("lyrics") or []
    vids = d.get("videos") or []

    def fits(v):
        return any(usable(l) and l.get("duration") and abs(l["duration"] - num(v.get("duration"))) <= 6 for l in lyr)
    clean = [v for v in vids if not v.get("flags")]
    for cond in (lambda v: v.get("official_channel") and fits(v), lambda v: v.get("official_channel"), fits):
        hit = next((v for v in clean if cond(v)), None)
        if hit:
            return hit
    return (clean[0] if clean else None) or (vids[0] if vids else None)


def best_lyrics(d, video):
    """bestLyrics(): timed first, then (for a Japanese-titled video) not an English release, the right song, a length
    that fits the video, the original script for a Japanese/Korean/Chinese song (kara.moe's hand-timed romaji for
    the rest), then the closest length."""
    ok = [l for l in d.get("lyrics") or [] if usable(l)]
    if not ok:
        return None
    dur = video.get("duration") if video else None

    def near(l):
        return bool(dur and l.get("duration") and abs(l["duration"] - dur) <= 12)
    jp_song = any(l.get("native") and l.get("name_match") for l in ok)

    def rank(l):
        km = l.get("source") == "karamoe"
        if jp_song:
            return 0 if l.get("native") else 1 if km and near(l) else 2
        return 0 if km and near(l) else 3 if km else 2 if l.get("japanese") else 1
    jp_video = bool(video) and bool(JP_TITLE.search(video.get("title") or ""))

    def eng(l):
        return 1 if jp_video and l.get("english") else 0

    def key(l):
        last = abs(num(l.get("duration")) - dur) if dur else -num(l.get("lines"))
        return (-int(bool(l.get("timed"))), eng(l), -num(l.get("name_match")), -int(near(l)), rank(l), last)
    return sorted(ok, key=key)[0]


def title_case(t):
    return CAP.sub(lambda m: m.group(1) + m.group(2).upper(), t) if t == t.lower() else t


def tidy_name(x):
    """tidyName(): without "(Official Video)", "- 2011 Remaster", a doubled "Creep;Creep", SHOUTED words."""
    t = JUNK.sub("", x or "")
    t = REMASTER_TAIL.sub("", t, count=1)
    t = MV_TAIL.sub("", t, count=1)
    t = js_trim(SPACES.sub(" ", t))
    parts = [p for p in DOUBLED.split(t) if p]
    if len(parts) > 1 and all(p.lower() == parts[0].lower() for p in parts):
        t = parts[0]
    if t == t.upper() and SHOUTED.search(t):
        t = CAP.sub(lambda m: m.group(1) + m.group(2).upper(), t.lower())
    return t


def names_from_video(v):
    """namesFromVideo(): 'Artist "Title" M/V' or 'Artist - Title | extra'."""
    if not v or not v.get("title"):
        return None
    title = v["title"]
    m = QUOTED.match(title)
    if m:
        t = m.group(2)
        par = PAREN.search(t)
        if par and not NATIVE.search(par.group(1)) and NATIVE.search(PAREN_ANY.sub("", t, count=1)):
            t = par.group(1)
        return {"artist": tidy_name(m.group(1)), "title": tidy_name(t)}
    m = DASHED.match(title)
    if m:
        return {"artist": tidy_name(m.group(1)), "title": tidy_name(BAR.split(m.group(2))[0])}
    return None


def auto_names(d, lyr, video):
    """autoNames(): from the picked lyrics (the official spelling), else the video's title, else the search."""
    typed = {"artist": title_case(js_trim(d.get("artist") or "")), "title": title_case(js_trim(d.get("title") or ""))}
    l = lyr if lyr and lyr.get("source") != "pasted" else None
    if l and l.get("artist") and l.get("title"):
        artist, title = tidy_name(l["artist"]), tidy_name(l["title"])
        if artist and title.lower().find(artist.lower()) == 0:  # "Radiohead - Creep" as the title
            rest = LEAD_SEP.sub("", _slice16(title, _u16(artist)), count=1)
            if rest and rest != title:
                title = rest
        if NATIVE.search(title) and typed["title"] and not NATIVE.search(typed["title"]):
            title = typed["title"]
        if NATIVE.search(artist) and typed["artist"] and not NATIVE.search(typed["artist"]):
            artist = typed["artist"]
        return {"artist": artist, "title": title, "from": "the lyrics"}
    v = names_from_video(video)
    if v and v["artist"] and v["title"]:
        return dict(v, **{"from": "the video's title"})
    return dict(typed, **{"from": "your search"})


def choose(d):
    """What the Make page would pick for this search: {"video", "lyrics", "artist", "title", "names_from"}."""
    video = pick_video(d)
    lyr = best_lyrics(d, video)
    n = auto_names(d, lyr, video)
    return {"video": video, "lyrics": lyr, "artist": n["artist"], "title": n["title"], "names_from": n["from"]}
