/**
 * chatView — turns the sidecar's event stream into something the UI can render.
 *
 * Pure module on purpose: this box has no DISPLAY and no xvfb, so the Electron
 * window cannot be opened here. Everything with a decision in it lives in this
 * file and is unit-tested headless; `renderer.js` is only DOM glue.
 *
 * Two channels are kept apart on every message: `reasoning` (the model's
 * thinking) and `content` (what it actually says). Neither is ever dropped —
 * the UI renders them in separate panes.
 *
 * Every exported reducer is pure: it returns a new state and never mutates the
 * state or the event data it was handed.
 */

export type Role = "user" | "assistant";
export type Channel = "reasoning" | "content";

export interface ToolCallView {
  readonly id: string;
  readonly name: string;
  readonly arguments: Record<string, unknown>;
  readonly level: string;
  readonly requiresApproval: boolean;
  readonly canAlwaysAllow: boolean;
  readonly reason: string;
  readonly status: "pending" | "awaiting-approval" | "running" | "denied" | "ok" | "error";
  readonly decision?: string;
  readonly content?: string;
  readonly errorCode?: string | null;
  readonly errorMessage?: string | null;
  readonly durationMs?: number;
}

export interface ApprovalView {
  readonly approvalId: string;
  readonly toolCallId: string;
  readonly name: string;
  readonly arguments: Record<string, unknown>;
  readonly level: string;
  readonly reason: string;
  readonly canAlwaysAllow: boolean;
}

export interface MessageView {
  readonly role: Role;
  readonly reasoning: string;
  readonly content: string;
  readonly toolCalls: readonly ToolCallView[];
  readonly streaming: boolean;
  readonly finishReason?: string;
  readonly usage?: Record<string, unknown> | null;
  readonly error?: { code: string; message: string } | null;
}

export interface ChatViewState {
  readonly messages: readonly MessageView[];
  readonly pendingApproval: ApprovalView | null;
  readonly streaming: boolean;
  readonly lastError: { code: string; message: string } | null;
  readonly stats: { readonly rounds: number; readonly toolCallsRun: number };
}

/* ---------------------------------------------------------------- helpers */

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asBool(value: unknown, fallback = false): boolean {
  return typeof value === "boolean" ? value : fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function emptyAssistant(streaming: boolean): MessageView {
  return {
    role: "assistant",
    reasoning: "",
    content: "",
    toolCalls: [],
    streaming,
    usage: null,
    error: null,
  };
}

/** Index of the last assistant message, or -1. */
function lastAssistantIndex(messages: readonly MessageView[]): number {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "assistant") return i;
  }
  return -1;
}

/**
 * Replace the last assistant message using `patch`, appending an empty
 * assistant first if the stream outran the placeholder (out-of-order events
 * must not throw).
 */
function withLastAssistant(
  state: ChatViewState,
  patch: (msg: MessageView) => MessageView,
): ChatViewState {
  const messages = state.messages.slice();
  let idx = lastAssistantIndex(messages);
  if (idx === -1) {
    messages.push(emptyAssistant(state.streaming));
    idx = messages.length - 1;
  }
  messages[idx] = patch(messages[idx]);
  return { ...state, messages };
}

/* ------------------------------------------------------------------ init */

export function initialChatView(): ChatViewState {
  return {
    messages: [],
    pendingApproval: null,
    streaming: false,
    lastError: null,
    stats: { rounds: 0, toolCallsRun: 0 },
  };
}

/* -------------------------------------------------------------- reducers */

export function reduceUserMessage(
  state: ChatViewState,
  text: string,
  imagePaths?: readonly string[],
): ChatViewState {
  const images = (imagePaths ?? []).filter((p) => typeof p === "string" && p.length > 0);
  // 同上：正文不再拼附带说明（2026-09-27）。
  const content = text;
  void images; // 参数保留以兼容调用方，正文不再用它
  const messages: MessageView[] = [
    ...state.messages,
    {
      role: "user",
      reasoning: "",
      content,
      toolCalls: [],
      streaming: false,
      usage: null,
      error: null,
    },
    emptyAssistant(true),
  ];
  return { ...state, messages, streaming: true, lastError: null, pendingApproval: null };
}

