"""Sharing the GPU with other programs (gpuwatch.py): reading the card (made-up nvidia-smi output), the start check and
who holds the card, pressure while a song is made, the pipeline's step gate and resume cache; then the studio's
state machine with tests/fake_pipeline.py: a song paused by another program goes back to the front of its queue and
resumes reusing what was done, "no room" and "out of GPU memory" pause instead of failing, priorities (songs made by
hand before an import's), an import far over 50 songs, and a pause on the helper PC. No GPU needed."""
import json, os, shutil, stat, tempfile, threading, time, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
from tests.test_cancel import Base, alive, new_job, pids, st, wait_for, R, run  # noqa: E402
import app as A  # noqa: E402
import config as C  # noqa: E402
import gpu  # noqa: E402
import gpuwatch as GW  # noqa: E402
import helper as H  # noqa: E402

GiB = 1024


def snap(used, total=32 * GiB, util=0, apps=()):
    return {"name": "Fake GPU", "total": total, "used": used, "free": total - used, "util": util, "apps": list(apps)}


class ReadTheCard(unittest.TestCase):
    def test_query_and_apps(self):
        self.assertEqual(gpu.parse_query("NVIDIA GeForce RTX 5090, 32607, 20656, 7\n"),
                         {"name": "NVIDIA GeForce RTX 5090", "total": 32607, "used": 20656, "free": 11951, "util": 7})
        self.assertEqual(gpu.parse_query("NVIDIA GeForce RTX 3090, 24576, 2048")["util"], None)   # three fields
        self.assertEqual(gpu.parse_query("NVIDIA X, 24576, 2048, [Not Supported]")["util"], None)
        self.assertIsNone(gpu.parse_query("No devices were found"))
        self.assertEqual(gpu.parse_apps("1088155, /usr/lib/ollama/llama-server, 19454\n399027, python3.13, 704\n"),
                         [(1088155, "/usr/lib/ollama/llama-server", 19454), (399027, "python3.13", 704)])
        self.assertEqual(gpu.parse_apps("1, game.exe, [N/A]\n"), [])   # (Windows-style: no per-process memory)
        self.assertEqual(gpu.parse_apps(""), [])

    def test_snapshot_from_the_tool(self):
        d = tempfile.mkdtemp()
        tool = os.path.join(d, "nvidia-smi")
        with open(tool, "w") as f:
            f.write('#!/bin/sh\ncase "$1" in\n  --query-compute-apps*) echo "4242, ComfyUI, 6144" ;;\n'
                    '  *) echo "NVIDIA GeForce RTX 5090, 32607, 26000, 93" ;;\nesac\n')
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IEXEC)
        old = os.environ["PATH"]
        os.environ["PATH"] = d
        try:
            s = gpu.snapshot()
        finally:
            os.environ["PATH"] = old
            shutil.rmtree(d)
        self.assertEqual((s["free"], s["util"], s["apps"]), (6607, 93, [(4242, "ComfyUI", 6144)]))
        os.environ["PATH"] = d   # (gone: no tool at all)
        try:
            self.assertIsNone(gpu.snapshot())
        finally:
            os.environ["PATH"] = old

    def test_oom_is_recognised(self):
        class OutOfMemoryError(RuntimeError):
            pass
        self.assertTrue(gpu.is_oom(OutOfMemoryError("CUDA out of memory. Tried to allocate 32.00 MiB")))
        self.assertTrue(gpu.is_oom(RuntimeError("cuDNN error: CUDNN_STATUS_ALLOC_FAILED")))
        self.assertFalse(gpu.is_oom(ValueError("bad lyrics")))


