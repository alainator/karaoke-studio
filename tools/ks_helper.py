"""What ks shows an agent about a song, worked out by the Studio itself (its own code and libraries): the by-ear check
of the lines as they are now, a picture of the vocals with the melody, the line blocks and the heard words, where the
forced aligner puts a line heard together with the line before it (align: the only one that needs the GPU, run in
its own process by the Studio), how well other lyrics fit, and romaji for original-script lines. The Studio serves these to ks over its API (app.py: /api/jobs/<id>/agent/*,
/api/agent/romaji), so ks needs only the Studio's address and a key, wherever it runs.

Each function returns a JSON-able value. For debugging it also runs on its own, with the Studio's Python:
    python -I tools/ks_helper.py check JOB_DIR OFFSET
    python -I tools/ks_helper.py look JOB_DIR OFFSET T0 T1 LINE OUT.png [ALIGNED_LINE_JSON]
    python -I tools/ks_helper.py align JOB_DIR OFFSET cuda|cpu PLAN_JSON
"""
import json, os, sys

STUDIO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the Studio's folder (this file is in tools/)
if STUDIO not in sys.path:
    sys.path.insert(0, STUDIO)
FONTS = os.path.join(STUDIO, "static", "octopus")


def lines_of(job_dir):
    d = json.load(open(os.path.join(job_dir, "words.json")))
    return d["lines"] if isinstance(d, dict) else d


def heard_of(job_dir):
    p = os.path.join(job_dir, "heard.json")
    if not os.path.exists(p):
        return None, []
    h = json.load(open(p))
    return h.get("lang"), h.get("words") or []


def check(job_dir, offset):
    from linecheck import heard_offsets
    lines = lines_of(job_dir)
    lang, heard = heard_of(job_dir)
    lead = [i for i, l in enumerate(lines) if not l.get("backing")]
    if heard:
        offs = heard_offsets([lines[i] for i in lead], [[s - offset, e - offset, w] for s, e, w in heard], lang)
    else:
        offs = [None] * len(lead)
    known = bool(heard) and sum(x is not None for x in offs) >= max(4, 0.15 * len(lead))
    sure = sum(1 for x in offs if x is not None and -1.0 <= x <= 2.5)
    return {"offsets": {str(i): (None if x is None else round(float(x), 2)) for i, x in zip(lead, offs)},
            "known": known, "sure": sure, "lead": len(lead), "lang": lang, "heard_words": len(heard)}


def _font(name, size):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(os.path.join(FONTS, name), size)
    except OSError:  # (the renderer's fonts aren't installed: get_renderer.py)
        return ImageFont.load_default()


def melody(job_dir):
    """(f0 Hz, periodicity, hop s) per frame of the vocals from pitch.f0.npz (pitch.py), or None when the song has no
    up-to-date one (it isn't made here: that's CREPE on the GPU, deploy/write-pitch.py)."""
    import numpy as np
    p, v = os.path.join(job_dir, "pitch.f0.npz"), os.path.join(job_dir, "vocals.m4a")
    try:
        z = np.load(p)
        st = os.stat(v)
        if str(z["stamp"]) != f"{st.st_size}:{int(st.st_mtime)}":
            return None
        return z["f0"].astype(np.float64), z["per"].astype(np.float64), float(z["hop"])
    except (OSError, KeyError, ValueError):
        return None


VOICED, JUMP = 0.5, 0.8   # as pitch.py: periodicity above 0.5 = sung; a new note where the pitch moves 0.8 semitone


def melody_marks(f0, per, k0, k1):
    """The melody between frames k0..k1: [(frame, midi)] for voiced frames, and the frames where a new note starts
    (singing starts, or the pitch moves more than JUMP semitones from the note's start for two frames in a row)."""
    import numpy as np
    pts, starts, ref, prev, moved = [], [], None, None, 0
    for k in range(max(0, k0), min(len(f0), k1)):
        if per[k] <= VOICED or not f0[k] > 0:
            ref, prev, moved = None, None, 0
            continue
        m = 69 + 12 * float(np.log2(f0[k] / 440.0))
        pts.append((k, m))
        if ref is None:
            starts.append(k); ref = m
        elif abs(m - ref) > JUMP:
            moved += 1
            if moved >= 2:
                starts.append(k - 1); ref, moved = m, 0
        else:
            moved = 0
        prev = m
    return pts, starts


