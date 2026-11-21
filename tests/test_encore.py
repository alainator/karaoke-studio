"""The player API (contract/ENCORE-STUDIO.md): pairing and tokens, every /api/encore/ answer against the contract's
schemas, idempotency, request statuses following songs, lyric flags on lines (a version too), plays, the files a
player may fetch, catalog v2 and pitch files, PiKaraoke left out, and the contract fixtures being what the studio's own
code makes. Calls go through the whole ASGI app (tests/_asgi.py): middleware, auth, error shapes, Range, ETag.
No GPU or network (the pitch tracker gets a made-up melody, except one CPU test on a short sine tone)."""
import json, os, shutil, sqlite3, subprocess, sys, tempfile, time, unittest, uuid

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import app as A  # noqa: E402
import catalog as CAT  # noqa: E402
import encore as E  # noqa: E402
import pitch as P  # noqa: E402
import schemalite as S  # noqa: E402
import numpy as np  # noqa: E402
from assgen import load_lines, save_lines  # noqa: E402
from tests._asgi import call, jcall  # noqa: E402

PITCHED = []
A.pitch_later = lambda jid: PITCHED.append(jid)   # pitch files are written by the tests themselves (A.write_pitch)
A.catalog_soon = lambda delay=0: A.update_catalog()
A.PK_URL = ""                                      # no PiKaraoke here (a test turns it on)
PRESET = os.environ["ENCORE_TOKEN"]
SYLL = ["la", "da", "na", "doo", "ba", "oh"]
_n = [0]


def nxt():
    _n[0] += 1
    return _n[0]


def addr():
    k = nxt()
    return (f"192.0.2.{10 + k % 200}", 41000 + k)


def lines_of(spec):
    out = []
    for i, (start, n) in enumerate(spec):
        t, words = start, []
        for k in range(n):
            words.append([round(t, 2), round(t + 0.42, 2), SYLL[(i + k) % 6] + str(i)])
            t += 0.5
        out.append({"words": words})
    return out


def melody(lines, duration, off=0.0):
    n = int(round(duration / P.HOP))
    f0, per = np.zeros(n), np.full(n, 0.05)
    for li, l in enumerate(lines):
        for k, (a, b, _) in enumerate(l["words"]):
            m = 57 + (li * 2 + k) % 9
            f0[int(round((a + off) / P.HOP)):int(round((b + off) / P.HOP))] = 440.0 * 2 ** ((m - 69) / 12)
            per[int(round((a + off) / P.HOP)):int(round((b + off) / P.HOP))] = 0.9
    return f0, per


def make_song(artist, title, spec=((10.0, 4), (14.0, 4), (18.5, 5)), status="done", publish=True, offset=0.0, version=None,
              duration=30.0, cache=True):
    k = nxt()
    jid = f"20260202-{k:06d}-enc-{k}"
    d = os.path.join(A.JOBS, jid)
    os.makedirs(d)
    lines = lines_of(spec)
    save_lines(os.path.join(d, "words.json"), lines, [])
    for f in ("out.mp4", "vocals.m4a"):
        open(os.path.join(d, f), "wb").write(b"placeholder " * 10)
    if cache:
        f0, per = melody(lines, duration, offset)
        np.savez_compressed(os.path.join(d, P.CACHE), f0=f0.astype(np.float32), per=per.astype(np.float16),
                            hop=np.float32(P.HOP), stamp=np.array(P._stamp(os.path.join(d, "vocals.m4a"))), model=np.array("test"))
    result = {"offset": 0.0, "lines": len(lines), "duration": duration, "lang": "en", "width": 1280, "height": 720}
    job = {"id": jid, "yt": "aaaaaaaaaaa", "artist": artist, "title": title, "status": status, "step": status,
           "created": time.time() + k, "finished": time.time(), "offset": offset, "result": result, "log": []}
    A.save(jid, job)
    if status == "done":
        A.rebuild_lyrics(jid, job)
    if publish:
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/publish", {"remove": []})
        assert st == 200, b
        A.write_pitch(jid)
    if version:
        slug = "v" + str(k)
        vd = os.path.join(d, "versions", slug)
        os.makedirs(vd)
        open(os.path.join(vd, "out.mp4"), "wb").write(b"version " * 10)
        json.dump({"name": version, "mode": "version", "slug": slug, "start": 3.0, "offset": 3.0, "trim_start": 0.0,
                   "aligned": "manual", "made_at": time.time()}, open(os.path.join(vd, "meta.json"), "w"))
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/videos/{slug}/publish", {})
        assert st == 200, b
        A.write_pitch(jid)
    return jid


def pair(player=None, name="Test kit", client=None):
    code, _ = A.STORE.new_code()
    st, _, b = jcall(A.app, "POST", "/api/encore/pair", {"code": code, "name": name, "player": player or str(uuid.uuid4()),
                                                         "encore": "0.4", "contract": "1"}, client=client or addr())
    assert st == 200, b
    return b["token"]


def api(method, path, body=None, token=PRESET, headers=None, **kw):
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    return jcall(A.app, method, path, body, h, **kw)


def rid():
    return str(uuid.uuid4())


def req_body(artist, title, **kw):
    return {"rid": kw.pop("rid", None) or rid(), "artist": artist, "title": title, "note": "", "by": "Mia", "party": "Test party",
            "at": int(time.time()), **kw}


class Base(unittest.TestCase):
    def valid(self, schema, data):
        self.assertEqual(S.errors(schema, data), [], json.dumps(data)[:400])


