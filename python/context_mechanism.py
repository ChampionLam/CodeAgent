"""上下文机制 v1（docs/model-config-spec.md §7，方案来自项目内部设计文档，不在本仓）。

核心立场：库（会话存储）是唯一事实源，上下文只是派生视图 —— 本模块
只做**视图投影**，不物理丢弃任何消息：所有变换都返回新列表（messages
里的原 dict 不被就地改写），调用方拿投影后的列表去派发请求，会话里
的原文保持原样。

v1 范围（契约 §8）：
  * 状态机计数/代数内存级，不落 SQLite（004 迁移下一轮）。
  * L1 落盘文件写在 sidecar 数据目录 spill/ 下，索引仅内存。
  * 不引入向量检索/嵌入模型。

估算口径（故意高估，宁早压）：ASCII ≈ 4 字符/token、CJK ≈ 1 字符/token。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------------------
# 常量：契约 §7.1 的硬数字（不得改动）
# ---------------------------------------------------------------------------

TRIGGER_RATIO = 0.80            # 触发阈值 = 窗口 × 80%
RESERVE_CAP_TOKENS = 16384      # 预留上限
RESERVE_RATIO = 0.25            # 预留 = min(16384, 窗口 × 25%)
TAIL_RATIO = 0.16               # 保留尾部 = 窗口 × 16%
SINGLE_TOOL_SHARE = 0.30        # 单条工具结果配额 = 窗口 × 30%
AGGREGATE_TOOL_SHARE = 0.50     # 聚合工具结果配额 = 窗口 × 50%
KEEP_RECENT_TOOL_RESULTS = 5    # 最近 5 条工具结果不做 L0 替换

PRESSURE_RETRY = 1              # 压力压缩重试 1 次
OVERFLOW_RETRY = 2              # 超额恢复重试 2 次
DEBOUNCE_MIN_SAVING = 0.10      # 反抖动：连续两次各省 < 10% → COOLING
EFFECTIVE_RATIO = 0.95          # 生效判据：after < before × 0.95

DEFAULT_WINDOW = 128000         # contextWindow 缺省（契约 §3 默认值）

# L1 头尾预览的字符数（头/尾各留这么多，纯本地展示量，不是 token 配额）
SPILL_PREVIEW_HEAD_CHARS = 800
SPILL_PREVIEW_TAIL_CHARS = 400

# 状态机六态（契约 §7.4）
IDLE = "IDLE"
FREE_SCAN = "FREE_SCAN"
COMPACTING = "COMPACTING"
OVERFLOW_RECOVERING = "OVERFLOW_RECOVERING"
COOLING = "COOLING"
TRIPPED = "TRIPPED"
STATES = (IDLE, FREE_SCAN, COMPACTING, OVERFLOW_RECOVERING, COOLING, TRIPPED)

# 事件名（契约 §7.5：只增，不改其他事件形状）
EVENT_CONTEXT_COMPACTED = "context.compacted"

CONTEXT_INVALID = "MODEL_INVALID"   # 加载期校验失败用的错误码（契约 §7.1）

# 失败分级（契约 §7.4：鉴权/配额/网络/空内容/截断 = 中止类）
ABORT_CLASSES = ("AUTH", "QUOTA", "NETWORK", "EMPTY", "TRUNCATED")

SUMMARY_NOTE = "已压缩早期对话以适配上下文（原文保留在会话库中）"

# L2 summary instruction, appended as the single final user message of the
# summary request. Cache-sharing rule: the request prefix must stay
# byte-identical to the main session, so any per-call or time-like content
# belongs ONLY in this tail instruction, never in the prefix.
SUMMARY_INSTRUCTION = (
    "把上方这段较早的对话压缩成一段客观的结构化摘要，分点保留：用户的"
    "目标、已确认的决定、涉及的文件路径与关键数字、尚未完成的事项。"
    "只陈述出现过的内容，不编造、不补充不在原文里的信息。"
    "不要评论，不要用 Markdown 标题。")


class ContextMechanismError(Exception):
    """机制内部错误（加载期校验、摘要守卫、边界违背等）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# token 估算（ASCII ≈ 4 字符/token，CJK ≈ 1 字符/token，故意高估）
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(
    "[\u2e80-\u2eff\u3040-\u30ff\u3130-\u318f\u31f0-\u31ff\u3400-\u4dbf"
    "\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def estimate_text_tokens(text: Any) -> int:
    """一段文本的 token 估算。CJK 1 字符 = 1 token，其余 4 字符 = 1 token。"""
    if not text:
        return 0
    s = text if isinstance(text, str) else str(text)
    cjk = len(_CJK_RE.findall(s))
    rest = len(s) - cjk
    return cjk + (rest + 3) // 4


def _content_text(msg: dict) -> str:
    """取一条消息的纯文本（content 是 str 或 blocks 数组）。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "image_url":
                    parts.append("[image]")   # data URL 很长但缓存住的是稳定开销
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def estimate_message_tokens(msg: dict) -> int:
    """一条消息的估算：content 文本 + tool_calls 的 JSON + 每条固定开销 4。"""
    total = estimate_text_tokens(_content_text(msg))
    tcs = msg.get("tool_calls")
    if isinstance(tcs, list):
        try:
            total += estimate_text_tokens(json.dumps(tcs, ensure_ascii=False))
        except (TypeError, ValueError):
            total += 64
    return total + 4


def estimate_messages_tokens(messages: Iterable[dict]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


# ---------------------------------------------------------------------------
# 预算计算（加载期可校验）
# ---------------------------------------------------------------------------


def reserve_tokens(window: int) -> int:
    return min(RESERVE_CAP_TOKENS, int(window * RESERVE_RATIO))


def trigger_point(window: int) -> int:
    """实际触发点 = min(窗口 × 80%, 窗口 − 预留)。"""
    return min(int(window * TRIGGER_RATIO), window - reserve_tokens(window))


def tail_tokens(window: int) -> int:
    return int(window * TAIL_RATIO)


def validate_window(window: int) -> int:
    """加载期校验（契约 §7.1）：保留尾部 < 实际触发点，不满足拒绝启用。"""
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        raise ContextMechanismError(
            CONTEXT_INVALID, "contextWindow 必须是正整数，收到 %r" % (window,))
    tail, point = tail_tokens(window), trigger_point(window)
    if not tail < point:
        raise ContextMechanismError(
            CONTEXT_INVALID,
            "上下文机制参数自洽性校验失败：保留尾部(%d) ≥ 触发点(%d)（窗口=%d），"
            "拒绝启用（不带病运行）" % (tail, point, window))
    return window


def window_from_model_entry(entry: Any) -> int:
    """从解出的模型条目读 contextWindow；防御式 .get，缺省 128000。

    另一个并行子代理正在给条目加这个字段；这里绝不 import 它的新函数。
    """
    if isinstance(entry, dict):
        raw = entry.get("contextWindow", DEFAULT_WINDOW)
    else:
        raw = getattr(entry, "contextWindow", DEFAULT_WINDOW)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WINDOW


def system_prompt_bytes(messages: Iterable[dict]) -> bytes:
    """系统提示的 sha256 —— 硬边界 5「字节稳定」的可验证判据。"""
    first = None
    for m in messages:
        if m.get("role") == "system":
            first = m
            break
    if first is None:
        return b""
    return hashlib.sha256(_content_text(first).encode("utf-8")).digest()


# ---------------------------------------------------------------------------
# 消息序列辅助（tool 配对 / 角色交替 —— 硬边界 3/4 的校验与修复）
# ---------------------------------------------------------------------------


def _is_tool_msg(m: dict) -> bool:
    return m.get("role") == "tool"


def _has_tool_calls(m: dict) -> bool:
    return bool(m.get("tool_calls"))


def check_pairs(messages: list[dict]) -> None:
    """硬边界 3：tool 消息必须能配到前面的 assistant.tool_calls，否则抛错。

    供投影后自检（「传输层组装的硬校验」）。
    """
    seen_ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant" and _has_tool_calls(m):
            for tc in m["tool_calls"]:
                cid = tc.get("id") if isinstance(tc, dict) else None
                if cid:
                    seen_ids.add(cid)
        elif _is_tool_msg(m):
            cid = m.get("tool_call_id")
            if cid not in seen_ids:
                raise ContextMechanismError(
                    "CONTEXT_PAIRING_BROKEN",
                    "孤儿 tool 消息（tool_call_id=%r 没有对应的 assistant.tool_calls）"
                    "—— 切断了 tool_call 与结果的配对" % (cid,))


def check_role_alternation(messages: list[dict]) -> None:
    """硬边界 4：user/assistant 不能连排同角色（tool 消息视作跟在 assistant 后的结果）。"""
    prev = None
    for m in messages:
        role = m.get("role")
        if role in ("user", "assistant"):
            if prev == role and role == "user":
                raise ContextMechanismError(
                    "ROLE_ALTERNATION_BROKEN",
                    "两条连续 user 消息（连排同角色），多数 chat API 会拒绝")
            prev = role
        # system / tool 不打断 user/assistant 交替链


def assert_contract_layer_untouched(original: list[dict], projected: list[dict]) -> None:
    """硬边界 1：系统提示（以及会话前缀里的 system 头）字节不变。"""
    if system_prompt_bytes(original) != system_prompt_bytes(projected):
        raise ContextMechanismError(
            "SYSTEM_PROMPT_MUTATED", "压缩改变了系统提示字节（硬边界 1 被破坏）")


def _boundary_validate(original: list[dict], projected: list[dict]) -> None:
    """投影后统一跑三条硬校验（3/4/1）。"""
    check_pairs(projected)
    check_role_alternation(projected)
    assert_contract_layer_untouched(original, projected)


# ---------------------------------------------------------------------------
# L0：旧工具输出换一行结构化摘要 + 相同结果去重（0 次 LLM）
# ---------------------------------------------------------------------------


def _tool_result_meta(msg: dict) -> dict:
    """从 tool 消息/配对的 assistant.tool_calls 里挖 L0 摘要需要的信息。"""
    return {
        "tool_call_id": msg.get("tool_call_id"),
        "content": _content_text(msg),
    }


def _find_call_args(messages: list[dict], tool_call_id: str) -> tuple[str, dict]:
    """在 assistant.tool_calls 里找该 id 的工具名与入参。找不到给空。"""
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            if not isinstance(tc, dict) or tc.get("id") != tool_call_id:
                continue
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = {"_raw": raw_args[:120]}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            return fn.get("name") or "unknown_tool", args
    return "unknown_tool", {}


def _key_args(args: dict) -> str:
    """关键入参：最多挑 2 个键值，拼成一行。"""
    if not isinstance(args, dict) or not args:
        return ""
    items = list(args.items())[:2]
    return ", ".join("%s=%s" % (k, str(v)[:60]) for k, v in items)


def l0_line(tool_name: str, args: dict, exit_code: Any, line_count: int) -> str:
    """L0 的一行结构化摘要（契约 §7.2：工具名 + 关键入参 + 退出码 + 输出行数）。"""
    return "[工具 %s(%s) 退出码=%s 输出%d行 — 旧工具结果已压缩占位]" % (
        tool_name, _key_args(args), exit_code if exit_code is not None else "-",
        max(line_count, 0))


def _exit_code_of(content: str) -> Any:
    """退出码从文本里尽量嗅探（exit code 1/2/127 等常见形态）；嗅不到就是 None。"""
    m = re.search(r"exit(?:ed)?(?:\s+(?:with\s+)?code)?\s*[:=]?\s*(\d+)", content[:400],
                  re.IGNORECASE)
    if m:
        return int(m.group(1))
    if re.match(r"^\s*(Traceback|ERROR|Error)", content):
        return 1
    return None


def l0_trim(messages: list[dict]) -> list[dict]:
    """L0：最近 5 条工具结果之外的旧工具结果换一行结构化摘要 + 相同结果去重。

    返回投影后的新列表（原 messages 不动）。0 次 LLM。
    相同结果去重：对已过保护期的工具结果，内容 sha256 相同的第二条起
    直接换成更短的「重复结果」占位行。
    """
    tool_indices = [i for i, m in enumerate(messages) if _is_tool_msg(m)]
    if not tool_indices:
        return list(messages)
    protected = set(tool_indices[-KEEP_RECENT_TOOL_RESULTS:])
    seen_content: set[str] = set()
    out: list[dict] = []
    for i, m in enumerate(messages):
        if not _is_tool_msg(m) or i in protected:
            out.append(m)
            continue
        content = _content_text(m)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if digest in seen_content:
            out.append({**m, "content": "[旧工具结果：与前序相同，已去重]"})
            continue
        seen_content.add(digest)
        name, args = _find_call_args(messages, m.get("tool_call_id") or "")
        line_count = content.count("\n") + 1 if content else 0
        out.append({**m, "content": l0_line(name, args, _exit_code_of(content),
                                            line_count)})
    return out


# ---------------------------------------------------------------------------
# L1：大输出落盘（0 次 LLM）—— 单条 30% / 聚合 50%
# ---------------------------------------------------------------------------


class SpillStore:
    """L1 落盘：写到 base 目录下 spill/。

    The file is the source of truth; on_spilled (when wired) lets the session
    store keep an index row of what went to disk.
    """

    def __init__(self, base_dir: str | None = None,
                 on_spilled: "Callable[[str, str], None] | None" = None) -> None:
        self.base_dir = base_dir
        self._n = 0
        self._lock = threading.Lock()
        self.on_spilled = on_spilled

    def _spill_dir(self) -> str | None:
        if not self.base_dir:
            return None
        path = os.path.join(self.base_dir, "spill")
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            return None

    def spill(self, content: str) -> tuple[str, str]:
        """原文落盘，返回 (locator 文本, 绝对路径)。落不了盘就返回原文（降级不丢数据）。"""
        d = self._spill_dir()
        if d is None:
            return content, ""
        with self._lock:
            self._n += 1
            name = "spill_%d_%s.txt" % (self._n, hashlib.sha256(
                content.encode("utf-8")).hexdigest()[:12])
        path = os.path.join(d, name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError:
            return content, ""
        if self.on_spilled is not None:
            try:
                self.on_spilled(path, content)
            except Exception as exc:  # bookkeeping must never break the chat
                sys.stderr.write("spill index hook failed: %s\n" % exc)
        return "path=%s bytes=%d" % (path, len(content.encode("utf-8"))), path

    @property
    def count(self) -> int:
        return self._n


def head_tail_preview(content: str, head_chars: int = SPILL_PREVIEW_HEAD_CHARS,
                      tail_chars: int = SPILL_PREVIEW_TAIL_CHARS) -> str:
    """头尾预览：契约说「路径 + 头尾预览」，模型按需读取。"""
    if len(content) <= head_chars + tail_chars + 32:
        return content
    return "%s\n...[落盘省略 %d 字符]...\n%s" % (
        content[:head_chars], len(content) - head_chars - tail_chars,
        content[-tail_chars:])


def l1_spill(messages: list[dict], window: int, store: SpillStore) -> list[dict]:
    """L1：单条超 窗口×30% 落盘；聚合超 窗口×50% 从最老开始落盘直到回到配额内。

    只处理 tool 消息（工具输出是大头）；0 次 LLM。返回投影后的新列表。
    """
    single_quota = int(window * SINGLE_TOOL_SHARE)
    agg_quota = int(window * AGGREGATE_TOOL_SHARE)

    out = list(messages)
    tool_indices = [i for i, m in enumerate(out) if _is_tool_msg(m)]

    # 单条超 30%：直接落
    for i in tool_indices:
        content = _content_text(out[i])
        if estimate_text_tokens(content) > single_quota:
            out[i] = _spill_message(out[i], content, store)

    # 聚合超 50%：从最老开始落盘，直到回到配额内
    while True:
        agg = sum(estimate_text_tokens(_content_text(m)) for m in out if _is_tool_msg(m))
        if agg <= agg_quota:
            break
        candidates = [i for i in tool_indices if not _is_spilled(out[i])]
        if not candidates:
            break
        i = candidates[0]                       # 最老的未落盘工具结果
        content = _content_text(out[i])
        out[i] = _spill_message(out[i], content, store) if content.strip() \
            else out[i]
    return out


_SPILL_MARK = "[落盘]"


def _is_spilled(msg: dict) -> bool:
    return _SPILL_MARK in _content_text(msg)[:64]


def _spill_message(msg: dict, content: str, store: SpillStore) -> dict:
    locator, _path = store.spill(content)
    if not locator.startswith("path="):
        # 落盘失败：降级为头尾预览（绝不丢数据，也绝不硬塞全文）
        return {**msg, "content": _SPILL_MARK + " " + head_tail_preview(content)}
    preview = head_tail_preview(content)
    return {**msg, "content": "%s %s\n%s" % (_SPILL_MARK, locator, preview)}


# ---------------------------------------------------------------------------
# L2: head-protect + tail-protect (16%) + middle summary (1 LLM call)
# ---------------------------------------------------------------------------


def _select_tail_protect(messages: list[dict], window: int) -> int:
    """Tail protection: walk from the end, gather until window x 16% est.

    Returns start index; messages[start:] is the protected tail. A tool msg
    always pulls in the assistant(tool_calls) right before it so pairs stay
    intact (hard boundary 3; orphans also fail check_pairs).
    """
    budget = tail_tokens(window)
    acc = 0
    start = len(messages)
    while start > 0 and acc < budget:
        start -= 1
        m = messages[start]
        acc += estimate_message_tokens(m)
        if _is_tool_msg(m):
            while start > 0 and messages[start - 1].get("role") == "assistant" \
                    and _has_tool_calls(messages[start - 1]):
                start -= 1
                acc += estimate_message_tokens(messages[start])
    return start


def _head_protect_end(messages: list[dict]) -> int:
    """Head protection: system block (contract layer) + nothing else forced.

    The head is every leading system message plus the session-prefix messages
    up to (not including) the first user turn.
    """
    idx = 0
    n = len(messages)
    while idx < n and messages[idx].get("role") == "system":
        idx += 1
    return idx


def session_prefix_for(messages: list[dict]) -> "list[dict] | None":
    """Main-session cache prefix: the head-protected originals, verbatim.

    This is exactly the leading system block the main session sends, copied
    byte-for-byte, so a summary request that starts with it shares the
    provider-side automatic prefix cache with the main session instead of
    re-prefilling everything from scratch on a cold prompt. Returns None
    when the session carries no leading system message: the caller then
    keeps the legacy cold summary request.
    """
    head_end = _head_protect_end(messages)
    if head_end <= 0:
        return None
    return [dict(m) for m in messages[:head_end]]


def _summarizer_accepts_messages(summarizer: Any) -> bool:
    """Whether the summarizer opted into the cache-sharing request shape.

    Legacy summarizers keep the Callable[[str], str] contract (flattened
    middle text). A summarizer that can take a full message list sets
    ``accepts_messages = True`` on itself; only then is the cache-sharing
    request assembled, so old callers see byte-identical behavior.
    """
    try:
        return bool(getattr(summarizer, "accepts_messages", False))
    except Exception:
        return False


def compact_l2(messages: list[dict], window: int,
               summarizer: "Callable[..., str]",
               session_prefix: "list[dict] | None" = None
               ) -> tuple[list[dict], dict]:
    """L2 middle summary. Returns (projected list, info dict).

    Hard boundary 2: if the summary is not smaller than the shadowed middle,
    raise ContextMechanismError and do not apply anything.

    Cache-sharing shape: when session_prefix is available (the main session's
    head-protected originals, e.g. from session_prefix_for) AND the
    summarizer opted in via ``accepts_messages = True``, the summarizer
    receives a ready-made message list:

        [session prefix ...] + [middle messages verbatim] + [instruction]

    so the request prefix is byte-identical to the main session's and only
    the tail differs; provider prefix caches hit instead of missing on a
    cold single-prompt request. Otherwise — no usable prefix (None / empty /
    not a list) or a legacy string-only summarizer — the flattened-text
    payload is kept exactly as before, never an error. Either way the
    summarizer is called exactly once, and boundary 2 still compares the
    summary against the shadowed middle only, so failure, retry and guard
    semantics are unchanged.
    """
    head_end = _head_protect_end(messages)
    tail_start = _select_tail_protect(messages, window)
    if tail_start <= head_end:
        raise ContextMechanismError(
            "NOTHING_TO_COMPACT",
            "head/tail protection overlap, no compressible middle")
    middle = messages[head_end:tail_start]
    middle_tokens = sum(estimate_message_tokens(m) for m in middle)
    if middle_tokens <= 0:
        raise ContextMechanismError("NOTHING_TO_COMPACT", "middle is empty")
    if (isinstance(session_prefix, list) and session_prefix
            and _summarizer_accepts_messages(summarizer)):
        # dict() copies: the request carries the session's bytes without
        # ever handing the summarizer a reference it could mutate into the
        # session store (the store stays the single source of truth).
        request_messages = ([dict(m) for m in session_prefix]
                            + [dict(m) for m in middle]
                            + [{"role": "user",
                                "content": SUMMARY_INSTRUCTION}])
        summary = summarizer(request_messages)
    else:
        middle_text = "\n".join("[%s] %s" % (m.get("role"), _content_text(m))
                                for m in middle)
        summary = summarizer(middle_text)
    summary_tokens = estimate_message_tokens({"role": "user", "content": summary})
    if summary_tokens >= middle_tokens:
        raise ContextMechanismError(
            "SUMMARY_NOT_SMALLER",
            "summary est %d >= shadowed %d - refuses to run (boundary 2)"
            % (summary_tokens, middle_tokens))
    summary_msg = {"role": "user",
                   "content": "[early-conversation summary (%d msgs -> %d est "
                              "tokens; original kept in the session store)]\n%s"
                              % (len(middle), summary_tokens, summary)}
    projected = list(messages[:head_end]) + [summary_msg] + list(messages[tail_start:])
    _boundary_validate(messages, projected)
    return projected, {
        "level": "L2",
        "headEnd": head_end,
        "tailStart": tail_start,
        "middleCount": len(middle),
        "tokensBefore": estimate_messages_tokens(messages),
        "tokensAfter": estimate_messages_tokens(projected),
        "llmCalls": 1,
        "middleTokens": middle_tokens,
        "summaryTokens": summary_tokens,
    }


# ---------------------------------------------------------------------------
# State machine (six states) + anti-debounce + in-memory counters
# ---------------------------------------------------------------------------


class ContextGovernor:
    """One per session (in-memory only in v1, never persisted).

    Mount point 1 (pre-step) is driven by ensure_before_dispatch.
    Mount point 2 (request-error) is driven by on_request_error.
    """

    def __init__(self, window: int, *, spill_dir: str | None = None,
                 summarizer: "Callable[..., str] | None" = None,
                 clock: "Callable[[], float] | None" = None,
                 session_prefix: "list[dict] | None" = None,
                 on_compacted: "Callable[[dict], None] | None" = None,
                 on_spilled: "Callable[[str, str], None] | None" = None) -> None:
        validate_window(window)
        self.window = window
        self.state = IDLE
        # Optional persistence hooks (wired by the sidecar). The governor stays
        # storage-agnostic: it reports what happened, the store decides where it
        # lands. A failing hook is reported and swallowed, never raised.
        self.on_compacted = on_compacted
        self.spill_store = SpillStore(spill_dir, on_spilled=on_spilled)
        self.summarizer = summarizer
        self.clock = clock or (lambda: 0.0)     # injectable fake clock
        # Optional main-session prefix handed in by the wiring side (sidecar)
        # for the L2 cache-sharing request. Only used when the session view
        # itself carries no leading system message; what the dispatch layer
        # actually sends is always derived first (it is the authoritative
        # prefix), so an injected prefix can never diverge from the wire.
        self.session_prefix = session_prefix
        self.generation = 0                      # verifiable-state-change judge
        self.pressure_retries = 0                # pressure compaction retries used
        self.overflow_attempts = 0               # overflow recovery retries used
        self.last_savings: list[float] = []      # last two compaction savings
        self.last_result: dict | None = None
        self.events: list[dict] = []             # pending context.compacted events
        self.llm_calls = 0                       # total LLM calls spent (meter)

    # -- events --------------------------------------------------------------

    def _push_event(self, level: str, before: int, after: int, llm_calls: int,
                    trigger: str, *, summary: str = "") -> None:
        data = {
            "level": level,
            "state": self.state,
            "trigger": trigger,
            "estimatedTokens": before,
            "tokensAfter": after,
            "llmCalls": llm_calls,
            "keptTailTokens": tail_tokens(self.window),
            "summaryNote": SUMMARY_NOTE,
            "generation": self.generation,
            "summaryText": summary,
        }
        self.events.append({"event": EVENT_CONTEXT_COMPACTED, "data": data})
        if self.on_compacted is not None:
            try:
                self.on_compacted(data)
            except Exception as exc:  # audit must never break the chat
                sys.stderr.write("compaction audit hook failed: %s\n" % exc)

    def drain_events(self) -> list[dict]:
        evts, self.events = self.events, []
        return evts

    # -- anti-debounce / effectiveness ----------------------------------------

    def _record_saving(self, before: int, after: int) -> float:
        saving = (before - after) / before if before > 0 else 0.0
        self.last_savings.append(saving)
        self.last_savings = self.last_savings[-2:]
        return saving

    def _debounce_tripped(self) -> bool:
        if len(self.last_savings) >= 2 and all(
                s < DEBOUNCE_MIN_SAVING for s in self.last_savings[-2:]):
            self.state = COOLING
            return True
        return False

    def _effective(self, before: int, after: int) -> bool:
        return after < before * EFFECTIVE_RATIO

    # -- mount point 1: pre-step ----------------------------------------------

    def ensure_before_dispatch(self, messages: list[dict]) -> tuple[list[dict], bool]:
        """Called before every round dispatch. Returns (messages_to_send, changed).

        Order fixed by contract 7.4: measure -> over the line? -> L0/L1 free
        -> re-measure -> enough stops at 0 LLM calls -> not enough upgrades
        to L2 -> re-measure. COOLING/TRIPPED sessions send verbatim.
        """
        if self.state in (COOLING, TRIPPED):
            return list(messages), False
        before = estimate_messages_tokens(messages)
        point = trigger_point(self.window)
        if before < point:
            self.state = IDLE
            return list(messages), False

        self.state = FREE_SCAN
        free = l1_spill(l0_trim(messages), self.window, self.spill_store)
        after_free = estimate_messages_tokens(free)
        if after_free < before:
            self.generation += 1
        if after_free < point:
            if after_free < before:
                saving = self._record_saving(before, after_free)
                self._push_event("L0|L1", before, after_free, 0, "pressure")
                self.last_result = {"level": "L0|L1", "before": before,
                                    "after": after_free, "saving": saving}
            self.state = IDLE
            return free, after_free < before

        # L2: exactly 1 LLM call, only when free means are not enough
        if self.summarizer is None:
            self.state = IDLE
            return free, after_free < before
        try:
            self.state = COMPACTING
            self.llm_calls += 1
            projected, info = compact_l2(
                free, self.window, self.summarizer,
                self._session_prefix_for(free))
        except ContextMechanismError:
            # summary-not-smaller or nothing-to-compact: keep the free
            # projection, go back to IDLE, do not loop on it
            self.state = IDLE
            return free, after_free < before
        after = info["tokensAfter"]
        self.generation += 1
        self._record_saving(info["tokensBefore"], after)
        self._push_event("L2", info["tokensBefore"], after, 1, "pressure",
                         summary=str(info.get("summary") or ""))
        self.last_result = {"level": "L2", "before": info["tokensBefore"],
                            "after": after}
        if after < point:
            self.state = IDLE
            return projected, True
        # still over the line after L2: anti-debounce first, then the single
        # pressure retry (only while a compaction is still effective)
        if self._debounce_tripped():
            return projected, True
        if self._effective(info["tokensBefore"], after) \
                and self.pressure_retries < PRESSURE_RETRY:
            self.pressure_retries += 1
            self.state = IDLE      # next pre-step pass retries once more
            return projected, True
        self.state = COOLING
        return projected, True

    # -- mount point 2: request-error -----------------------------------------

    def on_request_error(self, messages: list[dict], error: dict) -> dict:
        """Mount point 2. error is {"code": str, "message": str, "httpStatus": int?}.

        Returns a decision dict; see class docstring of the module tests for
        the exact action set.
        """
        if not is_overflow_error(error, messages, self.window):
            return {"action": "propagate"}

        self.state = OVERFLOW_RECOVERING

        if is_output_limit_error(error):
            return {"action": "lower_max_tokens",
                    "maxTokens": _lowered_max_tokens(error, self.window)}

        failure_class = classify_failure(error)
        if failure_class in ABORT_CLASSES:
            self.state = COOLING
            return {"action": "abort", "reason": failure_class}

        for _ in range(OVERFLOW_RETRY):
            before_gen = self.generation
            before_tokens = estimate_messages_tokens(messages)
            projected = self._compact_for_overflow(messages)
            after_tokens = estimate_messages_tokens(projected)
            if self.generation > before_gen or after_tokens < before_tokens:
                self.overflow_attempts += 1
                level = (self.last_result or {}).get("level", "L2")
                self._push_event(
                    level, before_tokens, after_tokens,
                    1 if level == "L2" else 0, "context-overflow")
                return {"action": "compact_retry", "messages": projected,
                        "shrinkWindow": None,
                        "overflowAttempts": self.overflow_attempts}
            # no verifiable state change: this attempt does not count as
            # progress; loop continues up to OVERFLOW_RETRY
        self.state = TRIPPED
        return {"action": "tripped",
                "warning": "compaction cannot bring the session back inside "
                           "the window (%d overflow-recovery attempts made no "
                           "verifiable state change)" % OVERFLOW_RETRY,
                "exits": ["start_new_session", "manual_retry_force_compact"]}

    def _compact_for_overflow(self, messages: list[dict]) -> list[dict]:
        """Mount-point-2 compaction: free first (0 LLM), L2 only if needed."""
        free = l1_spill(l0_trim(messages), self.window, self.spill_store)
        point = trigger_point(self.window)
        before_free = estimate_messages_tokens(messages)
        if estimate_messages_tokens(free) < point or self.summarizer is None:
            if estimate_messages_tokens(free) < before_free:
                self.generation += 1
                self.last_result = {"level": "L0|L1", "before": before_free,
                                    "after": estimate_messages_tokens(free)}
            return free
        try:
            self.llm_calls += 1
            projected, info = compact_l2(
                free, self.window, self.summarizer,
                self._session_prefix_for(free))
        except ContextMechanismError:
            return free
        self.generation += 1
        self.last_result = {"level": "L2", "before": info["tokensBefore"],
                            "after": info["tokensAfter"]}
        self._record_saving(info["tokensBefore"], info["tokensAfter"])
        return projected

    def _session_prefix_for(self, messages: list[dict]) -> "list[dict] | None":
        """Derive the summary-request prefix for this L2 pass.

        Priority: the leading system block of the view being compacted (that
        is what the dispatch layer actually sends, byte-identical) and only
        then an injected prefix. Returns None when neither exists, which
        keeps the legacy flattened-text summary request (never an error).
        """
        derived = session_prefix_for(messages)
        if derived is not None:
            return derived
        injected = self.session_prefix
        if isinstance(injected, list) and injected:
            return [dict(m) for m in injected]
        return None


# ---------------------------------------------------------------------------
# Mount point 2 helpers: overflow detection (three sources, wide) & triage
# ---------------------------------------------------------------------------

# source 1: explicit provider-confirmed codes / texts
_OVERFLOW_CODE_PATTERNS = (
    "context_window_exceeded", "context_length_exceeded", "context_too_long",
    "prompt_too_long", "maximum_context_length", "max_context_length",
    "input_too_long", "conversation_too_long",
)
_OVERFLOW_TEXT_PATTERNS = (
    "context window", "context length", "maximum context", "prompt too long",
    "too many tokens", "token limit", "input too large",
    "exceeds the context", "longer than the model context",
    "\u4e0a\u4e0b\u6587\u957f\u5ea6", "\u4e0a\u4e0b\u6587\u7a97\u53e3",
    "\u8d85\u8fc7\u4e86?\u6a21\u578b\u7684?\u4e0a\u4e0b\u6587",
)
# output-limit overflow: lower THIS request's max_tokens only, never the window
_OUTPUT_LIMIT_PATTERNS = (
    "max_tokens too large", "max_tokens", "max output tokens",
    "completion too large", "requested tokens exceed",
)
# abort-class failures (contract 7.4)
_ABORT_PATTERNS = {
    "AUTH": ("unauthorized", "401", "invalid api key", "authentication",
             "forbidden", "403", "\u9274\u6743", "api key"),
    "QUOTA": ("rate limit", "429", "quota", "insufficient_quota", "billing",
              "\u914d\u989d", "\u4f59\u989d"),
    "NETWORK": ("timeout", "timed out", "connection reset", "econnreset",
                "broken pipe", "connection refused", "reset by peer",
                "network", "\u65ad\u8fde"),
    "EMPTY": ("empty response", "empty message", "no content",
              "\u7a7a\u5185\u5bb9"),
    "TRUNCATED": ("truncated", "\u622a\u65ad"),
}


def _norm_error(error: Any) -> tuple[str, str, int | None]:
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        status = error.get("httpStatus") or error.get("status")
    else:
        code = str(getattr(error, "code", "") or "").strip()
        message = str(getattr(error, "message", "") or "").strip() \
            if getattr(error, "message", None) else str(error)
        status = getattr(error, "httpStatus", None) or getattr(error, "status", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    return code, message, status


def _matches(text: str, patterns: "Iterable[str]") -> bool:
    low = text.lower()
    for p in patterns:
        if p in low:
            return True
    return False


def is_overflow_error(error: Any, messages: list[dict] | None = None,
                      window: int = DEFAULT_WINDOW) -> bool:
    """Overflow detection, three sources, deliberately wide:

    1. explicit code/text;
    2. generic 400 + big session;
    3. disconnect + big session.
    "big session" = estimated tokens already at/over the trigger point.
    """
    code, message, status = _norm_error(error)
    blob = (code + " " + message).lower()
    if _matches(blob, _OVERFLOW_CODE_PATTERNS) or _matches(blob, _OVERFLOW_TEXT_PATTERNS):
        return True
    if messages is not None:
        big = estimate_messages_tokens(messages) >= trigger_point(window)
        if big and status == 400:
            return True                       # source 2
        if big and (status is None or status >= 500) and _matches(
                blob, _ABORT_PATTERNS["NETWORK"]):
            return True                       # source 3
    return False


def is_output_limit_error(error: Any) -> bool:
    """Output-cap overflow (max_tokens class)."""
    code, message, _ = _norm_error(error)
    blob = (code + " " + message).lower()
    if not _matches(blob, _OUTPUT_LIMIT_PATTERNS):
        return False
    return not _matches(blob, ("context window", "context length",
                               "prompt too long", "input too long",
                               "input too large", "\u4e0a\u4e0b\u6587"))


def classify_failure(error: Any) -> str:
    code, message, _ = _norm_error(error)
    blob = (code + " " + message).lower()
    for cls, patterns in _ABORT_PATTERNS.items():
        if _matches(blob, patterns):
            return cls
    return "OTHER"


def _lowered_max_tokens(error: Any, window: int) -> int:
    """Parse the cap from the error; fall back to half the non-reserved window."""
    code, message, _ = _norm_error(error)
    blob = code + " " + message
    m = re.search(r"(\d{3,})", blob)
    if m:
        val = int(m.group(1))
        if 256 <= val <= window:
            return max(64, val - 64)
    return max(64, (window - reserve_tokens(window)) // 2)


def reset(governor: ContextGovernor) -> None:
    """TRIPPED -> IDLE on manual retry (force); clears abort-class cooling too."""
    governor.state = IDLE
    governor.overflow_attempts = 0
    governor.pressure_retries = 0
    governor.last_savings = []
