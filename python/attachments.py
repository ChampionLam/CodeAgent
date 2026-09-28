"""附件存储 + 图片/文档表示（内容寻址）。

设计参考 dsh 的 attachment store：内容是地址，图片本身作为不可变对象存起来，
上层拿到的是一个引用（attach:<sha256>），而不是一堆裸字节或临时路径。

图片进入模型前要先过一道「表示」处理（normalize_image）：
字节上限硬挡、超目标的降采样、JPEG 走质量阶梯。规则在契约里定死，不在这里自由发挥。

文档（PDF/Office/ODF/RTF/EPUB 等）：存原始字节（内容寻址不变），另存一份
提取出的文本（extract_text 命名空间）供上层作为上下文注入。识别按内容
嗅探（PDF magic、ZIP 容器里的 word/document.xml 等），不信任扩展名。

本模块不做权限判断、不碰网络、不知道配置从哪来。
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
from dataclasses import dataclass

ATTACH_SCHEME = "attach:"

_MIME_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_FALLBACK_MIME = "application/octet-stream"

# ── 文档嗅探（按内容，不看扩展名）───────────────────────────────────────
_DOC_MIME_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\xd0\xcf\x11\xe0", "application/x-ole"),  # legacy .doc/.ppt/.xls
    (b"{\\rtf", "application/rtf"),
)
#: ZIP 容器里出现这些成员之一 → 对应的 OOXML / ODF MIME。
_DOC_MIME_BY_ZIP_MEMBER: tuple[tuple[str, str], ...] = (
    ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ("content.xml", "application/vnd.oasis.opendocument.text"),
)

# 降采样只在这三种格式上允许（PNG/GIF/WebP 无质量档，只能靠尺寸降）
_FORMATS_WITH_QUALITY = ("JPEG",)

#: 附件层文档字节上限（与提取层 MAX_DOCUMENT_BYTES 对齐）。
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


class AttachmentError(Exception):
    """附件层错误。code 是机器可读的，message 给人看。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AttachmentRef:
    sha256: str
    mime: str
    bytes_len: int
    width: int | None = None
    height: int | None = None
    original_name: str | None = None

    def uri(self) -> str:
        return ATTACH_SCHEME + self.sha256

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")

    @property
    def is_document(self) -> bool:
        """文档 = 有可提取文本的格式（提取层认得的那些）。"""
        import read_extract

        ext = os.path.splitext(self.original_name or "")[1].lower()
        return (self.mime in _DOC_MIME_BY_ZIP_MEMBER
                or self.mime in {m for _, m in _DOC_MIME_BY_MAGIC}
                or self.mime.startswith("text/")
                or ext in read_extract.EXTRACTABLE_EXTENSIONS
                or ext in read_extract.STDLIB_TEXT_EXTENSIONS)


@dataclass(frozen=True)
class ImagePolicy:
    max_input_bytes: int = 20 * 1024 * 1024
    resize_target_bytes: int = 5 * 1024 * 1024
    quality_steps: tuple[int, ...] = (85, 70, 50)
    max_dimension: int = 2048


def sniff_mime(data: bytes) -> str | None:
    """按内容嗅探 MIME，不信任扩展名。认不出返回 None。

    图片走原有 magic 表；文档（PDF/RTF/OLE + ZIP 容器的 OOXML/ODF）也
    按内容识别，拖进来的 PDF/Office 文件才能被认下来。
    """
    if not data:
        return None
    for magic, mime in _MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    # WEBP: RIFF????WEBP
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # 文档容器：PDF 头 / RTF 头 / OLE 复合文档头
    for magic, mime in _DOC_MIME_BY_MAGIC:
        if data.startswith(magic):
            return mime
    # ZIP 容器（docx/xlsx/pptx/odt/ods/odp/epub……）：看里面的关键成员。
    if data[:2] == b"PK":
        try:
            import zipfile

            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except Exception:
            return None
        for member, mime in _DOC_MIME_BY_ZIP_MEMBER:
            if member in names:
                return mime
    return None


