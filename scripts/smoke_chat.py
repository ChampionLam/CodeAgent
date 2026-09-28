#!/usr/bin/env python3
"""Headless E2E smoke test: real chat over the real MiniMax CN endpoint.

Spawns python/sidecar.py as a subprocess (never imports its internals) and
exercises the full chat pipeline:
  * Case 1 (happy path): one short chat -> res(accepted) -> chat.reasoning /
    chat.delta events -> chat.done with usage and textChars > 0.
  * Case 2 (cancel): start a long chat, immediately chat.cancel it -> the
    cancel response says cancelled:true, then chat.done with
    finishReason == "cancelled".

API key handling: read MINIMAX_CN_API_KEY from the environment, falling back
to the local `.env` in the repo root. The key is passed to the child via its
environment and is NEVER printed or written to any file.

stdout purity: every byte the sidecar writes to stdout must be parseable as
frames. This script's own logging goes to stderr only.
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIDECAR = os.path.join(HERE, "python", "sidecar.py")

# Network calls can take tens of seconds; overall budget ~120s.
FRAME_TIMEOUT = 90.0
PROC_EXIT_TIMEOUT = 15.0


def note(msg: str) -> None:
    """Progress logs go to stderr only (stdout purity is under test)."""
    print(f"[smoke_chat] {msg}", file=sys.stderr, flush=True)


def encode(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def read_frame(proc: subprocess.Popen, buf: bytearray, deadline: float) -> dict:
    """Parse one LSP-style frame from the child's stdout via a shared buffer.

    Blocks until a complete frame arrives, the deadline passes, or the child
    dies. Raises on EOF, malformed frames, or timeout. Only complete
    well-formed frames are ever accepted, so successful parsing of every byte
    is the stdout-purity assertion.
    """
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        if not buf:
            _fill_buffer(proc, buf, deadline)
        header.append(buf.pop(0))
        if len(header) > 1024:
            raise RuntimeError(f"header too large: {bytes(header[:80])!r}")
    text = header.decode("ascii")
    n = None
    for line in text.split("\r\n"):
        if line.lower().startswith("content-length:"):
            n = int(line.split(":", 1)[1].strip())
    if n is None:
        raise RuntimeError(f"no Content-Length in header: {text!r}")
    while len(buf) < n:
        _fill_buffer(proc, buf, deadline)
    body = bytes(buf[:n])
    del buf[:n]
    return json.loads(body.decode("utf-8"))


def _fill_buffer(proc: subprocess.Popen, buf: bytearray, deadline: float) -> None:
    """Pull more stdout bytes into buf; wait (bounded) until some arrive."""
    while not buf:
        if proc.poll() is not None:
            raise RuntimeError(
                f"sidecar exited (code {proc.returncode}) with {len(buf)} bytes buffered"
            )
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError(f"timeout waiting for stdout bytes ({deadline - time.time():.1f}s over)")
        r, _, _ = select.select([proc.stdout], [], [], min(remaining, 1.0))
        if not r:
            continue
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            if proc.poll() is not None:
                raise RuntimeError("EOF from sidecar stdout (process exited)")
            raise RuntimeError("EOF from sidecar stdout while still running")
        buf.extend(chunk)


def resolve_key() -> str:
    """Env var first, then the repo-root .env. Never logs the value."""
    key = os.environ.get("MINIMAX_CN_API_KEY", "").strip()
    if key:
        return key
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("MINIMAX_CN_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def dump_debug(proc: subprocess.Popen, frames: list[dict], stderr_tail: list[str]) -> None:
    """On assertion failure: print every received frame + collected stderr, no key."""
    print("[smoke_chat] FAIL: frames received:", file=sys.stderr, flush=True)
    for f in frames:
        print("  " + json.dumps(f, ensure_ascii=False), file=sys.stderr, flush=True)
    print("[smoke_chat] FAIL: sidecar stderr:", file=sys.stderr, flush=True)
    print("".join(stderr_tail), file=sys.stderr, end="", flush=True)


def start_drain_thread(proc: subprocess.Popen) -> list[str]:
    """Continuously drain the child's stderr into a list of lines."""
    lines: list[str] = []
    def _drain() -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                lines.append(raw.decode("utf-8", errors="replace"))
        except Exception:
            pass
    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    return lines


def collect_until_chat_done(proc, buf, chat_id, deadline, keep=None):
    """Read frames until the chat.done event for chat_id arrives (raises on chat.error)."""
    seen: list[dict] = []
    while True:
        frame = read_frame(proc, buf, deadline)
        seen.append(frame)
        if keep is not None:
            keep.append(frame)
        if frame.get("type") == "evt" and frame.get("event") == "chat.error":
            data = frame.get("data") or {}
            if data.get("id") == chat_id:
                raise RuntimeError(f"chat.error event: {json.dumps(data, ensure_ascii=False)}")
        if (
            frame.get("type") == "evt"
            and frame.get("event") == "chat.done"
            and (frame.get("data") or {}).get("id") == chat_id
        ):
            return frame.get("data") or {}, seen


