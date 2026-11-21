#!/usr/bin/env python3
"""Regenerate contract/fixtures with the Studio's own code: a throwaway studio (jobs, library, player store in a
temporary folder) makes three made-up songs with placeholder words (no real lyrics), publishes them the way the web
app does, and then every sample API exchange is a real call to the Studio's /api/encore/ handlers (through all its
middleware). The output is the same on every run (a fixed clock, ids, code and token), so the copy in Encore's repo
can be compared byte for byte.

    python -I contract/make_fixtures.py [OUT]       (default: contract/fixtures; run with the studio's Python, e.g.
                                                      in its image: no GPU, network or PiKaraoke needed; ffmpeg is,
                                                      for the §2.9 recording)

fixtures/library/: catalog.json and "Music Videos/" (.mp4 placeholders of a few bytes, .ass, .words.json, .pitch.json,
  .vocals.m4a: a real AAC file of a quarter second of silence, 782 bytes, made once with ffmpeg and kept below)
  - Nova Lane - Paper Lanterns: a solo (the last line a backing line)
  - The Quiet Arcade - Two Small Rivers: a duet with named singers (Ada, Bram), a line sung together, a line shared
    word by word; sung in Japanese with an original-script row (placeholder kana), so it has every lyric mode
  - Mika Sol - Neon Harbor: with a custom-video version, "Mia's version" (its own .mp4 and lyric files; the same sound
    and timing as the song, as every Studio version)
fixtures/api/NN-name.json: {"name", "about", "request": {method, path, headers, body, schema}, "response": {status,
  headers (only ETag, Content-Range, Accept-Ranges, Retry-After), body, schema}}; a body {"$file": path} is that fixture
  file (with "$range": [first, last] the bytes of it). The bearer token shows as "<token from pairing>". A request
  whose headers carry what describes the body (§2.9 words scoring) also names a "headers_schema".
fixtures/audio/sung.ogg: the §2.9 recording, made-up "singing" (a 330 Hz tone on the words a singer sang, silence
  elsewhere; 8 s of Opus, 8.7 KB, made once with ffmpeg and kept below). The words aligner is tests/fake_wordscore.py
  (a word is heard where the recording is loud) and the song has no real vocals to compare with, so every word is judged.
The pitch files come from the Studio's note maker (pitch.notes) fed a made-up melody (the CREPE step needs real
vocals), cached the way pitch.py caches it; their instrumental loudness ("music") comes from the Studio's loudness
code (pitch.music_db) fed made-up sound (the placeholder videos have none).
"""
import base64, json, os, shutil, sys, tempfile, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "fixtures")
ROOT = tempfile.mkdtemp(prefix="studio-fixtures-")
for k, v in {"KARAOKE_JOBS": f"{ROOT}/jobs", "KARAOKE_WORK": f"{ROOT}/work", "KARAOKE_MODELS": f"{ROOT}/models",
             "KARAOKE_LOGS": f"{ROOT}/logs", "KARAOKE_DATA": f"{ROOT}/data", "KARAOKE_FONTS": f"{ROOT}/fonts",
             "KARAOKE_LIBRARY": f"{ROOT}/library", "KARAOKE_MV_FOLDER": "Music Videos", "PIKARAOKE_URL": "",
             "PIKARAOKE_ADMIN_PASSWORD": "", "PIKARAOKE_DB": "", "OLLAMA_URL": "", "GPU_HELPER_URL": "", "HELPER_TOKEN": "",
             "STUDIO_ORIGINS": "", "STUDIO_PUBLIC_URL": "https://studio.example.lan", "STUDIO_NAME": "Karaoke Studio",
             "ENCORE_TOKEN": "", "ENCORE_ONLY_NETS": "", "KARAOKE_LYRICS_FONT": "classic", "KARAOKE_LYRICS_DISPLAY": "both",
             "AGENT_KEYS_REQUIRED": "0", "STUDIO_PERSON_HEADER": "X-authentik-username", "KARAOKE_DEVICE": "",
             "USE_LOCAL_GPU": "1", "MIN_GPU_FREE_MIB": "9000",
             "TZ": "UTC"}.items():
    os.environ[k] = v
for d in ("jobs", "work", "models", "logs", "data", "fonts", "library/Music Videos"):
    os.makedirs(f"{ROOT}/{d}", exist_ok=True)
sys.path.insert(0, REPO)
time.tzset()

# a fixed clock, token and studio id: the same files on every run
T0 = 1793534400.0
DAY = 86400
CLOCK = [T0]
_strftime = time.strftime
time.time = lambda: CLOCK[0]
time.strftime = lambda fmt, t=None: _strftime(fmt, time.gmtime(CLOCK[0]) if t is None else t)
import secrets  # noqa: E402
secrets.token_urlsafe = lambda n=32: "fixture-token-0123456789abcdef"

import numpy as np  # noqa: E402
import pitch as P  # noqa: E402


def made_up_music(video):
    """The instrumental for the loudness track: a placeholder video says its length; levels on the 0.5 dB grid, as
    ±A samples (RMS exactly A): quiet intro, a changing level, a silent last second."""
    import re
    secs = float(re.search(rb"([0-9.]+) s", open(video, "rb").read()).group(1))
    hop = int(P.SR * P.MUSIC_HOP)
    out = []
    for k in range(int(round(secs / P.MUSIC_HOP))):
        t = k * P.MUSIC_HOP
        lvl = None if t >= secs - 1.0 else -60.0 if t < 2.0 else -32.0 + ((k * 7) % 25) * 0.5
        a = 0.0 if lvl is None else 10 ** (lvl / 20)
        out.append(a * np.where(np.arange(hop) % 2, -1.0, 1.0))
    return np.concatenate(out).astype(np.float32)


P.music_audio = made_up_music
import app as A  # noqa: E402
import encore  # noqa: E402
from assgen import save_lines  # noqa: E402
from tests._asgi import call  # noqa: E402

A.STORE.now = lambda: CLOCK[0]
A.STORE.q("insert or replace into kv (k, v) values ('studio_id', ?)", ("9c1e7b42-5d3a-4f86-a0b9-2e4c6d8f1a3b",))
A.pitch_later = lambda jid: None   # written below, in order (not in the background)
A.gpu_free_mib = lambda: 32000      # (a free card, whatever machine this runs on: the auto-make ETAs are the same)
A.catalog_soon = lambda delay=0: None
# §2.9 words scoring: jobs run below, in order (no background thread), with the stand-in aligner
A.WORDS.now = lambda: CLOCK[0]
A.WORDS._start = lambda: None
A.WORDS.cmd = [sys.executable, "-I", os.path.join(REPO, "tests", "fake_wordscore.py")]
A.WORDS.reference = lambda song, device: None
A.WORDS.ref_cached = lambda song: True
_WJOBS = iter(["5d0c9a7e3b1f4e28a6c4d2b0e8f6a1c3", "6e1d0b8f4c2a4f39b7d5e3c1f9a7b2d4", "7f2e1c9a5d3b4a4ac8e6f4d2a0b8c3e5"])
A.WORDS.new_id = lambda: next(_WJOBS)

