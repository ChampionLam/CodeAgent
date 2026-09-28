"""百炼万相生图 provider（dashscope，走阿里云 MaaS 端点）。

端点、请求体、响应取值路径均已实测（见契约 5.3）：
  * 端点: https://<workspace>.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation
    workspace 从 DASHSCOPE_BASE_URL 的正则 https?://([^.]+)\\. 抓第一段；取不到抛 GenError("NOT_CONFIGURED")
  * 请求体: {"model":..., "input":{"messages":[{"role":"user","content":[{"text":...}]}]},
             "parameters":{"prompt_extend": true}}
  * 响应: output.choices[0].message.content[0].image → OSS URL，再下载落盘
"""
from __future__ import annotations

import os
import re
from typing import Callable, Sequence

from gen_provider import (
    GenError,
    GenProvider,
    GenResult,
    PROVIDER_ERROR,
    BAD_REQUEST,
    CANCELLED,
    fail,
    resolve_aspect_ratio,
    _snippet,
)

DEFAULT_MODEL = "wan2.7-image-pro"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
_ENDPOINT_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
_WORKSPACE_RE = re.compile(r"https?://([^.]+)\.")


class DashscopeImageProvider(GenProvider):
    name = "dashscope"
    kind = "image"
    api_key_env = "DASHSCOPE_API_KEY"

    _MODELS = {
        "wan2.7-image-pro": {
            "display": "万相 wan2.7-image-pro",
            "speed": "medium",
            "strengths": "中文渲染最强（默认）",
            "default": True,
        },
        "wan2.7-image": {
            "display": "万相 wan2.7-image",
            "speed": "fast",
            "strengths": "通用生图，速度快",
        },
    }

    def __init__(self, *, api_key: str | None = None, opener=None,
                 base_url: str | None = None, sleep=None) -> None:
        super().__init__(api_key=api_key, opener=opener, sleep=sleep)
        self.base_url = base_url  # None → 从 DASHSCOPE_BASE_URL 环境变量取

    def models(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._MODELS.items()}

    # ---- 内部 --------------------------------------------------------------

    def _workspace(self) -> str:
        base = (self.base_url or os.environ.get("DASHSCOPE_BASE_URL", "").strip()
                or DEFAULT_BASE_URL)
        m = _WORKSPACE_RE.match(base.strip())
        if not m:
            raise GenError("NOT_CONFIGURED",
                           "无法从 DASHSCOPE_BASE_URL 解析 workspace: %r" % base)
        return m.group(1)

    def _endpoint(self) -> str:
        return "https://%s.cn-beijing.maas.aliyuncs.com%s" % (self._workspace(), _ENDPOINT_PATH)

    # ---- 生成 --------------------------------------------------------------

    def generate(self, *, prompt: str, model: str | None, out_dir: str,
                 aspect_ratio: str | None = None, references: Sequence[str] = (),
                 params: dict | None = None,
                 is_cancelled: Callable[[], bool] = lambda: False) -> GenResult:
        def _run() -> GenResult:
            api_key = self._require_key()
            model_id = model or DEFAULT_MODEL
            if model_id not in self._MODELS:
                return fail(BAD_REQUEST, "未知模型: %s" % model_id)
            if is_cancelled():
                return fail(CANCELLED, "提交前已取消（未发请求、未扣配额）")
            payload = {
                "model": model_id,
                "input": {
                    "messages": [
                        {"role": "user", "content": [{"text": prompt}]}
                    ]
                },
                "parameters": {"prompt_extend": True},
            }
            status, body = self._post_json(self._endpoint(), payload, api_key=api_key)
            if status != 200:
                return fail(PROVIDER_ERROR,
                            "dashscope HTTP %d: %s" % (status, _snippet(body)))
            if not isinstance(body, dict):
                return fail(PROVIDER_ERROR,
                            "dashscope 响应非 JSON: %s" % _snippet(body))
            try:
                image_url = body["output"]["choices"][0]["message"]["content"][0]["image"]
            except (KeyError, IndexError, TypeError):
                return fail(PROVIDER_ERROR,
                            "dashscope 响应缺 image 字段: %s" % _snippet(body))
            if not image_url:
                return fail(PROVIDER_ERROR, "dashscope 返回空 image URL")
            if is_cancelled():
                return fail(CANCELLED, "生成已被取消（图已产出但未下载）")
            # 下载走注入的 opener（单测绝不打真网络）
            path = self._download_to_file(image_url, out_dir, suffix=".png", timeout=120)
            return GenResult(
                ok=True,
                files=(path,),
                meta={
                    "provider": self.name,
                    "model": model_id,
                    "prompt": prompt,
                    "aspect_ratio": resolve_aspect_ratio(aspect_ratio),
                    "raw_ids": {"request_id": body.get("request_id", "")},
                },
            )

        return self._guard(_run)
