from dataclasses import FrozenInstanceError, replace
from fractions import Fraction
import inspect
import unittest

from fde.capacity import kv_cache_bytes_per_token
from fde.retrieval import retrieve
from fde.scaleout import EXACT_SEARCH_CAP, Limits, Pilot, Projection, gpu_sequence_limit, project

# 5-3 assumptions; not measurements.
GPU = dict(gpu_bytes=48 * 2**30, utilization=0.9, weights_bytes=8_000_000_000 * 2,
           overhead_bytes=2 * 2**30, kv_bytes_per_token=kv_cache_bytes_per_token(32, 8, 128, 2),
           context_tokens=8_192)


class ScaleOutTests(unittest.TestCase):
    def setUp(self):
        # Case-table numbers per factory plus labelled proposals (widest role, audit bytes).
        self.pilot = Pilot(chunks=1_000_000, dims=768, bytes_per_value=4, widest_allowed_chunks=4_000,
                           burst_changes_per_second=500, users=50, questions_per_user_per_minute=1,
                           latency_seconds=5.0, audit_bytes_per_request=1_024)
        self.limits = Limits(gpu_sequences=gpu_sequence_limit(gpus=1, **GPU),
                             vector_bytes=32 * 2**30, ingest_per_second=2_000,
                             audit_bytes_per_day=2**30)

    def rows(self, **updates):
        args = {'factories': 10, 'allowed_growth': 10}
        args.update(updates)
        return {row.resource: row for row in project(self.pilot, self.limits, **args)}

    def test_exact_cap_matches_retrieval_default(self):
        self.assertEqual(EXACT_SEARCH_CAP, 5_000)
        self.assertEqual(inspect.signature(retrieve).parameters['cap'].default, EXACT_SEARCH_CAP)
        self.assertEqual(Limits(1, 1, 1, 1).exact_search_chunks, 5_000)
        with self.assertRaises(ValueError):
            Limits(1, 1, 1, 1, exact_search_chunks=5_001)

    def test_gpu_sequence_limit_reuses_capacity_plan(self):
        self.assertEqual(gpu_sequence_limit(gpus=1, **GPU), 26)
        self.assertEqual(gpu_sequence_limit(gpus=2, **GPU), 52)
        with self.assertRaises(ValueError):
            gpu_sequence_limit(gpus=0, **GPU)

    def test_ten_factories_exact_values(self):
        rows = self.rows()
        expected = {
            'exact_search': (4_000, 40_000, 5_000, Fraction(8)),
            'ingest_burst': (500, 5_000, 2_000, Fraction(5, 2)),
            'gpu_sequences': (5, 42, 26, Fraction(21, 13)),
            'raw_vectors': (3_072_000_000, 30_720_000_000, 34_359_738_368,
                            Fraction(30_720_000_000, 34_359_738_368)),
            'audit_log': (73_728_000, 737_280_000, 1_073_741_824, Fraction(737_280_000, 2**30)),
        }
        for name, (pilot, projected, limit, utilization) in expected.items():
            with self.subTest(resource=name):
                row = rows[name]
                self.assertEqual((row.pilot, row.projected, row.limit), (pilot, projected, limit))
                self.assertEqual(row.utilization, utilization)
                self.assertEqual(row.headroom, limit - projected)
        self.assertEqual(round(float(rows['raw_vectors'].utilization), 4), 0.8941)
        self.assertEqual(round(float(rows['audit_log'].utilization), 4), 0.6866)

    def test_first_bottleneck_first(self):
        order = [row.resource for row in project(self.pilot, self.limits, factories=10,
                                                 allowed_growth=10)]
        self.assertEqual(order, ['exact_search', 'ingest_burst', 'gpu_sequences',
                                 'raw_vectors', 'audit_log'])

    def test_factory_local_roles_keep_the_exact_path(self):
        rows = self.rows(allowed_growth=1)
        self.assertEqual(rows['exact_search'].projected, 4_000)
        self.assertEqual(rows['exact_search'].utilization, Fraction(4, 5))
        order = [row.resource for row in project(self.pilot, self.limits, factories=10,
                                                 allowed_growth=1)]
        self.assertEqual(order[0], 'ingest_burst')
        self.assertEqual(order.index('exact_search'), 3)

    def test_pilot_fits_every_limit(self):
        for row in project(self.pilot, self.limits, factories=1, allowed_growth=1):
            with self.subTest(resource=row.resource):
                self.assertEqual(row.projected, row.pilot)
                self.assertLess(row.utilization, 1)
        rows = self.rows(factories=1, allowed_growth=1)
        self.assertEqual(rows['gpu_sequences'].utilization, Fraction(5, 26))
        self.assertEqual(rows['ingest_burst'].utilization, Fraction(1, 4))

    def test_sequence_ceiling_is_exact(self):
        # 100 users x 10 factories / 60 x 7.5 s = 125 exactly; floats give 125.00000000000001.
        pilot = replace(self.pilot, users=100, latency_seconds=7.5)
        self.assertEqual(self.rows_for(pilot)['gpu_sequences'].projected, 125)
        pilot = replace(self.pilot, users=100, latency_seconds=0.6)  # 1,000/60 x 0.6 = 10
        self.assertEqual(self.rows_for(pilot)['gpu_sequences'].projected, 10)
        pilot = replace(self.pilot, users=101, latency_seconds=7.5)  # 126.25
        self.assertEqual(self.rows_for(pilot)['gpu_sequences'].projected, 127)

    def rows_for(self, pilot):
        return {row.resource: row for row in project(pilot, self.limits, factories=10,
                                                      allowed_growth=10)}

    def test_more_gpus_change_only_the_gpu_row(self):
        self.limits = replace(self.limits, gpu_sequences=gpu_sequence_limit(gpus=2, **GPU))
        rows = self.rows()
        self.assertEqual(rows['gpu_sequences'].utilization, Fraction(42, 52))
        self.assertEqual(rows['gpu_sequences'].headroom, 10)
        self.assertEqual(rows['exact_search'].utilization, Fraction(8))

    def test_limit_basis_is_explicit(self):
        bases = {row.resource: row.basis for row in project(self.pilot, self.limits,
                                                            factories=10, allowed_growth=10)}
        self.assertEqual(bases, {'exact_search': 'design', 'gpu_sequences': 'derived',
                                 'raw_vectors': 'assumption', 'ingest_burst': 'assumption',
                                 'audit_log': 'assumption'})
        with self.assertRaises(ValueError):
            Projection('x', 'B', 1, 1, 1, 'guess')

    def test_growth_arguments_fail_closed(self):
        for factories, allowed_growth in ((0, 1), (10, 0), (10, 11), (True, 1), (10, 1.0),
                                          (-1, 1), ('10', 1)):
            with self.subTest(factories=factories, allowed_growth=allowed_growth):
                with self.assertRaises(ValueError):
                    project(self.pilot, self.limits, factories=factories,
                            allowed_growth=allowed_growth)
        with self.assertRaises(TypeError):
            project({'chunks': 1}, self.limits, factories=1, allowed_growth=1)

    def test_pilot_and_limit_values_fail_closed(self):
        for field, value in (('chunks', 0), ('users', True), ('dims', 768.0), ('latency_seconds', 0),
                             ('latency_seconds', float('nan')), ('widest_allowed_chunks', 1_000_001),
                             ('audit_bytes_per_request', -1)):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    replace(self.pilot, **{field: value})
        for field in ('gpu_sequences', 'vector_bytes', 'ingest_per_second', 'audit_bytes_per_day'):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    replace(self.limits, **{field: 0})

    def test_results_are_frozen_integers(self):
        row = project(self.pilot, self.limits, factories=10, allowed_growth=10)[0]
        self.assertIs(type(row.projected), int)
        with self.assertRaises(FrozenInstanceError):
            row.limit = 50_000
        with self.assertRaises(FrozenInstanceError):
            self.pilot.chunks = 1


if __name__ == '__main__':
    unittest.main()
