"""Wiring tests for the context mechanism inside the sidecar (contract section 7).

These assert the *wiring* rather than the mechanism itself (that lives in
test_context_mechanism.py): sidecar must hand the per-model window to the agent
loop and must assemble a governor with a real summarizer and a spill directory
next to the workspace. Before 2026-09-23 the mechanism existed but was never
instantiated in production, so nothing ran at all.
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

import llm  # noqa: E402
import sidecar  # noqa: E402


class SpillDirTest(unittest.TestCase):
    def test_spill_sits_next_to_the_workspace(self):
        # POSIX-portable: os.path.abspath would prepend the cwd to "E:/..."
        # on Linux, so exercise the sibling rule with a normal absolute path.
        root = os.path.join(os.sep, "cache", "userdata", "workspace")
        d = sidecar._spill_dir(root)
        self.assertEqual(d, os.path.join(os.sep, "cache", "userdata", "spill"))

    def test_spill_falls_back_when_root_is_empty(self):
        self.assertTrue(sidecar._spill_dir("").endswith("spill"))


class SummarizerWiringTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._orig = llm.chat_once

    def tearDown(self):
        llm.chat_once = self._orig

    def test_summarizer_uses_the_main_model_and_returns_its_text(self):
        def fake_once(cfg, prompt, **kw):
            self.calls.append((cfg, prompt))
            return "摘要正文"
        llm.chat_once = fake_once
        cfg = llm.ModelConfig(base_url="https://example.invalid/v1", model="m",
                              api_key_env="NOPE", max_tokens=64, timeout_seconds=5)
        summarize = sidecar._make_summarizer(cfg)
        got = summarize("用户：把 A 文件改一下\n助手：好的")
        self.assertEqual(got, "摘要正文")
        self.assertEqual(len(self.calls), 1)
        self.assertIs(self.calls[0][0], cfg)
        self.assertIn("把 A 文件改一下", self.calls[0][1])


class ResolvedConfigCarriesGenerationFieldsTest(unittest.TestCase):
    """sidecar builds the per-request ModelConfig via from_resolved; if that
    dropped a field, the request body would silently lose it."""

    def test_from_resolved_keeps_window_and_generation_params(self):
        info = {
            "base_url": "https://example.invalid/v1", "model": "MiniMax-M3",
            "api_key_env": "K", "api_key": "x", "max_tokens": 4096,
            "timeout_seconds": 180, "provider": "minimax-cn",
            "contextWindow": 1000000, "thinking": False,
            "temperature": 0.3, "topP": 0.9,
        }
        cfg = llm.ModelConfig.from_resolved(info)
        self.assertEqual(cfg.context_window, 1000000)
        self.assertIs(cfg.thinking, False)
        self.assertEqual(cfg.temperature, 0.3)
        self.assertEqual(cfg.top_p, 0.9)

    def test_from_resolved_defaults_do_not_invent_values(self):
        cfg = llm.ModelConfig.from_resolved({"base_url": "u", "model": "m",
                                             "api_key_env": "K", "api_key": "x",
                                             "max_tokens": 8, "timeout_seconds": 5})
        self.assertEqual(cfg.context_window, 128000)
        self.assertIsNone(cfg.thinking)
        self.assertIsNone(cfg.temperature)
        self.assertIsNone(cfg.top_p)


class ResolveModelProviderTest(unittest.TestCase):
    """The chain that broke on 2026-09-23: resolve_model must expose `provider`,
    otherwise the section 6 thinking mapping silently matches nothing and the
    toggle looks implemented while sending no key at all."""

    def test_resolve_model_carries_provider_and_generation_fields(self):
        import json
        import tempfile
        import modelconfig as mc
        root = tempfile.mkdtemp()
        cfg = {"models": {"defaultId": "x", "items": [{
            "id": "x", "label": "X", "provider": "minimax-cn",
            "baseUrl": "https://example.invalid/v1", "model": "MiniMax-M3",
            "apiKeyEnv": "WIRING_TEST_KEY", "maxTokens": 2048, "timeoutSeconds": 180,
            "contextWindow": 1000000, "thinking": False, "temperature": 0.3, "topP": 0.9}]},
            "capabilities": {}}
        with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(cfg))
        os.environ["WIRING_TEST_KEY"] = "dummy"
        info = mc.resolve_model("x", root=root)
        self.assertEqual(info.get("provider"), "minimax-cn")
        cfgobj = llm.ModelConfig.from_resolved(info)
        self.assertEqual(cfgobj.provider, "minimax-cn")
        params = llm.build_generation_params(cfgobj.provider, cfgobj.thinking,
                                             cfgobj.temperature, cfgobj.top_p)
        self.assertEqual(params.get("thinking"), {"type": "disabled"})
        self.assertEqual(params.get("temperature"), 0.3)
        self.assertEqual(params.get("top_p"), 0.9)
        del os.environ["WIRING_TEST_KEY"]


if __name__ == "__main__":
    unittest.main()
