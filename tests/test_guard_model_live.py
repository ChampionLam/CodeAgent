"""guard 第二层（DecisionBert fp16）真实子进程端到端：本机实测用。

模型产物在盘（python/models/decision_bert/decision_bert.onnx 等）才
收集 —— CI / 没投放模型的机器上自动跳过，不红。这里跑的是真实
onnxruntime 子进程 + 真实 fp16 ONNX + 配对校准器，验证：
  * 子进程能起、能回校准后的 p；
  * 正则命中不调模型（子进程计数不涨）；
  * fail-open：路径指错 → predict 返回 None、guard 照常判定；
  * 词表一致性 fail-loud（2026-10-13 v6 fp16 起强制）：tokenizer 词表
    行数 != 模型 embedding 行数 → serve.py 拒绝起，predict 返回 None
    且 warn 日志写明 mismatch —— 不许静默错位跑。
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
import shutil  # noqa: E402
import tempfile  # noqa: E402

_MODEL = guard_model.default_model_path()
_HAVE_MODEL = all(os.path.isfile(p) for p in (
    _MODEL, _MODEL + ".data",
    guard_model.default_calibrator_path(),
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

    def test_v6_fp16_delivery_baseline(self):
        """交付包基线（2026-10-13 fp16 拍板）：ls -la ≈ 0.013、
        rm -rf / ≈ 0.977。钉住「模型/校准器/tokenizer 成套投放」——
        任何一件换错版本，这两个数立刻漂。
        """
        safe = self.model.predict("ls -la")
        self.assertIsNotNone(safe)
        self.assertAlmostEqual(safe["p"], 0.013, delta=0.02,
                               msg="ls -la 基线漂移：模型三件套不成套？")
        danger = self.model.predict("rm -rf /")
        self.assertIsNotNone(danger)
        self.assertAlmostEqual(danger["p"], 0.977, delta=0.02,
                               msg="rm -rf / 基线漂移：模型三件套不成套？")

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
class VocabMismatchFailLoudTests(unittest.TestCase):
    """词表一致性 fail-loud 回归（2026-10-13，v6 fp16 起强制）。

    坑：v6 模型词表扩到 22,765 行，配 v4 旧 tokenizer（21,128）ORT
    **不报错**（越界只在 id >= 22,765 才触发），p 静默漂移（实测
    ls -la 0.013 → 0.134）。serve.py 因此在启动时断言词表行数 ==
    embedding 行数，不一致拒绝起。本测试用截短词表的人为错配
    验证真的触发：predict → None + degradation_reason="unavailable"
    + 日志带 "vocab mismatch"。
    """

    def test_mismatched_tokenizer_fails_loudly(self):
        import json as _json

        # 造一个词表行数不同的 tokenizer：截掉 vocab 里最后一个
        # token（21128 行 != 22765 行，等价于投放了 v4 旧件）。
        with open(guard_model.default_tokenizer_path(),
                  encoding="utf-8") as f:
            tok = _json.load(f)
        vocab = tok["model"]["vocab"]
        last = max(vocab.values())
        del vocab[[k for k, v in vocab.items() if v == last][0]]

        tmp = tempfile.mkdtemp(prefix="guard_vocab_mismatch_")
        bad_tok = os.path.join(tmp, "tokenizer.json")
        try:
            with open(bad_tok, "w", encoding="utf-8") as f:
                _json.dump(tok, f)

            lines = []
            old_hook = guard_model._LOG_HOOK
            guard_model.set_log_hook(
                lambda level, msg: lines.append((level, msg)))
            m = None
            try:
                m = guard_model.GuardModel(
                    model_path=_MODEL,
                    calibrator_path=guard_model.default_calibrator_path(),
                    tokenizer_path=bad_tok)
                out = m.predict("ls -la")
                self.assertIsNone(out, "词表错配必须拒绝出结果")
                self.assertEqual(m.degradation_reason, "unavailable")
                self.assertTrue(
                    any("vocab mismatch" in msg for _, msg in lines),
                    "日志必须写明 mismatch 原因：%r" % (lines,))
            finally:
                guard_model.set_log_hook(old_hook)
                if m is not None:
                    m.shutdown()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


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
