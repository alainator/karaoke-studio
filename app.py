#!/usr/bin/env python3
"""Karaoke Studio (2026-11-17): make karaoke songs from official music videos, preview, publish to PiKaraoke.

    python -I app.py [--host 127.0.0.1] [--port 5570]      (settings: .env, see .env.example)

Search (YouTube + LRCLIB) -> job (pipeline.py subprocess, one at a time on the GPU) -> preview in the browser
(SubtitlesOctopus, same renderer as PiKaraoke's TV page) with a timing slider -> publish into the PiKaraoke
library (optionally deleting the older YouTube lyric version) and ask PiKaraoke to rescan.
There is no login of its own: put it behind an authenticating reverse proxy, or keep it on 127.0.0.1.
"""
import argparse, asyncio, http.cookiejar, json, math, os, re, shutil, sqlite3, subprocess, sys, threading, time
import logging, logging.handlers
import unicodedata, urllib.error, urllib.parse, urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
from assgen import load_cast, load_lines, make_ass, save_lines, who_of  # noqa: E402
import versions  # noqa: E402
import trimtools  # noqa: E402
import videoswap  # noqa: E402

# what the studio did and when: data/logs/studio.log (5 files of 2 MB kept)
os.makedirs(C.LOGS, exist_ok=True)
log = logging.getLogger("studio")
log.setLevel(logging.INFO)
if not log.handlers:
    _h = logging.handlers.RotatingFileHandler(os.path.join(C.LOGS, "studio.log"), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(_h)

JOBS, LIB, MV_DIR = C.JOBS, C.LIBRARY, C.MV_DIR
PK_DB, PK_URL = C.PIKARAOKE_DB, C.PIKARAOKE_URL
UA = {"User-Agent": C.USER_AGENT}
NEED_GPU_MIB = C.MIN_GPU_FREE_MIB
BIN = os.path.dirname(sys.executable)  # yt-dlp lives in the same virtualenv
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
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        tot, used = (int(x) for x in r.stdout.split(","))
        return tot - used
    except Exception:
        return 0


def vsize(job):
    r = job.get("result") or {}
    if r.get("width"):
        return (r["width"], r["height"])
    m = re.match(r"[^,]*,(\d+),(\d+)", r.get("video") or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def final_base(job):
    return f"{safe_name(job['artist'])} - {safe_name(job['title'])} (Music Video)"


# ---------------- search ----------------
BAD_WORDS = ("lyric", "karaoke", "live", "cover", "reaction", "remix", "instrumental", "8d", "sped up", "slowed",
             "nightcore", "tutorial", "lesson", "audio only", "visualizer", "fan made", "unofficial", "concert",
             "dance practice", "practice", "performance video", "performance", "making film", "making", "teaser",
             "behind", "fancam", "focus cam", "choreography", "relay dance", "dance cover", "skzoo ver", "shorts")


def yt_search(artist, title):
    q = f"ytsearch10:{artist} {title} official music video"
    env = C.tool_env()
    r = subprocess.run([f"{BIN}/yt-dlp", "--js-runtimes", "node", "--flat-playlist", "-J", "--", q],
                       capture_output=True, text=True, timeout=60, env=env)
    if r.returncode:
        raise HTTPException(502, "YouTube search failed: " + r.stderr.strip()[-200:])
    out, na, nt = [], norm(artist), norm(title)
    for e in json.loads(r.stdout).get("entries") or []:
        if not e or not e.get("id") or not e.get("duration"):
            continue
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
        bad = [w for w in BAD_WORDS if w in nt_]
        bad = [w for w in bad if not any(w != o and w in o for o in bad)]
        score -= 4 * len(bad)
        score += math.log10((e.get("view_count") or 0) + 1) / 3
        out.append({"id": e["id"], "title": t, "channel": ch, "duration": int(e["duration"]),
                    "views": e.get("view_count"), "verified": bool(e.get("channel_is_verified")),
                    "official_channel": score >= 5 and bool(nch), "flags": bad, "score": round(score, 2),
                    "thumb": f"https://i.ytimg.com/vi/{e['id']}/mqdefault.jpg"})
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


@app.get("/api/search")
def search(q: str):
    q = q.strip()
    if not q or len(q) > 200:
        raise HTTPException(400, "type Artist - Title")
    artist, _, title = q.partition(" - ")
    if not title:
        artist, title = "", q
        guess = split_query(q)  # typed without ' - ': work out which part is the artist
        if guess:
            artist, title = guess
    videos = yt_search(artist, title)
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
    except Exception as e:
        lyrics = km
        notes.append(f"LRCLIB didn't answer ({str(e)[:60]}); search again in a moment")
    return {"artist": artist.strip(), "title": title.strip(), "videos": videos, "lyrics": lyrics, "notes": notes}


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


@app.post("/api/jobs")
async def create_job(req: Request):
    check_origin(req)
    b = await req.json()
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
    slug = re.sub(r"[^a-z0-9]+", "-", norm(f"{artist} {title}"))[:60].strip("-") or "song"
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + slug
    os.makedirs(f"{JOBS}/{jid}")
    save(jid, {"id": jid, "yt": yt, "lrc_id": lrc_id, "lyrics_source": source, "artist": artist, "title": title,
               "video_title": str(b.get("video_title", ""))[:200], "status": "queued", "step": "queued",
               "gpu_pref": b.get("gpu") if b.get("gpu") in ("local", "remote") else "auto",
               **({"lyrics_text": pasted} if source == "pasted" else {}),
               "created": time.time(), "offset": 0.0, "log": []})
    log.info("queued %s | video %s | lyrics %s:%s | gpu %s%s", jid, yt, source, lrc_id if source != "pasted" else f"{len(pasted)} chars",
             b.get("gpu") or "auto", f" | replaces {b.get('replace')}" if b.get("replace") else "")
    # "Change video or lyrics" on an existing song: the new one takes its place once it has been made (if it fails,
    # the old song is still there). Only the studio's copy is removed; a published file stays in PiKaraoke until the
    # new song is published over it.
    old = str(b.get("replace") or "")
    if ID_RE.match(old) and old != jid and os.path.exists(os.path.join(JOBS, old, "job.json")):
        set_job(jid, replaces=old)
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
    return out


def _summary(job):
    keys = ("id", "artist", "title", "yt", "status", "step", "created", "finished", "error", "offset",
            "published", "published_as", "video_title", "result", "waiting", "edited", "unpublished_changes", "lang", "gpu", "ai_romaji", "video_v", "gpu_pref", "reviewed", "lrc_id", "lyrics_source", "trim", "video_task")
    return {k: job.get(k) for k in keys}


@app.get("/api/jobs")
def list_jobs():
    out = []
    for jid in sorted(os.listdir(JOBS), reverse=True):
        if ID_RE.match(jid) and os.path.exists(f"{JOBS}/{jid}/job.json"):
            try:
                out.append(summary(load(jid)))
            except Exception:
                pass
    return out


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    job = load(jid)
    s = summary(job)
    s["log"] = job.get("log", [])[-30:]
    s["steps"] = STEPS
    if job.get("status") == "done":
        s["videos"] = videos_listing(jid, job)
    return s


FILES = {"video.mp4": ("out.mp4", "video/mp4"), "vocals.m4a": ("vocals.m4a", "audio/mp4"),
         "lyrics.ass": ("lyrics.ass", "text/plain; charset=utf-8")}


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
    cache = "no-store" if name == "lyrics.ass" else "private, max-age=604800"
    return FileResponse(p, media_type=ctype, headers={"Cache-Control": cache})


@app.post("/api/jobs/{jid}/offset")
async def set_offset(jid: str, req: Request):
    check_origin(req)
    b = await req.json()
    off = float(b.get("offset", 0))
    if not -5 <= off <= 5:
        raise HTTPException(400, "offset must be within ±5 s")
    with lock:
        job = load(jid)
        open(job_path(jid, "lyrics.ass"), "w").write(make_ass(load_lines(job_path(jid, "words.json")), off, video_size=vsize(job),
                                                              cast=load_cast(job_path(jid, "words.json"))))
        if round(off, 2) != job.get("offset") and job.get("published"):
            job["unpublished_changes"] = True
        job["offset"] = round(off, 2)
        save(jid, job)
    return {"offset": job["offset"]}


# ---------------- lyric editor ----------------
@app.get("/api/jobs/{jid}/lines")
def get_lines(jid: str):
    job = load(jid)
    if job["status"] != "done":
        raise HTTPException(400, "finish making it first")
    return {"lines": load_lines(job_path(jid, "words.json")), "cast": load_cast(job_path(jid, "words.json")),
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
                    "ok": bool(l.get("ok")) and not l.get("weak"), "backing": bool(l.get("backing"))})
    if not out:
        raise HTTPException(400, "no lines left")
    out.sort(key=lambda l: l["words"][0][0])
    return out


@app.post("/api/jobs/{jid}/lines")
async def put_lines(jid: str, req: Request):
    check_origin(req)
    b = await req.json()
    with lock:
        job = load(jid)
        if job["status"] != "done":
            raise HTTPException(400, "finish making it first")
        lines = _valid_lines(b.get("lines"), (job.get("result") or {}).get("duration"))
        cast = b.get("cast")
        cast = [str(x).strip()[:40] for x in cast if str(x).strip()][:8] if isinstance(cast, list) else load_cast(job_path(jid, "words.json"))
        for l in lines:
            if l.get("members"):
                l["members"] = [i for i in l["members"] if i < len(cast)] or None
            if l.get("wwho"):
                l["wwho"] = [None if not x else x if "singer" in x else ({"members": [i for i in x["members"] if i < len(cast)]}
                             if any(i < len(cast) for i in x["members"]) else None) for x in l["wwho"]]
                l["wwho"] = l["wwho"] if any(l["wwho"]) else None
        if not os.path.exists(job_path(jid, "words.orig.json")):
            shutil.copyfile(job_path(jid, "words.json"), job_path(jid, "words.orig.json"))
        save_lines(job_path(jid, "words.json"), lines, cast)
        ass_p, ass_new = job_path(jid, "lyrics.ass"), make_ass(lines, job.get("offset", 0.0), video_size=vsize(job), cast=cast)
        try:
            ass_same = open(ass_p).read() == ass_new
        except OSError:
            ass_same = False
        open(ass_p, "w").write(ass_new)
        job["edited"] = time.time()
        if job.get("published") and not ass_same:  # clearing a ⚠ mark changes nothing on screen: nothing to republish
            job["unpublished_changes"] = True
        save(jid, job)
    return {"lines": len(lines)}


@app.post("/api/jobs/{jid}/trim")
async def trim_song(jid: str, req: Request):
    """Cut the start and/or end off a made song's video (ads, an intro skit, a long title card). start/end are seconds
    in the ORIGINAL video; start 0 and no end puts the whole video back. Lines move with the cut. Lines sung outside
    the kept part are refused unless drop_lines is true."""
    check_origin(req)
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
# as its own PiKaraoke entry. Downloading, lining up and rendering run in a background thread, one at a time per song;
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
                if changed:  # out.mp4 is new: the page must fetch it again, and PiKaraoke has the old one
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
    """A version's name in PiKaraoke: "Artist - Title (Mia's version)"."""
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
    """Remove a version (its studio copy; a published copy stays in PiKaraoke, like deleting a song). For the
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
        save(jid, job)
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
    """Publish a version to PiKaraoke as its own song, "Artist - Title (name)", with the song's lyrics."""
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
            raise HTTPException(400, "a song with that name is already in PiKaraoke")
        os.makedirs(MV_DIR, exist_ok=True)
        for src, ext in ((videoswap.entry_video(job_path(jid), key), ".mp4"), (job_path(jid, "lyrics.ass"), ".ass")):
            tmp = f"{MV_DIR}/.{base}{ext}.part"
            shutil.copyfile(src, tmp)
            os.replace(tmp, f"{MV_DIR}/{base}{ext}")
        meta.update(published=time.time(), published_as=base, unpublished_changes=False, lyrics_published=time.time())
        videoswap.save_meta(d, meta)
        log.info("published %s version %s as %s", jid, key, base)
        job.setdefault("log", []).append(f"published the version “{meta.get('name')}” as '{base}'")
        save(jid, job)
    msg = pk_rescan()
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
            open(job_path(jid, "lyrics.ass"), "w").write(make_ass(lines, job.get("offset", 0.0), video_size=vsize(job),
                                                                  cast=load_cast(job_path(jid, "words.json"))))
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
        open(job_path(jid, "lyrics.ass"), "w").write(make_ass(load_lines(orig), job.get("offset", 0.0), video_size=vsize(job),
                                                              cast=load_cast(orig)))
        job["edited"] = None
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
        save(jid, job)
    return {"reviewed": job["reviewed"]}


@app.post("/api/jobs/{jid}/rename")
async def rename(jid: str, req: Request):
    """Change a song's artist/title. If it's published, its files in PiKaraoke are renamed too."""
    check_origin(req)
    b = await req.json()
    artist, title = str(b.get("artist", "")).strip()[:120], str(b.get("title", "")).strip()[:160]
    if not artist or not title:
        raise HTTPException(400, "give both artist and title")
    with lock:
        job = load(jid)
        old = job.get("published_as")
        job.update(artist=artist, title=title)
        new = final_base(job)
        moved = False
        if old and old != new and os.path.exists(f"{MV_DIR}/{old}.mp4"):
            if os.path.exists(f"{MV_DIR}/{new}.mp4"):
                raise HTTPException(400, "a song with that name is already in PiKaraoke")
            for ext in (".mp4", ".ass"):
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
                for ext in (".mp4", ".ass"):
                    if os.path.exists(f"{MV_DIR}/{pa}{ext}"):
                        os.replace(f"{MV_DIR}/{pa}{ext}", f"{MV_DIR}/{nv}{ext}")
                m["published_as"] = nv
                videoswap.save_meta(videoswap.version_dir(job_path(jid), m["slug"]), m)
                moved = True
        save(jid, job)
    msg = pk_rescan() if moved else "renamed"
    return {"artist": artist, "title": title, "name": new, "message": msg}


@app.post("/api/jobs/{jid}/remake")
async def remake(jid: str, req: Request):
    """Make a finished song again with the current version, in place (same page; publishing replaces PiKaraoke's copy).
    The current lyrics (with any edits) are kept as words.before-remake.json."""
    check_origin(req)
    try:
        gpu = (await req.json()).get("gpu")
    except Exception:
        gpu = None
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
                   pre_remake={k: job.get(k) for k in ("edited", "result", "offset")})
        save(jid, job)
    log.info("remake %s | gpu %s | keeps edits: %s", jid, job.get("gpu_pref", "auto"), merge)
    wake.set()
    return {"ok": True}


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
        lines = load_lines(job_path(jid, "words.json"))
        open(job_path(jid, "lyrics.ass"), "w").write(make_ass(lines, job.get("offset", 0.0), video_size=vsize(job),
                                                              cast=load_cast(job_path(jid, "words.json"))))
        job.update(remake_undone=True, remake_note=None, edited=(job.get("pre_remake") or {}).get("edited") or time.time())
        if job.get("published"):
            job["unpublished_changes"] = True
        job.setdefault("log", []).append("remake undone: the lyrics are back to the version before it")
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
        open(job_path(jid, "lyrics.ass"), "w").write(make_ass(lines, job.get("offset", 0.0), video_size=vsize(job), cast=cast))
        job["result"] = dict(job.get("result") or {}, lang=to)
        job["lang"] = to  # a remake keeps the choice
        job["edited"] = time.time()
        if job.get("published"):
            job["unpublished_changes"] = True
        save(jid, job)
    return {"lines": n, "lang": to}


