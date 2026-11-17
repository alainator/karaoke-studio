#!/usr/bin/env python3
"""Guess which of two singers sings each line (the studio's "Detect 2 singers" button).

    python -I singers.py <job dir>        -> JSON on stdout: {"singers": [1|2 per line], "separation": 0..1}

Each line's stretch of the separated vocal track gets a voice fingerprint (SpeechBrain ECAPA, trained on
speakers). The average is removed (what all lines share: the mix, the singing style) and the lines are split
into two groups with k-means. The group that sings the first line is Singer 1. Lines too short to fingerprint
take the singer of the nearest line. Nothing is saved: the editor shows the result for review.
"""
import json, os, subprocess, sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
from assgen import load_lines  # noqa: E402

SR = 16000
job_dir = os.path.abspath(sys.argv[1])
job = json.load(open(f"{job_dir}/job.json"))
lines = load_lines(f"{job_dir}/words.json")
off = float(job.get("offset") or 0.0)  # words.json is before the timing slider; the audio isn't
if len(lines) < 4:
    sys.exit(json.dumps({"error": "too few lines"}))

wav = os.path.join(C.WORK, f"{job['yt']}-vocals.wav")
src = wav if os.path.exists(wav) else f"{job_dir}/vocals.m4a"
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                     capture_output=True, check=True).stdout
audio = np.frombuffer(raw, dtype=np.float32)

import torch  # noqa: E402
from sklearn.cluster import KMeans  # noqa: E402
from sklearn.metrics import silhouette_score  # noqa: E402
from speechbrain.inference.speaker import EncoderClassifier  # noqa: E402

dev = "cuda:0" if torch.cuda.is_available() else "cpu"
enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb",
                                     savedir=os.path.join(C.MODELS, "spkrec-ecapa-voxceleb"), run_opts={"device": dev})
embs, idx = [], []
for i, l in enumerate(lines):
    a, b = l["words"][0][0] + off, l["words"][-1][1] + off
    seg = audio[max(0, int(a * SR)):int(b * SR)]
    if len(seg) < SR * 0.6:
        continue
    with torch.no_grad():
        e = enc.encode_batch(torch.from_numpy(seg.copy()).float().unsqueeze(0).to(dev)).squeeze().cpu().numpy()
    embs.append(e / (np.linalg.norm(e) + 1e-9)); idx.append(i)
if len(embs) < 4:
    sys.exit(json.dumps({"error": "not enough sung lines to compare"}))
X = np.array(embs)
X = X - X.mean(0)
X /= np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
lab = KMeans(2, n_init=20, random_state=0).fit(X).labels_
sep = float(silhouette_score(X, lab, metric="cosine")) if len(set(lab)) == 2 else 0.0
first = lab[0]
by_line = {i: (1 if l == first else 2) for i, l in zip(idx, lab)}
starts = [l["words"][0][0] for l in lines]
out = []
for i in range(len(lines)):
    if i in by_line:
        out.append(by_line[i])
    else:  # too short to fingerprint: same singer as the nearest fingerprinted line
        j = min(by_line, key=lambda k: abs(starts[k] - starts[i]))
        out.append(by_line[j])
print(json.dumps({"singers": out, "separation": round(sep, 3)}))
