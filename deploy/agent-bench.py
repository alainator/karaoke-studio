#!/usr/bin/env python3
"""The agent benchmark: how close does a fixing agent get to the person's own hand fixes?

Songs a person fixed by hand are copied into a sandbox and put back to the Studio's automatic version (nothing of
the person's fix is left in the copy); an agent fixes them through ks against a throwaway Studio running this code;
then each song is compared line by line with the person's version.

    python3 deploy/agent-bench.py prepare OUT --pick 10 [--seed 1] [--jobs DIR]
    python3 deploy/agent-bench.py prepare OUT --songs JOB1,JOB2,...
    python3 deploy/agent-bench.py start OUT [--port 5680] [--cache DIR] [--image karaoke-studio:local] [--no-gpu]
    #   the fixing agent gets exactly two variables (and no access to OUT itself):
    #   STUDIO_URL=http://127.0.0.1:5680 STUDIO_AGENT_KEY_FILE=OUT/agent.key ks list --fix
    python3 deploy/agent-bench.py score OUT [--json]
    python3 deploy/agent-bench.py stop OUT [--port P]

prepare: --jobs is the Studio's jobs folder (default: KARAOKE_JOBS, as the Studio's config reads it). It is only
read: every file is copied (never linked), so nothing done in the sandbox can reach it. OUT/jobs/<id>/ gets the
song's sound, video, what was heard, its pictures' sources and the automatic lyrics (words.orig.json, marks
included, notes and "fine" ticks dropped) as words.json; job.json keeps only the Studio's own facts about the
making (no edit history, last edit, "checked by you", offset, notes, names, remake state; the timing offset back to
0). Subtitles (rendered from the person's timing) and the by-ear numbers ("heard in place", which a re-check may have
worked out on the person's version) are left out and worked out again from the automatic lyrics when the sandbox
starts. The person's version goes to OUT/truth/<id>.json, outside the sandbox's jobs. A leak check runs at
the end: no file in the sandbox may hold any of the person's word timings, notes or names.

--pick N: songs a person (not ks or an agent, in their last edit or anywhere in their edit history) edited, that
keep the same number of lead lines, weren't remade, trimmed or given another video; about 80% with lines the person
moved (more than 0.25 s), mixed languages, and about 20% the person checked or edited without moving any line ("do
no harm" songs).

start: a container from the Studio's image running THIS folder's code on 127.0.0.1:PORT, with OUT/jobs as its jobs,
OUT/data as its data (fresh: no players, no keys but the benchmark's), an empty OUT/library (nothing can be
published there anyway: the key can't), no GPU helper, no Ollama, no album lookups, and API calls needing a key. Its
GPU is the machine's: start the sandbox (and run an agent's ks align / look --align) only while the real Studio
isn't making songs, since the sandbox can't see that. The key allows read, edit and notes only (not clear_marks,
publish, remake, delete or make); it's written to OUT/agent.key (mode 600) and never printed. --allow-ok also allows
clear_marks (ks ok). The container is labelled with OUT and recorded in OUT/sandbox.json; a port whose container
belongs to another OUT is refused.

stop: removes OUT's own container (the one start recorded, or any labelled with OUT; with --port only that port's,
and only if it is OUT's). Never another sandbox's.

score: per song and in total, against the person's version. Lead lines are matched by their words near their place
(match(): same words in order, the same words a few lines away, the most alike text nearby, then the rest in order
between matched lines), so lines the agent added, deleted, split or joined are counted as such and the rest of the song
is still scored:
  moved lines (the person moved the start more than 0.25 s): fixed (now within 0.3 s of the person's start),
  improved (closer by more than 0.1 s), made worse (further by more than 0.1 s), untouched, changed (no closer),
  deleted (still counted as moved lines: deleting one isn't fixing it);
  kept lines (the person left them within 0.05 s): moved off (now more than 0.3 s from the person's start), by
  direction (earlier / later) and size (0.3-1, 1-2, over 2 s), and of those "probably good": moved EARLIER by 0.3-2 s
  with the same words as a line the person moved earlier by about as much (a chorus line the person fixed once and the
  agent in every repeat); deleted; each listed (song, the person's line number and start, how far, ⚠, whether the
  person moved a neighbour, repeat of a person's fix) under the table and in --json (kept_lines; moved_lines too);
  lines the agent added (or split off) and deleted (or joined);
  the boundary: for each moved line, the line before's end (people cut it in most late-line fixes): how often the
  person changed it, how often it's now within 0.3 s of theirs, and ends the person kept that are now off;
  lines whose words the agent changed (and how many match the person's words); the median start error before
  (Studio) and after (agent), over all lines and over moved lines. Times are video times (start + the song's offset).
"""
import argparse, json, os, random, re, shutil, statistics, subprocess, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOVED, KEPT = 0.25, 0.05      # the measurements' line classes: |person - studio| start
FIXED, BETTER = 0.3, 0.1      # fixed: within 0.3 s of the person's start; improved / worse: by more than 0.1 s
PORT = 5680
NAME = "ks-agent-bench"
OUT_LABEL = NAME + ".out"     # each sandbox container is labelled with its OUT, so stop finds exactly its own
SANDBOX = "sandbox.json"      # in OUT: the container (and port) start made, for stop

# what a sandbox copy may hold: the song's sound and pictures' sources, the automatic lyrics, and these
COPY = ("vocals.m4a", "out.mp4", "heard.json", "peaks.json", "onsets.json", "spectro.webp", "spectro.png",
        "pitch.f0.npz", "cover.jpg", "pipeline.log", "words.orig.json", "result.json")
