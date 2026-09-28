"""Regression: the sidecar must emit `evt hello` unsolicited at startup.

The desktop client waits for hello before it will send anything (45s budget,
"sidecar not ready: starting" until it lands). If the sidecar instead waits for
the client's first frame, both sides deadlock. That bug made the Electron UI
unusable until 2026-09-23 -- it survived the earlier smoke/E2E scripts because
those send a request *before* reading hello, which hid the ordering assumption.

Keep this test: it reads first and never sends anything.
"""
import json
import os
import queue
import subprocess
import sys
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIDECAR = os.path.join(REPO, "python", "sidecar.py")
TIMEOUT_S = 30.0


def _read_frame(stream):
    """Read one LSP-style frame: headers, blank line, then Content-Length bytes."""
    length = None
    while True:
        line = stream.readline()
        if not line:
            raise EOFError("stream closed before a frame arrived")
        line = line.strip()
        if not line:
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    if length is None:
        raise EOFError("frame had no Content-Length header")
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            raise EOFError("truncated frame body")
        body += chunk
    return json.loads(body.decode("utf-8"))


def _write_frame(stream, payload):
    body = json.dumps(payload).encode("utf-8")
    stream.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
    stream.flush()


def _read_with_timeout(stream, timeout=TIMEOUT_S):
    """Read a frame on a worker thread so a stuck sidecar fails instead of hanging."""
    box = queue.Queue()

    def pump():
        try:
            box.put(_read_frame(stream))
        except Exception as exc:  # noqa: BLE001 - surfaced to the test
            box.put(exc)

    threading.Thread(target=pump, daemon=True).start()
    try:
        return box.get(timeout=timeout)
    except queue.Empty:
        return TimeoutError(f"no frame within {timeout}s")


class HelloUnsolicitedTest(unittest.TestCase):
    def test_hello_arrives_before_any_request(self):
        proc = subprocess.Popen(
            [sys.executable, SIDECAR],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=REPO,
        )
        try:
            hello = _read_with_timeout(proc.stdout)
            self.assertNotIsInstance(hello, Exception, f"hello read failed: {hello!r}")
            self.assertEqual(hello.get("type"), "evt")
            self.assertEqual(hello.get("event"), "hello")
            self.assertEqual(hello["data"]["protocolVersion"], 1)
            self.assertIn("approval.respond", hello["data"]["capabilities"])

            _write_frame(proc.stdin, {
                "type": "req", "id": "p1", "method": "ping",
                "params": {"protocolVersion": 1},
            })
            pong = _read_with_timeout(proc.stdout)
            self.assertNotIsInstance(pong, Exception, f"ping failed: {pong!r}")
            self.assertTrue(pong.get("ok"), pong)
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()


if __name__ == "__main__":
    unittest.main()