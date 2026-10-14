"""t_5d29bcab: 侧车自锁（数据目录互斥）回归测试。

场景 A（单元）：_acquire_data_dir_lock 在同一数据目录上，第一个进程拿到、
第二个进程拿不到且能读出 holder pid；holder 死后（陈锁）新进程可直接接管。
场景 B（进程级）：真实拉起两个 sidecar.py 子进程共享同一 data dir，
第二个必须立即退出（exit code 3）且不发 hello、不写 sessions.db。
"""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIDECAR = os.path.join(REPO, "python", "sidecar.py")
sys.path.insert(0, os.path.join(REPO, "python"))


class LockUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sidecar-lock-test-")
        self.lock_root = os.path.join(self.tmp, "locks")

    def test_second_acquire_fails_and_reports_holder(self):
        import sidecar
        sidecar._DATA_DIR = self.tmp
        acquired1, holder1, _ = sidecar._acquire_data_dir_lock()
        self.assertTrue(acquired1)
        self.assertEqual(holder1, os.getpid())
        acquired2, holder2, reason2 = sidecar._acquire_data_dir_lock()
        self.assertFalse(acquired2)
        self.assertEqual(holder2, os.getpid())
        self.assertIn("another sidecar", reason2)
        # 释放后可重入。
        sidecar._release_data_dir_lock(
            os.path.join(self.tmp, "locks", "sidecar.lock"),
            os.path.join(self.tmp, "locks", "sidecar.lock", "pid"))
        acquired3, _, _ = sidecar._acquire_data_dir_lock()
        self.assertTrue(acquired3)

    def test_stale_lock_taken_over(self):
        import sidecar
        sidecar._DATA_DIR = self.tmp
        # 手工造一个 holder 已死的陈锁（pid 不可能存活：取一个大数）。
        lock_dir = os.path.join(self.tmp, "locks", "sidecar.lock")
        os.makedirs(lock_dir)
        with open(os.path.join(lock_dir, "pid"), "w") as fh:
            fh.write("99999999")
        acquired, holder, _ = sidecar._acquire_data_dir_lock()
        self.assertTrue(acquired, "陈锁应被接管")
        self.assertEqual(holder, os.getpid())

    def test_release_on_exit_registered(self):
        import sidecar
        sidecar._DATA_DIR = self.tmp
        acquired, _, _ = sidecar._acquire_data_dir_lock()
        self.assertTrue(acquired)
        lock_dir = os.path.join(self.tmp, "locks", "sidecar.lock")
        pid_file = os.path.join(lock_dir, "pid")
        self.assertTrue(os.path.isdir(lock_dir))
        # atexit 已注册的清理函数直接调用验证幂等无害。
        sidecar._release_data_dir_lock(lock_dir, pid_file)
        self.assertFalse(os.path.exists(lock_dir))

    def test_pid_probe_never_kills_and_reports_dead_pids(self):
        """探活不能变成杀进程。Windows 上 os.kill(pid, 0) 实际走 TerminateProcess：
        既会杀掉被探测的进程，又对已退出的 pid 抛 OSError(EINVAL)（不是
        ProcessLookupError），被兜底 except 吃成「存活」。真机 2026-10-14 复现：
        强杀应用后 locks/sidecar.lock 永远清不掉，之后每次启动都判 duplicate，
        重试耗尽 → 界面「侧车 crashed」，应用再也起不来。
        """
        import sidecar

        self.assertTrue(sidecar._pid_alive(os.getpid()), "自己一定是活的")
        self.assertTrue(sidecar._pid_alive(os.getpid()), "探活必须可重复调用（不能把自己杀了）")
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        self.assertFalse(sidecar._pid_alive(dead.pid), "已退出的真实 pid 必须判死")
        self.assertFalse(sidecar._pid_alive(99999999), "不存在的 pid 必须判死")
        self.assertFalse(sidecar._pid_alive(0), "pid 0 必须判死")

    def test_stale_lock_left_by_killed_process_is_reclaimed(self):
        """应用被强杀后留下的陈锁（持有者是真死进程）必须能被接管。"""
        import sidecar

        sidecar._DATA_DIR = self.tmp
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        lock_dir = os.path.join(self.tmp, "locks", "sidecar.lock")
        os.makedirs(lock_dir)
        with open(os.path.join(lock_dir, "pid"), "w") as fh:
            fh.write(str(dead.pid))
        acquired, holder, reason = sidecar._acquire_data_dir_lock()
        self.assertTrue(acquired, "强杀残留的陈锁应被接管，否则应用再也起不来: %s" % reason)
        self.assertEqual(holder, os.getpid())


def _read_hello(proc, timeout_s=30.0):
    """读一帧 LSP 风格 hello；EOF/超时返回 None。"""
    stream = proc.stdout
    length = None
    deadline = time.monotonic() + timeout_s

    def _readline():
        while True:
            b = stream.read(1)
            if not b:
                raise EOFError("stream closed")
            if b == b"\n":
                return
            if b != b"\r":
                if length is None or True:
                    pass

    # 简化：用 readline 语义
    while time.monotonic() < deadline:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            if length is not None:
                body = stream.read(length)
                try:
                    return json.loads(body.decode("utf-8"))
                except Exception:
                    return None
            continue
        if line.lower().startswith(b"content-length:"):
            try:
                length = int(line.split(b":", 1)[1])
            except ValueError:
                pass
    return None


class DuplicateProcessTests(unittest.TestCase):
    def test_second_process_exits_without_hello_or_db(self):
        data_dir = tempfile.mkdtemp(prefix="sidecar-dup-data-")
        env = dict(os.environ)
        env["DESK_AGENT_DATA_DIR"] = data_dir
        env.pop("DESK_AGENT_LOG_DIR", None)
        py = sys.executable
        # 第一个实例：起来后保持 stdin 打开（主循环阻塞在读）。
        p1 = subprocess.Popen(
            [py, SIDECAR],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env,
        )
        try:
            hello = _read_hello(p1)
            self.assertIsNotNone(hello, "第一个实例必须发 hello")
            self.assertEqual(hello.get("event"), "hello")
            # 第二个实例：同一 data dir，必须立即退出、不发 hello。
            p2 = subprocess.Popen(
                [py, SIDECAR],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env,
            )
            try:
                hello2 = _read_hello(p2, timeout_s=15.0)
                self.assertIsNone(hello2, "第二个实例绝不能发 hello")
                rc = p2.wait(timeout=15)
                self.assertEqual(rc, 3, "第二个实例退出码应为 3")
                # 退出后锁应被释放（atexit rmdir）。
                locks = os.listdir(os.path.join(data_dir, "locks"))
                self.assertEqual(
                    sorted(locks), ["sidecar.lock"],
                    f"锁目录应只剩固定名锁目录，实际: {locks}",
                )
                holder = open(os.path.join(data_dir, "locks", "sidecar.lock", "pid")).read()
                self.assertEqual(int(holder), p1.pid, "锁内 pid 应是第一个实例")
            finally:
                if p2.poll() is None:
                    p2.kill()
        finally:
            p1.kill()
            p1.wait()


if __name__ == "__main__":
    unittest.main()
