# -*- coding: utf-8 -*-
"""t_3424e94a 断言测试：auto_apply 默认值的真实行为。

判定（依据任务书 t_e0fbba7d 第 4-5 条 + 红线实际位置）：
  - 签名默认 auto_apply=True 是对的：任务书要求「验证通过才写到真实位置」。
  - 红线 R1-R5 在 apply 之前由 redline_violation() 拦截，与 auto_apply 无关。

本文件断言：
  1. 默认构造（不传 auto_apply）下，验证通过的合法补丁自动应用（stage=verified）。
  2. 默认构造下，红线场景（路径逃逸）依然 stop —— 证明 auto_apply=True
     不等于「红线自动过」。
  3. auto_apply=False 时：验证通过也不落盘（stage=skipped），原因可读。
  4. 正常补丁全程零真实模型（RecordingModel 桩）。
"""

import inspect
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import error_taxonomy as et
import self_heal as sh


def _facts():
    return et.ErrorFacts(kind=et.KIND_TOOL_FAILURE, source="tool:run_shell",
                         code="NONZERO_EXIT", message="ZeroDivisionError",
                         context={"argsSummary": ""})


GOOD_JSON = json.dumps({
    "diagnosis": "除零", "file": "broken.py",
    "old_text": "    return a / 0", "new_text": "    return a / b",
    "commands": [], "explanation": "改成除以 b",
}, ensure_ascii=False)


class _Model:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def __call__(self, prompt):
        self.calls += 1
        return self.reply


def _engine(tmp, model, ws=None, **kw):
    ws = ws or os.path.join(tmp, "ws")
    os.makedirs(ws, exist_ok=True)
    return sh.SelfHealEngine(
        ask_model=model, workspace_root=ws,
        run_verifier=lambda f, c: (True, "stub-ok"),
        data_dir=os.path.join(tmp, "data"), **kw), ws


class AutoApplyDefault(unittest.TestCase):

    def test_signature_default_is_true(self):
        sig = inspect.signature(sh.SelfHealEngine.__init__)
        self.assertIs(sig.parameters["auto_apply"].default, True,
                      "auto_apply 签名默认值应为 True")

    def test_default_applies_verified_patch(self):
        tmp = tempfile.mkdtemp()
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "broken.py"), "w", encoding="utf-8") as fh:
            fh.write("def div(a, b):\n    return a / 0\n")
        eng, _ = _engine(tmp, _Model(GOOD_JSON), ws=ws)  # 不传 auto_apply
        out = eng.heal(_facts())
        self.assertEqual(out.stage, "verified")
        self.assertTrue(out.applied)
        with open(os.path.join(ws, "broken.py"), encoding="utf-8") as fh:
            self.assertIn("return a / b", fh.read())

    def test_default_does_not_pass_redlines(self):
        """默认（True）下红线场景必须 stop 且失败原因可读。"""
        tmp = tempfile.mkdtemp()
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        bad = json.dumps({"diagnosis": "d", "file": "../outside.py",
                          "old_text": "x", "new_text": "y", "commands": []})
        eng, _ = _engine(tmp, _Model(bad), ws=ws)  # 不传 auto_apply
        out = eng.heal(_facts())
        self.assertEqual(out.stage, "stopped")
        self.assertIn("红线", out.failure_reason)
        self.assertIn("R5", out.failure_reason)
        self.assertFalse(out.applied)
        self.assertTrue(out.failure_reason.endswith("需人工确认") or
                        "已停止" in out.failure_reason)

    def test_auto_apply_false_skips_apply_with_readable_reason(self):
        tmp = tempfile.mkdtemp()
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "broken.py"), "w", encoding="utf-8") as fh:
            fh.write("def div(a, b):\n    return a / 0\n")
        eng, _ = _engine(tmp, _Model(GOOD_JSON), ws=ws, auto_apply=False)
        out = eng.heal(_facts())
        self.assertEqual(out.stage, "skipped")
        self.assertIn("auto_apply=False", out.failure_reason)
        self.assertFalse(out.applied)
        with open(os.path.join(ws, "broken.py"), encoding="utf-8") as fh:
            self.assertIn("a / 0", fh.read())  # 真实文件没动

    def test_no_real_model_dependency(self):
        tmp = tempfile.mkdtemp()
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "broken.py"), "w", encoding="utf-8") as fh:
            fh.write("def div(a, b):\n    return a / 0\n")
        model = _Model(GOOD_JSON)
        eng, _ = _engine(tmp, model, ws=ws)
        eng.heal(_facts())
        self.assertEqual(model.calls, 1)  # 桩模型，零网络


if __name__ == "__main__":
    unittest.main(verbosity=2)