# ---------------- the schema checker itself ----------------
class SchemaChecker(Base):
    def test_every_schema_loads_and_uses_known_keywords(self):
        names = sorted(os.listdir(S.SCHEMAS))
        self.assertGreaterEqual(len(names), 17)
        for n in names:
            S.errors(n, {})  # raises on a keyword it doesn't implement

    def test_negative_cases(self):
        u = str(uuid.uuid4())
        bad = {
            "pair.request.schema.json": {"code": "abc", "name": "x", "player": u, "encore": "1", "contract": "1"},
            "pair.response.schema.json": {"token": "short", "studio": "1", "contract": "1", "studio_name": "x", "studio_id": u},
            "hello.response.schema.json": {"api": 2, "contract": "1", "studio": "x", "catalog_version": 2, "studio_id": u},
            "requests.post.request.schema.json": {"rid": "nope", "artist": "", "title": "t", "note": "", "by": "b", "party": "", "at": 1},
            "requests.post.response.schema.json": {"rid": u, "status": "have", "id": None, "base": None},
            "requests.get.response.schema.json": {"requests": [{"rid": u, "status": "failed", "job": None, "id": None, "base": None,
                                                                "reason": None, "updated": 1}]},
            "flags.post.request.schema.json": {"fid": u, "id": "x", "base": "b", "t": -1, "line": 0, "kind": "late", "note": "",
                                               "by": "b", "at": 1},
            "flags.post.response.schema.json": {"fid": u, "status": "fine"},
            "plays.post.request.schema.json": {"plays": []},
            "plays.post.response.schema.json": {"stored": -1},
            "unpublished.get.response.schema.json": {"songs": [], "studio_url": "ftp://x"},
            "unpair.request.schema.json": {"x": 1},
            "unpair.response.schema.json": {"ok": False},
            "error.schema.json": {"detail": "x"},
            "pitch.schema.json": {"version": 1, "base": "b", "duration": 1, "hop": 0, "notes": [[1, 2, 200]], "range": None},
            "words.schema.json": {"version": 1, "base": "b", "parts": [], "lines": [{"i": 0, "start": 0, "end": 1, "words": [], "parts": []}]},
            "catalog.schema.json": {"version": 2, "songs": [{"id": "x"}]},
            # §2.6 profiles; the profile one also checks "not" (merged_into only on a tombstone)
            "profile-event.schema.json": {"eid": u, "stream": "profile", "uid": u, "at": 1, "name": "x", "color": "#000000",
                                          "pin_hash": None, "created": 1, "deleted": False, "merged_into": u},
            "profiles.sync.request.schema.json": {"player": u, "since": None, "events": [{"eid": u, "stream": "prefs", "uid": u, "at": 1,
                                                                                         "key": "Bad Key", "value": 1}]},
            "profiles.sync.response.schema.json": {"cursor": "", "events": []},
            "profiles.export.response.schema.json": {"uid": u, "deleted": False, "profile": {"name": "x", "color": "#000000",
                                                     "created": 1, "pin": "yes"}, "favorites": [], "prefs": [], "history": []},
        }
        self.assertEqual(sorted(bad), sorted(os.listdir(S.SCHEMAS)))
        for n, d in bad.items():
            with self.subTest(schema=n):
                self.assertTrue(S.errors(n, d))

    def test_unknown_keyword_raises(self):
        with self.assertRaises(ValueError):
            S.errors({"type": "string", "format": "email"}, "x")


