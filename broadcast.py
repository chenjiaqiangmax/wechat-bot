# -*- coding: utf-8 -*-
"""群发任务：数据模型、持久化与调度执行。

任务存在 ``broadcast_tasks.json``（与 ``bot_config.json`` 同目录），**由机器人
进程调度执行**，管理后台只读写同一个文件——只有机器人持有微信窗口，后台
自己发会跟自动回复抢窗口和焦点。

时间模型用「间隔」而不是 cron：

- 一次性任务记 ``run_at``，执行完自动停用；
- 循环任务记 ``unit``（hour / day）+ ``interval``（每 N 个），换算成秒后累加。

「每天」「每 2 天」「每 7 天」「每 N 小时」因此是同一个公式，界面上分两组
只是为了让人少算一次。

**文本 + 图片不是一条消息**：微信没有「一条消息既带文字又带图」的类型，
实际发送是先文本、紧接着图片两条。结果按「文本 1 条 + 图片 1 张」记录，
不假装合并。
"""

import contextlib
import json
import os
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
TASKS_FILE = os.path.join(ROOT, 'broadcast_tasks.json')
# 群发专用日志：逐目标一行，与 broadcast_tasks.json 的 last_results 同源。
BROADCAST_LOG = os.path.join(ROOT, 'broadcast.log')
LOCK_FILE = TASKS_FILE + '.lock'

STATUS_TEXT = {'success': '成功', 'partial': '部分成功', 'failed': '失败',
               'retry': '待重试'}

DAY_SECONDS = 86400

# 临时不可用（锁屏 / 断屏 / 微信未就绪）时的重试节奏。
# 这类失败不该消耗一个排期：每 2 天的任务因为锁屏 5 分钟就丢掉一次，不合理。
TRANSIENT_RETRY_SECONDS = 300     # 每 5 分钟重试一次
MAX_TRANSIENT_RETRIES = 12        # 连续 12 次（约 1 小时）仍不可用，就认作本次失败

# 单目标当场重试：只对「过一会儿可能就好」的失败用。
# 3 次 × 25 秒（打开会话上限）≈ 75 秒是单个坏目标的耗时上限，
# 不会把一整轮拖成几十分钟。
TARGET_ATTEMPTS = 3
TARGET_RETRY_WAIT = 4.0
# 一轮任务的总预算：超过就不再发剩下的目标，记为「跳过」等下轮。
# 目的是防止「10 个目标全部临时失败」时一轮跑掉半小时。
TASK_BUDGET_SECONDS = 900.0

# 失败性质决定「当场重试」还是「留给下一轮」。
FAIL_TRANSIENT = 'transient'      # 锁屏/断屏/UIA 卡住/COM 调用失败：过几分钟会好
FAIL_PERMANENT = 'permanent'      # 目标已不存在、图片缺失：再试一万次也一样
FAIL_UNKNOWN = 'unknown'          # 没见过的原因：按临时处理，当场重试一次

# 先判永久：「目标已不存在」里也有「不存在」，不能被临时规则先吃掉。
_PERMANENT_HINTS = ('目标已不存在', '图片不存在', '不存在', '未找到', '找不到')
_TRANSIENT_HINTS = ('会话不可交互', '未恢复', '桌面不可读', 'UIA 无响应',
                    '窗口不可见', 'COMError', '事件无法调用任何订户',
                    '超过时限', '打开会话超过', '超时', 'timeout',
                    'Timeout', '尚未就绪')


def classify_failure(err: str) -> str:
    """把失败原因归成 FAIL_TRANSIENT / FAIL_PERMANENT / FAIL_UNKNOWN。

    为什么不能只按关键字「是不是临时」判：``_is_transient`` 只覆盖 5 个
    短语，COMError 这类真实会自愈的失败会被当成永久失败、直接顺延一个周期
    （实测发生过：COMError 之后 next_run 被推到两天后）。
    """
    text = err or ''
    for hint in _PERMANENT_HINTS:
        if hint in text:
            return FAIL_PERMANENT
    for hint in _TRANSIENT_HINTS:
        if hint in text:
            return FAIL_TRANSIENT
    return FAIL_UNKNOWN


