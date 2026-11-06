import base64
import json
import unittest
from datetime import datetime, timedelta, timezone

from scripts.devstream.gh import GitHubError
from scripts.devstream.likes import count_likes, detect_burst, rank_entries
from scripts.sync_likes import run
from scripts.tests.fake_gh import FakeGitHub, GraphQLErrors, Status

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
API = "/repos/devstreamhq/devstream"
OLD = "2020-01-01T00:00:00Z"  # an established account
NEW = "2026-10-30T00:00:00Z"  # 6 days old: untrusted and under 90 days
MID = "2026-09-06T00:00:00Z"  # 60 days old: trusted but under 90 days
TODAY = "2026-11-05"
WEEK_AGO = "2026-10-29"


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


RECENT = iso(NOW - timedelta(hours=2))
LONG_AGO = iso(NOW - timedelta(days=3))


def node(login, created=OLD, reacted=LONG_AGO):
    return {"createdAt": reacted, "user": None if login is None else {"login": login, "createdAt": created}}


def entry(repo, status="live", repo_id=None):
    e = {"repo": repo, "status": status, "description": "d"}
    return e if repo_id is None else {**e, "repo_id": repo_id}


class World:
    """A FakeGitHub with discussions, users, repo meta and stats files wired up."""

    def __init__(self, entries, discussions, users=None, stats=None, stars=None, issues=(), page=100, ids=None):
        self.entries = entries
        self.ids = ids or {}  # repo -> databaseId GitHub reports for that name
        self.discussions = discussions  # {repo: [node, ...]}
        numbers = {normalize(r): i + 1 for i, r in enumerate(discussions)}
        self.by_number = {numbers[normalize(r)]: n for r, n in discussions.items()}
        self.stats = {"discussions.json": numbers, **(stats or {})}
        self.stars = stars or {}
        self.page = page
        self.cursors = None  # set to a constant to simulate a stuck cursor
        users = dict(users or {})
        for nodes in discussions.values():
            for n in nodes or []:
                if n["user"]:
                    users.setdefault(n["user"]["login"], n["user"]["createdAt"])
        routes = {
            f"GET {API}/git/ref/heads/stats": {"ref": "refs/heads/stats"},
            "GET /orgs/devstreamhq/blocks": [],
            "GRAPHQL DiscussionReactors": self._reactors,
            "GRAPHQL RepoMeta": self._meta,
            f"POST {API}/issues": self._open_issue,
            f"GET {API}/issues": self._list_issues,
        }
        for login, created in users.items():
            routes[f"GET /users/{login}"] = {
                "login": login, "created_at": created, "public_repos": 1, "type": "User"}
        for path, data in self.stats.items():
            routes[f"GET {API}/contents/{path}"] = {
                "content": base64.b64encode(json.dumps(data).encode()).decode(), "sha": f"sha-{path}"}
        snaps = sorted(p for p in self.stats if p.startswith("snapshots/"))
        if snaps:
            routes[f"GET {API}/contents/snapshots"] = [
                {"name": p.split("/")[1], "type": "file", "sha": f"sha-{p}"} for p in snaps]
        days = [f"snapshots/{NOW + timedelta(days=d):%Y-%m-%d}.json" for d in range(12)]
        for path in ("meta.json", "likes.json", "flags.json", "trust-cache.json", *days):
            routes[f"PUT {API}/contents/{path}"] = {}
        self.next_issue = 100
        for i in issues:  # issues that exist but the bot never opened
            routes[f"GET {API}/issues/{i['number']}"] = {"state": "open", **i}
        self.routes = routes
        self.gh = FakeGitHub(routes)

    def _reactors(self, v):
        repo, errors = {}, []
        i = 0
        while f"n{i}" in v:
            nodes = self.by_number[v[f"n{i}"]]
            if nodes is None:  # a deleted Discussion
                repo[f"d{i}"] = None
                errors.append({"type": "NOT_FOUND", "path": ["repository", f"d{i}"]})
                i += 1
                continue
            start = 0 if self.cursors or i else int(v.get("cursor") or 0)
            chunk = nodes[start:start + self.page]
            more = start + self.page < len(nodes)
            cursor = self.cursors or str(start + self.page)
            repo[f"d{i}"] = {"reactions": {
                "nodes": chunk, "pageInfo": {"hasNextPage": more, "endCursor": cursor if more else None}}}
            i += 1
        return GraphQLErrors({"repository": repo}, errors) if errors else {"repository": repo}

    def _meta(self, v):
        out, errors = {}, []
        i = 0
        while f"o{i}" in v:
            key = f"{v[f'o{i}']}/{v[f'n{i}']}"
            stars = self.stars.get(key, 0)
            if stars is None or stars == "forbidden":
                errors.append({"type": "NOT_FOUND" if stars is None else "FORBIDDEN", "path": [f"r{i}"]})
            out[f"r{i}"] = None if stars is None or stars == "forbidden" else {
                "nameWithOwner": key, "databaseId": self.ids.get(key, 1), "stargazerCount": stars,
                "primaryLanguage": {"name": "Go", "color": "#00ADD8"},
                "pushedAt": "2026-11-01T00:00:00Z", "owner": {"avatarUrl": "https://a/x.png"}}
            i += 1
        return GraphQLErrors(out, errors) if errors else out

    def _open_issue(self, body):
        n = self.next_issue
        self.next_issue += 1
        self.routes[f"GET {API}/issues/{n}"] = {"number": n, "state": "open", "title": body["title"],
                                                "user": {"login": "devstreamhq-bot[bot]"}}
        self.routes[f"PATCH {API}/issues/{n}"] = {}
        return {"number": n}

    def _list_issues(self, _):
        return [{"user": {"login": "mallory"}, **v} for k, v in self.routes.items()
                if k.startswith(f"GET {API}/issues/") and isinstance(v, dict) and v.get("state") == "open"]

    def close(self, n):
        self.routes[f"GET {API}/issues/{n}"]["state"] = "closed"

    def set_nodes(self, repo, nodes):
        self.by_number[self.stats["discussions.json"][normalize(repo)]] = nodes
        for n in nodes:
            self.routes.setdefault(f"GET /users/{n['user']['login']}", {
                "login": n["user"]["login"], "created_at": n["user"]["createdAt"],
                "public_repos": 1, "type": "User"})

    def persist(self):
        """Make the files this run wrote readable by the next run, as on the stats branch."""
        for _, p, body in self.puts():
            self.routes[f"GET {p}"] = {"content": body["content"], "sha": f"sha-{len(self.gh.calls)}"}
        self.gh.calls.clear()

    def run(self, excluded=frozenset(), now=NOW):
        run(self.gh, self.entries, now, excluded)

    def puts(self):
        return [c for c in self.gh.calls if c[0] == "PUT"]

    def written(self, path):
        for m, p, body in reversed(self.puts()):
            if p == f"{API}/contents/{path}":
                return json.loads(base64.b64decode(body["content"]))
        return None

    def current(self, path):
        """The file as the stats branch now holds it, written this run or earlier."""
        got = self.written(path)
        if got is None and f"GET {API}/contents/{path}" in self.routes:
            got = json.loads(base64.b64decode(self.routes[f"GET {API}/contents/{path}"]["content"]))
        return got

    def patches(self):
        return [c for c in self.gh.calls if c[0] == "PATCH"]

    def issues_opened(self):
        return [c[2] for c in self.gh.calls if c[0] == "POST" and c[1] == f"{API}/issues"]


