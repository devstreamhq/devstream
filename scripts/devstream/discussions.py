from scripts.devstream.gh import GitHubError

SITE = "https://devstream.jamesconway.dev"

_CATEGORY = """query LikesCategory {
  repository(owner: "devstreamhq", name: "devstream") {
    id
    discussionCategories(first: 50) { nodes { id slug } }
  }
}"""

_CREATE = """mutation CreateEntryDiscussion($repositoryId: ID!, $categoryId: ID!, $title: String!, $body: String!) {
  createDiscussion(input: {repositoryId: $repositoryId, categoryId: $categoryId, title: $title, body: $body}) {
    discussion { number }
  }
}"""


def likes_category(gh) -> tuple[str, str]:
    """Return (repo node id, category id) for the `likes` category."""
    repo = gh.graphql(_CATEGORY, {})["repository"]
    for c in repo["discussionCategories"]["nodes"]:
        if c["slug"] == "likes":
            return repo["id"], c["id"]
    raise GitHubError(404, "Discussion category 'likes' is missing")


def ensure_discussion(entry, gh, repo_id: str, category_id: str, existing: dict[str, int]) -> int:
    repo = entry["repo"]
    if repo in existing:
        return existing[repo]
    # The description already passed the filter in description.py.
    body = (
        f"{entry['description']}\n\n"
        f"**Repo:** https://github.com/{repo}\n\n"
        "React with 👍 to Like it. Every GitHub account counts once.\n\n"
        f"See where it ranks: {SITE}"
    )
    data = gh.graphql(
        _CREATE,
        {"repositoryId": repo_id, "categoryId": category_id, "title": repo, "body": body},
    )
    return data["createDiscussion"]["discussion"]["number"]
