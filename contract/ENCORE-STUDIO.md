# Encore ⇄ Karaoke Studio contract (draft 1, 2026-11-20)

Karaoke Studio makes karaoke songs (video + lyrics) and publishes them to a library folder. Encore is the party app that
plays them. They are one project and must work perfectly together, but the Studio's output stays generic (plain .mp4 +
.ass + a documented catalog) so other players can use it too.

Two channels:
1. **Files (Studio → Encore):** the catalog and per-song files in the library. Read-only for Encore.
2. **API (Encore → Studio):** song requests, lyric flags and plays go to the Studio; statuses come back.

Both repos keep a copy of `contract/` (this file, JSON Schemas, fixtures). The Studio repo is the source of truth;
Encore's copy must match it (a test checks the copy's `CONTRACT_VERSION` and file hashes when the Studio repo is
available). Change the contract only in both repos together, with tests on both sides.

## 1. Files

`catalog.json` in the library root, as documented in `studio/catalog.py`, with these changes (catalog `"version": 2`,
additive only; Encore accepts 1 and 2 and ignores unknown fields):

- `"pitch": "<base>.pitch.json"` or null on each song and each version: the sung melody, for singing scores (§1.1).
- `"plays": n`, `"last_played": unix time or null`: plays counted by the Studio from every player that reports
  them (Encore via §2.4, PiKaraoke's own history), so a kit that syncs the catalog sees home numbers too.

- `"vocals": "<base>.vocals.m4a"` or null on each song and each version: the separated lead vocal (the track the
  Studio removed from the video), for guide vocals in players. AAC in .m4a, same timeline as the song's video
  (VIDEO time, trims applied), mono or stereo, ~128 kb/s. Versions share the song's audio, so a version's file is a
  copy (or the same content) of the song's. A catalog written before this field existed has no `vocals` key: read
  it as null.
- `"album"`, `"year"` (integer), `"genre"`, `"cover": "<base>.cover.jpg"`, each or null, on each song: its album
  details and cover art (the Studio looks them up; the person can correct or remove them). The cover is an image
  (JPEG, or PNG) next to the video, about 500-600 px square, for players to show instead of their own tile. A
  catalog written before these existed has no such keys: read them as null.

### 1.1 `<base>.pitch.json`

The lead vocal's melody, from the Studio's separated vocals, in VIDEO time (like the .ass and words.json).

```json
{"version": 1, "base": "...", "duration": 231.4, "hop": 0.02,
 "notes": [[12.34, 12.80, 62.1], ...],
 "range": [55.0, 74.0]}
```
- `notes`: sung segments `[start, end, midi]` (midi as a float: 60.0 = middle C, 0.5 = a quarter tone); silence and
  unvoiced parts are gaps. Segments are cut at word boundaries where words.json has them.
- `range`: the lowest and highest midi sung (for drawing and for octave-tolerant scoring).
- Missing file = no score for that song (Encore still plays it).
- Optional (added 2026-11-20 for singing scores): `"music": {"hop": 0.1, "db": [-32.5, ...]}`: the loudness of the
  song's INSTRUMENTAL track (the audio of the published video, mixed to mono) as RMS dBFS per `hop` seconds of video
  time (value k covers [k·hop, (k+1)·hop); the last one what's left), rounded to 0.5 dB, from -90 (silence: JSON has
  no -inf) to 0. Players use it to follow the music through the whole song when deciding whether a mic is hearing the
  singer or the music (not only in gaps). Missing = players fall back to learning the room in the gaps. `version`
  stays 1. A version's file has the song's values (the same sound).
- Notes shorter than about 60 ms are fragments; players may merge or ignore them. Players use `range` for drawing.

## 2. API

Base URL: `STUDIO_URL` in Encore's config. At home: `http://karaoke-studio:5570` over a private Docker network shared
only by the two containers (`karaoke_link`). On the kit: the home Studio over Tailscale, through a route that only
forwards `/api/encore/`.

Auth: every call sends `Authorization: Bearer <token>`. Each linked Encore has its OWN token (one per player: the
home Encore, Ken's kit, ...), issued by pairing (§2.0); the Studio stores only a hash of each, compares in constant
time, and records the player's name and when it was last seen. Optional preset for Docker setups: `ENCORE_TOKEN` in
the Studio's env is accepted as one more player ("preset", can't be unlinked from the UI). Wrong or unlinked token:
401, rate-limited. Bodies are JSON, at most 64 KB. Nothing else on the Studio changes its auth (the web app stays
behind its own login, e.g. Authentik).

### 2.0 Linking (pairing)
Made for self-hosters: nobody copies keys around.
1. In the Studio web app (admins only), "Connect a player" shows a one-time pairing code (8 characters, no look-alike
   letters, valid 10 minutes, one use) and the address to type into Encore.
2. In Encore (station → System → Karaoke Studio, host only; also the phone's host area), the host types the Studio's
   address and the code, and a name for this player (default: "Encore on <hostname>").
3. Encore calls `POST /api/encore/pair` `{"code": "...", "name": "Ken's kit", "player": "<Encore instance uuid>",
   "encore": "<Encore version>", "contract": "<CONTRACT_VERSION>"}` (no bearer) → `{"token": "...", "studio":
   "<version>", "contract": "...", "studio_name": "...", "studio_url": "<web app URL>"}`. Wrong/expired code: 403;
   pairing attempts are rate-limited per address (5/min) and a code dies after 5 wrong tries.
4. Encore stores the token in its own data dir (not .env), never shows it, and shows "Linked to <studio_name>".
   "Unlink" in Encore forgets it and calls `POST /api/encore/unpair`; "Unlink" in the Studio's "Linked players"
   list (name, last seen, Encore version, outbox backlog last reported) revokes it: that Encore then sees 401 and
   shows "This Studio unlinked us; link again".
Re-pairing the same `player` uuid replaces its old token. `STUDIO_URL`/`STUDIO_TOKEN` in Encore's env remain an
optional preset (shown as "set by the server config" and not editable in the UI).

Contract mismatch: `hello` and `pair` return the Studio's contract version; Encore shows "The Studio is newer/older
than this Encore: update one of them" when the major numbers differ, and keeps working for anything both support.

### 2.0b Library files for remote players
An Encore on the same machine reads the library folder directly (`LIBRARY_DIR`). A linked Encore elsewhere (the
kit) syncs it over the API:
- `GET /api/encore/catalog` → catalog.json (ETag / If-None-Match).
- `GET /api/encore/files/<path>` → a file listed in the catalog (mp4, ass, words.json, pitch.json, vocals.m4a), with HTTP range
  support and ETag; only paths the catalog references are served.
The kit downloads new or changed files into its own library when online (built 2026-11-21: Encore's library sync,
its README "Library sync"): only paths the catalog names, each with If-None-Match / Range + If-Range on its ETag.

Every write carries a client-made id (`rid`, `fid`, `pid`: a UUID made by Encore) so retries are safe: sending the
same id twice changes nothing and returns the same answer. Encore keeps unsent items in an outbox and retries with
backoff (1 min, doubling, max 1 h) until the Studio answers 2xx; this is how a kit that's offline catches up later.

### 2.1 `GET /api/encore/hello`
→ `{"api": 1, "contract": "<CONTRACT_VERSION>", "studio": "<code version>", "catalog_version": 2}`
Encore shows "Studio connected" / "Studio not reachable" in the station's System view from this.

### 2.2 Song requests
`POST /api/encore/requests`
```json
{"rid": "uuid", "artist": "Spice Girls", "title": "Wannabe", "note": "the music video one", "by": "Mia",
 "party": "Friday at Ken's", "at": 1795200000}
```
→ `{"rid": "uuid", "status": "open" | "have" , "id": "<job id if have>", "base": "<base if have>"}` (auto-make, §2.2b:
also `failed` with `reason`, and `auto`, `stage`, `eta`)

Statuses (the Studio owns them; Encore polls):
| status | meaning |
|---|---|
| `open` | waiting in the Studio's Requests list |
| `making` | a song was started from it (`job` set) |
| `ready` | made, not published yet |
| `published` | in the catalog (`id`, `base` set) |
| `have` | the Studio already had it when it arrived (`id`, `base`) |
| `declined` | the Studio said no (`reason`, short text) |
| `failed` | party auto-make couldn't make it (`reason`; §2.2b) |

`GET /api/encore/requests?rids=a,b,c` (or `?since=<unix>`) → `{"requests": [{"rid", "status", "job", "id", "base",
"reason", "updated"}]}`

Studio side: a Requests list (who asked, when, at which party, the note) with "Find and make" (opens Make with the
search filled in; the job remembers the rid, so the status follows the job), "We have it" (pick the song) and
"Decline". Matching on arrival is accent- and case-insensitive, either name order.
Encore side: the phone shows "You asked for X: being made / ready soon / it's here!" and the TV can show "New: X
(asked for by Mia)" when it becomes published.

### 2.2b Party auto-make (built 2026-11-20: the Studio and Encore)
"Be on screen at the right time is better than nothing": while a party is on, a guest's request for a song the
library doesn't have can be made and published by the Studio by itself in a few minutes (a make takes ~2 min on an
RTX 5090, ~3 on a 3090), and the guest keeps their place in the queue meanwhile.

**Who decides.** Two switches, both needed:
- The Studio's admin (web app, Players tab): **Party auto-make** on, with limits (songs per hour, default 6; per day,
  30; longest song, 7 min; only when a graphics card is free or will be within N minutes, 10; the quality floor, 60%),
  and per linked player **may auto-make** (off by default). It's a Studio feature, not an agent: no agent key can
  start it or change it.
