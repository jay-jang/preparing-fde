from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import unittest

from fde.capacity import vector_bytes
from fde.release_gate import evaluate
from fde.reindex import (
    Change, ChunkRow, Reindex, apply_changes, catch_up_seconds, counts, parity,
)

ACL = 'sha256:' + 'a' * 64
REVOKED = 'sha256:' + 'b' * 64
BLUE = 'sha256:' + '1' * 64
GREEN = 'sha256:' + '2' * 64
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
WEEK = timedelta(days=7)


def snapshot():
    return {'c1': ChunkRow(1, False, ACL), 'c2': ChunkRow(1, False, ACL),
            'c3': ChunkRow(4, True, ACL)}


def later_changes():
    return [Change(1_001, 'c1', ChunkRow(2, False, ACL)),        # content update
            Change(1_002, 'c2', ChunkRow(1, True, ACL)),         # deletion
            Change(1_003, 'c1', ChunkRow(2, False, REVOKED)),    # ACL change only
            Change(1_004, 'c4', ChunkRow(1, False, ACL))]        # new chunk


def report(digest, leaked=False):
    rows = []
    for kind in ('factoid', 'semantic', 'unanswerable', 'forbidden'):
        for i in range(30):
            rows.append({'id': f'{kind}-{i}', 'kind': kind, 'retrieval_hit': True,
                         'supported': True, 'abstained': kind in ('unanswerable', 'forbidden'),
                         'leaked': leaked and i == 0, 'latency_ms': 1_000})
    return {'dataset_version': 'holdout-v1', 'artifact_digest': digest, 'rows': rows}


def caught_up_job(position=1_004):
    return Reindex(BLUE, GREEN).built(snapshot_position=1_000).caught_up(position)


