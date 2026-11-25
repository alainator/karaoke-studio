"""Sharing the GPU with other programs: Ollama (LLMs), ComfyUI, games and streaming, on the same card as the studio.
A queue is a queue: a song waits until the card has room, and gives the card back when another program needs it.

- Before a song starts (app.schedule_once): the card must have MIN_GPU_FREE_MIB free and not be busy computing for
  someone else (GPU_BUSY_PCT). Otherwise the song waits, saying who holds the card ("Ollama (gemma4:26b, 19 GB)").
- Before each step that loads a model (pipeline.py, gate()): that step's room (GPU_STEP_MIB). Without it, after a short
  wait, the pipeline stops with PAUSE_EXIT and "@pause <why>".
- While a song is made (watch()): when other programs' memory grows by GPU_PREEMPT_MIB and stays so for
  GPU_PREEMPT_GRACE_S, the pipeline is stopped. The studio's own memory comes from the pipeline ("@gpu <MiB>", see
  report()) because inside a container the driver doesn't list processes; where it does, the list is used.
- Running out of memory during a step counts as a pause too, never as a failed song (app._finish).
A paused song goes back to the front of its queue (app.queue_order) and starts again once the card has had room for
GPU_RESUME_S. The steps it had done are reused: downloads and stems are cached by video (KARAOKE_WORK), and what the
GPU worked out (language, Whisper's timing and transcription, the pitch, the talking check) is kept by Memo until the
song is made.

This module never imports torch (gpu.py does the asking), so the web app and the helper don't open a GPU context.
"""
import hashlib, json, os, re, shutil, sys, threading, time

import config as C
import gpu

PAUSE_EXIT = 75          # pipeline.py's exit code for "paused for the GPU" (EX_TEMPFAIL)
TINY_MIB = 300           # programs holding less than this aren't worth naming
GPU_STEPS = ("separate", "align", "render")   # the steps that hold the card (render: the talking check)


def gb(mib):
    """MiB as GB for people: 19, 8.5, 0.6."""
    return f"{mib / 1024:.0f}" if mib >= 10 * 1024 else f"{mib / 1024:.1f}".rstrip("0").rstrip(".") or "0"


# ---------------- who holds the card ----------------
def others(snap, own_mib=0, own_pids=(), ollama=None):
    """Who else holds memory on the card, in words ("Ollama (gemma4:26b, 19 GB)", "ComfyUI 6 GB, python3 2 GB",
    "other programs (12 GB)"), or None. snap: gpu.snapshot(); own_mib/own_pids: the studio's own use; ollama: the
    models Ollama has loaded ([{"name", "size_vram"}], its /api/ps), when it's set up."""
    if not snap:
        return None
    apps = [(pid, name, mib) for pid, name, mib in snap.get("apps") or [] if pid not in own_pids and mib >= TINY_MIB]
    if apps:
        out = []
        for _, name, mib in sorted(apps, key=lambda a: -a[2])[:3]:
            nm = os.path.basename(name or "") or "a program"
            out.append(("Ollama" if "ollama" in (name or "").lower() else nm) + f" {gb(mib)} GB")
        return ", ".join(out)
    rest = max(0, snap["used"] - own_mib)
    models = [m for m in ollama or [] if (m.get("size_vram") or 0) >> 20 >= TINY_MIB]
    if models:
        held = sum(m["size_vram"] >> 20 for m in models)
        names = ", ".join(str(m.get("name") or "?") for m in models[:2])
        more = rest - held
        return f"Ollama ({names}, {gb(held)} GB)" + (f" and other programs ({gb(more)} GB)" if more >= 1024 else "")
    return f"other programs ({gb(rest)} GB)" if rest >= 1024 else None


def using(who):
    """ "Ollama (gemma4:26b, 19 GB) is using it", "ComfyUI 6 GB, python3 2 GB are using it"."""
    bare = re.sub(r"\([^)]*\)", "", who)
    return f"{who} {'are' if ', ' in bare or ' and ' in bare or bare.startswith('other') else 'is'} using it"


