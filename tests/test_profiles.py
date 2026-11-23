"""Encore user profiles on the Studio's side (contract §2.6, schemas profile*.json): two players syncing after being
offline, never echoing a player's own events, idempotent eids, paging with "more", tombstones that erase a profile
everywhere (and its uid from requests, flags and plays) and drop late events, export without the PIN hash, the player
check, refused cursors, and the admin list (no history). Every body and answer is checked against the schemas."""
import json, os, time, unittest, uuid

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import app as A  # noqa: E402
import encore as E  # noqa: E402
import schemalite as S  # noqa: E402
from tests._asgi import jcall  # noqa: E402

_n = [0]


def U():
    return str(uuid.uuid4())


def addr():
    _n[0] += 1
    return (f"198.51.100.{10 + _n[0] % 200}", 42000 + _n[0])


def pair(name):
    player = U()
    code, _ = A.STORE.new_code()
    st, _, b = jcall(A.app, "POST", "/api/encore/pair", {"code": code, "name": name, "player": player, "encore": "0.6",
                                                         "contract": "1"}, client=addr())
    assert st == 200, b
    return player, b["token"]


T = [1795200000.0]


def at():
    T[0] += 1.001
    return round(T[0], 3)


def prof(uid, name="Mia", **kw):
    return {"eid": U(), "stream": "profile", "uid": uid, "at": at(), "name": name, "color": "#ff5fa2", "pin_hash": None,
            "created": 1795100000.0, "deleted": False, **kw}


def fav(uid, song, on=True):
    return {"eid": U(), "stream": "favorites", "uid": uid, "at": at(), "song": song, "on": on}


def pref(uid, key, value):
    return {"eid": U(), "stream": "prefs", "uid": uid, "at": at(), "key": key, "value": value}


def sung(uid, song="20261117-000000-x"):
    return {"eid": U(), "stream": "history", "uid": uid, "at": at(), "song": song, "base": "Artist - Title", "result": "finished",
            "party": "Friday"}


def tomb(uid, merged_into=None):
    return {"eid": U(), "stream": "profile", "uid": uid, "at": at(), "deleted": True, **({"merged_into": merged_into} if merged_into else {})}


