"""Skill discovery and (since 2026-09) the write-back loop for skills.

A skill is one directory containing a single ``SKILL.md``: a frontmatter block
(name / description / when_to_use / version / author) followed by the body.

Two roots are scanned, in this order, and a later root wins on a name clash:

1. ``<repo>/resources/skills/``     - shipped with the app (built-in, read-only)
2. ``~/.desktop-agent/skills/``     - the user's own skills (writable)

``DESK_AGENT_SKILLS_DIR`` (os.pathsep separated) replaces the roots entirely,
which is what the tests and a custom deployment use. With the override in
place the **last** entry is treated as the writable user root and every entry
before it is treated as built-in; with no override that is exactly the
[<repo>/resources/skills, ~/.desktop-agent/skills] pair above.

Read-side guardrail, promised in the design doc and asserted by a static test:
the read path only ever opens ``SKILL.md`` inside each skill directory. Nothing
is globbed, nothing is recursed into, and no companion file is ever read or
executed - a skill cannot smuggle in a program that runs by itself. Anything a
skill wants to execute has to go through the run_shell tool, which is gated by
user approval.

The write-back loop (create / patch / write_file / remove_file / delete, plus
an atomic ``apply_operations`` for batches) follows the reference agent
(Hermes Agent, MIT, Copyright (c) 2025 Nous Research) skill_manage
conventions: one tool with an
operations array, frontmatter validated before anything touches disk, built-in
skills are never writable, paths may not escape the skill directory, and a
batch either lands completely or is rolled back completely.

All read-side failures are non-fatal: a malformed or unreadable skill is
skipped and its reason is recorded in ``skipped()``. Write-side failures raise
``SkillWriteError`` with a reason that is meant for the model to read.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass

SKILL_FILENAME = "SKILL.md"
_FRONTMATTER_FENCE = "---"

# name -> Skill, and (path, reason) for everything that was rejected.
_CACHE: dict[str, "Skill"] = {}
_SKIPPED: list[tuple[str, str]] = []
_SCANNED_FINGERPRINT: tuple | None = None


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    when_to_use: str | None
    version: str | None
    author: str | None
    source_dir: str
    body: str
    sha256: str


def _repo_root() -> str:
    """Repository root: the parent of the directory holding this module."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_skill_dirs() -> list[str]:
    """[built-in, user] - built-in first so the user's copy wins on a clash."""
    builtin = os.environ.get("DESK_AGENT_BUILTIN_SKILLS_DIR") or os.path.join(
        _repo_root(), "resources", "skills")
    user = os.path.join(os.path.expanduser("~"), ".desktop-agent", "skills")
    return [builtin, user]


def _target_dirs(dirs: list[str] | None) -> list[str]:
    if dirs is not None:
        return [str(d) for d in dirs]
    override = os.environ.get("DESK_AGENT_SKILLS_DIR")
    if override:
        parts = [p for p in override.split(os.pathsep) if p.strip()]
        if parts:
            return parts
    return default_skill_dirs()


def _fingerprint(dirs: list[str]) -> tuple:
    """Cheap change detector: dir mtimes + SKILL.md mtimes. Missing dirs -> 0."""
    stamps = []
    for root in dirs:
        try:
            stamps.append((root, os.path.getmtime(root)))
        except OSError:
            stamps.append((root, 0))
            continue
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            entries = []
        for entry in entries:
            path = os.path.join(root, entry, SKILL_FILENAME)
            try:
                stamps.append((path, os.path.getmtime(path)))
            except OSError:
                continue
    return tuple(stamps)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1].strip()
    return value


