import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("setup_github", ROOT / "scripts/admin/setup_github.py")
sg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sg)
field_options, label_plan = sg.field_options, sg.label_plan

WANT = {"bug": ("d73a4a", "Something's broken"), "report": ("d93f0b", "Entry reported")}


class LabelPlanTest(unittest.TestCase):
    def test_creates_missing_and_updates_changed(self):
        have = {"bug": ("D73A4A", "old words")}
        create, update, delete, kept = label_plan(have, WANT, in_use=set())
        self.assertEqual(create, ["report"])
        self.assertEqual(update, ["bug"])
        self.assertEqual((delete, kept), ([], []))

    def test_colour_case_is_not_a_change(self):
        have = {"bug": ("D73A4A", "Something's broken"), "report": ("d93f0b", "Entry reported")}
        self.assertEqual(label_plan(have, WANT, in_use=set()), ([], [], [], []))

    def test_only_the_four_github_defaults_are_deleted(self):
        have = dict(WANT, wontfix=("ffffff", ""), duplicate=("cfd3d7", ""))
        _, _, delete, kept = label_plan(have, WANT, in_use={"duplicate"})
        self.assertEqual(delete, ["wontfix"])
        self.assertEqual(kept, ["duplicate"])

    def test_unknown_labels_are_never_deleted(self):
        have = dict(WANT, wip=("ededed", ""), javascript=("168700", ""))
        self.assertEqual(label_plan(have, WANT, in_use=set()), ([], [], [], []))

class FieldOptionsTest(unittest.TestCase):
    HAVE = [
        {"id": 1, "name": "Urgent", "color": "pink", "priority": 1},
        {"id": 2, "name": "High", "color": "red", "priority": 2},
        {"id": 3, "name": "Low", "color": "green", "priority": 4},
    ]

    def test_keeps_ids_by_name_and_drops_the_rest(self):
        got = field_options(self.HAVE, [("High", "red"), ("Medium", "yellow"), ("Low", "green")])
        self.assertEqual(got, [
            {"id": 2, "name": "High", "color": "red", "priority": 1},
            {"name": "Medium", "color": "yellow", "priority": 2},
            {"id": 3, "name": "Low", "color": "green", "priority": 3},
        ])

    def test_alias_renames_an_option_in_place(self):
        got = field_options(self.HAVE, [("L", "red")], alias={"L": "High"})
        self.assertEqual(got, [{"id": 2, "name": "L", "color": "red", "priority": 1}])


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(sg.skipped.clear)
        sg.skipped.clear()

    def test_clean_run_exits_0(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sg.summary(), 0)

    def test_a_skipped_step_exits_2_and_says_so(self):
        sg.skipped.append("org ruleset: HTTP 403")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(sg.summary(), 2)
        self.assertIn("NOT DONE", out.getvalue())
        self.assertIn("org ruleset: HTTP 403", out.getvalue())


class OptionalTest(unittest.TestCase):
    def setUp(self):
        for x in (sg.skipped, sg.notes):
            x.clear()
            self.addCleanup(x.clear)

    def run_failing(self, message):
        def fail():
            raise sg.GhError(message)
        with contextlib.redirect_stdout(io.StringIO()):
            sg.optional("org ruleset", fail)

    def test_a_plan_gate_is_a_note_not_a_skip(self):
        self.run_failing("gh: Upgrade to GitHub Team to enable this feature. (HTTP 403)")
        self.assertEqual(sg.skipped, [])
        self.assertEqual(len(sg.notes), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sg.summary(), 0)

    def test_any_other_403_is_still_a_skip(self):
        self.run_failing("gh: Resource not accessible by integration (HTTP 403)")
        self.assertEqual(len(sg.skipped), 1)
        self.assertEqual(sg.notes, [])


if __name__ == "__main__":
    unittest.main()
