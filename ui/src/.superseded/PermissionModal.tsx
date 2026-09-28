import { useEffect } from 'react';
import { useApp } from '../state/store';
import { Icon } from './Icon';
import './PermissionModal.css';

const LEVEL_TEXT = {
  L1: { title: '读取操作 · 安全', sub: '此操作仅读取文件或运行只读命令。' },
  L2: { title: '修改操作 · 需要确认', sub: '此操作会修改你工作区里的文件或安装依赖。' },
  L3: { title: '高风险操作 · 必须确认', sub: '此操作不可逆或影响范围较大，请仔细检查。' }
} as const;

export function PermissionModal() {
  const app = useApp();
  const p = app.pendingPermission;
  if (!p) return null;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); app.denyPermission(); }
      if (e.key === 'Enter') { e.preventDefault(); app.approvePermission(false); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [app]);

  const showAlways = p.level !== 'L3';
  const meta = LEVEL_TEXT[p.level];

  return (
    <div
      className="backdrop modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="perm-title"
      onClick={(e) => { if (e.target === e.currentTarget) app.denyPermission(); }}
    >
      <div className="card modal-card" data-level={p.level}>
        <div className="head">
          <div className="iconBubble" data-level={p.level}>
            {p.level === 'L3' ? <Icon.Warning size={18} /> :
             p.level === 'L2' ? <Icon.Shield size={18} /> :
             <Icon.Lock size={18} />}
          </div>
          <div className="titleBlock">
            <div id="perm-title" className="title">{meta.title}</div>
            <div className="subtitle">{p.tool} · {p.summary}</div>
          </div>
          <button
            className="closeBtn"
            onClick={app.denyPermission}
            aria-label="关闭"
          >
            <Icon.X size={14} />
          </button>
        </div>

        <div className="body">
          <div className="row">
            <span className="rowLabel">风险等级</span>
            <span className="rowValue">
              <span style={{
                fontFamily: 'var(--font-mono)',
                fontSize: 12,
                padding: '1px 6px',
                borderRadius: 4,
                background: p.level === 'L3' ? 'var(--semantic-danger-soft)' :
                            p.level === 'L2' ? 'var(--semantic-warn-soft)' :
                            'var(--semantic-info-soft)',
                color: p.level === 'L3' ? 'var(--semantic-danger)' :
                       p.level === 'L2' ? 'var(--semantic-warn)' :
                       'var(--semantic-info)',
                marginRight: 8
              }}>{p.level}</span>
              {meta.sub}
            </span>
          </div>

          <div className="row" style={{ alignItems: 'flex-start' }}>
            <span className="rowLabel">要执行</span>
            <div style={{ flex: 1 }}>
              <div className="payloadBox">{p.payload}</div>
            </div>
          </div>

          <div className="row" style={{ alignItems: 'flex-start' }}>
            <span className="rowLabel">为什么</span>
            <div className="reason" style={{ flex: 1, margin: 0 }}>{p.reason}</div>
          </div>
        </div>

        <div className="foot">
          <span className="kbdHint">
            <span><span className="kbd">Esc</span>拒绝</span>
            <span><span className="kbd">Enter</span>允许一次</span>
          </span>
          <div className="spacer" />
          {p.level === 'L3' && (
            <span className="l3Note">高风险操作不可设为"总是允许"</span>
          )}
          <button className="btn btnDeny" onClick={app.denyPermission}>
            拒绝
          </button>
          <button className="btn btnAllowOnce" onClick={() => app.approvePermission(false)}>
            允许一次
          </button>
          {showAlways && (
            <button className="btn btnAlways" onClick={() => app.approvePermission(true)}>
              <Icon.Check size={12} />
              总是允许
            </button>
          )}
        </div>
      </div>
    </div>
  );
}