- The Encore host, per party: **Auto-make missing songs** (Party settings -> Requests), offered only while the
  Studio says this player may (`hello.auto_make.allowed`).

**hello** (§2.1) carries `"auto_make": {"allowed": bool, "minutes": n | null}`: may this player ask now, and about
how many minutes a song asked for now takes until it's published (from the Studio's queue and its measured make
times). A Studio from before auto-make leaves it out: not allowed.

**The request** carries `"auto": true` and `"queue": {"eid": "<Encore's queue entry id>", "uid": "..."}` (the guest
is already queued with a placeholder). The POST answer says what the Studio did with it:
- taken: `{"status": "open", "auto": true, "stage": "finding", "eta": <unix>}`;
- taken but over a limit (or no card free soon): `{"status": "failed", "auto": true, "reason": "..."}` at once (the
  request stays in the Studio's Requests list; its admin can reopen it as a normal request);
- not taken (the admin's setting or the player's toggle is off): a normal answer, **without `auto`**; the request is
  an ordinary one;
- the Studio has it: `have`, as always (never made again, never replaced).

**Taken requests** answer polls with `auto: true`, `stage` (`finding` the video and lyrics, `downloading`, `making`,
`publishing`; null when nothing runs) and `eta` (unix time it should be published, to 10 s; null when not moving).
The Studio searches as the Make page does and picks the video, the lyrics and the names with the Make page's own
rules (`autopick.py`, a copy of the page's code that a test keeps identical), then makes the song. They end as:
- `published` (`id`, `base`): it cleared the quality floor (60% of the lines heard sung where they sit, the 10th
  percentile of the songs people had published; a song the by-ear check couldn't follow never goes out on its own;
  no more than one line crammed into too little time); in the Studio it's marked **quick make, not reviewed** until a
  person checks it;
- `ready` with `reason` "needs a look": made, below the floor; it waits for a person;
- `have` (`id`, `base`): the song turned out to be in the library under the names the lyrics give it;
- `failed` with `reason`, e.g. "couldn't search for it (YouTube or the lyrics sites didn't answer)", "no music
  video found", "no timed lyrics found", "too long for a quick make (9:05; the limit is 7 min)", "couldn't download
  the video", "lyrics didn't match the singing", "it was stopped at the Studio".
Auto-made songs are signed "auto-make for <player name>" (made and published by).

**Encore** keeps the guest's place with a placeholder entry that shows the stage and ETA (phone, station, TV up
next). A placeholder never goes on stage: when it reaches the front unready, it moves down one spot (once per turn,
and the singer is told). `published`/`have` and the song in the library: the placeholder becomes a normal entry.
`failed` or `ready`: the placeholder goes and the asker is told why ("the Studio made it but it needs a look first").
A request the Studio didn't take: the placeholder goes, the request stays in "Your requests". While any auto request
moves, Encore polls every 15 s.

### 2.3 Lyric flags
`POST /api/encore/flags`
```json
{"fid": "uuid", "id": "<catalog song id = job id>", "base": "<the version played>", "t": 83.4, "line": 17,
 "kind": "late" | "early" | "words" | "missing" | "other", "note": "", "by": "Sam", "at": 1795200000}
```
→ `{"fid", "status": "marked" | "noted" | "unknown_song"}`
- `t` is video time of the version played; `line` is the words.json line index if Encore knows it.
- The Studio puts a ⚠ mark on that line (or the line sung at `t`) with the note
  "Encore: <kind> (Sam, Friday at Ken's)". It never changes timing or words by itself. Several flags on one line
  stack into one mark with a count. If the job no longer exists: kept in a flags list (`noted`).
- The song page shows "Flagged in Encore (n)"; the Songs list's "Needs fixing" filter includes them.

### 2.4 Plays
`POST /api/encore/plays`
```json
{"plays": [{"pid": "uuid", "id": "<song id>", "base": "<version>", "at": 1795200000, "seconds": 212,
            "result": "finished" | "skipped" | "stopped", "singers": 2, "style": "romaji", "party": "Friday"}]}
```
Optional `"guide": "off" | "low" | "medium" | "full"`: the guide vocals level the song was sung with (§1 `vocals`;
left out when the song had none). The Studio may ignore it.
Optional `"score"` (added 2026-11-20): the singing score when the play was scored, same object as in §2.6 history
(`{"v": 1, "difficulty", "points", "pitch", "timing", "words"?, "acc", "grade", "max_combo", "partial"?}`); the
Studio stores it with the play (it may show it, e.g. best grade per song) and never needs it.
→ `{"stored": n}` (up to 200 per call). The Studio's Library "Sung" column and the catalog's `plays` count Encore
plays (finished, or stopped after more than half) plus PiKaraoke's history.

### 2.5 Made, not published
`GET /api/encore/unpublished` → `{"songs": [{"job", "artist", "title", "made"}], "studio_url": "<web app URL>"}`
Encore's host view shows "N songs are made but not published" with a link to the Studio (which asks for its own
login). Encore never publishes.

