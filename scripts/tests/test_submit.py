import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

from scripts.devstream.entries import save
from scripts.devstream.gh import GitHubError, RateLimited
from scripts.submit import ARCHIVED_DUP, DUPLICATE, RATE_LIMIT, comment, decide, main
from scripts.tests.fake_gh import FakeGitHub
from scripts.tests.test_quality import README, b64, iso, repo

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
R = "/repos/alice/tool"
SEARCH = (
    "GET /search/issues?q=repo:devstreamhq/devstream+label:submission"
    "+author:carol+created:>=2026-10-29"
)


def body(repo_="https://github.com/alice/tool", category="Developer Tools",
         description="A small tool that does one thing well"):
    return (
        f"### Repository\n\n{repo_}\n\n### Category\n\n{category}\n\n"
        f"### Description\n\n{description}\n\n### Checklist\n\n"
        "- [X] I read the quality bar\n- [X] This repo is open source\n"
    )


def issue(b=None, title="Submit: alice/tool"):
    return {"number": 7, "title": title, "user": {"login": "carol"}, "body": body() if b is None else b}


def gh(count=1, repo_kw=None, extra=None):
    routes = {
        "GET /orgs/devstreamhq/blocks": [],
        "GET /users/carol": {"created_at": iso(NOW - timedelta(days=400)), "public_repos": 3, "type": "User"},
        SEARCH: {"total_count": count, "items": []},
        f"GET {R}": repo(**(repo_kw or {})),
        f"GET {R}/community/profile": {"files": {"contributing": {}, "code_of_conduct": {}}},
        f"GET {R}/readme": b64(README),
    }
    routes.update(extra or {})
    return FakeGitHub(routes)


def entry(repo_="alice/tool", status="live"):
    return {
        "repo": repo_, "category": "developer-tools", "description": "An older tool that still works.",
        "submitted_by": "dave", "submitted_at": "2026-01-01T00:00:00Z", "issue": 1,
        "status": status, "warned_at": None, "superseded_by": None, "added_by_seed": False,
    }


def run(i=None, g=None, entries=()):
    return decide(issue() if i is None else i, g or gh(), NOW, list(entries), set())


class DecideTest(unittest.TestCase):
    def test_happy_path_returns_entry(self):
        d = run()
        self.assertTrue(d.ok)
        self.assertEqual(d.reasons, [])
        self.assertEqual(d.entry, {
            "repo": "alice/tool", "category": "developer-tools",
            "description": "A small tool that does one thing well.",
            "submitted_by": "carol", "submitted_at": "2026-11-05T12:00:00Z", "issue": 7,
            "status": "live", "warned_at": None, "superseded_by": None, "added_by_seed": False,
        })

    def test_duplicate_case_variant_rejected(self):
        for link in ("Alice/Tool", "https://github.com/ALICE/tool.git", "alice/tool/"):
            g = gh()
            d = run(issue(body(repo_=link)), g, [entry()])
            self.assertFalse(d.ok, link)
            self.assertEqual(d.reasons, ["This repo is already on the list."])
            # Caught before the repo is fetched, so no API calls are spent on it.
            self.assertNotIn(("GET", R, None), g.calls)

    def test_duplicate_after_rename_rejected(self):
        g = gh(repo_kw={"full_name": "alice/tool2"})
        d = run(g=g, entries=[entry("alice/tool2")])
        self.assertFalse(d.ok)
        self.assertIn("This repo is already on the list.", d.reasons)

    def test_archived_duplicate_message(self):
        for status in ("archived", "superseded"):
            d = run(entries=[entry(status=status)])
            self.assertEqual(d.reasons, [ARCHIVED_DUP])
        self.assertEqual(
            ARCHIVED_DUP,
            "This repo was listed before and is in the archive. "
            "It comes back automatically once it's active again.",
        )

    def test_fourth_submission_in_week_rejected(self):
        self.assertTrue(run(g=gh(count=3)).ok)
        d = run(g=gh(count=4))
        self.assertFalse(d.ok)
        self.assertEqual(d.reasons, [RATE_LIMIT])
        self.assertEqual(RATE_LIMIT, "You've hit the limit of 3 submissions a week. Try again in a few days.")

    def test_collects_multiple_repo_failures(self):
        g = gh(repo_kw={"archived": True, "license": None, "pushed_at": iso(NOW - timedelta(days=200))})
        d = run(issue(body(description="short")), g)
        self.assertFalse(d.ok)
        self.assertIsNone(d.entry)
        self.assertGreaterEqual(len(d.reasons), 3)
        self.assertIn("This repo is archived. Unarchive it to be listed.", d.reasons)
        self.assertIn("This repo hasn't had a push in the last 90 days.", d.reasons)
        self.assertTrue(any("license" in r for r in d.reasons))

    def test_form_failure_stops_early(self):
        g = gh()
        d = run(issue("### Repository\n\nalice/tool\n"), g)
        self.assertFalse(d.ok)
        self.assertEqual(len(d.reasons), 1)
        self.assertIn("incomplete", d.reasons[0])
        self.assertEqual(g.calls, [])

    def test_trust_failure_stops_early(self):
        g = gh(extra={"GET /users/carol": {"created_at": iso(NOW - timedelta(days=3)), "public_repos": 3, "type": "User"}})
        d = run(g=g, entries=[entry()])
        self.assertEqual(d.reasons, ["Your account needs to be at least 30 days old."])

    def test_api_failure_raises_not_rejects(self):
        def limited(_):
            raise RateLimited(0)

        with self.assertRaises(RateLimited):
            run(g=gh(extra={SEARCH: limited}))
        with self.assertRaises(RateLimited):
            run(g=gh(extra={f"GET {R}": limited}))

    def test_block_access_checked(self):
        g = gh()
        del g.routes["GET /orgs/devstreamhq/blocks"]
        with self.assertRaises(GitHubError):
            run(g=g)

    def test_description_falls_back_to_repo_description(self):
        d = run(issue(body(description="Buy now at https://spam.example")),
                gh(repo_kw={"description": "A handy tool for people"}))
        self.assertTrue(d.ok, d.reasons)
        self.assertEqual(d.entry["description"], "A handy tool for people.")

    def test_both_descriptions_fail_gives_submitter_reason(self):
        d = run(issue(body(description="short")), gh(repo_kw={"description": "Visit https://x.example now"}))
        self.assertEqual(d.reasons, ["Descriptions need to be between 10 and 140 characters."])


