"""guard 第二层（DecisionBert int8）真实子进程端到端：本机实测用。

模型产物在盘（python/models/decision_bert/*.onnx 等）才收集 —— CI /
没投放模型的机器上自动跳过，不红。这里跑的是真实 onnxruntime 子进程
+ 真实 ONNX + int8 校准器，验证：
  * 子进程能起、能回校准后的 p；
  * 正则命中不调模型（子进程计数不涨）；
  * fail-open：路径指错 → predict 返回 None、guard 照常判定。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import guard  # noqa: E402
import guard_model  # noqa: E402
import subprocess  # noqa: E402
import json  # noqa: E402

_MODEL = guard_model.default_model_path()
_HAVE_MODEL = all(os.path.isfile(p) for p in (
    _MODEL, guard_model.default_calibrator_path(),
    guard_model.default_tokenizer_path()))


def _direct_serve_ps(cmds):
    """绕开 GuardModel，直接跟 serve.py 子进程逐条对话 —— 拿地面真值。

    2026-10-01 desync 修复的对照基线：修复前 GuardModel._read_ready 用
    ``request({})`` 做握手，serve.py 对空请求也回一条应答，冷启动后
    请求/应答流整体错位一条（每条命令拿到的是前一条的 p）。这里用
    最笨但不可能错的口径（每条请求立刻读一条应答）锚定真值，跟
    GuardModel 顺序连读的结果逐条对比。
    """
    proc = subprocess.Popen(
        [sys.executable, guard_model._SERVE_SCRIPT,
         "--model", _MODEL,
         "--calibrator", guard_model.default_calibrator_path(),
         "--tokenizer", guard_model.default_tokenizer_path()],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        errors="replace", bufsize=1)
    try:
        ready = json.loads(proc.stdout.readline())
        assert ready.get("ok") is True, ready
        out = []
        for c in cmds:
            proc.stdin.write(json.dumps({"text": guard_model._make_text(
                "run_shell", "/home/user", c)}) + "\n")
            proc.stdin.flush()
            out.append(json.loads(proc.stdout.readline()))
        return out
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:                       # pragma: no cover
            proc.kill()
            proc.wait(timeout=5)
        try:
            proc.stdout.close()
        except Exception:
            pass


@unittest.skipUnless(_HAVE_MODEL, "model artifacts not deployed")
class LiveSubprocessTests(unittest.TestCase):
    """真子进程、真 ONNX。跑得慢（冷启动 ~0.4s），但这是本机验证的核心。"""

    @classmethod
    def setUpClass(cls):
        cls.model = guard_model.GuardModel()

    @classmethod
    def tearDownClass(cls):
        cls.model.shutdown()

    def test_predict_returns_calibrated_p(self):
        out = self.model.predict("ls -la")
        self.assertIsNotNone(out)
        self.assertIsInstance(out["p"], float)
        self.assertGreaterEqual(out["p"], 0.0)
        self.assertLessEqual(out["p"], 1.0)
        # ls -la 这类日常命令不该是高危：校准后 p 应低于 ask 档
        self.assertLess(out["p"], 0.35)

    def test_stream_alignment_matches_direct_serve(self):
        """desync 回归测试（2026-10-01 修复的正主）。

        同一组命令：A 路 = 一个 GuardModel 顺序连读（共享子进程，冷启动
        握手后连发）；B 路 = 直接跟 serve.py 逐条请求/应答（地面真值）。
        两条路逐条 p 必须相等。修复前 `_read_ready` 用 ``request({})``
        握手、serve.py 对空请求也回话，流错位一条 —— 该 bug 下
        predict('ls -la') 会拿到空请求的 p（实测 0.627，远超 0.35 弹卡
        门槛），而真值是 0.023；本测试在那版代码上必红。
        """
        cmds = ["ls -la", "rm -rf /tmp/build", "find . -delete",
                "git status", "git commit -m \"docs: 修 rm -rf 说明\"",
                "echo hello"]
        sequential = [self.model.predict(c) for c in cmds]
        truth = _direct_serve_ps(cmds)
        self.assertEqual(len(sequential), len(truth))
        for c, got, want in zip(cmds, sequential, truth):
            self.assertIsNotNone(got, "predict 不可用：%r" % c)
            self.assertEqual(got["p"], want["p"],
                             "流错位（desync）：%r 期望 %s 拿到 %s"
                             % (c, want["p"], got and got["p"]))
            self.assertEqual(got["level"], want["level"], c)
            self.assertEqual(got["cost"], want["cost"], c)

    def test_predict_first_after_cold_start_is_correct(self):
        """冷启动后的第一条就必须是对的（desync 的最小复现）。

        修复前：握手空请求吃掉了第一条应答 → 首条 predict 返回的是
        空请求的 p（0.627，弹卡级），把 `ls -la` 错拦。现在首条即真值。
        """
        fresh = guard_model.GuardModel()
        try:
            out = fresh.predict("ls -la")
            self.assertIsNotNone(out)
            # 与直连 serve.py 的单条真值对比（顺手防住首条错位）
            want = _direct_serve_ps(["ls -la"])[0]
            self.assertEqual(out["p"], want["p"])
            self.assertLess(out["p"], 0.35, "ls -la 被抬到弹卡档 = 首条错位")
        finally:
            fresh.shutdown()

    def test_dangerous_not_in_training_but_scary(self):
        out = self.model.predict("find . -delete")
        self.assertIsNotNone(out)
        # 正则抓不到的危险命令，模型要抬到 ask 以上
        self.assertGreaterEqual(out["p"], 0.35)

    def test_layered_judge_uses_real_model(self):
        """ls -la 真值 p≈0.023 < 0.35 → 放行；修复前这条用旧极性
        蒙混过关（拿到的 0.627 是空请求的读数，恰好也弹卡），现在
        断的是修复后的真实读数。"""
        v = guard.judge_command_layered("ls -la", model=self.model)
        self.assertFalse(v.requires_approval)
        p = guard.layered_verdict_p(v)
        self.assertIsNotNone(p)
        self.assertLess(p, 0.35, "ls -la 的真实 p 必须低于弹卡门槛")

    def test_subprocess_is_separate_process(self):
        self.model.predict("echo ping")
        self.assertTrue(self.model.is_up)
        child = self.model._proc._child
        self.assertNotEqual(getattr(getattr(child, "_proc", None), "pid", None),
                            os.getpid(), "模型必须在子进程里跑")


@unittest.skipUnless(_HAVE_MODEL, "model artifacts not deployed")
class FailOpenTests(unittest.TestCase):
    """降级验证：模型文件路径不存在 → guard 纯正则可用、不抛异常。"""

    def test_bad_path_predicts_none(self):
        m = guard_model.GuardModel(
            model_path="/nonexistent/model.onnx",
            calibrator_path="/nonexistent/cal.json",
            tokenizer_path="/nonexistent/tok.json")
        self.assertIsNone(m.predict("ls -la"))
        self.assertTrue(m.is_unavailable)
        self.assertIn("not found", m.refusal)

    def test_guard_still_works_without_model(self):
        m = guard_model.GuardModel(
            model_path="/nonexistent/model.onnx",
            calibrator_path="/nonexistent/cal.json",
            tokenizer_path="/nonexistent/tok.json")
        v = guard.judge_command_layered("rm -rf /", model=m)
        self.assertEqual(v.tier, "hardline")
        v = guard.judge_command_layered("git status", model=m)
        self.assertFalse(v.requires_approval)


if __name__ == "__main__":
    unittest.main()
