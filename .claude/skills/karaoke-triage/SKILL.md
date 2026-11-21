---
name: karaoke-triage
description: Find which Karaoke Studio songs need work and in what order - songs with lines marked ⚠, songs the studio couldn't follow by ear, failed songs - and summarize them or work through them. Use when asked which songs need fixing, for an overview or report of the library or a batch, or to go through the songs that need work.
---

# Which songs need work

- `ks status`: one line about the studio: songs, how many have lines to fix, failures, what's being made.
- `ks list --fix`: songs with ⚠ lines, most first. Columns: lines to fix, heard in place / lines, language,
  flags (P published, * changes not published yet, ✓ checked by the person, e edited), name, id.
- `ks list --failed`, then `ks log SONG` for why. A YouTube "403 Forbidden" is usually temporary: a retry
  fixes it (`ks retry SONG`, when the person agrees and your key allows remakes).
- `ks list --q "text"` finds songs by name.

## Sorting what you find
1. A few ⚠ lines (1-5) on a song that is otherwise heard in place: quick checks; often each one just needs a look
   and a note ("looks fine: ...") for the person to confirm.
2. Many ⚠ lines with the same large offset in `ks check` (for example lines 33-42 all heard 54 s earlier): the
   video's cut differs from the lyrics (a section left out, repeated or moved). One `--ripple` move per section.
3. Lines crammed together (several lines in a few seconds, overlapping): the aligner couldn't place a section.
   Find where each is heard (`ks heard`, `ks look`) and move them one by one.
4. "?" in the heard column, or very few heard: the studio couldn't follow the singing by ear (fast rap, heavy
   effects, a language Whisper handles badly). Fixing needs the spectrogram; be clear about how sure you are.
5. Published songs with ⚠ lines matter most if people sing them: say which ones are published.

When asked for a summary, give counts and the worst few by name, not the whole list. When asked to work through
songs, take them one at a time with the karaoke-fix skill, and report after each one or each few.
