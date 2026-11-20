"""Players (Encore) on the studio's side of contract/ENCORE-STUDIO.md: linked players and their tokens, pairing codes,
song requests, lyric flags and plays. No web code here (app.py has the /api/encore/ routes and the admin pages);
everything is kept in <KARAOKE_DATA>/encore.db (SQLite).

- Pairing: "Connect a player" makes a one-time code (8 characters without look-alikes, 10 minutes, one use, dead
  after 5 wrong tries; only one code at a time). POST /api/encore/pair with it gives the player its own token: 256
  random bits, kept only as a SHA-256 hash. Pairing the same player uuid again replaces its token. ENCORE_TOKEN (env)
  is accepted as one more player, "preset", which can't be unlinked here. Codes and tokens are never logged.
- Requests, flags and plays carry the player's own ids (rid, fid, pid): storing one twice changes nothing and
  answers what the first one answered.
- Request statuses follow the studio's songs: a job that remembers the request (job["encore_rids"], set by "Find and
  make", "We have it", a match on arrival, and kept when a song is made again in place of another) moves it to making,
  ready, published. A job that fails, is cancelled or deleted puts it back to open. "failed", "auto", "stage" and
  "eta" are for party auto-make (§2.2b, not built yet).
"""
import hashlib, hmac, json, os, re, secrets, sqlite3, threading, time, uuid
from collections import deque

import config as C

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    CONTRACT = open(os.path.join(HERE, "contract", "CONTRACT_VERSION")).read().strip()
except OSError:
    CONTRACT = "1"
API = 1
VERSION = "2026.11.20"          # the studio's version, as players see it (hello, pair)
CATALOG_VERSION = 2
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
CODE_LEN, CODE_TTL, CODE_TRIES = 8, 600, 5
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PRESET = "preset"
FINAL = ("published", "have", "declined", "failed")   # statuses the studio's songs don't move any more
COUNTED = "finished"            # a play counts when finished, or stopped after more than half the song

SCHEMA = """
create table if not exists kv (k text primary key, v text);
create table if not exists players (player text primary key, name text, token_hash text, created real, last_seen real,
    encore text, contract text, backlog integer);
create table if not exists requests (rid text primary key, player text, artist text, title text, note text, by_name text,
    party text, at integer, uid text, auto integer default 0, queue text, status text, job text, song_id text, base text,
    reason text, stage text, eta real, answer text, created real, updated real, detached integer default 0);
create index if not exists requests_player on requests (player, updated);
create table if not exists flags (fid text primary key, player text, song_id text, base text, job text, version text,
    t real, line integer, mapped integer, kind text, note text, by_name text, party text, at integer, uid text,
    status text, created real, dismissed real);
create index if not exists flags_job on flags (job);
create table if not exists plays (pid text primary key, player text, song_id text, base text, at integer, seconds integer,
    result text, singers integer, style text, party text, uid text, counted integer, created real);
"""


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def norm_code(code):
    return re.sub(r"[\s-]", "", str(code or "")).upper()


class RateLimit:
    """At most n hits per `per` seconds per key (an address)."""

    def __init__(self, n, per=60.0):
        self.n, self.per, self.hits, self.lock = n, per, {}, threading.Lock()

    def hit(self, key, now=None):
        """Count a hit; False if the key is over the limit (the hit still counts)."""
        now = time.time() if now is None else now
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and q[0] <= now - self.per:
                q.popleft()
            q.append(now)
            if len(self.hits) > 5000:  # forget idle addresses
                for k in [k for k, v in self.hits.items() if not v or v[-1] <= now - self.per]:
                    del self.hits[k]
            return len(q) <= self.n

    def over(self, key, now=None):
        now = time.time() if now is None else now
        with self.lock:
            q = self.hits.get(key) or ()
            return sum(1 for x in q if x > now - self.per) >= self.n


class PairError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


