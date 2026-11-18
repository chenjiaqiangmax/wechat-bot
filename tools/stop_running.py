# -*- coding: utf-8 -*-
"""一键启动前把正在跑的机器人停掉，再让调用方起新的。

`一键启动.bat` 的语义是「双击之后系统里从零起一个」。原来单实例锁会让第二次
双击直接打印「已有机器人在运行（PID xxx）」然后退出，用户要的是先停旧的再起
新的，所以把「停」这一步前移到启动脚本里。

停的顺序有讲究：管理后台（`pythonw -m admin`）可能拿着一个它自己 Popen 的
机器人，并在 `_supervise_bot` 里每 3 秒看一眼——直接 terminate 那个进程，监督
线程看到它退出会在 3 秒后重新拉起，和新起的实例撞车（两个进程抢同一个微信
窗口）。所以先调后台的 `/api/bot/stop`：它会置 `_bot_stop_requested`、清
`_bot_proc`，监督线程就不会再管它。后台没在监听时退回直接终止进程。

退出码：0 = 已经没有机器人在跑（本来就没有，或停成功了）；
        1 = 还有停不掉的进程，PID 打在 stderr 里。
"""

import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(ROOT, '.bot.pid')
ADMIN_URL = 'http://127.0.0.1:8765'

# 「哪个进程是机器人」和「怎么停」都只有一份实现，在管理后台里：管理页的
# 启动/停止判定必须和这里完全一致，各写一份迟早出现「管理页说没在跑、
# 脚本却认为有」这种自相矛盾。dafeiyu_bot.py 的重复实例检查也是这么做的。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from admin.server import find_bots, terminate_all       # noqa: E402


def admin_is_up(url=ADMIN_URL, timeout=1.5):
    """管理后台是否在监听（能连上就算，不看返回内容）。"""
    try:
        with urllib.request.urlopen(url + '/api/status', timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def stop_via_admin(url=ADMIN_URL, timeout=3.0):
    """让管理后台自己停机器人（它会把 supervise 的开关一起关掉）。

    返回 True 表示后台在线并已受理；机器人是否真的退出了仍要再查进程。
    """
    try:
        req = urllib.request.Request(
            url + '/api/bot/stop', data=b'', method='POST',
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception:
        return False


def clear_pid_file():
    """删掉旧实例留下的 PID 锁，避免新实例看到死 PID 再提示一次。

    只在确认没有机器人进程在跑之后调用：锁被新实例用着时删掉它，
    单实例保护就形同虚设。
    """
    if not os.path.exists(PID_FILE):
        return False
    try:
        os.remove(PID_FILE)
        return True
    except OSError:
        return False


def main():
    bots = find_bots(exclude_pid=os.getpid())
    if not bots:
        print('[启动] 没有正在运行的机器人，直接启动')
        clear_pid_file()
        return 0

    pids = [p.pid for p in bots]
    print(f'[启动] 发现正在运行的机器人（PID {"、".join(map(str, pids))}），先停掉')

    if admin_is_up():
        # 后台在线：让它自己停，这样监督线程不会把进程重新拉起来
        if stop_via_admin():
            print('[启动] 已请求管理后台停止机器人')
            for _ in range(20):          # 最多等 2 秒让它自己收尾
                time.sleep(0.1)
                if not find_bots(exclude_pid=os.getpid()):
                    break

    left = find_bots(exclude_pid=os.getpid())
    if left:
        still = terminate_all(left)
        if still:
            print('[启动] 停不掉：PID ' + '、'.join(map(str, still)), file=sys.stderr)
            return 1
        print(f'[启动] 已强制结束（PID {"、".join(map(str, pids))}）')
    else:
        print(f'[启动] 已结束（PID {"、".join(map(str, pids))}）')

    if find_bots(exclude_pid=os.getpid()):
        return 1
    if clear_pid_file():
        print('[启动] 已清掉旧实例的 PID 锁')
    return 0


if __name__ == '__main__':
    sys.exit(main())
