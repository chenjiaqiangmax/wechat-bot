"""Jev 式单帧定位的纯逻辑回归测试，不连接或操作真实微信。"""

import unittest
from typing import Any, cast
from unittest.mock import patch

from PIL import Image, ImageDraw

from wechatauto.guia import (
    INPUT_BOX_RATIO,
    WeChatGUI,
    _blank_frame,
    _cached_input_box_matches_frame,
    _detect_input_box_from_frame,
    _openim_title_match,
    _strip_emoji,
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
    gui.ensure_visible = lambda **_kwargs: True   # 窗口可用
    gui.set_clipboard = lambda text: True
    gui._typed_into_chat_input = lambda expect: False
    gui.wx_click = lambda x, y, right=False: clicks.append((x, y))
    return gui


def _popup_x2(rows):
    """OCR 在 2 倍图上运行，这里把期望坐标放大两倍喂回去。"""
    return [(text, x * 2, y * 2, w * 2, h * 2) for text, x, y, w, h in rows]


class TestBlankFrameAndWaitRender(unittest.TestCase):
    """白屏检测与渲染等待：还原/激活后 Qt 渲染延迟会给出白帧。"""

    def test_blank_frame_judgement(self):
        white = Image.new('RGB', (900, 650), 'white')
        self.assertTrue(_blank_frame(white))
        self.assertTrue(_blank_frame(None))          # 抓不到帧按白屏处理
        content = white.copy()
        ImageDraw.Draw(content).rectangle((0, 0, 400, 300), fill=(180, 60, 60))
        self.assertFalse(_blank_frame(content))

    def test_wait_render_returns_when_frame_recovers(self):
        gui = cast(Any, object.__new__(WeChatGUI))
        gui.render_w, gui.render_h = 900, 650
        gui._rel_to_screen = lambda rect: rect
        white = Image.new('RGB', (900, 650), 'white')
        ok = Image.new('RGB', (900, 650), 'white')
        ImageDraw.Draw(ok).rectangle((0, 0, 300, 200), fill=(90, 90, 90))
        frames = [white, white, ok]
        grabs = []
        gui._grab_screen = lambda rect: (grabs.append(rect), frames.pop(0))[1]
        with patch('wechatauto.guia.time.sleep'):
            self.assertTrue(gui._wait_render(timeout=5.0))
        self.assertEqual(len(grabs), 3)

    def test_wait_render_gives_up_after_timeout(self):
        gui = cast(Any, object.__new__(WeChatGUI))
        gui.render_w, gui.render_h = 900, 650
        gui._rel_to_screen = lambda rect: rect
        gui._grab_screen = lambda rect: Image.new('RGB', (900, 650), 'white')
        with patch('wechatauto.guia.time.sleep'):
            self.assertFalse(gui._wait_render(timeout=0.05))


class TestSearchPopup(unittest.TestCase):
    """搜索结果在独立浮层窗口里：必须抓浮层，不能抓主窗。"""

    def test_missing_popup_fails_closed(self):
        """搜索框定位不到时 fail-closed，不点击任何东西（uiacli 新链路）。"""
        gui = cast(Any, object.__new__(WeChatGUI))
        gui.main_hwnd = 111
        gui._past_deadline = lambda: False
        gui.ensure_visible = lambda **kw: True
        with patch('wechatauto.guia.uiacli.find', return_value={'found': False}), \
                patch('wechatauto.guia.time.sleep'):
            self.assertFalse(gui._search_chat('文件传输助手'))

    def test_nonempty_pane_does_not_confirm_wrong_chat(self):
        gui = cast(Any, object.__new__(WeChatGUI))
        checks = []
        gui._past_deadline = lambda: False
        gui._chat_is_open = lambda _name: (checks.append(_name) or False)
        gui._pane_has_content = lambda: True
        with patch('wechatauto.guia.time.sleep'):
            self.assertFalse(gui._chat_open_confirmed('目标群'))
        self.assertEqual(len(checks), 6)


class TestEmojiNameStripping(unittest.TestCase):
    """emoji 群名（💰🟥CHINA🟦🌏）OCR 读不出 emoji——目标名必须先剥再比。

    2026-11-23 群发实测：三轮「搜索浮层没有唯一候选」，浮层行只剩
    「CHINA」，带着 emoji 的目标名怎么都对不上。
    """

    def test_strip_emoji_variants(self):
        self.assertEqual(_strip_emoji('💰🟥CHINA🟦🌏'), 'CHINA')
        self.assertEqual(_strip_emoji('🦄Web3+AIGC 交流'), 'Web3+AIGC 交流')
        self.assertEqual(_strip_emoji('Axmax产品大师群🍳'), 'Axmax产品大师群')
        self.assertEqual(_strip_emoji('文件传输助手'), '文件传输助手')
        self.assertEqual(_strip_emoji('💰🟥🟦🌏'), '')

