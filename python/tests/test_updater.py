"""updater 单测（P0-2，t_db5df87b）。

覆盖任务要求的三个关键路径：
  * 校验失败（伪造 sha256 / 体积 / 黑名单路径 / 签名）
  * 替换失败回滚
  * 离线包路径（本地目录 + 本地 zip，同一管道）
外加版本比较与 check 静默降级、RPC 分发（真 handle_request）。

apply_local 的对象是「repo 根」，测试里 monkeypatch updater._repo_root()
到临时目录，绝不动真实仓库。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "python"))

import logging_kit  # noqa: E402
import sidecar  # noqa: E402
import updater  # noqa: E402


def _zip_dir(src_dir: str, dest_zip: str) -> None:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, files in os.walk(src_dir):
            for fn in files:
                full = os.path.join(base, fn)
                zf.write(full, os.path.relpath(full, src_dir))


class UpdaterTestBase(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.mkdtemp(prefix="updater-test-")
        logging_kit.reset_for_test()
        logging_kit.configure(self.sandbox, level="info")

        # 假的「安装目录」（repo 根）
        self.root = os.path.join(self.sandbox, "install")
        os.makedirs(self.root)
        self._write_pkg(self.root, "0.1.0")

        # 配置目录：appconfig 从 config.json 读 update 段
        self.cfgdir = os.path.join(self.sandbox, "cfg")
        os.makedirs(self.cfgdir)
        self._orig_root = updater._repo_root
        updater._repo_root = lambda: self.root
        self._orig_app_root = sidecar.appconfig._ROOT
        sidecar.appconfig._ROOT = self.cfgdir

        # 更新包源目录
        self.src = os.path.join(self.sandbox, "src")
        os.makedirs(self.src)

    def tearDown(self):
        updater._repo_root = self._orig_root
        sidecar.appconfig._ROOT = self._orig_app_root
        logging_kit.reset_for_test()
        shutil.rmtree(self.sandbox, ignore_errors=True)

    # ---- helpers -------------------------------------------------------

    def _write_pkg(self, root: str, version: str) -> None:
        with open(os.path.join(root, "package.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "codeagent", "version": version}, f)
        with open(os.path.join(root, "main.py"), "w", encoding="utf-8") as f:
            f.write(f"# codeagent {version}\n")

    def _new_pkg_dir(self, version: str = "0.2.0") -> str:
        d = os.path.join(self.sandbox, "newpkg")
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
        inner = os.path.join(d, f"codeagent-{version}")
        os.makedirs(inner)
        self._write_pkg(inner, version)
        return d

    def _manifest(self, zip_path: str, version: str = "0.2.0",
                  sha256: str | None = None, **extra: object) -> str:
        if sha256 is None:
            with open(zip_path, "rb") as f:
                sha256 = hashlib.sha256(f.read()).hexdigest()
        man = {"version": version, "notes": "test",
               "files": {"all": {"url": zip_path, "size": os.path.getsize(zip_path),
                                 "sha256": sha256}}}
        man.update(extra)
        with open(os.path.join(self.src, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(man, f)
        return sha256

    def _set_config(self, update: dict) -> None:
        with open(os.path.join(self.cfgdir, "config.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"update": update}, f)

    def _make_zip(self, version: str = "0.2.0") -> str:
        d = self._new_pkg_dir(version)
        z = os.path.join(self.sandbox, f"codeagent-{version}.zip")
        _zip_dir(d, z)
        return z


class TestVersionCompare(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(updater.version_cmp("0.1.0", "0.2.0"), -1)
        self.assertEqual(updater.version_cmp("1.2.0", "1.2.0"), 0)
        self.assertEqual(updater.version_cmp("2.0.0", "1.9.9"), 1)
        self.assertEqual(updater.version_cmp("1.2", "1.2.0"), 0)
        self.assertEqual(updater.version_cmp("1.0.0-beta", "1.0.0"), -1)


class TestCheck(UpdaterTestBase):
    def test_unconfigured_silent(self):
        res = updater.check()
        self.assertFalse(res["available"])
        self.assertIn("error", res)
        self.assertEqual(res["current"], "0.1.0")

    def test_bad_source_silent(self):
        self._set_config({"source": "/nonexistent/dir/xyz"})
        res = updater.check()
        self.assertFalse(res["available"])
        self.assertIn("error", res)

    def test_local_source_available(self):
        z = self._make_zip()
        self._manifest(z)
        self._set_config({"source": self.src})
        res = updater.check()
        self.assertTrue(res["available"])
        self.assertEqual(res["latest"], "0.2.0")
        self.assertEqual(res["current"], "0.1.0")

    def test_local_source_up_to_date(self):
        z = self._make_zip()
        self._manifest(z)
        self._set_config({"source": self.src})
        self._write_pkg(self.root, "0.2.0")
        res = updater.check()
        self.assertFalse(res["available"])

    def test_status(self):
        self._set_config({"source": self.src, "channel": "stable"})
        res = updater.status()
        self.assertEqual(res["currentVersion"], "0.1.0")
        self.assertEqual(res["sourceType"], "local")
        self.assertEqual(res["channel"], "stable")


class TestVerification(UpdaterTestBase):
    def test_fake_sha256_rejected(self):
        z = self._make_zip()
        self._manifest(z, sha256="deadbeef" * 8)
        self._set_config({"source": self.src})
        res = updater.download()
        self.assertFalse(res["ok"])
        self.assertIn("SHA", res["code"])
        # 下载产物必须被删（设计 §3.2 硬失败删包）
        leftovers = [f for f in os.listdir(updater.updates_dir())
                     if f.startswith("download-")]
        self.assertEqual(leftovers, [])

    def test_too_small_rejected(self):
        z = self._make_zip()
        self._manifest(z)
        with open(z, "wb") as f:
            f.write(b"")
        self._set_config({"source": self.src})
        res = updater.download()
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "TOO_SMALL")

    def test_max_bytes(self):
        z = self._make_zip()
        self._manifest(z)
        self._set_config({"source": self.src, "maxBytes": 10})
        res = updater.download()
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "TOO_LARGE")

    def test_signature_rejected(self):
        z = self._make_zip()
        man_sha = hashlib.sha256(open(z, "rb").read()).hexdigest()
        man = {"version": "0.2.0",
               "files": {"all": {"url": z, "sha256": man_sha,
                                 "signature": "sig-by-someone"}}}
        with open(os.path.join(self.src, "latest.json"), "w") as f:
            json.dump(man, f)
        self._set_config({"source": self.src})
        res = updater.download()
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "SIG_UNSUPPORTED")

    def test_blacklist_user_data_rejected(self):
        # 包里带 config.json → 拒
        d = self._new_pkg_dir()
        with open(os.path.join(d, "codeagent-0.2.0", "config.json"), "w") as f:
            f.write("{}")
        z = os.path.join(self.sandbox, "evil.zip")
        _zip_dir(d, z)
        with self.assertRaises(updater.UpdaterError) as ctx:
            updater.apply_local(z)
        self.assertEqual(ctx.exception.code, "WITH_USER_DATA")
        # 安装目录必须还是旧版本
        self.assertEqual(updater.read_current_version(), "0.1.0")

    def test_version_consistency_rejected(self):
        # manifest 说 9.9.9，包里是 0.2.0，文件名也对不上。
        # manifest 与包放同一目录（applyLocal 的 manifest 解析位置）。
        z = os.path.join(self.sandbox, "pkg.zip")
        d = self._new_pkg_dir("0.2.0")
        _zip_dir(d, z)
        man = {"version": "9.9.9", "files": {"all": {
            "url": z, "sha256": hashlib.sha256(
                open(z, "rb").read()).hexdigest()}}}
        with open(os.path.join(self.sandbox, "latest.json"), "w") as f:
            json.dump(man, f)
        with self.assertRaises(updater.UpdaterError) as ctx:
            updater.apply_local(z)
        self.assertEqual(ctx.exception.code, "VERSION_MISMATCH")


class TestApplyLocal(UpdaterTestBase):
    def test_dir_package_end_to_end(self):
        """离线目录包：构造→apply→版本变化→数据目录零变化。

        真实部署里用户数据在 userData（安装目录之外），这里同构建模：
        data/ 放在安装目录的兄弟位置，apply 切换整个安装目录也不该碰它。
        """
        data_dir = os.path.join(os.path.dirname(self.root), "data")
        os.makedirs(data_dir)
        with open(os.path.join(data_dir, "marker.txt"), "w") as f:
            f.write("user data")
        before = os.listdir(data_dir)

        d = self._new_pkg_dir("0.2.0")
        res = updater.apply_local(d)
        self.assertTrue(res["applied"])
        self.assertEqual(res["oldVersion"], "0.1.0")
        self.assertEqual(res["newVersion"], "0.2.0")
        self.assertTrue(os.path.isdir(res["backupPath"]))
        # 数据目录零变化
        self.assertEqual(os.listdir(data_dir), before)
        self.assertEqual(open(os.path.join(data_dir, "marker.txt")).read(),
                         "user data")
        # 版本文件自证
        with open(os.path.join(self.root, "package.json")) as f:
            self.assertEqual(json.load(f)["version"], "0.2.0")

    def test_zip_package_end_to_end(self):
        z = self._make_zip("0.3.0")
        res = updater.apply_local(z)
        self.assertTrue(res["applied"])
        self.assertEqual(res["newVersion"], "0.3.0")

    def test_missing_package(self):
        with self.assertRaises(updater.UpdaterError) as ctx:
            updater.apply_local(os.path.join(self.sandbox, "nope.zip"))
        self.assertEqual(ctx.exception.code, "NO_PACKAGE")

    # ---- t_6b66e727：空包/结构不符包硬闸门（原子替换前，现场零改动） ----

    def _empty_zip(self, name: str = "empty.zip") -> str:
        z = os.path.join(self.sandbox, name)
        with zipfile.ZipFile(z, "w"):
            pass
        return z

    def test_empty_zip_rejected_no_touch(self):
        z = self._empty_zip()
        before = sorted(os.listdir(self.root))
        with self.assertRaises(updater.UpdaterError) as ctx:
            updater.apply_local(z)
        self.assertEqual(ctx.exception.code, "INVALID_PACKAGE")
        # 现场零改动：安装目录原样、版本不变
        self.assertEqual(updater.read_current_version(), "0.1.0")
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_unrelated_files_rejected(self):
        # 只含无关文件（非 app 结构）→ 拒
        d = os.path.join(self.sandbox, "junkpkg")
        os.makedirs(d)
        with open(os.path.join(d, "readme.txt"), "w") as f:
            f.write("junk")
        with self.assertRaises(updater.UpdaterError) as ctx:
            updater.apply_local(d)
        self.assertEqual(ctx.exception.code, "INVALID_PACKAGE")
        self.assertEqual(updater.read_current_version(), "0.1.0")

    def test_toplevel_multidir_mixed_rejected(self):
        # 顶层多个目录混杂且无根 package.json（不像 app）→ 拒
        d = os.path.join(self.sandbox, "mixedpkg")
        for sub in ("a", "b"):
            os.makedirs(os.path.join(d, sub))
            with open(os.path.join(d, sub, "f.txt"), "w") as f:
                f.write("x")
        with self.assertRaises(updater.UpdaterError) as ctx:
            updater.apply_local(d)
        self.assertEqual(ctx.exception.code, "INVALID_PACKAGE")
        self.assertEqual(updater.read_current_version(), "0.1.0")

    def test_version_dir_nested_package_still_works(self):
        """深一层嵌套（codeagent-x.y.z/ 内层）是支持路径，不能改坏。"""
        z = self._make_zip("0.6.0")   # zip 顶层即 codeagent-0.6.0/
        res = updater.apply_local(z)
        self.assertTrue(res["applied"])
        self.assertEqual(res["newVersion"], "0.6.0")

    def test_shrunk_package_rejected(self):
        """有 package.json 但文件数远小于旧树 → 结构可疑，拒。"""
        # 加厚旧树（默认 root 只有 2 个文件，比率闸门看不出残包）
        for i in range(20):
            with open(os.path.join(self.root, f"m{i}.py"), "w") as f:
                f.write(f"# module {i}\n" * 10)
        d = os.path.join(self.sandbox, "tiny")
        os.makedirs(d)
        with open(os.path.join(d, "package.json"), "w") as f:
            json.dump({"version": "0.2.0"}, f)
        with self.assertRaises(updater.UpdaterError) as ctx:
            updater.apply_local(d)
        self.assertEqual(ctx.exception.code, "INVALID_PACKAGE")
        self.assertEqual(updater.read_current_version(), "0.1.0")

    def test_pkg_json_unreadable_version_rejected(self):
        d = self._new_pkg_dir("0.2.0")
        with open(os.path.join(d, "codeagent-0.2.0", "package.json"), "w") as f:
            json.dump({"name": "codeagent"}, f)   # 无 version
        with self.assertRaises(updater.UpdaterError) as ctx:
            updater.apply_local(d)
        self.assertEqual(ctx.exception.code, "INVALID_PACKAGE")
        self.assertEqual(updater.read_current_version(), "0.1.0")

    def test_staged_version_vs_manifest_rejected(self):
        # manifest 声明 0.2.0、staged 实为 0.3.0（且 sha 是对包算的，
        # 会通过三重校验）→ 由替换前闸门拦截
        z = self._make_zip("0.3.0")
        man = {"version": "0.2.0", "files": {"all": {
            "url": z, "sha256": hashlib.sha256(open(z, "rb").read()).hexdigest()}}}
        with open(os.path.join(self.sandbox, "latest.json"), "w") as f:
            json.dump(man, f)
        with self.assertRaises(updater.UpdaterError) as ctx:
            updater.apply_local(z)
        self.assertIn(ctx.exception.code, ("INVALID_PACKAGE", "VERSION_MISMATCH"))
        self.assertEqual(updater.read_current_version(), "0.1.0")

    def test_selfcheck_unreadable_version_rolls_back(self):
        """闸门被绕过/新包版本不可读时：swap 后自检失败必须自动回滚。"""
        d = self._new_pkg_dir("0.2.0")
        # 构造一个能过闸门但 package.json version 解析失败的包：
        # 用一个会抛错的 read_current_version 模拟 swap 后读不到版本
        orig = updater.read_current_version
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] == 2:          # 第 2 次 = swap 后读新版本 → 失败
                return "0.0.0-unknown"
            return orig()
        updater.read_current_version = flaky
        try:
            with self.assertRaises(updater.UpdaterError) as ctx:
                updater.apply_local(d)
            self.assertEqual(ctx.exception.code, "APPLY_FAIL")
        finally:
            updater.read_current_version = orig
        # 回滚：旧版本恢复原位
        self.assertEqual(updater.read_current_version(), "0.1.0")
        self.assertEqual(open(os.path.join(self.root, "main.py")).read(),
                         "# codeagent 0.1.0\n")

    def test_replace_failure_rolls_back(self):
        """替换失败自动回滚：在 apply 中途把 staged→root 的 rename 砸掉。"""
        d = self._new_pkg_dir("0.5.0")
        orig = updater._rename_with_retry
        calls = {"n": 0}

        state = {"fired": False}

        def boom(src, dst):
            calls["n"] += 1
            # 第一次 staged→root 时模拟 Windows 文件占用；重试即视为锁已释放
            # （回滚路径的 rename 也走这里，不能再炸）。
            if calls["n"] == 2 and not state["fired"] \
                    and os.path.abspath(dst) == os.path.abspath(
                        updater._repo_root()):
                state["fired"] = True
                raise updater.UpdaterError("RENAME_FAIL", "simulated lock")
            return orig(src, dst)

        updater._rename_with_retry = boom
        try:
            with self.assertRaises(updater.UpdaterError) as ctx:
                updater.apply_local(d)
            self.assertEqual(ctx.exception.code, "APPLY_FAIL")
        finally:
            updater._rename_with_retry = orig
        # 回滚后：旧版本还在原位
        self.assertEqual(updater.read_current_version(), "0.1.0")


class TestRpcDispatch(UpdaterTestBase):
    """真 handle_request 路由（协议版本按 R3 要求带上）。"""

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        req = {"type": "req", "id": "test-1", "method": method,
               "params": {"protocolVersion": sidecar.SIDECAR_PROTOCOL_VERSION,
                          **(params or {})}}
        return sidecar.handle_request(req)

    def test_check_rpc_silent(self):
        res = self._rpc("update.check")
        self.assertTrue(res["ok"])
        body = res["result"]
        self.assertFalse(body["available"])
        self.assertIn("error", body)

    def test_status_rpc(self):
        self._set_config({"source": self.src})
        res = self._rpc("update.status")
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["currentVersion"], "0.1.0")

    def test_applyLocal_rpc(self):
        z = self._make_zip("0.4.0")
        res = self._rpc("update.applyLocal", {"path": z})
        self.assertTrue(res["ok"])
        self.assertEqual(res["result"]["newVersion"], "0.4.0")

    def test_applyLocal_reject_then_rollback_reported(self):
        d = self._new_pkg_dir()
        with open(os.path.join(d, "codeagent-0.2.0", ".env"), "w") as f:
            f.write("SECRET=1")
        z = os.path.join(self.sandbox, "evil2.zip")
        _zip_dir(d, z)
        res = self._rpc("update.applyLocal", {"path": z})
        self.assertFalse(res["ok"])
        self.assertIn("WITH_USER_DATA", res["error"]["code"])
        self.assertEqual(updater.read_current_version(), "0.1.0")

    def test_applyLocal_bad_param(self):
        res = self._rpc("update.applyLocal", {"path": ""})
        self.assertFalse(res["ok"])
        self.assertIn("BAD_REQUEST", res["error"]["code"])


if __name__ == "__main__":
    unittest.main()
