import struct
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SVGS = sorted((ROOT / "assets").glob("*.svg")) + [ROOT / "site/public/favicon.svg"]
HEADERS = [ROOT / "assets/header-light.svg", ROOT / "assets/header-dark.svg"]
# GitHub renders README SVGs through <img>, which drops all of these.
FORBIDDEN = ("<script", "foreignObject", 'href="http', "@import")


class Assets(unittest.TestCase):
    def test_svgs_parse_and_are_self_contained(self):
        self.assertGreaterEqual(len(SVGS), 6)
        for path in SVGS:
            with self.subTest(path.name):
                ET.parse(path)
                text = path.read_text()
                for bad in FORBIDDEN:
                    self.assertNotIn(bad, text)

    def test_headers_are_small_and_respect_reduced_motion(self):
        for path in HEADERS:
            with self.subTest(path.name):
                self.assertLess(path.stat().st_size, 60 * 1024)
                self.assertIn("prefers-reduced-motion", path.read_text())

    def test_social_preview_is_1280_by_640(self):
        data = (ROOT / "assets/social-preview.png").read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(data[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">II", data[16:24]), (1280, 640))


if __name__ == "__main__":
    unittest.main()
