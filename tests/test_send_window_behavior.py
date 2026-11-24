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
        # 窗口可见性由独立的 _main_window_shown 判定（收进托盘时为 False）；
        # 这里关注的是「不最小化别人」，默认视为可见。
        gui._main_window_shown = Mock(return_value=True)
        return gui

    def test_ensure_visible_does_not_activate_by_default(self):
        """默认档只确认窗口存活，不抢前台——发送路径不需要前置。"""
        gui = self._gui(alive=True)
        with patch('wechatauto.guia.time.sleep', lambda *_: None):
            self.assertTrue(gui.ensure_visible())
        gui._minimize_blockers.assert_not_called()
        gui.bring_to_front.assert_not_called()

    def test_ensure_visible_activates_only_when_asked(self):
        """显式 activate=True 才置前（旧路径/真实键鼠路径用）。"""
        gui = self._gui(alive=True)
        with patch('wechatauto.guia.time.sleep', lambda *_: None):
            self.assertTrue(gui.ensure_visible(activate=True))
        gui._minimize_blockers.assert_not_called()
        gui.bring_to_front.assert_called_once_with(keep_topmost=False)

    def test_ensure_visible_failure_does_not_minimize_either(self):
        gui = self._gui(alive=False)
        with patch('wechatauto.guia.time.sleep', lambda *_: None):
            self.assertFalse(gui.ensure_visible())
        gui._minimize_blockers.assert_not_called()
        gui.bring_to_front.assert_not_called()


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


class TestSendFastPathVerifiesChat(unittest.TestCase):
    """send_msg 快速路径必须核实界面当前真的在目标会话。

    2026-11-20 实测：用户手动切走会话后，_current_chat 缓存仍指向旧会话，
    快速路径直接往缓存坐标打字发送——消息落进了当前选中的对话。遮挡下更糟：
    wx_click 的提前台重试会把微信抬起来，错误发送顺利完成。
    """

    def _gui(self):
        from wechatauto.guia import WeChatGUI
        gui = object.__new__(WeChatGUI)
        gui._input = Mock()
        gui.main_hwnd = 12345
        gui._get_uia = Mock(return_value=None)
        gui._window_kind = Mock(return_value=None)
        gui._current_chat = '文件传输助手'
        gui._last_input_box = (100, 200, 300, 40)
        gui._last_input_box_key = gui._input_box_cache_key()
        gui.ensure_visible = Mock(return_value=True)
        gui._remember_input_box = Mock()
        gui.open_chat = Mock(return_value=True)
        gui.input_text = Mock(return_value=True)
        gui.click_send = Mock(return_value=True)
        return gui

    def test_stale_cache_does_not_type_into_selected_chat(self):
        """界面已停在别的会话（_chat_is_open=False）时不得走快速路径。"""
        gui = self._gui()
        gui._chat_is_open = Mock(return_value=False)
        with patch('wechatauto.guia.time.sleep', lambda *_: None):
            gui.send_msg('缓存失效验证', who='文件传输助手')
        fast_calls = [c for c in gui.input_text.call_args_list
                      if c.kwargs.get('fast')]
        self.assertEqual(fast_calls, [],
                         '缓存失效时仍走了快速路径——消息会发进当前选中的会话')
        gui.open_chat.assert_called_once_with('文件传输助手')

    def test_valid_cache_still_uses_fast_path(self):
        """界面确实在目标会话时，快速路径照常复用输入框，不重新 open_chat。"""
        gui = self._gui()
        gui._chat_is_open = Mock(return_value=True)
        with patch('wechatauto.guia.time.sleep', lambda *_: None):
            gui.send_msg('连续发送', who='文件传输助手')
        gui.input_text.assert_called_once()
        self.assertTrue(gui.input_text.call_args.kwargs.get('fast'))
        gui.open_chat.assert_not_called()


