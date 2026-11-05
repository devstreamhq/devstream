#!/usr/bin/env bash
# Idempotent: creates the orphan `stats` branch and the ruleset that makes it bot-only.
# Needs an authenticated gh with admin rights.
set -euo pipefail

REPO=devstreamhq/devstream

if ! gh api "repos/$REPO/git/ref/heads/stats" >/dev/null 2>&1; then
  readme=$(gh api -X POST "repos/$REPO/git/blobs" \
    -f content='Generated stats for DevStream. Written by the bot only.' -f encoding=utf-8 --jq .sha)
  data=$(gh api -X POST "repos/$REPO/git/blobs" -f content='{}' -f encoding=utf-8 --jq .sha)
  # gh's -f flags can't build an array of objects, so the tree goes in as JSON.
  tree=$(printf '{"tree":[{"path":"README.md","mode":"100644","type":"blob","sha":"%s"},{"path":"discussions.json","mode":"100644","type":"blob","sha":"%s"}]}' "$readme" "$data" |
    gh api -X POST "repos/$REPO/git/trees" --input - --jq .sha)
  commit=$(gh api -X POST "repos/$REPO/git/commits" \
    -f message='chore(stats): start the stats branch' -f "tree=$tree" --jq .sha)
  gh api -X POST "repos/$REPO/git/refs" -f ref=refs/heads/stats -f "sha=$commit" >/dev/null
fi

RULESET=$(cat <<'J'
{
  "name": "stats-bot-only",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/stats"], "exclude": [] } },
  "bypass_actors": [
    { "actor_id": 5633513, "actor_type": "Integration", "bypass_mode": "always" }
  ],
  "rules": [{ "type": "update" }, { "type": "deletion" }, { "type": "non_fast_forward" }]
}
J
)

id=$(gh api "repos/$REPO/rulesets" --jq '.[] | select(.name == "stats-bot-only") | .id')
if [ -n "$id" ]; then
  echo "$RULESET" | gh api -X PUT "repos/$REPO/rulesets/$id" --input - >/dev/null
else
  echo "$RULESET" | gh api -X POST "repos/$REPO/rulesets" --input - >/dev/null
fi
