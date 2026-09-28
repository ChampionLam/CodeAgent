"""Mnemosyne engine client: MCP over stdio, running as its own process.

Design source: the project's internal design doc (not part of this repo). The
engine runs in a separate process because its ONNX arena does not return
memory to the OS once loaded (~97MB pinned in-process); as a child process it
costs ~220MB while warm and returns all of it when reaped, at a 0.82s cold
start payed only on wake.

We speak the small MCP subset we need by hand (handshake + tools/call) so the
app stays stdlib-only and does not pin an SDK version. Frames are
newline-delimited JSON-RPC 2.0, which is what the MCP stdio transport uses:

    -> {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
    <- {"jsonrpc":"2.0","id":1,"result":{...}}
    -> {"jsonrpc":"2.0","method":"notifications/initialized"}
    -> {"jsonrpc":"2.0","id":2,"method":"tools/call",
        "params":{"name":"mnemosyne_recall","arguments":{...}}}
    <- {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"<json>"}]}}

Reading uses a reader thread + queue rather than select(): select() on pipe
handles does not work on Windows, and this has to run there.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

from memory import memory_config
from memory.memory_proc import (
    IDLE_THRESHOLD_SECONDS,
    MemoryProcess,
    MemoryProcessError,
    MemoryTimeoutError,
)

MCP_MODULE = "mnemosyne.mcp_server"
CLIENT_NAME = "desk-agent"
CLIENT_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"
HANDSHAKE_TIMEOUT = 60.0
# A tool call embeds text. The very first one also downloads (then loads) the
# embedding model, which is why this is minutes rather than seconds: measured
# on the verify machine, a cold model fetch blew straight through 30s.
CALL_TIMEOUT = 180.0
# Consolidation is a whole pass over the bank, not a single tool call.
CONSOLIDATE_TIMEOUT = 300.0
CONSOLIDATE_SUBCOMMAND = "sleep"


class ChildProcess:
    """A spawned engine process speaking newline-delimited JSON on stdio."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True,
                                        name="memory-reader")
        self._reader.start()

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def _pump(self) -> None:
        try:
            for line in self._proc.stdout:          # type: ignore[union-attr]
                self._lines.put(line)
        except Exception:                            # pragma: no cover - pipe closed
            pass
        finally:
            self._lines.put("")                      # sentinel: stdout ended

    def write_line(self, text: str) -> None:
        self._proc.stdin.write(text + "\n")          # type: ignore[union-attr]
        self._proc.stdin.flush()                     # type: ignore[union-attr]

    def read_line(self, timeout: float) -> str | None:
        try:
            line = self._lines.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None
        if line == "":                               # stdout closed
            return None
        return line.strip() or None

    def kill(self) -> None:
        try:
            self._proc.kill()
        except Exception:                            # pragma: no cover - already gone
            pass


