import { useState } from 'react';
import { Icon, ToolGlyph, type ToolName } from './Icon';
import type { ToolCall, ToolStatus } from '../types';
import './ToolCallCard.css';

const STATUS_LABEL: Record<ToolStatus, string> = {
  running: '运行中',
  success: '成功',
  failed: '失败',
  rejected: '已拒绝',
  awaiting: '等待审批'
};

export function ToolCallCard({ tool }: { tool: ToolCall }) {
  const [open, setOpen] = useState(tool.status === 'running' || tool.status === 'awaiting');
  const Glyph = ToolGlyph[tool.name as ToolName] ?? ToolGlyph.default;

  return (
    <div className="toolCard" data-status={tool.status}>
      <button
        className="toolHead"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
      >
        <span className="toolIcon" aria-hidden><Glyph size={12} /></span>
        <span className="toolName">{tool.name}</span>
        <span className="toolSummary">{tool.summary}</span>
        <span className="statusPill" data-s={tool.status}>
          <span className="statusDot" />
          {STATUS_LABEL[tool.status]}
        </span>
        <span className={`expandIcon ${open ? 'open' : ''}`}>
          <Icon.ChevronRight size={12} />
        </span>
      </button>

      {open && (
        <div className="toolBody">
          {tool.level && (
            <>
              <div className="sectionLabel">风险等级</div>
              <div className="kv">
                <span className="k">level</span>
                <span className="v">{tool.level}</span>
              </div>
            </>
          )}
          <div className="sectionLabel">参数</div>
          <div className="kv">
            {Object.entries(tool.args).map(([k, v]) => (
              <div key={k} style={{ display: 'contents' }}>
                <span className="k">{k}</span>
                <span className="v">
                  {typeof v === 'string' ? (
                    <span className="code">{v}</span>
                  ) : (
                    JSON.stringify(v, null, 2)
                  )}
                </span>
              </div>
            ))}
          </div>

          {tool.result && (
            <>
              <div className="sectionLabel">返回</div>
              <div className="code">{tool.result}</div>
            </>
          )}

          {tool.status === 'running' && !tool.result && (
            <>
              <div className="sectionLabel">运行中</div>
              <div className="code" style={{ color: 'var(--text-tertiary)' }}>
                <span className="shimmer" style={{
                  display: 'inline-block',
                  width: 60, height: 10,
                  borderRadius: 3, verticalAlign: 'middle'
                }}> </span>
                <span style={{ marginLeft: 6 }}>正在等待子进程返回...</span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}