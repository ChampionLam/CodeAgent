"""run_shell 单测：平台差异、命令行包装、输出解码（零真网络）。

对应 2026-09-24 的现场：描述里写死 "/bin/sh -c"，Windows 上模型就写 POSIX 命令；
子进程输出按 GBK 解，读到 UTF-8 字节直接炸读线程，工具结果变空，模型只能反复重试。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import tools as T  # noqa: E402


class ShellPlatformTest(unittest.TestCase):
    def test_windows_note_names_cmd_and_bans_posix(self):
        with mock.patch.object(T, "_IS_WINDOWS", True):
            note = T._shell_note()
        self.assertIn("cmd.exe /c", note)
        self.assertIn("python3", note)
        self.assertIn(sys.executable, note)
        self.assertNotIn("runs through /bin/sh", note)

    def test_posix_note_names_sh(self):
        with mock.patch.object(T, "_IS_WINDOWS", False):
            self.assertIn("/bin/sh", T._shell_note())

    def test_description_matches_this_platform(self):
        """类属性在 import 时定型：POSIX 机器上按 POSIX 渲染，Windows 真机上必须按 cmd 渲染。"""
        want = "cmd.exe /c" if os.name == "nt" else "/bin/sh"
        self.assertIn(want, T.RunShellTool.description)
        self.assertIn(want, T.RunShellTool.parameters["properties"]["command"]["description"])

    def test_wrap_only_on_windows(self):
        with mock.patch.object(T, "_IS_WINDOWS", True):
            self.assertEqual(T._wrap_shell_command("dir"), "chcp 65001>nul & dir")
        with mock.patch.object(T, "_IS_WINDOWS", False):
            self.assertEqual(T._wrap_shell_command("ls"), "ls")

    def test_shell_env_forces_utf8_stdio(self):
        env = T._shell_env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUTF8"], "1")


class ShellRunTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="shellws-")
        self.tool = T.RunShellTool()

    def test_run_decodes_utf8_with_replace_and_sets_env(self):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            captured.update(kw)
            return subprocess.CompletedProcess(cmd, 0, stdout="hello", stderr="")

        with mock.patch.object(T.subprocess, "run", fake_run):
            res = self.tool.run({"command": "echo hello"}, workspace_root=self.ws)
        self.assertTrue(res.ok)
        self.assertEqual(captured["encoding"], "utf-8")
        self.assertEqual(captured["errors"], "replace")
        self.assertEqual(captured["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertIn("hello", res.content)

    def test_windows_wraps_the_command(self):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        with mock.patch.object(T, "_IS_WINDOWS", True), mock.patch.object(T.subprocess, "run", fake_run):
            self.tool.run({"command": "dir"}, workspace_root=self.ws)
        self.assertEqual(captured["cmd"], "chcp 65001>nul & dir")

    def test_non_utf8_bytes_degrade_instead_of_raising(self):
        raw = "你好".encode("gbk")  # 直接按 utf-8 解会抛 UnicodeDecodeError
        self.assertIsInstance(T._decode_partial(raw), str)
        self.assertTrue(T._decode_partial(raw))

    @unittest.skipIf(os.name == "nt", "POSIX printf")
    def test_real_command_emitting_non_utf8_output(self):
        res = self.tool.run({"command": "printf '\\xc4\\xe3\\xba\\xc3'"}, workspace_root=self.ws)
        self.assertTrue(res.ok)


if __name__ == "__main__":
    unittest.main()