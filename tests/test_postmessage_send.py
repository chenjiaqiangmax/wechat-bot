"""SEND_VIA_POSTMESSAGE 开关的接线测试。

要证明两件事：开关打开时 send_text 优先走「写值 + 点发送按钮」；
写值路径失败时必须回退到原来的逐字键入，而不是直接判发送失败。
"""
import unittest

from wechatauto.param import WxParam
from wechatauto.uia_driver import WeChatUIA


class TestPostMessageSend(unittest.TestCase):

    def setUp(self):
        self._old = WxParam.SEND_VIA_POSTMESSAGE

    def tearDown(self):
        WxParam.SEND_VIA_POSTMESSAGE = self._old

    def test_send_text_prefers_button_path(self):
        u = object.__new__(WeChatUIA)
        calls = []
        u.send_via_button = lambda text: (calls.append(text), True)[1]
        WxParam.SEND_VIA_POSTMESSAGE = True
        self.assertTrue(u.send_text('你好'))
        self.assertEqual(calls, ['你好'])

    def test_send_text_falls_back_when_button_path_fails(self):
        u = object.__new__(WeChatUIA)
        u.send_via_button = lambda text: False
        u._send_attempted = False
        called = []
        u.ensure_window = lambda: (called.append(1), False)[1]
        WxParam.SEND_VIA_POSTMESSAGE = True
        self.assertFalse(u.send_text('你好'))
        # 回退路径必须真的执行一次（ensure_window 是它的第一步）
        self.assertEqual(called, [1])
        self.assertFalse(u._send_attempted)

    def test_switch_off_keeps_old_path(self):
        u = object.__new__(WeChatUIA)
        u.send_via_button = lambda text: True
        u._send_attempted = False
        u.ensure_window = lambda: False
        WxParam.SEND_VIA_POSTMESSAGE = False
        self.assertFalse(u.send_text('你好'))


if __name__ == '__main__':
    unittest.main()