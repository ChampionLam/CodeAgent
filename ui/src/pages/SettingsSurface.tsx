import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import { useApp } from '../state/store';
import { Icon } from '../components/Icon';
import { TopBar } from '../components/TopBar';
import { SettingsPage } from './SettingsPage';
import {
  ACCENTS, THEME_PRESETS, MSG_SURFACES,
  type AppSettings, type ThemePreset, type ResolvedTheme, type WallpaperId, type AccentId
} from '../lib/appSettings';
import { WALLPAPER_URLS } from '../lib/wallpapers';
import './SettingsSurface.css';

type SectionId = 'appearance' | 'models' | 'general' | 'data' | 'about';

const SECTIONS: Array<{ id: SectionId; name: string; desc: string }> = [
  { id: 'appearance', name: '外观', desc: '主题与强调色' },
  { id: 'models', name: '模型', desc: '接入、校验、思考显示' },
  { id: 'general', name: '常规', desc: '发送键与快捷键' },
  { id: 'data', name: '数据', desc: '本机用量与诊断' },
  { id: 'about', name: '关于', desc: '版本与运行环境' }
];


/** One clipboard write + a short-lived confirmation on the button itself. */
function useCopy(): [string, (text: string, key: string) => void] {
  const [copied, setCopied] = useState('');
  const copy = (text: string, key: string) => {
    void navigator.clipboard?.writeText(text).then(() => {
      setCopied(key);
      window.setTimeout(() => setCopied(prev => (prev === key ? '' : prev)), 1600);
    }).catch(() => setCopied(''));
  };
  return [copied, copy];
}

function Row(props: { title: string; desc?: string; children: ReactNode }) {
  return (
    <div className="ss-row">
      <div className="ss-row__label">
        <div className="ss-row__title">{props.title}</div>
        {props.desc ? <div className="ss-row__desc">{props.desc}</div> : null}
      </div>
      <div className="ss-row__control">{props.children}</div>
    </div>
  );
}

/** Live `prefers-color-scheme` so the 跟随系统 card previews the right theme. */
function usePrefersDark(): boolean {
  const [dark, setDark] = useState(
    () => typeof window.matchMedia === 'function'
      && window.matchMedia('(prefers-color-scheme: dark)').matches
  );
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const on = () => setDark(mq.matches);
    on();
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  return dark;
}

/**
 * A miniature of the app chrome painted with a preset's own colours. It uses
 * the same variables the real UI uses, so what you see here is what you get —
 * it is not a screenshot.
 */
function ThemePreview(props: { theme: ResolvedTheme; wallpaper: WallpaperId; accent: AccentId }) {
  const url = WALLPAPER_URLS[props.wallpaper];
  const def = ACCENTS[props.accent] ?? ACCENTS.indigo;
  const set = props.theme === 'light' ? def.light : def;
  return (
    <div
      className={'ssh' + (url ? ' ssh--image' : '')}
      data-theme={props.theme}
      style={{ '--ssh-accent': set.base, ...(url ? { '--ssh-image': `url("${url}")` } : {}) } as CSSProperties}
      aria-hidden
    >
      <div className="ssh__bar">
        <span className="ssh__logo" />
        <span className="ssh__title" />
        <span className="ssh__pill" />
      </div>
      <div className="ssh__body">
        <div className="ssh__side">
          <span className="ssh__side-row is-on" />
          <span className="ssh__side-row" />
          <span className="ssh__side-row" />
        </div>
        <div className="ssh__chat">
          <div className="ssh__msg ssh__msg--ai"><span /><span /></div>
          <div className="ssh__msg ssh__msg--me"><span /></div>
          <div className="ssh__input"><span className="ssh__caret" /><span className="ssh__send" /></div>
        </div>
      </div>
    </div>
  );
}

function ThemeCard(props: {
  preset: ThemePreset;
  active: boolean;
  theme: ResolvedTheme;
  onPick: () => void;
}) {
  const { preset, active, theme, onPick } = props;
  return (
    <button
      type="button"
      className={'ss-theme' + (active ? ' is-active' : '')}
      onClick={onPick}
      aria-pressed={active}
      title={preset.name + '（' + preset.hint + '）'}
    >
      <ThemePreview theme={theme} wallpaper={preset.wallpaper} accent={preset.accent} />
      <span className="ss-theme__foot">
        <span className="ss-theme__name">{preset.name}</span>
        <span className="ss-theme__hint">{preset.hint}</span>
      </span>
      {active ? <span className="ss-theme__check"><Icon.Check size={12} /></span> : null}
    </button>
  );
}

