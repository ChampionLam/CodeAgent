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

**这是防手滑的 UX 护栏，不是安全边界。** 黑名单正则必然有绕过面（实测不拦：
绝对路径 `/bin/rm`、`$IFS` 拆词、`X=rm; $X -rf /`、`xargs rm`、`find . -delete`、
`python -c "shutil.rmtree(...)"`）。真正的边界在网络层（`web/egress.py`）与密钥
治理（key 只走 `.env`），这一层不承诺。

第二层（模型提示层，2026-10-01 起）：正则放行的 run_shell 命令再过一次
DecisionBert int8（子进程跑，见 guard_model.py），给校准后**弹卡概率** p。

**极性（2026-10-01 核定，不许再反）**：p 是「弹卡概率」，**高 p = 危险**。
  上游定义：noul = (action != allow)，即 action ∈ {ask, block} 记 1；
  标签映射 ACTION_HINT = {0:"放行", 1:"弹卡"}，p>=0.5 判弹卡。实测：ls -la p=0.023（安全）、
  rm -rf / p=0.944、find . -delete p=0.970。

  p >= 0.85        → 弹卡（模型高置信危险，UI 可标「模型很确定」）
  0.35 <= p < 0.85 → 弹卡（模型没把握，安全优先，照常弹卡）
  p < 0.35         → 放行 + 记入未知池（回流用）

2026-10-14 用户口径（改）：**默认上面这套不成立** —— 模型层不当闸门，只记录 p。
弹卡与否完全由前面两层正则决定。用户实测：读文档 + 画图跑的一串普通命令被模型层
判成「35% 可疑」条条弹卡（十几张卡还是出不了图），而正则那两层本来很克制。
只有 config.json 里 guard.modelGate=true（或 DESK_GUARD_MODEL_GATE=1）才恢复旧行为。

两个参数可配：0.35 是默认弹卡门槛、0.85 是高置信标注线，从 config.json
的 guard 段读（可覆盖），默认用上面两个数。
**模型层给的是提示，不是硬闸门，最终决定权在人手里**：它不推翻正则层
（正则命中 hardline/dangerous 时**根本不调模型**，直接弹卡、零附加延迟），
模型层挂了（文件缺失/依赖没装/子进程起不来）则 fail-open 回纯正则 ——
绝不因为模型挂了就让 agent 不能用。

其余一律放行：任意目录读写文件、普通 shell、联网。联网的安全边界在
``web/egress.py``（私网拒绝 + DNS 钉死 + 跨站重定向重验 + 字节上限），不靠弹窗。

