# -*- coding: utf-8 -*-
"""t_7738a2bc：用户 deny 后模型死循环重复弹审批卡 —— 确定性拦截的回归测试。

覆盖验收四点：
  ① 同一指纹第 2 次起不再产生 approval.request；
  ② 出现 DENIED_REPEAT 错误码；
  ③ 达到 deny 上限（MAX_USER_DENIES）后本轮以面向用户的收尾文案正常结束；
  ④ run 不挂死（生成器正常走完 / 事件序列里有 chat.done）。
另补：换一个真正不同的方式（B）仍可正常请求审批，没有被一刀切误杀。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

import agent_loop as AL
from agent_loop import LoopContext, LoopDeps, run
from test_agent_loop import (FakeConfig, FakeDecision, FakeToolResult,
                             make_deps, scripted_llm, text_script, tool_script,
                             kinds)


def _download_script(url, out, call_id="call_1"):
    cmd = ("Invoke-WebRequest -Uri '%s' -OutFile '%s'" % (url, out))
    return tool_script("run_shell",
                       '{"command": %s}' % __import__("json").dumps(cmd),
                       call_id=call_id)


class DenyFingerprintTest(unittest.TestCase):
    def test_repeat_denied_call_is_intercepted_without_new_approval(self):
        # 模型连续 4 轮原样重提同一下载命令；用户每次都 deny。
        llm = scripted_llm([
            _download_script("https://example.com/hermes-agent.zip",
                             "C:\\Downloads\\hermes-agent.zip"),
        ] * 6)
        deps = make_deps(llm, decision=FakeDecision("dangerous", True),
                         approval="deny")
        ctx = LoopContext(messages=[{"role": "user", "content": "下载"}])
        events = list(run(ctx, deps=deps))

        approvals = [e for e in events if e["type"] == "approval.request"]
        # 第 1 次 deny 达 MAX_USER_DENIES=2 前只有第 1 弹；实际上第一次 deny
        # 记数 1，第二次同指纹直接 DENIED_REPEAT，不再弹卡 → 只弹 1 张卡。
        self.assertEqual(len(approvals), 1, kinds(events))

        repeats = [e for e in events if e.get("errorCode") == "DENIED_REPEAT"]
        self.assertTrue(repeats, "应出现 DENIED_REPEAT")

        done = [e for e in events if e["type"] == "chat.done"]
        errs = [e for e in events if e["type"] == "chat.error"]
        self.assertTrue(done or errs, "run 必须正常结束，不挂死")
        if not done:
            self.assertEqual(errs[0]["code"], "DENIED_REPEAT")

        deltas = "".join(e["text"] for e in events
                         if e["type"] == "chat.delta")
        if done:
            # 上限兜底路径（本例走的是连犯兜底路径时，没有收尾文案）
            self.assertIn("停下来等你的指示", deltas, "应有面向用户的收尾文案")

    def test_deny_cap_terminates_run_for_user(self):
        # 两条不同命令都被拒 → user_denies 到 2 → 收尾结束。
        json_mod = __import__("json")
        llm = scripted_llm([
            tool_script("run_shell", json_mod.dumps(
                {"command": "curl -o a.zip https://a.example.com/a.zip"})),
            tool_script("run_shell", json_mod.dumps(
                {"command": "curl -o b.zip https://b.example.com/b.zip"})),
        ] + [text_script("done")] * 3)
        deps = make_deps(llm, decision=FakeDecision("dangerous", True),
                         approval="deny")
        ctx = LoopContext(messages=[{"role": "user", "content": "下载"}])
        events = list(run(ctx, deps=deps))
        approvals = [e for e in events if e["type"] == "approval.request"]
        self.assertEqual(len(approvals), 2)
        self.assertIn("chat.done", kinds(events))
        deltas = "".join(e["text"] for e in events if e["type"] == "chat.delta")
        self.assertIn("已连续被用户拒绝 2 次", deltas)

    def test_different_action_still_gets_approval(self):
        # 拒掉 A 之后，换 URL + 换落盘文件的合法 B 仍能正常弹卡（不误杀）。
        json_mod = __import__("json")
        llm = scripted_llm([
            tool_script("run_shell", json_mod.dumps(
                {"command": "Invoke-WebRequest 'https://x.example.com/old.zip' -OutFile 'C:/D/old.zip'"})),
            tool_script("run_shell", json_mod.dumps(
                {"command": "Invoke-WebRequest 'https://y.example.com/other.md' -OutFile 'C:/D/other.md'"})),
            text_script("好了"),
        ])
        approvals_seen = []

        def request_approval(payload):
            approvals_seen.append(payload)
            return "deny" if len(approvals_seen) == 1 else "allow_once"

        deps = make_deps(llm, decision=FakeDecision("dangerous", True))
        deps.request_approval = request_approval
        ctx = LoopContext(messages=[{"role": "user", "content": "下载"}])
        events = list(run(ctx, deps=deps))
        approvals = [e for e in events if e["type"] == "approval.request"]
        self.assertEqual(len(approvals), 2, "B 没被一刀切误杀")
        self.assertEqual(len(deps._execs), 1, "B 应真的执行了")

    def test_equivalent_rewrite_is_caught(self):
        # 同 URL、写法/参数顺序/大小写不同 → 仍算同指纹（url: 等价类命中）。
        json_mod = __import__("json")
        llm = scripted_llm([
            tool_script("run_shell", json_mod.dumps(
                {"command": "Invoke-WebRequest -Uri 'https://ex.com/pack.zip' -OutFile 'C:/D/pack.zip'"})),
            tool_script("run_shell", json_mod.dumps(
                {"command": "invoke-webrequest -outFile 'c:\\d\\PACK.zip' -uri 'HTTPS://EX.com/pack.zip'"})),
            text_script("好了"),
        ])
        approvals_seen = []

        def request_approval(payload):
            approvals_seen.append(payload)
            return "deny" if len(approvals_seen) == 1 else "allow_once"

        deps = make_deps(llm, decision=FakeDecision("dangerous", True))
        deps.request_approval = request_approval
        ctx = LoopContext(messages=[{"role": "user", "content": "下载"}])
        events = list(run(ctx, deps=deps))
        approvals = [e for e in events if e["type"] == "approval.request"]
        self.assertEqual(len(approvals), 1, "等价重写也该被拦，不弹第二张卡")
        repeats = [e for e in events if e.get("errorCode") == "DENIED_REPEAT"]
        self.assertTrue(repeats)

    def test_denied_repeat_loop_terminates_with_error(self):
        # 模型无视 DENIED_REPEAT 连续重提（用户已经 deny 过 1 次后指纹命中）：
        # 撞 MAX_DENIED_REPEATS 后以 DENIED_REPEAT 错误终止本轮，不无限烧循环。
        # 注：单次 deny 未到 MAX_USER_DENIES=2，所以循环靠 denied_repeat_hits 兜底。
        json_mod = __import__("json")
        cmd = json_mod.dumps({"command": "Invoke-WebRequest 'https://e.com/f.zip'"})
        # 第 1 条：deny（记指纹）；之后同命令重提若干次。
        llm = scripted_llm([tool_script("run_shell", cmd)] + [
            tool_script("run_shell", cmd, call_id="call_%d" % i)
            for i in range(2, 8)
        ] + [text_script("好了")])
        # 只 deny 第一次，之后即使弹卡也 allow —— 但不应再弹。
        approvals_seen = []

        def request_approval(payload):
            approvals_seen.append(payload)
            return "deny" if len(approvals_seen) == 1 else "allow_once"

        deps = make_deps(llm, decision=FakeDecision("dangerous", True))
        deps.request_approval = request_approval
        ctx = LoopContext(messages=[{"role": "user", "content": "下载"}])
        events = list(run(ctx, deps=deps))
        self.assertEqual(len(approvals_seen), 1)
        errs = [e for e in events if e["type"] == "chat.error"
                and e.get("code") == "DENIED_REPEAT"]
        self.assertTrue(errs, "连犯兜底应触发 DENIED_REPEAT 终止")


if __name__ == "__main__":
    unittest.main()