def normalize(r):
    return r.lower()


def burst_nodes(n, young, prefix="u", created=MID, reacted=RECENT):
    return [node(f"{prefix}{i}", created if i < young else OLD, reacted) for i in range(n)]


def titles(w):
    return [i["title"] for i in w.issues_opened()]


class Counting(unittest.TestCase):
    def test_untrusted_not_counted(self):
        self.assertEqual(count_likes(["a", "b", "c"], lambda u: u != "b"), 2)
        self.assertEqual(count_likes(["a", "A", "a"], lambda u: True), 1)
        w = World([entry("o/a")], {"o/a": [node("old1"), node("new1", NEW)]})
        w.run()
        self.assertEqual(w.written("likes.json")["o/a"]["likes"], 1)

    def test_owner_counted(self):
        w = World([entry("alice/tool")], {"alice/tool": [node("alice"), node("bob")]})
        w.run()
        self.assertEqual(w.written("likes.json")["alice/tool"]["likes"], 2)

    def test_excluded_user_not_counted(self):
        w = World([entry("o/a")], {"o/a": [node("alice"), node("bob")]})
        w.run(excluded={"bob"})
        self.assertEqual(w.written("likes.json")["o/a"]["likes"], 1)

    def test_deleted_user_skipped(self):
        w = World([entry("o/a")], {"o/a": [node(None), node("alice"), node(None, reacted=RECENT)]})
        w.run()
        self.assertEqual(w.written("likes.json")["o/a"]["likes"], 1)

    def test_paginates_past_100(self):
        nodes = [node(f"u{i}") for i in range(130)]
        w = World([entry("o/a"), entry("o/b")], {"o/a": [node("x")], "o/b": nodes})
        w.run()
        self.assertEqual(w.written("likes.json")["o/b"]["likes"], 130)
        cursors = [c[2].get("cursor") for c in w.gh.calls if c[1] == "DiscussionReactors"]
        self.assertEqual(cursors, [None, "100"])

    def test_batches_20_discussions_per_query(self):
        repos = {f"o/r{i:02}": [node("alice")] for i in range(25)}
        w = World([entry(r) for r in repos], repos)
        w.run()
        calls = [c for c in w.gh.calls if c[1] == "DiscussionReactors"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(w.written("likes.json")), 25)

    def test_endcursor_repeat_raises(self):
        w = World([entry("o/a")], {"o/a": [node(f"u{i}") for i in range(250)]})
        w.cursors = "stuck"
        with self.assertRaises(GitHubError):
            w.run()
        self.assertEqual(w.puts(), [])

    def test_api_error_writes_nothing(self):
        w = World([entry("o/a"), entry("o/b")], {"o/a": [node("alice")], "o/b": [node("bob")]})

        def boom(_):
            raise GitHubError(502, "bad gateway")
        w.gh.routes["GET /users/bob"] = boom
        with self.assertRaises(GitHubError):
            w.run()
        self.assertEqual(w.puts(), [])
        self.assertEqual(w.issues_opened(), [])

    def test_deleted_discussion_carries_previous_count(self):
        prev = {"o/b": {"likes": 7, "delta_7d": 0, "rank": 1, "rank_delta_7d": 0, "flagged": False}}
        w = World([entry("o/a"), entry("o/b")], {"o/a": [node("x")], "o/b": None},
                  stats={"likes.json": prev})
        with self.assertLogs(level="WARNING") as logs:
            w.run()
        self.assertEqual(w.written("likes.json")["o/b"]["likes"], 7)
        self.assertIn("Discussion 2", " ".join(logs.output))

    def test_all_discussions_deleted_aborts(self):
        w = World([entry("o/a"), entry("o/b")], {"o/a": None, "o/b": None})
        with self.assertRaises(GitHubError):
            w.run()
        self.assertEqual(w.puts(), [])

    def test_entry_without_discussion_has_zero(self):
        w = World([entry("o/a"), entry("o/new")], {"o/a": [node("alice")]})
        w.run()
        self.assertEqual(w.written("likes.json")["o/new"]["likes"], 0)