def is_retryable(kind: str) -> bool:
    """这个性质的失败值不值得当场再试一次。永久失败一律不试。"""
    return kind != FAIL_PERMANENT


def log_line(text: str) -> None:
    """往群发专用日志追加一行（带时间戳）。

    写日志失败绝不能拖垮发送，所以异常只提示不抛。
    """
    line = '[%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), text)
    try:
        with open(BROADCAST_LOG, 'a', encoding='utf-8') as f:
            f.write(line)
    except Exception as e:
        print(f'[群发] 写 {BROADCAST_LOG} 失败：{e}', flush=True)


_PROC_LOCK = threading.Lock()


@contextlib.contextmanager
def tasks_lock():
    """跨进程互斥：后台页与机器人进程都会写 broadcast_tasks.json。

    没有它时「后台保存任务」和「机器人回写执行结果」可能互相覆盖：后写的
    那份把对方刚写的字段抹掉。Windows 用 msvcrt 的 1 字节文件锁；
    其它平台退化成进程内锁。
    """
    if os.name != 'nt':
        with _PROC_LOCK:
            yield
        return
    import msvcrt
    f = open(LOCK_FILE, 'a+b')
    try:
        deadline = time.time() + 10
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        f.close()


def _ts(v) -> float:
    """把任意值转成时间戳；坏值当 0（表示未设置）。"""
    try:
        return float(v) if v else 0.0
    except (TypeError, ValueError):
        return 0.0


def new_id() -> str:
    """任务 id：时间前缀便于人眼排序，后缀防同一秒内重复。"""
    return 'bt_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:4]


def interval_seconds(task: dict) -> int:
    """循环任务的周期长度（秒）；一次性任务返回 0。"""
    sched = task.get('schedule') or {}
    if sched.get('type') != 'loop':
        return 0
    try:
        n = int(sched.get('interval') or 1)
    except (TypeError, ValueError):
        n = 1
    n = max(1, n)
    return n * DAY_SECONDS if sched.get('unit') == 'day' else n * 3600


WEEKDAY_NAMES = ('周日', '周一', '周二', '周三', '周四', '周五', '周六')


def weekday_index(ts: float) -> int:
    """时间戳是星期几：0=周日、1=周一 … 6=周六。

    和界面上 1/2/3/4/5/6/0 的勾选顺序一致。Python 的 tm_wday 是 0=周一，
    所以要把周日从 6 挪到 0，其余顺移。
    """
    return (time.localtime(ts).tm_wday + 1) % 7


def weekdays_label(weekdays) -> str:
    """把 [1,3,5,0] 变成「周一/周三/周五/周日」；空列表表示不限。"""
    if not weekdays:
        return ''
    return '/'.join(WEEKDAY_NAMES[int(w)] for w in weekdays
                    if isinstance(w, int) and 0 <= w <= 6)


REPEAT_KINDS = ('daily', 'weekly', 'interval', 'monthly')


def repeat_kind(task: dict) -> str:
    """任务的周期类型：``daily`` / ``interval`` / ``monthly`` / ``legacy_weekday``。

    ``repeat`` 是 2026-11-16 加的字段。老任务没有它，按原有字段推断——这样
    文件里存着的老任务读出来不报错、触发规则也不变：

    - 带 ``weekdays`` 的（旧版「每周几」）→ ``weekly``，逐周期前进到勾选的星期；
    - 按小时循环，或按天且间隔 > 1 且没勾星期 → ``interval``（界面已不再提供，
      只用于让老任务继续跑）；
    - 其余（按天、间隔 1）→ ``daily``。
    """
    sched = task.get('schedule') or {}
    r = str(sched.get('repeat') or '').strip().lower()
    if r in REPEAT_KINDS:
        return r
    if sched.get('weekdays'):
        return 'weekly'
    try:
        interval = int(sched.get('interval') or 1)
    except (TypeError, ValueError):
        interval = 1
    if sched.get('unit') != 'day' or interval > 1:
        return 'interval'
    return 'daily'


