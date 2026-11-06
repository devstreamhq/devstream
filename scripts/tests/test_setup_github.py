import importlib.util
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

    def test_extras_in_use_are_kept(self):
        have = dict(WANT, wontfix=("ffffff", ""), javascript=("168700", ""))
        _, _, delete, kept = label_plan(have, WANT, in_use={"javascript"})
        self.assertEqual(delete, ["wontfix"])
        self.assertEqual(kept, ["javascript"])


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


if __name__ == "__main__":
    unittest.main()
