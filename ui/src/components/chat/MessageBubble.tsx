import { memo, useState, type ReactNode, useMemo } from 'react';
import type { Message } from '../../types';
import { Icon } from '../Icon';
import { baseName, fileUrl } from '../../state/live';
import { isImagePath } from '../../lib/attachmentKinds';
import {
  copyArtifactPath, humanSize, isImageArtifact,
  openArtifact, revealArtifact
} from '../../lib/artifacts';
import type { MessageArtifact } from '../../types';
import { useApp } from '../../state/store';
import { fmtTokens } from '../../state/usage';
import { MarkdownView } from './MarkdownView';
import { extractLocalFiles, fileNameOf, kindOf } from '../../lib/localFileLinks';
import { ApprovalCard } from './ApprovalCard';
import './MessageBubble.css';

function timeOf(ts: number) {
  const d = new Date(ts);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function ActionBar({ children, align = 'left' }: { children: ReactNode; align?: 'left' | 'right' }) {
  return <div className={`msg__actions msg__actions--${align}`}>{children}</div>;
}

/** 一张文件卡：缩略图/图标 + 名字 + 大小 + 三个动作（打开 / 文件夹 / 复制路径）。 */
function ArtifactCard(props: { artifact: MessageArtifact }) {
  const { artifact } = props;
  const [copied, setCopied] = useState(false);
  return (
    <div className="afile" title={artifact.path}>
      <span className="afile__thumb">
        {isImageArtifact(artifact)
          ? <img src={fileUrl(artifact.path)} alt="" loading="lazy" />
          : <Icon.File size={15} />}
      </span>
      <span className="afile__text">
        <span className="afile__name">{artifact.name}</span>
        <span className="afile__meta">{humanSize(artifact.size)}</span>
      </span>
      <span className="afile__actions">
        <button className="afile__act" onClick={() => void openArtifact(artifact.path)}>打开</button>
        <button className="afile__act" onClick={() => void revealArtifact(artifact.path)}>文件夹</button>
        <button
          className="afile__act"
          onClick={() => void copyArtifactPath(artifact.path).then(ok => {
            if (!ok) return;
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          })}
        >{copied ? '已复制' : '复制路径'}</button>
      </span>
    </div>
  );
}

function MessageBubbleInner({ message }: { message: Message }) {
  const { thinkingDisplay } = useApp();
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try { await navigator.clipboard.writeText(message.content); } catch { /* noop */ }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  };

  if (message.role === 'user') {
    return (
      <div className="msg msg--user">
        <div className="msg__row">
          <div className="msg__user-wrap">
            {message.images && message.images.length > 0 && (
              <div className="msg__images">
                {message.images.map(p => {
                  // 历史消息里的图是 data URL（引擎发出那版的原文），直接给 <img> 用；
                  // 其它情况仍是磁盘路径 → 走 file:// 转换。
                  const inline = p.startsWith('data:image/');
                  return inline || isImagePath(p) ? (
                    <img
                      key={inline ? `inline-${p.length}-${p.slice(30, 46)}` : p}
                      className="msg__image"
                      src={inline ? p : fileUrl(p)}
                      alt={inline ? '图片' : baseName(p)}
                      title={inline ? '' : p}
                      onError={e => { e.currentTarget.style.display = 'none'; }}
                    />
                  ) : (
                    <span key={p} className="msg__file" title={p}>
                      <Icon.File size={13} />
                      <span className="msg__file-name">{baseName(p)}</span>
                    </span>
                  );
                })}
              </div>
            )}
            {/* 用户消息也走 markdown：粘贴的代码/表格/链接照常渲染（2026-09-26），
                纯短文本的观感由 md--user 的紧凑版式保住。 */}
            <div className="msg__user-bubble">
              <MarkdownView content={message.content} variant="user" />
            </div>
          </div>
        </div>
        <ActionBar align="right">
          <span className="msg__time mono">{timeOf(message.createdAt)}</span>
          <button className="msg__action" onClick={copy} title="复制">
            {copied ? <Icon.Check size={12} /> : <Icon.Copy size={12} />}
          </button>
          <button className="msg__action" title="编辑">
            <Icon.Edit size={12} />
          </button>
        </ActionBar>
      </div>
    );
  }

  if (message.role === 'system') {
    return (
      <div className="msg msg--system">
        <span className="msg__system-pill">{message.content}</span>
      </div>
    );
  }

  // 正文里的 [[file: 路径]] 标记剥掉，文件只作为末尾的附件卡出现（用户口径：
  // 「不是所有的本地文件都转换成这样,你只需要在最后结尾加上这个文件就好」）。
  const extracted = useMemo(() => extractLocalFiles(message.content ?? ''), [message.content]);
  const bodyText = extracted.text;
  const hasText = bodyText.length > 0;
  const thinking = message.thinking ?? '';
  const artifacts = useMemo<MessageArtifact[]>(() => {
    const backend = message.artifacts ?? [];
    const seen = new Set(backend.map(a => a.path));
    const extra: MessageArtifact[] = extracted.paths
      .filter(path => !seen.has(path))
      .map(path => ({ path, name: fileNameOf(path), size: -1, kind: kindOf(path) }));
    return [...backend, ...extra];
  }, [message.artifacts, extracted]);
  const hasThinking = thinking.length > 0;
  const stillThinking = hasThinking && message.streaming && !hasText;
  // 显示方式由用户控制（设置页「思考显示」）：折叠 / 展开 / 隐藏。
  // 隐藏时，生成过程中仍留一行「思考中」——模型思考的那几秒里没有任何正文，
  // 连指示都没有的话界面看着像卡死。
  const showThinkingBox = hasThinking && thinkingDisplay !== 'hidden';
  const showLiveOnly = hasThinking && thinkingDisplay === 'hidden' && stillThinking;

  return (
    <div className={`msg msg--assistant ${message.streaming ? 'is-streaming' : ''}`}>
      <div className="msg__row">
        <div className="msg__avatar" aria-hidden>
          <Icon.Sparkles size={14} />
        </div>
        <div className="msg__content">
          {/* Reasoning channel. Collapsed by default: while the model thinks
              there is no answer text yet, so this is the only live feedback. */}
          {showThinkingBox && (
            <details
              className={`msg__thinking ${stillThinking ? 'is-live' : ''}`}
              open={thinkingDisplay === 'expanded' && !stillThinking}
            >
              <summary className="msg__thinking-head">
                <Icon.Sparkles size={12} />
                {stillThinking ? '思考中…' : `思考过程（${thinking.length} 字）`}
              </summary>
              <div className="msg__thinking-body">{thinking}</div>
            </details>
          )}

          {showLiveOnly && (
            <div className="msg__thinking msg__thinking--live">
              <Icon.Sparkles size={12} />
              <span>思考中…</span>
            </div>
          )}

          {hasText && <MarkdownView content={bodyText} streaming={message.streaming} />}

          {/* 本轮产出的文件。用户要求：文件要直接出现在对话里，而不是只有一行路径。 */}
          {artifacts.length > 0 && (
            <div className="msg__artifacts">
              {artifacts.map(a => <ArtifactCard key={a.path} artifact={a} />)}
            </div>
          )}

          {/* 会话内审批卡：落在这条助手回合里，不遮对话 */}
          {(message.approvals ?? []).map(a => (
            <ApprovalCard key={a.id} request={a} />
          ))}

          {/* 工具操作卡不再展示（2026-09-25 用户口径：「我不要这种什么看目录这些操作显示」）。
              他要的是对话本身，不是我的执行流水账；工具失败时我会在回复里说清楚。 */}
        </div>
      </div>

      {!message.streaming && hasText && (
        <ActionBar>
          <span className="msg__time mono">{timeOf(message.createdAt)}</span>
          <button className="msg__action" onClick={copy} title="复制回答">
            {copied ? <Icon.Check size={12} /> : <Icon.Copy size={12} />}
          </button>
          <button className="msg__action" title="重新生成">
            <Icon.Refresh size={12} />
          </button>
          <span className="msg__tokens mono">
            {message.usage?.totalTokens ? `${fmtTokens(message.usage.totalTokens)} tokens` : ''}
          </span>
        </ActionBar>
      )}
    </div>
  );
}

// Memoised: while an answer streams only the newest bubble's props change, so the
// already-rendered history stops re-rendering on every chunk.
export const MessageBubble = memo(MessageBubbleInner);
