"""Sync bundles (contract/ENCORE-STUDIO.md §2.7): the player API carried on a USB stick, for a player with no network.

- Export (Studio -> player): plan() works out what goes in (every file of every catalog song, carried unless the player
  already has it, by its last bundle's `have`; whole songs per part, parts at most `part_bytes`), hashing the library
  files first (sha256, cached by path + size + mtime: only new or changed files are read). stream() writes one part as
  a POSIX ustar tar whose exact length is known before the first byte (size()), so the web app can send
  Content-Length: a download cut short fails in the browser instead of looking complete. A file that changes while it
  streams aborts the download (never a mix of two versions).
- Import (player -> Studio): read_player() checks the tar (manifest.json and manifest.sig first, the signature with that
  player's key, only the listed members, caps) and gives back the calls; the web app applies them with the player
  API's own handlers.
- The key: K = HMAC-SHA256(sha256(token) as bytes, "karaoke-sync-bundle-v1"); the store keeps sha256(token) only.
  K, the token and its hash are never logged or shown.
"""
import hashlib, hmac, io, json, os, re, sqlite3, tarfile, threading, time

LABEL = b"karaoke-sync-bundle-v1"
BUNDLE, CONTRACT = 1, "1"
BLOCK = 512
CAP = {"manifest.json": 4 << 20, "manifest.sig": 64, "catalog.json": 16 << 20, "answers.json": 8 << 20,
       "calls.json": 8 << 20, "file": 4 << 30}
UPLOAD_MAX = 64 << 20               # a player's bundle (calls only): well under this
HEX64 = re.compile(r"^[0-9a-f]{64}$")
KEEP_DAYS = 90


class BundleError(Exception):
    """A bundle that can't be used (the message is for the admin)."""


def key(token_hash_hex):
    return hmac.new(bytes.fromhex(token_hash_hex), LABEL, hashlib.sha256).digest()


def sign(k, data):
    return hmac.new(k, data, hashlib.sha256).hexdigest()


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                return h.hexdigest()
            h.update(b)


def pad(n):
    return (BLOCK - n % BLOCK) % BLOCK


def header(name, size, mtime):
    """One ustar header (no pax or GNU extension: names are short and ASCII by design)."""
    ti = tarfile.TarInfo(name)
    ti.size, ti.mtime, ti.mode, ti.type = size, int(mtime), 0o644, tarfile.REGTYPE
    ti.uname = ti.gname = ""
    return ti.tobuf(format=tarfile.USTAR_FORMAT, encoding="ascii", errors="strict")


def member_len(size):
    return BLOCK + size + pad(size)


class Hashes:
    """sha256 of library files, kept by path + size + mtime (a SQLite table next to the player store)."""

    def __init__(self, db_path):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("create table if not exists file_hashes (path text primary key, size integer, mtime_ns integer, sha256 text)")
            self.db.commit()

    def get(self, full, rel):
        st = os.stat(full)
        with self.lock:
            r = self.db.execute("select size, mtime_ns, sha256 from file_hashes where path=?", (rel,)).fetchone()
        if r and r[0] == st.st_size and r[1] == st.st_mtime_ns:
            return r[2]
        h = sha256_file(full)
        if os.stat(full).st_mtime_ns != st.st_mtime_ns:
            raise BundleError(f"{rel} changed while it was being read; try again")
        with self.lock:
            self.db.execute("insert or replace into file_hashes values (?, ?, ?, ?)", (rel, st.st_size, st.st_mtime_ns, h))
            self.db.commit()
        return h


