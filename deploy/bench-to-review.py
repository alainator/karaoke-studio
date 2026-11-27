#!/usr/bin/env python3
"""Put an agent benchmark's disputed lines on the Studio's Review tab: lines the person kept that the benchmark's
agent moved ("kept_lines" in agent-bench.py's score), so a person can say by ear who was right. Shown blind (A and B
in a fixed random order); nothing changes in the song unless the person picks the agent's timing.

    python3 deploy/bench-to-review.py OUT SONG:LINE [SONG:LINE ...]   [--what "agent benchmark"] [--dry]
    python3 deploy/bench-to-review.py OUT --all-kept [--max N]

OUT: an agent-bench.py folder (truth/, jobs/), only read. SONG: a job id (or a unique part of it); LINE: the line
number as the score prints it (the person's version). The live line is found by its words and start in the Studio
(STUDIO_URL, default http://127.0.0.1:5570); the agent's timing is sent as the other option, in the Studio's clock.
"""
import argparse, importlib.util, json, os, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("agent_bench", os.path.join(HERE, "agent-bench.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)
URL = os.environ.get("STUDIO_URL", "http://127.0.0.1:5570").rstrip("/")


def call(path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "ks/bench-to-review"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def agent_line(out, song, line):
    """(person's line, agent's line, person offset, agent offset) for the person's line number `line`."""
    truth = json.load(open(os.path.join(out, "truth", song + ".json")))
    now = json.load(open(os.path.join(out, "jobs", song, "words.json")))
    now = now if isinstance(now, dict) else {"lines": now}
    job = json.load(open(os.path.join(out, "jobs", song, "job.json")))
    no = float(job.get("offset") or 0.0)
    A, P, N = B.lead(truth["auto"]["lines"]), B.lead(truth["person"]["lines"]), B.lead(now["lines"])
    ao, po = truth["auto"]["offset"], truth["person"]["offset"]
    mp, mn = B.match(A, P, ao, po), B.match(A, N, ao, no)
    pnum = {id(l): k + 1 for k, l in enumerate(truth["person"]["lines"])}
    for i in range(len(A)):
        if mp[i] is not None and pnum.get(id(P[mp[i]])) == line:
            if mn[i] is None:
                raise SystemExit(f"{song} line {line}: the agent deleted it")
            return P[mp[i]], N[mn[i]], po, no
    raise SystemExit(f"{song}: no line {line} in the person's version")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out"); ap.add_argument("lines", nargs="*")
    ap.add_argument("--all-kept", action="store_true"); ap.add_argument("--max", type=int, default=50)
    ap.add_argument("--what", default="agent benchmark"); ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    songs = sorted(os.listdir(os.path.join(a.out, "truth")))
    want = []
    if a.all_kept:
        sc = json.load(open(a.out.rstrip("/") + "-score.json"))
        S = sc["songs"] if isinstance(sc["songs"], list) else list(sc["songs"].values())
        want = [(s["id"], k["line"]) for s in S for k in s.get("kept_lines", []) if k.get("delta") is not None][:a.max]
    for x in a.lines:
        q, _, n = x.rpartition(":")
        hit = [s[:-5] for s in songs if q in s[:-5]]
        if len(hit) != 1:
            raise SystemExit(f"{q}: {'no' if not hit else len(hit)} songs match")
        want.append((hit[0], int(n)))
    for song, line in want:
        p, n, po, no = agent_line(a.out, song, line)
        live = call(f"/api/jobs/{song}/lines")
        lo = float(live.get("offset") or 0.0)
        words = [[round(w[0] + no - lo, 3), round(w[1] + no - lo, 3), w[2]] for w in n["words"]]
        text = " ".join(w[2] for w in p["words"])
        start = p["words"][0][0] + po - lo
        d = words[0][0] - start
        print(f"{song} line {line}: agent {d:+.2f} s", end="")
        if a.dry:
            print(" (dry)")
            continue
        try:
            r = call("/api/reviews", {"job": song, "text": text, "start": start, "words": words, "kind": "bench",
                                      "what": a.what})
            print(f" -> review {r['id']}")
        except urllib.error.HTTPError as e:
            print(f" -> refused: {e.read().decode()[:160]}")


if __name__ == "__main__":
    main()
