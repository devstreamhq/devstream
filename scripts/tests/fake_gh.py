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

    .calls records (method, path, body) for every request.
    """

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[tuple[str, str, object]] = []
        self.last_redirect: str | None = None

    def _find(self, method, path, params, body):
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
                    return self._find(method, v.path, None, body)
                return v(body) if callable(v) else v
        return None

    def get(self, path, params=None):
        return self._find("GET", path, params, None)

    def paginate(self, path, params=None):
        page = self.get(path, params)
        if isinstance(page, dict) and "items" in page:
            page = page["items"]
        yield from page or []

    def post(self, path, body):
        return self._find("POST", path, None, body)

    def patch(self, path, body):
        return self._find("PATCH", path, None, body)

    def put(self, path, body):
        return self._find("PUT", path, None, body)

    def graphql(self, query: str, variables: dict) -> dict:
        m = re.search(r"\b(?:query|mutation)\s+(\w+)", query)
        self.calls.append(("GRAPHQL", m.group(1) if m else "", variables))
        for key in ([f"GRAPHQL {m.group(1)}"] if m else []) + ["GRAPHQL"]:
            if key in self.routes:
                v = self.routes[key]
                return v(variables) if callable(v) else v
        raise GitHubError(404, "no graphql route")
