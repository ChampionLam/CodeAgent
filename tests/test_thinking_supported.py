"""思考参数的「模型级能力」契约测试（catalog 优先，provider 表兜底）。

2026-09-25 用户纠正「怎么没有思考强度设置,肯定有啊,你的检索有问题」后重写：

* 事实（目录里带官方原文取证）：GLM-5.3 **始终思考、不可关**，但**有三档强度**
  `reasoning_effort: low/high/max`（docs.bigmodel.cn：「支持三个思考强度级别：
  low、high 和 max，并不再支持禁用思考功能」）；
* 实测（百炼 MAAS 兼容口，2026-09-25）：`enable_thinking=false` → 400
  「restricted to True」；`reasoning_effort` 非 low/high/max → 400 列出合法集；
* 所以「有没有旋钮」是**模型级**事实，不能按 provider 猜——provider 表里
  dashscope-bailian 只有 enable_thinking 布尔，照它走就发不出 reasoning_effort，
  还会把 enable_thinking=False 发出去撞 400。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import llm  # noqa: E402
import modelconfig as mc  # noqa: E402

BAILIAN_MAAS_GLM = {"provider": "dashscope-bailian", "model": "glm-5.3"}


class TestCatalogThinkingProfile(unittest.TestCase):
    """model_thinking_profile：目录优先，逐模型取证。"""

    def test_glm53_profile_comes_from_catalog(self):
        p = llm.model_thinking_profile("dashscope-bailian", "glm-5.3")
        self.assertEqual(p["source"], "catalog")
        self.assertEqual(p["levels"], ["low", "high", "max"])
        self.assertEqual(p["depth_param"], "reasoning_effort")
        # 厂商不允许关思考：off 必须是 None（不能发假值，发了 400）
        self.assertIsNone(p["off"])
        self.assertEqual(p["on"]["thinking"], {"type": "enabled"})

    def test_provider_map_is_the_fallback(self):
        p = llm.model_thinking_profile("minimax-cn", "不存在的模型")
        self.assertEqual(p["source"], "provider-map")
        self.assertEqual(p["on"], {"thinking": {"type": "adaptive"}})
        self.assertEqual(p["off"], {"thinking": {"type": "disabled"}})


class TestThinkingParamsForRequest(unittest.TestCase):
    """发出去的请求体片段：档位真发、不许发的绝不发。"""

    def test_depth_max_is_sent_as_reasoning_effort(self):
        got = llm.thinking_params_for("dashscope-bailian", True, "max", "glm-5.3")
        self.assertEqual(got, {"thinking": {"type": "enabled"}, "reasoning_effort": "max"})

    def test_no_enable_thinking_key_ever_on_this_model(self):
        for thinking in (True, False, None):
            got = llm.thinking_params_for("dashscope-bailian", thinking, "low", "glm-5.3")
            self.assertNotIn("enable_thinking", got, msg=f"thinking={thinking}")

    def test_requesting_off_on_undisable_model_sends_nothing(self):
        # 2026-09-26 改口径：glm-5.3 不可关思考（目录 off=null），用户要关时
        # 一个思考键都不发（旧逻辑替用户发 enabled+最低档；OpenRouter stealth
        # space-bunny 实测关思考参数本身就 400，宁可少传不可错传）。
        got = llm.thinking_params_for("dashscope-bailian", False, None, "glm-5.3")
        self.assertEqual(got, {})

    def test_nothing_sent_when_not_intervening(self):
        self.assertEqual(llm.thinking_params_for("dashscope-bailian", None, None, "glm-5.3"), {})

    def test_minimax_still_uses_the_section6_switch(self):
        self.assertEqual(llm.thinking_params_for("minimax-cn", True, None, "MiniMax-M3"),
                         {"thinking": {"type": "adaptive"}})
        self.assertEqual(llm.thinking_params_for("minimax-cn", False, None, "MiniMax-M3"),
                         {"thinking": {"type": "disabled"}})
        self.assertEqual(llm.thinking_params_for("minimax-cn", True, "high", "MiniMax-M3"),
                         {"thinking": {"type": "adaptive"}})

    def test_build_generation_params_passes_model_through(self):
        got = llm.build_generation_params("dashscope-bailian", True, 0.5, None, "high", True, "glm-5.3")
        self.assertEqual(got["reasoning_effort"], "high")
        self.assertEqual(got["temperature"], 0.5)
        self.assertNotIn("enable_thinking", got)


class TestTurnDepthValidation(unittest.TestCase):
    """档位校验按这条模型真实档位来（原来写死 low/medium/high，max 会被丢掉）。"""

    def _cfg(self, **kw):
        base = dict(base_url="https://example.invalid/v1", model="glm-5.3",
                    api_key_env="X", provider="dashscope-bailian")
        base.update(kw)
        return llm.ModelConfig(**base)

    def test_max_is_accepted_for_glm53(self):
        cfg = self._cfg()
        notes = llm.apply_turn_overrides(cfg, {"thinking": True, "thinkingDepth": "max"})
        self.assertEqual(cfg.thinking_depth, "max")
        self.assertEqual(notes, [])

    def test_garbage_depth_is_still_rejected_with_a_note(self):
        cfg = self._cfg()
        notes = llm.apply_turn_overrides(cfg, {"thinkingDepth": "疯狂"})
        self.assertIsNone(cfg.thinking_depth)
        self.assertTrue(notes)

    def test_low_medium_high_upstream_default_still_works(self):
        cfg = self._cfg(model="某个没进目录的模型", provider="openai")
        notes = llm.apply_turn_overrides(cfg, {"thinkingDepth": "medium"})
        self.assertEqual(cfg.thinking_depth, "medium")
        self.assertEqual(notes, [])


class TestDecorateThinkingForUi(unittest.TestCase):
    """界面拿到的字段：档位列表、能不能关、要不要画那一行。"""

    def test_glm53_row_is_a_real_depth_row(self):
        d = mc.decorate_thinking(dict(BAILIAN_MAAS_GLM))
        self.assertEqual(d["thinkingDepthLevels"], ["low", "high", "max"])
        self.assertTrue(d["thinkingAdapted"], "有 reasoning_effort 档位，界面必须给可点行")
        self.assertFalse(d["thinkingCanDisable"], "厂商不允许关思考，不给「不思考」")
        self.assertFalse(d["softThinkingDepth"])

    def test_minimax_has_switch_but_no_depth(self):
        d = mc.decorate_thinking({"provider": "minimax-cn", "model": "MiniMax-M3"})
        self.assertEqual(d["thinkingDepthLevels"], [])
        self.assertTrue(d["thinkingAdapted"])
        self.assertTrue(d["thinkingCanDisable"])
        self.assertTrue(d["softThinkingDepth"])

    def test_line_with_nothing_is_marked_unadapted(self):
        d = mc.decorate_thinking({"provider": "custom", "model": "whatever"})
        self.assertFalse(d["thinkingAdapted"])

    def test_entry_flag_false_forces_unadapted(self):
        d = mc.decorate_thinking({"provider": "dashscope-bailian", "model": "glm-5.3",
                                  "thinkingSupported": False})
        self.assertFalse(d["thinkingAdapted"])


if __name__ == "__main__":
    unittest.main()