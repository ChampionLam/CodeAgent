"""
desk-agent Python sidecar (M1-min).

Protocol on stdin/stdout (LSP-style framing):
    Content-Length: <N>\r\n\r\n<JSON payload>

Frame payload is a JSON object with:
    {"type":"req",  "id":"<uuid>", "method":"<m>", "params":{...}}
    {"type":"res",  "id":"<uuid>", "ok":true|false, "result"|"error":...}
    {"type":"evt",  "event":"<name>", "data":{...}}

HARD RULES enforced here:
  * R1  Logs go to STDERR only. stdout is reserved for frames.
  * R2  Frame = utf-8 JSON, exact Content-Length bytes. No trailing newline.
  * R3  protocolVersion is required and must equal SIDECAR_PROTOCOL_VERSION.

Supported methods (M1 + M2):
    ping -> {"pong": true, "ts": <unix-seconds>}
    info -> {"backendVersion", "protocolVersion", "capabilities", "pid"}
    tools.list -> {"tools", "levels", "canAlwaysAllow"}（每个条目带 enabled）
    skills.list -> {"skills": [...], "skipped": [[path, reason], ...]}
    tools.setEnabled -> 同 tools.list 的返回结构；参数 {"name", "enabled"}，
            落盘到 config.json 的 capabilities.toolsDisabled（非法工具名报错）
    chat -> {"accepted": true}; the reply streams back as chat.reasoning /
            chat.delta / chat.done / chat.error events, plus tool.call /
            tool.result, and approval.request when a command matches a
            dangerous pattern and needs a nod.
            Params: messages, model?, maxTokens?, sessionId?, workspaceRoot?,
            systemPrompt?, images? (paths or attach: refs, attached to the last
            user message), useTools? (default false), maxLoop?.
    chat.cancel -> {"cancelled": bool}
    approval.respond -> {"accepted": bool}; params: approvalId, decision
            (allow_once | allow_always | deny)

On startup, sends an unsolicited `evt hello` frame (R3 verification).
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
import tempfile
import threading
import time
import uuid

# Make sibling modules (llm, reasoning_split) importable even when this file
# is imported as a module from elsewhere (sys.path may not include our dir).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_loop  # noqa: E402
import appconfig  # noqa: E402
import attachments  # noqa: E402
import audit as audit_mod  # noqa: E402
import chat_persist  # noqa: E402
import context_mechanism  # noqa: E402
import guard  # noqa: E402
import llm  # noqa: E402
import modelconfig  # noqa: E402
import ocr_fallback  # noqa: E402
import prompt_build  # noqa: E402
import read_extract  # noqa: E402
import sessionstore  # noqa: E402
import skills_registry  # noqa: E402  (skills.list RPC 的数据源)
import tools  # noqa: E402
import reasoning_split  # noqa: E402  (思考/正文分流：只留正文)
import providers.gen_tool  # noqa: E402,F401  (import 副作用：注册 image_generate 工具)
import tools_web  # noqa: E402,F401  (import 副作用：注册 web_fetch / web_search 工具)
import subagent  # noqa: E402  (子代理引擎：Delegator / NodeResult / QueueError)
from reasoning_split import ReasoningSplitter  # noqa: E402

SIDECAR_PROTOCOL_VERSION: int = 1
SIDECAR_BACKEND_VERSION: str = "0.3.0"
SIDECAR_CAPABILITIES: list[str] = [
    "ping", "info", "chat", "chat.cancel", "approval.respond", "tools.list",
    "skills.list", "tools.setEnabled",
    "models.list", "models.presets", "models.upsert", "models.remove",
    "models.setDefault", "models.fetch",
    "sessions.list", "sessions.create", "sessions.rename", "sessions.delete",
    "sessions.messages", "messages.search",
    "usage.summary",
    "run.pending", "run.active", "run.interrupt", "run.dismiss",
]


def log(level: str, msg: str) -> None:
    """All logging goes to stderr. Never use print()."""
    sys.stderr.write(f"[sidecar {time.strftime('%H:%M:%S')}] {level}: {msg}\n")
    sys.stderr.flush()


def _active_provider() -> str | None:
    """当前默认模型的厂商 key，用来在目录里挑对同名模型的价格。"""
    try:
        data = modelconfig.load_models()
        items = data.get("items") or []
        active = data.get("activeId")
        for it in items:
            if it.get("id") == active:
                return it.get("provider")
        return items[0].get("provider") if items else None
    except Exception:  # 配置坏了不该拖垮用量查询
        return None


class ProtocolError(Exception):
    """Raised when a frame is malformed or violates protocol rules."""


class FrameError(Exception):
    """Raised when JSON payload is malformed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Frame I/O
# ---------------------------------------------------------------------------


