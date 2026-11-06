import json
import tempfile
import unittest
from pathlib import Path

from scripts.devstream import entries as E
from scripts.validate_data import validate


def make(repo="foo/bar", **kw):
    e = {
        "repo": repo,
        "repo_id": 1,
        "category": "testing",
        "description": "A thing that does a thing.",
        "submitted_by": "someone",
        "submitted_at": "2026-11-05T10:00:00Z",
        "issue": 1,
        "status": "live",
        "warned_at": None,
        "superseded_by": None,
        "added_by_seed": False,
    }
    e.update(kw)
    return e


class EntriesTest(unittest.TestCase):
    def test_normalize_repo_variants(self):
        for s in ("https://github.com/Foo/Bar.git/", "foo/bar", "FOO/BAR/"):
            self.assertEqual(E.normalize_repo(s), "foo/bar")

    def test_save_sorts_case_insensitive(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "e.json")
            E.save([make("b/x"), make("A/y"), make("c/z")], p)
            self.assertEqual([e["repo"] for e in E.load(p)], ["A/y", "b/x", "c/z"])
            text = Path(p).read_text(encoding="utf-8")
            self.assertTrue(text.endswith("}\n"))
            self.assertEqual(validate(json.loads(text)["entries"]), [])

    def test_find_duplicate_ignores_case(self):
        es = [make("Foo/Bar")]
        self.assertIs(E.find(es, "https://github.com/foo/BAR.git"), es[0])
        self.assertIsNone(E.find(es, "foo/baz"))
        self.assertTrue(any("duplicate" in m for m in validate([make("Foo/Bar"), make("foo/bar")])))

    def test_validate_rejects_unknown_category(self):
        self.assertTrue(validate([make(category="nope")]))

    def test_validate_rejects_unsorted(self):
        errs = validate([make("b/x"), make("a/y")])
        self.assertTrue(any("sorted" in m for m in errs))

    def test_validate_field_checks(self):
        self.assertEqual(validate([]), [])
        self.assertTrue(validate([make(status="bogus")]))
        self.assertTrue(validate([make("bad repo")]))
        self.assertTrue(validate([make("alice/tool\n")]))
        self.assertTrue(validate([make(submitted_at="2026-11-05")]))
        self.assertTrue(validate([make(issue=True)]))
        e = make()
        del e["description"]
        self.assertTrue(validate([e]))

    def test_validate_requires_a_positive_repo_id(self):
        e = make()
        del e["repo_id"]
        self.assertTrue(any("missing repo_id" in m for m in validate([e])))
        for bad in (None, 0, -3, True, "123", 1.5):
            self.assertTrue(validate([make(repo_id=bad)]), bad)

    def test_validate_rejects_description_the_filter_would_change(self):
        self.assertEqual(validate([make()]), [])
        for bad in ("<b>Bold</b> claims here.", "No closing period", "Visit spam.example today.", "x."):
            errors = validate([make(description=bad)])
            self.assertTrue(any("description" in e for e in errors), bad)

    def test_shipped_data_passes(self):
        doc = json.loads((Path(E.DEFAULT_PATH)).read_text(encoding="utf-8"))
        self.assertEqual(validate(doc["entries"]), [])


if __name__ == "__main__":
    unittest.main()
