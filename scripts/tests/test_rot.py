import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.devstream.entries import load, save
from scripts.devstream.gh import RateLimited
from scripts.devstream.quality import RepoResult, Undecided, check_repo
from scripts.rot import Action, finish, plan_rot, pr_body, prepare, select
from scripts.tests.fake_gh import FakeGitHub, Redirect

NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)
API = "/repos/devstreamhq/devstream"


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def entry(repo="alice/tool", status="live", warned_at=None, submitted_at="2026-01-01T00:00:00Z", issue=1):
    return {
        "repo": repo, "category": "developer-tools", "description": "An older tool that still works.",
        "submitted_by": "dave", "submitted_at": submitted_at, "issue": issue,
        "status": status, "warned_at": warned_at, "superseded_by": None, "added_by_seed": False,
    }


def ok(repo="alice/tool"):
    return RepoResult(True, repo, [], "ok")


def warn(repo="alice/tool"):
    return RepoResult(True, repo, ["This repo hasn't had a push in over a year."], "warn")


def gone(repo="alice/tool"):
    return RepoResult(False, repo, ["This repo no longer exists."], "remove")


def kinds(actions):
    return [(a.kind, a.repo) for a in actions]


class PlanTest(unittest.TestCase):
    def test_warn_then_archive_after_30_days(self):
        new, actions, _ = plan_rot([entry()], {"alice/tool": warn()}, NOW, {})
        self.assertEqual(new[0]["status"], "warned")
        self.assertEqual(new[0]["warned_at"], iso(NOW))
        self.assertEqual(kinds(actions), [("warn", "alice/tool")])
        self.assertIn("archive on", actions[0].message)

        later = NOW + timedelta(days=29)
        again, actions, _ = plan_rot(new, {"alice/tool": warn()}, later, {})
        self.assertEqual(again, new)
        self.assertEqual(actions, [])

        later = NOW + timedelta(days=30)
        archived, actions, _ = plan_rot(new, {"alice/tool": warn()}, later, {})
        self.assertEqual(archived[0]["status"], "archived")
        self.assertEqual(kinds(actions), [("archive", "alice/tool")])

        # Already archived and still stale: nothing more to say.
        _, actions, _ = plan_rot(archived, {"alice/tool": warn()}, later + timedelta(days=7), {})
        self.assertEqual(actions, [])

    def test_revive_clears_warning(self):
        for status in ("warned", "archived"):
            new, actions, _ = plan_rot([entry(status=status, warned_at=iso(NOW))], {"alice/tool": ok()}, NOW, {})
            self.assertEqual(new[0]["status"], "live", status)
            self.assertIsNone(new[0]["warned_at"])
            self.assertEqual(kinds(actions), [("revive", "alice/tool")])
            self.assertIn("Welcome back", actions[0].message)

    def test_404_removes(self):
        new, actions, rot = plan_rot([entry()], {"alice/tool": gone()}, NOW, {})
        self.assertEqual(new, [entry()])
        self.assertEqual(actions, [])
        self.assertEqual(rot["alice/tool"]["first_seen"], iso(NOW))
        self.assertIn("no longer exists", rot["alice/tool"]["reason"])

        new, actions, rot = plan_rot([entry()], {"alice/tool": gone()}, NOW + timedelta(days=7), rot)
        self.assertEqual(new, [])
        self.assertEqual(kinds(actions), [("remove", "alice/tool")])
        self.assertEqual(rot, {})

    def test_remove_needs_two_runs_six_days_apart(self):
        state = {"alice/tool": {"first_seen": iso(NOW), "reason": "x"}}
        licence = RepoResult(True, "alice/tool", ["We need an open-source license"], "remove")
        new, actions, rot = plan_rot([entry()], {"alice/tool": licence}, NOW + timedelta(days=5, hours=23), state)
        self.assertEqual(new, [entry()])
        self.assertEqual(actions, [])
        self.assertEqual(rot, state)

        new, actions, rot = plan_rot([entry()], {"alice/tool": licence}, NOW + timedelta(days=6), state)
        self.assertEqual(new, [])
        self.assertEqual(kinds(actions), [("remove", "alice/tool")])

    def test_recovery_clears_rot_state(self):
        state = {"alice/tool": {"first_seen": iso(NOW), "reason": "x"}}
        for r in (ok(), warn()):
            _, _, rot = plan_rot([entry()], {"alice/tool": r}, NOW + timedelta(days=7), state)
            self.assertEqual(rot, {}, r.rot)

    def test_stale_rot_record_for_unlisted_repo_is_dropped(self):
        # Otherwise a resubmitted repo would skip its week of grace.
        state = {"gone/away": {"first_seen": iso(NOW - timedelta(days=90)), "reason": "x"}}
        _, _, rot = plan_rot([entry()], {"alice/tool": ok()}, NOW, state)
        self.assertEqual(rot, {})

    def test_undecided_repo_unchanged(self):
        state = {"alice/tool": {"first_seen": iso(NOW - timedelta(days=30)), "reason": "x"}}
        e = entry(status="warned", warned_at=iso(NOW - timedelta(days=60)))
        new, actions, rot = plan_rot([e], {}, NOW, state)
        self.assertEqual((new, actions, rot), ([e], [], state))

    def test_rename_updates_repo(self):
        new, actions, _ = plan_rot([entry()], {"alice/tool": ok("alice/tool-ng")}, NOW, {})
        self.assertEqual(new[0]["repo"], "alice/tool-ng")
        self.assertEqual([(a.kind, a.key, a.repo) for a in actions], [("rename", "alice/tool", "alice/tool-ng")])

    def test_rename_moves_rot_record(self):
        state = {"alice/tool": {"first_seen": iso(NOW), "reason": "x"}}
        licence = RepoResult(True, "alice/tool-ng", ["no license"], "remove")
        new, _, rot = plan_rot([entry()], {"alice/tool": licence}, NOW + timedelta(days=1), state)
        self.assertEqual(new[0]["repo"], "alice/tool-ng")
        self.assertEqual(rot, {"alice/tool-ng": state["alice/tool"]})

    def test_rename_collision_removes_newer(self):
        older = entry("bob/kept", submitted_at="2026-01-01T00:00:00Z", issue=1)
        newer = entry("alice/tool", submitted_at="2026-03-01T00:00:00Z", issue=2)
        new, actions, _ = plan_rot([older, newer], {"alice/tool": ok("bob/kept"), "bob/kept": ok("bob/kept")}, NOW, {})
        self.assertEqual(new, [older])
        self.assertEqual(kinds(actions), [("remove", "alice/tool")])

        # Older one renamed onto the newer one's name: the older is kept and takes the name.
        older = entry("alice/tool", submitted_at="2026-01-01T00:00:00Z", issue=1)
        newer = entry("bob/kept", submitted_at="2026-03-01T00:00:00Z", issue=2)
        new, actions, _ = plan_rot([older, newer], {"alice/tool": ok("bob/kept")}, NOW, {})
        self.assertEqual([e["issue"] for e in new], [1])
        self.assertEqual(new[0]["repo"], "bob/kept")
        self.assertEqual(sorted(kinds(actions)), [("remove", "bob/kept"), ("rename", "bob/kept")])

    def test_rename_onto_hidden_entry_changes_nothing(self):
        hidden = entry("bob/kept", status="hidden", issue=2)
        e = entry()
        new, actions, _ = plan_rot([e, hidden], {"alice/tool": ok("bob/kept")}, NOW, {})
        self.assertEqual((new, actions), ([e, hidden], []))

    def test_case_only_rename_keeps_stats_key(self):
        new, actions, _ = plan_rot([entry()], {"alice/tool": ok("Alice/Tool")}, NOW, {})
        self.assertEqual(new[0]["repo"], "Alice/Tool")
        self.assertEqual([(a.kind, a.key) for a in actions], [("rename", "alice/tool")])


