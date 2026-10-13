# -*- coding: utf-8 -*-
"""error_taxonomy + self_heal 单测（P0-5）：分类/红线/沙箱/回滚/预算/经验/检索。

模型层全部用桩：ask_model 返回固定 JSON；run_verifier 可控成败 ——
不依赖真实模型即可跑通全链路（任务书 §8 要求）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import error_taxonomy as et          # noqa: E402
import self_heal as sh               # noqa: E402


class MissingDependencyTest(unittest.TestCase):
    def test_import_error_extracted(self):
        self.assertEqual(
            et.missing_dependency_of("ModuleNotFoundError: No module named 'pdfplumber.page'"),
            "pdfplumber")
        self.assertEqual(
            et.missing_dependency_of("ImportError: cannot import name 'x' from 'PIL'"),
            "PIL")

    def test_command_not_found_codes(self):
        self.assertEqual(et.missing_dependency_of("pdftotext: command not found", exit_code=127),
                         "pdftotext")
        self.assertEqual(
            et.missing_dependency_of("'magick' 不是内部或外部命令", exit_code=9009), "magick")

    def test_no_dependency_in_plain_errors(self):
        self.assertIsNone(et.missing_dependency_of("HTTP 500 internal error"))
        self.assertIsNone(et.missing_dependency_of(""))

    def test_fix_hint(self):
        hint = et.dependency_fix_hint("PIL")
        self.assertIn("pillow", hint)
        self.assertIn("pip install", hint)


class ClassifyTest(unittest.TestCase):
    def test_tool_failure_facts(self):
        class R:
            ok = False
            error_code = "EXTRACT"
            error_message = "no text layer"
            content = "no text layer"
            data = {}
        f = et.classify_tool_failure("read_file", {"path": "/tmp/a.pdf"}, R(),
                                     input_summary="读一下这个")
        self.assertEqual(f.kind, et.KIND_TOOL_FAILURE)
        self.assertEqual(f.source, "tool:read_file")
        self.assertEqual(f.context["tool"], "read_file")
        self.assertEqual(f.input_summary, "读一下这个")

    def test_tool_dependency_missing(self):
        class R:
            ok = False
            error_code = "COMMAND_NOT_FOUND"
            error_message = "pdftotext: command not found"
            content = ""
            data = {"exitCode": 127}
        f = et.classify_tool_failure("run_shell", {"command": "pdftotext a.pdf -"}, R())
        self.assertEqual(f.kind, et.KIND_DEPENDENCY_MISSING)
        self.assertEqual(f.context["dependency"], "pdftotext")

    def test_event_error_facts(self):
        f = et.classify_event_error({"code": "HTTP_ERROR", "message": "boom", "httpStatus": 502})
        self.assertEqual(f.kind, et.KIND_MODEL_ANOMALY)
        self.assertEqual(f.code, "HTTP_ERROR")
        self.assertEqual(f.context.get("httpStatus"), 502)

    def test_process_crash_facts(self):
        f = et.classify_process_crash(86, "Traceback ... File \"sidecar.py\"")
        self.assertEqual(f.kind, et.KIND_PROCESS_CRASH)
        self.assertEqual(f.code, "CRASH_EXIT_86")

    def test_import_exception_classifies_dependency(self):
        try:
            import nonexistent_module_xyz  # noqa: F401
        except ImportError as e:
            f = et.classify_exception(e)
            self.assertEqual(f.kind, et.KIND_DEPENDENCY_MISSING)
            self.assertEqual(f.context.get("dependency"), "nonexistent_module_xyz")


def _action_json(file="app.py", old="import foo", new="import bar",
                 commands=None, diagnosis="坏的", explanation="这样修"):
    return json.dumps({"diagnosis": diagnosis, "file": file, "old_text": old,
                       "new_text": new, "commands": commands or [],
                       "explanation": explanation}, ensure_ascii=False)


class RedlineTest(unittest.TestCase):
    def _action(self, **kw):
        defaults = {"file": "app.py", "old_text": "a", "new_text": "b",
                    "commands": [], "diagnosis": "", "explanation": ""}
        defaults.update(kw)
        return sh.HealAction(**defaults)

    def test_config_file_blocked(self):
        v = sh.redline_violation(self._action(file="config.json"), "/ws", None)
        self.assertIsNotNone(v)
        self.assertIn("R2", v)

    def test_env_blocked(self):
        self.assertIsNotNone(sh.redline_violation(self._action(file=".env"), "/ws", None))
        self.assertIsNotNone(sh.redline_violation(
            self._action(file="sub/dir/config.local.json"), "/ws", None))

    def test_pure_delete_blocked(self):
        v = sh.redline_violation(
            self._action(old_text="data = {...}", new_text=""), "/ws", None)
        self.assertIsNotNone(v)
        self.assertIn("R1", v)

    def test_system_code_blocked(self):
        self.assertIsNotNone(sh.redline_violation(self._action(file="python/tools.py"), "/ws", None))
        self.assertIsNotNone(sh.redline_violation(self._action(file="electron/main.ts"), "/ws", None))

    def test_install_and_network_blocked(self):
        self.assertIsNotNone(sh.redline_violation(
            self._action(commands=["pip install requests"]), "/ws", None))
        self.assertIsNotNone(sh.redline_violation(
            self._action(new_text="requests.get(url)"), "/ws", None))

    def test_path_escape_blocked(self):
        self.assertIsNotNone(sh.redline_violation(self._action(file="../x.py"), "/ws", None))
        self.assertIsNotNone(sh.redline_violation(self._action(file="/etc/passwd"), "/ws", None))

    def test_normal_patch_allowed(self):
        self.assertIsNone(sh.redline_violation(self._action(), "/ws", None))


class _Sink:
    """lesson 桩：record() 兼容现有 LessonStore 口径。"""

    def __init__(self):
        self.rows = []

    def record(self, tool, args, code, message):
        self.rows.append({"tool": tool, "code": code, "message": message,
                          "count": 1})

    def entries(self):
        return self.rows


class SelfHealEngineTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="heal-ws-")
        self.data = tempfile.mkdtemp(prefix="heal-data-")
        self.sink = _Sink()
        # 工作区里放一个可修的文件
        self.app = os.path.join(self.ws, "app.py")
        with open(self.app, "w", encoding="utf-8") as fh:
            fh.write("import json\n\n\ndef run():\n    return json.dumps({})\n")
        self.verifications = []

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def _engine(self, model_reply, verify=lambda f, c: (True, "ok")):
        def ask(prompt):
            self.prompt_seen = prompt
            return model_reply

        def verifier(path, commands):
            self.verifications.append((path, list(commands)))
            return verify(path, commands)

        return sh.SelfHealEngine(
            ask_model=ask, workspace_root=self.ws, run_verifier=verifier,
            lesson_sinks=[self.sink], data_dir=self.data,
            auto_apply=True, log=lambda *a, **k: None, run_id="run-test")

    def _facts(self, kind=et.KIND_TOOL_FAILURE, code="EXTRACT", **ctx):
        return et.ErrorFacts(kind=kind, source="tool:read_file", run_id="run-test",
                             session_id="s1", code=code, message="boom",
                             context=ctx or {"tool": "read_file",
                                             "argsSummary": "app.py"})

    def test_full_success_chain_applies_and_records_lesson(self):
        engine = self._engine(_action_json(
            file="app.py", old="import json", new="import json\nimport os"))
        out = engine.heal(self._facts())
        self.assertEqual(out.stage, "verified", out.failure_reason)
        self.assertTrue(out.applied)
        self.assertTrue(out.verified)
        with open(self.app, encoding="utf-8") as fh:
            self.assertIn("import os", fh.read())
        # 沙箱验证发生过（先沙箱后真实位置 = 2 次）
        self.assertEqual(len(self.verifications), 2)
        # 经验写进 sink（下次同类错误先查经验）
        self.assertTrue(self.sink.rows)
        self.assertEqual(self.sink.rows[0]["code"], "SELF_HEAL_OK")
        # 历史落盘（可查链路）
        with open(os.path.join(self.data, sh.HISTORY_FILENAME), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[0]["stage"], "verified")
        # 备份存在
        backups = os.path.join(self.data, "heal-backup")
        self.assertTrue(os.listdir(backups))

    def test_sandbox_failure_keeps_real_file_untouched(self):
        original = open(self.app, encoding="utf-8").read()
        engine = self._engine(
            _action_json(file="app.py", old="import json", new="import os"),
            verify=lambda f, c: (False, "AssertionError: x"))
        out = engine.heal(self._facts())
        self.assertEqual(out.stage, "failed")
        self.assertIn("沙箱验证失败", out.failure_reason)
        self.assertFalse(out.applied)
        self.assertEqual(open(self.app, encoding="utf-8").read(), original)
        # 失败原因记进 lessons
        self.assertEqual(self.sink.rows[0]["code"], "SELF_HEAL_FAIL")

    def test_model_garbage_fails_gracefully(self):
        engine = self._engine("模型说了一堆废话没有 JSON")
        out = engine.heal(self._facts())
        self.assertEqual(out.stage, "failed")
        self.assertIn("受约束动作", out.failure_reason)

    def test_redline_stops_before_anything(self):
        engine = self._engine(_action_json(
            file="config.json", old="a", new="b"))
        out = engine.heal(self._facts())
        self.assertEqual(out.stage, "stopped")
        self.assertIn("R2", out.failure_reason)
        self.assertFalse(self.verifications)     # 红线先于沙箱
        self.assertFalse(out.applied)

    def test_dependency_missing_skips_model_and_install(self):
        engine = self._engine(_action_json())
        out = engine.heal(self._facts(kind=et.KIND_DEPENDENCY_MISSING,
                                      code="DEP_MISSING", dependency="pdfplumber"))
        self.assertEqual(out.stage, "skipped")
        self.assertIn("pdfplumber", out.diagnosis)
        self.assertIn("pip install", out.diagnosis)   # 只提示，不装
        self.assertFalse(self.verifications)

    def test_same_error_budget_stops_after_n(self):
        engine = self._engine(_action_json())
        stages = []
        for _ in range(sh.SAME_ERROR_BUDGET + 1):
            out = engine.heal(self._facts(code="BUDGET"))
            stages.append(out.stage)
        self.assertEqual(stages[-1], "stopped")
        self.assertIn("停止自动尝试", out.failure_reason)

    def test_old_text_mismatch_fails_not_corrupts(self):
        engine = self._engine(_action_json(file="app.py", old="NOT IN FILE", new="x"))
        out = engine.heal(self._facts())
        self.assertEqual(out.stage, "failed")
        self.assertIn("old_text", out.failure_reason)

    def test_rollback_when_real_verify_fails(self):
        # 沙箱过、真实位置不过 → 自动回滚
        calls = {"n": 0}

        def verify(path, commands):
            calls["n"] += 1
            # 第 1 次是沙箱（过），第 2 次是真实位置（挂）
            return (calls["n"] == 1, "boom" if calls["n"] > 1 else "ok")

        engine = self._engine(_action_json(
            file="app.py", old="import json", new="import os"), verify=verify)
        out = engine.heal(self._facts())
        self.assertEqual(out.stage, "rolled_back")
        self.assertIn("回滚", out.rollback_reason + out.failure_reason)
        # 真实文件被恢复成原样
        with open(self.app, encoding="utf-8") as fh:
            self.assertNotIn("import os", fh.read())

    def test_engine_internal_exception_never_raises(self):
        def boom(prompt):
            raise RuntimeError("model exploded")

        engine = sh.SelfHealEngine(
            ask_model=boom, workspace_root=self.ws,
            lesson_sinks=[self.sink], data_dir=self.data,
            auto_apply=True, log=lambda *a, **k: None)
        out = engine.heal(self._facts())       # 不许抛
        self.assertEqual(out.stage, "failed")
        self.assertIn("自愈引擎内部异常", out.failure_reason)

    def test_heal_context_finds_prior_lesson(self):
        sink = _Sink()
        sink.rows.append({"tool": "self_heal", "target": "tool_failure/EXTRACT",
                          "count": 2, "remedy": "改 pages 参数就好"})
        f = self._facts()
        hit = sh.heal_context(f, [sink])
        self.assertIsNotNone(hit)
        self.assertIn("改 pages 参数就好", hit["suggestedAction"])
        self.assertIsNone(sh.heal_context(f, []))
        self.assertIsNone(sh.heal_context(self._facts(code="OTHER"), [sink]))

    def test_fenced_json_parsed(self):
        engine = self._engine("```json\n%s\n```" % _action_json(
            file="app.py", old="import json", new="import os"))
        out = engine.heal(self._facts())
        self.assertEqual(out.stage, "verified")


if __name__ == "__main__":
    unittest.main()
