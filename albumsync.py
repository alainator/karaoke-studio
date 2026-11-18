"""Map a music video's timeline onto the album recording, by matching their music.

LRCLIB's timestamps are for the album version, and music videos often differ from it: a skit before the song, a scene
that stops the music partway, a cut. Instead of guessing those differences from the lyrics, find the album audio
itself (YouTube Music's auto-generated "Artist - Topic" upload, or an "Official Audio" one, of the album's length),
compare the music of both, and get an exact map: "album 0:00-1:05 is at video +67.9 s, then +76.5 s ...".

    segs = album_map(video_audio_path, artist, title, album_length, work_dir, ytdlp, env)
    # -> [{"a0": album start, "a1": album end, "off": video time - album time, "score": match}] or None

Method: chroma (CENS) features of both at 10 frames a second; every 6 s chunk of the album is compared with every
position in the video (cosine similarity along the diagonal); a Viterbi path over the offsets picks, for each chunk,
the offset that matches well while changing as rarely as possible; each stretch of equal offset is then fine-tuned
on note onsets to a few hundredths of a second. Returns None when no album audio is found or the match is weak, so
the caller falls back to working from the lyrics alone.
"""
import json, os, re, subprocess

import numpy as np

SR = 22050
FPS = 10                  # feature frames per second
WIN, STEP = 6 * FPS, 2 * FPS   # album chunks of 6 s, every 2 s
BAD = re.compile(r"\b(live|cover|remix|karaoke|instrumental|acoustic|sped|slowed|nightcore|8d|reverb|mashup|edit|"
                 r"video|4k|\d+\s*fps|mv|m/v|pv|clip|reaction|lyrics? video|vma|vmas|awards?|concert|performance|"
                 r"tour|festival|session|unplugged|rehearsal|demo)\b", re.I)


def _norm(x):
    return re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()


def find_album_audio(artist, title, album_len, ytdlp, env, exclude=(), min_len=30.0, max_len=1e9):
    """YouTube uploads that may be the album recording, best first: not live/cover/remix, a plausible length (long
    enough for the lyrics, not longer than the video); the auto-generated 'Artist - Topic' uploads (YouTube Music,
    exactly the album track) first, then 'Official Audio', then the closest to LRCLIB's length. LRCLIB's length can
    be wrong (The Kill: listed 276 s, the album track is 231 s), so the caller checks each by matching the music."""
    seen, cands = set(), []
    for q in (f"{artist} {title} audio", f"{artist} - {title} topic", f"{artist} {title}"):
        r = subprocess.run([ytdlp, "--js-runtimes", "node", "--flat-playlist", "-J", "--", f"ytsearch12:{q}"],
                           capture_output=True, text=True, timeout=90, env=env)
        if r.returncode:
            continue
        for e in json.loads(r.stdout).get("entries") or []:
            vid, dur = e.get("id"), e.get("duration")
            if not vid or vid in seen or vid in exclude or not dur:
                continue
            seen.add(vid)
            name, ch = e.get("title") or "", e.get("channel") or e.get("uploader") or ""
            if not (min_len <= float(dur) <= max_len) or BAD.search(name):
                continue
            if _norm(title) and _norm(title) not in _norm(name):
                continue
            topic = ch.endswith(" - Topic")
            rank = (0 if topic else 1, 0 if "official audio" in name.lower() else 1, abs(float(dur) - album_len))
            cands.append((rank, vid, name, ch, float(dur)))
    cands.sort()
    return [{"id": vid, "title": name, "channel": ch, "duration": dur} for _, vid, name, ch, dur in cands]


def _decode(path, sr=SR):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def _chroma(y):
    import librosa
    c = librosa.feature.chroma_cens(y=y, sr=SR, hop_length=SR // FPS)  # 12 x frames, unit-norm columns
    c = c.T.astype(np.float32)
    c /= np.linalg.norm(c, axis=1, keepdims=True) + 1e-6
    return c


def _onsets(y):
    import librosa
    hop = 256
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=hop)
    env = (env - env.mean()) / (env.std() + 1e-6)
    return env, SR / hop


def _chunk_scores(A, M):
    """For each album chunk, its similarity with every video position: (chunks, positions)."""
    n_a, n_m = len(A), len(M)
    starts = list(range(0, max(1, n_a - WIN + 1), STEP))
    P = n_m - WIN + 1
    out = np.full((len(starts), max(P, 1)), -1.0, dtype=np.float32)
    if P < 1:
        return starts, out
    for ci, s in enumerate(starts):
        S = A[s:s + WIN] @ M.T          # WIN x n_m cosine similarities
        acc = np.zeros(P, dtype=np.float32)
        for k in range(WIN):
            acc += S[k, k:k + P]
        out[ci] = acc / WIN
    return starts, out


