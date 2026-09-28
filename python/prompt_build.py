"""System prompt assembly for the agent loop (orchestration layer).

This module owns exactly one job: turn (workspace, clock, skills, tools) into the
system prompt string. It does no I/O, calls no model, and never decides policy.

The shape follows the frozen design in the project's design archive
("desktop-agent-app-design.md", lines 281-285 and 571-586):

    system = base role
           + available skills   (name + description + when_to_use)
           + available tools    (name + one-line description)

Skills are listed only by metadata; the model calls read_skill(name) to pull the
full body. Tool JSON schemas are deliberately NOT duplicated here: they already
travel with every request in the OpenAI `tools` parameter, so repeating them in
the prompt would burn tokens on every turn for no behavioural gain.

Everything is pure and injectable (pass `now` and the lists explicitly) so the
prompt can be asserted byte-for-byte in tests.
"""
from __future__ import annotations

import os
import sys

import datetime as _dt
from typing import Iterable, Protocol, Sequence

# The skill-injection block is frozen by the design doc; keep the wording as is.
SKILLS_HEADER = "## 可用 skills"
# The instigation wording is deliberately firm: the reference agent (Hermes)
# phrases it as MUST load a partially-relevant skill rather than improvise steps,
# and that the
# bias should be toward loading. Soft phrasing ("you may call ...") measurably
# loses to this in practice, so the same bias is used here.
SKILLS_HINT = ("回话前先扫一遍下面的清单。只要跟当前任务沾边，就用 read_skill 读它的完整步骤再照做，"
               "不要凭记忆编流程。拿不准要不要读的时候，倾向于读——多读一份用不上的上下文，"
               "比漏掉关键步骤或踩坑要好。")
SKILLS_NAMES_ONLY_NOTE = ("（skill 数量较多，这里只列名字；需要哪一份直接 read_skill 读全文。）")
# The skill index degrades to names-only once the list gets long (same
# convention as the reference agent's skill index), so the
# rendered block cannot grow without bound as users install skills.
SKILLS_SECTION_CHAR_BUDGET = 2500

IDENTITY_TITLE = "## 身份与硬规（用户设定）"
IDENTITY_PRECEDENCE = ("这里的「- 」条目是硬规，每次回答都要严格遵守；除了内置的工作方式与安全边界之外，"
                       "它们优先于本提示词里的其它描述，但不能覆盖审批与权限边界。")

BASE_ROLE = """你是 CodeAgent，一个跑在用户自己电脑上的桌面助手。你能做什么、不能做什么，不靠这段话承诺——下面「能力与边界」那一节是按本机实际装了的工具生成出来的，以它为准，别声称清单以外的事。

## 工作方式
1. 先看再动。涉及文件内容、目录结构、程序输出这类事实，先用工具看，不许凭猜测下断言。
2. 不知道就按阶梯往上走，不要停在原地，也不要一上来就问人：先看本地（文件、已有资料、历史对话、上一轮的结果）能不能回答；本地说不清就联网查；确实查不到才回头问用户——问的时候讲清你已经试过什么、卡在哪一步。
3. 说要做就必须做。回复里写了「我去查一下」「让我试试」这类话，同一轮里就得带上对应的工具调用；只宣布不调用，这一轮等于白说。本机没这个能力、或者你做不到，就直接说做不到，不要用一句「我去查」敷衍过去。
4. 不许编。查到什么说什么，拿不准就说拿不准；引用外部信息要给出来处（网址或文件名）。你记忆里的东西只能当线索，不能当证据——原文优先于记忆，冲突时以刚查到或刚读到的为准。
5. 证据先落地再出结论：先把工具调用发出去、把事实拿回来，再组织回答；不要先写好一段结论再补调用。
6. 工具报错先读错误信息再决定下一步，不要换个参数原样重试；同一条路连续失败两次就换路（换工具、换地址、换思路），别死磕在一个思路上。
7. 用户说「那个文件」「上次那个」而你不确定指哪个时，要么先列出来给候选，要么直接问，不要自己挑一个动手。
8. 一次能拿到位的别拆成很多次调用；互不依赖的读取可以合并成一次。
9. 大段内容先写进文件，回复里给路径和要点，不要把整篇内容灌进聊天窗口。
10. 回答简短，先给结论再给必要细节，用中文回答。
11. 有现成 skill 讲这件事的，先读它的完整步骤再照做，不要凭记忆编流程。
12. 用户提出「以后都…」「记住…」「每次都…」这类长期要求时，把它存成硬规（存完一句话确认）；用户说不用了就把那条删掉。不要自己发明硬规，也不要拿它存一次性的请求。
13. 一轮活干完后，如果这套做法以后还会复用（要多次调用才跑通的流程、参数顺序有讲究、踩过坑），先问一句「要不要把刚才这套流程存成技能？」，**用户点头了才用 skill_manage 存**；不置可否就当不要，别追着问。名字要说清是什么活，description 写「什么时候该用」。一次性的操作不要存。

## 工具
工具通过函数调用提供，参数结构以调用接口为准。只读、只查的操作直接做，不用先问；删系统目录、格式化、删家目录这类真正危险的操作会被用户审批拦一次，这是设计如此：把你要做什么说清楚，不要试图绕过。"""


