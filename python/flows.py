"""流程（flow）引擎：把"一问一答往下走"的脚本做成可编辑的定义文件。

为什么要它（2026-10-09 用户口径）：项目会话一建好就该像助手真在跟你聊——先问
「新建项目还是接入已有项目」，**不同选择走不同的路**，问到齐了再把结果整理成
一条开场任务。说白了就是一个大的流程 skill：定义写在
`resources/flows/<id>.json` 里，改流程不用改代码。

**异常值是这套东西的重点**（用户特别点出来的）——人不会老老实实只点你给的
按钮，所以每种"没按剧本走"都有明确出口，不许把用户卡在半路：

  * 选项步骤收到**对不上任何选项**的话 → 不算错，判 off_script：流程当场收尾，
    把用户原话留在会话里，交给模型正常接着聊（模型能看到前面那几轮问答）；
  * 输入步骤收到**明显不是我们要的东西**（如要目录却给了别的）→ 判 retry：
    不前进、不写库，回一句说明 + 「重填 / 照样继续」两个出口；
  * 用户在流程候着的时候**直接发消息**（不是点按钮）→ 由界面结束流程，走正常
    对话。引擎这边不拦。

纯逻辑、不碰网络不碰库：读写会话是 sidecar 的事，这里只管"下一个该问什么"。
"""
from __future__ import annotations

import json
import os

#: 流程定义目录（仓内；缺文件不是错误，只是没流程可用）。
DEFAULT_FLOWS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", "flows")

#: 目录存在性检查的步骤名（JSON 里的 step.check）。
CHECK_DIR_EXISTS = "dir_exists"


