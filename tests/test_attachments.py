"""attachments.py 单测：内容寻址 + 图片嗅探 + 降采样归一 + data URL。"""
from __future__ import annotations

import base64
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

from PIL import Image  # noqa: E402

import attachments  # noqa: E402
from attachments import (  # noqa: E402
    AttachmentError, AttachmentStore, ImagePolicy, normalize_image, probe_image,
    sniff_mime, to_data_url,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def png_bytes(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def jpeg_bytes(width: int, height: int, quality: int = 95, noise: bool = False) -> bytes:
    im = Image.new("RGB", (width, height), (200, 60, 60))
    if noise:
        # 高频噪点 → JPEG 压不下去，用来逼出质量阶梯
        px = im.load()
        for y in range(height):
            for x in range(width):
                px[x, y] = ((x * 7 + y * 13) % 256, (x * 11) % 256, (y * 17) % 256)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = AttachmentStore(os.path.join(self._tmp.name, "objects"))

    def test_same_content_same_sha(self):
        a = self.store.save_bytes(b"hello world", mime="text/plain")
        b = self.store.save_bytes(b"hello world", mime="text/plain")
        self.assertEqual(a.sha256, b.sha256)
        self.assertEqual(a.uri(), b.uri())
        self.assertEqual(a.uri(), "attach:" + a.sha256)
        # 只落一份文件
        self.assertEqual(self.store.load_bytes(a), b"hello world")

    def test_different_content_different_sha(self):
        a = self.store.save_bytes(b"one")
        b = self.store.save_bytes(b"two")
        self.assertNotEqual(a.sha256, b.sha256)

    def test_save_file_records_original_name(self):
        path = os.path.join(self._tmp.name, "note.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("hi")
        ref = self.store.save_file(path)
        self.assertEqual(ref.original_name, "note.txt")
        self.assertTrue(self.store.exists(ref))
        self.assertEqual(ref.uri(), "attach:" + ref.sha256)
        self.assertEqual(self.store.load_bytes(ref), b"hi")

    def test_parse_uri_roundtrip(self):
        ref = self.store.save_bytes(png_bytes(8, 8), mime="image/png")
        again = self.store.parse_uri(ref.uri())
        self.assertEqual(again.sha256, ref.sha256)
        self.assertEqual(again.mime, "image/png")
        self.assertEqual(again.bytes_len, ref.bytes_len)

    def test_parse_uri_invalid(self):
        for bad in ["", "nope", "attach:", "attach:zz", "attach:" + "a" * 63,
                    "attach:" + "g" * 64]:
            with self.assertRaises(AttachmentError) as ctx:
                self.store.parse_uri(bad)
            self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_load_missing_raises_not_found(self):
        ghost = attachments.AttachmentRef(sha256="0" * 64, mime="image/png", bytes_len=1)
        with self.assertRaises(AttachmentError) as ctx:
            self.store.load_bytes(ghost)
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_save_bytes_rejects_non_bytes(self):
        with self.assertRaises(AttachmentError) as ctx:
            self.store.save_bytes("a string")  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "IO_ERROR")


class SniffTest(unittest.TestCase):
    def test_sniff_by_content_not_extension(self):
        self.assertEqual(sniff_mime(png_bytes(4, 4)), "image/png")
        self.assertEqual(sniff_mime(jpeg_bytes(4, 4)), "image/jpeg")
        self.assertEqual(sniff_mime(b"GIF89a...."), "image/gif")
        self.assertEqual(sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "), "image/webp")
        self.assertIsNone(sniff_mime(b"plain text"))
        self.assertIsNone(sniff_mime(b""))

    def test_probe_image(self):
        w, h, fmt = probe_image(png_bytes(12, 34))
        self.assertEqual((w, h), (12, 34))
        self.assertEqual(fmt, "PNG")
        w, h, fmt = probe_image(jpeg_bytes(20, 10))
        self.assertEqual((w, h), (20, 10))
        self.assertEqual(fmt, "JPEG")

    def test_probe_image_rejects_garbage(self):
        with self.assertRaises(AttachmentError) as ctx:
            probe_image(b"not an image at all")
        self.assertEqual(ctx.exception.code, "UNSUPPORTED_IMAGE")


class NormalizeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = AttachmentStore(os.path.join(self._tmp.name, "objects"))

    def test_non_image_passes_through_untouched(self):
        ref = self.store.save_bytes(b"just text", mime="text/plain")
        out_ref, meta = normalize_image(self.store, ref)
        self.assertEqual(out_ref.sha256, ref.sha256)
        self.assertFalse(meta["is_image"])
        self.assertFalse(meta["resized"])
        self.assertEqual(meta["final_bytes"], ref.bytes_len)
        self.assertEqual(set(meta.keys()), {
            "is_image", "resized", "orig_bytes", "final_bytes", "orig_width", "orig_height",
            "final_width", "final_height", "quality", "format"})

    def test_small_image_untouched(self):
        data = png_bytes(64, 64)
        ref = self.store.save_bytes(data, mime="image/png")
        out_ref, meta = normalize_image(self.store, ref)
        self.assertEqual(out_ref.sha256, ref.sha256)
        self.assertTrue(meta["is_image"])
        self.assertFalse(meta["resized"])
        self.assertEqual((meta["orig_width"], meta["orig_height"]), (64, 64))
        self.assertEqual(meta["orig_bytes"], meta["final_bytes"])
        self.assertIsNone(meta["quality"])

    def test_over_hard_cap_raises(self):
        data = png_bytes(200, 200)
        ref = self.store.save_bytes(data, mime="image/png")
        policy = ImagePolicy(max_input_bytes=len(data) - 1)
        with self.assertRaises(AttachmentError) as ctx:
            normalize_image(self.store, ref, policy)
        self.assertEqual(ctx.exception.code, "IMAGE_TOO_LARGE")
        # 硬顶拒绝时不许留下任何降采样产物
        self.assertEqual(len(os.listdir(os.path.join(self.store.root, ref.sha256[:2]))), 1)

    def test_large_jpeg_downscaled_within_limits(self):
        data = jpeg_bytes(600, 400, quality=95, noise=True)
        ref = self.store.save_bytes(data, mime="image/jpeg")
        policy = ImagePolicy(max_input_bytes=50 * 1024 * 1024,
                             resize_target_bytes=40 * 1024,
                             quality_steps=(85, 70, 50),
                             max_dimension=128)
        out_ref, meta = normalize_image(self.store, ref, policy)
        self.assertTrue(meta["resized"])
        self.assertNotEqual(out_ref.sha256, ref.sha256)          # 新附件
        self.assertTrue(self.store.exists(ref))                  # 原图保留
        self.assertLessEqual(max(meta["final_width"], meta["final_height"]), 128)
        self.assertLessEqual(meta["final_bytes"], policy.resize_target_bytes)
        self.assertIn(meta["quality"], policy.quality_steps)
        self.assertEqual(meta["orig_bytes"], len(data))

    def test_large_png_dimension_only_no_quality(self):
        data = png_bytes(600, 400)
        ref = self.store.save_bytes(data, mime="image/png")
        policy = ImagePolicy(max_input_bytes=50 * 1024 * 1024,
                             resize_target_bytes=1,          # 逼它必须动手
                             quality_steps=(85, 70, 50),
                             max_dimension=100)
        out_ref, meta = normalize_image(self.store, ref, policy)
        self.assertTrue(meta["resized"])
        self.assertIsNone(meta["quality"])                       # PNG 不降质量档
        self.assertEqual(meta["format"], "PNG")
        self.assertLessEqual(max(meta["final_width"], meta["final_height"]), 100)

    def test_oversize_after_resize_still_within_hard_cap_is_accepted(self):
        data = jpeg_bytes(400, 400, quality=95, noise=True)
        ref = self.store.save_bytes(data, mime="image/jpeg")
        policy = ImagePolicy(max_input_bytes=len(data) * 4,
                             resize_target_bytes=1,           # 永远达不到
                             quality_steps=(85, 70, 50),
                             max_dimension=64)
        out_ref, meta = normalize_image(self.store, ref, policy)
        self.assertTrue(meta["resized"])
        self.assertEqual(meta["quality"], 50)                    # 用尽阶梯后接受最后一档
        self.assertLessEqual(meta["final_bytes"], policy.max_input_bytes)

    def test_mime_sniffed_when_not_given(self):
        ref = self.store.save_bytes(png_bytes(8, 8))
        self.assertEqual(ref.mime, "image/png")


class DataUrlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = AttachmentStore(os.path.join(self._tmp.name, "objects"))

    def test_to_data_url_prefix_and_payload(self):
        raw = png_bytes(8, 8)
        ref = self.store.save_bytes(raw, mime="image/png")
        url = to_data_url(self.store, ref)
        self.assertTrue(url.startswith("data:image/png;base64,"))
        payload = url.split(",", 1)[1]
        self.assertEqual(base64.b64decode(payload), raw)

    def test_data_url_for_jpeg(self):
        raw = jpeg_bytes(8, 8)
        ref = self.store.save_bytes(raw, mime="image/jpeg")
        self.assertTrue(to_data_url(self.store, ref).startswith("data:image/jpeg;base64,"))


if __name__ == "__main__":
    unittest.main()