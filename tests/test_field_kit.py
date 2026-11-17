from dataclasses import FrozenInstanceError, replace
import math
import unittest

from fde.capacity import gpu_plan, in_flight_requests, kv_cache_bytes_per_token, vector_bytes
from fde.privacy import audit_record
from fde.provenance import AnswerRecord, BOUNDARIES, localize


class CapacityTests(unittest.TestCase):
    def plan(self, **updates):
        args = {'gpu_bytes': 51_539_607_552, 'utilization': 0.9,
                'weights_bytes': 16_000_000_000, 'overhead_bytes': 2_147_483_648,
                'kv_bytes_per_token': 131_072, 'context_tokens': 8_192, 'sequences': 5}
        args.update(updates)
        return gpu_plan(**args)

    def test_raw_vector_storage(self):
        self.assertEqual(vector_bytes(1_000_000, 768, 4), 3_072_000_000)
        self.assertEqual(vector_bytes(count=1_000_000, dims=768), 3_072_000_000)

    def test_hypothetical_gqa_kv_storage(self):
        per_token = kv_cache_bytes_per_token(layers=32, kv_heads=8, head_dim=128)
        self.assertEqual(per_token, 131_072)
        self.assertEqual(kv_cache_bytes_per_token(32, 8, 128, 2), per_token)
        self.assertEqual(per_token * 8_192, 1_073_741_824)

    def test_users_are_not_concurrent_requests(self):
        estimate = in_flight_requests(arrival_per_second=50 / 60, latency_seconds=5.0)
        self.assertIs(type(estimate), float)
        self.assertAlmostEqual(estimate, 4.1666666667)
        self.assertLess(estimate, 50)
        self.assertEqual(math.ceil(estimate), 5)

    def test_fitting_gpu_plan_exact_byte_accounting(self):
        plan = self.plan()
        self.assertTrue(plan.fits)
        self.assertEqual(plan.usable_bytes, 46_385_646_796)
        self.assertEqual(plan.weights_bytes, 16_000_000_000)
        self.assertEqual(plan.overhead_bytes, 2_147_483_648)
        self.assertEqual(plan.kv_bytes, 5_368_709_120)
        self.assertEqual(plan.headroom_bytes, 22_869_454_028)
        self.assertEqual(plan.max_sequences, 26)
        self.assertTrue(self.plan(sequences=26).fits)
        self.assertFalse(self.plan(sequences=27).fits)

    def test_fifty_simultaneous_requests_exceed_memory(self):
        plan = self.plan(sequences=50)
        self.assertFalse(plan.fits)
        self.assertEqual(plan.kv_bytes, 53_687_091_200)
        self.assertEqual(plan.headroom_bytes, -25_448_928_052)
        self.assertEqual(plan.max_sequences, 26)

    def test_exact_fit_and_no_remaining_sequence(self):
        plan = self.plan(gpu_bytes=64, utilization=1, weights_bytes=16, overhead_bytes=0,
                         kv_bytes_per_token=2, context_tokens=8, sequences=3)
        self.assertTrue(plan.fits)
        self.assertEqual((plan.headroom_bytes, plan.max_sequences), (0, 3))
        exhausted = self.plan(gpu_bytes=1)
        self.assertFalse(exhausted.fits)
        self.assertEqual(exhausted.max_sequences, 0)

    def test_remaining_memory_below_one_sequence(self):
        plan = self.plan(gpu_bytes=64, utilization=1.0, weights_bytes=16, overhead_bytes=0,
                         kv_bytes_per_token=2, context_tokens=32, sequences=1)
        self.assertEqual(plan.usable_bytes, 64)
        self.assertFalse(plan.fits)
        self.assertEqual((plan.headroom_bytes, plan.max_sequences), (-16, 0))

    def test_integer_inputs_fail_closed_in_every_position(self):
        for function, names in ((vector_bytes, ('count', 'dims', 'bytes_per_value')),
                                (kv_cache_bytes_per_token,
                                 ('layers', 'kv_heads', 'head_dim', 'bytes_per_value'))):
            for name in names:
                for value in (0, -1, True, 1.0, float('nan'), float('inf'), None, '1'):
                    args = dict.fromkeys(names, 1)
                    args[name] = value
                    with self.subTest(function=function.__name__, field=name, value=value):
                        with self.assertRaises(ValueError):
                            function(**args)
        for name in ('gpu_bytes', 'weights_bytes', 'kv_bytes_per_token',
                     'context_tokens', 'sequences', 'overhead_bytes'):
            values = (-1, True, 1.0, float('nan'), float('inf'), None, '1')
            for value in values + (() if name == 'overhead_bytes' else (0,)):
                with self.subTest(field=name, value=value):
                    with self.assertRaises(ValueError):
                        self.plan(**{name: value})

    def test_rates_and_utilization_reject_invalid_numbers(self):
        invalid = (0, -1, True, float('nan'), float('inf'), -float('inf'), None, '1')
        for name in ('arrival_per_second', 'latency_seconds'):
            for value in invalid:
                args = {'arrival_per_second': 1, 'latency_seconds': 1}
                args[name] = value
                with self.subTest(field=name, value=value):
                    with self.assertRaises(ValueError):
                        in_flight_requests(**args)
        for value in invalid + (1.01, 2):
            with self.subTest(utilization=value):
                with self.assertRaises(ValueError):
                    self.plan(utilization=value)

    def test_overhead_must_be_stated(self):
        with self.assertRaises(TypeError):
            gpu_plan(gpu_bytes=64, utilization=1, weights_bytes=16,
                     kv_bytes_per_token=2, context_tokens=8, sequences=1)

    def test_nonfinite_and_underflowed_estimates_fail_closed(self):
        for rate, latency in ((1e308, 2.0), (10 ** 400, 1), (1e-300, 1e-300)):
            with self.subTest(rate=rate, latency=latency):
                with self.assertRaises(ValueError):
                    in_flight_requests(rate, latency)
        with self.assertRaises(ValueError):
            self.plan(gpu_bytes=10 ** 400)

    def test_byte_totals_remain_integers_and_plan_is_frozen(self):
        self.assertEqual(vector_bytes(2 ** 60, 3, 2), 6 * 2 ** 60)
        plan = self.plan(gpu_bytes=2 ** 80, utilization=1)
        self.assertEqual(plan.usable_bytes, 2 ** 80)
        self.assertIs(type(plan.kv_bytes), int)
        self.assertIs(type(plan.headroom_bytes), int)
        with self.assertRaises(FrozenInstanceError):
            plan.fits = False


