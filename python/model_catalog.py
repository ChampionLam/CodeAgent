"""预置模型目录：窗口 / 思考参数 / 深度 / 价格。

数据来源：docs/model-catalog-research-*.json（官方文档逐字取证）合成到
model_catalog.json。这里只做「读 + 查 + 算钱」，不改数据。

算钱的三条纪律（用户明确要求）：
  1. 只有 pricingMode == "per-token" 且价格非空才算钱；套餐制（token-plan /
     subscription）留空 → 返回 None，界面显示「套餐制」，不许拿套餐价除额度
     反推单价；unknown 同理显示「未知」。
  2. 价格单位按官方原样（元/百万 token 或 USD/百万 token），所以不同币种
     的钱不能相加——聚合时按币种分开。
  3. 缓存价官方没公布时，缓存命中的 token 按普通输入价计，并在 note 里写明。
"""

from __future__ import annotations

import json
import os

_CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "model_catalog.json")
_cache: dict | None = None


def load() -> dict:
    global _cache
    if _cache is None:
        with open(_CATALOG_PATH, encoding="utf-8") as fh:
            _cache = json.load(fh)
    return _cache


def entries() -> list[dict]:
    return load().get("models", [])


def for_provider(provider: str) -> list[dict]:
    return [m for m in entries() if m.get("provider") == provider]


def find(provider: str, model: str) -> dict | None:
    for m in entries():
        if m.get("provider") == provider and m.get("model") == model:
            return m
    return None


def by_model_id(model: str, prefer_provider: str | None = None) -> dict | None:
    """按模型 id 找条目：优先 prefer_provider（当前配置的厂商）。"""
    hits = [m for m in entries() if m.get("model") == model]
    if not hits:
        return None
    for m in hits:
        if prefer_provider and m.get("provider") == prefer_provider:
            return m
    return hits[0]
