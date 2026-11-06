"""Weekly health check: warn, archive, revive, rename or remove listed repos.

python -m scripts.rot plan --out plan.json   (prints changed=true|false; REPO_INPUT limits it to one entry)
python -m scripts.rot body --plan plan.json
python -m scripts.rot finish --plan plan.json   (only after the entries PR has merged)
python -m scripts.rot review --plan plan.json   (opens needs-review issues for entries undecided 3 weeks running)
"""

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from scripts.devstream.discussions import REMOVED_PREFIX
from scripts.devstream.entries import DEFAULT_PATH, find, load, normalize_repo, save, sort_key
from scripts.devstream.gh import GitHub, GitHubError
from scripts.devstream.license_detect import extra_licenses_for
from scripts.devstream.quality import (
    MSG_GONE,
    RepoResult,
    MSG_LICENSE,
    MSG_NO_README,
    MSG_SHORT_README,
    Undecided,
    check_repo,
    get_repo,
)
from scripts.devstream.stats import ensure_branch, read_json, update_json
from scripts.validate_data import validate

CHECKED = {"live", "warned", "archived"}
GRACE = timedelta(days=30)
# A second sighting this much later confirms a removal, so a blip never deletes an entry.
CONFIRM = timedelta(days=6)
# An older sighting is a different episode, so it starts the week of grace again.
STALE = timedelta(days=14)
RENAME_KEEP = timedelta(days=8)
MSG_REPLACED = "A different repo now lives at this name, not the one that was listed."
REMOVAL_REASONS = (MSG_GONE, MSG_REPLACED, MSG_LICENSE, MSG_NO_README, MSG_SHORT_README)
REPO_INPUT = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}")
API = "/repos/devstreamhq/devstream"
BOT = "devstreamhq-bot[bot]"
# Full runs in a row that couldn't decide on an entry before a maintainer is asked to look.
UNDECIDED_WEEKS = 3
REVIEW_TITLE = "Health check can't decide on {repo}"
REVIEW_BODY = (
    "The weekly health check couldn't decide on `{repo}` for {weeks} weeks running, "
    "so it isn't being warned or removed. Could someone take a look?\n\nLast error: `{error}`\n"
)
REPLACED_TITLE = "A different repo now answers to {repo}"
REPLACED_BODY = (
    "`{repo}` was listed as GitHub repo {want}, but that name now belongs to repo {got}. "
    "Usually that means the owner renamed or deleted their account and someone else took the name.\n\n"
    "The entry has a removal notice, and the hourly Likes sync keeps its old card and holds it out of Trending. "
    "It still links to that name until it comes off the list, "
    "so could someone check which repo is the real one before then?\n"
)

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
    "If that's fixed before {date}, it stays listed. Otherwise it comes off at our first check from then on.\n"
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
LABELS = {"drop": "Removed as a duplicate", "notice": "Removal notice", "warn": "Warned", "archive": "Archived", "revive": "Back on the list", "remove": "Removed"}

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
    kind: str  # notice | warn | archive | revive | rename | remove | drop (lost a rename collision)
    repo: str  # the name after this run
    message: str  # Discussion comment; empty for a rename
    key: str  # stats key at the start of the run, which finds the Discussion


# Dates people read are in US Central, like the site; stored times stay UTC.
CENTRAL = ZoneInfo("America/Chicago")


