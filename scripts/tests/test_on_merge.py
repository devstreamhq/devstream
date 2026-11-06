import base64
import json
import unittest
from unittest import mock
from pathlib import Path

from scripts.devstream.gh import GitHubError
from datetime import datetime, timezone

from scripts.on_merge import CAP, main, run, track_hidden
from scripts.tests.fake_gh import FakeGitHub

FILE = "/repos/devstreamhq/devstream/contents/discussions.json"


def entry(i, status="live"):
    return {"repo": f"o/r{i}", "description": "d", "status": status}


REF = "/repos/devstreamhq/devstream/git/ref/heads/stats"


def gh(mapping=None, fail_at=None, threads=(), ref=True, put_fails=False):
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
        "GRAPHQL LikesDiscussions": {"repository": {"discussions": {
            "nodes": [{"number": n, "title": t, "author": {"login": a}} for t, n, a in threads],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}},
    }
    if ref:
        routes[f"GET {REF}"] = {"ref": "refs/heads/stats"}
    if put_fails:
        def boom(b):
            raise GitHubError(409, "conflict")
        routes[f"PUT {FILE}"] = boom
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

    def test_missing_stats_branch(self):
        g = gh({}, ref=False)
        with self.assertRaises(GitHubError) as c:
            run(g, [entry(1)], sleep=lambda s: None)
        self.assertEqual(c.exception.message, "stats branch is missing; run scripts/admin/setup_stats.sh")
        self.assertFalse([c for c in g.calls if c[0] == "GRAPHQL"])

    def test_reuses_existing_threads_after_failed_put(self):
        g = gh({}, threads=[("O/R1", 55, "devstreamhq-bot")])
        run(g, [entry(1)], sleep=lambda s: None)
        self.assertEqual(written(g), {"o/r1": 55})
        self.assertFalse([c for c in g.calls if c[1] == "CreateEntryDiscussion"])

    def test_ignores_threads_by_others(self):
        g = gh({}, threads=[("o/r1", 55, "mallory"), ("o/r2", 56, "devstreamhq-bot[bot]")])
        run(g, [entry(1), entry(2)], sleep=lambda s: None)
        self.assertEqual(written(g), {"o/r1": 101, "o/r2": 56})

    def test_mixed_case_shares_one_thread(self):
        g = gh({})
        run(g, [{**entry(0), "repo": "Foo/Bar"}, {**entry(0), "repo": "foo/bar"}], sleep=lambda s: None)
        self.assertEqual(written(g), {"foo/bar": 101})

    def test_create_error_survives_put_failure(self):
        g = gh({}, fail_at=2, put_fails=True)
        with self.assertRaises(GitHubError) as c:
            run(g, [entry(i) for i in range(3)], sleep=lambda s: None)
        self.assertEqual(c.exception.status, 500)

    def test_put_error_raised_when_creates_fine(self):
        g = gh({}, put_fails=True)
        with self.assertRaises(GitHubError) as c:
            run(g, [entry(1)], sleep=lambda s: None)
        self.assertEqual(c.exception.status, 409)

    def test_stored_mapping_wins_over_a_thread_with_the_same_title(self):
        # A thread retitled onto a listed name must never take over that entry's Likes.
        g = gh({"o/r1": 5}, threads=[("o/r1", 9, "devstreamhq-bot[bot]"), ("o/r2", 7, "devstreamhq-bot[bot]")])
        run(g, [entry(1), entry(2)])
        self.assertEqual(written(g), {"o/r1": 5, "o/r2": 7})

    def test_skips_removed_threads(self):
        g = gh({}, threads=[("Removed: o/r1", 9, "devstreamhq-bot[bot]"), ("o/r2", 7, "devstreamhq-bot[bot]")])
        run(g, [entry(2)])
        self.assertEqual(written(g), {"o/r2": 7})

    def test_no_put_when_unchanged(self):
        g = gh({"o/r1": 5})
        run(g, [entry(1)], sleep=lambda s: None)
        self.assertEqual(puts(g), [])


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def step_after(text, marker):
    """The text from marker to the end of the file."""
    return text[text.index(marker):]


HIDDEN = "/repos/devstreamhq/devstream/contents/hidden.json"
AT = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)


