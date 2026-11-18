# -*- coding: utf-8 -*-
"""虚拟屏开关关闭后「启动时拉回主屏」的纯逻辑测试。

只测判定函数，不移动真实窗口、不需要微信在运行：
- 哪些设备描述算虚拟显示器；
- 给定窗口矩形与显示器列表，什么时候该拉回主屏、什么时候不该动。
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dafeiyu_bot import _is_virtual_display, _needs_primary_recovery  # noqa: E402

PHYS = ('NVIDIA GeForce GT 740', (0, 0, 1920, 1080))
PHYS2 = ('Intel(R) UHD Graphics 630', (1920, 0, 3840, 1080))
IDD = ('IddSampleDriver Device', (1920, 0, 3840, 1080))


class TestVirtualDisplay(unittest.TestCase):
    def test_known_virtual_adapters(self):
        for name in ('IddSampleDriver Device', 'OrayIddDriver Device',
                     'GameViewer Virtual Display Adapter',
                     'USB Mobile Monitor Virtual Display',
                     'Some Virtual Display Adapter'):
            self.assertTrue(_is_virtual_display(name), name)

    def test_physical_adapters(self):
        for name in ('NVIDIA GeForce GT 740', 'Intel(R) UHD Graphics 630',
                     'AMD Radeon RX 580', ''):
            self.assertFalse(_is_virtual_display(name), name)


class TestNeedsPrimaryRecovery(unittest.TestCase):
    def test_window_fully_on_virtual_display(self):
        # 2026-11-17 实测的真实位置：微信整块在 IddSampleDriver 上
        self.assertTrue(_needs_primary_recovery(
            (1940, 20, 2840, 670), [PHYS, IDD]))

    def test_window_on_primary_physical(self):
        self.assertFalse(_needs_primary_recovery(
            (100, 100, 1000, 700), [PHYS, IDD]))

    def test_window_on_second_physical_monitor(self):
        self.assertFalse(_needs_primary_recovery(
            (2000, 100, 2900, 700), [PHYS, PHYS2]))

    def test_straddling_with_enough_visible_area(self):
        # 900x600 的窗口有 420x600 落在物理屏，约 47%，算看得见
        self.assertFalse(_needs_primary_recovery(
            (1500, 100, 2400, 700), [PHYS, IDD]))

    def test_mostly_on_virtual_display(self):
        # 只有 120x600（约 13%）在物理屏，主体仍在虚拟屏，应该拉回
        self.assertTrue(_needs_primary_recovery(
            (1800, 100, 2700, 700), [PHYS, IDD]))

    def test_center_on_physical_counts_as_visible(self):
        self.assertFalse(_needs_primary_recovery(
            (700, 100, 1800, 700), [PHYS, IDD]))

    def test_offscreen_window(self):
        self.assertTrue(_needs_primary_recovery(
            (-32000, -32000, -31000, -31500), [PHYS, IDD]))

    def test_no_display_information_means_no_move(self):
        self.assertFalse(_needs_primary_recovery((1940, 20, 2840, 670), []))

    def test_only_virtual_display_keeps_window(self):
        # 纯远程/无头机只有虚拟屏：窗口就在那块屏里时不要重排
        self.assertFalse(_needs_primary_recovery(
            (2000, 100, 2900, 700), [IDD]))

    def test_only_virtual_display_but_window_offscreen(self):
        self.assertTrue(_needs_primary_recovery(
            (-32000, -32000, -31000, -31500), [IDD]))

    def test_zero_size_window(self):
        self.assertTrue(_needs_primary_recovery((0, 0, 0, 0), [PHYS, IDD]))


if __name__ == '__main__':
    unittest.main()