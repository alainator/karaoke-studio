---
name: karaoke-make
description: Make new karaoke songs in Karaoke Studio - search for the music video and the lyrics, pick the right ones (right song, version and language, full length, lyrics that match), and queue them with ks. Use when asked to make, add or create karaoke songs or videos, or given a list of songs to make.
---

# Making songs

1. `ks search "Artist - Title"`: up to 10 videos (id, length, OC = likely the artist's or label's own channel,
   score, title, channel, flags) and lyrics (source:id, length, timed or plain, name match, script, artist - title).
   If the lyrics part is empty with a note that LRCLIB didn't answer (429), wait a minute and search again.
   Searches for non-English songs often work better with the original-script title, or the other way round.
   Given a YouTube link, `ks search LINK` lists that video first (marked "your link") with the song's names read from
   its title; check the names (they come from an uploader's title, then LRCLIB) and the video like any other.
2. Pick the video. Check the title and channel, not just the score:
   - The official music video of the original: not a lyric video (lyrics burned in would show twice), not a
     cover, remix, re-recording ("2020 version"), "short ver.", fan cut, or a version in another language
     (English versions of German, Greek or Spanish hits come up often).
   - A famous live or TV performance is fine when that's the version people know, or there's no official video.
   - A result without a channel name proves nothing either way; look closer.
3. Pick the lyrics: from the same song, timed if possible, with a length within about 12 s of the video (a
   music video can be longer because of an intro or scenes: the studio finds the song inside it). Prefer the
   original script (Japanese, Korean, Chinese, Cyrillic, Thai…): the studio romanizes it and shows the original
   above. kara.moe lyrics (hand-made) are best for anime and J-pop when listed.
4. Queue it: `ks make --yt VIDEO_ID --lyrics LYRICS_ID [--source karamoe] --artist "Artist" --title "Title"`
   (your key needs the "make" scope). For a song in another script, name it "Romanized title (original title)",
   e.g. Gruppa Krovi (Группа крови), so it can be found both ways in players.
5. Cantonese songs: when made, `ks reading SONG yue` (jyutping instead of pinyin; the timing stays).
6. When it's done (`ks list --q`), check it like any song (karaoke-fix skill). Don't publish unless asked.
   When the person asked for a batch to go out unreviewed, queue with `--auto` (needs the "publish" scope): each
   song is published by itself if its video check passes, otherwise it waits with the reason (`ks show SONG`).

Given a list or a YouTube playlist: `ks import PLAYLIST-LINK` (or `ks import FILE`, one song per line: Artist - Title
or a video link; `-` reads stdin; up to 2000) makes the Studio pick the video and timed lyrics for each the way party
auto-make picks (a playlist keeps its own videos), and prints a review: `[x]` ready rows, skipped rows with the reason
(no timed lyrics, too long, already in the library or the queue, a duplicate, an unavailable video, a lyric/live/cover
video found for a typed line). Nothing is made yet. Its picks are automatic: check every `[x]` row against the rules
above (right song and version, the official video, lyrics of the same length and language) before
`ks import IMPORT-ID --make` queues them (all at once, in list order: do that when the person asked for the whole
list made; otherwise follow the pacing below); for a row that's wrong, make that song by hand (steps 1-4) instead.
The person can do the same review on the Make tab (Import a list), with Change… per row.

The studio makes one song at a time per GPU. An import's songs are background work: songs made by hand (`ks make`,
the Make button, a remake) and party requests go before them, so queueing a whole import doesn't hold the person up.
For many songs with `ks make`, queue one, wait until `ks status` says nothing is being made, then the next; and hold
off while a party is singing (the GPU and disk are busy with playback). `ks status` shows the queue and why the next
song waits (the GPU held by another program, a song paused for the GPU).
