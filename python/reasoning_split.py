"""
流式推理标签分流器。

职责：把大模型流式输出里「内联在正文中的思考内容」与「真正的正文」分开。
部分 provider（实测 MiniMax 的 OpenAI 兼容端点）不提供独立的 reasoning_content
字段，而是把思考包在标签里直接塞进 delta.content：

    "a 用户要求只回答四个字 好吧我从简洁角度走 因为这个提问很直接 晴朗温暖"

本模块提供-块级的增量接口，把标签块内的文本路由到 reasoning 通道、块外的文本
路由到 content 通道。两条通道都保留，一个字符都不丢——分通道是为了让界面能用
不同样式渲染，不是为了隐藏或删除思考。

最关键的实现点是「悬挂缓冲」：标签可能被切在相邻 delta 之间（一个 delta 以
`<thi` 结尾、下一个以 `nk>` 开头）。逐块直接正则匹配必然出错，所以每次 feed
结束时，要把尾部「可能是某个标签前缀」的那几个字符挂起不发，等下一个 delta
拼上来再判定。挂起上限 = 最长 token 长度 - 1。
"""
from __future__ import annotations

# 标签集取自参考实现（Hermes Agent 侧的 _REASONING_TAGS 约定）
TAGS: tuple[str, ...] = (
    "REASONING_SCRATCHPAD",
    "think",
    "thinking",
    "reasoning",
    "thought",
)

# 每个 tag 都生成开/闭两种 token，统一小写便于大小写不敏感匹配
_TOKENS: tuple[str, ...] = tuple(
    tok for t in TAGS for tok in ("<" + t.lower() + ">", "</" + t.lower() + ">")
)

# 最长 token 是 </reasoning_scratchpad>（22 字符）→ 悬挂上限 21
MAX_TOKEN_LEN: int = max(len(t) for t in _TOKENS)
HOLD_MAX: int = MAX_TOKEN_LEN - 1


class ReasoningSplitter:
    """增量式分流器。典型用法：每收到一个 delta 调 feed()，流结束调 flush()。"""

    def __init__(self) -> None:
        self._mode: str = "content"   # "content" | "reasoning"
        self._skip_ws: bool = False   # 刚消费过闭标签 → 丢弃紧跟的空白
        self._buf: str = ""           # 待判定缓冲（含挂起部分）

    # -- 对外接口 ---------------------------------------------------------
    def feed(self, delta: str) -> tuple[str, str]:
        """送入一段新到达的文本，返回本次可安全发出的 (reasoning_text, content_text)。"""
        if not isinstance(delta, str):
            delta = "" if delta is None else str(delta)
        self._buf += delta
        return self._drain(flush=False)

    def flush(self) -> tuple[str, str]:
        """流结束时调用，把悬挂缓冲里的东西一次吐完。可重复调用（第二次返回空）。"""
        return self._drain(flush=True)

    # -- 内部 -------------------------------------------------------------
    def _drain(self, flush: bool) -> tuple[str, str]:
        reasoning: list[str] = []
        content: list[str] = []

        def emit(text: str, in_reasoning: bool) -> None:
            if not text:
                return
            if in_reasoning:
                reasoning.append(text)
                return
            if self._skip_ws:
                stripped = text.lstrip()
                if not stripped:
                    return          # 全是空白 → 整段丢弃，skip_ws 保持
                self._skip_ws = False
                content.append(stripped)
            else:
                content.append(text)

        while True:
            low = self._buf.lower()
            best_pos = -1
            best_tok = ""
            for tok in _TOKENS:
                p = low.find(tok)
                if p == -1:
                    continue
                # 取最靠前的；同位置取更长的（think vs thinking 这种前缀关系）
                if best_pos == -1 or p < best_pos or (p == best_pos and len(tok) > len(best_tok)):
                    best_pos, best_tok = p, tok
            if best_tok == "":
                break

            emit(self._buf[:best_pos], self._mode == "reasoning")
            self._buf = self._buf[best_pos + len(best_tok):]

            if best_tok.startswith("</"):
                if self._mode == "reasoning":
                    self._mode = "content"
                    self._skip_ws = True     # 闭标签后的空白要剥掉
                # content 态遇到孤立闭标签 → 直接丢弃
            else:
                if self._mode == "content":
                    self._mode = "reasoning"
                # reasoning 态遇到开标签 → 丢弃，继续停在 reasoning

        if flush:
            emit(self._buf, self._mode == "reasoning")
            self._buf = ""
            self._skip_ws = False
        else:
            hold = 0
            low = self._buf.lower()
            for length in range(min(len(self._buf), HOLD_MAX), 0, -1):
                suffix = low[-length:]
                if any(tok.startswith(suffix) and len(tok) > length for tok in _TOKENS):
                    hold = length
                    break
            if hold:
                held = self._buf[-hold:]
                emit(self._buf[:-hold], self._mode == "reasoning")
                self._buf = held
            else:
                emit(self._buf, self._mode == "reasoning")
                self._buf = ""

        return ("".join(reasoning), "".join(content))


# 供测试与调用方直接使用的便捷函数
def split_stream(chunks) -> tuple[str, str]:
    """把一串 delta 一次性分流，返回 (reasoning, content)。"""
    sp = ReasoningSplitter()
    rs: list[str] = []
    cs: list[str] = []
    for c in chunks:
        r, t = sp.feed(c)
        rs.append(r)
        cs.append(t)
    r, t = sp.flush()
    rs.append(r)
    cs.append(t)
    return ("".join(rs), "".join(cs))