@app.post("/api/jobs/{jid}/retry")
async def retry(jid: str, req: Request):
    check_origin(req)
    with lock:
        job = load(jid)
        if job["status"] != "failed":
            raise HTTPException(400, "only failed jobs can be retried")
        job.update(status="queued", step="queued", error=None, log=[])
        save(jid, job)
    wake.set()
    return {"ok": True}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str, req: Request):
    check_origin(req)
    with lock:
        job = load(jid)
        if job["status"] == "running" or video_busy(job):
            raise HTTPException(400, "it's running; wait for it to finish")
        shutil.rmtree(job_path(jid))  # only the studio's copy; a published song stays in PiKaraoke
    log.info("deleted %s (%s - %s)", jid, job.get("artist"), job.get("title"))
    return {"ok": True}


# ---------------- worker ----------------
# Two slots: this machine's GPU, and optionally a GPU helper on another PC (helper.py, GPU_HELPER_URL). The oldest
# queued job goes to the local GPU if it has room, else to the helper if it's online with room, else it waits.
wake = threading.Event()
slots = {"local": None, "remote": None}
helper_seen = {"at": 0.0, "health": None}


def local_gpu_name():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip().splitlines()[0].replace("NVIDIA GeForce ", "")
    except Exception:
        return "this server's GPU"


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
        job.update(status="running", step="lyrics", waiting=None, started=time.time(), gpu=gpu_name, log=[])
        save(jid, job)
    slots[where] = jid
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