def month_days_of(sched: dict) -> list:
    """清洗 month_days：只留 1-31 的整数，去重排序。"""
    out = []
    for d in (sched.get('month_days') or []):
        try:
            i = int(d)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= 31 and i not in out:
            out.append(i)
    out.sort()
    return out


def _next_monthly(task: dict, now: float, after_run: bool = False):
    """每月发：落在 month_days 指定的某一天，跨月自动跳。

    时间点（时:分）沿用原排期时间（没有就沿用 anchor），所以「每月 6 号
    09:30」这类设置能被保留。``after_run`` 表示刚执行完，今天要跳过。

    2 月 30 号这类不存在的日期不能靠 mktime 顺延——它会悄悄滚到下个月 1/2 号，
    所以落到具体月日之后要回读 ``tm_mon``/``tm_mday`` 校验，对不上就丢掉。
    """
    sched = task.get('schedule') or {}
    days = month_days_of(sched)
    if not days:
        return None
    start = _ts(task.get('next_run')) or _ts(sched.get('anchor')) or now
    lt = time.localtime(start)
    hh, mm = lt.tm_hour, lt.tm_min
    y, m = lt.tm_year, lt.tm_mon
    # 刚发完：今天之后的日期才算；保存重算：今天还没到点也算候选
    floor_day = lt.tm_mday if after_run else lt.tm_mday - 1
    for _ in range(36):          # 三年，足够跨过任何「这些月都没选中日」的组合
        for d in days:
            if d <= floor_day:
                continue
            try:
                ts = time.mktime((y, m, d, hh, mm, 0, 0, 0, -1))
            except (OverflowError, ValueError):
                continue
            got = time.localtime(ts)
            if got.tm_mon != m or got.tm_mday != d:
                continue          # 该月没有这一天（2 月 30 号），跳过
            if ts > now:
                return ts
        m += 1
        if m > 12:
            m, y = 1, y + 1
        floor_day = 0
    return None


def schedule_label(task: dict) -> str:
    """给界面看的一句话周期描述。"""
    sched = task.get('schedule') or {}
    if sched.get('type') != 'loop':
        return '一次性'
    kind = repeat_kind(task)
    if kind == 'monthly':
        ds = month_days_of(sched)
        return '每月 ' + '/'.join(f'{d}号' for d in ds) if ds else '每月（未选日期）'
    n = int(sched.get('interval') or 1)
    base = f'每 {n} 天' if sched.get('unit') == 'day' else f'每 {n} 小时'
    if kind == 'daily':
        return '每天'
    if kind == 'weekly':
        wd = weekdays_label(sched.get('weekdays') or [])
        return f'每周 {wd}' if wd else '每周（未选星期）'
    return base


def image_path(task: dict) -> str:
    """任务的图片绝对路径；没配图片返回空串。"""
    rel = str(task.get('image') or '').strip()
    if not rel:
        return ''
    return rel if os.path.isabs(rel) else os.path.join(ROOT, rel)


