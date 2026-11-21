"""Lyric text before timing (romanize.py): lines with nothing to sing are left out, brackets in romanized Chinese and
Thai lines stay with their words, Thai is romanized syllable by syllable without leftover Thai signs. Made-up text
only; no GPU or network."""
import os, re, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import romanize as R  # noqa: E402
from lyricmap import orig_spans  # noqa: E402

THAI = re.compile(r"[฀-๿]")
OPEN, CLOSE = "(（「“", ")）」”"


def as_timed(romanized):
    """The words a line ends up with after word timing (pipeline.align_at): a word without letters rides along with
    the word before it, and is lost when there is none."""
    out = []
    for w in romanized.split():
        if R.sound_key(w):
            out.append(w)
        elif out:
            out[-1] += " " + w
    return out


def balanced(words):
    text = " ".join(words)
    return all(text.count(a) == text.count(b) for a, b in zip(OPEN, CLOSE))


class UnsungLines(unittest.TestCase):
    def test_punctuation_only_lines_go(self):
        lines = [(0.0, "la la lo"), (4.0, '"""""'), (8.0, "Oh"), (12.0, "7"), (16.0, "爱"), (20.0, "(oh)"),
                 (24.0, "..."), (28.0, "‏\"\" \"\"‏"), (32.0, "＂＂"), (36.0, "سلام"), (40.0, "ก็")]
        kept, keyed, dropped = R.drop_unsung(lines, {0: "la", 2: "oh", 4: "ai", 6: "x"})
        self.assertEqual([x for _, x in kept], ["la la lo", "Oh", "7", "爱", "(oh)", "سلام", "ก็"])
        self.assertEqual([t for t, _ in kept], [0.0, 8.0, 12.0, 16.0, 20.0, 36.0, 40.0])
        self.assertEqual(keyed, {0: "la", 1: "oh", 3: "ai"})  # hand-made romaji follows its line; "..."'s is gone
        self.assertEqual(len(dropped), 4)

    def test_nothing_to_drop(self):
        lines = [(0.0, "one"), (1.0, "two words")]
        kept, keyed, dropped = R.drop_unsung(lines)
        self.assertEqual((kept, keyed, dropped), (lines, {}, []))
        self.assertFalse(R.has_words("") or R.has_words(None) or R.has_words(" - ! "))

    def test_pipeline_uses_it(self):
        src = open(os.path.join(_env.REPO, "pipeline.py"), encoding="utf-8").read()
        self.assertIn("drop_unsung(synced, hand_romaji)", src)


class Brackets(unittest.TestCase):
    LINES = ["（你好你好）我们", "(你好)我们", "我(你好)", "「你好」他们", "Hey (你好)"]

    def test_mandarin_line_opening_with_a_bracket_keeps_both(self):
        for line in self.LINES:
            for conv in (R.romanize_mandarin, R.romanize_cantonese):
                words = as_timed(conv(line))
                self.assertTrue(balanced(words), (line, conv.__name__, words))
                self.assertTrue(all(re.search(r"[A-Za-z]", w) for w in words), words)
        self.assertEqual(R.romanize_mandarin("(你好)我们"), "(ni hao) wo men")
        self.assertEqual(R.romanize_mandarin("我(你好)"), "wo (ni hao)")

    def test_pinyin_and_jyutping_have_the_same_words(self):
        # the aligner reads a Cantonese line as its pinyin, word for word (pipeline ALIGN_AS)
        for line in self.LINES + ["你好，我们", "他说：“你好”"]:
            self.assertEqual(len(R.romanize_mandarin(line).split()), len(R.romanize_cantonese(line).split()), line)

    def test_original_line_still_lights_up_by_character(self):
        for line in self.LINES[:4]:
            words = as_timed(R.romanize_mandarin(line))
            self.assertEqual(orig_spans(line, words)[1], "char", (line, words))

    def test_other_punctuation_unchanged(self):
        self.assertEqual(R.romanize_mandarin("你好，我们"), "ni hao ， wo men")
        self.assertEqual(R.romanize_mandarin("你好我们"), "ni hao wo men")

    def test_glue(self):
        self.assertEqual(R.glue_brackets(["(", "a", "b", ")", "c"]), ["(a", "b)", "c"])
        self.assertEqual(R.glue_brackets(["a", "，", "b"]), ["a", "，", "b"])
        self.assertEqual(R.glue_brackets(["a", "）（", "b", "）"]), ["a）", "（b）"])
        self.assertEqual(R.glue_brackets(["(", ")"]), ["()"])
        self.assertEqual(R.glue_brackets([]), [])


class Thai(unittest.TestCase):
    def test_syllables_romanize_whole(self):
        # made-up or everyday syllables with the shapes royin got wrong
        cases = {
            "ก็": "ko",          # mai taikhu alone after a consonant (was "k็")
            "แป็ก": "paek",      # mai taikhu with a vowel (was "pae็k")
            "เกราะ": "kro",      # leading vowel + two-consonant start + ะ (was "keraะ")
            "เกลี่ยน": "klian",  # leading vowel + two-consonant start (was "kelin")
            "ใพร": "phrai",
            "แกวน": "kwaen",
            "เหนือน": "nuean",   # silent ห behind a leading vowel (was "enuen")
            "หาด": "hat",        # sounded ห (was "at")
            "เห็ด": "het",
            "หนู": "nu",         # silent ห before a low consonant stays silent
        }
        for th, rom in cases.items():
            self.assertEqual(R._thai_syllable(th), rom, th)

    def test_words_are_cut_into_syllables(self):
        self.assertEqual(R.romanize_generic("จดหมาย", "thai"), "chotmai")  # was "chadahamai"
        self.assertEqual(R.romanize_generic("ก็ได้", "thai"), "kodai")

    def test_no_thai_left_and_one_word_per_thai_word(self):
        from pythainlp.tokenize import word_tokenize
        for line in ["ก็ได้ จดหมาย", "แป็กกา เกราะ เกลือ", "เหนือนหาด ok นกบิน"]:
            rom = R.romanize_generic(line, "thai")
            self.assertIsNone(THAI.search(rom), (line, rom))
            n = sum(len([w for w in word_tokenize(t, engine="newmm", keep_whitespace=False) if w.strip()])
                    if THAI.search(t) else 1 for t in line.split())
            self.assertEqual(len(rom.split()), n, (line, rom))
            self.assertIn(orig_spans(line, rom.split())[1], ("space", "thai"))  # not split by length
        rom = R.romanize_generic("นกบินจดหมาย", "thai")
        self.assertEqual(orig_spans("นกบินจดหมาย", rom.split())[1], "thai")

    def test_brackets(self):
        for line in ["(มา)กา", "กา (มา)", "กา(มา ดี)"]:
            words = as_timed(R.romanize_generic(line, "thai"))
            self.assertTrue(balanced(words), (line, words))
        self.assertEqual(R.romanize_generic("(มา)กา", "thai"), "(ma) ka")


if __name__ == "__main__":
    unittest.main()
