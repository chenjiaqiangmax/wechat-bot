# -*- coding: utf-8 -*-
"""uiacli 命令行桥的 Python 封装。

识别读通道统一走 tools/uiacli/publish/uiacli.exe（WinUia 库编译产物，
接口契约见 tools/uiacli/README.md）。每次调用一个短进程，JSON 单行
回 stdout；失败时返回空结构而不是抛异常，由调用方降级处理。

投递层（PostMessage 点击 / WM_CHAR 逐字）不在这里——那是纯 ctypes，
直接在 guia.py 里做；本模块只负责「读」。
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Dict, List, Optional, Tuple

_EXE_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 'tools', 'uiacli', 'publish', 'uiacli.exe'),
)


def _exe() -> Optional[str]:
    for p in _EXE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _run(args: List[str], timeout: float = 8.0) -> Optional[Dict]:
    """调 uiacli 子进程；不可用/超时/坏 JSON 一律返回 None。"""
    exe = _exe()
    if not exe:
        return None
    try:
        r = subprocess.run([exe] + [str(a) for a in args],
                           capture_output=True, text=True, timeout=timeout,
                           encoding='utf-8', errors='replace',
                           # 父进程 pythonw 无控制台，控制台子进程会自弹黑窗
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def available() -> bool:
    return _exe() is not None


def tree(hwnd: int) -> Optional[Dict]:
    """树规模探测：nodes/has_input/has_search_list。"""
    return _run(['tree', hwnd])


def read_input(hwnd: int) -> Optional[Dict]:
    """读聊天输入框：found/name(会话名)/value(草稿内容)。"""
    return _run(['read-input', hwnd])


def find(hwnd: int, kind: str, key: str) -> Optional[Dict]:
    """按 AutomationId→Name 找控件；rect 为屏幕 [l,t,r,b]。

    kind/key 一并回填进结果，供 write_value 复用同一目标。
    """
    d = _run(['find', hwnd, kind, key])
    if d and d.get('found'):
        d['kind'] = kind
        d['key'] = key
    return d


def list_items(hwnd: int, container_aid: str, max_n: int = 12
               ) -> List[Tuple[str, str, Optional[List[int]]]]:
    """列容器子项（name, aid, rect）；容器不存在返回空表。

    rect 为屏幕 [l,t,r,b]，离屏行可能缺（None）——弹层行 aid 常为空，
    点击直接用行 rect。
    """
    d = _run(['list-items', hwnd, container_aid, max_n])
    if not d or not d.get('items'):
        return []
    return [(str(it.get('name') or ''), str(it.get('aid') or ''),
             it.get('rect') if isinstance(it.get('rect'), list) else None)
            for it in d['items']]

def write_value(hwnd: int, kind: str, key: str, text: str) -> bool:
    """按 find 同路径定位控件后 ValuePattern.SetValue（写路径唯一入口）。

    走 hwnd+树搜索而不是屏幕命中测试，被遮挡、其他进程占用 UIA COM
    时同样可用。
    """
    d = _run(['write-value', hwnd, kind, key, text])
    return bool(d and d.get('written'))
