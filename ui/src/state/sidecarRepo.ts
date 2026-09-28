/**
 * ConversationRepo backed by the sidecar's sessions.db.
 *
 * Two different owners share this data, so the split matters:
 *
 *   - Messages are written by the backend, not here. python/chat_persist.py
 *     records each chat round as it happens; if the renderer also saved its
 *     in-memory list, every row would land twice. So `saveConversation` only
 *     carries metadata (create on first sight, rename when the title moved)
 *     and message bodies are read back with `loadMessages`.
 *
 *   - Browsing state is the renderer's: which conversation is open, which are
 *     pinned, whether the demo seed ran. That lives in localStorage, because
 *     those are per-window preferences, not facts about the user's history.
 */
import type { Message, Role, Session } from '../types';
import type { ConversationRepo, PersistedConversation } from './persistence';

// ---------------------------------------------------------------------------
// Bridge shape (mirrors electron/preload.ts `agent.sessions`)
// ---------------------------------------------------------------------------

interface SessionRow {
  id: string;
  title?: string;
  createdAt?: number | string;
  updatedAt?: number | string;
  model?: string;
  messageCount?: number;
}

interface MessageRow {
  id?: string;
  role?: string;
  content?: unknown;
  /** 独立的思考通道（messages.reasoning，2026-09-24 起落库）。 */
  reasoning?: unknown;
  createdAt?: number | string;
  created_at?: number | string;
  ordinal?: number;
}

interface SessionsBridge {
  sessions: {
    list(includeDeleted?: boolean): Promise<{ sessions: SessionRow[] }>;
    create(p: { sessionId?: string; title?: string; model?: string }):
      Promise<{ id: string; title: string }>;
    rename(sessionId: string, title: string): Promise<unknown>;
    remove(sessionId: string): Promise<unknown>;
    messages(sessionId: string, includeInactive?: boolean): Promise<{ messages: MessageRow[] }>;
    search(query: string, sessionId?: string): Promise<{ hits: Array<MessageRow & { sessionId?: string }> }>;
  };
}

export function sidecarSessions(): SessionsBridge['sessions'] | null {
  const w = globalThis as unknown as { agent?: Partial<SessionsBridge> };
  const api = w.agent?.sessions;
  return api && typeof api.list === 'function' ? api : null;
}

// ---------------------------------------------------------------------------
// Mapping
// ---------------------------------------------------------------------------

const LS_ACTIVE = 'workbuddy.activeSession';
const LS_PINNED = 'workbuddy.pinnedSessions';
const LS_SEEDED = 'workbuddy.seeded.v1';

/** Accept both epoch-ms numbers and timestamp strings; unknown shapes fall back. */
function toMs(v: number | string | undefined, fallback: number): number {
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'string' && v) {
    // SQLite returns "YYYY-MM-DD HH:MM:SS" in UTC with no zone marker. Parsed
    // as-is the browser would read it as local time, so a session created a
    // minute ago would show up eight hours old here.
    const naive = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$/.test(v);
    const parsed = Date.parse(naive ? v.replace(' ', 'T') + 'Z' : v);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return fallback;
}

/**
 * content 有两种形态：纯文本，或「图 / 文混排」的 content 数组（引擎发出去的那版）。
 * 落库时数组被存成 JSON 文本，重载会话直接当正文渲染就会漏出一大坨 base64
 * （2026-09-28 用户报的「回到完整界面是原始 base64」）。这里统一拆开：
 * 文本归正文、图归 images（data URL，气泡直接当图渲染）。
 */
