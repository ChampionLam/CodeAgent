"""扫描件 OCR 兜底：read_file 提取层抛 NeedsOcr 后的两条路。

背景：用户给 agent 的 PDF 常常是扫描件（无文本层）。read_extract 的
anydoc 路径会抛 NeedsOcrError/NeedsOcrExtraction —— 这不是错误，是
「这份内容需要看图识字」。本模块负责两条兜底路：

  (a) hosted：配置了 Firecrawl 类 key 时，用 anydoc 自带的 ocr='hosted'
      云端 OCR（key 由调用方传入，本模块不读环境、不落盘）。
  (b) local：pymupdf 把缺文本层的页渲染成 PNG，交给应用自己的多模态
      模型（llm.py 的 chat_once，模型配置复用应用现有的 vision 线路）
      逐页转文字。页数与图像尺寸都有上限 —— 别把 300 页扫描件一次性
      塞进模型。

设计约束（与 attachments/llm 一致）：
  * 不读配置文件、不读环境变量 —— api_key/model 配置全部由调用方
    （sidecar）注入，便于测试与部署面控制。
  * key 只进请求头，绝不进日志/返回值。
  * 失败如实上抛 OcrError（带 code），调用方决定降级为提示还是重试。
"""
from __future__ import annotations
import re

import base64
import os
from typing import Any, Callable, Optional

__all__ = [
    "OcrError",
    "OcrLimits",
    "HostedOcrConfig",
    "hosted_ocr",
    "local_ocr",
    "DEFAULT_OCR_LIMITS",
]

DEFAULT_OCR_LIMITS = None  # 占位（见 OcrLimits）


