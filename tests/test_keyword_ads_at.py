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


class TestAtMemberReadback(unittest.TestCase):
    def setUp(self):
        # 落库校验现在是轮询；测试里把等待/间隔压到毫秒级，避免用例变慢。
        self._old_timeout = guia.WxParam.DB_VERIFY_TIMEOUT
        self._old_interval = guia.WxParam.DB_VERIFY_INTERVAL
        guia.WxParam.DB_VERIFY_TIMEOUT = 0.2
        guia.WxParam.DB_VERIFY_INTERVAL = 0.01

    def tearDown(self):
        guia.WxParam.DB_VERIFY_TIMEOUT = self._old_timeout
        guia.WxParam.DB_VERIFY_INTERVAL = self._old_interval

    def _gui(self, read_values, click_ok=True, verify_ok=True):
        gui = object.__new__(guia.WeChatGUI)
        gui.ensure_visible = lambda: True
        gui.open_chat = lambda who: True
        gui.focus_input = lambda: True
        gui._input = _FakeInput()
        gui.get_input_box = lambda: (0, 0, 100, 100)
        gui.ocr = lambda box: [('张三', 10, 10, 20, 20)]
        gui.wx_click = lambda x, y: None
        gui.origin_x = gui.origin_y = 0
        gui.right_pane_left = 0
        gui.render_w = gui.render_h = 100
        gui._last_input_box = None
        gui._pending_text = None
        gui._send_attempted = False
        gui.clicked = 0
        gui.cleared = 0
        gui.verified = []

        def click_send():
            gui.clicked += 1
            gui._send_attempted = True
            return click_ok

        gui.click_send = click_send
        gui._clear_input = lambda: setattr(gui, 'cleared', gui.cleared + 1)
        gui._send_watermark = lambda who: 0
        gui._verify_sent = lambda *a, **kw: verify_ok
        values = iter(read_values)
        gui.read_input_text = lambda: next(values)
        return gui

    def test_success_appends_body_after_at_token(self):
        gui = self._gui(['@张三', '@张三你好'])
        typed = []
        with mock.patch.object(guia.rhythm, 'type_text',
                               side_effect=lambda text: typed.append(text)):
            r = guia.WeChatGUI.at_member(gui, '张三', '你好', '测试群')
        self.assertTrue(r.is_success)
        self.assertEqual(typed, ['你好'])          # 没有调用 input_text 清空重打
        self.assertEqual(gui._input.typed, ['@', '张三'])
        self.assertEqual(gui.clicked, 1)
        self.assertEqual(r['data']['at_name'], '张三')
        self.assertTrue(r['data']['at_token_ok'])
        self.assertTrue(r['data']['body_ok'])

    def test_token_mismatch_clears_and_does_not_send(self):
        gui = self._gui(['@李四'])
        typed = []
        with mock.patch.object(guia.rhythm, 'type_text',
                               side_effect=lambda text: typed.append(text)):
            r = guia.WeChatGUI.at_member(gui, '张三', '你好', '测试群')
        self.assertFalse(r.is_success)
        self.assertEqual(r['data']['reason'], 'at_token_mismatch')
        self.assertEqual(gui.cleared, 1)
        self.assertEqual(gui.clicked, 0)
        self.assertEqual(typed, [])

    def test_body_mismatch_clears_and_does_not_send(self):
        gui = self._gui(['@张三', '@张三'])
        with mock.patch.object(guia.rhythm, 'type_text', return_value=0):
            r = guia.WeChatGUI.at_member(gui, '张三', '你好', '测试群')
        self.assertFalse(r.is_success)
        self.assertEqual(r['data']['reason'], 'at_body_mismatch')
        self.assertEqual(gui.cleared, 1)
        self.assertEqual(gui.clicked, 0)

    def test_unreadable_input_fails_closed(self):
        gui = self._gui([''])
        r = guia.WeChatGUI.at_member(gui, '张三', '你好', '测试群')
        self.assertFalse(r.is_success)
        self.assertEqual(r['data']['reason'], 'at_unverified')
        self.assertEqual(gui.clicked, 0)

    def test_verify_true_success_and_failure(self):
        gui = self._gui(['@张三', '@张三你好'], verify_ok=True)
        with mock.patch.object(guia.rhythm, 'type_text', return_value=0):
            r = guia.WeChatGUI.at_member(gui, '张三', '你好', '测试群',
                                         verify=True)
        self.assertTrue(r.is_success)
        gui2 = self._gui(['@张三', '@张三你好'], verify_ok=False)
        with mock.patch.object(guia.rhythm, 'type_text', return_value=0):
            r2 = guia.WeChatGUI.at_member(gui2, '张三', '你好', '测试群',
                                          verify=True)
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
            return calls['n'] >= 2      # 第一次还没落库，第二次落库

        gui._verify_sent = flaky
        with mock.patch.object(guia.rhythm, 'type_text', return_value=0):
            r = guia.WeChatGUI.at_member(gui, '张三', '你好', '测试群',
                                         verify=True)
        self.assertTrue(r.is_success, r)
        self.assertGreaterEqual(calls['n'], 2)

    def test_uia_token_is_object_replacement_and_name_comes_from_item(self):
        """UIA 输入框把 @token 读成 \\ufffc；成员名以选中的 ListItem 为准。"""
        gui = self._gui(['\ufffc', '\ufffc你好'])

        class Item:
            Name = '张三'

            def __init__(self):
                self.clicked = 0

            def Click(self):
                self.clicked += 1

        item = Item()
        gui._uia_mention_item = lambda member, core: item
        gui.ocr = lambda box: []
        typed = []
        with mock.patch.object(guia.rhythm, 'type_text',
                               side_effect=lambda text: typed.append(text)):
            r = guia.WeChatGUI.at_member(gui, '张三', '你好', '测试群')
        self.assertTrue(r.is_success)
        self.assertEqual(item.clicked, 1)
        self.assertEqual(typed, ['你好'])
        self.assertTrue(r['data']['at_token_ok'])
        self.assertTrue(r['data']['body_ok'])
        self.assertEqual(gui.clicked, 1)

    def test_mention_item_prefers_exact_and_rejects_ambiguous(self):
        class Item:
            def __init__(self, name):
                self.Name = name

        exact = Item('张三')
        self.assertIs(
            guia.WeChatGUI._pick_mention_item(
                [Item('张三丰'), exact], '张三', '张三'), exact)
        self.assertIsNone(guia.WeChatGUI._pick_mention_item(
            [Item('张三丰'), Item('张三郎')], '张三', '张三'))
        self.assertIsNone(guia.WeChatGUI._pick_mention_item(
            [Item('张三'), Item('张三')], '张三', '张三'))


if __name__ == '__main__':
    unittest.main()
