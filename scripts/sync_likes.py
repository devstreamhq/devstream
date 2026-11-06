"""Count trusted Likes per entry, rank entries, flag Like bursts and write the results to stats.

python -m scripts.sync_likes   (needs GITHUB_TOKEN; EXCLUDED_USERS is optional)
"""

import copy
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

from scripts.devstream import entries as entries_mod
from scripts.devstream.entries import normalize_repo
from scripts.devstream.gh import GitHub, GitHubError
from scripts.devstream.likes import (
    count_likes,
    detect_burst,
    detect_drip,
    fetch_meta,
    fetch_reactors,
    rank_entries,
)
from scripts.devstream.stats import API, delete_file, ensure_branch, list_dir, read_json, update_json
from scripts.devstream.trust import TrustCache, ensure_block_access

# Flagged entries stay listed and ranked; only Trending hides them.
RANKED = {"live", "warned", "flagged"}
DAY = timedelta(hours=24)
WEEK = timedelta(days=7)
ISSUE_CAP = 5
LABELS = ["report", "needs-review"]
# How long a reviewed (closed) issue holds off a new one for the same entry.
COOLDOWN = {"burst": DAY, "drip": WEEK}
SNAPSHOTS_KEPT = 30
# A rename's finish normally follows its merge within minutes; past this, sync rather than freeze the board.
RENAME_WAIT = timedelta(hours=3)  # timed from likes.json's last commit, which a quiet board may have aged past 3h already


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_cache(data) -> TrustCache:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "trust-cache.json")
        with open(path, "w") as f:
            json.dump(data or {}, f)
        return TrustCache(path)


def _review(gh, flags: dict, now: datetime) -> None:
    """Mark records reviewed once their issue is closed or gone.

    Only numbers the bot opened are stored, so nobody can forge a record by
    opening an issue with the right title.
    """
    pending = {r["issue"] for recs in flags.values() for r in recs.values() if r["reviewed_at"] is None}
    done = set()
    for n in sorted(pending):
        try:
            issue = gh.get(f"{API}/issues/{n}")
        except GitHubError as e:
            if e.status != 410:  # 410: deleted
                raise
            issue = None
        if issue is None or issue.get("state") == "closed":
            done.add(n)
    for recs in flags.values():
        for r in recs.values():
            if r["reviewed_at"] is None and r["issue"] in done:
                r["reviewed_at"] = _iso(now)


def _due(flags: dict, key: str, kind: str, now: datetime) -> bool:
    rec = flags.get(key, {}).get(kind)
    if rec is None:
        return True
    if rec["reviewed_at"] is None:
        return False
    return now - _parse(rec["reviewed_at"]) >= COOLDOWN[kind]


def _record(kind: str, issue: int, now: datetime, aggregate: bool = False) -> dict:
    rec = {"kind": kind, "issue": issue, "opened_at": _iso(now), "reviewed_at": None}
    if aggregate:
        rec["aggregate"] = True
    return rec


def _body(kind: str, repo: str) -> str:
    # Fixed text only: naming reactors would ping them, and numbers or windows would show how to tune around the checks.
    if kind == "burst":
        return (
            "Likes on this entry jumped quickly, and an unusual share came from newer accounts. "
            "It's held out of Trending until a maintainer closes this issue."
        )
    return (
        "A steady stream of Likes on this entry came mostly from newer accounts. "
        "Nothing is hidden; this is for a maintainer to look at."
    )


def _aggregate(listed: list[tuple[str, str]]) -> dict:
    n = len(listed)
    lines = "\n".join(f"- {key}" for key, _ in sorted(listed))
    return {
        "title": f"Like burst: {n} {'entry' if n == 1 else 'entries'}",
        "body": f"Too many entries tripped the Like checks for one issue each, so they share this one:\n\n{lines}",
    }


def _snapshot_date(name: str):
    m = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2})\.json", name)
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date() if m else None
    except ValueError:
        return None


def _baseline(gh, now: datetime, renames: dict) -> dict | None:
    """The snapshot from 7 days ago, else the oldest one between 1 and 7 days old, else None.

    Keys are moved through renames.json, so a renamed entry is compared with its own past.
    """
    week, day = (now - WEEK).date(), (now - DAY).date()
    dates = sorted(d for n in list_dir(gh, "snapshots") if (d := _snapshot_date(n)))
    old = [d for d in dates if week < d <= day]
    pick = week if week in dates else (old[0] if old else None)
    if pick is None:
        return None
    data = read_json(gh, f"snapshots/{pick:%Y-%m-%d}.json")[0]
    if data is None:
        # Listed but unreadable: fail closed rather than read it as an empty baseline.
        raise GitHubError(404, f"snapshot {pick} is listed but could not be read")
    # Oldest first, so a->b then b->c lands on c. A rename before the snapshot is already in it.
    for old, r in sorted(renames.items(), key=lambda kv: kv[1]["at"]):
        if _parse(r["at"]).date() >= pick and old in data:
            data[r["to"]] = data.pop(old)
    return data


