"""The published-song catalog for players other than PiKaraoke (Encore): catalog.json in the library root, and a
Name.words.json next to each published song.

    catalog.json  {"version": 2, "generated": unix time, "songs": [entry, ...]}
      entry: {"id": job id, "artist", "title", "title_orig": title in its own script or null, "lang",
              "duration": seconds, "published": unix time, "folder": "Music Videos" (relative to the library root),
              "base": "Artist - Title (Music Video)" (the .mp4 / .ass names without extension),
              "lyrics": {"file": "<base>.ass", "default_mode": "both"|...|null, "modes": {mode: "<base>.<mode>.ass"}},
              "versions": [{"name": "Mia's version", "base": "...", "published": t, "pitch": "<base>.pitch.json"
                          or null, "vocals": "<base>.vocals.m4a" or null}]  (their lyric files are named like the
                          song's: <version base>.ass, .<mode>.ass, .words.json),
              "parts": [{"key": "m0"|"s1", "name", "color": "#rrggbb", "color_name", "lines": n}],
              "together": bool (lines sung by several parts), "group": normalised artist|title (two lyric-language
              versions of one song share it), "words": "<base>.words.json" or null, "lines": n, "first": seconds,
              "job_deleted": true (only on songs whose studio job was deleted while the files stayed),
              version 2 adds: "pitch": "<base>.pitch.json" or null (the sung melody, pitch.py), "plays": n and
              "last_played": unix time or null (plays of the song and its versions that count, from every player
              that reports them: Encore, and PiKaraoke's history), "vocals": "<base>.vocals.m4a" or null (the
              separated lead vocal on the video's timeline, trims applied, for guide vocals: guidevocals.py)}
      The contract with players (contract/ENCORE-STUDIO.md, contract/schemas/catalog.schema.json) says which changes
      are allowed: additive only; players ignore fields they don't know.

    Name.words.json  {"version": 1, "job", "base", "duration", "parts": [...as above], "together_color",
                      "lines": [{"i", "start", "end", "words": [[start, end, "word"], ...], "parts": [keys],
                                 "word_parts": [[keys] per word] (only lines shared word by word),
                                 "orig": "original-script row" (optional), "backing": true (optional)}]}
      times are VIDEO times (the job's timing offset applied), like the published .ass. "parts" [] = not assigned
      (everyone sings it).

Only songs whose files exist in the library are listed; their lyric modes are the .ass files actually there. Writing is
atomic (a temporary file then rename) and never raises out of refresh(): a failure is logged, publishing goes on.
"""
import json, os, re, threading, time, unicodedata

from assgen import MEMBER_RGB, MODES, SINGER_FILL, TOGETHER_FILL, load_cast, load_lines

CATALOG = "catalog.json"
VERSION = 2
WORDS_EXT = ".words.json"
PITCH_EXT = ".pitch.json"
VOCALS_EXT = ".vocals.m4a"   # (guidevocals.EXT)
_lock = threading.Lock()
PLAYS = None  # () -> play rows for add_plays; set by app.py (its store); None: encore.play_rows()

MEMBER_COLOR_NAMES = ["blue", "red", "green", "purple", "orange", "pink", "teal", "lime"]
SINGER_COLOR_NAMES = {1: "gold", 2: "blue", 3: "green"}


def ass_rgb(c):
    """&HBBGGRR& (ASS) -> #rrggbb."""
    h = re.sub(r"[^0-9A-Fa-f]", "", c.replace("&H", ""))[-6:].rjust(6, "0")
    return f"#{h[4:6]}{h[2:4]}{h[0:2]}".lower()


def _job_dir_ok(d):
    return os.path.isfile(os.path.join(d, "job.json"))


# ---- names ----
_LANG_PAREN = re.compile(r"\s*[\(\[\{（【]([^\)\]\}）】]*)[\)\]\}）】]")
_LANG_WORDS = re.compile(r"\b(ver|vers|version|english|eng|en|japanese|jp|jpn|korean|kor|kr|chinese|cn|mandarin|"
                         r"cantonese|spanish|french|german|italian|portuguese|vietnamese|thai|tagalog|romaji|"
                         r"lyrics?|tv ?size|full|translation|translated)\b", re.I)


def has_own_script(s):
    """True if the text has letters of a script other than Latin (Japanese, Korean, Cyrillic, Arabic…)."""
    return any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in s or "")