误伤防护（同类 agent 踩过的坑，见 Hermes Agent issue #93392）：命令名必须落在**命令位**上——行首、
``;`` / ``&&`` / ``||`` / ``|`` 之后、``$(`` 或反引号内、``sudo``/``env``/``exec``
之后。否则 ``git commit -m "别 rm -rf /"`` 这种把命令当参数写的会被误伤。没有命令
名可锚的规则（重定向、fork bomb）改比对**引号掩码版**。
"""
from __future__ import annotations

import os
import re
from typing import NamedTuple


def _log_internal(msg: str) -> None:
    """内部日志出口（数值/阈值只进这里，不进用户可见的 reason）。"""
    try:
        import logging_kit
        logging_kit.get_logger().log("info", msg, source="guard")
    except Exception:
        pass

# 命令位：行首、**换行**、命令分隔符之后、子 shell 内、sudo/env/exec 之类包装器之后。
# （2026-10-14 补换行：多行脚本里第二行往后的命令以前不在命令位，`Get-Content a.md`
#  换行接 `del x` 会被整条放过。）
_CMDPOS = r"(?:^|[;&|\n]|\|\||&&|\$\(|`|\bsudo\s+|\benv\s+|\bexec\s+|\(\s*)\s*"

# 递归删除的保护目标：删了没有恢复路径。
_SYSTEM_DIRS = (
    r"/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|"
    r"/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*"
)

#: 绝对路径（盘符开头，或 / 开头；排除 /f /s /q 这种单字母开关）——删文件类只对
#: 绝对目标弹卡，代理在自己工作区里清相对路径（`rm build/tmp.txt`）不算危险操作。
#: 注意别写成 `(?:^|\s)/…`：嵌进大正则里那个 `^` 锚字符串开头、`\s` 又常被前面的
#: `\s+` 吃掉，结果整条不命中（2026-10-14 踩过）。
_ABS_PATH = (r"[A-Za-z]:[\\/][^\s\"';|]*"
             r"|(?:^|[\s\"'=(,])/(?![A-Za-z](\s|$))[^\s\"';|]+")


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
    # 2026-10-14 补（用户口径：正则该拦的拦、读类永不弹）。以前这几类只在 /s|/q 形态
    # 下才命中，普通的删文件 / 删目录 / 动注册表 / 改权限都在盲区 —— 真机实测
    # `del D:\...\a.txt` 直接跑掉不弹卡。
    # 删文件类**只拦绝对路径**（盘符 / 或 / 开头）：代理在自己工作区里清构建产物
    # （`rm build/tmp.txt`、`Remove-Item .\build\out.txt`）属于正常干活，不该打断；
    # 动工作区之外的东西才点头。档位 dangerous（可按 动词+目标目录 记忆）。
    (_CMDPOS + r"(?:del|erase)\b[^\n]*(?:" + _ABS_PATH + r")", "删除文件（del）"),
    (_CMDPOS + r"(?:rd|rmdir)\b[^\n]*(?:" + _ABS_PATH + r")", "删除目录（rd/rmdir）"),
    (_CMDPOS + r"rm\b[^\n]*(?:" + _ABS_PATH + r")", "删除文件（rm）"),
    (_CMDPOS + r"(?:remove-item|ri)\b[^\n]*(?:" + _ABS_PATH + r")",
     "删除文件/目录（Remove-Item）"),
    (_CMDPOS + r"(?:unlink|shred)\b[^\n]*(?:" + _ABS_PATH + r")", "删除/清空文件（unlink）"),
    # 注册表 / 权限 / 服务 / 计划任务 / 防火墙：没有「工作区内」这种说法，无条件拦
    (_CMDPOS + r"reg\b[^\n]*(add|import)\b", "写注册表（reg add）"),
    (_CMDPOS + r"(?:new-itemproperty|set-itemproperty)\b", "改注册表值（Set-ItemProperty）"),
    (_CMDPOS + r"takeown\b", "夺取文件所有权（takeown）"),
    (_CMDPOS + r"set-executionpolicy\b", "改 PowerShell 执行策略"),
    (_CMDPOS + r"(?:schtasks|at)\b[^\n]*(/create|/delete|-create)",
     "改计划任务（schtasks）"),
    (_CMDPOS + r"netsh\b[^\n]*(firewall|advfirewall)[^\n]*(add|set|delete)",
     "改防火墙规则（netsh）"),
    (_CMDPOS + r"(?:rclone|aws|az|gsutil|gcloud)\b[^\n]*(delete|rm|remove)\b",
     "删除云端数据（云 CLI）"),
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


# ---------------------------------------------------------------------------
# 第二层：模型提示层（DecisionBert int8，子进程）。
# 只对「正则放行的 run_shell 命令」生效；命中正则时根本走不到这里。
# ---------------------------------------------------------------------------

#: 模型层默认阈值（可由 config.json 的 guard 段覆盖，见 guard_model._read_thresholds）。
#: **极性**：p 是弹卡概率，高 p = 危险（noul = action != allow，上游训练
#: 定义；ls -la 实测 p=0.023，rm -rf / p=0.944）。两个阈值之上**都是弹卡**：
MODEL_ASK_THRESHOLD = 0.35      # p >= 0.35 → 弹卡（默认门槛，安全优先）
MODEL_CONFIDENT_THRESHOLD = 0.85  # p >= 0.85 → 弹卡且 UI 标「模型很确定」

#: 懒加载的默认模型 client（serve.py 子进程）。缺文件/缺依赖时不可用，
#: judge_command_layered 自动 fail-open 到纯正则。测试可注入假件。
_default_model = None


def _get_default_model():
    """懒加载共享的单例 GuardModel；起不来就 None（纯正则）。"""
    global _default_model
    if _default_model is None:
        try:
            import guard_model
            _default_model = guard_model.GuardModel()
        except Exception:
            return None
    return _default_model


def warmup(*, log=None) -> bool:
    """启动预热（2026-10-01 冷启动静默 fail-open 修复）：后台线程调这个。

    背景：机器刚开机/页缓存被挤掉时，第一条命令的模型层要付整段冷启动
    （int8 冷缓存实测 4.26s、fp32 5.89s —— 后者曾超旧 5.0s 超时，
    **一声不响地**降级回纯正则）。这个函数把冷启动挪到启动期：
    spawn 共享单例 + 一条哑推理（``warmup ping``），不占关键路径。

    接线（sidecar.main 启动处，默认开）：

        threading.Thread(target=guard.warmup, daemon=True,
                         kwargs={"log": sidecar.log}).start()

    口径：**默认开**，但退化为静默无副作用 —— 模型层关了/缺文件/缺依赖/
    预热本身炸了，都只返回 False 并最多记一条 warn（log 给了才有），
    绝不影响启动、不留半起的子进程。关闭方法：
      * 整层关：``DESK_GUARD_MODEL_LAYER=0`` 或 config.json guard 段
        ``model_layer: false``（judge 回纯正则，warmup 直接跳过）；
      * 只关预热：config.json guard 段 ``model_warmup: false``（模型层
        照常工作，只是首条命令回到冷启动路径）。

    log 传 None 时降级日志走 guard_model 默认的 stderr 行。
    返回 True = 预热完成（模型层就绪）。
    """
    try:
        import guard_model
        if log is not None:
            guard_model.set_log_hook(log)
        if not model_layer_enabled():
            return False
        if not _warmup_enabled():
            return False
        model = _get_default_model()
        if model is None:
            return False
        return model.warmup() is not None
    except Exception:
        return False


def _warmup_enabled() -> bool:
    """预热开关：config.json guard 段 ``model_warmup``（默认 true）。

    与 model_layer_enabled 同一读法（无环境变量覆盖 —— 预热只是性能
    优化，不配单独的 env 逃生口）。
    """
    try:
        import appconfig
        cfg = None
        if hasattr(appconfig, "capability"):
            cfg = appconfig.capability("guard", None)
        if cfg is None and hasattr(appconfig, "section"):
            cfg = appconfig.section("guard", None)
        if isinstance(cfg, dict) and "model_warmup" in cfg:
            return bool(cfg["model_warmup"])
    except Exception:
        pass
    return True


def _default_text_builder(tool: str, cwd: str, command: str) -> str:
    """输入契约：与训练逐字一致（漂移只静默掉分不报错）。"""
    return f"[工具] {tool} [目录] {cwd} [命令] {command}"


def _model_action(p: float, thresholds: dict) -> str:
    """p → block / block_confident / allow 三档。

    **极性（不许反）**：p 是弹卡概率（noul = action != allow），高 p = 危险。
    两个阈值之上都弹卡，区别只在 UI 标注（p >= 0.85 「模型很确定」）；
    p < 0.35 才放行并记入未知池。旧版本曾把 p>=0.85 写成「高置信放行」，
    方向完全反了（`find . -delete` p=0.970 会被放走）—— 已修正，勿回退。
    """
    if p >= thresholds["confident"]:
        return "block_confident"
    if p >= thresholds["ask"]:
        return "block"
    return "allow"


def judge_command_layered(command: str, *, model=None,
                          thresholds: dict | None = None,
                          text_builder=None) -> Verdict:
    """两段式判定：正则硬闸（第一层，原样）→ 模型提示层（第二层）。

    分层口径（顺序不可反转）：
      1. 正则命中 hardline/dangerous → **立刻返回原 Verdict，不调模型**，
         零附加延迟；
      2. 正则放行 → 只对 run_shell 命令跑一次模型推理得校准后弹卡概率 p
         （高 p = 危险）：
           p >= confident(0.85)     → 弹卡（模型高置信，UI 标「模型很确定」）
           ask(0.35) <= p < conf    → 弹卡（模型没把握，安全优先）
           p < ask                  → 放行 + 记入未知池回流
      3. 模型层不可用（None / 抛异常）→ **fail-open 到纯正则**（放行）。

    模型层给的是提示，不是硬闸门，最终决定权在人手里。
    """
    verdict = judge_command(command)
    if verdict.requires_approval:
        return verdict                      # 第一层命中：绝不调模型
    text = str(command or "")
    if not text.strip():
        return verdict                      # 空命令：两层都无话可说

    if model is None:
        model = _get_default_model()
        thresholds = thresholds or _model_thresholds()
        text_builder = text_builder or _default_text_builder
    if model is None:
        return verdict                      # 模型层不可用 → 纯正则
    thresholds = thresholds or {"ask": MODEL_ASK_THRESHOLD,
                                "confident": MODEL_CONFIDENT_THRESHOLD}
    text_builder = text_builder or _default_text_builder

    try:
        out = model.predict(text, text=text_builder(
            "run_shell", "/home/user", text))
    except TypeError:
        # 假件/旧签名只有位置参数
        out = model.predict(text)
    except Exception:
        return verdict                      # 模型层炸了 → fail-open
    if not out:
        return verdict                      # 不可用 / 回复坏 → fail-open

    p = float(out.get("p", 0.0))
    action = _model_action(p, thresholds)
    # 2026-10-14 用户口径：模型层默认**只观察、不当闸门**。真正该拦的是上面两层
    # 正则；模型说「可疑」但没有正则命中时，弹卡只会变成噪音（读文档跑的一串
    # 普通命令条条弹卡，用户实测十几张卡还出不了图）。guard.modelGate=true 才恢复成硬闸。
    gate = model_gate_enabled()
    if action == "block_confident":
        # t_87ce5f71：面向用户的文案不许带内部术语（弹卡/校准/p=…/阈值），
        # 数值细节只进内部日志（日志里叫弹卡没问题）。
        _log_internal(
            "模型提示层：校准弹卡概率 p=%.3f ≥ %.2f（高置信危险）%s"
            % (p, thresholds["confident"],
               "，已拦截待确认" if gate else "，仅记录（模型层非闸门，不弹卡）"))
        if gate:
            return Verdict(True, "dangerous", "model-block",
                           "检测到可疑命令执行（危险等级：高）。这条命令可能改动"
                           "系统文件或从网络下载内容，确认后才会执行")
        return Verdict(False, "", "model-observe", "")
    if action == "block":
        _log_internal(
            "模型提示层：校准弹卡概率 p=%.3f 落在不确定带（%.2f–%.2f）%s"
            % (p, thresholds["ask"], thresholds["confident"],
               "，安全优先，已拦截待确认" if gate else "，仅记录（模型层非闸门，不弹卡）"))
        if gate:
            return Verdict(True, "dangerous", "model-block",
                           "检测到可疑命令执行（危险等级：中）。无法完全排除"
                           "风险，确认后才会执行")
        return Verdict(False, "", "model-observe", "")
    return Verdict(False, "", "",
                   "模型校准弹卡概率 p=%.3f < %.2f（放行，记入未知池回流）"
                   % (p, thresholds["ask"]))


def _model_thresholds() -> dict:
    """从 config.json 的 guard 段读阈值，缺省用默认两档。

    键名：guard.ask（默认 0.35，弹卡门槛）与 guard.confident（默认 0.85，
    高置信标注线）。兼容旧键 model_ask_threshold / model_confident_threshold。
    """
    try:
        import guard_model
        return guard_model.threshold_values()
    except Exception:
        return {"ask": MODEL_ASK_THRESHOLD,
                "confident": MODEL_CONFIDENT_THRESHOLD}


def model_layer_enabled() -> bool:
    """模型层总开关（2026-10-01，分层入口接线时加）。

    读法与阈值同一口径：config.json 的 guard 段 `model_layer`
    （兼容旧键 `layered`），可用环境变量 ``DESK_GUARD_MODEL_LAYER``
    覆盖（取值 0/1、true/false、on/off、yes/no）。**默认开**。

    关掉 = 一键回纯正则：judge_tool_layered 完全不碰 guard_model /
    不起子进程，行为与旧 judge 一字不差。默认开且模型层缺文件/缺依赖
    时自动 fail-open，所以这个开关只是逃生口，不是常态配置。
    """
    env = os.environ.get("DESK_GUARD_MODEL_LAYER", "").strip().lower()
    if env:
        return env not in ("0", "false", "off", "no")
    try:
        import appconfig
        cfg = None
        if hasattr(appconfig, "capability"):
            cfg = appconfig.capability("guard", None)
        if cfg is None and hasattr(appconfig, "section"):
            cfg = appconfig.section("guard", None)
        if isinstance(cfg, dict):
            for key in ("model_layer", "layered"):
                if key in cfg:
                    return bool(cfg[key])
    except Exception:
        pass
    return True


def model_gate_enabled() -> bool:
    """模型层能不能**单独**拦命令（2026-10-14 用户钦定口径：默认 **不能**）。

    config.json 的 guard 段 ``modelGate``（兼容 ``model_gate``），环境变量
    ``DESK_GUARD_MODEL_GATE`` 覆盖（0/1、true/false、on/off、yes/no）。

    默认 **False**：模型层只做观察与回流，弹不弹卡完全由两层正则决定。
    置 true 才恢复旧行为（p ≥ ask 即拦）—— 留给需要更严策略的部署。
    """
    env = os.environ.get("DESK_GUARD_MODEL_GATE", "").strip().lower()
    if env:
        return env not in ("0", "false", "off", "no")
    try:
        import appconfig
        cfg = None
        if hasattr(appconfig, "capability"):
            cfg = appconfig.capability("guard", None)
        if cfg is None and hasattr(appconfig, "section"):
            for key in ("guard", "capabilities"):
                try:
                    got = appconfig.section(key)
                except Exception:
                    got = None
                if isinstance(got, dict) and "guard" in got and isinstance(got["guard"], dict):
                    cfg = got["guard"]
                    break
                if isinstance(got, dict) and ("modelGate" in got or "model_gate" in got):
                    cfg = got
                    break
        if isinstance(cfg, dict):
            for key in ("modelGate", "model_gate"):
                if key in cfg:
                    return bool(cfg[key])
    except Exception:
        pass
    return False


def judge_tool_layered(tool_name: str, args: dict | None) -> Verdict:
    """工具级分层判定（agent_loop 的 default_deps.judge 接这里）。

    与 judge() 的唯一差别：run_shell 且正则放行的命令再过一次
    judge_command_layered（模型提示层）。其余工具（delete_path /
    读写文件 / 其他）**判定路径与 judge() 一字不差**——模型层只作用
    于 run_shell 且正则放行的命令，绝不扩大面。

    开关（model_layer_enabled，默认开）关掉时，这里就是纯 judge()。
    """
    if not model_layer_enabled():
        return judge(tool_name, args)
    if tool_name != "run_shell":
        return judge(tool_name, args)
    return judge_command_layered(_shell_command(args))


def _shell_command(args: dict | None) -> str:
    """从 run_shell 的 args 里取命令文本（与 judge() 同一取法）。"""
    args = args or {}
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def layered_verdict_p(verdict: Verdict) -> float | None:
    """从 layered 判定的 reason 里取回模型 p（展示/审计用）。"""
    m = re.search(r"p=([0-9.]+)", verdict.reason or "")
    return float(m.group(1)) if m else None


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

    # MCP 工具（mcp_* 前缀）：高风险外挂工具，一律走审批（design §2.3 的
    # mcp_default_ask：外挂 server 的能力边界不受我们控制，宁可问一次）。
    # P1 再按 server 细化信任档位。
    if tool_name.startswith("mcp_"):
        return Verdict(True, "dangerous", "mcp_default_ask",
                       "MCP 外部工具调用（%s）——首次使用需要确认" % tool_name)

    return Verdict(False, "", "", "")