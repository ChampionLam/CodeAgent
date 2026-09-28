"""危险命令判定（2026-09-25 按命令模式口径重做）。

设计参照 Hermes Agent（https://github.com/NousResearch/hermes-agent ，MIT
License，Copyright (c) 2025 Nous Research）的 approval 思路，按我们的工具面
裁剪重写：**没有权限分级，也没有工作区概念** —— agent
可以在任意目录干活，被拦的只有「会造成安全影响的操作」。

两类：

  hardline  —— 不可逆、没有补救路径（删根/系统目录/家目录、格式化、写裸设备、
               fork bomb、杀掉所有进程、关机重启）。命中必须用户点头，且不给
               「记住」的机会。
  dangerous —— 影响面大但救得回来（递归删普通目录、强杀进程、停服务、改分区、
               删卷影副本、改注册表/引导、把远端内容直接喂给 shell）。
               命中问一次。

其余一律放行：任意目录读写文件、普通 shell、联网。联网的安全边界在
``web/egress.py``（私网拒绝 + DNS 钉死 + 跨站重定向重验 + 字节上限），不靠弹窗。

误伤防护（同类 agent 踩过的坑，见 Hermes Agent issue #93392）：命令名必须落在**命令位**上——行首、
``;`` / ``&&`` / ``||`` / ``|`` 之后、``$(`` 或反引号内、``sudo``/``env``/``exec``
之后。否则 ``git commit -m "别 rm -rf /"`` 这种把命令当参数写的会被误伤。没有命令
名可锚的规则（重定向、fork bomb）改比对**引号掩码版**。
"""
from __future__ import annotations

import re
from typing import NamedTuple

# 命令位：行首、命令分隔符之后、子 shell 内、sudo/env/exec 之类包装器之后。
_CMDPOS = r"(?:^|[;&|]|\|\||&&|\$\(|`|\bsudo\s+|\benv\s+|\bexec\s+|\(\s*)\s*"

# 递归删除的保护目标：删了没有恢复路径。
_SYSTEM_DIRS = (
    r"/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|"
    r"/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*"
)


def _rm_path(path_alt: str, tail: str = r"(?:\s|$|[)`;|&])") -> str:
    """`rm` 的目标路径：整体被引号包住，或裸写且以空格/行尾/元字符收尾。"""
    return rf'(?:["\'](?:{path_alt})["\']|(?:{path_alt}){tail})'


# rm + 旗标组，三条 rm 规则共用
_RM = _CMDPOS + r"rm\s+(-[^\s]*\s+)*"

