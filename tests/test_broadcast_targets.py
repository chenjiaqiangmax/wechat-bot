# -*- coding: utf-8 -*-
"""群发目标搜索：按微信号（alias）必须搜得到，且 alias 要带给前端。

2026-11-20 的 bug：``search_contact`` 本来就搜 ``alias``，但
``broadcast_targets`` 没把 alias 返回出去，前端 ``bcHit`` 只比
name/remark/nick/id——用微信号搜到时这条被补查合并进来，紧接着又被
本地过滤滤掉，表现就是「按微信号搜不到人」。
"""

import unittest

from admin import server


class _FakeDB:
    """只实现 search_contact 的通讯录替身，按关键词过滤固定几行。"""

    def __init__(self, rows):
        self._rows = rows

    def search_contact(self, keyword, limit=50):
        """复刻真实实现的匹配面：昵称/备注/username/alias 都算命中。"""
        k = (keyword or '').lower()
        return [r for r in self._rows
                if k in (r.get('nick_name') or '').lower()
                or k in (r.get('remark') or '').lower()
                or k in (r.get('username') or '').lower()
                or k in (r.get('alias') or '').lower()][:limit]


class TestBroadcastTargetsByAlias(unittest.TestCase):
    """按微信号搜索的字段契约。"""

    def setUp(self):
        self._orig_db = server._get_db
        self._orig_chats = server.list_chats
        server.list_chats = list   # 空会话列表：只测通讯录那一路
        server._get_db = lambda: _FakeDB([
            {'username': 'zhicaiyan002', 'nick_name': '初见 久伴',
             'remark': '上海支联', 'alias': 'zcyw8023'},
        ])

    def tearDown(self):
        server._get_db = self._orig_db
        server.list_chats = self._orig_chats

    def test_alias_hit_is_returned_with_alias_field(self):
        got = server.broadcast_targets('zcyw8023')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]['id'], 'zhicaiyan002')
        self.assertEqual(got[0]['kind'], 'user')
        self.assertEqual(got[0]['alias'], 'zcyw8023',
                         '前端按 alias 做本地过滤，这个字段必须返回')

    def test_chat_list_entries_carry_empty_alias(self):
        server.list_chats = lambda: [{'id': 'filehelper', 'name': '文件传输助手'}]
        got = server.broadcast_targets('')
        self.assertEqual(got[0]['id'], 'filehelper')
        self.assertEqual(got[0]['alias'], '')


if __name__ == '__main__':
    unittest.main()