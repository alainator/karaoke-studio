"""deploy/agent-bench.py: the sandbox copy keeps nothing of the person's fix (every file compared), the source is never
changed, and the scoring counts on made-up lines."""
import importlib.util, json, os, shutil, tempfile, unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("agent_bench", os.path.join(HERE, "deploy", "agent-bench.py"))
ab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ab)

PERSON = "Pat Example"
NOTE = "looks fine: held vowel over the bridge"


def line(t, words, **kw):
    return dict({"words": [[round(t + k * 0.5, 3), round(t + k * 0.5 + 0.4, 3), w] for k, w in enumerate(words)]}, **kw)


def auto_lines():
    return [line(10.0, ["aa", "bb"], heard="aa bb"), line(14.0, ["cc", "dd"], weak=True, why="sung earlier?"),
            line(18.0, ["ee", "ff"]), line(19.0, ["oo"], backing=True), line(22.0, ["gg", "hh"])]


def person_lines():
    p = auto_lines()
    p[1] = line(12.85, ["cc", "dd"], note={"by": PERSON, "kind": "person", "text": NOTE, "at": 1})   # moved, mark gone
    p[2]["ok"] = True
    p[3] = line(20.4, ["oo"], backing=True)
    p[4] = line(22.0, ["gg", "hx"])                                                                      # retyped
    return p


