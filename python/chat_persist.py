"""Persist one chat round into the session store.

Kept as a separate module so sidecar.py never grows a block of database code:
sidecar hands over the raw pieces (request messages, the loop's final message
list, captured tool events, usage) and this module decides what to write.

Failure contract: persistence must never break the chat. Every entry point
catches its own exceptions and reports through the injected ``warn`` callback
(sidecar wires it to stderr logging). A broken store degrades to "nothing was
written this round", nothing else.

What lands in the session per chat call:
  * user message(s) new to the session (persisted up front, so a crash mid-loop
    still records the user turn);
  * after the loop: the assistant/tool tail the loop appended (assistant text
    with tool_calls JSON, tool results, final assistant content);
  * one usage_log row from the chat.done usage payload;
  * one tool_calls row per executed tool call (level, decision, duration,
    result or error), linked to the assistant message that carried the call.

Alignment rule: the client re-sends its full in-memory history on every chat,
but that history carries user/assistant text only -- tool rows and tool-call
carriers are written by the backend and never travel back from the renderer.
So alignment is by content (walk both lists, match role+text), never by count:
a positional prefix goes stale the moment one tool round lands, and then the
newest turns stop being written. The system prompt is rebuilt per chat and is
never stored.
"""
from __future__ import annotations

import json

import json


def _default_warn(level: str, msg: str) -> None:
    import sys
    sys.stderr.write("[chat_persist] %s: %s\n" % (level, msg))
    sys.stderr.flush()


