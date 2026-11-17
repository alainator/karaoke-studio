#!/usr/bin/env python3
"""GPU helper: lets a second PC with an NVIDIA card make songs for the studio when the main GPU is busy.

    python -I helper.py [--host 0.0.0.0] [--port 5580]      (settings: .env, see .env.example: HELPER_*)

Runs the same pipeline.py as the studio, one job at a time, and serves the finished files back. Every request
must carry the shared secret (HELPER_TOKEN, header X-Helper-Token); HELPER_ALLOW can limit which addresses may
connect (the studio's). The helper downloads the video itself, so the PC needs internet access, ffmpeg and node.

    GET    /health                 -> {"ok", "gpu", "free_mib", "busy"}
    POST   /jobs                   -> {"id"}   body: the studio's job (yt, lrc_id, lyrics_source, artist, title, lang)
    GET    /jobs/<id>              -> {"status": running|done|failed, "step", "log", "error"}
    GET    /jobs/<id>/files/<name> -> out.mp4, vocals.m4a, words.json, lyrics.ass, result.json
    DELETE /jobs/<id>
"""
import argparse, hmac, json, os, re, shutil, subprocess, sys, threading, time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402

TOKEN = os.environ.get("HELPER_TOKEN", "")
ALLOW = {a.strip() for a in os.environ.get("HELPER_ALLOW", "").split(",") if a.strip()}
JOBS = os.path.join(C.JOBS, "helper")
FILES = ("out.mp4", "vocals.m4a", "words.json", "lyrics.ass", "result.json")
ID_RE = re.compile(r"^[0-9a-z-]{8,80}$")

os.makedirs(JOBS, exist_ok=True)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
state = {"busy": None}
jobs = {}
lock = threading.Lock()


@app.middleware("http")
async def guard(req: Request, call_next):
    if ALLOW and (req.client.host if req.client else "") not in ALLOW:
        return _deny(403, "not allowed from this address")
    if not TOKEN or not hmac.compare_digest(req.headers.get("x-helper-token", ""), TOKEN):
        return _deny(401, "bad token")
    return await call_next(req)


def _deny(code, msg):
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": msg}, status_code=code)


def gpu():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        name, tot, used = [x.strip() for x in r.stdout.splitlines()[0].split(",")]
        return name, int(tot) - int(used)
    except Exception:
        return "?", 0


@app.get("/health")
def health():
    name, free = gpu()
    return {"ok": True, "gpu": name, "free_mib": free, "busy": state["busy"] is not None, "version": C.code_version()}


def run(jid):
    d = os.path.join(JOBS, jid)
    p = subprocess.Popen([sys.executable, "-I", os.path.join(HERE, "pipeline.py"), d], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, env=dict(C.tool_env(), PYTHONUNBUFFERED="1"))
    oom = False
    for line in p.stdout:
        line = line.rstrip()
        oom = oom or "out of memory" in line.lower()
        if line.startswith("@"):
            kind, _, text = line[1:].partition(" ")
            with lock:
                j = jobs[jid]
                if kind == "step":
                    j["step"] = text
                elif kind == "info":
                    j["log"].append(text)
                elif kind == "error":
                    j["error"] = text
    rc = p.wait()
    with lock:
        j = jobs[jid]
        ok = rc == 0 and all(os.path.exists(os.path.join(d, f)) for f in FILES)
        j["status"] = "done" if ok else "failed"
        if not ok and not j.get("error"):
            j["error"] = "the helper's GPU ran out of memory" if oom else f"pipeline exited with code {rc}"
        j["oom"] = oom
        state["busy"] = None


@app.post("/jobs")
async def create(req: Request):
    b = await req.json()
    jid = str(b.get("id", ""))
    if not ID_RE.match(jid) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", str(b.get("yt", ""))):
        raise HTTPException(400, "bad job")
    with lock:
        if state["busy"]:
            raise HTTPException(409, "busy")
        d = os.path.join(JOBS, jid)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        spec = {k: b.get(k) for k in ("yt", "lrc_id", "lyrics_source", "artist", "title", "lang")}
        json.dump(spec, open(os.path.join(d, "job.json"), "w"))
        jobs[jid] = {"status": "running", "step": "lyrics", "log": [], "error": None, "started": time.time()}
        state["busy"] = jid
    threading.Thread(target=run, args=(jid,), daemon=True).start()
    return {"id": jid}


@app.get("/jobs/{jid}")
def status(jid: str):
    if jid not in jobs:
        raise HTTPException(404, "no such job")
    with lock:
        return dict(jobs[jid])


@app.get("/jobs/{jid}/files/{name}")
def file(jid: str, name: str):
    if jid not in jobs or name not in FILES or not ID_RE.match(jid):
        raise HTTPException(404)
    p = os.path.join(JOBS, jid, name)
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p)


@app.delete("/jobs/{jid}")
def delete(jid: str):
    if not ID_RE.match(jid):
        raise HTTPException(404)
    with lock:
        if state["busy"] == jid:
            raise HTTPException(409, "still running")
        jobs.pop(jid, None)
    shutil.rmtree(os.path.join(JOBS, jid), ignore_errors=True)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5580)
    a = ap.parse_args()
    if len(TOKEN) < 24:
        sys.exit("set HELPER_TOKEN in .env to a long random secret (the same value goes in the studio's .env)")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
