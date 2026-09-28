"""Subagent delegation engine (design: docs/2026-09-25-subagent-design.md).

A bounded, non-blocking, memory-disciplined child-agent dispatcher:

- hard cap of 3 concurrent nodes (the user's number, an absolute constant,
  never derived from the host machine);
- dispatch() returns immediately; results come back later via
  consolidated_message() / join();
- one gc.collect() at the batch boundary only (never per round), with
  rss_before / rss_peak / rss_after_gc recorded on the Batch;
- node registry entries are removed on done / interrupted / failed, so
  active() drains to empty and node references are released;
- memory discipline: when a node finishes only its NodeResult (summary +
  usage) survives in memory - no message history, no streaming buffers,
  no raw tool output; the transcript lives in an append-only file on disk;
- bounded: tasks > effective limit are rejected with TOO_MANY_TASKS, no
  unbounded queueing;
- depth = 1: a task that asks for delegation / approval tools is rejected
  before any thread starts, and the child's node_ctx carries a
  forbidden_tools whitelist.

Stdlib only. Python 3.11+.
"""

from __future__ import annotations

import gc
import os
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Tunables. Absolute numbers on purpose: the baseline machine is 4c/8GB;
# the verification host's resources must never feed into any threshold.
# ---------------------------------------------------------------------------

MAX_CONCURRENT_HARD_CAP = 3              # concurrent node cap (user's number)
NODE_ID_PREFIX = "sa-"
MEMORY_GATE_RSS_BYTES = 500 * 1024 * 1024       # pre-dispatch gate
MEMORY_WATCHDOG_RSS_BYTES = 700 * 1024 * 1024   # in-flight watchdog
STALL_SECONDS = 600                      # 600s without output = suspected stall

# GIL 租约（2026-09-26 实测）：三个纯 CPU 子代理线程会把主循环护航到 p95 744ms
# （Python 默认切换间隔 5ms；8 核实测 n=7 采样/0.8s）。把间隔压到 0.5ms 后
# p95 → 24.6ms。节点活着时按下，最后一个节点结束时还原（全局计数，多批次安全）。
NODE_SWITCH_INTERVAL_S = 0.0005

_GIL_LEASE_LOCK = threading.Lock()
_GIL_LEASES = 0
_GIL_SAVED_INTERVAL = None


def _gil_lease_take() -> None:
    """一个节点开始：压小 GIL 切换间隔，别让纯 CPU 节点饿死主循环。"""
    global _GIL_LEASES, _GIL_SAVED_INTERVAL
    with _GIL_LEASE_LOCK:
        _GIL_LEASES += 1
        if _GIL_LEASES == 1:
            _GIL_SAVED_INTERVAL = sys.getswitchinterval()
            sys.setswitchinterval(NODE_SWITCH_INTERVAL_S)


def _gil_lease_release() -> None:
    """一个节点结束：最后一个走的时候把切换间隔还原。"""
    global _GIL_LEASES, _GIL_SAVED_INTERVAL
    with _GIL_LEASE_LOCK:
        _GIL_LEASES = max(0, _GIL_LEASES - 1)
        if _GIL_LEASES == 0 and _GIL_SAVED_INTERVAL is not None:
            sys.setswitchinterval(_GIL_SAVED_INTERVAL)
            _GIL_SAVED_INTERVAL = None

_MB = 1024 * 1024
_GB = 1024 * 1024 * 1024

#: Tool names a child agent must never get. Depth is 1: children cannot
#: spawn children, cannot ask for human approval, cannot write persistent
#: rules / memory. The engine rejects a task up-front if the caller tries
#: to inject any of these into the task payload.
FORBIDDEN_CHILD_TOOL_NAMES = frozenset({
    "delegate", "delegate_task",              # no sub-sub-agents
    "approval", "request_approval", "ask_user", "clarify",
    "save_rule", "remove_rule", "memory", "cronjob",
})

#: Absolute perf budgets used by tests/test_subagent_perf.py. Never derived
#: from the host machine's total memory or CPU count.
PERF_RSS_PEAK_BUDGET_BYTES = 500 * 1024 * 1024      # <= 500 MB absolute
PERF_MAINLOOP_P95_SECONDS = 0.300                   # p95 <= 300 ms


# ---------------------------------------------------------------------------
# Memory / CPU probes (fail open to None; callers treat None as "unknown").
# ---------------------------------------------------------------------------

