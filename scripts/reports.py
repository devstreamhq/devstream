"""Act on a "Report an entry" issue, and hide a reported entry in entries.json.

python -m scripts.reports decide (--event EVENT.json | --issue N) --out result.json
python -m scripts.reports dispatch --result result.json
python -m scripts.reports apply --result result.json
python -m scripts.reports issue --result result.json [--error]
python -m scripts.reports pr-body --result result.json
"""

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from scripts.devstream.entries import DEFAULT_PATH, find, load, normalize_repo, save
from scripts.devstream.gh import GitHub
from scripts.devstream.issue_form import FormError, _repo, parse_report
from scripts.devstream.trust import ACTIVITY_QUERY, check_user, ensure_block_access
from scripts.validate_data import validate

REPO = "devstreamhq/devstream"
API = f"/repos/{REPO}"
BOT = "devstreamhq-bot[bot]"
DAILY_LIMIT = 5
HIDE_AT = 3
# Established: old enough and active enough that a fresh sock puppet can't count toward a hide.
ESTABLISHED_AGE = timedelta(days=365)
ESTABLISHED_REPOS = 3
ESTABLISHED_CONTRIBUTIONS = 50

NOT_LISTED = "That repo isn't on the list, so there's nothing here for us to act on."
UNTRUSTED = (
    "Thanks for flagging this. We can't act on reports from this account automatically, "
    "so it's waiting for a maintainer to read instead."
)
RATE_LIMIT = (
    "Thanks for keeping an eye on the list. You've filed 5 reports in the last 24 hours, "
    "which is the daily limit, so we've closed this one. Still worried about it tomorrow? Send it again then."
)
ERROR = "Something went wrong on our side while handling this report. We've been pinged and will sort it out.\n"


@dataclass
class Action:
    kind: str  # comment, label, close, dispatch_rot or hide
    value: str | None = None


def _is_bot(issue: dict) -> bool:
    user = issue.get("user") or {}
    return user.get("login") == BOT or user.get("type") == "Bot"


def _reply(text: str, *labels: str) -> list[Action]:
    return [Action("label", lb) for lb in labels] + [Action("comment", text)]


def _close(text: str) -> list[Action]:
    return [Action("comment", text), Action("close")]


def _established(login: str, gh, now: datetime) -> bool:
    """Assumes check_user already passed."""
    user = gh.get(f"/users/{login}")
    if user is None or now - datetime.fromisoformat(user["created_at"].replace("Z", "+00:00")) < ESTABLISHED_AGE:
        return False
    if user["public_repos"] >= ESTABLISHED_REPOS:
        return True
    since = (now - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = gh.graphql(ACTIVITY_QUERY, {"login": login, "from": since})
    total = data["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"]
    return total >= ESTABLISHED_CONTRIBUTIONS


def _recent_reports(login: str, gh, now: datetime) -> int:
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = f"repo:{REPO}+label:report+author:{login}+created:>={since}"
    return gh.get(f"/search/issues?q={q}")["total_count"]


def _reporters(repo: str, open_reports: list[dict]) -> set[str]:
    """Distinct human logins with an open report on repo."""
    out = set()
    for r in open_reports:
        if _is_bot(r) or "pull_request" in r:
            continue
        try:
            form = parse_report(r.get("body") or "")
        except FormError:
            continue
        if normalize_repo(form["entry"]) == normalize_repo(repo):
            out.add(r["user"]["login"].lower())
    return out


def handle_report(issue: dict, gh, entries: list, open_reports: list[dict],
                  now: datetime | None = None, excluded=frozenset()) -> list[Action]:
    """API errors (GitHubError, RateLimited) propagate: an outage never decides a report."""
    now = now or datetime.now(timezone.utc)
    # The bot files its own report issues for Like bursts; those are for a maintainer.
    if _is_bot(issue):
        return []
    try:
        form = parse_report(issue.get("body") or "")
    except FormError as e:
        return _close(str(e))
    entry = find(entries, form["entry"])
    if entry is None:
        return _close(NOT_LISTED)
    repo = entry["repo"]
    login = issue["user"]["login"]

    ensure_block_access(gh)
    if check_user(login, gh, now, excluded):
        return _reply(UNTRUSTED, "needs-review")
    # check_user has already rejected logins outside [A-Za-z0-9-], so the query is safe.
    if _recent_reports(login, gh, now) > DAILY_LIMIT:
        return _close(RATE_LIMIT)

    queued = f"Thanks for reporting `{repo}`. It's in the queue, and a maintainer will take a look."
    if entry["status"] == "hidden":
        return _reply(f"Thanks. `{repo}` is already hidden while we review it, "
                      "so your report goes into the same review.", "needs-review")
    reason = form["reason"]
    hidden = f"Thanks for the heads-up. `{repo}` is hidden from the list while a maintainer reviews it."

    if reason == "dead":
        return [Action("dispatch_rot", repo)] + _reply(
            f"Thanks. A health check on `{repo}` is running now, and a maintainer will look at what it finds.",
            "needs-review")
    if reason == "malicious":
        if _established(login, gh, now):
            return [Action("hide", repo)] + _reply(hidden, "needs-review")
        return _reply(queued, "needs-review")
    if reason in ("spam", "other"):
        counted = 0
        for who in sorted(_reporters(repo, [issue, *open_reports])):
            if check_user(who, gh, now, excluded) is None and _established(who, gh, now):
                counted += 1
                if counted >= HIDE_AT:
                    return [Action("hide", repo)] + _reply(hidden, "needs-review")
        return _reply(queued, "needs-review")
    if reason == "superseded":
        raw = form["superseded_by"]
        if raw is None:
            return _reply(queued, "superseded", "needs-review")
        try:
            new = _repo(raw)
        except FormError:
            return _reply(
                f"Thanks for reporting `{repo}`. The \"Replaced by\" field doesn't look like a GitHub repo "
                "(owner/name), so a maintainer will review it without that.", "superseded", "needs-review")
        return _reply(f"Thanks. A maintainer will compare `{repo}` with `{new}` and decide what stays on the list.",
                      "superseded", "needs-review")
    return _reply(queued, "needs-review")


def _fetch_issue(gh, raw: str) -> dict | None:
    """The issue for a dispatched run, or None when there's nothing to decide."""
    # str.isdigit() accepts non-ASCII digits, so match ASCII explicitly.
    if not re.fullmatch(r"[0-9]{1,9}", raw):
        raise SystemExit(f"--issue must be an issue number, got {raw!r}")
    issue = gh.get(f"{API}/issues/{int(raw)}")
    if issue is None or "pull_request" in issue or issue.get("state") != "open" or _is_bot(issue):
        return None
    labels = {lb["name"] for lb in issue.get("labels") or []}
    # needs-review means a run already acted; acting again would comment twice.
    if "report" not in labels or "needs-review" in labels:
        return None
    return issue


def _read(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _values(result: dict, kind: str) -> list:
    return [a["value"] for a in result["actions"] if a["kind"] == kind]


def _decide(args) -> int:
    gh = GitHub(os.environ["GITHUB_TOKEN"])
    if args.issue is not None:
        issue = _fetch_issue(gh, args.issue)
        if issue is None:
            print(f"issue {args.issue} is not an open, undecided report", file=sys.stderr)
            return 0
    else:
        issue = _read(args.event)["issue"]
        if _is_bot(issue):
            return 0
    excluded = {u.strip() for u in os.environ.get("EXCLUDED_USERS", "").split(",") if u.strip()}
    open_reports = list(gh.paginate(f"{API}/issues?state=open&labels=report&per_page=100"))
    actions = handle_report(issue, gh, load(), open_reports, datetime.now(timezone.utc), excluded)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"issue": issue["number"], "actions": [asdict(a) for a in actions]}, f, indent=2)
    return 0


