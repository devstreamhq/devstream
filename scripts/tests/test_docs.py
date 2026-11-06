import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
PROSE = [README, ROOT / "CONTRIBUTING.md", ROOT / "GOVERNANCE.md", ROOT / "SUPPORT.md"]
# The house style guide's banned list: filler that reads as effort.
BANNED = [
    "seamless", "robust", "leverage", "empower", "unlock", "elevate", "revolutionary",
    "game-changing", "cutting-edge", "best-in-class", "delve", "tapestry", "testament",
    "realm", "navigate", "landscape", "in today's fast-paced world", "in an era of",
]
NOT_JUST = re.compile(r"\b(?:it|this|that)(?:'s|’s| is) not just\b", re.I)
LINK = re.compile(r"\]\(([^)\s]+)\)|(?:src|srcset|href)=\"([^\"]+)\"")


class Docs(unittest.TestCase):
    def test_readme_is_short(self):
        self.assertLess(len(README.read_text().splitlines()), 120)

    def test_no_banned_words(self):
        for path in PROSE:
            text = path.read_text().replace("’", "'")
            for word in BANNED:
                with self.subTest(path.name, word=word):
                    self.assertIsNone(re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text, re.I))

    def test_no_not_just_construction(self):
        for path in PROSE:
            with self.subTest(path.name):
                self.assertIsNone(NOT_JUST.search(path.read_text()))

    def test_readme_em_dashes_are_rare(self):
        self.assertLessEqual(README.read_text().count("—"), 3)

    def test_readme_relative_links_exist(self):
        targets = [a or b for a, b in LINK.findall(README.read_text())]
        relative = [t for t in targets if not re.match(r"[a-z]+:|#", t)]
        self.assertTrue(relative)
        for target in relative:
            with self.subTest(target):
                self.assertTrue((ROOT / target.split("#")[0]).exists())


if __name__ == "__main__":
    unittest.main()