class Burst(unittest.TestCase):
    def today(self, n, young):
        return [(f"u{i}", NOW) for i in range(n)], {f"u{i}": 10 if i < young else 400 for i in range(n)}

    def test_burst_10_mostly_new_flags(self):
        self.assertTrue(detect_burst(*self.today(10, 6)))

    def test_burst_9_does_not_flag(self):
        self.assertFalse(detect_burst(*self.today(9, 9)))

    def test_burst_exactly_half_does_not_flag(self):
        self.assertFalse(detect_burst(*self.today(10, 5)))

    def test_missing_age_counts_as_young(self):
        today, ages = self.today(10, 0)
        for i in range(6):
            del ages[f"u{i}"]
        self.assertTrue(detect_burst(today, ages))

    def test_old_reactions_ignored(self):
        nodes = burst_nodes(9, 9) + [node("late", NEW, LONG_AGO)]
        w = World([entry("o/a")], {"o/a": nodes})
        w.run()
        self.assertFalse(w.written("likes.json")["o/a"]["flagged"])

    def test_untrusted_young_reactors_do_not_flag(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10, created=NEW)})
        w.run()
        self.assertFalse(w.written("likes.json")["o/a"]["flagged"])
        self.assertEqual(w.issues_opened(), [])

    def test_trusted_young_burst_flags(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)})
        w.run()
        self.assertEqual(w.written("likes.json")["o/a"], {
            "likes": 10, "delta_7d": 0, "rank": 1, "rank_delta_7d": 0, "flagged": True})
        self.assertEqual(titles(w), ["Like burst: o/a"])

    def test_excluded_reactors_do_not_flag(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)})
        w.run(excluded={"u0", "u1"})
        self.assertFalse(w.written("likes.json")["o/a"]["flagged"])

    def test_age_boundary_89_vs_90_days(self):
        d89 = iso(NOW - timedelta(days=89, hours=23))
        d90 = iso(NOW - timedelta(days=90))
        w = World([entry("o/a")], {"o/a": burst_nodes(5, 5) + burst_nodes(5, 5, "v", created=d89)})
        w.run()
        self.assertTrue(w.written("likes.json")["o/a"]["flagged"])
        w = World([entry("o/a")], {"o/a": burst_nodes(5, 5) + burst_nodes(5, 5, "v", created=d90)})
        w.run()
        self.assertFalse(w.written("likes.json")["o/a"]["flagged"])
        self.assertTrue(detect_burst([(f"u{i}", NOW) for i in range(10)], {f"u{i}": 89 for i in range(10)}))
        self.assertFalse(detect_burst([(f"u{i}", NOW) for i in range(10)], {f"u{i}": 90 for i in range(10)}))

    def test_reaction_exactly_24h_old_counts(self):
        edge = iso(NOW - timedelta(hours=24))
        past = iso(NOW - timedelta(hours=24, seconds=1))
        w = World([entry("o/a")], {"o/a": burst_nodes(9, 9) + burst_nodes(1, 1, "v", reacted=edge)})
        w.run()
        self.assertTrue(w.written("likes.json")["o/a"]["flagged"])
        w = World([entry("o/a")], {"o/a": burst_nodes(9, 9) + burst_nodes(1, 1, "v", reacted=past)})
        w.run()
        self.assertFalse(w.written("likes.json")["o/a"]["flagged"])

    def test_six_bursts_give_five_issues_and_one_aggregate(self):
        repos = {f"o/r{j}": burst_nodes(10, 10, prefix="zqx") for j in range(6)}
        w = World([entry(r) for r in repos], repos)
        w.run()
        self.assertEqual(titles(w), [f"Like burst: o/r{j}" for j in range(5)] + ["Like burst: 1 entry"])
        agg = w.issues_opened()[-1]
        self.assertIn("o/r5", agg["body"])
        self.assertNotIn("zqx", agg["body"])
        self.assertNotIn("@", agg["body"])
        self.assertEqual(sorted(agg["labels"]), ["needs-review", "report"])
        self.assertTrue(all(v["flagged"] for v in w.written("likes.json").values()))

    def test_aggregate_reused_and_patched(self):
        first = {f"o/r{j}": burst_nodes(10, 10) for j in range(6)}
        later = {f"o/s{j}": [] for j in range(6)}
        w = World([entry(r) for r in {**first, **later}], {**first, **later})
        w.run()
        self.assertEqual(titles(w)[-1], "Like burst: 1 entry")
        agg = w.next_issue - 1
        w.persist()
        for r in later:
            w.set_nodes(r, burst_nodes(10, 10, prefix="zqx", reacted=iso(NOW + timedelta(minutes=30))))
        w.run(now=NOW + timedelta(hours=1))
        self.assertEqual(titles(w), [f"Like burst: o/s{j}" for j in range(5)])
        [(_, path, body)] = w.patches()
        self.assertEqual(path, f"{API}/issues/{agg}")
        self.assertEqual(body["title"], "Like burst: 2 entries")
        self.assertIn("o/r5", body["body"])
        self.assertIn("o/s5", body["body"])
        self.assertNotIn("zqx", body["body"])
        flags = w.written("flags.json")
        self.assertEqual((flags["o/r5"]["burst"]["issue"], flags["o/s5"]["burst"]["issue"]), (agg, agg))

    def test_overflow_repo_stays_flagged_while_aggregate_open(self):
        repos = {f"o/r{j}": burst_nodes(10, 10) for j in range(6)}
        w = World([entry(r) for r in repos], repos)
        w.run()
        agg = w.next_issue - 1
        w.persist()
        w.run(now=NOW + timedelta(hours=30))
        self.assertTrue(w.current("likes.json")["o/r5"]["flagged"])
        w.persist()
        w.close(agg)
        w.run(now=NOW + timedelta(hours=31))
        self.assertFalse(w.current("likes.json")["o/r5"]["flagged"])
        self.assertTrue(w.current("likes.json")["o/r0"]["flagged"])

    def test_drip_opens_review_issue_without_flag(self):
        days = [iso(NOW - timedelta(days=2 + i % 4)) for i in range(30)]
        nodes = [node(f"u{i}", MID if i < 16 else OLD, days[i]) for i in range(30)]
        w = World([entry("o/a")], {"o/a": nodes})
        w.run()
        self.assertFalse(w.written("likes.json")["o/a"]["flagged"])
        [issue] = w.issues_opened()
        self.assertEqual(issue["title"], "Like drip: o/a")
        self.assertIn("needs-review", issue["labels"])
        self.assertNotIn("u1", issue["body"])

    def test_drip_counts_toward_cap(self):
        days = [iso(NOW - timedelta(days=2 + i % 4)) for i in range(30)]
        repos = {f"o/r{j}": burst_nodes(10, 10) for j in range(5)}
        repos["o/z"] = [node(f"d{i}", MID, days[i]) for i in range(30)]
        w = World([entry(r) for r in repos], repos)
        w.run()
        self.assertEqual(titles(w)[-1], "Like burst: 1 entry")
        self.assertIn("o/z", w.issues_opened()[-1]["body"])
        self.assertNotIn("Like drip: o/z", titles(w))

    def flags(self, issue=7, reviewed=None, kind="burst"):
        rec = {"kind": kind, "issue": issue, "opened_at": iso(NOW - timedelta(days=2)), "reviewed_at": reviewed}
        return {"flags.json": {"o/a": {kind: rec}}}

    def test_spent_flags_are_pruned(self):
        def rec(kind, reviewed):
            return {"kind": kind, "issue": 7, "opened_at": iso(NOW - timedelta(days=30)),
                    "reviewed_at": None if reviewed is None else iso(NOW - reviewed)}
        stats = {"flags.json": {
            "o/a": {"burst": rec("burst", timedelta(days=2)), "drip": rec("drip", timedelta(days=3))},
            "o/b": {"burst": rec("burst", timedelta(hours=2)), "drip": rec("drip", timedelta(days=8))},
            "o/gone": {"burst": rec("burst", None)},
        }}
        w = World([entry("o/a"), entry("o/b", status="hidden")], {"o/a": [node("x")], "o/b": []},
                  stats=stats, issues=[{"number": 7}])
        w.run()
        # Past its cooldown, off the list, or both kinds gone: dropped. Inside its cooldown: kept.
        self.assertEqual(w.written("flags.json"), {"o/a": {"drip": rec("drip", timedelta(days=3))},
                                                   "o/b": {"burst": rec("burst", timedelta(hours=2))}})
        # Nothing else is spent, so the next run writes nothing.
        w.persist()
        w.run()
        self.assertIsNone(w.written("flags.json"))

    def test_flag_persists_while_issue_open(self):
        w = World([entry("o/a")], {"o/a": [node("x")]}, stats=self.flags(), issues=[{"number": 7}])
        w.run()
        self.assertTrue(w.written("likes.json")["o/a"]["flagged"])
        self.assertIsNone(w.written("flags.json"))
        self.assertEqual(w.issues_opened(), [])

    def test_flag_clears_when_issue_closed(self):
        w = World([entry("o/a")], {"o/a": [node("x")]}, stats=self.flags(),
                  issues=[{"number": 7, "state": "closed"}])
        w.run()
        self.assertFalse(w.written("likes.json")["o/a"]["flagged"])
        self.assertEqual(w.written("flags.json")["o/a"]["burst"]["reviewed_at"], iso(NOW))

    def test_deleted_issue_counts_as_reviewed(self):
        for gone in (None, Status(410)):
            w = World([entry("o/a")], {"o/a": [node("x")]}, stats=self.flags())
            if gone is not None:
                w.routes[f"GET {API}/issues/7"] = gone
            w.run()
            self.assertFalse(w.written("likes.json")["o/a"]["flagged"])
            self.assertEqual(w.written("flags.json")["o/a"]["burst"]["reviewed_at"], iso(NOW))

    def test_issue_checked_once_per_number(self):
        stats = {"flags.json": {r: {"burst": {"kind": "burst", "issue": 7, "opened_at": iso(NOW),
                                              "reviewed_at": None}} for r in ("o/a", "o/b")}}
        w = World([entry("o/a"), entry("o/b")], {"o/a": [], "o/b": []}, stats=stats, issues=[{"number": 7}])
        w.run()
        self.assertEqual(len([c for c in w.gh.calls if c[1] == f"{API}/issues/7"]), 1)

    def test_drip_record_never_flags(self):
        w = World([entry("o/a")], {"o/a": [node("x")]}, stats=self.flags(kind="drip"), issues=[{"number": 7}])
        w.run()
        self.assertFalse(w.written("likes.json")["o/a"]["flagged"])

    def test_forged_issue_has_no_effect(self):
        forged = {"number": 5, "title": "Like burst: o/a"}
        w = World([entry("o/a")], {"o/a": [node("x")]}, issues=[forged])
        w.run()
        self.assertFalse(w.written("likes.json")["o/a"]["flagged"])
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)}, issues=[forged])
        w.run()
        self.assertEqual(titles(w), ["Like burst: o/a"])
        self.assertEqual(w.written("flags.json")["o/a"]["burst"]["issue"], 100)

    def test_no_new_burst_issue_for_24h_after_close(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)})
        w.run()
        w.persist()
        w.close(100)
        w.run(now=NOW + timedelta(hours=1))  # sees the close
        self.assertEqual(w.issues_opened(), [])
        reviewed = NOW + timedelta(hours=1)
        w.persist()
        w.set_nodes("o/a", burst_nodes(10, 10, reacted=iso(reviewed + timedelta(hours=20))))
        w.run(now=reviewed + timedelta(hours=23))
        self.assertEqual(w.issues_opened(), [])
        w.persist()
        w.run(now=reviewed + timedelta(hours=24))
        self.assertEqual(titles(w), ["Like burst: o/a"])
        self.assertEqual(w.written("flags.json")["o/a"]["burst"]["reviewed_at"], None)

    def test_no_new_drip_issue_for_7_days_after_close(self):
        def drip(at):
            return [node(f"u{i}", MID, iso(at - timedelta(days=2 + i % 4))) for i in range(30)]
        w = World([entry("o/a")], {"o/a": drip(NOW)})
        w.run()
        self.assertEqual(titles(w), ["Like drip: o/a"])
        w.persist()
        w.close(100)
        w.run(now=NOW + timedelta(hours=1))
        reviewed = NOW + timedelta(hours=1)
        w.persist()
        w.set_nodes("o/a", drip(reviewed + timedelta(days=6)))
        w.run(now=reviewed + timedelta(days=6, hours=23))
        self.assertEqual(w.issues_opened(), [])
        w.persist()
        w.set_nodes("o/a", drip(reviewed + timedelta(days=7)))
        w.run(now=reviewed + timedelta(days=7))
        self.assertEqual(titles(w), ["Like drip: o/a"])

    def test_flags_round_trip(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)})
        w.run()
        self.assertEqual(w.written("flags.json"), {"o/a": {"burst": {
            "kind": "burst", "issue": 100, "opened_at": iso(NOW), "reviewed_at": None}}})
        w.persist()
        w.run(now=NOW + timedelta(minutes=5))
        self.assertIsNone(w.written("flags.json"))
        self.assertEqual(w.issues_opened(), [])
        self.assertTrue(w.current("likes.json")["o/a"]["flagged"])

    def test_failed_likes_write_after_issues_opens_no_duplicates(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)})

        def boom(_):
            raise GitHubError(409, "conflict")
        w.routes[f"PUT {API}/contents/likes.json"] = boom
        with self.assertRaises(GitHubError):
            w.run()
        self.assertEqual(titles(w), ["Like burst: o/a"])
        w.persist()
        w.routes.pop(f"GET {API}/contents/likes.json")  # that PUT never landed
        w.routes[f"PUT {API}/contents/likes.json"] = {}
        w.run(now=NOW + timedelta(minutes=5))
        self.assertEqual(w.issues_opened(), [])
        self.assertTrue(w.written("likes.json")["o/a"]["flagged"])

    def test_failed_flags_write_after_issues_opens_no_duplicates(self):
        repos = {f"o/r{j}": burst_nodes(10, 10) for j in range(6)}
        w = World([entry(r) for r in repos], repos)

        def boom(_):
            raise GitHubError(502, "bad gateway")
        w.routes[f"PUT {API}/contents/flags.json"] = boom
        with self.assertRaises(GitHubError):
            w.run()
        self.assertEqual(len(w.issues_opened()), 6)
        w.persist()
        w.routes.pop(f"GET {API}/contents/flags.json")  # that PUT never landed
        w.routes[f"PUT {API}/contents/flags.json"] = {}
        w.run(now=NOW + timedelta(minutes=5))
        # The lost records are rebuilt from the bot's open issues, not opened again.
        self.assertEqual(w.issues_opened(), [])
        flags = w.written("flags.json")
        self.assertEqual(sorted(r["burst"]["issue"] for r in flags.values()), [100, 101, 102, 103, 104, 105])

    def test_aggregate_patch_failure_keeps_the_run(self):
        first = {f"o/r{j}": burst_nodes(10, 10) for j in range(6)}
        later = {f"o/s{j}": [] for j in range(6)}
        w = World([entry(r) for r in {**first, **later}], {**first, **later})
        w.run()
        agg = w.next_issue - 1
        w.persist()

        def boom(_):
            raise GitHubError(502, "bad gateway")
        w.routes[f"PATCH {API}/issues/{agg}"] = boom
        for r in later:
            w.set_nodes(r, burst_nodes(10, 10, reacted=iso(NOW + timedelta(minutes=30))))
        with self.assertLogs(level="WARNING"):
            w.run(now=NOW + timedelta(hours=1))
        self.assertEqual(w.written("flags.json")["o/s5"]["burst"]["issue"], agg)
        self.assertIn("o/s5", w.written("likes.json"))

    def test_aggregate_closed_mid_edit_gets_a_new_issue(self):
        first = {f"o/r{j}": burst_nodes(10, 10) for j in range(6)}
        later = {f"o/s{j}": [] for j in range(6)}
        for gone in ({"state": "closed"}, None):
            w = World([entry(r) for r in {**first, **later}], {**first, **later})
            w.run()
            agg = w.next_issue - 1
            w.persist()
            if gone is None:
                del w.routes[f"PATCH {API}/issues/{agg}"]  # deleted: the fake answers 404
            else:
                w.routes[f"PATCH {API}/issues/{agg}"] = gone
            for r in later:
                w.set_nodes(r, burst_nodes(10, 10, reacted=iso(NOW + timedelta(minutes=30))))
            w.run(now=NOW + timedelta(hours=1))
            self.assertEqual(titles(w)[-1], "Like burst: 1 entry")
            self.assertEqual(w.written("flags.json")["o/s5"]["burst"]["issue"], w.next_issue - 1)
            self.assertNotEqual(w.next_issue - 1, agg)

    def test_review_lookup_404_counts_as_reviewed(self):
        w = World([entry("o/a")], {"o/a": [node("x")]}, stats=self.flags())

        def gone(_):
            raise GitHubError(404, "Not Found")
        w.routes[f"GET {API}/issues/7"] = gone
        w.run()
        self.assertEqual(w.written("flags.json")["o/a"]["burst"]["reviewed_at"], iso(NOW))

    def test_adopted_aggregate_keeps_the_entries_it_listed(self):
        first = {f"o/r{j}": burst_nodes(10, 10) for j in range(6)}
        later = {f"o/s{j}": [] for j in range(6)}
        w = World([entry(r) for r in {**first, **later}], {**first, **later})

        def boom(_):
            raise GitHubError(502, "bad gateway")
        w.routes[f"PUT {API}/contents/flags.json"] = boom
        with self.assertRaises(GitHubError):
            w.run()
        agg = w.next_issue - 1
        w.routes[f"GET {API}/issues/{agg}"]["body"] = w.issues_opened()[-1]["body"]
        w.persist()
        w.routes.pop(f"GET {API}/contents/flags.json")  # that PUT never landed
        w.routes[f"PUT {API}/contents/flags.json"] = {}
        w.set_nodes("o/r5", [])
        for r in later:
            w.set_nodes(r, burst_nodes(10, 10, reacted=iso(NOW + timedelta(minutes=30))))
        w.run(now=NOW + timedelta(hours=1))
        [(_, path, body)] = w.patches()
        self.assertEqual(path, f"{API}/issues/{agg}")
        self.assertEqual(body["title"], "Like burst: 7 entries")
        self.assertIn("- o/r5", body["body"])
        self.assertIn("- o/s5", body["body"])

    def test_one_failed_review_lookup_skips_only_that_issue(self):
        stats = {"flags.json": {r: {"burst": {"kind": "burst", "issue": n, "opened_at": iso(NOW),
                                              "reviewed_at": None}} for r, n in (("o/a", 7), ("o/b", 8))}}
        w = World([entry("o/a"), entry("o/b")], {"o/a": [], "o/b": []}, stats=stats,
                  issues=[{"number": 8, "state": "closed"}])

        def boom(_):
            raise GitHubError(502, "bad gateway")
        w.routes[f"GET {API}/issues/7"] = boom
        with self.assertLogs(level="WARNING"):
            w.run()
        flags = w.written("flags.json")
        self.assertIsNone(flags["o/a"]["burst"]["reviewed_at"])
        self.assertEqual(flags["o/b"]["burst"]["reviewed_at"], iso(NOW))
        self.assertTrue(w.written("likes.json")["o/a"]["flagged"])

    def test_failed_issue_post_keeps_flags_for_opened_ones(self):
        repos = {f"o/r{j}": burst_nodes(10, 10) for j in range(2)}
        w = World([entry(r) for r in repos], repos)
        opened = w._open_issue

        def second_fails(body):
            if w.next_issue == 101:
                raise GitHubError(502, "bad gateway")
            return opened(body)
        w.routes[f"POST {API}/issues"] = second_fails
        with self.assertRaises(GitHubError):
            w.run()
        self.assertEqual(list(w.written("flags.json")), ["o/r0"])
        self.assertIsNone(w.written("likes.json"))

    def test_burst_report_has_no_logins(self):
        w = World([entry("Owner/Tool")], {"Owner/Tool": burst_nodes(12, 8, prefix="zqx")})
        w.run()
        [issue] = w.issues_opened()
        self.assertEqual(issue["title"], "Like burst: Owner/Tool")
        self.assertEqual(sorted(issue["labels"]), ["needs-review", "report"])
        self.assertNotIn("zqx", issue["body"])
        self.assertNotIn("@", issue["body"])
        for word in ("24", "hour", "7 day", "90", "days old"):
            self.assertNotIn(word, issue["body"])

    def test_issue_bodies_have_no_windows(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)})
        w.run()
        [burst] = w.issues_opened()
        self.assertIn("jumped quickly", burst["body"])
        repos = [f"o/r{i}" for i in range(7)]
        w2 = World([entry(r) for r in repos], {r: burst_nodes(10, 10, prefix=f"p{i}") for i, r in enumerate(repos)})
        w2.run()
        for issue in w2.issues_opened():
            for word in ("24", "hour", "7 day", "90", "days old"):
                self.assertNotIn(word, issue["body"])

    def test_burst_report_deduped(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)}, stats=self.flags(), issues=[{"number": 7}])
        w.run()
        self.assertEqual(w.issues_opened(), [])
        self.assertTrue(w.written("likes.json")["o/a"]["flagged"])
        self.assertFalse([c for c in w.gh.calls if c[1].startswith(f"{API}/issues?")])

    def test_flagged_status_keeps_rank(self):
        w = World([entry("o/a", "flagged"), entry("o/b"), entry("o/h", "hidden")],
                  {"o/a": [node("x"), node("y")], "o/b": [node("x")], "o/h": [node("x")]})
        w.run()
        likes = w.written("likes.json")
        self.assertEqual(likes["o/a"]["rank"], 1)
        self.assertTrue(likes["o/a"]["flagged"])
        self.assertFalse(likes["o/b"]["flagged"])
        self.assertNotIn("o/h", likes)
        self.assertEqual(w.issues_opened(), [])


