import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

MAX_SLEEP = 600
MAX_HOPS = 5
TIMEOUT = 30


class GitHubError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.message = message


class RateLimited(Exception):
    def __init__(self, reset_at: int):
        super().__init__(f"rate limited until {reset_at}")
        self.reset_at = reset_at


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Redirects are followed by hand so the final path can be recorded.
    def redirect_request(self, *a, **k):
        return None


class GitHub:
    def __init__(self, token: str, base: str = "https://api.github.com", sleep=time.sleep):
        self.token = token
        self.base = base.rstrip("/")
        self.sleep = sleep
        self.last_redirect: str | None = None
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _send(self, method, url, body):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "devstream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=TIMEOUT) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers, e.read()

    @staticmethod
    def _rate_limit_reset(status, headers) -> int | None:
        if status not in (403, 429):
            return None
        retry = headers.get("retry-after")
        if headers.get("x-ratelimit-remaining") != "0" and retry is None:
            return None
        if retry is not None:
            return int(time.time()) + int(retry)
        return int(headers.get("x-ratelimit-reset") or time.time() + 60)

    def _request(self, method, path, params=None, body=None):
        """Return (headers, parsed json) or None on 404."""
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
        retried = False
        hops = 0
        while True:
            status, headers, raw = self._send(method, self.base + path, body)
            reset = self._rate_limit_reset(status, headers)
            if reset is not None:
                if reset - time.time() > MAX_SLEEP or retried:
                    raise RateLimited(reset)
                self.sleep(max(0, reset - time.time()))
                retried = True
                continue
            # 301/302 would turn a write into a GET, so only 307/308 are safe there.
            if status in (307, 308) or (method == "GET" and status in (301, 302)):
                hops += 1
                if hops > MAX_HOPS:
                    raise GitHubError(status, "too many redirects")
                loc = urllib.parse.urlsplit(headers["Location"])
                path = loc.path + (f"?{loc.query}" if loc.query else "")
                self.last_redirect = path
                continue
            if status == 404:
                return None
            if status >= 300:
                raise GitHubError(status, raw.decode(errors="replace")[:500])
            return headers, (json.loads(raw) if raw else None)

    def get(self, path, params=None):
        self.last_redirect = None
        r = self._request("GET", path, params)
        return None if r is None else r[1]

    def status(self, path) -> int:
        """HTTP status of a GET: 204 and 404 are answers here, not failures."""
        self.last_redirect = None
        r = self._request("GET", path)
        if r is None:
            return 404
        return 200 if r[1] is not None else 204

    def paginate(self, path, params=None) -> Iterator[dict]:
        self.last_redirect = None
        first = True
        while path:
            r = self._request("GET", path, params)
            if r is None:
                if first:
                    return
                # A vanished page would silently truncate the results.
                raise GitHubError(404, f"page not found: {path}")
            first = False
            headers, page = r
            yield from page["items"] if isinstance(page, dict) and "items" in page else page
            m = re.search(r'<([^>]+)>\s*;\s*rel="next"', headers.get("Link") or "")
            # Rebuilt against base so the token never leaves the API host.
            nxt = urllib.parse.urlsplit(m.group(1)) if m else None
            path = nxt.path + (f"?{nxt.query}" if nxt.query else "") if nxt else None
            params = None

    def _write(self, method, path, body):
        self.last_redirect = None
        r = self._request(method, path, body=body)
        if r is None:
            raise GitHubError(404, f"not found: {path}")
        return r[1]

    def post(self, path, body):
        return self._write("POST", path, body)

    def patch(self, path, body):
        return self._write("PATCH", path, body)

    def put(self, path, body):
        return self._write("PUT", path, body)

    def graphql(self, query: str, variables: dict) -> dict:
        self.last_redirect = None
        r = self._request("POST", "/graphql", body={"query": query, "variables": variables})
        if r is None:
            raise GitHubError(404, "graphql endpoint not found")
        out = r[1] or {}
        errors = out.get("errors")
        if any(e.get("type") == "RATE_LIMITED" for e in errors or []):
            raise RateLimited(int(r[0].get("x-ratelimit-reset") or time.time() + 60))
        if errors:
            raise GitHubError(200, json.dumps(out["errors"])[:500])
        return out.get("data")
