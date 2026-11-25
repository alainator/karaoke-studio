#!/usr/bin/env python3
"""Karaoke Studio (2026-11-17): make karaoke songs from official music videos, preview, publish to the library.

    python -I app.py [--host 127.0.0.1] [--port 5570]      (settings: .env, see .env.example)

Search (YouTube + LRCLIB) -> job (pipeline.py subprocess, one at a time on the GPU) -> preview in the browser
(SubtitlesOctopus, the libass renderer in the browser) with a timing slider -> publish into the library Encore
plays (optionally deleting the older YouTube lyric version), listed in its catalog.json.
There is no login of its own: put it behind an authenticating reverse proxy, or keep it on 127.0.0.1.
"""
import argparse, asyncio, hashlib, http.cookiejar, ipaddress, json, math, os, queue, re, shutil, sqlite3, subprocess, sys
import threading, time, uuid
import logging, logging.handlers
import unicodedata, urllib.error, urllib.parse, urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
from assgen import (MODES, display_modes, job_display, load_cast, load_lines, save_lines, variant_name,  # noqa: E402
                    who_of, write_lyrics)
import versions  # noqa: E402
import albummeta  # noqa: E402
import catalog  # noqa: E402
import hanzi  # noqa: E402
import romanize  # noqa: E402
import trimtools  # noqa: E402
import videoswap  # noqa: E402
import encore  # noqa: E402
import bundle  # noqa: E402
import pitch as pitchmod  # noqa: E402
import guidevocals  # noqa: E402
import schemalite  # noqa: E402
import gpu as gpumod  # noqa: E402
import agents  # noqa: E402
import automake as AM
import videocheck as VC  # noqa: E402
import autopick  # noqa: E402
import songlinks as SL  # noqa: E402
import wordscore  # noqa: E402

# what the studio did and when: data/logs/studio.log (5 files of 2 MB kept)
os.makedirs(C.LOGS, exist_ok=True)
log = logging.getLogger("studio")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.handlers.RotatingFileHandler(os.path.join(C.LOGS, "studio.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(_h)

JOBS, LIB, MV_DIR = C.JOBS, C.LIBRARY, C.MV_DIR
UA = {"User-Agent": C.USER_AGENT}
NEED_GPU_MIB = C.MIN_GPU_FREE_MIB
BIN = os.path.dirname(sys.executable)  # yt-dlp lives in the same virtualenv
PIPELINE = os.path.join(HERE, "pipeline.py")  # makes one song, as its own process (tests swap in a stand-in)
STEPS = ["queued", "lyrics", "download", "separate", "align", "render", "done"]
ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9-]{1,60}$")

os.makedirs(JOBS, exist_ok=True)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
lock = threading.Lock()


# ---------------- helpers ----------------
def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "-", s).strip(" .")
    return re.sub(r"\s+", " ", s)[:100] or "untitled"


def http_json(url, timeout=20, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            busy = isinstance(e, urllib.error.HTTPError) and e.code == 429  # rate-limited (LRCLIB, after quick searches)
            if i == tries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code < 500 and not busy):
                raise
            wait = 1.0 + i
            if busy:
                try:
                    wait = min(15.0, max(2.0, float(e.headers.get("Retry-After") or 0) or 3.0 * (i + 1)))
                except (TypeError, ValueError):
                    wait = 3.0 * (i + 1)
            time.sleep(wait)


def job_path(jid, *parts):
    if not ID_RE.match(jid or ""):
        raise HTTPException(404, "no such job")
    p = os.path.join(JOBS, jid, *parts)
    if not os.path.isdir(os.path.join(JOBS, jid)):
        raise HTTPException(404, "no such job")
    return p


def load(jid):
    return json.load(open(job_path(jid, "job.json")))


def save(jid, job):
    p = job_path(jid, "job.json")
    with open(p + ".tmp", "w") as f:
        json.dump(job, f, indent=1)
    os.replace(p + ".tmp", p)


def check_origin(req: Request):
    o = req.headers.get("origin")
    if o and o.rstrip("/") not in C.ORIGINS | {f"http://{req.url.netloc}"}:
        raise HTTPException(403, "bad origin")


def gpu_free_mib():
    return gpumod.free_mib()


def rebuild_lyrics(jid, job, lines=None, cast=None, offset=None):
    """Write the song's lyrics.ass in its display mode, and a lyrics.<mode>.ass per display mode (assgen.py).
    lines/cast/offset: the new ones, if they aren't saved in words.json / the job yet. True if lyrics.ass changed."""
    return write_lyrics(job_path(jid), job, lines=lines, cast=cast, offset=offset)


# a published song in the library: base.mp4, base.ass (its lyrics in the song's display mode) and, for songs
# with original-script lines, base.<mode>.ass per display mode (folder-based players read only base.ass and don't list .ass
# files as songs, so these sit there unused until a player lets the singer pick), and for players like Encore:
# base.words.json, base.pitch.json and base.vocals.m4a (the guide vocals; folder-based players don't list .m4a files)
LIB_EXTS = (".mp4", ".ass") + tuple(f".{m}.ass" for m in MODES) + (catalog.WORDS_EXT, catalog.PITCH_EXT, catalog.VOCALS_EXT, catalog.COVER_EXT)


def publish_lyrics(jid, base):
    """Copy the song's lyrics.ass and its display-mode variants next to base.mp4 in the library."""
    have = set()
    for src, ext in [(job_path(jid, "lyrics.ass"), ".ass")] + [(job_path(jid, variant_name(m)), f".{m}.ass") for m in MODES]:
        if not os.path.exists(src):
            continue
        tmp = f"{MV_DIR}/.{base}{ext}.part"
        shutil.copyfile(src, tmp)
        os.replace(tmp, f"{MV_DIR}/{base}{ext}")
        have.add(ext)
    for m in MODES:  # a mode the song no longer has (its original lines were removed)
        if f".{m}.ass" not in have and os.path.exists(f"{MV_DIR}/{base}.{m}.ass"):
            os.remove(f"{MV_DIR}/{base}.{m}.ass")
    # base.words.json: the lines, words and singers in video time, for Encore (catalog.py); never stops a publish
    try:
        catalog.write_words(job_path(jid), load(jid), MV_DIR, base)
    except Exception:
        log.exception("catalog: couldn't write the words file of %s", base)


def update_catalog():
    """Rewrite catalog.json in the library root (catalog.py) after a published song changed; never raises."""
    catalog.refresh(JOBS, LIB, MV_DIR, log)


# ---- album, year, genre and cover (albummeta.py): looked up once per song, editable on the song page ----
# job["album_meta"]: {"status": "found" | "none" (nothing matched) | "hand" (set on the song page) | "cleared",
#                     "album", "year", "genre", "source", "ref", "at", "by"}; the picture is the job's cover.jpg.
def _is_image(b):
    return bool(b) and (b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n")


def album_lookup(jid, by="studio"):
    """Look the song up now (blocking: a thread). Returns the new album_meta."""
    job = load(jid)
    m = albummeta.lookup(job.get("artist"), job.get("title"), job.get("title_orig"), (job.get("result") or {}).get("lang") or job.get("lang"))
    meta = {"status": "found" if m else "none", "at": round(time.time()), "by": by}
    cover = job_path(jid, "cover.jpg")
    if m:
        meta.update(album=m.get("album"), year=m.get("year"), genre=m.get("genre"), source=m.get("cover_source") or m.get("source"), ref=m.get("ref"))
    with lock:
        job = load(jid)
        if (job.get("album_meta") or {}).get("status") == "hand" and by == "studio":
            return job["album_meta"]          # (set by hand meanwhile: keep it)
        if m and _is_image(m.get("cover")):
            with open(cover + ".part", "wb") as f:
                f.write(m["cover"])
            os.replace(cover + ".part", cover)
        elif os.path.exists(cover):
            os.remove(cover)
        job["album_meta"] = meta
        save(jid, job)
    return meta


def album_fill():
    """Every made song without album details yet, one at a time (the sources' own pace), then the catalog."""
    time.sleep(45)
    n = 0
    for jid in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        try:
            job = load(jid)
        except Exception:
            continue
        if job.get("status") != "done" or job.get("album_meta"):
            continue
        try:
            album_lookup(jid)
            n += 1
        except Exception:
            log.exception("album: lookup failed for %s", jid)
        if n and n % 25 == 0:
            update_catalog()
    if n:
        update_catalog()
        log.info("album: looked up %d songs", n)


catalog.PLAYS = lambda: encore.play_rows(STORE)  # the catalog's play counts: this process's player store


def final_base(job):
    return f"{safe_name(job['artist'])} - {safe_name(job['title'])} (Music Video)"


# ---------------- search ----------------
BAD_WORDS = ("lyric", "karaoke", "live", "cover", "reaction", "remix", "instrumental", "8d", "sped up", "slowed",
             "nightcore", "tutorial", "lesson", "audio only", "visualizer", "fan made", "unofficial", "concert",
             "dance practice", "practice", "performance video", "performance", "making film", "making", "teaser",
             "behind", "fancam", "focus cam", "choreography", "relay dance", "dance cover", "skzoo ver", "shorts")


def yt_json(args, timeout=60):
    """yt-dlp's JSON answer for args (a search, one video, a playlist). Tests swap this for made-up answers.
    RuntimeError with yt-dlp's last words when it fails."""
    r = subprocess.run([f"{BIN}/yt-dlp", "--js-runtimes", "node", *args], capture_output=True, text=True, timeout=timeout,
                       env=C.tool_env())
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[-200:])
    return json.loads(r.stdout)


def video_flags(title):
    """The BAD_WORDS in a video's title ("lyric", "live", "cover"...), without the ones inside another ("dance cover").
    A word counts from its start, so "lyrics" and "covers" do and "Alive", "Discover" don't."""
    nt_ = norm(title)
    bad = [w for w in BAD_WORDS if re.search(r"\b" + re.escape(w), nt_)]   # from a word's start: "live" isn't "Alive"
    return [w for w in bad if not any(w != o and w in o for o in bad)]


def video_entry(e, artist, title):
    """One YouTube result (yt-dlp's entry) as the search lists it, scored for the song artist/title; None without an id
    or a length."""
    if not e or not e.get("id") or not e.get("duration"):
        return None
    na, nt = norm(artist), norm(title)
    t, ch = (e.get("title") or ""), (e.get("channel") or "")
    nt_, nch = norm(t), norm(ch)
    score = 0.0
    # the artist's own channel (or their VEVO); a result without a channel name proves nothing
    if na and nch and (na in nch or nch.replace(" ", "") in (na.replace(" ", "") + "vevo") or nch.endswith("vevo") and na.replace(" ", "") in nch.replace(" ", "")):
        score += 5
    if e.get("channel_is_verified"):
        score += 1.5
    if "official" in nt_ and "unofficial" not in nt_:
        score += 2
    if re.search(r"\bm v\b|\bmv\b|music video", nt_):  # "M/V" (K-pop labels), "MV", "Music Video"
        score += 2
    if nt and nt in nt_:
        score += 1
    bad = video_flags(t)
    score -= 4 * len(bad)
    score += math.log10((e.get("view_count") or 0) + 1) / 3
    return {"id": e["id"], "title": t, "channel": ch, "duration": int(e["duration"]),
            "views": e.get("view_count"), "verified": bool(e.get("channel_is_verified")),
            "official_channel": score >= 5 and bool(nch), "flags": bad, "score": round(score, 2),
            "thumb": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"}


def yt_search(artist, title):
    q = f"ytsearch10:{artist} {title} official music video"
    try:
        d = yt_json(["--flat-playlist", "-J", "--", q])
    except RuntimeError as e:
        raise HTTPException(502, "YouTube search failed: " + str(e))
    out = [v for v in (video_entry(e, artist, title) for e in d.get("entries") or []) if v]
    out.sort(key=lambda x: -x["score"])
    return out


KANA = re.compile(r"[\u3040-\u30ff]")
# common English words that romanised Japanese practically never contains (to tell English lyrics from romaji)
ENGLISH_HINTS = set("the you i and my me is your it's i'm don't can't of in it love all that this what with be are "
                    "we they was for on so just know never can will".split())


def lrc_search(artist, title, extra_titles=()):
    q = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    hits = [(h, False) for h in http_json(f"https://lrclib.net/api/search?{q}")]
    if len(hits) < 3:  # loose search as a fallback
        hits += [(h, False) for h in http_json("https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": f"{artist} {title}"}))]
    for t in extra_titles:  # e.g. the Japanese title kara.moe knows for a romaji search
        hits += [(h, True) for h in http_json("https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": t}))]
    seen, out = set(), []
    for h, by_title in hits:
        if h["id"] in seen or h.get("instrumental"):
            continue
        seen.add(h["id"])
        synced = h.get("syncedLyrics") or ""
        first = next((re.sub(r"^\[[^\]]*\]\s*", "", l) for l in synced.splitlines() if re.sub(r"^\[[^\]]*\]\s*", "", l).strip()), "")
        text = re.sub(r"\[[^\]]*\]", "", synced) or (h.get("plainLyrics") or "")
        letters = [c for c in text if c.isalpha()]
        japanese = bool(KANA.search(text))
        from romanize import script_of
        script = script_of(text.splitlines())  # by line: a line or two in another script doesn't make the song that script
        native = script != "latin"  # Japanese, Korean, Chinese and every other script the studio reads
        toks = re.findall(r"[a-z']+", text.lower())
        # (only for lyrics without Japanese: a Japanese song with a few English words isn't "English lyrics")
        english = not native and bool(toks) and sum(t in ENGLISH_HINTS for t in toks) / len(toks) > 0.12
        latin = not native and (not letters or sum(unicodedata.name(c, "").startswith("LATIN") for c in letters) / len(letters) >= 0.85)
        artist_ok = not artist or (lambda a, b: bool(a) and bool(b) and (a in b or b in a))(loose(artist), loose(h.get("artistName") or ""))
        mt = lambda x: re.sub(r"[\W_]+", " ", unicodedata.normalize("NFKC", x or "").lower()).strip()  # keeps 漢字/한글
        native_title = bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7a3]", title)) and bool(mt(title)) \
            and mt(title) in mt(h.get("trackName") or "")
        sample = " ".join(re.sub(r"^\[[^\]]*\]\s*", "", l) for l in synced.splitlines()[:14])
        out.append({"source": "lrclib", "id": h["id"], "artist": h.get("artistName"), "title": h.get("trackName"), "_sample": sample,
                    "album": h.get("albumName"), "duration": h.get("duration"), "timed": bool(synced.strip()),
                    "plain": bool((h.get("plainLyrics") or "").strip()),
                    "latin": latin, "japanese": japanese, "native": native, "script": script, "english": english,
                    "romaji": "automatic" if native else None,
                    "lines": sum(1 for l in (synced or h.get("plainLyrics") or "").splitlines() if l.strip()),
                    "first": (first or next((l.strip() for l in (h.get("plainLyrics") or "").splitlines() if l.strip()), ""))[:80],
                    "artist_match": artist_ok,
                    # a native-script title (月亮代表我的心) that matches counts even if the artist is typed in English
                    "name_match": native_title or (artist_ok and (by_title or (bool(norm(title)) and norm(title) in norm(h.get("trackName") or ""))))})
    return out[:20]


def check_romaji_entries(lyrics):
    """Romaji uploads are sometimes the wrong song (a 'Brave Shine' upload with Gurenge's lyrics). When Japanese lyrics
    of the song exist, romanise them and mark romaji entries that clearly don't match."""
    import difflib
    jp = next((l for l in lyrics if l["source"] == "lrclib" and l.get("japanese") and l["timed"] and l.get("name_match")), None) \
        or next((l for l in lyrics if l["source"] == "lrclib" and l.get("japanese") and l["timed"]), None)
    if not jp:
        return
    try:
        import cutlet
        kat = cutlet.Cutlet(); kat.use_foreign_spelling = False
        ref = loose(kat.romaji(jp["_sample"]))
    except Exception:
        return
    for l in lyrics:
        if l["source"] == "lrclib" and l["latin"] and not l.get("english") and l["timed"] and l.get("_sample"):
            sim = difflib.SequenceMatcher(None, loose(l["_sample"])[:400], ref[:400], autojunk=False).ratio()
            l["romaji_check"] = round(sim, 2)
            if sim < 0.45:
                l["mismatch"] = True


def loose(x):
    """Romaji compare that ignores long-vowel spelling (ô / ou / oo) and spaces."""
    x = norm(x).replace(" ", "")
    for a, b in (("ou", "o"), ("oo", "o"), ("uu", "u"), ("aa", "a"), ("ei", "e")):
        x = x.replace(a, b)
    return x


def km_search(title, artist=""):
    """kara.moe (Karaoke Mugen): hand-timed romaji for anime/J-pop. Only full-length entries can match a music
    video; also returns the Japanese titles it knows, to search LRCLIB with."""
    try:
        d = http_json("https://kara.moe/api/karas/search?" + urllib.parse.urlencode({"filter": title, "size": 12}))
    except Exception:
        return [], []
    out, jp = [], []
    for k in d.get("content") or []:
        t = k.get("titles") or {}
        want = loose(title)
        if not any(want and v and (want in loose(v) or loose(v) in want) for v in t.values()) and title not in t.values():
            continue  # kara.moe's search is fuzzy; keep only real title matches
        if t.get("jpn") and t["jpn"] not in jp:
            jp.append(t["jpn"])
        names = lambda key: ", ".join(x.get("name", "") for x in k.get(key) or [])
        versions = names("versions")
        if not k.get("lyrics_infos") or not ("Full" in versions or (k.get("duration") or 0) >= 150):
            continue
        who = names("singergroups") or names("singers")
        people = " ".join((who, names("songwriters"), names("series"), names("franchises")))
        artist_ok = not artist or loose(artist) in loose(people) or any(loose(x) in loose(artist) for x in who.split(", ") if x)
        out.append({"source": "karamoe", "id": k["kid"], "artist": who or names("series") or names("franchises"),
                    "title": t.get("qro") or t.get("eng") or t.get("jpn"), "album": "kara.moe · " + ", ".join(
                        x for x in (names("series") or names("franchises"), names("songtypes"), versions) if x),
                    "duration": k.get("duration"), "timed": True, "latin": True, "japanese": False,
                    "romaji": "hand", "lines": None, "first": "", "name_match": artist_ok})
    return out, jp[:2]


def split_query(q):
    """'radiohead creep' -> ('Radiohead', 'Creep'): find which part of a search without ' - ' is the artist, from LRCLIB
    (an entry whose artist and title together make up the search, else one whose artist starts or ends it) or from
    YouTube titles written 'Artist - Title'. Returns (artist, title) or None."""
    nq = norm(q)
    if not nq:
        return None
    try:
        hits = http_json("https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": q}))[:30]
    except Exception:
        hits = []
    from collections import Counter
    seen_artist = Counter(norm(h.get("artistName")) for h in hits)  # a real artist shows up in many entries
    rank = lambda c: (seen_artist[norm(c[0])], c[2])  # then: artist written first in the search
    full, part = [], []
    words = q.split()
    for h in hits:
        a, t = norm(h.get("artistName")), norm(h.get("trackName"))
        if a and t and a in nq and t in nq and abs(len(a) + len(t) + 1 - len(nq)) <= 3 and nq.replace(a, "", 1).strip() == t:
            full.append((h["artistName"], h["trackName"], nq.startswith(a)))  # artist and title together are the search
        n = len(a.split())
        if a and n < len(words) and norm(" ".join(words[:n])) == a:   # it starts with an artist: the rest is the title
            part.append((h["artistName"], " ".join(words[n:]), True))
        if a and n < len(words) and norm(" ".join(words[-n:])) == a:  # or ends with one
            part.append((h["artistName"], " ".join(words[:-n]), False))
    for cands in (full, part):
        if cands:
            a, t, _ = max(cands, key=rank)
            return a, t
    try:  # YouTube titles like "Artist - Title (Official Music Video)"
        for v in yt_search("", q)[:8]:
            m = re.match(r"^(.+?)\s+[-–—]\s+(.+?)(\s*[(\[].*)?$", v.get("title") or "")
            if m and norm(m.group(1)) and norm(m.group(2)) and norm(m.group(1)) in nq and norm(m.group(2)) in nq:
                return m.group(1).strip(), m.group(2).strip()
    except Exception:
        pass
    return None


def find_lyrics(artist, title):
    """The lyrics half of a search: kara.moe and LRCLIB entries (romaji checked against the Japanese original).
    -> (lyrics, notes, failed): failed when LRCLIB didn't answer (so "nothing found" isn't believed)."""
    notes = []
    km, jp_titles = km_search(title.strip(), artist.strip())
    try:
        lyrics = km + lrc_search(artist, title, jp_titles)
        if not any(l.get("japanese") for l in lyrics) and artist:  # look for the Japanese original to check romaji against
            try:
                extra = [l for l in lrc_search(artist, title, [f"{artist} {title}"]) if l.get("japanese")]
                lyrics += [l for l in extra if l["id"] not in {x["id"] for x in lyrics}][:3]
            except Exception:
                pass
        check_romaji_entries(lyrics)
        for l in lyrics:
            l.pop("_sample", None)
        failed = False
    except Exception as e:
        lyrics, failed = km, True
        notes.append(f"LRCLIB didn't answer ({str(e)[:60]}); search again in a moment")
    return lyrics, notes, failed


def song_search(artist, title, videos=True):
    """The search answer for a song whose names are known: videos (unless videos=False) and lyrics."""
    vids = yt_search(artist, title) if videos else []
    lyrics, notes, failed = find_lyrics(artist, title)
    return {"artist": artist.strip(), "title": title.strip(), "videos": vids, "lyrics": lyrics, "notes": notes,
            **({"lyrics_failed": True} if failed else {})}


def confirm_names(artist, title, feats=()):
    """Names read from a video's title, checked against LRCLIB: an entry with that title (brackets aside) and an artist
    alike gives its spelling (also with the two the other way round: "Title - Artist" uploads). Featured singers the
    entry leaves out are kept. Without an artist: split_query finds one in the title. -> (artist, title, source):
    source "LRCLIB", "the search" (split_query) or None (as read)."""
    bare = lambda x: loose(re.sub(r"\s*[(\[][^)\]]*[)\]]", "", x or ""))
    same_t = lambda a, b: bool(loose(a)) and (loose(a) == loose(b) or (bool(bare(a)) and bare(a) == bare(b)))
    na = lambda x: loose(re.sub(r"\band\b|&|,", " ", x or "", flags=re.I))   # "Queen and David Bowie" = "Queen & David Bowie"
    alike = lambda a, b: bool(na(a)) and bool(na(b)) and (na(a) in na(b) or na(b) in na(a))
    if not artist:
        got = split_query(title) if title else None
        return (got[0], got[1], "the search") if got else ("", title, None)
    try:
        hits = http_json("https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": f"{artist} {title}"}))[:30]
    except Exception:
        hits = []
    from collections import Counter
    for a, t in ((artist, title), (title, artist)):
        m = [h for h in hits if same_t(h.get("trackName"), t) and alike(h.get("artistName"), a)]
        if m:
            exact = [h for h in m if loose(h.get("trackName")) == loose(t)] or m
            common = lambda xs: Counter(xs).most_common(1)[0][0]   # the spelling most uploads agree on (ties: first)
            same_a = [h for h in m if na(h["artistName"]) == na(a)]   # all the singers the video names, when LRCLIB has them
            names_ = Counter(h["artistName"] for h in same_a or m)   # (with "&" first, the library's way: "Queen & David Bowie")
            la = autopick.tidy_name(max(names_, key=lambda x: (" & " in x, names_[x])))
            lt = autopick.tidy_name(common(h["trackName"] for h in exact))
            if loose(lt) != loose(t) and not re.search(r"[(\[]", t):   # "Title [1984]" on LRCLIB, "Title" in the video
                lt = re.sub(r"\s*[(\[][^)\]]*[)\]]", "", lt).strip() or lt
            if feats and not all(loose(f) in loose(la) for f in feats):
                la = a
            return la or a, lt or t, "LRCLIB"
    return artist, title, None


def names_for_video(title, channel):
    """A video's song names: read from its title (songlinks.names_from_video), then checked against LRCLIB.
    -> {"artist", "title", "from", "parsed"} (parsed: an artist was found)."""
    g = SL.names_from_video(title, channel)
    a, t, src = confirm_names(g["artist"], g["title"], g["feats"])
    how = src or {"title": "the video's title", "channel": "the video's title and channel"}.get(g["how"])
    return {"artist": a, "title": t, "from": how or "the video's title", "parsed": bool(a)}


def read_video(vid):
    """One YouTube video's facts (yt-dlp -J --no-playlist, only for an 11-character id). HTTPException 502 when it
    can't be read."""
    try:
        return yt_json(["-J", "--no-playlist", "--", SL.video_url(vid)], timeout=90)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
        log.info("reading video %s failed: %s", vid, str(e)[-160:])
        raise HTTPException(502, "couldn't read that video (private, removed, or not available here)")


def link_search(vid):
    """The search answer for a pasted video link: that video first (marked as the person's link), its names from its
    title, then the usual videos and lyrics for those names."""
    info = read_video(vid)
    names = names_for_video(info.get("title"), info.get("channel") or info.get("uploader"))
    try:
        d = song_search(names["artist"], names["title"])
    except HTTPException:   # YouTube's search failed: the link and the lyrics are still worth showing
        d = song_search(names["artist"], names["title"], videos=False)
        d["notes"].append("YouTube's search didn't answer, so only your video is listed")
    v = video_entry(dict(info, id=vid), names["artist"], names["title"])
    if not v:
        raise HTTPException(400, "that video has no length (a live stream?): pick another")
    v["link"] = True
    d["videos"] = [v] + [x for x in d["videos"] if x["id"] != vid]
    d["pinned"] = vid
    d["link"] = {"id": vid, "names_from": names["from"], "parsed": names["parsed"]}
    if not names["parsed"]:
        d["notes"].insert(0, "Couldn't tell the artist from the video's title: type the artist in (the title is the video's, cleaned up)")
    return d


@app.get("/api/search")
def search(q: str):
    q = q.strip()
    link = SL.parse_link(q)
    if link and link["video"]:   # a pasted video link names the song (watch?v=, youtu.be/, shorts/, &list= is ignored)
        return link_search(link["video"])
    if link:
        raise HTTPException(400, "that's a playlist link: use Import a list (under the search box) to make its songs")
    if SL.is_url(q):
        raise HTTPException(400, "only YouTube video links can be searched; for a video from another site, make the song "
                                 "from its YouTube video, then add yours on the song's page (Videos)")
    if not q or len(q) > 200:
        raise HTTPException(400, "type Artist - Title")
    artist, _, title = q.partition(" - ")
    if not title:
        artist, title = "", q
        guess = split_query(q)  # typed without ' - ': work out which part is the artist
        if guess:
            artist, title = guess
    return song_search(artist, title)


# ---------------- jobs ----------------
_LYR_PREVIEW = {}