class Ranking(unittest.TestCase):
    def test_rank_ties_broken_by_stars_then_name(self):
        likes = {"o/c": 5, "o/b": 5, "o/a": 5, "o/top": 9}
        stars = {"o/c": 100, "o/b": 7, "o/a": 7, "o/top": 0}
        self.assertEqual(rank_entries(likes, stars), {"o/top": 1, "o/c": 2, "o/a": 3, "o/b": 4})

    def test_rank_uses_meta_stars(self):
        w = World([entry("o/a"), entry("o/b")], {"o/a": [node("x")], "o/b": [node("y")]},
                  stars={"o/a": 1, "o/b": 50})
        w.run()
        likes = w.written("likes.json")
        self.assertEqual((likes["o/b"]["rank"], likes["o/a"]["rank"]), (1, 2))


class Snapshots(unittest.TestCase):
    def test_delta_from_snapshot_7_days_back(self):
        old = {"o/a": {"likes": 1, "rank": 2}, "o/b": {"likes": 4, "rank": 1}}
        w = World([entry("o/a"), entry("o/b"), entry("o/c")],
                  {"o/a": [node("x"), node("y"), node("z")], "o/b": [node("x")], "o/c": []},
                  stats={f"snapshots/{WEEK_AGO}.json": old})
        w.run()
        likes = w.written("likes.json")
        self.assertEqual((likes["o/a"]["delta_7d"], likes["o/a"]["rank_delta_7d"]), (2, 1))
        self.assertEqual((likes["o/b"]["delta_7d"], likes["o/b"]["rank_delta_7d"]), (-3, -1))
        self.assertEqual((likes["o/c"]["delta_7d"], likes["o/c"]["rank_delta_7d"]), (0, 0))

    def snap(self, days_ago, data):
        return {f"snapshots/{NOW - timedelta(days=days_ago):%Y-%m-%d}.json": data}

    def likes_for(self, stats):
        w = World([entry("o/a"), entry("o/new")],
                  {"o/a": [node("x"), node("y"), node("z")], "o/new": [node("x"), node("y")]}, stats=stats)
        w.run()
        return w.written("likes.json")

    def test_oldest_snapshot_used_when_no_7_day(self):
        stats = {**self.snap(3, {"o/a": {"likes": 2, "rank": 2}}), **self.snap(1, {"o/a": {"likes": 3, "rank": 1}})}
        a = self.likes_for(stats)["o/a"]
        self.assertEqual((a["delta_7d"], a["rank_delta_7d"]), (1, 1))

    def test_baseline_never_older_than_7_days(self):
        stats = {**self.snap(10, {"o/a": {"likes": 0, "rank": 1}}), **self.snap(4, {"o/a": {"likes": 2, "rank": 3}})}
        a = self.likes_for(stats)["o/a"]
        self.assertEqual((a["delta_7d"], a["rank_delta_7d"]), (1, 2))

    def test_impossible_and_non_ascii_dates_skipped(self):
        stats = {**self.snap(3, {"o/a": {"likes": 2, "rank": 1}}),
                 "snapshots/2026-10-35.json": {"o/a": {"likes": 0, "rank": 1}},
                 "snapshots/2026-10-٣٠.json": {"o/a": {"likes": 0, "rank": 1}},
                 "snapshots/2026-10-31.json.bak": {"o/a": {"likes": 0, "rank": 1}}}
        self.assertEqual(self.likes_for(stats)["o/a"]["delta_7d"], 1)

    def test_unreadable_baseline_raises_without_writes(self):
        w = World([entry("o/a")], {"o/a": [node("x")]}, stats=self.snap(3, {}))
        del w.routes[f"GET {API}/contents/snapshots/{NOW - timedelta(days=3):%Y-%m-%d}.json"]
        with self.assertRaises(GitHubError):
            w.run()
        self.assertEqual(w.puts(), [])

    def test_empty_baseline_file_counts_from_zero(self):
        got = self.likes_for(self.snap(3, {}))
        self.assertEqual(got["o/a"]["delta_7d"], 3)

    def test_only_today_snapshot_gives_zero(self):
        got = self.likes_for(self.snap(0, {"o/a": {"likes": 0, "rank": 1}}))
        self.assertEqual((got["o/a"]["delta_7d"], got["o/a"]["rank_delta_7d"]), (0, 0))

    def test_new_entry_delta_is_its_likes(self):
        got = self.likes_for(self.snap(3, {"o/a": {"likes": 1, "rank": 1}}))
        self.assertEqual((got["o/new"]["delta_7d"], got["o/new"]["rank_delta_7d"]), (2, 0))

    def test_exact_7_day_snapshot_preferred(self):
        stats = {**self.snap(7, {"o/a": {"likes": 2, "rank": 1}}), **self.snap(3, {"o/a": {"likes": 3, "rank": 1}})}
        self.assertEqual(self.likes_for(stats)["o/a"]["delta_7d"], 1)

    def test_listing_error_raises_without_writes(self):
        w = World([entry("o/a")], {"o/a": [node("x")]})
        w.routes[f"GET {API}/contents/snapshots"] = Status(500)
        with self.assertRaises(GitHubError):
            w.run()
        self.assertEqual(w.puts(), [])

    def test_missing_snapshot_delta_zero(self):
        w = World([entry("o/a")], {"o/a": [node("x"), node("y")]})
        w.run()
        self.assertEqual(w.written("likes.json")["o/a"], {
            "likes": 2, "delta_7d": 0, "rank": 1, "rank_delta_7d": 0, "flagged": False})

    def test_snapshot_written_once_per_day(self):
        w = World([entry("o/a")], {"o/a": [node("x")]})
        w.run()
        self.assertEqual(w.written(f"snapshots/{TODAY}.json"), {"o/a": {"likes": 1, "rank": 1}})
        w2 = World([entry("o/a")], {"o/a": [node("x"), node("y")]},
                   stats={f"snapshots/{TODAY}.json": {"o/a": {"likes": 1, "rank": 1}}})
        w2.run()
        self.assertIsNone(w2.written(f"snapshots/{TODAY}.json"))
        self.assertEqual(w2.written("likes.json")["o/a"]["likes"], 2)