class Profiles(unittest.TestCase):
    def setUp(self):
        self.home, self.home_tok = pair("Home Encore")
        self.kit, self.kit_tok = pair("Party kit")

    def now(self, who):
        """The player's cursor at the end of everything stored so far (other tests' events too)."""
        c = None
        while True:
            b = self.sync(who, [], c)
            c = b["cursor"]
            if not b.get("more"):
                return c

    def sync(self, who, events, since=None, status=200):
        player, tok = (self.home, self.home_tok) if who == "home" else (self.kit, self.kit_tok)
        body = {"player": player, "since": since, "events": events}
        self.assertEqual(S.errors("profiles.sync.request.schema.json", body), [])
        st, _, b = jcall(A.app, "POST", "/api/encore/profiles/sync", body, {"Authorization": "Bearer " + tok}, client=addr())
        self.assertEqual(st, status, b)
        if st == 200:
            self.assertEqual(S.errors("profiles.sync.response.schema.json", b), [], b)
        return b

    def export(self, uid, who="home", status=200):
        tok = self.home_tok if who == "home" else self.kit_tok
        st, _, b = jcall(A.app, "GET", f"/api/encore/profiles/{uid}/export", None, {"Authorization": "Bearer " + tok}, client=addr())
        self.assertEqual(st, status, b)
        if st == 200:
            self.assertEqual(S.errors("profiles.export.response.schema.json", b), [], b)
        return b

    def test_two_players_offline_then_sync(self):
        mia = U()
        home, kit = {"cursor": self.now("home")}, {"cursor": self.now("kit")}
        # both offline for a while: each made its own changes to the same profile
        p1, f1, s1 = prof(mia, pin_hash={"alg": "scrypt", "n": 16384, "r": 8, "p": 1, "salt": "ab" * 16, "hash": "cd" * 32}), fav(mia, "song-a"), sung(mia)
        f2, p2, s2 = fav(mia, "song-a", on=False), prof(mia, name="Mia K"), sung(mia, "song-b")
        a = self.sync("home", [p1, f1, s1], home["cursor"])
        self.assertEqual([e["eid"] for e in a["events"]], [])      # nothing from others yet, never its own
        b = self.sync("kit", [f2, p2, s2], kit["cursor"])
        self.assertEqual([e["eid"] for e in b["events"]], [p1["eid"], f1["eid"], s1["eid"]])  # home's, in stored order
        a2 = self.sync("home", [], a["cursor"])
        self.assertEqual([e["eid"] for e in a2["events"]], [f2["eid"], p2["eid"], s2["eid"]])
        self.assertEqual(self.sync("home", [], a2["cursor"])["events"], [])
        # the merged state: latest (at, eid) wins per record/key; history is everything
        x = self.export(mia)
        self.assertEqual(x["profile"]["name"], "Mia K")
        self.assertFalse(x["profile"]["pin"])                     # the latest record has no PIN
        self.assertEqual([(f["song"], f["on"]) for f in x["favorites"]], [("song-a", False)])
        self.assertEqual(len(x["history"]), 2)
        self.assertEqual({p["name"] for p in x["players"]}, {"Home Encore", "Party kit"})
        self.assertNotIn("pin_hash", json.dumps(x)); self.assertNotIn("cd" * 32, json.dumps(x))

    def test_repeated_eid_and_whole_batch_refused(self):
        u = U()
        e = pref(u, "style:ja", "romaji")
        kc = self.now("kit")
        c = self.sync("home", [e])["cursor"]
        self.sync("home", [e, e])                                  # the same event again: stored once
        got = self.sync("kit", [], kc)["events"]
        self.assertEqual(sum(1 for x in got if x["eid"] == e["eid"]), 1)
        bad = dict(pref(u, "guide", "full"), stream="nope")
        st, _, b = jcall(A.app, "POST", "/api/encore/profiles/sync", {"player": self.home, "since": c, "events": [pref(u, "guide", "low"), bad]},
                         {"Authorization": "Bearer " + self.home_tok}, client=addr())
        self.assertEqual(st, 400); self.assertIn("error", b)
        self.assertEqual(self.export(u)["prefs"], [{"key": "style:ja", "value": "romaji", "at": e["at"]}])  # nothing of it stored

    def test_tombstone_erases_everywhere(self):
        mia, other = U(), U()
        kit_c = self.now("kit")
        self.sync("kit", [prof(mia), fav(mia, "s1"), sung(mia), prof(other, name="Sam")])
        rid, fid, pid = U(), U(), U()
        A.STORE.q("insert into requests (rid, player, artist, title, uid, status, created, updated) values (?, ?, 'A', 'T', ?, 'open', 0, 0)", (rid, self.kit, mia))
        A.STORE.q("insert into flags (fid, player, uid, status, created) values (?, ?, ?, 'noted', 0)", (fid, self.kit, mia))
        A.STORE.q("insert into plays (pid, player, uid, created) values (?, ?, ?, 0)", (pid, self.kit, mia))
        home_c = self.now("home")
        t = tomb(mia)
        self.sync("home", [t])
        # every other player gets the tombstone, and never the profile's other events again
        kit_all = self.sync("kit", [], kit_c)["events"]
        self.assertEqual([e for e in kit_all if e["uid"] == mia], [{k: t[k] for k in ("eid", "stream", "uid", "at", "deleted")}])
        self.assertEqual(self.sync("home", [], home_c)["events"], [])          # (its own tombstone isn't echoed)
        # erased here, and the requests, flags and plays it sent stay without the uid
        self.assertEqual(A.STORE.q("select count(*) from profile_events where uid=? and stream != 'profile'", (mia,))[0][0], 0)
        for tbl, key, val in (("requests", "rid", rid), ("flags", "fid", fid), ("plays", "pid", pid)):
            self.assertIsNone(A.STORE.q(f"select uid from {tbl} where {key}=?", (val,))[0][0], tbl)
        # late events from an offline kit are accepted and dropped
        k = self.sync("kit", [fav(mia, "s2"), prof(mia, name="Back")])
        self.assertEqual(A.STORE.q("select count(*) from profile_events where uid=?", (mia,))[0][0], 1)
        self.assertEqual(self.export(mia), {"uid": mia, "deleted": True, "profile": None, "favorites": [], "prefs": [], "history": [], "players": [], "photo": None})
        self.assertEqual(self.export(other)["profile"]["name"], "Sam")     # others untouched
        # the admin list says deleted, no name
        row = [p for p in jcall(A.app, "GET", "/api/profiles")[2]["profiles"] if p["uid"] == mia][0]
        self.assertTrue(row["deleted"]); self.assertIsNone(row["name"])

    def test_merged_into(self):
        a, b = U(), U()
        kc = self.now("kit")
        self.sync("home", [prof(a, name="Mia (kit)"), prof(b, name="Mia"), tomb(a, merged_into=b)])
        got = [e for e in self.sync("kit", [], kc)["events"] if e["uid"] == a]
        self.assertEqual(got[0]["merged_into"], b)
        row = [p for p in jcall(A.app, "GET", "/api/profiles")[2]["profiles"] if p["uid"] == a][0]
        self.assertEqual(row["merged_into"], b)

    def test_paging(self):
        u = U()
        start = self.now("kit")
        for k in range(3):  # 3 x 200 = 600 events from home
            self.sync("home", [pref(u, f"k{k}_{i}", i) for i in range(200)])
        first = self.sync("kit", [], start)
        self.assertEqual((len(first["events"]), first.get("more")), (500, True))
        second = self.sync("kit", [], first["cursor"])
        self.assertEqual(len(second["events"]), 100); self.assertNotIn("more", second)
        self.assertEqual(len({e["eid"] for e in first["events"] + second["events"]}), 600)
        self.assertEqual(self.sync("kit", [], second["cursor"])["events"], [])

    def test_player_check_cursor_and_unknown(self):
        st, _, b = jcall(A.app, "POST", "/api/encore/profiles/sync", {"player": self.kit, "since": None, "events": []},
                         {"Authorization": "Bearer " + self.home_tok}, client=addr())
        self.assertEqual(st, 403)
        st, _, b = jcall(A.app, "POST", "/api/encore/profiles/sync", {"player": self.home, "since": "bogus", "events": []},
                         {"Authorization": "Bearer " + self.home_tok}, client=addr())
        self.assertEqual(st, 400); self.assertIn("cursor", b["error"])
        st, _, b = jcall(A.app, "POST", "/api/encore/profiles/sync", {"player": self.home, "since": "s99999999", "events": []},
                         {"Authorization": "Bearer " + self.home_tok}, client=addr())
        self.assertEqual(st, 400)
        self.export(U(), status=404)
        self.assertEqual(jcall(A.app, "GET", "/api/encore/profiles/not-a-uid/export", None, {"Authorization": "Bearer " + self.home_tok}, client=addr())[0], 404)
        self.assertEqual(jcall(A.app, "POST", "/api/encore/profiles/sync", {"player": self.home, "since": None, "events": []}, client=addr())[0], 401)

    def test_admin_list_has_no_history(self):
        u = U()
        self.sync("home", [prof(u, name="Lee", pin_hash={"alg": "scrypt", "n": 16384, "r": 8, "p": 1, "salt": "aa" * 8, "hash": "bb" * 16}), sung(u)])
        rows = jcall(A.app, "GET", "/api/profiles")[2]["profiles"]
        row = [p for p in rows if p["uid"] == u][0]
        self.assertEqual((row["name"], row["pin"], row["players"]), ("Lee", True, ["Home Encore"]))
        self.assertEqual(set(row), {"uid", "name", "color", "pin", "created", "last", "deleted", "merged_into", "players", "photo"})
        self.assertNotIn("bb" * 16, json.dumps(rows))
        key, _ = A.KEYS.create("Profile peeker", ["read"])
        self.assertEqual(jcall(A.app, "GET", "/api/profiles", None, {"X-Studio-Agent-Key": key})[0], 403)  # people only


