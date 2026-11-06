"""Decide on a repo submission issue, and apply an accepted one to entries.json.

python -m scripts.submit decide (--event EVENT.json | --issue N) --out result.json
python -m scripts.submit apply --result result.json   (prints the branch, "done" if this issue is already listed,
                                                     or nothing when it went down the reject path)
python -m scripts.submit decide ... --undecided undecided.json   (exits 3 when the run reached no decision)
python -m scripts.submit undecided --file undecided.json   (counts it, and parks the issue after 3)
python -m scripts.submit comment --result result.json --kind {accepted,rejected,pr,error}
python -m scripts.submit seed --file seeds.csv --out report.json [--apply]
"""

import argparse
import csv
import json
import os
import re
import sys
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from scripts.devstream.attempts import note_failure, record_from_file, spends_attempt
from scripts.devstream.description import clean_description
from scripts.devstream.entries import CATEGORIES, DEFAULT_PATH, Entry, find, load, normalize_repo, save
from scripts.devstream.gh import GitHub, GitHubError, RateLimited
from scripts.devstream.issue_form import FormError, parse_submission
from scripts.devstream.license_detect import extra_licenses_for
from scripts.devstream.quality import check_repo
from scripts.devstream.trust import blocked_logins, check_user
from scripts.validate_data import validate

REPO = "devstreamhq/devstream"
SITE = "https://devstream.jamesconway.dev"
QUALITY_BAR = f"https://github.com/{REPO}/wiki/Quality-Bar"
WEEKLY_LIMIT = 3
SEED_BY = "jamesconwaydev"
# The schema takes any integer; 0 marks an entry that no submission issue backs.
SEED_ISSUE = 0

RATE_LIMIT = "You've hit the limit of 3 submissions a week. Try again in a few days."
DUPLICATE = "This repo is already on the list."
ARCHIVED_DUP = (
    "This repo was listed before and is in the archive. "
    "It comes back automatically once it's active again."
)
NOT_FOUND = "We couldn't find that repo on GitHub. Is it public?"
SEED_DUP = "This repo appears earlier in the seed file."


@dataclass
class Decision:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    entry: Entry | None = None


def _dup_reason(e: Entry) -> str:
    return ARCHIVED_DUP if e["status"] in ("archived", "superseded") else DUPLICATE


def _other_recent_submissions(login: str, number: int, gh, now: datetime) -> int:
    """Excludes the current issue, so search lag can't let an extra one through."""
    since = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    q = f"repo:{REPO}+label:submission+author:{login}+created:>={since}"
    # shortcut: one page of 100 is plenty to see 3.
    return sum(1 for i in gh.get(f"/search/issues?q={q}&per_page=100")["items"] if i["number"] != number)


def decide(issue: dict, gh, now: datetime, entries: list[Entry], excluded: set[str]) -> Decision:
    """API errors (GitHubError, RateLimited, Undecided) propagate: never a rejection."""
    login = issue["user"]["login"]
    try:
        form = parse_submission(issue.get("body") or "")
    except FormError as e:
        return Decision(False, [str(e)])

    reason = check_user(login, gh, now, excluded | blocked_logins(gh))
    if reason:
        return Decision(False, [reason])

    reasons = []
    # check_user has already rejected logins outside [A-Za-z0-9-], so the query is safe.
    if _other_recent_submissions(login, issue["number"], gh, now) >= WEEKLY_LIMIT:
        reasons.append(RATE_LIMIT)

    more, canonical, description, repo_id = _check(form["repository"], form["description"], gh, now, entries,
                                                   form["category"])
    reasons += more
    if reasons:
        return Decision(False, reasons)
    return Decision(True, [], _entry(canonical, repo_id, form["category"], description, login, now, issue["number"]))


def _check(link: str, text: str, gh, now: datetime, entries: list[Entry],
           category: str) -> tuple[list[str], str, str | None, int | None]:
    """The repo and description checks: (reasons, canonical repo, cleaned description, repo ID)."""
    dup = find(entries, link)
    if dup:
        return [_dup_reason(dup)], link, None, None

    reasons = []
    result = check_repo(link, gh, now, "submit", allowed_extra=extra_licenses_for(category))
    if not result.exists:
        reasons.append(NOT_FOUND)
    else:
        dup = find(entries, result.canonical)
        if dup:
            return [_dup_reason(dup)], result.canonical, None, None
        reasons += result.failures

    description, why = clean_description(text)
    if description is None and result.meta.get("description"):
        description = clean_description(result.meta["description"])[0]
    if description is None:
        reasons.append(why)
    return reasons, result.canonical, description, result.repo_id


