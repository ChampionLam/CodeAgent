import { useState } from 'react';
import { useApp } from '../../state/store';
import type { ApprovalRequest } from '../../types';
import { Icon } from '../Icon';
import './ApprovalCard.css';

type Scope = 'once' | 'session' | 'always';

/**
 * 会话内审批卡（替代原弹窗）：
 * 危险操作被拦时，卡片作为一条消息落在它所属的助手回合里——
 * 不遮对话、不禁用输入，命令默认折叠，用户点按钮就地决定。
 * 设计来源：docs/2026-09-25-permission-inline-approval-design.md §3。
 *
 * 2026-10-14（用户口径「应该要加上类似 hermes 这种 session 内允许、一直允许」）：
 * 允许不再只有一次 —— 「本会话内允许」记到本次会话结束，「一直允许」落 config.json
 * 重启后仍然有效。hardline（不可逆操作）后端不给这两个口子，界面也就不画。
 */
export function ApprovalCard({ request }: { request: ApprovalRequest }) {
  const { approveApproval, denyApproval } = useApp();
  const [open, setOpen] = useState(false);
  const [scopeNote, setScopeNote] = useState('');

  const pending = request.decision == null;
  const allowAlways = request.canAlwaysAllow === true;
  const allowSession = request.canSessionAllow === true;

  const allow = (scope: Scope) => {
    approveApproval(request.id, scope);
    setScopeNote(scope === 'always' ? '已允许（以后不再问同一操作）'
      : scope === 'session' ? '已允许（本次会话内不再问）' : '');
  };

  return (
    <div className={`apcard ${pending ? 'is-pending' : 'is-resolved'} apcard--${request.decision ?? 'pending'}`}>
      {/* 头部：一行「需要你确认」+ 工具的人话描述 */}
      <div className="apcard__head">
        <span className="apcard__icon">
          <Icon.Shield size={14} />
        </span>
        <span className="apcard__title">{request.title}</span>
        {request.reason && <span className="apcard__reason">{request.reason}</span>}
      </div>

      {/* 命令/参数原文：默认折叠，点开才见全文 */}
      <button
        type="button"
        className="apcard__toggle"
        onClick={() => setOpen(v => !v)}
        aria-expanded={open}
      >
        <Icon.Terminal size={12} />
        <span className="apcard__toggle-label">{request.toolLabel}</span>
        <span className="apcard__toggle-hint">
          {open ? '收起详情' : request.detailHint}
        </span>
        <span className={`apcard__chevron ${open ? 'is-open' : ''}`}>
          <Icon.ChevronDown size={13} />
        </span>
      </button>

      {open && (
        <pre className="apcard__payload mono">{request.payload}</pre>
      )}

      {/* 决定区：待决时给按钮；决定后就地变成一行结果 */}
      {pending ? (
        <div className="apcard__foot">
          <button
            className="btn btn--sm apcard__deny"
            onClick={() => denyApproval(request.id)}
            title="拒绝执行，Agent 将改用其他方式继续"
          >
            <Icon.X size={12} />
            拒绝
          </button>
          <div className="apcard__foot-right">
            {allowSession && (
              <button
                className="btn btn--sm"
                onClick={() => allow('session')}
                title="本次会话内同一种操作不再问"
              >
                本会话内允许
              </button>
            )}
            {allowAlways && (
              <button
                className="btn btn--sm"
                onClick={() => allow('always')}
                title="以后同一种操作都不再问（可在设置里撤销）"
              >
                一直允许
              </button>
            )}
            <button
              className="btn btn--sm btn--primary apcard__allow"
              onClick={() => allow('once')}
              title="仅本次允许"
            >
              <Icon.Check size={12} />
              允许
            </button>
          </div>
        </div>
      ) : (
        <div className="apcard__result">
          {request.decision === 'denied' ? (
            <><Icon.X size={12} /><span>已拒绝，Agent 将改用其他方式继续</span></>
          ) : (
            <><Icon.Check size={12} /><span>{scopeNote || '已允许'}</span></>
          )}
        </div>
      )}
    </div>
  );
}