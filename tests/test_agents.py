"""Agents: keys with scopes (every mutating endpoint refuses without its scope), key lifecycle, AGENT_KEYS_REQUIRED,
edit attribution (person, agent, ks, web) with the shared Undo, notes signed by the studio, gpu.py's fallbacks, and
remote makes cut by their trim exactly like local ones. No GPU needed."""
import asyncio, json, os, shutil, sqlite3, stat, subprocess, sys, tempfile, threading, time, types, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
from tests import _asgi
import app as A  # noqa: E402
import agents  # noqa: E402
import config as C  # noqa: E402
import gpu  # noqa: E402
import trimtools  # noqa: E402
from assgen import load_lines  # noqa: E402

n = [0]


def make_song(lines=None, **kw):
    """A finished song with words.json (no media)."""
    n[0] += 1
    jid = time.strftime("%Y%m%d-%H%M%S") + f"-agent-song-{n[0]}"
    os.makedirs(f"{A.JOBS}/{jid}")
    lines = lines or [{"words": [[1.0, 2.0, "one"], [2.0, 2.5, "two"]]},
                      {"words": [[4.0, 5.0, "three"]], "weak": True, "why": "not heard here"},
                      {"words": [[7.0, 8.0, "four"]], "weak": True, "why": "late"},
                      {"words": [[10.0, 11.0, "five"]]}]
    json.dump({"version": 2, "lines": lines}, open(f"{A.JOBS}/{jid}/words.json", "w"))
    A.save(jid, {"id": jid, "yt": "abcdefghijk", "lrc_id": 1, "lyrics_source": "lrclib", "artist": "Tester",
                 "title": f"Agent song {n[0]}", "status": "done", "step": "done", "created": time.time(), "offset": 0.0,
                 "result": {"duration": 60.0, "lines": len(lines)}, "log": [], **kw})
    return jid


def api(method, url, body=None, key=None, headers=None):
    h = dict(headers or {})
    if key:
        h["X-Studio-Agent-Key"] = key
    return _asgi.jcall(A.app, method, url, body, h)


def new_key(name, scopes):
    key, row = A.KEYS.create(name, scopes)
    return key


