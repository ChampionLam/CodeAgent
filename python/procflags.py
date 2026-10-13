"""Windows 子进程「不弹控制台窗口」帮助函数（2026-10-13，t_0124 黑窗修复）。

背景：python 侧起子进程（guard 模型侧车、MCP server、run_shell、
self_heal 验证器……）默认走 CWD=console 的 Popen，Windows 上每个
非 GUI 子进程都会弹一个黑色控制台窗口，用户桌面就挂着两三个
conhost/cmd 窗。electron 层早已 `windowsHide: true`（main.ts /
stdioTransport.ts / deps.ts），python 层这次跟上。

统一入口：

- `no_window_kwargs()` → dict。Windows 返回
  ``{"creationflags": subprocess.CREATE_NO_WINDOW}``，其它平台返回
  ``{}``（`CREATE_NO_WINDOW` 只在 nt 的 subprocess 上存在，posix 引用
  即 AttributeError，所以必须按 `os.name` 分支取值，不能在模块顶层
  求值）。调用方写 ``**no_window_kwargs()`` 即可，非 Windows 上等价
  于什么都不加，行为零变化。
- `merge_no_window(kwargs)` → dict，给「已经在拼 kwargs dict」的调用
  点（mcp_client 的 Windows shell=True 分支）用：原样拷贝、再补
  creationflags，**不会**覆盖调用方已显式给出的值。

**与进程组/杀树的关系（这是本次修复最需要小心的地方）**：

- `CREATE_NO_WINDOW`（0x08000000）只影响控制台分配：子进程不继承/
  不新建控制台。它**不是** `DETACHED_PROCESS`/`CREATE_NEW_PROCESS_
  GROUP`，不改变进程组归属、不把子进程从作业/会话里摘出去，因此
  `Popen.kill()` / `wait()` / reap 语义完全不变 —— guard_model 的
  「kill 进程树后 reap + 关管道」路径（见 guard_model.py kill() 注释）
  依赖的正是这套语义。
- 唯一的相互作用：`CREATE_NO_WINDOW` 与 `DETACHED_PROCESS` 互斥
  （后者要求子进程没有控制台，前者要求「有但隐藏」式的无窗口）。
  全仓 python 层无人用 `DETACHED_PROCESS`/`start_new_session`
  （已 grep 复核），因此无冲突。
- 管道（stdin/stdout/stderr）、encoding/text、env、bufsize 均由调用
  方照旧传入，本模块只加 creationflags 一个键。
"""

from __future__ import annotations

import os
import subprocess

__all__ = ["no_window_kwargs", "merge_no_window", "CREATE_NO_WINDOW"]


def _creation_no_window() -> int | None:
    """nt 平台上 subprocess 暴露的 CREATE_NO_WINDOW 常量，其它平台 None。

    注意：`subprocess.CREATE_NO_WINDOW` 仅在 Windows 上作为模块属性存在
    （3.11 起正式文档化；`subprocess.list2cmdline` 一族同族常量都这样）。
    所以这里用 getattr 动态取，避免 import 期 AttributeError。
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", None)


def no_window_kwargs() -> dict:
    """返回应拼到 subprocess.Popen/run 的「无窗口」kwargs。

    - Windows (os.name == "nt")：``{"creationflags": CREATE_NO_WINDOW}``
    - 其它平台：``{}``（不改变任何行为，纯占位）
    """
    if os.name != "nt":
        return {}
    value = _creation_no_window()
    if value is None:              # 理论不可达（nt 上必有），防御性兜底
        return {}
    return {"creationflags": value}


def merge_no_window(kwargs: dict | None) -> dict:
    """拷贝一份 kwargs 并补上无窗口标志（不原地改、不覆盖显式值）。

    供已经手拼 kwargs dict 的调用点用（例：mcp_client 在 Windows 上
    拼 ``{"shell": True}``）。调用方若已显式给 creationflags，以调用方
    为准 —— 这里不越权。
    """
    merged = dict(kwargs or {})
    flags = no_window_kwargs()
    if "creationflags" not in merged:
        merged.update(flags)
    return merged


# 暴露常量便于测试引用（非 Windows 上为 None，测试按平台断言）。
CREATE_NO_WINDOW = _creation_no_window()
