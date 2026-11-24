"""「大肥鱼」管理后台：纯标准库 HTTP 服务 + 单页前端。

功能：机器人进程启停、运行状态、今日统计（解析 dafeiyu_bot.log）、
对话记录、实时日志、配置编辑（写 bot_config.json，机器人启动时覆盖常量）、
表情包管理、LLM 节点探测与链路测试。

只监听 127.0.0.1：页面能看到 API Key、能动键鼠，绝不暴露到局域网。

用法（仓库根目录）：
    python -m admin                 # 默认 http://127.0.0.1:8765
    python -m admin --port 9000
"""

import ast
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import psutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 让 `python -m admin` 之外的方式（比如从别处 import）也能找到根目录的 broadcast.py
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import broadcast
import keyword_ads  # type: ignore[import-not-found]

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
BOT_SCRIPT = os.path.join(ROOT, 'dafeiyu_bot.py')
BOT_LOG = os.path.join(ROOT, 'dafeiyu_bot.log')
BOT_STDERR = os.path.join(ROOT, 'dafeiyu_bot.stderr.log')
CONFIG_FILE = os.path.join(ROOT, 'bot_config.json')
START_CMD = os.path.join(ROOT, 'start_bot.cmd')
STICKER_DIR = os.path.join(ROOT, 'stickers')
STICKER_EXTS = ('.gif', '.jpg', '.jpeg', '.png', '.webp', '.bmp')

# 管理页可编辑的配置项：键名与 dafeiyu_bot._OVERRIDABLE 保持一致
CONFIG_KEYS = ('API_BASE_URL', 'API_KEY', 'MODEL', 'AT_NAME',
               'RECENT_N', 'MAX_TOKENS', 'LLM_TIMEOUT', 'TOOL_ROUNDS',
               'SEARCH_DAYS', 'CONTEXT_TOKEN_LIMIT', 'CHATGPT_COOLDOWN',
               'CHATGPT_TIMEOUT', 'SEND_RETRIES', 'SYSTEM_PROMPT', 'FAIL_REPLY',
               'WECHAT_DISPLAY', 'WECHAT_DISPLAY_ENABLED')
# 旧版「主机 + 端口」字段：读到就迁移成 base_url，写盘时不再保留
_LEGACY_KEYS = ('API_HOSTS', 'API_PORT')
_ENV_KEYS = {'WECHAT_DEEPSEEK_BASE_URL': 'API_BASE_URL',
             'WECHAT_DEEPSEEK_API_KEY': 'API_KEY',
             'WECHAT_DEEPSEEK_MODEL': 'MODEL',
             # 旧环境变量名仍然认，迁移到 base_url
             'WECHAT_DEEPSEEK_API_HOSTS': '_LEGACY_HOSTS',
             'WECHAT_DEEPSEEK_API_PORT': '_LEGACY_PORT'}
# API Key 在接口里永不回显：GET 返回空串 + api_key_set 标记，
# 保存时传空串表示「不修改」。
API_KEY_MASK = ''

_LINE_RE = re.compile(r'^\[(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\] (.*)$')
_TAG_RE = re.compile(r'^\[([^\]]+)\] ?(.*)$')
_RECV_RE = re.compile(r'^(?:<([^>]*)> )?(.*?) \| (.*?): (.*)$')   # 可选 <Agent> 前缀
_REPLY_RE = re.compile(r'^\(([\d.]+)s\) (.*)$')

_proc_lock = threading.Lock()
# 管理页自己拉起的机器人句柄：自动重启只认它，不接管一键启动.bat 起的进程
_bot_proc = None
# 用户主动停止（/api/bot/stop 或 restart）时置位，监督线程不要把它拉起来
_bot_stop_requested = False


# ------------------------- 配置 -------------------------

def _source_defaults() -> dict:
    """从 dafeiyu_bot.py 源码里静态读出常量默认值（不 import，免得拉起 wechatauto）。"""
    out = {}
    try:
        with open(BOT_SCRIPT, encoding='utf-8') as f:
            tree = ast.parse(f.read())
    except Exception:
        return out
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in CONFIG_KEYS:
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except Exception:
                pass                    # 带 os.environ 的表达式跳过，走环境变量兜底
    out.setdefault('API_BASE_URL', 'http://127.0.0.1:8000/v1')
    out.setdefault('API_KEY', '')
    out.setdefault('MODEL', 'deepseek-v4')
    # 同样是 os.environ.get 包着、literal_eval 读不到，这里补默认值；
    # 漏了这条页面会显示空，一保存就把「固定到虚拟屏」关掉了
    out.setdefault('WECHAT_DISPLAY', 'IddSampleDriver')
    # 虚拟屏总开关；关掉后机器人完全不碰微信窗口位置。默认开，与老行为一致
    out.setdefault('WECHAT_DISPLAY_ENABLED', True)
    return out


def _start_cmd_env() -> dict:
    """解析 start_bot.cmd 里的 `set WECHAT_DEEPSEEK_*=...` 当初始值。"""
    out = {}
    try:
        with open(START_CMD, encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'\s*set\s+(\w+)=(.*)$', line, re.IGNORECASE)
                if m and m.group(1) in _ENV_KEYS:
                    out[_ENV_KEYS[m.group(1)]] = m.group(2).strip()
    except OSError:
        pass
    # 旧版主机+端口环境变量 → base_url（读到即迁移）
    hosts, port = out.pop('_LEGACY_HOSTS', ''), out.pop('_LEGACY_PORT', '')
    if hosts and 'API_BASE_URL' not in out:
        host = [h.strip() for h in hosts.split(',') if h.strip()][0] if hosts else ''
        if host:
            p = port if str(port).isdigit() else '8000'
            out['API_BASE_URL'] = f'http://{host}:{p}/v1'
    return out


def _migrate_legacy(raw: dict) -> dict:
    """把 bot_config.json 里的旧 API_HOSTS/API_PORT 就地迁移成 API_BASE_URL。

    返回迁移后的 dict（旧键已删除）；没有 base_url 时才迁移，避免覆盖用户新配的。
    """
    if 'API_BASE_URL' in raw:
        raw.pop('API_HOSTS', None)
        raw.pop('API_PORT', None)
        return raw
    hosts = raw.pop('API_HOSTS', None)
    port = raw.pop('API_PORT', None)
    if hosts:
        if isinstance(hosts, str):
            hosts = [h.strip() for h in re.split(r'[,\s]+', hosts) if h.strip()]
        urls = []
        for h in hosts or []:
            h = str(h).strip().rstrip('/')
            if not h:
                continue
            if h.startswith(('http://', 'https://')):
                urls.append(h if h.endswith('/v1') else h + '/v1')
            else:
                p = port if str(port or '').isdigit() else 8000
                urls.append(f'http://{h}:{p}/v1')
        if urls:
            raw['API_BASE_URL'] = ','.join(urls)
    return raw


