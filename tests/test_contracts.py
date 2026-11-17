from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from fde.contracts import Contract, Field, SchemaBreak, replay, validate_batch

NOW = datetime(2026, 11, 16, 0, 0, 12, tzinfo=timezone.utc)

WORK_ORDERS = Contract(
    source='cmms.work-orders', revision=1, schema_versions=(1,), owner='정비 시스템 owner',
    fields=(Field('work_order_id', 'id', pattern=r'WO-\d{6}'),
            Field('asset_key', 'id', pattern=r'[A-Z]{1,3}-\d{1,4}'),
            Field('status', 'enum', choices=('open', 'closed')),
            Field('bearing_temp', 'quantity', unit='C', minimum=-40, maximum=150),
            Field('event_time', 'timestamp')),
    max_lag_seconds=30, max_skew_seconds=5)

# Mirrors the v2 source payload that fde/ingestion.py normalizes.
ERP_EQUIPMENT = Contract(
    source='erp.equipment', revision=1, schema_versions=(2,), owner='ERP 데이터 owner',
    fields=(Field('equipment_id', 'id', pattern=r'A\d{1,6}'),
            Field('name', 'text'),
            Field('status', 'enum', choices=('running', 'stopped', 'unknown')),
            Field('criticality', 'int', required=False, minimum=1, maximum=5),
            Field('event_time', 'timestamp')),
    max_lag_seconds=30, max_skew_seconds=5)

OK = {'schema_version': 1, 'work_order_id': 'WO-000123', 'asset_key': 'P-101',
      'status': 'closed', 'bearing_temp': {'value': 92.5, 'unit': 'C'},
      'event_time': '2026-11-16T09:00:00+09:00'}