def norm_key(s):
    """Unicode-aware: lower case, accents dropped, punctuation to spaces; Japanese/Korean/… letters are kept."""
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"[\W_]+", " ", s).strip()


def group_key(artist, title):
    """artist|title, normalised, without "(English version)"-style notes, so two lyric-language versions of one song
    land in one group."""
    t = _LANG_PAREN.sub(lambda m: "" if _LANG_WORDS.search(m.group(1)) else m.group(0), title or "")
    return f"{norm_key(artist)}|{norm_key(t) or norm_key(title)}"


# ---- lines and parts ----
def _line_parts(l):
    m = l.get("members") or []
    if m:
        return [f"m{i}" for i in m]
    s = l.get("singer")
    if s in (1, 2):
        return [f"s{s}"]
    if s == 3:
        return ["s1", "s2"]
    return []


def _who_parts(w):
    if not w:
        return None
    if w.get("members"):
        return [f"m{i}" for i in w["members"]]
    s = w.get("singer")
    return ["s1", "s2"] if s == 3 else ([f"s{s}"] if s in (1, 2) else None)


def parts_of(lines, cast):
    """The song's parts (only the ones some line uses), and whether lines are shared by several."""
    used, together = {}, False
    for l in lines:
        keys = set(_line_parts(l))
        for w in (l.get("wwho") or []):
            keys |= set(_who_parts(w) or [])
        if len(_line_parts(l)) > 1:
            together = True
        for k in keys:
            used[k] = used.get(k, 0) + 1
    out = []
    for i in range(8):
        k = f"m{i}"
        if k in used:
            name = cast[i] if i < len(cast) and cast[i] else f"Singer {i + 1}"
            out.append({"key": k, "name": name, "color": MEMBER_RGB[i].lower(), "color_name": MEMBER_COLOR_NAMES[i],
                        "lines": used[k]})
    for s in (1, 2):
        k = f"s{s}"
        if k in used:
            out.append({"key": k, "name": f"Part {s}", "color": ass_rgb(SINGER_FILL[s]), "color_name": SINGER_COLOR_NAMES[s],
                        "lines": used[k]})
    return out, together


def words_doc(job_dir, job, base):
    """The Name.words.json content for a job (times in video time)."""
    wp = os.path.join(job_dir, "words.json")
    lines = load_lines(wp)
    cast = load_cast(wp)
    off = float(job.get("offset") or 0.0)
    r2 = lambda t: round(float(t) + off, 2)
    parts, together = parts_of(lines, cast)
    out = []
    for i, l in enumerate(lines):
        ws = [[r2(a), r2(b), w] for a, b, w in l["words"]]
        x = {"i": i + 1, "start": min(w[0] for w in ws), "end": max(w[1] for w in ws), "words": ws, "parts": _line_parts(l)}
        if l.get("wwho"):
            lp = x["parts"]
            x["word_parts"] = [(_who_parts(w) or lp) for w in l["wwho"]]
        if l.get("orig"):
            x["orig"] = l["orig"]
        if l.get("backing"):
            x["backing"] = True
        out.append(x)
    together_color = ass_rgb(SINGER_FILL[3]) if any(p["key"].startswith("s") for p in parts) else ass_rgb(TOGETHER_FILL)
    return {"version": 1, "job": job.get("id"), "base": base, "duration": _duration(job), "parts": parts,
            "together": together, "together_color": together_color, "lines": out}


def _put_json(path, data):
    d, name = os.path.split(path)
    tmp = os.path.join(d, f".{name}.part")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def write_words(job_dir, job, mv_dir, base):
    """Write <base>.words.json next to the published song. Returns its file name."""
    _put_json(os.path.join(mv_dir, base + WORDS_EXT), words_doc(job_dir, job, base))
    return base + WORDS_EXT


def _duration(job):
    d = (job.get("result") or {}).get("duration")
    return round(float(d), 1) if d else None


# ---- entries ----
def _pitch(mv_dir, base):
    return base + PITCH_EXT if os.path.isfile(os.path.join(mv_dir, base + PITCH_EXT)) else None


def _vocals(mv_dir, base):
    return base + VOCALS_EXT if os.path.isfile(os.path.join(mv_dir, base + VOCALS_EXT)) else None


