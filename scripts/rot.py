"""Weekly health check: warn, archive, revive, rename or remove listed repos.

python -m scripts.rot plan --out plan.json   (prints changed=true|false; REPO_INPUT limits it to one entry)
python -m scripts.rot body --plan plan.json
python -m scripts.rot finish --plan plan.json   (only after the entries PR has merged)
"""

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from scripts.devstream.entries import DEFAULT_PATH, find, load, normalize_repo, save, sort_key
from scripts.devstream.gh import GitHub
from scripts.devstream.quality import (
    MSG_GONE,
    MSG_LICENSE,
    MSG_NO_README,
    MSG_SHORT_README,
    Undecided,
    check_repo,
)
from scripts.devstream.stats import ensure_branch, read_json, write_json
from scripts.validate_data import validate

CHECKED = {"live", "warned", "archived"}
GRACE = timedelta(days=30)
# A second sighting this much later confirms a removal, so a blip never deletes an entry.
CONFIRM = timedelta(days=6)
REMOVAL_REASONS = (MSG_GONE, MSG_LICENSE, MSG_NO_README, MSG_SHORT_README)
REPO_INPUT = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")

WARN = (
    "Heads up: `{repo}` moves to the archive on {date} unless something changes. "
    "Here's what we spotted:\n\n{reasons}\n\n"
    "Still working on it? Sort that out before then and it stays listed. "
    "If the only problem is a quiet year, a single commit does it.\n"
)
ARCHIVE = (
    "`{repo}` has moved to the archive, since nothing changed in the 30 days after our heads-up. "
    "Nothing's lost, though. The moment it's active again, it comes back to the list on its own.\n"
)
REVIVE = "Welcome back! `{repo}` is active again, so it's back on the list.\n"
NOTICE = (
    "Heads up: we're about to take `{repo}` off the list. Here's what we found:\n\n{reasons}\n\n"
    "If that's fixed by {date}, it stays listed. Otherwise it comes off at our next check after that.\n"
)
REMOVE = (
    "We've taken `{repo}` off the list. Here's why:\n\n{reasons}\n\n"
    "We looked twice, a week apart, before doing this. "
    "If that changes, you're welcome to submit it again.\n"
)
MERGED = (
    "`{repo}` now points to the same repo as `{kept}`, which was listed first, "
    "so we've folded this entry into that one. Likes count on that entry's thread from now on.\n"
)
LABELS = {"notice": "Removal notice", "warn": "Warned", "archive": "Archived", "revive": "Back on the list", "remove": "Removed"}

_ID = """query EntryDiscussionId($number: Int!) {
  repository(owner: "devstreamhq", name: "devstream") { discussion(number: $number) { id } }
}"""

_COMMENT = """mutation CommentOnEntry($discussionId: ID!, $body: String!) {
  addDiscussionComment(input: {discussionId: $discussionId, body: $body}) { comment { id } }
}"""

_RETITLE = """mutation UpdateEntryDiscussion($discussionId: ID!, $title: String!) {
  updateDiscussion(input: {discussionId: $discussionId, title: $title}) { discussion { number } }
}"""


@dataclass
class Action:
    kind: str  # notice | warn | archive | revive | rename | remove
    repo: str  # the name after this run
    message: str  # Discussion comment; empty for a rename
    key: str  # stats key at the start of the run, which finds the Discussion


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _bullets(failures) -> str:
    return "\n".join(f"- {f}" for f in failures)


