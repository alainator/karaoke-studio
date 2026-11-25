---
name: karaoke-fix
description: Fix a Karaoke Studio song's lyric timing and words the way a person does in the web editor - find the lines marked ⚠ (or the ones the person says are wrong), see where they are really sung (by ear and in a spectrogram picture), move, stretch, retype, split or join them with ks, check again, leave notes, and report what changed. Use when asked to fix, check, correct, retime or clean up a karaoke song, its lyrics or its timing.
---

# Fixing a song in Karaoke Studio

You edit through `ks` (`tools/ks` in the Studio's repository; see AGENTS.md for setup), which calls the same API as
the web editor. Every edit is saved at once, rebuilds the song's subtitles, is signed with your agent's name, and can
be undone with `ks undo SONG` (your last edit; `ks history SONG` lists them) or in the web editor. SONG is the job id
or a unique piece of "Artist - Title". `ks whoami` says what your key allows.

## What "good" means here
- A line must not light up late: more than about 1 s after its singing starts is wrong.
- Early is fine within reason: lines show about 2 s before their first word anyway, and up to about 2.5 s
  early is within the studio's tolerance. The next line may already show while the current one is sung.
- A line should stay up long enough to sing along (its last word ends where the singing of it ends).
- Perfect word-by-word timing isn't the goal: up a little early and up long enough so people can sing along.
- So the two directions aren't alike. **Be bold moving a line (or a boundary) EARLIER** by up to about 2 s when the
  heard words or the picture (stripes, melody row) show its singing starts earlier: one clear signal is enough, and if
  you're a bit off it's still harmless. **Be cautious moving a line LATER, or by more than about 2 s** either way: do
  it only when two signals agree (heard words, picture, `ks align`), and leave a note otherwise.
- The person does the final touches; your job is to get it close and to say what you weren't sure about.

## Workflow
1. `ks show SONG`: the lines with numbers, video times, ⚠ marks and why, and notes. Read the whys; they say late,
   early, or "not heard here".
2. `ks check SONG`: the by-ear check on the lines as they are now. It lists lines heard somewhere else, with
   the exact move to try (`→ ks move SONG N -3.07`). Many lines off by the same large amount usually means the
   video cuts or repeats a section differently from the lyrics: fix the first one with `--ripple`, then check
   again.
   `ks autofix SONG` applies only the clear ones (⚠ lines heard 1-15 s late or beyond the early tolerance, a run of
   3+ lines off by the same amount as one section) and checks again after each; `--dry` shows the first one it would
   make. Start with it, then work on what's left by hand. `ks check SONG --json` gives the same check as data.
3. For each line in doubt, look before you move:
   - `ks heard SONG --line N`: the words Whisper heard around it, with times.
   - `ks look SONG --line N` (or `--at 1:23 --span 14`), then open the PNG it prints. Top: the vocal spectrogram;
     singing shows as bright stripes, gaps show as dark columns. Cyan row: the melody (pitch of the sung frames, a
     white tick where a new note starts), so syllables show even where the singing never stops. Then the line blocks
     with their numbers (orange = ⚠, white = the line you asked for, small ticks = each word's start). Bottom
     (green): the words Whisper heard, at their times. A block should sit over the stripes of its own words, and its
     heard words should be under it. `--align` adds a magenta row and a dashed boundary: where the aligner puts
     line N when it hears it together with the line before (see below).
   - `ks align SONG N`: that alignment as numbers: each word's place and confidence, the line's start vs now, the
     boundary with the line before, and the pair edit to try. `ks align SONG --window 2:25 2:45` does every line in
     a stretch (a line far from where it's sung). It's a second opinion, not a gate: trust it when it agrees with the
     heard words; ignore it on a chorus opener right after a held note (it pulls the line onto that note), a duet
     reply, and lines whose words repeat nearby (it often lands on the wrong copy).
   - `ks frame SONG --line N` (or `--at T`), then open the PNG: what's on screen then, as players show it.
4. Fix with the smallest change that makes it right:
   - Whole line in the wrong place: `ks move SONG N +1.2`, or `ks move SONG N --to 1:23.40` (its start).
   - Wrong length (ends too soon, or runs into the next phrase): `ks stretch SONG N --end 1:27.10` (or `--start`).
   - A whole section shifted: `ks move SONG N DELTA --ripple` moves line N and every line after it.
   - Wrong words (misheard lyrics, a missing word): `ks text SONG N "the right words"`.
   - Two phrases in one line, or one phrase split in two: `ks split SONG N W` / `ks join SONG N`.
   - A word sung in pieces, one syllable held or stretched (the word's sweep runs ahead of or behind the singing,
     the melody row shows several notes under one word): `ks syllables SONG N W "tra|ge|dies"` splits word W (the
     pieces show joined, each lit at its own time; `ks show` prints them with |), then time each piece from the
     melody row's note starts with `ks word SONG N W.P --start T --end T` (3.2 = word 3's second piece; a shared
     boundary moves for both pieces). A held last syllable: `ks word SONG N W --end T` moves only its last piece's
     end. Never split words into separate words for this (that adds spaces and changes the word count players
     score). Most clear cases are split already when a song is made; `ks syllables SONG --auto --dry` lists what
     the melody would split in an older song, without `--dry` it splits them (one undoable edit). Not for Japanese,
     Chinese or Korean lines (already per character or per romanized word).
   - A line that isn't sung in this video: `ks delete-line SONG N`; one that's missing: `ks add SONG --start T "words"`.
   - A lyric line the studio left out that the video does sing: `ks skipped SONG` lists them; `ks unskip SONG K --start T`.
   - A ⚠ line that is actually fine (you looked and it sits right): leave it marked and say so on the line,
     `ks note SONG N "looks fine: <why, in a few words>"`. The marks are the person's review list, which they work
     through in the web editor, where your note shows next to the ⚠ reason, signed with your name. Clearing marks
     (`ks ok`) is theirs unless your key allows it and they asked you to.
   - Something the person should check on a line that isn't marked (words that may be wrong, a placement you could
     only guess): `ks note SONG N "check: ..." --mark` flags it for them with your note.
   - The whole song a bit early or late everywhere: `ks offset SONG 0.4` (-5..+5 s), not line by line.
5. The studio keeps lines in time order, so moving a line can renumber it and its neighbours. When ks says
   line numbers changed, run `ks show SONG` again before using any line number.
6. `ks check SONG` again. Repeat until nothing is left that's clearly wrong, or what's left can't be judged
   (say so instead of guessing).

## The usual cases
- **"not heard here, and its words fit the singing poorly"** is often a false alarm: drawn-out or melismatic
  singing, ad-libs, a refrain in a language Whisper mishears. Look at the picture: if the block sits over one sung
  phrase with about the right rhythm, between neighbours that are right, it's fine: `ks note` it ("looks fine: ...").
  If the singing runs on past the block (a held note), stretch the end to where the stripes stop instead (that
  clears the mark too).
- **A line that starts late, sung straight on from the line before** (the most common fix people make: 0.5-2 s
  late, often marked ⚠ but not always). There's no dark gap to see, and the crowd's timing shares the late start, so
  `ks alt-lyrics` won't help. It is a late **boundary**: the line before's held last note was stretched over this
  line's singing. Look where its first words are heard (`ks heard`) and where the melody row starts its first note
  after the held one (a white tick, a pitch jump). If either shows it starts earlier (by up to about 2 s), move the
  boundary there: the line before's end first, then this line's start (`ks stretch SONG N-1 --end T-0.05;
  ks stretch SONG N --start T`). `ks align SONG N` can confirm, but don't wait for it (see above for when to ignore
  it). If nothing shows where it starts, leave it and note it (`ks note SONG N "check: ..."`).
