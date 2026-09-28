/**
 * IndexedDB-backed persistence for WorkBuddy's session store.
 *
 * Goals
 *   - Native IndexedDB only (no npm deps).
 *   - Repository interface so the storage backend can be swapped
 *     (SQLite, real backend, in-memory tests) without touching the UI.
 *   - All IO wrapped in try/catch — persistence failures must NEVER crash
 *     the app. They are surfaced via console warnings + a returned boolean.
 *   - StrictMode-safe: openDB() is idempotent and the initial seed
 *     runs at most once per database (guarded by a sentinel key).
 *
 * Schema (DB: "workbuddy", version 1)
 *   - conversations  keyPath 'id'
 *       { id, title, createdAt, updatedAt, modelId, pinned?, unread?,
 *         workspace?, messages: Message[] }
 *   - kv  keyPath 'k'
 *       { k: 'activeSessionId' | 'seeded', v: any }
 */
import type { Message, Session } from '../types';
import { SidecarConversationRepo, sidecarSessions } from './sidecarRepo';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** Conversation row persisted to IndexedDB. */
export interface PersistedConversation {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  modelId: string;
  pinned?: boolean;
  unread?: boolean;
  workspace?: string;
  /** Messages belonging to this conversation. Held in full so the sidebar
     *  can search message bodies without an extra round-trip. */
  messages: Message[];
}

/** Loose shape for a session seed pair. */
export interface ConversationSeed {
  session: Session;
  messages: Message[];
}

export interface ConversationRepo {
  /** Open / upgrade the database. Safe to call multiple times. */
  open(): Promise<void>;

  /** Read every conversation. Order is unspecified; sort by updatedAt on caller. */
  listConversations(): Promise<PersistedConversation[]>;

  /** Read a single conversation by id, or null if missing. */
  getConversation(id: string): Promise<PersistedConversation | null>;

  /**
   * Message bodies for one conversation.
   *
   * Stores that keep rows server-side (sidecarRepo) return the transcripts
   * here instead of loading every session's full text up front; the
   * IndexedDB store simply hands back what it already has.
   */
  loadMessages(id: string): Promise<Message[]>;

  /** Upsert a conversation (replaces messages in full). */
  saveConversation(c: PersistedConversation): Promise<void>;

  /** Drop a conversation. No-op if it doesn't exist. */
  deleteConversation(id: string): Promise<void>;

  /** Bulk replace — used when the seed runs on first launch. */
  bulkSeed(rows: PersistedConversation[]): Promise<void>;

  // kv
  getActive(): Promise<string | null>;
  setActive(id: string): Promise<void>;

  /** Has the seed already been written? Used to avoid reseeding. */
  isSeeded(): Promise<boolean>;
  markSeeded(): Promise<void>;
}

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

const DB_NAME = 'workbuddy';
const DB_VERSION = 1;
const STORE_CONVERSATIONS = 'conversations';
const STORE_KV = 'kv';

const SEED_FLAG = 'seeded:v1';

export class IndexedDbConversationRepo implements ConversationRepo {
  private dbPromise: Promise<IDBDatabase> | null = null;

