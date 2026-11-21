"""autopick.py, the Python copy of the Make page's choosing: what it picks on made-up searches, and that it picks
exactly what the page's own JavaScript (cut out of static/index.html and run in node) picks, on hand-made edge cases
and a few hundred random searches. No network; made-up names only."""
import random, unittest

from tests import _env  # noqa: F401
from tests import _jsoracle as O
import autopick as P


def vid(i, title, dur, channel="Some Channel", official=False, flags=(), score=1.0):
    return {"id": f"v{i:010d}"[:11], "title": title, "channel": channel, "duration": dur, "views": 1000, "verified": official,
            "official_channel": official, "flags": list(flags), "score": score, "thumb": ""}


def lyr(i, artist, title, dur, timed=True, plain=True, native=False, japanese=False, latin=True, english=False,
        name_match=True, source="lrclib", lines=40, mismatch=None, artist_match=True):
    d = {"source": source, "id": i if source == "lrclib" else f"{i:08d}-0000-4000-8000-000000000000", "artist": artist,
         "title": title, "album": "", "duration": dur, "timed": timed, "plain": plain, "latin": latin, "japanese": japanese,
         "native": native, "english": english, "lines": lines, "first": "", "name_match": name_match, "artist_match": artist_match}
    if mismatch:
        d["mismatch"] = True
    return d


def search(artist, title, videos, lyrics):
    return {"artist": artist, "title": title, "videos": videos, "lyrics": lyrics, "notes": []}


EDGE = [
    # the artist's channel with a fitting length beats a better-scored one that doesn't fit
    search("Nova Lane", "Paper Lanterns",
           [vid(1, "Nova Lane - Paper Lanterns (Official Video)", 300, official=True, score=9),
            vid(2, "Nova Lane - Paper Lanterns", 241, official=True, score=7), vid(3, "Paper Lanterns lyric", 240, flags=["lyric"])],
           [lyr(10, "Nova Lane", "Paper Lanterns", 240.5), lyr(11, "Nova Lane", "Paper Lanterns", 239.0)]),
    # only flagged videos: the first one
    search("A", "B", [vid(4, "A - B (Live)", 200, flags=["live"]), vid(5, "A - B (cover)", 210, flags=["cover"])],
           [lyr(12, "A", "B", 200)]),
    # a Japanese song: the original script first, kara.moe's romaji only as a fallback
    search("June Arbor", "Copper Moon",
           [vid(6, "June Arbor「銅の月」Music Video", 250, official=True)],
           [lyr(13, "June Arbor", "Copper Moon", 249, source="karamoe", lines=None),
            lyr(14, "June Arbor", "銅の月", 251, native=True, japanese=True, latin=False),
            lyr(15, "June Arbor", "Copper Moon", 250, english=True)]),
    # not Japanese: kara.moe near the length first; an English release under a Japanese-titled video comes last
    search("Mika Sol", "Neon Harbor",
           [vid(7, "Mika Sol『ネオン港』", 200, official=True)],
           [lyr(16, "Mika Sol", "Neon Harbor", 200, english=True), lyr(17, "Mika Sol", "Neon Harbor", 205),
            lyr(18, "Mika Sol", "Neon Harbor", 201, source="karamoe", lines=None)]),
    # untimed and mismatched lyrics are skipped; nothing usable: no lyrics
    search("X", "Y", [vid(8, "X - Y", 180)], [lyr(19, "X", "Y", 180, timed=False, plain=False), lyr(20, "X", "Y", 180, mismatch=True)]),
    search("X", "Y", [], []),
    # names: junk, a doubled upload title, the artist inside the title, SHOUTED words, a romaji title kept
    search("the quiet arcade", "two small rivers", [vid(9, "THE QUIET ARCADE - TWO SMALL RIVERS (Official Music Video)", 200)],
           [lyr(21, "The Quiet Arcade", "The Quiet Arcade - Two Small Rivers (2011 Remaster)", 200)]),
    search("", "glass orchard", [vid(10, "LUMEN COAST - GLASS ORCHARD | Live at Home", 200)], []),
    search("Ada", "Kawa no Uta", [vid(11, "Ada 「川の歌(River Song)」 M/V", 200)], [lyr(22, "Ada", "川の歌", 200, native=True, latin=False)]),
    search("Bram", "Echo", [vid(12, "Bram - Echo", 150)], [lyr(23, "Bram", "Echo;Echo", 150), lyr(24, "Bram", "Echo", 151, lines=10)]),
    search("Iris Vale", "paper boats", [vid(13, "Paper Boats", 90)], [lyr(25, "IRIS VALE", "PAPER BOATS [Official Audio]", 90, plain=False)]),
    search("Iris Vale", "paper boats", [vid(14, "a video", 0)], [lyr(26, "Iris Vale", "Paper Boats", None, lines=12),
                                                                   lyr(27, "Iris Vale", "Paper Boats", 100, lines=30)]),
    search("Ço", "Ünïcode Ťitle", [vid(15, "Ço - Ünïcode Ťitle - 2020 Remastered", 100)], []),
]