# 布尔型配置项。不能靠 isinstance(v, bool) 判断：磁盘上若已存成 0/1，
# 读回来就是 int，再保存又会写成 int，形成死循环。用显式名单定死语义。
_BOOL_KEYS = ('WECHAT_DISPLAY_ENABLED',)


def load_config() -> dict:
    """生效配置 = 源码默认值 < start_bot.cmd 环境变量 < bot_config.json。"""
    cfg = _source_defaults()
    cfg.update(_start_cmd_env())
    raw = _migrate_legacy(_read_raw_config())
    for k, v in raw.items():
        if k not in CONFIG_KEYS:
            continue
        cfg[k] = _as_bool(v) if k in _BOOL_KEYS else v
    return cfg


def validate_config(data: dict) -> dict:
    """保存前校验：返回清洗后的值，不合法直接 ValueError（前端会弹错误）。"""
    out = {}
    if 'API_BASE_URL' in data:
        urls = [u.strip() for u in re.split(r'[,\s]+', str(data['API_BASE_URL'] or ''))
                if u.strip()]
        if not urls:
            raise ValueError('base_url 不能为空（例：https://api.deepseek.com/v1）')
        for u in urls:
            if not re.match(r'^https?://[^\s]+$', u):
                raise ValueError(f'base_url 格式不对：{u}（需要以 http:// 或 https:// 开头）')
        out['API_BASE_URL'] = ','.join(u.rstrip('/') for u in urls)
    if 'MODEL' in data:
        model = str(data['MODEL'] or '').strip()
        if not model:
            raise ValueError('模型名不能为空')
        out['MODEL'] = model
    if 'LLM_TIMEOUT' in data:
        try:
            t = int(data['LLM_TIMEOUT'])
        except (TypeError, ValueError):
            raise ValueError('LLM 超时必须是整数秒') from None
        if not 1 <= t <= 3600:
            raise ValueError('LLM 超时需在 1–3600 秒之间')
        out['LLM_TIMEOUT'] = t
    return out


def _as_bool(v) -> bool:
    """按字面解析布尔值。

    `bool('false')` 是 True，配置里传字符串时会把开关又打开；
    而 `isinstance(True, int)` 也为真，直接用 int 分支会把它转成 0/1。
    """
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes', 'on', '是', '开')
    return bool(v)


# bot_config.json 的读-改-写互斥：两个保存请求并发时后写方会抹掉先写方的键
_CONFIG_SAVE_LOCK = threading.Lock()


def save_config(data: dict) -> dict:
    checked = validate_config(data)
    # 读-改-写整段持锁：ThreadingHTTPServer 每请求一线程，两个保存请求
    # 并发时后写的一方会把对方的键整个抹掉（tmp+replace 只保证不半截，
    # 不保证不丢更新）。机器人只在启动时读这个文件，进程内锁足够。
    with _CONFIG_SAVE_LOCK:
        cfg = load_config()
        for k in CONFIG_KEYS:
            if k not in data:
                continue
            v = data[k]
            if k == 'API_KEY':
                # 空串 = 保持原密钥不变（页面不回显密钥，用户没重填就传空）
                if str(v or '') == API_KEY_MASK:
                    continue
                v = str(v)
            elif k in _BOOL_KEYS:
                # 必须在 int 分支之前：bool 是 int 的子类，否则 false 会被写成 0
                v = _as_bool(v)
            elif isinstance(cfg.get(k), int) or k in ('RECENT_N', 'MAX_TOKENS',
                                                      'TOOL_ROUNDS', 'SEARCH_DAYS',
                                                      'CONTEXT_TOKEN_LIMIT',
                                                      'CHATGPT_COOLDOWN',
                                                      'CHATGPT_TIMEOUT',
                                                      'SEND_RETRIES', 'LLM_TIMEOUT'):
                v = int(v)
            else:
                v = str(v)
            cfg[k] = v
        cfg.update(checked)
        raw = _migrate_legacy(_read_raw_config())
        raw.update({k: cfg[k] for k in CONFIG_KEYS if k in cfg})
        for k in _LEGACY_KEYS:              # 旧字段不再写盘
            raw.pop(k, None)
        _write_raw_config(raw)
    return public_config(cfg)


def public_config(cfg: dict) -> dict:
    """给前端的配置：密钥换成空串，另给 api_key_set 表示服务端是否已配置。"""
    out = dict(cfg)
    key = str(out.get('API_KEY') or '')
    out['API_KEY'] = API_KEY_MASK
    out['api_key_set'] = bool(key)
    return out


def _read_raw_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_raw_config(raw: dict) -> None:
    tmp = CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ------------------------- 多群 Agent -------------------------

TOOL_NAMES = ('search_group_messages', 'get_ai_news', 'send_sticker',
              'say_to_group', 'ask_chatgpt', 'chatgpt_image')
KNOWN_CHATS_FILE = os.path.join(ROOT, 'known_chats.json')
USAGE_FILE = os.path.join(ROOT, 'usage.jsonl')


def load_agents() -> list:
    """bot_config.json 的 AGENTS 原样返回；没配时给一个绑定 "*" 的默认 Agent。"""
    agents = _read_raw_config().get('AGENTS') or []
    if not agents:
        agents = [{'id': 'default', 'name': load_config().get('AT_NAME', '大肥鱼'),
                   'enabled': True, 'at_name': '', 'groups': ['*'],
                   'system_prompt': '', 'model': '', 'temperature': 0.7,
                   'max_tokens': '', 'recent_n': '', 'tools': list(TOOL_NAMES)}]
    return agents


def save_agents(agents: list) -> list:
    if not isinstance(agents, list) or not agents:
        raise ValueError('至少保留一个 Agent')
    seen, out = set(), []
    for i, a in enumerate(agents):
        aid = re.sub(r'[^\w-]', '', str(a.get('id') or '')) or f'agent{int(time.time())}{i}'
        while aid in seen:
            aid += '_'
        seen.add(aid)
        name = str(a.get('name') or '').strip()
        if not name:
            raise ValueError(f'第 {i + 1} 个 Agent 没有名字')
        num = lambda k, f: f(a[k]) if str(a.get(k, '')).strip() not in ('', 'None') else ''
        out.append({
            'id': aid, 'name': name, 'enabled': bool(a.get('enabled', True)),
            'at_name': str(a.get('at_name') or '').strip(),
            'groups': [str(g).strip() for g in a.get('groups') or [] if str(g).strip()],
            'system_prompt': str(a.get('system_prompt') or ''),
            'model': str(a.get('model') or '').strip(),
            'temperature': num('temperature', float), 'max_tokens': num('max_tokens', int),
            'recent_n': num('recent_n', int),
            'tools': list(TOOL_NAMES) if a.get('tools') is None
            else [t for t in a['tools'] if t in TOOL_NAMES],
        })
    # 同一个群绑给多个启用的 Agent 会让路由取决于顺序，提前拦下
    owner = {}
    for a in out:
        if not a['enabled']:
            continue
        for g in a['groups']:
            if g in owner and g != '*':
                raise ValueError(f'群「{g}」同时绑定给了「{owner[g]}」和「{a["name"]}」')
            owner.setdefault(g, a['name'])
    if sum(1 for a in out if a['enabled'] and '*' in a['groups']) > 1:
        raise ValueError('只能有一个启用的兜底 Agent（绑定 *）')
    with _CONFIG_SAVE_LOCK:
        raw = _read_raw_config()
        if not raw:                         # 第一次保存：把当前生效配置一起落盘
            raw = load_config()
        raw['AGENTS'] = out
        _write_raw_config(raw)
    return out


