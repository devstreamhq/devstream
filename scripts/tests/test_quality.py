import base64
import time
import unittest
from datetime import datetime, timedelta, timezone

from scripts.devstream.gh import GitHubError
from scripts.devstream.quality import Undecided, check_repo
from scripts.tests.fake_gh import FakeGitHub, Redirect

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
R = "/repos/alice/tool"
MSG_SHORT = "Your README needs a bit more detail (at least a short paragraph about what it does)."
MSG_NEW = (
    "New repos need to be at least 30 days old before they can be listed. "
    "Come back once yours is a month old."
)
README = "This tool does a useful thing for people who need it. " * 8


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def b64(s):
    return {"content": base64.encodebytes(s.encode()).decode(), "encoding": "base64"}


def repo(**kw):
    r = {
        "full_name": "alice/tool",
        "description": "A handy tool",
        "fork": False,
        "archived": False,
        "pushed_at": iso(NOW - timedelta(days=5)),
        "created_at": "2020-01-01T00:00:00Z",
        "license": {"spdx_id": "MIT"},
        "stargazers_count": 42,
        "language": "Python",
        "owner": {"login": "alice", "avatar_url": "https://a/x.png"},
        "default_branch": "main",
        "html_url": "https://github.com/alice/tool",
        "topics": ["cli"],
    }
    r.update(kw)
    return r


def gh(repo_kw=None, readme=README, extra=None, topics=("cli",), contributing=True, coc=True):
    routes = {
        f"GET {R}": repo(topics=list(topics), **(repo_kw or {})),
        f"GET {R}/community/profile": {
            "files": {
                "contributing": {} if contributing else None,
                "code_of_conduct": {} if coc else None,
                "issue_template": None,
                "pull_request_template": None,
                "license": None,
            }
        },
    }
    if readme is not None:
        routes[f"GET {R}/readme"] = b64(readme)
    routes.update(extra or {})
    return FakeGitHub(routes)


def run(g, mode="submit", full="alice/tool"):
    return check_repo(full, g, NOW, mode)