def write(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def make_source(root, jid="20260101-000000-some-song"):
    src = os.path.join(root, "jobs", jid)
    auto, person = {"version": 2, "lines": auto_lines()}, {"version": 2, "lines": person_lines()}
    write(os.path.join(src, "words.orig.json"), auto)
    write(os.path.join(src, "words.json"), person)
    for f in ("words.before-check.json", "words.before-remake.json", "mystery.json"):
        write(os.path.join(src, f), person)                       # every place a person's version could sit
    write(os.path.join(src, "history", "20260101-x.json"), {"by": PERSON, "kind": "person", "what": "move", "lines": person["lines"]})
    write(os.path.join(src, "history", "undone", "20260101-y.json"), {"by": PERSON, "kind": "person", "lines": person["lines"]})
    write(os.path.join(src, "wordscore.ref.json"), {"lines": person["lines"]})
    write(os.path.join(src, "heard.json"), {"lang": "en", "words": [[10.1, 10.4, "aa"]]})
    write(os.path.join(src, "result.json"), {"lang": "en", "duration": 60.0, "confidence": {"lines": 4, "sure": 4}})
    write(os.path.join(src, "job.json"), {
        "id": jid, "artist": "A", "title": "T", "status": "done", "offset": 0.3, "edited": 5.0, "reviewed": 6.0,
        "last_edit": {"by": PERSON, "kind": "person", "what": "move line 2"}, "made_by": PERSON, "published": 4.0,
        "published_by": PERSON, "quick": {"reviewed_by": PERSON}, "pre_remake": {"edited": 1}, "remake_note": NOTE,
        "ai_romaji": True, "album_meta": {"by": PERSON, "album": "X"}, "log": ["made"],
        "result": {"lang": "en", "duration": 60.0, "confidence": {"lines": 4, "sure": 4}}})
    for f in ("lyrics.ass", "lyrics.both.ass"):
        with open(os.path.join(src, f), "w") as fh:
            fh.write(f"Dialogue: 0,0:00:13.15,0:00:14.00,{NOTE}\n")   # subtitles drawn from the person's timing
    with open(os.path.join(src, "vocals.m4a"), "wb") as fh:
        fh.write(b"\0" * 64)
    with open(os.path.join(src, "pipeline.log"), "w") as fh:
        fh.write("aligned 5 lines\n")
    os.makedirs(os.path.join(src, "videos"), exist_ok=True)
    return src


def snapshot(d):
    out = {}
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            out[os.path.relpath(p, d)] = (open(p, "rb").read(), os.stat(p).st_mtime_ns)
    return out


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="bench-test-")
        self.src = make_source(self.root)
        self.dst = os.path.join(self.root, "out", "jobs", os.path.basename(self.src))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_nothing_of_the_person_remains_in_any_file(self):
        before = snapshot(self.src)
        truth = ab.reset_copy(self.src, self.dst)
        self.assertEqual(snapshot(self.src), before, "the source job must not change")
        person_t = ab._triples(person_lines()) - ab._triples(auto_lines())
        self.assertTrue(person_t)
        files = snapshot(self.dst)
        self.assertEqual(set(files) - set(ab.COPY) - {"words.json", "job.json"}, set())
        for rel, (raw, _) in files.items():
            text = raw.decode("utf-8", "replace")
            self.assertNotIn(PERSON, text, rel)
            self.assertNotIn(NOTE, text, rel)
            self.assertNotIn("0:00:13.15", text, rel)
            if rel.endswith(".json"):
                self.assertFalse(ab._all_triples(json.loads(text), set()) & person_t, rel)
        self.assertFalse(os.path.exists(os.path.join(self.dst, "history")))
        self.assertFalse(any(f.startswith("lyrics") for f in files))
        words = json.load(open(os.path.join(self.dst, "words.json")))
        self.assertEqual([l["words"] for l in words["lines"]], [l["words"] for l in auto_lines()])
        self.assertTrue(words["lines"][1]["weak"])                       # the Studio's own marks stay
        self.assertFalse(any("note" in l or "ok" in l for l in words["lines"]))
        self.assertEqual(json.load(open(os.path.join(self.dst, "words.orig.json"))), words)
        job = json.load(open(os.path.join(self.dst, "job.json")))
        self.assertEqual(job["offset"], 0.0)
        for k in ("last_edit", "made_by", "published_by", "quick", "pre_remake", "remake_note", "ai_romaji"):
            self.assertNotIn(k, job)
        self.assertFalse(job["edited"] or job["reviewed"] or job["published"])
        self.assertNotIn("confidence", job["result"])
        self.assertNotIn("confidence", json.load(open(os.path.join(self.dst, "result.json"))))
        self.assertEqual(ab.leaks(self.dst, truth), [])
        # the truth (kept outside the sandbox) is the person's version, offset included
        self.assertEqual(truth["person"]["offset"], 0.3)
        self.assertEqual(truth["person"]["lines"][1]["words"][0][0], 12.85)

    def test_leak_check_finds_a_planted_fix(self):
        truth = ab.reset_copy(self.src, self.dst)
        write(os.path.join(self.dst, "words.json"), {"version": 2, "lines": person_lines()})
        with open(os.path.join(self.dst, "pipeline.log"), "a") as fh:
            fh.write(f"note: {NOTE}\n")
        found = " ".join(ab.leaks(self.dst, truth))
        self.assertIn("words.json: holds", found)
        self.assertIn("pipeline.log: holds a person's name or note", found)

    def test_copies_are_files_not_links(self):
        ab.reset_copy(self.src, self.dst)
        a, b = os.stat(os.path.join(self.src, "vocals.m4a")), os.stat(os.path.join(self.dst, "vocals.m4a"))
        self.assertNotEqual(a.st_ino, b.st_ino)
        self.assertEqual(int(a.st_mtime), int(b.st_mtime))      # pitch.f0.npz stays valid (stamped with vocals')
        with open(os.path.join(self.dst, "vocals.m4a"), "ab") as fh:
            fh.write(b"x")
        self.assertEqual(os.path.getsize(os.path.join(self.src, "vocals.m4a")), 64)

    def test_pick_skips_agent_edits_and_remakes(self):
        jobs = os.path.join(self.root, "jobs")
        ok = os.path.basename(self.src)
        shutil.rmtree(os.path.join(self.src, "videos"))
        a = make_source(self.root, "20260101-000001-agent-song")
        write(os.path.join(a, "history", "z.json"), {"by": "Claude", "kind": "agent"})
        shutil.rmtree(os.path.join(a, "videos"))
        r = make_source(self.root, "20260101-000002-remade-song")
        shutil.rmtree(os.path.join(r, "videos"))
        j = json.load(open(os.path.join(r, "job.json"))); j["remade"] = 1; write(os.path.join(r, "job.json"), j)
        for d in (self.src, a, r):
            j = json.load(open(os.path.join(d, "job.json"))); j.pop("pre_remake"); write(os.path.join(d, "job.json"), j)
            os.remove(os.path.join(d, "words.before-remake.json"))   # (planted for the reset test: a remake's trace)
        chosen, info = ab.pick(jobs, 5)
        self.assertEqual([c["id"] for c in chosen], [ok])
        self.assertEqual(chosen[0]["moved"], 4)   # the 4 lead lines: the person's 0.3 s offset moves them all
        self.assertEqual(info["candidates"], 1)


