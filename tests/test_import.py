"""Pasted video links and imports (songlinks.py; the imports in app.py): which links are videos or playlists, the
lines of a pasted list, a song's names from a video's title; then the link search and the import lookup with
made-up yt-dlp and LRCLIB answers (no network): ready rows and each skip reason, a playlist keeping its own
videos, duplicates, songs the studio has, the review kept on disk, Make ticked, and who may use it.
Made-up artists and titles; the "lyrics" are la-la lines."""
import ipaddress, json, os, re, time, unittest, urllib.parse

from tests import _env  # noqa: F401  (must come first: throwaway folders)
from tests._asgi import jcall  # noqa: E402
import app as A  # noqa: E402
import config as C  # noqa: E402
import songlinks as SL  # noqa: E402


# ---------------- pure: links, lists, names ----------------
class Links(unittest.TestCase):
    def test_video_link_shapes(self):
        vid = "AbCdEfGhI_-"
        for url in (f"https://www.youtube.com/watch?v={vid}", f"https://youtube.com/watch?v={vid}&t=42s",
                    f"https://m.youtube.com/watch?feature=share&v={vid}", f"https://music.youtube.com/watch?v={vid}&si=xyz",
                    f"https://youtu.be/{vid}", f"https://youtu.be/{vid}?t=30&si=abc", f"https://www.youtube.com/shorts/{vid}",
                    f"https://www.youtube.com/embed/{vid}?start=4", f"https://www.youtube.com/live/{vid}", f"youtube.com/watch?v={vid}",
                    f"www.youtube.com/watch?v={vid}", f"https://www.youtube-nocookie.com/embed/{vid}", f"http://youtu.be/{vid}"):
            self.assertEqual(SL.parse_link(url), {"video": vid, "list": None}, url)
        got = SL.parse_link(f"https://www.youtube.com/watch?v={vid}&list=PLabcdefghij12345&index=3")
        self.assertEqual(got, {"video": vid, "list": "PLabcdefghij12345"})

    def test_playlists_and_not_links(self):
        self.assertEqual(SL.parse_link("https://www.youtube.com/playlist?list=PLabcdefghij12345"), {"video": None, "list": "PLabcdefghij12345"})
        self.assertEqual(SL.parse_link("https://music.youtube.com/playlist?list=OLAK5uy_abcdefghijk"), {"video": None, "list": "OLAK5uy_abcdefghijk"})
        for x in ("Nova Lane - Paper Lanterns", "https://example.com/watch?v=AbCdEfGhI_-", "https://www.youtube.com/@somechannel",
                  "https://www.youtube.com/watch?v=short", "https://youtu.be/", "", "https://evil.youtube.com.example.org/watch?v=AbCdEfGhI_-"):
            self.assertIsNone(SL.parse_link(x), x)
        self.assertTrue(SL.is_url("https://vimeo.com/12345"))
        self.assertTrue(SL.is_url("example.org/a/b"))
        self.assertFalse(SL.is_url("AC/DC - Thunder Road"))
        self.assertFalse(SL.is_url("Nova Lane - Paper Lanterns"))

    def test_playlist_or_list(self):
        self.assertEqual(SL.import_kind("  https://www.youtube.com/playlist?list=PLabcdefghij12345 \n"), ("playlist", "PLabcdefghij12345"))
        self.assertEqual(SL.import_kind("https://www.youtube.com/watch?v=AbCdEfGhI_-&list=PLabcdefghij12345"), ("playlist", "PLabcdefghij12345"))
        self.assertEqual(SL.import_kind("https://youtu.be/AbCdEfGhI_-"), ("list", None))
        self.assertEqual(SL.import_kind("https://www.youtube.com/watch?v=AbCdEfGhI_-&list=RDAbCdEfGhI_-&start_radio=1"), ("list", None))   # a mix
        self.assertEqual(SL.import_kind("https://www.youtube.com/playlist?list=RDCLAKabcdefghij"), ("playlist", "RDCLAKabcdefghij"))
        self.assertEqual(SL.import_kind("A - B\nhttps://www.youtube.com/playlist?list=PLabcdefghij12345"), ("list", None))