@app.get("/api/lyrics")
def lyrics_preview(source: str, id: str):
    """The text of one lyrics entry from the search, so it can be read before it's picked (like watching a video)."""
    key = f"{source}:{id}"
    if key not in _LYR_PREVIEW:
        try:
            if source == "lrclib" and id.isdigit():
                d = http_json(f"https://lrclib.net/api/get/{int(id)}")
                lines = [[int(m.group(1)) * 60 + float(m.group(2)), m.group(3).strip()] for m in
                         (re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", x.strip()) for x in (d.get("syncedLyrics") or "").splitlines()) if m and m.group(3).strip()]
                if len(lines) < 4:
                    lines = [[None, x.strip()] for x in (d.get("plainLyrics") or "").splitlines() if x.strip()]
            elif source == "karamoe" and re.fullmatch(r"[0-9a-f-]{36}", id):
                k = http_json(f"https://kara.moe/api/karas/{id}")
                info = next((l for l in k.get("lyrics_infos") or [] if l.get("default")), (k.get("lyrics_infos") or [None])[0])
                if not info:
                    raise HTTPException(404, "this kara.moe entry has no lyrics")
                url = "https://kara.moe/downloads/lyrics/" + urllib.parse.quote(info["filename"])
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                    text = r.read().decode("utf-8-sig")
                import karamoe
                lines = [[t_, w_] for t_, w_ in karamoe.parse(text)]
            else:
                raise HTTPException(400, "unknown lyrics entry")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"couldn't load these lyrics ({str(e)[:60]})")
        if len(_LYR_PREVIEW) > 200:
            _LYR_PREVIEW.clear()
        _LYR_PREVIEW[key] = lines[:400]
    return {"lines": _LYR_PREVIEW[key]}


def new_job(fields, actor):
    """A new song in the queue (job.json): fields = yt, lrc_id, lyrics_source, artist, title, video_title, gpu_pref and
    any extras. Returns its id."""
    slug = re.sub(r"[^a-z0-9]+", "-", norm(f"{fields['artist']} {fields['title']}"))[:60].strip("-") or "song"
    with lock:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        jid, n = f"{stamp}-{slug}", 1
        while os.path.exists(f"{JOBS}/{jid}"):  # two of the same song in one second
            n += 1
            jid = f"{stamp}-{slug[:57].strip('-')}-{n}"
        os.makedirs(f"{JOBS}/{jid}")
        # a title in its own script ("踊り子"): kept as the original title, the title romanized ("Odoriko"), so it reads
        # and sorts in Latin letters everywhere and players show the original next to it
        if not fields.get("title_orig") and catalog.split_orig(fields.get("title")):   # "Odoriko (踊り子)"
            t, o = catalog.split_orig(fields["title"])
            fields = dict(fields, title=t, title_orig=o)
        elif not fields.get("title_orig") and catalog.has_own_script(fields.get("title")):
            rt = romanize.romanize_title(fields["title"])
            if rt:
                fields = dict(fields, title_orig=fields["title"], title=rt)
        save(jid, {"id": jid, "status": "queued", "step": "queued", "gpu_pref": "auto", **fields,
                   "created": time.time(), "offset": 0.0, "lyrics_display": C.LYRICS_DISPLAY if C.LYRICS_DISPLAY in MODES else "both",
                   "made_by": agents.stamp(actor), "log": []})
    return jid


@app.post("/api/jobs")
async def create_job(req: Request):
    check_origin(req)
    b = await req.json()
    if b.get("replace"):  # "Change video or lyrics": the new song takes the old one's place
        _agent_refuse(agents.actor_of(req), "remake", "changing a song's video or lyrics")
    yt, lrc_id = str(b.get("yt", "")), b.get("lrc_id")
    source = b.get("lyrics_source") or "lrclib"
    artist, title = str(b.get("artist", "")).strip(), str(b.get("title", "")).strip()
    pasted = str(b.get("lyrics_text") or "").strip()[:20000]
    if source == "pasted":
        lrc_id = None
    ok_lyrics = (source == "lrclib" and isinstance(lrc_id, int)) or \
                (source == "karamoe" and isinstance(lrc_id, str) and re.fullmatch(r"[0-9a-f-]{36}", lrc_id)) or \
                (source == "pasted" and sum(1 for l in pasted.splitlines() if l.strip()) >= 4)
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", yt) or not ok_lyrics or not artist or not title:
        raise HTTPException(400, "pick a video and lyrics (or paste at least 4 lines), and give artist and title")
    auto_pub = bool(b.get("auto_publish"))
    if auto_pub:   # published without review when its video check passes (ks make --auto): a publish, so its scope
        _agent_refuse(agents.actor_of(req), "publish", "publishing automatically")
    ir = b.get("import_row") if isinstance(b.get("import_row"), dict) else {}   # Change… on an import's review
    ir = {"id": ir["id"], "n": ir["n"]} if IMP_ID.match(str(ir.get("id") or "")) and isinstance(ir.get("n"), int) else None
    jid = new_job({"yt": yt, "lrc_id": lrc_id, "lyrics_source": source, "artist": artist, "title": title,
                   "video_title": str(b.get("video_title", ""))[:200], **({"import": ir} if ir else {}),
                   "gpu_pref": b.get("gpu") if b.get("gpu") in ("local", "remote") else "auto",
                   **({"lyrics_text": pasted} if source == "pasted" else {}),
                   **({"auto_publish": {"by": agents.actor_of(req), "at": time.time()}} if auto_pub else {})}, agents.actor_of(req))
    log.info("queued %s | video %s | lyrics %s:%s | gpu %s%s", jid, yt, source, lrc_id if source != "pasted" else f"{len(pasted)} chars",
             b.get("gpu") or "auto", f" | replaces {b.get('replace')}" if b.get("replace") else "")
    if ir:
        import_row_made(ir["id"], ir["n"], jid, artist, title)
    # "Change video or lyrics" on an existing song: the new one takes its place once it has been made (if it fails,
    # the old song is still there). Only the studio's copy is removed; a published file stays in the library until the
    # new song is published over it.
    old = str(b.get("replace") or "")
    rids = []
    if ID_RE.match(old) and old != jid and os.path.exists(os.path.join(JOBS, old, "job.json")):
        set_job(jid, replaces=old)
        try:
            rids = list(load(old).get("encore_rids") or [])
        except Exception:
            pass
    rid = str(b.get("request") or "")
    if rid:  # "Find and make" on a song request (Requests): the request follows this song from now on
        r = STORE.get_request(rid)
        if r and r["status"] not in ("declined",):
            rids.append(rid)
    if rids:
        set_job(jid, encore_rids=sorted(set(rids)))
        refresh_requests()
    wake.set()
    return {"id": jid}


_CHECK = {}  # job id -> (words.json mtime, lines still marked to check)


def to_check(jid):
    """How many lines of a finished song are still marked ⚠ (marks go away as the user edits or clears them)."""
    try:
        path = job_path(jid, "words.json")
        mt = os.path.getmtime(path)
        if _CHECK.get(jid, (None,))[0] != mt:
            data = json.load(open(path))
            lines = data["lines"] if isinstance(data, dict) else []
            _CHECK[jid] = (mt, sum(1 for l in lines if isinstance(l, dict) and l.get("weak")))
        return _CHECK[jid][1]
    except Exception:
        return None


def summary(job):
    out = _summary(job)
    out["to_check"] = to_check(job["id"]) if job.get("status") == "done" else None
    out["updates"] = versions.missing(job) if job.get("status") == "done" else []
    out["can_undo_remake"] = bool(job.get("remade")) and not job.get("remake_undone") and job.get("status") == "done" \
        and os.path.exists(job_path(job["id"], "words.before-remake.json"))
    out["remake_note"] = job.get("remake_note")
    out["encore_flags"] = flag_counts().get(job["id"], 0)
    out["keep_backing"] = bool(job.get("keep_backing"))   # the song page's switch (the render's own: result.backing)
    out["voice_swaps"] = page_swaps(job)                    # in the page's video time; the render's: voice_swaps_now
    out["voice_swaps_now"] = page_swaps(job, used=True)
    out["title_orig"] = job.get("title_orig")              # the song page's Original title (its own script)
    lang = (job.get("result") or {}).get("lang") or job.get("lang")
    if lang in hanzi.CHINESE:                               # Chinese songs: which characters players see (hanzi.py)
        orig = job.get("title_orig") or (catalog.split_orig(job.get("title")) or (None, None))[1]
        out["hanzi"] = {"song": job.get("hanzi"), "studio": hanzi.studio_choice(), "shown": hanzi.song_choice(job),
                        "choices": [{"value": c, "label": hanzi.LABELS[c]} for c in hanzi.CHOICES],
                        "available": hanzi.available(),
                        "title_orig": catalog.shown_title_orig(job, orig) if orig else None}
    out["album_meta"] = job.get("album_meta")              # album, year, genre (albummeta.py); its cover: /cover
    out["has_cover"] = os.path.exists(job_path(job["id"], "cover.jpg"))
    return out


def _summary(job):
    keys = ("id", "artist", "title", "yt", "status", "step", "created", "finished", "error", "offset",
            "published", "published_as", "video_title", "result", "waiting", "edited", "unpublished_changes", "lang", "gpu", "ai_romaji", "video_v", "gpu_pref", "reviewed", "lrc_id", "lyrics_source", "trim", "video_task",
            "lyrics_display", "cancel_requested", "last_edit", "published_by", "made_by", "quick", "auto_held", "auto_retry", "auto_publish",
            "import", "library_removed")
    out = {k: job.get(k) for k in keys}
    am = job.get("automake")
    out["automake"] = {k: am.get(k) for k in ("player_name", "by", "party", "at")} if am else None
    return out


@app.get("/api/jobs")
def list_jobs(brief: int = 0):
    """Every song, newest first. brief=1: only what the Songs list shows (about a fifth of the size)."""
    out = []
    for jid in sorted(os.listdir(JOBS), reverse=True):
        if ID_RE.match(jid) and os.path.exists(f"{JOBS}/{jid}/job.json"):
            try:
                out.append(brief_summary(summary(load(jid))) if brief else summary(load(jid)))
            except Exception:
                pass
    return out


BRIEF_KEYS = ("id", "artist", "title", "yt", "status", "step", "created", "finished", "error", "published", "waiting",
              "unpublished_changes", "gpu", "reviewed", "to_check", "cancel_requested", "encore_flags", "published_as", "import",
              "library_removed")


def brief_summary(s):
    """A song as the Songs list needs it; empty fields are left out (570 songs: about 250 KB instead of 950 KB)."""
    r = s.get("result") or {}
    out = {k: s.get(k) for k in BRIEF_KEYS}
    out["lang"] = s.get("lang") or r.get("lang")
    out["result"] = {"confidence": r.get("confidence")} if r.get("confidence") else None
    out["updates_n"] = len(s.get("updates") or [])  # improvements since it was made (the song page lists them)
    q = s.get("quick")
    out["quick"] = 1 if q and not q.get("reviewed") else None   # auto-made and published, nobody has checked it yet
    return {k: v for k, v in out.items() if v not in (None, False, 0, "", [])}


def job_index():
    """Every song's name and state, straight from job.json (no lyrics files read): for matching songs by name."""
    out = []
    for jid in os.listdir(JOBS):
        if ID_RE.match(jid):
            try:
                j = json.load(open(os.path.join(JOBS, jid, "job.json")))
            except Exception:
                continue
            if j.get("artist") and j.get("title"):
                out.append({k: j.get(k) for k in ("id", "artist", "title", "status", "published", "published_as",
                                                  "created", "finished", "cancel_requested", "encore_rids", "error",
                                                  "started", "step", "gpu", "gpu_pref", "automake", "auto_held", "yt")})
    return out


@app.get("/api/jobs-like")
def jobs_like(artist: str = "", title: str = "", exclude: str = ""):
    """Songs already made (or waiting / being made) with this artist and title: the Make page asks before making the
    same song again."""
    out = [j for j in job_index() if j["id"] != exclude and j["status"] in ("done", "queued", "running")
           and same_song(j["artist"], j["title"], artist, title)]
    return sorted(out, key=lambda j: -(j.get("created") or 0))


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    job = load(jid)
    s = summary(job)
    s["log"] = job.get("log", [])[-30:]
    s["steps"] = STEPS
    if job.get("status") == "done":
        s["videos"] = videos_listing(jid, job)
        try:  # how the lyrics can be shown (songs with original-script lines), and how this song shows them
            lines = load_lines(job_path(jid, "words.json"))
            s["display_modes"] = display_modes(lines)
            s["lyrics_display"] = job_display(job, lines, s["display_modes"])
            s["video_check"] = video_check(job, lines)
        except Exception:
            s["display_modes"], s["lyrics_display"] = [], None
    return s


FILES = {"video.mp4": ("out.mp4", "video/mp4"), "vocals.m4a": ("vocals.m4a", "audio/mp4"),
         "lyrics.ass": ("lyrics.ass", "text/plain; charset=utf-8"),
         **{variant_name(m): (variant_name(m), "text/plain; charset=utf-8") for m in MODES}}


@app.get("/api/jobs/{jid}/file/{name}")
def job_file(jid: str, name: str):
    if name not in FILES:
        raise HTTPException(404)
    fn, ctype = FILES[name]
    p = job_path(jid, fn)
    if not os.path.exists(p):
        raise HTTPException(404, "not made yet")
    # video and vocals never change once made (pages ask for them with ?v=<finished time>), so the browser may keep
    # what it already downloaded and jump around without fetching it again; the lyrics change on every Save
    cache = "no-store" if name.endswith(".ass") else "private, max-age=604800"
    return FileResponse(p, media_type=ctype, headers={"Cache-Control": cache})


@app.post("/api/jobs/{jid}/offset")
async def set_offset(jid: str, req: Request):
    check_origin(req)
    b = await req.json()
    off = float(b.get("offset", 0))
    if not -5 <= off <= 5:
        raise HTTPException(400, "offset must be within ±5 s")
    actor = agents.actor_of(req)
    with lock:
        job = load(jid)
        rebuild_lyrics(jid, job, offset=off)
        if round(off, 2) != job.get("offset"):
            if job.get("published"):
                job["unpublished_changes"] = True
            if job.get("status") == "done":  # (undoable like a lines edit)
                what = f"whole-song offset {round(off, 2):+.2f} s"
                agents.snapshot(job_path(jid), actor, what, offset=job.get("offset") or 0.0)
                _edited(jid, job, actor, what)
        job["offset"] = round(off, 2)
        save(jid, job)
    return {"offset": job["offset"]}


@app.post("/api/jobs/{jid}/backing")
async def set_backing(jid: str, req: Request):
    """{on: bool, swaps?: [[start, end], ...] in the song page's video time}: "Keep backing vocals": the next make or
    remake takes only the lead singer out of the music (the harmonies and choirs stay; the guide vocals are the lead
    only). Swaps: where the model took the wrong voice for the lead, the two trade places. Nothing changes until it's
    remade."""
    check_origin(req)
    b = await req.json()
    with lock:
        job = load(jid)
        if job.get("status") not in ("done", "failed"):
            raise HTTPException(409, "wait until it's finished")
        what = []
        if "on" in b:
            job["keep_backing"] = bool(b.get("on"))
            what.append("keep backing vocals " + ("on" if job["keep_backing"] else "off"))
        if "swaps" in b:
            sw = b.get("swaps")
            if not isinstance(sw, list) or len(sw) > 20:
                raise HTTPException(400, "swaps: a list of [start, end] (at most 20)")
            t0 = float((job.get("trim") or {}).get("start") or 0)   # the page's video is the trimmed one
            dur = float((job.get("result") or {}).get("duration") or 1e9)
            out = []
            for x in sw:
                if not (isinstance(x, list) and len(x) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in x)):
                    raise HTTPException(400, "each swap is [start, end] in seconds")
                a, e = sorted(float(v) for v in x)
                a, e = max(0.0, a + t0), min(dur, e + t0)
                if e - a < 0.3:
                    raise HTTPException(400, "a swap is at least 0.3 s long")
                out.append([round(a, 2), round(e, 2)])
            out.sort()
            for p, n in zip(out, out[1:]):
                if n[0] < p[1]:
                    raise HTTPException(400, "swaps can't overlap")
            job["voice_swaps"] = out
            what.append("voices swapped at " + (", ".join(f"{a - t0:.1f}-{e - t0:.1f} s" for a, e in out) or "nowhere"))
        if not what:
            raise HTTPException(400, "give on and/or swaps")
        job.setdefault("log", []).append("backing vocals: " + "; ".join(what) + " (from the next remake)")
        _edited(jid, job, agents.actor_of(req), "; ".join(what))
        save(jid, job)
    log.info("job %s: %s", jid, "; ".join(what))
    return {"keep_backing": bool(job.get("keep_backing")), "swaps": page_swaps(job)}


def page_swaps(job, used=False):
    """The voice swaps in the song page's video time (the trimmed video): the job's, or (used) the current render's."""
    t0 = float((job.get("trim") or {}).get("start") or 0)
    src = (job.get("result") or {}).get("voice_swaps") if used else job.get("voice_swaps")
    return [[round(a - t0, 2), round(b - t0, 2)] for a, b in (src or [])]


@app.post("/api/jobs/{jid}/display")
async def set_display(jid: str, req: Request):
    """How a song with original-script lines shows its lyrics: both (original above the romanized line), original,
    romaji, kana (Japanese), or both_original (the original as the main line, the romanized line small above it). lyrics.ass (what is published) is drawn this way; every way is also kept as
    lyrics.<mode>.ass for players that let the singer pick."""
    check_origin(req)
    mode = str((await req.json()).get("mode") or "")
    with lock:
        job = load(jid)
        if job.get("status") != "done":
            raise HTTPException(400, "finish making it first")
        modes = display_modes(load_lines(job_path(jid, "words.json")))
        if mode not in modes:
            raise HTTPException(400, "this song can be shown as: " + (", ".join(modes) or "(only one way: it has no original-script lines)"))
        job["lyrics_display"] = mode
        if rebuild_lyrics(jid, job) and job.get("published"):
            job["unpublished_changes"] = True
        job.setdefault("log", []).append(f"lyrics shown as: {mode}")
        _edited(jid, job, agents.actor_of(req), f"lyrics shown as: {mode}")
        save(jid, job)
    return {"lyrics_display": mode, "unpublished_changes": bool(job.get("unpublished_changes"))}


# ---------------- lyric editor ----------------
@app.post("/api/jobs/{jid}/hanzi")
async def set_hanzi(jid: str, req: Request):
    """Chinese songs: the characters players see, traditional, simplified or lyrics (as the lyrics have them); null
    = the Studio's setting (KARAOKE_LYRICS_HANZI). Only what's shown changes (the subtitles, the published words'
    original rows, the original title): the lyrics, their timing and the pinyin / jyutping stay as they are."""
    check_origin(req)
    want = (await req.json()).get("hanzi")
    if want is not None and want not in hanzi.CHOICES:
        raise HTTPException(400, "hanzi: " + ", ".join(hanzi.CHOICES) + " or null (the Studio's setting)")
    with lock:
        job = load(jid)
        if job.get("status") != "done":
            raise HTTPException(400, "finish making it first")
        if ((job.get("result") or {}).get("lang") or job.get("lang")) not in hanzi.CHINESE:
            raise HTTPException(400, "only for Chinese songs")
        before = hanzi.song_choice(job)
        if want is None:
            job.pop("hanzi", None)
        else:
            job["hanzi"] = want
        files = []
        write_lyrics(job_path(jid), job, report=files)
        # published: the library's subtitles, words and title change with it (even when lyrics.ass doesn't: a song
        # shown romanized still publishes its Chinese rows)
        if job.get("published") and (files or before != hanzi.song_choice(job)):
            job["unpublished_changes"] = True
        what = f"Chinese characters: {hanzi.LABELS[hanzi.song_choice(job)]}" + ("" if want else " (the Studio's setting)")
        job.setdefault("log", []).append(what)
        _edited(jid, job, agents.actor_of(req), what)
        save(jid, job)
    return {"hanzi": summary(job).get("hanzi"), "unpublished_changes": bool(job.get("unpublished_changes"))}


@app.get("/api/jobs/{jid}/lines")
def get_lines(jid: str):
    job = load(jid)
    if job["status"] != "done":
        raise HTTPException(400, "finish making it first")
    lines = load_lines(job_path(jid, "words.json"))
    # Chinese lines as players show them, keyed by the line as written ("orig" itself stays as written: it is what
    # an edit saves and what the pinyin comes from)
    shown = {}
    lang = (job.get("result") or {}).get("lang") or job.get("lang")
    if lang in hanzi.CHINESE:
        origs = [l.get("orig") for l in lines]
        conv, _ = hanzi.show_lines(origs, lang, hanzi.song_choice(job))
        shown = {o: c for o, c in zip(origs, conv) if o and c != o}
    return {"lines": lines, "cast": load_cast(job_path(jid, "words.json")), "orig_shown": shown,
            "offset": job.get("offset", 0.0),
            "can_reset": os.path.exists(job_path(jid, "words.orig.json")),
            "skipped": (job.get("result") or {}).get("skipped", []),
            "duration": (job.get("result") or {}).get("duration")}


def _valid_lines(raw, duration):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 500:
        raise HTTPException(400, "expected 1-500 lines")
    limit = (duration or 3600) + 60
    out = []
    for l in raw:
        words = l.get("words") if isinstance(l, dict) else None
        if not isinstance(words, list) or not 1 <= len(words) <= 60:
            raise HTTPException(400, "each line needs 1-60 words")
        ww = l.get("wwho") if isinstance(l.get("wwho"), list) and len(l["wwho"]) == len(words) else [None] * len(words)
        clean = []
        for w, who in zip(words, ww):
            if not (isinstance(w, list) and len(w) == 3 and all(isinstance(x, (int, float)) for x in w[:2])
                    and isinstance(w[2], str)):
                raise HTTPException(400, "bad word entry")
            a, b, t = float(w[0]), float(w[1]), w[2].strip()[:80]
            if not (math.isfinite(a) and math.isfinite(b)) or not 0 <= a <= limit or not 0 <= b <= limit:
                raise HTTPException(400, "word time out of range")
            if t:
                clean.append(([round(a, 3), round(max(a, b), 3), t], who_of(who)))
        if not clean:
            continue
        clean.sort(key=lambda x: x[0][0])  # a word's own singer stays with it
        wwho = [who for _, who in clean]
        clean = [w for w, _ in clean]
        orig = l.get("orig")
        sg = l.get("singer")
        heard = l.get("heard")
        mem = l.get("members")
        mem = sorted({int(i) for i in mem if isinstance(i, int) and 0 <= i < 8}) if isinstance(mem, list) else None
        out.append({"words": clean, "orig": orig.strip()[:300] if isinstance(orig, str) and orig.strip() else None,
                    "singer": sg if sg in (1, 2, 3) else None, "members": mem or None,
                    "heard": heard.strip()[:200] if isinstance(heard, str) and heard.strip() else None,
                    "wwho": wwho if any(wwho) else None, "weak": bool(l.get("weak")),
                    "why": l["why"].strip()[:160] if l.get("weak") and isinstance(l.get("why"), str) and l["why"].strip() else None,
                    "ok": bool(l.get("ok")) and not l.get("weak"), "backing": bool(l.get("backing")),
                    "note": l.get("note") if isinstance(l.get("note"), dict) else None})  # (signed in put_lines)
    if not out:
        raise HTTPException(400, "no lines left")
    out.sort(key=lambda l: l["words"][0][0])
    return out


def _agent_refuse(actor, scope, what):
    """403 for an agent whose key doesn't allow this (the gate checks the endpoint; this is for what's in the body)."""
    if not agents.can(actor, scope):
        raise HTTPException(403, f"this agent key ({actor['name']}) doesn't allow {what} (scope '{scope}': "
                                 f"{agents.scope_label(scope)}); an admin can allow it on the Agents tab")


def _check_edited(job, b):
    """ks (and any client) can send the song's "edited" time it read: if the song changed since, nothing is saved."""
    if "if_edited" in b and b.get("if_edited") != job.get("edited"):
        who = (job.get("last_edit") or {}).get("by")
        raise HTTPException(409, "the song was edited since you read it" + (f" (by {who})" if who else "") + "; read it again")


def _edited(jid, job, actor, what):
    """Record who edited the song and when (shown on its page)."""
    job["last_edit"] = agents.stamp(actor, what)
    log.info("edit %s by %s (%s): %s", jid, actor["name"], actor["kind"], what)


@app.post("/api/jobs/{jid}/lines")
async def put_lines(jid: str, req: Request):
    check_origin(req)
    b = await req.json()
    actor = agents.actor_of(req)
    with lock:
        job = load(jid)
        if job["status"] != "done":
            raise HTTPException(400, "finish making it first")
        _check_edited(job, b)
        wp = job_path(jid, "words.json")
        old, old_cast = load_lines(wp), load_cast(wp)
        lines = _valid_lines(b.get("lines"), (job.get("result") or {}).get("duration"))
        cast = b.get("cast")
        cast = [str(x).strip()[:40] for x in cast if str(x).strip()][:8] if isinstance(cast, list) else old_cast
        for l in lines:
            if l.get("members"):
                l["members"] = [i for i in l["members"] if i < len(cast)] or None
            if l.get("wwho"):
                l["wwho"] = [None if not x else x if "singer" in x else ({"members": [i for i in x["members"] if i < len(cast)]}
                             if any(i < len(cast) for i in x["members"]) else None) for x in l["wwho"]]
                l["wwho"] = l["wwho"] if any(l["wwho"]) else None
        notes = agents.keep_notes(old, lines, actor)
        cleared = agents.marks_cleared(old, lines)
        if cleared:  # a mark that goes because its line was moved or reworded is an edit, not this
            _agent_refuse(actor, "clear_marks", f"clearing ⚠ marks ({cleared} line{'s' if cleared != 1 else ''})")
        if notes:
            _agent_refuse(actor, "notes", "changing notes")
        what = " ".join(str(b.get("what") or "").split())[:120] or agents.describe(old, lines)
        if not os.path.exists(job_path(jid, "words.orig.json")):
            shutil.copyfile(wp, job_path(jid, "words.orig.json"))
        agents.snapshot(job_path(jid), actor, what, lines=old, cast=old_cast)
        save_lines(wp, lines, cast)
        ass_same = not rebuild_lyrics(jid, job, lines, cast)
        job["edited"] = time.time()
        _edited(jid, job, actor, what)
        if job.get("published") and not ass_same:  # clearing a ⚠ mark changes nothing on screen: nothing to republish
            job["unpublished_changes"] = True
        save(jid, job)
    return {"lines": len(lines), "edited": job["edited"]}


@app.get("/api/jobs/{jid}/history")
def edit_history(jid: str):
    """The song's undoable edits, newest first: who (person, agent, ks or web), what, when."""
    job_path(jid)
    return {"history": agents.entries(job_path(jid)), "last_edit": load(jid).get("last_edit")}


@app.post("/api/jobs/{jid}/lines/undo")
async def undo_edit(jid: str, req: Request):
    """Put back the song as it was before its last edit, whoever made it. {"id"}: undo only if that's still the last
    one (a client that showed "Undo: <what>" mustn't undo someone else's newer edit)."""
    check_origin(req)
    b = await req.json() if (req.headers.get("content-length") or "0") != "0" else {}
    actor = agents.actor_of(req)
    with lock:
        job = load(jid)
        if job["status"] != "done":
            raise HTTPException(400, "finish making it first")
        e = agents.last(job_path(jid))
        if not e:
            raise HTTPException(400, "nothing to undo (Undo all edits goes back to the automatic version)")
        if b.get("id") and b["id"] != e["id"]:
            raise HTTPException(409, f"the last edit is now someone else's ({e.get('by')}: {e.get('what')}); look again")
        changed = False
        if "lines" in e:
            wp = job_path(jid, "words.json")
            save_lines(wp, e["lines"], e.get("cast") or [])
            changed = rebuild_lyrics(jid, job, load_lines(wp), load_cast(wp))
        if "offset" in e:
            changed = rebuild_lyrics(jid, job, offset=e["offset"]) or changed
            job["offset"] = round(float(e["offset"]), 2)
        agents.drop(job_path(jid), e["id"])
        job["edited"] = time.time()
        _edited(jid, job, actor, f"undo: {e.get('what')} ({e.get('by')})")
        if job.get("published") and changed:
            job["unpublished_changes"] = True
        save(jid, job)
    return {"undone": {k: e.get(k) for k in ("id", "by", "kind", "what", "at")}, "left": len(agents.entries(job_path(jid))),
            "edited": job["edited"]}


@app.post("/api/jobs/{jid}/notes")
async def put_notes(jid: str, req: Request):
    """A note on lines, signed by whoever sends it (an agent's key name, the signed-in person): {"lines": [1-based line
    numbers], "text", "mark": flag lines that aren't marked ⚠, "if_edited"}. Empty text removes the sender's own note.
    Lines the person checked as fine (✓) get no note. Undo takes it back like any edit."""
    check_origin(req)
    b = await req.json()
    actor = agents.actor_of(req)
    text = " ".join(str(b.get("text") or "").split())[:300]
    nums = b.get("lines")
    if not isinstance(nums, list) or not nums or not all(isinstance(n, int) for n in nums) or len(nums) > 100:
        raise HTTPException(400, "lines: a list of line numbers (1-based, as shown)")
    with lock:
        job = load(jid)
        if job["status"] != "done":
            raise HTTPException(400, "finish making it first")
        _check_edited(job, b)
        wp = job_path(jid, "words.json")
        lines, cast = load_lines(wp), load_cast(wp)
        old = json.loads(json.dumps(lines))
        for n in nums:
            if not 1 <= n <= len(lines):
                raise HTTPException(400, f"line {n} doesn't exist (1-{len(lines)})")
            l = lines[n - 1]
            if not text:
                if l.get("note") and l["note"].get("by") == actor["name"]:
                    l["note"] = None
                continue
            if l.get("ok") and not l.get("weak"):
                raise HTTPException(409, f"line {n} is checked as fine (✓): that's the person's call, so no note")
            if not l.get("weak") and not b.get("mark"):
                raise HTTPException(400, f"line {n} isn't marked ⚠; add mark (ks note --mark) to flag it with this note")
            if not l.get("weak"):
                l["weak"], l["ok"], l["why"] = True, False, f"flagged by {actor['name']}"
            l["note"] = {"by": actor["name"], "kind": actor["kind"], "text": text, "at": round(time.time(), 3)}
        if json.dumps(lines) == json.dumps(old):
            return {"changed": 0, "edited": job.get("edited")}
        what = (f"note on line{'s' if len(nums) != 1 else ''} " + ", ".join(map(str, nums))) if text else "note removed"
        agents.snapshot(job_path(jid), actor, what, lines=old, cast=cast)
        save_lines(wp, lines, cast)
        rebuild_lyrics(jid, job, lines, cast)
        job["edited"] = time.time()
        _edited(jid, job, actor, what)
        save(jid, job)
    return {"changed": len(nums), "edited": job["edited"]}


@app.post("/api/jobs/{jid}/trim")
async def trim_song(jid: str, req: Request):
    """Cut the start and/or end off a made song's video (ads, an intro skit, a long title card). start/end are seconds
    in the ORIGINAL video; start 0 and no end puts the whole video back. Lines move with the cut. Lines sung outside
    the kept part are refused unless drop_lines is true."""
    check_origin(req)
    actor = agents.actor_of(req)
    b = await req.json()
    try:
        start = max(0.0, float(b.get("start") or 0))
        end = float(b["end"]) if b.get("end") not in (None, "", 0) else None
    except (TypeError, ValueError):
        raise HTTPException(400, "start and end are seconds")

    def work():
        with lock:
            job = load(jid)
            if job["status"] != "done":
                raise HTTPException(400, "finish making it first")
            if video_busy(job):
                raise HTTPException(409, VIDEO_BUSY)
            res = job.get("result") or {}
            full = (res.get("trim") or {}).get("full") or res.get("duration") or 0
            if end is not None and (end <= start + 5 or end > full + 0.5):
                raise HTTPException(400, f"the end must be at least 5 s after the start and at most {full:.1f} s")
            if start >= full - 5:
                raise HTTPException(400, "the start is too close to the end of the video")
            old_start = float((job.get("trim") or {}).get("start") or 0)
            bad = trimtools.lines_cut_off(job_path(jid), old_start, start, end)
            if bad and not b.get("drop_lines"):
                raise HTTPException(409, "lines " + ", ".join(map(str, bad[:12])) + (" and more" if len(bad) > 12 else "")
                                    + " are sung outside the part you keep: move or delete them first, or trim anyway to drop them")
            jd = job_path(jid)
            custom = bool(videoswap.entries(jd))
            videoswap.put_official_back(jd)  # trimtools keeps (and cuts from) whatever out.mp4 is: it must be the official one
            try:
                length = trimtools.apply_trim(jd, start, end, old_start=old_start, offset=job.get("offset", 0.0))
            finally:
                if custom:  # custom videos follow the cut: made again from their places in the original video
                    keys = videoswap.official_changed(jd)
                    claim_video(job, "custom videos, for the new trim")
                    save(jid, job)
                    rerender_later(jid, keys, "custom videos, for the new trim")
            job["video_v"] = time.time()  # a new out.mp4: the page mustn't play its cached copy
            if start <= 0.05 and end is None:
                job.pop("trim", None)
            else:
                job["trim"] = {"start": round(start, 2), "end": round(end, 2) if end else None}
            job["result"] = json.load(open(job_path(jid, "result.json")))
            job["edited"] = time.time()
            agents.retire(jd)  # (earlier lines are in the old timing)
            _edited(jid, job, actor, "trim " + (f"{start:.1f}-{(end or full):.1f} s" if start > 0.05 or end else "removed"))
            if job.get("published"):
                job["unpublished_changes"] = True
            job.setdefault("log", []).append(f"trimmed to {start:.1f}-{(end or full):.1f} s of the video ({length:.1f} s)"
                                             if job.get("trim") else "trim removed: the whole video again")
            save(jid, job)
            return {"duration": round(length, 1), "trim": job.get("trim"), "dropped": bad if b.get("drop_lines") else [],
                    "videos": keys if custom else []}
    return await asyncio.to_thread(work)


# ---------------- custom videos (videoswap.py) ----------------
# People's own videos for a song: the song keeps its sound and lyrics and plays over another picture, either in place
# of the official video (out.mp4; the official is kept for "Restore official") or as a named second version published
# as its own library entry. Downloading, lining up and rendering run in a background thread, one at a time per song;
# job["video_task"] shows what it's doing (the song's status stays "done", so its page stays usable).
VIDEO_BUSY = "it's still making a video for this song; wait for it to finish"


def video_busy(job):
    return bool((job.get("video_task") or {}).get("running"))


def claim_video(job, what):
    """Mark the job as busy with a video (call under the lock, then save)."""
    job["video_task"] = {"running": True, "what": what, "step": "start", "text": "starting", "started": time.time(), "error": None}


def _video_progress(jid, **kw):
    with lock:
        job = load(jid)
        job["video_task"] = dict(job.get("video_task") or {}, **kw)
        save(jid, job)


def run_video(jid, what, fn):
    """Run fn(progress) -> (message, main_video_changed) in a background thread; the job was claimed already."""
    def go():
        try:
            msg, changed = fn(lambda step, text: _video_progress(jid, step=step, text=text))
            with lock:
                job = load(jid)
                job["video_task"] = {"running": False, "what": what, "done": time.time(), "error": None, "text": msg}
                if changed:  # out.mp4 is new: the page must fetch it again, and the library has the old one
                    job["video_v"] = time.time()
                    if job.get("published"):
                        job["unpublished_changes"] = True
                job.setdefault("log", []).append(msg)
                save(jid, job)
            log.info("video %s | %s", jid, msg)
        except Exception as e:
            err = str(e)[:300] or e.__class__.__name__
            log.warning("video %s | %s failed: %s", jid, what, err)
            try:
                with lock:
                    job = load(jid)
                    job["video_task"] = {"running": False, "what": what, "done": time.time(), "error": err}
                    job.setdefault("log", []).append(f"video ({what}) failed: {err[:150]}")
                    save(jid, job)
            except Exception:
                pass
    threading.Thread(target=go, daemon=True).start()


def rerender_later(jid, keys, what):
    """Make these custom videos again (after a trim or remake), from their stored places in the original video."""
    def fn(progress):
        jd = job_path(jid)
        done, failed, unsure, main = 0, [], 0, False
        for k in keys:
            d = videoswap.entry_dir(jd, k)
            meta = videoswap.load_meta(d) or {}
            label = "the replacement video" if k == "replace" else f"“{meta.get('name') or k}”"
            try:
                progress("render", f"making {label} again")
                m = videoswap.rerender(jd, k, realign=bool(meta.get("realign")))
                done += 1
                main = main or k == "replace"
                unsure += m.get("aligned") == "none"
            except Exception as e:
                failed.append(label)
                if meta:
                    meta.update(error=str(e)[:200]); videoswap.save_meta(d, meta)
        if failed and not done:
            raise RuntimeError("couldn't make " + ", ".join(failed) + " again")
        msg = f"{what}: {done} made again" + (f", {', '.join(failed)} failed (Re-render to try again)" if failed else "") \
            + (f"; {unsure} couldn't be lined up by sound: check where they start" if unsure else "")
        return msg, main
    run_video(jid, what, fn)


def version_base(job, meta):
    """A version's name in the library: "Artist - Title (Mia's version)"."""
    return f"{safe_name(job['artist'])} - {safe_name(job['title'])} ({safe_name(meta.get('name') or meta['slug'])})"


def videos_listing(jid, job=None):
    job = job or load(jid)
    jd = job_path(jid)
    items = []
    for m in videoswap.entries(jd):
        x = videoswap.public(m, jd)
        x["v"] = round(m.get("made_at") or 0)
        if m["mode"] == "version":
            x["publish_name"] = version_base(job, m)
            # lyrics edited since this version was published (publishing the song or the version updates them)
            x["lyrics_unpublished"] = bool(m.get("published") and (job.get("edited") or 0) > max(m.get("published") or 0, m.get("lyrics_published") or 0))
        items.append(x)
    return {"replaced": any(x["mode"] == "replace" for x in items), "items": items, "task": job.get("video_task"),
            "official_v": round(job.get("video_v") or job.get("finished") or 0)}


def _video_params(b):
    """mode, name, offset, fill, lead from a request (JSON body or query string)."""
    mode = str(b.get("mode") or "")
    if mode not in ("replace", "version"):
        raise HTTPException(400, "choose: replace the official video, or add it as another version")
    name = re.sub(r"\s+", " ", str(b.get("name") or "")).strip()[:60]
    if mode == "version" and not name:
        raise HTTPException(400, "give the version a name, like “Mia's version”")
    off = b.get("offset")
    try:
        off = None if off in (None, "") else float(off)
    except (TypeError, ValueError):
        raise HTTPException(400, "the start is in seconds")
    if off is not None and not -3600 < off < 3600:
        raise HTTPException(400, "that start is too far off")
    fill = b.get("fill") if b.get("fill") in videoswap.FILLS else "hold"
    lead = b.get("lead") if b.get("lead") in videoswap.LEADS else "hold"
    return mode, (name if mode == "version" else None), off, fill, lead


def _check_new_version(jid, mode, name):
    if mode == "version" and os.path.exists(os.path.join(videoswap.version_dir(job_path(jid), videoswap.slugify(name)), "meta.json")):
        raise HTTPException(409, "there's already a version with that name: pick another name, or remove that one first")


def _added(mode, name, meta):
    how = {"sound": f"lined up by sound (starts at {meta['offset']:+.2f} s, sure: {round(100 * (meta.get('confidence') or 0))}%)",
           "manual": f"starting at {meta['offset']:+.2f} s, as given",
           "none": "not lined up: " + (meta.get("reason") or "no match") + "; it starts with the song, nudge it into place"}[meta["aligned"]]
    return ("replaced the official video with a custom one" if mode == "replace" else f"added the version “{name}”") + ", " + how


@app.get("/api/jobs/{jid}/videos")
def list_videos(jid: str):
    return videos_listing(jid)


@app.post("/api/jobs/{jid}/videos")
async def add_video_link(jid: str, req: Request):
    """Add a custom video from a YouTube (or other yt-dlp) link. Body: {url, mode: replace|version, name (versions),
    offset (optional: seconds of the song where the video's start plays; otherwise it's lined up by sound),
    fill: hold|loop, lead: hold|black}. Runs in the background; GET .../videos shows how it's going."""
    check_origin(req)
    b = await req.json()
    url = str(b.get("url") or "").strip()
    if not re.match(r"https?://[^\s/]+\.[^\s]+$", url):
        raise HTTPException(400, "paste a web link to the video (https://...)")
    mode, name, off, fill, lead = _video_params(b)
    what = "the replacement video" if mode == "replace" else f"the version “{name}”"
    with lock:
        job = load(jid)
        if job["status"] != "done":
            raise HTTPException(400, "finish making it first")
        if video_busy(job):
            raise HTTPException(409, VIDEO_BUSY)
        _check_new_version(jid, mode, name)
        claim_video(job, what)
        _edited(jid, job, agents.actor_of(req), what)
        save(jid, job)

    def fn(progress):
        inc = job_path(jid, "videos", f"incoming-{int(time.time() * 1000)}")
        try:
            progress("download", "downloading the video")
            src = videoswap.fetch(url, inc)
            meta = videoswap.add(job_path(jid), mode, name, src, {"kind": "link", "url": url}, off, fill, lead, progress)
        finally:
            shutil.rmtree(inc, ignore_errors=True)
        return _added(mode, name, meta), mode == "replace"
    run_video(jid, what, fn)
    return {"ok": True}


@app.put("/api/jobs/{jid}/videos/upload")
async def add_video_file(jid: str, req: Request, mode: str = "", name: str = "", offset: str = "", fill: str = "hold",
                         lead: str = "hold", filename: str = "video.mp4"):
    """Add a custom video from a file: the request body is the file itself (no form encoding), the rest in the query
    string, as for a link. The file is streamed to disk; then it's lined up and rendered in the background."""
    check_origin(req)
    mode, name, off, fill, lead = _video_params({"mode": mode, "name": name, "offset": offset, "fill": fill, "lead": lead})
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in videoswap.VIDEO_EXTS:
        raise HTTPException(400, "that doesn't look like a video file (" + ", ".join(e[1:] for e in videoswap.VIDEO_EXTS[:6]) + "…)")
    size = int(req.headers.get("content-length") or 0)
    if size > videoswap.MAX_SOURCE_BYTES:
        raise HTTPException(413, "that file is too big (4 GB at most)")
    what = "the replacement video" if mode == "replace" else f"the version “{name}”"
    with lock:
        job = load(jid)
        if job["status"] != "done":
            raise HTTPException(400, "finish making it first")
        if video_busy(job):
            raise HTTPException(409, VIDEO_BUSY)
        _check_new_version(jid, mode, name)
        claim_video(job, what)
        _edited(jid, job, agents.actor_of(req), what)
        job["video_task"].update(step="upload", text="receiving the file")
        save(jid, job)
    inc = job_path(jid, "videos", f"incoming-{int(time.time() * 1000)}")
    path = os.path.join(inc, "upload" + ext)
    try:
        os.makedirs(inc, exist_ok=True)
        got = 0
        with open(path, "wb") as f:
            async for chunk in req.stream():
                got += len(chunk)
                if got > videoswap.MAX_SOURCE_BYTES:
                    raise HTTPException(413, "that file is too big (4 GB at most)")
                f.write(chunk)
        if got < 1000:
            raise HTTPException(400, "the file didn't arrive (it's empty)")
    except BaseException as e:
        shutil.rmtree(inc, ignore_errors=True)
        with lock:
            job = load(jid)
            job["video_task"] = {"running": False, "what": what, "done": time.time(),
                                 "error": e.detail if isinstance(e, HTTPException) else "the upload stopped before the end"}
            save(jid, job)
        raise
    shown = os.path.basename(filename)[:120]

    def fn(progress):
        try:
            meta = videoswap.add(job_path(jid), mode, name, path, {"kind": "file", "filename": shown, "bytes": got}, off, fill, lead, progress)
        finally:
            shutil.rmtree(inc, ignore_errors=True)
        return _added(mode, name, meta), mode == "replace"
    run_video(jid, what, fn)
    return {"ok": True, "bytes": got}