class StartCheck(unittest.TestCase):
    def test_room_and_busy(self):
        self.assertIsNone(GW.check_start(snap(10 * GiB), 22 * GiB, 9000, busy_pct=60))
        self.assertEqual(GW.check_start(snap(24 * GiB), 8 * GiB, 9000, busy_pct=60), "8 GB free, a song needs 8.8")
        self.assertEqual(GW.check_start(snap(10 * GiB, util=97), 22 * GiB, 9000, busy_pct=60), "it's 97% busy")
        self.assertIsNone(GW.check_start(snap(10 * GiB, util=97), 22 * GiB, 9000, busy_pct=0))          # not looked at
        self.assertIsNone(GW.check_start(snap(10 * GiB, util=97), 22 * GiB, 9000, busy_pct=60, own_busy=True))
        self.assertIsNone(GW.check_start(None, 22 * GiB, 9000, busy_pct=60))   # can't tell: never holds a song back

    def test_who_holds_it(self):
        s = snap(26 * GiB, apps=[(7, "/usr/lib/ollama/llama-server", 19454), (8, "ComfyUI", 5000), (9, "Xorg", 120)])
        self.assertEqual(GW.others(s), "Ollama 19 GB, ComfyUI 4.9 GB")         # tiny ones aren't named
        self.assertEqual(GW.others(s, own_pids={7}), "ComfyUI 4.9 GB")
        no_list = snap(19 * GiB + 400)   # (no process list: inside a container)
        self.assertEqual(GW.others(no_list, ollama=[{"name": "gemma4:26b", "size_vram": 19 << 30}]), "Ollama (gemma4:26b, 19 GB)")
        self.assertEqual(GW.others(snap(26 * GiB), ollama=[{"name": "qwen2.5:7b", "size_vram": 6 << 30}]),
                         "Ollama (qwen2.5:7b, 6 GB) and other programs (20 GB)")
        self.assertEqual(GW.others(snap(22 * GiB), own_mib=4 * GiB), "other programs (18 GB)")
        self.assertIsNone(GW.others(snap(500)))
        self.assertEqual(GW.using("Ollama (gemma4:26b, 19 GB)"), "Ollama (gemma4:26b, 19 GB) is using it")
        self.assertEqual(GW.using("Ollama 19 GB, ComfyUI 4.9 GB"), "Ollama 19 GB, ComfyUI 4.9 GB are using it")
        self.assertEqual(GW.using("other programs (18 GB)"), "other programs (18 GB) are using it")
        self.assertIsNone(GW.others(None))


class PressureWhileMaking(unittest.TestCase):
    def test_a_jump_that_stays_pauses(self):
        m = GW.Pressure(jump=3072, grace=20)
        self.assertIsNone(m.feed(0, 20 * GiB, 0))               # others hold 20 GB (Ollama idle): the baseline
        self.assertIsNone(m.feed(5, 27 * GiB, 7 * GiB))         # the studio's own 7 GB: not others
        self.assertIsNone(m.feed(10, 31 * GiB, 7 * GiB))        # others +4 GB: the grace starts
        self.assertIsNone(m.feed(15, 27 * GiB, 7 * GiB))        # gone again (a model loading for a moment)
        self.assertIsNone(m.feed(20, 31 * GiB, 7 * GiB))
        self.assertIsNone(m.feed(35, 31 * GiB, 7 * GiB))
        self.assertEqual(m.feed(41, 31 * GiB, 7 * GiB), "another program took 4 GB of the GPU")

    def test_baseline_is_the_least_and_off_means_never(self):
        m = GW.Pressure(jump=3072, grace=0)
        m.feed(0, 24 * GiB, 0)
        m.feed(1, 4 * GiB, 0)                                   # Ollama unloaded: the baseline drops...
        self.assertEqual(m.base, 24 * GiB)                      # ...once two looks agree
        m.feed(2, 4 * GiB, 0)
        self.assertEqual(m.base, 4 * GiB)
        self.assertIsNotNone(m.feed(3, 9 * GiB, 0))             # 5 GB more than the least: pressure
        self.assertIsNone(GW.Pressure(jump=0, grace=0).feed(0, 31 * GiB, 0))

    def test_the_studios_late_figure_after_freeing_a_model_isnt_pressure(self):
        m = GW.Pressure(jump=3072, grace=0)
        self.assertIsNone(m.feed(0, 15 * GiB, 8 * GiB))         # others 7 GB, Whisper 8 GB
        self.assertIsNone(m.feed(5, 8 * GiB, 8 * GiB))          # Whisper freed; the pipeline hasn't said so yet
        self.assertIsNone(m.feed(10, 8 * GiB, 1 * GiB))         # it has: others still 7 GB, nothing took the card
        self.assertIsNone(m.feed(15, 9 * GiB, 2 * GiB))

    def test_watch_pauses_only_during_a_gpu_step(self):
        class P:
            pid = os.getpid()
        state = {"step": "download", "used": 10 * GiB, "n": 0}
        got = []

        def sn():
            state["n"] += 1
            if state["n"] == 3:
                state["used"] = 20 * GiB           # another program takes 10 GB during the download
            if state["n"] == 6:
                state["step"] = "align"            # still there once the song needs the card
            return snap(state["used"])
        GW.watch(P(), reported=lambda: 0, step=lambda: state["step"], pause=got.append, snapshot=sn, every=0,
                 meter=GW.Pressure(jump=3072, grace=0), alive=lambda: state["n"] < 20)
        self.assertEqual((got, state["n"]), (["another program took 10 GB of the GPU"], 6))

    def test_own_memory_from_the_process_list_when_it_shows(self):
        mine = os.getpgid(os.getpid())
        s = snap(20 * GiB, apps=[(os.getpid(), "python3", 6000), (999999, "ollama", 14000)])
        self.assertEqual(GW.own_mib(s, mine, reported=1234), 6000)
        self.assertEqual(GW.own_mib(snap(20 * GiB), mine, reported=1234), 1234)   # (a container: no list)


