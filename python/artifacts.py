"""一轮对话产出的文件 -> 附件卡片（2026-09-27 用户要求）。

用户原话：「我让他把文件直接发出来,那对话窗就要能直接把文件发出来到对话窗里面」——
所以助手写出来的文件不该只以一行路径的形式躺在正文里，而要作为附件出现在对话中。

两个来源：
  1. 本轮真正写盘的工具调用（write_file / patch 成功的那次），路径取自工具参数；
  2. 助手正文里的显式标记 ``[[file: <path>]]``（想主动交付一个已存在的文件时用）。

本模块只做纯逻辑 + os.stat，不读文件内容、不写任何东西。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable

#: 一轮最多挂几个，避免刷屏
MAX_ARTIFACTS = 8

_MARKER = re.compile(r"\[\[\s*file\s*[:：]\s*([^\]]+?)\s*\]\]")

_VIDEO_EXT = frozenset({".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi"})
_AUDIO_EXT = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac", ".aac"})

_WRITE_TOOLS = ("write_file", "patch", "apply_patch")

#: 生成类工具（产物路径在 result 里，不在 arguments 里）
_GEN_TOOLS = frozenset({"image_generate", "video_generate"})
_MEDIA_PATH = re.compile(r"[^\s\"'<>()\[\]]+\.(?:png|jpe?g|webp|gif|bmp|mp4|webm|mov|m4v)\b", re.I)

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif", ".svg"}
_CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".cs", ".java", ".go", ".rs",
             ".c", ".h", ".cpp", ".sql", ".sh", ".ps1", ".bat", ".cmd", ".yml", ".yaml",
             ".toml", ".ini", ".css", ".html", ".xml"}
_DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
            ".rtf", ".epub"}


def kind_of(path: str) -> str:
    """image / video / audio / doc / code / text / other —— 前端按这个挑渲染方式。

    2026-10-15：视频/音频单独成类。之前生视频的产物落在 other 里，界面只给
    一个文件图标 + 「打开」，用户没法在对话里直接播。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _VIDEO_EXT:
        return "video"
    if ext in _AUDIO_EXT:
        return "audio"
    if ext in _DOC_EXT:
        return "doc"
    if ext in _CODE_EXT:
        return "code"
    if ext in (".txt", ".log", ".md", ".csv", ".tsv"):
        return "text"
    return "other"


def marker_paths(text: str) -> list[str]:
    """正文里的 [[file: path]] 标记（全角冒号也认）。"""
    return [m.group(1).strip() for m in _MARKER.finditer(text or "") if m.group(1).strip()]


def _field(row: dict, *keys: str):
    """同一份数据在两种命名下都取得到。

    事件流里是 name/arguments，落到 tool_calls 表里却是 tool_name/arguments_json ——
    2026-09-27 实测：只认前一组的话，附件永远是空的（库里 artifacts 为 NULL）。
    """
    for k in keys:
        if row.get(k) is not None:
            return row[k]
    return None


def _args_of(row: dict) -> dict:
    args = _field(row, "arguments", "arguments_json")
    if isinstance(args, dict):
        return args
    if isinstance(args, str) and args.strip():
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def written_paths(tool_events: Iterable[dict]) -> list[str]:
    """从本轮工具事件里挑出「确实写盘成功」的文件路径。"""
    out: list[str] = []
    for row in tool_events or []:
        if not isinstance(row, dict):
            continue
        if str(_field(row, "name", "tool_name") or "") not in _WRITE_TOOLS:
            continue
        # 失败的一律不算（事件流给 ok，表里给 error_json）
        if row.get("ok") is False or row.get("errorCode") or row.get("error_json"):
            continue
        path = _args_of(row).get("path")
        if isinstance(path, str) and path.strip():
            out.append(path.strip())
    return out


def _result_of(row: dict) -> dict:
    """工具结果：事件流给 result（dict），表里给 result_json（字符串）。"""
    res = _field(row, "result", "result_json")
    if isinstance(res, dict):
        return res
    if isinstance(res, str) and res.strip():
        try:
            parsed = json.loads(res)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def generated_paths(tool_events: Iterable[dict]) -> list[str]:
    """生图/生视频的产物路径。

    2026-10-14 真机：image_generate/video_generate 跑成功、文件也落盘了
    （7.7MB png / 4.5MB mp4），但聊天里什么都没有 —— 因为这里的收集只认
    write_file/patch 的 arguments.path，生成类工具的路径在 result 里。
    优先用 result.data.files（结构化），退化到从 result.content 认媒体扩展名。
    """
    out: list[str] = []
    for row in tool_events or []:
        if not isinstance(row, dict):
            continue
        if str(_field(row, "name", "tool_name") or "") not in _GEN_TOOLS:
            continue
        if row.get("ok") is False or row.get("errorCode") or row.get("error_json"):
            continue
        res = _result_of(row)
        if res.get("ok") is False:
            continue
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        found = [str(p).strip() for p in (data.get("files") or []) if str(p).strip()]
        if not found:
            # 两种形态都要认：表里的 result_json（result.content）和事件流捕获的
            # 扁平行（row["content"]）——线上走的是后者。
            text = str(res.get("content") or row.get("content") or "")
            found = [m.group(0) for m in _MEDIA_PATH.finditer(text)]
        out.extend(found)
    return out


def _resolve(path: str, workspace_root: str | None) -> str:
    if os.path.isabs(path) or not workspace_root:
        return path
    return os.path.join(workspace_root, path)


def collect(tool_events: Iterable[dict], text: str, *,
            workspace_root: str | None = None,
            max_items: int = MAX_ARTIFACTS) -> list[dict[str, Any]]:
    """汇总成本轮的附件列表；文件此刻已不在盘上的直接跳过（不吃死路径）。

    顺序：先「显式标记」（用户明确要的），再「本轮写盘」（顺带产物）；
    路径按大小写不敏感去重（Windows）。
    """
    candidates = (marker_paths(text) + written_paths(tool_events)
                  + generated_paths(tool_events))
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for raw in candidates:
        full = _resolve(raw, workspace_root)
        key = os.path.normcase(os.path.abspath(full))
        if key in seen:
            continue
        seen.add(key)
        try:
            st = os.stat(full)
        except OSError:
            continue
        if not os.path.isfile(full):
            continue
        out.append({
            "path": full,
            "name": os.path.basename(full) or full,
            "size": int(st.st_size),
            "kind": kind_of(full),
        })
        if len(out) >= int(max_items):
            break
    return out


__all__ = ["MAX_ARTIFACTS", "collect", "kind_of", "marker_paths", "written_paths"]