def _extract_text(content) -> str:
    """Flatten a protocol content field (string or block list) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "\n".join(parts)
    return ""


def _is_system(msg: dict) -> bool:
    return (msg or {}).get("role") == "system"


def _msg_text(msg) -> str:
    return _extract_text((msg or {}).get("content")).strip()


def _stored_text(row) -> str:
    return _extract_text((row or {}).get("content")).strip()


def _new_tail(stored: list, incoming: list) -> list:
    """Return the incoming messages the session does not hold yet.

    Walks both lists and matches on (role, text). Stored rows the client never
    re-sends (tool results, empty assistant tool-call carriers) are stepped
    over, so the walk stays aligned even when the two sides differ in length.
    The first message that matches nothing is where the new turns start, and
    everything from there on is new.
    """
    j = 0
    for idx, msg in enumerate(incoming):
        role = (msg or {}).get("role")
        text = _msg_text(msg)
        found = -1
        for k in range(j, len(stored)):
            row = stored[k] or {}
            if row.get("role") == role and _stored_text(row) == text:
                found = k
                break
        if found < 0:
            return list(incoming[idx:])
        j = found + 1
    return []


def _tail_after_last_request(loop_messages: list, request_messages: list) -> list:
    """Return the loop messages produced after the client's last turn.

    The loop compacts its own message list, so the request is not a positional
    prefix of it (a summarized middle is shorter than what the client sent).
    The client's last message is the anchor instead: the compaction path always
    keeps the newest turns, so everything after that anchor is this round's
    output. If the anchor cannot be found the old positional rule is used, with
    a warning, rather than writing the whole list.
    """
    loop = list(loop_messages or [])
    request = [m for m in (request_messages or []) if not _is_system(m)]
    if not request:
        return [m for m in loop if not _is_system(m)]
    anchor = request[-1]
    role = anchor.get("role")
    text = _msg_text(anchor)
    for idx in range(len(loop) - 1, -1, -1):
        row = loop[idx] or {}
        if not _is_system(row) and row.get("role") == role and _msg_text(row) == text:
            return [m for m in loop[idx + 1:] if not _is_system(m)]
    skip = len(request)
    if any(_is_system(m) for m in loop):
        skip += 1
    return [m for m in loop[skip:] if not _is_system(m)]


def _usage_numbers(usage) -> tuple[int, int, int]:
    """Pull (input, output, cached) tokens out of a provider usage payload.

    Accepts OpenAI-style (prompt_tokens/completion_tokens) and the
    input_tokens/output_tokens spelling. Missing numbers count as 0.
    """
    if not isinstance(usage, dict):
        return 0, 0, 0
    inp = usage.get("prompt_tokens")
    if inp is None:
        inp = usage.get("input_tokens", 0)
    out = usage.get("completion_tokens")
    if out is None:
        out = usage.get("output_tokens", 0)
    cached = 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens", 0)
    return int(inp or 0), int(out or 0), int(cached)


def open_chat_session(store, *, session_id: str | None, messages: list,
                       model: str, base_url: str | None,
                       warn=_default_warn) -> str | None:
    """Return the session id a chat should write into, creating it if needed.

    Rules: an existing non-deleted session is reused; a missing or deleted
    session id gets a brand-new session titled from the first user message.
    On store failure the requested id (or None) is returned unchanged so the
    chat can proceed -- every write below will simply no-op.
    """
    if store is None:
        return session_id
    try:
        if session_id:
            row = store.get_session(session_id)
            if row is not None and row.get("status") != "deleted":
                return session_id
        title = store.title_from_messages(messages) \
            if hasattr(store, "title_from_messages") \
            else _title_from_messages(messages)
        # 把客户端给的 id 一并带下去：不带的话这里会造一个裸 UUID 的新会话，
        # 而客户端用的是 s-<epoch>，两侧 id 空间不一致 -> 前端按自己的会话 id
        # 汇总用量时永远是 0（2026-09-27 实测：usage_log 里两种格式并存）。
        return store.create_session(model=model, base_url=base_url, title=title,
                                    session_id=session_id)
    except Exception as e:  # noqa: BLE001 - persistence must not break chat
        warn("WARN", "session open failed: %r" % (e,))
        return session_id


def _title_from_messages(messages: list) -> str:
    for msg in messages or []:
        if (msg or {}).get("role") != "user":
            continue
        text = _extract_text(msg.get("content")).strip()
        if text:
            return text[:30]
        break
    return "New session"


def persist_new_user_messages(store, session_id: str | None, messages: list,
                              warn=_default_warn) -> None:
    """Write user messages that are not yet stored in the session.

    Alignment is by content over the full history (include_inactive, so
    soft-archived rows count as present), not by row count: the client drops
    tool rows, which made the old count-based prefix permanently stale after
    the first tool round.
    """
    if store is None or not session_id:
        return
    try:
        stored = store.get_messages(session_id, include_inactive=True)
        incoming = [m for m in (messages or []) if not _is_system(m)]
        for msg in _new_tail(stored, incoming):
            store.append_message(session_id, msg)
    except Exception as e:  # noqa: BLE001
        warn("WARN", "user message persist failed: %r" % (e,))


def persist_chat_result(store, session_id: str | None, *,
                        loop_messages: list, request_messages: list,
                        system_prompt: str | None, usage,
                        tool_events: list, model: str, base_url: str | None,
                        reasoning: str = "", artifacts: list | None = None,
                        warn=_default_warn) -> None:
    """Write everything the loop produced after the request prefix.

    ``loop_messages`` is the LoopContext message list after the loop ran
    (system prompt possibly inserted at index 0, loop output appended at the
    end, middle possibly summarized away). ``request_messages`` is what the
    client sent. The output appended after the client's last turn is persisted;
    system rows are never stored.
    """
    if store is None or not session_id:
        return
    try:
        tail = _tail_after_last_request(loop_messages, request_messages)
        if not tail and usage is None:
            return
        written: list[dict] = []
        for msg in tail:
            written.append(store.append_message(session_id, msg))
        _persist_tool_calls(store, session_id, tail, written, tool_events, warn)
        # 思考正文单独落 reasoning 列，挂在整轮最后一条 assistant 上（2026-09-24）：
        # 正文只存答案，重载后思考仍进折叠块，不回灌进正文。
        if reasoning and reasoning.strip():
            last_assistant = ""
            for msg, row in zip(tail, written):
                if msg.get("role") == "assistant":
                    last_assistant = str(row.get("id") or "")
            if last_assistant and hasattr(store, "set_message_reasoning"):
                store.set_message_reasoning(session_id, last_assistant,
                                            reasoning.strip())
        # 产出文件挂在整轮最后一条 assistant 上（和 reasoning 同一时机）。
        # 事件流里的名字/参数形状未必可靠，所以再拿「本轮已落库的 tool_calls 行」
        # 兜一遍 —— 那是真值（tool_name / arguments_json），2026-09-27 实测：
        # 只信事件流时 artifacts 列一直是 NULL。
        try:
            import artifacts as artifacts_mod
            if not artifacts and hasattr(store, "get_tool_calls") and written:
                turn_msgs = {str(r.get("id") or "") for r in written}
                rows = [c for c in (store.get_tool_calls(session_id) or [])
                        if str(c.get("message_id") or "") in turn_msgs]
                artifacts = artifacts_mod.collect(rows, "", workspace_root=None)
                if artifacts:
                    warn("INFO", "artifacts recovered from tool_calls: %d" % len(artifacts))
        except Exception as e:  # noqa: BLE001
            warn("WARN", "artifact fallback failed: %r" % (e,))
        if artifacts:
            last_assistant_for_files = ""
            for msg, row in zip(tail, written):
                if msg.get("role") == "assistant":
                    last_assistant_for_files = str(row.get("id") or "")
            if last_assistant_for_files and hasattr(store, "set_message_artifacts"):
                store.set_message_artifacts(
                    session_id, last_assistant_for_files,
                    json.dumps(artifacts, ensure_ascii=False))
        if usage is not None:
            inp, out, cached = _usage_numbers(usage)
            store.append_usage(session_id=session_id, model=model,
                               base_url=base_url, input_tokens=inp,
                               output_tokens=out, cached_input_tokens=cached,
                               cost_usd=0.0)
    except Exception as e:  # noqa: BLE001
        warn("WARN", "chat result persist failed: %r" % (e,))


def _persist_tool_calls(store, session_id: str, tail: list, written: list,
                        tool_events: list, warn) -> None:
    """One tool_calls row per captured tool.call/tool.result pair.

    message_id resolution: ``written`` is the list of append_message results
    (each carries the assigned row id) aligned with ``tail`` by position --
    loop messages have no ids before they are stored. An assistant row whose
    tool_calls JSON contains the call id wins; otherwise the tool-role row
    with the matching tool_call_id. The column is NOT NULL, so unresolvable
    calls are skipped with a warning instead of failing the whole persist.
    """
    if not tool_events:
        return
    assistant_calls: dict[str, str] = {}   # tool_call_id -> message row id
    tool_messages: dict[str, str] = {}     # tool_call_id -> message row id
    for msg, row in zip(tail, written):
        row_id = str(row.get("id") or "")
        calls = msg.get("tool_calls")
        if msg.get("role") == "assistant" and isinstance(calls, list):
            for tc in calls:
                cid = (tc or {}).get("id")
                if cid:
                    assistant_calls[str(cid)] = row_id
        elif msg.get("role") == "tool" and msg.get("tool_call_id"):
            tool_messages[str(msg["tool_call_id"])] = row_id

    for event in tool_events:
        cid = str(event.get("id") or "")
        if not cid:
            continue
        message_id = assistant_calls.get(cid) or tool_messages.get(cid)
        if not message_id:
            warn("WARN", "tool call %s: no message to link, skipping row" % cid)
            continue
        ok = bool(event.get("ok"))
        try:
            store.append_tool_call(
                id=cid, session_id=session_id, message_id=message_id,
                tool_name=str(event.get("name") or ""),
                arguments=event.get("arguments") or {},
                permission_level=str(event.get("level") or ""),
                decision=str(event.get("decision") or ""),
                result={"content": event.get("content") or ""} if ok else None,
                error=None if ok else {"code": event.get("errorCode"),
                                       "message": event.get("errorMessage")},
                duration_ms=event.get("durationMs"),
                started_at=event.get("startedAt"),
                finished_at=event.get("finishedAt"))
        except Exception as e:  # noqa: BLE001
            warn("WARN", "tool call persist failed for %s: %r" % (cid, e))


def merge_tool_event(state: dict, event: str, data: dict) -> None:
    """Fold one tool.call / tool.result event into the per-chat capture map.

    ``state`` is a dict the caller keeps for the whole chat: id -> merged
    event. The result side wins on overlapping keys (it carries ok/content/
    duration/decision, the call side carries level/arguments).
    """
    cid = str(data.get("id") or "")
    if not cid:
        return
    row = state.setdefault(cid, {"id": cid})
    if event == "tool.call":
        for key in ("name", "arguments", "level"):
            if data.get(key) is not None:
                row[key] = data[key]
    elif event == "tool.result":
        for key in ("name", "ok", "content", "errorCode", "errorMessage",
                    "durationMs", "decision"):
            if data.get(key) is not None:
                row[key] = data[key]


__all__ = ["open_chat_session", "persist_new_user_messages",
           "persist_chat_result", "merge_tool_event"]
