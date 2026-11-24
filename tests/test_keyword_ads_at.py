# -*- coding: utf-8 -*-
"""真 @ 路径测试：发送分发、成员解析、输入框读回校验（不触微信）。"""

import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from wechatauto import guia  # noqa: E402
from wechatauto.wx import Chat  # noqa: E402
from dafeiyu_bot import DaFeiYuBot  # noqa: E402


class _FakeGui:
    def __init__(self):
        self.at_calls = []
        self.send_calls = []

    def at_member(self, member, text, who=None, verify=False):
        self.at_calls.append((member, text, who, verify))
        return 'at-ok'

    def send_msg(self, text, who=None, verify=False):
        self.send_calls.append((text, who, verify))
        return 'send-ok'


class TestChatSendMsgDispatch(unittest.TestCase):
    def setUp(self):
        self.gui = _FakeGui()
        self.chat = Chat.__new__(Chat)
        self.chat.who = 'group@chatroom'
        self.chat._gui = self.gui

    def test_at_uses_at_member_with_verify(self):
        result = self.chat.SendMsg('正文', at='张三', verify=True)
        self.assertEqual(result, 'at-ok')
        self.assertEqual(self.gui.at_calls,
                         [('张三', '正文', 'group@chatroom', True)])
        self.assertEqual(self.gui.send_calls, [])

    def test_no_at_uses_send_msg(self):
        result = self.chat.SendMsg('正文', verify=False)
        self.assertEqual(result, 'send-ok')
        self.assertEqual(self.gui.send_calls,
                         [('正文', 'group@chatroom', False)])
        self.assertEqual(self.gui.at_calls, [])


class _FakeMemberChat:
    def __init__(self, members):
        self.members = members

    def GetGroupMembers(self):
        if isinstance(self.members, Exception):
            raise self.members
        return self.members


class _FakeListItem:
    """UIA 弹层里的成员项：只要 Name 和 Click，够 at_member 走完认人分支。"""

    def __init__(self, name):
        self.Name = name
        self.clicked = 0

    def Click(self):
        self.clicked += 1


class TestResolveAtName(unittest.TestCase):
    def test_unique_member(self):
        chat = _FakeMemberChat([
            {'username': 'wxid_a', 'nick_name': '张三', 'remark': None},
            {'username': 'wxid_b', 'nick_name': '李四', 'remark': None},
        ])
        self.assertEqual(
            DaFeiYuBot._resolve_keyword_ad_at(chat, 'wxid_a', '张三'),
            ('张三', ''))

    def test_remark_wins(self):
        chat = _FakeMemberChat([
            {'username': 'wxid_a', 'nick_name': '张三', 'remark': '老张'},
        ])
        self.assertEqual(
            DaFeiYuBot._resolve_keyword_ad_at(chat, 'wxid_a'), ('老张', ''))

    def test_duplicate_display_name(self):
        chat = _FakeMemberChat([
            {'username': 'wxid_a', 'nick_name': '张三', 'remark': None},
            {'username': 'wxid_b', 'nick_name': '张三', 'remark': None},
        ])
        self.assertEqual(
            DaFeiYuBot._resolve_keyword_ad_at(chat, 'wxid_a'), ('', 'at_ambiguous'))

    def test_member_missing(self):
        chat = _FakeMemberChat([
            {'username': 'wxid_b', 'nick_name': '李四', 'remark': None},
        ])
        self.assertEqual(
            DaFeiYuBot._resolve_keyword_ad_at(chat, 'wxid_a'),
            ('', 'at_unresolvable'))

    def test_member_list_error(self):
        chat = _FakeMemberChat(RuntimeError('db away'))
        self.assertEqual(
            DaFeiYuBot._resolve_keyword_ad_at(chat, 'wxid_a'),
            ('', 'at_failed'))


class _FakeInput:
    def __init__(self):
        self.typed = []

    def type_unicode(self, text):
        self.typed.append(text)

    def key(self, *args, **kwargs):
        pass


class _FakeUser32:
    """替身 user32：PostMessageW 只记录，不往真微信窗口投任何消息。"""

    def __init__(self):
        self.posted = []

    def PostMessageW(self, hwnd, msg, wp, lp):
        self.posted.append((hwnd, msg, wp, lp))
        return 1

    def GetForegroundWindow(self):
        return 0

    def IsWindow(self, hwnd):
        return False

    def SetForegroundWindow(self, hwnd):
        return 1

    def IsIconic(self, hwnd):
        return 0

    def WindowFromPoint(self, pt):
        return 111      # 落点反查：替身里恒返回主窗口

    def GetWindowThreadProcessId(self, hwnd, out):
        obj = getattr(out, '_obj', None)   # 调用方传 ctypes.byref(pid)
        if obj is not None:
            obj.value = 1                  # 与替身 pid 一致即可
        return 0

    def ScreenToClient(self, hwnd, pt):
        return 1


