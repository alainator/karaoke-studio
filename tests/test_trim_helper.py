"""A trimmed song remade on the GPU helper comes back cut exactly as when it's made here: the helper never gets the
trim (it renders the whole video), and the studio applies it to the files it copies back, keeping the untrimmed
render here for later trim changes. Uses real (tiny) media made with ffmpeg; no GPU needed."""
import json, os, shutil, subprocess, sys, tempfile, threading, time, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
from tests.test_cancel import Base, new_job, st, wait_for  # (also points the studio and the helper at the stand-in pipeline)
import app as A  # noqa: E402
import config as C  # noqa: E402
import helper as H  # noqa: E402
import trimtools  # noqa: E402
from assgen import load_lines  # noqa: E402

TRIM = {"start": 3.0, "end": 10.5}


def duration(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p], capture_output=True, text=True)
    return float(r.stdout.strip())


class RemoteTrim(Base):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        cls.port = _env.free_port()
        cls.server = uvicorn.Server(uvicorn.Config(H.app, host="127.0.0.1", port=cls.port, log_level="warning"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        wait_for(lambda: cls.server.started, 10)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    def setUp(self):
        super().setUp()
        C.USE_LOCAL_GPU, C.GPU_HELPER_URL, C.HELPER_TOKEN = False, f"http://127.0.0.1:{self.port}", os.environ["HELPER_TOKEN"]
        self.sent = []
        real = A.helper_call

        def spy(path, method="GET", body=None, **kw):
            if path == "/jobs" and method == "POST":
                self.sent.append(dict(body))
            return real(path, method, body, **kw)
        A.helper_call = spy
        self.addCleanup(setattr, A, "helper_call", real)

    def local_reference(self):
        """What a make on this machine leaves: the pipeline's files, then pipeline.py's own trim step."""
        d = tempfile.mkdtemp(prefix="local-trim-")
        self.addCleanup(shutil.rmtree, d, True)
        json.dump({"fake": "media", "trim": TRIM, "offset": 0.0}, open(os.path.join(d, "job.json"), "w"))
        subprocess.run([sys.executable, "-I", _env.FAKE, d], check=True, capture_output=True)
        said = trimtools.reapply(d, TRIM)
        self.assertIn("trimmed to 3.0 s-10.5 s", said)
        return d

    def test_remote_make_is_trimmed_like_a_local_one(self):
        ref = self.local_reference()
        jid = new_job(title="Trimmed fake:media", trim=dict(TRIM))
        A.schedule_once()
        self.assertEqual(A.slots["remote"], jid)
        self.assertTrue(wait_for(lambda: st(jid) == "done", 60), A.load(jid))
        self.assertTrue(self.sent and "trim" not in self.sent[0], self.sent)  # the helper renders the whole video
        got = A.job_path(jid)
        self.assertEqual(load_lines(os.path.join(got, "words.json")), load_lines(os.path.join(ref, "words.json")))
        self.assertEqual(json.load(open(os.path.join(got, "heard.json"))), json.load(open(os.path.join(ref, "heard.json"))))
        r_got, r_ref = json.load(open(os.path.join(got, "result.json"))), json.load(open(os.path.join(ref, "result.json")))
        self.assertEqual((r_got["duration"], r_got["trim"]), (r_ref["duration"], r_ref["trim"]))
        self.assertEqual(r_got["trim"], {"start": 3.0, "end": 10.5, "full": 12.0})
        self.assertAlmostEqual(duration(os.path.join(got, "out.mp4")), duration(os.path.join(ref, "out.mp4")), delta=0.05)
        self.assertAlmostEqual(duration(os.path.join(got, "out.mp4")), 7.5, delta=0.1)
        self.assertAlmostEqual(duration(os.path.join(got, "untrimmed", "out.mp4")), 12.0, delta=0.1)  # kept here
        self.assertEqual(A.load(jid)["result"]["trim"]["start"], 3.0)
        self.assertIn("trimmed to 3.0 s-10.5 s of the video, as before", A.load(jid)["log"])
        # the lines moved with the cut: the first one sang at 4.0 s of the video, now at 1.0 s
        self.assertEqual(load_lines(os.path.join(got, "words.json"))[0]["words"][0][0], 1.0)

    def test_untrimmed_song_unchanged(self):
        jid = new_job(title="Whole fake:media")
        A.schedule_once()
        self.assertTrue(wait_for(lambda: st(jid) == "done", 60), A.load(jid))
        self.assertFalse(os.path.exists(A.job_path(jid, "untrimmed")))
        self.assertAlmostEqual(duration(A.job_path(jid, "out.mp4")), 12.0, delta=0.1)
        self.assertEqual(load_lines(A.job_path(jid, "words.json"))[0]["words"][0][0], 4.0)


if __name__ == "__main__":
    unittest.main()