class Renames(unittest.TestCase):
    def snap(self, data):
        return {f"snapshots/{WEEK_AGO}.json": data}

    def test_baseline_follows_a_rename(self):
        w = World([entry("o/new")], {"o/new": [node("x"), node("y"), node("z"), node("w")]},
                  stats={**self.snap({"o/old": {"likes": 3, "rank": 2}}),
                         "renames.json": {"o/old": {"to": "o/new", "at": iso(NOW - timedelta(days=2))}}})
        w.run()
        got = w.written("likes.json")["o/new"]
        self.assertEqual((got["delta_7d"], got["rank_delta_7d"]), (1, 1))

    def test_chained_renames_replay_in_order(self):
        w = World([entry("o/c")], {"o/c": [node("x"), node("y")]},
                  stats={**self.snap({"o/a": {"likes": 1, "rank": 1}}),
                         "renames.json": {"o/b": {"to": "o/c", "at": iso(NOW - timedelta(days=1))},
                                          "o/a": {"to": "o/b", "at": iso(NOW - timedelta(days=3))}}})
        w.run()
        self.assertEqual(w.written("likes.json")["o/c"]["delta_7d"], 1)

    def test_rename_older_than_the_baseline_is_not_replayed(self):
        # The snapshot already holds the new name, and the old one now belongs to someone else.
        w = World([entry("o/a"), entry("o/new")], {"o/a": [node("x")], "o/new": [node("x"), node("y")]},
                  stats={**self.snap({"o/a": {"likes": 0, "rank": 2}, "o/new": {"likes": 2, "rank": 1}}),
                         "renames.json": {"o/a": {"to": "o/new", "at": iso(NOW - timedelta(days=9))}}})
        w.run()
        likes = w.written("likes.json")
        self.assertEqual((likes["o/a"]["delta_7d"], likes["o/new"]["delta_7d"]), (1, 0))

    def test_rename_then_sync_keeps_likes_and_trend(self):
        from scripts.rot import Action, finish
        from scripts.tests.test_rot import store, threads

        row = {"likes": 3, "delta_7d": 0, "rank": 1, "rank_delta_7d": 0, "flagged": False}
        r, state = store({"discussions.json": {"o/old": 1}, "likes.json": {"o/old": row}}, threads({1: "o/old"}))
        plan = {"changed": True, "actions": [Action("rename", "o/new", "", "o/old").__dict__], "rot_state": {}}
        finish(FakeGitHub(r), plan, now=NOW - timedelta(days=2))
        self.assertEqual(state["likes.json"], {"o/new": row})

        w = World([entry("o/new")], {"o/new": [node("x"), node("y"), node("z"), node("w")]},
                  stats={**state, **self.snap({"o/old": {"likes": 3, "rank": 1}})})
        w.run()
        got = w.written("likes.json")["o/new"]
        self.assertEqual((got["likes"], got["delta_7d"], got["rank_delta_7d"]), (4, 1, 0))


