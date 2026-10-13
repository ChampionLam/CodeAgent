# -*- coding: utf-8 -*-
"""logging_kit 单测：轮转、级别过滤、崩溃落盘（任务 t_5dd44e42 第 6 条）。

全在临时目录沙箱里跑，不碰真实 data/。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging_kit  # noqa: E402


class Silence:
    """把 stderr 静音，避免测试期间刷屏。"""

    def __init__(self):
        self.saved = None

    def __enter__(self):
        import io
        self.saved = sys.stderr
        sys.stderr = io.StringIO()
        return self

    def __exit__(self, *a):
        sys.stderr = self.saved


class LoggingKitTests(unittest.TestCase):

    def setUp(self):
        self.sandbox = tempfile.mkdtemp(prefix="lk-test-")
        logging_kit.reset_for_test()

    def tearDown(self):
        logging_kit.reset_for_test()

    def _logger(self, **kw):
        kw.setdefault("data_dir", self.sandbox)
        kw.setdefault("level", "debug")
        lg = logging_kit.AgentLogger(**kw)
        return lg

    def _read_jsonl(self, path):
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    # -- 结构化字段 -------------------------------------------------------

    def test_record_fields(self):
        lg = self._logger()
        lg.log("info", "hello 世界", source="python-sidecar",
               run_id="r1", session_id="s1", data={"tool": "read_file"})
        path = os.path.join(lg.log_dir, "agent-%s.log" % _today())
        recs = self._read_jsonl(path)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["level"], "info")
        self.assertEqual(r["source"], "python-sidecar")
        self.assertEqual(r["run_id"], "r1")
        self.assertEqual(r["session_id"], "s1")
        self.assertEqual(r["data"], {"tool": "read_file"})
        # ISO 带时区偏移（±HH:MM 结尾）或 Z
        self.assertRegex(r["ts"], r"\d{4}-\d{2}-\d{2}T.*(Z|[+-]\d{2}:\d{2})")
        # 可选字段缺省时不出现
        self.assertNotIn("nope", r)

    def test_optional_fields_omitted_when_absent(self):
        lg = self._logger()
        lg.log("warn", "bare")
        path = os.path.join(lg.log_dir, "agent-%s.log" % _today())
        r = self._read_jsonl(path)[0]
        self.assertNotIn("run_id", r)
        self.assertNotIn("session_id", r)
        self.assertNotIn("data", r)

    # -- 级别过滤 ---------------------------------------------------------

    def test_level_filtering(self):
        lg = self._logger(level="info")
        with Silence():
            lg.debug("hidden")
            lg.info("kept-info")
            lg.warn("kept-warn")
        path = os.path.join(lg.log_dir, "agent-%s.log" % _today())
        recs = self._read_jsonl(path)
        self.assertEqual([r["level"] for r in recs], ["info", "warn"])

    def test_runtime_set_level(self):
        lg = self._logger(level="error")
        with Silence():
            lg.info("hidden")
            self.assertTrue(lg.set_level("info"))
            lg.info("kept")
            self.assertFalse(lg.set_level("bogus"))   # 非法级别不改
            lg.log("debug", "still-hidden")
        path = os.path.join(lg.log_dir, "agent-%s.log" % _today())
        recs = self._read_jsonl(path)
        self.assertEqual([r["level"] for r in recs], ["info"])

    def test_level_alias_warning(self):
        lg = self._logger(level="info")
        with Silence():
            lg.log("WARNING", "alias")
        path = os.path.join(lg.log_dir, "agent-%s.log" % _today())
        recs = self._read_jsonl(path)
        self.assertEqual(recs[0]["level"], "warn")

    # -- 轮转 ---------------------------------------------------------------

    def test_rotate_keeps_tail_on_overflow(self):
        lg = self._logger(level="debug", max_file_bytes=64 * 1024)
        lg._make_dir_safe(lg.log_dir)
        path = os.path.join(lg.log_dir, "agent-%s.log" % _today())
        with open(path, "w", encoding="utf-8") as f:
            f.write("x" * 70000 + "\n")          # 前半截垃圾
            f.write('{"level":"error","msg":"KEEP-ME-TAIL"}\n')
        lg._maybe_rotate(path)                    # 缺省 wrote_bytes 直接触发检查
        size = os.path.getsize(path)
        self.assertLessEqual(size, 40 * 1024 + 64)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("KEEP-ME-TAIL", content)   # 尾部保留
        self.assertNotIn("xxx", content.strip()[:100])  # 头部被砍（首行不完整也丢）

    def test_retention_sweep(self):
        lg = self._logger()
        d = lg.log_dir
        os.makedirs(d, exist_ok=True)
        old = os.path.join(d, "agent-19990101.log")
        with open(old, "w", encoding="utf-8") as f:
            f.write("stale\n")
        # mtime 拨回 30 天前
        import time as _t
        os.utime(old, (_t.time() - 30 * 86400,) * 2)
        today = os.path.join(d, "agent-%s.log" % _today())
        with open(today, "w", encoding="utf-8") as f:
            f.write('{"level":"info","msg":"now"}\n')
        lg._sweep_stale()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(today))

    def test_crash_files_capped(self):
        lg = self._logger()
        d = lg.log_dir
        os.makedirs(d, exist_ok=True)
        for i in range(15):
            with open(os.path.join(d, "crash-20250101-%06d.log" % i),
                      "w", encoding="utf-8") as f:
                f.write("c%d\n" % i)
        lg._sweep_stale()
        crashes = [p for p in os.listdir(d) if p.startswith("crash-")]
        self.assertLessEqual(len(crashes), logging_kit.DEFAULT_CRASH_FILES_KEEP)

    def test_log_dir_override(self):
        override = os.path.join(self.sandbox, "custom-logs")
        lg = self._logger(log_dir_override=override)
        with Silence():
            lg.info("here")
        self.assertTrue(os.path.exists(
            os.path.join(override, "agent-%s.log" % _today())))

    # -- 崩溃落盘 -----------------------------------------------------------

    def test_rotate_triggers_after_far_more_than_64_writes(self):
        """回归（t_93437ef6 Bug 2）：预算耗尽后大小检查必须仍生效。

        一次性预算的老实现里，第 64 条后 _maybe_rotate 永远 return，
        文件无限增长。新实现取模节流：远超 64 条的连续写入后，超阈值
        文件必须被轮转，且内容是尾部。
        """
        # 注意 64KB 下限：配 2048 会被抬到 65536（configure 注释有说明）
        lg = self._logger(max_file_bytes=2048)
        with Silence():
            for i in range(500):
                lg.log("info", "line-%04d " % i + "z" * 100)
        path = os.path.join(lg.log_dir, "agent-%s.log" % _today())
        size = os.path.getsize(path)
        # 阈值被夹到 65536：500 条×~130B≈65KB，必须触发过至少一次轮转，
        # 且最终大小在阈值附近（不是无限增长到 65950+ 也不早停）
        self.assertLessEqual(size, lg._max_file_bytes + 8 * 1024)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # 尾部保留：最后的行在，最早的行被砍
        self.assertIn("line-0499", content)
        self.assertNotIn("line-0000", content)

    def test_max_file_bytes_floor_documented(self):
        # 64KB 硬下限：小于它的配置被夹紧（刻意设计，防误配出无意义轮转）
        lg = self._logger(max_file_bytes=1024)
        self.assertEqual(lg._max_file_bytes, 64 * 1024)

    # -- 崩溃落盘 -----------------------------------------------------------

    def test_crash_dump_writes_file_and_error_record(self):
        lg = self._logger()
        tb = "Traceback (most recent call last):\n  File \"x.py\"\nBoom: RuntimeError"
        with Silence():
            path = lg.crash_dump(title="unit-test crash",
                                 traceback_text=tb,
                                 source="python-sidecar", run_id="r9")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("Boom: RuntimeError", body)      # 完整 traceback 在
        self.assertIn("recent log tail", body)
        # 同时有 critical 级 JSONL 记录
        jpath = os.path.join(lg.log_dir, "agent-%s.log" % _today())
        recs = self._read_jsonl(jpath)
        self.assertTrue(any(r["level"] == "critical" and r.get("run_id") == "r9"
                            for r in recs))
        crit = [r for r in recs if r["level"] == "critical"][0]
        self.assertIn("Boom", crit["data"]["traceback"])
        self.assertEqual(crit["data"]["crash_file"], path)

    def test_crash_dump_unwritable_dir_falls_back_to_tmp(self):
        # log 目录指向一个不可写路径（用只读文件占住目录名）
        readonly = os.path.join(self.sandbox, "blocked")
        with open(readonly, "w", encoding="utf-8") as f:
            f.write("i am a file, not a dir")
        lg = self._logger(log_dir_override=os.path.join(readonly, "logs"))
        with Silence():
            path = lg.crash_dump(title="t", traceback_text="TB-XYZ")
        self.assertIsNotNone(path)
        self.assertTrue(os.path.exists(path))
        self.assertIn(tempfile.gettempdir(), path)
        os.remove(path)   # 清理临时目录里的产物

    # -- 永不抛 ----------------------------------------------------------------

    def test_log_never_raises(self):
        readonly = os.path.join(self.sandbox, "blocked2")
        with open(readonly, "w", encoding="utf-8") as f:
            f.write("not a dir")
        lg = self._logger(log_dir_override=os.path.join(readonly, "logs"))
        with Silence():
            lg.info("no crash please")   # 不抛即通过
            path = lg.crash_dump(title="t2", traceback_text="tb")

    def test_data_truncated(self):
        lg = self._logger()
        big = {"blob": "y" * 8000}
        with Silence():
            lg.log("error", "big", data=big)
        path = os.path.join(lg.log_dir, "agent-%s.log" % _today())
        r = self._read_jsonl(path)[0]
        self.assertTrue(r["data"].get("_truncated"))

    def test_compat_log(self):
        logging_kit.reset_for_test()
        lg = logging_kit.configure(self.sandbox, level="info")
        with Silence():
            logging_kit.compat_log("warn", "compat works")
        path = os.path.join(lg.log_dir, "agent-%s.log" % _today())
        r = self._read_jsonl(path)[0]
        self.assertEqual(r["source"], "python-sidecar")
        self.assertEqual(r["msg"], "compat works")

    def test_singleton_configure_and_env_level(self):
        os.environ["DESK_AGENT_LOG_LEVEL"] = "warn"
        try:
            logging_kit.reset_for_test()
            lg = logging_kit.get_logger()   # 未 configure：惰性
            self.assertEqual(lg.level, "warn")
        finally:
            del os.environ["DESK_AGENT_LOG_LEVEL"]


def _today() -> str:
    import time
    return time.strftime("%Y%m%d")


if __name__ == "__main__":
    unittest.main()