def _read_proc_meminfo(field_name: str) -> Optional[int]:
    try:
        with open("/proc/meminfo", "r", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith(field_name):
                    parts = line.split()        # "MemAvailable:  8123456 kB"
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
                    return None
    except (OSError, ValueError):
        pass
    return None


def available_memory_bytes() -> Optional[int]:
    """Best-effort available RAM in bytes; None when it cannot be read."""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                # 必须是完整的 9 个字段：少一个 dwLength 就对不上，API 直接失败
                # （2026-09-26 真机 bug：少写 ullTotalPageFile/ullAvailPageFile 两个
                # 字段 → GlobalMemoryStatusEx 返回 0 → 可用内存被读成 0 → 降级到
                # 1 个节点。单元测试是注入数值的，所以全绿也发现不了。）
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_uint64),
                    ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64),
                    ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64),
                    ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(  # type: ignore[attr-defined]
                ctypes.byref(stat))
            if not ok:
                return None                      # 读不到就说读不到，别拿 0 当事实
            av = int(stat.ullAvailPhys)
            return av if av > 0 else None
        except Exception:
            return None
    return _read_proc_meminfo("MemAvailable:")


def rss_bytes() -> Optional[int]:
    """Best-effort current-process RSS in bytes; None when it cannot be read."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            import ctypes.wintypes

            class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.wintypes.DWORD),
                    ("PageFaultCount", ctypes.wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            pmc = PROCESS_MEMORY_COUNTERS_EX()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
            # 2026-09-26 真机 bug：windll 默认 restype=c_int，会把 64 位伪句柄
            # (-1) 截断成 32 位 → 句柄非法 → GetProcessMemoryInfo 失败 →
            # WorkingSetSize 永远是 0，而 0 被当成「进程不吃内存」→ 内存闸门失效。
            # 所以：显式声明 restype/argtypes，并且检查返回值。
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
            kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.wintypes.HANDLE,
                ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
                ctypes.wintypes.DWORD,
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.wintypes.BOOL
            handle = kernel32.GetCurrentProcess()
            if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return None
            return int(pmc.WorkingSetSize)
        except Exception:
            return None
    try:
        with open("/proc/self/status", "r", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()        # "VmRSS:     123456 kB"
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
                    return None
    except (OSError, ValueError):
        pass
    return None


def effective_limit(memory_available: Optional[int] = None,
                    cpu_count: Optional[int] = None) -> tuple[int, str]:
    """Effective node cap for a dispatch.

    limit = min(MAX_CONCURRENT_HARD_CAP, memory tier, cpu_count // 2).
    Memory tiers: <4GB -> 1, 4-8GB -> 2, >=8GB -> 3; unknown -> 3.
    Both default to None, which probes the machine (available_memory_bytes /
    os.cpu_count). Returns (limit, reason); the reason is user-readable and
    carries the concrete numbers, e.g.
    "available memory 5.4GB in 4-8GB tier, 2 nodes".
    """
    if memory_available is None:
        memory_available = available_memory_bytes()
    if cpu_count is None:
        cpu_count = os.cpu_count()

    if memory_available is None:
        mem_limit = MAX_CONCURRENT_HARD_CAP
        mem_reason = ("available memory unknown, assume %d nodes"
                      % MAX_CONCURRENT_HARD_CAP)
    elif memory_available < 4 * _GB:
        mem_limit = 1
        mem_reason = ("available memory %.1fGB < 4GB, 1 node"
                      % (memory_available / _GB))
    elif memory_available < 8 * _GB:
        mem_limit = 2
        mem_reason = ("available memory %.1fGB in 4-8GB tier, 2 nodes"
                      % (memory_available / _GB))
    else:
        mem_limit = MAX_CONCURRENT_HARD_CAP
        mem_reason = ("available memory %.1fGB >= 8GB, %d nodes"
                      % (memory_available / _GB, MAX_CONCURRENT_HARD_CAP))

    if not cpu_count or cpu_count <= 0:
        cpu_limit = MAX_CONCURRENT_HARD_CAP
        cpu_reason = ("cpu count unknown, assume %d nodes"
                      % MAX_CONCURRENT_HARD_CAP)
    else:
        cpu_limit = max(1, cpu_count // 2)
        cpu_reason = "%d cpus -> at most %d nodes" % (cpu_count, cpu_limit)

    limit = min(MAX_CONCURRENT_HARD_CAP, mem_limit, cpu_limit)
    parts = [
        "effective node limit %d" % limit,
        mem_reason,
        cpu_reason,
    ]
    if limit < MAX_CONCURRENT_HARD_CAP:
        parts.append("below the hard cap of %d (degraded)" % MAX_CONCURRENT_HARD_CAP)
    return limit, "; ".join(parts)


# ---------------------------------------------------------------------------
# Errors & result records.
# ---------------------------------------------------------------------------

class DelegationError(Exception):
    """Delegation refused / failed. code is one of:

    TOO_MANY_TASKS / MEMORY_GATE / DISABLED / STALLED
    """

    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__("%s: %s" % (code, message))


@dataclass
class NodeResult:
    node_id: str
    goal: str
    status: str                      # "done" / "interrupted" / "failed" / "stalled"
    summary: str = ""
    error: str = ""
    usage: dict | None = None        # {"input_tokens": .., "output_tokens": ..}
    transcript_path: str = ""
    partial: str = ""                # half-finished body at interruption
    elapsed_s: float = 0.0


@dataclass
class Batch:
    batch_id: str
    session_id: str
    node_ids: list[str]
    status: str                      # "running" / "done" / "partial"
    limit: int
    limit_reason: str = ""
    degraded: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    rss_before: Optional[int] = None
    rss_peak: Optional[int] = None
    rss_after_gc: Optional[int] = None


# ---------------------------------------------------------------------------
# Transcript: one append-only file per node, atomically persisted.
# ---------------------------------------------------------------------------

class _Transcript:
    """Append-only per-node transcript file.

    The disk file is the source of truth; the in-memory line buffer exists
    only while the node runs and is dropped by close(), so a finished node
    leaves nothing but the NodeResult behind.
    """

    def __init__(self, directory: Optional[str], node_id: str, goal: str):
        self.node_id = node_id
        self.path: str = ""
        self._chunks: list[str] = []
        if directory:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix="transcript-%s-" % node_id,
                                       suffix=".log", dir=directory)
            os.close(fd)
            self.path = tmp
            self._chunks.append("# subagent %s\n# goal: %s\n" % (node_id, goal))
            self._persist()

    def _persist(self) -> None:
        """Atomically rewrite the transcript (mkstemp + os.replace)."""
        if not self.path:
            return
        fd, tmp = tempfile.mkstemp(prefix="transcript-%s-rewrite-" % self.node_id,
                                   suffix=".log",
                                   dir=os.path.dirname(self.path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for chunk in self._chunks:
                    fh.write(chunk)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def write(self, text: str) -> None:
        if not self.path or not text:
            return
        self._chunks.append(text)
        self._persist()

    def tail_text(self, limit: int = 1500) -> str:
        """Last body text (used for the `partial` field on interruption)."""
        body = "".join(self._chunks)
        return body[-limit:]

    def close(self) -> None:
        """Flush and drop the in-memory buffer; disk keeps everything."""
        self._persist()
        self._chunks = []


# ---------------------------------------------------------------------------
# Node bookkeeping (mutable, dropped from the registry on any exit).
# ---------------------------------------------------------------------------

@dataclass
class _Node:
    node_id: str
    goal: str
    batch_id: str
    status: str = "running"
    started_at: float = 0.0
    last_output_at: float = 0.0


_TRUTHY_TASK_KEYS = frozenset({
    "delegate", "delegate_task",
    "approval", "request_approval", "ask_user", "clarify",
    "save_rule", "remove_rule", "memory", "cronjob",
})


def _fence_forbidden_tools(task: dict) -> Optional[str]:
    """Return a rejection reason when a task asks for forbidden tools."""
    # Direct keys: merely carrying "delegate": ... / "request_approval": ...
    # means this task wants a capability children must never have.
    for key in _TRUTHY_TASK_KEYS:
        if task.get(key):
            return ("task asks for forbidden tool %r: child agents cannot "
                    "delegate, seek approval, or write persistent rules"
                    % key)
    # List fields: a tools whitelist containing a forbidden name.
    for key in ("tools", "tool", "allowed_tools", "tool_names"):
        value = task.get(key)
        if not value:
            continue
        items = ([value] if isinstance(value, str)
                 else [v for v in value if isinstance(v, str)]
                 if isinstance(value, (list, tuple, set)) else [])
        for item in items:
            name = item.strip()
            if name in FORBIDDEN_CHILD_TOOL_NAMES:
                return ("task asks for forbidden tool %r: child agents cannot "
                        "delegate, seek approval, or write persistent rules"
                        % name)
    return None


class Delegator:
    """Bounded concurrent sub-agent dispatcher (see module docstring)."""

    def __init__(self, run_node, *, limit=None, emit=None,
                 transcripts_dir=None, collect=None,
                 clock: Callable[[], float] = time.monotonic,
                 rss: Callable[[], Optional[int]] = rss_bytes):
        # run_node(task: dict, node_ctx: dict) -> NodeResult is injected by
        # the caller: it is the real sub-agent loop and stays outside the
        # engine (tests inject fakes here).
        self._run_node = run_node
        self._emit = emit
        self._transcripts_dir = transcripts_dir
        self._collect: Callable[[], int] = (collect if collect is not None
                                            else gc.collect)
        self._clock = clock
        self._rss = rss
        self._limit_override = limit

        self._batches: dict[str, Batch] = {}
        # batch_id -> node_id -> NodeResult (the only surviving payload).
        self._results: dict[str, dict[str, NodeResult]] = {}
        # batch_id -> ordered node ids (consolidation order).
        self._order: dict[str, list[str]] = {}
        # live node registry; entries removed on done/failed/interrupted.
        self._nodes: dict[str, _Node] = {}
        self._cond = threading.Condition()
        self._batch_seq = 0
        self._gc_done: set[str] = set()
        # batch_id -> terminal status, applied only after boundary
        # housekeeping so join() never returns a "finished" batch whose
        # collect()/rss_after_gc/batch_done event have not happened yet.
        self._terminal: dict[str, str] = {}

    # -- public API ---------------------------------------------------------

    def dispatch(self, tasks: list[dict], session_id: str) -> Batch:
        """Start a batch. Returns immediately; never waits for nodes."""
        if not tasks:
            raise DelegationError("TOO_MANY_TASKS", "dispatch: empty task list")

        # Depth=1 fence: reject delegation / approval tool requests before
        # any thread starts.
        for task in tasks:
            reason = _fence_forbidden_tools(task)
            if reason:
                raise DelegationError("DISABLED", reason)

        # Pre-dispatch memory gate (explicit refusal, never silent).
        current_rss = self._rss()
        if current_rss is not None and current_rss > MEMORY_GATE_RSS_BYTES:
            raise DelegationError(
                "MEMORY_GATE",
                "dispatch refused: process RSS %.1fMB exceeds the %.0fMB gate"
                % (current_rss / _MB, MEMORY_GATE_RSS_BYTES / _MB),
                {"rss_bytes": current_rss, "gate_bytes": MEMORY_GATE_RSS_BYTES})

        if self._limit_override is not None:
            limit = int(self._limit_override)
            reason = "limit explicitly set to %d by the caller" % limit
        else:
            limit, reason = effective_limit()

        # Bounded: refuse oversize batches outright, no unbounded queueing.
        if len(tasks) > limit:
            raise DelegationError(
                "TOO_MANY_TASKS",
                "dispatch: %d tasks exceed the effective limit of %d nodes"
                % (len(tasks), limit),
                {"task_count": len(tasks), "limit": limit,
                 "limit_reason": reason})

        with self._cond:
            self._batch_seq += 1
            batch_id = "sb-%d" % self._batch_seq
            node_ids = ["%s%d" % (NODE_ID_PREFIX, i + 1)
                        for i in range(len(tasks))]
            batch = Batch(
                batch_id=batch_id,
                session_id=session_id,
                node_ids=node_ids,
                status="running",
                limit=limit,
                limit_reason=reason,
                degraded=limit < MAX_CONCURRENT_HARD_CAP,
                started_at=self._clock(),
                rss_before=current_rss,
            )
            self._batches[batch_id] = batch
            self._order[batch_id] = list(node_ids)
            self._results[batch_id] = {}

        for index, task in enumerate(tasks):
            node_id = node_ids[index]
            goal = str(task.get("goal") or ("task %d" % (index + 1)))
            node = _Node(node_id=node_id, goal=goal, batch_id=batch_id,
                         started_at=self._clock(), last_output_at=self._clock())
            with self._cond:
                self._nodes[node_id] = node
            _gil_lease_take()
            transcript = _Transcript(self._transcripts_dir, node_id, goal)
            self._event("subagent.start", {
                "node_id": node_id, "batch_id": batch_id, "goal": goal,
                "round": 0,
            })
            transcript.write("start goal=%s\n" % goal)
            worker = threading.Thread(
                target=self._run_one,
                args=(task, node, batch_id, transcript),
                name="subagent-worker-%s" % node_id,
                daemon=True,
            )
            worker.start()
        return batch

    def active(self) -> list[dict]:
        """Currently running nodes (for the UI / RPC layer)."""
        now = self._clock()
        with self._cond:
            rows = []
            for node in self._nodes.values():
                if node.status != "running":
                    continue
                rows.append({
                    "node_id": node.node_id,
                    "goal": node.goal,
                    "status": node.status,
                    "elapsed_s": round(now - node.started_at, 3),
                    "batch_id": node.batch_id,
                })
            return rows

    def batch(self, batch_id: str) -> Optional[Batch]:
        with self._cond:
            return self._batches.get(batch_id)

    def join(self, batch_id: str, timeout: Optional[float] = None) -> Batch:
        """Wait for the whole batch to finish (tests / resume path)."""
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            with self._cond:
                current = self._batches.get(batch_id)
                if current is None:
                    raise KeyError("join: unknown batch %r" % batch_id)
                if current.status != "running":
                    return current
                if deadline is None:
                    self._cond.wait(timeout=0.05)
                else:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        return current
                    self._cond.wait(timeout=min(0.05, remaining))

    def consolidated_message(self, batch_id: str) -> str:
        """Merge a finished batch into ONE message (design section 2.4).

        Failed / interrupted / stalled nodes are reported honestly; a
        failure is never rewritten as a success. When the batch ran
        degraded, the first line says so.
        """
        with self._cond:
            batch = self._batches.get(batch_id)
            order = list(self._order.get(batch_id, []))
            results = dict(self._results.get(batch_id, {}))
        if batch is None:
            raise KeyError("consolidated_message: unknown batch %r" % batch_id)

        lines: list[str] = []
        if batch.status == "running":
            lines.append("(batch %s still running)" % batch_id)
        if batch.degraded:
            lines.append("降级运行：%s" % batch.limit_reason)
        for node_id in order:
            result = results.get(node_id)
            if result is None:
                lines.append("· %s · status unknown" % node_id)
                continue
            mark = {"done": "✓", "failed": "✗", "interrupted": "⚠",
                    "stalled": "✗"}.get(result.status, "·")
            line = "%s %s · %s · %s" % (mark, node_id, result.goal,
                                        result.status)
            if result.status == "done" and result.summary:
                line += " · %s" % result.summary
            elif result.error:
                line += " · %s" % result.error
            elif result.partial:
                line += " · partial: %s" % result.partial
            lines.append(line)
        return "\n".join(lines)

    def watchdog_check(self) -> list[dict]:
        """Mark silent nodes as stalled (design 2.8: never swallow silently).

        A stalled node gets a NodeResult, is removed from the live node
        registry, and can finish the batch like any other terminal state.
        """
        now = self._clock()
        with self._cond:
            candidates = [node for node in self._nodes.values()
                          if node.status == "running"
                          and now - node.last_output_at > STALL_SECONDS]
        stalled = []
        for node in candidates:
            result = NodeResult(
                node_id=node.node_id, goal=node.goal, status="stalled",
                error="no output for %ds, suspected stall" % STALL_SECONDS,
            )
            self._finish_node(node, result, node.batch_id)
            stalled.append({"node_id": node.node_id, "batch_id": node.batch_id})
        return stalled

    # -- internals ----------------------------------------------------------

    def _event(self, event: str, payload: dict) -> None:
        if self._emit is not None:
            try:
                self._emit(event, payload)
            except Exception:
                pass

    def _sample_peak(self, batch_id: str) -> None:
        """Fold one rss() sample into the batch peak (in-flight watchdog)."""
        sample = self._rss()
        if sample is None:
            return
        with self._cond:
            batch = self._batches.get(batch_id)
            if batch is None:
                return
            if batch.rss_peak is None or sample > batch.rss_peak:
                batch.rss_peak = sample
            if sample > MEMORY_WATCHDOG_RSS_BYTES:
                pass  # event emitted by the child emitter; kept noise-free here

    def _run_one(self, task: dict, node: _Node, batch_id: str,
                 transcript: _Transcript) -> None:
        node_ctx: dict = {}
        try:
            node_ctx = {
                "node_id": node.node_id,
                "batch_id": batch_id,
                "goal": node.goal,
                # Depth=1 fence: the child's tool whitelist never includes
                # delegation / approval tools; the engine already rejected
                # any task that asked for them at dispatch time.
                "forbidden_tools": sorted(FORBIDDEN_CHILD_TOOL_NAMES),
                "emit": self._make_emitter(node, batch_id, transcript),
                "transcript_path": transcript.path,
                "clock": self._clock,
            }
            outcome = self._run_node(task, node_ctx)
            if not isinstance(outcome, NodeResult):
                outcome = NodeResult(
                    node_id=node.node_id, goal=node.goal, status="done",
                    summary=str(outcome))
            result = outcome
            result.node_id = node.node_id
            result.goal = node.goal
            result.transcript_path = transcript.path
        except BaseException as exc:  # noqa: BLE001 - nodes must never crash
            # Half-finished body: the disk transcript is the source of truth
            # for the partial text; the in-memory copy is dropped on close.
            partial_text = transcript.tail_text()
            try:
                transcript.write("interrupted: %s\n" % exc)
            except Exception:
                pass
            result = NodeResult(
                node_id=node.node_id, goal=node.goal, status="interrupted",
                error="%s: %s" % (type(exc).__name__, exc),
                partial=partial_text,
                transcript_path=transcript.path,
            )
            if isinstance(exc, MemoryError):
                result.status = "failed"
        finally:
            try:
                transcript.close()
            except Exception:
                pass

        self._finish_node(node, result, batch_id)
        # Reference discipline: task / node_ctx / transcript go out of scope
        # with this frame; only the NodeResult (summary + usage) survives in
        # the registry. No message history or raw tool output is retained.
        del task, node_ctx

    def _make_emitter(self, node: _Node, batch_id: str,
                      transcript: _Transcript) -> Callable[[str, dict], None]:
        def emit_child(event: str, payload: dict) -> None:
            now = self._clock()
            with self._cond:
                node.last_output_at = now
            merged = dict(payload or {})
            merged.setdefault("node_id", node.node_id)
            merged.setdefault("batch_id", batch_id)
            try:
                transcript.write("%s %s\n" % (event, merged))
            except Exception:
                pass
            if event in ("subagent.delta", "subagent.tool"):
                self._event(event, merged)
            # In-flight watchdog: over the threshold -> error event, no new
            # nodes accepted (the gate refuses them at dispatch).
            sample = self._rss()
            if sample is not None and sample > MEMORY_WATCHDOG_RSS_BYTES:
                self._event("subagent.error", {
                    "node_id": node.node_id,
                    "batch_id": batch_id,
                    "error": ("RSS %.1fMB exceeds the %.0fMB watchdog "
                              "threshold; no new nodes will be accepted"
                              % (sample / _MB, MEMORY_WATCHDOG_RSS_BYTES / _MB)),
                })
        return emit_child

    def _finish_node(self, node: _Node, result: NodeResult,
                     batch_id: str) -> None:
        result.elapsed_s = round(self._clock() - node.started_at, 3)
        event = ("subagent.done" if result.status == "done"
                 else "subagent.error")
        finished_now = False
        batch_obj: Optional[Batch] = None
        with self._cond:
            batch_obj = self._batches.get(batch_id)
            node.status = result.status
            results = self._results.get(batch_id)
            if results is not None:
                results[node.node_id] = result
            # Remove the live registry entry on ANY terminal state
            # (done / failed / interrupted / stalled): active() must drain
            # to empty and node references must be released.
            self._nodes.pop(node.node_id, None)
            _gil_lease_release()
            if batch_obj is not None and results is not None:
                remaining = [nid for nid in self._order.get(batch_id, [])
                             if nid not in results]
                if not remaining:
                    finished_now = batch_id not in self._gc_done
                    if finished_now:
                        self._gc_done.add(batch_id)
                    batch_obj.finished_at = self._clock()
                    # Terminal status is deferred until the boundary
                    # housekeeping below finishes; join() keeps waiting on
                    # _cond while batch_obj.status == "running".
                    self._terminal[batch_id] = ("done" if all(
                        results[nid].status == "done"
                        for nid in self._order.get(batch_id, []))
                        else "partial")
        self._event(event, {
            "node_id": node.node_id, "batch_id": batch_id,
            "status": result.status, "summary": result.summary,
            "error": result.error, "elapsed_s": result.elapsed_s,
        })
        if finished_now and batch_obj is not None:
            # Batch boundary housekeeping: exactly one collect() per batch,
            # never per round, with rss recorded before (dispatch), peak
            # (samples folded in below), and after gc.
            self._sample_peak(batch_id)
            self._collect()
            rss_after = self._rss()
            with self._cond:
                batch_obj.rss_after_gc = rss_after
                batch_obj.status = self._terminal.pop(
                    batch_id, batch_obj.status)
                self._cond.notify_all()
            self._event("subagent.batch_done", {
                "batch_id": batch_id,
                "status": batch_obj.status,
                "node_ids": list(batch_obj.node_ids),
                "rss_before": batch_obj.rss_before,
                "rss_peak": batch_obj.rss_peak,
                "rss_after_gc": batch_obj.rss_after_gc,
            })

    def _finish_node(self, node: _Node, result: NodeResult,
                     batch_id: str) -> None:
        result.elapsed_s = round(self._clock() - node.started_at, 3)
        event = ("subagent.done" if result.status == "done"
                 else "subagent.error")
        self._sample_peak(batch_id)
        finished_now = False
        batch_obj: Optional[Batch] = None
        with self._cond:
            batch_obj = self._batches.get(batch_id)
            node.status = result.status
            results = self._results.get(batch_id)
            if results is not None:
                results[node.node_id] = result
            # Remove the live registry entry on ANY terminal state
            # (done / failed / interrupted / stalled): active() must drain
            # to empty and node references must be released.
            self._nodes.pop(node.node_id, None)
            _gil_lease_release()
            if batch_obj is not None and results is not None:
                remaining = [nid for nid in self._order.get(batch_id, [])
                             if nid not in results]
                if not remaining:
                    finished_now = batch_id not in self._gc_done
                    if finished_now:
                        self._gc_done.add(batch_id)
                    batch_obj.finished_at = self._clock()
                    batch_obj.status = ("done" if all(
                        results[nid].status == "done"
                        for nid in self._order.get(batch_id, []))
                        else "partial")
                    # Batch boundary housekeeping: exactly one collect(),
                    # never per round, with rss recorded before (dispatch),
                    # peak (samples), and after gc. join() must not observe
                    # the terminal status until this whole block is done,
                    # so the flag flip happens at the very end.
                    self._sample_peak(batch_id)
                    gc_placeholder = True
                else:
                    gc_placeholder = False
                self._cond.notify_all()
            else:
                gc_placeholder = False
        self._event(event, {
            "node_id": node.node_id, "batch_id": batch_id,
            "status": result.status, "summary": result.summary,
            "error": result.error, "elapsed_s": result.elapsed_s,
        })
        if finished_now and batch_obj is not None:
            self._collect()
            rss_after = self._rss()
            with self._cond:
                batch_obj.rss_after_gc = rss_after
            self._event("subagent.batch_done", {
                "batch_id": batch_id,
                "status": batch_obj.status,
                "node_ids": list(batch_obj.node_ids),
                "rss_before": batch_obj.rss_before,
                "rss_peak": batch_obj.rss_peak,
                "rss_after_gc": batch_obj.rss_after_gc,
            })
            # The terminal status becomes visible to join() only after the
            # boundary housekeeping (collect + rss_after_gc) is complete.
            # batch_obj.status was already assigned above; join() waits on
            # _cond, which we release here.
            with self._cond:
                self._cond.notify_all()


def run_dummy_node(task: dict, node_ctx: dict) -> NodeResult:
    """Reference run_node for docs / smoke use: echoes the goal back."""
    return NodeResult(
        node_id=node_ctx["node_id"],
        goal=node_ctx["goal"],
        status="done",
        summary="echo: %s" % node_ctx["goal"],
        usage={"input_tokens": 0, "output_tokens": 0},
    )
