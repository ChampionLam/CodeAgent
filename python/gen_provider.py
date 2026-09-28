"""生成能力抽象基类 + 共用助手（desk-agent Python 侧）。

形态参照 Hermes Agent 的 ImageGenProvider / image_gen_registry 设计：
一个窄工具 + Provider 抽象基类 + 每家一个 provider 文件 + 注册表。

本文件只放三样东西：
  1. GenProvider 抽象基类（含 models() 目录与 generate() 签名）
  2. 全 provider 共用的助手（落盘 / 引用归一 / 宽高比解析 / ok-fail 构造）
  3. 默认 opener（urllib 实现，签名与契约一致；单测注入假 opener 绝不打真网络）

硬约束：
  * 所有网络调用必须走 opener（可注入），provider 自己绝不直接发请求。
  * MiniMax 绝不用 curl 子进程 —— 住宅 CN IP 上 curl 约一半概率 HTTP 000。
"""
from __future__ import annotations

import abc
import base64
import binascii
import json
import mimetypes
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable, Sequence

# ---------------------------------------------------------------------------
# 常量（契约固定集合，别加别减）
# ---------------------------------------------------------------------------

DEFAULT_ASPECT_RATIO = "1:1"
ALLOWED_ASPECT_RATIOS = ("1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9")

# 错误码固定集合（契约 5.1）
AUTH = "AUTH"
QUOTA = "QUOTA"
BAD_REQUEST = "BAD_REQUEST"
PROVIDER_ERROR = "PROVIDER_ERROR"
NETWORK = "NETWORK"
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"
POLL_FAILED = "POLL_FAILED"
NOT_CONFIGURED = "NOT_CONFIGURED"
GEN_ERROR_CODES = (AUTH, QUOTA, BAD_REQUEST, PROVIDER_ERROR, NETWORK, TIMEOUT,
                   CANCELLED, POLL_FAILED, NOT_CONFIGURED)

# opener 协议：url, data=None, headers=None, timeout=..., method=... -> (status:int, body:bytes)
Opener = Callable[..., tuple[int, bytes]]


