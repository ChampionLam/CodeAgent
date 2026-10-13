"""CodeAgent 程序更新（P0-2，t_db5df87b）。

设计依据 docs/design-agent-intelligence.md §3。只做 P0：
check / download / verify / 原子替换 + 回滚 / 离线包（本地 zip 或目录）。

职责边界：
  * 版本号、更新源、下载、校验、staged 切换、回滚 —— 全在这层
  * 不碰用户配置（config.json / .env）与数据目录（data/），只做「包里
    不能含这些路径」的黑名单校验
  * 不做 UI、不做断点续传（P1）、不做签名验签（P2，无证书）
  * 日志走 logging_kit（source="updater"），检查失败静默降级不抛

更新源形态（config.json update 段，缺省全有默认值）：
    "update": {
      "source": "https://example.com/updates/",   # URL 或本地目录
      "channel": "stable",
      "maxBytes": 524288000,
      "autoCheck": false, "autoDownload": false   # 这两个本层不消费，UI 层用
    }
source 也可以是本地目录路径（内网机器路径），latest.json 放该目录下。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logging_kit  # noqa: E402
import appconfig  # noqa: E402

DEFAULT_MAX_BYTES = 500 * 1024 * 1024  # 设计文档 §3.2：默认 500MB 上限
MIN_BYTES = 1

#: 包内出现这些路径前缀/文件名 → 拒绝安装（数据不变性硬规则，设计 §3.4）
PATH_BLACKLIST_EXACT = {"config.json", "config.mcp.json", "config.example.json",
                        ".env", "sessions.db"}
# v25 (t_27714fcb): "userdata/" added -- the Chromium profile dir the
# launcher creates via --user-data-dir=<InstallDir>\userdata belongs
# to the user exactly like data/ does.
PATH_BLACKLIST_PREFIX = ("data/", "skills/", "logs/", "updates/", "node_modules/",
                        "userdata/")

_UPDATES_DIRNAME = "updates"
_STAGED_DIRNAME = "staged-{version}"
_BACKUP_SUFFIX = ".bak-{ts}"
_RENAME_RETRIES = 3          # Windows 文件占用场景，未真机验证
_RENAME_RETRY_DELAY = 0.5

# t_6b66e727：原子替换前的 staged 硬闸门参数。
# 旧树统计排除这些目录（node_modules 从不进包、data/logs 是用户/运行产物），
# 只比较「程序本体」，避免正常升级被误杀。
_STAGED_EXCLUDE_DIRS = {"node_modules", ".git", "logs", "updates", "data", "skills"}

# v25 (t_27714fcb): directories under the INSTALL ROOT that belong to
# the user (live data, never shipped in a package -- the blacklist
# already enforces the "never in a package" half). When an update
# swaps the whole install tree, these are CARRIED OVER from the old
# tree into the new one after the atomic rename, before any delete.
# Sidecar data dir: <InstallDir>\data (logs/sessions.db/audit.db/
# memory/runs/attachments). Chromium profile: <InstallDir>\userdata
# (launcher writes --user-data-dir=%~dp0userdata). Both only when they
# sit INSIDE the repo root -- a DESK_AGENT_DATA_DIR / -DataDir pointing
# outside the tree was never in the swap path to begin with.
_USER_TREE_DIRS = ("data", "userdata")
_STAGED_MIN_RATIO = 0.25     # staged 文件数/体积低于旧树 1/4 → 结构可疑，拒绝


class UpdaterError(Exception):
    """更新失败。code 进 RPC 的 error 码，message 给用户看。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _log(level: str, msg: str, **kw: Any) -> None:
    logging_kit.get_logger().log(level, msg, source="updater", **kw)


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

