from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
from pathlib import Path
import unittest

import yaml

from fde.handover import DEFAULT_REQUIREMENTS, REASONS, Evidence, Requirement, assess

ON = date(2026, 11, 27)            # 4-1 D10 in the frame fixture (D1 = Monday 2026-11-16)
ROOT = Path(__file__).resolve().parents[1]


def d(month, day):
    return date(2026, month, day)


def complete():
    return (Evidence('runbook_review', 'it_ops', d(11, 25), 'OPS-50'),
            Evidence('alert_drill', 'it_ops', d(11, 24), 'DRILL-3'),
            Evidence('restore_drill', 'it_ops', d(11, 24), 'DRILL-4', measured=48),
            Evidence('oncall_roster', 'it_ops', d(11, 26), 'ROSTER-1'),
            Evidence('access_review', 'security_officer', d(11, 20), 'SEC-13'),
            Evidence('error_budget_policy', 'it_ops', d(11, 20), 'DR-005'),
            Evidence('known_issues', 'fde', d(11, 26), 'KI-1'),
            Evidence('dashboards', 'it_ops', d(11, 25), 'DASH-1'))


def swap(items, key, **updates):
    return tuple(replace(item, **updates) if item.key == key else item for item in items)


class HandoverTests(unittest.TestCase):
    def test_proposed_requirements_exact_values(self):
        rows = [(r.key, r.blocking, r.max_age_days, r.limit) for r in DEFAULT_REQUIREMENTS]
        self.assertEqual(rows, [
            ('runbook_review', True, 180, None), ('alert_drill', True, 90, None),
            ('restore_drill', True, 90, 60), ('oncall_roster', True, 30, None),
            ('access_review', True, 90, None), ('error_budget_policy', True, 365, None),
            ('known_issues', True, 30, None), ('dashboards', False, 180, None)])
        self.assertEqual(sum(r.blocking for r in DEFAULT_REQUIREMENTS), 7)

    def test_complete_fresh_evidence_is_ready(self):
        result = assess(DEFAULT_REQUIREMENTS, complete(), ON)
        self.assertEqual((result.ready, result.blocking, result.warnings), (True, (), ()))

    def test_frame_example_exact_result(self):
        evidence = (
            Evidence('runbook_review', 'it_ops', d(11, 25), 'OPS-50', None),
            Evidence('alert_drill', 'it_ops', d(11, 24), 'DRILL-3', None),
            Evidence('restore_drill', 'it_ops', d(11, 24), 'DRILL-4', 75),
            Evidence('oncall_roster', None, d(11, 26), 'ROSTER-1', None),
            Evidence('access_review', 'security_officer', d(8, 20), 'SEC-12', None),
            Evidence('error_budget_policy', 'it_ops', d(11, 20), 'DR-005', None),
            Evidence('known_issues', 'fde', d(11, 26), 'KI-1', None))
        result = assess(DEFAULT_REQUIREMENTS, evidence, ON)
        self.assertFalse(result.ready)
        self.assertEqual(result.blocking, (('access_review', 'stale'),
                                           ('oncall_roster', 'no_owner'),
                                           ('restore_drill', 'over_limit')))
        self.assertEqual(result.warnings, (('dashboards', 'missing'),))
        self.assertEqual((ON - d(8, 20)).days, 99)

    def test_freshness_boundary_is_inclusive(self):
        fresh = swap(complete(), 'restore_drill', collected_on=d(8, 29))   # exactly 90 days
        self.assertEqual((ON - d(8, 29)).days, 90)
        self.assertTrue(assess(DEFAULT_REQUIREMENTS, fresh, ON).ready)
        stale = swap(complete(), 'restore_drill', collected_on=d(8, 28))
        self.assertEqual(assess(DEFAULT_REQUIREMENTS, stale, ON).blocking,
                         (('restore_drill', 'stale'),))
        roster = swap(complete(), 'oncall_roster', collected_on=d(10, 27))  # 31 days
        self.assertEqual(assess(DEFAULT_REQUIREMENTS, roster, ON).blocking,
                         (('oncall_roster', 'stale'),))

    def test_future_dated_and_undated_evidence_is_refused(self):
        future = swap(complete(), 'alert_drill', collected_on=d(11, 28))
        self.assertEqual(assess(DEFAULT_REQUIREMENTS, future, ON).blocking,
                         (('alert_drill', 'future_dated'),))
        undated = swap(complete(), 'alert_drill', collected_on=None, reference=None)
        self.assertEqual(assess(DEFAULT_REQUIREMENTS, undated, ON).blocking,
                         (('alert_drill', 'no_reference'), ('alert_drill', 'undated')))

    def test_limit_needs_a_measurement_at_or_below_it(self):
        for measured, expected in ((60, ()), (0, ()), (61, (('restore_drill', 'over_limit'),)),
                                   (None, (('restore_drill', 'unmeasured'),))):
            with self.subTest(measured=measured):
                evidence = swap(complete(), 'restore_drill', measured=measured)
                self.assertEqual(assess(DEFAULT_REQUIREMENTS, evidence, ON).blocking, expected)

    def test_all_gaps_of_one_item_are_listed(self):
        evidence = swap(complete(), 'restore_drill', owner=None, collected_on=d(1, 5),
                        measured=None)
        self.assertEqual(assess(DEFAULT_REQUIREMENTS, evidence, ON).blocking, (
            ('restore_drill', 'no_owner'), ('restore_drill', 'stale'),
            ('restore_drill', 'unmeasured')))

    def test_missing_blocking_evidence_blocks_but_warnings_do_not(self):
        without_known = tuple(e for e in complete() if e.key != 'known_issues')
        result = assess(DEFAULT_REQUIREMENTS, without_known, ON)
        self.assertEqual((result.ready, result.blocking), (False, (('known_issues', 'missing'),)))
        stale_dash = swap(complete(), 'dashboards', collected_on=d(1, 5), owner=None)
        result = assess(DEFAULT_REQUIREMENTS, stale_dash, ON)
        self.assertTrue(result.ready)
        self.assertEqual(result.warnings, (('dashboards', 'no_owner'), ('dashboards', 'stale')))
        nothing = assess(DEFAULT_REQUIREMENTS, (), ON)
        self.assertEqual(len(nothing.blocking), 7)
        self.assertEqual(nothing.warnings, (('dashboards', 'missing'),))

    def test_unknown_or_duplicate_inputs_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'unknown requirement'):
            typo = (Evidence('runbok_review', 'fde', ON, 'X'),)
            assess(DEFAULT_REQUIREMENTS, complete() + typo, ON)
        with self.assertRaisesRegex(ValueError, 'duplicate evidence'):
            assess(DEFAULT_REQUIREMENTS, complete() + complete()[:1], ON)
        with self.assertRaisesRegex(ValueError, 'duplicate requirement'):
            assess(DEFAULT_REQUIREMENTS + DEFAULT_REQUIREMENTS[:1], complete(), ON)
        for arguments in ((DEFAULT_REQUIREMENTS, complete(), datetime(2026, 11, 27)),
                          (DEFAULT_REQUIREMENTS, complete(), '2026-11-27'),
                          ((), complete(), ON), (list(DEFAULT_REQUIREMENTS), complete(), ON),
                          (DEFAULT_REQUIREMENTS, list(complete()), ON),
                          (DEFAULT_REQUIREMENTS, ('runbook_review',), ON)):
            with self.subTest(arguments=[type(a).__name__ for a in arguments]):
                with self.assertRaises(ValueError):
                    assess(*arguments)

    def test_record_shapes_fail_closed(self):
        for args in (('Runbook', True, 90), ('runbook', 1, 90), ('runbook', True, 0),
                     ('runbook', True, 90.0), ('runbook', True, 90, -1),
                     ('runbook', True, 90, 1.5)):
            with self.subTest(requirement=args):
                with self.assertRaises(ValueError):
                    Requirement(*args)
        for args in (('alert_drill', 'IT Ops', ON, 'X'),
                     ('alert_drill', 'it_ops', '2026-11-27', 'X'),
                     ('alert_drill', 'it_ops', datetime(2026, 11, 27), 'X'),
                     ('alert_drill', 'it_ops', ON, 'free text'), ('alert_drill', 'it_ops', ON, ''),
                     ('alert_drill', 'it_ops', ON, 'X', -1),
                     ('alert_drill', 'it_ops', ON, 'X', True)):
            with self.subTest(evidence=args):
                with self.assertRaises(ValueError):
                    Evidence(*args)

    def test_frame_dates_follow_the_ten_working_day_schedule(self):
        days = [date(2026, 11, 16 + offset) for offset in range(12)]
        working = [day for day in days if day.weekday() < 5]
        self.assertEqual(working[0].strftime('%A'), 'Monday')
        self.assertEqual(working[9], ON)

    def test_alert_drill_names_the_rules_in_alerts_yaml(self):
        # 5-6 says the drill proves the 4-3 page and ticket alerts reached their receivers.
        groups = yaml.safe_load((ROOT / 'deploy' / 'alerts.yaml').read_text(encoding='utf-8'))
        severities = {r['alert']: r['labels']['severity']
                      for group in groups['groups'] for r in group['rules']}
        self.assertEqual(severities['RagFastBudgetBurn'], 'page')
        self.assertEqual(severities['RagTelemetryMissing'], 'ticket')

    def test_vocabulary_and_immutability(self):
        self.assertEqual(len(REASONS), 8)
        result = assess(DEFAULT_REQUIREMENTS, complete(), ON)
        with self.assertRaises(FrozenInstanceError):
            result.ready = False
        with self.assertRaises(FrozenInstanceError):
            DEFAULT_REQUIREMENTS[0].max_age_days = 1


if __name__ == '__main__':
    unittest.main()