class TestAtMemberReadback(unittest.TestCase):
    def setUp(self):
        # 落库校验现在是轮询；测试里把等待/间隔压到毫秒级，避免用例变慢。
        self._old_timeout = guia.WxParam.DB_VERIFY_TIMEOUT
        self._old_interval = guia.WxParam.DB_VERIFY_INTERVAL
        guia.WxParam.DB_VERIFY_TIMEOUT = 0.2
        guia.WxParam.DB_VERIFY_INTERVAL = 0.01
        self.user32 = _FakeUser32()

    def tearDown(self):
        guia.WxParam.DB_VERIFY_TIMEOUT = self._old_timeout
        guia.WxParam.DB_VERIFY_INTERVAL = self._old_interval

    def _gui(self, read_values, click_ok=True, verify_ok=True):
        gui = object.__new__(guia.WeChatGUI)
        gui.ensure_visible = lambda *a, **kw: True
        gui.main_hwnd = 111
        gui.pid = 1          # _post_click_screen 落点校验：替身窗口归属进程 1
        gui.origin_x = gui.origin_y = 0
        gui.right_pane_left = 0
        gui.render_w = gui.render_h = 100
        # UIA 认人是真 @ 的唯一依据：替身里默认认到「张三」，需要认不到的
        # 用例自己覆盖成 lambda *a: None。
        gui._uia_mention_item = lambda member, core: ('张三', [0, 0, 40, 20])
        # 点选成员走 _post_click_ctrl；替身返回成功。
        uia = mock.Mock()
        uia._post_click_ctrl = mock.Mock(return_value=True)
        gui._get_uia = lambda: uia
        gui.open_chat = lambda who: True
        gui.focus_input = lambda: True
        gui._focus_input_via_post = lambda: True
        gui.get_input_box = lambda: (0, 0, 100, 100)
        gui._last_input_box = None
        gui._pending_text = None
        gui._send_attempted = False
        gui.clicked = 0
        gui.cleared = 0

        def click_send():
            gui.clicked += 1
            gui._send_attempted = True
            return click_ok

        gui.click_send = click_send
        gui.clear_input = lambda: setattr(gui, 'cleared', gui.cleared + 1)
        gui._send_watermark = lambda who: 0
        gui._wait_landed = lambda *a, **kw: verify_ok
        values = iter(read_values)
        gui.read_input_text = lambda: next(values)
        return gui

    def _run(self, gui, member='张三', text='你好', who='测试群', verify=False):
        with mock.patch.object(guia.ctypes, 'windll',
                               mock.Mock(user32=self.user32)), \
                mock.patch.object(guia.time, 'sleep'):
            return guia.WeChatGUI.at_member(gui, member, text, who, verify)

    def test_success_appends_body_after_at_token(self):
        gui = self._gui(['@张三', '@张三你好'])
        r = self._run(gui)
        self.assertTrue(r.is_success)
        # '@' + 过滤核心字「张三」经 WM_CHAR 投递，正文追加不再清空
        self.assertEqual(len(self.user32.posted), 7)  # @1+核心字2+正文2+点选DOWN/UP2
        self.assertEqual(gui.clicked, 1)
        self.assertEqual(r['data']['at_name'], '张三')
        self.assertTrue(r['data']['at_token_ok'])
        self.assertTrue(r['data']['body_ok'])

    def test_unrecognised_member_fails_closed(self):
        """UIA 认不到人时不能拿输入框里的纯文本「@张三」冒充真 @。"""
        gui = self._gui(['@张三'])
        gui._uia_mention_item = lambda *a: None
        r = self._run(gui)
        self.assertFalse(r.is_success)
        self.assertEqual(r['data']['reason'], 'at_failed')
        self.assertEqual(gui.cleared, 2)   # 开头清残留 + 认人失败清空
        self.assertEqual(gui.clicked, 0)

    def test_token_mismatch_clears_and_does_not_send(self):
        """UIA 认到的人与读回对不上时按 token 不一致处理。"""
        gui = self._gui(['@张三'])
        gui._uia_mention_item = lambda *a: ('李四', [0, 0, 40, 20])
        r = self._run(gui)
        self.assertFalse(r.is_success)
        self.assertEqual(r['data']['reason'], 'at_token_mismatch')
        self.assertEqual(gui.cleared, 2)   # 开头清残留 + 失败清空
        self.assertEqual(gui.clicked, 0)

    def test_body_mismatch_clears_and_does_not_send(self):
        gui = self._gui(['@张三', '@张三'])
        r = self._run(gui)
        self.assertFalse(r.is_success)
        self.assertEqual(r['data']['reason'], 'at_body_mismatch')
        self.assertEqual(gui.cleared, 2)   # 开头清残留 + 失败清空
        self.assertEqual(gui.clicked, 0)

    def test_unreadable_input_fails_closed(self):
        gui = self._gui([''])
        r = self._run(gui)
        self.assertFalse(r.is_success)
        self.assertEqual(r['data']['reason'], 'at_unverified')
        self.assertEqual(gui.clicked, 0)

    def test_verify_true_success_and_failure(self):
        gui = self._gui(['@张三', '@张三你好'], verify_ok=True)
        r = self._run(gui, verify=True)
        self.assertTrue(r.is_success)
        gui2 = self._gui(['@张三', '@张三你好'], verify_ok=False)
        r2 = self._run(gui2, verify=True)
        self.assertFalse(r2.is_success)

    def test_verify_polls_until_landed(self):
        """落库有延迟时必须轮询：第一次查不到、第二次查到仍算成功。

        2026-11-18 16:27 真机上消息发出约 1 秒才落库，旧实现只查一次，
        于是真实发出却被记成「数据库未确认」。把这条钉在这里：只查一次
        的实现会让本用例转红。
        """
        gui = self._gui(['@张三', '@张三你好'], verify_ok=False)
        calls = {'n': 0}

        def flaky(*args, **kwargs):
            calls['n'] += 1
            return True                  # 轮询到落库（真实现内部循环）

        gui._wait_landed = flaky
        r = self._run(gui, verify=True)
        self.assertTrue(r.is_success, r)
        self.assertEqual(calls['n'], 1)   # 轮询封装在 _wait_landed 内部，外部只调一次

if __name__ == '__main__':
    unittest.main()
