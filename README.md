# Karaoke Studio

![Karaoke Studio: search, preview with word-by-word lyrics, and the waveform lyrics editor](docs/demo.gif)

<sub>The video and thumbnails are blurred in this demo for copyright; in the app they show normally.</sub>

Turn an official music video into a karaoke video, with the vocals removed and the lyrics lighting up word by word, then publish it to [PiKaraoke](https://github.com/vicwomg/pikaraoke).

Type `Artist - Title`. The studio finds the music video on YouTube and the lyrics on [LRCLIB](https://lrclib.net), removes the vocals on your GPU, and times every word to the singing. You preview it in the browser, fix anything you want, and publish.

## What it does

- **Finds the right video and lyrics.** Official music videos are preferred over lyric videos, live versions, covers and dance practices. Each suggestion can be opened first: watch the video, read the lyrics. No lyrics online? Paste them from any site; if LRCLIB has a timed version, your words get its timing.
- **Times the lyrics by listening.** It finds the album recording and matches it against the video's music, so skits, scenes in the middle and cuts are found exactly. Talking keeps its sound; only the singing is removed. Backing vocals in brackets get their own line, so they can overlap the lead.
- **Fixes lyric mistakes.** An optional local AI (via [Ollama](https://ollama.com)) catches dropped or swapped words and wrong Japanese readings, and keeps a change only if it fits the singing better.
- **Lets you edit easily.** A waveform or spectrogram of the vocals with a block per line: drag, stretch, or tap **S** as each line is sung, and starts snap to where notes begin. Slow playback to 0.5×, loop a line, split or join lines.
- **Tells you what to fix.** Every song is checked by ear after it's made: lines heard somewhere other than where they sit are marked ⚠ with the reason, the song page lists them, and the song list shows how many each song has left.
- **Colours singers.** Duets, groups of up to 8 named singers, and lines shared mid-way (paint the words of each singer). Pasting a Genius page with headers like `[Chorus: Name]` colours the lines for you.
- **Remakes safely.** Older songs show what a remake would improve. A remake keeps your edits, changes nothing if it finds nothing better, and can be undone.
- **Uses a second GPU.** A PC on your network can take songs when the main GPU is busy, or you can choose which GPU makes each song.
- **Works with Encore.** Link the Encore party app (or any player that speaks [the contract](contract/ENCORE-STUDIO.md)): guests' song requests land in a Requests list, lyric flags from the party mark the lines to fix, and what was sung counts in the Library. Each published song also gets its sung melody (`.pitch.json`) for singing scores and its separated vocals (`.vocals.m4a`) for guide vocals.

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

## How it works

1. [BS-RoFormer](https://github.com/nomadkaraoke/python-audio-separator) splits the vocals from the music.
2. Whisper ([stable-ts](https://github.com/jianfch/stable-ts), large-v3) transcribes the vocals, and the lyrics are matched against what was actually sung. That gives each line its place in the video, including where the video jumps.
3. The album recording is found on YouTube and matched against the video's music (librosa), giving an exact map of skits, scenes and cuts.
4. A forced aligner (torchaudio MMS) places every word. Lines that fit badly are marked for you to check.
5. The result is an `.ass` subtitle on two alternating rows, so the next line shows before it's sung.

## Requirements

- Linux with an NVIDIA GPU (about 9 GB free per song), Python 3.12, ffmpeg and Node.js.
- A player to publish to: [PiKaraoke](https://github.com/vicwomg/pikaraoke) 1.23 or newer, and/or Encore (or anything that reads `catalog.json`). Without PiKaraoke, leave `PIKARAOKE_URL` and `PIKARAOKE_DB` empty: publishing then skips its rescan. The studio works on its own for previews.
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
never change timing or words. Plays count in the Library's *Sung* column and in `catalog.json`, together with
PiKaraoke's own history.

**Melody files.** Publishing writes `<name>.pitch.json` in the background (CREPE on the song's separated vocals: a few
seconds on the GPU, about 20 s on the CPU). For songs published before, run once (when nobody is singing):
`python -I deploy/write-pitch.py --dry`, then without `--dry` (`--limit 5` to try a few first).

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

## Logs

- `data/logs/studio.log`: what the studio did and when (queued, started on which card, finished, failed and why, published).
- Each song's **log ↗** link: everything the pipeline printed while making it, every run.

## Security

**The studio has no login of its own**, and it can change your karaoke library. Keep it on `127.0.0.1`, or put it behind a reverse proxy that requires a login (Authentik, Authelia, oauth2-proxy…), and set `STUDIO_ORIGINS` to the address you use. The player API (`/api/encore/`) has its own keys (see Players): wrong keys get 401 and are rate-limited, pairing tries are limited to 5 a minute per address and a code dies after 5 wrong tries, and keys and codes are never logged.

## Notes

- You're responsible for what you download and how you use it. This is meant for karaoke at home.
- LRCLIB is a free community service; please keep `STUDIO_USER_AGENT` meaningful.
- Not affiliated with PiKaraoke. The lyric renderer is [JavascriptSubtitlesOctopus](https://github.com/libass/JavascriptSubtitlesOctopus).
- Lyrics come from [LRCLIB](https://lrclib.net) and [kara.moe](https://kara.moe) (Karaoke Mugen). Romanization uses [uroman](https://github.com/isi-nlp/uroman), [cutlet](https://github.com/polm/cutlet), [PyThaiNLP](https://github.com/PyThaiNLP/pythainlp) and [ToJyutping](https://github.com/CanCLID/ToJyutping). Scripts the TV can't draw use [Noto](https://notofonts.github.io) fonts (SIL Open Font License), embedded in the subtitle file.

## License

MIT, see [LICENSE](LICENSE).
