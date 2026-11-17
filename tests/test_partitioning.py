import os
from pathlib import Path
import re
import unittest

import psycopg
from psycopg.conninfo import make_conninfo

SQL = (Path(__file__).resolve().parents[1] / 'sql' / 'partitioning.sql').read_text()


@unittest.skipUnless(os.getenv('FDE_TEST_DSN'), 'run python scripts/check.py --all for an isolated PostgreSQL')
class PartitionTests(unittest.TestCase):
    """LIST partitions per factory tenant; RLS on the parent; separate schema fde_part."""

    def setUp(self):
        self.dsn = os.environ['FDE_TEST_DSN']
        self.admin = psycopg.connect(self.dsn, autocommit=True)
        self.reader = psycopg.connect(make_conninfo(self.dsn, user='fde_test_reader'), autocommit=True)
        self.admin.execute('DROP SCHEMA IF EXISTS fde_part CASCADE')
        self.admin.execute(SQL)
        self.admin.execute("INSERT INTO fde_part.memberships VALUES "
                           "('busan','kim',ARRAY['engineer']),('ulsan','lee',ARRAY['engineer']),"
                           "('busan','choi',ARRAY['engineer']),('ulsan','choi',ARRAY['engineer'])")
        for factory, chunk, roles in (('busan', 'p101-overheat', ['engineer']),
                                      ('busan', 'busan-payroll', ['hr']),
                                      ('ulsan', 'ulsan-manual', ['engineer'])):
            self.admin.execute('INSERT INTO fde_part.chunks VALUES (%s,%s,1,%s,%s,%s,%s,false)',
                               (factory, chunk, 'fixture text', [1., 0.], 'fixture-v1', roles))

    def tearDown(self):
        self.reader.close()
        self.admin.execute('DROP SCHEMA IF EXISTS fde_part CASCADE')
        self.admin.close()

    def read(self, sql, tenant='busan', subject='kim'):
        with self.reader.transaction():
            self.reader.execute("SELECT set_config('app.tenant',%s,true), set_config('app.subject',%s,true)",
                                (tenant, subject))
            return self.reader.execute(sql).fetchall()

    def explain(self, sql, tenant='busan', subject='kim'):
        lines = [row[0] for row in self.read('EXPLAIN (COSTS OFF) ' + sql, tenant, subject)]
        scanned = sorted({name for line in lines for name in re.findall(r' on (chunks_\w+) chunks', line)})
        removed = [line.strip() for line in lines if 'Subplans Removed' in line]
        return scanned, removed

    def ids(self, tenant, subject):
        return [row[0] for row in self.read('SELECT chunk_id FROM fde_part.chunks ORDER BY 1',
                                            tenant, subject)]

    def unguarded(self):
        return [row[0] for row in self.admin.execute(
            'SELECT partition::text FROM fde_part.unguarded_partitions ORDER BY 1').fetchall()]

    def test_parent_policy_limits_each_request_to_one_factory(self):
        self.assertEqual(self.ids('busan', 'kim'), ['p101-overheat'])
        self.assertEqual(self.ids('ulsan', 'lee'), ['ulsan-manual'])
        self.assertEqual(self.ids('ulsan', 'kim'), [])
        # A member of two factories still sees one factory per request.
        self.assertEqual(self.ids('busan', 'choi'), ['p101-overheat'])
        self.assertEqual(self.ids('ulsan', 'choi'), ['ulsan-manual'])

    def test_rls_condition_prunes_other_partitions_at_startup(self):
        scanned, removed = self.explain('SELECT chunk_id FROM fde_part.chunks')
        self.assertEqual(scanned, ['chunks_busan'])
        self.assertEqual(removed, ['Subplans Removed: 1'])

    def test_explicit_factory_predicate_prunes_at_plan_time(self):
        scanned, removed = self.explain("SELECT chunk_id FROM fde_part.chunks WHERE factory = 'busan'")
        self.assertEqual((scanned, removed), (['chunks_busan'], []))
        # A predicate for another factory cannot widen the request: RLS still applies.
        self.assertEqual(self.read("SELECT count(*) FROM fde_part.chunks WHERE factory = 'ulsan'"), [(0,)])

    def test_unset_identity_scans_nothing(self):
        scanned, removed = self.explain('SELECT chunk_id FROM fde_part.chunks', None, None)
        self.assertEqual((scanned, removed), ([], ['Subplans Removed: 2']))
        self.assertEqual(self.read('SELECT count(*) FROM fde_part.chunks', None, None), [(0,)])

    def test_direct_partition_read_without_grant_is_denied(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.read('SELECT chunk_id FROM fde_part.chunks_ulsan')
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.read("INSERT INTO fde_part.chunks VALUES ('busan','x',1,'x','{1}','m','{engineer}',false)")

    def test_stray_grant_on_guarded_partition_still_shows_nothing(self):
        self.assertEqual(self.unguarded(), [])
        self.admin.execute('GRANT SELECT ON fde_part.chunks_ulsan TO fde_reader')
        self.assertEqual(self.unguarded(), ['fde_part.chunks_ulsan'])
        # Named directly, only the partition's own (absent) policies apply: default deny.
        self.assertEqual(self.read('SELECT chunk_id FROM fde_part.chunks_ulsan'), [])

    def test_release_check_covers_sub_partitions_and_column_grants(self):
        # A factory split by document hash: the guarded middle level hides an unguarded leaf.
        self.admin.execute("CREATE TABLE fde_part.chunks_changwon PARTITION OF fde_part.chunks "
                           "FOR VALUES IN ('changwon') PARTITION BY HASH (chunk_id)")
        self.admin.execute('ALTER TABLE fde_part.chunks_changwon ENABLE ROW LEVEL SECURITY')
        self.admin.execute('ALTER TABLE fde_part.chunks_changwon FORCE ROW LEVEL SECURITY')
        self.admin.execute('CREATE TABLE fde_part.chunks_changwon_0 PARTITION OF fde_part.chunks_changwon '
                           'FOR VALUES WITH (MODULUS 1, REMAINDER 0)')
        self.assertEqual(self.unguarded(), ['fde_part.chunks_changwon_0'])
        self.admin.execute('ALTER TABLE fde_part.chunks_changwon_0 ENABLE ROW LEVEL SECURITY')
        self.admin.execute('ALTER TABLE fde_part.chunks_changwon_0 FORCE ROW LEVEL SECURITY')
        self.assertEqual(self.unguarded(), [])
        self.admin.execute('GRANT SELECT (chunk_id) ON fde_part.chunks_busan TO fde_reader')
        self.assertEqual(self.unguarded(), ['fde_part.chunks_busan'])

    def test_partition_without_rls_leaks_on_direct_grant(self):
        self.admin.execute("CREATE TABLE fde_part.chunks_changwon PARTITION OF fde_part.chunks "
                           "FOR VALUES IN ('changwon')")
        flags = self.admin.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                                   "WHERE oid = 'fde_part.chunks_changwon'::regclass").fetchone()
        self.assertEqual(flags, (False, False))
        self.assertEqual(self.unguarded(), ['fde_part.chunks_changwon'])
        self.admin.execute("INSERT INTO fde_part.chunks VALUES "
                           "('changwon','cw-secret',1,'x','{1}','fixture-v1','{hr}',false)")
        self.admin.execute('GRANT SELECT ON fde_part.chunks_changwon TO fde_reader')
        # The parent's policy is ignored when the partition is named: a busan engineer reads it.
        self.assertEqual(self.read('SELECT chunk_id FROM fde_part.chunks_changwon'), [('cw-secret',)])

    def test_unregistered_factory_is_rejected(self):
        with self.assertRaisesRegex(psycopg.errors.CheckViolation, 'no partition of relation'):
            self.admin.execute("INSERT INTO fde_part.chunks VALUES "
                               "('changwon','c1',1,'x','{1}','fixture-v1','{engineer}',false)")
        for factory in ('Busan', 'busan; DROP TABLE x', '', None, 'x' * 41):
            with self.subTest(factory=factory):
                with self.assertRaises(psycopg.errors.RaiseException):
                    self.admin.execute('CALL fde_part.add_factory(%s)', (factory,))
        self.admin.execute('CALL fde_part.add_factory(%s)', ('f' * 40,))
        self.assertEqual(self.unguarded(), [])

    def test_onboarding_new_factory(self):
        self.admin.execute("CALL fde_part.add_factory('changwon')")
        self.admin.execute("INSERT INTO fde_part.memberships VALUES ('changwon','park',ARRAY['engineer'])")
        self.admin.execute("INSERT INTO fde_part.chunks VALUES "
                           "('changwon','cw-manual',1,'x','{1}','fixture-v1','{engineer}',false)")
        self.assertEqual(self.unguarded(), [])
        self.assertEqual(self.ids('changwon', 'park'), ['cw-manual'])
        self.assertEqual(self.ids('busan', 'kim'), ['p101-overheat'])
        count = self.admin.execute("SELECT count(*) FROM pg_inherits "
                                   "WHERE inhparent = 'fde_part.chunks'::regclass").fetchone()[0]
        self.assertEqual(count, 3)

    def test_attach_staged_table_validates_rows(self):
        self.admin.execute('CREATE TABLE fde_part.chunks_changwon '
                           '(LIKE fde_part.chunks INCLUDING DEFAULTS INCLUDING CONSTRAINTS)')
        self.admin.execute("INSERT INTO fde_part.chunks_changwon VALUES "
                           "('busan','misrouted',1,'x','{1}','fixture-v1','{engineer}',false)")
        attach = ("ALTER TABLE fde_part.chunks ATTACH PARTITION fde_part.chunks_changwon "
                  "FOR VALUES IN ('changwon')")
        with self.assertRaisesRegex(psycopg.errors.CheckViolation, 'partition constraint'):
            self.admin.execute(attach)
        self.admin.execute('DELETE FROM fde_part.chunks_changwon')
        self.admin.execute("ALTER TABLE fde_part.chunks_changwon "
                           "ADD CONSTRAINT changwon_only CHECK (factory = 'changwon')")
        self.admin.execute(attach)
        self.assertEqual(self.unguarded(), ['fde_part.chunks_changwon'])
        self.admin.execute('ALTER TABLE fde_part.chunks_changwon ENABLE ROW LEVEL SECURITY')
        self.admin.execute('ALTER TABLE fde_part.chunks_changwon FORCE ROW LEVEL SECURITY')
        self.assertEqual(self.unguarded(), [])

    def test_detach_offboards_factory_and_keeps_table_for_export(self):
        self.admin.execute('ALTER TABLE fde_part.chunks DETACH PARTITION fde_part.chunks_ulsan')
        self.assertEqual(self.ids('ulsan', 'lee'), [])
        self.assertEqual(self.admin.execute('SELECT count(*) FROM fde_part.chunks_ulsan').fetchone()[0], 1)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.read('SELECT chunk_id FROM fde_part.chunks_ulsan', 'ulsan', 'lee')
        with self.assertRaisesRegex(psycopg.errors.CheckViolation, 'no partition of relation'):
            self.admin.execute("INSERT INTO fde_part.chunks VALUES "
                               "('ulsan','late-event',1,'x','{1}','fixture-v1','{engineer}',false)")
        self.admin.execute('DROP TABLE fde_part.chunks_ulsan')
        self.assertEqual(self.ids('busan', 'kim'), ['p101-overheat'])

    def test_primary_key_must_include_partition_key(self):
        with self.assertRaisesRegex(psycopg.Error, 'partition'):
            self.admin.execute('ALTER TABLE fde_part.chunks ADD CONSTRAINT global_chunk UNIQUE (chunk_id)')
        self.admin.execute("INSERT INTO fde_part.chunks VALUES "
                           "('ulsan','p101-overheat',1,'x','{1}','fixture-v1','{engineer}',false)")
        self.assertEqual(self.admin.execute("SELECT count(*) FROM fde_part.chunks "
                                            "WHERE chunk_id = 'p101-overheat'").fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