def normalize_task(raw: dict) -> dict:
    """补齐字段、收敛脏数据。

    单条坏数据不能让整个列表读不出来，所以这里不抛异常：缺的补默认值，
    类型不对就按默认值走。真正该拦的问题（空目标、没内容）留给 validate。
    """
    targets = []
    for t in (raw.get('targets') or []):
        if isinstance(t, str):
            t = {'id': t, 'name': t}
        if not isinstance(t, dict):
            continue
        tid = str(t.get('id') or '').strip()
        if not tid:
            continue
        kind = str(t.get('kind') or '').strip().lower()
        targets.append({
            'id': tid,
            'name': str(t.get('name') or tid),
            'kind': 'group' if kind == 'group' or tid.endswith('@chatroom')
                    else 'user',
        })

    sched = raw.get('schedule') or {}
    try:
        interval = max(1, int(sched.get('interval') or 1))
    except (TypeError, ValueError):
        interval = 1
    # 星期：只留 0-6 的整数，去重后按键排序。空列表 = 不限（旧任务没有这个
    # 字段，读出来就是空，行为跟以前一样：按 interval 触发）。
    weekdays = []
    for w in (sched.get('weekdays') or []):
        try:
            i = int(w)
        except (TypeError, ValueError):
            continue
        if 0 <= i <= 6 and i not in weekdays:
            weekdays.append(i)
    weekdays.sort()
    # 周期类型：空串表示「老任务，没有这个字段」，由 repeat_kind 按旧字段推断；
    # 不认识的值同样当空处理，不能因为它把任务读坏。
    repeat = str(sched.get('repeat') or '').strip().lower()
    if repeat not in REPEAT_KINDS:
        repeat = ''
    month_days = month_days_of(sched)
    unit = 'day' if sched.get('unit') == 'day' else 'hour'
    # 几种类型互斥：选中一种就把别的类型的输入清掉，持久化里不留会干扰的残值
    if repeat == 'daily':
        interval, unit, weekdays, month_days = 1, 'day', [], []
    elif repeat == 'weekly':
        interval, unit, month_days = 1, 'day', []
    elif repeat == 'interval':
        weekdays, month_days = [], []
    elif repeat == 'monthly':
        interval, unit, weekdays = 1, 'day', []
    return {
        'id': str(raw.get('id') or '').strip() or new_id(),
        'name': str(raw.get('name') or '').strip() or '未命名任务',
        'enabled': raw.get('enabled') is not False,
        'targets': targets,
        'text': str(raw.get('text') or ''),
        'image': str(raw.get('image') or '').strip(),
        'schedule': {
            'type': 'loop' if sched.get('type') == 'loop' else 'once',
            'run_at': _ts(sched.get('run_at')),
            'unit': unit,
            'interval': interval,
            'anchor': _ts(sched.get('anchor')),
            'weekdays': weekdays,
            'repeat': repeat,
            'month_days': month_days,
        },
        'next_run': _ts(raw.get('next_run')),
        'last_run': _ts(raw.get('last_run')),
        'last_status': str(raw.get('last_status') or ''),
        'last_detail': str(raw.get('last_detail') or ''),
        'last_results': [r for r in (raw.get('last_results') or [])
                         if isinstance(r, dict)][:50],
        'run_count': int(raw.get('run_count') or 0),
        'run_now': bool(raw.get('run_now')),
        'retry_count': int(raw.get('retry_count') or 0),
        'created_at': _ts(raw.get('created_at')) or time.time(),
        # 断点续跑用：本轮已确认成功的目标 id。这是白名单字段，漏了它就等于
        # 每次重启都从头重发一遍。
        'progress': _normalize_progress(raw.get('progress')),
    }


def _normalize_progress(raw) -> dict:
    '''任务执行进度：run_id + 本轮已完成的目标 id 列表。

    没有 run_id 或没有 done 就视为「没有进度」，避免读到一个半截对象
    就以为上一轮跑过。
    '''
    if not isinstance(raw, dict):
        return {}
    run_id = str(raw.get('run_id') or '').strip()
    done = [str(x) for x in (raw.get('done') or []) if x]
    if not run_id or not done:
        return {}
    return {'run_id': run_id, 'started_at': _ts(raw.get('started_at')),
            'done': done}


def new_run_id() -> str:
    '''一轮执行的标识，用来判断 progress 属于哪一次。'''
    return 'run_' + time.strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:4]