# ---------------- pairing and tokens ----------------
class Pairing(Base):
    def test_pair_hello_and_token_stored_hashed(self):
        player = str(uuid.uuid4())
        code, exp = A.STORE.new_code()
        self.assertEqual(len(code), 8)
        self.assertFalse(set(code) & set("01OIL"))
        self.assertAlmostEqual(exp - time.time(), 600, delta=5)
        body = {"code": code[:4].lower() + "-" + code[4:], "name": "Ken's kit", "player": player, "encore": "0.4", "contract": "1"}
        st, _, b = jcall(A.app, "POST", "/api/encore/pair", body, client=addr())
        self.assertEqual(st, 422)  # Encore sends it upper-case without the dash (the schema says so)
        st, _, b = jcall(A.app, "POST", "/api/encore/pair", dict(body, code=code), client=addr())
        self.assertEqual(st, 200, b)
        self.valid("pair.response.schema.json", b)
        self.assertGreaterEqual(len(b["token"]), 40)
        self.assertEqual(b["studio_id"], A.STORE.studio_id())
        db = open(A.STORE.path, "rb").read()
        self.assertNotIn(b["token"].encode(), db)
        self.assertNotIn(code.encode(), db)
        st, _, h = api("GET", "/api/encore/hello", token=b["token"])
        self.assertEqual(st, 200)
        self.valid("hello.response.schema.json", h)
        self.assertEqual((h["contract"], h["catalog_version"], h["studio_id"]), (E.CONTRACT, 2, b["studio_id"]))
        p = next(x for x in A.STORE.players() if x["player"] == player)
        self.assertEqual((p["name"], p["encore"], p["contract"]), ("Ken's kit", "0.4", "1"))
        # one use
        st, _, e = jcall(A.app, "POST", "/api/encore/pair", dict(body, code=code), client=addr())
        self.assertEqual(st, 403)
        self.valid("error.schema.json", e)

    def test_wrong_and_expired_codes(self):
        code, _ = A.STORE.new_code()
        body = {"code": "ABCDEFGH", "name": "x", "player": str(uuid.uuid4()), "encore": "0.4", "contract": "1"}
        for _ in range(4):
            self.assertEqual(jcall(A.app, "POST", "/api/encore/pair", body, client=addr())[0], 403)
        self.assertEqual(jcall(A.app, "POST", "/api/encore/pair", dict(body, code=code), client=addr())[0], 200)  # 4 wrong: still good
        code, _ = A.STORE.new_code()
        for _ in range(5):
            self.assertEqual(jcall(A.app, "POST", "/api/encore/pair", body, client=addr())[0], 403)
        self.assertEqual(jcall(A.app, "POST", "/api/encore/pair", dict(body, code=code), client=addr())[0], 403)  # dead after 5
        code, _ = A.STORE.new_code()
        A.STORE.code["expires"] = time.time() - 1
        self.assertEqual(jcall(A.app, "POST", "/api/encore/pair", dict(body, code=code), client=addr())[0], 403)
        code, _ = A.STORE.new_code()
        A.STORE.new_code()  # a new code replaces it
        self.assertEqual(jcall(A.app, "POST", "/api/encore/pair", dict(body, code=code), client=addr())[0], 403)

    def test_rate_limit_per_address(self):
        here, other = ("192.0.2.250", 1), ("192.0.2.251", 1)
        body = {"code": "ABCDEFGH", "name": "x", "player": str(uuid.uuid4()), "encore": "0.4", "contract": "1"}
        A.STORE.new_code()
        for _ in range(5):
            self.assertEqual(jcall(A.app, "POST", "/api/encore/pair", body, client=here)[0], 403)
        st, h, b = jcall(A.app, "POST", "/api/encore/pair", body, client=here)
        self.assertEqual(st, 429)
        self.valid("error.schema.json", b)
        code, _ = A.STORE.new_code()
        self.assertEqual(jcall(A.app, "POST", "/api/encore/pair", dict(body, code=code), client=here)[0], 429)  # even the right code
        self.assertEqual(jcall(A.app, "POST", "/api/encore/pair", dict(body, code=code), client=other)[0], 200)

    def test_repair_replaces_token_unlink_and_unpair(self):
        player = str(uuid.uuid4())
        t1 = pair(player)
        t2 = pair(player, name="Renamed kit")
        self.assertEqual(api("GET", "/api/encore/hello", token=t1)[0], 401)
        self.assertEqual(api("GET", "/api/encore/hello", token=t2)[0], 200)
        self.assertEqual(sum(1 for p in A.STORE.players() if p["player"] == player), 1)
        st, _, b = jcall(A.app, "DELETE", f"/api/players/{player}")
        self.assertEqual((st, b["ok"]), (200, True))
        st, _, e = api("GET", "/api/encore/hello", token=t2)
        self.assertEqual(st, 401)
        self.valid("error.schema.json", e)
        t3 = pair()
        st, _, b = api("POST", "/api/encore/unpair", {}, token=t3)
        self.assertEqual(st, 200)
        self.valid("unpair.response.schema.json", b)
        self.assertEqual(api("POST", "/api/encore/unpair", {}, token=t3)[0], 401)

    def test_preset_player(self):
        st, _, h = api("GET", "/api/encore/hello", token=PRESET)
        self.assertEqual(st, 200)
        p = next(x for x in A.STORE.players() if x["player"] == E.PRESET)
        self.assertTrue(p["preset"])
        fresh = E.Store(os.path.join(_env.ROOT, "fresh.db"), preset_token="x" * 40)
        self.assertEqual([(x["player"], x["last_seen"]) for x in fresh.players()], [(E.PRESET, None)])  # listed before it calls
        self.assertEqual(jcall(A.app, "DELETE", f"/api/players/{E.PRESET}")[0], 400)
        self.assertEqual(api("POST", "/api/encore/unpair", {}, token=PRESET)[0], 400)
        self.assertEqual(api("GET", "/api/encore/hello", token=PRESET)[0], 200)

    def test_bad_tokens_401_then_429_and_no_bearer(self):
        who = ("192.0.2.252", 1)
        self.assertEqual(api("GET", "/api/encore/hello", token=None, client=who)[0], 401)
        for _ in range(19):
            self.assertEqual(api("GET", "/api/encore/hello", token="nope-" + "x" * 40, client=who)[0], 401)
        self.assertEqual(api("GET", "/api/encore/hello", token=PRESET, client=who)[0], 429)  # this address waits a minute
        self.assertEqual(api("GET", "/api/encore/hello", token=PRESET, client=("192.0.2.253", 1))[0], 200)

    def test_backlog_and_version_reported(self):
        player = str(uuid.uuid4())
        t = pair(player)
        api("GET", "/api/encore/hello", token=t, headers={"X-Encore-Outbox": "7", "User-Agent": "Encore/0.5 contract/1"})
        p = next(x for x in A.STORE.players() if x["player"] == player)
        self.assertEqual((p["backlog"], p["encore"]), (7, "0.5"))
        st, _, lst = jcall(A.app, "GET", "/api/players")
        self.assertTrue(any(x["player"] == player for x in lst["players"]))

    def test_no_codes_or_tokens_in_the_log(self):
        code, _ = A.STORE.new_code()
        st, _, b = jcall(A.app, "POST", "/api/encore/pair", {"code": code, "name": "Log kit", "player": str(uuid.uuid4()),
                                                             "encore": "0.4", "contract": "1"}, client=addr())
        st2, _, c = jcall(A.app, "POST", "/api/players/code")
        for h in A.log.handlers:
            h.flush()
        text = open(os.path.join(A.C.LOGS, "studio.log"), encoding="utf-8").read()
        for secret in (code, b["token"], c["code"], PRESET):
            self.assertNotIn(secret, text)

    def test_bodies_and_errors(self):
        self.assertEqual(api("POST", "/api/encore/requests", b"x" * (65 * 1024))[0], 413)
        st, _, b = api("POST", "/api/encore/requests", b"{not json")
        self.assertEqual(st, 400)
        self.valid("error.schema.json", b)
        st, _, b = api("POST", "/api/encore/requests", {"rid": "x"})
        self.assertEqual(st, 422)
        st, _, b = api("GET", "/api/encore/nothing-here")
        self.assertEqual(st, 404)
        self.valid("error.schema.json", b)
        self.assertEqual(jcall(A.app, "GET", "/api/jobs/nope")[2], {"detail": "no such job"})  # the web app keeps its own shape

    def test_only_player_api_on_the_link_network(self):
        import ipaddress
        A._ONLY_NETS[:] = [ipaddress.ip_network("192.0.2.0/24")]
        try:
            self.assertEqual(call(A.app, "GET", "/api/status", server=("192.0.2.1", 5570))[0], 403)
            self.assertEqual(call(A.app, "GET", "/api/players", server=("192.0.2.1", 5570))[0], 403)
            self.assertEqual(api("GET", "/api/encore/hello", server=("192.0.2.1", 5570))[0], 200)
            self.assertEqual(call(A.app, "GET", "/api/players", server=("198.51.100.1", 5570))[0], 200)
        finally:
            A._ONLY_NETS[:] = []


