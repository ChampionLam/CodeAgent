"""编排层单测：能力清单必须跟工具表同源，调查阶梯必须在，宣称与工具表不许打架。

这一组守的是一个真机事故（2026-09-24）：BASE_ROLE 里写着「可以搜索内容」，
但本机只有 search_files（本地文件搜索），联网工具一个都没有。模型于是以为
自己能上网，用户说「你自己去联网查一下」时它答「好，我去查」，然后无工具可
调用，这一轮就空转结束。所以这里的断言都是「文案不许跟工具表冲突」。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

import prompt_build  # noqa: E402
import tools  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def tool_stub(*names):
    return [SimpleNamespace(name=n, description="%s does things" % n) for n in names]


LOCAL_ONLY = tool_stub("read_file", "list_dir", "search_files", "write_file",
                       "run_shell", "delete_path", "read_skill")
WITH_WEB = tool_stub("read_file", "list_dir", "search_files", "write_file",
                     "run_shell", "delete_path", "read_skill", "web_fetch", "web_search")
FETCH_ONLY = tool_stub("read_file", "run_shell", "web_fetch")


class BaseRoleTest(unittest.TestCase):
    def test_base_role_no_longer_claims_abilities_it_cannot_check(self):
        # 这一句是本轮修的病根，不能回来。
        self.assertNotIn("可以读写文件、执行命令、搜索内容", prompt_build.BASE_ROLE)

    def test_base_role_defers_to_the_generated_section(self):
        self.assertIn("能力与边界", prompt_build.BASE_ROLE)

    def test_investigation_ladder_is_present_and_ordered(self):
        role = prompt_build.BASE_ROLE
        self.assertIn("先看本地", role)
        self.assertIn("联网查", role)
        self.assertIn("确实查不到才回头问用户", role)
        # 老的第 3 条「信息不足就先问一句」教它退缩，必须已删掉
        self.assertNotIn("信息不足就先问一句", role)
        self.assertLess(role.index("先看本地"), role.index("确实查不到才回头问用户"))

    def test_announce_must_be_followed_by_a_call(self):
        role = prompt_build.BASE_ROLE
        self.assertIn("说要做就必须做", role)
        self.assertIn("只宣布不调用", role)

    def test_no_fabrication_rule_and_citation_rule(self):
        role = prompt_build.BASE_ROLE
        self.assertIn("不许编", role)
        self.assertIn("给出来处", role)

    def test_evidence_before_conclusion(self):
        self.assertIn("证据先落地再出结论", prompt_build.BASE_ROLE)

    def test_give_up_after_two_failures_on_the_same_route(self):
        self.assertIn("连续失败两次就换路", prompt_build.BASE_ROLE)

    def test_base_role_names_no_tool(self):
        # 提示词里写死工具名 = 迟早跟工具表漂移。BASE_ROLE 里一个工具名都不许有，
        # 工具名只出现在同源生成的那一节。
        role = prompt_build.BASE_ROLE
        for name in ("search_files", "web_fetch", "web_search", "read_skill",
                     "save_rule", "remove_rule", "run_shell"):
            self.assertNotIn(name, role)


class CapabilitySectionTest(unittest.TestCase):
    def test_local_only_machine_declares_no_web_access(self):
        block = prompt_build.capability_section(LOCAL_ONLY)
        self.assertIn("## 能力与边界", block)
        self.assertIn("本机没有的能力", block)
        self.assertIn("联网搜索", block)
        self.assertIn("抓取网页", block)
        self.assertNotIn("web_fetch", block.split("本机没有的能力")[0])

    def test_machine_with_web_tools_declares_them(self):
        block = prompt_build.capability_section(WITH_WEB)
        positive = block.split("本机没有的能力")[0]
        self.assertIn("web_fetch", positive)
        self.assertIn("web_search", positive)
        self.assertNotIn("联网搜索，", block)

    def test_fetch_without_search_explains_the_gap(self):
        block = prompt_build.capability_section(FETCH_ONLY)
        self.assertIn("抓取网页：web_fetch", block)
        self.assertIn("只能靠 web_fetch 抓已知网址", block)

    def test_local_search_is_flagged_as_not_internet(self):
        block = prompt_build.capability_section(LOCAL_ONLY)
        self.assertIn("只搜本地文件，搜不了互联网", block)

    def test_capability_rows_come_from_the_tool_list_only(self):
        # 同一份工具表输入，两次生成必须逐字一致（否则不是同源，是靠手写）
        self.assertEqual(prompt_build.capability_section(WITH_WEB),
                         prompt_build.capability_section(WITH_WEB))

    def test_empty_tool_list_renders_nothing(self):
        self.assertEqual(prompt_build.capability_section([]), "")

    def test_absent_capabilities_are_named_so_the_model_can_say_no(self):
        block = prompt_build.capability_section(LOCAL_ONLY)
        self.assertIn("看图片", block)
        self.assertIn("生成图片或视频", block)

    def test_every_catalog_row_is_reachable(self):
        # 全量工具在场时不该有「没有的能力」这种自相矛盾的行
        every = tool_stub(*[name for _label, owners, _c, _ac in prompt_build.CAPABILITY_CATALOG
                            for name in owners])
        block = prompt_build.capability_section(every)
        self.assertIn("本机没有的能力：无", block)


class BuildOrderTest(unittest.TestCase):
    def test_capability_sits_between_skills_and_tools(self):
        prompt = prompt_build.build(workspace="/ws",
                                    skills=[SimpleNamespace(name="s1", description="d",
                                                            when_to_use=None)],
                                    tools=WITH_WEB,
                                    now=__import__("datetime").datetime(2026, 9, 24, 12, 0, 0))
        self.assertLess(prompt.index("## 可用 skills"), prompt.index("## 能力与边界"))
        self.assertLess(prompt.index("## 能力与边界"), prompt.index("## 可用工具"))
        self.assertLess(prompt.index("## 可用工具"), prompt.index("## 运行环境"))

    def test_build_without_tools_has_no_capability_section(self):
        prompt = prompt_build.build(workspace="/ws", tools=[], skills=[],
                                    now=__import__("datetime").datetime(2026, 9, 24, 12, 0, 0))
        self.assertNotIn("## 能力与边界", prompt)
        self.assertIn(prompt_build.BASE_ROLE, prompt)


class LiveRegistryTest(unittest.TestCase):
    """真注册表也要对得上：注册了什么工具，提示词就宣称什么。"""

    def test_registry_matches_the_generated_section(self):
        section = prompt_build.capability_section(tools.list_tools())
        names = {t.name for t in tools.list_tools()}
        for name in sorted(names):
            self.assertIn(name, section)

    def test_real_machine_without_search_backend_says_so(self):
        import tools_web
        tools_web.register_web_tools()   # 别的测试可能 reset 过注册表
        saved = os.environ.pop("BOCHA_API_KEY", None)
        saved_t = os.environ.pop("TAVILY_API_KEY", None)
        saved_s = os.environ.pop("SEARXNG_BASE_URL", None)
        try:
            block = prompt_build.capability_section(tools.list_tools())
            # 工具写了就算有能力（后端没配是另一层，工具自己会说实话）
            self.assertIn("web_search", block)
        finally:
            if saved:
                os.environ["BOCHA_API_KEY"] = saved
            if saved_t:
                os.environ["TAVILY_API_KEY"] = saved_t
            if saved_s:
                os.environ["SEARXNG_BASE_URL"] = saved_s


if __name__ == "__main__":
    unittest.main()