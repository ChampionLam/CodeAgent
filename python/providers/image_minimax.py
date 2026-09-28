"""MiniMax 生图 provider（api.minimaxi.com，image-01 系列）。

契约 5.4（已实测）：
  * POST https://api.minimaxi.com/v1/image_generation
  * 请求体: {"model":..., "prompt":..., "n":1, "aspect_ratio":..., "response_format":"url"}
    references 非空 → 自动切 image-01-live 并把 references 放进 image_urls 数组
  * 响应: data.image_urls[0] 下载落盘
  * base_resp.status_code != 0 → 按码映射：2056→QUOTA；1004→AUTH；
    1026/其它 1xxx 安全过滤→BAD_REQUEST；其余→PROVIDER_ERROR
  * 绝不用 curl —— 住宅 CN IP 上 curl 约 50% 概率 HTTP 000，一律 urllib（opener 注入）
"""
from __future__ import annotations

from typing import Callable, Sequence

from gen_provider import (
    GenProvider,
    GenResult,
    BAD_REQUEST,
    CANCELLED,
    PROVIDER_ERROR,
    QUOTA,
    AUTH,
    fail,
    normalize_references,
    resolve_aspect_ratio,
)

DEFAULT_MODEL = "image-01"
LIVE_MODEL = "image-01-live"
_DEFAULT_BASE = "https://api.minimaxi.com"


class MiniMaxImageProvider(GenProvider):
    name = "minimax"
    kind = "image"
    api_key_env = "MINIMAX_CN_API_KEY"

    _MODELS = {
        "image-01": {
            "display": "MiniMax image-01",
            "speed": "medium",
            "strengths": "文生图（默认）",
            "default": True,
        },
        "image-01-live": {
            "display": "MiniMax image-01-live",
            "speed": "medium",
            "strengths": "带参考图的图生图",
        },
    }

    def __init__(self, *, api_key: str | None = None, opener=None,
                 base_url: str | None = None, sleep=None) -> None:
        super().__init__(api_key=api_key, opener=opener, sleep=sleep)
        self.base_url = (base_url or _DEFAULT_BASE).rstrip("/")
        self.endpoint = self.base_url + "/v1/image_generation"

    def models(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self._MODELS.items()}

    # ---- 错误码映射（契约固定） --------------------------------------------

    @staticmethod
    def _map_error(status_code) -> tuple[str, str]:
        """base_resp.status_code → (契约错误码, 人话)。2056→QUOTA、1004→AUTH、
        1026/其它 1xxx→BAD_REQUEST、其余→PROVIDER_ERROR。"""
        try:
            code = int(status_code)
        except (TypeError, ValueError):
            return PROVIDER_ERROR, "base_resp.status_code 非数值: %r" % (status_code,)
        if code == 2056:
            return QUOTA, "MiniMax 配额不足（2056）"
        if code == 1004:
            return AUTH, "MiniMax 鉴权失败（1004）"
        if code == 1026:
            return BAD_REQUEST, "MiniMax 内容安全过滤（1026）"
        if 1000 <= code < 2000:
            return BAD_REQUEST, "MiniMax 请求被拒（%d）" % code
        return PROVIDER_ERROR, "MiniMax 错误（%d）" % code

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
            refs = normalize_references(references)
            # references 非空 → 自动切 image-01-live（契约 5.4）
            if refs:
                model_id = LIVE_MODEL
            payload = {
                "model": model_id,
                "prompt": prompt,
                "n": 1,
                "aspect_ratio": resolve_aspect_ratio(aspect_ratio),
                "response_format": "url",
            }
            if refs:
                payload["image_urls"] = refs
            if params:
                # 只透传白名单参数，避免上层注入未知字段
                for k in ("n", "response_format"):
                    if k in params:
                        payload[k] = params[k]

            status, body = self._post_json(self.endpoint, payload, api_key=api_key)
            if not isinstance(body, dict):
                return fail(PROVIDER_ERROR,
                            "minimax HTTP %d 响应非 JSON" % status)
            # HTTP 200 也可能是错误：必须检查 base_resp.status_code
            base_resp = body.get("base_resp")
            sc = base_resp.get("status_code") if isinstance(base_resp, dict) else None
            if sc is not None and sc != 0:
                code, msg = self._map_error(sc)
                return fail(code, msg + " | %s" % (base_resp.get("status_msg") or ""))
            try:
                image_url = body["data"]["image_urls"][0]
            except (KeyError, IndexError, TypeError):
                return fail(PROVIDER_ERROR, "minimax 响应缺 data.image_urls: %r" % (body,))
            if is_cancelled():
                return fail(CANCELLED, "生成已被取消（图已产出但未下载）")
            # 下载走注入的 opener
            path = self._download_to_file(image_url, out_dir, suffix=".png", timeout=120)
            return GenResult(
                ok=True,
                files=(path,),
                meta={
                    "provider": self.name,
                    "model": model_id,
                    "prompt": prompt,
                    "aspect_ratio": payload["aspect_ratio"],
                    "raw_ids": {},
                },
            )

        return self._guard(_run)
