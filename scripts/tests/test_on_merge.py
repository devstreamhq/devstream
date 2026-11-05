import base64
import json
import unittest

from scripts.devstream.gh import GitHubError
from scripts.on_merge import CAP, run
from scripts.tests.fake_gh import FakeGitHub

FILE = "/repos/devstreamhq/devstream/contents/discussions.json"


def entry(i, status="live"):
    return {"repo": f"o/r{i}", "description": "d", "status": status}


def gh(mapping=None, fail_at=None):
    n = {"n": 0}

    def create(v):
        n["n"] += 1
        if n["n"] == fail_at:
            raise GitHubError(500, "boom")
        return {"createDiscussion": {"discussion": {"number": 100 + n["n"]}}}

    routes = {
        "GRAPHQL LikesCategory": {"repository": {"id": "R", "discussionCategories": {
            "nodes": [{"id": "C", "slug": "likes"}]}}},
        "GRAPHQL CreateEntryDiscussion": create,
        f"PUT {FILE}": {},
    }
    if mapping is not None:
        body = base64.b64encode(json.dumps(mapping).encode()).decode()
        routes[f"GET {FILE}"] = {"content": body + "\n", "sha": "abc"}
    return FakeGitHub(routes)


def puts(g):
    return [c for c in g.calls if c[0] == "PUT"]


def written(g):
    return json.loads(base64.b64decode(puts(g)[-1][2]["content"]))


class OnMerge(unittest.TestCase):
    def test_only_visible_statuses(self):
        g = gh({})
        es = [entry(i, s) for i, s in enumerate(
            ["live", "warned", "flagged", "hidden", "archived", "superseded"])]
        run(g, es, sleep=lambda s: None)
        self.assertEqual(sorted(written(g)), ["o/r0", "o/r1", "o/r2"])

    def test_cap_and_sleep(self):
        g, naps = gh({}), []
        run(g, [entry(i) for i in range(CAP + 5)], sleep=naps.append)
        self.assertEqual(len(written(g)), 40)
        self.assertEqual(naps, [2] * 39)

    def test_commit_shape_and_sha(self):
        g = gh({"o/old": 1})
        run(g, [entry(1), {**entry(0), "repo": "o/old"}], sleep=lambda s: None)
        body = puts(g)[0][2]
        self.assertEqual(body["branch"], "stats")
        self.assertEqual(body["sha"], "abc")
        self.assertEqual(body["message"], "chore(stats): map entry discussions")
        self.assertEqual(body["committer"], {
            "name": "devstreamhq-bot[bot]",
            "email": "353074438+devstreamhq-bot[bot]@users.noreply.github.com"})
        self.assertEqual(written(g), {"o/old": 1, "o/r1": 101})

    def test_missing_file_is_empty_and_has_no_sha(self):
        g = gh(None)
        run(g, [entry(1)], sleep=lambda s: None)
        self.assertNotIn("sha", puts(g)[0][2])

    def test_partial_progress_written_before_reraise(self):
        g = gh({}, fail_at=3)
        with self.assertRaises(GitHubError):
            run(g, [entry(i) for i in range(5)], sleep=lambda s: None)
        self.assertEqual(written(g), {"o/r0": 101, "o/r1": 102})

    def test_no_put_when_first_create_fails(self):
        g = gh({}, fail_at=1)
        with self.assertRaises(GitHubError):
            run(g, [entry(1)], sleep=lambda s: None)
        self.assertEqual(puts(g), [])

    def test_no_put_when_unchanged(self):
        g = gh({"o/r1": 5})
        run(g, [entry(1)], sleep=lambda s: None)
        self.assertEqual(puts(g), [])


if __name__ == "__main__":
    unittest.main()
