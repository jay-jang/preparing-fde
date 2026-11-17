from dataclasses import FrozenInstanceError, replace
import math
import unittest

from fde.metrics import (SLICES, Label, Query, Retrieved, paired_bootstrap, score_query,
                         score_run, summarize)

DOC, SEC, ALARM = 'manual-p101', '5 정비 > 5.2 과열', '3 경보 > 3.4 ZX-19'
LABELS = [Label(DOC, 3, SEC, (42, 43), grade=3), Label(DOC, 3, ALARM, (18, 18), grade=1)]
# Synthetic rankings for the running case; NOT retrieval measurements.
FIXED = [Retrieved('fx-0031', DOC, 3, '5 정비 > 5.1 소음', (40, 40)),
         Retrieved('fx-0034', DOC, 2, SEC, (42, 42)),
         Retrieved('fx-0035', DOC, 3, SEC, (43, 43)),
         Retrieved('fx-0036', DOC, 3, SEC, (43, 43)),
         Retrieved('fx-0011', DOC, 3, ALARM, (18, 18))]
STRUCTURED = [Retrieved('manual-p101:v3:0002', DOC, 3, SEC, (42, 43)),
              Retrieved('manual-p101:v3:0003', DOC, 3, SEC, (43, 44)),
              *(Retrieved(f'manual-p101:v3:{n:04d}', DOC, 3, '6 부품', (n + 43, n + 43))
                for n in (7, 8, 9))]
IDCG = 3 + 1 / math.log2(3)


def hits(prefix, total, misses):
    return {f'{prefix}{i:03d}': int(i >= misses) for i in range(total)}