#: Capability catalogue: the single source of truth for 「我能干什么」.
#:
#: Each row is (label, tool names that provide it, caveat, absent-caveat). The
#: rendered section is derived from the tools actually registered on this
#: machine, which is the fix for the failure that started this: BASE_ROLE used
#: to claim 「搜索内容」 while the only search tool was the local search_files,
#: so the model believed it could look things up on the internet and then had
#: no tool to call. Never hand-write capability prose again — add a row here.
CAPABILITY_CATALOG: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("读文件内容", ("read_file",), "", ""),
    ("看目录结构", ("list_dir",), "", ""),
    ("在本地文件里搜内容", ("search_files",), "只搜本地文件，搜不了互联网", ""),
    ("写或修改文件", ("write_file", "edit_file"), "任意目录都直接做，不用先问", ""),
    ("删除文件或目录", ("delete_path",), "删了回不来，会被审批拦一次", ""),
    ("执行命令行命令", ("run_shell",), "", ""),
    ("抓取网页", ("web_fetch",), "只能抓你已经知道的确切网址", ""),
    ("联网搜索", ("web_search",), "",
     "没有搜索工具，只能靠 web_fetch 抓已知网址；不知道网址就直说查不到"),
    ("看图片", ("read_image", "vision_analyze"), "仅限模型支持视觉时", ""),
    ("生成图片或视频", ("image_generate", "video_generate"), "", ""),
    ("读 skill 全文", ("read_skill",), "", ""),
    ("列 skill 清单", ("list_skills",), "", ""),
    ("增删改 skill（写回）", ("skill_manage",),
     "批里任何一条失败整批回滚，磁盘不留半截；内置 skill 只读，改不了；存新技能前必须先问用户", ""),
    ("存或删长期硬规", ("save_rule", "remove_rule"), "", ""),
    ("检索历史对话（跨会话）", ("search_conversations",),
     "能查到用户以前问过什么、我说过什么", "看不到历史对话，只能看当前这一轮"),
    ("派子代理并行干活", ("delegate",),
     "一次最多 3 个节点；子代理不能反问用户、也不能再往下派；它们的结果会合成一条消息回来，"
     "所以每个节点的 goal/context 要写全", ""),
)

CAPABILITY_TITLE = "## 能力与边界"
CAPABILITY_NOTE = ("这一节是按本机实际注册的工具现场生成的，和下面「可用工具」是同一份来源。"
                   "清单外的事你就是做不到，用户问到就直接说做不到，别绕、别编、别承诺。")


class _SkillLike(Protocol):
    name: str
    description: str
    when_to_use: str | None


class _ToolLike(Protocol):
    name: str
    description: str