JOB_KEYS = ("id", "yt", "lrc_id", "artist", "title", "title_orig", "video_title", "status", "step", "created", "waiting",
            "started", "finished", "log", "result", "lyrics_source", "lyrics_display", "gpu", "gpu_pref", "lang",
            "keep_backing", "album_meta")
LINE_DROP = ("note", "ok")    # a person's (or agent's) view of a line, never the Studio's
NOT_PEOPLE = {"studio", "web", "ks", "automake", "import", "auto", "agent-bench"}   # signatures that aren't a person


# ---------------- lines ----------------
def load_doc(path):
    d = json.load(open(path, encoding="utf-8"))
    return d if isinstance(d, dict) else {"version": 1, "lines": [{"words": l} for l in d]}


def words_of(l):
    """A line's words as the Studio saves them (it drops words with no text and trims the rest: a save mustn't count
    as a change)."""
    return [w for w in (l.get("words") or []) if str(w[2]).strip()]


def lead(lines):
    return [l for l in lines if isinstance(l, dict) and not l.get("backing") and words_of(l)]


def start(l):
    return words_of(l)[0][0]


def end(l):
    return words_of(l)[-1][1]


def starts(lines, offset):
    return [start(l) + offset for l in lead(lines)]


def text(l):
    return " ".join(str(w[2]).strip() for w in words_of(l))


# ---------------- prepare ----------------
def history_kinds(src):
    """Who made each edit in the song's history (undone and retired ones too)."""
    out = []
    h = os.path.join(src, "history")
    for root, _, files in os.walk(h):
        for f in files:
            if f.endswith(".json"):
                try:
                    out.append(json.load(open(os.path.join(root, f), encoding="utf-8")).get("kind"))
                except (OSError, ValueError):
                    out.append("?")
    return out


def versions(src, job):
    """(the Studio's automatic lyrics, the person's) of a job folder. A song never edited has no words.orig.json: it
    can still be a "do no harm" song if a person marked it checked (both versions are then its words.json)."""
    person = load_doc(os.path.join(src, "words.json"))
    orig = os.path.join(src, "words.orig.json")
    if os.path.exists(orig):
        return load_doc(orig), person
    if job.get("reviewed") and not job.get("edited") and not history_kinds(src):
        return person, person
    raise ValueError("no automatic version (words.orig.json) and not checked by a person")


def candidate(src):
    """A song --pick may use: {"id", "lang", "moved", "lines", "checked"} or None (with no reason: it's just skipped)."""
    try:
        job = json.load(open(os.path.join(src, "job.json"), encoding="utf-8"))
        auto, person = versions(src, job)
        auto, person = auto["lines"], person["lines"]
    except (OSError, ValueError, KeyError):
        return None
    if job.get("status") != "done" or not os.path.exists(os.path.join(src, "vocals.m4a")):
        return None
    if (job.get("last_edit") or {}).get("kind") in ("ks", "agent") or any(k in ("ks", "agent", "?") for k in history_kinds(src)):
        return None
    if job.get("remade") or job.get("pre_remake") or job.get("trim") or job.get("video_v") or any(
            os.path.exists(os.path.join(src, x)) for x in ("words.before-remake.json", "words.before-remake.auto.json",
                                                         "untrimmed", "videos", "versions")):
        return None
    a, p = starts(auto, 0.0), starts(person, float(job.get("offset") or 0.0))
    if not a or len(a) != len(p):
        return None
    moved = sum(1 for x, y in zip(a, p) if abs(y - x) > MOVED)
    edited = bool(job.get("edited") or job.get("last_edit") or history_kinds(src)) or \
        [text(l) for l in lead(auto)] != [text(l) for l in lead(person)] or moved
    if not edited and not job.get("reviewed"):
        return None
    return {"id": os.path.basename(src), "lang": (job.get("result") or {}).get("lang") or job.get("lang") or "?",
            "moved": moved, "lines": len(a), "checked": bool(job.get("reviewed"))}


def pick(jobs_dir, n, seed=1):
    """N songs: ~80% with moved lines (languages taken in turn), ~20% "do no harm" (checked/edited, nothing moved)."""
    cands = [c for c in (candidate(os.path.join(jobs_dir, d)) for d in sorted(os.listdir(jobs_dir))
                         if os.path.isdir(os.path.join(jobs_dir, d))) if c]
    rng = random.Random(seed)
    calm = [c for c in cands if not c["moved"]]
    moved = [c for c in cands if c["moved"]]
    n_calm = min(len(calm), max(1 if n >= 3 else 0, round(n * 0.2)))
    by_lang = {}
    for c in moved:
        by_lang.setdefault(c["lang"], []).append(c)
    for v in by_lang.values():
        rng.shuffle(v)
    out, langs = [], sorted(by_lang, key=lambda k: (-len(by_lang[k]), k))
    while len(out) < n - n_calm and any(by_lang.values()):
        for k in langs:
            if by_lang[k] and len(out) < n - n_calm:
                out.append(by_lang[k].pop())
    rng.shuffle(calm)
    calm.sort(key=lambda c: not c["checked"])   # songs a person marked "checked" first
    return out + calm[:n - len(out)], {"candidates": len(cands), "with_moves": len(moved), "no_moves": len(calm)}


def clean_lines(lines):
    out = []
    for l in lines:
        l = dict(l) if isinstance(l, dict) else {"words": l}
        for k in LINE_DROP:
            l.pop(k, None)
        out.append(l)
    return out


