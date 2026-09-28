/**
 * Global app store — single source of truth for the prototype.
 * In production this would be replaced by IPC events from the Electron main process.
 */
import {
  createContext, useContext, useState, useCallback, useEffect, useRef,
  type ReactNode
} from 'react';
import type {
  Session, Message, ModelConfig, PermissionRule,
  ApprovalRequest, ToolCall, ToolStatus,
  ContextCompactionNotice, ThinkingDepth, ThinkingDisplay, TurnOverrides
} from '../types';
import type { SubagentBatch, SubagentNode, SubagentNodeStatus } from '../types';
import { getAgent, summarizeArguments, formatArguments, setWorkspaceRoot, fetchModels, fetchModelPresets, saveModelConfig, deleteModelConfig, setDefaultModelConfig } from './live';
import type { RunInfo } from './live';
import type { RemoteModelPreset } from './live';
import { fetchUsageSummary, type UsageSummaryRemote } from './usage';
import { attachmentSummary } from '../lib/attachmentKinds';
import {
  type AppSettings, loadAppSettings, saveAppSettings, applyAppSettings, parseAppSettings
} from '../lib/appSettings';
import { applyWallpaper } from '../lib/wallpapers';
import {
  createDefaultRepo, toPersisted,
  type ConversationRepo, type PersistedConversation
} from './persistence';
import { hasSidecarStore } from './sidecarRepo';

export type View = 'chat' | 'usage' | 'settings';

/* ── 思考显示偏好（纯界面偏好，存 localStorage）───────────────────────────
 * 用户要求：思考不是「不展示」，而是由用户控制。这里存的就是这个选择：
 *   折叠（默认）/ 展开 / 隐藏（生成中仍显示一行「思考中」指示，否则模型
 *   思考那几秒界面像是卡死）。
 * ---------------------------------------------------------------------- */
const THINKING_DISPLAY_KEY = 'workbuddy.thinkingDisplay';

export function readThinkingDisplay(): ThinkingDisplay {
  try {
    const v = localStorage.getItem(THINKING_DISPLAY_KEY);
    if (v === 'expanded' || v === 'hidden' || v === 'collapsed') return v;
  } catch { /* private mode / no storage: fall back to the default */ }
  return 'collapsed';
}

function writeThinkingDisplay(v: ThinkingDisplay): void {
  try { localStorage.setItem(THINKING_DISPLAY_KEY, v); } catch { /* ignore */ }
}
export type ExportFormat = 'json' | 'md';

const TURN_OVERRIDES_KEY = 'desk-turn-overrides';

/** 会话级覆盖：{ 会话id: { 模型id: {thinking, thinkingDepth, contextWindow} } }。
    按模型分开存（用户口径 2026-09-26：非当前模型也能在模型面板里设思考/上下文，
    只有点列表里的模型名字才是切换模型）。 */
export type TurnOverridesBySession = Record<string, Record<string, TurnOverrides>>;

/**
 * 会话级覆盖存本地：只影响请求参数，不碰模型配置/凭证。
 * 老版本形状是 { 会话id: {…覆盖} }（不分模型），那一层的值是 bool/number，
 * 这里按「第二层必须是对象」判掉——分不出它属于哪个模型，留着会串到别的模型上。
 * 丢掉的只是这种临时开关，模型配置本身不受影响。
 */
function loadTurnOverrides(): TurnOverridesBySession {
  try {
    const raw = window.localStorage?.getItem(TURN_OVERRIDES_KEY);
    const parsed = raw ? JSON.parse(raw) : {};
    if (!parsed || typeof parsed !== 'object') return {};
    const out: TurnOverridesBySession = {};
    for (const [sid, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (!v || typeof v !== 'object') continue;
      const perModel: Record<string, TurnOverrides> = {};
      for (const [mid, o] of Object.entries(v as Record<string, unknown>)) {
        if (o && typeof o === 'object') perModel[mid] = o as TurnOverrides;
      }
      if (Object.keys(perModel).length) out[sid] = perModel;
    }
    return out;
  } catch {
    return {};
  }
}

/** 合并一层的覆盖值：undefined = 删掉这条（回到跟随模型设置）。 */
function mergeOverrides(
  prev: TurnOverridesBySession, sid: string, mid: string, patch: TurnOverrides
): TurnOverridesBySession {
  const forSession = { ...(prev[sid] || {}) };
  const cur = { ...(forSession[mid] || {}) } as Record<string, unknown>;
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined) delete cur[k];
    else cur[k] = v;
  }
  if (Object.keys(cur).length === 0) delete forSession[mid];
  else forSession[mid] = cur as TurnOverrides;
  const next = { ...prev };
  if (Object.keys(forSession).length === 0) delete next[sid];
  else next[sid] = forSession;
  return next;
}

function saveTurnOverrides(v: Record<string, TurnOverrides>): void {
  try {
    window.localStorage?.setItem(TURN_OVERRIDES_KEY, JSON.stringify(v));
  } catch {
    /* 存不下就算了，不影响本轮请求 */
  }
}

interface State {
  view: View;
  sessions: Session[];
  activeSessionId: string;
  messages: Record<string, Message[]>;
  models: ModelConfig[];
  /** False until the registry has been read from the sidecar at least once. */
  modelsReady: boolean;
  /** Error text from the last model-registry call (key never involved). */
  modelsError: string | null;
  /** Mainstream provider presets for the add-model form. */
  modelPresets: RemoteModelPreset[];
  activeModelId: string;
  /**
   * 权限规则的内存表。规则页与规则计数已从界面下线（2026-09-25 权限重做），
   * 但 store 的读写接口保留给后端那一路改造，先不动。
   */
  rules: PermissionRule[];
  /** 真实用量聚合（后端 usage_log）。null = 还没取到（界面显示占位，不编数）。 */
  usage: UsageSummaryRemote | null;
  /** 重新拉一次用量。传 days 换统计窗口；不传 sessionId = 跟当前会话。 */
  refreshUsage: (sessionId?: string | null, days?: number) => Promise<void>;
  /** 会话内审批：当前待决的那条（在消息流里渲染，不再有全局弹窗）。 */
  pendingApprovalId: string | null;
  isStreaming: boolean;
  /** 当前会话有没有可续的断点（有就显示「继续」条）。 */
  pendingRun: RunInfo | null;
  sidebarCollapsed: boolean;
  /** Live sidecar state from the Electron main process ("ready", "crashed", …). */
  sidecarState: string;
  /** Last chat-level error, shown under the composer. */
  lastError: string | null;
  /** True once the IndexedDB hydration step finishes. While false, the UI
   *  may render a loading state to avoid showing stale or empty data. */
  hydrated: boolean;
  /** Latest non-blocking context-compaction notice (contract §7.5),
   *  keyed to the session the chat belongs to. Not a chat message. */
  compactionNotices: Record<string, ContextCompactionNotice>;
  /** Real token usage of the current session's last completed chat turn
   *  (chat.done usage.promptTokens) — used by the StatusBar context meter. */
  lastPromptTokens: number | null;
  /** Reasoning channel display mode — user-controlled (not hidden away). */
  thinkingDisplay: ThinkingDisplay;
  /** Whole-application appearance/preference set (theme, accent, fonts, send key).
   *  Per-machine UI preference: localStorage only, never config.json, never the sidecar. */
  appSettings: AppSettings;
  /** 本会话**当前模型**的思考/深度/上下文覆盖（输入区控制条）。
   *  空对象 = 全部跟随模型设置。 */
  turnOverrides: TurnOverrides;
  /** 本会话**所有模型**的覆盖：{ 模型id: 覆盖 }——模型面板悬停到哪个模型读哪个。 */
  sessionOverrides: Record<string, TurnOverrides>;
  /** 子代理进度（subagent.* 事件驱动）：输入区上方那条状态条读它。null = 本会话还没派过。 */
  subagentBatch: SubagentBatch | null;
}

