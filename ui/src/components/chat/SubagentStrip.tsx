/**
 * 子代理进度条（用户 2026-09-26 选的 B 方案：常驻一行，放在输入区上方）。
 *
 * 为什么不做进消息流：delegate 是**非阻塞**的（派发 ~3ms 就返回），节点在后台继续跑，
 * 挂进消息里就得处理「消息已结束、事件还在来」这条别扭路径。B 方案只读引擎的
 * subagent.* 事件，不碰消息结构。
 * 如实说明代价：只在运行时看得见，重开应用这条不复活——合并后的结果照样在那条消息里。
 * 批次跑完留 3 秒就自动收掉（用户口径：完成了就关掉，不用一直展示）。
 */
import { useState } from 'react';
import { useApp } from '../../state/store';
import { Icon } from '../Icon';
import './SubagentStrip.css';

const STATUS_TEXT: Record<string, string> = {
  running: '进行中',
  done: '已完成',
  failed: '失败',
  interrupted: '已中断',
};

function clip(s: string, n = 64): string {
  const one = (s || '').replace(/\s+/g, ' ').trim();
  return one.length > n ? one.slice(0, n) + '…' : one;
}

export function SubagentStrip() {
  const { subagentBatch } = useApp();
  const [open, setOpen] = useState(false);

  if (!subagentBatch || subagentBatch.nodes.length === 0) return null;

  const nodes = subagentBatch.nodes;
  const total = nodes.length;
  const running = nodes.filter(n => n.status === 'running').length;
  const bad = nodes.filter(n => n.status === 'failed' || n.status === 'interrupted').length;
  const slowest = nodes.reduce((acc, n) => Math.max(acc, n.elapsedS ?? 0), 0);

  const summary = running > 0
    ? `子代理 ${total} 个 · 进行中 ${total - running}/${total}`
    : bad > 0
      ? `子代理 ${total} 个 · ${bad} 个没跑成`
      : `子代理 ${total} 个 · 已完成${slowest ? `（${slowest.toFixed(1)}s）` : ''}`;

  return (
    <div className={`substrip ${running > 0 ? 'is-live' : ''} ${open ? 'is-open' : ''}`}>
      <button
        type="button"
        className="substrip__head"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
        title="子代理节点"
      >
        <span className="substrip__glyph" aria-hidden>
          {running > 0 ? <span className="substrip__spin" /> : <Icon.Sparkles size={12} />}
        </span>
        <span className="substrip__summary">{summary}</span>
        {subagentBatch.rssPeakMb ? (
          <span className="substrip__mem mono">峰值 {subagentBatch.rssPeakMb}MB</span>
        ) : null}
        <span className={`substrip__chevron ${open ? 'is-open' : ''}`} aria-hidden>
          <Icon.ChevronDown size={12} />
        </span>
      </button>

      {open && (
        <ul className="substrip__nodes">
          {nodes.map(n => (
            <li key={n.nodeId} className={`substrip__node is-${n.status}`}>
              <span className="substrip__id mono">{n.nodeId}</span>
              <span className="substrip__state">{STATUS_TEXT[n.status] ?? n.status}</span>
              {n.status === 'running' && n.tool ? (
                <span className="substrip__tool mono">{n.tool}</span>
              ) : null}
              {n.elapsedS ? (
                <span className="substrip__dur mono">{n.elapsedS.toFixed(1)}s</span>
              ) : null}
              {n.lastText ? <span className="substrip__text">{clip(n.lastText)}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
