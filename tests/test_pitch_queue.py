"""Pitch files written after a publish (app.py pitch_queue): the queue survives a restart (a marker per song), a sweep
finds published songs without an up-to-date pitch file, failures aren't retried on the same vocals, and nothing is
tracked while a song is being made. No GPU: pitch.write is replaced."""
import os, threading, time, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import app as A  # noqa: E402

n = [0]


def song(published=True, pitch=False, status="done"):
    n[0] += 1
    jid = time.strftime("%Y%m%d-%H%M%S") + f"-pitch-queue-{n[0]}"
    os.makedirs(f"{A.JOBS}/{jid}")
    base = f"Pitch Tester - Song {n[0]} (Music Video)"
    A.save(jid, {"id": jid, "artist": "Pitch Tester", "title": f"Song {n[0]}", "status": status, "created": time.time(),
                 "offset": 0.0, "log": [], **({"published": time.time(), "published_as": base} if published else {})})
    with open(A.job_path(jid, "vocals.m4a"), "wb") as f:
        f.write(b"v" * 10)
    os.utime(A.job_path(jid, "vocals.m4a"), (time.time() - 100, time.time() - 100))
    if published:
        open(f"{A.MV_DIR}/{base}.mp4", "wb").close()
    if pitch:
        open(f"{A.MV_DIR}/{base}{A.pitchmod.EXT}", "w").write("{}")
    return jid, base


