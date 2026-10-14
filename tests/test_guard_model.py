"""guard 第二层（模型提示层）接线测试：2026-10-01。

覆盖：
  * 纯逻辑 manager（注入 spawn/clock/kill）：respawn、空闲 reap、
    spawn 两次失败后标记不可用、请求超时/死亡的处理。
  * 模型层判定档位（p → allow/ask/conservative）与阈值配置覆盖。
  * 两段式判定：正则命中不调模型、正则放行走模型、模型层不可用
    fail-open 回纯正则。
  * 输入契约逐字一致（[工具] run_shell [目录] /home/user [命令] …）。

真实子进程 / 真实模型的端到端跑在本机验证脚本里做（见
tests/test_guard_model_live.py，模型文件在盘才收集）。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import guard  # noqa: E402
import guard_model  # noqa: E402
import guard_model_proc  # noqa: E402
from guard_model_proc import (  # noqa: E402
    GuardModelProcess,
    SPAWN_MAX_ATTEMPTS,
)


class FakeChild:
    """协议假件：write_line 记录、read_line 按脚本回话。

    alive=False 模拟「进程还在但 stdout 已关/不回话」之外的两种死法：
    spawn 出来就死（is_up 为 False）与写请求时崩。
    """

    def __init__(self, replies=None, alive=True):
        self.written = []
        self.replies = list(replies or [])
        self._alive = alive
        self.killed = False

    @property
    def alive(self):
        return self._alive and not self.killed

    def write_line(self, text):
        if not self.alive:
            raise guard_model_proc.GuardModelProcessDeadError(
                "child died before write")
        self.written.append(text)

    def read_line(self, timeout):
        if not self.alive:
            return None
        if self.replies:
            return self.replies.pop(0)
        return None

    def kill(self):
        self.killed = True


class ProcessManagerTests(unittest.TestCase):
    """纯逻辑 manager：注入 spawn/clock/kill，零子进程。"""

    def test_ensure_up_spawns_once_and_reuses(self):
        spawns = []

        def spawn():
            spawns.append(1)
            return FakeChild()

        proc = GuardModelProcess(spawn)
        self.assertTrue(proc.ensure_up())
        self.assertFalse(proc.ensure_up(), "已活着不该再 spawn")
        self.assertEqual(len(spawns), 1)

    def test_spawn_failure_marks_unavailable_after_max_attempts(self):
        attempts = []

        def spawn():
            attempts.append(1)
            raise RuntimeError("no model file")

        proc = GuardModelProcess(spawn)
        self.assertFalse(proc.ensure_up())
        self.assertTrue(proc.is_unavailable)
        self.assertEqual(proc.spawn_attempt_count, SPAWN_MAX_ATTEMPTS)
        # 不可用之后：不再尝试
        self.assertFalse(proc.ensure_up())
        self.assertEqual(proc.spawn_attempt_count, SPAWN_MAX_ATTEMPTS)

    def test_dead_child_respawns_on_next_ensure_up(self):
        holder = {}

        def spawn():
            return FakeChild()

        proc = GuardModelProcess(spawn)
        proc.ensure_up()
        first = proc._child
        first.kill()
        self.assertFalse(proc.is_up)
        self.assertTrue(proc.ensure_up(), "死掉后要能重起")
        self.assertIsNot(proc._child, first)

    def test_idle_reap_kills_child(self):
        now = {"t": 0.0}
        child = FakeChild()
        proc = GuardModelProcess(lambda: child, clock=lambda: now["t"])
        proc.ensure_up()
        now["t"] = 100.0
        self.assertFalse(proc.maybe_reap(), "100s 不够 600s 阈值")
        now["t"] = 700.0
        self.assertTrue(proc.maybe_reap())
        self.assertTrue(child.killed)
        self.assertFalse(proc.maybe_reap(), "没有第二个孩子可杀")

    def test_request_roundtrip_and_touch(self):
        child = FakeChild(replies=['{"ok": true, "p": 0.5}'])
        now = {"t": 0.0}
        proc = GuardModelProcess(lambda: child, clock=lambda: now["t"])
        proc.ensure_up()
        reply = proc.request({"text": "x"}, 1.0)
        self.assertEqual(reply["p"], 0.5)
        self.assertIn('{"text": "x"}', child.written[0])
        # request 内部 touch 过（_last_used=0），把表拨到 100s：仍在 600s
        # 闲置阈值内，不该 reap；要测「用过之后重新计闲置」正确写法是先
        # 拨过一个完整阈值再验证 —— 那是 test_idle_reap 的事，这里只验
        # touch 重置了计时起点。
        now["t"] = 100.0
        self.assertFalse(proc.maybe_reap(), "用过之后要重新计闲置")

    def test_request_timeout_disposes_child(self):
        """活着的 child 不回话 → 超时（与 memory_proc 同语义：只把没死的
        child 不回话归为 timeout，死了才是 dead）。"""
        child = FakeChild(replies=[])   # read_line → None，但 child 还活着
        proc = GuardModelProcess(lambda: child)
        proc.ensure_up()
        with self.assertRaises(guard_model_proc.GuardModelTimeoutError):
            proc.request({"text": "x"}, 0.01)
        self.assertFalse(proc.is_up)

    def test_dead_child_before_reply_is_dead_error(self):
        """回话前死掉 → GuardModelProcessDeadError（与 memory_proc 一致）。"""
        child = FakeChild(replies=[], alive=False)
        proc = GuardModelProcess(lambda: child)
        proc.ensure_up()
        with self.assertRaises(guard_model_proc.GuardModelProcessDeadError):
            proc.request({"text": "x"}, 1.0)
        self.assertFalse(proc.is_up)

    def test_malformed_reply_disposes_child(self):
        child = FakeChild(replies=["not json at all"])
        proc = GuardModelProcess(lambda: child)
        proc.ensure_up()
        with self.assertRaises(guard_model_proc.GuardModelError):
            proc.request({"text": "x"}, 1.0)
        self.assertFalse(proc.is_up)


class ModelVerdictTests(unittest.TestCase):
    """模型层的角色（2026-10-14 用户口径改）：**默认只观察，不当闸门**。

    旧口径（p >= 0.85 高置信弹卡 / 0.35 <= p < 0.85 没把握也弹卡）今天被推翻：
    用户实测「读文档 + 画图」跑的一串普通命令被模型层条条判成可疑，十几张卡
    还出不了图。现在弹不弹卡完全由两层正则决定，模型层只记录 p（未知池回流仍在）。
    config.json 的 guard.modelGate=true / DESK_GUARD_MODEL_GATE=1 才恢复旧行为 ——
    下面把老口径也钉住，免得开关哪天失灵没人发现。
    """

    def _judge(self, command, p, thresholds=None, gate=False):
        import os

        class FakeModel:
            def predict(self, command, **kw):
                return {"p": p, "level": 1, "cost": 1}

        old = os.environ.get("DESK_GUARD_MODEL_GATE")
        os.environ["DESK_GUARD_MODEL_GATE"] = "1" if gate else "0"
        try:
            return guard.judge_command_layered(
                command, model=FakeModel(), thresholds=thresholds or
                {"confident": 0.85, "ask": 0.35})
        finally:
            if old is None:
                os.environ.pop("DESK_GUARD_MODEL_GATE", None)
            else:
                os.environ["DESK_GUARD_MODEL_GATE"] = old

    # -- 默认口径：只观察，不弹卡 -------------------------------------------

    def test_model_never_gates_by_default(self):
        """模型说危险也不算数 —— 这正是用户报的噪音源。"""
        for p in (0.97, 0.85, 0.5, 0.351):
            v = self._judge("Get-Content 报告.md", p=p)
            self.assertFalse(v.requires_approval, "p=%s 不该弹卡" % p)
            self.assertEqual(v.tier, "")
            self.assertEqual(v.pattern, "model-observe")

    def test_observe_verdict_has_no_user_facing_internals(self):
        """t_87ce5f71：面向用户的话里不许有 p=/阈值/弹卡这类内部术语。"""
        v = self._judge("Get-Content 报告.md", p=0.97)
        self.assertEqual(v.reason, "")

    def test_gate_defaults_to_off(self):
        import os
        old = os.environ.get("DESK_GUARD_MODEL_GATE")
        os.environ["DESK_GUARD_MODEL_GATE"] = "0"
        try:
            self.assertFalse(guard.model_gate_enabled())
        finally:
            if old is None:
                os.environ.pop("DESK_GUARD_MODEL_GATE", None)
            else:
                os.environ["DESK_GUARD_MODEL_GATE"] = old

    def test_low_p_allows_with_unknown_pool(self):
        v = self._judge("ls -la", p=0.10)
        self.assertFalse(v.requires_approval)
        self.assertIn("未知池", v.reason)

    # -- 老口径：只有显式开闸才成立（回归保护）-------------------------------

    def test_gate_on_restores_high_p_block(self):
        v = self._judge("find . -delete", p=0.97, gate=True)
        self.assertTrue(v.requires_approval)
        self.assertEqual(v.tier, "dangerous")
        # t_87ce5f71：面向用户的 reason 不再含 p=…/高置信 等内部术语
        self.assertIn("危险等级：高", v.reason)
        for term in ("弹卡", "校准", "概率", "p=", "0.97", "0.85",
                     "高置信", "很确定", "不确定带", "建议"):
            self.assertNotIn(term, v.reason)

    def test_gate_on_restores_mid_p_block(self):
        v = self._judge("git commit -m \"docs: 修 rm -rf 说明\"", p=0.5, gate=True)
        self.assertTrue(v.requires_approval)
        self.assertEqual(v.tier, "dangerous")
        self.assertIn("危险等级：中", v.reason)
        for term in ("弹卡", "校准", "概率", "p=", "0.5",
                     "高置信", "很确定", "不确定带", "建议"):
            self.assertNotIn(term, v.reason)

    def test_gate_on_boundaries(self):
        self.assertTrue(self._judge("x", p=0.90, gate=True).requires_approval)
        self.assertTrue(self._judge("x", p=0.85, gate=True).requires_approval)
        self.assertTrue(self._judge("x", p=0.849, gate=True).requires_approval)
        self.assertTrue(self._judge("x", p=0.35, gate=True).requires_approval)
        v = self._judge("x", p=0.349, gate=True)
        self.assertFalse(v.requires_approval)
        self.assertIn("未知池", v.reason)

    def test_gate_on_thresholds_configurable(self):
        v = self._judge("x", p=0.5, thresholds={"confident": 0.40, "ask": 0.10},
                        gate=True)
        self.assertTrue(v.requires_approval, "0.5 >= 0.40 → 弹卡（高置信）")
        v = self._judge("x", p=0.05, thresholds={"confident": 0.40, "ask": 0.10},
                        gate=True)
        self.assertFalse(v.requires_approval)
        self.assertIn("未知池", v.reason)


class LayeringTests(unittest.TestCase):
    """两段式：第一层正则硬闸不动，第二层只对放行的 run_shell 跑。"""

    def test_hardline_never_calls_model(self):
        calls = []

        class Spy:
            def predict(self, command, **kw):
                calls.append(command)
                return {"p": 0.99, "level": 3, "cost": 2}

        v = guard.judge_command_layered("rm -rf /", model=Spy())
        self.assertEqual(v.tier, "hardline")
        self.assertEqual(calls, [], "正则命中绝不调模型")

    def test_dangerous_never_calls_model(self):
        calls = []

        class Spy:
            def predict(self, command, **kw):
                calls.append(command)
                return {"p": 0.99, "level": 3, "cost": 2}

        v = guard.judge_command_layered("rm -rf ./build", model=Spy())
        self.assertEqual(v.tier, "dangerous")
        self.assertEqual(calls, [])

    def test_regex_pass_then_model_runs(self):
        calls = []

        class Spy:
            def predict(self, command, **kw):
                calls.append(command)
                return None

        v = guard.judge_command_layered("ls -la", model=Spy())
        self.assertEqual(calls, ["ls -la"])
        self.assertFalse(v.requires_approval, "模型 None → 正则放行")

    def test_model_unavailable_fail_open(self):
        class Dead:
            def predict(self, command, **kw):
                return None

        for cmd in ("ls -la", "git status", "echo hello"):
            v = guard.judge_command_layered(cmd, model=Dead())
            self.assertFalse(v.requires_approval, cmd)

    def test_model_raises_fail_open(self):
        class Exploding:
            def predict(self, command, **kw):
                raise RuntimeError("boom")

        v = guard.judge_command_layered("ls -la", model=Exploding())
        self.assertFalse(v.requires_approval)

    def test_judge_tool_entry_unchanged_without_model(self):
        """不装模型时 guard.judge 与旧版逐字一致（纯正则）。"""
        self.assertEqual(guard.judge("run_shell",
                                     {"command": "rm -rf /"}).tier,
                         "hardline")
        self.assertFalse(guard.judge("run_shell",
                                     {"command": "ls -la"}).requires_approval)

    def test_input_contract_format(self):
        """[工具] run_shell [目录] /home/user [命令] {cmd} 逐字一致。"""
        seen = []

        class Spy:
            def predict(self, command, **kw):
                seen.append(kw.get("text"))
                return None

        guard.judge_command_layered("ls -la", model=Spy(),
                                    text_builder=guard_model._make_text)
        self.assertEqual(seen, ["[工具] run_shell [目录] /home/user [命令] ls -la"])


class DefaultModelSingletonTests(unittest.TestCase):
    """guard 里懒加载的默认模型实例：没有模型文件时不炸。"""

    def test_get_model_returns_none_without_files(self):
        # 指到不存在路径：不可用、不抛异常
        m = guard_model.GuardModel(
            model_path="/nonexistent/x.onnx",
            calibrator_path="/nonexistent/c.json",
            tokenizer_path="/nonexistent/t.json")
        self.assertIsNone(m.predict("ls -la"))
        self.assertTrue(m.is_unavailable)

    def test_threshold_defaults(self):
        t = guard_model._read_thresholds()
        self.assertEqual(t, {"confident": 0.85, "ask": 0.35})


class TimeoutObservabilityTests(unittest.TestCase):
    """2026-10-01 冷启动静默 fail-open 修复的回归：超时必须可观测。

    旧代码 predict 超时（活 child 不回话）静默返回 None —— 冷页缓存
    机型（fp32 冷启动 5.89s > 旧 5.0s 阈值）第一波命令一声不响地丢失
    模型层。现在：超时 → degradation_reason="timeout" + 日志行；
    起不来 → "unavailable"（同样留痕但语义不同）。
    """

    def setUp(self):
        self._old_hook = guard_model._LOG_HOOK
        self.lines = []
        guard_model.set_log_hook(
            lambda level, msg: self.lines.append((level, msg)))

    def tearDown(self):
        guard_model.set_log_hook(self._old_hook)

    def _model_with_child(self, child):
        """真 GuardModel + 假 child（不依赖真模型文件/子进程）。"""
        m = guard_model.GuardModel(
            model_path="/nonexistent/x.onnx",
            calibrator_path="/nonexistent/c.json",
            tokenizer_path="/nonexistent/t.json")
        m._proc = guard_model_proc.GuardModelProcess(lambda: child)
        return m

    def test_default_timeout_is_15s(self):
        """盖住最慢冷启动（fp32 406MB 冷缓存 5.89s）再留余量。"""
        self.assertEqual(guard_model.DEFAULT_TIMEOUT, 15.0)
        import inspect
        sig = inspect.signature(guard_model.GuardModel.predict)
        self.assertEqual(sig.parameters["timeout"].default, 15.0)

    def test_timeout_returns_none_with_log_and_reason(self):
        """活 child 不回话 → None + reason="timeout" + warn 日志行。"""
        child = FakeChild(replies=[])          # 活着，但永不回话
        m = self._model_with_child(child)
        out = m.predict("ls -la", timeout=0.01)
        self.assertIsNone(out)
        self.assertEqual(m.degradation_reason, "timeout")
        self.assertTrue(any(lvl == "warn" for lvl, _ in self.lines),
                        "超时必须有 warn 级日志：%r" % (self.lines,))
        self.assertTrue(any("timed out" in msg for _, msg in self.lines),
                        "日志要写明超时：%r" % (self.lines,))

    def test_timeout_log_goes_to_stderr_prefix_by_default(self):
        """默认路径（没设 log hook）写 stderr 的 ``[guard-model warn]`` 行。"""
        guard_model.set_log_hook(None)
        child = FakeChild(replies=[])
        m = self._model_with_child(child)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            m.predict("ls -la", timeout=0.01)
        err = buf.getvalue()
        self.assertIn("[guard-model warn]", err)
        self.assertIn("timed out", err)
        self.assertIn("timeout", err)

    def test_unavailable_reason_on_missing_files(self):
        """缺文件 → "unavailable"（与超时区分开），日志带原因。"""
        m = guard_model.GuardModel(
            model_path="/nonexistent/x.onnx",
            calibrator_path="/nonexistent/c.json",
            tokenizer_path="/nonexistent/t.json")
        self.assertIsNone(m.predict("ls -la"))
        self.assertEqual(m.degradation_reason, "unavailable")
        self.assertTrue(any("not found" in msg for _, msg in self.lines),
                        "%r" % (self.lines,))

    def test_dead_child_is_unavailable_not_timeout(self):
        """死 child（写请求时崩）→ DeadError → "unavailable"，不是超时。

        钉住 2026-10-01 已修的分支语义：活 child 不回话才是 timeout。
        """
        class DyingChild(FakeChild):
            def write_line(self, text):
                self._alive = False          # 写请求这一刻死掉
                return super().write_line(text)

        m = self._model_with_child(DyingChild(replies=[]))
        self.assertIsNone(m.predict("ls -la"))
        self.assertEqual(m.degradation_reason, "unavailable")

    def test_success_clears_degradation_then_timeout_resets_it(self):
        child = FakeChild(replies=[
            '{"ok": true, "ready": true}',
            '{"ok": true, "p": 0.023, "level": 0, "cost": 0}',
        ])
        m = self._model_with_child(child)
        out = m.predict("ls -la")
        self.assertIsNotNone(out)
        self.assertEqual(out["p"], 0.023)
        self.assertIsNone(m.degradation_reason, "成功推理清掉降级标记")
        # 应答耗尽后再来一条：活着但不回话 → timeout
        self.assertIsNone(m.predict("ls -la", timeout=0.01))
        self.assertEqual(m.degradation_reason, "timeout")


class WarmupTests(unittest.TestCase):
    """启动预热（GuardModel.warmup / guard.warmup）回归，全部假件。"""

    def setUp(self):
        self._old_hook = guard_model._LOG_HOOK
        guard_model.set_log_hook(lambda level, msg: None)
        self._old_default = guard._default_model
        self._old_env = os.environ.get("DESK_GUARD_MODEL_LAYER")
        os.environ.pop("DESK_GUARD_MODEL_LAYER", None)

    def tearDown(self):
        guard_model.set_log_hook(self._old_hook)
        guard._default_model = self._old_default
        if self._old_env is None:
            os.environ.pop("DESK_GUARD_MODEL_LAYER", None)
        else:
            os.environ["DESK_GUARD_MODEL_LAYER"] = self._old_env

    def _model_with_child(self, child):
        m = guard_model.GuardModel(
            model_path="/nonexistent/x.onnx",
            calibrator_path="/nonexistent/c.json",
            tokenizer_path="/nonexistent/t.json")
        m._proc = guard_model_proc.GuardModelProcess(lambda: child)
        return m

    def test_guard_model_warmup_runs_dummy_inference(self):
        """warmup = spawn + 一条哑推理（"warmup ping"），不占关键路径。"""
        child = FakeChild(replies=[
            '{"ok": true, "ready": true}',
            '{"ok": true, "p": 0.01, "level": 0, "cost": 0}',
        ])
        m = self._model_with_child(child)
        out = m.warmup()
        self.assertIsNotNone(out)
        self.assertEqual(out["p"], 0.01)
        # 发出去的请求确实带着哑命令
        self.assertTrue(any("warmup ping" in w for w in child.written),
                        "%r" % (child.written,))

    def test_guard_model_warmup_fails_silently(self):
        """缺模型文件时 warmup 返回 None，不抛、不留半起的子进程。"""
        m = guard_model.GuardModel(
            model_path="/nonexistent/x.onnx",
            calibrator_path="/nonexistent/c.json",
            tokenizer_path="/nonexistent/t.json")
        self.assertIsNone(m.warmup())
        self.assertFalse(m.is_up)

    def test_guard_warmup_module_level_uses_shared_singleton(self):
        child = FakeChild(replies=[
            '{"ok": true, "ready": true}',
            '{"ok": true, "p": 0.01, "level": 0, "cost": 0}',
        ])
        m = self._model_with_child(child)
        guard._default_model = m
        self.assertTrue(guard.warmup())
        # 预热用的是共享单例：下一次判定直接热路径
        self.assertIs(guard._get_default_model(), m)

    def test_guard_warmup_skipped_when_layer_disabled(self):
        """DESK_GUARD_MODEL_LAYER=0：warmup 直接跳过，不碰模型。"""
        hits = []

        class Boom:
            def warmup(self):
                hits.append(1)
                raise AssertionError("模型层关了就不该预热")

        guard._default_model = Boom()
        os.environ["DESK_GUARD_MODEL_LAYER"] = "0"
        self.assertFalse(guard.warmup())
        self.assertEqual(hits, [])

    def test_guard_warmup_skipped_when_disabled_in_config(self):
        """config guard.model_warmup=false：只关预热，不关模型层。"""
        import appconfig
        old_cap = appconfig.capability
        appconfig.capability = lambda name, default=None, **kw: (
            {"model_warmup": False} if name == "guard" else default)
        try:
            hits = []

            class Boom:
                def warmup(self):
                    hits.append(1)
                    raise AssertionError("配置关了预热就不该跑")

            guard._default_model = Boom()
            self.assertFalse(guard.warmup())
            self.assertEqual(hits, [])
            # 模型层本身仍开（judge 路径不受影响）
            self.assertTrue(guard.model_layer_enabled())
        finally:
            appconfig.capability = old_cap

    def test_guard_warmup_returns_false_and_raises_nothing(self):
        """预热内部任何异常都被吞掉（上层只需 warn，不许崩启动）。"""
        class Boom:
            def warmup(self):
                raise RuntimeError("warmup exploded")

        guard._default_model = Boom()
        self.assertFalse(guard.warmup())     # 不抛即为通过


if __name__ == "__main__":
    unittest.main()