class CommentTest(unittest.TestCase):
    def test_accepted_text(self):
        d = run()
        text = comment({"ok": True, "reasons": [], "entry": d.entry, "issue": 7, "author": "carol"}, "accepted")
        self.assertEqual(
            text.strip(),
            "🎉 `alice/tool` is on the list. It's live at https://devstream.jamesconway.dev, "
            "and Likes start counting as soon as its Discussion opens.",
        )

    def test_rejected_lists_reasons_and_links_quality_bar(self):
        text = comment({"ok": False, "reasons": ["One.", "Two."], "entry": None, "issue": 7, "author": "carol"}, "rejected")
        self.assertIn("- One.\n- Two.\n", text)
        self.assertIn("https://github.com/devstreamhq/devstream/wiki/Quality-Bar", text)
        self.assertNotIn("bot", text.lower())

    def test_comment_never_echoes_issue_body(self):
        evil = "<script>alert(1)</script> @everyone"
        ok = run(issue(body(description=evil), title=evil))
        self.assertTrue(ok.ok, ok.reasons)
        bad = run(issue(f"### Repository\n\n{evil}\n", title=evil))
        self.assertFalse(bad.ok)
        for d, kind in ((ok, "accepted"), (bad, "rejected")):
            text = comment({"ok": d.ok, "reasons": d.reasons, "entry": d.entry, "issue": 7, "author": "carol"}, kind)
            self.assertNotIn("<script>", text)
            self.assertNotIn("@everyone", text)
            self.assertNotIn("alert", text)


class PrBodyTest(unittest.TestCase):
    def test_pr_body_puts_description_in_code_span(self):
        e = dict(run().entry, description="Pings @everyone when it runs.")
        text = comment({"ok": True, "reasons": [], "entry": e, "issue": 7, "author": "carol"}, "pr")
        self.assertIn("Closes #7\n", text)
        self.assertIn("`Pings @everyone when it runs.`", text)
        self.assertIn("Developer Tools", text)


def write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def issue_api(number=7, labels=("submission",), state="open", **kw):
    return dict(issue(), number=number, state=state, labels=[{"name": n} for n in labels], **kw)


