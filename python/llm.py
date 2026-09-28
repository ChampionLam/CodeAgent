"""模型客户端：读配置 + 真调 OpenAI 兼容端点做流式对话。

设计参考 dsh 的 llm 层：取消不硬杀（调用方停止等待，让 HTTP 流自己收干净）；
错误按 code 分类型，上层按 code 分派。

MiniMax 的坑（已实测）：响应体外层多一个 base_resp，HTTP 200 也可能是错误；
必须用 urllib，不能用 curl（住宅 CN IP 上 curl 约 50% 直接 HTTP 000）。

安全：key 只从环境变量读，绝不写文件、绝不进日志（api_key 带 repr=False）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator


class LlmError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ConfigError(LlmError):
    def __init__(self, message: str) -> None:
        super().__init__("CONFIG_ERROR", message)


class Cancelled(LlmError):
    def __init__(self, message: str = "generation cancelled") -> None:
        super().__init__("CANCELLED", message)


@dataclass
class ModelConfig:
    base_url: str
    model: str
    api_key_env: str
    max_tokens: int = 4096
    timeout_seconds: int = 180
    api_key: str = field(default="", repr=False)
    # 契约 §1.1 生成参数四字段 + provider（§6 映射键）。
    # 全部默认值 = 旧行为：请求体里不出现 temperature / top_p / 任何思考键。
    provider: str | None = None
    context_window: int = 128000
    thinking: bool | None = None
    # 思考深度档位（"low" | "medium" | "high" | None）。None = 不干预，
    # 与 thinking=None 同义：请求体里不出现任何深度键。
    thinking_depth: str | None = None
    # 这条线路/厂商吃不吃思考参数。False = 一律不往请求体塞思考键：
    # hg（百炼 MAAS）上的 glm-5.3 实测 enable_thinking=False 直接 400
    # 「restricted to True」，thinking={type:disabled} 同样 400。
    thinking_supported: bool = True
    temperature: float | None = None
    top_p: float | None = None

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    @property
    def contextWindow(self) -> int:
        """camelCase 别名：context_mechanism 用 getattr(entry, "contextWindow") 读。"""
        return self.context_window

    @classmethod
    def from_resolved(cls, info: dict) -> "ModelConfig":
        """用 modelconfig.resolve_model 的返回 dict 组装（sidecar 接线用）。

        兼容缺字段的 info：缺失走默认值，行为与旧 ModelConfig 一致。
        """
        cfg = cls(
            base_url=str(info.get("base_url") or ""),
            model=str(info.get("model") or ""),
            api_key_env=str(info.get("api_key_env") or ""),
            max_tokens=int(info.get("max_tokens") or 4096),
            timeout_seconds=int(info.get("timeout_seconds") or 180),
            provider=info.get("provider"),
            thinking_supported=bool(info.get("thinking_supported", True)),
            context_window=int(info.get("contextWindow") or 128000),
            thinking=info.get("thinking") if isinstance(info.get("thinking"), bool) else None,
            thinking_depth=(str(info.get("thinkingDepth")).strip()
                            if info.get("thinkingDepth") else None),
            temperature=info.get("temperature"),
            top_p=info.get("topP"),
        )
        cfg.api_key = str(info.get("api_key") or "")
        return cfg


def load_config(root: str | None = None) -> ModelConfig:
    """读 <root>/config.json，缺则回落 config.example.json。key 只从环境变量取。"""
    if root is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "config.json")
    if not os.path.exists(path):
        path = os.path.join(root, "config.example.json")
    if not os.path.exists(path):
        raise ConfigError("找不到配置文件: " + path)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError("配置不是合法 JSON: %s" % e) from e

    m = raw.get("model") or {}
    for k in ("baseUrl", "model", "apiKeyEnv"):
        if not m.get(k):
            raise ConfigError("配置缺字段 model." + k)

    cfg = ModelConfig(
        base_url=str(m["baseUrl"]),
        model=str(m["model"]),
        api_key_env=str(m["apiKeyEnv"]),
        max_tokens=int(m.get("maxTokens", 4096)),
        timeout_seconds=int(m.get("timeoutSeconds", 180)),
    )
    key = os.environ.get(cfg.api_key_env, "").strip()
    if not key:
        raise ConfigError("环境变量 %s 未设置（key 只从环境变量读）" % cfg.api_key_env)
    cfg.api_key = key
    return cfg


# ---------------------------------------------------------------------------
# thinking 参数映射（契约 §6，冻结表）
# ---------------------------------------------------------------------------

# 未适配（不传任何思考键）的 provider 集合：ollama-local、custom 及一切
# 未列出的值。宁可少传不可错传（400 比不干预更糟）。
THINKING_UNADAPTED_PROVIDERS: frozenset[str] = frozenset({
    "ollama-local", "custom", "",
})

#: provider -> (参数名, thinking=True 的值, thinking=False 的值)。
#: 契约 §6 映射表；thinking=None 时任何一家都不传任何思考相关键。
THINKING_PARAM_MAP: dict[str, tuple[str, Any, Any]] = {
    # minimax-cn / minimax-intl: thinking 对象 {"type": adaptive|disabled}
    "minimax-cn": ("thinking", {"type": "adaptive"}, {"type": "disabled"}),
    "minimax-intl": ("thinking", {"type": "adaptive"}, {"type": "disabled"}),
    # dashscope-bailian / siliconflow: enable_thinking 布尔
    "dashscope-bailian": ("enable_thinking", True, False),
    "siliconflow": ("enable_thinking", True, False),
    # deepseek / moonshot / zhipu: thinking 对象 {"type": enabled|disabled}
    "deepseek": ("thinking", {"type": "enabled"}, {"type": "disabled"}),
    "moonshot": ("thinking", {"type": "enabled"}, {"type": "disabled"}),
    "zhipu": ("thinking", {"type": "enabled"}, {"type": "disabled"}),
    # openai: reasoning_effort 档位（无布尔开关，"开"= high 是本项目自定选择）
    "openai": ("reasoning_effort", "high", "none"),
}


def thinking_params(provider: str | None, thinking: bool | None) -> dict[str, Any]:
    """纯函数：按契约 §6 把 (provider, thinking) 映射成请求体参数片段。

    返回一个可 dict.update 的片段：
      * thinking 为 None     -> {}（任何一家都不传任何思考相关键，回归旧行为）；
      * provider 已适配      -> {参数名: 对应值}；
      * provider 未适配      -> {}（ollama-local / custom / 未列值 / None / 空）。

    会不会「未适配」由 is_thinking_adapted(provider) 单独判断，发送侧用它
    打日志/事件标注，这里只管参数本身。
    """
    if thinking is None:
        return {}
    key = str(provider or "").strip()
    mapping = THINKING_PARAM_MAP.get(key)
    if mapping is None:
        return {}
    name, on_value, off_value = mapping
    return {name: on_value if thinking else off_value}


def is_thinking_adapted(provider: str | None) -> bool:
    """该 provider 是否在契约 §6 映射表里（前端灰字提示与后端日志共用）。"""
    key = str(provider or "").strip()
    return key in THINKING_PARAM_MAP


# ---------------------------------------------------------------------------
# thinking 深度（契约 §6 的「深度」列）
# ---------------------------------------------------------------------------

#: 本项目的统一深度档位（三档，前端下拉共用）。
THINKING_DEPTH_LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: provider -> (参数名, {本项目档位: 厂商取值})
#: 只列厂商文档里真有的深度参数。文档里没有深度参数的厂商（MiniMax 官方
#: OpenAI 兼容文档的 thinking 只有 adaptive / disabled，且不接受 enabled +
#: budget_tokens，实测 status 2013）不在此表 —— 请求体里就不出现任何深度键，
#: 宁可少传不可错传；这类厂商的「深度」走提示词软引导（prompt_build）。
THINKING_DEPTH_MAP: dict[str, tuple[str, dict[str, Any]]] = {
    # OpenAI Chat Completions 参考：reasoning_effort 支持
    # none|minimal|low|medium|high|xhigh|max（developers.openai.com/api/reference）
    "openai": ("reasoning_effort", {"low": "low", "medium": "medium", "high": "high"}),
}


def thinking_depth_params(
    provider: str | None,
    depth: str | None,
    thinking: bool | None = None,
) -> dict[str, Any]:
    """纯函数：把「深度档位」映射成请求体参数片段。

    规则：
      * depth 为空 / None       -> {}（不传任何深度键，保持旧行为）；
      * thinking 显式为 False   -> {}（关掉思考时深度没有意义，也不传）；
      * provider 有深度映射     -> {参数名: 对应取值}；
      * provider 无深度映射     -> {}（该厂商没有这个参数，交给软引导）。
    """
    level = str(depth or "").strip()
    if not level or thinking is False:
        return {}
    entry = THINKING_DEPTH_MAP.get(str(provider or "").strip())
    if entry is None:
        return {}
    name, levels = entry
    value = levels.get(level)
    return {name: value} if value is not None else {}


def is_thinking_depth_adapted(provider: str | None) -> bool:
    """该 provider 是否有厂商级深度参数（前端用它决定下拉框是否给硬档位）。"""
    return str(provider or "").strip() in THINKING_DEPTH_MAP


def thinking_depth_levels(provider: str | None) -> list[str]:
    """该 provider 支持的硬档位列表；无硬参数返回空表（前端转软档位）。"""
    entry = THINKING_DEPTH_MAP.get(str(provider or "").strip())
    if entry is None:
        return []
    _, levels = entry
    return [lv for lv in THINKING_DEPTH_LEVELS if lv in levels]


#: 目录 depth.param 是给人看的散文（"reasoning_effort: low/high/max（默认 max）"），
#: 这里从中抠出请求体里真用的参数名；认不出来就不当硬参数用。
_DEPTH_PARAM_NAMES = ("reasoning_effort", "thinking_budget", "max_thinking_tokens",
                      "thinking", "reasoning")


def _depth_param_name(prose: Any) -> str | None:
    text = str(prose or "").strip()
    if not text:
        return None
    head = text.split(":", 1)[0].split("：", 1)[0].strip()
    for name in _DEPTH_PARAM_NAMES:
        if head == name or head.startswith(name + " ") or head.startswith(name + "（"):
            return name
    return None


def _json_fragment(raw: Any) -> dict[str, Any] | None:
    """目录里 thinking.on / off 存的是请求体片段（JSON 字符串），逐字照发。"""
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return dict(val) if isinstance(val, dict) else None


def model_thinking_profile(provider: str | None, model: str | None = None) -> dict:
    """这条模型怎么发思考参数：**目录优先，§6 provider 表兜底**。

    返回 {on, off, depth_param, levels, source}：
      * on          —— 开思考要发的请求体片段（None = 不知道该发什么）；
      * off         —— 关思考要发的片段；**None = 厂商不支持关**（目录里 off=null），
                       这种线路绝不能发假值，发了就是 400（智谱/百炼 MAAS 实测）；
      * depth_param —— 厂商级深度参数名（reasoning_effort 之类），None = 没有；
      * levels      —— 该参数真吃的档位（目录里的 low/high/max …）；
      * source      —— "catalog" / "provider-map"，便于日志和排障。

    为什么目录优先：目录是**逐模型取证**的（带官方原文引用），provider 表只是按对接
    线路给的经验默认值。同一家模型换个线路，档位/可否定制都可能不同——按 provider
    猜就会出现「明明有 low/high/max，界面却当成不能调」这种错（2026-09-25）。
    """
    key = str(provider or "").strip()
    if model:
        try:
            import model_catalog  # 同目录模块：只读目录，不反向依赖 llm
            ent = model_catalog.by_model_id(str(model), prefer_provider=key or None)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("[llm] catalog lookup failed: %r\n" % (exc,))
            ent = None
        if ent:
            th = ent.get("thinking") or {}
            dep = ent.get("depth") or {}
            if th.get("supported"):
                return {
                    "on": _json_fragment(th.get("on")),
                    "off": _json_fragment(th.get("off")),
                    "depth_param": _depth_param_name(dep.get("param")) if dep.get("supported") else None,
                    "levels": [str(x) for x in (dep.get("levels") or [])],
                    "source": "catalog",
                }
    mapping = THINKING_PARAM_MAP.get(key)
    depth_map = THINKING_DEPTH_MAP.get(key)
    return {
        "on": ({mapping[0]: mapping[1]} if mapping else None),
        "off": ({mapping[0]: mapping[2]} if mapping else None),
        "depth_param": (depth_map[0] if depth_map else None),
        "levels": (list(depth_map[1].keys()) if depth_map else []),
        "source": "provider-map",
    }


def thinking_params_for(
    provider: str | None,
    thinking: bool | None,
    thinking_depth: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """目录优先版本的思考参数组装（thinking + 深度一起）。

    规则：
      * thinking 为 None 且没给深度 -> {}（不传任何思考键，回归旧行为）；
      * 厂商不允许关思考（profile["off"] is None，= modelconfig 的
        thinkingCanDisable=False）却要求关 -> **一个 reasoning 相关键都不发**。
        这类模型默认就是思考开启，不发键 = 维持默认。旧逻辑「保持开启并退到最低
        档」会替用户发 on 片段 + 最低 reasoning_effort，但端点对思考参数本身就挑：
        OpenRouter stealth/space-bunny-alpha 实测 reasoning={"enabled":false} 直接
        400（"Reasoning is mandatory for this endpoint and cannot be
        disabled."，2026-09-26 真机二分：裸请求 / tools / reasoning_effort 均 200，
        唯独关思考 400），百炼 MAAS 的 enable_thinking=false 同样 400
        （restricted to True）—— 宁可少传不可错传；
      * 给深度时按该模型真实参数名发，档位不在目录列表里也照发（自定义档位不丢），
        但会打一行日志。
    """
    prof = model_thinking_profile(provider, model)
    out: dict[str, Any] = {}
    level = str(thinking_depth or "").strip()
    if thinking is False:
        off = prof.get("off")
        if off:
            out.update(off)
        else:
            # 模型不允许关思考（thinkingCanDisable=False，目录 off=null）：
            # 一个思考键都不发。这类模型默认开启思考，不发键 = 维持厂商默认。
            # 旧逻辑在这里替用户发 on 片段 + 最低档 reasoning_effort：OpenRouter
            # stealth/space-bunny-alpha 上关思考本身就被端点 400（真机二分
            # 2026-09-26），百炼 MAAS enable_thinking=false 同样 400 —— 少传
            # 不可错传。仍打一行日志，让用户知道「关」没有被照办。
            sys.stderr.write(
                "[llm] thinking=off requested but model %r can not disable "
                "thinking; sending no thinking parameter (vendor default, "
                "source=%s)\n" % (str(model or ""), prof.get("source")))
            return {}
    elif thinking is True:
        on = prof.get("on")
        if on:
            out.update(on)
    if level and level.lower() != "default":
        param = prof.get("depth_param")
        if param:
            levels = [str(x).lower() for x in (prof.get("levels") or [])]
            if levels and level.lower() not in levels:
                sys.stderr.write(
                    "[llm] thinking depth %r not in model %r documented levels %r; "
                    "sending anyway\n" % (level, str(model or ""), levels))
            out[param] = level
    return out



def build_generation_params(
    provider: str | None,
    thinking: bool | None,
    temperature: float | None,
    top_p: float | None,
    thinking_depth: str | None = None,
    thinking_supported: bool = True,
    model: str | None = None,
) -> dict[str, Any]:
    """纯函数：拼装生成参数四字段在请求体里的落点（契约 §6 + §1.1）。

      * temperature / topP：仅当非 null 时出现键，null 时连键都没有；
      * thinking：三态，映射见 thinking_params()；
      * thinking_depth：档位，映射见 thinking_depth_params()（无映射的厂商不传）。

    供 stream_chat 使用，也供单测直接调用（不碰网络、不碰发送逻辑）。
    """
    params: dict[str, Any] = {}
    if temperature is not None:
        params["temperature"] = temperature
    if top_p is not None:
        params["top_p"] = top_p
    if thinking_supported:
        # 目录（模型级）优先：同一家模型换条线路，档位/能否关都可能不同，
        # 按 provider 猜会猜错（2026-09-25 glm-5.3 @ 百炼 MAAS 就是这么错的）。
        params.update(thinking_params_for(provider, thinking, thinking_depth, model))
    # False 的线路一个思考键都不发（发了就是 400，见字段注释）。
    return params


TURN_DEPTH_LEVELS = ("low", "medium", "high")


def _turn_depth_levels(cfg: "ModelConfig") -> tuple[str, ...]:
    """本轮允许的深度档位：**这条模型**目录里写了几档就是几档（含 max），
    目录里没有才退回 TURN_DEPTH_LEVELS。"""
    prof = model_thinking_profile(getattr(cfg, "provider", None),
                                  getattr(cfg, "model", None))
    levels = tuple(str(x).lower() for x in (prof.get("levels") or []))
    return levels or TURN_DEPTH_LEVELS


def apply_turn_overrides(cfg: ModelConfig, overrides: dict | None) -> list[str]:
    """把输入区控制条传来的本轮覆盖项贴到 cfg 上，返回被忽略项的说明。

    界面上的「思考开关 / 思考深度 / 上下文长度」是**会话级**控制：只影响这一轮，
    不写回 config.json，也不改模型的持久配置。所以这里只在内存里改 cfg 的副本
    （调用方传进来的对象），并且对不认识的取值**不静默吞掉**——返回一句人话让
    界面能显示出来，免得用户以为改了其实没生效。

    overrides 允许的键：
      thinking       True / False / None（None = 不干预，不传思考参数）
      thinkingDepth  "low" / "medium" / "high" / None（None 或 "default" = 不干预）
      contextWindow  正整数（token）；越界由 context_mechanism.validate_window 拒绝
    """
    notes: list[str] = []
    if not isinstance(overrides, dict):
        return notes

    # 这条线路不吃思考参数：覆盖项直接丢掉，并明确告诉界面，别让用户
    # 以为改了其实没生效（发了就是 400）。
    if not getattr(cfg, "thinking_supported", True):
        if "thinking" in overrides or "thinkingDepth" in overrides:
            notes.append(
                "这条线路不支持思考参数（厂商侧只能保持思考开启），"
                "这一轮不传思考设置。")
        overrides = {k: v for k, v in overrides.items()
                     if k not in ("thinking", "thinkingDepth")}

    if "thinking" in overrides:
        v = overrides.get("thinking")
        if v is None or isinstance(v, bool):
            cfg.thinking = v
        else:
            notes.append(f"思考开关取值不认识（{v!r}），这一轮不改思考设置。")

    if "thinkingDepth" in overrides:
        d = overrides.get("thinkingDepth")
        if d is None or (isinstance(d, str) and d.strip().lower() in ("", "default")):
            cfg.thinking_depth = None
        elif isinstance(d, str) and d.strip().lower() in _turn_depth_levels(cfg):
            cfg.thinking_depth = d.strip().lower()
        else:
            notes.append(f"思考深度取值不认识（{d!r}），这一轮不传深度参数。")

    if "contextWindow" in overrides:
        w = overrides.get("contextWindow")
        if isinstance(w, bool) or not isinstance(w, int) or w <= 0:
            notes.append(f"上下文长度取值不认识（{w!r}），这一轮沿用模型配置里的窗口。")
        else:
            cfg.context_window = w

    return notes


def _warn_unadapted_thinking(provider: str | None) -> None:
    """未适配 provider 上显式开了/关 thinking：不传参数，但在日志里标注。"""
    key = str(provider or "")
    sys.stderr.write(
        "[llm] thinking switch requested but provider %r is not adapted "
        "in the section-6 mapping; sending no thinking parameter\n" % key)
    sys.stderr.flush()


def _warn_unadapted_depth(provider: str | None, depth: str) -> None:
    """该 provider 没有厂商级深度参数：不传深度键，深度改走提示词软引导。"""
    key = str(provider or "")
    sys.stderr.write(
        "[llm] thinking depth %r requested but provider %r has no vendor depth "
        "parameter; sending no depth key (soft hint only)\n" % (depth, key))
    sys.stderr.flush()


#: Output-budget floor below which a thinking model can burn the whole
#: completion budget on reasoning and return an EMPTY answer. Measured, not
#: guessed (MiniMax-M3, 2026-09-24):
#:   thinking={"type":"adaptive"} + max_tokens=4096 -> finish_reason="length",
#:   content 0 chars, completion_tokens 4096 with reasoning_tokens 4096;
#:   thinking={"type":"disabled"} + max_tokens=4096 -> finish_reason="stop",
#:   content 1213 chars. I.e. reasoning and answer SHARE one output budget.
#: Floor applied when thinking is on. Same idea as Hermes Agent's global 4096
#: cap + thinking-budget patch (its public repo, agent/transports/chat_completions.py):
#: "A global cap of 4096 is enough for visible text, but ... thinking can
#: exhaust it on the first request". It raises the cap per provider profile
#: (meta-ai: default_max_tokens=16384 with the note "small caps can finish with
#: empty content"); we raise it for the providers we measured.
THINKING_MAX_TOKENS_FLOOR = 16384

#: Only providers whose shared budget we actually MEASURED. For the rest the
#: vendor docs do not say, so we stay quiet instead of inventing a warning.
THINKING_SHARED_BUDGET_PROVIDERS = ("minimax-cn", "minimax-intl")


def effective_max_tokens(provider: str | None, thinking: bool | None,
                         max_tokens: int | None) -> int:
    """实际发出去的输出预算 = 用户配置，但「思考开着」时抬到平台底线。

    预算本身仍归用户（关掉思考就用用户的值，实测 4096 完全够）；只是不能让
    思考把正文的预算吃掉——那会静默返回空答案，比报错更糟。配置不动，抬的是
    这一次请求的值，前端会把「实际发送多少」显示出来。
    """
    try:
        budget = int(max_tokens) if max_tokens is not None else 0
    except (TypeError, ValueError):
        budget = 0
    key = str(provider or "").strip()
    if key not in THINKING_SHARED_BUDGET_PROVIDERS or thinking is False:
        return budget
    return max(budget, THINKING_MAX_TOKENS_FLOOR)


def max_tokens_note(provider: str | None, thinking: bool | None,
                    max_tokens: int | None) -> str | None:
    """抬高了才给一句说明（没抬高返回 None）。"""
    effective = effective_max_tokens(provider, thinking, max_tokens)
    configured = max_tokens if isinstance(max_tokens, int) else 0
    if effective <= configured:
        return None
    return (f"开思考时输出预算按 {effective} 发：实测该厂商的思考与正文共用 "
            f"max_tokens（配置里的 {configured} 曾导致正文为空）。"
            f"关掉思考就按你配的 {configured} 发。")


#: 会把思考混在 content 分片里的块类型（Anthropic 风格 / 部分兼容网关）。
_THINK_BLOCK_TYPES = {
    "thinking", "thinking_delta", "reasoning", "reasoning_text",
    "reasoning.text", "reasoning.summary", "summary_text",
}


def _scalar_text(v: Any) -> str:
    """str 直接用；dict 则挑里面的文本键（不同网关键名不一样）。"""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("text", "content", "summary", "thinking", "reasoning"):
            if isinstance(v.get(k), str) and v[k]:
                return v[k]
    return ""


def _blocks_text(v: Any, only_thinking: bool) -> str:
    """列表形态：可能是 reasoning_details:[{text}]，也可能是 content:[{type:'thinking',text}]。"""
    if not isinstance(v, list):
        return ""
    out: list[str] = []
    for blk in v:
        if isinstance(blk, str):
            if not only_thinking:
                out.append(blk)
            continue
        if not isinstance(blk, dict):
            continue
        if only_thinking and str(blk.get("type", "")).lower() not in _THINK_BLOCK_TYPES:
            continue
        out.append(_scalar_text(blk))
    return "".join(out)


def reasoning_text_from(container: Any) -> str:
    """把「这一段里的思考文本」抽出来 —— 各家字段形状不同，这里全都认。

    2026-09-27 实测汇总（用户要求「每个模型的思考返回格式都不一样,你要都能适配到」）：
      * OpenAI 兼容系（deepseek / qwen / glm / 部分 minimax）：reasoning_content: str
      * OpenRouter：reasoning: str，且**同时**给 reasoning_details: [{type,text,summary}]
      * 部分网关 / Ollama：thinking: str
      * Anthropic 风格 / 块式网关：content: [{type:'thinking'|'reasoning.text', text|summary}]

    关键：**按优先级取第一个非空的来源，不叠加** —— OpenRouter 的 reasoning 就是
    reasoning_details 拼出来的副本，两个都取会把思考文本翻倍。
    """
    if not isinstance(container, dict):
        return ""
    for key in ("reasoning_content", "reasoning", "reasoning_text", "thinking"):
        txt = _scalar_text(container.get(key))
        if txt:
            return txt
    txt = _blocks_text(container.get("reasoning_details"), only_thinking=False)
    if txt:
        return txt
    # content 是数组时，只认标记为思考的块，别把正常回答也当思考
    return _blocks_text(container.get("content"), only_thinking=True)


def _iter_sse_json(resp):
    """拆出 SSE 里 data: 后面的 JSON 字符串，跳过空行与 [DONE]。"""
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body and body != "[DONE]":
            yield body


def _default_opener(req, timeout: float):
    """默认走 urllib（MiniMax 上 curl 会随机 HTTP 000，不用 curl）。"""
    return urllib.request.urlopen(req, timeout=timeout)


def stream_chat(
    cfg: ModelConfig,
    messages: Iterable[dict[str, Any]],
    *,
    is_cancelled: Callable[[], bool] = lambda: False,
    model: str | None = None,
    max_tokens: int | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    opener: Callable[[Any, float], Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """流式对话，逐个 yield 归一化事件：delta / reasoning / done / error。

    取消：is_cancelled() 为真 -> yield done(finishReason="cancelled") 后返回。
    HTTP 流由 with 块正常关闭（不硬断，不留半开 socket）。

    工具调用：传了 tools 就把它们带进请求；模型决定调用时，流里的 tool_calls 分片
    按 index 累加（参数会跨多个 delta 被切开），最终在 done 事件的 toolCalls 字段里
    给出完整的 [{id, type, name, arguments}]，此时 finishReason 通常是 "tool_calls"。

    opener 可注入（测试用），默认 urllib；返回的对象需支持 with 与逐行迭代。
    """
    payload = {
        "model": model or cfg.model,
        "messages": list(messages),
        # Thinking shares the output budget on some vendors, so the cap we
        # actually send may be higher than the configured one (see above).
        "max_tokens": max_tokens or effective_max_tokens(
            cfg.provider, cfg.thinking, cfg.max_tokens),
        "stream": True,
        # OpenAI 流式规范里的开关；不上这个开关时部分 provider（实测 MiniMax）
        # 的 usage 全程为 null，token 用量统计会拿不到数。
        "stream_options": {"include_usage": True},
    }
    # OpenRouter 不认 stream_options，得单独要 usage.include，否则整轮 usage 为 null
    # （实测：同一台机器上 MiniMax 有用量行、OpenRouter 一行都没有，2026-09-27）。
    if "openrouter.ai" in (getattr(cfg, "base_url", "") or "").lower():
        payload["usage"] = {"include": True}
    # 生成参数四字段（契约 §1.1/§6）：temperature/topP 仅非 null 才出现键；
    # thinking 三态走 §6 映射表；未适配 provider 不传键但在日志里标注。
    payload.update(build_generation_params(
        getattr(cfg, "provider", None),
        getattr(cfg, "thinking", None),
        getattr(cfg, "temperature", None),
        getattr(cfg, "top_p", None),
        getattr(cfg, "thinking_depth", None),
        getattr(cfg, "thinking_supported", True),
        getattr(cfg, "model", None),
    ))
    if (getattr(cfg, "thinking", None) is not None
            and not is_thinking_adapted(getattr(cfg, "provider", None))):
        _warn_unadapted_thinking(getattr(cfg, "provider", None))
    if (getattr(cfg, "thinking_depth", None)
            and not is_thinking_depth_adapted(getattr(cfg, "provider", None))):
        _warn_unadapted_depth(getattr(cfg, "provider", None),
                              getattr(cfg, "thinking_depth", None))
    if tools:
        payload["tools"] = list(tools)
        if tool_choice:
            payload["tool_choice"] = tool_choice
    req = urllib.request.Request(
        cfg.endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + cfg.api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    usage = None
    finish_reason = "stop"
    # tool_calls 按 index 累加：参数会跨多个 delta 被切开
    tool_calls: dict[int, dict[str, str]] = {}

    def _collected_tool_calls() -> list[dict[str, Any]]:
        return [
            {
                "id": tool_calls[i].get("id") or "",
                "type": "function",
                "name": tool_calls[i].get("name") or "",
                "arguments": tool_calls[i].get("arguments") or "",
            }
            for i in sorted(tool_calls)
        ]

    try:
        with (opener or _default_opener)(req, cfg.timeout_seconds) as resp:
            for body in _iter_sse_json(resp):
                if is_cancelled():
                    yield {"type": "done", "finishReason": "cancelled", "usage": usage,
                           "toolCalls": _collected_tool_calls()}
                    return
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    continue
                br = chunk.get("base_resp")
                if isinstance(br, dict) and br.get("status_code") not in (0, None):
                    yield {"type": "error", "code": "PROVIDER_ERROR",
                           "message": "%s: %s" % (br.get("status_code"), br.get("status_msg"))}
                    return
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    reason_txt = reasoning_text_from(delta)
                    if reason_txt:
                        yield {"type": "reasoning", "text": reason_txt}
                    for frag in delta.get("tool_calls") or []:
                        idx = frag.get("index")
                        if not isinstance(idx, int):
                            idx = 0
                        slot = tool_calls.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""})
                        if frag.get("id"):
                            slot["id"] = frag["id"]
                        fn = frag.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if delta.get("content"):
                        yield {"type": "delta", "text": delta["content"]}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        code = "AUTH" if e.code in (401, 403) else "HTTP_ERROR"
        yield {"type": "error", "code": code, "message": "HTTP %s %s" % (e.code, detail)}
        return
    except Exception as e:
        yield {"type": "error", "code": "NETWORK",
               "message": "%s: %s" % (type(e).__name__, e)}
        return

    yield {"type": "done", "finishReason": finish_reason, "usage": usage,
           "toolCalls": _collected_tool_calls()}


def parse_tool_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    """把模型给的 tool_call.arguments 字符串解析成 dict。

    返回 (args, error)。空串/空白视为无参数（{}）。解析失败不抛，
    由上层决定是把错误回注给模型还是中止（args 返回空 dict）。
    """
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, "arguments 不是合法 JSON: %s" % e
    if not isinstance(parsed, dict):
        return {}, "arguments 必须是 JSON 对象，收到 %s" % type(parsed).__name__
    return parsed, None


def chat_messages_once(cfg, messages, **kw):
    """One-shot full body from a ready-made message list (script/test use).

    Same error contract as chat_once: raises LlmError when the stream ends
    in an error event. Used by the L2 summarizer for cache-sharing requests
    that reuse the main session's message prefix verbatim instead of a cold
    single-prompt request.
    """
    out = []
    for ev in stream_chat(cfg, list(messages), **kw):
        if ev["type"] == "delta":
            out.append(ev["text"])
        elif ev["type"] == "error":
            raise LlmError(ev["code"], ev["message"])
    return "".join(out)


def chat_once(cfg, prompt, **kw):
    """一次拿完整正文（脚本/测试用）。出错抛 LlmError。"""
    return chat_messages_once(cfg, [{"role": "user", "content": prompt}], **kw)
