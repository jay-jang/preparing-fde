from dataclasses import FrozenInstanceError
from fractions import Fraction
import math
import unittest

from fde.queueing import erlang_b, erlang_c, mmc, smallest_servers

ARRIVAL = 50 / 60          # 50 users x one question per minute (proposal value)
SERVICE = 2.4              # assumed mean RUNNING seconds, so a = 2.0 Erlang


def reference_erlang_c(servers, offered):
    """Closed form with exact fractions, independent of the recursion."""
    load = Fraction(offered)
    head = sum(load ** k / math.factorial(k) for k in range(servers))
    tail = load ** servers / math.factorial(servers) * servers / (servers - load)
    return tail / (head + tail)


class ErlangTests(unittest.TestCase):
    def test_erlang_b_matches_hand_checked_fractions(self):
        self.assertAlmostEqual(erlang_b(1, 2.0), 2 / 3, places=15)
        self.assertAlmostEqual(erlang_b(3, 2.0), 4 / 19, places=15)
        self.assertAlmostEqual(erlang_b(4, 2.0), 2 / 21, places=15)
        self.assertAlmostEqual(erlang_b(5, 2.0), 4 / 109, places=15)

    def test_erlang_c_matches_hand_checked_fractions(self):
        self.assertAlmostEqual(erlang_c(3, 2.0), 4 / 9, places=15)
        self.assertAlmostEqual(erlang_c(4, 2.0), 4 / 23, places=15)
        self.assertAlmostEqual(erlang_c(5, 2.0), 4 / 67, places=15)
        self.assertAlmostEqual(erlang_c(1, 0.5), 0.5, places=15)   # M/M/1: P(wait) = rho

    def test_recursion_matches_closed_form(self):
        for servers, offered in ((5, 2.5), (6, 3.0), (7, 4.0), (12, 9.5)):
            with self.subTest(servers=servers, offered=offered):
                self.assertAlmostEqual(erlang_c(servers, offered),
                                       float(reference_erlang_c(servers, offered)), places=13)

    def test_large_server_counts_stay_finite_without_factorials(self):
        with self.assertRaises(OverflowError):
            float(math.factorial(2_000))
        blocking, waiting = erlang_b(2_000, 1_900.0), erlang_c(2_000, 1_900.0)
        self.assertTrue(0 < blocking < waiting < 1)
        self.assertAlmostEqual(blocking, 0.000678969296498602, places=15)
        self.assertAlmostEqual(waiting, 0.013406437303190364, places=14)
        self.assertLess(erlang_c(2_001, 1_900.0), waiting)

    def test_unstable_queue_is_refused(self):
        for servers, offered in ((2, 2.0), (2, 2.5), (5, 6.0)):
            with self.subTest(servers=servers, offered=offered):
                with self.assertRaises(ValueError):
                    erlang_c(servers, offered)
        with self.assertRaises(ValueError):
            mmc(1.0, 2.0, 2)


