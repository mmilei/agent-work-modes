"""The automated check for the benchmark task: `python -m unittest -q test_roman` must exit 0."""
import unittest

from roman import from_roman, to_roman


class Roman(unittest.TestCase):
    def test_round_trip(self):
        for n in range(1, 4000):
            self.assertEqual(from_roman(to_roman(n)), n)

    def test_known(self):
        for n, s in [(1, "I"), (4, "IV"), (9, "IX"), (14, "XIV"), (40, "XL"), (90, "XC"), (400, "CD"),
                     (1994, "MCMXCIV"), (2026, "MMXXVI"), (3999, "MMMCMXCIX")]:
            self.assertEqual(to_roman(n), s)
            self.assertEqual(from_roman(s), n)

    def test_rejects(self):
        for n in (0, -1, 4000, 2.5, "7", None):
            with self.assertRaises(ValueError):
                to_roman(n)
        for s in ("", "IIII", "VV", "IC", "MMMM", "XM", "iv", "ABC", 5, None):
            with self.assertRaises(ValueError):
                from_roman(s)


if __name__ == "__main__":
    unittest.main()
