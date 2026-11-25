"""UltraStar export: a published song as an UltraStar song folder, so singing games that read UltraStar .txt files
(UltraStar Deluxe, Vocaluxe, Performous, AllKaraoke...) can play it, scored against the Studio's own melody.

    folder = export(lib_dir, entry, out_dir, link=True)    # entry: the song's catalog.json entry

A folder "Artist - Title" with song.txt, audio.m4a (the instrumental: the published video's sound, not re-encoded),
vocals.m4a (the guide vocals, for players that offer them), video.mp4 and cover.jpg (hard links when the export is
on the same drive, copies otherwise).

song.txt (format as UltraStar Deluxe reads it; researched 2026-11-22 from USDX's parser, which is GPL: only the format
is taken, no code):
- #VERSION:1.1.0 (USDX refuses 2.0.0 and up; 1.x files are UTF-8 and may name VOCALS / INSTRUMENTAL).
- #BPM is a time grid only (the header's value times 4 is beats a minute): #BPM:375 = 1500 beats a minute = 40 ms a
  beat, so every time is round(seconds / 0.04) beats and nothing depends on the song's tempo. #GAP:0: beat 0 is the
  start of the video (the Studio's times are already video times).
- One note per piece of the melody inside each word (pitch.json's notes cut at the word's edges, as Encore's scorer
  cuts them): ": start length pitch text"; the word's text on its first piece, "~" on the others, a space after the
  word's last piece. Pitch is the nearest semitone, 0 = middle C (MIDI 60), as USDX's editor counts it.
- A word the melody track has no note for is freestyle ("F": shown and sung, not scored), so nobody loses points on
  a word the Studio couldn't hear the melody of.
- "- beat" between lyric lines; "E" at the end. Backing-vocal lines are left out (the singer isn't scored on them).
- A word sung in syllables (words.json "syl") gets its notes per piece, each piece's text on its first note.
Limits: one singer track (duets are exported as one); other words' notes are per word, not per syllable.
"""
import json, os, re, shutil, subprocess

BEAT = 0.04                 # seconds per beat (#BPM:375)
LANGS = {"en": "English", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "yue": "Cantonese", "es": "Spanish",
         "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian", "ru": "Russian", "nl": "Dutch", "sv": "Swedish",
         "pl": "Polish", "tr": "Turkish", "ar": "Arabic", "hi": "Hindi", "pa": "Punjabi", "vi": "Vietnamese", "th": "Thai",
         "id": "Indonesian", "tl": "Tagalog", "el": "Greek", "he": "Hebrew", "fa": "Persian", "uk": "Ukrainian"}


def _beat(t):
    return int(round(t / BEAT))


def _clean(s):
    """Header values on one line."""
    return re.sub(r"[\r\n]+", " ", str(s or "")).strip()


def notes_for(words, notes, syl=None):
    """[[start_beat, length, pitch or None, text]] for one line: each word's melody pieces (None: freestyle); a word
    sung in syllables (syl: words.json's list, null per unsplit word), per syllable. Not yet in order with its
    neighbours (in_order() does that for the whole song)."""
    out = []
    for j, (ws, we, text) in enumerate(words):
        parts = (syl[j] if syl and j < len(syl) and syl[j] else None) or [[ws, we, text]]
        for q, (ps, pe, ptext) in enumerate(parts):
            pieces = [(max(ps, a), min(pe, b), m) for a, b, m in notes if b > ps and a < pe and min(pe, b) - max(ps, a) > 0.02]
            if not pieces:
                pieces = [(ps, pe, None)]
            for k, (a, b, m) in enumerate(pieces):
                t = ptext if k == 0 else "~"
                if q == len(parts) - 1 and k == len(pieces) - 1:
                    t += " "
                out.append([_beat(a), max(1, _beat(b) - _beat(a)), None if m is None else int(round(m)) - 60, t])
    return out


def in_order(notes):
    """Whole beats in the song's order: each note starts after the one before it has ended (words and lines can
    overlap a little in the Studio's timing; UltraStar notes can't), shortening the one before when they touch."""
    for i in range(1, len(notes)):
        prev, cur = notes[i - 1], notes[i]
        if cur[0] <= prev[0]:
            cur[0] = prev[0] + 1
        if prev[0] + prev[1] > cur[0]:
            prev[1] = max(1, cur[0] - prev[0])
        if prev[0] + prev[1] > cur[0]:   # (prev is 1 beat long and still reaches it)
            cur[0] = prev[0] + prev[1]
    return notes


