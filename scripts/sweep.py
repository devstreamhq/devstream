"""Re-dispatch submissions and reports that no run finished, re-run an overdue health check, and
clear abandoned submit and rot branches and rot PRs.

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
BOT = "devstreamhq-bot[bot]"
ROT_AGE = timedelta(days=1)
ROT_DUE = timedelta(days=8)
# Each failed rot run can leave a PR behind, so a failing check is retried at most daily.
ROT_RETRY = timedelta(days=1)
ROT_BRANCH = re.compile(r"rot/[0-9]{1,20}")
# rot.yml's run-name for a full run; a one-repo re-check ("Health check: owner/name") doesn't cover the list.
FULL_ROT = "Health check"


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _delete_branch(gh, name) -> bool:
    try:
        gh.delete(f"{API}/git/refs/heads/{name}")
    except GitHubError as e:
        # Usually 422: already gone. One bad ref mustn't stop the dispatches.
        print(f"could not delete {name}: {e}")
        return False
    print(f"deleted {name}")
    return True


def _committed_at(gh, sha) -> datetime | None:
    commit = gh.get(f"{API}/commits/{sha}")
    return None if commit is None else _parse(commit["commit"]["committer"]["date"])


def _sweep_branches(gh, now) -> set[int]:
    """Delete stale branches; return issue numbers whose branch is still in use."""
    live = set()
    for ref in gh.paginate(f"{API}/git/matching-refs/heads/submit/"):
        m = re.fullmatch(r"refs/heads/(submit/([0-9]+))", ref["ref"])
        if not m:
            continue
        name, n = m.group(1), int(m.group(2))
        at = _committed_at(gh, ref["object"]["sha"])
        young = at is None or now - at <= BRANCH_AGE
        if young or gh.get(f"{API}/pulls?state=open&head={OWNER}:{name}"):
            live.add(n)
            continue
        if not _delete_branch(gh, name):
            live.add(n)  # still there as far as we know, so don't re-apply into it
    return live


def _sweep_rot_branches(gh, now) -> None:
    for ref in gh.paginate(f"{API}/git/matching-refs/heads/rot/"):
        name = ref["ref"].removeprefix("refs/heads/")
        if not ref["ref"].startswith("refs/heads/") or not ROT_BRANCH.fullmatch(name):
            continue
        at = _committed_at(gh, ref["object"]["sha"])
        if at is None or now - at <= ROT_AGE or gh.get(f"{API}/pulls?state=open&head={OWNER}:{name}"):
            continue
        _delete_branch(gh, name)


def _close_rot_prs(gh, now) -> None:
    """Close the bot's rot PRs that never merged, so next week's run starts clean."""
    for pr in gh.paginate(f"{API}/pulls", {"state": "open", "per_page": 100}):
        head = pr.get("head") or {}
        if ((pr.get("user") or {}).get("login") != BOT
                or (head.get("repo") or {}).get("full_name") != f"{OWNER}/devstream"
                or not ROT_BRANCH.fullmatch(head.get("ref") or "")
                or now - _parse(pr["created_at"]) <= ROT_AGE):
            continue
        try:
            gh.patch(f"{API}/pulls/{pr['number']}", {"state": "closed"})
        except GitHubError as e:
            print(f"could not close #{pr['number']}: {e}")
            continue
        print(f"closed #{pr['number']}")
        _delete_branch(gh, head["ref"])


def _latest_full_run(gh, query) -> datetime | None:
    # shortcut: looks at the newest 100 runs only; fine while one-repo re-checks stay rare.
    page = gh.get(f"{API}/actions/workflows/rot.yml/runs?{query}per_page=100")["workflow_runs"]
    return max((_parse(r["created_at"]) for r in page if r.get("display_title") == FULL_ROT), default=None)


def _rot_overdue(gh, now) -> bool:
    ok = _latest_full_run(gh, "status=success&")
    if ok is not None and now - ok <= ROT_DUE:
        return False
    latest = _latest_full_run(gh, "")
    return latest is None or now - latest > ROT_RETRY


def _list_write_waiting(gh) -> bool:
    # These share the list-writes group, which holds one waiting run.
    return any(
        gh.get(f"{API}/actions/workflows/{wf}/runs?status={s}")["total_count"] > 0
        for wf in ("submit.yml", "rot.yml", "on-merge.yml", "reports.yml") for s in ("queued", "pending")
    )


def sweep(gh, now: datetime) -> None:
    # Branches first, so an issue freed by a deleted branch can be dispatched this run.
    busy = _sweep_branches(gh, now)
    _sweep_rot_branches(gh, now)
    _close_rot_prs(gh, now)
    # The list-writes group keeps one pending run and cancels older ones, so never queue behind a waiting run.
    if _list_write_waiting(gh):
        print("a list-writes run is already waiting")
        return
    # One dispatch per sweep, and an overdue health check goes first.
    if _rot_overdue(gh, now):
        gh.post(f"{API}/actions/workflows/rot.yml/dispatches", {"ref": "main"})
        print("dispatched rot.yml")
        return
    todo = []
    # Submissions and reports share the one dispatch; every handled report is closed or needs-review.
    for label, workflow, done in (("submission", "submit.yml", {"accepted", "rejected", "needs-review"}),
                                  ("report", "reports.yml", {"needs-review"})):
        for issue in gh.paginate(f"{API}/issues?state=open&labels={label}&per_page=100"):
            labels = {lb["name"] for lb in issue["labels"]}
            user = issue.get("user") or {}
            if (label not in labels or "pull_request" in issue or labels & done
                    or issue["number"] in busy or now - _parse(issue["created_at"]) <= ISSUE_AGE
                    # The bot's own Like-burst reports are for a maintainer, not reports.yml.
                    or (label == "report" and (user.get("login") == BOT or user.get("type") == "Bot"))):
                continue
            todo.append((issue, workflow))
    if not todo:
        return
    oldest, workflow = min(todo, key=lambda t: _parse(t[0]["created_at"]))
    gh.post(f"{API}/actions/workflows/{workflow}/dispatches",
            {"ref": "main", "inputs": {"issue": str(oldest["number"])}})
    print(f"dispatched #{oldest['number']}")


if __name__ == "__main__":
    sweep(GitHub(os.environ["GITHUB_TOKEN"]), datetime.now(timezone.utc))