class Writes(unittest.TestCase):
    def test_write_order_and_commit_shape(self):
        w = World([entry("o/a")], {"o/a": [node("x")]})
        w.run()
        paths = [p.removeprefix(f"{API}/contents/") for _, p, _ in w.puts()]
        self.assertEqual(paths, ["meta.json", "likes.json", f"snapshots/{TODAY}.json", "trust-cache.json"])

    def test_flags_written_first(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)})
        w.run()
        paths = [p.removeprefix(f"{API}/contents/") for _, p, _ in w.puts()]
        self.assertEqual(paths, ["flags.json", "meta.json", "likes.json", f"snapshots/{TODAY}.json",
                                 "trust-cache.json"])
        for _, _, body in w.puts():
            self.assertEqual(body["branch"], "stats")
            self.assertEqual(body["committer"]["name"], "devstreamhq-bot[bot]")
        self.assertNotIn("sha", w.puts()[0][2])

    def test_meta_shape_and_skips_deleted_repo(self):
        w = World([entry("o/a"), entry("o/gone")], {"o/a": [node("x")], "o/gone": [node("x")]},
                  stars={"o/a": 3, "o/gone": None})
        w.run()
        self.assertEqual(w.written("meta.json"), {"o/a": {
            "stars": 3, "language": "Go", "language_color": "#00ADD8",
            "pushed_at": "2026-11-01T00:00:00Z", "avatar_url": "https://a/x.png"}})
        self.assertIn("o/gone", w.written("likes.json"))

    test_meta_skips_deleted_repo = test_meta_shape_and_skips_deleted_repo

    def test_meta_forbidden_repo_keeps_last_card(self):
        # An org IP allow list hides one repo from the App token; the rest of the run goes on.
        old = {"stars": 9, "language": "Go", "language_color": "#00ADD8",
               "pushed_at": "2026-10-01T00:00:00Z", "avatar_url": "https://a/old.png"}
        w = World([entry("o/a"), entry("o/ip")], {"o/a": [node("x")], "o/ip": [node("y")]},
                  stars={"o/a": 3, "o/ip": "forbidden"}, stats={"meta.json": {"o/ip": old}})
        w.run()
        meta = w.written("meta.json")
        self.assertEqual((meta["o/a"]["stars"], meta["o/ip"]), (3, old))
        self.assertEqual(w.written("likes.json")["o/ip"]["likes"], 1)

    def test_replaced_repo_keeps_last_card_and_is_flagged(self):
        # A different repo at the listed name: its stars and avatar mustn't reach the card or Trending.
        old = {"stars": 9, "language": "Go", "language_color": "#00ADD8",
               "pushed_at": "2026-10-01T00:00:00Z", "avatar_url": "https://a/old.png"}
        for status in ("live", "flagged"):
            w = World([entry("o/a", repo_id=1), entry("o/sq", status, repo_id=5)],
                      {"o/a": [node("x")], "o/sq": [node("y")]},
                      stars={"o/a": 3, "o/sq": 500}, ids={"o/a": 1, "o/sq": 6}, stats={"meta.json": {"o/sq": old}})
            with self.assertLogs(level="WARNING") as logs:
                w.run()
            self.assertTrue(any("o/sq" in m for m in logs.output))
            meta = w.written("meta.json")
            self.assertEqual((meta["o/a"]["stars"], meta["o/sq"]), (3, old))
            self.assertNotIn("repo_id", meta["o/a"])
            likes = w.written("likes.json")
            self.assertTrue(likes["o/sq"]["flagged"], status)
            self.assertFalse(likes["o/a"]["flagged"])

    def test_archived_and_superseded_entries_keep_a_card(self):
        # The Archive page shows their stars; only an entry gone from entries.json loses its card.
        old = {"stars": 9, "language": "Go", "language_color": "#00ADD8",
               "pushed_at": "2026-10-01T00:00:00Z", "avatar_url": "https://a/old.png"}
        # A hidden entry is off every page while a maintainer reviews it, so it gets no card.
        w = World([entry("o/a"), entry("o/arch", "archived"), entry("o/sup", "superseded"), entry("o/hid", "hidden")],
                  {"o/a": [node("x")]}, stars={"o/a": 3, "o/arch": 7, "o/sup": 4, "o/hid": 5},
                  stats={"meta.json": {"o/removed": old, "o/hid": old}})
        w.run()
        meta = w.written("meta.json")
        self.assertEqual({k: v["stars"] for k, v in meta.items()}, {"o/a": 3, "o/arch": 7, "o/sup": 4})
        [(_, _, variables)] = [c for c in w.gh.calls if c[1] == "RepoMeta"]
        self.assertNotIn("hid", variables.values())
        self.assertEqual(set(w.written("likes.json")), {"o/a"})

    def test_meta_batched_per_50(self):
        repos = {f"o/r{i:03}": [] for i in range(51)}
        w = World([entry(r) for r in repos], repos)
        w.run()
        self.assertEqual(len([c for c in w.gh.calls if c[1] == "RepoMeta"]), 2)

    def test_unchanged_files_not_rewritten(self):
        w = World([entry("o/a")], {"o/a": [node("x")]})
        w.run()
        stats = {p: w.written(p) for p in ("meta.json", "likes.json", "trust-cache.json")}
        stats[f"snapshots/{TODAY}.json"] = w.written(f"snapshots/{TODAY}.json")
        w2 = World([entry("o/a")], {"o/a": [node("x")]}, stats=stats)
        w2.run()
        self.assertEqual(w2.puts(), [])
        self.assertFalse([c for c in w2.gh.calls if c[1] == "/users/x"])

    def test_trust_cache_persisted(self):
        w = World([entry("o/a")], {"o/a": [node("x"), node("y", NEW)]})
        w.run()
        cache = w.written("trust-cache.json")
        self.assertIs(cache["x"]["trusted"], True)
        self.assertIs(cache["y"]["trusted"], False)

    def test_missing_stats_branch(self):
        w = World([entry("o/a")], {"o/a": [node("x")]})
        del w.gh.routes[f"GET {API}/git/ref/heads/stats"]
        with self.assertRaises(GitHubError):
            w.run()
        self.assertEqual(w.puts(), [])


