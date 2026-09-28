"""capabilities（vision_provider / vision / minimax_vision）的单元测试。

跑法（两种都支持）：
    cd <repo> && python3 -m unittest tests.test_vision -v
    python3 tests/test_vision.py

硬约束（照契约第 10 节）：
  * 不打真网络：所有 opener 都是假 opener（本文件没有任何真 HTTP 代码路径），
    绝不烧用户配额。
  * 不写项目目录：临时文件一律 tempfile。
  * 断言实际发出的 JSON（content 数组里 image_url.url 以 data:image/ 开头、
    text 块内容等于问题）。
  * 能力门：模型未声明图片能力 → MODEL_NO_IMAGE_INPUT 且没有发出任何请求。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

from PIL import Image  # noqa: E402

import attachments  # noqa: E402
from attachments import AttachmentStore  # noqa: E402

import capabilities.vision as vision_mod  # noqa: E402
from capabilities import vision_provider as vp  # noqa: E402
from capabilities.providers import MiniMaxVisionProvider  # noqa: E402
from capabilities.providers import minimax_vision as mv  # noqa: E402


# ---------------------------------------------------------------------------
# 测试助手
# ---------------------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_bytes(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def b64_png(width: int = 8, height: int = 8) -> str:
    import base64
    return "data:image/png;base64," + base64.b64encode(
        png_bytes(width, height)).decode("ascii")


class RecordingOpener:
    """假 opener：记录每次调用参数，返回预设响应。绝不打真网络。

    响应可以是 (status, body_bytes) 或 (status, dict)（自动 dumps），
    或一个异常实例（调用时抛出）。
    """

    def __init__(self, responses):
        self.calls = []
        self._responses = list(responses)

    def __call__(self, url, data=None, headers=None, timeout=None, method="GET"):
        self.calls.append({
            "url": url, "data": data, "headers": dict(headers or {}),
            "timeout": timeout, "method": method,
        })
        if not self._responses:
            raise AssertionError("假 opener 被调用了超出预设次数的请求")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        status, body = resp
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        return int(status), body


def ok_body(content: str, usage: dict | None = None) -> dict:
    body = {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": usage or {"total_tokens": 42},
    }
    return body


class FakeStore:
    """临时目录里的真实 AttachmentStore（tempfile，不写项目目录）。"""

    def __init__(self):
        self._tmp = tempfile.mkdtemp(prefix="vision_test_")
        self.store = AttachmentStore(self._tmp)

    def path(self):
        path = os.path.join(self._tmp, "img.png")
        with open(path, "wb") as f:
            f.write(png_bytes())
        return path


def _write_png(path: str) -> str:
    with open(path, "wb") as f:
        f.write(png_bytes())
    return path


def fake_vision_cfg(**over) -> vision_mod.VisionConfig:
    base = dict(provider="minimax", model="MiniMax-M3",
                max_input_bytes=20 * 1024 * 1024,
                resize_target_bytes=5 * 1024 * 1024,
                max_dimension=2048, timeout_seconds=120)
    base.update(over)
    return vision_mod.VisionConfig(**base)


def make_temp_root_with_config(cfg_json: str) -> str:
    """造一个临时 root：里面放 config.json（内容 cfg_json）。用完由测试清理。"""
    root = tempfile.mkdtemp(prefix="vision_cfg_")
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as f:
        f.write(cfg_json)
    return root


# ---------------------------------------------------------------------------
# minimax_vision：发出的 JSON / 错误映射 / 思考分流
# ---------------------------------------------------------------------------

class TestMiniMaxVisionPayload(unittest.TestCase):

    def setUp(self):
        self.p = MiniMaxVisionProvider()
        self._old_key = os.environ.get("MINIMAX_CN_API_KEY")
        os.environ["MINIMAX_CN_API_KEY"] = "test-key-not-real"

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("MINIMAX_CN_API_KEY", None)
        else:
            os.environ["MINIMAX_CN_API_KEY"] = self._old_key

    def _run(self, opener, question="图里有什么？", model=None):
        return self.p.describe(data_url=b64_png(), question=question,
                               model=model, opener=opener)

    def test_payload_shape_and_content_array(self):
        """实际发出的 JSON：model/max_tokens/messages/content 数组形状照契约 8.3。"""
        opener = RecordingOpener([(200, ok_body("一只猫"))])
        result = self._run(opener, question="图里有什么？", model="MiniMax-M3")
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(len(opener.calls), 1)
        call = opener.calls[0]
        self.assertEqual(call["url"], "https://api.minimaxi.com/v1/chat/completions")
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        self.assertTrue(call["headers"]["Authorization"].startswith("Bearer "))
        self.assertGreater(call["timeout"], 0)
        sent = json.loads(call["data"].decode("utf-8"))
        self.assertEqual(sent["model"], "MiniMax-M3")
        self.assertEqual(sent["max_tokens"], 1024)
        self.assertEqual(len(sent["messages"]), 1)
        msg = sent["messages"][0]
        self.assertEqual(msg["role"], "user")
        content = msg["content"]
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0]["type"], "image_url")
        self.assertTrue(content[0]["image_url"]["url"].startswith("data:image/"))
        self.assertEqual(content[1]["type"], "text")
        self.assertEqual(content[1]["text"], "图里有什么？")

    def test_payload_model_default_when_none(self):
        """不传 model 时默认 MiniMax-M3。"""
        opener = RecordingOpener([(200, ok_body("ok"))])
        result = self._run(opener)
        self.assertTrue(result.ok)
        sent = json.loads(opener.calls[0]["data"].decode("utf-8"))
        self.assertEqual(sent["model"], "MiniMax-M3")

    def test_non_image_data_url_rejected_without_request(self):
        """非 data:image/* 的 data_url 直接 UNSUPPORTED_IMAGE，不发请求。"""
        opener = RecordingOpener([])
        result = self.p.describe(data_url="data:text/plain;base64,QUJD",
                                 question="q", opener=opener)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "UNSUPPORTED_IMAGE")
        self.assertEqual(len(opener.calls), 0)

    def test_no_api_key_env_not_configured(self):
        """MINIMAX_CN_API_KEY 未设置 → NOT_CONFIGURED，不发请求。"""
        old = os.environ.pop("MINIMAX_CN_API_KEY", None)
        try:
            opener = RecordingOpener([])
            result = self.p.describe(data_url=b64_png(), question="q", opener=opener)
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "NOT_CONFIGURED")
            self.assertEqual(len(opener.calls), 0)
        finally:
            if old is not None:
                os.environ["MINIMAX_CN_API_KEY"] = old


class TestMiniMaxVisionErrorMapping(unittest.TestCase):

    def setUp(self):
        self.p = MiniMaxVisionProvider()
        self._old_key = os.environ.get("MINIMAX_CN_API_KEY")
        os.environ["MINIMAX_CN_API_KEY"] = "test-key-not-real"

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("MINIMAX_CN_API_KEY", None)
        else:
            os.environ["MINIMAX_CN_API_KEY"] = self._old_key

    def _run(self, opener):
        return self.p.describe(data_url=b64_png(), question="q", opener=opener)

    def test_http_401_maps_to_auth(self):
        result = self._run(RecordingOpener([(401, b'{"base_resp":{"status_code":1001}}')]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "AUTH")

    def test_http_403_maps_to_auth(self):
        result = self._run(RecordingOpener([(403, b"forbidden")]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "AUTH")

    def test_base_resp_1004_maps_to_auth(self):
        result = self._run(RecordingOpener(
            [(200, {"base_resp": {"status_code": 1004, "status_msg": "invalid api key"}})]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "AUTH")
        self.assertIn("1004", result.error_message or "")

    def test_base_resp_2056_maps_to_provider_error_quota(self):
        result = self._run(RecordingOpener(
            [(200, {"base_resp": {"status_code": 2056, "status_msg": "quota exceeded"}})]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PROVIDER_ERROR")
        self.assertIn("2056", result.error_message or "")

    def test_base_resp_other_maps_to_provider_error(self):
        result = self._run(RecordingOpener(
            [(200, {"base_resp": {"status_code": 1008, "status_msg": "boom"}})]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PROVIDER_ERROR")

    def test_base_resp_zero_is_success(self):
        body = ok_body("答：4713", usage={"total_tokens": 7})
        body["base_resp"] = {"status_code": 0}
        result = self._run(RecordingOpener([(200, body)]))
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "答：4713")

    def test_timeout_maps_to_timeout(self):
        import socket as _socket
        result = self._run(RecordingOpener([_socket.timeout("timed out")]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TIMEOUT")

    def test_connection_error_maps_to_network(self):
        result = self._run(RecordingOpener([ConnectionRefusedError("refused")]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NETWORK")

    def test_http_500_no_base_resp_maps_to_provider_error(self):
        result = self._run(RecordingOpener([(500, b"internal error")]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PROVIDER_ERROR")

    def test_invalid_json_maps_to_provider_error(self):
        result = self._run(RecordingOpener([(200, b"not json at all")]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PROVIDER_ERROR")

    def test_missing_choices_maps_to_provider_error(self):
        result = self._run(RecordingOpener([(200, {"foo": "bar"})]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PROVIDER_ERROR")

    def test_cancelled_before_request(self):
        opener = RecordingOpener([])
        result = self.p.describe(data_url=b64_png(), question="q",
                                 opener=opener, is_cancelled=lambda: True)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "CANCELLED")
        self.assertEqual(len(opener.calls), 0)


class TestReasoningSplit(unittest.TestCase):
    """正文里带思考标签：正文与思考被正确分流且不丢字。"""

    def setUp(self):
        self.p = MiniMaxVisionProvider()
        self._old_key = os.environ.get("MINIMAX_CN_API_KEY")
        os.environ["MINIMAX_CN_API_KEY"] = "test-key-not-real"

    def tearDown(self):
        if self._old_key is None:
            os.environ.pop("MINIMAX_CN_API_KEY", None)
        else:
            os.environ["MINIMAX_CN_API_KEY"] = self._old_key

    def test_thinking_tag_split_no_content_loss(self):
        inner = "用户问数字，我仔细看图，应该是4713。"
        content = "<thinking>%s</thinking>图中数字是 4713，左边蓝色方块，右边红色圆。" % inner
        result = self.p.describe(data_url=b64_png(), question="数字是几？",
                                 opener=RecordingOpener([(200, ok_body(content))]))
        self.assertTrue(result.ok)
        self.assertEqual(result.reasoning, inner)
        self.assertEqual(result.text, "图中数字是 4713，左边蓝色方块，右边红色圆。")

    def test_mixed_leading_and_trailing_content_preserved(self):
        content = "前面这行是正文。<think>中间是思考</think>后面也是正文。"
        result = self.p.describe(data_url=b64_png(), question="q",
                                 opener=RecordingOpener([(200, ok_body(content))]))
        self.assertTrue(result.ok)
        self.assertEqual(result.reasoning, "中间是思考")
        self.assertEqual(result.text, "前面这行是正文。后面也是正文。")

    def test_no_tag_all_in_text(self):
        result = self.p.describe(data_url=b64_png(), question="q",
                                 opener=RecordingOpener([(200, ok_body("纯正文"))]))
        self.assertTrue(result.ok)
        self.assertEqual(result.reasoning, "")
        self.assertEqual(result.text, "纯正文")

    def test_split_content_text_helper(self):
        r, t = vp.split_content_text("<thinking>abc</thinking>xyz")
        self.assertEqual((r, t), ("abc", "xyz"))


# ---------------------------------------------------------------------------
# vision.py：编排 / 能力门 / 配置 / 注册表
# ---------------------------------------------------------------------------

class TestVisionAnalyzeOrchestration(unittest.TestCase):

    def setUp(self):
        vision_mod._reset_for_tests()
        self._old_key = os.environ.get("MINIMAX_CN_API_KEY")
        os.environ["MINIMAX_CN_API_KEY"] = "test-key-not-real"
        self._tmp = tempfile.mkdtemp(prefix="vision_orch_")
        self.store = AttachmentStore(self._tmp)
        self.img_path = _write_png(os.path.join(self._tmp, "img.png"))
        # 能力门要过：造一个临时 root，模型声明 image 输入
        self.cfg_root = make_temp_root_with_config(json.dumps({
            "capabilities": {
                "vision": {"provider": "minimax", "model": "MiniMax-M3",
                           "maxInputBytes": 20971520, "resizeTargetBytes": 5242880,
                           "maxDimension": 2048, "timeoutSeconds": 120},
                "models": {"MiniMax-M3": {"inputModalities": ["text", "image"]}},
            }
        }))
        # 换掉注册表里的 provider 为假 provider（记录调用、绝不打网络）
        self.fake = FakeProvider("minimax", "MINIMAX_CN_API_KEY")
        vision_mod.register_provider(self.fake)

    def tearDown(self):
        vision_mod._reset_for_tests()
        if self._old_key is None:
            os.environ.pop("MINIMAX_CN_API_KEY", None)
        else:
            os.environ["MINIMAX_CN_API_KEY"] = self._old_key
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self.cfg_root, ignore_errors=True)

    def test_success_path_meta_and_attachment_uri(self):
        self.fake.next_result = vp.ok_result(
            text="一只猫", reasoning="", provider="minimax", model="MiniMax-M3",
            meta={"usage": {"total_tokens": 5}})
        result = vision_mod.vision_analyze(
            store=self.store, source=self.img_path, question="图里有什么？",
            config=fake_vision_cfg(), root=self.cfg_root)
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(result.text, "一只猫")
        self.assertEqual(result.provider, "minimax")
        self.assertEqual(result.model, "MiniMax-M3")
        self.assertIsNotNone(result.attachment_uri)
        self.assertTrue(result.attachment_uri.startswith("attach:"))
        self.assertIn("usage", result.meta)
        self.assertEqual(result.meta["usage"], {"total_tokens": 5})
        self.assertIn("normalize", result.meta)
        self.assertIn("elapsed_ms", result.meta)
        self.assertGreaterEqual(result.meta["elapsed_ms"], 0)
        # 发出的 data_url 确实来自 normalize 后那份附件
        sent = self.fake.calls[0]
        self.assertTrue(sent["data_url"].startswith("data:image/"))
        self.assertEqual(sent["question"], "图里有什么？")
        self.assertEqual(sent["model"], "MiniMax-M3")
        self.assertEqual(sent["timeout"], 120)

    def test_source_attach_uri_reference(self):
        ref = self.store.save_file(self.img_path)
        self.fake.next_result = vp.ok_result(text="ok")
        result = vision_mod.vision_analyze(
            store=self.store, source=ref.uri(), question="q",
            config=fake_vision_cfg(), root=self.cfg_root)
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(self.fake.calls[0]["data_url"].startswith("data:image/"), True)

    def test_provider_not_registered(self):
        vision_mod._reset_for_tests()
        result = vision_mod.vision_analyze(
            store=self.store, source=self.img_path, question="q",
            config=fake_vision_cfg(provider="nope"), root=self.cfg_root)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "NOT_CONFIGURED")

    def test_no_api_key_not_configured(self):
        os.environ.pop("MINIMAX_CN_API_KEY", None)
        try:
            result = vision_mod.vision_analyze(
                store=self.store, source=self.img_path, question="q",
                config=fake_vision_cfg(), root=self.cfg_root)
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code, "NOT_CONFIGURED")
            self.assertEqual(len(self.fake.calls), 0)
        finally:
            os.environ["MINIMAX_CN_API_KEY"] = self._old_key or ""

    def test_attachment_error_maps_code(self):
        """normalize 抛 AttachmentError → 映射 IMAGE_TOO_LARGE / UNSUPPORTED_IMAGE。"""
        # 造一张超硬顶的「图片」：mime 标 image 但字节超 max_input_bytes
        # save_bytes sniff 出 mime 才算 image；PNG 头 + 大垃圾字节
        big = png_bytes(8, 8) + b"\x00" * (1024)  # 只是要超过小 policy
        ref = self.store.save_bytes(big)
        tiny_cfg = fake_vision_cfg(max_input_bytes=16, resize_target_bytes=8,
                                   max_dimension=64)
        result = vision_mod.vision_analyze(
            store=self.store, source=ref.uri(), question="q",
            config=tiny_cfg, root=self.cfg_root)
        self.assertFalse(result.ok)
        self.assertIn(result.error_code, ("IMAGE_TOO_LARGE", "UNSUPPORTED_IMAGE"))
        self.assertEqual(len(self.fake.calls), 0)

    def test_provider_failure_passthrough(self):
        self.fake.next_result = vp.fail_result("PROVIDER_ERROR", "上游挂了")
        result = vision_mod.vision_analyze(
            store=self.store, source=self.img_path, question="q",
            config=fake_vision_cfg(), root=self.cfg_root)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PROVIDER_ERROR")
        self.assertEqual(result.error_message, "a " * 0 + "上游挂了")
        # 失败结果也带 attachment_uri 与 elapsed_ms
        self.assertTrue((result.attachment_uri or "").startswith("attach:"))
        self.assertIn("elapsed_ms", result.meta)


class TestCapabilityGate(unittest.TestCase):
    """能力门（dsh 式）：模型未声明图片能力 → MODEL_NO_IMAGE_INPUT，且没发任何请求。"""

    def setUp(self):
        vision_mod._reset_for_tests()
        self._old_key = os.environ.get("MINIMAX_CN_API_KEY")
        os.environ["MINIMAX_CN_API_KEY"] = "test-key-not-real"
        self._tmp = tempfile.mkdtemp(prefix="vision_gate_")
        self.store = AttachmentStore(self._tmp)
        self.img_path = _write_png(os.path.join(self._tmp, "img.png"))
        self.fake = FakeProvider("minimax", "MINIMAX_CN_API_KEY")
        vision_mod.register_provider(self.fake)
        # root：模型只声明 text（没有 image）
        self.gate_root = make_temp_root_with_config(json.dumps({
            "capabilities": {
                "vision": {"provider": "minimax", "model": "MiniMax-M3",
                           "maxInputBytes": 20971520, "resizeTargetBytes": 5242880,
                           "maxDimension": 2048, "timeoutSeconds": 120},
                "models": {"MiniMax-M3": {"inputModalities": ["text"]}},
            }
        }))

    def tearDown(self):
        vision_mod._reset_for_tests()
        if self._old_key is None:
            os.environ.pop("MINIMAX_CN_API_KEY", None)
        else:
            os.environ["MINIMAX_CN_API_KEY"] = self._old_key
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        shutil.rmtree(self.gate_root, ignore_errors=True)

    def test_model_without_image_input_rejected_no_request(self):
        result = vision_mod.vision_analyze(
            store=self.store, source=self.img_path, question="q",
            config=fake_vision_cfg(), root=self.gate_root)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "MODEL_NO_IMAGE_INPUT")
        # 没有发出任何请求
        self.assertEqual(len(self.fake.calls), 0)
        # 报文里写清是哪个模型没声明图片能力
        self.assertIn("MiniMax-M3", result.error_message or "")

    def test_model_not_declared_at_all_rejected(self):
        """模型连条目都没有 → 同样拒绝。"""
        cfg = fake_vision_cfg(model="Some-Other-Model")
        result = vision_mod.vision_analyze(
            store=self.store, source=self.img_path, question="q",
            config=cfg, root=self.gate_root)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "MODEL_NO_IMAGE_INPUT")
        self.assertEqual(len(self.fake.calls), 0)
        self.assertIn("Some-Other-Model", result.error_message or "")

    def test_model_with_image_declared_passes_gate(self):
        root = make_temp_root_with_config(json.dumps({
            "capabilities": {
                "vision": {"provider": "minimax", "model": "MiniMax-M3"},
                "models": {"MiniMax-M3": {"inputModalities": ["text", "image"]}},
            }
        }))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        self.fake.next_result = vp.ok_result(text="过了")
        result = vision_mod.vision_analyze(
            store=self.store, source=self.img_path, question="q",
            config=fake_vision_cfg(), root=root)
        self.assertTrue(result.ok, result.error_message)
        self.assertEqual(len(self.fake.calls), 1)


class TestRegistry(unittest.TestCase):

    def setUp(self):
        vision_mod._reset_for_tests()

    def tearDown(self):
        vision_mod._reset_for_tests()

    def test_register_and_get_by_name(self):
        p = FakeProvider("aaa", "AAA_KEY")
        vision_mod.register_provider(p)
        self.assertIs(vision_mod.get_provider("aaa"), p)
        self.assertIsNone(vision_mod.get_provider("bbb"))

    def test_reregister_overrides(self):
        p1 = FakeProvider("aaa", "AAA_KEY")
        p2 = FakeProvider("aaa", "AAA_KEY")
        vision_mod.register_provider(p1)
        vision_mod.register_provider(p2)
        self.assertIs(vision_mod.get_provider("aaa"), p2)

    def test_register_rejects_nameless(self):
        with self.assertRaises(ValueError):
            vision_mod.register_provider(FakeProvider("", "K"))

    def test_builtin_registered_on_use(self):
        vision_mod._reset_for_tests()
        vision_mod._ensure_builtin_registered()
        p = vision_mod.get_provider("minimax")
        self.assertIsNotNone(p)
        self.assertEqual(p.name, "minimax")
        self.assertEqual(p.api_key_env, "MINIMAX_CN_API_KEY")


class TestLoadVisionConfig(unittest.TestCase):

    def test_reads_project_example_config(self):
        """load_vision_config 能读项目根 config.example.json 的 capabilities.vision 段。"""
        project_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cfg = vision_mod.load_vision_config(root=project_root)
        self.assertEqual(cfg.provider, "minimax")
        self.assertEqual(cfg.model, "MiniMax-M3")
        self.assertEqual(cfg.max_input_bytes, 20 * 1024 * 1024)
        self.assertEqual(cfg.resize_target_bytes, 5 * 1024 * 1024)
        self.assertEqual(cfg.max_dimension, 2048)
        self.assertEqual(cfg.timeout_seconds, 120)

    def test_missing_section_falls_back_to_defaults(self):
        root = make_temp_root_with_config(json.dumps({"capabilities": {}}))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        cfg = vision_mod.load_vision_config(root=root)
        self.assertEqual(cfg.provider, "minimax")
        self.assertEqual(cfg.timeout_seconds, 120)

    def test_partial_overrides(self):
        root = make_temp_root_with_config(json.dumps({
            "capabilities": {"vision": {"model": "MiniMax-M2", "timeoutSeconds": 30}}}))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        cfg = vision_mod.load_vision_config(root=root)
        self.assertEqual(cfg.model, "MiniMax-M2")
        self.assertEqual(cfg.timeout_seconds, 30)
        # 没写的字段回落默认
        self.assertEqual(cfg.provider, "minimax")
        self.assertEqual(cfg.max_dimension, 2048)

    def test_invalid_types_fall_back(self):
        root = make_temp_root_with_config(json.dumps({
            "capabilities": {"vision": {"maxInputBytes": "lots", "timeoutSeconds": -5,
                                        "provider": "", "model": 123}}}))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        cfg = vision_mod.load_vision_config(root=root)
        self.assertEqual(cfg.max_input_bytes, 20 * 1024 * 1024)
        self.assertEqual(cfg.timeout_seconds, 120)
        self.assertEqual(cfg.provider, "minimax")
        self.assertEqual(cfg.model, "MiniMax-M3")


class TestVisionProviderBase(unittest.TestCase):

    def test_fail_result_code_validation(self):
        r = vp.fail_result("SOME_UNKNOWN_CODE", "msg")
        self.assertEqual(r.error_code, "PROVIDER_ERROR")
        r2 = vp.fail_result("AUTH", "msg")
        self.assertEqual(r2.error_code, "AUTH")
        self.assertFalse(r2.ok)

    def test_ok_result_defaults(self):
        r = vp.ok_result(text="hi")
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "hi")
        self.assertEqual(r.reasoning, "")
        self.assertEqual(r.meta, {})

    def test_vision_error_attributes(self):
        e = vp.VisionError("AUTH", "bad key")
        self.assertEqual(e.code, "AUTH")
        self.assertEqual(e.message, "bad key")

    def test_error_codes_fixed_set(self):
        self.assertEqual(set(vp.VISION_ERROR_CODES), {
            "NOT_CONFIGURED", "MODEL_NO_IMAGE_INPUT", "IMAGE_TOO_LARGE",
            "UNSUPPORTED_IMAGE", "AUTH", "NETWORK", "TIMEOUT", "PROVIDER_ERROR",
            "CANCELLED"})

    def test_aspect_hint_constant(self):
        self.assertEqual(vp.VISION_ASPECT_HINT, "图片按原始分辨率送出，不做裁剪")

    def test_abstract_class_cannot_instantiate(self):
        with self.assertRaises(TypeError):
            vp.VisionProvider()


# ---------------------------------------------------------------------------
# 假 provider（编排层测试用：记录调用、返回预设结果）
# ---------------------------------------------------------------------------

class FakeProvider:
    """实现 describe() 的假 provider：只记录调用并返回 next_result，不打网络。"""

    name = ""
    api_key_env = ""

    def __init__(self, name: str, api_key_env: str):
        self.name = name
        self.api_key_env = api_key_env
        self.calls = []
        self.next_result = vp.ok_result(text="")

    def describe(self, *, data_url, question, model=None, timeout=120,
                 opener=None, is_cancelled=lambda: False):
        self.calls.append({"data_url": data_url, "question": question,
                           "model": model, "timeout": timeout,
                           "opener": opener, "is_cancelled": is_cancelled})
        return self.next_result


if __name__ == "__main__":
    unittest.main(verbosity=2)
