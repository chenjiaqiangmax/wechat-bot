# -*- coding: utf-8 -*-
"""pytest 全局夹具：把文件日志关掉，别让测试往真实产出里写假记录。

``logger.py`` 的文件处理器是惰性初始化的（``_ensure_file_logger`` 见
``file_handler is None`` 才建），所以只要在任何一条日志写出去之前把
``WxParam.ENABLE_FILE_LOGGER`` 关掉，整轮测试就不会创建
``wechatauto_logs/app_<日期>.log``。

为什么必须这么做：这些日志是排查线上问题的唯一依据。测试用
``object.__new__(WeChatGUI)`` 造替身，会打出
「初始化 UIA 引擎失败：'WeChatGUI' object has no attribute 'main_hwnd'」
和「SetValue 写入失败：'_FakeElement' object has no attribute
'GetValuePattern'」这类只可能来自替身的记录，混进当天的真实日志后
没法区分「机器人真的坏了」还是「刚才跑了一遍测试」。
"""
import wechatauto.param as _param

_param.WxParam.ENABLE_FILE_LOGGER = False