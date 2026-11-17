from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import unittest

import yaml

from fde.incident import (PHASES, ActionItem, Entry, Postmortem, Timeline, classify, gaps,
                          review_due)

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]


def at(hour, minute, day=7):
    # Monday 2026-12-07, after the 4-1 D10 handover on 2026-11-27 (5-6).
    return datetime(2026, 12, day, hour, minute, tzinfo=KST)


def fixture(count=5):
    entries = (Entry('impact_start', at(9, 0), 'metric:rag_requests_total'),
               Entry('detected', at(9, 7), 'alert:RagFastBudgetBurn'),
               Entry('acknowledged', at(9, 12), 'page:oncall-ack'),
               Entry('mitigated', at(9, 40), 'ticket:OPS-41'),
               Entry('resolved', at(11, 5), 'ticket:OPS-41'))
    return Timeline(entries[:count])


def report(**updates):
    args = dict(incident_id='INC-007', severity='SEV2', timeline=fixture(),
                causes=('queue-no-cap', 'no-burst-test'),
                actions=(ActionItem('OPS-42', 'it_ops', date(2026, 12, 18), 'queue-no-cap'),
                         ActionItem('OPS-43', 'fde', date(2026, 12, 23), 'no-burst-test')))
    args.update(updates)
    return Postmortem(**args)


class TimelineTests(unittest.TestCase):
    def test_frame_fixture_exact_durations(self):
        self.assertEqual(fixture().durations(),
                         {'detect': 420, 'acknowledge': 300, 'mitigate': 2400, 'resolve': 7500})
        self.assertEqual(fixture().time('resolved'), at(11, 5))

    def test_open_incident_has_unknown_later_durations(self):
        self.assertEqual(fixture(3).durations(),
                         {'detect': 420, 'acknowledge': 300, 'mitigate': None, 'resolve': None})
        self.assertIsNone(fixture(1).time('detected'))
        self.assertEqual(fixture(1).durations(),
                         {'detect': None, 'acknowledge': None, 'mitigate': None, 'resolve': None})

    def test_timeline_must_be_monotonic_but_allows_equal_times(self):
        entries = fixture().entries
        early_ack = Entry('acknowledged', at(9, 6), 'page:oncall-ack')
        with self.assertRaisesRegex(ValueError, 'not monotonic'):
            Timeline(entries[:2] + (early_ack,))
        same = Timeline((entries[0], Entry('detected', at(9, 0), 'alert:synthetic-probe')))
        self.assertEqual(same.durations()['detect'], 0)
        utc = Entry('detected', datetime(2026, 12, 7, 0, 7, tzinfo=timezone.utc), 'alert:x')
        self.assertEqual(Timeline((entries[0], utc)).durations()['detect'], 420)

    def test_detection_includes_the_fast_burn_pending_wait(self):
        # The fixture's detected time comes from RagFastBudgetBurn, which pages only after
        # its condition has held for `for: 2m` (deploy/alerts.yaml, 4-3).
        groups = yaml.safe_load((ROOT / 'deploy' / 'alerts.yaml').read_text(encoding='utf-8'))
        rules = {r['alert']: r for group in groups['groups'] for r in group['rules']}
        fast = rules['RagFastBudgetBurn']
        self.assertEqual((fast['for'], fast['labels']['severity']), ('2m', 'page'))
        self.assertEqual(fixture().entries[1].source, 'alert:RagFastBudgetBurn')
        self.assertGreaterEqual(fixture().durations()['detect'], 120)

    def test_phases_form_a_prefix_without_gaps_or_repeats(self):
        entries = fixture().entries
        for broken in ((entries[1],), entries[:1] + entries[2:3], entries[:2] + entries[1:2],
                       (entries[1], entries[0]), entries[:4] + entries[3:4]):
            with self.subTest(phases=[e.phase for e in broken]):
                with self.assertRaises(ValueError):
                    Timeline(broken)
        self.assertEqual(tuple(e.phase for e in fixture().entries), PHASES)

    def test_entries_fail_closed(self):
        for phase, moment, source in (
                ('started', at(9, 0), 'alert:x'),
                ('detected', datetime(2026, 12, 7, 9), 'alert:x'),
                ('detected', at(9, 0).replace(microsecond=5), 'alert:x'),
                ('detected', '2026-12-07T09:00+09:00', 'alert:x'), ('detected', at(9, 0), ''),
                ('detected', at(9, 0), 'RagFastBudgetBurn'), ('detected', at(9, 0), 'alert: x')):
            with self.subTest(phase=phase, moment=moment, source=source):
                with self.assertRaises(ValueError):
                    Entry(phase, moment, source)
        for value in ((), [fixture().entries[0]], ('impact_start',)):
            with self.subTest(entries=value):
                with self.assertRaises(ValueError):
                    Timeline(value)
        with self.assertRaises(ValueError):
            fixture().time('started')