  open(): Promise<void> {
    if (this.dbPromise) return this.dbPromise.then(() => undefined);
    this.dbPromise = new Promise<IDBDatabase>((resolve, reject) => {
      if (typeof indexedDB === 'undefined') {
        reject(new Error('IndexedDB unavailable in this environment'));
        return;
      }
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE_CONVERSATIONS)) {
          db.createObjectStore(STORE_CONVERSATIONS, { keyPath: 'id' });
        }
        if (!db.objectStoreNames.contains(STORE_KV)) {
          db.createObjectStore(STORE_KV, { keyPath: 'k' });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error ?? new Error('indexedDB.open failed'));
      req.onblocked = () => reject(new Error('indexedDB.open blocked'));
    });
    return this.dbPromise.then(() => undefined).catch(err => {
      // Reset so a later call can try again instead of caching a rejection.
      this.dbPromise = null;
      throw err;
    });
  }

  private async tx(
    stores: string | string[],
    mode: IDBTransactionMode
  ): Promise<IDBTransaction> {
    await this.open();
    return this.dbPromise!.then(db => db.transaction(stores, mode));
  }

  private promisify<T>(req: IDBRequest<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error ?? new Error('indexedDB request failed'));
    });
  }

  async listConversations(): Promise<PersistedConversation[]> {
    try {
      const tx = await this.tx(STORE_CONVERSATIONS, 'readonly');
      const store = tx.objectStore(STORE_CONVERSATIONS);
      const all = await this.promisify(store.getAll());
      return (all as PersistedConversation[]) ?? [];
    } catch (err) {
      console.warn('[persistence] listConversations failed', err);
      return [];
    }
  }

  async getConversation(id: string): Promise<PersistedConversation | null> {
    try {
      const tx = await this.tx(STORE_CONVERSATIONS, 'readonly');
      const store = tx.objectStore(STORE_CONVERSATIONS);
      const row = await this.promisify(store.get(id));
      return (row as PersistedConversation) ?? null;
    } catch (err) {
      console.warn('[persistence] getConversation failed', err);
      return null;
    }
  }

  /** Bodies are stored inside the row here, so this is just a read. */
  async loadMessages(id: string): Promise<Message[]> {
    const row = await this.getConversation(id);
    return row?.messages ?? [];
  }

  async saveConversation(c: PersistedConversation): Promise<void> {
    try {
      const tx = await this.tx(STORE_CONVERSATIONS, 'readwrite');
      const store = tx.objectStore(STORE_CONVERSATIONS);
      await this.promisify(store.put(c));
    } catch (err) {
      console.warn('[persistence] saveConversation failed', err);
    }
  }

  async deleteConversation(id: string): Promise<void> {
    try {
      const tx = await this.tx(STORE_CONVERSATIONS, 'readwrite');
      const store = tx.objectStore(STORE_CONVERSATIONS);
      await this.promisify(store.delete(id));
    } catch (err) {
      console.warn('[persistence] deleteConversation failed', err);
    }
  }

  async bulkSeed(rows: PersistedConversation[]): Promise<void> {
    try {
      const tx = await this.tx([STORE_CONVERSATIONS, STORE_KV], 'readwrite');
      const conv = tx.objectStore(STORE_CONVERSATIONS);
      const kv = tx.objectStore(STORE_KV);
      for (const row of rows) {
        conv.put(row);
      }
      kv.put({ k: SEED_FLAG, v: Date.now() });
      await new Promise<void>((resolve, reject) => {
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error ?? new Error('bulkSeed tx error'));
        tx.onabort = () => reject(tx.error ?? new Error('bulkSeed tx abort'));
      });
    } catch (err) {
      console.warn('[persistence] bulkSeed failed', err);
    }
  }

  async getActive(): Promise<string | null> {
    try {
      const tx = await this.tx(STORE_KV, 'readonly');
      const row = await this.promisify(tx.objectStore(STORE_KV).get('activeSessionId'));
      return row ? String((row as { v: unknown }).v ?? '') : null;
    } catch (err) {
      console.warn('[persistence] getActive failed', err);
      return null;
    }
  }

  async setActive(id: string): Promise<void> {
    try {
      const tx = await this.tx(STORE_KV, 'readwrite');
      await this.promisify(tx.objectStore(STORE_KV).put({ k: 'activeSessionId', v: id }));
    } catch (err) {
      console.warn('[persistence] setActive failed', err);
    }
  }

  async isSeeded(): Promise<boolean> {
    try {
      const tx = await this.tx(STORE_KV, 'readonly');
      const row = await this.promisify(tx.objectStore(STORE_KV).get(SEED_FLAG));
      return !!row;
    } catch (err) {
      console.warn('[persistence] isSeeded failed', err);
      return false;
    }
  }

  async markSeeded(): Promise<void> {
    try {
      const tx = await this.tx(STORE_KV, 'readwrite');
      await this.promisify(tx.objectStore(STORE_KV).put({ k: SEED_FLAG, v: Date.now() }));
    } catch (err) {
      console.warn('[persistence] markSeeded failed', err);
    }
  }
}

// ---------------------------------------------------------------------------
// Test / future backend hook
// ---------------------------------------------------------------------------

/** In-memory backend, useful for tests or SSR previews. Implements the same
 *  interface so the store doesn't care which one is wired in. */
export class InMemoryConversationRepo implements ConversationRepo {
  private rows = new Map<string, PersistedConversation>();
  private kv = new Map<string, unknown>();
  private seededFlag = false;

  async open(): Promise<void> {
    /* no-op */
  }
  async listConversations(): Promise<PersistedConversation[]> {
    return Array.from(this.rows.values());
  }
  async getConversation(id: string): Promise<PersistedConversation | null> {
    return this.rows.get(id) ?? null;
  }
  async loadMessages(id: string): Promise<Message[]> {
    return this.rows.get(id)?.messages ?? [];
  }
  async saveConversation(c: PersistedConversation): Promise<void> {
    this.rows.set(c.id, c);
  }
  async deleteConversation(id: string): Promise<void> {
    this.rows.delete(id);
  }
  async bulkSeed(rows: PersistedConversation[]): Promise<void> {
    for (const r of rows) this.rows.set(r.id, r);
    this.seededFlag = true;
  }
  async getActive(): Promise<string | null> {
    const v = this.kv.get('activeSessionId');
    return typeof v === 'string' ? v : null;
  }
  async setActive(id: string): Promise<void> {
    this.kv.set('activeSessionId', id);
  }
  async isSeeded(): Promise<boolean> {
    return this.seededFlag;
  }
  async markSeeded(): Promise<void> {
    this.seededFlag = true;
  }
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

/** Pick the right backend for the current runtime. Always returns a working
 *  implementation — callers don't have to null-check. */
export function createDefaultRepo(): ConversationRepo {
  // Inside Electron the sidecar owns the history (sessions.db), so it wins.
  // IndexedDB stays as the fallback for a plain browser (vite dev, previews)
  // where there is no bridge to reach.
  const bridge = sidecarSessions();
  if (bridge) return new SidecarConversationRepo(bridge);
  if (typeof indexedDB !== 'undefined') {
    return new IndexedDbConversationRepo();
  }
  return new InMemoryConversationRepo();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a PersistedConversation from a Session + its messages. */
export function toPersisted(s: Session, messages: Message[]): PersistedConversation {
  return {
    id: s.id,
    title: s.title,
    createdAt: s.createdAt,
    updatedAt: s.updatedAt,
    modelId: s.modelId,
    pinned: s.pinned,
    unread: s.unread,
    workspace: s.workspace,
    messages
  };
}