class Plan:
    """What one export carries, part by part (made by plan(); stream() writes a part)."""

    def __init__(self, player, studio_id, studio_name, lib, cat_bytes, answers_bytes, files, parts, made):
        self.player, self.studio_id, self.studio_name, self.lib = player, studio_id, studio_name, lib
        self.cat_bytes, self.answers_bytes, self.files, self.parts, self.made = cat_bytes, answers_bytes, files, parts, made
        self.generated = json.loads(cat_bytes).get("generated")
        self._built = {}

    def _manifest(self, i):
        """(manifest bytes, the members after it [(name, size, full path or bytes, stat key)])."""
        carried = self.parts[i]["blobs"]   # sha256 -> (path, size)
        doc = {"bundle": BUNDLE, "kind": "studio", "contract": CONTRACT, "studio_id": self.studio_id,
               "studio_name": self.studio_name[:80], "player": self.player, "made": self.made, "part": [i + 1, len(self.parts)],
               "catalog": {"member": "catalog.json", "size": len(self.cat_bytes), "sha256": hashlib.sha256(self.cat_bytes).hexdigest(),
                           "generated": self.generated},
               "answers": {"member": "answers.json", "size": len(self.answers_bytes),
                           "sha256": hashlib.sha256(self.answers_bytes).hexdigest()},
               "files": [{"path": f["path"], "size": f["size"], "sha256": f["sha256"], "etag": f["etag"],
                          "member": "f/" + f["sha256"] if f["sha256"] in carried else None} for f in self.files]}
        members = [("catalog.json", len(self.cat_bytes), self.cat_bytes, None),
                   ("answers.json", len(self.answers_bytes), self.answers_bytes, None)]
        for sha, (path, size, key_) in carried.items():
            members.append(("f/" + sha, size, os.path.join(self.lib, *path.split("/")), key_))
        return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode(), members

    def built(self, i, k):
        if i not in self._built:
            m, members = self._manifest(i)
            self._built[i] = (m, sign(k, m).encode(), members)
        return self._built[i]

    def size(self, i, k):
        m, sig, members = self.built(i, k)
        return member_len(len(m)) + member_len(len(sig)) + sum(member_len(sz) for _, sz, _, _ in members) + 2 * BLOCK

    def stream(self, i, k, chunk=1 << 20):
        """The bytes of part i (0-based), exactly size(i, k) of them; raises if a file changed since it was hashed."""
        m, sig, members = self.built(i, k)
        now = self.made
        yield header("manifest.json", len(m), now) + m + b"\0" * pad(len(m))
        yield header("manifest.sig", len(sig), now) + sig + b"\0" * pad(len(sig))
        for name, size, src, key_ in members:
            yield header(name, size, now)
            if isinstance(src, (bytes, bytearray)):
                yield bytes(src)
            else:
                st = os.stat(src)
                if (st.st_size, st.st_mtime_ns) != key_:
                    raise BundleError(f"{name} changed since the export was prepared: prepare it again")
                left = size
                with open(src, "rb") as f:
                    while left:
                        b = f.read(min(chunk, left))
                        if not b:
                            raise BundleError("a library file got shorter while it was being sent")
                        left -= len(b)
                        yield b
            yield b"\0" * pad(size)
        yield b"\0" * (2 * BLOCK)

    def view(self):
        return {"made": self.made, "parts": [{"part": i + 1, "songs": len(p["songs"]), "bytes": p["bytes"]} for i, p in enumerate(self.parts)],
                "songs": sum(len(p["songs"]) for p in self.parts), "files": len(self.files)}


def plan(player, studio_id, studio_name, lib, cat_bytes, answers, files_of, etag_of, hashes, have=None, everything=False,
         part_bytes=None, progress=None, now=None):
    """Work out an export: every file of every song in the catalog (files_of(song) -> library paths), each carried
    unless the player has it (have: path -> the ETag it has) or `everything`. Whole songs per part, newest first."""
    cat = json.loads(cat_bytes)
    songs = sorted([s for s in cat.get("songs") or [] if isinstance(s, dict)], key=lambda s: -(s.get("published") or 0))
    have = have or {}
    paths = []
    for s in songs:
        paths.append((s, sorted(files_of(s))))
    total = sum(len(p) for _, p in paths)
    done, files, seen = 0, [], {}
    groups = []          # per song: {sha256: (path, size, (size, mtime_ns))} it needs carried
    for s, ps in paths:
        need = {}
        for rel in ps:
            full = os.path.join(lib, *rel.split("/"))
            if not os.path.isfile(full):
                continue   # the Studio is missing it: listed nowhere, the player can't complete that song (§2.7)
            st = os.stat(full)
            sha = hashes.get(full, rel)
            et = etag_of(rel, st)
            if rel not in seen:
                seen[rel] = True
                files.append({"path": rel, "size": st.st_size, "sha256": sha, "etag": et})
            if everything or have.get(rel) != et:
                need.setdefault(sha, (rel, st.st_size, (st.st_size, st.st_mtime_ns)))
            done += 1
            if progress:
                progress(done, total)
        groups.append((s.get("id"), need))
    parts, cur = [], {"songs": [], "blobs": {}, "bytes": 0}
    for sid, need in groups:
        if not need:
            continue
        add = {k: v for k, v in need.items() if k not in cur["blobs"]}
        size = sum(member_len(v[1]) for v in add.values())
        if part_bytes and cur["songs"] and cur["bytes"] + size > part_bytes:
            parts.append(cur)
            cur = {"songs": [], "blobs": {}, "bytes": 0}
            add, size = need, sum(member_len(v[1]) for v in need.values())
        cur["songs"].append(sid)
        cur["blobs"].update(add)
        cur["bytes"] += size
    if cur["songs"] or not parts:
        parts.append(cur)
    answers_bytes = json.dumps(answers, ensure_ascii=False, separators=(",", ":")).encode()
    return Plan(player, studio_id, studio_name, lib, cat_bytes, answers_bytes, files, parts, round(now or time.time(), 1))


