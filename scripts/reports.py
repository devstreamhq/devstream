"""Act on a "Report an entry" issue, and hide a reported entry in entries.json.

python -m scripts.reports decide (--event EVENT.json | --issue N) --out result.json
python -m scripts.reports dispatch --result result.json
python -m scripts.reports apply --result result.json
python -m scripts.reports issue --result result.json [--error]
python -m scripts.reports pr-body --result result.json
python -m scripts.reports decide ... --undecided undecided.json   (exits 3 when the run reached no decision)
python -m scripts.reports undecided --file undecided.json   (counts it, and parks the report after 3)
"""

import argparse
import json
import os
import re
import sys
import traceback
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from scripts.devstream.attempts import note_failure, record_from_file, spends_attempt
from scripts.devstream.entries import DEFAULT_PATH, find, load, normalize_repo, save
from scripts.devstream.gh import GitHub
from scripts.devstream.issue_form import FormError, _repo, parse_report
from scripts.devstream.stats import read_json
from scripts.devstream.trust import ACTIVITY_QUERY, LOGIN_RE, blocked_logins, check_user
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
# rot.yml names a one-repo run "Health check: owner/name".
ONE_REPO = "Health check: "
RECHECK_AFTER = timedelta(hours=24)
# One-repo checks an hour, across all reporters, before new dead reports wait for a maintainer.
CHECKS_PER_HOUR = 3