class OcrError(Exception):
    """OCR 兜底失败。code 机器可读（HOSTED_FAILED / LOCAL_FAILED / LIMITS）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class OcrLimits:
    """本地 OCR 的硬上限：页数、单页像素、DPI。"""

    def __init__(self, *, max_pages: int = 8, max_dimension: int = 1600,
                 dpi: int = 150) -> None:
        self.max_pages = int(max_pages)
        self.max_dimension = int(max_dimension)
        self.dpi = int(dpi)


class HostedOcrConfig:
    """(a) 路的配置。api_key 由调用方注入；None = 未配置（走不了这条路）。"""

    def __init__(self, api_key: Optional[str] = None,
                 api_url: Optional[str] = None) -> None:
        key = api_key.strip() if isinstance(api_key, str) else None
        url = api_url.strip() if isinstance(api_url, str) else None
        self.api_key = key or None
        self.api_url = url or None

    @property
    def enabled(self) -> bool:
        """只有非空的 key（去掉空白后仍非空）才算这条路可用。"""
        return bool(self.api_key and self.api_key.strip())


def hosted_ocr(path: str, cfg: HostedOcrConfig) -> str:
    """(a) 路：anydoc 的云端 OCR。返回 Markdown 文本。

    任何失败都转成 OcrError("HOSTED_FAILED", ...)，由调用方降级。
    """
    if not cfg.enabled:
        raise OcrError("HOSTED_FAILED", "未配置云端 OCR key（Firecrawl）")
    import read_extract

    mod = read_extract._anydoc()
    if mod is None:
        raise OcrError("HOSTED_FAILED", "anydoc 不可用，无法走云端 OCR")
    kwargs: dict = {"ocr": "hosted"}
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    if cfg.api_url:
        kwargs["api_url"] = cfg.api_url
    try:
        text = mod.to_markdown(path, **kwargs)
    except Exception as exc:
        # HostedError / 网络错误都归到一条；绝不含 key。
        raise OcrError("HOSTED_FAILED", "云端 OCR 失败: %s: %s"
                       % (type(exc).__name__, exc)) from exc
    if not isinstance(text, str) or not text.strip():
        raise OcrError("HOSTED_FAILED", "云端 OCR 返回空文本")
    return text.rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# (b) 本地路：pymupdf 渲染 + 应用自己的多模态模型
# ---------------------------------------------------------------------------


def _open_pdf(path: str):
    try:
        import pymupdf
    except Exception as exc:
        raise OcrError("LOCAL_FAILED", "pymupdf 不可用: %s" % (exc,)) from exc
    try:
        return pymupdf.open(path)
    except Exception as exc:
        raise OcrError("LOCAL_FAILED", "打不开 PDF: %s" % (exc,)) from exc


def _render_pages(path: str, pages: list, limits: OcrLimits) -> tuple:
    """把指定页（1-based）渲染成 (页码, PNG bytes)。

    返回 (rendered, skipped)：页数超过 limits.max_pages 时**不放弃**，
    先渲染前 max_pages 页，剩下的页码原样回给调用方去提示模型续读。
    """
    doc = _open_pdf(path)
    try:
        page_count = doc.page_count
        wanted = sorted({p for p in pages if 1 <= p <= page_count}) or [
            i + 1 for i in range(min(page_count, limits.max_pages))]
        skipped: list = []
        if len(wanted) > limits.max_pages:
            skipped = wanted[limits.max_pages:]
            wanted = wanted[:limits.max_pages]
        out = []
        for pno in wanted:
            page = doc[pno - 1]
            pix = page.get_pixmap(dpi=limits.dpi)
            if max(pix.width, pix.height) > limits.max_dimension:
                scale = limits.max_dimension / float(max(pix.width, pix.height))
                mat = _fitz_matrix(doc, scale)
                pix = page.get_pixmap(matrix=mat)
            out.append((pno, pix.tobytes("png")))
        return out, skipped
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _fitz_matrix(doc, scale: float):
    import pymupdf

    return pymupdf.Matrix(scale, scale)


_OCR_PROMPT = (
    "你是 OCR 助手。请把这张图片里的所有文字完整转写为纯文本输出：\n"
    "1. 保持原文的段落与换行结构；\n"
    "2. 表格用竖线加换行的形式逐行列出；\n"
    "3. 只输出转写结果，不要任何解释、评论或 markdown 代码块包裹；\n"
    "4. 看不清的字用「□」占位。"
)


def _page_messages(png: bytes) -> list:
    data_url = "data:image/png;base64,%s" % base64.b64encode(png).decode("ascii")
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": _OCR_PROMPT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
    }]


_THINK_RE = re.compile(r"(?is)<think(?:ing)?>.*?</think(?:ing)?>")
_OPEN_THINK_RE = re.compile(r"(?is)<think(?:ing)?>.*$")


def _strip_thinking(text: str) -> str:
    """剥掉模型自带的思考块。

    MiniMax / Qwen 这类模型会把  thinking... 一起返回；OCR 正文里
    带推理过程既污染上下文又浪费 token（真机上抓到过）。未闭合的（被截断）
    思考块一并从  thinking 处截掉。
    """
    cleaned = _THINK_RE.sub("", text or "")
    cleaned = _OPEN_THINK_RE.sub("", cleaned)
    return cleaned.strip()


def _pages_note(skipped: list, cap: int) -> str:
    """截断时的续读提示：告诉模型还有哪些页没读、怎么续读。"""
    if not skipped:
        return ""
    shown = ", ".join(str(p) for p in skipped[:24])
    if len(skipped) > 24:
        shown += " ...（共 %d 页）" % len(skipped)
    return ("\n\n[本地 OCR 单次上限 %d 页，本次没读的页：%s。要读这些页就再调一次 "
            "read_file 并在 pages 参数里指定，例如 \"pages\": \"%d-%d\"。]"
            % (cap, shown, skipped[0], skipped[-1]))


def local_ocr(path: str, pages: list, *, chat: Callable[..., str],
              limits: Optional[OcrLimits] = None, stats: Optional[dict] = None) -> str:
    """(b) 路：渲染 + 逐页调应用的多模态模型。

    chat 必须是 llm.chat_messages_once 那样的可调用：接收 (messages)，
    返回完整正文 str，出错抛 LlmError。这里用鸭子类型注入 —— 不 import
    llm，避免把模型配置耦合进提取层。

    返回拼好的文本：每页一个「## 第 N 页」小节 + 模型转写。模型失败
    单页如实标注（OCR_FAILED_PAGE），不静默丢页。
    """
    limits = limits or OcrLimits()
    rendered, skipped = _render_pages(path, pages, limits)
    if stats is not None:
        stats["pages_read"] = [pno for pno, _ in rendered]
        stats["pages_skipped"] = list(skipped)
    parts: list = []
    for pno, png in rendered:
        try:
            text = _strip_thinking(chat(_page_messages(png)))
        except Exception as exc:
            parts.append("## 第 %d 页\n[本页 OCR 失败: %s]" % (pno, exc))
            continue
        if not text:
            text = "[本页模型未返回文本]"
        parts.append("## 第 %d 页\n%s" % (pno, text))
    if not parts:
        raise OcrError("LOCAL_FAILED", "没有可 OCR 的页：%s" % (path,))
    return "\n\n".join(parts) + "\n" + _pages_note(skipped, limits.max_pages)


def extract_with_fallback(path: str, *, exc: "Exception",
                          hosted_cfg: Optional[HostedOcrConfig],
                          chat: Optional[Callable[..., str]],
                          limits: Optional[OcrLimits] = None,
                          pages_override: Optional[list] = None) -> tuple:
    """read_file 接的统一入口：NeedsOcrExtraction 之后的兜底分派。

    返回 (text, meta)：meta 描述走了哪条路（hosted/local/none）与页数。
    两条路都不可用/都失败时返回 ("", {"route": "none", ...})，调用方
    把 needs_ocr_message 展示给模型 —— 这不是异常路径，是「如实告诉
    模型这文件读不了、怎么补」。
    """
    pages = list(pages_override or getattr(exc, "pages", []) or [])
    meta: dict = {"route": "none", "pages": pages, "pages_read": [],
                  "pages_skipped": [], "hosted_error": "", "local_error": ""}
    if hosted_cfg is not None and hosted_cfg.enabled:
        try:
            text = hosted_ocr(path, hosted_cfg)
            meta["route"] = "hosted"
            return text, meta
        except OcrError as oerr:
            meta["hosted_error"] = "%s: %s" % (oerr.code, oerr.message)
    if chat is not None:
        try:
            stats: dict = {}
            text = local_ocr(path, pages, chat=chat, limits=limits, stats=stats)
            meta["route"] = "vision"
            meta["pages_read"] = stats.get("pages_read", [])
            meta["pages_skipped"] = stats.get("pages_skipped", [])
            return text, meta
        except OcrError as oerr:
            meta["local_error"] = "%s: %s" % (oerr.code, oerr.message)
    return "", meta