def look(job_dir, offset, t0, t1, sel, out, aligned=None):
    """The picture ks look shows. Rows, top to bottom: the vocals' spectrogram; the melody (pitch over time, sung
    frames only, a tick where a new note starts: syllables show inside unbroken singing); the line blocks; with
    aligned (an agent/align line, plus "prev_end": where the aligner ends the line before), where the aligner puts
    that line's words and the boundary (a dashed line at its start, a thin one where the line before ends); the words
    heard; the time axis."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from linecheck import _letters
    PXS = 50  # the studio's spectrogram: 50 px per second (app.py SPEC_PXS)
    sp = os.path.join(job_dir, "spectro.png")  # long songs (over about 5:27) are PNG: WebP can't be that wide
    spec = Image.open(sp if os.path.exists(sp) else os.path.join(job_dir, "spectro.webp")).convert("RGB")
    a, b = int(t0 * PXS), min(spec.width, int(t1 * PXS))
    crop = spec.crop((a, 0, max(a + 1, b), spec.height))
    X = 3  # horizontal zoom: 150 px per second
    W, SH = crop.width * X, crop.height * 3
    crop = crop.resize((W, SH), Image.NEAREST)
    lines = lines_of(job_dir)
    lang, heard = heard_of(job_dir)
    mel = melody(job_dir)
    font, small, cjk = _font("Arial.ttf", 14), _font("Arial.ttf", 12), _font("DroidSansFallback.ttf", 13)
    vis = [(i, l) for i, l in enumerate(lines) if l["words"][-1][1] + offset >= t0 and l["words"][0][0] + offset <= t1]
    LH, HH, AH = 26, 64, 22
    MH = 72 if mel else 0                     # melody row
    ALH = 44 if aligned else 0                # aligner row
    y_mel = SH
    y_lines = y_mel + MH
    y_al = y_lines + 3 * LH
    y_heard = y_al + ALH
    H = y_heard + HH + AH
    rows = ["spectrogram"] + (["melody"] if mel else []) + ["lines"] + (["aligner"] if aligned else []) + ["heard"]
    img = Image.new("RGB", (W, H), (16, 14, 22))
    img.paste(crop, (0, 0))
    g = ImageDraw.Draw(img)
    x_of = lambda t: int((t - t0) * PXS * X)
    for s in range(int(t0), int(t1) + 1):  # one tick a second, labelled every second
        x = x_of(s)
        if 0 <= x < W:
            g.line([(x, 0), (x, H - AH)], fill=(60, 60, 80), width=1)
            g.text((x + 2, H - AH + 4), f"{s // 60}:{s % 60:02d}", font=small, fill=(170, 170, 190))
    if mel:   # the melody: semitones, scaled to what's sung in the window
        f0, per, hop = mel
        pts, starts = melody_marks(f0, per, int(t0 / hop), int(t1 / hop) + 1)
        g.rectangle([0, y_mel, W, y_mel + MH - 1], fill=(22, 20, 32))
        if pts:
            ms = np.array([m for _, m in pts])
            lo, hi = float(np.percentile(ms, 2)) - 1, float(np.percentile(ms, 98)) + 1
            if hi - lo < 12:
                mid = (hi + lo) / 2; lo, hi = mid - 6, mid + 6
            y_of = lambda m: int(y_mel + MH - 6 - (min(hi, max(lo, m)) - lo) / (hi - lo) * (MH - 14))
            col = (90, 220, 235)
            for (k, m), (k2, m2) in zip(pts, pts[1:]):
                if k2 == k + 1 and abs(m2 - m) < 1.5:
                    g.line([(x_of(k * hop), y_of(m)), (x_of(k2 * hop), y_of(m2))], fill=col, width=2)
                else:
                    g.point((x_of(k * hop), y_of(m)), fill=col)
            for k in starts:   # a new note (a syllable, usually)
                g.line([(x_of(k * hop), y_mel + 1), (x_of(k * hop), y_mel + 7)], fill=(230, 240, 255), width=1)
        g.text((4, y_mel + 2), "melody", font=small, fill=(90, 220, 235))
    for k, (i, l) in enumerate(vis):
        s, e = l["words"][0][0] + offset, l["words"][-1][1] + offset
        y = y_lines + 4 + (k % 3) * LH
        col = (255, 176, 32) if l.get("weak") else (90, 160, 255)
        if i == sel:
            col = (255, 255, 255)
        g.rectangle([x_of(s), y, max(x_of(s) + 2, x_of(e)), y + LH - 6], outline=col, width=2)
        for ws, we, _w in l["words"]:  # each word's start as a small tick inside the block
            g.line([(x_of(ws + offset), y + LH - 10), (x_of(ws + offset), y + LH - 6)], fill=col, width=1)
        g.text((x_of(s) + 4, y + 2), f"{i + 1}{' !' if l.get('weak') else ''}{' (backing)' if l.get('backing') else ''}", font=font, fill=col)
    if aligned:   # the aligner's word starts for the asked line (magenta): a tick, a bar to the word's end, the word
        col = (255, 100, 210)
        if aligned.get("at") is not None and t0 <= aligned["at"] <= t1:   # the boundary with the line before: dashed
            xb = x_of(aligned["at"])
            for y in range(y_mel, y_al + ALH, 8):
                g.line([(xb, y), (xb, min(y + 4, y_al + ALH))], fill=col, width=1)
            if aligned.get("prev_end") is not None and t0 <= aligned["prev_end"] <= t1:   # where the line before ends
                xe = x_of(aligned["prev_end"])
                g.line([(xe, y_lines), (xe, y_al + 12)], fill=(200, 120, 255), width=1)
                g.line([(xe, y_al + 2), (xb, y_al + 2)], fill=(200, 120, 255), width=1)
            g.text((xb + 3, y_lines + 1), "boundary", font=small, fill=col)
        last_x = [-999, -999]
        for w in aligned["words"]:
            if w.get("at") is None or not (t0 <= w["at"] <= t1):
                continue
            x = x_of(w["at"])
            if w.get("end") is not None:
                g.line([(x, y_al + 7), (max(x, x_of(w["end"])), y_al + 7)], fill=(130, 60, 115), width=3)
            g.line([(x, y_al + 2), (x, y_al + 12)], fill=col, width=2)
            word = w["w"]
            f = cjk if any(ord(c) > 0x2E7F for c in word) else small
            r = min(range(2), key=lambda k: last_x[k])
            g.text((x + 1, y_al + 12 + r * 14), word, font=f, fill=col)
            last_x[r] = x + int(g.textlength(word, font=f)) + 4
    yh = y_heard + 4
    last_x = [-999, -999, -999]
    for s, e, w in heard:
        if not (t0 <= s <= t1):
            continue
        x = x_of(s)
        word = w.strip()
        if any(ord(c) > 0x2E7F for c in word):
            f = cjk
        elif any(ord(c) > 0x24F for c in word):
            word, f = _letters(word, lang) or word, small   # other scripts: romanized the way the lyrics are
        else:
            f = small
        r = min(range(3), key=lambda k: last_x[k])  # the row whose last word ends furthest left
        g.line([(x, yh), (x, yh + 6)], fill=(120, 230, 140), width=1)
        g.text((x + 1, yh + 6 + r * 18), word, font=f, fill=(120, 230, 140))
        last_x[r] = x + int(g.textlength(word, font=f)) + 4
    img.save(out)
    return {"out": out, "seconds": t1 - t0, "lines": [i + 1 for i, _ in vis], "rows": rows}


# ---------------- align: where the aligner puts a line when it hears it together with its neighbours ----------------
# Measured 2026-11-24 on 53 songs a person fixed (154 moved lines, 2212 kept): lines sung straight on from the previous
# one are often placed 0.5-2 s late (crowd timing, and the pipeline only searches from the line's LRC start). Aligning
# the previous line and this one TOGETHER over [previous start - 0.3, next start - 0.05] put 25% of the moved lines
# within 0.3 s of the person's fix (the Studio: 1%) but also moves about 10% of good lines: evidence, not a fix.
ALIGN_MAX = 60.0     # seconds of audio at most in one window


def lead_index(lines):
    return [i for i, l in enumerate(lines) if not l.get("backing") and l.get("words")]


def align_plan(lines, offset, n=None, t0=None, t1=None, duration=None):
    """Which lines to align together and over which window (video time). n: a 0-based line (the measured method:
    with the previous lead line, to the next lead line's start); or t0..t1: every lead line inside it.
    -> {"window": [a, b], "lines": [i...], "target": i | None, "prev": i | None} or raises ValueError (why)."""
    lead = lead_index(lines)
    start = lambda i: lines[i]["words"][0][0] + offset
    end = lambda i: lines[i]["words"][-1][1] + offset
    dur = duration or 1e9
    if n is not None:
        if not 0 <= n < len(lines):
            raise ValueError(f"line {n + 1} doesn't exist (1-{len(lines)})")
        if n not in lead:
            raise ValueError(f"line {n + 1} is a backing line: align a stretch with --window instead")
        k = lead.index(n)
        prev = lead[k - 1] if k else None
        nxt = lead[k + 1] if k + 1 < len(lead) else None
        a = max(0.0, (start(prev) if prev is not None else start(n) - 2.0) - 0.3)
        b = min(dur, max((start(nxt) - 0.05) if nxt is not None else end(n) + 3.0, end(n) + 0.2))
        if b - a > ALIGN_MAX:
            raise ValueError(f"the previous line starts {b - a:.0f} s before the next one (a long break between "
                             f"them): align a shorter stretch with --window")
        return {"window": [round(a, 2), round(b, 2)], "lines": ([prev] if prev is not None else []) + [n],
                "target": n, "prev": prev}
    if t0 is None or t1 is None or not 0 <= t0 < t1:
        raise ValueError("give a line, or a window start < end")
    if t1 - t0 > ALIGN_MAX:
        raise ValueError(f"at most {ALIGN_MAX:.0f} s at a time")
    inside = [i for i in lead if start(i) >= t0 - 0.05 and end(i) <= t1 + 0.5]
    if not inside:
        raise ValueError("no whole lead line inside that window")
    return {"window": [round(max(0.0, t0), 2), round(min(dur, t1), 2)], "lines": inside, "target": None, "prev": None}


def _clip(job_dir, a, b, sr=16000):
    import subprocess
    import numpy as np
    # the whole file decoded and cut here, as the pipeline does (an input seek could land a few ms off)
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", os.path.join(job_dir, "vocals.m4a"), "-vn", "-ac", "1",
                        "-ar", str(sr), "-f", "f32le", "-"], capture_output=True, check=True)
    y = np.frombuffer(r.stdout, np.float32)
    return y[int(a * sr):int(b * sr)].copy()


def align_words(job_dir, a, b, texts, device="cpu"):
    """Force-align words (their text as shown) in the vocals between a and b (video time), with torchaudio's MMS_FA
    and the same letters as the pipeline (romanize.sound_key). -> [(start, end, confidence 0-1) | None per word]
    (None: a word with no letters, e.g. "&")."""
    import numpy as np, torch, torchaudio
    from romanize import sound_key
    SR = 16000
    norm = [sound_key(w) for w in texts]
    keep = [k for k, x in enumerate(norm) if x]
    clip = _clip(job_dir, a, b, SR)
    if not keep:
        return [None] * len(texts)
    if len(clip) < SR // 4:
        raise ValueError("the window is too short (or past the end of the vocals)")
    if device == "cpu":
        torch.set_num_threads(min(4, os.cpu_count() or 1))
    fa = torchaudio.pipelines.MMS_FA
    model = fa.get_model(with_star=False).to(device)
    tok, aligner = fa.get_tokenizer(), fa.get_aligner()
    with torch.inference_mode():
        em, _ = model(torch.from_numpy(clip).float().unsqueeze(0).to(device))
        try:
            spans = aligner(em[0], tok([norm[k] for k in keep]))
        except Exception as e:  # more letters than the window has frames
            raise ValueError(f"the words don't fit in this window ({str(e)[:80]})")
    fps = len(clip) / SR / em.size(1)
    out = [None] * len(texts)
    for k, sp in zip(keep, spans):
        out[k] = (round(a + sp[0].start * fps, 3), round(a + sp[-1].end * fps, 3),
                  round(float(np.mean([s.score for s in sp])), 3))
    return out


def align_report(lines, offset, plan, got):
    """Per line of the plan: where its words are now and where the aligner puts them (video time), its start's move
    and the line's confidence (mean of its words'). got: align_words' answer for the plan's words, in order."""
    out, k = [], 0
    for i in plan["lines"]:
        ws = lines[i]["words"]
        mine = got[k:k + len(ws)]
        k += len(ws)
        words = [{"w": w[2], "now": round(w[0] + offset, 2),
                  "at": g[0] if g else None, "end": g[1] if g else None, "conf": g[2] if g else None}
                 for w, g in zip(ws, mine)]
        placed = [x for x in words if x["at"] is not None]
        now = round(ws[0][0] + offset, 2)
        at = placed[0]["at"] if placed else None
        out.append({"line": i + 1, "now": now, "now_end": round(ws[-1][1] + offset, 2), "at": at,
                    "end": placed[-1]["end"] if placed else None,
                    "delta": round(at - now, 2) if at is not None else None,
                    "conf": round(sum(x["conf"] for x in placed) / len(placed), 2) if placed else None, "words": words})
    return out


def align(job_dir, offset, plan, device="cpu"):
    """Run a plan (align_plan) on the job's vocals: {"window", "target", "prev", "device", "seconds", "lines": [...]}."""
    import time
    from assgen import load_lines
    t = time.time()
    lines = load_lines(os.path.join(job_dir, "words.json"))
    texts = [w[2] for i in plan["lines"] for w in lines[i]["words"]]
    got = align_words(job_dir, plan["window"][0], plan["window"][1], texts, device)
    return dict(plan, device=device, seconds=round(time.time() - t, 1), lines=align_report(lines, offset, plan, got))


