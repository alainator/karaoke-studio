"""ks align and the new ks look rows: which lines are aligned together over which window, the report (start vs now,
confidence, the line before moving too), the Studio's gate (never while a song is made, one at a time: "busy"), the
melody row from pitch.f0.npz (only an up-to-date one) and the aligner row. No GPU: the aligner process is faked."""
import importlib.machinery, importlib.util, json, os, subprocess, tempfile, threading, time, types, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
from tests import _asgi
import app as A  # noqa: E402
import agents  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_loader = importlib.machinery.SourceFileLoader("ks_tool_align", os.path.join(HERE, "tools", "ks"))
_spec = importlib.util.spec_from_loader("ks_tool_align", _loader)
ks = importlib.util.module_from_spec(_spec)
_loader.exec_module(ks)
K = A._ks()


def L(t, n=2, **kw):
    return dict({"words": [[t + k * 0.5, t + k * 0.5 + 0.4, f"w{k}"] for k in range(n)]}, **kw)


LINES = [L(10.0), L(13.0), L(13.5, 1, backing=True), L(16.0, 3), L(19.0), L(80.0)]


class PlanTests(unittest.TestCase):
    def test_line_with_the_lead_line_before_to_the_next_start(self):
        p = K.align_plan(LINES, 0.0, n=3)
        self.assertEqual(p["lines"], [1, 3])                 # the backing line in between isn't the line before
        self.assertEqual((p["target"], p["prev"]), (3, 1))
        self.assertEqual(p["window"], [12.7, 18.95])         # prev start - 0.3 .. next lead start - 0.05

    def test_offset_is_video_time(self):
        p = K.align_plan(LINES, 1.5, n=1)
        self.assertEqual(p["window"], [11.2, 17.45])

    def test_first_line_and_last_line(self):
        p = K.align_plan(LINES, 0.0, n=0)
        self.assertEqual((p["lines"], p["prev"]), ([0], None))
        self.assertEqual(p["window"][0], 7.7)
        p = K.align_plan(LINES[:5], 0.0, n=4, duration=21.0)
        self.assertEqual(p["window"], [15.7, 21.0])          # not past the song's end

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, "backing"):
            K.align_plan(LINES, 0.0, n=2)
        with self.assertRaisesRegex(ValueError, "long break"):
            K.align_plan(LINES, 0.0, n=5)                      # the line before starts 61 s earlier
        with self.assertRaisesRegex(ValueError, "doesn't exist"):
            K.align_plan(LINES, 0.0, n=9)

    def test_window_takes_the_whole_lead_lines_inside(self):
        p = K.align_plan(LINES, 0.0, t0=12.5, t1=20.0)
        self.assertEqual((p["lines"], p["target"]), ([1, 3, 4], None))


class ReportTests(unittest.TestCase):
    def report(self, prev_at, cur_at):
        plan = K.align_plan(LINES, 0.0, n=3)
        got = [(prev_at, prev_at + 0.3, 0.6), (prev_at + 0.5, prev_at + 0.8, 0.5),
               (cur_at, cur_at + 0.3, 0.3), (cur_at + 0.5, cur_at + 0.8, 0.1), None]
        r = dict(plan, device="cpu", seconds=1.0, lines=K.align_report(LINES, 0.0, plan, got))
        return r, ks.align_text(r)

    def test_earlier_line(self):
        r, out = self.report(13.0, 15.1)
        t = r["lines"][1]
        self.assertEqual((t["line"], t["now"], t["at"], t["delta"], t["conf"]), (4, 16.0, 15.1, -0.9, 0.2))
        self.assertIsNone(t["words"][2]["at"])
        self.assertIn("0.9 s earlier than now", out)
        self.assertIn("boundary: aligner 0:13.80 / 0:15.10 (line 2 ends, line 4 starts); now line 2 ends 0:13.90, "
                      "line 4 starts 0:16.00", out)
        self.assertIn("ks stretch SONG 4 --start 0:15.10", out)
        self.assertNotIn("SONG 2 --end", out)              # the line before ends before the new start: left alone
        self.assertIn("evidence, not a fix", out)

    def test_the_line_before_is_cut_first_when_it_runs_into_the_new_start(self):
        _, out = self.report(13.0, 13.6)
        self.assertIn("ks stretch SONG 2 --end 0:13.55; ks stretch SONG 4 --start 0:13.60", out)

    def test_later_start_is_left(self):
        _, out = self.report(13.0, 16.9)
        self.assertIn("later: lines may light up early", out)
        self.assertNotIn("ks stretch", out)

    def test_the_line_before_moving_too_is_called_out(self):
        _, out = self.report(11.9, 15.1)
        self.assertIn("the line before moves too (-1.1 s)", out)
        self.assertNotIn("ks stretch", out)

    def test_agrees(self):
        _, out = self.report(13.0, 16.1)
        self.assertIn("agrees with where the line starts now", out)


