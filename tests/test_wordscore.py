"""Singing scores on the Studio's side (contract §1.1 music, §2.4/§2.6 score, §2.9 words scoring): the instrumental's
loudness in pitch files and its backfill, scores on plays and history events, and the words-scoring endpoints: the
body limit (only that route above 64 KB), header checks before the body is read, jobs per player, busy, failures,
results forgotten after 10 minutes, the aligner process (tests/fake_wordscore.py: same pipes, no model) and its
keep-alive, the GPU rules, references from the song's own vocals, and the recording never touching the disk or the log.
No GPU or network; ffmpeg makes small real Opus/AAC/MP4 files."""
import json, os, re, shutil, subprocess, sys, tempfile, time, unittest, uuid

from tests import _env  # noqa: F401  (must come first: throwaway folders)
from tests.test_encore import A, Base, api, jcall, make_song, pair, call  # noqa: E402  (its stand-ins for pitch, catalog)
import encore as E  # noqa: E402
import pitch as P  # noqa: E402
import schemalite as S  # noqa: E402
import wordscore as W  # noqa: E402
import numpy as np  # noqa: E402

FAKE_WORDS = os.path.join(_env.HERE, "fake_wordscore.py")
HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def ffmpeg(*args, data=None):
    return subprocess.run(["ffmpeg", "-v", "error", "-nostdin", *args], input=data, capture_output=True, check=True).stdout


def tone_expr(spans, amp=0.3):
    on = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in spans) or "0"
    return f"{amp}*sin(2*PI*330*t)*({on})"


def recording(spans, seconds, fmt="ogg"):
    """Opus audio (bytes) with a tone on these spans (recording time), silence elsewhere."""
    return ffmpeg("-f", "lavfi", "-i", f"aevalsrc='{tone_expr(spans)}':s=16000:d={seconds}", "-ac", "1", "-c:a", "libopus",
                  "-b:a", "24k", "-f", fmt, "pipe:1")


def word_times(base):
    d = json.load(open(os.path.join(A.MV_DIR, base + ".words.json"), encoding="utf-8"))
    return [(l["i"], k, w[0], w[1]) for l in d["lines"] for k, w in enumerate(l["words"])]


def headers(tok, song, base, start=0.0, rate=1.0, lines=None, ctype="audio/ogg"):
    h = {"Authorization": "Bearer " + tok, "Content-Type": ctype, "X-Encore-Song": song, "X-Encore-Base": base,
         "X-Encore-Start": str(start), "X-Encore-Rate": str(rate)}
    if lines is not None:
        h["X-Encore-Lines"] = lines
    return h