class Lists(unittest.TestCase):
    def test_numbering_blanks_links(self):
        text = ("1. Nova Lane - Paper Lanterns\n\n  02 - Iris Vale - Glass River\n3) Bram - Echo\n- Mika Sol - Neon Harbor\n"
                "#5 Ada - Kawa\n99 Red Kites - Lowland\n311 - Amber Lane\n04 - https://youtu.be/BbCdEfGhI_-\nhttps://youtu.be/AbCdEfGhI_-?t=3\n"
                "https://www.youtube.com/playlist?list=PLabcdefghij12345\nhttps://vimeo.com/123\nLumen Coast\tTide Line\n")
        items, left = SL.parse_list(text)
        self.assertEqual(left, 0)
        self.assertEqual([i.get("q") for i in items if i["kind"] == "text"],
                         ["Nova Lane - Paper Lanterns", "Iris Vale - Glass River", "Bram - Echo", "Mika Sol - Neon Harbor",
                          "Ada - Kawa", "99 Red Kites - Lowland", "311 - Amber Lane", "Lumen Coast - Tide Line"])
        self.assertEqual([i["id"] for i in items if i["kind"] == "video"], ["BbCdEfGhI_-", "AbCdEfGhI_-"])
        self.assertEqual([i["why"] for i in items if i["kind"] == "bad"],
                         ["a playlist link inside a list: import it on its own", "not a YouTube video link"])
        self.assertEqual(items[0]["input"], "1. Nova Lane - Paper Lanterns")   # what was pasted, for the review

    def test_cap(self):
        items, left = SL.parse_list("\n".join(f"Artist {i} - Song {i}" for i in range(73)))
        self.assertEqual((len(items), left), (73, 0))                  # (the old cap was 50)
        items, left = SL.parse_list("\n".join(f"Artist {i} - Song {i}" for i in range(SL.MAX_IMPORT + 23)))
        self.assertEqual((len(items), left, SL.MAX_IMPORT), (2000, 23, 2000))   # only a guard against accidents
        self.assertEqual(items[-1]["q"], "Artist 1999 - Song 1999")
        self.assertEqual(SL.parse_list("\n\n  \n"), ([], 0))


class Names(unittest.TestCase):
    CASES = [
        # (video title, channel) -> (artist, title, how)
        (("Nova Lane - Paper Lanterns (Official Music Video) [4K]", "Nova Lane"), ("Nova Lane", "Paper Lanterns", "title")),
        (("Iris Vale – Glass River | Official Video", "Iris Vale"), ("Iris Vale", "Glass River", "title")),
        (("Bram - Echo (feat. Ada Moon)", "Bram"), ("Bram & Ada Moon", "Echo", "title")),
        (("Bram ft. Ada Moon - Echo (Official Video)", "Bram"), ("Bram & Ada Moon", "Echo", "title")),
        (("Echo [with Ada Moon & Kit Rowe] (Official Audio)", "BramVEVO"), ("Bram & Ada Moon & Kit Rowe", "Echo", "channel")),
        (("STELLAR ARC 'Night Drive' M/V", "Some Labels"), ("Stellar Arc", "Night Drive", "title")),   # (SHOUTED: as the Make page)
        (("TWINE \"Fern Lights\" M/V", "Some Labels"), ("TWINE", "Fern Lights", "title")),
        (("STELLAR ARC (스텔라아크) \"Moon Gate\" Official MV", "Some Entertainment"), ("Stellar Arc", "Moon Gate", "title")),
        (("[MV] HANA(하나) _ Blue Lantern Hour", "Some Music"), ("HANA", "Blue Lantern Hour", "title")),
        (("June Arbor『銅の月』Music Video", "June Arbor"), ("June Arbor", "銅の月", "title")),
        (("「夜の窓」Mika Sol", "Mika Sol Official"), ("Mika Sol", "夜の窓", "title")),
        (("Mika Sol \"神메뉴(Paper Crane Plan)\" M/V", "Some Records"), ("Mika Sol", "Paper Crane Plan (神메뉴)", "title")),
        (("Nova Lane - Paper Lanterns (Lyric Video)", "Nova Lane"), ("Nova Lane", "Paper Lanterns", "title")),
        (("Paper Lanterns", "Nova Lane - Topic"), ("Nova Lane", "Paper Lanterns", "channel")),
        (("Paper Lanterns (Official Video)", "NovaLaneVEVO"), ("Nova Lane", "Paper Lanterns", "channel")),
        (("Glass River (Official Music Video)", "Iris Vale Official"), ("Iris Vale", "Glass River", "channel")),
        (("Untitled Jam Session", "Some Records"), ("", "Untitled Jam Session", None)),
        (("Red Kites - Lowland ~Official Video", "x"), ("Red Kites", "Lowland", "title")),
        (("Red Kites - Lowland (Live at the Harbour Hall)", "x"), ("Red Kites", "Lowland", "title")),
        (("Red Kites - Lowland (Remastered 2009) - High Quality", "x"), ("Red Kites", "Lowland", "title")),
        (("Red Kites - Lowland (Kit's Version)", "x"), ("Red Kites", "Lowland (Kit's Version)", "title")),
        (("Lumen x Coast - Tide Line", "x"), ("Lumen & Coast", "Tide Line", "title")),
        (("THE QUIET ARCADE - TWO SMALL RIVERS (Official Video)", "x"), ("The Quiet Arcade", "Two Small Rivers", "title")),
        (("Rock N' Rollers - Callin' O' The Sea (Official Music Video)", "x"), ("Rock N' Rollers", "Callin' O' The Sea", "title")),
    ]

    def test_shapes(self):
        for (title, channel), want in self.CASES:
            g = SL.names_from_video(title, channel)
            self.assertEqual((g["artist"], g["title"], g["how"]), want, title)

    def test_flagged_videos_still_parse(self):
        # the name is read; the video's flag comes from the search's BAD_WORDS
        self.assertEqual(A.video_flags("Nova Lane - Paper Lanterns (Lyric Video)"), ["lyric"])
        self.assertEqual(A.video_flags("Nova Lane - Paper Lanterns (Official Video)"), [])
        self.assertEqual(A.video_flags("Red Kites - Alive in Amber (Official Video)"), [])     # "live" isn't in "Alive"
        self.assertEqual(A.video_flags("Red Kites - Discover Me (Live Covers)"), ["live", "cover"])

    def test_channels(self):
        self.assertEqual(SL.channel_artist("Universal Music Group"), "")
        self.assertEqual(SL.channel_artist("Red Kites TV"), "Red Kites")
        self.assertEqual(SL.channel_artist("Nova Lane - Topic"), "Nova Lane")
        self.assertEqual(SL.channel_artist(""), "")


