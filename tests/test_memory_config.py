"""Tests for memory/memory_config.py: the eight explicit overrides,
the four startup assertions and the injection budget. Zero network."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "python",
    ),
)

from memory import memory_config as MC  # noqa: E402


class BuildEnvTests(unittest.TestCase):
    """Case (a): all eight overrides are always written explicitly."""

    def _assert_all_eight(self, env):
        self.assertEqual(env[MC.EMBEDDING_MODEL_KEY], MC.EMBEDDING_MODEL_DEFAULT)
        self.assertEqual(env[MC.VEC_TYPE_KEY], MC.VEC_TYPE_DEFAULT)
        self.assertEqual(env[MC.LLM_ENABLED_KEY], MC.LLM_ENABLED_DEFAULT)
        self.assertEqual(env[MC.EMBEDDINGS_VIA_API_KEY],
                         MC.EMBEDDINGS_VIA_API_DEFAULT)
        self.assertEqual(env[MC.HOST_LLM_ENABLED_KEY],
                         MC.HOST_LLM_ENABLED_DEFAULT)
        self.assertEqual(env[MC.INJECTION_TOP_K_KEY], MC.INJECTION_TOP_K_DEFAULT)
        self.assertEqual(env[MC.INJECTION_ITEM_CHARS_KEY],
                         MC.INJECTION_ITEM_CHARS_DEFAULT)
        self.assertEqual(env[MC.INJECTION_TOTAL_CHARS_KEY],
                         MC.INJECTION_TOTAL_CHARS_DEFAULT)

    def test_all_eight_keys_present_on_empty_base(self):
        self._assert_all_eight(MC.build_env({}))

    def test_opposite_values_in_base_env_are_overwritten(self):
        # The design note warns package and schema defaults contradict
        # each other, so nothing may pass through un-pinned.
        base = {
            MC.EMBEDDING_MODEL_KEY: "BAAI/bge-m3",
            MC.VEC_TYPE_KEY: "float32",
            MC.LLM_ENABLED_KEY: "true",
            MC.EMBEDDINGS_VIA_API_KEY: "1",
            MC.HOST_LLM_ENABLED_KEY: "true",
            MC.INJECTION_TOP_K_KEY: "50",
            MC.INJECTION_ITEM_CHARS_KEY: "0",
            MC.INJECTION_TOTAL_CHARS_KEY: "99999",
        }
        self._assert_all_eight(MC.build_env(base))
        self.assertNotEqual(MC.build_env(base)[MC.INJECTION_ITEM_CHARS_KEY], "0")

    def test_base_env_not_mutated_and_unrelated_keys_kept(self):
        base = {"MNEMOSYNE_UNRELATED": "keep-me", MC.LLM_ENABLED_KEY: "true"}
        snapshot = dict(base)
        env = MC.build_env(base)
        self.assertEqual(base, snapshot)
        self.assertEqual(env["MNEMOSYNE_UNRELATED"], "keep-me")


class AssertSafeTests(unittest.TestCase):
    """Case (b): each of the four assertions catches its own failure."""

    def test_clean_env_from_build_env_passes(self):
        self.assertEqual(MC.assert_safe(MC.build_env({})), [])

    def test_llm_enabled_failure_is_caught(self):
        env = MC.build_env({})
        env[MC.LLM_ENABLED_KEY] = "true"
        failures = MC.assert_safe(env)
        self.assertEqual(len(failures), 1)
        self.assertIn(MC.ASSERT_LLM_DISABLED, failures[0])

    def test_embeddings_via_api_failure_is_caught(self):
        env = MC.build_env({})
        env[MC.EMBEDDINGS_VIA_API_KEY] = "1"
        failures = MC.assert_safe(env)
        self.assertEqual(len(failures), 1)
        self.assertIn(MC.ASSERT_EMBEDDINGS_LOCAL, failures[0])

    def test_remote_llm_url_failure_is_caught(self):
        env = MC.build_env({})
        env["MNEMOSYNE_LLM_API_URL"] = "https://example.invalid/v1"
        failures = MC.assert_safe(env)
        self.assertEqual(len(failures), 1)
        self.assertIn(MC.ASSERT_NO_REMOTE_LLM, failures[0])
        self.assertIn("MNEMOSYNE_LLM_API_URL", failures[0])

    def test_importer_enabled_failure_is_caught(self):
        env = MC.build_env({})
        env["MNEMOSYNE_IMPORTER_ENABLED"] = "true"
        failures = MC.assert_safe(env)
        self.assertEqual(len(failures), 1)
        self.assertIn(MC.ASSERT_IMPORTER_DISABLED, failures[0])

    def test_empty_remote_llm_value_is_not_a_failure(self):
        env = MC.build_env({})
        env["MNEMOSYNE_LLM_API_URL"] = ""
        self.assertEqual(MC.assert_safe(env), [])


class InjectionBudgetTests(unittest.TestCase):
    """Case (c): item truncation and head/tail total-budget split."""

    def test_short_texts_pass_through_untouched(self):
        block, truncated = MC.injection_budget(["alpha", "beta"])
        self.assertFalse(truncated)
        self.assertEqual(block, "alpha\n\nbeta")

    def test_single_overlong_item_is_truncated_to_item_limit(self):
        text = "x" * (MC.INJECTION_ITEM_CHARS + 500)
        block, truncated = MC.injection_budget([text])
        self.assertTrue(truncated)
        self.assertLessEqual(len(block), MC.INJECTION_ITEM_CHARS + 60)
        self.assertIn(MC.ITEM_TRUNCATION_MARKER, block)

    def test_total_budget_head_tail_split_with_marker(self):
        items = ["item-%03d " % i + "y" * 90 for i in range(90)]
        block, truncated = MC.injection_budget(items)
        self.assertTrue(truncated)
        self.assertLessEqual(len(block), MC.MEMORY_CONTEXT_MAX_CHARS)
        self.assertIn("chars of memory truncated", block)
        # Head keeps the FIRST item text, tail keeps the LAST item.
        self.assertIn("item-000", block[:100])
        self.assertIn("item-089", block[-200:])

    def test_head_tail_length_ratio_when_over_budget(self):
        items = ["z" * 2001] * 4  # 4 * 2001 + separators > 6000
        block, _ = MC.injection_budget(items)
        # The marker template starts with a newline; measure from it.
        marker_start = block.index("\n[...")
        self.assertEqual(marker_start, MC.MEMORY_CONTEXT_HEAD_CHARS)
        marker_end = block.index("]\n", marker_start) + len("]\n")
        tail_len = len(block) - marker_end
        self.assertEqual(tail_len, MC.MEMORY_CONTEXT_TAIL_CHARS)
        self.assertLessEqual(len(block), MC.MEMORY_CONTEXT_HEAD_CHARS
                             + MC.MEMORY_CONTEXT_TAIL_CHARS + 60)

    def test_empty_input_yields_empty_block(self):
        self.assertEqual(MC.injection_budget([]), ("", False))


class ModelCacheTest(unittest.TestCase):
    """The embedding model must not land on C:.

    Fastembed and the HF stack default under the user profile on Windows, and
    that is exactly the drive this project must not fill, so build_env pins
    them under the engine home -- while an explicit caller value still wins.
    """

    HOME = os.path.join("E:" + os.sep, "desk-agent-cache", "userdata", "data", "memory")

    def test_cache_paths_default_under_the_engine_home(self) -> None:
        env = MC.build_env({}, home=self.HOME)
        expected = os.path.join(self.HOME, "models")
        for key in MC.MODEL_CACHE_KEYS:
            self.assertEqual(env.get(key), expected, key)

    def test_an_explicit_cache_path_is_respected(self) -> None:
        env = MC.build_env({"FASTEMBED_CACHE_PATH": os.path.join("E:" + os.sep, "cache")},
                           home=self.HOME)
        self.assertEqual(env["FASTEMBED_CACHE_PATH"], os.path.join("E:" + os.sep, "cache"))
        self.assertEqual(env["HF_HOME"], os.path.join(self.HOME, "models"))

    def test_empty_values_are_treated_as_unset(self) -> None:
        env = MC.build_env({"HF_HOME": ""}, home=self.HOME)
        self.assertEqual(env["HF_HOME"], os.path.join(self.HOME, "models"))

    def test_no_home_means_no_cache_keys_invented(self) -> None:
        env = MC.build_env({})
        for key in MC.MODEL_CACHE_KEYS:
            self.assertNotIn(key, env)

    def test_safety_overrides_still_win_over_the_caller(self) -> None:
        env = MC.build_env({MC.LLM_ENABLED_KEY: "true"}, home=self.HOME)
        self.assertFalse(MC._truthy(env[MC.LLM_ENABLED_KEY]))
        self.assertEqual(MC.assert_safe(env), [])


if __name__ == "__main__":
    unittest.main()
