import re
import urllib.parse

from scripts.devstream.gh import GitHubError


class Redirect:
    def __init__(self, path: str):
        self.path = path


class FakeGitHub:
    """In-memory stand-in for GitHub, same public methods.

    routes maps "METHOD /path" to an object, or a callable (body) -> object.
    The query string is ignored unless the key includes it ("GET /x?a=1"
    is tried first). A missing key is a 404 (None); Redirect(path) answers
    with the routed value of GET path and sets .last_redirect.

    GraphQL routes: "GRAPHQL <OperationName>", where the name is the word
    after `query` or `mutation` in the document, or plain "GRAPHQL" as a
    catch-all. A missing route raises GitHubError(404).

    Only .get returns None on a missing route; post, patch and put raise
    GitHubError(404). A route whose value is None answers a write like a
    204. paginate yields nothing for a missing route (the real client does
    the same for a 404 on the first page; the fake has a single page).

    .last_redirect is reset at the start of every call.

    .calls records (method, path, body) for every request.
    """

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[tuple[str, str, object]] = []
        self.last_redirect: str | None = None

    def _find(self, method, path, params, body, write=False):
        full = path
        if params:
            full += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
        self.calls.append((method, path, body))
        for p in (full, path.split("?")[0]):
            key = f"{method} {p}"
            if key in self.routes:
                v = self.routes[key]
                if isinstance(v, Redirect):
                    self.last_redirect = v.path
                    return self._find(method, v.path, None, body, write)
                return v(body) if callable(v) else v
        if write:
            raise GitHubError(404, f"no route: {method} {path}")
        return None

    def get(self, path, params=None):
        self.last_redirect = None
        return self._find("GET", path, params, None)

    def paginate(self, path, params=None):
        self.last_redirect = None
        page = self._find("GET", path, params, None)
        if isinstance(page, dict) and "items" in page:
            page = page["items"]
        yield from page or []

    def _write(self, method, path, body):
        self.last_redirect = None
        return self._find(method, path, None, body, write=True)

    def post(self, path, body):
        return self._write("POST", path, body)

    def patch(self, path, body):
        return self._write("PATCH", path, body)

    def put(self, path, body):
        return self._write("PUT", path, body)

    def graphql(self, query: str, variables: dict) -> dict:
        self.last_redirect = None
        m = re.search(r"\b(?:query|mutation)\s+(\w+)", query)
        self.calls.append(("GRAPHQL", m.group(1) if m else "", variables))
        for key in ([f"GRAPHQL {m.group(1)}"] if m else []) + ["GRAPHQL"]:
            if key in self.routes:
                v = self.routes[key]
                return v(variables) if callable(v) else v
        raise GitHubError(404, "no graphql route")
