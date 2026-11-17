#!/usr/bin/env python3
"""Karaoke Studio (2026-11-17): make karaoke songs from official music videos, preview, publish to PiKaraoke.

    python -I app.py [--host 127.0.0.1] [--port 5570]      (settings: .env, see .env.example)

Search (YouTube + LRCLIB) -> job (pipeline.py subprocess, one at a time on the GPU) -> preview in the browser
(SubtitlesOctopus, same renderer as PiKaraoke's TV page) with a timing slider -> publish into the PiKaraoke
library (optionally deleting the older YouTube lyric version) and ask PiKaraoke to rescan.
There is no login of its own: put it behind an authenticating reverse proxy, or keep it on 127.0.0.1.
"""
import argparse, asyncio, http.cookiejar, json, math, os, re, shutil, sqlite3, subprocess, sys, threading, time
import unicodedata, urllib.error, urllib.parse, urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
from assgen import load_lines, make_ass, save_lines  # noqa: E402

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
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
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
            if i == tries - 1 or (isinstance(e, urllib.error.HTTPError) and e.code < 500):
                raise
            time.sleep(1.0 + i)


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
             "nightcore", "tutorial", "lesson", "audio only", "visualizer", "fan made", "unofficial", "concert")


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
        if na and (na in nch or nch.replace(" ", "") in (na.replace(" ", "") + "vevo") or nch.endswith("vevo") and na.replace(" ", "") in nch.replace(" ", "")):
            score += 5
        if e.get("channel_is_verified"):
            score += 1.5
        if "official" in nt_ and "unofficial" not in nt_:
            score += 2
        if nt and nt in nt_:
            score += 1
        bad = [w for w in BAD_WORDS if w in nt_]
        score -= 4 * len(bad)
        score += math.log10((e.get("view_count") or 0) + 1) / 3
        out.append({"id": e["id"], "title": t, "channel": ch, "duration": int(e["duration"]),
                    "views": e.get("view_count"), "verified": bool(e.get("channel_is_verified")),
                    "official_channel": score >= 5, "flags": bad, "score": round(score, 2),
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
        toks = re.findall(r"[a-z']+", text.lower())
        english = bool(toks) and sum(t in ENGLISH_HINTS for t in toks) / len(toks) > 0.12
        latin = not japanese and (not letters or sum(c.isascii() for c in letters) / len(letters) >= 0.85)
        out.append({"source": "lrclib", "id": h["id"], "artist": h.get("artistName"), "title": h.get("trackName"),
                    "album": h.get("albumName"), "duration": h.get("duration"), "timed": bool(synced.strip()),
                    "latin": latin, "japanese": japanese, "english": english, "romaji": "automatic" if japanese else None,
                    "lines": sum(1 for l in synced.splitlines() if l.strip()), "first": first[:80],
                    "name_match": by_title or (bool(norm(title)) and norm(title) in norm(h.get("trackName") or ""))})
    return out[:20]


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


@app.get("/api/search")
def search(q: str):
    q = q.strip()
    if not q or len(q) > 200:
        raise HTTPException(400, "type Artist - Title")
    artist, _, title = q.partition(" - ")
    if not title:
        artist, title = "", q
    videos = yt_search(artist, title)
    notes = []
    km, jp_titles = km_search(title.strip(), artist.strip())
    try:
        lyrics = km + lrc_search(artist, title, jp_titles)
    except Exception as e:
        lyrics = km
        notes.append(f"LRCLIB didn't answer ({str(e)[:60]}); search again in a moment")
    return {"artist": artist.strip(), "title": title.strip(), "videos": videos, "lyrics": lyrics, "notes": notes}


# ---------------- jobs ----------------
@app.post("/api/jobs")
async def create_job(req: Request):
    check_origin(req)
    b = await req.json()
    yt, lrc_id = str(b.get("yt", "")), b.get("lrc_id")
    source = b.get("lyrics_source") or "lrclib"
    artist, title = str(b.get("artist", "")).strip(), str(b.get("title", "")).strip()
    ok_lyrics = (source == "lrclib" and isinstance(lrc_id, int)) or \
                (source == "karamoe" and isinstance(lrc_id, str) and re.fullmatch(r"[0-9a-f-]{36}", lrc_id))
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", yt) or not ok_lyrics or not artist or not title:
        raise HTTPException(400, "pick a video and timed lyrics, and give artist and title")
    slug = re.sub(r"[^a-z0-9]+", "-", norm(f"{artist} {title}"))[:60].strip("-") or "song"
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + slug
    os.makedirs(f"{JOBS}/{jid}")
    save(jid, {"id": jid, "yt": yt, "lrc_id": lrc_id, "lyrics_source": source, "artist": artist, "title": title,
               "video_title": str(b.get("video_title", ""))[:200], "status": "queued", "step": "queued",
               "created": time.time(), "offset": 0.0, "log": []})
    wake.set()
    return {"id": jid}


def summary(job):
    keys = ("id", "artist", "title", "yt", "status", "step", "created", "finished", "error", "offset",
            "published", "published_as", "video_title", "result", "waiting", "edited", "unpublished_changes", "lang", "gpu", "ai_romaji")
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
    return FileResponse(p, media_type=ctype, headers={"Cache-Control": "no-store"})


@app.post("/api/jobs/{jid}/offset")
async def set_offset(jid: str, req: Request):
    check_origin(req)
    b = await req.json()
    off = float(b.get("offset", 0))
    if not -5 <= off <= 5:
        raise HTTPException(400, "offset must be within ±5 s")
    with lock:
        job = load(jid)
        open(job_path(jid, "lyrics.ass"), "w").write(make_ass(load_lines(job_path(jid, "words.json")), off, video_size=vsize(job)))
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
    return {"lines": load_lines(job_path(jid, "words.json")), "offset": job.get("offset", 0.0),
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
        clean = []
        for w in words:
            if not (isinstance(w, list) and len(w) == 3 and all(isinstance(x, (int, float)) for x in w[:2])
                    and isinstance(w[2], str)):
                raise HTTPException(400, "bad word entry")
            a, b, t = float(w[0]), float(w[1]), w[2].strip()[:80]
            if not (math.isfinite(a) and math.isfinite(b)) or not 0 <= a <= limit or not 0 <= b <= limit:
                raise HTTPException(400, "word time out of range")
            if t:
                clean.append([round(a, 3), round(max(a, b), 3), t])
        if not clean:
            continue
        clean.sort(key=lambda w: w[0])
        orig = l.get("orig")
        sg = l.get("singer")
        heard = l.get("heard")
        out.append({"words": clean, "orig": orig.strip()[:300] if isinstance(orig, str) and orig.strip() else None,
                    "singer": sg if sg in (1, 2, 3) else None,
                    "heard": heard.strip()[:200] if isinstance(heard, str) and heard.strip() else None})
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
        if not os.path.exists(job_path(jid, "words.orig.json")):
            shutil.copyfile(job_path(jid, "words.json"), job_path(jid, "words.orig.json"))
        save_lines(job_path(jid, "words.json"), lines)
        open(job_path(jid, "lyrics.ass"), "w").write(make_ass(lines, job.get("offset", 0.0), video_size=vsize(job)))
        job["edited"] = time.time()
        if job.get("published"):
            job["unpublished_changes"] = True
        save(jid, job)
    return {"lines": len(lines)}


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


def auto_romaji(jid):
    """After a song with automatic romaji is made: apply the AI's fixes (the plain version stays in words.orig.json)."""
    try:
        lines = load_lines(job_path(jid, "words.json"))
        checked, sugg = ai_romaji_suggest(lines)
    except Exception as e:
        set_job(jid, log=load(jid).get("log", []) + [f"AI romaji check skipped ({str(e)[:80]})"])
        return
    with lock:
        job = load(jid)
        if sugg:
            if not os.path.exists(job_path(jid, "words.orig.json")):
                shutil.copyfile(job_path(jid, "words.json"), job_path(jid, "words.orig.json"))
            for g in sugg:
                lines[g["i"]]["words"] = g["words"]
            save_lines(job_path(jid, "words.json"), lines)
            open(job_path(jid, "lyrics.ass"), "w").write(make_ass(lines, job.get("offset", 0.0), video_size=vsize(job)))
        job["ai_romaji"] = [{k: g[k] for k in ("japanese", "old", "new")} for g in sugg]
        job.setdefault("log", []).append(f"AI romaji check: {len(sugg)} of {checked} lines fixed" if sugg else
                                         f"AI romaji check: all {checked} lines look right")
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
        checked, out = await asyncio.to_thread(ai_romaji_suggest, lines)
    except Exception as e:
        raise HTTPException(502, f"the AI check failed ({str(e)[:100]})")
    if not checked:
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
        open(job_path(jid, "lyrics.ass"), "w").write(make_ass(load_lines(orig), job.get("offset", 0.0), video_size=vsize(job)))
        job["edited"] = None
        job["ai_romaji"] = None  # back to the plain automatic romaji
        if job.get("published"):
            job["unpublished_changes"] = True
        save(jid, job)
    return {"ok": True}


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
        if job["status"] == "running":
            raise HTTPException(400, "it's running; wait for it to finish")
        shutil.rmtree(job_path(jid))  # only the studio's copy; a published song stays in PiKaraoke
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
    return True


def worker():
    while True:
        try:
            schedule_once()
        except Exception as e:
            print(f"scheduler error: {e!r}", flush=True)
        wake.wait(10); wake.clear()


def schedule_once():
    queued = sorted((j for j in list_jobs() if j["status"] == "queued"), key=lambda j: j["created"])
    for j in queued:
        jid = j["id"]
        free = gpu_free_mib()
        if C.USE_LOCAL_GPU and slots["local"] is None and free >= NEED_GPU_MIB:
            if claim(jid, "local", LOCAL_GPU):
                threading.Thread(target=run_local, args=(jid,), daemon=True).start()
            continue
        if slots["remote"] is None:
            h = helper_health()
            if h and not h.get("busy") and h.get("free_mib", 0) >= NEED_GPU_MIB:
                if claim(jid, "remote", h.get("gpu", "helper").replace("NVIDIA GeForce ", "") + " (" + C.GPU_HELPER_NAME + ")"):
                    threading.Thread(target=run_remote, args=(jid,), daemon=True).start()
                continue
        why = (f"waiting for the graphics card ({free // 1024} GB free, needs {NEED_GPU_MIB // 1024})"
               if slots["local"] is None else "waiting: the graphics card is making another song")
        if C.GPU_HELPER_URL and slots["remote"] is None:
            why += f"; {C.GPU_HELPER_NAME} is " + ("busy" if helper_seen["health"] else "off or unreachable")
        if j.get("waiting") != why:
            set_job(jid, waiting=why)
        break  # first come, first served


def finish(jid, rc, err, oom, where):
    with lock:
        job = load(jid)
        job["finished"] = time.time()
        if rc == 0 and os.path.exists(job_path(jid, "out.mp4")) and os.path.exists(job_path(jid, "result.json")):
            job.update(status="done", step="done")
            job["result"] = json.load(open(job_path(jid, "result.json")))
            job["result"]["gpu"] = job.get("gpu")
        elif oom and job.get("oom_retries", 0) < 2:
            # something else grabbed the GPU mid-job (an LLM loading, another tool): wait and try again
            job.update(status="queued", step="queued", oom_retries=job.get("oom_retries", 0) + 1,
                       waiting="the graphics card ran out of memory; trying again when one is free")
            job.setdefault("log", []).append(f"ran out of GPU memory on {job.get('gpu')}; will retry")
        else:
            job.update(status="failed", error=err or ("the graphics card ran out of memory" if oom else f"pipeline exited with code {rc}"))
        save(jid, job)


def maybe_auto_romaji(jid, rc):
    if rc != 0 or not C.OLLAMA_URL or not os.path.exists(job_path(jid, "result.json")):
        return
    if json.load(open(job_path(jid, "result.json"))).get("romaji") != "automatic":
        return
    set_job(jid, step="romaji")
    auto_romaji(jid)


def run_local(jid):
    rc, err, oom = 1, None, False
    try:
        p = subprocess.Popen([sys.executable, "-I", f"{HERE}/pipeline.py", f"{JOBS}/{jid}"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, env=dict(C.tool_env(), PYTHONUNBUFFERED="1"))
        for line in p.stdout:
            line = line.rstrip()
            oom = oom or "out of memory" in line.lower()
            if not line.startswith("@"):
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
        helper_call("/jobs", "POST", {"id": jid, **{k: job.get(k) for k in ("yt", "lrc_id", "lyrics_source", "artist", "title", "lang")}})
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
            if st["status"] == "done":
                for name in ("out.mp4", "vocals.m4a", "words.json", "lyrics.ass", "result.json"):
                    dst = job_path(jid, name)
                    with helper_call(f"/jobs/{jid}/files/{name}", timeout=120, raw=True) as r, open(dst + ".part", "wb") as f:
                        shutil.copyfileobj(r, f, 1 << 20)
                    os.replace(dst + ".part", dst)
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
        job.update(status="done", published=time.time(), published_as=base, unpublished_changes=False)
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
    return {"gpu_free_gb": round(gpu_free_mib() / 1024, 1), "need_gb": NEED_GPU_MIB // 1024,
            "pikaraoke_password": bool(C.PIKARAOKE_PASSWORD), "romaji_ai": bool(C.OLLAMA_URL),
            "gpus": {"local": LOCAL_GPU, "local_busy": slots["local"] is not None,
                     "helper": C.GPU_HELPER_NAME if C.GPU_HELPER_URL else None,
                     "helper_online": bool(helper_seen["health"]) if C.GPU_HELPER_URL else None,
                     "helper_busy": slots["remote"] is not None},
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
    threading.Thread(target=worker, daemon=True).start()
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning", proxy_headers=True, forwarded_allow_ips="*")
