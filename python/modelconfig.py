"""Multi-model registry: config.json `models` section + env-file key writes.

Contract: docs/model-config-spec.md (single source of truth for field names
and error codes -- do not rename anything here without updating the spec).

Hard security rules enforced in this module:
  * API keys are only ever read from environment variables; they never enter
    config.json, never enter a returned dict, never enter a log message.
  * The panel sends `apiKey` to models.upsert; we write it to the env file
    (one `NAME=value` line) and set os.environ, then drop it. No echo back.
  * Writes to config.json are atomic (tmp file + os.replace) and preserve
    every other top-level section (capabilities etc.).

Legacy support: when config.json has an old-style top-level `model` section
and no `models.items`, we synthesize one item with id "legacy" (label taken
from model.model) so old deployments keep working untouched.

All comments in this file are English by local convention (mixed-language
comments near ASCII trip a local safety guard).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import re
import tempfile
import threading
from typing import Any

# llm owns the section-6 parameter maps (thinking switch + depth levels); it
# imports nothing from this module, so there is no cycle.
import llm  # noqa: E402
import model_catalog  # noqa: E402  (预置模型目录：窗口 / 价格 / 擅长)

# Error codes (fixed by the contract, do not rename)
MODEL_INVALID = "MODEL_INVALID"
MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
MODEL_KEY_MISSING = "MODEL_KEY_MISSING"
MODEL_LAST = "MODEL_LAST"

LEGACY_ID = "legacy"
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TIMEOUT_SECONDS = 180

# --- generation parameter fields (contract section 1.1, frozen) ---
# contextWindow: int, 1000..10000000, default 128000 (bool is not an int here)
DEFAULT_CONTEXT_WINDOW = 128000
CONTEXT_WINDOW_MIN = 1000
CONTEXT_WINDOW_MAX = 10000000
# thinking: true | false | null, default null (tri-state, no other types)
# thinkingDepth: "low" | "medium" | "high" | null, default null (json-friendly,
#   no other types). null = do not touch any depth key in the request body.
THINKING_DEPTH_VALUES: tuple[str, ...] = ("low", "medium", "high")
# temperature: number | null, default null, 0..2 inclusive
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 2.0
# topP: number | null, default null, 0..1 inclusive
TOP_P_MIN = 0.0
TOP_P_MAX = 1.0

# Field name -> default value, used for load-time backfill and for the
# "explicit null = back to default" branch of models.upsert.
GENERATION_DEFAULTS: dict[str, Any] = {
    "contextWindow": DEFAULT_CONTEXT_WINDOW,
    "thinking": None,
    "thinkingDepth": None,
    "temperature": None,
    "topP": None,
}

# --- provider resolution -----------------------------------------------------
# The `provider` field is a classification hint that decides which vendor
# parameter mapping (contract section 6) is used. Real configs drift: a model
# entry created from the legacy top-level `model` section (or hand-edited)
# carries provider "legacy" / "" / a free-text label, and then the thinking
# switch silently does nothing. So we resolve the provider key by falling back
# to the endpoint host -- the one thing that is always right, because the
# request actually goes there.
#: host suffix -> provider key (keys mirror PRESETS / the section 6 table).
PROVIDER_HOST_HINTS: dict[str, str] = {
    "api.minimaxi.com": "minimax-cn",
    "api.minimax.chat": "minimax-cn",
    "api.minimax.io": "minimax-intl",
    "api.deepseek.com": "deepseek",
    "dashscope.aliyuncs.com": "dashscope-bailian",
    "api.moonshot.cn": "moonshot",
    "api.moonshot.ai": "moonshot",
    "open.bigmodel.cn": "zhipu",
    "api.siliconflow.cn": "siliconflow",
    "api.openai.com": "openai",
    "localhost:11434": "ollama-local",
    "127.0.0.1:11434": "ollama-local",
}

#: provider values that are placeholders, not vendor keys -- always re-resolve.
PROVIDER_PLACEHOLDERS: frozenset[str] = frozenset({"", "legacy", "custom", "default"})


def _host_of(base_url: str | None) -> str:
    """Pull the bare host (with port) out of a baseUrl; '' when unparseable."""
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/?#]+)", raw)
    host = (m.group(1) if m else raw.split("/")[0]).strip()
    return host.rsplit("@", 1)[-1].lower()


def resolve_provider_key(provider: str | None, base_url: str | None) -> str:
    """Resolve the section-6 provider key for a model entry.

    Order:
      1. a real (non-placeholder) provider value wins as-is;
      2. otherwise match the baseUrl host against PROVIDER_HOST_HINTS;
      3. otherwise return whatever was given (unknown -> no parameters sent).

    Pure function: no I/O, no config writes (the resolved value is what callers
    put in the returned item; persisting it happens only when the user saves).
    """
    value = str(provider or "").strip()
    if value and value.lower() not in PROVIDER_PLACEHOLDERS:
        return value
    host = _host_of(base_url)
    if host:
        for hint, key in PROVIDER_HOST_HINTS.items():
            if host == hint or host.endswith("." + hint):
                return key
    return value

_ID_RE = re.compile(r"^[a-z0-9._-]+$")
_API_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_LOCK = threading.Lock()

# Config root override for the models registry. Production default is the
# repo root; tests point it at a temp dir so they never touch the real
# config.json / .env (set DESK_AGENT_CONFIG_ROOT, or patch _MODELS_ROOT).
_MODELS_ROOT_OVERRIDE: str | None = os.environ.get("DESK_AGENT_CONFIG_ROOT") or None


def _models_root() -> str:
    return _MODELS_ROOT_OVERRIDE or _ROOT


class ModelConfigError(Exception):
    """Raised for validation/lookup failures; carries a contract error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# env file location
