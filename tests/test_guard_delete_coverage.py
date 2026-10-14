r"""删数据类的正则覆盖（2026-10-14 真机实测暴露的盲区）。

背景：模型层从「闸门」降成「只观察」之后，弹卡完全由正则决定。真机上
`del D:\work\workspace\nonexistent-a.txt`（**没有** /s /q）不弹卡直接执行 ——
因为旧的正则只覆盖 `del /s`、`del /q` 这种递归形态。本文件把「删数据」这一类的
覆盖钉住：删文件/删目录/动注册表/改权限/改计划任务/改防火墙都要拦，
而读文档类（Get-Content、type、findstr、dir、ls、cat、grep…）一律不拦。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "python"))

import guard  # noqa: E402


def tier_of(command: str) -> str:
    return guard.judge_command(command).tier


class DeleteCoverageTests(unittest.TestCase):
    #: 删数据类：必须拦（dangerous 及以上）
    MUST_ASK = [
        r"del D:\proj\a.txt",
        r"del /f D:\proj\a.txt",
        r"erase D:\proj\a.txt",
        r"rd D:\proj\old",
        r"rmdir D:\proj\old",
        r"rm /tmp/a.txt",
        r"rm -f /tmp/a.txt",
        r"rm -rf /tmp/build/",
        r"del /s /q D:\proj\old",
        r"Remove-Item D:\proj\a.txt",
        r"remove-item -path D:\proj\a.txt -force",
        r"ri D:\proj\a.txt",
        r"unlink /tmp/report.md",
        r"reg add HKCU\Software\Foo /v Bar /d 1",
        r"reg import backup.reg",
        r"Set-ItemProperty -Path HKCU:\Software\Foo -Name Bar -Value 1",
        r"New-ItemProperty -Path HKCU:\Software\Foo -Name Bar -Value 1",
        r"takeown /f D:\proj\a.txt",
        r"Set-ExecutionPolicy Bypass",
        r"schtasks /create /tn foo /tr bar /sc daily",
        r"netsh advfirewall firewall add rule name=foo",
        r"rclone delete remote:path",
    ]

    #: 读/查类：一律不拦（用户口径：读文档永不弹卡）
    MUST_NOT_ASK = [
        r"Get-Content D:\proj\a.md",
        r"type D:\proj\a.md",
        r"gc D:\proj\a.md",
        r"findstr /i foo D:\proj\a.md",
        r"Select-String -Pattern foo -Path D:\proj\a.md",
        r"dir D:\proj",
        r"ls -la",
        r"cat report.md",
        r"head -n 20 log.txt",
        r"grep -rn TODO src/",
        r"tree D:\proj",
        r"echo hello",
        r"git status",
        r"python -c print(1)",
        r"where.exe python",
        r"Get-Process python",
        r"Test-Path D:\proj\a.txt",
        r"wc -l report.md",
        r"diff a.txt b.txt",
        # 工作区内的相对路径删除 = 代理清自己的产物，属于正常干活（口径见用例名）
        r"rm build/tmp.txt",
        r"Remove-Item .\build\out.txt",
        r"del out\a.tmp",
        r"rd build\old",
    ]

    def test_delete_family_asks(self):
        for cmd in self.MUST_ASK:
            with self.subTest(cmd=cmd):
                self.assertTrue(guard.judge_command(cmd).requires_approval,
                                "没拦：%s" % cmd)

    def test_read_family_never_asks(self):
        for cmd in self.MUST_NOT_ASK:
            with self.subTest(cmd=cmd):
                v = guard.judge_command(cmd)
                self.assertFalse(v.requires_approval,
                                 "误拦：%s（%s / %s）" % (cmd, v.tier, v.pattern))

    def test_recursive_delete_root_still_hardline(self):
        """删盘根这种不可逆的，仍然是 hardline（不可记忆）。"""
        for cmd in [r"del /s /q C:\*", "rd /s /q D:\\", r"rm -rf /"]:
            with self.subTest(cmd=cmd):
                self.assertEqual(tier_of(cmd), "hardline", cmd)

    def test_case_and_shape_insensitive(self):
        for cmd in [r"DEL D:\proj\a.txt", r"Del /F D:\proj\a.txt",
                    r"REMOVE-ITEM D:\proj\a.txt", r"Reg Add HKCU\Software\Foo"]:
            with self.subTest(cmd=cmd):
                self.assertTrue(guard.judge_command(cmd).requires_approval, cmd)

    def test_multiline_second_command_still_caught(self):
        cmd = "Get-Content a.md\ndel D:\\proj\\a.txt"
        v = guard.judge_command(cmd)
        self.assertTrue(v.requires_approval, cmd)

    def test_layered_judge_uses_same_regex(self):
        """judge_tool_layered 走同一套正则（模型层只观察，不参与拦）。"""
        v = guard.judge_tool_layered("run_shell", {"command": r"del D:\proj\a.txt"})
        self.assertTrue(v.requires_approval)
        v2 = guard.judge_tool_layered("run_shell", {"command": r"Get-Content D:\proj\a.md"})
        self.assertFalse(v2.requires_approval)


if __name__ == "__main__":
    unittest.main()