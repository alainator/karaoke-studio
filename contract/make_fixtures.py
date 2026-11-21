#!/usr/bin/env python3
"""Regenerate contract/fixtures with the Studio's own code: a throwaway studio (jobs, library, player store in a
temporary folder) makes three made-up songs with placeholder words (no real lyrics), publishes them the way the web
app does, and then every sample API exchange is a real call to the Studio's /api/encore/ handlers (through all its
middleware). The output is the same on every run (a fixed clock, ids, code and token), so the copy in Encore's repo
can be compared byte for byte.

    python -I contract/make_fixtures.py [OUT]       (default: contract/fixtures; run with the studio's Python, e.g.
                                                      in its image: no GPU, network or PiKaraoke needed)

fixtures/library/: catalog.json and "Music Videos/" (.mp4 placeholders of a few bytes, .ass, .words.json, .pitch.json,
  .vocals.m4a: a real AAC file of a quarter second of silence, 782 bytes, made once with ffmpeg and kept below)
  - Nova Lane - Paper Lanterns: a solo (the last line a backing line)
  - The Quiet Arcade - Two Small Rivers: a duet with named singers (Ada, Bram), a line sung together, a line shared
    word by word; sung in Japanese with an original-script row (placeholder kana), so it has every lyric mode
  - Mika Sol - Neon Harbor: with a custom-video version, "Mia's version" (its own .mp4 and lyric files; the same sound
    and timing as the song, as every Studio version)
fixtures/api/NN-name.json: {"name", "about", "request": {method, path, headers, body, schema}, "response": {status,
  headers (only ETag, Content-Range, Accept-Ranges, Retry-After), body, schema}}; a body {"$file": path} is that fixture
  file (with "$range": [first, last] the bytes of it). The bearer token shows as "<token from pairing>".
The pitch files come from the Studio's note maker (pitch.notes) fed a made-up melody (the CREPE step needs real
vocals), cached the way pitch.py caches it.
"""
import base64, json, os, shutil, sys, tempfile, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
OUT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "fixtures")
ROOT = tempfile.mkdtemp(prefix="studio-fixtures-")
for k, v in {"KARAOKE_JOBS": f"{ROOT}/jobs", "KARAOKE_WORK": f"{ROOT}/work", "KARAOKE_MODELS": f"{ROOT}/models",
             "KARAOKE_LOGS": f"{ROOT}/logs", "KARAOKE_DATA": f"{ROOT}/data", "KARAOKE_FONTS": f"{ROOT}/fonts",
             "KARAOKE_LIBRARY": f"{ROOT}/library", "KARAOKE_MV_FOLDER": "Music Videos", "PIKARAOKE_URL": "",
             "PIKARAOKE_ADMIN_PASSWORD": "", "PIKARAOKE_DB": "", "OLLAMA_URL": "", "GPU_HELPER_URL": "", "HELPER_TOKEN": "",
             "STUDIO_ORIGINS": "", "STUDIO_PUBLIC_URL": "https://studio.example.lan", "STUDIO_NAME": "Karaoke Studio",
             "ENCORE_TOKEN": "", "ENCORE_ONLY_NETS": "", "KARAOKE_LYRICS_FONT": "classic", "KARAOKE_LYRICS_DISPLAY": "both",
             "AGENT_KEYS_REQUIRED": "0", "STUDIO_PERSON_HEADER": "X-authentik-username", "KARAOKE_DEVICE": "",
             "TZ": "UTC"}.items():
    os.environ[k] = v
for d in ("jobs", "work", "models", "logs", "data", "fonts", "library/Music Videos"):
    os.makedirs(f"{ROOT}/{d}", exist_ok=True)
sys.path.insert(0, REPO)
time.tzset()

# a fixed clock, token and studio id: the same files on every run
T0 = 1793534400.0
DAY = 86400
CLOCK = [T0]
_strftime = time.strftime
time.time = lambda: CLOCK[0]
time.strftime = lambda fmt, t=None: _strftime(fmt, time.gmtime(CLOCK[0]) if t is None else t)
import secrets  # noqa: E402
secrets.token_urlsafe = lambda n=32: "fixture-token-0123456789abcdef"

import numpy as np  # noqa: E402
import app as A  # noqa: E402
import encore  # noqa: E402
import pitch as P  # noqa: E402
from assgen import save_lines  # noqa: E402
from tests._asgi import call  # noqa: E402

