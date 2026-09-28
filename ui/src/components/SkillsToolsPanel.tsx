/**
 * 技能与工具面板（居中弹窗，入口在侧栏 rail）。
 *
 * - 打开时才拉数据：window.agent.tools() / window.agent.skills()。
 *   浏览器直开（没有 window.agent）时显示不可用提示，不崩溃。
 * - 工具页签：每行一个启用开关，切换调 window.agent.toolSetEnabled()
 *   并用其返回值整表刷新。
 * - 技能页签：只读展示，内置技能带「内置」标记并标注不可编辑。
 * - 弹窗遮罩 fixed 全屏、内容限高内部滚动，页面本体不增高。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getAgent } from '../state/live';
import type { SkillInfo, ToolEntry } from '../types';
import { Icon } from './Icon';
import { toolCopyOf } from './toolCopy.zh';
import './SkillsToolsPanel.css';

type TabKey = 'tools' | 'skills';

type LoadPhase = 'loading' | 'ready' | 'unavailable' | 'error';

interface ToolsState {
  phase: LoadPhase;
  items: ToolEntry[];
  message: string;
}

interface SkillsState {
  phase: LoadPhase;
  items: SkillInfo[];
  skipped: [string, string][];
  message: string;
}

const ERR_BRIDGE = '未连接本地服务，工具与技能信息不可用。请通过应用启动。';
const ERR_FETCH = '信息加载失败，请稍后重试。';
const EMPTY_TOOLS = '暂无可用工具。';
const EMPTY_SKILLS = '暂无已加载技能。';

function filterByQuery<T extends { name: string; description?: string | null }>(
  items: T[],
  query: string
): T[] {
  const q = query.trim().toLowerCase();
  if (!q) return items;
  return items.filter(item => {
    const name = item.name.toLowerCase();
    const desc = (item.description ?? '').toLowerCase();
    return name.includes(q) || desc.includes(q);
  });
}

export function SkillsToolsPanel({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<TabKey>('tools');
  const [query, setQuery] = useState('');
  const [tools, setTools] = useState<ToolsState>({ phase: 'loading', items: [], message: '' });
  const [skills, setSkills] = useState<SkillsState>({ phase: 'loading', items: [], skipped: [], message: '' });
  const pendingRef = useRef(false);

  /* Esc 关闭（挂在 window 上，输入框聚焦时也生效）。 */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  /* 拉取数据：只在挂载后执行一次；两条通道互不阻塞。 */
  useEffect(() => {
    const agent = getAgent();
    if (!agent) {
      setTools({ phase: 'unavailable', items: [], message: ERR_BRIDGE });
      setSkills({ phase: 'unavailable', items: [], skipped: [], message: ERR_BRIDGE });
      return;
    }
    let cancelled = false;

    agent
      .tools()
      .then(r => {
        if (cancelled) return;
        const items = r?.tools ?? [];
        setTools(
          items.length
            ? { phase: 'ready', items, message: '' }
            : { phase: 'ready', items: [], message: EMPTY_TOOLS }
        );
      })
      .catch(() => {
        if (cancelled) return;
        setTools({ phase: 'error', items: [], message: ERR_FETCH });
      });

    if (agent.skills) {
      agent
        .skills()
        .then(r => {
          if (cancelled) return;
          const items = r?.skills ?? [];
          const skipped = r?.skipped ?? [];
          setSkills(
            items.length
              ? { phase: 'ready', items, skipped, message: '' }
              : { phase: 'ready', items: [], skipped, message: EMPTY_SKILLS }
          );
        })
        .catch(() => {
          if (cancelled) return;
          setSkills({ phase: 'error', items: [], skipped: [], message: ERR_FETCH });
        });
    } else {
      setSkills({ phase: 'unavailable', items: [], skipped: [], message: ERR_BRIDGE });
    }

    return () => {
      cancelled = true;
    };
  }, []);

  /* 工具开关：调 toolSetEnabled，用返回值整表刷新；失败回滚并提示。 */
  const toggleTool = useCallback(
    (name: string, next: boolean) => {
      const agent = getAgent();
      if (!agent || !agent.toolSetEnabled || pendingRef.current) return;
      pendingRef.current = true;
      const prev = tools;
      setTools({
        phase: 'ready',
        items: prev.items.map(t => (t.name === name ? { ...t, enabled: next } : t)),
        message: prev.message,
      });
      agent
        .toolSetEnabled({ name, enabled: next })
        .then(r => {
          const items = r?.tools ?? null;
          if (items) {
            setTools(
              items.length
                ? { phase: 'ready', items, message: '' }
                : { phase: 'ready', items: [], message: EMPTY_TOOLS }
            );
          }
        })
        .catch(() => {
          setTools({ ...prev, message: '状态更新失败，请稍后重试。' });
        })
        .finally(() => {
          pendingRef.current = false;
        });
    },
    [tools]
  );

  const visibleTools = useMemo(
    () => filterByQuery(tools.items, query),
    [tools.items, query]
  );
  const visibleSkills = useMemo(
    () => filterByQuery(skills.items, query),
    [skills.items, query]
  );

  return (
    <div
      className="skills-tools-overlay"
      onClick={e => {
        if (e.target === e.currentTarget) onClose();
      }}
      role="presentation"
    >
      <section
        className="skills-tools-panel"
        role="dialog"
        aria-modal="true"
        aria-label="技能与工具"
      >
        <header className="skills-tools-panel__head">
          <h2 className="skills-tools-panel__title">技能与工具</h2>
          <div className="skills-tools-panel__actions">
            <div className="skills-tools-tabs" role="tablist" aria-label="面板页签">
              <button
                className={`skills-tools-tabs__btn ${tab === 'tools' ? 'is-active' : ''}`}
                role="tab"
                aria-selected={tab === 'tools'}
                onClick={() => setTab('tools')}
              >
                工具
              </button>
              <button
                className={`skills-tools-tabs__btn ${tab === 'skills' ? 'is-active' : ''}`}
                role="tab"
                aria-selected={tab === 'skills'}
                onClick={() => setTab('skills')}
              >
                技能
              </button>
            </div>
            <button className="skills-tools-panel__close" onClick={onClose} title="关闭">
              <Icon.X size={13} />
            </button>
          </div>
        </header>

        <div className="skills-tools-panel__search">
          <Icon.Search size={13} />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder={tab === 'tools' ? '搜索工具' : '搜索技能'}
            aria-label={tab === 'tools' ? '搜索工具' : '搜索技能'}
          />
          {query && (
            <button className="skills-tools-panel__search-clear" onClick={() => setQuery('')}>
              <Icon.X size={11} />
            </button>
          )}
        </div>

        <div className="skills-tools-panel__body">
          {tab === 'tools' ? (
            <ToolList
              phase={tools.phase}
              items={visibleTools}
              message={tools.message}
              onToggle={toggleTool}
            />
          ) : (
            <SkillList
              phase={skills.phase}
              items={visibleSkills}
              skipped={skills.skipped}
              message={skills.message}
            />
          )}
        </div>
      </section>
    </div>
  );
}

