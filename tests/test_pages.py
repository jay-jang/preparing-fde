"""Static checks of the presentation build: frame structure, on-screen limits, evidence data, the zoom
atlas geometry (nested circles and portals), camera targets, presenter paths and the handout appendix."""
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import atlas  # noqa: E402
import build_pages  # noqa: E402

MAIN = ['0-1'] + [f'{c}-{n}' for c in '1234' for n in range(1, 6)] + ['0-2']
PLAYBOOK = [f'5-{n}' for n in range(1, 7)]
SCALE = [f'6-{n}' for n in range(1, 7)]
LINK = re.compile(r'\[[^\]]+\]\((https?://[^)\s]+)\)')


def corners(node):
    """World corners of a rotated box."""
    a = math.radians(node['rot'])
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        dx, dy = sx * node['w'] / 2, sy * node['h'] / 2
        yield node['x'] + dx * math.cos(a) - dy * math.sin(a), node['y'] + dx * math.sin(a) + dy * math.cos(a)


class PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = build_pages.SOURCE.read_text(encoding='utf-8')
        cls.intro, cls.frames, cls.appendix = build_pages.parse(cls.source)
        cls.world, payload = atlas.render(cls.frames, cls.appendix, build_pages.render, 'test')
        cls.data = json.loads(payload)
        cls.nodes = cls.data['nodes']

    def test_frames_follow_the_story(self):
        self.assertEqual([frame['key'] for frame in self.frames], MAIN + PLAYBOOK + SCALE)

    def test_on_screen_copy_stays_short(self):
        for frame in self.frames:
            with self.subTest(frame=frame['key']):
                self.assertLessEqual(len(frame['message']), 60)
                self.assertLessEqual(len(frame['label']), 12)
                self.assertEqual(len(frame['points']), 3)
                self.assertTrue(all(len(point) <= 32 for point in frame['points']))
                self.assertLessEqual(len(frame['code']['text'].splitlines()), 24)

    def test_every_frame_carries_backed_evidence_data(self):
        for frame in self.frames:
            with self.subTest(frame=frame['key']):
                rows = frame['data']
                self.assertTrue(4 <= len(rows) <= 7, len(rows))
                backed = [row for row in rows if '<a href="https://' in row['basis']
                          or row['basis'].startswith(('코드:', '계산:'))]
                self.assertGreaterEqual(len(backed), 2)
                self.assertTrue(all(re.search(r'\d', re.sub(r'<[^>]+>', '', row['value'])) for row in rows))

    def test_every_camera_target_exists_once(self):
        ids = Counter(re.findall(r'\sid="([^"]+)"', self.world))
        self.assertEqual([name for name, count in ids.items() if count > 1], [])
        for node in self.nodes:
            if node != 'overview':
                self.assertIn('n-' + node.replace('.', '-'), ids, node)
        for step in self.data['steps']:
            self.assertIn(step['node'], self.nodes, step['id'])

    def test_nested_geometry_fits(self):
        """Frames sit inside their planet without touching; portals sit inside their frame circle."""
        containers = {key: node for key, node in self.nodes.items() if node['kind'] in ('hub', 'planet')}
        for (a, first), (b, second) in ((x, y) for x in containers.items() for y in containers.items() if x[0] < y[0]):
            gap = math.hypot(first['x'] - second['x'], first['y'] - second['y']) - (first['w'] + second['w']) / 2
            self.assertGreater(gap, 300, f'{a} touches {b}')
        frames = {key: node for key, node in self.nodes.items() if node['kind'] == 'frame'}
        for key, frame in frames.items():
            parent = self.nodes[frame['parent']]
            reach = math.hypot(frame['x'] - parent['x'], frame['y'] - parent['y']) + frame['w'] / 2
            self.assertLess(reach, parent['w'] / 2, key)
            for other_key, other in frames.items():
                if other_key > key and other['parent'] == frame['parent']:
                    self.assertGreater(math.hypot(frame['x'] - other['x'], frame['y'] - other['y']), frame['w'], key)
            for kind in atlas.PORTAL_KINDS:
                portal = self.nodes[f'{key}.{kind}']
                for x, y in corners(portal):
                    self.assertLess(math.hypot(x - frame['x'], y - frame['y']), frame['w'] / 2, f'{key}.{kind}')
        overview = self.nodes['overview']
        for key, node in containers.items():
            self.assertLessEqual(abs(node['x']) + node['w'] / 2, overview['w'] / 2, key)
            self.assertLessEqual(abs(node['y']) + node['h'] / 2, overview['h'] / 2, key)

    def test_paths_use_known_steps(self):
        known = {step['id'] for step in self.data['steps']}
        for name, path in self.data['paths'].items():
            with self.subTest(path=name):
                self.assertTrue(set(path['steps']) <= known)
                self.assertGreater(path['minutes'], 0)

    def test_interview_paths_zoom_once_per_frame(self):
        steps = {step['id']: step for step in self.data['steps']}
        levels = {frame['key']: frame['level'] for frame in self.frames}
        for name, order, minutes in (('interview', atlas.INTERVIEW, 15), ('deep', atlas.DEEP, 30)):
            with self.subTest(path=name):
                path = self.data['paths'][name]
                self.assertEqual([sid for sid in path['steps'] if steps[sid]['kind'] == 'frame'], order)
                self.assertEqual(path['minutes'], minutes)
                for key in order:
                    detail = 'diagram' if levels[key] < 3 else 'code'
                    self.assertEqual(path['steps'][path['steps'].index(key) + 1], f'{key}.{detail}')
                self.assertEqual(path['steps'][:2], ['overview', 'hub'])
                self.assertEqual(path['steps'][-1], 'closing')

    def test_onboarding_visits_every_planet_frame_and_detail(self):
        steps = self.data['paths']['onboarding']['steps']
        frames = [sid for sid in steps if sid in {frame['key'] for frame in self.frames}]
        self.assertEqual(frames, MAIN + PLAYBOOK + SCALE)
        for key in frames:
            position = steps.index(key)
            self.assertEqual(steps[position + 1:position + 4], [f'{key}.diagram', f'{key}.code', f'{key}.data'])
        self.assertEqual([sid for sid in steps if sid.startswith('p')], [f'p{n}' for n in range(1, 7)])
        self.assertEqual(steps[steps.index('0-2.data') + 1:steps.index('0-2.data') + 3], ['closing', 'p5'])
        self.assertEqual(steps[-1], 'overview')
        for part, keys in (('part1', MAIN), ('part2', PLAYBOOK), ('part3', SCALE)):
            self.assertEqual(self.data['paths'][part]['frames'], keys)

    def test_payload_cannot_close_its_script_element(self):
        frames = [dict(frame, notes='</script><p>x</p>') for frame in self.frames]
        _, payload = atlas.render(frames, self.appendix, build_pages.render, 'test')
        self.assertNotIn('</script>', payload)


