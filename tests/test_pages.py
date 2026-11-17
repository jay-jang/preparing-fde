"""Static checks of the presentation build: frame structure, on-screen limits, camera targets and paths."""
from collections import Counter
import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import build_pages  # noqa: E402
import deck  # noqa: E402

MAIN = ['0-1', '1-1', '1-2', '1-3', '2-1', '2-2', '2-3', '3-1', '3-2', '3-3', '4-1', '4-2', '4-3', '0-2']
PLAYBOOK = ['5-1', '5-2', '5-3']
INTERVIEW = ['0-1', '1-1', '1-2', '2-1', '2-2', '2-3', '3-1', '4-1', '0-2']


class PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, cls.frames, appendix = build_pages.parse(build_pages.SOURCE.read_text(encoding='utf-8'))
        cls.canvas, payload = deck.render(cls.frames, build_pages.render(appendix), 'test')
        cls.data = json.loads(payload)

    def test_frames_follow_the_story(self):
        self.assertEqual([frame['key'] for frame in self.frames], MAIN + PLAYBOOK)

    def test_on_screen_copy_stays_short(self):
        for frame in self.frames:
            with self.subTest(frame=frame['key']):
                self.assertLessEqual(len(frame['message']), 60)
                self.assertEqual(len(frame['points']), 3)
                self.assertTrue(all(len(point) <= 32 for point in frame['points']))
                self.assertLessEqual(len(frame['code']['text'].splitlines()), 24)

    def test_every_camera_target_exists_once(self):
        ids = Counter(re.findall(r'\sid="([^"]+)"', self.canvas))
        duplicates = [name for name, count in ids.items() if count > 1]
        self.assertEqual(duplicates, [])
        for step in self.data['steps']:
            self.assertIn(step['target'], ids, step['id'])

    def test_paths_use_known_steps(self):
        known = {step['id'] for step in self.data['steps']}
        for name, path in self.data['paths'].items():
            with self.subTest(path=name):
                self.assertTrue(set(path['steps']) <= known)
                self.assertGreater(path['minutes'], 0)

    def test_interview_path_matches_handout(self):
        steps = {step['id']: step for step in self.data['steps']}
        path = self.data['paths']['interview']['steps']
        cards = [sid for sid in path if steps[sid]['kind'] == 'card']
        self.assertEqual(cards, INTERVIEW)
        self.assertEqual(self.data['paths']['interview']['minutes'], 15)
        # One detail per frame: the diagram for Level 1-2, the code for Level 3; end on the 0-2 outcomes.
        levels = {frame['key']: frame['level'] for frame in self.frames}
        for key in INTERVIEW:
            expected = f'{key}.diagram' if levels[key] < 3 else f'{key}.code'
            self.assertEqual(path[path.index(key) + 1], expected)
        self.assertEqual(path[-1], 'closing')

    def test_onboarding_covers_every_frame_with_overview_returns(self):
        steps = self.data['paths']['onboarding']['steps']
        cards = [sid for sid in steps if sid in {frame['key'] for frame in self.frames}]
        self.assertEqual(cards, MAIN + PLAYBOOK)
        self.assertEqual(steps.count('overview'), 4)
        self.assertEqual(steps[steps.index('0-2.code') + 1:steps.index('0-2.code') + 3], ['closing', 'c5'])
        self.assertEqual(steps[-1], 'canvas')

    def test_payload_cannot_close_its_script_element(self):
        _, payload = deck.render(self.frames, '</script><p>x</p>', 'test')
        self.assertNotIn('</script>', payload)


if __name__ == '__main__':
    unittest.main()
