"""Party auto-make (contract §2.2b, automake.py): who may use it (the admin's setting and the per-player toggle, never
an agent), the limits, the search -> pick -> make -> quality floor -> publish path with the stand-in pipeline, the
failures and their reasons, songs the Studio already has, the stage and ETA on polls, and the "quick make" marker.
No GPU, no YouTube: the search is made up and pipeline.py is tests/fake_pipeline.py."""
import json, os, time, unittest, uuid

from tests import _env  # noqa: F401  (must come first: throwaway folders)
from tests._asgi import jcall  # noqa: E402
import app as A  # noqa: E402
import automake as AM  # noqa: E402
import config as C  # noqa: E402
import schemalite as S  # noqa: E402
from assgen import save_lines  # noqa: E402

A.PIPELINE = _env.FAKE
A.gpu_free_mib = lambda: 99999
A.pitch_later = lambda jid: None
PRESET = os.environ["ENCORE_TOKEN"]

SEARCHES = {}      # query -> answer (or an Exception to raise)
MAKE = {}          # extra job fields for the stand-in pipeline (fake mode, confidence)
_orig_new_job = A.new_job


def fake_search(q):
    got = SEARCHES.get(q)
    if isinstance(got, Exception):
        raise got
    if got is not None:
        return got
    artist, _, title = q.partition(" - ")
    return made_up(artist, title)


def made_up(artist, title, dur=200, timed=True, videos=True):
    v = {"id": "vid" + uuid.uuid4().hex[:8], "title": f"{artist} - {title} (Official Video)", "channel": artist, "duration": dur,
         "views": 1, "verified": True, "official_channel": True, "flags": [], "score": 9.0, "thumb": ""}
    l = {"source": "lrclib", "id": 4242, "artist": artist, "title": title, "album": "", "duration": float(dur), "timed": timed,
         "plain": True, "latin": True, "japanese": False, "native": False, "english": True, "lines": 30, "first": "",
         "name_match": True, "artist_match": True}
    return {"artist": artist, "title": title, "videos": [v] if videos else [], "lyrics": [l], "notes": []}


A.search = fake_search
A.new_job = lambda fields, actor: _orig_new_job(dict(fields, **MAKE) if fields.get("automake") else fields, actor)


