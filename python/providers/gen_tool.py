"""生成类工具（image_generate）—— 把 gen_provider/gen_registry 挂到工具注册表。

职责边界（沿用 tools.py 第 9 节）：
  * 本模块**不做权限裁决**（2026-09-25 起只有 guard.py 按命令模式判要不要
    问用户，且没有分级），只管
    「给我合法参数 → 执行 → 如实返回」。
  * 不 import permissions（测试会用 ast 断言这条边界）。

接线形态（照 capabilities/vision.py 的 provider 注册表那套）：
  * provider 从 gen_registry 按配置 capabilities.<kind>_gen.provider 查；
    查不到 → NOT_CONFIGURED（不猜默认）。
  * 密钥只从环境变量 / 配置文件读，绝不落盘、绝不进日志。
  * ensure_registered() 幂等：每次把内置 provider 以**当前**密钥重新注册一遍，
    这样「刚在设置面板写进 .env 的 key」不用重启进程也能生效。

错误码：直接用 gen_provider 的契约错码集（AUTH / QUOTA / BAD_REQUEST /
PROVIDER_ERROR / NETWORK / TIMEOUT / CANCELLED / POLL_FAILED / NOT_CONFIGURED），
原样透传给上层，不在工具层二次翻译。
"""
from __future__ import annotations

import os
from typing import Callable

import appconfig
import gen_registry
import modelconfig
import tools
from gen_provider import CANCELLED, NOT_CONFIGURED, PROVIDER_ERROR
from tools import Tool, ToolResult

GEN_SUBDIR = "generated"


# ---------------------------------------------------------------------------
# 内置 provider 注册（幂等；每次用当前密钥重建实例）
# ---------------------------------------------------------------------------

_REGISTERED = False


def _import_builtins():
    """按需 import provider 实现。缺文件/缺依赖时跳过（不炸整个进程）。"""
    out = []
    try:
        from providers.image_minimax import MiniMaxImageProvider
        out.append(MiniMaxImageProvider)
    except Exception:
        pass
    try:
        from providers.image_dashscope import DashscopeImageProvider
        out.append(DashscopeImageProvider)
    except Exception:
        pass
    return out


def _env_key(env_name: str) -> str:
    """密钥解析：先 os.environ，再回落读 <config root>/.env。

    绝不打印、绝不写文件；只返回字符串给 provider 构造用。
    """
    if not env_name:
        return ""
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    try:
        path = modelconfig.env_file_path()
    except Exception:
        path = os.path.join(modelconfig._default_root(), ".env")
    try:
        return (appconfig.read_env_file(path).get(env_name) or "").strip()
    except Exception:
        return ""


def ensure_registered(*, force: bool = False) -> list[str]:
    """注册内置生成 provider（幂等）。返回被注册的 provider 名字列表。

    每次都按当前 env/.env 重建实例并覆盖注册：面板刚写入的 key 立刻可用，
    不需要重启 sidecar。
    """
    global _REGISTERED
    names: list[str] = []
    for cls in _import_builtins():
        try:
            env_name = getattr(cls, "api_key_env", "") or ""
            provider = cls(api_key=_env_key(env_name) or None)
        except Exception:
            continue
        try:
            gen_registry.register_provider(provider)
        except Exception:
            continue
        names.append(provider.name)
    _REGISTERED = bool(names)
    return names


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

