"""albummeta.py and the catalog's album fields: matching (same song, not a remix; the artist credited), iTunes first
then MusicBrainz, tidy album names, and the cover copied next to the published video (removed when cleared)."""
import json, os, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import albummeta as A  # noqa: E402
import catalog  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0fakejpeg"


class Fake:
    def __init__(self, itunes=None, mb=None):
        self.itunes, self.mb, self.urls = itunes or [], mb, []

    def fetch(self, url):
        self.urls.append(url)
        if "itunes.apple.com" in url:
            return {"results": self.itunes}
        if "/ws/2/recording" in url:
            return self.mb or {"recordings": []}
        if "/ws/2/release-group/" in url:
            return {"genres": [{"name": "rock", "count": 3}]}
        raise OSError("not stubbed")

    def bytes_(self, url):
        self.urls.append(url)
        return JPEG


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.old = (A.FETCH, A.FETCH_BYTES, A._ilast[0], A._last[0])
        A._ilast[0] = A._last[0] = 0
        import time
        self._sleep = time.sleep
        A.time.sleep = lambda s: None

    def tearDown(self):
        A.FETCH, A.FETCH_BYTES = self.old[0], self.old[1]
        A.time.sleep = self._sleep

    def use(self, f):
        A.FETCH, A.FETCH_BYTES = f.fetch, f.bytes_

    def test_titles(self):
        self.assertTrue(A._title_ok("Numb", "Numb"))
        self.assertTrue(A._title_ok("Chop Suey!", "Chop Suey"))
        self.assertFalse(A._title_ok("Gurenge", "Gurenge (Noc.V Remix)"))
        self.assertFalse(A._title_ok("Numb", "Numb (Live)"))
        self.assertTrue(A._title_ok("Numb (Live)", "Numb (Live)"))
        self.assertEqual(A.tidy_album("Meteora (Deluxe Edition)"), "Meteora")
        self.assertEqual(A.tidy_album("Gurenge - Single"), "Gurenge")

    def test_itunes_first(self):
        f = Fake(itunes=[{"trackName": "Numb", "artistName": "Linkin Park", "collectionName": "Meteora (Bonus Edition)",
                          "releaseDate": "2003-03-25T08:00:00Z", "primaryGenreName": "Hard Rock", "collectionId": 1,
                          "artworkUrl100": "https://x/100x100bb.jpg"}])
        self.use(f)
        m = A.lookup("Linkin Park", "Numb", lang="en")
        self.assertEqual((m["album"], m["year"], m["genre"], m["source"], m["cover"]), ("Meteora", 2003, "Hard Rock", "itunes", JPEG))
        self.assertIn("600x600bb", f.urls[-1])
        self.assertFalse(any("musicbrainz" in u for u in f.urls))

    def test_wrong_artist_or_remix_is_not_taken(self):
        self.use(Fake(itunes=[{"trackName": "Numb", "artistName": "Somebody Else", "collectionName": "X"},
                              {"trackName": "Numb (Remix)", "artistName": "Linkin Park", "collectionName": "Y"}]))
        self.assertIsNone(A.lookup("Linkin Park", "Numb"))

    def test_musicbrainz_when_itunes_has_nothing(self):
        mb = {"recordings": [{"title": "Tunak Tunak Tun", "artist-credit": [{"name": "Daler Mehndi"}],
                              "releases": [{"id": "r2", "title": "Best Of", "date": "2005", "status": "Official",
                                            "release-group": {"id": "g2", "title": "Best Of", "primary-type": "Album", "secondary-types": ["Compilation"]}},
                                           {"id": "r1", "title": "Tunak Tunak Tun", "date": "1998-01-01", "status": "Official",
                                            "release-group": {"id": "g1", "title": "Tunak Tunak Tun", "primary-type": "Album"}}]}]}
        self.use(Fake(mb=mb))
        m = A.lookup("Daler Mehndi", "Tunak Tunak Tun", lang="hi")
        self.assertEqual((m["album"], m["year"], m["genre"], m["source"], m["ref"]), ("Tunak Tunak Tun", 1998, "rock", "musicbrainz", "g1"))


class CatalogAlbumTests(unittest.TestCase):
    def test_fields_and_cover_file(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d)
        job_dir, mv = os.path.join(d, "job"), os.path.join(d, "mv")
        os.makedirs(job_dir); os.makedirs(mv)
        open(os.path.join(job_dir, "cover.jpg"), "wb").write(JPEG)
        job = {"album_meta": {"status": "found", "album": "Meteora", "year": 2003, "genre": "Hard Rock"}}
        f = catalog.album_fields(job_dir, job, mv, "Linkin Park - Numb (Music Video)")
        self.assertEqual(f, {"album": "Meteora", "year": 2003, "genre": "Hard Rock", "cover": "Linkin Park - Numb (Music Video).cover.jpg"})
        self.assertEqual(open(os.path.join(mv, f["cover"]), "rb").read(), JPEG)
        job["album_meta"] = {"status": "cleared"}
        f = catalog.album_fields(job_dir, job, mv, "Linkin Park - Numb (Music Video)")
        self.assertEqual(f, {"album": None, "year": None, "genre": None, "cover": None})
        self.assertFalse(os.path.exists(os.path.join(mv, "Linkin Park - Numb (Music Video).cover.jpg")))


if __name__ == "__main__":
    unittest.main()
