"""agent loop：把「流式对话 → 工具调用 → 权限裁决 → 审批 → 执行 → 回灌」串成循环。

设计要点：
  * 所有外部依赖走 LoopDeps 注入 —— 单测能零网络、零 shell、零数据库地跑整个循环。
  * 本模块不 import sidecar、不 print、不自己碰 stdin/stdout；它只 yield 事件，
    由上层（sidecar）决定怎么发给界面。
  * 权限裁决不在这里实现（调 guard.judge），工具执行也不在这里实现（调 tools.execute）。

事件形状与错误码在契约 v3 里定死，见模块末尾的 EVENT_* / 错误码常量。
"""
from __future__ import annotations

import json
import re
import time

import tool_lessons
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import context_mechanism

MAX_LOOP = 20
DUPLICATE_TOOL_WINDOW = 5
#: 同一调用被拒几次才真正终止本轮。真机教训（2026-09-25）：模型读 34 页扫描件
#: 时想「继续读剩下的页」原样重发 read_file，旧逻辑当场 return 掐死整轮，用户
#: 看到的是一条错误。改成先拒这一次 + 给可操作提示（改 pages 参数），连犯才终止。
MAX_DUPLICATE_REFUSALS = 2
# Consecutive failures of the SAME tool before we stop retrying it. Models
# rarely fix a failing call by re-sending it with slightly different arguments,
# but every re-send used to cost the user a fresh approval prompt (2026-09-24:
# 8 run_shell prompts in 72s). Past the budget we refuse pre-approval.
TOOL_FAILURE_BUDGET = 3                 # 保留：老口径（工具连续失败）已被下面两档取代
#: 同一个调用（工具 + 同参数）失败几次 → 不再重跑这个调用。
#: 真机教训（2026-09-25）：原来是「按工具名」数连续失败，模型拿 3 个**不同的**
#: 来源都抓失败（17173 分页、风云、百科）就被当成「在重复试同一件事」掐死整轮。
#: 换来源不是重复，改成按调用签名计数。
TOOL_FAIL_STREAK_BUDGET = 2
#: 一轮内同一工具总共失败几次 → 冷却该工具（换参数也没救，多半是工具/网络本身）。
TOOL_FAIL_TOTAL_BUDGET = 6
#: 冷却 + 同参拒绝累计几次 → 终止本轮（避免白烧循环）。
MAX_TOOL_REFUSALS = 3
DEFAULT_APPROVAL_TIMEOUT = 300
MAX_TOOL_OUTPUT_CHARS = 20000

# --- 「说了要做却没做」兜底 -------------------------------------------------
# Real transcript (2026-09-24): user said 「你自己去联网查一下」, the model answered
# 「好，我去查。让我跑几个搜索试试。」 — and emitted zero tool calls, so the round
# ended and the user saw nothing happen. The prompt now forbids this; the regex
# below is the mechanical backstop so the rule survives a model that ignores it.
MAX_ACTION_NUDGES = 1              # whole-run budget: correct at most once

# Matches a first-person promise to act, in the tail of the message, and does
# NOT match the completed forms (我查了 / 我看过) — those are reports, not
# promises, and nudging after them would be noise.
_ACTION_PROMISE_RE = re.compile(
    r"(?:我(?:先|现在|马上|立刻|这就)?(?:去|来|要)?(?:查一下|查|搜索|搜一下|搜|试一下|试|跑一下|跑|看一下|看看|找一下|找|读一下|联网)"
    r"(?!了|过|完|到)"
    r"|让(?:我|我们先|我先)(?:查|试|搜|跑|看|找)"
    r"|稍等[，,]?\s*我)"
)
ACTION_PROMISE_NUDGE = (
    "停一下：你刚才在回复里说了要去做（查/搜/试），但这一轮没有发出任何工具调用，所以什么都不会发生。"
    "现在二选一：要么立刻发出对应的工具调用去真的做；要么把话说清楚——本机没有这个能力、或者你做不到，"
    "就直说做不到并说明原因。不要只写一句「我去查」就结束。"
)


def promises_action(text: str) -> bool:
    """Whether a finished assistant message promised work it never performed.

    Only the tail is inspected: a promise buried mid-message is usually just
    narration ("我先看了 A，然后我去查了 B"), while the sentence right before
    the end is what the user is left waiting on.
    """
    if not text:
        return False
    tail = text[-160:]
    return bool(_ACTION_PROMISE_RE.search(tail))

