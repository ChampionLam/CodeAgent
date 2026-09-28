"""Search backends for the ``web_search`` tool.

Backend choice follows the two-tier shape used by the reference agent
(Hermes Agent; see its public repo's ``tools/web_tools.py`` and
``plugins/web/``): a **keyed tier** (a real vendor
API the user configured) is preferred, and a **keyless tier** backs it up so a
machine with zero web credentials can still search. The keyless tier is the
anonymous public MCP endpoints wrapped in :mod:`web.mcp` (Exa, Parallel) —
verified working from the user's CN network on 2026-09-24, where scraping
DuckDuckGo times out and Baidu answers with a JS shell.

Why not scrape a search-results page: the markup rots, the ToS is grey, and it
breaks silently. A vendor-maintained MCP endpoint returns a stable shape and
publishes its own free tier.

Tier order on each call:

1. explicit ``WEB_SEARCH_PROVIDER`` (or the ``provider=`` argument) wins
2. else the first configured keyed backend
3. else / on failure, walk the keyless ring (rotating start per process)
4. all failed → :class:`SearchFailed` carrying every reason, never a made-up answer

Turn the whole tool off with ``WEB_SEARCH_PROVIDER=none``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from web import mcp

DEFAULT_RESULTS = 8
MAX_RESULTS = 10
TIMEOUT = 20.0


class SearchNotConfigured(Exception):
    """No backend can serve a search — the tool turns this into a usable message."""


class SearchFailed(Exception):
    """Backends were tried but every one of them failed."""


@dataclass
class SearchItem:
    title: str
    url: str
    snippet: str = ""


@dataclass
class SearchResult:
    query: str
    provider: str
    items: list[SearchItem] = field(default_factory=list)


def _post_json(url: str, payload: dict, headers: dict[str, str], timeout: float = TIMEOUT) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        **headers,
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise SearchFailed("搜索后端返回 HTTP %s%s" % (exc.code, ("：" + detail) if detail else ""))
    except urllib.error.URLError as exc:
        raise SearchFailed("搜索后端连不上：%s" % (exc.reason,))
    except json.JSONDecodeError:
        raise SearchFailed("搜索后端返回的不是 JSON")


def _snippets(rows: list[dict], count: int) -> list[SearchItem]:
    items = []
    for row in rows[:count]:
        items.append(SearchItem(
            title=str(row.get("title") or "").strip(),
            url=str(row.get("url") or "").strip(),
            snippet=str(row.get("snippet") or "").strip()[:600],
        ))
    return [item for item in items if item.url]


# --------------------------------------------------------------------------
# Keyed tier — a real vendor API. Preferred whenever its key is present.
# --------------------------------------------------------------------------

def _bocha(query: str, count: int) -> list[SearchItem]:
    key = os.environ.get("BOCHA_API_KEY", "").strip()
    if not key:
        raise SearchNotConfigured("BOCHA_API_KEY 未配置")
    data = _post_json("https://api.bochaai.com/v1/web-search",
                      {"query": query, "count": count, "summary": True},
                      {"Authorization": "Bearer " + key})
    pages = (((data.get("data") or {}).get("webPages") or {}).get("value")) or []
    return _snippets([{
        "title": page.get("name"),
        "url": page.get("url"),
        "snippet": page.get("summary") or page.get("snippet"),
    } for page in pages], count)


def _tavily(query: str, count: int) -> list[SearchItem]:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        raise SearchNotConfigured("TAVILY_API_KEY 未配置")
    data = _post_json("https://api.tavily.com/search",
                      {"query": query, "max_results": count, "search_depth": "basic"},
                      {"Authorization": "Bearer " + key})
    return _snippets([{
        "title": hit.get("title"),
        "url": hit.get("url"),
        "snippet": hit.get("content"),
    } for hit in (data.get("results") or [])], count)


def _searxng(query: str, count: int) -> list[SearchItem]:
    base = os.environ.get("SEARXNG_BASE_URL", "").strip().rstrip("/")
    if not base:
        raise SearchNotConfigured("SEARXNG_BASE_URL 未配置")
    url = "%s/search?%s" % (base, urllib.parse.urlencode({"q": query, "format": "json"}))
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise SearchFailed("SearXNG 返回 HTTP %s（实例可能没开 JSON 输出）" % exc.code)
    except urllib.error.URLError as exc:
        raise SearchFailed("SearXNG 连不上：%s（内网地址需在 WEB_EGRESS_ALLOW_PRIVATE 里放行）" % (exc.reason,))
    return _snippets([{
        "title": hit.get("title"),
        "url": hit.get("url"),
        "snippet": hit.get("content"),
    } for hit in (data.get("results") or [])], count)


# --------------------------------------------------------------------------
# Keyless tier — anonymous public MCP endpoints (no key, no account).
# --------------------------------------------------------------------------

def _mcp_provider(url: str, tool: str, build_arguments, parser):
    """Wrap an MCP endpoint as a provider callable."""

    def run(query: str, count: int) -> list[SearchItem]:
        try:
            text = mcp.call(url, tool, build_arguments(query, count))
        except mcp.McpError as exc:
            raise SearchFailed(str(exc))
        return _snippets(parser(text, count), count)

    return run


def _parallel_hits(text: str, count: int) -> list[dict]:
    """Parallel returns JSON; fold ``excerpts[]`` into one snippet."""
    rows = []
    for hit in (json.loads(text).get("results") or []):
        rows.append({
            "title": hit.get("title"),
            "url": hit.get("url"),
            "snippet": " ".join(str(item) for item in (hit.get("excerpts") or [])),
        })
    return rows


_parallel = _mcp_provider(
    mcp.PARALLEL_MCP_URL, "web_search",
    lambda query, count: {"objective": query, "search_queries": [query],
                          "session_id": mcp._SESSION_ID},
    _parallel_hits,
)
_exa = _mcp_provider(
    mcp.EXA_MCP_URL, "web_search_exa",
    lambda query, count: {"query": query, "numResults": count},
    mcp.parse_exa_text,
)


#: name -> (callable, required env names, keyless?)
_PROVIDERS: dict[str, tuple] = {
    "bocha": (_bocha, ("BOCHA_API_KEY",), False),
    "tavily": (_tavily, ("TAVILY_API_KEY",), False),
    "searxng": (_searxng, ("SEARXNG_BASE_URL",), False),
    "parallel": (_parallel, (), True),
    "exa": (_exa, (), True),
}

#: Keyless walk order when nothing keyed is configured (or when a keyed call
#: fails). Rotated per process so a fleet does not hammer one vendor.
KEYLESS_RING = ("parallel", "exa")

_RING_CURSOR = int(mcp._SESSION_ID, 16) % len(KEYLESS_RING)

_OFF_VALUES = ("none", "off", "false", "disabled", "0")


def keyless_ring(cursor: int | None = None) -> list[str]:
    """The keyless walk order starting at *cursor* (defaults to this process')."""
    start = _RING_CURSOR if cursor is None else cursor % len(KEYLESS_RING)
    return [KEYLESS_RING[(start + step) % len(KEYLESS_RING)] for step in range(len(KEYLESS_RING))]


def available_providers() -> list[str]:
    """Keyed backends whose config is present, in preference order."""
    out = []
    for name, (_fn, envs, keyless) in _PROVIDERS.items():
        if keyless:
            continue
        if all(os.environ.get(env, "").strip() for env in envs):
            out.append(name)
    return out


def active_provider() -> str | None:
    """Which backend a search would start with, or None when switched off."""
    wanted = os.environ.get("WEB_SEARCH_PROVIDER", "").strip().lower()
    if wanted in _OFF_VALUES:
        return None
    if wanted in _PROVIDERS:
        return wanted
    keyed = available_providers()
    if keyed:
        return keyed[0]
    return keyless_ring()[0] if search_enabled() else None


def search_enabled() -> bool:
    """False only when the user explicitly turned web search off."""
    return os.environ.get("WEB_SEARCH_PROVIDER", "").strip().lower() not in _OFF_VALUES


def search(query: str, *, max_results: int = DEFAULT_RESULTS,
           provider: str | None = None) -> SearchResult:
    """Run one query, walking the tiers. Raises SearchNotConfigured/SearchFailed."""
    text = (query or "").strip()
    if not text:
        raise SearchNotConfigured("query 不能为空")
    count = max(1, min(int(max_results or DEFAULT_RESULTS), MAX_RESULTS))
    if not search_enabled():
        raise SearchNotConfigured(
            "本机把联网搜索关掉了（WEB_SEARCH_PROVIDER=none）；"
            "已知网址仍然可以用 web_fetch 抓")

    wanted = (provider or os.environ.get("WEB_SEARCH_PROVIDER", "")).strip().lower()
    order: list[str] = []
    if wanted in _PROVIDERS:
        order = [wanted]                      # 用户点名了后端：只用它，失败就报错
    else:
        order = available_providers()         # 有 key 的先上
        order += keyless_ring()               # 免 key 兜底永远排最后

    failures: list[str] = []
    for name in order:
        fn, envs, _keyless = _PROVIDERS[name]
        if any(not os.environ.get(env, "").strip() for env in envs):
            continue
        try:
            items = fn(text, count)
        except SearchNotConfigured as exc:
            failures.append("%s：%s" % (name, exc))
            continue
        except SearchFailed as exc:
            failures.append("%s：%s" % (name, exc))
            continue
        except (mcp.McpError, ValueError, KeyError, TypeError) as exc:
            # 解析不出来也算这个后端失败，换下一个，但要说实话
            failures.append("%s：返回内容对不上（%s）" % (name, exc))
            continue
        # 没有结果也是答案：不要因为「没命中」再去问下一个后端，那只是在烧额度
        return SearchResult(query=text, provider=name, items=items)

    if not failures:
        raise SearchNotConfigured("没有可用的搜索后端（可配 BOCHA_API_KEY / TAVILY_API_KEY / "
                                  "SEARXNG_BASE_URL；不配也能用免 key 的公共搜索端点）")
    raise SearchFailed("；".join(failures[:3]))


def render(result: SearchResult) -> str:
    """Render results for the model: title, URL, snippet — nothing else."""
    if not result.items:
        return "（%s 没有返回结果：%s）" % (result.provider, result.query)
    rows = ["搜索后端：%s　关键词：%s" % (result.provider, result.query), ""]
    for index, item in enumerate(result.items, 1):
        rows.append("%d. %s" % (index, item.title or "(无标题)"))
        rows.append("   %s" % item.url)
        if item.snippet:
            rows.append("   %s" % item.snippet.replace("\n", " ")[:300])
    rows.append("")
    rows.append("要读全文就挑一条 URL 用 web_fetch 抓下来，别把摘要当结论。")
    return "\n".join(rows)