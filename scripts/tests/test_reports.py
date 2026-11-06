import base64
import io
import json
import os
import tempfile
import time
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts.devstream.entries import load, save
from scripts.devstream.gh import GitHubError, RateLimited
from scripts.reports import ERROR, handle_report, main
from scripts.tests.fake_gh import FakeGitHub

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
API = "/repos/devstreamhq/devstream"
BOT = "devstreamhq-bot[bot]"
WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "reports.yml"
NASTY = "@everyone <script>alert(1)</script>"


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def body(entry="https://github.com/alice/tool", reason="Spam or SEO", superseded_by="_No response_",
         details="_No response_"):
    return (
        f"### Entry\n\n{entry}\n\n### Reason\n\n{reason}\n\n"
        f"### Replaced by (owner/name)\n\n{superseded_by}\n\n### Details\n\n{details}\n"
    )


def report(n=7, login="carol", user_type="User", age=timedelta(hours=1), **kw):
    return {"number": n, "title": "Report: alice/tool", "user": {"login": login, "type": user_type},
            "body": body(**kw), "labels": [{"name": "report"}], "state": "open", "created_at": iso(NOW - age)}


def entry(repo="alice/tool", status="live"):
    return {
        "repo": repo, "repo_id": zlib.crc32(repo.encode()), "category": "developer-tools", "description": "An older tool that still works.",
        "submitted_by": "dave", "submitted_at": "2026-01-01T00:00:00Z", "issue": 1,
        "status": status, "warned_at": None, "superseded_by": None, "added_by_seed": False,
    }


# login -> (account age in days, owned non-fork repos, contributions in the last year, forks)
USERS = {
    "carol": (400, 3, 0, 0),
    "dave": (800, 0, 50, 0),
    "erin": (366, 10, 0, 0),
    "frank": (100, 20, 500, 0),  # trusted, but under a year old
    "gina": (400, 2, 49, 0),     # trusted and old, but too little activity
    "hank": (400, 0, 0, 5),      # trusted and old, but every repo is a fork
    "newbie": (10, 5, 100, 0),   # untrusted: under 30 days
}


ROT_RUNS = f"GET {API}/actions/workflows/rot.yml/runs"


def rot_runs(*runs_):
    """runs_ is (age, title) pairs."""
    return {ROT_RUNS: {"total_count": len(runs_), "workflow_runs": [
        {"created_at": iso(NOW - a), "display_title": t, "status": "completed"} for a, t in runs_]}}


def search_key(login):
    return (f"GET /search/issues?q=repo:devstreamhq/devstream+label:report+author:{login}"
            "+created:>=2026-11-04T12:00:00Z&per_page=100")


def gh(counts=None, extra=None, edited=(), current=7):
    """counts: login -> number of the reporter's OTHER reports in the last 24h (search also returns current)."""
    routes = {"GET /orgs/devstreamhq/blocks": []}
    for login, (age, repos, _, forks) in USERS.items():
        routes[f"GET /users/{login}"] = {"created_at": iso(NOW - timedelta(days=age)),
                                         "public_repos": repos + forks, "type": "User"}
        routes[f"GET /users/{login}/repos?type=owner&per_page=100"] = (
            [{"fork": False}] * repos + [{"fork": True}] * forks)
        items = [{"number": current}] + [{"number": 100 + k} for k in range((counts or {}).get(login, 0))]
        routes[search_key(login)] = {"total_count": len(items), "items": items}

    def activity(v):
        total = USERS[v["login"]][2]
        return {"user": {"contributionsCollection": {"contributionCalendar": {"totalContributions": total}}}}

    def edits(v):
        return {"repository": {f"i{k}": {"lastEditedAt": iso(NOW) if n in edited else None}
                               for k, n in ((k, v[f"n{k}"]) for k in range(len(v)))}}

    routes[ROT_RUNS] = {"total_count": 0, "workflow_runs": []}
    routes["GRAPHQL UserActivity"] = activity
    routes["GRAPHQL ReportEdits"] = edits
    routes.update(extra or {})
    return FakeGitHub(routes)


def hidden_json(data):
    raw = base64.b64encode(json.dumps(data).encode()).decode()
    return {f"GET {API}/contents/hidden.json": {"content": raw, "sha": "h1"}}


