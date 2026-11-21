"""Library tab: which old songs already have a music video, which were made in the studio but not published, and the
"Remove old version" guard. Names are real-world shapes of PiKaraoke file names."""
import asyncio, json, os, time, unittest

from tests import _env  # noqa: F401
import app as A  # noqa: E402
from fastapi import HTTPException  # noqa: E402

LIB = A.LIB
MVD = A.MV_DIR
OLD = {  # old YouTube download -> the studio music video of the same song (None: there isn't one)
    "Billy Ocean - Caribbean Queen (No More Love On The Run) (Karaoke Version)---L5OsNVY5gBI.mp4": "Billy Ocean - Caribbean Queen (No More Love On the Run) (Music Video)",
    "Britney Spears - Baby One More Time - Karaoke Version from Zoom Karaoke---dyii_7HSLpE.mp4": "Britney Spears - Baby One More Time (Music Video)",
    "Eslabon Armado, Peso Pluma - Ella Baila Sola (Karaoke Version)---gk0OC3Zw4pg.mp4": "Eslabón Armado, Peso Pluma - Ella Baila Sola (Music Video)",
    "Hold Me, Thrill Me, Kiss Me, Kill Me (Karaoke) - U2---goL5gbRAqD0.mp4": "U2 - Hold Me, Thrill Me, Kiss Me, Kill Me (Music Video)",
    "I Don't wanna Know - Mario Winans Karaoke---lAh06bschSM.mp4": "Mario Winans - I Don't Wanna Know (Music Video)",
    "NSYNC - Bye Bye Bye (Karaoke Version)---4DwB0wtMIy4.mp4": "NSYNC - Bye Bye Bye (Music Video)",
    "Spice Girls - Wannabe (Karaoke Version)---BTDPZQGqjY8.mp4": "Spice Girls - Wannabe (Music Video)",
    "Ed Sheeran - Sing Karaoke---aaaaaaaaaaa.mp4": None,           # "Sing" is the title, not karaoke words
    "Alicia Keys - No One (Karaoke Version)---bbbbbbbbbbb.mp4": None,
    "Made Artist - Made Song (Karaoke Version)---ccccccccccc.mp4": None,   # made in the studio, not published
    "Queued Artist - Queued Song (Karaoke)---ddddddddddd.mp4": None,
    "Making Artist - Making Song - Sing King Karaoke---eeeeeeeeeee.mp4": None,
    "Failed Song (Karaoke) - Failed Artist---fffffffffff.mp4": None,
    "Cancelled Artist - Cancelled Song (Karaoke)---ggggggggggg.mp4": None,
    "Lionel Richie - Hello (Karaoke Version)---hhhhhhhhhhh.mp4": "Adele - Hello (Music Video)",  # title only: shown, not removable
}
n = [0]


def job(artist, title, status, **kw):
    n[0] += 1
    jid = f"20260101-0000{n[0]:02d}-lib-{n[0]}"
    os.makedirs(f"{A.JOBS}/{jid}", exist_ok=True)
    A.save(jid, {"id": jid, "artist": artist, "title": title, "status": status, "created": time.time() + n[0], "log": [], **kw})
    return jid


