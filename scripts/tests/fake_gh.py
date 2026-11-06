import re
import urllib.parse

from scripts.devstream.gh import GitHubError, _allowed


class Redirect:
    def __init__(self, path: str):
        self.path = path


class Status:
    """Route value for an answer with a bare status code and no body.

    Status(204) or Status(404) as a GET route: .get returns None and .status
    returns the code. Any other code of 300+ raises GitHubError from both.
    """

    def __init__(self, code: int):
        self.code = code


class GraphQLErrors:
    """GraphQL route value for a response carrying errors next to data.

    Raises GitHubError like the real client, unless allow_not_found is set
    and every error is NOT_FOUND; then the data comes back with its nulls.
    """

    def __init__(self, data: dict, errors: list[dict]):
        self.data = data
        self.errors = errors


class FakeGitHub:
    """In-memory stand-in for GitHub, same public methods.

    routes maps "METHOD /path" to an object, or a callable (body) -> object.
    The query string is ignored unless the key includes it ("GET /x?a=1"
    is tried first). A missing key is a 404 (None); Redirect(path) answers
    with the routed value of GET path and sets .last_redirect.

    GraphQL routes: "GRAPHQL <OperationName>", where the name is the word
    after `query` or `mutation` in the document, or plain "GRAPHQL" as a
    catch-all. A missing route raises GitHubError(404).

    .status(path) returns the code of a Status route, 200 for any other
    routed value, and 404 for a missing route.

    Only .get returns None on a missing route; post, patch, put and delete raise
    GitHubError(404). A route whose value is None answers a write like a
    204. paginate yields nothing for a missing route (the real client does
    the same for a 404 on the first page, unless must_exist; the fake has a single page).

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
        v = self._find("GET", path, params, None)
        return None if isinstance(v, Status) and self._ok(v) else v

    @staticmethod
    def _ok(v):
        if v.code >= 300 and v.code != 404:
            raise GitHubError(v.code, "status route")
        return True

    def status(self, path) -> int:
        self.last_redirect = None
        v = self._find("GET", path, None, None)
        if isinstance(v, Status):
            self._ok(v)
            return v.code
        return 404 if v is None else 200

    def paginate(self, path, params=None, must_exist=False):
        self.last_redirect = None
        page = self._find("GET", path, params, None)
        if isinstance(page, Status) and self._ok(page):
            page = None
        if page is None and must_exist:
            raise GitHubError(404, f"page not found: {path}")
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

    def delete(self, path, body=None):
        return self._write("DELETE", path, body)

    def graphql(self, query: str, variables: dict, allow_not_found: bool = False,
                allow_forbidden: bool = False) -> dict:
        self.last_redirect = None
        m = re.search(r"\b(?:query|mutation)\s+(\w+)", query)
        self.calls.append(("GRAPHQL", m.group(1) if m else "", variables))
        for key in ([f"GRAPHQL {m.group(1)}"] if m else []) + ["GRAPHQL"]:
            if key in self.routes:
                v = self.routes[key]
                v = v(variables) if callable(v) else v
                if isinstance(v, GraphQLErrors):
                    if not all(_allowed(e, allow_not_found, allow_forbidden) for e in v.errors):
                        raise GitHubError(200, str(v.errors))
                    return v.data
                return v
        raise GitHubError(404, "no graphql route")
