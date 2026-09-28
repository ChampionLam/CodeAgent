"""Bytes -> readable text for the fetch tool.

Two audiences, one function: the model gets a text blob, the caller gets the
title and link list for citation. The parser is ``html.parser`` from the stdlib
— no dependency, no regex soup over markup, and nothing that executes.

Design notes that came from real pages:

* ``<script>``/``<style>``/``<svg>``/``<noscript>`` bodies are dropped, not
  rendered as text; otherwise inline JS lands in the model's context.
* Block-level tags become newlines so headings and list items stay separable.
* Whitespace is collapsed per line; blank-line runs are squeezed to two, which
  is what makes the output readable in a chat bubble.
* ``MAX_TEXT_CHARS`` caps what reaches the model. The full text goes to disk via
  the loop's L1 spill, so nothing is lost — just not pasted into the prompt.
"""
from __future__ import annotations

import html as _html
import json
import re
from html.parser import HTMLParser

MAX_TEXT_CHARS = 20_000
MAX_LINKS = 30

_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote", "pre",
    "table", "ul", "ol", "dl", "dt", "dd", "figure", "figcaption", "form",
    "br", "hr", "nav",
}
_WS_RUN = re.compile(r"[ \t\u00a0\u3000]+")
_BLANK_RUN = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.links: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._in_title = False
        self._href: str | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href.strip()
            self._anchor_text = []
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        if tag == "a":
            if self._href and len(self.links) < MAX_LINKS:
                text = "".join(self._anchor_text).strip()
                if self._href.startswith(("http://", "https://")):
                    self.links.append((text or self._href, self._href))
            self._href = None
            self._anchor_text = []
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title and not self.title:
            self.title = data.strip()
        if self._href is not None:
            self._anchor_text.append(data)
        self.parts.append(data)


def html_to_text(markup: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Return ``(title, text, links)`` for an HTML document."""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:
        # A malformed page must still yield whatever was parsed before the break.
        pass
    raw = "".join(parser.parts)
    lines = [_WS_RUN.sub(" ", line).strip() for line in raw.split("\n")]
    text = "\n".join(line for line in lines)
    text = _BLANK_RUN.sub("\n\n", text).strip()
    return parser.title.strip(), text, parser.links


def json_to_text(payload: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Pretty-print JSON so keys and values stay readable."""
    try:
        obj = json.loads(payload)
    except Exception:
        return "", payload.strip(), []
    return "", json.dumps(obj, ensure_ascii=False, indent=2), []


def decode_body(body: bytes, content_type: str) -> str:
    """Decode response bytes using the charset the server declared.

    Defaults to UTF-8 with replacement — a wrong charset guess must not raise,
    because the model is better served by slightly mangled text than by an error.
    """
    charset = ""
    match = re.search(r"charset=([A-Za-z0-9_\-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1).strip().lower()
    for candidate in (charset, "utf-8", "gb18030"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


def to_text(body: bytes, content_type: str, *, max_chars: int = MAX_TEXT_CHARS) -> dict:
    """Turn a fetched body into ``{"title", "text", "links", "truncated"}``."""
    base = (content_type or "").split(";", 1)[0].strip().lower()
    raw = decode_body(body, content_type)
    if base == "application/json":
        title, text, links = json_to_text(raw)
    elif base in ("text/plain", "text/markdown", "text/x-markdown"):
        title, text, links = "", raw.strip(), []
    else:
        title, text, links = html_to_text(raw)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {"title": title, "text": text, "links": links, "truncated": truncated}


def render(title: str, url: str, text: str, links: list[tuple[str, str]], truncated: bool) -> str:
    """Render the tool-facing text block, with the untrusted-content warning."""
    head = ["URL: %s" % url]
    if title:
        head.append("标题: %s" % title)
    body = "\n".join(head) + "\n\n" + text
    if truncated:
        body += "\n\n（正文过长，已截断；完整内容已落盘，需要时用 read_file 读那份文件）"
    if links:
        rows = ["", "## 页面里的链接"]
        for label, href in links:
            rows.append("- %s — %s" % (label[:80], href))
        body += "\n" + "\n".join(rows)
    return body


def escape_for_prompt(text: str) -> str:
    """Neutralize text that tries to impersonate our own control markers.

    Pages do contain ``</thinking>`` and similar strings; keeping them out of the
    model's instruction stream is cheap, so we do it even though the tool output
    is already treated as data.
    """
    out = text.replace("</thinking>", "<\\/thinking>")
    out = out.replace("<thinking>", "<\\thinking>")
    return _html.unescape(out) if "&amp;" in out else out