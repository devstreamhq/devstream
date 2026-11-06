import unittest
import urllib.parse

from scripts.stars import TITLE, check
from scripts.tests.fake_gh import FakeGitHub

API = "/repos/devstreamhq/devstream"
SEARCH = "GET /search/issues"
CREATE = f"POST {API}/issues"


def fake(stars, found=()):
    return FakeGitHub({
        f"GET {API}": {"stargazers_count": stars},
        SEARCH: {"total_count": len(found), "items": [{"title": t, "number": 9} for t in found]},
        CREATE: {"number": 42},
    })


def creates(gh):
    return [c for c in gh.calls if c[0] == "POST"]


class StarsTest(unittest.TestCase):
    def test_below_100_does_nothing(self):
        gh = fake(99)
        self.assertIsNone(check(gh))
        self.assertEqual(creates(gh), [])

    def test_at_100_opens_the_issue(self):
        gh = fake(100)
        self.assertEqual(check(gh), 42)
        [(_, path, body)] = creates(gh)
        self.assertEqual(path, f"{API}/issues")
        self.assertEqual(body["title"], "Turn on GitHub Sponsors (we hit 100 stars)")
        self.assertEqual(body["labels"], ["priority: high", "area: docs"])
        self.assertIn(".github/FUNDING.yml", body["body"])
        self.assertIn("github: [jamesconwaydev]", body["body"])
        self.assertIn("devstreamhq/devstream", body["body"])

    def test_existing_issue_is_not_duplicated(self):
        gh = fake(250, found=[TITLE])
        self.assertIsNone(check(gh))
        self.assertEqual(creates(gh), [])

    def test_search_hit_with_a_different_title_still_opens(self):
        gh = fake(100, found=["Turn on GitHub Sponsors later"])
        self.assertEqual(check(gh), 42)

    def test_search_only_counts_bot_issues_in_any_state(self):
        gh = fake(100)
        check(gh)
        [path] = [c[1] for c in gh.calls if c[1].startswith("/search/issues")]
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["q"][0]
        self.assertIn("repo:devstreamhq/devstream", q)
        self.assertIn("is:issue", q)
        # A human can't suppress the issue by opening one with the same title.
        self.assertIn("author:app/devstreamhq-bot", q)
        self.assertNotIn("is:open", q)

if __name__ == "__main__":
    unittest.main()
