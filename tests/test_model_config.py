"""modelconfig + models.* RPC tests (contract: docs/model-config-spec.md).

Covers all seven requirements of contract section 5:
  1. legacy synthesis from the old top-level `model` section;
  2. hasKey semantics + no key value ever in a response;
  3. upsert (create / rename / id conflict / bad apiKeyEnv);
  4. remove (MODEL_LAST, default re-pointing);
  5. env file writes (in-place update, comments kept, no dup lines, env set);
  6. other config sections preserved across upsert/remove;
  7. chat modelId resolution -> MODEL_NOT_FOUND / MODEL_KEY_MISSING.

Zero network: everything runs against temp dirs and in-memory sidecar calls.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

import modelconfig as MC  # noqa: E402
import sidecar  # noqa: E402


LEGACY_CONFIG = {
    "model": {
        "baseUrl": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M3",
        "apiKeyEnv": "MINIMAX_CN_API_KEY",
        "maxTokens": 2048,
        "timeoutSeconds": 90,
    },
    "capabilities": {
        "workspaceRoot": "",
        "vision": {"provider": "minimax"},
        "models": {"MiniMax-M3": {"inputModalities": ["text", "image"]}},
    },
}

NEW_CONFIG = {
    "model": LEGACY_CONFIG["model"],
    "models": {
        "defaultId": "mm-m3",
        "items": [
            {
                "id": "mm-m3",
                "label": "MiniMax M3",
                "provider": "minimax-cn",
                "baseUrl": "https://api.minimaxi.com/v1",
                "model": "MiniMax-M3",
                "apiKeyEnv": "MINIMAX_CN_API_KEY",
                "maxTokens": 4096,
                "timeoutSeconds": 180,
                "inputModalities": ["text", "image"],
            },
            {
                "id": "ds-chat",
                "label": "DeepSeek Chat",
                "provider": "deepseek",
                "baseUrl": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
                "apiKeyEnv": "DEEPSEEK_API_KEY",
                "maxTokens": 8192,
                "timeoutSeconds": 60,
            },
        ],
    },
    "capabilities": LEGACY_CONFIG["capabilities"],
}


class TempRootCase(unittest.TestCase):
    """Base: temp repo root with config.json + a temp env file + clean env."""

    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="desk-agent-models-")
        self.env_file = os.path.join(self.root, "test.env")
        self._saved_env = dict(os.environ)
        # Isolate env var overrides and key values for every test.
        for var in ("DESK_AGENT_ENV_FILE", "MINIMAX_CN_API_KEY",
                    "DEEPSEEK_API_KEY", "TEST_MODEL_KEY"):
            os.environ.pop(var, None)
        os.environ["DESK_AGENT_ENV_FILE"] = self.env_file
        # Point the models registry at the temp root so RPCs (which have no
        # root parameter in the protocol) never touch the real config.json.
        self._saved_override = MC._MODELS_ROOT_OVERRIDE
        MC._MODELS_ROOT_OVERRIDE = self.root
        # sidecar caches nothing per-root, but keep the module-level config
        # cache clean so the legacy path is re-tested fresh each time.
        sidecar._CFG = None
        # Session persistence writes a real SQLite file. Point the data dir at
        # the temp root and drop the process-wide store so the next chat opens
        # its own database there instead of the developer's real sessions.db.
        self._saved_data_dir = sidecar._DATA_DIR
        self._saved_store = sidecar._SESSIONS
        sidecar._DATA_DIR = os.path.join(self.root, "data")
        sidecar._SESSIONS = None

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)
        MC._MODELS_ROOT_OVERRIDE = self._saved_override
        sidecar._CFG = None
        sidecar._DATA_DIR = self._saved_data_dir
        sidecar._SESSIONS = self._saved_store

    def write_config(self, cfg: dict) -> None:
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def read_config(self) -> dict:
        with open(os.path.join(self.root, "config.json"), encoding="utf-8") as f:
            return json.load(f)

    def rpc(self, method: str, params: dict | None = None):
        class _Reply(dict):
            """Both call styles: reply["result"] and reply.result."""
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


class LegacySynthesisTest(TempRootCase):
    """Contract 5.1: old config (only `model` section) synthesizes legacy."""

    def test_legacy_item_listed(self):
        self.write_config(LEGACY_CONFIG)
        res = self.rpc("models.list")
        self.assertTrue(res["ok"], res)
        result = res["result"]
        self.assertEqual(len(result["items"]), 1)
        item = result["items"][0]
        self.assertEqual(item["id"], "legacy")
        self.assertEqual(item["label"], "MiniMax-M3")
        self.assertEqual(item["baseUrl"], "https://api.minimaxi.com/v1")
        self.assertEqual(item["model"], "MiniMax-M3")
        self.assertEqual(item["apiKeyEnv"], "MINIMAX_CN_API_KEY")
        self.assertEqual(item["maxTokens"], 2048)
        self.assertEqual(item["timeoutSeconds"], 90)
        self.assertEqual(item["inputModalities"], ["text", "image"])
        self.assertEqual(result["defaultId"], "legacy")
        self.assertEqual(result["activeId"], "legacy")

    def test_legacy_chat_still_resolves(self):
        self.write_config(LEGACY_CONFIG)
        os.environ["MINIMAX_CN_API_KEY"] = "sk-legacy-test"
        info = MC.resolve_model(None, root=self.root)
        self.assertEqual(info["id"], "legacy")
        self.assertEqual(info["model"], "MiniMax-M3")
        self.assertEqual(info["api_key"], "sk-legacy-test")


class HasKeySemanticsTest(TempRootCase):
    """Contract 5.2: hasKey true/false; no key value in the response."""

    def test_haskey_reflects_environment(self):
        self.write_config(NEW_CONFIG)
        res = self.rpc("models.list")
        by_id = {it["id"]: it for it in res["result"]["items"]}
        self.assertFalse(by_id["mm-m3"]["hasKey"])   # env not set
        os.environ["MINIMAX_CN_API_KEY"] = "sk-secret-value-1"
        res = self.rpc("models.list")
        by_id = {it["id"]: it for it in res["result"]["items"]}
        self.assertTrue(by_id["mm-m3"]["hasKey"])
        self.assertFalse(by_id["ds-chat"]["hasKey"])
        # Empty string counts as unset.
        os.environ["MINIMAX_CN_API_KEY"] = "   "
        res = self.rpc("models.list")
        by_id = {it["id"]: it for it in res["result"]["items"]}
        self.assertFalse(by_id["mm-m3"]["hasKey"])

    def test_no_key_value_in_response(self):
        self.write_config(NEW_CONFIG)
        os.environ["MINIMAX_CN_API_KEY"] = "sk-secret-value-1"
        os.environ["DEEPSEEK_API_KEY"] = "sk-secret-value-2"
        res = self.rpc("models.list")
        text = json.dumps(res, ensure_ascii=False)
        self.assertNotIn("sk-secret-value-1", text)
        self.assertNotIn("sk-secret-value-2", text)
        for item in res["result"]["items"]:
            self.assertIn("hasKey", item)
            self.assertNotIn("apiKey", item)
            self.assertIsInstance(item["hasKey"], bool)


class UpsertTest(TempRootCase):
    """Contract 5.3: upsert create / rename / conflict / invalid apiKeyEnv."""

    def setUp(self):
        super().setUp()
        self.write_config(NEW_CONFIG)

    def test_create_with_explicit_id(self):
        res = self.rpc("models.upsert", {
            "id": "kimi", "label": "Kimi", "provider": "moonshot",
            "baseUrl": "https://api.moonshot.cn/v1", "model": "kimi-latest",
            "apiKeyEnv": "MOONSHOT_API_KEY", "maxTokens": 1024,
            "timeoutSeconds": 30, "inputModalities": ["text"]})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["id"], "kimi")
        cfg = self.read_config()
        ids = [it["id"] for it in cfg["models"]["items"]]
        self.assertIn("kimi", ids)
        self.assertEqual(len(ids), 3)

    def test_update_existing_id_replaces_fields(self):
        res = self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek V3", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1",
            "model": "deepseek-v3", "apiKeyEnv": "DEEPSEEK_API_KEY"})
        self.assertTrue(res["ok"], res)
        cfg = self.read_config()
        item = next(it for it in cfg["models"]["items"] if it["id"] == "ds-chat")
        self.assertEqual(item["label"], "DeepSeek V3")
        self.assertEqual(item["model"], "deepseek-v3")
        # defaults applied when omitted
        self.assertEqual(item["maxTokens"], 4096)
        self.assertEqual(item["timeoutSeconds"], 180)

    def test_id_omitted_generates_slug(self):
        res = self.rpc("models.upsert", {
            "label": "Qwen Max", "baseUrl": "https://x.example/v1",
            "model": "qwen-max", "apiKeyEnv": "QWEN_API_KEY"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["id"], "qwen-max")

    def test_id_omitted_generated_conflict_rejected(self):
        # "MiniMax M3" slugifies to "minimax-m3"; make one exist first.
        self.rpc("models.upsert", {"id": "minimax-m3", "label": "MiniMax M3",
                                   "baseUrl": "https://x.example/v1",
                                   "model": "m3", "apiKeyEnv": "X_API_KEY"})
        res = self.rpc("models.upsert", {
            "label": "MiniMax M3", "baseUrl": "https://x.example/v1",
            "model": "m3", "apiKeyEnv": "X_API_KEY"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_INVALID")

    def test_duplicate_id_rejected(self):
        """Contract 2: an omitted id is slugged from the label; a slug that
        collides with an existing id is refused (no auto-suffix)."""
        first = self.rpc("models.upsert", {
            "id": "kimi", "label": "Kimi", "provider": "moonshot",
            "baseUrl": "https://api.moonshot.cn/v1", "model": "kimi-latest",
            "apiKeyEnv": "MOONSHOT_API_KEY"})
        self.assertTrue(first["ok"], first)
        res = self.rpc("models.upsert", {
            "label": "Kimi", "baseUrl": "https://api.moonshot.cn/v1",
            "model": "kimi-k2", "apiKeyEnv": "MOONSHOT_API_KEY"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_INVALID")
        self.assertIn("duplicate", res["error"]["message"])

    def test_invalid_api_key_env_lowercase(self):
        res = self.rpc("models.upsert", {
            "id": "bad-1", "label": "Bad", "baseUrl": "https://x/v1",
            "model": "m", "apiKeyEnv": "bad_key"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_INVALID")
        self.assertIn("apiKeyEnv", res["error"]["message"])

    def test_invalid_api_key_env_dash(self):
        res = self.rpc("models.upsert", {
            "id": "bad-2", "label": "Bad", "baseUrl": "https://x/v1",
            "model": "m", "apiKeyEnv": "BAD-KEY"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_INVALID")

    def test_invalid_id_characters(self):
        res = self.rpc("models.upsert", {
            "id": "Bad Id!", "label": "Bad", "baseUrl": "https://x/v1",
            "model": "m", "apiKeyEnv": "X_API_KEY"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_INVALID")

    def test_missing_required_fields(self):
        for missing in ("label", "baseUrl", "model", "apiKeyEnv"):
            params = {"id": "x-%s" % missing, "label": "L", "baseUrl": "https://x/v1",
                      "model": "m", "apiKeyEnv": "X_API_KEY"}
            params.pop(missing)
            res = self.rpc("models.upsert", params)
            self.assertFalse(res["ok"], missing)
            self.assertEqual(res["error"]["code"], "MODEL_INVALID")
            self.assertIn(missing, res["error"]["message"])


class RemoveTest(TempRootCase):
    """Contract 5.4: MODEL_LAST on last entry; default re-points on remove."""

    def setUp(self):
        super().setUp()
        self.write_config(NEW_CONFIG)

    def test_remove_last_entry_rejected(self):
        self.rpc("models.remove", {"id": "ds-chat"})
        res = self.rpc("models.remove", {"id": "mm-m3"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_LAST")

    def test_remove_unknown_id(self):
        res = self.rpc("models.remove", {"id": "nope"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_NOT_FOUND")

    def test_remove_non_default(self):
        res = self.rpc("models.remove", {"id": "ds-chat"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["removed"], "ds-chat")
        self.assertNotIn("newDefaultId", res["result"])
        cfg = self.read_config()
        self.assertEqual(cfg["models"]["defaultId"], "mm-m3")

    def test_remove_default_repoints(self):
        res = self.rpc("models.remove", {"id": "mm-m3"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["removed"], "mm-m3")
        self.assertEqual(res["result"]["newDefaultId"], "ds-chat")
        cfg = self.read_config()
        self.assertEqual(cfg["models"]["defaultId"], "ds-chat")
        ids = [it["id"] for it in cfg["models"]["items"]]
        self.assertEqual(ids, ["ds-chat"])

    def test_remove_on_legacy_single_entry(self):
        # Legacy config has exactly one synthesized entry -> MODEL_LAST.
        self.write_config(LEGACY_CONFIG)
        res = self.rpc("models.remove", {"id": "legacy"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_LAST")


class EnvFileTest(TempRootCase):
    """Contract 5.5: env file write semantics."""

    def setUp(self):
        super().setUp()
        self.write_config(NEW_CONFIG)

    def _read_env_text(self) -> str:
        with open(self.env_file, encoding="utf-8") as f:
            return f.read()

    def test_upsert_with_api_key_writes_env_and_sets_environ(self):
        res = self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY", "apiKey": "sk-test-env-write"})
        self.assertTrue(res["ok"], res)
        text = self._read_env_text()
        self.assertIn("DEEPSEEK_API_KEY=sk-test-env-write", text)
        self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), "sk-test-env-write")
        # The response must not echo the key.
        self.assertNotIn("sk-test-env-write", json.dumps(res))
        # And config.json must not contain it either.
        self.assertNotIn("sk-test-env-write", json.dumps(self.read_config()))
        # hasKey flips to true right after the write.
        listed = self.rpc("models.list").result
        by_id = {it["id"]: it for it in listed["items"]}
        self.assertTrue(by_id["ds-chat"]["hasKey"])

    def test_update_existing_key_no_duplicate_lines(self):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("# provider keys\nDEEPSEEK_API_KEY=old-value\nOTHER_VAR=1\n")
        self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY", "apiKey": "new-value"})
        text = self._read_env_text()
        lines = [ln for ln in text.splitlines() if ln.strip()]
        key_lines = [ln for ln in lines if ln.startswith("DEEPSEEK_API_KEY=")]
        self.assertEqual(len(key_lines), 1)
        self.assertEqual(key_lines[0], "DEEPSEEK_API_KEY=new-value")
        # comments and other lines survive
        self.assertIn("# provider keys", text)
        self.assertIn("OTHER_VAR=1", text)
        self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), "new-value")

    def test_append_new_key_keeps_comments(self):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("# top comment\nFOO=bar\n")
        self.rpc("models.upsert", {
            "id": "kimi", "label": "Kimi", "baseUrl": "https://x/v1",
            "model": "kimi", "apiKeyEnv": "MOONSHOT_API_KEY",
            "apiKey": "sk-moon"})
        text = self._read_env_text()
        self.assertIn("# top comment", text)
        self.assertIn("FOO=bar", text)
        self.assertIn("MOONSHOT_API_KEY=sk-moon", text)
        lines = [ln for ln in text.splitlines() if ln.startswith("MOONSHOT_API_KEY=")]
        self.assertEqual(len(lines), 1)

    def test_env_file_missing_created(self):
        self.assertFalse(os.path.exists(self.env_file))
        MC.write_env_var("BRAND_NEW_KEY", "v", root=self.root)
        self.assertTrue(os.path.exists(self.env_file))
        self.assertIn("BRAND_NEW_KEY=v", self._read_env_text())
        self.assertEqual(os.environ.get("BRAND_NEW_KEY"), "v")

    def test_env_file_path_priority(self):
        # DESK_AGENT_ENV_FILE (set in setUp) beats <root>/.env.
        self.assertEqual(MC.env_file_path(self.root), self.env_file)
        os.environ.pop("DESK_AGENT_ENV_FILE")
        self.assertEqual(MC.env_file_path(self.root), os.path.join(self.root, ".env"))

    def test_empty_api_key_not_written(self):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write("FOO=bar\n")
        self.rpc("models.upsert", {
            "id": "kimi", "label": "Kimi", "baseUrl": "https://x/v1",
            "model": "kimi", "apiKeyEnv": "MOONSHOT_API_KEY", "apiKey": ""})
        self.assertNotIn("MOONSHOT_API_KEY", self._read_env_text())


class PreserveSectionsTest(TempRootCase):
    """Contract 5.6: other config sections survive upsert/remove/setDefault."""

    def setUp(self):
        super().setUp()
        self.write_config(NEW_CONFIG)

    def test_capabilities_preserved_after_upsert(self):
        before = self.read_config()["capabilities"]
        self.rpc("models.upsert", {
            "id": "new-one", "label": "New", "baseUrl": "https://x/v1",
            "model": "new-model", "apiKeyEnv": "NEW_KEY"})
        after = self.read_config()["capabilities"]
        self.assertEqual(after, before)

    def test_capabilities_preserved_after_remove_and_default(self):
        before = self.read_config()["capabilities"]
        self.rpc("models.remove", {"id": "ds-chat"})
        self.rpc("models.setDefault", {"id": "mm-m3"})
        after = self.read_config()["capabilities"]
        self.assertEqual(after, before)

    def test_top_level_model_section_preserved(self):
        self.rpc("models.upsert", {
            "id": "new-one", "label": "New", "baseUrl": "https://x/v1",
            "model": "new-model", "apiKeyEnv": "NEW_KEY"})
        cfg = self.read_config()
        self.assertEqual(cfg["model"], NEW_CONFIG["model"])


class SetDefaultTest(TempRootCase):
    def setUp(self):
        super().setUp()
        self.write_config(NEW_CONFIG)

    def test_set_default(self):
        res = self.rpc("models.setDefault", {"id": "ds-chat"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["defaultId"], "ds-chat")
        cfg = self.read_config()
        self.assertEqual(cfg["models"]["defaultId"], "ds-chat")
        listed = self.rpc("models.list").result
        self.assertEqual(listed["activeId"], "ds-chat")

    def test_set_default_unknown_id(self):
        res = self.rpc("models.setDefault", {"id": "ghost"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_NOT_FOUND")

    def test_set_default_missing_param(self):
        res = self.rpc("models.setDefault", {})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "BAD_REQUEST")


class PresetsTest(TempRootCase):
    def test_preset_shape(self):
        res = self.rpc("models.presets")
        self.assertTrue(res["ok"], res)
        presets = res["result"]["presets"]
        keys = {p["key"] for p in presets}
        self.assertEqual(keys, {
            "openai", "deepseek", "minimax-cn", "minimax-intl",
            "dashscope-bailian", "moonshot", "zhipu", "siliconflow",
            # 聚合商，含 stealth 匿名模型（2026-09-25 加 Space Bunny Alpha 时补的）
            "openrouter",
            "ollama-local", "custom"})
        for p in presets:
            self.assertEqual(
                set(p.keys()),
                {"key", "label", "baseUrl", "keyEnvHint", "needsKey", "notes",
                 "contextWindow", "thinkingSupported",
                 # depth capability (contract section 6): hard levels from the
                 # vendor, or the soft hint when the vendor has no parameter.
                 "thinkingDepthLevels", "softThinkingDepth",
                 # 这条线路吃不吃思考开关：厂商映射表里没有它，或该条目自己声明
                 # thinkingSupported=false（实测发了会 400），界面整行不显示思考强度。
                 "thinkingAdapted",
                 # 这条模型能不能关思考（目录 thinking.off 为 null 的是 false）：
                 # glm-5.3 厂商侧始终思考，界面不给「不思考」，发了就是 400。
                 "thinkingCanDisable",
                 # 预置模型目录（窗口 / 价格 / 擅长）：给「添加模型」的下拉用，
                 # 不靠 GET /models 现拉（现拉只有模型名，没有窗口和价格）。
                 "catalog"})
            self.assertIsInstance(p["catalog"], list, p["key"])
            for m in p["catalog"]:
                self.assertEqual(m["provider"], p["key"])
                self.assertIn(m["pricing"]["mode"],
                              ("per-token", "token-plan", "subscription", "unknown"))
            self.assertIsInstance(p["needsKey"], bool)
            self.assertIsInstance(p["thinkingSupported"], bool)
            self.assertIsInstance(p["contextWindow"], int)
            self.assertNotIsInstance(p["contextWindow"], bool)
            self.assertIsInstance(p["thinkingDepthLevels"], list)
            self.assertIsInstance(p["softThinkingDepth"], bool)
            self.assertIsInstance(p["thinkingAdapted"], bool)
            # Preset contextWindow is a suggestion and must itself be valid
            # under the same contract range as items (1000..10000000).
            self.assertGreaterEqual(p["contextWindow"], 1000)
            self.assertLessEqual(p["contextWindow"], 10000000)
        # thinkingSupported mirrors the section-6 mapping table: adapted
        # providers True, unadapted (ollama-local / custom) False.
        by_key = {p["key"]: p for p in presets}
        for k in ("openai", "deepseek", "minimax-cn", "minimax-intl",
                  "dashscope-bailian", "moonshot", "zhipu", "siliconflow"):
            self.assertTrue(by_key[k]["thinkingSupported"], k)
        self.assertFalse(by_key["ollama-local"]["thinkingSupported"])
        self.assertFalse(by_key["custom"]["thinkingSupported"])
        # No preset prescribes model names.
        for p in presets:
            self.assertNotIn("model", p)
            self.assertNotIn("models", p)

    def test_depth_levels_only_where_vendor_has_them(self):
        """硬档位只出现在真有这个参数的厂商上；MiniMax 官方只有 adaptive/disabled。"""
        res = self.rpc("models.presets")
        by_key = {p["key"]: p for p in res["result"]["presets"]}
        self.assertEqual(by_key["openai"]["thinkingDepthLevels"],
                         ["low", "medium", "high"])
        self.assertFalse(by_key["openai"]["softThinkingDepth"])
        # MiniMax: no budget_tokens / reasoning_effort in the vendor docs, and
        # the API rejects type="enabled", so only the soft hint is honest here.
        self.assertEqual(by_key["minimax-cn"]["thinkingDepthLevels"], [])
        self.assertTrue(by_key["minimax-cn"]["softThinkingDepth"])
        # ollama-local: thinking itself is unadapted -> no hint either.
        self.assertFalse(by_key["ollama-local"]["softThinkingDepth"])


class ChatModelIdTest(TempRootCase):
    """Contract 5.7: chat modelId -> MODEL_NOT_FOUND / MODEL_KEY_MISSING."""

    def setUp(self):
        super().setUp()
        self.write_config(NEW_CONFIG)

    class _NoThread:
        """Stand-in for threading.Thread: swallows the background spawn."""

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    def _chat(self, model_id=None, extra=None):
        params = {"protocolVersion": 1,
                  "messages": [{"role": "user", "content": "hi"}]}
        if model_id is not None:
            params["modelId"] = model_id
        params.update(extra or {})
        # handle_request() starts the chat on a daemon thread, but these tests
        # drive the worker inline (_run_chat_sync) and assert on what it saw.
        # A leftover background copy from an earlier test would race them:
        # duplicate events and stale captures (0.4% -> 40% flake depending on
        # timing). Suppress the spawn; the synchronous reply is still asserted.
        real_thread = sidecar.threading.Thread
        sidecar.threading.Thread = self._NoThread
        try:
            return sidecar.handle_request(
                {"type": "req", "id": "c1", "method": "chat", "params": params})
        finally:
            sidecar.threading.Thread = real_thread

    def _run_chat_sync(self, params) -> list:
        """Run the chat worker inline and collect chat.done/chat.error events."""
        events = []
        orig_emit = sidecar._emit

        def fake_emit(event, data):
            # Only the inline run below is under test. handle_request() has
            # already spawned the same chat on a daemon thread, and its emit
            # can still land inside this window (flaky duplicate events).
            if threading.current_thread() is not threading.main_thread():
                return
            if event in ("chat.error", "chat.done"):
                events.append((event, dict(data)))

        sidecar._emit = fake_emit
        try:
            sidecar._run_chat("c1", dict(params))
        finally:
            sidecar._emit = orig_emit
        return events

    def test_unknown_model_id_sync_error(self):
        res = self._chat(model_id="ghost")
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["error"]["code"], "MODEL_NOT_FOUND")

    def test_known_model_without_key_fails_before_loop(self):
        # ds-chat exists but DEEPSEEK_API_KEY is unset -> MODEL_KEY_MISSING.
        # The sync response is accepted; the error arrives as chat.error
        # before any LLM round is attempted.
        res = self._chat(model_id="ds-chat")
        self.assertTrue(res["ok"], res)
        events = self._run_chat_sync({
            "messages": [{"role": "user", "content": "hi"}],
            "modelId": "ds-chat", "workspaceRoot": self.root})
        errs = [d for kind, d in events if kind == "chat.error"]
        self.assertEqual(len(errs), 1, events)
        self.assertEqual(errs[0]["code"], "MODEL_KEY_MISSING")
        self.assertIn("DEEPSEEK_API_KEY", errs[0]["message"])
        # The message must not carry any key value (none set anyway).
        self.assertNotIn("Bearer", errs[0]["message"])

    def test_model_with_key_resolves(self):
        os.environ["DEEPSEEK_API_KEY"] = "sk-deep"
        info = MC.resolve_model("ds-chat", root=self.root)
        self.assertEqual(info["id"], "ds-chat")
        self.assertEqual(info["model"], "deepseek-chat")
        self.assertEqual(info["api_key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(info["api_key"], "sk-deep")
        self.assertEqual(info["base_url"], "https://api.deepseek.com/v1")

    def test_default_resolution_uses_default_id(self):
        os.environ["MINIMAX_CN_API_KEY"] = "sk-mm"
        info = MC.resolve_model(None, root=self.root)
        self.assertEqual(info["id"], "mm-m3")
        self.assertEqual(info["model"], "MiniMax-M3")

    def test_default_missing_key(self):
        # default mm-m3 has no key in env -> MODEL_KEY_MISSING with var name.
        with self.assertRaises(MC.ModelConfigError) as ctx:
            MC.resolve_model(None, root=self.root)
        self.assertEqual(ctx.exception.code, "MODEL_KEY_MISSING")
        self.assertIn("MINIMAX_CN_API_KEY", ctx.exception.message)

    def test_resolve_unknown_model_id(self):
        with self.assertRaises(MC.ModelConfigError) as ctx:
            MC.resolve_model("ghost", root=self.root)
        self.assertEqual(ctx.exception.code, "MODEL_NOT_FOUND")

    def _collect_chat_errors(self, req_id, timeout=5.0):
        """Run the chat worker synchronously and collect chat.error events."""
        import threading

        events = []

        orig_emit = sidecar._emit
        box = {"done": threading.Event()}

        def fake_emit(event, data):
            if event == "chat.error" and data.get("id") == req_id:
                events.append(data)
            if event in ("chat.done", "chat.error"):
                box["done"].set()

        sidecar._emit = fake_emit
        try:
            # Drive the worker body inline instead of a real thread: the
            # request handler already started one; wait for it to finish.
            deadline = timeout
            while not box["done"].is_set() and deadline > 0:
                import time
                time.sleep(0.02)
                deadline -= 0.02
        finally:
            sidecar._emit = orig_emit
        return events

    def test_image_gate_uses_resolved_modalities(self):
        """modelId without image in inputModalities rejects image input."""
        os.environ["DEEPSEEK_API_KEY"] = "sk-deep"
        os.environ["MINIMAX_CN_API_KEY"] = "sk-mm"
        # ds-chat declares no inputModalities -> image gate must reject it.
        events = self._run_chat_sync({
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
                {"type": "text", "text": "what is this"}]}],
            "modelId": "ds-chat", "workspaceRoot": self.root})
        errs = [d for kind, d in events if kind == "chat.error"]
        self.assertEqual(len(errs), 1, events)
        self.assertEqual(errs[0]["code"], "MODEL_NO_IMAGE_INPUT")
        # mm-m3 declares image -> the gate passes (loop then fails on the
        # injected opener being absent, which we do not drive here).
        info = MC.resolve_model("mm-m3", root=self.root)
        self.assertIn("image", {str(m).lower() for m in info["inputModalities"]})

    def test_resolved_model_drives_llm_payload(self):
        """The resolved modelId's name/baseUrl reach the LLM stream call."""
        os.environ["DEEPSEEK_API_KEY"] = "sk-deep"
        captured = {}

        def fake_stream(cfg, messages, **kw):
            captured["model"] = cfg.model
            captured["base_url"] = cfg.base_url
            captured["api_key"] = cfg.api_key
            return iter([{"type": "delta", "text": "ok"},
                         {"type": "done", "finishReason": "stop", "usage": {},
                          "toolCalls": []}])

        import agent_loop as AL
        orig_deps = sidecar.agent_loop.default_deps

        def patched_deps(**overrides):
            return orig_deps(llm_stream=fake_stream, **overrides)

        sidecar.agent_loop.default_deps = patched_deps
        try:
            events = self._run_chat_sync({
                "messages": [{"role": "user", "content": "hi"}],
                "modelId": "ds-chat", "workspaceRoot": self.root})
        finally:
            sidecar.agent_loop.default_deps = orig_deps
        self.assertEqual(captured["model"], "deepseek-chat")
        self.assertEqual(captured["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(captured["api_key"], "sk-deep")
        dones = [d for kind, d in events if kind == "chat.done"]
        self.assertEqual(len(dones), 1, events)

    def test_legacy_model_name_param_still_overrides(self):
        """Old `model` param overrides only the model NAME on top of modelId."""
        os.environ["DEEPSEEK_API_KEY"] = "sk-deep"
        captured = {}

        def fake_stream(cfg, messages, **kw):
            captured["model"] = cfg.model
            captured["base_url"] = cfg.base_url
            return iter([{"type": "done", "finishReason": "stop", "usage": {},
                          "toolCalls": []}])

        orig_deps = sidecar.agent_loop.default_deps

        def patched_deps(**overrides):
            return orig_deps(llm_stream=fake_stream, **overrides)

        sidecar.agent_loop.default_deps = patched_deps
        try:
            events = self._run_chat_sync({
                "messages": [{"role": "user", "content": "hi"}],
                "modelId": "ds-chat", "model": "deepseek-reasoner",
                "workspaceRoot": self.root})
        finally:
            sidecar.agent_loop.default_deps = orig_deps
        self.assertEqual(captured["model"], "deepseek-reasoner")
        self.assertEqual(captured["base_url"], "https://api.deepseek.com/v1")
        dones = [d for kind, d in events if kind == "chat.done"]
        self.assertEqual(len(dones), 1, events)


class GenerationParamsTest(TempRootCase):
    """Contract 5.8: the four v2 generation fields.

    Old configs missing the fields load with defaults; upsert keeps stored
    values on omitted fields and resets on explicit null; out-of-range and
    wrong-typed values raise MODEL_INVALID.
    """

    # A v1-style item (no generation fields at all).
    V1_ITEMS = [
        {"id": "mm-m3", "label": "MiniMax M3", "provider": "minimax-cn",
         "baseUrl": "https://api.minimaxi.com/v1", "model": "MiniMax-M3",
         "apiKeyEnv": "MINIMAX_CN_API_KEY", "maxTokens": 4096,
         "timeoutSeconds": 180},
        {"id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
         "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
         "apiKeyEnv": "DEEPSEEK_API_KEY", "maxTokens": 8192,
         "timeoutSeconds": 60},
    ]

    def _write_v1(self) -> None:
        self.write_config({
            "model": LEGACY_CONFIG["model"],
            "models": {"defaultId": "mm-m3", "items": self.V1_ITEMS},
            "capabilities": LEGACY_CONFIG["capabilities"],
        })

    def _item(self, cfg: dict, item_id: str) -> dict:
        return next(it for it in cfg["models"]["items"] if it["id"] == item_id)

    def test_old_config_loads_with_defaults(self):
        self._write_v1()
        res = self.rpc("models.list")
        self.assertTrue(res["ok"], res)
        by_id = {it["id"]: it for it in res["result"]["items"]}
        for mid in ("mm-m3", "ds-chat"):
            self.assertEqual(by_id[mid]["contextWindow"], 128000, mid)
            self.assertIsNone(by_id[mid]["thinking"], mid)
            self.assertIsNone(by_id[mid]["temperature"], mid)
            self.assertIsNone(by_id[mid]["topP"], mid)

    def test_legacy_section_synthesis_gets_defaults(self):
        self.write_config(LEGACY_CONFIG)
        res = self.rpc("models.list")
        item = res["result"]["items"][0]
        self.assertEqual(item["contextWindow"], 128000)
        self.assertIsNone(item["thinking"])
        self.assertIsNone(item["temperature"])
        self.assertIsNone(item["topP"])

    def test_upsert_create_defaults_and_explicit_values(self):
        self._write_v1()
        # New entry without the four fields -> defaults.
        res = self.rpc("models.upsert", {
            "id": "kimi", "label": "Kimi", "provider": "moonshot",
            "baseUrl": "https://api.moonshot.cn/v1", "model": "kimi-latest",
            "apiKeyEnv": "MOONSHOT_API_KEY"})
        self.assertTrue(res["ok"], res)
        item = self._item(self.read_config(), "kimi")
        self.assertEqual(item["contextWindow"], 128000)
        self.assertIsNone(item["thinking"])
        self.assertIsNone(item["temperature"])
        self.assertIsNone(item["topP"])
        # New entry with explicit values -> persisted verbatim.
        res = self.rpc("models.upsert", {
            "id": "kimi2", "label": "Kimi 2", "provider": "moonshot",
            "baseUrl": "https://api.moonshot.cn/v1", "model": "kimi-latest",
            "apiKeyEnv": "MOONSHOT_API_KEY",
            "contextWindow": 256000, "thinking": True,
            "temperature": 0.7, "topP": 0.9})
        self.assertTrue(res["ok"], res)
        item = self._item(self.read_config(), "kimi2")
        self.assertEqual(item["contextWindow"], 256000)
        self.assertIs(item["thinking"], True)
        self.assertEqual(item["temperature"], 0.7)
        self.assertEqual(item["topP"], 0.9)

    def test_upsert_update_omitted_fields_keep_stored_values(self):
        self._write_v1()
        self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "contextWindow": 256000, "thinking": True,
            "temperature": 0.5, "topP": 0.8})
        # Update touching none of the four fields.
        res = self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek V3", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-v3",
            "apiKeyEnv": "DEEPSEEK_API_KEY"})
        self.assertTrue(res["ok"], res)
        item = self._item(self.read_config(), "ds-chat")
        self.assertEqual(item["contextWindow"], 256000)
        self.assertIs(item["thinking"], True)
        self.assertEqual(item["temperature"], 0.5)
        self.assertEqual(item["topP"], 0.8)

    def test_upsert_update_partial_change_keeps_the_rest(self):
        self._write_v1()
        self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "contextWindow": 256000, "thinking": True,
            "temperature": 0.5, "topP": 0.8})
        # Only change temperature; the other three must survive.
        res = self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY", "temperature": 1.5})
        self.assertTrue(res["ok"], res)
        item = self._item(self.read_config(), "ds-chat")
        self.assertEqual(item["contextWindow"], 256000)
        self.assertIs(item["thinking"], True)
        self.assertEqual(item["temperature"], 1.5)
        self.assertEqual(item["topP"], 0.8)

    def test_upsert_explicit_null_resets_to_defaults(self):
        self._write_v1()
        self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "contextWindow": 256000, "thinking": False,
            "temperature": 0.5, "topP": 0.8})
        # Explicit null on all four -> all back to defaults.
        res = self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "contextWindow": None, "thinking": None,
            "temperature": None, "topP": None})
        self.assertTrue(res["ok"], res)
        item = self._item(self.read_config(), "ds-chat")
        self.assertEqual(item["contextWindow"], 128000)
        self.assertIsNone(item["thinking"])
        self.assertIsNone(item["temperature"])
        self.assertIsNone(item["topP"])

    def test_explicit_null_on_one_field_only_resets_that_field(self):
        self._write_v1()
        self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "contextWindow": 256000, "thinking": True,
            "temperature": 0.5, "topP": 0.8})
        res = self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY", "thinking": None})
        self.assertTrue(res["ok"], res)
        item = self._item(self.read_config(), "ds-chat")
        self.assertIsNone(item["thinking"])
        self.assertEqual(item["contextWindow"], 256000)
        self.assertEqual(item["temperature"], 0.5)
        self.assertEqual(item["topP"], 0.8)

    def test_thinking_tri_state_all_accepted(self):
        self._write_v1()
        for value in (True, False, None):
            res = self.rpc("models.upsert", {
                "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
                "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                "apiKeyEnv": "DEEPSEEK_API_KEY", "thinking": value})
            self.assertTrue(res["ok"], (value, res))

    def _invalid_field_cases(self) -> list[tuple[dict, str]]:
        base = {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY"}
        return [
            ({**base, "contextWindow": 999}, "contextWindow"),      # below min
            ({**base, "contextWindow": 10000001}, "contextWindow"),  # above max
            ({**base, "contextWindow": 1.5}, "contextWindow"),       # float
            ({**base, "contextWindow": True}, "contextWindow"),      # bool
            ({**base, "contextWindow": "128000"}, "contextWindow"),  # string
            ({**base, "thinking": "true"}, "thinking"),              # string
            ({**base, "thinking": 1}, "thinking"),                   # int
            ({**base, "temperature": 2.01}, "temperature"),          # above max
            ({**base, "temperature": -0.01}, "temperature"),         # below min
            ({**base, "temperature": True}, "temperature"),          # bool
            ({**base, "temperature": "0.7"}, "temperature"),         # string
            ({**base, "topP": 1.01}, "topP"),                        # above max
            ({**base, "topP": -0.01}, "topP"),                        # below min
            ({**base, "topP": False}, "topP"),                        # bool
            ({**base, "topP": "0.9"}, "topP"),                        # string
        ]

    def test_out_of_range_and_wrong_types_rejected(self):
        self._write_v1()
        for params, field in self._invalid_field_cases():
            res = self.rpc("models.upsert", params)
            self.assertFalse(res["ok"], (field, params.get(field), res))
            self.assertEqual(res["error"]["code"], "MODEL_INVALID",
                             (field, res))
            self.assertIn(field, res["error"]["message"], (field, res))
            # A rejected write must not leave a half-updated config.
            item = self._item(self.read_config(), "ds-chat")
            self.assertEqual(item["model"], "deepseek-chat", field)

    def test_boundary_values_accepted(self):
        self._write_v1()
        res = self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "contextWindow": 10000000, "temperature": 0, "topP": 0})
        self.assertTrue(res["ok"], res)
        self.rpc("models.upsert", {
            "id": "mm-m3", "label": "MiniMax M3", "provider": "minimax-cn",
            "baseUrl": "https://api.minimaxi.com/v1", "model": "MiniMax-M3",
            "apiKeyEnv": "MINIMAX_CN_API_KEY",
            "contextWindow": 1000, "temperature": 2, "topP": 1})
        item = self._item(self.read_config(), "ds-chat")
        self.assertEqual(item["contextWindow"], 10000000)
        self.assertEqual(item["temperature"], 0)
        self.assertEqual(item["topP"], 0)
        item = self._item(self.read_config(), "mm-m3")
        self.assertEqual(item["contextWindow"], 1000)
        self.assertEqual(item["temperature"], 2)
        self.assertEqual(item["topP"], 1)

    def test_create_with_invalid_generation_fields_rejected(self):
        self._write_v1()
        res = self.rpc("models.upsert", {
            "id": "new-bad", "label": "Bad", "provider": "deepseek",
            "baseUrl": "https://x/v1", "model": "m", "apiKeyEnv": "X_API_KEY",
            "temperature": 3})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "MODEL_INVALID")
        self.assertIn("temperature", res["error"]["message"])
        cfg = self.read_config()
        self.assertNotIn("new-bad", [it["id"] for it in cfg["models"]["items"]])

    def test_integer_temperature_and_topp_accepted(self):
        """JSON ints are numbers too; 0/1/2 are in range."""
        self._write_v1()
        res = self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "temperature": 1, "topP": 1})
        self.assertTrue(res["ok"], res)
        item = self._item(self.read_config(), "ds-chat")
        self.assertEqual(item["temperature"], 1)
        self.assertEqual(item["topP"], 1)

    def test_resolve_model_carries_generation_fields(self):
        self._write_v1()
        os.environ["DEEPSEEK_API_KEY"] = "sk-deep"
        info = MC.resolve_model("ds-chat", root=self.root)
        self.assertEqual(info["contextWindow"], 128000)
        self.assertIsNone(info["thinking"])
        self.assertIsNone(info["temperature"])
        self.assertIsNone(info["topP"])
        self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "contextWindow": 256000, "thinking": False,
            "temperature": 0.3, "topP": 0.95})
        info = MC.resolve_model("ds-chat", root=self.root)
        self.assertEqual(info["contextWindow"], 256000)
        self.assertIs(info["thinking"], False)
        self.assertEqual(info["temperature"], 0.3)
        self.assertEqual(info["topP"], 0.95)

    def test_saved_config_round_trips_fields(self):
        self._write_v1()
        self.rpc("models.upsert", {
            "id": "ds-chat", "label": "DeepSeek Chat", "provider": "deepseek",
            "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "apiKeyEnv": "DEEPSEEK_API_KEY",
            "contextWindow": 256000, "thinking": True,
            "temperature": 0.7, "topP": 0.9})
        # Fields survive a reload (models.list re-reads config.json).
        res = self.rpc("models.list")
        by_id = {it["id"]: it for it in res["result"]["items"]}
        self.assertEqual(by_id["ds-chat"]["contextWindow"], 256000)
        self.assertIs(by_id["ds-chat"]["thinking"], True)
        self.assertEqual(by_id["ds-chat"]["temperature"], 0.7)
        self.assertEqual(by_id["ds-chat"]["topP"], 0.9)
        # Other sections still preserved.
        self.assertEqual(self.read_config()["capabilities"],
                         LEGACY_CONFIG["capabilities"])

    def test_upsert_null_on_new_entry_means_default(self):
        self._write_v1()
        res = self.rpc("models.upsert", {
            "id": "kimi", "label": "Kimi", "provider": "moonshot",
            "baseUrl": "https://api.moonshot.cn/v1", "model": "kimi-latest",
            "apiKeyEnv": "MOONSHOT_API_KEY",
            "contextWindow": None, "thinking": None,
            "temperature": None, "topP": None})
        self.assertTrue(res["ok"], res)
        item = self._item(self.read_config(), "kimi")
        self.assertEqual(item["contextWindow"], 128000)
        self.assertIsNone(item["thinking"])
        self.assertIsNone(item["temperature"])
        self.assertIsNone(item["topP"])


