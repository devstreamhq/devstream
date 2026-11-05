"""Re-dispatch submissions that no run finished, and delete abandoned submit branches.

python -m scripts.sweep   (needs GITHUB_TOKEN)
"""

import os
import re
from datetime import datetime, timedelta, timezone

from scripts.devstream.gh import GitHub, GitHubError

OWNER = "devstreamhq"
API = f"/repos/{OWNER}/devstream"
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
        try:
            gh.delete(f"{API}/git/refs/heads/{name}")
        except GitHubError as e:
            # Usually 422: already gone. One bad ref mustn't stop the dispatches.
            print(f"could not delete {name}: {e}")
            live.add(n)  # still there as far as we know, so don't re-apply into it
            continue
        print(f"deleted {name}")
    return live


def _list_write_waiting(gh) -> bool:
    # submit.yml and rot.yml share the list-writes group, which holds one waiting run.
    return any(
        gh.get(f"{API}/actions/workflows/{wf}/runs?status={s}")["total_count"] > 0
        for wf in ("submit.yml", "rot.yml") for s in ("queued", "pending")
    )


def sweep(gh, now: datetime) -> None:
    # Branches first, so an issue freed by a deleted branch can be dispatched this run.
    busy = _sweep_branches(gh, now)
    # The list-writes group keeps one pending run and cancels older ones, so never queue behind a waiting run.
    if _list_write_waiting(gh):
        print("a list-writes run is already waiting")
        return
    todo = []
    for issue in gh.paginate(f"{API}/issues?state=open&labels=submission&per_page=100"):
        labels = {lb["name"] for lb in issue["labels"]}
        if ("pull_request" in issue or labels & {"accepted", "rejected", "needs-review"}
                or issue["number"] in busy or now - _parse(issue["created_at"]) <= ISSUE_AGE):
            continue
        todo.append(issue)
    if not todo:
        return
    oldest = min(todo, key=lambda i: _parse(i["created_at"]))
    gh.post(f"{API}/actions/workflows/submit.yml/dispatches",
            {"ref": "main", "inputs": {"issue": str(oldest["number"])}})
    print(f"dispatched #{oldest['number']}")


if __name__ == "__main__":
    sweep(GitHub(os.environ["GITHUB_TOKEN"]), datetime.now(timezone.utc))
