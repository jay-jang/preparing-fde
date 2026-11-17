from dataclasses import FrozenInstanceError
from datetime import date
from fractions import Fraction
from pathlib import Path
import re
import unittest

import yaml

from fde.error_budget import (MIN_REQUESTS, RULES, BurnRule, Policy, Slo, decide, permit,
                              requires_postmortem)

ROOT = Path(__file__).resolve().parents[1]
SLO = Slo('0.999', 30)       # the 4-3 / deploy/alerts.yaml example SLO (proposal value)
TODAY = date(2026, 11, 17)
POLICY = Policy(SLO, ('customer plant operations lead', 'FDE lead'), revisit=date(2027, 5, 31))


class BudgetArithmeticTests(unittest.TestCase):
    def test_thirty_day_budget_is_exactly_43_2_minutes(self):
        self.assertEqual(SLO.budget_ratio, Fraction(1, 1_000))
        self.assertEqual(SLO.window_minutes, 43_200)
        self.assertEqual(SLO.budget_minutes, Fraction(216, 5))
        self.assertEqual(float(SLO.budget_minutes), 43.2)

    def test_thresholds_match_workbook_table_5_8(self):
        self.assertEqual([rule.threshold(SLO) for rule in RULES],
                         [Fraction('14.4'), Fraction(6), Fraction(1)])
        self.assertEqual([rule.error_ratio(SLO) for rule in RULES],
                         [Fraction('0.0144'), Fraction('0.006'), Fraction('0.001')])
        self.assertEqual([(r.severity, r.long_minutes, r.short_minutes) for r in RULES],
                         [('page', 60, 5), ('page', 360, 30), ('ticket', 4_320, 360)])
        self.assertTrue(all(rule.long_minutes == 12 * rule.short_minutes for rule in RULES))

    def test_exhaustion_times_match_workbook_table_5_4(self):
        self.assertEqual(SLO.exhaustion_minutes(1), 43_200)          # 30 days
        self.assertEqual(SLO.exhaustion_minutes(2), 21_600)          # 15 days
        self.assertEqual(SLO.exhaustion_minutes(10), 4_320)          # 3 days
        self.assertEqual(SLO.exhaustion_minutes(1_000), Fraction(216, 5))   # 43.2 min
        self.assertEqual(SLO.exhaustion_minutes('14.4'), 3_000)      # 50 hours

    def test_the_window_changes_what_14_4_means(self):
        four_weeks = Slo('0.999', 28)
        self.assertEqual(four_weeks.consumed_by_burn('14.4', 60), Fraction(3, 140))   # 2.14%
        self.assertEqual(RULES[0].threshold(four_weeks), Fraction('13.44'))
        self.assertEqual(SLO.consumed_by_burn('14.4', 60), Fraction(1, 50))           # 2%
        self.assertEqual(Slo('0.995', 28).budget_minutes, Fraction('201.6'))

    def test_event_budgets_and_low_traffic(self):
        self.assertEqual(SLO.budget_events(1_000_000), 1_000)    # workbook policy example
        self.assertEqual(SLO.budget_events(7_200), Fraction('7.2'))   # 10 req/h for 30 days
        self.assertEqual(SLO.consumed_events(1, 7_200), Fraction(5, 36))   # 13.9%
        self.assertEqual(round(float(SLO.consumed_events(1, 7_200)) * 100, 1), 13.9)
        self.assertEqual(SLO.burn_rate(1, 10), 100)

    def test_canary_exposure_scales_budget_spend(self):
        # 20% errors on a 5% canary is a 1% overall error ratio (workbook canary example).
        overall = Fraction(5, 100) * Fraction(20, 100)
        self.assertEqual(overall, Fraction(1, 100))
        burn = SLO.burn_rate(1, 100)
        self.assertEqual(burn, 10)
        self.assertEqual(SLO.consumed_by_burn(burn, 60), Fraction(1, 72))   # 1.39% per hour


class AlertRuleTests(unittest.TestCase):
    def test_alerts_yaml_implements_the_fast_page_rule(self):
        groups = yaml.safe_load((ROOT / 'deploy' / 'alerts.yaml').read_text(encoding='utf-8'))
        rules = [r for group in groups['groups'] for r in group['rules']]
        rule = next(r for r in rules if r['alert'] == 'RagFastBudgetBurn')
        expr = rule['expr']
        fast = RULES[0]
        self.assertEqual(rule['labels']['severity'], fast.severity)
        units = {'m': 1, 'h': 60}
        found = re.findall(r'rag_requests_total\{outcome="error"\}\[(\d+)([mh])\]', expr)
        self.assertEqual(len(found), 2)
        windows = {int(n) * units[u] for n, u in found}
        self.assertEqual(windows, {fast.long_minutes, fast.short_minutes})
        factors = re.findall(r'> ([\d.]+) \* ([\d.]+)', expr)
        self.assertEqual(len(factors), 2)
        for burn, ratio in factors:
            self.assertEqual(Fraction(burn), fast.threshold(SLO))
            self.assertEqual(Fraction(ratio), SLO.budget_ratio)
        minimum = re.search(r'increase\(rag_requests_total\[1h\]\)\) >= (\d+)', expr)
        self.assertEqual(int(minimum[1]), MIN_REQUESTS)

    def test_fires_only_when_both_windows_burn_with_enough_requests(self):
        fast = RULES[0]
        self.assertTrue(fast.fires(SLO, (2, 100), (1, 9)))
        self.assertFalse(fast.fires(SLO, (3, 150), (0, 13)))     # short window recovered
        self.assertFalse(fast.fires(SLO, (2, 99), (1, 9)))       # below 100 requests
        self.assertFalse(fast.fires(SLO, (9, 625), (1, 9)))      # exactly 1.44% is not above
        self.assertTrue(fast.fires(SLO, (10, 625), (1, 9)))
        with self.assertRaises(ValueError):
            fast.fires(SLO, (3, 2), (0, 1))


