/**
 * Bridge to the Electron preload API (`window.agent`).
 *
 * The prototype used to fake everything with mock data. This module is the
 * only place that knows about the real sidecar IPC surface, so the store can
 * stay declarative: `send()` calls `chat()`, sidecar events arrive on
 * `onEvent()` and get folded into the message list.
 *
 * Event contract (python/agent_loop.py):
 *   chat.delta        { text }
 *   chat.reasoning    { text }
 *   tool.call         { id, name, arguments, level, requiresApproval, canAlwaysAllow, reason }
 *   tool.result       { id, name, ok, content, errorCode?, errorMessage?, durationMs, decision }
 *   approval.request  { approvalId, toolCallId, name, arguments, level, reason, canAlwaysAllow }
 *   chat.done         { finishReason, usage, rounds, toolCallsRun, reasoningChars, textChars }
 *   chat.error        { id, code, message }
 */
import type { SkillListPayload, ToolListPayload } from '../types';

export interface SidecarStatus {
  state: string;
  backendVersion?: string;
  protocolVersion?: number;
  pid?: number;
  capabilities?: string[];
  lastError?: { code: string; message: string };
  restartAttempts: number;
  startedAt: number;
}

/**
 * 一条运行断点（python/run_checkpoint.py）。界面只关心：哪个会话、什么状态、
 * 什么时候断的、断在第几轮 —— 据此决定要不要显示「继续」。
 */
export interface RunInfo {
  run_id: string;
  session_id?: string;
  status?: string;
  kind?: string;
  rounds?: number;
  reason?: string;
  updated_at?: string;
}

export interface SidecarEvent {
  event: string;
  chatId?: string;
  data: Record<string, unknown>;
}

export interface AgentApi {
  onStatus(cb: (s: SidecarStatus) => void): () => void;
  getStatus(): Promise<SidecarStatus>;
  info?(): Promise<unknown>;
  tools(): Promise<ToolListPayload>;
  /** 技能与工具面板：sidecar 返回全部已加载技能（只读展示）。 */
  skills?(): Promise<SkillListPayload>;
  /** 技能与工具面板：切换某个工具的启用状态，返回刷新后的 tools 载荷。 */
  toolSetEnabled?(payload: { name: string; enabled: boolean }): Promise<ToolListPayload>;
  chat(params: Record<string, unknown>): Promise<{ chatId: string }>;
  cancel(chatId: string): Promise<unknown>;
  approval(approvalId: string, decision: string): Promise<unknown>;
  pickImages(): Promise<string[]>;
  /** Model registry. Optional so the UI still boots against an older sidecar. */
  models?(): Promise<unknown>;
  modelPresets?(): Promise<unknown>;
  /** Ask the vendor which models it serves (GET /models). */
  modelFetch?(payload: Record<string, unknown>): Promise<unknown>;
  modelUpsert?(payload: Record<string, unknown>): Promise<unknown>;
  modelRemove?(id: string): Promise<unknown>;
  modelSetDefault?(id: string): Promise<unknown>;
  /** 视觉线路（capabilities.vision）：读图走哪个模型。 */
  vision?(): Promise<unknown>;
  visionSet?(id: string): Promise<unknown>;
  /** 告诉主进程当前开着的会话 id —— hub（Ctrl+Alt+H）默认接这条走。 */
  setActiveSession?(sessionId: string): void;
  /** 主进程让渲染层切到某条会话（hub 的「打开完整对话」）。返回取消订阅。 */
  onHubOpenSession?(cb: (sessionId: string) => void): () => void;
  /** 某条会话在别处（hub）进了新消息：主窗按需重取，切回来就能看见。 */
  onSessionRefresh?(cb: (sessionId: string) => void): () => void;
  /** 用量聚合（usage_log）——底部信息栏和用量页共用这一个方法。 */
  usage?: { summary(params?: { days?: number; sessionId?: string }): Promise<unknown> };
  /** Real filesystem path of a dropped/picked File (Electron dropped File.path in v32+). */
  pathForFile?(file: File): string;
  /** 剪贴板截图没有路径：把位图交给主进程落成临时文件，返回真实路径。 */
  saveTempImage?(payload: { dataUrl: string; name?: string }): Promise<string>;
  /** 应用自带屏幕截图：打开全屏遮罩框选（截图按钮 / Ctrl+Shift+S）。 */
  snipStart?(): Promise<boolean>;
  /** 截图完成后拿到临时文件路径；返回取消订阅的函数。 */
  onSnipAttached?(cb: (path: string) => void): () => void;
  /** 运行断点：接着跑上一次被断网/断电/关程序打断的任务。 */
  runs?: {
    pending(sessionId?: string): Promise<{ run: RunInfo | null }>;
    active(): Promise<{ runs: RunInfo[] }>;
    interrupt(runId: string, reason?: string): Promise<unknown>;
    dismiss(runId: string): Promise<unknown>;
  };
  /** 关程序但还有任务在跑：主进程问用户怎么办（返回值取消订阅）。 */
  onCloseConfirm?(cb: (payload: { runs: RunInfo[] }) => void): () => void;
  /** 回答：'wait' 等它做完 | 'interrupt' 中断并保存断点退出 | 'cancel' 取消。 */
  closeChoice?(choice: 'wait' | 'interrupt' | 'cancel'): void;
  onEvent(cb: (ev: SidecarEvent) => void): () => void;
}

