"""续跑重放：会话库的「界面形态」行必须转成厂商 API 形态。

2026-09-27 用户报障：点「上次任务未完成」的「继续」→ HTTP 400
`tool messages must include a non-empty string tool_call_id`（错误点名 messages[4]）。

根因：`_apply_resume` 原来把会话库的行（驼峰 `toolCallId` / `toolCalls`，给渲染层用的）
直接丢给厂商；厂商按 API 形态找 `tool_call_id`，找不到就是 400。

真机实证（会话 s-1790447666923）：老路径 payload[4] 的键是
`role,toolCallId`（无 tool_call_id）→ 与报错索引一致；新路径 tool 行 9 条
全部带 id，missing=0、orphan=0。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sidecar  # noqa: E402


def ui_row(role, content="", tcid="", calls=None):
    """一条会话库的行（界面形态）。"""
    row = {"role": role, "content": content}
    if tcid:
        row["toolCallId"] = tcid
    if calls:
        row["toolCalls"] = calls
    row["reasoning"] = "思考"
    return row


CALL = {"id": "call-1", "type": "function",
        "function": {"name": "write_file", "arguments": "{}"}}


class ApiMessagesFromRowsTest(unittest.TestCase):
    def test_tool_call_id_is_snake_case_and_matches_declaration(self):
        rows = [
            ui_row("user", "写个文件"),
            ui_row("assistant", "", calls=[CALL]),
            ui_row("tool", "Wrote 6 bytes", tcid="call-1"),
        ]
        out = sidecar._api_messages_from_rows(rows)
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "tool"])
        # 这是那条 400 的直接断言：tool 行必须有非空 tool_call_id
        self.assertEqual(out[2]["tool_call_id"], "call-1")
        self.assertNotIn("toolCallId", out[2])
        self.assertNotIn("reasoning", out[2])
        # assistant 的 tool_calls 原样透传（库里存的就是厂商格式）
        self.assertEqual(out[1]["tool_calls"], [CALL])
        self.assertEqual(out[1]["tool_calls"][0]["function"]["name"], "write_file")

    def test_orphan_tool_row_is_dropped(self):
        """没有对应 assistant tool_call 的 tool 行发出去同样 400，直接丢。"""
        rows = [
            ui_row("user", "hi"),
            ui_row("tool", "残留结果", tcid="call-gone"),
        ]
        out = sidecar._api_messages_from_rows(rows)
        self.assertEqual([m["role"] for m in out], ["user"])

    def test_tool_row_without_id_is_dropped(self):
        rows = [ui_row("user", "hi"), ui_row("tool", "没有 id")]
        out = sidecar._api_messages_from_rows(rows)
        self.assertNotIn("tool", [m["role"] for m in out])

    def test_extra_ui_fields_are_not_leaked(self):
        rows = [ui_row("user", "hi")]
        rows[0].update({"ordinal": 7, "active": 1, "artifacts": None,
                        "created_at": "2026-09-27"})
        out = sidecar._api_messages_from_rows(rows)
        self.assertEqual(sorted(out[0].keys()), ["content", "role"])

    def test_two_round_tool_turns_keep_ids_paired(self):
        call2 = {"id": "call-2", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}}
        rows = [
            ui_row("user", "u"),
            ui_row("assistant", "", calls=[CALL]),
            ui_row("tool", "r1", tcid="call-1"),
            ui_row("assistant", "", calls=[call2]),
            ui_row("tool", "r2", tcid="call-2"),
            ui_row("assistant", "done"),
        ]
        out = sidecar._api_messages_from_rows(rows)
        tools = [m for m in out if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools], ["call-1", "call-2"])
        for m in tools:
            self.assertTrue(m["tool_call_id"])

    def test_empty_and_none_rows(self):
        self.assertEqual(sidecar._api_messages_from_rows([]), [])
        self.assertEqual(sidecar._api_messages_from_rows(None), [])


if __name__ == "__main__":
    unittest.main()