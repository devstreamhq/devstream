import re
import unittest
from pathlib import Path

from scripts.devstream.entries import CATEGORIES
from scripts.devstream.issue_form import FormError, parse_submission

ROOT = Path(__file__).resolve().parents[2]
YAML = ROOT / ".github" / "ISSUE_TEMPLATE" / "submit-repo.yml"


def body(repo="https://github.com/o/r", cat="CLI & Terminal", desc="text", boxes="XX"):
    a, b = boxes
    return (
        f"### Repository\n\n{repo}\n\n### Category\n\n{cat}\n\n"
        f"### Description\n\n{desc}\n\n### Checklist\n\n"
        f"- [{a}] I read the quality bar\n- [{b}] This repo is open source\n"
    )


class IssueFormTest(unittest.TestCase):
    def test_parses_valid_body(self):
        self.assertEqual(
            parse_submission(body()),
            {"repository": "o/r", "category": "cli-terminal", "description": "text"},
        )

    def test_missing_field_raises_formerror(self):
        with self.assertRaises(FormError):
            parse_submission("### Repository\n\no/r\n")
        with self.assertRaises(FormError):
            parse_submission("")

    def test_no_response_raises(self):
        with self.assertRaises(FormError):
            parse_submission(body(desc="_No response_"))

    def test_category_display_name_to_slug(self):
        for slug, name in CATEGORIES.items():
            self.assertEqual(parse_submission(body(cat=name))["category"], slug)
        with self.assertRaises(FormError):
            parse_submission(body(cat="Nonsense"))

    def test_repository_variants(self):
        for r in (
            "https://github.com/O/R",
            "http://github.com/o/r",
            "https://www.github.com/o/r",
            "github.com/o/r",
            "o/r",
            "https://github.com/o/r.git",
            "https://github.com/o/r/",
            "https://github.com/o/r/tree/main/src",
            "https://github.com/o/r/blob/main/x.md",
            "https://github.com/o/r#readme",
            "https://github.com/o/r?tab=x",
        ):
            self.assertEqual(parse_submission(body(repo=r))["repository"], "o/r", r)

    def test_bad_repository_rejected(self):
        for r in (
            "https://gitlab.com/o/r",
            "https://github.com.evil.io/o/r",
            "gitlab.com/o/r",
            "https://github.com/o",
            "https://github.com/o/r/issues",
            "javascript:alert(1)",
            "o/r r",
            "-/..",
            "a" * 40 + "/r",
        ):
            with self.assertRaises(FormError, msg=r) as cm:
                parse_submission(body(repo=r))
            self.assertEqual(
                str(cm.exception), "That doesn't look like a GitHub repo link."
            )

    def test_checkboxes_required(self):
        for boxes in ("X ", " X", "  "):
            with self.assertRaises(FormError):
                parse_submission(body(boxes=boxes))
        parse_submission(body(boxes="xx"))

    def test_extra_and_duplicate_headings(self):
        b = body() + "\n### Other\n\nhi\n\n### Description\n\nsecond\n"
        self.assertEqual(parse_submission(b)["description"], "text")

    def test_crlf(self):
        self.assertEqual(parse_submission(body().replace("\n", "\r\n"))["repository"], "o/r")

    def test_body_cap(self):
        with self.assertRaises(FormError):
            parse_submission(body(desc="x" * 20001))


class TemplateTest(unittest.TestCase):
    def test_dropdown_options_match_categories(self):
        text = YAML.read_text(encoding="utf-8")
        block = re.search(r"^ +options:\n((?: +- .+\n)+)", text, re.M).group(1)
        opts = re.findall(r"^ +- (.+)$", block, re.M)
        self.assertEqual(opts, list(CATEGORIES.values()))

    def test_labels_match_parser_headings(self):
        labels = re.findall(r"^\s+label: (.+)$", YAML.read_text(encoding="utf-8"), re.M)
        for want in ("Repository", "Category", "Description", "Checklist"):
            self.assertIn(want, labels)


if __name__ == "__main__":
    unittest.main()
