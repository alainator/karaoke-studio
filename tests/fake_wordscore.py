#!/usr/bin/env python3
"""A stand-in for the words aligner (wordscore.py serve DEVICE KEEP_ALIVE), speaking the same protocol over its pipes:
a word is "sung" (confidence 0.9) where the recording is loud in the middle 0.3 s of its window, else 0.05. No model,
no GPU. FAKE_WORDS_FAIL=1: every request fails; FAKE_WORDS_SLOW=<seconds>: each answer waits that long."""
import json, os, select, sys, time

import numpy as np

SR = 16000
device, keep = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
out, inp = sys.stdout, sys.stdin.buffer
out.write(json.dumps({"ready": True, "load": 0.0, "device": device}) + "\n")
out.flush()
while True:
    r, _, _ = select.select([inp], [], [], keep)
    if not r:
        break
    line = inp.readline()
    if not line:
        break
    req = json.loads(line)
    a = np.frombuffer(inp.read(int(req["n"])), dtype=np.float32)
    if os.environ.get("FAKE_WORDS_SLOW"):
        time.sleep(float(os.environ["FAKE_WORDS_SLOW"]))
    if os.environ.get("FAKE_WORDS_FAIL"):
        ans = {"error": "RuntimeError: made to fail"}
    else:
        conf = []
        for key, r0, r1, _ in req["words"]:
            mid = (r0 + r1) / 2
            seg = a[max(0, int((mid - 0.15) * SR)):int((mid + 0.15) * SR)]
            conf.append(0.9 if len(seg) and float(np.sqrt(np.mean(seg.astype(np.float64) ** 2))) > 0.05 else 0.05)
        ans = {"conf": conf, "secs": 0.0}
    out.write(json.dumps(ans) + "\n")
    out.flush()
