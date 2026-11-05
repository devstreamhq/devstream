import unittest

from scripts.devstream.description import clean_description as clean


def ok(text):
    out, reason = clean(text)
    assert reason is None, (text, reason)
    return out


def bad(text):
    out, reason = clean(text)
    assert out is None and reason, (text, out)
    return reason


class DescriptionTest(unittest.TestCase):
    def test_passes_plain_text(self):
        self.assertEqual(ok("A fast file watcher for Node.js projects."), "A fast file watcher for Node.js projects.")

    def test_length_bounds(self):
        bad("too short")
        bad("x" * 141)
        ok("y" * 140)

    def test_length_applies_after_cleanup(self):
        bad("   short   ")
        ok("  " + "word " * 20 + "  ")

    def test_urls_rejected(self):
        for t in (
            "See https://example.org for details",
            "Mirror at http://foo.bar/x for all",
            "Docs live at www.thing for you",
            "Visit example.com for more info",
            "Hosted at foo.io/x right now",
            "Hosted at FOO.IO right now ok",
        ):
            self.assertIn("link", bad(t), t)

    def test_tech_names_allowed(self):
        for t in (
            "A tiny helper for Node.js and Vue.js apps.",
            "Supports v1.2 and 3.14 releases of the tool.",
            "Works like e.g. a linter, i.e. very fast.",
        ):
            ok(t)

    def test_forbidden_characters(self):
        for c in "<>[]()`*_#":
            bad(f"A tool with a {c} in the text here")

    def test_emoji_runs(self):
        ok("A neat tool \U0001F600\U0001F600 for people")
        bad("A neat tool \U0001F600\U0001F600\U0001F600 for people")
        ok("Fun \U0001F600 tool \U0001F600 with \U0001F600 spacing")
        bad("Stars ✨✨✨ everywhere in here")

    def test_all_caps_word(self):
        bad("This is the AMAZING tool around")
        ok("This is a HTTP and JSON tool.")
        ok("This is a SMALL tool for me.")

    def test_blocklist_whole_word_case_insensitive(self):
        bad("Play at the Casino right now")
        bad("Free PORN tool for the web")
        ok("Free pornucopia tool for the web.")

    def test_nfkc_and_zero_width_stripped_before_blocklist(self):
        bad("Best c​asino around for you")
        bad("Best ｃａｓｉｎｏ around for you")
        bad("Best ca⁠si﻿no around for you")

    def test_squashed_form_catches_spacing_tricks(self):
        bad("Best c.a.s.i.n.o around for you")
        bad("Best c a s i n o around for you")

    def test_squashed_ignores_short_terms(self):
        ok("Free p o r n tool for the web.")

    def test_trims_and_collapses_whitespace(self):
        self.assertEqual(ok("  A   tool\n\tthat   works  well  "), "A tool that works well.")

    def test_ends_with_period_added(self):
        self.assertEqual(ok("A tool that works well"), "A tool that works well.")
        self.assertEqual(ok("A tool that works well."), "A tool that works well.")
        self.assertEqual(ok("A tool that works well!"), "A tool that works well.")


if __name__ == "__main__":
    unittest.main()
