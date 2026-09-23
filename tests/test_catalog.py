import unittest

from catalog import subsidy


class SubsidyTest(unittest.TestCase):
    def test_cap_applies(self):
        self.assertEqual(subsidy(8000, 0.5, 3000), 3000)


if __name__ == "__main__":
    unittest.main()