class FlowError(ValueError):
    """流程定义坏了 / 调用姿势不对。code 走 RPC 的错误码，界面直接可读。"""

    def __init__(self, message: str, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------
# 读取与校验
# --------------------------------------------------------------------------

def _validate(flow: dict, where: str) -> dict:
    """定义自检：坏流程宁可加载时炸，也不要跑到一半卡住用户。"""
    if not isinstance(flow, dict):
        raise FlowError("流程定义必须是 JSON 对象：%s" % where)
    fid = flow.get("id")
    if not isinstance(fid, str) or not fid.strip():
        raise FlowError("流程定义缺 id：%s" % where)
    steps = flow.get("steps")
    if not isinstance(steps, list) or not steps:
        raise FlowError("流程 %s 没有 steps" % fid)
    by_id: dict[str, dict] = {}
    for step in steps:
        if not isinstance(step, dict):
            raise FlowError("流程 %s 的 step 必须是对象" % fid)
        sid = step.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise FlowError("流程 %s 有 step 缺 id" % fid)
        if sid in by_id:
            raise FlowError("流程 %s 的 step id 重复：%s" % (fid, sid))
        if not isinstance(step.get("say"), str) or not step["say"].strip():
            raise FlowError("流程 %s 的 step %s 没写 say（要问的话）" % (fid, sid))
        opts = step.get("options")
        inp = step.get("input")
        if opts is not None and inp is not None:
            raise FlowError("流程 %s 的 step %s 不能同时有 options 和 input" % (fid, sid))
        if opts is not None and isinstance(step.get("goto"), str):
            # 有选项时"下一步"由被选中的那项决定，外层再写一个 goto 只会是死配置。
            raise FlowError("流程 %s 的 step %s 有 options 就不该再写 goto（按选项各自的 goto 走）"
                            % (fid, sid))
        if not step.get("end") and opts is None and inp is None and not isinstance(step.get("goto"), str):
            raise FlowError("流程 %s 的 step %s 既没 options/input 也没 goto，会走到死路" % (fid, sid))
        if opts is not None:
            if not isinstance(opts, list) or not opts:
                raise FlowError("流程 %s 的 step %s 的 options 得是非空数组" % (fid, sid))
            for o in opts:
                if not isinstance(o, dict) or not isinstance(o.get("label"), str):
                    raise FlowError("流程 %s 的 step %s 有 option 缺 label" % (fid, sid))
                if not isinstance(o.get("value"), str) or not o["value"].strip():
                    raise FlowError("流程 %s 的 step %s 有 option 缺 value" % (fid, sid))
                if not isinstance(o.get("goto"), str):
                    raise FlowError("流程 %s 的 step %s 有 option 缺 goto" % (fid, sid))
        by_id[sid] = step
    if "greeting" in flow and not isinstance(flow["greeting"], str):
        raise FlowError("流程 %s 的 greeting 得是字符串（开场白），别写成别的" % fid)
    if isinstance(flow.get("greeting"), str) and not flow["greeting"].strip():
        raise FlowError("流程 %s 的 greeting 是空的：要么别写这个字段，要么写句话" % fid)
    start = flow.get("start")
    if not isinstance(start, str) or start not in by_id:
        raise FlowError("流程 %s 的 start 指向不存在的 step：%r" % (fid, start))
    # goto 全部要落地（否则用户走到一半会掉下去）
    for step in steps:
        targets = []
        if isinstance(step.get("options"), list):
            targets += [o.get("goto") for o in step["options"]]
        if isinstance(step.get("goto"), str):
            targets.append(step["goto"])
        for t in targets:
            if t not in by_id:
                raise FlowError("流程 %s 的 step %s 指向不存在的 step：%r"
                                % (fid, step["id"], t))
    out = dict(flow)
    out["_by_id"] = by_id
    return out


def load_definitions(directory: str | None = None) -> dict[str, dict]:
    """读目录下所有 *.json；单个文件坏了只跳过它，不连累别的流程。

    返回 {flow_id: flow}；目录不存在返回空表（没有流程 ≠ 报错）。
    """
    root = directory or DEFAULT_FLOWS_DIR
    out: dict[str, dict] = {}
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(root, name)
        try:
            with open(path, encoding="utf-8") as f:
                flow = _validate(json.load(f), path)
        except Exception:                       # 坏文件跳过：一个流程不该毁掉全部
            continue
        out[flow["id"]] = flow
    return out


def get_flow(flow_id: str, directory: str | None = None) -> dict:
    """取一个流程；没有就 FlowError（RPC 层翻成 BAD_REQUEST）。"""
    flows = load_definitions(directory)
    flow = flows.get(flow_id)
    if flow is None:
        raise FlowError("没有这个流程：%r" % flow_id)
    return flow


def public_definition(flow: dict) -> dict:
    """给界面的定义（去掉内部字段）。"""
    return {k: v for k, v in flow.items() if not k.startswith("_")}


def step_of(flow: dict, step_id: str) -> dict:
    step = (flow.get("_by_id") or {}).get(step_id)
    if step is None:
        raise FlowError("流程 %s 没有这个步骤：%r" % (flow.get("id"), step_id))
    return step


# --------------------------------------------------------------------------
# 走一步
# --------------------------------------------------------------------------

def _looks_like_path(text: str) -> bool:
    t = (text or "").strip().strip('"').strip("'")
    if not t or len(t) > 500:
        return False
    if t.startswith("~") or t.startswith("/") or t.startswith("\\\\"):
        return True
    return (len(t) > 2 and t[1] == ":" and t[2] in ("\\", "/"))     # D:\...


def _greeting_of(flow: dict) -> str:
    """流程定义里的开场白；没写就返回空串（老流程照样能用）。"""
    g = flow.get("greeting")
    return g.strip() if isinstance(g, str) else ""


def _check_answer(step: dict, answer: str) -> str:
    """输入步骤的"这答的是不是我要的" —— 返回空串表示没问题，否则是说明。

    只做**明显跑偏**的判定，不追求聪明：要目录却给了一句大白话，就别假装
    收下了（收了后面全错），回一句请它重填或照样继续。
    """
    if step.get("check") != CHECK_DIR_EXISTS:
        return ""
    if _looks_like_path(answer):
        return ""
    return "这不像个目录路径，复制一个更稳。"


def advance(flow: dict, *, step_id: str | None, answer: str | None,
            answers: dict | None = None, force: bool = False) -> dict:
    """算出下一步该干嘛。**不写库、不发消息**，纯函数（方便单测）。

    ``force=True``（界面上那个「照样继续」/「跳过」按钮）：明知这答的不像我们要
    的东西，用户坚持，那就收下、别拦着 —— 校验是用来提醒的，不是用来卡人的。

    返回四种形状之一：

      {"kind": "ask",   "step": <step>, "answers": {...}}      # 接着问
      {"kind": "retry", "step": <step>, "warn": "...",         # 这答的不对劲
       "options": [{"label": "重填"}, {"label": "照样继续"}], "answers": {...}}
      {"kind": "done",  "step": <终步>, "prompt": "...", "answers": {...}}
      {"kind": "off_script", "answers": {...}, "reason": "..."}  # 用户没按剧本走
    """
    answers = dict(answers or {})

    # 第一步：没有 step_id 时从 start 开始（这正是"项目会话一建好就说话"）。
    if not step_id:
        first = step_of(flow, flow["start"])
        # 起流程时带一句开场白：一上来直接抛问题太突兀。跟 say 一样是
        # 定义里可改的文案，界面/sidecar 只管把它落进记录。
        return {"kind": "ask", "step": first, "answers": answers,
                "greeting": _greeting_of(flow)}

    step = step_of(flow, step_id)
    options = step.get("options") if isinstance(step.get("options"), list) else None
    inp = step.get("input") if isinstance(step.get("input"), dict) else None

    # 用户直接说话、没点按钮（界面在流程候着时把他的话原样送过来，或送到这里）。
    if options is not None:
        hit = None
        for o in options:
            if answer is not None and answer.strip() == o["value"].strip():
                hit = o
                break
            if answer is not None and answer.strip() == o["label"].strip():
                hit = o
                break
        if hit is None:
            return {"kind": "off_script", "answers": answers,
                    "reason": "用户没在我们给的选项里选，说了别的"}
        nxt = step_of(flow, hit["goto"])
    elif inp is not None:
        text = (answer or "").strip()
        save_as = inp.get("save_as") or step_id
        optional = bool(inp.get("optional"))
        if not text and not optional and not force:
            return {"kind": "retry", "step": step, "warn": "这条还是说一句吧。",
                    "options": [{"label": "我重说", "role": "redo"},
                                {"label": "先不管这个", "role": "force"}],
                    "answers": answers}
        warn = _check_answer(step, text) if (text and not force) else ""
        if warn:
            return {"kind": "retry", "step": step, "warn": warn,
                    "options": [{"label": "我重说", "role": "redo"},
                                {"label": "就按这个来", "role": "force"}],
                    "answers": answers}
        if text:
            answers[save_as] = text
        nxt = step_of(flow, step["goto"])
    else:
        # 既没选项也没输入框：这步只是个"说一句"，答什么都是过（容错）。
        nxt = step_of(flow, step["goto"])

    if nxt.get("end"):
        return {"kind": "done", "step": nxt, "answers": answers,
                "prompt": compose_prompt(flow, answers)}
    return {"kind": "ask", "step": nxt, "answers": answers}


def compose_prompt(flow: dict, answers: dict, *, extra: str = "") -> str:
    """把收集到的东西拼成一条开场任务（放进输入框，用户点头再发）。

    顺序按用户实际被问到的先后走（answers 是 dict，这里按流程定义里的
    save_as 首次出现顺序排），读起来像人话，不像表单 dump。
    """
    order: list[str] = []
    for step in flow.get("steps") or []:
        inp = step.get("input") if isinstance(step.get("input"), dict) else None
        if inp and inp.get("save_as") and inp["save_as"] not in order:
            order.append(inp["save_as"])
    lines = ["项目会话，先按这个来："]
    for key in order:
        val = (answers.get(key) or "").strip()
        if val:
            lines.append("- %s：%s" % (key, val))
    if extra:
        lines.append("- 其他：%s" % extra.strip())
    lines.append("")
    lines.append("缺啥问我，别猜。")
    return "\n".join(lines)


def off_script_handoff(flow: dict, answers: dict, *, last_question: str = "") -> str:
    """用户跑偏时补的那句（让模型接手，而不是让流程愣在那儿）。

    不假装流程走完了：把"已经问出来的东西"交出去，剩下的交给正常对话。
    """
    parts = []
    if answers:
        parts.append("已知：")
        for key, val in answers.items():
            if (val or "").strip():
                parts.append("- %s：%s" % (key, val.strip()))
    if last_question:
        parts.append("（刚在问「%s」，你先说了别的）" % last_question.strip())
    return "\n".join(parts)