"""web/extract.py + web/mcp.py + tools_web.py 单测：正文抽取、搜索后端、工具外壳。

这些用例守的是同一条线：抓回来的东西怎样变成模型能读的文本。零真网络
（fetcher / runner / MCP 传输全部注入），所以离线也能跑。

夹具是真东西：Exa 的文本格式和 Parallel 的 JSON 形状都是 2026-09-24 从
真实端点抓下来的截断版，不是照着文档猜的。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

from web import egress, extract, mcp, search as web_search  # noqa: E402
import tools  # noqa: E402
import tools_web  # noqa: E402


class HtmlToTextTest(unittest.TestCase):
    def test_drops_script_style_and_head(self):
        markup = ("<html><head><title>标题</title><style>body{color:red}</style></head>"
                  "<body><script>var x='偷偷进来的';</script><p>正文一段</p></body></html>")
        title, text, _links = extract.html_to_text(markup)
        self.assertEqual(title, "标题")
        self.assertIn("正文一段", text)
        self.assertNotIn("偷偷进来的", text)
        self.assertNotIn("color:red", text)

    def test_block_tags_become_line_breaks(self):
        _t, text, _l = extract.html_to_text("<div>第一行</div><div>第二行</div><p>第三行</p>")
        self.assertEqual([line for line in text.split("\n") if line],
                         ["第一行", "第二行", "第三行"])

    def test_entities_are_decoded_and_whitespace_collapsed(self):
        _t, text, _l = extract.html_to_text("<p>a&nbsp;&amp;&nbsp;b   \t c</p>")
        self.assertEqual(text, "a & b c")

    def test_blank_line_runs_are_squeezed(self):
        _t, text, _l = extract.html_to_text("<p>a</p><p></p><p></p><p></p><p>b</p>")
        self.assertNotIn("\n\n\n", text)

    def test_links_are_collected_and_relative_ones_dropped(self):
        markup = ('<a href="https://example.com/a">甲</a><a href="/rel">乙</a>'
                  '<a href="https://example.com/b"></a>')
        _t, _text, links = extract.html_to_text(markup)
        self.assertEqual(links, [("甲", "https://example.com/a"),
                                 ("https://example.com/b", "https://example.com/b")])

    def test_link_count_is_capped(self):
        markup = "".join('<a href="https://e.com/%d">x</a>' % i for i in range(100))
        _t, _text, links = extract.html_to_text(markup)
        self.assertEqual(len(links), extract.MAX_LINKS)

    def test_malformed_markup_still_yields_text(self):
        _t, text, _l = extract.html_to_text("<p>开了没关 <b>粗体<div>下一段")
        self.assertIn("开了没关", text)
        self.assertIn("粗体", text)


class JsonAndCharsetTest(unittest.TestCase):
    def test_json_is_pretty_printed_in_place(self):
        body = b'{"a": 1, "b": {"c": "\xe4\xb8\xad"}}'
        out = extract.to_text(body, "application/json")
        self.assertIn('"a": 1', out["text"])
        self.assertIn("中", out["text"])
        self.assertEqual(out["links"], [])

    def test_invalid_json_falls_back_to_raw_text(self):
        out = extract.to_text(b"{not json", "application/json")
        self.assertEqual(out["text"], "{not json")

    def test_charset_header_is_honoured(self):
        body = "中文".encode("gb18030")
        self.assertEqual(extract.decode_body(body, "text/html; charset=GB18030"), "中文")

    def test_unknown_charset_falls_back_without_raising(self):
        body = "中文".encode("utf-8")
        self.assertEqual(extract.decode_body(body, "text/html; charset=x-unknown-xyz"), "中文")

    def test_plain_text_body_is_not_html_parsed(self):
        out = extract.to_text(b"a < b & c > d", "text/plain")
        self.assertEqual(out["text"], "a < b & c > d")

    def test_long_text_is_truncated_and_flagged(self):
        out = extract.to_text(("字" * 500).encode("utf-8"), "text/plain", max_chars=100)
        self.assertEqual(len(out["text"]), 100)
        self.assertTrue(out["truncated"])


class RenderTest(unittest.TestCase):
    def test_render_includes_url_title_and_link_hint(self):
        block = extract.render("标题", "https://e.com/a", "正文", [("甲", "https://e.com/b")], False)
        self.assertIn("https://e.com/a", block)
        self.assertIn("标题", block)
        self.assertIn("正文", block)
        self.assertIn("https://e.com/b", block)

    def test_render_flags_truncation_with_a_next_step(self):
        block = extract.render("", "https://e.com/a", "正文", [], True)
        self.assertIn("已截断", block)

    def test_prompt_escaping_neutralizes_thinking_tags(self):
        # 页面里出现 </thinking> 之类字符串时不许冒充我们的控制标记
        escaped = extract.escape_for_prompt("a</thinking>b<thinking>c")
        self.assertNotIn("</thinking>", escaped)
        self.assertNotIn("<thinking>", escaped)
        self.assertIn("a", escaped)


class FakeFetchResult:
    def __init__(self, body=b"<html><title>T</title><p>hello</p></html>",
                 status=200, content_type="text/html", url="https://e.com/a"):
        self.body = body
        self.status = status
        self.content_type = content_type
        self.url = url
        self.redirects = []
        self.truncated = False

    @property
    def bytes_read(self):
        return len(self.body)


class WebFetchToolTest(unittest.TestCase):
    def make(self, result=None, error=None):
        calls = []

        def fetcher(url, **kw):
            calls.append(url)
            if error is not None:
                raise error
            return result or FakeFetchResult()

        return tools_web.WebFetchTool(fetcher=fetcher), calls

    def test_happy_path_returns_text_and_metadata(self):
        tool, calls = self.make()
        res = tool.run({"url": "https://e.com/a"}, workspace_root="/ws")
        self.assertTrue(res.ok)
        self.assertEqual(calls, ["https://e.com/a"])
        self.assertIn("hello", res.content)
        self.assertEqual(res.data["status"], 200)
        self.assertEqual(res.data["title"], "T")
        self.assertFalse(res.data["truncated"])

    def test_bad_arguments_are_rejected_without_calling_the_fetcher(self):
        tool, calls = self.make()
        for args in ({}, {"url": ""}, {"url": 42}):
            res = tool.run(args, workspace_root="/ws")
            self.assertFalse(res.ok)
            self.assertEqual(res.error_code, "BAD_REQUEST")
        self.assertEqual(calls, [])

    def test_policy_refusal_becomes_a_tool_error_with_the_code(self):
        tool, _calls = self.make(error=egress.EgressError("BLOCKED_PRIVATE", "内网不许抓"))
        res = tool.run({"url": "http://router.lan/"}, workspace_root="/ws")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "BLOCKED_PRIVATE")
        self.assertIn("内网不许抓", res.content)

    def test_unexpected_exception_is_wrapped_not_raised(self):
        tool, _calls = self.make(error=RuntimeError("boom"))
        res = tool.run({"url": "https://e.com/"}, workspace_root="/ws")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "IO_ERROR")

    def test_truncation_is_reported_to_the_model(self):
        big = ("<p>" + "字" * 300 + "</p>").encode("utf-8")
        tool = tools_web.WebFetchTool(fetcher=lambda url, **kw: FakeFetchResult(body=big),
                                      max_text_chars=50)
        res = tool.run({"url": "https://e.com/big"}, workspace_root="/ws")
        self.assertTrue(res.ok)
        self.assertTrue(res.data["truncated"])

    def test_schema_declares_url_required(self):
        schema = tools_web.WebFetchTool().parameters
        self.assertEqual(schema["required"], ["url"])
        self.assertEqual(schema["properties"]["url"]["type"], "string")


class WebSearchToolTest(unittest.TestCase):
    def test_not_configured_is_an_honest_message(self):
        def runner(query, **kw):
            raise web_search.SearchNotConfigured("本机没有配搜索后端")
        res = tools_web.WebSearchTool(runner=runner).run({"query": "jev 模型"},
                                                         workspace_root="/ws")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "SEARCH_NOT_CONFIGURED")
        self.assertIn("web_fetch", res.content)

    def test_backend_failure_is_reported(self):
        def runner(query, **kw):
            raise web_search.SearchFailed("搜索后端返回 HTTP 500")
        res = tools_web.WebSearchTool(runner=runner).run({"query": "x"}, workspace_root="/ws")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "SEARCH_FAILED")

    def test_results_are_rendered_with_urls(self):
        def runner(query, **kw):
            return web_search.SearchResult(query=query, provider="bocha", items=[
                web_search.SearchItem(title="GLM-4.6 发布", url="https://e.com/glm",
                                      snippet="智谱发布 GLM-4.6")])
        res = tools_web.WebSearchTool(runner=runner).run({"query": "glm-4.6"},
                                                         workspace_root="/ws")
        self.assertTrue(res.ok)
        self.assertIn("https://e.com/glm", res.content)
        self.assertIn("bocha", res.content)
        self.assertEqual(res.data["results"][0]["url"], "https://e.com/glm")

    def test_bad_max_results_falls_back_to_default(self):
        seen = {}

        def runner(query, **kw):
            seen.update(kw)
            return web_search.SearchResult(query=query, provider="bocha", items=[])
        tools_web.WebSearchTool(runner=runner).run({"query": "x", "max_results": "abc"},
                                                   workspace_root="/ws")
        self.assertEqual(seen["max_results"], web_search.DEFAULT_RESULTS)

    def test_empty_query_is_rejected(self):
        res = tools_web.WebSearchTool(runner=lambda *a, **k: None).run({}, workspace_root="/ws")
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "BAD_REQUEST")


class ProviderSelectionTest(unittest.TestCase):
    """分层语义：有 key 的先上，没 key 走免 key 公共端点，'none' 才真关掉。"""

    def setUp(self):
        self.saved = {k: os.environ.get(k) for k in
                      ("BOCHA_API_KEY", "TAVILY_API_KEY", "SEARXNG_BASE_URL",
                       "WEB_SEARCH_PROVIDER")}
        for key in self.saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_keyless_floor_when_nothing_configured(self):
        # 一台什么都没有的机器也必须能搜 —— 这是「自己去查一下」不空转的前提。
        self.assertEqual(web_search.available_providers(), [])
        self.assertIn(web_search.active_provider(), web_search.KEYLESS_RING)

    def test_key_presence_selects_backend(self):
        os.environ["BOCHA_API_KEY"] = "k"
        self.assertEqual(web_search.active_provider(), "bocha")

    def test_explicit_provider_wins(self):
        os.environ["BOCHA_API_KEY"] = "k"
        os.environ["WEB_SEARCH_PROVIDER"] = "searxng"
        self.assertEqual(web_search.active_provider(), "searxng")

    def test_search_off_switch(self):
        os.environ["WEB_SEARCH_PROVIDER"] = "none"
        self.assertIsNone(web_search.active_provider())
        self.assertFalse(web_search.search_enabled())
        with self.assertRaises(web_search.SearchNotConfigured):
            web_search.search("x")

    def test_keyed_backend_is_served_first(self):
        os.environ["BOCHA_API_KEY"] = "k"
        used = []

        def fake_bocha(query, count):
            used.append("bocha")
            return [web_search.SearchItem(title="T", url="https://e.com/1", snippet="S")]

        def fake_call(url, tool, args, **_kw):
            used.append(url)
            return "{}"

        with mock.patch.dict(web_search._PROVIDERS,
                             {"bocha": (fake_bocha, ("BOCHA_API_KEY",), False)}), \
                mock.patch("web.mcp.call", fake_call):
            result = web_search.search("x")
        self.assertEqual(result.provider, "bocha")
        self.assertEqual(used, ["bocha"])       # 没去碰免 key 端点

    def test_keyed_failure_falls_back_to_keyless_ring(self):
        # 有 key 的后端挂了不能把用户的搜索也带下去，得兜到免 key 那层。
        os.environ["BOCHA_API_KEY"] = "k"
        order = []

        def boom(query, count):
            order.append("bocha")
            raise web_search.SearchFailed("HTTP 500")

        def fake_call(url, tool, args, **_kw):
            order.append(url)
            if url == mcp.PARALLEL_MCP_URL:
                return json.dumps({"results": [
                    {"title": "P", "url": "https://e.com/p", "excerpts": ["ex"]}]})
            return "Title: E\nURL: https://e.com/e\nHighlights:\nbody"

        with mock.patch.dict(web_search._PROVIDERS,
                             {"bocha": (boom, ("BOCHA_API_KEY",), False)}), \
                mock.patch.object(web_search, "_RING_CURSOR",
                                  web_search.KEYLESS_RING.index("parallel")), \
                mock.patch("web.mcp.call", fake_call):
            result = web_search.search("x")
        self.assertIn(result.provider, web_search.KEYLESS_RING)
        self.assertEqual(order[0], "bocha")
        self.assertGreater(len(order), 1)
        self.assertEqual(result.items[0].url, "https://e.com/p")

    def test_all_backends_failing_says_so(self):
        def fake_call(url, tool, args, **_kw):
            raise mcp.McpError("HTTP 429：rate limit")

        with mock.patch("web.mcp.call", fake_call):
            with self.assertRaises(web_search.SearchFailed) as ctx:
                web_search.search("x")
        self.assertIn("429", str(ctx.exception))

    def test_ring_rotates_per_start_point(self):
        first = web_search.keyless_ring(0)
        second = web_search.keyless_ring(1)
        self.assertEqual(sorted(first), sorted(web_search.KEYLESS_RING))
        self.assertNotEqual(first[0], second[0])

    def test_empty_query_raises_not_configured(self):
        with self.assertRaises(web_search.SearchNotConfigured):
            web_search.search("   ")


#: 真端点抓下来的截断夹具（2026-09-24，search.parallel.ai）。
PARALLEL_FIXTURE = json.dumps({
    "results": [
        {"url": "https://news.qq.com/rain/a/20260619A06NLW00",
         "title": "全球可用模型第一！智谱上线并开源GLM-5.2_腾讯新闻",
         "excerpts": ["全球可用模型第一！", "6月17日，智谱上线并开源新一代旗舰大模型GLM-5.2"]},
        {"url": "https://z.ai/blog/glm-5.2", "title": "GLM-5.2: Built for Long-Horizon Tasks",
         "excerpts": []},
    ]
}, ensure_ascii=False)

#: 同样来自真端点（mcp.exa.ai），SSE 帧里的 text 就是这段。
EXA_FIXTURE = "\n".join([
    "Title: GLM-5.2: Built for Long-Horizon Tasks - Z.ai",
    "URL: https://z.ai/blog/glm-5.2",
    "Published: 2026-06-16T00:00:00.000Z",
    "Author: N/A",
    "Highlights:",
    "GLM-5.2: Built for Long-Horizon Tasks",
    "...",
    "We're introducing GLM-5.2, our latest flagship model for long-horizon tasks.",
    "",
    "Title: 全球可用模型第一！智谱上线并开源GLM-5.2_腾讯新闻",
    "URL: https://news.qq.com/rain/a/20260619A06NLW00",
    "Highlights:",
    "国产大模型慢慢追上来了！",
])


class McpClientTest(unittest.TestCase):
    """免 key 那层的传输细节：SSE 帧、限流识别、错误归类。"""

    def test_parses_plain_jsonrpc_body(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [
            {"type": "text", "text": PARALLEL_FIXTURE}]}})
        self.assertEqual(mcp.parse_body(body), PARALLEL_FIXTURE)

    def test_parses_sse_framed_body(self):
        inner = json.dumps({"result": {"content": [{"type": "text", "text": "hello"}]}})
        self.assertEqual(mcp.parse_body("event: message\ndata: %s\n\n" % inner), "hello")

    def test_jsonrpc_error_becomes_mcp_error(self):
        body = json.dumps({"jsonrpc": "2.0", "error": {"code": -32000, "message": "Too many requests"}})
        with self.assertRaises(mcp.McpError) as ctx:
            mcp.parse_body(body)
        self.assertTrue(ctx.exception.rate_limited)

    def test_tool_level_error_is_raised_not_swallowed(self):
        body = json.dumps({"result": {"isError": True, "content": [
            {"type": "text", "text": "rate limit exceeded"}]}})
        with self.assertRaises(mcp.McpError) as ctx:
            mcp.parse_body(body)
        self.assertTrue(ctx.exception.rate_limited)

    def test_unrecognized_body_is_an_error_not_an_empty_result(self):
        with self.assertRaises(mcp.McpError):
            mcp.parse_body("<html>gateway</html>")

    def test_http_429_marks_rate_limited(self):
        import urllib.error

        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests",
                                          {}, None)

        with self.assertRaises(mcp.McpError) as ctx:
            mcp.call("https://mcp.example/x", "t", {}, opener=opener)
        self.assertTrue(ctx.exception.rate_limited)

    def test_transport_failure_is_reported_with_host(self):
        import urllib.error

        def opener(request, timeout):
            raise urllib.error.URLError("Name or service not known")

        with self.assertRaises(mcp.McpError) as ctx:
            mcp.call("https://mcp.example/x", "t", {}, opener=opener)
        self.assertIn("mcp.example", str(ctx.exception))


class KeylessProviderTest(unittest.TestCase):
    def test_parallel_results_map_to_items(self):
        with mock.patch("web.mcp.call", lambda *a, **k: PARALLEL_FIXTURE):
            items = web_search._parallel("GLM-5.2", 5)
        self.assertEqual(items[0].url, "https://news.qq.com/rain/a/20260619A06NLW00")
        self.assertIn("智谱", items[0].title)
        self.assertIn("全球可用模型第一", items[0].snippet)
        self.assertEqual(items[1].url, "https://z.ai/blog/glm-5.2")

    def test_exa_text_results_map_to_items(self):
        with mock.patch("web.mcp.call", lambda *a, **k: EXA_FIXTURE):
            items = web_search._exa("GLM-5.2", 5)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].url, "https://z.ai/blog/glm-5.2")
        self.assertIn("long-horizon", items[0].snippet)

    def test_exa_blocks_without_url_are_dropped(self):
        parsed = mcp.parse_exa_text("Title: 没有网址\nHighlights:\n正文", 5)
        self.assertEqual(parsed, [])

    def test_limit_is_respected(self):
        with mock.patch("web.mcp.call", lambda *a, **k: PARALLEL_FIXTURE):
            self.assertEqual(len(web_search._parallel("x", 1)), 1)

    def test_provider_failure_surfaces_as_search_failed(self):
        def boom(*a, **k):
            raise mcp.McpError("HTTP 429：rate limit")

        with mock.patch("web.mcp.call", boom):
            with self.assertRaises(web_search.SearchFailed):
                web_search._parallel("x", 5)


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        # 其它测试会 tools.register_defaults()，那会 clear 注册表后再装基础 11 个 ——
        # 所以这里不依赖 import 顺序，显式装一遍（函数本身幂等）。
        tools_web.register_web_tools()

    def test_import_registers_both_tools(self):
        names = {t.name for t in tools.list_tools()}
        self.assertIn("web_fetch", names)
        self.assertIn("web_search", names)

    def test_registration_is_idempotent(self):
        before = sorted(t.name for t in tools.list_tools())
        tools_web.register_web_tools()
        self.assertEqual(before, sorted(t.name for t in tools.list_tools()))

    def test_defaults_plus_web_is_the_app_state(self):
        # 应用启动时的真实顺序：tools 模块 import 时跑 register_defaults()
        # → 随后 import tools_web（副作用注册这两个工具）。
        tools._reset_for_tests()
        try:
            tools.register_defaults()
            tools_web.register_web_tools()
            names = {t.name for t in tools.list_tools()}
            self.assertLessEqual({"read_file", "run_shell", "web_fetch", "web_search"}, names)
        finally:
            # register_defaults() 是幂等累加，不会自己清空：别的测试依赖至少这 11 个
            tools.register_defaults()
            tools_web.register_web_tools()

    def test_sidecar_imports_the_web_tools(self):
        # 顺序依赖不能靠记忆维持：sidecar 必须真的 import tools_web，否则应用里
        # 这两个工具会静默消失（测试里 registration 是我们手动装的，掩盖得掉）。
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "python", "sidecar.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("import tools_web", source)

    def test_web_tools_never_ask_for_approval(self):
        """联网不逐次审批（Hermes 口径）：安全边界在 web/egress.py（私网拒绝 +
        DNS 钉死 + 跨站重定向重验 + 字节上限），不靠弹窗。旧的 L0 分级已取消。"""
        import guard
        for name, args in (("web_fetch", {"url": "https://example.com"}),
                           ("web_search", {"query": "python"})):
            self.assertFalse(guard.judge(name, args).requires_approval, name)


if __name__ == "__main__":
    unittest.main()