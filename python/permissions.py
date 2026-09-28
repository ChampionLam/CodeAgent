"""权限判定薄壳 —— 全部转发 guard.py（2026-09-25 起）。

判定口径参照 Hermes Agent（见 guard.py 模块头）：**没有分级、没有工作区**。
这个文件只保留两个东西：

  * judge —— 与 guard.judge 完全一致的入口，便于上层按模块注入。
  * rule_for_approval / Rule / is_inside_workspace 等 --- 删除。没有「总是允许」，
    permission_rules 表不删（历史数据可查），但判定不再读它。

工具的基准目录（解析相对路径用）改在 sidecar._base_dir()，默认用户主目录，
与权限判定无关。
"""
from __future__ import annotations

import guard as _guard


def judge(tool_name: str, args: dict | None) -> "guard.Verdict":
    """裁决一次工具调用。详见 guard.judge。"""
    return _guard.judge(tool_name, args)


# 供上层按名字取（agent_loop.default_deps / sidecar 都用 guard 本尊，不经过这里）
judge_command = _guard.judge_command
is_protected_path = _guard.is_protected_path
