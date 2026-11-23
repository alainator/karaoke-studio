"""The video check: what the Studio found out about a music video while making its song, and whether the song can go
out without a person looking at it.

The pipeline records the facts in result.json ("video_check"); verdict() turns them, with the by-ear confidence, into
checks a person can read on the song page (and `ks show`), and into one automatic fix when the cause is clear.

Why not just a score (calibrated 2026-11-22 on 119 songs people checked and 5 runs that went wrong): a wrong make and
a song that's hard to hear (Little Richard, the "na na na" of Hey Jude) look alike in every number the Studio has
(lines heard in place, word fit, heard words that aren't lyrics, lines left out). So the score only holds a song for a
look (the auto-make floor: 60% of lines heard in place; it held 5 of the 119 good songs and 4 of the 5 bad runs), and
automatic fixes come only from causes the Studio can see directly:

- language: lyrics written in English but listened to as Japanese (the 5th bad run, Fireworks, passed the floor at
  77%; only this check catches it). The fix is a remake in the lyrics' language.
- the album map: whether the video's music was matched with the album recording, and why not (it used to say nothing
  when it didn't run).
- lyrics copy: timestamps reaching past the end of the video (a copy for a longer version).
- lines left out: a quarter or more of the lyrics "not sung in this video" (a shortened edit, or another version's lyrics).
"""

import re

EN = set("the you i and my me is your it's i'm don't can't of in it love all that this what with be are we they "
         "was for on so just know never can will".split())
ROMAJI_HINT = set("wa wo ni ga kimi boku watashi naka kokoro nai desu dake kara made".split())
CJK_LANGS = ("ja", "ko", "zh", "yue")
LEFT_OUT = 0.25          # share of lines not sung in this video that's worth a note
PAST_END = 10.0          # lyrics timestamps this many seconds past the video's end: a copy for a longer version


def _share(text, words):
    t = re.findall(r"[a-z']+", (text or "").lower())
    return sum(w in words for w in t) / len(t) if t else 0.0


def lyrics_language(texts):
    """'en' for lyrics plainly in English, 'ja' for romaji, None when the words alone can't tell (other Latin-script
    languages, or not enough words). Same rule the pipeline uses for KaraMoe uploads."""
    t = " ".join(texts or [])
    en, rj = _share(t, EN), _share(t, ROMAJI_HINT)
    if en >= 0.15 and rj < 0.03:
        return "en"
    if rj >= 0.08 and en < 0.1:
        return "ja"
    return None


def verdict(result, floor_ok, floor_why):
    """{"ok": bool, "checks": [{"key", "ok", "note"}], "fix": {"lang": x} or None}. floor_ok/floor_why: the auto-make
    quality floor's answer (automake.check), so there is one rule for "good enough" and not two."""
    vc = (result or {}).get("video_check") or {}
    checks, fix = [], None

    # 1. the language listened in vs the language the lyrics are written in
    written, listened = vc.get("lyrics_lang"), (result or {}).get("lang")
    if written and listened:
        wrong = (written == "en" and listened in CJK_LANGS) or (written == "ja" and listened not in CJK_LANGS)
        if wrong:
            checks.append({"key": "language", "ok": False,
                           "note": f"the lyrics are in {_lang_name(written)} but the singing was listened to as {_lang_name(listened)}"})
            fix = {"lang": written}
        else:
            checks.append({"key": "language", "ok": True, "note": f"listened to in {_lang_name(listened)}, as the lyrics are written"})

    # 2. the album map
    am = vc.get("album") or {}
    state = am.get("state")
    if state == "used":
        parts = am.get("parts") or 1
        checks.append({"key": "album", "ok": True, "note": "timed from the album recording" +
                       (f" ({parts} parts: the video has scenes or cuts)" if parts > 1 else "")})
    elif state:
        checks.append({"key": "album", "ok": True, "info": True, "note": {
            "found": "the album recording was found but the lyrics fit better without it",
            "none": "no album recording to compare the video with: timed from the lyrics and the singing",
            "skipped": f"the album recording wasn't looked for ({am.get('why') or 'no reason given'})"}.get(state, state)})

    # 3. lyrics copy vs the video's length
    end, vdur = vc.get("lyrics_end"), (result or {}).get("duration")
    if end and vdur and end > vdur + PAST_END and state != "used":
        checks.append({"key": "lyrics", "ok": False,
                       "note": f"the lyrics' timestamps run to {end / 60:.0f}:{end % 60:02.0f}, past the video's end: "
                               "they may be for a longer version (another copy of the lyrics could fit better)"})

    # 4. lines left out
    n, left = (result or {}).get("lines") or 0, len((result or {}).get("skipped") or [])
    if n + left and left / (n + left) >= LEFT_OUT:
        checks.append({"key": "left_out", "ok": True, "info": True,
                       "note": f"{left} of {n + left} lyric lines aren't sung in this video (a shortened edit, or another version's lyrics)"})

    # 5. the floor
    checks.append({"key": "heard", "ok": bool(floor_ok), "note": floor_why})
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks, "fix": fix}


LANG_NAMES = {"en": "English", "ja": "Japanese", "ko": "Korean", "zh": "Mandarin", "yue": "Cantonese", "es": "Spanish",
              "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian", "hi": "Hindi", "pa": "Punjabi"}


def _lang_name(code):
    return LANG_NAMES.get(code, code)
