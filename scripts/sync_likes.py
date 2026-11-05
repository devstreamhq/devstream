"""Count trusted Likes per entry, rank entries, flag Like bursts and write the results to stats.

python -m scripts.sync_likes   (needs GITHUB_TOKEN; EXCLUDED_USERS is optional)
"""

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

from scripts.devstream import entries as entries_mod
from scripts.devstream.entries import normalize_repo
from scripts.devstream.gh import GitHub
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
AGGREGATE = re.compile(r"Like burst: \d+ entr(?:y|ies)")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load_cache(data) -> TrustCache:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "trust-cache.json")
        with open(path, "w") as f:
            json.dump(data or {}, f)
        return TrustCache(path)


def _open_report_titles(gh) -> set[str]:
    # The issues list, not search: search lags behind and would let a duplicate through.
    return {
        i["title"]
        for i in gh.paginate(f"{API}/issues?state=open&labels=report&per_page=100")
        if "pull_request" not in i
    }


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


def _aggregate_body(overflow: list[tuple[str, str]]) -> str:
    lines = "\n".join(f"- {repo} ({kind})" for repo, kind in overflow)
    return (
        "More entries showed a suspicious Like pattern this run than we open separate issues for. "
        f"These are the ones without their own issue yet:\n\n{lines}\n\n"
        "Each gets its own issue on a later run once the queue clears. Bursts are already flagged."
    )


def _report(gh, candidates: list[tuple[str, str, str]], open_titles: set[str]) -> None:
    """candidates are (kind, repo, body); at most ISSUE_CAP issues open, the rest go in one aggregate."""
    todo = [c for c in candidates if f"Like {c[0]}: {c[1]}" not in open_titles]
    for kind, repo, body in todo[:ISSUE_CAP]:
        gh.post(f"{API}/issues", {"title": f"Like {kind}: {repo}", "body": body, "labels": LABELS})
    overflow = [(repo, kind) for kind, repo, _ in todo[ISSUE_CAP:]]
    # A flood of throwaway entries must not turn into a flood of issues.
    if overflow and not any(AGGREGATE.fullmatch(t) for t in open_titles):
        n = len(overflow)
        title = f"Like burst: {n} {'entry' if n == 1 else 'entries'}"
        gh.post(f"{API}/issues", {"title": title, "body": _aggregate_body(overflow), "labels": LABELS})


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
    open_titles = _open_report_titles(gh)

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
            candidates.append(("burst", repo, _body("burst", repo, len(today), young_count(today, ages))))
        elif detect_drip(week, ages):
            candidates.append(("drip", repo, _body("drip", repo, len(week), young_count(week, ages))))
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
            # Closing the review issue is what clears a flag the burst window has outlived.
            "flagged": ranked[k]["status"] == "flagged"
            or k in bursts
            or f"Like burst: {ranked[k]['repo']}" in open_titles,
        }

    prev_meta, meta_sha = read_json(gh, "meta.json")
    snap_path = f"snapshots/{now:%Y-%m-%d}.json"
    snap_sha = read_json(gh, snap_path)[1]

    if candidates:
        _report(gh, candidates, open_titles)

    writes = [
        ("meta.json", meta, prev_meta, meta_sha, "chore(stats): update repo meta"),
        ("likes.json", out, prev_likes, likes_sha, "chore(stats): update likes"),
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
