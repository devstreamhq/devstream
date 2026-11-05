"""Like counting, ranking and burst detection over Discussion 👍 reactions."""

from datetime import datetime
from typing import Callable

from scripts.devstream.gh import GitHubError

BURST_MIN = 10
YOUNG_DAYS = 90
REACTOR_BATCH = 20
META_BATCH = 50

_REACTIONS = (
    "reactions(content: THUMBS_UP, first: 100{after}) "
    "{{ nodes {{ createdAt user {{ login createdAt }} }} pageInfo {{ hasNextPage endCursor }} }}"
)
_META = "nameWithOwner stargazerCount primaryLanguage { name color } pushedAt owner { avatarUrl }"


def count_likes(reactors: list[str], trusted: Callable[[str], bool]) -> int:
    return sum(1 for r in reactors if trusted(r))


def detect_burst(today: list[tuple[str, datetime]], ages: dict[str, int]) -> bool:
    # A missing age counts as young: an attacker gains nothing by hiding it.
    young = sum(1 for login, _ in today if ages.get(login, 0) < YOUNG_DAYS)
    return len(today) >= BURST_MIN and young * 2 > len(today)


def rank_entries(likes: dict[str, int], stars: dict[str, int]) -> dict[str, int]:
    order = sorted(likes, key=lambda r: (-likes[r], -stars.get(r, 0), r))
    return {r: i + 1 for i, r in enumerate(order)}


def _reactor_query(n: int, paged: bool) -> str:
    params = ", ".join(f"$n{i}: Int!" for i in range(n)) + (", $cursor: String" if paged else "")
    reactions = _REACTIONS.format(after=", after: $cursor" if paged else "")
    body = " ".join(f"d{i}: discussion(number: $n{i}) {{ {reactions} }}" for i in range(n))
    return (
        f"query DiscussionReactors({params}) {{ "
        f'repository(owner: "devstreamhq", name: "devstream") {{ {body} }} }}'
    )


def fetch_reactors(gh, numbers: list[int]) -> dict[int, list[dict]]:
    """Every 👍 node per Discussion number. A deleted Discussion has none."""
    out = {}
    for i in range(0, len(numbers), REACTOR_BATCH):
        chunk = numbers[i:i + REACTOR_BATCH]
        q = _reactor_query(len(chunk), False)
        repo = gh.graphql(q, {f"n{j}": n for j, n in enumerate(chunk)}, allow_not_found=True)["repository"]
        for j, n in enumerate(chunk):
            d = repo[f"d{j}"]
            if d is None:
                out[n] = []
                continue
            page, nodes, seen = d["reactions"], [], set()
            nodes += page["nodes"]
            while page["pageInfo"]["hasNextPage"]:
                cursor = page["pageInfo"]["endCursor"]
                if not cursor or cursor in seen:
                    raise GitHubError(200, f"reactions cursor repeated on discussion {n}")
                seen.add(cursor)
                d = gh.graphql(_reactor_query(1, True), {"n0": n, "cursor": cursor})["repository"]["d0"]
                page = d["reactions"]
                nodes += page["nodes"]
            out[n] = nodes
    return out


def fetch_meta(gh, repos: list[str]) -> dict[str, dict]:
    """Repo card data keyed as given. A deleted repo is left out; rot handles it."""
    out = {}
    for i in range(0, len(repos), META_BATCH):
        chunk = repos[i:i + META_BATCH]
        params = ", ".join(f"$o{j}: String!, $n{j}: String!" for j in range(len(chunk)))
        body = " ".join(f"r{j}: repository(owner: $o{j}, name: $n{j}) {{ {_META} }}" for j in range(len(chunk)))
        variables = {}
        for j, r in enumerate(chunk):
            variables[f"o{j}"], variables[f"n{j}"] = r.split("/", 1)
        data = gh.graphql(f"query RepoMeta({params}) {{ {body} }}", variables, allow_not_found=True)
        for j, r in enumerate(chunk):
            m = data.get(f"r{j}")
            if m is None:
                continue
            lang = m.get("primaryLanguage") or {}
            out[r] = {
                "stars": m["stargazerCount"],
                "language": lang.get("name"),
                "language_color": lang.get("color"),
                "pushed_at": m["pushedAt"],
                "avatar_url": m["owner"]["avatarUrl"],
            }
    return out