def probe_image(data: bytes) -> tuple[int, int, str]:
    """返回 (宽, 高, 格式)。打不开或不是图片抛 UNSUPPORTED_IMAGE。"""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise AttachmentError("UNSUPPORTED_IMAGE", "PIL 不可用: %s" % exc) from exc
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            return int(im.width), int(im.height), fmt
    except Exception as exc:
        raise AttachmentError("UNSUPPORTED_IMAGE", "无法解析图片: %s" % exc) from exc



class AttachmentStore:
    """内容寻址的对象存储。目录布局：<root>/<sha 前两位>/<sha>。"""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(os.path.expanduser(root))

    # -- 路径 ---------------------------------------------------------------
    def path_for(self, ref: AttachmentRef) -> str:
        sha = ref.sha256
        return os.path.join(self.root, sha[:2], sha)

    def parse_uri(self, uri: str) -> AttachmentRef:
        if not isinstance(uri, str) or not uri.startswith(ATTACH_SCHEME):
            raise AttachmentError("NOT_FOUND", "不是附件引用: %r" % (uri,))
        sha = uri[len(ATTACH_SCHEME):].strip()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha.lower()):
            raise AttachmentError("NOT_FOUND", "附件引用格式不对: %r" % (uri,))
        sha = sha.lower()
        path = os.path.join(self.root, sha[:2], sha)
        if not os.path.exists(path):
            raise AttachmentError("NOT_FOUND", "附件不存在: %s" % uri)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            raise AttachmentError("IO_ERROR", "读附件失败: %s" % exc) from exc
        mime = sniff_mime(data) or _FALLBACK_MIME
        width = height = None
        if mime.startswith("image/"):
            try:
                width, height, _ = probe_image(data)
            except AttachmentError:
                width = height = None
        return AttachmentRef(sha256=sha, mime=mime, bytes_len=len(data),
                             width=width, height=height)

    # -- 写入 / 读取 ---------------------------------------------------------
    def save_bytes(self, data: bytes, *, mime: str | None = None,
                   original_name: str | None = None) -> AttachmentRef:
        if not isinstance(data, (bytes, bytearray)):
            raise AttachmentError("IO_ERROR", "save_bytes 只接受 bytes")
        data = bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        resolved_mime = mime or sniff_mime(data) or _FALLBACK_MIME
        width = height = None
        if resolved_mime.startswith("image/"):
            try:
                width, height, _ = probe_image(data)
            except AttachmentError:
                width = height = None
            else:
                if mime is None:
                    # 嗅探为准：调用方没指定时，用真实格式的 MIME
                    sniffed = sniff_mime(data)
                    if sniffed:
                        resolved_mime = sniffed

        path = os.path.join(self.root, sha[:2], sha)
        if not os.path.exists(path):
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp-%d" % os.getpid()
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except OSError as exc:
                raise AttachmentError("IO_ERROR", "写附件失败: %s" % exc) from exc
        return AttachmentRef(sha256=sha, mime=resolved_mime, bytes_len=len(data),
                             width=width, height=height, original_name=original_name)

    def save_file(self, path: str, *, original_name: str | None = None) -> AttachmentRef:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            raise AttachmentError("IO_ERROR", "读文件失败: %s" % exc) from exc
        return self.save_bytes(data, original_name=original_name or os.path.basename(path))

    def load_bytes(self, ref: AttachmentRef) -> bytes:
        try:
            with open(self.path_for(ref), "rb") as f:
                return f.read()
        except OSError as exc:
            raise AttachmentError("NOT_FOUND", "附件不存在: %s" % ref.uri()) from exc

    def exists(self, ref: AttachmentRef) -> bool:
        return os.path.exists(self.path_for(ref))

    # -- 文档提取（文本持久化，键 = <sha>.extracted.txt） -----------------

    def extracted_text_path(self, ref: AttachmentRef) -> str:
        """提取文本的持久化路径：<root>/<sha前两位>/<sha>.extracted.txt。"""
        sha = ref.sha256
        return os.path.join(self.root, sha[:2], sha + ".extracted.txt")

    def extract_document_text(self, ref: AttachmentRef) -> str:
        """提取文档附件的文本（带缓存：同一份内容只提取一次）。

        提取失败（缺依赖/加密/纯扫描件）抛 AttachmentError(EXTRACT_FAILED,
        message 带原因与安装提示）；上层决定是否仍接受附件（带提示文本）。
        """
        import read_extract

        data = self.load_bytes(ref)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise AttachmentError(
                "EXTRACT_FAILED",
                "文档 %d 字节超过上限 %d 字节" % (len(data), MAX_DOCUMENT_BYTES))
        cache_path = self.extracted_text_path(ref)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    return fh.read()
            except OSError:
                pass  # 缓存读不了就重提
        fake_name = ref.original_name or ("attach-%s.pdf" % ref.sha256[:8])
        try:
            text = read_extract.extract_document_bytes(data, fake_name)
        except read_extract.NeedsOcrExtraction as exc:
            raise AttachmentError(
                "EXTRACT_FAILED",
                "扫描件（无文本层）：%s —— 需要先 OCR。页面：%s"
                % (fake_name, ", ".join(str(p) for p in exc.pages) or "未知")) from exc
        except read_extract.ExtractionError as exc:
            raise AttachmentError("EXTRACT_FAILED", "%s" % (exc,)) from exc
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            tmp = cache_path + ".tmp-%d" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, cache_path)
        except OSError:
            pass  # 缓存写失败不致命：下次重提
        return text


