"""Cancel a make: the state machine, killing the whole process group, the helper PC, and races. No GPU needed:
pipeline.py is replaced by tests/fake_pipeline.py. From the repo folder (the studio's Python, e.g. in its container):
    python -I -W ignore::ResourceWarning -m unittest discover -s tests -t . -v"""
import asyncio, json, os, threading, time, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import app as A  # noqa: E402
import config as C  # noqa: E402
import helper as H  # noqa: E402
from fastapi import HTTPException  # noqa: E402

A.PIPELINE = H.PIPELINE = _env.FAKE
A.gpu_free_mib = lambda: 99999
H.gpu = lambda: ("Fake GPU", 99999)
run = asyncio.run
R = _env.Req


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"  # a zombie is dead, just not reaped yet
    except FileNotFoundError:
        return False


def wait_for(cond, timeout=20, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


n = [0]


def new_job(**kw):
    n[0] += 1
    jid = time.strftime("%Y%m%d-%H%M%S") + f"-test-song-{n[0]}"
    os.makedirs(f"{A.JOBS}/{jid}")
    A.save(jid, {"id": jid, "yt": "abcdefghijk", "lrc_id": 1, "lyrics_source": "lrclib", "artist": "Tester", "title": f"Song {n[0]}",
                 "status": "queued", "step": "queued", "gpu_pref": "auto", "created": time.time() + n[0], "offset": 0.0, "log": [], **kw})
    return jid


def st(jid):
    return A.load(jid)["status"]


def pids(jid):
    p = A.job_path(jid, "pids.txt")
    return [int(x) for x in open(p).read().split()] if os.path.exists(p) and open(p).read().strip() else []


class Base(unittest.TestCase):
    def setUp(self):
        # only this test's jobs in the queue: mark leftovers done-with
        for j in os.listdir(A.JOBS):
            if A.ID_RE.match(j):
                job = A.load(j)
                if job["status"] in ("queued", "running"):
                    job["status"] = "failed"; A.save(j, job)
        wait_for(lambda: A.slots["local"] is None and A.slots["remote"] is None and not A.runners, 30)
        C.USE_LOCAL_GPU, C.GPU_HELPER_URL = True, ""

    def start_local(self, **kw):
        jid = new_job(**kw)
        A.schedule_once()
        self.assertEqual(A.slots["local"], jid)
        self.assertTrue(wait_for(lambda: len(pids(jid)) == 2), "fake pipeline didn't start")
        return jid


class CancelLocal(Base):
    def test_cancel_queued(self):
        C.USE_LOCAL_GPU = False  # nothing can take it
        jid = new_job()
        r = run(A.cancel_job(jid, R()))
        self.assertEqual(r["status"], "cancelled")
        job = A.load(jid)
        self.assertEqual(job["status"], "cancelled")
        self.assertIn("cancelled by you", job["log"])
        C.USE_LOCAL_GPU = True
        A.schedule_once()  # the scheduler leaves it alone
        self.assertIsNone(A.slots["local"])
        self.assertEqual(st(jid), "cancelled")

    def test_cancel_running_kills_group_and_frees_slot(self):
        jid = self.start_local()
        pp, cp = pids(jid)
        self.assertTrue(alive(pp) and alive(cp))
        r = run(A.cancel_job(jid, R()))
        self.assertEqual(r["status"], "stopping")
        self.assertEqual(A.get_job(jid)["cancel_requested"] is not None, True)
        # double cancel is harmless
        self.assertEqual(run(A.cancel_job(jid, R()))["status"], "stopping")
        # delete while stopping is refused
        with self.assertRaises(HTTPException):
            A.delete_job(jid, R())
        self.assertTrue(wait_for(lambda: st(jid) == "cancelled", 15))
        self.assertFalse(alive(pp), "pipeline still running")
        self.assertFalse(alive(cp), "pipeline's child still running")
        self.assertTrue(wait_for(lambda: A.slots["local"] is None and jid not in A.runners, 5))
        job = A.load(jid)
        self.assertNotIn("cancel_requested", job)
        self.assertEqual(job["log"][-1], "cancelled by you")
        A.schedule_once()  # not retried
        self.assertEqual(st(jid), "cancelled")
        self.assertEqual(run(A.cancel_job(jid, R()))["status"], "cancelled")  # cancel again: no-op

    def test_stubborn_child_gets_sigkill(self):
        jid = self.start_local(fake="stubborn")
        pp, cp = pids(jid)
        t0 = time.time()
        run(A.cancel_job(jid, R()))
        self.assertTrue(wait_for(lambda: st(jid) == "cancelled", 25))
        self.assertGreater(time.time() - t0, 8)  # it waited for SIGTERM first
        self.assertFalse(alive(pp)); self.assertFalse(alive(cp))

    def test_retry_then_delete_after_cancel(self):
        jid = self.start_local()
        run(A.cancel_job(jid, R()))
        self.assertTrue(wait_for(lambda: st(jid) == "cancelled", 15))
        wait_for(lambda: A.slots["local"] is None, 5)
        # Retry: made again from the start, and this time it works
        job = A.load(jid); job["fake"] = "ok"; A.save(jid, job)
        run(A.retry(jid, R()))
        self.assertEqual(st(jid), "queued")
        A.schedule_once()
        self.assertTrue(wait_for(lambda: st(jid) == "done", 15), A.load(jid))
        wait_for(lambda: A.slots["local"] is None, 5)
        with self.assertRaises(HTTPException):
            run(A.cancel_job(jid, R()))  # finished: nothing to cancel
        # Delete works for a cancelled song
        jid2 = new_job(); run(A.cancel_job(jid2, R()))
        A.delete_job(jid2, R())
        self.assertFalse(os.path.exists(f"{A.JOBS}/{jid2}"))

    def test_cancel_between_claim_and_start(self):
        jid = new_job()
        self.assertTrue(A.claim(jid, "local", "Fake GPU"))  # claimed, runner thread not started yet
        r = run(A.cancel_job(jid, R()))
        self.assertEqual(r["status"], "stopping")
        A.run_local(jid)  # sees the cancel and never starts the pipeline
        self.assertEqual(st(jid), "cancelled")
        self.assertFalse(os.path.exists(A.job_path(jid, "pids.txt")))
        self.assertIsNone(A.slots["local"])

    def test_claim_after_cancel_refused(self):
        jid = new_job()
        run(A.cancel_job(jid, R()))
        self.assertFalse(A.claim(jid, "local", "Fake GPU"))

    def test_running_without_runner(self):
        jid = new_job(status="running", step="align")  # e.g. the studio restarted / runner thread died
        self.assertEqual(run(A.cancel_job(jid, R()))["status"], "cancelled")

    def test_oom_with_cancel_is_cancelled_not_requeued(self):
        jid = new_job(status="running", cancel_requested=time.time())
        A.finish(jid, 1, None, True, "local")
        self.assertEqual(st(jid), "cancelled")

    def test_restart_recovery(self):
        a = new_job(status="running", cancel_requested=time.time())
        b = new_job(status="running")
        A.recover_jobs()
        self.assertEqual(st(a), "cancelled")
        self.assertEqual(st(b), "queued")
        for j in (a, b):
            job = A.load(j); job["status"] = "failed"; A.save(j, job)

    def test_video_task_refused(self):
        jid = new_job(status="done", video_task={"running": True, "what": "x"})
        with self.assertRaises(HTTPException) as e:
            run(A.cancel_job(jid, R()))
        self.assertEqual(e.exception.status_code, 409)

    def test_queued_remake_goes_back(self):
        jid = new_job(status="done", fake="ok", result={"lines": 3}, reviewed=123.0, published=1.0)
        for f in ("out.mp4", "result.json", "words.json", "words.orig.json"):
            open(A.job_path(jid, f), "w").write("{}")
        C.USE_LOCAL_GPU = False
        run(A.remake(jid, R({})))
        self.assertEqual(st(jid), "queued")
        self.assertFalse(os.path.exists(A.job_path(jid, "words.orig.json")))
        r = run(A.cancel_job(jid, R()))
        self.assertEqual(r["status"], "done")
        job = A.load(jid)
        self.assertEqual(job["result"], {"lines": 3})
        self.assertEqual(job["reviewed"], 123.0)
        self.assertTrue(os.path.exists(A.job_path(jid, "words.orig.json")))


    def test_started_remake_requeued_is_cancelled_not_restored(self):
        # a remake from before this change (no "unstarted" marker), or one that already ran and was requeued
        jid = new_job(status="queued", remade=time.time(), pre_remake={"edited": None, "result": {"lines": 1}, "offset": 0})
        open(A.job_path(jid, "out.mp4"), "w").write("x"); open(A.job_path(jid, "words.json"), "w").write("{}")
        self.assertEqual(run(A.cancel_job(jid, R()))["status"], "cancelled")
        # a remake that was claimed (it started) loses the marker
        jid = new_job(status="done", fake="ok", result={"lines": 3})
        for f in ("out.mp4", "result.json", "words.json"):
            open(A.job_path(jid, f), "w").write("{}")
        C.USE_LOCAL_GPU = False
        run(A.remake(jid, R({})))
        self.assertTrue(A.load(jid)["pre_remake"].get("unstarted"))
        self.assertTrue(A.claim(jid, "local", "Fake GPU"))
        self.assertNotIn("unstarted", A.load(jid)["pre_remake"])
        with A.lock:
            A.runners.pop(jid, None); A.slots["local"] = None
        job = A.load(jid); job["status"] = "queued"; A.save(jid, job)  # e.g. requeued after running out of GPU memory
        self.assertEqual(run(A.cancel_job(jid, R()))["status"], "cancelled")


class CancelRemote(Base):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        cls.server = uvicorn.Server(uvicorn.Config(H.app, host="127.0.0.1", port=_env.HELPER_PORT, log_level="warning"))
        threading.Thread(target=cls.server.run, daemon=True).start()
        wait_for(lambda: cls.server.started, 10)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True

    def setUp(self):
        super().setUp()
        C.USE_LOCAL_GPU, C.GPU_HELPER_URL, C.HELPER_TOKEN = False, f"http://127.0.0.1:{_env.HELPER_PORT}", os.environ["HELPER_TOKEN"]

    def hpids(self, jid):
        p = os.path.join(H.JOBS, jid, "pids.txt")
        return [int(x) for x in open(p).read().split()] if os.path.exists(p) and open(p).read().strip() else []

    def test_cancel_on_helper(self):
        jid = new_job()
        A.schedule_once()
        self.assertEqual(A.slots["remote"], jid)
        self.assertTrue(wait_for(lambda: len(self.hpids(jid)) == 2, 20), "helper didn't start it")
        pp, cp = self.hpids(jid)
        self.assertEqual(run(A.cancel_job(jid, R()))["status"], "stopping")
        self.assertTrue(wait_for(lambda: st(jid) == "cancelled", 30), A.load(jid))
        self.assertFalse(alive(pp)); self.assertFalse(alive(cp))
        self.assertTrue(wait_for(lambda: A.slots["remote"] is None, 5))
        self.assertTrue(wait_for(lambda: not os.path.exists(os.path.join(H.JOBS, jid)), 5), "helper copy not deleted")
        self.assertIsNone(H.state["busy"])
        A.schedule_once()
        self.assertEqual(st(jid), "cancelled")

    def test_helper_cancel_endpoint(self):
        self.assertEqual(A.helper_health()["busy"], False)
        with self.assertRaises(Exception):
            A.helper_call("/jobs/nope-nope-nope/cancel", "POST", {})  # 404
        # cancelled before its thread started the pipeline
        with H.lock:
            H.jobs["early-cancel-1"] = {"status": "running", "step": "lyrics", "log": [], "error": None, "cancel": True}
            H.state["busy"] = "early-cancel-1"
        os.makedirs(os.path.join(H.JOBS, "early-cancel-1"), exist_ok=True)
        H.run("early-cancel-1")
        self.assertEqual(H.jobs["early-cancel-1"]["status"], "cancelled")
        self.assertIsNone(H.state["busy"])

    def test_remote_finish_works(self):
        jid = new_job(title="Remote fake:ok")
        A.schedule_once()
        self.assertTrue(wait_for(lambda: st(jid) == "done", 30), A.load(jid))


if __name__ == "__main__":
    unittest.main()
