# -*- coding: utf-8 -*-
"""logging_kit — CodeAgent 结构化日志 + 轮转 + 崩溃兜底（设计文档 §4）。

职责（按 t_5dd44e42 任务书）：
1. 结构化：每条含 时间(ISO+时区)/级别/来源(python|electron|ui)/run_id/session_id
   (有则带)/消息/可选结构化字段。落盘 JSONL，一行一条，grep/jq 友好。
2. 落盘位置：日志目录可配置覆盖（DESK_AGENT_LOG_DIR 或 logging.logDir），
   默认 <dataDir>/logs/；文件名 agent-YYYYMMDD.log（JSONL）。
3. 轮转：按日切文件；单文件超过 max_file_bytes 时把前半截砍掉（保尾部，
   错误通常在尾部）；启动时清理只保留 retention_days 天（默认 7），
   crash-*.log 同样保留 N 份；写日志自身绝不能抛。
4. 级别：debug/info/warn/error 可配（config.json logging.level 或
   DESK_AGENT_LOG_LEVEL 环境变量），默认 info；运行时可 set_level() 改。
5. 崩溃兜底：crash_dump() 把完整 traceback + 最近 N 条日志行写进
   crash-<ts>.log 纯文本文件；写不进就降级写系统临时目录，再不行只回
   stderr —— 「日志写不进去」本身绝不抛。

本模块零第三方依赖，纯 stdlib；不 import sidecar/appconfig（避免环），配置
由调用方灌进来。设计上是进程内单例 get_logger()，但可 reset_for_test()。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------

LEVELS = ("debug", "info", "warn", "error", "critical")
_LEVEL_ORDER = {name: i for i, name in enumerate(LEVELS)}

DEFAULT_LEVEL = "info"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20MB，对齐设计文档 §4.2
DEFAULT_CRASH_TAIL_LINES = 50
DEFAULT_CRASH_FILES_KEEP = 10
DEFAULT_DATA_PAYLOAD_MAX = 2048  # 结构化字段截断（隐私/成本折中）

_ENV_LOG_DIR = "DESK_AGENT_LOG_DIR"
_ENV_LOG_LEVEL = "DESK_AGENT_LOG_LEVEL"


class LogConfigError(Exception):
    """配置非法（级别名拼错等）。调用方应吞掉并回退默认。"""


def _now_iso() -> str:
    """当前时刻 ISO8601 带本地时区偏移。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _normalize_level(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    lv = raw.strip().lower()
    if lv == "warning":
        lv = "warn"
    return lv if lv in _LEVEL_ORDER else None


# ---------------------------------------------------------------------------
# 内部：内存环形缓冲（崩溃时取「最近 N 条」用）
# ---------------------------------------------------------------------------

class _RingBuffer:
    def __init__(self, capacity: int) -> None:
        self._cap = max(1, capacity)
        self._items: list[dict] = []
        self._lock = threading.Lock()

    def push(self, item: dict) -> None:
        with self._lock:
            if len(self._items) >= self._cap:
                self._items.pop(0)
            self._items.append(item)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._items)


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class AgentLogger:
    """线程安全、永不抛的 JSONL 落盘日志器。"""

    def __init__(self, data_dir: str | None, *, level: str = DEFAULT_LEVEL,
                 log_dir_override: str | None = None,
                 retention_days: int = DEFAULT_RETENTION_DAYS,
                 max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> None:
        self._level = _normalize_level(level) or DEFAULT_LEVEL
        self._log_dir_override = log_dir_override
        self._data_dir = data_dir
        self._retention_days = max(1, int(retention_days))
        self._max_file_bytes = max(64 * 1024, int(max_file_bytes))  # 64KB 下限见 configure 注释
        self._buf = _RingBuffer(2000)
        self._write_lock = threading.Lock()
        self._day_key = ""
        self._path = ""
        # stderr 保持现有格式（Electron 侧调试管道依赖它），双写。
        self._stderr = sys.stderr if hasattr(sys.stderr, "write") else None

    # -- 基本属性 ---------------------------------------------------------

    @property
    def level(self) -> str:
        return self._level

    def set_level(self, level: str) -> bool:
        """运行时改级别；非法级别返回 False 不改。"""
        lv = _normalize_level(level)
        if lv is None:
            return False
        self._level = lv
        return True

    @property
    def log_dir(self) -> str:
        if self._log_dir_override:
            return self._log_dir_override
        base = self._data_dir or tempfile.gettempdir()
        return os.path.join(base, "logs")

    # -- 核心 -------------------------------------------------------------

    def enabled(self, level: str) -> bool:
        lv = _normalize_level(level) or "info"
        return _LEVEL_ORDER[lv] >= _LEVEL_ORDER[self._level]

    def log(self, level: str, msg: str, *, source: str = "python",
            run_id: str | None = None, session_id: str | None = None,
            data: dict | None = None) -> None:
        """记一条。永不抛：落盘失败只降级 stderr。"""
        try:
            self._log_inner(level, msg, source=source, run_id=run_id,
                            session_id=session_id, data=data)
        except Exception:                    # pragma: no cover - 兜底之兜底
            try:
                if self._stderr is not None:
                    self._stderr.write("[logging_kit] internal failure\n")
                    self._stderr.flush()
            except Exception:
                pass

    def _log_inner(self, level: str, msg: str, *, source: str, run_id,
                   session_id, data) -> None:
        lv = _normalize_level(level) or "info"
        rec: dict = {
            "ts": _now_iso(),
            "level": lv,
            "source": str(source or "python"),
            "msg": "" if msg is None else str(msg),
        }
        if run_id:
            rec["run_id"] = str(run_id)
        if session_id:
            rec["session_id"] = str(session_id)
        if data is not None:
            rec["data"] = _truncate_data(data)
        record = rec
        self._buf.push(record)
        # stderr 双写（保持 sidecar 老格式，Electron 调试管道友好）
        if self._stderr is not None:
            self._stderr.write("[%s %s] %s: %s\n" % (
                record["source"], record["ts"][11:19], lv, record["msg"]))
            self._stderr.flush()
        if not self.enabled(lv):
            return
        line = json.dumps(record, ensure_ascii=False, default=str)
        self._write_line(line)

    def _write_line(self, line: str) -> None:
        with self._write_lock:
            path = self._ensure_file()
            if path is None:
                return
            try:
                data = (line + "\n").encode("utf-8")
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                self._maybe_rotate(path, wrote_bytes=len(data))
            except OSError:
                # 落盘失败降级：不再重试（每条都试太吵），stderr 已双写过。

                # 结构化这条还是丢一条进临时目录，留一条指路信息
                try:
                    tmp = os.path.join(tempfile.gettempdir(),
                                       "codeagent-log-fallback.log")
                    with open(tmp, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except Exception:
                    pass

    def _ensure_file(self) -> str | None:
        """返回当前应写入的文件路径（跨日则切新文件）。失败返回 None。"""
        day = time.strftime("%Y%m%d")
        if day != self._day_key:
            self._day_key = day
            self._path = os.path.join(self.log_dir, "agent-%s.log" % day)
            self._make_dir_safe(self.log_dir)
            self._sweep_stale()      # 清理放切日/启动时，不占逐条路径
        return self._path or None

    def _make_dir_safe(self, d: str) -> None:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            self._path = ""

    # -- 轮转与保留 --------------------------------------------------------

    def _maybe_rotate(self, path: str, wrote_bytes: int = 1 << 30) -> None:
        """单文件超阈值：保尾部一半（错误通常在尾部）。

        检查节流按「自上次真实检查以来累计写入的字节数」：攒够
        _ROTATE_CHECK_BYTES（4KB）才 stat 一次大小。只要进程还在写日志，
        检查就一定会周期性发生（修复旧实现「一次性预算耗尽后永不轮转」），
        且超阈值的过冲被节流粒度（~4KB）封顶。wrote_bytes 缺省极大，
        直接触发一次检查（测试/显式调用用）。
        """
        if not path:
            return
        self._rotate_writes += 1
        self._bytes_since_check += wrote_bytes
        if self._bytes_since_check < self._ROTATE_CHECK_BYTES:
            return
        self._bytes_since_check = 0
        try:
            if os.path.getsize(path) <= self._max_file_bytes:
                return
            half = self._max_file_bytes // 2
            with open(path, "rb") as f:
                f.seek(0, os.SEEK_END)
                total = f.tell()
                start = total - half if total > half else 0
                f.seek(start)
                blob = f.read()
            nl = blob.find(b"\n")
            if nl >= 0:
                blob = blob[nl + 1:]
            tmp = path + ".rotate"
            with open(tmp, "wb") as f:
                f.write(blob)
            os.replace(tmp, path)
        except OSError:
            pass

    #: 自上次真实大小检查以来累计写入多少字节后再检查一次（防逐条 stat 开销）
    _ROTATE_CHECK_BYTES = 4096
    _rotate_writes = 0
    _bytes_since_check = 0

    def _sweep_stale(self) -> None:
        """删除超期日志与多余 crash 文件。任何失败都吞（清不掉不影响写）。"""
        try:
            d = self.log_dir
            if not os.path.isdir(d):
                return
            cutoff = time.time() - self._retention_days * 86400
            for p in glob.glob(os.path.join(d, "agent-*.log*")):
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except OSError:
                    pass
            self._sweep_crashes(d)
        except Exception:
            pass

    def _sweep_crashes(self, d: str) -> None:
        try:
            crashes = glob.glob(os.path.join(d, "crash-*.log"))
            if len(crashes) <= DEFAULT_CRASH_FILES_KEEP:
                return
            crashes.sort(key=lambda p: _crash_ts_of(p) or 0)
            for p in crashes[:-DEFAULT_CRASH_FILES_KEEP]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception:
            pass

    # -- 崩溃兜底 ----------------------------------------------------------

    def crash_dump(self, *, title: str, traceback_text: str,
                   source: str = "python",
                   run_id: str | None = None,
                   session_id: str | None = None,
                   extra: dict | None = None) -> str | None:
        """崩溃落盘：crash-<ts>.log（纯文本，完整 traceback + 最近 N 条日志）。

        返回写入路径；全部失败返回 None（不抛）。同时记一条 critical 级
        结构化日志（data 里带 traceback），保证 jsonl 里有 error 记录。
        """
        ts = time.strftime("%Y%m%d-%H%M%S")
        recent = self._buf.snapshot()[-DEFAULT_CRASH_TAIL_LINES:]
        lines = ["# CodeAgent crash %s" % _now_iso(),
                 "# source=%s run_id=%s session_id=%s"
                 % (source, run_id or "-", session_id or "-"),
                 "# title: %s" % (title,),
                 ""]
        if extra:
            try:
                lines.append("extra: %s" % json.dumps(
                    _truncate_data(extra), ensure_ascii=False, default=str))
                lines.append("")
            except Exception:
                pass
        lines.append(traceback_text or "(no traceback)")
        lines.append("")
        lines.append("---- recent log tail (%d entries) ----" % len(recent))
        for r in recent:
            try:
                lines.append(json.dumps(r, ensure_ascii=False, default=str))
            except Exception:
                lines.append(repr(r))
        body = "\n".join(lines) + "\n"
        path = self._write_crash(body, ts)
        # 结构化侧也记一条 critical（级别最高，任何阈值都放行）
        self.log("critical", "crash: %s" % title, source=source,
                 run_id=run_id, session_id=session_id,
                 data={"traceback": (traceback_text or "")[:DEFAULT_DATA_PAYLOAD_MAX],
                       "crash_file": path})
        return path

    def _write_crash(self, body: str, ts: str) -> str | None:
        for base in (self.log_dir, os.path.join(tempfile.gettempdir(),
                                                "codeagent-crash")):
            try:
                os.makedirs(base, exist_ok=True)
                path = os.path.join(base, "crash-%s.log" % ts)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(body)
                if base != self.log_dir and self._stderr is not None:
                    self._stderr.write(
                        "[logging_kit] log dir unwritable, crash written to %s\n"
                        % path)
                    self._stderr.flush()
                return path
            except OSError:
                continue
        if self._stderr is not None:
            try:
                self._stderr.write("[logging_kit] CRASH TRACEBACK LOST, "
                                   "printing to stderr:\n%s\n" % body)
                self._stderr.flush()
            except Exception:
                pass
        return None

    # -- 便捷方法 ----------------------------------------------------------

    def debug(self, msg: str, **kw) -> None:
        self.log("debug", msg, **kw)

    def info(self, msg: str, **kw) -> None:
        self.log("info", msg, **kw)

    def warn(self, msg: str, **kw) -> None:
        self.log("warn", msg, **kw)

    def error(self, msg: str, **kw) -> None:
        self.log("error", msg, **kw)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

_CRASH_TS_RE = re.compile(r"crash-(\d{8}-\d{6})\.log$")


def _crash_ts_of(path: str) -> str | None:
    m = _CRASH_TS_RE.search(os.path.basename(path))
    return m.group(1) if m else None


def _truncate_data(data: object, max_chars: int = DEFAULT_DATA_PAYLOAD_MAX) -> dict:
    """data 序列化后超 2KB 截断成字符串；非 dict 拒收（返回 {}）。"""
    if not isinstance(data, dict):
        return {}
    try:
        s = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:
        return {"_unserializable": True}
    if len(s) <= max_chars:
        return data
    return {"_truncated": True, "head": s[:max_chars]}


# ---------------------------------------------------------------------------
# 进程内单例（sidecar / agent_loop 都从这里拿）
# ---------------------------------------------------------------------------

_SINGLETON: AgentLogger | None = None
_SINGLETON_LOCK = threading.Lock()


def configure(data_dir: str | None, *, level: str | None = None,
              log_dir_override: str | None = None,
              retention_days: int = DEFAULT_RETENTION_DAYS,
              max_file_bytes: int = DEFAULT_MAX_FILE_BYTES) -> AgentLogger:
    """初始化/替换单例。sidecar 启动时调一次；参数来自 config + 环境变量。

    注意：max_file_bytes 有 64KB 硬下限（AgentLogger 内夹紧）。配置更小
    的值（如测试用 2KB）会被静默抬到 65536 —— 这是保尾部轮转语义
    （砍一半后还要有意义）的刻意设计，不是 bug。
    """
    global _SINGLETON
    lv = None
    if level is not None:
        lv = _normalize_level(level)
        if lv is None:
            raise LogConfigError("unknown log level: %r" % (level,))
    logger = AgentLogger(
        data_dir, level=lv or DEFAULT_LEVEL,
        log_dir_override=log_dir_override,
        retention_days=retention_days,
        max_file_bytes=max_file_bytes,
    )
    with _SINGLETON_LOCK:
        _SINGLETON = logger
    return logger


def get_logger() -> AgentLogger:
    """取单例；未 configure 时惰性建一个（环境变量优先，其次临时目录）。"""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = AgentLogger(
                data_dir=None,
                level=_normalize_level(os.environ.get(_ENV_LOG_LEVEL))
                or DEFAULT_LEVEL,
                log_dir_override=(os.environ.get(_ENV_LOG_DIR) or "").strip()
                or None,
            )
        return _SINGLETON


def reset_for_test() -> None:
    """测试隔离用：丢弃单例（下个 get_logger 重建）。"""
    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = None


# ---------------------------------------------------------------------------
# 兼容包装：sidecar.log(level, msg) 旧签名 → 结构化日志
# ---------------------------------------------------------------------------

def compat_log(level: str, msg: str) -> None:
    """sidecar 的 log() 老调用面（位置参数两个）转进 AgentLogger。

    老调用点零改动即可获得落盘；关键字扩展走 get_logger().log(...)。
    """
    get_logger().log(level, msg, source="python-sidecar")


def install_excepthooks(*, source: str = "python-sidecar",
                        extra: dict | None = None) -> None:
    """sys.excepthook + threading.excepthook 双保险：未捕获异常落 crash 文件。"""

    def _hook(t, value, tb) -> None:
        import traceback
        text = "".join(traceback.format_exception(t, value, tb))
        get_logger().crash_dump(
            title="uncaught %s" % t.__name__ if t else "uncaught exception",
            traceback_text=text, source=source, extra=extra)
        # 保留链式行为（stderr 已由 crash_dump 双写，这里不再重复打印原始样式）

    def _thread_hook(args) -> None:
        import traceback
        text = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        get_logger().crash_dump(
            title="uncaught in thread %s: %s"
                  % (args.thread.name if args.thread else "?",
                     args.exc_type.__name__ if args.exc_type else "?"),
            traceback_text=text, source=source, extra=extra)

    sys.excepthook = _hook
    threading.excepthook = _thread_hook     # type: ignore[attr-defined]