def hidden_gh(stored=None):
    routes = {f"PUT {HIDDEN}": {}}
    if stored is not None:
        routes[f"GET {HIDDEN}"] = {"content": base64.b64encode(json.dumps(stored).encode()).decode(), "sha": "h1"}
    return FakeGitHub(routes)


class TrackHidden(unittest.TestCase):
    def test_records_hidden_entries_only(self):
        g = hidden_gh()
        track_hidden(g, [entry(1), entry(2, "hidden")], AT)
        self.assertEqual(written(g), {"o/r2": {"hidden": True, "unhidden_at": None, "seen_at": "2026-11-06T15:00:00Z"}})

    def test_stamps_an_unhide(self):
        g = hidden_gh({"o/r1": {"hidden": True, "unhidden_at": None}})
        track_hidden(g, [entry(1)], AT)
        self.assertEqual(written(g), {"o/r1": {"hidden": False, "unhidden_at": "2026-11-06T15:00:00Z"}})

    def test_keeps_the_stamp_and_drops_entries_off_the_list(self):
        stamp = {"hidden": False, "unhidden_at": "2026-10-01T00:00:00Z"}
        g = hidden_gh({"o/r1": stamp, "o/gone": {"hidden": True, "unhidden_at": None}})
        track_hidden(g, [entry(1)], AT)
        self.assertEqual(written(g), {"o/r1": stamp})
        g = hidden_gh({"o/r1": stamp})
        track_hidden(g, [entry(1, "hidden")], AT)
        self.assertEqual(written(g), {"o/r1": {"hidden": True, "unhidden_at": "2026-10-01T00:00:00Z",
                                               "seen_at": "2026-11-06T15:00:00Z"}})

    def test_no_put_when_unchanged(self):
        g = hidden_gh({"o/r2": {"hidden": False, "unhidden_at": "2026-10-01T00:00:00Z"}})
        track_hidden(g, [entry(1), entry(2)], AT)
        self.assertEqual(puts(g), [])

    def test_failed_hidden_write_still_syncs_discussions(self):
        g = gh()

        def boom(b):
            raise GitHubError(500, "boom")
        g.routes[f"PUT {HIDDEN}"] = boom
        with mock.patch("scripts.on_merge.GitHub", return_value=g), \
                mock.patch("scripts.on_merge.entries_mod.load", return_value=[entry(1), entry(2, "hidden")]), \
                mock.patch.dict("os.environ", {"GITHUB_TOKEN": "t"}), \
                self.assertLogs(level="ERROR") as logs:
            main()
        self.assertIn("could not write hidden.json", logs.output[0])
        self.assertEqual(written(g), {"o/r1": 101})


class Workflows(unittest.TestCase):
    """Threads are made by the run that changed the list, while it still holds list-writes."""

    def test_rot_makes_threads_after_its_keys_move(self):
        text = (WORKFLOWS / "rot.yml").read_text()
        self.assertIn("python -m scripts.on_merge", step_after(text, "python -m scripts.rot finish"))

    def test_submit_makes_the_thread_once_its_pr_has_merged(self):
        text = (WORKFLOWS / "submit.yml").read_text()
        apply = text[text.index("\n  apply:\n"):text.index("\n  reject:\n")]
        tail = step_after(apply, "did not merge within 15 minutes")
        self.assertIn("python -m scripts.on_merge", tail)
        # A failed create is the hourly run's to retry; it mustn't park an accepted issue as needs-review.
        step = tail[tail.rindex("- ", 0, tail.index("python -m scripts.on_merge")):]
        self.assertIn("continue-on-error: true", step[:step.index("python -m scripts.on_merge")])

    def test_on_merge_is_only_the_hourly_backstop(self):
        text = (WORKFLOWS / "on-merge.yml").read_text()
        on = text[text.index("\non:\n"):text.index("\npermissions:")]
        self.assertNotIn("push:", on)
        self.assertIn("schedule:", on)
        self.assertIn("group: list-writes", text)

    def test_site_redeploys_after_the_runs_that_make_threads(self):
        text = (WORKFLOWS / "deploy.yml").read_text()
        after = text[text.index("workflow_run:"):text.index("types: [completed]")]
        for name in ("Submit", "Health check", "Create entry Discussions", "Sync Likes"):
            self.assertIn(f'"{name}"', after)


if __name__ == "__main__":
    unittest.main()