class Library(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name, mv in OLD.items():
            open(os.path.join(LIB, name), "wb").write(b"x")
            if mv:
                open(os.path.join(MVD, mv + ".mp4"), "wb").write(b"x")
        cls.pub = job("Spice Girls", "Wannabe", "done", published=1.0, published_as="Spice Girls - Wannabe (Music Video)")
        cls.made = job("Made Artist", "Made Song", "done", finished=1.0)
        cls.made_old = job("Made Artist", "Made Song", "failed")
        cls.queued = job("Queued Artist", "Queued Song", "queued")
        cls.making = job("Making Artist", "Making Song", "running")
        cls.failed = job("Failed Artist", "Failed Song", "failed")
        cls.cancelled = job("Cancelled Artist", "Cancelled Song", "cancelled")
        cls.other = job("Someone Else", "No One", "done")  # same title as Alicia Keys' old file: must not take its row

    def songs(self):
        return {s["file"]: s for s in A.library_songs()}

    def test_has_mv_both_orders_and_accents(self):
        s = self.songs()
        for name, mv in OLD.items():
            with self.subTest(name=name):
                self.assertEqual(s[name]["has_mv"], bool(mv))
                if mv:
                    self.assertEqual(s[name]["mv_title"], mv)
        self.assertEqual(s["Music Videos/Spice Girls - Wannabe (Music Video).mp4"]["job"]["id"], self.pub)
        self.assertEqual(s["Spice Girls - Wannabe (Karaoke Version)---BTDPZQGqjY8.mp4"]["mv_job"], self.pub)

    def test_a_published_file_whose_job_is_gone_finds_its_remake(self):
        open(os.path.join(MVD, "Remade Band - Remade Song (Music Video).mp4"), "wb").write(b"x")
        self.addCleanup(os.remove, os.path.join(MVD, "Remade Band - Remade Song (Music Video).mp4"))
        old = job("Remade Band", "Remade Song", "done", finished=1.0)                 # (not the one that published it)
        new = job("Remade Band", "Remade Song", "done", finished=2.0)
        job("Remade Band", "Another Song", "done")
        got = self.songs()["Music Videos/Remade Band - Remade Song (Music Video).mp4"]["job"]
        self.assertEqual(got["id"], new)                                              # the newest of that artist and title
        self.assertNotEqual(got["id"], old)
        open(os.path.join(MVD, "Nobody Made - This (Music Video).mp4"), "wb").write(b"x")
        self.addCleanup(os.remove, os.path.join(MVD, "Nobody Made - This (Music Video).mp4"))
        self.assertIsNone(self.songs()["Music Videos/Nobody Made - This (Music Video).mp4"]["job"])

    def test_queries(self):
        s = self.songs()
        self.assertEqual(s["Hold Me, Thrill Me, Kiss Me, Kill Me (Karaoke) - U2---goL5gbRAqD0.mp4"]["query"], "Hold Me, Thrill Me, Kiss Me, Kill Me - U2")
        self.assertEqual(s["I Don't wanna Know - Mario Winans Karaoke---lAh06bschSM.mp4"]["query"], "I Don't wanna Know - Mario Winans")
        self.assertEqual(s["Britney Spears - Baby One More Time - Karaoke Version from Zoom Karaoke---dyii_7HSLpE.mp4"]["query"], "Britney Spears - Baby One More Time")
        self.assertEqual(s["Ed Sheeran - Sing Karaoke---aaaaaaaaaaa.mp4"]["query"], "Ed Sheeran - Sing")
        self.assertEqual(s["Making Artist - Making Song - Sing King Karaoke---eeeeeeeeeee.mp4"]["query"], "Making Artist - Making Song")

    def test_studio_jobs(self):
        s = self.songs()
        j = lambda name: s[name]["job"]
        self.assertEqual(j("Made Artist - Made Song (Karaoke Version)---ccccccccccc.mp4")["id"], self.made)  # done beats failed
        self.assertEqual(j("Made Artist - Made Song (Karaoke Version)---ccccccccccc.mp4")["status"], "done")
        self.assertEqual(j("Queued Artist - Queued Song (Karaoke)---ddddddddddd.mp4")["status"], "queued")
        self.assertEqual(j("Making Artist - Making Song - Sing King Karaoke---eeeeeeeeeee.mp4")["status"], "running")
        self.assertEqual(j("Failed Song (Karaoke) - Failed Artist---fffffffffff.mp4")["status"], "failed")
        self.assertIsNone(j("Cancelled Artist - Cancelled Song (Karaoke)---ggggggggggg.mp4"))  # cancelled: Make again
        self.assertIsNone(j("Alicia Keys - No One (Karaoke Version)---bbbbbbbbbbb.mp4"))  # only another artist's "No One"

    def test_removable_and_remove(self):
        s = self.songs()
        self.assertTrue(s["Hold Me, Thrill Me, Kiss Me, Kill Me (Karaoke) - U2---goL5gbRAqD0.mp4"]["removable"])
        self.assertFalse(s["Lionel Richie - Hello (Karaoke Version)---hhhhhhhhhhh.mp4"]["removable"])  # a different artist
        self.assertFalse(s["Alicia Keys - No One (Karaoke Version)---bbbbbbbbbbb.mp4"]["removable"])
        for bad in ("Lionel Richie - Hello (Karaoke Version)---hhhhhhhhhhh.mp4", "Music Videos/NSYNC - Bye Bye Bye (Music Video).mp4",
                    "../outside---aaaaaaaaaaa.mp4"):
            with self.assertRaises(HTTPException):
                asyncio.run(A.remove_old(_env.Req({"file": bad})))
        name = "NSYNC - Bye Bye Bye (Karaoke Version)---4DwB0wtMIy4.mp4"
        open(os.path.join(LIB, name.replace(".mp4", ".ass")), "w").write("x")
        r = asyncio.run(A.remove_old(_env.Req({"file": name})))
        self.assertTrue(r["ok"])
        self.assertFalse(os.path.exists(os.path.join(LIB, name)))
        self.assertFalse(os.path.exists(os.path.join(LIB, name.replace(".mp4", ".ass"))))
        self.assertTrue(os.path.exists(os.path.join(MVD, "NSYNC - Bye Bye Bye (Music Video).mp4")))

    def test_old_versions_on_publish(self):
        self.assertEqual(A.old_versions({"artist": "U2", "title": "Hold Me, Thrill Me, Kiss Me, Kill Me"}),
                         ["Hold Me, Thrill Me, Kiss Me, Kill Me (Karaoke) - U2---goL5gbRAqD0.mp4"])
        self.assertEqual(A.old_versions({"artist": "Adele", "title": "Hello"}), [])

    def test_jobs_like(self):
        like = A.jobs_like("made artist", "MADE SONG")
        self.assertEqual([j["id"] for j in like], [self.made])  # failed ones aren't "already made"
        self.assertEqual(A.jobs_like("Made Artist", "Made Song", exclude=self.made), [])
        self.assertEqual([j["id"] for j in A.jobs_like("Eslabon", "Queued Song")], [])
        self.assertEqual([j["id"] for j in A.jobs_like("Queued Artist", "Queued Song")], [self.queued])

    def test_brief_list(self):
        full, brief = A.list_jobs(), A.list_jobs(brief=1)
        self.assertEqual([j["id"] for j in full], [j["id"] for j in brief])
        self.assertLess(len(json.dumps(brief)), len(json.dumps(full)))


if __name__ == "__main__":
    unittest.main()
