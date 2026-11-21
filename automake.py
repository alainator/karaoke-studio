"""Party auto-make (contract/ENCORE-STUDIO.md §2.2b): the rules, with no web code (app.py runs it).

A linked player's request may carry `auto: true` (its host turned auto-make on for the party). The Studio takes it
only when an admin turned "Party auto-make" on (Players tab) AND allowed that player ("may auto-make", off by
default), within the limits below. Taken, it searches, picks the video and lyrics as the Make page would
(autopick.py), makes the song, and publishes it without review if it clears the quality floor; otherwise the song
waits at `ready` with the reason "needs a look". It's a Studio feature, not an agent: no agent key can start it,
and it never publishes over a song that's already in the library.

The quality floor (why 60%): the Studio's by-ear check says how many lines it heard sung where they sit
(result.json confidence: sure of lines). Over the 47 songs people had published by 2026-11-20 (each played through
and fixed by a person), the 10th percentile was 0.60 (median 0.875); a song below that is worse than nine in ten of
the ones people let through. A song the check couldn't follow at all (sure = null) never goes out on its own.
"Squeezed lines": a line of 3+ words shown for under 0.12 s a word (a wrong lyrics timestamp crammed it); none of
those 47 had one, 2+ in a song happened in 6 of 570 makes, all bad: 2 or more stops the publish.
"""
import json, math, os, statistics, time

DEFAULTS = {"on": False, "per_hour": 6, "max_minutes": 7, "gpu_wait": 10, "per_day": 30, "floor": 60}
LIMITS = {"per_hour": (1, 60), "max_minutes": (2, 20), "gpu_wait": (0, 120), "per_day": (1, 500), "floor": (30, 95)}
KV = "automake"
SQUEEZE_PER_WORD, SQUEEZE_WORDS, SQUEEZE_MAX = 0.12, 3, 2
MIN_LINES = 4
SEARCH_S, PUBLISH_S = 25.0, 20.0                 # finding the video and lyrics; publishing (copy + catalog)
DEFAULT_MAKE_S = {"local": 115.0, "remote": 170.0}  # median makes measured on an RTX 5090 and the helper's RTX 3090
NEEDS_A_LOOK = "needs a look"
WHO = "auto-make"


def actor(player_name):
    """Who auto-made a song, as edits and publishes are signed."""
    return {"kind": "auto", "name": f"{WHO} for {player_name or 'a player'}"[:60]}


class SettingError(ValueError):
    pass


def load(store):
    r = store.q("select v from kv where k=?", (KV,))
    out = dict(DEFAULTS)
    if r:
        try:
            saved = json.loads(r[0][0])
            out.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except ValueError:
            pass
    return out


def save(store, changes):
    """Validate, then store all of it (or nothing). Returns the new settings."""
    if not isinstance(changes, dict):
        raise SettingError("send the settings as an object")
    cur = load(store)
    for k, v in changes.items():
        if k not in DEFAULTS:
            raise SettingError(f"unknown setting: {k}")
        if k == "on":
            if not isinstance(v, bool):
                raise SettingError("on: true or false")
        else:
            lo, hi = LIMITS[k]
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise SettingError(f"{k}: a whole number from {lo} to {hi}")
        cur[k] = v
    store.q("insert or replace into kv (k, v) values (?, ?)", (KV, json.dumps(cur)))
    return cur


# ---------------- the quality floor ----------------
def squeezed(lines):
    """Lines of 3+ words shown for under 0.12 s a word (a wrong lyrics timestamp crammed them)."""
    n = 0
    for l in lines:
        w = l.get("words") or []
        if l.get("backing") or len(w) < SQUEEZE_WORDS:
            continue
        if (w[-1][1] - w[0][0]) / len(w) < SQUEEZE_PER_WORD:
            n += 1
    return n


def check(result, lines, floor_pct):
    """(passes, why, stats) for a finished song: enough lines heard in place, no squeezed-lines disaster."""
    conf = (result or {}).get("confidence") or {}
    n, sure = conf.get("lines") or 0, conf.get("sure")
    sq = squeezed(lines)
    stats = {"lines": n, "sure": sure, "squeezed": sq, "floor": floor_pct}
    if n < MIN_LINES:
        return False, f"only {n} line{'s' if n != 1 else ''} of lyrics", stats
    if sure is None:
        return False, "the Studio couldn't follow the singing by ear", stats
    if sure < floor_pct / 100.0 * n:
        return False, f"{sure} of {n} lines heard in place (the floor is {floor_pct}%)", stats
    if sq >= SQUEEZE_MAX:
        return False, f"{sq} lines squeezed into too little time", stats
    return True, None, stats


# ---------------- why it failed (the guest sees it) ----------------
def failure_reason(job):
    """A short reason for a make that failed or was stopped, from where it stopped and what it said."""
    if job.get("status") == "cancelled":
        return "it was stopped at the Studio"
    err = (job.get("error") or "").lower()
    step = job.get("step") or ""
    if "yt-dlp" in err or step == "download" or "bad youtube id" in err:
        return "couldn't download the video"
    if step == "lyrics" or "lyrics" in err and ("empty" in err or "script" in err or "kara.moe" in err):
        return "no timed lyrics found"
    if step == "align":
        return "lyrics didn't match the singing"
    if "out of memory" in err:
        return "the graphics card ran out of memory"
    return "the make failed at the Studio"


def stage_of(step):
    """The contract's stage for a job's step."""
    return "downloading" if step in (None, "", "queued", "lyrics", "download") else "making"


# ---------------- when it will be ready ----------------
def make_seconds(jobs, local_name):
    """Median make time (start to finish, download included) per card from the last 30 finished songs on each;
    the measured defaults until there are 3."""
    by = {"local": [], "remote": []}
    for j in sorted(jobs, key=lambda j: j.get("finished") or 0, reverse=True):
        s, f = j.get("started"), j.get("finished")
        if j.get("status") != "done" or not s or not f or not 20 <= f - s <= 3600:  # (a real make takes minutes)
            continue
        where = "local" if (j.get("gpu") or "") == local_name else "remote"
        if len(by[where]) < 30:
            by[where].append(f - s)
    return {k: statistics.median(v) if len(v) >= 3 else DEFAULT_MAKE_S[k] for k, v in by.items()}


def plan(now, slots, running, queued, secs, extra=None):
    """When each queued song starts and ends, first come first served on the cards that are up.
    slots: {"local": bool, "remote": bool} (which cards take songs); running: {where: started}; queued: [(id, pref)]
    in queue order (pref auto/local/remote); secs: make seconds per card; extra: one more (id, pref) at the end (a
    song not queued yet). -> {id: (start, end, where)}; a song no card can take is left out."""
    free = {}
    for where, up in slots.items():
        if up:
            st = running.get(where)
            free[where] = max(now + 30.0, st + secs[where]) if st else now
    out = {}
    for jid, pref in list(queued) + ([extra] if extra else []):
        cands = [w for w in free if pref in ("auto", w)]
        if not cands:
            continue
        w = min(cands, key=lambda w: (free[w] + secs[w], w != "local"))
        start = free[w]
        free[w] = start + secs[w]
        out[jid] = (start, free[w], w)
    return out


def round_eta(t):
    return float(math.ceil(t / 10.0) * 10)
