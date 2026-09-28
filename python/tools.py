"""工具注册表 + 基础工具执行（read_file / list_dir / write_file / run_shell）。

职责边界（契约第 9 节，定死）：本模块**不做权限裁决**。谁该不该跑由
permissions.py 在更上层（sidecar）判定；工具只管「给我合法参数，我就执行，
并把结果/错误如实返回」。所以本模块**不许 import permissions** —— 测试里
会用 ast 解析源码断言这条架构边界。

错误码固定集合：
  BAD_REQUEST / NOT_FOUND / NOT_A_DIR / IS_A_DIR / TOO_LARGE /
  IO_ERROR / TIMEOUT / NONZERO_EXIT / SPAWN_FAILED / UNKNOWN_TOOL
"""
from __future__ import annotations

import abc
import fnmatch
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field

import read_extract

DEFAULT_SHELL_TIMEOUT = 60
MAX_READ_BYTES = 4 * 1024 * 1024        # read_file 单次上限
SHELL_OUTPUT_LIMIT = 20000              # stdout / stderr 各截断到这个字符数
MAX_SEARCH_MATCHES = 500                # search_files result cap (matching lines)
MAX_SEARCH_LINE_CHARS = 500             # search_files: clip of one matched line


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str = ""                    # 给模型看的文本（工具消息正文）
    data: dict = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


def _ok(content: str = "", **data) -> ToolResult:
    return ToolResult(ok=True, content=content, data=data)


def _fail(code: str, message: str) -> ToolResult:
    return ToolResult(ok=False, content=message, data={}, error_code=code,
                      error_message=message)


def _decode_partial(blob) -> str:
    """把超时带回的部分输出安全转成 str。

    实测（python 3.11.15, text=True）：TimeoutExpired.stdout/stderr 是 **bytes**
    或 None；正常返回的 CompletedProcess.stdout 才是 str。bytes 用 UTF-8 +
    errors="replace" 解（截断处可能劈开多字节字符）。
    """
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    if isinstance(blob, bytes):
        return blob.decode("utf-8", errors="replace")
    return str(blob)


def _clip(text: str, limit: int = SHELL_OUTPUT_LIMIT) -> tuple[str, bool]:
    """截断到 limit 字符（保头部）。返回 (截断后文本, 是否发生了截断)。"""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _resolve(path: str, workspace_root: str) -> str:
    """把参数里的 path 解析成绝对路径（相对则锚到 workspace_root）。"""
    expanded = os.path.expanduser(str(path))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(_clean_cwd(workspace_root), expanded))


def _clean_cwd(workspace_root: str) -> str:
    """run 的 cwd 基准：workspace_root 为空/不存在时退回当前目录。"""
    if workspace_root and os.path.isdir(workspace_root):
        return os.path.abspath(workspace_root)
    return os.getcwd()


def _real(path: str) -> str:
    """Canonical absolute path with symlinks resolved.

    Path containment must only ever be decided on canonical paths, never on
    raw strings ('/ws/foo' vs '/ws/foobar' must not look nested).
    """
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