### 2.6 Profiles (built 2026-11-20: Encore and the Studio)
Roles: Encore runs everything locally (the kit works a whole party offline); the Studio makes/edits songs, keeps the
master copies, and is home base: it backs up Encore's user profiles and syncs them between Encores (home, Ken's kit).

Encore accounts: **guests** (a name for one party, nothing kept) and **users** (persistent profile). Encore users are
not Studio/Authentik accounts. A profile: `{"uid": uuid, "name", "color", "pin_hash" (salted scrypt of a 4-digit
profile PIN, optional), "created", "deleted": bool}` plus event streams:
- `favorites`: `{"eid", "uid", "song": id, "on": bool, "at"}` (latest `at` wins per song)
- `history`: `{"eid", "uid", "song", "base", "at", "result", "party"}` (append-only; plays in §2.4 carry `uid` when
  a user sang)
- `prefs`: `{"eid", "uid", "key": "style:ja"|..., "value", "at"}` (latest `at` wins per key)
- `history` events may carry an optional `"score"` (added 2026-11-20; schema `$defs/score`, the same in the plays and
  profile schemas): `{"v": 1, "difficulty": "easy"|"normal"|"hard"|
  "expert", "points": int, "pitch": 0-100, "timing": 0-100, "words": 0-100 (optional, §2.9), "acc": 0-100,
  "grade": "AAA"|"AA"|"A"|"B"|"C"|"D", "max_combo": int, "partial": bool (optional: the song was skipped/stopped)}`.
  Scores sync with the rest of the user's history (a user's best grades follow them between kits).