def routes(rot=None, discussions=None, flags=None, extra=None):
    def file(data):
        if data is None:
            return None
        return {"content": base64.b64encode(json.dumps(data).encode()).decode(), "sha": "s"}

    r = {
        f"GET {API}/contents/rot.json": file(rot),
        f"GET {API}/contents/discussions.json": file(discussions),
        f"GET {API}/contents/flags.json": file(flags),
        f"PUT {API}/contents/rot.json": None,
        f"PUT {API}/contents/discussions.json": None,
        f"PUT {API}/contents/flags.json": None,
        f"GET {API}/git/ref/heads/stats": {"ref": "refs/heads/stats"},
    }
    r.update(extra or {})
    return r


def written(g, path):
    bodies = [b for m, p, b in g.calls if m == "PUT" and p == f"{API}/contents/{path}"]
    return [json.loads(base64.b64decode(b["content"])) for b in bodies]


def writes(g):
    return [c for c in g.calls if c[0] in ("POST", "PUT", "PATCH", "DELETE")
            or (c[0] == "GRAPHQL" and c[1] in ("CommentOnEntry", "UpdateEntryDiscussion"))]


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "entries.json")
        save([entry(), entry("bob/other", issue=2)], self.path)

    def run_prepare(self, g, check, repo=None):
        return prepare(g, NOW, self.path, repo, check=check)

    def test_rate_limited_aborts_with_no_actions(self):
        before = Path(self.path).read_text()

        def check(full, gh, now, mode):
            if full == "bob/other":
                raise RateLimited(0)
            return warn(full)

        g = FakeGitHub(routes())
        with self.assertRaises(RateLimited):
            self.run_prepare(g, check)
        self.assertEqual(Path(self.path).read_text(), before)
        self.assertEqual(writes(g), [])

    def test_undecided_repo_is_skipped_not_failed(self):
        def check(full, gh, now, mode):
            if full == "alice/tool":
                raise Undecided(0, "no pushed_at")
            return warn(full)

        plan = self.run_prepare(FakeGitHub(routes()), check)
        self.assertEqual(kinds([Action(**a) for a in plan["actions"]]), [("warn", "bob/other")])
        self.assertEqual({e["repo"]: e["status"] for e in load(self.path)}, {"alice/tool": "live", "bob/other": "warned"})

    def test_skips_hidden_and_superseded(self):
        save([entry(status="hidden"), entry("bob/other", status="superseded", issue=2)], self.path)
        seen = []
        self.run_prepare(FakeGitHub(routes()), lambda full, *a: seen.append(full) or ok(full))
        self.assertEqual(seen, [])

    def test_unchanged_entries_no_pr(self):
        before = Path(self.path).read_text()
        g = FakeGitHub(routes())
        plan = self.run_prepare(g, lambda full, *a: ok(full))
        self.assertFalse(plan["changed"])
        self.assertEqual(Path(self.path).read_text(), before)
        self.assertEqual(writes(g), [])

        # A first sighting changes only rot.json, which is written straight away.
        g = FakeGitHub(routes())
        plan = self.run_prepare(g, lambda full, *a: gone(full) if full == "alice/tool" else ok(full))
        self.assertFalse(plan["changed"])
        self.assertEqual(Path(self.path).read_text(), before)
        self.assertEqual(list(written(g, "rot.json")[0]), ["alice/tool"])
        self.assertEqual([c[1] for c in writes(g)], [f"{API}/contents/rot.json"])

    def test_changed_entries_defer_every_write(self):
        g = FakeGitHub(routes())
        plan = self.run_prepare(g, lambda full, *a: gone(full) if full == "alice/tool" else warn(full))
        self.assertTrue(plan["changed"])
        self.assertEqual(writes(g), [])
        self.assertEqual(list(plan["rot_state"]), ["alice/tool"])

    def test_invalid_new_entries_abort(self):
        g = FakeGitHub(routes())
        before = Path(self.path).read_text()
        with self.assertRaises(SystemExit):
            self.run_prepare(g, lambda full, *a: ok("not a valid/name!" if full == "alice/tool" else full))
        self.assertEqual(Path(self.path).read_text(), before)

    def test_redirect_is_a_rename_not_a_404(self):
        readme = base64.b64encode(("A tool that does a useful thing for people. " * 10).encode()).decode()
        g = FakeGitHub(routes(extra={
            "GET /repos/alice/tool": Redirect("/repositories/1"),
            "GET /repositories/1": {"full_name": "alice/tool-ng", "pushed_at": iso(NOW - timedelta(days=3)),
                                    "license": {"spdx_id": "MIT"}, "archived": False, "description": "A tool."},
            "GET /repos/alice/tool-ng/readme": {"content": readme, "encoding": "base64"},
        }))
        save([entry()], self.path)
        plan = self.run_prepare(g, check_repo)
        self.assertEqual(kinds([Action(**a) for a in plan["actions"]]), [("rename", "alice/tool-ng")])
        self.assertEqual(plan["rot_state"], {})
        self.assertEqual(load(self.path)[0]["repo"], "alice/tool-ng")

    def test_single_repo_dispatch_validates_input(self):
        entries = [entry(), entry("bob/other", issue=2)]
        self.assertEqual(select(entries, None), entries)
        self.assertEqual(select(entries, "Bob/Other"), [entries[1]])
        self.assertEqual(select(entries, "nobody/here"), [])
        for bad in ("bob", "bob/other/x", "-x/$(id)", "a/b c", "a" * 40 + "/b", "bob/other\n", ""):
            with self.assertRaises(SystemExit, msg=bad):
                select(entries, bad)

        seen = []
        self.run_prepare(FakeGitHub(routes()), lambda full, *a: seen.append(full) or ok(full), repo="bob/other")
        self.assertEqual(seen, ["bob/other"])