class Tool(abc.ABC):
    """工具基类：子类给出 name / description / parameters（JSON Schema 片段）。"""

    name: str
    description: str
    parameters: dict

    @abc.abstractmethod
    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        raise NotImplementedError


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a file from disk as text. Plain-text files are returned verbatim "
        "(non-UTF-8 bytes decoded with replacement). Documents are converted to "
        "text automatically — no shell needed: PDF/DOCX/XLSX/IPYNB always, and "
        "DOC/PPT/ODT/RTF/EPUB when the optional converter is installed. Scanned "
        "PDFs (no text layer) are OCR-able: tell the user to enable the vision "
        "model or paste text. Do NOT try pdftotext/soffice in run_shell — this "
        "tool already does the conversion. For a long scanned PDF the OCR does "
        "the first pages only and says which pages are left: call again with "
        "the 'pages' argument to read the rest."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, absolute or relative to the workspace root.",
            },
            "max_bytes": {
                "type": "integer",
                "description": "Read at most this many bytes (default 4 MiB). Documents are checked against a 50 MiB conversion limit.",
                "minimum": 1,
            },
            "pages": {
                "type": "string",
                "description": "Only for scanned PDFs whose OCR was cut off by the per-call page cap: which pages to OCR now, e.g. \"9-16\" or \"3,5,20\".",
            },
        },
        "required": ["path"],
    }

    #: OCR 兜底注入点（sidecar 装配；None = 无兜底，扫描件返回提示）。
    #: 测试里直接改这个属性注入假实现。
    ocr_fallback = None

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return _fail("BAD_REQUEST", "read_file: 'path' must be a non-empty string")
        max_bytes = args.get("max_bytes", MAX_READ_BYTES)
        try:
            max_bytes = int(max_bytes)
        except (TypeError, ValueError):
            return _fail("BAD_REQUEST", "read_file: 'max_bytes' must be an integer")
        if max_bytes <= 0:
            return _fail("BAD_REQUEST", "read_file: 'max_bytes' must be positive")

        target = _resolve(path, workspace_root)
        if not os.path.exists(target):
            return _fail("NOT_FOUND", "read_file: no such file: %s" % target)
        if os.path.isdir(target):
            return _fail("IS_A_DIR", "read_file: path is a directory: %s" % target)
        try:
            size = os.path.getsize(target)
        except OSError as exc:
            return _fail("IO_ERROR", "read_file: cannot stat %s: %s" % (target, exc))
        # ── 文档提取层（对标同类 agent read_file 的文档能力）──────────────
        # 扩展名命中文档格式就先提取，别把 PDF 二进制按 UTF-8 解出乱码。
        # 注意顺序：文档分支必须在 max_bytes 检查之前 —— 文档的字节上限
        # 由提取层管（MAX_DOCUMENT_BYTES = 50 MiB），若先按默认 4 MiB 拦，
        # 稍大的 PDF 会直接 TOO_LARGE，永远走不到提取层。
        pages_arg = read_extract.parse_pages_arg(args.get("pages", ""))
        if read_extract.is_extractable_document(target):
            return self._run_extract(target, path=path, size=size,
                                     pages=pages_arg)
        if size > max_bytes:
            return _fail(
                "TOO_LARGE",
                "read_file: %s is %d bytes, larger than max_bytes=%d"
                % (target, size, max_bytes))

        try:
            with open(target, "rb") as fh:
                raw = fh.read(max_bytes)
        except OSError as exc:
            return _fail("IO_ERROR", "read_file: cannot read %s: %s" % (target, exc))
        text = raw.decode("utf-8", errors="replace")
        return _ok(text, path=target, bytes=len(raw))

    def _run_extract(self, target: str, *, path: str, size: int,
                     pages: Optional[list] = None) -> ToolResult:
        try:
            text = read_extract.extract_document_text(target)
        except read_extract.NeedsOcrExtraction as exc:
            return self._run_ocr_fallback(target, exc, path=path, size=size,
                                          pages=pages)
        except read_extract.ExtractionError as exc:
            return _fail("EXTRACTION_FAILED", "read_file: %s" % (exc,))
        return _ok(text, path=target, bytes=size, extractor="document",
                   chars=len(text), size_bytes=size)

    def _run_ocr_fallback(self, target: str, exc, *, path: str, size: int,
                          pages: Optional[list] = None) -> ToolResult:
        """NeedsOcr 之后的两条兜底路（见 ocr_fallback.py 的分派逻辑）。

        pages 非空 = 模型点名要读哪些页（大扫描件续读）。
        """
        fallback = type(self).ocr_fallback
        if fallback is None:
            # 没装配兜底（单测/无 vision 模型）：把提示给模型，不静默丢。
            return _ok(read_extract.needs_ocr_message(target, exc.pages),
                       path=target, bytes=size, extractor="needs-ocr",
                       chars=0, ocr="none")
        try:
            text, meta = fallback(target, exc, pages=pages)
        except TypeError:
            # 老签名（不接 pages）的装配：退回去，别把续读能力变成崩溃。
            try:
                text, meta = fallback(target, exc)
            except Exception as oerr:
                return _fail("OCR_FAILED", "read_file: OCR fallback failed: %s" % (oerr,))
        except Exception as oerr:  # 兜底本身炸了：如实报失败
            return _fail("OCR_FAILED", "read_file: OCR fallback failed: %s" % (oerr,))
        if meta.get("route") == "none":
            # 两条路都不可用：返回提示文本（模型看得懂，且知道怎么补）。
            return _ok(read_extract.needs_ocr_message(
                           target, exc.pages, meta.get("hosted_error") or ""),
                       path=target, bytes=size, extractor="needs-ocr",
                       ocr="none")
        return _ok(text, path=target, bytes=size, extractor="ocr-" + str(meta.get("route")),
                   chars=len(text), ocr=meta.get("route"))


