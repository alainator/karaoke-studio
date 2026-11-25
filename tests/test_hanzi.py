"""Chinese characters as shown (hanzi.py): Traditional by default (Taiwan standard for Mandarin, Hong Kong for
Cantonese), Simplified, or as in the lyrics; a Studio setting and a per-song choice. Only what's shown changes (the
subtitles' hanzi rows, the published words' "orig", the original title): words.json, the pinyin / jyutping and the
timing stay exactly as made. Plus the "both_original" display mode (the original as the main line, the romanized
line small above it). Made-up text only; no GPU or network."""
import json, os, re, time, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import app as A  # noqa: E402
import catalog as CAT  # noqa: E402
import hanzi as HZ  # noqa: E402
from assgen import MODES, display_modes, make_ass, save_lines, write_lyrics  # noqa: E402
from romanize import romanize_cantonese, romanize_mandarin  # noqa: E402
from tests._asgi import jcall  # noqa: E402

HAS_CC = HZ.available()
_n = [0]
_saved = {}


def setUpModule():  # publishing here must not start pitch tracking or a late catalog write (other tests watch those)
    _saved["v"] = (A.pitch_later, A.catalog_soon)
    A.pitch_later = lambda jid: None
    A.catalog_soon = lambda delay=0: A.update_catalog()


def tearDownModule():
    A.pitch_later, A.catalog_soon = _saved["v"]

SIMP = ["我爱你 头发", "干杯 OK!", "以后 后来"]        # simplified (phrases: 头发 -> 頭髮, 干杯 -> 乾杯)
TRAD = ["我愛你 頭髮", "乾杯 OK!", "以後 後來"]
TRAD_ODD = ["台灣 說 游 里", "我們 了解 後來"]          # traditional, with characters OpenCC would still swap


def syllables(orig, lang="zh"):
    """The words a Chinese line gets when made: one romanized syllable per character (punctuation rides along)."""
    conv = romanize_cantonese if lang == "yue" else romanize_mandarin
    toks = []
    for t in re.sub(r"\s+", " ", conv(orig)).split():
        if not re.search(r"[A-Za-z]", t) and toks:
            toks[-1] += " " + t
        else:
            toks.append(t)
    return toks


def lines_of(origs, lang="zh", start=2.0):
    out, t = [], start
    for o in origs:
        ws = []
        for s in syllables(o, lang):
            ws.append([round(t, 2), round(t + 0.4, 2), s])
            t += 0.5
        out.append({"words": ws, "orig": o})
        t += 1.5
    return out


def make_chinese(origs, lang="zh", title="Ni Hao (你好)", title_orig=None, publish=True, hanzi=None, start=2.0):
    _n[0] += 1
    jid = f"20260404-{_n[0]:06d}-hanzi-{_n[0]}"
    d = os.path.join(A.JOBS, jid)
    os.makedirs(d)
    save_lines(os.path.join(d, "words.json"), lines_of(origs, lang, start), [])
    open(os.path.join(d, "out.mp4"), "wb").write(b"video " * 20)
    job = {"id": jid, "yt": "aaaaaaaaaaa", "artist": "Test Singer", "title": title, "status": "done", "step": "done",
           "created": time.time(), "finished": time.time(), "offset": 0.0, "log": [],
           "result": {"duration": 30.0, "lang": lang, "script": "zh"}}
    if title_orig:
        job["title_orig"] = title_orig
    if hanzi:
        job["hanzi"] = hanzi
    A.save(jid, job)
    A.rebuild_lyrics(jid, job)
    if publish:
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/publish", {"remove": []})
        assert st == 200, b
    return jid


def dialogue(ass, style):
    """(start, end, text without the position/size head) of each Dialogue of a style."""
    out = []
    for l in ass.splitlines():
        if l.startswith("Dialogue:") and l.split(",")[3] == style:
            p = l.split(",", 9)
            out.append((p[1], p[2], re.sub(r"^\{[^}]*\}", "", p[9])))
    return out


def shown_text(ass, style="Orig"):
    return "".join(re.sub(r"\{[^}]*\}|\\N", "", t) for _, _, t in dialogue(ass, style))


