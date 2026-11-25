"""The melody that follows the lead singer (pitch.lead_merge, frames(lead=True)): where the lead stem sings something
else than the whole vocal track's pitch, its pitch is taken; silence, octave slips and blips aren't. No GPU, no
karaoke model: the cache is written by the test."""
import os, tempfile, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import numpy as np  # noqa: E402
import pitch as P  # noqa: E402

N = 200
HZ = lambda midi: 440.0 * 2 ** ((midi - 69) / 12)


def tracks(full=64.0, lead=64.0, lead_db=-20.0, backing_db=-30.0, at=(50, 100)):
    """The whole track on `full` everywhere; the lead stem singing `lead` in frames [at) at lead_db (the backing stem
    at backing_db there), silent elsewhere."""
    f0, per = np.full(N, HZ(full)), np.full(N, 0.9)
    lf0, lper, ldb, bdb = np.full(N, HZ(full)), np.full(N, 0.05), np.full(N, -90.0), np.full(N, -30.0)
    a, b = at
    lf0[a:b], lper[a:b], ldb[a:b], bdb[a:b] = HZ(lead), 0.9, lead_db, backing_db
    return f0, per, lf0, lper, ldb, bdb


def midi(f0):
    return 69 + 12 * np.log2(f0 / 440.0)


class LeadMerge(unittest.TestCase):
    def test_a_harmony_on_top_gives_way_to_the_lead(self):
        f0, per = P.lead_merge(*tracks(full=69.0, lead=64.0))   # the tracker on a fourth above, the lead below
        m = midi(f0)
        self.assertTrue(np.allclose(m[55:95], 64.0))
        self.assertTrue(np.allclose(m[:45], 69.0) and np.allclose(m[105:], 69.0))

    def test_no_lead_heard_keeps_the_whole_track(self):
        for kw in ({"lead_db": -60.0}, {"at": (0, 0)},            # too quiet; the group sings it together (no lead)
                   {"backing_db": -19.0}):                         # two equally loud voices: no lead to follow
            f0, _ = P.lead_merge(*tracks(full=69.0, lead=64.0, **kw))
            self.assertTrue(np.allclose(midi(f0), 69.0), kw)

    def test_an_octave_or_the_same_note_isnt_another_voice(self):
        for lead in (76.0, 52.0, 64.4):
            f0, _ = P.lead_merge(*tracks(full=64.0, lead=lead))
            self.assertTrue(np.allclose(midi(f0), 64.0), lead)

    def test_a_blip_is_ignored(self):
        f0, _ = P.lead_merge(*tracks(full=69.0, lead=64.0, at=(80, 82)))
        self.assertTrue(np.allclose(midi(f0), 69.0))

    def test_lengths_may_differ_by_a_frame(self):
        f0, per, lf0, lper, ldb, bdb = tracks(full=69.0, lead=64.0)
        out, p = P.lead_merge(f0, per, lf0[:-1], lper[:-1], ldb[:-1], bdb[:-1])
        self.assertEqual(len(out), N)
        self.assertEqual(len(p), N)


class Frames(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(dir=_env.ROOT)
        open(os.path.join(self.d, "vocals.m4a"), "wb").write(b"vocals " * 10)
        self.saved = (P.lead_model, P.subprocess.run, P.gpu_free_mib)
        P.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("tracked again"))
        P.gpu_free_mib = lambda: 99999

    def tearDown(self):
        P.lead_model, P.subprocess.run, P.gpu_free_mib = self.saved

    def cache(self, lead):
        f0, per, lf0, lper, ldb, bdb = tracks(full=69.0, lead=64.0)
        extra = ({"lead_f0": lf0.astype(np.float32), "lead_per": lper.astype(np.float16), "lead_db": ldb.astype(np.float16),
                  "backing_db": bdb.astype(np.float16), "lead_model": np.array("karaoke.ckpt")} if lead else {})
        np.savez_compressed(os.path.join(self.d, P.CACHE), f0=f0.astype(np.float32), per=per.astype(np.float16),
                            hop=np.float32(P.HOP), stamp=np.array(P._stamp(os.path.join(self.d, "vocals.m4a"))),
                            model=np.array("full"), **extra)

    def test_a_cache_with_the_lead_gives_the_lead_melody_only_when_asked(self):
        self.cache(lead=True)
        self.assertTrue(np.allclose(midi(P.frames(self.d, lead=True)[0])[55:95], 64.0))
        self.assertTrue(np.allclose(midi(P.frames(self.d)[0]), 69.0))   # (a look: the whole track, as before)

    def test_without_the_model_an_old_cache_is_used_as_it_is(self):
        self.cache(lead=False)
        P.lead_model = lambda: None                                    # never downloaded: today's melody
        self.assertTrue(np.allclose(midi(P.frames(self.d, lead=True)[0]), 69.0))

    def test_on_the_cpu_an_old_cache_is_used_as_it_is(self):
        self.cache(lead=False)
        P.lead_model = lambda: "karaoke.ckpt"
        self.assertTrue(np.allclose(midi(P.frames(self.d, lead=True, device="cpu")[0]), 69.0))
        P.gpu_free_mib = lambda: P.LEAD_GPU_MIB - 1                    # a GPU without room for the karaoke model
        self.assertTrue(np.allclose(midi(P.frames(self.d, lead=True)[0]), 69.0))

    def test_an_old_cache_is_tracked_again_with_the_lead_on_a_gpu(self):
        self.cache(lead=False)
        P.lead_model = lambda: "karaoke.ckpt"
        calls = []

        class R:
            returncode, stdout, stderr = 0, "", ""

        def run(cmd, **k):
            calls.append(cmd)
            self.cache(lead=True)
            return R()
        P.subprocess.run = run
        self.assertTrue(np.allclose(midi(P.frames(self.d, lead=True)[0])[55:95], 64.0))
        self.assertEqual(calls[0][-1], "lead")

    def test_keep_backing_songs_dont_ask_for_the_lead(self):
        seen = []
        real = P.frames
        P.frames = lambda *a, **k: (seen.append(k.get("lead")), (_ for _ in ()).throw(RuntimeError("stop")))[1]
        try:
            for kb in (True, False):
                with self.assertRaises(RuntimeError):
                    P.write(self.d, {"keep_backing": kb}, self.d, [])
        finally:
            P.frames = real
        self.assertEqual(seen, [False, True])


if __name__ == "__main__":
    unittest.main()
