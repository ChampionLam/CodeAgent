"""Pure-logic manager for the DecisionBert guard-model subprocess.

直接照抄 python/memory/memory_proc.py 的形状与理由：模型引擎必须住
自己的子进程 —— ONNX arena 在加载后不还给 OS（int8 常驻 RSS 约
304MB），子进程在空闲 IDLE_THRESHOLD_SECONDS 后 reap 可把内存全部
还给 OS，只在按需 respawn 时付一次约 0.36s 冷启动。spawn 失败重试
有限次（SPAWN_MAX_ATTEMPTS）后标记不可用，调用方 fail-open 到
纯正则，绝不让模型层把 agent 搞不可用。

All side effects (spawn, kill, clock, pipe read/write) are injected by
the caller -- exactly how the tests drive it. Pure stdlib.
"""
from __future__ import annotations

import json
import time

# Idle threshold before reaping the model subprocess: 10 minutes.
# 与 memory_proc 相同的量级；guard 推理是每命令一条，不常驻更久。
IDLE_THRESHOLD_SECONDS = 600
# One retry when spawning the child fails, then mark unavailable.
SPAWN_MAX_ATTEMPTS = 2


class GuardModelError(Exception):
    """Base error for guard model subprocess failures."""


class GuardModelTimeoutError(GuardModelError):
    """A JSON-lines request exceeded its timeout."""


class GuardModelProcessDeadError(GuardModelError):
    """The model subprocess died or was never alive."""


class GuardModelProcessUnavailableError(GuardModelError):
    """Spawn failed twice; the manager gave up and is unavailable."""


class GuardModelProcess:
    """Lifecycle manager for the guard model subprocess.

    ``spawn`` must return a child exposing ``alive`` plus the
    ``write_line``/``read_line`` of the JSON-lines protocol; ``clock``
    returns the current time in seconds; ``kill`` disposes of a child.
    """

    def __init__(self, spawn, clock=time.monotonic, kill=None,
                 idle_threshold=IDLE_THRESHOLD_SECONDS):
        self._spawn = spawn
        self._clock = clock
        self._kill = kill
        self._idle_threshold = idle_threshold
        self._child = None
        self._last_used = None
        self._spawn_attempts = 0
        self._unavailable = False

    # -- state ---------------------------------------------------------
    @property
    def is_up(self) -> bool:
        """True if a live child is currently held."""
        return self._child is not None and bool(self._child.alive)

    @property
    def is_unavailable(self) -> bool:
        """True after SPAWN_MAX_ATTEMPTS consecutive spawn failures."""
        return self._unavailable

    @property
    def spawn_attempt_count(self) -> int:
        """Total spawn attempts since construction (test hook)."""
        return self._spawn_attempts

    # -- lifecycle -----------------------------------------------------
    def ensure_up(self) -> bool:
        """Bring the child up if needed; never raises.

        Returns True only when a NEW child was spawned during this
        call. Spawns at most SPAWN_MAX_ATTEMPTS times per call; once
        both fail the manager is marked unavailable and later calls
        make no further attempts.
        """
        if self.is_up:
            return False
        if self._unavailable:
            return False
        spawned = False
        for _ in range(SPAWN_MAX_ATTEMPTS):
            self._spawn_attempts += 1
            try:
                child = self._spawn()
            except Exception:
                # "never raises" has to hold for a spawn that blows up
                # too, not only for one that hands back a dead child.
                child = None
            if child is not None and getattr(child, "alive", False):
                self._child = child
                self.touch()
                spawned = True
                break
        if not spawned:
            self._unavailable = True
        return spawned

    def touch(self) -> None:
        """Record one use of the model engine."""
        self._last_used = self._clock()

    def maybe_reap(self, now=None) -> bool:
        """Kill the child if idle past the threshold; returns True when
        this call killed it."""
        if self._child is None:
            return False
        if now is None:
            now = self._clock()
        last = self._last_used
        if last is not None and (now - last) < self._idle_threshold:
            return False
        self._dispose_child()
        return True

    def shutdown(self) -> None:
        """Kill the child immediately (e.g. app exit) and free memory."""
        self._dispose_child()

    def _dispose_child(self) -> None:
        child = self._child
        self._child = None
        self._last_used = None
        if child is not None:
            if self._kill is not None:
                self._kill(child)
            elif hasattr(child, "kill"):
                child.kill()

    # -- JSON-lines protocol -------------------------------------------
    def request(self, payload: dict, timeout: float) -> dict:
        """Send one request over JSON-lines and await the response.

        Raises GuardModelTimeoutError on timeout, GuardModelProcessDeadError
        if the child is down, GuardModelError for a malformed response.
        The child is disposed after a dead/malformed response so a later
        ensure_up() respawns it fresh.
        """
        if not self.is_up:
            raise GuardModelProcessDeadError("guard model subprocess is not up")
        child = self._child
        try:
            child.write_line(json.dumps(payload))
            raw = child.read_line(timeout)
        except GuardModelTimeoutError:
            raise
        except Exception as exc:  # child crashed mid-request
            self._dispose_child()
            raise GuardModelProcessDeadError(
                "guard model subprocess died during request: %s" % (exc,)
            ) from exc
        if raw is None:
            # 丢弃 child 前先读生死：活着不回话是超时，死了才是 dead。
            # 必须在 _dispose_child() 之前取 —— dispose 会 kill，kill 之后再
            # 读 alive 就永远是 False 了。
            was_alive = bool(child.alive)
            self._dispose_child()
            if not was_alive:
                raise GuardModelProcessDeadError(
                    "guard model subprocess died before replying")
            raise GuardModelTimeoutError(
                "request timed out after %s seconds" % (timeout,))
        try:
            response = json.loads(raw)
        except ValueError as exc:
            self._dispose_child()
            raise GuardModelError(
                "malformed JSON response: %r" % (raw,)) from exc
        if not isinstance(response, dict):
            self._dispose_child()
            raise GuardModelError("non-object JSON response: %r" % (raw,))
        self.touch()
        return response


__all__ = [
    "GuardModelProcess", "GuardModelError", "GuardModelTimeoutError",
    "GuardModelProcessDeadError", "GuardModelProcessUnavailableError",
    "IDLE_THRESHOLD_SECONDS", "SPAWN_MAX_ATTEMPTS",
]