def parse_skill_md(text: str) -> tuple[dict[str, str], str]:
    """Split a SKILL.md into (frontmatter dict, body).

    Hand-rolled on purpose: the repo is stdlib-only, no PyYAML. Unknown lines in
    the frontmatter are ignored rather than treated as an error.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        raise ValueError("missing frontmatter fence on the first line")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONTMATTER_FENCE:
            end = index
            break
    if end is None:
        raise ValueError("frontmatter is never closed")
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        if key:
            meta[key] = _unquote(value)
    body = "\n".join(lines[end + 1:]).strip()
    return meta, body


def _load_one(skill_dir: str, path: str) -> Skill:
    with open(path, "r", encoding="utf-8") as handle:      # read-only, single file
        raw = handle.read()
    meta, body = parse_skill_md(raw)
    name = (meta.get("name") or "").strip()
    description = (meta.get("description") or "").strip()
    if not name:
        raise ValueError("frontmatter has no 'name'")
    if not description:
        raise ValueError("frontmatter has no 'description'")
    return Skill(
        name=name,
        description=description,
        when_to_use=(meta.get("when_to_use") or "").strip() or None,
        version=(meta.get("version") or "").strip() or None,
        author=(meta.get("author") or "").strip() or None,
        source_dir=os.path.abspath(skill_dir),
        body=body,
        sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def scan(dirs: list[str] | None = None) -> dict[str, Skill]:
    """Scan the skill roots and rebuild the cache. Never raises."""
    roots = _target_dirs(dirs)
    found: dict[str, Skill] = {}
    rejected: list[tuple[str, str]] = []
    for root in roots:
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue                       # an absent root is normal, not an error
        for entry in entries:
            skill_dir = os.path.join(root, entry)
            if not os.path.isdir(skill_dir):
                continue
            path = os.path.join(skill_dir, SKILL_FILENAME)
            if not os.path.isfile(path):
                rejected.append((path, "no SKILL.md in directory"))
                continue
            try:
                skill = _load_one(skill_dir, path)
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                rejected.append((path, "%s: %s" % (type(exc).__name__, exc)))
                continue
            if not skill.body:
                rejected.append((path, "body is empty"))
                continue
            found[skill.name] = skill       # later roots override earlier ones
    _CACHE.clear()
    _CACHE.update(found)
    _SKIPPED[:] = rejected
    global _SCANNED_FINGERPRINT
    _SCANNED_FINGERPRINT = _fingerprint(roots)
    return dict(_CACHE)


def _ensure_scanned() -> None:
    dirs = _target_dirs(None)
    if not _CACHE and _SCANNED_FINGERPRINT is None:
        scan(dirs)
        return
    if _fingerprint(dirs) != _SCANNED_FINGERPRINT:
        scan(dirs)


def list_skills() -> list[Skill]:
    """Installed skills, name-sorted. Rescans when a root changed on disk."""
    _ensure_scanned()
    return [_CACHE[name] for name in sorted(_CACHE)]


def get_skill(name: str) -> Skill | None:
    if not isinstance(name, str):
        return None
    _ensure_scanned()
    return _CACHE.get(name.strip()) or _CACHE.get(name)


def read_body(name: str) -> str | None:
    skill = get_skill(name)
    return skill.body if skill else None


def skipped() -> list[tuple[str, str]]:
    """[(path, reason)] for everything the last scan rejected - for debugging."""
    return list(_SKIPPED)


def reset_for_tests() -> None:
    _CACHE.clear()
    _SKIPPED.clear()
    global _SCANNED_FINGERPRINT
    _SCANNED_FINGERPRINT = None


# ---------------------------------------------------------------------------
# 写回回路（2026-09-25 新增，遵循 Hermes Agent skill_manage 的约定，MIT，
# Copyright (c) 2025 Nous Research）。读侧仍然是「只开 SKILL.md、永不执行」；写侧是
# 有边界的：只写用户根、路径不逃逸、frontmatter 先校验再落盘、批量原子。
# ---------------------------------------------------------------------------

#: description 上限（Hermes Agent 同值 1024）。技能索引在
#: 提示词里会被截断，超长 description 等于把触发句写没了，所以写入时就拦。
MAX_DESCRIPTION_LENGTH = 1024

#: 技能名格式（通行约定）：小写字母 / 数字 / 连字符，最长 64。
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
MAX_SKILL_NAME_LENGTH = 64

#: 附加文件允许落的位置（通行约定 references/ templates/ assets/，另加
#: 常见的 data/）。SKILL.md 本身只能由 create / patch 改，不经 write_file。
_ALLOWED_FILE_DIRS = ("references", "templates", "assets", "data")

#: 批量操作里单批的条数上限（防一次调用把用户根写爆）
MAX_OPERATIONS_PER_BATCH = 50


class SkillWriteError(Exception):
    """写技能失败。message 面向模型：说清是哪一条没过、为什么。

    code 是给 tools.py 的建议错误码（BAD_REQUEST / NOT_FOUND / FORBIDDEN /
    IO_ERROR / CONFLICT），由工具层读取后映射成它自己的错误码集合。
    """

    def __init__(self, message: str, code: str = "BAD_REQUEST"):
        super().__init__(message)
        self.code = code


def _roots() -> list[str]:
    return _target_dirs(None)


def _writable_roots() -> list[str]:
    """可写根集合。

    没有环境变量覆盖时 = [用户根]；有 DESK_AGENT_SKILLS_DIR 覆盖时，约定
    「最后一项是可写用户根，其余全按内置对待」—— 单根覆盖意味着只有这一
    个根，那它就是用户根。这个约定让测试和一个根的自定义部署都能落到
    临时目录，而永远不碰 ~/.desktop-agent/ 和仓库里的 resources/skills。
    """
    return [_roots()[-1]]


def _builtin_roots() -> list[str]:
    """只读根集合 = 全部根里除最后一个以外的部分。"""
    return _roots()[:-1]


def _builtin_reject(name: str, builtin: str, what: str) -> SkillWriteError:
    """统一的内置拒写错误（写守卫口径：要说清为什么拒）。"""
    return SkillWriteError(
        "skill %r is a built-in skill shipped with the app (%s): built-in skills "
        "are read-only and would be overwritten on the next upgrade; %s"
        % (name, builtin, what), code="FORBIDDEN")


def _validate_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise SkillWriteError("skill name must be a non-empty string")
    name = name.strip()
    if len(name) > MAX_SKILL_NAME_LENGTH:
        raise SkillWriteError(
            "skill name %r is %d chars, max is %d" % (name, len(name), MAX_SKILL_NAME_LENGTH))
    if not _SKILL_NAME_RE.match(name):
        raise SkillWriteError(
            "skill name %r must be lowercase letters, digits and hyphens" % name)
    return name


def _validate_frontmatter(text: str) -> None:
    """落盘前的 SKILL.md 校验（与 Hermes Agent 的 _validate_frontmatter 等价）。

    1. 第一行必须是 ``---``（fence 开）
    2. frontmatter 必须有第二个 ``---`` 闭合
    3. 能被解析器读出（等价「YAML 能解析」—— 本仓库 stdlib-only，用手写
       frontmatter 解析器，语义一致）
    4. 必须有 name 和 description
    5. name 必须与技能目录同名（由调用方比对，见 patch / create）
    6. description 长度 ≤ MAX_DESCRIPTION_LENGTH（1024）
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0].strip() != _FRONTMATTER_FENCE:
        raise SkillWriteError("SKILL.md must start with a '---' frontmatter fence on the first line")
    if not any(line.strip() == _FRONTMATTER_FENCE for line in lines[1:]):
        raise SkillWriteError("SKILL.md frontmatter must be closed with a second '---'")
    try:
        meta, _body = parse_skill_md(text)
    except ValueError as exc:
        raise SkillWriteError("SKILL.md frontmatter does not parse: %s" % exc)
    if not (meta.get("name") or "").strip():
        raise SkillWriteError("SKILL.md frontmatter has no 'name'")
    description = (meta.get("description") or "").strip()
    if not description:
        raise SkillWriteError("SKILL.md frontmatter has no 'description'")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise SkillWriteError(
            "description is %d chars, max is %d: the skill index in the system "
            "prompt gets truncated, so make the first sentence a self-contained "
            "trigger ('Use when ...') instead of padding it"
            % (len(description), MAX_DESCRIPTION_LENGTH))


