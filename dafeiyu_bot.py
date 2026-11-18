# -*- coding: utf-8 -*-
"""「大肥鱼」微信群 AI 机器人。

群里有人 @大肥鱼 时，把问题发给自建 LLM（OpenAI 兼容接口），再把回答发回群里。
基于 wechatauto 的全局消息监听（db.Listener，纯数据库轮询，收消息不需要动窗口），
只有「回话」那一刻才会驱动微信窗口。每个群有长期记忆：对话无上限累积，
估算上下文到 CONTEXT_TOKEN_LIMIT（默认 100K token）时自动把最老的历史
压成摘要（最近几轮永远保留原文）。被 @ 回答时还会附上该群最近
RECENT_N 条群消息当上下文，没聊过天也能接得上话。只认 @ 触发，
没被 @ 的消息只进围观记录，不回答。

内置 agent 工具循环：LLM 可调用 search_group_messages 搜索最近
SEARCH_DAYS 天的群聊消息（关键词/成员/群名，为空=全部，上限 2000 条）。

用法：
    python dafeiyu_bot.py            # 启动机器人，Ctrl+C 退出
    python dafeiyu_bot.py --test "在吗"   # 不连微信，只测 LLM 链路
    python dafeiyu_bot.py --search "关键词" [--member 名] [--group 群]

依赖：pip install -e . （wechatauto 本体），LLM 调用只用标准库。
日志：控制台照常输出，同时落盘到脚本同目录 dafeiyu_bot.log
（5MB 滚动，保留 2 份历史），每行带时间戳。
"""

import argparse
import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from wechatauto import WeChat

import broadcast
import keyword_ads

# ------------------------- 配置区 -------------------------

# LLM 接口（OpenAI 兼容）：请求 {base_url}/chat/completions，带 Bearer 鉴权。
# 通过环境变量配置，也可以直接改这里；管理页保存到 bot_config.json 后优先级最高。
#   WECHAT_DEEPSEEK_BASE_URL  完整接口地址，含 /v1，如 https://api.deepseek.com/v1
#                             多个用逗号分隔做故障转移（粘住第一个能用的）
#   WECHAT_DEEPSEEK_API_KEY   Bearer 令牌；本地部署可留空
#   WECHAT_DEEPSEEK_MODEL     模型名，如 deepseek-chat
API_BASE_URL = os.environ.get('WECHAT_DEEPSEEK_BASE_URL',
                              'http://127.0.0.1:8000/v1')
API_KEY = os.environ.get('WECHAT_DEEPSEEK_API_KEY', '')
MODEL = os.environ.get('WECHAT_DEEPSEEK_MODEL', 'deepseek-v4')

AT_NAME = '大肥鱼'          # 触发词：消息里包含 "@大肥鱼" 才回答（只认 @）
RECENT_N = 20               # 被 @ 回答时附带的「最近群消息」条数
RECENT_LINE_MAX = 200       # 群消息日志里单条消息截断长度，防长文刷爆上下文

# -- 群消息搜索工具（LLM 可调用，也可 --search 命令行直用）----------------
SEARCH_DAYS = 3             # 搜索窗口：最近几天
SEARCH_MAX_RESULTS = 2000   # 返回上限：最多多少条（超出保留最新的）
SEARCH_RESULT_MAX_CHARS = 90_000   # 塞给模型的结果长度上限（防撑爆上下文）
SEARCH_PAGE = 500           # 每个群分页拉取的步长
SEARCH_ROOM_LIMIT = 300     # 最多扫多少个群（按会话列表取前 N）
SEARCH_ROOM_PAGE_CAP = 50000   # 每个群最多回溯多少条
TOOL_ROUNDS = 4             # 单次回答允许的工具调用轮数

# -- 日志 ----------------------------------------------------------------
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'dafeiyu_bot.log')
LOG_MAX_BYTES = 5_000_000   # 单文件 5MB，超出滚动成 .1/.2
LOG_KEEP = 2                # 保留几个历史日志文件
PENDING_REPLIES_FILE = os.path.join(os.path.dirname(LOG_FILE),
                                    'pending_replies.json')

# -- 表情包/图片 ----------------------------------------------------------
# 往这个文件夹丢 GIF（微信会作为「动画表情」发出）或 jpg/png（图片消息），
# 文件名起清楚点，模型按名字挑。GIF 粘贴进微信即动画表情。
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'stickers')

CONTEXT_TOKEN_LIMIT = 100_000   # 群聊上下文估算 token 数到这个值就压缩历史
COMPRESS_KEEP_TURNS = 4         # 压缩时保留最近几轮原对话不压
SUMMARY_MAX_TOKENS = 1500       # 压缩摘要的长度上限
LLM_TIMEOUT = 90            # LLM 请求超时（秒），管理页可改
MAX_TOKENS = 600

# -- 窗口所在显示器 ------------------------------------------------------
# 微信主窗口固定放在哪块屏幕上，按显示器 DeviceString 的子串匹配
# （EnumDisplayDevices 的 DeviceString，如 "IddSampleDriver Device"）。
# 填 'IddSampleDriver' 就会把微信按在虚拟显示器上：机器人照常驱动它收发消息，
# 物理屏幕上不挡事。留空 = 不干预，窗口停在用户自己放的位置。
# 启动时校正一次，之后每次发送前复查，被拖走会拉回来。
WECHAT_DISPLAY = os.environ.get('WECHAT_DEEPSEEK_DISPLAY', 'IddSampleDriver')

# 虚拟屏总开关：关掉就完全不干预窗口位置——不校正、不搬回、看门狗不跑、
# 发送前也不等目标屏。没装 IddSampleDriver 的用户在管理页关掉这一项即可，
# 不必去清空上面的显示器名（老配置留空或填 '-' 仍然有效）。
WECHAT_DISPLAY_ENABLED = True


def _display_target() -> str:
    """当前要固定到的显示器名；返回空串表示不干预窗口位置。

    所有判断目标显示器的地方都必须走这里，别各自读 `WECHAT_DISPLAY`：
    漏一处就会出现「开关关掉了、看门狗还在搬窗口」。函数体读模块级全局，
    配置热加载覆盖后立刻生效。
    """
    if not WECHAT_DISPLAY_ENABLED:
        return ''
    kw = (WECHAT_DISPLAY or '').strip()
    return '' if kw.lower() in ('-', 'none', 'off') else kw


# 虚拟显示器识别：关闭虚拟屏开关后，启动时要把仍留在这些屏上的微信窗口拉回主屏。
# 只匹配已知虚拟显示适配器的描述（大小写不敏感）；NVIDIA/Intel/AMD 物理屏不受影响。
VIRTUAL_DISPLAY_MARKERS = (
    'iddsampledriver', 'orayidddriver', 'gameviewer',
    'usb mobile monitor', 'virtual display', 'virtual monitor',
    'spacedesk', 'parsec', 'sunshine',
)


def _is_virtual_display(device_string: str) -> bool:
    """设备描述是否属于虚拟显示器（IddSampleDriver / 向日葵 / GameViewer 等）。"""
    name = (device_string or '').lower()
    return any(marker in name for marker in VIRTUAL_DISPLAY_MARKERS)


def _needs_primary_recovery(window_rect, displays) -> bool:
    """启动时是否该把微信窗口拉回主屏。

    ``displays`` 是 ``[(设备描述, (左, 上, 右, 下)), ...]``。判据：

    - 窗口中心落在任一**物理屏**内，或与物理屏的交集达到窗口面积的 40%，
      说明用户本来就把它放在看得见的地方，不动（含第二块物理屏）；
    - 否则（整块在虚拟屏上、完全在虚拟桌面外）说明关掉虚拟屏开关后用户
      会看不见微信，需要拉回主屏。

    枚举不到显示器时返回 False：拿不到信息就不猜，避免把窗口搬到用户
    没预期的地方。
    """
    if not displays:
        return False
    left, top, right, bottom = (int(v) for v in window_rect)
    if right <= left or bottom <= top:
        return True
    physical = [rect for name, rect in displays
                if not _is_virtual_display(name)]
    if not physical:
        # 只有虚拟屏、没有物理屏（纯远程/无头机）：没有「物理主屏」可回。
        # 窗口还在任意一块屏内就尊重用户位置；完全在屏幕外才拉回主区域，
        # 避免每次启动都把用户摆好的窗口挪一遍。
        for _, (dl, dt, dr, db) in displays:
            if min(right, dr) > max(left, dl) \
                    and min(bottom, db) > max(top, dt):
                return False
        return True
    area = (right - left) * (bottom - top)
    cx, cy = (left + right) // 2, (top + bottom) // 2
    for dl, dt, dr, db in physical:
        if dl <= cx < dr and dt <= cy < db:
            return False
        ix = max(0, min(right, dr) - max(left, dl))
        iy = max(0, min(bottom, db) - max(top, dt))
        if ix * iy >= area * 0.4:
            return False
    return True


# 微信窗口尺寸。断屏/远程事件会把窗口挤成 160x28 这类碎片尺寸，
# 窗口校正逻辑绝不能沿用——那样会把它固化下来，guia 在过小的窗口里
# 找不到输入框，SendMsg 会一直卡住。低于 MIN_* 就按 DEFAULT_* 恢复。
DEFAULT_WIN_W, DEFAULT_WIN_H = 900, 650
MIN_WIN_W, MIN_WIN_H = 600, 400

# 显示看门狗节奏。屏在时只读枚举一遍（毫秒级），按 WATCH_INTERVAL 复查即可；
# 屏掉了之后的**修复动作很重**——SetDisplayConfig 会重建整个显示拓扑、让所有
# 窗口重排，Enable-PnpDevice 还会让 PnP 重扫设备，都会把桌面顶住。所以修复
# 必须退避：失败一次间隔翻倍，上限 WATCH_MAX_INTERVAL；连续失败
# WATCH_GIVEUP_AFTER 次就完全停手，只保留只读检测（屏自己回来立刻归位）。
WATCH_INTERVAL = 10.0          # 正常复查间隔（秒）
WATCH_MAX_INTERVAL = 300.0     # 修复失败后的最长间隔（秒）
# rc=5（会话不可写）的退避。比 300 短得多：这种失败只是权限不够、不会重建拓扑，
# 成本极低；而会话随时可能恢复（用户重新连远程），退避太长会让回复白等一轮。
WATCH_DENIED_INTERVAL = 60.0
WATCH_GIVEUP_AFTER = 8         # 连续失败这么多次后停止修复，只做只读等待
WATCH_DEVICE_CACHE_TTL = 300.0  # PnP 设备状态探测的缓存时间（秒）
                               # 查询要新起一个 PowerShell 进程，很贵；
                               # 状态为 OK 时短期内重复查没有信息量

# -- ChatGPT 桥（经 opencli，走你登录态的 Chrome）--------------------------
# 只开放 chatgpt ask / image 两个口子：固定纯文本，不暴露切模型/指定会话/
# 传文件/深度研究等参数；冷却限流防风控。
CHATGPT_COOLDOWN = 60       # 两次调用最小间隔（秒）
CHATGPT_RETRIES = 3         # 生成失败最多重试次数（不含首次尝试）
SEND_RETRIES = 3            # 群发送失败后的重试次数（不含首次，总尝试 1+3）
SEND_RETRY_WAIT = 4         # 两次发送尝试之间等待秒数（给窗口/剪贴板喘口气）
# 待发答案最多补发几轮（每轮内部仍按 1+SEND_RETRIES 次尝试）。
# 只针对临时失败（SKIP_SEND_REASONS）：没有这个上限时，_retry_pending_replies
# 会每 10 秒把同一条答案重新交给 _send_retry，窗口一直不恢复就永远循环。
# 3 轮约 30~45 秒，足够跨过一次锁屏/远程重连；仍不恢复就放弃并记录原因。
PENDING_MAX_ROUNDS = 3
# 发送前探活：UIA 的跨进程调用**没有超时保护**，微信忙于解码图片/重绘时
# 一次控件查询会一直不返回。实测 2026-11-14 11:10 一次 SendMsg 卡了 396 秒
# （正常 12~14s），期间 send_lock 被占死、后面所有回复一起堵。
# 这里在独立线程里新建 Control 对象探一下，超过这么久没回应就跳过本轮、
# 答案留在待发文件；探针线程让它自己卡着，不拖主线程。
UIA_PROBE_TIMEOUT = 2.0
# 单次 send() 耗时超过这么多秒就记一行日志（事后告警）：
# 没法中断已经发出去的 UIA 调用，至少让日志能看出这次卡了多久。
SEND_ATTEMPT_TIMEOUT = 40.0
BROADCAST_OPEN_TIMEOUT = 25   # 群发单目标「打开会话」的上限（秒）。正常 4~7 秒，
                              # 打不开的目标原本会拖 1~2 分钟（OCR 多轮+滚动重试）
# 发送卡死上限（秒）：send_lock 被占超过这么久就认为 UIA 卡死，整进程退出等重启。
# 为什么只能整进程退出：卡住的是一次 native/COM 调用，进程内无法中断（跨线程
# join 会死锁，2026-11-14 实测）；而答案在发送前已经 fsync 到 pending_replies.json，
# 重启后 _flush_pending_replies 会先核出站记录再补发，不会重复。
# 正常最长 18.5s（图文）、25s 是「打开会话」它的上限且在锁外，所以 120 是 6 倍余量。
SEND_HANG_SECONDS = float(os.environ.get(
    "WECHAT_DEEPSEEK_SEND_HANG_SECONDS", "120"))
# 看门狗专用退出码：一键启动.bat 用它区分「用户主动停」和「卡死被重启」。
WATCHDOG_EXIT_CODE = 75
# 仅用于验证看门狗：让 send() 人为睡这么久，模拟 UIA 卡死。默认 0 = 不启用。
TEST_HANG_SEND = float(os.environ.get("WECHAT_DEEPSEEK_TEST_HANG_SEND", "0"))
# 这两类失败是**临时状态**（Windows 会话断开、目标屏没回来），不是界面坏了。
# 命中时保留待发答案、不暂停机器人；桌面恢复后自动补发，重启也不丢。
# 文案必须与 _send_retry 里提前 return 的字符串对得上。
SKIP_SEND_REASONS = ('会话不可交互', '未恢复', '桌面不可读', 'UIA 无响应', '窗口不可见')
CHATGPT_RETRY_WAIT = 5      # 两次尝试之间的间隔（秒）
CHATGPT_TIMEOUT = 240       # ask / image 等回答的超时（秒），4 分钟
CHATGPT_IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'chatgpt_images')   # ChatGPT 生图下载目录

SYSTEM_PROMPT = """你是「大肥鱼」，一只 DeepSeek V4 二创的蓝色鲸鱼娘（前身「防火女」，现在活泼爱偷吃，偶尔穿女仆装），在微信群里当助手。

## 说话方式（最重要）
- **简短压倒一切**：这是群聊，没人想读长篇。默认一到两句话说完；只有被明确要求写代码/教程/总结时才可以长，长回答也只保留干货，删掉铺垫、重复和客套。发出前先自检：这句话删掉意思变吗？不变就删。
- 默认说人话：像靠谱的同事一样，清楚、自然、直接地把问题答好，不演不装。
- 只有纯闲聊、斗嘴、被开玩笑时才用一点人设腔（俏皮、傲娇、爱吃小鱼干），点到为止，最多一两句。
- 回答技术问题、帮忙办事、总结信息时：零梗零卖萌，直接给内容、给结论。
- 正常用「我」自称，别每句都"本鱼"；"翻肚皮""游走了"这类梗闲聊里偶尔一个就够，不许堆。
- 被叫「大肥鱼」可以嘴硬一句「才不是！」，然后照常好好回答。

## 输出格式（微信纯文本，必须遵守）
- 这里是微信聊天输入框，纯文本环境：禁止一切 Markdown。不要 **加粗**、不要 # 标题、不要以 - 或 * 开头的列表、不要代码块围栏。
- 需要分点就用「1．2．3．」编号或自然段分行；代码/命令直接贴成纯文本行，别加围栏。
- 不要输出半角星号 *、井号 #、反引号 ` 这些符号，它们在微信里显示出来就是乱码。

## 硬性规则
- 先办对事，再卖萌：任务正确性 > 人设。代码、命令、结论必须准确。
- 不真骂人：吐槽点到为止，绝不人身攻击。
- 短话优先：闲聊别啰嗦；长技术回答里，人设只出现在开头/结尾的包裹层，不污染代码块/命令/数据。
- 中文为主：默认中文；用户用其他语言时跟随，但保留贱萌语气。
- 需要严肃回答时，用户说"别演了/正经点"就退出人设。

你在微信群里，别人@你就是在跟你说话。回答就是直接发到群里的微信消息，不要用 markdown 标题、不要列清单符号堆砌，口语化一点。

## 工具
- 消息开头的「【昵称】」标记表示这句话是谁发的。
- 你可以调用 search_group_messages 工具：搜索【当前这个群】最近 3 天的消息。参数 keyword（按消息内容）、member（按发送者昵称/备注/wxid）都可选，都不填=返回本群全部（最多 2000 条）。只能搜本群，别的群搜不到。
- 被问「群里之前聊了什么 / 谁提过 X / 某人发过什么」这类问题时，先调工具搜证据再回答，别凭印象编；引用时带上时间。没搜到就直说没搜到。
- 总结类问题（比如「总结一下群消息」）只总结本群的，绝不要把别的群的内容扯进来。
- get_ai_news 工具能查 AI 圈实时资讯：action=hot 当前热点榜 Top10 / latest 最近 24 小时精选动态（可带 keyword、category 过滤） / daily 今日日报。被问「最近 AI 有什么新闻/热点/大事件」时先调它拿真数据再回答，别凭印象编。
- **不用自己写@**：系统回复时会自动 @ 提问的人。只有当你要点名「另一位」群友（不是提问者本人）时，才在回答开头写「@那个人的名字」；除此之外开头别加@。
- 你有 send_sticker 工具可以发表情包/图片（可用列表见工具描述，很多文件名本身就写了使用场景，照着挑）。**主动发、多发**：不用等别人要求——闲聊、被逗、吐槽、表达情绪、回答开头或结尾调节气氛、看到离谱/好笑的消息、干活报进度，场景合适就自己来一张；只有纯技术答疑、连续正经办事时才克制。**多换着花样**：不同表情都可以发，换着来，别总发同一张。发完工具会告诉你结果，之后正常给文字回答；只发表情不说话也行（结尾给空回答）。
- ask_chatgpt 工具可以把问题转给 ChatGPT 拿回答（调用有间隔限制，自己能答的别浪费）。拿到回答后**原样复述给群里**，不要改写成自己的口吻、不要加人设包裹；最多在开头补一句「ChatGPT 的回答：」。
- 遇到稍有难度的活——查实时资料/新闻/行情、写个 HTML/代码、做复杂分析比较——都可以转给 ask_chatgpt 让 ChatGPT 干：要联网信息的带 web_search=true。别硬撑着自己编。
- 群友要「网页/动画/图表/文档/小工具」这类成品（比如画个鹈鹕骑自行车的 HTML 动画）时，这是让 ChatGPT 生成**文件**，不是生图：转给 ChatGPT 的 prompt 里必须明确要求它直接生成文件（例：「请用代码生成一个完整的 xxx.html 文件并保存」），不要只让它在回答里贴代码——生成的文件会被自动下载并转发到群里，群里直接拿到能用的成品。只有用户明确想要一张图片（照片、插画、表情图）才用 chatgpt_image。
- 转活给 ChatGPT（ask_chatgpt / chatgpt_image）时，固定开场两连：**先用 say_to_group 发一句「正在调用ChatGPT」，紧接着用 send_sticker 发「骑ChatGpt」这张表情**，然后再调工具；干完、文件已转发后，正式回答里说「我已经让 ChatGPT 帮我干了」并简短交代结果。
- 有人问这个项目怎么部署、怎么自己养一条这样的鱼，或者想要/分享这个项目时，把仓库地址给他：https://github.com/chenjiaqiangmax/wechat-bot （README 里有安装部署步骤）。
- say_to_group 工具能在运行过程中随时往群里发一条消息（进度同步、长任务的中间反馈），闲聊能一句话答的别用；发的也是微信纯文本，禁 Markdown。"""