class ProvenanceTests(unittest.TestCase):
    def record(self, **updates):
        args = {'request_id': '12345678-1234-4abc-8def-1234567890ab',
                'tenant': 'fixture-factory', 'subject_pseudonym': '0' * 64,
                'key_id': 'fixture-v1', 'snapshot_id': 'backup-v1', 'source_positions': (('erp', 10), ('manual', 0)),
                'policy_version': 'policy-v1', 'index_version': 'index-v1',
                'embedding_model': 'embedding-v1', 'reranker': None,
                'prompt_version': 'prompt-decoding-v1', 'model_digest': 'sha256:' + 'a' * 64,
                'evidence': (('manual-p1', 2), ('maintenance-p2', 1)), 'abstained': False}
        args.update(updates)
        return AnswerRecord(**args)

    def test_identical_record_has_no_changed_inputs(self):
        record = self.record()
        self.assertEqual(localize(record, record), [])
        self.assertEqual(localize(record, replace(record)), [])

    def test_every_field_maps_to_its_boundary(self):
        record = self.record()
        changes = (('snapshot_id', 'backup-v2', 'data'),
                   ('source_positions', (('erp', 11), ('manual', 0)), 'data'),
                   ('policy_version', 'policy-v2', 'policy'),
                   ('index_version', 'index-v2', 'retrieval'),
                   ('embedding_model', 'embedding-v2', 'retrieval'),
                   ('reranker', 'reranker-v1', 'retrieval'),
                   ('evidence', (('manual-p1', 3),), 'retrieval'),
                   ('prompt_version', 'prompt-decoding-v2', 'generation'),
                   ('model_digest', 'sha256:' + 'b' * 64, 'generation'))
        for field, value, boundary in changes:
            with self.subTest(field=field):
                self.assertEqual(localize(record, replace(record, **{field: value})), [boundary])

    def test_multiple_boundaries_follow_pipeline_order(self):
        record = self.record()
        replay = replace(record, model_digest='sha256:' + 'b' * 64, snapshot_id='backup-v2')
        self.assertEqual(localize(record, replay), ['data', 'generation'])
        replay = replace(replay, policy_version='policy-v2', index_version='index-v2')
        self.assertEqual(localize(record, replay), list(BOUNDARIES))

    def test_prompt_evidence_order_is_significant(self):
        record = self.record()
        replay = replace(record, evidence=tuple(reversed(record.evidence)))
        self.assertEqual(localize(record, replay), ['retrieval'])

    def test_abstention_is_an_outcome(self):
        record = self.record()
        self.assertEqual(localize(record, replace(record, abstained=True)), [])
        self.assertEqual(self.record(abstained=True, evidence=()).evidence, ())
        abstained = self.record(abstained=True, evidence=())
        self.assertEqual(localize(record, abstained), ['retrieval'])

    def test_cross_tenant_refusal_precedes_request_and_principal_checks(self):
        record = self.record()
        replay = replace(record, tenant='other-factory', subject_pseudonym='b' * 64,
                         request_id='12345678-1234-4abc-8def-1234567890ac')
        with self.assertRaisesRegex(PermissionError, '^cross-tenant comparison refused$'):
            localize(record, replay)

    def test_replay_requires_same_request_and_principal(self):
        record = self.record()
        for updates in ({'request_id': '12345678-1234-4abc-8def-1234567890ac'},
                        {'subject_pseudonym': 'b' * 64}, {'key_id': 'fixture-v2'}):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    localize(record, replace(record, **updates))

    def test_comparison_requires_records_on_both_sides(self):
        for value in (None, {}, (), 'record'):
            for arguments in ((value, self.record()), (self.record(), value)):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        localize(*arguments)

    def test_subclass_that_skips_validation_is_refused(self):
        class Unchecked(AnswerRecord):
            def __post_init__(self):
                pass
        record = self.record()
        forged = Unchecked(**{**record.__dict__, 'evidence': 'free text'})
        for arguments in ((forged, record), (record, forged)):
            with self.subTest(arguments=type(arguments[0]).__name__):
                with self.assertRaises(ValueError):
                    localize(*arguments)

    def test_request_id_must_be_canonical_lowercase_uuid(self):
        request_id = self.record().request_id
        for value in (request_id.upper(), request_id.replace('-', ''), '{' + request_id + '}',
                      'urn:uuid:' + request_id, 'invalid', None, 1):
            with self.subTest(request_id=value):
                with self.assertRaises(ValueError):
                    self.record(request_id=value)

    def test_subject_and_model_digest_are_strict(self):
        for field, values in (('subject_pseudonym', ('user@example.com', 'a' * 63, 'A' * 64, None)),
                              ('model_digest', ('a' * 64, 'sha256:' + 'A' * 64,
                                                'sha256:' + 'a' * 63, None))):
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.record(**{field: value})

    def test_key_id_matches_privacy_format(self):
        for value in ('', 'x' * 41, 'key v1', 'key/v1', 'v1\n', None, 1):
            with self.subTest(key_id=value):
                with self.assertRaises(ValueError):
                    self.record(key_id=value)
        self.assertEqual(self.record(key_id='x' * 40).key_id, 'x' * 40)

    def test_identifiers_cannot_contain_whitespace_or_free_text(self):
        for field in ('tenant', 'snapshot_id', 'policy_version', 'index_version',
                      'embedding_model', 'reranker', 'prompt_version'):
            for value in ('', 'x' * 201, 'free text', 'v1\n', '<text>', 1):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.record(**{field: value})
        self.record(tenant='x' * 200, reranker='models/reranker-v1@sha256:abc+test')

    def test_pair_containers_identifiers_and_integers_are_strict(self):
        for field in ('source_positions', 'evidence'):
            for value in ([('a', 1)], (['a', 1],), (('a',),), (('a', 1, 2),),
                          (('a b', 1),), (('', 1),), ((1, 1),), (('a', -1),),
                          (('a', True),), (('a', 1.0),), (('a', float('nan')),),
                          (('a', float('inf')),), None):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.record(**{field: value})

    def test_sources_must_be_nonempty_unique_and_sorted(self):
        for value in ((), (('z', 0), ('a', 0)), (('a', 0), ('a', 1))):
            with self.subTest(sources=value):
                with self.assertRaises(ValueError):
                    self.record(source_positions=value)

    def test_evidence_is_unique_versioned_and_bounded(self):
        for value in ((('a', 0),), (('a', 1), ('a', 2)),
                      tuple((f'chunk-{index}', 1) for index in range(21))):
            with self.subTest(evidence=value):
                with self.assertRaises(ValueError):
                    self.record(evidence=value)
        evidence = tuple((f'chunk-{index}', 1) for index in range(20))
        self.assertEqual(len(self.record(evidence=evidence).evidence), 20)

    def test_only_real_abstention_allows_empty_evidence(self):
        for value in (0, 1, None, 'true'):
            with self.subTest(abstained=value):
                with self.assertRaises(ValueError):
                    self.record(abstained=value)
        with self.assertRaises(ValueError):
            self.record(abstained=False, evidence=())

    def test_privacy_hmac_pseudonym_is_accepted(self):
        event = audit_record(key=b'x' * 32, key_id='fixture-v1', tenant='fixture-factory',
                             subject='engineer@example.com', request_id=self.record().request_id,
                             decision='allow', artifact_digest=self.record().model_digest)
        record = self.record(subject_pseudonym=event['actor_hash'], key_id=event['key_id'])
        self.assertEqual(record.subject_pseudonym, event['actor_hash'])
        self.assertEqual(record.key_id, 'fixture-v1')
        self.assertNotIn('engineer@example.com', repr(record))

    def test_record_is_immutable(self):
        record = self.record()
        with self.assertRaises(FrozenInstanceError):
            record.policy_version = 'policy-v2'
