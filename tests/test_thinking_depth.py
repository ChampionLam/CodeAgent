"""思考（thinking）开关 + 深度档位测试（契约 §6 的「深度」列）。

覆盖四层：
  1. provider 解析：config 里 provider 是 "legacy"/空 时，按 baseUrl 主机名
     兜底解析成 §6 的 provider key（否则思考开关会静默失效）；
  2. llm 参数映射：有厂商级深度参数的 provider 才出深度键，其余一个键都不传；
  3. modelconfig 的字段落盘：thinkingDepth 校验 / 保留 / 显式 null 复位；
  4. prompt_build 的软档位：厂商没有深度参数时，深度只能靠一行提示词引导，
     而且只有软档位标记为真时才注入。

零网络：全部走纯函数 + 临时目录里的 models.* RPC。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

import llm  # noqa: E402
import modelconfig as MC  # noqa: E402
import prompt_build  # noqa: E402
import sidecar  # noqa: E402


CONFIG = {
    "models": {
        "defaultId": "legacy",
        "items": [
            {
                "id": "legacy",
                "label": "MiniMax-M3",
                "provider": "legacy",
                "baseUrl": "https://api.minimaxi.com/v1",
                "model": "MiniMax-M3",
                "apiKeyEnv": "MINIMAX_CN_API_KEY",
                "maxTokens": 4096,
                "timeoutSeconds": 180,
                "inputModalities": ["text", "image"],
            },
            {
                "id": "gpt",
                "label": "GPT",
                "provider": "openai",
                "baseUrl": "https://api.openai.com/v1",
                "model": "gpt-5",
                "apiKeyEnv": "OPENAI_API_KEY",
                "maxTokens": 8192,
                "timeoutSeconds": 120,
                "inputModalities": ["text"],
            },
        ],
    },
    "capabilities": {"workspaceRoot": "", "models": {}},
}


class ThinkingDepthCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="desk-agent-depth-")
        self._saved_env = dict(os.environ)
        self._saved_override = MC._MODELS_ROOT_OVERRIDE
        MC._MODELS_ROOT_OVERRIDE = self.root
        self.write_config(json.loads(json.dumps(CONFIG)))
        sidecar._CFG = None

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)
        MC._MODELS_ROOT_OVERRIDE = self._saved_override
        sidecar._CFG = None

    # -- helpers -----------------------------------------------------------
    def write_config(self, cfg: dict) -> None:
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def read_config(self) -> dict:
        with open(os.path.join(self.root, "config.json"), encoding="utf-8") as f:
            return json.load(f)

    def rpc(self, method: str, params: dict | None = None):
        class _Reply(dict):
            @property
            def result(self):
                return self.get("result")

            @property
            def error(self):
                return self.get("error")

        return _Reply(sidecar.handle_request({
            "type": "req", "id": "t1", "method": method,
            "params": {"protocolVersion": 1, **(params or {})},
        }))


class ProviderResolutionTest(unittest.TestCase):
    """provider 解析：占位值（legacy/空）必须按主机名兜底。"""

    def test_placeholder_provider_resolved_from_host(self):
        self.assertEqual(
            MC.resolve_provider_key("legacy", "https://api.minimaxi.com/v1"),
            "minimax-cn")

    def test_empty_provider_resolved_from_host(self):
        self.assertEqual(
            MC.resolve_provider_key("", "https://api.deepseek.com/v1"),
            "deepseek")

    def test_real_provider_wins_over_host(self):
        # A real key must not be second-guessed by the endpoint.
        self.assertEqual(
            MC.resolve_provider_key("zhipu", "https://api.minimaxi.com/v1"),
            "zhipu")

    def test_unknown_host_keeps_raw_value(self):
        self.assertEqual(
            MC.resolve_provider_key("legacy", "https://llm.internal/v1"),
            "legacy")

    def test_host_with_port_and_case(self):
        self.assertEqual(
            MC.resolve_provider_key(None, "http://LocalHost:11434/v1"),
            "ollama-local")

    def test_hint_hosts_are_real_preset_keys(self):
        keys = {p["key"] for p in MC.PRESETS}
        for host, key in MC.PROVIDER_HOST_HINTS.items():
            self.assertIn(key, keys, host)


class DepthParamTest(unittest.TestCase):
    """llm 侧的深度映射：有参数才传，没有就一个键都不出现。"""

    def test_openai_maps_all_levels(self):
        for level in ("low", "medium", "high"):
            self.assertEqual(
                llm.thinking_depth_params("openai", level),
                {"reasoning_effort": level})

    def test_vendor_without_depth_param_sends_nothing(self):
        # MiniMax 官方文档只给 adaptive/disabled，没有深度参数。
        for provider in ("minimax-cn", "minimax-intl", "deepseek", "zhipu",
                         "moonshot", "dashscope-bailian", "siliconflow"):
            self.assertEqual(llm.thinking_depth_params(provider, "high"), {},
                             provider)

    def test_unknown_level_and_provider_are_silent(self):
        self.assertEqual(llm.thinking_depth_params("openai", "ultra"), {})
        self.assertEqual(llm.thinking_depth_params("nope", "high"), {})
        self.assertEqual(llm.thinking_depth_params("openai", None), {})
        self.assertEqual(llm.thinking_depth_params("openai", ""), {})

    def test_depth_is_dropped_when_thinking_is_off(self):
        # 关掉思考时深度没有意义，也不能偷偷传。
        self.assertEqual(
            llm.thinking_depth_params("openai", "high", thinking=False), {})

    def test_depth_kept_when_thinking_unset_or_on(self):
        self.assertEqual(llm.thinking_depth_params("openai", "high", thinking=None),
                         {"reasoning_effort": "high"})
        self.assertEqual(llm.thinking_depth_params("openai", "high", thinking=True),
                         {"reasoning_effort": "high"})

    def test_build_generation_params_combines_fields(self):
        params = llm.build_generation_params("openai", True, 0.3, 0.9, "low")
        self.assertEqual(params["temperature"], 0.3)
        self.assertEqual(params["top_p"], 0.9)
        self.assertEqual(params["reasoning_effort"], "low")
        # OpenAI 的开关是 reasoning_effort=none，因此显式开时不该同时出现 none。
        self.assertNotEqual(params.get("reasoning_effort"), "none")

    def test_build_generation_params_without_depth_is_unchanged(self):
        params = llm.build_generation_params("minimax-cn", True, None, None, "high")
        self.assertEqual(params, {"thinking": {"type": "adaptive"}})

    def test_capability_helpers(self):
        self.assertTrue(llm.is_thinking_depth_adapted("openai"))
        self.assertFalse(llm.is_thinking_depth_adapted("minimax-cn"))
        self.assertEqual(llm.thinking_depth_levels("openai"),
                         ["low", "medium", "high"])
        self.assertEqual(llm.thinking_depth_levels("minimax-cn"), [])


class ModelConfigDepthTest(ThinkingDepthCase):
    """models.* 里的 thinkingDepth：校验、保留、复位、provider 自愈。"""

    def test_read_resolves_legacy_provider(self):
        res = self.rpc("models.list")
        self.assertTrue(res["ok"], res)
        item = next(i for i in res["result"]["items"] if i["id"] == "legacy")
        self.assertEqual(item["provider"], "minimax-cn")
        self.assertIsNone(item["thinkingDepth"])

    def test_create_with_depth_persists(self):
        res = self.rpc("models.upsert", {
            "id": "gpt2", "label": "GPT-2", "provider": "openai",
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-5-mini",
            "apiKeyEnv": "OPENAI_API_KEY", "thinking": True,
            "thinkingDepth": "medium"})
        self.assertTrue(res["ok"], res)
        item = next(i for i in self.read_config()["models"]["items"]
                    if i["id"] == "gpt2")
        self.assertEqual(item["thinkingDepth"], "medium")

    def test_bad_depth_rejected(self):
        res = self.rpc("models.upsert", {
            "id": "gpt3", "label": "GPT-3", "provider": "openai",
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-5-mini",
            "apiKeyEnv": "OPENAI_API_KEY", "thinkingDepth": "ultra"})
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["error"]["code"], MC.MODEL_INVALID)

    def test_non_string_depth_rejected(self):
        res = self.rpc("models.upsert", {
            "id": "gpt4", "label": "GPT-4", "provider": "openai",
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-5-mini",
            "apiKeyEnv": "OPENAI_API_KEY", "thinkingDepth": 3})
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["error"]["code"], MC.MODEL_INVALID)

    def test_update_keeps_depth_when_omitted(self):
        self.rpc("models.upsert", {
            "id": "gpt", "label": "GPT", "provider": "openai",
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-5",
            "apiKeyEnv": "OPENAI_API_KEY", "thinkingDepth": "high"})
        self.rpc("models.upsert", {
            "id": "gpt", "label": "GPT renamed", "provider": "openai",
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-5",
            "apiKeyEnv": "OPENAI_API_KEY"})
        item = next(i for i in self.read_config()["models"]["items"]
                    if i["id"] == "gpt")
        self.assertEqual(item["thinkingDepth"], "high")

    def test_explicit_null_resets_depth(self):
        self.rpc("models.upsert", {
            "id": "gpt", "label": "GPT", "provider": "openai",
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-5",
            "apiKeyEnv": "OPENAI_API_KEY", "thinkingDepth": "high"})
        self.rpc("models.upsert", {
            "id": "gpt", "label": "GPT", "provider": "openai",
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-5",
            "apiKeyEnv": "OPENAI_API_KEY", "thinkingDepth": None})
        item = next(i for i in self.read_config()["models"]["items"]
                    if i["id"] == "gpt")
        self.assertIsNone(item["thinkingDepth"])

    def test_update_without_provider_keeps_stored_one(self):
        self.rpc("models.upsert", {
            "id": "gpt", "label": "GPT renamed", "provider": None,
            "baseUrl": "https://api.openai.com/v1", "model": "gpt-5",
            "apiKeyEnv": "OPENAI_API_KEY"})
        item = next(i for i in self.read_config()["models"]["items"]
                    if i["id"] == "gpt")
        self.assertEqual(item["provider"], "openai")

    def test_legacy_entry_provider_healed_on_save(self):
        self.rpc("models.upsert", {
            "id": "legacy", "label": "MiniMax-M3",
            "baseUrl": "https://api.minimaxi.com/v1", "model": "MiniMax-M3",
            "apiKeyEnv": "MINIMAX_CN_API_KEY", "thinkingDepth": "high"})
        item = next(i for i in self.read_config()["models"]["items"]
                    if i["id"] == "legacy")
        self.assertEqual(item["provider"], "minimax-cn")

    def test_from_resolved_carries_depth(self):
        os.environ["OPENAI_API_KEY"] = "test-key-not-a-real-secret"
        info = MC.resolve_model("gpt", root=self.root)
        info["thinkingDepth"] = "low"
        cfg = llm.ModelConfig.from_resolved(info)
        self.assertEqual(cfg.thinking_depth, "low")
        self.assertEqual(llm.build_generation_params(
            cfg.provider, cfg.thinking, cfg.temperature, cfg.top_p,
            cfg.thinking_depth), {"reasoning_effort": "low"})

    def test_presets_expose_depth_capability(self):
        res = self.rpc("models.presets")
        by_key = {p["key"]: p for p in res["result"]["presets"]}
        self.assertEqual(by_key["openai"]["thinkingDepthLevels"],
                         ["low", "medium", "high"])
        self.assertTrue(by_key["minimax-cn"]["softThinkingDepth"])
        self.assertEqual(by_key["minimax-cn"]["thinkingDepthLevels"], [])

    def test_models_list_exposes_depth_capability(self):
        """已配置的模型也必须带这两个字段。

        界面就是靠 models.list 里这条判断「给硬档位还是软引导」的；漏了字段
        softThinkingDepth 恒为 undefined，界面就会一直当厂商有硬参数——等于骗用户。
        """
        items = self.rpc("models.list").result["items"]
        by_provider = {it.get("provider"): it for it in items}
        self.assertEqual(by_provider["openai"]["thinkingDepthLevels"],
                         ["low", "medium", "high"])
        self.assertFalse(by_provider["openai"]["softThinkingDepth"])
        self.assertEqual(by_provider["minimax-cn"]["thinkingDepthLevels"], [])
        self.assertTrue(by_provider["minimax-cn"]["softThinkingDepth"])


class SoftDepthPromptTest(unittest.TestCase):
    """软档位只在这三种条件同时成立时注入提示词。"""

    def _build(self, **kw):
        return prompt_build.build(workspace="/tmp/ws", **kw)

    def test_soft_hint_rendered_with_chinese_line(self):
        text = self._build(thinking_depth="high", thinking_depth_is_soft=True)
        self.assertIn(prompt_build.THINKING_DEPTH_TITLE, text)

    def test_hard_depth_does_not_touch_prompt(self):
        text = self._build(thinking_depth="high", thinking_depth_is_soft=False)
        self.assertNotIn(prompt_build.THINKING_DEPTH_TITLE, text)

    def test_no_depth_no_section(self):
        text = self._build(thinking_depth=None, thinking_depth_is_soft=True)
        self.assertNotIn(prompt_build.THINKING_DEPTH_TITLE, text)
        self.assertEqual(prompt_build.thinking_depth_section("nope"), "")

    def test_soft_depth_comes_from_loop_params(self):
        text = prompt_build.build_for_loop(
            {"workspaceRoot": "/tmp/ws", "thinkingDepth": "low",
             "thinkingDepthIsSoft": True})
        self.assertIn(prompt_build.THINKING_DEPTH_HINTS["low"], text)

    def test_hint_texts_are_distinct(self):
        hints = prompt_build.THINKING_DEPTH_HINTS
        self.assertEqual(set(hints), {"low", "medium", "high"})
        self.assertEqual(len(set(hints.values())), 3)


class EffectiveMaxTokensTest(unittest.TestCase):
    """开思考时抬高输出预算（参照 Hermes Agent：全局 4096 够正文，思考会一次耗尽）。

    纪律：①只在**实测过共享预算**的厂商上抬（没实测的不动，不编）；
    ②配置本身不改，只改这一次请求实际发的值；③抬了要能解释为什么。
    """

    def test_raises_when_thinking_is_on(self):
        self.assertEqual(llm.effective_max_tokens("minimax-cn", None, 4096),
                         llm.THINKING_MAX_TOKENS_FLOOR)
        self.assertEqual(
            llm.effective_max_tokens("minimax-cn", True, 4096),
            llm.THINKING_MAX_TOKENS_FLOOR)

    def test_user_value_stands_when_thinking_off(self):
        # 实测：思考关掉时 4096 完全够（正文 1213 字，finish=stop），不干预
        self.assertEqual(llm.effective_max_tokens("minimax-cn", False, 4096), 4096)

    def test_roomy_budget_is_left_alone(self):
        self.assertEqual(llm.effective_max_tokens("minimax-cn", None, 32768), 32768)

    def test_unmeasured_providers_untouched(self):
        for prov in ("openai", "dashscope-bailian", "deepseek", "moonshot", "zhipu",
                     "siliconflow", "ollama-local", "custom", "", None):
            self.assertEqual(llm.effective_max_tokens(prov, None, 4096), 4096, prov)

    def test_intl_alias_is_covered(self):
        self.assertEqual(llm.effective_max_tokens("minimax-intl", None, 2048),
                         llm.THINKING_MAX_TOKENS_FLOOR)

    def test_note_only_when_raised(self):
        note = llm.max_tokens_note("minimax-cn", None, 4096)
        self.assertIsNotNone(note)
        self.assertIn(str(llm.THINKING_MAX_TOKENS_FLOOR), note)
        self.assertIn("4096", note)
        self.assertIsNone(llm.max_tokens_note("minimax-cn", False, 4096))
        self.assertIsNone(llm.max_tokens_note("minimax-cn", None, 32768))
        self.assertIsNone(llm.max_tokens_note("openai", None, 4096))


class BudgetVisibleInListTest(ThinkingDepthCase):
    """抬高这件事要真的到达界面（models.list 带 effectiveMaxTokens + 说明）。"""

    def _cfg(self, max_tokens: int) -> dict:
        return {"models": {"defaultId": "m1", "items": [{
            "id": "m1", "label": "M3", "provider": "minimax-cn",
            "baseUrl": "https://api.minimaxi.com/v1", "model": "MiniMax-M3",
            "apiKeyEnv": "TEST_MODEL_KEY", "maxTokens": max_tokens,
        }]}}

    def test_list_shows_raised_cap_and_note(self):
        self.write_config(self._cfg(4096))
        item = self.rpc("models.list")["result"]["items"][0]
        self.assertEqual(item["maxTokens"], 4096)              # 配置不动
        self.assertEqual(item["effectiveMaxTokens"], llm.THINKING_MAX_TOKENS_FLOOR)
        self.assertIsNotNone(item["maxTokensNote"])

    def test_list_quiet_when_nothing_raised(self):
        self.write_config(self._cfg(32768))
        item = self.rpc("models.list")["result"]["items"][0]
        self.assertEqual(item["effectiveMaxTokens"], 32768)
        self.assertIsNone(item["maxTokensNote"])


if __name__ == "__main__":
    unittest.main()