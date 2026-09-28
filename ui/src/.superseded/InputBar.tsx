import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { useApp } from '../state/store';
import { Icon } from './Icon';
import './InputBar.css';

const MAX_ROWS = 6;
const LINE_PX = 22; // ~ line-height 1.6 * font-size 14

export function InputBar() {
  const app = useApp();
  const [value, setValue] = useState('');
  const ref = useRef<HTMLTextAreaElement>(null);
  const model = app.models.find(m => m.id === app.activeModelId);

  // Auto-grow
  useEffect(() => {
    const ta = ref.current;
    if (!ta) return;
    ta.style.height = 'auto';
    const next = Math.min(MAX_ROWS * LINE_PX, ta.scrollHeight);
    ta.style.height = `${next}px`;
  }, [value]);

  const submit = () => {
    const text = value.trim();
    if (!text) return;
    app.send(text);
    setValue('');
    if (ref.current) ref.current.style.height = 'auto';
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const placeholder = app.pendingPermission
    ? '等待权限确认...'
    : '向 WorkBuddy 提问，回车发送，Shift+回车换行';

  return (
    <div className="inputBar">
      <div className="inputWrap">
        <textarea
          ref={ref}
          className="textarea"
          value={value}
          onChange={e => setValue(e.target.value)}
          onKeyDown={onKey}
          placeholder={placeholder}
          rows={1}
          aria-label="输入消息"
          disabled={!!app.pendingPermission}
        />
        <div className="toolbar">
          <div className="toolLeft">
            <button className="modelMini" title="当前模型">
              <span style={{
                width: 6, height: 6, borderRadius: '50%',
                background: 'var(--semantic-success)'
              }} />
              {model?.model ?? '未选模型'}
            </button>
            <button className="toolBtn" title="附加文件" aria-label="附加文件">
              <Icon.Plus size={14} />
            </button>
          </div>

          {app.isStreaming ? (
            <button
              className="sendBtn sendStop"
              onClick={app.stopStreaming}
              title="停止生成 (Esc)"
              aria-label="停止生成"
            >
              <Icon.Stop size={13} />
            </button>
          ) : (
            <button
              className="sendBtn"
              onClick={submit}
              disabled={!value.trim() || !!app.pendingPermission}
              title="发送 (Enter)"
              aria-label="发送"
            >
              <Icon.Send size={14} />
            </button>
          )}
        </div>
      </div>
      <div className="hint">
        <span>WorkBuddy 可能会犯错，所有写操作前都会征求你的同意。</span>
        <span><span className="kbd">Enter</span> 发送 · <span className="kbd">Shift+Enter</span> 换行</span>
      </div>
    </div>
  );
}