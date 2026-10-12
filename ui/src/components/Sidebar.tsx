import { useMemo, useState } from 'react';
import { useApp, NEW_PROJECT_TITLE } from '../state/store';
import { Icon } from './Icon';
import { SkillsToolsPanel } from './SkillsToolsPanel';
import {
  PROJECT_PREVIEW_N,
  readSidebarSections,
  splitSidebarSessions,
  slicePreviewProjects,
  writeSidebarSections,
  type SidebarSectionsState,
} from '../sidebarSections';
import type { Session } from '../types';
import './Sidebar.css';

/** 快捷键印在按钮上：Mac 显示 ⌘，其它平台显示 Ctrl（用户要「Ctrl+N」这种写法）。 */
const MOD = /Mac/i.test(navigator.userAgent) ? '⌘' : 'Ctrl';

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

/** 一行会话。原样搬进独立组件：普通/项目会话共用，样式与拆分前一像素不差。 */
function SessionRow({ s }: { s: Session }) {
  const { activeSessionId, selectSession, view } = useApp();
  const active = s.id === activeSessionId && view === 'chat';
  // 独立「项目」分区后行内不再需要「项目」徽章（分区标题已说明），
  // 文件夹小标记保留（契约 §3.2.3 的可辨识性）。
  const isProject = s.kind === 'project';
  return (
    <button
      className={`session-row ${active ? 'is-active' : ''}`}
      onClick={() => selectSession(s.id)}
    >
      <span className={`session-row__accent ${active ? 'is-active' : ''}`} />
      <span className="session-row__main">
        <span className="session-row__title">
          {s.pinned && <Icon.Pin size={11} className="session-row__pin" />}
          {isProject && (
            <Icon.Folder size={11} className="session-row__project" />
          )}
          {s.title}
        </span>
        <span className="session-row__meta">
          <span>{relativeTime(s.updatedAt)}</span>
          {isProject && !!s.workspaceRoot && (
            <span className="session-row__ws" title={s.workspaceRoot}>
              {s.workspaceRoot}
            </span>
          )}
        </span>
      </span>
      {active && <span className="session-row__live" aria-hidden />}
    </button>
  );
}