def reset_copy(src, dst):
    """Copy one job into the sandbox as the Studio made it (see the module's doc). Returns the truth record."""
    jid = os.path.basename(src.rstrip("/"))
    job = json.load(open(os.path.join(src, "job.json"), encoding="utf-8"))
    auto, person = versions(src, job)
    os.makedirs(dst, exist_ok=True)
    for f in COPY:
        if f in ("words.orig.json", "result.json"):
            continue
        p = os.path.join(src, f)
        if os.path.isfile(p) and not os.path.islink(p):
            shutil.copy2(p, os.path.join(dst, f))   # a real copy; mtimes kept (pitch.f0.npz is stamped with vocals')
    doc = {k: v for k, v in auto.items() if k != "lines"}
    doc["lines"] = clean_lines(auto["lines"])
    for f in ("words.json", "words.orig.json"):
        with open(os.path.join(dst, f), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
    new = {k: job[k] for k in JOB_KEYS if k in job}
    new.update(offset=0.0, published=False, published_as=None, unpublished_changes=False, edited=None, reviewed=None)
    if isinstance(new.get("result"), dict):
        new["result"] = {k: v for k, v in new["result"].items() if k != "confidence"}
    if isinstance(new.get("album_meta"), dict):
        new["album_meta"] = {k: v for k, v in new["album_meta"].items() if k != "by"}
    with open(os.path.join(dst, "job.json"), "w", encoding="utf-8") as fh:
        json.dump(new, fh, ensure_ascii=False, indent=1)
    if os.path.exists(os.path.join(src, "result.json")):
        try:
            r = json.load(open(os.path.join(src, "result.json"), encoding="utf-8"))
            r.pop("confidence", None)
            with open(os.path.join(dst, "result.json"), "w", encoding="utf-8") as fh:
                json.dump(r, fh)
        except (OSError, ValueError):
            pass
    return {"id": jid, "artist": job.get("artist"), "title": job.get("title"),
            "lang": (job.get("result") or {}).get("lang") or job.get("lang"),
            "auto": {"lines": auto["lines"], "offset": 0.0},
            "person": {"lines": person["lines"], "offset": float(job.get("offset") or 0.0)},
            "names": sorted({x for x in [(job.get("last_edit") or {}).get("by"), job.get("made_by"), job.get("published_by")]
                             if isinstance(x, str) and x.strip() and x.strip().lower() not in NOT_PEOPLE})}


def _triples(lines):
    return {(round(float(w[0]), 3), round(float(w[1]), 3), str(w[2])) for l in lines for w in (l.get("words") or [])
            if isinstance(l, dict)}


def _all_triples(obj, out):
    """Every [start, end, "word"] anywhere in a JSON value."""
    if isinstance(obj, list):
        if len(obj) == 3 and all(isinstance(x, (int, float)) for x in obj[:2]) and isinstance(obj[2], str):
            out.add((round(float(obj[0]), 3), round(float(obj[1]), 3), obj[2]))
        else:
            for x in obj:
                _all_triples(x, out)
    elif isinstance(obj, dict):
        for x in obj.values():
            _all_triples(x, out)
    return out


def leaks(dst, truth):
    """What of the person's fix is still in a sandbox copy: [why] (empty = clean). Checks every file: names outside
    the allowed set, the person's own word timings (any word the person moved or retimed) in any JSON file, the
    person's notes, names and timing offset, and edit-history keys in job.json."""
    out = []
    allowed = set(COPY) | {"words.json", "job.json"}
    auto_t = _triples(truth["auto"]["lines"])
    poff = truth["person"]["offset"]
    mine = {(round(a + poff, 3), round(b + poff, 3), w) for a, b, w in _triples(truth["person"]["lines"])} - auto_t
    mine |= _triples(truth["person"]["lines"]) - auto_t
    notes = [l["note"].get("text") for l in truth["person"]["lines"] if isinstance(l, dict) and isinstance(l.get("note"), dict)
             and l["note"].get("text")]
    needles = [x for x in truth.get("names", []) + notes if x and len(x) >= 3]
    for root, dirs, files in os.walk(dst):
        for d in dirs:
            out.append(f"a folder: {os.path.relpath(os.path.join(root, d), dst)}")
        for f in files:
            p = os.path.join(root, f)
            rel = os.path.relpath(p, dst)
            if rel not in allowed:
                out.append(f"a file that isn't the Studio's own: {rel}")
            if f.endswith((".json", ".log", ".ass", ".txt")):
                raw = open(p, encoding="utf-8", errors="replace").read()
                for x in needles:
                    if x in raw:
                        out.append(f"{rel}: holds a person's name or note")
                if f.endswith(".json"):
                    try:
                        found = _all_triples(json.loads(raw), set()) & mine
                    except ValueError:
                        found = set()
                    if found:
                        out.append(f"{rel}: holds {len(found)} of the person's word timings")
    try:
        job = json.load(open(os.path.join(dst, "job.json"), encoding="utf-8"))
        extra = set(job) - set(JOB_KEYS) - {"offset", "published", "published_as", "unpublished_changes", "edited", "reviewed"}
        if extra:
            out.append(f"job.json keeps: {', '.join(sorted(extra))}")
        if job.get("offset") or job.get("edited") or job.get("reviewed") or job.get("last_edit"):
            out.append("job.json: offset, edited, reviewed or last edit set")
    except (OSError, ValueError):
        out.append("job.json missing")
    return out


def cmd_prepare(a):
    jobs = os.path.abspath(a.jobs)
    out = os.path.abspath(a.out)
    if os.path.commonpath([jobs, out]) == jobs:
        sys.exit("OUT can't be inside the jobs folder")
    if os.path.exists(os.path.join(out, "jobs")) and os.listdir(os.path.join(out, "jobs")):
        sys.exit(f"{out}/jobs isn't empty: use a new OUT")
    if a.songs:
        ids = [x.strip() for x in a.songs.split(",") if x.strip()]
        info = None
    else:
        chosen, info = pick(jobs, a.pick, a.seed)
        ids = [c["id"] for c in chosen]
    if not ids:
        sys.exit("no songs")
    for d in ("jobs", "truth", "data", "library"):
        os.makedirs(os.path.join(out, d), exist_ok=True)
    rows, bad = [], 0
    for jid in ids:
        src = os.path.join(jobs, jid)
        if not re.fullmatch(r"[\w.-]+", jid) or not os.path.isfile(os.path.join(src, "job.json")):
            print(f"skipped {jid}: no such song")
            continue
        try:
            truth = reset_copy(src, os.path.join(out, "jobs", jid))
        except (OSError, ValueError, KeyError) as e:
            print(f"skipped {jid}: {e}")
            continue
        with open(os.path.join(out, "truth", jid + ".json"), "w", encoding="utf-8") as fh:
            json.dump(truth, fh, ensure_ascii=False)
        found = leaks(os.path.join(out, "jobs", jid), truth)
        bad += bool(found)
        a_, p_ = starts(truth["auto"]["lines"], 0.0), starts(truth["person"]["lines"], truth["person"]["offset"])
        mv = sum(1 for x, y in zip(a_, p_) if abs(y - x) > MOVED) if len(a_) == len(p_) else None
        rows.append({"id": jid, "lang": truth["lang"], "lines": len(a_), "moved": mv})
        print(f"  {jid}  {truth['lang'] or '?':3} {len(a_):3} lines, {mv if mv is not None else '?'} moved by the person"
              + ("; LEAK: " + "; ".join(found) if found else ""))
    with open(os.path.join(out, "bench.json"), "w") as fh:
        json.dump({"made": time.time(), "songs": rows, "pick": info}, fh, indent=1)
    if info:
        print(f"(picked from {info['candidates']} songs a person edited: {info['with_moves']} with moved lines, "
              f"{info['no_moves']} without)")
    print(f"{len(rows)} songs in {out}/jobs; the person's versions in {out}/truth (keep the agent away from it); "
          f"leak check: {'CLEAN' if not bad else f'{bad} songs LEAK'}")
    if bad:
        sys.exit(1)


# ---------------- start / stop (docker) ----------------
def container(out, port):
    return f"{NAME}-{port}"


def cmd_start(a):
    out = os.path.abspath(a.out)
    if not os.path.isdir(os.path.join(out, "jobs")):
        sys.exit("prepare OUT first")
    name = container(out, a.port)
    other = subprocess.run(["docker", "inspect", "-f", "{{index .Config.Labels \"%s\"}}" % OUT_LABEL, name],
                           capture_output=True, text=True)
    if other.returncode == 0 and other.stdout.strip() not in ("", out):
        sys.exit(f"container {name} (port {a.port}) belongs to another sandbox ({other.stdout.strip()}): "
                 f"stop that one or use another --port")
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    cache = os.path.abspath(a.cache) if a.cache else os.path.join(out, "cache")
    os.makedirs(cache, exist_ok=True)
    for d in ("data/logs", "library", "work", "models"):
        os.makedirs(os.path.join(out, d), exist_ok=True)
    env = {"KARAOKE_JOBS": f"{out}/jobs", "KARAOKE_DATA": f"{out}/data", "KARAOKE_LOGS": f"{out}/data/logs",
           "KARAOKE_LIBRARY": f"{out}/library", "KARAOKE_WORK": f"{out}/work", "KARAOKE_MODELS": f"{out}/models",
           "KARAOKE_FONTS": f"{out}/data/fonts", "KARAOKE_LYRICS_FONT": "classic", "XDG_CACHE_HOME": cache,
           "GPU_HELPER_URL": "", "HELPER_TOKEN": "", "OLLAMA_URL": "", "ALBUM_LOOKUP": "0", "STUDIO_ORIGINS": "",
           "STUDIO_PUBLIC_URL": "", "STUDIO_NAME": "Agent benchmark", "ENCORE_TOKEN": "", "ENCORE_ONLY_NETS": "",
           "AGENT_KEYS_REQUIRED": "1", "USE_LOCAL_GPU": "0" if a.no_gpu else "1", "KARAOKE_WORDS_SCORE": "0",
           "PIKARAOKE_URL": "", "PIKARAOKE_DB": "", "NODE_BIN": ""}
    cmd = ["docker", "run", "-d", "--name", name, "--label", f"{NAME}=1", "--label", f"{OUT_LABEL}={out}", "--user", f"{os.getuid()}:{os.getgid()}",
           "-p", f"127.0.0.1:{a.port}:{a.port}", "-w", "/studio",
           "-v", f"{HERE}:/studio:ro",
           "-v", f"{out}:{out}", "-v", f"{cache}:{cache}" + (":ro" if a.cache else "")]
    if os.path.exists(os.path.join(HERE, ".env")):   # this code, never the folder's settings (they name real folders)
        cmd += ["-v", "/dev/null:/studio/.env:ro"]
    if not a.no_gpu:
        cmd += ["--gpus", "all"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    inside = (f"python -I /studio/deploy/agent-bench.py _inside {out}{' --allow-ok' if a.allow_ok else ''} && "
              f"exec python -I /studio/app.py --host 0.0.0.0 --port {a.port}")
    cmd += ["--entrypoint", "sh", a.image, "-c", inside]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit("docker run failed: " + r.stderr.strip()[-400:])
    url = f"http://127.0.0.1:{a.port}"
    with open(os.path.join(out, SANDBOX), "w") as fh:   # what stop removes (and nothing else)
        json.dump({"container": name, "port": a.port, "url": url, "started": time.time()}, fh)
    for _ in range(90):
        try:
            import urllib.request
            urllib.request.urlopen(url + "/api/status", timeout=2)
        except Exception as e:
            if getattr(e, "code", None) == 401:   # up (and asking for a key, as it should)
                break
            st = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name], capture_output=True, text=True).stdout.strip()
            if st != "true":
                logs = subprocess.run(["docker", "logs", "--tail", "30", name], capture_output=True, text=True)
                sys.exit("the sandbox stopped:\n" + (logs.stdout + logs.stderr)[-2000:])
            time.sleep(1)
            continue
        break
    key = os.path.join(out, "agent.key")
    print(f"sandbox Studio: {url} (container {name}); agent key in {key} (read, edit, notes"
          f"{', clear_marks' if a.allow_ok else ''}; never printed)")
    print("give the fixing agent exactly these two variables (and no access to OUT/truth):")
    print(f"  export STUDIO_URL={url}")
    print(f"  export STUDIO_AGENT_KEY_FILE={key}")
    print(f"then: python3 deploy/agent-bench.py score {a.out}; python3 deploy/agent-bench.py stop {a.out}")


