"""Review: a person checks, by ear, line timings an agent (or ks) changed or asked about, and says which is right.

An agent can't listen; a person can, in seconds. Every review item holds a line's two timings, played over the same
stretch of the song with the words lighting up, and the person answers: A is right, B is right, no real difference,
or both wrong. The answers are kept, so they are also test data: how often each agent's moves were right.

Where items come from:
- "edit": ks or an agent key moved a line's start or end by more than MOVED (agent-bench.py's "moved") without
  changing its words. A: the timing before, B: after (already applied). Edited again before review: B follows, A stays
  the first timing; put back as it was: the item closes.
- "ask": an agent wasn't sure and asked (ks ask): A is the line now, B its suggestion (not applied).
- "bench": a benchmark sandbox's timing for a line, against the line now (deploy/bench-to-review.py), shown blind
  (A and B in a fixed random order, nobody told which is the agent's).

An item names its line by the words' text and where it starts (lines are re-sorted, split and added, so numbers move).
The live line is looked up again whenever items are listed: if it no longer has the applied timing (edited by hand,
undone, remade), the item closes as "changed since" instead of asking about a line that isn't there any more.

Stored in <KARAOKE_DATA>/reviews.json; only people answer (the answer route is admin-only, and ks or an agent key is
refused in the handler too).
"""
import difflib, hashlib, json, os, secrets, threading, time

import config as C

MOVED = 0.25          # seconds: a start or end moved more than this is worth a person's ear (agent-bench.py MOVED)
SAME = 0.02           # seconds: word times this close are the same timing
VERDICTS = ("a", "b", "same", "neither")
KINDS = ("edit", "ask", "bench")
PATH = os.path.join(C.DATA, "reviews.json")
_lock = threading.RLock()
_open = [None]        # cached count of open items (for /api/status, polled every 30 s)


# ---- storage ----
def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("items"), list) else {"items": []}
    except (OSError, ValueError):
        return {"items": []}


def _save(d):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, PATH)
    _open[0] = sum(1 for x in d["items"] if x.get("state") == "open")


def open_count():
    if _open[0] is None:
        with _lock:
            _open[0] = sum(1 for x in _load()["items"] if x.get("state") == "open")
    return _open[0]


# ---- lines ----
def text_of(line):
    return " ".join(w[2] for w in line.get("words") or [])


def _words(line):
    return [[round(float(a), 3), round(float(b), 3), str(t)] for a, b, t in line.get("words") or []]


def same_timing(w1, w2):
    return len(w1) == len(w2) and all(abs(a[0] - b[0]) <= SAME and abs(a[1] - b[1]) <= SAME and a[2] == b[2]
                                      for a, b in zip(w1, w2))


def find_line(lines, text, start, near=3.0):
    """Index of the line with this text starting nearest `start` (within `near` s), or None."""
    best = None
    for i, l in enumerate(lines):
        if l.get("backing") or text_of(l) != text or not l.get("words"):
            continue
        d = abs(l["words"][0][0] - start)
        if d <= near and (best is None or d < best[0]):
            best = (d, i)
    return best[1] if best else None


def moved_lines(old, new):
    """[(old line, new line)] for lines whose words stayed the same and whose start or end moved more than MOVED."""
    a, b = [text_of(l) for l in old], [text_of(l) for l in new]
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            continue
        for o, n in zip(old[i1:i2], new[j1:j2]):
            if not o.get("words") or not n.get("words"):
                continue
            if (abs(o["words"][0][0] - n["words"][0][0]) > MOVED or abs(o["words"][-1][1] - n["words"][-1][1]) > MOVED):
                out.append((o, n))
    return out


def _option(key, role, words, label):
    return {"key": key, "role": role, "words": words, "label": label}


def _blind_order(seed):
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 2 == 1


def _item(job, kind, by, what, line_now, other_words, question=None, blind=False):
    """A new item: for "edit", line_now is the line before (A) and other_words the timing applied (B)."""
    rid = secrets.token_hex(6)
    now_w = _words(line_now)
    if kind == "edit":
        opts = [_option("a", "old", now_w, "Before"), _option("b", "new", other_words, "After")]
        live = "b"
    else:
        opts = [_option("a", "now", now_w, "Now"), _option("b", "alt", other_words, "Suggested")]
        live = "a"
        if blind and _blind_order(rid):
            opts = [dict(opts[1], key="a"), dict(opts[0], key="b")]
            live = "b"
        if blind:
            opts = [dict(o, label=o["key"].upper()) for o in opts]
    return {"id": rid, "job": job, "kind": kind, "created": round(time.time(), 3), "by": by, "what": (what or "")[:160],
            "question": (question or "")[:300] or None, "text": text_of(line_now), "blind": bool(blind),
            "options": opts, "live": live, "state": "open", "answer": None}


def opt(item, key):
    return next(o for o in item["options"] if o["key"] == key)


