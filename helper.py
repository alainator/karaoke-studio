#!/usr/bin/env python3
"""GPU helper: lets a second PC with an NVIDIA card make songs for the studio when the main GPU is busy.

    python -I helper.py [--host 0.0.0.0] [--port 5580]      (settings: .env, see .env.example: HELPER_*)

Runs the same pipeline.py as the studio, one job at a time, and serves the finished files back. Every request
must carry the shared secret (HELPER_TOKEN, header X-Helper-Token); HELPER_ALLOW can limit which addresses may
connect (the studio's). The helper downloads the video itself, so the PC needs internet access, ffmpeg and node.

    GET    /health                 -> {"ok", "gpu", "free_mib", "util" (how busy the card is, %), "busy"}
    POST   /jobs                   -> {"id"}   body: the studio's job (yt, lrc_id, lyrics_source, artist, title, lang,
                                              lyrics_text when the lyrics were pasted, resume)
    GET    /jobs/<id>              -> {"status": running|done|failed|cancelled|paused, "step", "log", "error",
                                       "paused": why (paused: this PC's GPU was needed by another program; the studio
                                       queues the song again), "oom"}
    GET    /jobs/<id>/files/<name> -> out.mp4, vocals.m4a, words.json, lyrics.ass, result.json
    POST   /jobs/<id>/cancel       -> {"ok", "status"}  stop it (the pipeline and everything it started); the job ends
                                      "cancelled" a few seconds later
    DELETE /jobs/<id>
"""
import argparse, hmac, json, os, re, shutil, subprocess, sys, threading, time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config as C  # noqa: E402
import gpu as gpu_mod  # noqa: E402
import gpuwatch  # noqa: E402

TOKEN = os.environ.get("HELPER_TOKEN", "")
ALLOW = {a.strip() for a in os.environ.get("HELPER_ALLOW", "").split(",") if a.strip()}
JOBS = os.path.join(C.JOBS, "helper")
PIPELINE = os.path.join(HERE, "pipeline.py")  # (tests swap in a stand-in)
FILES = ("out.mp4", "vocals.m4a", "words.json", "lyrics.ass", "result.json")
OPTIONAL = ("heard.json",)  # what was heard (the by-ear check): sent back too, but a song is fine without it
ID_RE = re.compile(r"^[0-9a-z-]{8,80}$")

os.makedirs(JOBS, exist_ok=True)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
state = {"busy": None}
jobs = {}
procs = {}  # job id -> its running pipeline (Popen); kept out of jobs[], which is sent as JSON
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
    """(name, free MiB) of this PC's GPU (gpu.py)."""
    return gpu_mod.info()


def gpu_util():
    """How busy this PC's card is (%), or None (a game or another program computing on it: the studio waits)."""
    return (gpu_mod.snapshot() or {}).get("util")


@app.get("/health")
def health():
    name, free = gpu()
    return {"ok": True, "gpu": name, "free_mib": free, "util": gpu_util() if state["busy"] is None else None,
            "busy": state["busy"] is not None, "version": C.code_version()}


def _pause(jid, why):
    """Another program keeps taking this PC's card during the song (a game): stop it; the studio queues it again."""
    with lock:
        j, p = jobs.get(jid), procs.get(jid)
        if j is None or p is None or j.get("cancel") or j.get("_stopper"):
            return
        j["_pausing"] = why
        t = threading.Thread(target=C.stop_group, args=(p,), daemon=True)
        j["_stopper"] = t
        t.start()