def run(i=None, g=None, entries=None, open_reports=()):
    i = report() if i is None else i
    return handle_report(i, g or gh(), [entry()] if entries is None else entries,
                         [i, *open_reports], now=NOW, excluded=set())


def kinds(actions):
    return [a.kind for a in actions]


def labels(actions):
    return {a.value for a in actions if a.kind == "label"}


def comments(actions):
    return [a.value for a in actions if a.kind == "comment"]


class ThresholdTest(unittest.TestCase):
    def test_three_distinct_trusted_hides(self):
        others = [report(8, "dave"), report(9, "erin")]
        actions = run(open_reports=others)
        self.assertIn(("hide", "alice/tool"), [(a.kind, a.value) for a in actions])
        self.assertIn("needs-review", labels(actions))
        self.assertIn("`alice/tool` is hidden", comments(actions)[0])

    def test_old_reports_stop_counting(self):
        self.assertNotIn("hide", kinds(run(open_reports=[report(8, "dave"), report(9, "erin", age=timedelta(days=31))])))
        self.assertIn("hide", kinds(run(open_reports=[report(8, "dave"), report(9, "erin", age=timedelta(days=29))])))

    def test_only_reports_after_an_unhide_count(self):
        g = gh(extra=hidden_json({"alice/tool": {"hidden": False, "unhidden_at": iso(NOW - timedelta(hours=2))}}))
        before = [report(8, "dave", age=timedelta(hours=3)), report(9, "erin", age=timedelta(hours=3))]
        self.assertNotIn("hide", kinds(run(g=g, open_reports=before)))
        after = [report(8, "dave"), report(9, "erin")]
        self.assertIn("hide", kinds(run(g=g, open_reports=after)))

    def test_unhidden_since_the_last_snapshot_counts_nothing_yet(self):
        rec = {"hidden": True, "unhidden_at": None, "seen_at": iso(NOW - timedelta(minutes=50))}
        g = gh(extra=hidden_json({"alice/tool": rec}))
        self.assertNotIn("hide", kinds(run(g=g, open_reports=[report(8, "dave"), report(9, "erin")])))

    def test_stale_snapshot_falls_back_to_the_window(self):
        rec = {"hidden": True, "unhidden_at": None, "seen_at": iso(NOW - timedelta(hours=3))}
        g = gh(extra=hidden_json({"alice/tool": rec}))
        self.assertIn("hide", kinds(run(g=g, open_reports=[report(8, "dave"), report(9, "erin")])))

    def test_unreadable_hidden_json_raises_and_hides_nothing(self):
        raw = base64.b64encode(b"{not json").decode()
        g = gh(extra={f"GET {API}/contents/hidden.json": {"content": raw, "sha": "h1"}})
        with self.assertRaises(ValueError):
            run(g=g, open_reports=[report(8, "dave"), report(9, "erin")])

    def test_malformed_or_naive_unhidden_at_raises(self):
        for bad in ("2026-11-05", "2026-11-05T10:00:00", "2026-11-05T10:00:00+00:00", "２０２６-11-05T10:00:00Z", 5):
            g = gh(extra=hidden_json({"alice/tool": {"hidden": False, "unhidden_at": bad}}))
            with self.assertRaises(ValueError, msg=bad):
                run(g=g, open_reports=[report(8, "dave"), report(9, "erin")])

    def test_unhide_of_another_entry_does_not_matter(self):
        g = gh(extra=hidden_json({"alice/other": {"hidden": True, "unhidden_at": None, "seen_at": iso(NOW)}}))
        self.assertIn("hide", kinds(run(g=g, open_reports=[report(8, "dave"), report(9, "erin")])))

    def test_something_else_never_hides_but_goes_to_review(self):
        others = [report(8, "dave"), report(9, "erin")]
        actions = run(report(reason="Something else"), open_reports=others)
        self.assertNotIn("hide", kinds(actions))
        self.assertEqual(labels(actions), {"needs-review"})
        self.assertIn("a maintainer will take a look", comments(actions)[0])
        self.assertNotIn("close", kinds(actions))

    def test_same_reporter_thrice_does_not_hide(self):
        others = [report(8, "carol"), report(9, "Carol")]
        actions = run(open_reports=others)
        self.assertNotIn("hide", kinds(actions))
        self.assertEqual(labels(actions), {"needs-review"})
        self.assertNotIn("close", kinds(actions))

    def test_reports_on_other_entries_do_not_count(self):
        others = [report(8, "dave"), report(9, "erin", entry="alice/other")]
        self.assertNotIn("hide", kinds(run(open_reports=others)))

    def test_entry_matched_in_any_spelling(self):
        others = [report(8, "dave", entry="ALICE/Tool.git"), report(9, "erin", entry="alice/tool/")]
        self.assertIn("hide", kinds(run(open_reports=others)))

    def test_trusted_but_not_established_does_not_count(self):
        for weak in ("frank", "gina"):
            actions = run(open_reports=[report(8, "dave"), report(9, weak)])
            self.assertNotIn("hide", kinds(actions), weak)
            self.assertEqual(labels(actions), {"needs-review"}, weak)
        # A weak reporter's own report is still valid, it just doesn't count.
        actions = run(report(login="frank"), open_reports=[report(8, "dave"), report(9, "erin")])
        self.assertNotIn("hide", kinds(actions))
        self.assertEqual(labels(actions), {"needs-review"})

    def test_only_spam_reports_count(self):
        for reason in ("Wrong category", "Dead or abandoned", "Replaced by something better",
                       "Malicious (malware, phishing, stolen code)", "Something else"):
            others = [report(8, "dave", reason=reason), report(9, "erin")]
            self.assertNotIn("hide", kinds(run(open_reports=others)), reason)
        others = [report(8, "dave", reason="Spam or SEO"), report(9, "erin")]
        self.assertIn("hide", kinds(run(open_reports=others)))

    def test_edited_report_does_not_count(self):
        others = [report(8, "dave"), report(9, "erin")]
        for edited in ((8,), (9,), (7,)):
            self.assertNotIn("hide", kinds(run(g=gh(edited=edited), open_reports=others)), edited)

    def test_category_report_edited_to_spam_does_not_count(self):
        # Issue 8 was decided as "Wrong category"; its body now says spam, but it was edited.
        others = [report(8, "dave", reason="Spam or SEO"), report(9, "erin")]
        actions = run(g=gh(edited=(8,)), open_reports=others)
        self.assertNotIn("hide", kinds(actions))
        self.assertEqual(labels(actions), {"needs-review"})

    def test_edit_check_is_one_query(self):
        g = gh()
        run(g=g, open_reports=[report(8, "dave"), report(9, "erin")])
        self.assertEqual(sum(1 for c in g.calls if c[:2] == ("GRAPHQL", "ReportEdits")), 1)

    def test_forks_do_not_make_a_reporter_established(self):
        self.assertNotIn("hide", kinds(run(open_reports=[report(8, "dave"), report(9, "hank")])))
        self.assertNotIn("hide", kinds(run(report(login="hank", reason="Malicious (malware, phishing, stolen code)"))))

    def test_untrusted_and_bot_reports_do_not_count(self):
        others = [report(8, "dave"), report(9, "newbie"), report(10, BOT, "Bot")]
        self.assertNotIn("hide", kinds(run(open_reports=others)))

    def test_unparseable_open_report_is_skipped(self):
        broken = dict(report(8, "dave"), body="### Entry\n\n")
        self.assertNotIn("hide", kinds(run(open_reports=[broken, report(9, "erin")])))


