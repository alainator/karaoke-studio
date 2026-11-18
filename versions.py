"""What changed in how songs are made, so a song made earlier can show what a remake would add.

Every song records the version that made it (result.json "pipeline"); songs from before that use the time they were
made. Add an entry here whenever a change makes songs come out better, with which songs it matters for:
"all", "latin" (Latin-alphabet lyrics), "ja" (Japanese), "native" (Korean, Chinese), "pasted" (pasted lyrics).
"""
IMPROVEMENTS = [  # (when it landed, unix time; songs it matters for; what a remake adds)
    (1794940958, "all", "the video's timing is chosen by how well the words fit the singing (fixed a 12 s wrong offset)"),
    (1794949370, "ja", "romaji readings checked against what is actually sung"),
    (1794950389, "ja", "special kanji readings (幻 sung \"yume\") picked by listening"),
    (1794957067, "latin", "AI lyrics check: words the lyrics dropped or swapped are fixed by listening"),
    (1794958211, "all", "lines squeezed by a wrong lyrics timestamp get their full time"),
    (1794959111, "all", "music videos with a skit or scenes: the timing follows the jumps, and the skit keeps its sound"),
    (1794962987, "all", "better at finding where a music video's timing jumps"),
    (1794963662, "all", "the video is re-encoded so jumping backwards in the editor doesn't freeze it"),
    (1794972339, "latin", "transcription first: the lyrics are matched to what is actually sung before timing"),
    (1794975439, "all", "lines whose timing the aligner is unsure about are marked for checking in the editor"),
    (1794975439, "ja", "mostly-English songs with a Japanese chorus are listened to in English"),
    (1794976824, "all", "talking outside the sung lines (an intro, a skit, an outro) keeps its sound, also with pasted lyrics"),
    (1794976824, "pasted", "lyrics-site leftovers (Genius's \"You might also like\" songs, \"Embed\") are removed from pasted lyrics"),
    (1794988822, "all", "the video is matched against the album recording itself, so skits, scenes and cuts are found exactly"),
    (1794988822, "latin", "backing vocals in brackets get their own line, timed separately so they can overlap the lead"),
    (1794988822, "pasted", "pasted lyrics get the timing of LRCLIB's timed version of the song, matched word by word"),
    (1794988822, "latin", "the AI lyrics check only makes small fixes (at most 2 words in a line)"),
    (1795013581, "all", "talking is also found by its sound, in any language, and keeps the video's own sound"),
    (1795013581, "all", "held last notes end where the sung pitch stops, not where the track gets quiet"),
    (1795013581, "all", "songs whose words can't be followed by ear: repeated sections are checked against where their melody repeats"),
]
VERSION = len(IMPROVEMENTS)


def missing(job):
    """What remaking this (finished) song would add, as short descriptions; [] if it's up to date."""
    res = job.get("result") or {}
    made = res.get("pipeline")
    if isinstance(made, int):
        newer = IMPROVEMENTS[made:]
    else:  # made before songs recorded their version: compare with when it was made
        t = job.get("started") or job.get("created") or 0
        newer = [x for x in IMPROVEMENTS if x[0] > t]
    lang = res.get("lang")
    kind = "ja" if lang == "ja" else "native" if lang in ("ko", "zh") else "latin"
    out = []
    for _, who, what in newer:
        if who not in ("all", kind) and not (who == "pasted" and job.get("lyrics_source") == "pasted"):
            continue
        if what.startswith("the video is re-encoded") and res.get("encoded") == "x264":
            continue  # already fixed by deploy/reencode-videos.py
        out.append(what)
    return out
