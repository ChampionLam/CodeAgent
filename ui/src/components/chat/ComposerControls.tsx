import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../state/store';
import { Icon } from '../Icon';
import { ModelPanel } from './ModelPanel';
import './ComposerControls.css';

/** 小气泡菜单：按钮 + 浮层，点外面或 Esc 关闭。
    modal=true 时不挂浮层，而是盖一层居中弹窗（遮罩 + 点遮罩关闭）；
    触发按钮的位置和开关行为不变。 */
function Chip({
  value, dot, menuClass, modal, children
}: {
  value: string;
  dot?: 'ok' | 'warn' | null;
  /** 菜单容器的额外类名（模型面板要放开宽度、去 padding）。 */
  menuClass?: string;
  /** 弹窗形态：不锚在按钮上，盖居中 modal（模型面板用）。 */
  modal?: boolean;
  children: (close: () => void) => React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onEsc = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onEsc);
    };
  }, [open]);

  return (
    <div className="cctl" ref={ref}>
      <button
        type="button"
        className={`cctl__btn ${open ? 'is-open' : ''}`}
        onClick={() => setOpen(v => !v)}
        aria-haspopup={modal ? 'dialog' : 'menu'}
        aria-expanded={open}
      >
        <span className="cctl__value">{value}</span>
        {dot && (
          <span
            className={`cctl__dot cctl__dot--${dot}`}
            title={dot === 'ok' ? 'key 已配置' : '未配置 key'}
            aria-hidden
          />
        )}
        <Icon.ChevronDown size={10} />
      </button>
      {open && !modal && (
        <div className={`cctl__menu ${menuClass ?? ''}`} role="menu">
          {children(() => setOpen(false))}
        </div>
      )}
      {open && modal && (
        <div
          className="cctl__modal-mask"
          onMouseDown={e => { if (e.target === e.currentTarget) setOpen(false); }}
        >
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}

/**
 * 输入区控制条：**只剩一个模型 chip**。
 *
 * 点开是**贴着 chip 浮出来的**「左列表 + 右详情」面板（ModelPanel）：模型在左边挑，
 * 思考强度 / 上下文窗口
 * 在右边用二级弹窗调。原来那个单独的「思考」chip 撤了——用户口径：右下那个思考模式
 * 不要了，一个入口就够。
 *
 * 面板里的选项都是**会话级**的：只作用于当前会话的每次请求，不写回 config.json。
 */
export function ComposerControls() {
  const { models, activeModelId } = useApp();
  const active = models.find(m => m.id === activeModelId);
  const dot: 'ok' | 'warn' | null = active ? (active.hasKey === false ? 'warn' : 'ok') : null;

  return (
    <div className="cctl-row">
      <Chip
        value={active?.name ?? (models.length ? '未选' : '未配置')}
        dot={dot}
        menuClass="cctl__menu--mpanel"
      >
        {close => <ModelPanel onClose={close} />}
      </Chip>
    </div>
  );
}