def _entry_meta(jid, key):
    try:
        d = videoswap.entry_dir(job_path(jid), key)
    except ValueError:
        raise HTTPException(404, "no such video")
    m = videoswap.load_meta(d)
    if not m:
        raise HTTPException(404, "no such video")
    return d, m


@app.post("/api/jobs/{jid}/videos/{key}/render")
async def rerender_video(jid: str, key: str, req: Request):
    """Make a custom video again. Body (all optional): {offset: seconds of the song where its start plays, or nudge:
    seconds to move it by (+ = later), fill: hold|loop, lead: hold|black, realign: true (line it up by sound again)}.
    With none of them, it's re-made where it was (e.g. after a failure). key: "replace" or the version's slug."""
    check_origin(req)
    b = await req.json()
    with lock:
        job = load(jid)
        if job["status"] != "done":
            raise HTTPException(400, "finish making it first")
        if video_busy(job):
            raise HTTPException(409, VIDEO_BUSY)
        d, meta = _entry_meta(jid, key)
        off = None
        try:
            if b.get("offset") not in (None, ""):
                off = float(b["offset"])
            elif b.get("nudge") not in (None, "", 0):
                off = float(meta.get("start") or 0) - videoswap.trim_start(job_path(jid)) + float(b["nudge"])
        except (TypeError, ValueError):
            raise HTTPException(400, "offset and nudge are seconds")
        if off is not None and not -3600 < off < 3600:
            raise HTTPException(400, "that start is too far off")
        what = "the replacement video" if key == "replace" else f"the version “{meta.get('name')}”"
        claim_video(job, what)
        _edited(jid, job, agents.actor_of(req), what)
        save(jid, job)
    fill, lead, realign = b.get("fill"), b.get("lead"), bool(b.get("realign"))

    def fn(progress):
        m = videoswap.rerender(job_path(jid), key, offset=off, fill=fill, lead=lead, realign=realign, progress=progress)
        return (f"made {what} again, starting at {m['offset']:+.2f} s" + (" (lined up by sound)" if realign and m["aligned"] == "sound"
                else " (couldn't line it up by sound)" if realign else "")), key == "replace"
    run_video(jid, what, fn)
    return {"ok": True}


@app.delete("/api/jobs/{jid}/videos/{key}")
def remove_video(jid: str, key: str, req: Request):
    """Remove a version (its studio copy; a published copy stays in the library, like deleting a song). For the
    replacement, use .../videos/restore."""
    check_origin(req)
    if key == "replace":
        raise HTTPException(400, "use Restore official for the replacement video")
    with lock:
        job = load(jid)
        if video_busy(job):
            raise HTTPException(409, VIDEO_BUSY)
        _, meta = _entry_meta(jid, key)
        videoswap.remove_version(job_path(jid), key)
        job.setdefault("log", []).append(f"removed the version “{meta.get('name')}”")
        _edited(jid, job, agents.actor_of(req), f"removed the version “{meta.get('name')}”")
        save(jid, job)
        if meta.get("published_as"):  # its published copy stays: the catalog keeps it, from its last listing
            update_catalog()
    return {"ok": True, "published_as": meta.get("published_as")}


@app.post("/api/jobs/{jid}/videos/restore")
async def restore_official(jid: str, req: Request):
    """Put the official video back (the replacement and its source are removed)."""
    check_origin(req)
    with lock:
        job = load(jid)
        if video_busy(job):
            raise HTTPException(409, VIDEO_BUSY)
        try:
            videoswap.restore_official(job_path(jid))
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        job["video_v"] = time.time()
        if job.get("published"):
            job["unpublished_changes"] = True
        job.setdefault("log", []).append("the official video is back")
        _edited(jid, job, agents.actor_of(req), "official video restored")
        save(jid, job)
    return {"ok": True}


@app.get("/api/jobs/{jid}/videos/{key}/file")
def video_file(jid: str, key: str):
    """The official render ("official"), the song's video ("replace" = out.mp4), or a version's."""
    jd = job_path(jid)
    if key == "official":
        p = videoswap.official_file(jd)
    elif key == "replace":
        p = os.path.join(jd, "out.mp4")
    else:
        _entry_meta(jid, key)
        p = videoswap.entry_video(jd, key)
    if not os.path.exists(p):
        raise HTTPException(404, "not made yet")
    return FileResponse(p, media_type="video/mp4", headers={"Cache-Control": "private, max-age=604800"})  # URLs carry ?v=


@app.post("/api/jobs/{jid}/videos/{key}/publish")
async def publish_version(jid: str, key: str, req: Request):
    """Publish a version to the library as its own song, "Artist - Title (name)", with the song's lyrics."""
    check_origin(req)
    with lock:
        job = load(jid)
        if job["status"] != "done":
            raise HTTPException(400, "finish making it first")
        if video_busy(job):
            raise HTTPException(409, VIDEO_BUSY)
        if key == "replace":
            raise HTTPException(400, "the replacement is the song's video: publish the song")
        d, meta = _entry_meta(jid, key)
        if meta.get("stale"):
            raise HTTPException(400, "the song changed since this version was made: Re-render it first")
        base = version_base(job, meta)
        if base == job.get("published_as") or base == final_base(job):
            raise HTTPException(400, "give the version a name that's different from the song's")
        if os.path.exists(f"{MV_DIR}/{base}.mp4") and base != meta.get("published_as"):
            raise HTTPException(400, "a song with that name is already in the library")
        os.makedirs(MV_DIR, exist_ok=True)
        tmp = f"{MV_DIR}/.{base}.mp4.part"
        shutil.copyfile(videoswap.entry_video(job_path(jid), key), tmp)
        os.replace(tmp, f"{MV_DIR}/{base}.mp4")
        publish_lyrics(jid, base)
        guidevocals.write(job_path(jid), MV_DIR, [base], log)  # the song's own sound: its vocals (never stops a publish)
        meta.update(published=time.time(), published_as=base, unpublished_changes=False, lyrics_published=time.time(),
                    published_by=agents.stamp(agents.actor_of(req)))
        videoswap.save_meta(d, meta)
        log.info("published %s version %s as %s", jid, key, base)
        job.setdefault("log", []).append(f"published the version “{meta.get('name')}” as '{base}'")
        save(jid, job)
        update_catalog()
    pitch_later(jid)
    msg = "published"
    return {"name": base, "message": msg}


@app.get("/api/jobs/{jid}/peaks")
def peaks(jid: str):
    """Loudness of the vocal stem, 100 values per second (0-255), for the editor's waveform."""
    cache = job_path(jid, "peaks.json")
    if not os.path.exists(cache):
        src = job_path(jid, "vocals.m4a")
        if not os.path.exists(src):
            raise HTTPException(404, "no vocals")
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                           capture_output=True, timeout=120)
        import numpy as np
        a = np.abs(np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32))
        n = len(a) // 80
        pk = a[: n * 80].reshape(n, 80).max(axis=1) if n else np.zeros(0)
        top = float(np.percentile(pk, 99.5)) if n else 1.0
        vals = np.clip(np.sqrt(pk / max(top, 1.0)) * 255, 0, 255).astype(int).tolist()
        with open(cache + ".tmp", "w") as f:
            json.dump({"rate": 100, "peaks": vals}, f, separators=(",", ":"))
        os.replace(cache + ".tmp", cache)
    return FileResponse(cache, media_type="application/json")


def _vocals16k(jid):
    import numpy as np
    src = job_path(jid, "vocals.m4a")
    if not os.path.exists(src):
        raise HTTPException(404, "no vocals")
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-ar", "16000", "-f", "f32le", "-"], capture_output=True, timeout=120)
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


SPEC_PXS = 50  # spectrogram pixels per second (the editor draws its visible part)


def _make_spectro(jid, cache):
    """A mel spectrogram of the vocal stem as a PNG (low notes at the bottom): voices show as bright stripes, so the
    start of a syllable or the end of a held note is easier to see than on the loudness waveform (as in Subtitle Edit)."""
    import numpy as np, librosa
    from PIL import Image
    y = _vocals16k(jid)
    S = librosa.feature.melspectrogram(y=y, sr=16000, n_fft=1024, hop_length=16000 // SPEC_PXS, n_mels=96, fmin=80, fmax=8000)
    db = librosa.power_to_db(S, ref=np.max)
    lo, hi = np.percentile(db, 5), np.percentile(db, 99.5)
    v = np.clip((db - lo) / max(1e-6, hi - lo), 0, 1)[::-1]  # low frequencies at the bottom
    stops = np.array([[18, 13, 24], [60, 30, 100], [140, 70, 160], [255, 201, 77], [255, 245, 220]], dtype=np.float32)
    pos = np.linspace(0, 1, len(stops))
    idx = (v * 255).astype(np.uint8)
    lut = np.stack([np.interp(np.linspace(0, 1, 256), pos, stops[:, c]) for c in range(3)], axis=1).astype(np.uint8)
    img = Image.fromarray(lut[idx], "RGB")
    if img.width > 16383:  # WebP can't be wider (a song over about 5:27 at 50 px/s): PNG has no such limit
        png = os.path.join(os.path.dirname(cache), "spectro.png")
        img.save(png + ".tmp.png", "PNG", optimize=True)
        os.replace(png + ".tmp.png", png)
        return
    img.save(cache + ".tmp.webp", "WEBP", quality=80, method=4)
    os.replace(cache + ".tmp.webp", cache)


def _make_onsets(jid, cache):
    """Where vocal notes and syllables start (onset detection on the vocal stem), for the editor's ticks and snapping.
    Onsets in near-silence (separation leftovers) are dropped."""
    import numpy as np, librosa
    y = _vocals16k(jid)
    hop = 160
    on = librosa.onset.onset_detect(y=y, sr=16000, hop_length=hop, backtrack=True, units="frames")
    rms = librosa.feature.rms(y=y, frame_length=640, hop_length=hop)[0]
    floor = 0.12 * float(np.percentile(rms, 90))
    keep = [round(f * hop / 16000, 2) for f in on if float(rms[f:f + 10].max(initial=0)) > floor]
    with open(cache + ".tmp", "w") as f:
        json.dump({"onsets": keep}, f, separators=(",", ":"))
    os.replace(cache + ".tmp", cache)


@app.get("/api/jobs/{jid}/spectrogram")
async def spectrogram(jid: str):
    if not ID_RE.match(jid):
        raise HTTPException(404)
    cache, png = job_path(jid, "spectro.webp"), job_path(jid, "spectro.png")
    if not os.path.exists(cache) and not os.path.exists(png):
        await asyncio.to_thread(_make_spectro, jid, cache)
    if os.path.exists(png):  # long songs (see _make_spectro)
        cache = png
    return FileResponse(cache, media_type="image/png" if cache == png else "image/webp", headers={"Cache-Control": "private, max-age=604800", "X-Px-Per-Second": str(SPEC_PXS)})


@app.get("/api/jobs/{jid}/onsets")
async def onsets(jid: str):
    if not ID_RE.match(jid):
        raise HTTPException(404)
    cache = job_path(jid, "onsets.json")
    if not os.path.exists(cache):
        await asyncio.to_thread(_make_onsets, jid, cache)
    return FileResponse(cache, media_type="application/json")


@app.post("/api/jobs/{jid}/detect-singers")
async def detect_singers(jid: str, req: Request):
    """Run singers.py (a few seconds); returns a singer per line for the editor to show. Nothing is saved."""
    check_origin(req)
    job = load(jid)
    if job["status"] != "done":
        raise HTTPException(400, "finish making it first")
    r = await asyncio.to_thread(subprocess.run, [sys.executable, "-I", f"{HERE}/singers.py", job_path(jid)],
                                capture_output=True, text=True, timeout=300, env=C.tool_env())
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        raise HTTPException(500, "singer detection failed: " + (r.stderr or r.stdout).strip()[-200:])
    if "error" in res:
        raise HTTPException(400, res["error"])
    return res


ROMAJI_PROMPT = (
    "You proofread romaji for Japanese song lyrics, for karaoke singers who can't read Japanese.\n"
    "Each line has the written lyrics (japanese) and an automatic romaji from a dictionary, which sometimes picks the wrong "
    "reading of a kanji (e.g. 刃 read 'ha' when the song sings 'yaiba', 何も 'nan mo' instead of 'nanimo') or splits words "
    "oddly (傷つけあう as 'kizutsuke a u'). HEARD_RULES"
    "Return the romaji as it is SUNG: Hepburn, particles は=wa, を=wo, へ=e, long vowels written as in the input (ou, uu), "
    "words separated by spaces like the input, English words unchanged. Only change what is wrong; copy correct lines exactly.\n\n")
HEARD_RULES = (
    "Lines may also have 'heard': a rough automatic transcription of what the singer sings around that line (it can "
    "include a word of the neighbouring lines and mistakes). Japanese lyrics often write one word but sing another "
    "(furigana), e.g. written 幻 sung 夢 'yume', written 約束 sung 言葉 'kotoba'. Use 'heard' ONLY for that: a word whose "
    "meaning is related to the written one but whose sound is completely different. If the heard word merely SOUNDS "
    "similar to the written reading (soete vs osorete, kodomo vs kotomo), it is a transcription mistake: keep the written "
    "reading. Never take extra words from 'heard', never change verbs, particles or kana, never add or drop words.\n")
ROMAJI_SCHEMA = {"type": "object", "required": ["lines"], "properties": {"lines": {"type": "array", "items": {
    "type": "object", "required": ["n", "romaji"], "properties": {"n": {"type": "integer"}, "romaji": {"type": "string"}}}}}}


def ai_romaji_suggest(lines):
    """Ask the local LLM to fix romaji readings (using what was heard, when known). Returns (checked, suggestions)."""
    items = []
    for i, l in enumerate(lines):
        if l.get("orig") and re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", l["orig"]):
            it = {"n": i, "japanese": l["orig"], "romaji": " ".join(w[2] for w in l["words"])}
            if l.get("heard"):
                it["heard"] = l["heard"]
            items.append(it)
    if not items:
        return 0, []
    rules = HEARD_RULES if any("heard" in it for it in items) else ""
    body = {"model": C.OLLAMA_MODEL, "prompt": ROMAJI_PROMPT.replace("HEARD_RULES", rules) + json.dumps(items, ensure_ascii=False),
            "stream": False, "think": False, "format": ROMAJI_SCHEMA, "keep_alive": "2m",
            "options": {"temperature": 0, "num_ctx": 12288, "num_predict": 4000}}
    r = urllib.request.Request(f"{C.OLLAMA_URL}/api/generate", data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=600) as f:
        res = json.load(f)
    fixed = {int(x["n"]): str(x["romaji"]).strip() for x in json.loads(res["response"])["lines"]}
    out = []
    for it in items:
        new = re.sub(r"\s+", " ", fixed.get(it["n"], "")).strip()
        old_n, new_n = len(it["romaji"].split()), len(new.split())
        # guard: a "fix" that adds or drops more than a couple of words is the model pasting neighbours: skip it
        if new and new.lower() != it["romaji"].lower() and len(new) <= 300 and abs(new_n - old_n) <= 2:
            out.append({"i": it["n"], "japanese": it["japanese"], "old": it["romaji"], "new": new,
                        "words": remap_words(lines[it["n"]]["words"], new)})
    return len(items), out


def romaji_fixes(jid, lines):
    """Text check (LLM reads the Japanese + what was heard), then the listening check (readings.py: the aligner scores
    special readings against the vocals). Returns combined suggestions against the saved lines."""
    import copy
    work = copy.deepcopy(lines)
    how = {}
    checked, sugg = ai_romaji_suggest(work)
    for g in sugg:
        work[g["i"]]["words"] = g["words"]
        how[g["i"]] = "read"
    try:
        r = subprocess.run([sys.executable, "-I", f"{HERE}/readings.py", job_path(jid)], input=json.dumps(work),
                           capture_output=True, text=True, timeout=600, env=C.tool_env())
        heard = json.loads(r.stdout.strip().splitlines()[-1]).get("changes", [])
    except Exception:
        heard = []
    for c in heard:
        work[c["i"]]["words"] = remap_words(work[c["i"]]["words"], c["new"])
        how[c["i"]] = "heard" if c["i"] not in how else "read+heard"
    out = []
    for i in sorted(how):
        old, new = " ".join(w[2] for w in lines[i]["words"]), " ".join(w[2] for w in work[i]["words"])
        if old != new:
            out.append({"i": i, "japanese": lines[i].get("orig") or "", "old": old, "new": new, "how": how[i],
                        "words": remap_words(lines[i]["words"], new)})
    return checked, out


def lyrics_fixes(jid, lines):
    """Latin-script songs: lyricscheck.py (LLM proposes the sung line where Whisper heard something else; the aligner
    decides against the vocals). Returns suggestions against the saved lines."""
    try:
        r = subprocess.run([sys.executable, "-I", f"{HERE}/lyricscheck.py", job_path(jid)], input=json.dumps(lines),
                           capture_output=True, text=True, timeout=900, env=C.tool_env())
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return 0, []
    out = [{"i": c["i"], "japanese": "", "old": c["old"], "new": c["new"], "how": "heard",
            "words": remap_words(lines[c["i"]]["words"], c["new"])} for c in res.get("changes", [])]
    return res.get("checked", 0), out


def auto_romaji(jid):
    """After a song with automatic romaji is made: apply the AI's fixes (the plain version stays in words.orig.json)."""
    try:
        lines = load_lines(job_path(jid, "words.json"))
        latin = not any(l.get("orig") for l in lines)
        checked, sugg = (lyrics_fixes if latin else romaji_fixes)(jid, lines)
    except Exception as e:
        set_job(jid, log=load(jid).get("log", []) + [f"AI lyrics check skipped ({str(e)[:80]})"])
        return
    with lock:
        job = load(jid)
        if sugg:
            if not os.path.exists(job_path(jid, "words.orig.json")):
                shutil.copyfile(job_path(jid, "words.json"), job_path(jid, "words.orig.json"))
            for g in sugg:
                lines[g["i"]]["words"] = g["words"]
            save_lines(job_path(jid, "words.json"), lines)
            rebuild_lyrics(jid, job, lines)
        job["ai_romaji"] = [{k: g[k] for k in ("japanese", "old", "new", "how")} for g in sugg]
        what = "lyrics" if latin else "romaji"
        job.setdefault("log", []).append(f"AI {what} check: {len(sugg)} lines fixed ({checked} looked different from what's sung)"
                                         if sugg else f"AI {what} check: no changes needed")
        save(jid, job)


def remap_words(old, text):
    """New text for a line, keeping the timing of words that didn't change; changed stretches share their old span."""
    import difflib
    new = text.split()
    a, b = [w[2].lower().strip(".,!?") for w in old], [w.lower().strip(".,!?") for w in new]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            out += [[old[i1 + k][0], old[i1 + k][1], new[j1 + k]] for k in range(i2 - i1)]
        elif j2 > j1:
            if i2 > i1:
                s0, e0 = old[i1][0], old[i2 - 1][1]
            else:  # inserted words: squeeze them in after the previous word
                s0 = out[-1][1] if out else old[0][0]
                e0 = old[i1][0] if i1 < len(old) else s0 + 0.3 * (j2 - j1)
            e0 = max(e0, s0 + 0.15 * (j2 - j1))
            ws = new[j1:j2]
            tot, t = sum(max(2, len(w)) for w in ws), s0
            for w in ws:
                d = (e0 - s0) * max(2, len(w)) / tot
                out.append([round(t, 3), round(t + d * 0.92, 3), w]); t += d
    return out or [list(w) for w in old]


_JP_PUNCT = {"、": ", ", "。": ". ", "！": "! ", "？": "? ", "，": ", ", "：": ": ", "；": "; ", "「": " ", "」": " ", "『": " ",
             "』": " ", "（": " (", "）": ") ", "〜": "~", "～": "~", "・": " ", "…": "… ", "　": " "}


def romanize_paste(text):
    """Pasted lyrics may be in Japanese/Korean script, or mix it with English inside one 'word' ("swim、生き残るために"):
    use normal punctuation, split where scripts change, and romanize the native parts the way the studio does."""
    from romanize import romanize_korean
    for k, v in _JP_PUNCT.items():
        text = text.replace(k, v)
    kat = None
    out_lines = []
    for ln in text.splitlines():
        parts = re.split(r"([\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf々ー]+|[\uac00-\ud7a3]+)", ln)
        buf = []
        for part in parts:
            if not part:
                continue
            if re.fullmatch(r"[\uac00-\ud7a3]+", part):
                buf.append(" " + romanize_korean(part) + " ")
            elif re.fullmatch(r"[\u3040-\u30ff\u4e00-\u9fff\u3400-\u4dbf々ー]+", part):
                if kat is None:
                    import cutlet
                    kat = cutlet.Cutlet(); kat.use_foreign_spelling = False
                buf.append(" " + kat.romaji(part) + " ")
            else:
                buf.append(part)
        out_lines.append(re.sub(r"[ \t]+", " ", "".join(buf)).strip())
    return "\n".join(out_lines)


def paste_suggest(lines, text):
    """Line up pasted romaji (any site's layout: lines split or joined differently, section headers) with the song's
    lines by their letters, and suggest each line's text from it. Returns suggestions like the AI check."""
    import difflib
    letters = lambda w: re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", w.replace("đ", "d").replace("Đ", "D")).lower())
    # a romanized song (Japanese/Korean/Chinese lines shown in romaji): pasted native script is romanized first, so
    # kanji or "、" can never end up inside a romaji line
    native_paste = any(l.get("orig") for l in lines) and bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7a3]", text))
    if native_paste or (any(l.get("orig") for l in lines) and re.search(r"[、。]", text)):
        text = romanize_paste(text)
    # the song: one letter string, remembering which line each letter belongs to
    S, sl = [], []
    for i, l in enumerate(lines):
        for w in l["words"]:
            for ch in letters(w[2]):
                S.append(ch); sl.append(i)
    # the pasted text: tokens (headers like [Chorus] and lone symbols dropped), each with its letter range
    # section headers like [Chorus: Bang Chan, Han] say who sings the lines below them
    toks, tok_who, who, cast = [], [], None, []
    GROUP = {"all", "everyone", "both", "together", "group", "tutti"}
    for ln in text.splitlines():
        for part in re.split(r"(\[[^\]]*\])", ln):
            hdr = re.fullmatch(r"\[([^\]:]*)(?::\s*([^\]]*))?\]", part.strip())
            if hdr:
                names = [n.strip(" .") for n in re.split(r",|&|\+|/|\band\b|\bwith\b|\bfeat\.?", hdr.group(2) or "") if n.strip(" .")]
                if not names:
                    who = None
                    continue
                who = set()
                for n in names:
                    if n.lower() in GROUP:
                        who.add("*")
                        continue
                    key = next((c for c in cast if c.lower() == n.lower()), None)
                    if key is None and len(cast) < 8:
                        cast.append(n); key = n
                    if key:
                        who.add(key)
                continue
            for t in part.split():
                if letters(t):
                    toks.append(t); tok_who.append(frozenset(who) if who else None)
    P, pt = [], []
    for k, t in enumerate(toks):
        for ch in letters(t):
            P.append(ch); pt.append(k)
    if len(P) < 20 or len(S) < 20:
        return []
    owner = [None] * len(P)   # pasted letter -> song line
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, "".join(S), "".join(P), autojunk=False).get_opcodes():
        if tag in ("equal", "replace"):
            for j in range(j1, j2):
                i = i1 + (j - j1) * max(1, i2 - i1) // max(1, j2 - j1)
                owner[j] = sl[min(i, i2 - 1)] if i2 > i1 else None
    # each pasted token goes to the line that owns most of its letters
    from collections import Counter
    by_line = {}
    for k in range(len(toks)):
        votes = Counter(owner[j] for j in range(len(P)) if pt[j] == k and owner[j] is not None)
        if votes:
            by_line.setdefault(votes.most_common(1)[0][0], []).append(toks[k])
    # who sings each line: the members of the section most of its letters came from
    members = []
    for i in range(len(lines)):
        votes = Counter(tok_who[pt[j]] for j in range(len(P)) if owner[j] == i and tok_who[pt[j]] is not None)
        w = votes.most_common(1)[0][0] if votes else None
        members.append(None if not w else sorted(range(len(cast))) if "*" in w else sorted(cast.index(n) for n in w if n in cast))
    paste_suggest.last = {"cast": cast if any(members) else [], "members": members}
    out = []
    for i, l in enumerate(lines):
        new = " ".join(by_line.get(i, []))
        old = " ".join(w[2] for w in l["words"])
        if not new or letters(new) == letters(old):
            continue
        if l.get("orig") and re.search(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7a3]", new):
            continue  # never put native script into a romaji line
        if l.get("orig") and native_paste:
            continue  # a page in Japanese/Korean script says nothing new about readings: only English lines change
        # skip pairs that share almost nothing (alignment slipped)
        if difflib.SequenceMatcher(None, letters(new), letters(old)).ratio() < 0.5:
            continue
        if old[:1].isupper() and new[:1].islower():
            new = new[0].upper() + new[1:]
        out.append({"i": i, "japanese": l.get("orig") or "", "old": old, "new": new, "words": remap_words(l["words"], new)})
    return out


@app.post("/api/jobs/{jid}/romaji-paste")
async def romaji_paste(jid: str, req: Request):
    """Use romaji pasted from a lyrics site; returns suggestions (nothing saved)."""
    check_origin(req)
    b = await req.json()
    text = str(b.get("text", ""))[:30000]
    job = load(jid)
    if job["status"] != "done":
        raise HTTPException(400, "finish making it first")
    paste_suggest.last = {"cast": [], "members": []}
    sugg = paste_suggest(load_lines(job_path(jid, "words.json")), text)
    extra = paste_suggest.last
    return {"model": "pasted romaji", "checked": len(load_lines(job_path(jid, "words.json"))), "suggestions": sugg,
            "cast": extra["cast"], "members": extra["members"] if extra["cast"] else []}


@app.post("/api/jobs/{jid}/romaji-check")
async def romaji_check(jid: str, req: Request):
    """Ask the local LLM to fix wrong readings in automatic romaji. Returns suggestions; nothing is saved."""
    check_origin(req)
    if not C.OLLAMA_URL:
        raise HTTPException(400, "no Ollama server configured (OLLAMA_URL)")
    job = load(jid)
    if job["status"] != "done":
        raise HTTPException(400, "finish making it first")
    lines = load_lines(job_path(jid, "words.json"))
    try:
        fixer = romaji_fixes if any(l.get("orig") for l in lines) else lyrics_fixes
        checked, out = await asyncio.to_thread(fixer, jid, lines)
    except Exception as e:
        raise HTTPException(502, f"the AI check failed ({str(e)[:100]})")
    if not checked and any(l.get("orig") for l in lines):
        raise HTTPException(400, "this song has no Japanese lines to check")
    return {"model": C.OLLAMA_MODEL, "checked": checked, "suggestions": out}


@app.post("/api/jobs/{jid}/lines/reset")
async def reset_lines(jid: str, req: Request):
    check_origin(req)
    with lock:
        job = load(jid)
        orig = job_path(jid, "words.orig.json")
        if not os.path.exists(orig):
            raise HTTPException(400, "nothing to reset")
        shutil.copyfile(orig, job_path(jid, "words.json"))
        rebuild_lyrics(jid, job)
        job["edited"] = None
        agents.retire(job_path(jid))
        _edited(jid, job, agents.actor_of(req), "all edits undone (back to the automatic version)")
        job["ai_romaji"] = None  # back to the plain automatic romaji
        if job.get("published"):
            job["unpublished_changes"] = True
        save(jid, job)
    return {"ok": True}


@app.get("/api/jobs/{jid}/log")
def job_log_text(jid: str):
    """Everything the pipeline printed while making this song (every run), for tracing a problem."""
    p = job_path(jid, "pipeline.log")
    if not os.path.exists(p):
        return PlainTextResponse("No log for this song: it was made before logs were kept. Remake it to get one.\n")
    with open(p, "rb") as f:
        f.seek(max(0, os.path.getsize(p) - 1_500_000))
        return PlainTextResponse(f.read().decode("utf-8", "replace"))


@app.post("/api/jobs/{jid}/reviewed")
async def set_reviewed(jid: str, req: Request):
    """The user played the song through and it's fine (or takes that back). Shown on the song list in place of the
    studio's own guess, which matters most for songs it can't follow by ear. A remake clears it."""
    check_origin(req)
    b = await req.json()
    with lock:
        job = load(jid)
        if job.get("status") != "done":
            raise HTTPException(400, "finish making it first")
        job["reviewed"] = time.time() if b.get("on", True) else None
        who = agents.actor_of(req)
        if job["reviewed"] and job.get("quick") and not job["quick"].get("reviewed") and who.get("kind") != "agent":
            job["quick"] = dict(job["quick"], reviewed=job["reviewed"], reviewed_by=who.get("name"))  # a person looked
        _edited(jid, job, who, "checked by you: " + ("on" if job["reviewed"] else "off"))
        save(jid, job)
    return {"reviewed": job["reviewed"]}


@app.post("/api/jobs/{jid}/rename")
async def rename(jid: str, req: Request):
    """Change a song's artist/title. If it's published, its files in the library are renamed too."""
    check_origin(req)
    b = await req.json()
    artist, title = str(b.get("artist", "")).strip()[:120], str(b.get("title", "")).strip()[:160]
    if not artist or not title:
        raise HTTPException(400, "give both artist and title")
    actor = agents.actor_of(req)
    with lock:
        job = load(jid)
        # the original title (its own script: 紅蓮華 for Gurenge): players show it next to the title; no file changes
        if "title_orig" in b:
            orig = str(b.get("title_orig") or "").strip()[:160] or None
            if orig != job.get("title_orig"):
                _edited(jid, job, actor, f"original title: {orig or '(none)'}")
                job["title_orig"] = orig
                if artist == job.get("artist") and title == job.get("title"):   # only that: saved, the catalog says it
                    save(jid, job)
                    if job.get("published"):
                        update_catalog()
                    return {"artist": artist, "title": title, "title_orig": orig, "name": final_base(job), "message": "original title"}
        old = job.get("published_as")
        if old and os.path.exists(f"{MV_DIR}/{old}.mp4"):  # it renames files in the library too
            _agent_refuse(actor, "publish", "renaming a published song (its files in the library change too)")
        _edited(jid, job, actor, f"renamed to {artist} - {title}")
        job.update(artist=artist, title=title)
        new = final_base(job)
        moved = False
        if old and old != new and os.path.exists(f"{MV_DIR}/{old}.mp4"):
            if os.path.exists(f"{MV_DIR}/{new}.mp4"):
                raise HTTPException(400, "a song with that name is already in the library")
            for ext in LIB_EXTS:
                if os.path.exists(f"{MV_DIR}/{old}{ext}"):
                    os.replace(f"{MV_DIR}/{old}{ext}", f"{MV_DIR}/{new}{ext}")
            job["published_as"] = new
            moved = True
        for m in videoswap.entries(job_path(jid)):  # published versions: "Artist - Title (name)" follows
            pa = m.get("published_as")
            if m["mode"] != "version" or not pa or not os.path.exists(f"{MV_DIR}/{pa}.mp4"):
                continue
            nv = version_base(job, m)
            if nv != pa and not os.path.exists(f"{MV_DIR}/{nv}.mp4"):
                for ext in LIB_EXTS:
                    if os.path.exists(f"{MV_DIR}/{pa}{ext}"):
                        os.replace(f"{MV_DIR}/{pa}{ext}", f"{MV_DIR}/{nv}{ext}")
                m["published_as"] = nv
                videoswap.save_meta(videoswap.version_dir(job_path(jid), m["slug"]), m)
                moved = True
        # the title card in the intro shows the new name
        if job.get("status") == "done" and os.path.exists(job_path(jid, "words.json")) and rebuild_lyrics(jid, job) \
                and job.get("published"):
            job["unpublished_changes"] = True
        save(jid, job)
        if moved:
            update_catalog()
    if moved:
        pitch_later(jid)  # the pitch files say their song's name: written again (from the cache, quick)
    msg = "renamed in the library" if moved else "renamed"
    return {"artist": artist, "title": title, "title_orig": job.get("title_orig"), "name": new, "message": msg}


@app.post("/api/jobs/{jid}/remake")
async def remake(jid: str, req: Request):
    """Make a finished song again with the current version, in place (same page; publishing replaces the library's copy).
    The current lyrics (with any edits) are kept as words.before-remake.json."""
    check_origin(req)
    try:
        gpu = (await req.json()).get("gpu")
    except Exception:
        gpu = None
    _remake(jid, agents.actor_of(req), gpu)
    return {"ok": True}


def _remake(jid, actor, gpu=None, note=None):
    """Make a finished song again in place (the remake endpoint, and the video check's one automatic retry)."""
    with lock:
        job = load(jid)
        if job["status"] != "done":
            raise HTTPException(400, "only finished songs can be remade")
        if video_busy(job):
            raise HTTPException(409, VIDEO_BUSY)
        if gpu in ("auto", "local", "remote"):  # the card chosen on the song page (otherwise as the song was first made)
            job["gpu_pref"] = gpu
        if os.path.exists(job_path(jid, "words.json")):
            shutil.copyfile(job_path(jid, "words.json"), job_path(jid, "words.before-remake.json"))
        # the old automatic version (before the user's edits) tells which lines they moved by hand
        merge = bool(job.get("edited")) and os.path.exists(job_path(jid, "words.orig.json"))
        if os.path.exists(job_path(jid, "words.before-remake.auto.json")):
            os.remove(job_path(jid, "words.before-remake.auto.json"))
        for f in ("peaks.json", "spectro.webp", "spectro.png", "onsets.json"):  # made from the old audio
            if os.path.exists(job_path(jid, f)):
                os.remove(job_path(jid, f))
        if os.path.exists(job_path(jid, "words.orig.json")):
            os.replace(job_path(jid, "words.orig.json"), job_path(jid, "words.before-remake.auto.json"))
        job.update(status="queued", step="queued", error=None, log=[], waiting=None, oom_retries=0, ai_romaji=None,
                   video_v=time.time(), remade=time.time(), reviewed=None, remake_merge=merge, remake_note=None, remake_undone=False,
                   pre_remake={**{k: job.get(k) for k in ("edited", "result", "offset", "reviewed", "ai_romaji", "remade", "remake_note")},
                               "log": job.get("log", [])[-30:], "unstarted": True})
        agents.retire(job_path(jid))  # (the remake retimes the lines; Undo remake is the way back)
        _edited(jid, job, actor, "remake")
        if note:
            job["log"].append(note)
        save(jid, job)
    log.info("remake %s | gpu %s | keeps edits: %s", jid, job.get("gpu_pref", "auto"), merge)
    wake.set()


