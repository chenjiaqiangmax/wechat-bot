# -*- coding: utf-8 -*-
"""发送前的窗口行为：回复只置前微信，不再碰用户其他程序的窗口。

2026-11-18 用户报告：机器人一回复，其他程序窗口全部被最小化。
根因是 ``WeChatGUI.ensure_visible()`` 无条件调用 ``_minimize_blockers()``，
而后者按「与微信主窗口矩形相交」枚举所有非微信窗口并 SW_MINIMIZE；
微信窗口一大，用户所有程序都相交。这里锁住修复后的行为。
"""

import unittest
from unittest.mock import Mock, patch


class TestEnsureVisibleDoesNotTouchOtherWindows(unittest.TestCase):
    """``ensure_visible`` 只能抢微信前台，不能最小化任何其他窗口。"""

    def _gui(self, alive=True):
        from wechatauto.guia import WeChatGUI
        gui = object.__new__(WeChatGUI)
        gui._last_visible_ok = False
        gui._last_visible_ts = 0
        # 只要发送路径还调用它，测试就失败——这比看日志更早拦回归。
        gui._minimize_blockers = Mock(
            side_effect=AssertionError('回复时不应最小化其他程序的窗口'))
        gui.bring_to_front = Mock(return_value=True)
        gui._update_render_rect = Mock()
        gui.is_alive = Mock(return_value=alive)
        return gui

    def test_ensure_visible_only_brings_wechat_to_front(self):
        gui = self._gui(alive=True)
        with patch('wechatauto.guia.time.sleep', lambda *_: None):
            self.assertTrue(gui.ensure_visible())
        gui._minimize_blockers.assert_not_called()
        gui.bring_to_front.assert_called_once_with(keep_topmost=True)

    def test_ensure_visible_failure_does_not_minimize_either(self):
        gui = self._gui(alive=False)
        with patch('wechatauto.guia.time.sleep', lambda *_: None):
            self.assertFalse(gui.ensure_visible())
        gui._minimize_blockers.assert_not_called()
        gui.bring_to_front.assert_called_once_with(keep_topmost=True)


class TestBringToFrontRestoresMinimized(unittest.TestCase):
    """最小化但仍是前台窗口时，bring_to_front 必须先还原再算成功。"""

    def test_minimized_foreground_window_is_restored(self):
        from types import SimpleNamespace
        from wechatauto import guia

        gui = object.__new__(guia.WeChatGUI)
        gui.main_hwnd = 1
        gui.render_hwnd = 0
        iconic = [True]
        calls = []

        class _U:
            def GetForegroundWindow(self):
                return 1

            def IsIconic(self, hwnd):
                return iconic[0]

            def GetWindowThreadProcessId(self, hwnd, out):
                return 0

            def SetWindowPos(self, *args):
                pass

            def SetForegroundWindow(self, hwnd):
                calls.append(('foreground', hwnd))

            def SetActiveWindow(self, hwnd):
                pass

            def BringWindowToTop(self, hwnd):
                pass

            def GetFocus(self):
                return 0

            def SystemParametersInfoW(self, *args):
                return 0

        gui._input = SimpleNamespace(_user32=_U())

        def restore(u, hwnd):
            calls.append(('restore', hwnd))
            iconic[0] = False

        with patch.object(guia, '_restore_keep_maximize', restore), \
                patch('wechatauto.guia.time.sleep', lambda *_: None):
            self.assertTrue(gui.bring_to_front())
        self.assertIn(('restore', 1), calls,
                      '前台已是微信但最小化时没有执行还原')


if __name__ == '__main__':
    unittest.main()