#!/usr/bin/env python3
"""Words scoring for players (contract §2.9): did the singer sing the song's words? A player sends its mic recording of
one song (Opus); the Studio aligns the words it expected against it and answers which words it heard.

How a word is judged: the same forced aligner that times the Studio's songs (torchaudio MMS_FA) places the word's
letters inside its window (its words.json time ± 0.8 s, mapped to the recording's time with X-Encore-Start and
X-Encore-Rate), hearing each line once with 1 s of context. The word's confidence is (the mean probability of its
letters where they were placed) × (how far each letter beats the best other letter at its strongest frame): silence
and other words give low values, the word sung gives high ones. A word counts as heard at THRESHOLD and above.
Which words are judged: only those the song's own separated vocals pass clearly (REF_MIN) and its instrumental (the
published video's sound) doesn't (below THRESHOLD). The others (sung too fast or buried, sung off their timing,
English spellings far from how they sound; or words the music alone already "passes", through vocals left in the
instrumental or a sound like a letter) are left out of the words and of the total: nobody gains or loses by them.
Both references are worked out once per song and kept in its job folder (wordscore.ref.json).
Calibrated 2026-11-20 on eight published songs (five English, three Japanese): their own separated vocals score 100
(by construction), their instrumentals 0 (by construction), another song's instrumental 1-9 (mean 5), silence 0, the
vocals with the instrumental 10 dB under them 87-99 (mean 93), through Opus at 40 kb/s (webm) 95-100 and 24 kb/s
(ogg) 93-99, cut to start at 30 s 100, sped up 1.25x 82-99 (mean 95); the vocals over another song's words and timing
8-26 (mean 18). Judged: about 70% of the words of the Japanese songs (romaji spells how they sound), 30% of English.

Scheduling: one job at a time, after anything else. The aligner runs in its own process (python wordscore.py serve
DEVICE), on the GPU only when no song is being made or waiting and the card has room for both (the app decides:
pick_device), else on the CPU (niced, 4 threads), else the job is `busy`. The process exits after KEEP_ALIVE seconds
without work, so the model never sits in VRAM; the app also stops it as soon as a song waits for the card.

Privacy: the recording stays in this process's memory (the job, while it waits and runs) and goes to ffmpeg and to
the aligner process through pipes: it is never written to disk, never logged, never stored with the song. It is
dropped as soon as the job is done, failed, found busy or expired, and results are forgotten after KEEP seconds.
"""
import hashlib, json, os, re, select, subprocess, sys, threading, time, uuid
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
SR = 16000
WINDOW = 0.8                    # seconds around each word's expected time (video time)
THRESHOLD = 0.25                # a word's confidence at which it counts as heard (calibrated: see above)
REF_MIN = 0.35                  # judged only if the original singer reaches this (a margin over THRESHOLD)
KEEP = 600                      # seconds a finished job's answer is kept
MAX_WAIT = 600                  # seconds a job may wait for its turn; then busy
MAX_BYTES = 8 * 1024 * 1024     # the recording (the only /api/encore/ body above 64 KB)
MAX_SECONDS = 20 * 60           # longest recording decoded
KEEP_ALIVE = float(os.environ.get("KARAOKE_WORDS_KEEP_ALIVE", "30"))
CPU_THREADS = 4
GPU_MIB = 2500                  # what the aligner needs on the card (measured: 1.7 GB peak)
CHUNK, PAD = 20.0, 1.0          # a line longer than 20 s is heard in pieces; each with 1 s of context each side
PENDING_MAX, PENDING_PER_PLAYER = 12, 3
FORMATS = {"audio/ogg": "ogg", "audio/webm": "webm"}
REF = "wordscore.ref.json"
REF_VERSION = 2
# measured 2026-11-20 on a 4-minute song: RTX 5090 0.5-0.9 s a pass (+ about 4 s to start the process and load
# the model); CPU (4 threads, niced) 27 s a pass. A song's first scoring makes 3 passes (its vocals, its
# instrumental, the recording), later ones 1.
GPU_START, GPU_PER_MIN, CPU_START, CPU_PER_MIN = 4.0, 0.25, 4.0, 7.0
CPU_OK = os.environ.get("KARAOKE_WORDS_CPU", "1").strip().lower() not in ("0", "no", "off", "false")
ENABLED = os.environ.get("KARAOKE_WORDS_SCORE", "1").strip().lower() not in ("0", "no", "off", "false")