class StepGate(unittest.TestCase):
    def test_room_no_room_and_unknown(self):
        said = []
        need = C.GPU_STEP_MIB["whisper"]
        self.assertTrue(GW.gate("listening", "whisper", snapshot=lambda: snap(32 * GiB - need - 10), out=lambda *a: said.append(a)))
        self.assertTrue(GW.gate("listening", "whisper", snapshot=lambda: None))
        self.assertTrue(GW.gate("x", "no-such-step", snapshot=lambda: snap(32 * GiB)))
        seen = iter([snap(31 * GiB), snap(31 * GiB), snap(20 * GiB)])   # room comes back while it waits
        self.assertTrue(GW.gate("listening", "whisper", wait_s=60, snapshot=lambda: next(seen), sleep=lambda s: None))
        with self.assertRaises(SystemExit) as e:
            GW.gate("listening to the vocals", "whisper", wait_s=0, snapshot=lambda: snap(30 * GiB), out=lambda *a: said.append(a))
        self.assertEqual(e.exception.code, GW.PAUSE_EXIT)
        self.assertEqual(said, [("pause", "the GPU has 2 GB free and listening to the vocals needs 7.3")])


class ResumeCache(unittest.TestCase):
    def test_written_always_read_only_when_resuming(self):
        d = tempfile.mkdtemp()
        calls = []
        f = lambda: calls.append(1) or {"heard": [1, 2]}
        GW.Memo(d, use=False, salt=["v1"]).get("align", ["text", "en"], f)
        m = GW.Memo(d, use=False, salt=["v1"])
        m.get("align", ["text", "en"], f)                       # a new make (not a resume) works it out again
        self.assertEqual(len(calls), 2)
        m = GW.Memo(d, use=True, salt=["v1"])
        self.assertEqual(m.get("align", ["text", "en"], f), {"heard": [1, 2]})
        self.assertEqual((len(calls), m.used), (2, ["align"]))  # resumed: taken from the cache
        GW.Memo(d, use=True, salt=["v1"]).get("align", ["other text", "en"], f)   # what it depends on changed
        GW.Memo(d, use=True, salt=["v2"]).get("align", ["other text", "en"], f)   # another pipeline version
        self.assertEqual(len(calls), 4)
        m.clear()
        self.assertFalse(os.path.exists(d))

    def test_old_ones_and_leftovers_go(self):
        work = tempfile.mkdtemp()
        old = GW.resume_dir(work, "20260101-000000-old")
        new = GW.resume_dir(work, "20260101-000000-new")
        os.makedirs(old); os.makedirs(new)
        os.utime(old, (time.time() - 8 * 86400,) * 2)
        for name, age in (("sep123-vocals.wav", 7200), ("bk9-lead.wav", 7200), ("sep5-inst.wav", 60), ("abcdefghijk-vocals.wav", 7200)):
            p = os.path.join(work, name)
            open(p, "w").close()
            os.utime(p, (time.time() - age,) * 2)
        GW.clean_resume(work)
        self.assertEqual((os.path.exists(old), os.path.exists(new)), (False, True))
        self.assertEqual(sorted(f for f in os.listdir(work) if f.endswith(".wav")), ["abcdefghijk-vocals.wav", "sep5-inst.wav"])
        shutil.rmtree(work)


