"""Turn-boundary hooks for the memory engine (write side + read side).

Design source: concepts/desktop-agent-memory-mechanism-v1.md (41-53):
  * write side  -- after a turn ends, enqueue it; a resident writer drains the
                   queue in the background, so a reply is never held up and a
                   turn itself costs zero extra LLM calls.
  * read side   -- recall starts in the background once the previous turn
                   ended; if it is not ready when the next turn assembles its
                   prompt, that turn simply has no memory block. Never wait.

Two consequences worth stating: memory text changes every turn, so it is
rendered at the very end of the prompt (ahead of nothing but the volatile
runtime block); and a missing/broken engine degrades to "no memory", never to
an error in the chat path.
"""
from __future__ import annotations

import os
import queue
import threading
import time

from memory import memory_config

# Canonical identity cards render in the identity section at the TOP of the
# prompt (deterministic presence, every turn); everything else renders at the
# bottom as ordinary recalled memory. Hard rules are not canonical facts: they
# live in the identity file, which is rendered every turn anyway.
IDENTITY_CATEGORIES = ("identity", "voice")
MEMORY_TITLE = "## 相关记忆（Mnemosyne）"
MEMORY_NOTE = "以下是与本轮可能相关的记忆，供参考；与当前对话冲突时以当前对话为准。"
WRITE_IDLE_TIMEOUT = 0.05        # how long a drain waits between queue checks


def _default_warn(level: str, message: str) -> None:  # pragma: no cover - injected
    pass


class MemoryHooks:
    """Owns the writer thread and produces the injection text for a turn."""

    def __init__(self, engine, *, enabled: bool = True, warn=None) -> None:
        self._engine = engine
        self._enabled = bool(enabled)
        self._warn = warn or _default_warn
        self._writes: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._writer: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._snapshot: str | None = None      # last ready injection text
        self._canonical: list[dict] = []       # raw slots behind that snapshot
        self._prefetching = False

    # ------------------------------------------------------------- write side

    def note_turn(self, user_text: str, assistant_text: str = "") -> None:
        """Queue a finished turn for background persistence. Never blocks."""
        if not self._enabled:
            return
        text = _summarise_turn(user_text, assistant_text)
        if not text:
            return
        self._writes.put(("turn", text))
        self._ensure_writer()

    def _ensure_writer(self) -> None:
        with self._lock:
            if self._writer is not None and self._writer.is_alive():
                return
            self._writer = threading.Thread(target=self._drain, daemon=True,
                                            name="memory-writer")
            self._writer.start()

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                kind, text = self._writes.get(timeout=WRITE_IDLE_TIMEOUT)
            except queue.Empty:
                if self._writes.empty():
                    return                     # idle: let the thread exit
                continue
            try:
                if kind == "turn":
                    self._engine.remember(text, source="turn", scope="global")
            except Exception as exc:            # engine down: drop, never raise
                self._warn("warn", "memory write skipped: %s" % (exc,))

    def drain(self, timeout: float = 5.0) -> None:
        """Test helper: wait until queued writes are flushed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._writes.empty():
                writer = self._writer
                if writer is None or not writer.is_alive():
                    return
            time.sleep(0.01)

    # -------------------------------------------------------------- read side

    def start_prefetch(self, query: str) -> None:
        """Begin background recall for the NEXT turn. Returns immediately."""
        if not self._enabled or not (query or "").strip():
            return
        with self._lock:
            if self._prefetching:
                return                          # previous one still running: skip
            self._prefetching = True
        threading.Thread(target=self._prefetch, args=(query,), daemon=True,
                         name="memory-prefetch").start()

    def _prefetch(self, query: str) -> None:
        try:
            canonical = self._engine.canonical_all()
            hits = self._engine.recall(query, limit=injection_top_k())
            text = render_injection(canonical, hits)
            with self._lock:
                self._canonical = list(canonical or [])
                self._snapshot = text
        except Exception as exc:
            self._warn("warn", "memory prefetch skipped: %s" % (exc,))
        finally:
            with self._lock:
                self._prefetching = False

    def injection(self) -> str | None:
        """Text ready for this turn, or None. Reads the last snapshot only."""
        if not self._enabled:
            return None
        with self._lock:
            return self._snapshot

    def canonical_slots(self) -> list[dict]:
        """The canonical slots behind the current snapshot (never blocks)."""
        if not self._enabled:
            return []
        with self._lock:
            return list(self._canonical)

    def rules(self) -> list[str]:
        """Hard rules stored as canonical facts (they render at the top)."""
        return slots_in(self.canonical_slots(), RULE_CATEGORIES)

    def identity_text(self) -> str | None:
        """Canonical identity cards, as one block for the identity section."""
        bodies = slots_in(self.canonical_slots(), IDENTITY_CATEGORIES)
        return "\n".join(bodies) if bodies else None

    def memory_block(self) -> str | None:
        """Snapshot text for the memory section: slot bodies + recalled hits."""
        return memory_block_of(self.canonical_slots(), self.injection())

    # --------------------------------------------------------------- upkeep

    def maybe_reap(self) -> bool:
        """Return the engine's memory to the OS when it has gone idle."""
        try:
            return self._engine.maybe_reap()
        except Exception:                       # pragma: no cover - defensive
            return False

    def close(self) -> None:
        self._stop.set()
        try:
            self._engine.close()
        except Exception:                       # pragma: no cover - defensive
            pass


