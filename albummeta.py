"""Album, year, genre and cover art for a song, from open sources: MusicBrainz (and its Cover Art Archive), with the
iTunes Search API as a fallback for songs MusicBrainz doesn't have (or has no cover for). Only the artist and title
are sent. MusicBrainz asks for at most one request a second and a User-Agent naming the program; both are kept.

    m = lookup("Linkin Park", "Numb")
    # -> {"album": "Meteora", "year": 2003, "genre": "nu metal", "source": "musicbrainz", "ref": release-group id,
    #     "cover": b"...jpeg bytes..." or None}  or None (nothing that matches well enough)

Matching: the title (and the original-script title, when there is one) must match after normalising (case, accents,
brackets like "(Music Video)" or "feat. ..."), and the artist must share a name with one of the recording's credited
artists. Among the matching recordings' releases, an official album wins over a single or EP, an earlier release over
a later one (the original album rather than a compilation or a reissue).
"""
import json, re, threading, time, unicodedata, urllib.error, urllib.parse, urllib.request

UA = "EncoreKaraokeStudio/1.0 ( https://github.com/alainator/karaoke-studio )"
MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"
ITUNES = "https://itunes.apple.com/search"
_lock = threading.Lock()
_last = [0.0]


def _get(url, accept="application/json", timeout=15, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else json.loads(data.decode("utf-8"))


def _mb(path, **q):
    """One MusicBrainz request, never more than one a second (all threads together). It answers 503 when it's busy
    or we're too quick: wait and try again, a few times."""
    url = f"{MB}/{path}?" + urllib.parse.urlencode(dict(q, fmt="json"))
    for k in range(4):
        with _lock:
            wait = (1.1 if k == 0 else 2.5 * k) - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            return FETCH(url)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or k == 3:
                raise


_ilock = threading.Lock()
_ilast = [0.0]


def _itunes(q):
    """One iTunes search. Apple allows about 20 a minute: one every 3.2 s (all threads together); when it says
    "too many" (403/429), wait a minute and try once more."""
    url = ITUNES + "?" + urllib.parse.urlencode(q)
    for k in range(2):
        with _ilock:
            wait = 3.2 - (time.time() - _ilast[0])
            if wait > 0:
                time.sleep(wait)
            _ilast[0] = time.time()
        try:
            return FETCH(url)
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429) or k == 1:
                raise
            time.sleep(60)


FETCH = _get   # (tests swap it)
FETCH_BYTES = lambda url: _get(url, accept="image/*", binary=True)   # noqa: E731


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode() or str(s or "")
    s = s.lower()
    s = re.sub(r"\((?:[^)]*(?:music video|official|lyric|audio|video|mv|feat\.?|ft\.?|with)[^)]*)\)|\[[^\]]*\]", " ", s)
    s = re.sub(r"\b(feat\.?|ft\.?|featuring)\b.*$", " ", s)
    return re.sub(r"[^\w]+", " ", s, flags=re.UNICODE).strip()


VARIANT = re.compile(r"\b(remix|mix|live|acoustic|instrumental|karaoke|version|edit|cover|demo|remaster(ed)?|tv size|off vocal|inst)\b", re.I)


def _title_ok(want, got):
    """The same song: the titles match once normalised, and the found one isn't a remix, live take, instrumental...
    that the wanted title doesn't say it is."""
    a, b = norm(want), norm(got)
    if not a or not b:
        return False
    if VARIANT.search(str(got or "")) and not VARIANT.search(str(want or "")):
        return False
    return a == b or (len(a) > 4 and (a in b or b in a) and abs(len(a) - len(b)) <= 12)


def tidy_album(name):
    """'Meteora (Deluxe Edition)' -> 'Meteora'; 'Song - Single' -> 'Song'."""
    n = re.sub(r"\s*[\(\[][^\)\]]*(deluxe|edition|remaster|expanded|anniversary|bonus)[^\)\]]*[\)\]]", "", str(name or ""), flags=re.I)
    return re.sub(r"\s*-\s*(Single|EP)$", "", n).strip() or None


def _artist_ok(want, credits):
    w = norm(want)
    if not w:
        return False
    names = [norm(c) for c in credits if c]
    return any(n and (n == w or n in w or w in n) for n in names)


