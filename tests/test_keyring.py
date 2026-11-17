from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import hmac
import unittest

from fde.keyring import Keyring, KeyVersion, plan_rotation
from fde.privacy import audit_record

DAY = timedelta(days=1)


def at(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


def pseudonym(key, subject='engineer-17'):
    return audit_record(key=key.material, key_id=key.key_id, tenant='busan', subject=subject,
                        request_id='00000000-0000-0000-0000-000000000002', decision='allow',
                        artifact_digest='sha256:' + 'a' * 64)


class KeyringTests(unittest.TestCase):
    def setUp(self):
        # Proposals, not standards: 365-day cryptoperiod, 365-day retention, 14-day lead.
        self.steps = plan_rotation('pii-hmac', start=at(2026, 12, 1), count=2,
                                   cryptoperiod=365 * DAY, retention=365 * DAY, lead=14 * DAY)
        self.ring = Keyring(tuple(
            KeyVersion('pseudonym', s.key_id, s.activates_at, s.retires_at, s.verify_until,
                       material=bytes([index]) * 32)
            for index, s in enumerate(self.steps, 1)))

    def test_rotation_plan_exact_dates(self):
        self.assertEqual([(s.key_id, s.distribute_by, s.activates_at, s.retires_at, s.verify_until)
                          for s in self.steps], [
            ('pii-hmac-v1', at(2026, 11, 17), at(2026, 12, 1), at(2027, 12, 1), at(2028, 11, 30)),
            ('pii-hmac-v2', at(2027, 11, 17), at(2027, 12, 1), at(2028, 11, 30), at(2029, 11, 30))])
        # The first record under a key stays linkable for cryptoperiod + retention.
        self.assertEqual(self.steps[0].verify_until - self.steps[0].activates_at, 730 * DAY)
        for kwargs in ({'lead': 0 * DAY}, {'lead': 365 * DAY}, {'retention': -DAY},
                       {'count': 0}, {'cryptoperiod': 365}, {'start': datetime(2026, 12, 1)}):
            args = dict(start=at(2026, 12, 1), count=2, cryptoperiod=365 * DAY,
                        retention=365 * DAY, lead=14 * DAY)
            args.update(kwargs)
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    plan_rotation('pii-hmac', **args)
        with self.assertRaises(ValueError):
            plan_rotation('x' * 38, start=at(2026, 12, 1), count=2, cryptoperiod=365 * DAY,
                          retention=0 * DAY, lead=DAY)

    def test_exactly_one_active_key_per_purpose(self):
        for moment, expected in ((at(2026, 12, 1), 'pii-hmac-v1'), (at(2027, 11, 30, 23, 59), 'pii-hmac-v1'),
                                 (at(2027, 12, 1), 'pii-hmac-v2'), (at(2028, 11, 29), 'pii-hmac-v2')):
            with self.subTest(moment=moment):
                self.assertEqual(self.ring.active_key('pseudonym', moment).key_id, expected)
        first, second = self.ring.versions
        overlap = replace(second, activates_at=second.activates_at - DAY)
        gap = replace(second, activates_at=second.activates_at + DAY)
        for broken in (overlap, gap):
            with self.assertRaises(ValueError):
                Keyring((first, broken))
        with self.assertRaises(ValueError):
            Keyring((first, replace(second, key_id=first.key_id)))
        with self.assertRaises(ValueError):
            Keyring([first])

    def test_states_follow_originator_and_recipient_periods(self):
        first = self.ring.versions[0]
        self.assertEqual([first.state(moment) for moment in (
            at(2026, 11, 30), at(2026, 12, 1), at(2027, 12, 1), at(2028, 11, 29), at(2028, 11, 30))],
            ['pending', 'active', 'verify-only', 'verify-only', 'retired'])

    def test_new_event_records_active_key_id_and_old_record_rederives(self):
        event = pseudonym(self.ring.active_key('pseudonym', at(2027, 3, 2)))
        self.assertEqual(event['key_id'], 'pii-hmac-v1')
        old = self.ring.key_for_record('pseudonym', event['key_id'], at(2027, 3, 2), now=at(2028, 2, 1))
        self.assertTrue(hmac.compare_digest(pseudonym(old)['actor_hash'], event['actor_hash']))
        newer = self.ring.active_key('pseudonym', at(2028, 2, 1))
        self.assertEqual(newer.key_id, 'pii-hmac-v2')
        # The same subject has a different pseudonym under each key version.
        self.assertNotEqual(pseudonym(newer)['actor_hash'], event['actor_hash'])

    def test_record_must_name_the_key_active_at_its_event_time(self):
        now = at(2028, 2, 1)
        for key_id, event_time in (('pii-hmac-v2', at(2027, 3, 2)), ('pii-hmac-v1', at(2028, 1, 5)),
                                   ('pii-hmac-v9', at(2027, 3, 2)), ('pii-hmac-v1', at(2028, 3, 1))):
            with self.subTest(key_id=key_id, event_time=event_time):
                with self.assertRaises(ValueError):
                    self.ring.key_for_record('pseudonym', key_id, event_time, now=now)
        with self.assertRaises(ValueError):
            self.ring.key_for_record('signing', 'pii-hmac-v1', at(2027, 3, 2), now=now)

    def test_retired_or_rotated_key_cannot_protect_new_data(self):
        self.assertEqual(self.ring.require_active('pseudonym', 'pii-hmac-v1', at(2027, 6, 1)).key_id,
                         'pii-hmac-v1')
        for key_id, moment in (('pii-hmac-v1', at(2027, 12, 1)), ('pii-hmac-v1', at(2029, 1, 1)),
                               ('pii-hmac-v2', at(2027, 11, 20))):
            with self.subTest(key_id=key_id, moment=moment):
                with self.assertRaises(PermissionError):
                    self.ring.require_active('pseudonym', key_id, moment)
        with self.assertRaises(PermissionError):
            self.ring.active_key('pseudonym', at(2028, 11, 30))   # plan ended: rotation overdue

    def test_verification_window_ends_at_verify_until(self):
        self.ring.key_for_record('pseudonym', 'pii-hmac-v1', at(2027, 11, 30), now=at(2028, 11, 29))
        with self.assertRaises(PermissionError):
            self.ring.key_for_record('pseudonym', 'pii-hmac-v1', at(2027, 11, 30), now=at(2028, 11, 30))

    def test_destruction_waits_for_retention_and_makes_pseudonyms_unlinkable(self):
        with self.assertRaises(PermissionError):
            self.ring.destroy('pii-hmac-v1', now=at(2028, 6, 1))
        self.assertEqual(self.ring.due_for_destruction(at(2028, 11, 30)), ('pii-hmac-v1',))
        ring = self.ring.destroy('pii-hmac-v1', now=at(2028, 11, 30))
        first = ring.versions[0]
        self.assertEqual((first.state(at(2027, 1, 1)), first.material), ('destroyed', None))
        self.assertEqual(ring.due_for_destruction(at(2028, 11, 30)), ())
        early = Keyring((replace(self.ring.versions[0], material=None, destroyed=True),
                         self.ring.versions[1]))
        with self.assertRaises(PermissionError):
            early.key_for_record('pseudonym', 'pii-hmac-v1', at(2027, 3, 2), now=at(2027, 6, 1))
        with self.assertRaises(PermissionError):
            early.active_key('pseudonym', at(2027, 6, 1))

    def test_key_material_and_identifiers_fail_closed(self):
        base = self.ring.versions[0]
        for updates in ({'key_id': 'x' * 41}, {'key_id': 'key v1'}, {'key_id': 'key/v1'},
                        {'material': b'x' * 31}, {'material': 'x' * 32},
                        {'reference': 'kms://fde/pii-hmac-v1'}, {'material': None},
                        {'purpose': 'encryption'}, {'retires_at': base.activates_at},
                        {'verify_until': base.retires_at - DAY},
                        {'activates_at': datetime(2026, 12, 1)}, {'destroyed': 1}):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    replace(base, **updates)
        self.assertEqual(replace(base, key_id='x' * 40).key_id, 'x' * 40)
        self.assertNotIn(repr(base.material), repr(base))   # repr must not leak key bytes
        with self.assertRaises(FrozenInstanceError):
            base.key_id = 'pii-hmac-v3'

    def test_signing_entries_hold_only_a_verification_key_reference(self):
        common = dict(activates_at=at(2026, 12, 1), retires_at=at(2027, 12, 1),
                      verify_until=at(2029, 12, 1))
        signing = KeyVersion('signing', 'release-2026', reference='file:/etc/fde/release-2026.pub',
                             **common)
        self.assertEqual(signing.state(at(2028, 1, 1)), 'verify-only')
        with self.assertRaises(ValueError):
            KeyVersion('signing', 'release-2026', material=b'k' * 32, **common)
        ring = Keyring((*self.ring.versions, signing))
        self.assertEqual(ring.active_key('signing', at(2027, 1, 1)).key_id, 'release-2026')
        self.assertEqual(ring.active_key('pseudonym', at(2027, 1, 1)).key_id, 'pii-hmac-v1')


if __name__ == '__main__':
    unittest.main()