WORDS = ["Nova", "Lane", "Paper", "Lanterns", "ECHO", "river", "Moon", "copper", "月", "川", "の", "歌", "별", "Ünï", "x"]
JUNK = ["", " (Official Music Video)", " [Lyric Video]", " (HD)", " - 2009 Remaster", " M/V", " | Live", ";", " / ", "「", "」",
        " (feat. Someone)", " official video", "  "]


def rand_text(rng, n=3):
    t = " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, n)))
    if rng.random() < 0.3:
        t = t.upper()
    return t + rng.choice(JUNK)


def rand_search(rng):
    a, t = rand_text(rng, 2), rand_text(rng, 3)
    vids = []
    for i in range(rng.randint(0, 6)):
        title = rng.choice([f"{a} - {t}", f"{a} 「{t}」 MV", t, f'{a} "{t}" M/V', rand_text(rng, 4)])
        vids.append(vid(rng.randint(0, 10 ** 9), title, rng.choice([0, 120, 180, 181, 200, 240, 300]), official=rng.random() < 0.5,
                        flags=rng.choice([[], [], ["live"], ["lyric"]]), score=round(rng.uniform(-4, 12), 2)))
    lyrs = []
    for i in range(rng.randint(0, 8)):
        native = rng.random() < 0.3
        lyrs.append(lyr(rng.randint(1, 10 ** 8), rng.choice([a, rand_text(rng, 2), ""]), rng.choice([t, rand_text(rng, 3), f"{a} - {t}"]),
                        rng.choice([None, 0, 120, 175, 180, 186, 193, 200, 238, 241.7, 300]), timed=rng.random() < 0.8,
                        plain=rng.random() < 0.7, native=native, japanese=native and rng.random() < 0.5, latin=not native,
                        english=rng.random() < 0.3, name_match=rng.random() < 0.7, source=rng.choice(["lrclib"] * 4 + ["karamoe"]),
                        lines=rng.choice([None, 10, 40, 41]), mismatch=rng.random() < 0.1))
    return search(rng.choice([a, "", a.lower()]), rng.choice([t, t.lower(), " " + t + " "]), vids, lyrs)


class AutoPick(unittest.TestCase):
    def test_choices(self):
        p = [P.choose(d) for d in EDGE]
        self.assertEqual(p[0]["video"]["id"], EDGE[0]["videos"][1]["id"])     # the fitting official video
        self.assertEqual(p[0]["lyrics"]["id"], 10)                             # the closest length
        self.assertEqual(p[1]["video"]["id"], EDGE[1]["videos"][0]["id"])
        self.assertEqual(p[2]["lyrics"]["id"], 14)                             # original script for a Japanese song
        self.assertEqual(p[3]["lyrics"]["source"], "karamoe")
        self.assertIsNone(p[4]["lyrics"])
        self.assertEqual((p[5]["video"], p[5]["lyrics"], p[5]["names_from"]), (None, None, "your search"))
        self.assertEqual((p[6]["artist"], p[6]["title"], p[6]["names_from"]), ("The Quiet Arcade", "Two Small Rivers", "the lyrics"))
        self.assertEqual((p[7]["artist"], p[7]["title"], p[7]["names_from"]), ("Lumen Coast", "Glass Orchard", "the video's title"))
        self.assertEqual(p[8]["title"], "Kawa no Uta")                         # the romaji typed beats the native title
        self.assertEqual(p[9]["title"], "Echo")                                # "Echo;Echo"
        self.assertEqual((p[10]["artist"], p[10]["title"]), ("Iris Vale", "Paper Boats"))
        self.assertEqual(P.tidy_name("Ço - Ünïcode Ťitle - 2020 Remastered"), "Ço - Ünïcode Ťitle")

    def test_same_as_the_browser(self):
        if not O.node():
            self.skipTest("no node here (the Studio's image has it)")
        rng = random.Random(20261120)
        cases = EDGE + [rand_search(rng) for _ in range(600)]
        js, py = O.picks(cases), O.py_picks(cases)
        bad = [(i, a, b) for i, (a, b) in enumerate(zip(py, js)) if a != b]
        self.assertEqual(bad[:5], [], f"{len(bad)} of {len(cases)} picks differ from the browser's")


if __name__ == "__main__":
    unittest.main()
