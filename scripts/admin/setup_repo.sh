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
  -f squash_merge_commit_title=PR_TITLE \
  -f squash_merge_commit_message=BLANK \
  -f homepage=https://devstream.jamesconway.dev \
  -f 'security_and_analysis[secret_scanning][status]=enabled' \
  -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled' >/dev/null

gh api -X PUT "repos/$REPO/topics" \
  -f 'names[]=awesome' -f 'names[]=awesome-list' -f 'names[]=developer-tools' \
  -f 'names[]=github' -f 'names[]=open-source' -f 'names[]=curated-list' \
  -f 'names[]=directory' -f 'names[]=ranking' >/dev/null

gh api -X PUT "repos/$REPO/private-vulnerability-reporting" >/dev/null

# Issue forms silently drop labels that don't exist, which would stop submit.yml and reports.yml firing.
gh label create submission -R "$REPO" --color 0e8a16 --description "Repo submission" --force >/dev/null
gh label create accepted -R "$REPO" --color 1d76db --description "Submission accepted" --force >/dev/null
gh label create rejected -R "$REPO" --color b60205 --description "Submission rejected" --force >/dev/null
gh label create needs-review -R "$REPO" --color fbca04 --description "Needs a maintainer" --force >/dev/null
gh label create report -R "$REPO" --color d93f0b --description "Entry reported" --force >/dev/null
gh label create superseded -R "$REPO" --color c5def5 --description "Reported as replaced by another repo" --force >/dev/null

# The analyze (...) contexts are CodeQL's matrix jobs. Scorecard isn't listed: it never runs on PRs.
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
        "required_review_thread_resolution": false,
        "require_extra_approval_for_unattributed_changes": true
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [
          { "context": "validate", "integration_id": 15368 },
          { "context": "test", "integration_id": 15368 },
          { "context": "lint", "integration_id": 15368 },
          { "context": "analyze (python)", "integration_id": 15368 },
          { "context": "analyze (javascript-typescript)", "integration_id": 15368 },
          { "context": "analyze (actions)", "integration_id": 15368 }
        ]
      }
    },
    {
      "type": "code_scanning",
      "parameters": {
        "code_scanning_tools": [
          { "tool": "CodeQL", "alerts_threshold": "errors", "security_alerts_threshold": "high_or_higher" }
        ]
      }
    },
    { "type": "required_linear_history" },
    { "type": "non_fast_forward" },
    { "type": "deletion" }
  ]
}
J
)

# A v* tag triggers release.yml, so only admins may create, move or delete one.
TAG_RULESET=$(cat <<'J'
{
  "name": "release-tags",
  "target": "tag",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/tags/v*"], "exclude": [] } },
  "bypass_actors": [
    { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always" }
  ],
  "rules": [
    { "type": "creation" },
    { "type": "update" },
    { "type": "deletion" }
  ]
}
J
)

upsert_ruleset() {
  local ids
  # This repo's own rulesets only: an org ruleset with the same name can't be updated from here.
  mapfile -t ids < <(gh api --paginate "repos/$REPO/rulesets?includes_parents=false" \
    --jq ".[] | select(.name==\"$1\") | .id" | sort -n)
  if [ "${#ids[@]}" -gt 1 ]; then
    # Deleting a ruleset is a call to make by hand, so update the oldest and say which others exist.
    echo "warning: ${#ids[@]} rulesets are named $1 (ids ${ids[*]}); updating ${ids[0]}, delete the rest by hand" >&2
  fi
  if [ "${#ids[@]}" -gt 0 ]; then
    echo "$2" | gh api -X PUT "repos/$REPO/rulesets/${ids[0]}" --input - >/dev/null
  else
    echo "$2" | gh api -X POST "repos/$REPO/rulesets" --input - >/dev/null
  fi
}
upsert_ruleset main-protection "$RULESET"
upsert_ruleset release-tags "$TAG_RULESET"
# The org setting caps this one: while the org forbids Actions approving PRs, GitHub refuses it.
if ! gh api -X PUT "repos/$REPO/actions/permissions/workflow" \
  -f default_workflow_permissions=read \
  -F can_approve_pull_request_reviews=true >/dev/null 2>&1; then
  org=$(gh api "orgs/${REPO%%/*}/actions/permissions/workflow" --jq .can_approve_pull_request_reviews 2>/dev/null || true)
  if [ "$org" != false ]; then
    echo "error: could not set the repo's Actions workflow permissions" >&2
    exit 1
  fi
  # Still make the default token read-only; only the approval part waits on the org.
  gh api -X PUT "repos/$REPO/actions/permissions/workflow" -f default_workflow_permissions=read >/dev/null
  echo "note: the ${REPO%%/*} org doesn't let GitHub Actions approve pull requests, so auto-approve.yml can't" >&2
  echo "  approve bot PRs yet. Allow it under the org's Settings > Actions > General, then run this again." >&2
fi
echo "done"
