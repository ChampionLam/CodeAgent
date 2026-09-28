"""工具失败经验库（自我进化，2026-09-25 用户要求「tool 执行要能自我进化」）。

设计口径（自己控频率，不做黑盒自动总结）：
- 只记「持续失败」的调用模式（同一个调用失败多次 / 工具级冷却），偶发失败不记；
- 键 = 工具 + 目标（host / 路径 / 命令头），计数累加，重复的合并；
- 注入进提示词的条数有硬上限（默认 4 条、每条一行），永不膨胀；
- 同一个键后来成功了 → 立刻降权/删除（自我纠正，不留过时经验）；
- 落盘原子替换，坏文件不影响启动。

运行时用法：sidecar 建一个 LessonStore 放进 LoopDeps.lessons，并在拼系统提示词时
调 ``lines()`` 注入「经验」一节。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time

MAX_ENTRIES = 30          # 库里最多留多少条（超了丢最老/最多的）
MAX_INJECT = 4            # 每次注入提示词最多几条
KEEP_MESSAGE = 160        # 错误原文保留多少字符
DECAY_ON_SUCCESS = 1      # 成功一次降多少权重

#: 工具失败后给模型的可操作替代方案（agent_loop 与提示词注入共用这一份）。
_TOOL_ALTERNATIVES = {
    "web_fetch": "换来源（先用 web_search 拿候选再抓具体页）或换浏览工具；同一个 URL 别再试。",
    "web_search": "换关键词，或直接抓已知的 URL；后端连不上就把这条告诉用户。",
    "read_file": "换路径，或带 pages 参数只读需要的页；提取不出来是文件本身的问题，别重复读。",
    "run_shell": "看 stderr / 退出码改命令；命令不存在就换等价命令，或把失败原因告诉用户。",
    "browser_exec": "换选择器或换页面；连不上就把错误原文交给用户。",
    "browser_navigate": "换地址或先探测可达性；别再原样重试。",
}
_DEFAULT_ALTERNATIVE = "换个工具或换个参数；实在不行把失败原因原样告诉用户，别用同样思路反复试。"


def alternatives_for(tool: str) -> str:
    """某个工具失败后该试什么（提示词和拒绝对话都用它，口径一致）。"""
    return _TOOL_ALTERNATIVES.get(tool, _DEFAULT_ALTERNATIVE)


_HOST_RE = re.compile(r"^https?://([^/:\s]+)", re.I)
_PATH_RE = re.compile(r"^[A-Za-z]:[\\/][^\s]*|[\\/][^\s]*\.\w{1,6}$")
_SHELL_HEAD_RE = re.compile(r"^\s*([\w./\\-]{1,40})")


def target_of(tool: str, args: dict) -> str:
    """从调用参数里提炼「目标」：host / 文件路径 / 命令头。

    只用于把同类失败归到一条经验上，不追求精确。
    """
    if not isinstance(args, dict):
        return ""
    for key in ("url", "target", "link"):
        val = str(args.get(key) or "").strip()
        if val:
            m = _HOST_RE.match(val)
            return (m.group(1) if m else val.split("/")[0])[:80].lower()
    for key in ("path", "file", "filepath", "filename"):
        val = str(args.get(key) or "").strip()
        if val:
            name = val.replace("/", "\\").rsplit("\\", 1)[-1]
            return name[:80].lower()
    for key in ("command", "cmd", "script"):
        val = str(args.get(key) or "").strip()
        if val:
            m = _SHELL_HEAD_RE.match(val)
            return (m.group(1) if m else val)[:40].lower()
    for key in ("query", "q", "pattern"):
        val = str(args.get(key) or "").strip()
        if val:
            return val[:40].lower()
    return ""


def _key(tool: str, args) -> str:
    """经验库主键：工具 + 目标（同目标的不同参数算同一个坑）。"""
    if isinstance(args, str):
        target = args
    else:
        target = target_of(tool, args)
    return "%s|%s" % (tool, target)


class LessonStore:
    """JSON 落盘的工具失败经验库。线程内单实例；并发写用原子替换兜住。"""

    def __init__(self, path: str):
        self.path = path
        self._entries: dict[str, dict] = {}
        self._load()

    # ---------- 落盘 ----------
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            self._entries = {}
            return
        items = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(items, dict):
            self._entries = {}
            return
        clean = {}
        for k, v in items.items():
            if isinstance(v, dict) and v.get("tool"):
                clean[str(k)] = v
        self._entries = clean

    def save(self) -> None:
        payload = {"version": 1, "entries": self._entries}
        folder = os.path.dirname(os.path.abspath(self.path))
        if folder and not os.path.isdir(folder):
            os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".lessons-", dir=folder or None)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ---------- 记账 ----------
    def record(self, tool: str, args, error_code, error_message) -> dict:
        """记一条持续失败。返回写入后的条目。"""
        key = _key(tool, args)
        target = key.split("|", 1)[1] if "|" in key else ""
        now = time.strftime("%Y-%m-%d %H:%M")
        entry = self._entries.get(key) or {
            "tool": tool, "target": target, "code": "", "message": "",
            "count": 0, "first": now, "last": now,
        }
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["code"] = str(error_code or entry.get("code") or "")[:40]
        msg = " ".join(str(error_message or entry.get("message") or "").split())
        entry["message"] = msg[:KEEP_MESSAGE]
        entry["last"] = now
        self._entries[key] = entry
        self._trim()
        self.save()
        return entry

    def note_success(self, tool: str, args) -> bool:
        """同一个坑后来填上了 → 降权，降到 0 就删（不留过时经验）。"""
        key = _key(tool, args)
        entry = self._entries.get(key)
        if not entry:
            return False
        entry["count"] = int(entry.get("count", 1)) - DECAY_ON_SUCCESS
        if entry["count"] <= 0:
            del self._entries[key]
        self.save()
        return True

    def _trim(self) -> None:
        if len(self._entries) <= MAX_ENTRIES:
            return
        order = sorted(self._entries.items(),
                       key=lambda kv: (int(kv[1].get("count", 0)), kv[1].get("last") or ""))
        for key, _ in order[:len(self._entries) - MAX_ENTRIES]:
            del self._entries[key]

    # ---------- 读 ----------
    def entries(self) -> list[dict]:
        return sorted(self._entries.values(),
                      key=lambda e: (-int(e.get("count", 0)), e.get("last") or ""))

    def lines(self, limit: int = MAX_INJECT) -> list[str]:
        """给提示词用的紧凑说明行（按失败次数排序，取前 limit 条）。"""
        out = []
        for e in self.entries()[:max(0, limit)]:
            tool = str(e.get("tool") or "")
            target = str(e.get("target") or "")
            code = str(e.get("code") or "")
            count = int(e.get("count", 0) or 0)
            where = (" %s" % target) if target else ""
            code_txt = ("（%s）" % code) if code else ""
            out.append("%s%s 失败过 %d 次%s → %s"
                       % (tool, where, count, code_txt, alternatives_for(tool)))
        return out

    def block(self, limit: int = MAX_INJECT) -> str:
        """拼成提示词里的一节；没有经验返回空串。"""
        rows = self.lines(limit)
        if not rows:
            return ""
        head = "## 经验（过去工具失败踩过的坑，别再照做）"
        return head + "\n" + "\n".join("- " + r for r in rows)