- later: `range`.

`POST /api/encore/profiles/sync` `{"player": uuid, "since": <studio cursor or null>, "events": [...]}` →
`{"cursor": "...", "events": [events from other players since the cursor]}`. Events are idempotent by `eid`; merging
is per stream as above, so two Encores editing offline never overwrite each other. Deleting a profile sends a
`profile` event with `deleted: true`; the Studio then erases that user's events from every stream and tells every
player on its next sync. `GET /api/encore/profiles/<uid>/export` → everything stored about that user (for "download
my data"). The Studio lists profiles (names, last seen, which players) for admins, no history browsing by default.

Built as Encore proposed (its PROPOSED.md, 2026-11-20), with the schemas `profile-event.schema.json`,
`profiles.sync.request.schema.json`, `profiles.sync.response.schema.json`, `profiles.export.response.schema.json` and
fixtures 24–31. What the Studio does:
- Stores each event once (by `eid`) with a growing sequence number and the sending player; relays events to the other
  players in that order, never back to the sender, at most 500 per answer (`more: true` when cut). It doesn't merge on
  the way: players merge by `(at, eid)` (latest wins per profile record, per (uid, song), per (uid, key); history whole).
- The cursor is opaque (`"s<number>"`). One it didn't give (or beyond what it holds: a rebuilt Studio) answers 400;
  Encore syncs again with `since: null`.
- `player` in the body must be the token's player (403). The preset player (`ENCORE_TOKEN`) has no uuid of its own on
  the Studio: any uuid is accepted, and its events are the preset's.
- A body that fails the schema (one bad event) answers 400 for the whole call and stores nothing (other `/api/encore/`
  bodies answer 422; §2.8.7 treats 400/413/422 alike).
- A tombstone erases every event of that uid, clears the uid on the requests, flags and plays it sent (they stay), is
  kept (eid, stream, uid, at, deleted, merged_into only) with a new position so every other player gets it, and makes
  the Studio drop later events for that uid (a second tombstone included).
- Export: unknown uid 404; deleted `{uid, deleted: true, profile: null, [] …}`; `profile.updated` = the winning
  record's `at`; `pin` only as true/false, never the hash. Admins see a list (Players tab → Singer profiles: name,
  colour, last activity, which players, PIN set); no history.

