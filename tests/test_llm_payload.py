"""llm.py payload assembly tests for the four v2 generation fields.

Contract sections 1.1 + 6 (frozen), test requirements 5.9 and 5.10:
  * thinking tri-state on the request body:
      - null -> NO thinking-related key at all (for every provider);
      - adapted provider true/false -> the mapped provider parameter;
      - unadapted provider (ollama-local / custom / unknown) -> no key AND
        a log line noting the unadapted switch;
  * temperature / topP: null -> key absent; non-null -> key present with
    the exact value.

Zero network: all requests go through a capturing fake opener.
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
from llm import (ModelConfig, build_generation_params, is_thinking_adapted,  # noqa: E402
                 stream_chat, thinking_params)

# Keys that count as "thinking-related" for the null case (contract section 6:
# no thinking-related key may appear when thinking is null).
THINKING_KEYS = {"thinking", "enable_thinking", "reasoning_effort"}


def cfg(**overrides) -> ModelConfig:
    """A ModelConfig with the generation fields overridable per test."""
    kw = dict(base_url="https://example.invalid/v1", model="test-model",
              api_key_env="TEST_KEY", max_tokens=128, timeout_seconds=5)
    kw.update(overrides)
    c = ModelConfig(**kw)
    c.api_key = "secret-should-never-log"
    return c


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


def run_chat(c: ModelConfig) -> dict:
    """Drive stream_chat once with a fake opener; return the sent payload."""
    opener = CapturingOpener(sse({"choices": [{"delta": {"content": "x"}}]}))
    list(stream_chat(c, [{"role": "user", "content": "hi"}], opener=opener))
    return opener.payload()


# ---------------------------------------------------------------------------
# pure mapping function (contract section 6, one per provider family)
# ---------------------------------------------------------------------------

class ThinkingMappingTableTest(unittest.TestCase):
    """The mapping table itself, provider by provider (contract section 6)."""

    def test_minimax_family(self):
        # minimax-cn / minimax-intl -> thinking {"type": adaptive|disabled}
        for provider in ("minimax-cn", "minimax-intl"):
            self.assertEqual(
                thinking_params(provider, True), {"thinking": {"type": "adaptive"}})
            self.assertEqual(
                thinking_params(provider, False), {"thinking": {"type": "disabled"}})
            self.assertEqual(thinking_params(provider, None), {})

    def test_dashscope_and_siliconflow(self):
        # dashscope-bailian / siliconflow -> enable_thinking boolean
        for provider in ("dashscope-bailian", "siliconflow"):
            self.assertEqual(thinking_params(provider, True),
                             {"enable_thinking": True})
            self.assertEqual(thinking_params(provider, False),
                             {"enable_thinking": False})
            self.assertEqual(thinking_params(provider, None), {})

    def test_deepseek_moonshot_zhipu(self):
        # deepseek / moonshot / zhipu -> thinking {"type": enabled|disabled}
        for provider in ("deepseek", "moonshot", "zhipu"):
            self.assertEqual(
                thinking_params(provider, True), {"thinking": {"type": "enabled"}})
            self.assertEqual(
                thinking_params(provider, False), {"thinking": {"type": "disabled"}})
            self.assertEqual(thinking_params(provider, None), {})

    def test_openai_reasoning_effort(self):
        self.assertEqual(thinking_params("openai", True),
                         {"reasoning_effort": "high"})
        self.assertEqual(thinking_params("openai", False),
                         {"reasoning_effort": "none"})
        self.assertEqual(thinking_params("openai", None), {})

    def test_unadapted_providers_send_nothing(self):
        for provider in ("ollama-local", "custom", "some-random-vendor",
                         "", None):
            self.assertEqual(thinking_params(provider, True), {}, provider)
            self.assertEqual(thinking_params(provider, False), {}, provider)
            self.assertEqual(thinking_params(provider, None), {}, provider)
            self.assertFalse(is_thinking_adapted(provider), provider)

    def test_adapted_flag_matches_table(self):
        for provider in ("minimax-cn", "minimax-intl", "dashscope-bailian",
                         "siliconflow", "deepseek", "moonshot", "zhipu",
                         "openai"):
            self.assertTrue(is_thinking_adapted(provider), provider)

    def test_null_thinking_never_emits_any_thinking_key(self):
        """Contract: thinking=null means NO thinking-related key, any provider."""
        for provider in ("minimax-cn", "openai", "ollama-local", "custom",
                         "deepseek", None, "unknown"):
            for absent in THINKING_KEYS:
                self.assertNotIn(absent,
                                 thinking_params(provider, None), provider)

    def test_mapping_covers_all_contract_providers(self):
        self.assertEqual(
            set(llm.THINKING_PARAM_MAP.keys()),
            {"minimax-cn", "minimax-intl", "dashscope-bailian", "siliconflow",
             "deepseek", "moonshot", "zhipu", "openai"})

    def test_mapping_is_pure(self):
        """Same input -> same output; input dict not mutated."""
        got_a = thinking_params("zhipu", True)
        got_b = thinking_params("zhipu", True)
        self.assertEqual(got_a, got_b)
        self.assertIsNot(got_a, got_b)


# ---------------------------------------------------------------------------
# request body landing spots (through stream_chat, fake opener)
# ---------------------------------------------------------------------------

class ThinkingRequestBodyTest(unittest.TestCase):
    """Contract 5.9: thinking tri-state as it lands in the request body."""

    def test_null_thinking_no_thinking_key_in_body(self):
        # Old behavior regression: legacy ModelConfig (no new fields) and
        # every provider with thinking=None emit no thinking-related key.
        legacy = cfg()  # defaults: provider None, thinking None
        p = run_chat(legacy)
        for k in THINKING_KEYS:
            self.assertNotIn(k, p)
        for provider in ("minimax-cn", "openai", "deepseek", "custom"):
            p = run_chat(cfg(provider=provider, thinking=None))
            for k in THINKING_KEYS:
                self.assertNotIn(k, p, provider)

    def test_minimax_true_false_in_body(self):
        p = run_chat(cfg(provider="minimax-cn", thinking=True))
        self.assertEqual(p["thinking"], {"type": "adaptive"})
        p = run_chat(cfg(provider="minimax-cn", thinking=False))
        self.assertEqual(p["thinking"], {"type": "disabled"})

    def test_dashscope_enable_thinking_in_body(self):
        p = run_chat(cfg(provider="dashscope-bailian", thinking=True))
        self.assertIs(p["enable_thinking"], True)
        p = run_chat(cfg(provider="dashscope-bailian", thinking=False))
        self.assertIs(p["enable_thinking"], False)

    def test_deepseek_thinking_object_in_body(self):
        p = run_chat(cfg(provider="deepseek", thinking=True))
        self.assertEqual(p["thinking"], {"type": "enabled"})
        p = run_chat(cfg(provider="deepseek", thinking=False))
        self.assertEqual(p["thinking"], {"type": "disabled"})

    def test_openai_reasoning_effort_in_body(self):
        p = run_chat(cfg(provider="openai", thinking=True))
        self.assertEqual(p["reasoning_effort"], "high")
        p = run_chat(cfg(provider="openai", thinking=False))
        self.assertEqual(p["reasoning_effort"], "none")

    def test_unadapted_provider_no_key_and_log_line(self):
        """Unadapted provider with an explicit switch: no key + stderr note."""
        for provider in ("ollama-local", "custom", "who-knows"):
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                p = run_chat(cfg(provider=provider, thinking=True))
            for k in THINKING_KEYS:
                self.assertNotIn(k, p, provider)
            self.assertIn("not adapted", buf.getvalue(), provider)
            self.assertIn(provider, buf.getvalue(), provider)
            # false also counts as an explicit switch -> same note.
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                p = run_chat(cfg(provider=provider, thinking=False))
            for k in THINKING_KEYS:
                self.assertNotIn(k, p, provider)
            self.assertIn("not adapted", buf.getvalue(), provider)

    def test_unadapted_provider_null_thinking_no_log(self):
        """thinking=null on an unadapted provider stays silent (old behavior)."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            p = run_chat(cfg(provider="ollama-local", thinking=None))
        for k in THINKING_KEYS:
            self.assertNotIn(k, p)
        self.assertEqual(buf.getvalue(), "")

    def test_adapted_provider_no_log_line(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            run_chat(cfg(provider="minimax-cn", thinking=True))
        self.assertNotIn("not adapted", buf.getvalue())

    def test_log_line_never_contains_the_key_secret(self):
        """The unadapted warning must not leak the api key value."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            run_chat(cfg(provider="custom", thinking=True))
        self.assertNotIn("secret-should-never-log", buf.getvalue())


class TemperatureTopPBodyTest(unittest.TestCase):
    """Contract 5.10: temperature / topP key presence rules."""

    def test_null_temperature_and_topp_keys_absent(self):
        p = run_chat(cfg())
        self.assertNotIn("temperature", p)
        self.assertNotIn("top_p", p)

    def test_non_null_temperature_and_topp_present_with_values(self):
        p = run_chat(cfg(temperature=0.7, top_p=0.9))
        self.assertEqual(p["temperature"], 0.7)
        self.assertEqual(p["top_p"], 0.9)

    def test_temperature_only(self):
        p = run_chat(cfg(temperature=1.5))
        self.assertEqual(p["temperature"], 1.5)
        self.assertNotIn("top_p", p)

    def test_topp_only(self):
        p = run_chat(cfg(top_p=0.25))
        self.assertNotIn("temperature", p)
        self.assertEqual(p["top_p"], 0.25)

    def test_boundary_values(self):
        p = run_chat(cfg(temperature=0, top_p=0))
        self.assertEqual(p["temperature"], 0)
        self.assertEqual(p["top_p"], 0)
        p = run_chat(cfg(temperature=2, top_p=1))
        self.assertEqual(p["temperature"], 2)
        self.assertEqual(p["top_p"], 1)

    def test_zero_is_not_treated_as_null(self):
        """0 is a valid non-null value; it must not be dropped (falsy trap)."""
        p = run_chat(cfg(temperature=0.0, top_p=0.0))
        self.assertIn("temperature", p)
        self.assertIn("top_p", p)

    def test_generation_params_do_not_break_core_payload(self):
        p = run_chat(cfg(provider="minimax-cn", thinking=True,
                         temperature=0.7, top_p=0.9))
        # The core fields keep their shape alongside the new ones.
        self.assertEqual(p["model"], "test-model")
        self.assertEqual(p["stream"], True)
        self.assertEqual(p["stream_options"], {"include_usage": True})
        # max_tokens is the one core field generation params DO touch: thinking
        # eats the same budget on this vendor, so the cap is raised (the config
        # keeps 128; only the request goes out higher).
        self.assertEqual(p["max_tokens"], llm.THINKING_MAX_TOKENS_FLOOR)
        self.assertEqual(p["messages"], [{"role": "user", "content": "hi"}])
        self.assertNotIn("tools", p)

    def test_max_tokens_untouched_when_thinking_off(self):
        p = run_chat(cfg(provider="minimax-cn", thinking=False,
                         temperature=0.7, top_p=0.9))
        self.assertEqual(p["max_tokens"], 128)


class BuildGenerationParamsTest(unittest.TestCase):
    """Direct unit tests of the pure assembly function (no network, no send)."""

    def test_all_null_gives_empty_dict(self):
        self.assertEqual(build_generation_params("minimax-cn", None, None, None), {})
        self.assertEqual(build_generation_params(None, None, None, None), {})
        self.assertEqual(build_generation_params("custom", None, None, None), {})

    def test_combined_values(self):
        got = build_generation_params("siliconflow", True, 0.5, 1.0)
        self.assertEqual(got, {"temperature": 0.5, "top_p": 1.0,
                               "enable_thinking": True})

    def test_temperature_and_topp_with_unadapted_thinking(self):
        got = build_generation_params("ollama-local", True, 0.5, 0.5)
        self.assertEqual(got, {"temperature": 0.5, "top_p": 0.5})


class ModelConfigFromResolvedTest(unittest.TestCase):
    """modelconfig.resolve_model dict -> ModelConfig wiring (no sidecar)."""

    def test_from_resolved_full(self):
        c = ModelConfig.from_resolved({
            "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "api_key_env": "DEEPSEEK_API_KEY", "api_key": "sk-x",
            "max_tokens": 8192, "timeout_seconds": 60,
            "provider": "deepseek", "contextWindow": 256000,
            "thinking": True, "temperature": 0.3, "topP": 0.95})
        self.assertEqual(c.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(c.model, "deepseek-chat")
        self.assertEqual(c.api_key, "sk-x")
        self.assertEqual(c.max_tokens, 8192)
        self.assertEqual(c.provider, "deepseek")
        self.assertEqual(c.context_window, 256000)
        self.assertEqual(c.contextWindow, 256000)   # camelCase alias
        self.assertIs(c.thinking, True)
        self.assertEqual(c.temperature, 0.3)
        self.assertEqual(c.top_p, 0.95)

    def test_from_resolved_minimal_defaults(self):
        c = ModelConfig.from_resolved({
            "base_url": "https://x/v1", "model": "m",
            "api_key_env": "K", "api_key": "sk"})
        self.assertIsNone(c.provider)
        self.assertIsNone(c.thinking)
        self.assertIsNone(c.temperature)
        self.assertIsNone(c.top_p)
        self.assertEqual(c.context_window, 128000)

    def test_from_resolved_drops_non_bool_thinking(self):
        """A malformed thinking value degrades to null, never crashes."""
        c = ModelConfig.from_resolved({
            "base_url": "https://x/v1", "model": "m",
            "api_key_env": "K", "api_key": "sk", "thinking": "yes"})
        self.assertIsNone(c.thinking)

    def test_from_resolved_to_payload_end_to_end(self):
        """resolve-model-shaped dict -> ModelConfig -> request body."""
        c = ModelConfig.from_resolved({
            "base_url": "https://api.minimaxi.com/v1", "model": "MiniMax-M3",
            "api_key_env": "MINIMAX_CN_API_KEY", "api_key": "sk-mm",
            "max_tokens": 4096, "timeout_seconds": 180,
            "provider": "minimax-cn", "contextWindow": 1000000,
            "thinking": False, "temperature": None, "topP": 0.9})
        p = run_chat(c)
        self.assertEqual(p["thinking"], {"type": "disabled"})
        self.assertNotIn("temperature", p)
        self.assertEqual(p["top_p"], 0.9)
        self.assertNotIn("enable_thinking", p)
        self.assertNotIn("reasoning_effort", p)

    def test_secret_not_in_payload(self):
        """api_key never lands in the JSON body (header only)."""
        p = run_chat(cfg(provider="deepseek", thinking=True))
        self.assertNotIn("secret-should-never-log", json.dumps(p))


if __name__ == "__main__":
    unittest.main()