def _year(date):
    m = re.match(r"(\d{4})", str(date or ""))
    return int(m.group(1)) if m else None


def _rank(rel):
    rg = rel.get("release-group") or {}
    kind = (rg.get("primary-type") or "").lower()
    second = [x.lower() for x in rg.get("secondary-types") or []]
    k = 0 if kind == "album" and not second else 1 if kind in ("single", "ep") and not second else 2
    official = 0 if (rel.get("status") or "").lower() == "official" else 1
    return (k, official, rel.get("date") or "9999")


def from_musicbrainz(artist, titles):
    for title in titles:
        q = f'recording:"{title}" AND artist:"{artist}"'
        try:
            res = _mb("recording", query=q, limit=15)
        except Exception:
            continue
        rels = []
        for rec in res.get("recordings") or []:
            credits = [c.get("name") or (c.get("artist") or {}).get("name") for c in rec.get("artist-credit") or [] if isinstance(c, dict)]
            if not (_title_ok(title, rec.get("title")) and _artist_ok(artist, credits)):
                continue
            rels += [r for r in rec.get("releases") or [] if (r.get("release-group") or {}).get("id")]
        if not rels:
            continue
        best = min(rels, key=_rank)
        rg = best["release-group"]
        out = {"album": tidy_album(rg.get("title") or best.get("title")), "year": _year(best.get("date")), "genre": None,
               "source": "musicbrainz", "ref": rg["id"], "cover": None}
        try:   # its genres (the most voted)
            g = _mb(f"release-group/{rg['id']}", inc="genres")
            gs = sorted(g.get("genres") or [], key=lambda x: -(x.get("count") or 0))
            out["genre"] = gs[0]["name"] if gs else None
        except Exception:
            pass
        for url in (f"{CAA}/release-group/{rg['id']}/front-500", f"{CAA}/release/{best['id']}/front-500"):
            try:
                out["cover"] = FETCH_BYTES(url)
                break
            except Exception:
                continue
        return out
    return None


STORES = {"ja": "JP", "ko": "KR", "vi": "VN", "zh": "TW", "es": "MX", "pt": "BR", "fr": "FR", "de": "DE", "ru": "RU",
          "id": "ID", "th": "TH", "it": "IT", "tr": "TR", "hi": "IN", "ar": "EG", "pl": "PL", "el": "GR", "tl": "PH"}


def from_itunes(artist, titles, lang=None):
    tries = [(t, c) for t in titles for c in (["US"] + ([STORES[lang]] if lang in STORES else []))]
    if lang in STORES:   # a local song: its own country's store first
        tries.sort(key=lambda x: x[1] == "US")
    for title, country in tries:
        try:
            res = _itunes({"term": f"{artist} {title}", "entity": "song", "limit": 15, "country": country})
        except Exception:
            continue
        hits = [r for r in res.get("results") or [] if _title_ok(title, r.get("trackName")) and _artist_ok(artist, [r.get("artistName")])]
        if not hits:
            continue
        hits.sort(key=lambda r: (0 if " - single" not in (r.get("collectionName") or "").lower() else 1, r.get("releaseDate") or "9999"))
        h = hits[0]
        out = {"album": tidy_album(h.get("collectionName")), "year": _year(h.get("releaseDate")),
               "genre": h.get("primaryGenreName"), "source": "itunes", "ref": str(h.get("collectionId") or ""), "cover": None}
        art = h.get("artworkUrl100")
        if art:
            try:
                out["cover"] = FETCH_BYTES(art.replace("100x100bb", "600x600bb"))
            except Exception:
                pass
        return out
    return None


def lookup(artist, title, title_orig=None, lang=None):
    """The best album match with its cover, or None. The iTunes catalogue first (it names the original album of
    popular songs most reliably, with a big cover), MusicBrainz for what iTunes doesn't have."""
    titles = [t for t in (title, title_orig) if t]
    it = from_itunes(artist, titles, lang)
    if it and it["cover"]:
        return it
    mb = from_musicbrainz(artist, titles)
    if mb:
        if not mb["cover"] and it and it["cover"]:
            mb["cover"], mb["cover_source"] = it["cover"], "itunes"
        return mb
    return it