def run(jid):
    d = os.path.join(JOBS, jid)
    with lock:  # cancelled before it even started: don't start it
        if jobs[jid].get("cancel"):
            jobs[jid].update(status="cancelled", error="cancelled")
            state["busy"] = None
            return
        # its own process group, so a cancel stops yt-dlp, ffmpeg and the GPU work it started too
        p = subprocess.Popen([sys.executable, "-I", PIPELINE, d], stdout=subprocess.PIPE, start_new_session=True,
                             stderr=subprocess.STDOUT, text=True, env=dict(C.tool_env(), PYTHONUNBUFFERED="1"))
        procs[jid] = p
        jobs[jid]["_own"] = 0
    oom = False
    C.job_log(d, f"run started on the helper, code {C.code_version()}", header=True)
    threading.Thread(target=gpuwatch.watch, daemon=True, name="gpu-watch", kwargs=dict(
        proc=p, reported=lambda: jobs[jid].get("_own", 0), step=lambda: jobs[jid].get("step"),
        pause=lambda why: _pause(jid, why))).start()
    for line in p.stdout:
        line = line.rstrip()
        if line.startswith("@gpu "):
            try:
                jobs[jid]["_own"] = int(line[5:])
            except ValueError:
                pass
            continue
        C.job_log(d, line)
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
                elif kind == "pause":
                    j["_pausing"] = text
    rc = p.wait()
    with lock:  # reaped: nothing left to stop
        procs.pop(jid, None)
        stopper = jobs[jid].get("_stopper")
    if stopper:
        stopper.join(15)  # a cancel: wait until everything the pipeline started is gone (the GPU is free again)
    with lock:
        procs.pop(jid, None)
        j = jobs[jid]
        j.pop("_stopper", None)
        ok = rc == 0 and all(os.path.exists(os.path.join(d, f)) for f in FILES)
        if j.get("cancel"):
            j.update(status="cancelled", error="cancelled")
            C.job_log(d, "cancelled", header=True)
        elif not ok and (j.get("_pausing") or rc == gpuwatch.PAUSE_EXIT):
            j.update(status="paused", paused=j.get("_pausing") or "no room on the GPU for the next step", error=None)
            C.job_log(d, f"paused: {j['paused']}", header=True)
        else:
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
        spec = {k: b.get(k) for k in ("yt", "lrc_id", "lyrics_source", "artist", "title", "lang", "lyrics_text", "resume")}
        if spec.get("lyrics_text") is not None:
            spec["lyrics_text"] = str(spec["lyrics_text"])[:20000]
        json.dump(spec, open(os.path.join(d, "job.json"), "w"))
        jobs[jid] = {"status": "running", "step": "lyrics", "log": [], "error": None, "started": time.time()}
        state["busy"] = jid
    threading.Thread(target=run, args=(jid,), daemon=True).start()
    return {"id": jid}


@app.post("/update")
async def update(req: Request):
    """The studio asks the helper to update itself (it saw different code): pull the studio's GitHub repo, install new
    packages if requirements.txt changed, then restart (systemd brings the service back). Refused while a job runs."""
    b = await req.json()
    want = str(b.get("want", ""))
    with lock:
        if state["busy"]:
            raise HTTPException(409, "busy")
        state["busy"] = "update"
    try:
        before = open(os.path.join(HERE, "requirements.txt")).read()
        r = subprocess.run(["git", "-C", HERE, "pull", "--ff-only", "-q"], capture_output=True, text=True, timeout=120)
        if r.returncode:
            raise HTTPException(500, "git pull failed: " + (r.stderr or r.stdout).strip()[-200:])
        if open(os.path.join(HERE, "requirements.txt")).read() != before:
            uv = shutil.which("uv") or "uv"
            r = subprocess.run([uv, "pip", "install", "--python", sys.executable, "-q", "-r", os.path.join(HERE, "requirements.txt")],
                               capture_output=True, text=True, timeout=1800)
            if r.returncode:
                raise HTTPException(500, "package install failed: " + (r.stderr or r.stdout).strip()[-200:])
        import importlib
        importlib.reload(C)
        now = C.code_version()
    finally:
        state["busy"] = None
    # restart into the new code shortly after answering (Restart=on-failure brings it back)
    threading.Timer(1.0, lambda: os._exit(3)).start()
    return {"ok": True, "version": now, "matches": now == want}


@app.post("/jobs/{jid}/cancel")
def cancel(jid: str):
    """Stop a job: SIGTERM its pipeline's process group, SIGKILL after 10 s. The job turns "cancelled" once the
    pipeline is gone (poll GET /jobs/<id>). Cancelling a finished or already-cancelled job changes nothing."""
    with lock:
        j = jobs.get(jid)
        if j is None:
            raise HTTPException(404, "no such job")
        if j["status"] != "running":
            return {"ok": True, "status": j["status"]}
        first = not j.get("cancel")
        j["cancel"] = True
        p = procs.get(jid)
        if first and p is not None:
            t = threading.Thread(target=C.stop_group, args=(p,), daemon=True)
            j["_stopper"] = t
            t.start()
    return {"ok": True, "status": "stopping"}


@app.get("/jobs/{jid}")
def status(jid: str):
    if jid not in jobs:
        raise HTTPException(404, "no such job")
    with lock:
        return {k: v for k, v in jobs[jid].items() if not k.startswith("_")}


@app.get("/jobs/{jid}/files/{name}")
def file(jid: str, name: str):
    if jid not in jobs or name not in FILES + OPTIONAL + ("pipeline.log",) or not ID_RE.match(jid):
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
