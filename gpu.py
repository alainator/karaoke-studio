"""The one place that knows about graphics cards: which device the models run on, how much GPU memory is free, and
what the card is called. Everything else asks here instead of saying "cuda" or calling nvidia-smi itself.

Tested on NVIDIA only (see CONTRIBUTING.md, "Hardware"). Other backends are seams for contributors:
- AMD (ROCm builds of PyTorch) reuse the "cuda" device name, so device() already says "cuda" there; free memory and
  the name come from rocm-smi when nvidia-smi isn't installed.
- Intel (XPU) and Apple (MPS) are picked when PyTorch reports them; their free memory isn't known yet (0).
- KARAOKE_DEVICE=cuda|cuda:1|xpu|mps|cpu forces a device (cpu: very slow, for trying things out).

The memory and name queries run command-line tools and never import torch, so the web app and the GPU helper (which
don't load models) don't start a GPU context of their own.
"""
import json, os, shutil, subprocess, sys

ENV = "KARAOKE_DEVICE"


def _run(cmd, timeout=10):
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _nvidia():
    """[(name, total MiB, used MiB)] per NVIDIA card, or None without nvidia-smi."""
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"])
    if not out:
        return None
    cards = []
    for row in out.strip().splitlines():
        try:
            name, tot, used = [x.strip() for x in row.rsplit(",", 2)]
            cards.append((name, int(float(tot)), int(float(used))))
        except ValueError:
            continue
    return cards or None


def _rocm():
    """[(name, total MiB, used MiB)] per AMD card (rocm-smi --json), or None. Untested on real hardware."""
    out = _run(["rocm-smi", "--showmeminfo", "vram", "--showproductname", "--json"])
    if not out:
        return None
    try:
        d = json.loads(out)
    except ValueError:
        return None
    cards = []
    for k in sorted(d):
        v = d[k] if isinstance(d[k], dict) else {}
        try:
            tot = int(v.get("VRAM Total Memory (B)")) // (1 << 20)
            used = int(v.get("VRAM Total Used Memory (B)")) // (1 << 20)
        except (TypeError, ValueError):
            continue
        cards.append((str(v.get("Card series") or v.get("Card SKU") or "AMD GPU"), tot, used))
    return cards or None


def _cards():
    return _nvidia() or _rocm() or []


def free_mib():
    """Free memory on the first GPU, in MiB (0 when it can't be told: no GPU, or a backend without a query yet).
    The studio waits for this much before it starts a song (MIN_GPU_FREE_MIB)."""
    c = _cards()
    return c[0][1] - c[0][2] if c else 0


def _num(x):
    try:
        return int(float(x.strip()))
    except (ValueError, AttributeError):
        return None   # "[N/A]", "[Not Supported]"


def parse_query(out):
    """nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu (csv, no header/units): the first card as
    {"name", "total", "used", "free" (MiB), "util" (percent, None if not reported)}, or None."""
    for row in (out or "").strip().splitlines():
        parts = [x.strip() for x in row.rsplit(",", 3)]
        if len(parts) == 3:   # (a driver or a stand-in without the utilisation field)
            parts.append(None)
        if len(parts) != 4:
            continue
        nm, tot, used, util = parts[0], _num(parts[1]), _num(parts[2]), _num(parts[3]) if parts[3] is not None else None
        if tot is None or used is None:
            continue
        return {"name": nm, "total": tot, "used": used, "free": tot - used, "util": util}
    return None


def parse_apps(out):
    """nvidia-smi --query-compute-apps=pid,process_name,used_memory (csv, no header/units) -> [(pid, name, MiB)].
    Inside a container this is usually empty (the driver's process ids aren't in the container's namespace)."""
    apps = []
    for row in (out or "").strip().splitlines():
        parts = [x.strip() for x in row.split(",")]
        if len(parts) < 3:
            continue
        pid, mib = _num(parts[0]), _num(parts[-1])
        if pid is None or mib is None:
            continue
        apps.append((pid, ",".join(parts[1:-1]).strip(), mib))
    return apps