def read_current_version() -> str:
    """当前版本：repo 根 package.json 的 version（构建信息源头）。"""
    root = _repo_root()
    path = os.path.join(root, "package.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        v = data.get("version")
        if isinstance(v, str) and v.strip():
            return v.strip()
    except (OSError, json.JSONDecodeError) as e:
        _log("warn", f"read package.json failed: {e}")
    return "0.0.0-unknown"


def _version_key(v: str) -> list[tuple[int, int, str]]:
    """semver-ish 比较键：数值段按数值、非数值段当 prerelease（比数值段小），
    段数补齐当 0。这样 1.0.0-beta < 1.0.0、1.2 == 1.2.0。"""
    parts = re.split(r"[.\-+_]", v.strip().lower())
    out: list[tuple[int, int, str]] = []
    for p in parts:
        if p.isdigit():
            out.append((0, int(p), ""))
        else:
            out.append((0, -1, p))
    return out


def version_cmp(a: str, b: str) -> int:
    la, lb = _version_key(a), _version_key(b)
    n = max(len(la), len(lb))
    pad = (0, 0, "")
    pa = la + [pad] * (n - len(la))     # 缺段当 0：1.2 == 1.2.0
    pb = lb + [pad] * (n - len(lb))
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


# ---------------------------------------------------------------------------
# 配置与源
# ---------------------------------------------------------------------------

def update_config() -> dict[str, Any]:
    """update 段（带默认值，旧 config 缺段也能跑；配置坏也降级为空段）。"""
    try:
        sec = appconfig.section("update", {}) or {}
    except Exception:                            # pragma: no cover - 配置坏
        sec = {}
    return sec if isinstance(sec, dict) else {}


def resolve_source() -> tuple[str, str]:
    """返回 (kind, target)。kind: "url" | "local" | "unconfigured"。

    source 可为 URL（http/https，国内可达地址）或本地目录（内网机器路径）。
    空串/未配置 → unconfigured（check 静默降级）。
    """
    src = str(update_config().get("source") or "").strip()
    if not src:
        return ("unconfigured", "")
    if re.match(r"^https?://", src, re.IGNORECASE):
        return ("url", src.rstrip("/") + "/")
    if os.path.isdir(os.path.expanduser(src)):
        return ("local", os.path.abspath(os.path.expanduser(src)))
    return ("bad", src)


def updates_dir() -> str:
    """下载与 staged 目录：安装目录的同级（staged 与安装目录分离，设计 §3.3）。

    repo 根即安装目录，updates 绝不能放进它里面——切换时整个根会被
    rename 走，staged 会跟着消失。放在父目录的 .codeagent-updates 下。
    """
    d = os.path.join(os.path.dirname(_repo_root()), ".codeagent-updates")
    os.makedirs(d, exist_ok=True)
    return d


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------

_PLATFORM_KEYS = ("win-x64", "linux-x64", "darwin-x64", "darwin-arm64")


def _platform_key() -> str:
    import platform
    machine = platform.machine().lower()
    system = platform.system().lower()
    if system == "windows":
        return "win-x64"
    if system == "darwin":
        return "darwin-arm64" if machine in ("arm64", "aarch64") else "darwin-x64"
    return "linux-x64"


def check() -> dict[str, Any]:
    """版本检查。**永不抛**：失败返回 {available: false, error: str} 并记 WARN。"""
    try:
        kind, target = resolve_source()
        current = read_current_version()
        if kind == "unconfigured":
            return {"available": False, "current": current,
                    "error": "update source not configured"}
        if kind == "bad":
            return {"available": False, "current": current,
                    "error": f"update source unreachable or not a dir: {target}"}

        manifest = _load_manifest(kind, target)
        latest = str(manifest.get("version") or "")
        if not latest:
            raise UpdaterError("BAD_MANIFEST", "latest.json missing version")
        entry = _platform_entry(manifest)
        if version_cmp(latest, current) <= 0:
            _log("info", f"update check: up to date ({current})")
            return {"available": False, "current": current, "latest": latest}
        result = {
            "available": True, "current": current, "latest": latest,
            "notes": manifest.get("notes") or "",
            "platform": _platform_key(),
            "file": entry,
        }
        _log("info", f"update available: {current} -> {latest}")
        return result
    except Exception as e:                       # noqa: BLE001 - 静默降级
        _log("warn", f"update check failed (silent): {e}")
        return {"available": False, "current": read_current_version(),
                "error": str(e)}


def _load_manifest(kind: str, target: str) -> dict[str, Any]:
    if kind == "url":
        import urllib.request
        url = target + "latest.json"
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
            raw = resp.read(2 * 1024 * 1024)
    else:
        with open(os.path.join(target, "latest.json"), "rb") as f:
            raw = f.read(2 * 1024 * 1024)
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise UpdaterError("BAD_MANIFEST", "latest.json not a JSON object")
    return data


def _platform_entry(manifest: dict[str, Any]) -> dict[str, Any]:
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise UpdaterError("BAD_MANIFEST", "latest.json missing files")
    key = _platform_key()
    entry = files.get(key) or files.get("all")
    if not isinstance(entry, dict) or not entry.get("url") and not entry.get("path"):
        raise UpdaterError("BAD_MANIFEST", f"no package for platform {key}")
    return entry


# ---------------------------------------------------------------------------
# 下载 + 三重校验（离线包走同一管道）
# ---------------------------------------------------------------------------

def _download(url_or_path: str, dest: str, max_bytes: int) -> int:
    if re.match(r"^https?://", url_or_path, re.IGNORECASE):
        import urllib.request
        with urllib.request.urlopen(url_or_path, timeout=60) as resp:  # noqa: S310
            with open(dest, "wb") as f:
                total = 0
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise UpdaterError("TOO_LARGE",
                                           f"download exceeds maxBytes={max_bytes}")
                    f.write(chunk)
            return total
    shutil.copyfile(url_or_path, dest)
    return os.path.getsize(dest)


def _sha256_file(path: str, max_bytes: int) -> str:
    h = hashlib.sha256()
    size = os.path.getsize(path)
    if size > max_bytes:
        raise UpdaterError("TOO_LARGE", f"package size {size} > maxBytes={max_bytes}")
    if size < MIN_BYTES:
        raise UpdaterError("TOO_SMALL", "package is empty")
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _verify_zip_package(zip_path: str, expect_version: str,
                        max_bytes: int, expected_sha: str | None) -> dict[str, Any]:
    """三重校验（设计 §3.2）：体积 → sha256 → 版本摘要一致性。

    expected_sha 为 None（离线包无 manifest）时跳过 sha256 并在 verified 里
    如实报告。签名：entry 带 signature 字段则必须能校验——本 P0 无证书体系，
    一律拒绝（校验不过拒绝安装，宁严勿松）。
    """
    verified: list[str] = []
    sha = _sha256_file(zip_path, max_bytes)
    verified.append("size")
    if expected_sha is not None:
        if sha != expected_sha.lower():
            raise UpdaterError("SHA_MISMATCH",
                               f"sha256 mismatch: expected {expected_sha}, got {sha}")
        verified.append("sha256")
    else:
        # 离线包无 manifest：没有任何完整性校验，如实标注，别让人误以为已校验
        verified.append("NO_INTEGRITY_CHECK")
        _log("warn", f"no sha256 in manifest for {zip_path}: "
                     f"integrity NOT verified (size check only)")
    entry_version = expect_version
    if entry_version and expected_sha is not None:
        # 在线包：版本必须出现在文件名或包内 package.json
        ok = entry_version in os.path.basename(zip_path)
        if not ok:
            pkg_v = _zip_package_version(zip_path)
            ok = pkg_v is not None and version_cmp(pkg_v, entry_version) == 0
        if not ok:
            raise UpdaterError("VERSION_MISMATCH",
                               f"manifest version {entry_version} not found in package")
        verified.append("version")
    _blacklist_check(zip_path)
    return {"sha256": sha, "sizeBytes": os.path.getsize(zip_path),
            "verified": verified}


def _zip_package_version(zip_path: str) -> str | None:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                if name.rstrip("/").endswith("package.json") and \
                        name.count("/") <= 1:
                    data = json.loads(zf.read(name).decode("utf-8"))
                    v = data.get("version")
                    if isinstance(v, str):
                        return v
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, KeyError):
        return None
    return None


