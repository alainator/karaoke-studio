"""Song loudness (contract §1 `loudness`, loudness.py): ebur128 measures tones at known levels right, silence is no
measurement, the vocals mix is measured with the vocals file; the record counts only while the published files are
unchanged; publishing measures in the background and the catalog lists it (song and versions); an old record or none
= null and the catalog still validates; deploy/write-loudness.py measures songs published before, skips measured
ones, and stops while a song is being made. Real (tiny) audio made with ffmpeg; no GPU or network."""
import json, os, shutil, subprocess, sys, threading, time, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import app as A  # noqa: E402
import catalog as CAT  # noqa: E402
import loudness as L  # noqa: E402
import schemalite as S  # noqa: E402
from tests import test_vocals as TV  # noqa: E402  (its song helpers)

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class Quiet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):  # (patched here, not at import: other test modules patch these their own way)
        cls._saved = (A.pitch_later, A.catalog_soon)
        A.pitch_later = lambda jid: None
        A.catalog_soon = lambda delay=0: A.update_catalog()

    @classmethod
    def tearDownClass(cls):
        A.pitch_later, A.catalog_soon = cls._saved

    def valid(self, data):
        self.assertEqual(S.errors(S.load("catalog.schema.json"), data), [])


def tone(path, amp, seconds=6.0, freq=1000, video=False):
    """A stereo sine at amplitude amp (1 = full scale) in an .mp4 (with a picture when video) or .m4a."""
    src = ["-f", "lavfi", "-i", f"aevalsrc={amp}*sin(2*PI*{freq}*t)|{amp}*sin(2*PI*{freq}*t):s=48000:d={seconds}"]
    pic = ["-f", "lavfi", "-i", f"color=c=black:s=64x64:r=5:d={seconds}"] if video else []
    maps = ["-map", "1:v", "-map", "0:a", *A.C.VENC] if video else []
    TV.ffmpeg(*src, *pic, *maps, "-c:a", "aac", "-b:a", "192k", "-shortest", "-f", "mp4", path)


def wait_measured():
    for t in list(threading.enumerate()):
        if t.name == "loudness":
            t.join(60)


