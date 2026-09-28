"""vision_analyze 能力入口（编排，契约 8.4）。

流程（定死，不许改顺序）：
  1. source 是路径 → store.save_file；是 attach:<sha> → store.parse_uri
  2. attachments.normalize_image（策略字段来自 VisionConfig）
  3. 能力门（dsh 式，不许跳过）：appconfig.model_declares_image(model) 为 False
     → fail_result("MODEL_NO_IMAGE_INPUT")，报文写清是哪个模型没声明图片能力
  4. attachments.to_data_url(store, ref_after_normalize)
  5. 按 config.provider 查注册表拿 provider，appconfig.resolve_key 取密钥，
     provider.describe(...)
  6. 成功：attachment_uri = 第 2 步之后那份 ref 的 uri；meta 放 normalize meta
     + usage（若有）+ elapsed_ms

vision 是独立能力 + 独立配置（capabilities.vision 段），不占用主模型循环。
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

_here = os.path.dirname(os.path.abspath(__file__))
_parent = os.path.dirname(_here)
if _parent not in sys.path:
    sys.path.insert(0, _parent)

from appconfig import AppConfigError, model_declares_image, resolve_key  # noqa: E402
from attachments import (  # noqa: E402
    ATTACH_SCHEME,
    AttachmentError,
    ImagePolicy,
    normalize_image,
    to_data_url,
)

from capabilities.vision_provider import (  # noqa: E402
    MODEL_NO_IMAGE_INPUT,
    IMAGE_TOO_LARGE,
    UNSUPPORTED_IMAGE,
    NOT_CONFIGURED,
    NETWORK,
    VisionResult,
    fail_result,
)
from capabilities.providers import MiniMaxVisionProvider  # noqa: E402

# ---------------------------------------------------------------------------
# 配置（capabilities.vision 段 → VisionConfig）
# ---------------------------------------------------------------------------

# config.json 键名（照 config.example.json 里的 camelCase）
_CFG_KEYS = {
    "provider": "provider",
    "model": "model",
    "max_input_bytes": "maxInputBytes",
    "resize_target_bytes": "resizeTargetBytes",
    "max_dimension": "maxDimension",
    "timeout_seconds": "timeoutSeconds",
}


@dataclass(frozen=True)
class VisionConfig:
    """vision 能力的独立配置（契约 8.4 默认值）。"""

    provider: str = "minimax"
    model: str = "MiniMax-M3"
    max_input_bytes: int = 20 * 1024 * 1024
    resize_target_bytes: int = 5 * 1024 * 1024
    max_dimension: int = 2048
    timeout_seconds: int = 120

    def image_policy(self) -> ImagePolicy:
        """把 vision 配置映射成 attachments 的 ImagePolicy（字段同名对齐）。"""
        return ImagePolicy(
            max_input_bytes=self.max_input_bytes,
            resize_target_bytes=self.resize_target_bytes,
            max_dimension=self.max_dimension,
        )


def load_vision_config(*, root: str | None = None) -> VisionConfig:
    """从 config.json / config.example.json 的 capabilities.vision 段读配置。

    段缺失或字段缺失时用 VisionConfig 默认值兜底（能力永远可用默认值，
    不因为配置缺一段就崩）。非法类型（比如 maxInputBytes 是字符串）也回落默认。
    """
    from appconfig import capability

    section = capability("vision", {}, root=root)
    if not isinstance(section, dict):
        section = {}
    kw: dict = {}
    for attr, key in _CFG_KEYS.items():
        if key in section:
            v = section[key]
            if attr in ("provider", "model"):
                if isinstance(v, str) and v.strip():
                    kw[attr] = v.strip()
            else:
                if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                    kw[attr] = v
    return VisionConfig(**kw)


# ---------------------------------------------------------------------------
# provider 注册表（本模块持有；vision 独立于 gen_registry）
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, "object"] = {}


def register_provider(provider) -> None:
    """按 provider.name 注册（重复注册覆盖，与 gen_registry 语义一致）。"""
    if provider is None or not getattr(provider, "name", ""):
        raise ValueError("register_provider 需要 name 非空的 provider 实例/类")
    _PROVIDERS[provider.name] = provider


def get_provider(name: str) -> "VisionProvider | None":
    """按名字取 provider；没注册过返回 None（不猜默认）。"""
    return _PROVIDERS.get(name) if isinstance(name, str) else None


def _ensure_builtin_registered() -> None:
    """首次访问时把内置 provider 注册好（幂等）。"""
    if "minimax" not in _PROVIDERS:
        register_provider(MiniMaxVisionProvider())


def _reset_for_tests() -> None:
    """清空注册表（测试隔离专用；生产代码不许调）。"""
    _PROVIDERS.clear()


# ---------------------------------------------------------------------------
# 编排入口
# ---------------------------------------------------------------------------

def _source_to_ref(store, source: str):
    """source（绝对路径 | attach:<sha>）→ AttachmentRef。"""
    if isinstance(source, str) and source.startswith(ATTACH_SCHEME):
        return store.parse_uri(source)
    return store.save_file(source)


def vision_analyze(*, store, source: str, question: str,
                   config: VisionConfig | None = None,
                   root: str | None = None) -> VisionResult:
    """把一张图 + 一个问题交给独立 vision 能力，返回 VisionResult。

    不打日志、不做权限判断（那是上层的事）；opener 用 provider 默认
    （生产走 urllib），单测通过给 provider 注入假 opener 的方式隔离网络。
    """
    _ensure_builtin_registered()
    cfg = config if config is not None else load_vision_config(root=root)
    t0 = time.monotonic()

    # 1. source → ref
    try:
        ref = _source_to_ref(store, source)
    except AttachmentError as e:
        return fail_result(e.code, e.message)

    # 2. normalize_image（能力门之前先过表示层：超大/不支持在这里挡）
    try:
        ref, norm_meta = normalize_image(store, ref, cfg.image_policy())
    except AttachmentError as e:
        code = e.code
        if code not in (IMAGE_TOO_LARGE, UNSUPPORTED_IMAGE):
            code = UNSUPPORTED_IMAGE
        return fail_result(code, e.message)

    # 3. 能力门（dsh 式，不许跳过）：模型没显式声明 image 输入 → 直接拒绝
    if not model_declares_image(cfg.model, root=root):
        return fail_result(
            MODEL_NO_IMAGE_INPUT,
            "模型 %s 未在配置里声明支持图片输入（capabilities.models.%s.inputModalities "
            "不含 image），已拒绝送图，不许静默丢图" % (cfg.model, cfg.model),
            provider=cfg.provider,
            model=cfg.model,
            attachment_uri=ref.uri(),
        )

    # 4. data URL
    try:
        data_url = to_data_url(store, ref)
    except AttachmentError as e:
        return fail_result(e.code, e.message)

    # 5. provider + 密钥 + describe
    provider = get_provider(cfg.provider)
    if provider is None:
        return fail_result(NOT_CONFIGURED,
                           "vision provider %r 未注册（已注册: %s）"
                           % (cfg.provider, sorted(_PROVIDERS)))
    try:
        api_key = resolve_key(provider.api_key_env, required=True)
    except AppConfigError as e:
        return fail_result(NOT_CONFIGURED, e.message, provider=provider.name)

    result = provider.describe(
        data_url=data_url,
        question=question,
        model=cfg.model,
        timeout=cfg.timeout_seconds,
    )

    # 6. 统一补 meta（attachment_uri 是 normalize 之后那份 ref）
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    meta = dict(result.meta or {})
    meta["normalize"] = norm_meta
    meta["elapsed_ms"] = elapsed_ms
    if "usage" not in meta:
        meta["usage"] = {}
    uri = ref.uri()
    if result.ok:
        return VisionResult(
            ok=True,
            text=result.text,
            reasoning=result.reasoning,
            provider=result.provider or provider.name,
            model=result.model or cfg.model,
            attachment_uri=uri,
            meta=meta,
        )
    return VisionResult(
        ok=False,
        provider=result.provider or provider.name,
        model=result.model or cfg.model,
        attachment_uri=result.attachment_uri or uri,
        meta=meta,
        error_code=result.error_code,
        error_message=result.error_message,
    )
