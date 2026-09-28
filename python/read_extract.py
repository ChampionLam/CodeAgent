"""文档提取层：把 PDF / Office / OpenDocument / Notebook 转成文本。

目标（对标同类 agent read_file 的文档能力，但独立实现，不复制其代码）：
read_file 碰到「扩展名像文档、字节不是纯文本」的文件时，不再把二进制
直接按 UTF-8 replacement 解出来喂给模型（那是乱码，会诱发模型去 shell
瞎试 pdftotext），而是先走这一层把文档真正转成文本。

分派表（按扩展名，大小写不敏感）：
  标准库路径（零三方依赖，永远可用）：
    .ipynb  -> json 解析 + 逐 cell 渲染（代码/输出都留，长输出截断）
    .docx   -> zipfile + word/document.xml（w:t/w:tab/w:br）
    .xlsx   -> zipfile + xl/worksheets/*.xml + sharedStrings
  anydoc 路径（可选依赖 firecrawl-anydoc，装了才启用）：
    .pdf .doc .docm .ppt .pps .pot .pptx .pptm .ppsx .ppsm
    .xls .xlsm .xlsb .odt .ods .odp .rtf .epub
    -> anydoc.to_markdown()，统一转 Markdown
    （.docx/.xlsx 仍走标准库路径 —— 装不装 anydoc 行为一致）

错误分类：本层只有一个 ExtractionError（read_file 侧按 fail 处理，
不抛给 agent_loop）；失败信息里保留 anydoc 的原始异常类型名
（UnsupportedError / MalformedError / EncryptedError / NeedsOcrError /
HostedError / PackageNotFoundError / ResourceLimitError），方便分诊。

尺寸上限：MAX_DOCUMENT_BYTES = 50 MiB。anydoc 的 Rust 内核是整文件进
内存无流式，pymupdf 也一样，不设顶会把一个工具回合钉死在 RAM 上。

扫描件：PDF 部分页有文本层、部分页是纯图片时，用 pymupdf 逐页数
字符，给一条「哪几页没读到、怎么补」的明确说明（covernote），绝不
静默丢内容。anydoc 抛 NeedsOcrError（整个文件都是扫描件）时由
sidecar 的 OCR 兜底接管（hosted / 本地多模态），见 sidecar.py。

本模块不做权限判断、不碰网络、不读应用配置 —— 全部参数由调用方传入。
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

__all__ = [
    "ExtractionError",
    "MAX_DOCUMENT_BYTES",
    "STDLIB_EXTENSIONS",
    "STDLIB_TEXT_EXTENSIONS",
    "is_text_attachment",
    "looks_like_text",
    "extract_text_bytes",
    "ANYDOC_EXTENSIONS",
    "EXTRACTABLE_EXTENSIONS",
    "anydoc_available",
    "extract_document_text",
    "extract_document_bytes",
    "is_extractable_document",
    "pdf_page_coverage",
    "needs_ocr_message",
    "PDF_EMPTY_PAGE_CHARS",
    "PDF_COVERAGE_MIN_EMPTY",
    "PDF_COVERAGE_MIN_RATIO",
    "PDF_COVERAGE_ABSOLUTE_EMPTY",
    "PDF_COVERAGE_SCAN_TIMEOUT",
]

# ---------------------------------------------------------------------------
# 分派表 / 上限
# ---------------------------------------------------------------------------

#: 标准库就能解的三种格式（零依赖路径，永远可用）。
STDLIB_EXTENSIONS = frozenset({".ipynb", ".docx", ".xlsx"})

#: 纯文本类格式：零依赖、直接按文本读（日志 / 配置 / 源码 / CSV 等）。
#: 用户 2026-09-26 反馈拖 .log 进来不支持 —— 这类文件根本不需要转换库，
#: 缺的是「按文本读」这条支路（anydoc 只管 Office/PDF/EPUB/CSV，不含纯文本）。
STDLIB_TEXT_EXTENSIONS = frozenset({
    # 纯文本与日志
    ".txt", ".text", ".log", ".out", ".err", ".trace",
    # 标记 / 结构化文本
    ".md", ".markdown", ".rst", ".json", ".jsonl", ".ndjson",
    ".csv", ".tsv", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".properties", ".xml", ".html", ".htm", ".css", ".sql",
    # 脚本与源码
    ".py", ".pyi", ".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1", ".psm1",
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".lua",
    ".r", ".swift", ".scala", ".vue", ".svelte",
    # 补丁 / 差异
    ".diff", ".patch",
})

#: 只在装了 firecrawl-anydoc（import anydoc）后才启用的格式。
ANYDOC_EXTENSIONS = frozenset({
    ".pdf",
    ".doc", ".docm",
    ".ppt", ".pps", ".pot", ".pptx", ".pptm", ".ppsx", ".ppsm",
    ".xls", ".xlsm", ".xlsb",
    ".odt", ".ods", ".odp",
    ".rtf", ".epub",
})

#: read_file 侧「这个扩展名值得走提取层」的完整集合（含未装 anydoc 的情况
#: —— 未装时打开会得到一条教安装的错误，而不是乱码）。
#: read_file 的分派闸门：**只放文档**。纯文本故意不在这里 —— read_file 对
#: 文本走的是「按行读、带行号」的另一条路，塞进来会把输出形态改坏（2026-09-26
#: 一次改坏被现有用例抓住）。附件侧的文本判定见 is_text_attachment。
EXTRACTABLE_EXTENSIONS = STDLIB_EXTENSIONS | ANYDOC_EXTENSIONS

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
#: 纯文本读取上限：超过就拒绝（免得把几百 MB 日志灌进上下文）。
MAX_TEXT_BYTES = 4 * 1024 * 1024
#: 「像不像文本」只看开头这么多字节。
_TEXT_SNIFF_BYTES = 64 * 1024
#: UTF-16 的 BOM：带 BOM 的文本里天然有 NUL，得先认出来再判文本。
_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
MAX_XLSX_BYTES = 50 * 1024 * 1024
_MAX_XLSX_ROWS_PER_SHEET = 5000
_MAX_XLSX_COLS = 256
#: 单个 notebook 输出块的截断上限（防一条训练日志淹掉整个提取）。
_MAX_OUTPUT_CHARS = 20_000

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


class ExtractionError(Exception):
    """受支持的文档没能转成文本。message 会带原因与（可能的）修复建议。"""


# ---------------------------------------------------------------------------
# anydoc 可选依赖（懒加载，缺失/失败退避重试，绝不阻塞）
# ---------------------------------------------------------------------------

_ANYDOC_UNSET = object()
_anydoc_module: Any = _ANYDOC_UNSET
_anydoc_lock = threading.Lock()
#: 首次 import 失败后的重试间隔（秒）。进程内不要每次 read_file 都硬试。
ANYDOC_RETRY_SECONDS = 300.0
_anydoc_failed_at: Optional[float] = None


def parse_pages_arg(spec, page_count: int = 0) -> list:
    """把 "9-16" / "3" / "1,5,9" 解析成 1-based 页码列表。

    只做解析不抛：看不懂的片段丢弃；page_count > 0 时丢掉越界页。
    大扫描件续读时，模型用 read_file 的 pages 参数走这里。
    """
    out = []
    for chunk in str(spec or "").replace("\uff0c", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(\d+)\s*[-\u2013~]\s*(\d+)$", chunk)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            out.extend(range(lo, hi + 1))
            continue
        if chunk.isdigit():
            out.append(int(chunk))
    uniq = sorted({p for p in out if p >= 1})
    if page_count > 0:
        uniq = [p for p in uniq if p <= page_count]
    return uniq


def _anydoc() -> Optional[Any]:
    """懒加载 anydoc；不可用时返回 None（调用方自己给出教学式错误）。"""
    global _anydoc_module, _anydoc_failed_at
    if _anydoc_module is not _ANYDOC_UNSET:
        return _anydoc_module
    with _anydoc_lock:
        if _anydoc_module is not _ANYDOC_UNSET:
            return _anydoc_module
        if (
            _anydoc_failed_at is not None
            and time.monotonic() - _anydoc_failed_at < ANYDOC_RETRY_SECONDS
        ):
            return None
        try:
            import importlib

            _anydoc_module = importlib.import_module("anydoc")
        except Exception:  # ImportError 或坏的原生绑定
            _anydoc_failed_at = time.monotonic()
            return None
        _anydoc_failed_at = None
    return _anydoc_module  # type: ignore[return-value]


def anydoc_available() -> bool:
    """anydoc 是否可用（schema/description 想按可用性分支时用）。"""
    return _anydoc() is not None


def _anydoc_missing_error(path: str) -> str:
    """anydoc 未装时的教学式错误文案（教怎么装，不指责用户）。"""
    return (
        "无法转换 %s：该格式需要可选依赖 firecrawl-anydoc（import anydoc），"
        "当前未安装。安装方法：在应用 venv 里执行 "
        "`pip install firecrawl-anydoc pymupdf`；或先用外部工具转成 "
        "docx/txt 再读。其余读取能力不受影响。" % (path,)
    )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def looks_like_text(data: bytes) -> bool:
    """字节像不像文本：带 UTF-16 BOM 算文本；有 NUL 不算；采样里可打印占比要够高。"""
    if not data:
        return True
    if data[:2] in _UTF16_BOMS:
        return True
    sample = data[:_TEXT_SNIFF_BYTES]
    if b"\x00" in sample:
        return False
    printable = sum(
        1 for b in sample if b in (9, 10, 13) or 32 <= b <= 126 or b >= 0x80)
    return printable / len(sample) >= 0.90


def _decode_text(data: bytes) -> str:
    """按常见编码依次试：UTF-8(BOM/裸) → GBK → UTF-16。

    Windows 上的日志经常是 GBK（中文乱码就是这么来的），所以必须试 GBK；
    全都不成才抛错 —— 绝不返回乱码。
    """
    for enc in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
        if enc == "utf-16" and len(data) % 2:
            continue
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ExtractionError("无法按文本解码（已尝试 utf-8 / gbk / utf-16）")


def _truncate_text(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    omitted = len(text) - _MAX_OUTPUT_CHARS
    return text[:_MAX_OUTPUT_CHARS] + "\n… [%d 字符已截断]" % (omitted,)


def extract_text_bytes(data: bytes, name: str = "") -> str:
    """纯文本提取（内存字节版，附件走这条）。"""
    if len(data) > MAX_TEXT_BYTES:
        raise ExtractionError(
            "文本过大（%d 字节，上限 %d 字节）" % (len(data), MAX_TEXT_BYTES))
    if not looks_like_text(data):
        raise ExtractionError(
            "看起来是二进制文件，不是文本%s" % ("：" + name if name else ""))
    text = _truncate_text(_decode_text(data))
    return text if text.endswith("\n") else text + "\n"


def _looks_like_text_file(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return looks_like_text(fh.read(_TEXT_SNIFF_BYTES))
    except OSError:
        return False


def _extract_text_file(path: str) -> str:
    try:
        size = os.path.getsize(path)
        if size > MAX_TEXT_BYTES:
            raise ExtractionError(
                "文本过大（%d 字节，上限 %d 字节）" % (size, MAX_TEXT_BYTES))
        with open(path, "rb") as fh:
            data = fh.read(MAX_TEXT_BYTES + 1)
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    return extract_text_bytes(data, name=os.path.basename(path))


def is_extractable_document(path: str) -> bool:
    """扩展名命中提取表（含 anydoc-gated 格式，无论装没装）。"""
    return Path(str(path)).suffix.lower() in EXTRACTABLE_EXTENSIONS


def is_text_attachment(path: str) -> bool:
    """附件闸门：扩展名是文本类，或（扩展名不认识时）内容像文本。

    单独一个函数是有意的：read_file 的闸门（is_extractable_document）只管文档，
    而附件这边 .log/.txt/….trace/没后缀的日志都该能收进来直接读。
    """
    p = str(path)
    if Path(p).suffix.lower() in STDLIB_TEXT_EXTENSIONS:
        return True
    return os.path.isfile(p) and _looks_like_text_file(p)


def extract_document_text(path: str) -> str:
    """按扩展名分派，返回提取出的文本（带结尾换行）。

    任何「格式支持但解不出来」的情况都抛 ExtractionError —— read_file
    会把它转成一个如实的工具失败（带原因与建议），绝不返回乱码。
    """
    ext = Path(str(path)).suffix.lower()
    if ext in STDLIB_TEXT_EXTENSIONS:
        return _extract_text_file(path)
    if ext in STDLIB_EXTENSIONS:
        _check_size(path)
        if ext == ".ipynb":
            return _extract_notebook(path)
        if ext == ".docx":
            return _extract_docx(path)
        return _extract_xlsx(path)
    if ext in ANYDOC_EXTENSIONS:
        # anydoc 没装、但内容是纯文本（比如 .csv）：直接按文本读，别为它拦路。
        if _anydoc() is None and (ext in STDLIB_TEXT_EXTENSIONS
                                  or _looks_like_text_file(path)):
            return _extract_text_file(path)
        return _extract_anydoc(path, ext)
    raise ExtractionError("不支持的文档类型: %r" % (path,))


def extract_document_bytes(data: bytes, path: str) -> str:
    """附件/内存字节版入口：按 path 的扩展名分派，同样返回文本。"""
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ExtractionError(
            "文档过大无法转换（%d 字节，上限 %d 字节）" % (len(data), MAX_DOCUMENT_BYTES))
    ext = Path(str(path)).suffix.lower()
    if ext in ANYDOC_EXTENSIONS:
        if _anydoc() is None and (ext in STDLIB_TEXT_EXTENSIONS
                                  or looks_like_text(data)):
            return extract_text_bytes(data, os.path.basename(str(path)))
        return _extract_anydoc_bytes(data, path)
    if ext in STDLIB_EXTENSIONS:
        pass  # 走下面的标准库路（.ipynb 是 JSON 文本，不能被嗅探截胡）
    elif ext in STDLIB_TEXT_EXTENSIONS or looks_like_text(data):
        return extract_text_bytes(data, os.path.basename(str(path)))
    else:
        raise ExtractionError("不支持的文档类型: %r" % (path,))
    import tempfile

    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as fh:
            fh.write(data)
            tmp_path = fh.name
        return extract_document_text(tmp_path)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _check_size(path: str) -> None:
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    if size > MAX_DOCUMENT_BYTES:
        raise ExtractionError(
            "文档过大无法转换（%d 字节，上限 %d 字节）" % (size, MAX_DOCUMENT_BYTES))


# ---------------------------------------------------------------------------
# anydoc 路径
# ---------------------------------------------------------------------------


def _extract_anydoc(path: str, ext: str) -> str:
    mod = _anydoc()
    if mod is None:
        raise ExtractionError(_anydoc_missing_error(path))
    _check_size(path)
    try:
        text = mod.to_markdown(path)
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    except Exception as exc:
        raise _map_anydoc_error(path, exc) from exc
    return _post_anydoc(text, path, ext)


def _extract_anydoc_bytes(data: bytes, path: str) -> str:
    mod = _anydoc()
    if mod is None:
        raise ExtractionError(_anydoc_missing_error(path))
    to_bytes = getattr(mod, "to_markdown_bytes", None)
    if to_bytes is None:
        # 老版本只有路径入口：落临时文件再走路径。
        import tempfile

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=Path(str(path)).suffix.lower(), delete=False) as fh:
                fh.write(data)
                tmp_path = fh.name
            return _extract_anydoc(tmp_path, Path(str(path)).suffix.lower())
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    try:
        text = to_bytes(data)
    except Exception as exc:
        raise _map_anydoc_error(path, exc) from exc
    return _post_anydoc(text, path, Path(str(path)).suffix.lower())


def _map_anydoc_error(path: str, exc: Exception) -> ExtractionError:
    """把 anydoc 的 ConvertError 家族映射成 ExtractionError（保留类型名）。

    NeedsOcrError 特殊：它不是「坏了」而是「这文件是纯扫描件」，read_file
    拿到后要转 OCR 兜底路径而不是当普通失败 —— 所以单独一个子类标记。
    """
    if type(exc).__name__ == "NeedsOcrError":
        pages = list(getattr(exc, "pages", []) or [])
        return NeedsOcrExtraction(path, pages)
    return ExtractionError("%s: %s" % (type(exc).__name__, exc))


class NeedsOcrExtraction(ExtractionError):
    """整份文档都是扫描件（无文本层），需要 OCR 兜底。

    pages 是 1-based 页码列表（可能为空 = anydoc 没给）。read_file 把它
    转成 OCR 提示信息（needs_ocr_message）而不是报错。
    """

    def __init__(self, path: str, pages: list) -> None:
        self.path = path
        self.pages = list(pages)
        super().__init__(
            "纯扫描件（无文本层），需要 OCR：%s（缺文本层的页：%s）"
            % (path, ", ".join(str(p) for p in self.pages) or "未知"))


def _post_anydoc(text: str, path: str, ext: str) -> str:
    """anydoc 成功返回后的公共后处理：空文本判断 + PDF 覆盖率检查。"""
    if not isinstance(text, str) or not text.strip():
        raise ExtractionError("文档没有可提取的文本")
    text = text.rstrip("\n") + "\n"
    if ext == ".pdf":
        note = pdf_coverage_note(path)
        if note:
            # 放头部：read_file 会截断长文本，放尾部模型可能永远看不到。
            text = note + text
    return text


# ---------------------------------------------------------------------------
# PDF 页级覆盖率（部分页是扫描件 → 明确说明，不静默丢内容）
# ---------------------------------------------------------------------------

PDF_EMPTY_PAGE_CHARS = 20
PDF_COVERAGE_MIN_EMPTY = 2
PDF_COVERAGE_MIN_RATIO = 0.2
PDF_COVERAGE_ABSOLUTE_EMPTY = 10
#: 逐页文本扫描的页数上限（防 300 页 PDF 钉死工具回合）。
PDF_COVERAGE_SCAN_TIMEOUT = 60.0


def _pdf_page_texts(path: str) -> Optional[list]:
    """逐页提取文本。优先 pymupdf；没有则退 pdftotext 子进程；都不行 → None。"""
    texts = _pdf_page_texts_pymupdf(path)
    if texts is not None:
        return texts
    return _pdf_page_texts_pdftotext(path)


def _pdf_page_texts_pymupdf(path: str) -> Optional[list]:
    try:
        import pymupdf
    except Exception:
        return None
    try:
        doc = pymupdf.open(path)
    except Exception:
        return None
    try:
        if doc.page_count > 2000:
            return None
        return [page.get_text("text") for page in doc]
    except Exception:
        return None
    finally:
        try:
            doc.close()
        except Exception:
            pass


def _pdf_page_texts_pdftotext(path: str) -> Optional[list]:
    import shutil as _shutil
    import subprocess

    if _shutil.which("pdftotext") is None:
        return None
    try:
        proc = subprocess.run(
            ["pdftotext", path, "-"],
            capture_output=True, timeout=PDF_COVERAGE_SCAN_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    pages = proc.stdout.decode("utf-8", errors="replace").split("\f")
    if pages and not pages[-1].strip():
        pages.pop()  # 结尾 form-feed 伪页
    return pages or None


def pdf_page_coverage(path: str) -> Optional[list]:
    """每页提取到的字符数（0-based 列表）。无法确定时返回 None。"""
    pages = _pdf_page_texts(path)
    if pages is None:
        return None
    return [len(p.strip()) for p in pages]


def _group_ranges(pages: list) -> list:
    ranges: list = []
    for p in pages:
        if ranges and p == ranges[-1][1] + 1:
            ranges[-1][1] = p
        else:
            ranges.append([p, p])
    return ranges


def _page_ranges(pages: list, cap: int = 12) -> str:
    parts = [("%d-%d" % (a, b)) if a != b else str(a)
             for a, b in _group_ranges(sorted(pages))]
    if len(parts) > cap:
        parts = parts[:cap] + ["…"]
    return ", ".join(parts)


#: 覆盖率警告里 gap 明细的条数上限（病态 PDF 不撑爆警告本身）。
PDF_GAP_MAP_MAX_ENTRIES = 20
_GAP_CONTEXT_CHARS = 60


def _gap_map(counts: list, texts: list, empty: list) -> str:
    """每个空洞区间标注它前面最近的一段文字（通常是章节分隔页）。"""
    ranges = _group_ranges(empty)
    lines: list = []
    for a, b in ranges[:PDF_GAP_MAP_MAX_ENTRIES]:
        label = ""
        for prev in range(a - 2, -1, -1):
            if counts[prev] >= PDF_EMPTY_PAGE_CHARS:
                snippet = " ".join(texts[prev].split())[:_GAP_CONTEXT_CHARS]
                label = ' —— 前文 "%s"（第 %d 页）' % (snippet, prev + 1)
                break
        span = "第 %d 页" % a if a == b else "第 %d-%d 页" % (a, b)
        n = b - a + 1
        lines.append("  %s（%d 页）%s" % (span, n, label))
    if len(ranges) > PDF_GAP_MAP_MAX_ENTRIES:
        rest = ranges[PDF_GAP_MAP_MAX_ENTRIES:]
        rest_pages = sum(b - a + 1 for a, b in rest)
        lines.append("  … 其余 %d 段（共 %d 页）" % (len(rest), rest_pages))
    return "\n".join(lines)


def pdf_coverage_note(path: str, display_path: Optional[str] = None) -> str:
    """很多 PDF 页提取不到文本时返回一条响亮的头部警告，否则返回 ''。"""
    texts = _pdf_page_texts(path)
    if not texts or len(texts) < 2:
        return ""
    counts = [len(p.strip()) for p in texts]
    empty = [i + 1 for i, n in enumerate(counts) if n < PDF_EMPTY_PAGE_CHARS]
    total = len(counts)
    if len(empty) < PDF_COVERAGE_MIN_EMPTY:
        return ""
    if (len(empty) / total < PDF_COVERAGE_MIN_RATIO
            and len(empty) < PDF_COVERAGE_ABSOLUTE_EMPTY):
        return ""
    shown = display_path or path
    return (
        "[提取覆盖率警告：%d / %d 页没有提取到文本。这些页很可能是扫描图片"
        "（或空白页）—— 下面的提取文本里【没有】它们的内容，即使某些章节"
        "标题下面看起来是空的。读不到的区间（每段标注了它前面最近的文字）：\n"
        "%s\n"
        "请先判断哪些区间真的需要读，不要全部 OCR。需要的区间可以让用户"
        "把对应页截图发到对话里（我按图看），或在外部 OCR 处理 %s。]\n"
        % (len(empty), total, _gap_map(counts, texts, empty), shown)
    )


def needs_ocr_message(path: str, pages: list, hosted_error: str = "") -> str:
    """整份文件都是扫描件（NeedsOcrError）时给模型的提示信息。"""
    page_list = ", ".join(str(p) for p in pages) if pages else "未知"
    msg = (
        "[需要 OCR：%s 是纯扫描件（%s 页没有文本层），文字内容未能提取。"
        % (path, page_list)
    )
    if hosted_error:
        msg += "云端 OCR 已尝试但失败（%s）。" % (hosted_error,)
    msg += (
        "如这部分内容重要：让用户把这几页截图（或整份 PDF）直接发到对话里，"
        "我按图看；或在外部 OCR 成文本后再读。不要为此反复重试读取或"
        "改走命令行工具。]"
    )
    return msg + "\n"


# ---------------------------------------------------------------------------
# 标准库路径：.ipynb
# ---------------------------------------------------------------------------


def _source_text(source) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, list):
        return "".join(item for item in source if isinstance(item, str))
    return ""


def _clean_stream_text(text: str) -> str:
    """去掉 ANSI 转义、折叠 \\r 进度条重绘（只留每行最后一帧）。"""
    cleaned = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text).replace("\r\n", "\n")
    lines = []
    for line in cleaned.split("\n"):
        frames = [f for f in line.split("\r") if f]
        lines.append(frames[-1] if frames else "")
    return "\n".join(lines)


def _base64_bytes(payload: str) -> int:
    """估算 base64 payload 解码后的字节数（忽略空白）。"""
    clean = re.sub(r"[^0-9+/=A-Za-z]", "", payload)
    padding = min(2, len(clean) - len(clean.rstrip("=")))
    return max(0, (len(clean) * 3) // 4 - padding)


def _human_size(n_bytes: int) -> str:
    return ("%d KB" % round(n_bytes / 1024)) if n_bytes >= 1024 else ("%d B" % n_bytes)


def _notebook_output_text(output: Any) -> str:
    """渲染一个 notebook 输出块：流文本/错误栈/文本结果保留，重载荷折成占位。"""
    if not isinstance(output, dict):
        return ""
    otype = output.get("output_type")
    if otype == "stream":
        body = _clean_stream_text(_source_text(output.get("text", "")))
        return body if body.strip() else ""
    if otype in {"error", "pyerr"}:
        traceback = output.get("traceback")
        tb_text = ""
        if isinstance(traceback, list):
            tb_text = _clean_stream_text(
                "\n".join(line for line in traceback if isinstance(line, str)))
        header = "Error: %s: %s" % (output.get("ename", ""), output.get("evalue", ""))
        return ("%s\n%s" % (header.rstrip(": "), tb_text)).rstrip()
    if otype in {"execute_result", "display_data", "pyout"}:
        data = output.get("data")
        if not isinstance(data, dict):
            # nbformat v3：mime 数据平铺在输出 dict 上。
            data = {}
            if isinstance(output.get("text"), (str, list)):
                data["text/plain"] = output["text"]
            for v3_key, mime in (("png", "image/png"), ("jpeg", "image/jpeg"),
                                 ("svg", "image/svg+xml"), ("html", "text/html")):
                if v3_key in output:
                    data[mime] = output[v3_key]
        for mime in ("text/plain", "text/markdown"):
            if mime in data:
                body = _clean_stream_text(_source_text(data[mime]))
                if body.strip():
                    return body
        for mime, value in data.items():
            if isinstance(mime, str) and mime.startswith("image/"):
                size = _base64_bytes(_source_text(value))
                return "[%s 输出 —— %s，已省略]" % (mime, _human_size(size))
        if "text/html" in data:
            html = _source_text(data["text/html"])
            return "[text/html 输出 —— %d 字符，已省略]" % (len(html),)
        mimes = ", ".join(str(m) for m in data) or "unknown"
        return "[%s 输出 —— 已省略]" % (mimes,)
    return ""


def _notebook_outputs(cell: dict) -> str:
    outputs = cell.get("outputs")
    if not isinstance(outputs, list):
        return ""
    blocks = [t for t in (_notebook_output_text(o) for o in outputs) if t]
    if not blocks:
        return ""
    joined = "\n".join(blocks)
    if len(joined) > _MAX_OUTPUT_CHARS:
        omitted = len(joined) - _MAX_OUTPUT_CHARS
        joined = joined[:_MAX_OUTPUT_CHARS] + "\n… [%d 输出字符已截断]" % (omitted,)
    return joined


def _extract_notebook(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            nb = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ExtractionError("不是合法的 notebook: %s" % (exc,)) from exc
    if not isinstance(nb, dict):
        raise ExtractionError("notebook 根不是对象")

    raw_cells = nb.get("cells")
    if isinstance(raw_cells, list):
        cells = raw_cells
    else:
        # nbformat v3：cells 挂在 worksheets 下。
        cells = [
            cell
            for ws in nb.get("worksheets", []) if isinstance(ws, dict)
            for cell in ws.get("cells", []) if isinstance(cell, dict)
        ]
    if not cells:
        raise ExtractionError("notebook 没有 cells")

    counts = {"markdown": 0, "code": 0, "raw": 0}
    labels = {"markdown": "Markdown", "code": "Code", "raw": "Raw"}
    out: list = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        typ = cell.get("cell_type")
        if typ not in labels:
            continue
        counts[typ] += 1
        suffix = " %d" % counts[typ] if typ != "raw" else ""
        out.append("# ── %s cell%s ──" % (labels[typ], suffix))
        out.append(_source_text(cell.get("source", "")).rstrip("\n"))
        out.append("")
        if typ == "code":
            rendered = _notebook_outputs(cell)
            if rendered:
                out.append("# ── Output (cell %d) ──" % counts[typ])
                out.append(rendered.rstrip("\n"))
                out.append("")
    if len(out) <= 3:
        raise ExtractionError("notebook 没有可读的 cell")
    return "\n".join(out).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# 标准库路径：.docx
# ---------------------------------------------------------------------------


def _zip_xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(zf.read(name))
    except KeyError as exc:
        raise ExtractionError("缺少 %s" % (name,)) from exc
    except ET.ParseError as exc:
        raise ExtractionError("%s 里的 XML 不合法: %s" % (name, exc)) from exc


def _extract_docx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as zf:
            root = _zip_xml(zf, "word/document.xml")
    except zipfile.BadZipFile as exc:
        raise ExtractionError("不是合法的 DOCX: %s" % (exc,)) from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc

    w = "{%s}" % NS_W
    lines: list = []
    for para in root.iter("%sp" % w):
        buf: list = []
        for node in para.iter():
            if node.tag == "%st" % w:
                buf.append(node.text or "")
            elif node.tag == "%stab" % w:
                buf.append("\t")
            elif node.tag in {"%sbr" % w, "%scr" % w}:
                buf.append("\n")
        lines.extend("".join(buf).split("\n"))
    if not any(line.strip() for line in lines):
        raise ExtractionError("DOCX 没有可提取的文本")
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# 标准库路径：.xlsx
# ---------------------------------------------------------------------------


def _extract_xlsx(path: str) -> str:
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    if size > MAX_XLSX_BYTES:
        raise ExtractionError(
            "XLSX 过大（%d 字节，上限 %d 字节）" % (size, MAX_XLSX_BYTES))
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            shared = _shared_strings(zf, names)
            sheets = _workbook_sheets(zf)
            rels = _workbook_rels(zf, names)
            out: list = []
            for name, state, rid in sheets:
                if state in {"hidden", "veryHidden"}:
                    continue
                part = _sheet_part(rels.get(rid, ""))
                if part not in names:
                    continue
                try:
                    rows = _sheet_rows(zf.read(part), shared)
                except ET.ParseError:
                    continue
                out.append("# ── Sheet: %s ──" % (name,))
                out.extend("\t".join(row) for row in rows)
                if not rows:
                    out.append("(empty)")
                out.append("")
    except zipfile.BadZipFile as exc:
        raise ExtractionError("不是合法的 XLSX: %s" % (exc,)) from exc
    except OSError as exc:
        raise ExtractionError(str(exc)) from exc
    if not out:
        raise ExtractionError("XLSX 没有可见的工作表内容")
    return "\n".join(out).rstrip("\n") + "\n"


def _shared_strings(zf: zipfile.ZipFile, names: set) -> list:
    if "xl/sharedStrings.xml" not in names:
        return []
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except ET.ParseError:
        return []
    s = "{%s}" % NS_S
    return ["".join(t.text or "" for t in item.iter("%st" % s))
            for item in root.iter("%ssi" % s)]


def _workbook_sheets(zf: zipfile.ZipFile) -> list:
    root = _zip_xml(zf, "xl/workbook.xml")
    s, r = "{%s}" % NS_S, "{%s}" % NS_REL
    return [
        (sheet.get("name", "Sheet"), sheet.get("state", "visible"),
         sheet.get("%sid" % r, ""))
        for sheet in root.iter("%ssheet" % s)
    ]


def _workbook_rels(zf: zipfile.ZipFile, names: set) -> dict:
    rels_path = "xl/_rels/workbook.xml.rels"
    if rels_path not in names:
        return {}
    try:
        root = ET.fromstring(zf.read(rels_path))
    except ET.ParseError:
        return {}
    rel_tag = "{%s}Relationship" % NS_PKG_REL
    return {rel.get("Id", ""): rel.get("Target", "")
            for rel in root.iter(rel_tag) if rel.get("Id")}


def _sheet_part(target: str) -> str:
    target = target.lstrip("/")
    return posixpath.normpath(target if target.startswith("xl/") else "xl/%s" % target)


def _col_index(ref: str) -> int:
    idx = 0
    for ch in ref:
        if not ch.isalpha():
            break
        idx = idx * 26 + ord(ch.upper()) - ord("A") + 1
    return max(idx - 1, 0)


def _sheet_rows(xml_bytes: bytes, shared: list) -> list:
    root = ET.fromstring(xml_bytes)
    s = "{%s}" % NS_S
    rows: list = []
    for row in root.iter("%srow" % s):
        if len(rows) >= _MAX_XLSX_ROWS_PER_SHEET:
            break
        cells: dict = {}
        max_col = -1
        for cell in row.iter("%sc" % s):
            col = _col_index(cell.get("r", "")) if cell.get("r") else max_col + 1
            if col >= _MAX_XLSX_COLS:
                continue
            cells[col] = _cell_value(cell, shared, s)
            max_col = max(max_col, col)
        rows.append([cells.get(i, "") for i in range(max_col + 1)]
                    if max_col >= 0 else [])
    while rows and not any(value.strip() for value in rows[-1]):
        rows.pop()
    return rows


def _cell_value(cell: ET.Element, shared: list, s: str) -> str:
    value = cell.findtext("%sv" % s) or ""
    typ = cell.get("t", "")
    if typ == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return ""
    if typ == "inlineStr":
        inline = cell.find("%sis" % s)
        return "" if inline is None else "".join(
            t.text or "" for t in inline.iter("%st" % s))
    if typ == "b":
        return "TRUE" if value.strip() in {"1", "true", "TRUE"} else "FALSE"
    if typ == "e":
        return value or "#ERROR"
    return value