def _entry(repo: str, repo_id: int, category: str, description: str, login: str, now: datetime, issue: int,
           seed: bool = False) -> Entry:
    return {
        "repo": repo,
        "repo_id": repo_id,
        "category": category,
        "description": description,
        "submitted_by": login,
        "submitted_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issue": issue,
        "status": "live",
        "warned_at": None,
        "superseded_by": None,
        "added_by_seed": seed,
    }


def comment(result: dict, kind: str) -> str:
    """Only fixed strings and the normalised owner/name; never text from the issue."""
    if kind == "accepted":
        return (
            f"🎉 `{result['entry']['repo']}` is on the list. It's live at {SITE}, "
            "and Likes start counting as soon as its Discussion opens.\n"
        )
    if kind == "error":
        return "Something went wrong on our side while adding this. We've been pinged and will sort it out.\n"
    if kind == "pr":
        e = result["entry"]
        # Code span so an @name in the description can't ping anyone.
        return (
            f"Closes #{result['issue']}\n\n"
            f"Category: {CATEGORIES[e['category']]}\n\n"
            f"Description: `{e['description']}`\n"
        )
    bullets = "".join(f"- {r}\n" for r in result["reasons"])
    return (
        "Thanks for the suggestion! It can't go on the list just yet:\n\n"
        f"{bullets}\n"
        f"The [quality bar]({QUALITY_BAR}) has the details. "
        "Fix what's above and submit again whenever you're ready. We'd love to see it back.\n"
    )


def _fetch_issue(gh, raw: str) -> dict | None:
    """The issue for a dispatched run, or None when there's nothing to decide."""
    # str.isdigit() accepts non-ASCII digits, so match ASCII explicitly.
    if not re.fullmatch(r"[0-9]{1,9}", raw):
        raise SystemExit(f"--issue must be an issue number, got {raw!r}")
    issue = gh.get(f"/repos/{REPO}/issues/{int(raw)}")
    if issue is None or "pull_request" in issue or issue.get("state") != "open":
        return None
    labels = {lb["name"] for lb in issue.get("labels") or []}
    # Already accepted or rejected: deciding again would comment twice.
    if "submission" not in labels or labels & {"accepted", "rejected"}:
        return None
    return issue


def _undecided(args) -> int:
    record_from_file(GitHub(os.environ["GITHUB_TOKEN"]), args.file, comment({}, "error"))
    return 0


def _decide(args) -> int:
    """Exit 3 with undecided.json on any failure, so the issue can be parked after a few."""
    try:
        return _decide_issue(args)
    except Exception as e:
        if args.undecided is None or not spends_attempt(e):
            raise
        traceback.print_exc()
        if args.issue is not None:
            n = int(args.issue)
        else:
            with open(args.event, encoding="utf-8") as f:
                n = json.load(f)["issue"]["number"]
        note_failure(args.undecided, n, e)
        return 3


def _decide_issue(args) -> int:
    gh = GitHub(os.environ["GITHUB_TOKEN"])
    if args.issue is not None:
        issue = _fetch_issue(gh, args.issue)
        if issue is None:
            print(f"issue {args.issue} is not an open, undecided submission", file=sys.stderr)
            return 0
    else:
        with open(args.event, encoding="utf-8") as f:
            issue = json.load(f)["issue"]
    entries = load()
    # A second dispatch of a submission another run already added: deciding again would reject it.
    if any(e["issue"] == issue["number"] for e in entries):
        print(f"issue {issue['number']} is already on the list", file=sys.stderr)
        return 0
    excluded = {u.strip() for u in os.environ.get("EXCLUDED_USERS", "").split(",") if u.strip()}
    d = decide(issue, gh, datetime.now(timezone.utc), entries, excluded)
    out = asdict(d) | {"issue": issue["number"], "author": issue["user"]["login"]}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return 0