/** The preload bridge, or null when running in a plain browser (vite dev). */
export function getAgent(): AgentApi | null {
  if (typeof window === 'undefined') return null;
  const w = window as unknown as { agent?: AgentApi };
  return w.agent ?? null;
}

// ── Model registry (config.json lives in the sidecar) ──────────────────────
// Contract: docs/model-config-spec.md. Key values never cross this boundary;
// the only key-ish fact here is `hasKey`.
export interface RemoteModel {
  id: string;
  label: string;
  provider?: string;
  baseUrl: string;
  model: string;
  apiKeyEnv?: string;
  hasKey?: boolean;
  maxTokens?: number;
  timeoutSeconds?: number;
  inputModalities?: string[];
  /* v2 generation params (contract §1.1/§2). The sidecar fills defaults for
   * old configs (contextWindow=128000, others null) before we see them. */
  contextWindow?: number;
  thinking?: boolean | null;
  /** 'low' | 'medium' | 'high' | null (contract §6 depth column). */
  thinkingDepth?: string | null;
  /** 该厂商真有厂商级深度参数时的档位；空表 = 没这个参数（走提示词软引导）。 */
  thinkingDepthLevels?: string[];
  /** true = 界面该说「这个模型只有软引导」；models.list 带下来。 */
  softThinkingDepth?: boolean;
  /** 该模型/线路是否真吃思考参数（models.list 的 thinkingAdapted）。false = 一个
      思考键都发不出去（provider 表未适配且目录里也没取到），面板那一行写静态说明。 */
  thinkingAdapted?: boolean;
  /** 这条模型能不能关思考（models.list 的 thinkingCanDisable）。glm-5.3 这类
   *  厂商侧「始终思考」的模型为 false：弹窗里不给「不思考」这一项。 */
  thinkingCanDisable?: boolean;
  /** 实际会发出去的输出预算 + 抬高说明（配置值不改）。 */
  effectiveMaxTokens?: number | null;
  maxTokensNote?: string | null;
  temperature?: number | null;
  topP?: number | null;
}

export interface RemoteModelList {
  defaultId: string;
  activeId?: string;
  items: RemoteModel[];
}

/** 预置模型目录里的一条（python model_catalog.json 的一条）。
 *  窗口 / 价格 / 擅长都在这儿——「模型详情」面板靠它填，查不到就不显示。 */
export interface CatalogEntry {
  provider?: string;
  model: string;
  label?: string;
  contextWindow?: number | null;
  maxOutput?: number | null;
  strengths?: string[];
  pricing?: {
    mode?: string;
    inputPerM?: number | null;
    outputPerM?: number | null;
    currency?: string;
    plan?: string | null;
  } | null;
  unverified?: string[];
}

export interface RemoteModelPreset {
  key: string;
  label: string;
  baseUrl: string;
  /** 该厂商典型上下文窗口，选中厂商时带进表单，省得用户查文档。 */
  contextWindow?: number;
  keyEnvHint?: string;
  needsKey?: boolean;
  notes?: string;
  /** From the §6 thinking map: false = provider is「未适配」(grey hint in the form). */
  thinkingSupported?: boolean;
  /** Vendor-level depth levels this provider really has ([] = none). */
  thinkingDepthLevels?: string[];
  /** True = no vendor depth parameter; the form offers the soft hint instead. */
  softThinkingDepth?: boolean;
  /** 该厂商是否真吃思考开关（未适配就不显示那一行）。 */
  thinkingAdapted?: boolean;
  /** false = 该模型/线路不允许关思考（不给「不思考」选项）。 */
  thinkingCanDisable?: boolean;
  /** 该对接方式下的预置模型目录（窗口 / 价格 / 擅长）。sidecar 一直在发，
   *  之前类型里没写，所以「模型详情」面板拿不到。 */
  catalog?: CatalogEntry[];
}

function requireModelBridge(): AgentApi {
  const agent = getAgent();
  if (!agent || !agent.models) {
    throw new Error('模型接口不可用：未连接本地 sidecar（请通过 Electron 启动应用）。');
  }
  return agent;
}

export async function fetchModels(): Promise<RemoteModelList> {
  const r = (await requireModelBridge().models!()) as RemoteModelList | null;
  return { defaultId: r?.defaultId ?? '', activeId: r?.activeId, items: r?.items ?? [] };
}

export async function fetchModelPresets(): Promise<RemoteModelPreset[]> {
  const agent = getAgent();
  if (!agent || !agent.modelPresets) return [];
  const r = (await agent.modelPresets()) as { presets?: RemoteModelPreset[] } | null;
  return r?.presets ?? [];
}

/** Vendor catalogue for the "pick a model" dropdown. */
export async function fetchRemoteModelIds(
  payload: Record<string, unknown>
): Promise<string[]> {
  return (await fetchRemoteModelsDetailed(payload)).ids;
}