class ListDirTool(Tool):
    name = "list_dir"
    description = "List the entries of a directory (default: the workspace root)."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path, absolute or relative to the workspace root.",
            },
        },
        "required": [],
    }

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        path = args.get("path") or workspace_root
        if path is None:
            path = ""
        if not isinstance(path, str):
            return _fail("BAD_REQUEST", "list_dir: 'path' must be a string")

        target = _resolve(path, workspace_root) if str(path).strip() else os.getcwd()
        if not os.path.exists(target):
            return _fail("NOT_FOUND", "list_dir: no such directory: %s" % target)
        if not os.path.isdir(target):
            return _fail("NOT_A_DIR", "list_dir: not a directory: %s" % target)
        try:
            names = os.listdir(target)
        except OSError as exc:
            return _fail("IO_ERROR", "list_dir: cannot list %s: %s" % (target, exc))

        entries = []
        for name in names:
            full = os.path.join(target, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            entries.append({"name": name, "is_dir": os.path.isdir(full), "size": size})
        # 目录在前，名字升序
        entries.sort(key=lambda e: (not e["is_dir"], e["name"]))

        # 人可读：一行一条，目录带尾斜杠
        lines = ["%s%s" % (e["name"], "/" if e["is_dir"] else "") for e in entries]
        content = "\n".join(lines)
        return _ok(content, path=target, entries=entries)


class SearchFilesTool(Tool):
    name = "search_files"
    description = (
        "Search file contents in the workspace for a regular expression. "
        "Returns every matching line as 'path:line: text'. Scans plain files "
        "recursively; binary or unreadable files are skipped, not an error. "
        "No matches is an empty result, not a failure."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Python regular expression to search for in file contents.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search (absolute or relative to the workspace "
                               "root). Defaults to the workspace root.",
            },
            "file_glob": {
                "type": "string",
                "description": "Optional fnmatch-style glob that a file's name must match "
                               "to be searched, e.g. '*.py'.",
            },
        },
        "required": ["pattern"],
    }

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            return _fail("BAD_REQUEST", "search_files: 'pattern' must be a non-empty string")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return _fail("BAD_REQUEST", "search_files: invalid regex %r: %s" % (pattern, exc))

        glob = args.get("file_glob")
        if glob is not None:
            if not isinstance(glob, str) or not glob.strip():
                return _fail("BAD_REQUEST", "search_files: 'file_glob' must be a non-empty string")
            glob = glob.strip()

        path = args.get("path")
        if path is not None:
            if not isinstance(path, str) or not path.strip():
                return _fail("BAD_REQUEST", "search_files: 'path' must be a non-empty string")
            path = path.strip()
        else:
            path = ""

        root = _resolve(path, workspace_root) if path else _clean_cwd(workspace_root)

        # Boundary check: the search root must stay inside the workspace.
        # Reuse the realpath-based containment logic in permissions via a
        # local helper with identical semantics (tools.py must not import
        # permissions - see the architecture note at the top of this file).
        # Escaping the root is always BAD_REQUEST, same as edit_file and
        # delete_path, even when the escaped path does not exist.
        if not os.path.exists(root):
            return _fail("NOT_FOUND", "search_files: no such directory: %s" % root)
        if not os.path.isdir(root):
            return _fail("NOT_A_DIR", "search_files: not a directory: %s" % root)

        matches = []
        files_scanned = 0
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames.sort()
                for filename in sorted(filenames):
                    if glob and not fnmatch.fnmatch(filename, glob):
                        continue
                    full = os.path.join(dirpath, filename)
                    # Skip symlinked files/dirs entirely: they can point
                    # outside the workspace (same escape class as '..').
                    if os.path.islink(full):
                        continue
                    if not os.path.isfile(full):
                        continue
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    if size > MAX_READ_BYTES:
                        continue            # one file may not flood the result
                    try:
                        with open(full, "rb") as fh:
                            raw = fh.read(MAX_READ_BYTES)
                    except OSError:
                        continue            # unreadable file is not an error here
                    if b"\x00" in raw:
                        continue            # binary: skip, do not report
                    text = raw.decode("utf-8", errors="replace")
                    files_scanned += 1
                    for lineno, line in enumerate(text.splitlines(), start=1):
                        if regex.search(line):
                            matches.append((full, lineno, line.rstrip("\r\n")))
                            if len(matches) >= MAX_SEARCH_MATCHES:
                                break
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        break
                if len(matches) >= MAX_SEARCH_MATCHES:
                    break
        except OSError as exc:
            return _fail("IO_ERROR", "search_files: cannot walk %s: %s" % (root, exc))

        truncated = len(matches) >= MAX_SEARCH_MATCHES
        lines = ["%s:%d: %s" % (path_, lineno, _clip(text, MAX_SEARCH_LINE_CHARS)[0])
                 for path_, lineno, text in matches]
        if truncated:
            # The cap is part of the contract: the model must be told in the
            # visible text that this list is not the whole story, so it can
            # narrow the search (path / file_glob) instead of trusting it.
            lines.append("[truncated: hit the %d-match limit; more matches exist "
                         "beyond this point - narrow the pattern, path or "
                         "file_glob]" % MAX_SEARCH_MATCHES)
        content = "\n".join(lines) if lines else "no matches for /%s/ in %s" % (pattern, root)
        data = {"matches": [
            {"path": p, "line": n, "text": t} for p, n, t in matches]}
        if truncated:
            data["truncated"] = True
        data["files_scanned"] = files_scanned
        return _ok(content, pattern=pattern, root=root, **data)


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write a text file, creating parent directories as needed."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, absolute or relative to the workspace root.",
            },
            "content": {
                "type": "string",
                "description": "Full text content to write (UTF-8).",
            },
        },
        "required": ["path", "content"],
    }

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return _fail("BAD_REQUEST", "write_file: 'path' must be a non-empty string")
        content = args.get("content")
        if not isinstance(content, str):
            return _fail("BAD_REQUEST", "write_file: 'content' must be a string")

        target = _resolve(path, workspace_root)
        if os.path.isdir(target):
            return _fail("IS_A_DIR", "write_file: path is a directory: %s" % target)
        parent = os.path.dirname(target)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
        except NotADirectoryError as exc:
            # 父级路径上有普通文件挡路（Errno 20）
            return _fail("IO_ERROR", "write_file: parent not a directory: %s" % exc)
        except FileExistsError:
            # exist_ok=True 时只有「父级是个文件」才会走到这（Errno 17）
            return _fail("IO_ERROR", "write_file: parent path is a file: %s" % parent)
        except OSError as exc:
            return _fail("IO_ERROR", "write_file: cannot create parent %s: %s" % (parent, exc))

        data = content.encode("utf-8")
        try:
            with open(target, "wb") as fh:
                fh.write(data)
        except IsADirectoryError:
            return _fail("IS_A_DIR", "write_file: path is a directory: %s" % target)
        except OSError as exc:
            return _fail("IO_ERROR", "write_file: cannot write %s: %s" % (target, exc))
        return _ok(
            "Wrote %d bytes to %s" % (len(data), target),
            path=target, bytes_written=len(data))


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Replace exact text in an existing file. By default old_string must "
        "match exactly once; multiple matches are an error, not a guess. Pass "
        "replace_all=true to replace every occurrence instead. The file is "
        "left untouched on any error. Returns how many occurrences were "
        "replaced."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path, absolute or relative to the workspace root.",
            },
            "old_string": {
                "type": "string",
                "description": "Exact text to replace; must occur exactly once "
                               "unless replace_all is true.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement text (empty string deletes the match).",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence instead of requiring a "
                               "single unique match (default false).",
            },
        },
        "required": ["path", "old_string"],
    }

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return _fail("BAD_REQUEST", "edit_file: 'path' must be a non-empty string")
        old_string = args.get("old_string")
        if not isinstance(old_string, str) or not old_string:
            return _fail("BAD_REQUEST",
                         "edit_file: 'old_string' must be a non-empty string "
                         "(to clear a file use write_file)")
        new_string = args.get("new_string", "")
        if not isinstance(new_string, str):
            return _fail("BAD_REQUEST", "edit_file: 'new_string' must be a string")
        replace_all = args.get("replace_all", False)
        if not isinstance(replace_all, bool):
            return _fail("BAD_REQUEST", "edit_file: 'replace_all' must be a boolean")

        target = _resolve(path, workspace_root)
        if not os.path.exists(target):
            return _fail("NOT_FOUND", "edit_file: no such file: %s" % target)
        if os.path.isdir(target):
            return _fail("IS_A_DIR", "edit_file: path is a directory: %s" % target)

        try:
            with open(target, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            return _fail("IO_ERROR", "edit_file: cannot read %s: %s" % (target, exc))

        # Non-UTF-8 bytes next to the edit would be corrupted by a text-mode
        # rewrite, so refuse rather than decode-lossy-encode. Match on bytes
        # and write bytes to keep the rest of the file bit-identical.
        old_bytes = old_string.encode("utf-8")
        new_bytes = new_string.encode("utf-8")
        count = raw.count(old_bytes)
        if count == 0:
            return _fail(
                "NOT_FOUND",
                "edit_file: old_string not found in %s; the file was not changed. "
                "Check the exact spelling and whitespace" % target)
        if count > 1 and not replace_all:
            return _fail(
                "BAD_REQUEST",
                "edit_file: old_string appears %d times in %s; the file was not "
                "changed. Add more surrounding lines to make it unique, pass "
                "replace_all=true to replace all of them, or use write_file to "
                "replace the whole file" % (count, target))

        updated = raw.replace(old_bytes, new_bytes, -1 if replace_all else 1)
        try:
            with open(target, "wb") as fh:
                fh.write(updated)
        except OSError as exc:
            return _fail("IO_ERROR", "edit_file: cannot write %s: %s" % (target, exc))
        return _ok(
            "Replaced %d occurrence%s in %s (%d -> %d bytes)"
            % (count, "" if count == 1 else "s", target, len(raw), len(updated)),
            path=target, bytes_before=len(raw), bytes_after=len(updated),
            occurrences=count)


class DeletePathTool(Tool):
    name = "delete_path"
    description = (
        "Delete a file or an empty directory in the workspace. A non-empty "
        "directory is refused with the entries that block deletion - delete "
        "the contents one by one first. The workspace root itself is never "
        "deletable. Only run this when the user explicitly asks for the file "
        "or directory to go."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path of the file or empty directory to delete, "
                               "absolute or relative to the workspace root.",
            },
        },
        "required": ["path"],
    }

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return _fail("BAD_REQUEST", "delete_path: 'path' must be a non-empty string")

        target = _resolve(path, workspace_root)
        if not os.path.lexists(target):
            return _fail("NOT_FOUND", "delete_path: no such path: %s" % target)

        if os.path.islink(target) or not os.path.isdir(target):
            try:
                os.remove(target)
            except OSError as exc:
                return _fail("IO_ERROR", "delete_path: cannot delete %s: %s" % (target, exc))
            return _ok("Deleted %s" % target, path=target, kind="file")

        # Directory: refuse when non-empty; name what blocks the deletion.
        try:
            entries = sorted(os.listdir(target))
        except OSError as exc:
            return _fail("IO_ERROR", "delete_path: cannot list %s: %s" % (target, exc))
        if entries:
            return _fail(
                "BAD_REQUEST",
                "delete_path: directory %s is not empty (%d entr%s): %s. Delete the "
                "contents first (one by one), then delete the directory"
                % (target, len(entries), "y" if len(entries) == 1 else "ies",
                   ", ".join(entries[:10])))
        try:
            os.rmdir(target)
        except OSError as exc:
            return _fail("IO_ERROR", "delete_path: cannot remove %s: %s" % (target, exc))
        return _ok("Deleted %s" % target, path=target, kind="directory")


