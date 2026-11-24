"""Library: "Remove from the library" (exactly the song's files, every version's; the catalog; the studio copy kept
and publishable again, or deleted too; songs with no studio copy; who may do it) and "Refresh library" (what changed,
nothing when nothing changed, never in the middle of a publish). Calls go through the whole ASGI app."""
import json, os, shutil, threading, unittest, urllib.parse

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import app as A  # noqa: E402
import agents  # noqa: E402
import catalog as CAT  # noqa: E402
from tests._asgi import call, jcall  # noqa: E402
from tests.test_encore import make_song, pair  # noqa: E402


def lib_files():
    """Every file in the library (relative, '/'), catalog.json left out."""
    out = set()
    for d, _, fs in os.walk(A.LIB):
        for f in fs:
            rel = os.path.relpath(os.path.join(d, f), A.LIB).replace(os.sep, "/")
            if rel != CAT.CATALOG:
                out.add(rel)
    return out


def entry(jid=None, base=None):
    for e in (CAT.read_catalog(A.LIB) or {}).get("songs", []):
        if (jid and e["id"] == jid) or (base and e["base"] == base):
            return e
    return None


def mv(name):
    return os.path.join(A.MV_DIR, name)


def touch(name, data=b"x"):
    open(mv(name), "wb").write(data)


def full_song(artist, title):
    """A published song with a version, and every kind of library file: video, lyrics, a display-mode lyrics file,
    words, pitch, guide vocals, cover (the version: its own video, lyrics, mode lyrics, words, pitch, vocals)."""
    jid = make_song(artist, title, version="Live take")
    job = A.load(jid)
    base = job["published_as"]
    vbase = base.replace("(Music Video)", "(Live take)")
    open(A.job_path(jid, "cover.jpg"), "wb").write(b"\xff\xd8\xff" + b"0" * 40)
    job["album_meta"] = {"status": "found", "album": "An Album", "year": 2001, "genre": "Pop"}
    A.save(jid, job)
    for b in (base, vbase):
        touch(b + ".both.ass", b"[Script Info]\n")
        touch(b + CAT.VOCALS_EXT)
    A.update_catalog()
    return jid, base, vbase


def remove(body, headers=None):
    return jcall(A.app, "POST", "/api/library/remove", body, headers)


