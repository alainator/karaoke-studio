# Karaoke Studio

![Karaoke Studio: search, preview with word-by-word lyrics, and the waveform lyrics editor](docs/demo.gif)

<sub>Demo: the music video and thumbnails are blurred on purpose (copyright); in the app they're shown normally.</sub>

Turn an official music video into a karaoke video, with the real video, the vocals removed, and word-by-word lyrics, then publish it to [PiKaraoke](https://github.com/vicwomg/pikaraoke).

You type `Artist - Title`. The studio finds the official video on YouTube and timed lyrics on [LRCLIB](https://lrclib.net). It removes the vocals on your GPU and lines up every word with the singing. You can preview the result in the browser before it goes anywhere near PiKaraoke.

- **Search:** shows music-video candidates (artist channel and "live", "lyrics" or "cover" uploads are flagged) and LRCLIB lyrics, with whether they're timed and how their length compares to the video. The best of each is picked for you.
- **Make:** one job at a time. Jobs wait if the GPU doesn't have enough free memory, for example while a big LLM is loaded. A typical song takes about a minute on an RTX 5090.
- **Preview:** plays the video with the instrumental and the moving lyrics, using the same renderer as PiKaraoke's TV page. A *Guide vocals* slider plays the separated vocals quietly along, and a *Lyrics timing* slider shifts the lyrics earlier or later without redoing anything.
- **Lyrics editor:** a waveform of the separated vocals with every lyric line drawn as a block. Drag a block to move a line, drag its edge to stretch it, click to jump, Space to play or pause. Below it is a line list where you can fix words, nudge a line by 0.1 s, start it at the playback position, add a missing line or delete one. For duets, *Detect 2 singers* fingerprints each line's voice in the vocal track (SpeechBrain ECAPA), splits the lines into two voices and colours them (Singer 1 gold, Singer 2 blue, Both green). Singer 1 is the voice that sings the chorus. Lines it isn't sure about get a ?. Review, *Swap 1↔2* if you like, right-click a waveform block to cycle its singer (1 → 2 → Both), then Save. On In The End it matched a hand-coloured version on 48 of 55 lines; lines sung by both voices can't be detected, so set those yourself. *Undo all edits* restores the automatic timing.
- **Japanese:** hand-timed romaji from [kara.moe](https://kara.moe) (the Karaoke Mugen database) when a full-length version exists. Otherwise Japanese lyrics from LRCLIB get automatic romaji (cutlet + MeCab), with the Japanese line shown above. Automatic readings are about 96–98% right. *Check romaji with AI* (optional, needs a local [Ollama](https://ollama.com) server) has a local model read the Japanese and suggest fixes, shown as old → new for you to tick and apply. On Brave Shine it fixed all four misreadings (刃 yaiba, 何も nanimo ×2, 傷つけあう) and changed nothing else. English translations in brackets inside romaji lyrics are dropped. On square or portrait videos the lyrics are scaled down so lines don't wrap.
- **Publish:** copies `Artist - Title (Music Video).mp4` and `.ass` into your PiKaraoke library and asks PiKaraoke to rescan. It can also remove the older YouTube lyric-video copy of the same song, but only exact matches, and you can untick them.
- **Library:** your PiKaraoke songs, most-sung first (read from PiKaraoke's play history), with a "Make MV version" button for songs that only have a lyric video or an anime TV version.

## How the timing works

1. [BS-RoFormer](https://github.com/nomadkaraoke/python-audio-separator) separates the vocals from the instrumental.
2. LRCLIB's synced lyrics give a human-made start time for each line. Those times are usually for the album version, so the video's offset is needed. There are three candidates: the median and the most common difference from a [stable-ts](https://github.com/jianfch/stable-ts) (Whisper large-v3) alignment, and the best fit of line starts against the vocal track. The word aligner tries each, and the offset whose words fit the singing best wins. Repeated choruses can fool Whisper, and dense rap fools the vocal fit; the word fit catches both.
3. Inside each line, torchaudio's MMS_FA forced aligner places every word on the vocal stem. Held last notes run as long as the singer does, and lines the video doesn't sing (album-only intros and the like) are dropped.
4. The output is an `.ass` subtitle with `\kf` word fills on two alternating rows, so the next line is visible before it's sung.

Works for Latin-alphabet and Japanese songs. Other scripts (Korean, Chinese, Cyrillic) are refused rather than turned into wrong romaji.

## Requirements

- Linux with an NVIDIA GPU (about 9 GB free VRAM per job), Python 3.12, ffmpeg, and Node.js (yt-dlp needs a JS runtime for YouTube).
- [PiKaraoke](https://github.com/vicwomg/pikaraoke) 1.23 or newer, if you want to publish to it. The studio works on its own for previews.

## Setup

```sh
git clone https://github.com/alainator/karaoke-studio && cd karaoke-studio
python3.12 -m venv venv
venv/bin/pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128   # pick the CUDA build for your driver
venv/bin/pip install -r requirements.txt
venv/bin/python -I get_renderer.py        # lyric renderer + fonts, taken from PiKaraoke's PyPI package
cp .env.example .env && chmod 600 .env    # then edit it
venv/bin/python -I app.py                 # http://127.0.0.1:5570
```

The first job downloads the separation model (about 610 MB) into `KARAOKE_MODELS`, and Whisper large-v3 (about 3 GB) into `~/.cache/whisper`.

`deploy/karaoke-studio.service.example` is a systemd unit.

## GPU helper (a second PC)

If another Linux PC on your network has an NVIDIA card, it can make songs while the main GPU is busy, for example with a big LLM loaded. The studio sends a queued job to the helper only when its own GPU lacks room and the helper is online with room. Both can run at once. If the PC shuts down or sleeps mid-song, the job goes back in the queue. Each song page shows which GPU made it.

On the PC (Arch/CachyOS; it asks for your password for pacman and the firewall):
```sh
curl -fsSLO https://raw.githubusercontent.com/alainator/karaoke-studio/main/deploy/install-helper.sh
bash install-helper.sh <studio server IP>
```
This installs about 10 GB (PyTorch, models). It prints a shared secret once and runs the helper as a user service on port 5580, which only the studio server may reach, and only with the secret. Then, on the studio server:
```sh
venv/bin/python -I deploy/set-helper.py <PC IP> "gaming PC"   # hidden prompt for the secret, checks the connection
sudo systemctl restart karaoke-studio
```

## Security

**The studio has no login of its own.** It can download, publish and delete files in your karaoke library. Keep it on `127.0.0.1`, or put it behind a reverse proxy that authenticates users (Authentik, Authelia, oauth2-proxy and the like), and set `STUDIO_ORIGINS` to the address you use. Requests that change something must come from those origins.

## Notes

- You are responsible for what you download from YouTube and how you use it. This is meant for private karaoke at home.
- LRCLIB is a free community service. Requests send the `STUDIO_USER_AGENT` header, so please keep it meaningful.
- Not affiliated with PiKaraoke. The lyric renderer is [JavascriptSubtitlesOctopus](https://github.com/libass/JavascriptSubtitlesOctopus) (MIT and others; see `static/octopus/COPYRIGHT` after `get_renderer.py`).

## License

MIT, see [LICENSE](LICENSE).