### 2.9 Words scoring (added 2026-11-20)
The third part of the singing score ("did they sing the actual words"), done by the Studio's GPU after the song, since
a kit's mini PC can't run the aligner quickly. Optional: without a linked Studio (or when it's busy/offline) a score
simply has no `words` part.

`POST /api/encore/score-words` (bearer token as §2): body = the singer's mic recording of one song as Opus audio
(`Content-Type: audio/ogg` or `audio/webm`, codec parameters allowed, mono, ≤ 48 kb/s, at most 8 MB — the only
`/api/encore/` body allowed above 64 KB), with headers (schema `score-words.post.headers.schema.json`):
- `X-Encore-Song`: the song id; `X-Encore-Base`: the version played (its words.json timing is used);
- `X-Encore-Start`: the video time (seconds, float, -60 to 86400) at the first audio sample, after the player's
  latency correction;
- `X-Encore-Rate`: playback rate (0.5 to 2.0) if a speed-up surprise was active from the start (1.0 normally; a
  recording whose rate changed mid-song is cut into separate calls or not sent);
- `X-Encore-Lines` (optional): comma-separated words.json `i` values to score (duets: the singer's own lines; `i`
  values the Studio's words.json doesn't have are ignored). Left out: every line but backing lines.
→ `202 {"job": "<id>", "eta": seconds, "status": "queued"|"busy"}`; then `GET /api/encore/score-words/<job>` →
`{"status": "queued"|"running"|"done"|"failed"|"busy", "words": [{"line": i, "w": index, "ok": bool, "conf": 0-1}],
  "heard": n, "total": n, "score": 0-100, "reason"?}`. `busy` = the Studio won't score now (no GPU free soon); the
player stops asking. Results are kept for 10 minutes, then forgotten.

Settled while building it (2026-11-20, the Studio):
- Fields by status: `queued`/`running`: `eta`; `done`: `words`, `heard`, `total`, `score`; `failed`/`busy`: `reason`.
  A `busy` job keeps nothing (the 202 already says `busy` when the Studio knows it at once).
- `line` = the words.json line's `i`; `w` = the word's index in that line's `words` (0-based).
- `words` lists only the words judged; `total` counts them, `score` = round(100 · heard / total). Not judged (left out,
  nobody gains or loses by them): words without letters, words not wholly inside the recording, and words the song's
  own separated vocals don't pass clearly or its instrumental alone already passes (see below). A recording with no
  word to judge ends `failed` ("no words to score in this recording").
- Errors: a missing or wrong header, a song or base the catalog doesn't have, none of `X-Encore-Lines` in the song, an
  empty body: 422 (checked before the body is read); over 8 MB: 413 (counted as the body arrives); more than 6
  recordings a minute from one player, or 3 of its recordings still waiting: 429 with `Retry-After`.
- A job is only the sending player's. An unknown job, another player's, or one forgotten after 10 minutes: **410**
  (not 404, which §2.8.6 keeps for "no player API here").

How the Studio scores: forced alignment of the expected words (the same kind of model it uses to time songs: torchaudio
MMS_FA) against the recording, windowed around each word's expected time ± 0.8 s (video time, mapped with Start and
Rate), each line heard once with 1 s of context; a word's confidence = the mean probability of its letters where they
were placed × how much each letter beats the best other letter there; a word counts as sung at 0.25 and above. Only
words the song's own separated vocals reach 0.35 on and its instrumental stays under 0.25 on are judged (worked out
once per song, cached in its job folder). Calibrated on eight published songs (five English, three Japanese): the
original vocals score 100 and the instrumentals 0 (by construction), another song's instrumental 1-9 (mean 5),
silence 0, the vocals with the instrumental 10 dB under them 87-99 (mean 93), through Opus 40 kb/s webm 95-100 and
24 kb/s ogg 93-99, recorded from 30 s in 100, at rate 1.25 82-99 (mean 95), the vocals against another song's words
8-26 (mean 18). About 70% of a Japanese song's words are judged and 30% of an English one's. It runs after anything
else and never delays a song being made: on the GPU only when no song is being made or waiting there and the card
keeps room for one (about 1 s for a 4-minute song, plus about 4 s to start), else on the CPU (about 30 s; 85 s the
first time a song is scored), else `busy`. The aligner leaves the GPU a few seconds after its last job.