def _verdict(e, r, now, rot) -> tuple[dict | None, list[Action]]:
    """The entry after this run (None when removed) and what happened to it."""
    old = normalize_repo(e["repo"])
    acts = []
    if r.exists and r.canonical != e["repo"]:
        e = {**e, "repo": r.canonical}
        acts.append(Action("rename", r.canonical, "", old))
        if old in rot:
            rot[normalize_repo(r.canonical)] = rot.pop(old)
    key = normalize_repo(e["repo"])
    repo = e["repo"]

    if r.rot == "remove":
        rec = rot.get(key)
        if rec is None:
            rot[key] = {"first_seen": _iso(now), "reason": " ".join(r.failures)}
            reasons = [f for f in r.failures if f in REMOVAL_REASONS] or r.failures
            date = now + CONFIRM
            msg = NOTICE.format(repo=repo, date=f"{date.day} {date:%B %Y}", reasons=_bullets(reasons))
            acts.append(Action("notice", repo, msg, old))
        elif now - _parse(rec["first_seen"]) >= CONFIRM:
            del rot[key]
            return None, [Action("remove", repo, REMOVE.format(repo=repo, reasons=_bullets(r.failures)), old)]
        return e, acts

    rot.pop(key, None)
    if r.rot == "warn":
        if e["status"] == "live" or (e["status"] == "warned" and not e["warned_at"]):
            e = {**e, "status": "warned", "warned_at": _iso(now)}
            date = now + GRACE
            msg = WARN.format(repo=repo, date=f"{date.day} {date:%B %Y}", reasons=_bullets(r.failures))
            acts.append(Action("warn", repo, msg, old))
        elif e["status"] == "warned" and now - _parse(e["warned_at"]) >= GRACE:
            e = {**e, "status": "archived"}
            acts.append(Action("archive", repo, ARCHIVE.format(repo=repo), old))
    elif e["status"] in ("warned", "archived"):
        e = {**e, "status": "live", "warned_at": None}
        acts.append(Action("revive", repo, REVIVE.format(repo=repo), old))
    return e, acts


def plan_rot(entries, results, now, rot_state):
    """Pure: (new entries, actions, new rot state). An entry with no result is left alone."""
    res = {normalize_repo(k): v for k, v in results.items()}
    listed = {normalize_repo(e["repo"]) for e in entries}
    # A record for a repo no longer listed would let a resubmission skip its week of grace.
    rot = {k: v for k, v in rot_state.items() if k in listed}

    groups: dict[str, list] = {}
    for e in entries:
        r = res.get(normalize_repo(e["repo"]))
        final = r.canonical if r and r.exists else e["repo"]
        groups.setdefault(normalize_repo(final), []).append(e)

    out, actions = [], []
    for group in groups.values():
        if len(group) > 1:
            # Two entries now name one repo. Never let that undo a moderator's hidden or superseded.
            if any(g["status"] not in CHECKED for g in group):
                logging.warning("rename collision with an unchecked entry: %s", [g["repo"] for g in group])
                out += group
                continue
            keep = min(group, key=lambda g: (g["submitted_at"], g["issue"]))
            r = res.get(normalize_repo(keep["repo"]))
            kept = r.canonical if r and r.exists else keep["repo"]
            for g in group:
                if g is not keep:
                    k = normalize_repo(g["repo"])
                    rot.pop(k, None)
                    actions.append(Action("remove", g["repo"], MERGED.format(repo=g["repo"], kept=kept), k))
            group = [keep]
        e = group[0]
        r = res.get(normalize_repo(e["repo"]))
        if r is None:
            out.append(e)
            continue
        e, acts = _verdict(e, r, now, rot)
        actions += acts
        if e is not None:
            out.append(e)
    return sorted(out, key=sort_key), actions, rot


def select(entries, raw):
    """The entries to check: all of them, or the one named by a dispatch input."""
    if raw is None:
        return entries
    if not isinstance(raw, str) or not REPO_INPUT.fullmatch(raw):
        raise SystemExit(f"repo must be owner/name, got {raw!r}")
    e = find(entries, raw)
    return [e] if e else []


def prepare(gh, now, path=DEFAULT_PATH, repo=None, check=check_repo) -> dict:
    """Check, plan and write the new entries.json locally. Any GitHubError aborts before a write."""
    entries = load(path)
    targets = [e for e in select(entries, repo) if e["status"] in CHECKED]
    ensure_branch(gh)
    rot_state, rot_sha = read_json(gh, "rot.json")
    rot_state = rot_state or {}
    results = {}
    for e in targets:
        try:
            results[e["repo"]] = check(e["repo"], gh, now, "rot")
        except Undecided as exc:
            logging.warning("skipping %s: %s", e["repo"], exc)
    new, actions, rot = plan_rot(entries, results, now, rot_state)
    changed = new != sorted(entries, key=sort_key)
    if changed:
        errors = validate(new)
        for err in errors:
            print(err, file=sys.stderr)
        if errors:
            raise SystemExit(1)
        save(new, path)
    elif rot != rot_state:
        write_json(gh, "rot.json", rot, rot_sha, "chore(stats): record failing repos")
        # Unchanged entries means the only actions are removal notices, safe to post now rot.json is saved.
        _post(gh, actions, read_json(gh, "discussions.json")[0] or {})
    return {"changed": changed, "actions": [asdict(a) for a in actions], "rot_state": rot}


