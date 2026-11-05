import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.devstream.gh import GitHub, GitHubError, RateLimited
from scripts.tests.fake_gh import FakeGitHub, Redirect

LONG = int(time.time()) + 100000


class H(BaseHTTPRequestHandler):
    seen: list = []
    hits = {"short": 0}

    def log_message(self, *a):
        pass

    def _send(self, status, body=None, headers=()):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = self.path
        H.seen.append((self.command, p, dict(self.headers)))
        port = self.server.server_address[1]
        if p == "/missing":
            self._send(404, {"message": "Not Found"})
        elif p == "/repos/old/name":
            self._send(301, None, [("Location", f"http://127.0.0.1:{port}/repositories/123")])
        elif p == "/repositories/123":
            self._send(200, {"full_name": "new/name"})
        elif p == "/limited":
            self._send(403, {}, [("x-ratelimit-remaining", "0"), ("x-ratelimit-reset", str(LONG))])
        elif p == "/short":
            H.hits["short"] += 1
            if H.hits["short"] == 1:
                self._send(429, {}, [("retry-after", "2")])
            else:
                self._send(200, {"ok": True})
        elif p == "/forbidden":
            self._send(403, {"message": "nope"})
        elif p == "/p1":
            nxt = f"<http://127.0.0.1:{port}/p2>; rel=\"next\""
            self._send(200, [{"n": 1}, {"n": 2}], [("Link", nxt)])
        elif p == "/p2":
            self._send(200, [{"n": 3}])
        elif p.startswith("/search"):
            self._send(200, {"items": [{"n": 9}]})
        else:
            self._send(500, {"message": "boom"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        if body["variables"].get("bad"):
            self._send(200, {"errors": [{"message": "x"}]})
        else:
            self._send(200, {"data": {"viewer": 1}})


class GitHubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.slept = []
        self.gh = GitHub("tok", self.base, sleep=self.slept.append)
        H.seen.clear()
        H.hits["short"] = 0

    def test_404_returns_none(self):
        self.assertIsNone(self.gh.get("/missing"))

    def test_redirect_followed_and_recorded(self):
        self.assertEqual(self.gh.get("/repos/old/name"), {"full_name": "new/name"})
        self.assertEqual(self.gh.last_redirect, "/repositories/123")

    def test_rate_limit_long_reset_raises(self):
        with self.assertRaises(RateLimited) as c:
            self.gh.get("/limited")
        self.assertEqual(c.exception.reset_at, LONG)
        self.assertEqual(self.slept, [])

    def test_rate_limit_short_reset_sleeps_and_retries(self):
        self.assertEqual(self.gh.get("/short"), {"ok": True})
        self.assertEqual(len(self.slept), 1)

    def test_plain_403_is_error(self):
        with self.assertRaises(GitHubError) as c:
            self.gh.get("/forbidden")
        self.assertEqual(c.exception.status, 403)

    def test_500_is_error(self):
        with self.assertRaises(GitHubError):
            self.gh.get("/other")

    def test_paginate_follows_link_header(self):
        self.assertEqual([i["n"] for i in self.gh.paginate("/p1")], [1, 2, 3])

    def test_paginate_search_items(self):
        self.assertEqual(list(self.gh.paginate("/search")), [{"n": 9}])

    def test_headers(self):
        self.gh.get("/p2")
        h = {k.lower(): v for k, v in H.seen[0][2].items()}
        self.assertEqual(h["authorization"], "Bearer tok")
        self.assertEqual(h["x-github-api-version"], "2022-11-28")
        GitHub("", self.base).get("/p2")
        self.assertNotIn("authorization", {k.lower() for k in H.seen[1][2]})

    def test_graphql(self):
        self.assertEqual(self.gh.graphql("query Q { viewer }", {}), {"viewer": 1})
        with self.assertRaises(GitHubError):
            self.gh.graphql("query Q { viewer }", {"bad": 1})


class FakeTest(unittest.TestCase):
    def test_same_public_methods(self):
        pub = lambda c: {n for n in dir(c) if not n.startswith("_") and callable(getattr(c, n))}
        self.assertEqual(pub(GitHub), pub(FakeGitHub))

    def test_routes(self):
        seen = []
        f = FakeGitHub({
            "GET /repos/a/b": {"x": 1},
            "GET /repos/c/d": Redirect("/repos/a/b"),
            "POST /p": lambda b: seen.append(b) or {"ok": 1},
            "GRAPHQL Likes": lambda v: {"v": v},
        })
        self.assertEqual(f.get("/repos/a/b", {"q": 1}), {"x": 1})
        self.assertIsNone(f.get("/nope"))
        self.assertEqual(f.get("/repos/c/d"), {"x": 1})
        self.assertEqual(f.last_redirect, "/repos/a/b")
        f.post("/p", {"a": 1})
        self.assertEqual(seen, [{"a": 1}])
        self.assertEqual(f.graphql("query Likes($a: Int) { x }", {"a": 1}), {"v": {"a": 1}})
        with self.assertRaises(GitHubError):
            f.graphql("query Other { x }", {})
        self.assertEqual(f.calls[0], ("GET", "/repos/a/b", None))


if __name__ == "__main__":
    unittest.main()
