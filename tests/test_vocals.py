"""Guide vocals (contract §1 `vocals`): publishing copies the job's vocals.m4a to <base>.vocals.m4a (song and versions),
re-encoding only a file that isn't AAC or is too big; a failure never stops a publish and leaves no stale file; rename,
trim + republish, deleting the job and the catalog keep it in step; players can fetch it; deploy/write-vocals.py fills
it in for songs published before. Real (tiny) audio made with ffmpeg; no GPU or network."""
import json, os, shutil, subprocess, sys, tempfile, time, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import app as A  # noqa: E402
import catalog as CAT  # noqa: E402
import guidevocals as GV  # noqa: E402
import schemalite as S  # noqa: E402
from assgen import save_lines  # noqa: E402
from tests._asgi import call, jcall  # noqa: E402

PRESET = os.environ["ENCORE_TOKEN"]
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
_n = [0]


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


def audio(path, seconds=3.0, kbps=96, codec="aac", freq=330):
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", "-ac", "2", "-c:a", codec,
           *(["-b:a", f"{kbps}k"] if codec == "aac" else []), "-f", "mp4", path)


def video(path, seconds=12.0):
    ffmpeg("-f", "lavfi", "-i", f"testsrc=size=160x90:rate=10:duration={seconds}", "-f", "lavfi", "-i",
           f"sine=frequency=220:duration={seconds}", "-map", "0:v", "-map", "1:a", *A.C.VENC, "-c:a", "aac", "-b:a", "64k",
           "-shortest", "-movflags", "+faststart", path)


def make_song(artist, title, vocals="aac", real_video=False, publish=True, seconds=12.0):
    _n[0] += 1
    jid = f"20260303-{_n[0]:06d}-voc-{_n[0]}"
    d = os.path.join(A.JOBS, jid)
    os.makedirs(d)
    lines = [{"words": [[3.0 + i * 2, 3.4 + i * 2, "la"], [3.5 + i * 2, 3.9 + i * 2, "da"]]} for i in range(3)]
    save_lines(os.path.join(d, "words.json"), lines, [])
    if real_video:
        video(os.path.join(d, "out.mp4"), seconds)
    else:
        open(os.path.join(d, "out.mp4"), "wb").write(b"video " * 20)
    vp = os.path.join(d, "vocals.m4a")
    if vocals == "aac":
        audio(vp, seconds)
    elif vocals == "big":
        audio(vp, seconds, kbps=320)
    elif vocals == "alac":
        audio(vp, seconds, codec="alac")
    elif vocals == "junk":
        open(vp, "wb").write(b"placeholder " * 10)
    json.dump({"offset": 0.0, "lines": 3, "duration": seconds, "lang": "en"}, open(os.path.join(d, "result.json"), "w"))
    job = {"id": jid, "yt": "aaaaaaaaaaa", "artist": artist, "title": title, "status": "done", "step": "done",
           "created": time.time(), "finished": time.time(), "offset": 0.0, "result": {"duration": seconds, "lang": "en"}, "log": []}
    A.save(jid, job)
    A.rebuild_lyrics(jid, job)
    if publish:
        pub(jid)
    return jid


def pub(jid):
    st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/publish", {"remove": []})
    assert st == 200, b
    return A.load(jid)["published_as"]


def add_version(jid, name="Live take"):
    slug = "v" + str(_n[0])
    vd = os.path.join(A.JOBS, jid, "versions", slug)
    os.makedirs(vd)
    open(os.path.join(vd, "out.mp4"), "wb").write(b"version " * 10)
    json.dump({"name": name, "mode": "version", "slug": slug, "start": 0.0, "offset": 0.0, "trim_start": 0.0,
               "aligned": "manual", "made_at": time.time()}, open(os.path.join(vd, "meta.json"), "w"))
    st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/videos/{slug}/publish", {})
    assert st == 200, b
    return b["name"]


def entry(jid):
    A.update_catalog()
    return next(e for e in CAT.read_catalog(A.LIB)["songs"] if e["id"] == jid)


def lib(name):
    return os.path.join(A.MV_DIR, name)


