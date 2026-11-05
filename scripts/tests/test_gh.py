import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.devstream.gh import GitHub, GitHubError, RateLimited
from scripts.tests.fake_gh import FakeGitHub, Redirect, Status

LONG = int(time.time()) + 100000


class Other(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Other.seen.append((self.path, dict(self.headers)))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"[]")


OTHER = ThreadingHTTPServer(("127.0.0.1", 0), Other)
threading.Thread(target=OTHER.serve_forever, daemon=True).start()


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
        elif p == "/loop":
            self._send(301, None, [("Location", f"http://127.0.0.1:{port}/loop")])
        elif p == "/foreign":
            self._send(301, None, [("Location", f"http://127.0.0.1:{OTHER.server_address[1]}/stolen")])
        elif p == "/xlink":
            nxt = f"<http://127.0.0.1:{OTHER.server_address[1]}/p2>; rel=\"next\""
            self._send(200, [{"n": 1}], [("Link", nxt)])
        elif p == "/twice":
            self._send(429, {}, [("retry-after", "1")])
        elif p == "/q1":
            self._send(200, [{"n": 1}], [("Link", f"<http://127.0.0.1:{port}/missing>; rel=\"next\"")])
        elif p == "/stolen":
            self._send(200, {"own": True})
        elif p == "/nocontent":
            self._send(204)
        elif p.startswith("/search"):
            self._send(200, {"items": [{"n": 9}]})
        else:
            self._send(500, {"message": "boom"})

    def _write(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n))
        H.seen.append((self.command, self.path, dict(self.headers)))
        if self.path == "/graphql":
            return body
        if self.path == "/moved":
            self._send(301, None, [("Location", "/elsewhere")])
        elif self.path == "/gone":
            self._send(404, {"message": "Not Found"})
        elif self.path == "/empty":
            self._send(204)
        else:
            self._send(200, {"echo": body, "method": self.command})

    def do_PATCH(self):
        self._write()

    def do_PUT(self):
        self._write()

    def do_POST(self):
        body = self._write()
        if body is None:
            return
        if body["variables"].get("rl"):
            hdr = [("x-ratelimit-reset", str(LONG))] if body["variables"]["rl"] == 1 else []
            self._send(200, {"errors": [{"type": "RATE_LIMITED", "message": "x"}]}, hdr)
        elif body["variables"].get("bad"):
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
        Other.seen.clear()
        H.hits["short"] = 0

    def test_404_returns_none(self):
        self.assertIsNone(self.gh.get("/missing"))

    def test_status_204_404_and_errors(self):
        self.assertEqual(self.gh.status("/nocontent"), 204)
        self.assertEqual(self.gh.status("/missing"), 404)
        self.assertEqual(self.gh.status("/short"), 200)
        with self.assertRaises(GitHubError):
            self.gh.status("/boom")
        with self.assertRaises(RateLimited):
            self.gh.status("/limited")

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

    def test_write_success(self):
        for m in ("post", "patch", "put"):
            r = getattr(self.gh, m)("/ok", {"a": 1})
            self.assertEqual(r, {"echo": {"a": 1}, "method": m.upper()})

    def test_write_204_is_none(self):
        self.assertIsNone(self.gh.post("/empty", {}))

    def test_write_404_raises(self):
        with self.assertRaises(GitHubError) as c:
            self.gh.post("/gone", {})
        self.assertEqual(c.exception.status, 404)

    def test_write_redirect_raises(self):
        with self.assertRaises(GitHubError) as c:
            self.gh.post("/moved", {})
        self.assertEqual(c.exception.status, 301)

    def test_last_redirect_reset(self):
        self.gh.get("/repos/old/name")
        self.assertIsNotNone(self.gh.last_redirect)
        self.gh.get("/p2")
        self.assertIsNone(self.gh.last_redirect)

    def test_paginate_later_404_raises(self):
        with self.assertRaises(GitHubError):
            list(self.gh.paginate("/q1"))

    def test_paginate_first_404_is_empty(self):
        self.assertEqual(list(self.gh.paginate("/missing")), [])

    def test_graphql_rate_limited(self):
        with self.assertRaises(RateLimited) as c:
            self.gh.graphql("query Q { viewer }", {"rl": 1})
        self.assertEqual(c.exception.reset_at, LONG)
        with self.assertRaises(RateLimited) as c:
            self.gh.graphql("query Q { viewer }", {"rl": 2})
        self.assertAlmostEqual(c.exception.reset_at, time.time() + 60, delta=5)

    def test_cross_host_redirect_not_followed(self):
        self.assertEqual(self.gh.get("/foreign"), {"own": True})
        self.assertEqual(Other.seen, [])
        self.assertEqual(self.gh.last_redirect, "/stolen")

    def test_cross_host_link_not_followed(self):
        self.assertEqual([i["n"] for i in self.gh.paginate("/xlink")], [1, 3])
        self.assertEqual(Other.seen, [])

    def test_too_many_redirects(self):
        with self.assertRaises(GitHubError):
            self.gh.get("/loop")

    def test_second_rate_limit_raises(self):
        with self.assertRaises(RateLimited):
            self.gh.get("/twice")
        self.assertEqual(len(self.slept), 1)


class FakeTest(unittest.TestCase):
    def test_same_public_methods(self):
        pub = lambda c: {n for n in dir(c) if not n.startswith("_") and callable(getattr(c, n))}
        self.assertEqual(pub(GitHub), pub(FakeGitHub))

    def test_status_routes(self):
        f = FakeGitHub({"GET /b": Status(204), "GET /x": {"a": 1}, "GET /e": Status(500)})
        self.assertEqual(f.status("/b"), 204)
        self.assertEqual(f.status("/x"), 200)
        self.assertEqual(f.status("/none"), 404)
        with self.assertRaises(GitHubError):
            f.status("/e")

    def test_write_missing_route_raises(self):
        f = FakeGitHub({"PUT /empty": None})
        for m in ("post", "patch", "put"):
            with self.assertRaises(GitHubError) as c:
                getattr(f, m)("/nope", {})
            self.assertEqual(c.exception.status, 404)
        self.assertIsNone(f.put("/empty", {}))

    def test_last_redirect_reset(self):
        f = FakeGitHub({"GET /a": Redirect("/b"), "GET /b": {}, "GET /c": {}})
        f.get("/a")
        self.assertEqual(f.last_redirect, "/b")
        f.get("/c")
        self.assertIsNone(f.last_redirect)

    def test_paginate_missing_first_page_empty(self):
        self.assertEqual(list(FakeGitHub({}).paginate("/x")), [])

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
