# -*- coding: utf-8 -*-
"""逐字输入层的纯逻辑测试：不发真实按键，只验证拆分和节奏区间。"""

import os
import ctypes
import random
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from wechatauto import rhythm  # noqa: E402


class TestTypingPlan(unittest.TestCase):
    def test_input_struct_size_matches_windows(self):
        # SendInput 的 cbSize 不对会整批返回 0，输入框只留下别的按键（如换行）
        self.assertEqual(ctypes.sizeof(rhythm._INPUT), 40)

    def test_cjk_units_and_delay(self):
        plan = rhythm.typing_plan('中', rng=random.Random(1), fast=False)
        self.assertEqual(plan[0][0], 'unit')
        self.assertEqual(plan[0][1], ord('中'))
        self.assertGreaterEqual(plan[0][2], 0.12)
        self.assertLessEqual(plan[0][2], 0.28)

    def test_ascii_units_and_delay(self):
        plan = rhythm.typing_plan('a', rng=random.Random(1), fast=False)
        self.assertEqual(plan[0][1], ord('a'))
        self.assertGreaterEqual(plan[0][2], 0.05)
        self.assertLessEqual(plan[0][2], 0.14)

    def test_punctuation_adds_pause(self):
        plan = rhythm.typing_plan('中，', rng=random.Random(2), fast=False)
        pauses = [p for p in plan if p[0] == 'pause']
        self.assertEqual(len(pauses), 1)
        self.assertGreaterEqual(pauses[0][2], 0.20)
        self.assertLessEqual(pauses[0][2], 0.60)

    def test_newline_is_shift_enter(self):
        plan = rhythm.typing_plan('a\nb', rng=random.Random(3), fast=False)
        kinds = [p[0] for p in plan]
        self.assertIn('newline', kinds)
        self.assertNotIn(ord('\n'), [p[1] for p in plan if p[0] == 'unit'])
        nl = next(p for p in plan if p[0] == 'newline')
        self.assertGreaterEqual(nl[2], 0.15)
        self.assertLessEqual(nl[2], 0.35)

    def test_emoji_is_surrogate_pair(self):
        plan = rhythm.typing_plan('😀', rng=random.Random(4), fast=False)
        units = [p[1] for p in plan if p[0] == 'unit']
        self.assertEqual(len(units), 2)
        self.assertTrue(all(0xD800 <= u <= 0xDFFF for u in units))
        self.assertEqual(plan[0][2], 0.005)      # 代理对之间紧接
        self.assertGreaterEqual(plan[1][2], 0.05)

    def test_tab_becomes_four_spaces(self):
        plan = rhythm.typing_plan('\t', rng=random.Random(5), fast=False)
        self.assertEqual([p[1] for p in plan if p[0] == 'unit'], [0x20] * 4)

    def test_long_text_uses_fast_range(self):
        plan = rhythm.typing_plan('a' * 301, rng=random.Random(6))
        delays = [p[2] for p in plan if p[0] == 'unit']
        self.assertTrue(delays)
        self.assertLessEqual(max(delays), 0.06)

    def test_empty_text(self):
        self.assertEqual(rhythm.typing_plan('', rng=random.Random(7)), [])

    def test_type_text_sends_full_sequence(self):
        units, newlines = [], []
        with mock.patch.object(rhythm, '_send_unicode_unit',
                               side_effect=units.append), \
                mock.patch.object(rhythm, '_press_shift_enter',
                                  side_effect=lambda: newlines.append(1)), \
                mock.patch.object(rhythm.time, 'sleep', return_value=None):
            total = rhythm.type_text('中a\n😀\t', rng=random.Random(8),
                                     fast=False)
        self.assertEqual(units, [ord('中'), ord('a'), 0xD83D, 0xDE00,
                                 *([0x20] * 4)])
        self.assertEqual(newlines, [1])
        self.assertGreater(total, 0)


if __name__ == '__main__':
    unittest.main()
