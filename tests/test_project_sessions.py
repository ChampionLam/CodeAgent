"""项目会话（007）：RPC 面上的五个字段 + 每轮目录/技能解析。

用户 2026-09-28 的需求口径：「本质上还是个会话，只不过会加一些特性」；
2026-10-09 补齐引擎层时定死的三条硬规矩，这里逐条钉住：

  1. **普通会话零变化** —— 不传项目参数时，创建/列表的返回与老版本一字不差
     （老键还在、新键有默认值），界面上不该看得出后端动过；
  2. **参数错必须是 BAD_REQUEST** —— 名字空 / 目录不存在 / kind 非法都是语义错，
     报成 INTERNAL 界面只能给一句没用的提示；未知 id 才是 SESSION_NOT_FOUND；
  3. **局部更新不许误伤** —— 只改名时目录与技能必须原样留着（这是 007 数据层
     就定下的语义，RPC 层照做，不能「补齐默认值再整行写回」）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "python"))

import appconfig  # noqa: E402
import llm  # noqa: E402
import modelconfig as MC  # noqa: E402
import sidecar  # noqa: E402
import prompt_build  # noqa: E402


class _ProjectCase(unittest.TestCase):
    """临时数据目录 + 直接打 RPC 帧（照 test_history_wire.py 的隔离口径）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = sidecar._DATA_DIR
        self._old_store = getattr(sidecar, "_SESSIONS", None)
        sidecar._DATA_DIR = self._tmp.name
        sidecar._SESSIONS = None
        # 项目目录（workspace_root 校验要求真存在）
        self._ws = os.path.join(self._tmp.name, "proj")
        os.makedirs(self._ws, exist_ok=True)

    def tearDown(self):
        tmp = os.path.normcase(os.path.abspath(self._tmp.name))
        for _attr in ("_SESSIONS", "_AUDIT"):
            store = getattr(sidecar, _attr, None)
            db = getattr(store, "db_path", None)
            if not isinstance(db, str):
                continue
            if not os.path.normcase(os.path.abspath(db)).startswith(tmp):
                continue
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        sidecar._SESSIONS = self._old_store
        sidecar._DATA_DIR = self._old_dir
        self._tmp.cleanup()

    def _req(self, method: str, **params) -> dict:
        params.setdefault("protocolVersion", sidecar.SIDECAR_PROTOCOL_VERSION)
        return sidecar.handle_request({"type": "req", "id": "p1",
                                       "method": method, "params": params})

    def _ok(self, resp: dict) -> dict:
        self.assertTrue(resp.get("ok"), "RPC 没成功：%r" % (resp,))
        return resp.get("result") or {}

    def _err(self, resp: dict) -> tuple:
        self.assertFalse(resp.get("ok"), "本该报错却成功了：%r" % (resp,))
        err = resp.get("error") or {}
        return err.get("code"), err.get("message")

    def _create_project(self, **params) -> dict:
        params.setdefault("kind", "project")
        params.setdefault("projectName", "我的项目")
        return self._ok(self._req("sessions.create", **params))

    # ---------- 1. 老行为不变 ----------

    def test_chat_session_create_is_unchanged(self):
        got = self._ok(self._req("sessions.create", title="新会话"))
        self.assertIn("id", got)
        self.assertEqual("新会话", got["title"])
        # 新键有默认值（界面不必猜缺键），老键一个不少
        self.assertEqual("chat", got["kind"])
        self.assertIsNone(got["projectName"])
        self.assertIsNone(got["workspaceRoot"])
        self.assertEqual([], got["projectSkills"])
        self.assertEqual([], got["projectFiles"])

    def test_chat_session_default_title_unchanged(self):
        got = self._ok(self._req("sessions.create"))
        self.assertEqual("New session", got["title"])
        self.assertEqual("chat", got["kind"])

    def test_chat_session_workspace_resolution_unchanged(self):
        """普通会话的目录解析必须还是「配置 → ~」，不能被项目逻辑碰一下。"""
        sid = self._ok(self._req("sessions.create", title="t"))["id"]
        self.assertEqual(sidecar._base_dir(None),
                         sidecar._resolve_workspace(None, session_id=sid))
        self.assertIsNone(sidecar._project_skills_of(sid))

    # ---------- 2. 创建项目会话 ----------

    def test_create_project_session_roundtrip(self):
        got = self._create_project(workspaceRoot=self._ws,
                                   projectSkills=["openspec", "excel"])
        sid = got["id"]
        self.assertEqual("project", got["kind"])
        self.assertEqual("我的项目", got["projectName"])
        self.assertEqual(self._ws, got["workspaceRoot"])
        self.assertEqual(["openspec", "excel"], got["projectSkills"])

        row = sidecar._session_store().get_session(sid)
        self.assertEqual("project", row["kind"])
        self.assertEqual("我的项目", row["project_name"])
        self.assertEqual(["openspec", "excel"], row["project_skills"])

    def test_create_project_without_name_is_bad_request(self):
        code, msg = self._err(self._req("sessions.create", kind="project"))
        self.assertEqual("BAD_REQUEST", code, msg)

    def test_create_project_blank_name_is_bad_request(self):
        code, _ = self._err(self._req("sessions.create", kind="project",
                                      projectName="   "))
        self.assertEqual("BAD_REQUEST", code)

    def test_create_project_missing_dir_is_bad_request(self):
        code, _ = self._err(self._req("sessions.create", kind="project",
                                      projectName="x",
                                      workspaceRoot=os.path.join(self._tmp.name, "nope")))
        self.assertEqual("BAD_REQUEST", code)

    def test_create_bad_kind_is_bad_request(self):
        code, _ = self._err(self._req("sessions.create", kind="folder",
                                      projectName="x"))
        self.assertEqual("BAD_REQUEST", code)

    def test_create_blank_project_params_mean_not_given(self):
        """界面会把空输入框一起发上来 —— "" 必须等于「没传」，不能当空名字炸。"""
        got = self._ok(self._req("sessions.create", title="t", kind="",
                                 projectName="", workspaceRoot="", projectSkills=[]))
        self.assertEqual("chat", got["kind"])
        self.assertIsNone(got["projectName"])

    # ---------- 3. 局部更新 ----------

    def test_update_project_is_wired(self):
        self.assertIn("sessions.updateProject", sidecar.SIDECAR_CAPABILITIES)

    def test_update_project_rename_keeps_dir_and_skills(self):
        sid = self._create_project(workspaceRoot=self._ws,
                                   projectSkills=["openspec"])["id"]
        got = self._ok(self._req("sessions.updateProject", sessionId=sid,
                                 projectName="改过名"))
        self.assertEqual("改过名", got["projectName"])
        self.assertEqual(self._ws, got["workspaceRoot"])
        self.assertEqual(["openspec"], got["projectSkills"])

    def test_update_project_skills_can_be_replaced(self):
        sid = self._create_project(workspaceRoot=self._ws)["id"]
        got = self._ok(self._req("sessions.updateProject", sessionId=sid,
                                 projectSkills=["excel"]))
        self.assertEqual(["excel"], got["projectSkills"])

    def test_update_project_unknown_id_is_session_not_found(self):
        code, _ = self._err(self._req("sessions.updateProject", sessionId="nope",
                                      projectName="x"))
        self.assertEqual("SESSION_NOT_FOUND", code)

    def test_update_project_requires_session_id(self):
        code, _ = self._err(self._req("sessions.updateProject", projectName="x"))
        self.assertEqual("BAD_REQUEST", code)

    def test_update_project_bad_dir_is_bad_request(self):
        sid = self._create_project(workspaceRoot=self._ws)["id"]
        code, _ = self._err(self._req("sessions.updateProject", sessionId=sid,
                                      workspaceRoot=os.path.join(self._tmp.name, "nope")))
        self.assertEqual("BAD_REQUEST", code)

    # ---------- 4. 列表带字段 ----------

    def test_list_carries_project_fields(self):
        chat = self._ok(self._req("sessions.create", title="普通"))["id"]
        proj = self._create_project(workspaceRoot=self._ws,
                                    projectSkills=["openspec"])["id"]
        rows = {r["id"]: r for r in
                self._ok(self._req("sessions.list"))["sessions"]}
        self.assertEqual("chat", rows[chat]["kind"])
        self.assertIsNone(rows[chat]["projectName"])
        self.assertEqual([], rows[chat]["projectSkills"])
        self.assertEqual("project", rows[proj]["kind"])
        self.assertEqual("我的项目", rows[proj]["projectName"])
        self.assertEqual(self._ws, rows[proj]["workspaceRoot"])
        self.assertEqual(["openspec"], rows[proj]["projectSkills"])
        # 老键不能丢
        for key in ("title", "updatedAt", "createdAt", "model", "status",
                    "messageCount", "totalInputTokens", "totalOutputTokens"):
            self.assertIn(key, rows[proj], "%s 掉了" % key)

    # ---------- 5. 每轮目录 / 技能解析 ----------

    def test_resolve_workspace_priority(self):
        sid = self._create_project(workspaceRoot=self._ws)["id"]
        other = os.path.join(self._tmp.name, "other")
        os.makedirs(other, exist_ok=True)
        # ① 项目目录在没显式传时生效
        self.assertEqual(os.path.abspath(self._ws),
                         sidecar._resolve_workspace(None, session_id=sid))
        # ② 显式传的永远优先（用户当轮临时切目录）
        self.assertEqual(os.path.abspath(other),
                         sidecar._resolve_workspace(other, session_id=sid))
        # ③ 未知 id / 没 id：落回配置基准，不抛
        self.assertEqual(sidecar._base_dir(None),
                         sidecar._resolve_workspace(None, session_id="nope"))
        self.assertEqual(sidecar._base_dir(None),
                         sidecar._resolve_workspace(None))

    def test_project_skills_filter(self):
        sid = self._create_project(workspaceRoot=self._ws,
                                   projectSkills=["openspec"])["id"]
        self.assertEqual(["openspec"], sidecar._project_skills_of(sid))
        # 没挂技能 = 不做过滤（None），普通会话同样 None
        bare = self._create_project(projectName="秃的")["id"]
        self.assertIsNone(sidecar._project_skills_of(bare))
        chat = self._ok(self._req("sessions.create", title="普通"))["id"]
        self.assertIsNone(sidecar._project_skills_of(chat))

    def test_system_prompt_skills_are_filtered(self):
        """allowed_skills 只影响「列哪些技能」，不是新机制。"""
        orig_hooks = sidecar._memory_hooks
        sidecar._memory_hooks = lambda: None            # 不碰记忆子系统
        try:
            full = sidecar._system_prompt_for(self._ws)
            none_matching = sidecar._system_prompt_for(
                self._ws, allowed_skills=["__no_such_skill__"])
        finally:
            sidecar._memory_hooks = orig_hooks
        # 过滤到一个都不剩 -> 技能段整段不渲染
        self.assertNotIn(prompt_build.SKILLS_HEADER, none_matching)
        self.assertNotEqual(full, none_matching)


