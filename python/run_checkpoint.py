"""运行断点（run checkpoint）：让「断网 / 断电 / 应用被杀」后的任务能续上。

口径（2026-09-25 用户定）：
- **续跑一律由用户点**，绝不自动接着跑（不偷偷烧 token）；
- 断网：短断自动退避重试 2-3 次，超过约 2 分钟就停下来等用户点；
- 后台任务（subagent）中断时**保留部分产出**，标成中断，等用户点「继续」。

存法：一条运行 = 数据目录下 ``runs/<run_id>.json``，原子写。
**不存消息正文**——消息已经在会话库里，续跑时按 session_id 从会话库重放，
这样断点文件始终只有几百字节，也不会和会话库抢真相。

状态机：``running`` → ``done`` / ``failed`` / ``interrupted`` / ``waiting_user``。
应用启动时把「还写着 running」的运行一律判为中断（单实例应用，启动那一刻不可能
还有别的东西在跑），这是最省事也最不容易误判的判定方式。
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import time

VERSION = 1
MAX_RUNS = 300          # 目录里最多留多少条，超了删最老
KEEP_FIELD = 200        # 文本字段截断
PARTIAL_FIELD = 1500    # 「写了一半的文字」单独放宽：续跑时要靠它接上话头

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_WAITING_USER = "waiting_user"

#: 可续的状态
RESUMABLE = (STATUS_INTERRUPTED, STATUS_WAITING_USER)

#: 允许写进断点文件的字段（别的字段一律丢掉，防止顺手塞进大对象）
_FIELDS = (
    "status", "rounds", "max_loop", "pending", "done_calls", "model", "workspace",
    "reason", "interrupt_hint", "resumed_from", "kind", "session_id", "usage_rounds",
    "partial_text", "partial_round",
)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clip(value, key: str = ""):
    limit = PARTIAL_FIELD if key == "partial_text" else KEEP_FIELD
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return value[:50]
    return value


class RunStore:
    """运行断点库（一个运行一个 JSON 文件，原子写）。"""

    def __init__(self, root: str):
        self.root = root

    # ---------- 内部 ----------
    def _path(self, run_id: str) -> str:
        safe = "".join(c for c in str(run_id) if c.isalnum() or c in "-_")
        return os.path.join(self.root, "%s.json" % safe)

    def _write(self, record: dict) -> dict:
        if not os.path.isdir(self.root):
            os.makedirs(self.root, exist_ok=True)
        path = self._path(record["run_id"])
        fd, tmp = tempfile.mkstemp(prefix=".run-", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(record, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return record

    def _read(self, path: str) -> dict | None:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not data.get("run_id"):
            return None
        return data

    def _prune(self) -> None:
        try:
            files = [os.path.join(self.root, f) for f in os.listdir(self.root)
                     if f.endswith(".json")]
        except OSError:
            return
        if len(files) <= MAX_RUNS:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for path in files[:len(files) - MAX_RUNS]:
            try:
                os.remove(path)
            except OSError:
                pass

    # ---------- 生命周期 ----------
    def start(self, kind: str = "chat", session_id: str = "", model: str = "",
              max_loop: int = 0, owner_pid: int | None = None,
              resumed_from: str = "") -> dict:
        run_id = "r-%s-%04d" % (time.strftime("%Y%m%d%H%M%S"), random.randint(0, 9999))
        record = {
            "version": VERSION,
            "run_id": run_id,
            "kind": kind,
            "session_id": session_id,
            "status": STATUS_RUNNING,
            "rounds": 0,
            "max_loop": int(max_loop or 0),
            "pending": [],
            "done_calls": [],
            "model": model or "",
            "workspace": "",
            "reason": "",
            "interrupt_hint": "",
            "resumed_from": resumed_from or "",
            "owner_pid": int(owner_pid if owner_pid is not None else os.getpid()),
            "started_at": _now(),
            "updated_at": _now(),
        }
        self._prune()
        return self._write(record)

    def update(self, run_id: str, **fields) -> dict | None:
        path = self._path(run_id)
        record = self._read(path)
        if record is None:
            return None
        for key, value in fields.items():
            if key in _FIELDS:
                record[key] = _clip(value, key)
        record["updated_at"] = _now()
        return self._write(record)

    def finish(self, run_id: str, status: str = STATUS_DONE, reason: str = "") -> dict | None:
        return self.update(run_id, status=status, reason=reason, pending=[])

    def interrupt(self, run_id: str, reason: str = "", hint: str = "") -> dict | None:
        return self.update(run_id, status=STATUS_INTERRUPTED, reason=reason,
                           interrupt_hint=hint, pending=[])

    def note_partial(self, run_id: str, text: str, round_no: int | None = None) -> dict | None:
        """记下「这一轮写到一半的正文」。

        为什么要有：点中断/关应用时，主进程会立刻把 sidecar 杀掉，循环根本走不到
        「把这一轮的文字落进会话库」那一步（2026-09-25 真机实测）。所以只能边写边存，
        续跑时靠它把话头接上，不然模型会一脸茫然地问「我先重做哪一步」。
        """
        fields = {"partial_text": text or ""}
        if round_no is not None:
            fields["partial_round"] = int(round_no)
        return self.update(run_id, **fields)

    def get(self, run_id: str) -> dict | None:
        return self._read(self._path(run_id))

    def list_all(self) -> list[dict]:
        try:
            files = [f for f in os.listdir(self.root) if f.endswith(".json")]
        except OSError:
            return []
        out = []
        for name in files:
            rec = self._read(os.path.join(self.root, name))
            if rec:
                out.append(rec)
        out.sort(key=lambda r: r.get("updated_at") or "")
        return out

    # ---------- 查询 ----------
    def latest_for_session(self, session_id: str, statuses=None) -> dict | None:
        rows = [r for r in self.list_all()
                if r.get("session_id") == session_id
                and (statuses is None or r.get("status") in statuses)]
        return rows[-1] if rows else None

    def pending_for_session(self, session_id: str) -> dict | None:
        """这个会话有没有「可续」的运行（给界面显示「继续」按钮用）。"""
        return self.latest_for_session(session_id, statuses=RESUMABLE)

    def sweep_on_start(self, reason: str = "应用退出或断电") -> list[dict]:
        """启动时把还在 running 的运行判为中断；返回被判中断的那些。"""
        touched = []
        for rec in self.list_all():
            if rec.get("status") == STATUS_RUNNING:
                updated = self.interrupt(rec["run_id"], reason=reason)
                if updated:
                    touched.append(updated)
        return touched