class ArithmeticTests(unittest.TestCase):
    def test_running_case_catch_up(self):
        build = 1_000_000 / 200
        self.assertEqual(build, 5_000)
        self.assertEqual(50 * build, 250_000)
        seconds = catch_up_seconds(50 * build, replay_per_second=200, change_per_second=50)
        self.assertAlmostEqual(seconds, 1_666.6666666666667, places=9)
        self.assertEqual(round(seconds, 1), 1_666.7)
        self.assertEqual(catch_up_seconds(0, replay_per_second=2, change_per_second=1), 0)

    def test_burst_rate_never_converges(self):
        for change in (200, 500):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    catch_up_seconds(250_000, replay_per_second=200, change_per_second=change)
        for bad in (-1, float('nan'), float('inf'), None, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    catch_up_seconds(bad, replay_per_second=200, change_per_second=50)
                with self.assertRaises(ValueError):
                    catch_up_seconds(1, replay_per_second=bad, change_per_second=50)

    def test_rollback_window_keeps_two_vector_sets(self):
        self.assertEqual(2 * vector_bytes(1_000_000, 768, 4), 6_144_000_000)


class ReplayAndParityTests(unittest.TestCase):
    def test_replay_carries_deletions_and_acl_changes(self):
        green, position = apply_changes(snapshot(), later_changes(), after=1_000)
        self.assertEqual(position, 1_004)
        self.assertEqual(green['c1'], ChunkRow(2, False, REVOKED))
        self.assertTrue(green['c2'].deleted)
        self.assertEqual(counts(green), (2, 2))
        blue = dict(green)
        self.assertEqual(parity(blue, green), ())

    def test_parity_reports_every_kind_of_difference_sorted(self):
        green, _ = apply_changes(snapshot(), later_changes(), after=1_000)
        blue = dict(green)
        blue['c1'] = ChunkRow(3, False, ACL)
        blue['c2'] = ChunkRow(1, False, ACL)
        del blue['c4']
        blue['c5'] = ChunkRow(1, False, ACL)
        self.assertEqual(parity(blue, green), (
            'acl:c1', 'extra:c4', 'missing:c5', 'tombstone:c2', 'version:c1'))
        stale, _ = apply_changes(snapshot(), later_changes()[:2], after=1_000)
        self.assertEqual(parity(green, stale), ('acl:c1', 'missing:c4'))
        # the frame example: green skipped an ACL revocation and a deletion
        blue = {'c1': ChunkRow(3, False, REVOKED), 'c2': ChunkRow(2, True, ACL)}
        skipped = {'c1': ChunkRow(3, False, ACL), 'c2': ChunkRow(2, False, ACL)}
        self.assertEqual(parity(blue, skipped), ('acl:c1', 'tombstone:c2'))

    def test_gaps_and_reordering_are_refused(self):
        changes = later_changes()
        for broken in (changes[1:], [changes[1], changes[0]] + changes[2:], changes[:1] * 2):
            with self.subTest(first=broken[0].position):
                with self.assertRaises(ValueError):
                    apply_changes(snapshot(), broken, after=1_000)
        with self.assertRaises(ValueError):
            apply_changes(snapshot(), changes, after=999)

    def test_regression_and_same_version_resurrection_are_refused(self):
        with self.assertRaises(ValueError):
            apply_changes(snapshot(), [Change(1_001, 'c1', ChunkRow(1, True, ACL)),
                                       Change(1_002, 'c1', ChunkRow(1, False, ACL))], after=1_000)
        with self.assertRaises(ValueError):
            apply_changes(snapshot(), [Change(1_001, 'c3', ChunkRow(3, False, ACL))], after=1_000)
        revived, _ = apply_changes(snapshot(), [Change(1_001, 'c3', ChunkRow(5, False, ACL))],
                                   after=1_000)
        self.assertFalse(revived['c3'].deleted)


class StateMachineTests(unittest.TestCase):
    def test_happy_path_switches_and_retires(self):
        green, _ = apply_changes(snapshot(), later_changes(), after=1_000)
        job = Reindex(BLUE, GREEN)
        self.assertEqual((job.state, job.serving), ('planned', BLUE))
        job = job.built(snapshot_position=1_000).caught_up(1_002).caught_up(1_004)
        job = job.verify(blue_rows=dict(green), green_rows=green, blue_position=1_004,
                         report=report(GREEN))
        self.assertEqual((job.state, job.serving), ('verified', BLUE))
        job = job.switch(now=NOW, rollback_window=WEEK, blue_position=1_010, green_position=1_010)
        self.assertEqual((job.state, job.serving, job.green_position), ('switched', GREEN, 1_010))
        self.assertEqual(job.rollback_until, NOW + WEEK)
        job = job.retire(now=NOW + WEEK)
        self.assertEqual((job.state, job.serving), ('retired', GREEN))

    def test_illegal_transitions_raise(self):
        planned = Reindex(BLUE, GREEN)
        with self.assertRaisesRegex(ValueError, 'planned -> switched'):
            planned.switch(now=NOW, rollback_window=WEEK, blue_position=1, green_position=1)
        with self.assertRaisesRegex(ValueError, 'built -> verified'):
            planned.built(snapshot_position=1_000).verify(
                blue_rows={}, green_rows={}, blue_position=1_000, report=report(GREEN))
        with self.assertRaisesRegex(ValueError, 'planned -> retired'):
            planned.retire(now=NOW)
        aborted = caught_up_job().abort()
        with self.assertRaisesRegex(ValueError, 'aborted -> built'):
            aborted.built(snapshot_position=1)
        with self.assertRaises(ValueError):
            caught_up_job().caught_up(1_003)

    def test_verify_needs_same_position_parity_bound_report_and_pass(self):
        green, _ = apply_changes(snapshot(), later_changes(), after=1_000)
        job = caught_up_job()
        good = dict(blue_rows=dict(green), green_rows=green, blue_position=1_004,
                    report=report(GREEN))
        self.assertTrue(evaluate(report(BLUE))['pass'])     # a real pass, but for blue
        cases = {'same source position': dict(blue_position=1_005),
                 'parity failed: acl:c1, missing:c4, tombstone:c2, version:c1':
                     dict(green_rows=snapshot()),
                 'not bound': dict(report=report(BLUE)),
                 'not bound ': dict(report={'pass': True}),
                 'did not pass': dict(report=report(GREEN, leaked=True)),
                 'missing dataset': dict(report={'artifact_digest': GREEN}),
                 'empty evaluation': dict(report={**report(GREEN), 'rows': []})}
        for message, override in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message.strip()):
                    job.verify(**{**good, **override})
        self.assertEqual(job.verify(**good).state, 'verified')

    def test_switch_needs_a_barrier_and_aware_time(self):
        green, _ = apply_changes(snapshot(), later_changes(), after=1_000)
        verified = caught_up_job().verify(blue_rows=dict(green), green_rows=green,
                                          blue_position=1_004, report=report(GREEN))
        with self.assertRaises(ValueError):
            verified.switch(now=NOW, rollback_window=WEEK, blue_position=1_010,
                            green_position=1_009)
        with self.assertRaises(ValueError):
            verified.switch(now=NOW.replace(tzinfo=None), rollback_window=WEEK,
                            blue_position=1_004, green_position=1_004)
        for window in (timedelta(0), 7, None):
            with self.subTest(window=window):
                with self.assertRaises(ValueError):
                    verified.switch(now=NOW, rollback_window=window, blue_position=1_004,
                                    green_position=1_004)

    def test_rollback_only_inside_window_and_only_if_blue_is_current(self):
        switched = Reindex(BLUE, GREEN, 'verified', 1_000, 1_004).switch(
            now=NOW, rollback_window=WEEK, blue_position=1_004, green_position=1_004)
        with self.assertRaisesRegex(ValueError, 'blue is behind'):
            switched.rollback(now=NOW + timedelta(days=1), blue_position=1_004,
                              green_position=1_020)
        with self.assertRaisesRegex(ValueError, 'window closed'):
            switched.rollback(now=NOW + WEEK, blue_position=1_020, green_position=1_020)
        with self.assertRaisesRegex(ValueError, 'still open'):
            switched.retire(now=NOW + WEEK - timedelta(seconds=1))
        rolled = switched.rollback(now=NOW + timedelta(days=1), blue_position=1_020,
                                   green_position=1_020)
        self.assertEqual((rolled.state, rolled.serving), ('rolled_back', BLUE))
        with self.assertRaises(ValueError):
            switched.abort()


