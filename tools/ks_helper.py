"""What ks shows an agent about a song, worked out by the Studio itself (its own code and libraries): the by-ear check
of the lines as they are now, a picture of the vocals with the line blocks and the heard words, how well other lyrics
fit, and romaji for original-script lines. The Studio serves these to ks over its API (app.py: /api/jobs/<id>/agent/*,
/api/agent/romaji), so ks needs only the Studio's address and a key, wherever it runs.

Each function returns a JSON-able value. For debugging it also runs on its own, with the Studio's Python:
    python -I tools/ks_helper.py check JOB_DIR OFFSET
    python -I tools/ks_helper.py look JOB_DIR OFFSET T0 T1 LINE OUT.png
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


def look(job_dir, offset, t0, t1, sel, out):
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
    font, small, cjk = _font("Arial.ttf", 14), _font("Arial.ttf", 12), _font("DroidSansFallback.ttf", 13)
    # rows: lines (alternating rows so overlapping ones stay apart), heard words, time axis
    vis = [(i, l) for i, l in enumerate(lines) if l["words"][-1][1] + offset >= t0 and l["words"][0][0] + offset <= t1]
    LH, HH, AH = 26, 64, 22
    H = SH + 3 * LH + HH + AH
    img = Image.new("RGB", (W, H), (16, 14, 22))
    img.paste(crop, (0, 0))
    g = ImageDraw.Draw(img)
    x_of = lambda t: int((t - t0) * PXS * X)
    for s in range(int(t0), int(t1) + 1):  # one tick a second, labelled every second
        x = x_of(s)
        if 0 <= x < W:
            g.line([(x, 0), (x, SH + 3 * LH + HH)], fill=(60, 60, 80), width=1)
            g.text((x + 2, H - AH + 4), f"{s // 60}:{s % 60:02d}", font=small, fill=(170, 170, 190))
    for k, (i, l) in enumerate(vis):
        s, e = l["words"][0][0] + offset, l["words"][-1][1] + offset
        y = SH + 4 + (k % 3) * LH
        col = (255, 176, 32) if l.get("weak") else (90, 160, 255)
        if i == sel:
            col = (255, 255, 255)
        g.rectangle([x_of(s), y, max(x_of(s) + 2, x_of(e)), y + LH - 6], outline=col, width=2)
        for ws, we, _w in l["words"]:  # each word's start as a small tick inside the block
            g.line([(x_of(ws + offset), y + LH - 10), (x_of(ws + offset), y + LH - 6)], fill=col, width=1)
        g.text((x_of(s) + 4, y + 2), f"{i + 1}{' !' if l.get('weak') else ''}{' (backing)' if l.get('backing') else ''}", font=font, fill=col)
    yh = SH + 3 * LH + 4
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
    return {"out": out, "seconds": t1 - t0, "lines": [i + 1 for i, _ in vis]}


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
        print(json.dumps(look(a[0], float(a[1]), float(a[2]), float(a[3]), int(a[4]), a[5])))
    elif cmd == "romaji":
        print(json.dumps(romaji(json.load(open(a[0]))), ensure_ascii=False))
    elif cmd == "altlyrics":
        print(json.dumps(altlyrics(a[0], float(a[1]), json.load(open(a[2])))))
    else:
        sys.exit(f"unknown: {cmd}")