FAIL_REPLY = '（晕鱼了…本鱼暂时联系不上大脑，游走喘口气，稍后再@本鱼一次）'

# -- 管理页覆盖配置 ------------------------------------------------------
# 管理后台（python -m admin）把配置写进同目录 bot_config.json，启动时覆盖
# 上面的同名常量；文件里没有的键保持原值。含内网地址/密钥，已 gitignore。
BOT_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'bot_config.json')
_OVERRIDABLE = {
    'API_BASE_URL': str, 'API_KEY': str, 'MODEL': str,
    'AT_NAME': str, 'RECENT_N': int, 'MAX_TOKENS': int, 'LLM_TIMEOUT': int,
    'TOOL_ROUNDS': int, 'SEARCH_DAYS': int, 'CONTEXT_TOKEN_LIMIT': int,
    'CHATGPT_COOLDOWN': int, 'CHATGPT_TIMEOUT': int, 'SEND_RETRIES': int,
    'SYSTEM_PROMPT': str, 'FAIL_REPLY': str, 'WECHAT_DISPLAY': str,
    'WECHAT_DISPLAY_ENABLED': bool,
}
# 其中这几项改完即可生效（管理页保存后无需重启）：每次请求前检查文件时间戳。
LIVE_KEYS = ('API_BASE_URL', 'API_KEY', 'MODEL', 'LLM_TIMEOUT')
_config_mtime = None


def _coerce(typ, val):
    """把 JSON 里读到的值转成目标类型；字符串去首尾空白。

    bool 单独解析：`bool('false')` 是 True，手写配置或管理页传字符串时
    会把开关又打开，必须按字面意思认。
    """
    if typ is bool:
        if isinstance(val, str):
            return val.strip().lower() in ('1', 'true', 'yes', 'on', '是', '开')
        return bool(val)
    return str(val).strip() if typ is str else typ(val)


def _apply_config_overrides(keys=None) -> None:
    """把 bot_config.json 里的配置覆盖到模块常量上。

    keys 为 None 时应用全部；否则只应用给定键（热生效用）。
    """
    global _config_mtime
    try:
        _config_mtime = os.path.getmtime(BOT_CONFIG_FILE)
        with open(BOT_CONFIG_FILE, encoding='utf-8') as f:
            cfg = json.load(f)
    except FileNotFoundError:
        _config_mtime = None
        return
    except Exception as e:
        print(f'[配置] {BOT_CONFIG_FILE} 读取失败，忽略：{e}', flush=True)
        return
    for key, typ in _OVERRIDABLE.items():
        if keys and key not in keys:
            continue
        val = cfg.get(key)
        if val in (None, '', []):
            # 空值一律按「不修改」处理（API_KEY 靠这条实现留空即保持）；
            # WECHAT_DISPLAY 是例外：空串本身就是有效值，表示「不固定窗口」，
            # 必须能覆盖掉默认的 IddSampleDriver
            if not (key == 'WECHAT_DISPLAY' and val == ''):
                continue
        try:
            globals()[key] = _coerce(typ, val)
        except (TypeError, ValueError):
            print(f'[配置] {key}={val!r} 类型不对，忽略', flush=True)


def _refresh_live_config() -> None:
    """配置文件变了就重新应用 LLM 相关项，让管理页保存后下一次请求即生效。"""
    try:
        mt = os.path.getmtime(BOT_CONFIG_FILE)
    except OSError:
        return
    if mt == _config_mtime:
        return
    _apply_config_overrides(LIVE_KEYS)
    print(f'[配置] 已热加载 LLM 配置：{API_BASE_URL} · {MODEL}', flush=True)


_apply_config_overrides()

# -- 多群 Agent ------------------------------------------------------------
# bot_config.json 里的 AGENTS 列表：每个 Agent 绑定若干群（群 ID 或群名，
# "*" = 兜底所有没被别的 Agent 认领的群），各自有触发名、人设、模型参数和
# 可用工具；记忆按 (Agent, 群) 隔离。没配 AGENTS 时等价于一个绑定 "*" 的
# 默认 Agent，行为和以前完全一样。AGENTS 改动热加载，不用重启。
TOOL_NAMES = ('search_group_messages', 'get_ai_news', 'send_sticker',
              'say_to_group', 'ask_chatgpt', 'chatgpt_image')


def _norm_agent(d: dict, idx: int = 0) -> dict:
    """补全 Agent 默认值；空字段回落到全局常量。"""
    tools = d.get('tools')
    return {
        'id': str(d.get('id') or f'agent{idx}'),
        'name': str(d.get('name') or AT_NAME),
        'enabled': d.get('enabled', True) is not False,
        'at_name': str(d.get('at_name') or '').strip() or AT_NAME,
        'groups': [str(g).strip() for g in (d.get('groups') or []) if str(g).strip()],
        'system_prompt': str(d.get('system_prompt') or '').strip() or SYSTEM_PROMPT,
        'model': str(d.get('model') or '').strip() or None,
        'temperature': float(d.get('temperature') if d.get('temperature') not in (None, '') else 0.7),
        'max_tokens': int(d.get('max_tokens') or MAX_TOKENS),
        'recent_n': int(d.get('recent_n') or RECENT_N),
        'tools': list(TOOL_NAMES) if tools is None else [t for t in tools if t in TOOL_NAMES],
    }


def _load_agents() -> list:
    try:
        with open(BOT_CONFIG_FILE, encoding='utf-8') as f:
            raw = json.load(f).get('AGENTS') or []
    except FileNotFoundError:
        raw = []
    except Exception as e:
        print(f'[配置] AGENTS 读取失败，用默认 Agent：{e}', flush=True)
        raw = []
    agents = [_norm_agent(a, i) for i, a in enumerate(raw) if isinstance(a, dict)]
    return agents or [_norm_agent({'id': 'default', 'groups': ['*']})]


# -- 用量记录 --------------------------------------------------------------
# 每次 LLM 调用追加一行 JSON 到 usage.jsonl，管理页据此画 token 用量和缓存
# 命中率。服务端返回 prompt_tokens_details.cached_tokens（vLLM 需开
# --enable-prompt-tokens-details）就用真值；否则按「本次请求与同节点最近
# 请求的最长公共前缀」估算前缀缓存复用量（cached_src='est'）。
USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'usage.jsonl')
USAGE_MAX_BYTES = 20_000_000
KNOWN_CHATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'known_chats.json')
_usage_lock = threading.Lock()
_ctx = threading.local()    # 当前回调线程在服务哪个群/Agent，用量记录用


def _record_usage(rec: dict) -> None:
    rec.setdefault('chat_id', getattr(_ctx, 'chat_id', ''))
    rec.setdefault('chat_name', getattr(_ctx, 'chat_name', ''))
    rec.setdefault('agent', getattr(_ctx, 'agent', ''))
    line = json.dumps(rec, ensure_ascii=False) + '\n'
    with _usage_lock:
        try:
            if os.path.exists(USAGE_FILE) and os.path.getsize(USAGE_FILE) > USAGE_MAX_BYTES:
                os.replace(USAGE_FILE, USAGE_FILE + '.1')
            with open(USAGE_FILE, 'a', encoding='utf-8') as f:
                f.write(line)
        except OSError:
            pass


def _common_prefix_len(a: str, b: str) -> int:
    """二分 + 切片比较（C 速度），10 万字也只比十几次。"""
    lo, hi = 0, min(len(a), len(b))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if a[:mid] == b[:mid]:
            lo = mid
        else:
            hi = mid - 1
    return lo

# ChatGPT 工具：只暴露 ask（转问题拿回答），范围收敛在提示词+限流双层
CHATGPT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'ask_chatgpt',
        'description': (
            '把问题转交给 ChatGPT 并拿回它的回答（经你的 ChatGPT 网页'
            '会话，最多等 240 秒）。用法：普通提问直接问；需要实时信息'
            '（今日新闻、最新行情、软件最新版本、比赛比分等联网内容）时'
            '把 web_search 设为 true；要「成品」（HTML 网页/动画、图表、'
            'CSV、PDF、文档等）时在 prompt 里明确要求 ChatGPT 直接生成'
            '文件，它生成的文件会自动下载并转发到群里。prompt 要写成'
            '完整、自包含的问题。'),
        'parameters': {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string',
                           'description': '转给 ChatGPT 的完整问题'},
                'web_search': {'type': 'boolean',
                               'description': '需要联网查实时信息时设为 true'},
            },
            'required': ['prompt'],
        },
    },
}

# ChatGPT 生图工具：生成图片自动下载到本地并转发到群里
CHATGPT_IMAGE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'chatgpt_image',
        'description': (
            '让 ChatGPT 生成图片（画图/作图），图片会自动下载并转发到'
            '本群。只在用户明确要求画图/生成图片时使用；生成可能要几分钟。'
            'prompt 要详细描述想要的画面。'),
        'parameters': {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string',
                           'description': '给 ChatGPT 的画图描述'},
            },
            'required': ['prompt'],
        },
    },
}

# 群内发消息工具：运行过程中随时同步进度（转 ChatGPT 前先说一声等）
SAY_TOOL = {
    'type': 'function',
    'function': {
        'name': 'say_to_group',
        'description': (
            '在干活过程中随时往群里发一条消息，不影响你最后的正式回答。'
            '典型用法：把活转给 ChatGPT 之前先说一句（如「我让 ChatGPT '
            '帮忙干了，稍等」）；干完、文件已转发后，正式回答里再说结果。'
            '闲聊能一句话答的场景不要调用。发的也是微信纯文本，禁 Markdown。'),
        'parameters': {
            'type': 'object',
            'properties': {
                'text': {'type': 'string',
                         'description': '要发到群里的消息内容'},
            },
            'required': ['text'],
        },
    },
}

# 暴露给 LLM 的工具：群消息搜索（实现在 DaFeiYuBot.search_group_messages）
# 只开放「当前会话」范围：跨群搜索会让 A 群的回复混进 B 群的内容（实测事故），
# 模侧不给跨群入口；命令行 --search 仍可全量搜。
SEARCH_TOOL = {
    'type': 'function',
    'function': {
        'name': 'search_group_messages',
        'description': (
            '搜索当前会话（本群）最近3天的消息。keyword=按消息内容搜，'
            'member=按发送者昵称/备注/wxid搜；都不填=返回本群全部消息。'
            '最多返回2000条（超出保留最新的）。只能搜本群，搜不了别的群。'
            '只在需要查本群历史时调用。'),
        'parameters': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string', 'description': '消息内容关键词'},
                'member': {'type': 'string',
                           'description': '发送者昵称/备注/wxid'},
            },
        },
    },
}

# AI 资讯工具：aihot.news 免费匿名 API（匿名 GET，约 60 次/分限流）
AIHOT_BASE = 'https://aihot.news/api/v1'
AIHOT_CATEGORIES = ('ai-models', 'ai-products', 'industry',
                    'paper', 'tip', 'opinion')
AIHOT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'get_ai_news',
        'description': (
            '查询 AI 圈实时资讯（来源 aihot.news 聚合站）。action 三选一：'
            'hot=当前热点榜 Top10；latest=最近 24 小时精选动态（可带 '
            'keyword 关键词、category 分类过滤）；daily=今日 AI 日报。'
            '被问「最近 AI 有什么新闻/热点/大事件」时先调它拿真数据，'
            '别凭印象编。'),
        'parameters': {
            'type': 'object',
            'properties': {
                'action': {'type': 'string',
                           'enum': ['hot', 'latest', 'daily'],
                           'description': 'hot=热榜 latest=最新动态 '
                                          'daily=日报'},
                'keyword': {'type': 'string',
                            'description': 'latest 模式下的关键词过滤'},
                'category': {'type': 'string',
                             'enum': list(AIHOT_CATEGORIES),
                             'description': 'latest 模式下的分类过滤'},
            },
            'required': ['action'],
        },
    },
}

# -----------------------------------------------------------


class _Tee:
    """行缓冲的 stdout/stderr 镜像：终端照常显示，同时落盘到 LOG_FILE。

    没带时间戳的行（bot 自己的 print）补上时间前缀；wechatauto 库日志
    本来就带 `YYYY-MM-DD` 前缀，原样保留。文件超 LOG_MAX_BYTES 滚动。
    """

    _TS_RE = re.compile(r'^\d{4}-\d{2}-\d{2} ')

    def __init__(self, stream):
        self._stream = stream
        self._buf = ''

    def write(self, s):
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            self._emit(line)
        return len(s)

    def _emit(self, line):
        if not self._TS_RE.match(line):
            line = time.strftime('[%m-%d %H:%M:%S] ') + line
        text = line + '\n'
        try:
            self._stream.write(text)
            self._stream.flush()
        except Exception:
            pass
        try:
            self._rotate_if_needed()
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(text)
            self._log_write_failed = False
        except Exception as exc:
            # 写文件失败绝不能静默：2026-11-14 13:24 起日志整整停了，排查时
            # 被当成「机器人不再收消息」，白绕了一大圈。只在第一次失败时
            # 往终端说一句（终端本身通常还能写）。
            if not getattr(self, '_log_write_failed', False):
                self._log_write_failed = True
                try:
                    self._stream.write(
                        f'[日志] 写入 {LOG_FILE} 失败：'
                        f'{type(exc).__name__}: {exc}\n')
                except Exception:
                    pass

    @staticmethod
    def _rotate_if_needed():
        try:
            if os.path.getsize(LOG_FILE) < LOG_MAX_BYTES:
                return
            for i in range(LOG_KEEP - 1, 0, -1):
                src = f'{LOG_FILE}.{i}'
                if os.path.exists(src):
                    os.replace(src, f'{LOG_FILE}.{i + 1}')
            os.replace(LOG_FILE, LOG_FILE + '.1')
        except Exception:
            pass

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        return False


def _setup_logging() -> None:
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)


def _looks_garbled(text: str) -> bool:
    """粗筛乱码：正常中文/代码/标点以外的怪符号占比过高就换节点重试。"""
    if len(text) < 8:
        return False
    ok = 0
    for ch in text:
        if (ch.isalnum() or ch.isspace()
                or '\u4e00' <= ch <= '\u9fff'      # 汉字
                or '\u3000' <= ch <= '\u303f'      # CJK 标点
                or '\uff00' <= ch <= '\uffef'      # 全角标点
                or ch in '.,!?;:\'"()[]{}<>/*-+=#@&%$_|~`^\\\n'
                or ord(ch) > 0x1F000):             # emoji 等
            ok += 1
    return ok / len(text) < 0.88


