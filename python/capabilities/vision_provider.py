"""VisionProvider 抽象基类 + 共用助手（desk-agent 独立能力层）。

形态参照 Hermes Agent：vision 是独立能力 + 独立配置，走自己的 provider/model，
不占用主聊天模型的循环。本文件只放三样东西：

  1. VisionProvider 抽象基类（describe() 契约签名）
  2. 共用助手：ok_result / fail_result 构造、错误码常量、
     思考分流（复用 python/reasoning_split.py 的 ReasoningSplitter）
  3. 网络注入协议说明（opener 形状照第一批 gen_provider.default_opener，
     本模块不重新发明 —— provider 直接复用那个实现）

硬约束：
  * 所有网络调用必须走 opener（可注入），单测一律传假 opener，绝不打真网络。
  * 思考与正文两条通道都保留，一个字都不丢。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Callable

import reasoning_split

# ---------------------------------------------------------------------------
# 常量（契约 8.2 固定集合，别加别减）
# ---------------------------------------------------------------------------

VISION_ASPECT_HINT = "图片按原始分辨率送出，不做裁剪"

NOT_CONFIGURED = "NOT_CONFIGURED"
MODEL_NO_IMAGE_INPUT = "MODEL_NO_IMAGE_INPUT"
IMAGE_TOO_LARGE = "IMAGE_TOO_LARGE"
UNSUPPORTED_IMAGE = "UNSUPPORTED_IMAGE"
AUTH = "AUTH"
NETWORK = "NETWORK"
TIMEOUT = "TIMEOUT"
PROVIDER_ERROR = "PROVIDER_ERROR"
CANCELLED = "CANCELLED"

VISION_ERROR_CODES = (
    NOT_CONFIGURED, MODEL_NO_IMAGE_INPUT, IMAGE_TOO_LARGE, UNSUPPORTED_IMAGE,
    AUTH, NETWORK, TIMEOUT, PROVIDER_ERROR, CANCELLED,
)

# opener 协议（与第一批 gen_provider.Opener 完全一致）：
#   url, data=None, headers=None, timeout=..., method=... -> (status:int, body:bytes)
Opener = Callable[..., tuple[int, bytes]]


# ---------------------------------------------------------------------------
# 结果与异常
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VisionResult:
    """vision provider describe() 的统一返回值（字段集合照契约 8.2）。

    成功：ok=True, text=正文（思考已剥离），reasoning=思考（独立通道，不丢字），
          meta=normalize meta + usage + elapsed_ms 等。
    失败：ok=False, error_code=固定错误码, error_message=人话。
    """

    ok: bool
    text: str = ""
    reasoning: str = ""
    provider: str = ""
    model: str = ""
    attachment_uri: str | None = None
    meta: dict = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


class VisionError(Exception):
    """provider 内部控制流异常。describe() 的对外统一出口是 VisionResult，不是异常。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code + ": " + message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class VisionProvider(abc.ABC):
    """vision provider 抽象基类（契约 8.2）。

    子类给出类属性 name / api_key_env，实现 describe()。
    网络一律通过注入的 opener（默认复用第一批 gen_provider.default_opener，
    不另起炉灶）；单测传假 opener，绝不打真网络。
    """

    name: str = ""
    api_key_env: str = ""

    @abc.abstractmethod
    def describe(self, *, data_url: str, question: str, model: str | None = None,
                 timeout: int = 120, opener=None,
                 is_cancelled: Callable[[], bool] = lambda: False) -> VisionResult:
        """把一张图 + 一个问题交给视觉模型，返回 VisionResult。

        data_url 必须是 data:<mime>;base64,<...> 形式；question 是用户问题原文。
        opener 为 None 时用默认 urllib opener（生产路径）；单测注入假 opener。
        """
        raise NotImplementedError

    # ---- 子类可用的共享机制 ------------------------------------------------
    def _require_key(self, api_key: str | None) -> str:
        if not (api_key or "").strip():
            raise VisionError(NOT_CONFIGURED,
                              "%s 未设置（vision 能力密钥只从环境变量读）" % self.api_key_env)
        return api_key.strip()

    def _guard(self, fn: Callable[[], VisionResult]) -> VisionResult:
        """把内部 VisionError 控制流翻译成失败 VisionResult（describe 的统一出口）。"""
        try:
            return fn()
        except VisionError as e:
            return fail_result(e.code, e.message)


# ---------------------------------------------------------------------------
# 共用助手（模块级；provider 与测试都直接用这些）
# ---------------------------------------------------------------------------

def ok_result(**kw) -> VisionResult:
    """成功结果。常用字段：text / reasoning / provider / model / attachment_uri / meta。"""
    return VisionResult(ok=True, **kw)


def fail_result(code: str, message: str, **kw) -> VisionResult:
    """失败结果。code 必须在 VISION_ERROR_CODES 里，其余字段（provider/model 等）可附带。"""
    if code not in VISION_ERROR_CODES:
        code = PROVIDER_ERROR
    return VisionResult(ok=False, error_code=code, error_message=message, **kw)


def split_content_text(raw: str) -> tuple[str, str]:
    """把模型输出正文里的内联思考标签分流成 (reasoning, text)。

    复用已有 reasoning_split.ReasoningSplitter：一次性 feed 全文 + flush。
    两条通道都保留，一个字都不丢 —— 分流是为了分开渲染，不是为了删内容。
    """
    if not isinstance(raw, str):
        raw = "" if raw is None else str(raw)
    sp = reasoning_split.ReasoningSplitter()
    r1, t1 = sp.feed(raw)
    r2, t2 = sp.flush()
    return (r1 + r2, t1 + t2)
