"""取厂商模型列表（models.fetch / fetch_remote_models）的测试。

用户视角：选厂商 → 点一下 → 从列表里挑模型，不该手打模型名。
安全口径：key 只在 Authorization 头里出现，绝不回显、不落日志、不进返回值。
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "python"))

import modelconfig as MC  # noqa: E402


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_returning(payload, seen: list | None = None):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def _open(req, timeout=None):
        if seen is not None:
            seen.append((req.full_url, dict(req.headers), timeout))
        return FakeResponse(raw)

    return _open


class FetchRemoteModelsTest(unittest.TestCase):
    def test_parses_openai_data_shape(self):
        seen: list = []
        ids = MC.fetch_remote_models(
            "https://api.example.com/v1", "sk-secret-value",
            opener=opener_returning({"object": "list", "data": [
                {"id": "MiniMax-M3"}, {"id": "MiniMax-M2.1"}, {"id": "MiniMax-M3"},
            ]}, seen))
        self.assertEqual(ids, ["MiniMax-M2.1", "MiniMax-M3"])  # 去重 + 排序
        url, headers, _ = seen[0]
        self.assertEqual(url, "https://api.example.com/v1/models")
        self.assertEqual(headers.get("Authorization"), "Bearer sk-secret-value")

    def test_parses_alternate_shapes(self):
        self.assertEqual(
            MC.fetch_remote_models("https://x/v1", opener=opener_returning(
                {"models": [{"name": "a"}]})), ["a"])
        self.assertEqual(
            MC.fetch_remote_models("https://x/v1", opener=opener_returning(
                ["b", {"id": "c"}])), ["b", "c"])

    def test_key_never_leaks_into_result_or_message(self):
        secret = "sk-do-not-echo-me"
        for payload in ({"data": []}, {"nope": 1}, b"<html>not json</html>"):
            try:
                MC.fetch_remote_models("https://x/v1", secret, opener=opener_returning(payload))
            except MC.ModelFetchError as e:
                self.assertNotIn(secret, e.message)
                self.assertNotIn(secret, str(e))
            else:
                self.fail("expected ModelFetchError")

    def test_no_key_means_no_authorization_header(self):
        seen: list = []
        MC.fetch_remote_models("http://localhost:11434/v1", "",
                               opener=opener_returning({"data": [{"id": "qwen3"}]}, seen))
        self.assertNotIn("Authorization", seen[0][1])

    def test_trailing_slash_is_not_doubled(self):
        seen: list = []
        MC.fetch_remote_models("https://x/v1/", opener=opener_returning({"data": [{"id": "m"}]}, seen))
        self.assertEqual(seen[0][0], "https://x/v1/models")

    def test_empty_base_url_is_rejected(self):
        with self.assertRaises(MC.ModelFetchError):
            MC.fetch_remote_models("", "k")

    def test_oversized_response_is_refused(self):
        big = b'{"data": [{"id": "' + b"x" * (MC.MODELS_RESPONSE_CAP + 10) + b'"}]}'
        with self.assertRaises(MC.ModelFetchError) as ctx:
            MC.fetch_remote_models("https://x/v1", opener=opener_returning(big))
        self.assertEqual(ctx.exception.code, "MODEL_FETCH_FAILED")

    def test_http_401_is_reported_as_key_problem(self):
        import urllib.error

        def _open(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with self.assertRaises(MC.ModelFetchError) as ctx:
            MC.fetch_remote_models("https://x/v1", "sk-x", opener=_open)
        self.assertEqual(ctx.exception.code, "MODEL_FETCH_UNAUTHORIZED")

    def test_network_error_is_short_not_a_traceback(self):
        def _open(req, timeout=None):
            raise OSError("connection refused")

        with self.assertRaises(MC.ModelFetchError) as ctx:
            MC.fetch_remote_models("https://x/v1", opener=_open)
        self.assertEqual(ctx.exception.code, "MODEL_FETCH_FAILED")
        self.assertNotIn("Traceback", ctx.exception.message)


class ReadEnvVarTest(unittest.TestCase):
    """models.fetch 没带 key 时要能从 .env 里捞到环境变量里的 key。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="desk-env-")
        self.path = os.path.join(self.dir, ".env")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("# comment\nMINIMAX_CN_API_KEY=sk-from-file\nOTHER=x\n")
        self._saved = os.environ.pop("MINIMAX_CN_API_KEY", None)
        os.environ["DESK_AGENT_ENV_FILE"] = self.path

    def tearDown(self):
        os.environ.pop("DESK_AGENT_ENV_FILE", None)
        os.environ.pop("MINIMAX_CN_API_KEY", None)
        if self._saved is not None:
            os.environ["MINIMAX_CN_API_KEY"] = self._saved

    def test_reads_value_from_env_file(self):
        self.assertEqual(MC.read_env_var("MINIMAX_CN_API_KEY"), "sk-from-file")

    def test_missing_name_is_empty_not_an_error(self):
        self.assertEqual(MC.read_env_var("NOT_THERE"), "")
        self.assertEqual(MC.read_env_var(""), "")