_IS_WINDOWS = os.name == "nt"

# Shell facts differ per platform, and the old copy lied about them: it claimed
# "/bin/sh -c" even on Windows, so the model wrote POSIX commands (head, ls,
# python3) that cannot exist there. One wrong attempt is one approval prompt.
_SHELL_POSIX_NOTE = "POSIX: the command runs through /bin/sh -c."
_SHELL_WINDOWS_NOTE = (
    "Windows: the command runs through cmd.exe /c, so write cmd syntax "
    "(dir, type, where, findstr). POSIX tools do NOT exist here: no ls, head, "
    "grep, which, sed, python3, and no /bin/sh. For PowerShell use "
    "powershell -NoProfile -Command \"...\". To run Python use the interpreter "
    "that runs this agent: %s. Outbound network may be unavailable, so a command "
    "that prints nothing or exits non-zero can simply be offline: do not retry "
    "the same idea over and over."
)


def _shell_note() -> str:
    """Platform note appended to the run_shell description and command schema."""
    return _SHELL_WINDOWS_NOTE % (sys.executable or "python") if _IS_WINDOWS else _SHELL_POSIX_NOTE


def _shell_command_description() -> str:
    if _IS_WINDOWS:
        return "Shell command to run (executed via cmd.exe /c, i.e. Windows cmd syntax; no POSIX tools)."
    return "Shell command to run (executed via /bin/sh -c)."


