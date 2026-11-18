"""Reading a kara.moe (Karaoke Mugen) lyrics file: (sung start, text) per line.

The files are ASS subtitles made for a karaoke player, and some carry more than the lyrics: title cards, credits,
"typewriter" intros that add a letter every tenth of a second. Two things tell them apart:
- Karaoke Mugen writes the sung lines with the Effect field "fx" (generated from its templates). When a file has
  such lines, only those are lyrics; other Dialogue lines are decoration.
- Decoration lines follow each other far faster than anyone sings, so a style paced like that is left out when the
  file also has a normally paced one.
(Seen: an entry opening with 43 effect lines in 8 seconds; read as lyrics, they threw the song 28 s off.)
"""
import re


def parse(text):
    rows = []  # (style, start of singing, words, effect field)
    for l in text.splitlines():
        if not l.startswith("Dialogue:"):
            continue
        f = l.split(",", 9)
        if len(f) < 10:
            continue
        try:
            h, m_, sec = f[1].split(":")
            start = int(h) * 3600 + int(m_) * 60 + float(sec)
        except ValueError:
            continue
        body = f[9]
        # leading \k before the first syllable is lead-in time, not singing
        lead, pending = 0.0, None
        for tag, txt in re.findall(r"(\{[^}]*\})|([^{]+)", body):
            if tag:
                ks = re.findall(r"\\[kK][fo]?(\d+)", tag)
                if ks:
                    if pending is not None:
                        lead += pending
                    pending = sum(int(x) for x in ks) / 100
            elif txt.strip():
                break
        words = re.sub(r"\s+", " ", re.sub(r"\{[^}]*\}", "", body).replace("\\N", " ")).strip()
        if words:
            rows.append((f[3].strip(), start + lead, words, f[8].strip().lower()))
    if sum(1 for r in rows if r[3] == "fx") >= 4:
        rows = [r for r in rows if r[3] == "fx"]
    by_style = {}
    for style, t, _, _ in rows:
        by_style.setdefault(style, []).append(t)
    def pace(ts):  # typical time between one line and the next
        ts = sorted(ts)
        gaps = sorted(b - a for a, b in zip(ts, ts[1:]))
        return gaps[len(gaps) // 2] if gaps else 99.0
    paces = {s: pace(ts) for s, ts in by_style.items()}
    if any(p >= 1.5 for s, p in paces.items() if len(by_style[s]) >= 4):
        effects = {s for s, p in paces.items() if len(by_style[s]) >= 5 and p < 0.6}
        rows = [r for r in rows if r[0] not in effects]
    return sorted((t, w) for _, t, w, _ in rows)
