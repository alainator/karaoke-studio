"""Test setup: point the studio at throwaway folders BEFORE app/config are imported. config.py reads a .env next to it
with setdefault, so every setting that could reach real things (the helper PC, PiKaraoke, Ollama, the real library
and jobs) is set here explicitly."""
import os, socket, sys, tempfile

ROOT = tempfile.mkdtemp(prefix="studio-test-")
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


HELPER_PORT = free_port()
for k, v in {
    "KARAOKE_JOBS": f"{ROOT}/jobs", "KARAOKE_WORK": f"{ROOT}/work", "KARAOKE_MODELS": f"{ROOT}/models",
    "KARAOKE_LOGS": f"{ROOT}/logs", "KARAOKE_FONTS": f"{ROOT}/fonts", "KARAOKE_LIBRARY": f"{ROOT}/library",
    "KARAOKE_MV_FOLDER": "Music Videos", "PIKARAOKE_URL": "http://127.0.0.1:9", "PIKARAOKE_ADMIN_PASSWORD": "",
    "PIKARAOKE_DB": "", "OLLAMA_URL": "", "GPU_HELPER_URL": "", "GPU_HELPER_NAME": "test helper",
    "HELPER_TOKEN": "test-token-" + "x" * 30, "HELPER_ALLOW": "", "USE_LOCAL_GPU": "1", "MIN_GPU_FREE_MIB": "0",
    "STUDIO_ORIGINS": "", "NODE_BIN": "", "KARAOKE_DATA": f"{ROOT}/data", "STUDIO_PUBLIC_URL": "", "STUDIO_NAME": "Test Studio",
    "ENCORE_TOKEN": "preset-token-" + "p" * 30, "ENCORE_ONLY_NETS": "", "ENCORE_STUDIO_ADDRESS": "http://studio.test:5570",
    "KARAOKE_LYRICS_FONT": "classic", "AGENT_KEYS_REQUIRED": "0", "STUDIO_PERSON_HEADER": "X-authentik-username", "ALBUM_LOOKUP": "0",
    "KARAOKE_DEVICE": "",
}.items():
    os.environ[k] = v
for d in ("jobs", "work", "models", "logs", "fonts", "data", "library/Music Videos"):
    os.makedirs(f"{ROOT}/{d}", exist_ok=True)
sys.path.insert(0, REPO)
FAKE = os.path.join(HERE, "fake_pipeline.py")


class Req:
    """Enough of a starlette Request for the endpoints (no Origin header, a JSON body)."""
    def __init__(self, body=None):
        self._b = body or {}
        self.headers = {}

        class U:
            netloc = "test"
        self.url = U()

    async def json(self):
        return self._b
