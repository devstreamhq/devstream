from scripts.devstream.entries import normalize_repo
from scripts.devstream.gh import GitHubError

BOT_LOGIN = "devstreamhq-bot"
SITE = "https://devstream.jamesconway.dev"
REMOVED_PREFIX = "Removed: "

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


_LIST = """query LikesDiscussions($cursor: String, $cat: ID!) {
  repository(owner: "devstreamhq", name: "devstream") {
    discussions(first: 100, after: $cursor, categoryId: $cat) {
      nodes { number title author { login } }
      pageInfo { hasNextPage endCursor }
    }
  }
}"""


def list_discussions(gh, category_id: str) -> dict[str, int]:
    """Every thread already in the category, keyed by normalised repo."""
    out, cursor = {}, None
    while True:
        page = gh.graphql(_LIST, {"cursor": cursor, "cat": category_id})["repository"]["discussions"]
        for n in page["nodes"]:
            # Only the bot's own threads count; anyone else's could hijack an entry's Likes.
            if ((n.get("author") or {}).get("login") or "").removesuffix("[bot]") != BOT_LOGIN:
                continue
            # Retitled by the health check when its entry lost a rename collision.
            if n["title"].startswith(REMOVED_PREFIX):
                continue
            out[normalize_repo(n["title"])] = n["number"]
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


def likes_category(gh) -> tuple[str, str]:
    """Return (repo node id, category id) for the `likes` category."""
    repo = gh.graphql(_CATEGORY, {})["repository"]
    for c in repo["discussionCategories"]["nodes"]:
        if c["slug"] == "likes":
            return repo["id"], c["id"]
    raise GitHubError(404, "Discussion category 'likes' is missing")


def ensure_discussion(entry, gh, repo_id: str, category_id: str, existing: dict[str, int]) -> int:
    repo = entry["repo"]
    key = normalize_repo(repo)
    if key in existing:
        return existing[key]
    # The description already passed the filter in description.py.
    body = (
        f"{entry['description'].replace('@', '@' + chr(0x200b))}\n\n"
        f"**Repo:** https://github.com/{repo}\n\n"
        "React with 👍 to Like it. Every GitHub account counts once.\n\n"
        f"See where it ranks: {SITE}"
    )
    data = gh.graphql(
        _CREATE,
        {"repositoryId": repo_id, "categoryId": category_id, "title": repo, "body": body},
    )
    return data["createDiscussion"]["discussion"]["number"]
