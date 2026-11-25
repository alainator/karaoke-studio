"""Syllable timing (syllables.py): a word sung in pieces ("flim|bar|dies") is kept whole in words.json with its pieces
beside it ("syl"), shown joined and lit piece by piece in the .ass, typed with | in the editor and ks, carried
through every reader that doesn't know about it, and split automatically where the melody clearly holds or stretches
a syllable over several notes. The words here are made up."""
import asyncio, importlib.machinery, importlib.util, json, os, re, shutil, subprocess, tempfile, time, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import numpy as np  # noqa: E402

import syllables as Y  # noqa: E402
from assgen import load_lines, make_ass, normalize, save_lines  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_loader = importlib.machinery.SourceFileLoader("ks_tool_syl", os.path.join(HERE, "tools", "ks"))
_spec = importlib.util.spec_from_loader("ks_tool_syl", _loader)
ks = importlib.util.module_from_spec(_spec)
_loader.exec_module(ks)

PIECES = [[10.5, 10.8, "flim"], [10.8, 11.1, "bar"], [11.1, 13.0, "dies"]]


def line(syl=True, **kw):
    return dict({"words": [[10.0, 10.4, "these"], [10.5, 13.0, "flimbardies"], [13.6, 14.0, "go"]],
                 "syl": [None, [list(p) for p in PIECES], None] if syl else None}, **kw)


def dialogue(ass, style="Main"):
    return [l.split(",,", 1)[1].split("}", 1)[1] for l in ass.splitlines() if l.startswith("Dialogue: 0") and f",{style}," in l]


def kparts(text):
    """[(tag, centiseconds, text)] of one dialogue's karaoke text."""
    return [(m.group(1), int(m.group(2)), m.group(3)) for m in re.finditer(r"\{[^}]*?\\(kf|k)(\d+)\}([^{]*)", text)]


NOSHOW = {"cards": False, "countdown": False}


class Data(unittest.TestCase):
    def test_round_trip(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "words.json")
        save_lines(p, [line(), line(syl=False)])
        raw = json.load(open(p))
        self.assertEqual(raw["lines"][0]["syl"], [None, PIECES, None])
        self.assertNotIn("syl", raw["lines"][1])          # no key at all for lines without pieces
        back = load_lines(p)
        self.assertEqual(back[0]["syl"][1], PIECES)
        self.assertEqual([w[2] for w in back[0]["words"]], ["these", "flimbardies", "go"])   # words stay whole
        self.assertIsNone(back[1]["syl"])

    def test_pieces_follow_a_moved_or_stretched_word(self):
        moved = Y.fit([12.5, 15.0, "flimbardies"], PIECES)
        self.assertEqual(moved, [[12.5, 12.8, "flim"], [12.8, 13.1, "bar"], [13.1, 15.0, "dies"]])
        stretched = Y.fit([10.5, 15.5, "flimbardies"], PIECES)   # twice as long
        self.assertEqual(stretched, [[10.5, 11.1, "flim"], [11.1, 11.7, "bar"], [11.7, 15.5, "dies"]])

    def test_pieces_go_when_the_word_changes(self):
        self.assertIsNone(Y.fit([10.5, 13.0, "flombardies"], PIECES))
        self.assertIsNone(Y.fit([10.5, 13.0, "flimbardies"], PIECES[:1]))
        l = normalize([dict(line(), words=line()["words"][:2])])[0]   # a word gone: the list no longer lines up
        self.assertIsNone(l["syl"])

    def test_a_trim_moves_them_with_the_words(self):
        import trimtools
        d = tempfile.mkdtemp()
        p = os.path.join(d, "words.json")
        save_lines(p, [line()])
        trimtools._shift_lines(p, -2.0, 300.0)
        self.assertEqual(load_lines(p)[0]["syl"][1][2], [9.1, 11.0, "dies"])

    def test_parse_and_mark(self):
        self.assertEqual(Y.parse("flim|bar|dies"), ("flimbardies", ["flim", "bar", "dies"]))
        self.assertEqual(Y.parse("|go|"), ("go", None))
        self.assertEqual(Y.line_text(normalize([line()])[0]), "these flim|bar|dies go")

    def test_set_piece_moves_the_shared_boundary(self):
        l = normalize([line()])[0]
        Y.set_piece(l, 1, 1, start=10.9)
        self.assertEqual(l["syl"][1][:2], [[10.5, 10.9, "flim"], [10.9, 11.1, "bar"]])
        Y.set_piece(l, 1, 2, end=13.5)                  # a held last syllable stretches its word
        self.assertEqual(l["words"][1], [10.5, 13.5, "flimbardies"])
        with self.assertRaises(ValueError):
            Y.set_piece(l, 1, 2, end=13.7)              # past the next word's start
        with self.assertRaises(ValueError):
            Y.set_piece(l, 0, 0, start=1.0)             # not split

    def test_set_split(self):
        l = normalize([line(syl=False)])[0]
        Y.set_split(l, 1, ["flimbar", "dies"])
        self.assertEqual(l["syl"][1][0][2], "flimbar")
        self.assertEqual(l["syl"][1][-1][1], 13.0)
        with self.assertRaises(ValueError):
            Y.set_split(l, 1, ["flam", "dies"])
        Y.set_split(l, 1, None)
        self.assertIsNone(l["syl"])


