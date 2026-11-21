"""Sync bundles (contract §2.7, bundle.py): Export for <player> (a tar with its exact length, signed with that player's
key, only what it doesn't have, parts) and Import a bundle (a player's requests, flags and plays applied with the API's
own handlers; auto stripped; refusals).

    python -m unittest tests.test_bundle
"""
import hashlib, hmac, io, ipaddress, json, os, tarfile, time, unittest, uuid

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import app as A  # noqa: E402
import bundle as B  # noqa: E402
import encore as E  # noqa: E402
import schemalite as S  # noqa: E402
from tests._asgi import call, jcall  # noqa: E402

LIB = A.LIB
MV = "Music Videos"


def write(rel, data):
    p = os.path.join(LIB, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def song(i, published):
    base = f"Artist {i} - Song {i} (Music Video)"
    return {"id": f"2026111{i}-000000-song-{i}", "artist": f"Artist {i}", "title": f"Song {i}", "lang": "en", "duration": 60.0,
            "published": published, "folder": MV, "base": base, "lyrics": {"file": base + ".ass", "modes": {}, "default_mode": None},
            "versions": [], "parts": [], "together": False, "group": f"artist {i}|song {i}", "words": base + ".words.json",
            "lines": 2, "first": 1.0, "pitch": None, "vocals": None, "plays": 0, "last_played": None}


def library(n=3):
    songs = [song(i, 1_800_000_000 + i) for i in range(1, n + 1)]
    for s in songs:
        write(f"{MV}/{s['base']}.mp4", os.urandom(3000 + 100 * int(s["id"][7])))
        write(f"{MV}/{s['base']}.ass", b"[Script Info]\n")
        write(f"{MV}/{s['base']}.words.json", json.dumps({"lines": []}).encode())
    write("catalog.json", json.dumps({"version": 2, "generated": 1_800_000_100.0, "songs": songs}).encode())
    A._CAT["key"] = None
    return songs


_N = [20]


def pair():
    player = str(uuid.uuid4())
    code, _ = A.STORE.new_code()
    _N[0] += 1   # (a new address each time: pairing is rate-limited per address)
    st, _, b = jcall(A.app, "POST", "/api/encore/pair", {"code": code, "name": "Ken's kit", "player": player, "encore": "0.8",
                                                         "contract": "1"}, client=(f"192.0.2.{_N[0]}", 40000))
    assert st == 200, b
    return player, b["token"]


def key_of(token):
    return B.key(hashlib.sha256(token.encode()).hexdigest())


def members(data):
    tf = tarfile.open(fileobj=io.BytesIO(data), mode="r|")
    out = []
    for ti in tf:
        out.append((ti.name, ti.isreg(), tf.extractfile(ti).read() if ti.isreg() else None))
    return out


def player_bundle(player, token, calls, studio_id=None, extra=None, sign_with=None):
    raw = json.dumps(calls).encode()
    m = {"bundle": 1, "kind": "player", "contract": "1", "studio_id": studio_id or A.STORE.studio_id(), "player": player,
         "player_name": "Ken's kit", "made": time.time(), "calls": {"member": "calls.json", "size": len(raw),
                                                                     "sha256": hashlib.sha256(raw).hexdigest()}}
    mb = json.dumps(m).encode()
    sig = B.sign(sign_with or key_of(token), mb).encode()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name, data in [("manifest.json", mb), ("manifest.sig", sig), ("calls.json", raw)] + (extra or []):
            ti = tarfile.TarInfo(name); ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return out.getvalue()


def export(player, **body):
    st, _, b = jcall(A.app, "POST", f"/api/players/{player}/export", body)
    assert st == 200, b
    for _ in range(200):
        st, _, v = jcall(A.app, "GET", f"/api/players/{player}/export")
        if v["state"] != "hashing":
            return v
        time.sleep(0.02)
    raise AssertionError("the export didn't finish")


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.songs = library()
        self.player, self.token = pair()

    def test_a_signed_tar_with_its_exact_length(self):
        v = export(self.player)
        self.assertEqual((v["state"], v["songs"], len(v["parts"])), ("ready", 3, 1))
        st, h, data = call(A.app, "GET", f"/api/players/{self.player}/export/1")
        self.assertEqual(st, 200)
        self.assertEqual(int(h["content-length"]), len(data))
        self.assertEqual(int(h["content-length"]), v["parts"][0]["bytes"])
        self.assertIn(".karaoke-sync.tar", h["content-disposition"])
        ms = members(data)
        self.assertEqual([n for n, *_ in ms[:4]], ["manifest.json", "manifest.sig", "catalog.json", "answers.json"])
        mb, sig = ms[0][2], ms[1][2].decode()
        self.assertTrue(hmac.compare_digest(sig, B.sign(key_of(self.token), mb)))
        m = json.loads(mb)
        self.assertEqual(S.errors("bundle.manifest.schema.json", m), [])
        self.assertEqual((m["kind"], m["player"], m["part"], m["studio_id"]), ("studio", self.player, [1, 1], A.STORE.studio_id()))
        blobs = {n: d for n, _, d in ms if n.startswith("f/")}
        for f in m["files"]:   # every file listed, carried, its bytes matching
            self.assertEqual(f["member"], "f/" + f["sha256"])
            self.assertEqual(hashlib.sha256(blobs[f["member"]]).hexdigest(), f["sha256"])
            self.assertEqual(f["etag"], A.file_etag(f["path"], os.stat(os.path.join(LIB, *f["path"].split("/")))))
        self.assertEqual(len(m["files"]), 9)
        answers = json.loads(dict((n, d) for n, _, d in ms)["answers.json"])
        self.assertEqual(S.errors("bundle.answers.schema.json", answers), [])
        self.assertEqual(S.errors("unpublished.get.response.schema.json", answers["unpublished"]), [])

    def test_only_what_the_player_doesnt_have_and_parts(self):
        export(self.player)
        _, _, data = call(A.app, "GET", f"/api/players/{self.player}/export/1")
        m = json.loads(members(data)[0][2])
        have = {f["path"]: f["etag"] for f in m["files"]}
        new = self.songs[0]["base"]
        write(f"{MV}/{new}.mp4", os.urandom(4000))   # remade since
        A.STORE.set_have(self.player, have)
        v = export(self.player)
        _, _, data = call(A.app, "GET", f"/api/players/{self.player}/export/1")
        m = json.loads(members(data)[0][2])
        carried = [f["path"] for f in m["files"] if f["member"]]
        self.assertEqual(carried, [f"{MV}/{new}.mp4"])
        self.assertEqual(len(m["files"]), 9)                  # still lists everything
        v = export(self.player, everything=True)
        self.assertEqual(v["songs"], 3)
        # parts: whole songs each, every part a complete bundle
        cat = open(os.path.join(LIB, "catalog.json"), "rb").read()
        plan = B.plan(self.player, A.STORE.studio_id(), "S", LIB, cat, {"requests": [], "unpublished": {"songs": [], "studio_url": None},
                      "applied": {"requests": [], "flags": [], "plays": []}}, A.catalog.files_of, A.file_etag, A.HASHES,
                      everything=True, part_bytes=6000)
        self.assertEqual(len(plan.parts), 3)
        k = key_of(self.token)
        for i in range(3):
            data = b"".join(plan.stream(i, k))
            self.assertEqual(len(data), plan.size(i, k))
            m = json.loads(members(data)[0][2])
            self.assertEqual(m["part"], [i + 1, 3])
            self.assertEqual(sum(1 for f in m["files"] if f["member"] and f["path"].endswith(".mp4")), 1)   # one song per part

    def test_a_file_changing_mid_download_cuts_it_short(self):
        export(self.player)
        write(f"{MV}/{self.songs[1]['base']}.mp4", os.urandom(50))
        # (under uvicorn the connection drops short of its Content-Length: the browser sees a broken download, never a
        # mixed file; this in-process client re-raises instead)
        with self.assertRaises(B.BundleError):
            call(A.app, "GET", f"/api/players/{self.player}/export/1")

    def test_admin_only(self):
        self.assertEqual(jcall(A.app, "POST", "/api/players/nobody/export", {})[0], 404)
        st, _, _ = jcall(A.app, "POST", f"/api/players/{self.player}/export", {}, {"Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        A._ONLY_NETS.append(ipaddress.ip_network("198.51.100.0/24"))   # the players-only network
        try:
            for method, path in (("GET", f"/api/players/{self.player}/export/1"), ("POST", "/api/players/import")):
                self.assertEqual(call(A.app, method, path, b"", server=("198.51.100.1", 5570))[0], 403, path)
        finally:
            A._ONLY_NETS.pop()
        # a player's token never gets the library tar
        st, _, _ = call(A.app, "GET", f"/api/players/{self.player}/export/1", headers={"Authorization": "Bearer " + self.token})
        self.assertNotEqual(st, 200) if not A.EXPORTS.get(self.player) else None


class ImportTests(unittest.TestCase):
    def setUp(self):
        library()
        self.player, self.token = pair()

    def calls(self, **kw):
        r = {"rid": str(uuid.uuid4()), "artist": "Nobody Known", "title": "A Song Nobody Has", "note": "", "by": "Mia",
             "party": "Sat", "at": int(time.time()), "auto": True}
        f = {"fid": str(uuid.uuid4()), "id": "x", "base": "b", "t": None, "line": None, "kind": "words", "note": "", "by": "Mia",
             "party": "Sat", "at": int(time.time())}
        p = {"pid": str(uuid.uuid4()), "id": "x", "base": "b", "at": int(time.time()), "seconds": 50, "result": "finished",
             "singers": 1, "style": "classic", "party": "Sat"}
        return dict({"requests": [r], "flags": [f], "plays": [p], "have": {f"{MV}/x.mp4": '"abc"'}}, **kw)

    def test_applied_as_the_player_without_auto(self):
        c = self.calls()
        A.STORE.set_automake(self.player, True)
        data = player_bundle(self.player, self.token, c)
        st, _, b = jcall(A.app, "POST", "/api/players/import", data, {"Content-Type": "application/x-tar"})
        self.assertEqual(st, 200, b)
        self.assertEqual((b["requests"], b["flags"], b["plays"]), (1, 1, 1))
        r = A.STORE.get_request(c["requests"][0]["rid"])
        self.assertEqual((r["player"], r["auto"], r["queue"], r["status"]), (self.player, 0, None, "open"))   # never made by itself
        self.assertEqual(A.STORE.get_flag(c["flags"][0]["fid"])["player"], self.player)
        self.assertEqual(A.STORE.get_have(self.player), c["have"])
        # twice: nothing new
        st, _, b = jcall(A.app, "POST", "/api/players/import", data, {"Content-Type": "application/x-tar"})
        self.assertEqual(st, 200)
        self.assertEqual(A.STORE.q("select count(*) from requests where rid=?", (c["requests"][0]["rid"],))[0][0], 1)
        # the next export lists them as applied
        ans = A._export_answers(self.player)
        self.assertIn(c["requests"][0]["rid"], ans["applied"]["requests"])
        self.assertIn(c["plays"][0]["pid"], ans["applied"]["plays"])

    def test_only_requests_no_plays(self):
        c = self.calls(flags=[], plays=[])
        st, _, b = jcall(A.app, "POST", "/api/players/import", player_bundle(self.player, self.token, c), {"Content-Type": "application/x-tar"})
        self.assertEqual((st, b.get("requests"), b.get("plays")), (200, 1, 0), b)

    def test_refusals(self):
        c = self.calls()
        cases = {
            "old link": player_bundle(self.player, self.token, c, sign_with=b"x" * 32),
            "not linked": player_bundle(str(uuid.uuid4()), self.token, c),
            "other Studio": player_bundle(self.player, self.token, c, studio_id=str(uuid.uuid4())),
            "extra member": player_bundle(self.player, self.token, c, extra=[("evil.sh", b"rm -rf /")]),
            "bad item": player_bundle(self.player, self.token, self.calls(flags=[{"fid": "nope"}])),
            "not a tar": b"hello" * 100,
        }
        for why, data in cases.items():
            st, _, b = jcall(A.app, "POST", "/api/players/import", data, {"Content-Type": "application/x-tar"})
            self.assertEqual(st, 400, why)
        self.assertIn("old link", jcall(A.app, "POST", "/api/players/import", cases["old link"])[2]["detail"])
        self.assertEqual(A.STORE.q("select count(*) from requests where player=?", (self.player,))[0][0], 0)


if __name__ == "__main__":
    unittest.main()
