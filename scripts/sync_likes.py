"""Count trusted Likes per entry, rank entries, flag Like bursts and write the results to stats.

python -m scripts.sync_likes   (needs GITHUB_TOKEN; EXCLUDED_USERS is optional)
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

from scripts.devstream import entries as entries_mod
from scripts.devstream.entries import normalize_repo
from scripts.devstream.gh import GitHub
from scripts.devstream.likes import YOUNG_DAYS, count_likes, detect_burst, fetch_meta, fetch_reactors, rank_entries
from scripts.devstream.stats import API, ensure_branch, read_json, write_json
from scripts.devstream.trust import TrustCache, ensure_block_access

RANKED = {"live", "warned", "flagged"}
WINDOW = timedelta(hours=24)


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _load_cache(data) -> TrustCache:
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "trust-cache.json")
        with open(path, "w") as f:
            json.dump(data or {}, f)
        return TrustCache(path)


def _report(gh, bursts: dict[str, tuple[str, int, int]]) -> None:
    # The issues list, not search: search lags behind and would let a duplicate through.
    titles = {
        i["title"]
        for i in gh.paginate(f"{API}/issues?state=open&labels=report&per_page=100")
        if "pull_request" not in i
    }
    for repo, total, young in bursts.values():
        title = f"Like burst: {repo}"
        if title in titles:
            continue
        # Fixed text and counts only: naming reactors would ping them.
        body = (
            f"{repo} picked up {total} Likes in the last 24 hours, and {young} of them came from "
            f"accounts under {YOUNG_DAYS} days old. That's what sock-puppet voting usually looks like, "
            "so we've flagged the entry and opened this for a maintainer to check.\n\n"
            "Only Likes from trusted accounts ever count toward the ranking. The entry stays listed "
            "while this is open."
        )
        gh.post(f"{API}/issues", {"title": title, "body": body, "labels": ["report", "needs-review"]})


def run(gh, entries, now: datetime, excluded: set[str]) -> None:
    """All reads come first, so an API error aborts before anything is written."""
    ensure_branch(gh)
    mapping = {normalize_repo(k): v for k, v in (read_json(gh, "discussions.json")[0] or {}).items()}
    ranked = {}
    for e in entries:
        if e["status"] in RANKED:
            ranked.setdefault(normalize_repo(e["repo"]), e)
    keys = sorted(ranked)

    meta = fetch_meta(gh, keys)
    reactors = fetch_reactors(gh, sorted({mapping[k] for k in keys if k in mapping}))

    cache_data, cache_sha = read_json(gh, "trust-cache.json")
    cache = _load_cache(cache_data)
    ensure_block_access(gh)
    seen = {}

    def trusted(login):
        # One check per run; the cache re-checks blocks on every call otherwise.
        if login not in seen:
            seen[login] = cache.trusted(login, gh, now, excluded)
        return seen[login]

    likes, bursts = {}, {}
    for k in keys:
        nodes = [n for n in reactors.get(mapping.get(k), []) if n.get("user")]
        likes[k] = count_likes([n["user"]["login"] for n in nodes], trusted)
        # Untrusted reactors count here: they are what an attack looks like.
        today = [(n["user"]["login"], _parse(n["createdAt"])) for n in nodes
                 if now - _parse(n["createdAt"]) <= WINDOW]
        ages = {n["user"]["login"]: (now - _parse(n["user"]["createdAt"])).days
                for n in nodes if n["user"].get("createdAt")}
        if detect_burst(today, ages):
            young = sum(1 for login, _ in today if ages.get(login, 0) < YOUNG_DAYS)
            bursts[k] = (ranked[k]["repo"], len(today), young)

    ranks = rank_entries(likes, {k: m["stars"] for k, m in meta.items()})
    old = read_json(gh, f"snapshots/{now - timedelta(days=7):%Y-%m-%d}.json")[0] or {}
    out = {}
    for k in keys:
        o = old.get(k)
        out[k] = {
            "likes": likes[k],
            "delta_7d": likes[k] - o["likes"] if o else 0,
            "rank": ranks[k],
            "rank_delta_7d": o["rank"] - ranks[k] if o else 0,
            "flagged": ranked[k]["status"] == "flagged" or k in bursts,
        }

    prev_meta, meta_sha = read_json(gh, "meta.json")
    prev_likes, likes_sha = read_json(gh, "likes.json")
    snap_path = f"snapshots/{now:%Y-%m-%d}.json"
    snap_sha = read_json(gh, snap_path)[1]

    was_flagged = {k for k, v in (prev_likes or {}).items() if v.get("flagged")}
    new_bursts = {k: v for k, v in bursts.items() if k not in was_flagged}
    if new_bursts:
        _report(gh, new_bursts)

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
