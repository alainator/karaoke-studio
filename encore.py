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
  ready, published. A job that fails, is cancelled or deleted puts it back to open.
- Plays may carry the singing score (§2.4 score); it's kept with the play as sent (the Library shows each song's best
  grade) and nothing depends on it. Profile history events carry theirs inside the event, relayed untouched.
- Party auto-make (§2.2b, automake.py): a request the Studio took (`am` set) answers with auto, stage and eta, and
  ends as published, ready with a reason ("needs a look") or failed with a reason. One it didn't take is a normal
  request (no auto fields), whatever the player asked.
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
GRADES = ("AAA", "AA", "A", "B", "C", "D")   # singing score grades, best first (§2.6 score)

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
create table if not exists profile_events (seq integer primary key autoincrement, eid text unique, uid text, stream text,
    at real, player text, body text, stored real);
create index if not exists profile_events_uid on profile_events (uid);
create table if not exists profile_tombs (uid text primary key, seq integer, at real, merged_into text, player text, stored real);
create table if not exists bundle_have (player text primary key, have text, at real);
"""
PROFILE_PAGE = 500              # events per sync answer (then "more": true)


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
            for c, t in (("am", "text"), ("am_at", "real")):  # party auto-make: taken, and when
                if c not in cols:
                    self.db.execute(f"alter table requests add column {c} {t}")
            if "automake" not in {r[1] for r in self.db.execute("pragma table_info(players)")}:  # "may auto-make"
                self.db.execute("alter table players add column automake integer default 0")
            if "score" not in {r[1] for r in self.db.execute("pragma table_info(plays)")}:  # singing scores (§2.4)
                self.db.execute("alter table plays add column score text")
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

    # ---- sync bundles (§2.7, bundle.py) ----
    def key_hash(self, player):
        """sha256(token) for a linked player (the bundle key's root), or None. Never logged or sent anywhere."""
        if player == PRESET:
            return self.preset_hash
        r = self.q("select token_hash from players where player=?", (player,))
        return r[0]["token_hash"] if r and r[0]["token_hash"] else None

    def set_have(self, player, have):
        self.q("insert or replace into bundle_have (player, have, at) values (?, ?, ?)", (player, json.dumps(have), self.now()))

    def get_have(self, player):
        r = self.q("select have from bundle_have where player=?", (player,))
        return json.loads(r[0]["have"]) if r else None

    def applied_ids(self, player, days=90):
        """Ids from this player the Studio has stored in the last `days` (a bundle's answers.applied)."""
        t = self.now() - days * 86400
        return {"requests": [r[0] for r in self.q("select rid from requests where player=? and created>=?", (player, t))],
                "flags": [r[0] for r in self.q("select fid from flags where player=? and created>=?", (player, t))],
                "plays": [r[0] for r in self.q("select pid from plays where player=? and created>=?", (player, t))]}

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
                        "preset": r["player"] == PRESET, "automake": bool(r["automake"])})
        return out

    def may_automake(self, player):
        r = self.q("select automake from players where player=?", (player,))
        return bool(r and r[0]["automake"])

    def set_automake(self, player, on):
        """The admin's "may auto-make" for one linked player. False if there's no such player."""
        with self.lock:
            n = self.db.execute("update players set automake=? where player=?", (1 if on else 0, player)).rowcount
            self.db.commit()
        return n > 0

    def unlink(self, player):
        """Revoke a player's token (its next call gets 401). The preset can't be unlinked here."""
        if player == PRESET:
            raise PairError(400, "the preset player is set by the server config (ENCORE_TOKEN)")
        with self.lock:
            n = self.db.execute("delete from players where player=?", (player,)).rowcount
            self.db.execute("delete from bundle_have where player=?", (player,))
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

    def set_answer(self, rid, ans):
        """What a repeated POST of this rid answers (the first answer, decided after the request was stored)."""
        self.q("update requests set answer=? where rid=?", (json.dumps(ans), rid))

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
               "reason": r["reason"] if r["status"] in ("declined", "failed") or (r["status"] == "ready" and r.get("am")) else None,
               "updated": round(r["updated"], 3)}
        if r.get("am"):  # taken for auto-make (§2.2b); a request that only asked for it is a normal one
            moving = r["status"] in ("open", "making")
            out.update(auto=True, stage=r.get("stage") if moving else None, eta=r.get("eta") if moving else None)
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

    # ---------------- profiles (§2.6; contract/schemas/profile*.json) ----------------
    # The Studio keeps every profile event once (by eid) with a growing sequence number and who sent it, and relays
    # them to the other players in that order; it doesn't merge them on the way (players merge by (at, eid) as the
    # contract says). A tombstone (profile, deleted: true) erases everything of its uid, here and in the requests,
    # flags and plays (they stay, without the uid), and is stored with a new position so every other player gets it;
    # events for that uid that arrive later are dropped. The cursor is "s<sequence>".
    @staticmethod
    def cursor_seq(since):
        """The sequence number in a cursor (0 = from the start); ValueError for a cursor this Studio didn't give."""
        if since is None:
            return 0
        m = re.fullmatch(r"s(\d{1,15})", str(since))
        if not m:
            raise ValueError("unknown cursor")
        return int(m.group(1))

    def profiles_sync(self, player, events, since):
        """Store a player's events (in order) and answer (events from other players after the cursor, cursor, more)."""
        start = self.cursor_seq(since)
        now = self.now()
        with self.lock:
            if start > self._top():
                raise ValueError("unknown cursor")
            for e in events:
                uid = e["uid"]
                if self.db.execute("select 1 from profile_tombs where uid=?", (uid,)).fetchone():
                    continue  # deleted everywhere: a late event from an offline player is dropped
                if self.db.execute("select 1 from profile_events where eid=?", (e["eid"],)).fetchone():
                    continue  # the same event again: stored once
                if e["stream"] == "profile" and e.get("deleted"):
                    self._erase_uid(uid)
                    tomb = {"eid": e["eid"], "stream": "profile", "uid": uid, "at": e["at"], "deleted": True,
                            **({"merged_into": e["merged_into"]} if e.get("merged_into") else {})}
                    cur = self.db.execute("insert into profile_events (eid, uid, stream, at, player, body, stored) "
                                          "values (?, ?, 'profile', ?, ?, ?, ?)", (e["eid"], uid, e["at"], player, json.dumps(tomb), now))
                    self.db.execute("insert into profile_tombs (uid, seq, at, merged_into, player, stored) values (?, ?, ?, ?, ?, ?)",
                                    (uid, cur.lastrowid, e["at"], e.get("merged_into"), player, now))
                    continue
                self.db.execute("insert into profile_events (eid, uid, stream, at, player, body, stored) values (?, ?, ?, ?, ?, ?, ?)",
                                (e["eid"], uid, e["stream"], e["at"], player, json.dumps(e, ensure_ascii=False), now))
            self.db.commit()
            rows = self.db.execute("select seq, body from profile_events where seq > ? and player != ? order by seq limit ?",
                                   (start, player, PROFILE_PAGE + 1)).fetchall()
            more = len(rows) > PROFILE_PAGE
            rows = rows[:PROFILE_PAGE]
            last = rows[-1]["seq"] if more else self._top()
        out = {"cursor": f"s{max(last, start)}", "events": [json.loads(r["body"]) for r in rows]}
        if more:
            out["more"] = True
        return out

    def _top(self):
        """The highest sequence number ever given (erased events keep theirs used)."""
        r = self.db.execute("select seq from sqlite_sequence where name='profile_events'").fetchone()
        return int(r[0]) if r else 0

    def _erase_uid(self, uid):
        """Delete-everywhere for one profile (call under the lock): its events go, and the requests, flags and plays
        it sent stay without the uid."""
        self.db.execute("delete from profile_events where uid=?", (uid,))
        for t in ("requests", "flags", "plays"):
            self.db.execute(f"update {t} set uid=null where uid=?", (uid,))

    @staticmethod
    def _wins(a, b):
        """Does event a beat event b (latest (at, eid) wins)?"""
        return (a["at"], a["eid"]) > (b["at"], b["eid"])

    def profile_state(self, uid):
        """The merged state of one profile: (deleted, merged_into, profile event or None, favorites, prefs, history,
        players that sent events) or None for a uid this Studio never heard of."""
        with self.lock:
            tomb = self.db.execute("select * from profile_tombs where uid=?", (uid,)).fetchone()
            rows = self.db.execute("select player, body from profile_events where uid=? order by seq", (uid,)).fetchall()
        if not tomb and not rows:
            return None
        prof, favs, prefs, hist, senders = None, {}, {}, [], []
        for r in rows:
            e = json.loads(r["body"])
            if r["player"] not in senders:
                senders.append(r["player"])
            if e["stream"] == "profile" and not e.get("deleted"):
                prof = e if prof is None or self._wins(e, prof) else prof
            elif e["stream"] == "favorites":
                k = e["song"]
                favs[k] = e if k not in favs or self._wins(e, favs[k]) else favs[k]
            elif e["stream"] == "prefs":
                k = e["key"]
                prefs[k] = e if k not in prefs or self._wins(e, prefs[k]) else prefs[k]
            elif e["stream"] == "history":
                hist.append(e)
        if tomb:
            return {"deleted": True, "merged_into": tomb["merged_into"], "profile": None, "favorites": {}, "prefs": {},
                    "history": [], "players": [tomb["player"]], "last": tomb["at"]}
        hist.sort(key=lambda e: (e["at"], e["eid"]))
        last = max([e["at"] for e in [prof] + list(favs.values()) + list(prefs.values()) + hist if e] or [0])
        return {"deleted": False, "merged_into": None, "profile": prof, "favorites": favs, "prefs": prefs,
                "history": hist, "players": senders, "last": last}

    def profile_export(self, uid):
        """GET /api/encore/profiles/<uid>/export: everything kept about one profile (never its PIN hash), or None."""
        st = self.profile_state(uid)
        if st is None:
            return None
        names = self.player_names()
        seen = {r["player"]: r["last_seen"] for r in self.q("select player, last_seen from players")}
        players = [{"player": p, "name": names.get(p) or "an unlinked player", "last_seen": seen.get(p)}
                   for p in st["players"] if UUID_RE.match(p or "")]
        if st["deleted"]:
            return {"uid": uid, "deleted": True, "profile": None, "favorites": [], "prefs": [], "history": [], "players": []}
        p = st["profile"]
        return {"uid": uid, "deleted": False,
                "profile": {"name": p["name"], "color": p["color"], "created": p["created"], "updated": p["at"],
                            "pin": bool(p.get("pin_hash"))} if p else None,
                "favorites": [{"song": e["song"], "on": e["on"], "at": e["at"]} for e in st["favorites"].values()],
                "prefs": [{"key": e["key"], "value": e["value"], "at": e["at"]} for e in st["prefs"].values()],
                "history": st["history"], "players": players}

    def profiles(self):
        """The admin list: name, colour, PIN set or not, last event, which players, deleted or not. No history."""
        with self.lock:
            uids = [r[0] for r in self.db.execute("select distinct uid from profile_events")]
            uids += [r[0] for r in self.db.execute("select uid from profile_tombs") if r[0] not in uids]
        names = self.player_names()
        out = []
        for uid in uids:
            st = self.profile_state(uid)
            if st is None:
                continue
            p = st["profile"]
            out.append({"uid": uid, "name": p["name"] if p else None, "color": p["color"] if p else None,
                        "pin": bool(p and p.get("pin_hash")), "created": p["created"] if p else None,
                        "last": st["last"], "deleted": st["deleted"], "merged_into": st["merged_into"],
                        "players": [names.get(x) or ("Preset player" if x == PRESET else "an unlinked player") for x in st["players"]]})
        out.sort(key=lambda r: (r["deleted"], -(r["last"] or 0)))
        return out

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
                                "uid, counted, created, score) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (p["pid"], player, p["id"], p["base"], int(p["at"]), int(p["seconds"]), p["result"],
                                 int(p.get("singers") or 1), p.get("style"), p.get("party") or "", p.get("uid"),
                                 1 if counted else 0, now, json.dumps(p["score"], separators=(",", ":")) if p.get("score") else None))
            self.db.commit()
            pids = [p["pid"] for p in plays]
            return self.db.execute(f"select count(*) from plays where pid in ({','.join('?' * len(pids))})", pids).fetchone()[0]

    def play_score(self, pid):
        """The singing score a play came with (§2.4), as sent, or None."""
        r = self.q("select score from plays where pid=?", (pid,))
        return json.loads(r[0][0]) if r and r[0][0] else None

    def best_grades(self):
        """base -> the best grade any scored play of it got (AAA best), for the Library."""
        best = {}
        for base, raw in self.q("select base, score from plays where score is not null"):
            try:
                g = json.loads(raw).get("grade")
            except (ValueError, AttributeError):
                continue
            if g in GRADES and (base not in best or GRADES.index(g) < GRADES.index(best[base])):
                best[base] = g
        return best

    def counted_plays(self):
        """(song id, base, last time, count) of the plays that count (Encore), grouped by song and version."""
        return [tuple(r) for r in self.q("select song_id, base, max(at), count(*) from plays where counted=1 group by song_id, base")]


def play_rows(store=None):
    """Every counted play (Encore's): [(song id or None, base/title, last time, count)]."""
    try:
        return (store or Store()).counted_plays()
    except Exception:
        return []


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