if __name__ == "__main__":
    unittest.main()


# ---------------- profile pictures (§2.6 photo) ----------------
import hashlib  # noqa: E402
from tests._asgi import call  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"picture" * 50


def photo(uid, data):
    return {"eid": U(), "stream": "photo", "uid": uid, "at": at(), "photo": hashlib.sha256(data).hexdigest() if data else None}


class ProfilePhotos(unittest.TestCase):
    setUp, now, sync, export = Profiles.setUp, Profiles.now, Profiles.sync, Profiles.export

    def put(self, uid, data, sha=None, who="home"):
        tok = self.home_tok if who == "home" else self.kit_tok
        sha = sha or hashlib.sha256(data).hexdigest()
        st, _, b = call(A.app, "PUT", f"/api/encore/profiles/{uid}/photo/{sha}", data,
                        {"Authorization": "Bearer " + tok, "Content-Type": "image/jpeg"}, client=addr())
        return st

    def get(self, uid, data, who="kit"):
        tok = self.home_tok if who == "home" else self.kit_tok
        return call(A.app, "GET", f"/api/encore/profiles/{uid}/photo/{hashlib.sha256(data).hexdigest()}", None,
                    {"Authorization": "Bearer " + tok}, client=addr())

    def test_a_picture_goes_up_then_its_event_and_the_other_player_gets_both(self):
        mia = U()
        kit = self.now("kit")
        self.sync("home", [prof(mia)])
        self.assertEqual(self.get(mia, JPEG)[0], 404)                 # not uploaded yet
        self.assertEqual(self.put(mia, JPEG), 200)
        ev = photo(mia, JPEG)
        self.assertEqual(S.errors("profile-event.schema.json", ev), [])
        self.sync("home", [ev])
        got = self.sync("kit", [], kit)["events"]
        self.assertIn(ev, got)
        st, h, body = self.get(mia, JPEG)
        self.assertEqual((st, body, h.get("content-type")), (200, JPEG, "image/jpeg"))
        self.assertEqual(self.export(mia)["photo"], {"sha": ev["photo"], "at": ev["at"]})
        listed = next(p for p in A.STORE.profiles() if p["uid"] == mia)
        self.assertEqual(listed["photo"], ev["photo"])
        st, _, body = call(A.app, "GET", f"/api/profiles/{mia}/photo")   # the Players tab
        self.assertEqual((st, body), (200, JPEG))
        # removed: the latest photo event is null; the export says so
        self.sync("home", [photo(mia, None)])
        self.assertIsNone(self.export(mia)["photo"])

    def test_a_real_size_picture_goes_through_the_player_gate(self):
        mia = U()
        big = b"\xff\xd8\xff" + os.urandom(150 * 1024)          # a 256 px photo can be well over the 64 KB JSON limit
        self.assertEqual(self.put(mia, big), 200)
        self.assertEqual(self.get(mia, big)[2], big)

    def test_refused_pictures(self):
        mia = U()
        self.assertEqual(self.put(mia, b"not a jpeg"), 400)
        self.assertEqual(self.put(mia, JPEG, sha="0" * 64), 400)            # not the picture that sha names
        self.assertEqual(self.put(mia, b"\xff\xd8\xff" + b"x" * (E.PHOTO_MAX + 1)), 413)
        self.assertEqual(self.get(U(), JPEG)[0], 404)                       # another profile's sha: not served
        self.assertEqual(self.put(mia, JPEG), 200)
        self.assertEqual(self.get(U(), JPEG)[0], 404)                       # only to the uid that uploaded it

    def test_deleting_a_profile_erases_its_pictures(self):
        mia = U()
        self.sync("home", [prof(mia)])
        self.assertEqual(self.put(mia, JPEG + b"mia"), 200)
        self.sync("home", [photo(mia, JPEG + b"mia")])
        f = A.STORE.photo_path(mia, hashlib.sha256(JPEG + b"mia").hexdigest())
        self.assertTrue(os.path.exists(f))
        self.sync("home", [tomb(mia)])
        self.assertFalse(os.path.exists(f))
        self.assertEqual(self.get(mia, JPEG + b"mia")[0], 404)
        self.assertEqual(self.put(mia, JPEG + b"mia"), 404)                  # a deleted profile takes no new one
        self.assertIsNone(self.export(mia)["photo"])