@unittest.skipUnless(HAS_CC, "needs opencc-python-reimplemented")
class Conversion(unittest.TestCase):
    def test_mandarin_to_traditional_with_phrases(self):
        out, script = HZ.show_lines(SIMP, "zh", "traditional")
        self.assertEqual(out, TRAD)
        self.assertEqual(script, "traditional")

    def test_cantonese_to_hong_kong(self):
        out, _ = HZ.show_lines(["这里 说"], "yue", "traditional")
        self.assertEqual(out, ["這裏 説"])                 # Hong Kong forms (Taiwan: 這裡 說)
        self.assertEqual(HZ.show_lines(["这里 说"], "zh", "traditional")[0], ["這裡 說"])

    def test_traditional_stays_exactly_as_written(self):
        for lang in ("zh", "yue"):
            for lines in (TRAD, TRAD_ODD, TRAD + TRAD_ODD):
                out, script = HZ.show_lines(lines, lang, "traditional")
                self.assertEqual(out, lines, lang)
                self.assertEqual(script, "traditional")
            self.assertEqual(HZ.show_text("後來", lang, "traditional"), "後來")

    def test_simplified_and_as_in_the_lyrics(self):
        self.assertEqual(HZ.show_lines(TRAD, "zh", "simplified")[0], SIMP)
        self.assertEqual(HZ.show_lines(SIMP, "zh", "simplified")[0], SIMP)   # already simplified: as is
        self.assertEqual(HZ.show_lines(SIMP, "zh", "lyrics"), (SIMP, "simplified"))
        self.assertEqual(HZ.show_lines(TRAD, "zh", "lyrics"), (TRAD, "traditional"))

    def test_latin_punctuation_and_empty_rows_kept(self):
        out, _ = HZ.show_lines(["Hello, 世界！我爱你 (oh) 123…", None, "", "Yeah yeah"], "zh", "traditional")
        self.assertEqual(out, ["Hello, 世界！我愛你 (oh) 123…", None, "", "Yeah yeah"])

    def test_words_are_not_swapped(self):
        # s2twp would make 消息 -> 訊息 and 内存 -> 記憶體: other words than the ones sung (and other lengths)
        out, _ = HZ.show_lines(["没有消息", "内存"], "zh", "traditional")
        self.assertEqual(out, ["沒有消息", "內存"])
        self.assertEqual([len(o) for o in out], [4, 2])

    def test_mixed_song_lines_with_traditional_characters_are_left(self):
        out, _ = HZ.show_lines(["我爱你 头发", "我爱你", "頭髮 头发"], "zh", "traditional")
        self.assertEqual(out, ["我愛你 頭髮", "我愛你", "頭髮 头发"])

    def test_only_chinese_songs(self):
        for lang in ("ja", "ko", None, "en"):
            self.assertEqual(HZ.show_lines(["国 学校"], lang, "traditional")[0], ["国 学校"])

    def test_title_on_its_own(self):
        self.assertEqual(HZ.show_text("Yan Yuan (演员)", "zh", "traditional"), "Yan Yuan (演員)")
        self.assertEqual(HZ.show_text("十年", "zh", "traditional"), "十年")
        self.assertIsNone(HZ.show_text(None, "zh", "traditional"))


class WithoutThePackage(unittest.TestCase):
    """An old image or a helper PC without opencc: everything as in the lyrics, the font as before."""

    def test_nothing_changes(self):
        saved = HZ._cc
        HZ._cc = lambda name: None
        try:
            self.assertFalse(HZ.available())
            for choice in HZ.CHOICES:
                self.assertEqual(HZ.show_lines(SIMP, "zh", choice), (SIMP, None))
                self.assertEqual(HZ.show_text("Yan Yuan (演员)", "zh", choice), "Yan Yuan (演员)")
            self.assertEqual(HZ.font_lang("zh", None), "zh")
            self.assertEqual(HZ.font_lang("yue", None), "yue")
            a = make_ass(lines_of(SIMP), mode="both", embed_fonts=False, lang="zh", hanzi="traditional")
            self.assertEqual(a, make_ass(lines_of(SIMP), mode="both", embed_fonts=False, lang="zh", hanzi="lyrics"))
        finally:
            HZ._cc = saved


