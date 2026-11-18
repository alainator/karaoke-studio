# Karaoke Studio

![Karaoke Studio: search, preview with word-by-word lyrics, and the waveform lyrics editor](docs/demo.gif)

<sub>Demo: the music video and thumbnails are blurred on purpose (copyright); in the app they're shown normally.</sub>

Turn an official music video into a karaoke video, with the real video, the vocals removed, and word-by-word lyrics, then publish it to [PiKaraoke](https://github.com/vicwomg/pikaraoke).

You type `Artist - Title`. The studio finds the official video on YouTube and timed lyrics on [LRCLIB](https://lrclib.net). It removes the vocals on your GPU and lines up every word with the singing. You can preview the result in the browser before it goes anywhere near PiKaraoke.

- **Search:** shows music-video candidates (artist channel and "live", "lyrics" or "cover" uploads are flagged) and LRCLIB lyrics, with whether they're timed and how their length compares to the video. The best of each is picked for you.
- **Make:** one job at a time. Jobs wait if the GPU doesn't have enough free memory, for example while a big LLM is loaded. A typical song takes about a minute on an RTX 5090.
- **Preview:** plays the video with the instrumental and the moving lyrics, using the same renderer as PiKaraoke's TV page. A *Guide vocals* slider plays the separated vocals quietly along, and a *Lyrics timing* slider shifts the lyrics earlier or later without redoing anything.
- **Lyrics editor:** a waveform of the separated vocals with every lyric line drawn as a block. Drag a block to move a line, drag its edge to stretch it, click to jump, Space to play or pause. Below it is a line list where you can fix words, nudge a line by 0.1 s, start it at the playback position, add a missing line or delete one. **Tap to sync:** click a line, play, and press `S` the moment it's sung; it moves there and the next line is selected, so you can keep tapping through a section. `←`/`→` nudge the selected line by 0.1 s (`Shift`: 1 s). With *Move the lines after it too* (off by default; turn it on when a whole section is off by the same amount), every move — S, arrows, −/+, ⏱, a typed time or dragging a block — shifts all later lines by the same amount, so one fix repairs a whole section that's off. For duets, *Detect 2 singers* fingerprints each line's voice in the vocal track (SpeechBrain ECAPA), splits the lines into two voices and colours them (Singer 1 gold, Singer 2 blue, Both green). Singer 1 is the voice that sings the chorus. Lines it isn't sure about get a ?. Review, *Swap 1↔2* if you like, right-click a waveform block to cycle its singer (1 → 2 → Both), then Save. For groups (K-pop and others), *Name singers* sets up to 8 members, each with their own colour; lines sung together stay gold, and a legend at the start of the song shows who's who on the TV. *Paste lyrics* also reads section headers like `[Chorus: Bang Chan, Han]` (Genius style) and colours every line by its section's singers, so fans can colour a song by pasting its lyrics page. On In The End it matched a hand-coloured version on 48 of 55 lines; lines sung by both voices can't be detected, so set those yourself. Lines shared by two singers (*A Whole New World*: "A whole new world" is Jasmine, "(Don't you dare close your eyes)" Aladdin) get **◐**: it shows the singers as brushes and the line's words; pick a singer, then click or drag over words to paint them (a word opening brackets paints the whole bracketed reply); on the TV the colour changes at those words. *Undo all edits* restores the automatic timing.
- **Lines to check:** each line's word fit is kept, and lines that fit far worse than the rest of the song (under half the median) are marked ⚠ in the editor, with a dashed outline on the waveform and *Next line to check*, which selects the next one and plays from just before it; moving a line clears its mark. On *Let Me Hear* (Fear, and Loathing in Las Vegas: fast, screamed, accented English) the marks fell exactly on the stretch that drifts.
- **Mostly-English songs with a Japanese, Korean or Chinese chorus** are listened to in English (Whisper told "Japanese" hears nothing in English verses); the native lines keep their romanization and original line.
- **Remaking older songs:** every song records the version that made it (`versions.py` lists the improvements and what kind of song each matters for). A song made before some of them shows them on its page, with *Remake with the latest version*, which makes it again in place (its edited lyrics are kept as `words.before-remake.json`; publishing afterwards replaces PiKaraoke's copy). The song list marks songs with improvements waiting.
- **Lyrics without timing:** when LRCLIB has no timed lyrics for a song, *Paste lyrics* on the Make page takes them from any lyrics site (section headers like `[Chorus]` are skipped; pasted LRC with `[01:23.45]` timestamps is used as timed). LRCLIB entries that only have plain lyrics can be picked too. Each line is placed by listening: Whisper aligns the lines to the vocals, and the transcription matching and word timing run as usual. Made from plain lyrics, Bắc Bling matched its timed version on 61 of 62 lines (within 0.5 s) and Creep on 34 of 37; The Kill, with its skit and scenes, on 31 of 41.
- **Transcription first:** Whisper transcribes the vocals freely, and the lyrics are lined up with that transcription word by word, in order. Heard words that aren't in the lyrics (a skit's dialogue, mishearings) are skipped, and every line that is clearly found gets an anchor at its earliest heard word (words can be held for seconds). The words are timed inside those windows, and the result is used only when it fits the vocals better than the timing from the lyrics' timestamps: on Black Parade 0.289 vs 0.255, on Creep 0.315 vs 0.304, while In The End, Numb and Bắc Bling keep the timestamp-based timing.
- **Music videos with scenes:** some videos start with a skit, or stop the song for a scene partway (*30 Seconds to Mars – The Kill*: a 68 s skit, then two more scenes that push the rest of the song 9 s and 26 s later). Candidate offsets come from three places: a piecewise path over vocal activity (each jump ≥ 2 s and penalised), the offset that best suits each run of 6 lines, and, most directly, the lyric lines found in a free transcription of the vocals (a shift several lines agree on). Then the word aligner scores every line at each candidate, and a second path over those scores picks where the timing jumps (lines always stay in order). It is used only if the words then fit the vocals better than with one offset: on The Kill word fit went from 0.144 to 0.238 and the result matches a transcription of the video; a second upload of the same video with sloppier lyric timestamps is fixed the same way (the transcription runs without a voice-activity filter, which had stopped it at the first long scene). Creep, In The End, Numb and Black Parade keep one offset. Outside the album recording (before the song, after it, and in the scenes between jumps) the video's own sound is kept, so skits keep their dialogue, with a 1 s crossfade into the karaoke track.
- **Video that seeks cleanly:** the video is re-encoded (x264, a keyframe every 2 s), never copied as downloaded: YouTube's H.264 plays in ffmpeg, but Chrome-based browsers (Chrome, Brave, Edge) hit a decode error after jumping backwards in it and the picture freezes while the sound goes on. Costs ~15 s a song. Songs made with an older version can be fixed with `python -I deploy/reencode-videos.py` (their PiKaraoke copy is updated the next time you publish them).
- **Squeezed lines:** each line is aligned inside the slot its LRCLIB timestamps give it, so one wrong timestamp used to cram a line (in *Welcome to the Black Parade*, "We'll carry on, we'll carry on" got 2.6 s because the next line's timestamp was 2.5 s early, and every chorus after it was off). After aligning, every pair of touching lines is aligned again together with the boundary free to move, and the joint timing is kept only when it fits the vocals clearly better. On Black Parade it fixed the choruses; on In The End and Numb it only moved line starts that were overlapping the previous line's last words, and Creep is unchanged.
- **AI lyrics check (any Latin-alphabet song):** lyrics from LRCLIB are typed by fans, so words get dropped or swapped. With Ollama configured, Whisper writes down what is actually sung on each line (cutting off the neighbouring lines' words), the local model proposes the line as sung wherever that differs, and the forced aligner scores the written and the proposed line against the vocals; a change is applied only if it clearly fits the singing better, so Whisper mishearings ("the clock ticks *like a wave*") can't change a line. Tested by planting fan-style mistakes into *In The End*: it restored a dropped "even", a dropped "before" and "realize" → "recognize", and it changed nothing on the correct lyrics of In The End, Numb and Creep. One-sound differences ("means" vs "meant") are too close to call by ear and are left as written. The song page lists every change, *Undo all edits* restores the original, and *Check lyrics with AI* in the editor runs it again.
- **Japanese:** hand-timed romaji from [kara.moe](https://kara.moe) (the Karaoke Mugen database) when a full-length version exists. Otherwise Japanese lyrics from LRCLIB get automatic romaji (cutlet + MeCab), with the Japanese line shown above. Automatic readings are about 96–98% right. With a local [Ollama](https://ollama.com) server configured, an **AI romaji check** runs automatically after each song with automatic romaji. A local model reads the Japanese plus Whisper's transcription of what is actually sung, then fixes wrong readings. It also catches special readings that the written lyrics can't show (幻 written but 夢 "yume" sung): the model proposes possible sung readings for kanji words (未来 ashita/asu, 運命 sadame, 空 kara…) and the forced aligner, which models sounds rather than kanji, scores each against the singer's voice; a reading wins only if it clearly fits the vocals better. On Brave Shine the fully automatic result matched a human romanization on 99.7% of letters. The song page lists every change, and *Undo all edits* restores the plain automatic romaji. On Brave Shine it fixed 6 lines (yume, yaiba, nanimo ×2, kizutsukeau, kotoba) and changed nothing that was right. *Check romaji with AI* in the editor runs it again on demand. For readings no tool can guess, *Paste romaji* takes a human romanization (e.g. a lyrics site's "Romanized" page) pasted whole: it is lined up with the song by sound (line breaks, merged lines and section headers don't matter), and every difference is shown as old → new to tick. On Brave Shine it fixed the remaining kara, ashita ×2, sadame and yume. Lyrics uploaded as Japanese + romaji + translation triples are merged, and the hand-made romaji is used. English translations in brackets inside romaji lyrics are dropped. On square or portrait videos the lyrics are scaled down so lines don't wrap.
- **Publish:** copies `Artist - Title (Music Video).mp4` and `.ass` into your PiKaraoke library and asks PiKaraoke to rescan. It can also remove the older YouTube lyric-video copy of the same song, but only exact matches, and you can untick them.
- **Library:** your PiKaraoke songs, most-sung first (read from PiKaraoke's play history), with a "Make MV version" button for songs that only have a lyric video or an anime TV version.

## How the timing works

1. [BS-RoFormer](https://github.com/nomadkaraoke/python-audio-separator) separates the vocals from the instrumental.
2. LRCLIB's synced lyrics give a human-made start time for each line. Those times are usually for the album version, so the video's offset is needed. There are three candidates: the median and the most common difference from a [stable-ts](https://github.com/jianfch/stable-ts) (Whisper large-v3) alignment, and the best fit of line starts against the vocal track. The word aligner tries each, and the offset whose words fit the singing best wins. Repeated choruses can fool Whisper, and dense rap fools the vocal fit; the word fit catches both.
3. Inside each line, torchaudio's MMS_FA forced aligner places every word on the vocal stem. Held last notes run as long as the singer does, and lines the video doesn't sing (album-only intros and the like) are dropped.
4. The output is an `.ass` subtitle with `\kf` word fills on two alternating rows, so the next line is visible before it's sung.

Works for Latin-alphabet languages, Japanese, Korean and Mandarin Chinese. Korean is romanized as it is pronounced, with sound-change rules applied first (묻는 → munneun, 몰라 → molla): on Stray Kids "TOP" that matched a human romanization on 99.7% of letters. Mandarin becomes pinyin, one syllable per character. Latin-alphabet languages with accents (Vietnamese, Spanish, French…) show exactly as written. Cantonese is detected by ear and refused for now. For Latin-alphabet songs that aren't English, Whisper is told the sung language instead of assuming English. Other scripts (Cyrillic, Thai…) are refused rather than turned into wrong romanizations.

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

If another Linux PC on your network has an NVIDIA card, it can make songs while the main GPU is busy, for example with a big LLM loaded. The studio sends a queued job to the helper only when its own GPU lacks room and the helper is online with room. Both can run at once. If the PC shuts down or sleeps mid-song, the job goes back in the queue. With a helper set up, the Make page has a **GPU** menu: *Auto* (the default: whichever card has room) or one particular card, each shown with its current state (free memory, busy, off). A song waiting for the card you picked doesn't hold up later songs that can use the other one. Each song page shows which GPU made it.

On the PC (Arch/CachyOS; it asks for your password for pacman and the firewall):
```sh
curl -fsSLO https://raw.githubusercontent.com/alainator/karaoke-studio/main/deploy/install-helper.sh
bash install-helper.sh <studio server IP>
```
This installs about 10 GB (PyTorch, models). The helper keeps itself up to date: when the studio sees it running different code, it asks the helper to pull the latest version from GitHub, install new packages and restart; until then it gets no jobs. It prints a shared secret once and runs the helper as a user service on port 5580, which only the studio server may reach, and only with the secret. Then, on the studio server:
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
