"""群发「执行时间」编辑与保存的排期契约。

2026-11-23 的 bug：编辑 loop 任务时前端把旧 ``next_run`` 原样传回，
后端只在 monthly/weekly 重算排期——daily/interval 改「首次执行时间」
后 ``next_run`` 保持旧值，而编辑框回填和列表显示的恰恰是 ``next_run``，
表现就是「改了执行时间，重新打开仍是旧值」。

修复契约（前端 ``bcSave`` 负责判断）：
- 用户改了执行时间 → 提交 ``next_run=0``，后端按新 anchor 重排；
- 没改执行时间 → 提交旧 ``next_run``，只改文案/目标不打乱排期。
这里锁的是后端对这两种提交的处理。
"""

import os
import sys
import tempfile
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import broadcast  # noqa: E402
from admin import server  # noqa: E402


def _loop_task(next_run: float, anchor: float) -> dict:
    return {
        'id': 'sched-test', 'name': '排期契约测试', 'enabled': True,
        'targets': [{'id': 'filehelper', 'name': '文件传输助手', 'kind': 'user'}],
        'text': '测试内容', 'image': '',
        'schedule': {'type': 'loop', 'repeat': 'daily', 'run_at': 0,
                     'unit': 'day', 'interval': 1,
                     'anchor': anchor, 'weekdays': [], 'month_days': []},
        'next_run': next_run,
    }


class TestBroadcastScheduleEdit(unittest.TestCase):
    """编辑 loop 任务时 next_run 的两种提交语义。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='bcsched-')
        self._orig_tasks = broadcast.TASKS_FILE
        self._orig_lock = broadcast.LOCK_FILE
        self._orig_log = broadcast.BROADCAST_LOG
        broadcast.TASKS_FILE = os.path.join(self.dir, 'tasks.json')
        broadcast.LOCK_FILE = broadcast.TASKS_FILE + '.lock'
        broadcast.BROADCAST_LOG = os.path.join(self.dir, 'broadcast.log')
        self.addCleanup(setattr, broadcast, 'TASKS_FILE', self._orig_tasks)
        self.addCleanup(setattr, broadcast, 'LOCK_FILE', self._orig_lock)
        self.addCleanup(setattr, broadcast, 'BROADCAST_LOG', self._orig_log)

    def test_changed_time_reschedules_from_anchor(self):
        """改了执行时间（next_run=0）：保存后 next_run 必须等于新 anchor。"""
        now = time.time()
        old_next = now + 86400          # 界面回填的旧排期
        new_anchor = now + 2 * 86400    # 用户改成的执行时间
        broadcast.save_tasks([_loop_task(old_next, now - 86400)])
        edited = _loop_task(0, new_anchor)   # 修复后的前端提交形态
        server.save_broadcast(edited)
        saved = broadcast.load_tasks()[0]
        self.assertAlmostEqual(saved['next_run'], new_anchor, delta=120,
                               msg='改了执行时间后 next_run 仍停留在旧排期')
        self.assertAlmostEqual(saved['schedule']['anchor'], new_anchor, delta=1)

    def test_unchanged_time_keeps_schedule(self):
        """没改执行时间（提交旧 next_run）：排期不被打乱。"""
        now = time.time()
        old_next = now + 86400
        anchor = now - 86400
        broadcast.save_tasks([_loop_task(old_next, anchor)])
        edited = _loop_task(old_next, anchor)   # 只改文案时的提交形态
        edited['text'] = '只改了文案'
        server.save_broadcast(edited)
        saved = broadcast.load_tasks()[0]
        self.assertAlmostEqual(saved['next_run'], old_next, delta=1,
                               msg='没改时间时排期不应被重算打乱')
        self.assertEqual(saved['text'], '只改了文案')

    def test_once_task_run_at_always_takes_effect(self):
        """once 任务改执行时间：run_at 直接驱动 next_run（这条链路本来就通）。"""
        now = time.time()
        run_at = now + 3 * 86400
        task = {
            'id': 'once-test', 'name': '一次性排期', 'enabled': True,
            'targets': [{'id': 'filehelper', 'name': '文件传输助手', 'kind': 'user'}],
            'text': 'x', 'image': '',
            'schedule': {'type': 'once', 'repeat': '', 'run_at': run_at,
                         'unit': 'day', 'interval': 1,
                         'anchor': 0, 'weekdays': [], 'month_days': []},
            'next_run': 0,
        }
        server.save_broadcast(task)
        saved = next(t for t in broadcast.load_tasks() if t['id'] == 'once-test')
        self.assertAlmostEqual(saved['next_run'], run_at, delta=1)


if __name__ == '__main__':
    unittest.main()

    def test_save_keeps_progress_done(self):
        """编辑保存不清断点：11-23 实锤 done 丢失会让已发过的群重发。"""
        old = _loop_task(time.time() + 3600, time.time())
        old['targets'] = [{'id': 'a', 'name': '群A', 'kind': 'group'},
                          {'id': 'b', 'name': '群B', 'kind': 'group'}]
        old['progress'] = {'run_id': 'r1', 'started_at': 1.0, 'done': ['a']}
        broadcast.save_tasks([old])
        edited = _loop_task(old['next_run'], old['schedule']['anchor'])
        edited['id'] = 'sched-test'
        edited['targets'] = old['targets']      # 管理页编辑目标列表后保存
        server.save_broadcast(edited)
        after = broadcast.load_tasks()[0]
        self.assertEqual(after['progress']['done'], ['a'])