def _report(gh, todo: list[tuple[str, str, str, str]], flags: dict, now: datetime) -> None:
    """todo is (kind, key, repo, body). Records are set right after each post, so a failure keeps them."""
    for kind, key, repo, body in todo[:ISSUE_CAP]:
        n = gh.post(f"{API}/issues", {"title": f"Like {kind}: {repo}", "body": body, "labels": LABELS})["number"]
        flags.setdefault(key, {})[kind] = _record(kind, n, now)
    overflow = [(key, kind) for kind, key, _, _ in todo[ISSUE_CAP:]]
    if not overflow:
        return
    # A flood of decoys must not become a flood of issues, nor let the target slip its flag.
    agg = next((r["issue"] for recs in flags.values() for r in recs.values()
                if r.get("aggregate") and r["reviewed_at"] is None), None)
    if agg is None:
        agg = gh.post(f"{API}/issues", {**_aggregate(overflow), "labels": LABELS})["number"]
        for key, kind in overflow:
            flags.setdefault(key, {})[kind] = _record(kind, agg, now, aggregate=True)
        return
    for key, kind in overflow:
        flags.setdefault(key, {})[kind] = _record(kind, agg, now, aggregate=True)
    listed = [(k, kind) for k, recs in flags.items() for kind, r in recs.items()
              if r["issue"] == agg and r["reviewed_at"] is None]
    gh.patch(f"{API}/issues/{agg}", _aggregate(listed))


def _merge_flags(flags: dict, before: dict):
    """A mutate for update_json: lay this run's new and changed records over the stored copy.

    Re-read on a 409, so records another writer stored survive, and so do the issue numbers
    this run opened: losing one would open the same issue again next hour.
    """
    def mutate(cur):
        for k, recs in flags.items():
            for kind, r in recs.items():
                if before.get(k, {}).get(kind) != r:
                    cur.setdefault(k, {})[kind] = r
        return cur
    return mutate


def _rename_in_flight(gh, now, entries, keys, mapping, prev_likes) -> bool:
    """True while a rename has merged but rot's finish hasn't moved discussions.json yet.

    The tell: a listed key has no thread, and a key that was ranked last time has a thread but is no
    longer listed. An entry removed for good drops out of likes.json, so it can't hold the board.
    """
    listed = {normalize_repo(e["repo"]) for e in entries}
    moved = [k for k in mapping if k not in listed and k in prev_likes]
    if not moved or all(k in mapping for k in keys):
        return False
    got = gh.get(f"{API}/commits", {"path": "likes.json", "sha": "stats", "per_page": 1}) or []
    at = got[0]["commit"]["committer"]["date"] if got else None
    if at is not None and now - _parse(at) <= RENAME_WAIT:
        logging.warning("a rename of %s is half done; skipping this sync", ", ".join(moved))
        return True
    logging.error("%s still mapped but unlisted after %s; syncing anyway", ", ".join(moved), RENAME_WAIT)
    return False


def _prune_snapshots(gh, today: str) -> None:
    names = sorted({n for n in list_dir(gh, "snapshots") if _snapshot_date(n)} | {today})
    for n in names[:-SNAPSHOTS_KEPT]:
        delete_file(gh, f"snapshots/{n}", f"chore(stats): drop snapshot {n.removesuffix('.json')}")


def _under_review(flags: dict, key: str) -> bool:
    # Closing the review issue is what clears a flag the burst window has outlived. Drips never flag.
    rec = flags.get(key, {}).get("burst")
    return rec is not None and rec["reviewed_at"] is None


