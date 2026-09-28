"""MiniMax 视觉 provider（契约 8.3，请求体已实测可跑通，照抄不改）。

端点：POST https://api.minimaxi.com/v1/chat/completions
鉴权：Authorization: Bearer <MINIMAX_CN_API_KEY>
请求体：content 数组 [image_url(data URL), text(问题)]，model/max_tokens 顶层字段。

硬约束：
  * 必须走 opener（可注入），单测传假 opener 绝不打真网络。
  * 绝不用 curl 子进程（住宅 CN IP 上 curl 约一半概率 HTTP 000）。
  * 正文里可能内联 <thinking> 标签：用 ReasoningSplitter 分流，两条都保留。
  * base_resp.status_code 非 0 按码映射；HTTP 401/403→AUTH；超时→TIMEOUT；
    连接类异常→NETWORK。
"""
from __future__ import annotations

import json
import socket
from typing import Callable

import sys as _sys
import os as _os

# 与第一批模块平级导入（python/ 平铺目录）
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from gen_provider import default_opener  # noqa: E402

from capabilities.vision_provider import (  # noqa: E402
    AUTH,
    NETWORK,
    PROVIDER_ERROR,
    TIMEOUT,
    VisionError,
    VisionResult,
    fail_result,
    ok_result,
    split_content_text,
)

# 端点（已实测）：M3 视觉走 /v1/chat/completions
ENDPOINT = "https://api.minimaxi.com/v1/chat/completions"

# 默认模型（不传 model 时用）
DEFAULT_MODEL = "MiniMax-M3"

# 请求体顶层 max_tokens（契约 8.3 照抄）
MAX_TOKENS = 1024

# base_resp.status_code → 错误码映射（契约 8.3 定死）
_BASE_RESP_CODE_MAP = {
    "1004": AUTH,        # 鉴权失败
    "2056": PROVIDER_ERROR,  # 配额/欠费
}


class MiniMaxVisionProvider:
    """MiniMax M3 视觉识别 provider。name="minimax", api_key_env="MINIMAX_CN_API_KEY"。"""

    name = "minimax"
    api_key_env = "MINIMAX_CN_API_KEY"

    def describe(self, *, data_url: str, question: str, model: str | None = None,
                 timeout: int = 120, opener=None,
                 is_cancelled: Callable[[], bool] = lambda: False) -> VisionResult:
        return self._guard(lambda: self._describe(
            data_url=data_url, question=question, model=model,
            timeout=timeout, opener=opener, is_cancelled=is_cancelled))

    # ---- 内部实现 ----------------------------------------------------------

    def _describe(self, *, data_url: str, question: str, model: str | None,
                  timeout: int, opener, is_cancelled: Callable[[], bool]) -> VisionResult:
        resolved_model = model or DEFAULT_MODEL
        opener = opener if opener is not None else default_opener

        if is_cancelled():
            return fail_result("CANCELLED", "请求已取消（vision）")

        if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
            raise VisionError("UNSUPPORTED_IMAGE",
                              "vision 只接受 data:image/* 的图片（收到: %.30s...）" % str(data_url))

        payload = self._build_payload(data_url=data_url, question=question,
                                      model=resolved_model)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + self._key(),
            "Content-Type": "application/json",
        }
        try:
            status, raw = opener(self._url(), data=body, headers=headers,
                                 timeout=timeout, method="POST")
        except VisionError:
            raise
        except (socket.timeout, TimeoutError) as e:
            raise VisionError(TIMEOUT, "视觉请求超时: %s" % e) from e
        except OSError as e:
            raise VisionError(NETWORK, "网络错误: %s" % e) from e

        if is_cancelled():
            return fail_result("CANCELLED", "请求已取消（vision）")

        return self._parse_response(status, raw, model=resolved_model)

    def _build_payload(self, *, data_url: str, question: str, model: str) -> dict:
        """契约 8.3 请求体，照抄：content 数组 = [image_url, text]。"""
        return {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": question},
                    ],
                }
            ],
        }

    def _url(self) -> str:
        return ENDPOINT

    def _key(self) -> str:
        """密钥只从环境变量读（appconfig.resolve_key 的等价语义），不落盘不打印。"""
        import os
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise VisionError("NOT_CONFIGURED",
                              "环境变量 %s 未设置（vision 密钥只从环境变量读）" % self.api_key_env)
        return key

    def _parse_response(self, status: int, raw: bytes, *, model: str) -> VisionResult:
        """解析响应：HTTP 状态码 → base_resp → choices[0].message.content → 分流。"""
        parsed = _try_json(raw)

        if status in (401, 403):
            return fail_result(AUTH, "HTTP %d：鉴权失败（%s）" % (status, self._snip(raw)))

        if status >= 400:
            # 4xx/5xx body 里可能带 base_resp，先按 base_resp 映射，没有再归 PROVIDER_ERROR
            if parsed is not None:
                mapped = self._base_resp_error(parsed)
                if mapped is not None:
                    return mapped
            return fail_result(PROVIDER_ERROR,
                               "HTTP %d: %s" % (status, self._snip(raw)))

        if parsed is None:
            return fail_result(PROVIDER_ERROR,
                               "响应不是合法 JSON: %s" % self._snip(raw))

        base_err = self._base_resp_error(parsed)
        if base_err is not None:
            return base_err

        # 响应取 choices[0].message.content（字符串）
        try:
            content = parsed["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return fail_result(PROVIDER_ERROR,
                               "响应里没有 choices[0].message.content: %s" % self._snip(parsed))

        if not isinstance(content, str):
            content = "" if content is None else str(content)

        # 思考分流：正文里可能内联 <thinking> 标签，两条通道都保留
        reasoning, text = split_content_text(content)

        return ok_result(
            text=text,
            reasoning=reasoning,
            provider=self.name,
            model=model,
            meta={"usage": parsed.get("usage", {})} if isinstance(parsed.get("usage"), dict) else {"usage": {}},
        )

    def _base_resp_error(self, parsed: dict) -> VisionResult | None:
        """MiniMax 错误封装：base_resp.status_code 非 0 按码映射。没有 base_resp 返回 None。"""
        base = parsed.get("base_resp") if isinstance(parsed, dict) else None
        if not isinstance(base, dict):
            return None
        code = base.get("status_code")
        if code is None or code == 0:
            return None
        code_str = str(code)
        mapped = _BASE_RESP_CODE_MAP.get(code_str, PROVIDER_ERROR)
        return fail_result(mapped,
                           "base_resp.status_code=%s: %s" % (code_str, base.get("status_msg", "")))

    def _guard(self, fn: Callable[[], VisionResult]) -> VisionResult:
        try:
            return fn()
        except VisionError as e:
            return fail_result(e.code, e.message)

    def _snip(self, body) -> str:
        try:
            if isinstance(body, (dict, list)):
                s = json.dumps(body, ensure_ascii=False)
            elif isinstance(body, (bytes, bytearray)):
                s = bytes(body).decode("utf-8", "replace")
            else:
                s = str(body)
        except Exception:
            s = repr(body)
        return s[:300]


def _try_json(raw: bytes | None):
    if not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        parsed = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None
