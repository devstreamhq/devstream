import base64
import json
import unittest

from scripts.devstream.gh import GitHubError
from scripts.devstream.stats import API, delete_file, list_dir, read_json, update_json
from scripts.tests.fake_gh import FakeGitHub


class ListDir(unittest.TestCase):
    def test_keeps_files_only(self):
        gh = FakeGitHub({f"GET {API}/contents/d": [
            {"name": "a.json", "type": "file"}, {"name": "sub", "type": "dir"}, {"name": "l", "type": "symlink"}]})
        self.assertEqual(list_dir(gh, "d"), ["a.json"])

    def test_missing_directory_is_empty(self):
        self.assertEqual(list_dir(FakeGitHub({}), "d"), [])

    def test_non_list_raises(self):
        # A path that is a file comes back as an object, not a listing.
        gh = FakeGitHub({f"GET {API}/contents/d": {"name": "d", "type": "file", "content": ""}})
        with self.assertRaises(GitHubError):
            list_dir(gh, "d")


class ReadJson(unittest.TestCase):
    def test_file_over_1mb_is_read_through_the_blob(self):
        raw = base64.encodebytes(json.dumps({"a": 1}).encode()).decode()  # the blobs API wraps lines
        gh = FakeGitHub({
            f"GET {API}/contents/big.json": {"content": "", "encoding": "none", "sha": "s1"},
            f"GET {API}/git/blobs/s1": {"content": raw, "encoding": "base64", "sha": "s1"},
        })
        self.assertEqual(read_json(gh, "big.json"), ({"a": 1}, "s1"))

    def test_missing_blob_raises(self):
        gh = FakeGitHub({f"GET {API}/contents/big.json": {"content": "", "encoding": "none", "sha": "s1"}})
        with self.assertRaises(GitHubError):
            read_json(gh, "big.json")


class Compact(unittest.TestCase):
    def test_compact_write_has_no_indent(self):
        gh = FakeGitHub({f"PUT {API}/contents/c.json": {}})
        update_json(gh, "c.json", lambda _: {"a": {"b": 1}}, "m", compact=True)
        body = base64.b64decode(gh.calls[-1][2]["content"]).decode()
        self.assertEqual(body, '{"a":{"b":1}}\n')


class DeleteFile(unittest.TestCase):
    def test_deletes_with_sha_on_stats_branch(self):
        gh = FakeGitHub({f"GET {API}/contents/s/a.json": {"content": "", "sha": "s9"},
                         f"DELETE {API}/contents/s/a.json": None})
        delete_file(gh, "s/a.json", "chore: drop")
        body = gh.calls[-1][2]
        self.assertEqual((body["sha"], body["branch"], body["message"]), ("s9", "stats", "chore: drop"))
        self.assertIn("committer", body)

    def test_retries_once_on_409(self):
        n = []

        def conflict(_):
            n.append(1)
            if len(n) == 1:
                raise GitHubError(409, "conflict")
        gh = FakeGitHub({f"GET {API}/contents/a.json": {"content": "", "sha": "s9"},
                         f"DELETE {API}/contents/a.json": conflict})
        delete_file(gh, "a.json", "m")
        self.assertEqual(len(n), 2)

    def test_already_gone_is_fine(self):
        delete_file(FakeGitHub({}), "a.json", "m")