class Scopes(unittest.TestCase):
    """Every API route that changes something is listed with a scope, and a key without that scope is refused."""

    @classmethod
    def setUpClass(cls):
        cls.jid = make_song()

    def routes(self):
        from fastapi.routing import APIRoute
        out = []
        for r in A.app.routes:
            if not isinstance(r, APIRoute) or not r.path.startswith("/api/") or r.path.startswith("/api/encore/"):
                continue
            for m in sorted(r.methods - {"HEAD"}):
                path = r.path.replace("{jid}", "20000101-000000-no-such-song")
                for name in ("key", "kid", "rid", "action", "player", "uid", "path", "name"):
                    path = path.replace("{%s}" % name, "x" + name)
                out.append((m, path, r.path))
        return out

    def test_every_route_is_mapped(self):
        unmapped = [(m, t) for m, p, t in self.routes() if m != "GET" and agents.scope_for(m, p) is None]
        self.assertEqual(unmapped, [], "add these to agents.ROUTES")
        for m, p, t in self.routes():
            self.assertIn(agents.scope_for(m, p), agents.SCOPE_NAMES + [agents.ADMIN], (m, t))

    def test_each_route_refuses_without_its_scope(self):
        checked = 0
        everything = new_key("All but one", agents.SCOPE_NAMES)
        for m, p, t in self.routes():
            scope = agents.scope_for(m, p)
            if p == "/api/agent/whoami":
                continue
            if scope == agents.ADMIN:  # never for agents, whatever the key allows
                st, _, b = api(m, p, {}, key=everything)
                self.assertEqual(st, 403, (m, t))
                self.assertIn("admins", b["detail"])
                continue
            others = [s for s in agents.SCOPE_NAMES if s != scope]
            A.KEYS.update(A.KEYS.auth(everything)["id"], others)
            st, _, b = api(m, p, {}, key=everything)
            self.assertEqual(st, 403, (m, t, st, b))
            self.assertEqual(b.get("scope"), scope, (m, t))
            # with the scope, the gate lets it through (the song doesn't exist: the endpoint answers 4xx itself)
            A.KEYS.update(A.KEYS.auth(everything)["id"], [scope])
            st, _, b = api(m, p, {}, key=everything)
            self.assertFalse(st == 403 and "doesn't allow" in str((b or {}).get("detail")), (m, t, st, b))
            checked += 1
        self.assertGreater(checked, 40)

    def test_body_level_scopes(self):
        jid = make_song()
        key = new_key("Fixer", ["read", "edit"])
        lines = api("GET", f"/api/jobs/{jid}/lines", key=key)[2]["lines"]
        # clearing a mark without moving the line: clear_marks
        cleared = json.loads(json.dumps(lines)); cleared[1]["weak"] = False
        st, _, b = api("POST", f"/api/jobs/{jid}/lines", {"lines": cleared}, key=key)
        self.assertEqual(st, 403); self.assertIn("clear_marks", b["detail"])
        ok = json.loads(json.dumps(lines)); ok[2]["weak"], ok[2]["ok"] = False, True
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines", {"lines": ok}, key=key)[0], 403)
        # moving a marked line clears its mark: that's an edit (the studio's own rule)
        moved = json.loads(json.dumps(lines)); moved[1]["weak"] = False
        moved[1]["words"] = [[w[0] + 0.5, w[1] + 0.5, w[2]] for w in moved[1]["words"]]
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines", {"lines": moved}, key=key)[0], 200)
        # a note through the lines needs notes
        lines = api("GET", f"/api/jobs/{jid}/lines", key=key)[2]["lines"]
        lines[2]["note"] = {"by": "someone", "text": "hello"}
        st, _, b = api("POST", f"/api/jobs/{jid}/lines", {"lines": lines}, key=key)
        self.assertEqual(st, 403); self.assertIn("notes", b["detail"])
        # with clear_marks the clear goes through
        A.KEYS.update(A.KEYS.auth(key)["id"], ["read", "edit", "clear_marks"])
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines", {"lines": cleared_now(jid)}, key=key)[0], 200)

    def test_rename_published_and_change_need_more(self):
        jid = make_song(published=time.time(), published_as="Tester - Pub")
        os.makedirs(A.MV_DIR, exist_ok=True)
        open(f"{A.MV_DIR}/Tester - Pub.mp4", "wb").write(b"x")
        key = new_key("Renamer", ["read", "edit", "make"])
        st, _, b = api("POST", f"/api/jobs/{jid}/rename", {"artist": "A", "title": "B"}, key=key)
        self.assertEqual(st, 403); self.assertIn("publish", b["detail"])
        st, _, b = api("POST", "/api/jobs", {"yt": "abcdefghijk", "lrc_id": 5, "artist": "A", "title": "B", "replace": jid}, key=key)
        self.assertEqual(st, 403); self.assertIn("remake", b["detail"])
        unpub = make_song()
        self.assertEqual(api("POST", f"/api/jobs/{unpub}/rename", {"artist": "Tester", "title": "Renamed"}, key=key)[0], 200)
        self.assertEqual(A.load(unpub)["last_edit"]["by"], "Renamer")


def cleared_now(jid):
    lines = load_lines(A.job_path(jid, "words.json"))
    for l in lines:
        l["weak"] = False
    return lines