def make_song(lines=LINES, **kw):
    jid = time.strftime("%Y%m%d-%H%M%S") + f"-align-{os.urandom(3).hex()}"
    d = f"{A.JOBS}/{jid}"
    os.makedirs(d)
    json.dump({"version": 2, "lines": lines}, open(f"{d}/words.json", "w"))
    A.save(jid, {"id": jid, "yt": "abcdefghijk", "lrc_id": 1, "lyrics_source": "lrclib", "artist": "Tester",
                 "title": "Align " + jid[-6:], "status": "done", "step": "done", "created": time.time(), "offset": 0.0,
                 "result": {"duration": 90.0, "lines": len(lines)}, "log": [], **kw})
    return jid, d


def fake_aligner(calls):
    def run(cmd, **kw):
        if len(cmd) > 3 and cmd[2].endswith("ks_helper.py") and cmd[3] == "align":
            calls.append(cmd)
            plan = json.loads(cmd[7])
            lines = [{"line": i + 1, "now": LINES[i]["words"][0][0], "at": LINES[i]["words"][0][0] - 0.5, "delta": -0.5,
                      "conf": 0.4, "end": None, "words": [{"w": w[2], "now": w[0], "at": w[0] - 0.5, "end": w[1] - 0.5, "conf": 0.4}
                                                          for w in LINES[i]["words"]]} for i in plan["lines"]]
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(dict(plan, device=cmd[6], seconds=0.1, lines=lines)), stderr="")
        return REAL_RUN(cmd, **kw)
    return run


REAL_RUN = subprocess.run


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        A.subprocess.run = fake_aligner(self.calls)
        self.free, self.index = A.gpu_free_mib, A.job_index
        A.gpu_free_mib = lambda: 30000
        A.job_index = lambda: []          # (other tests' songs in the shared folder may be queued)
        self.gpu_env = (A.C.USE_LOCAL_GPU, os.environ.get("KARAOKE_DEVICE"))
        A.C.USE_LOCAL_GPU = True          # (other tests may have turned the GPU off or forced a device)
        os.environ["KARAOKE_DEVICE"] = ""

    def tearDown(self):
        A.subprocess.run = REAL_RUN
        A.gpu_free_mib, A.job_index = self.free, self.index
        A.C.USE_LOCAL_GPU = self.gpu_env[0]
        if self.gpu_env[1] is None:
            os.environ.pop("KARAOKE_DEVICE", None)
        else:
            os.environ["KARAOKE_DEVICE"] = self.gpu_env[1]
        A.slots["local"] = None

    def test_align_runs_on_the_gpu_with_room(self):
        jid, _ = make_song()
        st, _, b = _asgi.jcall(A.app, "GET", f"/api/jobs/{jid}/agent/align?line=4")
        self.assertEqual(st, 200, b)
        self.assertEqual(b["target"], 3)
        self.assertEqual(self.calls[0][6], "cuda")
        self.assertEqual([x["line"] for x in b["lines"]], [2, 4])

    def test_cpu_when_the_card_is_full(self):
        A.gpu_free_mib = lambda: 100
        jid, _ = make_song()
        st, _, b = _asgi.jcall(A.app, "GET", f"/api/jobs/{jid}/agent/align?t0=12.5&t1=20")
        self.assertEqual(st, 200, b)
        self.assertEqual(self.calls[0][6], "cpu")

    def test_busy_while_a_song_is_made_or_another_align_runs(self):
        jid, _ = make_song()
        A.slots["local"] = "someone-else"
        st, _, b = _asgi.jcall(A.app, "GET", f"/api/jobs/{jid}/agent/align?line=4")
        self.assertEqual(st, 503)
        self.assertIn("busy, try in a moment", b["detail"])
        A.slots["local"] = None
        with A.ALIGN_LOCK:
            st, _, b = _asgi.jcall(A.app, "GET", f"/api/jobs/{jid}/agent/align?line=4")
        self.assertEqual(st, 503)
        self.assertEqual(self.calls, [])

    def test_queued_song_means_busy(self):
        jid, _ = make_song()
        A.job_index = lambda: [{"id": "x", "status": "queued"}]
        st, _, _ = _asgi.jcall(A.app, "GET", f"/api/jobs/{jid}/agent/align?line=4")
        self.assertEqual(st, 503)

    def test_bad_line_is_400_and_read_scope_is_enough(self):
        jid, _ = make_song()
        st, _, b = _asgi.jcall(A.app, "GET", f"/api/jobs/{jid}/agent/align?line=3")
        self.assertEqual(st, 400)
        self.assertIn("backing", b["detail"])
        self.assertEqual(agents.scope_for("GET", f"/api/jobs/{jid}/agent/align"), "read")


class LookRowsTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        A.subprocess.run = fake_aligner(self.calls)
        self.free, self.index = A.gpu_free_mib, A.job_index
        A.gpu_free_mib = lambda: 30000
        A.job_index = lambda: []          # (other tests' songs in the shared folder may be queued)
        self.gpu_env = (A.C.USE_LOCAL_GPU, os.environ.get("KARAOKE_DEVICE"))
        A.C.USE_LOCAL_GPU = True          # (other tests may have turned the GPU off or forced a device)
        os.environ["KARAOKE_DEVICE"] = ""

    def tearDown(self):
        A.subprocess.run = REAL_RUN
        A.gpu_free_mib, A.job_index = self.free, self.index
        A.C.USE_LOCAL_GPU = self.gpu_env[0]
        if self.gpu_env[1] is None:
            os.environ.pop("KARAOKE_DEVICE", None)
        else:
            os.environ["KARAOKE_DEVICE"] = self.gpu_env[1]
        A.slots["local"] = None

    def song(self, pitch=True, stale=False):
        import numpy as np
        from PIL import Image
        jid, d = make_song()
        Image.new("RGB", (90 * 50, 96), (20, 10, 30)).save(f"{d}/spectro.png")
        with open(f"{d}/vocals.m4a", "wb") as f:
            f.write(b"\0" * 100)
        json.dump({"lang": "en", "words": [[16.1, 16.4, "w0"]]}, open(f"{d}/heard.json", "w"))
        if pitch:
            st = os.stat(f"{d}/vocals.m4a")
            hop, n = 0.02, int(90 / 0.02)
            t = np.arange(n) * hop
            f0 = np.where((t % 1.0) < 0.5, 220.0, 247.0)          # a new note every half second
            per = np.where((t > 15) & (t < 18), 0.9, 0.1)
            np.savez_compressed(f"{d}/pitch.f0.npz", f0=f0.astype(np.float32), per=per.astype(np.float16), hop=np.float32(hop),
                                stamp=np.array(f"{st.st_size}:{int(st.st_mtime) + (5 if stale else 0)}"), model=np.array("full"))
        return jid, d

    def test_melody_and_aligner_rows(self):
        jid, _ = self.song()
        st, h, body = _asgi.call(A.app, "GET", f"/api/jobs/{jid}/agent/look?t0=12&t1=20&line=4&align=1")
        self.assertEqual(st, 200)
        self.assertEqual(body[:4], b"\x89PNG")
        self.assertEqual(h["x-rows"], "spectrogram,melody,lines,aligner,heard")
        self.assertEqual(h["x-align"], "ok")

    def test_no_melody_without_an_up_to_date_pitch_file(self):
        for kw in ({"pitch": False}, {"stale": True}):
            jid, _ = self.song(**kw)
            st, h, _ = _asgi.call(A.app, "GET", f"/api/jobs/{jid}/agent/look?t0=12&t1=20&line=4")
            self.assertEqual(st, 200)
            self.assertEqual(h["x-rows"], "spectrogram,lines,heard")

    def test_busy_aligner_still_draws_the_picture(self):
        jid, _ = self.song()
        A.slots["local"] = "making"
        st, h, _ = _asgi.call(A.app, "GET", f"/api/jobs/{jid}/agent/look?t0=12&t1=20&line=4&align=1")
        self.assertEqual(st, 200)
        self.assertEqual(h["x-align"], "busy")
        self.assertNotIn("aligner", h["x-rows"])

    def test_melody_note_starts(self):
        import numpy as np
        t = np.arange(200) * 0.02
        f0 = np.where(t < 2.0, 220.0, 262.0)
        per = np.where((t > 0.5) & (t < 3.5), 0.9, 0.0)
        pts, starts = K.melody_marks(f0, per, 0, 200)
        self.assertEqual(len(pts), sum(per > 0.5))
        self.assertEqual(len(starts), 2)                      # singing starts, then one pitch change (3 semitones)
        self.assertAlmostEqual(starts[1] * 0.02, 2.0, places=1)


class KeyFileTests(unittest.TestCase):
    def test_key_file_env(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "agent.key")
        with open(p, "w") as f:
            f.write("ksa_example\n")
        os.chmod(p, 0o600)
        env = {k: v for k, v in os.environ.items() if k != "STUDIO_AGENT_KEY"}
        env["STUDIO_AGENT_KEY_FILE"] = p
        old = dict(os.environ)
        try:
            os.environ.clear(); os.environ.update(env)
            self.assertEqual(ks.agent_key(), "ksa_example")
            os.environ["STUDIO_AGENT_KEY_FILE"] = os.path.join(d, "missing")
            with self.assertRaises(SystemExit):
                ks.agent_key()
        finally:
            os.environ.clear(); os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
