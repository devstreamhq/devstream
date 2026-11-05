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
ROT_QUEUED = f"GET {API}/actions/workflows/rot.yml/runs?status=queued"
ROT_PENDING = f"GET {API}/actions/workflows/rot.yml/runs?status=pending"
MERGE_QUEUED = f"GET {API}/actions/workflows/on-merge.yml/runs?status=queued"
MERGE_PENDING = f"GET {API}/actions/workflows/on-merge.yml/runs?status=pending"
ROT_OK = f"GET {API}/actions/workflows/rot.yml/runs?status=success&per_page=100"
ROT_LATEST = f"GET {API}/actions/workflows/rot.yml/runs?per_page=100"
ROT_DISPATCH = f"POST {API}/actions/workflows/rot.yml/dispatches"
ROT_REFS = f"GET {API}/git/matching-refs/heads/rot/"
OPEN_PRS = f"GET {API}/pulls?state=open&per_page=100"
BOT = "devstreamhq-bot[bot]"


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def issue(n, age=timedelta(hours=1), labels=("submission",), **kw):
    return {"number": n, "created_at": iso(NOW - age), "labels": [{"name": x} for x in labels], **kw}


def runs(*ages, title="Health check"):
    return {"total_count": len(ages),
            "workflow_runs": [{"created_at": iso(NOW - a), "display_title": title} for a in ages]}


def mixed(*runs_):
    """runs_ is (age, title) pairs, newest first like the API."""
    return {"total_count": len(runs_),
            "workflow_runs": [{"created_at": iso(NOW - a), "display_title": t} for a, t in runs_]}


def pr(n, ref_, age=timedelta(days=2), login=BOT, repo="devstreamhq/devstream"):
    return {"number": n, "created_at": iso(NOW - age), "user": {"login": login},
            "head": {"ref": ref_, "repo": {"full_name": repo}}}


def ref(n, sha=None):
    return {"ref": f"refs/heads/submit/{n}", "object": {"sha": sha or f"sha{n}"}}


def gh(issues=(), refs=(), routes=None):
    r = {
        ISSUES: list(issues), REFS: list(refs), DISPATCH: None, f"DELETE {API}/git/refs/heads/submit/9": None,
        QUEUED: {"total_count": 0}, PENDING: {"total_count": 0},
        ROT_QUEUED: {"total_count": 0}, ROT_PENDING: {"total_count": 0},
        MERGE_QUEUED: {"total_count": 0}, MERGE_PENDING: {"total_count": 0},
        ROT_OK: runs(timedelta(days=2)), ROT_LATEST: runs(timedelta(days=2)),
        ROT_DISPATCH: None, ROT_REFS: [], OPEN_PRS: [],
    }
    r.update(routes or {})
    return FakeGitHub(r)


def commit(sha, age):
    return {f"GET {API}/commits/{sha}": {"commit": {"committer": {"date": iso(NOW - age)}}}}


def dispatched(g):
    return [b["inputs"]["issue"] for m, p, b in g.calls if m == "POST" and "submit.yml" in p]


def rot_dispatched(g):
    return [b for m, p, b in g.calls if m == "POST" and "rot.yml" in p]