class DecideCliTest(unittest.TestCase):
    def main(self, g, *args):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("scripts.submit.GitHub", lambda token: g), \
                mock.patch("scripts.submit.load", lambda: []), \
                mock.patch("scripts.submit.datetime") as dt, \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "EXCLUDED_USERS": ""}):
            dt.now.return_value = NOW
            out = os.path.join(tmp, "result.json")
            try:
                code = main(["decide", *args, "--out", out])
            finally:
                self.wrote = os.path.exists(out)
            if self.wrote:
                with open(out) as f:
                    self.result = json.load(f)
            return code

    def test_api_error_exits_nonzero_without_result(self):
        def limited(_):
            raise RateLimited(0)

        with tempfile.TemporaryDirectory() as tmp:
            ev = os.path.join(tmp, "event.json")
            write(ev, {"issue": issue()})
            with self.assertRaises(RateLimited):
                self.main(gh(extra={SEARCH: limited}), "--event", ev)
        self.assertFalse(self.wrote)

    def test_dispatch_fetches_issue(self):
        g = gh(extra={"GET /repos/devstreamhq/devstream/issues/7": issue_api()})
        self.assertEqual(self.main(g, "--issue", "7"), 0)
        self.assertTrue(self.result["ok"], self.result)
        self.assertEqual((self.result["issue"], self.result["author"]), (7, "carol"))

    def test_dispatch_rejects_non_digit_input(self):
        for bad in ("7; rm -rf /", "-1", "", "٧", "7\n"):
            g = gh()
            with self.assertRaises(SystemExit):
                self.main(g, "--issue", bad)
            self.assertEqual(g.calls, [], bad)
            self.assertFalse(self.wrote)

    def test_dispatch_skips_handled_or_foreign_issues(self):
        cases = [
            issue_api(labels=("submission", "accepted")),
            issue_api(labels=("submission", "rejected")),
            issue_api(state="closed"),
            issue_api(labels=("bug",)),
            issue_api(pull_request={}),
        ]
        for i in cases:
            g = gh(extra={"GET /repos/devstreamhq/devstream/issues/7": i})
            self.assertEqual(self.main(g, "--issue", "7"), 0)
            self.assertFalse(self.wrote, i)
        g = gh()
        self.assertEqual(self.main(g, "--issue", "7"), 0)
        self.assertFalse(self.wrote)


class CliTest(unittest.TestCase):
    def test_apply_duplicate_on_fresh_main_routes_to_reject(self):
        for status, reason in (("live", DUPLICATE), ("archived", ARCHIVED_DUP)):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "entries.json")
                save([entry("Alice/Tool", status=status)], path)
                result = os.path.join(tmp, "result.json")
                write(result, {"ok": True, "reasons": [], "entry": run().entry, "issue": 7, "author": "carol"})
                p = subprocess.run([sys.executable, "-m", "scripts.submit", "apply", "--result", result,
                                    "--entries", path], capture_output=True, text=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(p.stdout.strip(), "")
                with open(path) as f:
                    self.assertEqual(len(json.load(f)["entries"]), 1)
                with open(result) as f:
                    self.assertEqual(json.load(f), {"ok": False, "reasons": [reason], "entry": None,
                                                    "issue": 7, "author": "carol"})

    def test_apply_appends_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "entries.json")
            save([entry("zed/zed")], path)
            result = os.path.join(tmp, "result.json")
            d = run()
            with open(result, "w") as f:
                json.dump({"ok": True, "reasons": [], "entry": d.entry, "issue": 7, "author": "carol"}, f)
            cmd = [sys.executable, "-m", "scripts.submit", "apply", "--result", result, "--entries", path]
            p = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.strip(), "submit/7")
            with open(path) as f:
                repos = [e["repo"] for e in json.load(f)["entries"]]
            self.assertEqual(repos, ["alice/tool", "zed/zed"])

            # A result that would break validation exits 1.
            write(result, {"ok": True, "reasons": [], "entry": dict(d.entry, repo="bob/x", category="nope"),
                           "issue": 8, "author": "carol"})
            p = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(p.returncode, 1)
            self.assertIn("unknown category", p.stderr)

    def test_apply_refuses_rejected_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "entries.json")
            save([], path)
            result = os.path.join(tmp, "result.json")
            with open(result, "w") as f:
                json.dump({"ok": False, "reasons": ["x"], "entry": None, "issue": 7, "author": "carol"}, f)
            p = subprocess.run([sys.executable, "-m", "scripts.submit", "apply", "--result", result,
                                "--entries", path], capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
