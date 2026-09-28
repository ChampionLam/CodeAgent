"""llm.py 思考参数构建回归：不可关思考的模型绝不能发 reasoning 键。

2026-09-26 真机事故（OpenRouter stealth/space-bunny-alpha，逐项二分定位）：
  * 裸请求（model + messages）        -> 200
  * tools 数组（13 个，含 delegate）   -> 200
  * reasoning: {"enabled": false}     -> **400** "Reasoning is mandatory for
    this endpoint and cannot be disabled."
  * reasoning_effort: "high"          -> 200
所以给「思考开关不适用」的模型发关思考参数就是 400。修复口径：模型不可关思考
（目录 thinking.off 为 null，= modelconfig 的 thinkingCanDisable=False）时，
thinking=false 一个思考键都不发（不发假 enabled:false，也不替用户发 enabled+最低档），
保持厂商默认。可关且用户要关 -> 照旧发关闭参数。

覆盖三层（零网络，stream_chat 走注入的假 opener）：
  1. 纯函数 thinking_params_for / build_generation_params；
  2. 真正拼请求体的 stream_chat payload；
  3. 判断依据来自已有字段：model_thinking_profile()['off'] / decorate_thinking 的
     thinkingCanDisable，不另造口径。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import llm  # noqa: E402
import modelconfig as mc  # noqa: E402
from llm import ModelConfig, stream_chat  # noqa: E402

# 事故模型：OpenRouter stealth/space-bunny-alpha，目录 thinking.off=null（不可关）。
UNDISABLE_MODEL = "stealth/space-bunny-alpha"
UNDISABLE_PROVIDER = "openrouter"
# 对照模型：目录 off 非 null（可关，发了就是厂商语义）。
DISABLEABLE_MODEL = "MiniMax-M3"
DISABLEABLE_PROVIDER = "minimax-cn"

# 一个 reasoning 键都不许出现的全集（契约 §6 + 目录 depth.param 认得的参数名）。
REASONING_KEYS = {"reasoning", "reasoning_effort", "thinking", "enable_thinking",
                  "thinking_budget", "max_thinking_tokens"}


class FakeResponse:
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


def run_chat(**cfg_kw) -> dict:
    """Drive stream_chat once with a fake opener; return the sent payload."""
    kw = dict(base_url="https://openrouter.ai/api/v1", model=UNDISABLE_MODEL,
              api_key_env="X", provider=UNDISABLE_PROVIDER, max_tokens=128,
              timeout_seconds=5)
    kw.update(cfg_kw)
    c = ModelConfig(**kw)
    c.api_key = "test-key"
    opener = CapturingOpener(sse({"choices": [{"delta": {"content": "x"}}]}))
    list(stream_chat(c, [{"role": "user", "content": "hi"}], opener=opener))
    return opener.payload()


class UndisableModelSendsNoReasoningKeyTest(unittest.TestCase):
    """验收 1：不可关思考的模型 + thinking=null / thinking=false -> 零 reasoning 键。"""

    def test_profile_says_off_is_none(self):
        # 前提：事故模型的目录条目 off=null，modelconfig 由此得 thinkingCanDisable=False。
        prof = llm.model_thinking_profile(UNDISABLE_PROVIDER, UNDISABLE_MODEL)
        self.assertIsNone(prof["off"])
        d = mc.decorate_thinking({"provider": UNDISABLE_PROVIDER,
                                  "model": UNDISABLE_MODEL})
        self.assertFalse(d["thinkingCanDisable"])

    def test_thinking_null_sends_no_reasoning_key(self):
        got = llm.build_generation_params(
            UNDISABLE_PROVIDER, None, None, None, None, True, UNDISABLE_MODEL)
        self.assertEqual(got, {})
        for k in REASONING_KEYS:
            self.assertNotIn(k, got, k)

    def test_thinking_false_sends_no_reasoning_key(self):
        """核心修复：用户要关，但模型不可关 -> 完全不发（旧代码发 reasoning_effort=minimal）。"""
        got = llm.build_generation_params(
            UNDISABLE_PROVIDER, False, None, None, None, True, UNDISABLE_MODEL)
        self.assertEqual(got, {})
        for k in REASONING_KEYS:
            self.assertNotIn(k, got, k)

    def test_thinking_false_with_depth_sends_no_reasoning_key(self):
        # 连深度一起给也一样：关不了就不发任何思考键（深度挂在开关的 else 分支）。
        got = llm.build_generation_params(
            UNDISABLE_PROVIDER, False, None, None, "high", True, UNDISABLE_MODEL)
        self.assertEqual(got, {})

    def test_stream_chat_payload_null_thinking_has_no_reasoning_key(self):
        """验收 1（请求体层）：thinking=null 走 stream_chat，payload 不含 reasoning 键。"""
        p = run_chat(thinking=None)
        self.assertEqual(p["model"], UNDISABLE_MODEL)
        for k in REASONING_KEYS:
            self.assertNotIn(k, p, k)
        # 其它核心字段不受影响。
        self.assertEqual(p["stream"], True)
        self.assertEqual(p["messages"], [{"role": "user", "content": "hi"}])

    def test_stream_chat_payload_false_thinking_has_no_reasoning_key(self):
        """验收 1（请求体层）：thinking=false 走 stream_chat，payload 不含 reasoning 键。"""
        p = run_chat(thinking=False)
        for k in REASONING_KEYS:
            self.assertNotIn(k, p, k)

    def test_log_line_says_undisable(self):
        """不发参数但仍打一行日志，用户能知道「关」没被照办。"""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            llm.thinking_params_for(UNDISABLE_PROVIDER, False, None, UNDISABLE_MODEL)
        self.assertIn("can not disable", buf.getvalue())
        self.assertIn("no thinking parameter", buf.getvalue())


class DisableableModelStillSendsOffParams(unittest.TestCase):
    """验收 2：可关思考的模型 + 用户要关 -> 照旧发关闭参数（原行为不变）。"""

    def test_minimax_off_still_sends_disabled(self):
        got = llm.thinking_params_for(DISABLEABLE_PROVIDER, False, None,
                                      DISABLEABLE_MODEL)
        self.assertEqual(got, {"thinking": {"type": "disabled"}})

    def test_provider_map_off_still_sends(self):
        # 没进目录的模型退回 §6 provider 表，off 照发。
        got = llm.thinking_params_for("deepseek", False, None, "没进目录的模型")
        self.assertEqual(got, {"thinking": {"type": "disabled"}})

    def test_catalog_off_fragment_sent_verbatim(self):
        # gpt-6-sol 目录 off = {"reasoning": {"effort": "none"}}，逐字照发。
        got = llm.thinking_params_for("openai", False, None, "gpt-6-sol")
        self.assertEqual(got, {"reasoning": {"effort": "none"}})

    def test_stream_chat_off_still_in_payload(self):
        p = run_chat(provider=DISABLEABLE_PROVIDER, model=DISABLEABLE_MODEL,
                     thinking=False)
        self.assertEqual(p["thinking"], {"type": "disabled"})


class UndisableModelStillHonorsExplicitOnAndDepth(unittest.TestCase):
    """不可关 ≠ 不能调：明确要开 / 要档位时照发（别把修复扩大化）。"""

    def test_thinking_true_still_sends_on_fragment(self):
        got = llm.thinking_params_for(UNDISABLE_PROVIDER, True, None,
                                      UNDISABLE_MODEL)
        self.assertEqual(got, {"reasoning_effort": "high"})

    def test_depth_still_sent_when_thinking_on(self):
        got = llm.thinking_params_for(UNDISABLE_PROVIDER, True, "max",
                                      UNDISABLE_MODEL)
        self.assertEqual(got, {"reasoning_effort": "max"})

    def test_depth_only_still_sent(self):
        # thinking=None + depth=high：档位是合法干预，照发（实测 200）。
        got = llm.thinking_params_for(UNDISABLE_PROVIDER, None, "high",
                                      UNDISABLE_MODEL)
        self.assertEqual(got, {"reasoning_effort": "high"})

    def test_stream_chat_on_and_depth_in_payload(self):
        p = run_chat(thinking=True, thinking_depth="max")
        self.assertEqual(p["reasoning_effort"], "max")


if __name__ == "__main__":
    unittest.main()