class SubmitTest(unittest.TestCase):
    def test_good_repo_passes_and_meta(self):
        r = run(gh())
        self.assertEqual((r.exists, r.canonical, r.failures, r.rot), (True, "alice/tool", [], "ok"))
        self.assertEqual(
            r.meta,
            {
                "stars": 42,
                "language": "Python",
                "pushed_at": iso(NOW - timedelta(days=5)),
                "description": "A handy tool",
                "avatar_url": "https://a/x.png",
                "default_branch": "main",
                "html_url": "https://github.com/alice/tool",
            },
        )

    def test_missing_repo(self):
        g = FakeGitHub({})
        r = run(g)
        self.assertEqual((r.exists, r.rot, r.failures), (False, "remove", ["This repo no longer exists."]))
        self.assertEqual(len(g.calls), 1)

    def test_push_90_days_boundary(self):
        ok = run(gh({"pushed_at": iso(NOW - timedelta(days=90))}))
        self.assertEqual(ok.failures, [])
        bad = run(gh({"pushed_at": iso(NOW - timedelta(days=90, seconds=1))}))
        self.assertEqual(len(bad.failures), 1)
        self.assertIn("90 days", bad.failures[0])

    def test_repo_29_days_old_rejected(self):
        r = run(gh({"created_at": iso(NOW - timedelta(days=29, hours=23))}))
        self.assertEqual(r.failures, [MSG_NEW])

    def test_repo_30_days_old_ok(self):
        r = run(gh({"created_at": iso(NOW - timedelta(days=30))}))
        self.assertEqual(r.failures, [])

    def test_missing_created_at_rejected(self):
        for bad in (None, "", "not a date", "2026-11-01T00:00:00"):
            with self.subTest(bad=bad):
                r = run(gh({"created_at": bad}))
                self.assertEqual(r.failures, [MSG_NEW])
        kw = repo()
        del kw["created_at"]
        g = gh()
        g.routes[f"GET {R}"] = kw
        self.assertEqual(run(g).failures, [MSG_NEW])

    def test_rot_mode_ignores_repo_age(self):
        r = run(gh({"created_at": iso(NOW - timedelta(days=1))}), "rot")
        self.assertEqual((r.failures, r.rot), ([], "ok"))
        r = run(gh({"created_at": None}), "rot")
        self.assertEqual((r.failures, r.rot), ([], "ok"))

    def test_archived_fails(self):
        r = run(gh({"archived": True}))
        self.assertEqual(len(r.failures), 1)
        self.assertIn("archived", r.failures[0])

    def test_fork_without_commits_fails(self):
        fork = {"fork": True, "parent": {"full_name": "bob/orig", "default_branch": "dev"}}
        cmp_ = "GET /repos/bob/orig/compare/dev...alice:main"
        for extra in ({}, {cmp_: {"ahead_by": 0}}):
            r = run(gh(fork, extra=extra))
            self.assertEqual(r.failures, ["Forks need their own commits to be listed."])

    def test_fork_ahead_passes(self):
        fork = {"fork": True, "parent": {"full_name": "bob/orig", "default_branch": "dev"}}
        r = run(gh(fork, extra={"GET /repos/bob/orig/compare/dev...alice:main": {"ahead_by": 3}}))
        self.assertEqual(r.failures, [])

    def test_license_missing_or_not_osi(self):
        for lic in ({"spdx_id": "NONE"}, {"spdx_id": "LicenseRef-custom"}):
            r = run(gh({"license": lic}))
            self.assertEqual(
                r.failures,
                ["We need an open-source license GitHub can recognise (like MIT or Apache-2.0)."],
                lic,
            )

    def test_null_or_noassertion_with_nothing_found(self):
        for lic in (None, {"spdx_id": "NOASSERTION"}):
            self.assertIn("We couldn't find an open-source license.", run(gh({"license": lic})).failures[0])

    def test_noassertion_falls_back_to_license_endpoint(self):
        g = gh({"license": {"spdx_id": "NOASSERTION"}}, extra={f"GET {R}/license": {"license": {"spdx_id": "Apache-2.0"}}})
        self.assertEqual(run(g).failures, [])

    def test_readme_missing_and_short(self):
        self.assertEqual(run(gh(readme=None)).failures, ["Add a README so people know what this is."])
        r = run(gh(readme="# Title\nshort"))
        self.assertEqual(
            r.failures,
            ["Your README needs a bit more detail (at least a short paragraph about what it does)."],
        )

    def test_readme_noise_not_counted(self):
        noise = (
            "# Heading\n![badge](http://x/y.svg)\n<div align='center'></div>\n"
            "```\n" + "code " * 100 + "\n```\n[link](http://very-long-url.example.com/" + "a" * 400 + ")\n"
        )
        self.assertEqual(len(run(gh(readme=noise)).failures), 1)
        self.assertEqual(run(gh(readme=noise + "x" * 300)).failures, [])

    def test_readme_exactly_300(self):
        self.assertEqual(run(gh(readme="a" * 300)).failures, [])
        self.assertEqual(len(run(gh(readme="a" * 299)).failures), 1)

    def test_description_empty(self):
        for d in (None, "   "):
            r = run(gh({"description": d}))
            self.assertEqual(len(r.failures), 1)
            self.assertIn("description", r.failures[0])

    def test_extras_counts_two_needed(self):
        none = gh(topics=(), contributing=False, coc=False)
        r = run(none)
        self.assertEqual(len(r.failures), 1)
        self.assertIn("CONTRIBUTING", r.failures[0])
        # one extra is not enough
        self.assertEqual(len(run(gh(topics=(), coc=False)).failures), 1)
        # two is
        self.assertEqual(run(gh(topics=(), coc=False, contributing=True, extra={f"GET {R}/releases?per_page=1": [{}]})).failures, [])

    def test_extras_each_kind(self):
        base = dict(topics=(), contributing=False, coc=False)
        cases = {
            "issue_template": {f"GET {R}/community/profile": {"files": {"issue_template": {}, "pull_request_template": None, "contributing": None, "code_of_conduct": None}}},
            "security": {f"GET {R}/contents/docs/SECURITY.md": {"name": "SECURITY.md"}},
            "workflows": {f"GET {R}/contents/.github/workflows": [{"name": "ci.yml"}]},
            "releases": {f"GET {R}/releases?per_page=1": [{"id": 1}]},
        }
        for name, extra in cases.items():
            r = run(gh(topics=("x",), **{k: v for k, v in base.items() if k != "topics"}, extra=extra))
            self.assertEqual(r.failures, [], name)
            r = run(gh(**base, extra=extra))
            self.assertEqual(len(r.failures), 1, name)

    def test_extras_short_circuit(self):
        g = gh()  # topics + contributing already = 2
        run(g)
        paths = [c[1] for c in g.calls]
        self.assertNotIn(f"{R}/releases?per_page=1", paths)
        self.assertFalse([p for p in paths if "contents" in p])

    def test_security_stops_at_first_hit(self):
        g = gh(topics=(), contributing=False, coc=False,
               extra={f"GET {R}/contents/SECURITY.md": {"x": 1}, f"GET {R}/contents/.github/workflows": [1]})
        run(g)
        paths = [c[1] for c in g.calls]
        self.assertNotIn(f"{R}/contents/.github/SECURITY.md", paths)

    def test_deprecated_marker_in_description_fails_submit(self):
        r = run(gh({"description": "Deprecated, use other"}))
        self.assertEqual(len(r.failures), 1)
        self.assertIn("deprecated", r.failures[0].lower())

    def test_deprecated_marker_in_readme_first_500(self):
        self.assertEqual(len(run(gh(readme="This project is no longer maintained. " + README)).failures), 1)
        self.assertEqual(run(gh(readme=README + " " * 600 + "deprecated")).failures, [])

    def test_collects_all_failures(self):
        r = run(gh({"archived": True, "license": None, "description": ""}, readme=None))
        self.assertGreaterEqual(len(r.failures), 4)
        self.assertEqual(r.rot, "ok")

    def test_redirect_sets_canonical(self):
        g = gh(extra={"GET /repos/old/name": Redirect("/repositories/99")})
        g.routes["GET /repositories/99"] = g.routes.pop(f"GET {R}")
        g.routes["GET /repos/alice/tool/readme"] = b64(README)
        r = run(g, full="old/name")
        self.assertEqual(r.canonical, "alice/tool")
        self.assertEqual(r.failures, [])
        self.assertTrue(all(c[1].startswith("/repos/alice/tool") or c[1] in ("/repos/old/name", "/repositories/99") for c in g.calls))
        self.assertIn(("GET", f"{R}/readme", None), g.calls)

    def test_api_error_raises(self):
        def boom(_):
            raise GitHubError(500, "boom")

        with self.assertRaises(GitHubError):
            run(gh(extra={f"GET {R}/readme": boom}))
        with self.assertRaises(GitHubError):
            run(gh(topics=(), contributing=False, coc=False, extra={f"GET {R}/releases?per_page=1": boom}))

    def test_redos_bracket_readme_is_fast(self):
        for text in ("[" * 100000, "![" * 50000, "<" * 100000):
            g = gh(readme=text)
            t = time.monotonic()
            run(g)
            # Generous for slow CI runners; the quadratic version took 18s or more.
            self.assertLess(time.monotonic() - t, 5, text[:3])

    def test_archived_word_in_text_is_not_deprecated(self):
        self.assertEqual(run(gh({"description": "Supports archived chats"})).failures, [])
        self.assertEqual(run(gh(readme="archived " + README), "rot").rot, "ok")

    def test_deprecated_message_names_the_word(self):
        r = run(gh({"description": "Deprecated, use other"}))
        self.assertEqual(len(r.failures), 1)
        self.assertIn('"Deprecated"', r.failures[0])

    def test_readme_encoding_none_uses_size(self):
        big = {"encoding": "none", "content": "", "size": 2_000_000}
        self.assertEqual(run(gh(extra={f"GET {R}/readme": big})).failures, [])
        small = {"encoding": "none", "content": "", "size": 100}
        self.assertEqual(run(gh(extra={f"GET {R}/readme": small})).failures, [MSG_SHORT])

    def test_bad_base64_is_undecided(self):
        bad = {"encoding": "base64", "content": "abcde"}
        for mode in ("submit", "rot"):
            with self.assertRaises(Undecided):
                run(gh(extra={f"GET {R}/readme": bad}), mode)

    def test_null_pushed_at(self):
        r = run(gh({"pushed_at": None}))
        self.assertEqual(len(r.failures), 1)
        self.assertIn("push", r.failures[0])
        with self.assertRaises(Undecided):
            run(gh({"pushed_at": None}), "rot")

    def test_403_on_readme_and_500_on_license_raise(self):
        def e403(_):
            raise GitHubError(403, "forbidden")

        def e500(_):
            raise GitHubError(500, "boom")

        for mode in ("submit", "rot"):
            with self.assertRaises(GitHubError):
                run(gh(extra={f"GET {R}/readme": e403}), mode)
            with self.assertRaises(GitHubError):
                run(gh({"license": {"spdx_id": "NOASSERTION"}}, extra={f"GET {R}/license": e500}), mode)


