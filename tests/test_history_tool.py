"""跨会话检索：能力接出来了，而且提示词口径对（2026-09-25）。

起因是真机截图：用户问「我今天问了什么问题」，模型答「我这边不存你之前的聊天内容」
并去翻目录。根因两条 —— 没有工具、提示词里还有「工作区」。这个文件两边都钉住。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "python"))

import prompt_build  # noqa: E402
import sidecar  # noqa: E402
import tools  # noqa: E402

SECRET = "蓝色独角兽"


class ConversationSearchTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = sidecar._DATA_DIR
        self._old_store = getattr(sidecar, "_SESSIONS", None)
        sidecar._DATA_DIR = self._tmp.name
        sidecar._SESSIONS = None
        tools.register_defaults()

    def tearDown(self):
        sidecar._SESSIONS = self._old_store
        sidecar._DATA_DIR = self._old_dir
        self._tmp.cleanup()

    def _seed(self):
        store = sidecar._session_store()
        a = store.create_session(model="MiniMax-M3", title="昨天的暗号")
        store.append_message(a, {"role": "user",
                                 "content": "记住这个暗号：%s。只回复两个字：收到。" % SECRET})
        store.append_message(a, {"role": "assistant", "content": "收到。"})
        b = store.create_session(model="MiniMax-M3", title="今天的闲聊")
        store.append_message(b, {"role": "user", "content": "今天天气不错"})
        return a, b

    def test_tool_is_registered_and_advertised(self):
        names = [s["function"]["name"] for s in tools.schemas()]
        self.assertIn("search_conversations", names)

    def test_search_returns_title_time_and_role(self):
        self._seed()
        hits = sidecar._search_conversations(SECRET)
        self.assertTrue(hits, "跨会话检索没命中")
        hit = hits[0]
        self.assertEqual(hit["sessionTitle"], "昨天的暗号")
        self.assertEqual(hit["role"], "user")
        self.assertIn("暗号", hit["snippet"])          # 上下文在
        self.assertIn("[", hit["snippet"])             # FTS5 用方括号标出命中片段
        self.assertTrue(hit["when"], "命中里没带时间")

    def test_sessions_do_not_leak_into_each_other(self):
        self._seed()
        self.assertEqual(sidecar._search_conversations("今天天气"), sidecar._search_conversations("今天天气"))
        self.assertFalse([h for h in sidecar._search_conversations("天气不错")
                          if h["sessionTitle"] == "昨天的暗号"])

    def test_tool_output_is_human_readable(self):
        self._seed()
        r = tools.execute("search_conversations", {"query": SECRET}, workspace_root=self._tmp.name)
        self.assertTrue(r.ok, r.content)
        self.assertIn("昨天的暗号", r.content)
        self.assertIn("用户", r.content)          # 角色翻成人话

    def test_no_hits_is_success_not_failure(self):
        r = tools.execute("search_conversations", {"query": "绝对不存在的词"},
                          workspace_root=self._tmp.name)
        self.assertTrue(r.ok, "查不到不该算失败")
        self.assertIn("没有找到", r.content)

    def test_empty_query_rejected(self):
        r = tools.execute("search_conversations", {"query": "  "}, workspace_root=self._tmp.name)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, "BAD_REQUEST")

    def test_unwired_hook_says_so_instead_of_lying(self):
        """没接上取数实现时如实报错，不许返回「没找到」（那是假答案）。"""
        old = tools._CONVERSATION_SEARCH
        tools.set_conversation_search(None)
        try:
            r = tools.execute("search_conversations", {"query": "x"},
                              workspace_root=self._tmp.name)
            self.assertFalse(r.ok)
            self.assertEqual(r.error_code, "UNAVAILABLE")
        finally:
            tools._CONVERSATION_SEARCH = old


class TimeWindowSearchTest(unittest.TestCase):
    """「我今天问了什么」这类问题关键词搜不出来 —— 得能按时间查（2026-09-25 真机）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = sidecar._DATA_DIR
        self._old_store = getattr(sidecar, "_SESSIONS", None)
        sidecar._DATA_DIR = self._tmp.name
        sidecar._SESSIONS = None
        tools.register_defaults()
        store = sidecar._session_store()
        sid = store.create_session(model="MiniMax-M3", title="今天的问题")
        store.append_message(sid, {"role": "user", "content": "帮我看一下这个报错"})
        store.append_message(sid, {"role": "assistant", "content": "好的"})
        store.append_message(sid, {"role": "user", "content": "再跑一遍测试"})

    def tearDown(self):
        sidecar._SESSIONS = self._old_store
        sidecar._DATA_DIR = self._old_dir
        self._tmp.cleanup()

    def test_since_today_lists_the_users_messages(self):
        r = tools.execute("search_conversations", {"since": "today"},
                          workspace_root=self._tmp.name)
        self.assertTrue(r.ok, r.content)
        self.assertIn("帮我看一下这个报错", r.content)
        self.assertIn("再跑一遍测试", r.content)
        self.assertIn("今天的问题", r.content)          # 带会话标题
        self.assertNotIn("好的", r.content)             # 默认只列用户说的

    def test_since_accepts_relative_and_iso(self):
        for token in ("today", "7d", "昨天"):
            r = tools.execute("search_conversations", {"since": token},
                              workspace_root=self._tmp.name)
            self.assertTrue(r.ok, "%s -> %s" % (token, r.content))

    def test_bad_since_token_is_not_an_empty_answer(self):
        """说不清的 since 不许当「没找到」返回 —— 那会变成一句假话。"""
        r = tools.execute("search_conversations", {"since": "银河纪元"},
                          workspace_root=self._tmp.name)
        self.assertTrue(r.ok)
        self.assertIn("没有找到", r.content)            # 明确说没找到，而不是编

    def test_neither_query_nor_since_is_rejected(self):
        r = tools.execute("search_conversations", {}, workspace_root=self._tmp.name)
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, "BAD_REQUEST")

    def test_since_resolution_is_local_day_boundary(self):
        """本地「今天」0 点要换算成 UTC（东八区差 8 小时），否则当天的记录会漏。"""
        import datetime as dt
        bound = sidecar._resolve_since("today")
        parsed = dt.datetime.strptime(bound, "%Y-%m-%d %H:%M:%S")
        local = dt.datetime.now()
        expected_utc = local.replace(hour=0, minute=0, second=0, microsecond=0) + (
            dt.datetime.utcnow() - local)
        self.assertLess(abs((parsed - expected_utc).total_seconds()), 120)


