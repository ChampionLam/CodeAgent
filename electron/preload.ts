/**
 * Preload script. The only bridge between renderer (sandboxed, no Node) and
 * the privileged main world. Whitelist only.
 *
 * The renderer gets exactly these methods and nothing else — no fs, no spawn,
 * no ipcRenderer passthrough. Anything the UI wants to do has to exist here
 * and therefore be reviewed.
 */
import { contextBridge, ipcRenderer, webUtils } from "electron";

export interface SidecarStatusLike {
  state: string;
  backendVersion?: string;
  protocolVersion?: number;
  pid?: number;
  capabilities?: string[];
  lastError?: { code: string; message: string };
  restartAttempts: number;
  startedAt: number;
}

export interface SidecarEventLike {
  event: string;
  chatId?: string;
  data: Record<string, unknown>;
}

export interface ChatParams {
  messages: Array<Record<string, unknown>>;
  images?: string[];
  useTools?: boolean;
  sessionId?: string;
  workspaceRoot?: string;
  systemPrompt?: string;
  model?: string;
  maxTokens?: number;
  maxLoop?: number;
}

const api = {
  // --- lifecycle / diagnostics -------------------------------------------
  onStatus(cb: (status: SidecarStatusLike) => void): () => void {
    const wrapped = (_e: unknown, status: SidecarStatusLike): void => cb(status);
    ipcRenderer.on("agent:status", wrapped);
    return () => ipcRenderer.removeListener("agent:status", wrapped);
  },
  async getStatus(): Promise<SidecarStatusLike> {
    return ipcRenderer.invoke("agent:status") as Promise<SidecarStatusLike>;
  },
  async ping(): Promise<unknown> {
    return ipcRenderer.invoke("agent:ping");
  },
  async info(): Promise<unknown> {
    return ipcRenderer.invoke("agent:info");
  },

  /**
   * Window buttons for the frameless window: the app's TopBar *is* the title
   * bar, so it drives minimise / maximise / close itself.
   */
  winControls: {
    async action(a: "minimize" | "maximize" | "close"): Promise<boolean> {
      return ipcRenderer.invoke("win:action", a) as Promise<boolean>;
    },
    async isMaximized(): Promise<boolean> {
      return ipcRenderer.invoke("win:is-maximized") as Promise<boolean>;
    },
    onMaximizedChange(cb: (v: boolean) => void): () => void {
      const wrapped = (_e: unknown, v: boolean): void => cb(v);
      ipcRenderer.on("win:maximized", wrapped);
      return () => ipcRenderer.removeListener("win:maximized", wrapped);
    },
  },

  // --- agent surface ------------------------------------------------------
  /** Tool schemas + which permission level each tool sits at. */
  async tools(): Promise<unknown> {
    return ipcRenderer.invoke("agent:tools");
  },

  /** Installed skills (name / description / whenToUse / builtin ...). */
  async skills(): Promise<unknown> {
    return ipcRenderer.invoke("agent:skills");
  },

  /** Enable or disable one tool; resolves with the refreshed tools payload. */
  async toolSetEnabled(input: { name: string; enabled: boolean }): Promise<unknown> {
    return ipcRenderer.invoke("agent:toolToggle", input);
  },

  /** Configured models (no key values, only `hasKey`). */
  async models(): Promise<unknown> {
    return ipcRenderer.invoke("agent:models");
  },

  /** Ask a vendor which models it serves (GET /models), for the picker. */
  async modelFetch(payload: Record<string, unknown>): Promise<unknown> {
    return ipcRenderer.invoke("agent:modelFetch", payload);
  },

  /** Provider presets for the "add model" form. */
  async modelPresets(): Promise<unknown> {
    return ipcRenderer.invoke("agent:modelPresets");
  },

  /** Add or update a model. `apiKey` (if present) is written to the env file. */
  async modelUpsert(payload: Record<string, unknown>): Promise<unknown> {
    return ipcRenderer.invoke("agent:modelUpsert", payload);
  },

  async modelRemove(id: string): Promise<unknown> {
    return ipcRenderer.invoke("agent:modelRemove", id);
  },

  async modelSetDefault(id: string): Promise<unknown> {
    return ipcRenderer.invoke("agent:modelSetDefault", id);
  },

  /** 视觉线路：读图走哪个模型（会话模型不吃图时用它把图读成文字）。 */
  async vision(): Promise<unknown> {
    return ipcRenderer.invoke("agent:vision");
  },

  async visionSet(id: string): Promise<unknown> {
    return ipcRenderer.invoke("agent:visionSet", id);
  },

  /** Start a chat; events arrive on onEvent. Resolves with { chatId }. */
  async chat(params: ChatParams): Promise<{ chatId: string }> {
    return ipcRenderer.invoke("agent:chat", params) as Promise<{ chatId: string }>;
  },

  async cancel(chatId: string): Promise<unknown> {
    return ipcRenderer.invoke("agent:cancel", chatId);
  },

  /** Answer an approval.request. decision: allow_once | allow_always | deny */
  async approval(approvalId: string, decision: string): Promise<unknown> {
    return ipcRenderer.invoke("agent:approval", { approvalId, decision });
  },

  /** Native "pick images" dialog; resolves with absolute paths ([] if cancelled). */
  async pickImages(): Promise<string[]> {
    return ipcRenderer.invoke("agent:pickImages") as Promise<string[]>;
  },

  /**
   * Session store, owned by the sidecar (sessions.db). The list survives a
   * restart; message bodies stay server-side and are fetched per session, so
   * the renderer never holds every conversation's full text in memory.
   */
  sessions: {
    async list(includeDeleted = false): Promise<{ sessions: unknown[] }> {
      return ipcRenderer.invoke("agent:sessions", { includeDeleted }) as Promise<{ sessions: unknown[] }>;
    },
    /** Create a session. The caller may pass its own sessionId; the store mints one otherwise. */
    async create(payload: { sessionId?: string; title?: string; model?: string }):
      Promise<{ id: string; title: string }> {
      return ipcRenderer.invoke("agent:sessionCreate", payload) as Promise<{ id: string; title: string }>;
    },
    async rename(sessionId: string, title: string): Promise<unknown> {
      return ipcRenderer.invoke("agent:sessionRename", { sessionId, title });
    },
    async remove(sessionId: string): Promise<unknown> {
      return ipcRenderer.invoke("agent:sessionDelete", { sessionId });
    },
    async messages(sessionId: string, includeInactive = false): Promise<{ messages: unknown[] }> {
      return ipcRenderer.invoke("agent:sessionMessages",
        { sessionId, includeInactive }) as Promise<{ messages: unknown[] }>;
    },
    /** FTS5 search over stored messages; omit sessionId to search every session. */
    async search(query: string, sessionId?: string): Promise<{ hits: unknown[] }> {
      return ipcRenderer.invoke("agent:messagesSearch",
        sessionId ? { query, sessionId } : { query }) as Promise<{ hits: unknown[] }>;
    },
  },

  /**
   * Token / money aggregation for the bottom status bar and the 用量统计 page.
   * Everything comes from the sidecar's usage_log rows (one per finished turn),
   * so nothing here is estimated from message text.
   */
  usage: {
    async summary(params: { days?: number; sessionId?: string } = {}): Promise<Record<string, unknown>> {
      return ipcRenderer.invoke("agent:usage", params) as Promise<Record<string, unknown>>;
    },
  },

  /**
   * 运行断点（2026-09-25）：断网/断电/关程序之后接着跑。
   * pending = 这个会话有没有可续的任务（界面据此显示「继续」）；
   * active = 现在有没有任务在跑（关程序时用它决定要不要问用户）；
   * interrupt = 中断并保存断点；dismiss = 忽略这条断点提示。
   */
  runs: {
    async pending(sessionId?: string): Promise<Record<string, unknown>> {
      return ipcRenderer.invoke("agent:runPending", sessionId) as Promise<Record<string, unknown>>;
    },
    async active(): Promise<Record<string, unknown>> {
      return ipcRenderer.invoke("agent:runActive") as Promise<Record<string, unknown>>;
    },
    async interrupt(runId: string, reason?: string): Promise<Record<string, unknown>> {
      return ipcRenderer.invoke("agent:runInterrupt", { runId, reason }) as Promise<Record<string, unknown>>;
    },
    async dismiss(runId: string): Promise<Record<string, unknown>> {
      return ipcRenderer.invoke("agent:runDismiss", runId) as Promise<Record<string, unknown>>;
    },
  },

  /** 关程序但还有任务在跑：主进程问用户怎么办。返回取消订阅函数。 */
  onCloseConfirm(cb: (payload: { runs: Array<Record<string, unknown>> }) => void): () => void {
    const wrapped = (_e: unknown, payload: { runs: Array<Record<string, unknown>> }): void => cb(payload);
    ipcRenderer.on("close:confirm", wrapped);
    return () => ipcRenderer.removeListener("close:confirm", wrapped);
  },

  /** 回答关程序的问题：'wait' | 'interrupt' | 'cancel'。 */
  closeChoice(choice: string): void {
    ipcRenderer.send("close:choice", choice);
  },

  /**
   * Real path of a File the user picked/dropped in the renderer. Electron
   * removed `File.path` in v32, so this is the supported replacement; with the
   * sandbox on, the renderer cannot reach webUtils itself, hence the bridge.
   */
  pathForFile(file: File): string {
    try {
      return webUtils.getPathForFile(file);
    } catch {
      return "";
    }
  },
  /**
   * 把剪贴板里的位图（截图）落成临时文件并返回真实路径。
   * 截图在剪贴板里没有文件路径，附件管线又只认路径，所以必须走这一步。
   */
  saveTempImage(payload: { dataUrl: string; name?: string }): Promise<string> {
    return ipcRenderer.invoke("agent:saveTempImage", payload);
  },

  /**
   * Open the in-app screenshot overlay (composer button or Ctrl+Shift+S).
   * Resolves once the overlay is up; the result comes back via onSnipAttached.
   */
  snipStart(): Promise<boolean> {
    return ipcRenderer.invoke("snip:start") as Promise<boolean>;
  },
  /** A finished screenshot arrives as a temp-file path, ready for the attachments. */
  onSnipAttached(cb: (path: string) => void): () => void {
    const wrapped = (_e: unknown, p: unknown): void => cb(String(p ?? ""));
    ipcRenderer.on("snip:attached", wrapped);
    return () => ipcRenderer.removeListener("snip:attached", wrapped);
  },

  /**
   * 用系统默认浏览器打开外链（消息里的引用 [1] 和普通链接都走这里）。
   * 应用窗口是 kiosk，外链不能在应用内打开。
   */
  openExternal(url: string): Promise<boolean> {
    return ipcRenderer.invoke("agent:openExternal", url) as Promise<boolean>;
  },

  /** 打开对话里附件的本地文件（系统默认程序）。 */
  openPath(path: string): Promise<{ ok: boolean; reason?: string }> {
    return ipcRenderer.invoke("agent:openPath", path) as Promise<{ ok: boolean; reason?: string }>;
  },

  /** 在资源管理器里定位该文件。 */
  showItemInFolder(path: string): Promise<{ ok: boolean; reason?: string }> {
    return ipcRenderer.invoke("agent:showItemInFolder", path) as Promise<{ ok: boolean; reason?: string }>;
  },

  /**
   * 另存为（图表导出的 PNG / SVG）。主进程只认 data: URL，会弹系统保存框；
   * 用户取消时返回 { ok: false, reason: 'canceled' }。
   */
  saveFile(name: string, dataUrl: string): Promise<{ ok: boolean; path?: string; reason?: string }> {
    return ipcRenderer.invoke("agent:saveFile", name, dataUrl) as Promise<{
      ok: boolean;
      path?: string;
      reason?: string;
    }>;
  },

  /**
   * 告诉主进程哪个会话正开着。hub（Ctrl+Alt+H）默认接着这条对话提问，
   * 于是上下文、模型、所有设置都和主窗一致，答完就落在同一条会话里。
   */
  setActiveSession(sessionId: string): void {
    ipcRenderer.send("session:active", sessionId);
  },

  /** 主进程让我们切到某条会话（hub 的「打开完整对话」）。 */
  onHubOpenSession(cb: (sessionId: string) => void): () => void {
    const wrapped = (_e: unknown, id: unknown): void => cb(String(id ?? ""));
    ipcRenderer.on("hub:openSession", wrapped);
    return () => ipcRenderer.removeListener("hub:openSession", wrapped);
  },

  /** 某条会话在别处（hub）进了新消息，主窗按需重取，切回来就能看见。 */
  onSessionRefresh(cb: (sessionId: string) => void): () => void {
    const wrapped = (_e: unknown, id: unknown): void => cb(String(id ?? ""));
    ipcRenderer.on("session:refresh", wrapped);
    return () => ipcRenderer.removeListener("session:refresh", wrapped);
  },

  /** Every sidecar event, in arrival order. Returns an unsubscribe function. */
  onEvent(cb: (ev: SidecarEventLike) => void): () => void {
    const wrapped = (_e: unknown, ev: SidecarEventLike): void => cb(ev);
    ipcRenderer.on("agent:event", wrapped);
    return () => ipcRenderer.removeListener("agent:event", wrapped);
  },
};

contextBridge.exposeInMainWorld("agent", api);

export type AgentApi = typeof api;