import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
PROSE = [README] + [ROOT / p for p in (
    "CONTRIBUTING.md", "GOVERNANCE.md", "SUPPORT.md", "CHANGELOG.md", "SECURITY.md",
    ".github/pull_request_template.md",
)]
# The house style guide's banned list: filler that reads as effort.
BANNED = [
    "seamless", "robust", "leverage", "empower", "unlock", "elevate", "revolutionary",
    "game-changing", "cutting-edge", "best-in-class", "delve", "tapestry", "testament",
    "realm", "navigate", "landscape", "in today's fast-paced world", "in an era of",
]
# Not shipped yet, so no page may claim them.
UNSHIPPED = re.compile(r"\bsponsor", re.I)
NOT_JUST = re.compile(r"\b(?:it|this|that)(?:'s|’s| is) not just\b", re.I)
LINK = re.compile(r"\]\(([^)\s]+)\)|(?:src|srcset|href)=\"([^\"]+)\"")
COMMENT = re.compile(r"<!--.*?-->", re.S)


def anchors(path: Path) -> set[str]:
    """GitHub's heading slugs: lowercase, punctuation dropped, spaces to hyphens, -1 for repeats."""
    seen: dict[str, int] = {}
    out = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*$", path.read_text(), re.M):
        slug = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


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

    def test_em_dashes_are_rare(self):
        for path in PROSE:
            with self.subTest(path.name):
                self.assertLessEqual(path.read_text().count("—"), 3)

    def test_no_unshipped_claims(self):
        for path in PROSE:
            with self.subTest(path.name):
                self.assertIsNone(UNSHIPPED.search(COMMENT.sub("", path.read_text())))

    def test_readme_has_relative_links(self):
        self.assertTrue([t for t in self._targets(README) if not re.match(r"[a-z]+:", t)])

    def test_relative_links_and_anchors_exist(self):
        for path in PROSE:
            for target in self._targets(path):
                if re.match(r"[a-z]+:", target):
                    continue
                file, _, anchor = target.partition("#")
                dest = (path.parent / file) if file else path
                with self.subTest(path.name, target=target):
                    self.assertTrue(dest.exists())
                    if anchor:
                        self.assertIn(anchor, anchors(dest))

    @staticmethod
    def _targets(path: Path) -> list[str]:
        return [a or b for a, b in LINK.findall(COMMENT.sub("", path.read_text()))]


if __name__ == "__main__":
    unittest.main()
