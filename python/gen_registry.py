"""生成 provider 注册表（desk-agent Python 侧）。

形态参照 Hermes Agent 的 image_gen_registry：register / list / get / get_active。
注册表**不做密钥解析** —— provider 实例由调用方构造并注入 key。

线程安全：一把 threading.Lock 串行化全部注册表操作。
"""
from __future__ import annotations

import logging
import threading
from typing import Any

import appconfig
from gen_provider import GenProvider

log = logging.getLogger(__name__)

_LOCK = threading.RLock()
# 结构：scope → name → provider
_PROVIDERS: dict[str, dict[str, GenProvider]] = {}

GLOBAL_SCOPE = "__global__"


def register_provider(provider: GenProvider, *, scope: str | None = None) -> None:
    """注册 provider。同名重复注册覆盖（记 debug 日志）。scope=None 进全局表。"""
    if provider is None:
        raise ValueError("provider 不能为 None")
    if not getattr(provider, "name", ""):
        raise ValueError("provider.name 不能为空")
    scope = scope or GLOBAL_SCOPE
    with _LOCK:
        bucket = _PROVIDERS.setdefault(scope, {})
        if provider.name in bucket:
            log.debug("gen_registry: 覆盖已注册 provider name=%s scope=%s",
                      provider.name, scope)
        bucket[provider.name] = provider


def list_providers(*, kind: str | None = None, scope: str | None = None) -> list[GenProvider]:
    """列出 provider，可按 kind 过滤。scope=None 只看全局表。"""
    with _LOCK:
        bucket = _PROVIDERS.get(scope or GLOBAL_SCOPE, {})
        out = list(bucket.values())
    if kind is not None:
        out = [p for p in out if getattr(p, "kind", None) == kind]
    return out


def get_provider(name: str, *, kind: str | None = None,
                 scope: str | None = None) -> GenProvider | None:
    """按 name（可再按 kind）查 provider；查不到返回 None。"""
    if not name:
        return None
    with _LOCK:
        bucket = _PROVIDERS.get(scope or GLOBAL_SCOPE, {})
        p = bucket.get(name)
    if p is None:
        return None
    if kind is not None and getattr(p, "kind", None) != kind:
        return None
    return p


def get_active(kind: str, *, config: dict | None = None) -> GenProvider | None:
    """读配置 capabilities.<kind>_gen.provider 去注册表里查。查不到返回 None（不猜默认）。"""
    if not kind:
        return None
    name: Any = None
    if config is not None:
        caps = config.get("capabilities") if isinstance(config, dict) else None
        if isinstance(caps, dict):
            seg = caps.get(kind + "_gen")
            if isinstance(seg, dict):
                name = seg.get("provider")
    else:
        try:
            seg = appconfig.capability(kind + "_gen")
        except Exception:
            return None
        if isinstance(seg, dict):
            name = seg.get("provider")
    if not isinstance(name, str) or not name:
        return None
    return get_provider(name, kind=kind)


def _reset_for_tests() -> None:
    """清空注册表（仅测试用，实现文件里别处不得调用）。"""
    with _LOCK:
        _PROVIDERS.clear()