def validate_task(task: dict) -> None:
    """保存前校验；不合法抛 ValueError，原因直接展示给用户。"""
    if not task.get('targets'):
        raise ValueError('至少要选一个群发目标')
    if not (task.get('text') or '').strip() and not (task.get('image') or '').strip():
        raise ValueError('文本和图片至少要填一个')
    if task.get('image'):
        path = image_path(task)
        if not os.path.isfile(path):
            raise ValueError(f'图片不存在或已被删除：{task["image"]}')
    sched = task.get('schedule') or {}
    if sched.get('type') == 'loop':
        # monthly / weekly 允许一个都不勾：那样任务不排期（不触发），
        # 由 compute_next_run 返回 None，不在这里拦保存。
        kind = repeat_kind(task)
        if kind in ('daily', 'interval') and interval_seconds(task) <= 0:
            raise ValueError('循环间隔必须大于 0')
        if not task.get('next_run'):
            raise ValueError('循环任务要指定首次执行时间')
        for w in (sched.get('weekdays') or []):
            if not isinstance(w, int) or not (0 <= w <= 6):
                raise ValueError('星期设置不合法（只能是 0-6）')
    else:
        if not task.get('next_run'):
            raise ValueError('一次性任务要指定执行时间')


def compute_next_run(task: dict, now: float, after_run: bool = False):
    """算下一次执行时间；一次性任务执行完返回 None。

    ``after_run``：刚执行完，在原来的时间点上加一个周期。
    落后太多时（机器人关了好几天）直接顺延到未来的最近一轮，**不连环补发**。
    """
    sched = task.get('schedule') or {}
    if sched.get('type') != 'loop':
        return None if after_run else (task.get('next_run') or None)
    kind = repeat_kind(task)
    if kind == 'monthly':
        return _next_monthly(task, now, after_run)
    step = interval_seconds(task)
    if step <= 0:
        return None
    nxt = _ts(task.get('next_run')) or _ts(sched.get('anchor')) or now
    if after_run:
        nxt += step
    weekdays = sched.get('weekdays') or []
    if kind == 'weekly':
        if not weekdays:
            # 一周一天都没勾 = 永不触发。不静默退化成「每天」，免得用户以为
            # 没选日期就默认天天发。
            print(f'[群发] 任务 {task.get("name") or task.get("id")} 的「每周」'
                  f'一天都没勾，不排期', flush=True)
            return None
        # 只在勾选的星期触发：从起点逐周期前进，跳过不在名单里的日子。
        # 上界 2000 步防死循环——老任务「每隔 2 天」+「只选周一」这种组合
        # 可能永远撞不上，真撞不上就返回 None（不排期），而不是卡住调度线程。
        for _ in range(2000):
            if nxt > now and weekday_index(nxt) in weekdays:
                return nxt
            nxt += step
        print(f'[群发] 任务 {task.get("name") or task.get("id")} 的星期设置'
              f'（每 {sched.get("interval")} 天 + {weekdays_label(weekdays)}）'
              f'永远不会命中，不排期', flush=True)
        return None
    while nxt <= now:
        nxt += step
    return nxt


def load_tasks() -> list:
    """读任务列表；文件不存在返回空表，坏任务跳过并打日志。"""
    try:
        with open(TASKS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f'[群发] {TASKS_FILE} 读取失败，按空列表处理：{e}', flush=True)
        return []
    items = raw.get('tasks') if isinstance(raw, dict) else raw
    out = []
    for i, item in enumerate(items or []):
        if not isinstance(item, dict):
            continue
        try:
            out.append(normalize_task(item))
        except Exception as e:
            print(f'[群发] 第 {i + 1} 条任务无效，已跳过：{e}', flush=True)
    return out


def save_tasks(tasks: list) -> None:
    """原子写：先写 .tmp 再替换，中途崩掉不会留下半个 JSON。"""
    tmp = TASKS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'tasks': tasks}, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, TASKS_FILE)


