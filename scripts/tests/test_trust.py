import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from scripts.devstream.gh import GitHubError
from scripts.devstream.trust import TrustCache, blocked_logins, check_user
from scripts.tests.fake_gh import FakeGitHub, Status

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
BLOCKS = "GET /orgs/devstreamhq/blocks"


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def gh(age=timedelta(days=400), repos=1, contribs=0, type_="User", extra=None):
    routes = {
        "GET /users/alice": {"created_at": iso(NOW - age), "public_repos": repos, "type": type_},
        "GRAPHQL UserActivity": {"user": {"contributionsCollection": {"contributionCalendar": {"totalContributions": contribs}}}},
    }
    routes.update(extra or {})
    return FakeGitHub(routes)


class CheckUserTest(unittest.TestCase):
    def check(self, g, login="alice", excluded=()):
        return check_user(login, g, NOW, set(excluded))

    def test_trusted(self):
        self.assertIsNone(self.check(gh()))

    def test_29_day_account_rejected(self):
        r = self.check(gh(age=timedelta(days=29, hours=23)))
        self.assertEqual(r, "Your account needs to be at least 30 days old.")

    def test_30_day_account_ok(self):
        self.assertIsNone(self.check(gh(age=timedelta(days=30))))

    def test_zero_repos_but_contributions_ok(self):
        g = gh(repos=0, contribs=3)
        self.assertIsNone(self.check(g))
        v = [c for c in g.calls if c[0] == "GRAPHQL"][0][2]
        self.assertEqual(v, {"login": "alice", "from": iso(NOW - timedelta(days=365))})

    def test_no_activity_rejected(self):
        self.assertEqual(
            self.check(gh(repos=0, contribs=0)),
            "We couldn't find public activity on your account in the last year.",
        )

    def test_repos_skip_graphql(self):
        g = gh(repos=2)
        self.check(g)
        self.assertFalse([c for c in g.calls if c[0] == "GRAPHQL"])

    def test_blocked_rejected_without_api_call(self):
        # Callers fold the org block list into excluded, so no per-login block lookup is made.
        g = gh()
        self.assertEqual(self.check(g, excluded=blocked_logins(FakeGitHub({BLOCKS: [{"login": "Alice"}]}))),
                         "This account can't submit to DevStream.")
        self.assertEqual(g.calls, [])

    def test_excluded_rejected_without_api_call(self):
        g = gh()
        self.assertEqual(self.check(g, excluded={"ALICE"}), "This account can't submit to DevStream.")
        self.assertEqual(g.calls, [])

    def test_bot_type_and_suffix(self):
        msg = "Bot accounts can't submit or Like."
        self.assertEqual(self.check(gh(type_="Bot")), msg)
        self.assertEqual(self.check(gh(), login="dependabot[bot]"), msg)

    def test_bad_login_no_api_call(self):
        for bad in ("", "-a", "a/../b", "a" * 40, "a b", "a_b"):
            g = gh()
            self.assertIsNotNone(self.check(g, login=bad), bad)
            self.assertEqual(g.calls, [], bad)

    def test_missing_user(self):
        r = self.check(FakeGitHub({}))
        self.assertEqual(r, "We couldn't find that GitHub account.")

    def test_api_error_propagates(self):
        with self.assertRaises(GitHubError):
            self.check(gh(extra={"GET /users/alice": Status(500)}))

        def boom(v):
            raise GitHubError(502, "x")

        with self.assertRaises(GitHubError):
            self.check(gh(repos=0, extra={"GRAPHQL UserActivity": boom}))


class BlockedLoginsTest(unittest.TestCase):
    def test_lowercased_set_from_one_listing(self):
        g = FakeGitHub({BLOCKS: [{"login": "Alice"}, {"login": "bob"}]})
        self.assertEqual(blocked_logins(g), {"alice", "bob"})
        self.assertEqual([c[1] for c in g.calls],
                         ["/orgs/devstreamhq/blocks?per_page=1", "/orgs/devstreamhq/blocks?per_page=100"])

    def test_empty_list_ok(self):
        self.assertEqual(blocked_logins(FakeGitHub({BLOCKS: []})), set())

    def test_404_raises(self):
        with self.assertRaises(GitHubError) as c:
            blocked_logins(FakeGitHub({}))
        self.assertEqual(c.exception.status, 404)

    def test_other_error_propagates(self):
        with self.assertRaises(GitHubError) as c:
            blocked_logins(FakeGitHub({BLOCKS: Status(500)}))
        self.assertEqual(c.exception.status, 500)


class TrustCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "trust.json")

    def test_trusted_caches_and_reuses(self):
        c = TrustCache(self.path)
        g = gh()
        self.assertTrue(c.trusted("alice", g, NOW, set()))
        n = len(g.calls)
        self.assertTrue(c.trusted("alice", g, NOW + timedelta(days=6), set()))
        # The block list is fetched once per run by the caller, so a hit costs nothing.
        self.assertEqual(g.calls[n:], [])

    def test_ttl_expiry(self):
        c = TrustCache(self.path)
        c.put("alice", {"trusted": True, "reason": None, "created_at": "x"}, NOW)
        self.assertIsNotNone(c.get("alice", NOW + timedelta(days=7)))
        self.assertIsNone(c.get("alice", NOW + timedelta(days=7, seconds=1)))

    def test_untrusted_cached_with_reason(self):
        c = TrustCache(self.path)
        g = gh(age=timedelta(days=2))
        self.assertFalse(c.trusted("alice", g, NOW, set()))
        self.assertIn("30 days", c.get("alice", NOW)["reason"])

    def test_excluded_rechecked_never_trusted(self):
        c = TrustCache(self.path)
        c.put("alice", {"trusted": True, "reason": None, "created_at": "x"}, NOW)
        self.assertFalse(c.trusted("alice", gh(), NOW, {"alice"}))
        self.assertFalse((c.get("alice", NOW) or {}).get("trusted"))

    def test_blocked_result_not_stored(self):
        c = TrustCache(self.path)
        self.assertFalse(c.trusted("alice", gh(), NOW, {"alice"}))
        self.assertIsNone(c.get("alice", NOW))
        self.assertTrue(c.trusted("alice", gh(), NOW, set()))

    def test_error_not_cached(self):
        c = TrustCache(self.path)
        with self.assertRaises(GitHubError):
            c.trusted("alice", gh(extra={"GET /users/alice": Status(500)}), NOW, set())
        self.assertIsNone(c.get("alice", NOW))

    def test_cached_trusted_then_blocked(self):
        c = TrustCache(self.path)
        self.assertTrue(c.trusted("alice", gh(), NOW, set()))
        g = gh()
        self.assertFalse(c.trusted("alice", g, NOW + timedelta(days=1), {"alice"}))
        self.assertIsNone(c.get("alice", NOW))
        self.assertEqual(g.calls, [])

    def test_cache_hit_invalid_login_makes_no_call(self):
        c = TrustCache(self.path)
        c.entries["a_b"] = {"trusted": True, "reason": None, "checked_at": iso(NOW), "created_at": None}
        g = gh()
        self.assertFalse(c.trusted("a_b", g, NOW, set()))
        self.assertEqual(g.calls, [])

    def test_bad_file_shapes_load_empty(self):
        for raw in (b"[]", b"3", b"\xff\xfe\x00bad", b"{nope"):
            with open(self.path, "wb") as f:
                f.write(raw)
            self.assertEqual(TrustCache(self.path).entries, {}, raw)

    def test_bad_entries_are_misses(self):
        c = TrustCache(self.path)
        good = {"trusted": True, "reason": None, "created_at": None}
        for e in (
            good,
            {**good, "checked_at": "garbage"},
            {**good, "checked_at": 5},
            {**good, "checked_at": "2026-11-05T12:00:00"},
            "str",
            None,
        ):
            c.entries["alice"] = e
            self.assertIsNone(c.get("alice", NOW), e)
            self.assertIsNone(c.account_age_days("alice", NOW), e)

    def test_string_false_is_not_trusted(self):
        c = TrustCache(self.path)
        c.entries["alice"] = {"trusted": "false", "reason": None, "checked_at": iso(NOW), "created_at": None}
        self.assertFalse(c.trusted("alice", gh(age=timedelta(days=1)), NOW, set()))

    def test_future_checked_at_is_miss(self):
        c = TrustCache(self.path)
        c.put("alice", {"trusted": True, "reason": None, "created_at": None}, NOW + timedelta(hours=1))
        self.assertIsNone(c.get("alice", NOW))

    def test_login_case_shares_entry(self):
        c = TrustCache(self.path)
        c.put("Alice", {"trusted": True, "reason": None, "created_at": None}, NOW)
        self.assertIsNotNone(c.get("alice", NOW))

    def test_save_roundtrip_and_format(self):
        c = TrustCache(self.path)
        c.trusted("alice", gh(), NOW, set())
        c.save()
        self.assertEqual(os.listdir(self.dir.name), ["trust.json"])
        e = json.load(open(self.path))["alice"]
        self.assertEqual(set(e), {"trusted", "reason", "checked_at", "created_at"})
        self.assertTrue(e["trusted"])
        self.assertEqual(TrustCache(self.path).get("alice", NOW), e)

    def test_account_age_days(self):
        c = TrustCache(self.path)
        self.assertIsNone(c.account_age_days("alice", NOW))
        c.trusted("alice", gh(age=timedelta(days=45, hours=5)), NOW, set())
        self.assertEqual(c.account_age_days("alice", NOW), 45)

    def test_missing_user_age_none(self):
        c = TrustCache(self.path)
        c.trusted("alice", FakeGitHub({}), NOW, set())
        self.assertIsNone(c.account_age_days("alice", NOW))


if __name__ == "__main__":
    unittest.main()
