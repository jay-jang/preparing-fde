from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import yaml

from deploy.render import render
from fde.bundle import verify_bundle
from fde.guardrails import ToolBudget, validate_answer
from fde.ingestion import Event
from fde.privacy import audit_record, mask_contact_fields, mask_known_patterns
from fde.release_gate import evaluate
from fde.retrieval import Chunk, Principal, bm25, cosine, rank, retrieve, statement_timeout


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.chunk = Chunk('manual-p1', 2, 'The pump must stop before inspection.', (1., 0.))

    def test_schema_migration_preserves_meaning(self):
        old = Event('acme', 'erp', 'A1', 1, 10, 0, 1, 'upsert',
                    {'asset_name': 'pump', 'state': 'running'})
        new = replace(old, schema_version=2, payload={'name': 'pump', 'status': 'running'})
        self.assertEqual(old.normalize(), new.normalize())

    def test_unknown_schema_and_fields_fail_closed(self):
        base = Event('a', 'erp', '1', 1, 1, 0, 3, 'upsert', {})
        with self.assertRaises(ValueError):
            base.normalize()
        with self.assertRaises(ValueError):
            replace(base, schema_version=2, payload={'name': 'x', 'status': 'running', 'owner_email': 'x'}).normalize()

    def test_lexical_and_dense_both_contribute(self):
        lexical = Chunk('lexical', 1, 'ZX-19 trip code', (0., 1.))
        semantic = Chunk('semantic', 1, 'An overheated pump should be shut down.', (1., 0.))
        self.assertGreater(bm25('ZX-19', [lexical, semantic])['lexical'], 0)
        selected = rank('ZX-19', (1., 0.), [lexical, semantic], k=2)
        self.assertEqual({c.id for c in selected}, {'lexical', 'semantic'})

    def test_vector_corruption_is_rejected(self):
        for left, right in [((1.,), (1., 2.)), ((0., 0.), (1., 0.)), ((float('nan'),), (1.,))]:
            with self.assertRaises(ValueError):
                cosine(left, right)

    def test_rrf_order_is_deterministic(self):
        other = replace(self.chunk, id='manual-p2')
        self.assertEqual(rank('pump', (1., 0.), [other, self.chunk]),
                         rank('pump', (1., 0.), [self.chunk, other]))

    def answer(self, **updates):
        claim = {'text': 'Stop the pump.', 'chunk_id': self.chunk.id,
                 'version': self.chunk.version, 'quote': 'must stop before inspection'}
        claim.update(updates)
        return json.dumps({'abstain': False, 'claims': [claim]})

    def test_citation_bound_to_retrieved_version(self):
        self.assertFalse(validate_answer(self.answer(), [self.chunk])['abstain'])
        for updates in ({'version': 1}, {'chunk_id': 'secret'}, {'quote': 'fabricated evidence'}):
            with self.assertRaises(ValueError):
                validate_answer(self.answer(**updates), [self.chunk])

    def test_guard_does_not_claim_to_prove_semantic_support(self):
        # Exact provenance is valid even when a malicious model reverses meaning.
        result = validate_answer(self.answer(text='Keep the pump running.'), [self.chunk])
        self.assertEqual(result['claims'][0]['text'], 'Keep the pump running.')

    def test_abstention_duplicate_keys_and_unknown_fields(self):
        self.assertTrue(validate_answer('{"abstain":true,"claims":[]}', [])['abstain'])
        for raw in ('{"abstain":true,"claims":[],"tool":"delete"}',
                    '{"abstain":true,"abstain":false,"claims":[]}'):
            with self.assertRaises(ValueError):
                validate_answer(raw, [])

    def test_agent_cannot_supply_identity_or_write_tool(self):
        calls = []
        def search(principal, query, remaining_seconds):
            calls.append((principal, query, remaining_seconds))
            return []
        principal = Principal('acme', 'alice')
        agent = ToolBudget(principal, search, calls=1)
        for name, arguments in [('delete', {'query': 'x'}), ('search', {'query': 'x', 'tenant': 'other'})]:
            with self.assertRaises(ValueError):
                agent.invoke(name, arguments)
        agent.invoke('search', {'query': 'pump'})
        self.assertEqual(calls[0][0], principal)
        with self.assertRaises(TimeoutError):
            agent.invoke('search', {'query': 'pump'})

    def test_tool_budget_stays_inside_the_request_deadline(self):
        principal = Principal('acme', 'alice')
        for seconds in (0, 5.5, 10):
            with self.assertRaises(ValueError):
                ToolBudget(principal, lambda *_: [], seconds=seconds)
        now = time.monotonic()
        agent = ToolBudget(principal, lambda *_: [], seconds=4.0, deadline=now + 0.25)
        self.assertLessEqual(agent.deadline, now + 0.25)
        for deadline in (now - 1, '5', float('nan')):
            with self.assertRaises(ValueError):
                ToolBudget(principal, lambda *_: [], deadline=deadline)

    def test_statement_timeout_follows_the_remaining_request_time(self):
        self.assertEqual(statement_timeout(250), 'SET LOCAL statement_timeout = 250')
        self.assertEqual(statement_timeout(2000), 'SET LOCAL statement_timeout = 2000')
        for value in (0, 2001, 250.0, '250', True):
            with self.assertRaises(ValueError):
                statement_timeout(value)
        with self.assertRaises(ValueError):  # checked before the connection is touched
            retrieve(None, Principal('acme', 'alice'), 'pump', (1.0, 0.0), 'm', timeout_ms=0)

    def test_pii_minimization_and_masking(self):
        record = mask_contact_fields({'asset_id': '00000000-0000-0000-0000-000000000001',
                                     'contact_email': 'user@example.com', 'name': 'private name'})
        self.assertNotIn('name', record)
        self.assertEqual(record['contact_email'], '[REDACTED]')
        self.assertEqual(mask_known_patterns('a@example.com 010-1234-5678'), '[EMAIL] [PHONE]')

    def test_audit_has_no_raw_identity_and_key_rotation_is_visible(self):
        args = {'key': b'x' * 32, 'key_id': 'v1', 'tenant': 'acme', 'subject': 'user@example.com',
                'request_id': '00000000-0000-0000-0000-000000000001',
                'decision': 'allow', 'artifact_digest': 'sha256:' + 'a' * 64}
        record = audit_record(**args)
        self.assertNotIn('user@example.com', json.dumps(record))
        self.assertNotEqual(record['actor_hash'], audit_record(**{**args, 'key': b'y'*32, 'key_id': 'v2'})['actor_hash'])

    def report(self):
        # Synthetic rows test gate arithmetic; these are NOT model measurements.
        rows = []
        for kind in ('factoid', 'semantic', 'unanswerable', 'forbidden'):
            for index in range(30):
                rows.append({'id': f'{kind}-{index}', 'kind': kind, 'retrieval_hit': True,
                             'supported': True, 'abstained': kind in ('unanswerable', 'forbidden'),
                             'leaked': False, 'latency_ms': 1000})
        return {'dataset_version': 'synthetic-unit-fixture', 'artifact_digest': 'sha256:'+'a'*64, 'rows': rows}

    def test_quality_gate_baseline_and_leak_in_any_slice(self):
        report = self.report()
        self.assertTrue(evaluate(report)['pass'])
        report['rows'][0]['leaked'] = True
        self.assertFalse(evaluate(report)['pass'])

    def test_quality_gate_cannot_pass_by_always_abstaining(self):
        report = self.report()
        for row in report['rows']:
            row['abstained'] = True
        self.assertFalse(evaluate(report)['pass'])

    def test_quality_gate_slice_regression_not_hidden_by_average(self):
        report = self.report()
        for row in report['rows']:
            if row['kind'] == 'semantic':
                row['retrieval_hit'] = False
        self.assertFalse(evaluate(report)['pass'])

    def test_latency_tail_is_checked_per_answerable_slice(self):
        report = self.report()
        for row in report['rows'][:6]:
            row['latency_ms'] = 7000
        result = evaluate(report)
        self.assertEqual(result['metrics']['p95_ms'], 1000)
        self.assertFalse(result['pass'])

    def test_quality_gate_empty_missing_nan_duplicate(self):
        for mutation in ('empty', 'missing', 'nan', 'duplicate'):
            report = self.report()
            if mutation == 'empty':
                report['rows'] = []
            elif mutation == 'missing':
                del report['rows'][0]['leaked']
            elif mutation == 'nan':
                report['rows'][0]['latency_ms'] = float('nan')
            else:
                report['rows'].append(report['rows'][0])
            with self.assertRaises(ValueError):
                evaluate(report)

    def test_deployment_requires_digest_and_roundtrips_yaml(self):
        with self.assertRaises(ValueError):
            render('registry.internal/fde/api:latest')
        docs = render('registry.internal/fde/api@sha256:' + 'a' * 64)
        self.assertEqual(list(yaml.safe_load_all(yaml.safe_dump_all(docs))), docs)
        deployment = next(d for d in docs if d['kind'] == 'Deployment')
        pod = deployment['spec']['template']['spec']
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertTrue(pod['containers'][0]['securityContext']['readOnlyRootFilesystem'])
        policies = [d for d in docs if d['kind'] == 'NetworkPolicy']
        self.assertTrue(any(d['spec']['podSelector'] == {} for d in policies))


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='fde-signature-')
        cls.path = Path(cls.temp.name)
        cls.private = cls.path / 'private.pem'
        cls.public = cls.path / 'approved.pub'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                        '-out', str(cls.private)], check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(cls.private), '-pubout', '-out', str(cls.public)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.bundle_dir = tempfile.TemporaryDirectory(dir=self.path)
        self.root = Path(self.bundle_dir.name)
        (self.root / 'model.bin').write_bytes(b'fixture-model-not-real-weights')
        self.manifest = {'schema_version': 1, 'release_id': 'r7', 'expires_at': '2030-01-01T00:00:00+00:00',
                         'files': [{'name': 'model.bin', 'sha256': hashlib.sha256((self.root/'model.bin').read_bytes()).hexdigest(),
                                    'bytes': (self.root/'model.bin').stat().st_size}]}
        self.sign()

    def tearDown(self):
        self.bundle_dir.cleanup()

    def sign(self):
        (self.root / 'release.json').write_text(json.dumps(self.manifest))
        subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(self.private),
                        '-out', str(self.root/'release.sig'), str(self.root/'release.json')],
                       check=True, capture_output=True)

    def verify(self, **updates):
        return verify_bundle(self.root, public_key=self.public, expected_release='r7',
                             now=updates.get('now', datetime(2026, 11, 16, tzinfo=timezone.utc)))

    def test_real_signature_verifies(self):
        self.assertEqual(self.verify()['release_id'], 'r7')

    def test_tampered_manifest_fails_signature(self):
        with (self.root/'release.json').open('a') as stream:
            stream.write(' ')
        with self.assertRaises(subprocess.CalledProcessError):
            self.verify()

    def test_tampered_artifact_fails_hash(self):
        data = (self.root/'model.bin').read_bytes()
        (self.root/'model.bin').write_bytes(b'X' + data[1:])
        with self.assertRaises(ValueError):
            self.verify()

    def test_expired_and_replayed_release_fail(self):
        with self.assertRaises(ValueError):
            self.verify(now=datetime(2031, 1, 1, tzinfo=timezone.utc))
        self.manifest['release_id'] = 'r6'
        self.sign()
        with self.assertRaises(ValueError):
            self.verify()

    def test_signed_path_escape_and_symlink_fail(self):
        self.manifest['files'][0]['name'] = '../private.pem'
        self.sign()
        with self.assertRaises(ValueError):
            self.verify()
        self.manifest['files'][0]['name'] = 'model.bin'
        self.sign()
        (self.root/'model.bin').unlink()
        (self.root/'model.bin').symlink_to(self.private)
        with self.assertRaises(ValueError):
            self.verify()