class Conflicts(unittest.TestCase):
    """A 409 on the stats branch means another writer got there first: re-read, re-apply, write again."""

    def conflict_once(self, w, path, then=None):
        n = []

        def put(_):
            n.append(1)
            if len(n) == 1:
                if then:
                    then()
                raise GitHubError(409, "conflict")
            return {}
        w.routes[f"PUT {API}/contents/{path}"] = put
        return n

    def test_flags_409_then_success_keeps_both_records(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)})
        other = {"o/z": {"burst": {"kind": "burst", "issue": 5, "opened_at": iso(NOW), "reviewed_at": None}}}

        def someone_wrote():
            w.routes[f"GET {API}/contents/flags.json"] = {
                "content": base64.b64encode(json.dumps(other).encode()).decode(), "sha": "sha-new"}
        n = self.conflict_once(w, "flags.json", someone_wrote)
        w.run()
        self.assertEqual(len(n), 2)
        self.assertEqual(titles(w), ["Like burst: o/a"])
        got = w.written("flags.json")
        self.assertEqual((got["o/a"]["burst"]["issue"], got["o/z"]["burst"]["issue"]), (100, 5))
        self.assertEqual(w.puts()[-5][2]["sha"], "sha-new")

    def test_flags_409_then_success_opens_no_duplicate_next_run(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)})
        self.conflict_once(w, "flags.json")
        w.run()
        w.persist()
        w.run(now=NOW + timedelta(minutes=5))
        self.assertEqual(w.issues_opened(), [])

    def test_likes_and_meta_409_after_someone_else_wrote_skips_this_hour(self):
        w = World([entry("o/a")], {"o/a": [node("x"), node("y")]})
        theirs = {"o/a": {"likes": 9}}
        for path in ("likes.json", "meta.json"):
            def someone_wrote(path=path):
                w.routes[f"GET {API}/contents/{path}"] = {
                    "content": base64.b64encode(json.dumps(theirs).encode()).decode(), "sha": "sha-theirs"}
            n = self.conflict_once(w, path, someone_wrote)
            w.run()
            self.assertEqual(len(n), 1, path)
            w.gh.calls.clear()

    def test_likes_409_then_success(self):
        w = World([entry("o/a")], {"o/a": [node("x"), node("y")]})
        n = self.conflict_once(w, "likes.json")
        w.run()
        self.assertEqual(len(n), 2)
        self.assertEqual(w.written("likes.json")["o/a"]["likes"], 2)


