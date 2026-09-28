"""权限层现在只剩一层薄壳（2026-09-25 用户钦定口径）。

用户原话：「把权限分级去掉，没有什么工作区的概念」「权限参照 Hermes Agent 的实现」。所以判定全部搬去了 guard.py，permissions.py 只做转发。

这个文件钉三件事：
  1. permissions.judge 就是 guard.judge —— 薄壳不许长出第二套口径；
  2. 旧口径的 API（Level / TOOL_BASE_LEVEL / CAN_ALWAYS_ALLOW / classify /
     is_inside_workspace / Rule / rule_for_approval / Decision）一个都不许回来；
  3. 行为抽样：日常放行、危险要问、工作区参数已消失。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "python"))

import guard  # noqa: E402
import permissions  # noqa: E402

#: 旧口径的 API：任何一条回来都意味着有人把分级/工作区又接了回去
BANNED = ("Level", "TOOL_BASE_LEVEL", "CAN_ALWAYS_ALLOW", "classify",
          "is_inside_workspace", "_inside_workspace", "_abs_target", "Rule",
          "rule_for_approval", "Decision", "judge_with_rules")


class ShimTest(unittest.TestCase):
    def test_judge_is_guard_judge(self):
        cases = [
            ("read_file", {"path": "/etc/hosts"}),
            ("write_file", {"path": "D:\\other\\a.txt"}),
            ("run_shell", {"command": "git status"}),
            ("run_shell", {"command": "rm -rf /"}),
            ("run_shell", {"command": "rm -rf ./build"}),
            ("delete_path", {"path": "/"}),
            ("web_fetch", {"url": "https://example.com"}),
        ]
        for name, args in cases:
            with self.subTest(tool=name, args=args):
                self.assertEqual(permissions.judge(name, args), guard.judge(name, args))

    def test_legacy_api_is_gone(self):
        for name in BANNED:
            self.assertFalse(hasattr(permissions, name),
                             "旧口径的 %s 又回来了：分级/工作区不该再有入口" % name)

    def test_workspace_argument_is_rejected(self):
        """judge 不再吃 workspace_root —— 传了就该报错，别被静默忽略。"""
        with self.assertRaises(TypeError):
            permissions.judge("read_file", {"path": "a.txt"}, workspace_root="/ws")

    def test_daily_ops_never_ask(self):
        for name, args in (("read_file", {"path": "E:\\proj\\a.py"}),
                           ("edit_file", {"path": "/var/www/x.conf"}),
                           ("list_dir", {"path": "C:\\"}),
                           ("run_shell", {"command": "npm install lodash"}),
                           ("web_search", {"query": "asyncio"}),
                           ("image_generate", {"prompt": "a cat"})):
            with self.subTest(tool=name):
                self.assertFalse(permissions.judge(name, args).requires_approval, name)

    def test_dangerous_still_asks(self):
        self.assertEqual(permissions.judge("run_shell", {"command": "rm -rf /"}).tier,
                         "hardline")
        self.assertEqual(permissions.judge("run_shell", {"command": "rm -rf ./build"}).tier,
                         "dangerous")
        self.assertTrue(permissions.judge("delete_path", {"path": "C:\\"}).requires_approval)

    def test_shim_exposes_the_same_helpers(self):
        self.assertIs(permissions.judge_command, guard.judge_command)
        self.assertIs(permissions.is_protected_path, guard.is_protected_path)


if __name__ == "__main__":
    unittest.main()