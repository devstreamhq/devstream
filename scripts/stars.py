"""Open the "turn on Sponsors" issue once the repo reaches 100 stars.

python -m scripts.stars   (needs GITHUB_TOKEN)
"""

import os
import urllib.parse

from scripts.devstream.gh import GitHub

REPO = "devstreamhq/devstream"
API = f"/repos/{REPO}"
GOAL = 100
TITLE = "Turn on GitHub Sponsors (we hit 100 stars)"
BODY = """We've passed 100 stars, which is the point James said Sponsors would go on.

To turn it on:

1. Check that the GitHub Sponsors profile for @jamesconwaydev is approved and live.
2. In devstreamhq/devstream, add `.github/FUNDING.yml` containing:

   ```yaml
   github: [jamesconwaydev]
   ```

3. Open that as a PR and merge it.

Done when the Sponsor button shows at the top of the repo page.
"""


def _exists(gh) -> bool:
    # Any state: once it's closed, it stays closed. Only the bot's own issue counts.
    q = f'repo:{REPO} is:issue in:title "{TITLE}" author:app/devstreamhq-bot'
    found = gh.get("/search/issues?" + urllib.parse.urlencode({"q": q}))
    return any(i["title"] == TITLE for i in (found or {}).get("items", []))


def check(gh) -> int | None:
    """Return the new issue's number, or None when nothing was opened."""
    if gh.get(API)["stargazers_count"] < GOAL or _exists(gh):
        return None
    issue = gh.post(f"{API}/issues", {"title": TITLE, "body": BODY, "labels": ["priority: high", "area: docs"]})
    print(f"opened #{issue['number']}")
    return issue["number"]


if __name__ == "__main__":
    check(GitHub(os.environ["GITHUB_TOKEN"]))
