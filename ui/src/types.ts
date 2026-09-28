export type SessionId = string;
export type Role = 'user' | 'assistant' | 'system';

export interface Session {
  id: SessionId;
  title: string;
  createdAt: number;
  updatedAt: number;
  modelId: string;
  pinned?: boolean;
  unread?: boolean;
  workspace?: string;
}

export type ToolStatus = 'running' | 'success' | 'failed' | 'rejected' | 'awaiting';

export interface ToolCall {
  id: string;
  name: string;
  /** Short label for the card summary row, e.g. "Bash · npm run build" */
  summary: string;
  /** 遗留风险等级字段：权限分级已下线，界面不再显示；后端事件里还带
   *  这个键，读了不报错，渲染层一律忽略。 */
  level: 'L0' | 'L1' | 'L2' | 'L3';
  status: ToolStatus;
  args: Record<string, unknown>;
  result?: string;
  startedAt: number;
  endedAt?: number;
}

/** Depth levels the app knows about; the vendor mapping decides which of
 *  them a given provider really supports (see models.presets). */
export type ThinkingDepth = 'low' | 'medium' | 'high';

/** How the reasoning channel is shown in chat — a user choice, not a hidden
 *  detail: 折叠（默认）/ 展开 / 隐藏（生成中仍显示"思考中"指示）. */
export type ThinkingDisplay = 'collapsed' | 'expanded' | 'hidden';

/**
 * 输入区控制条上的**会话级**覆盖：思考开关 / 思考深度 / 上下文长度。
 * 只作用于当前会话的每一次请求，不写回 config.json 里的模型配置。
 * 按「会话 × 模型」存：面板里给非当前模型设的值挂在那个模型头上（state/store.tsx）。
 * 字段缺省（undefined）= 跟随模型设置。
 */
export interface TurnOverrides {
  thinking?: boolean | null;
  thinkingDepth?: ThinkingDepth | null;
  contextWindow?: number | null;
}

/**
 * 助手一轮里产出的文件（2026-09-27）。只带元数据，内容仍在磁盘上；
 * 由后端 artifacts 收集（write_file/patch 成功 + 正文里的 [[file: path]]）。
 */
export interface MessageArtifact {
  path: string;
  name: string;
  size: number;
  /** image / doc / code / text / other —— 决定图标，只有 image 画缩略图 */
  kind: string;
}

export interface Message {
  id: string;
  role: Role;
  content: string;
  /** Local image paths attached to this message (rendered as thumbnails). */
  images?: string[];
  streaming?: boolean;
  /** Reasoning channel text (chat.reasoning). Rendered collapsed, not as the answer. */
  thinking?: string;
  /** 本轮产出的文件，渲染成对话里的附件卡片 */
  artifacts?: MessageArtifact[];
  /** Real token usage from chat.done (was a hardcoded "1.2k" before). */
  usage?: { totalTokens?: number; promptTokens?: number; completionTokens?: number };
  toolCalls?: ToolCall[];
  /** 本条助手回合里发生的审批请求（会话内卡片，不再走全局弹窗）。 */
  approvals?: ApprovalRequest[];
  createdAt: number;
}

/** ── 子代理进度（subagent.* 事件 → 输入区上方的状态条） ────────────── */

export type SubagentNodeStatus = 'running' | 'done' | 'failed' | 'interrupted';

/** 一个子代理节点。nodeId 由引擎给（node-1 / node-2 …）。 */
export interface SubagentNode {
  nodeId: string;
  goal: string;
  status: SubagentNodeStatus;
  /** 最近一次 subagent.tool 里的工具名——「这个节点正在干什么」。 */
  tool?: string;
  /** 该节点流式输出里最近的一段（只展示，不落库）。 */
  lastText?: string;
  elapsedS?: number;
}

/** 一次 delegate 批次。一批最多 3 个节点（引擎上限）。 */
export interface SubagentBatch {
  batchId: string;
  nodes: SubagentNode[];
  /** batch_done 的终态；null = 还在跑。 */
  status: SubagentNodeStatus | null;
  /** batch_done 带回来的峰值内存（MB），有就显示。 */
  rssPeakMb?: number;
}