class BroadcastScheduler(threading.Thread):
    """机器人进程里的群发调度线程。

    ``send_fn(target, task) -> (ok: bool, error: str)`` 由调用方注入——只有它
    知道怎么驱动微信窗口。``lock`` 传机器人的 ``send_lock``，保证群发不会和
    自动回复同时动窗口。
    """

    def __init__(self, send_fn, lock=None, poll: float = 30.0,
                 send_gap: float = 2.0, start_delay: float = 8.0):
        super().__init__(daemon=True, name='broadcast')
        self._send_fn = send_fn
        self._lock = lock
        self._poll = max(5.0, poll)
        self._gap = max(0.0, send_gap)
        self._start_delay = max(0.0, start_delay)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # 启动后先等一下：微信窗口和数据库刚起来时别抢着发
        if self._start_delay:
            self._stop.wait(self._start_delay)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                # 调度线程绝不能把机器人带崩
                print(f'[群发] 调度异常：{type(e).__name__}: {e}', flush=True)
            self._stop.wait(self._poll)

    def _tick(self) -> None:
        """扫一遍任务表，执行到期或标了「立即执行」的任务。"""
        now = time.time()
        for task in load_tasks():
            if not task.get('enabled'):
                continue
            due = task.get('run_now') or (
                task.get('next_run') and task['next_run'] <= now)
            if not due:
                continue
            self._execute(task)

    def _execute(self, task: dict) -> None:
        """执行一个任务：逐目标发送，汇总结果后回写。

        断点续跑：``progress`` 里已经确认成功的目标不再重发——群发是「发出去
        就收不回」的动作，重复发一次比漏发一次的代价大得多。进程被关窗/被杀
        后重启，只要这一轮没跑完，就从上次的位置接着发。
        """
        started = time.time()
        targets = task['targets']
        prog = task.get('progress') or {}
        done = set(prog.get('done') or [])
        run_id = prog.get('run_id') or new_run_id()
        pending = [t for t in targets if (t.get('id') or '') not in done]

        names = '、'.join(t['name'] for t in targets[:3])
        more = len(targets) - 3
        head = ('任务「%s」开始 id=%s 目标=%d 待发=%d'
                % (task['name'], task['id'], len(targets), len(pending)))
        if done:
            head += ' 续跑（上轮已完成 %d 个）' % len(done)
        log_line('===== ' + head + ' =====')
        print('[群发] 「%s」开始执行 → %s%s（%d 个目标，待发 %d）'
              % (task['name'], names, '…' if more > 0 else '',
                 len(targets), len(pending)), flush=True)

        # 先落盘 run_id：中途被关窗时，重启后能认出这是同一轮
        self._persist(task['id'], {
            'progress': {'run_id': run_id, 'started_at': started,
                         'done': sorted(done)},
        })

        if pending:
            if self._lock is not None:
                with self._lock:
                    results = self._send_all(task, pending, run_id, started)
            else:
                results = self._send_all(task, pending, run_id, started)
        else:
            results = []

        # 续跑时上轮已完成的目标照样记进这一轮结果里，汇总才不会少人
        already = [{'target': t.get('name') or t.get('id'), 'ok': True,
                    'error': '', 'retry': False, 'skipped': True,
                    'note': '上一轮已完成，未重发'}
                   for t in targets if (t.get('id') or '') in done]
        results = already + results

        # 只要有**可重试**的失败就短间隔补发，**不消耗排期**。
        #
        # 原来只在「全部目标都临时失败」时才重试，于是「3 个目标里 1 个
        # COMError」这种最常见的情况被记成 partial、next_run 直接推一整个
        # 周期（每 2 天的任务等于两天后才补）——可恢复的失败被当成了终局。
        #
        # progress 不清空：里面记着这一轮已经送达的目标，补发时只发没成功的。
        retryable_failed = [r for r in results if not r['ok'] and r.get('retry')]
        if retryable_failed:
            retries = int(task.get('retry_count') or 0) + 1
            if retries <= MAX_TRANSIENT_RETRIES:
                ok_n = sum(1 for r in results if r['ok'])
                total = len(results)
                updates = {
                    'last_run': started,
                    'last_status': 'retry',
                    'last_detail': (
                        '已成功 %d/%d，%d 个临时失败，%d 分钟后第 %d/%d 次补发：%s'
                        % (ok_n, total, len(retryable_failed),
                           TRANSIENT_RETRY_SECONDS // 60, retries,
                           MAX_TRANSIENT_RETRIES, retryable_failed[0]['error'])),
                    'last_results': results,
                    'retry_count': retries,
                    'run_now': False,
                    'next_run': time.time() + TRANSIENT_RETRY_SECONDS,
                }
                self._persist(task['id'], updates)
                print('[群发] 「%s」已成功 %d/%d，%d 个临时失败，%d 分钟后补发'
                      '（第 %d/%d 次）：%s'
                      % (task['name'], ok_n, total, len(retryable_failed),
                         TRANSIENT_RETRY_SECONDS // 60, retries,
                         MAX_TRANSIENT_RETRIES, retryable_failed[0]['error']),
                      flush=True)
                log_line('===== 任务「%s」结束：已成功 %d/%d，%d 个临时失败，'
                         '%d 分钟后补发（第 %d/%d 次）====='
                         % (task['name'], ok_n, total, len(retryable_failed),
                            TRANSIENT_RETRY_SECONDS // 60, retries,
                            MAX_TRANSIENT_RETRIES))
                return
            # 连续一小时都没恢复 → 当这次真的失败，顺延一个周期（下方正常分支）

        ok_n = sum(1 for r in results if r['ok'])
        total = len(results)
        if ok_n == total:
            status = 'success'
        elif ok_n:
            status = 'partial'
        else:
            status = 'failed'
        failed = [r for r in results if not r['ok']]
        detail = '%d/%d 个目标成功' % (ok_n, total)
        if failed:
            first = failed[0]
            detail += '；失败：%s（%s）' % (first['target'], first['error'])
            if len(failed) > 1:
                detail += ' 等 %d 个' % len(failed)

        updates = {
            'last_run': started,
            'last_status': status,
            'last_detail': detail,
            'last_results': results,
            'run_count': int(task.get('run_count') or 0) + 1,
            'run_now': False,
            'retry_count': 0,
            # 这一轮已经跑完（不管成没成），续跑标记清掉：下个周期是全新的一轮
            'progress': {},
        }
        nxt = compute_next_run(task, time.time(), after_run=True)
        updates['next_run'] = nxt or 0
        if nxt is None:
            # 一次性任务执行完自动停用，保留记录供事后查看
            updates['enabled'] = False
        self._persist(task['id'], updates)
        elapsed = time.time() - started
        print('[群发] 「%s」%s：%s（%.1fs）'
              % (task['name'], STATUS_TEXT.get(status, status), detail, elapsed),
              flush=True)
        log_line('===== 任务「%s」结束：成功 %d / 失败 %d（共 %d 个目标，%.1fs）====='
                 % (task['name'], ok_n, total - ok_n, total, elapsed))

    def _send_all(self, task: dict, pending: list, run_id: str,
                  started: float) -> list:
        """按目标顺序发送；单个目标失败不影响后面的目标。

        单目标策略：
          - 永久失败（目标不存在、图片缺失）→ 试一次就放弃，不为它浪费一轮；
          - 临时失败（锁屏/断屏/UIA 卡住/COM 调用失败）→ 当场最多试
            ``TARGET_ATTEMPTS`` 次，仍然失败就记 ``retry=True`` 留给下一轮；
          - 未知失败 → 同样当场重试（误判成永久的代价更高）；
          - 整轮耗时超过 ``TASK_BUDGET_SECONDS`` → 剩下的目标记「跳过」，
            不让一个坏目标或一片掉线把这一轮拖成半小时。

        每个逐目标结果同时写进 ``broadcast.log`` 和 ``last_results``，两处同源。
        """
        out = []
        total = len(task['targets'])
        idx = {t.get('id'): i for i, t in enumerate(task['targets'], 1)}
        for target in pending:
            tid = target.get('id') or ''
            name = target.get('name') or tid
            slot = idx.get(tid, 0)
            if time.time() - started > TASK_BUDGET_SECONDS:
                out.append({'target': name, 'ok': False, 'retry': True,
                            'error': '本轮已超 %.0fs 预算，跳过'
                                     % TASK_BUDGET_SECONDS})
                log_line('[%d/%d] %s → 跳过 | 本轮已超 %.0fs 预算，留给下一轮'
                         % (slot, total, name, TASK_BUDGET_SECONDS))
                print('[群发]   %s → 跳过（本轮超时预算）' % name, flush=True)
                continue
            if out and self._gap:
                time.sleep(self._gap)

            t0 = time.time()
            ok, err, retryable, attempts = self._try_target(target, task)
            used = time.time() - t0
            if ok:
                out.append({'target': name, 'ok': True, 'error': '',
                            'retry': False})
                log_line('[%d/%d] %s → 成功 | %.1fs | 第 %d 次尝试'
                         % (slot, total, name, used, attempts))
                # 每成功一个就落盘：中途被关窗，重启后不再重发它
                self._mark_done(task['id'], tid, run_id, started)
            else:
                kind = classify_failure(err)
                out.append({'target': name, 'ok': False, 'error': err,
                            'retry': retryable})
                log_line('[%d/%d] %s → 失败 | %.1fs | 第 %d 次尝试 | %s | %s'
                         % (slot, total, name, used, attempts, kind, err))
            print('[群发]   %s → %s'
                  % (name, '成功' if ok else ('失败：' + (err or '未知错误'))),
                  flush=True)
        return out

    def _try_target(self, target: dict, task: dict) -> tuple:
        """发一个目标，按失败性质决定当场重不重试。

        返回 ``(ok, error, retryable, attempts)``：``retryable`` 告诉调度器
        「这个失败是临时的，下一轮值得再试」。
        """
        err = ''
        retryable = False
        for attempt in range(1, TARGET_ATTEMPTS + 1):
            try:
                res = self._send_fn(target, task)
                ok = bool(res[0])
                err = res[1] if len(res) > 1 else ''
                retryable = bool(res[2]) if len(res) > 2 else False
            except Exception as e:
                ok, err, retryable = False, '%s: %s' % (type(e).__name__, e), True
            if ok:
                return True, '', False, attempt
            kind = classify_failure(err)
            if kind == FAIL_PERMANENT or not retryable:
                return False, err or '未知错误', retryable, attempt
            if attempt >= TARGET_ATTEMPTS:
                break
            log_line('  ↳ 第 %d 次失败（%s），%.0fs 后重试：%s'
                     % (attempt, kind, TARGET_RETRY_WAIT, err))
            print('[群发]   第 %d 次失败（%s），%.0fs 后重试'
                  % (attempt, kind, TARGET_RETRY_WAIT), flush=True)
            time.sleep(TARGET_RETRY_WAIT)
        return (False, err or '未知错误',
                classify_failure(err) != FAIL_PERMANENT, TARGET_ATTEMPTS)

    def _mark_done(self, task_id: str, target_id: str, run_id: str,
                   started: float) -> None:
        """把「这个目标本轮已成功」落盘，重启后据此跳过它。"""
        with tasks_lock():
            tasks = load_tasks()
            for t in tasks:
                if t['id'] != task_id:
                    continue
                prog = t.get('progress') or {}
                done = set(prog.get('done') or [])
                done.add(target_id)
                t['progress'] = {
                    'run_id': prog.get('run_id') or run_id,
                    'started_at': prog.get('started_at') or started,
                    'done': sorted(done),
                }
                break
            save_tasks(tasks)

    @staticmethod
    def _persist(task_id: str, updates: dict) -> None:
        """按 id 合并回写（持跨进程锁）。

        执行期间用户可能正在后台改别的任务，所以这里重新读一次再写，
        只动自己那条；外面的 tasks_lock 防止后台页同时在写、互相覆盖。
        """
        with tasks_lock():
            tasks = load_tasks()
            for t in tasks:
                if t.get('id') == task_id:
                    t.update(updates)
                    break
            else:
                return
            save_tasks(tasks)
