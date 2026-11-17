#!/usr/bin/env python3
"""Karaoke Studio (2026-11-17): make karaoke songs from official music videos, preview, publish to PiKaraoke.

    python -I app.py [--host 127.0.0.1] [--port 5570]      (settings: .env, see .env.example)

Search (YouTube + LRCLIB) -> job (pipeline.py subprocess, one at a time on the GPU) -> preview in the browser
(SubtitlesOctopus, same renderer as PiKaraoke's TV page) with a timing slider -> publish into the PiKaraoke
library (optionally deleting the older YouTube lyric version) and ask PiKaraoke to rescan.
There is no login of its own: put it behind an authenticating reverse proxy, or keep it on 127.0.0.1.
"""
import argparse, http.cookiejar, json, math, os, re, shutil, sqlite3, subprocess, sys, threading, time
import unicodedata, urllib.parse, urllib.request

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
from assgen import make_ass  # noqa: E402

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


def http_json(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


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


def lrc_search(artist, title):
    q = urllib.parse.urlencode({"track_name": title, "artist_name": artist})
    hits = http_json(f"https://lrclib.net/api/search?{q}")
    if len(hits) < 3:  # loose search as a fallback
        hits += http_json("https://lrclib.net/api/search?" + urllib.parse.urlencode({"q": f"{artist} {title}"}))
    seen, out = set(), []
    for h in hits:
        if h["id"] in seen or h.get("instrumental"):
            continue
        seen.add(h["id"])
        synced = h.get("syncedLyrics") or ""
        first = next((re.sub(r"^\[[^\]]*\]\s*", "", l) for l in synced.splitlines() if re.sub(r"^\[[^\]]*\]\s*", "", l).strip()), "")
        text = re.sub(r"\[[^\]]*\]", "", synced) or (h.get("plainLyrics") or "")
        letters = [c for c in text if c.isalpha()]
        latin = not letters or sum(c.isascii() for c in letters) / len(letters) >= 0.85
        out.append({"id": h["id"], "artist": h.get("artistName"), "title": h.get("trackName"), "album": h.get("albumName"),
                    "duration": h.get("duration"), "timed": bool(synced.strip()), "latin": latin,
                    "lines": sum(1 for l in synced.splitlines() if l.strip()), "first": first[:80],
                    "name_match": norm(title) in norm(h.get("trackName") or "")})
    return out[:15]


@app.get("/api/search")
def search(q: str):
    q = q.strip()
    if not q or len(q) > 200:
        raise HTTPException(400, "type Artist - Title")
    artist, _, title = q.partition(" - ")
    if not title:
        artist, title = "", q
    videos = yt_search(artist, title)
    lyrics = lrc_search(artist, title) if artist else lrc_search("", title)
    return {"artist": artist.strip(), "title": title.strip(), "videos": videos, "lyrics": lyrics}


# ---------------- jobs ----------------
@app.post("/api/jobs")
async def create_job(req: Request):
    check_origin(req)
    b = await req.json()
    yt, lrc_id = str(b.get("yt", "")), b.get("lrc_id")
    artist, title = str(b.get("artist", "")).strip(), str(b.get("title", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", yt) or not isinstance(lrc_id, int) or not artist or not title:
        raise HTTPException(400, "pick a video and timed lyrics, and give artist and title")
    slug = re.sub(r"[^a-z0-9]+", "-", norm(f"{artist} {title}"))[:60].strip("-") or "song"
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + slug
    os.makedirs(f"{JOBS}/{jid}")
    save(jid, {"id": jid, "yt": yt, "lrc_id": lrc_id, "artist": artist, "title": title, "lang": "en",
               "video_title": str(b.get("video_title", ""))[:200], "status": "queued", "step": "queued",
               "created": time.time(), "offset": 0.0, "log": []})
    wake.set()
    return {"id": jid}


def summary(job):
    keys = ("id", "artist", "title", "yt", "status", "step", "created", "finished", "error", "offset",
            "published", "published_as", "video_title", "result", "waiting")
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
        words = json.load(open(job_path(jid, "words.json")))
        open(job_path(jid, "lyrics.ass"), "w").write(make_ass(words, off))
        job["offset"] = round(off, 2)
        save(jid, job)
    return {"offset": job["offset"]}


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
wake = threading.Event()


current = {"jid": None}


def worker():
    while True:
        try:
            work_once()
        except Exception as e:  # keep the queue alive and mark the job that broke as failed
            print(f"worker error: {e!r}", flush=True)
            jid = current.get("jid")
            try:
                if jid:
                    with lock:
                        job = load(jid)
                        if job.get("status") in ("running", "queued"):
                            job.update(status="failed", error=f"studio error: {str(e)[:150]}"); save(jid, job)
            except Exception:
                pass  # e.g. the job was deleted meanwhile
            time.sleep(5)
        finally:
            current["jid"] = None


def work_once():
    if True:
        queued = sorted((j for j in list_jobs() if j["status"] == "queued"), key=lambda j: j["created"])
        if not queued:
            wake.wait(30); wake.clear(); return
        jid = queued[0]["id"]
        current["jid"] = jid
        free = gpu_free_mib()
        if free < NEED_GPU_MIB:
            with lock:
                job = load(jid); job["waiting"] = f"waiting for the graphics card ({free // 1024} GB free, needs {NEED_GPU_MIB // 1024})"; save(jid, job)
            wake.wait(30); wake.clear(); return
        with lock:
            job = load(jid); job.update(status="running", step="lyrics", waiting=None, started=time.time()); save(jid, job)
        env = dict(C.tool_env(), PYTHONUNBUFFERED="1")
        p = subprocess.Popen([sys.executable, "-I", f"{HERE}/pipeline.py", f"{JOBS}/{jid}"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        err = None
        for line in p.stdout:
            line = line.rstrip()
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
        with lock:
            job = load(jid)
            job["finished"] = time.time()
            if rc == 0 and os.path.exists(job_path(jid, "out.mp4")) and os.path.exists(job_path(jid, "result.json")):
                job.update(status="done", step="done")
                job["result"] = json.load(open(job_path(jid, "result.json")))
            else:
                job.update(status="failed", error=err or f"pipeline exited with code {rc}")
            save(jid, job)


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
        job.update(status="done", published=time.time(), published_as=base)
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
    mv = {norm(s["title"].replace("(Music Video)", "")) for s in songs if s["folder"] == "Music Videos"}
    for s in songs:
        if s["folder"] == "YouTube":
            q = re.sub(r"\s*[\(\[][^\)\]]*(karaoke|lyrics?|instrumental)[^\)\]]*[\)\]]", "", s["title"], flags=re.I)
            q = re.split(r"\s+_\s+|\s+-\s+(?=karaoke\b|lyrics?\b)", q, flags=re.I)[0].strip(" -_")
            s["query"] = q
            s["has_mv"] = norm(q) in mv
    songs.sort(key=lambda s: (-s["plays"], s["title"].lower()))
    return {"songs": songs}


@app.get("/api/status")
def status():
    return {"gpu_free_gb": round(gpu_free_mib() / 1024, 1), "need_gb": NEED_GPU_MIB // 1024,
            "pikaraoke_password": bool(C.PIKARAOKE_PASSWORD),
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
