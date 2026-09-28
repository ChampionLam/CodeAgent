import { useMemo, useState } from 'react';
import { useApp } from '../state/store';
import { Icon } from './Icon';
import { SkillsToolsPanel } from './SkillsToolsPanel';
import type { Session } from '../types';
import './Sidebar.css';

function relativeTime(ts: number) {
  const diff = Date.now() - ts;
  const m = Math.floor(diff / 60_000);
  if (m < 1) return '刚刚';
  if (m < 60) return `${m} 分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时前`;
  const d = Math.floor(h / 24);
  if (d === 1) return '昨天';
  if (d < 7) return `${d} 天前`;
  return `${Math.floor(d / 7)} 周前`;
}

function groupOf(ts: number): '今天' | '昨天' | '本周' | '更早' {
  const d = new Date(ts);
  const today = new Date();
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  if (ts >= startOfToday) return '今天';
  if (ts >= startOfToday - 86_400_000) return '昨天';
  if (ts >= startOfToday - 7 * 86_400_000) return '本周';
  return '更早';
}

export function Sidebar() {
  const {
    sessions, activeSessionId, selectSession, createSession,
    sidebarCollapsed, toggleSidebar, view, setView
  } = useApp();

  const [query, setQuery] = useState('');
  const [skillsPanelOpen, setSkillsPanelOpen] = useState(false);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(s => s.title.toLowerCase().includes(q));
  }, [sessions, query]);

  const groups = useMemo(() => {
    const pinned = filtered.filter(s => s.pinned);
    const rest = filtered.filter(s => !s.pinned);
    const buckets: Array<{ label: string; items: Session[] }> = [];
    if (pinned.length) buckets.push({ label: '置顶', items: pinned });

    const order: Array<'今天' | '昨天' | '本周' | '更早'> = ['今天', '昨天', '本周', '更早'];
    for (const label of order) {
      const items = rest
        .filter(s => groupOf(s.updatedAt) === label)
        .sort((a, b) => b.updatedAt - a.updatedAt);
      if (items.length) buckets.push({ label, items });
    }
    return buckets;
  }, [filtered]);

  if (sidebarCollapsed) {
    return (
      <aside className="sidebar sidebar--collapsed">
        <button className="sidebar__rail-btn" onClick={toggleSidebar} title="展开侧栏">
          <Icon.ChevronRight size={16} />
        </button>
        <button className="sidebar__rail-btn" onClick={createSession} title="新建会话">
          <Icon.Plus size={16} />
        </button>
        <div className="sidebar__rail-spacer" />
        <button
          className="sidebar__rail-btn"
          onClick={() => setSkillsPanelOpen(true)}
          title="技能与工具"
        >
          <Icon.Sparkles size={16} />
        </button>
        <button
          className={`sidebar__rail-btn ${view === 'usage' ? 'is-active' : ''}`}
          onClick={() => setView('usage')}
          title="用量统计"
        >
          <Icon.ChartBar size={16} />
        </button>
        <button
          className={`sidebar__rail-btn ${view === 'settings' ? 'is-active' : ''}`}
          onClick={() => setView('settings')}
          title="设置"
        >
          <Icon.Settings size={16} />
        </button>
        {skillsPanelOpen && <SkillsToolsPanel onClose={() => setSkillsPanelOpen(false)} />}
      </aside>
    );
  }

  return (
    <aside className="sidebar">
      <header className="sidebar__head">
        <button className="sidebar__new" onClick={createSession}>
          <Icon.Plus size={15} />
          <span>新建会话</span>
          <kbd className="sidebar__kbd">{/Mac/i.test(navigator.userAgent) ? '⌘ N' : 'Ctrl N'}</kbd>
        </button>
        <button className="sidebar__collapse" onClick={toggleSidebar} title="收起侧栏">
          <Icon.ChevronLeft size={15} />
        </button>
      </header>

      <div className="sidebar__search">
        <Icon.Search size={13} />
        <input
          value={query}
          onChange={e => setQuery(e.target.value)}
          placeholder="搜索会话"
          aria-label="搜索会话"
        />
        {query && (
          <button className="sidebar__search-clear" onClick={() => setQuery('')}>
            <Icon.X size={11} />
          </button>
        )}
      </div>

      <nav className="sidebar__list" aria-label="会话列表">
        {groups.length === 0 && (
          <div className="sidebar__empty">没有匹配的会话</div>
        )}
        {groups.map(g => (
          <section key={g.label} className="sidebar__group">
            <div className="sidebar__group-label">{g.label}</div>
            {g.items.map(s => {
              const active = s.id === activeSessionId && view === 'chat';
              return (
                <button
                  key={s.id}
                  className={`session-row ${active ? 'is-active' : ''}`}
                  onClick={() => selectSession(s.id)}
                >
                  <span className={`session-row__accent ${active ? 'is-active' : ''}`} />
                  <span className="session-row__main">
                    <span className="session-row__title">
                      {s.pinned && <Icon.Pin size={11} className="session-row__pin" />}
                      {s.title}
                    </span>
                    <span className="session-row__meta">
                      <span>{relativeTime(s.updatedAt)}</span>
                    </span>
                  </span>
                  {active && <span className="session-row__live" aria-hidden />}
                </button>
              );
            })}
          </section>
        ))}
      </nav>

      <footer className="sidebar__foot">
        <button
          className="sidebar__nav"
          onClick={() => setSkillsPanelOpen(true)}
        >
          <Icon.Sparkles size={15} />
          <span>技能与工具</span>
        </button>
        <button
          className="sidebar__nav"
          onClick={() => setView('usage')}
          data-active={view === 'usage'}
        >
          <Icon.ChartBar size={15} />
          <span>用量统计</span>
        </button>
        <button
          className="sidebar__nav"
          onClick={() => setView('settings')}
          data-active={view === 'settings'}
        >
          <Icon.Settings size={15} />
          <span>设置</span>
        </button>
      </footer>
      {/* 2026-09-26 修复：展开态也要渲染面板。原来只有收起分支挂了它，
          于是默认（展开）状态下点「技能与工具」只改了 state、界面上什么都不出。 */}
      {skillsPanelOpen && <SkillsToolsPanel onClose={() => setSkillsPanelOpen(false)} />}
    </aside>
  );
}
