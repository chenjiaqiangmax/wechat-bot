# -*- coding: utf-8 -*-
"""群聊关键词广告：群里有人聊到配置的关键词时，自动发一条广告。

设计要点：

- 配置放 ``keyword_ads.json``，机器人按文件 mtime 热加载——管理页保存后
  下一条消息就生效，不用重启进程，更不用改代码。
- 每个群可以单独启用/停用，也能单独覆盖冷却秒数；没单独配置的群走
  ``default_group_enabled`` 与 ``cooldown_seconds``。
- 同一个群在冷却期内只发一次。最后发送时间持久化到
  ``keyword_ads_state.json``，机器人重启不会立刻重复发。
- 每次命中（含冷却跳过、发送失败）写两处：
  ``keyword_ads.log`` 给人看，``keyword_ads_hits.jsonl`` 给管理后台表格读。
- 防止机器人自己触发自己：调用方先按 ``msg.attr == 'self'`` 过滤；这里
  再记录最近发出去的广告原文，10 分钟内原样出现就当回显忽略（数据库里
  发送者解析失败时，自发的消息可能被误标成 friend，这是最后一道保险）。

本模块只负责「该不该发、发什么、发过没有」，真正的发送由机器人进程用
``chat.SendMsg`` 完成。
"""

import json
import os
import random
import re
import threading
import time
from collections import deque

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(ROOT, 'keyword_ads.json')
STATE_FILE = os.path.join(ROOT, 'keyword_ads_state.json')
HITS_FILE = os.path.join(ROOT, 'keyword_ads_hits.jsonl')
LOG_FILE = os.path.join(ROOT, 'keyword_ads.log')

MAX_KEYWORDS = 200
MAX_COOLDOWN = 86400          # 冷却上限一天，再长就没有「及时响应」的意义了
HITS_MAX_BYTES = 5_000_000    # 命中明细滚动大小
LOG_MAX_BYTES = 5_000_000     # 人读日志滚动大小
ECHO_WINDOW = 600.0           # 自己发出的广告在这个窗口内原样出现 = 回显
SKIP_LOG_INTERVAL = 60.0      # 同一群的冷却跳过最多每分钟记一次，防刷屏

DEFAULT_KEYWORDS = [
    '便宜的模型', '便宜模型', '便宜的token', '便宜token',
    '便宜的 api', '便宜的api', '便宜api', '低价api', '低价模型',
    '便宜的gpt', '便宜的claude', '便宜的key', '白嫖模型',
]

DEFAULT_REPLY = (
    '看到群里有朋友在找便宜好用的模型 / token，自荐一下：'
    '大肥鱼是一个开源的微信群 AI 机器人，接任意 OpenAI 兼容接口（DeepSeek 等），'
    '@ 一下就回答，带群长期记忆和群消息搜索，部署教程在 '
    'https://github.com/dazhi666666/wechat-deepseek'
)

DEFAULT_SOFT_REPLIES = [
    '刚好看到你在找{keyword}，我这边正好在用一家，给你个参考：\\n{reply}\\n你先看看，不合适就当我没说～',
    '巧了，我最近也在看{keyword}，现在用的是这家：\\n{reply}\\n有需要可以看看，不需要忽略就行。',
    '我这边有个在用的渠道，价格还可以：\\n{reply}\\n你先参考下，合适再说。',
]

DEFAULT_INTENT_WORDS = [
    '有没有', '哪里', '哪儿', '求', '推荐', '渠道', '多少钱', '价格',
    '怎么买', '哪家', '想找', '在找', '需要', '好用', '怎么选',
]

MAX_SOFT_REPLIES = 20
MAX_SOFT_REPLY_CHARS = 1000
MAX_INTENT_WORDS = 200
PLACEHOLDER_RE = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')
ALLOWED_PLACEHOLDERS = {'reply', 'keyword', 'sender'}