def _chat_name_by_id() -> dict:
    """known_chats.json 的 {chat_id: 昵称} 映射，供发送事件按会话归属。"""
    try:
        with open(KNOWN_CHATS_FILE, encoding='utf-8') as f:
            known = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: (v.get('name') or '') for k, v in known.items()}


def list_chats() -> list:
    """机器人见过的会话（known_chats.json）+ 日志里出现过的群名，供绑定时挑选。"""
    try:
        with open(KNOWN_CHATS_FILE, encoding='utf-8') as f:
            known = json.load(f)
    except (OSError, ValueError):
        known = {}
    out = [{'id': k, 'name': v.get('name') or k, 'last': v.get('last'),
            'group': v.get('group', k.endswith('@chatroom'))} for k, v in known.items()]
    names = {c['name'] for c in out}
    for c in build_conversations(parse_events()):
        if c['group'] not in names:
            names.add(c['group'])
            out.append({'id': '', 'name': c['group'], 'last': c['ts'], 'group': True})
    out.sort(key=lambda c: (not c['group'], -(c['last'] or 0)))
    return out


# ------------------------- 群发任务 -------------------------
# 任务文件由 broadcast.py 定义，机器人进程负责调度执行；后台只读写。
_db_singleton = None


def _get_db():
    """后台进程复用一个 WeChatDB：首次解密约 6 秒，之后走缓存。"""
    global _db_singleton
    if _db_singleton is None:
        from wechatauto.db import WeChatDB
        _db_singleton = WeChatDB()
    return _db_singleton


def list_broadcast() -> list:
    """任务列表，附界面要用的派生字段（周期文案、状态文案、是否到期）。"""
    now = time.time()
    out = []
    for t in broadcast.load_tasks():
        item = dict(t)
        item['schedule_label'] = broadcast.schedule_label(t)
        item['status_label'] = broadcast.STATUS_TEXT.get(t['last_status'], '未执行')
        item['due'] = bool(t['enabled'] and t['next_run'] and t['next_run'] <= now)
        item['has_image'] = bool(t['image'])
        out.append(item)
    # 启用的排前面，同组按下次执行时间
    out.sort(key=lambda t: (not t['enabled'], t['next_run'] or float('inf')))
    return out


def broadcast_targets(q: str = '') -> list:
    """可选目标：机器人见过的会话 + 通讯录。

    通讯录有两万多行（含 92 个群、95 个公众号、一批空昵称），全量返回既
    没必要也会把列表塞满，所以按「有备注优先、其次昵称」取前 400 个；
    有关键词时交给 search_contact 匹配（上限 200），匹配面更全。
    """
    q = (q or '').strip()
    out, seen = [], set()
    for c in list_chats():
        if not c.get('id'):
            continue
        name = c.get('name') or c['id']
        if q and q.lower() not in name.lower() and q.lower() not in c['id'].lower():
            continue
        out.append({'id': c['id'], 'name': name, 'nick': '', 'remark': '',
                    'alias': '',
                    'kind': 'group' if c.get('group') else 'user'})
        seen.add(c['id'])
    try:
        for r in (_get_db().search_contact(q, 200 if q else 400) or []):
            uid = (r.get('username') or '').strip()
            if not uid or uid in seen:
                continue
            if uid.endswith('@chatroom') or uid.startswith('gh_'):
                continue          # 群已经在 known_chats 里；公众号不在这里选
            name = (r.get('remark') or r.get('nick_name') or '').strip()
            # 空名字和纯符号昵称（'#'、'$'、空格）认不出是谁，点了也分不清，
            # 留着只会把正常好友挤到列表后面
            if not name or not re.search(r'[0-9A-Za-z\u4e00-\u9fff]', name):
                continue
            out.append({
                'id': uid, 'name': name, 'kind': 'user',
                # 备注和昵称分开给前端：搜索时「备注命中」要排在「昵称命中」
                # 前面，只有 name 分不出这一条是备注还是昵称。
                'remark': (r.get('remark') or '').strip(),
                'nick': (r.get('nick_name') or '').strip(),
                # alias（微信号）也必须带出去：搜微信号时昵称/备注/username
                # 都不含这个词，前端只按 name 过滤会把命中的这条再滤掉，
                # 表现就是「按微信号搜不到」。
                'alias': (r.get('alias') or '').strip(),
            })
            seen.add(uid)
    except Exception as e:
        # 通讯录读不到（微信没登录/密钥还没取到）不该让整个选择器打不开
        print(f'[群发] 通讯录搜索失败：{type(e).__name__}: {e}', flush=True)
    # 群在前、好友在后；同类内保留查询给的顺序（通讯录那边是「备注优先 +
    # 拼音序」）。这里再按名字重排的话，'.'、'A'、数字开头的昵称会全涌到
    # 最前面，备注好友和正常拼音序都被打乱。
    out.sort(key=lambda c: 0 if c['kind'] == 'group' else 1)
    return out


def save_broadcast(data: dict) -> dict:
    """新建或更新一条任务；校验不通过抛 ValueError（前端直接显示原因）。

    整段持 ``broadcast.tasks_lock()``：机器人可能正在回写执行结果，
    不持锁的话两边各读一份旧数据、后写的那份会把对方的改动抹掉。
    """
    task = broadcast.normalize_task(data or {})
    with broadcast.tasks_lock():
        tasks = broadcast.load_tasks()
        old = next((t for t in tasks if t['id'] == task['id']), None)
        if old:
            task['created_at'] = old['created_at']
            # 编辑时没显式改时间就沿用原来的下次执行时间，避免把排期打乱。
            # 判据用 anchor：用户在界面改了执行时间，anchor 跟着变，旧
            # next_run 必须作废、按新 anchor 重排——否则 daily 任务改时间
            # 后 next_run 被旧值压住，界面重开还是旧值（2026-11-23 实测）。
            # once 任务没有 anchor 语义（两边都是 0），走 run_at 覆盖不受影响。
            anchor_changed = (float(old['schedule'].get('anchor') or 0)
                              != float(task['schedule'].get('anchor') or 0))
            if anchor_changed:
                task['next_run'] = 0
            elif not task['next_run']:
                task['next_run'] = old['next_run']
            # 正在跑的那一轮进度要保留，否则保存一下就等于让它从头重发
            if not task.get('progress') and old.get('progress'):
                task['progress'] = old['progress']
        sched = task['schedule']
        if sched['type'] == 'once':
            task['next_run'] = sched['run_at'] or task['next_run'] or time.time()
        elif not task['next_run']:
            task['next_run'] = sched['anchor'] or time.time()
        # 循环任务按新设置重算一次：编辑时沿用的旧 next_run 可能落在用户刚改掉
        # 的那天（旧星期设置、或每月发的选中日期），不重算就会出现「周日也发了」。
        if sched['type'] == 'loop' and broadcast.repeat_kind(task) in ('monthly', 'weekly'):
            base = task['next_run'] or sched['anchor'] or time.time()
            nxt = broadcast.compute_next_run(
                {'schedule': sched, 'next_run': base}, time.time())
            if nxt:
                task['next_run'] = nxt
        broadcast.validate_task(task)
        if old:
            tasks = [task if t['id'] == task['id'] else t for t in tasks]
        else:
            tasks.append(task)
        broadcast.save_tasks(tasks)
    return task


