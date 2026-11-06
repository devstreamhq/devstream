import base64
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.devstream.entries import load, save
from scripts.devstream.gh import GitHubError, RateLimited
from scripts.devstream.quality import (
    MSG_LICENSE,
    MSG_NO_README,
    MSG_SHORT_README,
    RepoResult,
    Undecided,
    check_repo,
)
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
        self.assertEqual(kinds(actions), [("notice", "alice/tool")])
        self.assertEqual(rot["alice/tool"]["first_seen"], iso(NOW))
        self.assertIn("no longer exists", rot["alice/tool"]["reason"])
        self.assertNotIn("notified", rot["alice/tool"])
        rot["alice/tool"]["notified"] = True  # finish sets this once the notice is posted

        new, actions, rot = plan_rot([entry()], {"alice/tool": gone()}, NOW + timedelta(days=7), rot)
        self.assertEqual(new, [])
        self.assertEqual(kinds(actions), [("remove", "alice/tool")])
        self.assertEqual(rot, {})

    def test_first_removal_sighting_names_the_reason(self):
        for failure in ("This repo no longer exists.", MSG_LICENSE, MSG_NO_README, MSG_SHORT_README):
            r = RepoResult(failure != "This repo no longer exists.", "alice/tool",
                           [failure, "This repo hasn't had a push in over a year."], "remove")
            _, actions, _ = plan_rot([entry()], {"alice/tool": r}, NOW, {})
            self.assertEqual(kinds(actions), [("notice", "alice/tool")])
            msg = actions[0].message
            self.assertIn(failure, msg)
            self.assertNotIn("push in over a year", msg)
            self.assertIn("before 11 November 2026", msg)  # NOW + 6 days, the earliest removal date

    def test_stale_first_sighting_starts_over(self):
        # One blip 90 days after an old sighting must not remove anything.
        state = {"alice/tool": {"first_seen": iso(NOW - timedelta(days=90)), "reason": "x", "notified": True}}
        new, actions, rot = plan_rot([entry()], {"alice/tool": gone()}, NOW, state)
        self.assertEqual(new, [entry()])
        self.assertEqual(kinds(actions), [("notice", "alice/tool")])
        self.assertEqual(rot["alice/tool"]["first_seen"], iso(NOW))
        self.assertNotIn("notified", rot["alice/tool"])

        # 14 days is still the same sighting.
        state["alice/tool"]["first_seen"] = iso(NOW - timedelta(days=14))
        new, actions, _ = plan_rot([entry()], {"alice/tool": gone()}, NOW, state)
        self.assertEqual((new, kinds(actions)), ([], [("remove", "alice/tool")]))

    def test_unnotified_sighting_never_removes(self):
        state = {"alice/tool": {"first_seen": iso(NOW - timedelta(days=7)), "reason": "x"}}
        new, actions, rot = plan_rot([entry()], {"alice/tool": gone()}, NOW, state)
        self.assertEqual(new, [entry()])
        self.assertEqual(kinds(actions), [("notice", "alice/tool")])
        self.assertEqual(rot["alice/tool"]["first_seen"], iso(NOW))

    def test_remove_needs_two_runs_six_days_apart(self):
        state = {"alice/tool": {"first_seen": iso(NOW), "reason": "x", "notified": True}}
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
        state = {"alice/tool": {"first_seen": iso(NOW), "reason": "x", "notified": True}}
        licence = RepoResult(True, "alice/tool-ng", ["no license"], "remove")
        new, _, rot = plan_rot([entry()], {"alice/tool": licence}, NOW + timedelta(days=1), state)
        self.assertEqual(new[0]["repo"], "alice/tool-ng")
        self.assertEqual(rot, {"alice/tool-ng": state["alice/tool"]})

    def test_rename_collision_removes_newer(self):
        older = entry("bob/kept", submitted_at="2026-01-01T00:00:00Z", issue=1)
        newer = entry("alice/tool", submitted_at="2026-03-01T00:00:00Z", issue=2)
        new, actions, _ = plan_rot([older, newer], {"alice/tool": ok("bob/kept"), "bob/kept": ok("bob/kept")}, NOW, {})
        self.assertEqual(new, [older])
        self.assertEqual(kinds(actions), [("drop", "alice/tool")])

        # Older one renamed onto the newer one's name: the older is kept and takes the name.
        older = entry("alice/tool", submitted_at="2026-01-01T00:00:00Z", issue=1)
        newer = entry("bob/kept", submitted_at="2026-03-01T00:00:00Z", issue=2)
        new, actions, _ = plan_rot([older, newer], {"alice/tool": ok("bob/kept")}, NOW, {})
        self.assertEqual([e["issue"] for e in new], [1])
        self.assertEqual(new[0]["repo"], "bob/kept")
        self.assertEqual(sorted(kinds(actions)), [("drop", "bob/kept"), ("rename", "bob/kept")])

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
        f"PUT {API}/contents/renames.json": None,
        f"GET {API}/git/ref/heads/stats": {"ref": "refs/heads/stats"},
    }
    r.update(extra or {})
    return r