def altlyrics(job_dir, offset, cands):
    """How well each LRC (album times) fits what's heard in this video: best whole-song shift, then the share of
    lines whose letters are found in the heard words around their (shifted) time. Also: squeezed lines (several
    lines within a second of each other: a sign of a careless sync)."""
    import re
    from difflib import SequenceMatcher
    from linecheck import _letters
    from romanize import sound_key
    lang, heard = heard_of(job_dir)
    hk = [(s, _letters(str(w), lang)) for s, e, w in heard]
    out = []
    for c in cands:
        rows = []
        for line in (c.get("synced") or "").splitlines():
            ts = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", line)
            text = re.sub(r"\[[^\]]*\]", "", line).strip()
            for m, sec in ts:
                if text:
                    rows.append((int(m) * 60 + float(sec), sound_key(text), len(text.split())))
        rows.sort()
        gaps = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
        squeezed = sum(1 for g in gaps if g < 0.8)
        test = [r for r in rows if r[2] >= 2 and len(r[1]) >= 6]

        def fit(off):
            n = 0
            for t, key, nw in test:
                a, b = t + off - 1.0, t + off + max(3.0, 0.5 * nw) + 1.0
                win = "".join(k for s, k in hk if a <= s <= b)
                if win:
                    m = sum(x.size for x in SequenceMatcher(None, key, win, autojunk=False).get_matching_blocks())
                    n += m >= 0.6 * len(key)
            return n
        best = max(range(-30, 31), key=lambda o: fit(float(o))) if test and hk else 0
        fine = max((best + k / 4.0 for k in range(-4, 5)), key=fit) if test and hk else 0.0
        f = fit(fine) if test and hk else 0
        out.append({"id": c["id"], "lines": len(rows), "squeezed": squeezed, "shift": round(fine, 2),
                    "fit": round(f / max(1, len(test)), 3), "tested": len(test)})
    return out


