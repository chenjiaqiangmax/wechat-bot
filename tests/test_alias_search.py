# -*- coding: utf-8 -*-
"""按微信号在搜索浮层里定位联系人（纯逻辑，不碰真实微信）。

2026-11-20 的失败：会话名「强」在通讯录里重名（``chenjiaqiang97`` 微信号
God-Cap，另有陌生 wxid 也叫「强」），搜「强」浮层里出现两个人，OCR 认不出
该点谁，于是判「搜索没命中」并放弃——机器人连发 4 次失败后暂停应答。
改成用数据库里的微信号搜之后，浮层里只剩目标一人；下面钉住的就是
「只在联系人区找，不被群聊区的『包含：昵称（微信号）』引走」。
"""

import unittest
from unittest.mock import patch

from wechatauto.guia import _alias_for_name, _display_name_for, _popup_alias_row

# 实测抓到的浮层（搜 God-Cap，frame 368x452，坐标已换算回原尺寸）
POPUP_ROWS = [
    ('常便用', 48, 19, 40, 16),
    ('G)', 313, 65, 20, 16),
    ('徵信号：God-Cap', 80, 79, 120, 16),
    ('群聊', 36, 115, 40, 16),
    ('11，22谷产业园线下局', 81, 150, 150, 18),
    ('包含：强〔信号：God-Cap)', 80, 175, 170, 18),
    ('到`的AntigravityNexus交流群', 37, 214, 200, 18),
    ('包含：强〔信号：God-Cap)', 80, 239, 170, 18),
    ('VR企业发展联盟', 79, 278, 120, 18),
    ('包含：强〔信号：God-Cap)', 80, 303, 170, 18),
    ('亘全部〔1刀', 35, 335, 60, 16),
    ('-/\\¯搜索网结不', 36, 374, 90, 16),
    ('0', 37, 403, 10, 10),
    ('God-Cap', 58, 406, 60, 16),
]


class TestPopupAliasRow(unittest.TestCase):
    """浮层里按微信号找联系人。"""

    def test_picks_contact_entry_above_the_group_section(self):
        hit = _popup_alias_row(POPUP_ROWS, 'God-Cap', 452)
        self.assertIsNotNone(hit, '联系人项没被选中')
        self.assertEqual(hit[2], 79, '应点「微信号：God-Cap」那行，不是群聊里的成员预览')

    def test_group_member_preview_and_web_result_are_ignored(self):
        # 群聊区的三行（175/239/303）和网络结果的 406 都含 alias，都不该被选
        hit = _popup_alias_row(POPUP_ROWS, 'God-Cap', 452)
        self.assertNotIn(hit[2], (175, 239, 303, 406))

    def test_returns_none_when_alias_absent(self):
        self.assertIsNone(_popup_alias_row(POPUP_ROWS, 'not-here', 452))

    def test_group_only_match_is_refused_not_clicked(self):
        """联系人区没命中、只有群聊项写着「包含：强（微信号）」时不能点。

        点它进的是群，消息会发到群里。这是这条改动最怕的错。
        """
        rows = [
            ('群聊', 36, 115, 40, 16),
            ('11，22谷产业园线下局', 81, 150, 150, 18),
            ('包含：强〔信号：God-Cap)', 80, 175, 170, 18),
        ]
        self.assertIsNone(_popup_alias_row(rows, 'God-Cap', 452))

    def test_tiny_alias_is_refused(self):
        self.assertIsNone(_popup_alias_row(POPUP_ROWS, 'Go', 452),
                          '两个字母太容易在群名里误命中')

    def test_without_section_header_falls_back_to_upper_half(self):
        rows = [('微信号：abc12345', 80, 90, 100, 16),
                ('abc12345 搜索结果', 40, 480, 200, 16)]
        hit = _popup_alias_row(rows, 'abc12345', 520)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[2], 90, '下半部分（网络结果）不该被当联系人')

    def test_ocr_confused_alias_still_finds_contact(self):
        """OCR 把 1 读成 l 时仍要命中联系人行。

        2026-11-20 实测：搜东耳映晓的微信号 cx1666688888，浮层里被读成
        ``cxl666688888``，精确包含匹配永不命中，机器人连试 4 轮后暂停。
        """
        rows = [
            ('东豆映晓', 80, 54, 90, 18),
            ('彳1号：cxl666688888', 80, 79, 150, 16),
            ('0Apple-aiEE', 61, 150, 80, 16),
            ('思包含：东巨映〔信号：cxl666688888）', 60, 174, 240, 18),
            ('瘦索网结果', 62, 212, 90, 16),
        ]
        hit = _popup_alias_row(rows, 'cx1666688888', 290)
        self.assertIsNotNone(hit, 'OCR 读成 cxl 后应仍能命中')
        self.assertEqual(hit[2], 79, '应点联系人那行，不是下面的群聊行')

    def test_confused_group_row_alone_is_still_refused(self):
        """只有群聊行含（错读的）alias 时不能点——哪怕它落在上限之内。

        分区标题也可能被 OCR 认错（「群聊」→「0Apple-aiEE」），
        所以群聊项要靠行内固定的「包含：」兜底排除。
        """
        rows = [
            ('东豆映晓', 80, 40, 90, 18),          # 姓名行不含 alias
            ('思包含：东巨映〔信号：cxl666688888）', 60, 90, 240, 18),
        ]
        self.assertIsNone(_popup_alias_row(rows, 'cx1666688888', 290))


class _FakeDB:
    """通讯录替身：两个同名「强」，只有一个带微信号。"""

    def search_contact(self, keyword, limit=50):
        return [{'username': 'chenjiaqiang97', 'nick_name': '强',
                 'remark': '', 'alias': 'God-Cap'},
                {'username': 'wxid_9631276311212', 'nick_name': '强',
                 'remark': '', 'alias': ''}]

    def get_nickname(self, user):
        return '强' if user == 'chenjiaqiang97' else user


class TestDisplayNameFor(unittest.TestCase):
    """机器人传进来的是 chat_id，不是显示名。"""

    def setUp(self):
        patcher = patch('wechatauto.guia._shared_db', return_value=_FakeDB())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_username_becomes_display_name(self):
        self.assertEqual(_display_name_for('chenjiaqiang97'), '强')

    def test_plain_display_name_is_left_alone(self):
        self.assertIsNone(_display_name_for('文件传输助手'))

    def test_alias_lookup_does_not_take_username(self):
        """username 不该被当成显示名去查 alias——否则搜 wxid 又搜不到。"""
        self.assertEqual(_alias_for_name('强'), 'God-Cap')
        self.assertIsNone(_alias_for_name('chenjiaqiang97'))

    def test_same_name_without_alias_still_resolves(self):
        """同名里只有一个带微信号时用它；都不带就返回 None。"""
        self.assertEqual(_alias_for_name('强'), 'God-Cap')


if __name__ == '__main__':
    unittest.main()