class Store:
    def __init__(self, path=None, preset_token=None, now=time.time):
        self.path = path or os.path.join(C.DATA, "encore.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.now = now
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            cols = {r[1] for r in self.db.execute("pragma table_info(requests)")}
            if "detached" not in cols:  # (a store from before "Not this song")
                self.db.execute("alter table requests add column detached integer default 0")
            self.db.commit()
        self.code = None            # {"hash", "expires", "wrong"}: the one pairing code that's valid now
        self.preset_hash = token_hash(preset_token) if preset_token else None
        self._seen = {}             # player -> when last_seen was last written (written at most once a minute)
        if self.preset_hash:        # listed from the start ("never seen" until it calls)
            self.q("insert or ignore into players (player, name, token_hash, created) values (?, ?, null, ?)",
                   (PRESET, "Preset player (ENCORE_TOKEN)", self.now()))

    def studio_id(self):
        """This studio's own uuid, made once and kept (players keep their unsent items per studio: §2.8)."""
        with self.lock:
            r = self.db.execute("select v from kv where k='studio_id'").fetchone()
            if r:
                return r[0]
            sid = str(uuid.uuid4())
            self.db.execute("insert into kv (k, v) values ('studio_id', ?)", (sid,))
            self.db.commit()
            return sid

    def q(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            rows = cur.fetchall()
            self.db.commit()
            return rows

    # ---------------- pairing ----------------
    def new_code(self):
        """A new one-time pairing code (the previous one stops working). Returns (code, expires)."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        with self.lock:
            self.code = {"hash": token_hash(code), "expires": self.now() + CODE_TTL, "wrong": 0}
        return code, self.code["expires"]

    def code_state(self):
        c = self.code
        if not c or c["expires"] <= self.now():
            return None
        return {"expires": c["expires"], "wrong": c["wrong"]}

    def cancel_code(self):
        with self.lock:
            self.code = None

    def pair(self, code, name, player, encore="", contract=""):
        """Link a player with the current code. Returns its new token; raises PairError(403) for a wrong, used or
        expired code (5 wrong tries end the code)."""
        given = token_hash(norm_code(code))
        with self.lock:
            c = self.code
            if not c or c["expires"] <= self.now():
                self.code = None
                raise PairError(403, "That pairing code isn't valid (it's wrong, used or expired).")
            if not hmac.compare_digest(given, c["hash"]):
                c["wrong"] += 1
                if c["wrong"] >= CODE_TRIES:
                    self.code = None
                raise PairError(403, "That pairing code isn't valid (it's wrong, used or expired).")
            self.code = None    # one use
            token = secrets.token_urlsafe(32)  # 256 bits
            now = self.now()
            old = self.db.execute("select created from players where player=?", (player,)).fetchone()
            self.db.execute("insert or replace into players (player, name, token_hash, created, last_seen, encore, contract, "
                            "backlog) values (?, ?, ?, ?, ?, ?, ?, ?)",
                            (player, name, token_hash(token), now if not old else old["created"], now, encore[:40],
                             contract[:20], None))
            self.db.commit()
            self._seen.pop(player, None)
        return token

    def auth(self, token):
        """The player a bearer token belongs to (its id), or None. Compares hashes in constant time."""
        if not token:
            return None
        h = token_hash(token)
        found = None
        if self.preset_hash and hmac.compare_digest(h, self.preset_hash):
            found = PRESET
        for r in self.q("select player, token_hash from players where token_hash is not null"):
            if hmac.compare_digest(h, r["token_hash"]) and not found:
                found = r["player"]
        return found

    def seen(self, player, encore=None, contract=None, backlog=None):
        """A player called: remember when (at most one write a minute), and what it says about itself."""
        now = self.now()
        if player == PRESET and not self.q("select 1 from players where player=?", (PRESET,)):
            self.q("insert or ignore into players (player, name, token_hash, created) values (?, ?, null, ?)",
                   (PRESET, "Preset player (ENCORE_TOKEN)", now))
        if now - self._seen.get(player, 0) < 60 and encore is None and backlog is None:
            return
        self._seen[player] = now
        sets, args = ["last_seen=?"], [now]
        for k, v in (("encore", encore), ("contract", contract), ("backlog", backlog)):
            if v is not None:
                sets.append(f"{k}=?")
                args.append(v)
        self.q(f"update players set {', '.join(sets)} where player=?", (*args, player))

    def players(self):
        out = []
        for r in self.q("select * from players order by created"):
            if r["player"] == PRESET and not self.preset_hash:
                continue
            out.append({"player": r["player"], "name": r["name"], "created": r["created"], "last_seen": r["last_seen"],
                        "encore": r["encore"], "contract": r["contract"], "backlog": r["backlog"],
                        "preset": r["player"] == PRESET})
        return out

    def unlink(self, player):
        """Revoke a player's token (its next call gets 401). The preset can't be unlinked here."""
        if player == PRESET:
            raise PairError(400, "the preset player is set by the server config (ENCORE_TOKEN)")
        with self.lock:
            n = self.db.execute("delete from players where player=?", (player,)).rowcount
            self.db.commit()
            self._seen.pop(player, None)
        return n > 0

    def player_names(self):
        return {r["player"]: r["name"] for r in self.q("select player, name from players")}

    # ---------------- requests ----------------
    def get_request(self, rid):
        r = self.q("select * from requests where rid=?", (rid,))
        return dict(r[0]) if r else None

    def add_request(self, player, b, arrival):
        """Store a request (once per rid). arrival: (status, job, id, base) from matching it with the songs.
        Returns (the answer, new?)."""
        old = self.get_request(b["rid"])
        if old:
            return json.loads(old["answer"]), False
        st, job, sid, base = arrival
        ans = {"rid": b["rid"], "status": "have" if st == "have" else "open",
               "id": sid if st == "have" else None, "base": base if st == "have" else None}
        now = self.now()
        q = b.get("queue")
        self.q("insert or ignore into requests (rid, player, artist, title, note, by_name, party, at, uid, auto, queue, status, "
               "job, song_id, base, reason, answer, created, updated) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (b["rid"], player, b.get("artist") or "", b["title"], b.get("note") or "", b["by"], b.get("party") or "",
                int(b.get("at") or 0), b.get("uid"), 1 if b.get("auto") else 0, json.dumps(q) if q else None, st, job,
                sid, base, None, json.dumps(ans), now, now))
        return ans, True

    def requests(self, player=None, rids=None, since=None, statuses=None):
        sql, args = "select * from requests where 1=1", []
        if player is not None:
            sql += " and player=?"
            args.append(player)
        if rids is not None:
            if not rids:
                return []
            sql += f" and rid in ({','.join('?' * len(rids))})"
            args += list(rids)
        if since is not None:
            sql += " and updated > ?"
            args.append(since)
        if statuses:
            sql += f" and status in ({','.join('?' * len(statuses))})"
            args += list(statuses)
        return [dict(r) for r in self.q(sql + " order by created desc", args)]

    def set_request(self, rid, **kw):
        """Change a request's status fields; `updated` moves only when something changed. True if it did."""
        old = self.get_request(rid)
        if not old:
            return False
        if all(old.get(k) == v for k, v in kw.items()):
            return False
        kw["updated"] = max(self.now(), (old.get("updated") or 0) + 0.001)
        self.q(f"update requests set {', '.join(k + '=?' for k in kw)} where rid=?", (*kw.values(), rid))
        return True

    @staticmethod
    def public_request(r):
        """A request as GET /api/encore/requests answers it."""
        out = {"rid": r["rid"], "status": r["status"], "job": r["job"] if r["status"] in ("making", "ready", "published") else None,
               "id": r["song_id"] if r["status"] in ("published", "have") else None,
               "base": r["base"] if r["status"] in ("published", "have") else None,
               "reason": r["reason"] if r["status"] in ("declined", "failed") else None, "updated": round(r["updated"], 3)}
        if r.get("auto"):
            out.update(auto=True, stage=r.get("stage"), eta=r.get("eta"))
        return out

    # ---------------- flags ----------------
    def get_flag(self, fid):
        r = self.q("select * from flags where fid=?", (fid,))
        return dict(r[0]) if r else None

    def add_flag(self, player, b, status, job=None, version=None, mapped=None):
        now = self.now()
        self.q("insert or ignore into flags (fid, player, song_id, base, job, version, t, line, mapped, kind, note, by_name, party, "
               "at, uid, status, created) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (b["fid"], player, b["id"], b["base"], job, version, b.get("t"), b.get("line"), mapped, b["kind"],
                b.get("note") or "", b["by"], b.get("party") or "", int(b.get("at") or 0), b.get("uid"), status, now))

    def flags(self, job=None, open_only=False, unmatched=False):
        sql, args = "select * from flags where 1=1", []
        if job is not None:
            sql += " and job=?"
            args.append(job)
        if unmatched:
            sql += " and job is null"
        if open_only:
            sql += " and dismissed is null"
        return [dict(r) for r in self.q(sql + " order by at, created", args)]

    def flag_counts(self):
        """job -> flags not dismissed yet."""
        return {r[0]: r[1] for r in self.q("select job, count(*) from flags where job is not null and dismissed is null group by job")}

    def dismiss_flags(self, fids=None, job=None):
        now = self.now()
        if fids:
            return self.q(f"update flags set dismissed=? where dismissed is null and fid in ({','.join('?' * len(fids))}) returning fid",
                          (now, *fids))
        if job:
            return self.q("update flags set dismissed=? where dismissed is null and job=? returning fid", (now, job))
        return []

    # ---------------- plays ----------------
    def add_plays(self, player, plays, duration_of):
        """Store plays (once per pid). duration_of(id, base) -> the song's length in seconds or None. Returns how many
        of these pids are stored now (new or not)."""
        now = self.now()
        with self.lock:
            for p in plays:
                d = duration_of(p["id"], p["base"])
                counted = p["result"] == COUNTED or (p["result"] == "stopped" and d and p["seconds"] > d / 2)
                self.db.execute("insert or ignore into plays (pid, player, song_id, base, at, seconds, result, singers, style, party, "
                                "uid, counted, created) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (p["pid"], player, p["id"], p["base"], int(p["at"]), int(p["seconds"]), p["result"],
                                 int(p.get("singers") or 1), p.get("style"), p.get("party") or "", p.get("uid"),
                                 1 if counted else 0, now))
            self.db.commit()
            pids = [p["pid"] for p in plays]
            return self.db.execute(f"select count(*) from plays where pid in ({','.join('?' * len(pids))})", pids).fetchone()[0]

    def counted_plays(self):
        """(song id, base, last time, count) of the plays that count (Encore), grouped by song and version."""
        return [tuple(r) for r in self.q("select song_id, base, max(at), count(*) from plays where counted=1 group by song_id, base")]


