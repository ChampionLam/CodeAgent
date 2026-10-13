"""厂商中立化回归（2026-10-12，用户点名两件事：未配模型时不报厂商名、不显示 legacy）。

覆盖：
  1. 空白模板（config.example.json 形状：单 model 段、字段全空）：
     a. 聊天报错文案不含任何厂商名 / env 变量名 / 内部字段名，只有可执行引导；
     b. models.list 合成不出 legacy 条目（items 为空）。
  2. 旧形状配置（有真实 baseUrl）仍可用：resolve_model 正常出结果，provider
     按 baseUrl 主机名解析（不落回 legacy），思考开关不再静默失效。
  3. models.items 条目 baseUrl/model 为空时：MODEL_KEY_MISSING 文案同样中立。
  4. 主机名认不出时 provider 读出为空（不是 legacy），界面渲染成「未配置」。

零网络：临时目录 + 纯函数。
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
import sidecar  # noqa: E402

# config.example.json 的原样形状（新装实例的兜底路径就是这个）。
BLANK_TEMPLATE = {
    "model": {
        "baseUrl": "",
        "model": "",
        "apiKeyEnv": "",
        "maxTokens": 4096,
        "timeoutSeconds": 180,
    },
    "capabilities": {"workspaceRoot": "", "models": {}},
}

# 旧形状但 baseUrl 真实：legacy 合成路径必须仍然可用。
OLD_SHAPE_CONFIG = {
    "model": {
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "apiKeyEnv": "DASHSCOPE_API_KEY",
        "maxTokens": 4096,
        "timeoutSeconds": 180,
    },
    "capabilities": {"workspaceRoot": "", "models": {}},
}

# 旧形状 + 认不出的主机名：provider 读出必须是空，不是 legacy。
UNKNOWN_HOST_CONFIG = {
    "model": {
        "baseUrl": "https://llm.internal/v1",
        "model": "m",
        "apiKeyEnv": "TEST_MODEL_KEY",
        "maxTokens": 4096,
        "timeoutSeconds": 180,
    },
    "capabilities": {"workspaceRoot": "", "models": {}},
}

# 新形状条目本身没配（baseUrl/model 空）但 env 名已填。
EMPTY_ITEM_CONFIG = {
    "models": {
        "defaultId": "blank",
        "items": [{
            "id": "blank", "label": "Blank", "provider": "",
            "baseUrl": "", "model": "", "apiKeyEnv": "TEST_MODEL_KEY",
            "maxTokens": 4096, "timeoutSeconds": 180,
        }],
    },
    "capabilities": {"workspaceRoot": "", "models": {}},
}


class _VendorNeutralCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="desk-agent-neutral-")
        self._saved_env = dict(os.environ)
        for var in ("DASHSCOPE_API_KEY", "TEST_MODEL_KEY"):
            os.environ.pop(var, None)
        self._saved_override = MC._MODELS_ROOT_OVERRIDE
        MC._MODELS_ROOT_OVERRIDE = self.root
        sidecar._CFG = None

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_env)
        MC._MODELS_ROOT_OVERRIDE = self._saved_override
        sidecar._CFG = None

    def write_config(self, cfg: dict) -> None:
        with open(os.path.join(self.root, "config.json"), "w",
                  encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def rpc(self, method: str, params: dict | None = None):
        return sidecar.handle_request({
            "type": "req", "id": "t1", "method": method,
            "params": {"protocolVersion": 1, **(params or {})},
        })

    def _chat_error_message(self) -> str:
        """无 modelId 聊天：worker 前置解析失败给出的 chat.error 原文。"""
        events: list[tuple[str, dict]] = []
        orig_emit = sidecar._emit

        def fake_emit(event, data):
            import threading
            if threading.current_thread() is threading.main_thread():
                if event in ("chat.error", "chat.done"):
                    events.append((event, dict(data)))

        sidecar._emit = fake_emit
        try:
            sidecar._run_chat("c1", {
                "protocolVersion": 1,
                "messages": [{"role": "user", "content": "hi"}],
                "workspaceRoot": self.root,
            })
        finally:
            sidecar._emit = orig_emit
        errs = [d for kind, d in events if kind == "chat.error"]
        self.assertEqual(len(errs), 1, events)
        return errs[0]["message"]


class BlankTemplateNeutral(_VendorNeutralCase):
    """空白模板：报错/列表都不得出现厂商名、env 名、legacy。"""

    VENDOR_WORDS = (
        "DASHSCOPE", "DASHSCOPE_API_KEY", "dashscope", "MINIMAX", "minimax",
        "DEEPSEEK", "deepseek", "OPENAI", "openai", "MOONSHOT", "moonshot",
        "ZHIPU", "zhipu", "apiKeyEnv", "baseUrl", "legacy",
    )

    def test_chat_error_is_neutral(self):
        self.write_config(BLANK_TEMPLATE)
        msg = self._chat_error_message()
        for word in self.VENDOR_WORDS:
            self.assertNotIn(word, msg, msg)
        self.assertIn("设置", msg)   # 可执行引导
        self.assertIn("尚未配置模型", msg)

    def test_models_list_has_no_legacy_item(self):
        self.write_config(BLANK_TEMPLATE)
        res = self.rpc("models.list")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["items"], [])
        self.assertIsNone(res["result"]["defaultId"])

    def test_resolve_model_neutral_message(self):
        # items 为空 + 显式给 modelId -> MODEL_NOT_FOUND；不给 modelId 的
        # resolve_model 在空 items 上也是 MODEL_NOT_FOUND，两者都中性。
        self.write_config(BLANK_TEMPLATE)
        with self.assertRaises(MC.ModelConfigError) as ctx:
            MC.resolve_model(None, root=self.root)
        self.assertEqual(ctx.exception.code, "MODEL_NOT_FOUND")
        self.assertIn("no models configured", ctx.exception.message)


class OldShapeStillWorks(_VendorNeutralCase):
    """旧形状（真实 baseUrl）：能聊、provider 不落回 legacy。"""

    def test_resolve_model_works_and_provider_resolved(self):
        self.write_config(OLD_SHAPE_CONFIG)
        os.environ["DASHSCOPE_API_KEY"] = "sk-test"
        info = MC.resolve_model(None, root=self.root)
        self.assertEqual(info["model"], "qwen-plus")
        # 按 baseUrl 主机名解析：思考开关（enable_thinking）不再静默失效。
        self.assertEqual(info["provider"], "dashscope-bailian")
        self.assertNotEqual(info["provider"], "legacy")

    def test_models_list_shows_resolved_provider(self):
        self.write_config(OLD_SHAPE_CONFIG)
        res = self.rpc("models.list")
        items = res["result"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["provider"], "dashscope-bailian")

    def test_unknown_host_provider_is_empty_not_legacy(self):
        self.write_config(UNKNOWN_HOST_CONFIG)
        os.environ["TEST_MODEL_KEY"] = "sk-test"
        info = MC.resolve_model(None, root=self.root)
        # 认不出主机名：空（界面渲染「未配置」），绝不是内部代号 legacy。
        self.assertEqual(info["provider"], "")

    def test_thinking_still_reaches_vendor(self):
        # 旧形状下 provider 已自愈成真实厂商 key：llm 映射表里能查到
        # （= thinking 开关生效），而不是 THINKING_UNADAPTED_PROVIDERS。
        self.write_config(OLD_SHAPE_CONFIG)
        os.environ["DASHSCOPE_API_KEY"] = "sk-test"
        info = MC.resolve_model(None, root=self.root)
        self.assertNotIn(info["provider"], llm.THINKING_UNADAPTED_PROVIDERS)
        self.assertNotIn(info["provider"], ("", "legacy", None))
        self.assertIn("enable_thinking",
                      llm.thinking_params(info["provider"], True))


class EmptyItemNeutral(_VendorNeutralCase):
    """models.items 条目未配置（baseUrl/model 空）：MODEL_KEY_MISSING 文案中立。"""

    def test_key_missing_message_neutral(self):
        self.write_config(EMPTY_ITEM_CONFIG)
        with self.assertRaises(MC.ModelConfigError) as ctx:
            MC.resolve_model(None, root=self.root)
        self.assertEqual(ctx.exception.code, "MODEL_KEY_MISSING")
        msg = ctx.exception.message
        self.assertNotIn("TEST_MODEL_KEY", msg, msg)
        self.assertIn("尚未配置模型", msg)

    def test_configured_vendor_keeps_env_name(self):
        # 已配置厂商（baseUrl/model 非空）但 key 没设：env 名保留（此时准确）。
        self.write_config(OLD_SHAPE_CONFIG)
        os.environ.pop("DASHSCOPE_API_KEY", None)
        with self.assertRaises(MC.ModelConfigError) as ctx:
            MC.resolve_model(None, root=self.root)
        self.assertEqual(ctx.exception.code, "MODEL_KEY_MISSING")
        self.assertIn("DASHSCOPE_API_KEY", ctx.exception.message)


class LlmLoadConfigNeutral(unittest.TestCase):
    """llm.load_config 直连路径（无 modelId 的兜底）：空白模板给引导，不报字段名。"""

    def test_blank_template_guides(self):
        root = tempfile.mkdtemp(prefix="desk-agent-llm-neutral-")
        path = os.path.join(root, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(BLANK_TEMPLATE, f)
        with self.assertRaises(llm.ConfigError) as ctx:
            llm.load_config(root)
        msg = str(ctx.exception)
        self.assertIn("尚未配置模型", msg)
        self.assertNotIn("model.baseUrl", msg)
        self.assertNotIn("model.apiKeyEnv", msg)

    def test_configured_but_key_missing_names_env(self):
        root = tempfile.mkdtemp(prefix="desk-agent-llm-neutral-")
        path = os.path.join(root, "config.json")
        cfg = json.loads(json.dumps(OLD_SHAPE_CONFIG))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        saved = os.environ.pop("DASHSCOPE_API_KEY", None)
        try:
            with self.assertRaises(llm.ConfigError) as ctx:
                llm.load_config(root)
            self.assertIn("DASHSCOPE_API_KEY", str(ctx.exception))
        finally:
            if saved is not None:
                os.environ["DASHSCOPE_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main()
