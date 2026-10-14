"""审批记忆的作用域（2026-10-14 用户口径：「一直允许也没用，命令稍微变动一点就不行」）。

判定键分两类：

- **只看不改**的命令（type / cat / Get-Content / findstr …）按**动词**记：
  允许过 `type a.md`，再读 `type b.md` 不该重新问。
- **改数据**的命令（rm / del / reg / icacls …）仍按整条命令记：
  变体必须重新点头，这是刻意留的保守面。
- hardline（不可逆）永远不可记忆。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import approval_memory as am  # noqa: E402


class VerbScopeTests(unittest.TestCase):
    def test_read_commands_share_one_scope(self):
        """读类：换文件不再问。"""
        pairs = [
            ("run_shell", {"command": "type D:/docs/a.md"},
             "run_shell", {"command": "type D:/docs/b.md"}),
            ("run_shell", {"command": "Get-Content 报告1.md"},
             "run_shell", {"command": "get-content 报告2.md"}),
            ("run_shell", {"command": "cat /tmp/a.log"},
             "run_shell", {"command": "cat  /tmp/b.log"}),
            ("run_shell", {"command": "findstr /n foo a.txt"},
             "run_shell", {"command": "findstr bar b.txt"}),
        ]
        for t1, a1, t2, a2 in pairs:
            self.assertEqual(am.signature(t1, a1), am.signature(t2, a2),
                             "%s 与 %s 应共用一条记忆" % (a1, a2))

    def test_verb_of_strips_paths_and_invocation_operator(self):
        self.assertEqual(am._verb_of("C:/Windows/System32/where.exe python"), "where.exe")
        self.assertEqual(am._verb_of("& tree /f"), "tree")
        self.assertEqual(am._verb_of("   Get-Content x"), "get-content")

    def test_mutating_commands_scope_to_verb_plus_directory(self):
        """改数据（2026-10-14 口径）：按「动词 + 目标目录」记 —— 同目录的变体不再问，
        换目标目录仍要点头；没有路径的（相对路径/纯动词）就按动词记。"""
        same = [
            ("run_shell", {"command": r"del D:\proj\a.txt"}),
            ("run_shell", {"command": r"del D:\proj\b.txt"}),
            ("run_shell", {"command": r"del /f D:\proj\b.txt"}),
        ]
        for i in range(1, len(same)):
            self.assertEqual(am.signature(*same[0]), am.signature(*same[i]),
                             "同目录的删除变体应共用一条记忆")
        diff = [
            ("run_shell", {"command": r"del D:\proj\a.txt"}),
            ("run_shell", {"command": r"del C:\Windows\x"}),
            ("run_shell", {"command": r"del D:\other\a.txt"}),
        ]
        for i in range(1, len(diff)):
            self.assertNotEqual(am.signature(*diff[0]), am.signature(*diff[i]),
                                "换目标目录不该共用一条记忆")
        # 动词不同也不能混
        self.assertNotEqual(am.signature("run_shell", {"command": r"del D:\proj\a.txt"}),
                            am.signature("run_shell", {"command": r"rd D:\proj\a"}))

    def test_whitespace_and_slashes_still_normalised(self):
        a = am.signature("run_shell", {"command": "cat  /tmp/a\\b.log"})
        b = am.signature("run_shell", {"command": "cat /tmp/a/b.log"})
        self.assertEqual(a, b)

    def test_hardline_never_rememorable(self):
        self.assertFalse(am.can_remember(am.HARDLINE))
        self.assertTrue(am.can_remember("dangerous"))
        self.assertTrue(am.can_remember(""))


class MemoryBehaviourTests(unittest.TestCase):
    def test_session_allow_covers_read_variants(self):
        """实测口径：允许一次 `type a.md`，同会话读别的文件不再弹。"""
        mem = am.ApprovalMemory("s1")
        mem.remember("run_shell", {"command": "type D:/docs/a.md"}, am.SCOPE_SESSION, "")
        self.assertEqual(
            mem.lookup("run_shell", {"command": "type D:/docs/别的.md"}),
            am.SCOPE_SESSION)
        self.assertEqual(mem.lookup("run_shell", {"command": "rm -rf x"}), "")

    def test_always_allow_survives_and_is_revocable(self):
        mem = am.ApprovalMemory("s1")
        mem.remember("run_shell", {"command": "type a.md"}, am.SCOPE_ALWAYS, "")
        self.assertEqual(mem.lookup("run_shell", {"command": "type b.md"}),
                         am.SCOPE_ALWAYS)
        entries = mem.list_always()
        self.assertTrue(entries, "「一直允许」必须能导出（撤销入口要用）")
        mem.forget(entries[0]["fp"])
        self.assertEqual(mem.lookup("run_shell", {"command": "type b.md"}), "")


if __name__ == "__main__":
    unittest.main()