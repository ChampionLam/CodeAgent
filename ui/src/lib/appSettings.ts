/**
 * Whole-application settings: theme mode, accent colour, wallpaper.
 *
 * Pure module on purpose — no DOM and no React — so the merge/validate/derive
 * logic can be unit-tested (tests/appSettings.test.ts). The thin DOM writer
 * lives at the bottom; everything above it is a pure function.
 *
 * Persisted in localStorage, next to the thinking-display preference
 * (workbuddy.thinkingDisplay) — app appearance is a per-machine UI preference,
 * NOT part of config.json, and it must never be sent to the sidecar.
 */

export type ThemeMode = 'dark' | 'light' | 'system';
export type AccentId = 'indigo' | 'violet' | 'blue' | 'teal' | 'amber' | 'rose';
export type ResolvedTheme = 'dark' | 'light';
export type WallpaperId = 'halo' | 'aurora' | 'deepsea' | 'snow';
/** 消息底纹：壁纸模式下助手正文那张面板，是透出壁纸还是跟气泡一样实色。 */
export type MsgSurface = 'wallpaper' | 'solid';

/** 背景风格。halo = 默认的 CSS 光晕（不引图片），其余是内置壁纸。 */
export interface WallpaperDef {
  id: WallpaperId;
  name: string;
  hint: string;
}

export const WALLPAPERS: WallpaperDef[] = [
  { id: 'halo', name: '光晕', hint: '默认 · 纯 CSS' },
  { id: 'aurora', name: '极光夜空', hint: '深色更配' },
  { id: 'deepsea', name: '深海鲸影', hint: '安静' },
  { id: 'snow', name: '雪原白熊', hint: '浅色更配' }
];
export const WALLPAPER_IDS = WALLPAPERS.map(w => w.id);

/**
 * 消息底纹两选一（2026-09-27 用户口径「放在外观里面设置吧」）。
 *
 * 来由：壁纸模式下助手正文坐一层 72% 面板（让壁纸透出来、字又压得住），而用户气泡
 * 是实色渐变 —— 于是「一边半透明一边不透明」。这里把口径交给用户，默认保持现状，
 * 选「两侧实色」就把那张面板也做实，和气泡一致。
 *
 * 没做「两侧都半透明」：气泡底是强调色渐变，透出壁纸后正文对比度会掉（此前审计要求
 * 逐元素 WCAG >= 3.0），所以只给了保守的那个方向。
 */
export interface MsgSurfaceDef {
  id: MsgSurface;
  name: string;
  hint: string;
}

export const MSG_SURFACES: MsgSurfaceDef[] = [
  { id: 'wallpaper', name: '透出壁纸', hint: '默认 · 助手正文和你的气泡都半透明，壁纸透得出来' },
  { id: 'solid', name: '两侧实色', hint: '两侧都换成实色底，字最稳、观感最统一' }
];
export const MSG_SURFACE_IDS = MSG_SURFACES.map(m => m.id);

export interface AppSettings {
  theme: ThemeMode;
  accent: AccentId;
  /** 背景风格（内置壁纸或默认光晕）。 */
  wallpaper: WallpaperId;
  /** 壁纸模式下消息底纹是否透出壁纸。 */
  msgSurface: MsgSurface;
}

export const APP_SETTINGS_KEY = 'codeagent.appSettings.v1';

export const DEFAULT_APP_SETTINGS: AppSettings = {
  theme: 'dark',
  accent: 'indigo',
  wallpaper: 'halo',
  msgSurface: 'wallpaper'
};

/** Accent palettes. Keys are stable ids (they are persisted). */
export interface AccentDef {
  name: string;
  /* 深色主题四件套（原有观感，别动） */
  base: string;
  hover: string;
  soft: string;
  ring: string;
  /** 强调色当「文字」用时（深色底要浅） */
  text: string;
  /* 浅色主题一套：底要够深，白字才读得清（审计量过：原来的 #14b8a6 +
     白字只有 2.49，低于 3.0）；text 在浅底上直接用 base。 */
  light: { base: string; hover: string; soft: string; ring: string };
}

