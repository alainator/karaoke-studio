"""Original titles: a song's title in its own script (紅蓮華 for Gurenge). The song page sets it (rename with
title_orig: no files change), the catalog gives it to players, and a new song whose title comes in its own script keeps
it as the original and gets a romanized title ("踊り子" -> "Odoriko")."""
import os, shutil, unittest

from tests import _env  # noqa: F401
import app as A  # noqa: E402
import catalog as CAT  # noqa: E402
import romanize  # noqa: E402
from tests._asgi import jcall  # noqa: E402
from tests.test_vocals import make_song, entry, lib  # noqa: E402


class RomanizeTitleTests(unittest.TestCase):
    def test_scripts(self):
        self.assertEqual(romanize.romanize_title("踊り子"), "Odoriko")
        self.assertIsNone(romanize.romanize_title("Gurenge"))
        self.assertIsNone(romanize.romanize_title(""))
        self.assertEqual(romanize.romanize_title("월광소나타"), "Wolgwangsonata")   # Korean
        self.assertIsNone(romanize.romanize_title("紅蓮華"))     # only Han characters: Chinese or Japanese? not guessed


class OriginalTitleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = (A.pitch_later, A.catalog_soon, A.PK_URL)
        A.pitch_later = lambda jid: None
        A.catalog_soon = lambda delay=0: A.update_catalog()
        A.PK_URL = ""

    @classmethod
    def tearDownClass(cls):
        A.pitch_later, A.catalog_soon, A.PK_URL = cls._saved

    def test_set_on_the_song_page_no_files_move(self):
        jid = make_song("LiSA", "Gurenge", vocals=None)
        base = A.load(jid)["published_as"]
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/rename", {"artist": "LiSA", "title": "Gurenge", "title_orig": " 紅蓮華 "})
        self.assertEqual(st, 200, b)
        self.assertEqual(b["message"], "original title")
        self.assertEqual(A.load(jid)["published_as"], base)          # the files stay where they are
        self.assertTrue(os.path.isfile(lib(base + ".mp4")))
        self.assertEqual(entry(jid)["title_orig"], "紅蓮華")          # players get it
        self.assertEqual(jcall(A.app, "GET", f"/api/jobs/{jid}")[2]["title_orig"], "紅蓮華")   # the song page shows it
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/rename", {"artist": "LiSA", "title": "Gurenge", "title_orig": ""})
        self.assertEqual((st, b["title_orig"]), (200, None))
        self.assertIsNone(entry(jid)["title_orig"])

    def test_with_a_rename_both_change(self):
        jid = make_song("Vaundy", "Odoriko draft", vocals=None)
        st, _, b = jcall(A.app, "POST", f"/api/jobs/{jid}/rename", {"artist": "Vaundy", "title": "Odoriko", "title_orig": "踊り子"})
        self.assertEqual(st, 200, b)
        e = entry(jid)
        self.assertEqual((e["title"], e["title_orig"]), ("Odoriko", "踊り子"))

    def test_a_new_song_in_its_own_script_is_romanized(self):
        jid = A.new_job({"yt": "aaaaaaaaaaa", "lrc_id": None, "artist": "Vaundy", "title": "踊り子"}, {"name": "test", "kind": "human"})
        self.addCleanup(shutil.rmtree, os.path.join(A.JOBS, jid), True)
        j = A.load(jid)
        self.assertEqual((j["title"], j["title_orig"]), ("Odoriko", "踊り子"))
        jid2 = A.new_job({"yt": "aaaaaaaaaaa", "lrc_id": None, "artist": "LiSA", "title": "Gurenge"}, {"name": "test", "kind": "human"})
        self.addCleanup(shutil.rmtree, os.path.join(A.JOBS, jid2), True)
        self.assertNotIn("title_orig", A.load(jid2))


if __name__ == "__main__":
    unittest.main()