class BodyTest(unittest.TestCase):
    def test_pr_body_has_no_free_text(self):
        actions = [
            Action("warn", "alice/tool", "SECRET message @someone", "alice/tool"),
            Action("rename", "alice/tool-ng", "", "alice/tool"),
            Action("remove", "bob/gone", "SECRET", "bob/gone"),
        ]
        body = pr_body(actions)
        self.assertNotIn("SECRET", body)
        self.assertNotIn("@", body)
        for repo in ("alice/tool", "alice/tool-ng", "bob/gone"):
            self.assertIn(f"`{repo}`", body)


class FinishTest(unittest.TestCase):
    def plan(self, actions, rot_state=None):
        return {"changed": True, "actions": [a.__dict__ for a in actions], "rot_state": rot_state or {}}

    def gql(self, g, op):
        return [v for m, o, v in g.calls if m == "GRAPHQL" and o == op]

    def test_rename_moves_stats_keys(self):
        r = routes(
            rot={"alice/tool": {"first_seen": "x", "reason": "y"}},
            discussions={"alice/tool": 5, "bob/other": 6},
            flags={"alice/tool": {"burst": {"issue": 3}}},
            extra={
                "GRAPHQL EntryDiscussionId": {"repository": {"discussion": {"id": "D5"}}},
                "GRAPHQL UpdateEntryDiscussion": {"updateDiscussion": {"discussion": {"number": 5}}},
            },
        )
        g = FakeGitHub(r)
        finish(g, self.plan([Action("rename", "alice/tool-ng", "", "alice/tool")],
                            {"alice/tool-ng": {"first_seen": "x", "reason": "y"}}))
        self.assertEqual(written(g, "discussions.json"), [{"alice/tool-ng": 5, "bob/other": 6}])
        self.assertEqual(written(g, "flags.json"), [{"alice/tool-ng": {"burst": {"issue": 3}}}])
        self.assertEqual(written(g, "rot.json"), [{"alice/tool-ng": {"first_seen": "x", "reason": "y"}}])
        self.assertEqual(self.gql(g, "UpdateEntryDiscussion"), [{"discussionId": "D5", "title": "alice/tool-ng"}])
        self.assertEqual(self.gql(g, "CommentOnEntry"), [])
        # Stats land before anything is said or retitled.
        last_put = max(i for i, c in enumerate(g.calls) if c[0] == "PUT")
        first_gql = min(i for i, c in enumerate(g.calls) if c[0] == "GRAPHQL")
        self.assertLess(last_put, first_gql)

    def test_comments_on_each_entry_discussion(self):
        r = routes(
            discussions={"alice/tool": 5},
            extra={
                "GRAPHQL EntryDiscussionId": lambda v: {"repository": {"discussion": {"id": f"D{v['number']}"}}},
                "GRAPHQL CommentOnEntry": {"addDiscussionComment": {"comment": {"id": "c"}}},
            },
        )
        g = FakeGitHub(r)
        finish(g, self.plan([Action("warn", "alice/tool", "Heads up", "alice/tool"),
                             Action("revive", "no/thread", "Welcome back", "no/thread")]))
        self.assertEqual(self.gql(g, "CommentOnEntry"), [{"discussionId": "D5", "body": "Heads up"}])
        self.assertEqual(written(g, "discussions.json"), [])
        self.assertEqual(written(g, "rot.json"), [])


if __name__ == "__main__":
    unittest.main()