class MaliciousTest(unittest.TestCase):
    def test_malicious_one_trusted_hides(self):
        actions = run(report(reason="Malicious (malware, phishing, stolen code)"))
        self.assertIn(("hide", "alice/tool"), [(a.kind, a.value) for a in actions])
        self.assertEqual(labels(actions), {"needs-review"})

    def test_malicious_needs_established_reporter(self):
        for login in ("frank", "gina", "hank"):
            actions = run(report(login=login, reason="Malicious (malware, phishing, stolen code)"))
            self.assertNotIn("hide", kinds(actions), login)
            self.assertEqual(labels(actions), {"needs-review"}, login)


class TrustTest(unittest.TestCase):
    def test_untrusted_reporter_no_action(self):
        for reason in ("Malicious (malware, phishing, stolen code)", "Dead or abandoned", "Spam or SEO"):
            actions = run(report(login="newbie", reason=reason))
            self.assertEqual(kinds(actions), ["label", "comment"], reason)
            self.assertEqual(labels(actions), {"needs-review"})

    def test_bot_authored_report_ignored(self):
        g = gh()
        self.assertEqual(run(report(login=BOT, user_type="Bot"), g), [])
        self.assertEqual(run(report(login="someapp", user_type="Bot"), g), [])
        self.assertEqual(run(report(login=BOT), g), [])
        self.assertEqual(g.calls, [])

    def test_workflow_skips_bot_authors(self):
        text = WORKFLOW.read_text()
        self.assertIn("github.event.issue.user.type != 'Bot'", text)
        self.assertIn("github.event.issue.user.login != 'devstreamhq-bot[bot]'", text)

    def test_report_rate_limit(self):
        actions = run(g=gh(counts={"carol": 5}))
        self.assertEqual(kinds(actions), ["comment", "close"])
        self.assertEqual(comments(actions)[0], "You've hit the daily limit of 5 reports. Try again tomorrow.")
        self.assertIn("label", kinds(run(g=gh(counts={"carol": 4}))))

    def test_rate_limit_excludes_current_issue(self):
        # Search shows the current issue plus 4 others: 4 counts, so it goes through.
        g = gh(counts={"carol": 4})
        self.assertNotIn("close", kinds(run(g=g)))
        # Search lag: the current issue isn't indexed yet, and 5 others still hit the limit.
        g = gh(counts={"carol": 5}, current=999)
        self.assertEqual(kinds(run(g=g)), ["comment", "close"])

    def test_untrusted_flooder_hits_limit(self):
        g = gh(counts={"newbie": 5})
        actions = run(report(login="newbie"), g)
        self.assertEqual(kinds(actions), ["comment", "close"])
        self.assertIn("daily limit", comments(actions)[0])
        # The limit runs before any trust lookups.
        self.assertNotIn(("GET", "/users/newbie", None), g.calls)

    def test_bad_login_never_reaches_search(self):
        g = gh()
        actions = run(report(login="bad login+label:x"), g)
        self.assertEqual(labels(actions), {"needs-review"})
        self.assertFalse(any("/search/" in c[1] for c in g.calls))

    def test_workflow_holds_list_writes_only_while_hiding(self):
        text = WORKFLOW.read_text()
        self.assertEqual(text.count("concurrency:"), 1)
        hide = text.index("\n  hide:\n")
        respond = text.index("\n  respond:\n")
        self.assertTrue(hide < text.index("concurrency:") < respond)
        self.assertIn("group: list-writes", text)

    def test_block_access_checked(self):
        g = gh(extra={"GET /orgs/devstreamhq/blocks": None})
        with self.assertRaises(Exception):
            run(g=g)

    def test_api_failure_raises_not_decides(self):
        def limited(_):
            raise RateLimited(0)

        with self.assertRaises(RateLimited):
            run(g=gh(extra={search_key("carol"): limited}))