# ---------------- made-up network ----------------
def lrc(i, artist, title, dur=200.0, timed=True):
    lines = "\n".join(f"[{(5 + 4 * k) // 60:02d}:{(5 + 4 * k) % 60:02d}.00]la da na {k}" for k in range(12))
    return {"id": i, "artistName": artist, "trackName": title, "albumName": "Album", "duration": dur, "instrumental": False,
            "syncedLyrics": lines if timed else "", "plainLyrics": "\n".join(f"la da na {k}" for k in range(12))}


def yt(vid, title, channel, dur=200, views=10 ** 6):
    return {"id": vid, "title": title, "channel": channel, "duration": dur, "view_count": views, "channel_is_verified": True}


class Net:
    """The made-up internet: LRCLIB entries, YouTube videos, searches and playlists; calls are counted."""
    def __init__(self):
        self.lrc, self.videos, self.searches, self.playlists, self.calls, self.lrc_down = [], {}, {}, {}, [], set()

    def http_json(self, url, timeout=20, tries=3):
        self.calls.append(url)
        if "kara.moe" in url:
            return {"content": []}
        if "lrclib.net/api/search" in url:
            q = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
            text = A.loose(q.get("q") or f"{q.get('artist_name', '')} {q.get('track_name', '')}")
            if any(A.loose(x) in text for x in self.lrc_down):
                raise A.urllib.error.URLError("LRCLIB is down (test)")
            out = []
            for h in self.lrc:
                if "track_name" in q:
                    if A.loose(q["track_name"]) and A.loose(q["track_name"]) in A.loose(h["trackName"]) \
                            and (not q.get("artist_name") or A.loose(q["artist_name"]) in A.loose(h["artistName"])):
                        out.append(h)
                elif A.loose(re.sub(r"\s*[(\[][^)\]]*[)\]]", "", h["trackName"])) in text:   # (LRCLIB ignores "[1984]")
                    out.append(h)
            return out
        if "lrclib.net/api/get/" in url:
            return next(h for h in self.lrc if str(h["id"]) == url.rsplit("/", 1)[1])
        raise AssertionError(url)

    def yt_json(self, args, timeout=60):
        self.calls.append(" ".join(args))
        target = args[-1]
        if target.startswith("ytsearch10:"):
            q = A.loose(target[len("ytsearch10:"):])
            return {"entries": next((v for k, v in self.searches.items() if A.loose(k) in q), [])}
        if "playlist?list=" in target:
            got = self.playlists.get(target.rsplit("=", 1)[1])
            if got is None:
                raise RuntimeError("ERROR: The playlist does not exist")
            return got
        if "watch?v=" in target and "--no-playlist" in args:
            got = self.videos.get(target.rsplit("=", 1)[1])
            if got is None:
                raise RuntimeError("ERROR: Private video. Sign in if you've been granted access")
            return got
        raise AssertionError(args)


_n = [0]