def add_plays(songs, rows):
    """Set each song's plays and last_played from play rows [(song id or None, base or file title, last, count)]: a
    row counts for the song whose id it names or one of whose files (the song's, a version's) it names."""
    by_base, ids = {}, {}
    for e in songs:
        ids[e["id"]] = e
        for b in [e["base"]] + [v["base"] for v in e.get("versions", [])]:
            by_base.setdefault(b, e)
    for e in songs:
        e["plays"], e["last_played"] = 0, None
    for sid, base, last, n in rows or []:
        e = by_base.get(base) or ids.get(sid)
        if e is None or not n:
            continue
        e["plays"] += int(n)
        if last is not None and (e["last_played"] is None or last > e["last_played"]):
            e["last_played"] = last


def _lyrics(mv_dir, base):
    modes = {m: f"{base}.{m}.ass" for m in MODES if os.path.isfile(os.path.join(mv_dir, f"{base}.{m}.ass"))}
    return {"file": f"{base}.ass" if os.path.isfile(os.path.join(mv_dir, f"{base}.ass")) else None, "modes": modes}


def entry_for(job_dir, job, lib_dir, mv_dir):
    """The catalog entry of a job, or None if it isn't published (or its video isn't in the library)."""
    base = job.get("published_as")
    if not base or not os.path.isfile(os.path.join(mv_dir, base + ".mp4")):
        return None
    lyr = _lyrics(mv_dir, base)
    want = job.get("lyrics_display")
    if lyr["modes"]:
        try:
            from assgen import style_settings
            want = want or style_settings().get("display") or "both"
        except Exception:
            want = want or "both"
        lyr["default_mode"] = want if want in lyr["modes"] else ("both" if "both" in lyr["modes"] else next(iter(lyr["modes"])))
    else:
        lyr["default_mode"] = None
    parts, together, n, first = [], False, 0, None
    wp = os.path.join(job_dir, "words.json")
    if os.path.exists(wp):
        lines = load_lines(wp)
        parts, together = parts_of(lines, load_cast(wp))
        n = len(lines)
        if lines:
            first = round(min(w[0] for w in lines[0]["words"]) + float(job.get("offset") or 0.0), 2)
    versions = []
    try:
        import videoswap
        for m in videoswap.entries(job_dir):
            pa = m.get("published_as")
            if m.get("mode") == "version" and pa and os.path.isfile(os.path.join(mv_dir, pa + ".mp4")):
                versions.append({"name": m.get("name") or m.get("slug"), "base": pa, "published": m.get("published"),
                                 "pitch": _pitch(mv_dir, pa), "vocals": _vocals(mv_dir, pa)})
    except Exception:
        pass
    title = job.get("title") or ""
    return {"id": job.get("id") or os.path.basename(job_dir), "artist": job.get("artist") or "", "title": title,
            "title_orig": job.get("title_orig") or (title if has_own_script(title) else None),   # (the song page's Original title)
            "lang": (job.get("result") or {}).get("lang") or job.get("lang"),
            "duration": _duration(job), "published": job.get("published"),
            "folder": os.path.relpath(mv_dir, lib_dir).replace(os.sep, "/"), "base": base, "lyrics": lyr,
            "versions": versions, "parts": parts, "together": together,
            "group": group_key(job.get("artist"), title),
            "words": base + WORDS_EXT if os.path.isfile(os.path.join(mv_dir, base + WORDS_EXT)) else None,
            "lines": n, "first": first, "pitch": _pitch(mv_dir, base), "plays": 0, "last_played": None,
            "vocals": _vocals(mv_dir, base)}