def snapshot():
    """The first GPU right now: {"name", "total", "used", "free" (MiB), "util" (percent or None), "apps": [(pid, name,
    MiB)] (programs on it, when the driver lets this process see them; else [])}, or None when it can't be told (no
    nvidia-smi / rocm-smi, or an Intel/Apple GPU). The studio's GPU check (gpuwatch.py) reads this."""
    snap = parse_query(_run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                             "--format=csv,noheader,nounits"]))
    if snap:
        snap["apps"] = parse_apps(_run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                                        "--format=csv,noheader,nounits"]))
        return snap
    c = _rocm()
    if c:
        return {"name": c[0][0], "total": c[0][1], "used": c[0][2], "free": c[0][1] - c[0][2], "util": None, "apps": []}
    return None


def name():
    """The first GPU's name ("NVIDIA GeForce RTX 3090"), or None."""
    c = _cards()
    return c[0][0] if c else None


def info():
    """(name or "?", free MiB): what the GPU helper reports."""
    c = _cards()
    return (c[0][0], c[0][1] - c[0][2]) if c else ("?", 0)


def device(cpu_ok=True):
    """The torch device for the models: KARAOKE_DEVICE if set, else CUDA (NVIDIA, or AMD through ROCm), else Intel
    XPU, else Apple MPS. Without any: "cpu" if cpu_ok, else an error (making a song on the CPU would take hours, so
    the pipeline asks for a GPU; set KARAOKE_DEVICE=cpu to insist)."""
    forced = os.environ.get(ENV, "").strip()
    if forced:
        return forced
    import torch
    if torch.cuda.is_available():
        return "cuda"
    xpu = getattr(torch, "xpu", None)
    if xpu is not None and getattr(xpu, "is_available", lambda: False)():
        return "xpu"
    mps = getattr(getattr(torch, "backends", None), "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    if cpu_ok:
        return "cpu"
    raise RuntimeError("no GPU found for PyTorch (CUDA, ROCm, XPU or MPS); set KARAOKE_DEVICE=cpu to run on the CPU (very slow)")


def empty_cache(dev=None):
    """Give cached GPU memory back between models (torch.cuda.empty_cache() and its XPU/MPS equivalents)."""
    import torch
    kind = str(dev or device()).split(":")[0]
    mod = {"cuda": getattr(torch, "cuda", None), "xpu": getattr(torch, "xpu", None), "mps": getattr(torch, "mps", None)}.get(kind)
    fn = getattr(mod, "empty_cache", None) if mod is not None else None
    if fn is not None:
        try:
            fn()
        except RuntimeError:
            pass


def is_gpu(dev):
    return str(dev).split(":")[0] in ("cuda", "xpu", "mps")


# ---- this process's own use (pipeline.py tells the studio, so it can tell its own memory from other programs') ----
def _torch_gpu():
    """torch.cuda when this process has already started using the GPU through it (NVIDIA, or AMD through ROCm), else
    None. Never imports torch and never starts a GPU context."""
    t = sys.modules.get("torch")
    c = getattr(t, "cuda", None) if t is not None else None
    try:
        return c if c is not None and c.is_initialized() else None
    except Exception:
        return None


def held_mib():
    """GPU memory this process holds through PyTorch (what its allocator reserved), MiB; 0 before it uses the GPU.
    The GPU context itself (some hundreds of MiB) isn't counted: gpuwatch adds an allowance for it."""
    c = _torch_gpu()
    try:
        return int(c.memory_reserved() // (1 << 20)) if c is not None else 0
    except Exception:
        return 0


def spare_mib():
    """Reserved by this process's allocator but not in use (reused by the next model without asking the card), MiB."""
    c = _torch_gpu()
    try:
        return int((c.memory_reserved() - c.memory_allocated()) // (1 << 20)) if c is not None else 0
    except Exception:
        return 0


def peak_mib(reset=True):
    """The most this process's allocator reserved since the last call (MiB; None before it used the GPU): the peak of
    the step that just ran."""
    c = _torch_gpu()
    if c is None:
        return None
    try:
        v = int(c.max_memory_reserved() // (1 << 20))
        if reset:
            c.reset_peak_memory_stats()
        return v
    except Exception:
        return None


def is_oom(e):
    """Did this exception come from the GPU running out of memory (CUDA/ROCm "out of memory", cuDNN/cuBLAS alloc)?"""
    name = type(e).__name__
    msg = str(e).lower()
    return name == "OutOfMemoryError" or "out of memory" in msg or "cudnn_status_alloc_failed" in msg \
        or "cublas_status_alloc_failed" in msg