class Keys(unittest.TestCase):
    def test_lifecycle(self):
        key, row = A.KEYS.create("Lifecycle", None, by="alex")
        self.assertTrue(key.startswith(agents.KEY_PREFIX)); self.assertGreater(len(key), 40)
        self.assertEqual(row["scopes"], agents.DEFAULT_SCOPES)
        self.assertNotIn("clear_marks", row["scopes"]); self.assertNotIn("publish", row["scopes"])
        # only the hash is kept, and the listing never carries it
        raw = open(A.KEYS.path, "rb").read()
        self.assertNotIn(key.encode(), raw)
        self.assertIn(agents.key_hash(key), [r[0] for r in sqlite3.connect(A.KEYS.path).execute("select hash from keys")])
        listed = [k for k in api("GET", "/api/agents")[2]["keys"] if k["id"] == row["id"]][0]
        self.assertNotIn("hash", listed); self.assertNotIn(key, json.dumps(listed))
        self.assertIsNone(listed["last_used"])
        # whoami with the key: the agent and its scopes; last used is now set
        st, _, w = api("GET", "/api/agent/whoami", key=key)
        self.assertEqual((st, w["kind"], w["name"]), (200, "agent", "Lifecycle"))
        self.assertIsNotNone(A.KEYS.get(row["id"])["last_used"])
        # scopes can change; a revoked key is refused (401), never treated as no key
        self.assertEqual(api("PATCH", f"/api/agents/{row['id']}", {"scopes": ["read"]})[2]["agent"]["scopes"], ["read"])
        self.assertEqual(api("POST", "/api/jobs", {}, key=key)[0], 403)
        self.assertEqual(api("DELETE", f"/api/agents/{row['id']}")[0], 200)
        st, _, b = api("GET", "/api/jobs", key=key)
        self.assertEqual(st, 401); self.assertIn("unknown agent key", b["detail"])
        self.assertEqual(api("DELETE", f"/api/agents/{row['id']}")[0], 404)

    def test_bad_input(self):
        self.assertEqual(api("POST", "/api/agents", {"name": ""})[0], 400)
        self.assertEqual(api("POST", "/api/agents", {"name": "X", "scopes": ["root"]})[0], 400)
        self.assertEqual(api("POST", "/api/agents", {"name": "<script>"})[0], 400)
        self.assertEqual(api("POST", "/api/agents", {"name": "Twice"})[0], 200)
        self.assertEqual(api("POST", "/api/agents", {"name": "twice"})[0], 400)
        self.assertEqual(api("GET", "/api/jobs", key="not-a-key")[0], 401)
        self.assertEqual(api("GET", "/api/jobs", key=agents.KEY_PREFIX + "x" * 43)[0], 401)

    def test_agents_cant_manage_keys(self):
        key = new_key("Sneaky", agents.SCOPE_NAMES)
        for m, p, b in (("GET", "/api/agents", None), ("POST", "/api/agents", {"name": "Mine"}),
                        ("POST", "/api/players/code", {}), ("GET", "/api/players", None)):
            self.assertEqual(api(m, p, b, key=key)[0], 403, p)

    def test_required(self):
        jid = make_song()
        key = new_key("Required", ["read"])
        try:
            C.AGENT_KEYS_REQUIRED = True
            st, _, b = api("GET", "/api/jobs")
            self.assertEqual(st, 401); self.assertIn("agent key", b["detail"])
            self.assertEqual(api("GET", "/api/jobs", headers={"X-authentik-username": "alex"})[0], 200)
            self.assertEqual(api("GET", "/api/jobs", key=key)[0], 200)
            self.assertEqual(api("GET", f"/api/jobs/{jid}/lines", key=key)[0], 200)
            self.assertEqual(_asgi.call(A.app, "GET", "/")[0], 200)                 # the page itself isn't the API
            self.assertEqual(_asgi.call(A.app, "GET", "/api/encore/hello")[0], 401)  # players: their own tokens
        finally:
            C.AGENT_KEYS_REQUIRED = False
        self.assertEqual(api("GET", "/api/jobs")[0], 200)  # default: as before