def _empty_meta() -> dict:
    return {
        "is_image": False, "resized": False,
        "orig_bytes": 0, "final_bytes": 0,
        "orig_width": None, "orig_height": None,
        "final_width": None, "final_height": None,
        "quality": None, "format": None,
    }


def _make_meta(*, is_image: bool, resized: bool, orig_bytes: int, final_bytes: int,
               orig_w=None, orig_h=None, final_w=None, final_h=None,
               quality=None, fmt=None) -> dict:
    meta = _empty_meta()
    meta.update({
        "is_image": is_image, "resized": resized,
        "orig_bytes": orig_bytes, "final_bytes": final_bytes,
        "orig_width": orig_w, "orig_height": orig_h,
        "final_width": final_w, "final_height": final_h,
        "quality": quality, "format": fmt,
    })
    return meta


def _encode_jpeg(im, quality: int, max_dimension: int) -> bytes:
    """等比缩到最长边不超过 max_dimension，再按 quality 编码成 JPEG。"""
    scale = min(1.0, float(max_dimension) / float(max(im.width, im.height)))
    if scale < 1.0:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                       _resample())
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _encode_dimension_only(im, fmt: str, max_dimension: int) -> bytes:
    """PNG/GIF/WebP：不降质量档，只缩尺寸。"""
    scale = min(1.0, float(max_dimension) / float(max(im.width, im.height)))
    if scale < 1.0:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))),
                       _resample())
    buf = io.BytesIO()
    save_kwargs = {}
    pil_format = fmt
    if fmt == "GIF":
        im = im.convert("P", palette=1)
    elif fmt == "WEBP":
        im = im.convert("RGB")
        save_kwargs["quality"] = 90  # WebP 走有损但仍不分档，只为控制体积
    elif fmt == "PNG":
        im = im.convert("RGBA") if im.mode not in ("RGBA", "P", "L") else im
    im.save(buf, format=pil_format, **save_kwargs)
    return buf.getvalue()


def _resample():
    from PIL import Image
    return getattr(Image, "Resampling", Image).LANCZOS