def album_map(video_path, artist, title, album_len, work, ytdlp, env, video_id=None, say=lambda *a: None,
              lyrics_end=0.0, video_len=None):
    """Find the album recording and map the video onto it; None if none matches well (see the module doc)."""
    yv = _decode(video_path)
    video_len = video_len or len(yv) / SR
    try:
        found = find_album_audio(artist, title, album_len or 0, ytdlp, env, exclude=(video_id,) if video_id else (),
                                 min_len=max(30.0, lyrics_end), max_len=video_len + 5.0)
    except Exception as e:
        say(f"album audio search failed ({str(e)[:60]})")
        return None
    if not found:
        say("no album recording found to compare the video with")
        return None
    M = _chroma(yv)
    for cand in found[:3]:
        path = os.path.join(work, f"{cand['id']}-album.m4a")
        if not os.path.exists(path):
            r = subprocess.run([ytdlp, "--js-runtimes", "node", "-q", "--no-playlist", "--no-progress", "-f", "bestaudio[ext=m4a]/bestaudio",
                                "-o", path, "--", f"https://www.youtube.com/watch?v={cand['id']}"], capture_output=True, text=True,
                               timeout=300, env=env)
            if r.returncode or not os.path.exists(path):
                continue
        ya = _decode(path)
        segs = _map_one(ya, yv, M)
        if segs and len(segs) == 1 and abs(segs[0]["off"]) < 1.5 and abs(len(ya) / SR - video_len) < 3.0:
            say(f"\"{cand['title']}\" is the video's own soundtrack (a re-upload), not the album")
            continue  # a copy of the video itself: tells nothing about how the video differs from the album
        if segs:
            say(f"album recording: \"{cand['title']}\" ({cand['channel']}): {segs[0]['cover']:.0%} of it found in the video, "
                f"{len(segs)} part{'s' if len(segs) != 1 else ''}")
            return segs
        say(f"\"{cand['title']}\" doesn't match the video's music well enough")
    return None