class ReasonTest(unittest.TestCase):
    def test_unknown_entry_rejected(self):
        g = gh()
        actions = run(report(entry="bob/unlisted"), g)
        self.assertEqual(kinds(actions), ["comment", "close"])
        self.assertIn("That repo isn't on the list", comments(actions)[0])

    def test_incomplete_or_bad_form_closed(self):
        for i in (dict(report(), body="### Entry\n\nalice/tool\n"), report(reason="Because"),
                  report(entry="not a repo at all"), dict(report(), body=None)):
            actions = run(i)
            self.assertEqual(kinds(actions), ["comment", "close"], i["body"])

    def test_dead_dispatches_rot_with_repo(self):
        actions = run(report(entry="https://github.com/Alice/Tool", reason="Dead or abandoned"))
        self.assertIn(("dispatch_rot", "alice/tool"), [(a.kind, a.value) for a in actions])
        self.assertEqual(labels(actions), {"needs-review"})
        self.assertNotIn("close", kinds(actions))
        self.assertEqual(comments(actions)[0], "A health check on `alice/tool` has been queued.")

    def test_second_dead_report_within_a_day_does_not_dispatch(self):
        dead = report(entry="https://github.com/Alice/Tool", reason="Dead or abandoned")
        for title in ("Health check: alice/tool", "Health check: Alice/Tool"):
            g = gh(extra=rot_runs((timedelta(hours=23), title)))
            actions = run(dead, g)
            self.assertNotIn("dispatch_rot", kinds(actions), title)
            self.assertEqual(labels(actions), {"needs-review"})
            self.assertIn("in the last day", comments(actions)[0])
        # Checks of other repos, and full runs, don't count.
        g = gh(extra=rot_runs((timedelta(hours=2), "Health check: alice/tool-ng"), (timedelta(hours=1), "Health check")))
        self.assertIn("dispatch_rot", kinds(run(dead, g)))
        call = next(c for c in g.calls if c[1].startswith(f"{API}/actions/workflows/rot.yml/runs"))
        self.assertIn("created=%3E%3D2026-11-04T12%3A00%3A00Z", call[1])

    def test_fourth_dead_report_within_an_hour_is_parked(self):
        dead = report(reason="Dead or abandoned")
        three = [(timedelta(minutes=15 * k + 5), f"Health check: x/r{k}") for k in range(3)]
        g = gh(extra=rot_runs(*three))
        actions = run(dead, g)
        self.assertNotIn("dispatch_rot", kinds(actions))
        self.assertEqual(labels(actions), {"needs-review"})
        self.assertIn("a maintainer", comments(actions)[0])
        # Two in the hour (the third is older) still dispatches.
        g = gh(extra=rot_runs(*three[:2], (timedelta(minutes=61), "Health check: x/r2")))
        self.assertIn("dispatch_rot", kinds(run(dead, g)))

    def test_superseded_labels_and_validates(self):
        actions = run(report(reason="Replaced by something better", superseded_by="https://github.com/Bob/New"))
        self.assertEqual(labels(actions), {"superseded", "needs-review"})
        self.assertNotIn("hide", kinds(actions))
        self.assertIn("`bob/new`", comments(actions)[0])

        actions = run(report(reason="Replaced by something better", superseded_by="definitely not a repo"))
        self.assertEqual(labels(actions), {"superseded", "needs-review"})
        self.assertIn("doesn't look like a GitHub repo", comments(actions)[0])

        actions = run(report(reason="Replaced by something better"))
        self.assertEqual(labels(actions), {"superseded", "needs-review"})

    def test_wrong_category_queued(self):
        actions = run(report(reason="Wrong category"), open_reports=[report(8, "dave"), report(9, "erin")])
        self.assertEqual(kinds(actions), ["label", "comment"])

    def test_already_hidden_only_labels_and_comments(self):
        for reason in ("Malicious (malware, phishing, stolen code)", "Dead or abandoned", "Spam or SEO"):
            actions = run(report(reason=reason), entries=[entry(status="hidden")],
                          open_reports=[report(8, "dave"), report(9, "erin")])
            self.assertEqual(kinds(actions), ["label", "comment"], reason)

    def test_comments_never_echo_details(self):
        cases = [
            report(details=NASTY, superseded_by=NASTY, reason="Replaced by something better"),
            report(details=NASTY, reason="Malicious (malware, phishing, stolen code)"),
            report(details=NASTY, reason="Dead or abandoned"),
            report(details=NASTY, reason=NASTY),
            report(details=NASTY, entry=NASTY),
            report(details=NASTY, entry="alice/tool " + NASTY),
            report(login="newbie", details=NASTY),
            dict(report(details=NASTY), title=NASTY),
        ]
        for i in cases:
            for text in comments(run(i)):
                self.assertNotIn("@everyone", text)
                self.assertNotIn("<script>", text)
                self.assertNotIn("alert", text)