class Display(unittest.TestCase):
    def test_pieces_joined_each_with_its_time(self):
        text = dialogue(make_ass([line()], embed_fonts=False, style=NOSHOW))[0]
        self.assertIn("flim{", text)
        self.assertNotIn("flim ", text)
        self.assertEqual(re.sub(r"\{[^}]*\}", "", text), "these flimbardies go")   # on screen: the word, joined
        parts = kparts(text)
        word = parts[2:6]    # (after the lead-in wait and "these ")
        # flim 0.3 s, bar 0.3 s, dies held 1.9 s, then the 0.6 s before "go" is a wait of its own
        self.assertEqual([(p[1], p[2]) for p in word], [(30, "flim"), (30, "bar"), (190, "dies "), (60, "")])
        self.assertEqual(sum(p[1] for p in word), 310)   # = the word's time unsplit (13.6 - 10.5)

    def test_sweep_off_held_piece_still_fills(self):
        parts = kparts(dialogue(make_ass([line()], embed_fonts=False, style=dict(NOSHOW, sweep=False)))[0])
        tags = {p[2]: p[0] for p in parts if p[2]}
        self.assertEqual(tags["flim"], "k")      # short pieces light at once, like every word with sweep off
        self.assertEqual(tags["dies "], "kf")    # the held one fills while it's held
        self.assertEqual(tags["these "], "k")
        on = {p[2]: p[0] for p in kparts(dialogue(make_ass([line()], embed_fonts=False, style=NOSHOW))[0]) if p[2]}
        self.assertEqual(set(on.values()), {"kf"})   # sweep on (the default): everything sweeps, as before

    def test_unsplit_lines_unchanged(self):
        a = make_ass([line(syl=False)], embed_fonts=False, style=NOSHOW)
        b = make_ass([{k: v for k, v in line().items() if k != "syl"}], embed_fonts=False, style=NOSHOW)
        self.assertEqual(a, b)
        self.assertIn("{\\kf310}flimbardies ", a)   # an unsplit word sweeps until the next one starts

    def test_rows_stay_in_step(self):
        """both / both_original: the original row is per word; the split word's parts add up to its word."""
        l = line(orig="思い出の")
        for mode in ("both", "both_original"):
            ass = make_ass([l], embed_fonts=False, mode=mode, style=NOSHOW)
            main, orig = kparts(dialogue(ass, "Main")[0]), kparts(dialogue(ass, "Orig")[0])
            self.assertEqual(sum(p[1] for p in main), sum(p[1] for p in orig), mode)
            self.assertEqual(len(orig), 4, mode)     # lead-in + one per word: the original row isn't split

    def test_no_line_break_inside_a_word(self):
        words = [[i * 1.0, i * 1.0 + 0.9, f"wordnumber{i}"] for i in range(14)]
        syl = [None] * 14
        syl[7] = Y.even(7.0, 7.9, ["word", "num", "ber7"])
        text = dialogue(make_ass([{"words": words, "syl": syl}], embed_fonts=False, style=NOSHOW))[0]
        self.assertIn("\\N", text)
        self.assertNotRegex(text, r"(word|num)\{[^}]*\}\\N|\\N\{[^}]*\}(num|ber7)")

    def test_backing_line_pieces(self):
        b = dict(line(), backing=True)
        text = [l for l in make_ass([line(syl=False), b], embed_fonts=False, style=NOSHOW).splitlines() if ",Back," in l][0]
        self.assertIn("flim{", text)