# ------------------------------------------------------------------ rendering


def _summarise_turn(user_text: str, assistant_text: str) -> str:
    """One compact memory line per turn: what the user asked, what happened."""
    user = " ".join((user_text or "").split())
    reply = " ".join((assistant_text or "").split())
    if not user:
        return ""
    if len(reply) > 400:
        reply = reply[:400] + "…"
    return ("用户：%s\n助手：%s" % (user, reply)).strip() if reply else "用户：%s" % user


def slots_in(canonical: list[dict], categories) -> list[str]:
    """Bodies of the canonical slots whose category is in ``categories``."""
    wanted = {str(c).lower() for c in categories}
    out: list[str] = []
    for slot in canonical or []:
        if str(slot.get("category") or "").lower() not in wanted:
            continue
        body = str(slot.get("body") or "").strip()
        if body and body not in out:
            out.append(body)
    return out


def memory_block_of(canonical: list[dict], snapshot: str | None) -> str | None:
    """Memory section text: the snapshot minus the slots rendered at the top."""
    if not snapshot:
        return None
    at_top = set(slots_in(canonical, IDENTITY_CATEGORIES))
    rows = [line for line in snapshot.splitlines()
            if not any(body in line for body in at_top)]
    # Nothing left after the top sections took their share: drop the header too.
    if not any(line.strip().startswith("-") or (line and not line.startswith("#"))
               for line in rows[2:]):
        return None
    return "\n".join(rows)


def _env_int(key: str, default: str) -> int:
    """Read an operator override, falling back to the note's default.

    Two of the injection knobs are ours, not the engine's: the engine does not
    read MNEMOSYNE_PREFETCH_TOP_K at all (checked against the installed
    package), and the prompt budget is ours by definition. They still have to
    do something, or they are just decorative names in a config file.
    """
    raw = str(os.environ.get(key, "") or "").strip()
    try:
        return int(raw) if raw else int(default)
    except (TypeError, ValueError):
        return int(default)


def injection_top_k() -> int:
    """How many recalled memories to ask for."""
    return _env_int(memory_config.INJECTION_TOP_K_KEY,
                    memory_config.INJECTION_TOP_K_DEFAULT)


def injection_budget() -> int:
    """Total characters the injected block may take."""
    return _env_int(memory_config.INJECTION_TOTAL_CHARS_KEY,
                    memory_config.INJECTION_TOTAL_CHARS_DEFAULT)


def render_injection(canonical: list[dict], hits: list[dict]) -> str | None:
    """Canonical facts first (stable identity), then recalled memories."""
    rows: list[str] = []
    for slot in canonical or []:
        body = str(slot.get("body") or "").strip()
        if not body:
            continue
        name = str(slot.get("name") or slot.get("category") or "").strip()
        rows.append("%s：%s" % (name, body) if name else body)
    for hit in hits or []:
        content = str(hit.get("content") or hit.get("text") or "").strip()
        if content:
            rows.append(content)
    if not rows:
        return None
    # The title and the note are part of the block the model sees, so they
    # count against the budget rather than sitting on top of it.
    header = "\n".join([MEMORY_TITLE, MEMORY_NOTE]) + "\n"
    joined, _truncated = memory_config.injection_budget(
        rows, max(0, injection_budget() - len(header)))
    if not joined.strip():
        return None
    return header + joined