EVENT_REASONING = "chat.reasoning"
EVENT_DELTA = "chat.delta"
EVENT_TOOL_CALL = "tool.call"
EVENT_TOOL_RESULT = "tool.result"
EVENT_APPROVAL = "approval.request"
EVENT_DONE = "chat.done"
EVENT_ERROR = "chat.error"
EVENT_CONTEXT_COMPACTED = "context.compacted"   # 契约 §7.5：只增不改
EVENT_NOTICE = "chat.notice"                       # 轻提示（如「网络中断，2 秒后重试」）

# 断网/瞬时故障退避重试（2026-09-25 用户口径）：短断自动重试 2-3 次，
# 超过总窗口就不要再自己试了 —— 报中断、落断点，等用户点「继续」。
NET_RETRY_MAX = 3
NET_RETRY_BASE = 2.0            # 秒：2 → 4 → 8
NET_RETRY_WINDOW = 120.0        # 秒
NET_RETRY_CODES = ("NETWORK", "TIMEOUT")
NET_RETRY_HTTP = (408, 429, 500, 502, 503, 504)

LOOP_LIMIT = "LOOP_LIMIT"
DUPLICATE_TOOL = "DUPLICATE_TOOL"
TOOL_FAILURE_LIMIT = "TOOL_FAILURE_LIMIT"
MODEL_NO_IMAGE_INPUT = "MODEL_NO_IMAGE_INPUT"
TOOL_ARGUMENTS_INVALID = "TOOL_ARGUMENTS_INVALID"
CONFIG_ERROR = "CONFIG_ERROR"
CANCELLED = "CANCELLED"
INTERNAL = "INTERNAL"
CONTEXT_TRIPPED = "CONTEXT_TRIPPED"

_VALID_DECISIONS = ("allow_once", "allow_always", "deny")


@dataclass
class LoopContext:
    messages: list[dict] = field(default_factory=list)
    session_id: str | None = None
    workspace_root: str = ""
    system_prompt: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    use_tools: bool = True
    max_loop: int = MAX_LOOP
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT
    context_window: int | None = None       # 上下文机制 v1 的窗口（None=禁用）


@dataclass
class LoopDeps:
    """全部可注入。真实接线在 sidecar 里，测试里全换假的。"""

    llm_stream: Callable[..., Iterator[dict]]
    request_approval: Callable[[dict], str]
    load_model_config: Callable[[], Any]
    exec_tool: Callable[[str, dict, str], Any]
    judge: Callable[..., Any]
    audit: Any | None = None
    is_cancelled: Callable[[], bool] = lambda: False
    tool_schemas: Callable[[], list] = lambda: []
    model_declares_image: Callable[[str], bool] = lambda _m: True
    splitter_factory: Callable[[], Any] | None = None
    # 上下文机制 v1（契约 §7）：governor 由 sidecar 装配（真实摘要器 + spill 目录），
    # 测试里注入确定性假 governor 或 None（禁用）。事件由 run() 以 context.compacted
    # 事件形式向上冒（sidecar 只负责转帧，不重复发）。
    context_governor: Any | None = None
    # 工具失败经验库（tool_lessons.LessonStore 或测试假件）；None = 不记经验。
    lessons: Any | None = None
    #: 这一轮的运行号（上层建的，断点文件里用它做主键）；空 = 不记断点。
    run_id: str = ""
    #: 断点回调：callable(payload) -> None。payload 见 _checkpoint()。None = 不记断点。
    checkpoint: Any | None = None


def default_deps(*, llm_stream=None, request_approval=None, **overrides) -> LoopDeps:
    """用真实模块装配一份 deps（sidecar 用）。测试请显式构造假实现。"""
    import appconfig
    import guard as guard_mod
    import llm
    import tools
    from reasoning_split import ReasoningSplitter

    kwargs: dict[str, Any] = {
        "llm_stream": llm_stream or llm.stream_chat,
        "request_approval": request_approval or (lambda _payload: "deny"),
        "load_model_config": llm.load_config,
        "exec_tool": lambda name, args, ws: tools.execute(name, args, workspace_root=ws),
        "judge": guard_mod.judge,
        "audit": None,
        "is_cancelled": lambda: False,
        "tool_schemas": tools.schemas,
        "model_declares_image": appconfig.model_declares_image,
        "splitter_factory": ReasoningSplitter,
    }
    kwargs.update(overrides)
    return LoopDeps(**kwargs)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated %d chars]" % (len(text) - limit)


def _has_image_block(messages: list[dict]) -> bool:
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    return True
    return False