def closed(g):
    return [(p, b) for m, p, b in g.calls if m == "PATCH"]


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
        body = [b for m, p, b in g.calls if m == "POST" and "submit.yml" in p][0]
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

    def test_no_dispatch_while_a_rot_run_waits(self):
        # Both share the list-writes group, so a new dispatch would cancel the waiting rot run.
        for key in (ROT_QUEUED, ROT_PENDING):
            g = gh([issue(1)], routes={key: {"total_count": 1}})
            sweep(g, NOW)
            self.assertEqual(dispatched(g), [], key)

    def test_no_dispatch_while_an_on_merge_run_waits(self):
        for key in (MERGE_QUEUED, MERGE_PENDING):
            g = gh([issue(1)], routes={key: {"total_count": 1}, ROT_OK: runs(), ROT_LATEST: runs()})
            sweep(g, NOW)
            self.assertEqual((dispatched(g), rot_dispatched(g)), ([], []), key)

    def test_dispatches_rot_when_no_success_in_8_days(self):
        for ok_runs in (runs(), runs(timedelta(days=8, minutes=1))):
            g = gh(routes={ROT_OK: ok_runs, ROT_LATEST: runs(timedelta(days=2))})
            sweep(g, NOW)
            self.assertEqual(rot_dispatched(g), [{"ref": "main"}])

    def test_no_rot_dispatch_after_a_recent_success(self):
        g = gh(routes={ROT_OK: runs(timedelta(days=7, hours=23)), ROT_LATEST: runs(timedelta(days=7))})
        sweep(g, NOW)
        self.assertEqual(rot_dispatched(g), [])

    def test_failing_rot_is_retried_at_most_daily(self):
        # Every failed run can leave a PR open, so a 15-minute retry loop would pile them up.
        g = gh(routes={ROT_OK: runs(), ROT_LATEST: runs(timedelta(hours=23))})
        sweep(g, NOW)
        self.assertEqual(rot_dispatched(g), [])

    def test_overdue_rot_goes_before_submissions(self):
        g = gh([issue(1)], routes={ROT_OK: runs(), ROT_LATEST: runs()})
        sweep(g, NOW)
        self.assertEqual((dispatched(g), rot_dispatched(g)), ([], [{"ref": "main"}]))

    def test_submissions_go_first_when_rot_is_not_overdue(self):
        g = gh([issue(1)])
        sweep(g, NOW)
        self.assertEqual((dispatched(g), rot_dispatched(g)), (["1"], []))

    def test_only_full_runs_count_as_a_health_check(self):
        # A one-repo re-check doesn't cover the rest of the list.
        oks = mixed((timedelta(hours=3), "Health check (one repo)"), (timedelta(days=9), "Health check"))
        latest = mixed((timedelta(hours=3), "Health check (one repo)"), (timedelta(days=2), "Health check"))
        g = gh(routes={ROT_OK: oks, ROT_LATEST: latest})
        sweep(g, NOW)
        self.assertEqual(rot_dispatched(g), [{"ref": "main"}])

        # Names that only start or end the same way don't count either.
        oks = mixed((timedelta(days=1), "Health check (one repo)"), (timedelta(days=1), "Health check "),
                    (timedelta(days=1), "health check"))
        g = gh(routes={ROT_OK: oks, ROT_LATEST: runs(timedelta(days=2))})
        sweep(g, NOW)
        self.assertEqual(rot_dispatched(g), [{"ref": "main"}])

    def test_no_rot_dispatch_while_list_writes_waits(self):
        for key in (QUEUED, PENDING, ROT_QUEUED, ROT_PENDING, MERGE_QUEUED, MERGE_PENDING):
            g = gh(routes={key: {"total_count": 1}, ROT_OK: runs(), ROT_LATEST: runs()})
            sweep(g, NOW)
            self.assertEqual(rot_dispatched(g), [], key)

    def test_deletes_old_rot_branch_without_open_pr(self):
        routes = {
            ROT_REFS: [{"ref": "refs/heads/rot/123", "object": {"sha": "r1"}},
                       {"ref": "refs/heads/rot/456", "object": {"sha": "r2"}},
                       {"ref": "refs/heads/rot/789", "object": {"sha": "r3"}}],
            **commit("r1", timedelta(days=1, minutes=1)),
            **commit("r2", timedelta(hours=23)),
            **commit("r3", timedelta(days=3)),
            f"GET {API}/pulls?state=open&head=devstreamhq:rot/123": [],
            f"GET {API}/pulls?state=open&head=devstreamhq:rot/789": [{"number": 50}],
            f"DELETE {API}/git/refs/heads/rot/123": None,
        }
        g = gh(routes=routes)
        sweep(g, NOW)
        self.assertEqual(deleted(g), [f"{API}/git/refs/heads/rot/123"])

    def test_ignores_rot_refs_that_are_not_run_ids(self):
        bad = ["refs/heads/rot/12x", "refs/heads/rot/", "refs/heads/rot/1/2", "refs/heads/rot/../main",
               "refs/heads/rot/1\n", "refs/heads/rot-1", "refs/heads/xrot/1"]
        routes = {ROT_REFS: [{"ref": r, "object": {"sha": "old"}} for r in bad], **commit("old", timedelta(days=9))}
        g = gh(routes=routes)
        sweep(g, NOW)
        self.assertEqual(deleted(g), [])

    def test_closes_old_bot_rot_prs_and_deletes_branch(self):
        routes = {
            OPEN_PRS: [
                pr(1, "rot/111"),
                pr(2, "rot/222", age=timedelta(hours=23)),
                pr(3, "rot/333", login="mallory"),
                pr(4, "rot/444", repo="mallory/devstream"),
                pr(5, "submit/7"),
                pr(6, "rot/6;x"),
            ],
            f"PATCH {API}/pulls/1": {},
            f"DELETE {API}/git/refs/heads/rot/111": None,
        }
        g = gh(routes=routes)
        sweep(g, NOW)
        self.assertEqual(closed(g), [(f"{API}/pulls/1", {"state": "closed"})])
        self.assertEqual(deleted(g), [f"{API}/git/refs/heads/rot/111"])

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
