#!/usr/bin/env python3
"""Connect the studio to a GPU helper PC (run on the studio server, from the studio folder).

    python3 -I deploy/set-helper.py <helper address> [name]       e.g. deploy/set-helper.py 192.168.1.20 "gaming PC"

Asks for the helper's shared secret with a hidden prompt (shown once by install-helper.sh on the PC), checks that
the helper answers with it, and saves GPU_HELPER_URL / GPU_HELPER_NAME / HELPER_TOKEN in .env (0600).
Restart the studio afterwards so it picks the settings up.
"""
import getpass, json, os, sys, urllib.request

ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
host = sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__)
name = sys.argv[2] if len(sys.argv) > 2 else "helper PC"
url = host if host.startswith("http") else f"http://{host}:5580"
token = getpass.getpass("Helper secret (typing is hidden): ").strip()
if len(token) < 24:
    sys.exit("that doesn't look like the secret (it's ~43 characters)")
try:
    req = urllib.request.Request(url + "/health", headers={"X-Helper-Token": token})
    h = json.load(urllib.request.urlopen(req, timeout=8))
except urllib.error.HTTPError as e:
    sys.exit(f"the helper refused it (HTTP {e.code}): {'wrong secret' if e.code == 401 else 'this server is not in its HELPER_ALLOW'}")
except Exception as e:
    sys.exit(f"can't reach the helper at {url} ({e}); is the PC on and the service running?")
keep = [l for l in open(ENV).read().splitlines() if not l.startswith(("GPU_HELPER_URL=", "GPU_HELPER_NAME=", "HELPER_TOKEN="))] \
    if os.path.exists(ENV) else []
fd = os.open(ENV + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write("\n".join(keep + [f"GPU_HELPER_URL={url}", f"GPU_HELPER_NAME={name}", f"HELPER_TOKEN={token}"]) + "\n")
os.replace(ENV + ".tmp", ENV)
print(f"connected: {h.get('gpu')} with {h.get('free_mib', 0) // 1024} GB free. Saved to .env.")
print("now restart the studio:  sudo systemctl restart karaoke-studio")
