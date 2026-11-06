"""Like counting, ranking and burst detection over Discussion 👍 reactions."""

from datetime import datetime
from typing import Callable

from scripts.devstream.gh import GitHubError

BURST_MIN = 10
DRIP_MIN = 30
YOUNG_DAYS = 90
REACTOR_BATCH = 20
META_BATCH = 50

_REACTIONS = (
    "reactions(content: THUMBS_UP, first: 100{after}) "
    "{{ nodes {{ createdAt user {{ login createdAt }} }} pageInfo {{ hasNextPage endCursor }} }}"
)
_META = "nameWithOwner stargazerCount primaryLanguage { name color } pushedAt owner { avatarUrl }"


def count_likes(reactors: list[str], trusted: Callable[[str], bool]) -> int:
    # GitHub logins are case-insensitive, so "A" and "a" are one account.
    return sum(1 for r in {r.lower(): r for r in reactors}.values() if trusted(r))


def young_count(reactions: list[tuple[str, datetime]], ages: dict[str, int]) -> int:
    # A missing age counts as young: an attacker gains nothing by hiding it.
    return sum(1 for login, _ in reactions if ages.get(login, 0) < YOUNG_DAYS)


def detect_burst(today: list[tuple[str, datetime]], ages: dict[str, int]) -> bool:
    return len(today) >= BURST_MIN and young_count(today, ages) * 2 > len(today)


def detect_drip(week: list[tuple[str, datetime]], ages: dict[str, int]) -> bool:
    """The slow version of a burst: spread over 7 days to stay under the 24-hour bar."""
    return len(week) >= DRIP_MIN and young_count(week, ages) * 2 > len(week)


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


def fetch_reactors(gh, numbers: list[int]) -> dict[int, list[dict] | None]:
    """Every 👍 node per Discussion number; None for a Discussion that no longer exists."""
    out = {}
    for i in range(0, len(numbers), REACTOR_BATCH):
        chunk = numbers[i:i + REACTOR_BATCH]
        q = _reactor_query(len(chunk), False)
        repo = gh.graphql(q, {f"n{j}": n for j, n in enumerate(chunk)}, allow_not_found=True)["repository"]
        for j, n in enumerate(chunk):
            d = repo[f"d{j}"]
            if d is None:
                out[n] = None
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
    if out and all(v is None for v in out.values()):
        # Every thread gone at once is a wrong token or repo, not deletions.
        raise GitHubError(404, "none of the entry Discussions were found")
    return out


def fetch_meta(gh, repos: list[str]) -> dict[str, dict]:
    """Repo card data keyed as given. A deleted or IP-allow-listed repo is left out; rot handles the deleted."""
    out = {}
    for i in range(0, len(repos), META_BATCH):
        chunk = repos[i:i + META_BATCH]
        params = ", ".join(f"$o{j}: String!, $n{j}: String!" for j in range(len(chunk)))
        body = " ".join(f"r{j}: repository(owner: $o{j}, name: $n{j}) {{ {_META} }}" for j in range(len(chunk)))
        variables = {}
        for j, r in enumerate(chunk):
            variables[f"o{j}"], variables[f"n{j}"] = r.split("/", 1)
        data = gh.graphql(f"query RepoMeta({params}) {{ {body} }}", variables, allow_not_found=True,
                          allow_forbidden=True)
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
