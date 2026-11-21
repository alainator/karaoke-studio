# Karaoke Studio

Read [AGENTS.md](AGENTS.md): how to check and fix songs with `tools/ks`, what an agent key allows, the rules (never
clear ⚠ marks or publish unless the key allows it and the person asked), notes, the quality bar, and how to verify by
ear and by picture. The playbooks are the skills in `.claude/skills/` (karaoke-fix, karaoke-triage, karaoke-make).

Working on the Studio's code instead:
- Tests (no GPU needed): `python -I -W ignore::ResourceWarning -m unittest discover -s tests -t .` with the Studio's
  Python (in Docker: the image's). New endpoints that change anything need a scope in `agents.ROUTES` (a test fails
  otherwise).
- `config.CODE_FILES` is the song-making code's fingerprint: a change to any of them makes the GPU helper wait until
  it has the same code (it updates itself from the repository).
- GPU code goes through `gpu.py` (see CONTRIBUTING.md, "Hardware").
- Keep the README short: one-line feature bullets, no test anecdotes.