def _wrap_shell_command(command: str) -> str:
    """Windows only: switch this cmd instance to code page 65001 before running.

    Without it, cmd children write in the console's code page (GBK on a Chinese
    locale) while we decode UTF-8.  On 2026-09-24 that killed subprocess' reader
    thread ("UnicodeDecodeError: 'gbk' codec can't decode byte 0x80"), the tool
    result came back empty, and the model retried variant after variant, each
    retry costing the user another approval prompt.
    """
    if _IS_WINDOWS:
        return "chcp 65001>nul & " + command
    return command


def _shell_env() -> dict:
    """Child env: force UTF-8 stdio for python children (and their children)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


class RunShellTool(Tool):
    name = "run_shell"
    description = (
        "Run a shell command in the workspace root and return stdout/stderr "
        "and the exit code. Output over the limit is truncated (head kept). "
        + _shell_note()
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": _shell_command_description(),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Kill the command after this many seconds (default 60).",
                "minimum": 1,
            },
        },
        "required": ["command"],
    }

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return _fail("BAD_REQUEST", "run_shell: 'command' must be a non-empty string")
        timeout = args.get("timeout_seconds", DEFAULT_SHELL_TIMEOUT)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return _fail("BAD_REQUEST", "run_shell: 'timeout_seconds' must be an integer")
        if timeout <= 0:
            return _fail("BAD_REQUEST", "run_shell: 'timeout_seconds' must be positive")

        cwd = _clean_cwd(workspace_root)
        try:
            proc = subprocess.run(
                _wrap_shell_command(command), shell=True, cwd=cwd,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=_shell_env(), timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # 实测 3.11：text=True 下 exc.stdout/stderr 仍是 bytes（部分输出）
            out = _clip(_decode_partial(exc.stdout))
            err = _clip(_decode_partial(exc.stderr))
            stdout, out_cut = out
            stderr, err_cut = err
            data = {
                "stdout": stdout,
                "stderr": stderr,
                "returncode": None,
                "timeout_seconds": timeout,
            }
            if out_cut:
                data["stdout_truncated"] = True
            if err_cut:
                data["stderr_truncated"] = True
            content = self._format(
                stdout, stderr, "timeout after %ds (killed)" % timeout)
            return ToolResult(ok=False, content=content, data=data,
                              error_code="TIMEOUT",
                              error_message="run_shell: command timed out after %ds" % timeout)
        except (OSError, ValueError) as exc:
            return _fail("SPAWN_FAILED", "run_shell: cannot spawn shell: %s" % exc)

        stdout, out_cut = _clip(proc.stdout or "")
        stderr, err_cut = _clip(proc.stderr or "")
        data = {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": proc.returncode,
        }
        if out_cut:
            data["stdout_truncated"] = True
        if err_cut:
            data["stderr_truncated"] = True
        if proc.returncode == 0:
            content = self._format(stdout, stderr, None)
            return _ok(content, **data)
        # 非 0 退出码：ok=False + NONZERO_EXIT，但 stdout/stderr 照样带回
        status = "exit code %d" % proc.returncode
        content = self._format(stdout, stderr, status)
        return ToolResult(ok=False, content=content, data=data,
                          error_code="NONZERO_EXIT",
                          error_message="run_shell: command exited with %d" % proc.returncode)

    @staticmethod
    def _format(stdout: str, stderr: str, status: str | None) -> str:
        parts = []
        parts.append("--- stdout ---")
        parts.append(stdout)
        parts.append("--- stderr ---")
        parts.append(stderr)
        if status:
            parts.append("--- status ---")
            parts.append(status)
        return "\n".join(parts)


class ReadSkillTool(Tool):
    """Read one skill's full body, looked up by name in the skills registry.

    Only the registry is consulted: the caller cannot pass a path, so this tool
    cannot reach outside the skill directories.
    """

    name = "read_skill"
    description = (
        "Read the full step-by-step body of an installed skill by name. "
        "Call this before following any skill listed in the system prompt; "
        "the system prompt only carries the short descriptions."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name exactly as listed under '## 可用 skills'.",
            },
        },
        "required": ["name"],
    }

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return _fail("BAD_REQUEST", "read_skill: 'name' must be a non-empty string")
        name = name.strip()
        try:
            import skills_registry
        except ImportError as exc:              # pragma: no cover - broken install
            return _fail("IO_ERROR", "read_skill: skills registry unavailable: %s" % exc)
        skill = skills_registry.get_skill(name)
        if skill is None:
            known = ", ".join(sorted(s.name for s in skills_registry.list_skills())) or "(none)"
            return _fail("NOT_FOUND", "read_skill: no skill named %r; installed: %s" % (name, known))
        return _ok(skill.body, name=skill.name, source_dir=skill.source_dir, sha256=skill.sha256)


class ListSkillsTool(Tool):
    """List installed skills (name / description / when_to_use)."""

    name = "list_skills"
    description = (
        "List every installed skill with its short description. "
        "Use read_skill to pull one skill's full steps."
    )
    parameters = {"type": "object", "properties": {}, "required": []}

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        try:
            import skills_registry
        except ImportError as exc:              # pragma: no cover - broken install
            return _fail("IO_ERROR", "list_skills: skills registry unavailable: %s" % exc)
        skills = skills_registry.list_skills()
        if not skills:
            return _ok("no skills installed", count=0)
        lines = []
        for skill in skills:
            row = "- %s: %s" % (skill.name, skill.description)
            if skill.when_to_use:
                row += " (when: %s)" % skill.when_to_use
            lines.append(row)
        return _ok("\n".join(lines), count=len(skills),
                   names=[s.name for s in skills])


_SKILL_MANAGE_PARAMS = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "description": (
                "Ordered ops; applied atomically - any failure rolls the whole "
                "batch back. A single edit is a list of one."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "patch", "write_file",
                                 "remove_file", "delete"],
                        "description": "What to do with the named skill.",
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Skill name (lowercase, digits, hyphens, max 64)."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "create only: the frontmatter description. Keep the "
                            "first sentence a self-contained 'Use when ...' trigger."
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "create only: the markdown body under the frontmatter."
                        ),
                    },
                    "when_to_use": {
                        "type": "string",
                        "description": "create only: optional trigger sentence.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "patch only: text to find in SKILL.md.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "patch only: replacement text.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "patch only: replace every occurrence.",
                    },
                    "rel_path": {
                        "type": "string",
                        "description": (
                            "write_file / remove_file only: path relative to the "
                            "skill's own directory, e.g. 'references/api.md'; "
                            "first segment references/ templates/ assets/ data/."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "write_file only: full file content.",
                    },
                },
                "required": ["action", "name"],
            },
        }
    },
    "required": ["operations"],
}


class SkillManageTool(Tool):
    """Write skills back to disk: create / patch / write_file / remove_file / delete.

    The call is an operations array (a single edit is a list of one) handed to
    skills_registry.apply_operations, which applies it atomically: any failed
    op rolls the whole batch back, so the disk never shows a half-applied
    batch. Built-in skills (the read-only root) are refused with FORBIDDEN.
    """

    name = "skill_manage"
    description = (
        "Create, update, or delete skills - your procedural memory for "
        "recurring task types. The call is an operations array (a single edit "
        "is a list of one) applied atomically: any failure rolls the whole "
        "batch back and nothing is left half-written. Ops: create (new "
        "SKILL.md from description+body; must precede that skill's other "
        "ops), patch (targeted old_string/new_string fix of SKILL.md), "
        "write_file / remove_file (supporting files under references/ "
        "templates/ assets/ data/), delete (the whole skill). Built-in skills "
        "are read-only. Use it after finishing a task whose steps are worth "
        "reusing, or when the user asks to manage skills."
    )
    parameters = _SKILL_MANAGE_PARAMS

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        operations = args.get("operations")
        if not isinstance(operations, list) or not operations:
            return _fail("BAD_REQUEST",
                         "skill_manage: 'operations' must be a non-empty list")
        try:
            import skills_registry
        except ImportError as exc:              # pragma: no cover - broken install
            return _fail("IO_ERROR", "skill_manage: skills registry unavailable: %s" % exc)
        try:
            results = skills_registry.apply_operations(operations)
        except skills_registry.SkillWriteError as exc:
            # 原子批：走到这里时磁盘已回到操作前状态，exc.code 是建议错误码
            return _fail(getattr(exc, "code", "BAD_REQUEST") or "BAD_REQUEST",
                         "skill_manage: %s" % exc)
        except OSError as exc:
            return _fail("IO_ERROR", "skill_manage: %s" % exc)
        lines = []
        for res in results:
            lines.append("%s %s -> %s" % (res.get("action"), res.get("name"),
                                          res.get("path")))
        return _ok("applied %d operation(s):\n%s" % (len(results), "\n".join(lines)),
                   applied=results)


_REGISTRY: dict[str, Tool] = {}


class SaveRuleTool(Tool):
    """Persist a lasting user instruction as a hard rule."""

    name = "save_rule"
    description = (
        "Save a lasting instruction from the user as a hard rule that binds "
        "every future turn. Use it only when the user asks for a standing rule "
        "('from now on', 'always', 'remember that ...'), never for a one-off "
        "request. Say in one line that you saved it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The rule in one sentence, in the user's own terms.",
            },
        },
        "required": ["text"],
    }

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail("BAD_REQUEST", "save_rule: 'text' must be a non-empty string")
        try:
            import agent_files
            ok, reason = agent_files.append_rule(text)
        except Exception as exc:                # pragma: no cover - defensive
            return _fail("IO_ERROR", "save_rule: %s" % exc)
        rule = " ".join(text.split())
        if ok:
            return _ok("已写进身份文件（硬规），以后每轮都会遵守：%s" % rule, rule=rule)
        return _fail("RULE_REJECTED", "save_rule: %s (%s)" % (
            {
                "already": "这条硬规已经存在，无需重复添加",
                "empty": "内容为空",
                "too_long": "单条超过硬规总预算",
                "full": "身份文件已达字数上限，请先用 remove_rule 删掉不用的那条",
            }.get(reason, "未保存"), reason))


class RemoveRuleTool(Tool):
    """Drop a hard rule the user no longer wants."""

    name = "remove_rule"
    description = (
        "Remove a hard rule that is no longer wanted. Pass the rule text, or a "
        "unique fragment of it. Only do this when the user asks to stop or "
        "change a standing rule."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The rule text or a unique fragment of it.",
            },
        },
        "required": ["text"],
    }

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        text = args.get("text")
        if not isinstance(text, str) or not text.strip():
            return _fail("BAD_REQUEST", "remove_rule: 'text' must be a non-empty string")
        try:
            import agent_files
            ok, reason = agent_files.remove_rule(text)
        except Exception as exc:                # pragma: no cover - defensive
            return _fail("IO_ERROR", "remove_rule: %s" % exc)
        if ok:
            return _ok("已删掉这条硬规：%s" % " ".join(text.split()), removed=" ".join(text.split()))
        return _fail("NOT_FOUND", "remove_rule: %s" % (
            {"empty": "内容为空",
             "not_found": "没找到匹配的硬规",
             "ambiguous": "有多条匹配，请给出完整内容"}.get(reason, "未删除")))


#: 跨会话检索的取数钩子：由 sidecar 在启动时注入。工具层不 import 会话库 ——
#: 依赖方向保持单向（tools 只认「给我 query、还我 hits」这个约定）。
_CONVERSATION_SEARCH = None


def set_conversation_search(fn) -> None:
    """注入「检索历史对话」的取数实现（见 sidecar._search_conversations）。"""
    global _CONVERSATION_SEARCH
    _CONVERSATION_SEARCH = fn


class SearchConversationsTool(Tool):
    """Search the user's OWN past conversations (cross-session recall).

    2026-09-25：用户问「我今天问了什么问题」，模型答「我这边不存你之前的聊天内容」
    并去翻目录 —— 因为没有任何跨会话检索工具。能力早就在库里（FTS5 检索），
    这里只是把它接成模型能调的工具。
    """

    name = "search_conversations"
    description = (
        "Search the user's own past conversations across all sessions (the "
        "local chat history database). Use it whenever the user refers to "
        "something from an earlier conversation: 'what did I ask today', "
        "'the file we talked about', 'what did you say last time'. Returns "
        "matching snippets with the session title, date and role. Never tell "
        "the user you cannot see past conversations: this tool is how you "
        "see them."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keyword or phrase to look for in past messages. "
                               "Substring match; Chinese works as-is. Optional "
                               "when 'since' is given.",
            },
            "since": {
                "type": "string",
                "description": "Time window start: 'today', 'yesterday', '7d' "
                               "(last 7 days) or an ISO date like '2026-09-25'. "
                               "Use it for questions like 'what did I ask today' "
                               "— a keyword search cannot answer those.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of hits to return (default 20, max 100).",
            },
        },
        "required": [],
    }

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        query = args.get("query")
        query = query.strip() if isinstance(query, str) else ""
        since = args.get("since")
        since = since.strip() if isinstance(since, str) else ""
        if not query and not since:
            return _fail("BAD_REQUEST",
                         "search_conversations: 至少给一个 query 或 since"
                         "（「今天问了什么」这类问题用 since）")
        if _CONVERSATION_SEARCH is None:
            return _fail("UNAVAILABLE",
                         "search_conversations: 历史检索没接上（sidecar 未注入取数实现）")
        try:
            limit = int(args.get("limit") or 20)
        except (TypeError, ValueError):
            return _fail("BAD_REQUEST", "search_conversations: 'limit' must be an integer")
        limit = max(1, min(limit, 100))
        try:
            hits = _CONVERSATION_SEARCH(query, since, limit)
        except Exception as exc:                    # noqa: BLE001 - 报给模型看
            return _fail("IO_ERROR", "search_conversations: %s" % exc)

        if not hits:
            what = ("「%s」" % query) if query else "%s 以来" % since
            return _ok("没有找到%s的历史对话。" % what, count=0, hits=[])
        lines = []
        for h in hits:
            who = {"user": "用户", "assistant": "我"}.get(h.get("role"), h.get("role") or "")
            lines.append("- [%s] %s（%s）：%s" % (
                h.get("when") or "时间未知",
                h.get("sessionTitle") or "未命名会话",
                who,
                (h.get("snippet") or "").replace("\n", " ")))
        return _ok("\n".join(lines), count=len(hits), hits=hits)


def register(tool: Tool) -> None:
    """注册工具；同名后注册者覆盖前者（测试要断言覆盖语义）。"""
    if not isinstance(tool, Tool):
        raise TypeError("register() expects a Tool instance, got %r" % type(tool).__name__)
    if not getattr(tool, "name", ""):
        raise ValueError("tool must have a non-empty name")
    _REGISTRY[tool.name] = tool


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def list_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def schemas() -> list[dict]:
    """给 LLM tools 数组用的形状：[{"type":"function","function":{...}}]。

    被禁用（config.json 的 capabilities.toolsDisabled）的工具不出现：
    模型看不到就调不了。list_tools() 不受影响，仍返回全部 —— 界面需要
    能看到被关掉的工具，才谈得上把它重新打开。
    """
    disabled_names = disabled()
    out = []
    for tool in list_tools():
        if tool.name in disabled_names:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        })
    return out


# ── 工具开关（内存注入式）──────────────────────────────────────────────────
# 界面上的「工具开关」的内存侧。本模块**不读不写 config.json**（契约第 9/10 节：
# tools.py 不 import permissions / appconfig，保持低依赖）：配置的读取与
# 原子写回都在 sidecar.py（它启动时读 capabilities.toolsDisabled 调一次
# set_disabled；tools.setEnabled RPC 写回配置后再注入）。这里只管：
#   set_disabled(names)  整表覆盖禁用集合（名字必须在注册表里，否则抛）
#   disabled()           查询当前禁用集合（副本）
#   schemas()            跳过被禁用的工具 —— 模型看不到就调不了；
#                        list_tools() 不变，界面仍能看到全部（含被禁的）。

#: 禁用集合的模块级缓存（sidecar 注入；默认空 = 全部启用）。
_DISABLED_TOOLS: set[str] = set()
_DISABLED_LOCK = threading.Lock()


class ToolToggleError(ValueError):
    """非法的工具开关参数（典型：名字不在注册表里）。

    code/message 两个字段是给 RPC 层用的：sidecar 捕获后原样转成错误响应。
    """

    def __init__(self, message: str, code: str = "UNKNOWN_TOOL") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def disabled() -> set[str]:
    """当前被禁用的工具名集合（返回副本，调用方改不动内部状态）。"""
    with _DISABLED_LOCK:
        return set(_DISABLED_TOOLS)


def set_disabled(names: list[str]) -> None:
    """整表覆盖禁用集合（纯内存，不落盘 —— 落盘是 sidecar 的事）。

    名单里有注册表不认识的名字 → ToolToggleError（UNKNOWN_TOOL），绝不
    默默收下：界面上的一个 typo 不该造出一个永远关不掉/开不掉的幽灵开关。
    """
    cleaned: list[str] = []
    for name in names or []:
        if not isinstance(name, str) or not name.strip():
            raise ToolToggleError("tool name must be a non-empty string",
                                  code="BAD_REQUEST")
        name = name.strip()
        if name not in _REGISTRY:
            raise ToolToggleError("unknown tool: %s" % name)
        if name not in cleaned:
            cleaned.append(name)
    payload = sorted(cleaned)
    with _DISABLED_LOCK:
        _DISABLED_TOOLS.clear()
        _DISABLED_TOOLS.update(payload)


def execute(name: str, args: dict, *, workspace_root: str) -> ToolResult:
    """按名字执行工具。未知工具 → UNKNOWN_TOOL（如实返回，不抛）。"""
    tool = get(name)
    if tool is None:
        return _fail("UNKNOWN_TOOL", "unknown tool: %s" % name)
    args = args or {}
    if not isinstance(args, dict):
        return _fail("BAD_REQUEST", "args must be an object")
    try:
        return tool.run(args, workspace_root=workspace_root)
    except Exception as exc:            # 工具实现不该把异常抛给调用方
        return _fail("IO_ERROR", "tool %s crashed: %s" % (name, exc))


def _reset_for_tests() -> None:
    """清空注册表 + 工具开关状态。测试用；生产代码不许调。"""
    _REGISTRY.clear()
    with _DISABLED_LOCK:
        _DISABLED_TOOLS.clear()


# ── delegate（子代理）────────────────────────────────────────────────────────
# 子代理引擎（python/subagent.py）的实例由 sidecar 装配后挂到这里：本模块不认识它，
# 也不做调度，只负责「模型要派活 → 转给引擎 → 立刻把句柄还给模型」。
# 会话号走线程局部：每轮对话在自己的线程里跑，工具也在同一线程执行。
_DELEGATOR = None
_DELEGATE_CTX = threading.local()

DELEGATE_DESCRIPTION = (
    "Start 1-3 subagent nodes that work on independent goals in parallel. "
    "Returns immediately with a batch id and the node ids; the nodes keep "
    "running in the background and their results are merged into a single "
    "message when the batch finishes. Use it for work that can be split into "
    "independent pieces (research one topic each, review several files, ...). "
    "A node cannot ask the user anything and cannot delegate further, so put "
    "everything it needs into its own goal/context."
)


def set_delegator(delegator) -> None:
    """挂上（或清空）子代理引擎。sidecar 每轮对话在自己的线程里调一次；
    未接线（None）时 delegate 工具会明确报 DELEGATE_UNAVAILABLE，不会静默失败。"""
    _DELEGATE_CTX.delegator = delegator


def get_delegator():
    return getattr(_DELEGATE_CTX, "delegator", None)


def set_delegate_session(session_id: str | None) -> None:
    """记下当前线程这一轮对话的会话号（dispatch 时要写进运行记录）。"""
    _DELEGATE_CTX.session_id = session_id


class DelegateTool(Tool):
    """把活派给最多 3 个并行子代理节点，立刻返回批次句柄。"""

    name = "delegate"
    description = DELEGATE_DESCRIPTION
    parameters = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "What this node must accomplish, stated so it needs no follow-up.",
                        },
                        "context": {
                            "type": "string",
                            "description": "Background only this node needs (paths, constraints, what to return).",
                        },
                    },
                    "required": ["goal"],
                },
            }
        },
        "required": ["tasks"],
    }

    def run(self, args: dict, *, workspace_root: str = "") -> ToolResult:
        delegator = get_delegator()
        if delegator is None:
            return _fail("DELEGATE_UNAVAILABLE",
                         "subagent engine is not wired in this build")
        tasks = args.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return _fail("BAD_REQUEST", "tasks must be a non-empty list of {goal, context}")
        session_id = getattr(_DELEGATE_CTX, "session_id", None) or ""
        try:
            batch = delegator.dispatch(tasks, session_id=session_id)
        except Exception as exc:                    # DelegationError 带 code/message
            return _fail(getattr(exc, "code", "DELEGATE_FAILED"), str(exc))
        nodes = getattr(batch, "node_ids", None) or []
        ids = [str(n) for n in nodes]
        reason = getattr(batch, "limit_reason", "") or ""
        limit_note = ""
        if reason:
            limit_note = " (%s)" % reason
        return _ok(
            "delegated %d node(s): %s — running in parallel%s; results merge into "
            "one message when the batch finishes. You are free to keep talking."
            % (len(ids), ", ".join(ids), limit_note),
            batch_id=getattr(batch, "batch_id", ""),
            node_ids=ids,
            limit=reason,
        )


def register_defaults() -> None:
    """把基础工具注册进全局注册表（可重复调用，幂等覆盖）。"""
    for tool in (ReadFileTool(), ListDirTool(), SearchFilesTool(), WriteFileTool(),
                 EditFileTool(), DeletePathTool(), RunShellTool(),
                 ReadSkillTool(), ListSkillsTool(),
                 SaveRuleTool(), RemoveRuleTool(),
                 SearchConversationsTool(),
                 DelegateTool(),
                 SkillManageTool()):
        register(tool)


register_defaults()
