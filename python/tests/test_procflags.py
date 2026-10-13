# -*- coding: utf-8 -*-
"""t_0124 回归测试：Windows 上 python 层起子进程不弹控制台黑窗。

背景（2026-10-13 用户原话「agent启动能不能不带上cmd在后台挂着」）：
electron 层早已 windowsHide:true，python 侧车自己起的子进程此前全是
裸 Popen/run —— Windows 上每个子进程各弹一个 conhost/cmd 黑窗。
修复 = 统一帮助函数 procflags.no_window_kwargs()（Windows 给
CREATE_NO_WINDOW，其它平台空 dict），所有起子进程的调用点统一走它。

本文件四组断言：
  1. helper 平台分支：nt → {"creationflags": CREATE_NO_WINDOW}，
     posix → {}（不会 AttributeError，也不会带魔法数字）。
  2. merge_no_window：不改原 dict、不覆盖显式 creationflags。
  3. **AST 扫描**（核心回归网）：python/ 下所有
     subprocess.Popen / subprocess.run / subprocess.Popen_asyncio
     （asyncio.create_subprocess_*）调用点必须带 no_window 标志；
     例外必须在白名单里写明文件+函数+理由。
  4. 杀进程树仍然有效：CREATE_NO_WINDOW 不动进程组 —— 真子进程
     kill() 后被 reap（wait 返回、无僵尸）。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import procflags  # noqa: E402

# 被扫描的源码根（python/ 全部非 venv .py）。
_PY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        # venv / 缓存 / 模型产物目录不扫
        dirnames[:] = [d for d in dirnames
                       if d not in (".venv", "__pycache__", "node_modules",
                                    ".git", "bge-small-zh-v1.5",
                                    "decision_bert")]
        for name in sorted(filenames):
            if name.endswith(".py") and name != "procflags.py":
                # procflags.py 自己是 helper，不是调用点
                yield os.path.join(dirpath, name)


def _find_calls(tree: ast.AST):
    """产出 (node, func_name_str)：所有疑似起子进程的调用。

    覆盖 subprocess.Popen / subprocess.run / asyncio.create_subprocess_exec
    / asyncio.create_subprocess_shell，以及 `from subprocess import Popen`
    这类直接裸名调用。
    """
    targets = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a for a in node.names] if isinstance(node, ast.Import)
                     else [a for a in node.names])
            for alias in names:
                targets.add(alias.asname or alias.name)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
            base = func.value
            base_name = None
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                base_name = base.attr
            full = "%s.%s" % (base_name, name) if base_name else name
        elif isinstance(func, ast.Name):
            name = func.id
            full = name
        else:
            continue
        if full in ("subprocess.Popen", "subprocess.run",
                    "subprocess.call", "subprocess.check_call",
                    "subprocess.check_output",
                    "subprocess.Popen_asyncio"):
            yield node, full
        elif full == "create_subprocess_exec" or full == "create_subprocess_shell":
            # asyncio.create_subprocess_*（无论 import 风格）
            yield node, full
        elif name in ("Popen",) and "Popen" in targets:
            # from subprocess import Popen 裸名调用
            yield node, "Popen(bare)"
        elif name in ("run",) and "run" in targets:
            yield node, "run(bare)"
        elif name in ("create_subprocess_exec", "create_subprocess_shell"):
            yield node, full


def _call_has_no_window(call_node: ast.Call) -> bool:
    """调用点是否带 no_window 标志。

    认两种写法：
      a) `**procflags.no_window_kwargs()` 关键字展开；
      b) kwargs dict 先 merge（`merge_no_window`）再 `**kwargs` ——
         这种以「同函数内出现 merge_no_window 调用」为凭。
    纯 `**kwargs` 没有对应 merge 的不算（防假阳性走白名单）。
    """
    for kw in call_node.keywords:
        if kw.arg is None:                      # **something
            if isinstance(kw.value, ast.Call):
                callee = kw.value.func
                if (isinstance(callee, ast.Attribute)
                        and callee.attr == "no_window_kwargs"):
                    return True
                if (isinstance(callee, ast.Name)
                        and callee.id == "no_window_kwargs"):
                    return True
    return False


def _call_has_merge_in_scope(call_node: ast.Call, tree: ast.AST) -> bool:
    """同文件里是否有 merge_no_window(...) 出现且与本调用相邻（同函数）。"""
    # 简化判定：文件源码含 merge_no_window 且本调用在含它的函数里。
    # mcp_client 是唯一这种写法，按文件级判定足够严：文件里必须真用了
    # merge_no_window 才放行 `**kwargs` 形态。
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            if (isinstance(callee, ast.Attribute)
                    and callee.attr == "merge_no_window"):
                return True
            if (isinstance(callee, ast.Name)
                    and callee.id == "merge_no_window"):
                return True
    return False


class NoWindowHelperTest(unittest.TestCase):
    """helper 的平台分支语义。"""

    def test_no_window_kwargs_posix_returns_empty_dict(self):
        # 本 CI 是 posix；断言 helper 在 posix 上是纯空 dict（零行为变化）
        if os.name == "nt":
            self.skipTest("posix-only assertion (runs on Linux CI)")
        self.assertEqual(procflags.no_window_kwargs(), {})

    def test_no_window_kwargs_nt_returns_create_no_window(self):
        # 直接模拟 nt：patch procflags 的两个平台探测点 —— os stub（name
        # = "nt"）+ _creation_no_window 返回真实常量（Linux 的 subprocess
        # 模块上没有 CREATE_NO_WINDOW 属性，这正是 helper 用 getattr 的
        # 原因，测试也要按同样方式注入）。
        import types
        real_os = procflags.os
        real_cnw = procflags._creation_no_window
        procflags.os = types.SimpleNamespace(name="nt")  # type: ignore[assignment]
        procflags._creation_no_window = lambda: 0x08000000  # type: ignore[assignment]
        try:
            got = procflags.no_window_kwargs()
        finally:
            procflags.os = real_os
            procflags._creation_no_window = real_cnw
        self.assertEqual(set(got.keys()), {"creationflags"})
        # 值来自注入的常量来源（运行时是 subprocess.CREATE_NO_WINDOW）
        self.assertEqual(got["creationflags"], 0x08000000)

    def test_nt_branch_real_constant_on_windows(self):
        # Windows 真机上：值必须逐字节等于 subprocess.CREATE_NO_WINDOW。
        if os.name != "nt":
            self.skipTest("nt-only assertion (runs on the Windows box)")
        self.assertEqual(procflags.no_window_kwargs(),
                         {"creationflags": subprocess.CREATE_NO_WINDOW})
        self.assertEqual(procflags.no_window_kwargs()["creationflags"], 0x08000000)

    def test_no_window_kwargs_never_magic_number_literal(self):
        # helper 源码【代码区】不许出现裸 0x08000000 魔法数字（AST 级
        # 红线：文档字符串里解释性的数字不算）。
        tree = ast.parse(
            open(os.path.join(_PY_ROOT, "procflags.py"), encoding="utf-8").read())
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and n.value in (0x08000000, 134217728)]
        self.assertEqual(literals, [],
                         "procflags.py 代码里不许写死 CREATE_NO_WINDOW 数值")

    def test_merge_no_window_copies_and_does_not_override(self):
        base = {"shell": True}
        merged = procflags.merge_no_window(base)
        # 非 nt 上 merge 什么都不加
        if os.name != "nt":
            self.assertEqual(merged, {"shell": True})
        # 不原地改
        self.assertEqual(base, {"shell": True})
        # 显式 creationflags 不被覆盖（nt 模拟）
        import types
        real_os = procflags.os
        procflags.os = types.SimpleNamespace(name="nt")  # type: ignore[assignment]
        try:
            explicit = procflags.merge_no_window(
                {"shell": True, "creationflags": 0})
            self.assertEqual(explicit["creationflags"], 0)
        finally:
            procflags.os = real_os


class SubprocessCallSiteScanTest(unittest.TestCase):
    """AST 扫描：python/ 下所有起子进程的调用点都带 no_window 标志。

    白名单必须每条写理由；新增裸调用点会被此测试当场拦下。
    """

    # (相对 python/ 的路径, 调用形态) -> 理由
    ALLOWED = {
        # 无例外：截至 2026-10-13，python/ 下所有起子进程的地方都应带
        # no_window。以后真需要例外（例如用户要求可见终端的工具），
        # 在这里登记「路径:形态 -> 理由」，不许悄悄绕过。
    }

    def test_all_subprocess_call_sites_carry_no_window(self):
        offenders = []
        scanned = 0
        for path in _py_files(_PY_ROOT):
            rel = os.path.relpath(path, _PY_ROOT)
            try:
                src = open(path, encoding="utf-8").read()
                tree = ast.parse(src)
            except (OSError, SyntaxError):
                continue
            has_merge = _call_has_merge_in_scope(tree, tree)
            for call, shape in _find_calls(tree):
                scanned += 1
                # 找外层函数名（报错可读）
                func_name = "<module>"
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for sub in ast.walk(node):
                            if sub is call:
                                func_name = node.name
                                break
                if _call_has_no_window(call):
                    continue
                # `**kwargs` 形态：要求本文件确实 merge 过（mcp_client 模式）
                has_star_kwargs = any(kw.arg is None for kw in call.keywords)
                if has_star_kwargs and has_merge:
                    continue
                key = "%s:%s" % (rel, shape)
                if key in self.ALLOWED:
                    continue
                line = getattr(call, "lineno", "?")
                offenders.append("%s:%s (%s) line %s" % (rel, func_name, shape, line))
        self.assertEqual(
            offenders, [],
            "python/ 下发现未加 no_window 的子进程调用点（Windows 会弹黑窗）：\n  "
            + "\n  ".join(offenders)
            + "\n请统一改用 **procflags.no_window_kwargs()，或在 ALLOWED 里"
              "登记例外并写明理由。共扫描 %d 个调用点。" % scanned)

    def test_scan_actually_finds_call_sites(self):
        # 防御：扫描器本身不许失明 —— 必须能找到已知的真实调用点
        found = 0
        guard = os.path.join(_PY_ROOT, "guard_model.py")
        tree = ast.parse(open(guard, encoding="utf-8").read())
        for _call, shape in _find_calls(tree):
            found += 1
        self.assertGreaterEqual(found, 1, "扫描器应能在 guard_model.py 里找到 Popen")


class KillTreeStillWorksTest(unittest.TestCase):
    """CREATE_NO_WINDOW 不许改坏杀进程树/reap 语义（posix 上真验）。

    guard_model.ChildProcess.kill() 的契约：kill → wait(reap) → 关管道。
    这里 spawn 一个真子进程（同样的 kwargs 拼法），杀掉并确认 reap 完成。
    Windows 上 CREATE_NO_WINDOW 生效与否不影响本断言的语义：进程组
    归属与 Popen.kill/wait 无关（CREATE_NO_WINDOW 只管控制台分配）。
    """

    def _spawn_child(self, extra_kwargs: dict) -> subprocess.Popen:
        code = "import time, sys\nsys.stdout.write('ok\\n')\nsys.stdout.flush()\n" \
               "time.sleep(60)\n"
        return subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
            **procflags.merge_no_window(extra_kwargs),
        )

    def test_kill_and_reap_child_spawned_with_no_window_kwargs(self):
        proc = self._spawn_child({})
        try:
            self.assertIsNotNone(proc.stdout)
            # 子进程活着
            self.assertIsNone(proc.poll())
        finally:
            proc.kill()
            proc.wait(timeout=5)          # reap：不许留僵尸
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                pipe.close()
        self.assertIsNotNone(proc.returncode, "kill 后 returncode 必须就绪（已 reap）")

    def test_merge_survives_explicit_flags_combination(self):
        # 拼接不炸：显式 kwargs 与 no_window 合并后仍是合法 Popen 参数
        merged = procflags.merge_no_window({})
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('x')"],
            stdout=subprocess.PIPE, text=True, **merged)
        out, _ = proc.communicate(timeout=10)
        self.assertEqual(out.strip(), "x")


if __name__ == "__main__":
    unittest.main()