def wait_for(cond, timeout=30, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def api(method, path, body=None, token=PRESET, headers=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    return jcall(A.app, method, path, body, h)


def valid(t, schema, data):
    t.assertEqual(S.errors(schema, data), [], json.dumps(data)[:400])


n = [0]


def req(title, artist="Auto Artist", **kw):
    n[0] += 1
    return {"rid": str(uuid.uuid4()), "artist": artist, "title": f"{title} {n[0]}", "note": "", "by": "Mia",
            "party": "Friday", "at": int(time.time()), "auto": True, "queue": {"eid": f"q{n[0]:010d}"}, **kw}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        code, _ = A.STORE.new_code()
        st, _, b = jcall(A.app, "POST", "/api/encore/pair", {"code": code, "name": "Party kit", "player": str(uuid.uuid4()),
                                                            "encore": "0.7", "contract": "1"}, client=("192.0.2.77", 40077))
        assert st == 200, b
        cls.tok = b["token"]
        cls.player = A.STORE.auth(cls.tok)

    def setUp(self):
        for j in os.listdir(A.JOBS):  # only this test's songs in the queue
            if A.ID_RE.match(j):
                job = A.load(j)
                if job["status"] in ("queued", "running"):
                    job["status"] = "failed"
                    A.save(j, job)
        wait_for(lambda: A.slots["local"] is None and A.slots["remote"] is None and not A.runners, 30)
        C.USE_LOCAL_GPU, C.GPU_HELPER_URL = True, ""
        A.STORE.q("update requests set am_at=null")
        A.STORE.q("delete from kv where k='automake'")
        AM.save(A.STORE, {"on": True})
        A.STORE.set_automake(self.player, True)
        A._MAKE_S["v"] = None
        SEARCHES.clear()
        MAKE.clear()
        MAKE.update(fake="conf")
        self.sync = True
        A.automake_enqueue = lambda rid: A.automake_run(rid)   # in order, inside the call (one test uses the thread)

    def post(self, body, token=None):
        st, _, b = api("POST", "/api/encore/requests", body, token=token or self.tok)
        self.assertEqual(st, 200, b)
        valid(self, "requests.post.response.schema.json", b)
        return b

    def poll(self, rid, token=None):
        st, _, b = api("GET", "/api/encore/requests?rids=" + rid, token=token or self.tok)
        self.assertEqual(st, 200, b)
        valid(self, "requests.get.response.schema.json", b)
        return {r["rid"]: r for r in b["requests"]}.get(rid)

    def make(self, rid):
        """Run the queued song through the stand-in pipeline; wait until the request stops moving."""
        A.schedule_once()
        ok = wait_for(lambda: (self.poll(rid) or {}).get("status") not in ("open", "making"), 40)
        self.assertTrue(ok, self.poll(rid))
        return self.poll(rid)


class Allowed(Base):
    def test_only_with_the_setting_and_the_player_toggle(self):
        st, _, h = api("GET", "/api/encore/hello", token=self.tok)
        valid(self, "hello.response.schema.json", h)
        self.assertTrue(h["auto_make"]["allowed"])
        self.assertGreaterEqual(h["auto_make"]["minutes"], 1)
        for setting, toggle in ((False, True), (True, False)):
            AM.save(A.STORE, {"on": setting})
            A.STORE.set_automake(self.player, toggle)
            self.assertEqual(api("GET", "/api/encore/hello", token=self.tok)[2]["auto_make"], {"allowed": False, "minutes": None})
            a = self.post(req("Not Allowed"))
            self.assertEqual(a["status"], "open")
            self.assertNotIn("auto", a)  # a normal request: Encore knows it wasn't taken
            self.assertNotIn("auto", self.poll(a["rid"]))
            self.assertIsNone(A.STORE.get_request(a["rid"])["job"])
        # another player (the preset) isn't allowed just because this one is
        AM.save(A.STORE, {"on": True})
        A.STORE.set_automake(self.player, True)
        self.assertNotIn("auto", self.post(req("Preset"), token=PRESET))

    def test_admin_only_never_an_agent(self):
        key, _ = A.KEYS.create("Robot", ["read", "edit", "notes", "make", "publish", "remake", "clear_marks", "delete"])
        h = {"X-Studio-Agent-Key": key}
        self.assertEqual(jcall(A.app, "POST", "/api/automake", {"on": True}, h)[0], 403)
        self.assertEqual(jcall(A.app, "GET", "/api/automake", None, h)[0], 403)
        self.assertEqual(jcall(A.app, "POST", f"/api/players/{self.player}/automake", {"on": True}, h)[0], 403)
        # a make by an agent (or anyone) can't smuggle the auto-make marks in
        st, _, b = jcall(A.app, "POST", "/api/jobs", {"yt": "abcdefghijk", "lrc_id": 1, "lyrics_source": "lrclib", "artist": "A",
                                                     "title": "Smuggled", "automake": {"rid": "x"}, "quick": {"at": 1}}, h)
        self.assertEqual(st, 200, b)
        job = A.load(b["id"])
        self.assertNotIn("automake", job)
        self.assertNotIn("quick", job)
        A.set_job(b["id"], status="failed")

    def test_settings_and_toggle_endpoints(self):
        self.assertEqual(jcall(A.app, "POST", "/api/automake", {"per_hour": 0})[0], 400)
        self.assertEqual(jcall(A.app, "POST", "/api/automake", {"on": "yes"})[0], 400)
        self.assertEqual(jcall(A.app, "POST", "/api/automake", {"nope": 1})[0], 400)
        st, _, b = jcall(A.app, "POST", "/api/automake", {"per_hour": 4, "max_minutes": 6, "gpu_wait": 5, "per_day": 9, "floor": 70})
        self.assertEqual(st, 200, b)
        self.assertEqual(b["settings"], {"on": True, "per_hour": 4, "max_minutes": 6, "gpu_wait": 5, "per_day": 9, "floor": 70})
        self.assertEqual(jcall(A.app, "POST", f"/api/players/{self.player}/automake", {"on": False})[0], 200)
        row = next(p for p in jcall(A.app, "GET", "/api/players")[2]["players"] if p["player"] == self.player)
        self.assertFalse(row["automake"])
        self.assertEqual(jcall(A.app, "POST", "/api/players/no-such-player/automake", {"on": True})[0], 404)
        self.assertEqual(jcall(A.app, "POST", f"/api/players/{self.player}/automake", {"on": 1})[0], 400)


class Limits(Base):
    def test_per_hour_and_per_day(self):
        AM.save(A.STORE, {"per_hour": 1})
        MAKE.update(fake="slow")
        a = self.post(req("First"))
        self.assertEqual((a["status"], a["auto"], a["stage"]), ("open", True, "finding"))
        b = self.post(req("Second"))
        self.assertEqual((b["status"], b["auto"]), ("failed", True))
        self.assertIn("1 songs an hour", b["reason"])
        self.assertEqual(self.poll(b["rid"])["status"], "failed")
        self.assertEqual(self.post(dict(req("Again"), rid=b["rid"])), b)  # the same rid: the same answer
        AM.save(A.STORE, {"per_hour": 10, "per_day": 1})
        self.assertIn("for today", self.post(req("Third"))["reason"])
        A.set_job(A.STORE.get_request(a["rid"])["job"], status="failed")

    def test_busy_cards_and_long_songs(self):
        AM.save(A.STORE, {"gpu_wait": 2})
        MAKE.update(fake="slow")
        busy = [self.post(req("Busy"))["rid"]]   # one card: each song takes the default ~2 min
        busy.append(self.post(req("Busy"))["rid"])
        c = self.post(req("Too Busy"))
        self.assertEqual(c["status"], "failed")
        self.assertIn("graphics cards are busy", c["reason"])
        for r in busy:
            A.set_job(A.STORE.get_request(r)["job"], status="failed")
        C.USE_LOCAL_GPU = False   # no card at all
        self.assertIn("no graphics card", self.post(req("No Card"))["reason"])
        C.USE_LOCAL_GPU = True
        body = req("Epic")
        SEARCHES[f"{body['artist']} - {body['title']}"] = made_up(body["artist"], body["title"], dur=9 * 60 + 5)
        self.post(body)
        r = self.poll(body["rid"])
        self.assertEqual((r["status"], r["reason"]), ("failed", "too long for a quick make (9:05; the limit is 7 min)"))


class Making(Base):
    def test_floor_passes_publishes_with_attribution_and_marker(self):
        MAKE.update(fake_lines=10, fake_sure=9)
        body = req("Glass Orchard")
        a = self.post(body)
        self.assertTrue(a["eta"] > time.time())
        r = self.poll(body["rid"])
        self.assertEqual((r["status"], r["stage"]), ("making", "downloading"))
        jid = r["job"]
        job = A.load(jid)
        self.assertEqual(job["made_by"]["by"], "auto-make for Party kit")
        self.assertEqual(job["automake"]["by"], "Mia")
        r = self.make(body["rid"])
        self.assertEqual((r["status"], r["id"], r["auto"], r["stage"], r["eta"]), ("published", jid, True, None, None))
        job = A.load(jid)
        self.assertEqual(job["published_by"]["by"], "auto-make for Party kit")
        self.assertEqual((job["quick"]["for"], job["quick"]["sure"], job["quick"]["lines"]), ("Party kit", 9, 10))
        self.assertTrue(os.path.exists(os.path.join(A.MV_DIR, job["published_as"] + ".mp4")))
        brief = next(s for s in A.list_jobs(brief=1) if s["id"] == jid)
        self.assertEqual(brief["quick"], 1)
        # an agent checking it doesn't count as a person's review; a person's does
        key, _ = A.KEYS.create("Checker", ["read", "clear_marks"])
        self.assertEqual(jcall(A.app, "POST", f"/api/jobs/{jid}/reviewed", {"on": True}, {"X-Studio-Agent-Key": key})[0], 200)
        self.assertNotIn("reviewed", A.load(jid)["quick"])
        jcall(A.app, "POST", f"/api/jobs/{jid}/reviewed", {"on": True}, {"X-authentik-username": "alex"})
        self.assertEqual(A.load(jid)["quick"]["reviewed_by"], "alex")
        self.assertNotIn("quick", next(s for s in A.list_jobs(brief=1) if s["id"] == jid))
        # asking again for the published song: have, never a second make
        again = self.post(dict(req("x"), title=body["title"]))
        self.assertEqual((again["status"], again["id"]), ("have", jid))
        self.assertNotIn("auto", again)

    def test_below_the_floor_waits_for_a_look(self):
        for fields, why in (({"fake_lines": 10, "fake_sure": 3}, "3 of 10 lines heard"),
                            ({"fake_lines": 10, "fake_sure": None}, "couldn't follow"),
                            ({"fake_lines": 10, "fake_sure": 10, "fake_squeezed": 3}, "squeezed"),
                            ({"fake_lines": 2, "fake_sure": 2}, "only 2 lines")):
            MAKE.clear()
            MAKE.update(fake="conf", **fields)
            body = req("Held")
            self.post(body)
            r = self.make(body["rid"])
            self.assertEqual((r["status"], r["reason"], r["auto"]), ("ready", "needs a look", True), fields)
            job = A.load(r["job"])
            self.assertIn(why, job["auto_held"])
            self.assertFalse(job.get("published"))
            self.assertNotIn("quick", job)

    def test_failures_and_their_reasons(self):
        cases = []
        b1 = req("Search Fails")
        SEARCHES[f"{b1['artist']} - {b1['title']}"] = A.HTTPException(502, "YouTube search failed")
        cases.append((b1, "couldn't search for it (YouTube or the lyrics sites didn't answer)"))
        b2 = req("No Timed")
        SEARCHES[f"{b2['artist']} - {b2['title']}"] = made_up(b2["artist"], b2["title"], timed=False)
        cases.append((b2, "no timed lyrics found"))
        b3 = req("No Video")
        SEARCHES[f"{b3['artist']} - {b3['title']}"] = made_up(b3["artist"], b3["title"], videos=False)
        cases.append((b3, "no music video found"))
        for body, why in cases:
            self.post(body)
            r = self.poll(body["rid"])
            self.assertEqual((r["status"], r["reason"], r["auto"]), ("failed", why, True))
        MAKE.update(fake="baddl")
        b4 = req("Bot Check")
        self.post(b4)
        r = self.make(b4["rid"])
        self.assertEqual((r["status"], r["reason"]), ("failed", "couldn't download the video"))
        # stopped at the Studio
        MAKE.update(fake="slow")
        b5 = req("Stopped")
        self.post(b5)
        jid = self.poll(b5["rid"])["job"]
        jcall(A.app, "POST", f"/api/jobs/{jid}/cancel", {})
        self.assertTrue(wait_for(lambda: self.poll(b5["rid"])["status"] == "failed"))
        self.assertEqual(self.poll(b5["rid"])["reason"], "it was stopped at the Studio")
        self.assertEqual(AM.failure_reason({"status": "failed", "step": "align", "error": "x"}), "lyrics didn't match the singing")

    def test_a_song_the_studio_has_under_the_picked_names(self):
        # published under the lyrics' names, asked for with a typo: found after the pick
        jid = make_published("Lumen Coast", "Glass River")
        body = req("x", artist="lumen coast")
        body["title"] = "glas river"
        SEARCHES["lumen coast - glas river"] = made_up("Lumen Coast", "Glass River")
        a = self.post(body)
        self.assertEqual(a["status"], "open")
        r = self.poll(body["rid"])
        self.assertEqual((r["status"], r["id"]), ("have", jid))

    def test_never_replaces_a_published_song(self):
        MAKE.update(fake="slow")
        body = req("Race")
        self.post(body)
        jid = self.poll(body["rid"])["job"]
        other = make_published(body["artist"], body["title"])  # a person publishes the same song meanwhile
        A.update_catalog()
        d = A.job_path(jid)  # (the song is never run: it ends as made here, as if it had finished)
        save_lines(os.path.join(d, "words.json"), [{"words": [[t, t + 0.5, "la"], [t + 0.5, t + 1, "da"], [t + 1, t + 1.5, "na"]]}
                                                   for t in range(5, 50, 4)], [])
        for f in ("out.mp4", "vocals.m4a"):
            open(os.path.join(d, f), "wb").write(b"new make " * 10)
        A.set_job(jid, status="done", step="done", result={"lines": 12, "confidence": {"lines": 12, "check": 0, "sure": 12}})
        A.auto_finish(jid)
        job = A.load(jid)
        self.assertFalse(job.get("published"))
        self.assertIn("already in the library", job["auto_held"])
        self.assertEqual(open(os.path.join(A.MV_DIR, A.load(other)["published_as"] + ".mp4"), "rb").read()[:6], b"placeh")
        r = self.poll(body["rid"])
        self.assertEqual((r["status"], r["id"]), ("have", other))

    def test_stages_and_eta_from_measured_make_times(self):
        for k in range(3):  # three songs that took 100 s each on this card
            make_published("Timing", f"Song {k}", started=time.time() - 1000, finished=time.time() - 900)
        A._MAKE_S["v"] = None
        MAKE.update(fake="slow")
        first = req("Queue One")
        a = self.post(first)
        t0 = time.time()
        self.assertAlmostEqual(a["eta"], t0 + AM.SEARCH_S + 100 + AM.PUBLISH_S, delta=15)
        second = req("Queue Two")
        self.post(second)
        r1, r2 = self.poll(first["rid"]), self.poll(second["rid"])
        self.assertEqual((r1["stage"], r2["stage"]), ("downloading", "downloading"))
        self.assertGreater(r2["eta"], r1["eta"] + 90)   # one card: the second waits for the first
        A.schedule_once()
        self.assertTrue(wait_for(lambda: A.load(r1["job"])["status"] == "running"))
        A.set_job(r1["job"], step="align")
        r1 = self.poll(first["rid"])
        self.assertEqual((r1["status"], r1["stage"]), ("making", "making"))
        started = A.load(r1["job"])["started"]
        self.assertAlmostEqual(r1["eta"], started + 100 + AM.PUBLISH_S, delta=11)
        for r in (r1, r2):
            jcall(A.app, "POST", f"/api/jobs/{r['job']}/cancel", {})
        wait_for(lambda: A.slots["local"] is None, 20)

    def test_a_retried_or_switched_off_auto_make_is_never_published_on_its_own(self):
        MAKE.update(fake="baddl")
        body = req("Retried")
        self.post(body)
        r = self.make(body["rid"])
        self.assertEqual(r["status"], "failed")
        jid = A.STORE.get_request(body["rid"])["job"]
        key, _ = A.KEYS.create("Retrier", ["read", "remake"])   # may retry, may not publish
        MAKE.clear()
        A.set_job(jid, fake="conf", fake_lines=10, fake_sure=10)
        self.assertEqual(jcall(A.app, "POST", f"/api/jobs/{jid}/retry", {}, {"X-Studio-Agent-Key": key})[0], 200)
        A.schedule_once()
        self.assertTrue(wait_for(lambda: A.load(jid).get("auto_held")))
        self.assertFalse(A.load(jid).get("published"))
        self.assertEqual(self.poll(body["rid"])["status"], "failed")   # Encore dropped its placeholder: it stays so
        # the admin switches auto-make off while a song is being made: it waits for a person
        MAKE.update(fake="conf", fake_lines=10, fake_sure=10)
        b2 = req("Switched Off")
        self.post(b2)
        AM.save(A.STORE, {"on": False})
        r2 = self.make(b2["rid"])
        self.assertEqual((r2["status"], r2["reason"]), ("ready", "needs a look"))
        self.assertFalse(A.load(r2["job"]).get("published"))

    def test_a_card_held_by_something_else_counts_as_busy(self):
        A.gpu_free_mib, A.NEED_GPU_MIB = (lambda: 100), 9000   # (an LLM holds the card)
        try:
            self.assertIn("no graphics card", self.post(req("Held Card"))["reason"])
            self.assertEqual(api("GET", "/api/encore/hello", token=self.tok)[2]["auto_make"], {"allowed": True, "minutes": None})
        finally:
            A.gpu_free_mib, A.NEED_GPU_MIB = (lambda: 99999), 0

    def test_the_worker_thread_and_restart_recovery(self):
        A.automake_enqueue = _real_enqueue
        MAKE.update(fake="slow")
        body = req("Threaded")
        self.post(body)
        self.assertTrue(wait_for(lambda: self.poll(body["rid"])["status"] == "making"))
        A.set_job(self.poll(body["rid"])["job"], status="failed")
        # a request left at "finding" (the Studio restarted) is picked up again
        b2 = req("Recovered")
        A.automake_enqueue = lambda rid: None
        self.post(b2)
        self.assertEqual(self.poll(b2["rid"])["stage"], "finding")
        A.automake_enqueue = _real_enqueue
        A.recover_automake()
        self.assertTrue(wait_for(lambda: self.poll(b2["rid"])["status"] == "making"))
        A.set_job(self.poll(b2["rid"])["job"], status="failed")


_real_enqueue = A.automake_enqueue
_k = [0]


def make_published(artist, title, **kw):
    _k[0] += 1
    jid = f"20260303-{_k[0]:06d}-auto-{_k[0]}"
    d = os.path.join(A.JOBS, jid)
    os.makedirs(d)
    save_lines(os.path.join(d, "words.json"), [{"words": [[5.0, 5.5, "la"], [5.5, 6.0, "da"]]}], [])
    for f in ("out.mp4", "vocals.m4a"):
        open(os.path.join(d, f), "wb").write(b"placeholder " * 10)
    job = {"id": jid, "yt": "aaaaaaaaaaa", "artist": artist, "title": title, "status": "done", "step": "done",
           "created": time.time(), "finished": kw.pop("finished", time.time()), "offset": 0.0, "gpu": A.LOCAL_GPU,
           "result": {"duration": 30.0, "lines": 1}, "log": [], **kw}
    A.save(jid, job)
    A.rebuild_lyrics(jid, job)
    st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/publish", {"remove": []})
    assert st == 200, b
    return jid


if __name__ == "__main__":
    unittest.main()


# ---------------- the video check (videocheck.py) and ks make --auto (batches) ----------------
import videocheck as VC  # noqa: E402


class VideoCheckRules(unittest.TestCase):
    def test_lyrics_language(self):
        self.assertEqual(VC.lyrics_language(["I've been working so hard", "you know it's all for you and me"]), "en")
        self.assertEqual(VC.lyrics_language(["kimi no kokoro wa doko ni", "boku wa naka de nai desu"]), "ja")
        self.assertIsNone(VC.lyrics_language(["la vida es un carnaval", "no hay que llorar"]))   # can't tell: no claim

    def test_verdict(self):
        res = {"lang": "ja", "lines": 10, "duration": 100.0, "skipped": [], "video_check": {"lyrics_lang": "en", "album": {"state": "skipped", "why": "x"}}}
        v = VC.verdict(res, True, "9 of 10 heard")
        self.assertFalse(v["ok"])                                    # passes the floor, but listened in the wrong language
        self.assertEqual(v["fix"], {"lang": "en"})
        res["lang"] = "en"
        v = VC.verdict(res, True, "9 of 10 heard")
        self.assertTrue(v["ok"])
        self.assertIsNone(v["fix"])
        self.assertTrue(any("wasn't looked for" in c["note"] for c in v["checks"]))   # the album map's skip is said
        res["video_check"]["lyrics_end"] = 200.0                       # timestamps past the video: a longer version's copy
        self.assertFalse(VC.verdict(res, True, "")["ok"])
        res["video_check"]["lyrics_end"] = 90.0
        res["skipped"] = [{}] * 5                                     # a third of the lyrics left out: a note, not a stop
        v = VC.verdict(res, True, "")
        self.assertTrue(v["ok"] and any(c["key"] == "left_out" for c in v["checks"]))
        self.assertFalse(VC.verdict(res, False, "3 of 10 lines heard in place")["ok"])   # the floor stops it


class Batches(Base):
    def batch(self, title, **fake):
        MAKE.clear()
        jid = _orig_new_job({"yt": "bbbbbbbbbbb", "lrc_id": 7, "lyrics_source": "lrclib", "artist": "Batch Band", "title": title,
                             "fake": "conf", **fake, "auto_publish": {"by": {"kind": "agent", "name": "batcher"}, "at": time.time()}},
                            {"kind": "agent", "name": "batcher"})
        return jid

    def settle(self, jid):
        """Run it (and the one automatic remake, if the check asks for it) until it's published or held."""
        done = lambda: A.load(jid).get("published") or A.load(jid).get("auto_held")
        for _ in range(3):
            A.schedule_once()
            if wait_for(done, 15):
                break
        self.assertTrue(done(), A.load(jid).get("status"))
        return A.load(jid)

    def test_a_batch_song_that_passes_is_published_by_itself(self):
        job = self.settle(self.batch("Passes", fake_lines=10, fake_sure=9))
        self.assertTrue(job.get("published"))
        self.assertEqual(job["published_by"]["by"], "batcher")
        self.assertEqual(job["quick"]["for"], "batch")

    def test_below_the_floor_waits_with_its_reason(self):
        job = self.settle(self.batch("Held", fake_lines=10, fake_sure=3))
        self.assertFalse(job.get("published"))
        self.assertIn("3 of 10", job["auto_held"])
        self.assertNotIn("auto_retry", job)                           # a low score alone is never remade by itself

    def test_wrong_language_is_made_again_once_in_the_lyrics_language(self):
        job = self.settle(self.batch("Listened Wrong", fake_lines=10, fake_sure=9, fake_written="en", fake_listen_once="ja"))
        self.assertEqual(job["auto_retry"]["fix"], {"lang": "en"})
        self.assertEqual(job["lang"], "en")
        self.assertTrue(job.get("published"), job.get("auto_held"))   # the remake listened in English and passed
        v = A.video_check(job)
        self.assertTrue(v["ok"] and v["retried"])

    def test_a_wrong_language_that_stays_wrong_is_held_not_remade_twice(self):
        job = self.settle(self.batch("Stays Wrong", fake_lines=10, fake_sure=9, fake_written="ja", fake_listen="en"))
        # fix = listen in Japanese; the stand-in keeps the set language, so it passes on the second make
        self.assertEqual(job["auto_retry"]["fix"], {"lang": "ja"})

    def test_auto_publish_needs_the_publish_scope(self):
        key, _ = A.KEYS.create("Maker", ["read", "make"])
        st, _, b = jcall(A.app, "POST", "/api/jobs", {"yt": "ccccccccccc", "lrc_id": 3, "lyrics_source": "lrclib", "artist": "A",
                                                     "title": "B", "auto_publish": True}, {"X-Studio-Agent-Key": key})
        self.assertEqual(st, 403, b)
        self.assertIn("publish", str(b))