def cmd_inside(a):
    """In the sandbox container, before the Studio starts: the agent key (once) and the subtitles from the reset lyrics."""
    sys.path.insert(0, HERE)
    out = os.path.abspath(a.out)
    key = os.path.join(out, "agent.key")
    if not os.path.exists(key):
        import agents
        scopes = ["read", "edit", "notes"] + (["clear_marks"] if a.allow_ok else [])
        k, _row = agents.KeyStore().create(f"bench agent {int(time.time()) % 100000}", scopes, by="agent-bench")
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(k + "\n")
    from assgen import write_lyrics
    import earcheck
    jobs = os.path.join(out, "jobs")
    for jid in sorted(os.listdir(jobs)):
        d = os.path.join(jobs, jid)
        jp = os.path.join(d, "job.json")
        job = json.load(open(jp, encoding="utf-8"))
        if isinstance(job.get("result"), dict) and "confidence" not in job["result"]:
            # "heard in place" of the automatic lines (the copy's was dropped: it may have been worked out on the
            # person's version); the marks stay as the Studio made them (save=False)
            try:
                conf = earcheck.check_job(d, save=False)
            except Exception as e:
                conf = None
                print(f"agent-bench: by-ear numbers for {jid}: {str(e)[:200]}", file=sys.stderr)
            if conf:
                job["result"]["confidence"] = conf
                with open(jp, "w", encoding="utf-8") as fh:
                    json.dump(job, fh, ensure_ascii=False, indent=1)
                rp = os.path.join(d, "result.json")
                if os.path.exists(rp):
                    r = json.load(open(rp, encoding="utf-8")); r["confidence"] = conf
                    with open(rp, "w", encoding="utf-8") as fh:
                        json.dump(r, fh)
        if not os.path.exists(os.path.join(d, "lyrics.ass")):
            try:
                write_lyrics(d, json.load(open(os.path.join(d, "job.json"), encoding="utf-8")))
            except Exception as e:
                print(f"agent-bench: lyrics for {jid}: {str(e)[:200]}", file=sys.stderr)