def _blacklist_check(zip_path: str) -> None:
    """包内不允许含用户配置/数据（设计 §3.4 硬规则）。

    对压缩包内每条路径的**每一层**做黑名单匹配（config.json 无论出现在
    包根还是包内子目录都算用户数据，例如 dist/config.json）。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as e:
        raise UpdaterError("BAD_ZIP", f"not a valid zip: {e}") from e
    for n in names:
        parts = [p for p in n.replace("\\", "/").split("/") if p]
        if not parts:
            continue
        low_parts = [p.lower() for p in parts]
        for i, seg in enumerate(low_parts):
            if seg in PATH_BLACKLIST_EXACT:
                raise UpdaterError(
                    "WITH_USER_DATA",
                    f"update package must not contain user path: {n}")
            if i == 0 and any(seg + "/" == p or seg == p.rstrip("/")
                              for p in PATH_BLACKLIST_PREFIX):
                # 前缀黑名单只看顶层段（data/、skills/ 作为包的顶层目录）
                raise UpdaterError(
                    "WITH_USER_DATA",
                    f"update package must not contain user path: {n}")


# ---------------------------------------------------------------------------
# 原子替换 + 回滚
# ---------------------------------------------------------------------------

def _rename_with_retry(src: str, dst: str) -> None:
    last: OSError | None = None
    for _ in range(_RENAME_RETRIES):
        try:
            os.rename(src, dst)
            return
        except OSError as e:                     # Windows 占用场景
            last = e
            time.sleep(_RENAME_RETRY_DELAY)
    if last:
        raise UpdaterError("RENAME_FAIL", f"rename {src} -> {dst}: {last}")


def _copy_tree(src: str, dst: str) -> None:
    """跨卷退化的 copy+swap 路径（先全量 bak）。"""
    tmp = dst + ".copying"
    if os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src, tmp)
    _rename_with_retry(tmp, dst)


def apply_local(path: str) -> dict[str, Any]:
    """离线包（zip 或目录）→ 同一管道：临时目录完整校验 → staged → 原子替换。

    返回 {ok, applied, oldVersion, newVersion, backupPath, appliedFiles}。
    任何一步失败：自动回滚到旧版本（backup 切回原位）并抛 UpdaterError。
    用户配置与数据目录全程不动（黑名单已保证包里不含）。
    """
    root = _repo_root()
    old_version = read_current_version()
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(p):
        raise UpdaterError("NO_PACKAGE", f"package not found: {p}")

    workdir = os.path.join(updates_dir(), "work")
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)

    # 离线 zip 或目录 → 统一成一个 zip 走校验管道（单一校验逻辑）
    if os.path.isdir(p):
        zip_path = os.path.join(workdir, "package.zip")
        _zip_dir(p, zip_path)
        entry = {}
        # 旁边或目录内可能带 manifest
        side = p if os.path.isfile(os.path.join(p, "latest.json")) else os.path.dirname(p)
        man = os.path.join(side, "latest.json")
        if os.path.isfile(man):
            entry = _try_load_json(man) or {}
    else:
        zip_path = os.path.join(workdir, "package.zip")
        shutil.copyfile(p, zip_path)
        entry = _try_load_json(os.path.join(os.path.dirname(p), "latest.json")) or {}
        # zip 内自带 manifest 也认
        if not entry:
            entry = _zip_manifest(zip_path) or {}

    max_bytes = _max_bytes()
    expect_version = str(entry.get("version") or "") or None
    files = entry.get("files") if isinstance(entry.get("files"), dict) else {}
    e = files.get(_platform_key()) or files.get("all") or {}
    expected_sha = str(e.get("sha256") or "") or None
    if expected_sha is None and isinstance(entry.get("sha256"), str) \
            and entry["sha256"].strip():
        expected_sha = entry["sha256"].strip()
    if isinstance(e.get("signature"), str) and e["signature"].strip():
        raise UpdaterError("SIG_UNSUPPORTED",
                           "package is signed but this build cannot verify signatures")
    report = _verify_zip_package(zip_path, expect_version or "", max_bytes,
                                 expected_sha)
    _log("info", f"package verified: {report}")

    # staged：<updates>/staged-<ts>/（与安装目录分离，设计 §3.3）
    staged = os.path.join(updates_dir(), _STAGED_DIRNAME.format(
        version=expect_version or "local-" + str(int(time.time()))))
    shutil.rmtree(staged, ignore_errors=True)
    os.makedirs(staged, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staged)
    staged_root = _find_staged_root(staged, expect_version)

    # t_6b66e727 硬闸门：原子替换之前校验 staged 根「像个 app」。
    # 校验不过 → INVALID_PACKAGE，现场零改动（与 SHA_MISMATCH 同路径）。
    _validate_staged_root(staged_root, expect_version, root, zip_path)

    # 原子替换：install-dir → install-dir.bak-<ts>，staged → install-dir
    backup = root + _BACKUP_SUFFIX.format(ts=int(time.time()))
    if os.path.exists(backup):
        shutil.rmtree(backup, ignore_errors=True)
    try:
        try:
            _rename_with_retry(root, backup)
        except UpdaterError:
            # 跨卷 rename 不可行的退化路径：copy+swap
            _copy_tree(root, backup)
            shutil.rmtree(root)
        _rename_with_retry(staged_root, root)
        # 目标位置可能残留同级 staged_root 的父壳（staged-<v>/ 内层）
        _cleanup_staged_shell(staged, staged_root)
        # v25 (t_27714fcb): CARRY the user-owned dirs out of the old
        # tree (now at `backup`) back into the new install root. The
        # swap above moved the WHOLE old tree -- including the user's
        # live data/ and userdata/ -- into backup; without this pass
        # the user's sessions.db would only exist in the .bak-<ts>
        # copy. Move is atomic-ish per dir; on any failure we roll
        # the whole update back (the generic except below).
        _carry_user_tree_dirs(backup, root)
    except Exception as e:                       # noqa: BLE001 - 回滚兜底
        _log("error", f"apply failed, rolling back: {e}")
        _rollback(root, backup, staged)
        raise UpdaterError("APPLY_FAIL", f"update failed, rolled back: {e}") from e

    new_version = read_current_version()
    # t_6b66e727：自检从「版本变了」升级为「版本可解析且与包声明一致」。
    if new_version == "0.0.0-unknown":
        _log("error", f"self-check failed after swap: version unreadable "
                      f"({new_version}); rolling back")
        _rollback(root, backup, staged)
        raise UpdaterError(
            "APPLY_FAIL",
            f"self-check failed: new version unreadable ({new_version}), "
            f"rolled back (backup kept at {backup})")
    if expect_version and version_cmp(new_version, expect_version) != 0:
        _log("error", f"self-check failed after swap: version {new_version} "
                      f"!= package version {expect_version}; rolling back")
        _rollback(root, backup, staged)
        raise UpdaterError(
            "APPLY_FAIL",
            f"self-check failed: applied version {new_version} != package "
            f"version {expect_version}, rolled back (backup kept at {backup})")
    _log("info", f"update applied: {old_version} -> {new_version} "
                 f"(backup: {os.path.basename(backup)}); self-check OK")
    return {
        "ok": True, "applied": True,
        "oldVersion": old_version, "newVersion": new_version,
        "backupPath": backup,
        "verified": report.get("verified"),
        "appliedFiles": sorted(os.listdir(staged_root))[:200] if
        os.path.isdir(staged_root) else [],
    }


def _carry_user_tree_dirs(backup: str, root: str) -> list[str]:
    """v25 (t_27714fcb): move user-owned dirs from the just-swapped-out
    old tree (``backup``) back into the new install root (``root``).

    The atomic swap moved the ENTIRE old tree into ``backup`` -- the
    user's live data/ and userdata/ went with it. The package never
    contains those paths (PATH_BLACKLIST enforces it), so the new tree
    has no competing copies; a leftover (hand-made) copy in the package
    would have failed _blacklist_check long before the swap.

    Any failure RAISES (caller rolls the whole update back) -- a
    user dir that cannot be carried over must fail the update, not
    silently drop the user's sessions. Returns the carried dir names.
    """
    carried = []
    for name in _USER_TREE_DIRS:
        src = os.path.join(backup, name)
        dst = os.path.join(root, name)
        if not os.path.exists(src):
            continue
        if os.path.exists(dst):
            # Defensive: never overwrite. Log and leave it in the
            # backup (the user can still reach it there).
            _log("warn", f"carry-over skipped, destination exists: {dst} "
                         f"(old copy preserved in {backup})")
            continue
        _rename_with_retry(src, dst)
        carried.append(name)
        _log("info", f"carried user dir into new tree: {dst}")
    return carried


def _rollback(root: str, backup: str, staged: str) -> None:
    try:
        if os.path.exists(backup):
            if os.path.exists(root):
                # v25 (t_27714fcb): move any carried-over user dirs BACK
                # into the backup tree first, so the restore below
                # returns the user's data together with the old version.
                for name in _USER_TREE_DIRS:
                    carried = os.path.join(root, name)
                    back = os.path.join(backup, name)
                    if os.path.exists(carried) and not os.path.exists(back):
                        try:
                            _rename_with_retry(carried, back)
                        except UpdaterError:            # pragma: no cover
                            _log("error", "rollback could not restore "
                                           f"user dir {carried}; it is "
                                           f"still intact in the new tree")
                shutil.rmtree(root, ignore_errors=True)
            try:
                _rename_with_retry(backup, root)
            except UpdaterError:
                _copy_tree(backup, root)
                shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(staged, ignore_errors=True)
        _log("warn", "rollback complete: restored previous version")
    except Exception as e:                       # pragma: no cover - 回滚也炸
        _log("error", f"ROLLBACK FAILED: {e} (backup at {backup})")


def _find_staged_root(staged: str, expect_version: str | None) -> str:
    """zip 解出来可能有单层包目录（codeagent-0.2.0/…），取真正的内容根。"""
    entries = [e for e in os.listdir(staged)
               if not e.startswith(".") and e != "latest.json"]
    if len(entries) == 1:
        only = os.path.join(staged, entries[0])
        if os.path.isdir(only) and os.path.isfile(
                os.path.join(only, "package.json")):
            return only
    return staged


# ---------------------------------------------------------------------------
# t_6b66e727：staged 根硬闸门（原子替换之前）
# ---------------------------------------------------------------------------

def _tree_stats(root: str) -> tuple[int, int]:
    """数文件数与总字节数，排除运行时/用户目录（只统计程序本体）。"""
    files = 0
    total = 0
    for base, dirs, fns in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in _STAGED_EXCLUDE_DIRS and d != ".codeagent-updates"]
        for fn in fns:
            full = os.path.join(base, fn)
            files += 1
            try:
                total += os.path.getsize(full)
            except OSError:                    # pragma: no cover - 权限/竞态
                pass
    return files, total


def _validate_staged_root(staged_root: str, expect_version: str | None,
                          old_root: str, zip_path: str) -> None:
    """staged 必须长得像个 app，否则 INVALID_PACKAGE 且现场零改动。

    闸门（对应 t_6b66e727 协调者报告的空包/结构不符包问题）：
      1. 入口标记：staged 根有 package.json；或 electron/、python/、dist/
         已知入口中至少存在两个（老式无根 manifest 的包）。
      2. 版本可读：有 package.json 就必须能解析出非空 version；manifest
         声明了版本时必须一致（换到替换前拦截，早于旧实现的 swap 后自检）。
      3. 规模对比：文件数与字节数不得低于旧树程序本体的 _STAGED_MIN_RATIO，
         杜绝「整个安装目录被换成空/残包」。
    """
    has_pkg = os.path.isfile(os.path.join(staged_root, "package.json"))
    known_dirs = [d for d in ("electron", "python", "dist")
                  if os.path.isdir(os.path.join(staged_root, d))]
    if not has_pkg and len(known_dirs) < 2:
        raise UpdaterError(
            "INVALID_PACKAGE",
            f"staged root does not look like an app package "
            f"(no package.json; known dirs: {known_dirs}): {zip_path}")

    if has_pkg:
        pkg_version = _read_pkg_version(os.path.join(staged_root, "package.json"))
        if pkg_version is None:
            raise UpdaterError(
                "INVALID_PACKAGE",
                f"staged package.json has no readable version: {zip_path}")
        if expect_version and version_cmp(pkg_version, expect_version) != 0:
            raise UpdaterError(
                "INVALID_PACKAGE",
                f"staged version {pkg_version} != package version "
                f"{expect_version}: {zip_path}")

    old_files, old_bytes = _tree_stats(old_root)
    new_files, new_bytes = _tree_stats(staged_root)
    if new_files == 0 or new_bytes == 0:
        raise UpdaterError(
            "INVALID_PACKAGE", f"staged package contains no files: {zip_path}")
    if old_files > 0 and new_files < old_files * _STAGED_MIN_RATIO:
        raise UpdaterError(
            "INVALID_PACKAGE",
            f"staged file count {new_files} far below installed "
            f"{old_files} (<{int(_STAGED_MIN_RATIO * 100)}%): {zip_path}")
    if old_bytes > 0 and new_bytes < old_bytes * _STAGED_MIN_RATIO:
        raise UpdaterError(
            "INVALID_PACKAGE",
            f"staged package size {new_bytes}B far below installed "
            f"{old_bytes}B (<{int(_STAGED_MIN_RATIO * 100)}%): {zip_path}")


def _read_pkg_version(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f).get("version")
        return v.strip() if isinstance(v, str) and v.strip() else None
    except (OSError, json.JSONDecodeError):
        return None


def _cleanup_staged_shell(staged: str, staged_root: str) -> None:
    if os.path.abspath(staged) == os.path.abspath(staged_root):
        return
    if os.path.exists(staged):
        shutil.rmtree(staged, ignore_errors=True)


def _zip_dir(src_dir: str, dest_zip: str) -> None:
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, _dirs, files in os.walk(src_dir):
            for fn in files:
                full = os.path.join(base, fn)
                rel = os.path.relpath(full, src_dir)
                zf.write(full, rel)


def _try_load_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _zip_manifest(zip_path: str) -> dict[str, Any] | None:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if "latest.json" in zf.namelist():
                data = json.loads(zf.read("latest.json").decode("utf-8"))
                return data if isinstance(data, dict) else None
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, KeyError):
        return None
    return None


def _max_bytes() -> int:
    raw = update_config().get("maxBytes")
    if isinstance(raw, int) and raw > 0:
        return raw
    return DEFAULT_MAX_BYTES


# ---------------------------------------------------------------------------
# 在线下载（download → apply）
# ---------------------------------------------------------------------------

def download() -> dict[str, Any]:
    """check 后把选中平台的包下到 updates/download-<version>.zip 并校验。"""
    info = check()
    if not info.get("available"):
        return {"ok": False, "error": info.get("error") or "no update available"}
    files = info.get("file")
    entry = files if isinstance(files, dict) else {}
    src = entry.get("url") or entry.get("path")
    if not src:
        return {"ok": False, "error": "manifest entry has no url/path"}
    if isinstance(entry.get("signature"), str) and entry["signature"].strip():
        return {"ok": False, "code": "SIG_UNSUPPORTED",
                "error": "package is signed but this build cannot verify signatures"}
    kind, target = resolve_source()
    if not re.match(r"^https?://", str(src), re.IGNORECASE) and kind == "url":
        src = target + str(src)           # 相对路径：拼在源根下
    dest = os.path.join(updates_dir(), f"download-{info['latest']}.zip")
    try:
        size = _download(str(src), dest, _max_bytes())
        report = _verify_zip_package(dest, info["latest"], _max_bytes(),
                                     str(entry.get("sha256") or "") or None)
        _log("info", f"update downloaded: {dest} ({size} bytes)")
        return {"ok": True, "path": dest, "sizeBytes": size,
                "verified": report.get("verified")}
    except UpdaterError as e:
        if os.path.exists(dest):
            os.remove(dest)               # 校验不过：删包（设计 §3.2 硬失败）
        _log("warn", f"update download rejected: {e.message}")
        return {"ok": False, "error": e.message, "code": e.code}


def apply() -> dict[str, Any]:
    """应用已下载的包（download 的产物）。"""
    info = check()
    if not info.get("available"):
        return {"ok": False, "error": info.get("error") or "no update available"}
    dest = os.path.join(updates_dir(), f"download-{info['latest']}.zip")
    if not os.path.isfile(dest):
        return {"ok": False, "error": "package not downloaded yet (call update.download first)"}
    return apply_local(dest)


# ---------------------------------------------------------------------------
# RPC 前排：状态查询
# ---------------------------------------------------------------------------

def status() -> dict[str, Any]:
    kind, target = resolve_source()
    return {
        "currentVersion": read_current_version(),
        "source": target or "",
        "sourceType": kind,
        "channel": str(update_config().get("channel") or "stable"),
        "updatesDir": updates_dir(),
    }


# ---------------------------------------------------------------------------
# CLI（端到端验证入口）：python3 python/updater.py apply-local <path>
# ---------------------------------------------------------------------------

def _cli(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "apply-local":
        try:
            result = apply_local(argv[2])
        except UpdaterError as e:
            print(json.dumps({"ok": False, "code": e.code, "error": e.message},
                             ensure_ascii=False, indent=2))
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print("usage: python3 python/updater.py apply-local <zip-or-dir>")
    return 64


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
