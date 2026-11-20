"""A tiny in-process HTTP client for the studio's ASGI app (the image has no httpx): the request goes through every
middleware and exception handler, as from uvicorn. call() -> (status, headers (lower-case names), body bytes)."""
import asyncio, json, urllib.parse


def call(app, method, url, body=None, headers=None, client=("192.0.2.10", 40000), server=("192.0.2.1", 5570)):
    path, _, qs = url.partition("?")
    raw = b"" if body is None else body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    h = [(k.lower().encode(), str(v).encode()) for k, v in (headers or {}).items()]
    names = {k for k, _ in h}
    if raw and b"content-length" not in names:
        h.append((b"content-length", str(len(raw)).encode()))
    if raw and b"content-type" not in names:
        h.append((b"content-type", b"application/json"))
    h.append((b"host", b"studio.test"))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": urllib.parse.unquote(path), "raw_path": path.encode(), "query_string": qs.encode(), "root_path": "",
             "headers": h, "client": client, "server": server, "state": {}}
    out = {"status": None, "headers": {}, "body": b""}
    sent = [False]

    async def receive():
        if not sent[0]:
            sent[0] = True
            return {"type": "http.request", "body": bytes(raw), "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(m):
        if m["type"] == "http.response.start":
            out["status"] = m["status"]
            out["headers"] = {k.decode().lower(): v.decode() for k, v in m.get("headers", [])}
        elif m["type"] == "http.response.body":
            out["body"] += m.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return out["status"], out["headers"], out["body"]


def jcall(app, method, url, body=None, headers=None, **kw):
    """call(), with the body parsed as JSON (None when empty or not JSON)."""
    st, h, b = call(app, method, url, body, headers, **kw)
    try:
        return st, h, json.loads(b) if b else None
    except ValueError:
        return st, h, None
