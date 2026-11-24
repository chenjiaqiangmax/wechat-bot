"""按微信号在搜索浮层里定位联系人（纯逻辑，不碰真实微信）。

2026-11-20 的失败：会话名「强」在通讯录里重名（``chenjiaqiang97`` 微信号
God-Cap，另有陌生 wxid 也叫「强」），搜「强」浮层里出现两个人，OCR 认不出
该点谁，于是判「搜索没命中」并放弃——机器人连发 4 次失败后暂停应答。
改成用数据库里的微信号搜之后，浮层里只剩目标一人；下面钉住的就是
「只在联系人区找，不被群聊区的『包含：昵称（微信号）』引走」。
"""

import unittest
from unittest.mock import patch

from wechatauto.guia import _alias_for_name, _display_name_for

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


class _FakeDB:
    """通讯录替身：两个同名「强」，只有一个带微信号。"""

    def search_contact(self, keyword, limit=50):
        return [{'username': 'chenjiaqiang97', 'nick_name': '强',
                 'remark': '', 'alias': 'God-Cap'},
                {'username': 'wxid_9631276311212', 'nick_name': '强',
                 'remark': '', 'alias': ''}]

    def get_nickname(self, user):
        return '强' if user == 'chenjiaqiang97' else user


class _CustomIdDB:
    """alias 空、username 就是自定义微信号（青枫燃霞），外加 openim 与内置账号。"""

    def search_contact(self, keyword, limit=50):
        rows = [{'username': 'lrw0591', 'nick_name': '青枫燃霞',
                 'remark': '', 'alias': ''},
                {'username': '25984983780028739@openim', 'nick_name': '代清艺',
                 'remark': '', 'alias': ''},
                {'username': 'filehelper', 'nick_name': '文件传输助手',
                 'remark': '', 'alias': ''},
                {'username': 'wxid_abc123', 'nick_name': 'wxid 联系人',
                 'remark': '', 'alias': ''}]
        return [r for r in rows
                if keyword in ((r.get('nick_name') or '').strip(),
                               (r.get('remark') or '').strip())]

    def get_nickname(self, user):
        return user


class TestUsernameAsAliasFallback(unittest.TestCase):
    """alias 列为空时，非 wxid_ 的自定义 username 就是微信号（2026-11-22 实测：
    「青枫燃霞」username=lrw0591=微信号，contact.alias 反而空；搜索框认它）。"""

    def setUp(self):
        patcher = patch('wechatauto.guia._shared_db', return_value=_CustomIdDB())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_custom_username_is_returned_as_alias(self):
        self.assertEqual(_alias_for_name('青枫燃霞'), 'lrw0591')

    def test_openim_username_is_not_an_alias(self):
        """``数字@openim`` 不是微信号，搜索框不认——返回 None 走显示名搜索。"""
        self.assertIsNone(_alias_for_name('代清艺'))

    def test_builtin_account_is_not_an_alias(self):
        """文件传输助手等内置账号的 username 不能当搜索词。"""
        self.assertIsNone(_alias_for_name('文件传输助手'))

    def test_wxid_username_is_not_an_alias(self):
        """wxid_* 是内部标识，搜索框搜不到，不返回。"""
        self.assertIsNone(_alias_for_name('wxid 联系人'))


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