from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import unittest

import psycopg
from psycopg.conninfo import make_conninfo

from fde.ingestion import Event, apply_event
from fde.retrieval import Principal, retrieve, revalidate_evidence


@unittest.skipUnless(os.getenv('FDE_TEST_DSN'), 'run python scripts/check.py --all for an isolated PostgreSQL')
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.dsn = os.environ['FDE_TEST_DSN']
        self.admin = psycopg.connect(self.dsn, autocommit=True)
        self.reader = psycopg.connect(make_conninfo(self.dsn, user='fde_test_reader'), autocommit=True)
        self.writer = psycopg.connect(make_conninfo(self.dsn, user='fde_test_ingest'), autocommit=True)
        self.admin.execute('TRUNCATE fde.identity_map,fde.receipts,fde.assets,fde.audit_outbox,fde.memberships,fde.chunks')
        self.admin.execute("INSERT INTO fde.identity_map VALUES ('acme','erp','A1','00000000-0000-0000-0000-000000000001',true)")
        self.admin.execute("INSERT INTO fde.memberships VALUES ('acme','alice',ARRAY['engineer']),('beta','bob',ARRAY['manager'])")
        for tenant, key, roles, content in [('acme', 'manual', ['engineer'], 'Stop the pump before inspection.'),
                                             ('acme', 'payroll', ['hr'], 'Private payroll information'),
                                             ('beta', 'secret', ['manager'], 'Beta proprietary pump secret')]:
            self.admin.execute('INSERT INTO fde.chunks VALUES (%s,%s,1,%s,%s,%s,%s,false)',
                               (tenant, key, content, [1., 0.], 'fixture-v1', roles))
        self.event = Event('acme', 'erp', 'A1', 1, 10, 0, 2, 'upsert', {'name': 'Pump A1', 'status': 'running'})

    def tearDown(self):
        self.reader.close()
        self.writer.close()
        self.admin.close()

    def test_duplicate_does_not_double_apply(self):
        self.assertEqual(apply_event(self.writer, self.event), 'applied')
        self.assertEqual(apply_event(self.writer, self.event), 'duplicate')
        self.assertEqual(self.admin.execute('SELECT count(*) FROM fde.audit_outbox').fetchone()[0], 1)

    def test_stale_update_cannot_resurrect_tombstone(self):
        apply_event(self.writer, replace(self.event, offset=20, op='delete', payload={}))
        self.assertEqual(apply_event(self.writer, self.event), 'stale')
        self.assertTrue(self.admin.execute('SELECT deleted FROM fde.assets').fetchone()[0])

    def test_changed_payload_at_same_position_fails(self):
        apply_event(self.writer, self.event)
        with self.assertRaises(ValueError):
            apply_event(self.writer, replace(self.event, payload={'name': 'different', 'status': 'stopped'}))
        self.assertEqual(self.admin.execute('SELECT status FROM fde.assets').fetchone()[0], 'running')

    def test_transaction_rolls_back_if_audit_outbox_fails(self):
        self.admin.execute('''CREATE FUNCTION fde.test_fail() RETURNS trigger LANGUAGE plpgsql AS
                           $$ BEGIN RAISE EXCEPTION 'injected audit failure'; END $$''')
        self.admin.execute('CREATE TRIGGER test_fail BEFORE INSERT ON fde.audit_outbox FOR EACH ROW EXECUTE FUNCTION fde.test_fail()')
        try:
            with self.assertRaises(psycopg.Error):
                apply_event(self.writer, self.event)
            for table in ('assets', 'receipts', 'audit_outbox'):
                self.assertEqual(self.admin.execute('SELECT count(*) FROM fde.' + table).fetchone()[0], 0)
        finally:
            self.admin.execute('DROP TRIGGER test_fail ON fde.audit_outbox')
            self.admin.execute('DROP FUNCTION fde.test_fail()')

    def test_concurrent_redelivery_has_one_effect(self):
        def execute(_):
            with psycopg.connect(make_conninfo(self.dsn, user='fde_test_ingest'), autocommit=True) as connection:
                return apply_event(connection, self.event)
        with ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sorted(workers.map(execute, range(2))), ['applied', 'duplicate'])

    def test_unmapped_identity_is_not_silently_created(self):
        with self.assertRaises(ValueError):
            apply_event(self.writer, replace(self.event, source_key='unknown'))
        self.assertEqual(self.admin.execute('SELECT count(*) FROM fde.receipts').fetchone()[0], 0)

    def test_reader_has_no_owner_superuser_or_bypass_privilege(self):
        privileges = self.reader.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
        self.assertEqual(privileges, (False, False))
        self.assertEqual(self.reader.execute('SELECT count(*) FROM fde.chunks').fetchone()[0], 0)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.reader.execute("UPDATE fde.memberships SET roles=ARRAY['hr']")

    def test_rls_applies_before_python_ranking(self):
        result = retrieve(self.reader, Principal('acme', 'alice'), 'pump', (1., 0.), 'fixture-v1')
        self.assertEqual([c.id for c in result], ['manual'])
        self.assertEqual(retrieve(self.reader, Principal('beta', 'alice'), 'pump', (1., 0.), 'fixture-v1'), [])
        self.assertEqual(retrieve(self.reader, Principal('acme', 'nobody'), 'pump', (1., 0.), 'fixture-v1'), [])

    def test_identity_does_not_leak_between_pooled_requests(self):
        retrieve(self.reader, Principal('acme', 'alice'), 'pump', (1., 0.), 'fixture-v1')
        self.assertEqual(self.reader.execute('SELECT count(*) FROM fde.chunks').fetchone()[0], 0)

    def test_revocation_and_document_delete_affect_next_request(self):
        principal = Principal('acme', 'alice')
        self.assertEqual(len(retrieve(self.reader, principal, 'pump', (1., 0.), 'fixture-v1')), 1)
        self.admin.execute("UPDATE fde.memberships SET roles=ARRAY['visitor'] WHERE subject='alice'")
        self.assertEqual(retrieve(self.reader, principal, 'pump', (1., 0.), 'fixture-v1'), [])
        self.admin.execute("UPDATE fde.memberships SET roles=ARRAY['engineer'] WHERE subject='alice'")
        self.admin.execute("UPDATE fde.chunks SET deleted=true WHERE tenant_id='acme'")
        self.assertEqual(retrieve(self.reader, principal, 'pump', (1., 0.), 'fixture-v1'), [])

    def test_nested_transaction_cannot_ack_before_outer_commit(self):
        with self.writer.transaction():
            with self.assertRaises(ValueError):
                apply_event(self.writer, self.event)

    def test_output_revalidation_rejects_revocation_during_generation(self):
        principal = Principal('acme', 'alice')
        evidence = retrieve(self.reader, principal, 'pump', (1., 0.), 'fixture-v1')
        revalidate_evidence(self.reader, principal, evidence, 'fixture-v1')
        self.admin.execute("DELETE FROM fde.memberships WHERE subject='alice'")
        with self.assertRaises(PermissionError):
            revalidate_evidence(self.reader, principal, evidence, 'fixture-v1')

    def test_output_revalidation_rejects_changed_document(self):
        principal = Principal('acme', 'alice')
        evidence = retrieve(self.reader, principal, 'pump', (1., 0.), 'fixture-v1')
        self.admin.execute("UPDATE fde.chunks SET document_version=2 WHERE chunk_id='manual'")
        with self.assertRaises(PermissionError):
            revalidate_evidence(self.reader, principal, evidence, 'fixture-v1')

    def test_corpus_limit_fails_instead_of_silent_truncation(self):
        self.admin.execute("INSERT INTO fde.chunks SELECT tenant_id,'manual2',document_version,content,embedding,embedding_model,allowed_roles,deleted FROM fde.chunks WHERE chunk_id='manual'")
        with self.assertRaises(ValueError):
            retrieve(self.reader, Principal('acme', 'alice'), 'pump', (1., 0.), 'fixture-v1', cap=1)