if __name__ == "__main__":
    unittest.main()


class SaveTargetTest(TempRootCase):
    """Regressions found live on a real machine (not reproducible in unit tests).

    Both were invisible in the unit tests because setUp always pre-created
    config.json: on a real box without one, a save wrote *the example file*
    and the freshly added model stole the global default.
    """

    def write_example(self, cfg: dict) -> str:
        path = os.path.join(self.root, "config.example.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return path

    def test_save_creates_config_json_and_leaves_example_untouched(self):
        example = self.write_example(LEGACY_CONFIG)
        with open(example, encoding="utf-8") as f:
            before = f.read()
        res = self.rpc("models.upsert", {
            "label": "Kimi", "provider": "moonshot",
            "baseUrl": "https://api.moonshot.cn/v1", "model": "kimi-latest",
            "apiKeyEnv": "MOONSHOT_API_KEY"})
        self.assertTrue(res["ok"], res)
        self.assertTrue(os.path.exists(os.path.join(self.root, "config.json")),
                        "save must create config.json, not edit the template")
        with open(example, encoding="utf-8") as f:
            self.assertEqual(f.read(), before, "config.example.json was rewritten")
        listed = self.rpc("models.list")["result"]
        self.assertIn("kimi", [i["id"] for i in listed["items"]])
        self.assertEqual(listed["defaultId"], "legacy")

    def test_adding_a_model_keeps_the_current_default(self):
        self.write_config(LEGACY_CONFIG)
        self.assertEqual(self.rpc("models.list")["result"]["defaultId"], "legacy")
        res = self.rpc("models.upsert", {
            "label": "Kimi", "provider": "moonshot",
            "baseUrl": "https://api.moonshot.cn/v1", "model": "kimi-latest",
            "apiKeyEnv": "MOONSHOT_API_KEY"})
        self.assertTrue(res["ok"], res)
        after = self.rpc("models.list")["result"]
        self.assertEqual(after["defaultId"], "legacy",
                         "adding a model must not steal the global default")
        self.assertIn("kimi", [i["id"] for i in after["items"]])