# ---------------- requests ----------------
class Requests(Base):
    @classmethod
    def setUpClass(cls):
        cls.pub = make_song("Sigur Rós", "Hoppípolla Æ")
        cls.made = make_song("Ready Artist", "Ready Song", publish=False)
        cls.making = make_song("Making Artist", "Making Song", status="running", publish=False)
        cls.tok = pair(name="Requests kit")

    def post(self, body, token=None):
        st, _, b = api("POST", "/api/encore/requests", body, token=token or self.tok)
        self.assertEqual(st, 200, b)
        self.valid("requests.post.response.schema.json", b)
        return b

    def get(self, q, token=None):
        st, _, b = api("GET", "/api/encore/requests?" + q, token=token or self.tok)
        self.assertEqual(st, 200, b)
        self.valid("requests.get.response.schema.json", b)
        return {r["rid"]: r for r in b["requests"]}

    def test_arrival_matching(self):
        a = self.post(req_body("sigur ros", "HOPPIPOLLA Æ"))
        self.assertEqual((a["status"], a["id"]), ("have", self.pub))
        b = self.post(req_body("Hoppipolla æ", "Sigur Ros"))  # names the other way round
        self.assertEqual(b["status"], "have")
        c = self.post(req_body("", "hoppípolla æ"))  # no artist: one artist has that title
        self.assertEqual(c["status"], "have")
        d = self.post(req_body("Ready Artist", "Ready Song"))
        e = self.post(req_body("Making Artist", "Making Song"))
        self.assertEqual((d["status"], e["status"]), ("open", "open"))  # the POST answer is open or have (§2.2)...
        got = self.get("rids=" + ",".join([d["rid"], e["rid"]]))
        self.assertEqual((got[d["rid"]]["status"], got[d["rid"]]["job"]), ("ready", self.made))  # ...the polls say more
        self.assertEqual((got[e["rid"]]["status"], got[e["rid"]]["job"]), ("making", self.making))
        self.assertIn(d["rid"], A.load(self.made)["encore_rids"])

    def test_title_alone_sung_by_several_stays_open(self):
        make_song("Adele", "Hello Again")
        make_song("Lionel Richie", "Hello Again")
        self.assertEqual(self.post(req_body("", "Hello Again"))["status"], "open")
        self.assertEqual(self.post(req_body("adele", "hello again"))["status"], "have")

    def test_idempotent_and_per_player(self):
        body = req_body("Nobody", "Unknown Song", note="the live one")
        a = self.post(body)
        b = self.post(dict(body, title="Changed"))
        self.assertEqual(a, b)
        self.assertEqual(A.STORE.q("select count(*) from requests where rid=?", (body["rid"],))[0][0], 1)
        other = pair(name="Other kit")
        self.assertEqual(self.get("rids=" + body["rid"], token=other), {})  # another player's request isn't listed
        self.assertEqual(self.get("rids=" + body["rid"] + ",not-a-uuid," + rid()).keys(), {body["rid"]})
        self.assertEqual(api("GET", "/api/encore/requests", token=self.tok)[0], 400)

    def test_lifecycle_find_and_make_to_published(self):
        body = req_body("Lumen Coast", "Glass Orchard")
        self.assertEqual(self.post(body)["status"], "open")
        t0 = time.time() - 1
        st, _, b = jcall(A.app, "POST", "/api/jobs", {"yt": "bbbbbbbbbbb", "lrc_id": 2, "lyrics_source": "lrclib", "artist": "Lumen Coast",
                                                     "title": "Glass Orchard", "request": body["rid"]})
        self.assertEqual(st, 200, b)
        jid = b["id"]
        try:
            self.assertEqual(A.load(jid)["encore_rids"], [body["rid"]])
            r = self.get("rids=" + body["rid"])[body["rid"]]
            self.assertEqual((r["status"], r["job"]), ("making", jid))
            self.assertIn(body["rid"], self.get(f"since={t0}"))
            # it fails: back to open; retried: making again
            A.set_job(jid, status="failed", error="x")
            self.assertEqual(self.get("rids=" + body["rid"])[body["rid"]]["status"], "open")
            A.set_job(jid, status="running")
            self.assertEqual(self.get("rids=" + body["rid"])[body["rid"]]["status"], "making")
            # made in place of itself ("Change video or lyrics"): the new song takes the request along
            time.sleep(1.05)  # (song ids are made of the time to the second and the name)
            st, _, b2 = jcall(A.app, "POST", "/api/jobs", {"yt": "ccccccccccc", "lrc_id": 3, "lyrics_source": "lrclib",
                                                          "artist": "Lumen Coast", "title": "Glass Orchard", "replace": jid})
            new = b2["id"]
            self.assertEqual(A.load(new)["encore_rids"], [body["rid"]])
            A.set_job(jid, status="failed")
            shutil.rmtree(os.path.join(A.JOBS, jid))
            r = self.get("rids=" + body["rid"])[body["rid"]]
            self.assertEqual((r["status"], r["job"]), ("making", new))
            # made, then published
            d = os.path.join(A.JOBS, new)
            save_lines(os.path.join(d, "words.json"), lines_of([(5.0, 3)]), [])
            for f in ("out.mp4", "vocals.m4a"):
                open(os.path.join(d, f), "wb").write(b"x" * 50)
            A.set_job(new, status="done", result={"duration": 20.0})
            self.assertEqual(self.get("rids=" + body["rid"])[body["rid"]]["status"], "ready")
            self.assertEqual(jcall(A.app, "POST", f"/api/jobs/{new}/publish", {"remove": []})[0], 200)
            r = self.get("rids=" + body["rid"])[body["rid"]]
            self.assertEqual((r["status"], r["id"], r["base"]), ("published", new, "Lumen Coast - Glass Orchard (Music Video)"))
        finally:
            for j in (jid, locals().get("new")):
                if j and os.path.isdir(os.path.join(A.JOBS, j)):
                    A.set_job(j, status="failed")

    def test_admin_decline_have_reopen_and_list(self):
        a = self.post(req_body("Iris Vale", "Paper Boats"))
        self.assertEqual(jcall(A.app, "POST", f"/api/requests/{a['rid']}/decline", {"reason": ""})[0], 400)
        self.assertEqual(jcall(A.app, "POST", f"/api/requests/{a['rid']}/decline", {"reason": "No usable video yet"})[0], 200)
        r = self.get("rids=" + a["rid"])[a["rid"]]
        self.assertEqual((r["status"], r["reason"]), ("declined", "No usable video yet"))
        jcall(A.app, "POST", f"/api/requests/{a['rid']}/reopen", {})
        self.assertEqual(self.get("rids=" + a["rid"])[a["rid"]]["status"], "open")
        jcall(A.app, "POST", f"/api/requests/{a['rid']}/have", {"job": self.pub})
        r = self.get("rids=" + a["rid"])[a["rid"]]
        self.assertEqual((r["status"], r["id"]), ("have", self.pub))
        st, _, lst = jcall(A.app, "GET", "/api/requests")
        row = next(x for x in lst["requests"] if x["rid"] == a["rid"])
        self.assertEqual((row["by"], row["party"], row["player"], row["song"]["id"]), ("Mia", "Test party", "Requests kit", self.pub))
        self.assertEqual(lst["open"], jcall(A.app, "GET", "/api/status")[2]["requests_open"])

    def test_reopen_and_not_this_song_stay_open(self):
        a = self.post(req_body("Sigur Ros", "Hoppipolla Æ"))
        self.assertEqual(a["status"], "have")
        jcall(A.app, "POST", f"/api/requests/{a['rid']}/reopen", {})
        self.assertEqual(self.get("rids=" + a["rid"])[a["rid"]]["status"], "open")   # not matched by its name again
        self.assertEqual(self.get("rids=" + a["rid"])[a["rid"]]["status"], "open")
        b = self.post(req_body("Ready Artist", "Ready Song"))
        self.assertEqual(self.get("rids=" + b["rid"])[b["rid"]]["status"], "ready")
        jcall(A.app, "POST", f"/api/requests/{b['rid']}/reopen", {})                  # "Not this song"
        self.assertEqual(self.get("rids=" + b["rid"])[b["rid"]]["status"], "open")
        self.assertNotIn(b["rid"], A.load(self.made).get("encore_rids") or [])
        jcall(A.app, "POST", f"/api/requests/{b['rid']}/have", {"job": self.made})    # the admin picks it after all
        self.assertEqual(self.get("rids=" + b["rid"])[b["rid"]]["status"], "ready")

    def test_auto_fields_are_kept(self):
        body = req_body("June Arbor", "Copper Moon", auto=True, queue={"eid": "q1", "uid": "u1"})
        self.assertEqual(self.post(body)["status"], "open")
        row = A.STORE.get_request(body["rid"])
        self.assertEqual((row["auto"], json.loads(row["queue"])), (1, {"eid": "q1", "uid": "u1"}))
        r = self.get("rids=" + body["rid"])[body["rid"]]
        self.assertEqual((r["auto"], r["stage"], r["eta"]), (True, None, None))