def pr_body(actions) -> str:
    """Owner/name only, so nothing a repo owner wrote reaches the PR."""
    lines = []
    for a in actions:
        for name in (a.repo, a.key):
            if not REPO_INPUT.fullmatch(name):
                raise ValueError(f"not an owner/name: {name!r}")
        if a.kind == "rename":
            lines.append(f"- Renamed: `{a.key}` is now `{a.repo}`")
        else:
            lines.append(f"- {LABELS[a.kind]}: `{a.repo}`")
    return "This week's health check:\n\n" + "\n".join(lines) + "\n"


def _move(data: dict, moves: dict[str, str]) -> dict:
    # Pop every old key first, so a chain like a->b, b->c can't carry a's value on to c.
    out = dict(data)
    popped = {new: out.pop(old) for old, new in moves.items() if old in out}
    out.update(popped)
    return out


def finish(gh, plan: dict) -> None:
    """Write stats, then comment and retitle. Run only once the entries PR has merged."""
    actions = [Action(**a) for a in plan["actions"]]
    moves = {a.key: normalize_repo(a.repo) for a in actions
             if a.kind == "rename" and a.key != normalize_repo(a.repo)}

    cur, sha = read_json(gh, "rot.json")
    if plan["rot_state"] != (cur or {}):
        write_json(gh, "rot.json", plan["rot_state"], sha, "chore(stats): record failing repos")
    discussions, dsha = read_json(gh, "discussions.json")
    numbers = dict(discussions or {})  # before the move, so a removed entry still finds its thread
    for path, data, fsha in (("discussions.json", discussions, dsha), ("flags.json", *read_json(gh, "flags.json"))):
        moved = _move(data or {}, moves)
        if moved != (data or {}):
            write_json(gh, path, moved, fsha, "chore(stats): follow renamed repos")

    _post(gh, actions, numbers)


def _post(gh, actions, numbers: dict) -> None:
    """Comment on, or retitle, each action's Discussion. numbers maps stats key to Discussion number."""
    failed = 0
    for a in actions:
        n = numbers.get(a.key)
        if n is None or (a.kind != "rename" and not a.message):
            continue
        try:
            d = gh.graphql(_ID, {"number": n}, allow_not_found=True)["repository"]["discussion"]
            if d is None:
                continue
            if a.kind == "rename":
                gh.graphql(_RETITLE, {"discussionId": d["id"], "title": a.repo})
            else:
                gh.graphql(_COMMENT, {"discussionId": d["id"], "body": a.message})
        except Exception:
            # One thread failing mustn't silence the rest; the run still fails below.
            logging.exception("could not update Discussion #%s for %s", n, a.repo)
            failed += 1
    if failed:
        raise SystemExit(f"{failed} Discussion update(s) failed")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.rot")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--out", required=True)
    pl.add_argument("--entries", default=DEFAULT_PATH)
    for name in ("body", "finish"):
        sub.add_parser(name).add_argument("--plan", required=True)
    args = p.parse_args(argv)

    if args.cmd == "body":
        with open(args.plan, encoding="utf-8") as f:
            sys.stdout.write(pr_body([Action(**a) for a in json.load(f)["actions"]]))
        return 0
    gh = GitHub(os.environ["GITHUB_TOKEN"])
    if args.cmd == "finish":
        with open(args.plan, encoding="utf-8") as f:
            finish(gh, json.load(f))
        return 0
    plan = prepare(gh, datetime.now(timezone.utc), args.entries, os.environ.get("REPO_INPUT") or None)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)
    print(f"changed={'true' if plan['changed'] else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