def toggle_broadcast(task_id: str, enabled: bool) -> dict:
    """启用/停用。重新启用循环任务时把过期的 next_run 顺延到未来。"""
    with broadcast.tasks_lock():
        tasks = broadcast.load_tasks()
        for t in tasks:
            if t['id'] != task_id:
                continue
            t['enabled'] = bool(enabled)
            if t['enabled'] and t['schedule']['type'] == 'loop':
                nxt = broadcast.compute_next_run(t, time.time())
                if nxt:
                    t['next_run'] = nxt
            broadcast.save_tasks(tasks)
            return t
    raise ValueError('任务不存在')


def run_broadcast_now(task_id: str) -> dict:
    """打上「立即执行」标记，由机器人下一轮轮询时真实发送。

    不清 ``progress``：上一轮如果被关窗打断，重新点「立即执行」应该是
    接着发剩下的，而不是把已经成功的人再发一遍。
    """
    with broadcast.tasks_lock():
        tasks = broadcast.load_tasks()
        for t in tasks:
            if t['id'] != task_id:
                continue
            if not t['enabled']:
                raise ValueError('任务已停用，先启用再立即执行')
            t['run_now'] = True
            broadcast.save_tasks(tasks)
            return t
    raise ValueError('任务不存在')


def delete_broadcast(task_id: str) -> None:
    """删除任务（正在执行的那一轮不受影响，只是不再排期）。"""
    with broadcast.tasks_lock():
        tasks = broadcast.load_tasks()
        left = [t for t in tasks if t['id'] != task_id]
        if len(left) == len(tasks):
            raise ValueError('任务不存在')
        broadcast.save_tasks(left)


# ------------------------- 关键词广告 -------------------------

def keyword_ads_view() -> dict:
    """关键词广告页要的全量数据：配置、逐群生效规则、最近命中。

    群清单来自机器人见过的会话（known_chats），另外补上配置里手填过、
    但机器人还没见过的群名/群 ID，避免保存后从列表里消失。
    """
    cfg = keyword_ads.load_config()
    groups, seen = [], set()
    for c in list_chats():
        if not c.get('group'):
            continue
        gid = str(c.get('id') or '')
        name = str(c.get('name') or gid)
        key = gid or name
        if not key or key in seen:
            continue
        seen.add(key)
        groups.append({'key': key, 'id': gid, 'name': name})
    for key in cfg['groups']:
        if key in seen:
            continue
        seen.add(key)
        is_id = key.endswith('@chatroom')
        groups.append({'key': key, 'id': key if is_id else '',
                       'name': '' if is_id else key})
    for g in groups:
        # 覆盖键可能是群 ID 也可能是群名，和机器人里 group_rule 的优先级一致
        entry = cfg['groups'].get(g['id']) if g['id'] in cfg['groups'] \
            else cfg['groups'].get(g['name'], {})
        enabled, cooldown = keyword_ads.group_rule(cfg, g['id'] or g['key'],
                                                   g['name'])
        g['enabled'] = enabled
        g['cooldown_seconds'] = cooldown
        g['mode'] = keyword_ads.group_mode(cfg, g['id'] or g['key'],
                                           g['name'])
        g['has_override'] = bool(entry)
        g['override_enabled'] = entry.get('enabled') if entry else None
        g['override_cooldown'] = entry.get('cooldown_seconds') if entry else None
        g['override_mode'] = entry.get('mode') if entry else None
    groups.sort(key=lambda g: (not g['enabled'], g['name'] or g['key']))
    return {'config': cfg, 'groups': groups,
            'hits': keyword_ads.read_hits(100),
            'log_file': os.path.basename(keyword_ads.LOG_FILE)}


# ------------------------- 用量统计 -------------------------

def _read_usage(since: float) -> list:
    recs = []
    for path in (USAGE_FILE + '.1', USAGE_FILE):
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get('ts', 0) >= since:
                        recs.append(r)
        except OSError:
            pass
    return recs


def _agg(recs) -> dict:
    p = sum(r.get('prompt', 0) for r in recs)
    c = sum(r.get('cached', 0) for r in recs)
    return {'calls': len(recs), 'prompt': p, 'cached': c,
            'completion': sum(r.get('completion', 0) for r in recs),
            'hit_rate': round(c / p * 100, 1) if p else None,
            'avg_secs': round(sum(r.get('secs', 0) for r in recs) / len(recs), 2) if recs else None}


def usage_stats(days: int = 7) -> dict:
    now = datetime.now()
    day0 = datetime(now.year, now.month, now.day)
    start = day0 - timedelta(days=days - 1)
    recs = _read_usage(start.timestamp())
    today = [r for r in recs if r['ts'] >= day0.timestamp()]

    daily = []
    for i in range(days):
        d0 = start + timedelta(days=i)
        rs = [r for r in recs if d0.timestamp() <= r['ts'] < (d0 + timedelta(days=1)).timestamp()]
        daily.append({'label': d0.strftime('%m-%d'), **_agg(rs)})
    hourly = []
    for h in range(24):
        h0 = (day0 + timedelta(hours=h)).timestamp()
        hourly.append(_agg([r for r in today if h0 <= r['ts'] < h0 + 3600]))

    def group_by(key, rs):
        g = {}
        for r in rs:
            g.setdefault(r.get(key) or '（未知）', []).append(r)
        return sorted(({'key': k, **_agg(v)} for k, v in g.items()),
                      key=lambda x: -(x['prompt'] + x['completion']))

    names = {a['id']: a['name'] for a in load_agents()}
    by_agent = group_by('agent', recs)
    for a in by_agent:
        a['name'] = names.get(a['key'], a['key'])
    return {
        'days': days, 'total': _agg(recs), 'today': _agg(today),
        'estimated': any(r.get('cached_src') == 'est' for r in recs),
        'server_reported': any(r.get('cached_src') == 'server' for r in recs),
        'daily': daily, 'hourly': hourly,
        'by_agent': by_agent, 'by_chat': group_by('chat_name', recs)[:12],
        'by_kind': group_by('kind', recs), 'by_host': group_by('host', recs),
        'recent': [{**r, 'time': datetime.fromtimestamp(r['ts']).strftime('%m-%d %H:%M:%S'),
                    'agent_name': names.get(r.get('agent'), r.get('agent'))}
                   for r in recs[-30:][::-1]],
    }