# ---------------- flags ----------------
class Flags(Base):
    @classmethod
    def setUpClass(cls):
        cls.tok = pair(name="Flags kit")
        cls.song = make_song("Flag Artist", "Flag Song", offset=0.5, version="Mia's version")
        cls.base = A.load(cls.song)["published_as"]
        cls.vbase = cls.base.replace("(Music Video)", "(Mia's version)")

    def flag(self, **kw):
        body = {"fid": str(uuid.uuid4()), "id": self.song, "base": self.base, "t": None, "line": None, "kind": "late", "note": "",
                "by": "Sam", "party": "Friday", "at": int(time.time())}
        body.update(kw)
        st, _, b = api("POST", "/api/encore/flags", body, token=self.tok)
        self.assertEqual(st, 200, b)
        self.valid("flags.post.response.schema.json", b)
        return body, b

    def lines(self):
        return load_lines(A.job_path(self.song, "words.json"))

    def test_marks_line_by_time_on_the_version_and_stacks(self):
        # line 2 (index 1) is sung 14.0-15.92 in the job's words, 14.5-16.42 in video time (offset +0.5); the version
        # has the song's timing, so t on it is the same
        before = self.lines()[1]["words"]
        _, a = self.flag(base=self.vbase, t=15.0, line=None, kind="late", by="Sam", party="Friday at Ken's")
        self.assertEqual(a["status"], "marked")
        l = self.lines()[1]
        self.assertEqual((l["weak"], l["why"]), (True, "Encore: late (Sam, Friday at Ken's)"))
        self.assertEqual(l["words"], before)  # timing and words untouched
        f = A.STORE.get_flag(a["fid"])
        self.assertEqual((f["mapped"], f["version"]), (1, "Mia's version"))
        self.flag(t=15.2, line=2, kind="words", by="Mia", party="")
        self.assertEqual(self.lines()[1]["why"], "Encore ×2: late (Sam, Friday at Ken's), words (Mia)")
        self.assertEqual(A.to_check(self.song), 1)
        s = jcall(A.app, "GET", f"/api/jobs/{self.song}")[2]
        self.assertGreaterEqual(s["encore_flags"], 2)
        brief = next(j for j in jcall(A.app, "GET", "/api/jobs?brief=1")[2] if j["id"] == self.song)
        self.assertGreaterEqual(brief["encore_flags"], 2)
        # the studio's own reason and a ks note stay; dismissing takes only Encore's part off
        ls = self.lines()
        ls[1]["why"] = ls[1]["why"] + " — heard 2 s later · Claude: check the chorus"
        save_lines(A.job_path(self.song, "words.json"), ls)
        self.flag(t=15.0, kind="early", by="Jo", party="P")
        why = self.lines()[1]["why"]
        self.assertTrue(why.startswith("Encore ×3: "), why)
        self.assertTrue(why.endswith(" — heard 2 s later · Claude: check the chorus"), why)
        self.assertLessEqual(len(why), 160)
        st, _, d = jcall(A.app, "POST", f"/api/jobs/{self.song}/encore-flags/dismiss", {})
        self.assertEqual(st, 200)
        l = self.lines()[1]
        self.assertEqual((l["weak"], l["why"]), (True, "heard 2 s later · Claude: check the chorus"))
        self.assertEqual(jcall(A.app, "GET", f"/api/jobs/{self.song}")[2]["encore_flags"], 0)

    def test_line_number_from_the_published_words_after_edits(self):
        # the player numbers lines as in the published words file; a line inserted in the studio since then must not
        # shift the mark to the wrong line
        pub = json.load(open(os.path.join(A.MV_DIR, self.base + ".words.json")))["lines"]
        ls = self.lines()
        ls.insert(0, {"words": [[2.0, 2.4, "new"], [2.5, 2.9, "line"]]})
        save_lines(A.job_path(self.song, "words.json"), ls)
        try:
            _, a = self.flag(t=None, line=3, kind="missing")
            self.assertEqual(A.STORE.get_flag(a["fid"])["mapped"], 3)  # published line 3 is line 4 now
            self.assertEqual(" ".join(w[2] for w in self.lines()[3]["words"]), " ".join(w[2] for w in pub[2]["words"]))
        finally:
            jcall(A.app, "POST", f"/api/jobs/{self.song}/encore-flags/dismiss", {})
            ls = self.lines()[1:]
            save_lines(A.job_path(self.song, "words.json"), ls)

    def test_whole_song_noted_unknown_and_idempotent(self):
        body, a = self.flag(t=None, line=None, kind="other", note="the whole thing")
        self.assertEqual(a["status"], "marked")
        self.assertIsNone(A.STORE.get_flag(a["fid"])["mapped"])
        again = api("POST", "/api/encore/flags", dict(body, kind="early"), token=self.tok)[2]
        self.assertEqual(again, a)
        self.assertEqual(A.STORE.q("select count(*) from flags where fid=?", (body["fid"],))[0][0], 1)
        # a scanned song ("f-" id) the studio knows by base
        _, b = self.flag(id="f-0123456789ab", t=11.0)
        self.assertEqual(b["status"], "marked")
        # unknown everywhere
        _, c = self.flag(id="f-ffffffffffff", base="Someone - Something (Music Video)", t=40.0)
        self.assertEqual(c["status"], "unknown_song")
        # known to the catalog, its job deleted: noted (kept in the Requests page's list)
        gone = make_song("Gone Artist", "Gone Song")
        gbase = A.load(gone)["published_as"]
        A.set_job(gone, status="failed")
        jcall(A.app, "DELETE", f"/api/jobs/{gone}")
        self.assertTrue(any(e.get("job_deleted") for e in CAT.read_catalog(A.LIB)["songs"] if e["base"] == gbase))
        _, d = self.flag(id=gone, base=gbase, t=12.0)
        self.assertEqual(d["status"], "noted")
        st, _, lst = jcall(A.app, "GET", "/api/requests")
        self.assertIn(d["fid"], [f["fid"] for f in lst["flags"]])
        self.assertEqual(jcall(A.app, "POST", "/api/flags/dismiss", {"fids": [d["fid"]]})[2], {"dismissed": 1})
        self.assertNotIn(d["fid"], [f["fid"] for f in jcall(A.app, "GET", "/api/requests")[2]["flags"]])
        jcall(A.app, "POST", f"/api/jobs/{self.song}/encore-flags/dismiss", {})

    def test_map_line(self):
        cur = [(10.0, 12.0, "a b"), (14.0, 16.0, "c d"), (15.5, 17.0, "e f"), (30.0, 32.0, "g h")]
        self.assertEqual(E.map_line(cur, t=11.0), 0)
        self.assertEqual(E.map_line(cur, t=13.4), 1)          # just before a line: that line
        self.assertEqual(E.map_line(cur, t=15.8), 2)          # overlapping lines: the one that started last
        self.assertEqual(E.map_line(cur, t=15.8, line=2, pub=[(10, 12, "a b"), (14, 16, "c d")]), 1)  # the line it named
        self.assertEqual(E.map_line(cur, t=45.0), None)       # nothing near (more than 8 s away)
        self.assertEqual(E.map_line(cur, t=25.0), 3)          # a gap before a line (within 8 s)
        self.assertEqual(E.map_line(cur, line=4), 3)          # no published words: the number as it is
        self.assertEqual(E.map_line(cur, line=9), None)
        self.assertEqual(E.map_line([], t=1.0), None)