def check_start(snap, free, need, busy_pct=None, own_busy=False):
    """Why a song can't start on this card now (None: it can). free: its free MiB; need: what a song needs; snap: for
    how busy it is (None: can't be told, which never holds a song back); own_busy: the studio itself is computing on
    it (the pitch worker, words scoring), so a busy card says nothing about other programs."""
    if free < need:
        return f"{gb(free)} GB free, a song needs {gb(need)}"
    busy_pct = C.GPU_BUSY_PCT if busy_pct is None else busy_pct
    util = (snap or {}).get("util")
    if busy_pct and util is not None and util >= busy_pct and not own_busy:
        return f"it's {util}% busy"
    return None


# ---------------- pressure while a song is made ----------------
class Pressure:
    """Is another program taking the card while a song is made? feed() every few seconds with the card's used memory
    and the studio's own; it returns why once the others' memory has stayed `jump` MiB above the least they held
    during this song for `grace` seconds (a model loading for a moment doesn't count)."""

    def __init__(self, jump=None, grace=None):
        self.jump = C.GPU_PREEMPT_MIB if jump is None else jump
        self.grace = C.GPU_PREEMPT_GRACE_S if grace is None else grace
        self.base = None    # the least the other programs held (over two looks in a row)
        self.prev = None
        self.since = None   # since when they've held `jump` more

    def feed(self, now, used, own):
        if not self.jump:
            return None
        rest = max(0, used - own)
        # The studio's own figure comes a second or two late: just after it frees a model, the card shows the memory
        # gone while the pipeline still says it holds it, and the others look smaller than they are for one look. So
        # the baseline only drops to what two looks in a row agree on.
        low = rest if self.prev is None else max(rest, self.prev)
        self.prev = rest
        if self.base is None or low < self.base:
            self.base = low
        if rest - self.base >= self.jump:
            self.since = self.since if self.since is not None else now
            if now - self.since >= self.grace:
                return f"another program took {gb(rest - self.base)} GB of the GPU"
        else:
            self.since = None
        return None


def own_mib(snap, pgid, reported):
    """The studio's own memory on the card for the pipeline whose process group is pgid: from the driver's process
    list when it shows the pipeline's processes (outside containers), else what the pipeline reported ("@gpu")."""
    mine = 0
    for pid, _, mib in (snap or {}).get("apps") or []:
        try:
            if os.getpgid(pid) == pgid:
                mine += mib
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return mine or (reported or 0)


def watch(proc, reported, step, pause, snapshot=None, every=5.0, meter=None, alive=None):
    """Thread body, while one pipeline runs: proc its Popen (start_new_session, so its pid is its group); reported()
    -> the MiB it last said it holds; step() -> the step it's on; pause(why) is called once, when another program
    keeps taking the card during a step that holds it. Returns when the pipeline ends or after pausing it."""
    snapshot = snapshot or gpu.snapshot
    meter = meter or Pressure()
    alive = alive or (lambda: proc.poll() is None)
    if not meter.jump:
        return
    while alive():
        time.sleep(every)
        if not alive():
            return
        snap = snapshot()
        if not snap:
            continue
        why = meter.feed(time.time(), snap["used"], own_mib(snap, proc.pid, reported()))
        if why and step() in GPU_STEPS:
            pause(why)
            return


# ---------------- in pipeline.py ----------------
_OUT = threading.Lock()


def say(kind, text=""):
    """One progress line ("@kind text") in a single write, so the reporting thread's lines never cut into another."""
    with _OUT:
        sys.stdout.write(f"@{kind} {text}".rstrip() + "\n")
        sys.stdout.flush()


