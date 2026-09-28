"""llm.py 单测：流式归一化 + 工具调用累加 + 可注入 opener（零真网络）。"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import llm  # noqa: E402
from llm import ModelConfig, parse_tool_arguments, stream_chat  # noqa: E402


def cfg() -> ModelConfig:
    c = ModelConfig(base_url="https://example.invalid/v1", model="test-model",
                    api_key_env="TEST_KEY", max_tokens=128, timeout_seconds=5)
    c.api_key = "secret-should-never-log"
    return c


class FakeResponse:
    """假装成 urlopen 的返回：支持 with 与逐行迭代。"""

    def __init__(self, lines: list[bytes]):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(self._lines)


def sse(*payloads: dict) -> FakeResponse:
    lines = [("data: " + json.dumps(p, ensure_ascii=False) + "\n").encode("utf-8")
             for p in payloads]
    lines.append(b"data: [DONE]\n")
    return FakeResponse(lines)


class CapturingOpener:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        return self.response

    def payload(self) -> dict:
        return json.loads(self.requests[-1].data.decode("utf-8"))

    def headers(self) -> dict:
        return {k.lower(): v for k, v in self.requests[-1].headers.items()}


class BasicStreamTest(unittest.TestCase):
    def test_delta_reasoning_done(self):
        opener = CapturingOpener(sse(
            {"choices": [{"delta": {"reasoning_content": "想一下"}}]},
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {"content": "世界"}, "finish_reason": "stop"}]},
            {"usage": {"total_tokens": 9, "prompt_tokens": 5, "completion_tokens": 4}},
        ))
        events = list(stream_chat(cfg(), [{"role": "user", "content": "hi"}], opener=opener))
        self.assertEqual([e["type"] for e in events],
                         ["reasoning", "delta", "delta", "done"])
        self.assertEqual(events[0]["text"], "想一下")
        self.assertEqual("".join(e["text"] for e in events if e["type"] == "delta"), "你好世界")
        done = events[-1]
        self.assertEqual(done["finishReason"], "stop")
        self.assertEqual(done["usage"]["total_tokens"], 9)
        self.assertEqual(done["toolCalls"], [])

    def test_payload_shape_and_usage_flag(self):
        opener = CapturingOpener(sse({"choices": [{"delta": {"content": "x"}}]}))
        list(stream_chat(cfg(), [{"role": "user", "content": "hi"}], opener=opener,
                         max_tokens=7))
        p = opener.payload()
        self.assertEqual(p["stream"], True)
        self.assertEqual(p["stream_options"], {"include_usage": True})
        self.assertEqual(p["max_tokens"], 7)
        self.assertEqual(p["model"], "test-model")
        self.assertNotIn("tools", p)                      # 没传就不带
        self.assertEqual(opener.headers()["authorization"], "Bearer secret-should-never-log")

    def test_multimodal_content_parts_pass_through(self):
        """图片走 content 数组原样透传（多模态输入不改协议）。"""
        msgs = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "text", "text": "这是什么"},
        ]}]
        opener = CapturingOpener(sse({"choices": [{"delta": {"content": "ok"}}]}))
        list(stream_chat(cfg(), msgs, opener=opener))
        sent = opener.payload()["messages"][0]["content"]
        self.assertEqual(sent[0]["type"], "image_url")
        self.assertTrue(sent[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(sent[1]["text"], "这是什么")

    def test_cancel_short_circuits(self):
        opener = CapturingOpener(sse(
            {"choices": [{"delta": {"content": "a"}}]},
            {"choices": [{"delta": {"content": "b"}}]},
        ))
        seen = {"n": 0}

        def cancelled():
            seen["n"] += 1
            return seen["n"] > 1        # 第一个 delta 之后开始取消

        events = list(stream_chat(cfg(), [{"role": "user", "content": "hi"}],
                                  is_cancelled=cancelled, opener=opener))
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["finishReason"], "cancelled")


class ToolCallTest(unittest.TestCase):
    def test_tools_injected_into_payload(self):
        tools = [{"type": "function", "function": {"name": "read_file",
                                                   "description": "读文件",
                                                   "parameters": {"type": "object"}}}]
        opener = CapturingOpener(sse({"choices": [{"delta": {"content": "x"}}]}))
        list(stream_chat(cfg(), [{"role": "user", "content": "hi"}], tools=tools,
                         tool_choice="auto", opener=opener))
        p = opener.payload()
        self.assertEqual(p["tools"], tools)
        self.assertEqual(p["tool_choice"], "auto")

    def test_fragmented_tool_call_accumulated_by_index(self):
        """参数跨多个 delta 被切开，必须按 index 拼起来（真实 stream 就这样切）。"""
        opener = CapturingOpener(sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": "{\"ci"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": "ty\":\"深圳\"}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"usage": {"total_tokens": 12, "prompt_tokens": 8, "completion_tokens": 4}},
        ))
        events = list(stream_chat(cfg(), [{"role": "user", "content": "天气"}], opener=opener))
        done = events[-1]
        self.assertEqual(done["finishReason"], "tool_calls")
        self.assertEqual(len(done["toolCalls"]), 1)
        tc = done["toolCalls"][0]
        self.assertEqual(tc["id"], "call_1")
        self.assertEqual(tc["name"], "get_weather")
        self.assertEqual(tc["type"], "function")
        self.assertEqual(json.loads(tc["arguments"]), {"city": "深圳"})

    def test_parallel_tool_calls_kept_separate(self):
        opener = CapturingOpener(sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "a", "function": {"name": "read_file",
                                                     "arguments": "{\"path\":\"1\"}"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 1, "id": "b", "function": {"name": "list_dir",
                                                     "arguments": "{\"path\":\"2\"}"}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ))
        done = list(stream_chat(cfg(), [{"role": "user", "content": "x"}],
                                opener=opener))[-1]
        self.assertEqual([t["id"] for t in done["toolCalls"]], ["a", "b"])
        self.assertEqual([t["name"] for t in done["toolCalls"]], ["read_file", "list_dir"])

    def test_name_fragment_also_concatenated(self):
        opener = CapturingOpener(sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "a", "function": {"name": "read_"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "file", "arguments": "{}"}}]}}]},
        ))
        done = list(stream_chat(cfg(), [{"role": "user", "content": "x"}],
                                opener=opener))[-1]
        self.assertEqual(done["toolCalls"][0]["name"], "read_file")


class ErrorTest(unittest.TestCase):
    def test_http_401_maps_to_auth(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError("https://x", 401, "unauthorized", {}, 
                                         io.BytesIO(b'{"error":"bad key"}'))
        events = list(stream_chat(cfg(), [{"role": "user", "content": "hi"}], opener=opener))
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["code"], "AUTH")

    def test_http_500_maps_to_http_error(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError("https://x", 500, "boom", {},
                                         io.BytesIO(b"server exploded"))
        events = list(stream_chat(cfg(), [{"role": "user", "content": "hi"}], opener=opener))
        self.assertEqual(events[-1]["code"], "HTTP_ERROR")
        self.assertIn("500", events[-1]["message"])

    def test_network_error(self):
        def opener(req, timeout):
            raise OSError("connection reset")
        events = list(stream_chat(cfg(), [{"role": "user", "content": "hi"}], opener=opener))
        self.assertEqual(events[-1]["code"], "NETWORK")

    def test_base_resp_error_surfaces(self):
        opener = CapturingOpener(sse(
            {"choices": [], "base_resp": {"status_code": 1026, "status_msg": "safety"}}))
        events = list(stream_chat(cfg(), [{"role": "user", "content": "hi"}], opener=opener))
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["code"], "PROVIDER_ERROR")
        self.assertIn("1026", events[-1]["message"])

    def test_broken_json_line_is_skipped(self):
        resp = FakeResponse([b"data: not-json\n",
                             b"data: " + json.dumps(
                                 {"choices": [{"delta": {"content": "ok"}}]}).encode() + b"\n",
                             b"data: [DONE]\n"])
        events = list(stream_chat(cfg(), [{"role": "user", "content": "hi"}],
                                  opener=CapturingOpener(resp)))
        self.assertEqual("".join(e.get("text", "") for e in events if e["type"] == "delta"),
                         "ok")


class ParseToolArgumentsTest(unittest.TestCase):
    def test_empty_is_empty_dict(self):
        self.assertEqual(parse_tool_arguments(""), ({}, None))
        self.assertEqual(parse_tool_arguments("   "), ({}, None))
        self.assertEqual(parse_tool_arguments(None), ({}, None))  # type: ignore[arg-type]

    def test_valid(self):
        args, err = parse_tool_arguments('{"path": "a.txt", "n": 2}')
        self.assertIsNone(err)
        self.assertEqual(args, {"path": "a.txt", "n": 2})

    def test_invalid_returns_error_not_raise(self):
        args, err = parse_tool_arguments("{not json")
        self.assertEqual(args, {})
        self.assertIsNotNone(err)
        self.assertIn("JSON", err)

    def test_non_object_rejected(self):
        args, err = parse_tool_arguments("[1,2,3]")
        self.assertEqual(args, {})
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()