export interface ModelConfig {
  id: string;
  name: string;
  baseUrl: string;
  /** Legacy mock field. The real registry carries no key value at all. */
  apiKey?: string;
  model: string;
  inputPrice: number;   // ¥ / 1k tokens
  outputPrice: number;  // ¥ / 1k tokens
  isDefault?: boolean;
  /** Provider family, used only to prefill the "add model" form. */
  provider?: string;
  /** Env var NAME holding the key. The value never reaches the renderer. */
  apiKeyEnv?: string;
  /** Whether that env var is set right now - the only key fact the UI gets. */
  hasKey?: boolean;
  maxTokens?: number;
  timeoutSeconds?: number;
  inputModalities?: string[];
  /* ── v2 generation params (contract §1.1, model-config-spec.md) ────── */
  /** Context window in tokens — drives the context mechanism (§7). Estimate, not exact. */
  contextWindow?: number;
  /** Tri-state thinking switch: null = send no thinking key at all, true/false = force on/off. */
  thinking?: boolean | null;
  /** Thinking depth (contract §6). null = send no depth key. Vendors without a
   *  depth parameter (MiniMax) keep this null and use the soft prompt hint. */
  thinkingDepth?: ThinkingDepth | null;
  /** 该厂商真有厂商级深度参数时的可选档位；空表 = 没有这个参数，界面只给软引导。 */
  thinkingDepthLevels?: ThinkingDepth[];
  /** true = 界面该说「靠提示词软引导」，别假装有硬旋钮。跟着 models.list 下来。 */
  softThinkingDepth?: boolean;
  /** 该厂商是否真吃思考开关（契约 §6 映射表里有它才为真）。false -> 面板整行不显示
      思考强度：hg 上的 glm-5.3 实测传 enable_thinking=False 直接 400。 */
  thinkingAdapted?: boolean;
  /** false = 厂商不允许关思考（不给「不思考」选项）。 */
  thinkingCanDisable?: boolean;
  /** 实际会发出去的输出预算（开思考时后端会抬到平台底线）；配置值不动。 */
  effectiveMaxTokens?: number | null;
  /** 抬高时的说明文案，null = 没抬高、无话可说。 */
  maxTokensNote?: string | null;
  /** Sampling temperature; null = the request key must not appear. */
  temperature?: number | null;
  /** Nucleus sampling; null = the request key must not appear. */
  topP?: number | null;
  /** True when this entry came from the sidecar (config.json) rather than mocks. */
  remote?: boolean;
}

/** Non-blocking banner fed by the sidecar's `context.compacted` event
 *  (contract §7.5). Ephemeral/in-memory only — never persisted as a message. */
export interface ContextCompactionNotice {
  /** chatRequestId the event belongs to. */
  id: string;
  /** L0 | L1 | L2. */
  level: string;
  /** estimatedTokens − tokensAfter when both were reported, else null. */
  savedTokens: number | null;
  llmCalls: number;
  summaryNote: string;
  receivedAt: number;
}

export interface PermissionRule {
  id: string;
  pattern: string;     // e.g. "Bash: rm *" / "Write: /etc/**"
  action: 'allow' | 'ask' | 'deny';
  description?: string;
}

export type PermissionLevel = 'L0' | 'L1' | 'L2' | 'L3';

/**
 * 会话内审批请求：approval.request 事件不再驱动全局弹窗，而是折成
 * Message.approvals 里的一条，由聊天流的 ApprovalCard 渲染。
 * level 字段照后端事件读进来（不报错），界面不显示。
 */
export interface ApprovalRequest {
  /** sidecar 的 approvalId，respond 时原样传回。 */
  id: string;
  /** 工具内部名（如 run_shell），卡片上只显示人话 toolLabel。 */
  tool: string;
  /** 遗留等级值，仅为兼容后端事件；界面不展示。 */
  level: PermissionLevel;
  /** 工具/命令的人话描述，卡片头那一行。 */
  title: string;
  /** 折叠头上的人话标签（如「执行命令」）。 */
  toolLabel: string;
  /** 折叠头右侧的提示（默认「展开详情」）。 */
  detailHint: string;
  /** 命令/参数原文，点「展开详情」才显示。 */
  payload: string;
  /** 被拦的原因（一行人话）。 */
  reason: string;
  /** true = 侧栏给「总是允许」按钮（按命令前缀记住）。 */
  canAlwaysAllow?: boolean;
  /** null = 待决；点击允许/拒绝后落为最终决定，卡片就地变成结果行。 */
  decision: 'allowed' | 'denied' | null;
  createdAt: number;
}

/* ── 技能与工具面板（window.agent.skills / toolSetEnabled 通道） ────── */

/** window.agent.tools() / toolSetEnabled() 返回的 tools 数组元素。
 *  enabled 为新增字段，是工具行启用开关的状态来源。 */
export interface ToolEntry {
  name: string;
  description: string;
  enabled: boolean;
}

/** tools 通道的载荷。面板只消费 tools 一项；levels / canAlwaysAllow 等
 *  字段后端会一并返回，此处不声明、不消费。 */
export interface ToolListPayload {
  tools?: ToolEntry[];
}

/** window.agent.skills() 返回的单个技能（面板内只读展示）。 */
export interface SkillInfo {
  name: string;
  description: string;
  whenToUse?: string | null;
  version?: string | null;
  author?: string | null;
  sourceDir: string;
  builtin: boolean;
}

/** skills 通道的载荷；skipped 为加载失败的 [技能名, 原因] 数组。 */
export interface SkillListPayload {
  skills?: SkillInfo[];
  skipped?: [string, string][];
}