# ---------------------------------------------------------------------------


def env_file_path(root: str | None = None) -> str:
    """Priority: DESK_AGENT_ENV_FILE env var, else <root>/.env."""
    override = os.environ.get("DESK_AGENT_ENV_FILE", "").strip()
    if override:
        return override
    return os.path.join(root or _ROOT, ".env")


def read_env_var(name: str, root: str | None = None) -> str:
    """Read a secret from the env file / os.environ. Never logs the value."""
    if not name:
        return ""
    if os.environ.get(name):
        return os.environ[name]
    path = env_file_path(root)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                k, v = stripped.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


class ModelFetchError(Exception):
    """Raised when the vendor's model list cannot be fetched.

    ``message`` is safe to show the user: it carries no key, no header dump.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: GET /models should be a small JSON document; refuse to read more than this.
MODELS_RESPONSE_CAP = 1 << 20  # 1 MiB


def _fetch_models_payload(base_url: str, api_key: str = "",
                          timeout: int = 20, opener=None) -> Any:
    """Ask an OpenAI-compatible endpoint for its catalogue; return the parsed body.

    Shared by fetch_remote_models (ids) and fetch_remote_models_detailed (ids +
    each model's own numbers), so one click in the UI still costs one request.

    The key goes into the Authorization header only; nothing here logs or
    returns it.
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ModelFetchError(MODEL_INVALID, "baseUrl is required to fetch models")
    url = base + "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    opener = opener or (lambda r, timeout: urllib.request.urlopen(r, timeout=timeout))
    try:
        with opener(req, timeout) as resp:
            raw = resp.read(MODELS_RESPONSE_CAP + 1)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ModelFetchError(
                "MODEL_FETCH_UNAUTHORIZED",
                "该厂商拒绝了这个 key（HTTP %d）" % e.code) from e
        raise ModelFetchError(
            "MODEL_FETCH_FAILED",
            "拉取模型列表失败：HTTP %d" % e.code) from e
    except Exception as e:  # noqa: BLE001 - surface a short reason, not a traceback
        raise ModelFetchError(
            "MODEL_FETCH_FAILED",
            "连不上该厂商：%s" % type(e).__name__) from e
    if len(raw) > MODELS_RESPONSE_CAP:
        raise ModelFetchError("MODEL_FETCH_FAILED", "模型列表响应过大，已放弃")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        raise ModelFetchError(
            "MODEL_FETCH_FAILED", "该地址没有返回 JSON（不像是 OpenAI 兼容接口）") from e


def _ids_from_payload(payload) -> list[str]:
    ids: list[str] = []

    def _collect(node) -> None:
        if isinstance(node, str):
            if node.strip():
                ids.append(node.strip())
            return
        if isinstance(node, list):
            for it in node:
                _collect(it)
            return
        if not isinstance(node, dict):
            return
        for key in ("data", "models", "model_list", "items"):
            if isinstance(node.get(key), list):
                _collect(node[key])
                return
        mid = node.get("id") or node.get("model") or node.get("name")
        if isinstance(mid, str) and mid.strip():
            ids.append(mid.strip())

    _collect(payload)
    if not ids:
        raise ModelFetchError("MODEL_FETCH_EMPTY", "该厂商返回了空列表")
    return sorted(set(ids))


def _positive_int(node: dict, key: str) -> int | None:
    """Only real positive ints count (bool is not an int here); else None."""
    v = node.get(key)
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        return None
    return v


def _meta_from_payload(payload) -> dict[str, dict[str, int]]:
    """Per-model numbers the vendor already handed back with the same response.

    OpenRouter-shaped: {"data": [{"id": ..., "context_length": 1000000,
    "top_provider": {"context_length": ..., "max_completion_tokens": ...}}]}.
    Only numbers actually present are recorded -- a model the vendor says
    nothing about gets no entry (the UI then offers its own ladder of window
    sizes instead of a value we made up).
    """
    out: dict[str, dict[str, int]] = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return out
    for entry in data:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id") or entry.get("model") or entry.get("name")
        if not isinstance(mid, str) or not mid.strip():
            continue
        top = entry.get("top_provider") if isinstance(entry.get("top_provider"), dict) else {}
        ctx = None
        for key in ("context_length", "context_window", "max_context_length"):
            ctx = _positive_int(entry, key)
            if ctx:
                break
        if not ctx:
            ctx = _positive_int(top, "context_length")
        nums: dict[str, int] = {}
        if ctx:
            nums["contextLength"] = ctx
        max_out = _positive_int(top, "max_completion_tokens")
        if max_out:
            nums["maxCompletionTokens"] = max_out
        if nums:
            out[mid.strip()] = nums
    return out


def fetch_remote_models(base_url: str, api_key: str = "",
                        timeout: int = 20, opener=None) -> list[str]:
    """Ask an OpenAI-compatible endpoint which models it serves.

    The user should not have to type a model name from memory: pick a vendor,
    fetch, choose. Uses urllib (the house style -- curl randomly returns HTTP
    000 on this network). Returns a sorted list of model ids.

    The key goes into the Authorization header only; nothing here logs or
    returns it.
    """
    return _ids_from_payload(_fetch_models_payload(base_url, api_key, timeout, opener))


def fetch_remote_models_detailed(base_url: str, api_key: str = "",
                                 timeout: int = 20, opener=None,
                                 ) -> tuple[list[str], dict[str, dict[str, int]]]:
    """One request, both answers: the id list plus each model's own numbers.

    Callers that show a context-window field need the second half: the vendor
    tells us the window in the same /models response, so the UI must not fall
    back to one blanket default (2026-09-27: a 1M model got saved as 128k
    because this information was thrown away).
    """
    payload = _fetch_models_payload(base_url, api_key, timeout, opener)
    return _ids_from_payload(payload), _meta_from_payload(payload)

def write_env_var(name: str, value: str, root: str | None = None) -> str:
    """Write `name=value` into the env file and set os.environ[name].

    Semantics required by the contract:
      * existing other lines and '#' comments are preserved;
      * an existing `name=` line is updated in place (no duplicate appended);
      * a missing name is appended at the end;
      * os.environ[name] is set immediately so no restart is needed.

    The value is a secret: this function never returns it, never logs it.
    Returns the path written. A trailing newline is ensured on append.
    """
    path = env_file_path(root)
    key_line = f"{name}={value}"
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == name:
            lines[i] = key_line
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(key_line)
    _atomic_write_text(path, "\n".join(lines) + "\n")
    os.environ[name] = value
    return path


# ---------------------------------------------------------------------------
# reading the models section
# ---------------------------------------------------------------------------


def _load_raw(root: str | None = None) -> tuple[dict, str]:
    """Return (parsed config dict, path). Falls back to config.example.json."""
    root = root or _ROOT
    path = os.path.join(root, "config.json")
    if not os.path.exists(path):
        path = os.path.join(root, "config.example.json")
    if not os.path.exists(path):
        raise ModelConfigError("CONFIG_ERROR", "config file not found: " + path)
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise ModelConfigError("CONFIG_ERROR", "config is not valid JSON: %s" % e) from e
    if not isinstance(raw, dict):
        raise ModelConfigError("CONFIG_ERROR", "config top level must be a JSON object")
    return raw, path


def _default_root() -> str:
    return _ROOT


def _backfill_generation_defaults(item: dict) -> dict:
    """Fill the four v2 generation fields with defaults when missing.

    Contract section 1.1: an old config.json without these fields must load
    with defaults and behave exactly like v1 (no key in the request body).
    Never raises; missing/null get the default, present values pass through.
    """
    for name, default in GENERATION_DEFAULTS.items():
        if item.get(name) is None:
            item[name] = default
    # Self-heal the provider hint on read: entries created from the legacy
    # section (or hand-edited) often say "legacy", which would silently disable
    # the section-6 mapping. Resolve it from the endpoint host so the thinking
    # switch actually reaches the vendor; nothing is written here (the value
    # gets persisted the next time the user saves the entry).
    item["provider"] = resolve_provider_key(item.get("provider"), item.get("baseUrl"))
    # Thinking shares the output budget on some vendors (measured on
    # MiniMax-M3: adaptive + max_tokens=4096 -> empty content). We do NOT touch
    # the configured value; we expose the cap that will actually be sent plus a
    # one-line explanation, so the UI can show both.
    item["effectiveMaxTokens"] = llm.effective_max_tokens(
        item.get("provider"), item.get("thinking"), item.get("maxTokens"))
    item["maxTokensNote"] = llm.max_tokens_note(
        item.get("provider"), item.get("thinking"), item.get("maxTokens"))
    return item


def _synthesize_legacy(raw: dict) -> dict:
    """Build one models item from the old top-level `model` section."""
    m = raw.get("model") or {}
    if not isinstance(m, dict):
        m = {}
    model_name = str(m.get("model") or "legacy")
    cap_models = ((raw.get("capabilities") or {}).get("models") or {})
    modalities = None
    if isinstance(cap_models, dict):
        entry = cap_models.get(model_name)
        if isinstance(entry, dict) and isinstance(entry.get("inputModalities"), list):
            modalities = entry["inputModalities"]
    item = {
        "id": LEGACY_ID,
        "label": model_name,
        "provider": "legacy",
        "baseUrl": str(m.get("baseUrl") or ""),
        "model": model_name,
        "apiKeyEnv": str(m.get("apiKeyEnv") or ""),
        "maxTokens": int(m.get("maxTokens", DEFAULT_MAX_TOKENS)),
        "timeoutSeconds": int(m.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)),
    }
    if modalities:
        item["inputModalities"] = list(modalities)
    _backfill_generation_defaults(item)
    return item