class Scorer(Base):
    """The scorer's pure parts: which words, their windows, what's judged."""

    def test_expected_words_lines_backing_and_letters(self):
        doc = {"lines": [{"i": 1, "words": [[1.0, 1.4, "Hel-lo"], [1.5, 1.9, "&"], [2.0, 2.4, "there"]]},
                         {"i": 2, "backing": True, "words": [[3.0, 3.4, "ooh"]]},
                         {"i": 3, "words": [[5.0, 5.4, "Café"]]}]}
        self.assertEqual(W.expected(doc), [(1, 0, "hello", 1.0, 1.4), (1, 2, "there", 2.0, 2.4), (3, 0, "cafe", 5.0, 5.4)])
        self.assertEqual([w[:2] for w in W.expected(doc, [2, 3])], [(2, 0), (3, 0)])  # a backing line asked for is scored

    def test_plan_maps_video_time_with_start_and_rate(self):
        words = [(1, 0, "a", 10.0, 10.5), (1, 1, "b", 12.0, 12.5), (2, 0, "c", 30.0, 31.0)]
        p = W.plan(words, start=9.0, rate=1.0, seconds=10.0)  # recording = video 9..19
        self.assertEqual([w[:2] for w in p], [(1, 0), (1, 1)])
        self.assertAlmostEqual(p[0][3], 0.2)          # (10 - 0.8 - 9)
        self.assertAlmostEqual(p[0][4], 2.3)
        p = W.plan(words, start=8.0, rate=2.0, seconds=2.0)  # sped up: recording second t = video 8 + 2t
        self.assertEqual([w[:2] for w in p], [(1, 0)])
        self.assertAlmostEqual(p[0][3], (10.0 - 0.8 - 8.0) / 2)
        self.assertEqual([w[:2] for w in W.plan(words, 10.2, 1.0, 60)], [(1, 1), (2, 0)])  # (1, 0) began before the recording

    def test_summarize_judges_only_what_the_references_allow(self):
        planned = [(1, k, "x", 0, 1) for k in range(5)]
        conf = [0.9, 0.1, 0.9, 0.9, 0.9]
        ref = {(1, 0): (0.8, 0.0), (1, 1): (0.8, None), (1, 2): (W.REF_MIN - 0.01, 0.0),  # the original is unsure: out
               (1, 3): (0.9, W.THRESHOLD)}                                                # the music alone passes: out
        r = W.summarize(planned, conf, ref)                                               # (1, 4): no reference: out
        self.assertEqual([(w["w"], w["ok"]) for w in r["words"]], [(0, True), (1, False)])
        self.assertEqual((r["heard"], r["total"], r["score"]), (1, 2, 50))
        r = W.summarize(planned, conf, None)
        self.assertEqual((r["heard"], r["total"], r["score"]), (4, 5, 80))
        self.assertEqual(W.summarize([], [], None)["total"], 0)

    def test_headers_parsing(self):
        self.assertEqual(W.parse_lines("3, 4,7"), [3, 4, 7])
        self.assertIsNone(W.parse_lines(None))
        for bad in ("3;4", "a", "-1", "1,,2"):
            with self.assertRaises(ValueError):
                W.parse_lines(bad)
        self.assertEqual(W.media_format("audio/webm;codecs=opus"), "webm")
        self.assertEqual(W.media_format("Audio/Ogg"), "ogg")
        self.assertIsNone(W.media_format("audio/mpeg"))

    def test_line_groups(self):
        words = [("a", 0.0, 1.0, 1), ("b", 0.5, 1.5, 1), ("c", 5.0, 6.0, 2), ("d", 30.0, 31.0, 2)]
        g = W.groups(words)
        self.assertEqual(sorted(x[2] for x in g), [[0, 1], [2], [3]])  # one line; a line longer than CHUNK is cut


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
class MusicTrack(Base):
    """§1.1: the instrumental's loudness in pitch.json, and deploy/write-music.py for the files already published."""

    def test_music_db_levels_floor_and_rounding(self):
        a = np.concatenate([np.full(1600, 0.5) * np.where(np.arange(1600) % 2, -1, 1), np.zeros(1600), np.full(800, 0.1)])
        self.assertEqual(P.music_db(a), [-6.0, -90.0, -20.0])  # (the last tenth of a second is only half there)
        self.assertEqual(P.music_db([]), [])
        self.assertEqual(P.music_db(np.full(1600, 1.5)), [0.0])  # never above 0 dBFS

    def make_video(self, path, seconds=3.0):
        ffmpeg("-f", "lavfi", "-i", f"color=c=black:s=64x64:d={seconds}", "-f", "lavfi", "-i",
               f"aevalsrc='0.25*sin(2*PI*440*t)*lt(t,2)':s=16000:d={seconds}", "-c:v", "libx264", "-c:a", "aac", "-shortest",
               "-y", path)

    def test_publish_writes_music_when_the_video_has_sound(self):
        jid = make_song("Loud Artist", "Loud Song", duration=30.0, publish=False)
        self.make_video(A.job_path(jid, "out.mp4"))
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/publish", {"remove": []})
        self.assertEqual(st, 200, b)
        A.write_pitch(jid)
        base = A.load(jid)["published_as"]
        d = json.load(open(os.path.join(A.MV_DIR, base + P.EXT), encoding="utf-8"))
        self.valid("pitch.schema.json", d)
        self.assertEqual(d["music"]["hop"], 0.1)
        db = d["music"]["db"]
        self.assertAlmostEqual(len(db), 30, delta=1)                         # (the video is 3 s long)
        self.assertTrue(all(-15.5 <= x <= -14.5 for x in db[2:18]), db[:20])  # a sine of amplitude 0.25: RMS -15 dBFS
        self.assertTrue(all(x == -90.0 for x in db[22:]), db)

    def test_no_music_when_the_video_has_no_sound(self):
        jid = make_song("Quiet Artist", "Quiet Song")  # (placeholder video)
        d = json.load(open(os.path.join(A.MV_DIR, A.load(jid)["published_as"] + P.EXT), encoding="utf-8"))
        self.assertNotIn("music", d)
        self.assertTrue(d["notes"])

    def test_backfill_on_a_copy_of_the_library(self):
        lib = tempfile.mkdtemp(dir=_env.ROOT)
        mv = os.path.join(lib, "Music Videos")
        os.makedirs(mv)
        doc = {"version": 1, "base": "A - B (Music Video)", "duration": 3.0, "hop": 0.02, "notes": [[0.5, 1.0, 60.0]], "range": [60.0, 60.0]}
        for b in ("A - B (Music Video)", "C - D (Music Video)"):
            json.dump(dict(doc, base=b), open(os.path.join(mv, b + P.EXT), "w"))
        self.make_video(os.path.join(mv, "A - B (Music Video).mp4"))
        env = {k: v for k, v in os.environ.items() if not k.startswith("KARAOKE_")}
        run = lambda *a: subprocess.run([sys.executable, "-I", os.path.join(_env.REPO, "deploy", "write-music.py"), "--library", lib, *a],
                                        capture_output=True, text=True, env=env, timeout=120)
        r = run("--dry")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("music", json.load(open(os.path.join(mv, "A - B (Music Video)" + P.EXT))))
        self.assertIn("1 pitch file(s) to do; 1 without their video", r.stdout)
        r = run()
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.load(open(os.path.join(mv, "A - B (Music Video)" + P.EXT)))
        self.assertEqual({k: d[k] for k in doc if k != "base"}, {k: doc[k] for k in doc if k != "base"})  # the rest as it was
        self.assertAlmostEqual(len(d["music"]["db"]), 30, delta=1)
        self.valid("pitch.schema.json", d)
        self.assertIn("0 pitch file(s) to do", run().stdout)  # done once
        self.assertEqual([f for f in os.listdir(mv) if f.startswith(".")], [])


