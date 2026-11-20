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
- [PiKaraoke](https://github.com/vicwomg/pikaraoke) 1.23 or newer to publish to. The studio works on its own for previews.
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

## Logs

- `data/logs/studio.log`: what the studio did and when (queued, started on which card, finished, failed and why, published).
- Each song's **log ↗** link: everything the pipeline printed while making it, every run.

## Security

**The studio has no login of its own**, and it can change your karaoke library. Keep it on `127.0.0.1`, or put it behind a reverse proxy that requires a login (Authentik, Authelia, oauth2-proxy…), and set `STUDIO_ORIGINS` to the address you use.

## Notes

- You're responsible for what you download and how you use it. This is meant for karaoke at home.
- LRCLIB is a free community service; please keep `STUDIO_USER_AGENT` meaningful.
- Not affiliated with PiKaraoke. The lyric renderer is [JavascriptSubtitlesOctopus](https://github.com/libass/JavascriptSubtitlesOctopus).
- Lyrics come from [LRCLIB](https://lrclib.net) and [kara.moe](https://kara.moe) (Karaoke Mugen). Romanization uses [uroman](https://github.com/isi-nlp/uroman), [cutlet](https://github.com/polm/cutlet), [PyThaiNLP](https://github.com/PyThaiNLP/pythainlp) and [ToJyutping](https://github.com/CanCLID/ToJyutping). Scripts the TV can't draw use [Noto](https://notofonts.github.io) fonts (SIL Open Font License), embedded in the subtitle file.

## License

MIT, see [LICENSE](LICENSE).