def _apply(args) -> int:
    with open(args.result, encoding="utf-8") as f:
        result = json.load(f)
    if result.get("ok") is not True or not result.get("entry"):
        print("result is not an accepted submission", file=sys.stderr)
        return 1
    entries = load(args.entries)
    # main may have moved since decide ran; a duplicate now goes down the reject path.
    dup = find(entries, result["entry"]["repo"])
    if dup and dup["issue"] == result["issue"]:
        # Another run of this same issue added it, and that run answers the issue.
        print("done")
        return 0
    if dup:
        result.update(ok=False, reasons=[_dup_reason(dup)], entry=None)
        with open(args.result, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        return 0
    entries.append(result["entry"])
    save(entries, args.entries)
    errors = validate(load(args.entries))
    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        return 1
    print(f"submit/{result['entry']['issue']}")
    return 0


def _seed_row(row: dict, gh, now: datetime, entries: list[Entry], seen: set[str]) -> dict:
    out = {"repo": row["repo"], "category": row["category"], "status": "fail", "reasons": [], "entry": None}
    if normalize_repo(row["repo"]) in seen:
        out["reasons"] = [SEED_DUP]
        return out
    if row["category"] not in CATEGORIES:
        out["reasons"] = [f"unknown category {row['category']!r}"]
        return out
    try:
        reasons, canonical, description, repo_id = _check(row["repo"], row["description"], gh, now, entries,
                                                          row["category"])
    except (GitHubError, RateLimited, OSError) as e:
        # Undecided is a GitHubError; none of these say anything about the repo.
        return out | {"status": "undecided", "reasons": [str(e)]}
    if normalize_repo(canonical) in seen:
        reasons = [SEED_DUP]
    if reasons:
        return out | {"reasons": reasons}
    return out | {"status": "ok", "entry": _entry(canonical, repo_id, row["category"], description, SEED_BY, now,
                                                   SEED_ISSUE, seed=True)}


def _seed(args) -> int:
    with open(args.file, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    done: dict[str, dict] = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as f:
            for r in json.load(f)["rows"]:
                if r["status"] != "undecided":
                    done.setdefault(r["repo"], r)
    gh = GitHub(os.environ["GITHUB_TOKEN"])
    entries = load(args.entries)
    known = list(entries)
    seen: set[str] = set()
    report = []
    for row in rows:
        r = done.pop(row["repo"], None) or _seed_row(row, gh, datetime.now(timezone.utc), known, seen)
        seen.add(normalize_repo(row["repo"]))
        if r["entry"]:
            seen.add(normalize_repo(r["entry"]["repo"]))
            known.append(r["entry"])
        report.append(r)
        # Written every row so a stopped run resumes where it left off.
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"rows": report}, f, indent=2, ensure_ascii=False)
        print(f"{r['status']}\t{r['repo']}\t{'; '.join(r['reasons'])}", flush=True)
    if not args.apply:
        return 0
    entries += [r["entry"] for r in report if r["status"] == "ok" and not find(entries, r["entry"]["repo"])]
    save(entries, args.entries)
    errors = validate(load(args.entries))
    for e in errors:
        print(e, file=sys.stderr)
    return 1 if errors else 0


def _comment(args) -> int:
    with open(args.result, encoding="utf-8") as f:
        sys.stdout.write(comment(json.load(f), args.kind))
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.submit")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    src = d.add_mutually_exclusive_group(required=True)
    src.add_argument("--event")
    src.add_argument("--issue")
    d.add_argument("--out", required=True)
    d.add_argument("--undecided")
    u = sub.add_parser("undecided")
    u.add_argument("--file", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--result", required=True)
    a.add_argument("--entries", default=DEFAULT_PATH)
    sd = sub.add_parser("seed")
    sd.add_argument("--file", required=True)
    sd.add_argument("--out", required=True)
    sd.add_argument("--entries", default=DEFAULT_PATH)
    sd.add_argument("--apply", action="store_true")
    c = sub.add_parser("comment")
    c.add_argument("--result", required=True)
    c.add_argument("--kind", required=True, choices=("accepted", "rejected", "pr", "error"))
    args = p.parse_args(argv)
    return {"decide": _decide, "apply": _apply, "comment": _comment, "seed": _seed,
            "undecided": _undecided}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
