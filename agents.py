"""Agents and who-did-what (README: "Finishing songs: by hand or with an agent"; AGENTS.md).

- Agent keys: made on the Agents tab (admins), one per agent ("Claude", "local model"...), each with scopes. A key is
  shown once; only its SHA-256 hash is kept (<KARAOKE_DATA>/agents.db). Requests carrying `X-Studio-Agent-Key` are
  checked against the key's scopes on every call (ROUTES below maps each endpoint to a scope; anything not listed is
  refused for agents). Requests without a key behave as before; AGENT_KEYS_REQUIRED makes API calls without a login
  header need a key.
- Attribution: every edit says who made it: a person (the proxy's login header, X-authentik-username by default),
  an agent (its key's name), "ks" (the command-line tool without a key) or "web" (the web app without a login header).
- Edit history: before each change to a song's lines (or its timing offset) the old state is kept in
  <job>/history/, with who made the change and what it was; Undo puts back the last one, whoever made it.
"""
import hashlib, json, os, re, secrets, sqlite3, threading, time, uuid

import config as C

# (scope, what it allows, on by default for a new key)
SCOPES = [
    ("read", "Read songs, lines, what was heard, pictures and logs", True),
    ("edit", "Edit timing and words (move, stretch, retype, split, join, add, delete lines; offset; undo)", True),
    ("notes", "Leave notes on lines and flag lines for you to check", True),
    ("make", "Make new songs (search, queue, cancel)", True),
    ("clear_marks", "Clear ⚠ marks, mark songs \"checked by you\", dismiss player flags", False),
    ("publish", "Publish songs (and rename published ones)", False),
    ("remake", "Remake, change video or lyrics, retry, trim, custom videos, reset to the automatic version", False),
    ("delete", "Delete songs, remove old versions from the library", False),
]
SCOPE_NAMES = [s for s, _, _ in SCOPES]
DEFAULT_SCOPES = [s for s, _, on in SCOPES if on]
ADMIN = "admin"   # never for agents: keys, players, requests admin, profiles
KEY_HEADER = "x-studio-agent-key"
KEY_PREFIX = "ksa_"
NAME_RE = re.compile(r"^[\w .'()+-]{1,40}$", re.UNICODE)

# Which scope each API call needs when it comes with an agent key. (methods, path pattern, scope); first match wins.
# GETs not listed are "read". Every other method must be listed (tests/test_agents.py walks app.routes); unlisted
# ones are refused for agents. /api/encore/* (players, with their own tokens) is outside this.
J = r"/api/jobs/[^/]+"
ROUTES = [
    (("GET", "POST", "PATCH", "DELETE", "PUT"), r"/api/agents(/.*)?", ADMIN),
    (("GET", "POST", "DELETE"), r"/api/players(/.*)?", ADMIN),
    (("POST",), r"/api/requests/[^/]+/[^/]+", ADMIN),
    (("GET", "POST", "DELETE"), r"/api/profiles(/.*)?", ADMIN),
    (("GET", "POST"), r"/api/automake", ADMIN),              # party auto-make: a Studio feature, never an agent's
    (("POST",), r"/api/jobs", "make"),                      # (with "replace" = Change video or lyrics: remake)
    (("POST",), J + r"/cancel", "make"),
    (("POST",), J + r"/lines", "edit"),                     # (clearing marks or ok needs clear_marks; see app.put_lines)
    (("POST",), J + r"/lines/undo", "edit"),
    (("POST",), J + r"/offset", "edit"),
    (("POST",), J + r"/display", "edit"),
    (("POST",), J + r"/backing", "edit"),
    (("POST",), J + r"/chinese-reading", "edit"),
    (("POST",), J + r"/rename", "edit"),                    # (a published song: publish too, see app.rename)
    (("POST",), J + r"/detect-singers", "edit"),            # (suggestions only, nothing saved)
    (("POST",), J + r"/romaji-paste", "edit"),              # (suggestions only)
    (("POST",), J + r"/romaji-check", "edit"),              # (suggestions only)
    (("POST",), J + r"/notes", "notes"),
    (("POST",), J + r"/reviewed", "clear_marks"),
    (("POST",), J + r"/encore-flags/dismiss", "clear_marks"),
    (("POST",), r"/api/flags/dismiss", "clear_marks"),
    (("POST",), J + r"/publish", "publish"),
    (("POST",), J + r"/videos/[^/]+/publish", "publish"),
    (("POST",), J + r"/remake", "remake"),
    (("POST",), J + r"/undo-remake", "remake"),
    (("POST",), J + r"/retry", "remake"),
    (("POST",), J + r"/trim", "remake"),
    (("POST",), J + r"/lines/reset", "remake"),
    (("POST",), J + r"/videos", "remake"),
    (("PUT",), J + r"/videos/upload", "remake"),
    (("POST",), J + r"/videos/restore", "remake"),
    (("POST",), J + r"/videos/[^/]+/render", "remake"),
    (("DELETE",), J + r"/videos/[^/]+", "remake"),
    (("DELETE",), J, "delete"),
    (("POST",), r"/api/library/remove-old", "delete"),
    (("POST",), J + r"/agent/alt-lyrics", "read"),          # (scores lyrics against the song; nothing saved)
    (("POST",), r"/api/agent/romaji", "read"),              # (romanizes text; nothing saved)
]
_ROUTES = [(set(m), re.compile(p + r"/?$"), s) for m, p, s in ROUTES]