export function Sidebar() {
  const {
    sessions, createSession, createProjectSession,
    sidebarCollapsed, toggleSidebar, view, setView
  } = useApp();

  const [query, setQuery] = useState('');
  const [skillsPanelOpen, setSkillsPanelOpen] = useState(false);
  // 「项目」分区折叠开关：持久化到 localStorage，重启后保持。
  const [sections, setSections] = useState<SidebarSectionsState>(() => readSidebarSections());
  // 「查看全部」：项目分区从「最近 5 条」展开到全量的原地开关（不弹窗）。
  const [showAllProjects, setShowAllProjects] = useState(false);

  const toggleProjects = () =>
    setSections(prev => {
      const next = { projectCollapsed: !prev.projectCollapsed };
      writeSidebarSections(next);
      return next;
    });

  /**
   * 新建项目 = 建一个项目会话，建好直接进对话（流程自己问「新建还是接入现有的」）。
   * 标题先占位，等对话问到项目名称（接入那条问出目录）再改写。
   * 2026-10-09 用户口径：这里是**对话**，不弹表单。
   */
  const startNewProject = () => createProjectSession({ projectName: NEW_PROJECT_TITLE });

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return sessions;
    return sessions.filter(s => s.title.toLowerCase().includes(q));
  }, [sessions, query]);

  const groups = useMemo(() => {
    // 2026-10-12：先按 kind 拆成「会话/项目」两个分区，再各自整理。
    // 会话分区保留时间分组 + 置顶；项目分区按 updatedAt 倒序、要短（默认 5 条）。
    const { chats, projects } = splitSidebarSessions(filtered);

    const chatGroups: Array<{ label: string; items: Session[] }> = [];
    const pinned = chats.filter(s => s.pinned);
    const rest = chats.filter(s => !s.pinned);
    if (pinned.length) chatGroups.push({ label: '置顶', items: pinned });

    const order: Array<'今天' | '昨天' | '本周' | '更早'> = ['今天', '昨天', '本周', '更早'];
    for (const label of order) {
      const items = rest
        .filter(s => groupOf(s.updatedAt) === label)
        .sort((a, b) => b.updatedAt - a.updatedAt);
      if (items.length) chatGroups.push({ label, items });
    }

    const projectsSorted = [...projects].sort((a, b) => b.updatedAt - a.updatedAt);
    return { chatGroups, projects: projectsSorted };
  }, [filtered]);

  if (sidebarCollapsed) {
    return (
      <aside className="sidebar sidebar--collapsed">
        <button className="sidebar__rail-btn" onClick={toggleSidebar} title="展开侧栏">
          <Icon.ChevronRight size={16} />
        </button>
        <button className="sidebar__rail-btn" onClick={createSession} title={`新建会话 (${MOD}+N)`}>
          <Icon.Plus size={16} />
        </button>
        <button
          className="sidebar__rail-btn"
          onClick={startNewProject}
          title={`新建项目 (${MOD}+P)`}
        >
          <Icon.Folder size={16} />
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
      {/* 「新建会话」和「新建项目」是同一层的两个入口：同一列、同一套样式，
          只有图标和快捷键不同（2026-10-09 用户口径：两者外观要一致）。 */}
      <header className="sidebar__head">
        <div className="sidebar__head-actions">
          <button className="sidebar__new" onClick={createSession}>
            <Icon.Plus size={15} />
            <span>新建会话</span>
            <kbd className="sidebar__kbd">{MOD}+N</kbd>
          </button>
          <button className="sidebar__new" onClick={startNewProject}>
            <Icon.Folder size={15} />
            <span>新建项目…</span>
            <kbd className="sidebar__kbd">{MOD}+P</kbd>
          </button>
        </div>
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
        {groups.chatGroups.length === 0 && groups.projects.length === 0 && (
          <div className="sidebar__empty">没有匹配的会话</div>
        )}

        {/* ── 会话分区：kind 缺省/未知都算 chat。搜索过滤空 → 整段隐藏。 ── */}
        {groups.chatGroups.length > 0 && (
          <section className="sidebar__section" aria-label="会话">
            {groups.chatGroups.map(g => (
              <div key={g.label} className="sidebar__group">
                <div className="sidebar__group-label">{g.label}</div>
                {g.items.map(s => (
                  <SessionRow key={s.id} s={s} />
                ))}
              </div>
            ))}
          </section>
        )}

        {/* ── 项目分区：只列最近 N 条，标题行带折叠开关（持久化）。 ── */}
        {groups.projects.length > 0 && (
          <section className="sidebar__section" aria-label="项目">
            <div className="sidebar__group-label sidebar__group-label--head">
              <button
                className="sidebar__section-toggle"
                onClick={toggleProjects}
                aria-expanded={!sections.projectCollapsed}
                title={sections.projectCollapsed ? '展开项目' : '折叠项目'}
              >
                {sections.projectCollapsed ? <Icon.ChevronRight size={13} /> : <Icon.ChevronDown size={13} />}
                <span>项目</span>
              </button>
              <span className="sidebar__section-count">{groups.projects.length}</span>
            </div>
            {!sections.projectCollapsed && (
              <>
                {(showAllProjects ? groups.projects : slicePreviewProjects(groups.projects))
                  .map(s => <SessionRow key={s.id} s={s} />)}
                {groups.projects.length > PROJECT_PREVIEW_N && (
                  <button
                    className="sidebar__more"
                    onClick={() => setShowAllProjects(v => !v)}
                    aria-expanded={showAllProjects}
                  >
                    {showAllProjects ? '收起' : `查看全部（共 ${groups.projects.length}）`}
                  </button>
                )}
              </>
            )}
          </section>
        )}
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