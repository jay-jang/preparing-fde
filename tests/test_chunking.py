from dataclasses import replace
import re
import unittest

from fde.chunking import Block, chunk_blocks, estimate_tokens
from fde.provenance import AnswerRecord


def manual():
    return [
        Block('heading', '5 정비', 41, level=1),
        Block('heading', '5.2 과열', 42, level=2),
        Block('paragraph', 'P-101 과열은 ZX-19 경보와 함께 기록된다.', 42),
        Block('step', '1. 펌프를 정지하고 잠금 표지를 단다.', 42),
        Block('step', '2. 냉각수 유량과 필터 막힘을 확인한다.', 43),
        Block('step', '3. 베어링 온도를 측정한다.', 43),
        Block('table_header', '부품 | 허용 온도', 43),
        Block('table_row', '베어링 | 80°C 이하', 43),
        Block('table_row', '모터 권선 | 120°C 이하', 44),
    ]


class ChunkingTests(unittest.TestCase):
    def test_estimator_exact_values(self):
        self.assertEqual(estimate_tokens('ZX-19 trip code'), 4)
        self.assertEqual(estimate_tokens('P-101'), 2)
        self.assertEqual(estimate_tokens('과열 점검'), 4)
        self.assertEqual(estimate_tokens('P-101 과열은 ZX-19 경보와 함께 기록된다.'), 17)
        self.assertEqual(estimate_tokens(''), 0)
        with self.assertRaises(TypeError):
            estimate_tokens(b'P-101')

    def test_frame_example_keeps_procedure_and_table_whole(self):
        chunks = chunk_blocks('manual-p101', 3, manual(), max_tokens=48)
        self.assertEqual([(c.chunk_id, c.section, c.pages, c.kinds, c.tokens) for c in chunks], [
            ('manual-p101:v3:0001', '5 정비 > 5.2 과열', (42, 42), ('paragraph',), 17),
            ('manual-p101:v3:0002', '5 정비 > 5.2 과열', (42, 43), ('procedure',), 45),
            ('manual-p101:v3:0003', '5 정비 > 5.2 과열', (43, 44), ('table',), 24),
        ])
        self.assertTrue(chunks[2].text.startswith('부품 | 허용 온도\n'))
        self.assertTrue(all(c.part is None for c in chunks))

    def test_units_pack_together_within_one_section(self):
        chunks = chunk_blocks('manual-p101', 3, manual(), max_tokens=80)
        self.assertEqual([(c.kinds, c.pages, c.tokens) for c in chunks],
                         [(('paragraph', 'procedure'), (42, 43), 62), (('table',), (43, 44), 24)])

    def test_oversized_unit_raises_instead_of_truncating(self):
        with self.assertRaisesRegex(ValueError, 'procedure of 45 tokens exceeds 40'):
            chunk_blocks('manual-p101', 3, manual(), max_tokens=40)

    def test_explicit_split_only_at_step_boundaries(self):
        chunks = chunk_blocks('manual-p101', 3, manual(), max_tokens=40, split_oversized=True)
        procedure = [c for c in chunks if c.kinds == ('procedure',)]
        self.assertEqual([(c.part, c.pages, c.tokens) for c in procedure],
                         [((1, 2), (42, 43), 33), ((2, 2), (43, 43), 12)])
        self.assertEqual(procedure[1].text, '3. 베어링 온도를 측정한다.')

    def test_split_table_repeats_header_in_every_part(self):
        chunks = chunk_blocks('manual-p101', 3, manual(), max_tokens=17, split_oversized=True)
        tables = [c for c in chunks if c.kinds == ('table',)]
        self.assertEqual([(c.part, c.tokens) for c in tables], [((1, 2), 15), ((2, 2), 16)])
        self.assertTrue(all(c.text.startswith('부품 | 허용 온도\n') for c in tables))
        self.assertEqual([c.text.count('부품 | 허용 온도') for c in tables], [1, 1])
        self.assertTrue(all(c.tokens <= 17 for c in chunks))

    def test_header_plus_one_row_over_budget_still_raises(self):
        with self.assertRaisesRegex(ValueError, 'table boundary item exceeds 14'):
            chunk_blocks('manual-p101', 3, manual()[:2] + manual()[6:], max_tokens=14,
                         split_oversized=True)

    def test_paragraph_splits_at_sentences_and_codes_stay_whole(self):
        text = 'P-101 과열 시 ZX-19 경보가 뜬다. 냉각수 밸브 V-12 개도를 본다. 필터 F-7 차압을 기록한다.'
        blocks = [Block('heading', '5.2 과열', 42, level=1), Block('paragraph', text, 42)]
        chunks = chunk_blocks('manual-p101', 3, blocks, max_tokens=25, split_oversized=True)
        self.assertEqual([c.part for c in chunks], [(1, 2), (2, 2)])
        joined = ' '.join(c.text for c in chunks)
        self.assertEqual(joined, text)
        for code in re.findall(r'[A-Z]{1,3}-\d+', text):
            self.assertTrue(any(code in c.text for c in chunks))
        long_sentence = Block('paragraph', 'P-101 ' * 30, 42)
        with self.assertRaisesRegex(ValueError, 'paragraph boundary item exceeds'):
            chunk_blocks('manual-p101', 3, blocks[:1] + [long_sentence], max_tokens=20,
                         split_oversized=True)

    def test_new_step_one_starts_the_next_procedure(self):
        second = [Block('step', '1. 필터를 교체한다.', 44), Block('step', '2. 펌프를 재가동한다.', 44)]
        chunks = chunk_blocks('manual-p101', 3, manual()[:6] + second, max_tokens=512)
        self.assertEqual([(c.kinds, c.pages, c.tokens) for c in chunks],
                         [(('paragraph', 'procedure', 'procedure'), (42, 44), 81)])

    def test_chunk_never_spans_two_sections(self):
        blocks = [Block('heading', '5.1 소음', 40, level=1), Block('paragraph', '베어링 소음.', 40),
                  Block('heading', '5.2 과열', 42, level=1), Block('paragraph', '냉각수 확인.', 42)]
        chunks = chunk_blocks('manual-p101', 3, blocks, max_tokens=512)
        self.assertEqual([c.section for c in chunks], ['5.1 소음', '5.2 과열'])

    def test_parser_damage_fails_closed(self):
        note = Block('paragraph', '주의: 잠금 상태를 확인한다.', 42)
        broken = {
            'out of sequence': manual()[:4] + [manual()[5]],
            'does not continue a procedure': manual()[:4] + [note] + manual()[4:],
            'row without its header': manual()[:2] + [manual()[7]],
            'header without rows': manual()[:2] + [manual()[6]],
            'must not go backwards': manual()[:3] + [replace(manual()[3], page=40)],
            'level skipped': manual()[:1] + [Block('heading', '5.2.1 상세', 42, level=3), note],
            'before the first heading': [note] + manual(),
            'no content blocks': manual()[:2],
        }
        for message, blocks in broken.items():
            with self.subTest(message), self.assertRaisesRegex(ValueError, message):
                chunk_blocks('manual-p101', 3, blocks, max_tokens=512)
        for bad in [dict(kind='image'), dict(text=' '), dict(page=0), dict(level=1)]:
            with self.subTest(bad), self.assertRaises(ValueError):
                Block(**{'kind': 'paragraph', 'text': 'x', 'page': 1, **bad})
        for doc_id, version in [('manual p101', 3), ('manual-p101', 0), ('manual-p101', True)]:
            with self.assertRaises(ValueError):
                chunk_blocks(doc_id, version, manual(), max_tokens=512)

    def test_ids_are_deterministic_and_hash_tracks_content(self):
        first = chunk_blocks('manual-p101', 3, manual(), max_tokens=48)
        self.assertEqual(first, chunk_blocks('manual-p101', 3, manual(), max_tokens=48))
        edited = manual()
        edited[8] = replace(edited[8], text='모터 권선 | 130°C 이하')
        second = chunk_blocks('manual-p101', 3, edited, max_tokens=48)
        self.assertEqual([c.chunk_id for c in first], [c.chunk_id for c in second])
        self.assertEqual([a.content_hash == b.content_hash for a, b in zip(first, second)],
                         [True, True, False])
        newer = chunk_blocks('manual-p101', 4, manual(), max_tokens=48)
        self.assertEqual(newer[0].chunk_id, 'manual-p101:v4:0001')

    def test_chunk_ids_fit_the_answer_record_format(self):
        chunks = chunk_blocks('manual-p101', 3, manual(), max_tokens=48)
        record = AnswerRecord(
            request_id='12345678-1234-4abc-8def-1234567890ab', tenant='fixture-factory',
            subject_pseudonym='0' * 64, key_id='fixture-v1', snapshot_id='backup-v1',
            source_positions=(('manual', 8),), policy_version='policy-v1',
            index_version='index-v1', embedding_model='embedding-v1', reranker=None,
            prompt_version='prompt-v1', model_digest='sha256:' + 'a' * 64,
            evidence=tuple((c.chunk_id, c.version) for c in chunks), abstained=False)
        self.assertEqual(len(record.evidence), 3)


if __name__ == '__main__':
    unittest.main()
