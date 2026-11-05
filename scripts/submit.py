"""Decide on a repo submission issue, and apply an accepted one to entries.json.

python -m scripts.submit decide --event EVENT.json --out result.json
python -m scripts.submit apply --result result.json
python -m scripts.submit comment --result result.json --kind {accepted,rejected,pr}
"""

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from scripts.devstream.description import clean_description
from scripts.devstream.entries import CATEGORIES, DEFAULT_PATH, Entry, find, load, save
from scripts.devstream.gh import GitHub
from scripts.devstream.issue_form import FormError, parse_submission
from scripts.devstream.quality import check_repo
from scripts.devstream.trust import check_user, ensure_block_access
from scripts.validate_data import validate

REPO = "devstreamhq/devstream"
SITE = "https://devstream.jamesconway.dev"
QUALITY_BAR = f"https://github.com/{REPO}/wiki/Quality-Bar"
WEEKLY_LIMIT = 3

RATE_LIMIT = "You've hit the limit of 3 submissions a week. Try again in a few days."
DUPLICATE = "This repo is already on the list."
ARCHIVED_DUP = (
    "This repo was listed before and is in the archive. "
    "It comes back automatically once it's active again."
)
NOT_FOUND = "We couldn't find that repo on GitHub. Is it public?"


@dataclass
class Decision:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    entry: Entry | None = None


def _dup_reason(e: Entry) -> str:
    return ARCHIVED_DUP if e["status"] in ("archived", "superseded") else DUPLICATE


def _recent_submissions(login: str, gh, now: datetime) -> int:
    since = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    q = f"repo:{REPO}+label:submission+author:{login}+created:>={since}"
    return gh.get(f"/search/issues?q={q}")["total_count"]


def decide(issue: dict, gh, now: datetime, entries: list[Entry], excluded: set[str]) -> Decision:
    """API errors (GitHubError, RateLimited, Undecided) propagate: never a rejection."""
    login = issue["user"]["login"]
    try:
        form = parse_submission(issue.get("body") or "")
    except FormError as e:
        return Decision(False, [str(e)])

    ensure_block_access(gh)
    reason = check_user(login, gh, now, excluded)
    if reason:
        return Decision(False, [reason])

    reasons = []
    # check_user has already rejected logins outside [A-Za-z0-9-], so the query is safe.
    if _recent_submissions(login, gh, now) > WEEKLY_LIMIT:
        reasons.append(RATE_LIMIT)

    dup = find(entries, form["repository"])
    if dup:
        return Decision(False, reasons + [_dup_reason(dup)])

    result = check_repo(form["repository"], gh, now, "submit")
    if not result.exists:
        reasons.append(NOT_FOUND)
    else:
        dup = find(entries, result.canonical)
        if dup:
            return Decision(False, reasons + [_dup_reason(dup)])
        reasons += result.failures

    description, why = clean_description(form["description"])
    if description is None and result.meta.get("description"):
        description = clean_description(result.meta["description"])[0]
    if description is None:
        reasons.append(why)

    if reasons:
        return Decision(False, reasons)
    return Decision(True, [], {
        "repo": result.canonical,
        "category": form["category"],
        "description": description,
        "submitted_by": login,
        "submitted_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issue": issue["number"],
        "status": "live",
        "warned_at": None,
        "superseded_by": None,
        "added_by_seed": False,
    })


def comment(result: dict, kind: str) -> str:
    """Only fixed strings and the normalised owner/name; never text from the issue."""
    if kind == "accepted":
        return (
            f"🎉 `{result['entry']['repo']}` is on the list. It's live at {SITE}, "
            "and Likes start counting as soon as its Discussion opens.\n"
        )
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


def _decide(args) -> int:
    with open(args.event, encoding="utf-8") as f:
        issue = json.load(f)["issue"]
    excluded = {u.strip() for u in os.environ.get("EXCLUDED_USERS", "").split(",") if u.strip()}
    gh = GitHub(os.environ["GITHUB_TOKEN"])
    d = decide(issue, gh, datetime.now(timezone.utc), load(), excluded)
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
    entries.append(result["entry"])
    save(entries, args.entries)
    errors = validate(load(args.entries))
    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        return 1
    print(f"submit/{result['entry']['issue']}")
    return 0


def _comment(args) -> int:
    with open(args.result, encoding="utf-8") as f:
        sys.stdout.write(comment(json.load(f), args.kind))
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.submit")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--event", required=True)
    d.add_argument("--out", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--result", required=True)
    a.add_argument("--entries", default=DEFAULT_PATH)
    c = sub.add_parser("comment")
    c.add_argument("--result", required=True)
    c.add_argument("--kind", required=True, choices=("accepted", "rejected", "pr"))
    args = p.parse_args(argv)
    return {"decide": _decide, "apply": _apply, "comment": _comment}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
