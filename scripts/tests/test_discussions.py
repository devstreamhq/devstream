import unittest

from scripts.devstream.discussions import ensure_discussion, likes_category
from scripts.devstream.gh import GitHubError
from scripts.tests.fake_gh import FakeGitHub

ENTRY = {"repo": "octo/tool", "description": "A tidy tool for tidy people"}


def gh(number=7):
    return FakeGitHub(
        {"GRAPHQL CreateEntryDiscussion": {"createDiscussion": {"discussion": {"number": number}}}}
    )


class EnsureDiscussion(unittest.TestCase):
    def test_creates_missing_only(self):
        g = gh(7)
        self.assertEqual(ensure_discussion(ENTRY, g, "R1", "C1", {}), 7)
        (_, op, v), = g.calls
        self.assertEqual(op, "CreateEntryDiscussion")
        self.assertEqual((v["repositoryId"], v["categoryId"], v["title"]), ("R1", "C1", "octo/tool"))
        self.assertEqual(
            v["body"],
            "A tidy tool for tidy people\n\n**Repo:** https://github.com/octo/tool\n\n"
            "React with 👍 to Like it. Every GitHub account counts once.\n\n"
            "See where it ranks: https://devstream.jamesconway.dev",
        )

    def test_idempotent_when_existing(self):
        g = gh()
        self.assertEqual(ensure_discussion(ENTRY, g, "R1", "C1", {"octo/tool": 3}), 3)
        self.assertEqual(g.calls, [])


class Mentions(unittest.TestCase):
    def test_body_has_no_bare_mention(self):
        import re
        g = gh()
        ensure_discussion({"repo": "o/r", "description": "Thanks @user and @org/team"}, g, "R", "C", {})
        self.assertIsNone(re.search(r"@\w", g.calls[0][2]["body"]))


class Category(unittest.TestCase):
    def test_finds_by_slug(self):
        g = FakeGitHub({"GRAPHQL LikesCategory": {"repository": {"id": "R9", "discussionCategories": {
            "nodes": [{"id": "A", "slug": "general"}, {"id": "L", "slug": "likes"}]}}}})
        self.assertEqual(likes_category(g), ("R9", "L"))

    def test_missing_raises(self):
        g = FakeGitHub({"GRAPHQL LikesCategory": {"repository": {"id": "R9", "discussionCategories": {
            "nodes": [{"id": "A", "slug": "general"}]}}}})
        with self.assertRaises(GitHubError) as c:
            likes_category(g)
        self.assertEqual(c.exception.status, 404)
        self.assertIn("Discussion category 'likes' is missing", c.exception.message)


if __name__ == "__main__":
    unittest.main()
