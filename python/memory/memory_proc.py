"""Pure-logic manager for the memory engine subprocess.

Design (approved note in the project's design archive,
"desktop-agent-memory-mechanism-v1", lines 21-39): the memory engine
MUST live in its own subprocess. Loading the ONNX engine in-process
leaves ~97MB held in the ONNX runtime arena forever (note line 27),
while a subprocess measures 219MB rss but returns 100% to the OS when
killed (note line 29). So the manager keeps the child alive while the
app is active, reaps it after IDLE_THRESHOLD_SECONDS of disuse and
respawns on demand, paying a one-off ~0.82s cold start (note line 37).
All side effects (spawn, kill, clock, pipe read/write) are injected by
the caller -- exactly how the tests drive it. Pure stdlib.
"""
from __future__ import annotations

import json
import time

# Idle threshold before reaping the memory subprocess: 10 minutes
# (note line 36, "reap the child after N idle minutes").
IDLE_THRESHOLD_SECONDS = 600
# One retry when spawning the child fails, then mark unavailable; a
# single retry keeps a failed machine from wedging startup.
SPAWN_MAX_ATTEMPTS = 2


class MemoryProcessError(Exception):
    """Base error for memory subprocess failures."""


class MemoryTimeoutError(MemoryProcessError):
    """A JSON-lines request exceeded its timeout."""


class MemoryProcessDeadError(MemoryProcessError):
    """The memory subprocess died or was never alive."""


class MemoryProcessUnavailableError(MemoryProcessError):
    """Spawn failed twice; the manager gave up and is unavailable."""


class MemoryProcess:
    """Lifecycle manager for the memory engine subprocess.

    ``spawn`` must return a child exposing ``alive`` plus the
    ``write_line``/``read_line`` of the JSON-lines protocol; ``clock``
    returns the current time in seconds; ``kill`` disposes of a child
    and releases its resources (the note's whole point: killing frees
    the 219MB back to the OS).
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
                # "never raises" has to hold for a spawn that blows up too,
                # not only for one that hands back a dead child.
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
        """Record one use of the memory engine."""
        self._last_used = self._clock()

    def maybe_reap(self, now=None) -> bool:
        """Kill the child if idle past the threshold; returns True when
        this call killed it. Repeated calls without a respawn in
        between never kill again: there is no child left to kill."""
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
        """Kill the held child, if any, and forget it."""
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

        Raises MemoryTimeoutError on timeout, MemoryProcessDeadError
        if the child is down, MemoryProcessError for a malformed
        response. The child is disposed after a dead/malformed
        response so a later ensure_up() respawns it fresh.
        """
        if not self.is_up:
            raise MemoryProcessDeadError("memory subprocess is not up")
        child = self._child
        try:
            child.write_line(json.dumps(payload))
            raw = child.read_line(timeout)
        except MemoryTimeoutError:
            raise
        except Exception as exc:  # child crashed mid-request
            self._dispose_child()
            raise MemoryProcessDeadError(
                "memory subprocess died during request: %s" % (exc,)
            ) from exc
        if raw is None:
            self._dispose_child()
            if not child.alive:
                raise MemoryProcessDeadError(
                    "memory subprocess died before replying")
            raise MemoryTimeoutError(
                "request timed out after %s seconds" % (timeout,))
        try:
            response = json.loads(raw)
        except ValueError as exc:
            self._dispose_child()
            raise MemoryProcessError(
                "malformed JSON response: %r" % (raw,)) from exc
        if not isinstance(response, dict):
            self._dispose_child()
            raise MemoryProcessError("non-object JSON response: %r" % (raw,))
        self.touch()
        return response