def est_tokens(text: str) -> int:
    """粗估 token 数（宁大勿小，早压缩总比撑爆上下文强）。

    中文按 1 字 ≈ 1 token，其余按 3 字符 ≈ 1 token——都比 DeepSeek 实际
    切词偏保守，100K 的阈值在真实 131K 窗口里有充足余量。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    return cjk + (len(text) - cjk) // 3 + 1


def _split_base_urls(raw) -> list:
    """把「逗号/空白分隔的 base_url」拆成列表，去掉尾部斜杠与空项。

    每个 base_url 都要写到 /v1 这一层（如 https://api.deepseek.com/v1），
    请求时直接拼 /chat/completions。
    """
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r'[,\s]+', str(raw or ''))
    return [p.strip().rstrip('/') for p in parts if p.strip().rstrip('/')]


def _classify_http_error(e) -> str:
    """把 LLM 请求异常翻译成能一眼看出原因的一句话（不含任何密钥）。"""
    if isinstance(e, urllib.error.HTTPError):
        hint = {401: '鉴权失败，检查 api_key',
                403: '拒绝访问，检查 api_key 权限',
                404: '接口不存在，检查 base_url 是否带对了 /v1 与路径',
                429: '被限流，稍后再试'}.get(e.code, '')
        return f'HTTP {e.code} {e.reason}' + (f'（{hint}）' if hint else '')
    if isinstance(e, urllib.error.URLError):
        reason = getattr(e, 'reason', e)
        if isinstance(reason, (socket.timeout, TimeoutError)) \
                or 'timed out' in str(reason).lower():
            return f'请求超时（{LLM_TIMEOUT}s）'
        return f'连接失败：{reason}'
    if isinstance(e, (socket.timeout, TimeoutError)):
        return f'请求超时（{LLM_TIMEOUT}s）'
    if isinstance(e, json.JSONDecodeError):
        return f'返回体不是合法 JSON：{e.msg}'
    return f'{type(e).__name__}: {e}'


class LLMClient:
    """OpenAI 兼容 Chat Completions 客户端（标准库实现，零依赖）。

    多个 base_url 时按顺序粘性切换：成功后粘住能用的那个（保住服务端
    prefix cache），报错/乱码才轮到下一个，全部失败才抛给上层回兜底话术。
    """

    def __init__(self):
        self._start = 0
        self._lock = threading.Lock()
        # 每个接口最近请求的序列化文本，估算前缀缓存命中用（近似 KV 缓存里还在的前缀）
        self._history = {}

    def chat_raw(self, messages: list, tools: list = None,
                 temperature: float = 0.7,
                 max_tokens: int = MAX_TOKENS, model: str = None,
                 kind: str = 'agent') -> dict:
        """粘性故障转移请求，返回完整的 assistant message dict。

        只有当前接口报错/乱码才向后切换，切换成功后粘住新的。
        """
        _refresh_live_config()      # 管理页刚保存的地址/密钥在下一次请求就生效
        bases = _split_base_urls(API_BASE_URL)
        if not bases:
            raise RuntimeError('没有配置 LLM base_url')
        with self._lock:                       # 串行化：失败换地址时推进起点
            start = self._start
        last_err = None
        for i in range(len(bases)):
            base = bases[(start + i) % len(bases)]
            try:
                m = self._request(base, messages, tools, temperature, max_tokens,
                                  model or MODEL, kind)
                # 带工具调用的回复没有正文，乱码检查只看纯文本回答
                if (not m.get('tool_calls')
                        and _looks_garbled(m.get('content') or '')):
                    print(f'[llm] {base} 返回疑似乱码，换下一个接口', flush=True)
                    last_err = RuntimeError('garbled')
                    continue
                with self._lock:
                    self._start = (start + i) % len(bases)
                return m
            except Exception as e:
                print(f'[llm] {base} 失败：{_classify_http_error(e)}',
                      flush=True)
                last_err = e
        raise last_err or RuntimeError('所有 LLM 接口均失败')

    def chat(self, messages: list, temperature: float = 0.7,
             max_tokens: int = MAX_TOKENS, model: str = None,
             kind: str = 'agent') -> str:
        """纯文本问答（不带工具），返回最终文本。"""
        m = self.chat_raw(messages, None, temperature, max_tokens, model, kind)
        return (m.get('content') or '').strip()

    def _request(self, base: str, messages: list, tools: list,
                 temperature: float, max_tokens: int, model: str,
                 kind: str) -> dict:
        """向 {base}/chat/completions 发一次请求，校验返回体结构。

        base 已是含 /v1 的完整地址；鉴权头只在配置了 key 时才带（本地部署
        常允许空 key，硬塞 `Bearer ` 反而会被部分网关拒掉）。
        """
        payload = {'model': model, 'messages': messages,
                   'temperature': temperature, 'max_tokens': max_tokens}
        if tools:
            payload['tools'] = tools
        headers = {'Content-Type': 'application/json'}
        if API_KEY:
            headers['Authorization'] = f'Bearer {API_KEY}'
        req = urllib.request.Request(
            base + '/chat/completions',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers=headers)
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            raw = r.read()
        try:
            out = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f'返回体不是合法 JSON（{e.msg}）：'
                             f'{raw[:200]!r}') from e
        try:
            msg = out['choices'][0]['message']
        except (KeyError, IndexError, TypeError) as e:
            # 常见于网关错误包装、模型名不对、或返回了非 chat 结构
            raise ValueError('返回体缺少 choices[0].message：'
                             f'{json.dumps(out, ensure_ascii=False)[:300]}') from e
        # 推理型模型在 max_tokens 太小时会把预算全花在 reasoning 上，
        # content 空但推理非空——这不算成功，明确报出来便于定位
        if not (msg.get('content') or '').strip() and not msg.get('tool_calls'):
            reasoning = (msg.get('reasoning_content') or '').strip()
            finish = (out['choices'][0].get('finish_reason') or '')
            raise ValueError(
                f'模型返回了空回答（finish_reason={finish or "?"}，'
                f'reasoning {len(reasoning)} 字）'
                + ('，多半是 max_tokens 太小被推理吃光了' if reasoning else ''))
        self._track(base, model, kind, tools, messages, out.get('usage') or {},
                    time.time() - t0)
        return msg or {}

    def _track(self, host, model, kind, tools, messages, usage, secs) -> None:
        """记一条用量；服务端没给 cached_tokens 时按公共前缀估算。"""
        try:
            prompt = int(usage.get('prompt_tokens') or 0)
            cached = (usage.get('prompt_tokens_details') or {}).get('cached_tokens')
            src = 'server'
            sig = json.dumps(tools or [], ensure_ascii=False) + \
                json.dumps(messages, ensure_ascii=False)
            with self._lock:
                hist = self._history.setdefault(host, deque(maxlen=64))
                if cached is None:
                    best = max((_common_prefix_len(sig, h) for h in hist), default=0)
                    cached, src = int(prompt * best / max(1, len(sig))), 'est'
                hist.append(sig)
            _record_usage({
                'ts': round(time.time(), 1), 'host': host, 'model': model,
                'kind': kind, 'prompt': prompt,
                'completion': int(usage.get('completion_tokens') or 0),
                'cached': int(cached), 'cached_src': src, 'secs': round(secs, 2)})
            # 同时留一行日志：后台「处理过程」只从日志里读，不读 usage.jsonl。
            # 纯聊天不调工具时，没有这行就只剩「收到 / 回复」两条，看起来像
            # 「没有抓到任务执行过程」。节点名去掉协议前缀，只留主机便于辨认。
            host_short = re.sub(r'^https?://', '', host or '').rstrip('/')
            print(f'[llm] {model} @ {host_short} | 入 {prompt} 出 '
                  f'{int(usage.get("completion_tokens") or 0)} '
                  f'缓存 {int(cached)}{"（估）" if src == "est" else ""} '
                  f'| {secs:.1f}s', flush=True)
        except Exception as e:                 # 统计出错绝不影响回话
            print(f'[用量] 记录失败：{e}', flush=True)


_GROUP_PREFIX_RE = None    # 延迟编译：群消息正文前的 "wxid_xxx:\n" 发送者前缀


def _strip_group_prefix(text: str) -> str:
    """去掉群消息正文开头的发送者前缀（wxid_xxx:\n），没有就原样返回。"""
    global _GROUP_PREFIX_RE
    if _GROUP_PREFIX_RE is None:
        import re
        _GROUP_PREFIX_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{5,31}:\n')
    return _GROUP_PREFIX_RE.sub('', text, count=1)


def _resp_ok(r) -> bool:
    """WxResponse 是否成功。没带 is_success 的老接口（返回 None）按成功算。"""
    return not (r is not None and hasattr(r, 'is_success') and not r.is_success)


def _resp_text(r) -> str:
    """从 WxResponse 里取一句可读的结果/失败原因，只截 200 字。

    **WxResponse 是 dict 子类**（`wechatauto/param.py`，键是 ``status`` /
    ``message`` / ``data``），不是带属性的对象——`getattr(r, 'message')`
    在它身上永远是 None。所以要先按 dict 取，再兼容真正带属性的对象。
    """
    if r is None:
        return ''
    if isinstance(r, dict):
        for k in ('message', 'msg', 'error', 'detail'):
            v = r.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:200]
        return str(r.get('status') or '')[:200]
    for attr in ('message', 'msg', 'data', 'error'):
        v = getattr(r, attr, None)
        if isinstance(v, dict):
            for k in ('message', 'msg', 'error', 'detail'):
                s = v.get(k)
                if isinstance(s, str) and s.strip():
                    return s.strip()[:200]
        elif isinstance(v, str) and v.strip():
            return v.strip()[:200]
    return str(r)[:200]


def _send_hang_exceeded(started_at: float, now: float, limit: float) -> bool:
    """一次发送是否已经超过「卡死」上限。

    ``started_at`` 为 0 表示当前没有发送在进行（空闲永远不算卡死）。
    抽成纯函数是为了能在测试里直接喂时间戳验证边界，不用真等 120 秒。
    """
    return bool(started_at) and (now - started_at) >= limit


def _is_transient(msg: str) -> bool:
    """失败原因是不是「过一会儿可能就好了」——锁屏 / 断屏 / 微信暂不可用。

    群发调度器靠它决定是「几分钟后重试」还是「真失败、顺延一个周期」。
    """
    return any(k in (msg or '') for k in SKIP_SEND_REASONS)


class DaFeiYuBot:
    def __init__(self):
        self.llm = LLMClient()
        # 每个群一份长期记忆：{'summary': 历史压缩摘要, 'turns': [(问, 答), ...]}
        # turns 无上限累积，估算上下文超过 CONTEXT_TOKEN_LIMIT 时把最老的
        # 一批压进 summary（最近 COMPRESS_KEEP_TURNS 轮永远保留原文）。
        self.memory = {}
        # 每个会话一份围观记录：deque[(发送者, 文本)]，所有消息都记，
        # 被 @ 时取最近 RECENT_N 条附进上下文（不含触发消息本身）。
        self.recent = {}
        # 跟进对话已移除：只认 @ 触发
        self.paused = False               # 发送失败后置 True，暂停应答防继续乱动界面
        self._db = None                    # 搜索工具用的 WeChatDB，按需打开
        self._opencli = None               # (node, main.js) 缓存，None=还没找过
        self._cgpt_last = 0.0              # 上次 ChatGPT 调用时间
        self._cgpt_count = 0               # 当天已调用次数
        self._cgpt_day = ''                # 当天日期（跨天重置计数）
        self.send_lock = threading.Lock()   # 回话驱动 GUI，必须串行
        # 看门狗用的「发送开始时刻」，0 表示空闲。用时间戳而不是 lock.locked()：
        # Lock 只知道有没有人占，不知道占了多久、是谁占的。
        self._send_started_at = 0.0
        self._watchdog_exit = False
        self._broadcast = None              # 群发调度器（run() 里启动）
        # 关键词广告：配置 keyword_ads.json，热加载；冷却按群持久化。
        # 与 @ 回答各自独立，不经过 LLM，也不写对话记忆。
        self._keyword_ads = keyword_ads.Engine()
        self.agents = _load_agents()        # 多群 Agent（bot_config.json AGENTS，热加载）
        self._agents_mtime = self._cfg_mtime()
        self.known = self._load_known()     # 见过的会话 {id: {name, last, group}}，管理页选群用
        self._known_saved = 0.0
        self._pending_lock = threading.Lock()
        self._pending_delivery_lock = threading.Lock()
        self._pending_chats = {}
        self._pending_wx = None
        self._pending_delivered = set()
        self._pending_account_warned = set()
        try:
            with open(PENDING_REPLIES_FILE, encoding='utf-8') as f:
                self._pending_replies = json.load(f)
            if not isinstance(self._pending_replies, dict):
                raise ValueError('待发回复文件必须是 JSON 对象')
        except FileNotFoundError:
            self._pending_replies = {}
        if self._pending_replies:
            print(f'[待发] 已恢复 {len(self._pending_replies)} 条未送达回复',
                  flush=True)

    def _save_pending_locked(self, replies: dict) -> None:
        """持有待发锁时先原子提交快照，成功后才替换可发送的内存状态。"""
        tmp = PENDING_REPLIES_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(replies, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PENDING_REPLIES_FILE)
        self._pending_replies = replies

    def _queue_reply(self, msg, chat, answer: str, agent_id: str) -> None:
        """按账号和来源消息去重，保存答案及发送前的真实会话水位。"""
        chat_id = chat.who
        db = self._pending_wx._db if self._pending_wx else chat._db
        account = db.wxid
        source = (getattr(msg, 'id', None)
                  or getattr(msg, 'create_time', None) or time.time_ns())
        key = json.dumps([account, agent_id, chat_id, str(source)],
                         ensure_ascii=False)
        with self._pending_lock:
            self._pending_chats[chat_id] = chat
            if key in self._pending_replies:
                return
            rows = db.get_messages(chat_id, limit=1)
            after_seq = max((row.get('sort_seq') or
                             row.get('create_time', 0) * 1000 for row in rows),
                            default=0)
            updated = dict(self._pending_replies)
            updated[key] = {
                'account': account,
                'chat_id': chat_id,
                'chat_name': getattr(chat, 'nickname', '') or chat_id,
                'answer': answer,
                'queued_at': time.time(),
                'after_seq': after_seq,
                # 已补发轮数；持久化，重启后不会把上限清零
                'rounds': 0,
            }
            self._save_pending_locked(updated)
        print(f'[待发] {chat_id}：答案已保存，等待送达', flush=True)

    @staticmethod
    def _reply_recorded(entry: dict, db) -> bool:
        """核对同账号、同会话水位之后的完整出站文本，处理发送后进程崩溃。"""
        offset = 0
        expected = entry['answer'].replace('\r\n', '\n').strip()
        while True:
            rows = db.get_messages(entry['chat_id'], limit=100, offset=offset)
            if not rows:
                return False
            sequences = []
            for row in rows:
                seq = row.get('sort_seq') or row.get('create_time', 0) * 1000
                sequences.append(seq)
                text = _strip_group_prefix(row.get('content') or '')
                if (seq > entry['after_seq']
                        and row.get('sender_username') == entry['account']
                        and text.replace('\r\n', '\n').strip() == expected):
                    return True
            if min(sequences) <= entry['after_seq']:
                return False
            offset += len(rows)

    def _drop_pending_locked(self, key: str, entry: dict, reason: str) -> None:
        """移除一条已放弃补发的答案并落盘（调用方持有 ``_pending_lock``）。

        必须从队列里删掉再记录：只打印「停止重试」但保留 entry，下一轮
        `_retry_pending_replies` 仍会把它交给 `_send_retry`，等于没上限。
        """
        updated = dict(self._pending_replies)
        if key not in updated:
            return
        del updated[key]
        self._save_pending_locked(updated)
        rounds = int(entry.get('rounds', 0) or 0)
        print(f'[!!!] 待发回复已停止重试：{entry.get("chat_id")}'
              f'（已补发 {rounds} 轮，每轮 {1 + SEND_RETRIES} 次）'
              f'最后一次失败：{reason}', flush=True)

    def _flush_pending_replies(self) -> None:
        """串行送出已保存答案；临时不可用就保留，真实发送失败仍暂停。

        临时失败按 `PENDING_MAX_ROUNDS` 限制补发轮数；到上限后删除待发
        条目并记录最后一次失败原因，避免外层线程永远重跑同一条。
        """
        if self.paused or not self._pending_delivery_lock.acquire(blocking=False):
            return
        try:
            while not self.paused:
                with self._pending_lock:
                    for key, entry in self._pending_replies.items():
                        chat = self._pending_chats.get(entry['chat_id'])
                        db = (self._pending_wx._db if self._pending_wx
                              else getattr(chat, '_db', None))
                        if db is not None and entry.get('account') == db.wxid:
                            break
                        if key not in self._pending_account_warned:
                            print(f'[待发] {entry["chat_id"]}：原账号未确认或与'
                                  f'当前账号不一致，保留但不发送', flush=True)
                            self._pending_account_warned.add(key)
                    else:
                        return
                if chat is None:
                    if self._pending_wx is None:
                        return
                    from wechatauto.wx import Chat, _AllMessageChat
                    wx = self._pending_wx
                    chat = _AllMessageChat(
                        entry['chat_id'], entry['chat_name'],
                        factory=lambda who: Chat(who, wx._gui, wx._db),
                        db=wx._db)
                    self._pending_chats[entry['chat_id']] = chat
                # 旧版本留下的 entry 没有 rounds 字段，按 0 处理；达到上限的
                # 先清掉再发送，防止上一轮保存后被中断的条目继续重试。
                rounds = int(entry.get('rounds', 0) or 0)
                if rounds >= PENDING_MAX_ROUNDS:
                    with self._pending_lock:
                        self._drop_pending_locked(
                            key, entry,
                            entry.get('last_error') or '达到补发轮数上限')
                    continue
                recorded = (key in self._pending_delivered
                            or self._reply_recorded(entry, db))
                if recorded:
                    ok, err = True, None
                else:
                    ok, err = self._send_retry(
                        lambda: chat.SendMsg(entry['answer']), '回答')
                if ok:
                    # 确认落盘失败时保留内存标记；重启后再以真实出站记录核对。
                    self._pending_delivered.add(key)
                    with self._pending_lock:
                        updated = dict(self._pending_replies)
                        del updated[key]
                        self._save_pending_locked(updated)
                    self._pending_delivered.discard(key)
                    print(f'[待发] {entry["chat_id"]}：'
                          f'{"已核对出站记录" if recorded else "已送达"}并移出待发',
                          flush=True)
                    continue
                if any(k in (err or '') for k in SKIP_SEND_REASONS):
                    rounds = int(entry.get('rounds', 0) or 0) + 1
                    if rounds >= PENDING_MAX_ROUNDS:
                        with self._pending_lock:
                            self._drop_pending_locked(
                                key, {**entry, 'rounds': rounds}, err)
                    else:
                        with self._pending_lock:
                            updated = dict(self._pending_replies)
                            if key in updated:
                                updated[key] = {**entry, 'rounds': rounds,
                                                'last_error': err}
                                self._save_pending_locked(updated)
                        print(f'[待发] {entry["chat_id"]}：补发失败 '
                              f'{rounds}/{PENDING_MAX_ROUNDS} 轮，保留答案，'
                              f'{err}', flush=True)
                else:
                    self.paused = True
                    print(f'[!!!] 发送失败（已重试 {SEND_RETRIES} 次）：{err}\n'
                          f'[!!!] 机器人已暂停应答（防止继续动键鼠），'
                          f'答案已保存，请检查微信窗口状态后重启程序', flush=True)
                return
        finally:
            self._pending_delivery_lock.release()

    def _retry_pending_replies(self) -> None:
        """只读探测恢复条件后补发；断屏时不叠加看门狗的拓扑修复动作。

        连续异常也计入熔断：基础设施长期坏掉时不能让这个线程每 10 秒
        永远重试同一件事。到上限就退出线程并保留待发文件，重启后再试。
        """
        streak = 0
        while True:
            try:
                with self._pending_lock:
                    pending = bool(self._pending_replies)
                keyword = _display_target()
                display_ready = not keyword or self._display_rect(keyword)
                if (pending and not self.paused and display_ready
                        and self._session_active() and self._desktop_readable()):
                    self._flush_pending_replies()
                streak = 0
            except Exception as e:
                streak += 1
                print(f'[待发] 补发异常（连续 {streak}/{PENDING_MAX_ROUNDS} 次），'
                      f'答案仍保留：{type(e).__name__}: {e}', flush=True)
                if streak >= PENDING_MAX_ROUNDS:
                    print('[!!!] 待发补发连续异常达到上限，停止补发线程；'
                          '待发答案仍保留在 pending_replies.json，'
                          '请检查日志后重启程序', flush=True)
                    return
            time.sleep(WATCH_INTERVAL)

    # -- 围观记录 -------------------------------------------------------

    def _record(self, msg, chat) -> None:
        chat_id = getattr(chat, 'who', '')
        self._touch_known(chat_id, getattr(chat, 'nickname', '') or chat_id)
        buf = self.recent.setdefault(chat_id, deque(maxlen=max(RECENT_N, 100)))
        if msg.type in ('text', '文本'):
            text = _strip_group_prefix(msg.content or '').strip()
        else:
            text = f'[{msg.type}]'          # 图片/文件等只留类型占位
        if text:
            ts = getattr(msg, 'create_time', None) or time.time()
            buf.append((ts, getattr(msg, 'sender', '') or '有人',
                        text[:RECENT_LINE_MAX]))

    def _recent_log(self, chat_id: str, n: int = RECENT_N) -> str:
        """最近群消息日志（不含最后一条 = 当前触发消息），没有返回空串。

        每条带时间戳和昵称，模型引用群消息时能报出「谁、什么时候说的」。
        """
        lines = ['[%s] %s: %s' % (time.strftime('%m-%d %H:%M',
                                                 time.localtime(ts)), who, text)
                 for ts, who, text in self.recent.get(chat_id, ())][:-1][-n:]
        if not lines:
            return ''
        return ('【本群最近聊天记录（供参考，不一定与问题相关）】\n'
                + '\n'.join(lines))

    # -- 已知会话 / Agent 路由 ----------------------------------------------

    @staticmethod
    def _load_known() -> dict:
        try:
            with open(KNOWN_CHATS_FILE, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _touch_known(self, chat_id: str, name: str) -> None:
        """记下见过的会话；新会话立即落盘，老会话最多 60 秒落一次。"""
        if not chat_id:
            return
        new = chat_id not in self.known
        self.known[chat_id] = {'name': name, 'last': int(time.time()),
                               'group': chat_id.endswith('@chatroom')}
        if new or time.time() - self._known_saved > 60:
            self._known_saved = time.time()
            try:
                tmp = KNOWN_CHATS_FILE + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self.known, f, ensure_ascii=False)
                os.replace(tmp, KNOWN_CHATS_FILE)
            except OSError:
                pass

    @staticmethod
    def _cfg_mtime():
        try:
            return os.path.getmtime(BOT_CONFIG_FILE)
        except OSError:
            return None

    def route(self, chat_id: str, name: str):
        """按群 ID / 群名找负责的 Agent：精确绑定优先，其次 "*" 兜底，都没有返回 None。

        bot_config.json 改了就重新加载 AGENTS（管理页改 Agent 不用重启）。
        """
        mt = self._cfg_mtime()
        if mt != self._agents_mtime:
            self._agents_mtime = mt
            self.agents = _load_agents()
            print(f'[配置] AGENTS 已重新加载：{len(self.agents)} 个', flush=True)
        live = [a for a in self.agents if a['enabled']]
        for a in live:
            if chat_id in a['groups'] or (name and name in a['groups']):
                return a
        return next((a for a in live if '*' in a['groups']), None)

    # -- 记忆管理 -------------------------------------------------------

    @staticmethod
    def _context_tokens(st: dict) -> int:
        t = est_tokens(st['summary'])
        for q, a in st['turns']:
            t += est_tokens(q) + est_tokens(a)
        return t

    def _summarize(self, old_summary: str, batch: list) -> str:
        """让 LLM 把旧摘要 + 一批最老对话压成一份新摘要。"""
        lines = '\n'.join(f'群友：{q}\n大肥鱼：{a}' for q, a in batch)
        prompt = (
            '你在维护一个微信群 AI 的长期记忆。请把【旧摘要】和【新对话】'
            '合并压缩成一份要点摘要，500 字以内，保留：聊过的话题、关键'
            '事实/结论/承诺、人名与称呼、群里的梗、没聊完的事。直接输出'
            '摘要正文，不要评论。\n\n'
            f'【旧摘要】\n{old_summary or "（无）"}\n\n【新对话】\n{lines}')
        return self.llm.chat([{'role': 'user', 'content': prompt}],
                             temperature=0.3, max_tokens=SUMMARY_MAX_TOKENS,
                             kind='summary')

    def _maybe_compress(self, chat_id: str, st: dict) -> None:
        """上下文估算 token 超限就滚动压缩最老的历史。"""
        while (len(st['turns']) > COMPRESS_KEEP_TURNS
               and self._context_tokens(st) > CONTEXT_TOKEN_LIMIT):
            batch = []
            # 一次压一批，单次摘要输入封顶 80K 估算 token，防止撑爆摘要请求
            while (len(st['turns']) > COMPRESS_KEEP_TURNS
                   and est_tokens('\n'.join(q + a for q, a in batch)) < 80_000):
                batch.append(st['turns'].popleft())
            before = self._context_tokens(st)
            st['summary'] = self._summarize(st['summary'], batch)
            print(f'[记忆] {chat_id[:20]}… 上下文≈{before} tokens，'
                  f'压缩 {len(batch)} 轮 → 摘要 {est_tokens(st["summary"])} tokens',
                  flush=True)

    # -- LLM 组装 -----------------------------------------------------

    def _build_messages(self, st: dict, question: str,
                        prompt: str = None) -> list:
        """严格前缀追加：开头固定，其余内容只会在末尾长出新块。

        「最近群消息」不再作为临时 system 消息插在倒数第二位（那样下一轮
        同位置内容就变了，前缀从那里断开），而是由 think 拼进问题消息并随
        (q, answer) 进历史——下一轮它原样出现在同一位置，vLLM 前缀缓存才能
        整段命中。唯一允许的前缀失效是滚动压缩和表情列表变化。
        """
        msgs = [{'role': 'system', 'content': prompt or SYSTEM_PROMPT}]
        if st['summary']:
            msgs.append({'role': 'system',
                         'content': f'【更早对话的摘要】\n{st["summary"]}'})
        for q, a in st['turns']:
            msgs += [{'role': 'user', 'content': q},
                     {'role': 'assistant', 'content': a}]
        msgs.append({'role': 'user', 'content': question})
        return msgs

    # -- 表情包/图片发送 ---------------------------------------------------

    @staticmethod
    def _list_stickers() -> list:
        """列出 MEDIA_DIR 下的表情/图片文件（GIF/JPG/PNG/WEBP/BMP）。"""
        try:
            return sorted(f for f in os.listdir(MEDIA_DIR)
                          if f.lower().endswith(
                              ('.gif', '.jpg', '.jpeg', '.png',
                               '.webp', '.bmp')))
        except OSError:
            return []

    def _sticker_tool(self) -> dict:
        """动态构建 send_sticker 工具定义，可用名单写进描述里。"""
        names = '、'.join(os.path.splitext(f)[0]
                          for f in self._list_stickers()) or '（暂无）'
        return {
            'type': 'function',
            'function': {
                'name': 'send_sticker',
                'description': (
                    '发送一个表情包/图片到群里。当前可用：' + names +
                    '。name 填上面的名字（可不含扩展名，支持模糊匹配）。'
                    '闲聊逗趣时用，正经技术回答不要乱发。'),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'name': {'type': 'string',
                                 'description': '表情/图片名字'},
                    },
                    'required': ['name'],
                },
            },
        }

    @staticmethod
    def _match_sticker(name: str):
        """按名字模糊匹配表情文件，返回完整路径或 None。"""
        files = DaFeiYuBot._list_stickers()
        if not files:
            return None
        key = os.path.splitext((name or '').strip().lower())[0]
        for f in files:                    # 精确：文件名含/不含扩展名都认
            if f.lower() == (name or '').strip().lower() \
                    or os.path.splitext(f)[0].lower() == key:
                return os.path.join(MEDIA_DIR, f)
        for f in files:                    # 模糊：子串命中就用
            if key and key in f.lower():
                return os.path.join(MEDIA_DIR, f)
        return None

    def _send_sticker(self, name: str, chat) -> str:
        """匹配并发送表情/图片，返回给模型的工具结果文本。

        发送失败只把错误回给模型（让它改用文字），不触发暂停——媒体粘贴
        失败和文本粘贴失败是同一类可重试问题，且文字链路有自己的保护。
        """
        if chat is None:
            return '当前模式无法发送媒体，请改用文字回答'
        path = self._match_sticker(name)
        if not path:
            avail = '、'.join(os.path.splitext(f)[0]
                              for f in self._list_stickers()) or '（空）'
            return f'没有找到「{name}」，可用：{avail}'
        t0 = time.time()
        ok, err = self._send_retry(lambda: chat.SendFiles(path), '表情')
        print(f'[表情] {os.path.basename(path)} -> '
              f'{"成功" if ok else err} ({time.time() - t0:.1f}s)', flush=True)
        if ok:
            return f'已发送 {os.path.basename(path)}'
        return f'发送失败（{err}），请改用文字回答'

    # -- ChatGPT 桥 -------------------------------------------------------

    def _resolve_opencli(self):
        """定位 npm 全局的 opencli 入口，返回 (node, main.js) 或 (None, None)。

        用 node 直跑 main.js 而不是 opencli.cmd：绕开 cmd.exe 的中文参数
        编码坑，node 从命令行拿到的就是 UTF-16 原文。纯文件定位（opencli
        的 shim 在 <全局目录>/opencli.cmd，包体在旁边的 node_modules/ 下），
        不起子进程——Windows 上 subprocess 直接调 npm.cmd 会 FileNotFoundError。
        """
        if self._opencli is None:
            node = shutil.which('node')
            main_js = None
            shim = shutil.which('opencli') or ''
            if node and shim:
                cand = os.path.join(os.path.dirname(shim), 'node_modules',
                                    '@jackwener', 'opencli',
                                    'dist', 'src', 'main.js')
                if os.path.isfile(cand):
                    main_js = cand
            self._opencli = (node, main_js)
        return self._opencli

    def _chatgpt_files(self, conversation_id: str, chat) -> list:
        """下载该会话里 ChatGPT 生成的文件并转发到群，返回已转发路径。

        走用户适配器 opencli chatgpt files（sandbox 产物/附件）；下载和
        转发任何失败都只记日志返回空列表，不影响文字回答。
        """
        node, main_js = self._resolve_opencli()
        if not node or not main_js or not conversation_id:
            return []
        out_dir = os.path.join(CHATGPT_IMAGES_DIR,
                               'files_' + time.strftime('%Y%m%d_%H%M%S'))
        os.makedirs(out_dir, exist_ok=True)
        try:
            proc = subprocess.run(
                [node, main_js, 'chatgpt', 'files',
                 '--conversation', conversation_id,
                 '--op', out_dir, '--format', 'json', '--timeout', '120'],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=180)
        except Exception as e:
            print(f'[ChatGPT] 文件拉取异常：{e}', flush=True)
            return []
        if proc.returncode != 0:
            return []                     # 没有文件是常态，静默
        files = []
        try:
            rows = json.loads(proc.stdout)
            if isinstance(rows, dict) and isinstance(rows.get('data'), list):
                rows = rows['data']
            for row in (rows if isinstance(rows, list) else [rows]):
                f = (row or {}).get('file') or ''
                if f and os.path.isfile(f):
                    files.append(f)
        except Exception:
            return []
        sent = []
        for f in files:
            ok, err = self._send_retry(lambda p=f: chat.SendFiles(p),
                                       '文件转发')
            print(f'[ChatGPT] 文件 {os.path.basename(f)} -> '
                  f'{"成功" if ok else err}', flush=True)
            if ok:
                sent.append(f)
        return sent

    def _ask_chatgpt(self, prompt: str, web_search: bool = False,
                     chat=None) -> str:
        """转问题给 ChatGPT，失败自动重试（最多 CHATGPT_RETRIES 次）。

        限流：冷却间隔。失败（没装桥/扩展没连/没登录/超时）只把原因回给
        模型，不影响其它回答链路。
        """
        prompt = (prompt or '').strip()
        if not prompt:
            return '错误：prompt 不能为空'
        now = time.time()
        if now - self._cgpt_last < CHATGPT_COOLDOWN:
            wait = int(CHATGPT_COOLDOWN - (now - self._cgpt_last)) + 1
            return f'调用太频繁，请 {wait} 秒后再试'
        node, main_js = self._resolve_opencli()
        if not node or not main_js:
            return 'ChatGPT 桥不可用：本机没找到 opencli 运行时'
        self._cgpt_last = now
        result = '未知失败'
        for attempt in range(1 + CHATGPT_RETRIES):
            if attempt:
                print(f'[ChatGPT] 失败，第 {attempt}/{CHATGPT_RETRIES} 次重试…',
                      flush=True)
                time.sleep(5)
            ok, result = self._ask_chatgpt_once(
                node, main_js, prompt, web_search, chat)
            if ok:
                return result
        return result

    def _ask_chatgpt_once(self, node, main_js, prompt: str,
                          web_search: bool, chat) -> tuple:
        """单次 ask 尝试，返回 (是否成功, 给模型的文本)。"""
        print(f'[ChatGPT] 转问{"（联网）" if web_search else ""}…', flush=True)
        t0 = time.time()
        cmd = [node, main_js, 'chatgpt', 'ask', prompt,
               '--format', 'json', '--timeout', str(CHATGPT_TIMEOUT),
               '--new']          # 每次尝试（含重试）都开全新会话，不接续旧对话
        if web_search:
            cmd.append('--web-search')
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=CHATGPT_TIMEOUT + 60)
        except subprocess.TimeoutExpired:
            return False, 'ChatGPT 超时没有给出回答'
        except FileNotFoundError:
            return False, 'ChatGPT 桥不可用：node 未找到'
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or '').strip()[:300]
            return False, (f'ChatGPT 调用失败：{err or "未知错误"}'
                           f'（检查 Chrome 的 OpenCLI 扩展和 chatgpt.com 登录态）')
        try:
            rows = json.loads(proc.stdout)
            if isinstance(rows, dict) and isinstance(rows.get('data'), list):
                rows = rows['data']          # opencli 把行包在 data 字段里
            row0 = (rows[0] if isinstance(rows, list) and rows
                    else rows if isinstance(rows, dict) else {})
            response = row0.get('response')
            conversation_id = str(row0.get('conversationId') or '')
        except Exception:
            return False, ('ChatGPT 返回了无法解析的内容：'
                           + (proc.stdout or '')[:200])
        if not (response or '').strip():
            return False, 'ChatGPT 没有返回内容'
        print(f'[ChatGPT] 完成 ({time.time() - t0:.0f}s)，'
              f'{len(response)} 字', flush=True)
        # 回答里若有生成的文件（代码解释器产物等），自动拉下来转发到群
        forwarded = []
        if chat is not None and conversation_id:
            try:
                forwarded = self._chatgpt_files(conversation_id, chat)
            except Exception as e:
                print(f'[ChatGPT] 文件拉取异常：{e}', flush=True)
        note = ''
        if forwarded:
            names = '、'.join(os.path.basename(f) for f in forwarded)
            note = f'\n（已把 ChatGPT 生成的文件发到群里：{names}）'
        return True, f'ChatGPT 的回答：\n{response[:4000]}{note}'

    def _chatgpt_image(self, prompt: str, chat) -> str:
        """让 ChatGPT 生图并转发到群，失败自动重试（最多 CHATGPT_RETRIES 次）。

        失败只把原因回给模型；下载到的文件按次放独立子目录，不自动清理。
        """
        prompt = (prompt or '').strip()
        if not prompt:
            return '错误：prompt 不能为空'
        if chat is None:
            return '当前模式无法转发图片，请改用文字回答'
        now = time.time()
        if now - self._cgpt_last < CHATGPT_COOLDOWN:
            wait = int(CHATGPT_COOLDOWN - (now - self._cgpt_last)) + 1
            return f'调用太频繁，请 {wait} 秒后再试'
        node, main_js = self._resolve_opencli()
        if not node or not main_js:
            return 'ChatGPT 桥不可用：本机没找到 opencli 运行时'
        self._cgpt_last = now
        result = '未知失败'
        for attempt in range(1 + CHATGPT_RETRIES):
            if attempt:
                print(f'[ChatGPT] 生图失败，第 {attempt}/{CHATGPT_RETRIES} 次'
                      f'重试…', flush=True)
                time.sleep(5)
            ok, result = self._chatgpt_image_once(node, main_js, prompt, chat)
            if ok:
                return result
        return result

    def _chatgpt_image_once(self, node, main_js, prompt: str, chat) -> tuple:
        """单次生图尝试，返回 (是否成功, 给模型的文本)。"""
        out_dir = os.path.join(CHATGPT_IMAGES_DIR,
                               time.strftime('%Y%m%d_%H%M%S'))
        os.makedirs(out_dir, exist_ok=True)
        # image 适配器没有 --new，先开一个全新会话再画（重试时同样生效）
        try:
            subprocess.run(
                [node, main_js, 'chatgpt', 'new'],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=90)
        except Exception as e:
            print(f'[ChatGPT] 新建会话失败（沿用当前会话画）：{e}', flush=True)
        print('[ChatGPT] 生图…', flush=True)
        t0 = time.time()
        try:
            proc = subprocess.run(
                [node, main_js, 'chatgpt', 'image', prompt,
                 '--op', out_dir, '--format', 'json',
                 '--timeout', str(CHATGPT_TIMEOUT)],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=CHATGPT_TIMEOUT + 90)
        except subprocess.TimeoutExpired:
            return False, 'ChatGPT 生图超时了'
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or '').strip()[:300]
            return False, (f'ChatGPT 生图失败：{err or "未知错误"}'
                           f'（检查 Chrome 的 OpenCLI 扩展和 chatgpt.com 登录态）')
        files = []
        try:
            rows = json.loads(proc.stdout)
            if isinstance(rows, dict) and isinstance(rows.get('data'), list):
                rows = rows['data']
            for row in (rows if isinstance(rows, list) else [rows]):
                f = (row or {}).get('file') or ''
                if not f:
                    continue
                # opencli 的输出带 📁/✅ 之类的装饰前缀，剥到盘符开头
                m = re.search(r'[A-Za-z]:[\\/].*', f)
                if m:
                    f = m.group(0)
                if os.path.isfile(f):
                    files.append(f)
        except Exception:
            pass
        if not files:
            return False, ('生图完成但没下载到图片文件：'
                           + (proc.stdout or '')[:200])
        sent = []
        for f in files:
            ok, err = self._send_retry(lambda p=f: chat.SendFiles(p),
                                       '图片转发')
            print(f'[ChatGPT] 图片 {os.path.basename(f)} -> '
                  f'{"成功" if ok else err}', flush=True)
            if ok:
                sent.append(f)
        if not sent:
            return False, '图片下载到了，但发到微信群失败了，请改用文字描述'
        return True, (f'已把 ChatGPT 生成的 {len(sent)}/{len(files)} 张图片'
                      f'发到群里，之后正常给文字回答')

    def _wait_for_display(self, timeout: float = 20.0) -> bool:
        """发送前确保目标显示器在，必要时主动修一次。

        断屏瞬间调 `SendMsg` 是最坏的时序：guia 的截图/OCR 在虚拟屏消失后
        会反复重试（实测卡了一分半没有任何输出），而它持有 `send_lock`，
        后续所有回复都会被堵住——表现就是「机器人收到了但不回」。

        **不能只等看门狗**：它遇到 `rc=5`（会话不可写）会退避到 60 秒，
        而用户此刻正等着回复。实测 09:02 屏掉、09:03 用户发消息，正好落在
        退避窗口里，回复被跳过。所以这里先主动修一次（成本是一次
        `SetDisplayConfig`），修不回来再按 timeout 轮询。

        虚拟屏开关关掉、或没配显示器名时直接返回 True（窗口位置不归我们管）。
        """
        keyword = _display_target()
        if not keyword:
            return True
        if self._display_rect(keyword):
            return True
        # 屏不在：不等看门狗，自己先试一次
        try:
            self._reenable_display(keyword)
        except Exception as e:
            print(f'[显示] 发送前修复异常：{type(e).__name__}: {e}', flush=True)
        if self._display_rect(keyword):
            print(f'[显示] 发送前修复成功，{keyword} 已回来', flush=True)
            return True
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._display_rect(keyword):
                return True
            time.sleep(2)
        return False

    @staticmethod
    def _desktop_readable() -> bool:
        """桌面 DC 是否可读——用来识别「会话断开、画面已冻结」。

        虚拟屏上 ``ImageGrab`` 会**成功但返回全黑**，那种情况 ``PrintWindow``
        仍然拿得到实时内容，发送没问题。但锁屏 / 远程断开时它会**抛异常**
        ``screen grab failed``，此时 DWM 停止合成，``PrintWindow`` 抓到的
        是冻结的最后一帧：搜索框里的字改了、列表却不重绘，``find_session``
        在死画面上永远找不到会话（实测卡 3 分钟以上，还占着 send_lock）。

        所以只看一个信号：grab 抛不抛异常。抛了就当桌面不可读，直接跳过
        本轮发送；答案保存在待发文件，等会话恢复后补发。
        """
        from PIL import ImageGrab
        try:
            ImageGrab.grab(bbox=(0, 0, 64, 64))
            return True
        except Exception:
            return False

    @staticmethod
    def _session_active() -> bool:
        """当前 Windows 会话是否可交互（WTSActive）。

        远程控制断开、锁屏时会话会变成 WTSDisconnected。此时 UIA 的跨进程
        调用**没有超时保护**：实测 ``ValuePattern.SetValue`` 卡了 5 分钟以上
        没返回，而它持有 ``send_lock``，之后所有回复都被堵死（日志停在
        ``[回复]``，既没有 ``[发送]`` 也没有失败——看起来像什么都没发生）。

        发送前先查一次，不可交互就保留待发答案，恢复后再补发，
        不会把整条发送链路一起拖住。

        用 pywin32 自带的 ``win32ts``（pywin32 已是依赖），不自己声明
        wtsapi32 的 ctypes 绑定——少写二十行，也不用管 WTSFreeMemory。

        查不出来时按「可交互」处理：误判成不可用会让机器人永久不回复，
        比偶发一次卡死更糟。
        """
        try:
            import win32ts
            state = win32ts.WTSQuerySessionInformation(
                win32ts.WTS_CURRENT_SERVER_HANDLE,
                win32ts.WTS_CURRENT_SESSION,
                win32ts.WTSConnectState)
            return state == win32ts.WTSActive
        except Exception:
            return True

    @staticmethod
    def _uia_responsive(timeout: float = UIA_PROBE_TIMEOUT) -> bool:
        """探测微信 UIA 是否还在响应（超时或异常都返回 False）。

        UIA 的跨进程调用没有超时保护：微信忙时一次读控件会一直不返回。
        这里在**独立线程里新建** Control 对象——对象属于探针线程自己，
        不存在「主线程创建的对象拿到别的线程用」那种 COM 死锁（实测过：
        在子线程里调主线程建的 UIA 对象，会把发送卡到超时）。探针卡住就
        让它卡在 daemon 线程里，不拖主线程。

        查不到窗口或探测抛异常一律按「不响应」处理：宁可多留一轮待发，
        也不要把主线程送进一个可能卡几分钟的 SendMsg。
        """
        hwnd = DaFeiYuBot._find_wechat_window()
        if not hwnd:
            return False
        box = {}

        def _probe():
            try:
                import uiautomation as auto
                auto.InitializeUIAutomationInCurrentThread()
                box['name'] = auto.ControlFromHandle(hwnd).ClassName
            except Exception as exc:
                box['error'] = exc

        t = threading.Thread(target=_probe, daemon=True, name='uia-probe')
        t.start()
        t.join(timeout)
        return (not t.is_alive()) and 'name' in box

    def _broadcast_send(self, target: dict, task: dict) -> tuple:
        """群发单个目标，返回 ``(是否成功, 失败原因, 是否可重试)``。

        第三个值是给调度器的：锁屏 / 断屏 / 微信未就绪这类**临时不可用**不消耗
        排期，过几分钟重试；目标已不存在、图片缺失这类才算真失败。

        **不复用 `_send_retry`**：那里整轮重试 4 次、单次可能十几秒，几十个
        目标会把一轮拖到几十分钟；群发目标是定时批量，失败就记账，等下一轮
        或用户手动重试。代价是偶发的「已发出但没确认」不会自动再试。

        文本和图片是两条微信消息（微信没有图文合一的类型），任一环节失败
        都算该目标失败，并带上具体原因：目标不存在 / 文本失败 / 图片缺失。
        """
        wx = self._pending_wx
        if wx is None:
            return False, '微信尚未就绪', True
        tid = target.get('id') or ''
        db = wx._db
        # get_nickname 查不到时会把 username 原样返回，不能当「存在」依据；
        # search_contact 精确比对 username 才作数（实测 0.02s 一次）。
        # 不先拦的话，不存在的目标会走完整个发送重试（实测 170 秒）才失败。
        try:
            rows = db.search_contact(tid) or []
            if not any((r.get('username') or '') == tid for r in rows):
                return False, '目标已不存在（群解散、被移出或好友已删除）', False
        except Exception:
            pass                     # 读库失败不阻断发送，交给 SendMsg 判
        if not self._wait_for_display():
            return False, f'目标显示器 {_display_target()!r} 未恢复', True
        if not self._session_active():
            return False, 'Windows 会话不可交互（远程断开/锁屏）', True
        if not self._desktop_readable():
            return False, '桌面不可读（会话断开，画面已冻结）', True

        text = (task.get('text') or '').strip()
        img = broadcast.image_path(task)
        if img and not os.path.isfile(img):
            return False, f'图片不存在：{task.get("image")}', False

        # 会话打开限时（实测有效）：OCR 每轮之间检查一次 open_deadline，
        # 打不开的目标 25 秒内放弃，而不是拖 1~2 分钟。
        # 单次 OCR/UIA 调用本身中断不了（跨线程会死锁，已验证），
        # 所以这是协作式超时，不是硬超时——硬超时需要进程隔离，
        # 但会引入两套 UIA client 冲突（实测 COMError），暂不做。
        gui = getattr(wx, '_gui', None)
        if gui is not None:
            gui.open_deadline = time.time() + BROADCAST_OPEN_TIMEOUT

        def _fail(why: str, retry: bool) -> tuple:
            """失败返回；如果已经超时，把原因换成可读的「打开会话超时」。"""
            if gui is not None and getattr(gui, 'open_deadline', 0) \
                    and time.time() > gui.open_deadline:
                return (False,
                        f'{why}（打开会话超过 {BROADCAST_OPEN_TIMEOUT} 秒上限）',
                        True)
            return False, why, retry

        try:
            from wechatauto.wx import Chat
            chat = Chat(tid, wx._gui, wx._db)
            self._send_started_at = time.time()
            try:
                with self.send_lock:
                    if text:
                        r = chat.SendMsg(text)
                        if not _resp_ok(r):
                            why = _resp_text(r) or '未知原因'
                            return _fail(f'文本发送失败：{why}', _is_transient(why))
                    if img:
                        r = chat.SendFiles(img)
                        if not _resp_ok(r):
                            why = _resp_text(r) or '未知原因'
                            return _fail(f'图片发送失败：{why}', _is_transient(why))
            finally:
                self._send_started_at = 0.0
        except Exception as e:
            return _fail(f'发送异常：{type(e).__name__}: {e}', True)
        finally:
            if gui is not None:
                gui.open_deadline = 0
        return True, '', False

    def _send_retry(self, send, desc: str = '消息',
                    retries: int = None) -> tuple:
        """带重试的群发送：send 是返回 WxResponse 的无参发送动作。

        默认共尝试 ``1 + SEND_RETRIES`` 次；关键词广告传 ``retries=0``，
        只尝试一次，避免一次失败在同一个窗口上连撞 4 次。返回
        ``(是否成功, 最后的失败原因)``。注意有重复发送风险（上次其实
        发出去了只是没确认），按需求宁可重复也要送到——关键词广告是例外，
        它宁可漏发也不重复刷屏。

        每次都先把答案存进待发文件，发送前查三道（显示器/会话/桌面）；
        真正调 send() 前再探一次 UIA 是否响应——UIA 卡住时 SendMsg 会
        一直等（实测 396 秒）并占死 send_lock，探针拦下来就只算临时失败
        （命中 SKIP_SEND_REASONS），答案保留待发，恢复后自动补发。
        """
        last = '未知失败'
        t0 = time.time()
        attempts = 1 + (SEND_RETRIES if retries is None
                        else max(0, int(retries)))
        # 断屏期间不能进 SendMsg：会卡死并占住 send_lock
        if not self._wait_for_display():
            return False, f'目标显示器 {_display_target()!r} 未恢复，本轮不发送'
        # 会话不可交互（远程断开/锁屏）时 UIA 调用会无限期阻塞，见
        # _session_active 的说明。这一关必须放在 SendMsg 之前。
        if not self._session_active():
            return False, 'Windows 会话不可交互（远程断开/锁屏），本轮不发送'
        # 会话显示 Active 但画面已冻结（DWM 停止合成）也要拦：此时
        # PrintWindow 抓到的是最后一帧，搜索框改了、列表不重绘，
        # find_session 在死画面上永远找不到会话。见 _desktop_readable。
        if not self._desktop_readable():
            return False, '桌面不可读（会话断开/锁屏，画面已冻结），本轮不发送'
        # 发送前复查窗口位置：被手动拖回主屏时拉回目标屏，
        # 否则机器人发消息会在用户正在用的屏幕上弹窗
        self._ensure_window_on_display()
        # 探针必须在 send() 之前：SendMsg 一旦进了不响应的 UIA 就出不来
        if not self._uia_responsive():
            print(f'[发送] {desc}微信 {UIA_PROBE_TIMEOUT:.0f}s 内没回应控件查询'
                  f'（UIA 无响应），本轮放弃，答案保留待发', flush=True)
            return False, (f'UIA 无响应：微信 {UIA_PROBE_TIMEOUT:.0f}s 内没有回应'
                           f'控件查询，答案保留待发')
        for i in range(attempts):
            raised = False
            attempt_t0 = time.time()
            try:
                self._send_started_at = time.time()
                try:
                    with self.send_lock:
                        if TEST_HANG_SEND:
                            time.sleep(TEST_HANG_SEND)
                        r = send()
                finally:
                    self._send_started_at = 0.0
            except Exception as e:
                last = f'{type(e).__name__}: {e}'
                print(f'[发送] {desc}第 {i + 1} 次异常：{last}', flush=True)
                r, raised = None, True
            used = time.time() - attempt_t0
            if used > SEND_ATTEMPT_TIMEOUT:
                # 已经进去的调用没法中断，只能事后记账：让它看起来不像正常的 12 秒
                print(f'[发送] {desc}第 {i + 1} 次耗时 {used:.1f}s'
                      f'（UIA 卡顿，正常 12~14s）', flush=True)
            # 异常绝不算成功。原来的写法在 except 里把 r 置 None，下一行
            # ``r is None`` 直接命中成功分支，于是「截图失败」这类真失败被
            # 记成「已发出」——消息丢了日志还看不出来。
            if not raised and (r is None or not hasattr(r, 'is_success')
                               or r.is_success):
                # 成功也留一行：否则日志里只有「收到/回复」，看不出到底发没发出去
                print(f'[发送] {desc}已发出（第 {i + 1} 次尝试，'
                      f'{time.time() - t0:.1f}s）', flush=True)
                return True, None
            if not raised:
                last = str(r)
            if i < attempts - 1:
                print(f'[发送] {desc}失败（{last}），{SEND_RETRY_WAIT}s 后'
                      f'重试第 {i + 2}/{attempts} 次', flush=True)
                time.sleep(SEND_RETRY_WAIT)
        return False, last

    def _say_to_group(self, text: str, chat) -> str:
        """运行中把一条进度消息直接发到群里（say_to_group 工具实现）。

        失败只把原因回给模型，不在这里暂停：暂停判定归正式回答那一路。
        """
        text = (text or '').strip()
        if not text:
            return '错误：text 不能为空'
        if chat is None:
            return '当前模式发不了消息'
        t0 = time.time()
        ok, err = self._send_retry(lambda: chat.SendMsg(text), 'say')
        print(f'[say] {text[:50]!r} -> {"成功" if ok else err} '
              f'({time.time() - t0:.1f}s)', flush=True)
        return '已发送' if ok else f'发送失败：{err}'

    def think(self, chat_id: str, question: str, recent: str = '',
              who: str = '', chat=None, agent: dict = None) -> tuple:
        """带群上下文跑一轮 agent 循环，返回 (回答, 要@的昵称或 None)。

        提问带上「【昵称】」前缀进上下文和长期记忆，模型因此知道在跟谁
        说话；模型在回答开头写「@昵称」就转成真正的 @ 发出，没有就不 @。
        记忆按 (Agent, 群) 隔离：同一个群换了 Agent 不会串人设。
        """
        agent = agent or self.agents[0]
        st = self.memory.setdefault(
            f'{agent["id"]}|{chat_id}', {'summary': '', 'turns': deque()})
        self._maybe_compress(chat_id, st)
        q = f'【{who}】{question}' if who else question
        if recent:
            # 围观记录拼进问题消息开头并随 (q, answer) 进历史：下一轮它原样
            # 留在原位，跨轮上下文严格前缀追加，vLLM 前缀缓存才能整段命中
            q = f'{recent}\n\n{q}'
        msgs = self._build_messages(st, q, agent['system_prompt'])
        answer = self._agent_loop(msgs, chat_id, chat, agent)
        at = None
        if answer.startswith('@'):
            # 优先按已知的发送者全名匹配（昵称可能带空格/emoji，按空白截断
            # 会切坏名字，实测 🍒 ୯ChouZa୬ 这种装饰名就翻车了）
            if who and answer[1:].startswith(who):
                at = who
                answer = answer[1 + len(who):].lstrip(' \u2005，,：:')
            else:
                m = re.match(r'^@(\S{1,32})\s*', answer)
                if m and m.group(1) not in (AT_NAME, agent['at_name']):
                    at = m.group(1)
                    answer = answer[m.end():].lstrip()
        st['turns'].append((q, answer))
        return answer, at

    # -- agent 工具循环 ---------------------------------------------------

    def _agent_loop(self, msgs: list, chat_id: str = None,
                    chat=None, agent: dict = None) -> str:
        """跑工具调用循环，返回最终文本回答（可能为空串=只发了媒体）。

        工具轮次只是临时消息（不进长期记忆，不影响前缀稳定）；接口不支持
        工具或全部节点失败时降级为无工具直答，保证一定能回话。
        send_sticker 发的是真文件（表情/图片），发成功后模型可以只给空文字。
        """
        agent = agent or self.agents[0]
        allowed = set(agent['tools'])
        all_tools = {'search_group_messages': SEARCH_TOOL, 'ask_chatgpt': CHATGPT_TOOL,
                     'chatgpt_image': CHATGPT_IMAGE_TOOL, 'say_to_group': SAY_TOOL,
                     'get_ai_news': AIHOT_TOOL}
        tools = [t for n, t in all_tools.items() if n in allowed]
        if 'send_sticker' in allowed:
            tools.append(self._sticker_tool())
        opts = {'temperature': agent['temperature'],
                'max_tokens': agent['max_tokens'], 'model': agent['model']}
        media_sent = False
        for _ in range(TOOL_ROUNDS):
            try:
                m = self.llm.chat_raw(msgs, tools=tools or None, **opts)
            except Exception as e:
                print(f'[agent] 工具请求失败（{e}），降级为无工具直答',
                      flush=True)
                text = self.llm.chat(msgs, **opts)
                return text if (text or media_sent) else FAIL_REPLY
            tcs = m.get('tool_calls') or []
            if not tcs:
                text = (m.get('content') or '').strip()
                if not text:
                    return '' if media_sent else FAIL_REPLY
                return text
            msgs.append({'role': 'assistant',
                         'content': m.get('content') or '',
                         'tool_calls': tcs})
            for tc in tcs:
                fn = tc.get('function') or {}
                try:
                    args = json.loads(fn.get('arguments') or '{}')
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                name = fn.get('name') or ''
                if name not in allowed:
                    result = f'工具 {name} 未对当前 Agent 开放'
                elif name == 'say_to_group':
                    result = self._say_to_group(args.get('text') or '', chat)
                elif name == 'send_sticker':
                    result = self._send_sticker(args.get('name') or '', chat)
                    if result.startswith('已发送'):
                        media_sent = True
                elif name == 'ask_chatgpt':
                    result = self._ask_chatgpt(args.get('prompt') or '',
                                               bool(args.get('web_search')),
                                               chat=chat)
                elif name == 'chatgpt_image':
                    result = self._chatgpt_image(args.get('prompt') or '', chat)
                    if result.startswith('已把'):
                        media_sent = True
                elif name == 'get_ai_news':
                    result = self._ai_news(args.get('action') or 'hot',
                                           args.get('keyword') or '',
                                           args.get('category') or '')
                else:
                    result = self._exec_tool(name, args, chat_id)
                msgs.append({'role': 'tool',
                             'tool_call_id': tc.get('id') or '',
                             'content': result})
        print('[agent] 工具轮数用尽，强制直接回答', flush=True)
        text = self.llm.chat(msgs, **opts)
        return text if (text or media_sent) else FAIL_REPLY

    def _exec_tool(self, name: str, args: dict, chat_id: str = None) -> str:
        t0 = time.time()
        try:
            if name == 'search_group_messages':
                out = self.search_group_messages(
                    args.get('keyword') or '', args.get('member') or '',
                    # 安全边界：搜索永远限定在「提问的这个群」，杜绝把别的
                    # 群的消息混进本群回复（2026-10-29 实测混群事故）
                    scope_username=chat_id)
            else:
                out = f'未知工具：{name}'
        except Exception as e:
            out = f'工具错误：{type(e).__name__}: {e}'
        print(f'[工具] {name} {args} -> {len(out)} 字 '
              f'({time.time() - t0:.1f}s)', flush=True)
        return out

    # -- AI 资讯工具（aihot.news）-----------------------------------------

    def _ai_news(self, action: str, keyword: str = '',
                 category: str = '') -> str:
        """查 aihot.news 的 AI 资讯（get_ai_news 工具实现）。

        免费匿名 GET，失败只把原因回给模型，不影响其它链路。
        """
        action = (action or 'hot').strip().lower()

        def _get(path: str):
            req = urllib.request.Request(
                AIHOT_BASE + path, headers={'User-Agent': 'dafeiyu-bot/1.0'})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode('utf-8'))

        try:
            if action == 'hot':
                data = _get('/hot-topics')
                lines = [f"{it.get('rank')}. {it.get('title')}"
                         f"（{it.get('sourceCount', 0)} 个来源）"
                         for it in data.get('items', [])[:10] if it.get('title')]
                return ('当前 AI 热点榜：\n' + '\n'.join(lines)) if lines \
                    else '热榜暂时为空'
            if action == 'daily':
                rep = (_get('/dailies/latest').get('report') or {})
                out = [f"AI 日报 {rep.get('date', '')}"]
                lead = rep.get('lead') or {}
                if lead.get('title'):
                    out.append(f"头条：{lead['title']}")
                    if lead.get('leadParagraph'):
                        out.append(lead['leadParagraph'])
                for sec in rep.get('sections', []):
                    titles = [i.get('title', '') for i in sec.get('items', [])
                              if i.get('title')]
                    if titles:
                        out.append(f"{sec.get('label', '')}："
                                   + '；'.join(titles))
                return '\n'.join(out)[:4000] if len(out) > 1 \
                    else '日报还没生成（每天 08:00 后可查）'
            if action == 'latest':
                q = {'mode': 'selected', 'window': '24h', 'limit': 10}
                if keyword.strip():
                    q['q'] = keyword.strip()
                if category.strip() in AIHOT_CATEGORIES:
                    q['category'] = category.strip()
                data = _get('/items?' + urllib.parse.urlencode(q))
                lines = []
                for it in data.get('items', []):
                    line = f"- {it.get('title') or ''}"
                    s = (it.get('summary') or '').strip()
                    if s:
                        line += f'：{s[:120]}'
                    ts = (it.get('publishedAt') or '')[:16].replace('T', ' ')
                    if ts:
                        line += f'（{ts}）'
                    lines.append(line)
                return ('最近 24 小时 AI 精选：\n' + '\n'.join(lines)) \
                    if lines else '最近 24 小时没有精选动态'
            return f'未知 action：{action}（hot / latest / daily）'
        except Exception as e:
            return f'AI 资讯查询失败：{type(e).__name__}: {e}'

    # -- 群消息搜索工具 ---------------------------------------------------

    def _ensure_db(self):
        if self._db is None:
            from wechatauto.db import WeChatDB
            self._db = WeChatDB()
        return self._db

    def search_group_messages(self, keyword: str = '', member: str = '',
                              group: str = '',
                              scope_username: str = None) -> str:
        """搜索最近 SEARCH_DAYS 天的群聊消息，返回可读文本。

        keyword 匹配消息文本（只搜文本消息），member 匹配发送者昵称/备注/
        wxid。默认扫会话列表里所有群（命令行 --search 用）；bot 回话时传
        ``scope_username``=提问的会话，**只搜这一个群**，杜绝跨群内容漏进
        回复。命中超过 SEARCH_MAX_RESULTS 时保留最新的，返回文本超长时再
        按 SEARCH_RESULT_MAX_CHARS 掐头（都带说明，模型知道被截了）。
        """
        db = self._ensure_db()
        cutoff = time.time() - SEARCH_DAYS * 86400
        kw = (keyword or '').strip().lower()
        mb = (member or '').strip().lower()
        gp = (group or '').strip()

        if scope_username:
            disp = db.get_nickname(scope_username) or scope_username
            rooms = [(scope_username, disp)]
        else:
            rooms = []
            for s in db.get_sessions(limit=SEARCH_ROOM_LIMIT):
                # get_sessions 返回 dict（username/summary/last_time...）
                un = (s.get('username') or '') if isinstance(s, dict) \
                    else (getattr(s, 'username', '') or '')
                if not un.endswith('@chatroom'):
                    continue
                disp = db.get_nickname(un) or un
                if gp and gp not in disp and gp not in un:
                    continue
                rooms.append((un, disp))

        hits = []          # (群显示名, 原始行)
        for un, disp in rooms:
            offset = 0
            while offset < SEARCH_ROOM_PAGE_CAP:
                rows = db.get_messages(un, limit=SEARCH_PAGE, offset=offset)
                if not rows:
                    break
                older = False
                for r in rows:
                    ts = r.get('create_time') or 0
                    if not ts or ts < cutoff:
                        older = True
                        break
                    hits.append((disp, r))
                if older:
                    break
                offset += SEARCH_PAGE

        nick = db.nickname_map()
        out = []           # (时间, 群名, 发送者, 文本)
        for disp, r in hits:
            t = str(r.get('type') or '').lower()
            if t in ('text', '文本'):
                text = _strip_group_prefix(str(r.get('content') or '')).strip()
            else:
                text = f'[{r.get("type")}]'
                if kw:
                    continue          # 关键词只搜文本消息
            sun = r.get('sender_username') or ''
            sender = nick.get(sun) or sun or '未知'
            if mb and mb not in sender.lower() and mb not in sun.lower():
                continue
            if kw and kw not in text.lower():
                continue
            out.append((r.get('create_time') or 0, disp, sender, text))

        out.sort(key=lambda x: x[0])
        total = len(out)
        note = ''
        if total > SEARCH_MAX_RESULTS:
            out = out[-SEARCH_MAX_RESULTS:]
            note = f'（命中 {total} 条，仅保留最新 {SEARCH_MAX_RESULTS} 条）'
        lines = []
        for ts, disp, sender, text in out:
            tm = time.strftime('%m-%d %H:%M', time.localtime(ts))
            lines.append(f'[{tm}] {disp} | {sender}: {text[:100]}')
        body = '\n'.join(lines)
        if len(body) > SEARCH_RESULT_MAX_CHARS:
            keep, size = [], 0
            for ln in reversed(lines):
                if size + len(ln) + 1 > SEARCH_RESULT_MAX_CHARS:
                    break
                keep.append(ln)
                size += len(ln) + 1
            body = '\n'.join(reversed(keep))
            note += f'（结果过长，丢弃最旧 {len(lines) - len(keep)} 条）'
        head = (f'近{SEARCH_DAYS}天群消息搜索：keyword={kw or "-"} '
                f'member={mb or "-"} group={gp or "-"}；'
                f'扫了 {len(rooms)} 个群，命中 {total} 条{note}')
        return head + ('\n' + body if body else '\n（无命中）')

    # -- 微信回调 -------------------------------------------------------

    @staticmethod
    def _resolve_keyword_ad_at(chat, sender_wxid: str,
                               sender_name: str = '') -> tuple:
        """把触发消息的发送者解析成群里的唯一显示名。

        返回 ``(at_name, reason)``；失败时 at_name 为空，reason 是
        ``at_ambiguous`` / ``at_unresolvable`` / ``at_failed``。宁可跳过，
        也不猜一个可能 @ 错人的名字。
        """
        try:
            members = chat.GetGroupMembers() or []
        except Exception:
            return '', 'at_failed'
        if not members:
            return '', 'at_unresolvable'
        found = None
        for m in members:
            if str(m.get('username') or '') == str(sender_wxid or ''):
                found = m
                break
        if found is None:
            return '', 'at_unresolvable'
        name = str(found.get('remark') or found.get('nick_name')
                   or sender_name or '').strip()
        if not name:
            return '', 'at_unresolvable'
        names = [str(m.get('remark') or m.get('nick_name') or '').strip()
                 for m in members]
        if names.count(name) > 1:
            return '', 'at_ambiguous'
        return name, ''

    def _maybe_keyword_ad(self, msg, chat, text: str,
                          chat_id: str, where: str) -> None:
        """群消息命中关键词时自动发一条广告。

        与 @ 回答完全独立：不经过 LLM、不写对话记忆；同一群按随机冷却限流，
        失败也只记日志，绝不因广告链路把正常应答带崩。

        自己发的消息有三道过滤：调用方按 ``msg.attr == 'self'`` 过滤；
        这里再核对一次发送者 wxid；最后 ``Engine.is_echo`` 会把「刚发出去
        的广告原文原样出现」当回显忽略（发送者解析失败时 attr 可能是
        friend，这是兜底，防止自己触发自己）。
        """
        try:
            engine = self._keyword_ads
            # 有人 @ 机器人时走正常回答，不要在这条上再补一条广告
            for a in self.agents:
                if a['enabled'] and f'@{a["at_name"]}' in text:
                    return
            sender_wxid = str(getattr(msg, 'sender_wxid', '')
                              or getattr(msg, 'wxid', '') or '').strip()
            sender_name = str(getattr(msg, 'sender', '') or '').strip()
            db = getattr(chat, '_db', None)
            if db is None and self._pending_wx is not None:
                db = getattr(self._pending_wx, '_db', None)
            self_id = str(getattr(db, 'wxid', '') or '').strip() if db else ''
            if sender_wxid and self_id and sender_wxid == self_id:
                return
            if engine.is_echo(chat_id, text):
                return
            decision = engine.evaluate(text, chat_id, where, sender_name)
            if not decision.get('hit'):
                return
            kws = '、'.join(decision['keywords'])
            base = {
                'group_id': chat_id,
                'group_name': where,
                'sender': sender_name,
                'mode': decision.get('mode', ''),
                'keywords': kws,
                'text': text[:200],
            }
            reason = decision.get('reason')
            if not decision.get('allowed'):
                if reason == 'cooldown':
                    if engine.skip_log_allowed(chat_id):
                        print(f'[关键词广告] {where} 命中「{kws}」，冷却中，'
                              f'还剩 {decision["remaining"]}s', flush=True)
                        engine.log_hit({**base, 'reply': '',
                                        'result': 'cooldown'})
                elif reason == 'soft_no_intent':
                    if engine.skip_log_allowed(chat_id):
                        print(f'[关键词广告] {where} 命中「{kws}」，'
                              f'陈述句非提问，跳过', flush=True)
                        engine.log_hit({**base, 'reply': '',
                                        'result': 'soft_skip'})
                elif reason == 'probability_skip':
                    if engine.skip_log_allowed(chat_id):
                        print(f'[关键词广告] {where} 命中「{kws}」，'
                              f'本次概率跳过', flush=True)
                        engine.log_hit({**base, 'reply': decision.get('reply', ''),
                                        'result': 'probability_skip'})
                elif reason == 'reply_empty':
                    print(f'[关键词广告] {where} 命中「{kws}」，但广告文案为空，'
                          f'未发送', flush=True)
                    engine.log_hit({**base, 'reply': '',
                                    'result': 'reply_empty'})
                return
            reply = decision['reply']
            at_name, at_reason = self._resolve_keyword_ad_at(
                chat, sender_wxid, sender_name)
            if not at_name:
                # 这轮不 @ 就不发；仍占冷却，避免每条命中都反复尝试同一个人
                engine.note_sent(chat_id, reply)
                print(f'[关键词广告] {where} 命中「{kws}」，'
                      f'无法确定 @ 对象（{at_reason}），未发送', flush=True)
                engine.log_hit({**base, 'reply': '',
                                'result': at_reason or 'at_failed',
                                'error': '无法确定@对象'})
                return
            pre_delay = max(0.0, float(decision.get('pre_delay') or 0))
            if pre_delay:
                time.sleep(pre_delay)
            # 先占冷却再发：发送失败也不让下一条命中消息继续撞卡住的窗口
            engine.note_sent(chat_id, reply)
            t0 = time.time()
            ok, err = self._send_retry(
                lambda: chat.SendMsg(reply, at=at_name, verify=True),
                '关键词广告', retries=0)
            typing_ms = int((time.time() - t0) * 1000)
            print(f'[关键词广告] {where} 命中「{kws}」-> '
                  f'{"已发送" if ok else err} ({typing_ms / 1000:.1f}s)',
                  flush=True)
            engine.log_hit({**base, 'at_name': at_name,
                            'typing_ms': typing_ms, 'reply': reply,
                            'result': 'sent' if ok else 'failed',
                            'error': (err or '')[:200]})
        except Exception:
            import traceback
            print('[关键词广告] 处理异常\n' + traceback.format_exc(),
                  flush=True)

    def on_message(self, msg, chat) -> None:
        try:
            if self.paused:
                return                      # 已因发送失败暂停：只听不回，等人工重启
            self._record(msg, chat)         # 什么消息都先进围观记录
            # 每条到达的消息都留一行（不回答的也留）：排查「监听是否在跑」
            # 全靠它，否则日志里除了被 @ 的那几次什么都看不见
            _who = getattr(msg, 'sender', '') or '有人'
            _where = getattr(chat, 'nickname', '') or getattr(chat, 'who', '')
            _text = (_strip_group_prefix(msg.content or '') if msg.type in ('text', '文本')
                     else f'[{msg.type}]')
            # 用「消息」而非「收到」：后台按 [收到]/[回复] 配对对话记录，
            # 普通消息也用「收到」会让它们占位成待回复，把真对话配错
            print(f'[消息] {_where} | {_who}: {_text[:60]!r}', flush=True)
            if getattr(msg, 'attr', '') == 'self':
                return                      # 自己（包括机器人本人）发的不管
            if msg.type not in ('text', '文本'):
                return                      # 只处理文本消息
            text = _strip_group_prefix(msg.content or '').strip()
            chat_id = getattr(chat, 'who', '')
            where = getattr(chat, 'nickname', '') or chat_id
            # 关键词广告：放在 route 之前，没被任何 Agent 认领的群也能生效；
            # 它和 @ 触发互相独立，命中也不影响后面要不要回答
            if chat_id.endswith('@chatroom'):
                self._maybe_keyword_ad(msg, chat, text, chat_id, where)
            # 按群找负责的 Agent；没有 Agent 认领的群直接不理
            agent = self.route(chat_id, where)
            if agent is None:
                return
            trigger = f'@{agent["at_name"]}'
            is_group = chat_id.endswith('@chatroom')
            if is_group:
                # 群聊只认 @：没被 @ 的一律不答（围观记录里已经有了，供上下文用）
                if trigger not in text:
                    return
                # 去掉 @前缀（微信 @ 后面常跟 U+2005 特殊空格）
                question = text.split(trigger, 1)[1]
                question = question.lstrip('\u2005\u2004\u2003 ').strip()
            else:
                # 私聊没法 @ 人，对方发来的每句话都是在跟机器人说话：
                # 去掉可选的 @前缀后整句当问题
                question = text
                if question.startswith(trigger):
                    question = question[len(trigger):]
                question = question.lstrip('\u2005\u2004\u2003 ').strip()
            if not question:
                question = '在吗？'
            recent = self._recent_log(chat_id, agent['recent_n'])

            who = getattr(msg, 'sender', '') or '有人'
            # 「<Agent>」前缀给管理页按 Agent 统计对话用
            print(f'[收到] <{agent["name"]}> {where} | {who}: {text!r}', flush=True)
            _ctx.chat_id, _ctx.chat_name, _ctx.agent = chat_id, where, agent['id']

            t0 = time.time()
            try:
                answer, at = self.think(chat_id, question, recent, who=who,
                                        chat=chat, agent=agent)
            except Exception as e:
                print(f'[llm] 全部节点失败：{e}', flush=True)
                answer, at = FAIL_REPLY, None
            print(f'[回复] ({time.time() - t0:.1f}s) {answer!r}'
                  f'{" [@%s]" % at if at else ""}', flush=True)

            if not answer:
                print('[回复] 媒体已发出，本轮无文字', flush=True)
                return
            if chat_id.endswith('@chatroom') and who and who != '有人':
                # @ 一律用纯文本前缀（不做真提及）：成员弹层点选实战全灭——
                # 花体名 OCR 认不出、大群弹层一屏放不下，还弄坏过输入框
                at = at or who
                answer = f'@{at} {answer}'
            self._queue_reply(msg, chat, answer, agent['id'])
            self._flush_pending_replies()
        except Exception:
            import traceback
            print('[回调异常]\n' + traceback.format_exc(), flush=True)

    # -- 启停 -----------------------------------------------------------

    @staticmethod
    def _enum_wechat_windows():
        """枚举微信顶层窗口，返回 [(hwnd, score, area, visible), ...]。

        score 是两个特征的加权分：类名以 Qt 开头 +4、标题含「微信/Weixin/WeChat」
        +5。调用方按需挑——找可见主窗口要 9 分（两个特征都中），找收进托盘的
        窗口只要 >0（微信改版时常常一个特征先失效）。判定口径与
        ``wechatauto.guia._find_main_window`` 保持一致，避免又多出第三套。
        """
        import ctypes
        from ctypes import wintypes

        u = ctypes.windll.user32
        out = []

        def _cb(h, lp):
            buf = ctypes.create_unicode_buffer(256)
            cls = ctypes.create_unicode_buffer(256)
            u.GetWindowTextW(h, buf, 256)
            u.GetClassNameW(h, cls, 256)
            title, cls_name = buf.value.strip(), cls.value
            score = 0
            if cls_name.startswith('Qt'):
                score += 4
            if any(k in title for k in ('微信', 'Weixin', 'WeChat')):
                score += 5
            if not score:
                return True
            r = wintypes.RECT()
            u.GetWindowRect(h, ctypes.byref(r))
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            if area > 0:
                out.append((h, score, area, bool(u.IsWindowVisible(h))))
            return True

        CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND,
                                wintypes.LPARAM)
        u.EnumWindows(CB(_cb), 0)
        return out

    @staticmethod
    def _restore_wechat_window() -> bool:
        """把收进托盘（不可见）的微信主窗口还原出来。

        WeChatGUI 定位主窗口只认可见顶层窗口，微信一托盘化启动就报
        「未找到微信主窗口」。这里把得分最高的不可见微信窗口 ShowWindow
        还原出来。只要 score>0 就认（不强求两个特征都中），微信改版后
        标题或类名之一变了也还能捞回来。
        """
        import ctypes

        best = None               # (hwnd, score, area)
        for hwnd, score, area, visible in DaFeiYuBot._enum_wechat_windows():
            if visible:
                continue          # 可见窗口轮不到我们管
            if best is None or (score, area) > (best[1], best[2]):
                best = (hwnd, score, area)
        if not best:
            return False
        ctypes.windll.user32.ShowWindow(best[0], 9)     # SW_RESTORE
        time.sleep(1.5)                        # 等窗口完成渲染再交回 WeChat()
        print(f'[启动] 已还原微信主窗口（hwnd={best[0]}）', flush=True)
        return True

    # -- 窗口固定到指定显示器 ------------------------------------------------
    # PnP 显示设备探测缓存 {keyword, status, ts}。查询走 PowerShell（新进程
    # + 整机显示设备枚举），屏长期不可用时不能被反复触发，见
    # ``_enable_display_device``。
    _dev_probe_cache = {}

    @staticmethod
    def _display_rect(keyword: str):
        """按 DeviceString 子串找显示器，返回它在虚拟桌面里的 (x, y, w, h)。

        用 pywin32 的 EnumDisplayDevices / EnumDisplaySettings 读位置和尺寸，
        不再自己声明 DISPLAY_DEVICEW / DEVMODEW（pyproject 已声明 pywin32，
        项目里十来个模块都在用它）。只认 StateFlags 带 ATTACHED_TO_DESKTOP
        的条目：没接上的虚拟显示器 EnumDisplaySettings 会抛 126，跳过即可。
        找不到返回 None。
        """
        import win32api

        kw = keyword.lower()
        i = 0
        while True:
            try:
                dev = win32api.EnumDisplayDevices(None, i)
            except Exception:
                return None
            i += 1
            if not dev.DeviceString:
                return None
            if kw not in dev.DeviceString.lower():
                continue
            if not (dev.StateFlags & 1):   # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
                continue
            try:
                s = win32api.EnumDisplaySettings(dev.DeviceName, -1)
            except Exception:
                continue
            return (s.Position_x, s.Position_y, s.PelsWidth, s.PelsHeight)

    @staticmethod
    def _find_wechat_window() -> int:
        """找可见的微信主窗口句柄，找不到返回 0。

        只认 score 满值（类名 Qt* + 标题含微信两个特征都中）的可见窗口，
        取面积最大的那个——避免把某个标题恰好含「微信」的其它窗口当成主窗口。
        """
        best = None               # (hwnd, area)
        for hwnd, score, area, visible in DaFeiYuBot._enum_wechat_windows():
            if not visible or score < 9:
                continue
            if best is None or area > best[1]:
                best = (hwnd, area)
        return best[0] if best else 0

    @staticmethod
    def _enable_display_device(keyword: str) -> bool:
        """按显示器名字反查并启用对应的 PnP 显示设备。

        有些情况下虚拟屏不是被踢出拓扑，而是 PnP 设备本身被禁用（远程工具
        断开时会这么做）。这时 ``SetDisplayConfig`` 返回 87，因为那块屏在
        系统里根本不存在；必须先用 SetupAPI 把设备启用。

        用 ``Get-PnpDevice`` + ``Enable-PnpDevice`` 走 PowerShell——纯粹为了
        避免在这一处再引一套 cfgmgr32 绑定，启用设备本身就是低频操作。

        **这个函数很贵**：每次都要新建一个 PowerShell 进程（5.1 启动约几百
        毫秒到几秒，峰值内存上百 MB），``Get-PnpDevice`` 还要枚举整机显示设备。
        看门狗在屏长期不可用时每轮都调它，正是把系统拖死的来源之一。所以这里
        带一层探测缓存：``WATCH_DEVICE_CACHE_TTL`` 秒内查过且状态为 OK 的直接
        返回，不再起进程——设备状态是 OK 说明它没被禁用，重复查没有信息量。

        返回是否成功启用（或本来就是启用状态）。
        """
        import subprocess

        cache = DaFeiYuBot._dev_probe_cache
        if cache.get('keyword') == keyword \
                and cache.get('status') == 'OK' \
                and time.time() - cache.get('ts', 0.0) < WATCH_DEVICE_CACHE_TTL:
            return True

        # DeviceString（如 'IddSampleDriver'）和 PnP 的 FriendlyName 不完全一样：
        # 后者带后缀（'IddSampleDriver Device'），所以用 -like 通配匹配。
        ps = (
            "$ErrorActionPreference='Stop';"
            f"$d = Get-PnpDevice -Class Display -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.FriendlyName -like '*{keyword}*' }};"
            "if (-not $d) { 'NOTFOUND'; exit 0 };"
            "$d | ForEach-Object {"
            "  if ($_.Status -ne 'OK') {"
            "    Enable-PnpDevice -InstanceId $_.InstanceId -Confirm:$false "
            "      -ErrorAction SilentlyContinue | Out-Null"
            "  }"
            "  $_.InstanceId + '|' + "
            "    (Get-PnpDevice -InstanceId $_.InstanceId).Status"
            "}"
        )
        try:
            out = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                                 capture_output=True, text=True, timeout=60)
        except Exception as e:
            print(f'[显示] 启用设备失败：{type(e).__name__}: {e}', flush=True)
            return False
        text = (out.stdout or '').strip()
        if 'NOTFOUND' in text or not text:
            print(f'[显示] 没找到名字含 {keyword!r} 的显示设备', flush=True)
            DaFeiYuBot._dev_probe_cache = {'keyword': keyword, 'status': '',
                                           'ts': time.time()}
            return False
        print(f'[显示] 设备状态：{text}', flush=True)
        ok = '|OK' in text
        DaFeiYuBot._dev_probe_cache = {
            'keyword': keyword,
            'status': 'OK' if ok else text.rsplit('|', 1)[-1],
            'ts': time.time(),
        }
        return ok

    @staticmethod
    def _reenable_display(keyword: str) -> str:
        """把被远程软件挤掉的虚拟显示器重新挂回扩展桌面。

        背景（实测）：UU 远程 / GameViewer 这类工具建连、断连时会重建显示
        拓扑，把 ROOT\\DISPLAY\\* 这类虚拟显示器排出去。这时
        `_display_rect()` 返回 None，微信窗口无处可去。

        实测可用的做法只有一条：`SetDisplayConfig(0, NULL, 0, NULL,
        SDC_TOPOLOGY_EXTEND | SDC_APPLY)` 返回 0 并把它拉回来。

        **不能带 `SDC_SAVE_TO_DATABASE` 或 `SDC_ALLOW_CHANGES`** —— 加上
        任一个，同一个调用就变成 87 (ERROR_INVALID_PARAMETER)，而这条拓扑
        本身是合法的（不带就是 0）。这是踩过的坑，不是估计。

        还要注意：设备被禁用再启用后 GDI 编号会变（实测 DISPLAY97 →
        DISPLAY98），所以这里不认 GDI 设备名，只靠 DeviceString 重新定位。

        返回状态字符串，调用方据此决定退避节奏：

        - ``'ok'``     —— 屏已经回来；
        - ``'denied'`` —— 会话无权改显示拓扑（``rc=5`` / ERROR_ACCESS_DENIED）。
          锁屏、UU/GameViewer 断开期间会话处于不可写状态，这类失败重试多少次
          都不会成功，调用方应当直接退到长间隔；
        - ``'failed'`` —— 其他失败（设备真被禁用且启用也没救回来等）。
        """
        import ctypes

        u = ctypes.windll.user32
        # 64 位下不声明 argtypes 会把指针按 32 位整数传，一律 87。
        u.SetDisplayConfig.argtypes = [ctypes.c_uint, ctypes.c_void_p,
                                       ctypes.c_uint, ctypes.c_void_p,
                                       ctypes.c_uint]
        u.SetDisplayConfig.restype = ctypes.c_long
        SDC_TOPOLOGY_EXTEND = 0x4
        SDC_APPLY = 0x80
        rc = u.SetDisplayConfig(0, None, 0, None,
                                SDC_TOPOLOGY_EXTEND | SDC_APPLY)
        denied = (rc == 5)             # ERROR_ACCESS_DENIED
        if rc != 0:
            print(f'[显示] 扩展拓扑 rc={rc}' +
                  ('（会话不可写，跳过后续重试）' if denied else ''),
                  flush=True)

        def appeared() -> bool:
            """轮询等目标屏进入活动桌面——拓扑切换是异步的。

            判据复用 `_display_rect()`：它已经封装了 EnumDisplayDevices、
            DeviceString 子串匹配和 ATTACHED_TO_DESKTOP 校验，这里只补一个
            外层轮询，不重复实现那套枚举。
            """
            for _ in range(8):             # 最多等 4 秒：实测拓扑切换 1~2 秒就落定
                if DaFeiYuBot._display_rect(keyword):
                    return True
                time.sleep(0.5)
            return False

        if appeared():
            return 'ok'

        # 权限都没有，启用设备同样会被拒——别再起 PowerShell 白烧资源。
        if denied:
            return 'denied'

        # 到这儿说明扩展拓扑没把它带回来。**不能按 rc 判断要不要救**：
        # 实测设备被 PnP 禁用时 SetDisplayConfig 照样返回 0，只是拓扑里
        # 没有这块屏——所以只有「屏真的没出现」才去启用设备。
        print(f'[显示] 扩展拓扑后仍未出现 {keyword!r}，尝试启用显示设备',
              flush=True)
        if not DaFeiYuBot._enable_display_device(keyword):
            return 'failed'
        # 设备刚启用不会自动进桌面，再扩展一次拓扑把它纳进来
        u.SetDisplayConfig(0, None, 0, None, SDC_TOPOLOGY_EXTEND | SDC_APPLY)
        return 'ok' if appeared() else 'failed'

    def _send_watchdog(self) -> None:
        """发送卡死看门狗：超时整进程退出，由管理页/一键启动脚本重新拉起。

        为什么监控线程拦得住：UIA 卡在不返回的一次调用里时，另一条 Python
        线程照常运行（实测 time.sleep / 纯 Python 忙循环 / ctypes native 阻塞
        三种情况都能按 0.5s 走心跳），而 uiautomation 的遍历是「纯 Python 循环
        + 每次一个 COM 调用」，两条路都让出 GIL。

        为什么只能整进程退出而不是放弃这一条：卡住的调用不会返回，会一直
        占着 send_lock，后面所有回复一起堵死；进程内无法中断它（跨线程 join
        会死锁，已实测）。
        """
        limit = max(5.0, SEND_HANG_SECONDS)
        while True:
            time.sleep(5)
            if self._watchdog_exit or not self._send_started_at:
                continue
            now = time.time()
            if not _send_hang_exceeded(self._send_started_at, now, limit):
                continue
            time.sleep(3)                       # 二次确认，排除采样抖动
            if self._watchdog_exit:
                continue
            now = time.time()
            if not _send_hang_exceeded(self._send_started_at, now, limit):
                continue
            held = now - self._send_started_at
            self._watchdog_exit = True
            print(f'[看门狗] 发送已卡住 {held:.0f}s（上限 {limit:.0f}s），'
                  f'退出进程等待重启；待发答案已在 pending_replies.json，'
                  f'重启后会先核出站记录再补发', flush=True)
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except Exception:
                    pass
            os._exit(WATCHDOG_EXIT_CODE)

    def _display_watchdog(self, wx) -> None:
        """盯着目标显示器在不在，掉了就重新挂回来，再把窗口搬回去。

        只在后台线程跑，只读地枚举显示器；发现目标屏不见了才动手。没有这一
        层的话，UU 远程断开后虚拟屏会一直缺席，直到下次手动跑 DisplaySwitch，
        期间微信窗口被系统挤到主屏一角、甚至最小化成 -32000。

        **节奏必须退避，不能每 10 秒重修一次**。修复动作本身很重：
        ``SetDisplayConfig`` 会重建整个显示拓扑（DWM 重排所有窗口），
        ``Enable-PnpDevice`` 会启一个 PowerShell 并触发 PnP 重扫设备。实测
        在一次 UU 断开后，屏被拓扑彻底排出去、``SetDisplayConfig`` 持续返回
        rc=5（会话不可写），看门狗仍每 10 秒重建一次拓扑，几分钟内就把整台
        机器顶到卡死。现在的规则：

        - 屏在时：``WATCH_INTERVAL`` 复查一次，成本是几次 GDI 枚举；
        - 屏不在：每失败一次间隔翻倍（10 → 20 → … → 300 秒）；
        - ``rc=5``：直接退到最长间隔（会话不可写，重试没有意义）；
        - 连续失败 ``WATCH_GIVEUP_AFTER`` 次：**完全停止修复动作**，只做
          只读检测（成本低），屏一旦自己回来立刻把窗口搬进去。

        这个线程不碰聊天内容，也不结束 / 重启微信；恢复不了就只打日志。
        """
        keyword = _display_target()
        if not keyword:
            return                          # 虚拟屏关着/没配目标屏，不干预
        misses = 0                          # 屏缺席的检测次数
        fails = 0                           # 连续修复失败次数
        interval = WATCH_INTERVAL
        while True:
            time.sleep(interval)
            try:
                if self._display_rect(keyword):
                    if misses:
                        print(f'[显示] {keyword} 已恢复（中断 {misses} 次检测）',
                              flush=True)
                        misses = fails = 0
                        interval = WATCH_INTERVAL
                        self._ensure_window_on_display()   # 窗口搬回去
                    continue

                misses += 1

                # 熔断：连续修不好就别再碰系统 API 了。只读检测照样跑，
                # 屏回来（用户重连远程/手动启用）时下一轮就能发现。
                if fails >= WATCH_GIVEUP_AFTER:
                    if misses in (1, 30, 300):   # 别刷屏
                        print(f'[显示] {keyword} 仍不在；已放弃自动修复'
                              f'（连续失败 {fails} 次），只做只读检测',
                              flush=True)
                    interval = WATCH_MAX_INTERVAL
                    continue

                print(f'[显示] {keyword} 不在了（第 {misses} 次检测），'
                      f'尝试重新纳入扩展桌面…', flush=True)
                status = self._reenable_display(keyword)
                if status == 'ok':
                    print(f'[显示] {keyword} 已重新挂上，搬回微信窗口', flush=True)
                    misses = fails = 0
                    interval = WATCH_INTERVAL
                    self._ensure_window_on_display(force=True)
                    continue

                fails += 1
                if status == 'denied':
                    # 会话不可写（锁屏/远程断开）：重试无意义，但别等太久——
                    # 用户重新连上远程后会话很快就可写，退避 300 秒会让
                    # 这段时间发来的消息全部跳过。
                    interval = WATCH_DENIED_INTERVAL
                    print(f'[显示] 当前会话无权修改显示拓扑，'
                          f'{interval:.0f}s 后再试', flush=True)
                else:
                    interval = min(interval * 2, WATCH_MAX_INTERVAL)
                    if fails in (1, 3, 10):
                        print(f'[显示] 修复失败 {fails} 次，'
                              f'{interval:.0f}s 后重试', flush=True)
            except Exception as e:          # 看门狗本身绝不能把主循环带崩
                print(f'[显示] 巡检异常：{type(e).__name__}: {e}', flush=True)
                interval = min(interval * 2, WATCH_MAX_INTERVAL)

    @staticmethod
    def _active_displays() -> list:
        """枚举当前挂在桌面上的显示器：[(设备描述, (左, 上, 右, 下)), ...]。

        复用 ``_display_rect()`` 的 pywin32 枚举方式，不再自己声明
        DISPLAY_DEVICEW / DEVMODEW。只收 ATTACHED_TO_DESKTOP 的设备；
        查询失败返回空列表，调用方据此不做判断。
        """
        import win32api

        out = []
        i = 0
        while True:
            try:
                dev = win32api.EnumDisplayDevices(None, i)
            except Exception:
                break
            i += 1
            if not dev.DeviceString:
                break
            if not (dev.StateFlags & 1):   # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
                continue
            try:
                s = win32api.EnumDisplaySettings(dev.DeviceName, -1)
            except Exception:
                continue
            out.append((dev.DeviceString,
                        (s.Position_x, s.Position_y,
                         s.Position_x + s.PelsWidth,
                         s.Position_y + s.PelsHeight)))
        return out

    def _recover_window_to_primary(self) -> bool:
        """关闭虚拟屏开关后，启动时把留在虚拟屏/屏幕外的微信窗口拉回主屏一次。

        开关关着的语义是「机器人不再持续干预窗口位置」，但如果上一次运行时
        窗口被留在了虚拟显示器上（比如刚关掉开关），用户会完全看不到微信。
        这里只在启动阶段恢复一次：已经落在物理屏（含第二块物理屏）上的窗口
        一律不动；恢复后发送路径也不会再搬它。
        """
        if _display_target():
            return False                       # 开关开着，走原有的固定逻辑
        import ctypes
        from ctypes import wintypes

        hwnd = self._find_wechat_window()
        if not hwnd:
            return False
        u = ctypes.windll.user32
        r = wintypes.RECT()
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)              # SW_RESTORE
            time.sleep(0.3)
        u.GetWindowRect(hwnd, ctypes.byref(r))
        if not _needs_primary_recovery(
                (r.left, r.top, r.right, r.bottom), self._active_displays()):
            return False
        work = wintypes.RECT()
        if not u.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):
            return False                       # SPI_GETWORKAREA 失败就不猜
        ww, wh = work.right - work.left, work.bottom - work.top
        if ww <= 0 or wh <= 0:
            return False
        w, h = r.right - r.left, r.bottom - r.top
        if w < MIN_WIN_W or h < MIN_WIN_H:
            w, h = DEFAULT_WIN_W, DEFAULT_WIN_H
        w, h = min(w, ww - 40), min(h, wh - 40)
        x = work.left + max(0, (ww - w) // 2)
        y = work.top + max(0, (wh - h) // 2)
        ok = u.SetWindowPos(hwnd, None, x, y, w, h, 0x0014)  # NOZORDER|NOACTIVATE
        print(f'[窗口] 虚拟屏开关已关闭，微信窗口仍在虚拟屏/屏幕外，'
              f'已拉回主屏 ({x},{y}) {w}x{h} -> {bool(ok)}', flush=True)
        return bool(ok)

    def _ensure_window_on_display(self, force: bool = False) -> bool:
        """把微信主窗口校正到可用的位置，别让它留在屏幕外。

        两步，顺序不能反：

        1. **先解最小化**。窗口最小化时 `GetWindowRect` 返回
           `(-32000, -32000)`，这时候 `SetWindowPos` 也改不动位置（Windows
           忽略对最小化窗口的移动），必须先 `SW_RESTORE`。漏了这一步的表现
           是发送第一次必定「屏幕截图失败」，等 guia 内部恢复窗口后才成功。
        2. **再看目标显示器**。`WECHAT_DISPLAY` 指定的屏（如 IddSampleDriver）
           不一定在——远程软件接管显示输出时会把虚拟屏挤掉。找不到就退化成
           「留在主屏可见区域」，保证窗口至少能用，而不是把它留在屏幕外。

        已经完整落在目标区域时什么都不做，不打断用户。返回最终是否到位。
        """
        import ctypes
        from ctypes import wintypes

        hwnd = self._find_wechat_window()
        if not hwnd:
            return False
        u = ctypes.windll.user32
        r = wintypes.RECT()

        # 1. 最小化窗口先还原，否则后面读到的坐标是 -32000 那个哨兵值
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, 9)          # SW_RESTORE
            time.sleep(0.3)

        keyword = _display_target()
        rect = None
        if keyword:
            rect = self._display_rect(keyword)
            if not rect:
                print(f'[窗口] 没找到含 {keyword!r} 的显示器'
                      f'（远程桌面/串流软件常会挤掉虚拟屏）；'
                      f'改为把微信留在主屏可见区域', flush=True)

        if rect:
            dx, dy, dw, dh = rect
            u.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            inside = (r.left >= dx and r.top >= dy
                      and r.right <= dx + dw and r.bottom <= dy + dh)
            if inside and not force:
                return True
            if u.IsZoomed(hwnd):
                u.ShowWindow(hwnd, 9)      # SW_RESTORE
                time.sleep(0.2)
                u.GetWindowRect(hwnd, ctypes.byref(r))
                w, h = r.right - r.left, r.bottom - r.top
            if w < MIN_WIN_W or h < MIN_WIN_H:
                # 断屏/远程事件会把窗口挤成 160x28 这类碎片尺寸。**绝不能
                # 沿用**：SetWindowPos 会把坏尺寸固化下来，之后 guia 在这么小
                # 的窗口里找不到输入框，SendMsg 会卡到超时（实测卡了一分半
                # 还没任何输出）。这里一律恢复成默认尺寸。
                print(f'[窗口] 当前 {w}x{h} 过小（断屏遗留），'
                      f'恢复为 {DEFAULT_WIN_W}x{DEFAULT_WIN_H}', flush=True)
                w, h = DEFAULT_WIN_W, DEFAULT_WIN_H
            nw = min(w, dw - 80) if w > dw - 80 else w
            nh = min(h, dh - 60) if h > dh - 60 else h
            nx = dx + 20 if (w > dw - 80 or not inside) else r.left
            ny = dy + 20 if (h > dh - 60 or not inside) else r.top
            ok = u.SetWindowPos(hwnd, None, nx, ny, nw, nh, 0x0004)  # SWP_NOZORDER
            print(f'[窗口] 微信移入 {keyword}：({nx},{ny}) {nw}x{nh} -> {bool(ok)}',
                  flush=True)
            time.sleep(0.3)
            u.GetWindowRect(hwnd, ctypes.byref(r))
            return (r.left >= dx and r.top >= dy
                    and r.right <= dx + dw and r.bottom <= dy + dh)

        # 2. 没有目标屏
        if not WECHAT_DISPLAY_ENABLED:
            # 开关关着 = 完全不干预窗口位置。上面解最小化那一步要保留
            # （窗口最小化时根本发不出消息），位置一律不动——用户拖到哪就是哪。
            return True
        # 开关开着但目标屏不在：保证窗口在可见桌面内。范围必须用**虚拟桌面**
        # （所有扩展屏的并集），GetSystemMetrics(0) 只有主屏宽度，会把虚拟屏/
        # 右侧扩展屏上的窗口误判成「在屏幕外」然后硬搬到主屏（2026-11-14 实测踩到）。
        u.GetWindowRect(hwnd, ctypes.byref(r))
        vx = u.GetSystemMetrics(76)         # SM_XVIRTUALSCREEN
        vy = u.GetSystemMetrics(77)         # SM_YVIRTUALSCREEN
        vw = u.GetSystemMetrics(78)         # SM_CXVIRTUALSCREEN
        vh = u.GetSystemMetrics(79)         # SM_CYVIRTUALSCREEN
        on_screen = (r.right > vx and r.bottom > vy
                     and r.left < vx + vw and r.top < vy + vh)
        if on_screen and not force:
            return True
        if u.IsZoomed(hwnd):
            u.ShowWindow(hwnd, 9)
            time.sleep(0.2)
            u.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w < MIN_WIN_W or h < MIN_WIN_H:
            print(f'[窗口] 当前 {w}x{h} 过小（断屏遗留），'
                  f'恢复为 {DEFAULT_WIN_W}x{DEFAULT_WIN_H}', flush=True)
            w, h = DEFAULT_WIN_W, DEFAULT_WIN_H
        nw, nh = min(w, vw - 80), min(h, vh - 60)
        ok = u.SetWindowPos(hwnd, None, vx + 20, vy + 20, nw, nh, 0x0004)
        print(f'[窗口] 目标屏不可用，微信拉回可见桌面 '
              f'({vx + 20},{vy + 20}) {nw}x{nh} -> {bool(ok)}', flush=True)
        return bool(ok)

    def _warmup_gui(self, wx) -> None:
        """后台预热 GUI 布局，把首次发送的 1~2 分钟挪到启动阶段。

        第一次 ``get_input_box()`` 要初始化 UIA，并在窗口尺寸与
        ``~/.wechatauto/layout-*.json`` 缓存不符时重校准布局——实测 122 秒
        （6 次探测各约 12 秒 + 校准 12 秒，之后稳定在 0.1 秒）。放在发送
        路径上就是「机器人重启后第一条回复特别慢」。

        只读操作，不改窗口内容；失败只打一行日志，不影响正常收发。
        """
        try:
            gui = getattr(wx, '_gui', None)
            if gui is None:
                return
            t0 = time.time()
            box = gui.get_input_box()
            print(f'[预热] 输入框定位 {box}（{time.time() - t0:.1f}s）',
                  flush=True)
        except Exception as e:
            print(f'[预热] 跳过：{type(e).__name__}: {e}', flush=True)

    def _log_db_status(self) -> None:
        """启动时打一条数据库自检：账号、解密缓存、会话数、密钥来源。

        没有这条，日志里只有「已上线」，根本看不出读库到底通没通——
        排查时要自己去翻 %TEMP% 才知道。失败只打警告，不阻断启动
        （发消息走 GUI，读库坏了仍能靠围观记录回话）。
        """
        try:
            db = self._ensure_db()
            sessions = db.get_sessions(limit=5)
            total = db.get_sessions(limit=10000)
            account = getattr(db, 'account', '') or '（未知）'
            print(f'[db] 数据库已解密：账号 {account}，会话 {len(total)} 个', flush=True)
            for s in sessions[:3]:
                un = (s.get('username') or '') if isinstance(s, dict) \
                    else (getattr(s, 'username', '') or '')
                print(f'[db]   例：{un} | {(db.get_nickname(un) or "")[:24]}',
                      flush=True)
        except Exception as e:
            print(f'[db] 数据库读取失败（{type(e).__name__}: {e}）；'
                  f'读消息/群搜索工具不可用，按 @ 回话仍可工作', flush=True)

    @staticmethod
    def _check_duplicate_instance() -> None:
        """同一台机器只允许跑一个机器人实例，发现重复直接退出。

        两个实例并存的代价很大，实测过：同时监听消息会重复回复；同时
        驱动微信窗口会抢焦点、截图全黑；各自跑显示看门狗会叠加重建
        显示拓扑（见 ``_display_watchdog`` 的说明），把整台机器顶卡。

        扫描复用 ``admin.server.find_bots()``：管理后台的「启动/停止」判定
        和这里的重复检查必须是同一套谓词，各写一份就会出现「管理页说没在
        跑、机器人自己却认为有实例」这种自相矛盾。查询失败不阻止启动——
        查不到进程不等于有重复。
        """
        try:
            from admin.server import find_bots
            dup = [p.pid for p in find_bots(exclude_pid=os.getpid())]
            if dup:
                print(f'[启动] 已有机器人实例在跑（PID {dup}），本次退出。'
                      f'两个实例会抢微信窗口、重复回复、叠加显示看门狗，'
                      f'先把旧实例停掉再启动。', flush=True)
                sys.exit(1)
        except SystemExit:
            raise
        except Exception as e:
            print(f'[启动] 重复实例检查跳过：{type(e).__name__}: {e}',
                  flush=True)

    def run(self) -> None:
        self._check_duplicate_instance()
        # 先把窗口尺寸/位置校正到目标屏，再初始化 GUI。
        # GUI 构造时会按当时窗口尺寸做布局校准；断屏事件常把窗口挤成
        # 160x28 / 296x388 这类碎片尺寸，在那个尺寸上校准出来的档位没用，
        # 移到正常尺寸后又得重校准一次（实测多花十几秒，且两次校准的
        # origin/比例会短暂不一致，第一次发送容易截图全黑）。
        self._ensure_window_on_display()
        # 虚拟屏开关关着时，把上次留在虚拟屏/屏幕外的窗口拉回主屏一次。
        # 必须在 WeChat() 之前做：GUI 构造时按窗口尺寸校准布局，
        # 先搬窗口再校准，避免搬动后布局档位对不上。
        self._recover_window_to_primary()
        try:
            wx = WeChat()
        except RuntimeError as e:
            # 微信收进托盘时主窗口不可见——自动还原一次再试，不再直接失败
            if '未找到微信主窗口' not in str(e) \
                    or not self._restore_wechat_window():
                raise
            self._recover_window_to_primary()   # 托盘还原后再判断一次位置
            wx = WeChat()
        # 再校正一次：WeChat() 初始化过程可能把窗口抢到前台或改回原尺寸
        if not self._ensure_window_on_display() and _display_target():
            print(f'[窗口] 微信未能移到 {_display_target()!r}，'
                  f'发送仍可用但窗口会留在原屏', flush=True)
        self._pending_wx = wx
        r = wx.AddListenAll(self.on_message)
        if r is not None and hasattr(r, 'is_success') and not r.is_success:
            print(f'开启全局监听失败：{r}', flush=True)
            return
        self._log_db_status()
        wx.StartListening()
        # 后端预热：首次 get_input_box 要 1~2 分钟（UIA 初始化 + 布局重校准），
        # 放后台跑，别让重启后的第一条回复替它等
        threading.Thread(target=self._warmup_gui, args=(wx,),
                         daemon=True).start()
        # 显示看门狗：UU 远程断连会把虚拟屏踢出桌面，这里每 10 秒复查一次，
        # 掉了就用 SetDisplayConfig 拉回扩展桌面，再把微信窗口搬回去
        threading.Thread(target=self._display_watchdog, args=(wx,),
                         daemon=True).start()
        # 发送卡死看门狗：UIA 调用卡住时进程内中断不了，只能整进程退出重来
        threading.Thread(target=self._send_watchdog, daemon=True).start()
        # 群发任务：到点由本进程发（只有机器人持有微信窗口）。
        # lock 传 None——_broadcast_send 内部自己 with send_lock，
        # 外层再持一道非可重入锁会死锁。
        self._broadcast = broadcast.BroadcastScheduler(
            send_fn=self._broadcast_send, lock=None)
        self._broadcast.start()
        threading.Thread(target=self._retry_pending_replies,
                         daemon=True).start()
        print(f'大肥鱼已上线：监听所有会话，@{AT_NAME} 触发回答，Ctrl+C 退出', flush=True)
        for a in self.agents:
            print(f'[Agent] {a["name"]}（@{a["at_name"]}）'
                  f'{"" if a["enabled"] else "【已停用】"} → '
                  f'{"、".join(a["groups"]) or "（未绑定群）"}', flush=True)
        kw = self._keyword_ads.config()
        print(f'[关键词广告] {"已启用" if kw["enabled"] else "已停用"}：'
              f'{len(kw["keywords"])} 个关键词，冷却 {kw["cooldown_seconds"]}s，'
              f'未配置的群'
              f'{"默认监听" if kw["default_group_enabled"] else "默认不监听"}',
              flush=True)
        wx.KeepRunning()


PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bot.pid')

# Ctrl+C 之后留给收尾的时间；超时还没退出就 os._exit，绝不留后台进程。
# 卡住的场景是真实存在的：COM/UIA 调用中断不了、某个线程拖住解释器退出。
SHUTDOWN_GRACE_SECONDS = 3.0


def _looks_like_bot(cmdline) -> bool:
    """这条命令行是不是我们的机器人进程。

    用来识别「.bot.pid 里的 PID 已被系统复用给别的程序」——只看 PID 存不存在
    会把不相干的进程当成已有实例，双击启动就永远报「已有机器人在运行」。
    """
    # 判据只有一份（管理后台的 is_bot_cmdline）：管理页的启动/停止、这个
    # 单实例检查、一键启动前的清理必须认同一批进程，各写一份迟早对不上。
    from admin.server import is_bot_cmdline
    return is_bot_cmdline(cmdline)


def _pid_alive(pid: int) -> bool:
    """这个 PID 是否是一个**还在运行的机器人实例**。

    只判「进程存在」不够：PID 会被系统复用，残留的 .bot.pid 撞上别的进程
    就会误判成「已有实例」，害得启动被拒。所以再核对一次命令行。
    psutil 不可用时退回 ``os.kill(pid, 0)``（保守：宁可拒绝也不双开）。
    """
    try:
        import psutil
        p = psutil.Process(pid)
        if not p.is_running():
            return False
        return _looks_like_bot(p.cmdline())
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _acquire_single_instance():
    """拿机器人单实例锁；已有实例在跑时返回它的 PID，拿到锁返回 None。

    用 PID 文件而不是端口：机器人不监听端口，而双击两次、或一键启动脚本
    与终端各起一个，会变成两个进程抢同一个微信窗口——互相把会话切走，
    回复也可能重复发。2026-11-14 实测一键启动脚本撞出过第二个实例。
    """
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, encoding='utf-8') as f:
                other = int(f.read().strip() or '0')
            if other and other != os.getpid():
                if _pid_alive(other):
                    return other
                # 死进程留下的锁：不挡启动，但要说出来，否则用户以为又被挡了
                print(f'[启动] 清掉残留锁（PID {other} 已不在运行）', flush=True)
        with open(PID_FILE, 'w', encoding='utf-8') as f:
            f.write(str(os.getpid()))
        atexit.register(_release_single_instance)
    except Exception:
        pass          # 锁文件本身出问题时不要拦住启动
    return None


def _shutdown(code: int = 0) -> None:
    """Ctrl+C 之后确保进程真的结束。

    正常路径下 main() 返回、解释器退出，这个 3 秒的兜底 timer 根本来不及
    触发（它随进程一起消失）。真正要救的是「主线程回来了但进程不退」：
    某个 daemon 线程卡在 COM/UIA 调用里、atexit 里的清理卡住——这时
    timer 会 os._exit，绝不把进程留给下一次启动去挡自己。

    先放锁再挂 timer：os._exit 跳过 atexit，锁必须在这里主动删。
    """
    _release_single_instance()

    def _bail() -> None:
        print(f'[退出] {SHUTDOWN_GRACE_SECONDS:.0f} 秒内没结束，强制退出',
              flush=True)
        os._exit(code)

    t = threading.Timer(SHUTDOWN_GRACE_SECONDS, _bail)
    t.daemon = True
    t.start()


def _release_single_instance() -> None:
    """退出时删锁文件，且只删自己写的那一份（别删掉后来者的）。"""
    try:
        with open(PID_FILE, encoding='utf-8') as f:
            if int(f.read().strip() or '0') != os.getpid():
                return
        os.remove(PID_FILE)
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description='大肥鱼微信群 AI 机器人')
    ap.add_argument('--test', metavar='问题', help='只测 LLM 链路，不连微信')
    ap.add_argument('--search', nargs='?', const='', metavar='关键词',
                    help='搜索近3天群消息后退出（不带值=全部）；'
                         '配合 --member/--group')
    ap.add_argument('--member', default='', help='按发送者过滤（配合 --search）')
    ap.add_argument('--group', default='', help='按群名过滤（配合 --search）')
    ap.add_argument('--agent', default='', help='--test 时用哪个 Agent（id，默认第一个）')
    args = ap.parse_args()

    _setup_logging()

    # 一次性命令（--search / --test）不加单实例锁：它们不常驻、不抢窗口
    once = args.search is not None or args.test is not None
    if not once:
        # 先拿锁再初始化：DaFeiYuBot() 要连微信、解密数据库，几十秒。
        # 放在后面的话第二个实例会白跑一遍才被拒，用户看到的就是「双击后
        # 转半天才说已有实例」。
        other = _acquire_single_instance()
        if other:
            print(f'[启动] 已有机器人在运行（PID {other}），本次不再启动。'
                  f'\n        这不是错误：多半是你之前开的那个窗口还在跑，消息照常处理。'
                  f'\n        它的日志在 dafeiyu_bot.log（本目录）；要重启先结束 PID {other}'
                  f'（任务管理器，或关掉它所在的那个窗口）再双击。')
            return

    try:
        bot = DaFeiYuBot()
        if args.search is not None:
            print(bot.search_group_messages(args.search, args.member, args.group))
            return
        if args.test is not None:
            q = args.test.lstrip('\u2005 ').strip() or '在吗？'
            print(f'问：{q}')
            agent = next((a for a in bot.agents if a['id'] == args.agent),
                         bot.agents[0])
            _ctx.chat_id, _ctx.chat_name, _ctx.agent = '--test--', '链路测试', agent['id']
            print(f'Agent：{agent["name"]}（@{agent["at_name"]}）')
            answer, at = bot.think('--test--', q, who='测试用户', agent=agent)
            print(f'答：{answer}' + (f'\n（将 @ {at}）' if at else ''))
            return
        bot.run()
    except KeyboardInterrupt:
        print('[退出] 收到 Ctrl+C，正在停止监听…', flush=True)
    finally:
        if not once:
            _shutdown()


if __name__ == '__main__':
    main()
