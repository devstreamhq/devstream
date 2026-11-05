"""Re-dispatch submissions that no run finished, and delete abandoned submit branches.

python -m scripts.sweep   (needs GITHUB_TOKEN)
"""

import os
import re
from datetime import datetime, timedelta, timezone

from scripts.devstream.gh import GitHub

OWNER = "devstreamhq"
API = f"/repos/{OWNER}/devstream"
MAX_DISPATCH = 5
ISSUE_AGE = timedelta(minutes=10)
BRANCH_AGE = timedelta(minutes=30)


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _sweep_branches(gh, now) -> set[int]:
    """Delete stale branches; return issue numbers whose branch is still in use."""
    live = set()
    for ref in gh.paginate(f"{API}/git/matching-refs/heads/submit/"):
        m = re.fullmatch(r"refs/heads/(submit/([0-9]+))", ref["ref"])
        if not m:
            continue
        name, n = m.group(1), int(m.group(2))
        commit = gh.get(f"{API}/commits/{ref['object']['sha']}")
        young = commit is None or now - _parse(commit["commit"]["committer"]["date"]) <= BRANCH_AGE
        if young or gh.get(f"{API}/pulls?state=open&head={OWNER}:{name}"):
            live.add(n)
            continue
        gh.delete(f"{API}/git/refs/heads/{name}")
        print(f"deleted {name}")
    return live


def sweep(gh, now: datetime) -> None:
    # Branches first, so an issue freed by a deleted branch is dispatched this run.
    busy = _sweep_branches(gh, now)
    sent = 0
    for issue in gh.paginate(f"{API}/issues?state=open&labels=submission&sort=created&direction=asc&per_page=100"):
        if sent >= MAX_DISPATCH:
            break
        labels = {lb["name"] for lb in issue["labels"]}
        if ("pull_request" in issue or labels & {"accepted", "rejected"} or issue["number"] in busy
                or now - _parse(issue["created_at"]) <= ISSUE_AGE):
            continue
        gh.post(f"{API}/actions/workflows/submit.yml/dispatches",
                {"ref": "main", "inputs": {"issue": str(issue["number"])}})
        print(f"dispatched #{issue['number']}")
        sent += 1


if __name__ == "__main__":
    sweep(GitHub(os.environ["GITHUB_TOKEN"]), datetime.now(timezone.utc))