class Priority(Base):
    def test_hand_before_imports_paused_first_in_its_class(self):
        imp1 = new_job(priority="import", **{"import": {"id": "imp-0000000001", "n": 1}})
        imp2 = new_job(priority="import")
        legacy = new_job(**{"import": {"id": "imp-0000000001", "n": 3}})   # queued before priorities: background
        hand = new_job(priority="hand")
        party = new_job(priority="party")
        imp_paused = new_job(priority="import", paused={"why": "x", "n": 1})
        order = [j["id"] for j in A.queued_jobs()]
        self.assertEqual(order, [hand, party, imp_paused, imp1, imp2, legacy])
        C.USE_LOCAL_GPU = False
        A.schedule_once()
        A._QV["v"] = None
        n, why = A.queue_place(A.load(imp2))
        self.assertEqual((n, why), (5, "queued behind 4 songs (imports wait behind songs made by hand)"))
        self.assertTrue(A.load(hand)["waiting"].startswith("waiting"))   # the one the scheduler looked at
        self.assertEqual(A.queue_view()["hand"], 2)

    def test_an_import_runs_when_nothing_else_waits(self):
        A.gpu_free_mib = lambda: 99999
        imp = new_job(priority="import", fake="ok")
        A.schedule_once()
        self.assertTrue(wait_for(lambda: st(imp) == "done", 15), A.load(imp))

    def test_new_party_request_eta_goes_ahead_of_imports(self):
        jobs = [{"id": f"i{k}", "status": "queued", "created": k, "priority": "import", "gpu_pref": "auto"} for k in range(5)]
        A._MAKE_S.update(at=time.time(), v={"local": 100.0, "remote": 100.0})
        now, secs, up, running, plan = A._queue_plan(jobs, extra=("new", "auto"))
        self.assertLess(plan["new"][0], plan["i0"][0])


