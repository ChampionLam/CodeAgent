"""Minimal JSON-RPC client for the anonymous public MCP search endpoints.

Ported design (not code) from the reference agent (Hermes Agent's keyless MCP
tier, its public repo plugins/web/keyless_mcp.py):
a fresh install with zero web credentials should still be able to search. Exa
and Parallel both run public, anonymous MCP endpoints with a free tier, and
both were verified reachable and returning real results from the user's CN
network on 2026-09-24 (Parallel ~1.8s, Exa ~2.2s) where DuckDuckGo times out
and Baidu answers with a JS shell.

Why an MCP endpoint instead of scraping a search-results page: the vendor
maintains the response shape, so the parser does not rot every time a page
gets restyled; and it needs no key, which is the whole point of this tier.

Security notes:
* Endpoint URLs are compiled-in constants — the model can never point this at
  an arbitrary host, so there is no SSRF surface here (unlike ``web_fetch``,
  which takes a model-supplied URL and therefore goes through ``egress``).
* Requests carry no user identifiers. Parallel's free tier wants a
  ``session_id`` purely for rate limiting, so we send a random per-process
  value that is never persisted and is not derived from the machine.
* stdlib only (``urllib``): the sidecar's venv has no ``requests``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
EXA_MCP_URL = "https://mcp.exa.ai/mcp"

#: Free-tier rate-limit correlation id: random per process, never persisted,
#: never derived from a user or machine identifier.
_SESSION_ID = uuid.uuid4().hex

TIMEOUT_SECONDS = 30.0

# A vendor that starts throttling should hand over to the next one instead of
# failing the user's search outright.
RATE_LIMIT_MARKERS = (
    "rate limit", "rate-limit", "ratelimit", "too many requests", "429",
    "quota exceeded", "slow down", "throttl",
)


class McpError(RuntimeError):
    """Transport, HTTP, JSON-RPC, or tool-level failure."""

    def __init__(self, message: str, *, rate_limited: bool = False):
        super().__init__(message)
        self.rate_limited = rate_limited


def is_rate_limit(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def _text_from_payload(payload) -> str | None:
    """Pull the text out of a JSON-RPC result body (plain or SSE framed)."""
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        error = payload["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise McpError("MCP 返回错误：%s" % message, rate_limited=is_rate_limit(str(message)))
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("isError"):
        chunks = [str(item.get("text") or "") for item in (result.get("content") or [])]
        detail = " ".join(chunks)[:300]
        raise McpError("MCP 工具报错：%s" % detail, rate_limited=is_rate_limit(detail))
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("text"):
            return str(item["text"])
    return None


def parse_body(body: str) -> str:
    """Accept a plain JSON body or an SSE-framed one; return the text payload."""
    stripped = (body or "").strip()
    if stripped.startswith("{"):
        try:
            text = _text_from_payload(json.loads(stripped))
            if text is not None:
                return text
        except json.JSONDecodeError:
            pass
    for line in stripped.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            text = _text_from_payload(json.loads(line[len("data: "):]))
        except json.JSONDecodeError:
            continue
        if text is not None:
            return text
    raise McpError("认不出的 MCP 响应格式")


def call(url: str, tool: str, arguments: dict, *, timeout: float = TIMEOUT_SECONDS,
         opener=None) -> str:
    """POST one ``tools/call`` and return its text payload.

    ``opener`` is a test seam: called with the prepared ``Request`` and timeout.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "workbuddy-desk-agent",
        },
        method="POST",
    )
    try:
        if opener is not None:
            response = opener(request, timeout)
            status = getattr(response, "status", 200)
            body = response.read().decode("utf-8", "replace")
        else:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:             # pragma: no cover - body may be gone
            pass
        raise McpError("HTTP %s：%s" % (exc.code, detail),
                       rate_limited=exc.code == 429 or is_rate_limit(detail))
    except urllib.error.URLError as exc:
        raise McpError("连不上 %s：%s" % (url, exc.reason))
    except TimeoutError:
        raise McpError("请求超时（%.0fs）：%s" % (timeout, url))
    if status >= 400:
        raise McpError("HTTP %s：%s" % (status, body[:300]),
                       rate_limited=status == 429)
    return parse_body(body)


# --------------------------------------------------------------------------
# Parallel: https://search.parallel.ai/mcp  (tools: web_search, web_fetch)
# --------------------------------------------------------------------------

def parallel_search(query: str, limit: int) -> list[dict]:
    """Return ``[{title, url, snippet}]`` from Parallel's free tier."""
    payload = json.loads(call(PARALLEL_MCP_URL, "web_search", {
        "objective": query,
        "search_queries": [query],
        "session_id": _SESSION_ID,
    }))
    out = []
    for index, hit in enumerate(payload.get("results") or []):
        if len(out) >= limit:
            break
        excerpts = hit.get("excerpts") or []
        out.append({
            "title": str(hit.get("title") or "").strip(),
            "url": str(hit.get("url") or "").strip(),
            "snippet": " ".join(str(item) for item in excerpts).strip(),
        })
    return out


# --------------------------------------------------------------------------
# Exa: https://mcp.exa.ai/mcp  (tools: web_search_exa, web_fetch_exa)
# --------------------------------------------------------------------------

def exa_search(query: str, limit: int) -> list[dict]:
    """Return ``[{title, url, snippet}]`` from Exa's free tier.

    Exa answers with formatted *text* rather than JSON, one block per result:

        Title: ... / URL: ... / Published: ... / Author: ... / Highlights: ...
    """
    text = call(EXA_MCP_URL, "web_search_exa", {"query": query, "numResults": limit})
    return parse_exa_text(text, limit)


def parse_exa_text(text: str, limit: int) -> list[dict]:
    out: list[dict] = []
    current: dict | None = None
    highlights: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Title:"):
            if current:
                current["snippet"] = " ".join(highlights).strip()
                out.append(current)
            current = {"title": stripped[len("Title:"):].strip(), "url": "", "snippet": ""}
            highlights = []
        elif stripped.startswith("URL:") and current is not None:
            current["url"] = stripped[len("URL:"):].strip()
        elif current is not None and stripped and not stripped.startswith(
                ("Published:", "Author:", "Highlights:")):
            highlights.append(stripped)
        if len(out) >= limit:
            break
    if current:
        current["snippet"] = " ".join(highlights).strip()
        out.append(current)
    return [item for item in out if item["url"]][:limit]