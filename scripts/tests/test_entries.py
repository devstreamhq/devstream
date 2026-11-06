import json
import os
import tempfile
import zlib
import unittest
from pathlib import Path

from unittest import mock

from scripts.devstream import entries as E
from scripts.validate_data import main, validate


def make(repo="foo/bar", **kw):
    e = {
        "repo": repo,
        "repo_id": zlib.crc32(repo.encode()),
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
        self.assertEqual(validate([make(status="bogus")]), ["entry 0 (foo/bar): unknown status 'bogus'"])
        self.assertEqual(validate([make("bad repo")]), ["entry 0 (bad repo): bad repo format"])
        self.assertEqual(validate([make("alice/tool\n")]), ["entry 0 (alice/tool\n): bad repo format"])
        self.assertEqual(validate([make(submitted_at="2026-11-05")]),
                         ["entry 0 (foo/bar): submitted_at must be ISO-8601 UTC (…Z)"])
        self.assertEqual(validate([make(issue=True)]), ["entry 0 (foo/bar): issue must be int"])
        self.assertEqual(validate([make(extra=1)]), ["entry 0 (foo/bar): unknown field extra"])
        e = make()
        del e["description"]
        self.assertEqual(validate([e]), ["entry 0 (foo/bar): missing description"])

    def test_every_status_is_valid_and_matches_the_schema(self):
        for s in E.STATUSES:
            self.assertEqual(validate([make(status=s)]), [], s)
        schema = json.loads((Path(E.DATA) / "schema.json").read_text(encoding="utf-8"))
        self.assertEqual(tuple(schema["$defs"]["entry"]["properties"]["status"]["enum"]), E.STATUSES)

    def test_save_leaves_the_old_file_when_the_write_fails(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "e.json")
            E.save([make("a/x")], p)
            before = Path(p).read_text(encoding="utf-8")
            with mock.patch("scripts.devstream.entries.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    E.save([make("b/y")], p)
            self.assertEqual(Path(p).read_text(encoding="utf-8"), before)
            self.assertEqual(os.listdir(d), ["e.json"])

    def test_validate_requires_a_positive_repo_id(self):
        e = make()
        del e["repo_id"]
        self.assertTrue(any("missing repo_id" in m for m in validate([e])))
        for bad in (None, 0, -3, True, "123", 1.5):
            self.assertTrue(validate([make(repo_id=bad)]), bad)

    def test_validate_rejects_duplicate_repo_id(self):
        errs = validate([make("a/x", repo_id=7), make("b/y", repo_id=7)])
        self.assertTrue(any("duplicate repo_id" in m for m in errs))
        self.assertEqual(validate([make("a/x", repo_id=7), make("b/y", repo_id=8)]), [])

    def test_validate_rejects_description_the_filter_would_change(self):
        self.assertEqual(validate([make()]), [])
        for bad in ("<b>Bold</b> claims here.", "No closing period", "Visit spam.example today.", "x."):
            errors = validate([make(description=bad)])
            self.assertTrue(any("description" in e for e in errors), bad)

    def test_cli_exit_codes_and_messages(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "e.json")
            cases = [
                ({"version": 1, "entries": [make()]}, 0, ""),
                ({"version": True, "entries": [make()]}, 1, "expected {version: 1, entries: [...]}"),
                ({"version": 1.0, "entries": [make()]}, 1, "expected {version: 1, entries: [...]}"),
                ({"version": 1, "entries": {}}, 1, "expected {version: 1, entries: [...]}"),
                ({"version": 1}, 1, "'entries'"),
                ({"version": 1, "entries": [make(status="bogus")]}, 1, "entry 0 (foo/bar): unknown status 'bogus'"),
            ]
            for doc, code, msg in cases:
                Path(p).write_text(json.dumps(doc), encoding="utf-8")
                with mock.patch("sys.stderr") as err:
                    self.assertEqual(main(["validate_data", p]), code, doc)
                self.assertIn(msg, "".join(c.args[0] for c in err.write.call_args_list))
            Path(p).write_text("{not json", encoding="utf-8")
            with mock.patch("sys.stderr"):
                self.assertEqual(main(["validate_data", p]), 1)

    def test_shipped_data_passes(self):
        doc = json.loads((Path(E.DEFAULT_PATH)).read_text(encoding="utf-8"))
        self.assertEqual(validate(doc["entries"]), [])


if __name__ == "__main__":
    unittest.main()
