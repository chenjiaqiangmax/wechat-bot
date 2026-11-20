"""Jev 式单帧定位的纯逻辑回归测试，不连接或操作真实微信。"""

import unittest
from typing import Any, cast
from unittest.mock import patch

from PIL import Image, ImageDraw

from wechatauto.guia import (
    INPUT_BOX_RATIO,
    WeChatGUI,
    _best_session_row,
    _cached_input_box_matches_frame,
    _chat_name_score,
    _detect_input_box_from_frame,
    _search_candidate_row,
)


def _frame(width=900, height=650, divider_y=520, has_divider=True):
    """合成常见浅色会话帧：白色输入区下方是灰色工具栏。"""
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)
    if has_divider:
        draw.rectangle((198, divider_y - 2, width - 1, divider_y - 1),
                       fill=(224, 224, 224))
    draw.rectangle((198, 596, width - 1, height - 1), fill=(235, 235, 235))
    return image


def _bare_gui(frames):
    """构造不触碰 Win32 的 WeChatGUI 定位替身。"""
    gui = cast(Any, object.__new__(WeChatGUI))
    gui.render_hwnd = 100
    gui.render_w = frames[0].width
    gui.render_h = frames[0].height
    gui.right_pane_left = 198
    gui.layout_profile = 'wide'
    gui._last_input_box = None
    gui._last_input_box_key = None
    gui._update_render_rect = lambda: None
    gui._get_uia = lambda: None
    gui._window_kind = lambda _uia: None
    gui._rel_to_screen = lambda rect: rect
    gui._grab_screen = lambda _rect: frames.pop(0)
    return gui


class TestSingleFrameInputLocator(unittest.TestCase):
    def test_finds_separator_and_bounds_from_one_frame(self):
        image = _frame()
        box = _detect_input_box_from_frame(image, 198)
        self.assertEqual(box, (198, 520, 900, 596))
        if box is None:
            self.fail('合成输入框没有被定位')
        self.assertTrue(_cached_input_box_matches_frame(image, box, 198))

    def test_rejects_white_chat_area_without_separator(self):
        self.assertIsNone(_detect_input_box_from_frame(
            _frame(has_divider=False), 198))

    def test_cache_reuses_only_when_current_boundary_matches(self):
        image = _frame()
        cached = (198, 520, 900, 596)
        self.assertTrue(_cached_input_box_matches_frame(image, cached, 198))
        self.assertFalse(_cached_input_box_matches_frame(
            _frame(divider_y=480), cached, 198))

    def test_success_uses_one_capture_and_dimension_change_invalidates_cache(self):
        frames = [_frame(), _frame(width=901)]
        gui = _bare_gui(frames)
        calls = []
        grab = gui._grab_screen
        gui._grab_screen = lambda rect: (calls.append(rect), grab(rect))[1]

        first = gui.get_input_box()
        self.assertEqual(first, (198, 520, 900, 596))
        self.assertEqual(len(calls), 1)

        gui.render_w = 901
        second = gui.get_input_box()
        self.assertEqual(second, (198, 520, 901, 596))
        self.assertEqual(len(calls), 2)
        self.assertEqual(gui._last_input_box_key, gui._input_box_cache_key())

    def test_probe_grabs_one_frame_instead_of_each_scan_line(self):
        gui = _bare_gui([_frame()])
        calls = []
        grab = gui._grab_screen
        gui._grab_screen = lambda rect: (calls.append(rect), grab(rect))[1]
        self.assertEqual(gui._probe_input_box(), (198, 520, 900, 596))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (0, 0, 900, 650))

    def test_missing_separator_uses_ratio_fallback_after_two_frames(self):
        gui = _bare_gui([_frame(has_divider=False), _frame(has_divider=False)])
        with patch('wechatauto.guia.time.sleep'):
            box = gui.get_input_box()
        expected = (198, int(650 * INPUT_BOX_RATIO[1]),
                    int(900 * INPUT_BOX_RATIO[2]), int(650 * INPUT_BOX_RATIO[3]))
        self.assertEqual(box, expected)

    def test_search_candidate_accepts_group_result_but_not_member_preview(self):
        rows = [
            ('群聊', 12, 40, 30, 16),
            ('目标群', 30, 80, 90, 20),
            ('成员，包含：目标群', 30, 110, 150, 18),
        ]
        self.assertEqual(_search_candidate_row(rows, '目标群'), rows[1])

    def test_similar_or_ambiguous_search_names_are_rejected(self):
        similar = [('目标群2', 30, 80, 90, 20)]
        duplicate = [('目标群', 30, 80, 90, 20), ('目标群', 30, 110, 90, 20)]
        self.assertIsNone(_search_candidate_row(similar, '目标群'))
        self.assertIsNone(_search_candidate_row(duplicate, '目标群'))
        self.assertEqual(_chat_name_score('件传输助', '文件传输助手'), 2)
        self.assertEqual(_chat_name_score('文件传输助手', '文件传输助手'), 3)
        self.assertEqual(_chat_name_score('文件传输助手（450）', '文件传输助手'), 3)
        self.assertIsNone(_best_session_row([
            {'name': '目标群'}, {'name': '目标群'},
        ], '目标群'))