class ScoreQueryTests(unittest.TestCase):
    def test_matches_scikit_learn_documented_example(self):
        # ndcg_score([[10, 0, 0, 1, 5]], ...) is shown as 0.69, 0.49 and 0.35 (k=4).
        a, b, c = (Label('d', 1, None, (p, p), g) for p, g in ((1, 10), (4, 1), (5, 5)))
        x, y, ra, rb, rc = (Retrieved(n, 'd', 1, '', (p, p))
                            for n, p in (('x', 2), ('y', 3), ('A', 1), ('B', 4), ('C', 5)))
        ideal = 10 + 5 / math.log2(3) + 1 / 2
        first = score_query([rc, rb, y, x, ra], [a, b, c]).ndcg
        second = score_query([x, y, rb, ra, rc], [a, b, c]).ndcg
        cut = score_query([x, y, rb, ra, rc], [a, b, c], k=4).ndcg
        self.assertAlmostEqual(first, (5 + 1 / math.log2(3) + 10 / math.log2(6)) / ideal, places=15)
        self.assertAlmostEqual(cut, (1 / 2 + 10 / math.log2(5)) / ideal, places=15)
        self.assertEqual([math.floor(v * 100) / 100 for v in (first, second, cut)], [0.69, 0.49, 0.35])
        self.assertEqual(round(first, 4), 0.6957)
        self.assertEqual(round(second, 4), 0.4937)
        self.assertEqual(round(cut, 4), 0.352)

    def test_running_case_fixed_versus_structured(self):
        fixed, structured = score_query(FIXED, LABELS), score_query(STRUCTURED, LABELS)
        self.assertEqual((fixed.hit, fixed.recall, fixed.rr), (1, 1.0, 1 / 3))
        self.assertAlmostEqual(fixed.ndcg, (3 / 2 + 1 / math.log2(6)) / IDCG, places=15)
        self.assertEqual((structured.hit, structured.recall, structured.rr), (1, 0.5, 1.0))
        self.assertAlmostEqual(structured.ndcg, 3 / IDCG, places=15)
        self.assertEqual(str(fixed), 'hit=1 recall=1.0000 rr=0.3333 ndcg=0.5197')
        self.assertEqual(str(structured), 'hit=1 recall=0.5000 rr=1.0000 ndcg=0.8262')

    def test_labels_survive_rechunking_and_renamed_ids(self):
        renamed = [replace(r, chunk_id=f'other-strategy-{i}') for i, r in enumerate(FIXED)]
        self.assertEqual(score_query(renamed, LABELS), score_query(FIXED, LABELS))

    def test_version_section_and_page_rules(self):
        label = LABELS[0]
        self.assertTrue(Retrieved('a', DOC, 3, SEC, (43, 44)).matches(label))
        self.assertTrue(Retrieved('b', DOC, 3, SEC + ' > 표 5-3', (43, 43)).matches(label))
        for miss in (Retrieved('c', DOC, 2, SEC, (42, 42)),            # older version
                     Retrieved('d', 'manual-p102', 3, SEC, (42, 42)),  # other document
                     Retrieved('e', DOC, 3, SEC, (44, 45)),            # pages do not overlap
                     Retrieved('f', DOC, 3, '5 정비 > 5.2 과열기', (42, 42)),
                     Retrieved('g', DOC, 3, '5 정비', (42, 42))):      # parent is not inside
            self.assertFalse(miss.matches(label), miss.chunk_id)

    def test_redundant_chunks_never_exceed_ideal(self):
        many = [Retrieved(f'c{i}', DOC, 3, SEC, (42, 42)) for i in range(5)]
        self.assertAlmostEqual(score_query(many, LABELS).ndcg, 3 / IDCG, places=15)
        ideal = [Retrieved('p', DOC, 3, SEC, (42, 43)), Retrieved('z', DOC, 3, ALARM, (18, 18))]
        self.assertEqual(score_query(ideal, LABELS).ndcg, 1.0)

    def test_cutoff_and_denominators(self):
        noise = [Retrieved(f'n{i}', DOC, 3, '9 부록', (90, 90)) for i in range(5)]
        late = noise + [Retrieved('late', DOC, 3, SEC, (42, 42))]
        self.assertEqual(score_query(late, LABELS, k=5), score_query(noise, LABELS, k=5))
        self.assertEqual((score_query(noise, LABELS).hit, score_query(noise, LABELS).ndcg), (0, 0.0))
        found = score_query(late, LABELS, k=6)
        self.assertEqual((found.hit, found.recall, found.rr), (1, 0.5, 1 / 6))
        three = LABELS + [Label(DOC, 3, '7 윤활', (60, 61), 2)]
        self.assertEqual(score_query(STRUCTURED, three).recall, 1 / 3)   # Hit@5 is still 1
        self.assertEqual(len(STRUCTURED), 5)                             # a full top-5

    def test_one_chunk_covering_two_labels(self):
        # Recall counts both labels; nDCG gives one rank one gain, so it stays <= 1.
        near = [Label(DOC, 3, SEC, (42, 42), 3), Label(DOC, 3, SEC, (43, 43), 1)]
        wide = score_query([Retrieved('w', DOC, 3, SEC, (42, 43))], near)
        self.assertEqual((wide.hit, wide.recall, wide.rr), (1, 1.0, 1.0))
        self.assertAlmostEqual(wide.ndcg, 3 / IDCG, places=15)
        both = score_query([Retrieved('w', DOC, 3, SEC, (42, 43)),
                            Retrieved('v', DOC, 3, SEC, (43, 43))], near)
        self.assertEqual((both.recall, both.ndcg), (1.0, 1.0))

    def test_invalid_inputs_fail_closed(self):
        with self.assertRaises(ValueError):
            score_query(FIXED, [])
        with self.assertRaises(ValueError):
            score_query(FIXED + FIXED[:1], LABELS)
        with self.assertRaises(ValueError):
            score_query(FIXED, LABELS + LABELS[:1])
        for k in (0, 101, True, 5.0):
            with self.assertRaises(ValueError):
                score_query(FIXED, LABELS, k=k)
        with self.assertRaises(TypeError):
            score_query([('fx-0031', DOC)], LABELS)
        for bad in (dict(grade=0), dict(pages=(43, 42)), dict(version=0), dict(document='a b')):
            with self.assertRaises(ValueError):
                replace(LABELS[0], **bad)
        with self.assertRaises(ValueError):
            Retrieved('x', DOC, 3, None, (1, 1))
        with self.assertRaises(FrozenInstanceError):
            LABELS[0].grade = 2


