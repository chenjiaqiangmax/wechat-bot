"""群发「用户活跃让路」机制。

2026-11-23 实测：用户正在用电脑时硬跑群发，点击全被「落点窗口不属于
微信」拦截（8/11 失败）。修法：``_send_all`` 每个目标前检测用户活跃
（前台不是微信 + 最近 30s 有键鼠输入），活跃就把剩余目标全部记可重试
让路，交给 5 分钟后的补发轮——已发出的目标由 ``progress.done`` 保护，
不会重发。
"""

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import broadcast  # noqa: E402
from broadcast import BroadcastScheduler, _user_active_decide  # noqa: E402


class TestUserActiveDecide(unittest.TestCase):
    """活跃判定纯函数：前台不是微信 + 最近有输入才算活跃。"""

    def test_foreground_other_window_recent_input_is_active(self):
        self.assertTrue(_user_active_decide(False, 5.0))

    def test_foreground_wechat_is_not_active(self):
        # 机器人干活时前台是微信（它自己置前），不能算用户活跃
        self.assertFalse(_user_active_decide(True, 0.0))

    def test_other_window_but_input_idle_is_not_active(self):
        self.assertFalse(_user_active_decide(False, 120.0))


class TestBroadcastYield(unittest.TestCase):
    """_send_all 检测到用户活跃后：剩余目标全部记可重试让路。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='bcyield-')
        self._orig = (broadcast.TASKS_FILE, broadcast.LOCK_FILE,
                      broadcast.BROADCAST_LOG)
        broadcast.TASKS_FILE = os.path.join(self.dir, 'tasks.json')
        broadcast.LOCK_FILE = broadcast.TASKS_FILE + '.lock'
        broadcast.BROADCAST_LOG = os.path.join(self.dir, 'broadcast.log')
        self.addCleanup(setattr, broadcast, 'TASKS_FILE', self._orig[0])
        self.addCleanup(setattr, broadcast, 'LOCK_FILE', self._orig[1])
        self.addCleanup(setattr, broadcast, 'BROADCAST_LOG', self._orig[2])

    def _scheduler(self):
        s = object.__new__(BroadcastScheduler)
        s._gap = 0
        s._try_target = Mock(return_value=(True, '', False, 1))
        s._mark_done = Mock()
        return s

    def _task(self, n=3):
        return {
            'id': 'yield-test', 'name': '让路测试', 'enabled': True,
            'targets': [{'id': f'g{i}', 'name': f'群{i}', 'kind': 'group'}
                        for i in range(n)],
            'text': 'x', 'image': '',
            'schedule': {'type': 'loop', 'repeat': 'daily', 'run_at': 0,
                         'unit': 'day', 'interval': 1, 'anchor': 0,
                         'weekdays': [], 'month_days': []},
            'next_run': 0,
        }

    def test_active_yields_remaining_targets(self):
        """第一个目标成功后检测到活跃：其余目标记让路（可重试），不再发送。"""
        s = self._scheduler()
        task = self._task(3)
        with patch.object(broadcast, '_user_active', return_value=True), \
                patch('broadcast.time.sleep'):
            results = s._send_all(task, task['targets'], 'run-x', time.time())
        # 目标处理顺序：每个目标前先检测——第一个目标就检测到活跃，
        # 三个目标全部记让路，_try_target 一次都不该被调
        s._try_target.assert_not_called()
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertFalse(r['ok'])
            self.assertTrue(r['retry'], '让路目标必须可重试，否则不会补发')
            self.assertIn('让路', r['error'])

    def test_not_active_sends_normally(self):
        """不活跃时照常逐个发送，不产生让路记录。"""
        s = self._scheduler()
        task = self._task(2)
        with patch.object(broadcast, '_user_active', return_value=False), \
                patch('broadcast.time.sleep'):
            results = s._send_all(task, task['targets'], 'run-x', time.time())
        self.assertEqual(s._try_target.call_count, 2)
        self.assertTrue(all(r['ok'] for r in results))


if __name__ == '__main__':
    unittest.main()