# ---------------------------------------------------------------------------
# 真链路：一轮对话实际用的是哪个目录、哪份提示词
# ---------------------------------------------------------------------------

_CFG = {
    "model": {"baseUrl": "https://example.invalid/v1", "model": "test-model",
              "apiKeyEnv": "TEST_MODEL_KEY", "maxTokens": 64, "timeoutSeconds": 5},
    "models": {
        "defaultId": "t1",
        "items": [{
            "id": "t1", "label": "T", "provider": "custom",
            "baseUrl": "https://example.invalid/v1", "model": "test-model",
            "apiKeyEnv": "TEST_MODEL_KEY", "maxTokens": 64, "timeoutSeconds": 5,
        }],
    },
}


class ProjectSessionChatWiringTest(unittest.TestCase):
    """真跑一轮 `_run_chat`（假 agent_loop.run + 假技能表），看引擎实际用了什么。

    上一条类能证明 `_resolve_workspace` / `_project_skills_of` 各自算得对；
    这一条证明**它们真被那轮对话用上了** —— 只测函数的话，「函数写好了但没接进
    ctx / 没传进 prompt」会一路绿着，这正是在这个项目上踩过的坑。
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="desk-agent-projchat-")
        self._saved_env = dict(os.environ)
        for var in ("DESK_AGENT_ENV_FILE", "TEST_MODEL_KEY"):
            os.environ.pop(var, None)
        os.environ["DESK_AGENT_ENV_FILE"] = os.path.join(self.root, "test.env")
        os.environ["TEST_MODEL_KEY"] = "test-key"
        self._saved_override = MC._MODELS_ROOT_OVERRIDE
        MC._MODELS_ROOT_OVERRIDE = self.root
        self._saved_cfg = getattr(sidecar, "_CFG", None)
        sidecar._CFG = llm.ModelConfig(base_url="https://example.invalid/v1",
                                       model="test-model", api_key_env="TEST_MODEL_KEY",
                                       api_key="test-key", max_tokens=64,
                                       timeout_seconds=5, provider="custom")
        # appconfig 的根也是模块级的（capabilities.workspaceRoot 从它读），
        # 不指过来就会落到仓库根那份 config.json 上。
        self._saved_app_root = appconfig._ROOT
        appconfig._ROOT = self.root
        self._saved_data_dir = sidecar._DATA_DIR
        self._saved_store = getattr(sidecar, "_SESSIONS", None)
        sidecar._DATA_DIR = os.path.join(self.root, "data")
        sidecar._SESSIONS = None
        self.default_ws = os.path.join(self.root, "default-ws")
        self.ws = os.path.join(self.root, "proj")
        os.makedirs(self.default_ws, exist_ok=True)
        os.makedirs(self.ws, exist_ok=True)
        cfg = dict(_CFG)
        # 全局兜底目录也指到临时目录：普通会话的解析结果要可断言，不能落到真 ~
        cfg["capabilities"] = {"workspaceRoot": self.default_ws}
        with open(os.path.join(self.root, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def tearDown(self):
        tmp = os.path.normcase(os.path.abspath(self.root))
        for _attr in ("_SESSIONS", "_AUDIT"):
            store = getattr(sidecar, _attr, None)
            db = getattr(store, "db_path", None)
            if isinstance(db, str) and os.path.normcase(os.path.abspath(db)).startswith(tmp):
                close = getattr(store, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        sidecar._SESSIONS = self._saved_store
        sidecar._DATA_DIR = self._saved_data_dir
        sidecar._CFG = self._saved_cfg
        appconfig._ROOT = self._saved_app_root
        MC._MODELS_ROOT_OVERRIDE = self._saved_override
        os.environ.clear()
        os.environ.update(self._saved_env)
        shutil.rmtree(self.root, ignore_errors=True)

    # -- 工具 --

    def _store(self):
        store = sidecar._session_store()
        self.assertIsNotNone(store, "临时目录里的会话库没起来")
        return store

    def _new_session(self, **kw) -> str:
        return self._store().create_session(
            model="t1", base_url="https://example.invalid/v1",
            title=kw.pop("title", "t"), **kw)

    def _chat_ctx(self, params) -> dict:
        """跑一轮真 _run_chat：把 agent_loop.run 换掉，只留它拿到的 ctx。"""
        seen: dict = {}
        orig_run = sidecar.agent_loop.run
        orig_emit = sidecar._emit
        orig_list = sidecar.skills_registry.list_skills
        orig_hooks = sidecar._memory_hooks

        def fake_run(ctx, *, deps):
            seen["ws"] = ctx.workspace_root
            seen["prompt"] = ctx.system_prompt
            return iter([{"type": "chat.done", "text": "ok"}])

        sidecar.agent_loop.run = fake_run
        sidecar._emit = lambda event, data: None
        sidecar.skills_registry.list_skills = lambda: [
            SimpleNamespace(name="alpha", description="甲技能", when_to_use=None),
            SimpleNamespace(name="beta", description="乙技能", when_to_use=None),
        ]
        sidecar._memory_hooks = lambda: None          # 不碰记忆子系统
        try:
            sidecar._run_chat("c1", dict(params))
        finally:
            sidecar.agent_loop.run = orig_run
            sidecar._emit = orig_emit
            sidecar.skills_registry.list_skills = orig_list
            sidecar._memory_hooks = orig_hooks
        self.assertIn("prompt", seen, "一轮对话没走到 loop：%r" % (seen,))
        return seen

    def _chat(self, sid: str, **extra) -> dict:
        params = {"messages": [{"role": "user", "content": "hi"}], "sessionId": sid}
        params.update(extra)
        return self._chat_ctx(params)

    # -- 断言 --

    def test_project_directory_reaches_the_loop(self):
        sid = self._new_session(kind="project", project_name="P",
                                workspace_root=self.ws)
        seen = self._chat(sid)
        self.assertEqual(os.path.abspath(self.ws), seen["ws"])

    def test_explicit_directory_still_wins(self):
        sid = self._new_session(kind="project", project_name="P",
                                workspace_root=self.ws)
        other = os.path.join(self.root, "other")
        os.makedirs(other, exist_ok=True)
        seen = self._chat(sid, workspaceRoot=other)
        self.assertEqual(os.path.abspath(other), seen["ws"])

    def test_chat_session_still_uses_the_configured_directory(self):
        sid = self._new_session()
        seen = self._chat(sid)
        self.assertEqual(os.path.abspath(self.default_ws), seen["ws"])

    def test_project_skills_reach_the_prompt(self):
        sid = self._new_session(kind="project", project_name="P",
                                workspace_root=self.ws,
                                project_skills=["alpha"])
        prompt = self._chat(sid)["prompt"]
        self.assertIn("alpha", prompt)
        self.assertNotIn("beta", prompt)

    def test_project_without_skills_lists_everything(self):
        """没挂技能的项目会话，技能清单必须与普通会话**一字不差**。

        只比技能那一段：整份提示词天然会差（项目会话的工作目录写在里面，
        这正是它该有的样子）。
        """
        bare = self._new_session(kind="project", project_name="P",
                                 workspace_root=self.ws)
        chat = self._new_session()

        def skills_block(prompt: str) -> str:
            if prompt_build.SKILLS_HEADER not in prompt:
                return ""
            tail = prompt.split(prompt_build.SKILLS_HEADER, 1)[1]
            return prompt_build.SKILLS_HEADER + tail.split("\n## ", 1)[0]

        bare_block = skills_block(self._chat(bare)["prompt"])
        chat_block = skills_block(self._chat(chat)["prompt"])
        self.assertIn("beta", bare_block)
        self.assertEqual(chat_block, bare_block)


if __name__ == "__main__":
    unittest.main()