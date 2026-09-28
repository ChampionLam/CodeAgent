"""image_generate 工具（providers/gen_tool.py）单元测试。

跑法：
    cd <repo> && python3 -m unittest tests.test_gen_tool -v

硬约束（照契约第 6 节）：
  * 不打真网络：provider 全是桩，opener 一次都不用。
  * 不写项目目录：临时文件一律 tempfile。
  * 不打印密钥：只断言「取到/没取到」，不输出值。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import appconfig  # noqa: E402
import gen_provider  # noqa: E402
import gen_registry  # noqa: E402
import modelconfig  # noqa: E402
import permissions  # noqa: E402
import tools  # noqa: E402
import providers.gen_tool as gen_tool  # noqa: E402
from gen_provider import GenError, GenResult, fail  # noqa: E402


class _StubProvider(gen_provider.GenProvider):
    """不碰网络的假 provider：记下调用参数，落一个假 PNG 字节。"""

    name = "stub"
    kind = "image"
    api_key_env = "STUB_IMAGE_KEY"

    def __init__(self, **kw):
        kw.setdefault("api_key", "stub-key")
        super().__init__(**kw)
        self.seen: dict = {}
        self.calls = 0

    def models(self) -> dict:
        return {"stub-1": {"display": "Stub", "default": True}}

    def generate(self, *, prompt, model, out_dir, aspect_ratio=None,
                 references=(), params=None, is_cancelled=lambda: False) -> GenResult:
        self.calls += 1
        self.seen = {"prompt": prompt, "model": model, "out_dir": out_dir,
                     "aspect_ratio": aspect_ratio, "references": list(references)}
        path = gen_provider.save_bytes_to(b"\x89PNG\r\n\x1a\nFAKE", out_dir, suffix=".png")
        return GenResult(ok=True, files=(path,),
                         meta={"provider": self.name, "model": model or "stub-1",
                               "prompt": prompt, "aspect_ratio": aspect_ratio or "1:1"})


class _FailingProvider(_StubProvider):
    name = "failing"

    def __init__(self, code="QUOTA", **kw):
        super().__init__(**kw)
        self.code = code

    def generate(self, **kw) -> GenResult:
        self.calls += 1
        return fail(self.code, "桩：故意失败")


class _CrashingProvider(_StubProvider):
    name = "crashing"

    def generate(self, **kw) -> GenResult:
        raise RuntimeError("桩：provider 内部崩溃")


class GenToolBase(unittest.TestCase):
    def setUp(self):
        gen_registry._reset_for_tests()
        self.tmp = tempfile.mkdtemp(prefix="gentool_")
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.ws, exist_ok=True)

    def tearDown(self):
        gen_registry._reset_for_tests()

    def _configure(self, provider_name, model=None):
        seg = {"provider": provider_name}
        if model:
            seg["model"] = model
        return mock.patch.object(appconfig, "capability",
                                 side_effect=lambda name, default=None, **kw:
                                 seg if name == "image_gen" else default)

    def _run(self, args, provider_name="stub"):
        with self._configure(provider_name):
            return tools.execute("image_generate", args, workspace_root=self.ws)


class ExposureTest(GenToolBase):
    def test_tool_is_registered_with_expected_schema(self):
        names = [t["function"]["name"] for t in tools.schemas()]
        self.assertIn("image_generate", names)
        schema = [t for t in tools.schemas() if t["function"]["name"] == "image_generate"][0]
        self.assertEqual(schema["function"]["parameters"]["required"], ["prompt"])
        self.assertIn("aspect_ratio", schema["function"]["parameters"]["properties"])
        self.assertIn("references", schema["function"]["parameters"]["properties"])

    def test_image_generate_never_asks_for_approval(self):
        """生图固定落在基准目录里，不需要审批（旧的 L1 分级已取消）。"""
        import guard
        self.assertFalse(guard.judge("image_generate", {"prompt": "a cat"}).requires_approval)

    def test_does_not_import_permissions(self):
        """架构边界：工具层不许自己裁决权限（ast 断言，照 tools.py 第 9 节）。"""
        import ast
        with open(gen_tool.__file__, encoding="utf-8") as fh:
            src = fh.read()
        mods = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        self.assertNotIn("permissions", mods)


class ArgumentTest(GenToolBase):
    def test_missing_prompt(self):
        r = self._run({})
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, "BAD_REQUEST")

    def test_blank_prompt(self):
        r = self._run({"prompt": "   "})
        self.assertEqual(r.error_code, "BAD_REQUEST")

    def test_prompt_not_string(self):
        r = self._run({"prompt": 123})
        self.assertEqual(r.error_code, "BAD_REQUEST")

    def test_references_must_be_list_or_string(self):
        r = self._run({"prompt": "x", "references": {"a": 1}})
        self.assertEqual(r.error_code, "BAD_REQUEST")

    def test_references_string_is_coerced(self):
        stub = _StubProvider()
        gen_registry.register_provider(stub)
        r = self._run({"prompt": "x", "references": "https://example.com/a.png"})
        self.assertTrue(r.ok, r.error_message)
        self.assertEqual(stub.seen["references"], ["https://example.com/a.png"])

    def test_model_must_be_string(self):
        r = self._run({"prompt": "x", "model": 7})
        self.assertEqual(r.error_code, "BAD_REQUEST")


class RoutingTest(GenToolBase):
    def test_unconfigured_provider_reports_not_configured(self):
        r = self._run({"prompt": "一只猫"}, provider_name="nope")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, gen_provider.NOT_CONFIGURED)
        self.assertIn("image_gen", r.content)

    def test_registered_names_listed_when_missing(self):
        gen_registry.register_provider(_StubProvider())
        r = self._run({"prompt": "一只猫"}, provider_name="nope")
        self.assertIn("stub", r.content)

    def test_success_writes_into_workspace_generated(self):
        stub = _StubProvider()
        gen_registry.register_provider(stub)
        r = self._run({"prompt": "一只在深圳骑车的水獭"})
        self.assertTrue(r.ok, r.error_message)
        self.assertEqual(stub.calls, 1)
        files = r.data["files"]
        self.assertEqual(len(files), 1)
        self.assertTrue(os.path.isfile(files[0]))
        self.assertEqual(os.path.dirname(files[0]),
                         os.path.join(os.path.abspath(self.ws), gen_tool.GEN_SUBDIR))
        self.assertIn(files[0], r.content)
        self.assertEqual(r.data["meta"]["model"], "stub-1")

    def test_out_dir_relative_to_given_workspace(self):
        stub = _StubProvider()
        gen_registry.register_provider(stub)
        other = os.path.join(self.tmp, "ws2")
        os.makedirs(other, exist_ok=True)
        with self._configure("stub"):
            r = tools.execute("image_generate", {"prompt": "x"}, workspace_root=other)
        self.assertTrue(r.ok, r.error_message)
        self.assertTrue(os.path.abspath(r.data["files"][0]).startswith(os.path.abspath(other)))

    def test_config_model_and_aspect_passed_through(self):
        stub = _StubProvider()
        gen_registry.register_provider(stub)
        with self._configure("stub", model="stub-1"):
            r = tools.execute("image_generate",
                              {"prompt": "x", "aspect_ratio": "16:9"},
                              workspace_root=self.ws)
        self.assertTrue(r.ok, r.error_message)
        self.assertEqual(stub.seen["model"], "stub-1")
        self.assertEqual(stub.seen["aspect_ratio"], "16:9")

    def test_explicit_model_overrides_config(self):
        stub = _StubProvider()
        gen_registry.register_provider(stub)
        with self._configure("stub", model="stub-1"):
            tools.execute("image_generate", {"prompt": "x", "model": "other-model"},
                          workspace_root=self.ws)
        self.assertEqual(stub.seen["model"], "other-model")


class FailureTest(GenToolBase):
    def test_provider_error_code_passthrough(self):
        gen_registry.register_provider(_FailingProvider(code="QUOTA"))
        r = self._run({"prompt": "x"}, provider_name="failing")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, "QUOTA")

    def test_provider_crash_maps_to_provider_error(self):
        gen_registry.register_provider(_CrashingProvider())
        r = self._run({"prompt": "x"}, provider_name="crashing")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, gen_provider.PROVIDER_ERROR)

    def test_success_without_files_is_error(self):
        class NoFile(_StubProvider):
            name = "nofile"

            def generate(self, **kw):
                return GenResult(ok=True, files=(), meta={})

        gen_registry.register_provider(NoFile())
        r = self._run({"prompt": "x"}, provider_name="nofile")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, gen_provider.PROVIDER_ERROR)


class KeyResolutionTest(GenToolBase):
    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {"STUB_IMAGE_KEY": "from-env"}, clear=False):
            self.assertEqual(gen_tool._env_key("STUB_IMAGE_KEY"), "from-env")

    def test_falls_back_to_env_file(self):
        env_path = os.path.join(self.tmp, ".env")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("OTHER=1\nSTUB_IMAGE_KEY=from-file\n")
        os.environ.pop("STUB_IMAGE_KEY", None)
        with mock.patch.object(modelconfig, "env_file_path", return_value=env_path):
            self.assertEqual(gen_tool._env_key("STUB_IMAGE_KEY"), "from-file")

    def test_missing_everywhere_returns_empty(self):
        os.environ.pop("STUB_IMAGE_KEY", None)
        with mock.patch.object(modelconfig, "env_file_path",
                               return_value=os.path.join(self.tmp, "nope.env")):
            self.assertEqual(gen_tool._env_key("STUB_IMAGE_KEY"), "")

    def test_ensure_registered_injects_env_key(self):
        seen = {}

        class Fake(gen_provider.GenProvider):
            name = "fake"
            kind = "image"
            api_key_env = "STUB_IMAGE_KEY"

            def __init__(self, **kw):
                super().__init__(**kw)
                seen["api_key"] = self.api_key

            def models(self):
                return {}

            def generate(self, **kw):
                raise AssertionError("not called")

        os.environ.pop("STUB_IMAGE_KEY", None)
        with mock.patch.object(gen_tool, "_import_builtins", return_value=[Fake]), \
             mock.patch.object(modelconfig, "env_file_path",
                               return_value=os.path.join(self.tmp, "none.env")):
            names = gen_tool.ensure_registered()
        self.assertEqual(names, ["fake"])
        self.assertIsNone(seen["api_key"])
        self.assertIsNotNone(gen_registry.get_provider("fake", kind="image"))

    def test_ensure_registered_survives_broken_provider(self):
        class Boom:
            name = "boom"
            kind = "image"
            api_key_env = "X"

            def __init__(self, **kw):
                raise RuntimeError("构造就炸")

        with mock.patch.object(gen_tool, "_import_builtins", return_value=[Boom]):
            names = gen_tool.ensure_registered()
        self.assertEqual(names, [])

    def test_builtin_imports_resolve(self):
        """内置 provider 类必须真能 import（名字写错就是这里红）。"""
        classes = gen_tool._import_builtins()
        names = sorted(c.name for c in classes)
        self.assertEqual(names, ["dashscope", "minimax"])


if __name__ == "__main__":
    unittest.main(verbosity=2)