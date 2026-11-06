import unittest

from scripts.devstream.gh import GitHubError
from scripts.devstream.stats import API, list_dir
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