def _day(d: datetime) -> str:
    d = d.astimezone(CENTRAL)
    return f"{d.day} {d:%B %Y}"


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
        # The clock only runs once the owner has been told, and only for a recent sighting.
        if rec is None or not rec.get("notified") or now - _parse(rec["first_seen"]) > STALE:
            rot[key] = {"first_seen": _iso(now), "reason": " ".join(r.failures)}
            reasons = [f for f in r.failures if f in REMOVAL_REASONS] or r.failures
            date = now + CONFIRM
            msg = NOTICE.format(repo=repo, date=_day(date), reasons=_bullets(reasons))
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
            msg = WARN.format(repo=repo, date=_day(date), reasons=_bullets(r.failures))
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
                    actions.append(Action("drop", g["repo"], MERGED.format(repo=g["repo"], kept=kept), k))
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
    """Check, plan and write the new entries.json locally. Any other GitHubError aborts before a write."""
    entries = load(path)
    targets = [e for e in select(entries, repo) if e["status"] in CHECKED]
    ensure_branch(gh)
    rot_state = read_json(gh, "rot.json")[0]
    rot_state = rot_state or {}
    results = {}
    undecided = {}
    replaced = []
    for e in targets:
        try:
            r = check(e["repo"], gh, now, "rot", allowed_extra=extra_licenses_for(e["category"]))
            if e.get("repo_id") is None:
                logging.warning("%s has no repo_id, so a replaced repo can't be caught", e["repo"])
            elif r.exists and r.repo_id is not None and r.repo_id != e["repo_id"]:
                got = r.repo_id
                # The owner may have moved (an account rename) and someone took the old name: follow the ID.
                moved = get_repo(gh, f"/repositories/{e['repo_id']}")
                r = moved and check(moved["full_name"], gh, now, "rot", allowed_extra=extra_licenses_for(e["category"]))
                if not (r and r.exists and r.repo_id == e["repo_id"]):
                    # Someone else's repo at the listed name inherits nothing: it reads as gone, and a person decides.
                    replaced.append({"repo": e["repo"], "kind": "replaced", "want": e["repo_id"], "got": got})
                    r = RepoResult(False, e["repo"], [MSG_REPLACED], "remove")
            results[e["repo"]] = r
        except GitHubError as exc:
            # An owner's IP allow list hides the repo from our token; that says nothing about the repo.
            if not isinstance(exc, Undecided) and not (exc.status == 403 and "IP allow list" in exc.message):
                raise
            logging.warning("skipping %s: %s", e["repo"], exc)
            undecided[normalize_repo(e["repo"])] = str(exc)
    review = []
    if repo is None:
        try:
            review = _track_undecided(gh, now, {normalize_repo(e["repo"]) for e in targets}, undecided)
        except Exception:
            # The weeks are only a review prompt; failing to record them mustn't stop the check.
            logging.exception("could not record undecided repos")
    review += replaced
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
        update_json(gh, "rot.json", lambda _: rot, "chore(stats): record failing repos")
        # Unchanged entries means the only actions are removal notices, safe to post now rot.json is saved.
        failed, notified = _post(gh, actions, read_json(gh, "discussions.json")[0] or {})
        failed += _mark_notified(gh, notified, rot)
        if failed:
            raise SystemExit(f"{failed} Discussion or stats update(s) failed")
    return {"changed": changed, "actions": [asdict(a) for a in actions], "rot_state": rot, "review": review}


def _track_undecided(gh, now, checked: set[str], undecided: dict[str, str]) -> list[dict]:
    """Count full runs in a row that skipped each entry. Returns the entries due a review issue."""
    def mutate(data):
        data = {k: v for k, v in data.items() if k in checked and k in undecided}
        for k in undecided:
            rec = data.get(k)
            # A retried run in the same week isn't another week.
            if rec is None or now - _parse(rec["at"]) >= CONFIRM:
                data[k] = {"weeks": (rec or {}).get("weeks", 0) + 1, "at": _iso(now)}
        return data

    update_json(gh, "undecided.json", mutate, "chore(stats): record undecided repos")
    data = read_json(gh, "undecided.json")[0] or {}
    return [{"repo": k, "weeks": v["weeks"], "error": " ".join(undecided[k].replace("`", "'").split())[:300]}
            for k, v in sorted(data.items()) if v["weeks"] >= UNDECIDED_WEEKS and k in undecided]


def open_reviews(gh, review: list[dict]) -> None:
    """One needs-review issue per entry, unless the bot already has one open with that title."""
    if not review:
        return
    open_titles = {i["title"] for i in gh.paginate(f"{API}/issues?state=open&labels=needs-review&per_page=100")
                   if (i.get("user") or {}).get("login") == BOT}
    for r in review:
        if not REPO_INPUT.fullmatch(r["repo"]):
            raise ValueError(f"not an owner/name: {r['repo']!r}")
        title, body = (REPLACED_TITLE, REPLACED_BODY) if r.get("kind") == "replaced" else (REVIEW_TITLE, REVIEW_BODY)
        title = title.format(repo=r["repo"])
        if title in open_titles:
            continue
        gh.post(f"{API}/issues", {"title": title, "body": body.format(**r), "labels": ["needs-review"]})


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