class Scores(Base):
    """§2.4 plays and §2.6 history events may carry the singing score."""
    SCORE = {"v": 1, "difficulty": "hard", "points": 512000, "pitch": 77.5, "timing": 90, "acc": 83.0, "grade": "AA", "max_combo": 41}

    @classmethod
    def setUpClass(cls):
        cls.tok = pair(name="Score kit")
        cls.song = make_song("Score Artist", "Score Song")
        cls.base = A.load(cls.song)["published_as"]

    def play(self, **kw):
        return {"pid": str(uuid.uuid4()), "id": self.song, "base": self.base, "at": int(time.time()), "seconds": 30,
                "result": "finished", "singers": 1, "style": None, "party": "Score night", **kw}

    def test_play_scores_stored_and_best_grade_in_the_library(self):
        p1, p2, p3 = self.play(score=self.SCORE), self.play(score=dict(self.SCORE, grade="AAA", words=64.0, partial=False)), self.play()
        st, _, b = api("POST", "/api/encore/plays", {"plays": [p1, p2, p3]}, token=self.tok)
        self.assertEqual((st, b), (200, {"stored": 3}))
        self.assertEqual(A.STORE.play_score(p1["pid"]), self.SCORE)
        self.assertIsNone(A.STORE.play_score(p3["pid"]))
        self.assertEqual(A.STORE.best_grades()[self.base], "AAA")
        lib = {s["file"]: s for s in A.library_songs()}
        self.assertEqual(lib[f"Music Videos/{self.base}.mp4"]["grade"], "AAA")

    def test_bad_scores_refused(self):
        for bad in (dict(self.SCORE, v=2), dict(self.SCORE, grade="S"), {k: v for k, v in self.SCORE.items() if k != "acc"},
                    dict(self.SCORE, pitch=101), dict(self.SCORE, words=-1), dict(self.SCORE, partial="yes")):
            self.assertEqual(api("POST", "/api/encore/plays", {"plays": [self.play(score=bad)]}, token=self.tok)[0], 422, bad)

    def test_history_scores_relayed_untouched(self):
        kit, home = str(uuid.uuid4()), str(uuid.uuid4())
        tk, th = pair(player=kit, name="Kit"), pair(player=home, name="Home")
        uid, eid = str(uuid.uuid4()), str(uuid.uuid4())
        ev = {"eid": eid, "stream": "history", "uid": uid, "at": time.time(), "song": self.song, "base": self.base,
              "result": "skipped", "party": "x", "score": dict(self.SCORE, partial=True, words=12.5)}
        st, _, b = api("POST", "/api/encore/profiles/sync", {"player": kit, "since": None, "events": [ev]}, token=tk)
        self.assertEqual(st, 200, b)
        got, since = [], None
        while True:  # (other tests' events come first: page through them)
            st, _, b = api("POST", "/api/encore/profiles/sync", {"player": home, "since": since, "events": []}, token=th)
            self.valid("profiles.sync.response.schema.json", b)
            got += [e for e in b["events"] if e["eid"] == eid]
            since = b["cursor"]
            if not b.get("more"):
                break
        self.assertEqual(got, [ev])
        bad = dict(ev, eid=str(uuid.uuid4()), score=dict(self.SCORE, grade="Z"))
        self.assertEqual(api("POST", "/api/encore/profiles/sync", {"player": kit, "since": None, "events": [bad]}, token=tk)[0], 400)


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg")
class WordsScoring(Base):
    """§2.9 through the whole app, with the stand-in aligner (same process, pipes and keep-alive as the real one)."""

    @classmethod
    def setUpClass(cls):
        cls.tok = pair(name="Words kit")
        cls.song = make_song("Words Artist", "Words Song", spec=((10.0, 4), (14.0, 4), (18.5, 5)), duration=30.0,
                             version="Duet take")
        cls.base = A.load(cls.song)["published_as"]
        cls.vbase = cls.base.replace("(Music Video)", "(Duet take)")
        cls.words = word_times(cls.base)

    def setUp(self):
        self.old = (A.WORDS.cmd, A.WORDS._start, A.WORDS.pick_device, A.WORDS.keep_alive, A.WORDS.now)
        A.WORDS.cmd = [sys.executable, "-I", FAKE_WORDS]
        A.WORDS._start = lambda: None       # jobs run here, one call at a time (run_once)
        A.WORDS.pick_device = lambda: "cpu"
        A.WORDS.keep_alive = 5.0
        A.WORDS_LIMIT.hits.clear()

    def tearDown(self):
        if A.WORDS.worker is not None:
            A.WORDS.worker.stop()
            A.WORDS.worker = None
        A.WORDS.cmd, A.WORDS._start, A.WORDS.pick_device, A.WORDS.keep_alive, A.WORDS.now = self.old
        for k in ("FAKE_WORDS_FAIL", "FAKE_WORDS_SLOW"):
            os.environ.pop(k, None)
        A.WORDS.purge()

    def sung(self, lines=None, skip=(), start=8.0, seconds=14.0):
        """A recording from video `start`: a tone on each word of these lines (all: None) except the (line, w) in skip."""
        spans = [(a - start, b - start) for i, k, a, b in self.words if (lines is None or i in lines) and (i, k) not in skip
                 and a >= start and b <= start + seconds]
        return recording(spans, seconds)

    def post(self, raw, expect=202, tok=None, **kw):
        h = headers(tok or self.tok, self.song, kw.pop("base", self.base), **kw)
        st, _, b = jcall(A.app, "POST", "/api/encore/score-words", raw, h)
        self.assertEqual(st, expect, b)
        self.valid("score-words.post.response.schema.json" if st == 202 else "error.schema.json", b)
        return b

    def status(self, job, tok=None, expect=200):
        st, _, b = jcall(A.app, "GET", f"/api/encore/score-words/{job}", None, {"Authorization": "Bearer " + (tok or self.tok)})
        self.assertEqual(st, expect, b)
        self.valid("score-words.get.response.schema.json" if st == 200 else "error.schema.json", b)
        return b

    def test_scores_the_words_sung(self):
        raw = self.sung(lines={1, 2}, skip={(2, 3)})   # recording from 8.0 s to 22.0 s; the singer skipped one word
        self.assertGreater(len(raw), 64 * 1024 // 8)
        b = self.post(raw, start=8.0, lines="1,2")
        self.assertEqual(b["status"], "queued")
        self.assertEqual(self.status(b["job"])["status"], "queued")
        self.assertEqual(A.WORDS.held_audio(), 1)
        self.assertTrue(A.WORDS.run_once())
        r = self.status(b["job"])
        self.assertEqual(r["status"], "done")
        self.assertEqual((r["heard"], r["total"], r["score"]), (7, 8, 88))
        self.assertEqual([(w["line"], w["w"]) for w in r["words"] if not w["ok"]], [(2, 3)])
        self.assertEqual(A.WORDS.held_audio(), 0)
        self.assertEqual(A.WORDS.worker.device, "cpu")
        # every line but backing ones when the player doesn't say; words outside the recording aren't judged
        b = self.post(self.sung(), start=8.0)
        A.WORDS.run_once()
        r = self.status(b["job"])
        self.assertEqual((r["heard"], r["total"]), (13, 13))
        self.assertEqual({w["line"] for w in r["words"]}, {1, 2, 3})

    def test_start_and_rate_map_the_recording_to_video_time(self):
        start, rate = 9.0, 1.25   # a speed-up from the start: recording second t is video 9 + 1.25 t
        spans = [((a - start) / rate, (b - start) / rate) for i, k, a, b in self.words if i == 2]
        b = self.post(recording(spans, 10.0), start=start, rate=rate, lines="2")
        A.WORDS.run_once()
        r = self.status(b["job"])
        self.assertEqual((r["heard"], r["total"]), (4, 4), r)
        b = self.post(recording(spans, 10.0), start=start, rate=1.0, lines="2")   # the wrong rate: the words aren't there
        A.WORDS.run_once()
        self.assertLess(self.status(b["job"])["heard"], 4)

    def test_a_version_uses_its_own_words_file(self):
        vw = word_times(self.vbase)
        b = self.post(recording([(a - 8.0, b - 8.0) for i, k, a, b in vw if i == 1], 14.0), base=self.vbase, start=8.0, lines="1")
        A.WORDS.run_once()
        r = self.status(b["job"])
        self.assertEqual((r["score"], r["total"]), (100, 4))

    def test_only_its_own_player_sees_a_job_and_answers_are_forgotten(self):
        b = self.post(self.sung(lines={1}), start=8.0, lines="1")
        other = pair(name="Other kit")
        self.status(b["job"], tok=other, expect=410)
        self.status("0" * 32, expect=410)
        self.status("not-a-job", expect=410)
        A.WORDS.run_once()
        self.assertEqual(self.status(b["job"])["status"], "done")
        A.WORDS.now = lambda: time.time() + W.KEEP + 1
        self.status(b["job"], expect=410)
        self.assertNotIn(b["job"], A.WORDS.jobs)

    def test_a_job_waiting_too_long_ends_busy_and_drops_its_audio(self):
        b = self.post(self.sung(lines={1}), start=8.0, lines="1")
        A.WORDS.now = lambda: time.time() + W.MAX_WAIT + 1
        r = self.status(b["job"])
        self.assertEqual(r, {"status": "busy", "reason": "waited too long"})
        self.assertEqual(A.WORDS.held_audio(), 0)
        A.WORDS.now = time.time

    def test_busy_keeps_nothing(self):
        A.WORDS.pick_device = lambda: None
        read = []
        real_body = A._audio_body
        A._audio_body = lambda req: read.append(1) or real_body(req)
        try:
            b = self.post(self.sung(lines={1}), start=8.0, lines="1")
        finally:
            A._audio_body = real_body
        self.assertEqual(read, [])                     # answered busy without reading the recording
        self.assertEqual((b["status"], b["eta"]), ("busy", 0))
        self.assertEqual(self.status(b["job"]), {"status": "busy", "reason": "the Studio's graphics card is busy"})
        self.assertEqual(A.WORDS.held_audio(), 0)
        A.WORDS.pick_device = lambda: "cpu"            # (free again by its turn, but the job was already answered busy)
        self.assertFalse(A.WORDS.run_once())
        # the device can also be gone by the job's turn
        b = self.post(self.sung(lines={1}), start=8.0, lines="1")
        A.WORDS.pick_device = lambda: None
        A.WORDS.run_once()
        self.assertEqual(self.status(b["job"])["status"], "busy")
        self.assertEqual(A.WORDS.held_audio(), 0)

    def test_failures(self):
        b = self.post(b"this is not audio", ctype="audio/webm", start=8.0)
        A.WORDS.run_once()
        self.assertEqual(self.status(b["job"]), {"status": "failed", "reason": "couldn't read the audio (Opus in ogg or webm)"})
        os.environ["FAKE_WORDS_FAIL"] = "1"
        b = self.post(self.sung(lines={1}), start=8.0, lines="1")
        A.WORDS.run_once()
        r = self.status(b["job"])
        self.assertEqual(r["status"], "failed")
        self.assertIn("the aligner failed", r["reason"])
        del os.environ["FAKE_WORDS_FAIL"]
        b = self.post(recording([], 4.0), start=100.0, lines="1")   # nothing to score in it (recorded after the song)
        A.WORDS.run_once()
        self.assertEqual(self.status(b["job"]), {"status": "failed", "reason": "no words to score in this recording"})
        self.assertEqual(A.WORDS.held_audio(), 0)

    def test_the_background_thread_scores_it(self):
        sc = W.Scorer(A.words_song, lambda: "cpu", cmd=[sys.executable, "-I", FAKE_WORDS], keep_alive=1.0)  # (its own)
        raw = self.sung(lines={1})
        jid, eta, st = sc.submit("p1", raw, "ogg", self.song, self.base, 8.0, 1.0, [1], 30.0)
        self.assertEqual(st, "queued")
        self.assertGreaterEqual(eta, 1)
        deadline, r = time.time() + 30, None
        while time.time() < deadline:
            r = sc.get("p1", jid)
            if r["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual((r["status"], r["score"]), ("done", 100))
        self.assertTrue(sc.thread.is_alive())
        self.assertEqual(sc.held_audio(), 0)
        self.assertIsNone(sc.get("p2", jid))

    def test_header_checks_before_the_body(self):
        raw = self.sung(lines={1})
        good = headers(self.tok, self.song, self.base, start=8.0)
        cases = [({"Content-Type": "audio/mpeg"}, "Content-Type"), ({"X-Encore-Start": "soon"}, "X-Encore-Start"),
                 ({"X-Encore-Start": "nan"}, "X-Encore-Start"), ({"X-Encore-Rate": "3"}, "X-Encore-Rate"),
                 ({"X-Encore-Rate": ""}, "X-Encore-Rate"), ({"X-Encore-Lines": "1;2"}, "X-Encore-Lines"),
                 ({"X-Encore-Lines": "77"}, "X-Encore-Lines"), ({"X-Encore-Base": "Nobody - Nothing (Music Video)"}, "unknown song"),
                 ({"X-Encore-Song": "20260101-000000-someone-else"}, "unknown song"), ({"X-Encore-Song": ""}, "X-Encore-Song")]
        for change, said in cases:
            A.WORDS_LIMIT.hits.clear()   # (wrong calls count too: 6 a minute)
            st, _, b = jcall(A.app, "POST", "/api/encore/score-words", raw, dict(good, **change))
            self.assertEqual(st, 422, (change, b))
            self.assertIn(said, b["error"])
        h = dict(good)
        del h["X-Encore-Start"]
        self.assertEqual(jcall(A.app, "POST", "/api/encore/score-words", raw, h)[0], 422)
        h = dict(good)
        del h["Authorization"]
        self.assertEqual(jcall(A.app, "POST", "/api/encore/score-words", raw, h)[0], 401)
        self.assertEqual(A.WORDS.held_audio(), 0)
        self.assertEqual(jcall(A.app, "POST", "/api/encore/score-words", b"", good)[0], 422)  # no recording

    def test_body_limit_only_for_this_route(self):
        big = os.urandom(1024 * 1024)   # 1 MB: fine here (the recording isn't decodable, so it fails later)
        b = self.post(big, ctype="audio/webm", start=8.0)
        A.WORDS.run_once()
        self.assertEqual(self.status(b["job"])["status"], "failed")
        h = headers(self.tok, self.song, self.base, start=8.0)
        too_big = b"\0" * (W.MAX_BYTES + 1)
        st, _, b = jcall(A.app, "POST", "/api/encore/score-words", too_big, h)
        self.assertEqual((st, b), (413, {"error": "the recording is too big (8 MB at most)"}))
        st, _, b = jcall(A.app, "POST", "/api/encore/score-words", too_big, dict(h, **{"Content-Length": "100"}))  # (lying)
        self.assertEqual(st, 413)
        self.assertEqual(api("POST", "/api/encore/plays", b"{" + b" " * (64 * 1024) + b"}", token=self.tok)[0], 413)  # others: 64 KB
        self.assertEqual(api("POST", "/api/encore/flags", b"{" + b" " * (64 * 1024) + b"}", token=self.tok)[0], 413)
        self.assertEqual(A.WORDS.held_audio(), 0)

    def test_rate_limit_per_player(self):
        A.WORDS_LIMIT.hits.clear()
        raw = self.sung(lines={1})
        for _ in range(3):
            self.post(raw, start=8.0, lines="1")
        b = self.post(raw, expect=429, start=8.0, lines="1")       # 3 waiting for this player
        self.assertIn("still being scored", b["error"])
        other = pair(name="Second words kit")
        self.post(raw, tok=other, start=8.0, lines="1")            # another player isn't held up
        while A.WORDS.run_once():
            pass
        self.post(raw, start=8.0, lines="1")
        self.post(raw, start=8.0, lines="1")
        b = self.post(raw, expect=429, start=8.0, lines="1")       # 6 a minute
        self.assertIn("too many", b["error"])
        while A.WORDS.run_once():
            pass

    def test_nothing_on_disk_and_nothing_in_the_log(self):
        def files():
            out = set()
            for root in (_env.ROOT, tempfile.gettempdir()):
                for d, _, fs in os.walk(root):
                    out |= {os.path.join(d, f) for f in fs}
            return out
        log = os.path.join(A.C.LOGS, "studio.log")
        before, log_before = files(), os.path.getsize(log) if os.path.exists(log) else 0
        raw = self.sung(lines={1, 2})
        b = self.post(raw, start=8.0, lines="1,2")
        A.WORDS.run_once()
        self.assertEqual(self.status(b["job"])["status"], "done")
        self.assertEqual(files() - before, set())                  # no temp file, no copy kept anywhere
        for h in A.log.handlers:
            h.flush()
        added = open(log, "rb").read()[log_before:]
        self.assertIn(b"words scor", added)                        # (it did log the job)
        self.assertNotIn(raw[:64], added)
        texts = {w[2] for l in json.load(open(os.path.join(A.MV_DIR, self.base + ".words.json")))["lines"] for w in l["words"]}
        self.assertFalse([t for t in texts if re.search(rb"\b" + re.escape(t.encode()) + rb"\b", added)])  # no lyrics either
        self.assertIsNone(A.WORDS.jobs[b["job"]]["audio"])

    def test_the_aligner_process_ends_after_its_keep_alive(self):
        A.WORDS.keep_alive = 0.5
        b = self.post(self.sung(lines={1}), start=8.0, lines="1")
        A.WORDS.run_once()
        w = A.WORDS.worker
        self.assertTrue(w.alive())
        deadline = time.time() + 10
        while w.alive() and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(w.alive())                                # it exits by itself (the model leaves the card)
        b = self.post(self.sung(lines={1}), start=8.0, lines="1")
        A.WORDS.run_once()                                         # a new one is started for the next job
        self.assertEqual(self.status(b["job"])["status"], "done")
        self.assertIsNot(A.WORDS.worker, w)

    def test_gpu_rules(self):
        old = (A.gpu_free_mib, A.job_index, A.NEED_GPU_MIB, A.C.USE_LOCAL_GPU, W.CPU_OK)
        try:
            A.NEED_GPU_MIB, A.C.USE_LOCAL_GPU = 9000, True
            A.gpu_free_mib = lambda: 30000
            A.job_index = lambda: [{"status": "done"}]
            self.assertEqual(A.words_device(), "cuda")
            A.gpu_free_mib = lambda: 11000                       # a song would no longer fit beside it
            self.assertEqual(A.words_device(), "cpu")
            A.gpu_free_mib = lambda: 30000
            A.job_index = lambda: [{"status": "queued"}]         # a song waits for a card
            self.assertEqual(A.words_device(), "cpu")
            A.job_index = lambda: [{"status": "done"}]
            A.slots["local"] = "20260101-000000-being-made"      # a song is being made here
            self.assertEqual(A.words_device(), "cpu")
            W.CPU_OK = False
            self.assertIsNone(A.words_device())                  # nowhere: busy
        finally:
            A.slots["local"] = None
            A.gpu_free_mib, A.job_index, A.NEED_GPU_MIB, A.C.USE_LOCAL_GPU, W.CPU_OK = old

    def test_an_idle_gpu_aligner_stops_when_a_song_wants_the_card(self):
        A.WORDS.pick_device = lambda: "cuda"
        b = self.post(self.sung(lines={1}), start=8.0, lines="1")
        A.WORDS.run_once()
        w = A.WORDS.worker
        self.assertEqual((w.device, w.alive()), ("cuda", True))
        A.WORDS.release_gpu()                                    # (the scheduler does this when a song is queued)
        self.assertFalse(w.alive())
        self.assertIsNone(A.WORDS.worker)
        b = self.post(self.sung(lines={1}), start=8.0, lines="1")
        A.WORDS.run_once()
        w = A.WORDS.worker
        A.WORDS.pick_device = lambda: "cpu"                      # the card is wanted: the idle check stops it too
        A.WORDS.idle_check()
        self.assertFalse(w.alive())
        A.WORDS.release_gpu()                                    # (nothing to stop: no error)

    def test_the_original_vocals_decide_which_words_are_judged(self):
        jid = make_song("Ref Artist", "Ref Song", spec=((10.0, 4), (14.0, 4)), duration=20.0)
        base = A.load(jid)["published_as"]
        words = word_times(base)
        # the "original singer" sings line 1 only (line 2 is buried in the mix): line 2 isn't judged
        orig = [(a, b) for i, k, a, b in words if i == 1]
        ffmpeg("-f", "lavfi", "-i", f"aevalsrc='{tone_expr(orig)}':s=16000:d=20", "-c:a", "aac", "-y", A.job_path(jid, "vocals.m4a"))
        sung = recording([(a - 8.0, b - 8.0) for i, k, a, b in words], 12.0)
        st, _, b = jcall(A.app, "POST", "/api/encore/score-words", sung, headers(self.tok, jid, base, start=8.0))
        self.assertEqual(st, 202, b)
        A.WORDS.run_once()
        r = self.status(b["job"])
        self.assertEqual((r["total"], {w["line"] for w in r["words"]}), (4, {1}))
        cache = A.job_path(jid, W.REF)
        self.assertTrue(os.path.exists(cache))                   # kept for the next singer
        ref = json.load(open(cache))
        self.assertEqual(len(ref["conf"]), 8)
        self.assertEqual(ref["conf"]["1:0"][1], None)            # (no instrumental to listen to: the placeholder video)
        asked = []
        real_ask = A.WORDS._ask
        A.WORDS._ask = lambda *a: asked.append(1) or real_ask(*a)
        try:
            st, _, b = jcall(A.app, "POST", "/api/encore/score-words", sung, headers(self.tok, jid, base, start=8.0))
            A.WORDS.run_once()
        finally:
            A.WORDS._ask = real_ask
        self.assertEqual(len(asked), 1)                          # the recording only: the references came from the cache
        self.assertEqual(self.status(b["job"])["total"], 4)


if __name__ == "__main__":
    unittest.main()
