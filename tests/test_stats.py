import unittest

from shipcheck.stats import mcnemar_exact, wilson_interval


class WilsonIntervalTest(unittest.TestCase):
    def test_known_value_half(self):
        lo, hi = wilson_interval(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=4)
        self.assertAlmostEqual(hi, 0.7634, places=4)

    def test_known_value_34_of_40(self):
        lo, hi = wilson_interval(34, 40)
        self.assertAlmostEqual(lo, 0.7091, places=3)
        self.assertAlmostEqual(hi, 0.9294, places=3)

    def test_zero_successes_starts_at_zero(self):
        lo, hi = wilson_interval(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 1.96**2 / (10 + 1.96**2), places=10)

    def test_all_successes_ends_at_one(self):
        lo, hi = wilson_interval(10, 10)
        self.assertEqual(hi, 1.0)
        self.assertAlmostEqual(lo, 10 / (10 + 1.96**2), places=10)

    def test_zero_cases_returns_the_whole_range(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))

    def test_rejects_impossible_counts(self):
        for k, n in ((5, 4), (-1, 4), (0, -1)):
            with self.assertRaises(ValueError):
                wilson_interval(k, n)

    def test_interval_narrows_as_the_sample_grows(self):
        small = wilson_interval(34, 40)
        large = wilson_interval(850, 1000)
        self.assertLess(large[1] - large[0], small[1] - small[0])


class McNemarExactTest(unittest.TestCase):
    def test_known_small_sample_values(self):
        self.assertAlmostEqual(mcnemar_exact(0, 5), 0.0625)
        self.assertAlmostEqual(mcnemar_exact(0, 6), 0.03125)
        self.assertAlmostEqual(mcnemar_exact(1, 9), 22 / 1024)
        self.assertAlmostEqual(mcnemar_exact(5, 1), 14 / 64)

    def test_symmetric_in_its_arguments(self):
        for b, c in ((0, 5), (1, 9), (3, 7), (5, 4)):
            self.assertEqual(mcnemar_exact(b, c), mcnemar_exact(c, b))

    def test_balanced_or_empty_disagreement_gives_one(self):
        self.assertEqual(mcnemar_exact(0, 0), 1.0)
        self.assertEqual(mcnemar_exact(2, 2), 1.0)
        self.assertEqual(mcnemar_exact(5, 4), 1.0)

    def test_rejects_negative_counts(self):
        with self.assertRaises(ValueError):
            mcnemar_exact(-1, 2)


if __name__ == "__main__":
    unittest.main()