class Consumers(unittest.TestCase):
    def test_words_doc_keeps_whole_words_and_adds_pieces_in_video_time(self):
        import catalog
        d = tempfile.mkdtemp()
        save_lines(os.path.join(d, "words.json"), [line(), line(syl=False)])
        doc = catalog.words_doc(d, {"id": "x", "offset": 0.5}, "Base")
        l0 = doc["lines"][0]
        self.assertEqual([w[2] for w in l0["words"]], ["these", "flimbardies", "go"])
        self.assertEqual(l0["syl"][1][0], [11.0, 11.3, "flim"])
        self.assertNotIn("syl", doc["lines"][1])

    def test_ultrastar_notes_per_syllable(self):
        import ultrastar as U
        notes = [[10.5, 10.8, 60.0], [10.8, 11.1, 62.0], [11.1, 13.0, 64.0]]
        out = U.notes_for(line()["words"], notes, line()["syl"])
        texts = [n[3] for n in out]
        self.assertEqual(texts[1:4], ["flim", "bar", "dies "])
        plain = [n[3] for n in U.notes_for(line()["words"], notes)]
        self.assertEqual(plain[1:4], ["flimbardies", "~", "~ "])

    def test_melody_and_scoring_see_whole_words(self):
        import pitch
        d = tempfile.mkdtemp()
        save_lines(os.path.join(d, "words.json"), [line()])
        self.assertEqual(pitch.word_spans(d, {"offset": 0.0}), [(10.0, 10.4), (10.5, 13.0), (13.6, 14.0)])

    def test_lyric_map_unaffected(self):
        from lyricmap import line_maps
        l = normalize([line(orig="思い出の")])[0]
        self.assertEqual(len(line_maps(l["orig"], tuple(w[2] for w in l["words"]), True)[0]), 3)


# ---- automatic splits from a melody ----
HOP = 0.02


def melody(notes, n=1000):
    """f0 / periodicity per 20 ms frame with these notes [(start, end, midi)] sung."""
    f0, per = np.zeros(n), np.zeros(n)
    for a, b, m in notes:
        k0, k1 = int(round(a / HOP)), int(round(b / HOP))
        f0[k0:k1] = 440.0 * 2 ** ((m - 69) / 12)
        per[k0:k1] = 0.9
    return f0, per


def notes_of(words, sung):
    import pitch
    f0, per = melody(sung)
    return pitch.notes(f0, per, [(w[0], w[1]) for w in words])[0]