class PauseAndResume(Base):
    def setUp(self):
        super().setUp()
        self.saved = (A.gpu_snapshot, A.WATCH_S, C.GPU_PREEMPT_MIB, C.GPU_PREEMPT_GRACE_S, C.GPU_RESUME_S, C.GPU_BUSY_PCT, A.ollama_models)
        self.card = {"used": 18 * GiB, "util": 0}
        A.gpu_snapshot = lambda: snap(self.card["used"], util=self.card["util"])
        A.WATCH_S, C.GPU_PREEMPT_MIB, C.GPU_PREEMPT_GRACE_S, C.GPU_RESUME_S, C.GPU_BUSY_PCT = 0.1, 3072, 0.3, 0, 60

    def tearDown(self):
        (A.gpu_snapshot, A.WATCH_S, C.GPU_PREEMPT_MIB, C.GPU_PREEMPT_GRACE_S, C.GPU_RESUME_S, C.GPU_BUSY_PCT, A.ollama_models) = self.saved

    def test_another_program_pauses_it_then_it_resumes_reusing_the_work(self):
        jid = new_job(priority="hand", fake="resumable")
        A.schedule_once()
        self.assertEqual(A.slots["local"], jid)
        older = new_job(priority="hand", fake="ok", created=A.load(jid)["created"] - 100)   # queued before it, even
        self.assertTrue(wait_for(lambda: len(pids(jid)) == 2 and A.runners.get(jid, {}).get("own") == 4000, 15))
        pp, cp = pids(jid)
        time.sleep(0.3)
        self.card["used"] += 8 * GiB                               # a game / an LLM takes 8 GB more
        self.assertTrue(wait_for(lambda: st(jid) == "queued", 20), A.load(jid))
        self.assertTrue(wait_for(lambda: A.slots["local"] is None, 10))
        self.assertFalse(alive(pp) or alive(cp), "the pipeline wasn't stopped")
        job = A.load(jid)
        self.assertEqual((job["resume"], job["paused"]["why"], job["paused"]["n"]), (True, "another program took 8 GB of the GPU", 1))
        self.assertIn("paused (another program took 8 GB", job["waiting"])
        self.assertNotEqual(job.get("status"), "failed")
        self.assertEqual([j["id"] for j in A.queued_jobs()][:2], [jid, older])   # back at the front
        self.assertTrue(os.path.exists(os.path.join(GW.resume_dir(C.WORK, jid), "whisper.json")))
        # the card is still full: it waits, saying who holds it
        A.ollama_models = lambda: [{"name": "gemma4:26b", "size_vram": 19 << 30}]
        A.gpu_free_mib = lambda: 32 * GiB - self.card["used"]
        A.NEED_GPU_MIB = 9000
        try:
            A.schedule_once()
            self.assertEqual(st(jid), "queued")
            self.assertIn("Ollama (gemma4:26b, 19 GB) and other programs (7 GB) are using it", A.load(jid)["waiting"])
            self.assertIn("a song needs 8.8", A.load(jid)["waiting"])
        finally:
            A.gpu_free_mib, A.NEED_GPU_MIB = (lambda: 99999), 0
        # free again: it carries on, taking Whisper's result from before the pause
        self.card["used"] = 18 * GiB
        A.schedule_once()
        self.assertTrue(wait_for(lambda: st(jid) == "done", 15), A.load(jid))
        job = A.load(jid)
        self.assertEqual(job["result"]["resumed"], ["whisper"])
        self.assertEqual(open(A.job_path(jid, "computed.txt")).read(), "whisper\n")   # worked out once, not twice
        self.assertFalse(job.get("resume") or job.get("paused"))
        self.assertFalse(os.path.exists(GW.resume_dir(C.WORK, jid)))                 # made: the cache is gone
        self.assertIn("carrying on after 1 pause for the GPU", job["log"][0])
        wait_for(lambda: A.slots["local"] is None, 5)

    def test_no_room_for_a_step_pauses_not_fails(self):
        jid = new_job(fake="pause")
        A.schedule_once()
        self.assertTrue(wait_for(lambda: st(jid) == "queued" and A.slots["local"] is None, 15), A.load(jid))
        job = A.load(jid)
        self.assertEqual(job["paused"]["why"], "the GPU has 2 GB free and removing the vocals needs 5")
        self.assertTrue(job["resume"])
        self.assertIn("run ended: paused", open(A.job_path(jid, "pipeline.log")).read())

    def test_out_of_memory_pauses_and_a_song_that_never_fits_fails_in_the_end(self):
        jid = new_job(fake="oom")
        for k in range(C.GPU_OOM_MAX - 1):
            A.schedule_once()
            self.assertTrue(wait_for(lambda: st(jid) == "queued" and A.slots["local"] is None and jid not in A.runners, 15), A.load(jid))
            self.assertEqual(A.load(jid)["oom_retries"], k + 1)
            self.assertIn("ran out of memory", A.load(jid)["paused"]["why"])
        A.schedule_once()
        self.assertTrue(wait_for(lambda: st(jid) == "failed", 15), A.load(jid))
        self.assertIn(f"ran out of memory {C.GPU_OOM_MAX} times in a row", A.load(jid)["error"])
        wait_for(lambda: A.slots["local"] is None, 5)

    def test_a_busy_card_holds_the_start_and_a_paused_song_waits_for_steady_room(self):
        jid = new_job(fake="ok")
        self.card["util"] = 95
        A.schedule_once()
        self.assertEqual(st(jid), "queued")
        self.assertIn("it's 95% busy", A.load(jid)["waiting"])
        self.card["util"] = 3
        A.set_job(jid, paused={"why": "x", "n": 1}, resume=True)
        C.GPU_RESUME_S = 60
        A._GPU_OK["since"] = None
        A.schedule_once()
        self.assertEqual(st(jid), "queued")
        self.assertIn("making sure it stays free", A.load(jid)["waiting"])
        A._GPU_OK["since"] = time.time() - 61
        A.schedule_once()
        self.assertTrue(wait_for(lambda: st(jid) == "done", 15), A.load(jid))
        wait_for(lambda: A.slots["local"] is None, 5)

    def test_cancel_a_paused_song_forgets_its_work(self):
        jid = new_job(fake="ok", paused={"why": "x", "n": 1}, resume=True)
        os.makedirs(GW.resume_dir(C.WORK, jid))
        self.assertEqual(run(A.cancel_job(jid, R()))["status"], "cancelled")
        self.assertFalse(os.path.exists(GW.resume_dir(C.WORK, jid)))
        self.assertFalse(A.load(jid).get("resume") or A.load(jid).get("paused"))

    def test_a_restart_resumes(self):
        jid = new_job(status="running")
        A.recover_jobs()
        self.assertEqual((st(jid), A.load(jid)["resume"]), ("queued", True))
        A.set_job(jid, status="failed")