def _first_line(text: str) -> str:
    """Collapse a description to its first non-empty line."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def skills_section(skills: Sequence[_SkillLike]) -> str:
    """Render the frozen `## 可用 skills` block; empty string when no skills.

    Long lists degrade to names only, so a user with a hundred installed skills
    does not pay for a hundred descriptions on every single turn.
    """
    full_rows = []
    name_rows = []
    for skill in skills or []:
        name = str(getattr(skill, "name", "") or "").strip()
        if not name:
            continue
        name_rows.append("- %s" % name)
        desc = _first_line(str(getattr(skill, "description", "") or ""))
        when = str(getattr(skill, "when_to_use", "") or "").strip()
        row = "- %s: %s" % (name, desc)
        if when:
            row += " (when: %s)" % when
        full_rows.append(row)
    if not full_rows:
        return ""
    head = "\n".join([SKILLS_HEADER, SKILLS_HINT])
    full = "\n".join([head] + full_rows)
    if len(full) <= SKILLS_SECTION_CHAR_BUDGET:
        return full
    return "\n".join([SKILLS_HEADER, SKILLS_HINT, SKILLS_NAMES_ONLY_NOTE] + name_rows)


#: Cross-session recall guidance. Injected ONLY when the recall tool is
#: actually registered (same gating convention as the reference agent's
#: SESSION_SEARCH_GUIDANCE) — telling the model to call a tool that is not
#: loaded is worse than saying nothing.
#: 口径：只说跨会话；历史内容一律不进常驻提示词，问一句天气不该付整份历史的 token
#: （用户 2026-09-25 口径）。
HISTORY_GUIDANCE = (
    "用户提到过去对话里的事、或你怀疑有相关的跨会话上下文时，先用 search_conversations "
    "回忆，不要让用户重复一遍；问「今天/昨天/最近问过什么」这类时间性问题传 since"
    "（since='today'/'yesterday'/'7d' 或 ISO 日期），不要拿「今天」当关键词搜 —— "
    "消息正文里没有这个词。也不要说「我这边不存聊天记录」：你能查，工具就是干这个的。"
)

def environment_section(workspace: str, now: str, tz: str, platform: str) -> str:
    """Volatile runtime facts, kept as the LAST section on purpose.

    Two reasons, both from the reference implementations: dsh renders runtime
    context after the first-party guidance, and the clock changes every turn, so
    anything after it can never be reused by a provider doing prefix caching.
    """
    if os.name == "nt":
        shell = "Windows cmd.exe（run_shell 走 cmd /c；没有 ls/head/grep/python3 这类 POSIX 命令）"
    else:
        shell = "/bin/sh -c"
    return "\n".join([
        "## 运行环境",
        "- 你干活时的当前目录：%s" % (workspace or "(未设置)"),
        "- 当前时间：%s（%s）" % (now, tz),
        "- 命令行：%s" % shell,
        "- Python 解释器：%s" % (sys.executable or "(未知)"),
        "- 运行平台：%s" % (platform or ""),
    ])


def _tool_names(tools) -> set[str]:
    """工具名集合：Tool 对象与 schema dict 两种形状都认。

    调用方两种都有（sidecar 传 tools.list_tools() 的对象，测试与前端传 schemas），
    所以这里不能只认一种 —— 之前只按对象取，gating 就永远不命中。
    """
    names = set()
    for tool in tools or []:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name and isinstance(tool, dict):
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
            name = str(fn.get("name") or tool.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def capability_section(tools: Sequence[_ToolLike]) -> str:
    """Render 「我能干什么 / 我不能干什么」 from the registered tools.

    Both halves matter. The positive list stops the model from hedging about
    things it can do; the negative list stops it from promising things it cannot
    — which is the exact failure this section exists for (a model insisting it
    would go search the web with no web tool registered).
    """
    present = set()
    for tool in tools or []:
        name = str(getattr(tool, "name", "") or "").strip()
        if name:
            present.add(name)
    if not present:
        return ""
    can, cannot = [], []
    for label, owners, caveat, absent_caveat in CAPABILITY_CATALOG:
        hit = [name for name in owners if name in present]
        if hit:
            row = "- %s：%s" % (label, "、".join(hit))
            if caveat:
                row += "（%s）" % caveat
            can.append(row)
            continue
        missing = label
        if absent_caveat and ("web_fetch" in present):
            missing += "（%s）" % absent_caveat
        cannot.append(missing)
    lines = [CAPABILITY_TITLE, CAPABILITY_NOTE, "", "你有的能力："]
    lines.extend(can)
    lines.append("")
    if cannot:
        lines.append("本机没有的能力（问到就直接说做不到）：")
        lines.append("- " + "、".join(cannot))
    else:
        lines.append("本机没有的能力：无（清单里列出的都能做）。")
    return "\n".join(lines)


def tools_section(tools: Sequence[_ToolLike]) -> str:
    """Render the compact tool list; empty string when there are no tools."""
    rows = []
    for tool in tools or []:
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        rows.append("- %s: %s" % (name, _first_line(str(getattr(tool, "description", "") or ""))))
    if not rows:
        return ""
    return "\n".join(["## 可用工具"] + rows)


def identity_section(identity: str | None) -> str:
    """The user's identity file: persona prose plus hard rules as bullets.

    One section, because the file is one thing: what the agent is and what it
    must always do. Absent file -> empty string (the built-in role is the
    fallback), so a machine without the file behaves exactly as before.
    """
    body = (identity or "").strip()
    if not body:
        return ""
    return "\n".join([IDENTITY_TITLE, IDENTITY_PRECEDENCE, body])


def instructions_section(instructions: str | None) -> str:
    """Project rules read from the working directory (AGENTS.md / WORKBUDDY.md).

    Stable between turns unless the user edits the file, so it stays ahead of
    the volatile runtime section and keeps the cached prefix reusable.
    """
    body = (instructions or "").strip()
    if not body:
        return ""
    return "\n".join(["## 项目指令（来自当前目录）", body])


def memory_section(memory: str | None) -> str:
    """Recalled memory for this turn, already rendered and budgeted.

    Volatile by nature (it changes with every question), so it belongs at the
    very end: everything above it stays byte-stable and cacheable.
    """
    body = (memory or "").strip()
    return body if body else ""


#: Soft depth wording -- used ONLY when the vendor has no depth parameter.
#: It is a nudge (how much effort to spend before answering), not a knob, and
#: the UI labels it as 「软引导」 so nobody mistakes it for a vendor parameter.
THINKING_DEPTH_HINTS: dict[str, str] = {
    "low": "思考程度：轻。先给判断，只有确实卡住才展开推演。",
    "medium": "思考程度：中。按需要展开推演，够用就收，别把时间花在重复验算上。",
    "high": "思考程度：深。先充分展开推演与自查（把可能的坑列一遍再选路），再给结论。",
}

THINKING_DEPTH_TITLE = "## 思考程度"


def lessons_section(lessons: str | None) -> str:
    """工具失败经验（tool_lessons 生成）。已经自带小标题，这里只做兜底判断。"""
    text = (lessons or "").strip()
    if not text:
        return ""
    if not text.startswith("#"):
        text = "## 经验（过去工具失败踩过的坑，别再照做）\n" + text
    return text


def thinking_depth_section(depth: str | None) -> str:
    """One-line soft depth hint; empty when the level is unknown/absent."""
    hint = THINKING_DEPTH_HINTS.get(str(depth or "").strip())
    if not hint:
        return ""
    return "\n".join([THINKING_DEPTH_TITLE, hint])


def build(
    *,
    workspace: str,
    skills: Iterable[_SkillLike] | None = None,
    tools: Iterable[_ToolLike] | None = None,
    now: _dt.datetime | None = None,
    platform: str | None = None,
    identity: str | None = None,
    instructions: str | None = None,
    memory: str | None = None,
    thinking_depth: str | None = None,
    thinking_depth_is_soft: bool = False,
    lessons: str | None = None,
) -> str:
    """Assemble the full system prompt. Deterministic when `now` is passed.

    ``thinking_depth`` (low/medium/high) is the user's depth choice. When the
    vendor has no depth parameter (``thinking_depth_is_soft``), the only lever
    left is wording, so a one-line hint goes into the prompt -- and it is
    advertised as a soft hint in the UI, never as a hard knob.
    """
    import platform as _platform

    stamp = now or _dt.datetime.now()
    if stamp.tzinfo is not None:
        tz = stamp.astimezone().strftime("%Z")
    else:
        tz = _dt.datetime.now().astimezone().strftime("%Z")
    locale_now = stamp.strftime("%Y-%m-%d %H:%M:%S")
    weekday = "星期" + "一二三四五六日"[stamp.weekday()]

    parts = [BASE_ROLE]

    # 跨会话引导：只有检索工具真装了才说（参照 Hermes Agent 的 gating 设计）
    if "search_conversations" in _tool_names(tools):
        parts.append(HISTORY_GUIDANCE)

    if thinking_depth and thinking_depth_is_soft:
        block = thinking_depth_section(thinking_depth)
        if block:
            parts.append(block)

    block = identity_section(identity)
    if block:
        parts.append(block)
    block = skills_section(list(skills or []))
    if block:
        parts.append(block)
    block = capability_section(list(tools or []))
    if block:
        parts.append(block)
    block = tools_section(list(tools or []))
    if block:
        parts.append(block)
    block = instructions_section(instructions)
    if block:
        parts.append(block)
    block = memory_section(memory)
    if block:
        parts.append(block)
    block = lessons_section(lessons)
    if block:
        parts.append(block)
    parts.append(environment_section(
        workspace=workspace,
        now="%s %s" % (locale_now, weekday),
        tz="Asia/Shanghai",
        platform=platform or _platform.system(),
    ))
    return "\n\n".join(parts)


def build_for_loop(params: dict, *, tools: Iterable[_ToolLike] | None = None,
                   skills: Iterable[_SkillLike] | None = None,
                   now: _dt.datetime | None = None,
                   identity: str | None = None,
                   instructions: str | None = None,
                   memory: str | None = None) -> str:
    """Convenience wrapper for the sidecar: pull what it needs out of chat params.

    `identity` / `instructions` are read from disk by the caller (agent_files),
    so this stays free of I/O and stays deterministic for tests.
    """
    return build(
        workspace=str(params.get("workspaceRoot") or params.get("workspace_root") or ""),
        skills=skills,
        tools=tools,
        now=now,
        identity=identity,
        instructions=instructions,
        memory=memory,
        thinking_depth=params.get("thinkingDepth"),
        thinking_depth_is_soft=bool(params.get("thinkingDepthIsSoft")),
    )