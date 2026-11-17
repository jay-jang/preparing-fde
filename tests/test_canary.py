from dataclasses import FrozenInstanceError
from fractions import Fraction
import math
from statistics import NormalDist
import unittest

from fde.canary import (Limit, Policy, Z95, decide, observations_needed, wilson, window_hours,
                        z_value)

# Proposal values for the running case, not measurements: 5% canary of 3,000 req/h,
# error ceiling 14.4 x 0.1% (deploy/alerts.yaml fast burn), promote below 1%.
ERROR = Limit(ceiling=0.0144, promote_below=0.01)
POLICY = Policy({'error': ERROR}, min_observations=200, max_observations=2_000)
BASELINE = {'error': (4, 3_800)}    # fixture: control share over the same window


class WilsonTests(unittest.TestCase):
    def test_z_values_come_from_the_standard_normal(self):
        self.assertEqual(Z95, NormalDist().inv_cdf(0.975))
        self.assertAlmostEqual(Z95, 1.959964, places=6)
        self.assertAlmostEqual(z_value(0.95), Z95, places=15)
        self.assertAlmostEqual(z_value(0.95, two_sided=False), 1.644854, places=6)

    def test_three_errors_in_two_hundred_and_the_baseline(self):
        interval = wilson(3, 200)
        self.assertAlmostEqual(interval.low, 0.005114237793258, places=12)
        self.assertAlmostEqual(interval.high, 0.043165728792690, places=12)
        self.assertEqual(round(interval.low * 100, 2), 0.51)
        self.assertEqual(round(interval.high * 100, 2), 4.32)
        self.assertEqual(interval.rate, 0.015)
        control = wilson(*BASELINE['error'])
        self.assertAlmostEqual(control.low, 0.000409421623436, places=12)
        self.assertAlmostEqual(control.high, 0.002703604760257, places=12)

    def test_matches_nist_handbook_one_sided_example(self):
        # e-Handbook 7.2.4.1: 26 defects in 200, z = 1.645, lower limit 0.09577.
        self.assertEqual(round(wilson(26, 200, z=1.645).low, 5), 0.09577)

    def test_zero_events_closed_form_and_bounds_stay_in_unit_interval(self):
        for total in (50, 200, 381):
            with self.subTest(total=total):
                interval = wilson(0, total)
                self.assertEqual(interval.low, 0.0)
                self.assertAlmostEqual(interval.high, Z95 ** 2 / (total + Z95 ** 2), places=15)
        self.assertEqual(wilson(200, 200).high, 1.0)
        wald_low = 0.005 - Z95 * math.sqrt(0.005 * 0.995 / 200)
        self.assertLess(wald_low, 0)                        # Wald goes below zero
        self.assertAlmostEqual(wilson(1, 200).low, 0.000883168715601, places=12)

    def test_observations_needed_before_promotion_is_possible(self):
        self.assertEqual(observations_needed(0.01), 381)
        self.assertLess(wilson(0, 381).high, 0.01)
        self.assertGreaterEqual(wilson(0, 380).high, 0.01)
        self.assertEqual(observations_needed(0.01, events=1), 563)
        self.assertEqual(observations_needed(0.0144), 263)

    def test_low_traffic_means_a_longer_window(self):
        self.assertEqual(window_hours(381, 3_000, 5), Fraction(127, 50))     # 2.54 h
        self.assertEqual(window_hours(381, 3_000, 5) * 60, Fraction(762, 5))  # 152.4 min
        self.assertEqual(window_hours(200, 3_000, 5), Fraction(4, 3))         # 80 min
        self.assertEqual(window_hours(381, 600, 5), Fraction(127, 10))        # 12.7 h