def finish(gh, plan: dict, now: datetime | None = None) -> None:
    """Write stats, then comment and retitle. Run only once the entries PR has merged.

    A failed stats write doesn't hold back the comments; the run fails at the end instead.
    """
    actions = [Action(**a) for a in plan["actions"]]
    moves = {a.key: normalize_repo(a.repo) for a in actions
             if a.kind == "rename" and a.key != normalize_repo(a.repo)}
    dropped = {a.key for a in actions if a.kind == "drop"}

    def rekey(data):
        # Drop first: the kept entry may be moving onto the dropped one's key.
        for k in dropped:
            data.pop(k, None)
        return _move(data, moves)

    now = now or datetime.now(timezone.utc)

    def record(data):
        # sync_likes replays these over snapshots taken before the rename; past its 7-day baseline they're unused.
        kept = {k: v for k, v in data.items() if isinstance(v, dict) and now - _parse(v["at"]) < RENAME_KEEP}
        # renames.json keeps one record per old name; a repeat rename replaces it, fine on a weekly schedule.
        # pending: the key move failed, so on_merge replays this record once; a completed move is never replayed.
        mark = {"pending": True} if "discussions.json" in failed else {}
        return {**kept, **{old: {"to": new, "at": _iso(now), **mark} for old, new in moves.items()}}

    numbers = dict(read_json(gh, "discussions.json")[0] or {})  # before the move, so dropped entries find their thread
    failed = set()
    for path, mutate, message in (
        ("rot.json", lambda _: plan["rot_state"], "chore(stats): record failing repos"),
        ("discussions.json", rekey, "chore(stats): follow renamed repos"),
        # After the key move, so record knows whether on_merge has to finish it.
        ("renames.json", record, "chore(stats): record renamed repos"),
        ("flags.json", rekey, "chore(stats): follow renamed repos"),
        # The site build reads these two by key, so they move now rather than at the next hourly sync.
        ("likes.json", rekey, "chore(stats): follow renamed repos"),
        ("meta.json", rekey, "chore(stats): follow renamed repos"),
    ):
        try:
            update_json(gh, path, mutate, message)
        except Exception:
            logging.exception("could not write %s", path)
            failed.add(path)
    # A title must match a key that is stored, or will be once on_merge replays the pending record.
    retitle = {"rename": not {"discussions.json", "renames.json"} <= failed, "drop": "discussions.json" not in failed}
    posted, notified = _post(gh, actions, numbers, retitle)
    total = len(failed) + posted + _mark_notified(gh, notified, plan["rot_state"])
    if total:
        raise SystemExit(f"{total} Discussion or stats update(s) failed")


def _mark_notified(gh, keys: set[str], planned: dict) -> int:
    """Start the removal clock for the records whose owner has now been told. Returns 1 on failure.

    Only a stored record from this plan is marked: if the plan's write failed, the stored one is
    an older sighting, and marking it would start the clock days early.
    """
    if not keys:
        return 0

    def mark(data):
        for k in keys & data.keys():
            if isinstance(data[k], dict) and data[k].get("first_seen") == (planned.get(k) or {}).get("first_seen"):
                data[k]["notified"] = True
        return data

    try:
        update_json(gh, "rot.json", mark, "chore(stats): record removal notices")
    except Exception:
        logging.exception("could not mark removal notices in rot.json")
        return 1
    return 0


def _post(gh, actions, numbers: dict, retitle: dict | None = None) -> tuple[int, set[str]]:
    """Comment on, or retitle, each action's Discussion.

    numbers maps stats key to Discussion number. Returns (failures, rot keys whose notice is done).
    An entry with no thread yet counts as told, or it could never be removed.
    """
    failed, notified = 0, set()
    for a in actions:
        n = numbers.get(a.key)
        if a.kind != "rename" and not a.message:
            continue
        try:
            d = None if n is None else gh.graphql(_ID, {"number": n}, allow_not_found=True)["repository"]["discussion"]
            if d is not None:
                if a.kind != "rename":
                    gh.graphql(_COMMENT, {"discussionId": d["id"], "body": a.message})
                if a.kind in ("rename", "drop") and (retitle or {}).get(a.kind, True):
                    # "Removed: " can never normalise to a listed repo, so on_merge won't adopt the thread.
                    title = a.repo if a.kind == "rename" else f"{REMOVED_PREFIX}{a.repo}"
                    gh.graphql(_RETITLE, {"discussionId": d["id"], "title": title})
            if a.kind == "notice":
                notified.add(normalize_repo(a.repo))
        except Exception:
            # One thread failing mustn't silence the rest; the run still fails at the end.
            logging.exception("could not update Discussion #%s for %s", n, a.repo)
            failed += 1
    return failed, notified


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m scripts.rot")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument("--out", required=True)
    pl.add_argument("--entries", default=DEFAULT_PATH)
    for name in ("body", "finish", "review"):
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
    if args.cmd == "review":
        with open(args.plan, encoding="utf-8") as f:
            open_reviews(gh, json.load(f)["review"])
        return 0
    plan = prepare(gh, datetime.now(timezone.utc), args.entries, os.environ.get("REPO_INPUT") or None)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)
    print(f"changed={'true' if plan['changed'] else 'false'}")
    print(f"review={'true' if plan['review'] else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
