"""Count trusted Likes per entry, rank entries, flag Like bursts and write the results to stats.

python -m scripts.sync_likes   (needs GITHUB_TOKEN; EXCLUDED_USERS is optional)
"""

import copy
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone

from scripts.devstream import entries as entries_mod
from scripts.devstream.entries import normalize_repo
from scripts.devstream.gh import GitHub, GitHubError
from scripts.devstream.likes import (
    YOUNG_DAYS,
    count_likes,
    detect_burst,
    detect_drip,
    fetch_meta,
    fetch_reactors,
    rank_entries,
    young_count,
)
from scripts.devstream.stats import API, ensure_branch, read_json, write_json
from scripts.devstream.trust import TrustCache, ensure_block_access

# Flagged entries stay listed and ranked; only Trending hides them.
RANKED = {"live", "warned", "flagged"}
DAY = timedelta(hours=24)
WEEK = timedelta(days=7)
ISSUE_CAP = 5
LABELS = ["report", "needs-review"]
# How long a reviewed (closed) issue holds off a new one for the same entry.
COOLDOWN = {"burst": DAY, "drip": WEEK}


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


def _body(kind: str, repo: str, total: int, young: int) -> str:
    # Fixed text and counts only: naming reactors would ping them.
    if kind == "burst":
        return (
            f"{repo} picked up {total} Likes in the last 24 hours, and {young} of them came from "
            f"accounts under {YOUNG_DAYS} days old. That's what sock-puppet voting usually looks like, "
            "so we've flagged the entry and opened this for a maintainer to check.\n\n"
            "The flag stays on while this issue is open, so close it once you've checked. "
            "The entry stays listed either way."
        )
    return (
        f"{repo} picked up {total} Likes over the last 7 days, and {young} of them came from "
        f"accounts under {YOUNG_DAYS} days old. That's slower than a burst, but it's the same "
        "pattern spread thin, so it's worth a maintainer's look.\n\n"
        "Nothing is flagged for this one. Close the issue once it's been checked."
    )


def _aggregate(listed: list[tuple[str, str]]) -> dict:
    n = len(listed)
    lines = "\n".join(f"- {key} ({kind})" for key, kind in sorted(listed))
    return {
        "title": f"Like burst: {n} {'entry' if n == 1 else 'entries'}",
        "body": (
            "More entries showed a suspicious Like pattern than we open separate issues for, "
            f"so they share this one:\n\n{lines}\n\n"
            "Bursts stay flagged while this is open. Close it once they've all been checked."
        ),
    }


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

    meta = fetch_meta(gh, keys)
    reactors = fetch_reactors(gh, sorted({mapping[k] for k in keys if k in mapping}))
    prev_likes, likes_sha = read_json(gh, "likes.json")
    prev_flags, flags_sha = read_json(gh, "flags.json")
    flags = copy.deepcopy(prev_flags or {})
    _review(gh, flags, now)

    cache_data, cache_sha = read_json(gh, "trust-cache.json")
    cache = _load_cache(cache_data)
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
                candidates.append(("burst", k, repo, _body("burst", repo, len(today), young_count(today, ages))))
        elif detect_drip(week, ages) and _due(flags, k, "drip", now):
            candidates.append(("drip", k, repo, _body("drip", repo, len(week), young_count(week, ages))))
    candidates.sort(key=lambda c: c[0] != "burst")

    ranks = rank_entries(likes, {k: m["stars"] for k, m in meta.items()})
    old = read_json(gh, f"snapshots/{now - WEEK:%Y-%m-%d}.json")[0] or {}
    out = {}
    for k in keys:
        o = old.get(k)
        out[k] = {
            "likes": likes[k],
            "delta_7d": likes[k] - o["likes"] if o else 0,
            "rank": ranks[k],
            "rank_delta_7d": o["rank"] - ranks[k] if o else 0,
            "flagged": ranked[k]["status"] == "flagged" or k in bursts or _under_review(flags, k),
        }

    prev_meta, meta_sha = read_json(gh, "meta.json")
    snap_path = f"snapshots/{now:%Y-%m-%d}.json"
    snap_sha = read_json(gh, snap_path)[1]

    if candidates:
        try:
            _report(gh, candidates, flags, now)
        except BaseException:
            # Without the records of issues already opened, the next run would open them again.
            if flags != (prev_flags or {}):
                try:
                    write_json(gh, "flags.json", flags, flags_sha, "chore(stats): record Like reviews")
                except Exception:
                    logging.exception("could not write flags.json")
            raise

    writes = [
        ("meta.json", meta, prev_meta, meta_sha, "chore(stats): update repo meta"),
        ("likes.json", out, prev_likes, likes_sha, "chore(stats): update likes"),
        ("flags.json", flags, prev_flags or {}, flags_sha, "chore(stats): record Like reviews"),
    ]
    if snap_sha is None:
        snap = {k: {"likes": v["likes"], "rank": v["rank"]} for k, v in out.items()}
        writes.append((snap_path, snap, None, None, f"chore(stats): snapshot likes for {now:%Y-%m-%d}"))
    writes.append(("trust-cache.json", cache.entries, cache_data, cache_sha, "chore(stats): update trust cache"))
    for path, data, before, sha, message in writes:
        if data != before:
            write_json(gh, path, data, sha, message)


def main():
    excluded = {u.strip().lower() for u in os.environ.get("EXCLUDED_USERS", "").split(",") if u.strip()}
    run(GitHub(os.environ["GITHUB_TOKEN"]), entries_mod.load(), datetime.now(timezone.utc), excluded)


if __name__ == "__main__":
    main()