class Attribution(unittest.TestCase):
    def edit(self, jid, headers=None, key=None, what=None):
        lines = api("GET", f"/api/jobs/{jid}/lines")[2]["lines"]
        lines[0]["words"] = [[w[0] + 0.1, w[1] + 0.1, w[2]] for w in lines[0]["words"]]
        body = {"lines": lines, **({"what": what} if what else {})}
        st, _, b = api("POST", f"/api/jobs/{jid}/lines", body, key=key, headers=headers)
        self.assertEqual(st, 200, b)
        return A.load(jid)["last_edit"]

    def test_who(self):
        jid = make_song()
        e = self.edit(jid, headers={"X-authentik-username": "alex"})
        self.assertEqual((e["by"], e["kind"]), ("alex", "person"))
        self.assertTrue(e["what"].startswith("editor:"))
        self.assertEqual(self.edit(jid)["by"], "web")
        e = self.edit(jid, headers={"User-Agent": "ks/2.0"}, what="move line 1")
        self.assertEqual((e["by"], e["kind"], e["what"]), ("ks", "ks", "move line 1"))
        key = new_key("Claude", None)
        e = self.edit(jid, key=key, headers={"X-authentik-username": "alex"})  # a key wins over a login header
        self.assertEqual((e["by"], e["kind"]), ("Claude", "agent"))
        h = api("GET", f"/api/jobs/{jid}/history")[2]["history"]
        self.assertEqual([x["by"] for x in h], ["Claude", "ks", "web", "alex"])
        self.assertEqual(api("GET", f"/api/jobs/{jid}")[2]["last_edit"]["by"], "Claude")

    def test_undo_is_the_same_for_everyone(self):
        jid = make_song()
        before = load_lines(A.job_path(jid, "words.json"))
        key = new_key("Undoer", None)
        self.edit(jid, key=key)
        after_agent = load_lines(A.job_path(jid, "words.json"))
        self.edit(jid, headers={"X-authentik-username": "alex"})
        top = api("GET", f"/api/jobs/{jid}/history")[2]["history"][0]
        self.assertEqual(top["by"], "alex")
        # an agent may undo a person's edit (its key allows edit); a stale id is refused
        st, _, b = api("POST", f"/api/jobs/{jid}/lines/undo", {"id": "nope"}, key=key)
        self.assertEqual(st, 409)
        st, _, b = api("POST", f"/api/jobs/{jid}/lines/undo", {"id": top["id"]}, key=key)
        self.assertEqual(st, 200, b); self.assertEqual(b["undone"]["by"], "alex")
        self.assertEqual(load_lines(A.job_path(jid, "words.json")), after_agent)
        # and the person undoes the agent's
        st, _, b = api("POST", f"/api/jobs/{jid}/lines/undo", {}, headers={"X-authentik-username": "alex"})
        self.assertEqual((st, b["undone"]["by"], b["left"]), (200, "Undoer", 0))
        self.assertEqual(load_lines(A.job_path(jid, "words.json")), before)
        self.assertEqual(A.load(jid)["last_edit"]["by"], "alex")
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines/undo", {})[0], 400)

    def test_offset_undo_and_retire(self):
        jid = make_song()
        self.assertEqual(api("POST", f"/api/jobs/{jid}/offset", {"offset": 0.4}, headers={"X-authentik-username": "alex"})[0], 200)
        self.edit(jid)
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines/undo", {})[0], 200)   # the lines edit
        self.assertEqual(A.load(jid)["offset"], 0.4)                               # the offset stays
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines/undo", {})[0], 200)   # now the offset
        self.assertEqual(A.load(jid)["offset"], 0.0)
        self.edit(jid)
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines/reset", {})[0], 200)  # back to automatic: history retired
        self.assertEqual(api("GET", f"/api/jobs/{jid}/history")[2]["history"], [])

    def test_stale_read_refused(self):
        jid = make_song()
        seen = api("GET", f"/api/jobs/{jid}")[2]["edited"]
        self.edit(jid, headers={"X-authentik-username": "alex"})
        lines = api("GET", f"/api/jobs/{jid}/lines")[2]["lines"]
        st, _, b = api("POST", f"/api/jobs/{jid}/lines", {"lines": lines, "if_edited": seen}, headers={"User-Agent": "ks/2.0"})
        self.assertEqual(st, 409); self.assertIn("alex", b["detail"])

    def test_notes_are_signed_by_the_studio(self):
        jid = make_song()
        key = new_key("Noter", ["read", "notes"])
        st, _, b = api("POST", f"/api/jobs/{jid}/notes", {"lines": [2], "text": "looks fine: held note"}, key=key)
        self.assertEqual(st, 200, b)
        l = load_lines(A.job_path(jid, "words.json"))[1]
        self.assertEqual((l["note"]["by"], l["note"]["kind"], l["note"]["text"]), ("Noter", "agent", "looks fine: held note"))
        self.assertTrue(l["weak"])  # a note never clears the mark
        # an unmarked line needs mark; a line checked as fine gets no note
        self.assertEqual(api("POST", f"/api/jobs/{jid}/notes", {"lines": [1], "text": "x"}, key=key)[0], 400)
        st, _, _ = api("POST", f"/api/jobs/{jid}/notes", {"lines": [1], "text": "words may be wrong", "mark": True}, key=key)
        self.assertEqual(st, 200)
        l1 = load_lines(A.job_path(jid, "words.json"))[0]
        self.assertTrue(l1["weak"]); self.assertEqual(l1["why"], "flagged by Noter")
        # the editor sends notes back as they are: kept; a forged author is re-signed by whoever saves
        lines = api("GET", f"/api/jobs/{jid}/lines")[2]["lines"]
        lines[3]["note"] = {"by": "Noter", "kind": "agent", "text": "forged"}
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines", {"lines": lines}, headers={"X-authentik-username": "alex"})[0], 200)
        now = load_lines(A.job_path(jid, "words.json"))
        self.assertEqual(now[1]["note"]["by"], "Noter")
        self.assertEqual((now[3]["note"]["by"], now[3]["note"]["kind"]), ("alex", "person"))
        # an agent clears only its own note; undo takes a note back
        self.assertEqual(api("POST", f"/api/jobs/{jid}/notes", {"lines": [4], "text": ""}, key=key)[2]["changed"], 0)
        self.assertEqual(api("POST", f"/api/jobs/{jid}/notes", {"lines": [2], "text": ""}, key=key)[0], 200)
        self.assertIsNone(load_lines(A.job_path(jid, "words.json"))[1]["note"])
        self.assertEqual(api("POST", f"/api/jobs/{jid}/lines/undo", {}, key=new_key("Undo2", ["edit"]))[0], 200)
        self.assertEqual(load_lines(A.job_path(jid, "words.json"))[1]["note"]["text"], "looks fine: held note")

    def test_publish_records_who(self):
        jid = make_song()
        for f in ("out.mp4", "vocals.m4a"):
            open(A.job_path(jid, f), "wb").write(b"x")
        A.rebuild_lyrics(jid, A.load(jid))
        st, _, b = api("POST", f"/api/jobs/{jid}/publish", {}, headers={"X-authentik-username": "alex"})
        self.assertEqual(st, 200, b)
        self.assertEqual(A.load(jid)["published_by"]["by"], "alex")