# ---------------- what to score (no model needed) ----------------
def expected(words_doc, lines=None, key=None):
    """The words to score: [(line i, word index (0-based), sound key, start, end)] in video time, from a published
    words.json. lines: the line `i` values to score (None: every line but backing lines). Words without letters (key
    empty) are left out. key: text -> letters a-z and ' (romanize.sound_key)."""
    if key is None:
        from romanize import sound_key as key
    want = set(lines) if lines is not None else None
    out = []
    for l in words_doc.get("lines") or []:
        i = l.get("i")
        if want is not None and i not in want:
            continue
        if want is None and l.get("backing"):
            continue
        for k, w in enumerate(l.get("words") or []):
            try:
                a, b, text = float(w[0]), float(w[1]), str(w[2])
            except (TypeError, ValueError, IndexError):
                continue
            kk = key(text)
            if kk and b >= a:
                out.append((i, k, kk, a, b))
    return out


def plan(words, start, rate, seconds, window=WINDOW):
    """The words inside the recording, each with its window in the recording's time: [(line, w, key, r0, r1)].
    Recording time t is video time start + t * rate. A word counts only if all of it falls inside the recording."""
    out = []
    for (i, k, kk, a, b) in words:
        ra, rb = (a - start) / rate, (b - start) / rate
        if ra < 0 or rb > seconds:
            continue
        out.append((i, k, kk, max(0.0, (a - window - start) / rate), min(seconds, (b + window - start) / rate)))
    return out


def summarize(planned, conf, ref=None, threshold=THRESHOLD, ref_min=REF_MIN):
    """The answer for a scored recording. planned: plan(); conf: one confidence per planned word; ref: {(line, w):
    (the original vocals' confidence, the instrumental's or None)} or None (no references: every word is judged)."""
    words = []
    for (i, k, _, _, _), c in zip(planned, conf):
        if ref is not None:
            v, m = ref.get((i, k), (None, None))
            if v is None or v < ref_min or (m is not None and m >= threshold):
                continue  # the original singer doesn't pass it clearly, or the music alone does: not judged
        words.append({"line": i, "w": k, "ok": bool(c >= threshold), "conf": round(float(min(1.0, max(0.0, c))), 3)})
    heard = sum(1 for w in words if w["ok"])
    return {"words": words, "heard": heard, "total": len(words), "score": round(100 * heard / len(words)) if words else 0}


def parse_lines(value):
    """X-Encore-Lines: "3,4,7" -> [3, 4, 7]; None when absent; ValueError when malformed."""
    if value is None or not value.strip():
        return None
    out = []
    for x in value.split(","):
        x = x.strip()
        if not re.fullmatch(r"\d{1,5}", x):
            raise ValueError("X-Encore-Lines: comma-separated line numbers (words.json i)")
        out.append(int(x))
    if len(out) > 2000:
        raise ValueError("X-Encore-Lines: too many lines")
    return out


def media_format(content_type):
    """audio/ogg or audio/webm (codec parameters allowed) -> the ffmpeg demuxer, else None."""
    return FORMATS.get((content_type or "").split(";")[0].strip().lower())


def decode(raw, fmt, seconds=MAX_SECONDS, ffmpeg="ffmpeg"):
    """Opus in ogg/webm (bytes) -> 16 kHz mono float32 samples, through pipes (nothing written to disk)."""
    import numpy as np
    p = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-f", fmt, "-i", "pipe:0", "-t", f"{seconds:.1f}", "-vn",
                        "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"], input=bytes(raw), capture_output=True,
                       timeout=120)
    if p.returncode != 0 or not p.stdout:
        raise ValueError("couldn't read the audio (Opus in ogg or webm)")
    return np.frombuffer(p.stdout, dtype=np.float32)


def decode_file(path, seconds=MAX_SECONDS):
    """A song's own audio file (its separated vocals) -> 16 kHz mono float32."""
    import numpy as np
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-t", f"{seconds:.1f}", "-vn", "-ac", "1",
                        "-ar", str(SR), "-f", "f32le", "pipe:1"], capture_output=True, timeout=300)
    if p.returncode != 0:
        raise ValueError("couldn't read the song's vocals")
    return np.frombuffer(p.stdout, dtype=np.float32)


# ---------------- the aligner (in its own process: python wordscore.py serve DEVICE) ----------------
def _emission(model, device, audio, a, b):
    """(start, frames per second, log-probs (frames x tokens)) for recording seconds [a, b], heard with PAD seconds of
    context each side."""
    import torch
    n, pad = len(audio), int(PAD * SR)
    s0, s1 = max(0, int(a * SR)), min(n, int(round(b * SR)))
    c0, c1 = max(0, s0 - pad), min(n, s1 + pad)
    with torch.inference_mode():
        em, _ = model(torch.from_numpy(audio[c0:c1].copy()).unsqueeze(0).to(device))
    em = em[0].float().cpu()
    fps = em.shape[0] / ((c1 - c0) / SR)
    k0, k1 = int(round((s0 - c0) / SR * fps)), int(round((s1 - c0) / SR * fps))
    return s0 / SR, fps, em[k0:k1]


def _word_conf(em, tokens, r0, r1):
    """A word's confidence in its window [r0, r1]: the mean probability of its letters where the forced aligner puts
    them, times how much each letter beats the best other letter at its strongest frame."""
    import torch
    import torchaudio.functional as F
    t0, fps, em = em
    f0, f1 = max(0, int((r0 - t0) * fps)), min(em.shape[0], int(-(-(r1 - t0) * fps // 1)))
    seg = em[f0:f1]
    if seg.shape[0] < len(tokens) + 1:
        return 0.0
    try:
        al, sc = F.forced_align(seg.unsqueeze(0), torch.tensor([tokens], dtype=torch.int32), blank=0)
    except Exception:
        return 0.0
    parts = F.merge_tokens(al[0], sc[0].exp())
    if not parts:
        return 0.0
    p = seg.exp()
    mean_prob = sum(float(s.score) for s in parts) / len(parts)
    beats = []
    for s in parts:
        fr = p[s.start:s.end]
        j = int(fr[:, s.token].argmax())
        best = float(fr[j, 1:].max())
        beats.append(float(fr[j, s.token]) / best if best > 0 else 0.0)
    return mean_prob * (sum(beats) / len(beats))


def groups(words):
    """The words' windows grouped by line (the aligner hears each line once, with the same context for the singer and
    for the original): [(start, end, [word indexes])] in recording time; a line longer than CHUNK is cut."""
    by = {}
    for n, w in enumerate(words):
        by.setdefault(w[3], []).append(n)
    out = []
    for g in by.values():
        cur = []
        for n in sorted(g, key=lambda n: words[n][1]):
            if cur and words[n][2] - words[cur[0]][1] > CHUNK:
                out.append(cur)
                cur = []
            cur.append(n)
        if cur:
            out.append(cur)
    return [(min(words[n][1] for n in g), max(words[n][2] for n in g), g) for g in out]


def confidences(model, device, dictionary, audio, words):
    """words: [(key, r0, r1, line)] in recording seconds -> one confidence (0-1) per word."""
    out = [0.0] * len(words)
    for a, b, idx in groups(words):
        if b - a < 0.05:
            continue
        em = _emission(model, device, audio, a, b)
        for n in idx:
            toks = [dictionary[c] for c in words[n][0] if c in dictionary]
            out[n] = _word_conf(em, toks, words[n][1], words[n][2]) if toks else 0.0
    return out


def serve(device, keep_alive=KEEP_ALIVE):
    """The aligner process: requests on stdin (a JSON line {"n": bytes, "words": [[key, r0, r1, line]...]} then n bytes of
    float32 audio), one JSON line answer each ({"conf": [...]} or {"error": "..."}). Exits on EOF or after keep_alive
    seconds without a request."""
    try:
        os.nice(10)
    except OSError:
        pass
    import numpy as np
    import torch, torchaudio
    torch.set_num_threads(CPU_THREADS)
    t0 = time.time()
    fa = torchaudio.pipelines.MMS_FA
    model = fa.get_model(with_star=False).to(device).eval()
    dictionary = fa.get_dict(star=None)
    out, inp = sys.stdout, sys.stdin.buffer
    out.write(json.dumps({"ready": True, "load": round(time.time() - t0, 2)}) + "\n")
    out.flush()
    while True:
        r, _, _ = select.select([inp], [], [], keep_alive)
        if not r:
            break
        line = inp.readline()
        if not line:
            break
        try:
            req = json.loads(line)
            raw = inp.read(int(req["n"]))
            audio = np.frombuffer(raw, dtype=np.float32)
            t = time.time()
            conf = confidences(model, device, dictionary, audio, req["words"])
            ans = {"conf": [round(c, 4) for c in conf], "secs": round(time.time() - t, 2)}
        except Exception as e:  # (never the audio or the words: only what went wrong)
            ans = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
        raw = audio = None
        out.write(json.dumps(ans) + "\n")
        out.flush()
        if device != "cpu":
            try:
                import gpu
                gpu.empty_cache(device)
            except Exception:
                pass


class Worker:
    """The aligner process, seen from the app: started for one device, asked over pipes, gone after its keep-alive."""

    def __init__(self, cmd, device, keep_alive=KEEP_ALIVE):
        self.device = device
        self.err = deque(maxlen=20)
        self.p = subprocess.Popen(cmd + [device, str(keep_alive)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, cwd=HERE)
        threading.Thread(target=self._drain, daemon=True).start()
        ready = self._read(timeout=300)
        if not ready or not ready.get("ready"):
            self.stop()
            raise RuntimeError("the aligner didn't start" + (": " + self.err[-1][:200] if self.err else ""))
        self.load = ready.get("load")

    def _drain(self):
        for line in self.p.stderr:
            self.err.append(line.decode("utf-8", "replace").strip())

    def _read(self, timeout):
        r, _, _ = select.select([self.p.stdout], [], [], timeout)
        if not r:
            return None
        line = self.p.stdout.readline()
        return json.loads(line) if line else None

    def alive(self):
        return self.p.poll() is None

    def ask(self, audio, words, timeout=900):
        """words: [(key, r0, r1, line)] -> confidences. Raises when the process is gone or fails."""
        raw = audio.astype("float32").tobytes()
        try:
            self.p.stdin.write((json.dumps({"n": len(raw), "words": [list(w) for w in words]}) + "\n").encode())
            self.p.stdin.write(raw)
            self.p.stdin.flush()
        except (BrokenPipeError, OSError):
            raise RuntimeError("the aligner stopped")
        finally:
            raw = None
        ans = self._read(timeout)
        if ans is None:
            self.stop()
            raise RuntimeError("the aligner didn't answer")
        if "error" in ans:
            raise RuntimeError("the aligner failed: " + ans["error"])
        return ans["conf"]

    def stop(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait()


# ---------------- the jobs (in the app) ----------------
class Scorer:
    """Words-scoring jobs. song(id, base) -> {"words": words.json doc, "duration", "vocals": path or None, "ref_dir":
    folder for the reference cache or None} or None; pick_device() -> a torch device name, "cpu", or None (busy).
    Jobs are kept in memory only (results for KEEP seconds)."""

    def __init__(self, song, pick_device, log=None, cmd=None, now=time.time, keep_alive=None):
        self.song, self.pick_device, self.log, self.now = song, pick_device, log, now
        self.cmd = cmd or [sys.executable, "-I", os.path.abspath(__file__), "serve"]
        self.keep_alive = KEEP_ALIVE if keep_alive is None else keep_alive
        self.jobs, self.queue = {}, deque()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.worker = None
        self.busy = False           # a job is running now
        self.thread = None
        self.ran = {"gpu": None, "cpu": None}   # the last job's seconds per minute of recording, per kind
        self.new_id = lambda: uuid.uuid4().hex

    # -- jobs
    def submit(self, player, raw, fmt, song_id, base, start, rate, lines, duration=None):
        """A new job (the recording stays in memory until it's scored). Returns (job id, eta seconds, status)."""
        self.purge()
        jid = self.new_id()
        now = self.now()
        try:  # (for the eta: a song's first scoring also hears its own vocals and instrumental)
            info = self.song(song_id, base)
            passes = 1 if info is None or self.ref_cached(info) else 3
        except Exception:
            passes = 3
        j = {"id": jid, "player": player, "status": "queued", "created": now, "updated": now, "audio": raw, "fmt": fmt,
             "song": song_id, "base": base, "start": start, "rate": rate, "lines": lines, "duration": duration,
             "passes": passes, "result": None, "reason": None}
        with self.lock:
            pending = [x for x in self.jobs.values() if x["status"] in ("queued", "running")]
            device = self.pick_device() if ENABLED else None
            if not ENABLED:
                self._end(j, "busy", reason="words scoring is off on this Studio")
            elif device is None:
                self._end(j, "busy", reason="the Studio's graphics card is busy")
            elif len(pending) >= PENDING_MAX:
                self._end(j, "busy", reason="too many recordings waiting")
            else:
                self.queue.append(jid)
            self.jobs[jid] = j
            eta = self.eta(jid) if j["status"] == "queued" else 0
        if j["status"] == "queued":
            self._start()
            self.wake.set()
        return jid, eta, j["status"]

    def answer_busy(self, player, song_id, base):
        """A job answered busy at once, before its recording was read (nothing kept). Returns its id."""
        self.purge()
        now = self.now()
        j = {"id": self.new_id(), "player": player, "status": "busy", "created": now, "updated": now, "audio": None,
             "song": song_id, "base": base, "result": None,
             "reason": "words scoring is off on this Studio" if not ENABLED else "the Studio's graphics card is busy"}
        with self.lock:
            self.jobs[j["id"]] = j
        return j["id"]

    def pending_of(self, player):
        with self.lock:
            return sum(1 for x in self.jobs.values() if x["player"] == player and x["status"] in ("queued", "running"))

    def get(self, player, jid):
        """The job's answer for its own player, or None (unknown, forgotten, or another player's)."""
        self.purge()
        with self.lock:
            j = self.jobs.get(jid)
            if not j or j["player"] != player:
                return None
            out = {"status": j["status"]}
            if j["status"] in ("queued", "running"):
                out["eta"] = self.eta(jid)
            elif j["status"] == "done":
                out.update(j["result"])
            else:
                out["reason"] = j["reason"]
            return out

    def eta(self, jid):
        """Seconds until this job should be done (rough: its turn, the aligner's start, the work)."""
        with self.lock:
            ahead = [self.jobs[x] for x in self.queue if x in self.jobs]
            cur = [x for x in self.jobs.values() if x["status"] == "running"]
            w = self.worker if self.worker is not None and self.worker.alive() else None
        dev = w.device if w is not None else (self.pick_device() or "cpu")
        gpu = dev != "cpu"
        per = (self.ran["gpu"] or GPU_PER_MIN) if gpu else (self.ran["cpu"] or CPU_PER_MIN)
        total = 0.0 if w is not None else (GPU_START if gpu else CPU_START)
        for x in cur + ahead:
            total += per * self._minutes(x) * x.get("passes", 1)
            if x["id"] == jid:
                break
        return max(1, int(round(total)))

    @staticmethod
    def _minutes(j):
        if j.get("duration"):
            return max(0.5, j["duration"] / max(0.25, j.get("rate") or 1.0) / 60)
        return max(0.5, len(j.get("audio") or b"") / 6000 / 60)  # (Opus at ~48 kb/s)

    def _end(self, j, status, result=None, reason=None):
        j.update(status=status, result=result, reason=reason, audio=None, updated=self.now())

    def purge(self):
        """Forget answers older than KEEP; a job that waited longer than MAX_WAIT ends busy (its audio dropped)."""
        now = self.now()
        with self.lock:
            for jid, j in list(self.jobs.items()):
                if j["status"] == "queued" and now - j["created"] > MAX_WAIT:
                    self._end(j, "busy", reason="waited too long")
                    try:
                        self.queue.remove(jid)
                    except ValueError:
                        pass
                if j["status"] in ("done", "failed", "busy") and now - j["updated"] > KEEP:
                    del self.jobs[jid]

    def held_audio(self):
        """How many jobs still hold a recording (tests: none once they're done)."""
        with self.lock:
            return sum(1 for j in self.jobs.values() if j.get("audio") is not None)

    # -- the worker thread
    def _start(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, daemon=True, name="wordscore")
                self.thread.start()

    def _loop(self):
        while True:
            self.wake.wait(5)
            self.wake.clear()
            while self.run_once():
                pass
            self.purge()
            self.idle_check()

    def idle_check(self):
        """Stop an idle aligner that may not use its device any more (a song waits for the card), or that exited."""
        with self.lock:
            w = self.worker
            if w is None or self.busy:
                return
            if not w.alive():
                self.worker = None
                return
            if w.device != "cpu" and self.pick_device() != w.device:
                self.worker = None
        if w is not None and self.worker is None and w.alive():
            w.stop()

    def release_gpu(self):
        """A song wants the card: an idle aligner on the GPU stops now (a running job finishes in seconds)."""
        with self.lock:
            w = self.worker
            if w is None or w.device == "cpu" or self.busy:
                return
            self.worker = None
        w.stop()

    def run_once(self):
        """Run the next job. False when there was none."""
        with self.lock:
            while self.queue and self.queue[0] not in self.jobs:
                self.queue.popleft()
            if not self.queue:
                return False
            j = self.jobs[self.queue.popleft()]
            if j["status"] != "queued":
                return True
            device = self.pick_device() if ENABLED else None
            if device is None:
                self._end(j, "busy", reason="the Studio's graphics card is busy")
                return True
            j.update(status="running", updated=self.now())
            self.busy = True
        t0 = time.time()
        try:
            result = self._score(j, device)
            with self.lock:
                if result["total"] == 0:
                    self._end(j, "failed", reason="no words to score in this recording")
                else:
                    self._end(j, "done", result=result)
            if self.log:
                self.log.info("players: words scored for %s on the %s in %.1f s: %s of %s", j["base"][:80],
                              "CPU" if device == "cpu" else "GPU", time.time() - t0, result["heard"], result["total"])
        except Exception as e:
            with self.lock:
                self._end(j, "failed", reason=str(e)[:160] or type(e).__name__)
            if self.log:
                self.log.warning("players: words scoring failed for %s (%s)", j["base"][:80], str(e)[:160])
        finally:
            with self.lock:
                self.busy = False
                j["audio"] = None
        return True

    def _aligner(self, device):
        old = None
        with self.lock:
            if self.worker is not None and (self.worker.device != device or not self.worker.alive()):
                old, self.worker = self.worker, None
        if old is not None:
            old.stop()
        if self.worker is None:
            self.worker = Worker(self.cmd, device, self.keep_alive)
        return self.worker

    def _ask(self, device, audio, words):
        t = time.time()
        try:
            conf = self._aligner(device).ask(audio, words)
        except RuntimeError:  # the process ended between jobs (its keep-alive): once more with a new one
            self.worker = None
            conf = self._aligner(device).ask(audio, words)
        mins = len(audio) / SR / 60
        if mins > 0.5:
            self.ran["cpu" if device == "cpu" else "gpu"] = (time.time() - t) / mins
        return conf

    def _score(self, j, device):
        s = self.song(j["song"], j["base"])
        if not s:
            raise ValueError("the song isn't in the library any more")
        dur = s.get("duration")
        limit = min(MAX_SECONDS, ((dur - j["start"]) / j["rate"] + 30) if dur else MAX_SECONDS)
        audio = decode(j["audio"], j["fmt"], max(5.0, limit))
        j["audio"] = None   # (decoded: the bytes go)
        try:
            planned = plan(expected(s["words"], j["lines"]), j["start"], j["rate"], len(audio) / SR)
            if not planned:
                return {"words": [], "heard": 0, "total": 0, "score": 0}
            ref = self.reference(s, device)
            conf = self._ask(device, audio, [(kk, r0, r1, i) for (i, _, kk, r0, r1) in planned])
        finally:
            audio = None
        return summarize(planned, conf, ref)

    @staticmethod
    def _ref_files(s):
        """(vocals or None, instrumental or None, the cache stamp, the cache file or None) for a song."""
        voc, mus = s.get("vocals"), s.get("music")
        if not voc or not os.path.exists(voc):
            return None, None, None, None
        if mus and not os.path.exists(mus):
            mus = None
        st = [[os.stat(f).st_size, int(os.stat(f).st_mtime)] if f else None for f in (voc, mus)]
        stamp = hashlib.sha256(json.dumps([st, s["words"].get("lines")], sort_keys=True, ensure_ascii=False)
                               .encode()).hexdigest()[:24] + f"-{REF_VERSION}"
        return voc, mus, stamp, os.path.join(s["ref_dir"], REF) if s.get("ref_dir") else None

    def ref_cached(self, s):
        """Are the song's references already worked out (or not needed: no vocals)?"""
        voc, _, stamp, cache = self._ref_files(s)
        if not voc:
            return True
        try:
            return bool(cache) and json.load(open(cache, encoding="utf-8")).get("stamp") == stamp
        except (OSError, ValueError, AttributeError):
            return False

    def reference(self, s, device):
        """{(line, w): (the original vocals' confidence, the instrumental's or None)} for every word of the song (cached
        in its job folder until the vocals, the video or the words change), or None without the song's vocals."""
        voc, mus, stamp, cache = self._ref_files(s)
        if not voc:
            return None
        if cache and os.path.exists(cache):
            try:
                d = json.load(open(cache, encoding="utf-8"))
                if d.get("stamp") == stamp:
                    return {tuple(int(x) for x in k.split(":")): tuple(v) for k, v in d["conf"].items()}
            except (OSError, ValueError, KeyError, TypeError):
                pass
        every = expected(s["words"], [l.get("i") for l in s["words"].get("lines") or []])
        out = {}
        for n, f in enumerate((voc, mus)):
            if not f:
                continue
            try:
                audio = decode_file(f)
            except (ValueError, OSError, subprocess.SubprocessError):
                audio = None
            if audio is None or not len(audio):
                if n == 0:
                    return None  # (no usable vocals: every word is judged)
                continue
            pl = plan(every, 0.0, 1.0, len(audio) / SR)
            conf = self._ask(device, audio, [(kk, r0, r1, i) for (i, _, kk, r0, r1) in pl]) if pl else []
            audio = None
            for (i, k, _, _, _), c in zip(pl, conf):
                out.setdefault((i, k), [None, None])[n] = round(c, 4)
        ref = {k: tuple(v) for k, v in out.items()}
        if cache:
            try:
                tmp = cache + ".part"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"stamp": stamp, "conf": {f"{i}:{k}": list(v) for (i, k), v in ref.items()}}, f,
                              separators=(",", ":"))
                os.replace(tmp, cache)
            except OSError:
                pass
        return ref


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "serve":
        sys.path.insert(0, HERE)
        serve(sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else KEEP_ALIVE)
    else:
        sys.exit("usage: wordscore.py serve cuda|cpu|<torch device> [keep-alive seconds]")