/** 每个模型自己的数字（窗口等），厂商在同一次 /models 响应里就给了。
 *  查不到的模型这里没有记录——调用方据此回落到自己的阶梯，不许编数。 */
export interface RemoteModelMeta {
  contextLength?: number | null;
  maxCompletionTokens?: number | null;
}

/** 同一次请求，两样都要：模型名 + 每个模型的窗口。
 *  「添加模型」那步要拿窗口自动填表单，不能只用 id 列表（2026-09-27 用户报窗口被
 *  限成 128k 就是这里漏了）。 */
export async function fetchRemoteModelsDetailed(
  payload: Record<string, unknown>
): Promise<{ ids: string[]; meta: Record<string, RemoteModelMeta> }> {
  const bridge = requireModelBridge();
  if (!bridge.modelFetch) throw new Error('当前版本不支持获取模型列表');
  const r = (await bridge.modelFetch(payload)) as
    { models?: string[]; meta?: Record<string, RemoteModelMeta> } | null;
  return { ids: r?.models ?? [], meta: r?.meta ?? {} };
}

/** Add or update. `apiKey` (optional) is written to the env file by the sidecar. */
export async function saveModelConfig(payload: Record<string, unknown>): Promise<string> {
  const r = (await requireModelBridge().modelUpsert!(payload)) as { id?: string } | null;
  return r?.id ?? String(payload.id ?? '');
}

export async function deleteModelConfig(id: string): Promise<void> {
  await requireModelBridge().modelRemove!(id);
}

export async function setDefaultModelConfig(id: string): Promise<void> {
  await requireModelBridge().modelSetDefault!(id);
}

/** 当前视觉线路（读图走哪个模型）。老 sidecar 没有这个方法就返回空。 */
export async function fetchVisionModel(): Promise<{ model?: string; provider?: string }> {
  const bridge = requireModelBridge();
  if (!bridge.vision) return {};
  const r = (await bridge.vision()) as
    { vision?: { model?: string; provider?: string } } | null;
  return r?.vision ?? {};
}

export async function saveVisionModel(id: string): Promise<void> {
  const bridge = requireModelBridge();
  if (!bridge.visionSet) throw new Error('当前版本不支持设置视觉模型');
  await bridge.visionSet(id);
}

/** One-line human summary of a tool call, used as the card's summary row. */
export function summarizeArguments(name: string, args: unknown): string {
  if (!args || typeof args !== 'object') return name;
  const a = args as Record<string, unknown>;
  const pick =
    (a.command as string) ??
    (a.path as string) ??
    (a.file_path as string) ??
    (a.query as string) ??
    (a.pattern as string);
  if (typeof pick === 'string' && pick) {
    const flat = pick.replace(/\s+/g, ' ').trim();
    return `${name} · ${flat.length > 60 ? flat.slice(0, 60) + '…' : flat}`;
  }
  return name;
}

/** Pretty JSON for the approval modal's payload block. */
export function formatArguments(args: unknown): string {
  try {
    return JSON.stringify(args ?? {}, null, 2);
  } catch {
    return String(args);
  }
}

/** file:// URL for an absolute local path — for previewing a picked image. */
export function fileUrl(p: string): string {
  const norm = p.replace(/\\/g, '/');
  return norm.startsWith('/') ? `file://${encodeURI(norm)}` : `file:///${encodeURI(norm)}`;
}

/**
 * Workspace root reported by the main process. Agents routinely name outputs
 * relative to the workspace ("gen-ok.png"), so the renderer needs it to turn
 * those refs into something displayable.
 */
let workspaceRoot = '';

export function setWorkspaceRoot(p: unknown): void {
  workspaceRoot = typeof p === 'string' ? p.replace(/\\/g, '/') : '';
}

export function getWorkspaceRoot(): string {
  return workspaceRoot;
}

/**
 * Displayable src for an image reference, or '' when it cannot be resolved
 * (better to render nothing than a broken <img>).
 * Accepts http(s), data:, file:, absolute paths, UNC, and workspace-relative.
 */
/**
 * Some models emit percent-encoded paths (E:%5Cdir%5Cchart.svg). Undo that
 * before matching or resolving, or the reference is silently unusable.
 */
export function unescapeRef(s: string): string {
  if (!/%[0-9a-f]{2}/i.test(s)) return s;
  return s
    .replace(/%5c/gi, '\\')
    .replace(/%2f/gi, '/')
    .replace(/%3a/gi, ':')
    .replace(/%20/g, ' ');
}

export function resolveImageSrc(ref: string): string {
  const s = unescapeRef(String(ref ?? '').trim()).replace(/^<|>$/g, '');
  if (!s) return '';
  if (/^(https?:|data:|file:)/i.test(s)) return s;
  if (/^[A-Za-z]:[\\/]/.test(s) || s.startsWith('\\\\') || s.startsWith('/')) return fileUrl(s);
  if (!workspaceRoot) return '';
  return fileUrl(`${workspaceRoot}/${s.replace(/^\.\//, '')}`);
}

/** Last path segment, for chip labels. */
export function baseName(p: string): string {
  const parts = p.replace(/\\/g, '/').split('/');
  return parts[parts.length - 1] || p;
}