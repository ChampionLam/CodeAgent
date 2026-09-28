import { useState, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
import { useApp } from '../state/store';
import { Icon } from './Icon';
import { ToolCallCard } from './ToolCallCard';
import type { Message } from '../types';
import './Chat.css';

function timeOf(ts: number) {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className={`copyBtn ${copied ? 'copied' : ''}`}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
        } catch {
          /* ignore — clipboard may be unavailable in test env */
        }
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      title="复制代码"
      aria-label="复制代码"
    >
      {copied ? <Icon.Check size={11} /> : <Icon.Copy size={11} />}
      <span>{copied ? '已复制' : '复制'}</span>
    </button>
  );
}

function MessageBubble({ msg, isLast }: { msg: Message; isLast: boolean }) {
  const isUser = msg.role === 'user';
  const cursorAfter = isLast && msg.streaming;

  return (
    <div className={`msg ${isUser ? 'msgUser' : ''}`}>
      <div className={`avatarMsg ${isUser ? 'avatarUser' : 'avatarAssistant'}`} aria-hidden>
        {isUser ? '我' : <Icon.Sparkles size={13} />}
      </div>
      <div className="bubble">
        <div className={`role ${isUser ? 'roleUser' : ''}`} style={isUser ? { flexDirection: 'row-reverse' } : undefined}>
          <span>{isUser ? '你' : 'WorkBuddy'}</span>
          <span className="timestamp">{timeOf(msg.createdAt)}</span>
        </div>
        {isUser ? (
          <div className="userBubble">{msg.content}</div>
        ) : (
          <>
            <MarkdownView content={msg.content} />
            {cursorAfter && <span className="streaming-cursor" aria-hidden />}
          </>
        )}

        {msg.toolCalls?.map(tc => (
          <ToolCallCard key={tc.id} tool={tc} />
        ))}
      </div>
    </div>
  );
}

function MarkdownView({ content }: { content: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          code(props) {
            const { children, className, node, ...rest } = props;
            const match = /language-(\w+)/.exec(className || '');
            const isBlock = !!match || (typeof children === 'string' && (children as string).includes('\n'));
            if (!isBlock) {
              return <code className={className} {...rest}>{children}</code>;
            }
            const codeStr = String(children).replace(/\n$/, '');
            return (
              <div className="codeBlock">
                <div className="codeHeader">
                  <span className="codeLang">{match?.[1] ?? 'text'}</span>
                  <CopyButton text={codeStr} />
                </div>
                <pre><code className={className}>{children}</code></pre>
              </div>
            );
          },
          a({ children, href }) {
            return <a href={href} target="_blank" rel="noreferrer">{children}</a>;
          }
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export function Chat() {
  const app = useApp();
  const msgs = app.messages[app.activeSessionId] || [];

  if (msgs.length === 0) {
    return <EmptyState />;
  }

  return (
    <div className="chat">
      <div className="inner">
        {msgs.map((m, i) => (
          <MessageBubble key={m.id} msg={m} isLast={i === msgs.length - 1} />
        ))}
      </div>
    </div>
  );
}

function EmptyState() {
  const app = useApp();
  const suggestions = [
    '帮我看一下 src/api/users.ts 的保存逻辑',
    '给 FastAPI 项目加一层缓存',
    '把这段 SQL 加个合适的索引',
    '解释一下 git rebase 和 merge 的区别'
  ];
  return (
    <div className="empty">
      <div className="emptyIcon"><Icon.Sparkles size={22} /></div>
      <div className="emptyTitle">开始一个新的会话</div>
      <div className="emptyHint">
        WorkBuddy 可以读你的代码、跑命令、查文件。<br />
        所有写操作都会先征求你的同意。
      </div>
      <div className="suggestRow">
        {suggestions.map(s => (
          <button
            key={s}
            className="suggestChip"
            onClick={() => app.send(s)}
          >{s}</button>
        ))}
      </div>
    </div>
  );
}