class RemoveFromLibrary(unittest.TestCase):
    def test_exactly_the_songs_files_go_and_it_can_be_published_again(self):
        jid, base, vbase = full_song("Remo Vale", "Gone Song")
        twin = make_song("remo vale", "gone song")                  # the same video, another name (lower case)
        similar = make_song("Remo Vale", "Gone Song Two")            # a name that starts like it
        touch(base.replace("(Music Video)", "(Music Video) (Acoustic)") + ".mp4")   # put there by hand
        touch(base + ".notes.txt")                                   # a stranger with the song's name in front
        e = entry(jid)
        self.assertEqual(e["cover"], base + CAT.COVER_EXT)
        mine = CAT.files_of(e)
        for want in [base + x for x in (".mp4", ".ass", ".both.ass", ".words.json", ".pitch.json", ".vocals.m4a", ".cover.jpg")] \
                + [vbase + x for x in (".mp4", ".ass", ".both.ass", ".words.json", ".pitch.json", ".vocals.m4a")]:
            self.assertIn("Music Videos/" + want, mine)              # (every kind of file is there to be removed)
            self.assertTrue(os.path.exists(mv(want)), want)
        before = lib_files()

        st, _, pv = jcall(A.app, "GET", f"/api/library/remove-preview?job={jid}")
        self.assertEqual(st, 200, pv)
        self.assertEqual(set(pv["files"]), mine)
        self.assertEqual((pv["job"], pv["studio"], pv["versions"]), (jid, True, ["Live take"]))
        self.assertEqual(lib_files(), before)                          # a preview deletes nothing

        st, _, r = remove({"job": jid}, {"X-authentik-username": "mia"})
        self.assertEqual(st, 200, r)
        self.assertEqual(set(r["removed"]), mine)
        self.assertEqual(before - lib_files(), mine)                   # those files, and nothing else
        self.assertEqual(lib_files(), before - mine)
        self.assertIsNone(entry(jid))
        for other in (twin, similar):                                  # the twin and the look-alike stay listed
            self.assertIsNotNone(entry(other), other)
            self.assertTrue(os.path.exists(mv(A.load(other)["published_as"] + ".mp4")))
        self.assertIsNone(entry(base=vbase))
        job = A.load(jid)                                               # the studio copy stays, marked
        self.assertIsNone(job["published"])
        self.assertIsNone(job["published_as"])
        self.assertEqual((job["library_removed"]["by"], job["library_removed"]["base"]), ("mia", base))
        self.assertIn("removed from the library by mia", job["log"][-1])
        self.assertFalse([m for m in A.videoswap.entries(A.job_path(jid)) if m.get("published")])
        self.assertEqual(jcall(A.app, "GET", f"/api/jobs/{jid}")[2]["library_removed"]["by"], "mia")
        self.assertEqual(remove({"job": jid})[0], 400)                 # not in the library any more

        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/publish", {"remove": []})   # Publish works again
        self.assertEqual(st, 200, b)
        self.assertTrue(os.path.exists(mv(base + ".mp4")))
        self.assertIsNotNone(entry(jid))
        self.assertNotIn("library_removed", A.load(jid))

    def test_and_delete_the_studio_copy_too(self):
        jid = make_song("Remo Vale", "Delete Both")
        base = A.load(jid)["published_as"]
        st, _, r = remove({"job": jid, "delete_job": True})
        self.assertEqual(st, 200, r)
        self.assertEqual(r["job_deleted"], jid)
        self.assertFalse(os.path.exists(os.path.join(A.JOBS, jid)))
        self.assertFalse(os.path.exists(mv(base + ".mp4")))
        self.assertIsNone(entry(jid))
        self.assertIsNone(entry(base=base))                            # not kept as a "job deleted" song either

    def test_a_song_with_no_studio_copy(self):
        jid = make_song("Remo Vale", "Orphan Song")
        base = A.load(jid)["published_as"]
        self.assertEqual(jcall(A.app, "DELETE", f"/api/jobs/{jid}")[0], 200)   # the files and catalog entry stay
        e = entry(base=base)
        self.assertTrue(e["job_deleted"])
        mine, before = CAT.files_of(e), lib_files()
        st, _, pv = jcall(A.app, "GET", "/api/library/remove-preview?" + urllib.parse.urlencode({"file": f"Music Videos/{base}.mp4"}))
        self.assertEqual((st, pv["kind"], pv["studio"], pv["job"]), (200, "catalog", False, None))
        self.assertEqual(remove({"base": base, "delete_job": True})[0], 400)    # nothing to delete in the studio
        self.assertEqual(lib_files(), before)
        st, _, r = remove({"base": base})
        self.assertEqual(st, 200, r)
        self.assertEqual(before - lib_files(), mine)
        self.assertIsNone(entry(base=base))
        self.assertEqual(remove({"base": base})[0], 404)

    def test_a_file_the_catalog_doesnt_list(self):
        for n in ("Hand Placed - Tune (Music Video).mp4", "Hand Placed - Tune (Music Video).ass", "Hand Placed - Tune (Music Video).cdg",
                  "Hand Placed - Tune (Music Video) (Live).mp4", "Hand Placed - Tune (Music Video) (Live).ass"):
            touch(n)
        old = "Hand Placed - Tune (Karaoke Version)---zzzzzzzzzzz"
        for ext in (".mp4", ".ass"):
            open(os.path.join(A.LIB, old + ext), "wb").write(b"x")
        before = lib_files()
        st, _, r = remove({"file": "Music Videos/Hand Placed - Tune (Music Video).mp4"})
        self.assertEqual(st, 200, r)
        self.assertEqual(before - lib_files(), {"Music Videos/Hand Placed - Tune (Music Video)" + x for x in (".mp4", ".ass", ".cdg")})
        st, _, r = remove({"file": old + ".mp4"})                      # an old download in the library root
        self.assertEqual(st, 200, r)
        self.assertEqual(before - lib_files(), {"Music Videos/Hand Placed - Tune (Music Video)" + x for x in (".mp4", ".ass", ".cdg")}
                         | {old + ".mp4", old + ".ass"})
        self.assertTrue(os.path.exists(mv("Hand Placed - Tune (Music Video) (Live).mp4")))
        st, _, r = remove({"base": "Hand Placed - Tune (Music Video) (Live)"})   # by its name, as ks does
        self.assertEqual(st, 200, r)
        self.assertEqual(set(r["removed"]), {"Music Videos/Hand Placed - Tune (Music Video) (Live)" + x for x in (".mp4", ".ass")})

    def test_bad_paths_and_bodies(self):
        secret = os.path.join(_env.ROOT, "secret-remove.mp4")
        open(secret, "wb").write(b"no")
        os.makedirs(os.path.join(A.LIB, "Linked"), exist_ok=True)
        os.symlink(_env.ROOT, os.path.join(A.LIB, "Linked", "out"))
        self.addCleanup(shutil.rmtree, os.path.join(A.LIB, "Linked"))
        before = lib_files()
        for bad in ("../secret-remove.mp4", "/etc/passwd", "Music Videos/../../secret-remove.mp4", "Linked/out/secret-remove.mp4",
                    "Music Videos\\x.mp4", ".hidden.mp4", "", "catalog.json"):
            with self.subTest(bad=bad):
                st, _, b = remove({"file": bad})
                self.assertIn(st, (400, 404), b)
        for bad in ("../secret-remove", "../../secret-remove", "Linked/out/secret-remove", "..", "Music Videos\\x"):
            with self.subTest(base=bad):
                st, _, b = remove({"base": bad})
                self.assertIn(st, (400, 404), b)
        self.assertEqual(remove({})[0], 400)
        self.assertEqual(remove({"job": "20000101-000000-no-such-song"})[0], 404)
        self.assertTrue(os.path.exists(secret))
        self.assertEqual(lib_files(), before)

    def test_who_may_remove(self):
        jid = make_song("Remo Vale", "Guarded Song")
        base = A.load(jid)["published_as"]
        tok = pair(name="Remover kit")
        for path, body in (("/api/library/remove", {"job": jid}), ("/api/library/refresh", {})):
            with self.subTest(path=path):     # a player's token: refused, whatever lets it through to this API
                st, _, b = jcall(A.app, "POST", path, body, {"Authorization": "Bearer " + tok})
                self.assertEqual(st, 403, b)
                self.assertIn("players", b["detail"])
        import ipaddress
        A._ONLY_NETS[:] = [ipaddress.ip_network("192.0.2.0/24")]       # the player-only network: nothing but /api/encore/
        try:
            self.assertEqual(call(A.app, "POST", "/api/library/remove", {"job": jid}, server=("192.0.2.1", 5570))[0], 403)
        finally:
            A._ONLY_NETS[:] = []
        self.assertFalse([r.path for r in A.app.routes if getattr(r, "path", "").startswith("/api/encore/")
                          and ("remove" in r.path or "library" in r.path)])     # (their API has nothing of the kind)
        # agents: the publish scope; deleting the studio copy too needs delete as well
        key, _ = A.KEYS.create("No publish", [s for s in agents.SCOPE_NAMES if s != "publish"])
        st, _, b = remove({"job": jid}, {"X-Studio-Agent-Key": key})
        self.assertEqual((st, b.get("scope")), (403, "publish"))
        key, _ = A.KEYS.create("Publisher", ["read", "publish"])
        st, _, b = remove({"job": jid, "delete_job": True}, {"X-Studio-Agent-Key": key})
        self.assertEqual(st, 403)
        self.assertIn("delete", b["detail"])
        self.assertTrue(os.path.exists(mv(base + ".mp4")))             # nothing went on any refusal
        self.assertIsNotNone(entry(jid))
        st, _, r = remove({"job": jid}, {"X-Studio-Agent-Key": key})
        self.assertEqual(st, 200, r)
        self.assertEqual(A.load(jid)["library_removed"]["by"], "Publisher")

    def test_refused_while_its_video_is_being_made(self):
        jid = make_song("Remo Vale", "Busy Song")
        job = A.load(jid)
        A.claim_video(job, "render")
        A.save(jid, job)
        self.assertEqual(remove({"job": jid})[0], 409)
        self.assertTrue(os.path.exists(mv(job["published_as"] + ".mp4")))