def stop_targets(out, port=None):
    """The containers stop removes for OUT: the one start recorded in OUT (or, with --port, that port's, but only if
    it was started for OUT), plus any other container labelled with OUT. Never a guess: a port's container started
    for another OUT is left alone."""
    names = []
    rec = os.path.join(out, SANDBOX)
    if port is not None:
        names.append(container(out, port))
    elif os.path.exists(rec):
        try:
            names.append(json.load(open(rec))["container"])
        except (OSError, ValueError, KeyError):
            pass
    r = subprocess.run(["docker", "ps", "-a", "--filter", f"label={OUT_LABEL}={out}", "--format", "{{.Names}}"],
                       capture_output=True, text=True)
    labelled = set(r.stdout.split()) if r.returncode == 0 else set()
    keep = []
    for n in names + sorted(labelled - set(names)):
        if n in labelled and n not in keep:
            keep.append(n)
        elif n not in labelled and n not in keep:
            print(f"{n}: not this sandbox's container (or not running): left alone")
    return keep


def cmd_stop(a):
    out = os.path.abspath(a.out)
    names = stop_targets(out, a.port)
    if not names:
        print(f"no sandbox container for {out}")
    for name in names:
        r = subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True)
        print(f"stopped {name}" if r.returncode == 0 else f"couldn't stop {name}: {r.stderr.strip()[-200:]}")
    rec = os.path.join(out, SANDBOX)
    try:
        if json.load(open(rec)).get("container") in names:
            os.remove(rec)
    except (OSError, ValueError):
        pass


# ---------------- score ----------------
NEAR = 3          # a line is looked for this many places either side (saving keeps lines in time order: a moved line can swap)
SIMILAR = 0.5     # retyped, split or joined lines: text this alike (0-1, plus 0.2 for the same first word) still matches
SIZES = ((0.3, 1.0), (1.0, 2.0), (2.0, None))   # how far kept lines were moved (s)
REPEAT = (0.3, 2.0)   # a kept line moved EARLIER by this much (s) ...
REPEAT_TOL = 0.5      # ... within this (or half the person's move, if more) of how far the person moved the same words earlier
REPEAT_SIM = 1.0      # "the same words": similar() at least this (the same first word and 80% alike: a repeat sung a bit differently)


def norm(t):
    """A line's words for matching: lower case, letters and digits only."""
    return " ".join(re.findall(r"\w+", (t or "").lower()))