def romaji(lines):
    """Romanize original-script lyric lines the way the studio does (cutlet for Japanese, its own for Korean and
    Chinese), so ks can find which original line a romaji-only line came from."""
    import re
    kana = re.compile(r"[\u3040-\u30ff]")
    if any(kana.search(t) for t in lines):
        import cutlet
        kat = cutlet.Cutlet()
        kat.use_foreign_spelling = False
        conv = kat.romaji
    else:
        from romanize import script_of, romanize_korean, romanize_mandarin
        sc = script_of(lines)
        conv = romanize_korean if sc == "ko" else romanize_mandarin if sc == "zh" else None
    return [re.sub(r"\s+", " ", conv(t)).strip() if conv else "" for t in lines]


if __name__ == "__main__":
    cmd, a = sys.argv[1], sys.argv[2:]
    if cmd == "check":
        print(json.dumps(check(a[0], float(a[1]))))
    elif cmd == "look":
        print(json.dumps(look(a[0], float(a[1]), float(a[2]), float(a[3]), int(a[4]), a[5],
                             json.loads(a[6]) if len(a) > 6 else None)))
    elif cmd == "romaji":
        print(json.dumps(romaji(json.load(open(a[0]))), ensure_ascii=False))
    elif cmd == "altlyrics":
        print(json.dumps(altlyrics(a[0], float(a[1]), json.load(open(a[2])))))
    elif cmd == "align":   # JOB_DIR OFFSET DEVICE PLAN_JSON (align_plan's answer); errors as {"error": why}
        try:
            print(json.dumps(align(a[0], float(a[1]), json.loads(a[3]), a[2]), ensure_ascii=False))
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
    else:
        sys.exit(f"unknown: {cmd}")