def _map_one(ya, yv, M):
    A = _chroma(ya)
    starts, sc = _chunk_scores(A, M)
    if not len(starts) or sc.shape[1] < 2:
        return None
    # Viterbi over offsets (in frames): staying costs nothing, changing offset costs PEN; each chunk scores its
    # similarity at that offset. Offsets cover every way the album chunk can sit in the video.
    n_c, P = sc.shape
    offs = np.arange(-starts[-1], P)               # video position - album position, in frames
    G = len(offs)
    score_at = np.full((n_c, G), -1.0, dtype=np.float32)
    for ci, s in enumerate(starts):
        pos = offs + s
        ok = (pos >= 0) & (pos < P)
        score_at[ci, ok] = sc[ci, pos[ok]]
    PEN = 0.8
    dp = score_at[0].copy()
    back = np.zeros((n_c, G), dtype=np.int32)
    for ci in range(1, n_c):
        k = int(np.argmax(dp))
        jump = dp[k] - PEN
        take = jump > dp
        back[ci] = np.where(take, k, np.arange(G))
        dp = score_at[ci] + np.where(take, jump, dp)
    path = [int(np.argmax(dp))]
    for ci in range(n_c - 1, 0, -1):
        path.append(int(back[ci][path[-1]]))
    path = path[::-1]
    chosen = np.array([score_at[ci, g] for ci, g in enumerate(path)])
    good = chosen >= 0.75
    if good.mean() < 0.6:
        return None
    # stretches of equal offset -> segments
    segs = []
    for ci, g in enumerate(path):
        o = float(offs[g]) / FPS
        a0, a1 = starts[ci] / FPS, (starts[ci] + WIN) / FPS
        if segs and abs(segs[-1]["off"] - o) < 0.25:
            segs[-1].update(a1=a1, n=segs[-1]["n"] + 1, score=segs[-1]["score"] + float(chosen[ci]), good=segs[-1]["good"] + int(good[ci]))
        else:
            segs.append({"a0": a0, "a1": a1, "off": o, "n": 1, "score": float(chosen[ci]), "good": int(good[ci])})
    # a lone 6 s chunk that matched somewhere else (a repeated riff) is noise: its neighbours decide
    segs = [s for s in segs if s["n"] >= 2 or len(segs) == 1]
    if not segs:
        return None
    for k in range(1, len(segs)):  # the parts meet halfway where noise was dropped
        if segs[k]["a0"] > segs[k - 1]["a1"]:
            mid = (segs[k - 1]["a1"] + segs[k]["a0"]) / 2
            segs[k - 1]["a1"], segs[k]["a0"] = mid, mid
    segs[0]["a0"] = 0.0
    segs[-1]["a1"] = max(segs[-1]["a1"], len(ya) / SR)
    # exact boundaries: the chunks only place a scene or cut to within a few seconds. Around each boundary, compare
    # album and video frame by frame under both offsets; the boundary is where the better one switches
    for k in range(1, len(segs)):
        f1, f2 = int(round(segs[k - 1]["off"] * FPS)), int(round(segs[k]["off"] * FPS))
        b0 = int(round(segs[k]["a0"] * FPS))
        lo, hi = max(0, b0 - 10 * FPS), min(len(A), b0 + 10 * FPS)
        fr_ = np.arange(lo, hi)
        def _sim(f_off):
            v = np.full(len(fr_), 0.0, dtype=np.float32)
            j = fr_ + f_off
            ok = (j >= 0) & (j < len(M))
            v[ok] = np.einsum("ij,ij->i", A[fr_[ok]], M[j[ok]])
            return np.convolve(v, np.ones(FPS) / FPS, mode="same")  # over ~1 s
        s1, s2 = _sim(f1), _sim(f2)
        if len(fr_) > 2:
            gain = np.concatenate([[0.0], np.cumsum(s1 - s2)])  # keeping offset 1 up to x, offset 2 after it
            x = int(np.argmax(gain[1:])) + 1 if gain[1:].max() > 0 else 0
            cut = (lo + x) / FPS
            segs[k - 1]["a1"], segs[k]["a0"] = cut, cut
    # fine-tune each part on note onsets (±0.5 s)
    env_a, fr = _onsets(ya)
    env_v, _ = _onsets(yv)
    out = []
    for s in segs:
        if s["good"] < max(1, 0.5 * s["n"]):
            continue  # a stretch that doesn't really match (the video cut it, or noise over it)
        i0, i1 = int(s["a0"] * fr), int(min(s["a1"], len(ya) / SR) * fr)
        best, best_c = s["off"], -1e9
        for d in np.arange(-0.5, 0.501, 1 / fr):
            j0 = int(round((s["a0"] + s["off"] + d) * fr))
            if j0 < 0 or j0 + (i1 - i0) > len(env_v) or i1 <= i0:
                continue
            c = float(np.dot(env_a[i0:i1], env_v[j0:j0 + (i1 - i0)])) / (i1 - i0)
            if c > best_c:
                best, best_c = s["off"] + d, c
        out.append({"a0": round(s["a0"], 2), "a1": round(s["a1"], 2), "off": round(best, 3), "score": round(s["score"] / s["n"], 3)})
    if not out:
        return None
    # a believable map: studio recordings match at 99-100 % (a live version of the song still matches its notes at
    # ~92 % and jumps all over), its parts follow each other through the video, and there are only a few of them
    mean_score = float(np.mean([s["score"] for s in out]))
    in_order = all(out[k]["a0"] + out[k]["off"] >= out[k - 1]["a1"] + out[k - 1]["off"] - 2.0 for k in range(1, len(out)))
    if mean_score < 0.96 or not in_order or len(out) > 5:
        return None
    out[0]["cover"] = float(good.mean())
    return out


def to_video(segs, t):
    """Album time -> video time with the segment covering it (or the nearest one)."""
    if not segs:
        return None
    inside = [s for s in segs if s["a0"] - 1.0 <= t <= s["a1"] + 1.0]
    s = inside[0] if inside else min(segs, key=lambda s: min(abs(t - s["a0"]), abs(t - s["a1"])))
    return t + s["off"]


def seg_off(segs, a):
    """The video offset at album time a (the part covering it, or the nearest one)."""
    inside = [s for s in segs if s["a0"] <= a < s["a1"]]
    s = inside[0] if inside else min(segs, key=lambda s: min(abs(a - s["a0"]), abs(a - s["a1"])))
    return s["off"]


if __name__ == "__main__":
    # python -I albumsync.py <video> <artist> <title> <album length> <last lyric time> <work dir> <yt-dlp> <video id>
    # prints "@say <message>" lines, then "@segs <json>" (or "@segs null")
    import sys
    video, artist, title, alen, lend, work, ytdlp, vid = sys.argv[1:9]
    env = dict(os.environ)
    segs = None
    try:
        segs = album_map(video, artist, title, float(alen or 0), work, ytdlp, env, video_id=vid, lyrics_end=float(lend or 0),
                         say=lambda m: print("@say " + m, flush=True))
    except Exception as e:
        print(f"@say album matching failed ({str(e)[:80]})", flush=True)
    print("@segs " + json.dumps(segs), flush=True)
