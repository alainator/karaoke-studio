"""Karaoke .ass from word timings (shared by the pipeline and the studio's timing slider)."""

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: RowA,Arial,72,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,60,60,170,1
Style: RowB,Arial,72,&H0000D7FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,60,60,70,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ts(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def clean(text):
    # braces and backslashes are ASS override syntax
    return text.replace("{", "(").replace("}", ")").replace("\\", "/")


def make_ass(lines, offset=0.0):
    """lines: [[[start, end, word], ...], ...] in seconds. offset shifts everything (+ = later)."""
    segs = [[(a + offset, b + offset, w) for a, b, w in l] for l in lines if l]
    events, hide = [], []
    for i, words in enumerate(segs):
        start, end = words[0][0], words[-1][1]
        show = start - 1.5
        if i >= 2:
            show = max(show, hide[i - 2] + 0.05)   # the row it reuses must be free
        if i >= 1:
            show = max(show, segs[i - 1][0][0] - 0.2)  # don't appear before the previous line starts
        show = max(0.0, min(show, start))
        h = end + 0.4
        hide.append(h)
        parts = [f"{{\\k{round((start - show) * 100)}}}"]
        for j, (a, b, w) in enumerate(words):
            nxt = words[j + 1][0] if j + 1 < len(words) else b
            parts.append(f"{{\\kf{max(1, round((nxt - a) * 100))}}}{clean(w)}{' ' if j + 1 < len(words) else ''}")
        events.append(f"Dialogue: 0,{ts(show)},{ts(h)},{'RowA' if i % 2 == 0 else 'RowB'},,0,0,0,,{''.join(parts)}")
    return HEADER + "\n".join(events) + "\n"