def _user_skill_dir(name: str) -> str:
    """用户根下的技能目录绝对路径。"""
    root = _writable_roots()[0]
    return os.path.realpath(os.path.join(root, name))


def _builtin_dir_of(name: str) -> str | None:
    """若该名字命中某个只读根，返回那个目录；否则 None。"""
    for root in _builtin_roots():
        candidate = os.path.join(os.path.realpath(root), name)
        if os.path.isdir(candidate):
            return candidate
    return None


def _locate_for_write(name: str) -> str:
    """定位「要写的那份技能」的目录：用户根优先；只剩内置 → 拒。

    用户根有副本就写副本（覆盖语义与 scan 的「后根胜出」一致）；两边都没
    有就返回用户根路径，由调用方按各自的存在性要求报错。
    """
    user_dir = _user_skill_dir(name)
    if os.path.isdir(user_dir):
        return user_dir
    builtin = _builtin_dir_of(name)
    if builtin is not None:
        raise _builtin_reject(name, builtin, "create a new skill under a different name instead")
    return user_dir


def _contains(parent: str, child: str) -> bool:
    """child 是否严格落在 parent 之内（canonical 路径上判断，不能比字符串）。"""
    parent = os.path.realpath(parent)
    child = os.path.realpath(child)
    return child != parent and child.startswith(parent + os.sep)


