"""审批记忆（2026-10-14 用户口径：「应该加上类似 hermes 这种 session 内允许、一直允许」）。

一次点头之后，**同样的操作别再问第二遍** —— 但有一条底线：

- **hardline（不可逆）永不记忆**：`rm -rf /`、格式化磁盘、关机这类，弹卡就是弹卡，
  不给「以后别问」的口子。产品不主动替用户记住「允许删根目录」。
- **本会话内允许**（session）：只活在进程内存里、按会话隔离；应用一关就没了。
- **一直允许**（always）：写 config.json 的 ``capabilities.approvalAllow``（原子写回、
  其它键保留），重启后仍然有效 —— 所以必须有撤销入口（``approval.forget`` RPC、
  以及审批卡上「撤销」）。

判定键（signature）：shell 类用归一化后的整条命令，其它工具用「工具名 + 排序后的
参数 JSON」。**刻意不做前缀匹配** —— 允许 `git status` 不等于允许任何 `git ...`：
前缀规则看着省事，实际是把闸门整块让出去（skill agent-approval-model-design §1）。
"""

from __future__ import annotations

import json
import os
import re
import time

#: 允许范围
SCOPE_SESSION = "session"
SCOPE_ALWAYS = "always"

#: 不可记忆的档位（不可逆操作；与 guard 的 tier 同名）
HARDLINE = "hardline"

#: 一直允许最多记多少条（防配置无限膨胀；超了丢最老的）
MAX_ALWAYS = 200

#: 预览文本长度（撤销入口要能让人看懂记住了什么）
PREVIEW_CHARS = 160

_SHELL_TOOLS = ("run_shell", "run_command", "shell", "powershell", "cmd")
_WS_RE = re.compile(r"\s+")

#: 只看不改的命令动词（2026-10-14 用户口径）：允许一次就按动词记住，
#: 换文件的变体不再重复弹卡。读取类**永远**按动词记。
_VERB_SCOPED = frozenset({
    # Unix
    "cat", "head", "tail", "more", "less", "ls", "dir", "find", "grep", "rg",
    "wc", "stat", "file", "tree", "pwd", "diff", "cmp", "which", "whereis",
    "echo", "date", "whoami", "hostname", "uname",
    # Windows / PowerShell
    "type", "get-content", "gc", "get-childitem", "gci", "dir", "findstr",
    "select-string", "sls", "get-item", "gi", "test-path", "measure-object",
    "compare-object", "where.exe", "where", "get-command", "get-process",
    "get-service", "get-date", "systeminfo", "ipconfig", "netstat",
})

#: 命令里的路径（盘符开头，或 ./ ../ / 开头），取「根两段」当记忆范围
_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][^\s\"';|]*"
    r"|(?:^|\s)(?:\.{1,2}[\\/][^\s\"';|]+|[\\/][^\s\"';|]{2,})")


def _target_root(cmd: str) -> str:
    """命令里第一个路径的「根两段」（动词 + 目标目录 记忆用）。

    2026-10-14 用户口径：「命令稍微变动一点就不算同一条」这条要治，但改数据的
    命令不能放开成整个动词 —— 折中成「动词 + 目标目录」：
      `del D:/proj/a.txt` 与 `del D:/proj/b.txt` → 同一条（同目录的变体不再问）
      `del D:/proj/a.txt` 与 `del C:/Windows/x` → 不同条（换目标仍要点头）
    取不到路径就返回空串，调用方退回按动词记。
    """
    m = _PATH_RE.search(str(cmd or ""))
    if not m:
        return ""
    text = m.group(0).strip().replace("\\", "/").rstrip("/")
    parts = [x for x in text.split("/") if x]
    if len(parts) >= 2:
        return "/".join(parts[:2]).lower()
    return text.lower()


def _verb_of(cmd: str) -> str:
    """取命令第一个词（动词），小写、去路径前缀与调用运算符。

    只用于「读类命令按动词记忆」这一件事，取不出来就返回空串（退回整条命令）。
    """
    text = str(cmd or "").strip()
    m = re.match(r"(?:&\s*)?(?:[A-Za-z]:)?[\\/]*(?:[^\\/\s]+[\\/])*([A-Za-z0-9_.-]+)", text)
    return m.group(1).lower() if m else ""


def _preview(value: str) -> str:
    text = value if isinstance(value, str) else str(value or "")
    text = _WS_RE.sub(" ", text).strip()
    return text[:PREVIEW_CHARS]


