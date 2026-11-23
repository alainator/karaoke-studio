"""Original titles (origtitle.py): found in kara.moe, LRCLIB or the video's title, and used only when they read back
as the song's title. Made-up answers instead of the network; cutlet does the Japanese readings."""
import os, time, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import origtitle as O  # noqa: E402


def fetcher(km=None, km_search=(), lrc=None, lrc_search=()):
    def fetch(url):
        if "kara.moe/api/karas/search" in url:
            return {"content": [{"titles": {"jpn": t}} for t in km_search]}
        if "kara.moe/api/karas/" in url:
            return {"titles": {"jpn": km, "qro": "romaji"}} if km else {}
        if "lrclib.net/api/get/" in url:
            return {"trackName": lrc} if lrc else {}
        if "lrclib.net/api/search" in url:
            return [{"trackName": t} for t in lrc_search]
        raise AssertionError(url)
    return fetch


class OrigTitle(unittest.TestCase):
    def find(self, title, fetch, source="karamoe", video_title="", lang="ja"):
        return O.find({"title": title, "lyrics_source": source, "lrc_id": 1 if source == "lrclib" else "k", "video_title": video_title},
                      fetch, lang=lang)

    def test_from_each_source(self):
        self.assertEqual(self.find("Gyakkou", fetcher(km="逆光"))["orig"], "逆光")
        self.assertEqual(self.find("Kiro", fetcher(lrc="季路"), source="lrclib")["source"], "LRCLIB")
        got = self.find("Soushi Souai", fetcher(), source="lrclib",
                        video_title="劇場版『名探偵コナン 100万ドルの五稜星』×aiko「相思相愛」スペシャルムービー")
        self.assertEqual((got["orig"], got["source"]), ("相思相愛", "the video's title"))   # the film's name doesn't read as it
        self.assertEqual(self.find("Mahou", fetcher(km_search=["魔法"]))["orig"], "魔法")

    def test_only_what_reads_back_as_the_title(self):
        self.assertIsNone(self.find("Brave Shine", fetcher(km="名探偵コナン")))         # an English title: nothing to add
        self.assertIsNone(self.find("Knock Out", fetcher(km="KNOCK OUTッ!")))           # mostly Latin
        self.assertIsNone(self.find("KABANERI OF THE IRON FORTRESS", fetcher(km="カバネリ オブ ジ アイアン フォートレス")))
        self.assertEqual(self.find("Peace Sign", fetcher(km="ピースサイン"))["orig"], "ピースサイン")   # its real title
        self.assertIsNone(self.find("逆光", fetcher(km="逆光")))                         # already in its own script

    def test_clean(self):
        self.assertEqual(O.clean("空の青さを知る人よ ~ Movie vers."), "空の青さを知る人よ")
        self.assertEqual(O.clean("ビバナミダ："), "ビバナミダ")
        self.assertEqual(O.clean("言わないけどね。"), "言わないけどね。")
        self.assertEqual(O.clean("ガジュマル ～Heaven in the Rain～"), "ガジュマル ～Heaven in the Rain～")


class AfterMake(unittest.TestCase):
    def test_a_made_song_gets_its_original_title_and_a_person_s_is_never_overwritten(self):
        import app as A
        saved = A.http_json
        A.http_json = fetcher(km="逆光")
        try:
            jid = A.new_job({"yt": "eeeeeeeeeee", "lrc_id": "k", "lyrics_source": "karamoe", "artist": "Ado", "title": "Gyakkou"},
                            {"kind": "person", "name": "t"})
            A.set_job(jid, status="done", result={"lang": "ja", "script": "latin", "lines": 3})
            self.assertEqual(A.fill_title_orig(jid)["orig"], "逆光")
            job = A.load(jid)
            self.assertEqual(job["title_orig"], "逆光")
            self.assertIn("original title: 逆光", job["log"][-1])
            A.set_job(jid, title_orig="自分の")
            self.assertIsNone(A.fill_title_orig(jid))
            self.assertEqual(A.load(jid)["title_orig"], "自分の")
            en = A.new_job({"yt": "fffffffffff", "lrc_id": 3, "lyrics_source": "lrclib", "artist": "X", "title": "Y"}, {"kind": "person", "name": "t"})
            A.set_job(en, status="done", result={"lang": "en", "script": "latin", "lines": 3})
            self.assertIsNone(A.fill_title_orig(en))           # an English song: not looked up
        finally:
            A.http_json = saved