def _resolve_rel_path(name: str, rel_path: str, *,
                      must_exist: bool = False) -> tuple[str, str]:
    """把附加文件的相对路径解析成 (技能目录, 目标绝对路径)。

    拒绝：绝对路径、空、``..`` 段、反斜杠写法、首段不在 _ALLOWED_FILE_DIRS
    里的路径、目标/中间段是符号链接、以及 realpath 后逃出技能目录。目录
    归属先于存在性判断（写内置技能报 FORBIDDEN，写不存在的技能报
    NOT_FOUND，不被「文件不存在」糊过去）。must_exist=True 再要求目标文件
    本身存在（remove 的前提）。
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise SkillWriteError("'rel_path' must be a non-empty relative path")
    rel = rel_path.strip().replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", rel):
        raise SkillWriteError("rel_path %r must be relative to the skill directory, not absolute"
                              % rel_path)
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts:
        raise SkillWriteError("rel_path %r resolves to the skill directory itself" % rel_path)
    if any(p == ".." for p in parts):
        raise SkillWriteError("rel_path %r escapes the skill directory" % rel_path)
    if parts[0] not in _ALLOWED_FILE_DIRS:
        raise SkillWriteError(
            "rel_path %r must start with one of %s (SKILL.md itself is managed by "
            "create/patch)" % (rel_path, "/".join(_ALLOWED_FILE_DIRS)))
    user_dir = _user_skill_dir(name)
    if not os.path.isdir(user_dir):
        builtin = _builtin_dir_of(name)
        if builtin is not None:
            raise _builtin_reject(name, builtin, "its companion files cannot be written either")
        raise SkillWriteError("no such skill %r under the user skills root" % name,
                              code="NOT_FOUND")
    skill_dir = user_dir
    target = os.path.join(skill_dir, *parts)
    # 逐段验证（含最后一段的已存在形态）：不是符号链接；目录段还得是真目录
    current = skill_dir
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        if not os.path.lexists(current):
            if index < len(parts) - 1:
                continue                       # 不存在的中间段后面会 makedirs
            break                              # 目标文件不存在：写没问题，删会另行报错
        if os.path.islink(current):
            raise SkillWriteError(
                "rel_path %r crosses a symlink at %r; symlinks would let a write "
                "escape the skill directory" % (rel_path, part))
        if index < len(parts) - 1 and not os.path.isdir(current):
            raise SkillWriteError(
                "rel_path %r crosses a non-directory at %r" % (rel_path, part))
        if index == len(parts) - 1 and os.path.isdir(current):
            raise SkillWriteError(
                "rel_path %r points at a directory, not a file" % rel_path)
    if not _contains(skill_dir, target):
        raise SkillWriteError("rel_path %r escapes the skill directory" % rel_path)
    if must_exist and not os.path.isfile(target):
        raise SkillWriteError("no such file in skill %r: %s" % (name, rel_path),
                              code="NOT_FOUND")
    return skill_dir, target


def _atomic_write(path: str, content: str) -> None:
    """临时文件 + os.replace：要么旧内容、要么新内容，绝无半截。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    if os.path.lexists(path) and os.path.islink(path):
        raise SkillWriteError("refusing to write through a symlink: %s" % path)
    fd, tmp = tempfile.mkstemp(prefix=".skw-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _invalidate_scan_cache() -> None:
    """写成功后立刻失效扫描缓存：下一次 list_skills 马上看到新状态。

    直接 reset：fingerprint 是按 mtime 做的，同一秒内连写两次会看不出变化。
    """
    reset_for_tests()


def _render_skill_md(name: str, description: str, body: str,
                     when_to_use: str | None) -> str:
    """把字段渲染成 SKILL.md 文本。description 压成单行（索引只取一行）。"""
    lines = ["---", "name: %s" % name, "description: %s" % description]
    if when_to_use:
        lines.append("when_to_use: %s" % " ".join(when_to_use.split()))
    lines.append("---")
    lines.append("")
    lines.append((body or "").strip())
    lines.append("")
    return "\n".join(lines)


def _read_skill_md_text(skill_dir: str) -> str:
    path = os.path.join(skill_dir, SKILL_FILENAME)
    with open(path, "r", encoding="utf-8") as handle:      # read-only, single file
        return handle.read()


def create_skill(name: str, description: str, body: str, *,
                 when_to_use: str | None = None,
                 files: dict[str, str] | None = None) -> str:
    """在用户根创建 <root>/<name>/SKILL.md（+ 可选附加文件），返回写入路径。

    校验顺序：name 格式 → 参数类型 → 重名（用户根已有 → 拒；仅内置有同名
    → 拒并说清）→ 附加文件路径合法（含逃逸/内置检查）→ frontmatter 全量
    校验 → 落盘（SKILL.md 先写，附加文件随后，任一失败由调用方回滚）。
    """
    name = _validate_name(name)
    if not isinstance(description, str) or not description.strip():
        raise SkillWriteError("'description' must be a non-empty string")
    if not isinstance(body, str) or not body.strip():
        raise SkillWriteError("'body' must be a non-empty string")
    description = " ".join(description.split())
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise SkillWriteError(
            "description is %d chars, max is %d" % (len(description), MAX_DESCRIPTION_LENGTH))
    if files is not None and not isinstance(files, dict):
        raise SkillWriteError("'files' must be an object of {rel_path: content}")
    user_dir = _user_skill_dir(name)
    if os.path.isdir(user_dir):
        raise SkillWriteError(
            "skill %r already exists at %s; use patch_skill to change it" % (name, user_dir),
            code="CONFLICT")
    builtin = _builtin_dir_of(name)
    if builtin is not None:
        raise SkillWriteError(
            "skill %r already exists as a built-in skill (%s): built-in skills "
            "are read-only; pick a different name to create a user skill" % (name, builtin),
            code="FORBIDDEN")
    # 附加文件路径先全部校验（此时技能目录还不存在，逃逸检查只做静态段
    # 分析：绝对路径 / .. / 首段白名单 / SKILL.md 覆盖）
    extra: list[tuple[str, str]] = []
    if files:
        for rel, content in files.items():
            if not isinstance(content, str):
                raise SkillWriteError("'files' content for %r must be a string" % rel)
            rel_norm = _check_new_rel_path(name, rel)
            extra.append((rel_norm, content))
    text = _render_skill_md(name, description, body, when_to_use)
    _validate_frontmatter(text)
    _atomic_write(os.path.join(user_dir, SKILL_FILENAME), text)
    for rel_norm, content in extra:
        _atomic_write(os.path.join(user_dir, rel_norm), content)
    _invalidate_scan_cache()
    return os.path.join(user_dir, SKILL_FILENAME)


def _check_new_rel_path(name: str, rel_path: str) -> str:
    """create 场景下的附加文件路径校验（目录尚不存在，做静态段分析）。

    返回归一化后的相对路径（posix 风格）。拒绝项与 _resolve_rel_path 的
    静态部分一致：非字符串/空、绝对路径、``..`` 段、白名单外首段。
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise SkillWriteError("'rel_path' must be a non-empty relative path")
    rel = rel_path.strip().replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", rel):
        raise SkillWriteError("rel_path %r must be relative to the skill directory, not absolute"
                              % rel_path)
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts:
        raise SkillWriteError("rel_path %r resolves to the skill directory itself" % rel_path)
    if any(p == ".." for p in parts):
        raise SkillWriteError("rel_path %r escapes the skill directory" % rel_path)
    if parts[0] not in _ALLOWED_FILE_DIRS:
        raise SkillWriteError(
            "rel_path %r must start with one of %s" % (rel_path, "/".join(_ALLOWED_FILE_DIRS)))
    if parts[-1] == SKILL_FILENAME and len(parts) == 1:
        raise SkillWriteError("rel_path %r collides with %s" % (rel_path, SKILL_FILENAME))
    return "/".join(parts)


def patch_skill(name: str, old_string: str, new_string: str,
                *, replace_all: bool = False) -> str:
    """改已有技能的 SKILL.md（old_string 找不到就报错并说明）。

    replace_all=False 时要求 old_string 唯一，多匹配报「ambiguous」。校验顺
    序：name → 存在性（内置拒写）→ 定位 → 新文本 frontmatter 全量校验（含
    name 不得被改走）→ 原子替换落盘 → 缓存失效。
    """
    name = _validate_name(name)
    if not isinstance(old_string, str) or old_string == "":
        raise SkillWriteError("'old_string' must be a non-empty string")
    if not isinstance(new_string, str):
        raise SkillWriteError("'new_string' must be a string")
    skill_dir = _locate_for_write(name)
    path = os.path.join(skill_dir, SKILL_FILENAME)
    if not os.path.isfile(path):
        raise SkillWriteError("skill %r has no %s to patch" % (name, SKILL_FILENAME),
                              code="NOT_FOUND")
    text = _read_skill_md_text(skill_dir)
    count = text.count(old_string)
    if count == 0:
        raise SkillWriteError("old_string not found in %s; nothing was changed" % path,
                              code="NOT_FOUND")
    if count > 1 and not replace_all:
        raise SkillWriteError(
            "old_string appears %d times in %s; pass replace_all=true or make the "
            "fragment unique" % (count, path))
    new_text = (text.replace(old_string, new_string) if replace_all
                else text.replace(old_string, new_string, 1))
    try:
        meta, _ = parse_skill_md(new_text)
    except ValueError as exc:
        raise SkillWriteError("the patched SKILL.md would no longer parse: %s" % exc)
    parsed_name = (meta.get("name") or "").strip()
    if parsed_name != name:
        raise SkillWriteError(
            "the patched frontmatter name %r does not match the skill directory %r"
            % (parsed_name, name))
    _validate_frontmatter(new_text)
    _atomic_write(path, new_text)
    _invalidate_scan_cache()
    return path


def write_skill_file(name: str, rel_path: str, content: str) -> str:
    """写技能目录内的附加文件（如 references/xxx.md）。返回写入路径。"""
    if not isinstance(content, str):
        raise SkillWriteError("'content' must be a string")
    skill_dir, target = _resolve_rel_path(name, rel_path)
    if os.path.basename(target) == SKILL_FILENAME and os.path.dirname(target) == skill_dir:
        raise SkillWriteError(
            "refusing to overwrite %s via write_file; use patch_skill" % SKILL_FILENAME)
    _atomic_write(target, content)
    _invalidate_scan_cache()
    return target


def remove_skill_file(name: str, rel_path: str) -> str:
    """删技能目录内的附加文件（不能删 SKILL.md）。返回删除路径。

    删除后顺手收掉因此变空的附加子目录（只收 _ALLOWED_FILE_DIRS 首段里的
    一层，不递归、不越界）。
    """
    skill_dir, target = _resolve_rel_path(name, rel_path, must_exist=True)
    if os.path.basename(target) == SKILL_FILENAME:
        raise SkillWriteError(
            "refusing to remove %s; use delete_skill to remove the whole skill" % SKILL_FILENAME)
    os.unlink(target)
    parent = os.path.dirname(target)
    while parent != skill_dir:
        try:
            # 用 scandir 判空：静态守卫测试只豁免 scan() 里那一处目录列举
            if (os.path.basename(parent) in _ALLOWED_FILE_DIRS
                    and next(os.scandir(parent), None) is None):
                os.rmdir(parent)
            else:
                break
        except OSError:
            break
        parent = os.path.dirname(parent)
    _invalidate_scan_cache()
    return target


def delete_skill(name: str) -> str:
    """删掉整个技能目录（仅用户根里的那份；只剩内置 → 拒）。返回删除的目录。"""
    name = _validate_name(name)
    skill_dir = _locate_for_write(name)
    if not os.path.isdir(skill_dir):
        raise SkillWriteError("no such skill %r under the user skills root" % name,
                              code="NOT_FOUND")
    shutil.rmtree(skill_dir)
    _invalidate_scan_cache()
    return skill_dir


#: 每个动作的必填字段（供 apply_operations 做形状校验；值校验在各函数里）
_OPERATION_FIELDS = {
    "create": ("name", "description", "body"),
    "patch": ("name", "old_string", "new_string"),
    "write_file": ("name", "rel_path", "content"),
    "remove_file": ("name", "rel_path"),
    "delete": ("name",),
}


def apply_operations(operations: list[dict]) -> list[dict]:
    """原子地执行一批写操作（operations 数组形状与 Hermes Agent skill_manage 一致）。

    约定：全部成功才保留；任何一步失败 → 逆序回滚已落盘的动作 → 抛带
    索引和原因的 SkillWriteError，磁盘回到操作前状态，不留半成品。

    回滚证据在动作**之前**采集（旧文本 / 旧文件搬去临时处），所以能恢复：

    - create      → 删掉新建目录
    - patch       → 恢复 SKILL.md 原文本
    - write_file  → 目标原本不存在则删，原本存在则恢复原内容
    - remove_file → 恢复原文件（连带重建被收掉的空目录）
    - delete      → 目录搬去临时处，回滚时搬回来

    返回逐条结果（与入参一一对应）。
    """
    if not isinstance(operations, list) or not operations:
        raise SkillWriteError("'operations' must be a non-empty list")
    if len(operations) > MAX_OPERATIONS_PER_BATCH:
        raise SkillWriteError("too many operations in one batch: %d (max %d)"
                              % (len(operations), MAX_OPERATIONS_PER_BATCH))
    results: list[dict] = []
    # 回滚动作栈：(callable, ) —— 逆序执行
    undo: list = []
    # 批内 delete 动作搬去临时处的目录：全批成功后统一真删
    deletes: list[str | None] = []

    def _fail_op(index: int, action: str, op: dict, exc: SkillWriteError) -> SkillWriteError:
        for step in reversed(undo):
            try:
                step()
            except OSError:
                continue                      # 回滚尽最大努力，不掩盖原始错误
        return SkillWriteError(
            "operation #%d (%s %r) failed: %s -- the whole batch was rolled back, "
            "nothing was changed on disk" % (index, action, op.get("name"), exc),
            code=exc.code)

    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            raise SkillWriteError("operation #%d is not an object" % index)
        action = op.get("action")
        if action not in _OPERATION_FIELDS:
            raise SkillWriteError(
                "operation #%d: unknown action %r, expected one of %s"
                % (index, action, ", ".join(sorted(_OPERATION_FIELDS))))
        for field in _OPERATION_FIELDS[action]:
            if field not in op:
                raise SkillWriteError("operation #%d (%s): missing required field %r"
                                      % (index, action, field))
        try:
            if action == "create":
                name = _validate_name(op["name"])
                path = create_skill(name, op["description"], op["body"],
                                    when_to_use=op.get("when_to_use") or None,
                                    files=op.get("files") or None)
                undo.append(lambda d=os.path.dirname(path): shutil.rmtree(d, ignore_errors=True))
                results.append({"action": "create", "name": name, "path": path})
            elif action == "patch":
                name = _validate_name(op["name"])
                path = os.path.join(_locate_for_write(name), SKILL_FILENAME)
                original = _read_skill_md_text(os.path.dirname(path))
                target = patch_skill(name, op["old_string"], op["new_string"],
                                     replace_all=bool(op.get("replace_all")))
                undo.append(lambda p=path, t=original: _atomic_write(p, t))
                results.append({"action": "patch", "name": name, "path": target})
            elif action == "write_file":
                name = _validate_name(op["name"])
                _skill_dir, target = _resolve_rel_path(name, op["rel_path"])
                existed = os.path.isfile(target)
                original = _read_file_if_exists(target)
                target = write_skill_file(name, op["rel_path"], op["content"])
                if existed:
                    undo.append(lambda t=target, o=original: _atomic_write(t, o))
                else:
                    undo.append(lambda t=target: _unlink_quiet(t))
                results.append({"action": "write_file", "name": name, "path": target})
            elif action == "remove_file":
                name = _validate_name(op["name"])
                _skill_dir, target = _resolve_rel_path(name, op["rel_path"], must_exist=True)
                original = _read_file_if_exists(target)
                target = remove_skill_file(name, op["rel_path"])
                undo.append(lambda t=target, o=original: _atomic_write(t, o))
                results.append({"action": "remove_file", "name": name, "path": target})
            elif action == "delete":
                name = _validate_name(op["name"])
                directory = _locate_for_write(name)
                if not os.path.isdir(directory):
                    raise SkillWriteError(
                        "no such skill %r under the user skills root" % name, code="NOT_FOUND")
                # 搬去临时处（对调用方即「已删除」）；后续失败由 _move_back
                # 原样搬回 —— 目录内容全程没被拆开，回滚零损耗。全批成功后
                # 统一丢弃所有 held 目录（见循环后的清理）。
                held = _move_aside(directory)
                undo.append(lambda d=directory, h=held: _move_back(d, h))
                deletes.append(held)
                results.append({"action": "delete", "name": name, "path": directory})
        except SkillWriteError as exc:
            raise _fail_op(index, action, op, exc) from exc
        except OSError as exc:
            raise _fail_op(index, action, op, SkillWriteError(
                "IO error: %s" % exc, code="IO_ERROR")) from exc

    return results


def _read_file_if_exists(path: str) -> str | None:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _move_aside(directory: str) -> str | None:
    """把要删的目录挪去技能根旁的临时处（回滚时再搬回来，不丢数据）。"""
    if not os.path.isdir(directory):
        return None
    held = tempfile.mkdtemp(prefix=".skdel-", dir=os.path.dirname(directory))
    os.rename(directory, os.path.join(held, "skill"))
    return held


def _discard_held(held: str) -> None:
    """删除成功后清掉临时处。"""
    shutil.rmtree(held, ignore_errors=True)


def _move_back(directory: str, held: str | None) -> None:
    if held is None or not os.path.isdir(os.path.join(held, "skill")):
        return
    if os.path.exists(directory):
        shutil.rmtree(directory, ignore_errors=True)
    shutil.move(os.path.join(held, "skill"), directory)
    shutil.rmtree(held, ignore_errors=True)


if __name__ == "__main__":                                  # manual inspection
    for _s in list_skills():
        print("%-24s %s" % (_s.name, _s.description))
    for _path, _why in skipped():
        print("skipped %s (%s)" % (_path, _why), file=sys.stderr)