def _normalized_items(raw: dict) -> list[dict]:
    """Return the items list, synthesizing the legacy entry when absent."""
    models = raw.get("models")
    if isinstance(models, dict) and models.get("items") is not None:
        items = models.get("items")
        if not isinstance(items, list):
            return []
        out = [_backfill_generation_defaults(dict(it))
               for it in items if isinstance(it, dict)]
        return out
    # Old-style config: only a top-level `model` section.
    legacy = _synthesize_legacy(raw)
    if legacy.get("apiKeyEnv"):
        return [legacy]
    return []


def _default_id(raw: dict, items: list[dict]) -> str | None:
    models = raw.get("models")
    if isinstance(models, dict) and isinstance(models.get("defaultId"), str):
        return models["defaultId"]
    return items[0]["id"] if items else None


def load_env_file(root: str | None = None) -> int:
    """把 <root>/.env 的键灌进 os.environ（已存在的环境变量优先，不覆盖）。

    为什么需要：sidecar 判断「这个模型配没配 key」看的是 os.environ，而
    用户是往 .env 里填的 key。没有这一步，换台机器部署就会出现界面显示
    「还没配 key」、但实际调用又能通的分裂状态（.5 上靠启动脚本灌环境变量
    绕过了，这里补成程序自己的行为）。

    纪律：只搬值。不打印、不返回、不写日志——这里只报「灌了几个键」。
    """
    root = root or _default_root()
    path = os.path.join(root, ".env")
    count = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                name, _, value = line.partition("=")
                name = name.strip()
                value = value.strip().strip('"').strip("'")
                if not name or name in os.environ:
                    continue
                os.environ[name] = value
                count += 1
    except FileNotFoundError:
        return 0
    except OSError:
        return 0
    return count


