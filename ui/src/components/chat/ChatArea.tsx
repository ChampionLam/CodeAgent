import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../state/store';
import type { Message } from '../../types';
import { Icon } from '../Icon';
import { MessageBubble } from './MessageBubble';
import './MessageBubble.css';

const SUGGESTIONS = [
  { icon: Icon.Bolt, text: '给这个项目加一层缓存' },
  { icon: Icon.Search, text: '审查当前的数据库查询' },
  { icon: Icon.Terminal, text: '跑一遍测试并修失败用例' },
  { icon: Icon.Shield, text: '把这个目录的项目结构讲一遍' }
];

/**
 * 项目会话的开场引导（用户 2026-10-09 口径：普通会话要等用户先问，项目会话
 * 一建好就主动说话）。三句话对应三种起步方式，点按钮**只把提示词填进输入框**，
 * 不自动发送 —— 用户还能改，也不烧 token、不落库。
 *
 * OpenSpec 的事实口径（不许改写成别的说法）：Fission-AI/OpenSpec，MIT，
 * spec 驱动开发（SDD），入口 /opsx:propose · /opsx:apply · /opsx:archive，
 * 产出落在项目里的 openspec/changes/<变更名>/（proposal.md / specs/ /
 * design.md / tasks.md）。
 */
const PROJECT_PROMPTS = [
  {
    icon: Icon.Bolt,
    text: '新建',
    prompt: '这是个新项目：<一句话说清要做什么>。先帮我把架子搭起来（目录结构 + 能跑起来的最小版本），边做边跟我说下一步。'
  },
  {
    icon: Icon.Terminal,
    text: '接现成的',
    prompt: '接的是已有目录（项目设置里指定过）。先读一遍代码和文档，跟我说说它现在是啥情况、结构咋样、哪儿有明显问题，再说下一步干啥。'
  },
  {
    icon: Icon.Sparkles,
    text: '先写方案',
    prompt: '用 OpenSpec（Fission-AI/OpenSpec）起这个项目：先把想法整理成 /opsx:propose "<变更名>"，在 openspec/changes/<变更名>/ 下出 proposal.md、specs/、design.md、tasks.md，我点头再用 /opsx:apply 按任务做。'
  }
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
  const {
    messages, activeSessionId, isStreaming, send, sessions, setComposerDraft,
    flowsWaiting, answerFlow
  } = useApp();
  const list = messages[activeSessionId] ?? [];
  const flow = flowsWaiting[activeSessionId];
  /** 「重填」按过之后就把那行收回（按 step id 记，换步自动复位）。 */
  const [retryHidden, setRetryHidden] = useState<string | null>(null);

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
    // 项目会话：开场用**对话形式**说一句（用户 2026-10-09 口径：不要标题式空态卡，
    // 要像助手真回了一条）。所以这里渲染的是跟真消息同一个组件的气泡，
    // 只是不落库、不调模型 —— 用户发出第一条消息后它自然消失。
    const project = sessions.find(s => s.id === activeSessionId);
    if (project?.kind === 'project') {
      const name = project.projectName || '未命名项目';
      const where = project.workspaceRoot
        ? `代码在 **${project.workspaceRoot}**，回头要改点上面项目名。`
        : '还没定目录，点上面项目名就能改。';
      const greeting: Message = {
        id: 'project-greeting',
        role: 'assistant',
        content:
          `这是 **${name}** 自己的会话，记录跟别处分开，回头想聊就点它。\n\n` +
          `${where}\n\n` +
          '先说这个：要**新建**一个，还是**接现成的**代码？',
        createdAt: Date.now(),
      };
      return (
        <div className="chatarea" ref={scrollerRef}>
          <div className="chatarea__inner">
            <MessageBubble message={greeting} />
            <div className="chatarea__greeting-chips">
              {PROJECT_PROMPTS.map(({ icon: IconCmp, text, prompt }) => (
                <button
                  key={text}
                  className="chatarea__suggestion"
                  onClick={() => setComposerDraft(prompt)}
                  title="填进输入框，不直接发送"
                >
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

          {/* 项目会话的流程正等着这一步：把可点的选项 / 该怎么答摆在上一条消息
              下面（问答本身已经由 sidecar 落进会话记录，这里只是"控件"）。 */}
          {flow?.waiting && (
            <div className="flow-wait">
              {flow.retryOptions?.length && retryHidden !== flow.step.id ? (
                <div className="chatarea__greeting-chips">
                  {flow.retryOptions.map(o => (
                    <button
                      key={o.label}
                      className="chatarea__suggestion"
                      onClick={() => {
                        // 「重填」= 收起这行让他重新打；「照样继续 / 跳过」= 拿被拒
                        // 的那句原话再提交一次（force），流程不再拦。
                        if (o.role === 'redo') {
                          setRetryHidden(flow.step.id);
                          return;
                        }
                        void answerFlow(activeSessionId, flow.retryAnswer ?? '', { force: true });
                      }}
                    >
                      <Icon.Bolt size={14} />
                      <span>{o.label}</span>
                    </button>
                  ))}
                </div>
              ) : null}

              {!flow.retryOptions?.length && flow.step.options?.length ? (
                <div className="chatarea__greeting-chips">
                  {flow.step.options.map(o => (
                    <button
                      key={o.value}
                      className="chatarea__suggestion"
                      onClick={() => void answerFlow(activeSessionId, o.value)}
                      title="选中这一条"
                    >
                      <Icon.Bolt size={14} />
                      <span>{o.label}</span>
                    </button>
                  ))}
                </div>
              ) : null}

              {!flow.retryOptions?.length && flow.step.input ? (
                <div className="flow-hint">
                  <span>
                    在下面说一句就行{flow.step.input.placeholder ? `，${flow.step.input.placeholder}` : ''}
                  </span>
                  {flow.step.input.optional ? (
                    <button
                      className="chatarea__suggestion"
                      onClick={() => void answerFlow(activeSessionId, '', { force: true })}
                      title="这一步可以跳过"
                    >
                      <span>这个先不填</span>
                    </button>
                  ) : null}
                </div>
              ) : null}
            </div>
          )}

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