class SeverityTests(unittest.TestCase):
    def test_proposed_matrix_exact_levels(self):
        self.assertEqual(classify(20, False, False), 'SEV2')
        self.assertEqual(classify(9, False, False), 'SEV3')
        self.assertEqual(classify(10, False, False), 'SEV2')
        self.assertEqual(classify(49, False, False), 'SEV2')
        self.assertEqual(classify(50, False, False), 'SEV1')
        self.assertEqual(classify(0, False, False), 'SEV3')
        self.assertEqual(classify(1, False, True), 'SEV1')
        self.assertEqual(classify(1, True, False), 'SEV1')
        self.assertEqual(classify(5, False, False, all_users=8, major_users=4), 'SEV2')

    def test_unknown_inputs_count_as_worst_case(self):
        self.assertEqual(classify(1, False, None), 'SEV1')
        self.assertEqual(classify(1, None, False), 'SEV1')
        self.assertEqual(classify(None, False, False), 'SEV1')

    def test_invalid_severity_inputs_fail_closed(self):
        for args in ((-1, False, False), (1.0, False, False), (True, False, False),
                     ('3', False, False), (1, 0, False), (1, False, 'no')):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    classify(*args)
        for kwargs in ({'all_users': 0}, {'major_users': 0}, {'major_users': 51},
                       {'all_users': 50.0}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    classify(1, False, False, **kwargs)

    def test_review_due_windows(self):
        resolved = at(11, 5)                      # Monday 2026-12-07
        self.assertEqual(resolved.strftime('%A'), 'Monday')
        self.assertEqual(review_due('SEV1', resolved), date(2026, 12, 10))
        self.assertEqual(review_due('SEV2', resolved), date(2026, 12, 14))
        self.assertIsNone(review_due('SEV3', resolved))
        friday = at(18, 0, day=11)
        self.assertEqual(review_due('SEV2', friday), date(2026, 12, 18))
        self.assertEqual(review_due('SEV1', friday), date(2026, 12, 14))
        # Five business days span at most 7 calendar days (Monday or Friday start).
        spans = [(review_due('SEV2', at(12, 0, day=day)) - date(2026, 12, day)).days
                 for day in range(7, 14)]
        self.assertEqual(spans, [7, 7, 7, 7, 7, 6, 5])
        # The window counts from the local date of the given offset: Sunday 23:30 UTC is
        # Monday 08:30 in KST, so the same instant yields different due dates.
        sunday_utc = datetime(2026, 12, 6, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(review_due('SEV2', sunday_utc), date(2026, 12, 11))
        self.assertEqual(review_due('SEV2', sunday_utc.astimezone(KST)), date(2026, 12, 14))
        for severity, moment in (('SEV4', resolved), ('SEV2', datetime(2026, 12, 7)),
                                 ('SEV2', date(2026, 12, 7)), (None, resolved)):
            with self.subTest(severity=severity, moment=moment):
                with self.assertRaises(ValueError):
                    review_due(severity, moment)


class PostmortemTests(unittest.TestCase):
    def test_complete_postmortem_has_no_gaps(self):
        self.assertEqual(gaps(report()), [])

    def test_frame_example_gaps(self):
        actions = (ActionItem('OPS-42', 'it_ops', date(2026, 12, 18), 'queue-no-cap'),
                   ActionItem('OPS-43', None, None, 'no-burst-test'))
        self.assertEqual(gaps(report(actions=actions)),
                         [('missing_due', 'OPS-43'), ('missing_owner', 'OPS-43')])

    def test_every_cause_needs_a_linked_action(self):
        actions = (ActionItem('OPS-42', 'it_ops', date(2026, 12, 18), 'queue-no-cap'),
                   ActionItem('OPS-44', 'fde', date(2026, 12, 23), 'operator-error'),
                   ActionItem('OPS-45', 'fde', date(2026, 12, 23), None))
        self.assertEqual(gaps(report(actions=actions)), [
            ('cause_without_action', 'no-burst-test'), ('unlinked_trigger', 'OPS-44'),
            ('unlinked_trigger', 'OPS-45')])

    def test_empty_and_unresolved_postmortems_are_incomplete(self):
        self.assertEqual(gaps(report(causes=(), actions=())),
                         [('no_action_items', 'INC-007'), ('no_causes', 'INC-007')])
        self.assertEqual(gaps(report(timeline=fixture(4))), [('unresolved', 'INC-007')])

    def test_postmortem_shapes_fail_closed(self):
        item = report().actions[0]
        for updates in ({'incident_id': ''}, {'severity': 'SEV0'}, {'timeline': fixture().entries},
                        {'causes': ['queue-no-cap']}, {'causes': ('a', 'a')},
                        {'causes': ('bad cause',)}, {'actions': [item]}, {'actions': (item, item)},
                        {'actions': ('OPS-42',)}):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    report(**updates)
        for args in (('', 'fde', None, None), ('OPS-1', 'Kim', None, None),
                     ('OPS-1', 'fde', '2026-12-18', None),
                     ('OPS-1', 'fde', datetime(2026, 12, 18, tzinfo=KST), None),
                     ('OPS-1', 'fde', None, 'bad trigger')):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    ActionItem(*args)
        with self.assertRaises(ValueError):
            gaps(fixture())
        with self.assertRaises(FrozenInstanceError):
            item.owner = None


if __name__ == '__main__':
    unittest.main()