class MemoryEngine:
    """Owns the engine process, the MCP handshake and the tool calls we use.

    Nothing here raises into the chat path: callers get (ok, detail) or an
    empty result when the engine is unavailable, because memory must never
    take a conversation down.
    """

    def __init__(self, *, python_exe: str | None = None, home: str | None = None,
                 base_env: dict | None = None,
                 idle_threshold: float = IDLE_THRESHOLD_SECONDS,
                 clock=time.monotonic, kill=None) -> None:
        self._python = python_exe or sys.executable
        self._home = home
        # Set by any write: an engine that only read does not need the
        # consolidation pass on the way out.
        self._dirty = False
        self._base_env = dict(base_env if base_env is not None else os.environ)
        self._next_id = 0
        self._lock = threading.Lock()
        # Why the last spawn attempt gave up (safety refusal, spawn error). The
        # supervisor swallows spawn failures by design, so the reason has to be
        # kept here or the operator only sees a generic "unavailable".
        self._refusal = ""
        self._proc = MemoryProcess(self._spawn, clock=clock, kill=kill,
                                   idle_threshold=idle_threshold)

    @property
    def child_pid(self) -> int | None:
        """PID of the engine subprocess, or None when it is not up.

        Real-machine smoke tests need this: the only reliable way to prove the
        OS reclaimed the memory is to watch this PID disappear. (wmic looks
        tempting for "find my children" and lies about it -- it kept reporting
        a killed process as alive -- so the PID is the thing to pin.)
        """
        child = self._proc._child            # noqa: SLF001 - deliberate peek
        popen = getattr(child, "_proc", None)
        return getattr(popen, "pid", None)

    # ------------------------------------------------------------- lifecycle

    def _spawn(self) -> ChildProcess:
        env = memory_config.build_env(self._base_env, home=self._home)
        problems = memory_config.assert_safe(env)
        if problems:
            self._refusal = "engine refused to start: %s" % "; ".join(problems)
            raise MemoryProcessError(self._refusal)
        if self._home:
            env.setdefault("MNEMOSYNE_HOME", self._home)
            os.makedirs(self._home, exist_ok=True)
        try:
            proc = subprocess.Popen(
                [self._python, "-m", MCP_MODULE, "--transport", "stdio"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except OSError as exc:
            # Normalise to the supervisor's own error type so it can apply its
            # retry-then-unavailable policy instead of letting OSError escape.
            self._refusal = "could not spawn %s: %s" % (MCP_MODULE, exc)
            raise MemoryProcessError(self._refusal) from exc
        return ChildProcess(proc)

    @property
    def is_up(self) -> bool:
        return self._proc.is_up

    @property
    def is_unavailable(self) -> bool:
        return self._proc.is_unavailable

    def start(self) -> tuple[bool, str]:
        """Ensure the process is up and handshaken. (ok, detail)."""
        with self._lock:
            try:
                fresh = self._proc.ensure_up()
            except Exception as exc:                # pragma: no cover - defensive
                # Report it as unavailable: callers treat "engine unavailable"
                # as "this turn simply has no memory", never as an error.
                return False, "engine unavailable: spawn failed: %s" % (exc,)
            if not self._proc.is_up:
                return False, self._refusal or "engine unavailable"
            if fresh:
                try:
                    self._handshake()
                except Exception as exc:
                    self._proc.shutdown()
                    return False, "handshake failed: %s" % (exc,)
            return True, "ready"

    def maybe_reap(self) -> bool:
        """Drop the process when idle, returning its memory to the OS."""
        with self._lock:
            try:
                return self._proc.maybe_reap()
            except Exception:                        # pragma: no cover - defensive
                return False

    def consolidate(self) -> dict:
        """Run the engine's consolidation pass (`mnemosyne sleep`, a CLI verb).

        Episodic memories carry no vectors until this runs, so long-term recall
        is keyword-only without it -- and keyword recall still returns plausible
        hits, which is exactly how that gap stays hidden. It is a CLI
        subcommand rather than an MCP tool, so it runs as a one-shot child with
        the same environment as the long-lived one.
        """
        exe = os.path.join(os.path.dirname(self._python),
                           "mnemosyne.exe" if os.name == "nt" else "mnemosyne")
        if not os.path.exists(exe):
            return {"status": "error", "detail": "no mnemosyne CLI next to %s" % self._python}
        env = memory_config.build_env(self._base_env, home=self._home)
        try:
            proc = subprocess.run([exe, CONSOLIDATE_SUBCOMMAND], env=env,
                                  capture_output=True, text=True,
                                  timeout=CONSOLIDATE_TIMEOUT)
        except Exception as exc:
            return {"status": "error", "detail": str(exc)}
        return {"status": "ok" if proc.returncode == 0 else "error",
                "returncode": proc.returncode,
                "output": (proc.stdout or proc.stderr or "")[-400:]}

    def close(self) -> None:
        """Consolidate once if this session wrote something, then kill the child.

        Shutdown is the natural hook: it happens while the app is closing, it
        costs one pass per session, and it means the next session's long-term
        recall has vectors to search. Nothing here may raise -- a memory
        engine that fails on the way out must not take the app with it.
        """
        if self._dirty:
            try:
                self.consolidate()
            except Exception:                      # pragma: no cover - defensive
                pass
        with self._lock:
            self._proc.shutdown()

    # ---------------------------------------------------------------- protocol

    def _handshake(self) -> None:
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        }, timeout=HANDSHAKE_TIMEOUT)
        self._notify("notifications/initialized")

    def _rpc(self, method: str, params: dict | None, *, timeout: float) -> dict:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        reply = self._proc.request(payload, timeout)
        if "error" in reply:
            raise MemoryProcessError("engine error: %s" % (reply["error"],))
        result = reply.get("result")
        if not isinstance(result, dict):
            raise MemoryProcessError("malformed RPC result: %r" % (reply,))
        return result

    def _notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        # A notification has no reply; the next request picks up any error.
        self._proc._child.write_line(json.dumps(payload))   # noqa: SLF001

    def call_tool(self, name: str, arguments: dict | None = None,
                  *, timeout: float = CALL_TIMEOUT) -> dict:
        """Invoke one engine tool and return its decoded payload."""
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}},
                           timeout=timeout)
        content = result.get("content") or []
        if not content:
            return {}
        text = str(content[0].get("text") or "")
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}

    # ------------------------------------------------------------- operations

    def remember(self, content: str, *, source: str = "user", scope: str = "global",
                 importance: float | None = None) -> dict:
        args: dict = {"content": content, "source": source, "scope": scope}
        if importance is not None:
            args["importance"] = importance
        self._dirty = True
        return self.call_tool("mnemosyne_remember", args)

    def recall(self, query: str, *, limit: int = 5) -> list[dict]:
        payload = self.call_tool("mnemosyne_recall", {"query": query, "limit": limit})
        for key in ("results", "memories", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def canonical_set(self, category: str, name: str, body: str,
                      *, source: str = "") -> dict:
        """Write one canonical slot (identity cards, stable preferences)."""
        self._dirty = True
        return self.call_tool("mnemosyne_remember_canonical",
                              {"category": category, "name": name, "body": body,
                               "source": source})

    def canonical_all(self) -> list[dict]:
        """Every canonical slot for this profile (no query: list-everything mode).

        This is the non-query-driven path the design relies on for identity:
        these values are injected on every turn rather than recalled by
        relevance.
        """
        payload = self.call_tool("mnemosyne_recall_canonical", {})
        for key in ("slots", "results", "canonical", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def canonical_forget(self, category: str, name: str) -> dict:
        return self.call_tool("mnemosyne_forget_canonical",
                              {"category": category, "name": name})


__all__ = ["MemoryEngine", "ChildProcess", "MemoryProcessError", "MemoryTimeoutError"]