class Auto(unittest.TestCase):
    def split(self, words, sung, lang="en", **kw):
        lines = [dict({"words": [list(w) for w in words]}, **kw)]
        n = Y.auto(lines, notes_of(words, sung), 0.0, lang)
        return n, lines[0].get("syl")

    def test_four_syllables_three_notes_the_last_held(self):
        n, syl = self.split([[2.0, 4.0, "tamberino"]], [(2.0, 2.4, 60), (2.4, 2.9, 64), (2.9, 4.0, 67)])
        self.assertEqual(n, 1)
        self.assertEqual([p[2] for p in syl[0]], ["tam", "be", "ri", "no"])
        self.assertAlmostEqual(syl[0][3][0], 2.86, places=2)   # the held syllable on the held note

    def test_three_syllables_three_notes(self):
        n, syl = self.split([[2.0, 4.6, "flimbardies"]], [(2.0, 2.4, 60), (2.4, 2.8, 64), (2.84, 4.6, 67)])
        self.assertEqual(n, 1)
        self.assertEqual([p[2] for p in syl[0]], ["flim", "bar", "dies"])
        self.assertAlmostEqual(syl[0][1][0], 2.4, places=2)     # where its note starts
        self.assertAlmostEqual(syl[0][2][0], 2.8, places=2)     # a little before (its consonant), not before the last
        self.assertEqual((syl[0][0][0], syl[0][-1][1]), (2.0, 4.6))

    def test_held_last_syllable_over_several_notes(self):
        n, syl = self.split([[2.0, 5.0, "lumo"]], [(2.0, 2.3, 60), (2.3, 3.0, 64), (3.0, 4.0, 66), (4.0, 5.0, 64)])
        self.assertEqual(n, 1)
        self.assertEqual([p[2] for p in syl[0]], ["lu", "mo"])
        self.assertAlmostEqual(syl[0][1][0], 2.3, places=2)

    def test_fewer_notes_with_a_held_last_one(self):
        n, syl = self.split([[2.0, 5.0, "flimbardies"]], [(2.0, 2.6, 60), (2.7, 5.0, 65)])
        self.assertEqual(n, 1)
        self.assertEqual([p[2] for p in syl[0]], ["flim", "bar", "dies"])
        self.assertAlmostEqual(syl[0][2][0], 2.66, places=2)

    def test_left_alone(self):
        sung = [(2.0, 2.4, 60), (2.4, 2.9, 64), (2.9, 4.0, 67)]
        self.assertEqual(self.split([[2.0, 4.0, "glow"]], sung)[0], 0)              # one syllable: it sweeps already
        self.assertEqual(self.split([[2.0, 2.4, "lumo"]], [(2.0, 2.2, 60), (2.2, 2.4, 64)])[0], 0)   # too short
        self.assertEqual(self.split([[2.0, 4.0, "lumo"]], [(2.0, 4.0, 60)])[0], 0)  # one note
        self.assertEqual(self.split([[2.0, 4.0, "tamberino"]], sung, lang="ja")[0], 0)
        self.assertEqual(self.split([[2.0, 4.0, "tamberino"]], sung, orig="原文")[0], 0)
        # more notes than syllables, all short (no held last syllable): not sure which syllable is where
        self.assertEqual(self.split([[2.0, 2.6, "lumo"]], [(2.0, 2.16, 60), (2.16, 2.3, 64), (2.3, 2.46, 67), (2.46, 2.6, 62)])[0], 0)

    def test_a_persons_split_is_kept(self):
        words = [[2.0, 4.6, "flimbardies"]]
        mine = [[2.0, 3.0, "flimbar"], [3.0, 4.6, "dies"]]
        lines = [{"words": words, "syl": [mine]}]
        self.assertEqual(Y.auto(lines, notes_of(words, [(2.0, 2.4, 60), (2.4, 2.8, 64), (2.84, 4.6, 67)]), 0.0, "en"), 0)
        self.assertEqual(lines[0]["syl"], [mine])

    def test_offset(self):
        words = [[1.5, 4.1, "flimbardies"]]   # the song's timing offset +0.5 s: the melody is in video time
        lines = [{"words": [list(w) for w in words]}]
        ns = notes_of([[2.0, 4.6, "x"]], [(2.0, 2.4, 60), (2.4, 2.8, 64), (2.84, 4.6, 67)])
        self.assertEqual(Y.auto(lines, ns, 0.5, "en"), 1)
        self.assertEqual((lines[0]["syl"][0][0][0], lines[0]["syl"][0][-1][1]), (1.5, 4.1))
        self.assertAlmostEqual(lines[0]["syl"][0][2][0], 2.3, places=2)

    def test_vowel_groups(self):
        for w, want in (("flimbardies", "flim|bar|dies"), ("lumo", "lu|mo"), ("glow", "glow"), ("tamble", "tam|ble"),
                        ("singing", "sing|ing"), ("glimes", "glimes"), ("plaver,", "pla|ver,"), ("oh-oh", "oh|-oh")):
            self.assertEqual("|".join(Y.syllables(w, "en")), w if want == w else want, w)
        self.assertEqual("|".join(Y.syllables("tengo", "es")), "ten|go")
        self.assertEqual("|".join(Y.syllables("dí", "es") + Y.syllables("día", "es")), "dí|dí|a")