def T(lines, offset=0.0):
    return {"lines": lines, "offset": offset}


class ScoreTests(unittest.TestCase):
    def truth(self, auto, person, poff=0.0):
        return {"id": "s", "lang": "en", "auto": T(auto), "person": T(person, poff)}

    def test_counts(self):
        auto = [line(t, ["w"]) for t in (10, 20, 30, 40, 50, 60)]
        person = [line(t, ["w"]) for t in (9.0, 18.5, 28.0, 40, 50, 60.15)]
        now = [line(t, ["w"]) for t in (9.1, 19.6, 30.5, 40, 51.0, 60)]
        r = ab.score_song(self.truth(auto, person), {"lines": now}, 0.0)
        self.assertTrue(r["scored"])
        self.assertEqual((r["moved"], r["fixed"], r["improved"], r["worse"], r["untouched"], r["changed"]), (3, 1, 1, 1, 0, 0))
        self.assertEqual((r["kept"], r["kept_broken"], r["small"]), (2, 1, 1))
        self.assertEqual(r["moved_median_before"], 1.5)

    def test_improved_untouched_and_words(self):
        auto = [line(10, ["a", "b"]), line(20, ["c"]), line(30, ["d"])]
        person = [line(8, ["a", "b"]), line(18, ["c"]), line(30, ["x"])]
        now = [line(9.0, ["a", "b"]), line(20, ["c"]), line(30, ["x"])]
        r = ab.score_song(self.truth(auto, person), {"lines": now}, 0.0)
        self.assertEqual((r["fixed"], r["improved"], r["untouched"]), (0, 1, 1))
        self.assertEqual((r["words_changed"], r["words_match"], r["person_words"], r["person_words_right"]), (1, 1, 1, 1))

    def test_the_line_before_end(self):
        auto = [line(10, ["a", "b"]), line(11.5, ["c"]), line(20, ["d", "e"]), line(21.2, ["f"])]
        person = [line(10, ["a"]), line(10.6, ["c"]), line(20, ["d", "e"]), line(20.7, ["f"])]   # 1: end cut; 3: not
        now = [line(10, ["a"]), line(10.6, ["c"]), line(20, ["d"]), line(20.7, ["f"])]           # 3's line before cut
        r = ab.score_song(self.truth(auto, person), {"lines": now}, 0.0)
        self.assertEqual((r["moved"], r["fixed"]), (2, 2))
        self.assertEqual((r["prev_end"], r["prev_end_cut"], r["prev_end_fixed"], r["prev_end_broken"]), (2, 1, 1, 1))

    def test_swapped_neighbours_are_matched_by_their_words(self):
        auto = [line(10, ["a"]), line(16.2, ["b"]), line(16.1, ["c", "d"]), line(20, ["e"])]   # out of order
        person = [line(10, ["a"]), line(15.5, ["b"]), line(16.1, ["c", "d"]), line(20, ["e"])]
        now = sorted(auto, key=lambda l: l["words"][0][0])                                     # saved: in time order
        r = ab.score_song(self.truth(auto, person), {"lines": now}, 0.0)
        self.assertEqual((r["moved"], r["untouched"], r["kept"], r["kept_broken"], r["words_changed"]), (1, 1, 3, 0, 0))

    def test_offsets_are_video_time(self):
        auto = [line(10, ["a"]), line(20, ["b"])]
        person = [line(9.5, ["a"]), line(19.5, ["b"])]      # the person moved the whole song by its offset
        r = ab.score_song(self.truth(auto, person, 0.5 - 1.0), {"lines": auto}, -1.0)   # agent: offset -1 s
        self.assertEqual((r["moved"], r["fixed"]), (2, 2))

    def test_deleted_and_added_lines_are_reported_not_skipped(self):
        auto = [line(t, [w]) for t, w in ((10, "a"), (20, "b"), (30, "c"), (40, "d"))]
        person = [line(t, [w]) for t, w in ((10, "a"), (18.5, "b"), (30, "c"), (40, "d"))]
        now = [line(10, ["a"]), line(30, ["c"]), line(35, ["new", "words"]), line(40, ["d"])]   # b deleted, a line added
        r = ab.score_song(self.truth(auto, person), {"lines": now}, 0.0)
        self.assertTrue(r["scored"])
        self.assertEqual((r["added"], r["deleted"]), (1, 1))
        self.assertEqual((r["moved"], r["moved_deleted"], r["fixed"]), (1, 1, 0))   # still counted: deleting isn't fixing
        self.assertEqual((r["kept"], r["kept_broken"], r["kept_deleted"]), (3, 0, 0))
        self.assertEqual(r["moved_lines"][0]["outcome"], "deleted")

    def test_split_and_joined_lines(self):
        auto = [line(10, ["a", "b", "c", "d"]), line(20, ["e", "f"]), line(22, ["g", "h"]), line(30, ["i"])]
        person = [line(9.0, ["a", "b", "c", "d"]), line(20, ["e", "f"]), line(22, ["g", "h"]), line(30, ["i"])]
        now = [line(9.0, ["a", "b"]), line(11.0, ["c", "d"]),               # split: the part with its first word
               line(19.5, ["e", "f", "g", "h"]), line(30, ["i"])]           # joined: the first of the two
        r = ab.score_song(self.truth(auto, person), {"lines": now}, 0.0)
        self.assertEqual((r["moved"], r["fixed"]), (1, 1))
        self.assertEqual((r["added"], r["deleted"]), (1, 1))
        self.assertEqual((r["kept"], r["kept_broken"], r["kept_earlier"], r["kept_deleted"]), (3, 1, 1, 1))
        self.assertEqual((r["words_changed"]), 2)

    def test_retyped_line_is_still_matched(self):
        auto = [line(10, ["a"]), line(20, ["hello", "there"]), line(30, ["c"])]
        person = [line(10, ["a"]), line(19, ["hello", "dear"]), line(30, ["c"])]
        now = [line(10, ["a"]), line(19.1, ["something", "else"]), line(30, ["c"])]   # nothing alike: the gap, in order
        r = ab.score_song(self.truth(auto, person), {"lines": now}, 0.0)
        self.assertEqual((r["moved"], r["fixed"], r["added"], r["deleted"]), (1, 1, 0, 0))

    def test_kept_lines_moved_off_by_direction_size_and_repeat_of_a_fix(self):
        chorus = ["oh", "la", "la"]
        auto = [line(10, chorus), line(20, ["x"]), line(30, ["y"]), line(40, chorus), line(50, ["z"]), line(60, chorus)]
        person = [line(9.0, chorus)] + auto[1:]                             # the person fixed the first chorus only
        now = [line(9.0, chorus), line(20, ["x"]), line(32.5, ["y"]), line(38.9, chorus), line(49.5, ["z"]),
               line(57.0, chorus)]
        r = ab.score_song(self.truth(auto, person), {"lines": now}, 0.0)
        self.assertEqual((r["kept"], r["kept_broken"], r["kept_earlier"], r["kept_later"]), (5, 4, 3, 1))
        self.assertEqual((r["kept_0.3-1 s"], r["kept_1-2 s"], r["kept_>2 s"]), (1, 1, 2))
        self.assertEqual(r["kept_repeat_fix"], 1)                           # 40: -1.1 like the person's -1.0; 60: -3 is too far
        rows = {x["auto_line"]: x for x in r["kept_lines"]}
        self.assertTrue(rows[4]["repeat_of_fix"]); self.assertFalse(rows[6]["repeat_of_fix"])
        self.assertEqual((rows[3]["direction"], rows[3]["delta"]), ("later", 2.5))
        self.assertEqual(rows[5]["neighbour_moved"], False)
        self.assertEqual(r["kept_lines"][0]["line"], 3)

    def test_deleting_or_adding_a_chorus_copy_keeps_the_others_paired_by_time(self):
        ch = ["oh", "la"]
        auto = [line(10, ["a"]), line(14, ch), line(17, ch), line(30, ["d"])]
        now = [line(10, ["a"]), line(17, ch), line(30, ["d"])]               # the agent deleted the FIRST copy
        r = ab.score_song(self.truth(auto, auto), {"lines": now}, 0.0)
        self.assertEqual((r["kept"], r["kept_broken"], r["kept_deleted"], r["deleted"]), (4, 0, 1, 1))
        self.assertEqual(r["kept_lines"][0]["auto_line"], 2)
        now = [line(10, ["a"]), line(12, ch), line(14, ch), line(17, ch), line(30, ["d"])]   # added a copy before
        r = ab.score_song(self.truth(auto, auto), {"lines": now}, 0.0)
        self.assertEqual((r["kept_broken"], r["added"], r["deleted"]), (0, 1, 0))

    def test_neighbour_of_a_person_move(self):
        auto = [line(10, ["a"]), line(20, ["b"]), line(30, ["c"])]
        person = [line(10, ["a"]), line(19, ["b"]), line(30, ["c"])]
        now = [line(9.4, ["a"]), line(19, ["b"]), line(30, ["c"])]
        r = ab.score_song(self.truth(auto, person), {"lines": now}, 0.0)
        self.assertEqual(r["kept_lines"][0]["neighbour_moved"], True)
        self.assertEqual(r["kept_lines"][0]["size"], "0.3-1 s")

    def test_backing_lines_are_not_matched(self):
        auto = [line(10, ["a"]), line(12, ["b"], backing=True), line(20, ["c"])]
        person = [line(10, ["a"]), line(20, ["c"])]
        r = ab.score_song(self.truth(auto, person), {"lines": auto}, 0.0)
        self.assertTrue(r["scored"])
        self.assertEqual((r["kept"], r["moved"]), (2, 0))


