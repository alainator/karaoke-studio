# Karaoke Studio in a container. Needs an NVIDIA GPU and the NVIDIA Container Toolkit on the host;
# see docker-compose.example.yml.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-trixie
# ffmpeg cuts and encodes the videos; node solves YouTube's JS challenge for yt-dlp
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node

# PyTorch with CUDA: the build has to suit the host's NVIDIA driver (cu128: driver 570 or newer)
ARG TORCH_INDEX=https://download.pytorch.org/whl/cu128
ARG TORCH_PACKAGES="torch torchaudio torchvision"
RUN pip install --no-cache-dir $TORCH_PACKAGES --index-url $TORCH_INDEX
COPY requirements.txt constraints.txt /tmp/
# a C compiler for the few packages without a ready-made wheel (diffq, for the vocal separator)
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc g++ \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir -r /tmp/requirements.txt -c /tmp/constraints.txt
# yt-dlp has to keep up with YouTube: its own layer, so updating it is a quick rebuild
# (docker compose build --build-arg YTDLP_REFRESH=$(date +%s))
ARG YTDLP_REFRESH=0
RUN pip install --no-cache-dir -U yt-dlp yt-dlp-ejs

WORKDIR /app
COPY . .
RUN python -I get_renderer.py \
 && useradd -u 1000 -m studio && mkdir -p /data /library && chown studio /data /library
USER studio
# everything the studio keeps goes under /data; published songs go to /library (PiKaraoke's song folder)
ENV NODE_BIN=/usr/local/bin \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    KARAOKE_JOBS=/data/jobs KARAOKE_WORK=/data/work KARAOKE_MODELS=/data/models KARAOKE_LOGS=/data/logs \
    KARAOKE_FONTS=/data/fonts KARAOKE_LIBRARY=/library \
    XDG_CACHE_HOME=/data/cache PYTHAINLP_DATA_DIR=/data/pythainlp \
    NUMBA_CACHE_DIR=/tmp/numba MPLCONFIGDIR=/tmp/matplotlib TRITON_CACHE_DIR=/tmp/triton
EXPOSE 5570
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s \
  CMD python -I -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5570/', timeout=8)"
CMD ["python", "-I", "app.py", "--host", "0.0.0.0", "--port", "5570"]
