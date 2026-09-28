"""gen_provider / gen_registry / providers 的单元测试。

跑法（两种都支持）：
    cd <repo> && python3 -m unittest tests.test_gen_providers -v
    python3 tests/test_gen_providers.py

硬约束（照契约第 6 节）：
  * 不打真网络：所有 opener 都是假 opener（本文件里没有任何真 HTTP 代码路径）。
  * 不写项目目录：临时文件一律 tempfile。
  * 断言实际发出的 JSON 字段（万相 input.messages[0].content[0].text、
    MiniMax model/n/aspect_ratio/response_format）。
  * video 轮询失败不重新提交（断言提交只调了一次）。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import gen_registry  # noqa: E402
import gen_provider  # noqa: E402
from gen_provider import (  # noqa: E402
    ALLOWED_ASPECT_RATIOS,
    DEFAULT_ASPECT_RATIO,
    GEN_ERROR_CODES,
    GenError,
    GenResult,
    fail,
    ok,
    resolve_aspect_ratio,
    save_b64_image,
    save_bytes_to,
    normalize_references,
)
from providers.image_dashscope import DashscopeImageProvider  # noqa: E402
from providers.image_minimax import MiniMaxImageProvider  # noqa: E402
from providers.video_minimax import MiniMaxVideoProvider  # noqa: E402


# ---------------------------------------------------------------------------
# 假 opener：按 URL 分发脚本化响应，记录每次请求的 (url, method, headers, body)
# ---------------------------------------------------------------------------

class FakeOpener:
    """url → list[(status, body)] 脚本化响应，按调用顺序弹出。

    用途：
      * 断言实际发出的请求 URL / method / headers / JSON body
      * 统计某 URL 被调了几次（video：提交只能 1 次）
    """

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def __call__(self, url, data=None, headers=None, timeout=..., method="GET"):
        with self.lock:
            self.calls.append({
                "url": url,
                "method": method or "GET",
                "headers": dict(headers) if headers else {},
                "data": data,
                "timeout": timeout,
            })
        # 匹配规则：脚本里第一个「以该 URL 前缀开头且有剩余响应」的条目
        for key in list(self.script.keys()):
            if url.startswith(key) and self.script.get(key):
                status, body = self.script[key].pop(0)
                return status, _as_bytes(body)
        raise AssertionError("FakeOpener: 未脚本化的请求: %r (已有 keys=%r)"
                             % (url, list(self.script.keys())))

    def calls_to(self, url_prefix: str) -> list[dict]:
        return [c for c in self.calls if c["url"].startswith(url_prefix)]

    def json_bodies(self, url_prefix: str) -> list[dict]:
        return [json.loads(c["data"].decode("utf-8")) for c in self.calls_to(url_prefix)]


def _as_bytes(body) -> bytes:
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMI"
    "QAAAABJRU5ErkJggg=="
)


def _ds_ok_body(request_id="req-1", image_url="https://oss.example.com/img.png") -> dict:
    return {
        "request_id": request_id,
        "output": {
            "choices": [
                {"message": {"content": [{"image": image_url}]}}
            ]
        },
    }


def _mm_ok_body(url="https://mm.example.com/img.png") -> dict:
    return {
        "base_resp": {"status_code": 0, "status_msg": ""},
        "data": {"image_urls": [url]},
    }


def _mm_err_body(status_code: int, status_msg: str = "err") -> dict:
    return {
        "base_resp": {"status_code": status_code, "status_msg": status_msg},
        "data": {},
    }


class GenTestCase(unittest.TestCase):
    """公共：注册表每测前清空；临时目录。"""

    def setUp(self) -> None:
        gen_registry._reset_for_tests()
        self.tmp = tempfile.mkdtemp(prefix="desk_gen_test_")

    def tearDown(self) -> None:
        for name in os.listdir(self.tmp):
            try:
                os.unlink(os.path.join(self.tmp, name))
            except OSError:
                pass
        os.rmdir(self.tmp)
        gen_registry._reset_for_tests()

    # ---- helpers -----------------------------------------------------------

    def new_ds(self, opener, base_url="https://myws.example.com"):
        return DashscopeImageProvider(api_key="k-ds", opener=opener, base_url=base_url)

    def new_mm_img(self, opener):
        return MiniMaxImageProvider(api_key="k-mm", opener=opener)

    def new_mm_video(self, opener, **kw):
        kw.setdefault("sleep", lambda s: None)     # 单测绝不真等 10s
        kw.setdefault("poll_interval_s", 0)
        return MiniMaxVideoProvider(api_key="k-mm", opener=opener, **kw)


# ===========================================================================
# gen_provider 助手
# ===========================================================================

class TestHelpers(unittest.TestCase):

    def test_save_bytes_to_creates_dir_and_returns_abs_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "nested", "out")
            path = save_bytes_to(b"hello", out, suffix=".png")
            self.assertTrue(os.path.isabs(path))
            self.assertTrue(path.startswith(os.path.abspath(out)))
            self.assertTrue(path.endswith(".png"))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"hello")
            # 目录已按需创建
            self.assertTrue(os.path.isdir(out))
            # 文件名唯一：两次不互相覆盖
            path2 = save_bytes_to(b"hello", out, suffix=".png")
            self.assertNotEqual(path, path2)

    def test_save_b64_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = save_b64_image(base64.b64encode(_PNG_1x1).decode(), tmp, fmt="png")
            self.assertTrue(path.endswith(".png"))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), _PNG_1x1)

    def test_save_b64_image_rejects_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GenError) as ctx:
                save_b64_image("!!!not-base64!!!", tmp, fmt="png")
            self.assertEqual(ctx.exception.code, "BAD_REQUEST")
            with self.assertRaises(GenError) as ctx:
                save_b64_image(base64.b64encode(b"x").decode(), tmp, fmt="png/../evil")
            self.assertEqual(ctx.exception.code, "BAD_REQUEST")

    def test_normalize_references_local_path_to_data_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "ref.png")
            with open(p, "wb") as f:
                f.write(_PNG_1x1)
            out = normalize_references([p])
            self.assertEqual(len(out), 1)
            self.assertTrue(out[0].startswith("data:image/png;base64,"))
            payload = out[0].split(",", 1)[1]
            self.assertEqual(base64.b64decode(payload), _PNG_1x1)

    def test_normalize_references_passthrough_and_missing(self):
        self.assertEqual(normalize_references(["https://x.example/a.png"]), ["https://x.example/a.png"])
        self.assertEqual(normalize_references(["data:image/png;base64,AAA"]), ["data:image/png;base64,AAA"])
        # 空串/None 条目被跳过
        self.assertEqual(normalize_references(["", "   "]), [])
        with self.assertRaises(GenError) as ctx:
            normalize_references(["/nonexistent/ref.png"])
        self.assertEqual(ctx.exception.code, "BAD_REQUEST")

    def test_resolve_aspect_ratio(self):
        # 合法值原样
        for v in ALLOWED_ASPECT_RATIOS:
            self.assertEqual(resolve_aspect_ratio(v), v)
        # None / 空 / 非法 → 默认
        self.assertEqual(resolve_aspect_ratio(None), DEFAULT_ASPECT_RATIO)
        self.assertEqual(resolve_aspect_ratio(""), DEFAULT_ASPECT_RATIO)
        self.assertEqual(resolve_aspect_ratio("5:7"), DEFAULT_ASPECT_RATIO)
        self.assertEqual(resolve_aspect_ratio("banana"), DEFAULT_ASPECT_RATIO)
        # 容错归一
        self.assertEqual(resolve_aspect_ratio("16:9 "), "16:9")
        self.assertEqual(resolve_aspect_ratio("16x9"), "16:9")
        self.assertEqual(resolve_aspect_ratio("16 / 9"), "16:9")

    def test_ok_fail_shapes(self):
        r = ok(provider="x", model="y")
        self.assertTrue(r.ok)
        self.assertEqual(r.meta, {"provider": "x", "model": "y"})
        self.assertIsNone(r.error_code)
        f = fail("QUOTA", "没了")
        self.assertFalse(f.ok)
        self.assertEqual(f.error_code, "QUOTA")
        self.assertEqual(f.error_message, "没了")
        self.assertEqual(f.files, ())

    def test_genresult_is_frozen(self):
        r = GenResult(ok=True)
        with self.assertRaises(Exception):
            r.ok = False   # frozen dataclass


# ===========================================================================
# 注册表
# ===========================================================================

class TestRegistry(GenTestCase):

    def test_register_list_get(self):
        ds = self.new_ds(FakeOpener({}))
        mm = self.new_mm_img(FakeOpener({}))
        gen_registry.register_provider(ds)
        gen_registry.register_provider(mm)
        self.assertEqual(len(gen_registry.list_providers()), 2)
        self.assertIs(gen_registry.get_provider("dashscope"), ds)
        self.assertIs(gen_registry.get_provider("minimax"), mm)

    def test_register_same_name_overrides(self):
        a = self.new_ds(FakeOpener({}))
        b = self.new_ds(FakeOpener({}))
        gen_registry.register_provider(a)
        gen_registry.register_provider(b)      # 同名覆盖 + debug 日志
        self.assertEqual(len(gen_registry.list_providers()), 1)
        self.assertIs(gen_registry.get_provider("dashscope"), b)

    def test_list_filter_by_kind(self):
        ds = self.new_ds(FakeOpener({}))
        mmv = self.new_mm_video(FakeOpener({}))
        gen_registry.register_provider(ds)
        gen_registry.register_provider(mmv)
        imgs = gen_registry.list_providers(kind="image")
        vids = gen_registry.list_providers(kind="video")
        self.assertEqual([p.name for p in imgs], ["dashscope"])
        self.assertEqual([p.name for p in vids], ["minimax"])
        self.assertEqual(len(imgs) + len(vids), 2)

    def test_get_provider_with_kind_mismatch_returns_none(self):
        ds = self.new_ds(FakeOpener({}))
        gen_registry.register_provider(ds)
        self.assertIsNone(gen_registry.get_provider("dashscope", kind="video"))
        self.assertIs(gen_registry.get_provider("dashscope", kind="image"), ds)

    def test_get_provider_unknown_name_returns_none(self):
        self.assertIsNone(gen_registry.get_provider("nope"))

    def test_scope_isolation(self):
        ds = self.new_ds(FakeOpener({}))
        gen_registry.register_provider(ds, scope="sess-1")
        self.assertEqual(gen_registry.list_providers(), [])          # 全局看不到
        self.assertIsNone(gen_registry.get_provider("dashscope"))    # 全局查不到
        self.assertIs(gen_registry.get_provider("dashscope", scope="sess-1"), ds)
        self.assertEqual(len(gen_registry.list_providers(scope="sess-1")), 1)

    def test_get_active_from_config_dict(self):
        ds = self.new_ds(FakeOpener({}))
        mmv = self.new_mm_video(FakeOpener({}))
        gen_registry.register_provider(ds)
        gen_registry.register_provider(mmv)
        cfg = {"capabilities": {"image_gen": {"provider": "dashscope"},
                                "video_gen": {"provider": "minimax"}}}
        self.assertIs(gen_registry.get_active("image", config=cfg), ds)
        self.assertIs(gen_registry.get_active("video", config=cfg), mmv)

    def test_get_active_unknown_provider_returns_none(self):
        ds = self.new_ds(FakeOpener({}))
        gen_registry.register_provider(ds)
        cfg = {"capabilities": {"image_gen": {"provider": "nonexistent"}}}
        self.assertIsNone(gen_registry.get_active("image", config=cfg))

    def test_get_active_missing_section_returns_none(self):
        ds = self.new_ds(FakeOpener({}))
        gen_registry.register_provider(ds)
        self.assertIsNone(gen_registry.get_active("image", config={"capabilities": {}}))

    def test_register_rejects_bad_provider(self):
        class NoName(gen_provider.GenProvider):
            name = ""
            kind = "image"
            api_key_env = "X"

            def generate(self, **kw):  # pragma: no cover
                raise NotImplementedError

        with self.assertRaises(ValueError):
            gen_registry.register_provider(NoName())
        with self.assertRaises(ValueError):
            gen_registry.register_provider(None)

    def test_registry_thread_safety(self):
        # 8 线程并发注册不同名字，最终一个不少
        class P(gen_provider.GenProvider):
            def __init__(self, n):
                self.name = "p%d" % n
                self.kind = "image"
                self.api_key_env = "X"

            def models(self):
                return {}

            def generate(self, **kw):  # pragma: no cover
                raise NotImplementedError

        def worker(i):
            gen_registry.register_provider(P(i))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(gen_registry.list_providers()), 8)


# ===========================================================================
# dashscope 生图
# ===========================================================================

class TestDashscope(GenTestCase):

    DS_KEY_PREFIX = "https://myws.cn-beijing.maas.aliyuncs.com"

    def test_request_body_and_headers_and_download(self):
        opener = FakeOpener({
            self.DS_KEY_PREFIX: [
                (200, _ds_ok_body(image_url="https://oss.example.com/1.png")),
            ],
            "https://oss.example.com/1.png": [(200, _PNG_1x1)],
        })
        prov = self.new_ds(opener)
        res = prov.generate(prompt="一只猫", model=None, out_dir=self.tmp,
                            aspect_ratio="16:9")
        self.assertTrue(res.ok, res.error_message)
        self.assertEqual(len(res.files), 1)
        self.assertTrue(res.files[0].endswith(".png"))
        with open(res.files[0], "rb") as f:
            self.assertEqual(f.read(), _PNG_1x1)
        # 断言实际发出的请求体
        bodies = opener.json_bodies(self.DS_KEY_PREFIX)
        self.assertEqual(len(bodies), 1)
        b = bodies[0]
        self.assertEqual(b["model"], "wan2.7-image-pro")
        self.assertEqual(b["input"]["messages"][0]["role"], "user")
        self.assertEqual(b["input"]["messages"][0]["content"][0]["text"], "一只猫")
        self.assertEqual(b["parameters"], {"prompt_extend": True})
        # 鉴权头
        call = opener.calls_to(self.DS_KEY_PREFIX)[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer k-ds")
        self.assertEqual(call["method"], "POST")
        # meta
        self.assertEqual(res.meta["provider"], "dashscope")
        self.assertEqual(res.meta["model"], "wan2.7-image-pro")
        self.assertEqual(res.meta["prompt"], "一只猫")
        self.assertEqual(res.meta["aspect_ratio"], "16:9")

    def test_explicit_model_and_endpoint(self):
        opener = FakeOpener({
            self.DS_KEY_PREFIX: [(200, _ds_ok_body(image_url="https://oss.example.com/2.png"))],
            "https://oss.example.com/2.png": [(200, _PNG_1x1)],
        })
        prov = self.new_ds(opener)
        res = prov.generate(prompt="p", model="wan2.7-image", out_dir=self.tmp)
        self.assertTrue(res.ok)
        self.assertEqual(opener.json_bodies(self.DS_KEY_PREFIX)[0]["model"], "wan2.7-image")

    def test_workspace_from_env_when_base_url_none(self):
        opener = FakeOpener({
            "https://envws.cn-beijing.maas.aliyuncs.com": [
                (200, _ds_ok_body(image_url="https://oss.example.com/3.png"))],
            "https://oss.example.com/3.png": [(200, _PNG_1x1)],
        })
        old = os.environ.get("DASHSCOPE_BASE_URL")
        os.environ["DASHSCOPE_BASE_URL"] = "https://envws.cn-beijing.maas.aliyuncs.com"
        try:
            prov = DashscopeImageProvider(api_key="k", opener=opener)
            res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
            self.assertTrue(res.ok)
            self.assertEqual(len(opener.calls_to(
                "https://envws.cn-beijing.maas.aliyuncs.com")), 1)
        finally:
            if old is None:
                os.environ.pop("DASHSCOPE_BASE_URL", None)
            else:
                os.environ["DASHSCOPE_BASE_URL"] = old

    def test_workspace_parse_failure_raises_not_configured(self):
        prov = DashscopeImageProvider(api_key="k", opener=FakeOpener({}),
                                      base_url="not-a-url")
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "NOT_CONFIGURED")

    def test_missing_key_returns_not_configured(self):
        prov = DashscopeImageProvider(api_key=None, opener=FakeOpener({}))
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "NOT_CONFIGURED")

    def test_unknown_model_rejected_without_network(self):
        opener = FakeOpener({})
        prov = self.new_ds(opener)
        res = prov.generate(prompt="p", model="nope-model", out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "BAD_REQUEST")
        self.assertEqual(opener.calls, [])     # 未发任何请求

    def test_http_error_maps_to_provider_error(self):
        opener = FakeOpener({self.DS_KEY_PREFIX: [(500, {"error": "boom"})]})
        prov = self.new_ds(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "PROVIDER_ERROR")
        self.assertIn("500", res.error_message)

    def test_missing_image_field_maps_to_provider_error(self):
        opener = FakeOpener({self.DS_KEY_PREFIX: [(200, {"output": {"choices": []}})]})
        prov = self.new_ds(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "PROVIDER_ERROR")

    def test_cancel_before_submit(self):
        opener = FakeOpener({})
        prov = self.new_ds(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp,
                            is_cancelled=lambda: True)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "CANCELLED")
        self.assertEqual(opener.calls, [])

    def test_cancel_after_generate_before_download(self):
        opener = FakeOpener({
            self.DS_KEY_PREFIX: [(200, _ds_ok_body(image_url="https://oss.example.com/4.png"))],
        })
        calls = {"n": 0}

        def cancelled():
            calls["n"] += 1
            return calls["n"] > 1     # 提交前未取消；生成回来后（下载前）取消

        prov = self.new_ds(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp,
                            is_cancelled=cancelled)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "CANCELLED")
        self.assertEqual(len(opener.calls_to(self.DS_KEY_PREFIX)), 1)   # 生成发了，下载没发


# ===========================================================================
# MiniMax 生图
# ===========================================================================

class TestMiniMaxImage(GenTestCase):

    MM = "https://api.minimaxi.com/v1/image_generation"

    def test_request_body_fields(self):
        opener = FakeOpener({
            self.MM: [(200, _mm_ok_body("https://mm.example.com/a.png"))],
            "https://mm.example.com/a.png": [(200, _PNG_1x1)],
        })
        prov = self.new_mm_img(opener)
        res = prov.generate(prompt="一只狗", model=None, out_dir=self.tmp,
                             aspect_ratio="9:16")
        self.assertTrue(res.ok, res.error_message)
        b = opener.json_bodies(self.MM)[0]
        # 契约字段逐个断言
        self.assertEqual(b["model"], "image-01")
        self.assertEqual(b["prompt"], "一只狗")
        self.assertEqual(b["n"], 1)
        self.assertEqual(b["aspect_ratio"], "9:16")
        self.assertEqual(b["response_format"], "url")
        # 鉴权头
        call = opener.calls_to(self.MM)[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer k-mm")
        # 落盘
        self.assertEqual(len(res.files), 1)
        with open(res.files[0], "rb") as f:
            self.assertEqual(f.read(), _PNG_1x1)
        self.assertEqual(res.meta["provider"], "minimax")
        self.assertEqual(res.meta["model"], "image-01")

    def test_references_switch_to_live_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = os.path.join(tmp, "ref.png")
            with open(ref, "wb") as f:
                f.write(_PNG_1x1)
            opener = FakeOpener({
                self.MM: [(200, _mm_ok_body("https://mm.example.com/b.png"))],
                "https://mm.example.com/b.png": [(200, _PNG_1x1)],
            })
            prov = self.new_mm_img(opener)
            res = prov.generate(prompt="p", model="image-01", out_dir=self.tmp,
                                references=[ref])
            self.assertTrue(res.ok)
            b = opener.json_bodies(self.MM)[0]
            self.assertEqual(b["model"], "image-01-live")     # 自动切 live
            self.assertIn("image_urls", b)
            self.assertEqual(len(b["image_urls"]), 1)
            self.assertTrue(b["image_urls"][0].startswith("data:image/png;base64,"))
            self.assertEqual(res.meta["model"], "image-01-live")

    def test_http_ref_passthrough(self):
        opener = FakeOpener({
            self.MM: [(200, _mm_ok_body("https://mm.example.com/c.png"))],
            "https://mm.example.com/c.png": [(200, _PNG_1x1)],
        })
        prov = self.new_mm_img(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp,
                            references=["https://r.example.com/x.jpg"])
        self.assertTrue(res.ok)
        b = opener.json_bodies(self.MM)[0]
        self.assertEqual(b["model"], "image-01-live")
        self.assertEqual(b["image_urls"], ["https://r.example.com/x.jpg"])

    def test_base_resp_2056_maps_to_quota(self):
        opener = FakeOpener({self.MM: [(200, _mm_err_body(2056, "quota exhausted"))]})
        prov = self.new_mm_img(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "QUOTA")
        self.assertIn("2056", res.error_message)

    def test_base_resp_1004_maps_to_auth(self):
        opener = FakeOpener({self.MM: [(200, _mm_err_body(1004, "invalid api key"))]})
        prov = self.new_mm_img(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "AUTH")

    def test_base_resp_1026_and_1xxx_map_to_bad_request(self):
        for sc in (1026, 1027, 1113):
            opener = FakeOpener({self.MM: [(200, _mm_err_body(sc, "content filter"))]})
            prov = self.new_mm_img(opener)
            res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
            self.assertFalse(res.ok, sc)
            self.assertEqual(res.error_code, "BAD_REQUEST", sc)

    def test_base_resp_other_maps_to_provider_error(self):
        opener = FakeOpener({self.MM: [(200, _mm_err_body(3011, "internal"))]})
        prov = self.new_mm_img(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "PROVIDER_ERROR")

    def test_missing_key(self):
        prov = MiniMaxImageProvider(api_key=None, opener=FakeOpener({}))
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertEqual(res.error_code, "NOT_CONFIGURED")

    def test_bad_aspect_ratio_falls_back(self):
        opener = FakeOpener({
            self.MM: [(200, _mm_ok_body("https://mm.example.com/d.png"))],
            "https://mm.example.com/d.png": [(200, _PNG_1x1)],
        })
        prov = self.new_mm_img(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp,
                             aspect_ratio="17:33")
        self.assertTrue(res.ok)
        self.assertEqual(opener.json_bodies(self.MM)[0]["aspect_ratio"], "1:1")

    def test_models_catalog(self):
        prov = self.new_mm_img(FakeOpener({}))
        models = prov.models()
        self.assertIn("image-01", models)
        self.assertIn("image-01-live", models)
        ds = self.new_ds(FakeOpener({}))
        self.assertIn("wan2.7-image-pro", ds.models())
        self.assertIn("wan2.7-image", ds.models())
        v = self.new_mm_video(FakeOpener({}))
        self.assertIn("MiniMax-Hailuo-2.3", v.models())


# ===========================================================================
# MiniMax 生视频
# ===========================================================================

class TestMiniMaxVideo(GenTestCase):

    SUBMIT = "https://api.minimaxi.com/v1/video_generation"
    QUERY = "https://api.minimaxi.com/v1/query/video_generation"
    RETRIEVE = "https://api.minimaxi.com/v1/files/retrieve"

    def _script_ok(self, statuses=("InQueue", "InProgress", "Success")):
        """提交 → 轮询序列 → retrieve → download 的完整脚本。"""
        polls = []
        for i, st in enumerate(statuses):
            polls.append((200, {
                "base_resp": {"status_code": 0, "status_msg": ""},
                "task_id": "task-1",
                "status": st,
                "file_id": "file-1" if st == "Success" else None,
            }))
        return {
            self.SUBMIT: [(200, {"base_resp": {"status_code": 0, "status_msg": ""},
                                 "task_id": "task-1"})],
            self.QUERY: polls,
            self.RETRIEVE: [(200, {"base_resp": {"status_code": 0, "status_msg": ""},
                                   "file": {"file_id": "file-1",
                                            "download_url": "https://mm.example.com/v.mp4"}})],
            "https://mm.example.com/v.mp4": [(200, b"\x00\x00\x00\x18ftypmp42" + b"body")],
        }

    def test_success_flow_and_meta(self):
        opener = FakeOpener(self._script_ok())
        prov = self.new_mm_video(opener)
        res = prov.generate(prompt="日落", model=None, out_dir=self.tmp,
                            params={"duration": 6})
        self.assertTrue(res.ok, res.error_message)
        # 提交只发生一次（契约：提交即扣配额）
        self.assertEqual(len(opener.calls_to(self.SUBMIT)), 1)
        # 轮询至少 3 次（InQueue→InProgress→Success）
        self.assertGreaterEqual(len(opener.calls_to(self.QUERY)), 3)
        # 请求体
        b = opener.json_bodies(self.SUBMIT)[0]
        self.assertEqual(b["model"], "MiniMax-Hailuo-2.3")
        self.assertEqual(b["prompt"], "日落")
        self.assertEqual(b["duration"], 6)
        self.assertNotIn("aspect_ratio", b)     # Hailuo 不吃这个字段
        # 轮询 URL 用斜杠路径 + task_id 参数
        qcalls = opener.calls_to(self.QUERY)
        for c in qcalls:
            self.assertIn("/v1/query/video_generation?task_id=task-1", c["url"])
            self.assertEqual(c["method"], "GET")
        # retrieve
        rcalls = opener.calls_to(self.RETRIEVE)
        self.assertEqual(len(rcalls), 1)
        self.assertIn("file_id=file-1", rcalls[0]["url"])
        # 落盘 mp4
        self.assertEqual(len(res.files), 1)
        self.assertTrue(res.files[0].endswith(".mp4"))
        with open(res.files[0], "rb") as f:
            self.assertEqual(f.read(), b"\x00\x00\x00\x18ftypmp42" + b"body")
        # meta
        self.assertEqual(res.meta["provider"], "minimax")
        self.assertEqual(res.meta["model"], "MiniMax-Hailuo-2.3")
        self.assertEqual(res.meta["prompt"], "日落")
        self.assertEqual(res.meta["duration"], 6)
        self.assertEqual(res.meta["raw_ids"], {"task_id": "task-1", "file_id": "file-1"})

    def test_submit_quota_2056_stops_immediately(self):
        opener = FakeOpener({
            self.SUBMIT: [(200, {"base_resp": {"status_code": 2056,
                                               "status_msg": "quota exhausted"},
                                 "task_id": "task-should-not-be-used"})],
        })
        prov = self.new_mm_video(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "QUOTA")
        # 2056 → 立即停止：没有轮询、没有下载
        self.assertEqual(opener.calls_to(self.QUERY), [])
        self.assertEqual(len(opener.calls_to(self.SUBMIT)), 1)

    def test_status_fail_reports_poll_failed_no_resubmit(self):
        opener = FakeOpener({
            self.SUBMIT: [(200, {"base_id": "x", "base_resp": {"status_code": 0},
                                 "task_id": "task-1"})],
            self.QUERY: [(200, {"base_resp": {"status_code": 0},
                                "task_id": "task-1", "status": "Fail"})],
        })
        prov = self.new_mm_video(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "POLL_FAILED")
        # 绝不重新提交
        self.assertEqual(len(opener.calls_to(self.SUBMIT)), 1)

    def test_poll_transport_error_no_resubmit_after_retries(self):
        # 轮询持续 HTTP 500：最多重试 2 次（共 3 次轮询尝试）后 POLL_FAILED
        opener = FakeOpener({
            self.SUBMIT: [(200, {"base_resp": {"status_code": 0}, "task_id": "task-1"})],
            self.QUERY: [(500, {"error": "boom"})] * 10,
        })
        prov = self.new_mm_video(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "POLL_FAILED")
        # 关键断言：提交只发生一次（提交即扣配额，轮询失败绝不允许重新提交）
        self.assertEqual(len(opener.calls_to(self.SUBMIT)), 1)
        # 轮询恰好 3 次（首次 + 2 次重试）
        self.assertEqual(len(opener.calls_to(self.QUERY)), 3)

    def test_poll_timeout_reports_poll_failed(self):
        # 一直 InQueue，poll_total_limit_s 拉到 0 → 立刻超时
        opener = FakeOpener({
            self.SUBMIT: [(200, {"base_resp": {"status_code": 0}, "task_id": "task-1"})],
            self.QUERY: [(200, {"base_resp": {"status_code": 0},
                                "task_id": "task-1", "status": "InQueue"})] * 5,
        })
        prov = self.new_mm_video(opener, poll_total_limit_s=0.0)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "POLL_FAILED")
        self.assertIn("轮询超时 0s", res.error_message)      # 实例的上限值进消息
        self.assertIn("不重新提交", res.error_message)
        self.assertEqual(len(opener.calls_to(self.SUBMIT)), 1)

    def test_cancel_during_poll(self):
        opener = FakeOpener({
            self.SUBMIT: [(200, {"base_resp": {"status_code": 0}, "task_id": "task-1"})],
            self.QUERY: [(200, {"base_resp": {"status_code": 0},
                                "task_id": "task-1", "status": "InQueue"})] * 10,
        })
        polls = {"n": 0}

        def cancelled():
            return polls["n"] >= 2     # 两次轮询之后（第三次轮询前）取消

        prov = self.new_mm_video(opener)
        # 让 is_cancelled 只在轮询点被感知：包装 opener 计轮询次数
        real_opener = opener

        def counting_opener(url, *a, **kw):
            res = real_opener(url, *a, **kw)
            if url.startswith(self.QUERY):
                polls["n"] += 1
            return res

        opener2 = counting_opener
        prov.opener = opener2
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp,
                            is_cancelled=cancelled)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "CANCELLED")
        self.assertIn("task-1", res.error_message)
        # 提交只发生一次；轮询停了
        self.assertEqual(len(opener.calls_to(self.SUBMIT)), 1)
        self.assertEqual(polls["n"], 2)

    def test_cancel_before_submit(self):
        opener = FakeOpener({})
        prov = self.new_mm_video(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp,
                            is_cancelled=lambda: True)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "CANCELLED")
        self.assertEqual(opener.calls, [])

    def test_submit_http_error(self):
        opener = FakeOpener({self.SUBMIT: [(500, {"base_resp": {"status_code": 0}})]})
        prov = self.new_mm_video(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "PROVIDER_ERROR")

    def test_submit_missing_task_id(self):
        opener = FakeOpener({self.SUBMIT: [(200, {"base_resp": {"status_code": 0}})]})
        prov = self.new_mm_video(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "PROVIDER_ERROR")
        self.assertEqual(opener.calls_to(self.QUERY), [])

    def test_unknown_model_rejected_without_network(self):
        opener = FakeOpener({})
        prov = self.new_mm_video(opener)
        res = prov.generate(prompt="p", model="Hailuo-999", out_dir=self.tmp)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "BAD_REQUEST")
        self.assertEqual(opener.calls, [])

    def test_missing_key(self):
        prov = MiniMaxVideoProvider(api_key=None, opener=FakeOpener({}), sleep=lambda s: None)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertEqual(res.error_code, "NOT_CONFIGURED")

    def test_query_uses_slash_path_not_underscore(self):
        # 防回归：轮询路径必须是 /v1/query/video_generation（下划线写法 404）
        opener = FakeOpener(self._script_ok())
        prov = self.new_mm_video(opener)
        res = prov.generate(prompt="p", model=None, out_dir=self.tmp)
        self.assertTrue(res.ok)
        for c in opener.calls:
            self.assertNotIn("query_video_generation", c["url"])

    def test_gen_error_codes_constant_matches_contract(self):
        self.assertEqual(set(GEN_ERROR_CODES), {
            "AUTH", "QUOTA", "BAD_REQUEST", "PROVIDER_ERROR", "NETWORK", "TIMEOUT",
            "CANCELLED", "POLL_FAILED", "NOT_CONFIGURED"})


class ImageFormatSniffTest(unittest.TestCase):
    """扩展名必须服从字节，不服从调用方。

    回归（2026-09-23 真机实测）：MiniMax image-01 回的是 JPEG，provider 写死
    .png 落盘 → 用户工作区里一个 .png 文件装的是 JPEG，扩展名撒谎。
    """

    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    GIF = b"GIF89a" + b"\x00" * 32
    WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def test_sniff_known_formats(self):
        for blob, ext in ((self.PNG, "png"), (self.JPG, "jpg"),
                          (self.GIF, "gif"), (self.WEBP, "webp")):
            self.assertEqual(gen_provider.sniff_image_ext(blob), ext)

    def test_sniff_unknown_falls_back_to_default(self):
        self.assertEqual(gen_provider.sniff_image_ext(b"NOT-AN-IMAGE", default="png"), "png")
        self.assertEqual(gen_provider.sniff_image_ext(b"", default="jpg"), "jpg")

    def test_jpeg_bytes_never_saved_as_png(self):
        b64 = base64.b64encode(self.JPG).decode()
        path = gen_provider.save_b64_image(b64, self.dir, fmt="png")
        self.assertTrue(path.endswith(".jpg"), path)
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(3), b"\xff\xd8\xff")

    def test_png_bytes_stay_png(self):
        b64 = base64.b64encode(self.PNG).decode()
        self.assertTrue(gen_provider.save_b64_image(b64, self.dir, fmt="png").endswith(".png"))

    def test_download_suffix_follows_bytes(self):
        class DL(gen_provider.GenProvider):
            name = "dl"
            kind = "image"
            api_key_env = "X"

            def models(self):
                return {}

            def generate(self, **kw):
                raise AssertionError("not called")

        prov = DL(api_key="k", opener=lambda url, **kw: (200, self.JPG))
        path = prov._download_to_file("https://example.com/pic.png", self.dir, suffix=".png")
        self.assertTrue(path.endswith(".jpg"), path)

    def test_download_non_200_still_raises(self):
        class DL(gen_provider.GenProvider):
            name = "dl2"
            kind = "image"
            api_key_env = "X"

            def models(self):
                return {}

            def generate(self, **kw):
                raise AssertionError("not called")

        prov = DL(api_key="k", opener=lambda url, **kw: (500, b"nope"))
        with self.assertRaises(gen_provider.GenError):
            prov._download_to_file("https://example.com/pic.png", self.dir, suffix=".png")


if __name__ == "__main__":
    unittest.main()