# ---------------- PiKaraoke's play history ----------------
def pk_plays(db_path):
    """(None, song title, last time, count) per song title from PiKaraoke's database (read-only); [] without one.
    Studio songs are listed there by their file name ("Artist - Title (Music Video)" = the catalog's base)."""
    if not db_path or not os.path.exists(db_path):
        return []
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = c.execute("select song_title, max(played_at), count(*) from plays where performer != 'claude-test' "
                         "group by song_title").fetchall()
    finally:
        c.close()
    return [(None, t, pk_time(last), n) for t, last, n in rows]


def pk_time(s):
    """PiKaraoke's played_at ("YYYY-MM-DD HH:MM:SS", UTC) -> unix time."""
    import calendar
    try:
        return calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return None


def play_rows(store=None):
    """Every counted play, from Encore and PiKaraoke: [(song id or None, base/title, last time, count)]."""
    rows = []
    try:
        rows += (store or Store()).counted_plays()
    except Exception:
        pass
    try:
        rows += pk_plays(C.PIKARAOKE_DB)
    except Exception:
        pass
    return rows


# ---------------- flags: which line, and the ⚠ note ----------------
def _words_key(text):
    return re.sub(r"[\W_]+", " ", (text or "").casefold()).strip()


def map_line(cur, t=None, line=None, pub=None):
    """The current line (index into cur) a flag is about, or None.
    cur: [(start, end, text)] of the job's lines now, in video time; pub: the same for the published words file the
    player had (its lines numbered from 1, as the flag's `line`), or None. The published line is looked up by text
    near its time (lines may have been edited, split or moved since), else the line sung at t."""
    if not cur:
        return None
    if line and pub and 1 <= line <= len(pub):
        ps, pe, pt = pub[line - 1]
        same = [i for i, (s, e, x) in enumerate(cur) if _words_key(x) == _words_key(pt)]
        if same:
            i = min(same, key=lambda i: abs(cur[i][0] - ps))
            if abs(cur[i][0] - ps) <= 30:
                return i
        if t is None:
            t = ps
    if t is not None:
        hit = [i for i, (s, e, x) in enumerate(cur) if s - 1.0 <= t <= e + 0.5]
        if hit:
            if line and pub and 1 <= line <= len(pub):
                k = _words_key(pub[line - 1][2])
                same = [i for i in hit if _words_key(cur[i][2]) == k]
                if same:
                    return same[0]
            started = [i for i in hit if cur[i][0] <= t]
            return max(started, key=lambda i: cur[i][0]) if started else min(hit, key=lambda i: cur[i][0])
        gap = lambda i: cur[i][0] - t if t < cur[i][0] else t - cur[i][1]
        i = min(range(len(cur)), key=gap)
        if gap(i) <= 8.0:
            return i
    if line and not pub and 1 <= line <= len(cur):
        return line - 1
    return None


