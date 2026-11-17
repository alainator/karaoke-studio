#!/usr/bin/env bash
# Install the Karaoke Studio GPU helper on a Linux PC with an NVIDIA card (tested on CachyOS/Arch).
#
#   bash install-helper.sh <studio server address>      e.g. bash install-helper.sh 10.0.0.140
#
# - installs git, ffmpeg, node and uv with pacman (asks for your password once), if they're missing
# - sets up ~/karaoke-studio (Python 3.12 venv, PyTorch with CUDA, the pipeline's packages, ~10 GB with models)
# - creates a random shared secret, shows it ONCE so you can type it into the studio server, and stores it in .env
# - runs the helper as a systemd *user* service on port 5580 (runs while you're logged in; only the studio server
#   may connect), and opens that port in ufw/firewalld for the server only
# Re-running updates the code and keeps the secret. Remove: systemctl --user disable --now karaoke-helper
set -euo pipefail
SERVER="${1:?usage: bash install-helper.sh <studio server IP>}"
DIR="$HOME/karaoke-studio"
PORT=5580

echo "== 1/6 system packages"
need=()
for p in git ffmpeg node uv; do command -v "$p" >/dev/null || need+=("$p"); done
if ((${#need[@]})); then
  pkgs=(); for p in "${need[@]}"; do case $p in node) pkgs+=(nodejs);; *) pkgs+=("$p");; esac; done
  sudo pacman -S --needed --noconfirm "${pkgs[@]}"
fi
command -v nvidia-smi >/dev/null || { echo "nvidia-smi not found: install the NVIDIA driver first"; exit 1; }
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

echo "== 2/6 code"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone https://github.com/alainator/karaoke-studio "$DIR"; fi
cd "$DIR"

echo "== 3/6 Python + PyTorch (CUDA) + packages (a few GB, takes a while)"
[ -x venv/bin/python ] || uv venv --python 3.12 venv
uv pip install --python venv/bin/python -q torch torchaudio --index-url https://download.pytorch.org/whl/cu128
uv pip install --python venv/bin/python -q -r requirements.txt
venv/bin/python -c "import torch; assert torch.cuda.is_available(), 'PyTorch cannot see the GPU'; print('PyTorch sees', torch.cuda.get_device_name(0))"

echo "== 4/6 settings"
if [ ! -f .env ] || ! grep -q '^HELPER_TOKEN=' .env; then
  TOKEN=$(venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))")
  umask 077
  cat >> .env <<EOF
# Karaoke Studio GPU helper
HELPER_TOKEN=$TOKEN
HELPER_ALLOW=$SERVER
EOF
  echo
  echo "  Shared secret for the studio server (type it there when asked; it is shown only this once):"
  echo
  echo "      $TOKEN"
  echo
else
  sed -i "s/^HELPER_ALLOW=.*/HELPER_ALLOW=$SERVER/" .env
  echo "  keeping the existing secret (.env); to see it: grep HELPER_TOKEN $DIR/.env"
fi
chmod 600 .env

echo "== 5/6 download the models once (so the first song isn't slow)"
venv/bin/python -I - <<'PY'
import os, sys
sys.path.insert(0, os.getcwd())
import config as C
os.makedirs(C.MODELS, exist_ok=True)
from audio_separator.separator import Separator
Separator(model_file_dir=C.MODELS, output_dir=C.WORK).load_model(C.SEP_MODEL)
import stable_whisper
stable_whisper.load_model(C.WHISPER_MODEL, device="cpu")
import torchaudio
torchaudio.pipelines.MMS_FA.get_model(with_star=False)
print("models ready")
PY

echo "== 6/6 service + firewall"
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/karaoke-helper.service" <<EOF
[Unit]
Description=Karaoke Studio GPU helper (takes songs from the studio when its GPU is busy)

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python -I $DIR/helper.py --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=10
Nice=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now karaoke-helper
systemctl --user restart karaoke-helper
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow from "$SERVER" to any port $PORT proto tcp comment 'karaoke studio -> gpu helper'
elif command -v firewall-cmd >/dev/null && sudo firewall-cmd --state >/dev/null 2>&1; then
  sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=$SERVER port port=$PORT protocol=tcp accept"
  sudo firewall-cmd --reload
fi
sleep 3
if curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/health" | grep -q 40; then
  echo "helper is running on port $PORT (it answers only the studio server, with the secret)"
else
  echo "helper didn't start: journalctl --user -u karaoke-helper -n 50"; exit 1
fi
