"""The Studio app (PWA): the manifest (icons inlined), the service worker served for the whole site, push keys and
subscriptions (people only), and a notification for each new guest request. Sending is stubbed: no network."""
import json, os, subprocess, unittest

from tests import _env  # noqa: F401  (must come first: throwaway folders)
from tests import _asgi
import app as A  # noqa: E402


class PwaTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.real = A._webpush

        def fake(*args, data=None):
            if args[0] == "keys":
                return self.real("keys")          # (the real Node script: a real key pair)
            self.sent.append(json.loads(data))
            return {"status": 201}
        A._webpush = fake
        self.addCleanup(setattr, A, "_webpush", self.real)
        if os.path.exists(A.PUSH_FILE):
            os.remove(A.PUSH_FILE)

    def test_manifest_and_worker(self):
        st, h, b = _asgi.call(A.app, "GET", "/manifest.webmanifest")
        m = json.loads(b)
        self.assertEqual((st, m["display"], m["start_url"]), (200, "standalone", "/"))
        self.assertTrue(all(i["src"].startswith("data:image/png;base64,") for i in m["icons"]))
        self.assertIn("maskable", [i["purpose"] for i in m["icons"]])
        st, h, b = _asgi.call(A.app, "GET", "/sw.js")
        self.assertEqual((st, h.get("service-worker-allowed")), (200, "/"))
        self.assertIn(b"showNotification", b)

    def test_subscribe_and_notify_on_a_request(self):
        st, _, key = _asgi.jcall(A.app, "GET", "/api/push")
        self.assertEqual(st, 200)
        self.assertEqual(len(key["key"]), 87)      # a P-256 public key, uncompressed, base64url
        self.assertEqual(oct(os.stat(A.PUSH_FILE).st_mode & 0o777), "0o600")
        bad = _asgi.jcall(A.app, "POST", "/api/push/subscribe", {"sub": {"endpoint": "http://x"}})
        self.assertEqual(bad[0], 400)
        sub = {"endpoint": "https://push.example.com/s1", "keys": {"p256dh": "BKey", "auth": "auth"}}
        self.assertEqual(_asgi.jcall(A.app, "POST", "/api/push/subscribe", {"sub": sub, "label": "Android tablet (app)"})[0], 200)
        A.apply_request("player-x", {"rid": "r-pwa-1", "artist": "Toto", "title": "Africa", "by": "Mia"})
        for _ in range(50):
            if self.sent:
                break
            import time; time.sleep(0.05)
        self.assertEqual(len(self.sent), 1)
        p = self.sent[0]["payload"]
        self.assertIn("Toto – Africa", p["title"])
        self.assertIn("Mia", p["body"])
        self.assertEqual(p["url"], "/#requests")
        self.assertEqual(_asgi.jcall(A.app, "POST", "/api/push/unsubscribe", {"endpoint": sub["endpoint"]})[2]["devices"], 0)


if __name__ == "__main__":
    unittest.main()
