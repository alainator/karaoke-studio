"""Check every finished song by ear and mark the lines that need fixing (for songs made before this was automatic).

    venv/bin/python -I deploy/check-library.py            # all finished songs that have no check yet
    venv/bin/python -I deploy/check-library.py --all      # check every song again
    venv/bin/python -I deploy/check-library.py --dry      # only report, change nothing

Transcribes each song's vocal track once on the GPU (about 20 s a song, saved as heard.json), then marks the lines
that were heard somewhere other than where they sit. Words, timing, singers and published files are not touched:
only the ⚠ marks change. The first time, a song's words.json is copied to words.before-check.json.
"""
import json, os, shutil, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config as C
import earcheck
from assgen import normalize

ALL, DRY = "--all" in sys.argv, "--dry" in sys.argv
model = None
todo = marked = 0
for jid in sorted(os.listdir(C.JOBS)):
    d = os.path.join(C.JOBS, jid)
    try:
        job = json.load(open(os.path.join(d, "job.json")))
    except Exception:
        continue
    if job.get("status") != "done" or not os.path.exists(os.path.join(d, "words.json")) or not os.path.exists(os.path.join(d, "vocals.m4a")):
        continue
    if (job.get("result") or {}).get("confidence") and not ALL:
        continue
    hp = os.path.join(d, "heard.json")
    if not os.path.exists(hp):
        import stable_whisper
        model = model or stable_whisper.load_model(C.WHISPER_MODEL, device="cuda")
        lines = normalize(json.load(open(os.path.join(d, "words.json"))))
        lang = earcheck.heard_language(lines, (job.get("result") or {}).get("lang"))
        tr = model.transcribe(os.path.join(d, "vocals.m4a"), language=lang, word_timestamps=True, vad=False, verbose=None,
                              condition_on_previous_text=False)
        json.dump({"lang": tr.language, "words": [[round(w.start, 2), round(w.end, 2), w.word] for sg in tr.segments for w in sg.words]},
                  open(hp, "w"), ensure_ascii=False)
    if not DRY and not os.path.exists(os.path.join(d, "words.before-check.json")):
        shutil.copyfile(os.path.join(d, "words.json"), os.path.join(d, "words.before-check.json"))
    conf = earcheck.check_job(d, save=not DRY)
    todo += 1; marked += conf["check"]
    heard = "couldn't follow the words" if conf["sure"] is None else f"{conf['sure']} of {conf['lines']} lines heard in place"
    print(f"{conf['check']:3} to check | {heard} | {job.get('artist')} - {job.get('title')}", flush=True)
print(f"{todo} songs checked, {marked} lines marked" + (" (dry run: nothing changed)" if DRY else ""))