class _GenTool(Tool):
    """生成类工具共同骨架：查 provider → 组装 out_dir → 调 generate()。"""

    kind = ""            # "image" | "video"
    tool_kind_arg = None  # 参数里覆盖 kind 的字段（本实现不用，留扩展位）

    def _config_segment(self) -> dict:
        seg = appconfig.capability(self.kind + "_gen", {})
        return seg if isinstance(seg, dict) else {}

    def _active_provider(self):
        """按配置取 provider；查不到返回 (None, 配置段)。每次先刷注册表。"""
        ensure_registered()
        cfg = self._config_segment()
        return gen_registry.get_active(self.kind, config={"capabilities": {self.kind + "_gen": cfg}}), cfg

    def _out_dir(self, workspace_root: str) -> str:
        base = workspace_root or os.getcwd()
        return os.path.join(base, GEN_SUBDIR)

    def run(self, args: dict, *, workspace_root: str) -> ToolResult:
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return ToolResult(ok=False, content="%s: 'prompt' must be a non-empty string" % self.name,
                              error_code="BAD_REQUEST",
                              error_message="%s: 'prompt' must be a non-empty string" % self.name)
        prompt = prompt.strip()

        refs = args.get("references") or []
        if isinstance(refs, str):
            refs = [refs]
        if not isinstance(refs, list):
            return ToolResult(ok=False, content="%s: 'references' must be an array of paths or URLs" % self.name,
                              error_code="BAD_REQUEST",
                              error_message="%s: 'references' must be an array" % self.name)
        model = args.get("model")
        if model is not None and not isinstance(model, str):
            return ToolResult(ok=False, content="%s: 'model' must be a string" % self.name,
                              error_code="BAD_REQUEST",
                              error_message="%s: 'model' must be a string" % self.name)
        aspect = args.get("aspect_ratio")
        if aspect is not None and not isinstance(aspect, str):
            return ToolResult(ok=False, content="%s: 'aspect_ratio' must be a string" % self.name,
                              error_code="BAD_REQUEST",
                              error_message="%s: 'aspect_ratio' must be a string" % self.name)

        provider, cfg = self._active_provider()
        if provider is None:
            wanted = cfg.get("provider") or "(未配置)"
            registered = [p.name for p in gen_registry.list_providers(kind=self.kind)]
            msg = ("%s: 没有可用的 %s provider。配置 capabilities.%s_gen.provider=%r，"
                   "已注册: %s" % (self.name, self.kind, self.kind, wanted, registered or "无"))
            return ToolResult(ok=False, content=msg, error_code=NOT_CONFIGURED,
                              error_message=msg)
        if isinstance(refs, list) and refs and not self.supports_references:
            msg = "%s: 该能力暂不支持参考图" % self.name
            return ToolResult(ok=False, content=msg, error_code="BAD_REQUEST",
                              error_message=msg)

        try:
            result = provider.generate(
                prompt=prompt,
                model=model or cfg.get("model"),
                out_dir=self._out_dir(workspace_root),
                aspect_ratio=aspect,
                references=refs,
            )
        except Exception as exc:                     # provider 不该把异常抛出来
            msg = "%s: provider 崩溃: %s" % (self.name, exc)
            return ToolResult(ok=False, content=msg, error_code=PROVIDER_ERROR,
                              error_message=msg)

        if not getattr(result, "ok", False):
            msg = result.error_message or ("%s: 生成失败" % self.name)
            return ToolResult(ok=False, content=msg,
                              error_code=result.error_code or PROVIDER_ERROR,
                              error_message=msg,
                              data={"meta": dict(getattr(result, "meta", {}) or {})})

        files = list(getattr(result, "files", ()) or ())
        meta = dict(getattr(result, "meta", {}) or {})
        if not files:
            msg = "%s: provider 返回成功但没有文件" % self.name
            return ToolResult(ok=False, content=msg, error_code=PROVIDER_ERROR,
                              error_message=msg, data={"meta": meta})
        # 展示口径：路径单独成行，渲染层按路径识别媒体（已验证的链路）
        lines = ["%s: 已生成 %d 个文件" % (self.name, len(files))]
        lines += [str(p) for p in files]
        if meta.get("model"):
            lines.append("model: %s" % meta["model"])
        if meta.get("aspect_ratio"):
            lines.append("aspect_ratio: %s" % meta["aspect_ratio"])
        return ToolResult(ok=True, content="\n".join(lines),
                          data={"files": files, "meta": meta})

    @property
    def supports_references(self) -> bool:
        return False


class ImageGenerateTool(_GenTool):
    name = "image_generate"
    kind = "image"
    description = (
        "Generate an image from a text prompt and save it into the workspace. "
        "Use this whenever the user asks you to create/draw/produce a picture, "
        "poster, illustration or concept art. The generated image file path is "
        "returned in the result so it can be shown to the user."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "What to draw. Be specific about subject, style, lighting and mood.",
            },
            "aspect_ratio": {
                "type": "string",
                "description": "Output aspect ratio, e.g. 1:1, 16:9, 9:16. Default 1:1.",
                "enum": ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "21:9"],
            },
            "references": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional reference image paths or URLs (image-to-image).",
            },
            "model": {
                "type": "string",
                "description": "Optional provider model override; omit to use the configured default.",
            },
        },
        "required": ["prompt"],
    }

    @property
    def supports_references(self) -> bool:
        return True


def register_defaults() -> None:
    """把所有生成类工具注册进 tools 全局注册表（幂等覆盖）。"""
    for tool in (ImageGenerateTool(),):
        tools.register(tool)


register_defaults()