class StopTests(unittest.TestCase):
    """stop removes only OUT's own container: the one start recorded, or any labelled with OUT; never port 5680 by
    default, never another OUT's container on the same port."""
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="bench-stop-")
        self.real = ab.subprocess.run
        self.labelled = {}

        def fake(cmd, **kw):
            import types
            if cmd[:3] == ["docker", "ps", "-a"]:
                want = cmd[cmd.index("--filter") + 1].split("=", 1)[1]
                names = [n for n, o in self.labelled.items() if f"{ab.OUT_LABEL}={o}" == want]
                return types.SimpleNamespace(returncode=0, stdout="\n".join(names), stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        ab.subprocess.run = fake

    def tearDown(self):
        ab.subprocess.run = self.real
        shutil.rmtree(self.out, ignore_errors=True)

    def test_nothing_recorded_removes_nothing(self):
        self.labelled = {"ks-agent-bench-5680": "/somewhere/else"}
        self.assertEqual(ab.stop_targets(self.out), [])

    def test_the_recorded_container(self):
        self.labelled = {"ks-agent-bench-5681": self.out, "ks-agent-bench-5680": "/somewhere/else"}
        write(os.path.join(self.out, ab.SANDBOX), {"container": "ks-agent-bench-5681", "port": 5681})
        self.assertEqual(ab.stop_targets(self.out), ["ks-agent-bench-5681"])

    def test_a_port_of_another_sandbox_is_left_alone(self):
        self.labelled = {"ks-agent-bench-5680": "/somewhere/else"}
        self.assertEqual(ab.stop_targets(self.out, 5680), [])
        self.labelled = {"ks-agent-bench-5682": self.out}
        self.assertEqual(ab.stop_targets(self.out), ["ks-agent-bench-5682"])   # labelled with OUT, nothing recorded


if __name__ == "__main__":
    unittest.main()