function contentParts(content: unknown): { text: string; images: string[] } {
  let blocks: unknown[] | null = null;
  if (Array.isArray(content)) {
    blocks = content;
  } else if (typeof content === 'string') {
    const s = content.trim();
    if (s.startsWith('[{') && s.includes('"type"')) {
      try {
        const parsed: unknown = JSON.parse(s);
        if (Array.isArray(parsed)) blocks = parsed;
      } catch {
        /* 不是 JSON，就老老实实当纯文本 */
      }
    }
  }
  if (!blocks) return { text: typeof content === 'string' ? content : '', images: [] };
  const text = blocks
    .map(b => (b && typeof b === 'object' && (b as { type?: string }).type === 'text'
      ? String((b as { text?: string }).text ?? '')
      : ''))
    .filter(Boolean)
    .join('\n');
  const images = blocks
    .map(b => (b && typeof b === 'object' && (b as { type?: string }).type === 'image_url'
      ? String((b as { image_url?: { url?: string } }).image_url?.url ?? '')
      : ''))
    .filter(Boolean);
  return { text, images };
}

function textOf(content: unknown): string {
  return contentParts(content).text;
}

/**
 * 思考标签的兜底拆分（2026-09-24）。
 *
 * 引擎侧已经改成正文只收分流后的 body、思考单独落 reasoning 列；这里再兜一层，
 * 是为了那些修数据之前落库、正文里还夹着 <thinking> 原文的老消息 —— 重载会话
 * 时把它们就地拆成「思考 / 正文」，别再让思考当正文显示。
 */
const THINK_TAGS = ['thinking', 'think', 'reasoning', 'thought', 'REASONING_SCRATCHPAD'];

