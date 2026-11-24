# Karaoke Studio for agents

Karaoke Studio makes karaoke videos and is built so an agent can do the checking and fixing that comes after: find
the lines it marked ⚠, listen and look at where they are really sung, retime or retype them, and leave notes for
the person who makes the final calls. People can also do all of it by hand in the web editor; both ways edit the same
songs, with the same Undo, and every edit is signed with who made it.

This file is for any agent (Claude Code, a local model with a shell tool, a script). It doesn't depend on one vendor.
The step-by-step playbooks are plain Markdown in `.claude/skills/` (Claude Code loads them as skills; other agents can
read them as instructions):

| Playbook | When |
|---|---|
| [`karaoke-fix`](.claude/skills/karaoke-fix/SKILL.md) | Fix one song's timing and words |
| [`karaoke-triage`](.claude/skills/karaoke-triage/SKILL.md) | Which songs need work, in what order |
| [`karaoke-make`](.claude/skills/karaoke-make/SKILL.md) | Pick the video and lyrics for new songs and queue them |

## Setup: ks

`tools/ks` is a single Python 3 file with no dependencies beyond the standard library. It talks to the Studio's
HTTP API only, so it runs on any machine that can reach the Studio (copy it, or put the repo's `tools/` on PATH).

1. In the Studio's web app, **Agents** tab: give the agent a name (it signs every edit and note with it) and tick
   what it may do. Make the key; it is shown once.
2. On the agent's machine: `ks setup --url http://<studio address>:5570`, paste the key when asked. The key is kept
   in `~/.config/karaoke-studio/agent-key` (readable only by you; `%APPDATA%\karaoke-studio\` on Windows) and never
   printed. Or set `STUDIO_URL` and `STUDIO_AGENT_KEY` (or `STUDIO_AGENT_KEY_FILE`, a file holding the key) in the
   agent's environment.
3. `ks whoami`: who the Studio takes you for and what your key allows. `ks status`: is it up, what's queued.

Without a key, ks still works against a Studio that doesn't require one (`AGENT_KEYS_REQUIRED` off): its edits are
signed "ks" and nothing is restricted. Give agents a key anyway, so their edits carry their name and the risky
actions stay off.

## What the key allows (scopes)

| Scope | Default | Allows |
|---|---|---|
| read | on | songs, lines, what was heard, pictures, logs, search |
| edit | on | move, stretch, retype, split, join, add and delete lines; offset; reading; rename an unpublished song; undo |
| notes | on | notes on lines, flag a line for the person (`ks note … --mark`) |
| make | on | queue new songs, import a list or playlist (`ks import`), cancel a make |
| clear_marks | off | clear ⚠ marks (`ks ok`), "checked by you", dismiss player flags |
| publish | off | publish, rename a published song, remove a song from the library (`ks remove-from-library`), refresh the library's catalog (`ks refresh-library`) |
| remake | off | remake, change video or lyrics, retry, trim, custom videos, reset to the automatic version |
| delete | off | delete songs, remove old versions from the library (with publish: `ks remove-from-library --delete-job`) |

The Studio checks the scope on every call and answers 403 with the scope it needed. Key management, linked players
and requests are for people only. A mark that goes because its line was moved or reworded is an edit (the web editor
does the same); clearing a mark without changing the line needs clear_marks.

## Rules

- **Never clear ⚠ marks or publish unless the key allows it and the person asked you to.** The marks are the
  person's review list. If a marked line is fine, say so with a note and leave the mark.
- Publishing, removing from the library, remakes, resets, trims, custom videos and deletes change what players get
  or redo work: only when asked. `ks remove-from-library` lists the files and asks; use `--yes` only when the person
  asked for that exact song to go.
- Don't quote song lyrics in chat or reports (they're copyrighted): refer to line numbers and times ("line 14, 0:36").
- Don't edit a song the person is editing in the web app at the same moment. ks refuses to save when the song changed
  since it read it; if that happens, ask.
- Make the smallest change that fixes a line. Don't retime a line just to clear its mark.
- Every edit can be undone: `ks undo SONG` takes back your own last edit (`--any` takes back the last one whoever
  made it, only when asked); `ks history SONG` lists them. People have the same Undo in the editor.

## Notes

A note sits on one line, next to its ⚠ reason in the web editor, signed with your agent's name ("Claude: looks fine:
held note over the guitar"). Keep it to one short sentence that tells the person what to do or why not:

- `looks fine: <why>`: you looked and listened and the line sits right ("looks fine: drawn-out vowel, block covers
  the phrase").
- `check: <what>`: something you couldn't settle ("check: the second word may be 'night', heard 'light'").
- `moved: <what and why>` only when the edit itself isn't obvious; normal fixes don't need a note (the edit is signed).

`ks note SONG N "text"` on a ⚠ line; add `--mark` to flag an unmarked line; `--clear` removes your note. Notes are
undoable like edits.

## The quality bar

- A line must not light up late: more than about 1 s after its singing starts is wrong.
- Early is fine within reason: lines show about 2 s before their first word anyway; up to about 2.5 s early is within
  the Studio's tolerance.
- A line stays up until its singing ends (held last notes included).
- Word-by-word perfection isn't the goal: up a little early and long enough to sing along. The person does the final
  touches.

## The quick first pass

`ks autofix SONG` applies only the fixes the by-ear check is clear about: ⚠ lines heard 1-15 s late or early beyond
the 2.5 s tolerance, and a run of three or more neighbouring lines off by the same amount moved together as one
section. It checks again after every fix, skips songs the studio couldn't follow by ear, never touches lines marked
fine and never clears a mark. `--dry` shows what it would do. People have the same in the web editor: **🎧 Check by
ear** in a song's ⚠ box, with a Move button per line and "Apply all clear fixes" (nothing is saved until Save).
`ks list --json` and `ks check SONG --json` give the same information as data, for scripts.

## The video check (and publishing by itself)

Every song made gets a **video check** (`ks show` prints it; the song page shows it above Publish): the language the
singing was listened to in vs the lyrics' language, whether the album recording was found and used (and why not),
lyrics timestamps that run past the video (a copy for a longer version), lyrics left out, and the quality floor (60% of
lines heard in place, the same rule as party auto-make). A song that's hard to hear (shouted, mumbled, a "na na na"
outro) scores like a wrong one, so a low score only holds a song for a look; it's never remade by itself.

`ks make ... --auto` (needs the "publish" scope; only when the person asked for songs to go out unreviewed): when
the song is made, it's published if its check passes (the old YouTube copy of the song is removed, as Publish
offers); a song listened to in the wrong language is made again once in the lyrics' language; anything else waits,
with the reason on its page and in `ks show` ("held: ..."). Its ⚠ lines stay for the person either way.

## Verifying by ear and by picture

You can't listen, but the Studio can, and it can draw what it hears:

- `ks check SONG`: the Studio's by-ear check of the lines as they are now: which lines are heard somewhere else, and
  the move to try.
- `ks heard SONG --line N`: the words the transcription heard around a line, with times.
- `ks look SONG --line N`: a PNG of the vocals' spectrogram (singing = bright stripes), the melody (pitch of the sung
  frames, a tick at each new note: syllables show inside unbroken singing), the line blocks and the heard words under
  them. A block should sit over its own stripes and words. Open the picture; don't guess. `--align` adds where the
  aligner puts line N and the boundary with the line before.
- `ks align SONG N`: the forced aligner (the one that timed the song) hears line N together with the line before
  it, from the previous line's start to the next line's start: each word's place and confidence, the start vs now,
  the boundary, and the pair edit to try (`--window START END`: every line in a stretch; `--json` for scripts).
  It uses the Studio's GPU for a few seconds and answers "busy" while a song is being made. (A song with no pitch
  file yet gets one on its first `ks look`: a few seconds more.)
- Lines sung straight on from the previous one are the commonest late lines (0.5-2 s): no dark gap shows, the
  crowd's lyric timing shares the late start (`ks alt-lyrics` won't help), and the line before's end usually runs
  into them. Fix the boundary (the line before's end, then the line's start) when the heard words or the melody row
  show the singing starts earlier (by up to about 2 s: one clear signal is enough; early is harmless, late is the
  error). Moving a line later, or by more than about 2 s, needs two signals; otherwise leave a `check:` note. A late
  chorus line: check and fix every repeat alike. `ks align` is a second opinion: not on chorus openers after a held
  note, duet replies or repeated lines.
- `ks frame SONG --line N`: the video frame with the lyrics as players show them.
- After editing, run `ks check` again. If the Studio couldn't follow the song by ear (few or no heard words), judge
  each line by its stripes and say how sure you are.

## Reporting back

What you changed (how many lines, what kind, roughly where), what's still uncertain and why (your notes), that
`ks undo SONG` or Undo in the editor reverses edits, and whether the song needs publishing again (published songs
keep the old version until then).

## For people running a Studio

- The **Agents** tab makes, changes and revokes keys; the list shows when each key was last used. Keys are stored
  only as hashes.
- `AGENT_KEYS_REQUIRED=1` (in `.env`) makes API calls without a login header need a key: use it when other programs
  can reach the Studio's port. `STUDIO_PERSON_HEADER` names the header your login proxy sets (default
  `X-authentik-username`); edits by people are signed with it, otherwise "web".
- The song page says who edited it last; the editor's **Undo** takes back the last edit, whoever made it.
- `deploy/agent-bench.py` measures an agent against your own hand fixes: it copies songs you fixed into a sandbox
  reset to the Studio's automatic version (your fixes kept apart), runs a throwaway Studio on them with a key that
  can only read, edit and leave notes, and scores the agent's result line by line against yours. The fixing agent
  needs only `STUDIO_URL` and `STUDIO_AGENT_KEY_FILE` (ks reads the key from that file). The script's own help says
  how to run it.