# ---------------- reading a player's bundle ----------------
def _read_exact(tf, ti, cap):
    if not ti.isreg():
        raise BundleError(f"{ti.name!r} isn't a plain file")
    if ti.size > cap:
        raise BundleError(f"{ti.name!r} is too big")
    data = tf.extractfile(ti).read()
    if len(data) != ti.size:
        raise BundleError("the file is cut short")
    return data


def read_player(data, key_for, studio_id, schema_errors):
    """A player's bundle (the uploaded bytes) -> (player, manifest, calls). key_for(player) = that linked player's K, or
    None (not linked). schema_errors(schema name, doc) -> [errors]. Raises BundleError with the reason."""
    if len(data) > UPLOAD_MAX:
        raise BundleError("that's far too big for a player's bundle")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(data), mode="r|")
        first = tf.next()
        if not first or first.name != "manifest.json":
            raise BundleError("this isn't a karaoke sync bundle (no manifest first)")
        mbytes = _read_exact(tf, first, CAP["manifest.json"])
        second = tf.next()
        if not second or second.name != "manifest.sig":
            raise BundleError("the bundle has no signature")
        sig = _read_exact(tf, second, CAP["manifest.sig"]).decode("ascii", "replace").strip()
        try:
            m = json.loads(mbytes)
        except ValueError:
            raise BundleError("the manifest isn't JSON") from None
        player = m.get("player") if isinstance(m, dict) else None
        k = key_for(player) if isinstance(player, str) else None
        if not k:
            raise BundleError("it's from a player that isn't linked to this Studio")
        if not HEX64.match(sig) or not hmac.compare_digest(sig, sign(k, mbytes)):
            raise BundleError("the signature doesn't match: it was made for an old link (export it again from the player) or changed on the way")
        errs = schema_errors("bundle.manifest.schema.json", m)
        if errs:
            raise BundleError("the manifest isn't right: " + "; ".join(errs[:3]))
        if m["kind"] != "player":
            raise BundleError("that's a bundle for a player, not one from a player")
        if m["studio_id"] != studio_id:
            raise BundleError("it was made for another Studio")
        if m["contract"].split(".")[0] != CONTRACT:
            raise BundleError(f"it speaks contract {m['contract']}; this Studio speaks {CONTRACT}")
        ti = tf.next()
        if not ti or ti.name != m["calls"]["member"]:
            raise BundleError("the bundle's calls are missing")
        raw = _read_exact(tf, ti, CAP["calls.json"])
        if len(raw) != m["calls"]["size"] or hashlib.sha256(raw).hexdigest() != m["calls"]["sha256"]:
            raise BundleError("the bundle's calls don't match its manifest")
        if tf.next() is not None:
            raise BundleError("the bundle has more in it than its manifest lists")
    except tarfile.TarError:
        raise BundleError("this isn't a readable tar file") from None
    try:
        calls = json.loads(raw)
    except ValueError:
        raise BundleError("the calls aren't JSON") from None
    errs = schema_errors("bundle.calls.schema.json", calls)
    if errs:
        raise BundleError("the calls aren't right: " + "; ".join(errs[:3]))
    return player, m, calls
