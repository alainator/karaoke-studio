---
name: karaoke-make
description: Make new karaoke songs in Karaoke Studio - search for the music video and the lyrics, pick the right ones (right song, version and language, full length, lyrics that match), and queue them with ks. Use when asked to make, add or create karaoke songs or videos, or given a list of songs to make.
---

# Making songs

1. `ks search "Artist - Title"`: up to 10 videos (id, length, OC = likely the artist's or label's own channel,
   score, title, channel, flags) and lyrics (source:id, length, timed or plain, name match, script, artist - title).
   If the lyrics part is empty with a note that LRCLIB didn't answer (429), wait a minute and search again.
   Searches for non-English songs often work better with the original-script title, or the other way round.
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

For many songs, don't queue them all at once while the person may be making their own: the studio makes one song
at a time per GPU, first come first served. Queue one, wait until `ks status` says nothing is being made, then the
next; and hold off while a party is singing (the GPU and disk are busy with playback).
