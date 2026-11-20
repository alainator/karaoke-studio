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
# optional: a local Ollama server to proofread automatic romaji ("Check romaji with AI"); leave empty to hide it
OLLAMA_URL = env("OLLAMA_URL", "").rstrip("/")
OLLAMA_MODEL = env("OLLAMA_MODEL", "gemma4:26b")
# optional: a GPU helper on another PC (helper.py) that takes jobs when this GPU is busy
GPU_HELPER_URL = env("GPU_HELPER_URL", "").rstrip("/")
GPU_HELPER_NAME = env("GPU_HELPER_NAME", "helper PC")
HELPER_TOKEN = env("HELPER_TOKEN", "")      # shared secret, the same value on both machines
USE_LOCAL_GPU = env("USE_LOCAL_GPU", "1") != "0"   # 0 = send every job to the helper (testing)
USER_AGENT = env("STUDIO_USER_AGENT", "karaoke-studio/0.1 (+https://github.com/alainator/karaoke-studio)")


# how the lyrics look (assgen.py); 1 = on, 0 = off. Existing songs change when their lyrics are next saved, or all at
# once with deploy/rebuild-lyrics.py.
#   display: for songs with an original-script line (Japanese, Korean, Chinese…), what new songs show by default:
#            both (original above the romanized line), original, romaji, or kana (Japanese in hiragana); each song can
#            pick its own on its page
#   sweep: words fill gradually as they're sung (else a whole word lights up at once)
#   countdown: dots before the first line and after a gap, going one by one until the singing starts
#   stagger: the top row starts at the left and the bottom row ends at the right (else both centred)
#   cards: the title and artist during the intro, and "♪ seconds left" in long instrumental parts
#   font: heavy (Noto Sans CJK Black, embedded in each song's subtitles) or classic (the TV's Arial / Droid Sans)
_on = lambda name: env(name, "1").strip().lower() not in ("0", "no", "off", "false")
LYRICS_DISPLAY = env("KARAOKE_LYRICS_DISPLAY", "both").strip().lower()
LYRICS_SWEEP = _on("KARAOKE_LYRICS_SWEEP")
LYRICS_COUNTDOWN = _on("KARAOKE_LYRICS_COUNTDOWN")
LYRICS_STAGGER = _on("KARAOKE_LYRICS_STAGGER")
LYRICS_CARDS = _on("KARAOKE_LYRICS_CARDS")
LYRICS_FONT = env("KARAOKE_LYRICS_FONT", "heavy").strip().lower()


# how the karaoke video is encoded (see pipeline.py, render): re-encoded, never copied from YouTube, because
# Chrome-based browsers freeze on YouTube's stream after jumping backwards
VENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "48", "-pix_fmt", "yuv420p"]


CODE_FILES = ("pipeline.py", "assgen.py", "config.py", "romanize.py", "versions.py", "albumsync.py", "fonts.py", "earcheck.py", "linecheck.py", "karamoe.py", "lyricmap.py")  # the modules that make songs (pipeline.py imports these)


def code_version():
    """Fingerprint of the code that makes songs; the studio only sends jobs to a helper running the same."""
    import hashlib
    h = hashlib.sha256()
    for f in CODE_FILES:
        h.update(open(os.path.join(HERE, f), "rb").read())
    return h.hexdigest()[:12]


def tool_env():
    """Environment for subprocesses (yt-dlp needs node on PATH)."""
    e = dict(os.environ)
    if NODE_BIN:
        e["PATH"] = NODE_BIN + os.pathsep + e.get("PATH", "/usr/bin:/bin")
    return e


# ---- logs ----
# data/logs/studio.log: what the studio did and when (songs queued, started, finished, failed, published…).
# <job folder>/pipeline.log: everything the pipeline printed while making that song, every run appended.
LOGS = env("KARAOKE_LOGS", os.path.join(HERE, "data", "logs"))


def job_log(job_dir, line, header=False):
    """Append one line of pipeline output (or a run header) to the job's pipeline.log; never raises."""
    import time
    try:
        line = line.rstrip()
        if not header and (not line or "%|" in line or "\r" in line):  # progress bars
            return
        with open(os.path.join(job_dir, "pipeline.log"), "a", encoding="utf-8", errors="replace") as f:
            f.write((f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {line} ====\n" if header else f"{time.strftime('%H:%M:%S')} {line[:2000]}\n"))
    except Exception:
        pass