def similar(a, b):
    import difflib
    if not a or not b:
        return 0.0
    first = a.split()[0] == b.split()[0]
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() + (0.2 if first else 0.0)


def match(A, X, oa=0.0, ox=0.0):
    """For each line A[i], its line in X, or None when it has none (deleted, or joined into a neighbour); X's lines
    no A line matched were added (or split off). Counts may differ. In turn: runs of lines with the same words in the
    same order; a line with the same words nearby (saving keeps lines in time order, so a moved line can pass a
    neighbour); the most alike line nearby (retyped words; of a split line the part with its first word, which carries
    its start; of a joined pair the first); then, between two matched lines, the rest in order when as many are left
    on both sides. Last, lines with the same words (a chorus) go to the copy nearest in time (oa, ox: the offsets,
    for video time): deleting or adding one copy mustn't pair the others with the wrong one."""
    import difflib
    ta, tx = [norm(text(l)) for l in A], [norm(text(l)) for l in X]
    m, used = [None] * len(A), set()

    def take(i, j):
        m[i] = j; used.add(j)

    for blk in difflib.SequenceMatcher(None, ta, tx, autojunk=False).get_matching_blocks():
        for k in range(blk.size):
            take(blk.a + k, blk.b + k)

    def expect(i):   # where A[i] should be in X: after the nearest matched line before it
        p = next((k for k in range(i - 1, -1, -1) if m[k] is not None), None)
        return i if p is None else m[p] + (i - p)

    def near(i):
        e = expect(i)
        return sorted((j for j in range(max(0, e - NEAR), min(len(X), e + NEAR + 1)) if j not in used),
                      key=lambda j: (abs(j - e), j))

    for i in range(len(A)):
        if m[i] is None:
            j = next((j for j in near(i) if tx[j] == ta[i]), None)
            if j is not None:
                take(i, j)
    for i in range(len(A)):
        if m[i] is None:
            best = max(near(i), key=lambda j: similar(ta[i], tx[j]), default=None)
            if best is not None and similar(ta[i], tx[best]) >= SIMILAR:
                take(i, best)
    i = 0
    while i < len(A):   # gaps between matched lines with as many left on both sides: in order (words rewritten)
        if m[i] is not None:
            i += 1
            continue
        k = i
        while k < len(A) and m[k] is None:
            k += 1
        lo = m[i - 1] + 1 if i else 0
        hi = m[k] if k < len(A) else len(X)
        free = [j for j in range(lo, hi) if j not in used]
        if hi >= lo and len(free) == k - i:
            for n, j in enumerate(free):
                take(i + n, j)
        i = k
    t_a = lambda i: start(A[i]) + oa
    t_x = lambda j: start(X[j]) + ox
    for _ in range(2):
        for u in range(len(A)):   # an A line left over, its twin nearby matched to a copy nearer to u's time: u's
            if m[u] is not None:
                continue
            for i in range(max(0, u - NEAR), min(len(A), u + NEAR + 1)):
                if m[i] is not None and ta[i] == ta[u] and abs(t_x(m[i]) - t_a(u)) < abs(t_x(m[i]) - t_a(i)):
                    m[u], m[i] = m[i], None
                    break
        for i in range(len(A)):   # a matched line, an unmatched copy of its words nearer to its time: that one
            if m[i] is None:
                continue
            for j in range(max(0, m[i] - NEAR), min(len(X), m[i] + NEAR + 1)):
                if j not in used and tx[j] == ta[i] and abs(t_x(j) - t_a(i)) < abs(t_x(m[i]) - t_a(i)):
                    used.discard(m[i]); used.add(j); m[i] = j
    return m


def size_of(d):
    d = abs(d)
    return next((f"{a:g}-{b:g} s" if b else f">{a:g} s") for a, b in SIZES if d >= a and (b is None or d < b))


COUNTS = ("lines", "moved", "fixed", "improved", "worse", "untouched", "changed", "moved_deleted", "kept", "kept_broken",
          "kept_earlier", "kept_later", "kept_repeat_fix", "kept_deleted", "kept_touched", "small", "added", "deleted",
          "person_gone", "words_changed", "words_match", "person_words", "person_words_right", "prev_end", "prev_end_cut",
          "prev_end_fixed", "prev_end_broken") + tuple(f"kept_{size_of(a)}" for a, _ in SIZES)