def same(a, b):
    return open(a, "rb").read() == open(b, "rb").read()


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
class GuideVocals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):  # (patched here, not at import: other test modules patch these their own way)
        cls._saved = (A.pitch_later, A.catalog_soon)
        A.pitch_later = lambda jid: None
        A.catalog_soon = lambda delay=0: A.update_catalog()

    @classmethod
    def tearDownClass(cls):
        A.pitch_later, A.catalog_soon = cls._saved

    def valid(self, schema, data):
        self.assertEqual(S.errors(schema, data), [], json.dumps(data)[:400])

    def test_publish_copies_the_vocals_for_the_song_and_its_versions(self):
        jid = make_song("Vox Artist", "Copied Song")
        base = A.load(jid)["published_as"]
        self.assertTrue(same(lib(base + ".vocals.m4a"), A.job_path(jid, "vocals.m4a")))  # AAC 96 kb/s: copied as is
        self.assertEqual(CAT.VOCALS_EXT, GV.EXT)
        vbase = add_version(jid)
        e = entry(jid)
        self.assertEqual(e["vocals"], base + ".vocals.m4a")
        self.assertEqual(e["versions"][0]["vocals"], vbase + ".vocals.m4a")
        self.assertTrue(same(lib(vbase + ".vocals.m4a"), A.job_path(jid, "vocals.m4a")))
        self.valid("catalog.schema.json", CAT.read_catalog(A.LIB))
        self.assertFalse([f for f in os.listdir(A.MV_DIR) if f.startswith(".")])  # no temporary files left
        # republishing the song refreshes its versions' copies too
        os.remove(lib(vbase + ".vocals.m4a"))
        pub(jid)
        self.assertTrue(os.path.isfile(lib(vbase + ".vocals.m4a")))
        # the Library tab doesn't list it as a song
        self.assertNotIn(f"Music Videos/{base}.vocals.m4a", {s["file"] for s in A.library_songs()})

    def test_only_a_big_or_non_aac_file_is_made_again(self):
        for kind in ("big", "alac"):
            with self.subTest(kind=kind):
                jid = make_song("Vox Artist", f"Encoded {kind}", vocals=kind)
                p = lib(A.load(jid)["published_as"] + ".vocals.m4a")
                codec, kbps = GV.probe(p)
                self.assertEqual(codec, "aac")
                self.assertLessEqual(kbps, GV.MAX_KBPS)
                self.assertFalse(same(p, A.job_path(jid, "vocals.m4a")))
                d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
                                         capture_output=True, text=True).stdout)
                self.assertAlmostEqual(d, 12.0, delta=0.1)  # the same timeline

    def test_a_failure_never_stops_publishing_and_leaves_no_stale_file(self):
        jid = make_song("Vox Artist", "Broken Vocals", vocals="junk", publish=False)
        base = A.final_base(A.load(jid))
        open(lib(base + ".vocals.m4a"), "wb").write(b"an old one")  # from an earlier publish
        self.assertEqual(pub(jid), base)
        self.assertFalse(os.path.exists(lib(base + ".vocals.m4a")))
        self.assertIsNone(entry(jid)["vocals"])
        jid2 = make_song("Vox Artist", "No Vocals", vocals=None)
        self.assertIsNone(entry(jid2)["vocals"])
        self.assertTrue(os.path.isfile(lib(A.load(jid2)["published_as"] + ".mp4")))
        self.assertEqual(GV.write(A.job_path(jid2), A.MV_DIR, [], None), [])

    def test_rename_moves_it(self):
        jid = make_song("Vox Artist", "Old Name")
        old = A.load(jid)["published_as"]
        vold = add_version(jid, "Mia's take")
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/rename", {"artist": "Vox Artist", "title": "New Name"})
        self.assertEqual(st, 200, b)
        new = A.load(jid)["published_as"]
        self.assertNotEqual(new, old)
        for gone, there in ((old, new), (vold, vold.replace("Old Name", "New Name"))):
            self.assertFalse(os.path.exists(lib(gone + ".vocals.m4a")))
            self.assertTrue(os.path.isfile(lib(there + ".vocals.m4a")))
        e = entry(jid)
        self.assertEqual((e["vocals"], e["versions"][0]["vocals"]), (new + ".vocals.m4a", vold.replace("Old Name", "New Name") + ".vocals.m4a"))

    def test_trim_then_publish_again_updates_it(self):
        jid = make_song("Vox Artist", "Trimmed Song", real_video=True)
        base = A.load(jid)["published_as"]
        before = open(lib(base + ".vocals.m4a"), "rb").read()
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/trim", {"start": 1.0, "end": 11.0})
        self.assertEqual(st, 200, b)
        self.assertTrue(A.load(jid)["unpublished_changes"])
        self.assertEqual(open(lib(base + ".vocals.m4a"), "rb").read(), before)  # the published one waits for a publish
        pub(jid)
        self.assertTrue(same(lib(base + ".vocals.m4a"), A.job_path(jid, "vocals.m4a")))
        self.assertNotEqual(open(lib(base + ".vocals.m4a"), "rb").read(), before)
        d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                  lib(base + ".vocals.m4a")], capture_output=True, text=True).stdout)
        self.assertAlmostEqual(d, 10.0, delta=0.15)

    def test_deleting_the_job_keeps_the_published_file_listed(self):
        jid = make_song("Vox Artist", "Deleted Job")
        base = A.load(jid)["published_as"]
        self.assertEqual(jcall(A.app, "DELETE", f"/api/jobs/{jid}")[0], 200)
        e = next(e for e in CAT.read_catalog(A.LIB)["songs"] if e["base"] == base)
        self.assertTrue(e.get("job_deleted"))
        self.assertEqual(e["vocals"], base + ".vocals.m4a")
        os.remove(lib(base + ".vocals.m4a"))
        A.update_catalog()
        self.assertIsNone(next(e for e in CAT.read_catalog(A.LIB)["songs"] if e["base"] == base)["vocals"])

    def test_players_can_fetch_it_with_ranges(self):
        jid = make_song("Vox Artist", "Fetched Song")
        vbase = add_version(jid, "Fetch take")
        base = A.load(jid)["published_as"]
        A.update_catalog()
        auth = {"Authorization": "Bearer " + PRESET}
        for b in (base, vbase):
            p = ("Music Videos/" + b + ".vocals.m4a").replace(" ", "%20").replace("'", "%27")
            st, h, body = call(A.app, "GET", "/api/encore/files/" + p, headers=auth)
            self.assertEqual((st, h["content-type"]), (200, "audio/mp4"))
            self.assertEqual(body, open(lib(b + ".vocals.m4a"), "rb").read())
            st, h, part = call(A.app, "GET", "/api/encore/files/" + p, headers=dict(auth, Range="bytes=4-11"))
            self.assertEqual((st, part), (206, body[4:12]))
        # a vocals file the catalog doesn't name isn't served
        open(lib("Unlisted (Music Video).vocals.m4a"), "wb").write(b"x")
        self.assertEqual(call(A.app, "GET", "/api/encore/files/Music%20Videos/Unlisted%20(Music%20Video).vocals.m4a", headers=auth)[0], 404)

    def test_backfill_script(self):
        a = make_song("Vox Backfill", "Old Publish")
        b = make_song("Vox Backfill", "Changed Since", real_video=True)
        ba, bb = A.load(a)["published_as"], A.load(b)["published_as"]
        for x in (ba, bb):
            os.remove(lib(x + ".vocals.m4a"))
        A.update_catalog()
        # b was made again after publishing (a new video of another length): its vocals don't fit the published one
        video(A.job_path(b, "out.mp4"), 9.0)
        script = os.path.join(_env.REPO, "deploy", "write-vocals.py")
        run = lambda *x: subprocess.run([sys.executable, "-I", "-B", script, "--jobs", f"{a},{b}", *x], capture_output=True,
                                        text=True, env=dict(os.environ), timeout=120)
        r = run("--dry")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("1 song(s) to do", r.stdout)
        self.assertIn(f"skipped {b}", r.stdout)
        self.assertFalse(os.path.exists(lib(ba + ".vocals.m4a")))
        r = run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(same(lib(ba + ".vocals.m4a"), A.job_path(a, "vocals.m4a")))
        self.assertFalse(os.path.exists(lib(bb + ".vocals.m4a")))
        self.assertEqual(entry(a)["vocals"], ba + ".vocals.m4a")
        self.assertIsNone(entry(b)["vocals"])
        r = subprocess.run([sys.executable, "-I", "-B", script, "--dry", "--limit", "1"], capture_output=True, text=True,
                           env=dict(os.environ), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(a, r.stdout.split("to do")[1].split("skipped")[0])  # done already: not listed again


if __name__ == "__main__":
    unittest.main()