def run_cases(proc: subprocess.Popen, buf: bytearray, all_frames: list[dict]) -> int:
    # --- Case 1: happy path -------------------------------------------------
    chat1_id = "chat-happy-1"
    req1 = {
        "type": "req",
        "id": chat1_id,
        "method": "chat",
        "params": {
            "protocolVersion": 1,
            "messages": [{"role": "user", "content": "只回四个字：晴朗温暖"}],
        },
    }
    proc.stdin.write(encode(req1))
    proc.stdin.flush()

    deadline = time.time() + FRAME_TIMEOUT
    hello = read_frame(proc, buf, deadline)
    all_frames.append(hello)
    assert hello.get("type") == "evt" and hello.get("event") == "hello", f"bad hello: {hello}"
    note("hello frame ok")

    res1 = read_frame(proc, buf, deadline)
    all_frames.append(res1)
    assert res1.get("type") == "res" and res1.get("id") == chat1_id and res1.get("ok") is True, (
        f"expected res ok for {chat1_id}: {res1}"
    )
    assert res1.get("result", {}).get("accepted") is True, f"expected accepted: {res1}"
    note("res(accepted) ok")

    n_deltas = 0
    case1_frames: list[dict] = []
    done1, _ = collect_until_chat_done(proc, buf, chat1_id, deadline, keep=case1_frames)
    all_frames.extend(case1_frames)
    for f in case1_frames:
        if f.get("type") == "evt" and f.get("event") == "chat.delta":
            n_deltas += 1
    note(f"case1 chat.done: {json.dumps(done1, ensure_ascii=False)}")
    assert n_deltas >= 1, f"expected >=1 chat.delta event, got {n_deltas}"
    assert isinstance(done1.get("textChars"), int) and done1.get("textChars", 0) > 0, (
        f"chat.done textChars must be > 0: {done1}"
    )
    assert done1.get("usage") is not None, f"chat.done usage must not be null: {done1}"
    print(f"[smoke_chat] chat.done: usage={json.dumps(done1.get('usage'), ensure_ascii=False)} "
          f"textChars={done1.get('textChars')} finishReason={done1.get('finishReason')}",
          file=sys.stderr, flush=True)

    # --- Case 2: cancel -----------------------------------------------------
    chat2_id = "chat-cancel-2"
    req2 = {
        "type": "req",
        "id": chat2_id,
        "method": "chat",
        "params": {
            "protocolVersion": 1,
            "messages": [{"role": "user", "content": "写一篇 500 字短文，主题：秋天的清晨"}],
        },
    }
    proc.stdin.write(encode(req2))
    proc.stdin.flush()
    # Immediately cancel.
    cancel_id = "cancel-2"
    req_cancel = {
        "type": "req",
        "id": cancel_id,
        "method": "chat.cancel",
        "params": {"protocolVersion": 1, "id": chat2_id},
    }
    proc.stdin.write(encode(req_cancel))
    proc.stdin.flush()

    deadline2 = time.time() + FRAME_TIMEOUT
    case2_frames: list[dict] = []
    res2 = res_cancel = None
    done2 = None
    while True:
        frame = read_frame(proc, buf, deadline2)
        case2_frames.append(frame)
        if frame.get("type") == "res" and frame.get("id") == chat2_id:
            res2 = frame
        elif frame.get("type") == "res" and frame.get("id") == cancel_id:
            res_cancel = frame
        elif (
            frame.get("type") == "evt"
            and frame.get("event") == "chat.done"
            and (frame.get("data") or {}).get("id") == chat2_id
        ):
            done2 = frame.get("data") or {}
            break
    all_frames.extend(case2_frames)
    assert res2 is not None and res2.get("ok") is True and res2.get("result", {}).get("accepted") is True, (
        f"expected res(accepted) for {chat2_id}: {res2}"
    )
    assert res_cancel is not None and res_cancel.get("result", {}).get("cancelled") is True, (
        f"expected cancelled:true in cancel response: {res_cancel}"
    )
    assert done2 is not None, "never received chat.done for the cancelled chat"
    assert done2.get("finishReason") == "cancelled", (
        f"chat.done finishReason must be 'cancelled': {done2}"
    )
    note(f"case2 chat.done: {json.dumps(done2, ensure_ascii=False)}")
    print(f"[smoke_chat] cancelled chat.done: finishReason={done2.get('finishReason')} "
          f"textChars={done2.get('textChars')}",
          file=sys.stderr, flush=True)

    note(f"total frames read: {len(all_frames)}")
    return 0


def main() -> int:
    key = resolve_key()
    if not key:
        print("[smoke_chat] FAIL: no MINIMAX_CN_API_KEY in env or the repo-root .env",
              file=sys.stderr, flush=True)
        return 1
    note("API key resolved (value not shown)")

    env = dict(os.environ)
    env["MINIMAX_CN_API_KEY"] = key
    note(f"spawning: {sys.executable} {SIDECAR}")
    proc = subprocess.Popen(
        [sys.executable, SIDECAR],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stderr_tail = start_drain_thread(proc)
    frames: list[dict] = []
    buf = bytearray()
    rc = 1
    try:
        rc = run_cases(proc, buf, frames)
    except Exception as e:
        print(f"[smoke_chat] FAIL: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        dump_debug(proc, frames, stderr_tail)
        rc = 1
    finally:
        # Close stdin, wait for a clean exit code 0.
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=PROC_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            note("sidecar did not exit in time; killing")
            proc.kill()
            proc.wait(timeout=5)
        note(f"sidecar exit code: {proc.returncode}")
        if proc.returncode != 0:
            print(f"[smoke_chat] FAIL: expected exit code 0, got {proc.returncode}",
                  file=sys.stderr, flush=True)
            dump_debug(proc, frames, stderr_tail)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