def _duplicate_hint(name: str, args: dict) -> str:
    """重复调用被拒时给模型的可操作提示：改哪儿才有新结果。"""
    if name == "read_file":
        return ("  要继续读同一个文件就**只改 pages 参数**指定页码（例如 "
                "pages=\"9-34\"，capabilities.ocr.maxPages 是单次上限），"
                "或换一个 path；原样重发不会有新结果。")
    return "  换参数或换工具再来；原样重发不会产生新结果。"


#: 「写了一半的正文」落盘节奏：边流边存，最多每 2 秒写一次（关应用是硬杀，
#: 只有已经落盘的东西才留得下来）。上限 1500 字，够接上话头，也不会把断点文件写肥。
PARTIAL_EVERY_S = 2.0
PARTIAL_EVERY_CHARS = 600   # 快流别只靠时间节流：攒够这么多字也立刻落盘
PARTIAL_MAX = 1500


def _flush_partial(deps, text_parts, rounds) -> None:
    """把这一轮写到一半的正文报给断点库（没写东西就不报）。"""
    body = "".join(text_parts).strip()
    if body:
        _checkpoint(deps, {"event": "partial", "text": body[:PARTIAL_MAX], "rounds": rounds})


def _checkpoint(deps, payload: dict) -> None:
    """把运行状态报给上层（断点文件）。断点写失败绝不许影响这一轮对话。"""
    cb = getattr(deps, "checkpoint", None)
    if cb is None:
        return
    body = dict(payload)
    if getattr(deps, "run_id", ""):
        body.setdefault("run_id", deps.run_id)
    try:
        cb(body)
    except Exception:                                    # noqa: BLE001
        pass


def _is_retryable(error: dict) -> bool:
    """这个错误是「网断了/瞬时故障」还是「这事本身不行」。

    只退避重试前者 —— 401/404 之类的重试一百次也没用，反而烧钱。
    """
    code = str(error.get("code") or "").upper()
    if code in NET_RETRY_CODES:
        return True
    status = error.get("httpStatus")
    return isinstance(status, int) and status in NET_RETRY_HTTP


def _sleep_interruptible(seconds: float, is_cancelled) -> None:
    """可打断的等待（用户点取消就别再等）。"""
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if is_cancelled():
            return
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _call_signature(name: str, args: dict) -> str:
    """调用签名（工具 + 规范化参数）——只有同一签名的失败才算「在重复试同一件事」。"""
    try:
        return "%s|%s" % (name, _canonical([name, args]))
    except Exception:                                    # noqa: BLE001
        return "%s|%r" % (name, args)


def _tool_refusal_note(reason: str, name: str, last_error: str) -> str:
    """拒绝执行某个工具调用时给模型的话：为什么拒 + 真实错误 + 该换什么。"""
    parts = ["%s，本次调用不再执行。" % reason]
    if last_error:
        parts.append("最后一次的错误是：%s" % last_error)
    parts.append("下一步：%s" % tool_lessons.alternatives_for(name))
    parts.append("如果确实做不到，就直接把失败原因告诉用户，别再换花样重试。")
    return " ".join(parts)


def _canonical(args: Any) -> str:
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(args)


def _norm_args(raw: Any) -> tuple[dict, str | None]:
    """把 tool_call 的 arguments 统一解析成 dict。优先用 llm.parse_tool_arguments。"""
    if isinstance(raw, dict):
        return raw, None
    try:
        import llm
        return llm.parse_tool_arguments(raw if isinstance(raw, str) else "")
    except Exception:
        text = (raw or "").strip() if isinstance(raw, str) else ""
        if not text:
            return {}, None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            return {}, "arguments 不是合法 JSON: %s" % e
        if not isinstance(parsed, dict):
            return {}, "arguments 必须是 JSON 对象"
        return parsed, None