def scope_for(method, path):
    """The scope an agent needs for this call; ADMIN = never an agent; None = not listed (refused)."""
    for methods, rx, scope in _ROUTES:
        if method in methods and rx.match(path):
            return scope
    if method in ("GET", "HEAD"):
        return "read"
    return None


def scope_label(scope):
    return next((label for s, label, _ in SCOPES if s == scope), scope)


# ---------------- who is asking ----------------
def person_header():
    return (C.PERSON_HEADER or "x-authentik-username").lower()


def actor_from_headers(headers, key_row=None):
    """{"kind": agent|person|ks|web, "name", "key"?, "scopes"?} for a request's headers (a dict-like with lower-case
    names, or starlette's Headers)."""
    if key_row is not None:
        return {"kind": "agent", "name": key_row["name"], "key": key_row["id"], "scopes": list(key_row["scopes"])}
    get = headers.get if hasattr(headers, "get") else (lambda k, d=None: d)
    who = " ".join(str(get(person_header()) or get(person_header().title()) or "").split())[:60]
    if who:
        return {"kind": "person", "name": who}
    ua = str(get("user-agent") or "")
    if ua.startswith("ks/"):
        return {"kind": "ks", "name": "ks"}
    return {"kind": "web", "name": "web"}


def actor_of(req):
    """The actor the gate put on the request, or one worked out from its headers (handlers called directly)."""
    st = getattr(req, "state", None)
    a = getattr(st, "actor", None) if st is not None else None
    return a or actor_from_headers(getattr(req, "headers", {}) or {})


def stamp(actor, what=None, at=None):
    """What gets stored with an edit: who (name and kind) and when; never the key itself."""
    d = {"by": actor["name"], "kind": actor["kind"], "at": round(at or time.time(), 3)}
    if what:
        d["what"] = str(what)[:120]
    return d


def can(actor, scope):
    return actor.get("kind") != "agent" or scope in (actor.get("scopes") or ())