export const ACCENTS: Record<AccentId, AccentDef> = {
  indigo: {
    name: '靛蓝',
    base: '#6366f1', hover: '#7c7ff5', soft: 'rgba(99, 102, 241, 0.12)', ring: 'rgba(99, 102, 241, 0.35)', text: '#a5a8fa',
    light: { base: '#4f46e5', hover: '#4338ca', soft: 'rgba(79, 70, 229, 0.10)', ring: 'rgba(79, 70, 229, 0.30)' }
  },
  violet: {
    name: '紫罗兰',
    base: '#8b5cf6', hover: '#a78bfa', soft: 'rgba(139, 92, 246, 0.13)', ring: 'rgba(139, 92, 246, 0.35)', text: '#c4b5fd',
    light: { base: '#7c3aed', hover: '#6d28d9', soft: 'rgba(124, 58, 237, 0.10)', ring: 'rgba(124, 58, 237, 0.30)' }
  },
  blue: {
    name: '天蓝',
    base: '#0ea5e9', hover: '#38bdf8', soft: 'rgba(14, 165, 233, 0.13)', ring: 'rgba(14, 165, 233, 0.35)', text: '#7dd3fc',
    light: { base: '#0369a1', hover: '#075985', soft: 'rgba(3, 105, 161, 0.10)', ring: 'rgba(3, 105, 161, 0.30)' }
  },
  teal: {
    name: '青绿',
    base: '#14b8a6', hover: '#2dd4bf', soft: 'rgba(20, 184, 166, 0.13)', ring: 'rgba(20, 184, 166, 0.35)', text: '#5eead4',
    light: { base: '#0f766e', hover: '#115e59', soft: 'rgba(15, 118, 110, 0.10)', ring: 'rgba(15, 118, 110, 0.30)' }
  },
  amber: {
    name: '琥珀',
    base: '#f59e0b', hover: '#fbbf24', soft: 'rgba(245, 158, 11, 0.14)', ring: 'rgba(245, 158, 11, 0.35)', text: '#fcd34d',
    light: { base: '#b45309', hover: '#92400e', soft: 'rgba(180, 83, 9, 0.10)', ring: 'rgba(180, 83, 9, 0.30)' }
  },
  rose: {
    name: '玫红',
    base: '#f43f5e', hover: '#fb7185', soft: 'rgba(244, 63, 94, 0.13)', ring: 'rgba(244, 63, 94, 0.35)', text: '#fda4af',
    light: { base: '#be123c', hover: '#9f1239', soft: 'rgba(190, 18, 60, 0.10)', ring: 'rgba(190, 18, 60, 0.30)' }
  }
};
export const ACCENT_IDS = Object.keys(ACCENTS) as AccentId[];

/**
 * Theme presets for 外观 — each card is a (mode, accent) pair with a name and a
 * live mini preview. The stored model stays two separate fields, so the accent
 * row below can still fine-tune a preset.
 */
/**
 * 主题卡：一张卡 = 深浅模式 + 背景打包（和 WorkBuddy 那种一致）。
 * 背景不再单独选，选了哪张卡就同时决定深浅与背景。
 */
export interface ThemePreset {
  id: string;
  name: string;
  hint: string;
  mode: ResolvedTheme;
  wallpaper: WallpaperId;
  accent: AccentId;
}