def store(files=None, extra=None, conflicts=()):
    """Stats routes backed by a dict, so a PUT is seen by the next GET.

    conflicts maps a path to how many PUTs answer 409 first; each 409 also lets
    "another writer" add {"other/writer": 1} to the file.
    """
    state = dict(files or {})
    left = dict(conflicts)

    def get(path):
        def answer(_):
            if state.get(path) is None:
                return None
            return {"content": base64.b64encode(json.dumps(state[path]).encode()).decode(), "sha": f"sha-{path}"}
        return answer

    def put(path):
        def answer(body):
            if left.get(path):
                left[path] -= 1
                state[path] = {**(state.get(path) or {}), "other/writer": 1}
                raise GitHubError(409, "sha mismatch")
            state[path] = json.loads(base64.b64decode(body["content"]))
        return answer

    r = {f"GET {API}/git/ref/heads/stats": {"ref": "refs/heads/stats"}}
    for path in ("rot.json", "discussions.json", "flags.json", "likes.json", "meta.json", "renames.json"):
        r[f"GET {API}/contents/{path}"] = get(path)
        r[f"PUT {API}/contents/{path}"] = put(path)
    r.update(extra or {})
    return r, state


def threads(titles):
    """GraphQL routes over a {number: title} dict that UpdateEntryDiscussion edits."""
    def retitle(v):
        titles[int(v["discussionId"][1:])] = v["title"]
        return {"updateDiscussion": {"discussion": {"number": int(v["discussionId"][1:])}}}

    return {
        "GRAPHQL EntryDiscussionId": lambda v: {"repository": {"discussion": {"id": f"D{v['number']}"}}},
        "GRAPHQL CommentOnEntry": {"addDiscussionComment": {"comment": {"id": "c"}}},
        "GRAPHQL UpdateEntryDiscussion": retitle,
    }


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

        def check(full, gh, now, mode, **kw):
            if full == "bob/other":
                raise RateLimited(0)
            return warn(full)

        g = FakeGitHub(routes())
        with self.assertRaises(RateLimited):
            self.run_prepare(g, check)
        self.assertEqual(Path(self.path).read_text(), before)
        self.assertEqual(writes(g), [])

    def test_undecided_repo_is_skipped_not_failed(self):
        def check(full, gh, now, mode, **kw):
            if full == "alice/tool":
                raise Undecided(0, "no pushed_at")
            return warn(full)

        plan = self.run_prepare(FakeGitHub(routes()), check)
        self.assertEqual(kinds([Action(**a) for a in plan["actions"]]), [("warn", "bob/other")])
        self.assertEqual({e["repo"]: e["status"] for e in load(self.path)}, {"alice/tool": "live", "bob/other": "warned"})

    def test_skips_hidden_and_superseded(self):
        save([entry(status="hidden"), entry("bob/other", status="superseded", issue=2)], self.path)
        seen = []
        self.run_prepare(FakeGitHub(routes()), lambda full, *a, **kw: seen.append(full) or ok(full))
        self.assertEqual(seen, [])

    def test_unchanged_entries_no_pr(self):
        before = Path(self.path).read_text()
        g = FakeGitHub(routes())
        plan = self.run_prepare(g, lambda full, *a, **kw: ok(full))
        self.assertFalse(plan["changed"])
        self.assertEqual(Path(self.path).read_text(), before)
        self.assertEqual(writes(g), [])

        # A first sighting changes only rot.json, which is written straight away.
        g = FakeGitHub(routes())
        plan = self.run_prepare(g, lambda full, *a, **kw: gone(full) if full == "alice/tool" else ok(full))
        self.assertFalse(plan["changed"])
        self.assertEqual(Path(self.path).read_text(), before)
        self.assertEqual(list(written(g, "rot.json")[0]), ["alice/tool"])
        self.assertEqual([c[1] for c in writes(g)], [f"{API}/contents/rot.json"])

    def test_first_sighting_comment_posts_after_the_stats_write(self):
        extra = {
            "GRAPHQL EntryDiscussionId": {"repository": {"discussion": {"id": "D5"}}},
            "GRAPHQL CommentOnEntry": {"addDiscussionComment": {"comment": {"id": "c"}}},
        }
        r, state = store({"discussions.json": {"alice/tool": 5}}, extra)
        g = FakeGitHub(r)
        self.run_prepare(g, lambda full, *a, **kw: gone(full) if full == "alice/tool" else ok(full))
        self.assertTrue(state["rot.json"]["alice/tool"]["notified"])
        self.assertEqual([c[:2] for c in writes(g)],
                         [("PUT", f"{API}/contents/rot.json"), ("GRAPHQL", "CommentOnEntry"),
                          ("PUT", f"{API}/contents/rot.json")])
        self.assertIn("no longer exists", writes(g)[1][2]["body"])

        def refuse(_):
            raise GitHubError(409, "sha mismatch")

        g = FakeGitHub(routes(discussions={"alice/tool": 5}, extra={**extra, f"PUT {API}/contents/rot.json": refuse}))
        with self.assertRaises(GitHubError):
            self.run_prepare(g, lambda full, *a, **kw: gone(full) if full == "alice/tool" else ok(full))
        self.assertEqual([c for c in writes(g) if c[0] == "GRAPHQL"], [])

    def test_notice_that_fails_to_post_stays_unnotified(self):
        def boom(_):
            raise GitHubError(502, "bad gateway")

        r, state = store({"discussions.json": {"alice/tool": 5}},
                         {**threads({}), "GRAPHQL CommentOnEntry": boom})
        with self.assertRaises(SystemExit):
            self.run_prepare(FakeGitHub(r), lambda full, *a, **kw: gone(full) if full == "alice/tool" else ok(full))
        self.assertNotIn("notified", state["rot.json"]["alice/tool"])

    def test_notice_without_a_thread_counts_as_notified(self):
        # Otherwise an entry still waiting for its Discussion could never be removed.
        r, state = store({"discussions.json": {}}, threads({}))
        self.run_prepare(FakeGitHub(r), lambda full, *a, **kw: gone(full) if full == "alice/tool" else ok(full))
        self.assertTrue(state["rot.json"]["alice/tool"]["notified"])

    def test_github_error_aborts_with_no_writes(self):
        before = Path(self.path).read_text()

        def check(full, gh, now, mode, **kw):
            if full == "bob/other":
                raise GitHubError(502, "bad gateway")
            return gone(full)

        g = FakeGitHub(routes())
        with self.assertRaises(GitHubError):
            self.run_prepare(g, check)
        self.assertEqual(Path(self.path).read_text(), before)
        self.assertEqual(writes(g), [])

    def test_changed_entries_defer_every_write(self):
        g = FakeGitHub(routes())
        plan = self.run_prepare(g, lambda full, *a, **kw: gone(full) if full == "alice/tool" else warn(full))
        self.assertTrue(plan["changed"])
        self.assertEqual(writes(g), [])
        self.assertEqual(list(plan["rot_state"]), ["alice/tool"])

    def test_invalid_new_entries_abort(self):
        g = FakeGitHub(routes())
        before = Path(self.path).read_text()
        with self.assertRaises(SystemExit):
            self.run_prepare(g, lambda full, *a, **kw: ok("not a valid/name!" if full == "alice/tool" else full))
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

    def test_cc_licence_kept_only_in_learning_resources(self):
        readme = base64.b64encode(("A shelf of guides and handbooks for people. " * 10).encode()).decode()

        def g():
            return FakeGitHub(routes(extra={
                "GET /repos/alice/tool": {"full_name": "alice/tool", "pushed_at": iso(NOW - timedelta(days=3)),
                                          "license": {"spdx_id": "CC0-1.0"}, "archived": False, "description": "x"},
                "GET /repos/alice/tool/readme": {"content": readme, "encoding": "base64"},
            }))

        save([{**entry(), "category": "learning-resources"}], self.path)
        plan = self.run_prepare(g(), check_repo)
        self.assertEqual((plan["actions"], plan["rot_state"]), ([], {}))
        save([entry()], self.path)
        plan = self.run_prepare(g(), check_repo)
        self.assertEqual(kinds([Action(**a) for a in plan["actions"]]), [("notice", "alice/tool")])

    def test_single_repo_dispatch_validates_input(self):
        entries = [entry(), entry("bob/other", issue=2)]
        self.assertEqual(select(entries, None), entries)
        self.assertEqual(select(entries, "Bob/Other"), [entries[1]])
        self.assertEqual(select(entries, "nobody/here"), [])
        for bad in ("bob", "bob/other/x", "-x/$(id)", "a/b c", "a" * 40 + "/b", "bob/other\n", ""):
            with self.assertRaises(SystemExit, msg=bad):
                select(entries, bad)

        seen = []
        self.run_prepare(FakeGitHub(routes()), lambda full, *a, **kw: seen.append(full) or ok(full), repo="bob/other")
        self.assertEqual(seen, ["bob/other"])