def write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


class FailedDecideTest(unittest.TestCase):
    def decide(self, exc, tmp):
        def stuck(*a, **kw):
            raise exc

        with mock.patch("scripts.reports.GitHub", lambda token: gh()), \
                mock.patch("scripts.reports.handle_report", stuck), \
                mock.patch("scripts.reports.load", lambda: [entry()]), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}), \
                mock.patch("sys.stderr", io.StringIO()):
            ev, out, und = (os.path.join(tmp, f) for f in ("event.json", "result.json", "undecided.json"))
            write(ev, {"issue": report()})
            code = main(["decide", "--event", ev, "--out", out, "--undecided", und])
            self.assertFalse(os.path.exists(out))
            return code, und

    def test_failed_decide_is_counted_and_parked_after_three(self):
        comments, labels = [], []

        def post(b):
            comments.append({"id": 100 + len(comments), "user": {"login": BOT}, "body": b["body"]})

        def patch(b):
            comments[0]["body"] = b["body"]

        g = FakeGitHub({f"GET {API}/issues/7/comments": lambda _: list(comments),
                        f"POST {API}/issues/7/comments": post,
                        f"PATCH {API}/issues/comments/100": patch,
                        f"POST {API}/issues/7/labels": lambda b: labels.extend(b["labels"])})
        for k in (1, 2, 3):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(self.decide(GitHubError(502, "bad gateway"), tmp)[0], 3)
                with mock.patch("scripts.reports.GitHub", lambda token: g), \
                        mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}):
                    self.assertEqual(main(["undecided", "--file", os.path.join(tmp, "undecided.json")]), 0)
        self.assertEqual(labels, ["needs-review"])
        self.assertTrue(comments[1]["body"].startswith(ERROR))

    def test_failed_rot_dispatch_is_counted_too(self):
        def boom(_):
            raise GitHubError(502, "bad gateway")

        g = FakeGitHub({f"POST {API}/actions/workflows/rot.yml/dispatches": boom})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("scripts.reports.GitHub", lambda token: g), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}), \
                mock.patch("sys.stderr", io.StringIO()):
            res, und = os.path.join(tmp, "result.json"), os.path.join(tmp, "undecided.json")
            write(res, {"issue": 7, "actions": [{"kind": "dispatch_rot", "value": "alice/tool"}]})
            self.assertEqual(main(["dispatch", "--result", res, "--undecided", und]), 3)
            with open(und) as f:
                self.assertEqual(json.load(f), {"issue": 7, "error": "GitHub API 502: bad gateway"})

    def test_rate_limit_still_in_force_spends_no_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RateLimited):
                self.decide(RateLimited(int(time.time()) + 600), tmp)
            self.assertFalse(os.path.exists(os.path.join(tmp, "undecided.json")))


