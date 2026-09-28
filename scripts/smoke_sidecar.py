#!/usr/bin/env python3
"""Headless smoke test: spawn the real sidecar, send a ping, verify pong.

This is the canonical proof required by the M1-min task: the Python sidecar
must really run on this machine and respond correctly to the LSP-style frame
protocol.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIDECAR = os.path.join(HERE, "python", "sidecar.py")


def encode(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def read_frame(stream) -> dict:
    """Minimal LSP frame reader for the smoke test."""
    header = bytearray()
    while True:
        ch = stream.read(1)
        if not ch:
            raise RuntimeError("EOF in header")
        header.extend(ch)
        if header.endswith(b"\r\n\r\n"):
            break
        if len(header) > 1024:
            raise RuntimeError("header too large")
    text = header.decode("ascii")
    n = None
    for line in text.split("\r\n"):
        if line.lower().startswith("content-length:"):
            n = int(line.split(":", 1)[1].strip())
    if n is None:
        raise RuntimeError("no Content-Length")
    body = b""
    while len(body) < n:
        chunk = stream.read(n - len(body))
        if not chunk:
            raise RuntimeError("EOF in body")
        body += chunk
    return json.loads(body.decode("utf-8"))


def main() -> int:
    print(f"[smoke] spawning: python3 {SIDECAR}", flush=True)
    proc = subprocess.Popen(
        [sys.executable, SIDECAR],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None

    # 1) info() — should get VERSION_MISMATCH because we send pv=2
    bad = {"type": "req", "id": "test-1", "method": "info", "params": {"protocolVersion": 2}}
    proc.stdin.write(encode(bad))
    proc.stdin.flush()
    hello = read_frame(proc.stdout)
    res1 = read_frame(proc.stdout)
    print("[smoke] hello frame:", json.dumps(hello, ensure_ascii=False), flush=True)
    print("[smoke] version mismatch response:", json.dumps(res1, ensure_ascii=False), flush=True)
    assert hello["type"] == "evt" and hello["event"] == "hello"
    assert res1["ok"] is False
    assert res1["error"]["code"] == "VERSION_MISMATCH"

    # 2) ping with correct version
    good = {"type": "req", "id": "test-2", "method": "ping", "params": {"protocolVersion": 1}}
    proc.stdin.write(encode(good))
    proc.stdin.flush()
    res2 = read_frame(proc.stdout)
    print("[smoke] ping response:", json.dumps(res2, ensure_ascii=False), flush=True)
    assert res2["ok"] is True
    assert res2["result"]["pong"] is True
    assert isinstance(res2["result"]["ts"], int)

    # 3) info with correct version
    info = {"type": "req", "id": "test-3", "method": "info", "params": {"protocolVersion": 1}}
    proc.stdin.write(encode(info))
    proc.stdin.flush()
    res3 = read_frame(proc.stdout)
    print("[smoke] info response:", json.dumps(res3, ensure_ascii=False), flush=True)
    assert res3["ok"] is True
    assert res3["result"]["protocolVersion"] == 1
    assert "ping" in res3["result"]["capabilities"]
    assert res3["result"]["pid"] == proc.pid

    # Close stdin cleanly to let the sidecar exit.
    proc.stdin.close()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)

    # Sidecar must have logged to stderr (rule R1).
    err = proc.stderr.read().decode("utf-8", errors="replace")
    print("[smoke] stderr (should contain only log lines):", flush=True)
    print(err, end="", flush=True)

    rc = proc.returncode
    print(f"[smoke] sidecar exit code: {rc}", flush=True)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())