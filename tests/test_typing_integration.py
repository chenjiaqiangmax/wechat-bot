# -*- coding: utf-8 -*-
"""文本输入路径测试：确认 guia/uia_driver 不再走剪贴板整段粘贴。"""

import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from wechatauto import guia  # noqa: E402
from wechatauto import uia_driver  # noqa: E402


class _FakeInput:
    def __init__(self):
        self.keys = []

    def key(self, *args, **kwargs):
        self.keys.append((args, kwargs))


class TestGuiaInputText(unittest.TestCase):
    def _gui(self, has_text=True, pending_ok=True):
        gui = object.__new__(guia.WeChatGUI)
        gui.get_input_box = lambda: (0, 0, 100, 100)
        gui.focus_input = lambda box=None: True
        gui.foreground_is_wechat = lambda: True
        gui._input = _FakeInput()
        gui._input_box_has_text = lambda box=None: has_text
        gui._pending_text_ok = lambda text: pending_ok
        gui._last_input_box = None
        gui.set_clipboard = mock.Mock(
            side_effect=AssertionError('正文输入不应再碰剪贴板'))
        return gui

    def test_full_path_types_without_clipboard(self):
        gui = self._gui()
        with mock.patch.object(guia.rhythm, 'type_text') as typed, \
                mock.patch.object(guia.time, 'sleep', return_value=None):
            ok = gui.input_text('你好')
        self.assertTrue(ok)
        typed.assert_called_once_with('你好')
        gui.set_clipboard.assert_not_called()
        self.assertEqual(gui._pending_text, '你好')
        self.assertEqual(gui._last_input_box, (0, 0, 100, 100))

    def test_fast_path_types_without_clipboard(self):
        gui = self._gui()
        with mock.patch.object(guia.rhythm, 'type_text') as typed, \
                mock.patch.object(guia.time, 'sleep', return_value=None):
            ok = gui.input_text('x', box=(0, 0, 100, 100), fast=True)
        self.assertTrue(ok)
        typed.assert_called_once_with('x')
        gui.set_clipboard.assert_not_called()

    def test_readback_mismatch_retries_once_then_fails(self):
        gui = self._gui(has_text=False)
        with mock.patch.object(guia.rhythm, 'type_text') as typed, \
                mock.patch.object(guia.time, 'sleep', return_value=None):
            ok = gui.input_text('你好')
        self.assertFalse(ok)
        self.assertEqual(typed.call_count, 2)
        gui.set_clipboard.assert_not_called()


class _FakeElement:
    def __init__(self):
        self.keys = []

    def SendKeys(self, keys, waitTime=0):
        self.keys.append(keys)


class TestUiaSendText(unittest.TestCase):
    def _uia(self):
        u = object.__new__(uia_driver.WeChatUIA)
        e = _FakeElement()
        u.ensure_window = lambda: True
        u._chat_input = lambda: e
        u._click_ctrl = lambda ctrl: True
        u._paste_into = mock.Mock(
            side_effect=AssertionError('发送正文不应再调用 _paste_into'))
        u._paste_landed = lambda ctrl, text: (True, text, 1.0)
        u._send_attempted = False
        u._chat_input_cache = None
        return u, e

    def test_send_text_uses_type_text_not_paste(self):
        u, e = self._uia()
        with mock.patch.object(uia_driver.rhythm, 'type_text') as typed, \
                mock.patch.object(uia_driver.rhythm, 'gate'), \
                mock.patch.object(uia_driver.rhythm, 'nap'):
            ok = u.send_text('你好')
        self.assertTrue(ok)
        typed.assert_called_once_with('你好')
        u._paste_into.assert_not_called()
        self.assertEqual(e.keys, ['{Ctrl}a{Delete}', '{Enter}'])

    def test_send_text_mismatch_does_not_enter(self):
        u, e = self._uia()
        u._paste_landed = lambda ctrl, text: (False, '', 0.1)
        with mock.patch.object(uia_driver.rhythm, 'type_text'), \
                mock.patch.object(uia_driver.rhythm, 'gate'), \
                mock.patch.object(uia_driver.rhythm, 'nap'):
            ok = u.send_text('你好')
        self.assertFalse(ok)
        u._paste_into.assert_not_called()
        self.assertNotIn('{Enter}', e.keys)


if __name__ == '__main__':
    unittest.main()