def signature(name: str, args: dict | None) -> str:
    """判定键：同一条命令 / 同一组参数 = 同一个操作。

    只做「等价写法归一」：压空白、反斜杠统一；**不**小写、不做前缀匹配 ——
    宁可多问一次，也不把「看起来像」的另一次操作一起放行。
    """
    tool = str(name or "")
    data = args if isinstance(args, dict) else {}
    if tool in _SHELL_TOOLS:
        cmd = data.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            # 参数名变了也不能失忆：退回「第一个字符串参数」。
            cmd = next((v for v in data.values() if isinstance(v, str) and v.strip()), "")
        body = _preview(cmd).replace("\\", "/")
        # 2026-10-14 用户口径：判定键分两档，治「命令稍微变动一点就不算同一条」——
        #   · 读类动词（type/cat/Get-Content/findstr…）：按**动词**记，换文件不再问
        #   · 其它动词（del/rm/reg/Remove-Item…）：按**动词 + 目标目录**记，
        #     同目录的变体不再问，换目标目录仍要点头（改数据不能放开成整个动词）
        # hardline 一律不可记忆（can_remember 拦）。
        verb = _verb_of(body)
        if not verb or verb in _VERB_SCOPED:
            return "cmd:%s *" % verb
        root = _target_root(body)
        return "cmd:%s @%s" % (verb, root) if root else "cmd:%s *" % verb
    try:
        blob = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:                                     # pragma: no cover - 兜底
        blob = str(data)
    return "%s:%s" % (tool, _preview(blob).replace("\\", "/"))


def can_remember(tier: str) -> bool:
    """这一档能不能被记住（hardline 永远不能）。"""
    return str(tier or "").strip().lower() != HARDLINE


class ApprovalMemory:
    """一个会话的审批记忆。

    ``always`` 是持久化那份（sidecar 从 config.json 读进来），``on_change`` 在
    「一直允许」变化时被调用（sidecar 负责原子写回）。两者都可缺省 —— 测试直接
    构造即用。
    """

    def __init__(self, session_id: str = "", always: list | None = None,
                 on_change=None) -> None:
        self.session_id = str(session_id or "")
        self._always: list[dict] = [e for e in (always or []) if isinstance(e, dict) and e.get("fp")]
        self._on_change = on_change
        self._session: set[str] = set()

    # -- 查 ---------------------------------------------------------------
    def lookup(self, name: str, args: dict | None) -> str:
        """记住过没有："" / "session" / "always"。"""
        fp = signature(name, args)
        if any(e.get("fp") == fp for e in self._always):
            return SCOPE_ALWAYS
        if fp in self._session:
            return SCOPE_SESSION
        return ""

    # -- 记 ---------------------------------------------------------------
    def remember(self, name: str, args: dict | None, scope: str, tier: str = "") -> bool:
        """记一次允许。hardline 一律拒绝记忆（返回 False，调用方照旧弹卡）。"""
        if not can_remember(tier):
            return False
        fp = signature(name, args)
        if scope == SCOPE_ALWAYS:
            self._always = [e for e in self._always if e.get("fp") != fp]
            self._always.append({
                "fp": fp,
                "tool": str(name or ""),
                "preview": _preview(self._describe(name, args)),
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            })
            while len(self._always) > MAX_ALWAYS:
                self._always.pop(0)
            self._flush()
            return True
        if scope == SCOPE_SESSION:
            self._session.add(fp)
            return True
        return False

    # -- 撤 ---------------------------------------------------------------
    def forget(self, fp: str = "") -> int:
        """撤销一条（空 = 全撤）。返回撤掉的条数。"""
        if not fp:
            removed = len(self._always)
            self._always = []
            self._session.clear()
            if removed:
                self._flush()
            return removed
        before = len(self._always)
        self._always = [e for e in self._always if e.get("fp") != fp]
        self._session.discard(fp)
        removed = before - len(self._always)
        if removed:
            self._flush()
        return removed

    def list_always(self) -> list[dict]:
        return [dict(e) for e in self._always]

    # -- 内部 -------------------------------------------------------------
    def _flush(self) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(self.list_always())
        except Exception:
            # 落盘失败不该连坐对话：这次允许照样生效（内存里已经记了），
            # 只是重启后要重新问一次。
            pass

    @staticmethod
    def _describe(name: str, args: dict | None) -> str:
        data = args if isinstance(args, dict) else {}
        if str(name) in _SHELL_TOOLS:
            return str(data.get("command") or "")
        try:
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception:                                 # pragma: no cover
            return str(data)