def _tool_message(tool_call_id: str, text: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": _truncate(text)}


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


def _pre_step(ctx: LoopContext, deps: LoopDeps) -> list[dict]:
    """挂载点一（pre-step）：派发前做上下文治理。返回发送视图。

    governor 缺失（老调用/测试禁用）时原样发 list(messages)。
    库是唯一事实源：返回的是投影副本，ctx.messages 不被改。
    """
    governor = deps.context_governor
    if governor is None:
        return list(ctx.messages)
    try:
        projected, _changed = governor.ensure_before_dispatch(ctx.messages)
    except Exception:
        # 挂载点一失败不挡对话（同类桌面客户端的通行纪律）：发原始视图
        return list(ctx.messages)
    return projected if isinstance(projected, list) else list(ctx.messages)


def _drain_governor_events(governor: Any) -> list[dict]:
    """把 governor 攒的 context.compacted 事件翻成本 loop 的事件形状。"""
    if governor is None:
        return []
    drain = getattr(governor, "drain_events", None)
    if drain is None:
        return []
    try:
        raw = drain()
    except Exception:
        return []
    out = []
    for item in raw or []:
        data = dict(item.get("data") or {})
        out.append({"type": EVENT_CONTEXT_COMPACTED, **data})
    return out


def run(ctx: LoopContext, *, deps: LoopDeps) -> Iterator[dict]:
    """公共入口：跑一轮对话，并把运行断点报给 ``deps.checkpoint``。

    断点语义（2026-09-25）：正常跑完 → ``end/done``；报错结束 → ``end/failed``；
    被网络拖到放弃 → ``interrupt``（可续）；迭代被提前掐断 → 什么都不写，
    于是状态留在 ``running``，应用下次启动时会被判为中断。
    """
    finished = False
    exit_code = ""
    exit_retryable = False
    exit_status = None
    try:
        for ev in _run(ctx, deps=deps):
            kind = ev.get("type")
            if kind == EVENT_DONE:
                finished = True
            elif kind == EVENT_ERROR:
                exit_code = str(ev.get("code") or "ERROR")
                exit_retryable = bool(ev.get("retryable"))
                exit_status = ev.get("httpStatus")
            yield ev
    finally:
        if finished:
            _checkpoint(deps, {"event": "end", "status": "done"})
        elif exit_code:
            # 瞬时故障（断网/限流/5xx）拖到放弃 = 中断（可续，等用户点「继续」）；
            # 其它错误（401/404/参数不对…）重试也没用 = 失败（不给「继续」入口）。
            transient = exit_retryable or exit_code.upper() in NET_RETRY_CODES \
                or (isinstance(exit_status, int) and exit_status in NET_RETRY_HTTP)
            if transient:
                _checkpoint(deps, {"event": "end", "status": "interrupted",
                                   "reason": "网络/接口异常", "interrupt_hint": exit_code})
            else:
                _checkpoint(deps, {"event": "end", "status": "failed", "reason": exit_code})


def _run(ctx: LoopContext, *, deps: LoopDeps) -> Iterator[dict]:
    """跑一轮用户消息引发的完整循环，逐个 yield 事件。"""
    try:
        yield from _run_inner(ctx, deps)
    except Exception as e:  # 契约：任何未预期异常都变成 chat.error，不许抛出去
        yield {"type": EVENT_ERROR, "code": INTERNAL, "message": "%s: %s" % (type(e).__name__, e)}


def _run_inner(ctx: LoopContext, deps: LoopDeps) -> Iterator[dict]:
    messages = ctx.messages
    if ctx.system_prompt and not any(m.get("role") == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": ctx.system_prompt})

    # 解析模型配置（拿默认模型名）
    try:
        cfg = deps.load_model_config()
    except Exception as e:
        yield {"type": EVENT_ERROR, "code": getattr(e, "code", CONFIG_ERROR),
               "message": str(e)}
        return

    model = ctx.model or getattr(cfg, "model", "") or ""

    # 图片能力门：声明式判定，不声明就不送、不猜
    if _has_image_block(messages) and not deps.model_declares_image(model):
        yield {"type": EVENT_ERROR, "code": MODEL_NO_IMAGE_INPUT,
               "message": "模型 %s 未声明支持图片输入（capabilities.models.<模型>.inputModalities "
                          "不含 image），已拒绝送图" % model}
        return

    splitter = deps.splitter_factory() if deps.splitter_factory else None
    recent: list[str] = []
    fail_streak: dict[str, int] = {}       # 调用签名（工具+参数）→ 失败次数
    fail_total: dict[str, int] = {}        # 工具 → 本轮总失败次数
    last_error: dict[str, str] = {}        # 工具 → 最近一次错误原文（给模型解释）
    cooldown: set = set()                  # 本轮冷掉的工具
    tool_refusals = 0                      # 冷却/同参拒绝累计
    succeeded_keys: set = set()            # 本轮成功过的调用签名
    rounds = 0
    tool_calls_run = 0
    approval_seq = 0
    tools_arg = deps.tool_schemas() if ctx.use_tools else None

    # Mount point 2 (request-error, contract 7.4): per-request max_tokens
    # override (output-cap overflow only; ctx.max_tokens is never touched)
    # and a forced derived view handed back by the governor after a
    # compact_retry decision. The override lasts only for this dispatch.
    max_tokens_override: int | None = None
    forced_view: list[dict] | None = None
    lower_max_tokens_used = 0          # whole-run budget: at most 1 resend
    compact_retry_used = 0             # whole-run budget: OVERFLOW_RETRY
    action_nudges = 0                  # whole-run budget: "说了要做却没调用" 纠一次
    dup_refusals = 0                   # whole-run budget: 重复调用拒几次后再终止
    net_retries = 0                    # 本轮已自动重试次数（断网退避）
    net_retry_started = time.monotonic()
    done_call_ids: set = set()         # 已执行完的调用 id（断点用：续跑不重做）

    while rounds < max(1, ctx.max_loop):
        rounds += 1
        _checkpoint(deps, {"event": "round", "rounds": rounds,
                           "max_loop": max(1, ctx.max_loop),
                           "done_calls": sorted(done_call_ids)})
        # Mount point 1 (pre-step, contract 7.4): before every round measure,
        # downgrade for free when possible, L2 only if needed. send_messages
        # is the derived view; ctx.messages (the session store) stays intact.
        if forced_view is not None:
            send_messages = forced_view
            forced_view = None
        else:
            send_messages = _pre_step(ctx, deps)
        for ev in _drain_governor_events(deps.context_governor):
            yield ev
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = "stop"
        usage = None
        tool_calls: list[dict] = []
        retry_round = False
        net_retry_wait = 0.0           # >0 = 这一轮要退避重试
        partial_saved_at = 0.0         # 这一轮的「半截正文」上次落盘时刻
        partial_saved_chars = 0        # 上次落盘时已经写了多少字（攒够也提前存）

        for ev in deps.llm_stream(cfg, list(send_messages), tools=tools_arg,
                                  model=ctx.model,
                                  max_tokens=max_tokens_override if max_tokens_override is not None
                                  else ctx.max_tokens,
                                  is_cancelled=deps.is_cancelled):
            kind = ev.get("type")
            if kind == "delta":
                chunk = ev.get("text") or ""
                if splitter is not None:
                    # text_parts 收的是「分流后的正文」，不是原始 delta ——
                    # 原始 delta 里带 <thinking> 标签，以前它被当成正文落库
                    # 又回灌给模型，界面上就露出了一整段思考（2026-09-24 修）。
                    reason, body = splitter.feed(chunk)
                    if reason:
                        reasoning_parts.append(reason)
                        yield {"type": EVENT_REASONING, "text": reason}
                    if body:
                        text_parts.append(body)
                        yield {"type": EVENT_DELTA, "text": body}
                else:
                    text_parts.append(chunk)
                    yield {"type": EVENT_DELTA, "text": chunk}
                # 边流边存「写了一半的正文」（节流）：用户点中断/关应用是硬杀进程，
                # 只靠循环收尾那段代码是存不下来的（2026-09-25 实测踩过）。
                written = sum(len(p) for p in text_parts)
                if (time.monotonic() - partial_saved_at >= PARTIAL_EVERY_S
                        or written - partial_saved_chars >= PARTIAL_EVERY_CHARS):
                    partial_saved_at = time.monotonic()
                    partial_saved_chars = written
                    _flush_partial(deps, text_parts, rounds)
            elif kind == "reasoning":
                reasoning_parts.append(ev.get("text") or "")
                yield {"type": EVENT_REASONING, "text": ev.get("text") or ""}
            elif kind == "done":
                finish_reason = ev.get("finishReason") or "stop"
                usage = ev.get("usage")
                tool_calls = list(ev.get("toolCalls") or [])
            elif kind == "error":
                # Mount point 2: ask the governor what to do before giving up.
                error: dict = {"code": ev.get("code") or "UNKNOWN",
                               "message": ev.get("message") or ""}
                if ev.get("httpStatus") is not None:
                    error["httpStatus"] = ev.get("httpStatus")
                governor = deps.context_governor
                decision = None
                if governor is not None:
                    try:
                        decision = governor.on_request_error(send_messages, error)
                    except Exception:
                        decision = None
                if decision is None:
                    # 断网/瞬时故障：先自己退避重试几次（用户口径：2-3 次、
                    # 总窗口 2 分钟）；超出就报中断落断点，等用户点「继续」。
                    elapsed = time.monotonic() - net_retry_started
                    if (_is_retryable(error) and net_retries < NET_RETRY_MAX
                            and elapsed < NET_RETRY_WINDOW):
                        net_retries += 1
                        net_retry_wait = min(NET_RETRY_BASE * (2 ** (net_retries - 1)),
                                             max(1.0, NET_RETRY_WINDOW - elapsed))
                        yield {"type": EVENT_NOTICE, "code": "NET_RETRY",
                               "text": "%s（网络中断，%d 秒后第 %d 次重试）"
                                          % (error.get("message") or "网络异常",
                                             int(net_retry_wait), net_retries)}
                        break
                    if _is_retryable(error):
                        yield {"type": EVENT_ERROR, "code": error["code"],
                               # 显式标记「这是瞬时故障」：光看错误码不行 ——
                               # 限流会包成 HTTP_ERROR，只看码会误判成「不可续」
                               # （2026-09-25 端到端实测踩过：免费模型限流后没有
                               # 「继续」入口，但提示词里却写着「点继续即可」）。
                               "retryable": True, "httpStatus": error.get("httpStatus"),
                               "message": "%s（已自动重试 %d 次仍未恢复，已存断点，"
                                          "恢复网络后点「继续」即可接着跑）"
                                          % (error.get("message") or "", net_retries)}
                        return
                    yield {"type": EVENT_ERROR, "code": error["code"],
                           "message": error["message"]}
                    return
                # contract 7.5: context.compacted goes out before the retry
                for gev in _drain_governor_events(governor):
                    yield gev
                action = decision.get("action")
                if action in ("propagate", "abort"):
                    yield {"type": EVENT_ERROR, "code": error["code"],
                           "message": error["message"]}
                    return
                if action == "lower_max_tokens":
                    if lower_max_tokens_used >= 1:
                        yield {"type": EVENT_ERROR, "code": error["code"],
                               "message": error["message"]}
                        return
                    lower_max_tokens_used += 1
                    max_tokens_override = decision.get("maxTokens")
                    retry_round = True
                    break
                if action == "compact_retry":
                    if compact_retry_used >= context_mechanism.OVERFLOW_RETRY:
                        yield {"type": EVENT_ERROR, "code": error["code"],
                               "message": error["message"]}
                        return
                    compact_retry_used += 1
                    forced_view = decision.get("messages")
                    if forced_view is None:
                        forced_view = list(send_messages)
                    retry_round = True
                    break
                if action == "tripped":
                    warning = decision.get("warning") or ""
                    exits = decision.get("exits") or []
                    exits_text = " / ".join(str(x) for x in exits)
                    yield {"type": EVENT_ERROR, "code": CONTEXT_TRIPPED,
                           "message": "%s | recovery exits: %s" % (warning, exits_text)
                           if exits_text else warning}
                    return
                # unknown action: treat as propagate
                yield {"type": EVENT_ERROR, "code": error["code"],
                       "message": error["message"]}
                return

        # A governor-directed resend of THIS round: the failed partial output
        # must not be stored as an assistant message, so we skip everything
        # below (splitter flush, _append_assistant) and go straight back to
        # the top of the outer while.
        if retry_round:
            if splitter is not None:
                splitter.flush()
            continue

        # 冲掉分流器里悬挂的尾巴（标签可能被切在流末尾）
        if splitter is not None:
            reason, body = splitter.flush()
            if reason:
                reasoning_parts.append(reason)
                yield {"type": EVENT_REASONING, "text": reason}
            if body:
                text_parts.append(body)
                yield {"type": EVENT_DELTA, "text": body}

        if net_retry_wait > 0:
            wait = net_retry_wait
            net_retry_wait = 0.0
            _sleep_interruptible(wait, deps.is_cancelled)
            if deps.is_cancelled():
                _flush_partial(deps, text_parts, rounds)
                yield {"type": EVENT_DONE, "finishReason": "cancelled", "usage": usage,
                       "rounds": rounds, "toolCallsRun": tool_calls_run}
                return
            rounds = max(0, rounds - 1)      # 退避重试不占轮次预算
            continue

        if deps.is_cancelled():
            _flush_partial(deps, text_parts, rounds)
            _append_assistant(messages, "".join(text_parts), [])
            yield {"type": EVENT_DONE, "finishReason": "cancelled", "usage": usage,
                   "rounds": rounds, "toolCallsRun": tool_calls_run}
            return

        _append_assistant(messages, "".join(text_parts), tool_calls)

        if not tool_calls:
            # 「说了要做却没做」机器兜底（2026-09-24 真机实证）：模型回一句
            # 「好，我去查」然后一个工具调用都不发，这一轮就这么结束了，用户
            # 看到的就是「没下文」。提示词里写了规矩，这里给它一次机会自己纠。
            # 走 system 角色：chat_persist 明确不落 system 行，所以这句提醒
            # 进模型上下文但不进会话库（界面上不会冒出一条假消息）。
            body_text = "".join(text_parts)
            if action_nudges < MAX_ACTION_NUDGES and promises_action(body_text):
                action_nudges += 1
                messages.append({"role": "system", "content": ACTION_PROMISE_NUDGE})
                continue
            yield {"type": EVENT_DONE, "finishReason": finish_reason, "usage": usage,
                   "rounds": rounds, "toolCallsRun": tool_calls_run}
            return

        for tool_call in tool_calls:
            call_id = tool_call.get("id") or ""
            name = tool_call.get("name") or ""
            args, arg_error = _norm_args(tool_call.get("arguments"))

            if arg_error:
                note = "工具参数无法解析：%s" % arg_error
                yield {"type": EVENT_TOOL_RESULT, "id": call_id, "name": name, "ok": False,
                       "content": note, "errorCode": TOOL_ARGUMENTS_INVALID,
                       "errorMessage": arg_error, "durationMs": 0, "decision": "deny"}
                messages.append(_tool_message(call_id, note))
                continue

            key = _canonical([name, args])
            # Both guards run BEFORE judge/approval on purpose: a call we are
            # going to refuse must not pop an approval dialog first.
            if key in recent and key in succeeded_keys:
                # 只拦「重复一个已经成功过的调用」（重发不会产生新结果）。
                # 失败过的调用重发是正常重试，归失败守卫管（TOOL_FAIL_STREAK_BUDGET），
                # 免得同一件事被两个守卫各拒一次（2026-09-25）。
                dup_refusals += 1
                note = ("工具 %s 在最近 %d 轮内被用同样参数重复调用，本次调用被拒绝。%s"
                        % (name, DUPLICATE_TOOL_WINDOW, _duplicate_hint(name, args)))
                messages.append(_tool_message(call_id, note))
                yield {"type": EVENT_TOOL_RESULT, "id": call_id, "name": name, "ok": False,
                       "content": note, "errorCode": DUPLICATE_TOOL, "errorMessage": note,
                       "durationMs": 0, "decision": "deny"}
                if dup_refusals > MAX_DUPLICATE_REFUSALS:
                    yield {"type": EVENT_ERROR, "code": DUPLICATE_TOOL,
                           "message": note + "（已累计 %d 次重复，终止本轮）" % dup_refusals}
                    return
                continue

            call_key = _call_signature(name, args)
            streak = fail_streak.get(call_key, 0)
            reason = ""
            if name in cooldown:
                reason = "工具 %s 本轮已冷却（失败太多，换参数也没用）" % name
            elif streak >= TOOL_FAIL_STREAK_BUDGET:
                reason = "同一个调用（%s，同样的参数）已经失败 %d 次" % (name, streak)
            if reason:
                tool_refusals += 1
                note = _tool_refusal_note(reason, name, last_error.get(name, ""))
                messages.append(_tool_message(call_id, note))
                yield {"type": EVENT_TOOL_RESULT, "id": call_id, "name": name, "ok": False,
                       "content": note, "errorCode": TOOL_FAILURE_LIMIT, "errorMessage": note,
                       "durationMs": 0, "decision": "deny"}
                if tool_refusals > MAX_TOOL_REFUSALS:
                    yield {"type": EVENT_ERROR, "code": TOOL_FAILURE_LIMIT,
                           "message": note + "（已累计 %d 次，终止本轮）" % tool_refusals}
                    return
                continue

            # 权限口径（2026-09-25，参照 Hermes Agent 的通行做法）：只按命令模式拦，其余放行。
            # 审批事件与 tools.list 的键名保持原契约（level / requiresApproval /
            # canAlwaysAllow / reason），值换成新口径：level 填 tier
            # （hardline / dangerous / ""），canAlwaysAllow 恒 false（没有
            # 「总是允许」了）。键名由界面侧另一路收尾时再统一改。
            verdict = deps.judge(name, args)
            tier = str(getattr(verdict, "tier", "") or "")
            requires = bool(getattr(verdict, "requires_approval", False))
            can_always = False
            reason = str(getattr(verdict, "reason", "") or "")

            yield {"type": EVENT_TOOL_CALL, "id": call_id, "name": name, "arguments": args,
                   "level": tier, "requiresApproval": requires,
                   "canAlwaysAllow": can_always, "reason": reason}

            if requires:
                approval_seq += 1
                approval_id = "apr_%d" % approval_seq
                payload = {"approvalId": approval_id, "toolCallId": call_id, "name": name,
                           "arguments": args, "level": tier, "reason": reason,
                           "canAlwaysAllow": can_always}
                yield {"type": EVENT_APPROVAL, **payload}
                raw_decision = deps.request_approval(payload)
                final_decision = raw_decision if raw_decision in _VALID_DECISIONS else "deny"
            else:
                final_decision = "allow_once"

            _audit_decision(deps, ctx, call_id=call_id, name=name, args=args,
                            level=tier, decision=final_decision, reason=reason)

            if final_decision == "deny":
                note = "用户拒绝了这次工具调用（%s）" % (reason or "no reason given")
                messages.append(_tool_message(call_id, note))
                yield {"type": EVENT_TOOL_RESULT, "id": call_id, "name": name, "ok": False,
                       "content": note, "errorCode": "DENIED", "errorMessage": reason,
                       "durationMs": 0, "decision": "deny"}
                continue

            started = time.time()
            result = deps.exec_tool(name, args, ctx.workspace_root)
            duration_ms = int((time.time() - started) * 1000)
            ok = bool(getattr(result, "ok", False))
            content = getattr(result, "content", "") or ""
            error_code = getattr(result, "error_code", None)
            error_message = getattr(result, "error_message", None)

            _audit_tool_call(deps, ctx, call_id=call_id, name=name, args=args,
                             level=tier, decision=final_decision, result=result,
                             duration_ms=duration_ms)

            messages.append(_tool_message(call_id, content))
            tool_calls_run += 1
            yield {"type": EVENT_TOOL_RESULT, "id": call_id, "name": name, "ok": ok,
                   "content": _truncate(content), "errorCode": error_code,
                   "errorMessage": error_message, "durationMs": duration_ms,
                   "decision": final_decision}

            if ok:
                succeeded_keys.add(key)
                fail_streak.pop(call_key, None)
                fail_total.pop(name, None)
                last_error.pop(name, None)
                if deps.lessons is not None:
                    try:
                        deps.lessons.note_success(name, args)
                    except Exception:                        # 经验库坏了不许影响主流程
                        pass
            else:
                fail_streak[call_key] = streak + 1
                fail_total[name] = fail_total.get(name, 0) + 1
                last_error[name] = " ".join(
                    str(error_message or error_code or "").split())[:200]
                if fail_total[name] >= TOOL_FAIL_TOTAL_BUDGET:
                    cooldown.add(name)
                persistent = (fail_streak[call_key] >= TOOL_FAIL_STREAK_BUDGET
                              or name in cooldown)
                if persistent and deps.lessons is not None:
                    try:
                        deps.lessons.record(name, args, error_code, error_message)
                    except Exception:
                        pass
            recent.append(key)
            done_call_ids.add(call_id)
            _checkpoint(deps, {"event": "tools", "rounds": rounds,
                               "done_calls": sorted(x for x in done_call_ids if x),
                               "pending": [c.get("id") for c in tool_calls
                                           if c.get("id") not in done_call_ids]})
            recent[:] = recent[-DUPLICATE_TOOL_WINDOW:]

    # 循环上限
    messages.append({"role": "assistant",
                     "content": "已达最大循环次数 %d，停止。" % max(1, ctx.max_loop)})
    yield {"type": EVENT_ERROR, "code": LOOP_LIMIT,
           "message": "已达最大循环次数 %d" % max(1, ctx.max_loop)}


def _append_assistant(messages: list[dict], text: str, tool_calls: list[dict]) -> None:
    msg: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = [
            {"id": tc.get("id") or "", "type": "function",
             "function": {"name": tc.get("name") or "",
                          "arguments": tc.get("arguments") if isinstance(
                              tc.get("arguments"), str) else json.dumps(
                                  tc.get("arguments") or {}, ensure_ascii=False)}}
            for tc in tool_calls
        ]
    messages.append(msg)


def _audit_decision(deps: LoopDeps, ctx: LoopContext, *, call_id: str, name: str,
                    args: dict, level: str, decision: str, reason: str) -> None:
    audit = deps.audit
    if audit is None:
        return
    try:
        # level 这一列现在存 tier（hardline / dangerous / ""），列名与 schema 不动。
        audit.log_decision(tool_name=name, level=level, decision=decision,
                           arguments=args, session_id=ctx.session_id,
                           tool_call_id=call_id, reason=reason)
    except Exception:
        pass


def _audit_tool_call(deps: LoopDeps, ctx: LoopContext, *, call_id: str, name: str,
                     args: dict, level: str, decision: str, result: Any,
                     duration_ms: int) -> None:
    audit = deps.audit
    if audit is None:
        return
    ok = bool(getattr(result, "ok", False))
    try:
        audit.log_tool_call(id=call_id, tool_name=name, arguments=args,
                            level=level, decision=decision,
                            session_id=ctx.session_id,
                            result={"content": getattr(result, "content", "")} if ok else None,
                            error=None if ok else {"code": getattr(result, "error_code", None),
                                                   "message": getattr(result, "error_message", None)},
                            duration_ms=duration_ms)
    except Exception:
        pass