def build(jobs_dir, lib_dir, mv_dir, old=None, log=None, plays=None):
    """Every published song's entry. Songs from an older catalog whose job is gone but whose files are still in the
    library (a deleted job leaves its published song in PiKaraoke) are kept, marked job_deleted. A job that can't be
    read is left out (and logged). plays: play rows for add_plays (None: Encore's and PiKaraoke's, encore.play_rows)."""
    songs, ids, bases = [], set(), set()
    for jid in sorted(os.listdir(jobs_dir)) if os.path.isdir(jobs_dir) else []:
        d = os.path.join(jobs_dir, jid)
        if not _job_dir_ok(d):
            continue
        try:
            job = json.load(open(os.path.join(d, "job.json"), encoding="utf-8"))
            e = entry_for(d, job, lib_dir, mv_dir)
        except Exception:
            if log:
                log.exception("catalog: left out %s", jid)
            continue
        if e:
            songs.append(e)
            ids.add(e["id"])
            bases.add(e["base"])
            bases.update(v["base"] for v in e["versions"])
    old_by_id = {e.get("id"): e for e in (old or {}).get("songs", []) if isinstance(e, dict)}
    for e in songs:  # versions removed in the studio while their published copy stays
        o = old_by_id.get(e["id"])
        for v in (o or {}).get("versions", []):
            if v.get("base") not in bases and os.path.isfile(os.path.join(lib_dir, e["folder"], v["base"] + ".mp4")):
                vd = os.path.join(lib_dir, e["folder"])
                e["versions"].append(dict(v, pitch=_pitch(vd, v["base"]), vocals=_vocals(vd, v["base"])))
                bases.add(v["base"])
    for o in (old or {}).get("songs", []):
        if not isinstance(o, dict) or o.get("id") in ids or o.get("base") in bases:
            continue
        folder = os.path.join(lib_dir, o.get("folder") or "")
        if o.get("base") and os.path.isfile(os.path.join(folder, o["base"] + ".mp4")):
            o = dict(o, job_deleted=True, lyrics=dict(_lyrics(folder, o["base"]), default_mode=(o.get("lyrics") or {}).get("default_mode")),
                     versions=[dict(v, pitch=_pitch(folder, v["base"]), vocals=_vocals(folder, v["base"]))
                               for v in o.get("versions", []) if os.path.isfile(os.path.join(folder, v.get("base", "") + ".mp4"))],
                     pitch=_pitch(folder, o["base"]), vocals=_vocals(folder, o["base"]),
                     words=o["base"] + WORDS_EXT if os.path.isfile(os.path.join(folder, o["base"] + WORDS_EXT)) else None)
            if o["lyrics"]["default_mode"] not in o["lyrics"]["modes"]:
                o["lyrics"]["default_mode"] = next(iter(o["lyrics"]["modes"]), None)
            songs.append(o)
            bases.add(o["base"])
    songs.sort(key=lambda e: (norm_key(e["artist"]), norm_key(e["title"]), e["base"]))
    if plays is None:
        try:
            import encore
            plays = PLAYS() if PLAYS else encore.play_rows()
        except Exception:
            if log:
                log.exception("catalog: couldn't read the play counts")
            plays = []
    add_plays(songs, plays)
    return songs


def read_catalog(lib_dir):
    try:
        return json.load(open(os.path.join(lib_dir, CATALOG), encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_catalog(jobs_dir, lib_dir, mv_dir, log=None, plays=None, now=None):
    with _lock:
        songs = build(jobs_dir, lib_dir, mv_dir, read_catalog(lib_dir), log, plays)
        _put_json(os.path.join(lib_dir, CATALOG), {"version": VERSION, "generated": round(time.time() if now is None else now, 1),
                                                   "songs": songs})
        return len(songs)


def files_of(song):
    """Every library file a catalog entry names (paths relative to the library root, '/'-separated): the song's video,
    lyrics, words, pitch and vocals, and its versions' (named like the song's: <version base>.mp4, .ass, .<mode>.ass,
    .words.json; pitch and vocals when listed). Players may fetch only these (GET /api/encore/files/...)."""
    folder = (song.get("folder") or "").strip("/")
    pre = folder + "/" if folder else ""
    lyr = song.get("lyrics") or {}
    out = {pre + song["base"] + ".mp4"}
    for f in [lyr.get("file"), *(lyr.get("modes") or {}).values(), song.get("words"), song.get("pitch"), song.get("vocals")]:
        if f:
            out.add(pre + f)
    modes = list((lyr.get("modes") or {}).keys())
    for v in song.get("versions") or []:
        b = v.get("base")
        if not b:
            continue
        out.update(pre + b + ext for ext in [".mp4", ".ass", WORDS_EXT] + [f".{m}.ass" for m in modes])
        for k in ("pitch", "vocals"):
            if v.get(k):
                out.add(pre + v[k])
    return out


def refresh(jobs_dir, lib_dir, mv_dir, log=None, words=None):
    """Rewrite catalog.json (and first, if given, words files: [(job_dir, job, base), ...]). Never raises."""
    ok = True
    for job_dir, job, base in words or []:
        try:
            write_words(job_dir, job, mv_dir, base)
        except Exception:
            ok = False
            if log:
                log.exception("catalog: couldn't write the words file of %s", base)
    try:
        n = write_catalog(jobs_dir, lib_dir, mv_dir, log)
        if log:
            log.info("catalog: %d songs in %s", n, CATALOG)
    except Exception:
        ok = False
        if log:
            log.exception("catalog: couldn't write %s", CATALOG)
    return ok