def studio_song(artist, title, status="queued", published=False):
    """A song the studio already has (waiting to be made, or published in the library)."""
    _n[0] += 1
    jid = f"20260404-{_n[0]:06d}-imp-{_n[0]}"
    os.makedirs(os.path.join(A.JOBS, jid))
    job = {"id": jid, "yt": "zzzzzzzzzzz", "artist": artist, "title": title, "status": status, "step": status,
           "created": time.time(), "offset": 0.0, "log": []}
    if published:
        base = A.final_base(job)
        open(os.path.join(A.MV_DIR, base + ".mp4"), "wb").write(b"placeholder")
        job.update(published=time.time(), published_as=base)
    A.save(jid, job)
    return jid


REAL_SEARCH = next(r.endpoint for r in A.app.routes if getattr(r, "path", "") == "/api/search")
PERSON = {"X-authentik-username": "Mia"}


class Base(unittest.TestCase):
    def setUp(self):
        self.net = Net()
        self.saved = {k: getattr(A, k) for k in ("http_json", "yt_json", "search", "import_enqueue", "IMPORT_GAP_S")}
        A.http_json, A.yt_json, A.search = self.net.http_json, self.net.yt_json, REAL_SEARCH   # (another test module fakes search)
        A.import_enqueue = lambda iid: None   # the lookup runs inside the test (import_run), not in the thread
        A.IMPORT_GAP_S = 0
        self.mine = []

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(A, k, v)
        for j in os.listdir(A.JOBS):   # leave nothing queued for later tests
            if A.ID_RE.match(j):
                job = A.load(j)
                if job["status"] in ("queued", "running") and (job.get("import") or j.startswith("20260404-")):
                    A.set_job(j, status="failed", error="test")

    def song(self, artist, title, vid, dur=200, lyrics_id=None, timed=True, flags_title=None, channel=None):
        """A song the made-up internet knows: LRCLIB lyrics and an official video found by searching for it."""
        if lyrics_id:
            self.net.lrc.append(lrc(lyrics_id, artist, title, dur, timed))
        v = yt(vid, flags_title or f"{artist} - {title} (Official Video)", channel or artist, dur)
        self.net.searches[f"{artist} {title}"] = [v]
        return v

    def start(self, text, headers=PERSON):
        st, _, b = jcall(A.app, "POST", "/api/imports", {"text": text}, headers)
        self.assertEqual(st, 200, b)
        A.import_run(b["id"])
        st, _, d = jcall(A.app, "GET", f"/api/imports/{b['id']}")
        self.assertEqual(st, 200, d)
        return d


class LinkSearch(Base):
    def q(self, text):
        return jcall(A.app, "GET", "/api/search?q=" + urllib.parse.quote(text))

    def test_a_pasted_link_pins_its_video_and_names_the_song(self):
        self.net.lrc.append(lrc(501, "Nova Lane", "Paper Lanterns"))
        self.net.videos["NOVAlane001"] = yt("NOVAlane001", "NOVA LANE - PAPER LANTERNS (Official Music Video) [4K]", "Some Uploader", 204, 50)
        self.net.searches["Nova Lane Paper Lanterns"] = [yt("NOVAofficia", "Nova Lane - Paper Lanterns (Official Video)", "Nova Lane"),
                                                         yt("NOVAlane001", "dup", "Some Uploader")]
        st, _, d = self.q("https://www.youtube.com/watch?v=NOVAlane001&list=PLxyzxyzxyz12&t=30s")
        self.assertEqual(st, 200, d)
        self.assertEqual((d["artist"], d["title"]), ("Nova Lane", "Paper Lanterns"))   # LRCLIB's spelling
        self.assertEqual(d["pinned"], "NOVAlane001")
        self.assertEqual([v["id"] for v in d["videos"]], ["NOVAlane001", "NOVAofficia"])   # pinned first, not twice
        self.assertTrue(d["videos"][0]["link"])
        self.assertEqual(d["link"]["names_from"], "LRCLIB")
        self.assertEqual([l["id"] for l in d["lyrics"]], [501])
        self.assertTrue(any("--no-playlist" in c and "watch?v=NOVAlane001" in c for c in self.net.calls))

    def test_channel_names_the_artist_and_a_lyric_video_is_flagged(self):
        self.net.videos["IRISvale001"] = yt("IRISvale001", "Glass River (Lyric Video)", "Iris Vale - Topic", 180)
        st, _, d = self.q("https://youtu.be/IRISvale001")
        self.assertEqual(st, 200, d)
        self.assertEqual((d["artist"], d["title"]), ("Iris Vale", "Glass River"))
        self.assertEqual(d["videos"][0]["flags"], ["lyric"])

    def test_nothing_parses_says_so(self):
        self.net.videos["JAMjam00001"] = yt("JAMjam00001", "Untitled Jam Session", "Some Records", 180)
        st, _, d = self.q("https://www.youtube.com/shorts/JAMjam00001")
        self.assertEqual(st, 200, d)
        self.assertEqual((d["artist"], d["title"], d["link"]["parsed"]), ("", "Untitled Jam Session", False))
        self.assertIn("Couldn't tell the artist", d["notes"][0])

    def test_other_links(self):
        st, _, d = self.q("https://www.youtube.com/playlist?list=PLxyzxyzxyz12")
        self.assertEqual(st, 400)
        self.assertIn("Import a list", d["detail"])
        st, _, d = self.q("https://vimeo.com/12345")
        self.assertEqual(st, 400)
        self.assertIn("only YouTube", d["detail"])
        st, _, d = self.q("https://youtu.be/PRIVATE0001")
        self.assertEqual(st, 502)
        self.assertIn("couldn't read that video", d["detail"])

    def test_a_long_link_is_fine(self):
        self.net.videos["NOVAlane001"] = yt("NOVAlane001", "Nova Lane - Paper Lanterns", "Nova Lane")
        url = "https://www.youtube.com/watch?v=NOVAlane001&" + "&".join(f"x{i}=yyyyyyyyyy" for i in range(30))
        self.assertGreater(len(url), 200)
        self.assertEqual(self.q(url)[0], 200)