# ---------------------------------------------------------------------------
# 结果与异常
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GenResult:
    """generate() 的统一返回值（字段集合照契约，别加别减）。

    成功：ok=True, files=落盘绝对路径, meta=provider/model/prompt/aspect_ratio/duration/raw_ids
    失败：ok=False, error_code=固定错误码, error_message=人话
    """
    ok: bool
    files: tuple[str, ...] = ()
    meta: dict = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class GenError(Exception):
    """provider 内部控制流异常。generate() 的对外统一出口是 GenResult，不是异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code + ": " + message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 默认 opener（urllib 实现，全 provider 共用；单测注入假 opener）
# ---------------------------------------------------------------------------

def default_opener(url: str, data: bytes | None = None, headers: dict | None = None,
                   timeout: int = 120, method: str = "GET") -> tuple[int, bytes]:
    """用 urllib 发一次请求，返回 (status, body)。

    设计要点：
      * 绝不起 curl 子进程（住宅 CN IP 上 curl 约一半概率 HTTP 000）。
      * HTTPError（4xx/5xx）不抛，把 status 和 body 带回来给上层按码映射
        （MiniMax 会在 4xx body 里带 base_resp）。
      * 网络层异常（DNS / 连接拒绝 / SSL）→ GenError("NETWORK")；
        超时单独归 GenError("TIMEOUT")。
    """
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers or {},
        method=method or ("POST" if data is not None else "GET"),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return int(e.code), body
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise GenError(TIMEOUT, "请求超时: %s" % e) from e
        raise GenError(NETWORK, "网络错误: %s" % e) from e
    except (socket.timeout, TimeoutError) as e:
        raise GenError(TIMEOUT, "请求超时: %s" % e) from e


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class GenProvider(abc.ABC):
    """生成能力 provider 抽象基类。

    子类必须给出类属性 name / kind / api_key_env，并实现 models() 与 generate()。
    网络一律通过 self.opener（构造注入，默认 urllib 实现）；
    self.sleep 同理可注入（视频轮询的等待，单测传 no-op）。
    """

    name: str = ""
    kind: str = ""            # "image" | "video"
    api_key_env: str = ""

    def __init__(self, *, api_key: str | None = None, opener: Opener | None = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self.api_key = api_key
        self.opener: Opener = opener if opener is not None else default_opener
        self.sleep = sleep if sleep is not None else time.sleep

    # ---- 子类实现 ----------------------------------------------------------

    def models(self) -> dict[str, dict]:
        """模型 id → {display, speed, strengths, ...}。子类覆写。"""
        raise NotImplementedError

    @abc.abstractmethod
    def generate(self, *, prompt: str, model: str | None, out_dir: str,
                 aspect_ratio: str | None = None, references: Sequence[str] = (),
                 params: dict | None = None,
                 is_cancelled: Callable[[], bool] = lambda: False) -> GenResult:
        raise NotImplementedError

    # ---- 子类可用的共享机制（放基类，避免每个 provider 各写一份） ----------

    def _require_key(self) -> str:
        if not (self.api_key or "").strip():
            raise GenError(NOT_CONFIGURED,
                           "%s 未设置（构造 provider 时注入 api_key）" % self.api_key_env)
        return self.api_key.strip()

    def _bearer_headers(self, api_key: str, *, content_type: str = "application/json") -> dict:
        return {
            "Authorization": "Bearer " + api_key,
            "Content-Type": content_type,
        }

    def _post_json(self, url: str, payload: dict, *, api_key: str,
                   timeout: int = 180) -> tuple[int, dict | None]:
        """POST JSON 并尝试解 JSON。返回 (status, parsed_or_None)。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        status, raw = self.opener(url, data=body,
                                  headers=self._bearer_headers(api_key),
                                  timeout=timeout, method="POST")
        return status, _try_json(raw)

    def _get_json(self, url: str, *, api_key: str | None = None,
                  timeout: int = 120) -> tuple[int, dict | None]:
        headers = self._bearer_headers(api_key) if api_key else None
        status, raw = self.opener(url, data=None, headers=headers,
                                  timeout=timeout, method="GET")
        return status, _try_json(raw)

    def _get_bytes(self, url: str, *, timeout: int = 300) -> bytes:
        """经 self.opener 下载字节。非 200 抛 GenError("NETWORK")。"""
        status, raw = self.opener(url, data=None, headers=None,
                                  timeout=timeout, method="GET")
        if status != 200:
            raise GenError(NETWORK, "下载失败 HTTP %d: %s" % (status, url))
        return raw

    def _download_to_file(self, url: str, out_dir: str, *, suffix: str,
                          timeout: int = 300) -> str:
        """经 self.opener 下载并落盘（provider 用的下载路径，保证可注入）。

        suffix 只当「认不出格式时的兜底」：真实扩展名按字节魔数判——实测
        MiniMax image-01 返回的是 JPEG，写死 .png 会让扩展名撒谎。
        """
        data = self._get_bytes(url, timeout=timeout)
        ext = sniff_image_ext(data, default=(suffix or ".png").strip().lstrip("."))
        return save_bytes_to(data, out_dir, suffix="." + ext)

    def _guard(self, fn: Callable[[], GenResult]) -> GenResult:
        """把内部 GenError 控制流翻译成失败 GenResult（generate 的统一出口）。"""
        try:
            return fn()
        except GenError as e:
            return fail(e.code, e.message)


# ---------------------------------------------------------------------------
# 共用助手（模块级；provider 与测试都直接用这些）
# ---------------------------------------------------------------------------

def _try_json(raw: bytes | None):
    if not isinstance(raw, (bytes, bytearray)):
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _snippet(body) -> str:
    """把响应压成短字符串给错误信息（不打 key，只截前 300 字）。"""
    try:
        if isinstance(body, (dict, list)):
            s = json.dumps(body, ensure_ascii=False)
        elif isinstance(body, (bytes, bytearray)):
            s = body.decode("utf-8", "replace")
        else:
            s = str(body)
    except Exception:
        s = repr(body)
    return s[:300]