class Setting(unittest.TestCase):
    def test_default_traditional_and_song_override(self):
        self.assertEqual(A.C.LYRICS_HANZI, "traditional")
        self.assertEqual(HZ.studio_choice(), "traditional")
        self.assertEqual(HZ.song_choice({}), "traditional")
        self.assertEqual(HZ.song_choice({"hanzi": "simplified"}), "simplified")
        self.assertEqual(HZ.song_choice({"hanzi": "nonsense"}), "traditional")
        saved = A.C.LYRICS_HANZI
        A.C.LYRICS_HANZI = "lyrics"
        try:
            self.assertEqual(HZ.song_choice({}), "lyrics")
        finally:
            A.C.LYRICS_HANZI = saved

    def test_config_names(self):
        self.assertEqual(HZ.config_for("zh", "traditional"), "s2tw")
        self.assertEqual(HZ.config_for("yue", "traditional"), "s2hk")
        self.assertEqual(HZ.config_for("zh", "simplified"), "t2s")
        self.assertIsNone(HZ.config_for("zh", "lyrics"))
        self.assertIsNone(HZ.config_for("ja", "traditional"))

    def test_endpoint(self):
        jid = make_chinese(SIMP, publish=False)
        st, _, b = jcall(A.app, "GET", f"/api/jobs/{jid}")
        self.assertEqual(b["hanzi"]["shown"], "traditional")
        self.assertIsNone(b["hanzi"]["song"])
        for bad in ("big5", 3):
            self.assertEqual(jcall(A.app, "POST", f"/api/jobs/{jid}/hanzi", {"hanzi": bad})[0], 400)
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/hanzi", {"hanzi": "simplified"})
        self.assertEqual(st, 200, b)
        self.assertEqual((b["hanzi"]["song"], b["hanzi"]["shown"]), ("simplified", "simplified"))
        self.assertEqual(A.load(jid)["hanzi"], "simplified")
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/hanzi", {"hanzi": None})
        self.assertEqual((st, b["hanzi"]["song"], b["hanzi"]["shown"]), (200, None, "traditional"))
        self.assertNotIn("hanzi", A.load(jid))
        latin = make_chinese(["la la"], publish=False)
        j = A.load(latin); j["result"]["lang"] = "en"; A.save(latin, j)
        self.assertEqual(jcall(A.app, "POST", f"/api/jobs/{latin}/hanzi", {"hanzi": "traditional"})[0], 400)
        self.assertNotIn("hanzi", jcall(A.app, "GET", f"/api/jobs/{latin}")[2])

    @unittest.skipUnless(HAS_CC, "needs opencc-python-reimplemented")
    def test_per_song_choice_redraws_and_marks_a_published_song(self):
        jid = make_chinese(SIMP)
        ass = open(A.job_path(jid, "lyrics.ass"), encoding="utf-8").read()
        self.assertIn("頭髮", shown_text(ass))
        self.assertNotIn("头发", shown_text(ass))
        self.assertFalse(A.load(jid).get("unpublished_changes"))
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/hanzi", {"hanzi": "lyrics"})
        self.assertTrue(b["unpublished_changes"])
        ass = open(A.job_path(jid, "lyrics.ass"), encoding="utf-8").read()
        self.assertIn("头发", shown_text(ass))