def load_models(root: str | None = None) -> dict:
    """Return {defaultId, items, activeId} with hasKey computed per item.

    items are plain dicts (contract field names, camelCase) and NEVER contain
    any key value -- only the boolean `hasKey`.
    """
    raw, _path = _load_raw(root)
    items = _normalized_items(raw)
    default_id = _default_id(raw, items)
    active_id = default_id
    if active_id is not None and not any(it.get("id") == active_id for it in items):
        # defaultId points at a missing entry: fall back to first item.
        active_id = items[0]["id"] if items else None
    out_items = []
    for it in items:
        env_name = str(it.get("apiKeyEnv") or "")
        has_key = bool(env_name) and bool(os.environ.get(env_name, "").strip())
        out = dict(it)
        out["hasKey"] = has_key
        out_items.append(out)
    return {"defaultId": default_id, "items": out_items, "activeId": active_id}


def find_item(model_id: str, root: str | None = None) -> dict | None:
    """Look up one item by id (or None)."""
    for it in load_models(root)["items"]:
        if it.get("id") == model_id:
            return it
    return None


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    """Derive a URL-safe slug from a display label or model name."""
    s = (text or "").strip().lower()
    out = []
    for ch in s:
        if re.match(r"[a-z0-9._-]", ch):
            out.append(ch)
        elif ch == " ":
            out.append("-")
    slug = "".join(out).strip("-.")
    return slug or "model"