class ContractTests(unittest.TestCase):
    def test_frame_example_counts_and_reasons(self):
        bad = dict(OK, asset_key='P101', bearing_temp={'value': 198.5, 'unit': 'F'})
        late = dict(OK, event_time='2026-11-16T08:58:00+09:00')
        result = validate_batch(WORK_ORDERS, [(10, OK), (11, bad), (12, late)], ingest_time=NOW)
        self.assertEqual(result.counts, {'accepted': 2, 'quarantined': 1, 'bad_id_format': 1,
                                         'unit_mismatch': 1, 'late_event': 1})
        self.assertEqual(result.quarantined[0].reasons,
                         ('bad_id_format:asset_key', 'unit_mismatch:bearing_temp'))
        self.assertEqual([(a.position, a.lag_seconds, a.warnings) for a in result.accepted],
                         [(10, 12.0, ()), (12, 132.0, ('late_event',))])

    def test_accepted_record_is_unchanged(self):
        result = validate_batch(WORK_ORDERS, [(10, OK)], ingest_time=NOW)
        self.assertEqual(result.accepted[0].record, OK)
        self.assertIsNot(result.accepted[0].record, OK)

    def test_no_silent_coercion(self):
        cases = {
            'wrong_type:bearing_temp': {'bearing_temp': {'value': '92.5', 'unit': 'C'}},
            'out_of_range:bearing_temp': {'bearing_temp': {'value': 150.5, 'unit': 'C'}},
            'not_in_enum:status': {'status': 'CLOSED'},
            'null_value:status': {'status': None},
            'bad_timestamp:event_time': {'event_time': '2026-11-16T09:00:00'},
            'wrong_type:work_order_id': {'work_order_id': 123},
            'bad_id_format:asset_key': {'asset_key': 'P-\u0661\u0660\u0661'},  # Arabic-Indic digits
        }
        for reason, update in cases.items():
            with self.subTest(reason):
                result = validate_batch(WORK_ORDERS, [(1, dict(OK, **update))], ingest_time=NOW)
                self.assertEqual(result.quarantined[0].reasons, (reason,))
        missing = {k: v for k, v in OK.items() if k != 'status'}
        result = validate_batch(WORK_ORDERS, [(1, missing)], ingest_time=NOW)
        self.assertEqual(result.quarantined[0].reasons, ('missing_field:status',))

    def test_int_rejects_bool_and_float_and_optional_may_be_absent(self):
        base = {'schema_version': 2, 'equipment_id': 'A1', 'name': 'Pump A1',
                'status': 'running', 'event_time': '2026-11-16T00:00:00Z'}
        records = [(1, base), (2, dict(base, criticality=3)), (3, dict(base, criticality=3.0)),
                   (4, dict(base, criticality=True)), (5, dict(base, criticality=6))]
        result = validate_batch(ERP_EQUIPMENT, records, ingest_time=NOW)
        self.assertEqual([a.position for a in result.accepted], [1, 2])
        self.assertEqual([q.reasons for q in result.quarantined],
                         [('wrong_type:criticality',), ('wrong_type:criticality',),
                          ('out_of_range:criticality',)])

    def test_shape_change_stops_the_batch(self):
        cases = [dict(OK, schema_version=2), dict(OK, technician_email='x@example.com'),
                 {k: v for k, v in OK.items() if k != 'schema_version'}, ['not', 'an', 'object']]
        for record in cases:
            with self.subTest(record), self.assertRaises(SchemaBreak) as caught:
                validate_batch(WORK_ORDERS, [(10, OK), (11, record)], ingest_time=NOW)
            self.assertEqual(caught.exception.position, 11)
        self.assertEqual(caught.exception.reason, 'record is not an object')

    def test_future_event_beyond_skew_is_quarantined(self):
        within = dict(OK, event_time='2026-11-16T09:00:17+09:00')
        beyond = dict(OK, event_time='2026-11-16T09:00:18+09:00')
        result = validate_batch(WORK_ORDERS, [(1, within), (2, beyond)], ingest_time=NOW)
        self.assertEqual(result.accepted[0].lag_seconds, -5.0)
        self.assertEqual(result.quarantined[0].reasons, ('future_event_time:event_time',))

    def test_lag_p95_is_nearest_rank_over_accepted(self):
        records = [(i, dict(OK, event_time=(NOW - timedelta(seconds=i)).isoformat()))
                   for i in range(1, 21)]
        result = validate_batch(WORK_ORDERS, records, ingest_time=NOW)
        self.assertEqual(result.lag_p95_seconds, 19.0)
        self.assertNotIn('late_event', result.counts)
        self.assertIsNone(validate_batch(WORK_ORDERS, [], ingest_time=NOW).lag_p95_seconds)

    def test_replay_after_contract_revision_uses_original_clock(self):
        deferred = dict(OK, status='deferred')
        first = validate_batch(WORK_ORDERS, [(7, deferred)], ingest_time=NOW)
        self.assertEqual(first.counts, {'accepted': 0, 'quarantined': 1, 'not_in_enum': 1})
        with self.assertRaisesRegex(ValueError, 'newer contract revision'):
            replay(WORK_ORDERS, first.quarantined)
        revised = replace(WORK_ORDERS, revision=2, fields=(
            *WORK_ORDERS.fields[:2], Field('status', 'enum', choices=('open', 'closed', 'deferred')),
            *WORK_ORDERS.fields[3:]))
        again = replay(revised, first.quarantined)
        self.assertEqual(again.counts, {'accepted': 1, 'quarantined': 0})
        self.assertEqual((again.accepted[0].position, again.accepted[0].lag_seconds), (7, 12.0))
        self.assertEqual((first.quarantined[0].source, first.quarantined[0].contract_revision),
                         ('cmms.work-orders', 1))
        other_source = replace(revised, source='erp.equipment')
        with self.assertRaisesRegex(ValueError, 'source that quarantined'):
            replay(other_source, first.quarantined)

    def test_batch_input_validation(self):
        with self.assertRaises(ValueError):
            validate_batch(WORK_ORDERS, [(2, OK), (2, OK)], ingest_time=NOW)
        with self.assertRaises(ValueError):
            validate_batch(WORK_ORDERS, [OK], ingest_time=NOW)
        with self.assertRaises(ValueError):
            validate_batch(WORK_ORDERS, [(1, OK)], ingest_time=datetime(2026, 11, 16))
        with self.assertRaises(TypeError):
            replay(WORK_ORDERS, [])

    def test_contract_definition_fails_closed(self):
        bad_fields = [dict(name='Asset', kind='text'), dict(name='a', kind='id'),
                      dict(name='a', kind='enum'), dict(name='a', kind='quantity'),
                      dict(name='a', kind='text', minimum=1),
                      dict(name='a', kind='int', minimum=5, maximum=1),
                      dict(name='a', kind='id', pattern='(')]
        for kwargs in bad_fields:
            with self.subTest(kwargs), self.assertRaises(ValueError):
                Field(**kwargs)
        for change in [dict(owner=' '), dict(schema_versions=()), dict(schema_versions=1),
                       dict(max_lag_seconds=0),
                       dict(fields=WORK_ORDERS.fields[:4]), dict(revision=0),
                       dict(fields=WORK_ORDERS.fields + (Field('status', 'text'),))]:
            with self.subTest(change), self.assertRaises(ValueError):
                replace(WORK_ORDERS, **change)


if __name__ == '__main__':
    unittest.main()