def normalize_image(store: AttachmentStore, ref: AttachmentRef,
                    policy: ImagePolicy | None = None) -> tuple[AttachmentRef, dict]:
    """把附件整理成「可以安全送进模型」的表示。

    返回 (ref, meta)：ref 可能是新对象（降采样后的），meta 字段集合固定。
    非图片原样返回；超过硬顶直接抛 IMAGE_TOO_LARGE（不静默截断）。
    """
    policy = policy or ImagePolicy()
    data = store.load_bytes(ref)

    if not ref.mime.startswith("image/"):
        return ref, _make_meta(is_image=False, resized=False,
                               orig_bytes=len(data), final_bytes=len(data))

    n = len(data)
    if n > policy.max_input_bytes:
        raise AttachmentError(
            "IMAGE_TOO_LARGE",
            "图片 %d 字节超过硬顶 %d 字节" % (n, policy.max_input_bytes))

    try:
        width, height, fmt = probe_image(data)
    except AttachmentError:
        # mime 说是图片但解不开：按非图片处理，让上层自己决定
        return ref, _make_meta(is_image=False, resized=False,
                               orig_bytes=n, final_bytes=n)

    fits_bytes = n <= policy.resize_target_bytes
    fits_dim = max(width, height) <= policy.max_dimension
    if fits_bytes and fits_dim:
        return ref, _make_meta(is_image=True, resized=False, orig_bytes=n, final_bytes=n,
                               orig_w=width, orig_h=height, final_w=width, final_h=height,
                               fmt=fmt)

    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        if fmt in _FORMATS_WITH_QUALITY:
            new_data = None
            used_quality = policy.quality_steps[-1] if policy.quality_steps else None
            for q in policy.quality_steps:
                candidate = _encode_jpeg(im, q, policy.max_dimension)
                new_data = candidate
                used_quality = q
                if len(candidate) <= policy.resize_target_bytes:
                    break
        else:
            new_data = _encode_dimension_only(im, fmt, policy.max_dimension)
            used_quality = None

    if len(new_data) > policy.max_input_bytes:
        raise AttachmentError(
            "IMAGE_TOO_LARGE",
            "降采样后仍 %d 字节，超过硬顶 %d 字节" % (len(new_data), policy.max_input_bytes))

    new_mime = sniff_mime(new_data) or ref.mime
    new_ref = store.save_bytes(new_data, mime=new_mime, original_name=ref.original_name)
    new_w, new_h, _ = probe_image(new_data)
    meta = _make_meta(is_image=True, resized=True,
                      orig_bytes=n, final_bytes=len(new_data),
                      orig_w=width, orig_h=height, final_w=new_w, final_h=new_h,
                      quality=used_quality, fmt=fmt)
    return new_ref, meta


def normalize_attachment(store: AttachmentStore, ref: AttachmentRef,
                         policy: ImagePolicy | None = None) -> tuple:
    """图片走 normalize_image；文档提取文本并把 meta 标成文档。

    返回 (ref, meta)：meta 字段在图片 meta 基础上多一个 is_document /
    document_chars / document_note；图片路径行为与 normalize_image 完全
    一致（不回归）。文档提取失败不抛 —— 返回 meta 带原因，上层决定
    接受还是拒绝。
    """
    if not ref.is_document:
        return normalize_image(store, ref, policy)

    meta = _make_meta(is_image=False, resized=False,
                      orig_bytes=ref.bytes_len, final_bytes=ref.bytes_len)
    meta["is_document"] = True
    try:
        text = store.extract_document_text(ref)
        meta["document_chars"] = len(text)
        meta["document_text"] = text
    except AttachmentError as exc:
        meta["document_chars"] = 0
        meta["document_note"] = str(exc)
    return ref, meta


def document_context_text(store: AttachmentStore, refs: list,
                          *, max_chars: int = 120000) -> str:
    """把一批文档附件的提取文本拼成给模型的上下文块。

    超长截断（保头部）；没有可用文本的附件如实标注一句。返回空串 =
    没有任何文档内容可注入。
    """
    import read_extract

    parts: list = []
    for ref in refs:
        header = "【附件文档 %s】" % (ref.original_name or ref.uri())
        try:
            text = store.extract_document_text(ref)
        except AttachmentError as exc:
            parts.append("%s\n（文本提取失败：%s）" % (header, exc))
            continue
        if not text.strip():
            parts.append("%s\n（文档没有可提取的文本）" % (header,))
            continue
        parts.append("%s\n%s" % (header, text.rstrip()))
    joined = "\n\n".join(parts)
    if not joined.strip():
        return ""
    if len(joined) > max_chars:
        joined = joined[:max_chars] + ("\n…[已截断 %d 字符]"
                                       % (len(joined) - max_chars))
    return joined


def to_data_url(store: AttachmentStore, ref: AttachmentRef) -> str:
    data = store.load_bytes(ref)
    return "data:%s;base64,%s" % (ref.mime, base64.b64encode(data).decode("ascii"))