# ------------------------- 进程管理 -------------------------

def _bot_env() -> dict:
    """子进程环境：把生效的 LLM 配置（含 start_bot.cmd 里的）透传成环境变量。

    不传的话，没有 bot_config.json 时管理页拉起的机器人/链路测试会退回
    127.0.0.1 默认地址。
    """
    cfg = load_config()
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    env['WECHAT_DEEPSEEK_BASE_URL'] = str(cfg.get('API_BASE_URL') or '')
    env['WECHAT_DEEPSEEK_API_KEY'] = str(cfg.get('API_KEY', ''))
    env['WECHAT_DEEPSEEK_MODEL'] = str(cfg.get('MODEL', ''))
    # 旧变量名不能再漏给子进程，避免两条配置源打架
    for k in ('WECHAT_DEEPSEEK_API_HOSTS', 'WECHAT_DEEPSEEK_API_PORT'):
        env.pop(k, None)
    return env


def is_bot_cmdline(cmd) -> bool:
    """这条命令行是不是**常驻**的机器人进程。

    只认「把 dafeiyu_bot.py 当脚本参数传进去」的进程，按 basename 判：
    `bash -c "python dafeiyu_bot.py"` 的第三个参数是整条命令（一个字符串），
    它以脚本名结尾却不是脚本，按 endswith 判会把它当成机器人——杀掉就是
    杀用户的终端。一次性模式（`--test` / `--search`）不碰微信窗口，也不算。
    """
    if not cmd:
        return False
    if '--test' in cmd or '--search' in cmd:
        return False
    return any(os.path.basename(str(c)) == 'dafeiyu_bot.py' for c in cmd)


def terminate_all(procs, grace: float = 5.0, kill_grace: float = 3.0) -> list:
    """先 terminate，宽限期内没退再 kill。返回仍然活着的 PID 列表。

    「发出去就收不回」的进程先礼后兵：terminate 让它走自己的退出路径
    （删锁、关窗口），卡住不动才 kill。管理页的停止和 `tools/stop_running.py`
    共用这一份，两处判据必须一致。
    """
    for q in procs:
        try:
            q.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=grace)
    for q in alive:
        try:
            q.kill()
        except psutil.NoSuchProcess:
            pass
    if alive:
        _, alive = psutil.wait_procs(alive, timeout=kill_grace)
    return [q.pid for q in alive]


def find_bots(exclude_pid: Optional[int] = None) -> list:
    """列出正在跑的机器人进程（管理页拉起的、start_bot.cmd 起的都算）。

    只看**命令行里真正把 dafeiyu_bot.py 当脚本传进去的 python 进程**：
    光在 cmdline 里搜字符串会把 bash/PowerShell 包装进程也算进来
    （它们的命令行原样带着整条 `python dafeiyu_bot.py`）。``--test`` /
    ``--search`` 这类不碰微信窗口的一次性模式不参与判断。

    ``exclude_pid`` 用于排除调用者自己——机器人启动时拿它查「有没有别的
    实例在跑」（两个实例会抢微信窗口、重复回复、叠加重建显示拓扑）。
    """
    out = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if exclude_pid is not None and p.info['pid'] == exclude_pid:
                continue
            if 'python' not in (p.info['name'] or '').lower():
                continue
            if is_bot_cmdline(p.info['cmdline']):
                out.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def find_bot() -> psutil.Process:
    """找正在跑的机器人进程（不论是管理页拉起的还是 start_bot.cmd 起的）。"""
    bots = find_bots()
    return bots[0] if bots else None


def start_bot() -> dict:
    global _bot_proc, _bot_stop_requested
    with _proc_lock:
        if find_bot():
            return {'ok': False, 'error': '机器人已经在运行'}
        env = _bot_env()
        flags = 0
        if os.name == 'nt':
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        # stdout 丢弃：机器人自己会 tee 到 dafeiyu_bot.log；stderr 留一份兜底
        # 抓 _setup_logging 之前的崩溃（比如 import 失败）
        err = open(BOT_STDERR, 'ab')
        _bot_proc = subprocess.Popen(
            [sys.executable, BOT_SCRIPT], cwd=ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=err, creationflags=flags)
        _bot_stop_requested = False
        err.close()
    for _ in range(20):                 # 等进程出现，最多 2 秒
        time.sleep(0.1)
        if find_bot():
            break
    return {'ok': True}


def stop_bot() -> dict:
    global _bot_proc, _bot_stop_requested
    with _proc_lock:
        # 先置位再杀：否则监督线程看到「进程没了」会立刻又拉起来
        _bot_stop_requested = True
        _bot_proc = None
        p = find_bot()
        if not p:
            return {'ok': False, 'error': '机器人没在运行'}
        terminate_all([p] + p.children(recursive=True), grace=5)
    print(f'[{time.strftime("%m-%d %H:%M:%S")}] [管理] 机器人已被管理页停止', flush=True)
    return {'ok': True}


def _supervise_bot() -> None:
    """机器人意外退出时重新拉起（只管管理页自己 Popen 的那个）。

    不接管 ``一键启动.bat`` 起的机器人：那是用户自己在前台跑的进程，
    后台服务去重启它会把用户的窗口状态搅乱。那条路自己由 bat 的循环负责。

    防反复拉起：5 分钟内崩 3 次就停手并提示看 stderr——连续崩说明是
    代码/环境问题（比如微信没登录），盲目重启只会刷屏。
    """
    global _bot_proc
    restarts = []
    while True:
        time.sleep(3)
        with _proc_lock:
            proc, stopping = _bot_proc, _bot_stop_requested
        if proc is None or stopping or proc.poll() is None:
            continue
        with _proc_lock:
            if _bot_proc is not proc:        # 期间被别人换了句柄，不重复处理
                continue
            _bot_proc = None
            now = time.time()
            restarts = [t for t in restarts if now - t < 300]
            if len(restarts) >= 3:
                print(f'[{time.strftime("%m-%d %H:%M:%S")}] [管理] 机器人 5 分钟内'
                      f'已退出 3 次，停止自动重启；请查 {BOT_STDERR}', flush=True)
                continue
            restarts.append(now)
        print(f'[{time.strftime("%m-%d %H:%M:%S")}] [管理] 机器人退出'
              f'（code {proc.returncode}），3 秒后自动重启', flush=True)
        time.sleep(3)
        start_bot()


# ------------------------- 日志解析 -------------------------