@app.post("/api/jobs/{jid}/undo-remake")
async def undo_remake(jid: str, req: Request):
    """Put back the lyrics the song had before its last remake (the new video and sound stay)."""
    check_origin(req)
    with lock:
        job = load(jid)
        if job["status"] != "done" or not os.path.exists(job_path(jid, "words.before-remake.json")):
            raise HTTPException(400, "there's no earlier version to go back to")
        shutil.copyfile(job_path(jid, "words.before-remake.json"), job_path(jid, "words.json"))
        if os.path.exists(job_path(jid, "words.before-remake.auto.json")):
            shutil.copyfile(job_path(jid, "words.before-remake.auto.json"), job_path(jid, "words.orig.json"))
        rebuild_lyrics(jid, job)
        job.update(remake_undone=True, remake_note=None, edited=(job.get("pre_remake") or {}).get("edited") or time.time())
        if job.get("published"):
            job["unpublished_changes"] = True
        job.setdefault("log", []).append("remake undone: the lyrics are back to the version before it")
        agents.retire(job_path(jid))
        _edited(jid, job, agents.actor_of(req), "undo remake")
        save(jid, job)
    return {"ok": True}


@app.post("/api/jobs/{jid}/chinese-reading")
async def chinese_reading(jid: str, req: Request):
    """Switch a Chinese song between pinyin (Mandarin) and jyutping (Cantonese) without remaking it: each line is
    romanized again from its Chinese line, keeping the timing (one syllable per character either way)."""
    check_origin(req)
    to = (await req.json()).get("to")
    if to not in ("zh", "yue"):
        raise HTTPException(400, "to: zh or yue")
    from romanize import romanize_cantonese, romanize_mandarin
    conv = romanize_cantonese if to == "yue" else romanize_mandarin
    with lock:
        job = load(jid)
        if job["status"] != "done" or (job.get("result") or {}).get("script", "zh") != "zh":
            raise HTTPException(400, "only for finished Chinese songs")
        path, orig_p = job_path(jid, "words.json"), job_path(jid, "words.orig.json")
        lines, cast = load_lines(path), load_cast(path)

        def convert(ls):
            n_ = 0
            for l in ls:
                if not l.get("orig"):
                    continue
                toks = []
                for t in re.sub(r"\s+", " ", conv(l["orig"])).split():  # punctuation rides with the word before it, as when made
                    if not re.search(r"[A-Za-z]", t) and toks:
                        toks[-1] += " " + t
                    else:
                        toks.append(t)
                l["words"] = [[w[0], w[1], t] for w, t in zip(l["words"], toks)] if len(toks) == len(l["words"]) \
                    else remap_words(l["words"], " ".join(toks))
                n_ += 1
            return n_
        n = convert(lines)
        # the automatic version ("Undo all edits", and what a remake compares the user's edits with) switches too, so
        # its lines still pair with these
        if os.path.exists(orig_p):
            o_lines, o_cast = load_lines(orig_p), load_cast(orig_p)
            convert(o_lines)
            save_lines(orig_p, o_lines, o_cast)
        save_lines(path, lines, cast)
        if not os.path.exists(orig_p):
            shutil.copyfile(path, orig_p)
        job["result"] = dict(job.get("result") or {}, lang=to)
        job["lang"] = to  # a remake keeps the choice
        rebuild_lyrics(jid, job, lines, cast)  # (Cantonese is drawn with the traditional-character font)
        job["edited"] = time.time()
        agents.retire(job_path(jid))  # (older lines are in the other reading)
        _edited(jid, job, agents.actor_of(req), "reading: " + ("jyutping (Cantonese)" if to == "yue" else "pinyin (Mandarin)"))
        if job.get("published"):
            job["unpublished_changes"] = True
        save(jid, job)
    return {"lines": n, "lang": to}


@app.post("/api/jobs/{jid}/retry")
async def retry(jid: str, req: Request):
    check_origin(req)
    with lock:
        job = load(jid)
        if job["status"] not in ("failed", "cancelled"):
            raise HTTPException(400, "only failed or cancelled songs can be tried again")
        job.pop("cancel_requested", None)
        job.update(status="queued", step="queued", error=None, waiting=None, log=[])
        save(jid, job)
    log.info("retry %s", jid)
    wake.set()
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str, req: Request):
    check_origin(req)
    with lock:
        job = load(jid)
        if job["status"] == "running":
            raise HTTPException(400, "it's stopping; try again in a few seconds" if job.get("cancel_requested")
                                else "it's being made; cancel it first (or wait for it to finish)")
        if video_busy(job):
            raise HTTPException(400, VIDEO_BUSY)
        shutil.rmtree(job_path(jid))  # only the studio's copy; a published song stays in the library
        if job.get("published"):  # and stays in the catalog, from its last listing (marked job_deleted)
            update_catalog()
    log.info("deleted %s (%s - %s)", jid, job.get("artist"), job.get("title"))
    return {"ok": True}


def _mark_cancelled(job, note="cancelled by you"):
    """(under the lock) The song ends as cancelled: Retry makes it again from the start, Delete removes it."""
    job.pop("cancel_requested", None)
    job.update(status="cancelled", step="cancelled", waiting=None, error=None, finished=time.time())
    job.setdefault("log", []).append(note)


def _cancel_queued(jid, job):
    """(under the lock) A song that hasn't started is cancelled; a remake that hasn't started yet goes back to the
    song as it was (nothing of it was touched but its automatic-timing copy, which is put back)."""
    pr = job.get("pre_remake") or {}
    if job.get("remade") and pr.get("unstarted") and os.path.exists(job_path(jid, "out.mp4")) \
            and os.path.exists(job_path(jid, "words.json")):
        if os.path.exists(job_path(jid, "words.before-remake.auto.json")):
            os.replace(job_path(jid, "words.before-remake.auto.json"), job_path(jid, "words.orig.json"))
        job.pop("cancel_requested", None)
        job.update(status="done", step="done", waiting=None, error=None, oom_retries=0, remake_merge=False,
                   remake_undone=True,  # the remake overwrote what "Undo remake" would have gone back to
                   **{k: pr.get(k) for k in ("edited", "result", "offset", "reviewed", "ai_romaji", "remade", "remake_note")})
        job["log"] = list(pr.get("log") or []) + ["remake cancelled: the song is as it was"]
        return "restored"
    _mark_cancelled(job)
    return "cancelled"


def _helper_stop(jid, cleanup=False):
    """Ask the GPU helper to stop this job (it kills the pipeline's process group). cleanup: also wait for it to end
    and remove the helper's copy (a job the studio no longer follows, e.g. after a restart)."""
    try:
        helper_call(f"/jobs/{jid}/cancel", "POST", {}, timeout=10)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # the helper doesn't have it (or runs code from before cancel existed)
            return False
        return False
    except Exception:
        return False
    if cleanup:
        for _ in range(20):
            time.sleep(2)
            try:
                if helper_call(f"/jobs/{jid}", timeout=5).get("status") != "running":
                    helper_call(f"/jobs/{jid}", "DELETE", timeout=10)
                    break
            except Exception:
                break
    return True


@app.post("/api/jobs/{jid}/cancel")
async def cancel_job(jid: str, req: Request):
    """Stop making a song. Waiting: cancelled at once. Being made: the pipeline (here or on the helper PC) is stopped
    and the song turns "cancelled" a few seconds later (Retry makes it again, Delete removes it)."""
    check_origin(req)
    remote = orphan = False
    with lock:
        job = load(jid)
        st = job.get("status")
        if st == "cancelled":
            return {"ok": True, "status": "cancelled"}
        if st == "running" and job.get("cancel_requested"):
            return {"ok": True, "status": "stopping"}
        if st == "queued":
            what = _cancel_queued(jid, job)
            save(jid, job)
            log.info("cancel %s (it was waiting): %s", jid, what)
            orphan = bool(C.GPU_HELPER_URL)  # after a studio restart the helper may still be making it
            if orphan:
                threading.Thread(target=_helper_stop, args=(jid, True), daemon=True).start()
            return {"ok": True, "status": job["status"]}
        if st == "done" and video_busy(job):
            raise HTTPException(409, "the song is made; what's running is one of its videos, which can't be stopped "
                                     "(it takes a few minutes)")
        if st != "running":
            raise HTTPException(400, "it isn't being made" + (" (it's finished already)" if st == "done" else ""))
        r = runners.get(jid)
        if r is None:  # "running" but nothing is making it (a studio restart or a crashed runner): just mark it
            _mark_cancelled(job)
            save(jid, job)
            log.info("cancel %s (nothing was making it)", jid)
            return {"ok": True, "status": "cancelled"}
        job["cancel_requested"] = time.time()
        job.setdefault("log", []).append("stopping…")
        save(jid, job)
        p = r.get("proc")
        if p is not None and r.get("stopper") is None:
            r["stopper"] = threading.Thread(target=C.stop_group, args=(p,), daemon=True)
            r["stopper"].start()
        remote = r["where"] == "remote"
    log.info("cancel %s (it was being made on %s)", jid, "the helper" if remote else "this server")
    if remote:  # run_remote keeps asking until the helper has stopped it, then ends the song as cancelled
        threading.Thread(target=_helper_stop, args=(jid,), daemon=True).start()
    return {"ok": True, "status": "stopping"}


# ---------------- worker ----------------
# Two slots: this machine's GPU, and optionally a GPU helper on another PC (helper.py, GPU_HELPER_URL). The oldest
# queued job goes to the local GPU if it has room, else to the helper if it's online with room, else it waits.
wake = threading.Event()
slots = {"local": None, "remote": None}
# songs being made: job id -> {"where": local|remote, "proc": the pipeline's Popen (local), "stopper": thread stopping it}.
# Added by claim() and removed by the runner thread when it ends, both under the lock.
runners = {}
helper_seen = {"at": 0.0, "health": None}


def local_gpu_name():
    return (gpumod.name() or "this server's GPU").replace("NVIDIA GeForce ", "")


LOCAL_GPU = local_gpu_name()
_CV = {}


def code_version():
    """Fingerprint of the song-making code as it is on disk now (it can change under a running studio after a
    merge): the helper must run the same, or the two cards make songs with different code."""
    try:
        key = tuple(os.path.getmtime(os.path.join(HERE, f)) for f in C.CODE_FILES)
    except OSError:
        key = None
    if key is None or _CV.get("key") != key:
        _CV.update(key=key, v=C.code_version())
    return _CV["v"]


