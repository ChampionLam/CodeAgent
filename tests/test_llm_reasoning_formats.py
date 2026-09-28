"""思考文本解析：各家 provider 的字段形状都要认（2026-09-27 用户要求）。

背景：llm.py 原来只看 delta["reasoning_content"] 一个字段，于是 OpenRouter
（delta.reasoning / delta.reasoning_details）的思考整段丢失——同一台机器上
MiniMax 的 usage 行有、OpenRouter 一行都没有、思考也一条没落库。

关键约束：OpenRouter 的 reasoning 就是 reasoning_details 拼出来的副本，
两个都取会把思考文本翻倍，所以必须「按优先级取第一个非空来源」。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import llm  # noqa: E402
from llm import ModelConfig, reasoning_text_from, stream_chat  # noqa: E402
from test_llm import CapturingOpener, sse  # noqa: E402


#: 真实抓到的形状（provider -> delta 片段）
SHAPES = [
    # deepseek / qwen / glm / 部分 minimax：裸字符串
    ("reasoning_content 字符串", {"reasoning_content": "想甲"}, "想甲"),
    # OpenRouter：reasoning 是拼接好的字符串
    ("openrouter reasoning 字符串", {"reasoning": "想乙"}, "想乙"),
    # OpenRouter：分块数组
    ("openrouter reasoning_details", {"reasoning_details": [
        {"type": "reasoning.text", "text": "想甲"}, {"type": "reasoning.text", "text": "乙"}]}, "想甲乙"),
    # OpenRouter 同时给两份 —— 只能算一次，否则思考翻倍
    ("openrouter 两份并存不叠加", {"reasoning": "想甲", "reasoning_details": [{"text": "想甲"}]}, "想甲"),
    # 只有摘要（部分模型只回 summary）
    ("reasoning_details 只给 summary", {"reasoning_details": [
        {"type": "reasoning.summary", "summary": "摘要"}]}, "摘要"),
    # Ollama / 部分网关
    ("thinking 字符串", {"thinking": "想丙"}, "想丙"),
    # Anthropic 风格：思考混在 content 分片里，正常回答也在里面
    ("content 分片里的 thinking 块", {"content": [
        {"type": "thinking", "thinking": "想丁"}, {"type": "text", "text": "答案"}]}, "想丁"),
    ("content 分片里的 reasoning.text 块", {"content": [
        {"type": "reasoning.text", "text": "想戊"}, {"type": "text", "text": "答案"}]}, "想戊"),
    # 字典形态（有的网关给 {"type":..., "text":...}）
    ("reasoning 是字典", {"reasoning": {"type": "reasoning.text", "text": "想己"}}, "想己"),
    # 不该被当思考的
    ("纯正文 delta 不算思考", {"content": "答案"}, ""),
    ("空 delta", {}, ""),
    ("None", None, ""),
]


class ReasoningFormatsTest(unittest.TestCase):
    def test_every_shape(self):
        for name, delta, want in SHAPES:
            with self.subTest(shape=name):
                self.assertEqual(reasoning_text_from(delta), want)

    def test_openrouter_double_source_is_not_duplicated(self):
        """回归：两份并存时若叠加，会把思考文本翻倍。"""
        got = reasoning_text_from({"reasoning": "甲乙丙",
                                   "reasoning_details": [{"text": "甲乙丙"}]})
        self.assertEqual(got, "甲乙丙")
        self.assertNotEqual(got, "甲乙丙甲乙丙")


class StreamReasoningTest(unittest.TestCase):
    def _cfg(self, base_url="https://example.invalid/v1"):
        c = ModelConfig(base_url=base_url, model="m", api_key_env="K",
                        max_tokens=64, timeout_seconds=5)
        c.api_key = "k"
        return c

    def test_stream_collects_three_different_shapes_in_one_turn(self):
        opener = CapturingOpener(sse(
            {"choices": [{"delta": {"reasoning_content": "甲"}}]},
            {"choices": [{"delta": {"reasoning": "乙"}}]},
            {"choices": [{"delta": {"reasoning_details": [{"type": "reasoning.text", "text": "丙"}]}}]},
            {"choices": [{"delta": {"content": "正文"}, "finish_reason": "stop"}]},
        ))
        events = list(stream_chat(self._cfg(), [{"role": "user", "content": "hi"}],
                                  opener=opener))
        reason = "".join(e["text"] for e in events if e["type"] == "reasoning")
        text = "".join(e["text"] for e in events if e["type"] == "delta")
        self.assertEqual(reason, "甲乙丙")
        self.assertEqual(text, "正文")
        self.assertEqual(events[-1]["type"], "done")


class OpenRouterUsageTest(unittest.TestCase):
    """OpenRouter 不认 stream_options，必须单独带 usage.include 才回 token 用量。"""

    def _payload_for(self, base_url: str) -> dict:
        c = ModelConfig(base_url=base_url, model="m", api_key_env="K",
                        max_tokens=64, timeout_seconds=5)
        c.api_key = "k"
        opener = CapturingOpener(sse({"choices": [{"delta": {"content": "x"},
                                                   "finish_reason": "stop"}]}))
        list(stream_chat(c, [{"role": "user", "content": "hi"}], opener=opener))
        return opener.payload()

    def test_openrouter_asks_for_usage(self):
        payload = self._payload_for("https://openrouter.ai/api/v1")
        self.assertEqual(payload.get("usage"), {"include": True})
        # 老开关留着没坏处
        self.assertEqual(payload.get("stream_options"), {"include_usage": True})

    def test_other_providers_do_not_get_the_extra_key(self):
        payload = self._payload_for("https://api.minimaxi.com/v1")
        self.assertNotIn("usage", payload)
        self.assertEqual(payload.get("stream_options"), {"include_usage": True})


if __name__ == "__main__":
    unittest.main()