/* 一张卡 = 深浅 + 背景 + 强调色，成套切（不再单独给强调色选项）。 */
export const THEME_PRESETS: ThemePreset[] = [
  { id: 'halo-dark', name: '深色', hint: '默认 · 光晕', mode: 'dark', wallpaper: 'halo', accent: 'indigo' },
  { id: 'aurora', name: '极光夜空', hint: '深色 · 极光 · 紫', mode: 'dark', wallpaper: 'aurora', accent: 'violet' },
  { id: 'deepsea', name: '深海鲸影', hint: '深色 · 深海 · 天蓝', mode: 'dark', wallpaper: 'deepsea', accent: 'blue' },
  { id: 'snow', name: '雪原白熊', hint: '浅色 · 雪原 · 青绿', mode: 'light', wallpaper: 'snow', accent: 'teal' },
  { id: 'halo-light', name: '浅色', hint: '光晕 · 靛蓝', mode: 'light', wallpaper: 'halo', accent: 'indigo' }
];


const THEME_MODES: ThemeMode[] = ['dark', 'light', 'system'];

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/**
 * Tolerant parse: unknown or corrupt fields fall back to defaults, one field at
 * a time (a half-written object must never wipe the whole preference set).
 */
export function parseAppSettings(raw: unknown): AppSettings {
  if (!isPlainObject(raw)) return { ...DEFAULT_APP_SETTINGS };
  const o = raw;
  return {
    theme: THEME_MODES.includes(o.theme as ThemeMode) ? (o.theme as ThemeMode) : DEFAULT_APP_SETTINGS.theme,
    accent: ACCENT_IDS.includes(o.accent as AccentId) ? (o.accent as AccentId) : DEFAULT_APP_SETTINGS.accent,
    wallpaper: WALLPAPER_IDS.includes(o.wallpaper as WallpaperId) ? (o.wallpaper as WallpaperId) : DEFAULT_APP_SETTINGS.wallpaper,
    msgSurface: MSG_SURFACE_IDS.includes(o.msgSurface as MsgSurface) ? (o.msgSurface as MsgSurface) : DEFAULT_APP_SETTINGS.msgSurface,
  };
}

/** 'system' follows the OS via prefers-color-scheme. */
export function resolveTheme(mode: ThemeMode, prefersDark: boolean): ResolvedTheme {
  if (mode === 'system') return prefersDark ? 'dark' : 'light';
  return mode === 'light' ? 'light' : 'dark';
}

/**
 * The CSS custom properties an AppSettings implies. Pure — the caller decides
 * where to write them (documentElement in the app, a fake root in tests).
 */
export function cssVarsFor(s: AppSettings, theme: ResolvedTheme = 'dark'): Record<string, string> {
  const accent = ACCENTS[s.accent] ?? ACCENTS[DEFAULT_APP_SETTINGS.accent];
  const set = theme === 'light' ? accent.light : accent;
  const vars: Record<string, string> = {
    '--accent': set.base,
    '--accent-hover': set.hover,
    '--accent-soft': set.soft,
    '--accent-ring': set.ring,
    '--accent-text': theme === 'light' ? set.base : accent.text
  };
  return vars;
}

export function loadAppSettings(): AppSettings {
  try {
    const raw = window.localStorage?.getItem(APP_SETTINGS_KEY);
    if (!raw) return { ...DEFAULT_APP_SETTINGS };
    return parseAppSettings(JSON.parse(raw));
  } catch {
    return { ...DEFAULT_APP_SETTINGS };
  }
}

export function saveAppSettings(s: AppSettings): void {
  try {
    window.localStorage?.setItem(APP_SETTINGS_KEY, JSON.stringify(s));
  } catch {
    /* storage disabled — preferences simply do not survive a restart */
  }
}

/** Writes theme + variables onto a root element. Safe to call repeatedly. */
export function applyAppSettings(
  s: AppSettings,
  root: HTMLElement,
  prefersDark: boolean
): ResolvedTheme {
  const theme = resolveTheme(s.theme, prefersDark);
  root.dataset.theme = theme;
  /* CSS 靠 [data-msg-surface='solid'] 覆盖壁纸模式那张半透明面板 */
  root.dataset.msgSurface = s.msgSurface;
  root.style.colorScheme = theme;
  const vars = cssVarsFor(s, theme);
  for (const [k, v] of Object.entries(vars)) root.style.setProperty(k, v);
  return theme;
}
