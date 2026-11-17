from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import re
import threading
import time
import unittest

from fde.actions import (APPROVER_ROLE, Approval, IdempotencyConflict, Result, StaleVersion,
                         WorkOrderStore, approve, draft_work_order)
from fde.retrieval import Chunk, Principal

T0 = datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc)
KEY = '6f1c2a9e-0d4b-4c1e-9a57-3b2f8e1d4c60'
STEP = '1. 펌프를 정지하고 잠금 표지를 단다.'
EVIDENCE = [Chunk('manual-p101:v3:0002', 3, STEP, (1.0, 0.0))]
ALICE = Principal('acme', 'alice')


def make_draft(principal=ALICE, **updates):
    arguments = dict(asset_id='P-101', asset_version=7, summary='P-101 반복 과열 점검',
                     steps=(STEP,), citations=(('manual-p101:v3:0002', 3),), evidence=EVIDENCE)
    arguments.update(updates)
    return draft_work_order(principal, **arguments)


class SlowStore(WorkOrderStore):
    """Widens the check-then-write window: without the lock both threads would write."""

    def _write(self, draft, current):
        time.sleep(0.02)
        return super()._write(draft, current)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.draft = make_draft()
        self.approval = approve(self.draft, approver='park', roles={APPROVER_ROLE}, now=T0)
        self.store = WorkOrderStore('acme', {'P-101': 7})

    def run_once(self, key=KEY, version=7, minutes=2, approval=None, draft=None):
        return self.store.execute(draft or self.draft, approval or self.approval, key,
                                  expected_version=version, now=T0 + timedelta(minutes=minutes))

    def test_executes_once_and_replays_the_stored_result(self):
        first = self.run_once()
        again = self.run_once(minutes=3)
        self.assertEqual(first, Result('WO-000001', 'P-101', 8, replayed=False))
        self.assertEqual(again, Result('WO-000001', 'P-101', 8, replayed=True))
        self.assertEqual((self.store.writes, self.store.version('P-101')), (1, 8))

    def test_same_key_with_a_different_payload_is_a_conflict(self):
        self.run_once()
        other = make_draft(summary='P-101 냉각수 점검')
        for attempt in (dict(version=8), dict(draft=other)):
            with self.assertRaises(IdempotencyConflict):
                self.run_once(minutes=4, **attempt)
        self.assertTrue(issubclass(IdempotencyConflict, ValueError))
        self.assertEqual(self.store.writes, 1)

    def test_changed_asset_needs_a_new_draft_and_approval(self):
        self.store = WorkOrderStore('acme', {'P-101': 9})        # changed after approval
        with self.assertRaisesRegex(StaleVersion, 'expected version 7, current 9'):
            self.run_once()
        with self.assertRaisesRegex(ValueError, 'differs from the approved draft'):
            self.run_once(version=9)                              # rejection was not stored
        fresh = make_draft(asset_version=9)
        approval = approve(fresh, approver='park', roles={APPROVER_ROLE}, now=T0)
        self.assertEqual(self.run_once(version=9, draft=fresh, approval=approval).version, 10)
        self.assertEqual(self.store.writes, 1)

    def test_approval_rules_fail_closed(self):
        with self.assertRaises(PermissionError):
            approve(self.draft, approver='alice', roles={APPROVER_ROLE}, now=T0)
        with self.assertRaises(PermissionError):
            approve(self.draft, approver='park', roles={'engineer'}, now=T0)
        forged = [replace(self.approval, approver='alice'),
                  replace(self.approval, roles=frozenset({'engineer'})),
                  replace(self.approval, draft_digest=make_draft(steps=('펌프 계속 가동',)).digest)]
        for approval in forged:
            with self.assertRaises(PermissionError):
                self.run_once(approval=approval)
        with self.assertRaises(PermissionError):
            self.run_once(minutes=30)                              # expires_at is exclusive
        with self.assertRaises(PermissionError):
            self.run_once(minutes=-1)                              # before it was granted
        with self.assertRaises(PermissionError):
            WorkOrderStore('beta', {'P-101': 7}).execute(
                self.draft, self.approval, KEY, 7, T0 + timedelta(minutes=2))
        self.assertEqual(self.approval.expires_at - self.approval.approved_at, timedelta(minutes=30))
        self.assertEqual(self.store.writes, 0)

    def test_edited_draft_is_not_covered_by_the_approval(self):
        edited = replace(self.draft, steps=(STEP, '2. 경보를 무시하고 재가동한다.'))
        self.assertNotEqual(edited.digest, self.draft.digest)
        with self.assertRaisesRegex(PermissionError, 'does not cover'):
            self.run_once(draft=edited)
        self.assertRegex(self.draft.digest, r'^sha256:[0-9a-f]{64}$')
        self.assertEqual(make_draft().digest, self.draft.digest)

    def test_draft_needs_retrieved_current_citations(self):
        for citations in ((('manual-p101:v3:0002', 2),), (('secret-hr', 1),), ()):
            with self.assertRaises(ValueError):
                make_draft(citations=citations)
        with self.assertRaises(TypeError):
            make_draft(evidence=[('manual-p101:v3:0002', 3)])
        with self.assertRaises(FrozenInstanceError):
            self.draft.summary = 'changed'

    def test_requester_and_tenant_come_from_the_verified_principal(self):
        self.assertEqual((self.draft.tenant, self.draft.requested_by), ('acme', 'alice'))
        with self.assertRaises(TypeError):
            make_draft(requested_by='mallory')          # not a model-settable argument
        with self.assertRaises(TypeError):
            make_draft(principal={'tenant': 'acme', 'subject': 'park'})
        with self.assertRaises(PermissionError):         # alice cannot approve her own draft
            approve(self.draft, approver=ALICE.subject, roles={APPROVER_ROLE}, now=T0)

    def test_approval_is_single_use_even_with_a_new_key(self):
        self.run_once()
        with self.assertRaisesRegex(ValueError, 'already executed'):
            self.run_once(key='a' * 16)                            # e.g. after a key purge
        self.assertEqual(self.store.writes, 1)

    def test_replay_after_approval_expiry_returns_without_writing(self):
        self.run_once()
        replay = self.run_once(minutes=120)
        self.assertEqual((replay.work_order_id, replay.replayed), ('WO-000001', True))
        self.assertEqual(self.store.writes, 1)

    def test_two_threads_same_key_write_once(self):
        for _ in range(20):
            store = SlowStore('acme', {'P-101': 7})
            barrier, results = threading.Barrier(2), []

            def worker():
                barrier.wait()
                results.append(store.execute(self.draft, self.approval, KEY, 7,
                                             T0 + timedelta(minutes=2)))
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sorted(r.replayed for r in results), [False, True])
            self.assertEqual({r.work_order_id for r in results}, {'WO-000001'})
            self.assertEqual(store.writes, 1)

    def test_two_threads_different_keys_one_wins(self):
        self.store = SlowStore('acme', {'P-101': 7})
        barrier, outcomes = threading.Barrier(2), []

        def worker(key):
            barrier.wait()
            try:
                outcomes.append(self.run_once(key=key).work_order_id)
            except ValueError as error:
                outcomes.append(type(error).__name__)
        threads = [threading.Thread(target=worker, args=(k * 16,)) for k in 'ab']
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ['ValueError', 'WO-000001'])
        self.assertEqual(self.store.writes, 1)

    def test_inputs_are_validated(self):
        naive = datetime(2026, 11, 17, 9, 0)
        bad = [lambda: self.run_once(key='k' * 15), lambda: self.run_once(key='x' * 15 + ' '),
               lambda: self.run_once(key='k' * 256), lambda: self.run_once(version=0),
               lambda: self.store.execute(self.draft, self.approval, KEY, 7, naive),
               lambda: approve(self.draft, approver='park', roles={APPROVER_ROLE}, now=T0,
                               ttl=timedelta(hours=9)),
               lambda: make_draft(asset_id='p101'), lambda: make_draft(steps=()),
               lambda: replace(self.approval, expires_at=T0),
               lambda: WorkOrderStore('acme', {'P-101': 0})]
        for call in bad:
            with self.assertRaises(ValueError):
                call()
        unknown = make_draft(asset_id='P-102')
        approval = approve(unknown, approver='park', roles={APPROVER_ROLE}, now=T0)
        with self.assertRaisesRegex(ValueError, 'unknown asset'):
            self.run_once(draft=unknown, approval=approval)
        self.assertTrue(re.fullmatch(r'[A-Za-z0-9_-]{16,255}', KEY))
        self.assertIsInstance(self.approval, Approval)


if __name__ == '__main__':
    unittest.main()