HARDLINE_PATTERNS: list[tuple[str, str]] = [
    # 删根：裸写 / 、重复斜杠 //、/./ 和 /../ 这类等价写法，可带尾部通配
    (_RM + _rm_path(r"/(?:(?:\.\.?)?/)*(?:\.\.?)?\**|/ \*"), "递归删除根文件系统"),
    (_RM + _rm_path(_SYSTEM_DIRS), "递归删除系统目录"),
    (_RM + _rm_path(r"(?:~|\$\{?HOME\}?)(?:/?|/\*)?"), "递归删除家目录"),
    # Windows：删盘根或整个系统盘
    (_CMDPOS + r"(?:del|erase)\b[^\n]*(?:/s|/q)[^\n]*[a-z]:[\\/](\*|$|\s)",
     "递归删除盘符根目录（del /s）"),
    (_CMDPOS + r"rd\s+/s\s+/q\s+[a-z]:[\\/](\*|$|\s)", "递归删除盘符根目录（rd /s /q）"),
    (_CMDPOS + r"remove-item\b[^\n]*-recurse[^\n]*\s[a-z]:[\\/]\s*$",
     "递归删除盘符根目录（Remove-Item -Recurse）"),
    (_CMDPOS + r"mkfs(\.[a-z0-9]+)?\b", "格式化文件系统（mkfs）"),
    (_CMDPOS + r"format\b[^\n]*[a-z]:", "格式化驱动器（format）"),
    (_CMDPOS + r"format-volume\b", "格式化卷（Format-Volume）"),
    (_CMDPOS + r"clear-disk\b", "擦除磁盘（Clear-Disk）"),
    (_CMDPOS + r"dd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*",
     "向裸块设备写入（dd）"),
    (r">\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b", "重定向到裸块设备"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (_CMDPOS + r"kill\s+(-[^\s]+\s+)*-1\b", "杀掉所有进程（kill -1）"),
    (_CMDPOS + r"(shutdown|reboot|halt|poweroff)\b", "关机/重启"),
    (_CMDPOS + r"init\s+[06]\b", "init 0/6（关机/重启）"),
    (_CMDPOS + r"systemctl\s+(poweroff|reboot|halt|kexec)\b", "systemctl 关机/重启"),
    (_CMDPOS + r"(stop-computer|restart-computer)\b", "关机/重启（PowerShell）"),
    (_CMDPOS + r"telinit\s+[06]\b", "telinit 0/6（关机/重启）"),
    (_CMDPOS + r"vssadmin\b[^\n]*delete\s+shadows", "删除卷影副本（vssadmin）"),
    (_CMDPOS + r"wbadmin\b[^\n]*delete\s+(catalog|backup)", "删除备份（wbadmin）"),
    (_CMDPOS + r"bcdedit\b[^\n]*(/set|/delete)", "改引导配置（bcdedit）"),
    (_CMDPOS + r"cipher\b[^\n]*/w", "擦除磁盘空闲空间（cipher /w）"),
]

# 影响面大但有救：问一次
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (_CMDPOS + r"rm\s+(-[^\s]*\s+)*-[a-z]*r", "递归删除"),
    (_CMDPOS + r"(?:del|erase)\b[^\n]*(?:/s|/q)", "递归删除（del）"),
    (_CMDPOS + r"(rd|rmdir)\s+/s\b", "递归删除目录（rd /s）"),
    (_CMDPOS + r"taskkill\b[^\n]*/f", "强制结束进程（taskkill /F）"),
    (_CMDPOS + r"stop-process\b[^\n]*-force", "强制结束进程（Stop-Process -Force）"),
    (_CMDPOS + r"killall\b[^\n]*(-9|-kill|-s\s+kill)", "强制结束进程（killall）"),
    (_CMDPOS + r"kill\b[^\n]*-9", "强制结束进程（kill -9）"),
    (_CMDPOS + r"stop-service\b[^\n]*-force", "强制停服务（Stop-Service -Force）"),
    (_CMDPOS + r"sc\b[^\n]*(stop|delete)\s", "停/删服务（sc）"),
    (_CMDPOS + r"diskpart\b", "分区操作（diskpart）"),
    (_CMDPOS + r"reg\b[^\n]*delete\b", "删除注册表项（reg delete）"),
    (_CMDPOS + r"remove-itemproperty\b[^\n]*-force", "删除注册表值（Remove-ItemProperty）"),
    (_CMDPOS + r"icacls\b[^\n]*(/grant|/reset)", "改文件权限（icacls）"),
    (_CMDPOS + r"chmod\b[^\n]*(-r[^\n]*)?777", "放开所有人可写（chmod 777）"),
    (_CMDPOS + r"chown\b[^\n]*-r[^\n]*\broot\b", "递归改属主为 root（chown -R）"),
    (_CMDPOS + r"git\b[^\n]*push[^\n]*(--force|(^|\s)-f(\s|$))", "强推远端（git push --force）"),
    (_CMDPOS + r"docker\b[^\n]*(system\s+prune|volume\s+(rm|prune)\b)",
     "清空 Docker 数据（system prune / volume rm）"),
    # 把远端内容直接喂给 shell —— 命令混淆的经典形态
    (r"(iwr|invoke-webrequest|curl|wget)\b[^\n|]*\|\s*(iex|invoke-expression|sh|bash|zsh|powershell|pwsh)\b",
     "把远端内容直接执行（pipe to shell）"),
    (r"\b(iex|invoke-expression)\b[^\n]*(http|downloadstring)", "执行远端内容（Invoke-Expression）"),
    # 命令混淆：把解码后的内容直接喂给 shell（base64 / xxd / tr / openssl）
    (r"(base64\s+(-d|--decode)|xxd\s+-r|tr\s|openssl\s+(enc|base64)\s+-d)[^\n|]*\|\s*(sh|bash|zsh|python3?|perl)\b",
     "解码后直接执行（可能是在藏命令）"),
]

# 没有命令名可锚的规则（重定向 / fork bomb 之外还有这些），比对引号掩码版
_QUOTE_MASKED = (
    r"curl\b[^\n|]*\|\s*(sh|bash|zsh)\b",
    r"wget\b[^\n|]*\|\s*(sh|bash|zsh)\b",
)

_HARDLINE_RE = [(re.compile(p, re.I | re.S), r) for p, r in HARDLINE_PATTERNS]
_DANGEROUS_RE = [(re.compile(p, re.I | re.S), r) for p, r in DANGEROUS_PATTERNS]
_MASKED_RE = [(re.compile(p, re.I | re.S), "把远端内容直接执行（pipe to shell）")
              for p in _QUOTE_MASKED]


def _mask_quoted(command: str) -> str:
    """把引号里的内容抹成等长空格，防止引号内的文字被当成命令匹配。"""
    out = []
    quote = ""
    for ch in command:
        if quote:
            if ch == quote:
                quote = ""
                out.append(ch)
            else:
                out.append(" ")
            continue
        if ch in "\"'":
            quote = ch
        out.append(ch)
    return "".join(out)


def _payloads(command: str) -> list[str]:
    """包装器里的载荷也算一遍：sh -c / bash -c / eval / cmd /c / powershell -Command。

    引号不是绕过手段 —— `sh -c "rm -rf /"` 必须照样命中。
    """
    out = [command]
    for m in re.finditer(
            r"\b(?:sh|bash|zsh|cmd|powershell|pwsh)\b\s+(?:-[a-z]*c|-command)\s+(.+)",
            command, re.I | re.S):
        # 载荷常被引号包着（`bash -c "rm -rf /"`），剥掉首尾引号再当命令看，
        # 否则命令位锚点（要求行首/分隔符开头）就永远匹配不上。
        out.append(m.group(1).strip().strip("\"'"))
    for m in re.finditer(r"\beval\s+(.+)", command, re.I | re.S):
        out.append(m.group(1).strip().strip("\"'"))
    return out


