"""Settings, from environment variables or a .env file next to this file (see .env.example)."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv(os.path.join(HERE, ".env"))
env = os.environ.get

# PiKaraoke song folder; published songs go to "<library>/Music Videos/"
LIBRARY = env("KARAOKE_LIBRARY", os.path.join(HERE, "data", "library"))
MV_DIR = os.path.join(LIBRARY, env("KARAOKE_MV_FOLDER", "Music Videos"))
# PiKaraoke: URL for the library rescan, admin password (if one is set), and its database for play counts
PIKARAOKE_URL = env("PIKARAOKE_URL", "http://127.0.0.1:5555").rstrip("/")
PIKARAOKE_PASSWORD = env("PIKARAOKE_ADMIN_PASSWORD", "")
PIKARAOKE_DB = env("PIKARAOKE_DB", "")
# working folders: jobs (previews), download/stem cache, separation model weights
JOBS = env("KARAOKE_JOBS", os.path.join(HERE, "data", "jobs"))
WORK = env("KARAOKE_WORK", os.path.join(HERE, "data", "work"))
MODELS = env("KARAOKE_MODELS", os.path.join(HERE, "data", "models"))
SEP_MODEL = env("KARAOKE_SEPARATION_MODEL", "model_bs_roformer_ep_317_sdr_12.9755.ckpt")
WHISPER_MODEL = env("KARAOKE_WHISPER_MODEL", "large-v3")
# the page's own address(es), for the Origin check on changes (comma-separated)
ORIGINS = {o.strip().rstrip("/") for o in env("STUDIO_ORIGINS", "").split(",") if o.strip()}
# folder with a `node` binary (yt-dlp needs a JS runtime for YouTube), prepended to PATH if set
NODE_BIN = env("NODE_BIN", "")
# don't start a job unless this much GPU memory is free (MiB)
MIN_GPU_FREE_MIB = int(env("MIN_GPU_FREE_MIB", "9000"))
USER_AGENT = env("STUDIO_USER_AGENT", "karaoke-studio/0.1 (+https://github.com/alainator/karaoke-studio)")


def tool_env():
    """Environment for subprocesses (yt-dlp needs node on PATH)."""
    e = dict(os.environ)
    if NODE_BIN:
        e["PATH"] = NODE_BIN + os.pathsep + e.get("PATH", "/usr/bin:/bin")
    return e