class CliTest(unittest.TestCase):
    def test_dispatch_fetches_issue_and_validates_input(self):
        for bad in ("7; rm -rf /", "-1", "", "٧", "7\n"):
            g = gh()
            with tempfile.TemporaryDirectory() as tmp, \
                    mock.patch("scripts.reports.GitHub", lambda token: g), \
                    mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}):
                with self.assertRaises(SystemExit):
                    main(["decide", "--issue", bad, "--out", os.path.join(tmp, "r.json")])
            self.assertEqual(g.calls, [], bad)

    def decide(self, g, *args):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("scripts.reports.GitHub", lambda token: g), \
                mock.patch("scripts.reports.load", lambda: [entry()]), \
                mock.patch("scripts.reports.datetime") as dt, \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "EXCLUDED_USERS": ""}):
            dt.now.return_value = NOW
            out = os.path.join(tmp, "result.json")
            self.assertEqual(main(["decide", *args, "--out", out]), 0)
            if not os.path.exists(out):
                return None
            with open(out) as f:
                return json.load(f)

    def test_dispatch_skips_handled_or_foreign_issues(self):
        cases = [
            dict(report(), labels=[{"name": "report"}, {"name": "needs-review"}]),
            dict(report(), state="closed"),
            dict(report(), labels=[{"name": "bug"}]),
            dict(report(), pull_request={}),
            report(login=BOT, user_type="Bot"),
        ]
        for i in cases:
            g = gh(extra={f"GET {API}/issues/7": i})
            self.assertIsNone(self.decide(g, "--issue", "7"), i)

    def test_decide_writes_actions(self):
        i = report(reason="Dead or abandoned")
        g = gh(extra={f"GET {API}/issues/7": i, f"GET {API}/issues": [i]})
        r = self.decide(g, "--issue", "7")
        self.assertEqual(r["issue"], 7)
        self.assertIn({"kind": "dispatch_rot", "value": "alice/tool"}, r["actions"])

    def test_dead_dispatches_rot_with_repo(self):
        g = FakeGitHub({f"POST {API}/actions/workflows/rot.yml/dispatches": None})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("scripts.reports.GitHub", lambda token: g), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}):
            path = os.path.join(tmp, "r.json")
            write(path, {"issue": 7, "actions": [{"kind": "dispatch_rot", "value": "alice/tool"},
                                                 {"kind": "label", "value": "needs-review"}]})
            self.assertEqual(main(["dispatch", "--result", path]), 0)
            write(path, {"issue": 7, "actions": [{"kind": "label", "value": "needs-review"}]})
            self.assertEqual(main(["dispatch", "--result", path]), 0)
        self.assertEqual(g.calls, [("POST", f"{API}/actions/workflows/rot.yml/dispatches",
                                    {"ref": "main", "inputs": {"repo": "alice/tool"}})])

    def test_issue_posts_labels_comment_and_close(self):
        g = FakeGitHub({f"POST {API}/issues/7/labels": None, f"POST {API}/issues/7/comments": None,
                        f"PATCH {API}/issues/7": None})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("scripts.reports.GitHub", lambda token: g), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}):
            path = os.path.join(tmp, "r.json")
            write(path, {"issue": 7, "actions": [{"kind": "label", "value": "needs-review"},
                                                 {"kind": "comment", "value": "hi"},
                                                 {"kind": "close", "value": None},
                                                 {"kind": "hide", "value": "alice/tool"}]})
            self.assertEqual(main(["issue", "--result", path]), 0)
            self.assertEqual(main(["issue", "--result", path, "--error"]), 0)
        self.assertEqual(g.calls, [
            ("POST", f"{API}/issues/7/labels", {"labels": ["needs-review"]}),
            ("POST", f"{API}/issues/7/comments", {"body": "hi"}),
            ("PATCH", f"{API}/issues/7", {"state": "closed", "state_reason": "not_planned"}),
            ("POST", f"{API}/issues/7/labels", {"labels": ["needs-review"]}),
            ("POST", f"{API}/issues/7/comments", {"body": mock.ANY}),
        ])
        self.assertIn("went wrong on our side", g.calls[-1][2]["body"])

    def test_hide_pr_touches_only_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "entries.json")
            save([entry("zed/zed"), entry("Alice/Tool")], path)
            before = load(path)
            result = os.path.join(tmp, "result.json")
            write(result, {"issue": 7, "actions": [{"kind": "hide", "value": "alice/tool"}]})
            with mock.patch("sys.stdout") as out:
                self.assertEqual(main(["apply", "--result", result, "--entries", path]), 0)
            self.assertEqual(out.write.call_args_list[0].args[0].strip(), "report/7")
            after = load(path)
            self.assertEqual([e["status"] for e in after], ["hidden", "live"])
            self.assertEqual([dict(e, status=None) for e in after], [dict(e, status=None) for e in before])
            self.assertEqual(sorted(os.listdir(tmp)), ["entries.json", "result.json"])

            # Already hidden on fresh main: no branch, nothing to commit.
            with mock.patch("sys.stdout") as out:
                self.assertEqual(main(["apply", "--result", result, "--entries", path]), 0)
            self.assertEqual("".join(c.args[0] for c in out.write.call_args_list).strip(), "")

            # Gone from fresh main while the job queued: fail without a branch or a write.
            save([entry("zed/zed")], path)
            before = open(path, encoding="utf-8").read()
            with mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
                self.assertEqual(main(["apply", "--result", result, "--entries", path]), 1)
            self.assertEqual("".join(c.args[0] for c in out.write.call_args_list), "")
            self.assertIn("alice/tool is no longer on the list", "".join(c.args[0] for c in err.write.call_args_list))
            self.assertEqual(open(path, encoding="utf-8").read(), before)

        # The workflow commits entries.json and nothing else, so auto-approve accepts the PR.
        text = WORKFLOW.read_text()
        self.assertIn("git add data/entries.json\n", text)
        self.assertEqual(text.count("git add"), 1)
        self.assertIn('subject="chore(list): hide $REPO_NAME pending review"', text)


if __name__ == "__main__":
    unittest.main()