TOKEN_SHOWN = "<token from pairing>"
PLAYER = "6f1c2a90-3b7e-4c1d-9e55-0a7b8c9d1e2f"
CODE = "K7M3PQ9X"
U = lambda name: str(uuid.uuid5(uuid.NAMESPACE_URL, "encore-fixture:" + name))
R = ["b3f1c9a2-1d4e-4f6a-8b7c-2e9d0a1b3c4d", "c4a2d0b3-2e5f-4a7b-9c8d-3f0e1b2c4d5e", "d5b3e1c4-3f6a-4b8c-8d9e-4a1f2c3d5e6f",
     "e6c4f2d5-4a7b-4c9d-9e0f-5b2a3d4e6f7a", "f7d5a3e6-5b8c-4dae-8f1a-6c3b4e5f7a8b", "a8e6b4f7-6c9d-4ebf-9a2b-7d4c5f6a8b9c"]
FID = ["0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "1b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e", "2c3d4e5f-6a7b-4c8d-8e9f-1a2b3c4d5e6f"]
PID = ["3d4e5f6a-7b8c-4d9e-9f0a-2b3c4d5e6f7a", "4e5f6a7b-8c9d-4e0f-8a1b-3c4d5e6f7a8b", "5f6a7b8c-9d0e-4f1a-9b2c-4d5e6f7a8b9c"]
UID = "7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
SYLL = ["la", "da", "na", "doo", "ba", "oh"]
KANA = {"la": "ら", "da": "だ", "na": "な", "doo": "どう", "ba": "ば", "oh": "お"}
VOCALS_M4A = base64.b64decode(
    "AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAtZtb292AAAAbG12aGQAAAAAAAAAAAAAAAAAAAPoAAAA+gABAAABAAAAAAAA"
    "AAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAACJXRy"
    "YWsAAABcdGtoZAAAAAMAAAAAAAAAAAAAAAEAAAAAAAAA+gAAAAAAAAAAAAAAAQEAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAA"
    "AAAAAAAAAEAAAAAAAAAAAAAAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAAPoAAAQAAAEAAAAAAZ1tZGlhAAAAIG1kaGQAAAAA"
    "AAAAAAAAAAAAAB9AAAAL0FXEAAAAAAAtaGRscgAAAAAAAAAAc291bgAAAAAAAAAAAAAAAFNvdW5kSGFuZGxlcgAAAAFIbWluZgAA"
    "ABBzbWhkAAAAAAAAAAAAAAAkZGluZgAAABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAAEMc3RibAAAAGpzdHNkAAAAAAAAAAEA"
    "AABabXA0YQAAAAAAAAABAAAAAAAAAAAAAQAQAAAAAB9AAAAAAAA2ZXNkcwAAAAADgICAJQABAASAgIAXQBUAAAAAAD6AAAAA/QWA"
    "gIAFFYhW5QAGgICAAQIAAAAgc3R0cwAAAAAAAAACAAAAAgAABAAAAAABAAAD0AAAABxzdHNjAAAAAAAAAAEAAAABAAAAAwAAAAEA"
    "AAAUc3RzegAAAAAAAAAEAAAAAwAAABRzdGNvAAAAAAAAAAEAAAMCAAAAGnNncGQBAAAAcm9sbAAAAAIAAAAB//8AAAAcc2JncAAA"
    "AAByb2xsAAAAAQAAAAMAAAABAAAAPXVkdGEAAAA1bWV0YQAAAAAAAAAhaGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAI"
    "aWxzdAAAAAhmcmVlAAAAFG1kYXQBGCAHARggBwEYIAc=")
SUNG_OGG = base64.b64decode(
    "T2dnUwACAAAAAAAAAAAAAAAAAAAAAEkq9qABE09wdXNIZWFkAQE4AYA+AAAAAABPZ2dTAAAAAAAAAAAAAAAAAAABAAAASZW+VAEu"
    "T3B1c1RhZ3MGAAAAZmZtcGVnAQAAABQAAABlbmNvZGVyPUxhdmMgbGlib3B1c09nZ1MAAIC7AAAAAAAAAAAAAAIAAACj5qjRMggJ"
    "CAgICAgICAgICAgICAgICAgIJCYqKS0vLSswKTArLCsoLCgtLy0oGhAQECotKygtSAvkwTbsxYBIB8lyJ+FE6lBIB8l5yMlXwEgH"
    "yXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnI"
    "yVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIpuhDVIZuke7gJ2Snjbmn"
    "oIqVCZdPQHcVKSGnJwYokqC114BIoUIZZXBcDCG3swr0Zj2fQsv2y9iFf6mcqHNlJwhv6f9MDcmDc0ihQhl4a1Gy8Q2UuiVRfu7f"
    "ovikj/2TKKXrQ4QqQyj7dw8no/ZRlzeujkihQhl4a1lK76zp7zKCYeooFpNa5Av/KgOHaiX4oBkPKegfDrTVF8ngSKFCGXhrWU6K"
    "0wggQ4Ve6FG5MRVjUVW1IgkOsfwi7bTSJUHWn/dyfAhwyEaASKFCGWVwZdm1QQZNc8vmRyoE6TrAlpY+tDcsDPQsBDU8kwixCkXR"
    "wHu2jfN4btxIoUIZeGtYxn+kEO+5i0mtQhQj3v5A6+fMjZuf83X3LEfujB+NaRZsh7rzlxBIoUIZeGtZTon0f8dE8cHZ2LTaE+7Q"
    "Ito31KnYFkag95d1QNP7fGcsaKOASKFCGXhrTWVZqtnsyzMcNnuXnoyf6OXsFhiZWTt2TiPnhO9vmHznjGvKKf5gTI9YSKFZXmYr"
    "Ji2f/HomYMVb2Puo43lKMFUhHEOYpuJOYI7OV38EYhq23XBIoop5ZXBcDGVYOVNCy6M9WFA5WSKPWoJc9kaVljDk9fzLfsEphT7Z"
    "DYVPJhGOTSBIoop5ZXBcDCHfgNP3nhO7pnoZfOBwB/QewoS9nrUkk61zK6P2hGSxomQwSKKKeW/wbkgjLU795sLlybcvL9K+oM28"
    "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"
    "F5wWVg6PBiAEulCikwis/VZJhBxIrqaWAPEyLQYD3pt/SwaK3xWmrWUNNZ7JqnfXki0bHOfXeEBIrqaV59A42vp6KS+E4S+ZIE/y"
    "LAkAdkyj/1BVAxJ/vddz4bluUtc0SK6mlefQTC9VUQagoMhzh2msr9OpqYdAmET9w7vVuYQuYEiupvfabhaiSJ7xWwFCxHp/KFde"
    "BZ+58+uC0vC/n8MMUEiupvfab1FsbPbaHoGR8oFnaoabhv/hzOBIhclig3SROT6Mgk3ke33sfz9232bT2UwgSD93cAQyExYLwEv6"
    "8d8pK7hrQEg6MDB5rCrtKWqj4HQnv8iASB1ppmLI16u6vrLjYhbuJUgMBaC91jBNWTDrXmCpSAqCpMUnHwW29wHgJosUSAmcpMUn"
    "H1Pt1UQ8yW68W4BICZykxSi3Y3dYnJ0jQ49ASAlz56vgx3zGpQ1zceXJKkgItqTFaBi032J8TNm26YXgSAi2pMVTBCEvF+5rG9Z8"
    "b0OwSAi2pMUl0hGH7PeQOCIgVPqNSAi2pMUnIhPf+hNGU2x9+v+fSAi7GXfJnQYfSsyJCH+cRwgBG0gIr3In4UTqUEgIr3nIyVfA"
    "SAivecjJV8BICK95yMlXwEgIr3nIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJ"
    "ecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfAT2dnUwAA"
    "AO4CAAAAAAAAAAAABQAAAEg6ap4yCAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAhIB8l5"
    "yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlX"
    "wEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgH"
    "yXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnI"
    "yVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfA"
    "SAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJ"
    "ecjJV8BIB8l5yMlXwEgHyXnIyVfAT2dnUwAAgKkDAAAAAAAAAAAABgAAAPxKnM4yCAgICAgICAgICAgICAgICAgICAgICAgICAgI"
    "CAgICAgICAgICAgICCQmLCYqKCsuJi9IB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5"
    "yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlX"
    "wEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgH"
    "yXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnI"
    "yVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEim6ENUhm6R7uAnZKeNuaegipUJl09AdxUpIacnBiiS"
    "oLXXgEihQhllcFwMIbezCvRmPZ9Cy/bL2IV/qZyoc2UnCG/p/0wNyYNzSKFCGXhrUbLxDZS7EPDjDIYedyx3Sm7yfkCMs5ZFn9YO"
    "wzf06PkYbuZ9W1hIoUIZeGtZSu5tR0B6HF5+06ID8dLd1Jj2i6OHnHyMuZ7wMkG2gEihQhl4a1lOiRPIzhfLGHbQ+uSnJ0Y/y4qu"
    "pDO7g36KjNmHLV+S/Uu8zkihQhllcGXZs/YL+2yopUojdeOKFH4mUpgalSQv+Do2hfnMrYL5xNpIoUIZeGtYxn+ctj0zLnm/AhtU"
    "ahw6ef20z5oT49rQpLtLPQVkjXjdLXsmSKFCGXhrWU6JHV791UdZrWTycG20Xu7lFvsFYCnyMLwVD1V69v0qwcBUsCiboEihQhl4"
    "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"
    "yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlX"
    "wEgHyXnIyVfAT2dnUwAAANwFAAAAAAAAAAAACQAAAEl8PD8yCAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI"
    "CAgICAgICAgICAhIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfA"
    "SAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJ"
    "ecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJ"
    "V8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BI"
    "B8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5"
    "yMlXwEgHyXnIyVfASAfJecjJV8BIB8l5yMlXwEgHyXnIyVfAT2dnUwAEON0FAAAAAAAAAAAACgAAAEuJ74sBCEgHyXnIyVfA")
FAKE_MP4 = lambda seconds: (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"placeholder video, not playable: "
                            + f"{seconds:.1f} s".encode()).ljust(96, b".")


def lines_of(spec):
    """spec: [(start, members or None, n words, extra)] -> studio lines (words.json v2) with placeholder words."""
    out = []
    for i, (start, members, n, extra) in enumerate(spec):
        t, words = start, []
        for k in range(n):
            words.append([round(t, 2), round(t + 0.42, 2), SYLL[(i + k) % len(SYLL)]])
            t += 0.5
        l = {"words": words}
        if members:
            l["members"] = members
        l.update(extra)
        out.append(l)
    return out


def melody(lines, duration, lo=57.0):
    """A made-up sung melody for the note maker: f0 (Hz) and CREPE-like periodicity per 20 ms frame."""
    n = int(round(duration / P.HOP))
    f0, per = np.zeros(n), np.full(n, 0.05)
    for li, l in enumerate(lines):
        for k, (a, b, _) in enumerate(l["words"]):
            m = lo + (li * 2 + k) % 9 + (0.5 if k % 3 == 2 else 0.0)
            i0, i1 = int(round(a / P.HOP)), int(round(b / P.HOP))
            f0[i0:i1] = 440.0 * 2 ** ((m - 69) / 12)
            per[i0:i1] = 0.9
    return f0, per


def make_job(jid, artist, title, lang, duration, lines, cast=(), status="done"):
    d = os.path.join(A.JOBS, jid)
    os.makedirs(d)
    save_lines(os.path.join(d, "words.json"), lines, list(cast))
    open(os.path.join(d, "out.mp4"), "wb").write(FAKE_MP4(duration))
    open(os.path.join(d, "vocals.m4a"), "wb").write(VOCALS_M4A)  # published as <base>.vocals.m4a (guidevocals.py)
    f0, per = melody(lines, duration)
    np.savez_compressed(os.path.join(d, P.CACHE), f0=f0.astype(np.float32), per=per.astype(np.float16), hop=np.float32(P.HOP),
                        stamp=np.array(P._stamp(os.path.join(d, "vocals.m4a"))), model=np.array("fixture"))
    result = {"offset": 0.0, "lines": len(lines), "duration": duration, "lang": lang, "width": 1920, "height": 1080,
              "video": "h264,1920,1080", "encoded": "x264", "pipeline": A.versions.VERSION}
    json.dump(result, open(os.path.join(d, "result.json"), "w"))
    job = {"id": jid, "yt": "aaaaaaaaaaa", "lrc_id": 1, "lyrics_source": "lrclib", "artist": artist, "title": title,
           "status": status, "step": status if status == "done" else "align", "created": CLOCK[0], "finished": CLOCK[0],
           "offset": 0.0, "lyrics_display": "both", "result": result, "log": []}
    A.save(jid, job)
    A.rebuild_lyrics(jid, job)
    return jid


def publish(jid):
    st, _, b = call(A.app, "POST", f"/api/jobs/{jid}/publish", {"remove": []})
    assert st == 200, (st, b)
    A.write_pitch(jid)


EXCHANGES = []
KEEP_HEADERS = ("etag", "content-range", "accept-ranges", "retry-after")


def ex(n, name, about, method, path, body=None, schema=None, res_schema=None, headers=None, auth=True, client=None,
       expect=None, body_file=None, req_file=None, headers_schema=None):
    """Make the call for real, and keep it as fixtures/api/NN-name.json."""
    h = dict(headers or {})
    if auth:
        h["Authorization"] = "Bearer " + (TOKEN if auth is True else auth)
    st, rh, rb = call(A.app, method, path, body, h, **({"client": client} if client else {}))
    if expect is not None:
        assert st == expect, (name, st, rb[:300])
    shown = {k: ("Bearer " + TOKEN_SHOWN if k == "Authorization" and (auth is True or auth in MASKED) else v) for k, v in h.items()}
    if body_file:
        res_body = body_file
    else:
        res_body = json.loads(rb) if rb else None
    request = {"method": method, "path": path, "headers": shown, "body": req_file if req_file else body, "schema": schema}
    if headers_schema:
        request["headers_schema"] = headers_schema
    EXCHANGES.append((n, name, {"name": name, "about": about, "request": request,
                                "response": {"status": st, "headers": {k.title() if k != "etag" else "ETag": v for k, v in rh.items()
                                                                       if k in KEEP_HEADERS},
                                             "body": res_body, "schema": res_schema}}))
    return st, rh, rb


TOKEN = None
MASKED = set()   # other players' tokens from pairing, shown as TOKEN_SHOWN too


def main():
    global TOKEN
    err = "error.schema.json"
    # ---- the songs
    solo = make_job("20261101-120000-nova-lane-paper-lanterns", "Nova Lane", "Paper Lanterns", "en", 33.0,
                    lines_of([(12.4, None, 4, {}), (16.8, None, 5, {}), (24.0, None, 4, {}), (29.5, None, 3, {"backing": True})]))
    publish(solo)
    CLOCK[0] += DAY
    duet_lines = lines_of([(8.0, [0], 4, {}), (11.0, [1], 4, {}), (14.5, [0, 1], 5, {}),
                           (19.0, [0], 4, {"wwho": [{"members": [0]}, {"members": [0]}, {"members": [1]}, {"members": [1]}]}),
                           (23.0, [1], 3, {})])
    for l in duet_lines:
        l["orig"] = "".join(KANA[w[2]] for w in l["words"])
    duet = make_job("20261102-090000-the-quiet-arcade-two-small-rivers", "The Quiet Arcade", "Two Small Rivers", "ja", 27.5,
                    duet_lines, cast=["Ada", "Bram"])
    publish(duet)
    CLOCK[0] += DAY
    ver_lines = lines_of([(10.0, None, 4, {}), (14.0, None, 4, {}), (18.5, None, 5, {})])
    ver = make_job("20261103-183000-mika-sol-neon-harbor", "Mika Sol", "Neon Harbor", "en", 24.0, ver_lines, status="running")
    vdir = os.path.join(A.JOBS, ver, "versions", "mias-version")
    os.makedirs(vdir)
    open(os.path.join(vdir, "out.mp4"), "wb").write(FAKE_MP4(24.0))
    json.dump({"name": "Mia's version", "mode": "version", "slug": "mias-version", "source": {"kind": "file", "filename": "mia.mp4"},
               "start": 0.0, "offset": 0.0, "trim_start": 0.0, "aligned": "manual", "fill": "hold", "lead": "hold",
               "made_at": CLOCK[0]}, open(os.path.join(vdir, "meta.json"), "w"))
    A.update_catalog()

    # ---- linking
    CLOCK[0] = T0 + 10 * DAY
    A.STORE.new_code()
    A.STORE.code["hash"] = encore.token_hash(CODE)
    pair = {"code": CODE, "name": "Encore on kit-01", "player": PLAYER, "encore": "0.4", "contract": "1"}
    kit = ("192.0.2.40", 50000)
    _, _, b = ex(1, "pair", "Linking: the host typed the Studio's address and the code it shows (no bearer).", "POST",
                 "/api/encore/pair", pair, "pair.request.schema.json", "pair.response.schema.json", auth=False, client=kit, expect=200)
    TOKEN = json.loads(b)["token"]
    ex(2, "pair-wrong-code", "A wrong, used or expired code (this one was used by the pairing above).", "POST", "/api/encore/pair",
       dict(pair, code="K7M3PQ9Y"), "pair.request.schema.json", err, auth=False, client=kit, expect=403)
    for _ in range(3):
        call(A.app, "POST", "/api/encore/pair", dict(pair, code="K7M3PQ9Z"), client=kit)
    ex(3, "pair-rate-limited", "More than 5 pairing tries a minute from one address.", "POST", "/api/encore/pair",
       dict(pair, code="K7M3PQ9Z"), "pair.request.schema.json", err, auth=False, client=kit, expect=429)
    ex(4, "hello", "Connection check (System view).", "GET", "/api/encore/hello", None, None, "hello.response.schema.json", expect=200)

    # ---- requests
    CLOCK[0] += 60
    req = {"rid": R[0], "artist": "Lumen Coast", "title": "Glass Orchard", "note": "the music video one", "by": "Mia",
           "party": "Friday at Ken's", "at": int(CLOCK[0])}
    ex(5, "request-new", "A song the library doesn't have.", "POST", "/api/encore/requests", req,
       "requests.post.request.schema.json", "requests.post.response.schema.json", expect=200)
    CLOCK[0] += 5
    ex(6, "request-duplicate", "The same rid again (a retry after a lost answer): nothing changes, the same answer.",
       "POST", "/api/encore/requests", req, "requests.post.request.schema.json", "requests.post.response.schema.json", expect=200)
    CLOCK[0] += 55
    ex(7, "request-have", "The Studio already has it (matched accent- and case-insensitively, either name order).", "POST",
       "/api/encore/requests", {"rid": R[1], "artist": "Paper Lanterns", "title": "nova lane", "note": "", "by": "Sam",
                                "party": "Friday at Ken's", "at": int(CLOCK[0])},
       "requests.post.request.schema.json", "requests.post.response.schema.json", expect=200)
    # (not kept) a request for a song being made: the answer says open, the polls say making, then published
    CLOCK[0] += 60
    call(A.app, "POST", "/api/encore/requests", {"rid": R[2], "artist": "Mika Sol", "title": "Neon Harbor", "note": "", "by": "Jo",
                                                 "party": "Friday at Ken's", "at": int(CLOCK[0])}, {"Authorization": "Bearer " + TOKEN})
    call(A.app, "POST", "/api/encore/requests", {"rid": R[3], "artist": "Iris Vale", "title": "Paper Boats", "note": "", "by": "Ana",
                                                 "party": "Friday at Ken's", "at": int(CLOCK[0])}, {"Authorization": "Bearer " + TOKEN})
    # the Studio's admin: "Find and make" for Glass Orchard (the Make page sends the rid with the new song)
    CLOCK[0] += 120
    st, _, b = call(A.app, "POST", "/api/jobs", {"yt": "bbbbbbbbbbb", "lrc_id": 2, "lyrics_source": "lrclib", "artist": "Lumen Coast",
                                                "title": "Glass Orchard", "request": R[0]})
    assert st == 200, b
    lumen = json.loads(b)["id"]
    A.set_job(lumen, status="running", step="separate", started=CLOCK[0])
    CLOCK[0] += 300
    st, _, b = call(A.app, "POST", f"/api/requests/{R[3]}/decline", {"reason": "No usable video yet"})
    assert st == 200, b
    # Neon Harbor is made, and published with its version
    CLOCK[0] += 300
    A.set_job(ver, status="done", step="done", finished=CLOCK[0])
    publish(ver)
    CLOCK[0] += 60
    st, _, b = call(A.app, "POST", f"/api/jobs/{ver}/videos/mias-version/publish", {})
    assert st == 200, b
    A.write_pitch(ver)
    CLOCK[0] += 240
    ex(8, "requests-by-rids", "Polling the requests still moving (up to 40 rids a call).", "GET",
       f"/api/encore/requests?rids={R[0]},{R[2]},{R[3]}", None, None, "requests.get.response.schema.json", expect=200)
    since = int(T0 + 10 * DAY + 400)
    ex(9, "requests-since", "Everything from this player updated after a time.", "GET", f"/api/encore/requests?since={since}",
       None, None, "requests.get.response.schema.json", expect=200)

    # ---- flags
    CLOCK[0] += 60
    vbase = A.load(ver)["published_as"].replace("(Music Video)", "(Mia's version)")
    vw = json.load(open(os.path.join(A.MV_DIR, vbase + ".words.json"), encoding="utf-8"))["lines"][1]
    ex(10, "flag-line", "Lyrics off while singing the custom version: t in that version's video time (a Studio version has the "
       "song's sound and timing), line = its words.json 'i'.", "POST", "/api/encore/flags",
       {"fid": FID[0], "id": ver, "base": vbase, "t": round(vw["start"] + 0.6, 2), "line": vw["i"], "kind": "late", "note": "",
        "by": "Sam", "party": "Friday at Ken's", "at": int(CLOCK[0])},
       "flags.post.request.schema.json", "flags.post.response.schema.json", expect=200)
    CLOCK[0] += 60
    ex(11, "flag-whole-song", "Flagged afterwards from Last songs: the whole song (no time, no line).", "POST", "/api/encore/flags",
       {"fid": FID[1], "id": duet, "base": A.load(duet)["published_as"], "t": None, "line": None, "kind": "words",
        "note": "second verse", "by": "Host", "party": "Friday at Ken's", "at": int(CLOCK[0])},
       "flags.post.request.schema.json", "flags.post.response.schema.json", expect=200)
    CLOCK[0] += 60
    ex(12, "flag-unknown-song", "A song Encore found by scanning the folders (an 'f-' id), which the Studio doesn't know by its base "
       "either.", "POST", "/api/encore/flags",
       {"fid": FID[2], "id": "f-0123456789ab", "base": "Someone - Something (Music Video)", "t": 40.0, "line": None, "kind": "early",
        "note": "", "by": "Jo", "party": "Friday at Ken's", "at": int(CLOCK[0])},
       "flags.post.request.schema.json", "flags.post.response.schema.json", expect=200)

    # ---- plays: earlier parties (not kept), then a batch
    sb, vb = A.load(solo)["published_as"], A.load(ver)["published_as"]
    old = [{"pid": U(f"old-{i}"), "id": solo, "base": sb, "at": int(T0 + DAY * (3 + i)), "seconds": 33, "result": "finished",
            "singers": 1, "style": None, "party": "Earlier"} for i in range(6)]
    old += [{"pid": U(f"old-v-{i}"), "id": ver, "base": vb, "at": int(T0 + DAY * (5 + i)), "seconds": 24, "result": "finished",
             "singers": 1, "style": None, "party": "Earlier"} for i in range(2)]
    old += [{"pid": U("old-skip"), "id": ver, "base": vb, "at": int(T0 + DAY * 8), "seconds": 4, "result": "skipped",
             "singers": 1, "style": None, "party": "Earlier"}]
    call(A.app, "POST", "/api/encore/plays", {"plays": old}, {"Authorization": "Bearer " + TOKEN})
    CLOCK[0] += 60
    plays = [{"pid": PID[0], "id": solo, "base": sb, "at": int(CLOCK[0]), "seconds": 33, "result": "finished", "singers": 1,
              "style": None, "party": "Friday at Ken's", "guide": "low"},
             {"pid": PID[1], "id": duet, "base": A.load(duet)["published_as"], "at": int(CLOCK[0]) + 60, "seconds": 9,
              "result": "skipped", "singers": 2, "style": "romaji", "party": "Friday at Ken's"},
             {"pid": PID[2], "id": ver, "base": vbase, "at": int(CLOCK[0]) + 120, "seconds": 18, "result": "stopped", "singers": 1,
              "style": None, "party": "Friday at Ken's", "uid": UID}]
    ex(13, "plays", "A batch (up to 200). Counted: finished, or stopped after more than half (the version's 18 of 24 s); the "
       "first one was sung with guide vocals (guide: low), the last one carries a user profile uid (§2.6, planned).", "POST", "/api/encore/plays", {"plays": plays},
       "plays.post.request.schema.json", "plays.post.response.schema.json", expect=200)
    ex(14, "plays-duplicate", "The same batch again (a retry): stored counts them again, nothing is added.", "POST",
       "/api/encore/plays", {"plays": plays}, "plays.post.request.schema.json", "plays.post.response.schema.json", expect=200)

    # ---- made, not published: Glass Orchard is made now
    CLOCK[0] += 600
    A.set_job(lumen, status="done", step="done", finished=CLOCK[0], result={"duration": 30.0, "lang": "en"})
    ex(15, "unpublished", "Made but not published (the host's view links to the Studio's web app).", "GET",
       "/api/encore/unpublished", None, None, "unpublished.get.response.schema.json", expect=200)

    # ---- the library for remote players
    CLOCK[0] = T0 + 11 * DAY
    A.update_catalog()
    for f in os.listdir(A.MV_DIR):  # the files' ETags use their times: the same ones on every run
        os.utime(os.path.join(A.MV_DIR, f), (T0, T0))
    _, rh, _ = ex(16, "catalog", "The catalog for a remote player (§2.0b); body = fixtures/library/catalog.json.", "GET",
                  "/api/encore/catalog", None, None, "catalog.schema.json", expect=200, body_file={"$file": "library/catalog.json"})
    ex(17, "catalog-not-modified", "Unchanged since the ETag the player has.", "GET", "/api/encore/catalog", None, None, None,
       headers={"If-None-Match": rh["etag"]}, expect=304)
    name = f"Music Videos/{sb}.mp4"
    ex(18, "file-range", "A file the catalog lists, with a range (§2.0b); body = the first 16 bytes of that file.", "GET",
       "/api/encore/files/" + name.replace(" ", "%20"), None, None, None, headers={"Range": "bytes=0-15"}, expect=206,
       body_file={"$file": "library/" + name, "$range": [0, 15]})
    ex(19, "file-not-in-catalog", "Only paths the catalog references are served.", "GET", "/api/encore/files/catalog.json.bak",
       None, None, err, expect=404)
    ex(20, "unlinked", "A token the Studio doesn't know (unlinked, or wrong): every call answers 401.", "POST", "/api/encore/plays",
       {"plays": plays[:1]}, "plays.post.request.schema.json", err, auth="fixture-token-revoked-00000000000", client=("192.0.2.41", 50001),
       expect=401)

    # ---- §2.2b party auto-make: the admin turns it on and allows the kit; the searches are made up (no network) and the
    # worker runs here in order; the make times are the measured defaults (no song here has a start time)
    t_am = CLOCK[0]
    CLOCK[0] += 60
    A.AM.save(A.STORE, {"on": True})
    A.STORE.set_automake(PLAYER, True)
    A.automake_enqueue = lambda rid: None   # (run below, in order)

    def made_up_search(q):
        artist, _, title = q.partition(" - ")
        v = lambda i, t, dur, flags=(): {"id": f"am{i}xyzAbCd"[:11], "title": t, "channel": artist, "duration": dur, "views": 1000,
                                         "verified": True, "official_channel": True, "flags": list(flags), "score": 8.0, "thumb": ""}
        return {"artist": artist, "title": title, "notes": [],
                "videos": [v(1, f"{artist} - {title} (Official Video)", 212), v(2, f"{artist} - {title} (Live)", 260, ["live"])],
                "lyrics": [{"source": "lrclib", "id": 900001 + len(title), "artist": artist, "title": title, "album": "", "duration": 211.0,
                            "timed": True, "plain": True, "latin": True, "japanese": False, "native": False, "script": "latin",
                            "english": True, "romaji": None, "lines": 38, "first": "", "artist_match": True, "name_match": True}]}
    A.search = made_up_search
    ex(22, "request-auto", "§2.2b: a party with auto-make on (the guest is already queued with a placeholder), from a player the "
       "Studio's admin allows to auto-make: taken (auto, stage finding, eta: when it should be published).", "POST",
       "/api/encore/requests",
       {"rid": R[4], "artist": "June Arbor", "title": "Copper Moon", "note": "", "by": "Jo", "party": "Friday at Ken's",
        "at": int(CLOCK[0]), "auto": True, "queue": {"eid": "q3f9a1c2d4e", "uid": UID}},
       "requests.post.request.schema.json", "requests.post.response.schema.json", expect=200)
    call(A.app, "POST", "/api/encore/requests", {"rid": R[5], "artist": "June Arbor", "title": "Tin Lantern", "note": "", "by": "Jo",
                                                 "party": "Friday at Ken's", "at": int(CLOCK[0]), "auto": True,
                                                 "queue": {"eid": "q3f9a1c2d4f"}}, {"Authorization": "Bearer " + TOKEN})
    CLOCK[0] += 20
    A.automake_run(R[4])     # found, picked, queued
    A.automake_run(R[5])
    j4, j5 = A.STORE.get_request(R[4])["job"], A.STORE.get_request(R[5])["job"]
    CLOCK[0] += 30
    A.set_job(j4, status="running", step="separate", started=CLOCK[0] - 25, gpu=A.LOCAL_GPU)
    A.set_job(j5, status="failed", step="download", error="yt-dlp failed: ERROR: [youtube] amXXXXXXXXX: Sign in to confirm you're not a bot")
    ex(23, "requests-auto-status", "§2.2b: auto-make progress: one being made (stage, eta), one that failed (its video couldn't be "
       "downloaded: the reason).", "GET", f"/api/encore/requests?rids={R[4]},{R[5]}", None, None,
       "requests.get.response.schema.json", expect=200)
    CLOCK[0] += 60
    st_, _, b_ = call(A.app, "POST", "/api/encore/requests", {"rid": "b9f7c5a8-7d0e-4fc0-8b3c-8e5d6a7b9c0d", "artist": "June Arbor",
                                                             "title": "Glass Kite", "note": "", "by": "Jo", "party": "Friday at Ken's",
                                                             "at": int(CLOCK[0]), "auto": True}, {"Authorization": "Bearer " + TOKEN})
    A.AM.save(A.STORE, {"per_hour": 2})
    ex(32, "request-auto-limit", "§2.2b: taken for auto-make, but over the Studio's limit (here 2 songs an hour): failed at once, "
       "with the reason; the request stays in the Studio's list.", "POST", "/api/encore/requests",
       {"rid": "c0a8d6b9-8e1f-4ad1-9c4d-9f6e7b8c0d1e", "artist": "June Arbor", "title": "Paper Comet", "note": "", "by": "Mia",
        "party": "Friday at Ken's", "at": int(CLOCK[0]), "auto": True, "queue": {"eid": "q3f9a1c2d50"}},
       "requests.post.request.schema.json", "requests.post.response.schema.json", expect=200)
    ex(33, "hello-auto-make", "§2.2b: hello says whether this player may ask for auto-make, and about how long a song asked for "
       "now takes (minutes) until it's published.", "GET", "/api/encore/hello", None, None, "hello.response.schema.json", expect=200)
    A.AM.save(A.STORE, {"on": False, "per_hour": 6})
    CLOCK[0] = t_am + 260   # (the profile exchanges below keep their times)
    # ---- §2.6 profiles: a second player (the home Encore) and one singer's profile, synced, exported, deleted
    CLOCK[0] += 60
    HOME = "8b9c0d1e-2f3a-4b4c-9d5e-6f7a8b9c0d1e"
    A.STORE.new_code()
    A.STORE.code["hash"] = encore.token_hash("H4N8RT2W")
    secrets.token_urlsafe = lambda n=32: "fixture-token-home-0123456789abc"
    st, _, b = call(A.app, "POST", "/api/encore/pair", {"code": "H4N8RT2W", "name": "Encore at home", "player": HOME,
                                                        "encore": "0.6", "contract": "1"}, client=("192.0.2.50", 50100))
    assert st == 200, b
    secrets.token_urlsafe = lambda n=32: "fixture-token-0123456789abcdef"
    HOME_TOKEN = json.loads(b)["token"]
    MASKED.add(HOME_TOKEN)
    EV = lambda k: U("profile-event-" + str(k))
    t = round(CLOCK[0], 3)
    mia = [{"eid": EV(1), "stream": "profile", "uid": UID, "at": t, "name": "Mia", "color": "#ff5fa2",
            "pin_hash": {"alg": "scrypt", "n": 16384, "r": 8, "p": 1, "salt": "00112233445566778899aabbccddeeff",
                         "hash": "a1" * 32}, "created": t, "deleted": False},
           {"eid": EV(2), "stream": "favorites", "uid": UID, "at": t + 12.5, "song": solo, "on": True},
           {"eid": EV(3), "stream": "prefs", "uid": UID, "at": t + 13.25, "key": "style:ja", "value": "romaji"},
           {"eid": EV(4), "stream": "history", "uid": UID, "at": t + 300.75, "song": duet, "base": A.load(duet)["published_as"],
            "result": "finished", "party": "Friday at Ken's", "style": "romaji", "guide": "low"}]
    sreq, sres = "profiles.sync.request.schema.json", "profiles.sync.response.schema.json"
    CLOCK[0] += 400
    _, _, b = ex(24, "profiles-sync-push", "§2.6: the kit sends a new singer's profile, a favourite, a lyric-style pref and a song "
                 "they sang (its first sync: since null). Nothing from other players yet; the cursor covers what it sent.",
                 "POST", "/api/encore/profiles/sync", {"player": PLAYER, "since": None, "events": mia}, sreq, sres, expect=200)
    kit_cursor = json.loads(b)["cursor"]
    _, _, b = ex(25, "profiles-sync-pull", "§2.6: the home Encore syncs: it gets the kit's events in the order the Studio stored "
                 "them (players merge them: latest (at, eid) wins per record, song or key; history is kept whole).",
                 "POST", "/api/encore/profiles/sync", {"player": HOME, "since": None, "events": []}, sreq, sres,
                 auth=HOME_TOKEN, expect=200)
    home_cursor = json.loads(b)["cursor"]
    ex(26, "profiles-sync-repeat", "§2.6: the same events again (a retry after a lost answer): stored once; the kit never gets "
       "its own events back.", "POST", "/api/encore/profiles/sync", {"player": PLAYER, "since": kit_cursor, "events": mia},
       sreq, sres, expect=200)
    ex(27, "profiles-export", "§2.6: everything the Studio keeps about one singer (\"Download my data\"): the winning "
       "favourite per song and pref per key, all history, the players that sent them; the PIN only as set or not.",
       "GET", f"/api/encore/profiles/{UID}/export", None, None, "profiles.export.response.schema.json", expect=200)
    CLOCK[0] += 600
    ex(28, "profiles-sync-delete", "§2.6: the singer deletes their profile on the home Encore: a tombstone. The Studio erases "
       "everything of that uid (also the uid on requests, flags and plays) and keeps only the tombstone.",
       "POST", "/api/encore/profiles/sync", {"player": HOME, "since": home_cursor, "events": [
           {"eid": EV(5), "stream": "profile", "uid": UID, "at": round(CLOCK[0], 3), "deleted": True}]}, sreq, sres,
       auth=HOME_TOKEN, expect=200)
    CLOCK[0] += 60
    _, _, b = ex(29, "profiles-sync-after-delete", "§2.6: the kit comes back with a late favourite for the deleted singer (dropped) and gets "
       "the tombstone, never the singer's other events.", "POST", "/api/encore/profiles/sync",
       {"player": PLAYER, "since": kit_cursor, "events": [{"eid": EV(6), "stream": "favorites", "uid": UID,
                                                          "at": round(CLOCK[0], 3), "song": ver, "on": True}]},
       sreq, sres, expect=200)
    kit_cursor = json.loads(b)["cursor"]
    ex(30, "profiles-export-deleted", "§2.6: a deleted singer exports as deleted, with nothing else.", "GET",
       f"/api/encore/profiles/{UID}/export", None, None, "profiles.export.response.schema.json", expect=200)
    ex(31, "profiles-sync-bad-cursor", "§2.6: a cursor this Studio didn't give (a rebuilt Studio): 400; Encore syncs again with "
       "since null.", "POST", "/api/encore/profiles/sync", {"player": PLAYER, "since": "s999999", "events": []}, sreq, err,
       expect=400)

    # ---- §2.9 words scoring: Nova Lane's first two lines, recorded from 12.0 s (the singer skipped the last two words of
    # line 2); then a call with a header missing, one while the Studio is busy, one whose audio can't be read, and an
    # answer asked for after 10 minutes
    CLOCK[0] += 60
    wh = {"Content-Type": "audio/ogg", "X-Encore-Song": solo, "X-Encore-Base": sb, "X-Encore-Start": "12.000",
          "X-Encore-Rate": "1.0", "X-Encore-Lines": "1,2"}
    hs, wres, wget = "score-words.post.headers.schema.json", "score-words.post.response.schema.json", "score-words.get.response.schema.json"
    _, _, b = ex(34, "score-words", "§2.9: the singer's recording of one song (the body: Opus audio, at most 8 MB; body = "
                 "fixtures/audio/sung.ogg), described by its headers: the song and the version played, the video time at its "
                 "first sample, the playback rate, the lines that are this singer's. Answers 202 with a job to poll.",
                 "POST", "/api/encore/score-words", SUNG_OGG, None, wres, headers=wh, expect=202,
                 req_file={"$file": "audio/sung.ogg"}, headers_schema=hs)
    wjob = json.loads(b)["job"]
    CLOCK[0] += 1
    ex(35, "score-words-queued", "§2.9: not scored yet (eta: about how many seconds).", "GET", f"/api/encore/score-words/{wjob}",
       None, None, wget, expect=200)
    A.WORDS.run_once()
    CLOCK[0] += 4
    ex(36, "score-words-done", "§2.9: scored: each judged word (line = words.json i, w = its index in the line from 0) heard or "
       "not, with the aligner's confidence; heard of total and the score (0-100). Kept 10 minutes.", "GET",
       f"/api/encore/score-words/{wjob}", None, None, wget, expect=200)
    ex(37, "score-words-bad-headers", "§2.9: a header missing or wrong (here X-Encore-Start): 422, before the body is read.",
       "POST", "/api/encore/score-words", SUNG_OGG, None, err, headers={k: v for k, v in wh.items() if k != "X-Encore-Start"},
       expect=422, req_file={"$file": "audio/sung.ogg"}, headers_schema=hs)
    CLOCK[0] += 60
    pick = A.WORDS.pick_device
    A.WORDS.pick_device = lambda: None   # (a song being made, and CPU scoring off)
    _, _, b = ex(38, "score-words-busy", "§2.9: the Studio won't score it now (no graphics card free soon): busy at once; it "
                 "keeps nothing.", "POST", "/api/encore/score-words", SUNG_OGG, None, wres, headers=wh, expect=202,
                 req_file={"$file": "audio/sung.ogg"}, headers_schema=hs)
    A.WORDS.pick_device = pick
    ex(39, "score-words-busy-status", "§2.9: a busy job: the player stops asking (the score has no words part).", "GET",
       f"/api/encore/score-words/{json.loads(b)['job']}", None, None, wget, expect=200)
    CLOCK[0] += 60
    st, _, b = call(A.app, "POST", "/api/encore/score-words", b"not audio at all", dict(wh, **{"Content-Type": "audio/webm",
                                                                                             "Authorization": "Bearer " + TOKEN}))
    assert st == 202, (st, b)
    A.WORDS.run_once()
    CLOCK[0] += 5
    ex(40, "score-words-failed", "§2.9: a recording the Studio couldn't score (here: not Opus audio): failed, with the reason.",
       "GET", f"/api/encore/score-words/{json.loads(b)['job']}", None, None, wget, expect=200)
    CLOCK[0] += 601
    ex(41, "score-words-forgotten", "§2.9: answers are kept 10 minutes; after that (or for a job that isn't this player's): 410.",
       "GET", f"/api/encore/score-words/{wjob}", None, None, err, expect=410)
    if A.WORDS.worker is not None:
        A.WORDS.worker.stop()

    # ---- §2.4 / §2.6 singing scores: a scored play, and a scored song in a singer's history (relayed untouched)
    CLOCK[0] += 60
    score = {"v": 1, "difficulty": "normal", "points": 734210, "pitch": 81.5, "timing": 92.0, "words": 78.0, "acc": 84.2,
             "grade": "A", "max_combo": 57}
    ex(42, "plays-scored", "§2.4: a play with its singing score (the same object as on history events); the Studio keeps it with "
       "the play (its Library shows each song's best grade) and never needs it.", "POST", "/api/encore/plays",
       {"plays": [{"pid": U("scored-play"), "id": solo, "base": sb, "at": int(CLOCK[0]), "seconds": 33, "result": "finished",
                   "singers": 1, "style": None, "party": "Friday at Ken's", "guide": "off", "score": score}]},
       "plays.post.request.schema.json", "plays.post.response.schema.json", expect=200)
    UID2 = "9c0d1e2f-3a4b-4c5d-8e6f-7a8b9c0d1e2f"
    CLOCK[0] += 60
    t2 = round(CLOCK[0], 3)
    ana = [{"eid": EV(7), "stream": "profile", "uid": UID2, "at": t2, "name": "Ana", "color": "#5fb4ff", "pin_hash": None,
            "created": t2, "deleted": False},
           {"eid": EV(8), "stream": "history", "uid": UID2, "at": t2 + 240.5, "song": solo, "base": sb, "result": "stopped",
            "party": "Friday at Ken's", "style": None, "score": dict(score, points=301877, grade="C", max_combo=12, partial=True)}]
    ex(43, "profiles-sync-score", "§2.6: a singer's history event with the singing score (here a song stopped early: partial).",
       "POST", "/api/encore/profiles/sync", {"player": PLAYER, "since": kit_cursor, "events": ana}, sreq, sres, expect=200)
    ex(44, "profiles-sync-score-relayed", "§2.6: the home Encore gets the scored history event exactly as it was sent.", "POST",
       "/api/encore/profiles/sync", {"player": HOME, "since": None, "events": []}, sreq, sres, auth=HOME_TOKEN, expect=200)

    CLOCK[0] += 60
    ex(21, "unpair", "Unlink from Encore's side (the token stops working).", "POST", "/api/encore/unpair", {},
       "unpair.request.schema.json", "unpair.response.schema.json", expect=200)

    # ---- write
    lib_out, api_out, audio_out = os.path.join(OUT, "library"), os.path.join(OUT, "api"), os.path.join(OUT, "audio")
    for d in (lib_out, api_out, audio_out):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(lib_out, "Music Videos"))
    os.makedirs(api_out)
    os.makedirs(audio_out)
    open(os.path.join(audio_out, "sung.ogg"), "wb").write(SUNG_OGG)
    shutil.copyfile(os.path.join(A.LIB, "catalog.json"), os.path.join(lib_out, "catalog.json"))
    for f in sorted(os.listdir(A.MV_DIR)):
        if not f.startswith("."):
            shutil.copyfile(os.path.join(A.MV_DIR, f), os.path.join(lib_out, "Music Videos", f))
    for n, name, d in EXCHANGES:
        with open(os.path.join(api_out, f"{n:02d}-{name}.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.write("\n")
    print(f"wrote {len(EXCHANGES)} exchanges and {len(os.listdir(os.path.join(lib_out, 'Music Videos')))} library files to {OUT}")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