# ---------------- plays, the catalog, pitch, files ----------------
class PlaysCatalogFiles(Base):
    @classmethod
    def setUpClass(cls):
        cls.tok = pair(name="Plays kit")
        cls.song = make_song("Play Artist", "Play Song", version="Live take", duration=40.0)
        cls.base = A.load(cls.song)["published_as"]
        cls.vbase = cls.base.replace("(Music Video)", "(Live take)")

    def plays(self, items):
        st, _, b = api("POST", "/api/encore/plays", {"plays": items}, token=self.tok)
        self.assertEqual(st, 200, b)
        self.valid("plays.post.response.schema.json", b)
        return b["stored"]

    def play(self, result, seconds, base=None, at=None):
        return {"pid": str(uuid.uuid4()), "id": self.song, "base": base or self.base, "at": at or int(time.time()), "seconds": seconds,
                "result": result, "singers": 1, "style": None, "party": "P"}

    def entry(self):
        A.update_catalog()
        return next(e for e in CAT.read_catalog(A.LIB)["songs"] if e["id"] == self.song)

    def test_counting_and_idempotency(self):
        n0 = self.entry()["plays"]
        batch = [self.play("finished", 40), self.play("stopped", 25, base=self.vbase, at=2000000000), self.play("stopped", 15),
                 self.play("skipped", 39)]
        self.assertEqual(self.plays(batch), 4)
        self.assertEqual(self.plays(batch), 4)  # a retried batch: the same answer, nothing added
        self.assertEqual(self.plays(batch[:1] + [self.play("finished", 40)]), 2)
        e = self.entry()
        self.assertEqual(e["plays"], n0 + 3)    # finished x2, stopped after more than half (25 of 40) on the version
        self.assertEqual(e["last_played"], 2000000000)
        lib = {s["file"]: s for s in A.library_songs()}
        self.assertEqual(lib[f"Music Videos/{self.vbase}.mp4"]["plays"], 1)
        self.assertEqual(lib[f"Music Videos/{self.base}.mp4"]["plays"], n0 + 2)
        self.assertEqual(api("POST", "/api/encore/plays", {"plays": [self.play("finished", 1)] * 201}, token=self.tok)[0], 422)

    def test_pikaraoke_history_counts_too(self):
        db = os.path.join(_env.ROOT, "pk.db")
        c = sqlite3.connect(db)
        c.execute("create table plays (id integer primary key, song_title text, performer text, played_at text)")
        c.executemany("insert into plays (song_title, performer, played_at) values (?, ?, ?)",
                      [(self.base, "Ann", "2033-05-18 03:33:20"), (self.base, "Bo", "2033-05-18 03:33:10"),
                       (self.base, "claude-test", "2033-05-18 03:40:00")])
        c.commit()
        c.close()
        n0 = self.entry()["plays"]
        A.C.PIKARAOKE_DB = db
        try:
            e = self.entry()
            self.assertEqual(e["plays"], n0 + 2)
            self.assertEqual(e["last_played"], 2000000000)  # 2033-05-18 03:33:20 UTC
            self.assertEqual(E.pk_time("2033-05-18 03:33:20"), 2000000000)
        finally:
            A.C.PIKARAOKE_DB = ""

    def test_catalog_v2_validates_and_pitch_files(self):
        cat = CAT.read_catalog(A.LIB)
        self.assertEqual(cat["version"], 2)
        self.valid("catalog.schema.json", cat)
        e = self.entry()
        self.assertEqual(e["pitch"], self.base + ".pitch.json")
        self.assertEqual(e["versions"][0]["pitch"], self.vbase + ".pitch.json")
        for b in (self.base, self.vbase):
            d = json.load(open(os.path.join(A.MV_DIR, b + ".pitch.json")))
            self.valid("pitch.schema.json", d)
            self.assertEqual((d["base"], d["hop"], d["duration"]), (b, 0.02, 40.0))
            w = json.load(open(os.path.join(A.MV_DIR, b + ".words.json")))
            self.valid("words.schema.json", w)
            words = [x for l in w["lines"] for x in l["words"]]
            self.assertEqual(len(d["notes"]), len(words))  # one note a word (the made-up melody changes on every word)
            for (a, z, m), (wa, wz, _) in zip(d["notes"], words):
                self.assertGreaterEqual(a, wa - 0.011)
                self.assertLessEqual(z, wz + 0.011)
            self.assertEqual(d["range"], [min(n[2] for n in d["notes"]), max(n[2] for n in d["notes"])])

    def test_notes_cut_at_word_boundaries_and_held_notes(self):
        f0 = np.full(200, 220.0)              # one held A3 (MIDI 57) for 4 s
        per = np.full(200, 0.9)
        ns, rng = P.notes(f0, per, [(0.5, 1.0), (1.0, 2.0), (3.0, 3.5)])
        self.assertEqual(ns, [[0.5, 2.0, 57.0], [3.0, 3.5, 57.0]])  # a note held over a word cut stays one note
        f0[60:80] = 330.0                     # a jump of 7 semitones inside the second word: two notes
        ns, rng = P.notes(f0, per, [(1.0, 2.0)])
        self.assertEqual([n[2] for n in ns], [57.0, 64.0, 57.0])
        self.assertEqual(rng, [57.0, 64.0])
        per[:] = 0.1                          # nothing voiced
        self.assertEqual(P.notes(f0, per, [(0, 4)]), ([], None))

    def test_pitch_failure_never_stops_publishing_and_leaves_no_stale_file(self):
        jid = make_song("No Pitch", "No Pitch Song", cache=False, publish=False)
        open(A.job_path(jid, "vocals.m4a"), "wb").write(b"not audio")
        PITCHED.clear()
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/publish", {"remove": []})
        self.assertEqual(st, 200)
        self.assertEqual(PITCHED, [jid])
        base = A.load(jid)["published_as"]
        open(os.path.join(A.MV_DIR, base + ".pitch.json"), "w").write("{}")  # an old one
        with self.assertRaises(Exception):
            A.write_pitch(jid)
        self.assertFalse(os.path.exists(os.path.join(A.MV_DIR, base + ".pitch.json")))
        self.assertIsNone(next(e for e in CAT.read_catalog(A.LIB)["songs"] if e["id"] == jid)["pitch"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_crepe_on_a_sine_tone_cpu(self):
        try:
            import torchcrepe  # noqa: F401
        except ImportError:
            self.skipTest("no torchcrepe")
        d = tempfile.mkdtemp(dir=_env.ROOT)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=2", "-c:a", "aac",
                        os.path.join(d, "vocals.m4a")], check=True)
        f0, per = P.frames(d, device="cpu")
        ns, rng = P.notes(f0, per, [(0.2, 1.8)])
        self.assertTrue(ns)
        self.assertAlmostEqual(ns[0][2], 57.0, delta=0.3)
        self.assertTrue(os.path.exists(os.path.join(d, P.CACHE)))

    def test_catalog_and_files_for_remote_players(self):
        A.update_catalog()
        st, h, raw = call(A.app, "GET", "/api/encore/catalog", headers={"Authorization": "Bearer " + self.tok})
        self.assertEqual(st, 200)
        self.valid("catalog.schema.json", json.loads(raw))
        st2, h2, b2 = call(A.app, "GET", "/api/encore/catalog", headers={"Authorization": "Bearer " + self.tok, "If-None-Match": h["etag"]})
        self.assertEqual((st2, b2, h2["etag"]), (304, b"", h["etag"]))
        auth = {"Authorization": "Bearer " + self.tok}
        p = f"Music Videos/{self.base}.mp4".replace(" ", "%20")
        st, fh, body = call(A.app, "GET", "/api/encore/files/" + p, headers=auth)
        full = open(os.path.join(A.MV_DIR, self.base + ".mp4"), "rb").read()
        self.assertEqual((st, body), (200, full))
        st, rh, part = call(A.app, "GET", "/api/encore/files/" + p, headers=dict(auth, Range="bytes=2-9"))
        self.assertEqual((st, part, rh["content-range"]), (206, full[2:10], f"bytes 2-9/{len(full)}"))
        self.assertEqual(call(A.app, "GET", "/api/encore/files/" + p, headers=dict(auth, **{"If-None-Match": fh["etag"]}))[0], 304)
        for ext in (".ass", ".words.json", ".pitch.json"):
            self.assertEqual(call(A.app, "GET", "/api/encore/files/" + f"Music Videos/{self.vbase}{ext}".replace(" ", "%20"), headers=auth)[0], 200)
        # only what the catalog names; no way out of the library
        secret = os.path.join(_env.ROOT, "secret.txt")
        open(secret, "w").write("no")
        os.symlink(secret, os.path.join(A.MV_DIR, "Sneaky (Music Video).ass"))
        open(os.path.join(A.MV_DIR, "Unlisted (Music Video).mp4"), "wb").write(b"x")
        for bad in ["catalog.json", "Music%20Videos/Unlisted%20(Music%20Video).mp4", "../secret.txt", "..%2Fsecret.txt",
                    "Music%20Videos/../../secret.txt", "%2E%2E/%2E%2E/etc/passwd", "/etc/passwd", "Music%20Videos/Sneaky%20(Music%20Video).ass",
                    "Music%20Videos//" + p.split("/", 1)[1], "Music%20Videos\\" + p.split("/", 1)[1]]:
            with self.subTest(path=bad):
                st, _, b = jcall(A.app, "GET", "/api/encore/files/" + bad, None, auth)
                self.assertEqual(st, 404)
                self.valid("error.schema.json", b)
        self.assertEqual(call(A.app, "GET", "/api/encore/files/" + p)[0], 401)

    def test_unpublished(self):
        jid = make_song("Unpub Artist", "Unpub Song", publish=False)
        st, _, b = api("GET", "/api/encore/unpublished", token=self.tok)
        self.assertEqual(st, 200)
        self.valid("unpublished.get.response.schema.json", b)
        self.assertIn(jid, [s["job"] for s in b["songs"]])
        self.assertNotIn(self.song, [s["job"] for s in b["songs"]])
        self.assertIsNone(b["studio_url"])
        A.C.STUDIO_PUBLIC_URL = "https://studio.example.lan"
        try:
            self.assertEqual(api("GET", "/api/encore/unpublished", token=self.tok)[2]["studio_url"], "https://studio.example.lan")
        finally:
            A.C.STUDIO_PUBLIC_URL = ""


