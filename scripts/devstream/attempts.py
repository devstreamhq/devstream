"""Bound how often an issue's decide run can fail, so one stuck issue can't hold the sweeper."""

import json
import re
import time

from scripts.devstream.gh import RateLimited

API = "/repos/devstreamhq/devstream"
BOT = "devstreamhq-bot[bot]"
MAX_FAILURES = 3
COUNTER = re.compile(r"<!-- devstream:undecided ([0-9]{1,4}) -->")
RETRYING = "We couldn't finish checking this one yet, so we'll try again shortly.\n\n<!-- devstream:undecided {} -->\n"
PARKED = "We couldn't finish checking this one, so it's waiting for a maintainer.\n\n<!-- devstream:undecided {} -->\n"


def spends_attempt(exc: BaseException) -> bool:
    """A rate limit still in force just waits for its reset; any other failure counts."""
    return not (isinstance(exc, RateLimited) and exc.reset_at > time.time())


def note_failure(path: str, issue: int, exc: BaseException) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"issue": issue, "error": str(exc)}, f)


def record_failure(gh, n: int, error: str, error_body: str) -> bool:
    """Count one more failed run on issue n; from the MAX_FAILURES-th on, park it. True when parked.

    The count lives in a hidden marker in the bot's own comment, so the issue's author can't forge it.
    """
    mine = next((c for c in gh.paginate(f"{API}/issues/{n}/comments?per_page=100")
                 if (c.get("user") or {}).get("login") == BOT and COUNTER.search(c.get("body") or "")), None)
    # Read-then-write, so two runs failing at once can count as one. That's fine: the sweeper
    # dispatches one item at a time, and a lost count only costs one extra retry.
    count = int(COUNTER.search(mine["body"]).group(1)) + 1 if mine else 1
    body = (RETRYING if count < MAX_FAILURES else PARKED).format(count)
    if mine:
        gh.patch(f"{API}/issues/comments/{mine['id']}", {"body": body})
    else:
        gh.post(f"{API}/issues/{n}/comments", {"body": body})
    if count < MAX_FAILURES:
        return False
    # The sweeper skips needs-review, so it moves on. Re-added each time, so a maintainer's retry
    # that fails again can't bring the starvation back.
    gh.post(f"{API}/issues/{n}/labels", {"labels": ["needs-review"]})
    if count > MAX_FAILURES:
        return True
    detail = " ".join(error.replace("`", "'").split())[:300]
    gh.post(f"{API}/issues/{n}/comments", {"body": error_body + f"\n`{detail}`\n"})
    return True


def record_from_file(gh, path: str, error_body: str) -> None:
    with open(path, encoding="utf-8") as f:
        u = json.load(f)
    if type(u["issue"]) is not int or u["issue"] <= 0:
        raise SystemExit(f"bad issue number {u['issue']!r}")
    record_failure(gh, u["issue"], str(u["error"]), error_body)