- **A repeated chorus**: when a chorus (or any repeated) line was late, find the same line in every repeat
  (`ks show SONG`) and check each one the same way; they are usually late alike, so fix them all alike, each at
  its own heard start.
- **A held last note** ending after the block: stretch the end to where the singing stops.
- **Lyrics with careless timing**: many lines squeezed together, or whole sections misplaced. Line by line is
  slow: `ks alt-lyrics SONG` scores every LRCLIB copy against what's heard; a copy that fits clearly better (for
  example 90% against 45%) means the song should be remade with it (`ks change SONG --lyrics ID`, only when the
  person agrees and your key allows remakes; otherwise report it).
- **A cut that sings lyric lines the studio left out**: a TV-size, MV or film cut skips a verse and jumps to another
  part, so a stretch of lines holds the wrong part of the lyrics, and the heard words there match lines in
  `ks skipped SONG`. Put the sung ones back in order at their heard times (`ks unskip SONG K --start T --end T`),
  delete the lines whose words aren't sung anywhere in the video (check the picture first), then `ks fill-orig SONG`
  for the original-script rows. These are the song's own lyric lines, so this isn't changing words. `ks alt-lyrics`
  can't help with a cut like this, because every copy of the lyrics has the full song.
- Heard words can belong to backing vocals or a second singer; check against the line's own words and its
  neighbours before moving anything onto them.
- Don't move a line onto the singing of a neighbouring line; if two lines compete for one phrase, the lyrics
  probably repeat a part this video doesn't (delete only when the picture shows no singing for it anywhere near).

## Rules
- Don't quote song lyrics in your replies: refer to line numbers and times ("line 14, 0:36").
- Don't change lines the person marked fine (✓ in ks show) or edited by hand, unless they ask.
- Don't clear ⚠ marks to make a song look done. A timing fix clears its line's mark, which is fine, but only retime a
  line that is actually off, never just to clear its mark.
- If the person may be editing the same song in the web app right now, don't: ks refuses to save if the song changed
  since it read it; if that happens, ask.
- Publishing, removing from the library, "checked by you", remakes, resets, trims, custom videos and deletes change what players get or redo
  work: only when the person asks, and only if your key allows it (the Studio refuses otherwise).
- A song Whisper couldn't follow ("couldn't follow by ear", or heard 0/N) can't be fixed from the heard words:
  use the spectrogram (each line's syllables against the stripes) and say how sure you are.

## Reporting
Say what you changed (how many lines, which kinds of fix, roughly where), what's still uncertain and why, and that
`ks undo SONG` (or Undo in the editor) reverses the last edit. If the song is published, say it needs publishing
again before players get the changes.
