# -*- coding: utf-8 -*-
"""t_ac650ad0 回归测试：真模型下自愈拿不到可执行补丁的修复。

覆盖三块：
  1. 提示词硬化 —— 契约条目写死（字段名/逐字 old_text/禁解释文字）。
  2. 解析失败 → 带原样报错重试一次；重试产出可用补丁 → 正常走完
     沙箱验证 + 应用，stage=verified。
  3. 重试也不产出 → failed 不崩；重试路径真调了模型（ask_model 计数）。
  4. 契约字段与红线/防抖行为不回归。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import error_taxonomy as et
import self_heal


def _facts(message="ZeroDivisionError: division by zero", tb=""):
    return et.ErrorFacts(
        kind=et.KIND_TOOL_FAILURE, source="tool:run_shell",
        code="NONZERO_EXIT", message=message,
        context={"argsSummary": ""}, traceback=tb)


class RecordingModel:
    """按顺序吐预置回复，记录每个 prompt。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else ""


def _make_engine(tmp, model, workspace=None):
    ws = workspace or os.path.join(tmp, "ws")
    os.makedirs(ws, exist_ok=True)
    return (self_heal.SelfHealEngine(
        ask_model=model, workspace_root=ws,
        run_verifier=lambda f, c: (True, "stub-ok"),
        data_dir=os.path.join(tmp, "data"), auto_apply=True), ws)


GOOD_JSON = json.dumps({
    "diagnosis": "除零",
    "file": "broken.py",
    "old_text": "    return a / 0",
    "new_text": "    return a / b",
    "commands": [],
    "explanation": "改成除以 b",
}, ensure_ascii=False)


class PromptHardening(unittest.TestCase):

    def test_contract_baked_into_prompt(self):
        model = RecordingModel([GOOD_JSON])
        eng, _ = _make_engine(tempfile.mkdtemp(), model)
        prompt = eng._diagnosis_prompt(_facts())
        for token in ('"diagnosis"', '"file"', '"old_text"', '"new_text"',
                      '"commands"', '"explanation"', "逐字", "解释性文字",
                      "old_text 必须逐字来自文件内容"):
            self.assertIn(token, prompt, "提示词缺契约条目: %r" % token)

    def test_retry_prompt_echoes_previous_output(self):
        model = RecordingModel([GOOD_JSON])
        eng, _ = _make_engine(tempfile.mkdtemp(), model)
        rp = eng._retry_prompt(_facts(), "前面解释了一堆 后面没 JSON", "输出不是合法 JSON 对象")
        self.assertIn("前面解释了一堆", rp)
        self.assertIn("只输出 JSON", rp)
        self.assertIn("输出不是合法 JSON 对象", rp)


class RetryOnce(unittest.TestCase):

    def test_first_reply_bad_retry_yields_patch_verified(self):
        tmp = tempfile.mkdtemp()
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "broken.py"), "w", encoding="utf-8") as fh:
            fh.write("def div(a, b):\n    return a / 0\n")
        # 第一轮：真模型式废话（有 diagnosis 没 file）——就是线上日志那种
        bad = ("根据日志分析，broken.py 存在除零错误，建议检查该函数的除法逻辑，"
               "可能是参数传入为零导致运行时异常。")
        model = RecordingModel([bad, GOOD_JSON])
        eng, _ = _make_engine(tmp, model, workspace=ws)
        out = eng.heal(_facts())
        self.assertEqual(out.stage, "verified")
        self.assertTrue(out.applied)
        with open(os.path.join(ws, "broken.py"), encoding="utf-8") as fh:
            self.assertIn("return a / b", fh.read())
        self.assertEqual(len(model.prompts), 2)  # 重试真的调了模型
        self.assertIn("只输出 JSON", model.prompts[1])

    def test_non_json_then_json_retry(self):
        tmp = tempfile.mkdtemp()
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "broken.py"), "w", encoding="utf-8") as fh:
            fh.write("def div(a, b):\n    return a / 0\n")
        fenced = "```json\n%s\n```" % GOOD_JSON.replace("a / 0", "a / 0")
        model = RecordingModel(["我认为这个错误是除零导致的。", fenced])
        eng, _ = _make_engine(tmp, model, workspace=ws)
        out = eng.heal(_facts())
        self.assertEqual(out.stage, "verified")
        with open(os.path.join(ws, "broken.py"), encoding="utf-8") as fh:
            self.assertIn("a / b", fh.read())

    def test_retry_also_fails_then_failed_not_crash(self):
        tmp = tempfile.mkdtemp()
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "broken.py"), "w", encoding="utf-8") as fh:
            fh.write("def div(a, b):\n    return a / 0\n")
        model = RecordingModel(["还是废话", "又是废话"])
        eng, _ = _make_engine(tmp, model, workspace=ws)
        out = eng.heal(_facts())
        self.assertEqual(out.stage, "failed")
        self.assertIn("重试", out.failure_reason)
        self.assertEqual(len(model.prompts), 2)
        # 文件没被乱动
        with open(os.path.join(ws, "broken.py"), encoding="utf-8") as fh:
            self.assertIn("a / 0", fh.read())

    def test_first_reply_good_no_extra_call(self):
        tmp = tempfile.mkdtemp()
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "broken.py"), "w", encoding="utf-8") as fh:
            fh.write("def div(a, b):\n    return a / 0\n")
        model = RecordingModel([GOOD_JSON])
        eng, _ = _make_engine(tmp, model, workspace=ws)
        out = eng.heal(_facts())
        self.assertEqual(out.stage, "verified")
        self.assertEqual(len(model.prompts), 1)  # 首轮可用 → 不重试

    def test_model_exception_does_not_kill_heal(self):
        class Boom:
            def __call__(self, prompt):
                raise RuntimeError("LLM down")
        tmp = tempfile.mkdtemp()
        eng, _ = _make_engine(tmp, Boom())
        out = eng.heal(_facts())
        self.assertEqual(out.stage, "failed")


class GuardsIntact(unittest.TestCase):

    def test_dependency_missing_skips_model(self):
        model = RecordingModel([])
        eng, _ = _make_engine(tempfile.mkdtemp(), model)
        facts = et.ErrorFacts(kind=et.KIND_DEPENDENCY_MISSING, code="DEP_MISSING",
                              context={"dependency": "numpy"})
        out = eng.heal(facts)
        self.assertEqual(out.stage, "skipped")
        self.assertEqual(model.prompts, [])

    def test_redline_still_stops(self):
        tmp = tempfile.mkdtemp()
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        with open(os.path.join(ws, "t.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1\n")
        bad = json.dumps({"diagnosis": "d", "file": "../outside.py",
                          "old_text": "x", "new_text": "y", "commands": []})
        model = RecordingModel([bad])
        eng, _ = _make_engine(tmp, model, workspace=ws)
        out = eng.heal(_facts())
        self.assertEqual(out.stage, "stopped")
        self.assertIn("红线", out.failure_reason)

    def test_budget_still_stops(self):
        tmp = tempfile.mkdtemp()
        eng, ws = _make_engine(tmp, RecordingModel([GOOD_JSON]))
        # 预写历史把该错误灌满
        os.makedirs(eng.data_dir, exist_ok=True)
        hist = [{"kind": et.KIND_TOOL_FAILURE, "code": "NONZERO_EXIT"}
                for _ in range(self_heal.SAME_ERROR_BUDGET)]
        with open(eng.history_path, "w", encoding="utf-8") as fh:
            json.dump(hist, fh)
        out = eng.heal(_facts())
        self.assertEqual(out.stage, "stopped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