class ConfirmNames(Base):
    def test_lrclib_spelling_wins(self):
        self.net.lrc += [lrc(1, "Nova lane", "Paper Lanterns"), lrc(2, "Nova Lane", "Paper Lanterns"), lrc(3, "Nova Lane", "Paper lanterns")]
        self.assertEqual(A.confirm_names("NOVA LANE", "paper lanterns"), ("Nova Lane", "Paper Lanterns", "LRCLIB"))   # most uploads agree
        self.assertEqual(A.confirm_names("Paper Lanterns", "Nova Lane"), ("Nova Lane", "Paper Lanterns", "LRCLIB"))   # "Title - Artist"

    def test_brackets_collaborators_and_feats(self):
        self.net.lrc += [lrc(4, "Iris Vale", "Glass River [1984]"), lrc(5, "Red Kites", "Lowland"), lrc(6, "Red Kites", "Lowland"),
                         lrc(7, "Red Kites & Ada Moon", "Lowland"), lrc(8, "Bram", "Echo")]
        self.assertEqual(A.confirm_names("Iris Vale", "Glass River"), ("Iris Vale", "Glass River", "LRCLIB"))
        self.assertEqual(A.confirm_names("Red Kites and Ada Moon", "Lowland"), ("Red Kites & Ada Moon", "Lowland", "LRCLIB"))
        self.assertEqual(A.confirm_names("Red Kites", "Lowland"), ("Red Kites", "Lowland", "LRCLIB"))
        self.assertEqual(A.confirm_names("Bram & Ada Moon", "Echo", ["Ada Moon"]), ("Bram & Ada Moon", "Echo", "LRCLIB"))

    def test_nothing_known_or_no_artist(self):
        self.assertEqual(A.confirm_names("Kit Rowe", "Unknown Song"), ("Kit Rowe", "Unknown Song", None))
        self.net.lrc.append(lrc(9, "Nova Lane", "Paper Lanterns"))
        self.assertEqual(A.confirm_names("", "Nova Lane Paper Lanterns"), ("Nova Lane", "Paper Lanterns", "the search"))
        self.assertEqual(A.confirm_names("", "Untitled Jam"), ("", "Untitled Jam", None))