class RomanizationUnchanged(unittest.TestCase):
    """The pinyin / jyutping, the words and their timing are the same whatever characters are shown."""

    def test_words_json_and_romanized_rows(self):
        for lang in ("zh", "yue"):
            for origs in (SIMP, TRAD):
                jid = make_chinese(origs, lang=lang, publish=False)
                wp = A.job_path(jid, "words.json")
                before = open(wp, "rb").read()
                romanized = {}
                for choice in HZ.CHOICES:
                    st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/hanzi", {"hanzi": choice})
                    self.assertEqual(st, 200, b)
                    self.assertEqual(open(wp, "rb").read(), before)       # words.json: untouched
                    for m in ("both", "romaji", "both_original"):
                        ass = open(A.job_path(jid, f"lyrics.{m}.ass"), encoding="utf-8").read()
                        romanized.setdefault(m, set()).add(json.dumps(dialogue(ass, "Main"), ensure_ascii=False))
                for m, seen in romanized.items():
                    self.assertEqual(len(seen), 1, (lang, origs, m))   # the same romanized rows, text and timing
                # the editor (and every edit) gets the lines as written
                got = jcall(A.app, "GET", f"/api/jobs/{jid}/lines")[2]
                self.assertEqual([l["orig"] for l in got["lines"]], origs)
                # what the pipeline / the Pinyin-Jyutping switch romanize: the lines as written, so the same as today
                words = [[w[2] for w in l["words"]] for l in json.load(open(wp))["lines"]]
                self.assertEqual(words, [syllables(o, lang) for o in origs])

    def test_switching_reading_romanizes_the_lines_as_written(self):
        jid = make_chinese(SIMP, publish=False, hanzi="traditional")
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/chinese-reading", {"to": "yue"})
        self.assertEqual(st, 200, b)
        ls = json.load(open(A.job_path(jid, "words.json")))["lines"]
        self.assertEqual([l["orig"] for l in ls], SIMP)
        self.assertEqual([[w[2] for w in l["words"]] for l in ls], [syllables(o, "yue") for o in SIMP])


@unittest.skipUnless(HAS_CC, "needs opencc-python-reimplemented")
class Published(unittest.TestCase):
    def entry(self, jid):
        A.update_catalog()
        return next(e for e in CAT.read_catalog(A.LIB)["songs"] if e["id"] == jid)

    def test_catalog_title_orig_and_words(self):
        jid = make_chinese(SIMP, title="Yan Yuan (演员)")
        e = self.entry(jid)
        self.assertEqual((e["title"], e["title_orig"]), ("Yan Yuan", "演員"))
        self.assertEqual(e["base"], A.load(jid)["published_as"])               # files keep their names
        self.assertIn("演员", e["base"])
        words = json.load(open(os.path.join(A.MV_DIR, e["words"]), encoding="utf-8"))
        self.assertEqual([l["orig"] for l in words["lines"]], TRAD)
        self.assertEqual(A.summary(A.load(jid))["hanzi"]["title_orig"], "演員")
        self.assertEqual(A.load(jid)["title"], "Yan Yuan (演员)")              # the song page's title as typed
        # the song's own title_orig, and a title written only in Chinese
        jid2 = make_chinese(TRAD, title="Hou Lai", title_orig="后来")
        self.assertEqual(self.entry(jid2)["title_orig"], "後來")
        jid3 = make_chinese(TRAD, title="后来", hanzi="lyrics")
        self.assertEqual(self.entry(jid3)["title_orig"], "后来")
        jid4 = make_chinese(TRAD, title="Hou Lai (後來)", hanzi="simplified")
        self.assertEqual(self.entry(jid4)["title_orig"], "后来")

    def test_the_title_card_is_converted(self):
        jid = make_chinese(SIMP, title="Yan Yuan (演员)", publish=False, start=12.0)
        ass = open(A.job_path(jid, "lyrics.ass"), encoding="utf-8").read()
        self.assertIn("演員", "".join(t for _, _, t in dialogue(ass, "Card")))