DEFAULT_CONFIG = {
    'enabled': True,
    # 未单独配置的群默认不监听：避免刚上线就把广告铺到全部群，
    # 需要哪个群就在管理页单独打开
    'default_group_enabled': False,
    'mode': 'soft',
    'keywords': list(DEFAULT_KEYWORDS),
    'reply': DEFAULT_REPLY,
    'soft_replies': list(DEFAULT_SOFT_REPLIES),
    'soft_require_intent': True,
    'soft_intent_words': list(DEFAULT_INTENT_WORDS),
    'cooldown_seconds': 1800,
    'cooldown_jitter_seconds': 600,
    'reply_probability': 1.0,
    'pre_delay_min_seconds': 1,
    'pre_delay_max_seconds': 5,
    'groups': {},
}

_WS_RE = re.compile(r'\s+')


def _norm(text) -> str:
    """匹配用的归一化：去空白（含全角）、转小写。

    这样「便宜的 token」「便宜的token」「便宜的  TOKEN」都能命中同一条
    关键词，不必让用户为空格和大小写写多个变体。
    """
    return _WS_RE.sub('', str(text or '')).lower()


def _as_bool(v) -> bool:
    """按字面解析布尔值；`bool('false')` 是 True，不能直接用。"""
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes', 'on', '是', '开')
    return bool(v)


def _as_int(v, lo: int, hi: int):
    """把值转成范围内的整数；转不了返回 None（表示「没配置」）。"""
    if v is None or v == '':
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _as_float(v, lo: float, hi: float, default=None):
    """把值转成范围内的浮点数；转不了返回 default。"""
    if v is None or v == '':
        return default
    try:
        n = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _as_text_list(v) -> list:
    """把 textarea/逗号分隔/列表统一成字符串列表。"""
    if isinstance(v, str):
        v = re.split(r'[\n,，、]+', v)
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x or '').strip() for x in v if str(x or '').strip()]


def _clean_soft_replies(raw) -> list:
    """加载配置时清洗软模板：丢坏行，全坏/空则回落内置 3 条。

    保存时由 ``_validate_soft_replies`` 严格报错；加载时不能因为一行坏模板
    就让整个关键词广告失效，所以这里选择跳过坏行。
    """
    items = _as_text_list(raw)
    out = []
    for s in items[:MAX_SOFT_REPLIES]:
        s = s.strip()[:MAX_SOFT_REPLY_CHARS]
        if not s:
            continue
        # 模板以「一行一条」存配置/展示，真正的换行用字面 \n 表示；
        # 渲染时再还原，避免页面按行拆分把一条多行模板拆成几条。
        s = s.replace('\r\n', '\n').replace('\n', '\\n')
        names = set(PLACEHOLDER_RE.findall(s))
        if names - ALLOWED_PLACEHOLDERS or 'reply' not in names:
            continue
        out.append(s)
    return out or list(DEFAULT_SOFT_REPLIES)


def _validate_soft_replies(raw) -> None:
    """保存时严格校验：每条非空模板都必须含 {reply}，且不能有未知占位符。"""
    items = _as_text_list(raw)
    for s in items:
        names = set(PLACEHOLDER_RE.findall(s))
        unknown = names - ALLOWED_PLACEHOLDERS
        if unknown:
            raise ValueError(f'软模板含未知占位符：{", ".join(sorted(unknown))}')
        if 'reply' not in names:
            raise ValueError('每条软模板都必须包含 {reply}，保证第一条有链接/价格')