class PromptWordingTest(unittest.TestCase):
    """提示词口径：不许再说「工作区」，必须告诉模型能检索历史。"""

    def _prompt(self) -> str:
        tools.register_defaults()
        # 用 Tool 对象（生产里 sidecar 传的就是 tools.list_tools()）——
        # 能力与边界那一节是按对象渲染的，传 schema dict 会让它整节为空。
        return prompt_build.build(workspace="/tmp/desk",
                                  skills=[], tools=tools.list_tools())

    def test_no_workspace_wording(self):
        p = self._prompt()
        self.assertNotIn("工作区", p, "提示词里还在说工作区，模型就会满口工作区")

    def test_history_capability_is_advertised(self):
        p = self._prompt()
        self.assertIn("search_conversations", p)
        self.assertIn("检索历史对话", p)

    def test_guidance_follows_the_tool(self):
        """引导跟着工具走：工具没装就不许提它（参照 Hermes Agent 的 gating）。"""
        without = prompt_build.build(
            workspace="/tmp/desk", skills=[],
            tools=[{"function": {"name": "read_file"}}])
        self.assertNotIn("search_conversations", without)

    def test_workflow_tells_the_model_to_search_instead_of_denying(self):
        p = self._prompt()
        self.assertIn("search_conversations", p)
        self.assertIn("不存聊天记录", p)           # 明确禁止这句假话
        self.assertIn("since", p)                  # 时间性问题怎么问


if __name__ == "__main__":
    unittest.main()