class BothOriginal(unittest.TestCase):
    """The original as the main line (lit up word by word, as in "original"), the romanized line small above it."""

    def test_modes(self):
        self.assertIn("both_original", MODES)
        self.assertEqual(display_modes(lines_of(TRAD)), ["both", "original", "romaji", "both_original"])
        ja = [{"words": [[1.0, 1.5, "sakura"]], "orig": "さくら"}]
        self.assertEqual(display_modes(ja), ["both", "original", "romaji", "kana", "both_original"])
        self.assertEqual(display_modes([{"words": [[1.0, 1.5, "la"]]}]), [])

    def test_original_row_as_in_original_mode(self):
        for lines in (lines_of(TRAD), [{"words": [[1.0, 1.4, "sa"], [1.5, 2.0, "ku"], [2.1, 2.8, "ra"]], "orig": "さくら"},
                                       {"words": [[4.0, 4.5, "Oh"], [4.6, 5.0, "yeah"]], "orig": None},
                                       {"words": [[7.0, 7.5, "sa"], [7.6, 8.0, "ku"]], "orig": "さく"}]):
            orig = make_ass(lines, mode="original", embed_fonts=False, lang="zh")
            bo = make_ass(lines, mode="both_original", embed_fonts=False, lang="zh")
            self.assertEqual(dialogue(bo, "Orig"), dialogue(orig, "Orig"))   # same text, sweep and times
            # one romanized row per line that has an original, over the same time; lines without one as in "original"
            with_orig = [l for l in lines if l.get("orig")]
            main_bo, main_o = dialogue(bo, "Main"), dialogue(orig, "Main")
            self.assertEqual(len(main_bo), len(with_orig) + len(main_o))
            self.assertEqual([x[:2] for x in dialogue(bo, "Orig")], [x[:2] for x in main_bo if x not in main_o])

    def test_romanized_row_is_small_and_above(self):
        lines = lines_of(TRAD)
        bo = make_ass(lines, mode="both_original", embed_fonts=False, lang="zh")
        styles = {l.split(",")[0][7:]: int(l.split(",")[2]) for l in bo.splitlines() if l.startswith("Style:")}
        self.assertLess(styles["Main"], styles["Orig"])
        ev = [l for l in bo.splitlines() if l.startswith("Dialogue: 0,")]
        pos = {}
        for l in ev:
            st, y = l.split(",")[3], int(re.search(r"\\pos\(\d+,(\d+)\)", l).group(1))
            fs = int(re.search(r"\\fs(\d+)", l).group(1))
            pos.setdefault(l.split(",")[1], {})[st] = (y, fs)
        for start, rows in pos.items():
            self.assertLess(rows["Main"][0], rows["Orig"][0])       # the romanized row sits above the original
            self.assertLess(rows["Main"][1], rows["Orig"][1] * 0.75)  # and is clearly smaller
        # the romanized row lights up in step with the original (the same sweep times)
        k = lambda t: re.findall(r"\\kf?(\d+)", t)
        for (_, _, a), (_, _, b) in zip(dialogue(bo, "Main"), dialogue(bo, "Orig")):
            self.assertEqual(sum(map(int, k(a))), sum(map(int, k(b))))

    @unittest.skipUnless(HAS_CC, "needs opencc-python-reimplemented")
    def test_original_row_is_converted(self):
        bo = make_ass(lines_of(SIMP), mode="both_original", embed_fonts=False, lang="zh", hanzi="traditional")
        self.assertIn("頭髮", shown_text(bo))

    def test_mode_file_published_listed_and_removed(self):
        jid = make_chinese(TRAD)
        self.assertTrue(os.path.isfile(A.job_path(jid, "lyrics.both_original.ass")))
        base = A.load(jid)["published_as"]
        self.assertTrue(os.path.isfile(os.path.join(A.MV_DIR, base + ".both_original.ass")))
        A.update_catalog()
        e = next(e for e in CAT.read_catalog(A.LIB)["songs"] if e["id"] == jid)
        self.assertEqual(e["lyrics"]["modes"]["both_original"], base + ".both_original.ass")
        self.assertIn(f"{e['folder']}/{base}.both_original.ass", CAT.files_of(e))
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/display", {"mode": "both_original"})
        self.assertEqual((st, b["lyrics_display"]), (200, "both_original"))
        self.assertEqual(open(A.job_path(jid, "lyrics.ass"), encoding="utf-8").read(),
                         open(A.job_path(jid, "lyrics.both_original.ass"), encoding="utf-8").read())
        st, _, b = jcall(A.app, "POST", "/api/library/remove", {"job": jid})
        self.assertEqual(st, 200, b)
        self.assertFalse(os.path.exists(os.path.join(A.MV_DIR, base + ".both_original.ass")))

    def test_rebuild_report_lists_a_new_mode_file(self):
        jid = make_chinese(TRAD, publish=False)
        os.remove(A.job_path(jid, "lyrics.both_original.ass"))
        rep = []
        self.assertFalse(write_lyrics(A.job_path(jid), A.load(jid), dry=True, report=rep))
        self.assertEqual(rep, ["lyrics.both_original.ass"])
        self.assertFalse(os.path.exists(A.job_path(jid, "lyrics.both_original.ass")))


if __name__ == "__main__":
    unittest.main()
