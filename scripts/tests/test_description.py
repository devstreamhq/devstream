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
    def test_rejects_mentions(self):
        for t in ("Thanks to @user for the idea", "Thanks to \uff20user for the idea", "Built with @org/team help"):
            self.assertEqual(bad(t), "Descriptions can't mention people or teams.")

    def test_passes_plain_text(self):
        self.assertEqual(ok("A fast file watcher for Node.js projects."), "A fast file watcher for Node.js projects.")

    def test_length_bounds(self):
        bad("too short")
        bad("x" * 141)
        ok("y" * 139)

    def test_length_applies_after_cleanup(self):
        bad("   short   ")
        ok("  " + "word " * 20 + "  ")

    def test_trailing_punctuation_cannot_shrink_below_minimum(self):
        bad("Toolkit!!!")
        bad("Toolkit?!")
        bad("Tool.......")

    def test_output_is_a_fixed_point(self):
        # validate() re-runs the filter on stored text, so cleaning must be idempotent.
        inputs = [
            "Toolkit!!!", "Toolkit?!", "Toolkit!!!!", "Small tool?!", "Nice tool!!!", "Great CLI tool?!?!",
            "Tool.......", "Ends with a comma,", "Ends with colons::", "Semicolon end;", "Just right.",
            "  lots   of    spaces   here  ", "A fast file watcher for Node.js projects.",
            "Fast \U0001f680\U0001f680 tool for builds", "Love it \u2764\ufe0f\u2764\ufe0f really a lot",
            "Zero\u200bwidth tool for people", "\uff26ullwidth letters tool here", "x" * 138 + "!!",
            "y" * 139, "z" * 140, "w" * 139 + ".", "v" * 141, "short", "abcdefghi!", "abcdefghij!",
            "Mixed ending?.!", "Tabs\tand\nnewlines in it", "Dots... in the middle... end...",
            "Questions? Yes, answered here?", "Ending with spaces and bang !  ",
        ]
        self.assertGreaterEqual(len(inputs), 30)
        for x in inputs:
            f, reason = clean(x)
            if reason is None:
                self.assertEqual(clean(f), (f, None), repr(x))

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

    def test_mixed_script_words_rejected(self):
        for t in (
            "Best \u0441\u0430sino around for you",
            "Visit ex\u0430mple.com now ok",
            "Visit example.c\u043em now ok",
        ):
            self.assertIn("alphabet", bad(t), t)

    def test_single_script_words_allowed(self):
        ok("\u0418\u043d\u0441\u0442\u0440\u0443\u043c\u0435\u043d\u0442 \u0434\u043b\u044f \u0440\u0430\u0431\u043e\u0442\u044b")
        ok("\u0394\u03b5\u03bb\u03c4\u03b1 tool for the web")

    def test_final_length_includes_period(self):
        bad("y" * 140)
        self.assertEqual(len(ok("y" * 139)), 140)
        self.assertEqual(len(ok("y" * 139 + ".")), 140)

    def test_squashed_match_is_anchored(self):
        for t in (
            "Specialists tooling for teams",
            "Alphabet online docs helper",
            "Essex chat bot for devs",
            "Fake idea board app",
            "Homework helper for students",
            "Spy appender for tests",
            "Hack accounting reports",
            "Pump and dumper for logs",
        ):
            ok(t)
        bad("Best c.a.s.i.n.o around for you")
        bad("Get v-i-a-g-r-a here today ok")

    def test_real_tlds_and_ips_rejected(self):
        for t in (
            "Run spam.sh/install to start",
            "Visit evil.rs for the thing",
            "Hosted at foo.cs/x for you",
            "Server on 192.168.1.1 right now",
            "Fetch 1.2.3.4/x for the thing",
            "Node.js:8080 is the place",
        ):
            self.assertIn("link", bad(t), t)

    def test_js_names_allowed_with_punctuation(self):
        ok("Is this Node.js? Yes, and Vue.js too.")
        ok("Node.js: a small helper for Vue.js apps")

    def test_invisible_characters_stripped(self):
        for ch in ("\u00ad", "\u2062", "\u034f", "\u180e", "\ufe0f"):
            bad(f"Free po{ch}rn tool for the web")

    def test_emoji_run_with_variation_selector(self):
        bad("Stars \u2728\ufe0f\u2728\ufe0f\u2728\ufe0f everywhere here")
        ok("Stars \u2728\ufe0f\u2728\ufe0f everywhere here")

    def test_removed_terms_allowed(self):
        ok("Work from home setup scripts for devs")
        ok("A bulk sms sender for alert systems")
        ok("Model passive income streams in code")


if __name__ == "__main__":
    unittest.main()
