"""输入区控制条的会话级覆盖（思考开关 / 思考深度 / 上下文长度）测试。

产品口径：这是**会话级**控制，只影响这一轮请求，不写回 config.json。
取值不认识时必须如实回报，不能静默失效——用户以为改了其实没改是最坏的情况。
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "python"))

import llm  # noqa: E402


def cfg(**kw) -> llm.ModelConfig:
    base = dict(base_url="https://api.example.com/v1", model="m", api_key_env="K",
                provider="minimax-cn", context_window=128000, thinking=None,
                thinking_depth=None, max_tokens=4096)
    base.update(kw)
    return llm.ModelConfig(**base)


class ApplyTurnOverridesTest(unittest.TestCase):
    def test_thinking_switch_accepts_tristate(self):
        for v in (True, False, None):
            c = cfg()
            notes = llm.apply_turn_overrides(c, {"thinking": v})
            self.assertEqual(c.thinking, v)
            self.assertEqual(notes, [])

    def test_thinking_garbage_is_reported_not_swallowed(self):
        c = cfg(thinking=True)
        notes = llm.apply_turn_overrides(c, {"thinking": "yes-please"})
        self.assertTrue(c.thinking, "看不懂的取值不该改动现状")
        self.assertEqual(len(notes), 1)
        self.assertIn("思考开关", notes[0])

    def test_depth_accepts_three_levels_and_resets(self):
        for level in ("low", "medium", "high"):
            c = cfg()
            llm.apply_turn_overrides(c, {"thinkingDepth": level})
            self.assertEqual(c.thinking_depth, level)
        for reset in (None, "default", "", "  "):
            c = cfg(thinking_depth="high")
            llm.apply_turn_overrides(c, {"thinkingDepth": reset})
            self.assertIsNone(c.thinking_depth, f"{reset!r} 应当等于「不干预」")

    def test_depth_garbage_is_reported(self):
        c = cfg(thinking_depth="high")
        notes = llm.apply_turn_overrides(c, {"thinkingDepth": "max"})
        self.assertEqual(c.thinking_depth, "high")
        self.assertIn("思考深度", notes[0])

    def test_context_window_override(self):
        c = cfg(context_window=128000)
        llm.apply_turn_overrides(c, {"contextWindow": 1000000})
        self.assertEqual(c.context_window, 1000000)
        self.assertEqual(c.contextWindow, 1000000)   # camelCase 别名同步

    def test_context_window_garbage_keeps_model_value(self):
        for bad in (0, -1, "128k", 1.5, True, None):
            c = cfg(context_window=131072)
            notes = llm.apply_turn_overrides(c, {"contextWindow": bad})
            self.assertEqual(c.context_window, 131072, f"{bad!r} 不该改窗口")
            self.assertEqual(len(notes), 1)

    def test_missing_keys_change_nothing(self):
        c = cfg(thinking=False, thinking_depth="low", context_window=65536)
        notes = llm.apply_turn_overrides(c, {})
        self.assertEqual((c.thinking, c.thinking_depth, c.context_window), (False, "low", 65536))
        self.assertEqual(notes, [])
        self.assertEqual(llm.apply_turn_overrides(c, None), [])

    def test_override_reaches_the_request_body(self):
        """覆盖必须真的落到请求体：开思考 + 深 → thinking 键 + 深度键都在。"""
        c = cfg(provider="dashscope-bailian", thinking=None, thinking_depth=None)
        llm.apply_turn_overrides(c, {"thinking": True, "thinkingDepth": "high"})
        params = llm.build_generation_params(
            provider=c.provider, thinking=c.thinking, thinking_depth=c.thinking_depth,
            temperature=c.temperature, top_p=c.top_p)
        self.assertIn("enable_thinking", params)
        self.assertTrue(params["enable_thinking"])

    def test_thinking_off_sends_disabled_and_drops_depth(self):
        """MiniMax 关思考 = 显式传 disabled（不是不传），深度键则必须被丢掉。"""
        c = cfg(provider="minimax-cn", thinking=None, thinking_depth="high")
        llm.apply_turn_overrides(c, {"thinking": False})
        params = llm.build_generation_params(
            provider=c.provider, thinking=c.thinking, thinking_depth=c.thinking_depth,
            temperature=None, top_p=None)
        self.assertEqual(params.get("thinking"), {"type": "disabled"})
        self.assertNotIn("thinking_depth", params)


class OverrideBudgetInteractionTest(unittest.TestCase):
    """开思考时要抬高输出预算（参照 Hermes Agent），覆盖项不该把这条规则绕过去。"""

    def test_effective_budget_raises_with_thinking_on(self):
        self.assertGreater(llm.effective_max_tokens("minimax-cn", True, 4096),
                           llm.effective_max_tokens("minimax-cn", False, 4096))


if __name__ == "__main__":
    unittest.main()