# ---------------- PiKaraoke is optional ----------------
class PiKaraokeOptional(Base):
    def test_publish_without_pikaraoke(self):
        self.assertEqual(A.PK_URL, "")
        jid = make_song("Solo Player", "No PiKaraoke", publish=False)
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/publish", {"remove": []})
        self.assertEqual((st, b["message"]), (200, "published"))
        self.assertTrue(os.path.exists(os.path.join(A.MV_DIR, b["name"] + ".mp4")))
        self.assertIsNone(A.pk_rescan())
        st, _, r = jcall(A.app, "POST", f"/api/jobs/{jid}/rename", {"artist": "Solo Player", "title": "No PiKaraoke Renamed"})
        self.assertEqual((st, r["message"]), (200, "renamed in the library"))
        A.C.PIKARAOKE_DB = ""
        self.assertTrue(isinstance(A.library_songs(), list))

    def test_rescan_unreachable(self):
        A.PK_URL = "http://127.0.0.1:9"  # nothing listens there
        try:
            self.assertIn("didn't answer", A.pk_rescan())
        finally:
            A.PK_URL = ""

    def test_rescan_login_flow_against_a_fake_pikaraoke(self):
        """PiKaraoke 1.23: POST /auth (form) sets a session cookie when the password is right and redirects either way;
        POST /sync_library needs the cookie (403 without)."""
        import http.server, threading, urllib.parse
        state = {"password": "right", "refuse_once": False, "syncs": 0, "auths": 0}

        class PK(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                ok = "Incorrect admin password!" if "bad=1" in self.path else "home"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(ok.encode())

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
                if self.path == "/auth":
                    state["auths"] += 1
                    good = urllib.parse.parse_qs(body).get("admin_password") == [state["password"]]
                    self.send_response(302)
                    if good:
                        self.send_header("Set-Cookie", "session=admin; Path=/; HttpOnly")
                    self.send_header("Location", "/" if good else "/?bad=1")
                    self.end_headers()
                elif self.path == "/sync_library":
                    if "session=admin" not in (self.headers.get("cookie") or "") or state["refuse_once"]:
                        state["refuse_once"] = False
                        self.send_response(403)
                        self.end_headers()
                        return
                    state["syncs"] += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status": "started"}')
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PK)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        A.PK_URL = f"http://127.0.0.1:{srv.server_address[1]}"
        old_pw = A.C.PIKARAOKE_PASSWORD
        try:
            A.C.PIKARAOKE_PASSWORD = "right"
            self.assertIn("rescanning", A.pk_rescan())
            self.assertEqual(state["syncs"], 1)
            state["refuse_once"] = True       # e.g. PiKaraoke restarted between the login and the sync: one more try
            self.assertIn("rescanning", A.pk_rescan())
            self.assertEqual((state["syncs"], state["auths"]), (2, 3))
            A.C.PIKARAOKE_PASSWORD = "wrong"
            self.assertIn("refused the admin password", A.pk_rescan())
            self.assertEqual(state["syncs"], 2)
            A.C.PIKARAOKE_PASSWORD = ""       # no password configured, but PiKaraoke has one
            self.assertIn("set PIKARAOKE_ADMIN_PASSWORD", A.pk_rescan())
        finally:
            A.PK_URL, A.C.PIKARAOKE_PASSWORD = "", old_pw
            srv.shutdown()