def _clean_intent_words(raw) -> list:
    """意图词去重、限量；空列表回落默认，避免意图闸门永远关着。"""
    items = _as_text_list(raw)
    out, seen = [], set()
    for s in items[:MAX_INTENT_WORDS]:
        key = _norm(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out or list(DEFAULT_INTENT_WORDS)


def normalize_config(raw: dict) -> dict:
    """把磁盘上或管理页传来的配置清洗成稳定结构。

    缺失字段回落默认值；``keywords`` 显式传空列表时保留空（用户主动清空，
    不替他恢复默认）；非法单条值丢弃而不是让整份配置失效。
    """
    raw = raw if isinstance(raw, dict) else {}

    kws = raw.get('keywords')
    if isinstance(kws, str):
        kws = re.split(r'[\n,，、]+', kws)
    if kws is None:
        kws = list(DEFAULT_KEYWORDS)
    if not isinstance(kws, (list, tuple)):
        kws = []
    out_kw, seen = [], set()
    for k in kws:
        s = str(k or '').strip()
        key = _norm(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out_kw.append(s)
    out_kw = out_kw[:MAX_KEYWORDS]

    reply = raw.get('reply')
    reply = DEFAULT_REPLY if reply is None else str(reply).strip()

    mode = str(raw.get('mode') or 'soft').strip().lower()
    if mode not in ('hard', 'soft'):
        mode = 'soft'
    soft_replies = _clean_soft_replies(raw.get('soft_replies'))
    soft_require_intent = raw.get('soft_require_intent')
    soft_require_intent = (True if soft_require_intent is None
                           else _as_bool(soft_require_intent))
    soft_intent_words = _clean_intent_words(raw.get('soft_intent_words'))

    cooldown = _as_int(raw.get('cooldown_seconds'), 0, MAX_COOLDOWN)
    if cooldown is None:
        cooldown = DEFAULT_CONFIG['cooldown_seconds']
    jitter = _as_int(raw.get('cooldown_jitter_seconds'), 0, MAX_COOLDOWN)
    if jitter is None:
        jitter = DEFAULT_CONFIG['cooldown_jitter_seconds']
    probability = _as_float(raw.get('reply_probability'), 0.0, 1.0, 1.0)
    pre_min = _as_float(raw.get('pre_delay_min_seconds'), 0.0, 15.0, 1.0)
    pre_max = _as_float(raw.get('pre_delay_max_seconds'), 0.0, 15.0, 5.0)
    if pre_min > pre_max:
        pre_min, pre_max = pre_max, pre_min

    groups_raw = raw.get('groups')
    groups = {}
    if isinstance(groups_raw, dict):
        for gid, entry in groups_raw.items():
            key = str(gid or '').strip()
            if not key:
                continue
            entry = entry if isinstance(entry, dict) else {}
            item = {}
            if entry.get('enabled') is not None:
                item['enabled'] = _as_bool(entry.get('enabled'))
            cd = _as_int(entry.get('cooldown_seconds'), 0, MAX_COOLDOWN)
            if cd is not None:
                item['cooldown_seconds'] = cd
            group_mode = str(entry.get('mode') or '').strip().lower()
            if group_mode in ('hard', 'soft'):
                item['mode'] = group_mode
            groups[key] = item

    return {
        'enabled': _as_bool(raw.get('enabled', True)),
        'default_group_enabled': _as_bool(
            raw.get('default_group_enabled', DEFAULT_CONFIG['default_group_enabled'])),
        'mode': mode,
        'keywords': out_kw,
        'reply': reply,
        'soft_replies': soft_replies,
        'soft_require_intent': soft_require_intent,
        'soft_intent_words': soft_intent_words,
        'cooldown_seconds': cooldown,
        'cooldown_jitter_seconds': jitter,
        'reply_probability': probability,
        'pre_delay_min_seconds': pre_min,
        'pre_delay_max_seconds': pre_max,
        'groups': groups,
    }


def load_config(path: str = None) -> dict:
    """读配置文件并清洗；文件不存在用默认值，坏 JSON 则直接停用。

    坏配置时停用而不是套默认关键词，是为了避免「改坏文件 → 机器人拿默认
    广告乱发」这种更糟的结果。
    """
    path = path or CONFIG_FILE
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        return normalize_config({})
    except Exception as e:
        print(f'[关键词广告] {path} 读取失败，已临时停用：{e}', flush=True)
        cfg = normalize_config({})
        cfg['enabled'] = False
        return cfg
    return normalize_config(raw)


def save_config(raw: dict, path: str = None) -> dict:
    """校验并原子写配置；不合法抛 ValueError（管理页直接显示原因）。"""
    raw = raw if isinstance(raw, dict) else {}
    _validate_soft_replies(raw.get('soft_replies'))
    cfg = normalize_config(raw)
    if not cfg['keywords']:
        raise ValueError('至少保留一个关键词')
    if not cfg['reply']:
        raise ValueError('广告文案不能为空')
    path = path or CONFIG_FILE
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return cfg


def group_rule(cfg: dict, chat_id: str, group_name: str = '') -> tuple:
    """解析某个群的实际规则，返回 ``(是否监听, 冷却秒数)``。

    群 ID 优先于群名（群名会改，ID 不会）；两项都能作为覆盖键。
    """
    entry = {}
    for key in (str(chat_id or '').strip(), str(group_name or '').strip()):
        if key and key in cfg['groups']:
            entry = cfg['groups'][key]
            break
    enabled = (cfg['default_group_enabled'] if 'enabled' not in entry
               else entry['enabled'])
    cooldown = (cfg['cooldown_seconds'] if 'cooldown_seconds' not in entry
                else entry['cooldown_seconds'])
    return bool(enabled), int(cooldown)


def group_mode(cfg: dict, chat_id: str, group_name: str = '') -> str:
    """解析某个群实际用的模式：hard/soft；逐群覆盖优先于全局。"""
    entry = {}
    for key in (str(chat_id or '').strip(), str(group_name or '').strip()):
        if key and key in cfg['groups']:
            entry = cfg['groups'][key]
            break
    mode = str(entry.get('mode') or cfg.get('mode') or 'soft').strip().lower()
    return mode if mode in ('hard', 'soft') else 'soft'


def has_intent(text: str, words) -> bool:
    """消息像不像在提问：有问号，或含任一意图词；不看大小写/空格。"""
    raw = str(text or '')
    if '?' in raw or '？' in raw:
        return True
    body = _norm(raw)
    return any(_norm(w) and _norm(w) in body for w in (words or ()))


def render_soft_reply(template: str, reply: str, keyword: str = '',
                      sender: str = '') -> str:
    """渲染软模板；未知占位符在保存/加载时已被挡掉，这里只做替换。

    模板在配置里是一行一条，换行写成字面 ``\n``，这里还原成真实换行。
    """
    text = (str(template or '')
            .replace('{reply}', str(reply or ''))
            .replace('{keyword}', str(keyword or ''))
            .replace('{sender}', str(sender or '你')))
    return text.replace('\\n', '\n')


def match_keywords(text: str, keywords) -> list:
    """返回命中的关键词原文（按配置顺序），没命中返回空列表。"""
    body = _norm(text)
    if not body:
        return []
    out = []
    for kw in keywords or ():
        k = _norm(kw)
        if k and k in body:
            out.append(kw)
    return out


class Engine:
    """关键词广告的判定与记账；一个机器人进程持有一个实例。

    配置按 mtime 热加载；冷却与回显记录在内存 + 状态文件里。所有写文件
    动作都在锁内完成，消息回调多线程并发时不会互相踩。
    """

    def __init__(self, config_path: str = None, state_path: str = None,
                 hits_path: str = None, log_path: str = None, rng=None):
        self.config_path = config_path or CONFIG_FILE
        self.state_path = state_path or STATE_FILE
        self.hits_path = hits_path or HITS_FILE
        self.log_path = log_path or LOG_FILE
        self._rng = rng or random.Random()
        self._lock = threading.RLock()
        self._mtime = None
        self._cfg = None
        self._last_sent = {}      # chat_id -> 最近一次真正尝试发送的时间
        self._echoes = {}         # chat_id -> deque[(归一化广告原文, 时间)]
        self._skip_logged = {}    # chat_id -> 上次记录「冷却跳过」的时间
        self._load_state()

    # -- 配置 ---------------------------------------------------------

    def config(self) -> dict:
        """当前生效配置；文件 mtime 变了就重新读。"""
        try:
            mt = os.path.getmtime(self.config_path)
        except OSError:
            mt = None
        with self._lock:
            if self._cfg is None or mt != self._mtime:
                self._cfg = load_config(self.config_path)
                self._mtime = mt
            return self._cfg

    # -- 冷却与回显 ---------------------------------------------------

    def _load_state(self) -> None:
        """恢复最近 7 天的发送时间，重启后不会立刻对同一个群重发。"""
        try:
            with open(self.state_path, encoding='utf-8') as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        last = raw.get('last_sent') if isinstance(raw, dict) else None
        if not isinstance(last, dict):
            return
        cutoff = time.time() - 7 * 86400
        self._last_sent = {
            str(k): float(v) for k, v in last.items()
            if isinstance(v, (int, float)) and float(v) >= cutoff
        }

    def _save_state_locked(self) -> None:
        """原子写状态文件；失败只告警，不影响发送。"""
        tmp = self.state_path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'last_sent': self._last_sent}, f,
                          ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except OSError as e:
            print(f'[关键词广告] 写 {self.state_path} 失败：{e}', flush=True)

    def evaluate(self, text: str, chat_id: str, group_name: str = '',
                 sender_name: str = '', now: float = None) -> dict:
        """判断一条群消息要不要回广告。

        返回 dict，关键字段：``hit``（是否命中关键词）、``allowed``（是否
        可以发）、``reason``（disabled/not_group/group_disabled/no_match/
        reply_empty/soft_no_intent/cooldown/probability_skip/ok）、
        ``mode``、``keywords``、``reply``、``pre_delay``、``remaining``。
        ``hit=False`` 表示这条消息根本不该触发，调用方不必记日志。
        """
        now = time.time() if now is None else now
        cfg = self.config()
        if not cfg['enabled']:
            return {'hit': False, 'reason': 'disabled'}
        if not str(chat_id or '').endswith('@chatroom'):
            return {'hit': False, 'reason': 'not_group'}
        enabled, cooldown = group_rule(cfg, chat_id, group_name)
        if not enabled:
            return {'hit': False, 'reason': 'group_disabled'}
        matched = match_keywords(text, cfg['keywords'])
        if not matched:
            return {'hit': False, 'reason': 'no_match'}
        mode = group_mode(cfg, chat_id, group_name)
        reply = (cfg['reply'] or '').strip()
        if not reply:
            return {'hit': True, 'allowed': False, 'reason': 'reply_empty',
                    'mode': mode, 'keywords': matched}
        if mode == 'soft':
            if cfg['soft_require_intent'] and not has_intent(
                    text, cfg['soft_intent_words']):
                return {'hit': True, 'allowed': False, 'reason': 'soft_no_intent',
                        'mode': 'soft', 'keywords': matched, 'reply': ''}
            template = self._rng.choice(cfg['soft_replies']
                                        or DEFAULT_SOFT_REPLIES)
            reply = render_soft_reply(
                template, reply, matched[0] if matched else '', sender_name)
        with self._lock:
            last = self._last_sent.get(str(chat_id), 0.0)
        jitter = min(int(cfg['cooldown_jitter_seconds']), max(0, int(cooldown)))
        effective = float(cooldown)
        if cooldown > 0 and jitter > 0:
            effective = max(0.0, cooldown + self._rng.uniform(-jitter, jitter))
        if cooldown > 0 and now - last < effective:
            return {'hit': True, 'allowed': False, 'reason': 'cooldown',
                    'mode': mode, 'keywords': matched, 'reply': reply,
                    'cooldown': effective,
                    'remaining': max(1, int(effective - (now - last)) + 1)}
        if self._rng.random() > float(cfg['reply_probability']):
            return {'hit': True, 'allowed': False, 'reason': 'probability_skip',
                    'mode': mode, 'keywords': matched, 'reply': reply,
                    'cooldown': effective}
        lo = float(cfg['pre_delay_min_seconds'])
        hi = float(cfg['pre_delay_max_seconds'])
        pre_delay = self._rng.uniform(lo, hi) if hi > 0 else 0.0
        return {'hit': True, 'allowed': True, 'reason': 'ok',
                'mode': mode, 'keywords': matched, 'reply': reply,
                'pre_delay': pre_delay, 'cooldown': effective}

    def note_sent(self, chat_id: str, reply: str, now: float = None) -> None:
        """在真正发送前占住冷却，并记下原文用于回显防护。

        先占再发：发送失败也不要在下一条消息上继续重试，否则一个卡住的
        窗口会被每条命中消息连续撞很多次。
        """
        now = time.time() if now is None else now
        chat_id = str(chat_id)
        with self._lock:
            self._last_sent[chat_id] = now
            q = self._echoes.setdefault(chat_id, deque(maxlen=8))
            q.append((_norm(reply), now))
            cutoff = now - ECHO_WINDOW
            for key in list(self._echoes):
                self._echoes[key] = deque(
                    ((t, ts) for t, ts in self._echoes[key] if ts >= cutoff),
                    maxlen=8)
                if not self._echoes[key]:
                    self._echoes.pop(key, None)
            self._save_state_locked()

    def is_echo(self, chat_id: str, text: str, now: float = None) -> bool:
        """这条消息是不是自己刚发出去的广告原文（发送者解析失败时的兜底）。"""
        now = time.time() if now is None else now
        body = _norm(text)
        if not body:
            return False
        with self._lock:
            for t, ts in self._echoes.get(str(chat_id), ()):
                if t == body and now - ts < ECHO_WINDOW:
                    return True
        return False

    def skip_log_allowed(self, chat_id: str, now: float = None) -> bool:
        """同一群的「冷却跳过」最多每分钟记一次，避免命中密集时刷满日志。"""
        now = time.time() if now is None else now
        chat_id = str(chat_id)
        with self._lock:
            if now - self._skip_logged.get(chat_id, 0.0) < SKIP_LOG_INTERVAL:
                return False
            self._skip_logged[chat_id] = now
            return True

    # -- 命中记录 -----------------------------------------------------

    def log_hit(self, record: dict, now: float = None) -> None:
        """写一条命中记录：JSONL 给后台读，文本日志给人看。

        任何 IO 失败都只告警——广告发不出去已经够糟了，不能再把消息回调
        带崩。
        """
        now = time.time() if now is None else now
        rec = dict(record)
        rec.setdefault('ts', round(now, 3))
        rec['time'] = time.strftime('%Y-%m-%d %H:%M:%S',
                                    time.localtime(now))
        rec.setdefault('group_id', '')
        rec.setdefault('group_name', '')
        rec.setdefault('sender', '')
        rec.setdefault('mode', '')
        rec.setdefault('at_name', '')
        rec.setdefault('typing_ms', 0)
        rec.setdefault('keywords', '')
        rec.setdefault('text', '')
        rec.setdefault('reply', '')
        rec.setdefault('result', 'sent')
        rec.setdefault('error', '')
        line = json.dumps(rec, ensure_ascii=False) + '\n'
        status = {'sent': '已发送', 'failed': '发送失败',
                  'cooldown': '冷却跳过', 'reply_empty': '未配置文案',
                  'soft_skip': '非提问跳过',
                  'probability_skip': '概率跳过',
                  'at_failed': '@失败',
                  'at_ambiguous': '@对象有歧义',
                  'at_unresolvable': '@对象找不到',
                  'at_token_mismatch': '@对象不符',
                  'at_body_mismatch': '正文未落框',
                  'at_unverified': '@未验证',
                  }.get(rec['result'], rec['result'])
        human = (f'[{time.strftime("%m-%d %H:%M:%S", time.localtime(now))}] '
                 f'命中{status}｜群「{rec["group_name"]}」({rec["group_id"]})'
                 f'｜关键词「{rec["keywords"]}」')
        if rec['reply']:
            human += f'｜发出：{rec["reply"][:200]}'
        if rec['error']:
            human += f'｜错误：{rec["error"]}'
        human += '\n'
        with self._lock:
            self._append_rotated(self.hits_path, line, HITS_MAX_BYTES)
            self._append_rotated(self.log_path, human, LOG_MAX_BYTES)

    @staticmethod
    def _append_rotated(path: str, text: str, limit: int) -> None:
        """追加一行，超过 limit 先滚动成 .1 再写。调用方持锁。"""
        try:
            if os.path.exists(path) and os.path.getsize(path) > limit:
                os.replace(path, path + '.1')
            with open(path, 'a', encoding='utf-8') as f:
                f.write(text)
        except OSError as e:
            print(f'[关键词广告] 写 {path} 失败：{e}', flush=True)


def read_hits(limit: int = 100, path: str = None) -> list:
    """读最近 N 条命中记录（新→旧），只扫文件尾部，大文件也不卡。"""
    path = path or HITS_FILE
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    start = max(0, size - 512_000)
    try:
        with open(path, 'rb') as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    text = data.decode('utf-8', errors='replace')
    if start > 0:
        text = text.split('\n', 1)[-1]     # 丢掉截断的半行
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out[-max(1, int(limit)):][::-1]