class Verdict(NamedTuple):
    """一次命令判定的结果。requires_approval 为真时，UI 出会话内审批卡。"""

    requires_approval: bool
    tier: str            # "hardline" | "dangerous" | ""（放行）
    pattern: str
    reason: str

    @property
    def allowed_silently(self) -> bool:
        return not self.requires_approval


def judge_command(command: str) -> Verdict:
    """判定一条 shell 命令。hardline 优先于 dangerous。"""
    text = str(command or "")
    if not text.strip():
        return Verdict(False, "", "", "")

    variants = _payloads(text)
    masked = [_mask_quoted(v) for v in variants] + variants

    for compiled, reason in _HARDLINE_RE:
        for v in variants:
            if compiled.search(v):
                return Verdict(True, "hardline", compiled.pattern, reason)
    for compiled, reason in _MASKED_RE:
        for v in masked:
            if compiled.search(v):
                return Verdict(True, "hardline", compiled.pattern, reason)

    for compiled, reason in _DANGEROUS_RE:
        for v in variants:
            if compiled.search(v):
                return Verdict(True, "dangerous", compiled.pattern, reason)

    return Verdict(False, "", "", "")


#: 删路径时的保护位置：盘根 / 系统根 / 家目录顶层的直接子目录 —— 删了多半回不来
_PROTECTED_ROOTS = frozenset({
    "/", "/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/var",
    "/root", "/home", "/opt", "/dev", "/proc", "/sys", "/srv", "/mnt",
})
#: Windows：盘根、系统目录、用户配置文件顶层
_PROTECTED_WIN_ROOTS = frozenset({"windows", "program files", "program files (x86)", "users"})
_RE_PROTECTED_TOPDIR = re.compile(r"^/(home|root|usr|etc|var|boot|lib|opt|srv|mnt)/[^/]+$")
_RE_WIN_TOP = re.compile(r"^[a-z]:/(windows|program files(?: \(x86\))?|users)(/[^/]+)?$")


def is_protected_path(path: str) -> bool:
    """是不是「删了回不来」的位置。只用于 delete_path，不用于读写。"""
    raw = str(path or "").strip().strip("\"'").strip()
    if not raw:
        return False
    norm = raw.replace("\\", "/").lower()
    # 盘根：`C:`、`C:/`、`C:\` 归一化后都变成 `c:`
    if norm in _PROTECTED_WIN_ROOTS:
        return True
    if re.fullmatch(r"[a-z]:/?", norm):
        return True
    if _RE_WIN_TOP.match(norm):
        return True
    while len(norm) > 1 and norm.endswith("/"):
        norm = norm[:-1]
    if norm in _PROTECTED_ROOTS:
        return True
    return bool(_RE_PROTECTED_TOPDIR.match(norm))


#: 读了/改了就可能泄密的凭证位置（对应同类 agent 的「access to SSH keys / secrets」类目）
_CREDENTIAL_MARKERS = (
    ".ssh/id_rsa", ".ssh/id_dsa", ".ssh/id_ecdsa", ".ssh/id_ed25519", ".ssh/authorized_keys",
    ".aws/credentials", ".git-credentials", ".netrc", ".kube/config",
    "/etc/shadow", "/etc/sudoers", "\\sam", "\\system32\\config\\",
    "login data", "cookies.sqlite", ".env",
)

#: 会碰文件的工具：只对凭证位置问一次，其余一律放行
_FILE_TOOLS = ("read_file", "edit_file", "write_file", "list_dir", "search_files")


def _arg_path(args: dict) -> str:
    for key in ("path", "target", "target_path", "file_path", "out_dir", "output_dir"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def judge(tool_name: str, args: dict | None) -> Verdict:
    """工具级判定。**没有分级、没有工作区**：只有命令模式与极少数的保护位置。

    - run_shell   → 看命令模式（hardline / dangerous）
    - delete_path → 落在保护位置才拦（删普通文件不再问）
    - 读写文件    → 一律放行，只有碰凭证位置才问一次
    - 其余工具    → 一律放行（联网不弹；安全边界在 web/egress.py）
    """
    args = args or {}
    if tool_name == "run_shell":
        cmd = ""
        for key in ("command", "cmd", "script"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                cmd = value
                break
        return judge_command(cmd)

    if tool_name == "delete_path":
        target = _arg_path(args)
        if is_protected_path(target):
            return Verdict(True, "hardline", "protected-path", "删除受保护位置：%s" % target)
        return Verdict(False, "", "", "")

    if tool_name in _FILE_TOOLS:
        target = _arg_path(args).lower().replace("/", "\\")
        for marker in _CREDENTIAL_MARKERS:
            if marker.replace("/", "\\") in target:
                return Verdict(True, "dangerous", "credential-path",
                               "碰到凭证位置：%s" % _arg_path(args))
        return Verdict(False, "", "", "")

    return Verdict(False, "", "", "")