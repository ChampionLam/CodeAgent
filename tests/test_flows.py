"""项目会话的「流程」：一问一答往下走 + **异常值怎么处理**。

用户 2026-10-09 口径：项目会话一建好就该像助手真在聊——先问「新建还是接入已有的」，
不同选择走不同的路，问到齐了再整理成开场任务；**重点是别把用户卡在半路**，
他答别的、答错、干脆不按剧本走，都要有出口。

所以这里钉四类异常：
  1. 选项步骤收到对不上任何选项的话 → off_script（流程收尾，交给模型，不硬拦）
  2. 输入步骤答得明显不像 → retry（给「我重说 / 就按这个来」两条路，不前进不写库）
  3. 必填留空 → retry（同一套出口，别死循环）
  4. 校验不通过但用户坚持（force）→ 收下往前走
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "python"))

import flows  # noqa: E402
import sidecar  # noqa: E402

FLOW_ID = "project-setup"


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = sidecar._DATA_DIR
        self._old_store = getattr(sidecar, "_SESSIONS", None)
        sidecar._DATA_DIR = self._tmp.name
        sidecar._SESSIONS = None
        self.flow = flows.get_flow(FLOW_ID)

    def tearDown(self):
        sidecar._SESSIONS = self._old_store
        sidecar._DATA_DIR = self._old_dir
        self._tmp.cleanup()

    def _req(self, method, **params):
        params.setdefault("protocolVersion", sidecar.SIDECAR_PROTOCOL_VERSION)
        return sidecar.handle_request({"type": "req", "id": "f1",
                                       "method": method, "params": params})

    def _ok(self, resp):
        self.assertTrue(resp.get("ok"), "RPC 没成功：%r" % (resp,))
        return resp.get("result") or {}

    def _err(self, resp):
        self.assertFalse(resp.get("ok"), "本该报错却成功了：%r" % (resp,))
        err = resp.get("error") or {}
        return err.get("code"), err.get("message")

    def _project(self, **kw) -> str:
        kw.setdefault("kind", "project")
        kw.setdefault("projectName", "流程用例")
        return self._ok(self._req("sessions.create", **kw))["id"]

    def _messages(self, sid) -> list:
        return self._ok(self._req("sessions.messages", sessionId=sid))["messages"]


class DefinitionTest(_Base):
    def test_repo_flow_loads_and_is_valid(self):
        got = flows.load_definitions()
        self.assertIn(FLOW_ID, got, "仓里的 project-setup 流程没被加载")
        f = got[FLOW_ID]
        self.assertEqual("mode", f["start"])
        mode = f["_by_id"]["mode"]["options"]
        # 标签说人话（短），值才是落库/进提示词的正式说法 —— 两套别混。
        self.assertEqual(["新建项目", "接入现有项目"], [o["label"] for o in mode])
        self.assertEqual(["新建项目", "接入现有项目"], [o["value"] for o in mode])
        # 新建那条先问项目名称（2026-10-09 用户要求：对话里问名字，不弹表单）。
        self.assertEqual("name", mode[0]["goto"])
        self.assertEqual(["mode", "name", "newdir", "dir", "goal", "openspec", "done"],
                         [s["id"] for s in f["steps"]])
        # 技术栈不在这套里问（2026-10-09 用户：不着急、不是重点）。
        self.assertNotIn("技术栈", [s.get("input", {}).get("save_as") for s in f["steps"]])

    def test_broken_definition_is_rejected_not_silently_kept(self):
        bad = {"id": "x", "start": "a", "steps": [{"id": "a", "say": "？",
               "options": [{"label": "L", "value": "v", "goto": "nowhere"}]}]}
        with self.assertRaises(flows.FlowError):
            flows._validate(bad, "tmp")
        # 也有"两个出口都有"这种必须拦的写法
        with self.assertRaises(flows.FlowError):
            flows._validate({"id": "y", "start": "a", "steps": [
                {"id": "a", "say": "？", "goto": "a",
                 "options": [{"label": "L", "value": "v", "goto": "a"}]}]}, "tmp")

    def test_missing_dir_in_definitions_is_not_an_error(self):
        self.assertEqual({}, flows.load_definitions(os.path.join(self._tmp.name, "nope")))

    def test_blank_greeting_is_rejected(self):
        bad = {"id": "x", "start": "a", "greeting": "   ",
               "steps": [{"id": "a", "say": "问一句？", "input": {"save_as": "A"}, "end": True}]}
        with self.assertRaises(flows.FlowError):
            flows._validate(bad, "x")

    def test_unknown_flow(self):
        with self.assertRaises(flows.FlowError):
            flows.get_flow("no-such-flow")


class BranchTest(_Base):
    def _advance(self, **kw):
        return flows.advance(self.flow, **kw)

    def test_start_asks_the_first_question(self):
        out = self._advance(step_id=None, answer=None)
        self.assertEqual("ask", out["kind"])
        self.assertEqual("mode", out["step"]["id"])

    def test_start_carries_a_greeting_but_later_steps_do_not(self):
        start = self._advance(step_id=None, answer=None)
        self.assertTrue(start.get("greeting"), "起流程要带一句开场白")
        nxt = self._advance(step_id="mode", answer="新建项目")
        self.assertFalse(nxt.get("greeting"), "走到第二步就不该再招呼一遍")

    def test_new_project_asks_the_name_first(self):
        out = self._advance(step_id="mode", answer="新建项目")
        self.assertEqual("name", out["step"]["id"])

    def test_name_answer_is_remembered_then_dir(self):
        out = self._advance(step_id="name", answer="数据看板", answers={})
        self.assertEqual("newdir", out["step"]["id"], "名称定完就该定目录")
        self.assertEqual("数据看板", out["answers"]["项目名称"])

    def test_new_dir_then_goal(self):
        out = self._advance(step_id="newdir", answer=r"D:\work\data-dashboard", answers={})
        self.assertEqual("goal", out["step"]["id"])
        self.assertEqual(r"D:\work\data-dashboard", out["answers"]["项目目录"])

    def test_existing_project_goes_to_dir(self):
        out = self._advance(step_id="mode", answer="接入现有项目")
        self.assertEqual("dir", out["step"]["id"])

    def test_option_label_also_works(self):
        out = self._advance(step_id="mode", answer="新建项目")
        self.assertEqual("name", out["step"]["id"])

    def test_input_answer_is_remembered(self):
        out = self._advance(step_id="goal", answer="做个周报机器人",
                            answers={})
        self.assertEqual("openspec", out["step"]["id"])
        self.assertEqual("做个周报机器人", out["answers"]["项目目标"])

    def test_end_step_produces_prompt(self):
        out = self._advance(step_id="openspec", answer="用 OpenSpec：先写改动提案，我确认后再实现",
                            answers={"项目名称": "数据看板", "项目目标": "做个周报机器人",
                                     "项目目录": r"D:\work\data-dashboard"})
        self.assertEqual("done", out["kind"])
        self.assertIn("做个周报机器人", out["prompt"])
        self.assertIn(r"D:\work\data-dashboard", out["prompt"])
        self.assertIn("别猜", out["prompt"])


class ExceptionPathTest(_Base):
    """异常值——这套流程的重点（用户点名要）。"""

    def _advance(self, **kw):
        return flows.advance(self.flow, **kw)

    def test_off_script_option_answer_is_not_an_error(self):
        out = self._advance(step_id="mode", answer="你帮我看看我那个仓库要不要重写")
        self.assertEqual("off_script", out["kind"])
        self.assertIn("没在我们给的选项里", out["reason"])

    def test_dir_answer_that_is_not_a_path_asks_again(self):
        out = self._advance(step_id="dir", answer="我也忘了在哪")
        self.assertEqual("retry", out["kind"])
        self.assertIn("不像个目录路径", out["warn"])
        labels = [o["label"] for o in out["options"]]
        self.assertEqual(["我重说", "就按这个来"], labels)

    def test_force_lets_the_user_push_through(self):
        out = self._advance(step_id="dir", answer="我也忘了在哪", force=True)
        self.assertEqual("ask", out["kind"])
        self.assertEqual("openspec", out["step"]["id"])
        self.assertEqual("我也忘了在哪", out["answers"]["项目目录"])

    def test_required_answer_left_empty_asks_again(self):
        out = self._advance(step_id="goal", answer="   ")
        self.assertEqual("retry", out["kind"])
        self.assertIn("我重说", [o["label"] for o in out["options"]])

    def test_path_shaped_answer_passes(self):
        out = self._advance(step_id="dir", answer=r"D:\work\repo")
        self.assertEqual("ask", out["kind"])
        self.assertEqual(r"D:\work\repo", out["answers"]["项目目录"])

    def test_off_script_handoff_summarises_what_we_know(self):
        text = flows.off_script_handoff(self.flow, {"项目目标": "做周报"},
                                        last_question="新建还是接入已有的？")
        self.assertIn("做周报", text)
        self.assertIn("新建还是接入已有的？", text)


class BoundaryTest(_Base):
    """边界与异常（2026-10-09 用户要求专门补的一批）。

    这一批盯的是"人不会老实答题"的那几处：空着不答、答得不像、答得太长、
    客气话对不上选项。**断言的是引擎的既定行为**，界面怎么收拾（截断/去引号）
    在 ui/src/state/store.tsx 的 tidyProjectName，别混到这里来。
    """

    def _advance(self, **kw):
        return flows.advance(self.flow, **kw)

    def test_name_step_is_required(self):
        out = self._advance(step_id="name", answer="   ")
        self.assertEqual("retry", out["kind"], "名字要拿来当标题，不能空着往下走")
        self.assertEqual("name", out["step"]["id"])
        self.assertIn("说一句", out["warn"])

    def test_name_is_trimmed_when_kept(self):
        out = self._advance(step_id="name", answer="  数据看板  ", answers={})
        self.assertEqual("ask", out["kind"])
        self.assertEqual("数据看板", out["answers"]["项目名称"])

    def test_overlong_name_is_kept_verbatim_by_engine(self):
        long_name = "报表" * 40
        out = self._advance(step_id="name", answer=long_name, answers={})
        self.assertEqual(long_name, out["answers"]["项目名称"],
                         "引擎不截断；截断成标题是界面的事")

    def test_quoted_name_is_kept_verbatim_by_engine(self):
        out = self._advance(step_id="name", answer="「数据看板」", answers={})
        self.assertEqual("「数据看板」", out["answers"]["项目名称"])

    def test_new_dir_must_look_like_a_path(self):
        out = self._advance(step_id="newdir", answer="就放我电脑里吧")
        self.assertEqual("retry", out["kind"])
        self.assertIn("不像个目录路径", out["warn"])
        self.assertEqual(["我重说", "就按这个来"], [o["label"] for o in out["options"]])

    def test_quoted_path_and_unc_path_pass(self):
        for answer, nxt in ((r'"D:\work\x y"', "goal"),
                            (r"\\server\share\proj", "goal")):
            out = self._advance(step_id="newdir", answer=answer, answers={})
            self.assertEqual("ask", out["kind"], answer)
            self.assertEqual(nxt, out["step"]["id"], answer)

    def test_near_miss_option_goes_off_script(self):
        """客气话（"新建项目吧"）对不上选项 → off_script，交给模型接着聊，不卡人。"""
        out = self._advance(step_id="mode", answer="新建项目吧", answers={})
        self.assertEqual("off_script", out["kind"])

    def test_empty_option_answer_goes_off_script(self):
        out = self._advance(step_id="mode", answer="")
        self.assertEqual("off_script", out["kind"], "选项步骤空答也不该抛错")

    def test_emoji_and_punctuation_survive_the_prompt(self):
        out = self._advance(step_id="goal", answer="做个报表机器人 🚀", answers={})
        self.assertEqual("做个报表机器人 🚀", out["answers"]["项目目标"])


class FlowRpcTest(_Base):
    def test_flows_list_returns_definitions(self):
        got = self._ok(self._req("flows.list"))
        ids = [f["id"] for f in got["flows"]]
        self.assertIn(FLOW_ID, ids)

    def test_next_seeds_the_first_question_into_the_record(self):
        sid = self._project()
        self.assertEqual([], self._messages(sid), "新会话本该是空的")
        got = self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID))
        self.assertEqual("ask", got["kind"])
        self.assertEqual("mode", got["step"]["id"])
        msgs = self._messages(sid)
        # 先招呼一句再问第一件事：直接抛问题太突兀（用户 2026-10-09 提的）。
        self.assertEqual(2, len(msgs))
        self.assertEqual(["assistant", "assistant"], [m["role"] for m in msgs])
        self.assertTrue(msgs[0]["content"].strip(), "开场白不该是空的")
        self.assertIn("新建", msgs[1]["content"])
        # 传下来的步骤里不能带内部字段
        self.assertNotIn("goto", got["step"])
        self.assertNotIn("check", got["step"])

    def test_walking_the_branch_records_both_sides(self):
        sid = self._project()
        self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID))
        got = self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID,
                                 stepId="mode", answer="接入现有项目",
                                 answers={}))
        self.assertEqual("ask", got["kind"])
        self.assertEqual("dir", got["step"]["id"])
        msgs = self._messages(sid)
        self.assertEqual(["assistant", "assistant", "user", "assistant"],
                         [m["role"] for m in msgs])
        self.assertEqual("接入现有项目", msgs[2]["content"])

    def test_retry_does_not_advance_and_says_why(self):
        sid = self._project()
        self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID))
        got = self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID,
                                 stepId="mode", answer="接入现有项目", answers={}))
        self.assertEqual("dir", got["step"]["id"])
        retry = self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID,
                                   stepId="dir", answer="不清楚",
                                   answers={"项目目录": ""}))
        self.assertEqual("retry", retry["kind"])
        self.assertEqual("dir", retry["step"]["id"], "重试不该往前走")
        self.assertEqual(["我重说", "就按这个来"], [o["label"] for o in retry["options"]])
        self.assertEqual(["redo", "force"], [o.get("role") for o in retry["options"]])

    def test_off_script_writes_nothing_and_hands_over(self):
        sid = self._project()
        self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID))
        before = self._messages(sid)
        got = self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID,
                                 stepId="mode", answer="先说别的，我这个项目其实是想改个 bug",
                                 answers={}))
        self.assertEqual("off_script", got["kind"])
        self.assertEqual(len(before), len(self._messages(sid)),
                         "off_script 状态不该写任何消息（界面会把它当普通消息发）")

    def test_end_returns_the_prompt_and_does_not_loop(self):
        sid = self._project()
        self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID))
        self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID,
                           stepId="mode", answer="新建项目", answers={}))
        self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID,
                           stepId="name", answer="数据看板", answers={}))
        self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID,
                           stepId="newdir", answer=r"D:\work\data-dashboard", answers={}))
        self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID,
                           stepId="goal", answer="做个周报机器人",
                           answers={"项目名称": "数据看板", "项目目标": "做个周报机器人"}))
        done = self._ok(self._req("flows.next", sessionId=sid, flowId=FLOW_ID,
                                  stepId="openspec", answer="先不用 OpenSpec，直接聊",
                                  answers={"项目名称": "数据看板", "项目目标": "做个周报机器人",
                                           "项目目录": r"D:\work\data-dashboard"}))
        self.assertEqual("done", done["kind"])
        self.assertTrue(done["step"]["end"])
        self.assertIn("做个周报机器人", done["prompt"])
        self.assertIn("数据看板", done["prompt"], "问到的项目名称要进开场任务")

    def test_errors_are_readable(self):
        sid = self._project()
        code, _ = self._err(self._req("flows.next", sessionId=sid, flowId="ghost"))
        self.assertEqual("BAD_REQUEST", code)
        code, _ = self._err(self._req("flows.next", sessionId=sid, flowId=FLOW_ID,
                                      stepId="ghost"))
        self.assertEqual("BAD_REQUEST", code)
        code, _ = self._err(self._req("flows.next", sessionId="nope", flowId=FLOW_ID))
        self.assertEqual("SESSION_NOT_FOUND", code)
        code, _ = self._err(self._req("flows.next", flowId=FLOW_ID))
        self.assertEqual("BAD_REQUEST", code)

    def test_allowlist_has_the_new_methods(self):
        for m in ("flows.list", "flows.next"):
            self.assertIn(m, sidecar.SIDECAR_CAPABILITIES)


if __name__ == "__main__":
    unittest.main()