def run(gh, entries, now: datetime, excluded: set[str]) -> None:
    """Every read comes before any issue or file is written, so a failed read publishes nothing."""
    ensure_branch(gh)
    mapping = {normalize_repo(k): v for k, v in (read_json(gh, "discussions.json")[0] or {}).items()}
    ranked = {}
    for e in entries:
        if e["status"] in RANKED:
            ranked.setdefault(normalize_repo(e["repo"]), e)
    keys = sorted(ranked)
    prev_likes = read_json(gh, "likes.json")[0]
    if _rename_in_flight(gh, now, entries, keys, mapping, prev_likes or {}):
        return

    prev_meta = read_json(gh, "meta.json")[0]
    # A repo GitHub hides from us this hour keeps its last card rather than losing stars and avatar.
    listed = set(keys)
    meta = {k: v for k, v in (prev_meta or {}).items() if k in listed} | fetch_meta(gh, keys)
    reactors = fetch_reactors(gh, sorted({mapping[k] for k in keys if k in mapping}))
    prev_flags = read_json(gh, "flags.json")[0]
    flags = copy.deepcopy(prev_flags or {})
    _review(gh, flags, now)

    cache = _load_cache(read_json(gh, "trust-cache.json")[0])
    ensure_block_access(gh)
    seen = {}

    def trusted(login):
        # One check per run; the cache re-checks blocks on every call otherwise.
        if login not in seen:
            seen[login] = cache.trusted(login, gh, now, excluded)
        return seen[login]

    likes, bursts, candidates = {}, set(), []
    for k in keys:
        repo = ranked[k]["repo"]
        nodes = reactors.get(mapping.get(k), [])
        if nodes is None:
            # A missing thread must not wipe the count; rot or a re-map sorts it out.
            likes[k] = ((prev_likes or {}).get(k) or {}).get("likes", 0)
            logging.warning("Discussion %s for %s not found; keeping %d Likes", mapping[k], repo, likes[k])
            continue
        users = {n["user"]["login"].lower(): n for n in nodes if n.get("user")}
        likes[k] = count_likes([n["user"]["login"] for n in users.values()], trusted)
        # Only Likes that count can flag: throwaway accounts can't move the ranking, so they can't flag it.
        counted = [n for n in users.values() if trusted(n["user"]["login"])]
        ages = {n["user"]["login"]: (now - _parse(n["user"]["createdAt"])).days
                for n in counted if n["user"].get("createdAt")}
        dated = [(n["user"]["login"], _parse(n["createdAt"])) for n in counted]
        today = [r for r in dated if now - r[1] <= DAY]
        week = [r for r in dated if now - r[1] <= WEEK]
        if detect_burst(today, ages):
            bursts.add(k)
            if _due(flags, k, "burst", now):
                candidates.append(("burst", k, repo, _body("burst", repo)))
        elif detect_drip(week, ages) and _due(flags, k, "drip", now):
            candidates.append(("drip", k, repo, _body("drip", repo)))
    candidates.sort(key=lambda c: c[0] != "burst")

    ranks = rank_entries(likes, {k: m["stars"] for k, m in meta.items()})
    old = _baseline(gh, now, read_json(gh, "renames.json")[0] or {})
    out = {}
    for k in keys:
        o = (old or {}).get(k)
        base = (o["likes"] if o else 0) if old is not None else likes[k]
        out[k] = {
            "likes": likes[k],
            "delta_7d": likes[k] - base,
            "rank": ranks[k],
            "rank_delta_7d": o["rank"] - ranks[k] if o else 0,
            "flagged": ranked[k]["status"] == "flagged" or k in bursts or _under_review(flags, k),
        }

    snap_path = f"snapshots/{now:%Y-%m-%d}.json"
    flags_write = ("flags.json", _merge_flags(flags, prev_flags or {}), "chore(stats): record Like reviews")

    if candidates:
        try:
            _report(gh, candidates, flags, now)
        except BaseException:
            # Without the records of issues already opened, the next run would open them again.
            if flags != (prev_flags or {}):
                try:
                    update_json(gh, *flags_write)
                except Exception:
                    logging.exception("could not write flags.json")
            raise

    snap = {k: {"likes": v["likes"], "rank": v["rank"]} for k, v in out.items()}
    # Each is re-applied onto a fresh read if another writer 409s it first.
    update_json(gh, *flags_write)  # first, so a later failed write can't lose the record of issues opened
    # Built from the file as it was read; if someone else changed it since, theirs stands until next hour.
    update_json(gh, "meta.json", lambda cur: meta if cur == (prev_meta or {}) else cur, "chore(stats): update repo meta")
    update_json(gh, "likes.json", lambda cur: out if cur == (prev_likes or {}) else cur, "chore(stats): update likes")
    update_json(gh, snap_path, lambda cur: cur or snap, f"chore(stats): snapshot likes for {now:%Y-%m-%d}")
    # Past the TTL a record is never read again, and unpruned the file passes 1 MB.
    fresh = {k: v for k, v in cache.entries.items() if cache.get(k, now) is not None}
    update_json(gh, "trust-cache.json", lambda _: fresh, "chore(stats): update trust cache", compact=True)
    _prune_snapshots(gh, snap_path.removeprefix("snapshots/"))

def main():
    excluded = {u.strip().lower() for u in os.environ.get("EXCLUDED_USERS", "").split(",") if u.strip()}
    run(GitHub(os.environ["GITHUB_TOKEN"]), entries_mod.load(), datetime.now(timezone.utc), excluded)


if __name__ == "__main__":
    main()