class PausedOnTheHelper(Base):
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

    def tearDown(self):
        C.GPU_HELPER_URL = ""

    def test_no_room_on_the_helper_requeues_it_paused(self):
        jid = new_job(title="Song fake:pause")
        A.schedule_once()
        self.assertEqual(A.slots["remote"], jid)
        self.assertTrue(wait_for(lambda: st(jid) == "queued" and A.slots["remote"] is None, 30), A.load(jid))
        job = A.load(jid)
        self.assertEqual(job["paused"]["why"], "test helper: the GPU has 2 GB free and removing the vocals needs 5")
        self.assertTrue(job["resume"])
        self.assertIn("util", A.helper_health())
        A.set_job(jid, status="failed")


class BigImport(Base):
    def test_hundreds_of_rows_are_made_and_wait_behind_songs_by_hand(self):
        from tests._asgi import jcall
        n = 120
        iid = "imp-00000000b1"
        rows = [{"n": i + 1, "input": f"Artist {i} - Song {i}", "kind": "text", "q": f"Artist {i} - Song {i}", "status": "ready",
                 "tick": True, "artist": f"Artist {i}", "title": f"Song {i}",
                 "video": {"id": f"vid{i:08d}", "title": f"Artist {i} - Song {i}", "channel": "x", "duration": 200, "flags": []},
                 "lyrics": {"source": "lrclib", "id": 1000 + i, "lines": 30, "timed": True, "lang": "English"}} for i in range(n)]
        os.makedirs(A.IMPORTS, exist_ok=True)
        with open(A.imp_path(iid), "w") as f:
            json.dump({"id": iid, "created": time.time(), "by": {"by": "Mia", "kind": "person"}, "kind": "list", "left_out": 0,
                       "state": "ready", "rows": rows, "error": None}, f)
        C.USE_LOCAL_GPU = False
        hand = new_job(priority="hand")
        st_, _, r = jcall(A.app, "POST", f"/api/imports/{iid}/make", {"rows": [{"n": x["n"]} for x in rows]},
                          {"X-authentik-username": "Mia"})
        self.assertEqual(st_, 200, r)
        self.assertEqual(len(r["made"]), n)
        q = A.queued_jobs()
        self.assertEqual(q[0]["id"], hand)                        # made earlier by hand: first anyway
        later = new_job(priority="hand")                          # made by hand after the import: still before it
        q = [j["id"] for j in A.queued_jobs()]
        self.assertEqual(q[:2], [hand, later])
        self.assertEqual(q[2:], [m["id"] for m in r["made"]])     # the import's songs in list order
        self.assertEqual({A.load(m["id"])["priority"] for m in r["made"]}, {"import"})
        for j in q:
            A.set_job(j, status="failed")


if __name__ == "__main__":
    unittest.main()