# ---------------- the contract's fixtures are the studio's own output ----------------
class Fixtures(Base):
    def test_regenerated_fixtures_match_the_committed_ones(self):
        here = os.path.join(_env.REPO, "contract", "fixtures")
        out = tempfile.mkdtemp(dir=_env.ROOT)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("KARAOKE_", "PIKARAOKE_", "ENCORE_", "STUDIO_"))}
        r = subprocess.run([sys.executable, "-I", "-B", os.path.join(_env.REPO, "contract", "make_fixtures.py"), out],
                           capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])

        def files(root):
            return {os.path.relpath(os.path.join(d, f), root): open(os.path.join(d, f), "rb").read()
                    for d, _, fs in os.walk(root) for f in fs}
        mine, theirs = files(out), files(here)
        self.assertEqual(sorted(mine), sorted(theirs))
        self.assertEqual([f for f in mine if mine[f] != theirs[f]], [])

    def test_fixtures_match_the_schemas(self):
        fx = os.path.join(_env.REPO, "contract", "fixtures")
        cat = json.load(open(os.path.join(fx, "library", "catalog.json")))
        self.valid("catalog.schema.json", cat)
        self.assertEqual([(s["title"], s["plays"]) for s in cat["songs"]], [("Neon Harbor", 3), ("Paper Lanterns", 7), ("Two Small Rivers", 0)])
        mv = os.path.join(fx, "library", "Music Videos")
        for f in os.listdir(mv):
            if f.endswith((".words.json", ".pitch.json")):
                self.valid("words.schema.json" if f.endswith(".words.json") else "pitch.schema.json", json.load(open(os.path.join(mv, f))))
        names = sorted(os.listdir(os.path.join(fx, "api")))
        self.assertEqual(len(names), 31)  # (24-31: §2.6 profiles)
        for n in names:
            x = json.load(open(os.path.join(fx, "api", n)))
            if x["request"]["schema"]:
                self.valid(x["request"]["schema"], x["request"]["body"])
            body = x["response"]["body"]
            if isinstance(body, dict) and "$file" in body:
                self.assertTrue(os.path.isfile(os.path.join(fx, body["$file"])), n)
            elif x["response"]["schema"]:
                self.valid(x["response"]["schema"], body)

    def test_contract_version(self):
        self.assertEqual(open(os.path.join(_env.REPO, "contract", "CONTRACT_VERSION")).read().strip(), E.CONTRACT)
        self.assertEqual(E.CONTRACT, "1")


if __name__ == "__main__":
    unittest.main()
