"""tools.py 的单元测试（契约第 9、10 节）。

跑法（两种都支持）：
    cd <repo> && python3 -m unittest tests.test_tools -v
    python3 tests/test_tools.py

硬约束：
  * 架构边界：tools.py 不许 import permissions（ast 源码断言 + 命名空间断言双保险）。
  * 不写项目目录：临时文件一律 tempfile（setUp 建 workspace，tearDown 删）。
  * run_shell 的超时/退出码语义：非 0 退出码仍带回 stdout+stderr；
    超时带回已经产生的输出。
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import tools  # noqa: E402
from tools import (  # noqa: E402
    DEFAULT_SHELL_TIMEOUT,
    MAX_READ_BYTES,
    ReadFileTool,
    ListDirTool,
    RunShellTool,
    ToolResult,
    WriteFileTool,
)

TOOLS_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python", "tools.py")


class ToolsTestBase(unittest.TestCase):
    """每个测试一套干净的注册表 + 一次性 workspace。"""

    def setUp(self) -> None:
        tools._reset_for_tests()
        tools.register_defaults()
        self.ws = tempfile.mkdtemp(prefix="desk_tools_ws_")
        self._prev_cwd = os.getcwd()

    def tearDown(self) -> None:
        # 只清注册表，不动模块级常量；workspace 是 tempfile 的，系统自己回收
        tools._reset_for_tests()
        tools.register_defaults()
        os.chdir(self._prev_cwd)     # 防御 run_shell 测试改过 cwd 的场景
        import shutil
        shutil.rmtree(self.ws, ignore_errors=True)

    # ---- helpers -------------------------------------------------------
    def write_ws(self, rel: str, content: str = "") -> str:
        full = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return full

    def run_ok(self, name: str, args: dict) -> ToolResult:
        result = tools.execute(name, args, workspace_root=self.ws)
        self.assertTrue(result.ok, "expected success, got %s: %s" % (result.error_code, result.error_message))
        return result


class TestArchitectureBoundary(ToolsTestBase):
    """契约第 10 节：tools.py 不许 import permissions（架构边界）。"""

    def test_no_permissions_import_in_source(self) -> None:
        with open(TOOLS_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split(".")[0])
        self.assertNotIn("permissions", imports, "tools.py import 了 permissions：违反第 9 节边界")
        self.assertNotIn("appconfig", imports)
        self.assertNotIn("audit", imports)

    def test_permissions_not_in_module_namespace(self) -> None:
        # 即便 permissions 被其它测试 import 进了 sys.modules，tools 自己的
        # 命名空间也不能有它
        self.assertNotIn("permissions", vars(tools))


class TestRegistry(ToolsTestBase):

    def test_defaults_registered(self) -> None:
        names = {t.name for t in tools.list_tools()}
        # 基础 4 个 + 编排层的 read_skill / list_skills / skill_manage + 硬规层的 save_rule / remove_rule + 文件工具 3 个
        self.assertEqual(names, {"read_file", "list_dir", "write_file", "run_shell", "search_conversations",
                                 "read_skill", "list_skills", "skill_manage",
                                 "save_rule", "remove_rule",
                                 "search_files", "edit_file", "delete_path",
                                 "delegate"})

    def test_get_returns_instance_or_none(self) -> None:
        self.assertIsInstance(tools.get("read_file"), ReadFileTool)
        self.assertIsInstance(tools.get("run_shell"), RunShellTool)
        self.assertIsNone(tools.get("no_such_tool"))

    def test_register_overrides_same_name(self) -> None:
        class ProbeTool(tools.Tool):
            name = "read_file"
            description = "probe override"
            parameters = {"type": "object", "properties": {}}

            def run(self, args, *, workspace_root):
                return tools.ToolResult(ok=True, content="PROBE")

        tools.register(ProbeTool())
        result = tools.execute("read_file", {"path": "whatever"}, workspace_root=self.ws)
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "PROBE")

    def test_schemas_shape(self) -> None:
        schemas = tools.schemas()
        self.assertEqual(len(schemas), 14)  # base + skills + skill_manage + hard rules + file tools + history + delegate
        for schema in schemas:
            self.assertEqual(schema["type"], "function")
            fn = schema["function"]
            self.assertEqual(sorted(fn.keys()), ["description", "name", "parameters"])
            self.assertIsInstance(fn["description"], str)
            self.assertIsInstance(fn["parameters"], dict)
            self.assertEqual(fn["parameters"].get("type"), "object")
        by_name = {s["function"]["name"] for s in schemas}
        self.assertEqual(by_name, {"save_rule", "remove_rule",
                                   "read_file", "list_dir", "write_file", "run_shell", "search_conversations",
                                   "read_skill", "list_skills", "skill_manage",
                                   "search_files", "edit_file", "delete_path",
                                   "delegate"})

    def test_schemas_sorted_stable_after_reregister(self) -> None:
        tools._reset_for_tests()
        tools.register_defaults()
        first = [s["function"]["name"] for s in tools.schemas()]
        tools.register_defaults()       # 幂等
        again = [s["function"]["name"] for s in tools.schemas()]
        self.assertEqual(first, again)

    def test_execute_unknown_tool(self) -> None:
        result = tools.execute("definitely_not_a_tool", {}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "UNKNOWN_TOOL")
        self.assertIn("definitely_not_a_tool", result.content)

    def test_register_rejects_non_tool(self) -> None:
        with self.assertRaises(TypeError):
            tools.register("not-a-tool")

    def test_list_tools_returns_copy(self) -> None:
        lst = tools.list_tools()
        lst.clear()
        self.assertEqual(len(tools.list_tools()), 14)  # base + skills + skill_manage + hard rules + file tools + history + delegate


class TestReadFile(ToolsTestBase):

    def test_reads_text_file(self) -> None:
        path = self.write_ws("notes.md", "hello desk-agent\n")
        result = self.run_ok("read_file", {"path": path})
        self.assertEqual(result.content, "hello desk-agent\n")
        self.assertEqual(result.data["bytes"], 17)
        self.assertEqual(result.data["path"], path)

    def test_relative_path_resolves_to_workspace(self) -> None:
        self.write_ws("rel.txt", "rel content")
        result = self.run_ok("read_file", {"path": "rel.txt"})
        self.assertEqual(result.content, "rel content")

    def test_not_found(self) -> None:
        result = tools.execute("read_file", {"path": os.path.join(self.ws, "missing.txt")}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")

    def test_is_a_dir(self) -> None:
        result = tools.execute("read_file", {"path": self.ws}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "IS_A_DIR")

    def test_too_large(self) -> None:
        path = self.write_ws("big.txt", "x" * 100)
        result = tools.execute("read_file", {"path": path, "max_bytes": 10}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOO_LARGE")
        self.assertIn("100", result.error_message or "")
        # 边界：正好等于 max_bytes 不算超
        ok_edge = self.run_ok("read_file", {"path": path, "max_bytes": 100})
        self.assertEqual(ok_edge.content, "x" * 100)

    def test_bad_request_missing_path(self) -> None:
        result = tools.execute("read_file", {}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_bad_request_bad_max_bytes(self) -> None:
        path = self.write_ws("small.txt", "s")
        for bad in ("abc", -1, 0):
            with self.subTest(bad=bad):
                result = tools.execute("read_file", {"path": path, "max_bytes": bad}, workspace_root=self.ws)
                self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_non_utf8_replaces_not_raises(self) -> None:
        path = os.path.join(self.ws, "bin.txt")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfeAB\xff")
        result = self.run_ok("read_file", {"path": path})
        self.assertIn("AB", result.content)          # 不抛，替换字符在
        self.assertEqual(result.data["bytes"], 5)

    def test_default_max_bytes_is_4mib(self) -> None:
        self.assertEqual(MAX_READ_BYTES, 4 * 1024 * 1024)
        # 不真造 4MiB 文件：只验证常量与语义挂钩（不造文件、不发慢路径）


class TestListDir(ToolsTestBase):

    def test_lists_default_workspace_root(self) -> None:
        self.write_ws("a.txt", "a")
        self.write_ws("dir_b/inner.txt", "b")
        result = self.run_ok("list_dir", {})
        names = [e["name"] for e in result.data["entries"]]
        self.assertEqual(names, ["dir_b", "a.txt"])   # 目录在前、名字升序
        self.assertEqual(result.content, "dir_b/\na.txt")
        by_name = {e["name"]: e for e in result.data["entries"]}
        self.assertTrue(by_name["dir_b"]["is_dir"])
        self.assertFalse(by_name["a.txt"]["is_dir"])
        self.assertEqual(by_name["a.txt"]["size"], 1)

    def test_absolute_path(self) -> None:
        result = self.run_ok("list_dir", {"path": self.ws})
        self.assertEqual(result.data["path"], self.ws)

    def test_not_found(self) -> None:
        result = tools.execute("list_dir", {"path": os.path.join(self.ws, "missing_dir")}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_FOUND")

    def test_not_a_dir(self) -> None:
        path = self.write_ws("plain.txt", "x")
        result = tools.execute("list_dir", {"path": path}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_A_DIR")

    def test_bad_request_path_type(self) -> None:
        result = tools.execute("list_dir", {"path": 123}, workspace_root=self.ws)
        self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_empty_dir(self) -> None:
        empty = os.path.join(self.ws, "empty")
        os.mkdir(empty)
        result = self.run_ok("list_dir", {"path": "empty"})
        self.assertEqual(result.content, "")
        self.assertEqual(result.data["entries"], [])

    def test_hidden_files_included(self) -> None:
        self.write_ws(".hidden", "h")
        result = self.run_ok("list_dir", {})
        self.assertIn(".hidden", result.content)


class TestWriteFile(ToolsTestBase):

    def test_writes_and_reports_bytes(self) -> None:
        target = os.path.join(self.ws, "out", "new.txt")
        result = self.run_ok("write_file", {"path": target, "content": "line1\nline2\n"})
        self.assertEqual(result.data["bytes_written"], 12)
        self.assertIn(target, result.content)
        self.assertIn("12", result.content)
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "line1\nline2\n")

    def test_creates_parent_dirs(self) -> None:
        target = os.path.join(self.ws, "a", "b", "c", "deep.txt")
        self.run_ok("write_file", {"path": target, "content": "deep"})
        self.assertTrue(os.path.isfile(target))
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "deep")

    def test_overwrites_existing_file(self) -> None:
        target = self.write_ws("exists.txt", "old")
        self.run_ok("write_file", {"path": target, "content": "new!"})
        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "new!")

    def test_relative_path_anchors_to_workspace(self) -> None:
        self.run_ok("write_file", {"path": "rel/rel.txt", "content": "rel"})
        self.assertTrue(os.path.isfile(os.path.join(self.ws, "rel", "rel.txt")))

    def test_is_a_dir(self) -> None:
        result = tools.execute("write_file", {"path": self.ws, "content": "x"}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "IS_A_DIR")

    def test_bad_request_missing_content(self) -> None:
        result = tools.execute("write_file", {"path": "x.txt"}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_bad_request_missing_path(self) -> None:
        result = tools.execute("write_file", {"content": "x"}, workspace_root=self.ws)
        self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_bad_request_content_type(self) -> None:
        result = tools.execute("write_file", {"path": "x.txt", "content": 123}, workspace_root=self.ws)
        self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_parent_is_a_plain_file_maps_to_io_error(self) -> None:
        blocker = self.write_ws("blocker", "x")
        result = tools.execute(
            "write_file",
            {"path": os.path.join(blocker, "child.txt"), "content": "y"},
            workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "IO_ERROR")

    def test_unicode_content_roundtrip(self) -> None:
        target = os.path.join(self.ws, "uni.txt")
        self.run_ok("write_file", {"path": target, "content": "中文内容 🚀"})
        result = self.run_ok("read_file", {"path": target})
        self.assertEqual(result.content, "中文内容 🚀")


class TestRunShell(ToolsTestBase):

    def test_success_returns_stdout_stderr(self) -> None:
        result = self.run_ok("run_shell", {"command": "printf OUT; printf ERR >&2"})
        self.assertEqual(result.data["stdout"], "OUT")
        self.assertEqual(result.data["stderr"], "ERR")
        self.assertEqual(result.data["returncode"], 0)
        self.assertIn("OUT", result.content)
        self.assertIn("ERR", result.content)

    def test_cwd_is_workspace_root(self) -> None:
        result = self.run_ok("run_shell", {"command": "pwd"})
        # realpath 防 /tmp → /private/tmp 之类的符号链接差异
        self.assertEqual(os.path.realpath(result.data["stdout"].strip()),
                         os.path.realpath(self.ws))

    def test_cwd_falls_back_when_workspace_empty(self) -> None:
        before = os.getcwd()
        result = tools.execute("run_shell", {"command": "pwd"}, workspace_root="")
        self.assertTrue(result.ok)
        # workspace_root 为空 → cwd 退回当前目录
        self.assertEqual(os.path.realpath(result.data["stdout"].strip()),
                         os.path.realpath(before))

    def test_nonzero_exit_still_returns_outputs(self) -> None:
        result = tools.execute("run_shell", {"command": "printf PARTIAL_OUT; printf BOOM_ERR >&2; exit 3"},
                               workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NONZERO_EXIT")
        self.assertEqual(result.data["stdout"], "PARTIAL_OUT")
        self.assertEqual(result.data["stderr"], "BOOM_ERR")
        self.assertEqual(result.data["returncode"], 3)
        # content 里模型能看到输出与退出码
        self.assertIn("PARTIAL_OUT", result.content)
        self.assertIn("BOOM_ERR", result.content)
        self.assertIn("3", result.content)

    def test_timeout_returns_partial_output(self) -> None:
        result = tools.execute(
            "run_shell",
            {"command": "printf BEFORE_TIMEOUT; sleep 5", "timeout_seconds": 1},
            workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TIMEOUT")
        # 已经产生的输出必须带回来（模型靠它恢复）
        self.assertIn("BEFORE_TIMEOUT", result.data["stdout"])
        self.assertIn("BEFORE_TIMEOUT", result.content)

    def test_timeout_partial_stderr_too(self) -> None:
        result = tools.execute(
            "run_shell",
            {"command": "printf E1 >&2; sleep 5", "timeout_seconds": 1},
            workspace_root=self.ws)
        self.assertEqual(result.error_code, "TIMEOUT")
        self.assertIn("E1", result.data["stderr"])

    def test_stdout_truncated_at_20000_chars(self) -> None:
        result = self.run_ok("run_shell", {"command": "python3 -c \"print('A' * 30000)\""})
        self.assertEqual(len(result.data["stdout"]), 20000)
        self.assertTrue(result.data.get("stdout_truncated"))
        self.assertTrue(result.data["stdout"].startswith("AAAA"))

    def test_stderr_truncated_at_20000_chars(self) -> None:
        result = tools.execute(
            "run_shell",
            {"command": "python3 -c \"import sys; sys.stderr.write('B' * 30000)\"; exit 1"},
            workspace_root=self.ws)
        self.assertEqual(len(result.data["stderr"]), 20000)
        self.assertTrue(result.data.get("stderr_truncated"))

    def test_bad_request_missing_command(self) -> None:
        result = tools.execute("run_shell", {}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_bad_request_bad_timeout(self) -> None:
        for bad in ("fast", 0, -1):
            with self.subTest(bad=bad):
                result = tools.execute("run_shell", {"command": "true", "timeout_seconds": bad},
                                       workspace_root=self.ws)
                self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_default_timeout_constant(self) -> None:
        self.assertEqual(DEFAULT_SHELL_TIMEOUT, 60)


class TestExecuteRobustness(ToolsTestBase):

    def test_execute_catches_tool_crash(self) -> None:
        class Crashy(tools.Tool):
            name = "crashy"
            description = "always crashes"
            parameters = {"type": "object", "properties": {}}

            def run(self, args, *, workspace_root):
                raise RuntimeError("boom")

        tools.register(Crashy())
        result = tools.execute("crashy", {}, workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "IO_ERROR")
        self.assertIn("boom", result.error_message or "")

    def test_execute_rejects_non_dict_args(self) -> None:
        result = tools.execute("read_file", "not-a-dict", workspace_root=self.ws)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BAD_REQUEST")

    def test_tool_result_is_frozen(self) -> None:
        result = tools.ToolResult(ok=True)
        with self.assertRaises(Exception):
            result.ok = False     # type: ignore[misc]

    def test_error_result_shape(self) -> None:
        result = tools._fail("NOT_FOUND", "nope")
        self.assertEqual(result.content, result.error_message)
        self.assertEqual(result.data, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
