"""配置读取层（desk-agent）。

职责边界：只负责「读 config.json / config.example.json + 解析密钥来源」，
不做业务判断、不缓存状态、不碰网络。

设计约定：
  * config.json 优先，缺则回落 config.example.json（与 llm.py 的 load_config 一致）。
  * 密钥永远只从环境变量取（或由调用方显式传入），绝不写文件、绝不进日志。
  * 每个能力（vision / image_gen / video_gen / tools）读自己的段，互不干扰。
"""
from __future__ import annotations

import json
import os
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AppConfigError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = "CONFIG_ERROR"
        self.message = message


def config_path(root: str | None = None) -> str:
    root = root or _ROOT
    path = os.path.join(root, "config.json")
    if os.path.exists(path):
        return path
    return os.path.join(root, "config.example.json")


def load(root: str | None = None) -> dict[str, Any]:
    """读整份配置。文件缺失或不是合法 JSON 一律抛 AppConfigError。"""
    path = config_path(root)
    if not os.path.exists(path):
        raise AppConfigError("找不到配置文件: " + path)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise AppConfigError("配置不是合法 JSON: %s" % e) from e
    if not isinstance(raw, dict):
        raise AppConfigError("配置顶层必须是 JSON 对象")
    return raw


def section(name: str, default: Any = None, *, root: str | None = None) -> Any:
    """取顶层某个段；不存在返回 default。"""
    return load(root).get(name, default)


def capabilities(*, root: str | None = None) -> dict[str, Any]:
    """capabilities 段（vision / image_gen / video_gen / models 都挂在这里）。"""
    cap = section("capabilities", {}, root=root)
    return cap if isinstance(cap, dict) else {}


def capability(name: str, default: Any = None, *, root: str | None = None) -> Any:
    return capabilities(root=root).get(name, default)


def resolve_key(env_name: str, *, required: bool = True) -> str:
    """从环境变量取密钥。key 只从环境变量读，绝不落盘。"""
    key = os.environ.get(env_name, "").strip()
    if not key and required:
        raise AppConfigError("环境变量 %s 未设置（密钥只从环境变量读）" % env_name)
    return key


def read_env_file(path: str) -> dict[str, str]:
    """解析 key=value 形式的 .env（供 E2E 脚本注入子进程环境用）。

    只做最小解析：忽略空行与 '#' 起始行，去掉值两端的引号。
    本函数返回的字典里可能含密钥 —— 调用方不得打印、不得写文件。
    """
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def model_declares_image(model_id: str, *, root: str | None = None) -> bool:
    """dsh 式能力门：模型是否显式声明支持图片输入。

    声明位置：config.json 的 capabilities.models.<model_id>.inputModalities。
    没声明就是不支持 —— 不猜、不默认放行。
    """
    models = capability("models", {}, root=root)
    if not isinstance(models, dict):
        return False
    entry = models.get(model_id)
    if not isinstance(entry, dict):
        return False
    modalities = entry.get("inputModalities")
    if not isinstance(modalities, list):
        return False
    return "image" in [str(m).lower() for m in modalities]