A.STORE.now = lambda: CLOCK[0]
A.STORE.q("insert or replace into kv (k, v) values ('studio_id', ?)", ("9c1e7b42-5d3a-4f86-a0b9-2e4c6d8f1a3b",))
A.pitch_later = lambda jid: None   # written below, in order (not in the background)
A.catalog_soon = lambda delay=0: None

TOKEN_SHOWN = "<token from pairing>"
PLAYER = "6f1c2a90-3b7e-4c1d-9e55-0a7b8c9d1e2f"
CODE = "K7M3PQ9X"
U = lambda name: str(uuid.uuid5(uuid.NAMESPACE_URL, "encore-fixture:" + name))
R = ["b3f1c9a2-1d4e-4f6a-8b7c-2e9d0a1b3c4d", "c4a2d0b3-2e5f-4a7b-9c8d-3f0e1b2c4d5e", "d5b3e1c4-3f6a-4b8c-8d9e-4a1f2c3d5e6f",
     "e6c4f2d5-4a7b-4c9d-9e0f-5b2a3d4e6f7a", "f7d5a3e6-5b8c-4dae-8f1a-6c3b4e5f7a8b", "a8e6b4f7-6c9d-4ebf-9a2b-7d4c5f6a8b9c"]
FID = ["0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "1b2c3d4e-5f6a-4b7c-9d8e-0f1a2b3c4d5e", "2c3d4e5f-6a7b-4c8d-8e9f-1a2b3c4d5e6f"]
PID = ["3d4e5f6a-7b8c-4d9e-9f0a-2b3c4d5e6f7a", "4e5f6a7b-8c9d-4e0f-8a1b-3c4d5e6f7a8b", "5f6a7b8c-9d0e-4f1a-9b2c-4d5e6f7a8b9c"]
UID = "7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
SYLL = ["la", "da", "na", "doo", "ba", "oh"]
KANA = {"la": "ら", "da": "だ", "na": "な", "doo": "どう", "ba": "ば", "oh": "お"}
VOCALS_M4A = base64.b64decode(
    "AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAtZtb292AAAAbG12aGQAAAAAAAAAAAAAAAAAAAPoAAAA+gABAAABAAAAAAAA"
    "AAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAAACJXRy"
    "YWsAAABcdGtoZAAAAAMAAAAAAAAAAAAAAAEAAAAAAAAA+gAAAAAAAAAAAAAAAQEAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAA"
    "AAAAAAAAAEAAAAAAAAAAAAAAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAAPoAAAQAAAEAAAAAAZ1tZGlhAAAAIG1kaGQAAAAA"
    "AAAAAAAAAAAAAB9AAAAL0FXEAAAAAAAtaGRscgAAAAAAAAAAc291bgAAAAAAAAAAAAAAAFNvdW5kSGFuZGxlcgAAAAFIbWluZgAA"
    "ABBzbWhkAAAAAAAAAAAAAAAkZGluZgAAABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEAAAEMc3RibAAAAGpzdHNkAAAAAAAAAAEA"
    "AABabXA0YQAAAAAAAAABAAAAAAAAAAAAAQAQAAAAAB9AAAAAAAA2ZXNkcwAAAAADgICAJQABAASAgIAXQBUAAAAAAD6AAAAA/QWA"
    "gIAFFYhW5QAGgICAAQIAAAAgc3R0cwAAAAAAAAACAAAAAgAABAAAAAABAAAD0AAAABxzdHNjAAAAAAAAAAEAAAABAAAAAwAAAAEA"
    "AAAUc3RzegAAAAAAAAAEAAAAAwAAABRzdGNvAAAAAAAAAAEAAAMCAAAAGnNncGQBAAAAcm9sbAAAAAIAAAAB//8AAAAcc2JncAAA"
    "AAByb2xsAAAAAQAAAAMAAAABAAAAPXVkdGEAAAA1bWV0YQAAAAAAAAAhaGRscgAAAAAAAAAAbWRpcmFwcGwAAAAAAAAAAAAAAAAI"
    "aWxzdAAAAAhmcmVlAAAAFG1kYXQBGCAHARggBwEYIAc=")
FAKE_MP4 = lambda seconds: (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"placeholder video, not playable: "
                            + f"{seconds:.1f} s".encode()).ljust(96, b".")


def lines_of(spec):
    """spec: [(start, members or None, n words, extra)] -> studio lines (words.json v2) with placeholder words."""
    out = []
    for i, (start, members, n, extra) in enumerate(spec):
        t, words = start, []
        for k in range(n):
            words.append([round(t, 2), round(t + 0.42, 2), SYLL[(i + k) % len(SYLL)]])
            t += 0.5
        l = {"words": words}
        if members:
            l["members"] = members
        l.update(extra)
        out.append(l)
    return out


def melody(lines, duration, lo=57.0):
    """A made-up sung melody for the note maker: f0 (Hz) and CREPE-like periodicity per 20 ms frame."""
    n = int(round(duration / P.HOP))
    f0, per = np.zeros(n), np.full(n, 0.05)
    for li, l in enumerate(lines):
        for k, (a, b, _) in enumerate(l["words"]):
            m = lo + (li * 2 + k) % 9 + (0.5 if k % 3 == 2 else 0.0)
            i0, i1 = int(round(a / P.HOP)), int(round(b / P.HOP))
            f0[i0:i1] = 440.0 * 2 ** ((m - 69) / 12)
            per[i0:i1] = 0.9
    return f0, per


def make_job(jid, artist, title, lang, duration, lines, cast=(), status="done"):
    d = os.path.join(A.JOBS, jid)
    os.makedirs(d)
    save_lines(os.path.join(d, "words.json"), lines, list(cast))
    open(os.path.join(d, "out.mp4"), "wb").write(FAKE_MP4(duration))
    open(os.path.join(d, "vocals.m4a"), "wb").write(VOCALS_M4A)  # published as <base>.vocals.m4a (guidevocals.py)
    f0, per = melody(lines, duration)
    np.savez_compressed(os.path.join(d, P.CACHE), f0=f0.astype(np.float32), per=per.astype(np.float16), hop=np.float32(P.HOP),
                        stamp=np.array(P._stamp(os.path.join(d, "vocals.m4a"))), model=np.array("fixture"))
    result = {"offset": 0.0, "lines": len(lines), "duration": duration, "lang": lang, "width": 1920, "height": 1080,
              "video": "h264,1920,1080", "encoded": "x264", "pipeline": A.versions.VERSION}
    json.dump(result, open(os.path.join(d, "result.json"), "w"))
    job = {"id": jid, "yt": "aaaaaaaaaaa", "lrc_id": 1, "lyrics_source": "lrclib", "artist": artist, "title": title,
           "status": status, "step": status if status == "done" else "align", "created": CLOCK[0], "finished": CLOCK[0],
           "offset": 0.0, "lyrics_display": "both", "result": result, "log": []}
    A.save(jid, job)
    A.rebuild_lyrics(jid, job)
    return jid


def publish(jid):
    st, _, b = call(A.app, "POST", f"/api/jobs/{jid}/publish", {"remove": []})
    assert st == 200, (st, b)
    A.write_pitch(jid)


EXCHANGES = []
KEEP_HEADERS = ("etag", "content-range", "accept-ranges", "retry-after")


def ex(n, name, about, method, path, body=None, schema=None, res_schema=None, headers=None, auth=True, client=None,
       expect=None, body_file=None):
    """Make the call for real, and keep it as fixtures/api/NN-name.json."""
    h = dict(headers or {})
    if auth:
        h["Authorization"] = "Bearer " + (TOKEN if auth is True else auth)
    st, rh, rb = call(A.app, method, path, body, h, **({"client": client} if client else {}))
    if expect is not None:
        assert st == expect, (name, st, rb[:300])
    shown = {k: ("Bearer " + TOKEN_SHOWN if k == "Authorization" and (auth is True or auth in MASKED) else v) for k, v in h.items()}
    if body_file:
        res_body = body_file
    else:
        res_body = json.loads(rb) if rb else None
    EXCHANGES.append((n, name, {"name": name, "about": about,
                                "request": {"method": method, "path": path, "headers": shown, "body": body, "schema": schema},
                                "response": {"status": st, "headers": {k.title() if k != "etag" else "ETag": v for k, v in rh.items()
                                                                       if k in KEEP_HEADERS},
                                             "body": res_body, "schema": res_schema}}))
    return st, rh, rb


TOKEN = None
MASKED = set()   # other players' tokens from pairing, shown as TOKEN_SHOWN too


def main():
    global TOKEN
    err = "error.schema.json"
    # ---- the songs
    solo = make_job("20261101-120000-nova-lane-paper-lanterns", "Nova Lane", "Paper Lanterns", "en", 33.0,
                    lines_of([(12.4, None, 4, {}), (16.8, None, 5, {}), (24.0, None, 4, {}), (29.5, None, 3, {"backing": True})]))
    publish(solo)
    CLOCK[0] += DAY
    duet_lines = lines_of([(8.0, [0], 4, {}), (11.0, [1], 4, {}), (14.5, [0, 1], 5, {}),
                           (19.0, [0], 4, {"wwho": [{"members": [0]}, {"members": [0]}, {"members": [1]}, {"members": [1]}]}),
                           (23.0, [1], 3, {})])
    for l in duet_lines:
        l["orig"] = "".join(KANA[w[2]] for w in l["words"])
    duet = make_job("20261102-090000-the-quiet-arcade-two-small-rivers", "The Quiet Arcade", "Two Small Rivers", "ja", 27.5,
                    duet_lines, cast=["Ada", "Bram"])
    publish(duet)
    CLOCK[0] += DAY
    ver_lines = lines_of([(10.0, None, 4, {}), (14.0, None, 4, {}), (18.5, None, 5, {})])
    ver = make_job("20261103-183000-mika-sol-neon-harbor", "Mika Sol", "Neon Harbor", "en", 24.0, ver_lines, status="running")
    vdir = os.path.join(A.JOBS, ver, "versions", "mias-version")
    os.makedirs(vdir)
    open(os.path.join(vdir, "out.mp4"), "wb").write(FAKE_MP4(24.0))
    json.dump({"name": "Mia's version", "mode": "version", "slug": "mias-version", "source": {"kind": "file", "filename": "mia.mp4"},
               "start": 0.0, "offset": 0.0, "trim_start": 0.0, "aligned": "manual", "fill": "hold", "lead": "hold",
               "made_at": CLOCK[0]}, open(os.path.join(vdir, "meta.json"), "w"))
    A.update_catalog()

    # ---- linking
    CLOCK[0] = T0 + 10 * DAY
    A.STORE.new_code()
    A.STORE.code["hash"] = encore.token_hash(CODE)
    pair = {"code": CODE, "name": "Encore on kit-01", "player": PLAYER, "encore": "0.4", "contract": "1"}
    kit = ("192.0.2.40", 50000)
    _, _, b = ex(1, "pair", "Linking: the host typed the Studio's address and the code it shows (no bearer).", "POST",
                 "/api/encore/pair", pair, "pair.request.schema.json", "pair.response.schema.json", auth=False, client=kit, expect=200)
    TOKEN = json.loads(b)["token"]
    ex(2, "pair-wrong-code", "A wrong, used or expired code (this one was used by the pairing above).", "POST", "/api/encore/pair",
       dict(pair, code="K7M3PQ9Y"), "pair.request.schema.json", err, auth=False, client=kit, expect=403)
    for _ in range(3):
        call(A.app, "POST", "/api/encore/pair", dict(pair, code="K7M3PQ9Z"), client=kit)
    ex(3, "pair-rate-limited", "More than 5 pairing tries a minute from one address.", "POST", "/api/encore/pair",
       dict(pair, code="K7M3PQ9Z"), "pair.request.schema.json", err, auth=False, client=kit, expect=429)
    ex(4, "hello", "Connection check (System view).", "GET", "/api/encore/hello", None, None, "hello.response.schema.json", expect=200)

    # ---- requests
    CLOCK[0] += 60
    req = {"rid": R[0], "artist": "Lumen Coast", "title": "Glass Orchard", "note": "the music video one", "by": "Mia",
           "party": "Friday at Ken's", "at": int(CLOCK[0])}
    ex(5, "request-new", "A song the library doesn't have.", "POST", "/api/encore/requests", req,
       "requests.post.request.schema.json", "requests.post.response.schema.json", expect=200)
    CLOCK[0] += 5
    ex(6, "request-duplicate", "The same rid again (a retry after a lost answer): nothing changes, the same answer.",
       "POST", "/api/encore/requests", req, "requests.post.request.schema.json", "requests.post.response.schema.json", expect=200)
    CLOCK[0] += 55
    ex(7, "request-have", "The Studio already has it (matched accent- and case-insensitively, either name order).", "POST",
       "/api/encore/requests", {"rid": R[1], "artist": "Paper Lanterns", "title": "nova lane", "note": "", "by": "Sam",
                                "party": "Friday at Ken's", "at": int(CLOCK[0])},
       "requests.post.request.schema.json", "requests.post.response.schema.json", expect=200)
    # (not kept) a request for a song being made: the answer says open, the polls say making, then published
    CLOCK[0] += 60
    call(A.app, "POST", "/api/encore/requests", {"rid": R[2], "artist": "Mika Sol", "title": "Neon Harbor", "note": "", "by": "Jo",
                                                 "party": "Friday at Ken's", "at": int(CLOCK[0])}, {"Authorization": "Bearer " + TOKEN})
    call(A.app, "POST", "/api/encore/requests", {"rid": R[3], "artist": "Iris Vale", "title": "Paper Boats", "note": "", "by": "Ana",
                                                 "party": "Friday at Ken's", "at": int(CLOCK[0])}, {"Authorization": "Bearer " + TOKEN})
    # the Studio's admin: "Find and make" for Glass Orchard (the Make page sends the rid with the new song)
    CLOCK[0] += 120
    st, _, b = call(A.app, "POST", "/api/jobs", {"yt": "bbbbbbbbbbb", "lrc_id": 2, "lyrics_source": "lrclib", "artist": "Lumen Coast",
                                                "title": "Glass Orchard", "request": R[0]})
    assert st == 200, b
    lumen = json.loads(b)["id"]
    A.set_job(lumen, status="running", step="separate", started=CLOCK[0])
    CLOCK[0] += 300
    st, _, b = call(A.app, "POST", f"/api/requests/{R[3]}/decline", {"reason": "No usable video yet"})
    assert st == 200, b
    # Neon Harbor is made, and published with its version
    CLOCK[0] += 300
    A.set_job(ver, status="done", step="done", finished=CLOCK[0])
    publish(ver)
    CLOCK[0] += 60
    st, _, b = call(A.app, "POST", f"/api/jobs/{ver}/videos/mias-version/publish", {})
    assert st == 200, b
    A.write_pitch(ver)
    CLOCK[0] += 240
    ex(8, "requests-by-rids", "Polling the requests still moving (up to 40 rids a call).", "GET",
       f"/api/encore/requests?rids={R[0]},{R[2]},{R[3]}", None, None, "requests.get.response.schema.json", expect=200)
    since = int(T0 + 10 * DAY + 400)
    ex(9, "requests-since", "Everything from this player updated after a time.", "GET", f"/api/encore/requests?since={since}",
       None, None, "requests.get.response.schema.json", expect=200)

    # ---- flags
    CLOCK[0] += 60
    vbase = A.load(ver)["published_as"].replace("(Music Video)", "(Mia's version)")
    vw = json.load(open(os.path.join(A.MV_DIR, vbase + ".words.json"), encoding="utf-8"))["lines"][1]
    ex(10, "flag-line", "Lyrics off while singing the custom version: t in that version's video time (a Studio version has the "
       "song's sound and timing), line = its words.json 'i'.", "POST", "/api/encore/flags",
       {"fid": FID[0], "id": ver, "base": vbase, "t": round(vw["start"] + 0.6, 2), "line": vw["i"], "kind": "late", "note": "",
        "by": "Sam", "party": "Friday at Ken's", "at": int(CLOCK[0])},
       "flags.post.request.schema.json", "flags.post.response.schema.json", expect=200)
    CLOCK[0] += 60
    ex(11, "flag-whole-song", "Flagged afterwards from Last songs: the whole song (no time, no line).", "POST", "/api/encore/flags",
       {"fid": FID[1], "id": duet, "base": A.load(duet)["published_as"], "t": None, "line": None, "kind": "words",
        "note": "second verse", "by": "Host", "party": "Friday at Ken's", "at": int(CLOCK[0])},
       "flags.post.request.schema.json", "flags.post.response.schema.json", expect=200)
    CLOCK[0] += 60
    ex(12, "flag-unknown-song", "A song Encore found by scanning the folders (an 'f-' id), which the Studio doesn't know by its base "
       "either.", "POST", "/api/encore/flags",
       {"fid": FID[2], "id": "f-0123456789ab", "base": "Someone - Something (Music Video)", "t": 40.0, "line": None, "kind": "early",
        "note": "", "by": "Jo", "party": "Friday at Ken's", "at": int(CLOCK[0])},
       "flags.post.request.schema.json", "flags.post.response.schema.json", expect=200)

    # ---- plays: earlier parties (not kept), then a batch
    sb, vb = A.load(solo)["published_as"], A.load(ver)["published_as"]
    old = [{"pid": U(f"old-{i}"), "id": solo, "base": sb, "at": int(T0 + DAY * (3 + i)), "seconds": 33, "result": "finished",
            "singers": 1, "style": None, "party": "Earlier"} for i in range(6)]
    old += [{"pid": U(f"old-v-{i}"), "id": ver, "base": vb, "at": int(T0 + DAY * (5 + i)), "seconds": 24, "result": "finished",
             "singers": 1, "style": None, "party": "Earlier"} for i in range(2)]
    old += [{"pid": U("old-skip"), "id": ver, "base": vb, "at": int(T0 + DAY * 8), "seconds": 4, "result": "skipped",
             "singers": 1, "style": None, "party": "Earlier"}]
    call(A.app, "POST", "/api/encore/plays", {"plays": old}, {"Authorization": "Bearer " + TOKEN})
    CLOCK[0] += 60
    plays = [{"pid": PID[0], "id": solo, "base": sb, "at": int(CLOCK[0]), "seconds": 33, "result": "finished", "singers": 1,
              "style": None, "party": "Friday at Ken's", "guide": "low"},
             {"pid": PID[1], "id": duet, "base": A.load(duet)["published_as"], "at": int(CLOCK[0]) + 60, "seconds": 9,
              "result": "skipped", "singers": 2, "style": "romaji", "party": "Friday at Ken's"},
             {"pid": PID[2], "id": ver, "base": vbase, "at": int(CLOCK[0]) + 120, "seconds": 18, "result": "stopped", "singers": 1,
              "style": None, "party": "Friday at Ken's", "uid": UID}]
    ex(13, "plays", "A batch (up to 200). Counted: finished, or stopped after more than half (the version's 18 of 24 s); the "
       "first one was sung with guide vocals (guide: low), the last one carries a user profile uid (§2.6, planned).", "POST", "/api/encore/plays", {"plays": plays},
       "plays.post.request.schema.json", "plays.post.response.schema.json", expect=200)
    ex(14, "plays-duplicate", "The same batch again (a retry): stored counts them again, nothing is added.", "POST",
       "/api/encore/plays", {"plays": plays}, "plays.post.request.schema.json", "plays.post.response.schema.json", expect=200)

    # ---- made, not published: Glass Orchard is made now
    CLOCK[0] += 600
    A.set_job(lumen, status="done", step="done", finished=CLOCK[0], result={"duration": 30.0, "lang": "en"})
    ex(15, "unpublished", "Made but not published (the host's view links to the Studio's web app).", "GET",
       "/api/encore/unpublished", None, None, "unpublished.get.response.schema.json", expect=200)

    # ---- the library for remote players
    CLOCK[0] = T0 + 11 * DAY
    A.update_catalog()
    for f in os.listdir(A.MV_DIR):  # the files' ETags use their times: the same ones on every run
        os.utime(os.path.join(A.MV_DIR, f), (T0, T0))
    _, rh, _ = ex(16, "catalog", "The catalog for a remote player (§2.0b); body = fixtures/library/catalog.json.", "GET",
                  "/api/encore/catalog", None, None, "catalog.schema.json", expect=200, body_file={"$file": "library/catalog.json"})
    ex(17, "catalog-not-modified", "Unchanged since the ETag the player has.", "GET", "/api/encore/catalog", None, None, None,
       headers={"If-None-Match": rh["etag"]}, expect=304)
    name = f"Music Videos/{sb}.mp4"
    ex(18, "file-range", "A file the catalog lists, with a range (§2.0b); body = the first 16 bytes of that file.", "GET",
       "/api/encore/files/" + name.replace(" ", "%20"), None, None, None, headers={"Range": "bytes=0-15"}, expect=206,
       body_file={"$file": "library/" + name, "$range": [0, 15]})
    ex(19, "file-not-in-catalog", "Only paths the catalog references are served.", "GET", "/api/encore/files/catalog.json.bak",
       None, None, err, expect=404)
    ex(20, "unlinked", "A token the Studio doesn't know (unlinked, or wrong): every call answers 401.", "POST", "/api/encore/plays",
       {"plays": plays[:1]}, "plays.post.request.schema.json", err, auth="fixture-token-revoked-00000000000", client=("192.0.2.41", 50001),
       expect=401)

    # ---- §2.2b auto-make (reserved): the Studio stores the fields; the progress shown here is set by hand (auto-make
    # itself comes in a later round)
    CLOCK[0] += 60
    ex(22, "request-auto", "§2.2b (reserved): a party with auto-make on; the guest is already queued with a placeholder. The Studio "
       "keeps auto and queue; until it makes songs by itself, it answers like any request.", "POST", "/api/encore/requests",
       {"rid": R[4], "artist": "June Arbor", "title": "Copper Moon", "note": "", "by": "Jo", "party": "Friday at Ken's",
        "at": int(CLOCK[0]), "auto": True, "queue": {"eid": "q3f9a1c2d4e", "uid": UID}},
       "requests.post.request.schema.json", "requests.post.response.schema.json", expect=200)
    call(A.app, "POST", "/api/encore/requests", {"rid": R[5], "artist": "June Arbor", "title": "Tin Lantern", "note": "", "by": "Jo",
                                                 "party": "Friday at Ken's", "at": int(CLOCK[0]), "auto": True,
                                                 "queue": {"eid": "q3f9a1c2d4f"}}, {"Authorization": "Bearer " + TOKEN})
    CLOCK[0] += 200
    A.STORE.set_request(R[4], status="making", job="20261111-212000-june-arbor-copper-moon", stage="making", eta=CLOCK[0] + 500)
    A.STORE.set_request(R[5], status="failed", reason="couldn't download the video", stage=None)
    A.refresh_requests = lambda: 0   # (the made-up auto-make job doesn't exist: keep the hand-set status)
    ex(23, "requests-auto-status", "§2.2b (reserved): auto-make progress (stage, eta), and one that failed (with a reason). "
       "Set by hand here: auto-make is built later.", "GET", f"/api/encore/requests?rids={R[4]},{R[5]}", None, None,
       "requests.get.response.schema.json", expect=200)
    # ---- §2.6 profiles: a second player (the home Encore) and one singer's profile, synced, exported, deleted
    CLOCK[0] += 60
    HOME = "8b9c0d1e-2f3a-4b4c-9d5e-6f7a8b9c0d1e"
    A.STORE.new_code()
    A.STORE.code["hash"] = encore.token_hash("H4N8RT2W")
    secrets.token_urlsafe = lambda n=32: "fixture-token-home-0123456789abc"
    st, _, b = call(A.app, "POST", "/api/encore/pair", {"code": "H4N8RT2W", "name": "Encore at home", "player": HOME,
                                                        "encore": "0.6", "contract": "1"}, client=("192.0.2.50", 50100))
    assert st == 200, b
    secrets.token_urlsafe = lambda n=32: "fixture-token-0123456789abcdef"
    HOME_TOKEN = json.loads(b)["token"]
    MASKED.add(HOME_TOKEN)
    EV = lambda k: U("profile-event-" + str(k))
    t = round(CLOCK[0], 3)
    mia = [{"eid": EV(1), "stream": "profile", "uid": UID, "at": t, "name": "Mia", "color": "#ff5fa2",
            "pin_hash": {"alg": "scrypt", "n": 16384, "r": 8, "p": 1, "salt": "00112233445566778899aabbccddeeff",
                         "hash": "a1" * 32}, "created": t, "deleted": False},
           {"eid": EV(2), "stream": "favorites", "uid": UID, "at": t + 12.5, "song": solo, "on": True},
           {"eid": EV(3), "stream": "prefs", "uid": UID, "at": t + 13.25, "key": "style:ja", "value": "romaji"},
           {"eid": EV(4), "stream": "history", "uid": UID, "at": t + 300.75, "song": duet, "base": A.load(duet)["published_as"],
            "result": "finished", "party": "Friday at Ken's", "style": "romaji", "guide": "low"}]
    sreq, sres = "profiles.sync.request.schema.json", "profiles.sync.response.schema.json"
    CLOCK[0] += 400
    _, _, b = ex(24, "profiles-sync-push", "§2.6: the kit sends a new singer's profile, a favourite, a lyric-style pref and a song "
                 "they sang (its first sync: since null). Nothing from other players yet; the cursor covers what it sent.",
                 "POST", "/api/encore/profiles/sync", {"player": PLAYER, "since": None, "events": mia}, sreq, sres, expect=200)
    kit_cursor = json.loads(b)["cursor"]
    _, _, b = ex(25, "profiles-sync-pull", "§2.6: the home Encore syncs: it gets the kit's events in the order the Studio stored "
                 "them (players merge them: latest (at, eid) wins per record, song or key; history is kept whole).",
                 "POST", "/api/encore/profiles/sync", {"player": HOME, "since": None, "events": []}, sreq, sres,
                 auth=HOME_TOKEN, expect=200)
    home_cursor = json.loads(b)["cursor"]
    ex(26, "profiles-sync-repeat", "§2.6: the same events again (a retry after a lost answer): stored once; the kit never gets "
       "its own events back.", "POST", "/api/encore/profiles/sync", {"player": PLAYER, "since": kit_cursor, "events": mia},
       sreq, sres, expect=200)
    ex(27, "profiles-export", "§2.6: everything the Studio keeps about one singer (\"Download my data\"): the winning "
       "favourite per song and pref per key, all history, the players that sent them; the PIN only as set or not.",
       "GET", f"/api/encore/profiles/{UID}/export", None, None, "profiles.export.response.schema.json", expect=200)
    CLOCK[0] += 600
    ex(28, "profiles-sync-delete", "§2.6: the singer deletes their profile on the home Encore: a tombstone. The Studio erases "
       "everything of that uid (also the uid on requests, flags and plays) and keeps only the tombstone.",
       "POST", "/api/encore/profiles/sync", {"player": HOME, "since": home_cursor, "events": [
           {"eid": EV(5), "stream": "profile", "uid": UID, "at": round(CLOCK[0], 3), "deleted": True}]}, sreq, sres,
       auth=HOME_TOKEN, expect=200)
    CLOCK[0] += 60
    ex(29, "profiles-sync-after-delete", "§2.6: the kit comes back with a late favourite for the deleted singer (dropped) and gets "
       "the tombstone, never the singer's other events.", "POST", "/api/encore/profiles/sync",
       {"player": PLAYER, "since": kit_cursor, "events": [{"eid": EV(6), "stream": "favorites", "uid": UID,
                                                          "at": round(CLOCK[0], 3), "song": ver, "on": True}]},
       sreq, sres, expect=200)
    ex(30, "profiles-export-deleted", "§2.6: a deleted singer exports as deleted, with nothing else.", "GET",
       f"/api/encore/profiles/{UID}/export", None, None, "profiles.export.response.schema.json", expect=200)
    ex(31, "profiles-sync-bad-cursor", "§2.6: a cursor this Studio didn't give (a rebuilt Studio): 400; Encore syncs again with "
       "since null.", "POST", "/api/encore/profiles/sync", {"player": PLAYER, "since": "s999999", "events": []}, sreq, err,
       expect=400)

    CLOCK[0] += 60
    ex(21, "unpair", "Unlink from Encore's side (the token stops working).", "POST", "/api/encore/unpair", {},
       "unpair.request.schema.json", "unpair.response.schema.json", expect=200)

    # ---- write
    lib_out, api_out = os.path.join(OUT, "library"), os.path.join(OUT, "api")
    for d in (lib_out, api_out):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.join(lib_out, "Music Videos"))
    os.makedirs(api_out)
    shutil.copyfile(os.path.join(A.LIB, "catalog.json"), os.path.join(lib_out, "catalog.json"))
    for f in sorted(os.listdir(A.MV_DIR)):
        if not f.startswith("."):
            shutil.copyfile(os.path.join(A.MV_DIR, f), os.path.join(lib_out, "Music Videos", f))
    for n, name, d in EXCHANGES:
        with open(os.path.join(api_out, f"{n:02d}-{name}.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.write("\n")
    print(f"wrote {len(EXCHANGES)} exchanges and {len(os.listdir(os.path.join(lib_out, 'Music Videos')))} library files to {OUT}")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(ROOT, ignore_errors=True)