def _unique_stem() -> str:
    return time.strftime("gen_%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _suffix_from_url(url: str, default: str = ".png") -> str:
    low = str(url).split("?", 1)[0].lower()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        if low.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return default


def save_bytes_to(data: bytes, out_dir: str, *, suffix: str) -> str:
    """把字节写进 out_dir（按需创建），返回绝对路径。文件名唯一防覆盖。"""
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    path = os.path.abspath(os.path.join(out_dir, _unique_stem() + suffix))
    with open(path, "wb") as f:
        f.write(data)
    return path


def save_b64_image(data_b64: str, out_dir: str, *, fmt: str = "jpg") -> str:
    """base64 图片落盘。非法 base64 / 非法后缀抛 GenError("BAD_REQUEST")。"""
    try:
        data = base64.b64decode(data_b64, validate=False)
    except (binascii.Error, ValueError, TypeError) as e:
        raise GenError(BAD_REQUEST, "base64 解码失败: %s" % e) from e
    fmt = (fmt or "jpg").strip().lower().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{2,5}", fmt):
        raise GenError(BAD_REQUEST, "非法图片格式后缀: %r" % fmt)
    # fmt 只是兜底：真实格式按字节判（provider 常返回 jpg，写死后缀会骗人）
    fmt = sniff_image_ext(data, default=fmt)
    return save_bytes_to(data, out_dir, suffix="." + fmt)


def sniff_image_ext(data: bytes, *, default: str = "png") -> str:
    """按字节魔数判真实图片格式，认不出才退回 default（不猜）。

    背景（2026-09-23 实测）：MiniMax image-01 回的是 JPEG，原先写死 .png
    → 磁盘上一个 .png 文件里装的是 JPEG，扩展名撒谎。
    """
    if not data:
        return default
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if data.startswith(b"BM"):
        return "bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return default


def save_url_image(url: str, out_dir: str, *, fmt: str = "jpg", timeout: int = 120) -> str:
    """用默认 opener 下载 URL 图片落盘（生产路径；单测不直接走这里）。

    非 200 抛 GenError("NETWORK")。不做网络层重试（重试是上层策略）。
    """
    status, raw = default_opener(url, data=None, headers=None, timeout=timeout, method="GET")
    if status != 200:
        raise GenError(NETWORK, "图片下载失败 HTTP %d: %s" % (status, url))
    fmt = (fmt or "jpg").strip().lstrip(".") or "jpg"
    fmt = sniff_image_ext(raw, default=fmt)
    return save_bytes_to(raw, out_dir, suffix="." + fmt)


def normalize_references(references: Sequence[str]) -> list[str]:
    """本地路径 → data URL；http(s)/data 原样。本地路径不存在抛 GenError("BAD_REQUEST")。"""
    out: list[str] = []
    for ref in references or ():
        if not isinstance(ref, str) or not ref.strip():
            continue
        ref = ref.strip()
        if ref.lower().startswith(("http://", "https://", "data:")):
            out.append(ref)
            continue
        if not os.path.isfile(ref):
            raise GenError(BAD_REQUEST, "参考图路径不存在: %s" % ref)
        with open(ref, "rb") as f:
            data = f.read()
        mime, _ = mimetypes.guess_type(ref)
        if not mime or not mime.startswith("image/"):
            # 按内容嗅探兜底（扩展名不可靠）
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                mime = "image/png"
            elif data[:3] == b"\xff\xd8\xff":
                mime = "image/jpeg"
            elif data[:6] in (b"GIF87a", b"GIF89a"):
                mime = "image/gif"
            elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                mime = "image/webp"
            else:
                mime = "application/octet-stream"
        out.append("data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii")))
    return out


def resolve_aspect_ratio(value: str | None,
                          allowed: Sequence[str] = ALLOWED_ASPECT_RATIOS) -> str:
    """归一宽高比：None/空/非法 → DEFAULT_ASPECT_RATIO（契约语义）。"""
    if value is None:
        return DEFAULT_ASPECT_RATIO
    v = str(value).strip()
    if not v:
        return DEFAULT_ASPECT_RATIO
    if v in allowed:
        return v
    # 容错：把 "16 / 9" / "16x9" 归一成 "16:9"
    m = re.fullmatch(r"(\d+)\s*[:x/]\s*(\d+)", v)
    if m:
        cand = "%s:%s" % (m.group(1), m.group(2))
        if cand in allowed:
            return cand
    return DEFAULT_ASPECT_RATIO


def ok(**meta) -> GenResult:
    return GenResult(ok=True, meta=meta)


def fail(code: str, message: str) -> GenResult:
    return GenResult(ok=False, error_code=code, error_message=message)
