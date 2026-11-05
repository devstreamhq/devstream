import unittest


class Smoke(unittest.TestCase):
    def test_import(self):
        import scripts.devstream  # noqa: F401


if __name__ == "__main__":
    unittest.main()