def helper_call(path, method="GET", body=None, timeout=10, raw=False):
    req = urllib.request.Request(C.GPU_HELPER_URL + path, method=method, headers={
        "X-Helper-Token": C.HELPER_TOKEN, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    r = urllib.request.urlopen(req, timeout=timeout)
    return r if raw else json.load(r)


def helper_health():
    if not C.GPU_HELPER_URL:
        return None
    try:
        h = helper_call("/health", timeout=3)
    except Exception:
        h = None
    helper_seen.update(at=time.time(), health=h)
    return h


def ask_helper_update():
    """Ask an out-of-date helper to update itself from GitHub (at most every 10 minutes)."""
    if time.time() - helper_seen.get("asked", 0) < 600:
        return
    helper_seen["asked"] = time.time()

    def go():
        try:
            r = helper_call("/update", "POST", {"want": code_version()}, timeout=1900)
            print(f"helper update: {r}", flush=True)
        except Exception as e:
            print(f"helper update failed: {e!r}", flush=True)
    threading.Thread(target=go, daemon=True).start()


def set_job(jid, **kw):
    with lock:
        job = load(jid)
        job.update(kw)
        save(jid, job)
        return job


def claim(jid, where, gpu_name):
    with lock:
        job = load(jid)
        if job["status"] != "queued":
            return False
        job.pop("cancel_requested", None)
        if job.get("pre_remake"):
            job["pre_remake"].pop("unstarted", None)  # a remake that started can't be cancelled back to the old song
        job.update(status="running", step="lyrics", waiting=None, started=time.time(), gpu=gpu_name, log=[])
        save(jid, job)
        slots[where] = jid
        runners[jid] = {"where": where, "proc": None, "stopper": None}
    log.info("started %s on %s (%s)", jid, gpu_name, where)
    C.job_log(job_path(jid), f"run started on {gpu_name} ({where}), code {C.code_version()}", header=True)
    return True


def worker():
    while True:
        try:
            schedule_once()
        except Exception as e:
            print(f"scheduler error: {e!r}", flush=True)
            log.exception("scheduler error")
        wake.wait(10); wake.clear()


def schedule_once():
    queued = sorted((j for j in list_jobs() if j["status"] == "queued"), key=lambda j: j["created"])
    if queued:
        WORDS.release_gpu()  # (words scoring never holds the card while a song waits for one)
    for j in queued:
        jid = j["id"]
        pref = load(jid).get("gpu_pref", "auto")  # "auto": whichever card is free; "local"/"remote": only that one
        use_local = pref in ("auto", "local") and C.USE_LOCAL_GPU
        use_remote = pref in ("auto", "remote") and bool(C.GPU_HELPER_URL)
        free = gpu_free_mib()
        if use_local and slots["local"] is None and free >= NEED_GPU_MIB:
            if claim(jid, "local", LOCAL_GPU):
                threading.Thread(target=run_local, args=(jid,), daemon=True).start()
            continue
        if use_remote and slots["remote"] is None:
            h = helper_health()
            if h and h.get("version") != code_version():
                helper_seen["stale"] = True
                ask_helper_update()
                h = None  # the helper runs different code: don't use it until it's updated
            elif h:
                helper_seen["stale"] = False
            if h and not h.get("busy") and h.get("free_mib", 0) >= NEED_GPU_MIB:
                if claim(jid, "remote", h.get("gpu", "helper").replace("NVIDIA GeForce ", "") + " (" + C.GPU_HELPER_NAME + ")"):
                    threading.Thread(target=run_remote, args=(jid,), daemon=True).start()
                continue
        parts = []
        if use_local:
            parts.append(f"{LOCAL_GPU}: " + (f"{free // 1024} GB free, needs {NEED_GPU_MIB // 1024}" if slots["local"] is None
                                              else "making another song"))
        if use_remote:
            parts.append(f"{C.GPU_HELPER_NAME}: " + ("making another song" if slots["remote"] is not None else
                         "updating to the new studio code (if this stays, update it: on that PC run deploy/install-helper.sh again)"
                         if helper_seen.get("stale") else "busy" if helper_seen["health"] else "off or unreachable"))
        why = ("waiting for " + ("the card you picked" if pref != "auto" else "a graphics card") + " (" + "; ".join(parts) + ")"
               if parts else "waiting: the card you picked isn't set up in this studio")
        if j.get("waiting") != why:
            set_job(jid, waiting=why)
        if pref == "auto":
            break  # first come, first served: later songs can't use a card this one couldn't
        # a song waiting for one particular card doesn't hold up songs that can use the other one


def cancel_requested(jid):
    try:
        return bool(load(jid).get("cancel_requested"))
    except Exception:
        return False


def finish(jid, rc, err, oom, where):
    _finish(jid, rc, err, oom, where)
    try:
        job = load(jid)
    except Exception:
        return
    if job.get("status") == "done":
        threading.Thread(target=after_make, args=(jid,), daemon=True, name="after-make").start()
    elif job.get("automake"):
        refresh_requests()


# songs sung in a language with its own script: the original title goes next to the Latin one (origtitle.py)
OWN_SCRIPT_LANGS = {"ja", "ko", "zh", "yue", "ru", "uk", "be", "bg", "sr", "mk", "kk", "mn", "el", "he", "ar", "fa", "ur",
                    "ps", "hi", "mr", "ne", "bn", "pa", "gu", "ta", "te", "kn", "ml", "si", "th", "lo", "km", "my", "ka", "hy", "am"}


def fill_title_orig(jid):
    """A made song without an original title, sung in a language with its own script: find it (kara.moe, LRCLIB, the
    video's title; only one that reads back as the song's title) and set it. Never overwrites one a person set."""
    try:
        job = load(jid)
        r = job.get("result") or {}
        if job.get("title_orig") or not (r.get("lang") in OWN_SCRIPT_LANGS or r.get("script") not in (None, "latin")):
            return None
        import origtitle
        got = origtitle.find(job, http_json, lang=r.get("lang"))
        if not got:
            return None
        with lock:
            j = load(jid)
            if j.get("title_orig"):
                return None
            j["title_orig"] = got["orig"]
            j.setdefault("log", []).append(f"original title: {got['orig']} (from {got['source']}, reads as \"{got['read_as']}\")")
            save(jid, j)
        if j.get("published"):
            update_catalog()
        log.info("original title for %s: %s (%s)", jid, got["orig"], got["source"])
        return got
    except Exception:
        log.exception("original title for %s", jid)
        return None


def after_make(jid):
    """A song was made: its original title, then party auto-make or a batch (ks make --auto) finishes it."""
    fill_title_orig(jid)
    job = load(jid)
    if job.get("automake"):  # party auto-make: check it and publish it (or say why not); the request follows
        auto_finish(jid)
    elif job.get("auto_publish"):   # ks make --auto (a batch)
        batch_finish(jid)


def _finish(jid, rc, err, oom, where):
    with lock:
        job = load(jid)
        if job.get("cancel_requested"):  # stopped by the user: not failed, not retried
            _mark_cancelled(job)
            save(jid, job)
            took = f"{job['finished'] - job['started']:.0f}s" if job.get("started") else "?"
            log.info("cancelled %s on %s after %s", jid, where, took)
            C.job_log(job_path(jid), "run ended: cancelled by you", header=True)
            return
        job["finished"] = time.time()
        if rc == 0 and os.path.exists(job_path(jid, "out.mp4")) and os.path.exists(job_path(jid, "result.json")):
            job.update(status="done", step="done")
            job["result"] = json.load(open(job_path(jid, "result.json")))
            job["result"]["gpu"] = job.get("gpu")
            if job.get("published"):  # a remade song: the library still has the old one until it's published again
                job["unpublished_changes"] = True
            old = job.pop("replaces", None)  # made to replace another song: that one goes now
            adopted = 0
            if old and ID_RE.match(old) and old != jid and os.path.exists(os.path.join(JOBS, old, "job.json")):  # it may be gone already
                try:
                    if load(old).get("status") not in ("running", "queued"):
                        try:  # its custom videos come along (lined up again by sound: it's a different official video)
                            adopted = videoswap.adopt(job_path(old), job_path(jid))
                        except Exception:
                            log.exception("couldn't carry the custom videos of %s over", old)
                        shutil.rmtree(job_path(old))
                        log.info("replaced %s with %s", old, jid)
                except Exception:
                    log.exception("couldn't remove the replaced song %s", old)
            # A remake (or a song that took over another's custom videos) has a new out.mp4 made from the official
            # video: it becomes the official copy, and every custom video is made again over it (the audio is new too).
            # Done here, not in pipeline.py, because a remake on the helper PC never sees the custom videos.
            try:
                if videoswap.entries(job_path(jid)):
                    keys = videoswap.official_changed(job_path(jid), realign=bool(adopted))
                    what = "custom videos, taken over from the song this one replaces" if adopted else "custom videos, over the remade song"
                    claim_video(job, what)
                    rerender_later(jid, keys, what)
            except Exception as e:
                log.exception("custom videos of %s", jid)
                job.setdefault("log", []).append(f"couldn't re-make the custom videos ({str(e)[:100]}); use Re-render on each")
            if job.get("remade") and job.get("remake_merge"):
                try:
                    r = merge_remake(jid)
                except Exception as e:
                    r = None
                    job.setdefault("log", []).append(f"couldn't carry your edits over ({str(e)[:80]}); Undo remake restores them")
                if r is not None:
                    ch, ad = r
                    job["remake_note"] = ("the remake found nothing to change: your words and timing are kept as they were" if not ch and not ad else
                                          f"{ch} line{'s' if ch != 1 else ''} got new timing (keeping your singers and word fixes)"
                                          + (f", {ad} line{'s' if ad != 1 else ''} added" if ad else "")
                                          + "; lines you had moved by hand kept your timing")
                    job["edited"] = time.time()
                    job.setdefault("log", []).append("remake: " + job["remake_note"])
            # the lyrics as the studio draws them, with the song's timing offset, display mode and title, and one file per
            # display mode (a song made on the helper PC comes back with only its lyrics.ass)
            try:
                rebuild_lyrics(jid, job)
            except Exception as e:
                log.exception("lyrics of %s", jid)
                job.setdefault("log", []).append(f"couldn't redraw the lyrics ({str(e)[:100]})")
        elif oom and job.get("oom_retries", 0) < 2:
            # something else grabbed the GPU mid-job (an LLM loading, another tool): wait and try again
            job.update(status="queued", step="queued", oom_retries=job.get("oom_retries", 0) + 1,
                       waiting="the graphics card ran out of memory; trying again when one is free")
            job.setdefault("log", []).append(f"ran out of GPU memory on {job.get('gpu')}; will retry")
        else:
            job.update(status="failed", error=err or ("the graphics card ran out of memory" if oom else f"pipeline exited with code {rc}"))
        save(jid, job)
        took = f"{job['finished'] - job['started']:.0f}s" if job.get("started") else "?"
        if job["status"] == "done":
            c = (job.get("result") or {}).get("confidence") or {}
            log.info("done %s on %s in %s | %s lines, %s to check, %s heard in place", jid, where, took, c.get("lines"), c.get("check"), c.get("sure"))
        elif job["status"] == "failed":
            log.error("failed %s on %s after %s | %s", jid, where, took, job.get("error"))
        else:
            log.warning("requeued %s after running out of GPU memory on %s", jid, where)
        C.job_log(job_path(jid), f"run ended: {job['status']}" + (f" ({job.get('error')})" if job["status"] == "failed" else ""), header=True)


def _ltext(l):
    return re.sub(r"[^\w]+", " ", " ".join(w[2] for w in l["words"]).lower()).strip()


def _pair(a, b):
    """Pair two lists of lines in order by text (a DP over text similarity >= 0.5); returns {index in a: index in b}."""
    import difflib
    n, m = len(a), len(b)
    sim = [[0.0] * m for _ in range(n)]
    for i in range(n):
        ta = _ltext(a[i])
        for j in range(m):
            if abs(i * m / max(1, n) - j) <= 40:  # far-apart lines can't be the same line
                r = difflib.SequenceMatcher(None, ta, _ltext(b[j])).ratio()
                sim[i][j] = r if r >= 0.5 else 0.0
    best = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best[i][j] = max(best[i + 1][j], best[i][j + 1], (sim[i][j] + best[i + 1][j + 1]) if sim[i][j] else 0.0)
    out, i, j = {}, 0, 0
    while i < n and j < m:
        if sim[i][j] and abs(best[i][j] - (sim[i][j] + best[i + 1][j + 1])) < 1e-9:
            out[i] = j; i += 1; j += 1
        elif best[i][j] == best[i + 1][j]:
            i += 1
        else:
            j += 1
    return out


def merge_remake(jid):
    """After remaking a song the user had edited, keep their work where it still holds:
       - a line they moved by hand keeps their timing (they checked it by ear);
       - a line whose new automatic timing is the same as the old one stays exactly as they left it;
       - otherwise the line takes the new timing, with their singer, member colours, shared-line painting and word fixes.
    Returns (lines changed, lines added); (0, 0) means the remake found nothing to change and their version is kept."""
    ed_p, auto_p, new_p = job_path(jid, "words.before-remake.json"), job_path(jid, "words.before-remake.auto.json"), job_path(jid, "words.json")
    if not (os.path.exists(ed_p) and os.path.exists(auto_p) and os.path.exists(new_p)):
        return None
    edited, old_auto, new = load_lines(ed_p), load_lines(auto_p), load_lines(new_p)
    cast = load_cast(ed_p)
    e2a, a2n = _pair(edited, old_auto), _pair(old_auto, new)
    same = lambda x, y: abs(x["words"][0][0] - y["words"][0][0]) <= 0.15 and abs(x["words"][-1][1] - y["words"][-1][1]) <= 0.15
    # The ⚠ marks are the studio's opinion, not the user's work, so they always follow the remake: a line the user
    # placed by hand is checked (no mark), any other line takes the new version's mark and reason.
    out, changed, marks = [], 0, 0
    def remark(l, weak, why):
        nonlocal marks
        if l.get("ok"):
            weak, why = False, None  # the user said this line is fine
        if bool(l.get("weak")) == bool(weak) and (l.get("why") or None) == (why or None if weak else None):
            return l
        marks += 1
        return dict(l, weak=bool(weak), why=why if weak else None)
    for i, l in enumerate(edited):
        a = e2a.get(i)
        if a is None or not same(l, old_auto[a]) or a2n.get(a) is None:
            out.append(remark(l, False, None)); continue  # added by the user, moved by hand, or gone from the remake: theirs stays
        n = new[a2n[a]]
        if same(n, old_auto[a]):
            out.append(remark(l, n.get("weak"), n.get("why"))); continue  # same timing: only its mark can change
        words = n["words"]
        if _ltext(l) != _ltext(old_auto[a]):  # the user fixed words: keep their text on the new timing
            words = remap_words(n["words"], " ".join(w[2] for w in l["words"]))
        ww = l.get("wwho") if l.get("wwho") and len(l["wwho"]) == len(words) else None
        out.append(dict(l, words=words, wwho=ww, weak=n.get("weak"), why=n.get("why")))
        changed += 1
    # lines the remake found that the old version didn't have at all (the user deleting a line is respected)
    paired_new = set(a2n.values())
    added = [n for j, n in enumerate(new) if j not in paired_new]
    out += added
    out.sort(key=lambda l: l["words"][0][0])
    shutil.copyfile(new_p, job_path(jid, "words.orig.json"))  # "Undo all edits" now goes to the new automatic version
    if not changed and not added and not marks:
        shutil.copyfile(ed_p, new_p)  # found nothing: their version, exactly
    else:
        save_lines(new_p, out, cast)  # (with only marks changed, every word and time is still theirs)
    return changed, len(added)


def maybe_auto_romaji(jid, rc):
    if rc != 0 or not C.OLLAMA_URL or not os.path.exists(job_path(jid, "result.json")):
        return
    res = json.load(open(job_path(jid, "result.json")))
    ja_auto = res.get("romaji") == "automatic" and res.get("lang") == "ja"
    latin = not res.get("romaji") and res.get("lang") not in ("ko", "zh", "ja") and res.get("script", "latin") == "latin"
    if not (ja_auto or latin):  # Korean/Chinese romanization and hand-made romaji aren't checked
        return
    set_job(jid, step="romaji")
    auto_romaji(jid)


def run_local(jid):
    rc, err, oom, crash = 1, None, False, None
    try:
        with lock:  # cancelled between claim and here: don't start it
            if load(jid).get("cancel_requested"):
                p = None
            else:  # its own process group, so a cancel stops yt-dlp, ffmpeg and the GPU work it started too
                p = subprocess.Popen([sys.executable, "-I", PIPELINE, f"{JOBS}/{jid}"], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, env=dict(C.tool_env(), PYTHONUNBUFFERED="1"),
                                     start_new_session=True)
                runners.setdefault(jid, {"where": "local", "stopper": None})["proc"] = p
        if p is None:
            finish(jid, 1, None, False, "local")
            return
        for line in p.stdout:
            line = line.rstrip()
            oom = oom or "out of memory" in line.lower()
            C.job_log(job_path(jid), line)
            if not line.startswith("@"):
                if re.match(r"[A-Za-z_.]*(Error|Exception)\b", line):  # a crash: keep what it said, to show with the failure
                    crash = line[:200]
                continue
            kind, _, text = line[1:].partition(" ")
            with lock:
                job = load(jid)
                if kind == "step":
                    job["step"] = text
                elif kind == "info":
                    job.setdefault("log", []).append(text)
                elif kind == "error":
                    err = text
                save(jid, job)
        rc = p.wait()
        with lock:  # gone: a cancel from now on (OOM wait, romaji check) has no process to stop
            r = runners.get(jid) or {}
            r.pop("proc", None)
            stopper = r.get("stopper")
        if stopper:
            stopper.join(15)  # cancelled: wait until everything it started is gone, so the GPU is free again
        if rc != 0 and not err and not oom and crash:
            err = f"it stopped with an error ({crash})"
        if oom and not cancel_requested(jid):
            time.sleep(20)
        if not cancel_requested(jid):
            maybe_auto_romaji(jid, rc)
        finish(jid, rc, err, oom, "local")
    except Exception as e:
        try:
            with lock:
                job = load(jid)
                if job.get("cancel_requested"):
                    _mark_cancelled(job)
                else:
                    job.update(status="failed", error=f"studio error: {str(e)[:150]}")
                save(jid, job)
        except Exception:
            pass
    finally:
        with lock:
            runners.pop(jid, None)
            slots["local"] = None
        wake.set()


def run_remote(jid):
    """Run the job on the GPU helper, mirror its progress, then copy the finished files here. Cancelled: keep asking
    the helper to stop it until it has, then end the song as cancelled (and remove the helper's copy)."""
    asked = 0.0
    try:
        job = load(jid)
        if job.get("cancel_requested"):  # cancelled between claim and here: don't send it
            finish(jid, 1, None, False, "remote")
            return
        helper_call("/jobs", "POST", {"id": jid, **{k: job.get(k) for k in ("yt", "lrc_id", "lyrics_source", "artist", "title", "lang", "lyrics_text")}})
        misses = 0
        while True:
            time.sleep(2)
            stopping = cancel_requested(jid)
            if stopping and time.time() - asked > 10:  # (a helper on code from before cancel existed says 404:
                asked = time.time()                    #  the song then ends when the helper finishes it)
                _helper_stop(jid)
            try:
                st = helper_call(f"/jobs/{jid}")
                misses = 0
            except Exception:
                misses += 1
                if misses >= 15:  # ~30 s without an answer: the PC was shut down or went to sleep
                    if stopping:
                        finish(jid, 1, None, False, "remote")
                    else:
                        set_job(jid, status="queued", step="queued", waiting="the helper PC went away; waiting for a graphics card")
                    return
                continue
            if not os.path.isdir(job_path(jid)):
                return  # deleted meanwhile
            if not stopping:
                set_job(jid, step=st.get("step") or "lyrics", log=st.get("log", []))
            if st["status"] == "running":
                continue
            try:  # the helper's full pipeline output for this run, whether it worked or not
                with helper_call(f"/jobs/{jid}/files/pipeline.log", timeout=30, raw=True) as r, open(job_path(jid, "pipeline.log"), "ab") as f:
                    f.write(r.read()[-2_000_000:])
            except Exception:
                C.job_log(job_path(jid), "(the helper's pipeline output couldn't be fetched)")
            if st["status"] == "done" and not stopping:
                for name in ("out.mp4", "vocals.m4a", "words.json", "lyrics.ass", "result.json"):
                    dst = job_path(jid, name)
                    with helper_call(f"/jobs/{jid}/files/{name}", timeout=120, raw=True) as r, open(dst + ".part", "wb") as f:
                        shutil.copyfileobj(r, f, 1 << 20)
                    os.replace(dst + ".part", dst)
                try:  # what the helper heard (the by-ear check uses it later); a helper on older code doesn't send it
                    with helper_call(f"/jobs/{jid}/files/heard.json", timeout=60, raw=True) as r, open(job_path(jid, "heard.json.part"), "wb") as f:
                        shutil.copyfileobj(r, f)
                    os.replace(job_path(jid, "heard.json.part"), job_path(jid, "heard.json"))
                except Exception:
                    if os.path.exists(job_path(jid, "heard.json.part")):
                        os.remove(job_path(jid, "heard.json.part"))
                # a trimmed song (a remake): the helper made it untrimmed; cut it here exactly as pipeline.py does for
                # a song made on this machine (it keeps the untrimmed copy here, for changing the trim later)
                trim_err = None
                try:
                    said = trimtools.reapply(job_path(jid), load(jid).get("trim"))
                    if said:
                        with lock:
                            j = load(jid)
                            j.setdefault("log", []).append(said)
                            save(jid, j)
                        C.job_log(job_path(jid), said)
                except Exception as e:
                    log.exception("trim of %s after the helper", jid)
                    trim_err = f"couldn't trim it as before ({str(e)[:100]})"
                if trim_err:
                    finish(jid, 1, trim_err, False, "remote")
                else:
                    maybe_auto_romaji(jid, 0)
                    finish(jid, 0, None, False, "remote")
            else:
                finish(jid, 1, st.get("error"), bool(st.get("oom")), "remote")
            try:
                helper_call(f"/jobs/{jid}", "DELETE")
            except Exception:
                pass
            return
    except Exception as e:
        try:
            if cancel_requested(jid):
                finish(jid, 1, None, False, "remote")
            else:
                set_job(jid, status="queued", step="queued", waiting=f"the helper PC had a problem ({str(e)[:60]}); waiting for a graphics card")
        except Exception:
            pass
    finally:
        with lock:
            runners.pop(jid, None)
            slots["remote"] = None
        wake.set()


# ---------------- the library: names and old versions ----------------
FILLER = {"karaoke", "version", "versions", "lyrics", "lyric", "with", "instrumental", "official", "video", "hd", "hq",
          "original", "key", "songs", "song", "sing", "king", "karafun", "along", "backing", "track", "no", "vocals",
          "vocal", "minus", "one", "the", "by", "in", "style", "of", "zoom", "lower", "higher", "4k", "1080p", "720p"}


# Library names: "Artist - Title (Karaoke Version)---<YouTube id>", "Title (Karaoke) - Artist", "Artist - Title -
# Karaoke Version from Zoom Karaoke", "Title - Artist Karaoke", "Singer - Title (Series OP)" (Anime/), and the
# studio's "Artist - Title (Music Video)". loose() makes the compare case- and accent-insensitive (Eslabon = Eslabón).
OLD_YT_RE = re.compile(r"(.+)---([A-Za-z0-9_-]{11})\.(mp4|webm|mkv|mp3)")  # an old YouTube download
KARAOKE_TAIL = {"karaoke", "version", "from", "zoom", "karafun", "lyrics", "lyric", "instrumental", "with", "hd", "hq",
                "official", "video", "sing", "king"}


def lib_name_parts(name, folder):
    """The song in a library file name, as its " - " parts, without the karaoke words around it."""
    q = re.sub(r"---[A-Za-z0-9_-]{11}$", "", name)
    if folder == "YouTube":
        q = re.sub(r"\s*[\(\[][^\)\]]*(karaoke|lyrics?|instrumental)[^\)\]]*[\)\]]", "", q, flags=re.I)
        q = re.split(r"\s+_\s+", q)[0]
    else:  # Anime/ from kara.moe: "Singer - Title (Series OP) (AI instrumental)"
        q = re.sub(r"\s*\([^)]*\)", "", q)
    parts = []
    for part in re.split(r"\s+[-–—]\s+", q):
        w = part.split()
        for i, x in enumerate(w):  # "… Karaoke", "… Karaoke Version from Zoom Karaoke": cut from that "Karaoke" on
            if norm(x) == "karaoke" and all(norm(y) in KARAOKE_TAIL for y in w[i + 1:]):
                w = w[:i]
                while w and norm(w[-1]) in ("zoom", "karafun", "from"):
                    w.pop()
                break
        while w and norm(w[0]) == "karaoke":  # "Karaoke Artist - Title"
            w = w[1:]
        part = " ".join(w).strip(" -_|:")
        if part:
            parts.append(part)
    while len(parts) > 2 and all(norm(x) in FILLER for x in parts[-1].split()):  # "… - Sing King", "… - HD"
        parts.pop()
    return parts


def same_song(a1, t1, a2, t2):
    """Two artist/title pairs name the same song: the same title, and artists alike (one contains the other, ignoring
    case, accents and spaces: "Eslabón Armado" and "Eslabon Armado, Peso Pluma")."""
    ta, tb, aa, ab = loose(t1), loose(t2), loose(a1), loose(a2)
    return bool(ta) and ta == tb and bool(aa) and bool(ab) and (aa in ab or ab in aa)


def lib_matches(parts, artist, title, strict=False):
    """Is the library entry (lib_name_parts) the song artist/title? "Artist - Title" or "Title - Artist". In the
    first order the title alone is enough (artists are spelt many ways), unless strict (for removing files): then the
    artist must be alike too, as it must in the second order."""
    t, a = loose(title), loose(artist)
    if not t or not parts:
        return False
    alike = lambda x: bool(a) and bool(x) and (a in x or x in a)
    if len(parts) == 1:
        return loose(parts[0]) == loose(f"{artist} {title}") or (not strict and loose(parts[0]) == t)
    if loose(" ".join(parts[1:])) == t and (not strict or alike(loose(parts[0]))):
        return True
    return loose(" ".join(parts[:-1])) == t and alike(loose(parts[-1]))


def mv_name(name):
    """(artist, title) of a studio file "Artist - Title (Music Video)" in Music Videos/ (None for other names)."""
    m = re.fullmatch(r"(.+?) - (.+) \(Music Video\)", name)
    return (m.group(1), m.group(2)) if m else None


def old_versions(job):
    """YouTube lyric versions of the same song in the library root (only clear matches: title and artist)."""
    out = []
    for name in sorted(os.listdir(LIB)):
        m = OLD_YT_RE.fullmatch(name)
        if m and os.path.isfile(os.path.join(LIB, name)) and \
                lib_matches(lib_name_parts(m.group(1), "YouTube"), job["artist"], job["title"], strict=True):
            out.append(name)
    return out


def remove_lib_file(name):
    """Delete an old song (and its lyrics files) from the top of the library; only plain file names there."""
    p = os.path.realpath(os.path.join(LIB, name))
    if os.path.dirname(p) != os.path.realpath(LIB) or not os.path.isfile(p):
        return False
    stem = os.path.splitext(p)[0]
    for ext in (os.path.splitext(p)[1], ".ass", ".cdg", ".ASS"):
        if os.path.isfile(stem + ext):
            os.remove(stem + ext)
    return True


@app.get("/api/jobs/{jid}/publish-info")
def publish_info(jid: str):
    job = load(jid)
    base = final_base(job)
    return {"name": base, "exists": os.path.exists(f"{MV_DIR}/{base}.mp4"), "old_versions": old_versions(job)}


@app.get("/api/jobs/{jid}/cover")
def job_cover(jid: str):
    p = job_path(jid, "cover.jpg")
    if not os.path.isfile(p):
        raise HTTPException(404, "no cover")
    return FileResponse(p, media_type="image/png" if open(p, "rb").read(4) == b"\x89PNG" else "image/jpeg", headers={"Cache-Control": "no-cache"})


@app.post("/api/jobs/{jid}/album")
async def job_album(jid: str, req: Request):
    """{"action": "lookup"} looks it up again; {"action": "clear"} removes the album and cover (players show the
    letter tile); {"action": "set", "album", "year", "genre"} sets them by hand (the cover stays)."""
    check_origin(req)
    b = await req.json()
    actor = agents.actor_of(req)
    load(jid)
    a = b.get("action")
    who = agents.stamp(actor)["by"]
    if a == "lookup":
        meta = await asyncio.to_thread(album_lookup, jid, who)
    elif a in ("clear", "set"):
        with lock:
            job = load(jid)
            if a == "clear":
                meta = {"status": "cleared", "at": round(time.time()), "by": who}
                if os.path.exists(job_path(jid, "cover.jpg")):
                    os.remove(job_path(jid, "cover.jpg"))
            else:
                y = str(b.get("year") or "").strip()
                if y and not (y.isdigit() and 1900 <= int(y) <= 2100):
                    raise HTTPException(400, "the year is four digits, like 2003")
                meta = dict(job.get("album_meta") or {}, status="hand", album=str(b.get("album") or "").strip()[:200] or None,
                            year=int(y) if y else None, genre=str(b.get("genre") or "").strip()[:80] or None, at=round(time.time()), by=who)
            job["album_meta"] = meta
            save(jid, job)
    else:
        raise HTTPException(400, "action: lookup, clear or set")
    if load(jid).get("published"):
        await asyncio.to_thread(update_catalog)
    return {"album_meta": meta, "has_cover": os.path.exists(job_path(jid, "cover.jpg"))}


@app.post("/api/jobs/{jid}/publish")
async def publish(jid: str, req: Request):
    check_origin(req)
    b = await req.json()
    return do_publish(jid, [str(x) for x in b.get("remove", [])], agents.actor_of(req))


def do_publish(jid, remove, actor):
    """Publish a made song into the library (and remove the listed old YouTube versions), signed by actor."""
    with lock:
        job = load(jid)
        if job["status"] not in ("done",):
            raise HTTPException(400, "finish making it first")
        if video_busy(job):
            raise HTTPException(409, VIDEO_BUSY)
        rm = videoswap.load_meta(videoswap.replace_dir(job_path(jid)))
        if rm and rm.get("stale"):  # out.mp4 would be the official picture while the page says it's theirs
            raise HTTPException(409, "your video in place of the official one needs Re-render first (or Restore official)")
        allowed = set(old_versions(job))
        if any(x not in allowed for x in remove):
            raise HTTPException(400, "can only remove the listed old versions")
        base = final_base(job)
        os.makedirs(MV_DIR, exist_ok=True)
        tmp = f"{MV_DIR}/.{base}.mp4.part"
        shutil.copyfile(job_path(jid, "out.mp4"), tmp)
        os.replace(tmp, f"{MV_DIR}/{base}.mp4")
        publish_lyrics(jid, base)
        removed = [name for name in remove if remove_lib_file(name)]
        replaced = remove_replaced_copies(job, base)
        # its published versions share these lyrics: they get them too
        vbases = []
        for m in videoswap.entries(job_path(jid)):
            pa = m.get("published_as")
            if m["mode"] == "version" and pa and os.path.exists(f"{MV_DIR}/{pa}.mp4"):
                publish_lyrics(jid, pa)
                vbases.append(pa)
                m["lyrics_published"] = time.time()
                videoswap.save_meta(videoswap.version_dir(job_path(jid), m["slug"]), m)
        # the guide vocals (base.vocals.m4a), the song's and its versions' (the same sound); never stops a publish
        guidevocals.write(job_path(jid), MV_DIR, [base] + vbases, log)
        job.pop("library_removed", None)   # (removed from the library earlier: back in it now)
        job.update(status="done", published=time.time(), published_as=base, unpublished_changes=False,
                   published_by=agents.stamp(actor))
        log.info("published %s as %s (by %s)", jid, base, job["published_by"]["by"])
        job.setdefault("log", []).append(f"published as '{base}'" + (f"; removed {len(removed)} old version(s)" if removed else "")
                                         + (f"; replaced the earlier copy '{', '.join(replaced)}'" if replaced else ""))
        save(jid, job)
        update_catalog()
    pitch_later(jid)
    if C.ALBUM_LOOKUP and not job.get("album_meta"):   # a newly published song: its album and cover, then the catalog
        threading.Thread(target=lambda: (album_lookup(jid), update_catalog()), daemon=True).start()
    msg = "published"
    twins = published_twins(job, base)
    if twins:
        msg += (f"; this song is also published by {', '.join(t['id'] for t in twins)} (the same video): "
                "the library lists it twice until one of them is deleted")
    return {"name": base, "removed": removed, "replaced": replaced, "twins": [t["id"] for t in twins], "message": msg}


def _same_song(a_artist, a_title, b_artist, b_title):
    return (catalog.norm_key(a_artist), catalog.norm_key(a_title)) == (catalog.norm_key(b_artist), catalog.norm_key(b_title))


def remove_replaced_copies(job, base):
    """(under the lock, publishing) The library copy of this song left by a remake: same artist and title, its job
    deleted (ks change / a remake replaces the job), published under another name (2026-11-22: Light Years Away was
    listed twice). Its files go (with its versions'); a copy whose job still exists is never touched here."""
    out = []
    for e in (catalog.read_catalog(LIB) or {}).get("songs", []):
        if not e.get("job_deleted") or e.get("base") == base or e.get("folder") != "Music Videos" \
                or not _same_song(e.get("artist"), e.get("title"), job.get("artist"), job.get("title")):
            continue
        for b in [e["base"]] + [v.get("base") for v in e.get("versions") or [] if v.get("base")]:
            for f in os.listdir(MV_DIR):
                if f.startswith(b + ".") and os.path.isfile(os.path.join(MV_DIR, f)):
                    os.remove(os.path.join(MV_DIR, f))
        out.append(e["base"])
        log.info("publish: removed %s, the earlier copy of %s - %s (its job is gone)", e["base"], job.get("artist"), job.get("title"))
    return out


def published_twins(job, base):
    """Other studio songs published from the same video, same artist and title, under another name: both stay listed
    (each has a job), so say so (2026-11-22: Dance Dance twice, one of them in lower case)."""
    return [j for j in job_index() if j["id"] != job["id"] and j.get("published") and j.get("published_as") != base
            and j.get("yt") and j.get("yt") == job.get("yt") and _same_song(j["artist"], j["title"], job.get("artist"), job.get("title"))]


LIB_EXTS_MEDIA = (".mp4", ".webm", ".mkv", ".mp3", ".zip")
JOB_RANK = {"done": 0, "running": 1, "queued": 2, "failed": 3}  # which of several studio songs a library song points to


def library_songs():
    """Every song file in the library. plays/last_played (unix time): Encore's plays that
    count (finished, or stopped after more than half), by file name; grade: the best singing score grade Encore
    reported for it (or None)."""
    plays = {}
    for _, title, last, n in encore.play_rows(STORE):
        p = plays.get(title, (0, None))
        plays[title] = (p[0] + n, max(x for x in (p[1], last) if x is not None) if (p[1] or last) else None)
    try:
        grades = STORE.best_grades()  # the best singing score grade per version played (Encore, §2.4)
    except sqlite3.Error:
        grades = {}
    songs = []
    for d, _, files in os.walk(LIB):
        for f in files:
            if f.lower().endswith(LIB_EXTS_MEDIA) and not f.startswith("."):
                rel = os.path.relpath(os.path.join(d, f), LIB)
                folder = os.path.dirname(rel) or "YouTube"
                title = re.sub(r"---[A-Za-z0-9_-]{11}$", "", os.path.splitext(f)[0])
                n, last = plays.get(title, (0, None))
                try:
                    added = os.path.getmtime(os.path.join(d, f))
                except OSError:
                    added = None
                songs.append({"file": rel, "title": title, "folder": folder, "plays": n, "last_played": last, "added": added,
                              "lyrics": os.path.exists(os.path.join(d, os.path.splitext(f)[0] + ".ass")),
                              "grade": grades.get(title)})
    jobs = job_index()
    by_pub = {j["published_as"]: j for j in jobs if j.get("published_as")}
    mvs = []  # (artist, title, library title, studio song) of every studio music video in the library
    for s in songs:
        if s["folder"] == "Music Videos":
            j = by_pub.get(s["title"])
            nm = mv_name(s["title"])
            if not j and nm:   # the job that published it is gone (replaced by a remake not published yet): the newest
                # song of that artist and title, so the file still has its page ("Welcome to the Black Parade", 2026-11-21)
                key = (catalog.norm_key(nm[0]), catalog.norm_key(nm[1]))
                same = [x for x in jobs if x["status"] in JOB_RANK
                        and (catalog.norm_key(x.get("artist")), catalog.norm_key(x.get("title"))) == key]
                j = max(same, key=lambda x: x.get("created") or 0) if same else None
            s["job"] = {"id": j["id"], "status": j["status"]} if j else None
            if nm:
                mvs.append((nm[0], nm[1], s["title"], s["job"]))
    for s in songs:
        if s["folder"] == "Music Videos":
            continue
        parts = lib_name_parts(s["title"], s["folder"])
        s["query"] = " - ".join(parts) or s["title"]
        # an anime song whose video already IS the official (anime) music video doesn't need one
        s["official_mv"] = s["folder"] != "YouTube" and bool(re.search(r"\([^)]*\bMV\b[^)]*\)", s["title"]))
        mv = next((m for m in mvs if lib_matches(parts, m[0], m[1])), None)
        s["has_mv"] = bool(mv)
        s["mv_title"], s["mv_job"] = (mv[2], (mv[3] or {}).get("id")) if mv else (None, None)
        # an old YouTube download next to its music video: it can be removed so the song is listed once
        s["removable"] = bool(mv) and s["folder"] == "YouTube" and bool(OLD_YT_RE.fullmatch(s["file"])) \
            and any(lib_matches(parts, m[0], m[1], strict=True) for m in mvs)
        s["job"] = None
        # made in the studio but not published (or waiting, being made, failed): point there, not at Make. Artist and
        # title must both match here: another artist's song of the same name mustn't hide the Make button.
        if not mv:
            cands = [j for j in jobs if j["status"] in JOB_RANK and lib_matches(parts, j["artist"], j["title"], strict=True)]
            if cands:
                j = min(cands, key=lambda j: (JOB_RANK[j["status"]], -(j.get("created") or 0)))
                s["job"] = {"id": j["id"], "status": j["status"], "published": bool(j.get("published")),
                            "when": j.get("finished") or j.get("created"), "stopping": bool(j.get("cancel_requested"))}
    songs.sort(key=lambda s: (-s["plays"], s["title"].lower()))
    return songs


@app.get("/api/library")
def library():
    return {"songs": library_songs()}


@app.post("/api/library/remove-old")
async def remove_old(req: Request):
    """Remove an old YouTube karaoke download from the library, when the studio's music video of the same song
    is there (so the song is listed once). Only that file and its lyrics files go."""
    check_origin(req)
    name = str((await req.json()).get("file") or "")
    with lock:
        s = next((x for x in library_songs() if x["file"] == name), None)
        if not s or not s.get("removable"):
            raise HTTPException(400, "only an old YouTube karaoke file whose music video is in the library can be removed here")
        if not remove_lib_file(name):
            raise HTTPException(404, "it's gone already")
    log.info("library: removed old version %s (the music video %s stays)", name, s.get("mv_title"))
    return {"ok": True, "message": "The library is up to date"}


# ---- remove a song from the library; refresh the catalog (README: "Library") ----
# A catalog song goes with exactly the files catalog.files_of names for it (its video, lyrics, words, pitch, vocals,
# cover, and its versions'); nothing else in the folder is touched. Its studio song stays (shown as removed from the
# library; Publish puts it back) unless the person deletes that too. A library file the catalog doesn't list (an old
# YouTube download, a video put there by hand) goes with its side files of exactly the same name. Players aren't
# told directly: an Encore reading this folder sees catalog.json change at once; a linked one drops the song on its
# next library sync (every 10 minutes, or Sync now on that Encore).
SIDE_EXTS = (".ass", ".ASS", ".cdg", ".CDG") + tuple(e for e in LIB_EXTS if e not in (".mp4", ".ass"))
PLAYERS_NOTE = "Linked Encores drop it on their next library sync (within 10 minutes, or Sync now on the Encore)"
REFRESH_NOTE = "Linked Encores get the changes on their next library sync (within 10 minutes, or Sync now on the Encore)"


def _refuse_players(req):
    """Players (Encore) never change the library: a call carrying a player's token is refused, whatever the network
    or the proxy let through (their own API, /api/encore/, has nothing that removes)."""
    auth = req.headers.get("authorization") or ""
    if auth[:7].lower() == "bearer " and STORE.auth(auth[7:].strip()):
        raise HTTPException(403, "players can't change the library")


def _lib_path(rel):
    """A library path ('/'-separated, relative to the library root, as the catalog names files) as a path inside
    the library, or None (absolute, '..', dot names, or a folder that leads outside)."""
    rel = str(rel or "")
    parts = rel.split("/")
    if not rel or "\\" in rel or "\x00" in rel or any(not p or p.startswith(".") for p in parts):
        return None
    p = os.path.join(LIB, *parts)
    root = os.path.realpath(LIB)
    return p if os.path.commonpath([os.path.realpath(os.path.dirname(p)), root]) == root else None


def removal_target(job=None, base=None, file=None):
    """What removing a song would delete, without deleting anything: {"song", "kind": "catalog" | "file", "base",
    "bases", "files": library paths that exist, "versions": names, "jobs": studio songs published there, "job": the
    studio song asked about (or the catalog song's own), "studio": does it have a studio song}."""
    cv = catalog_view()
    entry, j = None, None
    if job:
        j = load(job)
        b = j.get("published_as")
        if not j.get("published") or not b:
            raise HTTPException(400, "this song isn't in the library")
        hit = cv["by_base"].get(b)
        if not hit:   # published, but its video is gone from the library (by hand): only the studio's state changes
            return {"song": f"{j.get('artist')} - {j.get('title')}", "kind": "catalog", "base": b, "bases": [b], "files": [],
                    "versions": [], "jobs": [j["id"]], "job": j["id"], "studio": True}
        entry = hit[0]
    elif base and not cv["by_base"].get(str(base)) and "/" not in str(base) and os.path.isfile(os.path.join(MV_DIR, f"{base}.mp4")):
        return removal_target(file=os.path.relpath(os.path.join(MV_DIR, f"{base}.mp4"), LIB).replace(os.sep, "/"))
    elif base:   # (a music video put there by hand, not in the catalog: as its file, above)
        hit = cv["by_base"].get(str(base))
        if not hit:
            raise HTTPException(404, "no song of that name in the library")
        entry = hit[0]
    elif file:
        rel = str(file)
        p = _lib_path(rel)
        if not p or not os.path.isfile(p):
            raise HTTPException(404, "no such file in the library")
        entry = next((e for e in cv["songs"] if rel in catalog.files_of(e)), None)
        if entry is None:   # not a catalog song: that file and its side files of the same name
            stem, ext = os.path.splitext(rel)
            if ext.lower() not in LIB_EXTS_MEDIA:
                raise HTTPException(400, "pick the song's video")
            files = [rel] + [stem + x for x in SIDE_EXTS if stem + x not in cv["files"] and os.path.isfile(_lib_path(stem + x) or "")]
            return {"song": os.path.basename(stem), "kind": "file", "base": None, "bases": [], "files": sorted(set(files)),
                    "versions": [], "jobs": [], "job": None, "studio": False}
    else:
        raise HTTPException(400, "say which song: job, base or file")
    bases = [entry["base"]] + [v["base"] for v in entry.get("versions") or [] if v.get("base")]
    files = sorted(f for f in catalog.files_of(entry) if _lib_path(f) and os.path.lexists(_lib_path(f)))
    own = entry["id"] if ID_RE.match(entry.get("id") or "") and os.path.isfile(os.path.join(JOBS, entry["id"], "job.json")) else None
    jobs = {x["id"] for x in job_index() if x.get("published") and x.get("published_as") in bases}
    jobs |= {x for x in (own, j and j["id"]) if x}
    return {"song": catalog.song_name(entry), "kind": "catalog", "base": entry["base"], "bases": bases, "files": files,
            "versions": [v.get("name") or v.get("base") for v in entry.get("versions") or []], "jobs": sorted(jobs),
            "job": j["id"] if j else own, "studio": bool(j or own)}


def remove_from_library(t, actor, delete_job=False):
    """(under the lock) Delete a removal_target's files, mark its studio songs "removed from the library" (they can be
    published again), optionally delete the studio song too, then rewrite the catalog. Everything that can refuse
    refuses before anything is deleted."""
    if delete_job and not t["job"]:
        raise HTTPException(400, "this song has no studio copy to delete")
    for jid in t["jobs"]:
        jj = load(jid)
        if jj.get("status") == "running":
            raise HTTPException(409, f"“{jj.get('artist')} - {jj.get('title')}” is being made right now; wait for it to finish")
        if video_busy(jj):
            raise HTTPException(409, VIDEO_BUSY)
    gone = []
    for rel in t["files"]:
        p = _lib_path(rel)
        if p and os.path.lexists(p) and not os.path.isdir(p):
            os.remove(p)
            gone.append(rel)
    who = agents.stamp(actor)
    bases = set(t["bases"])
    for jid in t["jobs"]:
        jj = load(jid)
        for m in videoswap.entries(job_path(jid)):   # its published versions went too
            if m.get("mode") == "version" and m.get("published_as") in bases:
                for k in ("published", "published_as", "lyrics_published", "published_by", "unpublished_changes"):
                    m.pop(k, None)
                videoswap.save_meta(videoswap.version_dir(job_path(jid), m["slug"]), m)
        was = jj.get("published_as")
        jj.update(published=None, published_as=None, unpublished_changes=False,
                  library_removed=dict(who, base=was, files=len(gone)))
        jj.setdefault("log", []).append(f"removed from the library by {who['by']} ('{was}', {len(gone)} files)")
        save(jid, jj)
    deleted = None
    if delete_job:
        shutil.rmtree(job_path(t["job"]))
        deleted = t["job"]
    update_catalog()
    log.info("library: %s removed %s (%s) by %s (%s): %s%s", "song" if t["kind"] == "catalog" else "file", t["song"],
             ", ".join(t["jobs"]) or "no studio song", who["by"], who["kind"], ", ".join(gone) or "no files",
             f"; deleted the studio song {deleted}" if deleted else "")
    return {"ok": True, "song": t["song"], "removed": gone, "jobs": t["jobs"], "job_deleted": deleted,
            "message": f"Removed “{t['song']}” from the library ({len(gone)} file{'s' if len(gone) != 1 else ''})"
                       + ("; its studio copy is deleted too" if deleted else "; its studio copy stays (Publish puts it back)" if t["jobs"] else "")
                       + (". " + PLAYERS_NOTE + "." if t["kind"] == "catalog" else "."),
            "players": PLAYERS_NOTE}


@app.get("/api/library/remove-preview")
def remove_preview(job: str = "", base: str = "", file: str = ""):
    """What "Remove from the library" would delete (nothing is changed)."""
    t = removal_target(job or None, base or None, file or None)
    return {k: t[k] for k in ("song", "kind", "base", "files", "versions", "jobs", "job", "studio")}


@app.post("/api/library/remove")
async def remove_song(req: Request):
    """{"job": id} | {"base": catalog base} | {"file": library path}, "delete_job": bool. Same permission as publishing
    (agents: the publish scope; deleting the studio song too: delete as well). Players never."""
    check_origin(req)
    _refuse_players(req)
    b = await req.json()
    actor = agents.actor_of(req)
    delete_job = bool(b.get("delete_job"))
    if delete_job and not agents.can(actor, "delete"):
        raise HTTPException(403, f"this agent key ({actor['name']}) doesn't allow: {agents.scope_label('delete')} (scope 'delete')")

    def work():
        with lock:
            t = removal_target(str(b.get("job") or "") or None, str(b.get("base") or "") or None, str(b.get("file") or "") or None)
            return remove_from_library(t, actor, delete_job)
    return await asyncio.to_thread(work)


@app.post("/api/library/refresh")
def refresh_library(req: Request):
    """Rebuild catalog.json from the library folder and the studio's songs (files added or removed by hand) and say
    what changed. Nothing changed: the file is left as it is. Same permission as publishing."""
    check_origin(req)
    _refuse_players(req)
    actor = agents.actor_of(req)
    with lock:   # never in the middle of a publish or a removal
        try:
            r = catalog.refresh_report(JOBS, LIB, MV_DIR, log)
        except OSError as e:
            log.exception("library: refresh couldn't write %s", catalog.CATALOG)
            raise HTTPException(500, f"couldn't write {catalog.CATALOG}: {e.strerror or e}")
    log.info("library: refreshed by %s: %d added, %d removed, %d changed%s", agents.stamp(actor)["by"], len(r["added"]),
             len(r["removed"]), len(r["changed"]), "" if r["written"] else " (catalog unchanged)")
    said = ", ".join(f"{len(r[k])} {k}" for k in ("added", "removed", "changed") if r[k])
    r["players"] = REFRESH_NOTE if r["written"] else None
    r["message"] = (f"Library refreshed: {said or 'catalog rewritten'} ({r['songs']} songs). {REFRESH_NOTE}." if r["written"]
                    else f"Nothing changed: {r['songs']} songs in the catalog")
    return r


@app.get("/api/status")
def status():
    if C.GPU_HELPER_URL and time.time() - helper_seen["at"] > 20:
        helper_health()  # the Make page shows each card's state
    hh = helper_seen["health"] or {}
    return {"helper_free_gb": round(hh.get("free_mib", 0) / 1024, 1) if hh else None, "helper_gpu": (hh.get("gpu") or "").replace("NVIDIA GeForce ", "") or None,"gpu_free_gb": round(gpu_free_mib() / 1024, 1), "need_gb": NEED_GPU_MIB // 1024,
            "romaji_ai": bool(C.OLLAMA_URL),
            "requests_open": STORE.q("select count(*) from requests where status='open'")[0][0],
            "pitch_pending": pitch_pending(),
            "gpus": {"local": LOCAL_GPU, "local_busy": slots["local"] is not None,
                     "helper": C.GPU_HELPER_NAME if C.GPU_HELPER_URL else None,
                     "helper_online": bool(helper_seen["health"]) if C.GPU_HELPER_URL else None,
                     "helper_busy": slots["remote"] is not None, "helper_outdated": bool(helper_seen.get("stale")),
                     "code_version": code_version()},
            "renderer": os.path.exists(f"{HERE}/static/octopus/subtitles-octopus.js"),
            "fonts": sorted(f"/static/octopus/{f}" for f in os.listdir(f"{HERE}/static/octopus")
                            if f.lower().endswith((".ttf", ".otf")))
            if os.path.isdir(f"{HERE}/static/octopus") else []}


def recover_jobs():
    """At startup: a job left "running" by a crash/restart goes back in the queue, unless it was being cancelled."""
    for j in list_jobs():
        with lock:
            job = load(j["id"])
            if job.get("status") == "running":
                if job.get("cancel_requested"):
                    _mark_cancelled(job, "cancelled by you (the studio restarted while stopping it)")
                else:
                    job.update(status="queued", step="queued")
                save(j["id"], job)
            if (job.get("video_task") or {}).get("running"):  # not requeued: the custom videos say they need a re-render
                job["video_task"] = dict(job["video_task"], running=False, error="the studio restarted while making it; try again (Re-render)")
                save(j["id"], job)


# ---------------- players (Encore): contract/ENCORE-STUDIO.md ----------------
# /api/encore/* is the player API: a bearer token per linked player (pairing: "Connect a player" on the Players page),
# JSON bodies of at most 64 KB checked against contract/schemas (a words-scoring recording: Opus audio, at most 8 MB),
# errors as {"error": "..."}. The admin pages
# (/api/players, /api/requests, flags on the song page) are the web app's own, behind its login like the rest.
STORE = encore.Store(preset_token=C.ENCORE_TOKEN or None)
MAX_BODY = 64 * 1024
PAIR_LIMIT = encore.RateLimit(5, 60)          # pairing tries per address and minute
BAD_TOKENS = encore.RateLimit(20, 60)         # wrong tokens per address and minute, then 429 until it calms down
_ONLY_NETS = []
for _n in C.ENCORE_ONLY_NETS.split(","):
    if _n.strip():
        try:
            _ONLY_NETS.append(ipaddress.ip_network(_n.strip(), strict=False))
        except ValueError:
            log.error("ENCORE_ONLY_NETS: %r isn't a network (like 10.42.0.0/24); ignored", _n.strip())
UA_RE = re.compile(r"^\S+?/(\S+)\s+contract/(\S+)")


class EncoreError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _err(status, msg, headers=None):
    return JSONResponse({"error": msg}, status_code=status, headers=headers)


def _addr(req):
    return req.client.host if req.client else "?"


def _only_player_api(req):
    """The request came in on a network that may use only the player API (ENCORE_ONLY_NETS; by the studio's own
    address on that network, which a client can't fake)."""
    try:
        ip = ipaddress.ip_address((req.scope.get("server") or ("",))[0])
    except ValueError:
        return False
    return any(ip in n for n in _ONLY_NETS)


@app.middleware("http")
async def encore_gate(req: Request, call_next):
    path = req.url.path
    if not path.startswith("/api/encore/"):
        if _ONLY_NETS and _only_player_api(req):
            return _err(403, "only the player API (/api/encore/) answers on this network")
        return await call_next(req)
    audio = path == "/api/encore/score-words" and req.method == "POST"  # (§2.9: the one body that's audio)
    picture = req.method == "PUT" and path.startswith("/api/encore/profiles/") and "/photo/" in path   # (§2.6 photo)
    limit = wordscore.MAX_BYTES if audio else encore.PHOTO_MAX if picture else MAX_BODY
    try:
        if int(req.headers.get("content-length") or 0) > limit:
            return _err(413, "the recording is too big (8 MB at most)" if audio else
                        f"a profile picture is at most {encore.PHOTO_MAX // 1024} KB" if picture else "the body is too big (64 KB at most)")
    except ValueError:
        return _err(400, "bad Content-Length")
    if path != "/api/encore/pair":
        addr = _addr(req)
        if BAD_TOKENS.over(addr):
            return _err(429, "too many wrong tokens; wait a minute", {"Retry-After": "60"})
        auth = req.headers.get("authorization") or ""
        player = STORE.auth(auth[7:].strip()) if auth[:7].lower() == "bearer " else None
        if not player:
            BAD_TOKENS.hit(addr)
            return _err(401, "Unknown player token (not linked, or unlinked by the Studio).")
        req.state.player = player
        m = UA_RE.match(req.headers.get("user-agent") or "")
        backlog = req.headers.get("x-encore-outbox")
        try:
            STORE.seen(player, encore=m.group(1)[:40] if m else None, contract=m.group(2)[:20] if m else None,
                       backlog=max(0, int(backlog)) if backlog not in (None, "") else None)
        except (ValueError, sqlite3.Error):
            pass
    try:
        return await call_next(req)
    except EncoreError as e:  # (normally turned into an answer by the handler below)
        return _err(e.status, e.msg)


# ---------------- agents: keys, scopes, who is asking (agents.py; AGENTS.md) ----------------
KEYS = agents.KeyStore()
BAD_KEYS = encore.RateLimit(20, 60)           # wrong agent keys per address and minute, then 429


@app.middleware("http")
async def agent_gate(req: Request, call_next):
    """Every API call (not the player API) gets an actor: an agent (X-Studio-Agent-Key, checked against its scopes
    here), a person (the login proxy's header), ks or the web app. A wrong or revoked key is refused, never treated as
    no key. AGENT_KEYS_REQUIRED: calls with neither a login header nor a key are refused."""
    path = req.url.path
    if not path.startswith("/api/") or path.startswith("/api/encore/"):
        return await call_next(req)
    key = req.headers.get(agents.KEY_HEADER)
    row = None
    if key is not None:
        addr = _addr(req)
        if BAD_KEYS.over(addr):
            return JSONResponse({"detail": "too many wrong agent keys; wait a minute"}, status_code=429, headers={"Retry-After": "60"})
        row = KEYS.auth(key.strip())
        if not row:
            BAD_KEYS.hit(addr)
            return JSONResponse({"detail": "unknown agent key (revoked, or not made on this Studio): an admin makes keys on the Agents tab"},
                                status_code=401)
    elif C.AGENT_KEYS_REQUIRED and not (req.headers.get(agents.person_header()) or "").strip():
        return JSONResponse({"detail": "this Studio needs a login (through its proxy) or an agent key (X-Studio-Agent-Key)"},
                            status_code=401)
    actor = agents.actor_from_headers(req.headers, row)
    req.state.actor = actor
    if row is not None and path != "/api/agent/whoami":
        scope = agents.scope_for(req.method, path)
        if scope in (None, agents.ADMIN):
            return JSONResponse({"detail": f"agents can't use {req.method} {path}: it's for the Studio's admins, in the web app",
                                 "scope": scope}, status_code=403)
        if scope not in row["scopes"]:
            return JSONResponse({"detail": f"this agent key ({row['name']}) doesn't allow: {agents.scope_label(scope)} "
                                           f"(scope '{scope}'); an admin can allow it on the Agents tab", "scope": scope},
                                status_code=403)
        if req.method not in ("GET", "HEAD"):
            log.info("agent %s: %s %s", row["name"], req.method, path)
    return await call_next(req)


@app.exception_handler(EncoreError)
async def _encore_error(req: Request, e: EncoreError):
    return _err(e.status, e.msg, {"Retry-After": "60"} if e.status == 429 else None)


from starlette.exceptions import HTTPException as _StarletteHTTPException  # noqa: E402
from fastapi.exception_handlers import http_exception_handler as _default_http_handler  # noqa: E402


@app.exception_handler(_StarletteHTTPException)
async def _http_error(req: Request, e):
    if req.url.path.startswith("/api/encore/"):  # the player API's errors: {"error": "..."} (unknown paths too)
        return _err(e.status_code, str(e.detail))
    return await _default_http_handler(req, e)


async def _body(req, schema):
    raw = await req.body()
    if len(raw) > MAX_BODY:
        raise EncoreError(413, "the body is too big (64 KB at most)")
    try:
        b = json.loads(raw or b"null")
    except ValueError:
        raise EncoreError(400, "the body isn't JSON")
    errs = schemalite.errors(schema, b)
    if errs:
        raise EncoreError(422, "; ".join(errs[:6]))
    return b


def _guard(fn):
    """A player API handler: an unexpected failure answers 500 {"error"} (and is logged)."""
    import functools

    @functools.wraps(fn)
    async def wrap(*a, **kw):
        try:
            if asyncio.iscoroutinefunction(fn):
                return await fn(*a, **kw)
            from starlette.concurrency import run_in_threadpool
            return await run_in_threadpool(fn, *a, **kw)  # (reads files: not on the event loop)
        except (EncoreError, HTTPException):
            raise
        except Exception:
            log.exception("player API: %s failed", fn.__name__)
            raise EncoreError(500, "the Studio hit an error; it's in its log")
    return wrap


# ---- the catalog as players see it (cached until catalog.json changes) ----
_CAT = {"key": None}


def catalog_view():
    """{"songs", "by_base": base -> (song, version name or None), "by_id", "files": paths players may fetch}."""
    p = os.path.join(LIB, catalog.CATALOG)
    try:
        st = os.stat(p)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key != _CAT["key"] or key is None:
        data = catalog.read_catalog(LIB) or {"songs": []}
        songs = [e for e in data.get("songs", []) if isinstance(e, dict) and e.get("base")]
        by_base, files = {}, set()
        for e in songs:
            by_base.setdefault(e["base"], (e, None))
            for v in e.get("versions") or []:
                if v.get("base"):
                    by_base.setdefault(v["base"], (e, v.get("name")))
            files |= catalog.files_of(e)
        _CAT.update(key=key, songs=songs, by_base=by_base, by_id={e["id"]: e for e in songs}, files=files)
    return _CAT


_CAT_TIMER = {"t": None}


def catalog_soon(delay=20.0):
    """Rewrite catalog.json shortly (plays arrive in bursts: one rewrite for the lot)."""
    with lock:
        if _CAT_TIMER["t"] is None:
            def run():
                _CAT_TIMER["t"] = None
                update_catalog()
            _CAT_TIMER["t"] = threading.Timer(delay, run)
            _CAT_TIMER["t"].daemon = True
            _CAT_TIMER["t"].start()


# ---- pitch files (pitch.py), written in the background after a publish ----
# The work survives a restart: a song waiting for its pitch files has a marker in its job folder (pitch.todo, written
# when it's queued, removed once its files are written or it failed), and at startup the worker takes the marked songs
# up again, then sweeps the published songs for a missing pitch file or one older than the vocals (a remake). It
# sweeps again every PITCH_SWEEP_S while it has nothing to do, so songs whose files went missing any other way fill in
# too. It never tracks while a song is being made here or is about to be (pitch_idle): it waits for that.
# A song whose pitch couldn't be made is remembered with its vocals' stamp (pitch.failed): the sweep leaves it alone
# until its vocals change (a publish still tries again).
PITCH_TODO, PITCH_FAILED = "pitch.todo", "pitch.failed"
PITCH_SWEEP_S = 3600.0
PITCH_IDLE_POLL_S = 30.0
_PITCH = {"thread": None, "order": [], "cv": threading.Condition(), "sweep_at": 0.0, "current": None}


def pitch_later(jid):
    """Write the published song's <base>.pitch.json (and its versions') soon; never holds up the publish."""
    pitch_queue(jid)


def pitch_queue(jid, mark=True):
    """Queue the song for its pitch files (once: a song already waiting isn't queued twice) and start the worker.
    mark: leave its marker (pitch.todo) so a restart picks it up again."""
    if mark:
        try:
            p = job_path(jid, PITCH_TODO)
            with open(p + ".tmp", "w") as f:
                f.write(str(time.time_ns()))   # (a token: a song queued again while it's being written keeps its marker)
            os.replace(p + ".tmp", p)
        except (HTTPException, OSError):
            pass
    with _PITCH["cv"]:
        if jid not in _PITCH["order"]:
            _PITCH["order"].append(jid)
        _PITCH["cv"].notify()
    _pitch_start()


def _pitch_start():
    with _PITCH["cv"]:
        if _PITCH["thread"] is None or not _PITCH["thread"].is_alive():
            _PITCH["thread"] = threading.Thread(target=_pitch_worker, daemon=True, name="pitch")
            _PITCH["thread"].start()


def pitch_pending():
    """How many songs wait for their pitch files (the one being written included)."""
    with _PITCH["cv"]:
        return len(_PITCH["order"]) + (1 if _PITCH["current"] else 0)


def pitch_idle():
    """True when pitch tracking may run: no song is being made here, and none is about to be (one waiting that may use
    this card while it has room). Songs waiting only for the helper PC, or for room the card doesn't have, don't hold
    it up (they can wait for hours)."""
    try:
        if slots["local"] is not None:
            return False
        if C.USE_LOCAL_GPU and any(j["status"] == "queued" and (j.get("gpu_pref") or "auto") in ("auto", "local")
                                   for j in job_index()):
            return gpu_free_mib() < NEED_GPU_MIB
        return True
    except Exception:
        return False


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def pitch_missing(jid, job=None):
    """True when a published song (or one of its versions) has no pitch file, or one older than its vocals, and
    making it hasn't failed on these same vocals before."""
    try:
        job = job or load(jid)
        if job.get("status") != "done":
            return False
        voc = job_path(jid, "vocals.m4a")
        if not os.path.exists(voc):
            return False
        bases = published_bases(jid, job)
        if not bases:
            return False
        vt = os.path.getmtime(voc)
        if all(os.path.exists(f"{MV_DIR}/{b}{pitchmod.EXT}") and os.path.getmtime(f"{MV_DIR}/{b}{pitchmod.EXT}") >= vt
               for b in bases):
            return False
        return _read(job_path(jid, PITCH_FAILED)) != pitchmod._stamp(voc)
    except (HTTPException, OSError, ValueError):
        return False


def pitch_resume():
    """At startup (and after each sweep interval): queue the songs with a marker (oldest first), then the published
    songs whose pitch file is missing or stale. Returns how many were queued."""
    marked, swept = [], []
    for jid in sorted(os.listdir(JOBS)) if os.path.isdir(JOBS) else []:
        if not ID_RE.match(jid):
            continue
        if os.path.exists(os.path.join(JOBS, jid, PITCH_TODO)):
            marked.append(jid)
        elif pitch_missing(jid):
            swept.append(jid)
    for jid in marked:
        pitch_queue(jid, mark=False)
    for jid in swept:
        pitch_queue(jid)
    _PITCH["sweep_at"] = time.time()
    if marked or swept:
        log.info("pitch: %d song(s) to do (%d left from before, %d without an up-to-date pitch file)",
                 len(marked) + len(swept), len(marked), len(swept))
    return len(marked) + len(swept)


def published_bases(jid, job):
    out = []
    if job.get("published_as") and os.path.exists(f"{MV_DIR}/{job['published_as']}.mp4"):
        out.append(job["published_as"])
    for m in videoswap.entries(job_path(jid)):
        pa = m.get("published_as")
        if m.get("mode") == "version" and pa and os.path.exists(f"{MV_DIR}/{pa}.mp4"):
            out.append(pa)
    return out


def write_pitch(jid):
    job = load(jid)
    bases = published_bases(jid, job)
    if not bases:
        return []
    try:
        files = pitchmod.write(job_path(jid), job, MV_DIR, bases, log)
        log.info("pitch: wrote %s", ", ".join(files))
        return files
    finally:
        update_catalog()


def pitch_one(jid):
    """Write one queued song's pitch files and settle its marker: removed when done (unless the song was queued again
    meanwhile), a failure remembered with the vocals' stamp."""
    try:
        todo = job_path(jid, PITCH_TODO)
    except HTTPException:
        return  # the song was deleted meanwhile
    token = _read(todo)
    try:
        write_pitch(jid)
        try:
            os.remove(job_path(jid, PITCH_FAILED))
        except (HTTPException, OSError):
            pass
    except HTTPException:
        return
    except Exception as e:
        log.warning("pitch: no pitch file for %s (%s)", jid, str(e)[:200])
        try:
            st = pitchmod._stamp(job_path(jid, "vocals.m4a"))
            with open(job_path(jid, PITCH_FAILED), "w") as f:
                f.write(st)
        except (HTTPException, OSError):
            pass
    if token is not None and _read(todo) == token:
        try:
            os.remove(todo)
        except OSError:
            pass


def _pitch_worker():
    if not _PITCH["sweep_at"]:
        try:
            pitch_resume()
        except Exception:
            log.exception("pitch: couldn't look for songs without pitch files")
    while not _PITCH.get("stop"):   # (stop: the tests' own workers)
        with _PITCH["cv"]:
            while not _PITCH["order"] and not _PITCH.get("stop"):
                left = PITCH_SWEEP_S - (time.time() - _PITCH["sweep_at"])
                if left <= 0:
                    break
                _PITCH["cv"].wait(timeout=min(left, PITCH_SWEEP_S))
            jid = _PITCH["order"][0] if _PITCH["order"] else None
        if _PITCH.get("stop"):
            break
        if jid is None:  # nothing to do for a while: sweep (when idle)
            if pitch_idle():
                try:
                    pitch_resume()
                except Exception:
                    log.exception("pitch: couldn't look for songs without pitch files")
            else:
                _PITCH["sweep_at"] = time.time() - PITCH_SWEEP_S + PITCH_IDLE_POLL_S
            continue
        if not pitch_idle():  # a song is being made: wait, then look again
            time.sleep(PITCH_IDLE_POLL_S)
            continue
        with _PITCH["cv"]:
            if jid in _PITCH["order"]:
                _PITCH["order"].remove(jid)
            _PITCH["current"] = jid
        try:
            pitch_one(jid)
        except Exception:
            log.exception("pitch: %s", jid)
        finally:
            with _PITCH["cv"]:
                _PITCH["current"] = None


# ---- requests: matching and statuses ----
def link_request(jid, rid):
    """The song remembers the request (its status follows the song; kept when the song is made again in place)."""
    with lock:
        job = load(jid)
        rids = set(job.get("encore_rids") or [])
        if rid not in rids:
            job["encore_rids"] = sorted(rids | {rid})
            save(jid, job)


def unlink_request(rid):
    for j in job_index():
        if rid in (j.get("encore_rids") or []):
            with lock:
                job = load(j["id"])
                job["encore_rids"] = [x for x in job.get("encore_rids") or [] if x != rid]
                save(j["id"], job)


def match_song(artist, title, jobs=None):
    """What the studio has of a requested song: (status, job id, song id, base) with status have (published: id and
    base), ready (made, not published), making (waiting or being made) or open (nothing). Case- and accent-
    insensitive, either name order; without an artist, a title only matches when one artist has it."""
    artist, title = (artist or "").strip(), (title or "").strip()
    if not title:
        return ("open", None, None, None)
    jobs = job_index() if jobs is None else jobs

    def same(a, t):
        if artist:
            return same_song(a, t, artist, title) or same_song(a, t, title, artist)
        return bool(loose(t)) and loose(t) == loose(title)
    cands = [j for j in jobs if same(j["artist"], j["title"])]
    cat = [e for e in catalog_view()["songs"] if e.get("job_deleted") and same(e.get("artist"), e.get("title"))]
    if not artist and len({loose(x["artist"]) for x in cands} | {loose(e["artist"]) for e in cat}) > 1:
        return ("open", None, None, None)  # "Hello", sung by several: the admin picks
    pub = [j for j in cands if j.get("published") and j.get("published_as") and os.path.exists(f"{MV_DIR}/{j['published_as']}.mp4")]
    if pub:
        j = max(pub, key=lambda j: j.get("published") or 0)
        return ("have", j["id"], j["id"], j["published_as"])
    if cat:
        return ("have", None, cat[0]["id"], cat[0]["base"])
    for st, name in (("done", "ready"), ("running", "making"), ("queued", "making")):
        c = [j for j in cands if j["status"] == st]
        if c:
            return (name, max(c, key=lambda j: j.get("created") or 0)["id"], None, None)
    return ("open", None, None, None)


def _job_status(j):
    if j["status"] in ("queued", "running"):
        return ("making", j["id"], None, None)
    if j["status"] == "done":
        if j.get("published") and j.get("published_as"):
            return ("published", j["id"], j["id"], j["published_as"])
        return ("ready", j["id"], None, None)
    return None  # failed or cancelled: open again (Retry or another make picks it up)


def refresh_requests():
    """Bring the requests still moving (open, making, ready) up to date with the studio's songs. Returns how many
    changed. A request follows the newest song that remembers it; an open one that a song now matches (made without
    "Find and make") follows that song too."""
    moving = STORE.requests(statuses=("open", "making", "ready"))
    if not moving:
        return 0
    jobs = job_index()
    by_id, by_rid = {j["id"]: j for j in jobs}, {}
    for j in sorted(jobs, key=lambda j: j.get("created") or 0):
        for rid in j.get("encore_rids") or []:
            by_rid[rid] = j
    changed = 0
    etas = None
    for r in moving:
        j = by_rid.get(r["rid"]) or (by_id.get(r["job"]) if r["job"] else None)
        if r.get("am"):  # taken for party auto-make: its own rules (failed and ready keep their reasons)
            if etas is None:
                etas = auto_etas(jobs)
            kw = _auto_status(r, j, etas)
            if kw:
                changed += STORE.set_request(r["rid"], **kw)
            continue
        st = _job_status(j) if j else None
        if st is None and r["status"] == "open" and not j and not r.get("detached"):
            m = match_song(r["artist"], r["title"], jobs)
            if m[0] != "open":
                st = ("published" if m[0] == "have" else m[0], m[1], m[2], m[3])
                if m[1]:
                    link_request(m[1], r["rid"])
        st = st or ("open", None, None, None)
        changed += STORE.set_request(r["rid"], status=st[0], job=st[1], song_id=st[2], base=st[3], reason=None)
    return changed


# ---- party auto-make (automake.py; contract §2.2b) ----
def _auto_status(r, j, etas):
    """The status fields of a request taken for auto-make, from its song (None: leave it as it is)."""
    if not j:
        if r["job"]:  # its song was deleted at the Studio
            return dict(status="failed", reason="it was removed at the Studio", stage=None, eta=None, song_id=None, base=None)
        return None   # still finding the video and lyrics (the worker moves it on)
    base = dict(job=j["id"], song_id=None, base=None)
    if j["status"] in ("queued", "running"):
        return dict(base, status="making", reason=None, stage=AM.stage_of(j.get("step")) if j["status"] == "running" else "downloading",
                    eta=etas.get(j["id"]))
    if j["status"] == "done":
        if j.get("published") and j.get("published_as"):
            return dict(base, status="published", song_id=j["id"], base=j["published_as"], reason=None, stage=None, eta=None)
        if j.get("auto_held") or not j.get("automake"):  # below the floor (or a person's song): waits for a look
            return dict(base, status="ready", reason=AM.NEEDS_A_LOOK if j.get("automake") else None, stage=None, eta=None)
        return dict(base, status="making", reason=None, stage="publishing", eta=r.get("eta") or AM.round_eta(time.time() + AM.PUBLISH_S))
    return dict(base, status="failed", reason=AM.failure_reason(j), stage=None, eta=None)


_MAKE_S = {"at": 0.0, "v": None}


def make_seconds(jobs):
    """Median make time per card (local, remote), from the finished songs; cached for 10 minutes."""
    if _MAKE_S["v"] is None or time.time() - _MAKE_S["at"] > 600:
        _MAKE_S.update(at=time.time(), v=AM.make_seconds(jobs, LOCAL_GPU))
    return _MAKE_S["v"]


def _queue_plan(jobs, extra=None):
    now = time.time()
    secs = make_seconds(jobs)
    local_running = any(j["status"] == "running" and (runners.get(j["id"]) or {}).get("where", "local") == "local" for j in jobs)
    up = {"local": bool(C.USE_LOCAL_GPU) and (local_running or gpu_free_mib() >= NEED_GPU_MIB),  # (something else may hold it)
          "remote": bool(C.GPU_HELPER_URL) and bool(helper_seen.get("health")) and not helper_seen.get("stale")}
    running = {}
    for j in jobs:
        if j["status"] == "running" and j.get("started"):
            where = (runners.get(j["id"]) or {}).get("where") or ("local" if j.get("gpu") == LOCAL_GPU else "remote")
            running[where] = j["started"]
    queued = [(j["id"], j.get("gpu_pref") or "auto") for j in sorted(jobs, key=lambda j: j.get("created") or 0) if j["status"] == "queued"]
    return now, secs, up, running, AM.plan(now, up, running, queued, secs, extra)


def auto_etas(jobs):
    """job id -> when the song should be published (unix time, to 10 s), for the songs waiting or being made."""
    now, secs, up, running, plan = _queue_plan(jobs)
    out = {}
    for j in jobs:
        if j["status"] == "running" and j.get("started"):
            where = (runners.get(j["id"]) or {}).get("where") or ("local" if j.get("gpu") == LOCAL_GPU else "remote")
            end = j["started"] + secs.get(where, AM.DEFAULT_MAKE_S["local"])
            out[j["id"]] = AM.round_eta(max(end, now + 30.0) + AM.PUBLISH_S)
        elif j["status"] == "queued" and j["id"] in plan:
            out[j["id"]] = AM.round_eta(plan[j["id"]][1] + AM.PUBLISH_S)
    return out


def new_song_eta(jobs=None):
    """(wait for a card in seconds, when a song asked for now would be published) or (None, None): no card."""
    jobs = job_index() if jobs is None else jobs
    now, secs, up, running, plan = _queue_plan(jobs, extra=("new", "auto"))
    got = plan.get("new")
    if not got:
        return None, None
    start = max(got[0], now + AM.SEARCH_S)
    return got[0] - now, AM.round_eta(start + secs[got[2]] + AM.PUBLISH_S)


def automake_offer(player):
    """For hello (§2.2b): may this player ask for auto-make now, and about how many minutes it takes."""
    cfg = AM.load(STORE)
    if not (cfg["on"] and STORE.may_automake(player)):
        return {"allowed": False, "minutes": None}
    _, eta = new_song_eta()
    return {"allowed": True, "minutes": max(1, math.ceil((eta - time.time()) / 60)) if eta else None}


def automake_counts(now):
    q = lambda since: STORE.q("select count(*) from requests where am='taken' and am_at > ?", (since,))[0][0]
    return q(now - 3600), q(now - 86400)


def automake_take(player, b, ans):
    """A new request that asks for auto-make: take it (when the admin allows it for this player, within the limits)
    or leave it a normal request. Returns the POST answer (kept for a repeated rid)."""
    cfg = AM.load(STORE)
    if not (cfg["on"] and STORE.may_automake(player)):
        return ans
    rid, now = b["rid"], time.time()
    hour, day = automake_counts(now)
    wait, eta = new_song_eta()
    why = None
    if hour >= cfg["per_hour"]:
        why = f"the Studio's auto-make limit is reached ({cfg['per_hour']} songs an hour)"
    elif day >= cfg["per_day"]:
        why = f"the Studio's auto-make limit for today is reached ({cfg['per_day']} songs)"
    elif wait is None:
        why = "no graphics card is free at the Studio"
    elif wait > cfg["gpu_wait"] * 60:
        why = f"the Studio's graphics cards are busy (about {math.ceil(wait / 60)} min to wait)"
    if why:
        STORE.set_request(rid, am="taken", status="failed", reason=why, stage=None, eta=None)
        ans = dict(ans, status="failed", reason=why, auto=True, stage=None, eta=None)
        log.info("auto-make: request %s… not made: %s", rid[:8], why)
    else:
        STORE.set_request(rid, am="taken", am_at=now, stage="finding", eta=eta)
        ans = dict(ans, auto=True, stage="finding", eta=eta)
        automake_enqueue(rid)
        log.info("auto-make: request %s… %r - %r taken", rid[:8], b.get("artist"), b["title"])
    STORE.set_answer(rid, ans)
    return ans


_AM_Q = queue.Queue()
_AM_T = {"thread": None}


def automake_enqueue(rid):
    if _AM_T["thread"] is None or not _AM_T["thread"].is_alive():
        _AM_T["thread"] = threading.Thread(target=_automake_worker, daemon=True, name="automake")
        _AM_T["thread"].start()
    _AM_Q.put(rid)


def _automake_worker():
    while True:
        rid = _AM_Q.get()
        try:
            automake_run(rid)
        except Exception as e:
            log.exception("auto-make: request %s", rid[:8])
            try:
                STORE.set_request(rid, status="failed", reason="the Studio hit an error", stage=None, eta=None)
            except Exception:
                pass


PICK_WHY = {"video": "no music video found", "lyrics": "no timed lyrics found", "have": "already in the library",
            "ready": "already made (on the Songs tab, not published yet)", "making": "already in the make queue",
            "name": "a song with this name is already in the library"}


def pick_song(d, max_s, video=None, fallback=("", ""), names=None):
    """Choose a song's video and timed lyrics from a search answer the way the Make page does (autopick.py), and say
    why it can't be made as it is: party auto-make and imports. video: one the person chose (a playlist's own), kept;
    only the lyrics are picked for it. -> {"video", "lyrics", "artist", "title", "names_from", "kind", "why", "have"}:
    kind None (ready to make) or video, lyrics, long (longer than max_s), have, ready, making (match_song: the studio
    has it), name (a file of that name is in the library); why says it in words (long: the caller says it).
    fallback: the names asked for, when neither the pick nor the search has any. names: {"artist", "title", "from"}
    that win over the picked lyrics' (a video's own title, checked against LRCLIB)."""
    if video is None:
        p = autopick.choose(d)
    else:
        l = autopick.best_lyrics(d, video)
        n = autopick.auto_names(d, l, video)
        p = {"video": video, "lyrics": l, "artist": n["artist"], "title": n["title"], "names_from": n["from"]}
    if names and names.get("artist") and names.get("title"):
        p.update(artist=names["artist"], title=names["title"], names_from=names.get("from") or "the video's title")
    v, l = p["video"], p["lyrics"]
    out = dict(p, artist=p["artist"] or d.get("artist") or fallback[0] or "", title=p["title"] or d.get("title") or fallback[1] or "",
               kind=None, why=None, have=None)
    if not v:
        out["kind"] = "video"
    elif not l or not l.get("timed"):
        out["kind"] = "lyrics"
    elif (v.get("duration") or 0) > max_s:
        out["kind"] = "long"
    else:
        have = match_song(out["artist"], out["title"])  # it may be here under the names the lyrics give it
        out["have"] = have
        if have[0] in ("have", "ready", "making"):
            out["kind"] = {"have": "have", "ready": "ready", "making": "making"}[have[0]]
        elif os.path.exists(f"{MV_DIR}/{final_base(out)}.mp4"):
            out["kind"] = "name"
    if out["kind"] and out["kind"] != "long":
        out["why"] = PICK_WHY[out["kind"]]
    return out


def _auto_fail(rid, why):
    STORE.set_request(rid, status="failed", reason=why, stage=None, eta=None)
    log.info("auto-make: request %s… not made: %s", rid[:8], why)


def automake_run(rid):
    """Find the video and lyrics for a request taken for auto-make, the way the Make page picks them, and queue the
    song (or say why not)."""
    r = STORE.get_request(rid)
    if not r or r["status"] != "open" or r.get("am") != "taken" or r.get("job"):
        return
    cfg = AM.load(STORE)
    q = f"{r['artist']} - {r['title']}" if (r["artist"] or "").strip() else r["title"]
    try:
        d = search(q)
    except Exception as e:
        log.warning("auto-make: search for %s… failed: %s", rid[:8], str(getattr(e, "detail", e))[:120])
        return _auto_fail(rid, "couldn't search for it (YouTube or the lyrics sites didn't answer)")
    p = pick_song(d, cfg["max_minutes"] * 60, fallback=(r["artist"], r["title"]))
    v, l, artist, title = p["video"], p["lyrics"], p["artist"], p["title"]
    if p["kind"] == "long":
        m, sec = divmod(int(v["duration"]), 60)
        return _auto_fail(rid, f"too long for a quick make ({m}:{sec:02d}; the limit is {cfg['max_minutes']} min)")
    if p["kind"] == "have":
        have = p["have"]
        STORE.set_request(rid, status="have", job=have[1], song_id=have[2], base=have[3], stage=None, eta=None)
        return
    if p["kind"] in ("ready", "making"):  # someone's song: the request follows it (no auto-publish)
        link_request(p["have"][1], rid)
        refresh_requests()
        return
    if p["kind"] == "name":  # never replaced by a quick make
        base = final_base({"artist": artist, "title": title})
        hit = catalog_view()["by_base"].get(base)
        if hit:
            STORE.set_request(rid, status="have", song_id=hit[0]["id"], base=base, stage=None, eta=None)
            return
    if p["kind"]:
        return _auto_fail(rid, p["why"])
    pname = STORE.player_names().get(r["player"]) or ("the preset player" if r["player"] == encore.PRESET else "a player")
    jid = new_job({"yt": v["id"], "lrc_id": l["id"], "lyrics_source": l["source"], "artist": artist, "title": title,
                   "video_title": str(v.get("title") or "")[:200], "gpu_pref": "auto", "encore_rids": [rid],
                   "automake": {"rid": rid, "player": r["player"], "player_name": pname, "by": r["by_name"], "party": r["party"],
                                "at": time.time(), "video": v["id"], "lyrics": f"{l['source']}:{l['id']}",
                                "names_from": p["names_from"]}}, AM.actor(pname))
    log.info("auto-make: queued %s for %s (asked by %s) | video %s | lyrics %s:%s", jid, pname, r["by_name"], v["id"], l["source"], l["id"])
    STORE.set_request(rid, status="making", job=jid, stage="downloading")
    refresh_requests()
    wake.set()


def video_check(job, lines=None):
    """The song's video check (videocheck.py): the facts the pipeline found, read with the auto-make quality floor
    (one rule for "good enough"). None before the song is made."""
    r = job.get("result")
    if not r:
        return None
    if lines is None:
        lines = load_lines(job_path(job["id"], "words.json"))
    ok, why, stats = AM.check(r, lines, AM.load(STORE)["floor"])
    if ok:   # (the floor gives a reason only when it stops a song)
        why = f"{stats['sure']} of {stats['lines']} lines heard where they sit"
    v = VC.verdict(r, ok, why)
    v["stats"] = stats
    if job.get("auto_retry"):
        v["retried"] = job["auto_retry"]
    return v


def try_auto_fix(jid, job, v):
    """A song that didn't pass, for a cause the check can name (the language), or with another copy of the lyrics that
    fits what was heard clearly better: remade once with that. Never twice, and never for a low score alone
    (hard-to-hear songs score like wrong ones). True if a remake was started."""
    if job.get("auto_retry") or job.get("published"):
        return False
    fix, why = v.get("fix"), "; ".join(c["note"] for c in v["checks"] if not c["ok"])
    if not fix:   # another copy of the lyrics that fits what was heard clearly better (evidence, not just a low score)
        try:
            alt = better_lyrics(jid, job)
        except Exception:
            log.exception("video check: scoring other lyrics for %s", jid)
            alt = None
        if alt:
            fix = {"lrc_id": alt["lrc_id"], "lyrics_source": "lrclib"}
            why += f"; another copy of the lyrics fits what's heard better ({alt['fit']:.0%} of lines vs {alt['was']:.0%})"
    if not fix:
        return False
    with lock:
        j = load(jid)
        j["auto_retry"] = {"at": time.time(), "fix": fix, "why": why.strip("; ")}
        if fix.get("lang"):
            j["lang"] = fix["lang"]
        if fix.get("lrc_id"):
            j["auto_retry"]["lyrics_before"] = {"lrc_id": j.get("lrc_id"), "lyrics_source": j.get("lyrics_source")}
            j["lrc_id"], j["lyrics_source"] = fix["lrc_id"], fix["lyrics_source"]
        save(jid, j)
    _remake(jid, {"kind": "auto", "name": "video check"}, note=f"video check: made again ({j['auto_retry']['why']})")
    log.info("video check: %s made again with %s", jid, fix)
    return True


LYRICS_BETTER = 0.15   # another copy of the lyrics must fit what's heard by this much more (share of lines) to be used


def better_lyrics(jid, job):
    """Another LRCLIB copy of the song that fits what was heard in this video clearly better than its lyrics now
    (the same scoring as ks alt-lyrics: no GPU). {"lrc_id", "fit", "was"} or None."""
    if not os.path.exists(job_path(jid, "heard.json")):
        return None
    seen, cands = set(), []
    for q in ({"track_name": job.get("title") or "", "artist_name": job.get("artist") or ""},
              {"q": f"{job.get('artist') or ''} {job.get('title') or ''}"}):
        try:
            hits = http_json("https://lrclib.net/api/search?" + urllib.parse.urlencode(q)) or []
        except Exception:
            hits = []
        for x in hits:
            if x.get("id") not in seen and x.get("syncedLyrics") and not x.get("instrumental"):
                seen.add(x["id"]); cands.append({"id": x["id"], "synced": x["syncedLyrics"]})
    cur = job.get("lrc_id") if job.get("lyrics_source") == "lrclib" else None
    if cur is not None and cur not in seen:
        try:
            x = http_json(f"https://lrclib.net/api/get/{int(cur)}")
            if x and x.get("syncedLyrics"):
                cands.append({"id": x["id"], "synced": x["syncedLyrics"]})
        except Exception:
            pass
    if len(cands) < 2 and cur is not None:
        return None
    scores = _ks().altlyrics(job_path(jid), float(job.get("offset") or 0.0), cands[:30])
    by = {r["id"]: r for r in scores}
    was = by[cur]["fit"] if cur in by else 0.0
    best = max((r for r in scores if r["id"] != cur), key=lambda r: (r["fit"], -r["squeezed"]), default=None)
    if best and best["fit"] >= was + LYRICS_BETTER and best["fit"] >= 0.4:
        return {"lrc_id": best["id"], "fit": round(best["fit"], 2), "was": round(was, 2)}
    return None


def held_reason(v):
    return "; ".join(c["note"] for c in v["checks"] if not c["ok"]) or "it needs a look"


def auto_finish(jid):
    """An auto-made song is done: publish it if it clears the quality floor (and nothing of that name is in the
    library), else it waits for a look. The request follows."""
    rid = None
    try:
        job = load(jid)
        am = job.get("automake") or {}
        rid = am.get("rid")
        if job.get("status") != "done" or job.get("published") or job.get("auto_held") or job.get("quick"):
            return
        r = STORE.get_request(rid) if rid else None
        if not (r and r.get("am") == "taken" and r["status"] in ("open", "making") and r.get("job") == jid
                and AM.load(STORE)["on"] and STORE.may_automake(am.get("player"))):
            # made again by hand (Retry after it failed), or the admin turned it off meanwhile: a person publishes it
            with lock:
                j = load(jid)
                j["auto_held"] = "the request isn't waiting for this make any more (or auto-make was turned off)"
                j.setdefault("log", []).append("auto-make: not published automatically; publish it yourself if it's right")
                save(jid, j)
            log.info("auto-make: %s made, not published automatically (its request isn't waiting for it)", jid)
            return
        if rid:
            STORE.set_request(rid, status="making", stage="publishing", eta=AM.round_eta(time.time() + AM.PUBLISH_S))
        v = video_check(job)
        if not v["ok"] and try_auto_fix(jid, job, v):
            if rid:
                STORE.set_request(rid, status="making", stage="making", eta=None)
            return
        ok, why, stats = v["ok"], held_reason(v), v["stats"]
        base = final_base(job)
        if ok and os.path.exists(f"{MV_DIR}/{base}.mp4"):  # a song of that name was published meanwhile: never replaced
            ok, why = False, "a song with this name is already in the library"
            hit = catalog_view()["by_base"].get(base)
            if hit and rid:
                STORE.set_request(rid, status="have", song_id=hit[0]["id"], base=base, stage=None, eta=None)
        if not ok:
            with lock:
                j = load(jid)
                j["auto_held"] = why
                j.setdefault("log", []).append(f"auto-make: not published ({why}); it needs a look")
                save(jid, j)
            log.info("auto-make: %s not published: %s", jid, why)
            # the person hears about it on their devices (Studio app), so nothing waits unseen until after the party
            notify(f"⏸ {job.get('artist') or ''} – {job.get('title') or ''}".replace("⏸  – ", "⏸ "), f"Made for the party but held for a look: {why}", f"/#jobs/{jid}")
            return
        who = AM.actor(am.get("player_name"))
        do_publish(jid, [], who)
        with lock:
            j = load(jid)
            j["quick"] = {"at": time.time(), "for": am.get("player_name"), "by": am.get("by"), "party": am.get("party"), **stats}
            j.setdefault("log", []).append(f"{who['name']}: published without review ({stats['sure']} of {stats['lines']} lines heard in place)")
            save(jid, j)
        log.info("auto-make: published %s for %s (%s of %s heard in place)", jid, am.get("player_name"), stats["sure"], stats["lines"])
    except Exception as e:
        log.exception("auto-make: finishing %s", jid)
        try:
            set_job(jid, auto_held=f"auto-make hit an error ({str(e)[:80]})")
        except Exception:
            pass
    finally:
        refresh_requests()


def batch_finish(jid):
    """ks make --auto: published without review when its video check passes (with the old YouTube copy of the song
    removed, as Publish offers), otherwise remade once for a cause the check can name, otherwise it waits for a look
    with the reason on its page. Never over a song of that name that another make published."""
    try:
        job = load(jid)
        if job.get("status") != "done" or job.get("published") or job.get("auto_held"):
            return
        v = video_check(job)
        if not v["ok"] and try_auto_fix(jid, job, v):
            return
        ok, why = v["ok"], held_reason(v)
        base = final_base(job)
        other = (catalog_view()["by_base"].get(base) or [None])[0]
        if ok and os.path.exists(f"{MV_DIR}/{base}.mp4") and other and other.get("id") != jid:
            ok, why = False, "a song with this name is already in the library"
        if not ok:
            with lock:
                j = load(jid)
                j["auto_held"] = why
                j.setdefault("log", []).append(f"not published automatically ({why}); it needs a look")
                save(jid, j)
            log.info("batch: %s held: %s", jid, why)
            return
        do_publish(jid, old_versions(job), job["auto_publish"].get("by") or {"kind": "auto", "name": "batch"})
        set_job(jid, quick={"at": time.time(), "for": "batch", **v["stats"]})
        log.info("batch: published %s (%s of %s heard in place)", jid, v["stats"]["sure"], v["stats"]["lines"])
    except Exception as e:
        log.exception("batch: finishing %s", jid)
        try:
            set_job(jid, auto_held=f"publishing automatically hit an error ({str(e)[:80]})")
        except Exception:
            pass


def recover_automake():
    """At startup: requests still being looked for go back to the worker; auto-made songs that finished while the
    Studio was down get their check."""
    for r in STORE.requests(statuses=("open",)):
        if r.get("am") == "taken" and not r.get("job"):
            automake_enqueue(r["rid"])
    for j in job_index():
        if j.get("automake") and j["status"] == "done" and not j.get("published") and not j.get("auto_held"):
            if not load(j["id"]).get("quick"):
                threading.Thread(target=auto_finish, args=(j["id"],), daemon=True).start()
        elif j["status"] == "done" and not j.get("published") and not j.get("auto_held"):
            full = load(j["id"])
            if full.get("auto_publish") and not full.get("quick"):
                threading.Thread(target=batch_finish, args=(j["id"],), daemon=True).start()


# ---- imports: a YouTube playlist or a pasted list of songs, looked up one by one, reviewed, then made ----
# Each import is data/imports/<id>.json (kept 24 hours): its rows fill in as a background thread looks the songs up
# (one at a time, a pause between them), then the person reviews them on the Make tab (or ks import) and makes the
# ticked ones. Nothing is made before that. The picks are pick_song's, the same as party auto-make's.
IMPORTS = os.path.join(C.DATA, "imports")
IMPORT_KEEP_S = 24 * 3600
IMPORT_GAP_S = 1.5          # between songs: gentle on LRCLIB and YouTube
IMP_ID = re.compile(r"^imp-[0-9a-f]{10}$")
_IMP_LOCK = threading.RLock()
_IMP_Q = queue.Queue()
_IMP_T = {"thread": None}


def imp_path(iid):
    if not IMP_ID.match(iid or ""):
        raise HTTPException(404, "no such import")
    return os.path.join(IMPORTS, iid + ".json")


def imp_load(iid):
    try:
        imp = json.load(open(imp_path(iid)))
    except (OSError, ValueError):
        raise HTTPException(404, "no such import (imports are kept for 24 hours)")
    if time.time() - imp["created"] > IMPORT_KEEP_S:
        raise HTTPException(404, "no such import (imports are kept for 24 hours)")
    return imp


def imp_save(imp):
    os.makedirs(IMPORTS, exist_ok=True)
    p = imp_path(imp["id"])
    with open(p + ".tmp", "w") as f:
        json.dump(imp, f, indent=1)
    os.replace(p + ".tmp", p)


def imp_all():
    """The imports kept (newest first); old ones are deleted."""
    out = []
    for f in sorted(os.listdir(IMPORTS)) if os.path.isdir(IMPORTS) else []:
        if not f.endswith(".json"):
            continue
        p = os.path.join(IMPORTS, f)
        try:
            imp = json.load(open(p))
        except (OSError, ValueError):
            continue
        if time.time() - imp.get("created", 0) > IMPORT_KEEP_S:
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        out.append(imp)
    return sorted(out, key=lambda i: -i["created"])


def can_make_row(r):
    return bool(r.get("video") and r.get("lyrics") and r["lyrics"].get("timed") and not r.get("job"))


def imp_view(imp):
    rows = imp.get("rows") or []
    count = lambda st: sum(1 for r in rows if r["status"] == st)
    return {k: imp.get(k) for k in ("id", "created", "by", "kind", "playlist", "state", "left_out", "error")} | {
        "expires": imp["created"] + IMPORT_KEEP_S, "total": len(rows), "waiting": count("waiting"), "ready": count("ready"),
        "skipped": count("skipped"), "made": sum(1 for r in rows if r.get("job")),
        "rows": [{k: v for k, v in r.items() if k not in ("entry",)} | {"can_make": can_make_row(r)} for r in rows]}


def lyr_lang(l):
    """The lyrics' language or script, in words (for the review)."""
    sc = l.get("script")
    if l.get("japanese"):
        return "Japanese"
    if sc in ("ko", "zh"):
        return {"ko": "Korean", "zh": "Chinese"}[sc]
    if sc and sc != "latin":
        return sc.capitalize()
    if l.get("source") == "karamoe":
        return "romaji (kara.moe)"
    return "English" if l.get("english") else "Latin letters"


def _mmss(s):
    m, sec = divmod(int(s or 0), 60)
    return f"{m}:{sec:02d}"


def import_row(row, seen):
    """Look one row up: its video (the playlist's own, or the search's best), timed lyrics and names, and whether it's
    ready to make or skipped (why). seen: {key: row number} of the rows before it (duplicates)."""
    notes = []
    if row["kind"] == "video":
        info = row.get("entry")
        if info is None:
            info = read_video(row["id"])   # (a link pasted in a list; HTTPException: unavailable)
        if not info.get("title") or info.get("title") in ("[Private video]", "[Deleted video]") or not info.get("duration") \
                or (info.get("availability") or "public") not in ("public", "unlisted"):
            return {"status": "skipped", "why": "the video is unavailable (private, deleted or not playable here)",
                    "video": {"id": row["id"], "title": info.get("title") or "", "channel": "", "duration": None, "flags": [],
                              "thumb": f"https://i.ytimg.com/vi/{row['id']}/mqdefault.jpg", "link": True}}
        names = names_for_video(info.get("title"), info.get("channel") or info.get("uploader"))
        v = video_entry(dict(info, id=row["id"]), names["artist"], names["title"])
        v["link"] = True
        d = song_search(names["artist"], names["title"], videos=False)
        p = pick_song(d, SL.IMPORT_MAX_S, video=v, names=names if names["parsed"] else None)
        if not names["parsed"]:
            notes.append("couldn't tell the artist from the video's title")
        if v["flags"]:
            notes.append(f"looks like a {', '.join(v['flags'])} video (kept: it's your pick)")
    else:
        d = search(row["q"])
        p = pick_song(d, SL.IMPORT_MAX_S)
    v, l = p["video"], p["lyrics"]
    out = {"artist": p["artist"], "title": p["title"], "names_from": p.get("names_from"), "notes": notes,
           "video": {k: v.get(k) for k in ("id", "title", "channel", "duration", "flags", "thumb", "official_channel", "link")} if v else None,
           "lyrics": {"source": l["source"], "id": l["id"], "artist": l.get("artist"), "title": l.get("title"), "lines": l.get("lines"),
                      "duration": l.get("duration"), "timed": bool(l.get("timed")), "lang": lyr_lang(l)} if l else None}
    other = re.search(r"\b((?:19|20)\d\d\s+version|re-?recorded|new version|short ver(?:sion)?|tv (?:size|ver(?:sion)?))\b", (v or {}).get("title") or "", re.I)
    if other:   # (the skill's rule: the original, not a re-recording or a short version)
        notes.append(f"the video's title says \u201c{other.group(1)}\u201d: another version of the song?")
    if v and l and l.get("duration") and v.get("duration") and l["duration"] > v["duration"] + 12:
        notes.append(f"the lyrics run {round(l['duration'] - v['duration'])} s longer than the video (a shortened video?)")
    kind, why = p["kind"], p["why"]
    if kind == "lyrics" and d.get("lyrics_failed"):
        why = "couldn't look up the lyrics (LRCLIB didn't answer): try again later"
    elif kind == "long":
        why = f"too long ({_mmss(v['duration'])}; an import makes songs up to {SL.IMPORT_MAX_S // 60} min: tick it to make it anyway)"
    elif kind == "lyrics" and l and not l.get("timed"):
        why = "no timed lyrics found (only plain ones: Change… to make it by hand)"
    if not kind and row["kind"] == "text" and v and v.get("flags"):
        kind, why = "flagged", f"the best video found looks like a {', '.join(v['flags'])} video: Change… to pick another"
    keys = [k for k in ((v or {}).get("id"), (loose(out["artist"]), loose(out["title"])) if out["title"] else None) if k]
    dup = next((seen[k] for k in keys if k in seen), None)
    if dup and kind not in ("video", "lyrics"):
        kind, why = "dup", f"the same song as row {dup}"
    for k in keys:
        seen.setdefault(k, row["n"])
    out.update(status="skipped" if kind else "ready", why=why, tick=not kind and bool(v and l))
    return out


def import_enqueue(iid):
    if _IMP_T["thread"] is None or not _IMP_T["thread"].is_alive():
        _IMP_T["thread"] = threading.Thread(target=_import_worker, daemon=True, name="imports")
        _IMP_T["thread"].start()
    _IMP_Q.put(iid)


def _import_worker():
    while True:
        iid = _IMP_Q.get()
        try:
            import_run(iid)
        except Exception as e:
            log.exception("import %s", iid)
            try:
                with _IMP_LOCK:
                    imp = imp_load(iid)
                    imp.update(state="ready", error=f"the Studio hit an error looking them up ({str(e)[:80]})")
                    for r in imp["rows"]:
                        if r["status"] == "waiting":
                            r.update(status="skipped", why="not looked up (the Studio hit an error)", tick=False)
                    imp_save(imp)
            except Exception:
                pass


def import_list_playlist(lst):
    """A playlist's videos (at most MAX_IMPORT) and how many it has: yt-dlp --flat-playlist (one listing, no video
    pages). -> (title, entries, total)."""
    d = yt_json(["--flat-playlist", "-J", "--playlist-end", str(SL.MAX_IMPORT), "--", SL.playlist_url(lst)], timeout=120)
    entries = [e for e in d.get("entries") or [] if e and e.get("id") and SL.VIDEO_ID.fullmatch(str(e["id"]))]
    return d.get("title") or "", entries[:SL.MAX_IMPORT], max(d.get("playlist_count") or 0, len(entries))


def import_run(iid):
    """Look an import's songs up, one at a time (rows fill in as they're done)."""
    with _IMP_LOCK:
        imp = imp_load(iid)
    if imp["state"] == "listing":
        try:
            title, entries, total = import_list_playlist(imp["playlist"]["id"])
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            log.info("import %s: playlist %s: %s", iid, imp["playlist"]["id"], str(e)[-160:])
            with _IMP_LOCK:
                imp = imp_load(iid)
                imp.update(state="ready", error="couldn't read the playlist (private, removed, or a mix YouTube won't list)")
                imp_save(imp)
            return
        with _IMP_LOCK:
            imp = imp_load(iid)
            if imp["state"] != "listing":
                return
            imp["playlist"]["title"] = title
            imp["left_out"] = max(0, total - len(entries))
            imp["rows"] = [{"n": i + 1, "input": e.get("title") or e["id"], "kind": "video", "id": e["id"], "status": "waiting",
                            "entry": {k: e.get(k) for k in ("title", "channel", "uploader", "duration", "view_count", "availability",
                                                            "channel_is_verified")}} for i, e in enumerate(entries)]
            imp["state"] = "looking"
            if not entries:
                imp.update(state="ready", error="the playlist has no videos")
            imp_save(imp)
    seen = {}
    first = True
    for row in list(imp["rows"]):
        if row["status"] != "waiting":
            for k in ((row.get("video") or {}).get("id"), (loose(row.get("artist")), loose(row.get("title"))) if row.get("title") else None):
                if k:
                    seen.setdefault(k, row["n"])
            continue
        with _IMP_LOCK:
            if imp_load(iid)["state"] != "looking":
                return   # cancelled
        if not first:
            time.sleep(IMPORT_GAP_S)
        first = False
        try:
            got = import_row(row, seen)
        except HTTPException as e:
            got = {"status": "skipped", "why": str(e.detail), "tick": False}
        except Exception as e:
            log.exception("import %s row %s", iid, row["n"])
            got = {"status": "skipped", "why": f"couldn't look it up ({str(e)[:60]})", "tick": False}
        with _IMP_LOCK:
            imp = imp_load(iid)
            if imp["state"] != "looking":
                return
            r = next(x for x in imp["rows"] if x["n"] == row["n"])
            r.update(got)
            r.pop("entry", None)
            imp_save(imp)
    with _IMP_LOCK:
        imp = imp_load(iid)
        if imp["state"] == "looking":
            imp["state"] = "ready"
            imp_save(imp)
    log.info("import %s: looked up %s songs, %s ready", iid, len(imp["rows"]), sum(1 for r in imp["rows"] if r["status"] == "ready"))


def recover_imports():
    """At startup: imports still being looked up carry on."""
    for imp in imp_all():
        if imp.get("state") in ("listing", "looking"):
            import_enqueue(imp["id"])


@app.post("/api/imports")
async def import_create(req: Request):
    """Start an import: {text}: one YouTube playlist link, or lines of "Artist - Title" / video links. Looked up in the
    background; GET /api/imports/{id} shows the rows as they fill in. Nothing is made until /make."""
    check_origin(req)
    b = await req.json()
    text = str(b.get("text") or "").strip()
    if not text or len(text) > 20000:
        raise HTTPException(400, "paste a YouTube playlist link, or songs one per line (Artist - Title, or a video link)")
    kind, lst = SL.import_kind(text)
    iid = "imp-" + uuid.uuid4().hex[:10]
    imp = {"id": iid, "created": time.time(), "by": agents.stamp(agents.actor_of(req)), "kind": kind, "left_out": 0,
           "state": "listing" if kind == "playlist" else "looking", "rows": [], "error": None}
    if kind == "playlist":
        imp["playlist"] = {"id": lst, "title": ""}
    else:
        items, left = SL.parse_list(text)
        if not items:
            raise HTTPException(400, "no songs in that list: one per line, Artist - Title or a YouTube video link")
        imp["left_out"] = left
        imp["rows"] = [{"n": i + 1, "input": x["input"], "kind": x["kind"], **({"q": x["q"]} if x["kind"] == "text" else {}),
                        **({"id": x["id"]} if x["kind"] == "video" else {}),
                        **({"status": "skipped", "why": x["why"], "tick": False} if x["kind"] == "bad" else {"status": "waiting"})}
                       for i, x in enumerate(items)]
    with _IMP_LOCK:
        imp_save(imp)
    log.info("import %s by %s: %s", iid, imp["by"]["by"], f"playlist {lst}" if kind == "playlist" else f"{len(imp['rows'])} lines")
    import_enqueue(iid)
    return imp_view(imp)


@app.get("/api/imports")
def import_list():
    """The imports kept (24 hours), newest first, without their rows."""
    return {"imports": [{k: v for k, v in imp_view(i).items() if k != "rows"} for i in imp_all()]}


@app.get("/api/imports/{iid}")
def import_get(iid: str):
    with _IMP_LOCK:
        return imp_view(imp_load(iid))


@app.post("/api/imports/{iid}/cancel")
async def import_cancel(iid: str, req: Request):
    """Stop looking up and put the import away (songs already made from it stay)."""
    check_origin(req)
    with _IMP_LOCK:
        imp = imp_load(iid)
        imp["state"] = "cancelled"
        imp_save(imp)
    return imp_view(imp)


@app.post("/api/imports/{iid}/make")
async def import_make(iid: str, req: Request):
    """Make the ticked rows: {rows: [{n, artist, title}], gpu}. Each becomes a normal song in the queue (in list order,
    made by whoever pressed it, tagged with the import), with the video and lyrics the lookup chose; a row already made
    is skipped, so pressing twice doesn't make anything twice."""
    check_origin(req)
    b = await req.json()
    rows = b.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > SL.MAX_IMPORT or not all(isinstance(x, dict) and isinstance(x.get("n"), int) for x in rows):
        raise HTTPException(400, "tick the songs to make")
    actor = agents.actor_of(req)
    gpu = b.get("gpu") if b.get("gpu") in ("local", "remote") else "auto"
    made = []
    with _IMP_LOCK:
        imp = imp_load(iid)
        if imp["state"] in ("listing", "looking"):
            raise HTTPException(409, "still looking the songs up: wait until it's done")
        if imp["state"] == "cancelled":
            raise HTTPException(409, "this import was cancelled")
        by_n = {r["n"]: r for r in imp["rows"]}
        todo = []
        for x in rows:
            r = by_n.get(x["n"])
            if not r:
                raise HTTPException(400, f"there's no row {x['n']}")
            if r.get("job"):
                continue
            if not can_make_row(r):
                raise HTTPException(400, f"row {r['n']} has no video or timed lyrics to make (Change… finds them by hand)")
            artist = str(x.get("artist") if x.get("artist") is not None else r.get("artist") or "").strip()[:200]
            title = str(x.get("title") if x.get("title") is not None else r.get("title") or "").strip()[:200]
            if not artist or not title:
                raise HTTPException(400, f"row {r['n']}: give the artist and the title")
            todo.append((r, artist, title))
        for r, artist, title in sorted(todo, key=lambda t: t[0]["n"]):
            v, l = r["video"], r["lyrics"]
            jid = new_job({"yt": v["id"], "lrc_id": l["id"], "lyrics_source": l["source"], "artist": artist, "title": title,
                           "video_title": str(v.get("title") or "")[:200], "gpu_pref": gpu,
                           "import": {"id": iid, "n": r["n"]}}, actor)
            r.update(job=jid, artist=artist, title=title, status="made", tick=False)
            made.append({"n": r["n"], "id": jid})
            log.info("import %s: queued %s (row %s) | video %s | lyrics %s:%s", iid, jid, r["n"], v["id"], l["source"], l["id"])
        sent = {x["n"] for x in rows}
        for r in imp["rows"]:   # the person's untick stays (a reload shows what's left as they left it)
            if r["n"] not in sent and not r.get("job"):
                r["tick"] = False
        if made:
            imp["state"] = "made"
        imp_save(imp)
    if made:
        wake.set()
    return {"made": made, "import": imp_view(imp)}


def import_row_made(iid, n, jid, artist, title):
    """A row made by hand (Change… on the review, then Make on the search): it's done, not made again."""
    with _IMP_LOCK:
        try:
            imp = imp_load(iid)
        except HTTPException:
            return
        r = next((x for x in imp["rows"] if x["n"] == n), None)
        if r:
            r.update(job=jid, artist=artist, title=title, status="made", tick=False, how="by hand")
            imp_save(imp)


# ---- flags: the ⚠ marks ----
_FC = {"at": 0.0, "v": {}}


def flag_counts():
    """job id -> Encore flags not dismissed (cached for 2 s: the Songs list asks for every song)."""
    if time.time() - _FC["at"] > 2:
        _FC.update(at=time.time(), v=STORE.flag_counts())
    return _FC["v"]


def _spans(lines, off):
    return [(min(w[0] for w in l["words"]) + off, max(w[1] for w in l["words"]) + off, " ".join(w[2] for w in l["words"]))
            for l in lines]


def _published_lines(base):
    """[(start, end, text)] of the words file the players have for this base (video time), or None."""
    try:
        d = json.load(open(os.path.join(MV_DIR, base + catalog.WORDS_EXT), encoding="utf-8"))
        return [(l["start"], l["end"], " ".join(w[2] for w in l["words"])) for l in d["lines"]]
    except Exception:
        return None


def remark_line(jid, idx):
    """(under the lock) Line idx's ⚠ mark from the Encore flags on it that aren't dismissed: set, stacked with a
    count, or taken off again (only Encore's part: the studio's own reason and a ks note stay). Timing and words are
    never touched. True if words.json changed."""
    wp = job_path(jid, "words.json")
    lines = load_lines(wp)
    if not 0 <= idx < len(lines):
        return False
    mine = [f for f in STORE.flags(job=jid, open_only=True) if f["mapped"] == idx]
    l = lines[idx]
    old = l.get("why") if l.get("weak") else None
    if mine:
        why = encore.merge_why(old, mine)
        if l.get("weak") and old == why:
            return False
        l.update(weak=True, ok=False, why=why)
    else:
        if not (old or "").startswith(encore.ENCORE_TAG):
            return False
        rest = encore.merge_why(old, [])
        l.update(weak=bool(rest), why=rest or None)
    save_lines(wp, lines)
    return True


def flag_song(b):
    """(status, job id, version name, line index) for a flag (§2.3, §2.8): marked when the studio has the song's job
    (on the line it's about; on the whole song when it gives no time and no line, or when no line fits: the song page
    lists it either way), noted when the song is known (catalog id or base) but its job is gone, unknown_song when
    neither is known. Songs a player found by scanning folders ("f-..." ids) are matched by base."""
    jid = b["id"] if ID_RE.match(b["id"]) and os.path.isfile(os.path.join(JOBS, b["id"], "job.json")) else None
    cv = catalog_view()
    hit = cv["by_base"].get(b["base"])
    if not jid and hit and ID_RE.match(hit[0]["id"]) and os.path.isfile(os.path.join(JOBS, hit[0]["id"], "job.json")):
        jid = hit[0]["id"]
    if not jid:
        if hit or b["id"] in cv["by_id"]:
            return ("noted", None, hit[1] if hit else None, None)
        return ("unknown_song", None, None, None)
    job = load(jid)
    version = None
    if b["base"] != job.get("published_as"):
        version = next((m.get("name") for m in videoswap.entries(job_path(jid)) if m.get("published_as") == b["base"]),
                       hit[1] if hit and hit[0]["id"] == jid else None)
    if b.get("t") is None and b.get("line") is None:
        return ("marked", jid, version, None)
    if job.get("status") != "done" or not os.path.exists(job_path(jid, "words.json")):
        return ("marked", jid, version, None)  # being made again: listed on its page, no line to mark yet
    # a custom-video version plays over the song's own sound and timing (videoswap.py), so its video time is the
    # song's: the same lines, the same offset
    cur = _spans(load_lines(job_path(jid, "words.json")), float(job.get("offset") or 0.0))
    idx = encore.map_line(cur, b.get("t"), b.get("line"), _published_lines(b["base"]))
    return ("marked", jid, version, idx)


# ---- the player API ----
@app.post("/api/encore/pair")
@_guard
async def encore_pair(req: Request):
    if not PAIR_LIMIT.hit(_addr(req)):
        raise EncoreError(429, "Too many tries. Wait a minute, then try again.")
    b = await _body(req, "pair.request.schema.json")
    name = " ".join(b["name"].split())[:60]
    try:
        token = STORE.pair(b["code"], name, b["player"], b["encore"], b["contract"])
    except encore.PairError as e:
        log.warning("players: pairing refused (%s) from %s", "wrong or expired code", _addr(req))
        raise EncoreError(e.status, str(e))
    _FC["at"] = 0
    log.info("players: linked %r (player %s…, Encore %s, contract %s)", name, b["player"][:8], b["encore"][:20], b["contract"][:10])
    out = {"token": token, "studio": encore.VERSION, "contract": encore.CONTRACT, "studio_name": C.STUDIO_NAME,
           "studio_id": STORE.studio_id()}
    if C.STUDIO_PUBLIC_URL:
        out["studio_url"] = C.STUDIO_PUBLIC_URL
    return out


@app.post("/api/encore/unpair")
@_guard
async def encore_unpair(req: Request):
    await _body(req, "unpair.request.schema.json")
    p = req.state.player
    try:
        STORE.unlink(p)
    except encore.PairError as e:
        raise EncoreError(e.status, str(e))
    log.info("players: %s… unlinked itself", p[:8])
    return {"ok": True}


@app.get("/api/encore/hello")
@_guard
def encore_hello(req: Request):
    return {"api": encore.API, "contract": encore.CONTRACT, "studio": encore.VERSION, "catalog_version": catalog.VERSION,
            "studio_id": STORE.studio_id(), "auto_make": automake_offer(req.state.player)}


@app.post("/api/encore/requests")
@_guard
async def encore_request(req: Request):
    b = await _body(req, "requests.post.request.schema.json")
    return apply_request(req.state.player, b)


# ---- the Studio app (PWA): installable, and a notification on each device that asked for one ----
# The VAPID key pair and the devices' push subscriptions live in DATA/push.json (only the Studio reads it); webpush.mjs
# (Node's own crypto) encrypts and sends. People only (agents.ROUTES: ADMIN).
PUSH_FILE = os.path.join(C.DATA, "push.json")
push_lock = threading.Lock()


def _push_load():
    try:
        return json.load(open(PUSH_FILE))
    except (OSError, ValueError):
        return {"vapid": None, "subs": []}


def _push_save(d):
    os.makedirs(os.path.dirname(PUSH_FILE), exist_ok=True)
    with open(PUSH_FILE + ".tmp", "w") as f:
        json.dump(d, f)
    os.chmod(PUSH_FILE + ".tmp", 0o600)
    os.replace(PUSH_FILE + ".tmp", PUSH_FILE)


def _webpush(*args, data=None):
    # NODE_BIN is the folder node is in (config.py puts it on PATH for the tools); else PATH's node
    node = os.path.join(C.NODE_BIN, "node") if C.NODE_BIN and os.path.isdir(C.NODE_BIN) else (shutil.which("node") or "node")
    r = subprocess.run([node, os.path.join(HERE, "webpush.mjs"), *args], input=data, capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout or "{}")


def push_keys():
    with push_lock:
        d = _push_load()
        if not d.get("vapid"):
            d["vapid"] = _webpush("keys")
            _push_save(d)
        return d


def notify(title, body, url="/"):
    """A notification on every subscribed device (in a thread: never slows a request down). Gone ones are dropped."""
    def go():
        d = push_keys()
        gone = []
        for s in list(d.get("subs") or []):
            try:
                r = _webpush("send", data=json.dumps({"sub": s["sub"], "payload": {"title": title, "body": body, "url": url},
                                                       "vapid": d["vapid"], "subject": "https://github.com/alainator/karaoke-studio"}))
            except Exception as e:
                log.warning("push: %s", e); continue
            if r.get("status") in (404, 410):
                gone.append(s["sub"]["endpoint"])
            elif not (200 <= (r.get("status") or 0) < 300):
                log.warning("push to %s: %s %s", s.get("label") or "a device", r.get("status"), r.get("error") or "")
        if gone:
            with push_lock:
                d = _push_load()
                d["subs"] = [s for s in d.get("subs") or [] if s["sub"]["endpoint"] not in gone]
                _push_save(d)
    threading.Thread(target=go, daemon=True, name="push").start()


@app.get("/api/push")
def push_info():
    d = push_keys()
    return {"key": d["vapid"]["public"], "devices": [{"label": s.get("label"), "at": s.get("at")} for s in d.get("subs") or []]}


@app.post("/api/push/subscribe")
async def push_subscribe(req: Request):
    """{"sub": the browser's PushSubscription (endpoint, keys), "label": "Galaxy Tab"}: this device gets notifications."""
    check_origin(req)
    b = await req.json()
    sub = b.get("sub") or {}
    ep, keys = str(sub.get("endpoint") or ""), sub.get("keys") or {}
    if not ep.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "not a push subscription")
    push_keys()
    with push_lock:
        d = _push_load()
        d["subs"] = [s for s in d.get("subs") or [] if s["sub"]["endpoint"] != ep][-19:] + [
            {"sub": {"endpoint": ep, "keys": {"p256dh": str(keys["p256dh"]), "auth": str(keys["auth"])}},
             "label": str(b.get("label") or "a device")[:60], "at": round(time.time())}]
        _push_save(d)
    return {"devices": len(d["subs"])}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(req: Request):
    check_origin(req)
    ep = str((await req.json()).get("endpoint") or "")
    with push_lock:
        d = _push_load()
        d["subs"] = [s for s in d.get("subs") or [] if s["sub"]["endpoint"] != ep]
        _push_save(d)
    return {"devices": len(d["subs"])}


@app.post("/api/push/test")
async def push_test(req: Request):
    check_origin(req)
    notify("Karaoke Studio", "Notifications work on this device.", "/#requests")
    return {"ok": True}


def apply_request(player, b):
    """A song request (§2.2), from the API or a sync bundle (§2.7: those come without auto). Idempotent by rid."""
    old = STORE.get_request(b["rid"])
    if old:
        return json.loads(old["answer"])
    st, jid, sid, base = match_song(b.get("artist") or "", b["title"])
    ans, new = STORE.add_request(player, b, (st, jid, sid, base))
    if new:
        if jid and st in ("ready", "making"):
            link_request(jid, b["rid"])
        elif b.get("auto") and st == "open":  # party auto-make (§2.2b): taken only if the admin allows it for this player
            ans = automake_take(player, b, ans)
        log.info("players: request %s… %r - %r from %s: %s", b["rid"][:8], b.get("artist"), b["title"], b["by"], st)
        song = f"{b.get('artist')} – {b['title']}" if b.get("artist") else b["title"]
        how = {"ready": "already in the library", "making": "being made"}.get(st) or ("making it now (party auto-make)" if (ans or {}).get("status") == "making" else "waiting for you")
        notify(f"🎤 {song}", f"Asked by {b.get('by') or 'a guest'} · {how}", "/#requests")
    return ans


@app.get("/api/encore/requests")
@_guard
def encore_requests(req: Request, rids: str = "", since: str = ""):
    if not rids and since == "":
        raise EncoreError(400, "give rids=a,b,c or since=<unix time>")
    ids = None
    if rids:
        ids = [x for x in rids.split(",") if encore.UUID_RE.match(x.strip().lower())][:200]
    try:
        t = float(since) if since != "" else None
    except ValueError:
        raise EncoreError(400, "since is a unix time")
    refresh_requests()
    rows = STORE.requests(player=req.state.player, rids=ids, since=t)
    if ids is not None:  # in the order asked for
        at = {x: i for i, x in enumerate(ids)}
        rows.sort(key=lambda r: at.get(r["rid"], 1e9))
    else:                # oldest change first: a player can keep the last `updated` as its next `since`
        rows.sort(key=lambda r: r["updated"])
    return {"requests": [STORE.public_request(r) for r in rows]}


@app.post("/api/encore/flags")
@_guard
async def encore_flag(req: Request):
    b = await _body(req, "flags.post.request.schema.json")
    return apply_flag(req.state.player, b)


def apply_flag(player, b):
    """A lyric flag (§2.3), from the API or a sync bundle. Idempotent by fid."""
    old = STORE.get_flag(b["fid"])
    if old:
        return {"fid": b["fid"], "status": old["status"]}
    st, jid, version, idx = flag_song(b)
    with lock:
        if STORE.get_flag(b["fid"]):  # the same flag arrived twice at once
            return {"fid": b["fid"], "status": STORE.get_flag(b["fid"])["status"]}
        STORE.add_flag(player, b, st, job=jid, version=version, mapped=idx)
        _FC["at"] = 0
        if st == "marked" and idx is not None:
            try:
                remark_line(jid, idx)
            except Exception:
                log.exception("players: couldn't mark line %s of %s", idx + 1, jid)
    log.info("players: flag %s… %s on %s%s%s from %s: %s", b["fid"][:8], b["kind"], jid or b["base"][:60],
             f" ({version})" if version else "", f" line {idx + 1}" if idx is not None else "", b["by"], st)
    return {"fid": b["fid"], "status": st}


@app.post("/api/encore/plays")
@_guard
async def encore_plays(req: Request):
    b = await _body(req, "plays.post.request.schema.json")
    return apply_plays(req.state.player, b["plays"])


def apply_plays(player, plays):
    """Plays (§2.4), from the API or a sync bundle. Idempotent by pid."""
    cv = catalog_view()

    def duration_of(sid, base):
        e = (cv["by_base"].get(base) or (None,))[0] or cv["by_id"].get(sid)
        return (e or {}).get("duration")
    before = STORE.q("select count(*) from plays where counted=1")[0][0]
    n = STORE.add_plays(player, plays, duration_of)
    if STORE.q("select count(*) from plays where counted=1")[0][0] != before:
        catalog_soon()
    return {"stored": n}


@app.post("/api/encore/profiles/sync")
@_guard
async def encore_profiles_sync(req: Request):
    """§2.6: store this player's profile events, answer the other players' events since its cursor."""
    try:
        b = await _body(req, "profiles.sync.request.schema.json")
    except EncoreError as e:  # (PROPOSED.md rule 9: an invalid event refuses the whole call with 400)
        if e.status == 422:
            raise EncoreError(400, e.msg)
        raise
    player = req.state.player
    # (the preset player has no uuid of its own here: its events are kept as the preset's)
    if player != encore.PRESET and b["player"] != player:
        raise EncoreError(403, "player isn't this token's player")
    try:
        return await asyncio.to_thread(STORE.profiles_sync, player, b["events"], b["since"])
    except ValueError:
        raise EncoreError(400, "unknown cursor: sync again with since null")


@app.get("/api/encore/profiles/{uid}/export")
@_guard
def encore_profile_export(uid: str, req: Request):
    """§2.6: everything the Studio keeps about one profile (its PIN only as set / not set)."""
    if not encore.UUID_RE.match(uid):
        raise EncoreError(404, "no such profile")
    x = STORE.profile_export(uid)
    if x is None:
        raise EncoreError(404, "no such profile")
    return x


@app.put("/api/encore/profiles/{uid}/photo/{sha}")
@_guard
async def encore_profile_photo_put(uid: str, sha: str, req: Request):
    """§2.6 photo: a profile picture's bytes (a JPEG named by its sha256), before the photo event that points to it."""
    if not encore.UUID_RE.match(uid) or not encore.SHA_RE.match(sha):
        raise EncoreError(404, "no such picture")
    data = await req.body()
    r = await asyncio.to_thread(STORE.photo_put, uid, sha, data)
    if r == "big":
        raise EncoreError(413, f"a profile picture is at most {encore.PHOTO_MAX // 1024} KB")
    if r == "bad":
        raise EncoreError(400, "not a JPEG, or not the picture that sha256 names")
    if r == "deleted":
        raise EncoreError(404, "that profile was deleted")
    return {"stored": True}


@app.get("/api/encore/profiles/{uid}/photo/{sha}")
@_guard
def encore_profile_photo_get(uid: str, sha: str, req: Request):
    """§2.6 photo: a profile picture another player uploaded (404 until it's here, or once the profile is deleted)."""
    f = STORE.photo_path(uid, sha) if encore.UUID_RE.match(uid) and encore.SHA_RE.match(sha) else None
    if not f:
        raise EncoreError(404, "no such picture")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})


