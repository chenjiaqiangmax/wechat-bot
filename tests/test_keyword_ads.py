# -*- coding: utf-8 -*-
"""关键词广告的纯逻辑与消息接线测试（不触微信、不发真实消息）。

覆盖：关键词归一化匹配、逐群开关与冷却覆盖、冷却持久化、广告回显防护、
配置校验，以及 ``DaFeiYuBot.on_message`` 里「命中就发、自身消息不触发、
冷却期内不重复」这条真实路径（发送用假函数替换，只记录参数）。

所有会写文件的路径都在 ``setUp`` 里换到临时目录，跑完不会污染仓库里的
``keyword_ads.json`` / ``keyword_ads_hits.jsonl`` / ``known_chats.json``。
"""

import os
import random
import shutil
import sys
import tempfile
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import keyword_ads as ka  # noqa: E402

REPLY = '测试广告：便宜的模型看这里'


class KeywordAdsTestBase(unittest.TestCase):
    """每个用例一套独立的临时配置/状态/日志文件。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kwads-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.cfg_path = os.path.join(self.dir, 'keyword_ads.json')
        self.state_path = os.path.join(self.dir, 'state.json')
        self.hits_path = os.path.join(self.dir, 'hits.jsonl')
        self.log_path = os.path.join(self.dir, 'ads.log')
        ka.save_config({
            'enabled': True,
            'default_group_enabled': False,
            'mode': 'hard',
            'keywords': ['便宜的模型', '便宜的 token'],
            'reply': REPLY,
            'cooldown_seconds': 1800,
            'cooldown_jitter_seconds': 0,
            'reply_probability': 1.0,
            'pre_delay_min_seconds': 0,
            'pre_delay_max_seconds': 0,
            'groups': {'on@chatroom': {'enabled': True},
                       '短冷却@chatroom': {'enabled': True,
                                          'cooldown_seconds': 5}},
        }, self.cfg_path)

    def engine(self):
        return ka.Engine(self.cfg_path, self.state_path,
                         self.hits_path, self.log_path)


class TestMatch(KeywordAdsTestBase):
    def test_space_and_case_insensitive(self):
        cfg = ka.load_config(self.cfg_path)
        self.assertEqual(
            ka.match_keywords('有没有 便宜的  TOKEN 渠道', cfg['keywords']),
            ['便宜的 token'])
        self.assertEqual(ka.match_keywords('便宜的token', cfg['keywords']),
                         ['便宜的 token'])
        self.assertEqual(ka.match_keywords('完全无关', cfg['keywords']), [])

    def test_multiple_keywords_in_order(self):
        self.assertEqual(ka.match_keywords('便宜的模型和便宜的token',
                                           ['便宜的 token', '便宜的模型']),
                         ['便宜的 token', '便宜的模型'])

    def test_string_keywords_are_split(self):
        cfg = ka.normalize_config({'keywords': 'a，b\nc、d'})
        self.assertEqual(cfg['keywords'], ['a', 'b', 'c', 'd'])

    def test_duplicate_by_normalized_form(self):
        cfg = ka.normalize_config({'keywords': ['便宜的 token', '便宜的token']})
        self.assertEqual(cfg['keywords'], ['便宜的 token'])


class TestGroupRule(KeywordAdsTestBase):
    def test_default_and_override(self):
        cfg = ka.load_config(self.cfg_path)
        self.assertEqual(ka.group_rule(cfg, 'on@chatroom'), (True, 1800))
        self.assertEqual(ka.group_rule(cfg, 'off@chatroom'), (False, 1800))
        self.assertEqual(ka.group_rule(cfg, '短冷却@chatroom'), (True, 5))

    def test_name_can_be_override_key(self):
        cfg = ka.normalize_config({'groups': {'测试群': {'enabled': True}}})
        self.assertEqual(ka.group_rule(cfg, 'x@chatroom', '测试群')[0], True)

    def test_cooldown_clamped(self):
        cfg = ka.normalize_config({'cooldown_seconds': 999999})
        self.assertEqual(cfg['cooldown_seconds'], ka.MAX_COOLDOWN)
        cfg = ka.normalize_config({'cooldown_seconds': -1})
        self.assertEqual(cfg['cooldown_seconds'], 0)


class TestEngine(KeywordAdsTestBase):
    def test_cooldown_and_persistence(self):
        e = self.engine()
        now = time.time()
        d = e.evaluate('求便宜的模型', 'on@chatroom', '测试群', now=now)
        self.assertTrue(d['hit'] and d['allowed'])
        e.note_sent('on@chatroom', d['reply'], now=now)
        d2 = e.evaluate('求便宜的模型', 'on@chatroom', '测试群', now=now + 60)
        self.assertFalse(d2['allowed'])
        self.assertEqual(d2['reason'], 'cooldown')
        self.assertGreater(d2['remaining'], 0)
        # 冷却过了就能再发
        d3 = e.evaluate('求便宜的模型', 'on@chatroom', '测试群',
                        now=now + 1801)
        self.assertTrue(d3['allowed'])
        # 重启后冷却仍然有效
        e2 = self.engine()
        d4 = e2.evaluate('求便宜的模型', 'on@chatroom', '测试群', now=now + 60)
        self.assertEqual(d4['reason'], 'cooldown')

    def test_disabled_group_does_not_even_match(self):
        d = self.engine().evaluate('便宜的模型', 'off@chatroom', '别的群')
        self.assertFalse(d['hit'])
        self.assertEqual(d['reason'], 'group_disabled')

    def test_global_switch_and_non_group(self):
        ka.save_config({'enabled': False, 'keywords': ['便宜的模型'],
                        'reply': REPLY}, self.cfg_path)
        e = self.engine()
        self.assertEqual(e.evaluate('便宜的模型', 'on@chatroom')['reason'],
                         'disabled')
        ka.save_config({'enabled': True, 'keywords': ['便宜的模型'],
                        'reply': REPLY}, self.cfg_path)
        self.assertEqual(e.evaluate('便宜的模型', 'friend')['reason'],
                         'not_group')

    def test_echo_guard(self):
        e = self.engine()
        now = time.time()
        e.note_sent('on@chatroom', REPLY, now=now)
        self.assertTrue(e.is_echo('on@chatroom', REPLY, now=now + 5))
        self.assertFalse(e.is_echo('on@chatroom', REPLY, now=now + ka.ECHO_WINDOW + 1))
        self.assertFalse(e.is_echo('other@chatroom', REPLY, now=now + 5))


class TestPersistenceHelpers(KeywordAdsTestBase):
    def test_save_config_validates(self):
        with self.assertRaises(ValueError):
            ka.save_config({'keywords': [], 'reply': 'x'}, self.cfg_path)
        with self.assertRaises(ValueError):
            ka.save_config({'keywords': ['a'], 'reply': ''}, self.cfg_path)

    def test_log_hit_and_read_back(self):
        e = self.engine()
        e.log_hit({'group_id': 'on@chatroom', 'group_name': '测试群',
                   'keywords': '便宜的模型', 'reply': REPLY,
                   'result': 'sent'})
        rows = ka.read_hits(path=self.hits_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['group_name'], '测试群')
        self.assertEqual(rows[0]['result'], 'sent')
        self.assertTrue(os.path.getsize(self.log_path) > 0)

    def test_broken_json_disables_instead_of_defaulting(self):
        with open(self.cfg_path, 'w', encoding='utf-8') as f:
            f.write('{ not json')
        cfg = ka.load_config(self.cfg_path)
        self.assertFalse(cfg['enabled'])


class _FakeMsg:
    def __init__(self, text, attr='friend', sender='张三',
                 wxid='wxid_friend', sender_wxid=None):
        self.content = text
        self.type = 'text'
        self.attr = attr
        self.sender = sender
        self.sender_wxid = sender_wxid or wxid
        self.wxid = wxid
        self.id = str(time.time_ns())
        self.create_time = time.time()


class _FakeDB:
    def __init__(self, wxid='wxid_self'):
        self.wxid = wxid


class _FakeChat:
    def __init__(self, who='on@chatroom', name='测试群', db=None,
                 members=None):
        self.who = who
        self.nickname = name
        self._db = db
        self._members = members if members is not None else [
            {'username': 'wxid_friend', 'nick_name': '张三',
             'remark': None, 'is_owner': 0},
        ]
        self.sent = []
        self.calls = []

    def GetGroupMembers(self):
        return list(self._members)

    def SendMsg(self, text, **kw):
        self.sent.append(text)
        self.calls.append((text, kw))
        return None


class TestSoftMode(KeywordAdsTestBase):
    """软模式、意图闸门与节奏字段的纯逻辑测试。"""

    def setUp(self):
        super().setUp()
        ka.save_config({
            'enabled': True,
            'default_group_enabled': False,
            'mode': 'soft',
            'keywords': ['便宜的模型', '便宜的 token', '低价api'],
            'reply': 'https://example.test 5折',
            'soft_replies': ['{sender} 看这：{keyword}\n{reply}'],
            'soft_require_intent': True,
            'soft_intent_words': ['哪里', '求', '有没有', '推荐'],
            'cooldown_seconds': 0,
            'cooldown_jitter_seconds': 0,
            'reply_probability': 1.0,
            'pre_delay_min_seconds': 0,
            'pre_delay_max_seconds': 0,
            'groups': {'on@chatroom': {'enabled': True}},
        }, self.cfg_path)

    def engine(self, seed=1):
        return ka.Engine(self.cfg_path, self.state_path, self.hits_path,
                         self.log_path, rng=random.Random(seed))

    def test_soft_question_renders_template(self):
        d = self.engine().evaluate('哪里有便宜的 token？', 'on@chatroom',
                                   sender_name='张三')
        self.assertTrue(d['allowed'])
        self.assertEqual(d['mode'], 'soft')
        self.assertIn('https://example.test', d['reply'])
        self.assertIn('张三', d['reply'])
        self.assertIn('便宜的 token', d['reply'])
        self.assertNotIn('{reply}', d['reply'])

    def test_soft_statement_skip_does_not_consume_cooldown(self):
        e = self.engine()
        now = time.time()
        d = e.evaluate('低价api', 'on@chatroom', '测试群', '张三', now=now)
        self.assertFalse(d['allowed'])
        self.assertEqual(d['reason'], 'soft_no_intent')
        d2 = e.evaluate('求便宜的模型', 'on@chatroom', '测试群', '张三',
                        now=now + 1)
        self.assertTrue(d2['allowed'])

    def test_soft_intent_gate_off_allows_statement(self):
        raw = ka.load_config(self.cfg_path)
        raw['soft_require_intent'] = False
        ka.save_config(raw, self.cfg_path)
        d = self.engine().evaluate('低价api', 'on@chatroom', sender_name='张三')
        self.assertTrue(d['allowed'])

    def test_hard_group_override(self):
        raw = ka.load_config(self.cfg_path)
        raw['groups']['on@chatroom']['mode'] = 'hard'
        ka.save_config(raw, self.cfg_path)
        d = self.engine().evaluate('哪里有便宜的 token？', 'on@chatroom',
                                   sender_name='张三')
        self.assertEqual(d['mode'], 'hard')
        self.assertEqual(d['reply'], 'https://example.test 5折')

    def test_template_validation(self):
        base = {
            'keywords': ['a'], 'reply': 'x', 'mode': 'soft',
            'soft_replies': ['没有占位符'],
        }
        with self.assertRaises(ValueError):
            ka.save_config(base, self.cfg_path)
        base['soft_replies'] = ['{reply} {unknown}']
        with self.assertRaises(ValueError):
            ka.save_config(base, self.cfg_path)
        base['soft_replies'] = ['看这里：{reply}']
        cfg = ka.save_config(base, self.cfg_path)
        self.assertEqual(cfg['soft_replies'], ['看这里：{reply}'])

    def test_empty_templates_fall_back_to_builtin(self):
        cfg = ka.normalize_config({'keywords': ['a'], 'reply': 'x',
                                   'soft_replies': []})
        self.assertEqual(len(cfg['soft_replies']), len(ka.DEFAULT_SOFT_REPLIES))
        self.assertTrue(all('{reply}' in t for t in cfg['soft_replies']))

    def test_bad_template_is_skipped_on_load(self):
        cfg = ka.normalize_config({'keywords': ['a'], 'reply': 'x',
                                   'soft_replies': ['{reply}{bad}']})
        self.assertEqual(cfg['soft_replies'], ka.DEFAULT_SOFT_REPLIES)

    def test_old_config_defaults_to_soft(self):
        cfg = ka.normalize_config({'keywords': ['a'], 'reply': 'x'})
        self.assertEqual(cfg['mode'], 'soft')
        self.assertNotIn('daily_limit', cfg)
        self.assertNotIn('hourly_limit', cfg)
        self.assertNotIn('sender_cooldown_seconds', cfg)

    def test_cooldown_jitter_range(self):
        base = {
            'keywords': ['a'], 'reply': 'x', 'mode': 'hard',
            'cooldown_seconds': 1800, 'cooldown_jitter_seconds': 600,
            'pre_delay_min_seconds': 0, 'pre_delay_max_seconds': 0,
            'groups': {'on@chatroom': {'enabled': True}},
        }
        ka.save_config(base, self.cfg_path)
        d = self.engine(seed=2).evaluate('a', 'on@chatroom')
        self.assertTrue(1200 <= d['cooldown'] <= 2400)

    def test_probability_skip_and_no_cooldown_consumption(self):
        base = {
            'keywords': ['a'], 'reply': 'x', 'mode': 'hard',
            'cooldown_seconds': 0, 'reply_probability': 0,
            'pre_delay_min_seconds': 0, 'pre_delay_max_seconds': 0,
            'groups': {'on@chatroom': {'enabled': True}},
        }
        ka.save_config(base, self.cfg_path)
        d = self.engine(seed=3).evaluate('a', 'on@chatroom')
        self.assertEqual(d['reason'], 'probability_skip')
        base['reply_probability'] = 1
        ka.save_config(base, self.cfg_path)
        self.assertTrue(self.engine().evaluate('a', 'on@chatroom')['allowed'])

    def test_pre_delay_range(self):
        base = {
            'keywords': ['a'], 'reply': 'x', 'mode': 'hard',
            'cooldown_seconds': 0,
            'pre_delay_min_seconds': 1, 'pre_delay_max_seconds': 5,
            'groups': {'on@chatroom': {'enabled': True}},
        }
        ka.save_config(base, self.cfg_path)
        d = self.engine(seed=4).evaluate('a', 'on@chatroom')
        self.assertTrue(1 <= d['pre_delay'] <= 5)

    def test_state_only_has_last_sent(self):
        e = self.engine()
        e.note_sent('on@chatroom', 'x')
        with open(self.state_path, encoding='utf-8') as f:
            import json
            state = json.load(f)
        self.assertEqual(set(state), {'last_sent'})


class TestBotWiring(KeywordAdsTestBase):
    """走真实 on_message：关键词命中就发，自身消息与冷却期不重复。"""

    def setUp(self):
        super().setUp()
        import dafeiyu_bot as botmod
        self.botmod = botmod
        self._saved = {}
        for name, val in (('KNOWN_CHATS_FILE',
                           os.path.join(self.dir, 'known_chats.json')),
                          ('PENDING_REPLIES_FILE',
                           os.path.join(self.dir, 'pending.json'))):
            self._saved[name] = getattr(botmod, name)
            setattr(botmod, name, val)
            self.addCleanup(setattr, botmod, name, self._saved[name])
        self.bot = botmod.DaFeiYuBot()
        # 不依赖仓库里真实的 bot_config.json：用户可能把默认 Agent 停用或
        # 改了触发名，那会让「@ 机器人不补广告」这条测试失效。
        self.bot.agents = [{
            'id': 'default', 'name': '大肥鱼', 'at_name': '大肥鱼',
            'enabled': True, 'groups': ['*'], 'recent_n': 20,
            'system_prompt': '', 'model': '', 'temperature': '',
            'max_tokens': '', 'tools': [],
        }]
        self.bot._keyword_ads = self.engine()
        # 被 @ 的消息会走正常回答链路；测试里没有模型，替换成空回答避免真发请求
        self.bot.think = lambda *a, **k: ('', None)
        self.sent = []
        self.retry_calls = []

        def fake_retry(send, desc='消息', **kw):
            self.retry_calls.append((desc, kw))
            send()
            self.sent.append(desc)
            return True, None

        self.bot._send_retry = fake_retry
        # 本类测的是「真 @ 接线」。ENABLE_UIA 关掉时 on_message 会走纯文本
        # 降级（真 @ 依赖 UIA 认人），那是另一条路径，由
        # test_uia_disabled_degrades_to_plain_text 单独覆盖。
        from wechatauto.param import WxParam
        self._saved_uia = WxParam.ENABLE_UIA
        WxParam.ENABLE_UIA = True
        self.addCleanup(setattr, WxParam, 'ENABLE_UIA', self._saved_uia)

    def test_uia_disabled_degrades_to_plain_text(self):
        """关掉 UIA 后真 @ 做不了，广告要降级为纯文本前缀照常发出去。"""
        from wechatauto.param import WxParam
        WxParam.ENABLE_UIA = False
        chat = _FakeChat()
        self.bot.on_message(_FakeMsg('有没有便宜的 token 渠道'), chat)
        self.assertEqual(len(chat.calls), 1)
        text, kw = chat.calls[0]
        self.assertTrue(text.startswith('@张三 '), text)
        self.assertEqual(kw, {'verify': True})

    def test_hit_sends_and_logs(self):
        chat = _FakeChat()
        self.bot.on_message(_FakeMsg('有没有便宜的 token 渠道'), chat)
        self.assertEqual(chat.sent, [REPLY])
        rows = ka.read_hits(path=self.hits_path)
        self.assertEqual(rows[0]['keywords'], '便宜的 token')
        self.assertEqual(rows[0]['result'], 'sent')
        self.assertEqual(rows[0]['group_id'], 'on@chatroom')

    def test_cooldown_blocks_second_message(self):
        chat = _FakeChat()
        self.bot.on_message(_FakeMsg('便宜的模型'), chat)
        self.bot.on_message(_FakeMsg('再问一次便宜的模型'), chat)
        self.assertEqual(len(chat.sent), 1)
        rows = ka.read_hits(path=self.hits_path)
        self.assertEqual(rows[0]['result'], 'cooldown')

    def test_self_message_ignored(self):
        chat = _FakeChat()
        self.bot.on_message(_FakeMsg('便宜的模型', attr='self'), chat)
        self.assertEqual(chat.sent, [])

    def test_sender_wxid_self_ignored_even_if_attr_friend(self):
        chat = _FakeChat(db=_FakeDB('wxid_self'))
        self.bot.on_message(_FakeMsg('便宜的模型', attr='friend',
                                     wxid='wxid_self'), chat)
        self.assertEqual(chat.sent, [])

    def test_echo_of_own_ad_ignored(self):
        # 冷却设为 0，排除「冷却拦截」的干扰，验证的必须是回显防护本身
        ka.save_config({'enabled': True, 'default_group_enabled': False,
                        'keywords': ['便宜的模型'], 'reply': REPLY,
                        'cooldown_seconds': 0,
                        'groups': {'on@chatroom': {'enabled': True}}},
                       self.cfg_path)
        self.bot._keyword_ads = self.engine()
        chat = _FakeChat()
        # 真实顺序：先记「这条广告是我发的」，再模拟数据库把它标成 friend 收回来
        self.bot._keyword_ads.note_sent('on@chatroom', REPLY)
        self.bot.on_message(_FakeMsg(REPLY, attr='friend'), chat)
        self.assertEqual(chat.sent, [])

    def test_disabled_group_no_send(self):
        chat = _FakeChat(who='off@chatroom', name='没启用的群')
        self.bot.on_message(_FakeMsg('便宜的模型'), chat)
        self.assertEqual(chat.sent, [])

    def test_message_mentioning_bot_is_not_ad(self):
        chat = _FakeChat()
        self.bot.on_message(_FakeMsg('@大肥鱼 便宜的模型怎么选', attr='friend'),
                            chat)
        self.assertEqual(chat.sent, [])

    def test_hit_sends_with_real_at_and_verify(self):
        chat = _FakeChat()
        self.bot.on_message(_FakeMsg('有没有便宜的 token'), chat)
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(chat.calls[0][1], {'at': '张三', 'verify': True})
        self.assertEqual(self.retry_calls[0][1].get('retries'), 0)

    def _set_ad_config(self, **extra):
        cfg = {
            'enabled': True,
            'default_group_enabled': False,
            'mode': 'soft',
            'keywords': ['便宜的模型', '便宜的 token', '低价api'],
            'reply': 'https://example.test 5折',
            'soft_replies': ['{sender} 看这：{keyword}\n{reply}'],
            'soft_require_intent': True,
            'soft_intent_words': ['哪里', '求', '有没有', '推荐'],
            'cooldown_seconds': 0,
            'cooldown_jitter_seconds': 0,
            'reply_probability': 1.0,
            'pre_delay_min_seconds': 0,
            'pre_delay_max_seconds': 0,
            'groups': {'on@chatroom': {'enabled': True}},
        }
        cfg.update(extra)
        ka.save_config(cfg, self.cfg_path)
        self.bot._keyword_ads = self.engine()

    def test_soft_question_bot_sends_soft_reply_with_at(self):
        self._set_ad_config()
        chat = _FakeChat()
        self.bot.on_message(_FakeMsg('哪里有便宜的 token？'), chat)
        self.assertEqual(len(chat.sent), 1)
        self.assertIn('https://example.test', chat.sent[0])
        self.assertEqual(chat.calls[0][1], {'at': '张三', 'verify': True})
        rows = ka.read_hits(path=self.hits_path)
        self.assertEqual(rows[0]['mode'], 'soft')
        self.assertEqual(rows[0]['result'], 'sent')

    def test_soft_statement_bot_does_not_send(self):
        self._set_ad_config()
        chat = _FakeChat()
        self.bot.on_message(_FakeMsg('低价api'), chat)
        self.assertEqual(chat.sent, [])
        rows = ka.read_hits(path=self.hits_path)
        self.assertEqual(rows[0]['result'], 'soft_skip')

    def test_ambiguous_at_does_not_send(self):
        self._set_ad_config()
        chat = _FakeChat(members=[
            {'username': 'wxid_friend', 'nick_name': '张三', 'remark': None},
            {'username': 'wxid_other', 'nick_name': '张三', 'remark': None},
        ])
        self.bot.on_message(_FakeMsg('有没有便宜的 token'), chat)
        self.assertEqual(chat.sent, [])
        rows = ka.read_hits(path=self.hits_path)
        self.assertEqual(rows[0]['result'], 'at_ambiguous')

    def test_unresolvable_at_does_not_send(self):
        self._set_ad_config()
        chat = _FakeChat(members=[
            {'username': 'wxid_other', 'nick_name': '李四', 'remark': None},
        ])
        self.bot.on_message(_FakeMsg('有没有便宜的 token'), chat)
        self.assertEqual(chat.sent, [])
        rows = ka.read_hits(path=self.hits_path)
        self.assertEqual(rows[0]['result'], 'at_unresolvable')

    def test_probability_skip_does_not_send(self):
        self._set_ad_config(reply_probability=0)
        chat = _FakeChat()
        self.bot.on_message(_FakeMsg('有没有便宜的 token'), chat)
        self.assertEqual(chat.sent, [])
        rows = ka.read_hits(path=self.hits_path)
        self.assertEqual(rows[0]['result'], 'probability_skip')

    def test_hard_group_override_sends_hard_reply(self):
        self._set_ad_config(
            groups={'on@chatroom': {'enabled': True, 'mode': 'hard'}})
        chat = _FakeChat()
        self.bot.on_message(_FakeMsg('哪里有便宜的 token？'), chat)
        self.assertEqual(chat.sent, ['https://example.test 5折'])
        rows = ka.read_hits(path=self.hits_path)
        self.assertEqual(rows[0]['mode'], 'hard')


if __name__ == '__main__':
    unittest.main()