class Lookup(Base):
    def test_a_pasted_list_ready_and_every_skip_reason(self):
        self.song("Nova Lane", "Paper Lanterns", "NOVAvideo01", lyrics_id=601)
        self.song("Iris Vale", "Quiet Song", "IRISvideo01")                       # no lyrics anywhere
        self.song("Bram", "Long Song", "BRAMvideo01", dur=11 * 60, lyrics_id=602)
        self.song("Lumen Coast", "Glass River", "LUMENvideo1", lyrics_id=603)
        studio_song("Lumen Coast", "Glass River", published=True)
        self.song("Red Kites", "Lowland", "REDvideo001", lyrics_id=604)
        studio_song("Red Kites", "Lowland", status="queued")
        self.song("Mika Sol", "Neon Harbor", "MIKAvideo01", lyrics_id=605, flags_title="Mika Sol - Neon Harbor (Lyric Video)",
                  channel="Some Lyrics Channel")
        self.song("Ada Moon", "Kawa", "ADAvideo001", lyrics_id=606, flags_title="Ada Moon - Kawa (2001 Version) (Official Video)")
        self.net.lrc_down.add("Ada Moon Kawa")
        self.net.videos["JUNEarbor01"] = yt("JUNEarbor01", "June Arbor - Copper Moon (Official Video)", "June Arbor", 230)
        self.net.lrc.append(lrc(607, "June Arbor", "Copper Moon", 231))
        text = ("1. Nova Lane - Paper Lanterns\n2. Iris Vale - Quiet Song\n3. Bram - Long Song\n4. Lumen Coast - Glass River\n"
                "5. Red Kites - Lowland\n6. Mika Sol - Neon Harbor\n7. nova lane - paper lanterns\n8. Ada Moon - Kawa\n"
                "https://youtu.be/JUNEarbor01?t=1\nhttps://youtu.be/PRIVATE0001\nhttps://vimeo.com/1\n")
        d = self.start(text)
        self.assertEqual((d["state"], d["total"], d["waiting"]), ("ready", 11, 0))
        rows = {r["n"]: r for r in d["rows"]}
        why = lambda n: rows[n]["why"]
        self.assertEqual((rows[1]["status"], rows[1]["tick"], rows[1]["video"]["id"], rows[1]["lyrics"]["id"]), ("ready", True, "NOVAvideo01", 601))
        self.assertEqual((rows[1]["artist"], rows[1]["title"], rows[1]["lyrics"]["lang"]), ("Nova Lane", "Paper Lanterns", "Latin letters"))
        self.assertEqual(why(2), "no timed lyrics found")
        self.assertFalse(rows[2]["can_make"])
        self.assertIn("too long (11:00", why(3))
        self.assertTrue(rows[3]["can_make"] and not rows[3]["tick"])   # skipped, but the person may tick it
        self.assertEqual(why(4), "already in the library")
        self.assertEqual(why(5), "already in the make queue")
        self.assertIn("looks like a lyric video", why(6))
        self.assertEqual(why(7), "the same song as row 1")
        self.assertIn("LRCLIB didn't answer", why(8))
        self.assertIn("2001 Version", rows[8]["notes"][0])   # a re-recording? said, not skipped
        self.assertEqual((rows[9]["status"], rows[9]["video"]["id"], rows[9]["artist"], rows[9]["title"]),
                         ("ready", "JUNEarbor01", "June Arbor", "Copper Moon"))
        self.assertIn("couldn't read that video", why(10))
        self.assertEqual(why(11), "not a YouTube video link")
        self.assertEqual([r["n"] for r in d["rows"] if r["status"] == "ready"], [1, 9])
        self.assertFalse(any("first" in (r.get("lyrics") or {}) for r in d["rows"]))   # no lyrics text kept

    def test_a_playlist_keeps_its_own_videos(self):
        for i, (a, t) in enumerate((("Nova Lane", "Paper Lanterns"), ("Iris Vale", "Glass River"), ("Bram", "Echo"))):
            self.net.lrc.append(lrc(700 + i, a, t))
            self.net.searches[f"{a} {t}"] = [yt(f"OTHERvid0{i}x", f"{a} - {t} (Official Video)", a)]   # what a search would pick
        self.net.playlists["PLparty00001"] = {"title": "Party night", "playlist_count": 64, "entries": [
            {"id": "PLAYvid0001", "title": "Nova Lane - Paper Lanterns (Official Video)", "channel": "Nova Lane", "duration": 201},
            {"id": "PLAYvid0002", "title": "Iris Vale - Glass River (Live at the Hall)", "channel": "Iris Vale", "duration": 205},
            {"id": "PLAYvid0003", "title": None, "channel": None, "duration": None},                 # a private video
            {"id": "PLAYvid0004", "title": "Bram - Echo (Lyrics)", "channel": "Lyrics World", "duration": 199},
            {"id": "PLAYvid0005", "title": "Nova Lane - Paper Lanterns (Lyric Video)", "channel": "x", "duration": 200}]}
        d = self.start("https://www.youtube.com/playlist?list=PLparty00001")
        self.assertEqual((d["kind"], d["playlist"]["title"], d["left_out"]), ("playlist", "Party night", 59))
        rows = d["rows"]
        self.assertEqual([r["video"]["id"] for r in rows], [f"PLAYvid000{i}" for i in range(1, 6)])
        self.assertEqual([r["status"] for r in rows], ["ready", "ready", "skipped", "ready", "skipped"])
        self.assertIn("live", rows[1]["notes"][0])                 # flagged, not skipped: the person chose it
        self.assertEqual(rows[1]["title"], "Glass River")
        self.assertIn("unavailable", rows[2]["why"])
        self.assertEqual(rows[4]["why"], "the same song as row 1")
        self.assertFalse(any(c.startswith("ytsearch") or "ytsearch10:" in c for c in self.net.calls))   # no searching for videos
        self.assertTrue(any("--flat-playlist" in c and "--playlist-end 2000" in c for c in self.net.calls))

    def test_a_playlist_that_cant_be_read(self):
        d = self.start("https://www.youtube.com/playlist?list=PLnothere001")
        self.assertEqual((d["state"], d["total"]), ("ready", 0))
        self.assertIn("couldn't read the playlist", d["error"])

    def test_kept_on_disk_listed_and_expired(self):
        self.song("Nova Lane", "Paper Lanterns", "NOVAvideo01", lyrics_id=601)
        d = self.start("Nova Lane - Paper Lanterns")
        path = os.path.join(C.DATA, "imports", d["id"] + ".json")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(json.load(open(path))["rows"][0]["status"], "ready")   # (a page reload reads it back)
        st, _, lst = jcall(A.app, "GET", "/api/imports")
        self.assertIn(d["id"], [i["id"] for i in lst["imports"]])
        self.assertEqual(next(i for i in lst["imports"] if i["id"] == d["id"])["by"]["by"], "Mia")
        imp = json.load(open(path))
        imp["created"] -= A.IMPORT_KEEP_S + 5
        json.dump(imp, open(path, "w"))
        self.assertEqual(jcall(A.app, "GET", f"/api/imports/{d['id']}")[0], 404)
        jcall(A.app, "GET", "/api/imports")
        self.assertFalse(os.path.exists(path))

    def test_a_lookup_interrupted_by_a_restart_carries_on(self):
        self.song("Nova Lane", "Paper Lanterns", "NOVAvideo01", lyrics_id=601)
        st, _, b = jcall(A.app, "POST", "/api/imports", {"text": "Nova Lane - Paper Lanterns"}, PERSON)
        queued = []
        A.import_enqueue = queued.append
        A.recover_imports()
        self.assertIn(b["id"], queued)

    def test_empty_and_cancelled(self):
        self.assertEqual(jcall(A.app, "POST", "/api/imports", {"text": "  \n "})[0], 400)
        self.assertEqual(jcall(A.app, "POST", "/api/imports", {"text": "\n1. \n"})[0], 400)
        st, _, b = jcall(A.app, "POST", "/api/imports", {"text": "A - B\nC - D"}, PERSON)
        jcall(A.app, "POST", f"/api/imports/{b['id']}/cancel", {})
        A.import_run(b["id"])   # a cancelled lookup stops
        d = jcall(A.app, "GET", f"/api/imports/{b['id']}")[2]
        self.assertEqual((d["state"], d["waiting"]), ("cancelled", 2))
        self.assertEqual(jcall(A.app, "POST", f"/api/imports/{b['id']}/make", {"rows": [{"n": 1}]})[0], 409)


