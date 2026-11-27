"""Review (reviews.py): lines ks or an agent moved become items a person answers by ear; agents ask about a timing
without changing it; only people answer; an answer can set the line (as an edit, with Undo); items close when their
line changed since; benchmark items are blind. No GPU, no media."""
import unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
from tests.test_agents import api, make_song, new_key
import app as A  # noqa: E402
import reviews  # noqa: E402
from assgen import load_lines  # noqa: E402

KS = {"User-Agent": "ks/2.0"}
ME = {"X-authentik-username": "alex"}


def lines_of(jid):
    return api("GET", f"/api/jobs/{jid}/lines")[2]["lines"]


def move(jid, i, d, headers=None, key=None):
    ls = lines_of(jid)
    ls[i]["words"] = [[w[0] + d, w[1] + d, w[2]] for w in ls[i]["words"]]
    st, _, b = api("POST", f"/api/jobs/{jid}/lines", {"lines": ls}, headers=headers, key=key)
    assert st == 200, b


def items(jid, state="open"):
    return [x for x in api("GET", f"/api/reviews?state={state}")[2]["items"] if x["job"] == jid]


class Review(unittest.TestCase):
    def test_moved_by_ks_then_person_answers(self):
        jid = make_song()
        move(jid, 2, -0.8, headers=KS)                       # "four": 7.0 -> 6.2
        it, = items(jid)
        self.assertEqual((it["kind"], it["by"]["name"], it["line"]), ("edit", "ks", 3))
        self.assertEqual([o["words"][0][0] for o in it["options"]], [7.0, 6.2])   # A before, B after
        self.assertEqual(api("GET", "/api/status")[2]["reviews_open"] >= 1, True)
        # ks (or an agent key) can't answer its own move
        self.assertEqual(api("POST", f"/api/reviews/{it['id']}/answer", {"verdict": "b"}, headers=KS)[0], 403)
        key = new_key("Review agent", None)
        self.assertEqual(api("POST", f"/api/reviews/{it['id']}/answer", {"verdict": "b"}, key=key)[0], 403)
        # the person says the old timing was right: the line goes back, as an edit Undo can take back
        st, _, b = api("POST", f"/api/reviews/{it['id']}/answer", {"verdict": "a", "note": "heard it at 7"}, headers=ME)
        self.assertEqual(st, 200, b)
        self.assertEqual(b["item"]["answer"]["applied"], "set to Before")
        self.assertEqual(load_lines(A.job_path(jid, "words.json"))[2]["words"][0][0], 7.0)
        self.assertEqual(A.load(jid)["last_edit"]["by"], "alex")
        self.assertEqual(items(jid), [])
        st = api("GET", "/api/reviews?state=done")[2]["stats"]
        self.assertEqual(st["ks"]["wrong"], 1)

    def test_small_or_syllable_edits_ask_nothing(self):
        jid = make_song()
        move(jid, 1, 0.1, headers=KS)
        move(jid, 0, 0.3, headers=ME)                        # a person's own edit isn't reviewed
        self.assertEqual(items(jid), [])

    def test_a_block_moved_together_is_one_item(self):
        jid = make_song()
        ls = lines_of(jid)
        for l in ls[1:]:
            l["words"] = [[w[0] - 0.5, w[1] - 0.5, w[2]] for w in l["words"]]
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines", {"lines": ls}, headers=KS)[0], 200)
        it, = items(jid)
        self.assertEqual((it["text"], it["line"]), ("three", 2))
        self.assertIn("2 lines after it", it["what"])

    def test_moved_again_then_put_back(self):
        jid = make_song()
        move(jid, 2, -0.8, headers=KS)
        move(jid, 2, -0.4, headers=KS)                       # 6.2 -> 5.8: same item, B follows, A stays
        it, = items(jid)
        self.assertEqual([o["words"][0][0] for o in it["options"]], [7.0, 5.8])
        move(jid, 2, 1.2, headers=KS)                        # back at 7.0: settled
        self.assertEqual(items(jid), [])
        self.assertEqual(items(jid, "done")[0]["closed"], "put back as it was")

    def test_closes_when_the_line_changed_since(self):
        jid = make_song()
        move(jid, 2, -0.8, headers=KS)
        move(jid, 2, 0.05, headers=ME)                       # the person nudged it in the editor
        self.assertEqual(items(jid), [])
        self.assertEqual(items(jid, "done")[0]["closed"], "the line changed since")

    def test_undo_closes_it(self):
        jid = make_song()
        move(jid, 2, -0.8, headers=KS)
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines/undo", {}, headers=KS)[0], 200)
        self.assertEqual(items(jid), [])

    def test_ask_then_pick_the_suggestion(self):
        jid = make_song()
        st, _, b = api("POST", "/api/reviews", {"job": jid, "line": 2, "shift": -0.6, "question": "sung earlier?"}, headers=KS)
        self.assertEqual(st, 200, b)
        self.assertEqual(load_lines(A.job_path(jid, "words.json"))[1]["words"][0][0], 4.0)   # nothing changed yet
        it, = items(jid)
        self.assertEqual((it["kind"], it["question"], it["options"][1]["words"][0][0]), ("ask", "sung earlier?", 3.4))
        # the same question twice is one item
        api("POST", "/api/reviews", {"job": jid, "line": 2, "shift": -0.6}, headers=KS)
        self.assertEqual(len(items(jid)), 1)
        st, _, b = api("POST", f"/api/reviews/{it['id']}/answer", {"verdict": "b"}, headers=ME)
        self.assertEqual(st, 200, b)
        l = load_lines(A.job_path(jid, "words.json"))[1]
        self.assertEqual((l["words"][0][0], l["weak"]), (3.4, False))   # set, and its ⚠ cleared (a person decided)

    def test_ask_needs_the_notes_scope_and_the_same_words(self):
        jid = make_song()
        key = new_key("Review no notes", ["read", "edit"])
        self.assertEqual(api("POST", "/api/reviews", {"job": jid, "line": 2, "shift": -0.6}, key=key)[0], 403)
        st, _, _ = api("POST", "/api/reviews", {"job": jid, "line": 1, "words": [[0.5, 1.5, "uno"], [1.5, 2.0, "two"]]}, headers=KS)
        self.assertEqual(st, 400)
        self.assertEqual(api("POST", "/api/reviews", {"job": jid, "line": 1, "shift": 0}, headers=KS)[0], 400)

    def test_same_and_neither(self):
        jid = make_song()
        for n in (1, 4):
            api("POST", "/api/reviews", {"job": jid, "line": n, "shift": -0.5}, headers=KS)
        a, b = items(jid)
        api("POST", f"/api/reviews/{a['id']}/answer", {"verdict": "same"}, headers=ME)
        api("POST", f"/api/reviews/{b['id']}/answer", {"verdict": "neither"}, headers=ME)
        ls = load_lines(A.job_path(jid, "words.json"))
        self.assertEqual(ls[0]["words"][0][0], 1.0)
        self.assertTrue(ls[3]["weak"] and "Review" in ls[3]["why"])

    def test_bench_is_blind_until_answered(self):
        jid = make_song()
        st, _, b = api("POST", "/api/reviews", {"job": jid, "text": "four", "start": 7.0, "shift": -0.7, "kind": "bench",
                                                "what": "bench new2"}, headers=KS)
        self.assertEqual(st, 200, b)
        it, = items(jid)
        self.assertTrue(it["blind"])
        self.assertEqual({o["label"] for o in it["options"]}, {"A", "B"})
        self.assertFalse(any("role" in o for o in it["options"]) or "what" in it)
        alt = next(o["key"] for o in reviews.get(it["id"])["options"] if o["role"] == "alt")
        api("POST", f"/api/reviews/{it['id']}/answer", {"verdict": alt, "apply": False}, headers=ME)
        self.assertEqual(load_lines(A.job_path(jid, "words.json"))[2]["words"][0][0], 7.0)   # apply off: as it was
        done = items(jid, "done")[0]
        self.assertTrue(all("role" in o for o in done["options"]))
        self.assertEqual(api("GET", "/api/reviews?state=done")[2]["stats"]["bench new2"]["right"], 1)

    def test_answer_refused_when_line_changed(self):
        jid = make_song()
        api("POST", "/api/reviews", {"job": jid, "line": 3, "shift": -0.6}, headers=KS)
        it, = items(jid)
        move(jid, 2, 0.5, headers=ME)
        st, _, _ = api("POST", f"/api/reviews/{it['id']}/answer", {"verdict": "b"}, headers=ME)
        self.assertIn(st, (409,))


if __name__ == "__main__":
    unittest.main()
