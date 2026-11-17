from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime
import unittest

from fde.decisions import FINDINGS, STATUSES, Decision, Raci, Stakeholder, review

DAY = date(2026, 11, 16)


def people(**vacant):
    roles = ('plant_manager', 'maintenance_lead', 'data_owner_erp', 'data_owner_cmms',
             'data_owner_manuals', 'security_officer', 'it_ops', 'sponsor', 'fde')
    return tuple(Stakeholder(role, None if vacant.get(role) else f'{role}-alias') for role in roles)


def matrix(**overrides):
    rows = {
        'scope': (('maintenance_lead', 'A'), ('fde', 'R'), ('plant_manager', 'C'),
                  ('sponsor', 'I')),
        'source_cmms': (('data_owner_cmms', 'A'), ('fde', 'R'), ('it_ops', 'C')),
        'acl': (('security_officer', 'A'), ('it_ops', 'R'), ('fde', 'R')),
        'release': (('it_ops', 'A'), ('fde', 'R'), ('maintenance_lead', 'C')),
    }
    rows.update(overrides)
    return tuple(Raci(name, assignments) for name, assignments in rows.items())


def accepted(identifier='DR-001', **updates):
    args = dict(id=identifier, status='accepted', decided_on=DAY, workstream='scope',
                owner='maintenance_lead', approved_by=('maintenance_lead',))
    args.update(updates)
    return Decision(**args)