def _validate_item(item: dict, existing_ids: set[str], *, is_new: bool) -> None:
    """Raise ModelConfigError(MODEL_INVALID, ...) on any bad field."""
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise ModelConfigError(MODEL_INVALID, "id is required (a slug like [a-z0-9._-]+)")
    if not _ID_RE.match(item_id):
        raise ModelConfigError(
            MODEL_INVALID,
            "id %r must match [a-z0-9._-]+" % item_id)
    if is_new and item_id in existing_ids:
        raise ModelConfigError(MODEL_INVALID, "duplicate id: %s" % item_id)
    label = item.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ModelConfigError(MODEL_INVALID, "label is required (display name)")
    base_url = item.get("baseUrl")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ModelConfigError(MODEL_INVALID, "baseUrl is required")
    model = item.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ModelConfigError(MODEL_INVALID, "model is required (model name is free text)")
    api_key_env = item.get("apiKeyEnv")
    if not isinstance(api_key_env, str) or not api_key_env:
        raise ModelConfigError(MODEL_INVALID, "apiKeyEnv is required")
    if not _API_KEY_ENV_RE.match(api_key_env):
        raise ModelConfigError(
            MODEL_INVALID,
            "apiKeyEnv %r must match [A-Z][A-Z0-9_]* (env var name)" % api_key_env)
    if item.get("provider") is not None and not isinstance(item.get("provider"), str):
        raise ModelConfigError(MODEL_INVALID, "provider must be a string when present")
    for num_field, default in (("maxTokens", DEFAULT_MAX_TOKENS),
                               ("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)):
        v = item.get(num_field, default)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise ModelConfigError(
                MODEL_INVALID, "%s must be a positive integer" % num_field)
    modalities = item.get("inputModalities")
    if modalities is not None:
        if (not isinstance(modalities, list)
                or not modalities
                or not all(isinstance(m, str) and m.strip() for m in modalities)):
            raise ModelConfigError(
                MODEL_INVALID,
                "inputModalities must be a non-empty list of strings (e.g. [\"text\",\"image\"])")
        lowered = {str(m).lower() for m in modalities}
        if not lowered <= {"text", "image"}:
            raise ModelConfigError(
                MODEL_INVALID, "inputModalities only supports \"text\" and \"image\"")
    # --- generation parameter fields (contract section 1.1) ---
    cw = item.get("contextWindow", DEFAULT_CONTEXT_WINDOW)
    if not isinstance(cw, int) or isinstance(cw, bool):
        raise ModelConfigError(
            MODEL_INVALID, "contextWindow must be an integer (token estimate)")
    if not CONTEXT_WINDOW_MIN <= cw <= CONTEXT_WINDOW_MAX:
        raise ModelConfigError(
            MODEL_INVALID,
            "contextWindow must be within %d..%d, got %d"
            % (CONTEXT_WINDOW_MIN, CONTEXT_WINDOW_MAX, cw))
    thinking = item.get("thinking", None)
    if thinking is not None and not isinstance(thinking, bool):
        raise ModelConfigError(
            MODEL_INVALID,
            "thinking must be true, false, or null (tri-state), got %s"
            % type(thinking).__name__)
    depth = item.get("thinkingDepth", None)
    if depth is not None:
        if not isinstance(depth, str) or depth.strip().lower() not in THINKING_DEPTH_VALUES:
            raise ModelConfigError(
                MODEL_INVALID,
                "thinkingDepth must be one of %s or null, got %r"
                % ("/".join(THINKING_DEPTH_VALUES), depth))
    for name, lo, hi in (("temperature", TEMPERATURE_MIN, TEMPERATURE_MAX),
                         ("topP", TOP_P_MIN, TOP_P_MAX)):
        v = item.get(name, None)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ModelConfigError(
                MODEL_INVALID, "%s must be a number or null" % name)
        if not lo <= v <= hi:
            raise ModelConfigError(
                MODEL_INVALID, "%s must be within %s..%s (inclusive), got %s"
                % (name, lo, hi, v))


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def _atomic_write_text(path: str, text: str) -> None:
    """Write via tmp file + os.replace; fails if the dir is unwritable."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save_config(raw: dict, root: str | None = None) -> None:
    """Serialize config back, atomically, preserving unknown sections.

    Always writes config.json: config.example.json is a read-only template, and
    a save must never overwrite it (that is where the sample config lives).
    """
    root = root or _ROOT
    path = os.path.join(root, "config.json")
    text = json.dumps(raw, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(path, text)


def upsert_model(params: dict, root: str | None = None) -> dict:
    """Create or update one models item. Returns {id}.

    When params["apiKey"] is a non-empty string it is written to the env file
    (in-place update, comments preserved) and injected into os.environ; it is
    never echoed back and never stored in config.json.
    """
    with _LOCK:
        raw, _path = _load_raw(root)
        items = _normalized_items(raw)
        existing_ids = {str(it.get("id")) for it in items}
        item_id = params.get("id")
        is_new = item_id is None
        if is_new:
            item_id = _slugify(str(params.get("label") or params.get("model") or ""))
            if item_id in existing_ids:
                raise ModelConfigError(
                    MODEL_INVALID,
                    "duplicate id: %s already exists "
                    "(refuse to auto-suffix; pass an explicit id to update it)"
                    % item_id)
        item_id = str(item_id)
        # Generation fields (contract section 2): on update, a missing field
        # keeps the stored value and an explicit null resets it to the
        # default; on create, missing means the default.
        existing_item = None
        if not is_new:
            existing_item = next(
                (it for it in items if str(it.get("id")) == item_id), None)
        item = {
            "id": item_id,
            "label": params.get("label"),
            "baseUrl": params.get("baseUrl"),
            "model": params.get("model"),
            "apiKeyEnv": params.get("apiKeyEnv"),
            "maxTokens": params.get("maxTokens", DEFAULT_MAX_TOKENS),
            "timeoutSeconds": params.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS),
        }
        for name, default in GENERATION_DEFAULTS.items():
            if name in params:
                v = params.get(name)
                item[name] = default if v is None else v
            elif existing_item is not None and name in existing_item:
                item[name] = existing_item[name]
            else:
                item[name] = default
        if params.get("provider") is not None:
            item["provider"] = params.get("provider")
        elif existing_item is not None and existing_item.get("provider"):
            # Missing provider on update keeps the stored value (same rule as the
            # generation fields) instead of dropping it.
            item["provider"] = existing_item.get("provider")
        # Persist a resolved provider key: a "legacy"/empty hint would disable
        # the section-6 vendor mapping for this entry forever.
        item["provider"] = resolve_provider_key(item.get("provider"), item.get("baseUrl"))
        if params.get("inputModalities") is not None:
            item["inputModalities"] = params.get("inputModalities")
        _validate_item(item, existing_ids, is_new=is_new)

        # Secret handling: write env + inject, then drop the raw secret.
        api_key = params.get("apiKey")
        if isinstance(api_key, str) and api_key:
            write_env_var(str(params["apiKeyEnv"]), api_key, root=root)
        _strip_secrets(item)
        # An explicit id that already exists is an update: replace in place so
        # the list keeps its order and no duplicate id can slip in.
        at = next((i for i, it in enumerate(items)
                   if str(it.get("id")) == item_id), None)
        if at is None:
            new_items = items + [item]
        else:
            new_items = list(items)
            new_items[at] = item
        # Adding a model must not silently re-point every new session at it, so
        # keep whatever the runtime default is right now as the default.
        try:
            current_default = str(load_models(root=root).get("defaultId") or "")
        except ModelConfigError:
            current_default = ""
        _apply_items(raw, new_items, default_hint=current_default or item_id)
        _save_config(raw, root)
        return {"id": item_id}


def _strip_secrets(item: dict) -> None:
    item.pop("apiKey", None)
    item.pop("hasKey", None)


def _apply_items(raw: dict, items: list[dict], default_hint: str | None = None,
                 force_default: bool = False) -> None:
    """Set raw["models"] = {defaultId, items}, keeping a valid default.

    force_default=True pins defaultId to default_hint even when the current
    default is still valid (that is what models.setDefault needs).
    """
    models = raw.get("models")
    if not isinstance(models, dict):
        models = {}
    models["items"] = items
    default_id = models.get("defaultId")
    ids = {str(it.get("id")) for it in items}
    if force_default and default_hint and default_hint in ids:
        models["defaultId"] = default_hint
        raw["models"] = models
        return
    if default_id not in ids:
        models["defaultId"] = default_hint if (default_hint and default_hint in ids) \
            else (items[0]["id"] if items else None)
    raw["models"] = models


def remove_model(model_id: str, root: str | None = None) -> dict:
    """Remove one item. Refuses the last one; re-points the default."""
    with _LOCK:
        raw, _path = _load_raw(root)
        items = _normalized_items(raw)
        ids = [str(it.get("id")) for it in items]
        if model_id not in ids:
            raise ModelConfigError(MODEL_NOT_FOUND, "no such model id: %s" % model_id)
        if len(items) <= 1:
            raise ModelConfigError(MODEL_LAST, "cannot remove the last model entry")
        new_items = [it for it in items if str(it.get("id")) != model_id]
        default_id = _default_id(raw, items)
        result = {"removed": model_id}
        if default_id == model_id:
            new_default = new_items[0]["id"]
            result["newDefaultId"] = new_default
            _apply_items(raw, new_items, default_hint=new_default)
        else:
            _apply_items(raw, new_items)
        _save_config(raw, root)
        return result


def set_default_model(model_id: str, root: str | None = None) -> dict:
    """Point models.defaultId at an existing item id."""
    with _LOCK:
        raw, _path = _load_raw(root)
        items = _normalized_items(raw)
        ids = {str(it.get("id")) for it in items}
        if model_id not in ids:
            raise ModelConfigError(MODEL_NOT_FOUND, "no such model id: %s" % model_id)
        _apply_items(raw, items, default_hint=model_id, force_default=True)
        _save_config(raw, root)
        return {"defaultId": model_id}


# ---------------------------------------------------------------------------
# presets
# ---------------------------------------------------------------------------

# Presets only suggest the baseUrl and the env var NAME for the key -- never
# a list of model names (users fill the model field themselves).
# contextWindow is a *suggested* default for the new entry (users pick the
# model themselves, so this is a starting hint, not a claim about any model).
# thinkingSupported mirrors the section 6 mapping table.
PRESETS: list[dict] = [
    {"key": "openai", "label": "OpenAI", "baseUrl": "https://api.openai.com/v1",
     "keyEnvHint": "OPENAI_API_KEY", "needsKey": True,
     "notes": "OpenAI 官方接口",
     "contextWindow": 128000, "thinkingSupported": True},
    {"key": "deepseek", "label": "DeepSeek", "baseUrl": "https://api.deepseek.com/v1",
     "keyEnvHint": "DEEPSEEK_API_KEY", "needsKey": True,
     "notes": "DeepSeek 官方接口（OpenAI 兼容）",
     "contextWindow": 128000, "thinkingSupported": True},
    {"key": "minimax-cn", "label": "MiniMax CN", "baseUrl": "https://api.minimaxi.com/v1",
     "keyEnvHint": "MINIMAX_CN_API_KEY", "needsKey": True,
     "notes": "MiniMax 国内站（api.minimaxi.com）",
     "contextWindow": 1000000, "thinkingSupported": True},
    {"key": "minimax-intl", "label": "MiniMax Intl", "baseUrl": "https://api.minimax.io/v1",
     "keyEnvHint": "MINIMAX_INTL_API_KEY", "needsKey": True,
     "notes": "MiniMax 国际站（api.minimax.io）",
     "contextWindow": 1000000, "thinkingSupported": True},
    {"key": "dashscope-bailian", "label": "DashScope Bailian",
     "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "keyEnvHint": "DASHSCOPE_API_KEY", "needsKey": True,
     "notes": "阿里云百炼公有云，OpenAI 兼容模式",
     "contextWindow": 131072, "thinkingSupported": True},
    {"key": "moonshot", "label": "Moonshot", "baseUrl": "https://api.moonshot.cn/v1",
     "keyEnvHint": "MOONSHOT_API_KEY", "needsKey": True,
     "notes": "月之暗面 Kimi",
     "contextWindow": 131072, "thinkingSupported": True},
    {"key": "zhipu", "label": "Zhipu", "baseUrl": "https://open.bigmodel.cn/api/paas/v4",
     "keyEnvHint": "ZHIPU_API_KEY", "needsKey": True,
     "notes": "智谱 GLM，OpenAI 兼容",
     "contextWindow": 128000, "thinkingSupported": True},
    {"key": "siliconflow", "label": "SiliconFlow", "baseUrl": "https://api.siliconflow.cn/v1",
     "keyEnvHint": "SILICONFLOW_API_KEY", "needsKey": True,
     "notes": "SiliconFlow 聚合平台",
     "contextWindow": 131072, "thinkingSupported": True},
    {"key": "openrouter", "label": "OpenRouter", "baseUrl": "https://openrouter.ai/api/v1",
     "keyEnvHint": "OPENROUTER_API_KEY", "needsKey": True,
     "notes": "OpenRouter 聚合平台（含匿名/隐身模型）",
     "contextWindow": 131072, "thinkingSupported": True},
    {"key": "ollama-local", "label": "Ollama (local)",
     "baseUrl": "http://localhost:11434/v1",
     "keyEnvHint": "OLLAMA_API_KEY", "needsKey": False,
     "notes": "本地 Ollama，key 可留空",
     "contextWindow": 8192, "thinkingSupported": False},
    # 2026-09-26 修正：custom 原来是 needsKey=False，界面就印成「（本地，无需 key）」——
    # 这是错的。自定义 OpenAI 兼容端点绝大多数是自建/中转的远程服务，是要 key 的；
    # 只有真的本地服务才无需 key。这里改成 True，界面那句误导文案随之消失。
    {"key": "custom", "label": "Custom OpenAI-compatible",
     "baseUrl": "", "keyEnvHint": "CUSTOM_API_KEY", "needsKey": True,
     "notes": "自建或中转的 OpenAI 兼容端点，地址要自己填",
     "contextWindow": 128000, "thinkingSupported": False},
]


def decorate_thinking(entry: dict) -> dict:
    """给模型/预设条目补上深度能力字段（单一口径，UI 靠它决定给硬档位还是软引导）。

    ``thinkingDepthLevels`` 列出该 provider 真有厂商级深度参数时的档位（MiniMax 这类
    没有该参数的厂商为空表）；``softThinkingDepth`` 为真表示界面该给「软引导」的说法，
    而不是假装有个硬旋钮。

    models.list（已配置的模型）和 models.presets（可选的对接方式）都得走这里——
    否则界面拿不到字段，就会一直显示成有硬参数，等于骗用户。
    """
    key = str(entry.get("provider") or entry.get("key") or "").strip()
    # 目录（模型级）优先：glm-5.3 这类「能调档但关不掉」的模型，只有目录说得清。
    prof = llm.model_thinking_profile(key, entry.get("model"))
    levels = [str(x) for x in (prof.get("levels") or [])]
    entry["thinkingDepthLevels"] = levels
    # 能不能关思考：目录 thinking.off 为 null 的模型（glm-5.3）不能给「不思考」选项。
    entry["thinkingCanDisable"] = bool(prof.get("off"))
    # 界面靠这个决定要不要显示「思考强度」那一行：这条线路/模型一个思考键都发不出去
    # 才算没旋钮（此时界面写静态说明，见 ModelPanel）。
    entry["thinkingAdapted"] = (bool(prof.get("on") or prof.get("off"))
                                and bool(entry.get("thinkingSupported", True)))
    entry["softThinkingDepth"] = bool(entry.get("thinkingSupported", True)) and not levels
    return entry


def list_presets() -> dict:
    """Presets + per-provider thinking depth capability (single source: llm).

    深度能力字段见 ``decorate_thinking``；``catalog`` 是该对接方式下的预置模型
    目录（窗口 / 价格 / 擅长），用来填「添加模型」的下拉，不再靠 GET /models
    现拉——现拉只能拿到模型名，拿不到窗口和价格。
    """
    presets = []
    for p in PRESETS:
        entry = decorate_thinking(dict(p))
        entry["catalog"] = model_catalog.for_provider(str(p.get("key") or ""))
        presets.append(entry)
    return {"presets": presets}


# ---------------------------------------------------------------------------
# chat resolution (session-level modelId override)
# ---------------------------------------------------------------------------


def resolve_model(model_id: str | None, root: str | None = None) -> dict:
    """Resolve the modelId a chat should use, plus its ModelConfig fields.

    Rules (contract section 4):
      * modelId given -> must exist (else MODEL_NOT_FOUND);
      * no modelId -> models.defaultId (legacy config -> the "legacy" item);
      * the resolved item's env var must be set (else MODEL_KEY_MISSING,
        message names the env var but never its value).
    Returns a dict of ModelConfig-shaped fields (base_url/model/api_key_env/
    max_tokens/timeout_seconds/api_key -- the api_key value lives only in the
    returned struct, which the caller must not log).
    """
    data = load_models(root)
    items = data["items"]
    if model_id is not None:
        item = next((it for it in items if it.get("id") == model_id), None)
        if item is None:
            raise ModelConfigError(
                MODEL_NOT_FOUND, "no such model id: %s" % model_id)
    else:
        item = next((it for it in items if it.get("id") == data["activeId"]), None)
        if item is None:
            if not items:
                raise ModelConfigError(
                    MODEL_NOT_FOUND, "no models configured")
            item = items[0]
    env_name = str(item.get("apiKeyEnv") or "")
    api_key = os.environ.get(env_name, "").strip()
    if not env_name or not api_key:
        raise ModelConfigError(
            MODEL_KEY_MISSING,
            "environment variable %s is not set (key only comes from the "
            "environment / .env file)" % env_name)
    return {
        "base_url": str(item.get("baseUrl") or ""),
        "model": str(item.get("model") or ""),
        "api_key_env": env_name,
        "max_tokens": int(item.get("maxTokens", DEFAULT_MAX_TOKENS)),
        "timeout_seconds": int(item.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)),
        "api_key": api_key,
        "id": str(item.get("id")),
        # The provider is what the generation-parameter mapping keys off
        # (contract section 6): without it, thinking would never be sent.
        "provider": item.get("provider"),
        "inputModalities": list(item.get("inputModalities") or []),
        # Generation parameters (contract section 1.1); _normalized_items has
        # already backfilled defaults for old configs.
        "contextWindow": int(item.get("contextWindow", DEFAULT_CONTEXT_WINDOW)),
        "thinking": item.get("thinking", None),
        # Output cap actually sent (raised above the configured one when
        # thinking is on) + why; note is null when nothing was raised.
        "effectiveMaxTokens": item.get("effectiveMaxTokens"),
        "maxTokensNote": item.get("maxTokensNote"),
        # Depth level (contract section 1.1/6); null = send no depth key.
        "thinkingDepth": item.get("thinkingDepth", None),
        # 这条线路能不能收发思考参数（配置项 thinkingSupported，缺省 True）。
        "thinking_supported": bool(item.get("thinkingSupported", True)),
        "temperature": item.get("temperature", None),
        "topP": item.get("topP", None),
    }
