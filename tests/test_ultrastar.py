"""UltraStar export (ultrastar.py): song.txt as UltraStar Deluxe reads it, from the Studio's words and melody, and the
same rules checked over every song in a real-looking library."""
import json, os, re, shutil, tempfile, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
import ultrastar as U  # noqa: E402

ENTRY = {"artist": "Nova Rae", "title": "Glass Sky", "title_orig": "玻璃天空", "lang": "en", "year": 2024, "genre": "Pop",
         "base": "Nova Rae - Glass Sky (Music Video)", "folder": "Music Videos"}
LINES = [{"words": [[1.00, 1.40, "Hello"], [1.50, 2.32, "sky"]]},
         {"backing": True, "words": [[1.60, 2.00, "(ooh)"]]},
         {"words": [[3.00, 3.40, "come"], [3.40, 3.80, "home"]]}]
NOTES = [[1.00, 1.40, 60.2], [1.50, 1.90, 62.0], [1.90, 2.32, 64.4], [3.00, 3.40, 59.6]]   # "home": no note


def check_rules(txt):
    """USDX's reading rules: headers first, notes in order without overlap, line breaks between, E at the end."""
    lines = txt.rstrip("\n").split("\n")
    assert lines[-1] == "E", lines[-1]
    heads = [l for l in lines if l.startswith("#")]
    assert all(lines.index(h) < len(heads) for h in heads)        # every header before the first note
    assert {"#BPM:375", "#GAP:0"} <= set(heads) and any(h.startswith("#TITLE:") for h in heads)
    end = -1
    for l in lines[len(heads):-1]:
        assert l.strip(), "empty lines break loading"
        if l.startswith("- "):
            b = int(l[2:])
            assert b >= end, (l, end)
            continue
        m = re.match(r"^([:*FRG]) (\d+) (\d+) (-?\d+) (.+)$", l)
        assert m, l
        start, length, pitch = int(m[2]), int(m[3]), int(m[4])
        assert start >= end and length >= 1 and -48 <= pitch <= 48, l
        end = start + length
    return True


class UltraStar(unittest.TestCase):
    def test_song_txt(self):
        txt = U.song_txt(ENTRY, LINES, NOTES, {"audio": "audio.m4a", "video": "video.mp4", "vocals": "vocals.m4a"})
        self.assertTrue(check_rules(txt))
        self.assertIn("#TITLE:Glass Sky (玻璃天空)", txt)
        self.assertIn("#LANGUAGE:English", txt)
        self.assertIn("#VOCALS:vocals.m4a", txt)
        body = [l for l in txt.split("\n") if l and not l.startswith("#")]
        self.assertEqual(body, [": 25 10 0 Hello ",            # 1.00 s = beat 25, 0.40 s = 10 beats, MIDI 60 = 0
                                ": 38 10 2 sky",                # "sky" in two pieces: the word, then "~"
                                ": 48 10 4 ~ ",
                                "- 58",                         # the line ends with its last note
                                ": 75 10 0 come ",              # (the backing line isn't there)
                                "F 85 10 0 home ",              # no melody for "home": freestyle, not scored
                                "E"])

    def test_export_folder(self):
        lib, out = tempfile.mkdtemp(dir=_env.ROOT), tempfile.mkdtemp(dir=_env.ROOT)
        d = os.path.join(lib, "Music Videos")
        os.makedirs(d)
        b = os.path.join(d, ENTRY["base"])
        import subprocess
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=4", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=4",
                        "-shortest", "-c:a", "aac", "-c:v", "libx264", b + ".mp4"], check=True)
        json.dump({"lines": LINES}, open(b + ".words.json", "w"))
        json.dump({"notes": NOTES}, open(b + ".pitch.json", "w"))
        e = dict(ENTRY, words=ENTRY["base"] + ".words.json", pitch=ENTRY["base"] + ".pitch.json")
        dest = U.export(lib, e, out)
        self.assertEqual(sorted(os.listdir(dest)), ["audio.m4a", "song.txt", "video.mp4"])
        self.assertTrue(check_rules(open(os.path.join(dest, "song.txt"), encoding="utf-8").read()))
        self.assertEqual(os.stat(os.path.join(dest, "video.mp4")).st_ino, os.stat(b + ".mp4").st_ino)   # a hard link
        self.assertTrue(os.path.getsize(os.path.join(dest, "audio.m4a")) > 1000)
        shutil.rmtree(lib); shutil.rmtree(out)