class DecisionLogTests(unittest.TestCase):
    def test_consistent_map_matrix_and_log_has_no_findings(self):
        log = (accepted(status='superseded'),
               accepted('DR-002', decided_on=date(2026, 11, 20), supersedes='DR-001'),
               accepted('DR-003', status='proposed', workstream='acl', owner='security_officer',
                        approved_by=()),
               accepted('DR-004', status='rejected', reason='write tools wait for 2-5 approval'))
        self.assertEqual(review(people(), matrix(), log), [])
        self.assertEqual(review(people(), matrix(), ()), [])

    def test_frame_example_exact_findings(self):
        cast = tuple(Stakeholder(role, who) for role, who in (
            ('plant_manager', 'busan-plant'), ('maintenance_lead', 'busan-maint'),
            ('data_owner_cmms', None), ('security_officer', 'sec-team'),
            ('it_ops', 'ops-team'), ('sponsor', 'cio-office'), ('fde', 'fde-1')))
        rows = (Raci('scope', (('maintenance_lead', 'A'), ('fde', 'R'), ('sponsor', 'I'))),
                Raci('source_cmms', (('data_owner_cmms', 'A'), ('fde', 'R'), ('it_ops', 'C'))),
                Raci('acl', (('security_officer', 'A'), ('it_ops', 'A'), ('fde', 'R'))))
        log = (
            Decision('DR-001', 'superseded', date(2026, 11, 16), 'scope', 'maintenance_lead',
                     approved_by=('maintenance_lead',)),
            Decision('DR-002', 'accepted', date(2026, 11, 20), 'scope', 'fde', supersedes='DR-001'),
            Decision('DR-003', 'proposed', date(2026, 11, 20), 'acl', None, supersedes='DR-009'),
        )
        self.assertEqual(review(cast, rows, log), [
            ('dangling_supersedes', 'DR-003'), ('missing_owner', 'DR-003'),
            ('multiple_accountable', 'acl'), ('unsigned_decision', 'DR-002'),
            ('vacant_accountable', 'source_cmms')])

    def test_exactly_one_accountable_and_at_least_one_responsible(self):
        rows = matrix(acl=(('security_officer', 'A'), ('it_ops', 'A'), ('fde', 'R')),
                      release=(('it_ops', 'R'), ('fde', 'R')),
                      scope=(('maintenance_lead', 'A'), ('fde', 'C')))
        self.assertEqual(review(people(), rows, ()), [
            ('multiple_accountable', 'acl'), ('no_accountable', 'release'),
            ('no_responsible', 'scope')])

    def test_vacant_accountable_blocks_but_vacant_consulted_does_not(self):
        self.assertEqual(review(people(data_owner_cmms=True), matrix(), ()),
                         [('vacant_accountable', 'source_cmms')])
        self.assertEqual(review(people(plant_manager=True), matrix(), ()), [])

    def test_unknown_roles_in_matrix_owner_and_approvers(self):
        rows = matrix(scope=(('maintenance_lead', 'A'), ('fde', 'R'), ('qa_lead', 'C')))
        log = (accepted(owner='vendor_pm', approved_by=('maintenance_lead', 'auditor')),)
        self.assertEqual(review(people(), rows, log), [
            ('unknown_role', 'auditor'), ('unknown_role', 'qa_lead'),
            ('unknown_role', 'vendor_pm')])

    def test_owner_workstream_and_accountable_signature(self):
        log = (accepted(owner=None),
               accepted('DR-002', workstream='budget'),
               accepted('DR-003', workstream='acl', owner='fde', approved_by=('fde',)),
               accepted('DR-004', status='superseded', workstream='release', approved_by=()),
               accepted('DR-005', status='accepted', workstream='release', supersedes='DR-004',
                        owner='it_ops', approved_by=('it_ops',)))
        self.assertEqual(review(people(), matrix(), log), [
            ('missing_owner', 'DR-001'), ('unknown_workstream', 'DR-002'),
            ('unsigned_decision', 'DR-003'), ('unsigned_decision', 'DR-004')])
        proposed = (accepted(status='proposed', approved_by=()),)
        self.assertEqual(review(people(), matrix(), proposed), [])

    def test_unsigned_check_waits_for_a_single_accountable(self):
        rows = matrix(scope=(('maintenance_lead', 'A'), ('sponsor', 'A'), ('fde', 'R')))
        self.assertEqual(review(people(), rows, (accepted(approved_by=()),)),
                         [('multiple_accountable', 'scope')])

    def test_rejection_needs_a_reason(self):
        for reason in ('', '   '):
            with self.subTest(reason=reason):
                log = (accepted(status='rejected', reason=reason),)
                self.assertEqual(review(people(), matrix(), log),
                                 [('rejected_without_reason', 'DR-001')])

    def test_supersedes_chain_must_point_to_an_existing_record(self):
        log = (accepted(supersedes='DR-009'),)
        self.assertEqual(review(people(), matrix(), log), [('dangling_supersedes', 'DR-001')])

    def test_supersedes_cycles_are_reported_for_every_member(self):
        loop = (accepted(status='superseded', supersedes='DR-002'),
                accepted('DR-002', status='superseded', supersedes='DR-001'))
        self.assertEqual(review(people(), matrix(), loop), [
            ('supersedes_cycle', 'DR-001'), ('supersedes_cycle', 'DR-002')])
        self_loop = (accepted(supersedes='DR-001'),)
        self.assertEqual(review(people(), matrix(), self_loop), [
            ('not_marked_superseded', 'DR-001'), ('supersedes_cycle', 'DR-001')])
        tail = (accepted(status='superseded', supersedes='DR-002'),
                accepted('DR-002', status='superseded', supersedes='DR-003'),
                accepted('DR-003', status='superseded', supersedes='DR-002'))
        self.assertIn(('supersedes_cycle', 'DR-002'), review(people(), matrix(), tail))
        self.assertNotIn(('supersedes_cycle', 'DR-001'), review(people(), matrix(), tail))

    def test_superseded_status_and_successors_must_agree(self):
        orphan = (accepted(status='superseded'),)
        self.assertEqual(review(people(), matrix(), orphan),
                         [('superseded_without_successor', 'DR-001')])
        unmarked = (accepted(), accepted('DR-002', supersedes='DR-001'))
        self.assertEqual(review(people(), matrix(), unmarked),
                         [('not_marked_superseded', 'DR-001')])
        fork = (accepted(status='superseded'), accepted('DR-002', supersedes='DR-001'),
                accepted('DR-003', supersedes='DR-001'))
        self.assertEqual(review(people(), matrix(), fork), [('competing_successors', 'DR-001')])
        pending = (accepted(), accepted('DR-002', status='proposed', supersedes='DR-001'))
        self.assertEqual(review(people(), matrix(), pending), [])
        refused = (accepted(status='superseded'),
                   accepted('DR-002', status='rejected', reason='x', supersedes='DR-001'))
        self.assertEqual(review(people(), matrix(), refused),
                         [('superseded_without_successor', 'DR-001')])

    def test_longer_chains_count_superseded_successors(self):
        # DR-001 <- DR-002 <- DR-003: DR-002 was accepted before DR-003 replaced it.
        chain = (accepted(status='superseded'),
                 accepted('DR-002', status='superseded', supersedes='DR-001'),
                 accepted('DR-003', supersedes='DR-002'))
        self.assertEqual(review(people(), matrix(), chain), [])
        unmarked = (accepted(),) + chain[1:]
        self.assertEqual(review(people(), matrix(), unmarked),
                         [('not_marked_superseded', 'DR-001')])
        fork = chain + (accepted('DR-004', status='superseded', supersedes='DR-001'),)
        self.assertEqual(review(people(), matrix(), fork), [
            ('competing_successors', 'DR-001'), ('superseded_without_successor', 'DR-004')])

    def test_record_shapes_fail_closed(self):
        for updates in ({'id': 'ADR-1'}, {'id': 'DR-1'}, {'id': 'dr-001'}, {'status': 'deprecated'},
                        {'status': None}, {'decided_on': '2026-11-16'},
                        {'decided_on': datetime(2026, 11, 16)}, {'workstream': 'Scope'},
                        {'owner': ''}, {'approved_by': ['maintenance_lead']},
                        {'approved_by': ('fde', 'fde')}, {'supersedes': 'DR-1'},
                        {'reason': None}, {'reason': 'x' * 501}):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    accepted(**updates)
        for assignments in ((), [('fde', 'R')], (('fde', 'X'),), (('fde', 'R'), ('fde', 'A')),
                            (('fde',),), (('Fde', 'R'),), (('fde', ['R']),)):
            with self.subTest(assignments=assignments):
                with self.assertRaises(ValueError):
                    Raci('scope', assignments)
        for role, assignee in (('', 'x'), ('plant manager', 'x'), ('fde', ''), ('fde', 'a b')):
            with self.subTest(role=role, assignee=assignee):
                with self.assertRaises(ValueError):
                    Stakeholder(role, assignee)

    def test_collections_are_typed_unique_and_ids_never_reused(self):
        with self.assertRaises(ValueError):
            review(people(), matrix(), (accepted(), accepted(status='rejected', reason='x')))
        with self.assertRaises(ValueError):
            review(people() + people()[:1], matrix(), ())
        with self.assertRaises(ValueError):
            review(people(), matrix() + matrix()[:1], ())
        for arguments in (((), matrix(), ()), (people(), (), ()), (list(people()), matrix(), ()),
                          (people(), matrix(), [accepted()]), (people(), matrix(), ('DR-001',))):
            with self.subTest(arguments=[type(a).__name__ for a in arguments]):
                with self.assertRaises(ValueError):
                    review(*arguments)

    def test_vocabularies_and_immutability(self):
        self.assertEqual(STATUSES, ('proposed', 'accepted', 'rejected', 'superseded'))
        self.assertEqual(len(FINDINGS), 14)
        record = accepted()
        with self.assertRaises(FrozenInstanceError):
            record.status = 'rejected'
        self.assertEqual(replace(record, status='superseded').status, 'superseded')
        self.assertEqual(record.status, 'accepted')


if __name__ == '__main__':
    unittest.main()