def score_song(truth, now_doc, now_offset):
    """Compare the sandbox's lines now with the person's (truth) and the Studio's. Returns a dict of counts, plus
    "moved_lines" and "kept_lines" (each line the person moved; each kept line the agent moved off or deleted)."""
    A, P, N = lead(truth["auto"]["lines"]), lead(truth["person"]["lines"]), lead(now_doc["lines"])
    ao, po = truth["auto"]["offset"], truth["person"]["offset"]
    r = {"id": truth["id"], "lang": truth.get("lang"), "lines": len(P)}
    c = dict.fromkeys(COUNTS, 0)
    c["lines"] = len(P)
    mp, mn = match(A, P, ao, po), match(A, N, ao, now_offset)
    pnum = {id(l): k + 1 for k, l in enumerate(truth["person"]["lines"])}   # line numbers as the person's version has them
    anum = {id(l): k + 1 for k, l in enumerate(truth["auto"]["lines"])}
    c["added"] = len(N) - sum(1 for j in mn if j is not None)
    c["deleted"] = sum(1 for j in mn if j is None)
    pdelta = [(start(P[mp[i]]) + po) - (start(la) + ao) if mp[i] is not None else None for i, la in enumerate(A)]
    moved_at = {i for i, d in enumerate(pdelta) if d is not None and abs(d) > MOVED}
    fixes = [(norm(text(A[i])), pdelta[i]) for i in sorted(moved_at) if pdelta[i] < 0]   # the person's earlier moves
    err_before, err_after, mv_before, mv_after, moved_lines, kept_lines = [], [], [], [], [], []
    clock = lambda t: f"{int(t // 60)}:{t % 60:05.2f}"
    for i, la in enumerate(A):
        if mp[i] is None:
            c["person_gone"] += 1   # the person deleted (or joined) it: nothing to compare with
            continue
        lp = P[mp[i]]
        ln = N[mn[i]] if mn[i] is not None else None
        a, p = start(la) + ao, start(lp) + po
        row = {"song": truth["id"], "line": pnum.get(id(lp)), "auto_line": anum.get(id(la)), "at": clock(p),
               "weak": bool(la.get("weak")), "person_delta": round(p - a, 2)}
        if text(lp) != text(la):
            c["person_words"] += 1
        if ln is None:
            if i in moved_at:
                c["moved"] += 1; c["moved_deleted"] += 1
                moved_lines.append(dict(row, agent_delta=None, outcome="deleted"))
            elif abs(p - a) < KEPT:
                c["kept"] += 1; c["kept_deleted"] += 1
                kept_lines.append(dict(row, delta=None, direction="deleted", size=None, neighbour_moved=bool(
                    {i - 1, i + 1} & moved_at), repeat_of_fix=False))
            else:
                c["small"] += 1
            continue
        n = start(ln) + now_offset
        ea, en, touched = abs(a - p), abs(n - p), abs(n - a) > 0.01
        err_before.append(ea); err_after.append(en)
        if i in moved_at:
            c["moved"] += 1
            mv_before.append(ea); mv_after.append(en)
            if i and mp[i - 1] is not None and mn[i - 1] is not None:   # the boundary: the line before's end
                pa, pp, pn = end(A[i - 1]) + ao, end(P[mp[i - 1]]) + po, end(N[mn[i - 1]]) + now_offset
                c["prev_end"] += 1
                if abs(pp - pa) > MOVED:
                    c["prev_end_cut"] += 1
                    c["prev_end_fixed"] += abs(pn - pp) <= FIXED
                else:
                    c["prev_end_broken"] += abs(pn - pp) > FIXED
            out = ("untouched" if not touched else "fixed" if en <= FIXED else "improved" if en < ea - BETTER
                   else "worse" if en > ea + BETTER else "changed")
            c[out] += 1
            moved_lines.append(dict(row, agent_delta=round(n - a, 2), outcome=out))
        elif abs(p - a) < KEPT:
            c["kept"] += 1
            if en > FIXED:
                d = n - p
                t_ = norm(text(la))   # a repeat of a line the person moved earlier by about as much: the same fix
                rep = d < 0 and REPEAT[0] <= -d <= REPEAT[1] and any(
                    (ft == t_ or similar(ft, t_) >= REPEAT_SIM) and abs(d - f) <= max(REPEAT_TOL, abs(f) / 2)
                    for ft, f in fixes)
                c["kept_broken"] += 1
                c["kept_earlier" if d < 0 else "kept_later"] += 1
                c[f"kept_{size_of(d)}"] += 1
                c["kept_repeat_fix"] += rep
                kept_lines.append(dict(row, delta=round(d, 2), direction="earlier" if d < 0 else "later", size=size_of(d),
                                       neighbour_moved=bool({i - 1, i + 1} & moved_at), repeat_of_fix=rep))
            elif touched:
                c["kept_touched"] += 1
        else:
            c["small"] += 1
        if text(ln) != text(la):
            c["words_changed"] += 1
            c["words_match"] += text(ln) == text(lp)
        if text(lp) != text(la):
            c["person_words_right"] += text(ln) == text(lp)
    med = lambda xs: round(statistics.median(xs), 2) if xs else None
    return dict(r, scored=True, **c, median_before=med(err_before), median_after=med(err_after),
                moved_median_before=med(mv_before), moved_median_after=med(mv_after), moved_lines=moved_lines,
                kept_lines=kept_lines, _err=(err_before, err_after, mv_before, mv_after))


def score_all(out):
    res = []
    tdir = os.path.join(out, "truth")
    for f in sorted(os.listdir(tdir)):
        if not f.endswith(".json"):
            continue
        truth = json.load(open(os.path.join(tdir, f), encoding="utf-8"))
        d = os.path.join(out, "jobs", truth["id"])
        try:
            now = load_doc(os.path.join(d, "words.json"))
            off = float(json.load(open(os.path.join(d, "job.json"), encoding="utf-8")).get("offset") or 0.0)
        except (OSError, ValueError):
            res.append({"id": truth["id"], "lang": truth.get("lang"), "scored": False, "why": "the sandbox copy is gone"})
            continue
        res.append(score_song(truth, now, off))
    ok = [r for r in res if r.get("scored")]
    tot = {k: sum(r[k] for r in ok) for k in COUNTS}
    eb = [x for r in ok for x in r["_err"][0]]; ea = [x for r in ok for x in r["_err"][1]]
    mb = [x for r in ok for x in r["_err"][2]]; ma = [x for r in ok for x in r["_err"][3]]
    med = lambda xs: round(statistics.median(xs), 2) if xs else None
    tot.update(songs=len(res), scored=len(ok), median_before=med(eb), median_after=med(ea),
               moved_median_before=med(mb), moved_median_after=med(ma))
    for r in res:
        r.pop("_err", None)
    return res, tot