# ---------------- keys ----------------
def key_hash(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class KeyStore:
    SCHEMA = """create table if not exists keys (id text primary key, name text, hash text unique, scopes text,
                created real, created_by text, last_used real, uses integer default 0)"""

    def __init__(self, path=None, now=time.time):
        self.path = path or os.path.join(C.DATA, "agents.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.now = now
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.execute(self.SCHEMA)
            self.db.commit()
        self._used = {}

    @staticmethod
    def _row(r):
        return {"id": r["id"], "name": r["name"], "scopes": json.loads(r["scopes"] or "[]"), "created": r["created"],
                "created_by": r["created_by"], "last_used": r["last_used"], "uses": r["uses"] or 0}

    @staticmethod
    def clean_scopes(scopes):
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise ValueError("scopes is a list of names")
        bad = [s for s in scopes if s not in SCOPE_NAMES]
        if bad:
            raise ValueError("unknown scope: " + ", ".join(bad[:4]))
        return [s for s in SCOPE_NAMES if s in scopes]

    @staticmethod
    def clean_name(name):
        name = " ".join(str(name or "").split())
        if not NAME_RE.match(name):
            raise ValueError("give the agent a name of 1-40 letters, digits, spaces or . ' ( ) + -")
        return name

    def create(self, name, scopes=None, by=None):
        """A new key: (the key, shown only now; its row)."""
        name = self.clean_name(name)
        scopes = self.clean_scopes(DEFAULT_SCOPES if scopes is None else scopes)
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        kid = uuid.uuid4().hex[:12]
        with self.lock:
            if self.db.execute("select 1 from keys where lower(name)=lower(?)", (name,)).fetchone():
                raise ValueError(f"there's already an agent called {name}; pick another name or revoke that key first")
            self.db.execute("insert into keys (id, name, hash, scopes, created, created_by) values (?, ?, ?, ?, ?, ?)",
                            (kid, name, key_hash(key), json.dumps(scopes), self.now(), (by or "")[:60] or None))
            self.db.commit()
        return key, self.get(kid)

    def get(self, kid):
        with self.lock:
            r = self.db.execute("select * from keys where id=?", (kid,)).fetchone()
        return self._row(r) if r else None

    def list(self):
        with self.lock:
            rows = self.db.execute("select * from keys order by created").fetchall()
        return [self._row(r) for r in rows]

    def update(self, kid, scopes):
        scopes = self.clean_scopes(scopes)
        with self.lock:
            n = self.db.execute("update keys set scopes=? where id=?", (json.dumps(scopes), kid)).rowcount
            self.db.commit()
        return self.get(kid) if n else None

    def revoke(self, kid):
        with self.lock:
            n = self.db.execute("delete from keys where id=?", (kid,)).rowcount
            self.db.commit()
        self._used.pop(kid, None)
        return bool(n)

    def auth(self, key):
        """The key's row, or None. Looked up by hash (the hash of a 256-bit random key can't be guessed, so a lookup
        by it leaks nothing a timing attack could use); last_used is written at most once a minute."""
        if not key or not key.startswith(KEY_PREFIX) or len(key) > 200:
            return None
        with self.lock:
            r = self.db.execute("select * from keys where hash=?", (key_hash(key),)).fetchone()
            if not r:
                return None
            now = self.now()
            if now - self._used.get(r["id"], 0) >= 60:
                self._used[r["id"]] = now
                self.db.execute("update keys set last_used=?, uses=coalesce(uses, 0)+1 where id=?", (now, r["id"]))
                self.db.commit()
            return self._row(r)


# ---------------- edit history (Undo) ----------------
HISTORY = "history"
KEEP = 200


def _hdir(job_dir):
    return os.path.join(job_dir, HISTORY)


def snapshot(job_dir, actor, what, lines=None, cast=None, offset=None):
    """Keep the state from before an edit: its lines (and cast), or its offset for an offset change, with who made the
    edit and what it was. Returns the entry's id."""
    d = _hdir(job_dir)
    os.makedirs(d, exist_ok=True)
    eid = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1e6) % 1_000_000:06d}-{secrets.token_hex(2)}"
    e = dict(stamp(actor, what), id=eid)
    if lines is not None:
        e["lines"], e["cast"] = lines, cast or []
    if offset is not None:
        e["offset"] = offset
    with open(os.path.join(d, eid + ".json.tmp"), "w") as f:
        json.dump(e, f, ensure_ascii=False)
    os.replace(os.path.join(d, eid + ".json.tmp"), os.path.join(d, eid + ".json"))
    old = sorted(x for x in os.listdir(d) if x.endswith(".json"))
    for x in old[:-KEEP]:
        os.remove(os.path.join(d, x))
    return eid


def entries(job_dir):
    """Undoable edits, newest first, without their saved lines."""
    d = _hdir(job_dir)
    if not os.path.isdir(d):
        return []
    out = []
    for x in sorted((x for x in os.listdir(d) if x.endswith(".json")), reverse=True):
        try:
            e = json.load(open(os.path.join(d, x)))
        except (OSError, ValueError):
            continue
        out.append({k: e.get(k) for k in ("id", "by", "kind", "at", "what")} | {"offset_only": "lines" not in e})
    return out