class BodyTest(unittest.TestCase):
    def test_pr_body_has_no_free_text(self):
        actions = [
            Action("warn", "alice/tool", "SECRET message @someone", "alice/tool"),
            Action("rename", "alice/tool-ng", "", "alice/tool"),
            Action("remove", "bob/gone", "SECRET", "bob/gone"),
            Action("notice", "carol/soon", "SECRET", "carol/soon"),
        ]
        body = pr_body(actions)
        self.assertNotIn("SECRET", body)
        self.assertNotIn("@", body)
        for repo in ("alice/tool", "alice/tool-ng", "bob/gone", "carol/soon"):
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

    def test_rename_moves_likes_and_meta_and_records_the_rename(self):
        likes = {"likes": 9, "delta_7d": 1, "rank": 1, "rank_delta_7d": 0, "flagged": False}
        stale = {"to": "x/y", "at": iso(NOW - timedelta(days=8))}
        recent = {"to": "c/d", "at": iso(NOW - timedelta(days=6))}
        r, state = store({"discussions.json": {"alice/tool": 5}, "likes.json": {"alice/tool": likes},
                          "meta.json": {"alice/tool": {"stars": 3}},
                          "renames.json": {"old/gone": stale, "a/b": recent}}, threads({5: "alice/tool"}))
        finish(FakeGitHub(r), self.plan([Action("rename", "alice/tool-ng", "", "alice/tool")]), now=NOW)
        # The deploy after this run reads likes.json and meta.json under the new name.
        self.assertEqual(state["likes.json"], {"alice/tool-ng": likes})
        self.assertEqual(state["meta.json"], {"alice/tool-ng": {"stars": 3}})
        # sync_likes reads old snapshots through this; past the 7-day baseline a record is dead weight.
        self.assertEqual(state["renames.json"], {"a/b": recent, "alice/tool": {"to": "alice/tool-ng", "at": iso(NOW)}})

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


    def test_notice_in_finish_marks_the_record_notified(self):
        rec = {"first_seen": iso(NOW), "reason": "x"}
        r, state = store({"rot.json": {}, "discussions.json": {"alice/tool": 5}}, threads({}))
        finish(FakeGitHub(r), self.plan([Action("notice", "alice/tool", "Heads up", "alice/tool"),
                                         Action("warn", "bob/other", "Hi", "bob/other")],
                                        {"alice/tool": rec}))
        self.assertEqual(state["rot.json"], {"alice/tool": {**rec, "notified": True}})

    def test_stats_write_retries_once_after_409(self):
        r, state = store({"discussions.json": {"alice/tool": 5}, "flags.json": {"alice/tool": {"burst": 1}}},
                         threads({5: "alice/tool"}), conflicts={"flags.json": 1})
        finish(FakeGitHub(r), self.plan([Action("rename", "alice/tool-ng", "", "alice/tool")]))
        # The other writer's change survives, and the move is re-applied on top of it.
        self.assertEqual(state["flags.json"], {"alice/tool-ng": {"burst": 1}, "other/writer": 1})
        self.assertEqual(state["discussions.json"], {"alice/tool-ng": 5})

    def test_comments_still_post_when_a_stats_write_fails(self):
        titles = {5: "alice/tool"}
        r, state = store({"discussions.json": {"alice/tool": 5, "bob/other": 6}, "flags.json": {"alice/tool": {}}},
                         threads(titles), conflicts={"flags.json": 2})
        g = FakeGitHub(r)
        with self.assertRaises(SystemExit):
            finish(g, self.plan([Action("rename", "alice/tool-ng", "", "alice/tool"),
                                 Action("warn", "bob/other", "Heads up", "bob/other")]))
        self.assertEqual(self.gql(g, "CommentOnEntry"), [{"discussionId": "D6", "body": "Heads up"}])
        self.assertEqual(titles[5], "alice/tool-ng")

    def test_failed_rot_write_never_starts_the_clock_on_an_old_record(self):
        # An unnotified sighting from a week ago is replanned as a fresh one; that write fails.
        old_rec = {"first_seen": iso(NOW - timedelta(days=7)), "reason": "x"}
        _, actions, rot = plan_rot([entry()], {"alice/tool": gone()}, NOW, {"alice/tool": old_rec})
        self.assertEqual(kinds(actions), [("notice", "alice/tool")])
        r, state = store({"rot.json": {"alice/tool": old_rec}, "discussions.json": {"alice/tool": 5}},
                         threads({5: "alice/tool"}), conflicts={"rot.json": 2})
        g = FakeGitHub(r)
        with self.assertRaises(SystemExit):
            finish(g, {"changed": True, "actions": [a.__dict__ for a in actions], "rot_state": rot})
        self.assertEqual(len(self.gql(g, "CommentOnEntry")), 1)
        self.assertNotIn("notified", state["rot.json"]["alice/tool"])

        new, actions, _ = plan_rot([entry()], {"alice/tool": gone()}, NOW + timedelta(days=1), state["rot.json"])
        self.assertEqual((new, kinds(actions)), ([entry()], [("notice", "alice/tool")]))

    def test_no_retitle_when_the_key_move_fails(self):
        titles = {5: "alice/tool", 6: "bob/kept"}
        r, state = store({"discussions.json": {"alice/tool": 5, "bob/kept": 6}}, threads(titles),
                         conflicts={"discussions.json": 2})
        g = FakeGitHub(r)
        with self.assertRaises(SystemExit):
            finish(g, self.plan([Action("rename", "alice/tool-ng", "", "alice/tool"),
                                 Action("drop", "bob/kept", "Merged", "bob/kept"),
                                 Action("warn", "alice/tool-ng", "Heads up", "alice/tool")]))
        self.assertEqual(self.gql(g, "UpdateEntryDiscussion"), [])
        self.assertEqual(titles, {5: "alice/tool", 6: "bob/kept"})
        self.assertEqual(sorted(c["body"] for c in self.gql(g, "CommentOnEntry")), ["Heads up", "Merged"])

    def test_collision_keeps_the_older_entrys_thread_through_on_merge(self):
        from scripts.devstream.entries import normalize_repo
        from scripts.on_merge import run as on_merge
        from scripts.validate_data import REPO_RE

        older = entry("alice/tool", submitted_at="2026-01-01T00:00:00Z", issue=1)
        newer = entry("bob/kept", submitted_at="2026-03-01T00:00:00Z", issue=2)
        new, actions, rot = plan_rot([older, newer], {"alice/tool": ok("bob/kept")}, NOW, {})
        titles = {5: "alice/tool", 6: "bob/kept"}
        listing = {"repository": {"discussions": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}

        def list_threads(_):
            nodes = [{"number": n, "title": t, "author": {"login": "devstreamhq-bot[bot]"}}
                     for n, t in sorted(titles.items())]
            return {"repository": {"discussions": {**listing["repository"]["discussions"], "nodes": nodes}}}

        r, state = store({"discussions.json": {"alice/tool": 5, "bob/kept": 6}}, {
            **threads(titles),
            "GRAPHQL LikesCategory": {"repository": {"id": "R", "discussionCategories": {
                "nodes": [{"id": "C", "slug": "likes"}]}}},
            "GRAPHQL LikesDiscussions": list_threads,
        })
        g = FakeGitHub(r)
        finish(g, {"changed": True, "actions": [a.__dict__ for a in actions], "rot_state": rot})
        self.assertEqual(titles, {5: "bob/kept", 6: "Removed: bob/kept"})
        self.assertEqual(state["discussions.json"], {"bob/kept": 5})
        dropped = normalize_repo(titles[6])
        self.assertNotIn(dropped, {normalize_repo(e["repo"]) for e in new + [older, newer]})
        self.assertIsNone(REPO_RE.fullmatch(dropped))

        on_merge(g, new, sleep=lambda s: None)
        self.assertEqual(state["discussions.json"]["bob/kept"], 5)
        self.assertEqual(self.gql(g, "CreateEntryDiscussion"), [])


if __name__ == "__main__":
    unittest.main()