export function clearApproval(state: ChatViewState): ChatViewState {
  if (state.pendingApproval === null) return state;
  return { ...state, pendingApproval: null };
}

export function lastAssistant(state: ChatViewState): MessageView | undefined {
  const idx = lastAssistantIndex(state.messages);
  return idx === -1 ? undefined : state.messages[idx];
}

export function reduceEvent(
  state: ChatViewState,
  event: string,
  data: Record<string, unknown>,
): ChatViewState {
  const payload = asRecord(data);
  switch (event) {
    case "chat.reasoning": {
      const text = asString(payload.text);
      if (!text) return state;
      return withLastAssistant(state, (m) => ({ ...m, reasoning: m.reasoning + text }));
    }

    case "chat.delta": {
      const text = asString(payload.text);
      if (!text) return state;
      return withLastAssistant(state, (m) => ({ ...m, content: m.content + text }));
    }

    case "tool.call": {
      const call: ToolCallView = {
        id: asString(payload.id),
        name: asString(payload.name),
        arguments: asRecord(payload.arguments),
        level: asString(payload.level),
        requiresApproval: asBool(payload.requiresApproval),
        canAlwaysAllow: asBool(payload.canAlwaysAllow),
        reason: asString(payload.reason),
        status: asBool(payload.requiresApproval) ? "awaiting-approval" : "running",
      };
      return withLastAssistant(state, (m) => ({ ...m, toolCalls: [...m.toolCalls, call] }));
    }

    case "approval.request": {
      const approval: ApprovalView = {
        approvalId: asString(payload.approvalId),
        toolCallId: asString(payload.toolCallId),
        name: asString(payload.name),
        arguments: asRecord(payload.arguments),
        level: asString(payload.level),
        reason: asString(payload.reason),
        canAlwaysAllow: asBool(payload.canAlwaysAllow),
      };
      return { ...state, pendingApproval: approval };
    }

    case "tool.result": {
      const id = asString(payload.id);
      const ok = asBool(payload.ok);
      const decision = asString(payload.decision);
      const status: ToolCallView["status"] = decision === "deny"
        ? "denied"
        : ok ? "ok" : "error";
      const next = withLastAssistant(state, (m) => ({
        ...m,
        toolCalls: m.toolCalls.map((tc) => (tc.id === id
          ? {
            ...tc,
            status,
            decision,
            content: asString(payload.content),
            errorCode: (payload.errorCode ?? null) as string | null,
            errorMessage: (payload.errorMessage ?? null) as string | null,
            durationMs: asNumber(payload.durationMs),
          }
          : tc)),
      }));
      // The approval this result belongs to is settled now.
      if (next.pendingApproval && (
        next.pendingApproval.toolCallId === id
        || next.pendingApproval.name === asString(payload.name)
      )) {
        return { ...next, pendingApproval: null };
      }
      return next;
    }

    case "chat.done": {
      const finishReason = asString(payload.finishReason, "stop");
      const usage = (payload.usage ?? null) as Record<string, unknown> | null;
      const next = withLastAssistant(state, (m) => ({
        ...m,
        streaming: false,
        finishReason,
        usage,
      }));
      return {
        ...next,
        streaming: false,
        pendingApproval: null,
        stats: {
          rounds: asNumber(payload.rounds),
          toolCallsRun: asNumber(payload.toolCallsRun),
        },
      };
    }

    case "chat.error": {
      const error = {
        code: asString(payload.code, "UNKNOWN"),
        message: asString(payload.message),
      };
      const next = withLastAssistant(state, (m) => ({ ...m, streaming: false, error }));
      return { ...next, streaming: false, lastError: error, pendingApproval: null };
    }

    default:
      // hello and anything else the UI does not render: pass through untouched.
      return state;
  }
}