class Editor(unittest.TestCase):
    """The editor's own functions (static/index.html), run in node."""

    def run_js(self, body):
        node = shutil.which("node")
        if not node:
            self.skipTest("no node")
        src = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
        start = src.index("const SYL_MIN = ")
        end = src.index("\n}\n", src.index("function setPiece(")) + 3
        js = src[start:end] + "\n" + body
        r = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_marker_parsing_and_pieces(self):
        out = self.run_js("""
const l = {words: [[10, 10.4, "these"], [10.5, 13, "flimbardies"]], syl: null};
const typed = "these flim|bar|dies".split(/\\s+/).map(parseSyl);
typed.forEach(([_, p], k) => setSplit(l, k, p));
const text = lineEditText(l);
const even = l.syl[1].map(p => p[2]);
setPiece(l, 1, 2, l.syl[1][2][0], 14);          // stretch the held last syllable: the word's end follows
const moved = fitPieces([12.5, 15.5, "flimbardies"], l.syl[1]).map(p => +p[0].toFixed(3));
const gone = fitPieces([10.5, 13, "flombardies"], l.syl[1]);
console.log(JSON.stringify({text, even, typed, end: l.words[1][1], moved, gone, bar: parseSyl("|go|")}));
""")
        self.assertEqual(out["text"], "these flim|bar|dies")
        self.assertEqual(out["even"], ["flim", "bar", "dies"])
        self.assertEqual(out["typed"][1], ["flimbardies", ["flim", "bar", "dies"]])
        self.assertEqual(out["end"], 14)
        self.assertEqual(out["moved"][0], 12.5)
        self.assertIsNone(out["gone"])
        self.assertEqual(out["bar"], ["go", None])


class Ks(unittest.TestCase):
    def test_typed_words_and_pieces(self):
        self.assertEqual(ks.typed_words("these flim|bar|dies"), [("these", None), ("flimbardies", ["flim", "bar", "dies"])])
        l = {"words": [[10.0, 10.4, "these"], [10.5, 13.0, "flimbardies"]]}
        ks.set_pieces(l, 1, ["flim", "bar", "dies"])
        self.assertEqual(ks.marked_text(l), "these flim|bar|dies")
        self.assertEqual(ks.text_of(l), "these flimbardies")     # (what ks compares lines by stays plain)
        ks.set_pieces(l, 1, None)
        self.assertIsNone(l["syl"])

    def run_edit(self, cmd, lines, **args):
        import argparse
        got = {}
        ks.resolve = lambda q: {"id": "x", "published": False}
        ks.edit = lambda job, ch, what: got.update(msg=ch(lines, 0.0), what=what)
        cmd(argparse.Namespace(song="s", **args))
        return got

    def test_word_piece_and_held_end(self):
        lines = [line()]
        self.run_edit(ks.cmd_word, lines, n="1", w="2.2", start="10.9", end=None)
        self.assertEqual(lines[0]["syl"][1][:2], [[10.5, 10.9, "flim"], [10.9, 11.1, "bar"]])
        self.assertNotIn("weak", lines[0])                  # a piece retimed: the line's ⚠ (if any) stays
        self.run_edit(ks.cmd_word, lines, n="1", w="2", start=None, end="13.4")
        self.assertEqual(lines[0]["syl"][1][2], [11.1, 13.4, "dies"])   # only the last piece's end moved
        self.assertEqual(lines[0]["words"][1][1], 13.4)

    def test_syllables_command(self):
        lines = [line(syl=False)]
        self.run_edit(ks.cmd_syllables, lines, n="1", w="2", text="flim|bardies", auto=False, dry=False)
        self.assertEqual([p[2] for p in lines[0]["syl"][1]], ["flim", "bardies"])
        with self.assertRaises(SystemExit):
            self.run_edit(ks.cmd_syllables, lines, n="1", w="2", text="flam|bardies", auto=False, dry=False)

    def test_text_keeps_and_makes_pieces(self):
        lines = [line()]
        self.run_edit(ks.cmd_text, lines, n="1", words="those flim|bar|dies go")
        self.assertEqual(lines[0]["syl"][1][2], [11.1, 13.0, "dies"])   # same pieces: their timing kept
        self.assertEqual(lines[0]["words"][0][2], "those")
        self.run_edit(ks.cmd_text, lines, n="1", words="those flimbardies go")
        self.assertIsNone(lines[0]["syl"])