class RunningCaseTests(unittest.TestCase):
    def test_five_slots_at_running_case_load(self):
        estimate = mmc(ARRIVAL, SERVICE, 5)
        self.assertAlmostEqual(estimate.offered_load, 2.0, places=15)
        self.assertAlmostEqual(estimate.utilization, 0.4, places=15)
        self.assertAlmostEqual(estimate.wait_probability, 4 / 67, places=15)
        self.assertAlmostEqual(estimate.mean_wait_seconds, 16 / 335, places=15)
        self.assertAlmostEqual(estimate.wait_exceeds(1), 4 / 67 * math.exp(-1.25), places=15)
        self.assertEqual(round(estimate.wait_exceeds(1), 4), 0.0171)
        self.assertEqual(estimate.wait_exceeds(0), estimate.wait_probability)

    def test_four_slots_miss_the_wait_target(self):
        estimate = mmc(ARRIVAL, SERVICE, 4)
        self.assertAlmostEqual(estimate.mean_wait_seconds, 0.208695652173913, places=14)
        self.assertAlmostEqual(estimate.wait_exceeds(1), 4 / 23 * math.exp(-5 / 6), places=15)
        self.assertEqual(round(estimate.wait_exceeds(1), 4), 0.0756)

    def test_smallest_slot_count_for_target(self):
        found = smallest_servers(ARRIVAL, SERVICE, wait_seconds=1, max_probability=0.05)
        self.assertEqual(found.servers, 5)

    def test_doubling_traffic_is_nonlinear(self):
        self.assertEqual(round(mmc(2 * ARRIVAL, SERVICE, 5).wait_exceeds(1), 4), 0.3653)
        self.assertEqual(round(mmc(2 * ARRIVAL, SERVICE, 6).wait_exceeds(1), 4), 0.1238)
        found = smallest_servers(2 * ARRIVAL, SERVICE, wait_seconds=1, max_probability=0.05)
        self.assertEqual(found.servers, 7)
        self.assertEqual(round(found.wait_exceeds(1), 4), 0.0387)

    def test_ten_factories_reach_the_single_gpu_sequence_limit(self):
        # 6-1: x10 factories -> 500 users; 5-3 assumptions give 26 sequences per GPU
        found = smallest_servers(10 * ARRIVAL, SERVICE, wait_seconds=1, max_probability=0.05)
        self.assertEqual(found.servers, 25)
        self.assertAlmostEqual(found.utilization, 0.8, places=12)
        self.assertEqual(round(mmc(10 * ARRIVAL, SERVICE, 24).wait_exceeds(1), 4), 0.0563)
        self.assertEqual(round(found.wait_exceeds(1), 4), 0.0260)

    def test_measured_service_times_replace_the_assumption(self):
        measured = {6: 3.6, 4: 2.4, 5: 3.0}     # fixture, not a real load-test result
        self.assertEqual(round(mmc(ARRIVAL, 3.0, 5).wait_exceeds(1), 4), 0.0567)
        found = smallest_servers(ARRIVAL, measured, wait_seconds=1, max_probability=0.05)
        self.assertEqual((found.servers, found.service_seconds), (6, 3.6))
        self.assertEqual(round(found.wait_exceeds(1), 4), 0.0431)

    def test_no_extrapolation_beyond_measured_slots(self):
        with self.assertRaises(ValueError):
            smallest_servers(ARRIVAL, {4: 2.4, 5: 3.0}, wait_seconds=1, max_probability=0.05)
        with self.assertRaises(ValueError):
            smallest_servers(ARRIVAL, {}, wait_seconds=1, max_probability=0.05)
        with self.assertRaises(ValueError):
            smallest_servers(ARRIVAL, SERVICE, wait_seconds=1, max_probability=0.05, max_servers=4)


class ValidationTests(unittest.TestCase):
    def test_invalid_numbers_fail_closed(self):
        for value in (0, -1, True, float('nan'), float('inf'), None, '1'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    mmc(value, SERVICE, 5)
                with self.assertRaises(ValueError):
                    mmc(ARRIVAL, value, 5)
        for servers in (0, -1, 1.0, True, 100_001, None):
            with self.subTest(servers=servers):
                with self.assertRaises(ValueError):
                    erlang_b(servers, 1.0)
        for bad in ({True: 2.4}, {0: 2.4}, {5: 0}, {5: float('nan')}):
            with self.subTest(mapping=bad):
                with self.assertRaises(ValueError):
                    smallest_servers(ARRIVAL, bad, wait_seconds=1, max_probability=0.05)

    def test_targets_and_wait_times_are_validated(self):
        estimate = mmc(ARRIVAL, SERVICE, 5)
        for seconds in (-1, float('nan'), float('inf'), None):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    estimate.wait_exceeds(seconds)
        for probability in (0.0, 1.0, 1, -0.1, float('nan')):
            with self.subTest(probability=probability):
                with self.assertRaises(ValueError):
                    smallest_servers(ARRIVAL, SERVICE, wait_seconds=1, max_probability=probability)

    def test_estimate_is_frozen(self):
        estimate = mmc(ARRIVAL, SERVICE, 5)
        with self.assertRaises(FrozenInstanceError):
            estimate.servers = 6


if __name__ == '__main__':
    unittest.main()
