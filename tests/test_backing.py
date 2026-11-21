"""\"Keep backing vocals\" (a song's switch, pipeline 3b): the route saves it on the job (the next remake uses it), only for a
finished song; the pipeline step itself needs the GPU models (checked by ear, see config.BACKING_MODEL).

    python -m unittest tests.test_backing
"""
import json, os, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import app as A  # noqa: E402
import config as C  # noqa: E402
from tests._asgi import jcall  # noqa: E402


def make(jid, status="done"):
    os.makedirs(os.path.join(A.JOBS, jid), exist_ok=True)
    A.save(jid, {"id": jid, "artist": "A", "title": "T", "status": status, "created": 1, "log": []})


class BackingTests(unittest.TestCase):
    def test_the_switch_is_saved_for_the_next_remake(self):
        make("20261121-000000-a-t")
        st, _, b = jcall(A.app, "POST", "/api/jobs/20261121-000000-a-t/backing", {"on": True})
        self.assertEqual((st, b), (200, {"keep_backing": True, "swaps": []}))
        self.assertTrue(A.load("20261121-000000-a-t")["keep_backing"])
        self.assertTrue(jcall(A.app, "GET", "/api/jobs/20261121-000000-a-t")[2]["keep_backing"])   # the page sees it
        st, _, b = jcall(A.app, "POST", "/api/jobs/20261121-000000-a-t/backing", {"on": False})
        self.assertFalse(A.load("20261121-000000-a-t")["keep_backing"])
        self.assertEqual(jcall(A.app, "POST", "/api/jobs/20261121-000000-a-t/backing", {"on": True}, {"Origin": "https://evil.example"})[0], 403)

    def test_not_while_it_is_being_made(self):
        make("20261121-000001-a-t", status="running")
        self.assertEqual(jcall(A.app, "POST", "/api/jobs/20261121-000001-a-t/backing", {"on": True})[0], 409)

    def test_the_model(self):
        self.assertIn("karaoke", C.BACKING_MODEL)
        src = open(os.path.join(_env.REPO, "pipeline.py")).read()
        self.assertIn('"-i", inst_out', src)          # the render uses the music with the backing when it's on
        self.assertIn('"-i", voc_out', src)           # the guide vocals: the lead only


if __name__ == "__main__":
    unittest.main()


class FaintBackingRowTests(unittest.TestCase):
    """With the backing vocals kept in the music, their lyric row is drawn see-through (the music sings it)."""
    LINES = {"version": 2, "lines": [{"words": [[1.0, 1.5, "la"], [1.5, 2.0, "la"]]},
                                     {"words": [[2.5, 3.0, "ooh"]], "backing": True}]}

    def back_events(self, faint):
        import assgen
        text = assgen.make_ass(self.LINES, embed_fonts=False, backing_faint=faint)
        return [l for l in text.splitlines() if l.startswith("Dialogue:") and ",Back," in l]

    def test_see_through_only_when_kept(self):
        self.assertTrue(self.back_events(True))
        self.assertTrue(all("\\alpha&H80&" in l for l in self.back_events(True)))
        self.assertFalse(any("\\alpha&H80&" in l for l in self.back_events(False)))


class SwapTests(unittest.TestCase):
    """Voice swaps: set in the song page's (trimmed) video time, kept in the original video's time."""

    def setUp(self):
        self.jid = "20261121-000002-a-t"
        make(self.jid)
        job = A.load(self.jid)
        job.update(trim={"start": 10.0, "end": None}, result={"duration": 300.0, "backing": True, "voice_swaps": []})
        A.save(self.jid, job)

    def post(self, body):
        return jcall(A.app, "POST", f"/api/jobs/{self.jid}/backing", body)

    def test_page_time_in_original_time_kept(self):
        st, _, b = self.post({"on": True, "swaps": [[261.0, 285.5]]})
        self.assertEqual(st, 200, b)
        self.assertEqual(A.load(self.jid)["voice_swaps"], [[271.0, 295.5]])        # +10 s of trim
        view = jcall(A.app, "GET", f"/api/jobs/{self.jid}")[2]
        self.assertEqual((view["voice_swaps"], view["voice_swaps_now"], view["keep_backing"]), ([[261.0, 285.5]], [], True))
        self.assertEqual(self.post({"swaps": [[400, 500]]})[0], 400)                # wholly past the song's end
        self.post({"swaps": []})
        self.assertEqual(A.load(self.jid)["voice_swaps"], [])

    def test_refusals(self):
        for bad in ({"swaps": [[5, 5.1]]}, {"swaps": [[1, 20], [10, 30]]}, {"swaps": [["a", 2]]}, {"swaps": "x"},
                    {"swaps": [[1, 2]] * 21}, {}):
            self.assertEqual(self.post(bad)[0], 400, bad)
