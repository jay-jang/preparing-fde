from dataclasses import FrozenInstanceError
from fractions import Fraction
import inspect
import unittest

from fde.ann import (EF_SEARCH_DEFAULT, EXACT_CAP, MAX_SCAN_TUPLES_DEFAULT,
                     MAX_SHORTFALL_DEFAULT, plan_search, shortfall)
from fde.retrieval import retrieve

TOTAL = 10_000_000  # 6-1 assumption: ten factories of 1,000,000 chunks


class FilteredAnnTests(unittest.TestCase):
    def test_defaults_follow_pgvector_and_the_exact_path(self):
        self.assertEqual(EF_SEARCH_DEFAULT, 40)
        self.assertEqual(MAX_SCAN_TUPLES_DEFAULT, 20_000)
        self.assertEqual(MAX_SHORTFALL_DEFAULT, Fraction(1, 100))
        self.assertEqual(EXACT_CAP, inspect.signature(retrieve).parameters['cap'].default)

    def test_pgvector_readme_example_ten_percent_gives_four(self):
        plan = plan_search(total=TOTAL, allowed=1_000_000, k=5)
        self.assertEqual(plan.selectivity, Fraction(1, 10))
        self.assertEqual(plan.expected_matches, 4)
        self.assertEqual(round(float(plan.shortfall), 4), 0.629)
        self.assertEqual(plan.expected_tuples, 50)
        self.assertEqual((plan.strategy, plan.iterative_scan), ('iterative', 'relaxed_order'))

    def test_small_allowed_set_uses_exact_path(self):
        for allowed in (0, 4_000, 5_000):
            with self.subTest(allowed=allowed):
                plan = plan_search(total=TOTAL, allowed=allowed, k=5)
                self.assertEqual(plan.strategy, 'exact')
                self.assertEqual(plan.expected_matches, allowed)
                self.assertIsNone(plan.expected_tuples)
                self.assertIsNone(plan.shortfall)
                self.assertEqual(plan.settings(), {})
        self.assertNotEqual(plan_search(total=TOTAL, allowed=5_001, k=5).strategy, 'exact')

    def test_headquarters_role_needs_iterative_scan(self):
        plan = plan_search(total=TOTAL, allowed=40_000, k=5)
        self.assertEqual(plan.selectivity, Fraction(1, 250))
        self.assertEqual(plan.expected_matches, Fraction(4, 25))
        self.assertEqual(float(plan.expected_matches), 0.16)
        self.assertEqual(plan.expected_tuples, 1_250)
        self.assertEqual(plan.strategy, 'iterative')
        self.assertEqual(plan.settings(), {'hnsw.ef_search': 40, 'hnsw.iterative_scan': 'relaxed_order',
                                           'hnsw.max_scan_tuples': 20_000})

    def test_broad_filter_post_filter_is_enough(self):
        plan = plan_search(total=TOTAL, allowed=5_000_000, k=5)
        self.assertEqual(plan.expected_matches, 20)
        # P(X < 5), X ~ Binomial(40, 1/2) = (1 + 40 + 780 + 9,880 + 91,390) / 2**40
        self.assertEqual(plan.shortfall, Fraction(102_091, 2**40))
        self.assertEqual((plan.strategy, plan.iterative_scan), ('post_filter', 'off'))
        self.assertEqual(plan.settings(), {'hnsw.ef_search': 40, 'hnsw.iterative_scan': 'off'})

    def test_mean_equal_to_k_is_not_enough(self):
        # Expected 40 x 1/8 = 5 = k, yet the first scan misses k in about 43% of queries.
        plan = plan_search(total=8_000_000, allowed=1_000_000, k=5)
        self.assertEqual(plan.expected_matches, 5)
        self.assertEqual(round(float(plan.shortfall), 4), 0.4287)
        self.assertEqual((plan.strategy, plan.iterative_scan), ('iterative', 'relaxed_order'))
        loose = plan_search(total=8_000_000, allowed=1_000_000, k=5, max_shortfall=Fraction(1, 2))
        self.assertEqual(loose.strategy, 'post_filter')

    def test_shortfall_edges(self):
        self.assertEqual(shortfall(40, 0, 10, 5), 1)
        self.assertEqual(shortfall(40, 10, 10, 5), 0)
        self.assertEqual(shortfall(40, 1, 2, 41), 1)
        self.assertEqual(shortfall(2, 1, 2, 2), Fraction(3, 4))
        with self.assertRaises(ValueError):
            shortfall(40, 11, 10, 5)

    def test_partition_key_filter_searches_one_partition(self):
        shared = plan_search(total=TOTAL, allowed=1_000_000, k=5)
        partition = plan_search(total=TOTAL, allowed=1_000_000, k=5, partition_rows=1_000_000)
        self.assertEqual(partition.strategy, 'partition')
        self.assertEqual(partition.index_rows, 1_000_000)
        self.assertEqual(partition.selectivity, 1)
        self.assertEqual(partition.expected_matches, 40)
        self.assertEqual(partition.iterative_scan, 'off')
        self.assertEqual(partition.shortfall, 0)
        self.assertGreater(partition.expected_matches, shared.expected_matches)
        inside = plan_search(total=TOTAL, allowed=40_000, k=5, partition_rows=1_000_000)
        self.assertEqual((inside.strategy, inside.iterative_scan), ('partition', 'relaxed_order'))
        self.assertEqual(inside.expected_tuples, 125)

    def test_unreachable_k_fails_instead_of_widening(self):
        with self.assertRaisesRegex(ValueError, 'never widen the ACL'):
            plan_search(total=100_000_000, allowed=20_000, k=5)
        plan = plan_search(total=100_000_000, allowed=20_000, k=5, max_scan_tuples=25_000)
        self.assertEqual(plan.expected_tuples, 25_000)
        self.assertEqual(plan.allowed, 20_000)
        self.assertEqual(plan_search(total=100_000_000, allowed=20_000, k=5,
                                     partition_rows=1_000_000).expected_tuples, 250)

    def test_selectivity_threshold_for_default_budget(self):
        # k / max_scan_tuples = 5 / 20,000: below this selectivity the default budget is exceeded.
        self.assertEqual(plan_search(total=20_000_000, allowed=5_001, k=5).strategy, 'iterative')
        with self.assertRaises(ValueError):
            plan_search(total=40_000_000, allowed=9_999, k=5)
        self.assertEqual(plan_search(total=40_000_000, allowed=10_000, k=5).expected_tuples, 20_000)

    def test_k_cannot_exceed_candidate_list(self):
        with self.assertRaisesRegex(ValueError, 'ef_search'):
            plan_search(total=TOTAL, allowed=40_000, k=41)
        plan = plan_search(total=TOTAL, allowed=40_000, k=41, ef_search=100)
        self.assertEqual(plan.expected_matches, Fraction(2, 5))

    def test_invalid_inputs_fail_closed(self):
        cases = ({'total': 0, 'allowed': 0, 'k': 5}, {'total': 10, 'allowed': 11, 'k': 5},
                 {'total': 10, 'allowed': -1, 'k': 5}, {'total': 10, 'allowed': 1, 'k': 0},
                 {'total': True, 'allowed': 1, 'k': 5}, {'total': 10.0, 'allowed': 1, 'k': 5},
                 {'total': TOTAL, 'allowed': 6_000, 'k': 5, 'ef_search': 1_001},
                 {'total': TOTAL, 'allowed': 6_000, 'k': 5, 'exact_cap': 5_001},
                 {'total': TOTAL, 'allowed': 6_000, 'k': 5, 'partition_rows': 5_999},
                 {'total': TOTAL, 'allowed': 6_000, 'k': 5, 'partition_rows': TOTAL + 1},
                 {'total': TOTAL, 'allowed': 6_000, 'k': 5, 'max_scan_tuples': 0},
                 {'total': TOTAL, 'allowed': 6_000, 'k': 5, 'max_shortfall': 0.01},
                 {'total': TOTAL, 'allowed': 6_000, 'k': 5, 'max_shortfall': Fraction(0)},
                 {'total': TOTAL, 'allowed': 6_000, 'k': 5, 'max_shortfall': Fraction(1)})
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    plan_search(**arguments)

    def test_plan_is_frozen(self):
        plan = plan_search(total=TOTAL, allowed=40_000, k=5)
        with self.assertRaises(FrozenInstanceError):
            plan.allowed = TOTAL


if __name__ == '__main__':
    unittest.main()