function splitThinking(raw: string): { reason: string; body: string } {
  let body = raw;
  const parts: string[] = [];
  for (const tag of THINK_TAGS) {
    const re = new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`, 'gi');
    body = body.replace(re, (_m: string, inner: string) => {
      const text = String(inner).trim();
      if (text) parts.push(text);
      return '';
    });
  }
  return { reason: parts.join('\n\n').trim(), body: body.trim() };
}

function readPinned(): Set<string> {
  try {
    const raw = localStorage.getItem(LS_PINNED);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(Array.isArray(arr) ? arr.filter(x => typeof x === 'string') : []);
  } catch {
    return new Set();
  }
}

function writePinned(ids: Set<string>): void {
  try {
    localStorage.setItem(LS_PINNED, JSON.stringify([...ids]));
  } catch {
    /* storage blocked: pinning just does not survive the session */
  }
}

export class SidecarConversationRepo implements ConversationRepo {
  private bridge: SessionsBridge['sessions'];
  /** Ids we know exist server-side, so save() does not re-create them. */
  private known = new Set<string>();
  /** Title we last sent per session, so debounced saves stay quiet. */
  private sentTitle = new Map<string, string>();
  private pinned = new Set<string>();

  constructor(bridge: SessionsBridge['sessions']) {
    this.bridge = bridge;
  }

  open(): Promise<void> {
    this.pinned = readPinned();
    return Promise.resolve();
  }

  async listConversations(): Promise<PersistedConversation[]> {
    const res = await this.bridge.list(false);
    const rows = Array.isArray(res?.sessions) ? res.sessions : [];
    const now = Date.now();
    const out: PersistedConversation[] = [];
    for (const row of rows) {
      if (!row || typeof row.id !== 'string') continue;
      this.known.add(row.id);
      out.push({
        id: row.id,
        title: row.title || '新会话',
        createdAt: toMs(row.createdAt, now),
        updatedAt: toMs(row.updatedAt, now),
        modelId: row.model || '',
        pinned: this.pinned.has(row.id),
        // Bodies are fetched per session (loadMessages); the sidebar shows
        // previews from the rows it has rather than holding every transcript.
        messages: []
      });
    }
    return out;
  }

  async getConversation(id: string): Promise<PersistedConversation | null> {
    const rows = await this.listConversations();
    const row = rows.find(r => r.id === id);
    if (!row) return null;
    return { ...row, messages: await this.loadMessages(id) };
  }

  /** Messages for one session, mapped to the renderer's model. */
  async loadMessages(id: string): Promise<Message[]> {
    const res = await this.bridge.messages(id, false);
    const rows = Array.isArray(res?.messages) ? res.messages : [];
    const out: Message[] = [];
    const now = Date.now();
    rows.forEach((row, i) => {
      const role = String(row?.role ?? '');
      if (role !== 'user' && role !== 'assistant' && role !== 'system') return;
      const parts = contentParts(row?.content);
      const split = splitThinking(parts.text);
      const content = split.body;
      const images = parts.images;
      // 思考单独一列（老行可能还夹在正文里，兜底拆出来），渲染成折叠块。
      const stored = textOf(row?.reasoning);
      const thinking = [split.reason, stored].filter(Boolean).join('\n\n').trim();
      // Tool rows and tool-call-only assistant rows exist server-side (the
      // backend records them) but have no bubble of their own in this UI.
      if (!content && !thinking && !images.length) return;
      out.push({
        id: String(row?.id ?? `${id}-${i}`),
        role: role as Role,
        content,
        ...(thinking ? { thinking } : {}),
        ...(images.length ? { images } : {}),
        createdAt: toMs(row?.createdAt ?? row?.created_at, now)
      });
    });
    return out;
  }

  async saveConversation(c: PersistedConversation): Promise<void> {
    if (!c?.id) return;
    const title = c.title || '新会话';
    if (!this.known.has(c.id)) {
      await this.bridge.create({
        sessionId: c.id,
        title,
        ...(c.modelId ? { model: c.modelId } : {})
      });
      this.known.add(c.id);
      this.sentTitle.set(c.id, title);
      return;
    }
    // The store flushes on a debounce after every turn, so only a real title
    // change is worth a round-trip.
    if (this.sentTitle.get(c.id) === title) return;
    await this.bridge.rename(c.id, title);
    this.sentTitle.set(c.id, title);
  }

  async deleteConversation(id: string): Promise<void> {
    await this.bridge.remove(id);
    this.known.delete(id);
    if (this.pinned.delete(id)) writePinned(this.pinned);
  }

  /**
   * The store seeds demo conversations on first launch. With a real store
   * behind us that would push mock rows into the user's history, so seeding is
   * recorded as done and nothing is written.
   */
  bulkSeed(): Promise<void> {
    this.markSeeded();
    return Promise.resolve();
  }

  /** Set by the store when a session's pin toggles (there is no column for it). */
  setPinned(id: string, pinned: boolean): void {
    if (pinned) this.pinned.add(id);
    else this.pinned.delete(id);
    writePinned(this.pinned);
  }

  /** FTS5 search over stored messages; no sessionId means every session. */
  async searchMessages(query: string, sessionId?: string): Promise<Array<{ sessionId: string; text: string }>> {
    const res = await this.bridge.search(query, sessionId);
    const hits = Array.isArray(res?.hits) ? res.hits : [];
    return hits.map(h => ({
      sessionId: String(h?.sessionId ?? ''),
      text: textOf(h?.content)
    }));
  }

  // -- browsing state (renderer-local) --------------------------------------

  getActive(): Promise<string | null> {
    try {
      return Promise.resolve(localStorage.getItem(LS_ACTIVE));
    } catch {
      return Promise.resolve(null);
    }
  }

  setActive(id: string): Promise<void> {
    try {
      localStorage.setItem(LS_ACTIVE, id);
    } catch {
      /* ignore */
    }
    return Promise.resolve();
  }

  isSeeded(): Promise<boolean> {
    try {
      return Promise.resolve(localStorage.getItem(LS_SEEDED) === '1');
    } catch {
      return Promise.resolve(false);
    }
  }

  markSeeded(): Promise<void> {
    try {
      localStorage.setItem(LS_SEEDED, '1');
    } catch {
      /* ignore */
    }
    return Promise.resolve();
  }
}

/** Convenience for the store: does this build have a real session store? */
export function hasSidecarStore(): boolean {
  return sidecarSessions() !== null;
}

/** Session list hygiene: newest first, pinned on top. Mirrors the store's sort. */
export function sortSessions(rows: Session[]): Session[] {
  return [...rows].sort((a, b) => {
    if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1;
    return b.updatedAt - a.updatedAt;
  });
}