def song_txt(entry, lines, notes, media):
    """The text of song.txt. lines: words.json lines; notes: pitch.json notes; media: which files the folder has."""
    head = {"VERSION": "1.1.0", "TITLE": entry.get("title"), "ARTIST": entry.get("artist"),
            "LANGUAGE": LANGS.get(entry.get("lang") or "", entry.get("lang")), "YEAR": entry.get("year"),
            "GENRE": entry.get("genre"), "AUDIO": media.get("audio"), "INSTRUMENTAL": media.get("audio"),
            "VOCALS": media.get("vocals"), "VIDEO": media.get("video"), "COVER": media.get("cover"),
            "BPM": "375", "GAP": "0"}
    if entry.get("title_orig"):
        head["TITLE"] = f"{entry['title']} ({entry['title_orig']})"
    out = [f"#{k}:{_clean(v)}" for k, v in head.items() if v]
    flat = []   # [line number, note]
    for i, l in enumerate(x for x in lines if not x.get("backing") and x.get("words")):
        flat += [[i, n] for n in notes_for(l["words"], notes, l.get("syl"))]
    in_order([n for _, n in flat])
    for k, (i, (b, n, p, t)) in enumerate(flat):
        if k and i != flat[k - 1][0]:
            pb, pn = flat[k - 1][1][0], flat[k - 1][1][1]
            out.append(f"- {pb + pn}")   # a new lyric line: the break where the line before it ends
        out.append(f"{'F' if p is None else ':'} {b} {n} {p if p is not None else 0} {t}")
    out.append("E")
    return "\n".join(out) + "\n"


def _place(src, dst, link):
    if os.path.exists(dst):
        os.remove(dst)
    if link:
        try:
            os.link(src, dst)
            return
        except OSError:
            pass
    shutil.copyfile(src, dst)


def safe_folder(name):
    return re.sub(r'[\\/:*?"<>|]+', "-", name).strip(" .")[:150] or "song"


def export(lib_dir, entry, out_dir, link=True):
    """Write one song's UltraStar folder; returns its path. Raises FileNotFoundError when the song's files are gone."""
    folder_dir = os.path.join(lib_dir, entry.get("folder") or "")
    base = entry["base"]
    video = os.path.join(folder_dir, base + ".mp4")
    words = os.path.join(folder_dir, entry.get("words") or base + ".words.json")
    if not (os.path.isfile(video) and os.path.isfile(words)):
        raise FileNotFoundError(base)
    lines = json.load(open(words, encoding="utf-8"))["lines"]
    notes = []
    if entry.get("pitch") and os.path.isfile(os.path.join(folder_dir, entry["pitch"])):
        notes = json.load(open(os.path.join(folder_dir, entry["pitch"]), encoding="utf-8")).get("notes") or []
    dest = os.path.join(out_dir, safe_folder(f"{entry.get('artist')} - {entry.get('title')}"))
    os.makedirs(dest, exist_ok=True)
    media = {"video": "video.mp4", "audio": "audio.m4a"}
    _place(video, os.path.join(dest, "video.mp4"), link)
    audio = os.path.join(dest, "audio.m4a")
    if not os.path.exists(audio) or os.path.getmtime(audio) < os.path.getmtime(video):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", video, "-vn", "-map", "0:a:0", "-c:a", "copy", audio], check=True)
    if entry.get("vocals") and os.path.isfile(os.path.join(folder_dir, entry["vocals"])):
        _place(os.path.join(folder_dir, entry["vocals"]), os.path.join(dest, "vocals.m4a"), link)
        media["vocals"] = "vocals.m4a"
    cover = os.path.join(folder_dir, entry["cover"]) if entry.get("cover") else os.path.join(folder_dir, base + ".cover.jpg")
    if os.path.isfile(cover):
        _place(cover, os.path.join(dest, "cover.jpg"), link)
        media["cover"] = "cover.jpg"
    with open(os.path.join(dest, "song.txt"), "w", encoding="utf-8") as f:
        f.write(song_txt(entry, lines, notes, media))
    return dest