Privacy (owner's choice): Encore only sends recordings when its host setting "Score the words (sends singers' audio to
the Studio)" is on. The Studio keeps the audio in memory only while scoring (it reaches the decoder and the aligner
through pipes; no temp file), never stores it with the song, never logs it, and drops it as soon as the job is done,
fails, is found busy or waits too long (10 minutes).

### 2.7 Transports and sync bundles
The Studio can be any PC (Linux, Windows, macOS with Docker; an NVIDIA GPU only for making songs) and doesn't have to
be on all the time; the kit works disconnected. So syncing doesn't depend on how they connect:
- **Network:** LAN, Tailscale or any VPN. On a LAN, the Studio announces itself over mDNS
  (`_karaoke-studio._tcp`, TXT: name, contract, api URL) so Encore can offer it without typing an address. Songs come
  down with §2.0b; requests, flags, plays and profiles go up with §2.2–2.6.
- **Direct cable:** the kit's Ethernet port (or USB-C in network/gadget mode) runs DHCP; a PC plugged in gets an
  address and syncs as on a LAN. Joining the kit's hotspot works the same way.
- **USB stick (no network at all):** sync bundles, below (built 2026-11-21).

**Sync bundles.** One file each way, carried on a stick: the same data as the API, nothing more.
- **Studio → player:** the Studio's web app, Players → *Export for <player>* (admin only, never on `/api/encore/`):
  a download `<player>-<YYYYMMDD>[-part-<i>-of-<n>].karaoke-sync.tar`. Copied to the stick; the player imports it.
