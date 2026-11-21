# Karaoke Studio

![Karaoke Studio: search, preview with word-by-word lyrics, and the waveform lyrics editor](docs/demo.gif)

<sub>The video and thumbnails are blurred in this demo for copyright; in the app they show normally.</sub>

Turn an official music video into a karaoke video, with the vocals removed and the lyrics lighting up word by word.

Karaoke Studio and [Encore](https://github.com/alainator/encore), the party app, connect to each other: guests' requests reach the studio, and the songs it publishes reach the party. Neither needs the other, and the songs are plain video and subtitle files (`.mp4` + `.ass`) that other karaoke apps can play too.

Type `Artist - Title`. The studio finds the music video on YouTube and the lyrics on [LRCLIB](https://lrclib.net), removes the vocals on your GPU, and times every word to the singing. You preview it in the browser, fix anything you want, and publish.

## What it does

- **Finds the right video and lyrics.** Official music videos are preferred over lyric videos, live versions, covers and dance practices. Each suggestion can be opened first: watch the video, read the lyrics. No lyrics online? Paste them from any site; if LRCLIB has a timed version, your words get its timing.
- **Times the lyrics by listening.** It finds the album recording and matches it against the video's music, so skits, scenes in the middle and cuts are found exactly. Talking keeps its sound; only the singing is removed. Backing vocals in brackets get their own line, so they can overlap the lead.
- **Fixes lyric mistakes.** An optional local AI (via [Ollama](https://ollama.com)) catches dropped or swapped words and wrong Japanese readings, and keeps a change only if it fits the singing better.
- **Lets you edit easily.** A waveform or spectrogram of the vocals with a block per line: drag, stretch, or tap **S** as each line is sung, and starts snap to where notes begin. Slow playback to 0.5×, loop a line, split or join lines.
- **Tells you what to fix.** Every song is checked by ear after it's made: lines heard somewhere other than where they sit are marked ⚠ with the reason, the song page lists them, and the song list shows how many each song has left.
- **Colours singers.** Duets, groups of up to 8 named singers, and lines shared mid-way (paint the words of each singer). Pasting a Genius page with headers like `[Chorus: Name]` colours the lines for you.
- **Remakes safely.** Older songs show what a remake would improve. A remake keeps your edits, changes nothing if it finds nothing better, and can be undone.
- **Works with an agent, or without.** An AI agent (Claude Code, a local model, anything that can run a command) can do the first pass of checking and fixing through `ks`, with only the permissions you give it; every edit is signed and undoable. Or do it all by hand.
- **Uses a second GPU.** A PC on your network can take songs when the main GPU is busy, or you can choose which GPU makes each song.
- **Party auto-make.** With your OK (per linked player), a party's request for a missing song is found, made and published by the studio in a few minutes, picked the way the Make page picks; songs below a quality floor wait for a look, and quick makes are marked until someone checks them.
- **Works with Encore.** Link the Encore party app (or any player that speaks [the contract](contract/ENCORE-STUDIO.md)): guests' song requests land in a Requests list, lyric flags from the party mark the lines to fix, and what was sung counts in the Library. Each published song also gets its sung melody (`.pitch.json`, with the instrumental's loudness) for singing scores and its separated vocals (`.vocals.m4a`) for guide vocals; the studio can also score whether a singer sang the words, and the Library shows each song's best grade.

## Languages

| Language | How the lyrics are shown |
|---|---|
| English and other Latin-alphabet languages (Spanish, French, Vietnamese…) | As written |
| Japanese | Romaji with the Japanese line above it: hand-made from [kara.moe](https://kara.moe) when available, otherwise automatic and checked by listening |
| Korean | Romanized the way it's pronounced, with the Korean line above it |
| Chinese | Pinyin (Mandarin) or jyutping (Cantonese) with the Chinese line above it. Cantonese is picked up when the lyrics are written in spoken Cantonese; otherwise one click on the song page switches, keeping the timing |
| Russian, Ukrainian and other Cyrillic; Greek | Romanized, with the original line above it |
| Hindi and other Indian scripts, Thai, Georgian, Armenian, Lao, Khmer, Burmese, Amharic | Romanized, with the original line above it (the song carries the font it needs, so the TV can show it) |
| Arabic, Persian, Urdu, Hebrew | In their own script, words lighting up right to left |
| Mixed (an English song with a chorus in another script) | Each line as above |

Tested with one song each in Russian, Ukrainian, Greek, Hindi, Thai, Arabic, Hebrew and Cantonese; the other scripts use the same path.

## Local models, no cloud AI

Making a song needs no API keys and sends nothing to an AI service: every model runs on your own GPU (the internet is
used only to fetch the video and the lyrics). The optional agent (see *Finishing songs*) is the only piece that may be a
cloud service, and only if you choose one.

| Step | Model | Where |
|---|---|---|
| Vocals from music | BS-RoFormer (`model_bs_roformer_ep_317_sdr_12.9755.ckpt`, via [audio-separator](https://github.com/nomadkaraoke/python-audio-separator)) | pipeline |
| Hearing the words | Whisper large-v3 (via [stable-ts](https://github.com/jianfch/stable-ts)) | pipeline |
| Placing every word | MMS forced aligner (`torchaudio.pipelines.MMS_FA`) | pipeline, AI checks |
| Talking vs singing | AST AudioSet classifier (`MIT/ast-finetuned-audioset-10-10-0.4593`) | pipeline |
| Held notes, melody | CREPE (torchcrepe: tiny in the pipeline, full for `.pitch.json`) | pipeline, publish |
| Who sings (experimental) | SpeechBrain ECAPA (`speechbrain/spkrec-ecapa-voxceleb`) | Detect 2 singers |
| Lyrics and romaji checks (optional) | any [Ollama](https://ollama.com) model (`OLLAMA_MODEL`) | AI checks |

TODO before a release: each model's weights licence, download size and VRAM, checked against its model card (not yet
verified; don't rely on this table for licensing).

| Model | Weights licence | Download | VRAM |
|---|---|---|---|
| BS-RoFormer | TODO | TODO | TODO |
| Whisper large-v3 | TODO | TODO | TODO |
| MMS_FA | TODO | TODO | TODO |
| AST AudioSet | TODO | TODO | TODO |
| CREPE (torchcrepe) | TODO | TODO | TODO |
| ECAPA (SpeechBrain) | TODO | TODO | TODO |

## How it works

1. [BS-RoFormer](https://github.com/nomadkaraoke/python-audio-separator) splits the vocals from the music.
2. Whisper ([stable-ts](https://github.com/jianfch/stable-ts), large-v3) transcribes the vocals, and the lyrics are matched against what was actually sung. That gives each line its place in the video, including where the video jumps.
3. The album recording is found on YouTube and matched against the video's music (librosa), giving an exact map of skits, scenes and cuts.
4. A forced aligner (torchaudio MMS) places every word. Lines that fit badly are marked for you to check.
5. The result is an `.ass` subtitle on two alternating rows, so the next line shows before it's sung.

## Requirements

- Linux with an NVIDIA GPU (about 9 GB free per song), Python 3.12, ffmpeg and Node.js. Other GPUs aren't tested; see [CONTRIBUTING.md](CONTRIBUTING.md#hardware).
- Somewhere to play the songs: Encore, or any karaoke app that plays `.mp4` videos with `.ass` subtitles (publishing writes them to a library folder; see *Players*). The studio works on its own for previews.
- Optional: [Ollama](https://ollama.com) for the AI lyrics and romaji checks.

## Setup

```sh
git clone https://github.com/alainator/karaoke-studio && cd karaoke-studio
python3.12 -m venv venv
venv/bin/pip install torch torchaudio torchvision --index-url https://download.pytorch.org/whl/cu128   # match your driver
venv/bin/pip install -r requirements.txt
venv/bin/python -I get_renderer.py        # lyric renderer and fonts
cp .env.example .env && chmod 600 .env    # then edit it
venv/bin/python -I app.py                 # http://127.0.0.1:5570
```

The first song downloads the models (about 4 GB). `deploy/karaoke-studio.service.example` is a systemd unit.

### With Docker

Needs Docker with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/) instead of Python, ffmpeg and Node.js:
```sh
cp docker-compose.example.yml docker-compose.yml && cp .env.example .env   # then edit both
docker compose up -d --build                                                 # http://127.0.0.1:5570
```
The image installs PyTorch for CUDA 12.8 (NVIDIA driver 570 or newer); `TORCH_INDEX` in the Dockerfile picks another build. Songs, downloads and models go to `./data`.

For songs made with older versions: `venv/bin/python -I deploy/check-library.py` checks each one by ear and marks the lines to fix (about 20 s a song, changes only the marks), and `deploy/reencode-videos.py` fixes their videos for smooth seeking.

## A second GPU (optional)

On the other PC (Arch/CachyOS):
```sh
curl -fsSLO https://raw.githubusercontent.com/alainator/karaoke-studio/main/deploy/install-helper.sh
bash install-helper.sh <studio server IP>
```
It installs about 10 GB and prints a secret. Then, on the studio server:
```sh
venv/bin/python -I deploy/set-helper.py <PC IP> "gaming PC"   # asks for the secret
sudo systemctl restart karaoke-studio
```
The helper only accepts the studio server, and only with the secret. It updates itself from GitHub whenever the studio runs newer code.

## Players (Encore)

The studio's songs are plain files (`.mp4`, `.ass`) plus a documented catalog (`catalog.json` in the library root, a
`.words.json`, a `.pitch.json` and a `.vocals.m4a` per song), so any player can use them. Encore also talks to the studio through the
player API (`/api/encore/`), described with JSON Schemas and sample exchanges in [`contract/`](contract/ENCORE-STUDIO.md)
(this repo has the master copy; `contract/make_fixtures.py` regenerates the samples with the studio's own code).

**Linking a player.** On the **Players** tab, *Connect a player* shows the studio's address and a one-time code (8
characters, 10 minutes, one use). Type both into Encore (station → System → Karaoke Studio). Each player gets its own
key, kept only as a hash here; *Unlink* stops it at once. For a Docker setup without clicking, `ENCORE_TOKEN` in `.env`
(and the same value as `STUDIO_TOKEN` in Encore's) is one more player, "preset".

**What players send.** Song requests show on the **Requests** tab (with a count in the menu): *Find and make* opens the
Make tab with the search filled in, and the request then follows that song (being made, ready, published); *We have
it* and *Decline* answer it. Lyric flags ("the words light up late here") put a ⚠ on that line with a note like
"Encore: late (Sam, Friday at Ken's)" and show as *Flagged in Encore* on the song's page and in *Needs fixing*; they
never change timing or words. Plays count in the Library's *Sung* column and in `catalog.json`.

**Party auto-make.** Off by default. On the **Players** tab: *Party auto-make* (with limits: songs per hour and per
day, the longest song, only if a graphics card is free within N minutes, the quality floor) and, per linked player,
*May auto-make*. When both are on and the party's host turned *Auto-make missing songs* on in Encore, a request for a
song the studio doesn't have is searched, picked (`autopick.py`: the Make page's own rules, kept identical by a test),
made and, if at least 60% of its lines were heard in place (the 10th percentile of songs people published; the floor is
a setting) and no lines are crammed, published without review, signed "auto-make for <player>". Songs list:
*quick make, not reviewed* (and a filter) until a person marks it checked. Below the floor it waits as made ("needs a
look"); it never replaces a published song. Encore sees the stage and an ETA (from the queue and the measured make
times) and keeps the guest's place meanwhile (contract §2.2b). Agents can't turn it on or start it.

**Melody files.** Publishing writes `<name>.pitch.json` in the background (CREPE on the song's separated vocals: a few
seconds on the GPU, about 20 s on the CPU). For songs published before, run once (when nobody is singing):
`python -I deploy/write-pitch.py --dry`, then without `--dry` (`--limit 5` to try a few first). The file also carries
the instrumental's loudness every 0.1 s (`music`); to add it to pitch files written before: `python -I
deploy/write-music.py --dry`, then without `--dry` (ffmpeg only, about a second a song).

**Words scoring.** A linked player can send a singer's recording of a song (Opus, at most 8 MB); the studio aligns the
song's words against it and answers which ones it heard (contract §2.9). It runs after everything else: on the GPU only
when no song is being made or waiting (about a second), else on the CPU (about 30 s), else it answers "busy"
(`KARAOKE_WORDS_CPU=0`: never on the CPU; `KARAOKE_WORDS_SCORE=0`: off). The recording stays in memory, is never written
to disk or logged, and is dropped when it's scored; answers are forgotten after 10 minutes.

**Guide vocals.** Publishing also copies the song's separated lead vocal next to it as `<name>.vocals.m4a` (AAC,
~128 kb/s, 3-5 MB a song, on the video's timeline with any trim applied; versions get the same file), so a player can
play it quietly under the karaoke for singers who want a little help. A trim or remake changes it in the studio; the
published copy follows on the next publish. For songs published before: `python -I deploy/write-vocals.py --dry`,
then without `--dry` (a plain copy, no GPU; songs changed since they were published are skipped: publish them again).

**Networking (Docker).** Encore reaches the studio over a private network that only the two containers join:
```yaml
# in both compose files
services:
  karaoke-studio:           # (in Encore's file: the encore service)
    networks: [karaoke_link]
networks:
  karaoke_link:
    external: true          # made once: docker network create --internal karaoke_link
```
Encore's `STUDIO_URL` (or the address typed when linking) is then `http://karaoke-studio:5570`, which is also what
*Connect a player* shows (`ENCORE_STUDIO_ADDRESS`). `--internal` keeps the network off the internet; the studio keeps
its other networks. The studio has no login of its own, and a container on that network can reach all of it, not
only `/api/encore/`. To shut the rest off there, set `ENCORE_ONLY_NETS` to that network's subnet (`docker network
inspect karaoke_link`; give it a fixed `--subnet` when creating it): requests arriving on the studio's address in that
subnet may then only use the player API. Check the web app still answers through your proxy afterwards (if Docker
routes the published port over that network, the proxy gets 403 too; then leave it empty).
A player elsewhere (a party kit on Tailscale) should get a route that forwards only `/api/encore/` to the studio.
Settings: `STUDIO_PUBLIC_URL` (the web app's address, shown in Encore's host view), `STUDIO_NAME`, `KARAOKE_DATA`
(where `encore.db` is kept: default the folder above the logs).

**No network: a USB stick** (contract §2.7, `bundle.py`). A player that can't reach the studio syncs by stick.
*Export…* on its row makes a `.karaoke-sync.tar` with the songs it doesn't have yet (by what its last bundle said it
has; or everything) and its requests' answers; *one file* for an exFAT/NTFS stick, or *parts of 3.9 GB* for FAT32
(each part a complete file with whole songs). The first export fingerprints every library file once (sha256, kept in
`bundle-hashes.db`); later ones only read new files. The download has its exact length, so a cut-off copy fails in
the browser. The player imports it, and writes its own file back (its requests, lyric flags and plays, and what it
has): *Import a player's bundle…* applies them with the player API's own code, as that player, except that requests
never start a party auto-make. Each file is signed with that player's key (from its pairing token: nobody else can
make or read-and-change one), so a file from an old link is refused ("export it again"). These are admin pages: the
players-only network (`ENCORE_ONLY_NETS`) never reaches them. Profiles sync over the network only.

## Finishing songs: by hand or with an agent

Every song is checked by ear after it's made, and the lines that may be off are marked ⚠ with the reason. Finishing a
song means going through those: by hand in the web editor, or by letting an agent do the first pass.

**By hand.** Open the song: the ⚠ list jumps to each line; drag, stretch or tap **S** where it's sung, click the ⚠ if
it's fine, Save. **Undo** takes back the last edit; *Undo all edits* goes back to the automatic version.

**With an agent.** `tools/ks` does what the editor does, from a terminal, and lets an agent "see" a song: the Studio's
by-ear check of the lines as they are now, the words it heard around a line, a picture of the vocals with the line
blocks, and the video frame with the lyrics. On the **Agents** tab, make a key for each agent and tick what it may do:
reading, editing timing and words, notes and making new songs are on by default; clearing ⚠ marks, publishing,
remakes (and trims, custom videos, resets) and deleting are off. The agent leaves notes on lines ("Claude: looks fine: held note") for you
to confirm in the editor, and every edit and note is signed with its name (people's with their login name). Undo
works the same whoever made the edit, and the song page says who edited it last. [AGENTS.md](AGENTS.md) is the guide
for agents (setup, rules, notes, the quality bar); `.claude/skills/` has the playbooks, which Claude Code loads as
skills.

`ks` needs only Python 3: `ks setup --url http://<studio>:5570` stores the address and the key (asked for, never
shown), or set `STUDIO_URL` and `STUDIO_AGENT_KEY`.

## Logs

- `data/logs/studio.log`: what the studio did and when (queued, started on which card, finished, failed and why, published).
- Each song's **log ↗** link: everything the pipeline printed while making it, every run.

## Security

**The studio has no login of its own**, and it can change your karaoke library. Keep it on `127.0.0.1`, or put it behind a reverse proxy that requires a login (Authentik, Authelia, oauth2-proxy…), and set `STUDIO_ORIGINS` to the address you use. Agent keys (see Finishing songs) are checked on every call; set `AGENT_KEYS_REQUIRED=1` so that API calls without your proxy's login header need one (for when other programs can reach the studio's port; the login header named by `STUDIO_PERSON_HEADER` is trusted as sent, so only the proxy may reach the port). The player API (`/api/encore/`) has its own keys (see Players): wrong keys get 401 and are rate-limited, pairing tries are limited to 5 a minute per address and a code dies after 5 wrong tries, and keys and codes are never logged.

## Notes

- You're responsible for what you download and how you use it. This is meant for karaoke at home.
- LRCLIB is a free community service; please keep `STUDIO_USER_AGENT` meaningful.
- The lyric renderer is [JavascriptSubtitlesOctopus](https://github.com/libass/JavascriptSubtitlesOctopus).
- Lyrics come from [LRCLIB](https://lrclib.net) and [kara.moe](https://kara.moe) (Karaoke Mugen). Romanization uses [uroman](https://github.com/isi-nlp/uroman), [cutlet](https://github.com/polm/cutlet), [PyThaiNLP](https://github.com/PyThaiNLP/pythainlp) and [ToJyutping](https://github.com/CanCLID/ToJyutping). Scripts the TV can't draw use [Noto](https://notofonts.github.io) fonts (SIL Open Font License), embedded in the subtitle file. The web app uses [Atkinson Hyperlegible Next](https://www.brailleinstitute.org/freefont/) and [Bricolage Grotesque](https://github.com/ateliertriay/bricolage) (SIL Open Font License; licence files in `static/fonts/`), the same as Encore.

## License

MIT, see [LICENSE](LICENSE).
