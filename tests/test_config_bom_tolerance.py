"""真机发现（2026-10-14）：config.json / .env 带 UTF-8 BOM 时配置整套读不出来。

Windows 上记事本、以及 PowerShell 的 `Set-Content -Encoding UTF8` 都会写出 BOM。
侧车原先一律用 encoding="utf-8" 读，json.load 直接抛「不是合法 JSON」，于是模型配置
整套失效（新装实例界面显示「未配置」、侧车模型层永久降级），而且怎么重启都救不回来
——直到有人把 BOM 去掉。utf-8-sig 对无 BOM 的文件与 utf-8 完全等价，所以这是纯增益。
"""
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "python"))

BOM = "\ufeff"


class BomToleranceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="config-bom-")

    def _write_bom(self, name, text):
        """按 Windows 记事本的口径写：UTF-8 + BOM。"""
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(BOM.encode("utf-8") + text.encode("utf-8"))
        return path

    def test_appconfig_load_accepts_bom(self):
        import appconfig

        self._write_bom("config.json", json.dumps(
            {"model": {"baseUrl": "http://127.0.0.1:1/v1", "model": "stub-1",
                       "apiKeyEnv": "STUB_KEY"}}))
        cfg = appconfig.load(self.tmp)
        self.assertEqual(cfg["model"]["model"], "stub-1")

    def test_appconfig_section_accepts_bom(self):
        import appconfig

        self._write_bom("config.json", json.dumps({"logging": {"level": "debug"}}))
        self.assertEqual(
            (appconfig.section("logging", {}, root=self.tmp) or {}).get("level"), "debug")

    def test_env_file_accepts_bom(self):
        import appconfig

        self._write_bom(".env", "STUB_KEY=abc\n# comment\nOTHER=1\n")
        env = appconfig.read_env_file(os.path.join(self.tmp, ".env"))
        self.assertEqual(env.get("STUB_KEY"), "abc", "BOM 不能粘到第一个键名上")
        self.assertEqual(env.get("OTHER"), "1")

    def test_llm_load_config_accepts_bom(self):
        import llm

        self._write_bom("config.json", json.dumps(
            {"model": {"baseUrl": "http://127.0.0.1:1/v1", "model": "stub-1",
                       "apiKeyEnv": "STUB_KEY"}}))
        # load_config 连密钥一起解析，所以这里得让环境变量存在（密钥只从环境变量读）。
        os.environ.setdefault("STUB_KEY", "unit-test-key")
        try:
            cfg = llm.load_config(self.tmp)
        finally:
            os.environ.pop("STUB_KEY", None)
        self.assertEqual(cfg.model, "stub-1")


if __name__ == "__main__":
    unittest.main()