class HandoutTests(unittest.TestCase):
    """The appendix doubles as interview preparation: questions, roleplays, glossary and sources."""

    @classmethod
    def setUpClass(cls):
        cls.source = build_pages.SOURCE.read_text(encoding='utf-8')
        _, cls.frames, cls.appendix = build_pages.parse(cls.source)

    def section(self, title):
        match = re.search(rf'^## {re.escape(title)}\n([\s\S]*?)(?=^## |\Z)', self.appendix, re.MULTILINE)
        self.assertIsNotNone(match, title)
        return match[1]

    def test_follow_up_questions_cover_the_new_frames(self):
        rows = re.findall(r'^\| (?!질문 \|)(?!-)(.+?) \| (.+) \|$', self.section('면접 후속 질문과 설계 판단의 근거'),
                          re.MULTILINE)
        self.assertGreaterEqual(len(rows), 30)
        cited = set(re.findall(r'\b(\d-\d)\b', ' '.join(answer for _, answer in rows)))
        self.assertLessEqual({frame['key'] for frame in self.frames if frame['key'][0] in '123456'} - cited, set())

    def test_roleplays_and_glossary(self):
        self.assertGreaterEqual(len(re.findall(r'^### \d+\. ', self.section('고객 상황 롤플레이'), re.MULTILINE)), 8)
        terms = re.findall(r'^\| (?!용어 \|)(?!-)(.+?) \| (.+?) \| (.+?) \|$', self.section('용어집'), re.MULTILINE)
        self.assertGreaterEqual(len(terms), 40)

    def test_sources_are_https_and_cited(self):
        links = set(LINK.findall(self.section('주요 공식 자료와 활용 위치')))
        self.assertGreaterEqual(len(links), 42)
        everything = set(LINK.findall(self.source))
        self.assertTrue(all(url.startswith('https://') for url in everything))


if __name__ == '__main__':
    unittest.main()
