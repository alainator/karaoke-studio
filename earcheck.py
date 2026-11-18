"""Mark the lines of a finished song that need checking, by ear (see linecheck.py), and say how sure the studio is.

    conf = mark(lines, heard, lang)        # sets "weak" and "why" on each line, returns the song's confidence
    conf = check_job(job_dir)              # the same for a job folder that has heard.json (no GPU needed)

A line is marked when it comes up late (the singing starts over a second before it) or far too early (well over two
seconds ahead, so it leaves the screen before it's sung), when it wasn't heard and
its words fit badly (the aligner's own doubt), or when the studio placed it by a guess. Lines the user cleared
("ok") stay cleared. Backing-vocal lines aren't judged.
"""
import json, os
from linecheck import heard_offsets

# It's karaoke: a line that is up a little early and stays long enough to sing is good enough. A line appears up to
# 2 s before its first word and stays up to 2 s after its last (assgen LEAD, LINGER), so:
LATE = 1.0   # the singing starts this long before the line lights up: the singer is caught out
EARLY = 2.5  # the line runs this far ahead of the singing: it can leave the screen before its last words are sung


def mark(lines, heard, lang=None, offset=0.0):
    lead = [l for l in lines if not l.get("backing")]
    offs = heard_offsets(lead, [[s - offset, e - offset, w] for s, e, w in heard], lang) if heard else [None] * len(lead)
    known = bool(heard) and sum(x is not None for x in offs) >= max(4, 0.15 * len(lead))  # could the song be followed at all?
    sure = check = 0
    for l, x in zip(lead, offs):
        why = None
        if l.get("_guess"):
            why = l["_guess"]
        elif x is not None and x < -LATE:
            why = f"late: the singing starts about {-x:.0f} s before this line lights up"
        elif x is not None and x > EARLY:
            why = f"early: this line runs about {x:.0f} s ahead of the singing and may leave the screen too soon"
        elif x is None and l.get("_lowfit"):
            why = "not heard here, and its words fit the singing poorly" if known else "its words fit the singing poorly"
        if l.get("ok") and not l.get("_guess"):
            why = None  # the user said this line is fine
        l["weak"], l["why"] = bool(why), why
        check += bool(why)
        sure += bool(x is not None and not why)
    for l in lines:
        if l.get("backing"):
            l["weak"], l["why"] = False, None
    return {"lines": len(lead), "check": check, "sure": sure if known else None}


def heard_language(lines, lang):
    """The language to transcribe in: the song's, unless its lines are mostly plain Latin text (an English song with
    a Japanese chorus is listened to in English, like the pipeline does); None lets Whisper decide."""
    lead = [l for l in lines if not l.get("backing")]
    native = sum(1 for l in lead if l.get("orig"))
    if lang in ("ja", "ko", "zh", "yue") and native < 0.5 * max(1, len(lead)):
        return "en" if native else None
    return {"yue": "zh"}.get(lang, lang)


def check_job(job_dir, save=True):
    """Re-mark a finished job from its saved transcription; returns the confidence, or None if it has none."""
    from assgen import normalize, save_lines, load_cast
    hp, wp = os.path.join(job_dir, "heard.json"), os.path.join(job_dir, "words.json")
    if not (os.path.exists(hp) and os.path.exists(wp)):
        return None
    heard = json.load(open(hp))
    job = json.load(open(os.path.join(job_dir, "job.json")))
    lines = normalize(json.load(open(wp)))
    for l in lines:  # old marks came from word fit alone: keep that doubt only as a hint for unheard lines
        l["_lowfit"] = bool(l.get("weak")) and not l.get("why")
    conf = mark(lines, heard["words"], heard.get("lang"), float(job.get("offset") or 0.0))
    if save:
        save_lines(wp, lines, load_cast(wp))
        rp = os.path.join(job_dir, "result.json")
        if os.path.exists(rp):
            res = json.load(open(rp)); res["confidence"] = conf
            json.dump(res, open(rp + ".tmp", "w")); os.replace(rp + ".tmp", rp)
        jp = os.path.join(job_dir, "job.json")  # the app reads the result from here
        job = json.load(open(jp))
        if isinstance(job.get("result"), dict):
            job["result"]["confidence"] = conf
            json.dump(job, open(jp + ".tmp", "w")); os.replace(jp + ".tmp", jp)
    return conf