ENCORE_TAG = "Encore"
REASON_SEP = " — "        # between Encore's part of a line's ⚠ note and the reason that was there before it
CLAUDE_SEP = " · Claude: "  # ks note's separator (kept after everything)
WHY_MAX = 160


def flag_label(f):
    who = ", ".join(x for x in (f.get("by_name") or f.get("by"), f.get("party")) if x)
    return f"{f['kind']} ({who})" if who else f["kind"]


def encore_note(flags):
    """'Encore: late (Sam, Friday at Ken's)'; several: 'Encore ×3: late (...), early (...)'."""
    if not flags:
        return ""
    head = f"{ENCORE_TAG}: " if len(flags) == 1 else f"{ENCORE_TAG} ×{len(flags)}: "
    return head + ", ".join(flag_label(f) for f in flags)


def merge_why(old, flags):
    """A line's ⚠ note with Encore's part (first) replaced by these flags', keeping the studio's earlier reason and a
    ks note. At most 160 characters (what words.json keeps); Encore's part is shortened first."""
    old = old or ""
    base, _, claude = old.partition(CLAUDE_SEP)
    if base.startswith(ENCORE_TAG):
        base = base.partition(REASON_SEP)[2]
    mine = encore_note(flags)
    if not mine:
        return ((base or "") + (CLAUDE_SEP + claude if claude else ""))[:WHY_MAX] if (base or claude) else ""
    tail = (REASON_SEP + base if base else "") + (CLAUDE_SEP + claude if claude else "")
    room = WHY_MAX - len(tail)
    if len(mine) > room:
        if room < 24:  # no room for both: Encore's part alone
            tail, room = "", WHY_MAX
        if len(mine) > room:
            mine = mine[:room - 1].rstrip(", ") + "…"
    return (mine + tail)[:WHY_MAX]
