import { useEffect, useState } from 'react';
import { useApp } from '../state/store';
import { Icon } from './Icon';
import './TopBar.css';

/**
 * Window buttons for the frameless Electron window (the TopBar is the only
 * title bar). Absent when the UI runs in a plain browser, where the OS chrome
 * already provides them — hence the optional access.
 */
const winControls = (
  window as unknown as {
    agent?: {
      winControls?: {
        action: (a: 'minimize' | 'maximize' | 'close') => Promise<boolean>;
        isMaximized: () => Promise<boolean>;
        onMaximizedChange: (cb: (v: boolean) => void) => () => void;
      };
    };
  }
).agent?.winControls;

export function TopBar() {
  const { sessions, activeSessionId, view, setView } = useApp();
  const session = sessions.find(s => s.id === activeSessionId);
  const [maximized, setMaximized] = useState(false);

  useEffect(() => {
    if (!winControls) return;
    void winControls.isMaximized().then(setMaximized).catch(() => undefined);
    return winControls.onMaximizedChange(setMaximized);
  }, []);

  return (
    <header className="topbar">
      <div className="topbar__left">
        <span className="topbar__brand">
          <Icon.Robot size={18} className="topbar__logo" />
          <span className="topbar__brand-name">CodeAgent</span>
        </span>
      </div>

      <div className="topbar__right">
        {/* 原来这颗齿轮没有 onClick —— 用户会挨个点，点了没反应就是死按钮。
            接成进设置（本就在设置里时置灰，避免自我跳转）。 */}
        <button
          className={'btn btn--ghost btn--icon' + (view === 'settings' ? ' is-active' : '')}
          title="设置"
          aria-label="设置"
          disabled={view === 'settings'}
          onClick={() => setView('settings')}
        >
          <Icon.Cog size={15} />
        </button>

        {winControls && (
          <div className="winbtns">
            <button
              className="winbtn"
              title="最小化"
              aria-label="最小化"
              onClick={() => void winControls.action('minimize')}
            >
              <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
                <path d="M0 5.5h10" stroke="currentColor" strokeWidth="1" />
              </svg>
            </button>
            <button
              className="winbtn"
              title={maximized ? '向下还原' : '最大化'}
              aria-label={maximized ? '向下还原' : '最大化'}
              onClick={() => void winControls.action('maximize').then(setMaximized)}
            >
              {maximized ? (
                <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
                  <path d="M3 3h5.5v5.5H3z" fill="none" stroke="currentColor" strokeWidth="1" />
                  <path d="M1.5 7V1.5H7" fill="none" stroke="currentColor" strokeWidth="1" />
                </svg>
              ) : (
                <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
                  <rect x="0.5" y="0.5" width="9" height="9" fill="none" stroke="currentColor" strokeWidth="1" />
                </svg>
              )}
            </button>
            <button
              className="winbtn winbtn--close"
              title="关闭"
              aria-label="关闭"
              onClick={() => void winControls.action('close')}
            >
              <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
                <path d="M0.5 0.5l9 9M9.5 0.5l-9 9" stroke="currentColor" strokeWidth="1" />
              </svg>
            </button>
          </div>
        )}
      </div>
    </header>
  );
}