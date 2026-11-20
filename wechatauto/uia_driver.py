# -*- coding: utf-8 -*-
"""wechatauto.uia_driver —— 基于 UI Automation 的微信 4.x 混合驱动引擎（可选路径）

背景
----
微信 4.1.12+ 的聊天区域使用自绘渲染，冷启动时对 UIAutomation 只暴露
``Qt51514QWindowIcon`` 空壳（2 个子节点），看不到任何 ``mmui::`` 控件——
这是旧版 wxauto 的 UIA 方案失效的原因。

实测（微信 4.1.12.26 / WMPF）发现：**热激活 Qt accessibility gate 后**，
UIA 树会立即物化为 ``mmui::MainWindow``，其中：
  * 搜索框 ``mmui::XValidatorTextEdit``（Name=「搜索」）可定位；
  * 输入关键词后下拉 ``AutomationId=search_list`` 暴露结果列表
    （真实结果 aid 前缀 ``search_item_*``），点击可打开会话；
  * 打开会话后 ``AutomationId=chat_input_field`` 输入框可定位，
    其 ``.Name`` 即当前聊天对象（三重校验依据）。

相比坐标 + OCR（``wechatauto.guia``），UIA 路径更准（无 OCR 抖动/生僻字
问题）、更快（无放大/多轮投票）。本模块将其封装为与 guia 相同的调用面，
供 :meth:`WeChatGUI.open_chat` / 发送方法**优先尝试、失败自动降级到 OCR**
（见 guia.py 的混合驱动逻辑）。

热激活安全性：只写运行中 Weixin 进程的 1 个 Qt accessibility byte
（从 Weixin.dll 扫描 RVA，字节码模式匹配 + qt.accessibility.core 引用
距离过滤），不注入代码、不重启进程；该 byte 本就是系统读屏器会写入的
状态位。微信升级后扫描逻辑通常仍有效，硬编码版本表仅作兜底。

使用前提：``pip install uiautomation pywin32``。
"""

from __future__ import annotations

import ctypes
import difflib
import json
import os
import re
import struct
import time
from ctypes import wintypes
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import uiautomation as auto

try:
    import win32gui, win32con, win32process, win32api
    _HAS_WIN32 = True
except Exception:                                   # pragma: no cover
    _HAS_WIN32 = False

from wechatauto import rhythm
from wechatauto import accessibility as _acc
from wechatauto.logger import wxlog
from wechatauto.param import WxParam

# ---------------------------------------------------------------------------
# 控件锚点
# ---------------------------------------------------------------------------
MAIN_CLASS = "mmui::MainWindow"
LOGIN_CLASS = "mmui::LoginWindow"
MAIN_NAMES = ("微信", "Weixin")
LOGIN_BTN_NAMES = ("进入微信", "登录", "进入")
LOGIN_OUTLINE_CLASS = "mmui::XOutlineButton"
SEARCH_EDIT_CLASS = "mmui::XValidatorTextEdit"
SEARCH_EDIT_NAME = "搜索"
SESSION_LIST_AID = "session_list"
SEARCH_LIST_AID = "search_list"
RESULT_AID_PREFIX = "search_item_"                 # 真实可打开结果的 aid 前缀
CHAT_INPUT_AID = "chat_input_field"                # 输入框；其 .Name == 当前聊天对象
# 输入框控件的 Name 是「会话名 + 输入占位符尾巴」拼出来的：框内为空时占位符
# 会被拼进 Name。2026-10-07 本机 4.1.15.13 实测尾巴是「按住鼠标 语音输入文字」
# （文件传输助手 → Name = "文件传输助手按住鼠标 语音输入文字"）。不剥掉的话
# 全库那些 ``current_chat() == who`` 比较统统失效：会话明明已经打开，open_chat
# 照样返回 False，发送被推去走 OCR/坐标路径（慢且更易错）。
# 2026-10-13 升到 4.1.15.53，同一颗键的文案换成「长按鼠标 语音输入」，逐字抄文案的
# 字面表整表失配、症状原样复发——所以按形状匹配这一族（(长按|按住)鼠标 + 语音输入 +
# 可选「文字」，裸的「语音输入文字」也在同一个模式里），不再记平台文案。
# 未命中任何尾巴就原样返回，行为与剥之前的版本一致。
CHAT_NAME_TAIL = re.compile(r"(?:(?:长按|按住)鼠标\s*)?语音输入(?:文字)?$")


def _clean_chat_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    stripped = name.strip()
    head = CHAT_NAME_TAIL.sub("", stripped).strip()
    if head:
        return head
    return stripped or None
# 行内表情码：微信输入框把 [微笑] 这类代码渲染成 1 个 U+FFFC 对象占位符
# （2026-10-04 实测 4.1.15.13），发送前回读比对时要按这个折算。
_STICKER_CODE = re.compile(r"\[[^\[\]]{1,10}\]")

# ---------------------------------------------------------------------------
# 新旧版本兼容候选
# 实测（4.1.13.65）：类名仍是上面这些，但 **AutomationId 变成了点分路径**
# （例如 MainView.main_tabbar、MainView….main_window_sub_splitter_view），
# 按短名精确等值匹配会失配。因此：单值锚点保留（兼容外部引用），
# 新增候选元组 + 容错匹配（精确 / 点分段相等 / 结尾匹配）+ 结构兜底。
# ---------------------------------------------------------------------------
MAIN_CLASSES = ("mmui::MainWindow",)
CHAT_WINDOW_CLASSES = ("mmui::ChatSingleWindow",)
HISTORY_WINDOW_CLASSES = ("mmui::SearchMsgUniqueChatWindow",)
LOGIN_CLASSES = ("mmui::LoginWindow",)
MAIN_NAME_HINTS = ("微信", "Weixin")               # 标题按“包含”匹配（新版可能带未读数）
SEARCH_EDIT_CLASSES = ("mmui::XValidatorTextEdit",)
# 「搜索」输入框在 4.1.15 有三处：左上会话搜索、聊天记录搜索、群成员面板搜索。
# 只有**会话搜索**能打开会话；后两处都在 ChatDetailView 子树里，误选它们会让
# open_chat 把词打进聊天搜索/群成员搜索里，然后报「会话打不开」。
SEARCH_EDIT_EXCLUDE_ANCESTORS = (
    "mmui::ChatDetailView",
    "mmui::ChatMessagePage",
    "mmui::ChatTitleBarMasterView",
    "mmui::ChatRoomMemberInfoView",
    "mmui::ChatInfoView",
)
SESSION_LIST_AIDS = ("session_list",)
SEARCH_LIST_AIDS = ("search_list",)
CHAT_INPUT_AIDS = ("chat_input_field",)
SNS_LIST_CLASSES = ("mmui::TimeLineListView",)
SNS_LIST_AIDS = ("sns_list",)

# 左侧导航栏（4.1.13 实测）：MainTabBar 下四个 XTabBarItem，顺序固定
# 微信/通讯录/收藏/发现。tab 自己读不到选中态（ButtonControl，既不支持
# SelectionItem 模式，LegacyIAccessible.State 也恒为 0），各页控件在树里又常驻，
# 所以判不出当前页——只能无条件点一下，见 WeChatUIA.back_to_chat_tab。
MAIN_TAB_BAR_CLS = "mmui::MainTabBar"
TAB_ITEM_CLS = "mmui::XTabBarItem"
CHAT_TAB_NAME = "微信"


def _title_is_main(title: str) -> bool:
    """主窗口标题判定：兼容“微信/Weixin”及带后缀（未读数等）的新版标题。"""
    if not title:
        return False
    if title in MAIN_NAMES:
        return True
    return any(hint in title for hint in MAIN_NAME_HINTS)


def _aid_hit(aid: str, candidates) -> bool:
    """AutomationId 容错匹配（新旧版通用）。

    旧版 aid 是短名（session_list）；新版是点分路径
    （MainView.main_tabbar…）。故：精确 / 点分段完全相等 / 结尾匹配 均算命中。
    """
    a = (aid or "").strip().lower()
    if not a:
        return False
    segs = a.split(".")
    for c in candidates:
        cl = (c or "").lower()
        if not cl:
            continue
        if a == cl or a.endswith("." + cl) or cl in segs:
            return True
    return False


def _find_by(root, pred, max_depth: int = 40, max_nodes: int = 4000):
    """子树里找第一个满足 pred 的控件（新旧版结构差异的兜底定位手段）。

    必须带节点预算：每次 `GetChildren()` 都是一次跨进程 COM 调用，从桌面根
    之类的宽起点遍历会摊出几十万个节点，把调用方卡死好几分钟。实测
    `_find_search_list` 从 `GetRootControl()` 起走深度 25，直接让
    `SendMsg` 永不返回，还占着 send_lock 把后续回复全堵死。
    到预算就放弃返回 None，让调用方走 OCR 等后备路径。
    """
    if root is None:
        return None
    stack = [(root, 0)]
    seen = 0
    while stack:
        el, d = stack.pop()
        seen += 1
        if seen > max_nodes:
            wxlog.debug("_find_by 达到节点预算 %d，放弃遍历", max_nodes)
            return None
        if d > max_depth:
            continue
        try:
            if pred(el):
                return el
        except Exception:
            pass
        try:
            for k in el.GetChildren():
                stack.append((k, d + 1))
        except Exception:
            pass
    return None
DEFAULT_EXE = r"C:\Program Files\Tencent\Weixin\Weixin.exe"

# 搜索结果分区标题（aid 为空且名字命中此集合的才算分区头，其余空 aid 视为建议项）
SECTION_HEADERS = {
    "最常使用", "最近使用", "联系人", "群聊", "公众号", "服务号", "订阅号",
    "聊天记录", "收藏", "功能", "小程序", "最近使用过的小程序", "视频号",
    "企业微信联系人", "朋友圈", "搜索网络结果", "企业微信",
}

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class UIATimeout(Exception):
    """UIA 操作超时。"""