function AppearanceSection(props: {
  settings: AppSettings;
  setSettings: (patch: Partial<AppSettings>) => void;
}) {
  const { settings, setSettings } = props;
  const prefersDark = usePrefersDark();
  return (
    <>
      <section className="ss-card">
        <header className="ss-card__head">
          <h2 className="ss-card__title">全部主题</h2>
          <p className="ss-card__desc">缩略图就是这套配色的真实效果——用同一套样式变量画的，不是截图。选完立即整窗生效，重启后保持。</p>
        </header>
        <div className="ss-themes">
          {THEME_PRESETS.map(p => (
            <ThemeCard
              key={p.id}
              preset={p}
              theme={p.mode}
              active={settings.theme === p.mode && settings.wallpaper === p.wallpaper}
              onPick={() => setSettings({ theme: p.mode, wallpaper: p.wallpaper, accent: p.accent })}
            />
          ))}
        </div>
      </section>

      {/* 消息底纹：一个决定点两选一（2026-09-27 用户口径「放在外观里面设置吧」，
          起因是壁纸模式下助手正文半透明、气泡实色的不一致）。 */}
      <section className="ss-card">
        <header className="ss-card__head">
          <h2 className="ss-card__title">消息底纹</h2>
          <p className="ss-card__desc">
            壁纸模式下两侧都可以半透明：助手正文坐一层 72% 的面板，你的气泡取 82% ——
            壁纸透得出来，字也还压得住。想要最稳的观感就选「两侧实色」，两边一起换成实色底。
          </p>
        </header>
        <Row title="壁纸模式下的消息底" desc={MSG_SURFACES.find(m => m.id === settings.msgSurface)?.hint}>
          <div className="ss-seg" role="group" aria-label="消息底纹">
            {MSG_SURFACES.map(m => (
              <button
                key={m.id}
                type="button"
                className={'ss-seg__btn' + (settings.msgSurface === m.id ? ' is-active' : '')}
                aria-pressed={settings.msgSurface === m.id}
                onClick={() => setSettings({ msgSurface: m.id })}
              >
                {m.name}
              </button>
            ))}
          </div>
        </Row>
      </section>

    </>
  );
}

