# Contributing

Thanks for helping. Small, focused pull requests are easiest to review. Run the tests before sending one:

```sh
python -I -W ignore::ResourceWarning -m unittest discover -s tests -t .     # the Studio's Python (or its Docker image)
```

They need no GPU and no network: the pipeline is replaced by `tests/fake_pipeline.py`, and every setting that could
reach real things is pointed at throwaway folders (`tests/_env.py`).

- An endpoint that changes anything needs a scope in `agents.ROUTES` (agents are checked against it; a test walks
  every route).
- A change to the song-making code (`config.CODE_FILES`) changes the code fingerprint: a GPU helper takes songs only
  when it runs the same code.
- Keep the README short. Agent-facing docs live in `AGENTS.md` and `.claude/skills/`.

## Hardware

The Studio is built and tested only on hardware its authors own. We don't claim support for anything else, but we've
left seams so people with other hardware can make it work and tell us.

**Tested**

| Part | Where |
|---|---|
| NVIDIA GeForce RTX 5090 (32 GB) | the main Studio |
| NVIDIA GeForce RTX 3090 (24 GB) | two different PCs (one runs as a GPU helper) |

Linux, the NVIDIA driver 570 or newer, PyTorch with CUDA (the Docker image's default), about 9 GB of free GPU memory
per song.

**May work (untested; reports welcome)**

| Backend | Why it might | What to expect |
|---|---|---|
| AMD (ROCm) | ROCm builds of PyTorch reuse the `cuda` device name, so the models run unchanged; free memory comes from `rocm-smi` | the separator, Whisper and torchaudio need ROCm wheels; the Dockerfile installs CUDA ones (`TORCH_INDEX`) |
| Intel (XPU) | `gpu.device()` picks `xpu` when PyTorch reports it | free memory isn't known yet (the Studio waits for `MIN_GPU_FREE_MIB`: set it to 0); some models may lack XPU kernels |
| Apple (MPS) | `gpu.device()` picks `mps` when PyTorch reports it | same as XPU; Docker on macOS has no GPU, so run it without Docker |
| CPU only | `KARAOKE_DEVICE=cpu` | works for trying things out; a song takes a very long time |

**How it's wired.** Everything that touches the GPU goes through [`gpu.py`](gpu.py):

- `gpu.device()`: the torch device for the models (`KARAOKE_DEVICE` if set, else CUDA/ROCm, XPU, MPS; the pipeline
  refuses to fall back to the CPU silently).
- `gpu.free_mib()`, `gpu.name()`, `gpu.info()`: free memory and the card's name, from `nvidia-smi`, else `rocm-smi`,
  else 0/None. These run command-line tools and never import torch, so the web app and the helper don't open a GPU
  context.
- `gpu.empty_cache(dev)`: frees cached memory between models.

Nothing else says `"cuda"` or calls `nvidia-smi` (a test checks).

**Adding a backend**

1. Make `gpu.device()` return the right torch device, and add a memory query to `free_mib()`/`name()` (a vendor tool
   like `rocm-smi` or `xpu-smi`, parsed defensively: a broken or missing tool must give 0, not an exception).
2. Add tests in `tests/test_agents.py` (`Gpu`): fake the tool's output on PATH and a fake `torch` module, like the
   NVIDIA and ROCm cases.
3. Check the models that don't go through `gpu.device()` themselves: the vocal separator (`audio-separator` picks its
   own device), and Whisper (`stable-ts`).
4. Make one song end to end and compare it with the same song made on NVIDIA: `words.json` line times within about
   0.1 s, the same lines marked ⚠. Say in the pull request which card, driver and PyTorch build you used, how long
   the song took and the peak GPU memory.
5. Add the card to "May work" (or "Tested by contributors") with what you found.

The same goes for the GPU helper (`helper.py`, `deploy/install-helper.sh`): its installer assumes Arch-based Linux and
NVIDIA; another backend needs its own install steps.