def report(every=1.0):
    """Start a thread saying "@gpu <MiB>" (what this process holds on the card, with the context allowance) whenever
    it changes by 256 MiB or more, and every 30 s; nothing until the process uses the GPU."""
    def go():
        last, at = None, 0.0
        while True:
            time.sleep(every)
            held = gpu.held_mib()
            mib = held + C.GPU_CONTEXT_MIB if held or gpu._torch_gpu() is not None else 0
            if mib and (last is None or abs(mib - last) >= 256 or time.time() - at >= 30):
                say("gpu", str(mib))
                last, at = mib, time.time()
    threading.Thread(target=go, daemon=True, name="gpu-report").start()


def gate(what, step, wait_s=30.0, every=5.0, snapshot=None, sleep=time.sleep, out=None):
    """Before a step loads a model: does the card have its room (C.GPU_STEP_MIB[step])? Waits up to wait_s for it,
    then stops the pipeline (PAUSE_EXIT, "@pause <why>"): the studio puts the song back at the front of the queue and
    starts it again, reusing what's done, when the card has room. A card whose memory can't be read never stops it."""
    need = C.GPU_STEP_MIB.get(step, 0)
    if not need or os.environ.get("KARAOKE_DEVICE", "").strip().lower() == "cpu":
        return True
    snapshot = snapshot or gpu.snapshot
    out = out or say
    end = time.time() + wait_s
    while True:
        snap = snapshot()
        if not snap:
            return True
        room = snap["free"] + gpu.spare_mib()
        if room >= need:
            return True
        if time.time() >= end:
            out("pause", f"the GPU has {gb(room)} GB free and {what} needs {gb(need)}")
            sys.exit(PAUSE_EXIT)
        sleep(every)


class Memo:
    """What the GPU steps worked out for one song, kept until it's made, so a song paused for the GPU (or cut short by
    a restart) doesn't listen again: <KARAOKE_WORK>/resume/<job id>/<name>.json, each under a key of everything it
    depends on (the audio file, the lyrics, the language, the model, the pipeline's version). Always written after
    a step works; read only when the song is resumed (use=True); clear() when the song is done."""

    def __init__(self, folder, use, salt=()):
        self.folder, self.use, self.salt = folder, bool(use), list(salt)
        self.used = []   # names taken from the cache this run (said in the log)

    def key(self, parts):
        return hashlib.sha1(json.dumps([self.salt, parts], ensure_ascii=False, default=str).encode()).hexdigest()[:20]

    def get(self, name, parts, fn):
        """fn()'s value (JSON-able), from the cache when resuming and the key matches."""
        p = os.path.join(self.folder, name + ".json")
        k = self.key(parts)
        if self.use:
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("key") == k:
                    self.used.append(name)
                    return d["value"]
            except (OSError, ValueError, KeyError):
                pass
        v = fn()
        try:   # (never fails the song: at worst a resume works it out again)
            os.makedirs(self.folder, exist_ok=True)
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"key": k, "value": v}, f, ensure_ascii=False, default=_plain)
            os.replace(p + ".tmp", p)
        except Exception:
            try:
                os.remove(p + ".tmp")
            except OSError:
                pass
        return v

    def clear(self):
        shutil.rmtree(self.folder, ignore_errors=True)


def _plain(o):
    """numpy numbers (and the like) as plain ones for JSON."""
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o).__name__)


def resume_dir(work, jid):
    return os.path.join(work, "resume", os.path.basename(jid))


LEFTOVER = re.compile(r"^(sep|bk)\d+-(vocals|inst|lead|backing)\.wav$")


def clean_resume(work, days=7):
    """Remove resume caches of songs nobody resumed for a week (cancelled, deleted, or resumed on the other PC), and
    the separator's half-written tracks of a pipeline stopped during separation (an hour old: never a running one's)."""
    try:
        for f in os.listdir(work):
            p = os.path.join(work, f)
            if LEFTOVER.match(f) and time.time() - os.path.getmtime(p) > 3600:
                os.remove(p)
    except OSError:
        pass
    root = os.path.join(work, "resume")
    try:
        for d in os.listdir(root):
            p = os.path.join(root, d)
            if time.time() - os.path.getmtime(p) > days * 86400:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass
