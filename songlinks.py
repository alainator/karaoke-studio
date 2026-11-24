"""Video links and song lists for the Make tab, with no network: which YouTube links are videos or playlists, the
lines of a pasted list, and a song's artist and title read from a video's title (and its channel).

    parse_link("https://youtu.be/abcdefghijk?t=30")   -> {"video": "abcdefghijk", "list": None}
    parse_list(text)                                  -> (items, left_out)
    names_from_video(title, channel)                  -> {"artist", "title", "how"}

The names follow the library's own: featured singers go into the artist joined with " & " ("Rema & Selena Gomez"),
not into the title; "(Official Video)", "[4K]", "M/V" and the like go. The Studio then checks them against LRCLIB
(app.confirm_names), whose spelling wins when it has the song.
"""
import re, urllib.parse

import autopick

MAX_IMPORT = 50          # songs per import
IMPORT_MAX_S = 10 * 60   # an import skips videos longer than this (a person can still make one by hand)

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
LIST_ID = re.compile(r"[A-Za-z0-9_-]{10,80}")
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be",
            "youtube-nocookie.com", "www.youtube-nocookie.com"}
URL_RE = re.compile(r"^(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", re.I)


def is_url(s):
    """Looks like a web link (one word, a dotted host, http(s):// or a path)."""
    s = (s or "").strip()
    return bool(s) and " " not in s and bool(URL_RE.match(s)) and ("/" in s or s.lower().startswith("http"))


def parse_link(s):
    """A YouTube link -> {"video": 11-character id or None, "list": playlist id or None}. None when it isn't a
    YouTube video or playlist link. watch?v=, youtu.be/, music.youtube.com, shorts/, embed/, live/; extra
    parameters (&t=, &si=, &index=) don't matter."""
    s = (s or "").strip()
    if not s or any(c.isspace() for c in s):
        return None
    if not re.match(r"(?i)^https?://", s):
        if not re.match(r"(?i)^(www\.|m\.|music\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/", s):
            return None
        s = "https://" + s
    try:
        u = urllib.parse.urlsplit(s)
        host = (u.hostname or "").lower()
    except ValueError:
        return None
    if host not in YT_HOSTS:
        return None
    qs = urllib.parse.parse_qs(u.query)
    vid = None
    if host.endswith("youtu.be"):
        vid = u.path.strip("/").split("/")[0]
    else:
        m = re.match(r"^/(shorts|embed|live|v|e)/([^/]+)", u.path)
        if m:
            vid = m.group(2)
        elif u.path.rstrip("/") in ("/watch", ""):
            vid = (qs.get("v") or [None])[0]
    vid = vid if vid and VIDEO_ID.fullmatch(vid) else None
    lst = (qs.get("list") or [None])[0]
    lst = lst if lst and LIST_ID.fullmatch(lst) else None
    if not vid and not lst:
        return None
    return {"video": vid, "list": lst}


def video_url(vid):
    return f"https://www.youtube.com/watch?v={vid}"


def playlist_url(lst):
    return f"https://www.youtube.com/playlist?list={lst}"


def import_kind(text):
    """What an import's text is: ("playlist", list id) for one playlist link on its own (a watch link from inside a
    playlist counts: the person pasted the playlist; not a watch link from a YouTube mix, list=RD…: autoplay adds
    those), else ("list", None)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if len(lines) == 1:
        link = parse_link(lines[0])
        if link and link["list"] and not (link["video"] and link["list"].startswith("RD")):   # (a YouTube mix: the song itself)
            return "playlist", link["list"]
    return "list", None


NUMBERING = re.compile(r"^(?:#?\d{1,3}\s*[.):\]]\s*|#\d{1,3}\s+|[-*•·]\s+)")
NUMBER_DASH = re.compile(r"^\d{1,3}\s+[-–—]\s+(?=.+\s[-–—]\s|\S+://|www\.|youtu)")   # "01 - Artist - Title" (not "311 - Title")


def parse_list(text, cap=MAX_IMPORT):
    """The songs in a pasted list: one per line, "Artist - Title" or a YouTube video link (mixed is fine). Blank
    lines go; "1. ", "01 - ", "3) ", "- " in front go. -> (items, left_out): items (at most cap) are
    {"input", "kind": "text" (with "q") | "video" (with "id") | "bad" (with "why")}."""
    items = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = NUMBER_DASH.sub("", NUMBERING.sub("", line, count=1), count=1).strip()
        if not line:
            continue
        link = parse_link(line)
        if link and link["video"]:
            items.append({"input": raw.strip(), "kind": "video", "id": link["video"]})
        elif link:
            items.append({"input": raw.strip(), "kind": "bad", "why": "a playlist link inside a list: import it on its own"})
        elif is_url(line):
            items.append({"input": raw.strip(), "kind": "bad", "why": "not a YouTube video link"})
        else:
            q = re.sub(r"\s*\t+\s*", " - ", line) if "\t" in line and " - " not in line else line  # pasted from a spreadsheet
            items.append({"input": raw.strip(), "kind": "text", "q": re.sub(r"\s+", " ", q)[:200]})
    return items[:cap], max(0, len(items) - cap)


# ---------------- names from a video's title ----------------
NATIVE = re.compile(r"[぀-ヿ一-鿿가-힣]")
_JUNK = (r"official|music\s*video|lyrics?|audio|visuali[sz]er|\bvideo\b|\bm\s*/\s*v\b|\bmv\b|\bpv\b|\bhd\b|\bhq\b|"
         r"high\s*quality|\b4k\b|\b8k\b|\d{3,4}p\b|remaster(ed)?|explicit|clean\s+version|radio\s+edit|album\s+version|"
         r"single\s+version|full\s+ver|prod\.|produced\s+by|upgrade|color\s+coded|subtitles?|\bsubs?\b|"
         r"\blive\b|performance|teaser|shorts?\b")
JUNK = re.compile(_JUNK, re.I)
BRACKETS = re.compile(r"\s*([(\[【（〔])([^()\[\]【】（）〔〕]*)([)\]】）〕])")
FEAT_IN = re.compile(r"^\s*(?:feat\.?|ft\.?|featuring|with)\s+(.+?)\s*$", re.I)
FEAT_TAIL = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s+(.+)$", re.I)
SEP = re.compile(r"\s+[-–—_]\s+|\s*[|｜]\s*|\s+~\s*|\s*～\s*")
DASH = re.compile(r"\s+[-–—_]\s+")
JP_QUOTE = re.compile(r"^(.*?)\s*[『「](.+?)[』」](.*)$")
KQUOTE = re.compile(r"^(.+?)\s+['‘\"“](.+?)['’\"”](?=$|\s|[(\[【])(.*)$")
CHANNEL_TAIL = re.compile(r"(\s*-\s*topic|\s*vevo|\s+official(\s+(youtube\s+)?channel)?|official|\s+tv|\s+channel|(?<=[a-z])Music)$", re.I)
LABEL = re.compile(r"\b(labels?|entertainment|records|recordings|music|playlists?|charts|studios?|media|network|productions?|"
                   r"vevo|smtown|1thek|stone music|kpop)\b", re.I)


def _junk_only(x):
    """A bracket or tail that is only "Official Video", "4K", "M/V", "Lyrics", "Live at …": nothing of the song's name."""
    if re.match(r"^\s*live\b", x or "", re.I):
        return True
    rest = JUNK.sub(" ", x or "")
    rest = re.sub(r"[\s\W_]+", " ", re.sub(r"\b(the|a|an|and|in|with|new|ver|version|of|by|from|on|at|full)\b", " ", rest, flags=re.I))
    return not rest.strip() or not re.sub(r"\d", "", rest).strip()


def _split_feat(x, tail=True):
    """'Title (feat. A & B)' -> ('Title', ['A & B']); also '[with A]' and (tail) 'Title ft. A'."""
    feats = []

    def br(m):
        f = FEAT_IN.match(m.group(2))
        if f:
            feats.append(f.group(1).strip())
            return ""
        return m.group(0)
    x = BRACKETS.sub(br, x)
    m = FEAT_TAIL.search(x) if tail else None
    if m:
        feats.append(m.group(1).strip())
        x = x[:m.start()]
    return x.strip(), feats


def _drop_junk(x):
    """Without junk brackets ("(Official Video)", "[4K]", "【MV】") and junk tails ("| Official Video", "~Official Video",
    "- High Quality")."""
    x = BRACKETS.sub(lambda m: "" if _junk_only(m.group(2)) else m.group(0), x)
    parts = SEP.split(x)
    keep = [p for p in parts if p.strip() and not _junk_only(p)]
    if keep and len(keep) < len(parts):   # rebuild with " - " between what's left (only dashes separate names)
        x = " - ".join(p.strip() for p in keep)
    x = re.sub(r"\s+(official\s+)?(music\s+video|m\s*/\s*v|mv|video)$", "", x.strip(), flags=re.I)
    return re.sub(r"\s{2,}", " ", x).strip(" -–—_|~")


def clean_artist(a):
    a = re.sub(r"\s+", " ", a or "").strip(" -–—_|~:")
    m = re.match(r"^(.*?[A-Za-z].*?)\s*[(（]([^)）]+)[)）]$", a)   # "BTS (방탄소년단)": the name players know
    if m and NATIVE.search(m.group(2)) and not NATIVE.search(m.group(1)):
        a = m.group(1)
    a = re.sub(r"\s+[x×]\s+", " & ", a)   # "A x B" (lower-case x: "Lil Nas X" keeps its X)
    return autopick.tidy_name(a).strip()


def clean_title(t):
    t = autopick.tidy_name(_drop_junk(t))
    par = re.match(r"^(.*?)\s*[(（]([^)）]+)[)）]$", t)   # "神메뉴(God's Menu)" -> "God's Menu (神메뉴)", the library's way
    if par and NATIVE.search(par.group(1)) and not NATIVE.search(par.group(2)) and re.search(r"[A-Za-z]", par.group(2)):
        t = f"{par.group(2).strip()} ({par.group(1).strip()})"
    return t.strip(" -–—_|~:\"'“”‘’")


def channel_artist(channel):
    """The artist a channel stands for: "Nova Lane - Topic", "NovaLaneVEVO", "Nova Lane Official" -> "Nova Lane";
    a label's or a compilation channel ("Some Records", "HYBE LABELS") -> ""."""
    c = (channel or "").strip()
    for _ in range(3):
        c2 = CHANNEL_TAIL.sub("", c).strip()
        if c2 == c or not c2:
            break
        c = c2
    if not c or LABEL.search(c):
        return ""
    if " " not in c and re.search(r"[a-z][A-Z]", c):   # "NovaLane" (VEVO names drop the spaces)
        c = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", c)
    return clean_artist(c)


def names_from_video(title, channel=""):
    """A song's artist and title from a YouTube video's title (and its channel when the title has only the song's
    name). -> {"artist", "title", "how"}: how = "title" (both from the title), "channel" (the artist from the channel),
    or None (no artist found: the title alone, cleaned)."""
    t = re.sub(r"\s+", " ", title or "").strip()
    # leading tags: "[MV]", "【MV】", "[Official Video]"
    while True:
        m = re.match(r"^\s*[(\[【（〔]([^)\]】）〕]*)[)\]】）〕]\s*", t)
        if not m or not _junk_only(m.group(1)) or not t[m.end():].strip():
            break
        t = t[m.end():]
    t, feats = _split_feat(t, tail=False)   # (a tail "ft. X - Title" is split after the names are)
    artist = song = None
    m = JP_QUOTE.match(t)
    if m and m.group(2).strip():
        before, inner, after = m.group(1), m.group(2), m.group(3)
        artist = before if before.strip() else (_drop_junk(after) if not _junk_only(after) else "")
        song = inner
    if song is None:
        m = KQUOTE.match(t)
        if m and not DASH.search(m.group(1)):   # ARTIST 'TITLE' M/V (an apostrophe in "Guns N' Roses - …" isn't one)
            artist, song = m.group(1), m.group(2)
    if song is None:
        x = _drop_junk(t)
        parts = [p for p in DASH.split(x) if p.strip()]
        if len(parts) < 2:   # "Artist | Title" (no dash): the order is checked against LRCLIB later
            parts = [p for p in re.split(r"\s+[|｜]\s+", x) if p.strip()]
        if len(parts) >= 2:
            artist, song = parts[0], " - ".join(parts[1:])
        else:
            song = x
    song, more = _split_feat(song)
    feats += more
    if artist:
        artist, more = _split_feat(artist)
        feats += more
    artist = clean_artist(artist) if artist else ""
    song = clean_title(song)
    how = "title" if artist else None
    if not artist:
        artist = channel_artist(channel)
        how = "channel" if artist else None
    if artist:
        for f in feats:
            f = clean_artist(re.sub(r"\s*(,|\band\b)\s*", " & ", f))
            if f and f.lower() not in artist.lower():
                artist = f"{artist} & {f}"
    if not song:   # (a title that was only junk)
        song, how = clean_title(t) or t, how
    return {"artist": artist, "title": song, "how": how, "feats": feats}