interface Actions {
  setView(v: View): void;
  selectSession(id: string): void;
  createSession(): void;
  toggleSidebar(): void;
  setActiveModel(id: string): void;
  /** 会话内审批卡：允许（remember=true 时走「总是允许」口径）。 */
  approveApproval(id: string, remember: boolean): void;
  /** 会话内审批卡：拒绝，拒绝结果照旧回给模型改路线。 */
  denyApproval(id: string): void;
  send(text: string, images?: string[]): void;
  stopStreaming(): void;
  updateModel(id: string, patch: Partial<ModelConfig>): void;
  addModel(m: ModelConfig): void;
  deleteModel(id: string): void;
  /** Re-read the registry from the sidecar. Resolves false when it is not up yet. */
  refreshModels(): Promise<boolean>;
  /** Persist one model (add or edit). `apiKey`, if given, goes to the env file. */
  saveModel(payload: Record<string, unknown>): Promise<string>;
  removeModel(id: string): Promise<void>;
  setDefaultModel(id: string): Promise<void>;
  addRule(r: PermissionRule): void;
  deleteRule(id: string): void;
  resolveTool(toolId: string, status: ToolStatus, result?: string): void;
  // ── New (session management) ──────────────────────────────────────────
  renameSession(id: string, title: string): void;
  deleteSession(id: string): void;
  togglePin(id: string): void;
  exportSession(id: string, format: ExportFormat): void;
  /** Drop the compaction banner for a session (user dismissed it). */
  dismissCompactionNotice(sessionId: string): void;
  /** 接着跑上次被中断的任务（用户点「继续」才调）。 */
  resumeRun(): Promise<void>;
  /** 忽略这条断点提示。 */
  dismissPendingRun(): Promise<void>;
  /** Reasoning display: 折叠 / 展开 / 隐藏. Persisted locally. */
  setThinkingDisplay(v: ThinkingDisplay): void;
  /** 输入区控制条：改本会话当前模型的思考 / 深度 / 上下文（undefined = 跟随模型设置）。 */
  setTurnOverride(patch: TurnOverrides): void;
  /** 同上，但指定模型（模型面板悬停到哪个模型就改哪个）。 */
  setModelOverride(modelId: string, patch: TurnOverrides): void;
  /** 整机界面偏好：合并写入 + 立即应用到 :root + 落 localStorage。 */
  setAppSettings(patch: Partial<AppSettings>): void;
}

const Ctx = createContext<(State & Actions) | null>(null);

/** Index of the most recent streaming message, or -1. */
function lastStreamingIdx(list: Message[]): number {
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].streaming) return i;
  }
  return -1;
}

/** Coerce an event payload number to a non-negative number, or null. */
function toNonNegative(v: unknown): number | null {
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

/** Generate a short, human-readable title from the first user message.
 *  - strip newlines, collapse whitespace
 *  - trim to ~20 chars with ellipsis
 *  - fallback to "新会话" if input is empty. */
function deriveTitle(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  if (!flat) return '新会话';
  return flat.length > 20 ? flat.slice(0, 20) + '…' : flat;
}

/** Stable sort: pinned first, then by updatedAt desc. */
function sortSessions(list: Session[]): Session[] {
  return [...list].sort((a, b) => {
    if (Boolean(a.pinned) !== Boolean(b.pinned)) return a.pinned ? -1 : 1;
    return b.updatedAt - a.updatedAt;
  });
}

/** Find the next session to fall back to after a delete. */
function pickFallback(list: Session[], excludeId: string): string | null {
  const remaining = list.filter(s => s.id !== excludeId);
  if (remaining.length === 0) return null;
  const sorted = sortSessions(remaining);
  return sorted[0]?.id ?? null;
}

// ---------------------------------------------------------------------------
// Export helpers
// ---------------------------------------------------------------------------

function safeFileBase(title: string): string {
  return (title || 'session').replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 60) || 'session';
}

function triggerDownload(filename: string, blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, 0);
}