class UIANotAvailable(Exception):
    """UIA 树不可用（热激活失败 / 微信未运行等），调用方应降级到 OCR。"""


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class WeChatUIA:
    """基于 UIAutomation 的微信 4.x 驱动引擎（混合驱动首选路径）。

    用法（一般通过 WeChatGUI 自动切换，也可独立使用）：:

        from wechatauto.uia_driver import WeChatUIA
        uia = WeChatUIA()
        uia.ensure_window()
        uia.open_chat('文件传输助手')
        uia.send_text('你好')

    多开时可传 hwnd=目标主窗口句柄；查找和恢复只限该窗口及原进程，
    目标失效时返回失败。省略 hwnd 保留自动发现行为。
    """

    def __init__(self, timeout: float = 15.0, search_timeout: float = 2.0,
                 hwnd: Optional[int] = None):
        self.timeout = timeout
        self.hwnd = hwnd
        self._bound_pid = self._pid_from_hwnd(hwnd) if hwnd is not None else None
        self._win = None
        # 聊天输入框定位结果 (win, ctrl, ts)。current_chat() 和 send_text()
        # 会连着调两次 _chat_input，每次全树 Exists() 都是几千次跨进程 COM
        # 调用；2026-11-14 实测其中一次 GetNextSiblingControl 卡了 45 分钟
        # 没返回，把该会话的 worker 和补发锁一起堵死。
        self._chat_input_cache = None
        # 会话搜索词缓存：username -> (上次搜索成功的词, 时间戳)。
        # 好处（2026-11-14 用户提出）：命中时只搜这一个词，既不用每次
        # WeChatDB().search_contact() 查库，也不用先白搜一轮 wxid。
        self._search_kw_cache = {}
        # 线程安全：WeChatBot 等宿主可能在后台线程实例化本驱动，COM 未初始化
        # 时 uiautomation 会报「尚未调用 CoInitialize / 无法加载
        # UIAutomationCore.dll」。CoInitializeEx 幂等，主线程重复调用无害。
        try:
            auto.InitializeUIAutomationInCurrentThread()
        except Exception:
            pass
        try:
            auto.SetGlobalSearchTimeout(search_timeout)
        except Exception:
            pass

    # ------------------------------------------------------------------ 基础
    @staticmethod
    def is_running() -> bool:
        """微信是否在运行（多判据；**不把“探测失败”当成“未运行”**）。

        判据：① tasklist（中文 Windows 输出为 GBK）；② 主窗口标题（微信/Weixin，
        登录窗也算）；③ psutil 进程枚举。任一命中即 True。
        只有**所有判据都出错失败**时才写一行 stderr 说明原因——避免把探测失败
        伪装成“微信没在运行”（那种假报错会误导上层去重启/唤起微信）。
        """
        errors, absent = [], []
        try:
            import subprocess
            out = subprocess.run(
                ["tasklist", "/fi", "imagename eq Weixin.exe", "/nh"],
                capture_output=True, text=True, encoding="gbk", errors="replace",
                timeout=10).stdout or ""
            if "Weixin.exe" in out:
                return True
            absent.append("tasklist")
        except Exception as exc:
            errors.append("tasklist: %r" % (exc,))
        try:
            if _HAS_WIN32:
                hits = []

                def _cb(hwnd, _):
                    try:
                        if win32gui.IsWindowVisible(hwnd) and _title_is_main(
                                win32gui.GetWindowText(hwnd)):
                            hits.append(hwnd)
                    except Exception:
                        pass
                    return True

                win32gui.EnumWindows(_cb, None)
                if hits:
                    return True
                absent.append("主窗口标题")
            else:
                errors.append("主窗口标题: 无 win32")
        except Exception as exc:
            errors.append("主窗口标题: %r" % (exc,))
        try:
            import psutil
            for proc in psutil.process_iter(["name"]):
                if (proc.info.get("name") or "").lower() == "weixin.exe":
                    return True
            absent.append("psutil")
        except Exception as exc:
            errors.append("psutil: %r" % (exc,))
        if errors and not absent:
            import sys as _sys
            _sys.stderr.write(
                "[wechatauto] WeChatUIA.is_running(): 所有判据均失败，无法确定微信状态"
                "（%s）；本次按 False 返回，但不要据此断定微信已退出\n"
                % "；".join(errors))
        return False

    def wake(self) -> None:
        """weixin:// 协议唤起/显示窗口（托盘态也能拉起）；失败则拉起 exe。"""
        try:
            os.startfile("weixin://")
            return
        except Exception:
            pass
        try:
            import subprocess
            subprocess.Popen([DEFAULT_EXE])
        except Exception:
            wxlog.warning("拉起微信失败")

    def _wake_accessibility(self) -> bool:
        """确保 mmui 树物化：设系统读屏标志 + 逐窗口热写并校验（含候选重试）。

        这里兜住异常：PE 扫描/热写任何一步出错都只该让「UIA 这条路这次不可用」，
        调用方会自己回落 OCR/坐标。以前不兜，扫描里一个异常会一路冒出
        ``ensure_window``，把 ``quick_send`` 整个打断（用户实测崩在 198MB DLL 的
        扫描循环里）。KeyboardInterrupt 属于 BaseException，不在此列，照常中断。
        """
        ok = False
        candidates = self._candidate_hwnds()
        if not candidates and getattr(self, 'hwnd', None) is not None:
            return False
        for hwnd in candidates:
            try:
                ok = self._hot_activate_accessibility(hwnd) or ok
            except Exception as e:
                wxlog.warning("热激活 UIA 异常，本轮跳过（改用 OCR/坐标定位）：%s", e)
        if not ok:
            # 字节那条路走不通（不认识的结构/扫描失败）才动系统标志：真机对照过，
            # 树只跟 gate 字节走，读屏标志是全系统级开关，别在常规路径上顺手开。
            self._set_screen_reader_flag(True)
        if ok:
            self._win = None
            time.sleep(0.2)
        return ok

    # ------------------------------------------------------------------ 热激活
    # gate 本体挪到了 wechatauto.accessibility（扫 RVA / 写进程内存 / 校验），
    # 这里只留同名转发：调用方和离线自检一个字都不用改，而且 _wake_accessibility
    # 依旧走 self.，所以「worker 被换掉时它要能看见」那条判据仍然成立。
    _set_screen_reader_flag = staticmethod(_acc.AccessibilityGate._set_screen_reader_flag)
    _pid_from_hwnd = staticmethod(_acc.AccessibilityGate._pid_from_hwnd)
    _process_modules = staticmethod(_acc.AccessibilityGate._process_modules)
    _weixin_dll_module = classmethod(
        lambda cls, pid: _acc.AccessibilityGate._weixin_dll_module(pid))
    _mmui_present = staticmethod(_acc.AccessibilityGate._mmui_present)
    # 离线自检直接拿这几个当被测函数（PE 扫描的等价性/容错），保留同名入口
    _pe_sections = staticmethod(_acc.AccessibilityGate._pe_sections)
    _section_for_rva = staticmethod(_acc.AccessibilityGate._section_for_rva)
    _offset_to_rva = staticmethod(_acc.AccessibilityGate._offset_to_rva)
    _rip_xrefs_to_rva = staticmethod(_acc.AccessibilityGate._rip_xrefs_to_rva)
    _rip_xrefs_to_rva_ref = staticmethod(_acc.AccessibilityGate._rip_xrefs_to_rva_ref)
    _scan_qaccessible_candidates = staticmethod(
        _acc.AccessibilityGate._scan_qaccessible_candidates)
    _scan_qaccessible_active_rva = staticmethod(
        _acc.AccessibilityGate._scan_qaccessible_active_rva)
    _qaccessible_candidate_rvas = staticmethod(
        _acc.AccessibilityGate._qaccessible_candidate_rvas)
    _qaccessible_active_rva = staticmethod(_acc.AccessibilityGate._qaccessible_active_rva)
    _read_process_byte = staticmethod(_acc.AccessibilityGate._read_process_byte)
    _write_process_byte = staticmethod(_acc.AccessibilityGate._write_process_byte)

    def _hot_activate_accessibility(self, hwnd: int, verify: bool = True,
                                    max_candidates: int = 4) -> bool:
        return _acc.AccessibilityGate._hot_activate_accessibility(
            hwnd, verify=verify, max_candidates=max_candidates)

    @staticmethod
    def gate_block_hint(diag: dict, bits: Optional[int] = None) -> str:
        return _acc.gate_block_hint(diag, bits)

    def _warn_gate_blocked(self, diag: dict) -> None:
        _acc.warn_gate_blocked(diag)

    # ------------------------------------------------------------------ 窗口定位
    def _wechat_hwnds(self, diag: Optional[dict] = None):
        """可见、标题像主窗的微信窗口，按面积从大到小。

        只保留加载了 Weixin.dll 的主进程窗口，过滤掉无 DLL 的辅助进程窗口
        （其热激活必然失败，只会产生噪音警告）。

        ``diag`` 传一个 dict 时会填入扫描计数（``win32`` / ``matched`` / ``kept`` /
        ``dropped_no_dll``），供「一个窗口都没留下」时给出能定位的警告——那正是
        32 位解释器读不到 64 位进程模块的特征，静默返回空会被用户当成「控件树被屏蔽」。
        """
        if diag is not None:
            diag.update({'win32': bool(_HAS_WIN32), 'matched': 0, 'kept': 0,
                         'dropped_no_dll': 0})
        if not _HAS_WIN32:
            return []
        res = []

        def cb(h, _):
            try:
                title = win32gui.GetWindowText(h)
                if win32gui.IsWindowVisible(h) and _title_is_main(title):
                    if diag is not None:
                        diag['matched'] += 1
                    pid = self._pid_from_hwnd(h)
                    if pid and self._weixin_dll_module(pid):
                        l, t, r, b = win32gui.GetWindowRect(h)
                        res.append(((r - l) * (b - t), h))
                        if diag is not None:
                            diag['kept'] += 1
                    elif diag is not None:
                        diag['dropped_no_dll'] += 1
            except Exception:
                pass
            return True
        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            pass
        res.sort(reverse=True)
        return [h for _, h in res]

    def _anchor(self, hwnd):
        try:
            return auto.ControlFromHandle(hwnd)
        except Exception:
            return None

    def _candidate_hwnds(self, diag=None):
        """显式绑定后不再全局择窗；关闭或句柄被另一进程复用时失败。"""
        hwnd = getattr(self, 'hwnd', None)
        if hwnd is None:
            return self._wechat_hwnds(diag) if diag is not None else self._wechat_hwnds()
        try:
            if (win32gui.IsWindow(hwnd) and self._bound_pid
                    and self._pid_from_hwnd(hwnd) == self._bound_pid):
                return [hwnd]
        except Exception as exc:
            wxlog.debug('绑定的微信窗口不可用：%s', exc)
        return []

    def _find_main(self):
        # 自动发现仍只选主窗；独立窗必须由调用方显式指定 HWND。
        classes = MAIN_CLASSES
        if getattr(self, 'hwnd', None) is not None:
            classes += CHAT_WINDOW_CLASSES + HISTORY_WINDOW_CLASSES
        for h in self._candidate_hwnds():
            c = self._anchor(h)
            if c is not None and (c.ClassName or "") in classes:
                if getattr(self, 'hwnd', None) is not None and int(c.NativeWindowHandle) != h:
                    continue
                return c
        return None

    def window_kind(self) -> str:
        """返回绑定窗口类型：main / chat / history / unknown；不切换窗口。"""
        win = self._find_main()
        if win is not None:
            for kind, classes in (('main', MAIN_CLASSES), ('chat', CHAT_WINDOW_CLASSES),
                                  ('history', HISTORY_WINDOW_CLASSES)):
                if win.ClassName in classes:
                    return kind
        return 'unknown'

    def _login_window(self):
        for h in self._candidate_hwnds():
            c = self._anchor(h)
            if c is not None and (c.ClassName or "") in LOGIN_CLASSES:
                return c
        return None

    # ------------------------------------------------------------------ 前台/登录
    @staticmethod
    def _force_foreground(hwnd) -> bool:
        if not _HAS_WIN32 or not hwnd:
            return False
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception:
            pass
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        cur = win32api.GetCurrentThreadId()
        fg = win32gui.GetForegroundWindow()
        fgt = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        try:
            if fgt:
                win32process.AttachThreadInput(cur, fgt, True)
        except Exception:
            pass
        ok = False
        for _ in range(3):
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
                if win32gui.GetForegroundWindow() == hwnd:
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(0.1)
        try:
            if fgt:
                win32process.AttachThreadInput(cur, fgt, False)
        except Exception:
            pass
        return ok

    def _activate(self, win) -> None:
        if not self._force_foreground(win.NativeWindowHandle):
            try:
                win.ShowWindow(9)
            except Exception:
                pass
            try:
                win.SetActive()
            except Exception:
                pass
        time.sleep(0.25)

    def _wait_main(self, timeout: float, allow_login: bool = True,
                   allow_accessibility_wake: bool = True,
                   activate: bool = True):
        deadline = time.time() + max(timeout, 15)
        last_wake = 0.0
        last_pull = time.time()
        while time.time() < deadline:
            if allow_login:
                self._auto_login()
            w = self._find_main()
            if w is not None:
                self._win = w
                if activate:
                    self._activate(w)
                return w
            if (allow_accessibility_wake and self._login_window() is None
                    and self._candidate_hwnds() and time.time() - last_wake > 5):
                # 周期性复写 gate：微信重启/重建窗口后该字节会归零，
                # 需补写以免子控件「昨天能扫今天扫不到」（内部含校验与候选重试）
                self._wake_accessibility()
                last_wake = time.time()
                continue
            if (getattr(self, 'hwnd', None) is None and self._login_window() is None
                    and time.time() - last_pull > 6):
                self.wake()
                last_pull = time.time()
            time.sleep(0.8)
        raise UIATimeout("等待微信主窗口超时（客户端未就绪）。")

    def is_materialized(self) -> bool:
        """当前是否已物化出 mmui 树（即能扫到子控件）。"""
        return self._find_main() is not None

    def describe_layout(self) -> dict:
        """诊断自检：报告当前微信布局与关键锚点解析情况（新旧版兼容排查）。

        返回 dict：main_class / title / layout(merged|legacy|chat|standalone-chat|history) /
        anchors{main_window,search_box,session_list,chat_input,main_tabbar,sns_list}。
        锚点为 None 表示当前窗口没有该控件，需结合布局判断是否失配。
        """
        rep = {"main_class": None, "title": None, "layout": "unknown",
               "anchors": {}, "wechat_hwnds": self._candidate_hwnds()}
        w = self._find_main()
        if w is None:
            rep["layout"] = "not-materialized-or-not-running"
            return rep
        rep["main_class"] = w.ClassName
        try:
            rep["title"] = w.Name
        except Exception:
            pass
        merged = _find_by(w, lambda c: (c.ClassName or "") in ("mmui::SNSContentView",
                                                              "mmui::TimeLineListView"),
                          max_depth=30)
        legacy = _find_by(w, lambda c: (c.ClassName or "") == "mmui::SNSWindow",
                          max_depth=10)
        rep["layout"] = ("standalone-chat" if w.ClassName in CHAT_WINDOW_CLASSES
                         else "history" if w.ClassName in HISTORY_WINDOW_CLASSES
                         else "merged" if merged is not None
                         else "legacy" if legacy is not None else "chat")

        def _desc(el):
            if el is None:
                return None
            return "%s|%s" % (getattr(el, "ClassName", "") or "",
                              getattr(el, "AutomationId", "") or "")

        checks = {
            "main_window": lambda c: (c.ClassName or "") in MAIN_CLASSES,
            "search_box": lambda c: (c.ControlTypeName == "EditControl"
                                     and SEARCH_EDIT_NAME in (c.Name or "")),
            "session_list": lambda c: _aid_hit(getattr(c, "AutomationId", ""),
                                               SESSION_LIST_AIDS),
            "chat_input": lambda c: _aid_hit(getattr(c, "AutomationId", ""),
                                             CHAT_INPUT_AIDS),
            "main_tabbar": lambda c: (c.ClassName or "") == "mmui::MainTabBar",
            "sns_list": lambda c: (c.ClassName or "") in SNS_LIST_CLASSES
                                  or _aid_hit(getattr(c, "AutomationId", ""), SNS_LIST_AIDS),
        }
        for name, pred in checks.items():
            rep["anchors"][name] = _desc(_find_by(w, pred, max_depth=30))
        return rep

    def ensure_materialized(self, timeout: float = 6.0, force: bool = False) -> bool:
        """确保 mmui 树物化：窗口在但子控件扫不到时热写 gate 并校验。

        与 ensure_window 的分工：本方法不拉起/置前窗口，只修复「窗口存在、
        但树退化成 Qt 空壳」的状态，供长驻进程按需自愈（微信重启/升级后
        gate byte 归零导致子控件整片消失的场景）。force=True 时即使当前
        已物化也重新走一遍热写校验。
        """
        if not force and self._find_main() is not None:
            return True
        scan = {}
        if not self._candidate_hwnds(scan):
            self._warn_gate_blocked(scan)
            return False
        deadline = time.time() + max(1.0, timeout)
        while True:
            self._wake_accessibility()
            if self._find_main() is not None:
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    def ensure_window(self, wake: bool = True, timeout: Optional[float] = None,
                      activate: bool = False) -> bool:
        """确保可访问的绑定窗口存在，返回是否成功（默认**不抢前台**）。

        优先 ControlFromHandle 按句柄锚定拿 mmui 树；若树未物化（只剩 Qt
        外壳）则对当前 Weixin 进程热激活，不重启微信。任一环节失败返回
        False，由调用方降级到 OCR。

        默认 ``activate=False``：只在窗口最小化时还原，**不抢前台**。发送路径
        全程用 UIA 写值 + PostMessage 投递，不需要窗口在前台；只有必须走
        真实鼠标键盘的旧路径才显式传 ``activate=True``。
        """
        timeout = timeout or self.timeout
        w = self._find_main()
        if w is not None:
            self._win = w
            try:
                fg = ctypes.windll.user32.GetForegroundWindow()
                if fg and fg != w.NativeWindowHandle:
                    self._user_fg = fg
            except Exception:
                pass
            if activate:
                self._activate(w)
            else:
                try:
                    if self._input._user32.IsIconic(w.NativeWindowHandle):
                        w.ShowWindow(9)
                except Exception:
                    pass
            return True
        self._win = None
        if not wake:
            return False
        try:
            if self._login_window() is not None:
                self._auto_login()
            elif self._candidate_hwnds():
                # 窗口在但树可能是 Qt 空壳：热写 gate 并校验（候选自动重试）
                self.ensure_materialized(timeout=min(6.0, max(2.0, timeout / 2)))
            else:
                if getattr(self, 'hwnd', None) is not None:
                    return False
                # 走到这里说明连可用窗口都没扫到：以前是静默改走「读屏标志 + wake」，
                # 用户只看到「没树」，看不出是没登录还是位数不对。
                scan = {}
                self._wechat_hwnds(scan)
                self._warn_gate_blocked(scan)
                self._set_screen_reader_flag(True)
                self.wake()
            self._wait_main(timeout, activate=activate)
            return self._win is not None
        except UIATimeout:
            return False
        except Exception as e:
            wxlog.debug("ensure_window 失败：%s", e)
            return False

    def _auto_login(self) -> bool:
        lw = self._login_window()
        if lw is None:
            return False
        self._force_foreground(lw.NativeWindowHandle)
        time.sleep(0.3)
        for nm in LOGIN_BTN_NAMES:
            btn = lw.ButtonControl(Name=nm)
            if btn.Exists(0.2, 0.1):
                btn.Click()
                time.sleep(1.2)
                return True
        ob = lw.ButtonControl(ClassName=LOGIN_OUTLINE_CLASS)
        if ob.Exists(0.2, 0.1) and (ob.Name or "").strip():
            ob.Click()
            time.sleep(1.2)
            return True
        return False

    # ------------------------------------------------------------------ 剪贴板
    @staticmethod
    def _clip_get() -> str:
        try:
            import pyperclip
            return pyperclip.paste()
        except Exception:
            pass
        try:
            return auto.GetClipboardText()
        except Exception:
            return ""

    @staticmethod
    def _clip_set(text: str) -> None:
        try:
            import pyperclip
            pyperclip.copy(text)
            return
        except Exception:
            pass
        try:
            auto.SetClipboardText(text)
        except Exception:
            pass

    @staticmethod
    def _set_cursor(x: int, y: int) -> None:
        """把光标走到目标点（轨迹与步数见 :mod:`wechatauto.rhythm`）。"""
        try:
            import ctypes
            rhythm.move_to(ctypes.windll.user32, int(x), int(y))
        except Exception:
            pass

    @staticmethod
    def _mouse_wheel(delta: int) -> None:
        try:
            import ctypes
            ctypes.windll.user32.mouse_event(0x0800, 0, 0, int(delta), 0)
        except Exception:
            pass

    @staticmethod
    def _left_click() -> None:
        try:
            import ctypes
            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # down
            rhythm.nap(0.05)
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # up
        except Exception:
            pass

    @staticmethod
    def _right_click() -> None:
        try:
            import ctypes
            ctypes.windll.user32.mouse_event(0x0008, 0, 0, 0, 0)  # down
            rhythm.nap(0.05)
            ctypes.windll.user32.mouse_event(0x0010, 0, 0, 0, 0)  # up
        except Exception:
            pass

    # ---------------------------------------------------------- 渲染层穿透
    # 微信 4.x 的界面全画在 MMUIRenderSubWindowHW 上，该窗口带
    # WS_EX_LAYERED|WS_EX_TRANSPARENT：mouse_event 的命中测试会跳过它，
    # 点击落到后面的主窗口，界面上就是「点了没反应」。uiautomation 的
    # Control.Click() 内部也是 mouse_event，所以同样打不中（实测：搜索下拉
    # 结果点完界面纹丝不动）。guia 侧的 wx_click/wx_wheel 一直在处理这件事，
    # UIA 驱动这边补齐——先临时摘掉鼠标所在那条窗口链的 WS_EX_TRANSPARENT。
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x00000020

    @staticmethod
    def _win_chain(x: int, y: int) -> List[int]:
        """(x,y) 这一点从最上层窗口开始、逐级向上的句柄列表。"""
        try:
            u = ctypes.windll.user32
            cur = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        except Exception:
            return []
        out = []
        for _ in range(6):
            if not cur:
                break
            out.append(int(cur))
            cur = u.GetParent(cur)
        return out

    def _clear_transparent(self, x: int, y: int) -> List[Tuple[int, int]]:
        """摘掉该点窗口链上的 WS_EX_TRANSPARENT，返回待还原的 (句柄, 原样式)。"""
        u = ctypes.windll.user32
        saved = []
        for h in self._win_chain(x, y):
            try:
                ex = ctypes.c_long(u.GetWindowLongW(h, self.GWL_EXSTYLE)).value & 0xFFFFFFFF
            except Exception:
                continue
            if ex & self.WS_EX_TRANSPARENT:
                try:
                    u.SetWindowLongW(h, self.GWL_EXSTYLE,
                                     ex & ~self.WS_EX_TRANSPARENT)
                    saved.append((h, ex))
                except Exception:
                    pass
        if saved:
            time.sleep(0.05)
        return saved

    def _restore_transparent(self, saved: List[Tuple[int, int]]) -> None:
        u = ctypes.windll.user32
        for h, ex in saved:
            try:
                u.SetWindowLongW(h, self.GWL_EXSTYLE, ex)
            except Exception:
                pass
        if saved:
            time.sleep(0.05)

    def _click_at(self, x: int, y: int, right: bool = False) -> None:
        self._set_cursor(x, y)
        rhythm.nap(0.12)
        saved = self._clear_transparent(x, y)
        try:
            if right:
                self._right_click()
            else:
                self._left_click()
        finally:
            self._restore_transparent(saved)
        rhythm.nap(0.2)

    def _click_ctrl(self, ctrl, right: bool = False) -> bool:
        """点一下控件矩形**内部的随机点**（走 _click_at，不吃 WS_EX_TRANSPARENT 的亏）。

        故意不点正中：每次都命中同一个像素是脚本最明显的特征，而人手落在
        控件内任意处。``rhythm.point`` 保证四边内缩后再取点。
        """
        try:
            r = ctrl.BoundingRectangle
        except Exception:
            return False
        if not r or r.width() <= 0 or r.height() <= 0:
            return False
        x, y = rhythm.point((r.left, r.top, r.right, r.bottom))
        self._click_at(x, y, right=right)
        return True

    def _raw_click_ctrl(self, ctrl) -> bool:
        """不做「摘 WS_EX_TRANSPARENT」那一步的兜底点击，但**仍走拟人落点与轨迹**。

        2026-10-23 实测（主窗最大化、停在朋友圈页）：``_click_ctrl`` 那条
        「临时摘掉 WS_EX_TRANSPARENT」的流程在这个布局下会把点击吃掉，连点
        4 次导航栏都不切页；裸 ``mouse_event`` 一次就切回去了。这里只省掉
        摘透明层，落点仍用 :func:`rhythm.point`（控件内随机点）、光标仍用
        :func:`rhythm.move_to` 走过去、等待仍用 :func:`rhythm.nap`。
        只在常规路径全部失败后调用，不改变日常点击策略。
        """
        try:
            r = ctrl.BoundingRectangle
        except Exception:
            return False
        if not r or r.width() <= 0 or r.height() <= 0:
            return False
        x, y = rhythm.point((r.left, r.top, r.right, r.bottom))
        try:
            self._force_foreground(self._win.NativeWindowHandle)
            u = ctypes.windll.user32
            rhythm.move_to(u, int(x), int(y))
            rhythm.nap(0.12)
            u.mouse_event(0x0002, 0, 0, 0, 0)
            rhythm.nap(0.06)
            u.mouse_event(0x0004, 0, 0, 0, 0)
            rhythm.nap(0.2)
            return True
        except Exception:
            return False

    def _wheel_at(self, x: int, y: int, delta: int) -> None:
        self._set_cursor(x, y)
        rhythm.nap(0.1)
        saved = self._clear_transparent(x, y)
        try:
            self._mouse_wheel(delta)
        finally:
            self._restore_transparent(saved)

    # ------------------------------------------------------------------ 控件定位
    @staticmethod
    def _search_button(win):
        """微信 4.1.15 起搜索入口**收起**成一个按钮：Name=「搜索」的 ButtonControl。"""
        return _find_by(win, lambda c: (c.Name or "").strip() == SEARCH_EDIT_NAME
                        and "Button" in c.ControlTypeName, max_depth=40)

    def _search_box_present(self, win):
        """搜索框已经展开时的定位（4.1.13 及以前它常驻）。

        4.1.15 的树里可能同时有三颗 Name='搜索' 的 Edit：
        左上会话搜索、聊天记录搜索、群成员面板搜索。**必须优先会话搜索框**，
        否则 open_chat 的词会被打进聊天内搜索里（实测：群信息面板开着时必然踩），
        然后搜不到任何会话。判据用祖先类名，不靠坐标/版本号。
        """
        cands = []
        try:
            for c, _d in auto.WalkControl(win, maxDepth=40):
                try:
                    if (c.ControlTypeName == "EditControl"
                            and SEARCH_EDIT_NAME in (c.Name or "")):
                        cands.append(c)
                except Exception:
                    continue
        except Exception as e:
            # 微信正在重绘/关窗时，遍历会撞上失效元素（COMError: 元素不可用）。
            # 已经拿到的候选照常用，后面的 _find_by 兜底也会再试一次。
            wxlog.debug("遍历搜索框时元素失效：%s", e)
        if cands:
            def rank(c):
                try:
                    left = c.BoundingRectangle.left
                except Exception:
                    left = 1 << 30
                return (1 if self._in_chat_scope(c) else 0, left)
            return min(cands, key=rank)
        # 2) 新旧版兜底：Name 含“搜索”的编辑框（忽略类名变化）
        return _find_by(win, lambda c: (c.ControlTypeName == "EditControl"
                                        and SEARCH_EDIT_NAME in (c.Name or "")))

    @staticmethod
    def _in_chat_scope(ctrl) -> bool:
        """控件是否在右侧聊天区（聊天记录/群成员面板）里。"""
        cur = ctrl
        for _ in range(10):
            try:
                cur = cur.GetParentControl()
            except Exception:
                return False
            if cur is None:
                return False
            try:
                if (cur.ClassName or "") in SEARCH_EDIT_EXCLUDE_ANCESTORS:
                    return True
            except Exception:
                continue
        return False

    def _search_box(self, win, expand: bool = False):
        """定位搜索输入框。

        ``expand=True`` 允许**点一下收起的搜索按钮**把它展开（4.1.15+ 静止状态下
        树里没有输入框）。实测 ``Invoke()`` 是空操作、必须真点，所以这动作有副作用，
        只有真要搜索时才传；``search_box_rect`` 那种只读锚点不传。
        """
        box = self._search_box_present(win)
        if box is not None or not expand:
            return box
        btn = self._search_button(win)
        if btn is None or not self._post_click_ctrl(btn):
            return None
        for _ in range(4):
            time.sleep(0.3)
            box = self._search_box_present(win)
            if box is not None:
                return box
        return None

    def _chat_input(self, win=None):
        win = win or self._win
        if win is None or win.ClassName in HISTORY_WINDOW_CLASSES:
            return None
        cache = getattr(self, '_chat_input_cache', None)
        if (cache is not None and cache[0] is win
                and time.time() - cache[2] < 30.0):
            return cache[1]

        def _remember(ctrl):
            """记住定位结果，30 秒内不再全树重搜。"""
            self._chat_input_cache = (win, ctrl, time.time())
            return ctrl

        # 1) 旧版：aid 短名精确匹配
        e = win.EditControl(AutomationId=CHAT_INPUT_AID)
        if e.Exists(1.0, 0.2):
            return _remember(e)
        # 2) 新版：aid 点分路径 → 容错匹配
        hit = _find_by(win, lambda c: _aid_hit(getattr(c, "AutomationId", ""),
                                               CHAT_INPUT_AIDS))
        if hit is not None:
            return _remember(hit)
        # 独立窗只认发送控件的明确锚点，不把其他编辑框当作聊天输入框。
        if win.ClassName in CHAT_WINDOW_CLASSES:
            return None
        # 3) 结构兜底：聊天区里可编辑的 Edit（排除搜索框）。
        # 预算压到 800：这一步是纯客户端逐节点遍历（每次 GetChildren 都是一次
        # 跨进程调用），树大时既慢又容易撞上卡顿的 COM 调用；正常能找到的情况
        # 在前两步就命中了。
        hit = _find_by(win, lambda c: (c.ControlTypeName == "EditControl"
                                       and SEARCH_EDIT_NAME not in (c.Name or "")),
                       max_nodes=800)
        return _remember(hit) if hit is not None else None

    def current_chat(self) -> Optional[str]:
        if self._win is not None and self._win.ClassName in CHAT_WINDOW_CLASSES:
            # 独立窗的输入控件 Name 只是「输入」，会话名称来自根窗口。
            return self._win.Name or None
        e = self._chat_input()
        return (_clean_chat_name(e.Name) if e else None)

    def search_box_rect(self) -> Optional[Tuple[int, int, int, int]]:
        """搜索框的物理矩形 (left, top, right, bottom)，拿不到返回 None。

        给 guia 的 OCR 兜底路径当点击锚点：那边的 ``SEARCH_BOX_RATIO`` 是按
        窗高比例算的，窗口一改尺寸就飘（实测飘到聊天输入框里，搜索词被粘进
        了别人的会话）。控件树里有精确矩形，没必要猜。
        """
        if self._win is None and not self.ensure_window():
            return None
        target = self._search_box(self._win) or self._search_button(self._win)
        if target is None:
            return None
        try:
            r = target.BoundingRectangle
            return (r.left, r.top, r.right, r.bottom)
        except Exception:
            return None

    def _find_search_list(self, timeout: float = 3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            # 1) 旧版：aid 短名。搜索必须限定在**微信窗口子树**里：
            #    uiautomation 的全局搜索（不给 searchFromControl）会遍历整个
            #    桌面，配 searchDepth=0xFFFFFFFF 实测一次 48 秒才返回。
            try:
                lst = auto.ListControl(searchFromControl=self._win,
                                       searchDepth=0xFFFFFFFF,
                                       AutomationId=SEARCH_LIST_AID)
                if lst.Exists(0.2, 0.1):
                    return lst
            except Exception as e:
                wxlog.debug("搜索下拉(旧锚点)读取失败：%s", e)
            # 2) 新版：aid 点分路径 → 从**微信窗口**子树按属性容错匹配。
            #    绝不能从 auto.GetRootControl()（桌面根）起遍历：桌面下挂着
            #    所有窗口、任务栏和每个应用，深度 25 能摊出几十万个节点，
            #    每个 GetChildren 都是一次跨进程 COM 调用——实测会把
            #    SendMsg 卡死好几分钟不返回，而且它持有 send_lock，
            #    后面所有回复全被堵住（表现就是「机器人收到了但不回」）。
            root = self._win
            if root is None:
                # 没有窗口引用就不要退回桌面根：从桌面遍历等于卡死。
                # 宁可返回 None 让上层走 OCR 回退。
                wxlog.debug("未持有微信窗口引用，跳过搜索下拉的新锚点搜索")
            try:
                hit = _find_by(root, lambda c: (c.ControlTypeName == "ListControl"
                                                and _aid_hit(getattr(c, "AutomationId", ""),
                                                             SEARCH_LIST_AIDS)),
                               max_depth=25) if root is not None else None
            except Exception as e:
                wxlog.debug("搜索下拉(新锚点)读取失败：%s", e)
                hit = None
            if hit is not None:
                return hit
            time.sleep(0.2)
        return None

    def _collect_results(self, keyword: str, settle: float = 1.5) -> List[dict]:
        """采集去重后的候选 [{name, section, aid, cell}]。"""
        lst = self._find_search_list()
        if lst is None:
            return []
        deadline = time.time() + settle
        raw = []
        while time.time() < deadline:
            section = None
            raw = []
            for c in lst.GetChildren():
                name = c.Name or ""
                aid = c.AutomationId or ""
                if not aid:
                    head = name.strip()
                    if head in SECTION_HEADERS:
                        section = head
                    continue
                if not aid.startswith(RESULT_AID_PREFIX):
                    continue
                raw.append({
                    "cell": c,
                    "name": name.split("\n", 1)[0].strip(),
                    "section": section,
                    "aid": aid,
                })
            if raw:
                break
            time.sleep(0.2)

        kw = keyword.strip()
        matched = [r for r in raw if r["name"] == kw]
        if not matched:
            matched = raw[:]

        # 最常使用/最近使用中重复出现的同一 aid 去重
        non_freq = {r["aid"] for r in matched
                    if r["section"] not in ("最常使用", "最近使用")}
        pruned = [r for r in matched
                  if not (r["section"] in ("最常使用", "最近使用")
                          and r["aid"] in non_freq)]
        for i, r in enumerate(pruned):
            r["index"] = i
        return pruned

    # ------------------------------------------------------------------ 高层动作
    # 搜索词缓存有效期（秒）。对方改名/改备注后缓存词会搜不到，届时清掉重走。
    SEARCH_KW_TTL = 1800.0

    def _search_keywords(self, keyword: str) -> List[str]:
        """本次要依次试的搜索词。

        命中缓存（同一 username 在 ``SEARCH_KW_TTL`` 内成功打开过）时**只返回
        那一个词**——它已被证实能让微信搜索框命中，直接省掉查库和前面失败的
        候选。缓存词失效时 ``open_chat`` 会清缓存并重走 ``_resolve_search_keyword``。
        """
        hit = self._search_kw_cache.get(keyword)
        if hit and (time.time() - hit[1]) < self.SEARCH_KW_TTL:
            return [hit[0]]
        return self._resolve_search_keyword(keyword)

    @staticmethod
    def _resolve_search_keyword(keyword: str) -> List[str]:
        """把账号/username 解析成微信搜索框能命中的关键词列表。

        微信搜索框**不认 wxid**（系统账号），只认微信号(alias) / 昵称 / 备注。
        2026-11-14 实测：搜 alias「God-Cap」能直接命中「强」，且在通讯录里唯一
        ——重名场景下这是最可靠的词（本账号 `.` 45 人、`L` 32 人重名）。
        顺序：alias → remark → nick_name → 原始 keyword（兜底，通常搜不到）。
        """
        candidates: List[str] = []
        try:
            from wechatauto.db import WeChatDB
            db = WeChatDB()
            for hit in db.search_contact(keyword):
                for k in (hit.get("alias"), hit.get("remark"),
                          hit.get("nick_name")):
                    if k and k not in candidates:
                        candidates.append(k)
        except Exception:
            pass
        if keyword and keyword not in candidates:
            candidates.append(keyword)
        return candidates

    @staticmethod
    def _chat_page_ready(win) -> bool:
        """聊天页那一片「看得见」没有：会话列表或搜索框任一在树里就算看得见。

        只认这两样，不认消息列表——窄窗口里开着会话时消息列表本来就在。
        实测（主窗 848×1274、开着会话）：``session_list`` 与搜索框**整片不在树里**，
        而导航栏 4 颗 tab 的 ``IsSelected``/``SelectionItemPattern`` 一个都读不出来，
        所以判「有没有回到会话列表」只能靠这两样，不能靠 tab 的选中态。
        """
        if win is None:
            return False
        return (_find_by(win, lambda c: _aid_hit(getattr(c, "AutomationId", ""),
                                                 SESSION_LIST_AIDS), max_depth=30)
                is not None
                or _find_by(win, lambda c: (c.ControlTypeName == "EditControl"
                                            and SEARCH_EDIT_NAME in (c.Name or "")),
                            max_depth=30) is not None)

    def back_to_chat_tab(self, settle: float = 1.0, max_clicks: int = 2,
                         require_list: bool = False, force: bool = False) -> bool:
        """点导航栏第一个 tab（「微信」栏），把主窗带回聊天页。

        朋友圈相关接口（``WeChat.SwitchToMoments``）会把主窗留在朋友圈页，那里
        会话列表和消息列表都不渲染，之后一切按搜索框/消息列表走的操作静默失败
        （实测只剩一句 ``message_list is None``）。

        这里**不按控件判页**（切到通讯录/收藏/发现以后，会话列表、消息列表、搜索框
        照样报 ``offscreen=False``、rect 一个像素都不变，树读不出当前是哪一页），
        但**要按「聊天页那一片有没有渲染出来」再决定点不点第二下**：窄窗口（实测
        848px 宽）里开着会话时，点第一下「微信」只把主窗带回聊天页，**当前会话还开着**，
        ``session_list`` 和搜索框仍然不在树里——用户实测要再点一次才退出会话。
        只点一下的旧写法会让下一步的搜索框查找直接失败，看起来就是「会话打不开」。
        所以改成：点一下 → 等 ``settle`` → 看聊天页那一片出来没有 → 没出来且还有额度
        就再点一次。已经出来就**不再多点**（那一下会把用户正在看的会话切掉）。
        ``require_list=True`` 时判据更严：只认 ``session_list``（调用方真的要会话列表，
        而不只是搜索框）。tab 名匹配不上（改版/语言包）就按导航栏第一个 item 兜底，
        顺序固定微信在最前。找不到 MainTabBar 返回 False，不抛。

        ``force=True`` 时不看「聊天页那片在不在」，**无条件先点一次**。
        用户实测（2026-11-14）：微信停在「微信游戏」/朋友圈这类页面时，左侧会话
        列表和搜索框**仍然全在树里**，``_chat_page_ready`` 会判成“已就绪”而跳过
        点击；其实右侧根本没有聊天输入框，接下来 ``_chat_input`` 只能全树遍历，
        表现就是发送迟迟不返回。要发消息就不能靠“树里有没有会话列表”猜页面。
        """
        if self._win is None and not self.ensure_window():
            return False
        if self._win.ClassName in CHAT_WINDOW_CLASSES + HISTORY_WINDOW_CLASSES:
            return (not require_list and self._chat_input() is not None)
        bar = _find_by(self._win, lambda c: (c.ClassName or "") == MAIN_TAB_BAR_CLS,
                       max_depth=20)
        if bar is None:
            wxlog.debug("未找到 %s，跳不回聊天页", MAIN_TAB_BAR_CLS)
            return False
        item = _find_by(bar, lambda c: (c.ClassName or "") == TAB_ITEM_CLS
                        and (c.Name or "").strip() == CHAT_TAB_NAME, max_depth=6)
        if item is None:
            item = _find_by(bar, lambda c: (c.ClassName or "") == TAB_ITEM_CLS,
                            max_depth=6)
        if item is None:
            wxlog.debug("%s 下没有 %s，跳不回聊天页", MAIN_TAB_BAR_CLS, TAB_ITEM_CLS)
            return False

        def _ready():
            if not require_list:
                return self._chat_page_ready(self._win)
            w = self._find_main() or self._win
            return _find_by(w, lambda c: _aid_hit(getattr(c, "AutomationId", ""),
                                                  SESSION_LIST_AIDS),
                            max_depth=30) is not None

        if _ready() and not force:
            # 会话列表/搜索框本来就在：一点都不用点。旧写法无条件先点一下，那一下
            # 在窄窗口里正好把用户开着的会话切走。
            wxlog.debug("聊天页那一片已经渲染，不点导航栏")
            return True
        # 走 _click_ctrl 而不是 Control.Click()：后者是裸 mouse_event，会被
        # 渲染层的 WS_EX_TRANSPARENT 挡掉（见 _click_at 上方注释）。
        for n in range(1, max(1, int(max_clicks)) + 1):
            wxlog.debug("点导航栏「%s」栏第 %d 次，确保主窗回到聊天页/退出当前会话",
                        CHAT_TAB_NAME, n)
            if not self._post_click_ctrl(item):
                return False
            rhythm.nap(settle)
            if _ready():
                if n > 1:
                    wxlog.info("点第 %d 次「%s」栏才退出会话（会话列表已渲染）",
                               n, CHAT_TAB_NAME)
                return True
        # 点满还不算退出：窄窗口开着会话时搜索框不在树里，这一步不补点的话
        # open_chat 的搜索框查找会直接失败，报出来的就是「会话打不开」。
        # 新布局（最大化 + 竖排导航）下常规点击会被渲染层吃掉，且切页后的
        # 会话列表是懒渲染：点一次后要按 2.5s 轮询判据，仍不行再点一次
        # （人手遇到没反应的 tab 也会再点一下）。
        for attempt in (1, 2):
            if not self._raw_click_ctrl(item):
                break
            deadline = time.time() + 2.5
            while time.time() < deadline:
                rhythm.nap(0.35)
                if _ready():
                    wxlog.info("常规点击切不回聊天页，按拟人落点直接点击 %d 次后成功",
                               attempt)
                    return True
        wxlog.warning("点了 %d 次导航栏「%s」，会话列表和搜索框还是不在树里——"
                      "主窗大概率还开着会话（窄窗口点一次退不出去），"
                      "后面按搜索框/会话列表走的动作会失败",
                      max(1, int(max_clicks)), CHAT_TAB_NAME)
        return False

    def open_chat(self, keyword: str, index: Optional[int] = None,
                  section: Optional[str] = None, retries: int = 2,
                  deadline: float = 0) -> bool:
        """搜索并打开联系人/群聊；独立窗只接受当前会话。返回是否成功。

        ``deadline`` 是绝对时间戳（0 = 不限）：群发时用来限制一个打不开的
        目标把整轮拖太久——搜索的每轮重试之间检查一次。

        传入 username/wxid 时自动映射为昵称/备注/微信号再搜索（微信搜索框
        不认 wxid）。搜索框残留会影响命中，每次打开前先清空重试。
        """
        if not self.ensure_window(activate=False):
            return False
        win = self._win
        if win.ClassName in HISTORY_WINDOW_CLASSES:
            return False
        if win.ClassName in CHAT_WINDOW_CLASSES:
            # 独立窗固定属于一个会话。原生 ID 来自该根节点，不跨账号查库映射。
            aid = win.AutomationId or ''
            username = aid[len('ChatSingleWindow'):] if aid.startswith('ChatSingleWindow') else ''
            matched = bool(keyword and keyword in (self.current_chat(), username))
            if not matched:
                wxlog.warning('独立聊天窗口不支持切换到其他会话')
            return matched
        # 搜索框只在聊天页渲染：主窗停在朋友圈页、或窄窗口里还开着会话时，搜索框
        # 根本不在树里。这里先把界面带回聊天页（必要时点两次退出会话）。
        if not self.back_to_chat_tab():
            wxlog.warning("没能带回聊天页（窄窗口里会话可能还开着），"
                          "下面这步找不到搜索框就会失败")
        win = self._win
        box = self._search_box(win, expand=True)
        if box is None:
            return False

        def _try_words(words):
            """按顺序试候选词，返回 (命中的结果列表, 命中的词)。"""
            for word in words:
                for attempt in range(max(1, retries)):
                    if deadline and time.time() > deadline:
                        wxlog.warning('搜索 %r 超过时限，放弃', keyword)
                        return [], keyword
                    if attempt == 0:
                        # 优先 SetValue：点击一旦没抢到焦点，Ctrl+V 就会把词
                        # 打进当时聚焦的别的控件里（实测落进过聊天输入框）。
                        wrote = self._set_text(box, word)
                        if not wrote:
                            self._paste_into(box, word, clear=True)
                    else:
                        # 优先 WM_CHAR：消息投递，不需要窗口在前台、不移动光标，
                        # 实测能触发微信搜索（2026-11-19）。失败才回退真实粘贴。
                        wrote = self._type_into(box, word)
                        if not wrote:
                            self._paste_into(box, word, clear=True)
                    wxlog.debug("搜索词 %r 用 %s 写入", word,
                                "SetValue" if wrote else "粘贴")
                    rhythm.nap(0.8)
                    got = self._collect_results(word)
                    if got:
                        return got, word
                    rhythm.nap(0.4)
            return [], keyword

        words = self._search_keywords(keyword)
        wxlog.debug("搜索「%s」的候选词：%s", keyword, words)
        results, used_kw = _try_words(words)
        if not results and len(words) == 1:
            # 缓存词失效了（对方改名/改备注、或这是首次的最后一个候选）：
            # 清掉缓存重走完整候选，别因为一条过期缓存就判定「会话打不开」。
            self._search_kw_cache.pop(keyword, None)
            results, used_kw = _try_words(self._resolve_search_keyword(keyword))
        if results:
            self._search_kw_cache[keyword] = (used_kw, time.time())

        if not results:
            return False

        if section:
            filtered = [r for r in results
                        if (r["section"] or "") == section]
            if filtered:
                results = filtered
        if len(results) > 1:
            # 私聊优先「联系人」分区、群优先「群聊」分区。搜 alias 时微信会把
            # 聊过这个号的群一起带出来（实测搜 God-Cap 返回 1 个联系人 +
            # 3 个群聊），不分区的话可能选中群。
            want = "群聊" if keyword.endswith("@chatroom") else "联系人"
            preferred = [r for r in results if (r["section"] or "") == want]
            if preferred:
                results = preferred
        if len(results) > 1 and index is not None:
            filtered = [r for r in results if r["index"] == index]
            if filtered:
                results = filtered
        elif len(results) > 1:
            # 有精确名称命中时优先精确命中，否则用第一个
            exact = [r for r in results if r["name"] == used_kw]
            if exact:
                results = exact

        chosen = results[0]
        if not self._post_click_ctrl(chosen["cell"]):
            self._click_ctrl(chosen["cell"])
        # 点开会话是异步的：固定等 0.7 秒时 current_chat() 读到的可能还是旧
        # 会话，于是明明点开了却被判成失败（2026-11-14 实测第一次 open_chat
        # 返回 False，31.7 秒；第二次才 True）。按结果名轮询最多 3 秒。
        deadline = time.time() + 3.0
        last = None
        while time.time() < deadline:
            time.sleep(0.4)
            last = self.current_chat()
            if last and (last == chosen["name"] or last == used_kw):
                return True
        wxlog.debug("点开后会话名是 %r，期望 %r / %r", last,
                    chosen["name"], used_kw)
        return False

    # ------------------------------------------------------------------
    # 发送：写值 + PostMessage 点发送按钮
    # ------------------------------------------------------------------
    def _click_target_hwnd_at(self, x: int, y: int) -> int:
        """找覆盖该屏幕点、**面积最小**的微信顶层窗口（弹层优先于主窗口）。

        微信的搜索下拉、@ 弹层都是独立的顶层窗口（实测搜索弹层是
        ``Qt51514QWindowToolSaveBits``，单独一个 hwnd）。一律投给主窗口会让
        坐标落在主窗口的会话列表上，把会话切到别处。这里枚举微信进程的全部
        可见顶层窗口，按面积从小到大取，不依赖窗口是否在前台——``WindowFromPoint``
        在窗口被遮挡时返回的是遮挡窗口，不能用。
        """
        u = ctypes.windll.user32
        if self._win is None:
            return 0
        from wechatauto.utils.win32 import enum_windows_by_pid
        pid = ctypes.wintypes.DWORD()
        u.GetWindowThreadProcessId(self._win.NativeWindowHandle, ctypes.byref(pid))
        best = []
        for hwnd in enum_windows_by_pid(int(pid.value)):
            rr = ctypes.wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(rr))
            if rr.left <= x <= rr.right and rr.top <= y <= rr.bottom:
                area = (rr.right - rr.left) * (rr.bottom - rr.top)
                best.append((area, hwnd))
        if not best:
            return self._win.NativeWindowHandle
        best.sort()
        return int(best[0][1])

    def _post_click_at(self, x: int, y: int, hwnd: int = 0) -> bool:
        """把鼠标点击消息投递给指定窗口，不移动真实光标。

        ``hwnd`` 为空时按落点自动选最具体的微信窗口（弹层优先），这是搜索
        结果、@ 弹层能点中的前提。
        """
        u = ctypes.windll.user32
        target = hwnd or self._click_target_hwnd_at(x, y)
        if not target:
            return False
        client = ctypes.wintypes.POINT(int(x), int(y))
        u.ScreenToClient(target, ctypes.byref(client))
        lp = (client.y << 16) | (client.x & 0xFFFF)
        u.PostMessageW(target, 0x0201, 1, lp)
        time.sleep(0.04)
        u.PostMessageW(target, 0x0202, 0, lp)
        return True

    def _ctrl_root_hwnd(self, ctrl) -> int:
        """控件所属顶层窗口的句柄；取不到时退回主窗口。"""
        for getter in (lambda: ctrl.top_level_parent().handle,
                       lambda: ctrl.NativeWindowHandle):
            try:
                h = getter()
            except Exception:
                continue
            if h:
                root = ctypes.windll.user32.GetAncestor(int(h), 2)  # GA_ROOT
                return int(root or h)
        return self._win.NativeWindowHandle if self._win is not None else 0

    def _post_click_ctrl(self, ctrl) -> bool:
        """点控件矩形中心（消息投递，不碰真实鼠标、不按落点反查窗口）。"""
        try:
            r = ctrl.BoundingRectangle
        except Exception:
            return False
        if not r or r.width() <= 0 or r.height() <= 0:
            return False
        # 不指定 hwnd：按落点自动选最具体的微信窗口（搜索/＠ 弹层优先）。
        # 微信控件收到点击会自己激活窗口（拦不住），所以每一次控件点击之后
        # 都立刻把前台还给点击前的窗口；放在这里而不是各调用点，是为了不
        # 漏掉搜索框、搜索弹层、@ 弹层这些零散的点击。
        prev_fg = ctypes.windll.user32.GetForegroundWindow()
        ok = self._post_click_at((r.left + r.right) // 2,
                                 (r.top + r.bottom) // 2)
        self._restore_foreground(prev_fg)
        return ok

    def _restore_foreground(self, prev_fg: int) -> None:
        """把前台还给用户的窗口。

        微信控件收到鼠标消息后会自己 ``SetForegroundWindow``（实测
        ``Invoke`` / ``Select`` / ``PostMessage`` 三种都触发，``WS_EX_NOACTIVATE``
        也拦不住），所以在每次点击之后立刻把前台还回去。

        ``prev_fg`` 不可靠：连续点击时它常常已经是微信（上一轮点击留下的），
        所以当它是微信或为空时改用 ``_user_fg``——初始化时记住的那个用户窗口。
        """
        wx_h = self._win.NativeWindowHandle if self._win is not None else 0
        if not prev_fg or prev_fg == wx_h:
            prev_fg = getattr(self, '_user_fg', 0)
        if not prev_fg or prev_fg == wx_h:
            return
        u = ctypes.windll.user32
        if u.GetForegroundWindow() == prev_fg:
            return
        time.sleep(0.2)
        try:
            cur = ctypes.windll.kernel32.GetCurrentThreadId()
            tid = u.GetWindowThreadProcessId(prev_fg, None)
            if tid:
                u.AttachThreadInput(cur, tid, True)
            u.SetForegroundWindow(prev_fg)
            if tid:
                u.AttachThreadInput(cur, tid, False)
        except Exception:
            pass

    def _send_button(self):
        """定位「发送」按钮；30 秒内复用结果，避免每次全树遍历。"""
        cache = getattr(self, '_send_btn_cache', None)
        if cache is not None and time.time() - cache[1] < 30.0:
            return cache[0]
        btn = _find_by(self._win,
                       lambda c: (c.ControlTypeName == 'ButtonControl'
                                  and (c.Name or '') == '发送'),
                       max_depth=30, max_nodes=4000)
        if btn is not None:
            self._send_btn_cache = (btn, time.time())
        return btn

    def send_via_button(self, text: str) -> bool:
        """用 UIA 写值 + PostMessage 点发送按钮发一条文本。

        与逐字键入相比不移动真实鼠标、不逐字打字；代价是微信自己在写值和
        点击时激活窗口一次（实测 Invoke/Select/PostMessage 都会触发，是
        微信控件自身的行为，外部拦不住）。发送结束后把前台还给原来的窗口，
        让用户只看到一次短暂闪烁而不是被切走。
        """
        self._send_attempted = False
        if not self.ensure_window(activate=False):
            return False
        u = ctypes.windll.user32
        prev_fg = u.GetForegroundWindow()
        e = self._chat_input()
        if e is None:
            return False
        self._post_click_ctrl(e)
        time.sleep(0.3)
        try:
            e.GetValuePattern().SetValue(text)
        except Exception as exc:
            wxlog.debug('SetValue 写入失败：%s', exc)
            return False
        time.sleep(0.25)
        ok, got, score = self._paste_landed(e, text)
        if not ok:
            self._chat_input_cache = None
            wxlog.warning(
                'SetValue 未落进输入框（相似度 %.2f < 阈值 %.2f），不发送；读回=%r',
                score, float(WxParam.SEND_CONTENT_RATIO), (got or '')[:80])
            try:
                e.GetValuePattern().SetValue('')
            except Exception:
                pass
            return False
        btn = self._send_button()
        self._send_attempted = True
        if btn is None:
            wxlog.warning('找不到发送按钮，回退回车')
            try:
                e.SendKeys("{Enter}", waitTime=0.05)
            except Exception:
                return False
        elif not self._post_click_ctrl(btn):
            return False
        # 微信激活窗口后把前台还回去：不改窗口位置，只恢复焦点归属
        if prev_fg and prev_fg != self._win.NativeWindowHandle:
            self._restore_foreground(prev_fg)
        return True

    def send_text(self, text: str) -> bool:
        """在已打开会话的输入框发送文本。返回是否成功。

        ``WxParam.SEND_VIA_POSTMESSAGE`` 打开时走 :meth:`send_via_button`
        （写值 + PostMessage 点发送按钮），否则走原来的逐字键入 + 回车。

        回车之前先回读输入框内容比对（阈值 ``WxParam.SEND_CONTENT_RATIO``）：
        Ctrl+V 有可能整个落空（焦点没落到输入框上），这时按键照样打下去，
        发出去的会是空消息或上一轮残留的内容。判不达标就不回车并清空。
        """
        if WxParam.SEND_VIA_POSTMESSAGE:
            if self.send_via_button(text):
                return True
            wxlog.debug('send_via_button 未成功，回退逐字键入 + 回车')
        self._send_attempted = False
        if not self.ensure_window():
            return False
        e = self._chat_input()
        if e is None:
            return False
        self._click_ctrl(e)
        try:
            e.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
        except Exception:
            pass
        # 逐字键入正文（不再整段 Ctrl+V）；先聚焦输入框，再按拟人节奏打。
        rhythm.type_text(text)
        rhythm.gate('send')
        rhythm.nap(0.2)
        ok, got, score = self._paste_landed(e, text)
        if not ok:
            # 上次定位到的控件可能已随微信重绘失效，下次重新找，别一直用死缓存
            self._chat_input_cache = None
            wxlog.warning(
                "内容未落进输入框（相似度 %.2f < 阈值 %.2f），不回车；读回=%r",
                score, float(WxParam.SEND_CONTENT_RATIO), (got or "")[:80])
            try:
                e.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            except Exception:
                pass
            return False
        try:
            # SendKeys 抛异常也可能已经投递回车，调用方不能据 False 重新发送。
            self._send_attempted = True
            e.SendKeys("{Enter}", waitTime=0.05)
        except Exception:
            return False
        return True

    def send_text_to(self, text: str, who: str) -> bool:
        """打开会话并发送文本（组合动作）。"""
        if self.current_chat() == who:
            return self.send_text(text)
        if not self.open_chat(who):
            return False
        return self.send_text(text)

    def voice_call(self, who: Optional[str] = None, video: bool = False) -> bool:
        """发起语音/视频通话（点击标题栏通话按钮 → 选择菜单项）。

        微信 4.x 标题栏暴露 ``mmui::ChatVoIPView`` 下的 ``voip_button``
        （aid=voip_button）。点击后弹出 ``mmui::XMenuView`` 菜单，含
        「语音通话」「视频通话」两个 ``MenuItemControl``（aid=XMenuItem）；
        需再点击目标菜单项才真正发起通话。video=True 时选「视频通话」，
        否则选「语音通话」。
        """
        if not self.ensure_window():
            return False
        if who and not self.current_chat() == who:
            if not self.open_chat(who):
                return False
        win = self._win
        if win is None:
            return False
        # voip_button 控件树会动态重建，需重试定位
        btn = None
        for _ in range(6):
            try:
                btn = win.ButtonControl(AutomationId="voip_button")
                if btn.Exists(0.6, 0.2):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not btn or not btn.Exists(0):
            return False
        rhythm.gate('call')
        try:
            self._click_ctrl(btn)
            rhythm.nap(0.8)
        except Exception:
            return False
        # 菜单里选择 语音/视频 通话项
        target_name = "视频通话" if video else "语音通话"
        for _ in range(4):
            try:
                item = win.MenuItemControl(AutomationId="XMenuItem", Name=target_name)
                if item.Exists(0.5, 0.2):
                    self._click_ctrl(item)
                    time.sleep(0.5)
                    return True
            except Exception:
                pass
            # 菜单可能延迟出现或树重建，重试
            time.sleep(0.4)
        return False

    def _find_friend_row(self):
        """找一条对方（friend）消息行控件，供右键头像触发拍一拍。

        消息行 rect 是全宽，方向需按行内内容重心判断（friend 内容靠左，
        self 靠右）。返回 friend 消息行控件。
        """
        lst = self._message_list()
        if lst is None:
            return None
        try:
            from PIL import ImageGrab as IG
            import numpy as np
        except Exception:
            return None
        # 可视区（消息列表内部矩形），过滤掉滚出可视区的行
        lr = lst.BoundingRectangle
        vis_top, vis_bottom = lr.top, lr.bottom
        # 优先文字行，其次其他内容行（动画表情/图片/引用等）
        rows = list(lst.GetChildren())
        for ch in sorted(rows, key=lambda c: c.ClassName != "mmui::ChatTextItemView"):
            try:
                cn = ch.ClassName or ""
                if not cn.startswith("mmui::Chat"):
                    continue
                if cn in ("mmui::ChatItemView", "mmui::ChatSystemInfoItemView"):
                    continue
                r = ch.BoundingRectangle
                if r.bottom - r.top < 40:
                    continue
                # 行必须在可视区内（含部分露出），排除被遮挡/滚出的行
                if r.top >= vis_bottom or r.bottom <= vis_top:
                    continue
                img = IG.grab(bbox=(r.left, r.top, r.right, r.bottom))
                arr = np.array(img.convert("RGB"))
                bg = (arr.max(axis=2) > 235) & ((arr.max(axis=2) - arr.min(axis=2)) < 22)
                colc = (~bg).sum(axis=0)
                total = int(colc.sum())
                if total < 20:
                    continue
                weighted = sum(x * colc[x] for x in range(len(colc))) / total
                if weighted < arr.shape[1] * 0.5:
                    return ch
            except Exception:
                continue
        return None

    def poke(self, who: Optional[str] = None) -> bool:
        """对联系人发起「拍一拍」（右键头像 → 点击拍一拍菜单项）。

        微信 4.x 的拍一拍只能通过右键聊天中对方头像触发，菜单为自绘
        不暴露 UIA；因此用「右键头像 + 全屏 OCR 定位拍一拍文字」实现。
        需要 winsdk OCR 可用。
        """
        if not self.ensure_window():
            return False
        if who and not self.current_chat() == who:
            if not self.open_chat(who):
                return False
        row = self._find_friend_row()
        if row is None:
            return False
        try:
            r = row.BoundingRectangle
        except Exception:
            return False
        # 头像位于消息行最左侧约 40-50px 处
        ax = int(r.left + rhythm.spread(62, 80))
        ay = int(rhythm.spread(r.top + r.height() * 0.35,
                               r.top + r.height() * 0.65))
        try:
            from wechatauto.guia import ScreenOCR
            import PIL.ImageGrab as IG
        except Exception:
            return False
        rhythm.gate('poke')
        for attempt in range(2):
            self._set_cursor(ax, ay)
            rhythm.nap(0.2)
            self._right_click()
            rhythm.nap(1.0)
            img = IG.grab()
            res = ScreenOCR.recognize(img)
            for text, x, y, w, h in res:
                t = (text or "").replace(" ", "")
                if "拍一拍" in t or t == "拍一" or t.startswith("拍一"):
                    # 点在该菜单项矩形内部的一个随机点
                    cx, cy = rhythm.point((x, y, x + w, y + h))
                    self._set_cursor(cx, cy)
                    rhythm.nap(0.2)
                    self._left_click()
                    rhythm.nap(0.5)
                    return True
        return False

    def _right_click_latest_row(self, who: Optional[str] = None) -> Optional[Tuple[int, int]]:
        """打开会话并右键最新一条消息行，返回气泡内右键坐标 (x, y)；失败返回 None。"""
        if not self.ensure_window():
            return None
        if who and not self.current_chat() == who:
            if not self.open_chat(who):
                return None
        lst = self._message_list()
        if lst is None:
            return None
        rows = list(lst.GetChildren())
        # 取可视区内最底部（最新）的消息行；时间分隔行 ChatItemView 跳过
        candidates = []
        for ch in rows:
            try:
                cn = ch.ClassName or ""
                if not cn.startswith("mmui::Chat"):
                    continue
                if cn in ("mmui::ChatItemView", "mmui::ChatSystemInfoItemView"):
                    continue
                r = ch.BoundingRectangle
                if r.bottom - r.top < 40:
                    continue
                candidates.append((ch, r))
            except Exception:
                continue
        if not candidates:
            return None
        # 最新消息在可视区底部（消息列表打开即定位在最新），取 bottom 最大者
        target = max(candidates, key=lambda t: t[1].bottom)
        ch, r = target
        # 消息行内取内容重心 x（self 靠右、friend 靠左），y 在行的中段随机取
        try:
            from PIL import ImageGrab as IG
            img = IG.grab(bbox=(r.left, r.top, r.right, r.bottom))
            import numpy as np
            arr = np.array(img.convert("RGB"))
            bg = (arr.max(axis=2) > 235) & ((arr.max(axis=2) - arr.min(axis=2)) < 22)
            colc = (~bg).sum(axis=0)
            total = int(colc.sum())
            if total >= 20:
                cx = sum(x * colc[x] for x in range(len(colc))) / total
                cx = int(r.left + cx)
            else:
                cx = (r.left + r.right) // 2
        except Exception:
            cx = (r.left + r.right) // 2
        cy = int(rhythm.spread(r.top + r.height() * 0.35,
                               r.top + r.height() * 0.65))
        self._set_cursor(cx, cy)
        rhythm.nap(0.2)
        self._right_click()
        rhythm.nap(1.0)
        return (cx, cy)

    def _uia_find_menu_item(self, name_sub: str, max_depth: int = 6):
        """在主窗口树内查找菜单项控件（UIA 方案）。

        微信 4.x 右键菜单在热激活后物化为 UIA 节点：``mmui::XMenu`` 下挂
        ``mmui::XMenuView``（Name 即菜单文字）。只遍历主窗口子树，避免
        触发 Windows UIA 根遍历的系统挂起 bug。返回匹配的控件或 None。
        """
        w = self._win
        if w is None:
            return None
        found = [None]

        def walk(c, d=0):
            if found[0] is not None or d > max_depth:
                return
            try:
                children = c.GetChildren()
            except Exception:
                return
            for ch in children:
                try:
                    nm = ch.Name or ""
                    cn = ch.ClassName or ""
                except Exception:
                    nm = cn = ""
                if cn == "mmui::XMenuView" and name_sub in nm:
                    found[0] = ch
                    return
                walk(ch, d + 1)

        try:
            walk(w)
        except Exception:
            return None
        return found[0]

    def _uia_click_menu_item(self, ctrl) -> bool:
        """通过 UIA Invoke/LegacyIAccessible 或鼠标点击菜单项控件。"""
        for method in ("Invoke", "Select", "Expand"):
            try:
                getattr(ctrl, method)()
                time.sleep(0.3)
                return True
            except Exception:
                continue
        try:
            r = ctrl.BoundingRectangle
            cx, cy = rhythm.point((r.left, r.top, r.right, r.bottom))
            self._set_cursor(cx, cy)
            rhythm.nap(0.2)
            self._left_click()
            rhythm.nap(0.3)
            return True
        except Exception:
            return False

    def recall_last_message(self, who: Optional[str] = None) -> bool:
        """撤回当前会话最新一条自己发送的消息。

        UIA 方案优先：右键消息行后，主窗口树内 ``mmui::XMenuView`` 已物化
        菜单项（Name 含「撤回」），直接定位点击；若菜单项为「删除」（消息
        超过 2 分钟撤回时限）则返回失败。UIA 不可用/未命中时降级到 OCR
        （全屏识别「撤回」文字定位点击）。两者都失败返回 False。
        """
        rhythm.gate('recall')
        pos = self._right_click_latest_row(who)
        if pos is None:
            return False
        # UIA 方案
        item = self._uia_find_menu_item("撤回")
        if item is not None:
            if self._uia_click_menu_item(item):
                return True
        # OCR 兜底
        try:
            from wechatauto.guia import ScreenOCR
            import PIL.ImageGrab as IG
        except Exception:
            return False
        for attempt in range(2):
            if attempt > 0:
                cx, cy = pos
                self._set_cursor(cx, cy)
                rhythm.nap(0.2)
                self._right_click()
                rhythm.nap(1.0)
            img = IG.grab()
            res = ScreenOCR.recognize(img)
            for text, x, y, w, h in res:
                t = (text or "").replace(" ", "")
                if "撤回" in t or t == "撤回":
                    cxx, cyy = rhythm.point((x, y, x + w, y + h))
                    self._set_cursor(cxx, cyy)
                    rhythm.nap(0.2)
                    self._left_click()
                    rhythm.nap(0.5)
                    return True
        return False

    # ------------------------------------------------------------------ 剪贴板粘贴
    def _set_text(self, ctrl, text: str) -> bool:
        """用 UIA ValuePattern 直接写文本：不动鼠标、不发按键，返回是否写成。

        只给搜索框这类「填完就等它自己出结果」的控件用。聊天输入框不走这条
        （见 :meth:`_paste_into`）：SetValue 不会让 Qt 控件获得焦点，而发送靠
        的是回车键落到那个焦点上。
        """
        try:
            vp = ctrl.GetValuePattern()
            if vp is None or vp.IsReadOnly:
                return False
            vp.SetValue(text or "")
        except Exception:
            return False
        time.sleep(0.1)
        return True

    def _type_into(self, ctrl, text: str) -> bool:
        """用 WM_CHAR 往控件里逐字投递，不移动光标、不要求窗口在前台。

        先用 ValuePattern 清空（它只改值、不触发搜索），再点一下控件拿焦点，
        然后按 UTF-16 码元投递 WM_CHAR。投完读回确认。换行与 emoji 会被
        微信丢弃（2026-11-19 实测），这类文本由调用方改用别的路径。
        """
        if not text:
            return True
        try:
            ctrl.GetValuePattern().SetValue('')
        except Exception:
            pass
        if not self._post_click_ctrl(ctrl):
            return False
        time.sleep(0.15)
        hwnd = self._win.NativeWindowHandle
        if not hwnd:
            return False
        data = text.encode('utf-16-le')
        for i in range(0, len(data), 2):
            cp = int.from_bytes(data[i:i + 2], 'little')
            ctypes.windll.user32.PostMessageW(hwnd, 0x0102, cp, 1)
            # 间隔太短会丢字：0.01s 投 10 个字只进去 3 个（2026-11-19 实测）。
            time.sleep(0.035)
        time.sleep(0.25)
        got = self._read_edit_value(ctrl)
        return bool(got) and text in (got or '')

    def _paste_into(self, ctrl, text: str, clear: bool = True) -> None:
        """点击控件拿焦点，再走剪贴板 Ctrl+V（发送类输入框用这条）。"""
        self._click_ctrl(ctrl)
        rhythm.nap(0.1)
        if clear:
            try:
                ctrl.SendKeys("{Ctrl}a{Delete}", waitTime=0.05)
            except Exception:
                pass
        self._clip_set(text)
        try:
            ctrl.SendKeys("{Ctrl}v", waitTime=0.05)
        except Exception as e:
            # 落不进去时由 send_text 的回读校验拦下回车，这里不静默当作成功。
            wxlog.debug("Ctrl+V 投递异常：%s", e)

    @staticmethod
    def _read_edit_value(ctrl) -> Optional[str]:
        """读输入框当前内容；控件不暴露值时返回 None（表示"读不到"，不是"空"）。

        本依赖版本的 ``Control`` 没有 ``GetValue()``，只能走 pattern：
        ``ValuePattern`` 优先，``LegacyIAccessiblePattern`` 兜底。
        """
        try:
            vp = ctrl.GetValuePattern()
            if vp is not None:
                return vp.Value
        except Exception:
            pass
        try:
            ap = ctrl.GetLegacyIAccessiblePattern()
            if ap is not None:
                return ap.Value
        except Exception:
            pass
        return None

    @staticmethod
    def _compare_norm(text: str) -> str:
        """比对用归一化：折叠空白，并把表情码折算成输入框里的占位符。

        实测（微信 4.1.15.13 / ``mmui::ChatInputField``）：传入的 ``[微笑]`` 在
        输入框里是 1 个 U+FFFC 对象替换符，整串表情码不在了。两侧都折算才能对上，
        否则「好的[微笑]」只有 0.444，会被阈值误拦。大小写、全半角、标点的
        差异不作废——那些是真差异。
        """
        return _STICKER_CODE.sub("￼", "".join((text or "").split()))

    @classmethod
    def _paste_landed(cls, ctrl, text: str) -> Tuple[bool, str, float]:
        """回读输入框并与待发内容比相似度。返回 (是否达标, 读回值, 相似度)。

        读不回值时判达标 —— 没有探测能力不等于内容错了。若把"读不到"当失败，
        控件改版哪天不暴露 ValuePattern 了，发送功能会全线锁死。
        """
        need = float(getattr(WxParam, "SEND_CONTENT_RATIO", 0.6) or 0)
        if need <= 0:
            return True, "", 1.0
        got = cls._read_edit_value(ctrl)
        if got is None:
            wxlog.debug("输入框不暴露内容，跳过发送前回读校验")
            return True, "", 1.0
        want = cls._compare_norm(text)
        have = cls._compare_norm(got)
        if not want:
            return True, got, 1.0
        score = difflib.SequenceMatcher(None, want, have, autojunk=False).ratio()
        return score >= need, got, score

    # ------------------------------------------------------------------ 消息列表定位
    def _message_list(self, win=None):
        """定位当前会话的消息列表控件（RecyclerListView / chat_message_list）。"""
        win = win or self._win
        if win is None:
            return None
        # 记录窗和聊天窗使用不同 aid；优先明确锚点，避免拿到其他虚拟列表。
        hit = _find_by(win, lambda c: c.ClassName == 'mmui::RecyclerListView'
                       and _aid_hit(getattr(c, 'AutomationId', ''),
                                    ('chat_message_list', 'chat_log_message_list')))
        if hit is not None:
            return hit

        def walk(c, d=0):
            if d > 15:
                return None
            for ch in c.GetChildren():
                try:
                    cn = ch.ClassName or ""
                except Exception:
                    continue
                if cn == "mmui::RecyclerListView":
                    return ch
                r = walk(ch, d + 1)
                if r is not None:
                    return r
            return None

        return walk(win)

    def find_in_message_list(self, predicate, match_last: bool = False,
                             max_scrolls: int = 40) -> Optional[Tuple]:
        """在消息列表中按谓词查找消息控件，返回 (className, name, rect)。

        RecyclerListView 是虚拟化列表，只实例化可视区约 12 条；历史消息需
        滚动。ScrollPattern 不可用（返回空），改用鼠标滚轮驱动列表滚动。
        match_last=True 时滚到底后从底部向上找，用于取最新表情/消息。
        """
        lst = self._message_list()
        if lst is None:
            return None

        def scroll(direction: str, times: int = 1) -> None:
            # direction: 'up'=滚向最新(底部), 'down'=滚向历史(更早)
            # 实测（微信4.1.12.26）：open_chat 后消息列表定位在底部(最新)，
            # 滚轮 +120 滚向历史(更早)，-120 滚向最新。
            try:
                r = lst.BoundingRectangle
                cx = (r.left + r.right) // 2
                cy = (r.top + r.bottom) // 2
                self._set_cursor(cx, cy)
                delta = -120 if direction == 'up' else 120
                for _ in range(times):
                    self._mouse_wheel(delta)
                    time.sleep(0.2)
            except Exception:
                pass

        def visible():
            out = []
            for ch in lst.GetChildren():
                try:
                    cn = ch.ClassName or ""
                    nm = ch.Name or ""
                    if cn == "mmui::ChatItemView":  # 时间分隔行跳过
                        continue
                    if predicate(cn, nm):
                        out.append((cn, nm, ch))
                except Exception:
                    continue
            return out

        # 列表定位在底部(最新)。match_last 直接取当前可视区最匹配的一条
        # （最新消息已实例化），没有才向上(滚向历史)翻找。
        if match_last:
            got = visible()
            if got:
                ch = got[-1][2]
                try:
                    r = ch.BoundingRectangle
                    return (got[-1][0], got[-1][1], r)
                except Exception:
                    pass
            for _ in range(max_scrolls):
                got = visible()
                if got:
                    ch = got[-1][2]
                    try:
                        r = ch.BoundingRectangle
                        return (got[-1][0], got[-1][1], r)
                    except Exception:
                        pass
                scroll('down', 2)  # 滚向历史找更早匹配
            return None

        # 非 match_last：从底部(最新)向历史逐屏扫描
        seen = set()
        for _ in range(max_scrolls):
            for cn, nm, ch in visible():
                try:
                    r = ch.BoundingRectangle
                    key = (r.left, r.top, r.right, r.bottom)
                except Exception:
                    continue
                if key in seen:
                    continue
                seen.add(key)
                if predicate(cn, nm):
                    return (cn, nm, r)
            scroll('down', 2)
        return None

    # ------------------------------------------------------------------ 调试
    def dump(self, max_depth: int = 16, max_nodes: int = 1500):
        """打印主窗口 UIA 树（控件名失效时重新勘察）。"""
        if not self.ensure_window():
            print("UIA 树不可用")
            return
        win = self._win
        cnt = [0]

        def walk(c, d=0):
            if d > max_depth or cnt[0] > max_nodes:
                return
            for k in c.GetChildren():
                if cnt[0] > max_nodes:
                    break
                cnt[0] += 1
                try:
                    print("  " * d + f"{k.ControlTypeName} class={k.ClassName!r} "
                          f"name={(k.Name or '')[:30]!r} aid={k.AutomationId!r}")
                except Exception:
                    continue
                walk(k, d + 1)

        walk(win)
