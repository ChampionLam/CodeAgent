import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../state/store';
import { Icon } from '../Icon';
import { MessageBubble } from './MessageBubble';
import './MessageBubble.css';

const SUGGESTIONS = [
  { icon: Icon.Bolt, text: '给这个项目加一层缓存' },
  { icon: Icon.Search, text: '审查当前的数据库查询' },
  { icon: Icon.Terminal, text: '跑一遍测试并修失败用例' },
  { icon: Icon.Shield, text: '把这个目录的项目结构讲一遍' }
];

/** One-line, non-blocking banner for the sidecar's context.compacted event
 *  (contract §7.5). Never a modal, never blocks input; dismissible. */
function CompactionBanner() {
  const { compactionNotices, activeSessionId, dismissCompactionNotice } = useApp();
  const notice = compactionNotices[activeSessionId];
  if (!notice) return null;
  const saved = notice.savedTokens != null
    ? `，约节省 ${notice.savedTokens.toLocaleString('en-US')} token`
    : '';
  const llm = notice.llmCalls > 0 ? '（含 1 次摘要调用）' : '（无需 LLM 调用）';
  const text = notice.summaryNote || '已压缩早期对话以适配上下文窗口';
  return (
    <div className="ctxbanner" role="status">
      <Icon.Cog size={12} />
      <span className="ctxbanner__text">
        {text} · 级别 {notice.level || '—'}
        {saved}{notice.llmCalls > 0 || notice.savedTokens != null ? llm : ''}。原文保留在会话库中。
      </span>
      <button
        className="ctxbanner__close"
        onClick={() => dismissCompactionNotice(activeSessionId)}
        title="关闭提示"
        aria-label="关闭压缩提示"
      >
        ×
      </button>
    </div>
  );
}

export function ChatArea() {
  const { messages, activeSessionId, isStreaming, send } = useApp();
  const list = messages[activeSessionId] ?? [];

  const scrollerRef = useRef<HTMLDivElement>(null);
  // Follow intent lives in a ref, not in state read back from the DOM: our own
  // follow scroll moves scrollTop too, so deciding from the position fights itself
  // and made the jump button flap while an answer streamed.
  const stickRef = useRef(true);
  const ownScrollRef = useRef(-1);
  const rafRef = useRef<number | null>(null);
  const [showJump, setShowJump] = useState(false);

  const scrollToBottom = (behavior: ScrollBehavior = 'smooth') => {
    const el = scrollerRef.current;
    if (!el) return;
    stickRef.current = true;
    setShowJump(false);
    el.scrollTo({ top: el.scrollHeight, behavior });
  };

  const last = list[list.length - 1];
  const streaming = isStreaming && last?.streaming;

  // Only new text should move the view. Keyed on lengths + session id so unrelated
  // store updates (model list, usage, session list) never touch the scroll position.
  const growth = `${activeSessionId}:${list.length}:${last?.content?.length ?? 0}:${last?.thinking?.length ?? 0}:${last?.toolCalls?.length ?? 0}:${last?.streaming ? 1 : 0}`;

  // Follow the newest text with an INSTANT scroll, batched to at most one per frame.
  // This used to be a smooth scroll fired on every store update - the stream emits
  // many chunks per second, so each chunk restarted the easing animation and the
  // viewport never settled. Measured on the verify box: 9% of the chat pane
  // repainting every 180 ms while MiniMax-M3 answered, which is the flicker.
  useEffect(() => {
    if (!stickRef.current || rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      const el = scrollerRef.current;
      if (!el || !stickRef.current) return;
      el.scrollTop = el.scrollHeight;
      ownScrollRef.current = el.scrollTop;
    });
  }, [growth]);

  useEffect(() => () => { if (rafRef.current !== null) cancelAnimationFrame(rafRef.current); }, []);

  const release = () => {
    if (!stickRef.current) return;
    stickRef.current = false;
    setShowJump(true);
  };

  // Scrolling up is a deliberate user act; wheel is the common case.
  const onWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (e.deltaY < 0) release();
  };

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    if (Math.abs(el.scrollTop - ownScrollRef.current) < 2) return; // our own follow
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distance < 4) {
      if (!stickRef.current) {
        stickRef.current = true;
        setShowJump(false);
      }
    } else if (distance > 120) {
      release();
    }
  };

  const today = new Date();
  const dayLabel = `今天 · ${today.getMonth() + 1} 月 ${today.getDate()} 日`;

  if (list.length === 0) {
    return (
      <div className="chatarea" ref={scrollerRef}>
        <div className="chatarea__empty">
          <div className="chatarea__empty-mark">
            <Icon.Sparkles size={24} />
          </div>
          <div className="chatarea__empty-title">开始一个新会话</div>
          <div className="chatarea__empty-desc">
            可读写文件、执行命令、查阅文档、联网搜索。仅真正危险的操作（删除系统、格式化等）需要先行确认——在会话内点击即可，不弹出对话框。
          </div>
          <div className="chatarea__suggestions">
            {SUGGESTIONS.map(({ icon: IconCmp, text }) => (
              <button key={text} className="chatarea__suggestion" onClick={() => send(text)}>
                <IconCmp size={14} />
                <span>{text}</span>
              </button>
            ))}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="chatarea-wrap">
      <div
        className="chatarea"
        ref={scrollerRef}
        onScroll={onScroll}
        onWheel={onWheel}
      >
        <div className="chatarea__inner">
          <div className="chatarea__daybar">
            <span>{dayLabel}</span>
          </div>

          {list.map(m => (
            <MessageBubble key={m.id} message={m} />
          ))}

          {/* Always mounted: the row keeps its height when the answer finishes, so
              the pane no longer shrinks by a row and pops the content. */}
          <div
            className={`chatarea__streaming${streaming ? '' : ' chatarea__streaming--idle'}`}
            aria-hidden={!streaming}
          >
            <span className="toolcard__spin" aria-hidden />
            Agent 正在生成回答…
          </div>
        </div>
      </div>

      {showJump && (
        <button className="chatarea__jump" onClick={() => scrollToBottom()}>
          <Icon.ArrowDown size={13} />
          回到最新
        </button>
      )}
    </div>
  );
}