class PolicyTests(unittest.TestCase):
    def test_states_follow_consumed_budget(self):
        cases = {10: ('normal', Fraction(25, 108)), 25: ('slow', Fraction(125, 216)),
                 Fraction('43.2'): ('slow', Fraction(1)), 45: ('freeze', Fraction(25, 24))}
        for minutes, (state, consumed) in cases.items():
            with self.subTest(minutes=minutes):
                decision = decide(POLICY, bad_minutes=minutes, today=TODAY)
                self.assertEqual((decision.state, decision.consumed), (state, consumed))
        decision = decide(POLICY, bad_minutes=10, today=TODAY)
        self.assertEqual(decision.remaining, Fraction(83, 108))
        self.assertEqual(SLO.budget_minutes * decision.remaining, Fraction('33.2'))
        self.assertEqual((decision.canary_scale, decision.max_changes), (1, None))
        slow = decide(POLICY, bad_minutes=25, today=TODAY)
        self.assertEqual((slow.canary_scale, slow.max_changes), (2, 1))

    def test_request_based_sli_counts_bad_events(self):
        # 4-3 availability is request-based; the workbook policy example: 1,000,000 requests.
        cases = {499: 'normal', 500: 'slow', 1_000: 'slow', 1_001: 'freeze'}
        for bad, state in cases.items():
            with self.subTest(bad=bad):
                decision = decide(POLICY, bad_events=(bad, 1_000_000), today=TODAY)
                self.assertEqual((decision.state, decision.consumed), (state, Fraction(bad, 1_000)))
        self.assertTrue(requires_postmortem(POLICY, incident_bad_events=(201, 1_000_000)))
        self.assertFalse(requires_postmortem(POLICY, incident_bad_events=(200, 1_000_000)))
        for kwargs in ({}, {'bad_minutes': 1, 'bad_events': (1, 100)}, {'bad_events': [1, 100]},
                       {'bad_events': (2, 1)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                decide(POLICY, today=TODAY, **kwargs)

    def test_freeze_allows_only_p0_fixes_and_security(self):
        frozen = decide(POLICY, bad_minutes=45, today=TODAY)
        self.assertEqual(frozen.allowed, ('p0_fix', 'security'))
        for kind in ('feature', 'config', 'fix'):
            with self.subTest(kind=kind), self.assertRaises(PermissionError):
                permit(frozen, kind)
        permit(frozen, 'p0_fix')
        permit(frozen, 'security', changes=3)
        with self.assertRaises(ValueError):
            permit(frozen, 'hotfix')

    def test_slow_state_ships_one_change_per_release(self):
        slow = decide(POLICY, bad_minutes=25, today=TODAY)
        permit(slow, 'feature')
        with self.assertRaises(PermissionError):
            permit(slow, 'feature', changes=2)
        permit(decide(POLICY, bad_minutes=10, today=TODAY), 'feature', changes=5)
        for changes in (0, 1.0, True):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                permit(slow, 'fix', changes=changes)

    def test_postmortem_over_twenty_percent_of_budget(self):
        self.assertTrue(requires_postmortem(POLICY, 10))                # 23.1%
        self.assertFalse(requires_postmortem(POLICY, 8))                 # 18.5%
        self.assertEqual(POLICY.slo.budget_minutes * POLICY.postmortem_over, Fraction('8.64'))
        self.assertFalse(requires_postmortem(POLICY, Fraction('8.64')))  # exactly 20%
        self.assertTrue(requires_postmortem(POLICY, Fraction('8.65')))

    def test_invalid_inputs_fail_closed(self):
        with self.assertRaises(TypeError):
            Slo(0.999, 30)                                  # floats are not rounded
        for target, days in (('1', 30), ('0', 30), ('abc', 30), ('999/1000', 30), ('1e-3', 30),
                             ('0.999', 0), ('0.999', 30.0)):
            with self.subTest(target=target, days=days), self.assertRaises(ValueError):
                Slo(target, days)
        with self.assertRaises(ValueError):
            decide(POLICY, bad_minutes=-1, today=TODAY)
        with self.assertRaises(ValueError):
            decide(POLICY, bad_minutes=43_201, today=TODAY)          # more than the window
        with self.assertRaises(ValueError):
            decide(POLICY, bad_minutes=1, today=date(2027, 6, 1))   # past the revisit date
        for approvers in (('FDE lead',), ('FDE lead', 'FDE lead'), ('FDE lead', ' '), ['a', 'b']):
            with self.subTest(approvers=approvers), self.assertRaises(ValueError):
                Policy(SLO, approvers, revisit=date(2027, 5, 31))
        with self.assertRaises(ValueError):
            RULES[2].threshold(Slo('0.999', 1))             # 3-day window inside a 1-day SLO
        with self.assertRaises(ValueError):
            BurnRule('page', 5, 60, Fraction('0.02'))
        with self.assertRaises(FrozenInstanceError):
            SLO.days = 28


if __name__ == '__main__':
    unittest.main()