- **Player → Studio:** the player writes `<player>-to-<studio>-<YYYYMMDD-HHMM>.karaoke-sync.tar` to the stick; the
  Studio's web app imports it (Players → *Import a bundle*, an upload).
- **Profiles (§2.6) and words scoring (§2.9) don't travel by stick:** profile events carry PIN hashes and recordings
  are never stored. They wait for the network.

**The file:** an uncompressed POSIX tar (ustar; member names under 100 bytes, no extended or GNU headers, so its size
is known before it's written). Members, in this order:
1. `manifest.json` (at most 4 MB);
2. `manifest.sig`: 64 lower-case hex digits, HMAC-SHA256 over the exact bytes of `manifest.json` with the key
   `K = HMAC-SHA256(key = bytes.fromhex(sha256(token)), msg = "karaoke-sync-bundle-v1")`. `token` is the player's
   pairing token (§2.0): the player has it, the Studio keeps its sha256 (hex), so both can make K and nobody else can.
   K, the token and its hash are never shown or logged;
3. the other members the manifest names, each once (in any order).

A reader refuses the whole file unless: the first two members are those, the signature is right for its link, the
manifest's `bundle` is 1, its contract major matches, and `player` / `studio_id` are this link's. Then it reads the
rest as a stream (never extracts the tar): only regular files with exactly the names the manifest lists, each exactly
its `size` and `sha256`; a link, a device, an unlisted or a duplicate name, or a size over the cap refuses the file
(what was applied before that point stays applied: every step is idempotent). Caps: manifest 4 MB, `catalog.json`
16 MB, `answers.json` and `calls.json` 8 MB, a library file 4 GB. A file made for an older link (re-paired since) is
refused as "made for an old link: export it again".

**Studio → player** (`kind: "studio"`):
```
manifest.json  {"bundle": 1, "kind": "studio", "contract": "1", "studio_id": uuid, "studio_name": str, "player": uuid,
                "made": unix, "part": [i, n],
                "catalog": {"member": "catalog.json", "size", "sha256", "generated": the catalog's "generated"},
                "answers": {"member": "answers.json", "size", "sha256"},
                "files": [{"path": library path (§2.0b), "size", "sha256", "etag": the §2.0b ETag of that file,
                           "member": "f/<sha256>" or null}]}
catalog.json   the full catalog (§1), in every part
answers.json   {"requests": [as GET /api/encore/requests: this player's requests updated in the last 90 days],
                "unpublished": {as GET /api/encore/unpublished},
                "applied": {"requests": [rid], "flags": [fid], "plays": [pid]}}   (ids from this player the Studio has
                                                                                    stored, last 90 days)
f/<sha256>     a library file's bytes (several paths with the same bytes: one member)
```
- `files` lists every file of every song in the catalog (the §2.0b set). `member` is null for one this part doesn't
  carry: the Studio leaves out what the player already has (its last `have`, below) unless the admin asks for
  everything, and a part carries whole songs only (parts for FAT32 sticks: at most ~3.9 GB each; every part is a
  complete, valid bundle with the same catalog and answers).
- The player imports the songs whose files are all then on its drive (carried in this part, or already there with that
  sha256 or that ETag), with §2.0b's rules: staged, moved in per song, `catalog.json` lists only complete songs, the
  ETags recorded so the next network sync downloads nothing again. A catalog older (`generated`) than the newest it has
  applied (from the network or a bundle) leaves its catalog and deletions alone; its files and answers still apply.
- `answers` apply as if polled: request statuses as §2.2, made-not-published as §2.5, and every id in `applied` leaves
  the player's outbox (sent).

**Player → Studio** (`kind: "player"`):
```
manifest.json  {"bundle": 1, "kind": "player", "contract": "1", "studio_id": uuid, "player": uuid, "player_name": str,
                "made": unix, "calls": {"member": "calls.json", "size", "sha256"}}
calls.json     {"requests": [POST /api/encore/requests bodies], "flags": [POST /api/encore/flags bodies],
                "plays": [the items of POST /api/encore/plays], "have": {library path: §2.0b ETag, ...}}
```
- Writing a bundle sends nothing: items stay in the player's outbox until the Studio's next bundle lists them in
  `applied`, or the network delivers them (either way by their ids, so twice changes nothing). Plays held for a words
  score (§2.9) wait until they're released.
- The Studio applies them with the same handlers as the API, as that player, except that requests lose `auto` and
  `queue` (§2.2b: a party is long over by the time a stick comes home: no song is made by itself). It keeps `have` for
  that player's next export.
- Schemas: `schemas/bundle.manifest.schema.json` (both kinds), `bundle.answers.schema.json`,
  `bundle.calls.schema.json`. Fixtures: `fixtures/bundles/` (made by the Studio's own code with the fixture token).

### 2.8 Clarifications (settled 2026-11-20 after Encore's first build)
1. Flag `line` = the words.json line's `i` value exactly as written there (1-based). `t` and `line` may both be null =
   a flag on the whole song (sent from "Last songs" after it played).
2. Flags carry an optional `party` (the Studio's mark text uses it) and optional `uid`.
3. `noted` = the song id/base is known but its job is gone (kept in the Studio's flags list); `unknown_song` = neither
   the id nor the base is known. Songs Encore found by scanning (ids starting `f-`) are matched by `base`.
4. Plays answer `stored` = how many of that call's plays the Studio now holds (a retried batch gets the same number);
   any 2xx = all delivered. `plays`/`last_played` are per song (versions add up into their song). `seconds` = video
   position reached (the song's length if finished). `style` null = only the plain .ass was used.
5. A repeated `rid`/`fid`/`pid` gets the first answer again. Poll answers leave out unknown rids.
6. Unpair → `{"ok": true}`; an already-revoked token → 401. Error bodies are `{"error": "<short text>"}`; clients
   never rely on the text. 404 on `/api/encore/*` = no player API at that address (Encore backs off).
7. `stage` may be null when nothing is running; `failed` always has a `reason`; `ready` may have a `reason`
   ("needs a look", auto-make only). Encore's own state for an item the Studio refused (400/413/422) is `rejected`.
8. v2 catalog: `pitch`, `plays`, `last_played` present on every song (null allowed), `pitch` on every version.
9. **Studio identity:** `hello` and `pair` return `"studio_id"` (a uuid the Studio makes once). Every outbox item is
   tied to the studio_id it was made for: re-linking the same Studio drains them; linking a DIFFERENT Studio keeps
   them aside (shown as "N items for <old studio name>") and never sends them to the new one.
10. Contract versions compare by major (the part before the first dot); a mismatch warns but keeps working.
11. Default player name: "Encore on <PUBLIC_URL host>" (inside Docker the hostname is a container id).
12. Outbox backlog (for the Studio's "Linked players" list): any call may carry `X-Encore-Outbox: <n>` (items waiting
    to be sent); the Studio shows the last number reported. It reads the Encore version from
    `User-Agent: Encore/<version> contract/<CONTRACT_VERSION>` (both optional; pairing reports them first).
13. A Studio version (a custom video) plays over the song's own sound and timing: its words.json has the song's times
    (only `base` differs), so a flag's `t` on a version maps to the same line as on the song.

## 3. Tests on both sides
- `contract/schemas/*.json`: JSON Schemas for catalog.json v2, words.json, pitch.json, and every API body/answer.
- `contract/fixtures/`: a small sample library (catalog + words + pitch for 3 songs: a solo, a duet with parts, one
  with a custom video version) and sample API exchanges.
- Studio: catalog.py output and every `/api/encore/` handler validate against the schemas; the fixtures are
  generated by the Studio's own code (a script regenerates them).
- Encore: loads the fixture library and plays through the API fixtures against a fake Studio; outbox retry and
  idempotency tests.

## 4. PiKaraoke
Publishing keeps working without PiKaraoke: the PiKaraoke rescan and play history are optional (skipped cleanly when
not configured), so the Studio can serve Encore alone once PiKaraoke is retired.