def _read_log_lines() -> list:
    """按时间顺序读 .2/.1/当前日志的所有行。"""
    lines = []
    for path in (BOT_LOG + '.2', BOT_LOG + '.1', BOT_LOG):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                lines.extend(f.read().splitlines())
        except OSError:
            pass
    return lines


def _unrepr(s: str) -> str:
    try:
        v = ast.literal_eval(s)
        return v if isinstance(v, str) else s
    except Exception:
        return s


_EVENTS_CACHE = {'key': None, 'value': []}


def parse_events() -> list:
    """把日志解析成事件列表：[{t, tag, body}]；多行异常栈归到上一事件。

    结果按三个日志文件的 (mtime, size) 缓存：/api/status 每 5 秒轮询、
    /api/conversations 每次按键都调，日志没变时不重复正则扫几 MB 文本。
    """
    keys = []
    for path in (BOT_LOG + '.2', BOT_LOG + '.1', BOT_LOG):
        try:
            st = os.stat(path)
            keys.append((st.st_mtime_ns, st.st_size))
        except OSError:
            keys.append(None)
    key = tuple(keys)
    if key == _EVENTS_CACHE['key']:
        return _EVENTS_CACHE['value']
    events = _parse_events_uncached()
    _EVENTS_CACHE['key'] = key
    _EVENTS_CACHE['value'] = events
    return events


def _parse_events_uncached() -> list:
    events = []
    for raw in _read_log_lines():
        m = _LINE_RE.match(raw)
        if not m:
            if events and not raw.startswith('20'):   # 库日志行跳过，续行并入
                events[-1]['extra'] = events[-1].get('extra', '') + raw + '\n'
            continue
        dt = _mk_dt(m)
        rest = m.group(6)
        tm = _TAG_RE.match(rest)
        if tm:
            events.append({'t': dt, 'tag': tm.group(1), 'body': tm.group(2)})
        elif '已上线' in rest:
            events.append({'t': dt, 'tag': '上线', 'body': rest})
        else:
            events.append({'t': dt, 'tag': '', 'body': rest})
    return events


def _mk_dt(m) -> datetime:
    now = datetime.now()
    dt = datetime(now.year, int(m.group(1)), int(m.group(2)),
                  int(m.group(3)), int(m.group(4)), int(m.group(5)))
    if dt > now + timedelta(days=1):    # 跨年：去年 12 月的日志行
        dt = dt.replace(year=now.year - 1)
    return dt


def build_conversations(events: list) -> list:
    """收到/回复配对成对话，中间的工具调用挂到对话的 steps 上。

    不同群的回调在各自线程里跑，可能交错；按 FIFO 配对，绝大多数情况准确。
    """
    convs, pending = [], []
    # 发送阶段的事件（[发送]/[待发]）挂在「最近一条已生成回复」的对话上：
    # 它们出现在 [回复] 之后，此刻 pending 已空、中间可能插进了别的群的新
    # 问题，挂 convs[-1] 会挂错对话。不挂的话「发出去了没有」在后台完全不
    # 可见——2026-11-20 的重复发送事故里，对话记录只显示回复 ok，两次失败
    # 重试和最终送达全都看不到。
    last_replied = None
    # 多 Agent 之前的日志没有 <Agent> 前缀：算在兜底 Agent 头上
    fallback = next((a['name'] for a in load_agents() if '*' in (a.get('groups') or [])), '')
    for ev in events:
        tag, body = ev['tag'], ev['body']
        if tag == '上线':
            # 重启后上一进程「收到未回复」的条目已无从配对：继续留在队首
            # 会把新会话的回复配到旧问题上（FIFO 弹出）。
            pending.clear()
            continue
        if tag == '收到':
            m = _RECV_RE.match(body)
            if not m:
                continue
            c = {'time': ev['t'].strftime('%Y-%m-%d %H:%M:%S'), 'ts': ev['t'].timestamp(),
                 'agent': m.group(1) or fallback, 'group': m.group(2), 'who': m.group(3),
                 'question': _unrepr(m.group(4)),
                 'answer': None, 'latency': None, 'steps': [], 'status': 'pending'}
            convs.append(c)
            pending.append(c)
        elif tag == '回复' and pending:
            m = _REPLY_RE.match(body)
            c = pending.pop(0)
            if m:
                c['latency'] = float(m.group(1))
                ans = m.group(2)
                at = re.search(r" \[@(.+)\]$", ans)
                if at:
                    ans = ans[:at.start()]
                c['answer'] = _unrepr(ans)
                c['status'] = 'fail' if '晕鱼了' in c['answer'] else 'ok'
            else:
                c['answer'] = body
                c['status'] = 'ok'
            last_replied = c
        elif tag in ('工具', '表情', 'say', 'ChatGPT', 'agent', '!!!', 'llm') and pending:
            pending[0]['steps'].append({'time': ev['t'].strftime('%H:%M:%S'),
                                        'tag': tag, 'body': body})
            if tag == '!!!':
                pending[0]['status'] = 'fail'
        elif tag == '!!!' and convs and ('发送失败' in body or '停止重试' in body):
            (last_replied or convs[-1])['status'] = 'fail'   # 回复生成了但没发出去
        elif tag in ('发送', '待发') and last_replied is not None:
            # 发送阶段的成败与最终送达：没有这几行，后台看不出「发出去了没有」。
            # [待发] 行自带 chat_id、[发送] 行 desc 是「回答@chat_id」——按会话
            # 归属而不是一律挂 last_replied：补发场景里 A 的送达状态不能错挂
            # 到 B 的卡片上（B 在 A 补发前完成了一轮对话）。
            target = last_replied
            m_chat = re.match(r'^(?:回答@)?([\w@.-]+@chatroom|wxid_[\w@.-]+'
                              r'|[A-Za-z][\w@.-]*@[\w.-]+)', body)
            if m_chat:
                name = _chat_name_by_id().get(m_chat.group(1), '')
                same = [c for c in convs if c['group'] == name] if name else []
                if same:
                    target = same[-1]
            target['steps'].append({'time': ev['t'].strftime('%H:%M:%S'),
                                    'tag': tag, 'body': body})
            if tag == '发送' and '已发出' in body:
                target['status'] = 'ok'
    return convs