def _popup_gui(clicks):
    """构造只走浮层搜索的 WeChatGUI 替身（不碰 Win32）。"""
    gui = cast(Any, object.__new__(WeChatGUI))
    gui.main_hwnd = 1234
    gui.search_box = (68, 37, 237, 70)
    gui.render_w, gui.render_h = 900, 650
    gui.origin_x = gui.origin_y = 0
    gui._past_deadline = lambda: False
    gui._rel_to_screen = lambda rect: rect
    gui._input = type('Input', (), {
        'key': lambda *_args, **_kwargs: None,
        'type_unicode': lambda *_args, **_kwargs: None,
    })()
    gui.ensure_visible = lambda **_kwargs: None
    gui.set_clipboard = lambda text: True
    gui._typed_into_chat_input = lambda expect: False
    gui.wx_click = lambda x, y, right=False: clicks.append((x, y))
    return gui


def _popup_x2(rows):
    """OCR 在 2 倍图上运行，这里把期望坐标放大两倍喂回去。"""
    return [(text, x * 2, y * 2, w * 2, h * 2) for text, x, y, w, h in rows]


class TestSearchPopup(unittest.TestCase):
    """搜索结果在独立浮层窗口里：必须抓浮层，不能抓主窗。"""

    def test_clicks_the_unique_popup_result(self):
        clicks = []
        gui = _popup_gui(clicks)
        with patch('wechatauto.guia._find_search_popup',
                   return_value=(999, (875, 317, 1243, 905))), \
                patch('wechatauto.guia._capture_window_frame',
                      return_value=Image.new('RGB', (368, 588))), \
                patch('wechatauto.guia.ScreenOCR.recognize',
                      return_value=_popup_x2([
                          ('功能', 40, 30, 60, 20),
                          ('文件传输助手', 100, 160, 200, 40),
                          ('搜索网络结果', 40, 340, 120, 20),
                          ('文件传输助手', 100, 400, 200, 40),
                      ])), \
                patch('wechatauto.guia.time.sleep'), \
                patch('wechatauto.guia.rhythm.point', side_effect=lambda r: (
                    (r[0] + r[2]) // 2, (r[1] + r[3]) // 2)):
            self.assertTrue(gui._search_chat('文件传输助手'))
        # 第一下点搜索框，第二下点浮层里的「功能」项（不是下方网络结果）
        self.assertEqual(clicks, [(152, 53), (1075, 497)])

    def test_rejects_candidate_only_in_network_results(self):
        clicks = []
        gui = _popup_gui(clicks)
        with patch('wechatauto.guia._find_search_popup',
                   return_value=(999, (875, 317, 1243, 905))), \
                patch('wechatauto.guia._capture_window_frame',
                      return_value=Image.new('RGB', (368, 588))), \
                patch('wechatauto.guia.ScreenOCR.recognize',
                      return_value=_popup_x2([
                          ('搜索网络结果', 40, 340, 120, 20),
                          ('文件传输助手', 100, 400, 200, 40),
                      ])), \
                patch('wechatauto.guia.time.sleep'), \
                patch('wechatauto.guia.rhythm.point', side_effect=lambda r: (
                    (r[0] + r[2]) // 2, (r[1] + r[3]) // 2)):
            self.assertFalse(gui._search_chat('文件传输助手'))
        self.assertEqual(clicks, [(152, 53)])   # 只点了搜索框，没点网络结果

    def test_missing_popup_fails_closed(self):
        clicks = []
        gui = _popup_gui(clicks)
        with patch('wechatauto.guia._find_search_popup', return_value=None), \
                patch('wechatauto.guia.time.sleep'), \
                patch('wechatauto.guia.rhythm.point', side_effect=lambda r: (
                    (r[0] + r[2]) // 2, (r[1] + r[3]) // 2)):
            self.assertFalse(gui._search_chat('文件传输助手'))
        self.assertEqual(clicks, [(152, 53)])

    def test_nonempty_pane_does_not_confirm_wrong_chat(self):
        gui = cast(Any, object.__new__(WeChatGUI))
        checks = []
        gui._past_deadline = lambda: False
        gui._chat_is_open = lambda _name: (checks.append(_name) or False)
        gui._pane_has_content = lambda: True
        with patch('wechatauto.guia.time.sleep'):
            self.assertFalse(gui._chat_open_confirmed('目标群'))
        self.assertEqual(len(checks), 6)


if __name__ == '__main__':
    unittest.main()