def made_tick(r, n):
    return next(x for x in r["import"]["rows"] if x["n"] == n)["tick"]


class MakeTicked(Base):
    def test_only_ticked_rows_in_order_with_the_tag_once(self):
        self.song("Nova Lane", "Paper Lanterns", "NOVAvideo01", lyrics_id=601)
        self.song("Bram", "Long Song", "BRAMvideo01", dur=11 * 60, lyrics_id=602)
        self.song("Iris Vale", "Glass River", "IRISvideo01", lyrics_id=603)
        self.song("Iris Vale", "Quiet Song", "IRISvideo02")
        d = self.start("Bram - Long Song\nNova Lane - Paper Lanterns\nIris Vale - Glass River\nIris Vale - Quiet Song")
        iid = d["id"]
        body = {"rows": [{"n": 2, "artist": "Nova Lane", "title": "Paper Lanterns (Remix Night)"}, {"n": 1}], "gpu": "bogus"}
        st, _, r = jcall(A.app, "POST", f"/api/imports/{iid}/make", body, PERSON)
        self.assertEqual(st, 200, r)
        self.assertEqual([m["n"] for m in r["made"]], [1, 2])      # list order, not the order sent
        jobs = [A.load(m["id"]) for m in r["made"]]
        self.assertEqual([(j["yt"], j["lrc_id"], j["lyrics_source"]) for j in jobs], [("BRAMvideo01", 602, "lrclib"), ("NOVAvideo01", 601, "lrclib")])
        self.assertEqual([j["import"] for j in jobs], [{"id": iid, "n": 1}, {"id": iid, "n": 2}])
        self.assertEqual(jobs[1]["title"], "Paper Lanterns (Remix Night)")   # the person's edit
        self.assertEqual({(j["made_by"]["by"], j["made_by"]["kind"], j["gpu_pref"], j["status"]) for j in jobs}, {("Mia", "person", "auto", "queued")})
        self.assertLessEqual(jobs[0]["created"], jobs[1]["created"])
        self.assertFalse(made_tick(r, 4) or made_tick(r, 3))   # rows not sent stay unticked (a reload shows them so)
        self.assertFalse(any(j.get("auto_publish") or j.get("automake") for j in jobs))
        made = {x["n"]: x for x in r["import"]["rows"]}
        self.assertEqual((made[1]["status"], made[2]["job"], made[3]["status"]), ("made", r["made"][1]["id"], "ready"))
        before = len(os.listdir(A.JOBS))
        st, _, again = jcall(A.app, "POST", f"/api/imports/{iid}/make", body, PERSON)   # a second press
        self.assertEqual((st, again["made"], len(os.listdir(A.JOBS))), (200, [], before))
        st, _, bad = jcall(A.app, "POST", f"/api/imports/{iid}/make", {"rows": [{"n": 4}]}, PERSON)
        self.assertEqual(st, 400)
        self.assertIn("no video or timed lyrics", bad["detail"])
        self.assertEqual(jcall(A.app, "POST", f"/api/imports/{iid}/make", {"rows": [{"n": 3, "artist": " "}]}, PERSON)[0], 400)
        listed = {j["id"]: j for j in A.list_jobs(brief=1)}
        self.assertEqual(listed[r["made"][0]["id"]]["import"], {"id": iid, "n": 1})   # the Songs list can show where it came from

    def test_not_while_looking(self):
        st, _, b = jcall(A.app, "POST", "/api/imports", {"text": "A - B"}, PERSON)
        st, _, r = jcall(A.app, "POST", f"/api/imports/{b['id']}/make", {"rows": [{"n": 1}]}, PERSON)
        self.assertEqual(st, 409)

    def test_change_then_make_by_hand_takes_the_rows_place(self):
        self.song("Iris Vale", "Quiet Song", "IRISvideo02")
        d = self.start("Iris Vale - Quiet Song")
        st, _, r = jcall(A.app, "POST", "/api/jobs", {"yt": "HANDpicked1", "lrc_id": 999, "lyrics_source": "lrclib", "artist": "Iris Vale",
                                                     "title": "Quiet Song", "import_row": {"id": d["id"], "n": 1}}, PERSON)
        self.assertEqual(st, 200, r)
        self.assertEqual(A.load(r["id"])["import"], {"id": d["id"], "n": 1})
        row = jcall(A.app, "GET", f"/api/imports/{d['id']}")[2]["rows"][0]
        self.assertEqual((row["status"], row["job"], row["how"], row["can_make"]), ("made", r["id"], "by hand", False))