@app.get("/api/encore/unpublished")
@_guard
def encore_unpublished(req: Request):
    return unpublished_view()


def unpublished_view():
    songs = [{"job": j["id"], "artist": j["artist"], "title": j["title"], "made": j.get("finished") or j.get("created") or 0}
             for j in job_index() if j["status"] == "done" and not j.get("published")]
    songs.sort(key=lambda s: -s["made"])
    return {"songs": songs, "studio_url": C.STUDIO_PUBLIC_URL or None}


# ---- §2.9 words scoring (wordscore.py): the singer's recording against the song's words ----
WORDS_LIMIT = encore.RateLimit(6, 60)          # recordings per player and minute


def words_device():
    """Where words scoring may run now: the GPU only when no song is being made here or waits for a card, and the card
    keeps room for a song beside the aligner; else the CPU (unless KARAOKE_WORDS_CPU=0); else None (busy)."""
    try:
        if C.USE_LOCAL_GPU and slots["local"] is None and not any(j["status"] == "queued" for j in job_index()):
            if gpu_free_mib() - wordscore.GPU_MIB >= max(NEED_GPU_MIB, wordscore.GPU_MIB):
                return pitchmod._gpu_device()
    except Exception:
        pass
    return "cpu" if wordscore.CPU_OK else None