def _dispatch(args) -> int:
    for repo in _values(_read(args.result), "dispatch_rot"):
        GitHub(os.environ["GITHUB_TOKEN"]).post(
            f"{API}/actions/workflows/rot.yml/dispatches", {"ref": "main", "inputs": {"repo": repo}})
    return 0


def _apply(args) -> int:
    """Print the branch name, or nothing when fresh main already has the entry hidden."""
    result = _read(args.result)
    entries = load(args.entries)
    for repo in _values(result, "hide"):
        e = find(entries, repo)
        if e is None:
            print(f"{repo} is no longer on the list", file=sys.stderr)
            return 1
        if e["status"] == "hidden":
            return 0
        e["status"] = "hidden"
        save(entries, args.entries)
        errors = validate(load(args.entries))
        for err in errors:
            print(err, file=sys.stderr)
        if errors:
            return 1
        print(f"report/{result['issue']}")
    return 0


def _issue(args) -> int:
    result = _read(args.result)
    gh = GitHub(os.environ["GITHUB_TOKEN"])
    n = result["issue"]
    if args.error:
        gh.post(f"{API}/issues/{n}/labels", {"labels": ["needs-review"]})
        gh.post(f"{API}/issues/{n}/comments", {"body": ERROR})
        return 0
    labels = _values(result, "label")
    if labels:
        gh.post(f"{API}/issues/{n}/labels", {"labels": labels})
    for text in _values(result, "comment"):
        gh.post(f"{API}/issues/{n}/comments", {"body": text})
    if _values(result, "close"):
        gh.patch(f"{API}/issues/{n}", {"state": "closed", "state_reason": "not_planned"})
    return 0


def _pr_body(args) -> int:
    result = _read(args.result)
    sys.stdout.write(f"Hides `{_values(result, 'hide')[0]}` while #{result['issue']} is reviewed.\n")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.reports")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    src = d.add_mutually_exclusive_group(required=True)
    src.add_argument("--event")
    src.add_argument("--issue")
    d.add_argument("--out", required=True)
    for name in ("dispatch", "apply", "issue", "pr-body"):
        s = sub.add_parser(name)
        s.add_argument("--result", required=True)
    sub.choices["apply"].add_argument("--entries", default=DEFAULT_PATH)
    sub.choices["issue"].add_argument("--error", action="store_true")
    args = p.parse_args(argv)
    return {"decide": _decide, "dispatch": _dispatch, "apply": _apply, "issue": _issue,
            "pr-body": _pr_body}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