class Api(unittest.TestCase):
    """Saving through the Studio: pieces travel with their words, and the auto split is an undoable edit."""

    @classmethod
    def setUpClass(cls):
        from tests import test_agents as TA
        cls.TA, cls.A = TA, TA.A

    def test_save_sorts_pieces_with_their_words(self):
        jid = self.TA.make_song()
        l = line()
        l["words"] = [l["words"][1], l["words"][0], l["words"][2]]   # out of order: the studio sorts them
        l["syl"] = [l["syl"][1], None, None]
        st, _, b = self.TA.api("POST", f"/api/jobs/{jid}/lines", {"lines": [l]})
        self.assertEqual(st, 200, b)
        saved = load_lines(f"{self.A.JOBS}/{jid}/words.json")[0]
        self.assertEqual(saved["syl"][1], PIECES)
        ass = open(f"{self.A.JOBS}/{jid}/lyrics.ass", encoding="utf-8").read()
        self.assertIn("flim{", ass)

    def test_auto_endpoint(self):
        jid = self.TA.make_song([{"words": [[2.0, 4.6, "flimbardies"], [4.8, 5.2, "go"]]}], published=True)
        A = self.A
        f0, per = melody([(2.0, 2.4, 60), (2.4, 2.8, 64), (2.84, 4.6, 67)])
        keep = A._look_melody, A.pitchmod.frames
        A._look_melody = lambda j: "ok"
        A.pitchmod.frames = lambda d, **kw: (f0, per)
        try:
            st, _, b = self.TA.api("POST", f"/api/jobs/{jid}/syllables", {"dry": True})
            self.assertEqual((st, b["split"], b["saved"]), (200, 1, False), b)
            self.assertEqual(b["words"], [{"line": 1, "w": 1, "word": "flim|bar|dies"}])
            self.assertIsNone(load_lines(f"{A.JOBS}/{jid}/words.json")[0]["syl"])
            st, _, b = self.TA.api("POST", f"/api/jobs/{jid}/syllables", {})
            self.assertEqual((st, b["saved"]), (200, True), b)
            self.assertEqual(len(load_lines(f"{A.JOBS}/{jid}/words.json")[0]["syl"][0]), 3)
            job = A.load(jid)
            self.assertTrue(job["unpublished_changes"])
            self.assertIn("syllables", job["last_edit"]["what"])
            st, _, b = self.TA.api("POST", f"/api/jobs/{jid}/lines/undo", {})
            self.assertEqual(st, 200, b)
            self.assertIsNone(load_lines(f"{A.JOBS}/{jid}/words.json")[0]["syl"])
        finally:
            A._look_melody, A.pitchmod.frames = keep

    def test_describe_says_syllables(self):
        import agents
        old = normalize([line(syl=False)])
        self.assertEqual(agents.describe(old, normalize([line()])), "editor: syllables in 1 line")


if __name__ == "__main__":
    unittest.main()