def finish(jid, rc, err, oom, where):
    with lock:
        job = load(jid)
        job["finished"] = time.time()
        if rc == 0 and os.path.exists(job_path(jid, "out.mp4")) and os.path.exists(job_path(jid, "result.json")):
            job.update(status="done", step="done")
            job["result"] = json.load(open(job_path(jid, "result.json")))
            job["result"]["gpu"] = job.get("gpu")
            if job.get("published"):  # a remade song: PiKaraoke still has the old one until it's published again
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
                    lines = load_lines(job_path(jid, "words.json"))
                    open(job_path(jid, "lyrics.ass"), "w").write(make_ass(lines, job.get("offset", 0.0), video_size=vsize(job),
                                                                          cast=load_cast(job_path(jid, "words.json"))))
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
        p = subprocess.Popen([sys.executable, "-I", f"{HERE}/pipeline.py", f"{JOBS}/{jid}"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=dict(C.tool_env(), PYTHONUNBUFFERED="1"))
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
        if rc != 0 and not err and not oom and crash:
            err = f"it stopped with an error ({crash})"
        if oom:
            time.sleep(20)
        maybe_auto_romaji(jid, rc)
        finish(jid, rc, err, oom, "local")
    except Exception as e:
        try:
            set_job(jid, status="failed", error=f"studio error: {str(e)[:150]}")
        except Exception:
            pass
    finally:
        slots["local"] = None
        wake.set()


def run_remote(jid):
    """Run the job on the GPU helper, mirror its progress, then copy the finished files here."""
    try:
        job = load(jid)
        helper_call("/jobs", "POST", {"id": jid, **{k: job.get(k) for k in ("yt", "lrc_id", "lyrics_source", "artist", "title", "lang", "lyrics_text")}})
        misses = 0
        while True:
            time.sleep(2)
            try:
                st = helper_call(f"/jobs/{jid}")
                misses = 0
            except Exception:
                misses += 1
                if misses >= 15:  # ~30 s without an answer: the PC was shut down or went to sleep
                    set_job(jid, status="queued", step="queued", waiting="the helper PC went away; waiting for a graphics card")
                    return
                continue
            if not os.path.isdir(job_path(jid)):
                return  # deleted meanwhile
            set_job(jid, step=st.get("step") or "lyrics", log=st.get("log", []))
            if st["status"] == "running":
                continue
            try:  # the helper's full pipeline output for this run, whether it worked or not
                with helper_call(f"/jobs/{jid}/files/pipeline.log", timeout=30, raw=True) as r, open(job_path(jid, "pipeline.log"), "ab") as f:
                    f.write(r.read()[-2_000_000:])
            except Exception:
                C.job_log(job_path(jid), "(the helper's pipeline output couldn't be fetched)")
            if st["status"] == "done":
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
            set_job(jid, status="queued", step="queued", waiting=f"the helper PC had a problem ({str(e)[:60]}); waiting for a graphics card")
        except Exception:
            pass
    finally:
        slots["remote"] = None
        wake.set()


# ---------------- PiKaraoke ----------------
def pk_rows(sql, args=()):
    if not PK_DB or not os.path.exists(PK_DB):
        return []
    c = sqlite3.connect(f"file:{PK_DB}?mode=ro", uri=True, timeout=5)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


FILLER = {"karaoke", "version", "versions", "lyrics", "lyric", "with", "instrumental", "official", "video", "hd", "hq",
          "original", "key", "songs", "song", "sing", "king", "karafun", "along", "backing", "track", "no", "vocals",
          "vocal", "minus", "one", "the", "by", "in", "style", "of", "zoom", "lower", "higher", "4k", "1080p", "720p"}


def old_versions(job):
    """YouTube lyric versions of the same song in the library root (only clear matches)."""
    na, nt = norm(job["artist"]), norm(job["title"])
    out = []
    for name in sorted(os.listdir(LIB)):
        m = re.fullmatch(r"(.+)---([A-Za-z0-9_-]{11})\.(mp4|webm|mkv|mp3)", name)
        if not m or not os.path.isfile(os.path.join(LIB, name)):
            continue
        stem = norm(m.group(1))
        for first, second in ((na, nt), (nt, na)):  # "Artist - Title ..." or "Title - Artist ..."
            if na and nt and stem.startswith(first + " " + second):
                rest = stem[len(first) + 1 + len(second):].split()
                if all(t in FILLER for t in rest):
                    out.append(name)
                    break
    return out


def pk_rescan():
    """Log in as PiKaraoke admin (if a password is configured) and start a library sync."""
    pw = C.PIKARAOKE_PASSWORD
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        if pw:
            op.open(urllib.request.Request(f"{PK_URL}/auth", data=urllib.parse.urlencode(
                {"admin_password": pw, "next": "/"}).encode()), timeout=15).read()
        r = op.open(urllib.request.Request(f"{PK_URL}/sync_library", data=b""), timeout=15)
        return "PiKaraoke is rescanning; the song shows up in a few seconds"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "saved, but PiKaraoke refused the rescan (admin password missing or wrong); it appears after PiKaraoke's next restart"
        return f"saved, but the rescan failed (HTTP {e.code}); it appears after PiKaraoke's next restart"
    except Exception as e:
        return f"saved, but PiKaraoke didn't answer ({str(e)[:80]}); it appears after its next restart"


@app.get("/api/jobs/{jid}/publish-info")
def publish_info(jid: str):
    job = load(jid)
    base = final_base(job)
    return {"name": base, "exists": os.path.exists(f"{MV_DIR}/{base}.mp4"), "old_versions": old_versions(job)}


@app.post("/api/jobs/{jid}/publish")
async def publish(jid: str, req: Request):
    check_origin(req)
    b = await req.json()
    remove = [str(x) for x in b.get("remove", [])]
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
        for src, ext in (("out.mp4", ".mp4"), ("lyrics.ass", ".ass")):
            tmp = f"{MV_DIR}/.{base}{ext}.part"
            shutil.copyfile(job_path(jid, src), tmp)
            os.replace(tmp, f"{MV_DIR}/{base}{ext}")
        removed = []
        for name in remove:
            p = os.path.realpath(os.path.join(LIB, name))
            if os.path.dirname(p) != os.path.realpath(LIB):
                continue
            stem = os.path.splitext(p)[0]
            for ext in (os.path.splitext(p)[1], ".ass", ".cdg", ".ASS"):
                if os.path.isfile(stem + ext):
                    os.remove(stem + ext)
            removed.append(name)
        # its published versions share these lyrics: they get them too
        for m in videoswap.entries(job_path(jid)):
            pa = m.get("published_as")
            if m["mode"] == "version" and pa and os.path.exists(f"{MV_DIR}/{pa}.mp4"):
                shutil.copyfile(job_path(jid, "lyrics.ass"), f"{MV_DIR}/.{pa}.ass.part")
                os.replace(f"{MV_DIR}/.{pa}.ass.part", f"{MV_DIR}/{pa}.ass")
                m["lyrics_published"] = time.time()
                videoswap.save_meta(videoswap.version_dir(job_path(jid), m["slug"]), m)
        job.update(status="done", published=time.time(), published_as=base, unpublished_changes=False)
        log.info("published %s as %s", jid, base)
        job.setdefault("log", []).append(f"published as '{base}'" + (f"; removed {len(removed)} old version(s)" if removed else ""))
        save(jid, job)
    msg = pk_rescan()
    return {"name": base, "removed": removed, "message": msg}


@app.get("/api/library")
def library():
    plays = {}
    try:
        for title, n, last in pk_rows("select song_title, count(*), max(played_at) from plays "
                                      "where performer != 'claude-test' group by song_title"):
            plays[title] = (n, last)
    except Exception:
        pass
    songs = []
    for d, _, files in os.walk(LIB):
        for f in files:
            if f.lower().endswith((".mp4", ".webm", ".mkv", ".mp3", ".zip", ".cdg")) and not f.startswith("."):
                if f.lower().endswith(".cdg"):
                    continue
                rel = os.path.relpath(os.path.join(d, f), LIB)
                folder = os.path.dirname(rel) or "YouTube"
                title = re.sub(r"---[A-Za-z0-9_-]{11}$", "", os.path.splitext(f)[0])
                n, last = plays.get(title, (0, None))
                songs.append({"file": rel, "title": title, "folder": folder, "plays": n, "last_played": last,
                              "lyrics": os.path.exists(os.path.join(d, os.path.splitext(f)[0] + ".ass"))})
    mv_full = {loose(s["title"].replace("(Music Video)", "")) for s in songs if s["folder"] == "Music Videos"}
    mv_titles = {loose(s["title"].replace("(Music Video)", "").split(" - ", 1)[-1]) for s in songs if s["folder"] == "Music Videos"}
    for s in songs:
        if s["folder"] == "Music Videos":
            continue
        if s["folder"] == "YouTube":
            q = re.sub(r"\s*[\(\[][^\)\]]*(karaoke|lyrics?|instrumental)[^\)\]]*[\)\]]", "", s["title"], flags=re.I)
            q = re.split(r"\s+_\s+|\s+-\s+(?=karaoke\b|lyrics?\b)", q, flags=re.I)[0].strip(" -_")
        else:  # e.g. Anime/ from kara.moe: "Singer - Title (Series OP) (AI instrumental)"
            q = re.sub(r"\s*\([^)]*\)", "", s["title"]).strip(" -_")
        s["query"] = q
        title_part = q.split(" - ", 1)[-1]
        # an anime song whose video already IS the official (anime) music video doesn't need one
        s["official_mv"] = s["folder"] != "YouTube" and bool(re.search(r"\([^)]*\bMV\b[^)]*\)", s["title"]))
        s["has_mv"] = loose(q) in mv_full or loose(title_part) in mv_titles
    songs.sort(key=lambda s: (-s["plays"], s["title"].lower()))
    return {"songs": songs}


@app.get("/api/status")
def status():
    if C.GPU_HELPER_URL and time.time() - helper_seen["at"] > 20:
        helper_health()  # the Make page shows each card's state
    hh = helper_seen["health"] or {}
    return {"helper_free_gb": round(hh.get("free_mib", 0) / 1024, 1) if hh else None, "helper_gpu": (hh.get("gpu") or "").replace("NVIDIA GeForce ", "") or None,"gpu_free_gb": round(gpu_free_mib() / 1024, 1), "need_gb": NEED_GPU_MIB // 1024,
            "pikaraoke_password": bool(C.PIKARAOKE_PASSWORD), "romaji_ai": bool(C.OLLAMA_URL),
            "gpus": {"local": LOCAL_GPU, "local_busy": slots["local"] is not None,
                     "helper": C.GPU_HELPER_NAME if C.GPU_HELPER_URL else None,
                     "helper_online": bool(helper_seen["health"]) if C.GPU_HELPER_URL else None,
                     "helper_busy": slots["remote"] is not None, "helper_outdated": bool(helper_seen.get("stale")),
                     "code_version": code_version()},
            "renderer": os.path.exists(f"{HERE}/static/octopus/subtitles-octopus.js"),
            "fonts": sorted(f"/static/octopus/{f}" for f in os.listdir(f"{HERE}/static/octopus")
                            if f.lower().endswith((".ttf", ".otf")))
            if os.path.isdir(f"{HERE}/static/octopus") else []}


# ---------------- static ----------------
@app.get("/")
def index():
    return FileResponse(f"{HERE}/static/index.html", headers={"Cache-Control": "no-cache"})


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
    # a job left "running" by a crash/restart goes back in the queue
    for j in list_jobs():
        if j["status"] == "running":
            job = load(j["id"]); job.update(status="queued", step="queued"); save(j["id"], job)
        if (j.get("video_task") or {}).get("running"):  # not requeued: the custom videos say they need a re-render
            job = load(j["id"])
            job["video_task"] = dict(job["video_task"], running=False, error="the studio restarted while making it; try again (Re-render)")
            save(j["id"], job)
    threading.Thread(target=worker, daemon=True).start()
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