def words_song(sid, base):
    """What words scoring needs about a published song or version (wordscore.Scorer), or None. Versions share the
    song's sound and timing: the song's vocals and video are the references."""
    e, _ = catalog_view()["by_base"].get(base) or (None, None)
    if not e or (sid != e["id"] and not sid.startswith("f-")):  # (scanned songs, "f-…": matched by base, §2.8.3)
        return None
    folder = os.path.join(LIB, *(e.get("folder") or "").split("/"))
    try:  # (the version's own words file: the song's timing, its base)
        doc = json.load(open(os.path.join(folder, base + ".words.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return None
    jdir = os.path.join(JOBS, e["id"]) if ID_RE.match(e["id"]) and os.path.isdir(os.path.join(JOBS, e["id"])) else None
    # the published vocals (on the published video's timeline, like its words), else the job's (a trim or remake since
    # publishing moves those)
    voc = os.path.join(folder, e["vocals"]) if e.get("vocals") else None
    if not (voc and os.path.isfile(voc)):
        voc = os.path.join(jdir, "vocals.m4a") if jdir else None
    return {"words": doc, "duration": e.get("duration"), "vocals": voc, "music": os.path.join(folder, e["base"] + ".mp4"),
            "ref_dir": jdir}


WORDS = wordscore.Scorer(words_song, words_device, log)


def _num_header(req, name, lo, hi, label):
    v = (req.headers.get(name) or "").strip()
    try:
        x = float(v)
    except ValueError:
        x = float("nan")
    if not math.isfinite(x) or not lo <= x <= hi:
        raise EncoreError(422, f"{label}: a number from {lo:g} to {hi:g}")
    return x


async def _audio_body(req):
    """The recording (at most 8 MB, counted as it arrives: a missing or wrong Content-Length doesn't get past it)."""
    buf = bytearray()
    async for chunk in req.stream():
        buf += chunk
        if len(buf) > wordscore.MAX_BYTES:
            raise EncoreError(413, "the recording is too big (8 MB at most)")
    return bytes(buf)


@app.post("/api/encore/score-words")
@_guard
async def encore_score_words(req: Request):
    """§2.9: score the words of one recording (the body: Opus audio). The headers are checked before the body is read."""
    player = req.state.player
    if not WORDS_LIMIT.hit(player):
        raise EncoreError(429, "too many recordings; wait a minute")
    if WORDS.pending_of(player) >= wordscore.PENDING_PER_PLAYER:
        raise EncoreError(429, "this player's earlier recordings are still being scored")
    fmt = wordscore.media_format(req.headers.get("content-type"))
    if not fmt:
        raise EncoreError(422, "Content-Type: audio/ogg or audio/webm (Opus)")
    sid, base = (req.headers.get("x-encore-song") or "").strip(), (req.headers.get("x-encore-base") or "").strip()
    if not sid or len(sid) > 80 or not base or len(base) > 300:
        raise EncoreError(422, "X-Encore-Song and X-Encore-Base: the song id and the version played")
    start = _num_header(req, "x-encore-start", -60.0, 24 * 3600.0, "X-Encore-Start")
    rate = _num_header(req, "x-encore-rate", 0.5, 2.0, "X-Encore-Rate")
    try:
        lines = wordscore.parse_lines(req.headers.get("x-encore-lines"))
    except ValueError as e:
        raise EncoreError(422, str(e))
    s = await asyncio.to_thread(words_song, sid, base)
    if not s:
        raise EncoreError(422, "unknown song (not in the Studio's catalog, or without words)")
    if lines is not None:
        known = {l.get("i") for l in s["words"].get("lines") or []}
        lines = [i for i in lines if i in known]
        if not lines:
            raise EncoreError(422, "none of X-Encore-Lines are lines of this song")
    if not wordscore.ENABLED or await asyncio.to_thread(WORDS.pick_device) is None:  # busy: the recording isn't read
        jid = WORDS.answer_busy(player, sid, base)
        log.info("players: words scoring %s… for %s from %s…: busy", jid[:8], base[:80], player[:8])
        return JSONResponse({"job": jid, "eta": 0, "status": "busy"}, status_code=202)
    raw = await _audio_body(req)
    if not raw:
        raise EncoreError(422, "no recording in the body")
    jid, eta, status = await asyncio.to_thread(WORDS.submit, player, raw, fmt, sid, base, start, rate, lines, s.get("duration"))
    raw = None
    log.info("players: words scoring %s… for %s from %s…: %s", jid[:8], base[:80], player[:8], status)
    return JSONResponse({"job": jid, "eta": eta, "status": status}, status_code=202)


@app.get("/api/encore/score-words/{job}")
@_guard
def encore_score_words_get(job: str, req: Request):
    """§2.9: a scoring job's state; only the player that sent it sees it. Unknown or forgotten (after 10 minutes): 410."""
    x = WORDS.get(req.state.player, job) if re.fullmatch(r"[0-9a-f]{32}", job) else None
    if x is None:
        raise EncoreError(410, "no such scoring job (answers are kept 10 minutes)")
    return x


def _etag_match(req, etag):
    inm = req.headers.get("if-none-match")
    if not inm:
        return False
    tags = [x.strip() for x in inm.split(",")]
    return "*" in tags or etag in tags or ("W/" + etag) in tags


@app.get("/api/encore/catalog")
@_guard
def encore_catalog(req: Request):
    p = os.path.join(LIB, catalog.CATALOG)
    if not os.path.exists(p):
        update_catalog()
    try:
        raw = open(p, "rb").read()
    except OSError:
        raise EncoreError(503, "the Studio has no catalog yet")
    etag = '"' + hashlib.sha256(raw).hexdigest()[:24] + '"'
    h = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_match(req, etag):
        return Response(status_code=304, headers=h)
    return Response(raw, media_type="application/json", headers=h)


TYPES = {".mp4": "video/mp4", ".m4a": "audio/mp4", ".ass": "text/plain; charset=utf-8", ".json": "application/json",
         ".jpg": "image/jpeg", ".png": "image/png"}   # (album covers)


def file_etag(path, st):
    """A library file's §2.0b ETag (also what sync bundles carry, so a player's next network sync agrees)."""
    return '"' + hashlib.sha256(f"{path}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:24] + '"'


@app.get("/api/encore/files/{path:path}")
@_guard
def encore_file(path: str, req: Request):
    """A library file the catalog names (exact path, '/'-separated, relative to the library root); Range and ETag."""
    if path not in catalog_view()["files"]:
        raise EncoreError(404, "not found")
    root = os.path.realpath(LIB)
    full = os.path.realpath(os.path.join(root, *path.split("/")))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        raise EncoreError(404, "not found")
    st = os.stat(full)
    etag = file_etag(path, st)
    h = {"ETag": etag, "Cache-Control": "no-cache", "Accept-Ranges": "bytes"}
    if _etag_match(req, etag):
        return Response(status_code=304, headers=h)
    ext = ".json" if full.endswith(".json") else os.path.splitext(full)[1].lower()
    return FileResponse(full, media_type=TYPES.get(ext, "application/octet-stream"), headers=h, stat_result=st)


# ---- the admin pages (the web app, behind its own login) ----
@app.get("/api/profiles/{uid}/photo")
def profile_photo(uid: str):
    """The admin page: a profile's picture now (Players tab)."""
    st = STORE.profile_state(uid) if encore.UUID_RE.match(uid) else None
    f = STORE.photo_path(uid, st["photo"]["sha"]) if st and st.get("photo") else None
    if not f:
        raise HTTPException(404, "no picture")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.get("/api/profiles")
def profiles_list():
    """Encore user profiles backed up here (§2.6): names, colours, last activity, which players. No history."""
    return {"profiles": STORE.profiles()}


@app.get("/api/players")
def players_list():
    return {"players": STORE.players(), "code": STORE.code_state(), "address": C.ENCORE_STUDIO_ADDRESS,
            "address_lan": C.ENCORE_STUDIO_ADDRESS_LAN if C.ENCORE_STUDIO_ADDRESS_LAN != C.ENCORE_STUDIO_ADDRESS else None,
            "studio_name": C.STUDIO_NAME, "contract": encore.CONTRACT, "studio": encore.VERSION,
            "preset": bool(C.ENCORE_TOKEN)}


# ---- sync bundles (§2.7, bundle.py): Players -> Export for <player> / Import a bundle. Admin only (the web app);
# the players-only network never reaches these (ENCORE_ONLY_NETS: /api/encore/ only). ----
HASHES = bundle.Hashes(os.path.join(C.DATA, "bundle-hashes.db"))
EXPORTS = {}            # player -> {"state": hashing|ready|failed, "done", "total", "plan", "error", "at"}
PART_GB_MIN = 0.5


def _export_answers(player):
    rows = STORE.requests(player=player, since=time.time() - bundle.KEEP_DAYS * 86400)
    rows.sort(key=lambda r: r["updated"])
    return {"requests": [STORE.public_request(r) for r in rows], "unpublished": unpublished_view(),
            "applied": STORE.applied_ids(player, bundle.KEEP_DAYS)}


@app.post("/api/players/{player}/export")
async def players_export_prepare(player: str, req: Request):
    """{everything: bool, part_gb: number or null}: work out the bundle (hashing new library files first; a job)."""
    check_origin(req)
    if not STORE.key_hash(player):
        raise HTTPException(404, "no such linked player")
    b = await req.json()
    everything = bool(b.get("everything"))
    pg = b.get("part_gb")
    part_bytes = None
    if pg not in (None, "", 0):
        try:
            pg = float(pg)
        except (TypeError, ValueError):
            raise HTTPException(400, "part_gb is a number")
        if not PART_GB_MIN <= pg <= 1000:
            raise HTTPException(400, f"parts are at least {PART_GB_MIN} GB")
        part_bytes = int(pg * 1e9)
    cur = EXPORTS.get(player)
    if cur and cur["state"] == "hashing":
        return players_export_view(player)
    job = {"state": "hashing", "done": 0, "total": 0, "plan": None, "error": None, "at": time.time()}
    EXPORTS[player] = job

    def work():
        try:
            if not os.path.exists(os.path.join(LIB, catalog.CATALOG)):
                update_catalog()
            cat_bytes = open(os.path.join(LIB, catalog.CATALOG), "rb").read()
            prog = lambda d, t: job.update(done=d, total=t)
            job["plan"] = bundle.plan(player, STORE.studio_id(), C.STUDIO_NAME or "Karaoke Studio", LIB, cat_bytes,
                                      _export_answers(player), catalog.files_of, file_etag, HASHES,
                                      have=None if everything else STORE.get_have(player), everything=everything,
                                      part_bytes=part_bytes, progress=prog)
            job["state"] = "ready"
            v = job["plan"].view()
            log.info("players: bundle for %s ready: %d song(s) in %d part(s)", player[:8], v["songs"], len(v["parts"]))
        except Exception as e:
            job.update(state="failed", error=str(e) if isinstance(e, bundle.BundleError) else "the Studio hit an error; it's in its log")
            log.exception("players: bundle for %s failed", player[:8])
    threading.Thread(target=work, daemon=True).start()
    return players_export_view(player)


@app.get("/api/players/{player}/export")
def players_export_view(player: str):
    job = EXPORTS.get(player)
    if not job:
        return {"state": None}
    out = {k: job[k] for k in ("state", "done", "total", "error", "at")}
    if job["plan"]:
        k = bundle.key(STORE.key_hash(player) or "00")
        out |= job["plan"].view()
        for p in out["parts"]:
            p["bytes"] = job["plan"].size(p["part"] - 1, k)
    return out


@app.get("/api/players/{player}/export/{part}")
def players_export_download(player: str, part: int):
    """One part of the prepared bundle, as a download with its exact length."""
    job = EXPORTS.get(player)
    kh = STORE.key_hash(player)
    if not job or job["state"] != "ready" or not kh:
        raise HTTPException(409, "prepare the export first")
    plan = job["plan"]
    if not 1 <= part <= len(plan.parts):
        raise HTTPException(404, "no such part")
    k = bundle.key(kh)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (STORE.player_names().get(player) or "player"))[:40].strip("-") or "player"
    n = len(plan.parts)
    fname = f"{name}-{time.strftime('%Y%m%d', time.localtime(plan.made))}{f'-part-{part}-of-{n}' if n > 1 else ''}.karaoke-sync.tar"
    log.info("players: bundle part %d/%d for %s downloading (%d bytes)", part, n, player[:8], plan.size(part - 1, k))
    return StreamingResponse(plan.stream(part - 1, k), media_type="application/x-tar",
                             headers={"Content-Length": str(plan.size(part - 1, k)),
                                      "Content-Disposition": f'attachment; filename="{fname}"', "Cache-Control": "no-store"})


@app.post("/api/players/import")
async def players_import(req: Request):
    """A player's bundle (the raw tar as the body): its requests, flags and plays, as if it had sent them."""
    check_origin(req)
    try:
        if int(req.headers.get("content-length") or 0) > bundle.UPLOAD_MAX:
            raise HTTPException(413, "that's far too big for a player's bundle")
    except ValueError:
        raise HTTPException(400, "bad Content-Length")
    data = await req.body()

    def key_for(p):
        kh = STORE.key_hash(p) if isinstance(p, str) and (p == encore.PRESET or encore.UUID_RE.match(p)) else None
        return bundle.key(kh) if kh else None
    try:
        player, m, calls = bundle.read_player(data, key_for, STORE.studio_id(), schemalite.errors)
    except bundle.BundleError as e:
        log.info("players: a bundle was refused: %s", e)
        raise HTTPException(400, str(e))
    bad = []
    for kind, schema in (("requests", "requests.post.request.schema.json"), ("flags", "flags.post.request.schema.json")):
        for x in calls[kind]:
            bad += schemalite.errors(schema, x)[:1]
    if calls["plays"]:   # (the API's schema wants at least one: a bundle may have none)
        bad += schemalite.errors("plays.post.request.schema.json", {"plays": calls["plays"]})[:1]
    if bad:
        raise HTTPException(400, "the bundle's items aren't right: " + "; ".join(bad[:3]))
    got = {"requests": 0, "flags": 0, "plays": 0}
    for b in calls["requests"]:
        b = {k: v for k, v in b.items() if k not in ("auto", "queue")}   # §2.7: never made by itself from a stick
        apply_request(player, b)
        got["requests"] += 1
    for b in calls["flags"]:
        apply_flag(player, b)
        got["flags"] += 1
    for i in range(0, len(calls["plays"]), 200):
        got["plays"] += apply_plays(player, calls["plays"][i:i + 200])["stored"]
    STORE.set_have(player, calls["have"])
    STORE.seen(player)
    log.info("players: bundle from %s imported: %d request(s), %d flag(s), %d play(s)", player[:8], got["requests"],
             got["flags"], len(calls["plays"]))
    return {"player": player, "player_name": STORE.player_names().get(player) or m.get("player_name"), "made": m["made"],
            "requests": got["requests"], "flags": got["flags"], "plays": len(calls["plays"]), "have": len(calls["have"])}


@app.post("/api/players/code")
async def players_code(req: Request):
    """A new one-time pairing code (shown only here, never logged); the previous one stops working."""
    check_origin(req)
    code, expires = STORE.new_code()
    log.info("players: a pairing code was made (valid 10 minutes)")
    return {"code": code, "expires": expires, "address": C.ENCORE_STUDIO_ADDRESS,
            "address_lan": C.ENCORE_STUDIO_ADDRESS_LAN if C.ENCORE_STUDIO_ADDRESS_LAN != C.ENCORE_STUDIO_ADDRESS else None}


@app.delete("/api/players/code")
def players_code_cancel(req: Request):
    check_origin(req)
    STORE.cancel_code()
    return {"ok": True}


@app.post("/api/players/{player}/automake")
async def players_automake(player: str, req: Request):
    """The admin's "may auto-make" for one linked player (party auto-make: also needs the Studio-wide setting)."""
    check_origin(req)
    on = (await req.json()).get("on")
    if not isinstance(on, bool):
        raise HTTPException(400, "on: true or false")
    if not STORE.set_automake(player, on):
        raise HTTPException(404, "no such player")
    log.info("players: %s… may auto-make: %s (by %s)", player[:8], "yes" if on else "no", agents.actor_of(req)["name"])
    return {"ok": True, "automake": on}


@app.get("/api/automake")
def automake_info():
    """Party auto-make: the settings, how many it made in the last hour and day, the last requests it took."""
    now = time.time()
    hour, day = automake_counts(now)
    names = STORE.player_names()
    recent = [{"rid": r["rid"], "title": r["title"], "artist": r["artist"], "by": r["by_name"], "party": r["party"],
               "player": names.get(r["player"]) or "a player", "status": r["status"], "stage": r["stage"], "reason": r["reason"],
               "job": r["job"], "created": r["created"]}
              for r in STORE.q("select * from requests where am='taken' order by created desc limit 15")]
    wait, eta = new_song_eta()
    secs = make_seconds(job_index())
    return {"settings": AM.load(STORE), "defaults": AM.DEFAULTS, "limits": AM.LIMITS, "hour": hour, "day": day,
            "recent": recent, "minutes": max(1, math.ceil((eta - now) / 60)) if eta else None,
            "make_seconds": secs if C.GPU_HELPER_URL else {"local": secs["local"]}}


@app.post("/api/automake")
async def automake_settings(req: Request):
    check_origin(req)
    try:
        cfg = AM.save(STORE, await req.json())
    except AM.SettingError as e:
        raise HTTPException(400, str(e))
    log.info("auto-make settings: %s (by %s)", json.dumps(cfg), agents.actor_of(req)["name"])
    return automake_info()


@app.delete("/api/players/{player}")
def players_unlink(player: str, req: Request):
    check_origin(req)
    try:
        ok = STORE.unlink(player)
    except encore.PairError as e:
        raise HTTPException(e.status, str(e))
    if not ok:
        raise HTTPException(404, "no such player")
    log.info("players: unlinked %s… from the studio", player[:8])
    return {"ok": True}


def _request_row(r, names, jobs):
    j = jobs.get(r["job"]) if r["job"] else None
    if not j and r["song_id"]:
        j = jobs.get(r["song_id"])
    return {"rid": r["rid"], "artist": r["artist"], "title": r["title"], "note": r["note"], "by": r["by_name"],
            "party": r["party"], "at": r["at"], "status": r["status"], "reason": r["reason"], "job": r["job"],
            "song_id": r["song_id"], "base": r["base"], "updated": r["updated"], "created": r["created"],
            "auto": bool(r["auto"]), "automake": bool(r.get("am")), "stage": r.get("stage"), "eta": r.get("eta"),
            "player": names.get(r["player"]) or ("Preset player" if r["player"] == encore.PRESET else "an unlinked player"),
            "song": {"id": j["id"], "artist": j["artist"], "title": j["title"], "status": j["status"],
                     "published": bool(j.get("published"))} if j else None}


@app.get("/api/requests")
def requests_list():
    refresh_requests()
    names = STORE.player_names()
    jobs = {j["id"]: j for j in job_index()}
    rows = [_request_row(r, names, jobs) for r in STORE.requests()]
    return {"requests": rows, "open": sum(1 for r in rows if r["status"] == "open"),
            "flags": [dict(f, by=f["by_name"]) for f in STORE.flags(unmatched=True, open_only=True)]}


@app.post("/api/requests/{rid}/{action}")
async def request_action(rid: str, action: str, req: Request):
    """decline {reason} | have {job} (the studio has it: that song) | reopen."""
    check_origin(req)
    r = STORE.get_request(rid)
    if not r:
        raise HTTPException(404, "no such request")
    b = await req.json() if (req.headers.get("content-length") or "0") != "0" else {}
    if action == "decline":
        reason = " ".join(str(b.get("reason") or "").split())[:120]
        if not reason:
            raise HTTPException(400, "say why, in a few words (the guest sees it)")
        unlink_request(rid)
        STORE.set_request(rid, status="declined", reason=reason, job=None, song_id=None, base=None)
    elif action == "have":
        jid = str(b.get("job") or "")
        job = load(jid)
        st = _job_status({**job, "id": jid})
        if not st:
            raise HTTPException(400, "that song failed or was cancelled; pick a made one")
        unlink_request(rid)
        link_request(jid, rid)
        if st[0] == "published":
            STORE.set_request(rid, status="have", job=jid, song_id=jid, base=job["published_as"], reason=None)
        else:
            STORE.set_request(rid, status=st[0], job=jid, song_id=None, base=None, reason=None)
    elif action == "reopen":  # (also "Not this song"): waits again, and isn't matched to a song by its name any more
        unlink_request(rid)
        STORE.set_request(rid, status="open", reason=None, job=None, song_id=None, base=None, detached=1, am=None, stage=None, eta=None)
    else:
        raise HTTPException(404, "unknown action")
    log.info("players: request %s… %s", rid[:8], action)
    return {"ok": True, "status": STORE.get_request(rid)["status"]}


@app.get("/api/jobs/{jid}/encore-flags")
def job_flags(jid: str):
    job_path(jid)
    names = STORE.player_names()
    return {"flags": [dict(f, by=f["by_name"], line_no=f["mapped"] + 1 if f["mapped"] is not None else None,
                           player=names.get(f["player"]) or f["player"][:8]) for f in STORE.flags(job=jid)]}


@app.post("/api/jobs/{jid}/encore-flags/dismiss")
async def job_flags_dismiss(jid: str, req: Request):
    """Done with these flags (or all of the song's): they stop counting, and their part of a line's ⚠ note goes."""
    check_origin(req)
    b = await req.json()
    fids = [str(x) for x in (b.get("fids") or [])]
    with lock:
        job_path(jid)
        lines = {f["mapped"] for f in STORE.flags(job=jid, open_only=True) if f["mapped"] is not None and (not fids or f["fid"] in fids)}
        done = STORE.dismiss_flags(fids=fids) if fids else STORE.dismiss_flags(job=jid)
        _FC["at"] = 0
        if os.path.exists(job_path(jid, "words.json")):
            for i in sorted(lines):
                remark_line(jid, i)
    return {"dismissed": len(done)}


@app.post("/api/flags/dismiss")
async def other_flags_dismiss(req: Request):
    """Flags on songs the studio doesn't have (any more): done with them."""
    check_origin(req)
    b = await req.json()
    fids = [str(x) for x in (b.get("fids") or [])]
    return {"dismissed": len(STORE.dismiss_flags(fids=fids))}


# ---- agents: keys (admins, in the web app) and what ks asks for ----
@app.get("/api/agents")
def agents_list():
    return {"keys": KEYS.list(), "scopes": [{"scope": k, "label": t, "default": d} for k, t, d in agents.SCOPES],
            "required": C.AGENT_KEYS_REQUIRED, "person_header": C.PERSON_HEADER}


@app.post("/api/agents")
async def agents_create(req: Request):
    """A new agent key: {"name", "scopes"}. The key is in this answer only; the studio keeps its hash."""
    check_origin(req)
    b = await req.json()
    try:
        key, row = KEYS.create(b.get("name"), b.get("scopes"), by=agents.actor_of(req)["name"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    log.info("agents: a key was made for %s (%s)", row["name"], ", ".join(row["scopes"]))
    return {"key": key, "agent": row}


@app.patch("/api/agents/{kid}")
async def agents_update(kid: str, req: Request):
    check_origin(req)
    b = await req.json()
    try:
        row = KEYS.update(kid, b.get("scopes"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not row:
        raise HTTPException(404, "no such agent key")
    log.info("agents: %s may now: %s", row["name"], ", ".join(row["scopes"]) or "nothing")
    return {"agent": row}


@app.delete("/api/agents/{kid}")
def agents_revoke(kid: str, req: Request):
    check_origin(req)
    row = KEYS.get(kid)
    if not row or not KEYS.revoke(kid):
        raise HTTPException(404, "no such agent key")
    log.info("agents: the key of %s was revoked", row["name"])
    return {"ok": True}


@app.get("/api/agent/whoami")
def agent_whoami(req: Request):
    """Who the studio takes the caller for, and (for an agent) what its key allows. Never the key."""
    a = agents.actor_of(req)
    allowed = a.get("scopes") if a["kind"] == "agent" else agents.SCOPE_NAMES
    return {"kind": a["kind"], "name": a["name"], "scopes": allowed, "studio": C.STUDIO_NAME,
            "labels": {k: t for k, t, _ in agents.SCOPES}, "required": C.AGENT_KEYS_REQUIRED}


def _ks():
    """tools/ks_helper.py (loaded by its path: "tools" is too common a name to import as a package)."""
    m = sys.modules.get("ks_helper")
    if m is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("ks_helper", os.path.join(HERE, "tools", "ks_helper.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        sys.modules["ks_helper"] = m
    return m


def _done(jid):
    job = load(jid)
    if job.get("status") != "done":
        raise HTTPException(400, "finish making it first")
    return job


@app.get("/api/jobs/{jid}/heard")
def job_heard(jid: str):
    """What the transcription heard in the song's vocals (heard.json: {"lang", "words": [[start, end, word]...]}, video
    time without the offset), for ks heard."""
    p = job_path(jid, "heard.json")
    if not os.path.exists(p):
        raise HTTPException(404, "this song has no record of what was heard (made before the by-ear check, or on a "
                                 "helper on older code); deploy/check-library.py --missing-heard makes one")
    return FileResponse(p, media_type="application/json", headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/{jid}/agent/check")
async def agent_check(jid: str):
    """The by-ear check of the lines as they are now: for each lead line, how far from it its words are heard."""
    job = _done(jid)
    return await asyncio.to_thread(_ks().check, job_path(jid), float(job.get("offset") or 0.0))


@app.get("/api/jobs/{jid}/agent/look")
async def agent_look(jid: str, t0: float, t1: float, line: int = 0, align: int = 0):
    """A picture (PNG) of [t0, t1] (video time): the vocals' spectrogram, the melody (pitch.f0.npz, tracked now when
    the song has none: _look_melody; X-Melody says which), the line blocks (orange = ⚠; line = the 1-based line drawn in white), with align=1 where the aligner puts
    that line's words (heard together with the line before: agent/align), and the words heard. X-Rows: the rows drawn."""
    import tempfile
    job = _done(jid)
    if not (0 <= t0 < t1 and t1 - t0 <= 120):
        raise HTTPException(400, "t0 < t1, at most 120 s apart")
    cache, png = job_path(jid, "spectro.webp"), job_path(jid, "spectro.png")
    if not os.path.exists(cache) and not os.path.exists(png):
        await asyncio.to_thread(_make_spectro, jid, cache)
    melody = await asyncio.to_thread(_look_melody, jid)
    aligned, note = None, ""
    if align and line > 0:
        try:
            r = await asyncio.to_thread(_run_align, jid, job, line - 1, None, None)
            aligned = next((x for x in r["lines"] if x["line"] == line), None)
            prev = next((x for x in r["lines"] if r.get("prev") is not None and x["line"] == r["prev"] + 1), None)
            if aligned and prev:   # the boundary: where the aligner ends the line before (drawn with this line's start)
                aligned = dict(aligned, prev_line=prev["line"], prev_end=prev.get("end"))
        except HTTPException as e:
            note = "busy" if e.status_code == 503 else str(e.detail)[:120]
    fd, out = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        r = await asyncio.to_thread(_ks().look, job_path(jid), float(job.get("offset") or 0.0), t0, t1, line - 1, out,
                                    aligned)
        data = open(out, "rb").read()
    finally:
        os.remove(out)
    return Response(data, media_type="image/png", headers={
        "Cache-Control": "no-store", "X-Lines": ",".join(map(str, r["lines"])), "X-Rows": ",".join(r.get("rows") or []),
        "X-Align": note.encode("ascii", "replace").decode() if note else ("ok" if aligned else ""),
        "X-Melody": melody.encode("ascii", "replace").decode()})


LOOK_PITCH_LOCK = threading.Lock()   # one melody tracked for a look at a time


def _look_melody(jid):
    """The melody row's pitch file (pitch.f0.npz) for a look: "ok" when the song has an up-to-date one; else it's
    tracked now (CREPE in its own process: a few seconds on the GPU when it has room, about 20 s on the CPU) and cached
    for every later look and for publishing: "made". Songs published before pitch files existed, or whose publish
    queue was lost to a restart, have none. Never while a song is being made ("busy": the picture comes without it)."""
    d = job_path(jid)
    if _ks().melody(d) is not None:
        return "ok"
    if not os.path.exists(job_path(jid, "vocals.m4a")):
        return "no vocals"
    if not LOOK_PITCH_LOCK.acquire(timeout=300):   # another look is tracking (maybe this song): wait for it
        return "busy"
    try:
        if _ks().melody(d) is not None:
            return "ok"
        dev = align_device()
        if dev is None:
            return "busy"
        pitchmod.frames(d, timeout=240, log=log, device=dev)
        return "made" if _ks().melody(d) is not None else "failed"
    except Exception as e:
        log.warning("look: no melody for %s (%s)", jid, str(e)[:200])
        return "failed"
    finally:
        LOOK_PITCH_LOCK.release()


# ---- agent/align: one line heard together with the line before it (tools/ks_helper.py align_plan, align) ----
ALIGN_LOCK = threading.Lock()   # one at a time
ALIGN_GPU_MIB = wordscore.GPU_MIB


def align_device():
    """Where an agent's align may run now: never while a song is being made here or waits for the card (None = busy);
    the GPU when it has room, else the CPU (measured: about 3 s for a line either way, most of it starting the process)."""
    if slots["local"] is not None or any(j["status"] in ("queued", "running") for j in job_index()):
        return None
    try:
        if C.USE_LOCAL_GPU and gpu_free_mib() >= ALIGN_GPU_MIB:
            return pitchmod._gpu_device()
    except Exception:
        pass
    return "cpu"


def _run_align(jid, job, n, t0, t1):
    """Align a line (0-based n, with the line before) or every lead line in [t0, t1]; HTTPException 503 when busy."""
    K = _ks()
    lines = load_lines(job_path(jid, "words.json"))
    off = float(job.get("offset") or 0.0)
    try:
        plan = K.align_plan(lines, off, n=n, t0=t0, t1=t1, duration=(job.get("result") or {}).get("duration"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ALIGN_LOCK.acquire(blocking=False):
        raise HTTPException(503, "busy, try in a moment (another align is running)")
    try:
        dev = align_device()
        if dev is None:
            raise HTTPException(503, "busy, try in a moment (a song is being made)")
        r = subprocess.run([sys.executable, "-I", os.path.join(HERE, "tools", "ks_helper.py"), "align", job_path(jid),
                            str(off), dev, json.dumps(plan)], capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "the aligner took too long")
    finally:
        ALIGN_LOCK.release()
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        log.warning("agent align %s failed: %s", jid, (r.stderr or "").strip()[-300:])
        raise HTTPException(500, "the aligner failed: " + ((r.stderr or "").strip().splitlines() or ["no output"])[-1][:200])
    if out.get("error"):
        raise HTTPException(400, out["error"])
    return out


@app.get("/api/jobs/{jid}/agent/align")
async def agent_align(jid: str, line: int = 0, t0: float = -1.0, t1: float = -1.0):
    """Where the forced aligner (MMS_FA, as the pipeline) puts a line's words when it hears them together with the
    line before it, over [previous line's start - 0.3, next line's start] (line: 1-based), or every lead line inside
    [t0, t1] (video time, at most 60 s). Per line: its start now and the aligner's, the move, the line's confidence
    and each word's. Nothing is saved. 503 "busy" while a song is being made or another align runs."""
    job = _done(jid)
    if line > 0:
        return await asyncio.to_thread(_run_align, jid, job, line - 1, None, None)
    if t0 < 0 or t1 <= t0:
        raise HTTPException(400, "give line=N, or t0 and t1 (video time)")
    return await asyncio.to_thread(_run_align, jid, job, None, t0, t1)


@app.get("/api/jobs/{jid}/agent/frame")
async def agent_frame(jid: str, t: float, width: int = 960):
    """The video at t seconds with the lyrics drawn as players show them (PNG)."""
    import tempfile
    _done(jid)
    if not 0 <= t <= 7200 or not 160 <= width <= 1920:
        raise HTTPException(400, "t in seconds of the video; width 160-1920")
    jd = job_path(jid)
    ass = os.path.join(jd, "lyrics.ass").replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    fonts = os.path.join(HERE, "static", "octopus").replace(":", "\\:")
    fd, out = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        r = await asyncio.to_thread(subprocess.run, ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-copyts", "-i",
                                    os.path.join(jd, "out.mp4"), "-vf", f"subtitles='{ass}':fontsdir='{fonts}',scale={width}:-2",
                                    "-frames:v", "1", out], capture_output=True, text=True, timeout=120)
        data = open(out, "rb").read() if os.path.getsize(out) else b""
    finally:
        os.remove(out)
    if r.returncode or not data:
        raise HTTPException(500, "ffmpeg: " + (r.stderr or "no picture").strip()[-300:])
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/jobs/{jid}/agent/alt-lyrics")
async def agent_alt_lyrics(jid: str, req: Request):
    """How well other timed lyrics fit what's heard in this video: {"candidates": [{"id", "synced"}]} (at most 30, as
    LRCLIB gives them) -> [{"id", "fit", "shift", "lines", "squeezed", "tested"}]. Nothing is saved."""
    check_origin(req)
    raw = await req.body()
    if len(raw) > 3_000_000:
        raise HTTPException(413, "at most 3 MB of lyrics")
    try:
        b = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "the body isn't JSON")
    cands = [{"id": c.get("id"), "synced": str(c.get("synced") or "")} for c in (b.get("candidates") or [])
             if isinstance(c, dict)][:30]
    job = _done(jid)
    if not os.path.exists(job_path(jid, "heard.json")):
        raise HTTPException(400, "no heard.json for this song (made before the by-ear check): can't score lyrics against it")
    return {"scores": await asyncio.to_thread(_ks().altlyrics, job_path(jid), float(job.get("offset") or 0.0), cands)}


@app.post("/api/agent/romaji")
async def agent_romaji(req: Request):
    """Original-script lyric lines romanized the way the studio does: {"lines": [...]} -> {"romaji": [...]}."""
    check_origin(req)
    b = await req.json()
    lines = [str(x)[:300] for x in (b.get("lines") or [])][:300]
    return {"romaji": await asyncio.to_thread(_ks().romaji, lines) if lines else []}


# ---------------- static ----------------
@app.get("/")
def index():
    return FileResponse(f"{HERE}/static/index.html", headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest")
def manifest():
    """The Studio app (installable). Its icons are inlined: a login in front of the Studio (Authentik) may not let the
    browser fetch them on their own while it installs the app."""
    import base64

    def icon(name, size, purpose):
        b = open(f"{HERE}/static/pwa/{name}", "rb").read()
        return {"src": "data:image/png;base64," + base64.b64encode(b).decode(), "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
    m = {"name": C.STUDIO_NAME if getattr(C, "STUDIO_NAME", "") else "Karaoke Studio", "short_name": "Studio", "start_url": "/",
         "scope": "/", "display": "standalone", "background_color": "#15111c", "theme_color": "#15111c",
         "description": "Make karaoke songs, answer the party's requests, fix lyrics.",
         "icons": [icon("icon-192.png", 192, "any"), icon("icon-512.png", 512, "any"), icon("icon-maskable-512.png", 512, "maskable")]}
    return JSONResponse(m, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    return FileResponse(f"{HERE}/static/sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


app.mount("/static", StaticFiles(directory=f"{HERE}/static"), name="static")


@app.middleware("http")
async def headers(req: Request, call_next):
    r = await call_next(req)
    r.headers.setdefault("X-Content-Type-Options", "nosniff")
    return r


if __name__ == "__main__":
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5570)
    a = ap.parse_args()
    recover_jobs()
    recover_automake()
    recover_imports()
    threading.Thread(target=worker, daemon=True).start()
    _pitch_start()   # the pitch files left to write before the restart, and any song missing one (when idle)
    if C.ALBUM_LOOKUP:
        threading.Thread(target=album_fill, daemon=True, name="album-fill").start()
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
