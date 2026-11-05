import base64
import json
import unittest
from datetime import datetime, timedelta, timezone

from scripts.devstream.gh import GitHubError
from scripts.devstream.likes import count_likes, detect_burst, rank_entries
from scripts.sync_likes import run
from scripts.tests.fake_gh import FakeGitHub

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
API = "/repos/devstreamhq/devstream"
OLD = "2020-01-01T00:00:00Z"  # an established account
NEW = "2026-10-30T00:00:00Z"  # 6 days old: untrusted and under 90 days
TODAY = "2026-11-05"
WEEK_AGO = "2026-10-29"


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


RECENT = iso(NOW - timedelta(hours=2))
LONG_AGO = iso(NOW - timedelta(days=3))


def node(login, created=OLD, reacted=LONG_AGO):
    return {"createdAt": reacted, "user": None if login is None else {"login": login, "createdAt": created}}


def entry(repo, status="live"):
    return {"repo": repo, "status": status, "description": "d"}


class World:
    """A FakeGitHub with discussions, users, repo meta and stats files wired up."""

    def __init__(self, entries, discussions, users=None, stats=None, stars=None, issues=(), page=100):
        self.entries = entries
        self.discussions = discussions  # {repo: [node, ...]}
        numbers = {normalize(r): i + 1 for i, r in enumerate(discussions)}
        self.by_number = {numbers[normalize(r)]: n for r, n in discussions.items()}
        self.stats = {"discussions.json": numbers, **(stats or {})}
        self.stars = stars or {}
        self.page = page
        self.cursors = None  # set to a constant to simulate a stuck cursor
        users = dict(users or {})
        for nodes in discussions.values():
            for n in nodes:
                if n["user"]:
                    users.setdefault(n["user"]["login"], n["user"]["createdAt"])
        routes = {
            f"GET {API}/git/ref/heads/stats": {"ref": "refs/heads/stats"},
            "GET /orgs/devstreamhq/blocks?per_page=1": [],
            "GRAPHQL DiscussionReactors": self._reactors,
            "GRAPHQL RepoMeta": self._meta,
            f"GET {API}/issues": list(issues),
            f"POST {API}/issues": {"number": 99},
        }
        for login, created in users.items():
            routes[f"GET /users/{login}"] = {
                "login": login, "created_at": created, "public_repos": 1, "type": "User"}
        for path, data in self.stats.items():
            routes[f"GET {API}/contents/{path}"] = {
                "content": base64.b64encode(json.dumps(data).encode()).decode(), "sha": f"sha-{path}"}
        for path in ("meta.json", "likes.json", "trust-cache.json", f"snapshots/{TODAY}.json"):
            routes[f"PUT {API}/contents/{path}"] = {}
        self.routes = routes
        self.gh = FakeGitHub(routes)

    def _reactors(self, v):
        repo = {}
        i = 0
        while f"n{i}" in v:
            nodes = self.by_number[v[f"n{i}"]]
            start = 0 if self.cursors or i else int(v.get("cursor") or 0)
            chunk = nodes[start:start + self.page]
            more = start + self.page < len(nodes)
            cursor = self.cursors or str(start + self.page)
            repo[f"d{i}"] = {"reactions": {
                "nodes": chunk, "pageInfo": {"hasNextPage": more, "endCursor": cursor if more else None}}}
            i += 1
        return {"repository": repo}

    def _meta(self, v):
        out = {}
        i = 0
        while f"o{i}" in v:
            key = f"{v[f'o{i}']}/{v[f'n{i}']}"
            stars = self.stars.get(key, 0)
            out[f"r{i}"] = None if stars is None else {
                "nameWithOwner": key, "stargazerCount": stars,
                "primaryLanguage": {"name": "Go", "color": "#00ADD8"},
                "pushedAt": "2026-11-01T00:00:00Z", "owner": {"avatarUrl": "https://a/x.png"}}
            i += 1
        return out

    def run(self, excluded=frozenset()):
        run(self.gh, self.entries, NOW, excluded)

    def puts(self):
        return [c for c in self.gh.calls if c[0] == "PUT"]

    def written(self, path):
        for m, p, body in reversed(self.puts()):
            if p == f"{API}/contents/{path}":
                return json.loads(base64.b64decode(body["content"]))
        return None

    def issues_opened(self):
        return [c[2] for c in self.gh.calls if c[0] == "POST" and c[1] == f"{API}/issues"]


def normalize(r):
    return r.lower()


def burst_nodes(n, young, prefix="u"):
    return [node(f"{prefix}{i}", NEW if i < young else OLD, RECENT) for i in range(n)]


class Counting(unittest.TestCase):
    def test_untrusted_not_counted(self):
        self.assertEqual(count_likes(["a", "b", "c"], lambda u: u != "b"), 2)
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

    def test_burst_counts_untrusted_reactors(self):
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)})
        w.run()
        self.assertEqual(w.written("likes.json")["o/a"], {
            "likes": 0, "delta_7d": 0, "rank": 1, "rank_delta_7d": 0, "flagged": True})
        self.assertEqual(len(w.issues_opened()), 1)

    def test_burst_report_has_no_logins(self):
        w = World([entry("Owner/Tool")], {"Owner/Tool": burst_nodes(12, 8, prefix="zqx")})
        w.run()
        [issue] = w.issues_opened()
        self.assertEqual(issue["title"], "Like burst: Owner/Tool")
        self.assertEqual(sorted(issue["labels"]), ["needs-review", "report"])
        self.assertNotIn("zqx", issue["body"])
        self.assertNotIn("@", issue["body"])
        self.assertIn("12", issue["body"])

    def test_burst_report_deduped(self):
        open_issue = {"number": 5, "title": "Like burst: o/a", "labels": [{"name": "report"}]}
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)}, issues=[open_issue])
        w.run()
        self.assertEqual(w.issues_opened(), [])
        self.assertTrue(w.written("likes.json")["o/a"]["flagged"])
        listing = [c for c in w.gh.calls if c[0] == "GET" and c[1].startswith(f"{API}/issues")]
        self.assertIn("labels=report", listing[0][1])
        self.assertIn("state=open", listing[0][1])

    def test_no_second_report_while_still_flagged(self):
        prev = {"o/a": {"likes": 0, "delta_7d": 0, "rank": 1, "rank_delta_7d": 0, "flagged": True}}
        w = World([entry("o/a")], {"o/a": burst_nodes(10, 10)}, stats={"likes.json": prev})
        w.run()
        self.assertEqual(w.issues_opened(), [])

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


class Writes(unittest.TestCase):
    def test_write_order_and_commit_shape(self):
        w = World([entry("o/a")], {"o/a": [node("x")]})
        w.run()
        paths = [p.removeprefix(f"{API}/contents/") for _, p, _ in w.puts()]
        self.assertEqual(paths, ["meta.json", "likes.json", f"snapshots/{TODAY}.json", "trust-cache.json"])
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


if __name__ == "__main__":
    unittest.main()