function exportConversation(
  session: Session,
  messages: Message[],
  format: ExportFormat
): void {
  const base = safeFileBase(session.title);
  const stamp = new Date(session.updatedAt).toISOString().slice(0, 19).replace(/[:T]/g, '-');
  if (format === 'json') {
    const payload = {
      session: {
        id: session.id, title: session.title,
        createdAt: session.createdAt, updatedAt: session.updatedAt,
        modelId: session.modelId, workspace: session.workspace
      },
      messages
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    triggerDownload(`${base}-${stamp}.json`, blob);
    return;
  }
  // md
  const lines: string[] = [];
  lines.push(`# ${session.title}`);
  lines.push('');
  lines.push(`- 会话 ID: \`${session.id}\``);
  lines.push(`- 模型: \`${session.modelId}\``);
  lines.push(`- 创建时间: ${new Date(session.createdAt).toISOString()}`);
  lines.push(`- 更新时间: ${new Date(session.updatedAt).toISOString()}`);
  lines.push('');
  lines.push('---');
  lines.push('');
  for (const m of messages) {
    const who = m.role === 'user' ? '👤 用户' : m.role === 'assistant' ? '🤖 助手' : '⚙️ 系统';
    lines.push(`## ${who} · ${new Date(m.createdAt).toISOString()}`);
    lines.push('');
    lines.push(m.content || '');
    if (m.toolCalls && m.toolCalls.length) {
      lines.push('');
      lines.push('### 工具调用');
      for (const tc of m.toolCalls) {
        lines.push(`- **${tc.name}** (${tc.level}, ${tc.status}): ${tc.summary}`);
      }
    }
    lines.push('');
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  triggerDownload(`${base}-${stamp}.md`, blob);
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

/** 工具名 → 人话（审批卡与工具卡都用它，别把命令原文糊到用户脸上）。 */
const TOOL_LABELS: Record<string, string> = {
  run_shell: '执行命令',
  read_file: '读取文件',
  write_file: '写入文件',
  edit_file: '修改文件',
  delete_path: '删除文件',
  list_dir: '列目录',
  search_files: '搜索文件',
  read_image: '查看图片',
  web_fetch: '抓取网页',
  web_search: '联网搜索',
  image_generate: '生成图片',
  video_generate: '生成视频',
  read_skill: '读取技能',
  list_skills: '列出技能'
};

/** 把某条审批就地标成已决：卡片从按钮变结果行（值用界面口径 allowed/denied）。 */
function decideApprovalInMessages(
  prev: Record<string, Message[]>, sid: string, approvalId: string,
  decision: ApprovalRequest['decision']
): Record<string, Message[]> {
  const list = prev[sid];
  if (!list) return prev;
  return {
    ...prev,
    [sid]: list.map(m =>
      m.approvals && m.approvals.some(a => a.id === approvalId)
        ? { ...m, approvals: m.approvals.map(a => (a.id === approvalId ? { ...a, decision } : a)) }
        : m
    )
  };
}

export function AppProvider({ children }: { children: ReactNode }) {
  const repoRef = useRef<ConversationRepo | null>(null);
  if (!repoRef.current) repoRef.current = createDefaultRepo();

  // ── Core state ────────────────────────────────────────────────────────
  const [view, setView] = useState<View>('chat');
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string>('');
  const [messages, setMessages] = useState<Record<string, Message[]>>({});
  // The registry is authoritative in the sidecar; never seed it from mocks.
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [activeModelId, setActiveModelId] = useState('');
  const [modelsReady, setModelsReady] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [modelPresets, setModelPresets] = useState<RemoteModelPreset[]>([]);
  // 输入区控制条的会话级覆盖：{ 会话id: {thinking, thinkingDepth, contextWindow} }。
  // 只影响请求参数，不写 .env / config.json；存本地，重开还在。
  const [turnOverridesBySession, setTurnOverridesBySession] = useState<TurnOverridesBySession>(() => loadTurnOverrides());
  const turnOverridesRef = useRef(turnOverridesBySession);
  useEffect(() => {
    turnOverridesRef.current = turnOverridesBySession;
    saveTurnOverrides(turnOverridesBySession);
  }, [turnOverridesBySession]);
  const [defaultModelId, setDefaultModelId] = useState('');
  const modelsRef = useRef<ModelConfig[]>([]);
  const defaultModelIdRef = useRef('');
  const activeModelIdRef = useRef('');
  useEffect(() => { modelsRef.current = models; }, [models]);
  useEffect(() => { defaultModelIdRef.current = defaultModelId; }, [defaultModelId]);
  useEffect(() => { activeModelIdRef.current = activeModelId; }, [activeModelId]);

  const refreshModels = useCallback(async (): Promise<boolean> => {
    try {
      const list = await fetchModels();
      const mapped: ModelConfig[] = list.items.map(m => ({
        id: m.id,
        name: m.label || m.model,
        baseUrl: m.baseUrl,
        model: m.model,
        provider: m.provider,
        apiKeyEnv: m.apiKeyEnv,
        hasKey: m.hasKey,
        maxTokens: m.maxTokens,
        timeoutSeconds: m.timeoutSeconds,
        inputModalities: m.inputModalities,
        contextWindow: m.contextWindow,
        // 厂商吃不吃思考开关（后端算好的），漏了这行面板就永远显示思考强度行。
        thinkingAdapted: m.thinkingAdapted,
        thinkingCanDisable: m.thinkingCanDisable,
        thinking: m.thinking ?? null,
        thinkingDepth: (m.thinkingDepth as ThinkingDepth | null | undefined) ?? null,
        // 深度能力字段必须从 models.list 一路带到界面：漏了这里，ComposerControls
        // 拿到的 softThinkingDepth 恒为 undefined，MiniMax 那种没有硬参数的厂商
        // 也会显示得像有个硬旋钮。
        thinkingDepthLevels: (m.thinkingDepthLevels as ThinkingDepth[] | undefined) ?? [],
        softThinkingDepth: Boolean(m.softThinkingDepth),
        effectiveMaxTokens: m.effectiveMaxTokens ?? null,
        maxTokensNote: m.maxTokensNote ?? null,
        temperature: m.temperature ?? null,
        topP: m.topP ?? null,
        inputPrice: 0,
        outputPrice: 0,
        isDefault: m.id === list.defaultId,
        remote: true
      }));
      setModels(mapped);
      setDefaultModelId(list.defaultId);
      setModelsReady(true);
      setModelsError(null);
      // Keep the current pick if it still exists, else fall back to the session's
      // own model, then to the global default.
      const keep = activeModelIdRef.current;
      if (!keep || !mapped.some(m => m.id === keep)) {
        const s = sessionsRef.current.find(x => x.id === activeIdRef.current);
        const fromSession = s?.modelId && mapped.some(m => m.id === s.modelId) ? s.modelId : '';
        setActiveModelId(fromSession || list.defaultId || mapped[0]?.id || '');
      }
      return true;
    } catch (e) {
      setModelsError(e instanceof Error ? e.message : String(e));
      return false;
    }
  }, []);

  /**
   * 输入区控制条：改本会话的思考 / 深度 / 上下文。传 undefined = 恢复跟随模型设置。
   */
  const setTurnOverride = useCallback((patch: TurnOverrides) => {
    const sid = activeIdRef.current;
    const mid = activeModelIdRef.current;
    if (!sid || !mid) return;
    setTurnOverridesBySession(prev => mergeOverrides(prev, sid, mid, patch));
  }, []);

  /**
   * 同上，但指定模型——模型面板悬停到哪个模型就改哪个（用户口径 2026-09-26：
   * 非当前模型也能在这里设定思考/上下文，只有点列表里的模型名字才是切换模型）。
   */
  const setModelOverride = useCallback((modelId: string, patch: TurnOverrides) => {
    const sid = activeIdRef.current;
    if (!sid || !modelId) return;
    setTurnOverridesBySession(prev => mergeOverrides(prev, sid, modelId, patch));
  }, []);

  /** Session-level override: picks apply to the active conversation only. */
  const setActiveModel = useCallback((id: string) => {
    setActiveModelId(id);
    const sid = activeIdRef.current;
    setSessions(prev => prev.map(s => (s.id === sid ? { ...s, modelId: id } : s)));
    window.setTimeout(() => flushPersist(sid), 0);
  }, []);

  const syncModelForSession = useCallback((sessionId: string) => {
    const s = sessionsRef.current.find(x => x.id === sessionId);
    const list = modelsRef.current;
    const wanted = s?.modelId;
    if (wanted && list.some(m => m.id === wanted)) setActiveModelId(wanted);
    else if (defaultModelIdRef.current) setActiveModelId(defaultModelIdRef.current);
    else if (list[0]) setActiveModelId(list[0].id);
  }, []);

  const saveModel = useCallback(async (payload: Record<string, unknown>): Promise<string> => {
    const id = await saveModelConfig(payload);
    await refreshModels();
    void fetchModelPresets().then(setModelPresets).catch(() => undefined);
    return id;
  }, [refreshModels]);

  const removeModel = useCallback(async (id: string): Promise<void> => {
    await deleteModelConfig(id);
    await refreshModels();
  }, [refreshModels]);

  const setDefaultModel = useCallback(async (id: string): Promise<void> => {
    await setDefaultModelConfig(id);
    await refreshModels();
  }, [refreshModels]);
  const [rules, setRules] = useState<PermissionRule[]>([]);
  // Real token/money aggregation from the sidecar's usage_log (one row per
  // finished turn). Replaces the old mockUsage block — the bar and the usage
  // page read this same object, so their numbers cannot drift apart.
  const [usageSummary, setUsageSummary] = useState<UsageSummaryRemote | null>(null);
  const usageDaysRef = useRef<number>(7);
  const refreshUsage = useCallback(async (sessionId?: string | null, days?: number): Promise<void> => {
    const wantDays = days ?? usageDaysRef.current;
    const sid = sessionId === undefined ? activeIdRef.current : sessionId;
    try {
      const r = await fetchUsageSummary(sid ? { days: wantDays, sessionId: sid } : { days: wantDays });
      if (r) setUsageSummary(r);
    } catch {
      // 取不到就保留上一次的数：宁可显示旧数，也不编一个数出来。
    }
  }, []);
  // 会话内审批（设计 §3）：approval.request 落到所属助手回合的
  // Message.approvals 里，由聊天流的 ApprovalCard 渲染；这里只记
  // 「当前待决那条的 id」，供唯一性判断，不再有全局弹窗状态。
  const [pendingApprovalId, setPendingApprovalId] = useState<string | null>(null);
  /** 子代理进度：只在内存里，不落库（重开应用只剩合并后的结果，这个不复活）。 */
  const [subagentBatch, setSubagentBatch] = useState<SubagentBatch | null>(null);
  const [isStreaming, setIsStreaming] = useState(false);
  const [userCollapsed, setUserCollapsed] = useState(false);
  const [narrowExpanded, setNarrowExpanded] = useState(false);
  const [narrow, setNarrow] = useState(() => typeof window !== 'undefined' && window.innerWidth < 1000);
  // Below 1000 the sidebar auto-collapses to the icon rail; a manual toggle
  // there re-expands it over the squeezed centre. Policy mirrors dsh's
  // ui-layout/columns.ts (SIDEBAR_AUTO_COLLAPSE), but with a 60px hysteresis
  // band: flipping on a single threshold made the centre jump 224px every time
  // a drag wobbled across it.
  useEffect(() => {
    const onResize = () => {
      const w = window.innerWidth;
      setNarrow(prev => (prev ? w < 1060 : w < 1000));
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  const sidebarCollapsed = narrow ? !narrowExpanded : userCollapsed;
  const [hydrated, setHydrated] = useState(false);
  const [sidecarState, setSidecarState] = useState<string>('starting');
  const [lastError, setLastError] = useState<string | null>(null);
  // Contract §7.5: context.compacted banners, keyed by session id.
  const [compactionNotices, setCompactionNotices] = useState<Record<string, ContextCompactionNotice>>({});
  // 运行断点：断网/断电/关程序留下的可续任务（点「继续」才接着跑）。
  const [pendingRun, setPendingRun] = useState<RunInfo | null>(null);
  // Real usage.promptTokens from the last chat.done of the ACTIVE session.
  // Ref-mirrored so the event handler can clear it on session switch.
  const [lastPromptTokens, setLastPromptTokens] = useState<number | null>(null);
  // Reasoning display choice (user-controlled, survives reload).
  const [thinkingDisplay, setThinkingDisplayState] =
    useState<ThinkingDisplay>(readThinkingDisplay);
  const setThinkingDisplay = useCallback((v: ThinkingDisplay) => {
    setThinkingDisplayState(v);
    writeThinkingDisplay(v);
  }, []);

  /* Whole-application appearance settings. Applied to documentElement so every
     stylesheet that reads a token follows immediately. */
  const [appSettings, setAppSettingsState] = useState<AppSettings>(() => loadAppSettings());
  const prefersDark = useCallback(
    () => (typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-color-scheme: dark)').matches
      : true),
    []
  );
  const setAppSettings = useCallback((patch: Partial<AppSettings>) => {
    setAppSettingsState(prev => {
      const next = parseAppSettings({ ...prev, ...patch });
      saveAppSettings(next);
      applyWallpaper(next.wallpaper, document.documentElement);
      applyAppSettings(next, document.documentElement, prefersDark());
      return next;
    });
  }, [prefersDark]);

  /* 'system' theme keeps following the OS while the app stays open. */
  useEffect(() => {
    if (appSettings.theme !== 'system' || typeof window.matchMedia !== 'function') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const on = () => applyAppSettings(appSettings, document.documentElement, mq.matches);
    applyWallpaper(appSettings.wallpaper, document.documentElement);
    on();
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [appSettings]);
  const promptTokensBySessionRef = useRef<Map<string, number>>(new Map());

  // Active chat id + which session each chat belongs to (events echo chatId).
  const chatIdRef = useRef<string | null>(null);
  const sessionByChatRef = useRef<Map<string, string>>(new Map());

  // Track pending debounced writes per session so we don't lose the final flush.
  const persistTimers = useRef<Map<string, number>>(new Map());
  // Refs mirror latest state so async IO callbacks see fresh values.
  const sessionsRef = useRef(sessions);
  const messagesRef = useRef(messages);
  const activeIdRef = useRef(activeSessionId);
  useEffect(() => { sessionsRef.current = sessions; }, [sessions]);
  useEffect(() => { messagesRef.current = messages; }, [messages]);
  useEffect(() => { activeIdRef.current = activeSessionId; }, [activeSessionId]);

  // hub（Ctrl+Alt+H）默认接着当前这条对话提问：把这个 id 交给主进程，
  // 于是上下文、模型、所有设置都和主窗一致。
  useEffect(() => {
    if (!activeSessionId) return;
    try {
      getAgent()?.setActiveSession?.(activeSessionId);
    } catch {
      /* 老 preload 没有这方法，忽略 */
    }
  }, [activeSessionId]);

  // hub 里问完的那轮落在同一条会话里，主进程会喊我们重取一次；点
  // 「打开完整对话」也走这里。不重取的话切回来看着像对话没进去。
  useEffect(() => {
    const refresh = (id: string): void => {
      if (!id) return;
      setActiveSessionId(id);
      const repo = repoRef.current;
      if (!repo) return;
      void (async () => {
        try {
          const list = await repo.loadMessages(id);
          setMessages(prev => ({ ...prev, [id]: list }));
        } catch (err) {
          console.warn('[store] hub refresh failed, will re-fetch on open', err);
        }
      })();
    };
    const agent = getAgent();
    const offOpen = agent?.onHubOpenSession?.(refresh);
    const offRefresh = agent?.onSessionRefresh?.(refresh);
    return () => {
      offOpen?.();
      offRefresh?.();
    };
  }, []);

  // Hydrate from IndexedDB on mount. StrictMode-safe: guarded by a single
  // module-level flag so the seed only runs once.
  const hydratedRef = useRef(false);
  useEffect(() => {
    if (hydratedRef.current) return;
    hydratedRef.current = true;

    // Dev-only debug hook: expose the store on window so E2E tests can drive
    // actions that the parallel UI agent hasn't built buttons for yet.
    // We use typeof import.meta to detect Vite's dev mode without depending
    // on the `vite/client` types (which are not currently in tsconfig).
    const isDev =
      typeof import.meta !== 'undefined' &&
      (import.meta as { env?: { DEV?: boolean } }).env?.DEV === true;
    if (isDev && typeof window !== 'undefined') {
      (window as unknown as { __workbuddyStore?: unknown }).__workbuddyStore = {
        getState: () => ({
          view, sessions, activeSessionId, messages, models, activeModelId,
          rules, usage: usageSummary, pendingApprovalId, isStreaming, sidebarCollapsed, hydrated
        }),
        actions: {
          renameSession, deleteSession, togglePin, exportSession,
          selectSession, createSession, send
        }
      };
    }

    const repo = repoRef.current!;
    (async () => {
      try {
        await repo.open();
        const seeded = await repo.isSeeded();
        if (!seeded) {
          // 真 store 空着开场是有意的：原型那批演示数据不属于用户的历史。
          await repo.markSeeded();
        }
        // A cold app start can reach the bridge before the sidecar is
        // listening. One retry stops that race from dropping the whole store
        // back to the demo rows.
        let stored: PersistedConversation[] = [];
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            stored = await repo.listConversations();
            break;
          } catch (err) {
            if (attempt === 2) throw err;
            console.warn('[store] session list attempt ' + (attempt + 1) + ' failed', err);
            await new Promise(r => setTimeout(r, 900));
          }
        }
        if (stored.length) {
          const nextSessions: Session[] = [];
          const nextMessages: Record<string, Message[]> = {};
          for (const row of stored) {
            const { messages: msgs, ...rest } = row;
            nextSessions.push({
              id: rest.id,
              title: rest.title,
              createdAt: rest.createdAt,
              updatedAt: rest.updatedAt,
              modelId: rest.modelId,
              pinned: rest.pinned,
              unread: rest.unread,
              workspace: rest.workspace
            });
            nextMessages[row.id] = msgs ?? [];
          }
          const storedActive = await repo.getActive();
          const openId = storedActive && nextSessions.some(s => s.id === storedActive)
            ? storedActive
            : (nextSessions[0]?.id ?? '');
          if (openId) {
            // Transcripts are fetched per session: only the one being opened
            // has to be in memory at startup. A failure here is recoverable
            // (opening the session fetches again), so it must not abort the
            // hydration and leave the list empty.
            try {
              nextMessages[openId] = await repo.loadMessages(openId);
            } catch (err) {
              console.warn('[store] transcript fetch failed, will retry on open', err);
              nextMessages[openId] = [];
            }
          }
          setSessions(sortSessions(nextSessions));
          setMessages(nextMessages);
          if (openId) setActiveSessionId(openId);
        } else if (hasSidecarStore()) {
          // Store exists but holds nothing yet: open a real first conversation
          // instead of leaving the mock rows on screen.
          const nowMs = Date.now();
          const id = `s-${nowMs}`;
          const fresh: Session = {
            id,
            title: '新会话',
            createdAt: nowMs,
            updatedAt: nowMs,
            modelId: defaultModelIdRef.current || ''
          };
          // Same cold-start race as the read path, so the first write retries too.
          for (let attempt = 0; attempt < 3; attempt++) {
            try {
              await repo.saveConversation(toPersisted(fresh, []));
              break;
            } catch (err) {
              if (attempt === 2) throw err;
              await new Promise(r => setTimeout(r, 900));
            }
          }
          await repo.setActive(id);
          setSessions([fresh]);
          setMessages({ [id]: [] });
          setActiveSessionId(id);
        }
      } catch (err) {
        console.warn('[store] hydration failed', err);
      } finally {
        setHydrated(true);
        // 首屏拉一次真实用量：底部栏和用量页都靠它，取不到就显示占位。
        void refreshUsage(null);
      }
    })();

    // Flush any debounced writes on tab close.
    const onBeforeUnload = () => {
      const map = persistTimers.current;
      for (const [id, t] of map) {
        clearTimeout(t);
        flushPersist(id);
      }
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Live sidecar wiring ───────────────────────────────────────────────
  /** Mutate the currently-streaming assistant message of `sid`, if any. */
  function patchStreaming(sid: string, fn: (m: Message) => Message): void {
    setMessages(prev => {
      const list = prev[sid] || [];
      const idx = lastStreamingIdx(list);
      if (idx === -1) return prev;
      const next = [...list];
      next[idx] = fn(next[idx]);
      return { ...prev, [sid]: next };
    });
  }

  function endStreaming(sid: string): void {
    setMessages(prev => {
      const list = prev[sid] || [];
      const idx = lastStreamingIdx(list);
      if (idx === -1) return prev;
      const next = [...list];
      next[idx] = { ...next[idx], streaming: false };
      return { ...prev, [sid]: next };
    });
    setIsStreaming(false);
  }

  function appendNotice(sid: string, text: string): void {
    const now = Date.now();
    setMessages(prev => ({
      ...prev,
      [sid]: [...(prev[sid] || []), { id: `n-${now}`, role: 'assistant', content: text, createdAt: now }]
    }));
  }

  // 子代理状态条：批次跑完自动收掉（用户口径 2026-09-26：「完成了就关掉,不用一直展示」）。
  // 留 3 秒给人看到「已完成」，然后清空；期间只要还有事件进来 timer 就被重排，
  // 所以不会在真正跑完前提前消失。
  useEffect(() => {
    if (!subagentBatch || subagentBatch.nodes.length === 0) return;
    const stillRunning = subagentBatch.nodes.some(n => n.status === 'running');
    if (stillRunning) return;
    const timer = window.setTimeout(() => setSubagentBatch(null), 3000);
    return () => window.clearTimeout(timer);
  }, [subagentBatch]);

  // Subscribe once: sidecar events carry the whole chat lifecycle.
  useEffect(() => {
    const agent = getAgent();
    if (!agent) return;
    const offEvent = agent.onEvent(ev => {
      const data = ev.data || {};
      const sid = (ev.chatId && sessionByChatRef.current.get(ev.chatId)) || activeIdRef.current;
      switch (ev.event) {
        // ── 子代理：引擎按节点发 start/delta/tool/done|error，批次收尾发 batch_done ──
        // 这些事件不属于某条消息，只喂给输入区上方那条状态条（用户选的 B 方案）。
        case 'subagent.start': {
          const nodeId = String(data.node_id ?? '');
          const batchId = String(data.batch_id ?? '');
          if (!nodeId) break;
          setSubagentBatch(prev => {
            const base: SubagentBatch = prev && prev.batchId === batchId
              ? prev
              : { batchId, nodes: [], status: null };
            const node: SubagentNode = {
              nodeId,
              goal: String(data.goal ?? ''),
              status: 'running'
            };
            const nodes = base.nodes.some(n => n.nodeId === nodeId)
              ? base.nodes.map(n => (n.nodeId === nodeId ? { ...n, ...node } : n))
              : [...base.nodes, node];
            return { ...base, nodes, status: null };
          });
          break;
        }
        case 'subagent.delta': {
          const nodeId = String(data.node_id ?? '');
          const text = String(data.text ?? '');
          setSubagentBatch(prev => !prev ? prev : ({
            ...prev,
            nodes: prev.nodes.map(n => n.nodeId === nodeId ? { ...n, lastText: text } : n)
          }));
          break;
        }
        case 'subagent.tool': {
          const nodeId = String(data.node_id ?? '');
          const name = String(data.name ?? '');
          setSubagentBatch(prev => !prev ? prev : ({
            ...prev,
            nodes: prev.nodes.map(n => n.nodeId === nodeId ? { ...n, tool: name || n.tool } : n)
          }));
          break;
        }
        case 'subagent.done':
        case 'subagent.error': {
          const nodeId = String(data.node_id ?? '');
          const failed = ev.event === 'subagent.error';
          const status = (failed ? 'failed' : String(data.status ?? 'done')) as SubagentNodeStatus;
          const elapsed = typeof data.elapsed_s === 'number' ? data.elapsed_s as number : undefined;
          setSubagentBatch(prev => !prev ? prev : ({
            ...prev,
            nodes: prev.nodes.map(n => n.nodeId === nodeId
              ? {
                  ...n,
                  status,
                  elapsedS: elapsed ?? n.elapsedS,
                  tool: undefined,
                  lastText: failed ? String(data.error ?? n.lastText ?? '') : n.lastText
                }
              : n)
          }));
          break;
        }
        case 'subagent.batch_done': {
          const peak = typeof data.rss_peak === 'number' ? data.rss_peak as number : null;
          setSubagentBatch(prev => !prev ? prev : ({
            ...prev,
            status: String(data.status ?? 'done') as SubagentNodeStatus,
            rssPeakMb: peak !== null ? Math.round(peak / 1048576 * 10) / 10 : prev.rssPeakMb
          }));
          break;
        }
        case 'chat.delta':
          patchStreaming(sid, m => ({ ...m, content: (m.content || '') + String(data.text ?? '') }));
          break;
        case 'tool.call': {
          const tc: ToolCall = {
            id: String(data.id),
            name: String(data.name),
            summary: summarizeArguments(String(data.name), data.arguments),
            level: String(data.level ?? 'L1') as ToolCall['level'],
            args: (data.arguments as Record<string, unknown>) ?? {},
            status: data.requiresApproval ? 'awaiting' : 'running',
            startedAt: Date.now()
          };
          patchStreaming(sid, m => ({ ...m, toolCalls: [...(m.toolCalls || []), tc] }));
          break;
        }
        case 'tool.result':
          patchStreaming(sid, m => ({
            ...m,
            toolCalls: (m.toolCalls || []).map(tc => tc.id === String(data.id)
              ? {
                  ...tc,
                  status: data.ok
                    ? 'success'
                    : (data.decision === 'deny' ? 'rejected' : 'failed'),
                  result: typeof data.content === 'string' && data.content
                    ? data.content
                    : (data.errorMessage ? String(data.errorMessage) : tc.result),
                  endedAt: Date.now()
                }
              : tc)
          }));
          break;
        case 'approval.request': {
          // 会话内审批（设计 §3）：不再驱动全局弹窗，折成当前助手回合
          // Message.approvals 里的一条，由 ApprovalCard 渲染。level
          // 照后端事件读进来（不报错），界面不显示。
          const req: ApprovalRequest = {
            id: String(data.approvalId),
            tool: String(data.name),
            level: String(data.level ?? 'L1') as ApprovalRequest['level'],
            title: '需要确认',
            toolLabel: TOOL_LABELS[data.name as string] ?? '执行命令',
            detailHint: '展开详情',
            payload: formatArguments(data.arguments),
            reason: String(data.reason ?? ''),
            canAlwaysAllow: data.canAlwaysAllow === true,
            decision: null,
            createdAt: Date.now()
          };
          setPendingApprovalId(req.id);
          patchStreaming(sid, m => ({ ...m, approvals: [...(m.approvals || []), req] }));
          persistSoon(sid);
          break;
        }
        case 'chat.reasoning':
          // Hidden reasoning channel: without this the first tokens look like a
          // freeze (M3 spends seconds thinking before any visible text).
          patchStreaming(sid, m => ({ ...m, thinking: (m.thinking || '') + String(data.text ?? '') }));
          break;
        case 'chat.artifacts':
          // 本轮产出的文件（后端在落库后单独推一次）
          patchStreaming(sid, m => ({ ...m, artifacts: (data.artifacts as Message['artifacts']) ?? [] }));
          break;
        case 'chat.done': {
          const u = (data.usage ?? {}) as Record<string, number>;
          const total = u.total_tokens ?? u.totalTokens
            ?? ((u.prompt_tokens ?? 0) + (u.completion_tokens ?? 0));
          const prompt = u.prompt_tokens ?? u.promptTokens;
          // Track the real prompt size per session: the StatusBar context meter
          // prefers this (contract §7.1: display uses server-reported usage).
          if (typeof prompt === 'number' && prompt >= 0) {
            promptTokensBySessionRef.current.set(sid, prompt);
            setLastPromptTokens(prompt);
          }
          if (total) {
            patchStreaming(sid, m => ({
              ...m,
              usage: {
                totalTokens: total,
                promptTokens: prompt,
                completionTokens: u.completion_tokens ?? u.completionTokens
              }
            }));
          }
          endStreaming(sid);
          // 这一轮收尾了：断点条按后端的最新状态显示。
          void refreshPendingRun(sid);
          persistSoon(sid, 0);
          // 这一轮已经落库：刷新真实用量（底部栏 / 用量页同步更新）。
          void refreshUsage(sid);
          break;
        }
        case 'context.compacted': {
          // Non-blocking banner (§7.5). The event carries estimatedTokens /
          // tokensAfter / llmCalls / summaryNote; we only surface one line.
          const before = toNonNegative(data.estimatedTokens);
          const after = toNonNegative(data.tokensAfter);
          const notice: ContextCompactionNotice = {
            id: String(ev.chatId ?? data.id ?? ''),
            level: String(data.level ?? ''),
            savedTokens: before !== null && after !== null ? Math.max(0, before - after) : null,
            llmCalls: Number(data.llmCalls ?? 0) || 0,
            summaryNote: String(data.summaryNote ?? ''),
            receivedAt: Date.now()
          };
          setCompactionNotices(prev => ({ ...prev, [sid]: notice }));
          break;
        }
        case 'chat.notice': {
          // 控制条上的覆盖项取值不认识时，后端如实回报一句，不静默失效
          if (data.text) appendNotice(sid, String(data.text));
          // 断网自动退避重试：会把这一轮重放一遍，先把已经流出来的半截文本清掉，
          // 否则用户看到的是「重试前的半截 + 重试后的全文」两段叠在一起。
          if (data.code === 'NET_RETRY') {
            const list = messagesRef.current[sid] || [];
            const last = list[list.length - 1];
            if (last && last.role === 'assistant' && last.streaming) {
              setMessages(prev => {
                const rows = prev[sid] || [];
                return {
                  ...prev,
                  [sid]: rows.map(m => (m.id === last.id ? { ...m, content: '', thinking: '' } : m))
                };
              });
            }
          }
          break;
        }

        case 'chat.error':
          setLastError(`${data.code ?? 'ERROR'}: ${data.message ?? ''}`);
          appendNotice(sid, `[错误] ${data.code ?? 'ERROR'}: ${data.message ?? ''}`);
          endStreaming(sid);
          // 网络拖到放弃那种错误，后端已经把它存成可续断点了：拉一次让「继续」条出现。
          void refreshPendingRun(sid);
          break;
        default:
          break;
      }
    });
    const offStatus = agent.onStatus(s => setSidecarState(s.state));
    void agent.getStatus().then(s => setSidecarState(s.state)).catch(() => undefined);
    // Workspace root lets us resolve relative image refs the agent writes.
    // The sidecar may still be booting at mount, so retry instead of giving up
    // on the first failure — otherwise every relative path stays unresolvable.
    let tries = 0;
    let timer: number | undefined;
    const fetchRoot = (): void => {
      if (!agent.info) return;
      void agent.info()
        .then(i => {
          const root = (i as Record<string, unknown>)?.workspaceRoot;
          if (typeof root === 'string' && root) setWorkspaceRoot(root);
          else if (++tries < 30) timer = window.setTimeout(fetchRoot, 2000);
        })
        .catch(() => {
          if (++tries < 30) timer = window.setTimeout(fetchRoot, 2000);
        });
    };
    fetchRoot();
    // Same story for the model registry: config.json lives in the sidecar, so
    // the list is empty until it answers.
    let modelTries = 0;
    let modelTimer: number | undefined;
    const fetchModelRegistry = (): void => {
      void refreshModels().then(ok => {
        if (!ok) {
          if (++modelTries < 30) modelTimer = window.setTimeout(fetchModelRegistry, 2000);
          return;
        }
        void fetchModelPresets().then(setModelPresets).catch(() => undefined);
      });
    };
    fetchModelRegistry();
    let presetsTries = 0;
    const offInfoStatus = agent.onStatus(s => {
      if (s.state === 'ready') {
        tries = 0;
        fetchRoot();
        refreshModels().then(ok => {
          if (ok) void fetchModelPresets().then(setModelPresets).catch(() => undefined);
          else if (++presetsTries < 10) modelTimer = window.setTimeout(fetchModelRegistry, 2000);
        });
      }
    });
    return () => {
      offEvent(); offStatus(); offInfoStatus();
      if (timer) window.clearTimeout(timer);
      if (modelTimer) window.clearTimeout(modelTimer);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Persistence helpers ───────────────────────────────────────────────
  function flushPersist(id: string): void {
    const s = sessionsRef.current.find(x => x.id === id);
    if (!s) return;
    const msgs = messagesRef.current[id] ?? [];
    void repoRef.current!.saveConversation(toPersisted(s, msgs));
  }

  function persistSoon(id: string, delay = 400): void {
    const map = persistTimers.current;
    const prev = map.get(id);
    if (prev) clearTimeout(prev);
    const handle = window.setTimeout(() => {
      map.delete(id);
      flushPersist(id);
    }, delay);
    map.set(id, handle);
  }

  function persistActiveSoon(): void {
    persistSoon(activeIdRef.current);
  }

  function persistImmediate(id: string): void {
    const map = persistTimers.current;
    const prev = map.get(id);
    if (prev) {
      clearTimeout(prev);
      map.delete(id);
    }
    flushPersist(id);
  }

  // ── Actions ───────────────────────────────────────────────────────────

  const selectSession = useCallback((id: string) => {
    setActiveSessionId(id);
    // 本会话的 token/花费属于这个会话：切过去就刷新，别让底部栏挂着上一个会话的数。
    void refreshUsage(id);
    // 这个会话有没有没跑完的任务（断点条据此显示）。
    void refreshPendingRun(id);
    // Switching conversations re-points the model picker: the session's own
    // model if it still exists, otherwise the global default.
    syncModelForSession(id);
    setView('chat');
    // Re-point the real-usage context number to the newly selected session.
    setLastPromptTokens(promptTokensBySessionRef.current.get(id) ?? null);
    void repoRef.current?.setActive(id);
    // The transcript may live only in the backend (sidecar sessions.db). Pull
    // it if this session has nothing in memory yet; a session that is truly
    // empty renders ChatArea's empty state either way.
    const repo = repoRef.current;
    if (repo && !(messagesRef.current[id]?.length)) {
      void repo.loadMessages(id)
        .then(list => {
          if (!list.length) return;
          setMessages(prev => (prev[id]?.length ? prev : { ...prev, [id]: list }));
        })
        .catch(err => console.warn('[store] loadMessages failed', err));
    }
  }, []);

  const createSession = useCallback(() => {
    const nowMs = Date.now();
    const id = `s-${nowMs}`;
    const modelId = activeModelId;
    const newSession: Session = {
      id,
      title: '新会话',
      createdAt: nowMs,
      updatedAt: nowMs,
      modelId
    };
    setSessions(prev => sortSessions([newSession, ...prev]));
    setMessages(prev => ({ ...prev, [id]: [] }));
    setActiveSessionId(id);
    setView('chat');
    // A fresh session must not inherit a pending approval from the old one:
    // the pending tool call belongs to the previous conversation.
    setPendingApprovalId(null);
    // Persist immediately so a refresh before any message still shows it.
    void repoRef.current?.saveConversation(toPersisted(newSession, []));
    void repoRef.current?.setActive(id);
  }, [activeModelId]);

  const renameSession = useCallback((id: string, title: string) => {
    const clean = title.trim();
    if (!clean) return;
    setSessions(prev =>
      prev.map(s => (s.id === id ? { ...s, title: clean } : s))
    );
    persistImmediate(id);
  }, []);

  const deleteSession = useCallback((id: string) => {
    setSessions(prev => {
      const next = prev.filter(s => s.id !== id);
      // Fallback if we deleted the active session.
      setActiveSessionId(curr => {
        if (curr !== id) return curr;
        const fallback = pickFallback(next, id);
        return fallback ?? next[0]?.id ?? '';
      });
      return next;
    });
    setMessages(prev => {
      const { [id]: _removed, ...rest } = prev;
      void _removed;
      return rest;
    });
    void repoRef.current?.deleteConversation(id);
  }, []);

  const togglePin = useCallback((id: string) => {
    setSessions(prev =>
      prev.map(s => (s.id === id ? { ...s, pinned: !s.pinned } : s))
    );
    persistImmediate(id);
  }, []);

  const exportSession = useCallback((id: string, format: ExportFormat) => {
    const s = sessionsRef.current.find(x => x.id === id);
    if (!s) return;
    const msgs = messagesRef.current[id] ?? [];
    exportConversation(s, msgs, format);
  }, []);

  const toggleSidebar = useCallback(() => {
    if (window.innerWidth < 1060) {
      setNarrowExpanded(v => !v);
    } else {
      setUserCollapsed(v => !v);
    }
  }, []);

  const dismissCompactionNotice = useCallback((sessionId: string) => {
    setCompactionNotices(prev => {
      if (!(sessionId in prev)) return prev;
      const { [sessionId]: _drop, ...rest } = prev;
      return rest;
    });
  }, []);

  /** 会话内审批卡：允许。设计已取消「总是允许」（后端 canAlwaysAllow 一律 false），
   *  所以这里恒发 allow_once —— 不做「一次点头永久放行」。 */
  const approveApproval = useCallback((id: string, _remember: boolean) => {
    setPendingApprovalId(curr => (curr === id ? null : curr));
    setMessages(prev => decideApprovalInMessages(prev, activeSessionId, id, 'allowed'));
    void getAgent()?.approval(id, 'allow_once').catch(() => undefined);
  }, [activeSessionId]);

  /** 会话内审批卡：拒绝。拒绝结果照旧回给模型让它改路线（不卡死、不死循环）。 */
  const denyApproval = useCallback((id: string) => {
    setPendingApprovalId(curr => (curr === id ? null : curr));
    setMessages(prev => decideApprovalInMessages(prev, activeSessionId, id, 'denied'));
    void getAgent()?.approval(id, 'deny').catch(() => undefined);
  }, [activeSessionId]);

  /** 拉一次「这个会话有没有可续断点」。桥没有这个方法（老 sidecar）就当没有。 */
  const refreshPendingRun = useCallback(async (sessionId?: string) => {
    const target = sessionId === undefined ? activeIdRef.current : sessionId;
    const agent = getAgent();
    if (!agent?.runs?.pending || !target) {
      setPendingRun(null);
      return;
    }
    try {
      const r = await agent.runs.pending(target);
      setPendingRun((r?.run as RunInfo) || null);
    } catch {
      setPendingRun(null);
    }
  }, []);

  // 会话一变（含启动时恢复上次会话）就查一次断点：别等用户点来点去才看见「继续」。
  useEffect(() => {
    if (!activeSessionId) {
      setPendingRun(null);
      return;
    }
    void refreshPendingRun(activeSessionId);
  }, [activeSessionId, refreshPendingRun]);

  /** 继续上次被中断的任务：没有新用户发言，后端用会话库里的历史重放。 */
  const resumeRun = useCallback(async () => {
    const run = pendingRun;
    const id = activeSessionId;
    const agent = getAgent();
    if (!run || !agent?.chat) return;
    const nowMs = Date.now();
    const history = (messagesRef.current[id] || [])
      .filter(m => (m.role === 'user' || m.role === 'assistant') && (m.content || '').length > 0)
      .map(m => ({ role: m.role as 'user' | 'assistant', content: m.content }));
    setPendingRun(null);
    setIsStreaming(true);
    setLastError(null);
    setMessages(prev => ({
      ...prev,
      [id]: [
        ...(prev[id] || []).map(m => (m.streaming ? { ...m, streaming: false } : m)),
        { id: `a-${nowMs}`, role: 'assistant', content: '', streaming: true, createdAt: nowMs } as Message
      ]
    }));
    try {
      const r = await agent.chat({
        sessionId: id,
        // 后端会用会话库里的真实历史重建，这里只是不让请求空着。
        messages: history.length ? history : [{ role: 'user', content: '继续上次的任务' }],
        useTools: true,
        resumeRunId: run.run_id,
        ...(activeModelIdRef.current ? { modelId: activeModelIdRef.current } : {})
      });
      chatIdRef.current = r.chatId;
      sessionByChatRef.current.set(r.chatId, id);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setLastError(msg);
      appendNotice(id, `[错误] 继续失败：${msg}`);
      endStreaming(id);
      void refreshPendingRun(id);
    }
  }, [pendingRun, activeSessionId, appendNotice, endStreaming]);

  const dismissPendingRun = useCallback(async () => {
    const run = pendingRun;
    setPendingRun(null);
    if (!run) return;
    try {
      await getAgent()?.runs?.dismiss(run.run_id);
    } catch {
      /* 忽略失败也不该影响界面 */
    }
  }, [pendingRun]);

  const send = useCallback((text: string, images?: string[]) => {
    const id = activeSessionId;
    const nowMs = Date.now();
    // 不再往正文里拼「[附带 N 个文件]」：附件在消息下方本来就有卡片，
    // 正文里多这一行只是噪音（2026-09-27 用户口径「这里的附带一个文件不要了」）。
    const withNote = text;
    const userMsg: Message = {
      id: `u-${nowMs}`,
      role: 'user',
      content: withNote,
      images: images && images.length ? [...images] : undefined,
      createdAt: nowMs
    };
    // Finalize anything still streaming, then append the new turn.
    setMessages(prev => ({
      ...prev,
      [id]: [
        ...(prev[id] || []).map(m => (m.streaming ? { ...m, streaming: false } : m)),
        userMsg
      ]
    }));
    // Auto-title: derive from first user message, bump updatedAt.
    setSessions(prev => {
      const next = prev.map(s => {
        if (s.id !== id) return s;
        const isUntitled = !s.title || s.title === '新会话';
        return {
          ...s,
          title: isUntitled ? deriveTitle(text) : s.title,
          updatedAt: nowMs
        };
      });
      return sortSessions(next);
    });
    setIsStreaming(true);
    setLastError(null);
    persistSoon(id);

    const agent = getAgent();
    if (!agent) {
      appendNotice(id, '[错误] 未连接本地 sidecar（window.agent 不存在）。请通过 Electron 启动应用。');
      endStreaming(id);
      return;
    }

    // History for the model: everything already persisted, plus this turn.
    const history = [
      ...(messagesRef.current[id] || [])
        .filter(m => m.role === 'user' || m.role === 'assistant')
        .filter(m => (m.content || '').length > 0)
        .map(m => ({ role: m.role as 'user' | 'assistant', content: m.content })),
      { role: 'user' as const, content: text }
    ];

    // The empty assistant bubble that sidecar events will fill in.
    setMessages(prev => ({
      ...prev,
      [id]: [...(prev[id] || []), {
        id: `a-${nowMs}`, role: 'assistant', content: '', streaming: true, createdAt: nowMs
      } as Message]
    }));

    void agent.chat({
      sessionId: id,
      messages: history,
      useTools: true,
      // Session-level model override; the sidecar falls back to its default.
      ...(activeModelIdRef.current ? { modelId: activeModelIdRef.current } : {}),
      // 输入区控制条：只在这一轮生效的覆盖。全空时不带这个键，
      // 老路径的请求参数逐字节不变。
      ...(Object.keys(((turnOverridesRef.current[id] || {})[activeModelIdRef.current]) || {}).length
        ? { overrides: { ...(((turnOverridesRef.current[id] || {})[activeModelIdRef.current]) || {}) } } : {}),
      // Paths only: the sidecar stores it, applies the vision policy and turns
      // it into a data URL on the last user message (python/sidecar.py).
      ...(images && images.length ? { images } : {})
    })
      .then(r => {
        chatIdRef.current = r.chatId;
        sessionByChatRef.current.set(r.chatId, id);
      })
      .catch(e => {
        const msg = e instanceof Error ? e.message : String(e);
        setLastError(msg);
        appendNotice(id, `[错误] 发送失败：${msg}`);
        endStreaming(id);
      });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId]);

  const stopStreaming = useCallback(() => {
    const agent = getAgent();
    const cid = chatIdRef.current;
    if (agent && cid) {
      void agent.cancel(cid).catch(() => undefined);
    }
    setIsStreaming(false);
    setMessages(prev => {
      const list = prev[activeSessionId] || [];
      const idx = lastStreamingIdx(list);
      if (idx === -1) return prev;
      const next = [...list];
      next[idx] = { ...next[idx], streaming: false };
      return { ...prev, [activeSessionId]: next };
    });
    persistActiveSoon();
  }, [activeSessionId]);

  const updateModel = useCallback((id: string, patch: Partial<ModelConfig>) => {
    setModels(prev => prev.map(m => m.id === id ? { ...m, ...patch } : m));
  }, []);

  const addModel = useCallback((m: ModelConfig) => setModels(prev => [...prev, m]), []);
  const deleteModel = useCallback((id: string) => setModels(prev => prev.filter(m => m.id !== id)), []);

  const addRule = useCallback((r: PermissionRule) => setRules(prev => [...prev, r]), []);
  const deleteRule = useCallback((id: string) => setRules(prev => prev.filter(r => r.id !== id)), []);

  const resolveTool = useCallback((toolId: string, status: ToolStatus, result?: string) => {
    setMessages(prev => {
      const list = prev[activeSessionId] || [];
      const next = list.map(m => {
        if (!m.toolCalls) return m;
        return {
          ...m,
          toolCalls: m.toolCalls.map((tc: ToolCall) =>
            tc.id === toolId
              ? { ...tc, status, endedAt: Date.now(), result: result ?? tc.result }
              : tc
          )
        };
      });
      return { ...prev, [activeSessionId]: next };
    });
    persistActiveSoon();
  }, [activeSessionId]);

  const state: State = {
    view, sessions, activeSessionId, messages, models, activeModelId,
    modelsReady, modelsError, modelPresets,
    rules, usage: usageSummary, refreshUsage,
    pendingApprovalId, isStreaming, sidebarCollapsed, hydrated,
    sidecarState, lastError, compactionNotices, lastPromptTokens, pendingRun,
    thinkingDisplay, appSettings,
    turnOverrides: (turnOverridesBySession[activeSessionId] || {})[activeModelId] || {},
    sessionOverrides: turnOverridesBySession[activeSessionId] || {},
    subagentBatch
  };

  const actions: Actions = {
    setView, selectSession, createSession, toggleSidebar,
    setActiveModel,
    approveApproval, denyApproval, send, stopStreaming,
    updateModel, addModel, deleteModel, addRule, deleteRule, resolveTool,
    renameSession, deleteSession, togglePin, exportSession,
    refreshModels, saveModel, removeModel, setDefaultModel,
    dismissCompactionNotice, setThinkingDisplay, setTurnOverride, setModelOverride,
    setAppSettings, resumeRun, dismissPendingRun
  };

  return <Ctx.Provider value={{ ...state, ...actions }}>{children}</Ctx.Provider>;
}

export function useApp() {
  const v = useContext(Ctx);
  if (!v) throw new Error('useApp must be used inside <AppProvider>');
  return v;
}