function GeneralSection() {
  const isMac = /Mac/i.test(navigator.userAgent);
  const mod = isMac ? '⌘' : 'Ctrl';
  const KEYS: Array<[string, string]> = [
    ['Enter', '发送消息（中文输入法组字时先确认候选词，不会误发）'],
    ['Shift+Enter', '换行'],
    [mod + '+N', '新建会话'],
    [mod + '+Shift+S', '截图取词'],
    ['Esc', '返回对话 / 关掉弹层']
  ];
  return (
    <section className="ss-card">
      <header className="ss-card__head">
        <h2 className="ss-card__title">快捷键</h2>
      </header>
      {/* 表格形态：左边键位、右边说明（2026-09-27 用户口径「用表格展示，快捷键靠左，描述靠右」） */}
      <table className="ss-keys">
        <tbody>
          {KEYS.map(([k, d]) => (
            <tr key={k}>
              <td className="ss-keys__key"><kbd>{k}</kbd></td>
              <td className="ss-keys__desc">{d}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function DataSection() {
  const { sessions, models, messages, activeSessionId, sidecarState } = useApp();
  const [copied, copy] = useCopy();
  const msgCount = (messages[activeSessionId] || []).length;
  const activeSession = sessions.find(s => s.id === activeSessionId);
  const report = useMemo(() => [
    'CodeAgent 诊断',
    `界面版本: ${typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : 'dev'}`,
    `运行环境: ${navigator.userAgent}`,
    `窗口: ${window.innerWidth}x${window.innerHeight} dpr=${window.devicePixelRatio}`,
    `sidecar: ${sidecarState}`,
    `模型: ${models.length} 个（会话内默认 ${models.find(m => m.isDefault)?.name ?? '未设'}）`,
    `会话: ${sessions.length} 个，当前「${activeSession?.title ?? '-'}」消息 ${msgCount} 条`,
    `主题: ${document.documentElement.dataset.theme ?? '-'}`
  ].join('\n'), [models, sessions, msgCount, sidecarState, activeSession]);
  return (
    <>
      <section className="ss-card">
        <header className="ss-card__head">
          <h2 className="ss-card__title">本机用量</h2>
          <p className="ss-card__desc">全部存在这台机器上，不上传。</p>
        </header>
        <div className="ss-stats">
          <div className="ss-stat"><div className="ss-stat__v">{models.length}</div><div className="ss-stat__k">已配置模型</div></div>
          <div className="ss-stat"><div className="ss-stat__v">{sessions.length}</div><div className="ss-stat__k">会话</div></div>
          <div className="ss-stat"><div className="ss-stat__v">{msgCount}</div><div className="ss-stat__k">当前会话消息</div></div>
          <div className="ss-stat">
            <div className={'ss-stat__v' + (sidecarState === 'ready' ? ' is-ok' : ' is-warn')}>{sidecarState}</div>
            <div className="ss-stat__k">本地服务</div>
          </div>
        </div>
      </section>
      <section className="ss-card">
        <header className="ss-card__head">
          <h2 className="ss-card__title">诊断</h2>
          <p className="ss-card__desc">出问题时把这段贴给我，比截图有用。</p>
        </header>
        <pre className="ss-diag">{report}</pre>
        <div className="ss-card__foot">
          <button className="btn btn--outline btn--sm" onClick={() => copy(report, 'diag')}>
            {copied === 'diag' ? <Icon.Check size={12} /> : <Icon.Copy size={12} />}
            {copied === 'diag' ? '已复制' : '复制诊断信息'}
          </button>
        </div>
      </section>
    </>
  );
}

function AboutSection() {
  const ua = navigator.userAgent;
  const chrome = /Chrome\/([\d.]+)/.exec(ua)?.[1] ?? '-';
  const electron = /Electron\/([\d.]+)/.exec(ua)?.[1] ?? '-';
  const platform = /\(([^)]+)\)/.exec(ua)?.[1] ?? '-';
  const version = typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : 'dev';
  const openExternal = (window as unknown as { agent?: { openExternal?: (u: string) => Promise<boolean> } }).agent?.openExternal;
  return (
    <section className="ss-card">
      <header className="ss-card__head">
        <h2 className="ss-card__title">CodeAgent</h2>
        <p className="ss-card__desc">本地桌面 Agent：Electron 壳 + React 界面 + Python sidecar，模型走你自己的 Key。</p>
      </header>
      <div className="ss-about">
        <div className="ss-about__logo" aria-hidden />
        <div>
          <div className="ss-about__name">CodeAgent <span className="ss-about__ver">v{version}</span></div>
          <div className="ss-about__sub">界面与模型配置存本机；API Key 只写 .env，界面读不到值。</div>
        </div>
      </div>
      <dl className="ss-dl">
        <div><dt>Electron</dt><dd>{electron}</dd></div>
        <div><dt>Chromium</dt><dd>{chrome}</dd></div>
        <div><dt>平台</dt><dd className="ss-dl__wrap">{platform}</dd></div>
      </dl>
      {openExternal ? (
        <div className="ss-card__foot">
          <button
            className="btn btn--outline btn--sm"
            onClick={() => void openExternal('https://github.com/ChampionLam/CodeAgent')}
          >
            打开仓库
          </button>
        </div>
      ) : null}
    </section>
  );
}

/**
 * The whole-window settings surface. It replaces the sidebar AND the workspace
 * while keeping the TopBar (frameless window: the TopBar is the only title bar,
 * so covering it would take away drag + window buttons).
 */
export function SettingsSurface() {
  const { appSettings, setAppSettings, setView } = useApp();
  const [section, setSection] = useState<SectionId>('appearance');
  const current = SECTIONS.find(s => s.id === section) ?? SECTIONS[0];

  return (
    <div className="app-body">
      <div className="settings-surface">
        {/* ⚠️ TopBar 必须留在这里：App.tsx 的设置分支只渲染本组件，
            而不渲染 app-body 里的那个 TopBar。无边框窗口下它是唯一的标题栏，
            去掉就没法拖动 / 关窗（2026-09-27 我误删过一次，当场回滚）。
            重复的「设置」不在这里解决 —— TopBar 里本来就有一个「设置」按钮，
            所以这一行不再放「设置」标题，只留返回，避免同一个词出现两次。 */}
        <TopBar />
        <div className="ss-topbar">
          <button className="ss-back" onClick={() => setView('chat')}>
            <Icon.ChevronLeft size={14} /> 返回对话
            <span className="kbd-hint">Esc</span>
          </button>
        </div>
        <div className="settings-surface__inner">
          <nav className="ss-nav" aria-label="设置分区">
            <ul className="ss-nav__list">
              {SECTIONS.map(s => (
                <li key={s.id}>
                  <button
                    className={'ss-nav__item' + (section === s.id ? ' is-active' : '')}
                    onClick={() => setSection(s.id)}
                    aria-current={section === s.id}
                  >
                    <span className="ss-nav__name">{s.name}</span>
                  </button>
                </li>
              ))}
            </ul>
          </nav>
          <main className="ss-main">
            <header className="ss-main__head">
              <h1 className="ss-main__title">{current.name}</h1>
              <p className="ss-main__desc">{current.desc}</p>
            </header>
            <div className="ss-main__body">
              {section === 'appearance' && (
                <AppearanceSection settings={appSettings} setSettings={setAppSettings} />
              )}
              {section === 'models' && <SettingsPage />}
              {section === 'general' && <GeneralSection />}
              {section === 'data' && <DataSection />}
              {section === 'about' && <AboutSection />}
            </div>
          </main>
        </div>
      </div>
    </div>
  );
}
