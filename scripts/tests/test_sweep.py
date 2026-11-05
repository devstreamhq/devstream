import unittest
from datetime import datetime, timedelta, timezone

from scripts.devstream.gh import GitHubError
from scripts.sweep import sweep
from scripts.tests.fake_gh import FakeGitHub

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
API = "/repos/devstreamhq/devstream"
ISSUES = f"GET {API}/issues"
REFS = f"GET {API}/git/matching-refs/heads/submit/"
DISPATCH = f"POST {API}/actions/workflows/submit.yml/dispatches"
QUEUED = f"GET {API}/actions/workflows/submit.yml/runs?status=queued"
PENDING = f"GET {API}/actions/workflows/submit.yml/runs?status=pending"


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(n, age=timedelta(hours=1), labels=("submission",), **kw):
    return {"number": n, "created_at": iso(NOW - age), "labels": [{"name": x} for x in labels], **kw}


def ref(n, sha=None):
    return {"ref": f"refs/heads/submit/{n}", "object": {"sha": sha or f"sha{n}"}}


def gh(issues=(), refs=(), routes=None):
    r = {
        ISSUES: list(issues), REFS: list(refs), DISPATCH: None, f"DELETE {API}/git/refs/heads/submit/9": None,
        QUEUED: {"total_count": 0}, PENDING: {"total_count": 0},
    }
    r.update(routes or {})
    return FakeGitHub(r)


def commit(sha, age):
    return {f"GET {API}/commits/{sha}": {"commit": {"committer": {"date": iso(NOW - age)}}}}


def dispatched(g):
    return [b["inputs"]["issue"] for m, p, b in g.calls if m == "POST"]


def deleted(g):
    return [p for m, p, b in g.calls if m == "DELETE"]


class SweepTest(unittest.TestCase):
    def test_skips_handled_young_and_pr_issues(self):
        g = gh([
            issue(2, labels=("submission", "accepted")),
            issue(3, labels=("submission", "rejected")),
            issue(4, age=timedelta(minutes=9)),
            issue(5, pull_request={}),
            issue(7, labels=("submission", "needs-review"), age=timedelta(days=2)),
            issue(6, age=timedelta(minutes=11)),
        ])
        sweep(g, NOW)
        self.assertEqual(dispatched(g), ["6"])
        body = [b for m, p, b in g.calls if m == "POST"][0]
        self.assertEqual(body, {"ref": "main", "inputs": {"issue": "6"}})

    def test_needs_review_is_never_dispatched(self):
        g = gh([issue(7, labels=("submission", "needs-review"))])
        sweep(g, NOW)
        self.assertEqual(dispatched(g), [])

    def test_caps_at_one_per_run(self):
        g = gh([issue(n, age=timedelta(hours=10 - n)) for n in range(1, 9)])
        sweep(g, NOW)
        self.assertEqual(dispatched(g), ["1"])

    def test_dispatches_oldest_first(self):
        g = gh([issue(3, age=timedelta(hours=1)), issue(1, age=timedelta(hours=5)), issue(2, age=timedelta(hours=3))])
        sweep(g, NOW)
        self.assertEqual(dispatched(g), ["1"])

    def test_no_dispatch_while_a_submit_run_waits(self):
        for key in (QUEUED, PENDING):
            g = gh([issue(1)], routes={key: {"total_count": 1}})
            sweep(g, NOW)
            self.assertEqual(dispatched(g), [], key)

    def test_failed_delete_does_not_stop_the_sweep(self):
        def gone(_):
            raise GitHubError(422, "Reference does not exist")

        routes = {
            **commit("sha8", timedelta(hours=1)), **commit("sha9", timedelta(hours=1)),
            f"GET {API}/pulls?state=open&head=devstreamhq:submit/8": [],
            f"GET {API}/pulls?state=open&head=devstreamhq:submit/9": [],
            f"DELETE {API}/git/refs/heads/submit/8": gone,
        }
        g = gh([issue(1)], refs=[ref(8), ref(9)], routes=routes)
        sweep(g, NOW)
        self.assertEqual(deleted(g), [f"{API}/git/refs/heads/submit/8", f"{API}/git/refs/heads/submit/9"])
        self.assertEqual(dispatched(g), ["1"])

    def test_deletes_old_branch_without_open_pr(self):
        routes = {**commit("sha9", timedelta(minutes=31)), f"GET {API}/pulls?state=open&head=devstreamhq:submit/9": []}
        g = gh(refs=[ref(9)], routes=routes)
        sweep(g, NOW)
        self.assertEqual(deleted(g), [f"{API}/git/refs/heads/submit/9"])

    def test_keeps_young_branch_and_branch_with_open_pr(self):
        routes = {
            **commit("sha9", timedelta(minutes=29)),
            **commit("sha8", timedelta(hours=5)),
            f"GET {API}/pulls?state=open&head=devstreamhq:submit/8": [{"number": 40}],
            f"GET {API}/pulls?state=open&head=devstreamhq:submit/9": [],
        }
        g = gh([issue(8), issue(9), issue(10)], refs=[ref(8), ref(9)], routes=routes)
        sweep(g, NOW)
        self.assertEqual(deleted(g), [])
        # An issue whose branch still exists is mid-apply, so it isn't dispatched again.
        self.assertEqual(dispatched(g), ["10"])

    def test_freed_issue_is_dispatched_after_its_branch_goes(self):
        routes = {**commit("sha9", timedelta(hours=1)), f"GET {API}/pulls?state=open&head=devstreamhq:submit/9": []}
        g = gh([issue(9)], refs=[ref(9)], routes=routes)
        sweep(g, NOW)
        self.assertEqual(dispatched(g), ["9"])

    def test_ignores_refs_that_are_not_submit_numbers(self):
        g = gh(refs=[{"ref": "refs/heads/submit/x;y", "object": {"sha": "s"}}])
        sweep(g, NOW)
        self.assertEqual(deleted(g), [])


if __name__ == "__main__":
    unittest.main()
