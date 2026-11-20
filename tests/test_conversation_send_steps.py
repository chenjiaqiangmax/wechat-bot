# -*- coding: utf-8 -*-
"""后台对话记录必须能看到发送阶段的成败（2026-11-20 重复发送事故的回归钉）。

事故复盘：机器人 14:17:53 已把回复发出并入库，但像素级确认失败判成
「未确认」，又重发了两条；管理页对话记录里 steps 只有 llm 一行——
[发送]/[待发] 事件被 build_conversations 整体丢弃，「发出去了没有」
在后台完全不可见。
"""

import unittest
from datetime import datetime

from admin.server import build_conversations


def _ev(tag, body, hms='14:17:00'):
    t = datetime(2026, 11, 20, *map(int, hms.split(':')))
    return {'t': t, 'tag': tag, 'body': body}


class TestConversationSendSteps(unittest.TestCase):
    """[发送]/[待发] 要挂到对话上，成败要反映到状态。"""

    def _events(self):
        """复刻 14:17:31 那次事故的日志事件序列。"""
        return [
            _ev('收到', "<大肥鱼> 强 | 强: '你又在摸鱼'", '14:17:31'),
            _ev('llm', 'deepseek-v4.1-flash @ newapi | 入 2595 出 25', '14:17:32'),
            _ev('回复', "(1.1s) '才不是，我这是战略性地节省体力！'", '14:17:32'),
            _ev('待发', 'chenjiaqiang97：答案已保存，等待送达', '14:17:33'),
            _ev('发送', "回答失败（{'message': '已尝试发送但结果未确认'}），4s 后重试第 2/4 次", '14:17:57'),
            _ev('发送', "回答失败（{'message': '已尝试发送但结果未确认'}），4s 后重试第 3/4 次", '14:18:13'),
            _ev('发送', '回答已发出（第 3 次尝试，71.1s）', '14:18:44'),
            _ev('待发', 'chenjiaqiang97：已送达并移出待发', '14:18:44'),
        ]

    def test_send_events_attach_to_the_conversation(self):
        convs = build_conversations(self._events())
        self.assertEqual(len(convs), 1)
        tags = [s['tag'] for s in convs[0]['steps']]
        self.assertEqual(tags.count('发送'), 3, '[发送] 事件被丢弃了')
        self.assertEqual(tags.count('待发'), 2, '[待发] 事件被丢弃了')

    def test_final_success_keeps_status_ok(self):
        convs = build_conversations(self._events())
        self.assertEqual(convs[0]['status'], 'ok',
                         '最终已发出，对话不能标成失败')

    def test_give_up_marks_fail(self):
        events = self._events()[:5] + [
            _ev('!!!', '发送失败（已重试 3 次）：已尝试发送但结果未确认', '14:21:31'),
        ]
        convs = build_conversations(events)
        self.assertEqual(convs[0]['status'], 'fail',
                         '重试全部失败后对话必须标成 fail')

    def test_send_goes_to_last_replied_not_newest_question(self):
        """[回复] 之后插进别的群的新问题，发送事件不能挂错对话。"""
        events = [
            _ev('收到', '<大肥鱼> 群A | 甲: ' + repr('问题A'), '14:17:31'),
            _ev('回复', "(1.1s) '答案A'", '14:17:32'),
            _ev('收到', '<大肥鱼> 群B | 乙: ' + repr('问题B'), '14:17:40'),
            _ev('发送', '回答已发出（第 1 次尝试，9.0s）', '14:17:45'),
        ]
        convs = build_conversations(events)
        self.assertEqual(len(convs), 2)
        self.assertEqual([s['tag'] for s in convs[0]['steps']], ['发送'],
                         '发送事件挂到了更新的问题上')
        self.assertEqual(convs[1]['steps'], [])


if __name__ == '__main__':
    unittest.main()
