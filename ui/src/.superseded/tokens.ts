/* Design tokens — single source of truth for colors, typography, motion */
export const tokens = {
  // Surfaces (Layered dark, like Claude Desktop / Linear / Cursor)
  bg: {
    app: '#0f0f10',        // outermost canvas
    sidebar: '#131316',    // sidebar panel
    panel: '#1a1a1d',      // raised panel (cards, topbar)
    card: '#1e1e22',       // nested card
    hover: '#232328',      // hover row
    active: '#2a2a31',     // pressed/active
    code: '#0c0c0d'        // code block background
  },
  border: {
    subtle: '#222226',
    default: '#2a2a2e',
    strong: '#36363c',
    focus: '#6366f1'
  },
  text: {
    primary: '#e8e8ea',
    secondary: '#a8a8b0',
    tertiary: '#8b8b94',
    muted: '#6f6f79',
    inverse: '#0f0f10'
  },
  accent: {
    base: '#6366f1',       // indigo (default)
    hover: '#7c7ff5',
    soft: 'rgba(99, 102, 241, 0.12)',
    ring: 'rgba(99, 102, 241, 0.35)'
  },
  semantic: {
    success: '#10b981',
    successSoft: 'rgba(16, 185, 129, 0.12)',
    warn: '#f59e0b',
    warnSoft: 'rgba(245, 158, 11, 0.12)',
    danger: '#ef4444',
    dangerSoft: 'rgba(239, 68, 68, 0.12)',
    info: '#3b82f6',
    infoSoft: 'rgba(59, 130, 246, 0.12)'
  },
  radius: {
    xs: '4px',
    sm: '6px',
    md: '8px',
    lg: '10px',
    xl: '12px',
    pill: '999px'
  },
  shadow: {
    sm: '0 1px 2px rgba(0,0,0,0.4)',
    md: '0 4px 12px rgba(0,0,0,0.35), 0 0 0 1px rgba(255,255,255,0.04)',
    lg: '0 12px 32px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.05)',
    focus: '0 0 0 3px rgba(99,102,241,0.25)'
  },
  font: {
    stack: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
    mono: '"SF Mono", "JetBrains Mono", "Cascadia Code", Consolas, "Liberation Mono", Menlo, monospace'
  },
  motion: {
    fast: '120ms cubic-bezier(0.2, 0.8, 0.2, 1)',
    base: '180ms cubic-bezier(0.2, 0.8, 0.2, 1)',
    slow: '240ms cubic-bezier(0.2, 0.8, 0.2, 1)'
  }
} as const;

export type Tokens = typeof tokens;