class RotTest(unittest.TestCase):
    def test_ok(self):
        r = run(gh(), "rot")
        self.assertEqual((r.rot, r.failures), ("ok", []))

    def test_rot_mode_warns_on_365_days(self):
        at = lambda d: gh({"pushed_at": iso(NOW - d)})
        self.assertEqual(run(at(timedelta(days=365)), "rot").rot, "ok")
        self.assertEqual(run(at(timedelta(days=365, seconds=1)), "rot").rot, "warn")

    def test_rot_warns_archived_and_deprecated(self):
        self.assertEqual(run(gh({"archived": True}), "rot").rot, "warn")
        self.assertEqual(run(gh({"description": "unmaintained"}), "rot").rot, "warn")

    def test_rot_mode_removes_on_license_gone(self):
        self.assertEqual(run(gh({"license": None}), "rot").rot, "remove")

    def test_rot_removes_on_readme(self):
        self.assertEqual(run(gh(readme=None), "rot").rot, "remove")
        self.assertEqual(run(gh(readme="tiny"), "rot").rot, "remove")

    def test_rot_remove_beats_warn(self):
        self.assertEqual(run(gh({"license": None, "archived": True}), "rot").rot, "remove")

    def test_rot_gone_repo(self):
        r = run(FakeGitHub({}), "rot")
        self.assertEqual((r.exists, r.rot), (False, "remove"))

    def test_rot_skips_entry_rules(self):
        g = gh({"fork": True, "parent": {"full_name": "b/o", "default_branch": "m"}, "description": ""},
               topics=(), contributing=False, coc=False)
        r = run(g, "rot")
        self.assertEqual(r.rot, "ok")
        paths = [c[1] for c in g.calls]
        self.assertFalse([p for p in paths if "compare" in p or "community" in p or "contents" in p])

    def test_rot_api_error_raises(self):
        def boom(_):
            raise GitHubError(500, "boom")

        with self.assertRaises(GitHubError):
            run(gh(extra={f"GET {R}/readme": boom}), "rot")


if __name__ == "__main__":
    unittest.main()
