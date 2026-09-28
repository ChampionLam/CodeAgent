import { useEffect } from 'react';
import { AppProvider, useApp } from './state/store';
import { Sidebar } from './components/Sidebar';
import { TopBar } from './components/TopBar';
import { StatusBar } from './components/StatusBar';
import { ChatArea } from './components/chat/ChatArea';
import { Composer } from './components/chat/Composer';
import ResumeBar from './components/chat/ResumeBar';
import { SubagentStrip } from './components/chat/SubagentStrip';
import CloseConfirmModal from './components/CloseConfirmModal';
import { UsagePage } from './pages/UsagePage';
import { SettingsSurface } from './pages/SettingsSurface';
import './styles/app.css';

function Shell() {
  const { view, setView, createSession } = useApp();

  // Esc is the way out of anything that replaced the chat. When a text field
  // has focus the first Esc only blurs it, so a half-typed model / API-key
  // draft is never thrown away by one stray keystroke.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // 新建会话。侧栏「新建会话」按钮上一直印着 Ctrl N，但从来没实现过
      // （2026-09-27 用户报「新建会话的快捷键不生效」）。
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === 'n') {
        if (document.querySelector('[role="dialog"]')) return;
        e.preventDefault();
        createSession();
        return;
      }
      // 审批卡现在在消息流里，不需要（也不该）用 Esc「关掉」它。
      if (e.key !== 'Escape') return;
      // 弹窗开着时 Esc 全交给弹窗自己：本监听挂 document、比弹窗的 window 监听先跑，
      // 这里要是先把输入框 blur 掉，弹窗那边看到的 activeElement 已经是 body，
      // 会误判成「没在打字」把填了一半的表单关掉（2026-09-27 实测踩到）。
      if (document.querySelector('[role="dialog"]')) return;
      const el = document.activeElement as HTMLElement | null;
      const editing = !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
      if (editing) {
        el.blur();
        return;
      }
      if (view !== 'chat') {
        // 2026-09-26：页面上有弹窗时不退页面。原来在设置页按 Esc 会「先退到对话、
        // 再把弹窗关掉」——因为本监听挂 document、比弹窗自己的 window 监听先跑。
        if (document.querySelector('[role="dialog"]')) return;
        e.preventDefault();
        setView('chat');
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [view, setView, createSession]);

  return (
    <div className="app-frame">
      {/* No app-drawn title bar: the window is frameless on Windows and the
          TopBar is the single title bar — brand, session and window buttons
          all live in it. A second bar here stacked two title bars. */}
      {view === 'settings' ? (
        /* 设置是「整窗」的：它连左侧栏一起盖住，只剩 TopBar（无边框窗口，
           那是唯一的标题栏，盖掉就没法拖动/关窗）。 */
        <SettingsSurface />
      ) : (
        <div className="app-body">
          <Sidebar />
          <main className="workspace">
            <TopBar />
            {view === 'chat' && (
              <div className="chat-column">
                <ChatArea />
                {/* 「上次任务没跑完 · 继续」——只在真有可续断点时出现 */}
                <ResumeBar />
                {/* 子代理进度：只在有批次时出现，跑完留最后一批的摘要 */}
                <SubagentStrip />
                <Composer />
              </div>
            )}
            {view === 'usage' && <UsagePage />}
          </main>
        </div>
      )}

      <StatusBar />

      {/* 关程序但还有任务在跑时的询问（主进程发 close:confirm 才出现） */}
      <CloseConfirmModal />

    </div>
  );
}

export function App() {
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  );
}