function ToolList({
  phase,
  items,
  message,
  onToggle,
}: {
  phase: LoadPhase;
  items: ToolEntry[];
  message: string;
  onToggle: (name: string, enabled: boolean) => void;
}) {
  if (phase === 'loading') return <PanelNote>正在加载工具列表…</PanelNote>;
  if (phase === 'unavailable' || phase === 'error') return <PanelNote>{message}</PanelNote>;
  if (!items.length) return <PanelNote>{message || EMPTY_TOOLS}</PanelNote>;
  return (
    <ul className="skills-tools-list" aria-label="工具列表">
      {items.map(t => {
        const zh = toolCopyOf(t.name);
        return (
          <li key={t.name} className="skills-tool-row">
            <div className="skills-tool-row__main">
              <span className="skills-tool-row__head">
                {/* 中文名给人看；英文 tool name 保留成小字，因为日志/审批卡片里都是它。
                    没有中文条目的工具回落到英文原文，不编名字。 */}
                <span className="skills-tool-row__zh">{zh ? zh.label : t.name}</span>
                {zh && <span className="skills-tool-row__slug">{t.name}</span>}
              </span>
              <span className="skills-tool-row__desc" title={t.description}>
                {zh ? zh.hint : t.description}
              </span>
            </div>
            <button
              type="button"
              className={`switch ${t.enabled ? 'switch--on' : ''}`}
              role="switch"
              aria-checked={t.enabled}
              aria-label={`启用工具 ${t.name}`}
              onClick={() => onToggle(t.name, !t.enabled)}
            >
              <span className="switch__text">{t.enabled ? '已启用' : '已停用'}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function SkillList({
  phase,
  items,
  skipped,
  message,
}: {
  phase: LoadPhase;
  items: SkillInfo[];
  skipped: [string, string][];
  message: string;
}) {
  if (phase === 'loading') return <PanelNote>正在加载技能列表…</PanelNote>;
  if (phase === 'unavailable' || phase === 'error') return <PanelNote>{message}</PanelNote>;
  if (!items.length) return <PanelNote>{message || EMPTY_SKILLS}</PanelNote>;
  return (
    <>
      <ul className="skills-tools-list" aria-label="技能列表">
        {items.map(s => (
          <li key={s.name} className="skills-skill-row">
            <div className="skills-skill-row__head">
              <span className="skills-skill-row__name">{s.name}</span>
              {s.builtin && (
                <span className="skills-skill-row__badge" title="内置技能不可编辑">
                  内置
                </span>
              )}
            </div>
            {s.description && <p className="skills-skill-row__desc">{s.description}</p>}
            {s.whenToUse && (
              <p className="skills-skill-row__when">
                <span className="skills-skill-row__label">适用场景</span>
                {s.whenToUse}
              </p>
            )}
            {(s.version || s.author) && (
              <p className="skills-skill-row__meta">
                {s.version && <span>版本 {s.version}</span>}
                {s.version && s.author && <span className="skills-skill-row__dot" />}
                {s.author && <span>作者 {s.author}</span>}
              </p>
            )}
            {s.builtin && (
              <p className="skills-skill-row__builtin-note">内置技能随应用提供，不可编辑。</p>
            )}
          </li>
        ))}
      </ul>
      {skipped.length > 0 && (
        <div className="skills-tools-skipped">
          <div className="skills-tools-skipped__title">以下技能未能加载</div>
          <ul>
            {skipped.map(([name, reason]) => (
              <li key={name}>
                <span className="skills-tools-skipped__name">{name}</span>
                <span className="skills-tools-skipped__reason">{reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  );
}

function PanelNote({ children }: { children: React.ReactNode }) {
  return <div className="skills-tools-note">{children}</div>;
}
