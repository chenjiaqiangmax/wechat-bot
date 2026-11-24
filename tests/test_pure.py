"""wechatauto 纯逻辑层最小单测（不触微信、不开窗口、不动鼠标键盘）。

运行方式（任意装有依赖的 Python，在本目录）::

    python -m unittest discover -s tests -v

覆盖对象都是「输入→输出确定」的纯函数：密钥校验、内容格式化、
AutomationId 容错匹配、点赞名单解析、节奏档位不变量。
UI 驱动、DB 解密等需要真机/真库的部分不在本文件范围。
"""

import hashlib
import hmac as hmac_mod
import os
import struct
import sys
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from wechatauto.db import (  # noqa: E402
    PAGE_SZ,
    RESERVE_SZ,
    WeChatDB,
    _verify_enc_key,
)
from wechatauto.uia_driver import _aid_hit  # noqa: E402
from wechatauto import rhythm  # noqa: E402


def _make_page1(key: bytes, salt: bytes, body_seed: int = 7) -> bytes:
    """按 _verify_enc_key 的算法合成一页「合法」的 page1。

    布局：salt(16) + 密文区(至 PAGE_SZ-RESERVE_SZ+16) + ... + 尾部 HMAC(64)。
    """
    page = bytearray(PAGE_SZ)
    page[0:16] = salt
    for i in range(16, PAGE_SZ - RESERVE_SZ + 16):
        page[i] = (i * body_seed + 3) % 256
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    hm = hmac_mod.new(mac_key, bytes(page[16: PAGE_SZ - RESERVE_SZ + 16]),
                      hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    page[PAGE_SZ - 64: PAGE_SZ] = hm.digest()
    return bytes(page)


class TestVerifyEncKey(unittest.TestCase):
    KEY = bytes(range(32))
    SALT = bytes(range(16, 32))

    def setUp(self):
        self.page = _make_page1(self.KEY, self.SALT)

    def test_correct_key_and_header_salt(self):
        self.assertTrue(_verify_enc_key(self.KEY, self.page))

    def test_wrong_key_rejected(self):
        bad = bytes(range(1, 33))
        self.assertFalse(_verify_enc_key(bad, self.page))

    def test_48byte_raw_key_with_explicit_salt(self):
        self.assertTrue(_verify_enc_key(self.KEY + self.SALT, self.page))

    def test_explicit_salt_overrides_header(self):
        # 头部 salt 故意放垃圾，靠显式 salt 参数校验通过
        bad_header = bytearray(self.page)
        bad_header[0:16] = b"\x00" * 16
        self.assertTrue(_verify_enc_key(self.KEY, bytes(bad_header),
                                        salt=self.SALT))
        # 显式 salt 长度不对 ⇒ 直接拒绝
        self.assertFalse(_verify_enc_key(self.KEY, self.page, salt=b"\x00" * 8))

    def test_short_page_rejected(self):
        self.assertFalse(_verify_enc_key(self.KEY, self.page[:PAGE_SZ - 1]))


class TestProbableKey(unittest.TestCase):
    def test_good_key(self):
        self.assertTrue(WeChatDB._probable_key(bytes(range(32))))

    def test_wrong_length(self):
        self.assertFalse(WeChatDB._probable_key(bytes(range(31))))

    def test_low_entropy(self):
        self.assertFalse(WeChatDB._probable_key(b"\x01" * 32))


class TestAidHit(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(_aid_hit("session_list", ["session_list"]))

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(_aid_hit("  Session_List  ", ["SESSION_LIST"]))

    def test_dot_path_endswith(self):
        self.assertTrue(_aid_hit("MainView.main_tabbar.session_list",
                                 ["session_list"]))

    def test_candidate_is_dot_segment(self):
        # 方向：aid 为点分全路径，候选为其中一个短名段
        self.assertTrue(_aid_hit("MainView.main_tabbar.extra", ["main_tabbar"]))

    def test_no_hit(self):
        self.assertFalse(_aid_hit("session_list", ["chat_input_field", "search_list"]))

    def test_empty_inputs(self):
        self.assertFalse(_aid_hit("", ["session_list"]))
        self.assertFalse(_aid_hit("session_list", [None, ""]))


class TestFriendlyContent(unittest.TestCase):
    def test_plain_utf8(self):
        self.assertEqual(WeChatDB._friendly_content("你好".encode(), "文本"), "你好")

    def test_image_md5_extracted(self):
        # md5 提取只在 UTF-8 解码失败的二进制 blob 上进行（真实图片消息即如此）
        blob = b"\xff\xfe<msg><img md5=\"aabbccdd00112233445566778899eeff\"/>\x00"
        self.assertEqual(
            WeChatDB._friendly_content(blob, "图片"),
            "[图片 md5=aabbccdd00112233445566778899eeff]")

    def test_undecodable_falls_back_to_type(self):
        self.assertEqual(WeChatDB._friendly_content(b"\xff\xfe\x00\x01", "语音"),
                         "[语音]")


class TestSplitLikeNames(unittest.TestCase):
    def setUp(self):
        from wechatauto import moment
        self.moment = moment
        self.sep = moment._lang(moment.MOMENTS, "分隔符_点赞") or "，"

    def _names_text(self, names):
        return "赞：" + self.sep.join(names)

    def test_basic(self):
        self.assertEqual(self.moment._split_like_names(self._names_text(["张三", "李四"])),
                         ["张三", "李四"])

    def test_empty(self):
        self.assertEqual(self.moment._split_like_names(""), [])
        self.assertEqual(self.moment._split_like_names("   "), [])

    def test_drops_empty_parts(self):
        text = self.sep.join(["张三", "", "  ", "李四"])
        self.assertEqual(self.moment._split_like_names(text), ["张三", "李四"])

    def test_no_prefix_still_parses(self):
        self.assertEqual(self.moment._split_like_names(self.sep.join(["张三", "李四"])),
                         ["张三", "李四"])


class TestRhythmInvariants(unittest.TestCase):
    """节奏层「只加长不缩短」的不变量（rhythm.py 注释承诺）。"""

    def test_every_profile_nap_floor_ge_1(self):
        for name, prof in rhythm.PROFILES.items():
            lo, _hi = prof.nap
            self.assertGreaterEqual(lo, 1.0, f"档位 {name} 的 nap 下限 < 1.0")

    def test_off_profile_is_identity(self):
        old = rhythm.profile().name
        try:
            rhythm.set_profile("off")
            self.assertEqual(rhythm.spread(2.0, 5.0), 2.0)
            self.assertEqual(rhythm.nap(0.0), 0.0)  # base=0 不触发 sleep
        finally:
            rhythm.set_profile(old)

    def test_type_gap_pauses_then_returns(self):
        old = rhythm.profile().name
        try:
            rhythm.set_profile("off")
            self.assertEqual(rhythm.type_gap(), 0.04)  # off 档 = 原始固定值
        finally:
            rhythm.set_profile(old)


class TestSendRetry(unittest.TestCase):
    """``_send_retry``：抛异常必须算失败并重试，不能记成「已发出」。

    抓过的真 bug：旧实现 ``except`` 里把 ``r`` 置 ``None``，紧接着的
    ``if r is None or ...`` 直接命中成功分支，于是「截图全黑」这类真失败
    被记成「已发出」——消息丢了，日志里还看不出来。
    """

    def _bot(self):
        import threading
        import dafeiyu_bot as m
        bot = object.__new__(m.DaFeiYuBot)
        bot.send_lock = threading.Lock()
        # 这几步在真机上要等显示器/挪窗口/探测会话与桌面，测试里直接放行
        bot._wait_for_display = lambda timeout=20.0: True
        bot._ensure_window_on_display = lambda force=False: True
        bot._session_active = lambda: True
        bot._desktop_readable = lambda: True
        bot._uia_responsive = lambda timeout=2.0: True
        return bot

    def test_exception_counts_as_failure_and_retries(self):
        import dafeiyu_bot as m
        old = m.SEND_RETRY_WAIT
        m.SEND_RETRY_WAIT = 0
        try:
            calls = []
            bot = self._bot()

            def boom():
                calls.append(1)
                raise RuntimeError('截图全黑')

            ok, err = bot._send_retry(boom, '测试')
            self.assertFalse(ok)
            self.assertIn('截图全黑', err)
            self.assertEqual(len(calls), 1 + m.SEND_RETRIES)
        finally:
            m.SEND_RETRY_WAIT = old

    def test_first_try_success_stops_immediately(self):
        class _Resp:
            is_success = True

        bot = self._bot()
        calls = []
        ok, err = bot._send_retry(
            lambda: (calls.append(1), _Resp())[1], '测试')
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(len(calls), 1)

    def test_unconfirmed_send_stops_retrying(self):
        """「已尝试发送但结果未确认」不能重试：消息可能已经发出去了。

        2026-11-20 真机实测：回车后消息其实已入库，但像素级确认（输入框
        清空）没通过，旧实现当成普通失败又重发了两条，用户连撤两条。guia
        层的约定本来就是「投递后未确认不得重新提交」，这里必须终止本轮，
        答案留在待发，由恢复循环的 _reply_recorded 查库核对。
        """
        import dafeiyu_bot as m
        old = m.SEND_RETRY_WAIT
        m.SEND_RETRY_WAIT = 0
        try:
            calls = []

            class _Unconfirmed:
                is_success = False

                def __str__(self):
                    return "{'status': '失败', 'message': '" \
                           + m.UNCONFIRMED_SEND_MARK + \
                           "，为避免重复发送不再自动重试'}"

            bot = self._bot()
            ok, err = bot._send_retry(lambda: (calls.append(1), _Unconfirmed())[1],
                                      '回答')
            self.assertFalse(ok)
            self.assertIn(m.UNCONFIRMED_SEND_MARK, err or '')
            self.assertEqual(len(calls), 1,
                             '未确认的发送被重试了——这就是重复发送的来源')
        finally:
            m.SEND_RETRY_WAIT = old

    def test_uia_unresponsive_keeps_answer_pending(self):
        """UIA 不响应时不能进 SendMsg：算临时失败，答案留在待发表。

        真机实测：微信解码图片时一次 ``SendMsg`` 卡了 396 秒才返回，期间
        ``send_lock`` 被占死，后面所有回复一起堵（用户看到的就是「不会回复」）。
        已经进去的调用没法中断，所以只能在进去之前用探针拦住。
        """
        import dafeiyu_bot as m
        bot = self._bot()
        bot._uia_responsive = lambda timeout=2.0: False
        calls = []
        ok, err = bot._send_retry(lambda: calls.append(1), '测试')
        self.assertFalse(ok)
        self.assertIn('UIA 无响应', err or '')
        self.assertEqual(calls, [], 'UIA 不响应时仍然调了 send（会卡 396 秒）')
        self.assertTrue(any(k in (err or '') for k in m.SKIP_SEND_REASONS),
                        '探针拦下的原因必须命中 SKIP_SEND_REASONS，否则会被'
                        '当成真实失败而暂停机器人')

    def test_probe_returns_false_when_uia_call_blocks(self):
        """探针本身卡住时要能超时返回 False，不能把调用方一起拖住。"""
        import threading
        import time
        from unittest.mock import patch
        import dafeiyu_bot as m
        gate = threading.Event()

        class _Hanging:
            @property
            def ClassName(self):
                gate.wait(5)
                return 'mmui::MainWindow'

        try:
            with patch.object(m.DaFeiYuBot, '_find_wechat_window',
                              staticmethod(lambda: 1)), \
                    patch('uiautomation.ControlFromHandle',
                          lambda hwnd: _Hanging()):
                t0 = time.time()
                ok = m.DaFeiYuBot._uia_responsive(timeout=0.3)
                elapsed = time.time() - t0
        finally:
            gate.set()
        self.assertFalse(ok)
        self.assertLess(elapsed, 2.0, '探针卡住时没有按超时返回')


class TestDeferredReplies(unittest.TestCase):
    """真实回调遇到临时断屏时，答案须落盘并能在重启后补发。"""

    def setUp(self):
        import os
        import time
        import tempfile
        from unittest.mock import patch
        import dafeiyu_bot as m
        self.m = m
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, filename in (('PENDING_REPLIES_FILE', 'pending.json'),
                               ('KNOWN_CHATS_FILE', 'known.json')):
            p = patch.object(m, name, os.path.join(self.tmp.name, filename),
                             create=True)
            p.start()
            self.addCleanup(p.stop)

    def _bot(self, readable):
        from unittest.mock import Mock
        bot = self.m.DaFeiYuBot()
        bot.route = lambda *_: {'id': 'default', 'name': '大肥鱼',
                                'at_name': '大肥鱼', 'recent_n': 20}
        bot.think = Mock(return_value=('你好，有什么事吗？', None))
        bot._wait_for_display = lambda timeout=20: True
        bot._ensure_window_on_display = lambda force=False: True
        bot._session_active = lambda: True
        bot._desktop_readable = lambda: readable
        bot._uia_responsive = lambda timeout=2.0: True
        return bot

    def _input(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        msg = SimpleNamespace(id='incoming-1', create_time=1794616717,
                              attr='friend', type='text', content='你好',
                              sender='强')
        db = SimpleNamespace(wxid='self-account',
                             get_messages=Mock(return_value=[]))
        chat = SimpleNamespace(who='chenjiaqiang97', nickname='强', _db=db,
                               SendMsg=Mock(return_value=SimpleNamespace(
                                   is_success=True)))
        return msg, chat

    def test_temporary_failure_survives_restart_and_sends_once(self):
        import json
        bot = self._bot(readable=False)
        msg, chat = self._input()
        bot.on_message(msg, chat)
        chat.SendMsg.assert_not_called()
        self.assertFalse(bot.paused)
        self.assertTrue(os.path.isfile(self.m.PENDING_REPLIES_FILE),
                        '临时不可发送时，回复被直接丢弃了')
        # 同一来源的再次回调不能把一条待发答案排成两条。
        bot.on_message(msg, chat)
        with open(self.m.PENDING_REPLIES_FILE, encoding='utf-8') as f:
            self.assertEqual(len(json.load(f)), 1)
        restored = self._bot(readable=True)
        restored._pending_chats[chat.who] = chat
        restored._flush_pending_replies()
        restored._flush_pending_replies()
        chat.SendMsg.assert_called_once_with('你好，有什么事吗？')
        restored.think.assert_not_called()
        with open(self.m.PENDING_REPLIES_FILE, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {})

    def test_real_send_failure_keeps_reply_and_pauses_gui(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        import json
        bot = self._bot(readable=True)
        msg, chat = self._input()
        chat.SendMsg.return_value = SimpleNamespace(is_success=False)
        with patch.object(self.m, 'SEND_RETRY_WAIT', 0):
            bot.on_message(msg, chat)
        self.assertTrue(bot.paused)
        self.assertEqual(chat.SendMsg.call_count, 1 + self.m.SEND_RETRIES)
        self.assertTrue(os.path.isfile(self.m.PENDING_REPLIES_FILE),
                        '真正发送失败也须保留答案，供检查窗口后重启续发')
        with open(self.m.PENDING_REPLIES_FILE, encoding='utf-8') as f:
            self.assertEqual(len(json.load(f)), 1)
        bot._flush_pending_replies()
        self.assertEqual(chat.SendMsg.call_count, 1 + self.m.SEND_RETRIES)

    def test_other_account_cannot_send_saved_reply(self):
        bot = self._bot(readable=False)
        msg, chat = self._input()
        bot.on_message(msg, chat)
        restored = self._bot(readable=True)
        chat._db.wxid = 'another-account'
        restored._pending_chats[chat.who] = chat
        restored._flush_pending_replies()
        chat.SendMsg.assert_not_called()
        self.assertEqual(len(restored._pending_replies), 1)

    def test_transient_failure_stops_after_pending_round_cap(self):
        """临时失败不能无限补发：到 PENDING_MAX_ROUNDS 就删除并停止。

        旧实现只在内层 _send_retry 限 4 次，外层 _retry_pending_replies
        每 10 秒又把同一条交给它，日志里从 09:01 一直重复到 09:14。
        """
        from unittest.mock import patch
        import json

        class _Fail:
            is_success = False

            def __str__(self):
                return ("{'status': '失败', 'message': '微信窗口不可见"
                        "（可能锁屏/会话断开）', 'data': None}")

        bot = self._bot(readable=False)
        msg, chat = self._input()
        # 直接入队，避免 on_message 里的第一次补发先占掉一轮；本用例只测
        # 外层补发轮数上限。
        bot._queue_reply(msg, chat, '你好，有什么事吗？', 'default')
        self.assertEqual(len(bot._pending_replies), 1)
        chat.SendMsg.return_value = _Fail()
        bot._desktop_readable = lambda: True
        cap = 2
        with patch.object(self.m, 'SEND_RETRY_WAIT', 0), \
                patch.object(self.m, 'PENDING_MAX_ROUNDS', cap):
            bot._flush_pending_replies()
            self.assertEqual(len(bot._pending_replies), 1)
            self.assertEqual(chat.SendMsg.call_count,
                             1 + self.m.SEND_RETRIES)
            bot._flush_pending_replies()
        self.assertFalse(bot.paused,
                         '临时失败到上限应放弃该条，不应暂停整个机器人')
        self.assertEqual(bot._pending_replies, {})
        self.assertEqual(chat.SendMsg.call_count,
                         cap * (1 + self.m.SEND_RETRIES))
        with open(self.m.PENDING_REPLIES_FILE, encoding='utf-8') as f:
            self.assertEqual(json.load(f), {})

    def test_retry_thread_stops_after_consecutive_exceptions(self):
        """补发线程自身连续异常也要熔断，不能每 10 秒永远重试。"""
        import threading
        from unittest.mock import Mock, patch

        bot = self._bot(readable=True)
        bot._pending_replies = {'k': {'chat_id': 'x'}}
        bot._display_rect = Mock(side_effect=RuntimeError('boom'))
        with patch.object(self.m, '_display_target', lambda: 'X'), \
                patch.object(self.m, 'WATCH_INTERVAL', 0.01), \
                patch.object(self.m, 'PENDING_MAX_ROUNDS', 2):
            t = threading.Thread(target=bot._retry_pending_replies)
            t.start()
            t.join(3)
        self.assertFalse(t.is_alive(), '连续异常达到上限后补发线程没有停止')
        self.assertIn('k', bot._pending_replies, '熔断时不应丢掉待发答案')

    def test_failed_save_cannot_leave_an_unsaved_sendable_reply(self):
        from unittest.mock import patch
        bot = self._bot(readable=True)
        msg, chat = self._input()
        with patch.object(self.m.os, 'replace', side_effect=OSError('写盘失败')):
            bot.on_message(msg, chat)
        chat.SendMsg.assert_not_called()
        self.assertEqual(bot._pending_replies, {})
        bot._flush_pending_replies()
        chat.SendMsg.assert_not_called()

    def test_failed_ack_is_reconciled_from_database_after_restart(self):
        from unittest.mock import patch
        bot = self._bot(readable=True)
        msg, chat = self._input()
        replace = self.m.os.replace
        calls = []

        def fail_ack(src, dst):
            if dst == self.m.PENDING_REPLIES_FILE:
                calls.append(1)
                if len(calls) == 2:
                    raise OSError('确认落盘失败')
            return replace(src, dst)

        with patch.object(self.m.os, 'replace', side_effect=fail_ack):
            bot.on_message(msg, chat)
        chat.SendMsg.assert_called_once()
        self.assertEqual(len(bot._pending_replies), 1)
        # 模拟微信确已写入的出站记录；重启后须据此确认，而不是再点发送。
        chat._db.get_messages.return_value = [{
            'sender_username': 'self-account', 'sort_seq': 10,
            'content': '你好，有什么事吗？'}]
        restored = self._bot(readable=True)
        restored._pending_chats[chat.who] = chat
        restored._flush_pending_replies()
        chat.SendMsg.assert_called_once()
        self.assertEqual(restored._pending_replies, {})


class TestSearchKeywordCache(unittest.TestCase):
    """搜索词缓存：命中只搜一个词，过期/无缓存才走完整候选。

    真机链路（2026-11-14 用户指出）：`open_chat` 每次发送都要
    `WeChatDB().search_contact()` 查库，拿到 ``[wxid, 昵称, 备注]`` 后
    **先搜一遍搜不到的 wxid**，才轮到昵称——一次白搜约 1.2~2.7 秒。
    """

    def _uia(self):
        from wechatauto.uia_driver import WeChatUIA
        uia = object.__new__(WeChatUIA)
        uia._search_kw_cache = {}
        return uia

    def test_cache_hit_returns_single_word_and_skips_db(self):
        import time
        uia = self._uia()
        uia._search_kw_cache['chenjiaqiang97'] = ('强', time.time())

        def _boom(_kw):
            raise AssertionError('命中缓存时不应该再查 DB/重算候选')

        uia._resolve_search_keyword = _boom
        self.assertEqual(uia._search_keywords('chenjiaqiang97'), ['强'],
                         '命中缓存时没有直接返回上次成功的搜索词')

    def test_expired_cache_falls_back_to_full_candidates(self):
        import time
        uia = self._uia()
        uia._search_kw_cache['chenjiaqiang97'] = (
            '强', time.time() - uia.SEARCH_KW_TTL - 1)
        asked = []
        uia._resolve_search_keyword = lambda kw: (
            asked.append(kw), ['chenjiaqiang97', '强'])[1]
        words = uia._search_keywords('chenjiaqiang97')
        self.assertEqual(asked, ['chenjiaqiang97'], '过期缓存应当重算候选')
        self.assertIn('chenjiaqiang97', words)

    def test_no_cache_uses_full_candidates(self):
        uia = self._uia()
        uia._resolve_search_keyword = lambda kw: ['wxid_x', '张三']
        self.assertEqual(uia._search_keywords('wxid_x'), ['wxid_x', '张三'])


class TestSearchKeywordAlias(unittest.TestCase):
    """alias（微信号）必须是首个搜索词。

    真机实测（2026-11-14）：搜 alias「God-Cap」→ 结果第 1 条就在「联系人」
    分区，正是「强」（chenjiaqiang97），点开后 current_chat == '强'。
    昵称重名严重（本账号 `.` 45 人、`L` 32 人、`ㅤ` 27 人），所以昵称只能
    排在 alias 和备注之后；username 本身搜索框不认，放最后兜底。
    """

    def test_alias_first_and_username_last(self):
        from unittest.mock import patch
        from wechatauto.uia_driver import WeChatUIA
        fake = [{'username': 'chenjiaqiang97', 'nick_name': '强',
                 'remark': '', 'alias': 'God-Cap'}]
        with patch('wechatauto.db.WeChatDB') as MockDB:
            MockDB.return_value.search_contact.return_value = fake
            words = WeChatUIA._resolve_search_keyword('chenjiaqiang97')
        self.assertEqual(words[0], 'God-Cap',
                         'alias 没有排在首个候选，重名时仍会走昵称')
        self.assertIn('强', words)
        self.assertEqual(words[-1], 'chenjiaqiang97')


class TestDisplaySwitch(unittest.TestCase):
    """虚拟屏开关：关掉后所有判断目标显示器的地方都必须认为「不干预」。

    2026-11-14 之前「不用虚拟屏」只能靠把 WECHAT_DISPLAY 留空；现在多了
    明确的布尔开关。启动校正、发送前等待、看门狗、待发轮询四处都走
    `_display_target()`，这组用例锁住它的判定顺序——漏掉任何一处，
    现象都是「开关关了、窗口还在被搬」。
    """

    def _target(self, enabled, name):
        import dafeiyu_bot as m
        from unittest.mock import patch
        with patch.object(m, 'WECHAT_DISPLAY_ENABLED', enabled), \
                patch.object(m, 'WECHAT_DISPLAY', name):
            return m._display_target()

    def test_disabled_wins(self):
        """开关关掉时，显示器名填什么都不干预。"""
        self.assertEqual(self._target(False, 'IddSampleDriver'), '')

    def test_enabled_uses_name(self):
        self.assertEqual(self._target(True, 'IddSampleDriver'),
                         'IddSampleDriver')

    def test_legacy_off_values(self):
        """老配置里留空 / 填 - 仍然算关闭，不逼用户去改文件。"""
        for name in ('', '   ', '-', 'none', 'OFF'):
            self.assertEqual(self._target(True, name), '',
                             f'{name!r} 应视为关闭')

    def test_strip(self):
        self.assertEqual(self._target(True, '  IddSampleDriver Device '),
                         'IddSampleDriver Device')


class TestDisplayConfigParse(unittest.TestCase):
    """配置读盘：开关按字面解析（`bool('false')` 是 True 这个坑）。"""

    def test_bool_literals(self):
        import dafeiyu_bot as m
        self.assertIs(m._coerce(bool, True), True)
        self.assertIs(m._coerce(bool, False), False)

    def test_bool_strings(self):
        import dafeiyu_bot as m
        for s in ('false', '0', 'no', 'off', 'FALSE', '  False '):
            self.assertIs(m._coerce(bool, s), False, f'{s!r} 应为 False')
        for s in ('true', '1', 'yes', 'on', '是'):
            self.assertIs(m._coerce(bool, s), True, f'{s!r} 应为 True')

    def test_json_false_really_turns_off(self):
        """bot_config.json 写 false 后必须真的关掉，不能当空值跳过。"""
        import json
        import tempfile
        import dafeiyu_bot as m
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'bot_config.json')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump({'WECHAT_DISPLAY_ENABLED': False}, f)
            old_mtime = m._config_mtime
            try:
                with patch.object(m, 'BOT_CONFIG_FILE', p):
                    m._apply_config_overrides()
                    self.assertIs(m.WECHAT_DISPLAY_ENABLED, False)
                    self.assertEqual(m._display_target(), '')
            finally:
                m.WECHAT_DISPLAY_ENABLED = True   # 别把测试状态漏给后面的用例
                m._config_mtime = old_mtime


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestBroadcastSchedule(unittest.TestCase):
    """群发任务的周期换算与下次执行时间。

    「每 N 天 / 每 N 小时」都换算成秒后累加；关机几天回来只顺延到最近
    一轮，不连环补发（否则一开机就把攒了十几轮的消息全轰出去）。
    """

    def test_interval_seconds(self):
        from broadcast import interval_seconds
        self.assertEqual(interval_seconds(
            {'schedule': {'type': 'loop', 'unit': 'hour', 'interval': 7}}), 25200)
        self.assertEqual(interval_seconds(
            {'schedule': {'type': 'loop', 'unit': 'day', 'interval': 2}}), 172800)
        self.assertEqual(interval_seconds({'schedule': {'type': 'once'}}), 0)

    def test_after_run_advances_one_period(self):
        from broadcast import compute_next_run
        now = 1_700_000_000.0
        task = {'schedule': {'type': 'loop', 'unit': 'day', 'interval': 2},
                'next_run': now}
        self.assertEqual(compute_next_run(task, now, after_run=True),
                         now + 2 * 86400)

    def test_overdue_jumps_to_next_future_slot(self):
        from broadcast import compute_next_run
        now = 1_700_000_000.0
        task = {'schedule': {'type': 'loop', 'unit': 'day', 'interval': 2},
                'next_run': now}
        later = now + 10 * 86400
        nxt = compute_next_run(task, later)
        self.assertGreater(nxt, later)
        self.assertLessEqual(nxt - later, 2 * 86400)

    def test_once_returns_none_after_run(self):
        from broadcast import compute_next_run
        task = {'schedule': {'type': 'once'}, 'next_run': 1.0}
        self.assertIsNone(compute_next_run(task, 2.0, after_run=True))

    def test_bad_interval_falls_back_to_one(self):
        from broadcast import normalize_task
        t = normalize_task({'targets': ['a@chatroom'], 'text': 'x',
                            'schedule': {'type': 'loop', 'unit': 'day',
                                         'interval': 'abc'}})
        self.assertEqual(t['schedule']['interval'], 1)


class TestBroadcastValidate(unittest.TestCase):
    """保存前校验：空目标 / 没内容 / 图片不存在都要带原因拒绝。"""

    def _task(self, **kw):
        from broadcast import normalize_task
        base = {'name': 't',
                'targets': [{'id': 'filehelper', 'name': '文件传输助手'}],
                'text': 'hi', 'schedule': {'type': 'once', 'run_at': 1.0},
                'next_run': 1.0}
        base.update(kw)
        return normalize_task(base)

    def test_empty_targets(self):
        from broadcast import validate_task
        with self.assertRaises(ValueError) as cm:
            validate_task(self._task(targets=[]))
        self.assertIn('目标', str(cm.exception))

    def test_empty_content(self):
        from broadcast import validate_task
        with self.assertRaises(ValueError) as cm:
            validate_task(self._task(text='', image=''))
        self.assertIn('文本', str(cm.exception))

    def test_missing_image(self):
        from broadcast import validate_task
        with self.assertRaises(ValueError) as cm:
            validate_task(self._task(image='stickers/__nope__.png'))
        self.assertIn('图片不存在', str(cm.exception))

    def test_ok_task_passes(self):
        from broadcast import validate_task
        validate_task(self._task())


class TestBroadcastNormalize(unittest.TestCase):
    """脏数据收敛：单条坏数据不能让整个任务列表读不出来。"""

    def test_string_target(self):
        from broadcast import normalize_task
        t = normalize_task({'targets': ['123@chatroom'], 'text': 'x'})
        self.assertEqual(t['targets'][0]['id'], '123@chatroom')
        self.assertEqual(t['targets'][0]['kind'], 'group')

    def test_missing_id_gets_one(self):
        from broadcast import normalize_task
        t = normalize_task({'targets': ['a'], 'text': 'x'})
        self.assertTrue(t['id'].startswith('bt_'))

    def test_unknown_status_text(self):
        from broadcast import STATUS_TEXT
        self.assertEqual(STATUS_TEXT.get('', '未执行'), '未执行')

class TestBroadcastTransientRetry(unittest.TestCase):
    """临时不可用（锁屏 / 断屏）不消耗排期，过几分钟重试。

    2026-11-16 09:34 实测：锁屏时「每 2 天」的群发任务被当成失败并顺延两天，
    等于白丢一次。临时状态应该短重试，连续一小时都不行才认失败。
    """

    def setUp(self):
        import broadcast, tempfile
        self.dir = tempfile.mkdtemp()
        # 三个路径都要换：_execute 会写日志，_persist 会拿锁文件
        self._orig = (broadcast.TASKS_FILE, broadcast.BROADCAST_LOG,
                      broadcast.LOCK_FILE)
        broadcast.TASKS_FILE = os.path.join(self.dir, 'tasks.json')
        broadcast.BROADCAST_LOG = os.path.join(self.dir, 'broadcast.log')
        broadcast.LOCK_FILE = broadcast.TASKS_FILE + '.lock'
        # 让路判定读真实键入活动——跑测试的人正在打字会把目标全判成
        # 「让路未发送」，劫走本类的失败分类用例。测试视为用户不活跃。
        self._ua = patch.object(broadcast, '_user_active',
                                lambda: False)
        self._ua.start()
        self.addCleanup(self._ua.stop)

        def _restore():
            (broadcast.TASKS_FILE, broadcast.BROADCAST_LOG,
             broadcast.LOCK_FILE) = self._orig
        self.addCleanup(_restore)

    def _mk(self, **kw):
        import broadcast, time
        t = broadcast.normalize_task({
            'name': 't', 'targets': [{'id': 'a', 'name': 'A'}], 'text': 'x',
            'schedule': {'type': 'loop', 'unit': 'day', 'interval': 2},
            'next_run': time.time() - 1, **kw})
        broadcast.save_tasks([t])
        return t

    def _sched(self, ret):
        import broadcast
        return broadcast.BroadcastScheduler(
            send_fn=lambda target, task: ret, start_delay=0, send_gap=0)

    def test_transient_keeps_schedule(self):
        import broadcast, time
        t = self._mk()
        self._sched((False, '微信窗口不可见（可能锁屏/会话断开）', True))._execute(t)
        after = broadcast.load_tasks()[0]
        self.assertEqual(after['last_status'], 'retry')
        self.assertEqual(after['run_count'], 0)          # 不算执行过一次
        self.assertEqual(after['retry_count'], 1)
        self.assertEqual(after['schedule']['interval'], 2)
        self.assertAlmostEqual(after['next_run'],
                               time.time() + broadcast.TRANSIENT_RETRY_SECONDS,
                               delta=10)

    def test_too_many_retries_falls_back_to_period(self):
        import broadcast, time
        t = self._mk(retry_count=broadcast.MAX_TRANSIENT_RETRIES)
        before = t['next_run']
        self._sched((False, '微信窗口不可见', True))._execute(t)
        after = broadcast.load_tasks()[0]
        self.assertEqual(after['last_status'], 'failed')
        self.assertEqual(after['run_count'], 1)
        self.assertEqual(after['retry_count'], 0)        # 重试计数清零
        self.assertAlmostEqual(after['next_run'], before + 2 * 86400, delta=10)

    def test_permanent_failure_not_retried(self):
        import broadcast, time
        t = self._mk()
        self._sched((False, '目标已不存在（群解散、被移出或好友已删除）', False))._execute(t)
        after = broadcast.load_tasks()[0]
        self.assertEqual(after['last_status'], 'failed')
        self.assertEqual(after['run_count'], 1)


class TestRespTextDict(unittest.TestCase):
    """WxResponse 是 dict 子类，取原因必须先按 dict 取。

    2026-11-16 09:34 的日志把整个 ``{'status','message','data'}`` 打进了
    last_detail，就是因为当时只按属性取、而 dict 的键不是属性。
    """

    def test_wxresponse_message(self):
        from wechatauto.param import WxResponse
        from dafeiyu_bot import _resp_text
        r = WxResponse.failure('微信窗口不可见（可能锁屏/会话断开）')
        self.assertEqual(_resp_text(r), '微信窗口不可见（可能锁屏/会话断开）')

    def test_plain_dict(self):
        from dafeiyu_bot import _resp_text
        self.assertEqual(_resp_text({'status': '失败', 'message': '出错了', 'data': None}),
                         '出错了')

    def test_none_and_object(self):
        from dafeiyu_bot import _resp_text
        self.assertEqual(_resp_text(None), '')

        class R:
            message = '对象消息'
        self.assertEqual(_resp_text(R()), '对象消息')

class TestOpenDeadline(unittest.TestCase):
    """群发限时：open_deadline 到了，open_chat/find_session 的每轮 OCR 提前收手。

    单次截屏/OCR 调用本身中断不了（跨线程会死锁，已实测），能做的只有
    在多轮重试之间检查截止时间——把「打不开的目标拖 1~2 分钟」压到上限。
    """

    def _fake(self, deadline):
        class Fake:
            open_deadline = deadline
        return Fake()

    def test_no_deadline_never_expires(self):
        from wechatauto.guia import WeChatGUI
        self.assertFalse(WeChatGUI._past_deadline(self._fake(0)))

    def test_future_deadline(self):
        import time
        from wechatauto.guia import WeChatGUI
        self.assertFalse(WeChatGUI._past_deadline(self._fake(time.time() + 60)))

    def test_past_deadline(self):
        import time
        from wechatauto.guia import WeChatGUI
        self.assertTrue(WeChatGUI._past_deadline(self._fake(time.time() - 1)))


class TestSendWatchdog(unittest.TestCase):
    """发送卡死看门狗：超过上限就整进程退出，等外部重新拉起。

    为什么是整进程退出而不是放弃这一条：卡住的 COM/UIA 调用不会返回，
    进程内中断不了（跨线程 join 会死锁，已实测），它会一直占着 send_lock。
    """

    def test_idle_never_counts_as_hung(self):
        from dafeiyu_bot import _send_hang_exceeded
        self.assertFalse(_send_hang_exceeded(0, 1e9, 120))

    def test_normal_text_send_within_limit(self):
        # 实测正常文本 13.4s、图文 18.5s，都远低于 120s
        from dafeiyu_bot import _send_hang_exceeded
        self.assertFalse(_send_hang_exceeded(1000.0, 1013.4, 120))
        self.assertFalse(_send_hang_exceeded(1000.0, 1018.5, 120))

    def test_known_396s_hang_trips(self):
        # 2026-11-14 实测过一次 SendMsg 卡 396 秒
        from dafeiyu_bot import _send_hang_exceeded
        self.assertTrue(_send_hang_exceeded(1000.0, 1396.0, 120))

    def test_boundary_is_inclusive(self):
        from dafeiyu_bot import _send_hang_exceeded
        self.assertFalse(_send_hang_exceeded(1000.0, 1119.9, 120))
        self.assertTrue(_send_hang_exceeded(1000.0, 1120.0, 120))

    def test_exit_code_is_dedicated(self):
        # 一键启动.bat 靠这个码判断「该自动重启」，不能和普通异常的 1 撞
        from dafeiyu_bot import WATCHDOG_EXIT_CODE
        self.assertEqual(WATCHDOG_EXIT_CODE, 75)

    def test_default_limit_is_safe_multiple(self):
        # 正常最长 18.5s（图文）；25s 的「打开会话」上限在 send_lock 之外
        from dafeiyu_bot import SEND_HANG_SECONDS
        self.assertGreaterEqual(SEND_HANG_SECONDS, 60)

    def test_test_hang_injection_is_off_by_default(self):
        from dafeiyu_bot import TEST_HANG_SEND
        self.assertEqual(TEST_HANG_SEND, 0.0)


class TestPidFileStaleness(unittest.TestCase):
    """残留 .bot.pid 不能挡住启动：PID 会被系统复用给别的程序。"""

    def test_recognises_bot_cmdline(self):
        from dafeiyu_bot import _looks_like_bot
        self.assertTrue(_looks_like_bot(['python', 'dafeiyu_bot.py']))
        self.assertTrue(_looks_like_bot([r'C:\\Python313\\python.exe', 'dafeiyu_bot.py']))

    def test_other_process_is_not_a_bot(self):
        # PID 被复用成记事本/浏览器之类时，必须判定「不是我们的实例」
        from dafeiyu_bot import _looks_like_bot
        self.assertFalse(_looks_like_bot(['notepad.exe', 'a.txt']))
        self.assertFalse(_looks_like_bot(['python', 'other_script.py']))
        self.assertFalse(_looks_like_bot([]))
        self.assertFalse(_looks_like_bot(None))


class TestBroadcastFailureClass(unittest.TestCase):
    """失败分类：决定「当场重试」还是「留给下一轮」。

    这条线原来只靠 dafeiyu_bot._is_transient 的 5 个短语，COMError 不在其中，
    实测导致一次 COMError 被记成永久失败、next_run 直接顺延两天。
    """

    def test_comerror_is_transient(self):
        import broadcast as b
        err = ("发送异常：COMError: (-2147220991, '事件无法调用任何订户', "
               "(None, None, None, 0, None))")
        self.assertEqual(b.classify_failure(err), b.FAIL_TRANSIENT)

    def test_missing_target_is_permanent(self):
        import broadcast as b
        self.assertEqual(
            b.classify_failure('目标已不存在（群解散、被移出或好友已删除）'),
            b.FAIL_PERMANENT)

    def test_missing_image_is_permanent(self):
        import broadcast as b
        self.assertEqual(b.classify_failure('图片不存在：stickers/x.png'),
                         b.FAIL_PERMANENT)

    def test_open_timeout_is_transient(self):
        import broadcast as b
        self.assertEqual(b.classify_failure('打开会话超过 25 秒上限'),
                         b.FAIL_TRANSIENT)

    def test_locked_screen_is_transient(self):
        import broadcast as b
        self.assertEqual(b.classify_failure('Windows 会话不可交互（远程断开/锁屏）'),
                         b.FAIL_TRANSIENT)

    def test_unknown_falls_back_to_unknown(self):
        import broadcast as b
        self.assertEqual(b.classify_failure('某种没见过的错误'), b.FAIL_UNKNOWN)

    def test_permanent_is_not_retryable(self):
        import broadcast as b
        self.assertFalse(b.is_retryable(b.FAIL_PERMANENT))
        self.assertTrue(b.is_retryable(b.FAIL_TRANSIENT))
        self.assertTrue(b.is_retryable(b.FAIL_UNKNOWN))


class TestBroadcastProgress(unittest.TestCase):
    """断点续跑字段：normalize_task 是白名单，漏了 progress 就等于每次从头重发。"""

    def test_progress_survives_normalize(self):
        import broadcast as b
        task = b.normalize_task({
            'id': 't1', 'targets': [{'id': 'a'}], 'text': 'hi',
            'progress': {'run_id': 'r1', 'started_at': 111.0, 'done': ['a']},
        })
        self.assertEqual(task['progress']['run_id'], 'r1')
        self.assertEqual(task['progress']['done'], ['a'])

    def test_half_progress_is_dropped(self):
        import broadcast as b
        self.assertEqual(b.normalize_task({'progress': {'done': ['a']}})['progress'], {})
        self.assertEqual(b.normalize_task({'progress': {'run_id': 'r'}})['progress'], {})
        self.assertEqual(b.normalize_task({'progress': 'bad'})['progress'], {})


class TestBroadcastTargetRetry(unittest.TestCase):
    """单目标重试：临时失败当场有限次重试，永久失败一次就停。"""

    def setUp(self):
        # _try_target 里的重试会写 broadcast.log；不隔离的话每跑一次测试
        # 都会往真实的群发日志里塞几行假目标。
        import os
        import tempfile
        import broadcast as b
        self.b = b
        self.dir = tempfile.mkdtemp(prefix='dfy_bclog_')
        self._saved = (b.BROADCAST_LOG, b.TARGET_RETRY_WAIT)
        b.BROADCAST_LOG = os.path.join(self.dir, 'broadcast.log')
        b.TARGET_RETRY_WAIT = 0

    def tearDown(self):
        import shutil
        b = self.b
        (b.BROADCAST_LOG, b.TARGET_RETRY_WAIT) = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def _scheduler(responses):
        """responses: [(ok, err, retryable), ...]，按调用顺序取，用完停在最后一个。"""
        calls = []

        def send(target, task):
            calls.append(target.get('id'))
            return responses[min(len(calls) - 1, len(responses) - 1)]

        return lib().BroadcastScheduler(send_fn=send), calls

    def test_transient_retries_then_succeeds(self):
        b = self.b
        sched, calls = self._scheduler([
            (False, 'COMError: x', True),
            (False, 'COMError: x', True),
            (True, '', False),
        ])
        ok, err, retryable, attempts = sched._try_target({'id': 'a'}, {})
        self.assertTrue(ok)
        self.assertEqual(attempts, 3)
        self.assertEqual(len(calls), 3)

    def test_permanent_stops_after_one_attempt(self):
        sched, calls = self._scheduler([(False, '目标已不存在', False)])
        ok, err, retryable, attempts = sched._try_target({'id': 'a'}, {})
        self.assertFalse(ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(calls), 1)
        self.assertFalse(retryable)

    def test_transient_exhausts_all_attempts(self):
        b = self.b
        sched, calls = self._scheduler([(False, 'UIA 无响应', True)])
        ok, err, retryable, attempts = sched._try_target({'id': 'a'}, {})
        self.assertFalse(ok)
        self.assertEqual(attempts, b.TARGET_ATTEMPTS)
        self.assertEqual(len(calls), b.TARGET_ATTEMPTS)
        self.assertTrue(retryable, '临时失败要留给下一轮')


def lib():
    """取 broadcast 模块（放在文件末尾，避免和上面的既有 import 冲突）。"""
    import broadcast
    return broadcast


class TestBroadcastPartialAndResume(unittest.TestCase):
    """部分临时失败要短间隔补发；中断续跑不能重发已成功的。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix='dfy_bc_')
        import broadcast as b
        self.b = b
        self._saved = (b.TASKS_FILE, b.BROADCAST_LOG, b.LOCK_FILE,
                       b.TARGET_RETRY_WAIT)
        b.TASKS_FILE = os.path.join(self.dir, 'tasks.json')
        b.BROADCAST_LOG = os.path.join(self.dir, 'broadcast.log')
        b.LOCK_FILE = b.TASKS_FILE + '.lock'
        b.TARGET_RETRY_WAIT = 0
        # 让路判定读真实键入活动——跑测试的人正在打字就会把所有目标判成
        # 「让路未发送」，预算/断点用例全被劫走。测试一律视为用户不活跃。
        self._ua = patch.object(b, '_user_active', lambda: False)
        self._ua.start()
        self.addCleanup(self._ua.stop)

    def tearDown(self):
        b = self.b
        (b.TASKS_FILE, b.BROADCAST_LOG, b.LOCK_FILE,
         b.TARGET_RETRY_WAIT) = self._saved
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _make(self, targets, progress=None):
        b = self.b
        raw_task = {
            'id': 'bt_t1', 'name': '测试任务', 'enabled': True,
            'targets': [{'id': i, 'name': n} for i, n in targets],
            'text': 'x',
        }
        if progress:
            raw_task['progress'] = progress
        task = b.normalize_task(raw_task)
        b.save_tasks([task])
        return b.load_tasks()[0]

    def test_partial_transient_schedules_short_retry(self):
        import time
        from unittest import mock
        task = self._make([('a', '甲'), ('b', '乙')])
        calls = []

        def send(target, _t):
            calls.append(target['id'])
            if target['id'] == 'a':
                return True, '', False
            return False, 'COMError: x', True

        sched = self.b.BroadcastScheduler(send_fn=send)
        sched._gap = 0.0
        with mock.patch.object(self.b, 'TARGET_ATTEMPTS', 1):
            sched._execute(task)
        after = self.b.load_tasks()[0]
        self.assertEqual(after['last_status'], 'retry')
        self.assertEqual(after['retry_count'], 1)
        self.assertLessEqual(after['next_run'],
                             time.time() + self.b.TRANSIENT_RETRY_SECONDS + 5)
        # 已成功的目标要留在 progress 里，补发时跳过
        self.assertEqual(after['progress']['done'], ['a'])

    def test_permanent_failure_does_not_schedule_retry(self):
        task = self._make([('a', '甲'), ('b', '乙')])

        def send(target, _t):
            if target['id'] == 'a':
                return True, '', False
            return False, '目标已不存在', False

        sched = self.b.BroadcastScheduler(send_fn=send)
        sched._gap = 0.0
        sched._execute(task)
        after = self.b.load_tasks()[0]
        self.assertEqual(after['last_status'], 'partial')
        self.assertEqual(after['retry_count'], 0)
        self.assertEqual(after['progress'], {},
                         '这一轮跑完了，续跑标记要清掉')

    def test_resume_skips_already_done_target(self):
        task = self._make([('a', '甲'), ('b', '乙')],
                          progress={'run_id': 'r1', 'started_at': 1.0,
                                    'done': ['a']})
        calls = []

        def send(target, _t):
            calls.append(target['id'])
            return True, '', False

        sched = self.b.BroadcastScheduler(send_fn=send)
        sched._gap = 0.0
        sched._execute(task)
        self.assertEqual(calls, ['b'], '已经成功过的甲不能再发一次')
        after = self.b.load_tasks()[0]
        self.assertEqual(after['last_status'], 'success')
        self.assertEqual(len(after['last_results']), 2)

    def test_roundtrip_stop_restart_resume(self):
        """停用→重启→续跑往返：中断收尾保留 done，重启续跑不重发已成功的。

        注：正常跑完一轮（无临时失败）收尾会清 progress——loop 任务每个
        周期本来就要重发全部目标，断点只在「中断续跑」语义下生效。
        """
        from unittest import mock
        self._make([('a', '群A'), ('b', '群B')],
                   progress={'run_id': 'r1', 'started_at': 1.0,
                             'done': ['a']})
        calls = []

        def send(target, _t):
            calls.append(target['id'])
            return False, 'COMError: x', True    # b 临时失败，本轮中断

        sched = self.b.BroadcastScheduler(send_fn=send)   # 重启后的新进程
        sched._gap = 0.0
        with mock.patch.object(self.b, 'TARGET_ATTEMPTS', 1):
            sched._execute(self.b.load_tasks()[0])       # 续跑
        self.assertEqual(calls, ['b'])           # a 已发过，绝不重发
        after = self.b.load_tasks()[0]
        self.assertEqual(after['progress']['done'], ['a'])   # 断点保留
        self.assertEqual(after['last_status'], 'retry')      # 留给续跑

    def test_budget_stops_remaining_targets(self):
        task = self._make([('a', '甲'), ('b', '乙')])

        def send(target, _t):
            return True, '', False

        sched = self.b.BroadcastScheduler(send_fn=send)
        sched._gap = 0.0
        with mock_budget(self.b):
            sched._execute(task)
        after = self.b.load_tasks()[0]
        self.assertEqual(after['last_status'], 'retry',
                         '跳过算可重试，本轮应安排补发')
        skipped = [r for r in after['last_results'] if '跳过' in r['error']]
        self.assertTrue(skipped, '预算耗尽的目标要记「跳过」')


class mock_budget:
    """把 TASK_BUDGET_SECONDS 临时设成负数：让第一个目标之后全部超预算。"""

    def __init__(self, mod):
        self.mod = mod

    def __enter__(self):
        self.old = self.mod.TASK_BUDGET_SECONDS
        self.mod.TASK_BUDGET_SECONDS = -1.0

    def __exit__(self, *a):
        self.mod.TASK_BUDGET_SECONDS = self.old


class TestSingleInstanceAndShutdown(unittest.TestCase):
    """Ctrl+C 之后必须真正退出；残留的死 PID 不能挡住下一次启动。

    2026-11-16 用户反复撞到的现象是「Ctrl+C 退出后再双击，被已有实例挡住」。
    两个方向都要覆盖：进程退出时主动放锁，以及锁指向死进程时判它无效。
    """

    def setUp(self):
        import os, tempfile
        import dafeiyu_bot as b
        self.b = b
        self.dir = tempfile.mkdtemp(prefix='dfy_lock_')
        self.pid_file = os.path.join(self.dir, '.bot.pid')
        self._orig = b.PID_FILE
        b.PID_FILE = self.pid_file

    def tearDown(self):
        import shutil
        self.b.PID_FILE = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_dead_pid_is_not_alive(self):
        """一个不存在的 PID 不能被当成「还在跑的机器人」。"""
        self.assertFalse(self.b._pid_alive(999999))

    def test_other_process_is_not_a_bot(self):
        """PID 被复用给别的程序时不能误判（命令行对不上）。"""
        import os
        self.assertFalse(self.b._pid_alive(os.getpid()))

    def test_stale_pid_file_does_not_block_start(self):
        """锁文件里是死 PID 时要能直接启动，并改写成自己的 PID。"""
        import os
        Path = __import__('pathlib').Path
        Path(self.pid_file).write_text('999999', encoding='utf-8')
        self.assertIsNone(self.b._acquire_single_instance())
        self.assertEqual(Path(self.pid_file).read_text(encoding='utf-8').strip(),
                         str(os.getpid()))

    def test_live_bot_blocks_start(self):
        """锁里是**活着的机器人**时才拒绝启动。"""
        import os
        from unittest import mock
        Path = __import__('pathlib').Path
        # 锁里必须是**别人**的 PID：写自己的会被 other == os.getpid() 跳过
        other = os.getpid() + 1
        Path(self.pid_file).write_text(str(other), encoding='utf-8')
        with mock.patch.object(self.b, '_pid_alive', return_value=True):
            self.assertEqual(self.b._acquire_single_instance(), other)

    def test_shutdown_releases_lock_and_arms_force_exit(self):
        """_shutdown 必须先放锁，再挂一个 daemon 的强制退出兜底。"""
        import os
        from unittest import mock
        Path = __import__('pathlib').Path
        Path(self.pid_file).write_text(str(os.getpid()), encoding='utf-8')
        with mock.patch.object(self.b.threading, 'Timer') as timer:
            self.b._shutdown()
        self.assertFalse(Path(self.pid_file).exists(), '锁必须在这里删掉：os._exit 跳过 atexit')
        timer.assert_called_once()
        self.assertTrue(timer.return_value.daemon, '兜底 timer 不能拖住进程退出')
        timer.return_value.start.assert_called_once()

    def test_shutdown_only_removes_own_lock(self):
        """别删掉后来者写的锁（旧实例退出时新实例已经启动）。"""
        import os
        Path = __import__('pathlib').Path
        Path(self.pid_file).write_text(str(os.getpid() + 1), encoding='utf-8')
        self.b._release_single_instance()
        self.assertTrue(Path(self.pid_file).exists())


class TestStopRunningCmdline(unittest.TestCase):
    """一键启动前该杀谁、绝对不能杀谁。

    2026-11-16 用户要求「启动前有服务在跑就先结束再启动」，于是把停旧实例
    前移进 `一键启动.bat`。这里锁住判据：**只杀常驻机器人**，包装进程
    和一次性命令都不许碰——杀错 bash 等于把用户的终端一起关了。
    """

    def setUp(self):
        # 判据只有一份：管理后台的 is_bot_cmdline（tools/stop_running.py 和
        # dafeiyu_bot._looks_like_bot 都转发到它）
        import admin.server as srv
        self.mod = srv

    def test_plain_bot_is_a_target(self):
        self.assertTrue(self.mod.is_bot_cmdline(
            ['C:/Python313/python.exe', 'dafeiyu_bot.py']))
        self.assertTrue(self.mod.is_bot_cmdline(
            ['python', 'D:/develop/wechat-deepseek/dafeiyu_bot.py']))

    def test_one_shot_modes_are_not_targets(self):
        """--test / --search 不碰微信窗口，不该被杀。"""
        self.assertFalse(self.mod.is_bot_cmdline(
            ['python', 'dafeiyu_bot.py', '--test', 'hi']))
        self.assertFalse(self.mod.is_bot_cmdline(
            ['python', 'dafeiyu_bot.py', '--search', 'kw']))

    def test_shell_wrappers_are_not_targets(self):
        """bash / cmd 的包装进程命令行里也带着整条命令，不能当成机器人。"""
        self.assertFalse(self.mod.is_bot_cmdline(
            ['bash', '-c', 'python dafeiyu_bot.py']))
        self.assertFalse(self.mod.is_bot_cmdline(
            ['cmd.exe', '/c', 'python dafeiyu_bot.py > log 2>&1']))

    def test_other_scripts_are_not_targets(self):
        self.assertFalse(self.mod.is_bot_cmdline(
            ['python', 'tools/stop_running.py']))
        self.assertFalse(self.mod.is_bot_cmdline(
            ['python', '-m', 'admin']))
        self.assertFalse(self.mod.is_bot_cmdline(None))
        self.assertFalse(self.mod.is_bot_cmdline([]))


class TestLogSources(unittest.TestCase):
    """后台日志页的两个来源：机器人日志与群发日志各读各的文件。

    群发日志是 `broadcast.py` 的 `log_line` 单独落盘的，读取走同一套
    「按字节 offset 增量 + 滚动 reset」机制。这里锁住两点：换源真的换了文件、
    文件不存在时返回空尾部而不是抛错（前端靠这个进空态）。
    """

    def setUp(self):
        import tempfile
        import admin.server as srv
        import broadcast
        self.dir = tempfile.mkdtemp(prefix='dfy_logsrc_')
        self.bot_log = os.path.join(self.dir, 'bot.log')
        self.bc_log = os.path.join(self.dir, 'broadcast.log')
        self._srv, self._bc = srv, broadcast
        self._orig = (srv.BOT_LOG, broadcast.BROADCAST_LOG)
        srv.BOT_LOG = self.bot_log
        broadcast.BROADCAST_LOG = self.bc_log
        with open(self.bot_log, 'w', encoding='utf-8') as f:
            f.write('[11-16 10:00:00] [消息] 机器人日志里的一行\n')
        with open(self.bc_log, 'w', encoding='utf-8') as f:
            f.write('[2026-11-16 11:33:12] ===== 任务「t」开始 =====\n')

    def tearDown(self):
        import shutil
        self._srv.BOT_LOG, self._bc.BROADCAST_LOG = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_broadcast_source_reads_broadcast_file(self):
        r = self._srv.tail_log(-1, 'broadcast')
        self.assertEqual(r['src'], 'broadcast')
        self.assertIn('任务「t」开始', r['text'])
        self.assertNotIn('机器人日志里的一行', r['text'])

    def test_bot_source_still_reads_bot_file(self):
        r = self._srv.tail_log(-1, 'bot')
        self.assertIn('机器人日志里的一行', r['text'])
        self.assertNotIn('任务「t」开始', r['text'])

    def test_missing_file_is_an_empty_tail(self):
        """文件不存在时给空尾部：前端据此显示空状态，不是报错。"""
        os.remove(self.bc_log)
        r = self._srv.tail_log(-1, 'broadcast')
        self.assertTrue(r['reset'])
        self.assertEqual(r['text'], '')
        self.assertEqual(r['offset'], 0)


class TestBroadcastWeekdays(unittest.TestCase):
    """执行时间加星期维度：只在勾选的星期触发，旧任务（无此字段）行为不变。

    界面上 1/2/3/4/5/6/0 表示周一到周日（0 为周日），和 weekday_index 一致。
    """

    def setUp(self):
        import broadcast
        self.b = broadcast

    def test_weekday_index_matches_ui_order(self):
        import time
        # 2026-11-16 是周一，2026-11-15 是周日
        mon = time.mktime((2026, 11, 16, 9, 0, 0, 0, 0, -1))
        sun = time.mktime((2026, 11, 15, 9, 0, 0, 0, 0, -1))
        self.assertEqual(self.b.weekday_index(mon), 1)
        self.assertEqual(self.b.weekday_index(sun), 0)

    def test_only_selected_weekdays_fire(self):
        """勾了周一/周三/周五/周日，连续四次都得落在这四天。"""
        import time
        now = time.mktime((2026, 11, 16, 8, 0, 0, 0, 0, -1))   # 周一
        sched = {'type': 'loop', 'unit': 'day', 'interval': 1,
                 'anchor': now, 'weekdays': [0, 1, 3, 5]}
        task = {'schedule': sched, 'next_run': now}
        want = {1, 3, 5, 0}
        cur = self.b.compute_next_run(task, now)
        seen = []
        for _ in range(4):
            self.assertIsNotNone(cur)
            wd = self.b.weekday_index(cur)
            self.assertIn(wd, want, f'{time.strftime("%m-%d", time.localtime(cur))} 不在勾选的星期里')
            seen.append(wd)
            cur = self.b.compute_next_run({'schedule': sched, 'next_run': cur}, cur, after_run=True)
        self.assertEqual(set(seen), want)

    def test_empty_weekdays_keeps_old_behaviour(self):
        """旧任务没有 weekdays 字段：仍按 interval 每天触发。"""
        import time
        now = time.mktime((2026, 11, 16, 8, 0, 0, 0, 0, -1))
        sched = {'type': 'loop', 'unit': 'day', 'interval': 1, 'anchor': now}
        nxt = self.b.compute_next_run({'schedule': sched, 'next_run': now}, now)
        self.assertIsNotNone(nxt)
        # 每天触发：下一次就是 +1 天
        self.assertAlmostEqual(nxt - now, self.b.DAY_SECONDS, delta=1)

    def test_interval_two_with_single_weekday_lands_on_it(self):
        """每隔 2 天 + 只选周一：要落到隔周的周一，而不是撞不上就返回 None。"""
        import time
        mon = time.mktime((2026, 11, 16, 8, 0, 0, 0, 0, -1))
        sched = {'type': 'loop', 'unit': 'day', 'interval': 2,
                 'anchor': mon, 'weekdays': [1]}
        nxt = self.b.compute_next_run({'schedule': sched, 'next_run': mon}, mon)
        self.assertIsNotNone(nxt)
        self.assertEqual(self.b.weekday_index(nxt), 1)
        self.assertGreater(nxt - mon, self.b.DAY_SECONDS * 6)

    def test_normalize_filters_and_sorts_weekdays(self):
        t = self.b.normalize_task({
            'targets': [{'id': 'filehelper'}], 'text': 'x',
            'schedule': {'type': 'loop', 'unit': 'day', 'interval': 1,
                         'weekdays': [3, 1, 9, 'x', 3, 0]},
        })
        self.assertEqual(t['schedule']['weekdays'], [0, 1, 3])

    def test_normalize_old_task_has_empty_weekdays(self):
        """旧任务里没有这个字段，读出来是空列表，不能报错。"""
        t = self.b.normalize_task({
            'targets': [{'id': 'filehelper'}], 'text': 'x',
            'schedule': {'type': 'loop', 'unit': 'day', 'interval': 1},
        })
        self.assertEqual(t['schedule']['weekdays'], [])

    def test_validate_rejects_bad_weekdays(self):
        with self.assertRaises(ValueError):
            self.b.validate_task({
                'targets': [{'id': 'filehelper'}], 'text': 'x', 'next_run': 1,
                'schedule': {'type': 'loop', 'unit': 'day', 'interval': 1,
                             'weekdays': [9]},
            })


class TestBroadcastRepeatKinds(unittest.TestCase):
    """三种互斥周期类型：每天 / 隔几天 / 每月。

    覆盖连续排期、跨月、31 号不存在于某些月、以及没有新字段的旧任务兼容。
    """

    def setUp(self):
        import broadcast
        self.b = broadcast

    def _ts(self, y, mo, d, hh=9, mm=30):
        import time
        return time.mktime((y, mo, d, hh, mm, 0, 0, 0, -1))

    def _seq(self, sched, start, n):
        """从 start 起连续排 n 次（第一次算「现在」，之后算「刚跑完」）。"""
        cur = self.b.compute_next_run({'schedule': sched, 'next_run': start}, start)
        out = []
        for _ in range(n):
            self.assertIsNotNone(cur, '排期返回了 None')
            out.append(cur)
            cur = self.b.compute_next_run({'schedule': sched, 'next_run': cur}, cur, after_run=True)
        return out

    def _days(self, seq):
        import time
        return [time.strftime('%Y-%m-%d', time.localtime(x)) for x in seq]

    def test_repeat_kind_inference(self):
        """新任务看 repeat；老任务按 unit/interval/weekdays 推断。"""
        self.assertEqual(self.b.repeat_kind({'schedule': {'repeat': 'daily'}}), 'daily')
        self.assertEqual(self.b.repeat_kind({'schedule': {'repeat': 'interval'}}), 'interval')
        self.assertEqual(self.b.repeat_kind({'schedule': {'repeat': 'monthly'}}), 'monthly')
        self.assertEqual(self.b.repeat_kind({'schedule': {'unit': 'day', 'interval': 1}}), 'daily')
        self.assertEqual(self.b.repeat_kind({'schedule': {'unit': 'day', 'interval': 2}}), 'interval')
        self.assertEqual(self.b.repeat_kind({'schedule': {'unit': 'hour', 'interval': 1}}), 'interval')
        self.assertEqual(
            self.b.repeat_kind({'schedule': {'unit': 'day', 'interval': 1, 'weekdays': [1]}}),
            'weekly')

    def test_daily_next_runs(self):
        sched = {'type': 'loop', 'repeat': 'daily', 'unit': 'day', 'interval': 1,
                 'anchor': self._ts(2026, 11, 16)}
        self.assertEqual(self._days(self._seq(sched, sched['anchor'], 3)),
                         ['2026-11-17', '2026-11-18', '2026-11-19'])

    def test_interval_days_next_runs(self):
        sched = {'type': 'loop', 'repeat': 'interval', 'unit': 'day', 'interval': 2,
                 'anchor': self._ts(2026, 11, 16)}
        self.assertEqual(self._days(self._seq(sched, sched['anchor'], 3)),
                         ['2026-11-18', '2026-11-20', '2026-11-22'])

    def test_interval_hours_next_runs(self):
        sched = {'type': 'loop', 'repeat': 'interval', 'unit': 'hour', 'interval': 6,
                 'anchor': self._ts(2026, 11, 16)}
        got = self._seq(sched, sched['anchor'], 3)
        self.assertAlmostEqual(got[0] - sched['anchor'], 6 * 3600, delta=1)
        self.assertAlmostEqual(got[1] - got[0], 6 * 3600, delta=1)

    def test_monthly_cross_month(self):
        """11 月 16 日之后，1/3/6/9 号都过了 —— 要跳到 12 月，再跨到明年 1 月。"""
        sched = {'type': 'loop', 'repeat': 'monthly', 'month_days': [1, 3, 6, 9],
                 'anchor': self._ts(2026, 11, 16)}
        self.assertEqual(self._days(self._seq(sched, sched['anchor'], 6)),
                         ['2026-12-01', '2026-12-03', '2026-12-06',
                          '2026-12-09', '2027-01-01', '2027-01-03'])

    def test_monthly_keeps_time_of_day(self):
        import time
        sched = {'type': 'loop', 'repeat': 'monthly', 'month_days': [6],
                 'anchor': self._ts(2026, 11, 16, 9, 30)}
        ts = self.b.compute_next_run({'schedule': sched, 'next_run': sched['anchor']},
                                     sched['anchor'])
        lt = time.localtime(ts)
        self.assertEqual((lt.tm_mday, lt.tm_hour, lt.tm_min), (6, 9, 30))

    def test_monthly_31_skips_short_months(self):
        """31 号在 2 月/4 月不存在：不能顺延成 3 月 1 日 / 5 月 1 日。"""
        sched = {'type': 'loop', 'repeat': 'monthly', 'month_days': [31],
                 'anchor': self._ts(2026, 12, 15)}
        self.assertEqual(self._days(self._seq(sched, sched['anchor'], 4)),
                         ['2026-12-31', '2027-01-31', '2027-03-31', '2027-05-31'])

    def test_monthly_without_days_returns_none(self):
        sched = {'type': 'loop', 'repeat': 'monthly', 'month_days': [],
                 'anchor': self._ts(2026, 11, 16)}
        self.assertIsNone(
            self.b.compute_next_run({'schedule': sched, 'next_run': sched['anchor']},
                                    sched['anchor']))

    def test_monthly_same_day_after_run_skips_to_next_day(self):
        """同一天发过之后不能原地重复：要跳到下一个选中日期。"""
        sched = {'type': 'loop', 'repeat': 'monthly', 'month_days': [1, 6],
                 'anchor': self._ts(2026, 12, 1)}
        # 12-01 08:00 排期：今天 09:30 还没到，落点就是今天
        early = self._ts(2026, 12, 1, 8, 0)
        first = self.b.compute_next_run({'schedule': sched, 'next_run': sched['anchor']}, early)
        self.assertEqual(self._days([first]), ['2026-12-01'])
        # 发完之后同一天不能再排进来，跳到 12-06
        nxt = self.b.compute_next_run({'schedule': sched, 'next_run': first}, first, after_run=True)
        self.assertEqual(self._days([nxt]), ['2026-12-06'])

    def test_old_task_with_weekdays_still_works(self):
        """上一版存下来的「每周几」任务（文件里没有 repeat）仍按星期触发。"""
        sched = {'type': 'loop', 'unit': 'day', 'interval': 1,
                 'anchor': self._ts(2026, 11, 16), 'weekdays': [1, 3, 5, 0]}
        self.assertEqual(self._days(self._seq(sched, sched['anchor'], 4)),
                         ['2026-11-18', '2026-11-20', '2026-11-22', '2026-11-23'])

    def test_old_task_with_interval_still_works(self):
        sched = {'type': 'loop', 'unit': 'day', 'interval': 2,
                 'anchor': self._ts(2026, 11, 16)}
        self.assertEqual(self._days(self._seq(sched, sched['anchor'], 3)),
                         ['2026-11-18', '2026-11-20', '2026-11-22'])

    def test_old_task_without_any_new_field_still_works(self):
        """最老的任务：只有 type/unit/interval/anchor，读出来不能报错。"""
        t = self.b.normalize_task({
            'targets': [{'id': 'filehelper'}], 'text': 'x',
            'schedule': {'type': 'loop', 'unit': 'day', 'interval': 3,
                         'anchor': self._ts(2026, 11, 16)},
        })
        self.assertEqual(t['schedule']['repeat'], '')
        self.assertEqual(t['schedule']['month_days'], [])
        self.assertEqual(self.b.repeat_kind(t), 'interval')

    def test_normalize_clears_other_kinds_daily(self):
        """互斥：选了 daily 就把 interval / weekdays / month_days 清干净。"""
        t = self.b.normalize_task({
            'targets': [{'id': 'filehelper'}], 'text': 'x',
            'schedule': {'type': 'loop', 'repeat': 'daily', 'unit': 'hour', 'interval': 9,
                         'weekdays': [1, 3], 'month_days': [6]},
        })
        self.assertEqual(t['schedule']['repeat'], 'daily')
        self.assertEqual(t['schedule']['interval'], 1)
        self.assertEqual(t['schedule']['unit'], 'day')
        self.assertEqual(t['schedule']['weekdays'], [])
        self.assertEqual(t['schedule']['month_days'], [])

    def test_normalize_clears_other_kinds_monthly(self):
        t = self.b.normalize_task({
            'targets': [{'id': 'filehelper'}], 'text': 'x',
            'schedule': {'type': 'loop', 'repeat': 'monthly', 'unit': 'hour', 'interval': 7,
                         'weekdays': [2], 'month_days': [9, 1, 9]},
        })
        self.assertEqual(t['schedule']['interval'], 1)
        self.assertEqual(t['schedule']['unit'], 'day')
        self.assertEqual(t['schedule']['weekdays'], [])
        self.assertEqual(t['schedule']['month_days'], [1, 9])

    def test_normalize_keeps_unknown_repeat_blank(self):
        """不认识的值当空处理（由 repeat_kind 推断），不能把任务读坏。"""
        t = self.b.normalize_task({
            'targets': [{'id': 'filehelper'}], 'text': 'x',
            'schedule': {'type': 'loop', 'repeat': 'biweekly', 'unit': 'day', 'interval': 1},
        })
        self.assertEqual(t['schedule']['repeat'], '')
        self.assertEqual(self.b.repeat_kind(t), 'daily')

    def test_validate_monthly_ok_with_days(self):
        self.b.validate_task({
            'targets': [{'id': 'filehelper'}], 'text': 'x', 'next_run': 1,
            'schedule': {'type': 'loop', 'repeat': 'monthly', 'month_days': [6]},
        })

    # ---------- 第二版：每周 / 每月任意日期 ----------

    def test_weekly_next_runs(self):
        """每周：只在勾选的星期触发，跳过其余日子。"""
        sched = {'type': 'loop', 'repeat': 'weekly', 'unit': 'day', 'interval': 1,
                 'anchor': self._ts(2026, 11, 16), 'weekdays': [1, 3, 5]}
        self.assertEqual(self._days(self._seq(sched, sched['anchor'], 4)),
                         ['2026-11-18', '2026-11-20', '2026-11-23', '2026-11-25'])

    def test_weekly_all_seven_days_equals_daily(self):
        sched = {'type': 'loop', 'repeat': 'weekly', 'unit': 'day', 'interval': 1,
                 'anchor': self._ts(2026, 11, 16), 'weekdays': [0, 1, 2, 3, 4, 5, 6]}
        self.assertEqual(self._days(self._seq(sched, sched['anchor'], 3)),
                         ['2026-11-17', '2026-11-18', '2026-11-19'])

    def test_weekly_without_days_returns_none(self):
        """一天都没勾 = 不触发（不静默退化成每天）。"""
        sched = {'type': 'loop', 'repeat': 'weekly', 'unit': 'day', 'interval': 1,
                 'anchor': self._ts(2026, 11, 16), 'weekdays': []}
        self.assertIsNone(self.b.compute_next_run(
            {'schedule': sched, 'next_run': sched['anchor']}, sched['anchor']))

    def test_weekly_allows_empty_days_on_save(self):
        self.b.validate_task({
            'targets': [{'id': 'filehelper'}], 'text': 'x', 'next_run': 1,
            'schedule': {'type': 'loop', 'repeat': 'weekly',
                         'unit': 'day', 'interval': 1, 'weekdays': []},
        })

    def test_normalize_weekly_clears_month_and_interval(self):
        t = self.b.normalize_task({
            'targets': [{'id': 'filehelper'}], 'text': 'x',
            'schedule': {'type': 'loop', 'repeat': 'weekly', 'unit': 'hour', 'interval': 7,
                         'weekdays': [5, 1], 'month_days': [3]},
        })
        self.assertEqual(t['schedule']['repeat'], 'weekly')
        self.assertEqual(t['schedule']['interval'], 1)
        self.assertEqual(t['schedule']['unit'], 'day')
        self.assertEqual(t['schedule']['weekdays'], [1, 5])
        self.assertEqual(t['schedule']['month_days'], [])

    def test_monthly_any_dates(self):
        """月内日期不再限定 1/2/3/6/9，任意组合都按选中日期触发。"""
        sched = {'type': 'loop', 'repeat': 'monthly', 'month_days': [5, 17, 28],
                 'anchor': self._ts(2026, 11, 16)}
        self.assertEqual(self._days(self._seq(sched, sched['anchor'], 5)),
                         ['2026-11-17', '2026-11-28', '2026-12-05',
                          '2026-12-17', '2026-12-28'])

    def test_monthly_allows_empty_days(self):
        """一个都不勾不再拦保存：能保存，只是不排期。"""
        self.b.validate_task({
            'targets': [{'id': 'filehelper'}], 'text': 'x', 'next_run': 1,
            'schedule': {'type': 'loop', 'repeat': 'monthly',
                         'unit': 'day', 'interval': 1, 'month_days': []},
        })

    def test_monthly_empty_days_returns_none(self):
        sched = {'type': 'loop', 'repeat': 'monthly', 'month_days': [],
                 'anchor': self._ts(2026, 11, 16)}
        self.assertIsNone(self.b.compute_next_run(
            {'schedule': sched, 'next_run': sched['anchor']}, sched['anchor']))

    def test_schedule_label_weekly(self):
        self.assertEqual(self.b.schedule_label(
            {'schedule': {'type': 'loop', 'repeat': 'weekly', 'weekdays': [1, 3]}}),
            '每周 周一/周三')
        self.assertEqual(self.b.schedule_label(
            {'schedule': {'type': 'loop', 'repeat': 'weekly', 'weekdays': []}}),
            '每周（未选星期）')

    def test_schedule_label_for_three_kinds(self):
        self.assertEqual(self.b.schedule_label(
            {'schedule': {'type': 'loop', 'repeat': 'daily'}}), '每天')
        self.assertEqual(self.b.schedule_label(
            {'schedule': {'type': 'loop', 'repeat': 'interval', 'unit': 'day', 'interval': 2}}),
            '每 2 天')
        self.assertEqual(self.b.schedule_label(
            {'schedule': {'type': 'loop', 'repeat': 'monthly', 'month_days': [1, 6]}}),
            '每月 1号/6号')