class PitchQueue(unittest.TestCase):
    def setUp(self):
        self.saved = (A.pitchmod.write, A.update_catalog, A._pitch_start, A.job_index, dict(A.slots), A.pitchmod.lead_settled)
        self.written, self.splits, self.failing = [], [], False

        def write(job_dir, job, mv_dir, bases, log=None, device=None, split=True):
            if self.failing:
                raise RuntimeError("pitch tracking failed")
            for b in bases:
                open(os.path.join(mv_dir, b + A.pitchmod.EXT), "w").write("{}")
            self.written.append(os.path.basename(job_dir))
            self.splits.append(split)
            return [b + A.pitchmod.EXT for b in bases]
        A.pitchmod.write = write
        A.pitchmod.lead_settled = lambda job_dir, job: False   # (no lead tracked yet: one beside a make gets redone)
        A.update_catalog = lambda: None
        A._pitch_start = lambda: None          # no worker thread unless a test starts one
        A.job_index = lambda: []               # (other tests' leftover jobs don't make the studio look busy)
        A.slots.update(local=None, remote=None)
        # earlier tests' songs: done with (their markers and missing pitch files aren't this test's)
        for j in os.listdir(A.JOBS):
            for f in (A.PITCH_TODO, A.PITCH_FAILED):
                try:
                    os.remove(os.path.join(A.JOBS, j, f))
                except OSError:
                    pass
        self._hide = []
        for j in os.listdir(A.JOBS):
            if A.pitch_missing(j):
                os.rename(os.path.join(A.JOBS, j), os.path.join(A.JOBS, "_" + j))   # (not a job id: skipped)
                self._hide.append(j)
        with A._PITCH["cv"]:
            A._PITCH["order"].clear()
            A._PITCH["now"].clear()

    def tearDown(self):
        A.pitchmod.write, A.update_catalog, A._pitch_start, A.job_index, slots, A.pitchmod.lead_settled = self.saved
        A.slots.update(slots)
        for j in self._hide:
            os.rename(os.path.join(A.JOBS, "_" + j), os.path.join(A.JOBS, j))
        with A._PITCH["cv"]:
            A._PITCH["order"].clear()
            A._PITCH["now"].clear()

    def restart(self):
        """What a restart leaves: the job folders, not the queue in memory."""
        with A._PITCH["cv"]:
            A._PITCH["order"].clear()
            A._PITCH["now"].clear()

    def test_publish_leaves_a_marker_and_queues_once(self):
        jid, _ = song()
        A.pitch_queue(jid)
        A.pitch_queue(jid)
        self.assertTrue(os.path.exists(A.job_path(jid, A.PITCH_TODO)))
        self.assertEqual(A._PITCH["order"], [jid])
        self.assertEqual(A.pitch_pending(), 1)

    def test_restart_resumes_marked_songs_first_then_the_sweep(self):
        a, _ = song()
        b, _ = song()
        A.pitch_queue(b)                       # queued (marked) before the restart, a wasn't: the sweep finds it
        fresh, _ = song(pitch=True)            # has an up-to-date pitch file: left alone
        unpub, _ = song(published=False)       # not published: left alone
        making, _ = song(status="running")
        self.restart()
        self.assertEqual(A.pitch_resume(), 2)
        self.assertEqual(A._PITCH["order"], [b, a])
        for j in (fresh, unpub, making):
            self.assertNotIn(j, A._PITCH["order"])
        self.assertTrue(os.path.exists(A.job_path(a, A.PITCH_TODO)))   # swept songs are marked too
        # the work is done and settled: nothing left after another restart
        for j in list(A._PITCH["order"]):
            A.pitch_one(j)
        self.assertEqual(sorted(self.written), sorted([a, b]))
        self.assertFalse(os.path.exists(A.job_path(a, A.PITCH_TODO)))
        self.restart()
        self.assertEqual(A.pitch_resume(), 0)

    def test_sweep_finds_a_pitch_file_older_than_the_vocals(self):
        jid, base = song(pitch=True)
        self.assertFalse(A.pitch_missing(jid))
        p = f"{A.MV_DIR}/{base}{A.pitchmod.EXT}"
        os.utime(p, (time.time() - 1000, time.time() - 1000))   # the song was made again after its pitch file
        self.assertTrue(A.pitch_missing(jid))
        os.remove(p)
        self.assertTrue(A.pitch_missing(jid))

    def test_a_failure_isnt_swept_again_until_the_vocals_change(self):
        jid, _ = song()
        A.pitch_queue(jid)
        self.failing = True
        A.pitch_one(jid)
        self.assertFalse(os.path.exists(A.job_path(jid, A.PITCH_TODO)))
        self.assertTrue(os.path.exists(A.job_path(jid, A.PITCH_FAILED)))
        self.assertFalse(A.pitch_missing(jid))
        self.restart()
        self.assertEqual(A.pitch_resume(), 0)
        with open(A.job_path(jid, "vocals.m4a"), "wb") as f:   # made again: new vocals
            f.write(b"w" * 20)
        self.assertTrue(A.pitch_missing(jid))
        self.failing = False
        A.pitch_queue(jid)
        A.pitch_one(jid)
        self.assertFalse(os.path.exists(A.job_path(jid, A.PITCH_FAILED)))
        self.assertFalse(A.pitch_missing(jid))

    def test_queued_again_while_writing_keeps_the_marker(self):
        jid, _ = song()
        A.pitch_queue(jid)
        real = A.pitchmod.write

        def write(*a, **k):
            time.sleep(0.002)
            A.pitch_queue(jid)                 # republished while its pitch file was being written
            return real(*a, **k)
        A.pitchmod.write = write
        A.pitch_one(jid)
        self.assertTrue(os.path.exists(A.job_path(jid, A.PITCH_TODO)))

    def test_a_publish_beside_a_make_is_written_now_and_again_with_the_lead_later(self):
        jid, base = song()
        A.pitch_queue(jid, now=True)   # (what pitch_later does; other tests replace it)
        self.assertEqual(A._PITCH["now"], [jid])
        with A._PITCH["cv"]:
            A._PITCH["now"].remove(jid)        # (the worker takes it: a song is being made here)
        A.pitch_one(jid, split=False)
        self.assertEqual(self.splits, [False])
        self.assertTrue(os.path.exists(f"{A.MV_DIR}/{base}{A.pitchmod.EXT}"))   # written at once, the plain melody
        self.assertTrue(os.path.exists(A.job_path(jid, A.PITCH_TODO)))         # and not done with
        self.assertEqual(A._PITCH["order"], [jid])                              # waits for an idle studio
        with A._PITCH["cv"]:
            A._PITCH["order"].clear()
        A.pitch_one(jid)
        self.assertEqual(self.splits, [False, True])
        self.assertFalse(os.path.exists(A.job_path(jid, A.PITCH_TODO)))
        # a song whose melody can't get better from the lead (keep backing vocals, or tracked with it) isn't redone
        jid2, _ = song()
        A.pitchmod.lead_settled = lambda job_dir, job: True
        A.pitch_queue(jid2, now=True)   # (what pitch_later does; other tests replace it)
        with A._PITCH["cv"]:
            A._PITCH["now"].remove(jid2)
        A.pitch_one(jid2, split=False)
        self.assertFalse(os.path.exists(A.job_path(jid2, A.PITCH_TODO)))
        self.assertNotIn(jid2, A._PITCH["order"])

    def test_one_odd_job_folder_doesnt_stop_the_sweep(self):
        bad, _ = song()
        good, _ = song()
        real = A.published_bases

        def pb(jid, job):
            if jid == bad:
                raise RuntimeError("an odd versions folder")
            return real(jid, job)
        A.published_bases = pb
        A._PITCH["sweep_at"] = 0.0
        try:
            self.assertEqual(A.pitch_resume(), 1)
        finally:
            A.published_bases = real
        self.assertEqual(A._PITCH["order"], [good])
        self.assertGreater(A._PITCH["sweep_at"], 0)

    def test_deleted_song_is_dropped(self):
        jid, _ = song()
        A.pitch_queue(jid)
        os.remove(A.job_path(jid, A.PITCH_TODO))
        os.remove(A.job_path(jid, "vocals.m4a"))
        os.remove(A.job_path(jid, "job.json"))
        os.rmdir(os.path.join(A.JOBS, jid))
        A.pitch_one(jid)                       # no error
        self.assertEqual(self.written, [])

    def test_busy_studio_waits(self):
        saved = A.gpu_free_mib, A.NEED_GPU_MIB, A.C.USE_LOCAL_GPU
        A.NEED_GPU_MIB, A.C.USE_LOCAL_GPU = 8000, True
        try:
            A.gpu_free_mib = lambda: 99999
            A.job_index = lambda: [{"id": "x", "status": "queued", "gpu_pref": "auto"}]
            self.assertFalse(A.pitch_idle())   # about to be made here
            A.job_index = lambda: [{"id": "x", "status": "queued", "gpu_pref": "remote"}]
            self.assertTrue(A.pitch_idle())    # waits for the helper PC only: may wait for hours
            A.job_index = lambda: [{"id": "x", "status": "queued", "gpu_pref": "local"}]
            A.gpu_free_mib = lambda: 0
            self.assertTrue(A.pitch_idle())    # the card has no room for it anyway
        finally:
            A.gpu_free_mib, A.NEED_GPU_MIB, A.C.USE_LOCAL_GPU = saved
        A.job_index = lambda: []
        A.slots["local"] = "some-song"
        self.assertFalse(A.pitch_idle())
        A.slots["local"] = None
        self.assertTrue(A.pitch_idle())
        A.slots["remote"] = "a-song-on-the-helper"   # being made on the helper PC: the backlog waits, a publish doesn't
        self.assertFalse(A.pitch_idle())
        self.assertFalse(A.pitch_busy_here())

    def test_worker_writes_a_publish_at_once_and_the_rest_when_idle(self):
        old, _ = song()                        # left from before a restart: waits for an idle studio
        new, _ = song()                        # published now
        A.slots["remote"] = "a-song-on-the-helper"
        A.slots["local"] = "some-song"
        saved = (A.PITCH_IDLE_POLL_S, A._PITCH["sweep_at"])
        A.PITCH_IDLE_POLL_S = 0.05
        A._PITCH["sweep_at"] = time.time()
        A.pitch_queue(old)
        A.pitch_queue(new, now=True)   # (what pitch_later does; other tests replace it)
        t = threading.Thread(target=A._pitch_worker, daemon=True)
        t.start()
        try:
            end = time.time() + 5
            while not self.written and time.time() < end:
                time.sleep(0.02)
            time.sleep(0.3)
            self.assertEqual(self.written, [new])          # at once, beside the make (without the lead)
            self.assertEqual(self.splits, [False])
            A.slots["local"] = None                        # done here; the helper PC still makes one: still waits
            time.sleep(0.3)
            self.assertEqual(self.written, [new])
            A.slots["remote"] = None
            end = time.time() + 5
            while len(self.written) < 3 and time.time() < end:
                time.sleep(0.02)
            self.assertEqual(sorted(self.written[1:]), sorted([old, new]))   # the rest, and the publish with its lead
            self.assertEqual(self.splits[1:], [True, True])
        finally:
            A._PITCH["stop"] = True
            with A._PITCH["cv"]:
                A._PITCH["cv"].notify_all()
            t.join(5)
            A._PITCH.pop("stop", None)
            A.PITCH_IDLE_POLL_S, A._PITCH["sweep_at"] = saved
        self.assertFalse(t.is_alive())

    def test_worker_tracks_only_when_nothing_is_being_made(self):
        jid, _ = song()
        A.slots["local"] = "some-song"         # a song is being made here
        saved = (A.PITCH_IDLE_POLL_S, A._PITCH["sweep_at"])
        A.PITCH_IDLE_POLL_S = 0.05
        A._PITCH["sweep_at"] = time.time()     # (the startup sweep: done)
        A.pitch_queue(jid)
        t = threading.Thread(target=A._pitch_worker, daemon=True)
        t.start()
        try:
            time.sleep(0.4)
            self.assertEqual(self.written, [])
            self.assertEqual(A.pitch_pending(), 1)
            A.slots["local"] = None            # done making it
            end = time.time() + 5
            while not self.written and time.time() < end:
                time.sleep(0.02)
            self.assertEqual(self.written, [jid])
            end = time.time() + 5
            while os.path.exists(A.job_path(jid, A.PITCH_TODO)) and time.time() < end:
                time.sleep(0.02)
            self.assertFalse(os.path.exists(A.job_path(jid, A.PITCH_TODO)))
        finally:
            A._PITCH["stop"] = True
            with A._PITCH["cv"]:
                A._PITCH["cv"].notify_all()
            t.join(5)
            A._PITCH.pop("stop", None)
            A.PITCH_IDLE_POLL_S, A._PITCH["sweep_at"] = saved
        self.assertFalse(t.is_alive())


if __name__ == "__main__":
    unittest.main()