def entry(jid):
    return next(e for e in CAT.read_catalog(A.LIB)["songs"] if e["id"] == jid)


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
class Measure(unittest.TestCase):
    def setUp(self):
        self.d = os.path.join(_env.ROOT, f"loud-{time.time_ns()}")
        os.makedirs(self.d)

    def test_tones_at_known_levels(self):
        # a 1 kHz sine at amplitude 0.5 in both channels is -6.0 LUFS (BS.1770: 0 dBFS in both = 0 LUFS) and peaks
        # at -6 dBTP; 10 dB quieter reads 10 LU quieter
        tone(os.path.join(self.d, "a.mp4"), 0.5)
        tone(os.path.join(self.d, "b.mp4"), round(0.5 / 10 ** 0.5, 5))
        a, b = L.measure_file(os.path.join(self.d, "a.mp4")), L.measure_file(os.path.join(self.d, "b.mp4"))
        self.assertAlmostEqual(a[0], -6.0, delta=1.0)
        self.assertAlmostEqual(a[1], -6.0, delta=1.5)   # (AAC overshoots a little)
        self.assertAlmostEqual(a[0] - b[0], 10.0, delta=0.3)
        self.assertAlmostEqual(a[1] - b[1], 10.0, delta=0.5)

    def test_silence_and_junk_are_no_measurement(self):
        TV.ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "4", "-c:a", "aac", os.path.join(self.d, "s.m4a"))
        self.assertIsNone(L.measure_file(os.path.join(self.d, "s.m4a")))
        open(os.path.join(self.d, "junk.mp4"), "wb").write(b"not a video " * 10)
        self.assertIsNone(L.measure_file(os.path.join(self.d, "junk.mp4")))
        self.assertIsNone(L.measure(self.d, "junk"))

    def test_parse(self):
        self.assertEqual(L.parse("  I:  -11.8 LUFS\n  Peak:  -0.4 dBFS\n"), (-11.8, -0.4))
        self.assertEqual(L.parse("  I: -70.0 LUFS\n  Peak: -inf dBFS\n"), None)
        self.assertEqual(L.parse("  I: -20.0 LUFS\n  Peak: -inf dBFS\n"), (-20.0, None))
        self.assertIsNone(L.parse(""))

    def test_mix_and_the_record_follows_the_files(self):
        mv, jd = os.path.join(self.d, "mv"), os.path.join(self.d, "job")
        os.makedirs(mv), os.makedirs(jd)
        tone(os.path.join(mv, "S.mp4"), 0.25, video=True)
        self.assertEqual(L.update(jd, mv, "S")[0], "measured")
        cur = L.current(jd, mv, "S")
        self.assertIsNone(cur["mix_lufs"])                       # no vocals file
        tone(os.path.join(mv, "S" + L.VOCALS_EXT), 0.25, freq=440)
        state, cur = L.update(jd, mv, "S")                       # vocals came: measured again, with the mix
        self.assertEqual(state, "measured")
        self.assertGreater(cur["mix_lufs"], cur["lufs"] + 2)    # two tones as loud as each other: about +3 LU
        self.assertEqual(set(cur), {"v", "lufs", "peak_db", "mix_lufs", "mix_peak_db"})
        self.assertEqual(L.update(jd, mv, "S")[0], "kept")
        # the vocals change: the mix no longer counts; the video changes: nothing counts
        tone(os.path.join(mv, "S" + L.VOCALS_EXT), 0.1, seconds=5.0, freq=440)
        self.assertIsNone(L.current(jd, mv, "S")["mix_lufs"])
        tone(os.path.join(mv, "S.mp4"), 0.25, seconds=4.0, video=True)
        self.assertIsNone(L.current(jd, mv, "S"))
        self.assertIsNone(L.current(jd, mv, "Other"))


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
class Publish(Quiet):
    def test_publish_measures_and_the_catalog_lists_it(self):
        jid = TV.make_song("Loud Band", "Loud Song", real_video=True)
        wait_measured()
        base = A.load(jid)["published_as"]
        self.assertTrue(os.path.exists(A.job_path(jid, L.FILE)))
        e = entry(jid)
        self.assertEqual(e["loudness"]["v"], 1)
        self.assertLess(e["loudness"]["lufs"], -10)             # (ffmpeg's sine source: 1/8 of full scale)
        self.assertIsNotNone(e["loudness"]["mix_lufs"])          # it has guide vocals
        vb = TV.add_version(jid)
        wait_measured()
        v = next(x for x in entry(jid)["versions"] if x["base"] == vb)
        self.assertEqual(v["loudness"], entry(jid)["loudness"])  # a version plays over the song's own sound
        self.valid(CAT.read_catalog(A.LIB))
        # the published video replaced by hand (or a remake published elsewhere): null until measured again
        TV.video(os.path.join(A.MV_DIR, base + ".mp4"), 7.0)
        A.update_catalog()
        self.assertIsNone(entry(jid)["loudness"])
        TV.pub(jid)
        wait_measured()
        self.assertIsNotNone(entry(jid)["loudness"])

    def test_without_a_measurement_null_and_valid(self):
        jid = TV.make_song("Quiet Band", "Fake Video")           # (not a real video: nothing to measure)
        wait_measured()
        self.assertIsNone(entry(jid)["loudness"])
        old = {"version": 2, "songs": [dict(e) for e in CAT.read_catalog(A.LIB)["songs"]]}
        for e in old["songs"]:                                   # a catalog written before the field existed
            e.pop("loudness", None)
            for v in e.get("versions") or []:
                v.pop("loudness", None)
        self.valid(old)


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
class Backfill(Quiet):
    script = os.path.join(_env.REPO, "deploy", "write-loudness.py")

    def sandbox(self, *jids):
        """A copy of these songs' jobs and published files (other tests leave songs "running" in the shared jobs)."""
        root = os.path.join(_env.ROOT, f"backfill-{time.time_ns()}")
        jobs, mv = os.path.join(root, "jobs"), os.path.join(root, "library", "Music Videos")
        os.makedirs(jobs), os.makedirs(mv)
        for jid in jids:
            shutil.copytree(A.job_path(jid), os.path.join(jobs, jid))
            base = A.load(jid).get("published_as")
            for f in os.listdir(A.MV_DIR):
                if base and f.startswith(base + "."):
                    shutil.copy(os.path.join(A.MV_DIR, f), mv)
        self.jobs, self.lib = jobs, os.path.join(root, "library")
        return jobs, mv

    def run_it(self, *x):
        return subprocess.run([sys.executable, "-I", "-B", self.script, "--jobs", self.jobs, "--library", self.lib, *x],
                              capture_output=True, text=True, env=dict(os.environ), timeout=120)

    def song(self, jid):
        return next(e for e in CAT.read_catalog(self.lib)["songs"] if e["id"] == jid)

    def test_measures_old_songs_and_skips_measured_ones(self):
        a = TV.make_song("Backfill Band", "Old Song", real_video=True)
        wait_measured()
        jobs, _ = self.sandbox(a)
        os.remove(os.path.join(jobs, a, L.FILE))                 # published before the studio measured it
        r = self.run_it("--dry")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"{a}:", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(jobs, a, L.FILE)))
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("LUFS", r.stdout)
        self.assertIsNotNone(self.song(a)["loudness"]["lufs"])
        r = self.run_it("--dry")
        self.assertIn("0 song(s) to measure", r.stdout)          # measured: skipped
        self.assertIn(f"{a}:", self.run_it("--dry", "--force").stdout)

    def test_stops_while_a_song_is_being_made(self):
        a = TV.make_song("Backfill Band", "Waiting Song", real_video=True)
        busy = TV.make_song("Backfill Band", "Being Made", publish=False)
        wait_measured()
        jobs, _ = self.sandbox(a, busy)
        os.remove(os.path.join(jobs, a, L.FILE))
        p = os.path.join(jobs, busy, "job.json")
        json.dump(dict(json.load(open(p)), status="running"), open(p, "w"))
        r = self.run_it()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("a song is being made", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(jobs, a, L.FILE)))


if __name__ == "__main__":
    unittest.main()