class WhoMay(Base):
    def test_agents_need_the_make_scope(self):
        key, _ = A.KEYS.create("Import reader", ["read"])
        h = {"X-Studio-Agent-Key": key}
        st, _, b = jcall(A.app, "POST", "/api/imports", {"text": "A - B"}, h)
        self.assertEqual((st, b["scope"]), (403, "make"))
        self.assertEqual(jcall(A.app, "GET", "/api/imports", headers=h)[0], 200)   # reading is "read"
        key2, _ = A.KEYS.create("Import maker", ["read", "make"])
        st, _, b = jcall(A.app, "POST", "/api/imports", {"text": "A - B"}, {"X-Studio-Agent-Key": key2})
        self.assertEqual(st, 200, b)
        self.assertEqual(json.load(open(os.path.join(C.DATA, "imports", b["id"] + ".json")))["by"], {"by": "Import maker", "kind": "agent", "at": b["by"]["at"]})
        st, _, r = jcall(A.app, "POST", f"/api/imports/{b['id']}/make", {"rows": [{"n": 1}]}, h)
        self.assertEqual(st, 403)

    def test_players_cant_import(self):
        token = os.environ["ENCORE_TOKEN"]
        st, _, b = jcall(A.app, "POST", "/api/encore/imports", {"text": "A - B"}, {"Authorization": "Bearer " + token})
        self.assertEqual(st, 404)                               # the player API has no import
        A._ONLY_NETS[:] = [ipaddress.ip_network("192.0.2.0/24")]   # a players-only network (ENCORE_ONLY_NETS)
        try:
            st, _, b = jcall(A.app, "POST", "/api/imports", {"text": "A - B"}, {"Authorization": "Bearer " + token})
            self.assertEqual(st, 403)
        finally:
            A._ONLY_NETS[:] = []

    def test_a_login_or_key_when_required(self):
        C.AGENT_KEYS_REQUIRED = True
        try:
            self.assertEqual(jcall(A.app, "POST", "/api/imports", {"text": "A - B"})[0], 401)
            self.assertEqual(jcall(A.app, "POST", "/api/imports", {"text": "A - B"}, PERSON)[0], 200)
        finally:
            C.AGENT_KEYS_REQUIRED = False


if __name__ == "__main__":
    unittest.main()
