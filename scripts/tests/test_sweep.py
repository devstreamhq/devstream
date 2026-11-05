import unittest
from datetime import datetime, timedelta, timezone

from scripts.sweep import sweep
from scripts.tests.fake_gh import FakeGitHub

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
API = "/repos/devstreamhq/devstream"
ISSUES = f"GET {API}/issues"
REFS = f"GET {API}/git/matching-refs/heads/submit/"
DISPATCH = f"POST {API}/actions/workflows/submit.yml/dispatches"


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(n, age=timedelta(hours=1), labels=("submission",), **kw):
    return {"number": n, "created_at": iso(NOW - age), "labels": [{"name": x} for x in labels], **kw}


def ref(n, sha=None):
    return {"ref": f"refs/heads/submit/{n}", "object": {"sha": sha or f"sha{n}"}}


def gh(issues=(), refs=(), routes=None):
    r = {ISSUES: list(issues), REFS: list(refs), DISPATCH: None, f"DELETE {API}/git/refs/heads/submit/9": None}
    r.update(routes or {})
    return FakeGitHub(r)


def commit(sha, age):
    return {f"GET {API}/commits/{sha}": {"commit": {"committer": {"date": iso(NOW - age)}}}}


def dispatched(g):
    return [b["inputs"]["issue"] for m, p, b in g.calls if m == "POST"]


def deleted(g):
    return [p for m, p, b in g.calls if m == "DELETE"]


class SweepTest(unittest.TestCase):
    def test_dispatches_only_stale_unhandled_submissions(self):
        g = gh([
            issue(1),
            issue(2, labels=("submission", "accepted")),
            issue(3, labels=("submission", "rejected")),
            issue(4, age=timedelta(minutes=9)),
            issue(5, pull_request={}),
            issue(6, age=timedelta(minutes=11)),
        ])
        sweep(g, NOW)
        self.assertEqual(dispatched(g), ["1", "6"])
        body = [b for m, p, b in g.calls if m == "POST"][0]
        self.assertEqual(body, {"ref": "main", "inputs": {"issue": "1"}})

    def test_caps_at_five_per_run(self):
        g = gh([issue(n) for n in range(1, 9)])
        sweep(g, NOW)
        self.assertEqual(dispatched(g), ["1", "2", "3", "4", "5"])

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
