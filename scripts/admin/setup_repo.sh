#!/usr/bin/env bash
# Idempotent repo configuration. Needs an authenticated gh with admin rights.
set -euo pipefail

REPO=devstreamhq/devstream

gh api -X PATCH "repos/$REPO" \
  -F has_discussions=true \
  -F delete_branch_on_merge=true \
  -F allow_auto_merge=true \
  -F allow_squash_merge=true \
  -F allow_merge_commit=false \
  -F allow_rebase_merge=false \
  -f homepage=https://devstream.jamesconway.dev \
  -f 'security_and_analysis[secret_scanning][status]=enabled' \
  -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled' >/dev/null

gh api -X PUT "repos/$REPO/topics" \
  -f 'names[]=awesome' -f 'names[]=awesome-list' -f 'names[]=developer-tools' \
  -f 'names[]=github' -f 'names[]=open-source' -f 'names[]=curated-list' \
  -f 'names[]=directory' -f 'names[]=ranking' >/dev/null

gh api -X PUT "repos/$REPO/private-vulnerability-reporting" >/dev/null

RULESET=$(cat <<'J'
{
  "name": "main-protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "bypass_actors": [
    { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request" }
  ],
  "rules": [
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false
      }
    },
    { "type": "required_linear_history" },
    { "type": "non_fast_forward" },
    { "type": "deletion" }
  ]
}
J
)

ID=$(gh api "repos/$REPO/rulesets" --jq '.[] | select(.name=="main-protection") | .id')
if [ -n "$ID" ]; then
  echo "$RULESET" | gh api -X PUT "repos/$REPO/rulesets/$ID" --input - >/dev/null
else
  echo "$RULESET" | gh api -X POST "repos/$REPO/rulesets" --input - >/dev/null
fi
# Last: fails while the org forbids Actions approving PRs (org setting).
gh api -X PUT "repos/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=read \
  -F can_approve_pull_request_reviews=true >/dev/null
echo "done"
