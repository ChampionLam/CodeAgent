"""会话标题 = 模型生成的短标题（t_37f5d060）。

覆盖验收口径的五个场景（桩模型，零真实网络）：
  ① 正常生成：模型输出 → 清洗（剥引号/句号、≤14字）→ 落库 + 'auto' 标记；
  ② 用户改过标题（title_source='user'）→ 不生成、不覆盖；
  ③ 模型调用失败 → 回退现有逻辑（首问截断 30 字）且不报错；
  ④ 空会话（没有有效用户消息）→ 不生成，连回退都不走；
  ⑤ 同一会话第二次调用 → 已是 'auto'，直接跳过（只生成一次）。

另覆盖：set_generated_title 的 SQL 条件（NULL 老行可写、auto 不可写）、
rename_session(source=) 的标记语义、模型输出不合规格时的清洗兜底。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))  # noqa: E402

import chat_persist        # noqa: E402
import sessionstore        # noqa: E402
import llm                 # noqa: E402


LONG_QUESTION = (
    "我想用 Python 把我们公司内部那套很老的 Excel 报表流程自动化掉，"
    "大概有二十多张表，每周要从各个部门收上来的文件里抽数、做校验、"
    "再汇总成一张给老板看的总表，现在全是人工复制粘贴，特别容易出错，"
    "能不能帮我设计一个整体的方案，包括用什么库、怎么组织目录、"
    "以及怎么让不懂技术的同事也能一键运行？"
)


class _FakeCfg:
    """够 llm.chat_messages_once 认的最小形状（本测试直接 monkeypatch llm）。"""


class TitleCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desk-title-gen-")
        self.store = sessionstore.SessionStore(os.path.join(self.dir, "sessions.db"))
        self.sid = self.store.create_session(model="MiniMax-M3", base_url=None,
                                             title="New session")
        self._old = llm.chat_messages_once
        self.calls: list[dict] = []

    def tearDown(self) -> None:
        llm.chat_messages_once = self._old
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _stub_model(self, output: str | Exception):
        def fake(cfg, messages, **kw):
            self.calls.append({"messages": messages, **kw})
            if isinstance(output, Exception):
                raise output
            return output
        chat_persist.llm = llm  # 兼容旧桩写法（现直接 patch llm 模块）
        llm.chat_messages_once = fake

    def _title_row(self):
        row = self.store.get_session(self.sid)
        return row.get("title"), row.get("title_source")

    def _first_turn(self):
        return ([{"role": "user", "content": LONG_QUESTION}],
                [{"role": "system", "content": "x"},
                 {"role": "user", "content": LONG_QUESTION},
                 {"role": "assistant", "content": "可以从 pandas + openpyxl 入手……"}],
                [{"role": "user", "content": LONG_QUESTION}])

    # ── ① 正常生成 ────────────────────────────────────────────────────────
    def test_generated_short_title_lands_and_marks_auto(self):
        self._stub_model("「Excel 报表自动化」")
        msgs, loop, req = self._first_turn()
        title = chat_persist.generate_session_title(
            self.store, self.sid, messages=msgs, loop_messages=loop,
            request_messages=req, cfg=_FakeCfg())
        self.assertEqual(title, "Excel 报表自动化")
        self.assertEqual(self._title_row(), ("Excel 报表自动化", "auto"))
        # 契约：一次小请求、max_tokens 很小、不带 tools、非流式(once 调用)
        self.assertEqual(len(self.calls), 1)
        self.assertLessEqual(self.calls[0].get("max_tokens"), 64)
        self.assertNotIn("tools", self.calls[0])
        # 提示词带上了问答双方
        prompt = self.calls[0]["messages"][0]["content"]
        self.assertIn(LONG_QUESTION[:20], prompt)
        self.assertIn("助手：", prompt)

    def test_model_output_cleaned(self):
        for raw, want in [
            ('"读文档改脚本"', "读文档改脚本"),
            ("# 调试登录超时。", "调试登录超时"),
            ("-  写周报汇总 ", "写周报汇总"),
            ("好的，标题是：数据清洗脚本", "好的，标题是：数据清洗脚本"),  # 不硬剥前缀，只截长
            ("这是一个非常非常非常非常非常长的标题超过十四个字了", "这是一个非常非常非常非常非常长的"[:14]),
        ]:
            self._stub_model(raw)
            msgs, loop, req = self._first_turn()
            title = chat_persist.generate_session_title(
                self.store, self.sid, messages=msgs, loop_messages=loop,
                request_messages=req, cfg=_FakeCfg())
            self.assertEqual(title, want, "raw=%r" % raw)
            # 每轮重开一个会话，避免 'auto' 已标记挡住下一轮
            self.sid = self.store.create_session(model="m", base_url=None,
                                                 title="New session")

    # ── ② 用户改过标题：不许覆盖 ──────────────────────────────────────────
    def test_user_renamed_title_is_not_overwritten(self):
        self._stub_model("不该出现的标题")
        self.store.rename_session(self.sid, "用户手改的标题", source="user")
        msgs, loop, req = self._first_turn()
        title = chat_persist.generate_session_title(
            self.store, self.sid, messages=msgs, loop_messages=loop,
            request_messages=req, cfg=_FakeCfg())
        self.assertIsNone(title)
        self.assertEqual(self._title_row(), ("用户手改的标题", "user"))
        self.assertEqual(self.calls, [])   # 锁定在调用模型之前判断，零请求

    def test_project_title_is_not_overwritten(self):
        self._stub_model("不该出现的标题")
        self.store.rename_session(self.sid, "项目名标题", source="project")
        msgs, loop, req = self._first_turn()
        self.assertIsNone(chat_persist.generate_session_title(
            self.store, self.sid, messages=msgs, loop_messages=loop,
            request_messages=req, cfg=_FakeCfg()))
        self.assertEqual(self._title_row(), ("项目名标题", "project"))

    # ── ③ 模型失败 → 回退现有逻辑，不报错 ────────────────────────────────
    def test_model_failure_falls_back_to_first_question_clip(self):
        self._stub_model(RuntimeError("HTTP 503"))
        msgs, loop, req = self._first_turn()
        title = chat_persist.generate_session_title(
            self.store, self.sid, messages=msgs, loop_messages=loop,
            request_messages=req, cfg=_FakeCfg())
        # 现有逻辑：首问截断 30 字
        self.assertEqual(title, LONG_QUESTION[:30])
        self.assertEqual(self._title_row(), (LONG_QUESTION[:30], "auto"))

    def test_no_config_falls_back_without_model_call(self):
        self._stub_model("不该被调用")
        msgs, loop, req = self._first_turn()
        title = chat_persist.generate_session_title(
            self.store, self.sid, messages=msgs, loop_messages=loop,
            request_messages=req, cfg=None)
        self.assertEqual(title, LONG_QUESTION[:30])
        self.assertEqual(self.calls, [])

    def test_malformed_output_falls_back(self):
        self._stub_model("“”")     # 剥完为空 → 回退
        msgs, loop, req = self._first_turn()
        title = chat_persist.generate_session_title(
            self.store, self.sid, messages=msgs, loop_messages=loop,
            request_messages=req, cfg=_FakeCfg())
        self.assertEqual(title, LONG_QUESTION[:30])

    # ── ④ 空会话：不生成 ──────────────────────────────────────────────────
    def test_empty_session_generates_nothing(self):
        self._stub_model("不该被调用")
        title = chat_persist.generate_session_title(
            self.store, self.sid, messages=[], loop_messages=[],
            request_messages=[], cfg=_FakeCfg())
        self.assertIsNone(title)
        self.assertEqual(self._title_row(), ("New session", None))
        self.assertEqual(self.calls, [])

    def test_blank_user_message_generates_nothing(self):
        title = chat_persist.generate_session_title(
            self.store, self.sid,
            messages=[{"role": "user", "content": "   "}],
            loop_messages=[], request_messages=[], cfg=_FakeCfg())
        self.assertIsNone(title)

    def test_missing_session_id_is_none(self):
        self.assertIsNone(chat_persist.generate_session_title(
            self.store, None, messages=[{"role": "user", "content": "x"}],
            cfg=_FakeCfg()))

    # ── ⑤ 只生成一次 ──────────────────────────────────────────────────────
    def test_second_call_is_skipped(self):
        self._stub_model("Excel 报表自动化")
        msgs, loop, req = self._first_turn()
        first = chat_persist.generate_session_title(
            self.store, self.sid, messages=msgs, loop_messages=loop,
            request_messages=req, cfg=_FakeCfg())
        self.assertEqual(first, "Excel 报表自动化")
        self.assertEqual(len(self.calls), 1)
        # 第二轮（用户接着聊）：'auto' 已标记 → 直接跳过，零请求
        msgs2 = msgs + [{"role": "user", "content": "再细化一下第一点"}]
        second = chat_persist.generate_session_title(
            self.store, self.sid, messages=msgs2,
            loop_messages=loop + [{"role": "user", "content": "再细化一下第一点"},
                                  {"role": "assistant", "content": "好"}],
            request_messages=msgs2, cfg=_FakeCfg())
        self.assertIsNone(second)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self._title_row(), ("Excel 报表自动化", "auto"))


class StoreSemantics(unittest.TestCase):
    """set_generated_title / rename_session 的 SQL 语义。"""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="desk-title-store-")
        self.store = sessionstore.SessionStore(os.path.join(self.dir, "sessions.db"))

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_old_rows_null_source_are_writable(self):
        sid = self.store.create_session(model="m", base_url=None, title="t")
        self.store._conn.execute("UPDATE sessions SET title_source=NULL WHERE id=?", (sid,))
        self.store._conn.commit()
        self.assertTrue(self.store.set_generated_title(sid, "新标题"))
        self.assertEqual(self.store.get_session(sid)["title"], "新标题")
        self.assertEqual(self.store.get_session(sid)["title_source"], "auto")

    def test_set_generated_title_twice_second_fails(self):
        sid = self.store.create_session(model="m", base_url=None, title="t")
        self.assertTrue(self.store.set_generated_title(sid, "一"))
        self.assertFalse(self.store.set_generated_title(sid, "二"))
        self.assertEqual(self.store.get_session(sid)["title"], "一")

    def test_set_generated_title_respects_user_lock(self):
        sid = self.store.create_session(model="m", base_url=None, title="t")
        self.store.rename_session(sid, "用户标题", source="user")
        self.assertFalse(self.store.set_generated_title(sid, "自动标题"))
        self.assertEqual(self.store.get_session(sid)["title"], "用户标题")
        self.assertTrue(self.store.session_title_is_locked(sid))

    def test_rename_without_source_keeps_marker(self):
        sid = self.store.create_session(model="m", base_url=None, title="t")
        self.store.rename_session(sid, "auto 标题")            # 无 source：兼容老调用
        self.assertEqual(self.store.get_session(sid)["title_source"], None)
        self.store.set_generated_title(sid, "生成")            # 还能写
        self.store.rename_session(sid, "界面又改了")           # 无 source：不动标记
        self.assertEqual(self.store.get_session(sid)["title"], "界面又改了")
        self.assertEqual(self.store.get_session(sid)["title_source"], "auto")

    def test_unknown_session_is_locked(self):
        self.assertTrue(self.store.session_title_is_locked("no-such-session"))


if __name__ == "__main__":
    unittest.main()
