# -*- coding: utf-8 -*-
"""self_heal — CodeAgent 自愈引擎（t_e0fbba7d P0-5，用户最看重的一块）。

任务书要求的完整链路：
  病历(error_taxonomy.ErrorFacts) -> 调模型诊断 -> 产出受约束动作
  -> 沙箱验证(临时副本) -> 通过才应用到真实位置 -> 失败自动回滚
  -> 写 _lessons_store -> 下次同类错误先查经验
  -> 日志/UI 全程可见「病历 -> 模型建议 -> 验证结果 -> 是否应用」

红线（红线检查在 apply 之前，一律 stop 并报告，不许为验证通过而放行）：
  R1 删除用户数据（文件内容删除/整文件删除）
  R2 改用户配置(.env/config.json/config.mcp.json 等)
  R3 装系统级软件（commands 里出现包管理器安装）
  R4 外发网络请求（patch 文本里出现 http 调用/爬虫类命令）
  同一错误自愈 N 次后停止并报告（SAME_ERROR_BUDGET，按 kind+code 计数，落盘）。

模型层可替换桩：引擎不直接 import llm；诊断函数由调用方注入
（sidecar 注入 llm.chat_once 的包装；测试注入假模型，零真实模型依赖）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

import error_taxonomy as et

# ---------------------------------------------------------------------------
# 配置常量
# ---------------------------------------------------------------------------

#: 同一错误（kind+code）自动自愈的最大次数，超过停止并报告。
SAME_ERROR_BUDGET = 3
#: 每个补丁最多改几个文件（受约束动作的核心限制之一）。
MAX_PATCH_FILES = 3
#: 诊断提示词里各片段的截断。
SNIPPET_MAX = 3000
#: 重试提示里回显上一轮模型原样输出的截断长度。
RETRY_SNIPPET = 600
#: 解析失败/无可执行补丁时带原样报错重试的次数（1 = 仅重试一次）。
MODEL_RETRIES = 1
#: 沙箱验证超时（秒）。
VERIFY_TIMEOUT = 60

#: 沙箱根目录名（挂在 data/ 下，自动清理）。
SANDBOX_DIRNAME = "heal-sandbox"

#: 自愈历史（防无限重试 + UI 可查）落盘文件名，存 data/ 下。
HISTORY_FILENAME = "self_heal_history.json"
HISTORY_MAX = 50

#: 红线动作需要人工确认的确认词（UI 把 reason 带给用户，用户答复）。
REDACT_WORDS = ("rm ", "del ", "format ", "mkfs", "pip install", "apt install",
                "apt-get install", "yum install", "dnf install", "winget install",
                "npm install -g", "curl ", "wget ", "Invoke-WebRequest",
                "requests.post", "requests.get", "urllib", "httpx")

_CONFIG_FILE_RE = re.compile(
    r"(^|[\\/])(\.env|config(\.[\w\-]+)?\.json|settings\.json|\.env\.\w+)$", re.I)
#: workspace 根 之外的路径一律算红线 R5（写非 workspace 路径）。
_FORBIDDEN_OUTSIDE = ("python", "electron", "ui", "node_modules", ".git")


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------

@dataclass
class HealAction:
    """模型产出的**受约束**动作（不是无边界的改仓库）。"""

    diagnosis: str = ""                  # 为什么坏
    file: str = ""                       # 要改哪个文件（workspace 相对路径）
    old_text: str = ""                   # 找什么
    new_text: str = ""                   # 换成什么
    commands: list = field(default_factory=list)   # 验证/修复命令（沙箱里跑）
    explanation: str = ""                # 为什么这样改


@dataclass
class HealOutcome:
    """一次自愈的完整结局 —— UI / 日志 / lessons 全都用它。"""

    run_id: str = ""
    kind: str = ""
    code: str = ""
    stage: str = "created"               # created|diagnosed|patched|sandboxed|applied|verified|rolled_back|skipped|failed|stopped
    diagnosis: str = ""
    action: HealAction | None = None
    sandbox_dir: str = ""
    verified: bool = False
    applied: bool = False
    rollback_reason: str = ""
    failure_reason: str = ""
    lesson: dict = field(default_factory=dict)
    ts: str = ""

    def to_dict(self) -> dict:
        d = {
            "runId": self.run_id, "kind": self.kind, "code": self.code,
            "stage": self.stage, "diagnosis": self.diagnosis,
            "sandboxDir": self.sandbox_dir, "verified": self.verified,
            "applied": self.applied, "rollbackReason": self.rollback_reason,
            "failureReason": self.failure_reason, "ts": self.ts,
        }
        if self.action is not None:
            d["action"] = {
                "file": self.action.file, "diagnosis": self.action.diagnosis,
                "oldTextLen": len(self.action.old_text or ""),
                "newText": (self.action.new_text or "")[:300],
                "commands": list(self.action.commands),
                "explanation": self.action.explanation,
            }
        if self.lesson:
            d["lesson"] = self.lesson
        return d


# ---------------------------------------------------------------------------
# 红线检查（纯函数，单测的重点）
# ---------------------------------------------------------------------------

def redline_violation(action: HealAction, workspace_root: str,
                      facts: "et.ErrorFacts") -> str | None:
    """返回红线描述；None = 无红线。检查 apply 前做，不许为测试通过放行。"""
    import os as _os
    rel = (action.file or "").strip().replace("\\", "/")
    # R1/R2: 目标文件名
    if _CONFIG_FILE_RE.search(rel):
        return "R2 红线：补丁目标是用户配置文件（%s）" % rel
    # R1: new_text 为空 + old_text 非空 + 无 commands → 纯删除内容
    if action.old_text and not action.new_text and not action.commands:
        return "R1 红线：补丁只删内容不写新内容（删除用户数据）"
    # R5: 路径逃逸出 workspace
    if rel.startswith("..") or _os.path.isabs(rel):
        return "R5 红线：补丁路径逃逸 workspace（%s）" % rel
    # 系统代码（python/**、electron/**、ui/** —— 只建议，不自动改）
    top = rel.split("/")[0] if "/" in rel else ""
    if top in _FORBIDDEN_OUTSIDE:
        return "R3 红线：目标是 CodeAgent 自身代码（%s），只出建议不改" % rel
    # R3/R4: 命令检查
    joined = " ".join(str(c) for c in (action.commands or []))
    for word in REDACT_WORDS:
        if word in joined:
            return "R3/R4 红线：命令含 %r（装软件/外发网络）" % word
    # R4: new_text 引入网络请求
    for word in ("requests.", "urllib", "httpx", "http.post", "http.get"):
        if word in (action.new_text or ""):
            return "R4 红线：补丁引入网络请求代码"
    return None


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class SelfHealEngine:
    """一次构造一个引擎；engine.heal(facts) 跑完整链路。

    依赖全部注入（零 import llm/tools，测试零网络零真实模型）：
      - ask_model(prompt) -> str          模型诊断（可替换桩）
      - workspace_root: str               被修的 workspace
      - run_verifier(path, commands) -> (ok, log)  沙箱里跑命令（可替换桩）
      - lesson_sinks: list                写经验的 store（有 .record() 即可）
      - data_dir: str                     沙箱/历史/lessons 落盘根
      - auto_apply: bool                  默认 True —— 沙箱验证通过的补丁自动落地
                                          （备份+回滚兜底）。注意：这**不是**红线开关——
                                          红线 R1-R5 在 apply 之前由 redline_violation()
                                          独立拦截，命中一律 stop，与本参数无关。
                                          auto_apply=False 仅表示「补丁已验证但等用户
                                          确认后才应用」，不代表红线安全。
    """

    def __init__(self, *, ask_model, workspace_root: str, run_verifier=None,
                 lesson_sinks=None, data_dir: str = "", auto_apply: bool = True,
                 log=None, run_id: str = ""):
        self.ask_model = ask_model
        self.workspace_root = os.path.abspath(workspace_root or ".")
        self.run_verifier = run_verifier or _default_verifier
        self.lesson_sinks = list(lesson_sinks or [])
        self.data_dir = data_dir or os.path.join(self.workspace_root, "..", "data")
        self.auto_apply = auto_apply
        self.log = log or (lambda *a, **k: None)
        self.run_id = run_id or ("heal_%d" % int(time.time()))
        self.history_path = os.path.join(self.data_dir, HISTORY_FILENAME)

    # ------------------------------------------------------------------
    # 主链路
    # ------------------------------------------------------------------

    def heal(self, facts: "et.ErrorFacts", trigger: str = "auto") -> HealOutcome:
        """完整链路。任何内部异常都收进 outcome，绝不让自愈弄死主流程。"""
        out = HealOutcome(run_id=self.run_id, kind=facts.kind, code=facts.code,
                          ts=time.strftime("%Y-%m-%d %H:%M:%S"))
        try:
            # 0. 预算：同一错误自愈次数超限 → 停止并报告
            count = self._error_count(facts)
            if count >= SAME_ERROR_BUDGET:
                out.stage = "stopped"
                out.failure_reason = (
                    "同一错误（%s/%s）已自动自愈 %d 次仍未根治，停止自动尝试，"
                    "请人工处理。历史见 %s" % (facts.kind, facts.code, count,
                                                  self.history_path))
                self._record_history(out)
                self._emit_lesson(out, facts, success=False,
                                  note="自愈次数超限停止")
                return out

            # 依赖缺失：不走模型/沙箱 —— 只出修复提示（任务书红线：不自动装）
            if facts.kind == et.KIND_DEPENDENCY_MISSING:
                dep = str(facts.context.get("dependency") or "")
                out.diagnosis = ("依赖缺失：%s。" % dep
                                 + et.dependency_fix_hint(dep) if dep else "依赖缺失。")
                out.stage = "skipped"
                out.failure_reason = "红线：自动安装依赖被禁止，仅提示用户"
                self._record_history(out)
                return out

            # 1. 调模型诊断 → 受约束动作（解析失败/没给补丁 → 带原样报错重试一次）
            prompt = self._diagnosis_prompt(facts)
            raw = self.ask_model(prompt)
            out.stage = "diagnosed"
            action = self._parse_action(raw)
            if action is None or not action.file:
                problem = ("输出不是合法 JSON 对象" if action is None
                           else "只给了诊断，没给可执行的 file/old_text/new_text")
                self.log("warn", "heal: diagnosis unusable (%s), retrying "
                                 "with JSON-only reminder" % problem)
                retry_prompt = self._retry_prompt(facts, raw, problem)
                retry_raw = self.ask_model(retry_prompt)
                retry_action = self._parse_action(retry_raw)
                if retry_action is not None and retry_action.file:
                    raw, action = retry_raw, retry_action
                    self.log("info", "heal: retry produced an actionable patch")
                else:
                    # 重试也没拿到 → 用重试轮的输出定性失败（原样入日志）
                    raw = retry_raw or raw
                    action = retry_action
            out.action = action
            out.diagnosis = action.diagnosis if action else ""
            if action is None or not action.file:
                out.stage = "failed"
                out.failure_reason = (
                    "模型诊断（含 %d 次重试）未给出可用的受约束动作（原始输出已入日志）"
                    % MODEL_RETRIES)
                self.log("warn", "heal: model gave no actionable patch: %s"
                         % _clip(raw, 300))
                self._record_history(out)
                return out

            # 2. 红线检查（apply 前做，不管后面验证过不过）
            violation = redline_violation(action, self.workspace_root, facts)
            if violation:
                out.stage = "stopped"
                out.failure_reason = violation + " —— 已停止，需人工确认"
                self._emit_lesson(out, facts, success=False, note=violation)
                self._record_history(out)
                return out

            # 3. 沙箱：临时副本上应用补丁 + 跑验证
            sandbox = self._make_sandbox()
            out.sandbox_dir = sandbox
            target_rel = action.file.replace("\\", "/")
            real_path = os.path.join(self.workspace_root, target_rel)
            if not os.path.isfile(real_path):
                out.stage = "failed"
                out.failure_reason = "补丁目标文件不存在：%s" % target_rel
                self._record_history(out)
                return out
            sb_file = os.path.join(sandbox, target_rel)
            os.makedirs(os.path.dirname(sb_file) or sandbox, exist_ok=True)
            shutil.copy2(real_path, sb_file)

            patched = _apply_text_patch(sb_file, action.old_text, action.new_text)
            if not patched:
                out.stage = "failed"
                out.failure_reason = (
                    "补丁的 old_text 在目标文件里找不到（模型给的上下文对不上）")
                self._record_history(out)
                return out
            out.stage = "patched"

            ok, verify_log = self.run_verifier(sb_file, action.commands)
            out.verified = bool(ok)
            out.stage = "sandboxed" if ok else "failed"
            if not ok:
                out.failure_reason = "沙箱验证失败：%s" % _clip(verify_log, 400)
                # 沙箱不过 → 丢弃沙箱、记失败原因、不动真实位置
                self._record_history(out)
                self._emit_lesson(out, facts, success=False,
                                  note="沙箱验证失败：" + _clip(verify_log, 200))
                return out

            # 4. 验证通过 → 应用到真实位置（先备份）
            # auto_apply 只管「验证通过的补丁是否自动落盘」，红线已在上面第 2 步
            # redline_violation() 拦截完毕（命中即 stop），与本分支无关。
            if not self.auto_apply:
                out.stage = "skipped"
                out.failure_reason = "auto_apply=False：补丁已验证，等用户确认后才应用"
                self._record_history(out)
                return out
            backup = self._backup(real_path)
            apply_ok = _apply_text_patch(real_path, action.old_text, action.new_text)
            if not apply_ok:
                out.stage = "rolled_back"
                out.rollback_reason = "真实位置应用补丁失败（old_text 失配），已恢复备份"
                if backup and os.path.isfile(backup):
                    shutil.copy2(backup, real_path)
                self._record_history(out)
                return out
            # 沙箱命令同时在真实位置跑一遍（验证命令本身就是修复的一部分，如补装 import）
            try:
                ok2, _ = self.run_verifier(real_path, action.commands)
                if not ok2:
                    shutil.copy2(backup, real_path)
                    out.stage = "rolled_back"
                    out.rollback_reason = "真实位置验证未通过，已回滚恢复备份"
                    self._record_history(out)
                    self._emit_lesson(out, facts, success=False,
                                      note="真实位置验证失败，回滚")
                    return out
            except Exception as exc:          # noqa: BLE001
                shutil.copy2(backup, real_path)
                out.stage = "rolled_back"
                out.rollback_reason = "真实位置验证异常（%s），已恢复备份" % exc
                self._record_history(out)
                return out

            out.applied = True
            out.stage = "verified"
            self._emit_lesson(out, facts, success=True,
                              note="自愈成功：" + _clip(action.explanation, 120))
            self._record_history(out)
            return out

        except Exception as exc:              # noqa: BLE001 - 自愈绝不能连坐主流程
            out.stage = "failed"
            out.failure_reason = "自愈引擎内部异常：%s: %s" % (type(exc).__name__, exc)
            try:
                self._record_history(out)
            except Exception:                 # noqa: BLE001
                pass
            return out

    # ------------------------------------------------------------------
    # 组装与内部
    # ------------------------------------------------------------------

    def _diagnosis_prompt(self, facts: "et.ErrorFacts") -> str:
        d = facts.to_dict()
        code_snippets = self._related_code_snippets(facts)
        return (
            "你是桌面 agent 的自愈诊断模块。下面是一次运行故障的「病历」和相关代码/配置片段。\n"
            "产出一个**受约束的修复动作**。\n"
            "\n"
            "【输出契约 —— 违反即作废】\n"
            "1. 只输出一个 JSON 对象，前面/后面/里面都不得有任何解释性文字。\n"
            "2. JSON 字段只有这 6 个，一个不少：\n"
            '   {"diagnosis": "为什么坏，一句话",\n'
            '    "file": "workspace 相对路径（要改的那个文件）",\n'
            '    "old_text": "要替换的原文（必须能在文件里逐字找到，从上面的文件内容里复制，不要凭记忆编）",\n'
            '    "new_text": "替换成的新文本",\n'
            '    "commands": ["验证命令（可选，最多 2 条，不得安装软件/不得访问网络）"],\n'
            '    "explanation": "为什么这样改能修好"}\n'
            "3. old_text 必须逐字来自文件内容（含缩进/空格），改用编辑器思维：在片段里找到出错那一行原样抄进 old_text。\n"
            "4. 不许输出 ```` ``` ```` 围栏、不许输出 markdown、不许在 JSON 外加一句“以下是建议”。\n"
            "5. 无法给出可执行补丁时：file 留空字符串，diagnosis 写清用户需要做什么。\n"
            "\n"
            "硬性限制：一次只改一个文件；不得改 python/、electron/、ui/ 下的系统代码；"
            "不得改 .env 或任何 config*.json；不得删除数据；不得安装依赖；不得发起网络请求。"
            "如果判断需要上述动作，输出 file 为空字符串并在 diagnosis 里说明需要用户做什么。\n\n"
            "--- 病历 ---\n%s\n\n--- 相关代码/配置片段 ---\n%s\n\n"
            % (json.dumps(d, ensure_ascii=False)[:6000], code_snippets))

    def _retry_prompt(self, facts: "et.ErrorFacts", raw_output: str,
                      problem: str) -> str:
        """第一次没拿到可执行补丁后的重试提示：带上原样输出 + 只输出 JSON。"""
        return (
            "你上一轮的输出没有被接受，原因：%s\n"
            "你上一轮的原样输出（开头 %d 字符）：\n%s\n"
            "\n"
            "重试。只输出 JSON —— 不带任何解释、不带 markdown 围栏、不带前后缀。"
            "字段与契约同上一轮：{diagnosis,file,old_text,new_text,commands,explanation}；"
            "old_text 必须逐字来自文件内容；"
            "无法给出可执行补丁就输出 file 为空字符串并在 diagnosis 里说明。\n"
            "\n"
            "--- 病历 ---\n%s\n\n--- 相关代码/配置片段 ---\n%s\n"
            % (problem, RETRY_SNIPPET, _clip(raw_output, RETRY_SNIPPET),
               json.dumps(facts.to_dict(), ensure_ascii=False)[:6000],
               self._related_code_snippets(facts)))

    def _related_code_snippets(self, facts: "et.ErrorFacts") -> str:
        """病历指向的文件（工具目标/traceback 里的文件）各取片段。"""
        candidates = []
        ctx = facts.context or {}
        args = ctx.get("argsSummary")
        if isinstance(args, str):
            # 从参数里抠路径（read_file 等场景）
            m = re.findall(r"[A-Za-z]:[\\\\/][\w\\\\/ .-]+|/[\w\- ./]+", args)
            candidates.extend(m[:2])
        if facts.traceback:
            m = re.findall(r'File "([^"]+)"', facts.traceback)
            candidates.extend(m[:3])
        out_lines = []
        for cand in candidates[:3]:
            p = cand if os.path.isabs(cand) else os.path.join(self.workspace_root, cand)
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                out_lines.append("### %s（前 %d 字符）\n%s"
                                 % (cand, SNIPPET_MAX, text[:SNIPPET_MAX]))
            except OSError:
                continue
        if not out_lines:
            return "（无可用片段：按诊断给出你最可能的判断）"
        return "\n\n".join(out_lines)

    def _parse_action(self, raw: str) -> HealAction | None:
        """模型输出 → HealAction。宽容解析：剥 markdown 围栏、截取首个 JSON 对象。"""
        if not raw:
            return None
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[\w]*\s*|\s*```$", "", text).strip()
        start = text.find("{")
        if start < 0:
            return None
        # 从第一个 { 开始找配对的 }
        depth = 0
        end = -1
        in_str = False
        esc = False
        for i, ch in enumerate(text[start:], start=start):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            return None
        try:
            obj = json.loads(text[start:end + 1])
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        action = HealAction(
            diagnosis=str(obj.get("diagnosis") or "")[:500],
            file=str(obj.get("file") or "").strip()[:300],
            old_text=str(obj.get("old_text") or ""),
            new_text=str(obj.get("new_text") or ""),
            commands=[str(c) for c in (obj.get("commands") or [])
                      if str(c).strip()][:2],
            explanation=str(obj.get("explanation") or "")[:500])
        if not action.file:
            return action          # 诊断了但需要用户动作（红线）
        return action

    def _make_sandbox(self) -> str:
        base = os.path.join(self.data_dir, SANDBOX_DIRNAME)
        os.makedirs(base, exist_ok=True)
        path = tempfile.mkdtemp(prefix="sb-", dir=base)
        return path

    def _backup(self, real_path: str) -> str:
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = os.path.join(self.data_dir, "heal-backup", ts)
        os.makedirs(backup_dir, exist_ok=True)
        dst = os.path.join(backup_dir, os.path.basename(real_path))
        shutil.copy2(real_path, dst)
        return dst

    def _emit_lesson(self, out: HealOutcome, facts: "et.ErrorFacts", *,
                     success: bool, note: str) -> None:
        """结论写进每个 lesson sink（现有 _lessons_store 等）。"""
        lesson = {
            "tool": "self_heal", "target": "%s/%s" % (facts.kind, facts.code),
            "code": "SELF_HEAL_%s" % ("OK" if success else "FAIL"),
            "message": note, "count": 1,
            "first": out.ts, "last": out.ts,
            "remedy": note,   # 给下次同类错误先查经验的字段（heal_context 用）
        }
        out.lesson = lesson
        for sink in self.lesson_sinks:
            try:
                record = getattr(sink, "record", None)
                if callable(record):
                    record("self_heal", {"kind": facts.kind, "code": facts.code},
                           lesson["code"], lesson["message"])
                    continue
                add = getattr(sink, "add", None)
                if callable(add):
                    add(lesson)
            except Exception:                 # noqa: BLE001 - 经验库坏了不许连坐
                continue

    # -- 历史与预算 -------------------------------------------------------

    def _error_count(self, facts: "et.ErrorFacts") -> int:
        history = _load_json(self.history_path, default=[])
        n = 0
        for rec in history if isinstance(history, list) else []:
            if (isinstance(rec, dict)
                    and rec.get("kind") == facts.kind and rec.get("code") == facts.code):
                n += 1
        return n

    def _record_history(self, out: HealOutcome) -> None:
        history = _load_json(self.history_path, default=[])
        if not isinstance(history, list):
            history = []
        history.append(out.to_dict())
        history = history[-HISTORY_MAX:]
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            _save_json(self.history_path, history)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 默认沙箱验证器（子进程跑命令，可整体替换）
# ---------------------------------------------------------------------------

def _default_verifier(file_path: str, commands: list) -> tuple[bool, str]:
    import subprocess

    import procflags
    if not commands:
        # 无验证命令 → 只做语法检查（py 文件）
        if file_path.endswith(".py"):
            r = subprocess.run(["python", "-m", "py_compile", file_path],
                               capture_output=True, text=True,
                               timeout=VERIFY_TIMEOUT,
                               **procflags.no_window_kwargs())
            return r.returncode == 0, (r.stderr or r.stdout or "syntax OK")
        return True, "no verifier commands; patched file exists"
    cwd = os.path.dirname(file_path) or "."
    logs = []
    for cmd in commands[:2]:
        try:
            r = subprocess.run(["sh", "-c", cmd] if os.name != "nt" else ["cmd", "/c", cmd],
                               cwd=cwd, capture_output=True, text=True,
                               timeout=VERIFY_TIMEOUT,
                               **procflags.no_window_kwargs())
        except subprocess.TimeoutExpired:
            return False, "验证命令超时（%ds）：%s" % (VERIFY_TIMEOUT, cmd)
        logs.append("$ %s\n%s%s" % (cmd, r.stdout or "", r.stderr or ""))
        if r.returncode != 0:
            return False, "\n".join(logs)
    return True, "\n".join(logs) or "all commands passed"


# ---------------------------------------------------------------------------
# 纯文本补丁（原子、精确匹配）
# ---------------------------------------------------------------------------

def _apply_text_patch(path: str, old_text: str, new_text: str) -> bool:
    """逐字替换一次；old_text 找不到返回 False。原子写（临时文件 + rename）。"""
    if not old_text:
        return False
    try:
        with open(path, "r", encoding="utf-8", errors="strict") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return False
    if text.count(old_text) < 1:
        return False
    new = text.replace(old_text, new_text, 1)
    fd, tmp = tempfile.mkstemp(prefix=".heal-", dir=os.path.dirname(path) or ".")
    try:
        import os as _os
        with _os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new)
        _os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return True


# ---------------------------------------------------------------------------
# L1: 检索式自愈（P0-5 顺带交付 —— 下次同类错误先查经验）
# ---------------------------------------------------------------------------

def heal_context(facts: "et.ErrorFacts", lesson_sinks) -> dict | None:
    """命中历史经验则返回 {summary, matchedLessons, suggestedAction}，否则 None。

    与 _lessons_block() 同款防御：全程 try/except，坏了返回 None。
    """
    try:
        matched = []
        for sink in (lesson_sinks or []):
            entries = getattr(sink, "entries", None)
            if not callable(entries):
                continue
            for e in entries() or []:
                if not isinstance(e, dict):
                    continue
                if e.get("tool") != "self_heal":
                    continue
                target = str(e.get("target") or "")
                if target == "%s/%s" % (facts.kind, facts.code):
                    matched.append(e)
        if not matched:
            return None
        best = sorted(matched, key=lambda e: -int(e.get("count", 0) or 0))[0]
        return {
            "summary": "此前同类错误已自愈处理过（%s 次）" % int(best.get("count", 1) or 1),
            "matchedLessons": matched[:3],
            "suggestedAction": str(best.get("remedy") or best.get("message") or ""),
        }
    except Exception:                         # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _clip(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