NOT_LISTED = "That repo isn't on the list, so there's nothing here for us to act on."
UNTRUSTED = (
    "Thanks for flagging this. We can't act on reports from this account automatically, "
    "so it's waiting for a maintainer to read instead."
)
RATE_LIMIT = "You've hit the daily limit of 5 reports. Try again tomorrow."
COUNTED = ("spam", "other")
# A spam or other report counts toward a hide for this long.
REPORT_MAX_AGE = timedelta(days=30)
# hidden.json saying hidden while the entry is visible means "just un-hidden" only while on_merge is keeping it fresh.
JUST_UNHIDDEN = timedelta(hours=2)
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
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
    since = (now - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = gh.graphql(ACTIVITY_QUERY, {"login": login, "from": since})
    if data["user"]["contributionsCollection"]["contributionCalendar"]["totalContributions"] >= ESTABLISHED_CONTRIBUTIONS:
        return True
    # Forks cost one click each, so only repos the account made itself count.
    owned = 0
    for r in gh.paginate(f"/users/{login}/repos?type=owner&per_page=100"):
        owned += r.get("fork") is False
        if owned >= ESTABLISHED_REPOS:
            return True
    return False


def _other_recent_reports(login: str, number: int, gh, now: datetime) -> int:
    """Excludes the current issue, so search lag can't let an extra one through."""
    since = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = f"repo:{REPO}+label:report+author:{login}+created:>={since}"
    # shortcut: one page of 100 is plenty to see 5.
    return sum(1 for i in gh.get(f"/search/issues?q={q}&per_page=100")["items"] if i["number"] != number)


def _recent_checks(gh, now: datetime) -> list[tuple[datetime, str]]:
    """(created, normalised repo) for one-repo health checks from the last 24 hours, any status.

    Every queued or running check is in there too: a run can't wait in the queue longer than that.
    """
    since = (now - RECHECK_AFTER).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = urllib.parse.urlencode({"created": f">={since}", "per_page": 100})
    # shortcut: newest 100 runs only. The cap allows 72 a day, plus manual runs; paginate if those
    # ever push a day's one-repo checks past 100.
    page = gh.get(f"{API}/actions/workflows/rot.yml/runs?{query}") or {}
    return [(datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")), normalize_repo(r["display_title"][len(ONE_REPO):]))
            for r in page.get("workflow_runs", []) if (r.get("display_title") or "").startswith(ONE_REPO)]


def _unedited(gh, numbers: list[int]) -> set[int]:
    """The issues whose body is still as filed: an edited report could have been decided as something else."""
    if not numbers:
        return set()
    fields = " ".join(f"i{k}: issue(number: $n{k}) {{ lastEditedAt }}" for k in range(len(numbers)))
    params = ", ".join(f"$n{k}: Int!" for k in range(len(numbers)))
    owner, name = REPO.split("/")
    query = f'query ReportEdits({params}) {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'
    data = gh.graphql(query, {f"n{k}": n for k, n in enumerate(numbers)})["repository"]
    return {n for k, n in enumerate(numbers) if (data.get(f"i{k}") or {"lastEditedAt": "gone"})["lastEditedAt"] is None}


def _parse(s) -> datetime:
    """Strict UTC timestamp: fromisoformat would take a bare date or a naive time."""
    if not isinstance(s, str) or not TIMESTAMP.fullmatch(s):
        raise ValueError(f"bad timestamp: {s!r}")
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _counts_since(repo: str, gh, now: datetime) -> datetime:
    """Reports filed before this don't count: too old, or already reviewed when a maintainer un-hid the entry."""
    since = now - REPORT_MAX_AGE
    # No hidden.json yet means the plain 30-day window, as before it existed.
    rec = (read_json(gh, "hidden.json")[0] or {}).get(normalize_repo(repo)) or {}
    # Hidden at on_merge's last look but visible now: un-hidden since, after every report so far.
    # A stale look means on_merge is failing, so fall back rather than block hides indefinitely.
    if rec.get("hidden") and now - _parse(rec.get("seen_at")) <= JUST_UNHIDDEN:
        return now
    if rec.get("unhidden_at") is not None:
        since = max(since, _parse(rec["unhidden_at"]))
    return since


def _reporters(repo: str, open_reports: list[dict], gh, since: datetime) -> set[str]:
    """Distinct human logins with an open, unedited spam or other report on repo, filed after since."""
    found = {}
    for r in open_reports:
        if _is_bot(r) or "pull_request" in r or _parse(r["created_at"]) < since:
            continue
        try:
            form = parse_report(r.get("body") or "")
        except FormError:
            continue
        if form["reason"] in COUNTED and normalize_repo(form["entry"]) == normalize_repo(repo):
            found[r["number"]] = r["user"]["login"].lower()
    keep = _unedited(gh, sorted(found))
    return {who for n, who in found.items() if n in keep}


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

    # The login goes into a search query, so it has to be a plain GitHub login first.
    if not LOGIN_RE.match(login):
        return _reply(UNTRUSTED, "needs-review")
    # Before the trust lookups, so a flood costs one search per report.
    if _other_recent_reports(login, issue["number"], gh, now) >= DAILY_LIMIT:
        return _close(RATE_LIMIT)
    excluded = set(excluded) | blocked_logins(gh)
    if check_user(login, gh, now, excluded):
        return _reply(UNTRUSTED, "needs-review")

    queued = f"Thanks for reporting `{repo}`. It's in the queue, and a maintainer will take a look."
    if entry["status"] == "hidden":
        return _reply(f"Thanks. `{repo}` is already hidden while we review it, "
                      "so your report goes into the same review.", "needs-review")
    reason = form["reason"]
    hidden = f"Thanks for the heads-up. `{repo}` is hidden from the list while a maintainer reviews it."

    if reason == "dead":
        recent = _recent_checks(gh, now)
        if normalize_repo(repo) in {r for _, r in recent}:
            return _reply(f"Thanks. `{repo}` had a health check in the last day, "
                          "so a maintainer will take it from here.", "needs-review")
        if sum(now - at <= timedelta(hours=1) for at, _ in recent) >= CHECKS_PER_HOUR:
            return _reply(f"Thanks for reporting `{repo}`. A lot of health checks are queued right now, "
                          "so a maintainer will take a look instead.", "needs-review")
        return [Action("dispatch_rot", repo)] + _reply(
            f"A health check on `{repo}` has been queued.",
            "needs-review")
    if reason == "malicious":
        if _established(login, gh, now):
            return [Action("hide", repo)] + _reply(hidden, "needs-review")
        return _reply(queued, "needs-review")
    if reason in ("spam", "other"):
        counted = 0
        others = [r for r in open_reports if r.get("number") != issue["number"]]
        for who in sorted(_reporters(repo, [issue, *others], gh, _counts_since(repo, gh, now))):
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
    """Exit 3 with undecided.json on any failure, so the report can be parked after a few."""
    try:
        return _decide_issue(args)
    except Exception as e:
        if args.undecided is None or not spends_attempt(e):
            raise
        traceback.print_exc()
        n = int(args.issue) if args.issue is not None else _read(args.event)["issue"]["number"]
        note_failure(args.undecided, n, e)
        return 3


def _undecided(args) -> int:
    record_from_file(GitHub(os.environ["GITHUB_TOKEN"]), args.file, ERROR)
    return 0


def _decide_issue(args) -> int:
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
    result = _read(args.result)
    try:
        for repo in _values(result, "dispatch_rot"):
            GitHub(os.environ["GITHUB_TOKEN"]).post(
                f"{API}/actions/workflows/rot.yml/dispatches", {"ref": "main", "inputs": {"repo": repo}})
    except Exception as e:
        # No comment has gone out yet, so this run reached no decision either.
        if args.undecided is None or not spends_attempt(e):
            raise
        traceback.print_exc()
        note_failure(args.undecided, result["issue"], e)
        return 3
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
    d.add_argument("--undecided")
    sub.add_parser("undecided").add_argument("--file", required=True)
    for name in ("dispatch", "apply", "issue", "pr-body"):
        s = sub.add_parser(name)
        s.add_argument("--result", required=True)
    sub.choices["apply"].add_argument("--entries", default=DEFAULT_PATH)
    sub.choices["issue"].add_argument("--error", action="store_true")
    sub.choices["dispatch"].add_argument("--undecided")
    args = p.parse_args(argv)
    return {"decide": _decide, "dispatch": _dispatch, "apply": _apply, "issue": _issue,
            "pr-body": _pr_body, "undecided": _undecided}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