class FetchRemoteModelsDetailedTest(unittest.TestCase):
    """窗口这类「每个模型不一样」的事实必须跟着列表一起回来。

    2026-09-27 用户报：「我刚添加的模型，支持 1M 的，现在只有最大 128」——
    厂商在同一次 /models 响应里就给了 context_length，早先的实现只留了 id，
    于是新加的模型一律落到厂商模板的默认窗口上。
    """

    OPENROUTER = {"data": [
        {"id": "nvidia/nemotron-3-ultra-550b-a55b:free",
         "context_length": 1000000,
         "top_provider": {"context_length": 1000000, "max_completion_tokens": 65536}},
        {"id": "aion-labs/aion-2.0", "context_length": 131072,
         "top_provider": {"context_length": 131072, "max_completion_tokens": 32768}},
        {"id": "no-numbers-at-all"},
    ]}

    def test_ids_and_meta_come_from_one_request(self):
        seen: list = []
        ids, meta = MC.fetch_remote_models_detailed(
            "https://openrouter.ai/api/v1", "***",
            opener=opener_returning(self.OPENROUTER, seen))
        self.assertEqual(len(seen), 1, "一次点击只能请求厂商一次")
        self.assertEqual(ids, ["aion-labs/aion-2.0", "no-numbers-at-all",
                               "nvidia/nemotron-3-ultra-550b-a55b:free"])
        self.assertEqual(meta["nvidia/nemotron-3-ultra-550b-a55b:free"],
                         {"contextLength": 1000000, "maxCompletionTokens": 65536})
        self.assertEqual(meta["aion-labs/aion-2.0"]["contextLength"], 131072)

    def test_model_without_numbers_is_absent_not_guessed(self):
        _, meta = MC.fetch_remote_models_detailed(
            "https://x/v1", opener=opener_returning(self.OPENROUTER))
        self.assertNotIn("no-numbers-at-all", meta)

    def test_top_provider_is_the_fallback_source(self):
        _, meta = MC.fetch_remote_models_detailed("https://x/v1", opener=opener_returning(
            {"data": [{"id": "m", "top_provider": {"context_length": 262144}}]}))
        self.assertEqual(meta["m"], {"contextLength": 262144})

    def test_junk_numbers_are_not_taken(self):
        _, meta = MC.fetch_remote_models_detailed("https://x/v1", opener=opener_returning(
            {"data": [
                {"id": "bool", "context_length": True},
                {"id": "zero", "context_length": 0},
                {"id": "neg", "context_length": -5},
                {"id": "str", "context_length": "128000"},
                {"id": "float", "context_length": 128000.5},
            ]}))
        self.assertEqual(meta, {})

    def test_plain_id_fetch_still_works(self):
        ids = MC.fetch_remote_models("https://x/v1", opener=opener_returning(self.OPENROUTER))
        self.assertEqual(len(ids), 3)


if __name__ == "__main__":
    unittest.main()