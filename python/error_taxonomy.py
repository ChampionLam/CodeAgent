# -*- coding: utf-8 -*-
"""error_taxonomy — 把四类现况统一整理成固定 schema 的「病历」(t_e0fbba7d §1)。

四类（ErrorKind）：
  process_crash      进程崩溃（含非零退出 / 看门狗超时）
  tool_failure       工具执行失败（ToolResult.ok=False，含 errorCode）
  model_anomaly      模型返回异常（EVENT_ERROR：解析失败/超限/异常输出）
  dependency_missing 依赖/环境缺失（ImportError / 命令不存在 127/9009）

每条 ErrorFacts 都带上下文：traceback（截断）、最近事件（截断）、当时输入、
结构化字段（工具名/参数摘要/错误码）。纯函数 + dataclass，零依赖、可单测；
输入可以是 BaseException、dict（agent_loop 的 EVENT_ERROR 形状）或
ToolResult（tools.py）。本模块只「整理」，不做任何修复动作。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

KIND_PROCESS_CRASH = "process_crash"
KIND_TOOL_FAILURE = "tool_failure"
KIND_MODEL_ANOMALY = "model_anomaly"
KIND_DEPENDENCY_MISSING = "dependency_missing"
KIND_UNKNOWN = "unknown"

ERROR_KINDS = (KIND_PROCESS_CRASH, KIND_TOOL_FAILURE, KIND_MODEL_ANOMALY,
               KIND_DEPENDENCY_MISSING, KIND_UNKNOWN)

#: 病历里各文本字段的截断上限（防止单条病历把提示词撑爆）。
TRACEBACK_MAX = 4000
RECENT_EVENTS_MAX = 8000
INPUT_MAX = 1200

# ---------------------------------------------------------------------------
# 依赖缺失识别（L0 判据，全部来自真实现况的信号）
# ---------------------------------------------------------------------------

#: 先抓「No module named X」（最可信），再退回 ImportError 尾部的引号名。
_NO_MODULE_RE = re.compile(r"No module named\s+'?([\w\.]+)'?", re.I)
_IMPORT_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError)[:\s]+(?:No module named\s+)?'?([\w\.]+)'?", re.I)
_WIN_CMD_NOT_FOUND = re.compile(r"is not recognized as an internal or external command", re.I)
_NOT_FOUND_CN = re.compile(r"找不到|无法识别|命令不存在|不是内部或外部命令")
_MODULE_HINT_RE = re.compile(r"(?:^|\W)module\W+([\w\.]+)", re.I)

#: 命令不存在在 Linux / Windows 下的退出码。
_EXIT_NOT_FOUND_CODES = (127, 9009)

_PKG_ALIAS = {"PIL": "pillow", "bs4": "beautifulsoup4", "yaml": "pyyaml",
              "cv2": "opencv-python", "sklearn": "scikit-learn"}


def _clean_module(raw: str) -> str:
    raw = (raw or "").strip().strip("'\"").strip()
    return raw


def missing_dependency_of(message: str, exit_code: Any = None,
                          traceback_text: str = "") -> str | None:
    """从错误文本里提取缺失的依赖名；提取不出返回 None。

    只认三类硬信号：
    - ModuleNotFoundError/ImportError 的模块名（取顶层包名）
    - Windows「不是内部或外部命令」/ 中文「找不到」 + 命令头（取首词）
    - 退出码 127 / 9009 + 文本首词
    """
    text = " ".join(str(message or "").split()) or " ".join(
        str(traceback_text or "").split())
    if not text:
        return None
    m = _NO_MODULE_RE.search(text)
    if m:
        name = _clean_module(m.group(1))
        if name:
            return name.split(".")[0]
    m = _IMPORT_RE.search(text)
    if m:
        name = _clean_module(m.group(1))
        if name and name not in ("cannot", "No", "no", "named"):
            top = name.split(".")[0]
            return top
        # 「ImportError: cannot import name 'x' from 'PIL'」：取 from 后面的名。
        m2 = re.search(r"from\s+'?([\w\.]+)'?", text)
        if m2:
            return _clean_module(m2.group(1)).split(".")[0]
    if exit_code in _EXIT_NOT_FOUND_CODES or _WIN_CMD_NOT_FOUND.search(text) \
            or _NOT_FOUND_CN.search(text):
        # 命令头取消息第一个词（"pdftotext: command not found" → pdftotext）。
        first = text.split(":")[0].strip().split()
        if first:
            cand = first[0].strip("'\"").strip()
            if cand and len(cand) < 40:
                return cand
        head = text.split(":")[-1].strip().split()
        if head:
            return head[0].strip("'\"")[:40] or None
    return None


def dependency_fix_hint(dep: str) -> str:
    """依赖缺失的安装建议（只提示，绝不自动装 —— 红线）。"""
    pkg = _PKG_ALIAS.get(dep, dep)
    return ("缺少依赖 %s。修复建议（需你手动执行，程序不会自动安装）："
            "Python 依赖用 `pip install %s`；命令行工具请按系统安装"
            "（Windows 可用 winget/scoop，Linux 用包管理器）。装好后重试即可。"
            % (dep, pkg))


# ---------------------------------------------------------------------------
# 病历
# ---------------------------------------------------------------------------

@dataclass
class ErrorFacts:
    """固定 schema 的「病历」——自愈三步（诊断/修复/验证）的唯一输入。"""

    kind: str = KIND_UNKNOWN
    source: str = "sidecar"             # sidecar | agent_loop | tool:<name> | electron
    run_id: str | None = None
    session_id: str | None = None
    code: str = ""                      # 错误码（TOOL_*/HTTP_ERROR/INTERNAL...）
    message: str = ""                   # 错误消息原文（截断）
    traceback: str = ""                 # traceback 原文（截断）
    recent_events: list = field(default_factory=list)   # 最近事件（裁剪）
    context: dict = field(default_factory=dict)         # 工具名/参数摘要/退出码等
    input_summary: str = ""             # 当时的输入（用户消息/工具参数摘要）

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "source": self.source,
            "runId": self.run_id, "sessionId": self.session_id,
            "code": self.code, "message": self.message,
            "traceback": self.traceback[:TRACEBACK_MAX],
            "recentEvents": self.recent_events[:RECENT_EVENTS_MAX],
            "context": self.context, "inputSummary": self.input_summary,
        }


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s[:limit]


def classify_exception(exc: BaseException, *, source: str = "agent_loop",
                       run_id: str | None = None, session_id: str | None = None,
                       recent_events: list | None = None,
                       context: dict | None = None,
                       input_summary: str = "") -> ErrorFacts:
    """把异常整理成病历。ImportError 家族 → dependency_missing。"""
    import traceback as tb_mod
    tb_text = tb_mod.format_exc() if not isinstance(exc, str) else str(exc)
    msg = str(exc)
    dep = missing_dependency_of(msg, traceback_text=tb_text) \
        if isinstance(exc, ImportError) else None
    kind = KIND_DEPENDENCY_MISSING if dep else (
        KIND_PROCESS_CRASH if isinstance(exc, SystemExit) and (exc.code or 0) not in (0, None)
        else KIND_MODEL_ANOMALY)
    ctx = dict(context or {})
    if dep:
        ctx["dependency"] = dep
    return ErrorFacts(
        kind=kind, source=source, run_id=run_id, session_id=session_id,
        code=type(exc).__name__, message=_clip(msg, 800), traceback=tb_text[:TRACEBACK_MAX],
        recent_events=list(recent_events or []), context=ctx,
        input_summary=_clip(input_summary, INPUT_MAX))


def classify_event_error(error: dict, *, source: str = "agent_loop",
                         run_id: str | None = None, session_id: str | None = None,
                         recent_events: list | None = None,
                         input_summary: str = "") -> ErrorFacts:
    """agent_loop 已 yield 的 EVENT_ERROR dict → 病历（模型返回异常）。"""
    code = str(error.get("code") or "UNKNOWN")
    msg = str(error.get("message") or "")
    dep = missing_dependency_of(msg, exit_code=error.get("httpStatus"))
    kind = KIND_DEPENDENCY_MISSING if dep else KIND_MODEL_ANOMALY
    ctx = {k: v for k, v in error.items() if k not in ("code", "message")}
    if dep:
        ctx["dependency"] = dep
    return ErrorFacts(
        kind=kind, source=source, run_id=run_id, session_id=session_id,
        code=code, message=_clip(msg, 800), recent_events=list(recent_events or []),
        context=ctx, input_summary=_clip(input_summary, INPUT_MAX))


def classify_tool_failure(tool_name: str, args: dict | None, result,
                          *, run_id: str | None = None,
                          session_id: str | None = None,
                          recent_events: list | None = None,
                          input_summary: str = "") -> ErrorFacts:
    """ToolResult（ok=False）→ 病历（工具执行失败，含依赖缺失判定）。"""
    ok = bool(getattr(result, "ok", False))
    code = str(getattr(result, "error_code", None) or "TOOL_FAILED")
    msg = str(getattr(result, "error_message", None)
              or getattr(result, "content", "") or "")
    # 依赖缺失判定看错误码 + 消息 + context 里的退出码（run_shell 传的那种）。
    exit_code = None
    data = getattr(result, "data", None) or {}
    if isinstance(data, dict):
        exit_code = data.get("exitCode", data.get("exit_code"))
    dep = None
    if code in ("DEP_MISSING", "COMMAND_NOT_FOUND", "MODULE_NOT_FOUND"):
        dep = missing_dependency_of(msg, exit_code=exit_code)
    if dep:
        kind, code = KIND_DEPENDENCY_MISSING, "DEP_MISSING"
    else:
        kind = KIND_TOOL_FAILURE
    ctx = {"tool": tool_name, "argsSummary": _clip(
        _args_summary(args), 600)}
    if exit_code is not None:
        ctx["exitCode"] = exit_code
    if dep:
        ctx["dependency"] = dep
    return ErrorFacts(
        kind=kind, source="tool:%s" % tool_name, run_id=run_id,
        session_id=session_id, code=code, message=_clip(msg, 800),
        recent_events=list(recent_events or []), context=ctx,
        input_summary=_clip(input_summary, INPUT_MAX))


def classify_process_crash(exit_code: Any, tail_log: str = "", *,
                           run_id: str | None = None,
                           session_id: str | None = None) -> ErrorFacts:
    """子进程非零退出 / 崩溃日志 → 病历（进程崩溃，含非零退出）。"""
    code_num = exit_code if isinstance(exit_code, int) else None
    msg = "sidecar/process exited with code %s" % (exit_code,)
    dep = missing_dependency_of(tail_log, exit_code=code_num)
    kind = KIND_DEPENDENCY_MISSING if dep else KIND_PROCESS_CRASH
    return ErrorFacts(
        kind=kind, source="process", run_id=run_id, session_id=session_id,
        code="CRASH_EXIT_%s" % (exit_code,), message=_clip(msg, 400),
        traceback=_clip(tail_log, TRACEBACK_MAX),
        context={"exitCode": exit_code} if code_num is not None else {},
        )


def _args_summary(args) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        return args[:300]
    try:
        import json as _json
        return _json.dumps(args, ensure_ascii=False)[:300]
    except Exception:                       # noqa: BLE001
        return str(args)[:300]
