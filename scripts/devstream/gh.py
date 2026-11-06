import json
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

MAX_SLEEP = 600
MAX_HOPS = 5
TIMEOUT = 30
ATTEMPTS = 3
TRANSIENT = (502, 503, 504)
NETWORK = (urllib.error.URLError, TimeoutError, ConnectionError)
# Only a document that opens as a query is a read; anything else (a fragment first, say) may be a mutation.
LEADING_COMMENTS = re.compile(r"(\s*#[^\n]*(\n|$))*")
READ_QUERY = re.compile(r"\s*(query\b|\{)")


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


def _allowed(e: dict, not_found: bool, forbidden: bool) -> bool:
    # A FORBIDDEN with no path is the whole query refused, never one hidden repo.
    return (not_found and e.get("type") == "NOT_FOUND") or (forbidden and e.get("type") == "FORBIDDEN" and bool(e.get("path")))


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

    @staticmethod
    def _retryable(method, read, status=None, exc=None) -> bool:
        """read: a GET or a GraphQL query. PUT, PATCH and DELETE give the same result twice (contents PUTs are sha-guarded)."""
        if exc is None:
            return (method != "POST" or read) and (status in TRANSIENT or (read and status == 500))
        if read:
            return True
        # Anything else may have reached GitHub; a POST or mutation sent twice duplicates an issue, thread or comment.
        return isinstance(getattr(exc, "reason", None), (ConnectionRefusedError, socket.gaierror))

    def _backoff(self, fails):
        self.sleep(2 ** (fails - 1) * (1 + random.random() / 2))

    def _request(self, method, path, params=None, body=None, read=None):
        """Return (headers, parsed json) or None on 404."""
        read = method == "GET" if read is None else read
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
        retried = False
        hops = 0
        fails = 0
        while True:
            try:
                status, headers, raw = self._send(method, self.base + path, body)
            except NETWORK as e:
                fails += 1
                if fails >= ATTEMPTS or not self._retryable(method, read, exc=e):
                    raise
                self._backoff(fails)
                continue
            if self._retryable(method, read, status) and fails + 1 < ATTEMPTS:
                fails += 1
                self._backoff(fails)
                continue
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

    def paginate(self, path, params=None, must_exist=False) -> Iterator[dict]:
        """must_exist: a 404 on the first page raises instead of reading as an empty list."""
        self.last_redirect = None
        first = True
        while path:
            r = self._request("GET", path, params)
            if r is None:
                if first and not must_exist:
                    return
                # A vanished page would silently truncate the results.
                raise GitHubError(404, f"page not found: {path}")
            first = False
            headers, page = r
            items = page["items"] if isinstance(page, dict) and "items" in page else page
            if not isinstance(items, list):
                # A dict here would yield its keys as if they were items.
                raise GitHubError(200, f"not a list page: {path}")
            yield from items
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

    def delete(self, path, body=None):
        return self._write("DELETE", path, body)

    def graphql(self, query: str, variables: dict, allow_not_found: bool = False,
                allow_forbidden: bool = False) -> dict:
        """allow_not_found returns data with nulls where only NOT_FOUND errors occurred.
        allow_forbidden also lets through FORBIDDEN errors on one alias (an org IP allow list)."""
        self.last_redirect = None
        r = self._request("POST", "/graphql", body={"query": query, "variables": variables},
                          read=bool(READ_QUERY.match(query, LEADING_COMMENTS.match(query).end())))
        if r is None:
            raise GitHubError(404, "graphql endpoint not found")
        out = r[1] or {}
        errors = out.get("errors")
        if any(e.get("type") == "RATE_LIMITED" for e in errors or []):
            raise RateLimited(int(r[0].get("x-ratelimit-reset") or time.time() + 60))
        if errors and not all(_allowed(e, allow_not_found, allow_forbidden) for e in errors):
            raise GitHubError(200, json.dumps(out["errors"])[:500])
        return out.get("data")