# ---- making items ----
def from_edit(job, old, new, actor, what):
    """After ks or an agent key saved a song's lines: an item for each line it moved (see the module doc).
    Returns how many items were added or updated."""
    if actor.get("kind") not in ("agent", "ks"):
        return 0
    pairs = moved_lines(old, new)
    # a block moved together (ks move --ripple, a whole chorus): one item for its first line, not one per line
    runs, k = [], 0
    while k < len(pairs):
        d = pairs[k][1]["words"][0][0] - pairs[k][0]["words"][0][0]
        j = k + 1
        while j < len(pairs) and abs(pairs[j][1]["words"][0][0] - pairs[j][0]["words"][0][0] - d) <= SAME:
            j += 1
        if j - k >= 3:
            runs.append((pairs[k], j - k - 1))
            k = j
        else:
            runs.append((pairs[k], 0))
            k += 1
    with _lock:
        d = _load()
        mine = [x for x in d["items"] if x["job"] == job and x["kind"] == "edit" and x["state"] == "open"]
        n = 0
        for (o, nl), more in runs:
            ow, nw = _words(o), _words(nl)
            what_ = (what or "") + (f" (and the {more} lines after it, moved the same)" if more else "")
            prev = next((x for x in mine if x["text"] == text_of(o) and same_timing(opt(x, "b")["words"], ow)), None)
            if prev:   # moved again before anyone listened: B follows, A stays the first timing
                if same_timing(opt(prev, "a")["words"], nw):
                    prev.update(state="closed", closed="put back as it was", closed_at=round(time.time(), 3))
                else:
                    opt(prev, "b")["words"] = nw
                    prev.update(what=what_[:160], by=_by(actor))
                n += 1
                continue
            d["items"].append(_item(job, "edit", _by(actor), what_, o, nw))
            n += 1
        if n:
            _save(d)
    return n


def _by(actor):
    return {"name": actor.get("name"), "kind": actor.get("kind")}


def add(job, line, other_words, actor, kind="ask", question=None, blind=False, what=None):
    """An "ask" or "bench" item: line is the live line now; other_words the other timing (not applied)."""
    if kind not in ("ask", "bench"):
        raise ValueError("kind")
    if same_timing(_words(line), other_words):
        raise ValueError("that is the line's timing now")
    with _lock:
        d = _load()
        dup = next((x for x in d["items"] if x["job"] == job and x["state"] == "open" and x["text"] == text_of(line)
                    and any(same_timing(o["words"], other_words) for o in x["options"])), None)
        if dup:
            return dup
        it = _item(job, kind, _by(actor), what, line, other_words, question, blind)
        d["items"].append(it)
        _save(d)
        return it


# ---- listing, with the live line checked ----
def refresh(items, lines_of):
    """Close open items whose line no longer has the applied timing. lines_of(job) -> its lines (None: song gone).
    Returns True when something closed (the caller saves)."""
    cache, changed = {}, False
    for x in items:
        if x["state"] != "open":
            continue
        if x["job"] not in cache:
            cache[x["job"]] = lines_of(x["job"])
        lines = cache[x["job"]]
        live = opt(x, x["live"])["words"]
        if lines is None:
            x.update(state="closed", closed="the song is gone", closed_at=round(time.time(), 3))
            changed = True
            continue
        i = find_line(lines, x["text"], live[0][0], near=0.05)
        if i is None or not same_timing(_words(lines[i]), live):
            x.update(state="closed", closed="the line changed since", closed_at=round(time.time(), 3))
            changed = True
    return changed


def listing(lines_of, state="open"):
    with _lock:
        d = _load()
        if refresh(d["items"], lines_of):
            _save(d)
        items = [x for x in d["items"] if state == "all" or x["state"] == state
                 or (state == "done" and x["state"] in ("answered", "closed"))]
        key = (lambda x: (x.get("answer") or {}).get("at") or x.get("closed_at") or 0) if state == "done" else (lambda x: x["created"])
        return sorted(items, key=key, reverse=state == "done"), stats(d["items"])


def get(rid):
    with _lock:
        return next((x for x in _load()["items"] if x["id"] == rid), None)


def stats(items):
    """Per who made or suggested the change: how the people's answers came out ("edit" and "ask" items; "bench" is
    per kind). right = the changed / suggested timing was the one; wrong = the other one was."""
    out = {}
    for x in items:
        a = x.get("answer")
        if not a:
            continue
        who = x["by"]["name"] if x["kind"] != "bench" else (x.get("what") or "bench")
        s = out.setdefault(who, {"right": 0, "wrong": 0, "same": 0, "neither": 0})
        v = a["verdict"]
        if v in ("same", "neither"):
            s[v] += 1
        else:
            role = opt(x, v)["role"]
            s["right" if role in ("new", "alt") else "wrong"] += 1
    return out


def answer(rid, verdict, actor, note=None, applied=None):
    """Record a person's answer. applied: what was done to the song ("set to A" / ...), or None."""
    if verdict not in VERDICTS:
        raise ValueError("verdict")
    with _lock:
        d = _load()
        x = next((x for x in d["items"] if x["id"] == rid), None)
        if x is None:
            raise KeyError(rid)
        x["answer"] = {"verdict": verdict, "by": actor.get("name"), "at": round(time.time(), 3),
                       "note": " ".join(str(note or "").split())[:300] or None, "applied": applied}
        x["state"] = "answered"
        if applied and verdict in ("a", "b"):
            x["live"] = verdict
        _save(d)
        return x