class ValidationTests(unittest.TestCase):
    def test_values_fail_closed(self):
        for args in ((0, False, ACL), (1, 0, ACL), (True, False, ACL), (1, False, 'sha256:x')):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    ChunkRow(*args)
        with self.assertRaises(ValueError):
            Change(-1, 'c1', ChunkRow(1, False, ACL))
        with self.assertRaises(ValueError):
            Change(1, 'bad id', ChunkRow(1, False, ACL))
        with self.assertRaises(TypeError):
            Change(1, 'c1', (1, False, ACL))
        with self.assertRaises(TypeError):
            parity({'c1': (1, False, ACL)}, {})
        for blue, green in ((BLUE, BLUE), ('index-v1', GREEN), (BLUE, None)):
            with self.subTest(blue=blue, green=green):
                with self.assertRaises(ValueError):
                    Reindex(blue, green)

    def test_restored_state_must_be_consistent(self):
        for args in (('switched', 1_000, 1_004), ('verified', None, None),
                     ('planned', 1_000, 1_000), ('caught_up', 1_000, 999),
                     ('built', 1_000, True)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    Reindex(BLUE, GREEN, *args)
        with self.assertRaises(ValueError):
            Reindex(BLUE, GREEN, 'verified', 1_000, 1_004, NOW)
        with self.assertRaises(ValueError):
            Reindex(BLUE, GREEN, 'switched', 1_000, 1_004, NOW.replace(tzinfo=None))
        restored = Reindex(BLUE, GREEN, 'switched', 1_000, 1_004, NOW + WEEK)
        self.assertEqual(restored.serving, GREEN)
        self.assertEqual(Reindex(BLUE, GREEN).abort().state, 'aborted')

    def test_values_are_frozen(self):
        job = Reindex(BLUE, GREEN)
        with self.assertRaises(FrozenInstanceError):
            job.state = 'switched'
        with self.assertRaises(FrozenInstanceError):
            ChunkRow(1, False, ACL).deleted = True


if __name__ == '__main__':
    unittest.main()