class KsHelperApi(unittest.TestCase):
    """What ks asks the Studio to work out (tools/ks_helper.py through the API): by ear, lyrics scores, romaji."""

    def test_check_altlyrics_romaji(self):
        jid = make_song()
        json.dump({"lang": "en", "words": [[1.0, 1.4, "one"], [1.5, 2.0, "two"], [4.1, 4.9, "three"], [7.0, 7.8, "four"],
                                           [10.0, 10.8, "five"]]}, open(A.job_path(jid, "heard.json"), "w"))
        key = new_key("Reader", ["read"])
        st, _, c = api("GET", f"/api/jobs/{jid}/agent/check", key=key)
        self.assertEqual(st, 200, c); self.assertEqual(c["lead"], 4); self.assertEqual(c["heard_words"], 5)
        self.assertEqual(api("GET", f"/api/jobs/{jid}/heard", key=key)[2]["lang"], "en")
        synced = "\n".join(f"[00:{t:05.2f}]{w}" for t, w in ((1.0, "one two one two"), (4.0, "three three three"),
                                                             (7.0, "four four four"), (10.0, "five five five")))
        st, _, b = api("POST", f"/api/jobs/{jid}/agent/alt-lyrics", {"candidates": [{"id": 11, "synced": synced}, {"id": 12, "synced": ""}]}, key=key)
        self.assertEqual(st, 200, b)
        self.assertEqual(sorted(r["id"] for r in b["scores"]), [11, 12])
        self.assertEqual([r["lines"] for r in b["scores"] if r["id"] == 11], [4])
        st, _, b = api("POST", "/api/agent/romaji", {"lines": ["きみのなまえ"]}, key=key)
        self.assertEqual(st, 200, b); self.assertTrue(b["romaji"][0].strip()); self.assertNotIn("き", b["romaji"][0])
        self.assertEqual(api("POST", "/api/agent/romaji", {"lines": []}, key=key)[2], {"romaji": []})