class Size(unittest.TestCase):
    def test_trust_cache_pruned_past_ttl_and_compact(self):
        stale = {"checked_at": iso(NOW - timedelta(days=8)), "trusted": True, "reason": None, "created_at": OLD}
        w = World([entry("o/a")], {"o/a": [node("x")]}, stats={"trust-cache.json": {"gone": stale}})
        w.run()
        self.assertEqual(list(w.written("trust-cache.json")), ["x"])
        put = [b for _, p, b in w.puts() if p.endswith("trust-cache.json")][0]
        self.assertNotIn("\n ", base64.b64decode(put["content"]).decode())

    def test_only_the_last_30_snapshots_are_kept(self):
        stats = {f"snapshots/{NOW - timedelta(days=d):%Y-%m-%d}.json": {} for d in range(1, 33)}
        w = World([entry("o/a")], {"o/a": [node("x")]}, stats=stats)
        for p in stats:
            w.routes[f"DELETE {API}/contents/{p}"] = None
        w.run()
        deletes = [(i, c) for i, c in enumerate(w.gh.calls) if c[0] == "DELETE"]
        old = [f"{API}/contents/snapshots/{NOW - timedelta(days=d):%Y-%m-%d}.json" for d in (32, 31, 30)]
        self.assertEqual([c[1] for _, c in deletes], old)
        self.assertEqual(deletes[0][1][2]["sha"], f"sha-snapshots/{NOW - timedelta(days=32):%Y-%m-%d}.json")
        snap_put = max(i for i, c in enumerate(w.gh.calls) if c[0] == "PUT" and c[1].endswith(f"{TODAY}.json"))
        self.assertGreater(deletes[0][0], snap_put)

    def test_30_or_fewer_snapshots_deletes_nothing(self):
        stats = {f"snapshots/{NOW - timedelta(days=d):%Y-%m-%d}.json": {} for d in range(1, 30)}
        w = World([entry("o/a")], {"o/a": [node("x")]}, stats=stats)
        w.run()
        self.assertFalse([c for c in w.gh.calls if c[0] == "DELETE"])


class HalfDoneRename(unittest.TestCase):
    ROW = {"likes": 2, "delta_7d": 0, "rank": 1, "rank_delta_7d": 0, "flagged": False}

    def world(self, likes_age):
        # The rename PR merged but rot's finish hasn't moved discussions.json yet.
        w = World([entry("o/new")], {"o/old": [node("x"), node("y")]}, stats={"likes.json": {"o/old": self.ROW}})
        w.routes[f"GET {API}/commits"] = [{"commit": {"committer": {"date": iso(NOW - likes_age)}}}]
        return w

    def writes(self, w):
        return [c for c in w.gh.calls if c[0] in ("PUT", "POST", "PATCH", "DELETE")]

    def test_rename_within_3_hours_writes_nothing(self):
        w = self.world(timedelta(hours=2))
        w.run()
        self.assertEqual(self.writes(w), [])

    def test_rename_after_3_hours_syncs_anyway(self):
        w = self.world(timedelta(hours=4))
        with self.assertLogs(level="ERROR"):
            w.run()
        self.assertIn("o/new", w.written("likes.json"))

    def test_removed_entry_and_new_entry_still_sync(self):
        # o/gone was removed long ago: it isn't in likes.json, so it's no rename in flight.
        w = World([entry("o/a"), entry("o/b")], {"o/a": [node("x")], "o/gone": [node("y")]},
                  stats={"likes.json": {"o/a": self.ROW}})
        w.run()
        self.assertEqual(sorted(w.written("likes.json")), ["o/a", "o/b"])

    def test_new_entry_without_thread_still_syncs(self):
        w = World([entry("o/a"), entry("o/b")], {"o/a": [node("x")]})
        w.run()
        self.assertEqual(w.written("likes.json")["o/b"]["likes"], 0)

if __name__ == "__main__":
    unittest.main()