def compute_stats() -> dict:
    events = parse_events()
    convs = build_conversations(events)
    now = datetime.now()
    today = now.date()
    day_ago = now - timedelta(hours=24)

    today_convs = [c for c in convs if datetime.fromtimestamp(c['ts']).date() == today]
    lat = [c['latency'] for c in today_convs if c['latency'] is not None]
    hourly = [0] * 24
    for c in convs:
        dt = datetime.fromtimestamp(c['ts'])
        if dt >= day_ago:
            idx = 23 - int((now - dt).total_seconds() // 3600)
            if 0 <= idx < 24:
                hourly[idx] += 1
    groups = Counter(c['group'] for c in convs if datetime.fromtimestamp(c['ts']) >= now - timedelta(days=7))
    users = Counter(c['who'] for c in convs if datetime.fromtimestamp(c['ts']) >= now - timedelta(days=7))
    tools = Counter()
    for ev in events:
        if ev['t'].date() != today:
            continue
        if ev['tag'] == '工具':
            tools[ev['body'].split(' ', 1)[0]] += 1
        elif ev['tag'] in ('表情', 'say'):
            tools['send_sticker' if ev['tag'] == '表情' else 'say_to_group'] += 1
        elif ev['tag'] == 'ChatGPT' and '转问' in ev['body']:
            tools['ask_chatgpt'] += 1
    errors = [{'time': e['t'].strftime('%m-%d %H:%M:%S'), 'tag': e['tag'], 'body': e['body'][:300]}
              for e in events if e['tag'] in ('!!!', '回调异常', 'llm')
              or (e['tag'] == 'ChatGPT' and '失败' in e['body'])]

    # 当前运行状态：最后一次上线之后有没有出现「已暂停」
    paused = False
    last_online = None
    for ev in events:
        if ev['tag'] == '上线':
            paused, last_online = False, ev['t']
        elif ev['tag'] == '!!!' and '暂停' in ev['body']:
            paused = True
    return {
        'today': {
            'questions': len(today_convs),
            'replied': sum(1 for c in today_convs if c['status'] == 'ok'),
            'failed': sum(1 for c in today_convs if c['status'] == 'fail'),
            'avg_latency': round(sum(lat) / len(lat), 1) if lat else None,
            'p_max_latency': round(max(lat), 1) if lat else None,
            'tool_calls': sum(tools.values()),
        },
        'hourly': hourly,
        'hour_labels': [(now - timedelta(hours=23 - i)).strftime('%H') for i in range(24)],
        'groups': groups.most_common(8),
        'users': users.most_common(8),
        'tools': tools.most_common(),
        'errors': errors[-30:][::-1],
        'paused': paused,
        'last_online': last_online.strftime('%Y-%m-%d %H:%M:%S') if last_online else None,
        'total_convs': len(convs),
        'usage_today': _agg(_read_usage(datetime(now.year, now.month, now.day).timestamp())),
        'agents_today': Counter(c['agent'] or '未分配' for c in today_convs).most_common(),
    }


def bot_status() -> dict:
    p = find_bot()
    st = {'running': bool(p)}
    if p:
        try:
            st.update(pid=p.pid, started=p.create_time(),
                      uptime=int(time.time() - p.create_time()),
                      memory_mb=round(p.memory_info().rss / 1048576, 1),
                      cpu=p.cpu_percent(interval=0.1))
        except psutil.Error:
            pass
    return st


def tail_log(offset: int, src: str = 'bot', initial_bytes: int = 64_000) -> dict:
    """增量读日志尾部：按字节 offset 续读，首次读或文件变小就从头读尾部。

    src='bot' 读机器人日志；src='broadcast' 读群发专用日志（`broadcast.py`
    的 `log_line` 单独落盘的那份）；src='keyword' 读关键词广告的命中日志。
    几份用同一套机制读，前端只换数据源，历史记录因此和「当前会话」无关——
    首次读取就是文件末尾那一段。
    """
    path = {'broadcast': broadcast.BROADCAST_LOG,
            'keyword': keyword_ads.LOG_FILE}.get(src, BOT_LOG)
    try:
        size = os.path.getsize(path)
    except OSError:
        return {'offset': 0, 'text': '', 'reset': True, 'src': src}
    reset = offset < 0 or offset > size        # 首次读取 / 日志滚动过
    start = max(0, size - initial_bytes) if reset else offset
    with open(path, 'rb') as f:
        f.seek(start)
        data = f.read(min(size - start, 1_000_000))
    text = data.decode('utf-8', errors='replace')
    if reset and start > 0:                    # 丢掉截断的半行
        text = text.split('\n', 1)[-1]
    return {'offset': start + len(data), 'text': text, 'reset': reset, 'src': src}


# ------------------------- LLM 探测 -------------------------

def probe_nodes() -> list:
    """探测每个 base_url：GET {base}/models，看能否连通、模型是否在列表里。

    base_url 已是含 /v1 的完整地址，直接拼 /models；鉴权头仅在配了 key 时带。
    """
    cfg = load_config()
    bases = [u.strip().rstrip('/')
             for u in str(cfg.get('API_BASE_URL') or '').split(',') if u.strip()]
    results = [None] * len(bases)
    headers = {}
    if cfg.get('API_KEY'):
        headers['Authorization'] = f'Bearer {cfg["API_KEY"]}'

    def one(i, base):
        url = base + '/models'
        req = urllib.request.Request(url, headers=headers)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                body = json.loads(r.read())
            models = [m.get('id') for m in body.get('data', [])]
            results[i] = {'base_url': base, 'ok': True,
                          'ms': int((time.time() - t0) * 1000),
                          'models': models, 'has_model': cfg['MODEL'] in models}
        except urllib.error.HTTPError as e:
            results[i] = {'base_url': base, 'ok': False,
                          'ms': int((time.time() - t0) * 1000),
                          'error': f'HTTP {e.code} {e.reason}'}
        except Exception as e:
            results[i] = {'base_url': base, 'ok': False,
                          'ms': int((time.time() - t0) * 1000),
                          'error': str(e)[:200]}

    ts = [threading.Thread(target=one, args=(i, b)) for i, b in enumerate(bases)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return results


def test_llm(question: str, agent: str = '') -> dict:
    """跑 `dafeiyu_bot.py --test`：走完整的提示词 + 工具链路，不连微信。"""
    t0 = time.time()
    try:
        cmd = [sys.executable, BOT_SCRIPT, '--test', question or '在吗？']
        if agent:
            cmd += ['--agent', agent]
        r = subprocess.run(cmd,
                           cwd=ROOT, capture_output=True, timeout=240,
                           env=_bot_env(),
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        out = (r.stdout + r.stderr).decode('utf-8', errors='replace')
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        out, ok = '超时（240s）', False
    return {'ok': ok, 'output': out.strip(), 'seconds': round(time.time() - t0, 1)}


# ------------------------- 表情包 -------------------------

def _safe_sticker(name: str) -> str:
    name = os.path.basename(name or '').strip()
    if not name or name.startswith('.') or not name.lower().endswith(STICKER_EXTS):
        raise ValueError('文件名不合法（只支持 gif/jpg/png/webp/bmp）')
    return os.path.join(STICKER_DIR, name)


def list_stickers() -> list:
    out = []
    try:
        for f in sorted(os.listdir(STICKER_DIR)):
            if f.lower().endswith(STICKER_EXTS):
                st = os.stat(os.path.join(STICKER_DIR, f))
                out.append({'file': f, 'name': os.path.splitext(f)[0],
                            'size': st.st_size, 'mtime': st.st_mtime})
    except OSError:
        pass
    usage = Counter()
    for ev in parse_events():
        if ev['tag'] == '表情':
            usage[ev['body'].split(' ->', 1)[0].strip()] += 1
    for s in out:
        s['used'] = usage.get(s['file'], 0)
    return out


# ------------------------- HTTP -------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'DaFeiYuAdmin/1.0'

    def log_message(self, fmt, *args):         # 静音访问日志（前端每秒轮询）
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path, ctype=None):
        if not os.path.isfile(path):
            return self._json({'error': 'not found'}, 404)
        with open(path, 'rb') as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower()
        ctype = ctype or {'.html': 'text/html; charset=utf-8', '.gif': 'image/gif',
                          '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                          '.webp': 'image/webp', '.bmp': 'image/bmp',
                          '.svg': 'image/svg+xml'}.get(ext, 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        if os.path.basename(os.path.dirname(path)) == 'assets':   # 素材图不常变，让浏览器缓存
            self.send_header('Cache-Control', 'max-age=86400')
        else:
            # 页面本体绝不缓存：改了前端还让浏览器拿旧版，
            # 会出现「保存了没反应 / 字段还是空的」这种假故障
            self.send_header('Cache-Control', 'no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode('utf-8'))

    def _same_origin(self) -> bool:
        """写操作只接受本页发起的请求，挡掉别的网页对 127.0.0.1 的跨站 POST。"""
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        return origin is None or urllib.parse.urlparse(origin).netloc == host

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        try:
            if p in ('/', '/index.html'):
                return self._file(os.path.join(STATIC_DIR, 'index.html'))
            if p == '/api/status':
                return self._json({'bot': bot_status(), **compute_stats()})
            if p == '/api/conversations':
                convs = build_conversations(parse_events())[::-1]
                kw = (q.get('q') or [''])[0].strip().lower()
                if kw:
                    convs = [c for c in convs if kw in (c['question'] or '').lower()
                             or kw in (c['answer'] or '').lower()
                             or kw in c['who'].lower() or kw in c['group'].lower()]
                return self._json(convs[:300])
            if p == '/api/logs':
                src = (q.get('src') or ['bot'])[0]
                if src not in ('bot', 'broadcast', 'keyword'):
                    src = 'bot'
                return self._json(tail_log(int((q.get('offset') or ['-1'])[0]), src))
            if p == '/api/config':
                return self._json({'config': public_config(load_config()),
                                   'file_exists': os.path.exists(CONFIG_FILE)})
            if p == '/api/agents':
                return self._json({'agents': load_agents(), 'tools': TOOL_NAMES,
                                   'defaults': {k: load_config().get(k) for k in
                                                ('AT_NAME', 'MODEL', 'MAX_TOKENS', 'RECENT_N')}})
            if p == '/api/chats':
                return self._json(list_chats())
            if p == '/api/usage':
                return self._json(usage_stats(max(1, min(90, int((q.get('days') or ['7'])[0])))))
            if p == '/api/nodes':
                return self._json(probe_nodes())
            if p == '/api/stickers':
                return self._json(list_stickers())
            if p == '/api/broadcast':
                return self._json({'tasks': list_broadcast()})
            if p == '/api/keyword-ads':
                return self._json(keyword_ads_view())
            if p == '/api/keyword-ads/hits':
                limit = max(1, min(500, int((q.get('limit') or ['100'])[0])))
                return self._json({'hits': keyword_ads.read_hits(limit)})
            if p == '/api/broadcast/targets':
                return self._json({'targets': broadcast_targets(
                    (q.get('q') or [''])[0])})
            if p.startswith('/assets/'):
                name = os.path.basename(urllib.parse.unquote(p[len('/assets/'):]))
                return self._file(os.path.join(STATIC_DIR, 'assets', name))
            if p.startswith('/stickers/'):
                return self._file(_safe_sticker(urllib.parse.unquote(p[len('/stickers/'):])))
            return self._json({'error': 'not found'}, 404)
        except ValueError as e:
            return self._json({'error': str(e)}, 400)
        except Exception as e:
            return self._json({'error': f'{type(e).__name__}: {e}'}, 500)

    def do_POST(self):
        if not self._same_origin():
            return self._json({'error': 'cross-origin request refused'}, 403)
        p = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
            if p == '/api/bot/start':
                return self._json(start_bot())
            if p == '/api/bot/stop':
                return self._json(stop_bot())
            if p == '/api/bot/restart':
                stop_bot()
                time.sleep(1)
                return self._json(start_bot())
            if p == '/api/config':
                cfg = save_config(body)
                return self._json({'ok': True, 'config': cfg})
            if p == '/api/test':
                return self._json(test_llm(body.get('question', ''), body.get('agent', '')))
            if p == '/api/agents':
                return self._json({'ok': True, 'agents': save_agents(body.get('agents'))})
            if p == '/api/stickers/upload':
                path = _safe_sticker(body.get('file'))
                if os.path.exists(path) and not body.get('overwrite'):
                    return self._json({'error': '同名文件已存在'}, 409)
                data = base64.b64decode(body.get('data', '').split(',')[-1])
                if len(data) > 10 * 1024 * 1024:
                    return self._json({'error': '文件超过 10MB'}, 400)
                os.makedirs(STICKER_DIR, exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(data)
                return self._json({'ok': True})
            if p == '/api/stickers/rename':
                src = _safe_sticker(body.get('file'))
                ext = os.path.splitext(src)[1]
                dst = _safe_sticker(body.get('name', '').strip() + ext)
                if os.path.exists(dst):
                    return self._json({'error': '同名文件已存在'}, 409)
                os.replace(src, dst)
                return self._json({'ok': True, 'file': os.path.basename(dst)})
            if p == '/api/stickers/delete':
                os.remove(_safe_sticker(body.get('file')))
                return self._json({'ok': True})
            if p == '/api/keyword-ads/save':
                cfg = keyword_ads.save_config(body or {})
                return self._json({'ok': True, 'config': cfg})
            if p == '/api/broadcast/save':
                return self._json({'ok': True, 'task': save_broadcast(body)})
            if p == '/api/broadcast/toggle':
                return self._json({'ok': True, 'task': toggle_broadcast(
                    str(body.get('id') or ''), bool(body.get('enabled')))})
            if p == '/api/broadcast/run':
                return self._json({'ok': True,
                                   'task': run_broadcast_now(str(body.get('id') or ''))})
            if p == '/api/broadcast/delete':
                delete_broadcast(str(body.get('id') or ''))
                return self._json({'ok': True})
            return self._json({'error': 'not found'}, 404)
        except ValueError as e:
            return self._json({'error': str(e)}, 400)
        except FileNotFoundError:
            return self._json({'error': '文件不存在'}, 404)
        except Exception as e:
            return self._json({'error': f'{type(e).__name__}: {e}'}, 500)


def serve(host: str = '127.0.0.1', port: int = 8765) -> None:
    threading.Thread(target=_supervise_bot, daemon=True,
                     name='bot-supervisor').start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f'大肥鱼管理后台：http://{host}:{port}  （Ctrl+C 退出）', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