class RefreshLibrary(unittest.TestCase):
    def refresh(self):
        st, _, r = jcall(A.app, "POST", "/api/library/refresh", {})
        self.assertEqual(st, 200, r)
        return r

    def test_finds_what_changed_by_hand_and_is_idempotent(self):
        a = make_song("Fresh Kite", "Hand Removed")
        b = make_song("Fresh Kite", "Hand Changed")
        ba, bb = A.load(a)["published_as"], A.load(b)["published_as"]
        name_a, name_b = "Fresh Kite - Hand Removed", "Fresh Kite - Hand Changed"
        self.refresh()                                     # settle whatever other tests left
        p = os.path.join(A.LIB, CAT.CATALOG)
        raw, mt = open(p, "rb").read(), os.stat(p).st_mtime_ns
        r = self.refresh()
        self.assertEqual((r["written"], r["added"], r["removed"], r["changed"]), (False, [], [], []))
        self.assertEqual((open(p, "rb").read(), os.stat(p).st_mtime_ns), (raw, mt))   # same bytes: the same ETag
        self.assertIn("Nothing changed", r["message"])

        keep = os.path.join(_env.ROOT, "kept.mp4")
        shutil.move(mv(ba + ".mp4"), keep)                  # removed by hand on the server
        touch(bb + ".romaji.ass", b"[Script Info]\n")       # a lyrics file added by hand
        touch("Nobody Made - Dropped In (Music Video).mp4")
        self.addCleanup(os.remove, mv("Nobody Made - Dropped In (Music Video).mp4"))
        r = self.refresh()
        self.assertTrue(r["written"])
        self.assertIn(name_a, r["removed"])
        self.assertIn(name_b, r["changed"])
        self.assertNotIn(name_b, r["added"] + r["removed"])
        self.assertIn("Nobody Made - Dropped In (Music Video).mp4", r["unlisted"])   # no studio song: can't be listed
        self.assertIsNone(entry(a))
        self.assertIn("romaji", entry(b)["lyrics"]["modes"])
        self.assertEqual(self.refresh()["written"], False)  # again: nothing

        shutil.move(keep, mv(ba + ".mp4"))                   # put back by hand
        r = self.refresh()
        self.assertEqual((r["added"], r["removed"], r["changed"]), ([name_a], [], []))
        self.assertIsNotNone(entry(a))

    def test_plays_alone_are_no_change(self):
        jid = make_song("Fresh Kite", "Played")
        self.refresh()
        old = CAT.read_catalog(A.LIB)
        new = json.loads(json.dumps(old["songs"]))
        for e in new:
            e["plays"] = (e.get("plays") or 0) + 3
        self.assertEqual(CAT.diff(old["songs"], new), {"added": [], "removed": [], "changed": []})
        self.assertIsNotNone(entry(jid))

    def test_waits_for_a_publish(self):
        make_song("Fresh Kite", "Waiting")
        out = {}
        with A.lock:                                         # a publish (or a removal) is going on
            t = threading.Thread(target=lambda: out.update(r=jcall(A.app, "POST", "/api/library/refresh", {})))
            t.start()
            t.join(0.5)
            self.assertTrue(t.is_alive())
        t.join(30)
        self.assertEqual(out["r"][0], 200)


if __name__ == "__main__":
    unittest.main()