def _read_exact(stream, n: int) -> bytes:
    """Read exactly n bytes; raise ProtocolError on EOF before n."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise ProtocolError("EOF before header complete")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(stream) -> dict:
    """Parse one LSP-style frame from a binary stream (sys.stdin.buffer).

    Handles:
      * sticky packets: returns only the first complete frame, leaving the
        rest in the OS pipe buffer for the next call.
      * half packets: blocks until more bytes arrive (or EOF).

    The header is `Content-Length: <N>\r\n\r\n` and N is the number of UTF-8
    bytes of the JSON body. We then decode with `strict=False` to handle
    multi-byte UTF-8 boundaries split across reads (the header guarantees
    we read exactly N bytes, but decode must still tolerate splits).
    """
    # Read header up to and including the blank line.
    header = bytearray()
    seen_length: int | None = None
    while True:
        ch = stream.read(1)
        if not ch:
            if header:
                raise ProtocolError("EOF mid-header")
            raise ProtocolError("EOF")
        header.extend(ch)
        # Look for \r\n\r\n boundary.
        if header.endswith(b"\r\n\r\n"):
            break
        # Defensive: cap header size.
        if len(header) > 1024:
            raise ProtocolError("header too large")

    text = header.decode("ascii", errors="replace")
    seen_length = None
    for line in text.split("\r\n"):
        if not line:
            continue
        if line.lower().startswith("content-length:"):
            try:
                seen_length = int(line.split(":", 1)[1].strip())
            except ValueError as exc:
                raise ProtocolError(f"bad Content-Length: {line!r}") from exc
        # Other headers are ignored; spec only defines Content-Length.
    if seen_length is None:
        raise ProtocolError("missing Content-Length")
    if seen_length < 0 or seen_length > 64 * 1024 * 1024:
        raise ProtocolError(f"Content-Length out of range: {seen_length}")

    body = _read_exact(stream, seen_length)
    # `strict=False` lets the decoder buffer a partial multi-byte codepoint
    # across reads. Since we already read the full body, this is just a
    # safety net for future chunked reads. Errors still raise.
    try:
        text_body = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProtocolError(f"utf-8 decode error: {exc}")
    try:
        payload = json.loads(text_body)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"json decode error: {exc}")
    if not isinstance(payload, dict):
        raise ProtocolError("payload is not a JSON object")
    return payload


def _write_frame_unlocked(payload: dict, out=None) -> None:
    """Serialize and write a single LSP-style frame to a binary stream.

    Defaults to ``sys.stdout.buffer``. Tests can pass an in-memory buffer.
    """
    if out is None:
        out = sys.stdout.buffer
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    out.write(header)
    out.write(body)
    out.flush()



# Thread-safe stdout
# A chat runs on a worker thread while the main loop keeps servicing stdin,
# so ping/chat.cancel stay responsive during a long stream.  Both threads
# write frames to the same stdout, so writes must be serialized -- without
# this lock two frames can interleave and corrupt the stream.
_WRITE_LOCK = threading.Lock()

# Chat state: _CHATS maps req_id -> {"cancel": bool}. _CHAT_LOCK guards it.
_CHAT_LOCK = threading.Lock()
_CHATS: dict = {}

# Config cache: lazily loaded once by _chat_config(); on failure nothing is
# cached so the next chat retries the load.
_CFG_LOCK = threading.Lock()

# Per-chat scratch shared with the L2 summarizer: the tool schemas the main
# session is sending this round. Each chat runs on its own thread (see
# handle_request), so a thread-local is the right scope. The summarizer
# repeats them so the provider's cache prefix (tools -> system -> history)
# matches the main session byte for byte instead of diverging at byte zero.
_CHAT_TLS = threading.local()
_CFG = None


def write_frame(payload: dict, out=None) -> None:
    """Serialize frame writes across the main loop and chat worker threads."""
    with _WRITE_LOCK:
        _write_frame_unlocked(payload, out)


def _chat_config() -> "llm.ModelConfig":
    """Load (once) and return the model config. Errors propagate, never cached."""
    global _CFG
    with _CFG_LOCK:
        if _CFG is None:
            _CFG = llm.load_config()
        return _CFG


def _spill_dir(base_dir: str) -> str:
    """L1 spill directory: a sibling of the base dir, so it lands on the same disk."""
    parent = os.path.dirname(os.path.abspath(base_dir or "")) or os.path.expanduser("~")
    return os.path.join(parent, "spill")


def _make_summarizer(cfg: "llm.ModelConfig"):
    """L2 mid-section summarizer. Contract section 7.8: v1 uses the main model only.

    Two payload shapes, one model:
      * list of messages — the governor already assembled a cache-sharing
        request (main-session system prefix + middle + summary instruction);
        dispatch it verbatim via chat_messages_once so the request prefix is
        byte-identical to the main session's and the provider's automatic
        prefix cache hits instead of re-prefilling a cold prompt.
      * plain string — legacy fallback (no session prefix available): build
        the old single cold user prompt, byte-for-byte the pre-change shape.
    Failure contract is the same one chat_once always had: LlmError raises,
    the governor/loop degrade exactly as before.
    """
    def _summarize(payload):
        if isinstance(payload, list):
            # Same tools array the loop is sending this round, so providers
            # that key the prefix on tool definitions (tools first) still hit.
            tools_arg = getattr(_CHAT_TLS, "tools", None)
            if tools_arg:
                return llm.chat_messages_once(cfg, payload, tools=tools_arg)
            return llm.chat_messages_once(cfg, payload)
        prompt = (
            "把下面这段较早的对话压缩成一段客观摘要，保留：用户的目标、已确认的决定、"
            "涉及的文件路径与关键事实、尚未完成的事项。不要评论，不要用 Markdown 标题。\n\n"
            + payload
        )
        return llm.chat_once(cfg, prompt)
    # Opt in to the governor's cache-sharing request shape (message list).
    # Without this attribute the governor keeps the legacy flattened-text
    # summary request, so old wiring and tests are unaffected.
    _summarize.accepts_messages = True
    return _summarize


def _emit(event: str, data: dict) -> None:
    """Send one evt frame to the client."""
    write_frame({"type": "evt", "event": event, "data": data})


# ---------------------------------------------------------------------------
# Agent 能力接线（数据目录 / 审计 / 审批通道 / 图片附件）
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DATA_DIR = os.environ.get("DESK_AGENT_DATA_DIR") or os.path.join(_REPO_ROOT, "data")
#: 工具失败经验库（自我进化）：落盘在数据目录，进程内单例。
_LESSONS = None


def _lessons_block() -> str:
    """给系统提示词的「经验」一节；没有经验返回空串。"""
    store = _lessons_store()
    if store is None:
        return ""
    try:
        return store.block()
    except Exception as exc:                                 # noqa: BLE001
        log("warn", "tool lessons block failed: %s" % exc)
        return ""


def _lessons_store():
    """工具失败经验库单例；建不起来就返回 None（不影响主流程）。"""
    global _LESSONS
    if _LESSONS is None:
        try:
            import tool_lessons
            os.makedirs(_DATA_DIR, exist_ok=True)
            _LESSONS = tool_lessons.LessonStore(
                os.path.join(_DATA_DIR, "tool_lessons.json"))
        except Exception as exc:                             # noqa: BLE001
            log("warn", "tool lessons unavailable: %s" % exc)
            return None
    return _LESSONS

# 审批槽：approvalId -> {"event": threading.Event, "decision": str|None}
_APPROVAL_LOCK = threading.Lock()
_APPROVALS: dict = {}

_AUDIT_LOCK = threading.Lock()
_AUDIT = None

# Sessions store (sessions.db), same lazy-open pattern as the audit store:
# one connection for the process, own lock. Migration happens on first open.
# If the DB cannot be opened at all we log a warning and continue without
# persistence -- chats must never die because the disk did.
_SESSIONS_LOCK = threading.Lock()
_SESSIONS: object = None


_RUNS = None


def _run_store():
    """运行断点库（data/runs/）。不可用返回 None，绝不因此打断对话。"""
    global _RUNS
    if _RUNS is None:
        try:
            import run_checkpoint
            os.makedirs(_DATA_DIR, exist_ok=True)
            _RUNS = run_checkpoint.RunStore(os.path.join(_DATA_DIR, "runs"))
        except Exception as exc:                          # noqa: BLE001
            log("warn", "run checkpoint unavailable: %s" % exc)
    return _RUNS


def _run_status_consts():
    import run_checkpoint
    return run_checkpoint


def _checkpoint_handler(run_id: str):
    """把 loop 报来的运行状态写进断点库。

    收尾只认「当前还是 running」才落终态，免得被后面的 end 事件覆盖了中断标记。
    """
    def _handle(payload):
        store = _run_store()
        if store is None:
            return
        rc = _run_status_consts()
        try:
            kind = str(payload.get("event") or "")
            if kind == "interrupt":
                store.interrupt(run_id, reason=payload.get("reason") or "中断",
                                hint=payload.get("interrupt_hint") or "")
                return
            if kind == "end":
                rec = store.get(run_id) or {}
                if rec.get("status") != rc.STATUS_RUNNING:
                    return
                want = str(payload.get("status") or "done")
                if want == "interrupted":
                    store.interrupt(run_id, reason=payload.get("reason") or "中断",
                                    hint=payload.get("interrupt_hint") or "")
                else:
                    store.finish(run_id, status=want, reason=payload.get("reason") or "")
                    # 正常收尾：这一轮的正文已经落进会话库，半截快照没用了，清掉。
                    store.note_partial(run_id, "")
                return
            if kind == "partial":
                store.note_partial(run_id, str(payload.get("text") or ""),
                                   payload.get("rounds"))
                return
            fields = {k: payload[k] for k in ("rounds", "max_loop", "done_calls", "pending")
                      if k in payload}
            if kind == "round":
                # 新的一轮意味着上一轮的正文已经正经落进会话库了，半截快照可以清掉。
                fields["partial_text"] = ""
            if fields:
                store.update(run_id, **fields)
        except Exception as exc:                          # noqa: BLE001
            log("warn", "run checkpoint write failed: %s" % exc)   # 断点写失败绝不影响对话

    return _handle

def _api_messages_from_rows(rows: list) -> list:
    """会话库的行 -> 厂商 API 形态的消息（2026-09-27 新增）。

    会话库里存的是**界面形态**（驼峰 `toolCallId` / `toolCalls`，给渲染层用），
    而发给厂商必须用 API 形态（`tool_call_id` / `tool_calls` 下划线）。
    续跑时直接把界面形态的 dict 丢给厂商 -> tool 消息的对不上 id ->
    HTTP 400 "tool messages must include a non-empty string tool_call_id"
    （错误里点名 messages[4] 就是这么来的）。

    另外顺手丢掉「孤儿 tool 行」：没有对应 assistant tool_call 的 tool 消息
    发出去同样是 400（厂商要求 tool 必须紧跟声明它的 assistant）。
    """
    out: list = []
    declared: set = set()          # 已经发给厂商的 assistant tool_call id
    for m in rows or []:
        role = m.get("role")
        if role == "tool":
            tcid = str(m.get("toolCallId") or m.get("tool_call_id") or "")
            if not tcid or tcid not in declared:
                continue
            out.append({"role": "tool", "tool_call_id": tcid,
                        "content": m.get("content") or ""})
        elif role == "assistant":
            calls = m.get("toolCalls") or m.get("tool_calls") or []
            msg = {"role": "assistant", "content": m.get("content") or ""}
            if calls:
                msg["tool_calls"] = calls
                for c in calls:
                    if isinstance(c, dict) and c.get("id"):
                        declared.add(c["id"])
            out.append(msg)
        elif role:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def _close_resumed_run(run_id: str) -> None:
    """续跑这一轮真跑完了，才把旧断点收尾（见 _apply_resume 里的说明）。"""
    if not run_id:
        return
    store = _run_store()
    if store is None:
        return
    try:
        store.finish(run_id, status=_run_status_consts().STATUS_DONE, reason="已续跑")
        # 「半截正文」已经喂回给模型了，留着没用了。
        store.note_partial(run_id, "")
    except Exception as exc:                              # noqa: BLE001
        log("warn", "resumed run close failed: %s" % exc)


def _resume_note(prev: dict) -> str:
    """续跑时给模型的一句交代（用 system 角色，不会被当成用户发言落库）。"""
    done = prev.get("done_calls") or []
    note = ("[续跑] 上一轮任务在 %s 被中断（原因：%s），大约停在第 %s 轮。"
            "已执行过的工具调用不会重做（共 %d 个）。请接着把这件事做完，"
            "已经完成的部分不要重复做。"
            % (prev.get("updated_at") or "?", prev.get("reason") or "未知",
               prev.get("rounds") or "?", len(done)))
    partial = str(prev.get("partial_text") or "").strip()
    if partial:
        note += ("\n[中断时你正在写的正文] 下面这些是你被打断前已经写出来的内容，"
                 "可能没写完，也还没有落进会话历史。**接着它往下写或据此继续干活**，"
                 "不要从头再写一遍，也不要问用户「要我重做哪一步」：\n"
                 "----\n%s\n----" % partial)
    return note


def _apply_resume(params: dict, messages: list) -> tuple[list, str]:
    """请求里带 resumeRunId 时：用会话库里的消息重建上下文 + 加续跑交代。

    返回 (messages, resumed_from)；没得续就原样返回。
    """
    run_id = str(params.get("resumeRunId") or "")
    if not run_id:
        return messages, ""
    try:
        store = _run_store()
        if store is None:
            return messages, ""
        rc = _run_status_consts()
        prev = store.get(run_id)
        if not prev or prev.get("status") not in rc.RESUMABLE:
            log("warn", "resume requested but not resumable: %s" % run_id)
            return messages, ""
        sid = prev.get("session_id") or params.get("sessionId")
        rebuilt = []
        if sid:
            try:
                rebuilt = _session_store().get_messages(sid, include_inactive=False)
            except Exception as exc:                      # noqa: BLE001
                log("warn", "resume rebuild failed: %s" % exc)
        if rebuilt:
            # 界面形态 -> API 形态（少了这步 tool 消息的 id 就丢了，厂商 400）
            messages = _api_messages_from_rows(rebuilt)
        messages = list(messages) + [{"role": "system", "content": _resume_note(prev)}]
        # 2026-09-27 修：原来在这里就把断点标成「已续跑」。可这会儿请求还没发出去，
        # 厂商一报错（如 HTTP 400 tool_call_id）断点已经被消费掉、ResumeBar 也消失了，
        # 用户连重试的机会都没有。改成等这一轮真跑完再收尾 —— 见 _close_resumed_run。
        log("INFO", "resume: %s -> session %s (%d msgs)" % (run_id, sid, len(messages)))
        return messages, run_id
    except Exception as exc:                              # noqa: BLE001
        log("warn", "resume failed, falling back to client history: %s" % exc)
        return messages, ""


def _session_store():
    """Session store (lazy-open once). Unavailable -> None, chat continues."""
    global _SESSIONS
    with _SESSIONS_LOCK:
        if _SESSIONS is None:
            try:
                os.makedirs(_DATA_DIR, exist_ok=True)
                path = os.path.join(_DATA_DIR, "sessions.db")
                _SESSIONS = sessionstore.SessionStore(path)
                log("INFO", "sessions db: %s (schema version %s)"
                    % (path, _SESSIONS.schema_version()))
            except Exception as e:
                log("WARN", "session store unavailable: %r" % (e,))
                _SESSIONS = False
        return _SESSIONS or None


def _resolve_since(token: str) -> str | None:
    """把 since 说法换算成库里的 UTC 时间底线（today / yesterday / 7d / ISO 日期）。

    库里的 created_at 是 datetime('now')，也就是 UTC；用户说的「今天」是本地时间，
    所以这里按本机本地时区把当天的 0 点换算成 UTC，否则东八区会差 8 小时。
    """
    import datetime as _dt
    t = (token or "").strip().lower()
    if not t:
        return None
    now_local = _dt.datetime.now()
    today0 = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    words = {"today": 0, "今天": 0, "yesterday": 1, "昨天": 1}
    if t in words:
        start = today0 - _dt.timedelta(days=words[t])
    elif re.fullmatch(r"\d+d", t):
        start = today0 - _dt.timedelta(days=int(t[:-1]) - 1)
    else:
        try:
            start = _dt.datetime.fromisoformat(t)
        except ValueError:
            return None
        if start.hour == 0 and start.minute == 0:
            start = start.replace(tzinfo=None)
    offset = _dt.datetime.utcnow() - now_local      # 本地 → UTC 的偏移
    return (start + offset).strftime("%Y-%m-%d %H:%M:%S")


def _search_conversations(query: str = "", since: str = "", limit: int = 20) -> list[dict]:
    """跨会话检索历史消息 —— 供 search_conversations 工具取数（2026-09-25 接出）。

    能力本来就在库里（FTS5 trigram 中文可搜），只是从没做成模型能调的工具，
    于是模型会说「我这边不存你之前的聊天内容」。这里把它接上：带上会话标题与
    日期，让「上次那个」「我今天问了什么」有真正的一手答案。只读，不改数据。
    """
    store = _session_store()
    if store is None or (not query and not since):
        return []

    # 只给 since（没有关键词）：按时间列出 —— 「我今天问了什么」就是这种
    if not query:
        bound = _resolve_since(since)
        if bound is None:
            return []
        rows = store.recent_messages(since=bound, limit=int(limit))
        return [{
            "sessionId": r["sessionId"],
            "sessionTitle": r["sessionTitle"],
            "when": str(r["createdAt"] or "")[:16],
            "role": r["role"],
            "snippet": (r["content"] or "").strip()[:200],
        } for r in rows]

    hits = store.search_messages(query, limit=int(limit))
    out: list[dict] = []
    for h in hits:
        row = store.get_session(h["sessionId"]) or {}
        out.append({
            "sessionId": h["sessionId"],
            "sessionTitle": row.get("title") or "",
            # 只留到分钟：给模型看的是「什么时候」，不是精度
            "when": str(row.get("updated_at") or row.get("created_at") or "")[:16],
            "role": h.get("role") or "",
            "ordinal": h.get("ordinal"),
            "snippet": (h.get("snippet") or "").strip(),
        })
    return out


# 工具层只认「给我 query、还我 hits」这个约定（见 tools.set_conversation_search）
tools.set_conversation_search(_search_conversations)


def model_name_for_chat(cfg) -> str:
    """Model name stamped on the persisted session row (never None)."""
    if cfg is None:
        return "unknown"
    return str(getattr(cfg, "model", None) or "unknown")


def base_url_for_chat(cfg):
    """Base URL stamped on the persisted session row (None when unknown)."""
    if cfg is None:
        return None
    value = getattr(cfg, "base_url", None)
    return value if isinstance(value, str) and value else None


# ------------------------------------------------------------------ memory
#
# The engine (Mnemosyne) runs as its own process; hooks own the two turn-
# boundary calls. Everything here is best-effort: if the package is not
# installed the hooks stay disabled, and any engine failure means "this turn
# simply has no memory", never an error in the chat path.
_MEMORY_HOOKS = None
_MEMORY_READY = None


def _memory_hooks():
    """Lazily build the hooks, or None when the engine cannot be used."""
    global _MEMORY_HOOKS, _MEMORY_READY
    if _MEMORY_READY is not None:
        return _MEMORY_HOOKS
    _MEMORY_READY = False
    try:
        import importlib.util
        if importlib.util.find_spec("mnemosyne") is None:
            log("info", "memory engine not installed: memory hooks disabled")
            return None
        from memory import memory_client, memory_hooks
        engine = memory_client.MemoryEngine(home=os.path.join(_DATA_DIR, "memory"))
        _MEMORY_HOOKS = memory_hooks.MemoryHooks(
            engine, warn=lambda level, msg: log(level, "memory: %s" % msg))
        _MEMORY_READY = True
    except Exception as exc:                    # pragma: no cover - defensive
        log("warn", "memory hooks unavailable: %s" % exc)
        _MEMORY_HOOKS = None
    return _MEMORY_HOOKS


def _last_user_text(messages) -> str:
    for msg in reversed(list(messages or [])):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            return content if isinstance(content, str) else ""
    return ""


def _last_reply_text(messages) -> str:
    for msg in reversed(list(messages or [])):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content")
            return content if isinstance(content, str) else ""
    return ""


def _memory_text():
    """Recalled memory for this turn (never blocks on the engine)."""
    hooks = _memory_hooks()
    if hooks is None:
        return None
    try:
        return hooks.injection()
    except Exception:                           # pragma: no cover - defensive
        return None


def _base_dir(params_ws=None) -> str:
    """工具解析相对路径用的基准目录（「当前目录」口径，2026-09-25 起）。

    请求参数优先，其次配置 capabilities.workspaceRoot（旧键，兼容存量配置），
    默认用户主目录。**不参与权限判定**（判定在 guard.py，只看命令模式），
    系统提示词里也不再出现。
    """
    if isinstance(params_ws, str) and params_ws.strip():
        return os.path.abspath(os.path.expanduser(params_ws.strip()))
    configured = appconfig.capability("workspaceRoot", "") or ""
    if isinstance(configured, str) and configured.strip():
        return os.path.abspath(os.path.expanduser(configured.strip()))
    return os.path.expanduser("~")


def _audit_store():
    """审计库（懒开一次）。开不起来就返回 None —— 审计不应挡住对话。"""
    global _AUDIT
    with _AUDIT_LOCK:
        if _AUDIT is None:
            try:
                os.makedirs(_DATA_DIR, exist_ok=True)
                _AUDIT = audit_mod.AuditStore(os.path.join(_DATA_DIR, "audit.db"))
                log("INFO", "audit db: %s" % os.path.join(_DATA_DIR, "audit.db"))
            except Exception as e:
                log("WARN", "audit store unavailable: %r" % (e,))
                _AUDIT = False
        return _AUDIT or None


def _attachment_store() -> "attachments.AttachmentStore":
    return attachments.AttachmentStore(os.path.join(_DATA_DIR, "attachments"))


def _vision_policy() -> "attachments.ImagePolicy":
    cfg = appconfig.capability("vision", {}) or {}
    return attachments.ImagePolicy(
        max_input_bytes=int(cfg.get("maxInputBytes", 20 * 1024 * 1024)),
        resize_target_bytes=int(cfg.get("resizeTargetBytes", 5 * 1024 * 1024)),
        max_dimension=int(cfg.get("maxDimension", 2048)),
    )


# ---------------------------------------------------------------------------
# 扫描件 OCR 兜底（契约见 ocr_fallback.py 模块头）
# ---------------------------------------------------------------------------

def _ocr_fallback_cfg() -> "ocr_fallback.HostedOcrConfig | None":
    """云端 OCR 配置。key 只从环境变量取，绝不进日志/返回值。"""
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip() or None
    if not key:
        return None
    section = appconfig.capability("ocr", {}) or {}
    if not isinstance(section, dict) or section.get("hosted") is False:
        return None
    url = section.get("apiUrl") or None
    return ocr_fallback.HostedOcrConfig(api_key=key, api_url=url)


def _vision_text_only(raw: str) -> str:
    """只留正文：MiniMax 这类端点把思考内联在正文里（<thinking>…</thinking>）。

    2026-09-28：vision 预读以前直接把 chat_messages_once 的原文注入，结果把模型
    整段思考也塞进了主对话上下文。这里复用 vision 模块现成的分流器只取正文；
    分流后正文为空则退回原文 —— 宁可多带一点，也不能把答案吃空。
    """
    text = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    try:
        sp = reasoning_split.ReasoningSplitter()
        r1, t1 = sp.feed(text)
        r2, t2 = sp.flush()
        body = (t1 + t2).strip()
    except Exception:
        return text
    return body or text


def _ocr_chat() -> "callable | None":
    """本地路的多模态模型入口：复用 vision 线路（llm.chat_messages_once）。

    vision 没配或模型不吃图 → 返回 None（走不了本地路，只剩提示）。
    """
    cfg = appconfig.capability("vision", {}) or {}
    if not isinstance(cfg, dict) or not (cfg.get("provider") or cfg.get("model")):
        return None
    base_cfg = None
    try:
        base_cfg = _chat_config()
    except Exception:
        base_cfg = None
    if base_cfg is None:
        return None
    from dataclasses import replace as _dc_replace

    vision_cfg = _dc_replace(
        base_cfg,
        model=str(cfg.get("model") or base_cfg.model),
        base_url=str(cfg.get("baseUrl") or base_cfg.base_url),
        provider=str(cfg.get("provider") or base_cfg.provider or "custom"),
        timeout_seconds=int(cfg.get("timeoutSeconds", 180)),
    )
    key_env = cfg.get("apiKeyEnv")
    if isinstance(key_env, str) and key_env.strip():
        vision_cfg.api_key = os.environ.get(key_env.strip(), "").strip() or vision_cfg.api_key
    def _read(messages, **kw):
        return _vision_text_only(llm.chat_messages_once(vision_cfg, messages, **kw))

    return _read


def _wire_ocr_fallback() -> None:
    """把 OCR 兜底接到 ReadFileTool（进程启动时装配一次）。"""
    hosted_cfg = _ocr_fallback_cfg()
    chat = _ocr_chat()
    if hosted_cfg is None and chat is None:
        # 两条路都没有：保持 None，read_file 会返回提示而不是兜底。
        tools.ReadFileTool.ocr_fallback = None
        return
    limits = ocr_fallback.OcrLimits(
        max_pages=int((appconfig.capability("ocr", {}) or {}).get("maxPages", 8)),
        max_dimension=int((appconfig.capability("ocr", {}) or {}).get("maxDimension", 1600)),
        dpi=int((appconfig.capability("ocr", {}) or {}).get("dpi", 150)),
    )

    def _fallback(path: str, exc: Exception, pages=None) -> tuple:
        # pages 非空 = 模型点名续读哪些页（read_file 的 pages 参数）。
        return ocr_fallback.extract_with_fallback(
            path, exc=exc, hosted_cfg=hosted_cfg, chat=chat, limits=limits,
            pages_override=pages)

    tools.ReadFileTool.ocr_fallback = staticmethod(_fallback)


def _ocr_status() -> dict:
    """给 info / 诊断用的 OCR 状态（不含任何 key）。"""
    hosted = _ocr_fallback_cfg() is not None
    local = _ocr_chat() is not None
    return {"hosted": hosted, "vision": local,
            "anydoc": read_extract.anydoc_available()}


def _attach_images(messages: list, images: list, chat_model: str = "") -> list:
    """把图片挂到最后一个 user 消息上（content 数组 + data URL）。

    图片先落附件库、过尺寸策略，再转 data URL。任何一张处理失败都直接抛，
    由调用方转成 chat.error —— 不静默丢图。

    文档附件（2026-09-25 起）：非 image/* 的可提取文档不再被拒。原始字节
    照样进附件库（内容寻址），提取出的文本拼成上下文块附加到最后一条
    user 消息的文本前面 —— 模型直接读到文档内容，不需要自己去 read_file。
    """
    store = _attachment_store()
    policy = _vision_policy()
    blocks = []
    doc_refs = []
    for src in images:
        if not isinstance(src, str) or not src.strip():
            continue
        src = src.strip()
        ref = store.parse_uri(src) if src.startswith(attachments.ATTACH_SCHEME) \
            else store.save_file(src)
        if ref.is_document:
            doc_refs.append(ref)
            continue
        ref_after, meta = attachments.normalize_image(store, ref, policy)
        if not meta.get("is_image"):
            # 不是图片就别硬塞：否则会发出一个 data:text/plain 的假图片块
            raise attachments.AttachmentError(
                "UNSUPPORTED_IMAGE",
                "不是图片，无法作为图片附件送出：%s（实际识别为 %s）" % (src, ref.mime))
        blocks.append({"type": "image_url",
                       "image_url": {"url": attachments.to_data_url(store, ref_after)}})
    # 文档：提取文本注入上下文（提取失败 = 每份一条说明，不挡整轮对话）。
    doc_text = ""
    if doc_refs:
        doc_text = attachments.document_context_text(store, doc_refs)
    # 图先过 vision 线路（2026-09-28 用户报「为什么没有读图片的能力」）：
    # 会话模型没声明吃图时，图块发过去等于白给 —— 先让 vision 模型读成文字
    # 并进上下文，主模型照着文字答。声明了吃图就直接发，不重复调一次。
    image_text = ""
    if blocks and not appconfig.model_declares_image(chat_model):
        _read = _ocr_chat()
        if _read is not None:
            _ask = [{
                "role": "user",
                "content": blocks + [{"type": "text", "text": (
                    "请用中文描述图片内容；如果是截图、报错、表格或代码，"
                    "把里面的文字尽量原样抄出来。")}],
            }]
            try:
                try:
                    # 读图不需要思考链：省 token，别把草稿也喂给主模型
                    image_text = (_read(_ask, thinking=False) or "").strip()
                except TypeError:
                    # 该线路不吃 thinking 参数（llm 层按 provider 判定）
                    image_text = (_read(_ask) or "").strip()
            except Exception as exc:
                log("WARN", "vision 读图失败，继续不带描述：%s" % exc)
    if blocks or doc_text or image_text:
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            text_part = content if isinstance(content, str) else ""
            context = "\n\n".join(x for x in (image_text, doc_text) if x)
            if context:
                prefix = "%s\n\n" % context if text_part else context
                text_part = prefix + text_part
            if blocks:
                new_content = blocks + ([{"type": "text", "text": text_part}] if text_part else [])
                msg["content"] = new_content
            else:
                msg["content"] = text_part
            break
    return messages


def _wait_for_approval(chat_id: str, payload: dict, timeout: float = 300.0) -> str:
    """等客户端回复审批。

    注意：approval.request 帧由 agent_loop 的 yield 发出（_run_chat 统一转帧），
    这里**只负责等**，不要再 emit 一次 —— 会变成弹两次（实测踩过）。
    """
    approval_id = payload.get("approvalId") or ""
    slot = {"event": threading.Event(), "decision": None, "chatId": chat_id}
    with _APPROVAL_LOCK:
        _APPROVALS[approval_id] = slot
    try:
        if not slot["event"].wait(timeout):
            log("WARN", "approval %s timed out after %ss -> deny" % (approval_id, timeout))
            return "deny"
        decision = slot["decision"]
        return decision if decision in ("allow_once", "allow_always", "deny") else "deny"
    finally:
        with _APPROVAL_LOCK:
            _APPROVALS.pop(approval_id, None)


def _drop_approvals(chat_id: str) -> None:
    """会话结束时清掉它残留的审批槽，避免客户端拿到失效的 approvalId。"""
    with _APPROVAL_LOCK:
        for key in [k for k, v in _APPROVALS.items() if v.get("chatId") == chat_id]:
            _APPROVALS.pop(key, None)


# ---------------------------------------------------------------------------
# Request dispatch
# ---------------------------------------------------------------------------


def _make_error_response(req_id, code: str, message: str) -> dict:
    return {"type": "res", "id": req_id, "ok": False, "error": {"code": code, "message": message}}


def _make_ok_response(req_id, result: dict) -> dict:
    return {"type": "res", "id": req_id, "ok": True, "result": result}


def _check_protocol_version(params: dict | None) -> None:
    """R3: enforce protocol version match, no silent downgrade."""
    pv = (params or {}).get("protocolVersion")
    if pv is None:
        raise FrameError("BAD_REQUEST", "params.protocolVersion is required")
    if pv != SIDECAR_PROTOCOL_VERSION:
        raise FrameError(
            "VERSION_MISMATCH",
            f"protocolVersion={pv} != sidecar={SIDECAR_PROTOCOL_VERSION}",
        )


def _handle_models_rpc(req_id, method: str, params: dict) -> dict:
    """Dispatch models.* RPCs (contract: docs/model-config-spec.md).

    Keys never appear in responses, logs, or config.json: upsert writes the
    provided apiKey straight into the env file + os.environ, and every list
    result exposes only the boolean `hasKey`.
    """
    try:
        if method == "models.list":
            data = modelconfig.load_models(root=modelconfig._models_root())
            items = []
            for it in data["items"]:
                out = {k: v for k, v in it.items() if k not in ("hasKey", "apiKey")}
                out["hasKey"] = bool(it.get("hasKey"))
                # 深度能力字段必须跟着走：界面靠 softThinkingDepth 决定给硬档位还是软引导，
                # 漏了它界面就永远当厂商有硬参数（MiniMax 那种其实没有）。
                items.append(modelconfig.decorate_thinking(out))
            return _make_ok_response(req_id, {
                "defaultId": data.get("defaultId"),
                "items": items,
                "activeId": data.get("activeId"),
            })
        if method == "models.presets":
            return _make_ok_response(req_id, modelconfig.list_presets())
        if method == "models.fetch":
            if not isinstance(params, dict):
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params must be an object")
            base_url = str(params.get("baseUrl") or "").strip()
            if not base_url:
                # 没填就按对接方式取模板里的地址（用户根本不该手填这个）
                preset = next((x for x in modelconfig.PRESETS
                               if x.get("key") == params.get("provider")), None)
                base_url = str((preset or {}).get("baseUrl") or "").strip()
            # key 优先级：本次随请求带来的 > 环境变量（已在 .env 里的）
            api_key = str(params.get("apiKey") or "").strip()
            if not api_key:
                env_name = str(params.get("apiKeyEnv") or "").strip()
                api_key = modelconfig.read_env_var(env_name)
            try:
                ids, meta = modelconfig.fetch_remote_models_detailed(base_url, api_key)
            except modelconfig.ModelFetchError as e:
                return _make_error_response(req_id, e.code, e.message)
            return _make_ok_response(req_id, {
                "models": ids, "count": len(ids), "baseUrl": base_url,
                # 每个模型的真实窗口（厂商同一次响应里给的）。查不到的模型这里就没有，
                # 前端据此自动带出「上下文窗口」，拉不到就让用户按阶梯挑。
                "meta": meta,
            })
        if method == "models.upsert":
            if not isinstance(params, dict):
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params must be an object")
            result = modelconfig.upsert_model(params, root=modelconfig._models_root())
            return _make_ok_response(req_id, result)
        if method == "models.remove":
            mid = params.get("id")
            if not isinstance(mid, str) or not mid:
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params.id is required")
            return _make_ok_response(req_id, modelconfig.remove_model(mid, root=modelconfig._models_root()))
        if method == "models.setDefault":
            mid = params.get("id")
            if not isinstance(mid, str) or not mid:
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params.id is required")
            return _make_ok_response(
                req_id, modelconfig.set_default_model(mid, root=modelconfig._models_root()))
    except modelconfig.ModelConfigError as e:
        return _make_error_response(req_id, e.code, e.message)
    except Exception as e:  # noqa: BLE001 - surfaced to the client
        log("ERROR", "models rpc failed: %r" % (e,))
        return _make_error_response(req_id, "INTERNAL", str(e))
    return _make_error_response(req_id, "BAD_METHOD", f"unknown method: {method}")


def _handle_usage_rpc(req_id, method: str, params: dict) -> dict:
    """Usage aggregation RPC.

    底部信息栏和「用量统计」页都走这一个方法：同一份 usage_log 聚合，两处数字
    不可能对不上。钱按预置模型目录的官价现算（套餐制不给单价，异币种不相加）。
    """
    store = _session_store()
    if store is None:
        return _make_error_response(req_id, "STORE_UNAVAILABLE",
                                    "sessions.db is not available")
    sid = params.get("sessionId")
    sid = sid if isinstance(sid, str) and sid.strip() else None
    days = params.get("days")
    days = int(days) if isinstance(days, (int, float)) and days > 0 else 7
    try:
        data = store.usage_summary(days=days, session_id=sid)
    except Exception as e:  # noqa: BLE001 - surfaced to the client
        log("ERROR", "usage rpc failed: %r" % (e,))
        return _make_error_response(req_id, "INTERNAL", str(e))
    return _make_ok_response(req_id, data)


def _handle_sessions_rpc(req_id, method: str, params: dict) -> dict:
    """Dispatch sessions.* / messages.search RPCs.

    Persistence must never break the chat, and equally must never lie about
    success: a store that cannot even open maps to STORE_UNAVAILABLE, and
    per-call errors surface as INTERNAL instead of pretending nothing happened.
    """
    store = _session_store()
    if store is None:
        return _make_error_response(req_id, "STORE_UNAVAILABLE",
                                    "sessions.db is not available")
    try:
        if method == "sessions.list":
            # Soft-deleted rows stay recoverable, so the UI can ask for them.
            items = store.list_sessions(include_deleted=bool(params.get("includeDeleted")))
            return _make_ok_response(req_id, {
                "sessions": [{
                    "id": it["id"],
                    "title": it["title"],
                    "updatedAt": it["updated_at"],
                    "createdAt": it["created_at"],
                    "model": it["model"],
                    "status": it["status"],
                    "messageCount": it["message_count"],
                    "totalInputTokens": it["total_input_tokens"],
                    "totalOutputTokens": it["total_output_tokens"],
                } for it in items],
            })
        if method == "sessions.create":
            title = params.get("title")
            title = title if isinstance(title, str) and title.strip() else "New session"
            # The UI owns its session ids (it keys in-memory message maps by
            # them), so a caller-supplied sessionId is honoured as-is; only
            # when it is absent does the store mint one.
            wanted = params.get("sessionId")
            wanted = wanted if isinstance(wanted, str) and wanted.strip() else None
            sid = store.create_session(
                model=str(params.get("model") or "default"),
                base_url=params.get("baseUrl") if isinstance(params.get("baseUrl"), str) else None,
                title=title, session_id=wanted)
            return _make_ok_response(req_id, {"id": sid, "title": title})
        if method == "sessions.rename":
            sid = params.get("sessionId") or params.get("id")
            title = params.get("title")
            if not isinstance(sid, str) or not sid:
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params.id is required")
            if not isinstance(title, str) or not title.strip():
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params.title must be a non-empty string")
            try:
                store.rename_session(sid, title)
            except LookupError:
                return _make_error_response(req_id, "SESSION_NOT_FOUND",
                                            f"no such session: {sid}")
            return _make_ok_response(req_id, {"id": sid, "title": title})
        if method == "sessions.delete":
            sid = params.get("sessionId") or params.get("id")
            if not isinstance(sid, str) or not sid:
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params.id is required")
            try:
                store.delete_session(sid)
            except LookupError:
                return _make_error_response(req_id, "SESSION_NOT_FOUND",
                                            f"no such session: {sid}")
            return _make_ok_response(req_id, {"removed": sid})
        if method == "sessions.messages":
            sid = params.get("sessionId") or params.get("id")
            if not isinstance(sid, str) or not sid:
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params.sessionId is required")
            include_inactive = bool(params.get("includeInactive", False))
            row = store.get_session(sid)
            if row is None:
                return _make_error_response(req_id, "SESSION_NOT_FOUND",
                                            f"no such session: {sid}")
            msgs = store.get_messages(sid, include_inactive=include_inactive)
            # 2026-09-25: 工具调用行一起回传 —— 跨会话历史里要还原工具卡
            # （状态/结果/耗时在 tool_calls 表里，之前只有写没有读）。
            calls = store.get_tool_calls(sid)
            return _make_ok_response(req_id, {
                "sessionId": sid,
                "active": row.get("status") != "deleted",
                "includeInactive": include_inactive,
                "messages": [_message_wire(m) for m in msgs],
                "toolCalls": calls,
            })
        if method == "messages.search":
            query = params.get("query")
            if not isinstance(query, str) or not query.strip():
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params.query is required")
            sid = params.get("sessionId")
            if sid is not None and (not isinstance(sid, str) or not sid):
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params.sessionId must be a non-empty string")
            hits = store.search_messages(query, session_id=sid)
            return _make_ok_response(req_id, {
                "sessionId": sid,
                "query": query,
                "hits": [{
                    "sessionId": h["sessionId"],
                    "ordinal": h["ordinal"],
                    "role": h["role"],
                    "snippet": h["snippet"],
                } for h in hits],
            })
    except Exception as e:  # noqa: BLE001 - surfaced to the client
        log("ERROR", "sessions rpc failed: %r" % (e,))
        return _make_error_response(req_id, "INTERNAL", str(e))
    return _make_error_response(req_id, "BAD_METHOD", f"unknown method: {method}")


def _message_wire(m: dict) -> dict:
    """Message row -> protocol message shape (same field names as chat).

    toolCalls stays the parsed list when the row has one, so a client replay
    can rebuild the exact assistant/tool turn the model produced.
    """
    out = {
        "id": m["id"],
        "role": m["role"],
        "content": m["content"],
        "createdAt": m["createdAt"],
        "ordinal": m["ordinal"],
    }
    # 2026-09-25: 思考内容也要出线。之前存在库里却不回传，跨会话历史/刷新之后
    # 思考就没了（用户看到的「思考过程」只活在流式那一瞬间）。
    if m.get("reasoning"):
        out["reasoning"] = m["reasoning"]
    # 2026-09-27: 产出文件出线，刷新/重载后附件卡片仍在
    if m.get("artifacts"):
        out["artifacts"] = m["artifacts"]
    if m.get("toolCallId"):
        out["toolCallId"] = m["toolCallId"]
    if m.get("toolCallsJson") is not None:
        out["toolCallsJson"] = m["toolCallsJson"]
        if m.get("toolCalls") is not None:
            out["toolCalls"] = m["toolCalls"]
    return out


def _resolve_model_override(params: dict) -> tuple[str | None, str | None]:
    """Resolve chat params.modelId to (error_code, message); both None = ok.

    Model resolution itself happens in the worker thread via
    modelconfig.resolve_model; this only pre-checks existence so a bad
    modelId fails fast without spawning a thread.
    """
    model_id = params.get("modelId")
    if model_id is None:
        return None, None
    if not isinstance(model_id, str) or not model_id:
        return "MODEL_INVALID", "params.modelId must be a non-empty string"
    if modelconfig.find_item(model_id, root=_models_rpc_root()) is None:
        return "MODEL_NOT_FOUND", "no such model id: %s" % model_id
    return None, None


def _models_rpc_root() -> str:
    """Root for models.* config reads/writes (overridable for tests)."""
    return modelconfig._models_root()


#: 工具开关（capabilities.toolsDisabled）的配置根。测试用
#: DESK_AGENT_CONFIG_ROOT 指向临时目录做隔离，绝不碰真实 config.json。
_TOOLS_CONFIG_ROOT_OVERRIDE: str | None = os.environ.get("DESK_AGENT_CONFIG_ROOT") or None


def _tools_config_root() -> str:
    return _TOOLS_CONFIG_ROOT_OVERRIDE or appconfig._ROOT


def _load_tools_disabled() -> None:
    """启动时把 config.json 的 capabilities.toolsDisabled 注入 tools。

    读一次：键不存在 / 配置坏掉 → 什么都注不进（= 全部启用）。注册表里
    已不存在的名字（老版本配置遗留）在 tools.set_disabled 的校验前先丢
    掉 —— 陈年配置不许弄坏新版本。文件缺失时 appconfig.load 抛
    AppConfigError，同样当空表。
    """
    raw: object = []
    try:
        raw = appconfig.capability("toolsDisabled", [], root=_tools_config_root())
    except appconfig.AppConfigError:
        raw = []
    if not isinstance(raw, list):
        return
    known = {t.name for t in tools.list_tools()}
    names = [str(n).strip() for n in raw
             if isinstance(n, str) and n.strip() and n.strip() in known]
    try:
        tools.set_disabled(names)
    except tools.ToolToggleError as exc:        # 防御：过滤后不该发生
        log("warn", "startup toolsDisabled ignored: %s" % exc)


def _persist_vision(payload: dict) -> None:
    """把视觉线路（capabilities.vision）原子写回 config.json。

    2026-09-28：设置页选「读图走哪个模型」落这里 —— 会话模型不吃图时，图片先
    交给这个模型读成文字再进上下文。写法照 _persist_tools_disabled：临时文件
    + os.replace，其它键一律保留。
    """
    root = _tools_config_root()
    try:
        raw = appconfig.load(root=root)
    except appconfig.AppConfigError:
        raw = {}
    caps = raw.get("capabilities")
    if not isinstance(caps, dict):
        caps = {}
        raw["capabilities"] = caps
    vision = caps.get("vision")
    if not isinstance(vision, dict):
        vision = {}
    vision.update({k: v for k, v in payload.items() if v})
    caps["vision"] = vision
    path = os.path.join(root, "config.json")
    text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _vision_payload() -> dict:
    """当前视觉线路配置（读 config.json 的 capabilities.vision）。"""
    cfg = appconfig.capability("vision", {}) or {}
    return dict(cfg) if isinstance(cfg, dict) else {}


def _handle_vision_rpc(req_id: str, method: str, params: dict) -> dict:
    """vision.get / vision.set —— 设置页那条「读图走哪个模型」。

    set 只收 modelId，provider/baseUrl/apiKeyEnv 从该模型自己的配置里抄过来，
    避免同一个模型在两处各写一份、改一处忘一处。声明不吃图的模型直接拒。
    """
    if method == "vision.get":
        return _make_ok_response(req_id, {"vision": _vision_payload()})
    model_id = params.get("modelId")
    if not isinstance(model_id, str) or not model_id.strip():
        return _make_error_response(req_id, "BAD_REQUEST",
                                    "params.modelId must be a non-empty string")
    item = modelconfig.find_item(model_id, root=_models_rpc_root())
    if not isinstance(item, dict):
        return _make_error_response(req_id, "MODEL_NOT_FOUND",
                                    "no such model id: %s" % model_id)
    model_name = str(item.get("model") or "")
    if not appconfig.model_declares_image(model_name):
        return _make_error_response(
            req_id, "MODEL_NO_IMAGE_INPUT",
            "模型 %s 未声明支持图片输入（capabilities.models.<模型>.inputModalities "
            "不含 image），不能作为视觉模型" % model_name)
    keep = {k: item.get(k) for k in ("provider", "model", "baseUrl", "apiKeyEnv")
            if item.get(k)}
    try:
        _persist_vision(keep)
    except OSError as exc:
        return _make_error_response(
            req_id, "IO_ERROR", "cannot persist vision to config.json: %s" % exc)
    return _make_ok_response(req_id, {"vision": _vision_payload()})


def _persist_tools_disabled(names: list[str]) -> None:
    """把禁用表原子写回 config.json（临时文件 + os.replace，其它键保留）。

    config.json 不存在时以 config.example.json（appconfig.load 的回落）为
    起点抄一份再写 config.json —— 模板永远只读。
    """
    root = _tools_config_root()
    try:
        raw = appconfig.load(root=root)
    except appconfig.AppConfigError:
        raw = {}
    caps = raw.get("capabilities")
    if not isinstance(caps, dict):
        caps = {}
        raw["capabilities"] = caps
    caps["toolsDisabled"] = list(names)
    path = os.path.join(root, "config.json")
    text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _tools_list_payload() -> dict:
    """tools.list / tools.setEnabled 共用的返回体（契约键名保持不变）。

    每个条目在原 schema 上加 "enabled"：禁用的工具不进模型的 tools 数组
    （schemas() 跳过），但必须原样出现在这份列表里，界面才有东西可开关。
    """
    disabled_names = tools.disabled()
    entries = []
    levels = {}
    for tool in tools.list_tools():
        entries.append({
            "type": "function",
            # 界面用的扁平字段（2026-09-26）：模型要的是嵌套 schema，界面只是想
            # 列个名字/描述，别再让界面自己去扒 function.name。
            "name": tool.name,
            "description": tool.description,
            "enabled": tool.name not in disabled_names,
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        })
        levels[tool.name] = ""
    return {
        "tools": entries,
        "levels": levels,
        "canAlwaysAllow": {"hardline": False, "dangerous": False, "": False},
    }


def _skills_list_payload() -> dict:
    """skills.list 的返回体：已装技能 + 被扫描拒绝的失败项。

    builtin 的判定和 skills_registry 的写口径一致（source_dir 落在只读根
    集合里）：同一个函数算出来的值，读写两侧永远不吵架。
    """
    skills = []
    try:
        builtin_roots = [os.path.realpath(r)
                         for r in skills_registry._builtin_roots()]

        def _is_builtin(source_dir: str) -> bool:
            real = os.path.realpath(source_dir)
            return any(real == root or real.startswith(root + os.sep)
                       for root in builtin_roots)

        for skill in skills_registry.list_skills():
            skills.append({
                "name": skill.name,
                "description": skill.description,
                "whenToUse": skill.when_to_use,
                "version": skill.version,
                "author": skill.author,
                "sourceDir": skill.source_dir,
                "builtin": _is_builtin(skill.source_dir),
            })
        skipped = [[path, reason] for path, reason in skills_registry.skipped()]
    except Exception as exc:                      # noqa: BLE001 - 扫描不许炸 RPC
        log("warn", "skills.list failed: %s" % exc)
        skills = []
        skipped = []
    return {"skills": skills, "skipped": skipped}


def handle_request(req: dict) -> dict:
    """Dispatch a single request frame; return the response frame."""
    req_id = req.get("id")
    if req_id is None:
        # Shouldn't happen if framing is right, but be strict.
        return _make_error_response("noid", "BAD_REQUEST", "missing id")
    method = req.get("method")
    params = req.get("params") or {}
    if not isinstance(method, str):
        return _make_error_response(req_id, "BAD_REQUEST", "method must be string")

    try:
        _check_protocol_version(params)
    except FrameError as e:
        return _make_error_response(req_id, e.code, e.message)

    if method == "ping":
        return _make_ok_response(req_id, {"pong": True, "ts": int(time.time())})
    if method == "info":
        return _make_ok_response(
            req_id,
            {
                "backendVersion": SIDECAR_BACKEND_VERSION,
                "protocolVersion": SIDECAR_PROTOCOL_VERSION,
                "capabilities": list(SIDECAR_CAPABILITIES),
                "pid": os.getpid(),
            },
        )
    if method == "run.pending":
        # 这个会话有没有「可续」的运行（界面据此显示「继续」按钮）。
        session_id = params.get("sessionId")
        store = _run_store()
        run = None
        if store is not None and session_id:
            try:
                run = store.pending_for_session(str(session_id))
            except Exception as exc:                      # noqa: BLE001
                log("warn", "run.pending failed: %s" % exc)
        return _make_ok_response(req_id, {"run": run})

    if method == "run.dismiss":
        # 用户点了「忽略」：这条断点不再提示（任务本身不动）。
        run_id = str(params.get("runId") or "")
        store = _run_store()
        updated = None
        if store is not None and run_id:
            try:
                updated = store.finish(run_id, status=_run_status_consts().STATUS_DONE,
                                       reason="用户忽略")
            except Exception as exc:                      # noqa: BLE001
                log("warn", "run.dismiss failed: %s" % exc)
        return _make_ok_response(req_id, {"runId": run_id, "run": updated})

    if method == "run.active":
        # 正在跑的运行（关程序时要提示「有任务在跑」）。
        store = _run_store()
        runs = []
        if store is not None:
            try:
                runs = [r for r in store.list_all()
                        if r.get("status") == _run_status_consts().STATUS_RUNNING]
            except Exception as exc:                      # noqa: BLE001
                log("warn", "run.active failed: %s" % exc)
        return _make_ok_response(req_id, {"runs": runs})

    if method == "run.interrupt":
        # 用户主动中断（例如关程序前选「中断并保存断点」）。
        run_id = str(params.get("runId") or "")
        store = _run_store()
        updated = None
        if store is not None and run_id:
            try:
                updated = store.interrupt(run_id, reason=str(params.get("reason") or "用户中断"))
            except Exception as exc:                      # noqa: BLE001
                log("warn", "run.interrupt failed: %s" % exc)
        return _make_ok_response(req_id, {"runId": run_id, "run": updated})

    if method == "chat":
        messages = params.get("messages")
        if not isinstance(messages, list) or not messages:
            return _make_error_response(req_id, "BAD_REQUEST",
                                        "params.messages must be a non-empty list")
        # Session-level modelId override: fail fast on unknown ids so the
        # client gets a synchronous error instead of a chat.error stream.
        model_err, model_msg = _resolve_model_override(params)
        if model_err:
            return _make_error_response(req_id, model_err, model_msg)
        # 图片在起工作线程之前先处理掉：处理不了就当场报错，别开线程再失败
        images = params.get("images") or []
        if images:
            if not isinstance(images, list):
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params.images must be a list")
            try:
                _chat_model = ""
                _mid = params.get("modelId")
                if isinstance(_mid, str) and _mid:
                    try:
                        _info = modelconfig.resolve_model(
                            _mid, root=modelconfig._models_root())
                        _chat_model = str(
                            getattr(_info, "model", "")
                            or (dict(_info).get("model")
                                if isinstance(_info, dict) else "") or "")
                    except Exception:
                        _chat_model = ""
                messages = _attach_images([dict(m) for m in messages], images,
                                          chat_model=_chat_model)
            except attachments.AttachmentError as e:
                return _make_error_response(req_id, e.code, e.message)
            except Exception as e:
                return _make_error_response(req_id, "IMAGE_ERROR", str(e))
        params = dict(params)
        params["messages"] = messages
        with _CHAT_LOCK:
            _CHATS[req_id] = {"cancel": False}
        t = threading.Thread(target=_run_chat, args=(req_id, params),
                             name="chat-%s" % req_id, daemon=True)
        t.start()
        # Ack immediately; the stream arrives as chat.* events afterwards.
        return _make_ok_response(req_id, {"accepted": True,
                                          "useTools": bool(params.get("useTools", False))})

    if method == "approval.respond":
        approval_id = params.get("approvalId")
        decision = params.get("decision")
        if not isinstance(approval_id, str) or not approval_id:
            return _make_error_response(req_id, "BAD_REQUEST",
                                        "params.approvalId is required")
        if decision not in ("allow_once", "allow_always", "deny"):
            return _make_error_response(
                req_id, "BAD_REQUEST",
                "params.decision must be one of allow_once | allow_always | deny")
        with _APPROVAL_LOCK:
            slot = _APPROVALS.get(approval_id)
            if slot is None:
                return _make_ok_response(req_id, {"accepted": False,
                                                  "reason": "no such pending approval"})
            slot["decision"] = decision
            slot["event"].set()
        return _make_ok_response(req_id, {"accepted": True,
                                          "approvalId": approval_id,
                                          "decision": decision})

    if method in ("tools.list", "tools.setEnabled"):
        # 键名沿用旧契约（levels / canAlwaysAllow），值换新口径（2026-09-25）：
        # level -> tier（hardline / dangerous / ""，工具粒度不再有基准级别，
        # 全部默认放行），canAlwaysAllow 一律 false（没有「总是允许」）。
        # tools.setEnabled 在返回前先改配置（先原子落盘，再把新表注入
        # tools.set_disabled；非法名字报 UNKNOWN_TOOL），然后走同一段拼装
        # 逻辑返回全量列表 —— 一个开关点下去，界面拿到的就是最新状态，
        # 不用再补一次 tools.list。
        if method == "tools.setEnabled":
            name = params.get("name")
            enabled = params.get("enabled")
            if not isinstance(name, str) or not name.strip():
                return _make_error_response(req_id, "BAD_REQUEST",
                                            "params.name is required")
            if not isinstance(enabled, bool):
                return _make_error_response(
                    req_id, "BAD_REQUEST",
                    "params.enabled is required and must be a boolean")
            name = name.strip()
            current = tools.disabled()
            if enabled:
                current.discard(name)
            else:
                current.add(name)
            try:
                # 先校验 + 落盘，成功后才注入内存 —— 磁盘和内存永不同时
                # 各改一半。set_disabled 自带注册表校验（未知名字抛
                # ToolToggleError），所以这里先 dry-run 校验再落盘。
                known = {t.name for t in tools.list_tools()}
                if name not in known:
                    return _make_error_response(
                        req_id, "UNKNOWN_TOOL", "unknown tool: %s" % name)
                _persist_tools_disabled(sorted(current))
                tools.set_disabled(sorted(current))
            except tools.ToolToggleError as exc:
                return _make_error_response(req_id, exc.code, exc.message)
            except OSError as exc:
                return _make_error_response(
                    req_id, "IO_ERROR",
                    "cannot persist toolsDisabled to config.json: %s" % exc)
        return _make_ok_response(req_id, _tools_list_payload())

    if method == "skills.list":
        return _make_ok_response(req_id, _skills_list_payload())

    if method in ("models.list", "models.presets", "models.upsert",
                  "models.remove", "models.setDefault", "models.fetch"):
        return _handle_models_rpc(req_id, method, params)

    if method in ("vision.get", "vision.set"):
        return _handle_vision_rpc(req_id, method, params)

    if method in ("sessions.list", "sessions.create", "sessions.rename",
                  "sessions.delete", "sessions.messages", "messages.search"):
        return _handle_sessions_rpc(req_id, method, params)

    if method == "usage.summary":
        return _handle_usage_rpc(req_id, method, params)

    if method == "chat.cancel":
        target = params.get("id")
        if not isinstance(target, str) or not target:
            return _make_error_response(req_id, "BAD_REQUEST",
                                        "params.id (the chat request id) is required")
        with _CHAT_LOCK:
            entry = _CHATS.get(target)
            if entry is None:
                return _make_ok_response(req_id, {"cancelled": False,
                                                  "reason": "no such in-flight chat"})
            entry["cancel"] = True
        return _make_ok_response(req_id, {"cancelled": True})

    return _make_error_response(req_id, "BAD_METHOD", f"unknown method: {method}")


def emit_hello_event() -> dict:
    """The unsolicited hello event sent right after startup."""
    return {
        "type": "evt",
        "event": "hello",
        "data": {
            "protocolVersion": SIDECAR_PROTOCOL_VERSION,
            "backendVersion": SIDECAR_BACKEND_VERSION,
            "pid": os.getpid(),
            "capabilities": list(SIDECAR_CAPABILITIES),
        },
    }


def main() -> int:
    log("INFO", f"sidecar starting (pid={os.getpid()}, protocolVersion={SIDECAR_PROTOCOL_VERSION})")
    # key 只从环境变量 / .env 读：先把 .env 灌进进程环境，界面上的
    # 「已配置 / 还没配」才和实际能不能调通一致。只报键数，不回显值。
    loaded = modelconfig.load_env_file()
    if loaded:
        log("INFO", f"env: loaded {loaded} key(s) from .env")

    # 启动时把「还写着 running」的运行判为中断：单实例应用，启动这一刻不可能
    # 还有别的东西在跑这一轮。这样断电/被杀/关程序之后的遗留任务都能被续上。
    try:
        store = _run_store()
        touched = store.sweep_on_start() if store is not None else []
        if touched:
            log("INFO", "run checkpoint: %d 个未完成的任务已标为中断，可在会话里点「继续」"
                % len(touched))
    except Exception as e:  # pragma: no cover - 断点库不可用不影响启动
        log("WARN", "run checkpoint sweep failed: %s" % (e,))

    # 文档提取 + 扫描件 OCR 兜底装配（进程内一次）。失败只降级（read_file
    # 返回教学式提示），绝不挡启动。
    try:
        _wire_ocr_fallback()
        log("INFO", "ocr fallback wired: %s" % _ocr_status())
    except Exception as e:  # pragma: no cover - 配置读不出来
        log("WARN", "ocr fallback wiring failed: %s" % (e,))

    # 工具开关（capabilities.toolsDisabled）：启动时读一次 config.json 注入
    # tools 的内存禁用表。配置坏掉 / 键缺失 = 全部启用，绝不挡启动。
    try:
        _load_tools_disabled()
        disabled_count = len(tools.disabled())
        if disabled_count:
            log("INFO", "tools: %d tool(s) disabled via config.json" % disabled_count)
    except Exception as e:  # pragma: no cover - 配置读不出来
        log("WARN", "tools disabled loading failed: %s" % (e,))

    # Hello is an *unsolicited* event and must go out before we read anything:
    # the client gates every request behind it (45s budget, "sidecar not ready:
    # starting" until it lands). Blocking on the client's first frame here
    # deadlocks both sides -- that is why the desktop UI could never connect.
    try:
        write_frame(emit_hello_event())
    except Exception as e:  # pragma: no cover - stdout broken
        log("ERROR", f"failed to write hello: {e}")
        return 4

    # Main loop.
    raw_stdin = sys.stdin.buffer
    while True:
        try:
            req = read_frame(raw_stdin)
        except ProtocolError as e:
            log("INFO", f"stdin closed or protocol error: {e}; exiting")
            return 0
        if req.get("type") != "req":
            log("WARN", f"ignoring non-request frame type={req.get('type')}")
            continue
        try:
            write_frame(handle_request(req))
        except Exception as e:
            log("ERROR", f"handler error: {e}")
            continue


def _system_prompt_for(workspace_root: str, memory: str | None = None,
                       cfg=None) -> str:
    """Orchestration: base role + identity + skills + tools + project rules.

    (design doc 281-285). The renderer's old two-line prompt is deliberately
    NOT used any more: prompt assembly lives here so the front end and the back
    end cannot drift apart. Everything below degrades to "that layer absent"
    rather than a crash: a missing skills registry, no identity file, or a
    workspace without AGENTS.md all just render fewer sections.
    """
    skills = []
    try:
        import skills_registry
        skills = skills_registry.list_skills()
    except Exception as exc:                    # missing module or unreadable dir
        log("warn", "skills scan unavailable: %s" % exc)
    try:
        tool_list = tools.list_tools()
    except Exception as exc:                    # pragma: no cover - defensive
        log("warn", "tool list unavailable: %s" % exc)
        tool_list = []
    # User-authored layers (agent_files): identity lives in the agent's own
    # data dir, project rules live in the workspace itself. Read per turn, so
    # editing either file applies to the very next message.
    identity = instructions = None
    try:
        import agent_files
        identity = agent_files.load_identity()
        instructions = agent_files.load_instructions(workspace_root)
    except Exception as exc:                    # pragma: no cover - defensive
        log("warn", "agent files unavailable: %s" % exc)
    # Split the memory snapshot in two: canonical identity cards belong in the
    # identity section (deterministic presence every turn), everything else
    # stays in the volatile memory block. memory_block() already excludes the
    # identity-category slots, so nothing is rendered twice.
    memory_block = memory
    try:
        hooks = _memory_hooks()
        if hooks is not None:
            top = hooks.identity_text()
            memory_block = hooks.memory_block()
            if top:
                identity = (identity + "\n" + top) if identity else top
    except Exception:                           # pragma: no cover - defensive
        memory_block = memory
    # Thinking depth: vendors with a real depth parameter get it in the request
    # body (llm.THINKING_DEPTH_MAP); vendors without one (MiniMax) only get the
    # prompt-level hint, and it is labelled as soft in the UI.
    # 历史会话不进常驻上下文（2026-09-25 用户口径）：常驻只放
    # 精炼记忆（identity / Mnemosyne 召回），历史内容一律按需用
    # search_conversations 工具查 —— 问一句天气不该付整份历史的 token。
    depth = getattr(cfg, "thinking_depth", None) if cfg is not None else None
    depth_is_soft = bool(depth) \
        and getattr(cfg, "thinking", None) is not False \
        and not llm.is_thinking_depth_adapted(getattr(cfg, "provider", None))
    prompt = prompt_build.build(workspace=workspace_root, skills=skills, tools=tool_list,
                                identity=identity, instructions=instructions,
                                memory=memory_block,
                                thinking_depth=depth,
                                thinking_depth_is_soft=depth_is_soft,
                                lessons=_lessons_block())
    if os.environ.get("DESK_AGENT_LOG_PROMPT"):
        log("info", "system prompt (%d chars, %d skills, %d tools):\n%s"
            % (len(prompt), len(skills), len(tool_list), prompt))
    # 产出文件会自动作为附件出现在对话里（2026-09-27 用户要求）。写进提示词，
    # 模型才知道两件事：不用把整份文件贴进正文；要交付已有文件该怎么标记。
    prompt += ("\n\n# 文件交付\n"
               "你写入或修改的文件会自动作为附件出现在对话里（带文件名、大小，"
               "可点击打开）。所以不要把整份文件内容再贴一遍，写完说清楚"
               "\"已生成 xxx，见附件\" 即可。如果用户要的是已经存在的文件，"
               "在回复里单独写一行 [[file: 绝对路径]]，它就会作为附件发出去。\n")
    return prompt


# ── 子代理（2026-09-26）───────────────────────────────────────────────────────
# 每轮对话在自己的线程里装配一个委托器（引擎 = python/subagent.py），句柄交给
# tools.set_delegator（线程局部，见 tools.py）。子代理节点跑一轮自己的 agent loop：
# 自己的 messages、自己的运行记录、schema 里没有 delegate（深度 1）、审批一律拒、
# 不问用户。批次跑完由 watcher 把合并结果写回会话 + 通知界面，用户不用点也不用等。
_SUBAGENT_NODE_MAX_LOOP = 12
_SUBAGENT_LIVE_MAX = 8
_SUBAGENT_LIVE: list = []          # 最近的委托器（供后面的界面卡片查询）


def _subagent_config() -> dict:
    cfg = appconfig.capability("delegation", {}) or {}
    return cfg if isinstance(cfg, dict) else {}


def _emit_subagent(event: str, payload: dict) -> None:
    name = str(event)
    if not name.startswith("subagent."):
        name = "subagent.%s" % name          # 引擎已经自带前缀，别加两遍
    try:
        _emit(name, payload or {})
    except Exception:                                   # noqa: BLE001
        pass                                            # 界面事件不能弄死节点


def _subagent_schemas() -> list:
    """子代理看到的工具：和主循环同一份，但去掉 delegate（深度 1 围栏）。"""
    return [s for s in tools.schemas()
            if (s.get("function") or {}).get("name") != "delegate"]


def _make_subagent_runner(*, cfg, cancel, session_id, window):
    """造一个 run_node：引擎调它跑一个节点的完整循环。"""
    ws = _base_dir(None)
    prompt = _system_prompt_for(ws, memory=_memory_text(), cfg=cfg)
    model = model_name_for_chat(cfg)
    node_max_loop = int(_subagent_config().get("nodeMaxLoop") or _SUBAGENT_NODE_MAX_LOOP)
    schemas = _subagent_schemas()

    def run_node(task: dict, node_ctx: dict) -> "subagent.NodeResult":
        goal = str(node_ctx.get("goal") or task.get("goal") or "")
        extra = str(task.get("context") or "").strip()
        body = goal if not extra else "%s\n\n%s" % (goal, extra)
        node_id = str(node_ctx.get("node_id") or "")
        run_rec = None
        if _run_store() is not None:
            try:
                run_rec = _run_store().start(kind="subagent", session_id=session_id,
                                             model=model, max_loop=node_max_loop)
            except Exception as exc:                    # noqa: BLE001
                log("warn", "subagent run checkpoint start failed: %s" % exc)
        ctx = agent_loop.LoopContext(
            messages=[{"role": "user", "content": body}],
            session_id=session_id,
            workspace_root=ws,
            system_prompt=prompt,
            model=model,
            max_tokens=None,
            use_tools=True,
            max_loop=node_max_loop,
            context_window=int(window),
        )
        deps = agent_loop.default_deps(
            request_approval=lambda payload: "deny",
            is_cancelled=cancel,
            audit=None,
            lessons=None,
            run_id=(run_rec or {}).get("run_id", ""),
            checkpoint=_checkpoint_handler(run_rec["run_id"]) if run_rec else None,
            tool_schemas=lambda: schemas,
            # 2026-09-26 真机 bug（子代理状态条抓出来的）：agent_loop 用
            # deps.load_model_config() 取「本轮生效的模型配置」，不覆盖就退回
            # llm.load_config() 读默认那段。于是节点出现「模型名取自本轮 cfg
            # （如 OpenRouter 的 stealth/space-bunny-alpha）、端点却走默认线路
            # （MiniMax/百炼）」的错配 → HTTP 400 invalid params, unknown model。
            # 节点必须与发起它的这一轮对话共用同一份 resolved cfg。
            load_model_config=lambda: cfg,
        )
        text: list = []
        usage = None
        err = ""
        try:
            for ev in agent_loop.run(ctx, deps=deps):
                etype = ev.get("type")
                if etype == agent_loop.EVENT_DELTA:
                    chunk = ev.get("text") or ""
                    text.append(chunk)
                    _emit_subagent("delta", {"nodeId": node_id, "text": chunk})
                elif ev.get("usage"):
                    usage = ev.get("usage")
                elif etype == agent_loop.EVENT_TOOL_RESULT:
                    _emit_subagent("tool", {"nodeId": node_id, "name": ev.get("name"),
                                            "ok": bool(ev.get("ok"))})
                elif etype == agent_loop.EVENT_ERROR:
                    err = "%s: %s" % (ev.get("code"), ev.get("message"))
        except Exception as exc:                        # noqa: BLE001
            err = "%s: %s" % (type(exc).__name__, exc)
        summary = "".join(text).strip()
        if err:
            status = "failed"
        elif cancel():
            status = "interrupted"
        else:
            status = "done"
        if run_rec is not None:
            try:
                if status == "done":
                    _run_store().finish(run_rec["run_id"])
                else:
                    _run_store().interrupt(run_rec["run_id"], reason=err or "子代理被中断")
            except Exception as exc:                    # noqa: BLE001
                log("warn", "subagent run checkpoint close failed: %s" % exc)
        return subagent.NodeResult(
            node_id=node_id, goal=goal, status=status, summary=summary,
            error=err, usage=usage,
            partial=summary if status == "interrupted" else "")

    return run_node


def _watch_subagent_batch(delegator, batch, session_id: str) -> None:
    """批次跑完：合并结果写回会话 + 通知界面（不改用户会话的其它内容）。"""
    batch_id = getattr(batch, "batch_id", "")

    def _wait() -> None:
        try:
            delegator.join(batch_id)
            merged = delegator.consolidated_message(batch_id)
        except Exception as exc:                        # noqa: BLE001
            merged = "子代理批次 %s 出问题了：%s" % (batch_id, exc)
        if not merged:
            merged = "子代理批次 %s 结束了，但没有可读的结果。" % batch_id
        if session_id:
            try:
                _session_store().append_messages(
                    session_id, [{"role": "assistant", "content": merged}])
            except Exception as exc:                    # noqa: BLE001
                log("warn", "subagent result not persisted: %s" % exc)
        _emit_subagent("batch_done", {"batchId": batch_id, "sessionId": session_id,
                                      "message": merged})

    threading.Thread(target=_wait, name="subagent-watch", daemon=True).start()


class _WatchedDelegator(subagent.Delegator):
    """引擎 + 「批次完了写回会话」这一层；引擎本身不认识会话库。"""

    def dispatch(self, tasks: list, session_id: str = ""):
        batch = super().dispatch(tasks, session_id=session_id)
        _watch_subagent_batch(self, batch, session_id)
        return batch


def _make_delegator(*, cfg, cancel, session_id, window):
    conf = _subagent_config()
    if not bool(conf.get("enabled", True)):
        return None
    runner = _make_subagent_runner(cfg=cfg, cancel=cancel, session_id=session_id,
                                   window=window)
    delegator = _WatchedDelegator(
        runner,
        limit=conf.get("maxConcurrentChildren") or None,
        emit=_emit_subagent)
    _SUBAGENT_LIVE.append(delegator)
    while len(_SUBAGENT_LIVE) > _SUBAGENT_LIVE_MAX:
        _SUBAGENT_LIVE.pop(0)
    return delegator


def _run_chat(req_id, params) -> None:
    """Worker thread: run the agent loop, emitting chat.* / tool.* / approval.* events.

    走的是一条统一路径：agent_loop 负责「流式对话 → 工具调用 → 裁决 → 审批 →
    执行 → 回灌 → 再对话」，这里只把事件翻成帧。useTools 默认 false，所以老的
    纯聊天调用行为不变；带工具是显式打开。
    """
    messages = params.get("messages")
    if not isinstance(messages, list) or not messages:
        _emit("chat.error", {"id": req_id, "code": "BAD_REQUEST",
                             "message": "params.messages must be a non-empty list"})
        return
    messages, resumed_from = _apply_resume(params, messages)

    # Session-level modelId override (contract section 4): resolve it here so
    # MODEL_NOT_FOUND / MODEL_KEY_MISSING surface as chat.error immediately,
    # before any loop round. Without modelId the legacy config path is kept
    # byte-for-byte identical (load_config + env key requirement).
    model_id = params.get("modelId")
    resolved_cfg = None
    resolved_modalities: set[str] | None = None
    if model_id is not None:
        try:
            info = modelconfig.resolve_model(model_id, root=modelconfig._models_root())
        except modelconfig.ModelConfigError as e:
            _emit("chat.error", {"id": req_id, "code": e.code, "message": e.message})
            return
        # from_resolved carries contextWindow / thinking / temperature / topP
        # so they reach the request body (contract 1.1 + 6).
        resolved_cfg = llm.ModelConfig.from_resolved(info)
        resolved_modalities = {str(m).lower() for m in info["inputModalities"]}
    else:
        try:
            resolved_cfg = _chat_config()   # 配置有问题要当场报，别等循环里再失败
        except Exception as e:
            _emit("chat.error", {"id": req_id, "code": getattr(e, "code", "CONFIG_ERROR"),
                                 "message": str(e)})
            return

    def cancelled() -> bool:
        with _CHAT_LOCK:
            entry = _CHATS.get(req_id)
        return bool(entry and entry.get("cancel"))

    # 输入区控制条（契约 §6.1）：思考开关 / 思考深度 / 上下文长度是会话级覆盖，
    # 只作用于这一轮，不写回 config.json。取值不认识就如实说一句，别静默失效。
    for note in llm.apply_turn_overrides(resolved_cfg, params.get("overrides")):
        _emit("chat.notice", {"id": req_id, "text": note})

    window = getattr(resolved_cfg, "context_window", None) or 128000
    try:
        context_mechanism.validate_window(int(window))
    except Exception as e:
        # Contract 7.1: a window that cannot hold the tail is refused up front,
        # never run in a degraded state.
        _emit("chat.error", {"id": req_id, "code": getattr(e, "code", "MODEL_INVALID"),
                             "message": str(e)})
        return

    ctx = agent_loop.LoopContext(
        messages=list(messages),
        session_id=params.get("sessionId"),
        workspace_root=_base_dir(params.get("workspaceRoot")),
        system_prompt=_system_prompt_for(_base_dir(params.get("workspaceRoot")),
                                         memory=_memory_text(),
                                         cfg=resolved_cfg),
        model=params.get("model"),
        max_tokens=params.get("maxTokens"),
        use_tools=bool(params.get("useTools", False)),
        max_loop=int(params.get("maxLoop") or agent_loop.MAX_LOOP),
        context_window=int(window),
    )
    # Session persistence: resolve/create the session BEFORE the loop runs so
    # the user turn survives a crash mid-loop. chat_persist owns every DB
    # decision; sidecar only hands over the pieces. A failing store degrades
    # to "nothing written", never to a dead chat.
    _persist_warn = lambda level, msg: log(level, "chat persist: %s" % msg)  # noqa: E731
    session_id = chat_persist.open_chat_session(
        _session_store(), session_id=ctx.session_id, messages=messages,
        model=model_name_for_chat(resolved_cfg), base_url=base_url_for_chat(resolved_cfg),
        warn=_persist_warn)
    ctx.session_id = session_id
    chat_persist.persist_new_user_messages(_session_store(), session_id, messages,
                                           warn=_persist_warn)
    # Tool calls observed this chat, keyed by call id; one tool_calls row each.
    tool_capture: dict = {}
    usage_seen = None
    # 运行断点（2026-09-25）：这一轮从开始就在盘上有记录，断电/关程序/断网后能续。
    run_rec = None
    if _run_store() is not None:
        try:
            run_rec = _run_store().start(
                kind="chat", session_id=session_id,
                model=model_name_for_chat(resolved_cfg), max_loop=ctx.max_loop,
                resumed_from=resumed_from)
        except Exception as exc:                          # noqa: BLE001
            log("warn", "run checkpoint start failed: %s" % exc)
    deps = agent_loop.default_deps(
        request_approval=lambda payload: _wait_for_approval(req_id, payload),
        is_cancelled=cancelled,
        audit=_audit_store(),
        lessons=_lessons_store(),
        run_id=(run_rec or {}).get("run_id", ""),
        checkpoint=_checkpoint_handler(run_rec["run_id"]) if run_rec else None,
    )
    # 子代理（2026-09-26）：这一轮对话的委托器挂到本线程上（见 _make_delegator）。
    # 工具在同一个线程里执行，所以 delegate 拿到的就是这一轮自己的引擎。
    tools.set_delegate_session(session_id)
    tools.set_delegator(_make_delegator(cfg=resolved_cfg, cancel=cancelled,
                                        session_id=session_id, window=window)
                        if ctx.use_tools else None)
    # Hand this round's tool schemas to the summarizer (thread-local, see
    # _CHAT_TLS): the L2 cache-sharing request then carries exactly the same
    # tools array the loop does, which several providers include in the
    # cached prefix ahead of the system prompt.
    _CHAT_TLS.tools = deps.tool_schemas() if ctx.use_tools else None
    # Context mechanism v1 (contract 7): one governor per chat call, wired with
    # a real summarizer and a spill directory next to the workspace. The
    # session_prefix hands the main session's system prompt to the governor
    # as a fallback for the L2 cache-sharing request: normally the loop's
    # message view already carries it (agent_loop inserts it), and only when
    # it somehow does not does the governor reuse this injected prefix.
    # Persistence hooks: the governor reports what it did, the store decides
    # where it lands. Audit failures are logged and swallowed -- a bookkeeping
    # problem must never take down the chat it is describing.
    _gov_session_id = ctx.session_id

    def _audit_compaction(data: dict) -> None:
        store = _session_store()
        if store is None or not _gov_session_id:
            return
        try:
            if "L2" in str(data.get("level") or ""):
                # Only summary generations get a record row: L0/L1 reuse the
                # current generation, which is UNIQUE per session.
                store.append_compaction_record(
                    session_id=_gov_session_id,
                    generation=int(data.get("generation") or 0),
                    trigger=str(data.get("trigger") or "pressure"),
                    tokens_before=int(data.get("estimatedTokens") or 0),
                    tokens_after=int(data.get("tokensAfter") or 0),
                    summary_text=str(data.get("summaryText") or ""),
                )
            store.set_context_state(
                _gov_session_id,
                context_length=int(data.get("tokensAfter") or 0),
                overflow_attempts=int(getattr(deps.context_governor, "overflow_attempts", 0) or 0),
            )
        except Exception as exc:
            log("warn", "compaction audit write failed: %s" % exc)

    def _index_spill(path: str, content: str) -> None:
        store = _session_store()
        if store is None or not _gov_session_id:
            return
        try:
            store.append_spilled_output(
                session_id=_gov_session_id, path=path, content=content,
                preview_head=content[:200],
                preview_tail=content[-200:] if len(content) > 200 else "")
        except Exception as exc:
            log("warn", "spill index write failed: %s" % exc)

    deps.context_governor = context_mechanism.ContextGovernor(
        int(window),
        spill_dir=_spill_dir(ctx.workspace_root),
        summarizer=_make_summarizer(resolved_cfg),
        session_prefix=([{"role": "system", "content": ctx.system_prompt}]
                        if ctx.system_prompt else None),
        on_compacted=_audit_compaction,
        on_spilled=_index_spill,
    )

    # modelId override: the agent loop calls load_model_config per round; pin
    # it to the resolved ModelConfig so the whole loop (image gate included)
    # sees the selected model. The legacy `model` param still overrides the
    # model NAME only, on top of whichever config is active.
    # （2026-09-25 起 judge 已由 default_deps 接到 guard.judge，不再读规则表。）

    if resolved_cfg is not None:
        deps.load_model_config = lambda: resolved_cfg
        if resolved_modalities is not None:
            deps.model_declares_image = lambda _m: "image" in resolved_modalities
        # params.model (model-name override) wins for the name only; otherwise
        # the resolved modelId's model name must be used, not the global cfg's.
        ctx.model = params.get("model") or resolved_cfg.model
        if params.get("model"):
            # Tool rounds call the LLM without an explicit model kwarg; they read
            # cfg.model, so the override has to land on the config too.
            resolved_cfg.model = str(params["model"])

    n_reason = 0
    n_text = 0
    reason_parts: list[str] = []
    text_parts: list[str] = []
    try:
        for ev in agent_loop.run(ctx, deps=deps):
            kind = ev.get("type") or "chat.error"
            data = {k: v for k, v in ev.items() if k != "type"}
            data["id"] = req_id
            if kind == "chat.reasoning":
                n_reason += len(data.get("text") or "")
                reason_parts.append(data.get("text") or "")
            elif kind == "chat.delta":
                n_text += len(data.get("text") or "")
                text_parts.append(data.get("text") or "")
            elif kind == "chat.done":
                data.setdefault("reasoningChars", n_reason)
                data.setdefault("textChars", n_text)
                # 这一轮真跑完了，旧断点才算收尾（提前收会把断点烧掉）
                _close_resumed_run(resumed_from)
            # Session persistence capture: fold tool events as they stream by
            # and keep the last usage report. Nothing here writes to disk;
            # persist_chat_result does that once, after the loop.
            if kind in ("tool.call", "tool.result"):
                chat_persist.merge_tool_event(tool_capture, kind, data)
            if data.get("usage") is not None:
                usage_seen = data["usage"]
            _emit(kind, data)
    except Exception as e:
        log("ERROR", "chat worker failed: %r" % (e,))
        _emit("chat.error", {"id": req_id, "code": "INTERNAL", "message": str(e)})
    finally:
        # Persist whatever the loop produced (soft archive: partial rounds are
        # kept too). persist_chat_result swallows its own failures, so this
        # can never turn a chat into a crash.
        # 本轮产出的文件 -> 附件（用户要求：文件要直接落到对话里）
        try:
            import artifacts as artifacts_mod
            _cap_rows = list(tool_capture.values())
            _turn_text = "".join(text_parts)
            turn_artifacts = artifacts_mod.collect(
                _cap_rows, _turn_text, workspace_root=ws)
        except Exception as e:  # noqa: BLE001
            log("WARN", "artifact collect failed: %r" % (e,))
            turn_artifacts = []
        chat_persist.persist_chat_result(
            _session_store(), session_id,
            loop_messages=list(ctx.messages), request_messages=messages,
            system_prompt=ctx.system_prompt, usage=usage_seen,
            tool_events=list(tool_capture.values()),
            artifacts=turn_artifacts,
            reasoning="".join(reason_parts),
            model=model_name_for_chat(resolved_cfg),
            base_url=base_url_for_chat(resolved_cfg), warn=_persist_warn)
        if turn_artifacts:
            # 落库之后单独推一次：done 事件发出时附件还没算出来，
            # 流式那一轮靠这个事件就能立刻看到文件卡（2026-09-27）。
            _emit("chat.artifacts", {"id": req_id, "artifacts": turn_artifacts})
        # Turn-boundary memory upkeep: queue the write, kick off recall for the
        # next turn, and let an idle engine hand its memory back to the OS.
        try:
            hooks = _memory_hooks()
            if hooks is not None:
                question = _last_user_text(messages)
                hooks.note_turn(question, _last_reply_text(ctx.messages))
                hooks.start_prefetch(question)
                hooks.maybe_reap()
        except Exception as exc:                # pragma: no cover - defensive
            log("warn", "memory upkeep skipped: %s" % exc)
        with _CHAT_LOCK:
            _CHATS.pop(req_id, None)
        _drop_approvals(req_id)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        log("INFO", "stdout pipe closed by peer")
        sys.exit(0)
    except KeyboardInterrupt:
        log("INFO", "interrupted")
        sys.exit(0)