class TestEnsureVisibleWhenWindowHidden(unittest.TestCase):
    """微信收进托盘（WS_VISIBLE=False）时 ensure_visible 必须返回 False。

    2026-11-20 深夜：微信被收进托盘，进程和句柄都还在、WGC 还能抓到最后一帧，
    看上去像正常界面，但真实点击和打字全部落空——机器人重试 4 轮后暂停。
    这里锁住「先自动还原一次，还原不了就快速失败」的行为。
    """

    def _gui(self, shown_sequence=(True,)):
        from wechatauto.guia import WeChatGUI
        gui = object.__new__(WeChatGUI)
        gui.main_hwnd = 12345
        gui._last_visible_ok = False
        gui._last_visible_ts = 0
        gui._main_window_shown = Mock(side_effect=shown_sequence)
        gui._update_render_rect = Mock()
        gui.bring_to_front = Mock(return_value=True)
        gui.is_alive = Mock(return_value=True)
        return gui

    def test_hidden_window_is_restored_once(self):
        """托盘还原首选微信全局热键（走微信自己的显示路径，不白屏）。

        2026-11-23 实测：ShowWindow(SW_SHOW) 只标可见标志，Qt 收不到显示
        事件，渲染子窗口不重建——96% 白屏且各种程序化手段救不回。热键
        （Ctrl+Alt+W）走微信自己的显示流程，侧栏内容 100% 渲染。
        ShowWindow 路径只作为热键没生效时的兜底。
        """
        gui = self._gui([False, True])
        with patch('wechatauto.guia.time.sleep', lambda *_: None), \
                patch.object(type(gui), '_show_via_hotkey',
                             wraps=gui._show_via_hotkey) as hotkey:
            self.assertTrue(gui.ensure_visible(activate=True),
                            '还原成功后应报告窗口可用')
        hotkey.assert_called_once_with()

    def test_still_hidden_fails_fast(self):
        gui = self._gui()
        gui._main_window_shown = Mock(return_value=False)
        with patch('wechatauto.guia.time.sleep', lambda *_: None), \
                patch('wechatauto.guia._restore_keep_maximize'):
            self.assertFalse(gui.ensure_visible(activate=True),
                             '窗口仍不可见时必须返回 False，让调用方快速失败')
            self.assertFalse(gui.ensure_visible(),
                             '不激活的分支同样不能把隐藏窗口当可用')

    def test_15s_cache_does_not_hide_a_hidden_window(self):
        """15 秒缓存期内窗口被收进托盘，也要立刻发现并失败。"""
        import time as _t
        gui = self._gui()
        gui._main_window_shown = Mock(return_value=False)
        gui._last_visible_ok = True
        gui._last_visible_ts = _t.time()      # 缓存有效期内
        with patch('wechatauto.guia.time.sleep', lambda *_: None), \
                patch('wechatauto.guia._restore_keep_maximize'):
            self.assertFalse(gui.ensure_visible(),
                             '缓存不能让隐藏窗口蒙混过关')



class TestWxClickBlockedRetry(unittest.TestCase):
    """wx_click 落点被拦后的重激活重试。

    2026-11-23 群发实测：用户正用电脑时，旧的「置顶后 0.35s 盲等复查」
    会被用户切回窗口打穿，20+ 次拦截空转。新逻辑最多两轮重激活，每轮
    确认前台真的回到微信再复查落点；全被挡才放弃并收回置顶。
    """

    def _gui(self):
        from wechatauto.guia import WeChatGUI
        gui = object.__new__(WeChatGUI)
        gui._input = Mock()
        gui.pid = 25976
        gui.render_hwnd = 777
        gui._input._user32.GetWindowLongW.return_value = 0
        gui.bring_to_front = Mock(return_value=True)
        gui.restore_zorder = Mock()
        gui._foreground_is_wechat = Mock(return_value=True)
        gui._input.real_click = Mock()
        return gui

    def test_retry_after_reactivate_clicks_once(self):
        """第一次被拦、重激活后复查通过：点一次，且因抬过置顶要收回。"""
        gui = self._gui()
        gui.point_belongs_to_wechat = Mock(side_effect=[False, True])
        with patch('wechatauto.guia.time.sleep'):
            gui.wx_click(500, 300)
        gui._input.real_click.assert_called_once()
        gui.restore_zorder.assert_called_once()

    def test_all_blocked_gives_up_without_click(self):
        """两轮重激活后仍被拦：不点击、收回置顶。"""
        gui = self._gui()
        gui.point_belongs_to_wechat = Mock(return_value=False)
        with patch('wechatauto.guia.time.sleep'):
            gui.wx_click(500, 300)
        gui._input.real_click.assert_not_called()
        gui.restore_zorder.assert_called_once()

    def test_unblocked_clicks_without_bringing_front(self):
        """落点本来就属于微信：直接点，不抢前台。"""
        gui = self._gui()
        gui.point_belongs_to_wechat = Mock(return_value=True)
        with patch('wechatauto.guia.time.sleep'):
            gui.wx_click(500, 300)
        gui._input.real_click.assert_called_once()
        gui.bring_to_front.assert_not_called()
        gui.restore_zorder.assert_not_called()


    def test_second_round_rescues_after_user_switches_back(self):
        """第一轮重激活后用户又切回窗口（复查仍拦）：第二轮救回。

        旧逻辑只重激活一次、0.35s 后直接复查，复查失败就放弃——这个
        场景在旧代码上不会点击（证伪点）。
        """
        gui = self._gui()
        # 拦 → 第一轮复查仍拦（用户切回）→ 第二轮复查通过
        gui.point_belongs_to_wechat = Mock(side_effect=[False, False, True])
        with patch('wechatauto.guia.time.sleep'):
            gui.wx_click(500, 300)
        gui._input.real_click.assert_called_once()
        self.assertEqual(gui.bring_to_front.call_count, 2)


if __name__ == '__main__':
    unittest.main()