def cmd_score(a):
    out = os.path.abspath(a.out)
    res, tot = score_all(out)
    if a.json:
        print(json.dumps({"songs": res, "total": tot}, indent=1, ensure_ascii=False))
        return
    print(f"{'song':44} lang  moved: fixed better worse untouched changed deleted | kept: broken (earlier) deleted | "
          f"lines added deleted | words: changed (=person's) | median error before->after s")
    for r in res:
        if not r.get("scored"):
            print(f"{r['id'][:44]:44} {r.get('lang') or '?':4}  NOT SCORED: {r['why']}")
            continue
        print(f"{r['id'][:44]:44} {r.get('lang') or '?':4}  {r['moved']:3}: {r['fixed']:3} {r['improved']:4} {r['worse']:4} "
              f"{r['untouched']:6} {r['changed']:7} {r['moved_deleted']:5} | {r['kept']:3}: {r['kept_broken']:3} "
              f"({r['kept_earlier']:2}) {r['kept_deleted']:5} | {r['added']:5} {r['deleted']:5} | {r['words_changed']:3} "
              f"({r['words_match']})  | {r['median_before']} -> {r['median_after']}"
              f" (moved lines {r['moved_median_before']} -> {r['moved_median_after']})")
    t = tot
    print(f"\ntotal: {t['scored']} of {t['songs']} songs scored, {t['lines']} lead lines")
    print(f"  lines the person moved: {t['moved']}: fixed {t['fixed']}, improved {t['improved']}, made worse "
          f"{t['worse']}, untouched {t['untouched']}, changed but no closer {t['changed']}, deleted {t['moved_deleted']}")
    sizes = ", ".join(f"{size_of(a_)} {t[f'kept_{size_of(a_)}']}" for a_, _ in SIZES)
    print(f"  lines the person kept: {t['kept']}: moved off by the agent {t['kept_broken']} (now >{FIXED} s from the "
          f"person's start: {t['kept_earlier']} earlier, {t['kept_later']} later; {sizes}), deleted {t['kept_deleted']}, "
          f"moved a little but still fine {t['kept_touched']}; small person moves (0.05-0.25 s): {t['small']}")
    print(f"    of those moved off, probably good (a repeat of a line the person moved earlier by about as much): "
          f"{t['kept_repeat_fix']}; the rest: {t['kept_broken'] - t['kept_repeat_fix']}")
    print(f"  lines the agent added (or split off): {t['added']}; deleted (or joined): {t['deleted']}; lines the person "
          f"deleted or joined (not compared): {t['person_gone']}")
    print(f"  the line before each moved line ({t['prev_end']}): the person changed its end on {t['prev_end_cut']}, now "
          f"within {FIXED} s of the person's on {t['prev_end_fixed']} of those; ends the person kept but now more than "
          f"{FIXED} s off: {t['prev_end_broken']}")
    print(f"  words: the agent changed {t['words_changed']} lines' words ({t['words_match']} now as the person's); the person "
          f"changed {t['person_words']} ({t['person_words_right']} matched by the agent)")
    print(f"  median start error, Studio -> now: all lines {t['median_before']} -> {t['median_after']} s; moved lines "
          f"{t['moved_median_before']} -> {t['moved_median_after']} s")
    kl = [x for r in res for x in r.get("kept_lines") or []]
    if kl:
        print(f"\nkept lines the agent moved off or deleted ({len(kl)}; line = the person's numbering, at = the person's start):")
        print(f"  {'song':40} line  at        delta   ⚠  neighbour moved by the person  repeat of a person's fix")
        for x in kl:
            dl = "deleted" if x["delta"] is None else f"{x['delta']:+.2f}"
            print(f"  {x['song'][:40]:40} {x['line'] or '?':>4}  {x['at']:8}  {dl:>7}  {'⚠' if x['weak'] else ' '}  "
                  f"{'yes' if x['neighbour_moved'] else '-':29}  {'yes' if x['repeat_of_fix'] else '-'}")
    skipped = [r for r in res if not r.get("scored")]
    if skipped:
        print(f"  NOT SCORED: {len(skipped)} songs (see above)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prepare"); s.add_argument("out")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--songs", help="job ids, comma-separated"); g.add_argument("--pick", type=int)
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--jobs", help="the Studio's jobs folder (read only; default: its config's KARAOKE_JOBS)")
    s.set_defaults(f=cmd_prepare)
    s = sub.add_parser("start"); s.add_argument("out"); s.add_argument("--port", type=int, default=PORT)
    s.add_argument("--cache", help="model cache folder (XDG_CACHE_HOME with MMS_FA), mounted read-only; default "
                                   "OUT/cache (the aligner's model is downloaded there on first use)")
    s.add_argument("--image", default="karaoke-studio:local"); s.add_argument("--no-gpu", action="store_true")
    s.add_argument("--allow-ok", action="store_true", help="the key may also clear ⚠ marks (ks ok)")
    s.set_defaults(f=cmd_start)
    s = sub.add_parser("_inside"); s.add_argument("out"); s.add_argument("--allow-ok", action="store_true"); s.set_defaults(f=cmd_inside)
    s = sub.add_parser("stop"); s.add_argument("out")
    s.add_argument("--port", type=int, help="only that port's container, if it is OUT's (default: what start recorded)")
    s.set_defaults(f=cmd_stop)
    s = sub.add_parser("score"); s.add_argument("out"); s.add_argument("--json", action="store_true"); s.set_defaults(f=cmd_score)
    a = ap.parse_args()
    if a.cmd == "prepare" and not a.jobs:
        sys.path.insert(0, HERE)
        import config as C
        a.jobs = C.JOBS
    a.f(a)


if __name__ == "__main__":
    main()