class AggregateTests(unittest.TestCase):
    def test_score_run_requires_exactly_the_query_ids(self):
        queries = [Query('q1', 'factoid', tuple(LABELS)), Query('q2', 'semantic', tuple(LABELS))]
        scores = score_run(queries, {'q1': FIXED, 'q2': STRUCTURED})
        self.assertEqual(scores['q2'].recall, 0.5)
        for run in ({'q1': FIXED}, {'q1': FIXED, 'q2': [], 'q3': []}):
            with self.assertRaises(ValueError):
                score_run(queries, run)
        with self.assertRaises(ValueError):
            score_run(queries + queries[:1], {'q1': FIXED, 'q2': STRUCTURED})
        with self.assertRaises(ValueError):
            Query('q4', 'unanswerable', tuple(LABELS))
        with self.assertRaises(TypeError):
            score_run([('q1', 'factoid')], {'q1': FIXED})
        with self.assertRaises(TypeError):
            summarize(queries, {'q1': (1, 1.0, 1.0, 1.0), 'q2': scores['q2']})
        self.assertEqual(SLICES, ('factoid', 'semantic'))   # fde/release_gate.py slice names

    def test_overall_mean_hides_semantic_slice(self):
        hit, miss = [Retrieved('h', DOC, 3, SEC, (42, 42))], []
        outcome = {**hits('f', 70, 1), **hits('s', 30, 6)}   # factoid 69/70, semantic 24/30
        queries = [Query(q, 'factoid' if q[0] == 'f' else 'semantic', tuple(LABELS)) for q in outcome]
        run = {q: hit if value else miss for q, value in outcome.items()}
        summary = summarize(queries, score_run(queries, run))
        self.assertEqual(summary['all'].hit_rate, 0.93)
        self.assertEqual(summary['factoid'].hit_rate, 69 / 70)
        self.assertEqual(summary['semantic'].hit_rate, 0.8)
        self.assertEqual((summary['all'].n, summary['factoid'].n, summary['semantic'].n), (100, 70, 30))
        failing = [name for name in SLICES if summary[name].hit_rate < 0.90]
        self.assertEqual(failing, ['semantic'])   # 4-2 gate: each slice >= 90%
        with self.assertRaises(ValueError):
            summarize(queries[:-1], score_run(queries, run))


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.base = hits('s', 30, 6)                                    # 24/30
        self.cand = {**self.base, 's002': 1, 's003': 1, 's004': 1, 's005': 1, 's029': 0}

    def test_thirty_queries_cannot_separate_ten_points(self):
        result = paired_bootstrap(self.base, self.cand, seed=7)
        self.assertEqual(sum(self.cand.values()), 27)
        self.assertEqual((result.n, result.mean_difference), (30, 0.1))
        self.assertEqual((result.low, result.high), (-1 / 30, 7 / 30))
        self.assertEqual(result.verdict, 'inconclusive')
        self.assertEqual(str(result), 'n=30 diff=+0.1000 95% [-0.0333, +0.2333] inconclusive')

    def test_same_effect_on_more_queries_is_separated(self):
        base = hits('q', 120, 24)                                       # 96/120
        cand = {**base, **{f'q{i:03d}': 1 for i in range(8, 24)},
                **{f'q{i:03d}': 0 for i in range(116, 120)}}            # 16 fixed, 4 broken
        result = paired_bootstrap(base, cand, seed=7)
        self.assertEqual(sum(cand.values()), 108)
        self.assertEqual((result.mean_difference, result.low, result.high), (0.1, 4 / 120, 0.175))
        self.assertEqual(result.verdict, 'better')
        self.assertEqual(paired_bootstrap(cand, base, seed=7).verdict, 'worse')

    def test_deterministic_and_validated(self):
        self.assertEqual(paired_bootstrap(self.base, self.cand, seed=3),
                         paired_bootstrap(dict(reversed(self.base.items())), self.cand, seed=3))
        explicit = paired_bootstrap(self.base, self.cand, resamples=9999, confidence=0.95, seed=7)
        self.assertEqual(paired_bootstrap(self.base, self.cand, seed=7), explicit)   # defaults
        self.assertEqual(explicit.confidence, 0.95)
        bad_calls = [({**self.base, 'extra': 1}, self.cand, {}),
                     (self.base, {**self.cand, 's000': math.nan}, {}),
                     (self.base, self.cand, {'resamples': 999}),
                     (self.base, self.cand, {'confidence': 1.0}),
                     (self.base, self.cand, {'confidence': 1}),
                     ({'only': 1}, {'only': 0}, {})]
        for baseline, candidate, options in bad_calls:
            with self.assertRaises(ValueError):
                paired_bootstrap(baseline, candidate, **options)
        with self.assertRaises(TypeError):
            paired_bootstrap(list(self.base.values()), self.cand)


if __name__ == '__main__':
    unittest.main()