def last(job_dir):
    d = _hdir(job_dir)
    if not os.path.isdir(d):
        return None
    xs = sorted(x for x in os.listdir(d) if x.endswith(".json"))
    return json.load(open(os.path.join(d, xs[-1]))) if xs else None


def drop(job_dir, eid, why="undone"):
    """Move an entry out of the history (undone, or retired)."""
    d = _hdir(job_dir)
    os.makedirs(os.path.join(d, why), exist_ok=True)
    src = os.path.join(d, eid + ".json")
    if os.path.exists(src):
        os.replace(src, os.path.join(d, why, eid + ".json"))


def retire(job_dir):
    """The song's timing changed underneath the history (a trim, a remake, a reset, another reading): its entries
    would put lines back at the wrong times, so none of them can be undone any more."""
    for e in entries(job_dir):
        drop(job_dir, e["id"], "retired")


# ---------------- what an edit to the lines changed ----------------
def _wkey(l):
    return json.dumps(l.get("words"), ensure_ascii=False)


def marks_cleared(old, new):
    """How many lines lost their ⚠ or were marked fine (ok) without being retimed or reworded. A mark that goes
    because its line moved is an edit (the studio's own rule), not clearing a mark."""
    weak, unok = {}, {}
    for l in old:
        if l.get("weak"):
            weak[_wkey(l)] = weak.get(_wkey(l), 0) + 1
        if not l.get("ok"):
            unok[_wkey(l)] = unok.get(_wkey(l), 0) + 1
    n = 0
    for l in new:
        k = _wkey(l)
        hit = False
        if not l.get("weak") and weak.get(k):
            weak[k] -= 1
            hit = True
        if l.get("ok") and unok.get(k):
            unok[k] -= 1
            hit = True
        n += hit
    return n


def _note_key(n):
    return (n.get("by"), n.get("text")) if isinstance(n, dict) else None


def clean_note(n):
    if not isinstance(n, dict):
        return None
    text = " ".join(str(n.get("text") or "").split())[:300]
    by = " ".join(str(n.get("by") or "").split())[:60]
    if not text or not by:
        return None
    kind = n.get("kind") if n.get("kind") in ("agent", "person", "ks", "web") else "web"
    try:
        at = round(float(n.get("at") or 0), 3)
    except (TypeError, ValueError):
        at = 0.0
    return {"by": by, "kind": kind, "text": text, "at": at}


def keep_notes(old, new, actor):
    """Notes on lines are signed by the studio: a note sent back unchanged keeps its author; a new or changed one is
    signed by whoever saves it. Returns how many notes are new or changed (an agent needs the notes scope)."""
    known = {_note_key(clean_note(l.get("note"))) for l in old if clean_note(l.get("note"))}
    changed = 0
    for l in new:
        n = clean_note(l.get("note")) if isinstance(l.get("note"), dict) else None
        if n is None:
            l["note"] = None
            continue
        if _note_key(n) in known:
            l["note"] = n
            continue
        # new or edited: the text is the sender's, the name is the studio's
        l["note"] = dict(n, by=actor["name"], kind=actor["kind"], at=round(time.time(), 3))
        changed += 1
    gone = len(known - {_note_key(l["note"]) for l in new if l.get("note")})
    return changed + gone


def describe(old, new):
    """A short "what" for an edit that didn't say (the web editor's Save)."""
    o = {(_wkey(l), l.get("orig")) for l in old}
    n = {(_wkey(l), l.get("orig")) for l in new}
    ch = len(n - o)
    if len(new) > len(old):
        return f"editor: {ch} line{'s' if ch != 1 else ''} changed, {len(new) - len(old)} added"
    if len(new) < len(old):
        return f"editor: {ch} line{'s' if ch != 1 else ''} changed, {len(old) - len(new)} removed"
    if ch:
        return f"editor: {ch} line{'s' if ch != 1 else ''} changed"
    return "editor: marks, singers or notes"