class DecisionTests(unittest.TestCase):
    def test_no_verdict_before_minimum_observations(self):
        verdict = decide({'error': (3, 199)}, BASELINE, POLICY)
        self.assertEqual((verdict.action, verdict.reasons),
                         ('continue', ('error: insufficient observations',)))
        verdict = decide({'error': (0, 200)}, {'error': (0, 150)}, POLICY)
        self.assertEqual(verdict.action, 'continue')

    def test_rollback_when_canary_is_worse_than_baseline(self):
        verdict = decide({'error': (3, 200)}, BASELINE, POLICY)
        self.assertEqual(verdict.action, 'rollback')
        self.assertEqual(verdict.reasons, ('error: worse than baseline',))
        canary, control = verdict.intervals['error']
        self.assertGreater(canary.low, control.high)
        self.assertLess(canary.low, ERROR.ceiling)

    def test_zero_errors_continue_until_upper_bound_is_below_threshold(self):
        self.assertEqual(decide({'error': (0, 200)}, BASELINE, POLICY).action, 'continue')
        self.assertEqual(decide({'error': (1, 200)}, BASELINE, POLICY).action, 'continue')
        self.assertEqual(decide({'error': (0, 381)}, BASELINE, POLICY).action, 'promote')
        self.assertEqual(decide({'error': (1, 381)}, BASELINE, POLICY).action, 'continue')
        self.assertEqual(decide({'error': (1, 563)}, BASELINE, POLICY).action, 'promote')

    def test_absolute_ceiling_catches_a_degraded_control(self):
        verdict = decide({'error': (10, 200)}, {'error': (100, 3_800)}, POLICY)
        self.assertEqual(verdict.reasons, ('error: lower bound above ceiling',))
        self.assertEqual(verdict.action, 'rollback')

    def test_acl_leak_rolls_back_before_any_statistics(self):
        verdict = decide({'error': (0, 10)}, BASELINE, POLICY, leaks=1)
        self.assertEqual((verdict.action, verdict.reasons), ('rollback', ('acl leak: 1',)))

    def test_inconclusive_canary_is_not_promoted_at_the_cap(self):
        policy = Policy({'error': ERROR}, min_observations=200, max_observations=400)
        verdict = decide({'error': (2, 400)}, BASELINE, policy)
        self.assertEqual(verdict.action, 'rollback')
        self.assertEqual(verdict.reasons[0], 'inconclusive at max observations')
        self.assertEqual(decide({'error': (2, 399)}, BASELINE, policy).action, 'continue')

    def test_every_metric_must_pass_before_promotion(self):
        # Fixture abstention limits; the same rule applies to each watched rate.
        policy = Policy({'error': ERROR, 'abstain': Limit(0.15, 0.10)}, 200, 2_000)
        baseline = {'error': (4, 3_800), 'abstain': (152, 3_800)}
        verdict = decide({'error': (0, 400), 'abstain': (20, 400)}, baseline, policy)
        self.assertEqual(verdict.action, 'promote')
        verdict = decide({'error': (0, 400), 'abstain': (30, 400)}, baseline, policy)
        self.assertEqual(verdict.action, 'rollback')
        self.assertEqual(verdict.reasons, ('abstain: worse than baseline',))

    def test_invalid_inputs_fail_closed_and_policy_is_frozen(self):
        for canary in ({'error': (5, 4)}, {'error': (1.0, 200)}, {'error': (True, 200)},
                       {'error': [0, 200]}, {}, {'error': (0, 200), 'latency': (0, 200)}):
            with self.subTest(canary=canary), self.assertRaises(ValueError):
                decide(canary, BASELINE, POLICY)
        with self.assertRaises(ValueError):
            Limit(ceiling=0.01, promote_below=0.02)
        with self.assertRaises(ValueError):
            Policy({'error': ERROR}, min_observations=500, max_observations=400)
        with self.assertRaises(ValueError):
            Policy({}, 200, 2_000)
        with self.assertRaises(ValueError):
            wilson(0, 0)
        with self.assertRaises(ValueError):
            wilson(1, 10, z=2)
        with self.assertRaises(ValueError):
            observations_needed(0.0)
        with self.assertRaises(ValueError):
            observations_needed(1e-9, cap=1_000)
        with self.assertRaises(ValueError):
            window_hours(381, 3_000, 101)
        with self.assertRaises(TypeError):
            decide({'error': (0, 200)}, BASELINE, {'error': ERROR})
        with self.assertRaises(ValueError):
            decide({'error': (0, 200)}, BASELINE, POLICY, leaks=-1)
        limits = {'error': ERROR}
        policy = Policy(limits, 200, 2_000)
        limits['abstain'] = Limit(0.15, 0.10)
        self.assertEqual(set(policy.limits), {'error'})
        with self.assertRaises(TypeError):
            policy.limits['abstain'] = Limit(0.15, 0.10)
        with self.assertRaises(FrozenInstanceError):
            policy.min_observations = 1


if __name__ == '__main__':
    unittest.main()