class Gpu(unittest.TestCase):
    """gpu.py: nvidia-smi first, rocm-smi second, nothing = 0; the device follows KARAOKE_DEVICE, then PyTorch."""

    def setUp(self):
        self.bin = tempfile.mkdtemp(prefix="fakebin-")
        self.path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bin  # only the fake tools

    def tearDown(self):
        os.environ["PATH"] = self.path
        os.environ.pop(gpu.ENV, None)
        shutil.rmtree(self.bin, ignore_errors=True)

    def tool(self, name, out):
        p = os.path.join(self.bin, name)
        with open(p, "w") as f:
            f.write("#!/bin/sh\nprintf '%s\\n' '" + out.replace("'", "") + "'\n")  # (printf: a shell builtin; PATH has only these)
        os.chmod(p, 0o755)

    def test_nothing(self):
        self.assertEqual((gpu.free_mib(), gpu.name(), gpu.info()), (0, None, ("?", 0)))

    def test_nvidia(self):
        self.tool("nvidia-smi", "NVIDIA GeForce RTX 3090, 24576, 2048\nNVIDIA GeForce RTX 3090, 24576, 0")
        self.tool("rocm-smi", "{}")  # (not asked: NVIDIA comes first)
        self.assertEqual(gpu.free_mib(), 22528)
        self.assertEqual(gpu.info(), ("NVIDIA GeForce RTX 3090", 22528))
        self.assertIs(A.gpumod, gpu)  # (A.gpu_free_mib itself is stubbed by test_cancel)
        self.assertEqual(A.local_gpu_name(), "RTX 3090")

    def test_rocm(self):
        self.tool("rocm-smi", json.dumps({"card0": {"Card series": "Radeon RX 7900 XTX", "VRAM Total Memory (B)": str(24 << 30),
                                                   "VRAM Total Used Memory (B)": str(4 << 30)}}))
        self.assertEqual(gpu.info(), ("Radeon RX 7900 XTX", 20480))

    def test_garbage(self):
        self.tool("nvidia-smi", "No devices were found")
        self.assertEqual(gpu.free_mib(), 0)

    def fake_torch(self, cuda=False, xpu=False, mps=False):
        calls = []
        t = types.SimpleNamespace(
            cuda=types.SimpleNamespace(is_available=lambda: cuda, empty_cache=lambda: calls.append("cuda")),
            xpu=types.SimpleNamespace(is_available=lambda: xpu, empty_cache=lambda: calls.append("xpu")),
            mps=types.SimpleNamespace(empty_cache=lambda: calls.append("mps")),
            backends=types.SimpleNamespace(mps=types.SimpleNamespace(is_available=lambda: mps)))
        return t, calls

    def with_torch(self, t, fn):
        old = sys.modules.get("torch")
        sys.modules["torch"] = t
        try:
            return fn()
        finally:
            if old is None:
                sys.modules.pop("torch", None)
            else:
                sys.modules["torch"] = old

    def test_device(self):
        t, calls = self.fake_torch(cuda=True)
        self.assertEqual(self.with_torch(t, gpu.device), "cuda")  # NVIDIA (and ROCm builds): unchanged
        self.with_torch(t, lambda: gpu.empty_cache("cuda"))
        self.assertEqual(calls, ["cuda"])
        t, _ = self.fake_torch(xpu=True)
        self.assertEqual(self.with_torch(t, gpu.device), "xpu")
        t, _ = self.fake_torch(mps=True)
        self.assertEqual(self.with_torch(t, gpu.device), "mps")
        t, calls = self.fake_torch()
        self.assertEqual(self.with_torch(t, gpu.device), "cpu")
        with self.assertRaises(RuntimeError):
            self.with_torch(t, lambda: gpu.device(cpu_ok=False))  # the pipeline won't silently run on the CPU
        self.with_torch(t, lambda: gpu.empty_cache("cpu"))
        self.assertEqual(calls, [])
        os.environ[gpu.ENV] = "cuda:1"
        self.assertEqual(gpu.device(cpu_ok=False), "cuda:1")

    def test_no_hard_coded_cuda(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for f in ("pipeline.py", "app.py", "helper.py", "pitch.py", "readings.py", "lyricscheck.py", "singers.py"):
            src = open(os.path.join(here, f)).read()
            self.assertNotIn("nvidia-smi", src, f)
            self.assertNotIn('"cuda"' if f != "pitch.py" else '.to("cuda")', src, f)
            self.assertNotIn("torch.cuda.", src, f)
        self.assertIn("gpu.py", C.CODE_FILES)  # the helper must run the same device code


if __name__ == "__main__":
    unittest.main()
