/**
 * renderer.js — DOM glue only.
 *
 * Everything with a decision in it lives in `chatView.ts` (pure, unit-tested).
 * This file wires that state to the DOM and to `window.agent` (the preload
 * bridge). It is bundled by esbuild into `renderer.bundle.js` so it can load as
 * a classic script — ES modules don't load over file:// in Chromium.
 *
 * Rendering rule: model output goes in via `textContent`, never `innerHTML`.
 */
import {
  clearApproval,
  initialChatView,
  reduceEvent,
  reduceUserMessage,
  type ChatViewState,
  type MessageView,
  type ToolCallView,
} from "./chatView";

const SYSTEM_PROMPT =
  "你是一个桌面助手。用可用的工具完成任务，不要凭空猜文件内容；" +
  "工具报错时先看错误信息再决定下一步。回答简短。";

const el = (id: string): HTMLElement => {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing element #${id}`);
  return node;
};

const transcript = el("transcript");
const input = el("input") as HTMLTextAreaElement;
const sendBtn = el("send") as HTMLButtonElement;
const stopBtn = el("stop") as HTMLButtonElement;
const pickBtn = el("pick") as HTMLButtonElement;
const useTools = el("useTools") as HTMLInputElement;
const stateBadge = el("state");
const metaLine = el("meta");
const chips = el("chips");
const statusLine = el("statusLine");
const overlay = el("overlay");
const dialog = el("dialog");
const dlgTitle = el("dlgTitle");
const dlgSub = el("dlgSub");
const dlgWarn = el("dlgWarn");
const dlgLevel = el("dlgLevel");
const dlgReason = el("dlgReason");
const dlgArgs = el("dlgArgs");
const dlgOnce = el("dlgOnce") as HTMLButtonElement;
const dlgAlways = el("dlgAlways") as HTMLButtonElement;
const dlgDeny = el("dlgDeny") as HTMLButtonElement;

let state: ChatViewState = initialChatView();
let chatId: string | null = null;
let pickedImages: string[] = [];

/* --------------------------------------------------------------- wiring */

const api = (window as unknown as { agent: AgentBridge }).agent;

interface AgentBridge {
  onEvent(cb: (ev: { event: string; chatId?: string; data: Record<string, unknown> }) => void): () => void;
  onStatus(cb: (s: { state: string; backendVersion?: string; pid?: number }) => void): () => void;
  chat(params: Record<string, unknown>): Promise<{ chatId: string }>;
  cancel(chatId: string): Promise<unknown>;
  approval(approvalId: string, decision: string): Promise<unknown>;
  pickImages(): Promise<string[]>;
  getStatus(): Promise<{ state: string; backendVersion?: string; pid?: number }>;
}

/* ------------------------------------------------------------- render */

let frameQueued = false;
function renderSoon(): void {
  if (frameQueued) return;
  frameQueued = true;
  requestAnimationFrame(() => {
    frameQueued = false;
    render();
  });
}

function render(): void {
  if (state.messages.length === 0) {
    transcript.innerHTML =
      '<div class="empty">还没开始。输入一句话试试，比如：<br />' +
      '<code>读一下 workspace 里的文件，告诉我里面写了什么</code></div>';
  } else {
    transcript.replaceChildren(...state.messages.map(renderMessage));
  }
  transcript.scrollTop = transcript.scrollHeight;

  renderChips();
  updateComposer();

  if (state.lastError) {
    statusLine.textContent = `${state.lastError.code}: ${state.lastError.message}`;
    statusLine.classList.add("error");
  } else if (state.stats.rounds > 0) {
    statusLine.classList.remove("error");
    statusLine.textContent = `本轮：${state.stats.rounds} 次模型调用 · ${state.stats.toolCallsRun} 次工具调用`;
  } else {
    statusLine.classList.remove("error");
    statusLine.textContent = "";
  }
}

function renderMessage(msg: MessageView, index: number): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = `msg ${msg.role}`;
  wrap.dataset.index = String(index);

  const who = document.createElement("div");
  who.className = "who";
  who.textContent = msg.role === "user" ? "你" : "助手";
  wrap.appendChild(who);

  if (msg.role === "user") {
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = msg.content;
    wrap.appendChild(bubble);
    return wrap;
  }

  const body = document.createElement("div");
  body.className = "body";

  if (msg.reasoning) {
    const det = document.createElement("details");
    det.className = "think";
    const sum = document.createElement("summary");
    sum.textContent = `思考 · ${msg.reasoning.length} 字（点击展开）`;
    const inner = document.createElement("div");
    inner.className = "think-body";
    inner.textContent = msg.reasoning;
    det.append(sum, inner);
    body.appendChild(det);
  }

  const content = document.createElement("div");
  content.className = "content";
  content.textContent = msg.content;
  if (msg.streaming) {
    const caret = document.createElement("span");
    caret.className = "cursor";
    content.appendChild(caret);
  }
  body.appendChild(content);

  for (const call of msg.toolCalls) body.appendChild(renderToolCall(call));

  if (msg.error) {
    const err = document.createElement("div");
    err.className = "err";
    err.textContent = `${msg.error.code}: ${msg.error.message}`;
    body.appendChild(err);
  } else if (msg.finishReason && msg.finishReason !== "stop") {
    const note = document.createElement("div");
    note.className = "err";
    note.style.color = "var(--muted)";
    note.textContent = `结束原因：${msg.finishReason}`;
    body.appendChild(note);
  }

  if (msg.usage) {
    const u = msg.usage as { total_tokens?: number; prompt_tokens?: number; completion_tokens?: number };
    const note = document.createElement("div");
    note.className = "who";
    note.textContent = `tokens: ${u.total_tokens ?? "?"}（输入 ${u.prompt_tokens ?? "?"} / 输出 ${u.completion_tokens ?? "?"}）`;
    body.appendChild(note);
  }

  wrap.appendChild(body);
  return wrap;
}

function renderToolCall(call: ToolCallView): HTMLElement {
  const det = document.createElement("details");
  det.className = "tool";
  if (call.status === "awaiting-approval" || call.status === "denied" || call.status === "error") {
    det.open = true;
  }
  const sum = document.createElement("summary");

  const name = document.createElement("span");
  name.className = "name";
  name.textContent = call.name;
  const lvl = document.createElement("span");
  lvl.className = `lvl ${call.level}`;
  lvl.textContent = call.level || "?";
  const stat = document.createElement("span");
  stat.className = `stat ${call.status}`;
  stat.textContent = STATUS_LABEL[call.status] ?? call.status;

  sum.append(name, lvl, stat);
  det.appendChild(sum);

  const pre = document.createElement("pre");
  const lines: string[] = [];
  lines.push(`参数: ${JSON.stringify(call.arguments, null, 2)}`);
  if (call.decision) lines.push(`裁决: ${call.decision}`);
  if (call.durationMs !== undefined) lines.push(`耗时: ${call.durationMs}ms`);
  if (call.errorCode) lines.push(`错误: ${call.errorCode} ${call.errorMessage ?? ""}`);
  if (call.content) lines.push(`结果:\n${call.content}`);
  pre.textContent = lines.join("\n");
  det.appendChild(pre);
  return det;
}

const STATUS_LABEL: Record<string, string> = {
  pending: "待执行",
  "awaiting-approval": "等你确认",
  running: "执行中",
  denied: "已拒绝",
  ok: "完成",
  error: "失败",
};

function renderChips(): void {
  chips.replaceChildren(...pickedImages.map((p, i) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    const label = document.createElement("span");
    label.textContent = p.split(/[\\/]/).pop() ?? p;
    const rm = document.createElement("button");
    rm.type = "button";
    rm.textContent = "×";
    rm.addEventListener("click", () => {
      pickedImages.splice(i, 1);
      render();
    });
    chip.append(label, rm);
    return chip;
  }));
}

function updateComposer(): void {
  const busy = state.streaming;
  sendBtn.disabled = busy;
  stopBtn.disabled = !busy;
  sendBtn.textContent = busy ? "生成中…" : "发送";
}

/* ------------------------------------------------------------ approval */

function showDialog(): void {
  const p = state.pendingApproval;
  if (!p) return;
  dialog.classList.toggle("l3", p.level === "L3");
  dlgTitle.textContent = `请求执行：${p.name}`;
  dlgSub.textContent = p.level === "L3"
    ? "这是破坏性操作，请仔细核对后再决定。"
    : "这个操作需要你的确认。";
  if (p.level === "L3") {
    dlgWarn.hidden = false;
    dlgWarn.textContent = "L3 属于破坏性操作（删除 / 格式化 / 系统级命令），无法「总是允许」，每次都需确认。";
  } else if (p.level === "L2") {
    dlgWarn.hidden = false;
    dlgWarn.textContent = "L2 是工作区外的写入或命令执行，无法「总是允许」，每次都需确认。";
  } else {
    dlgWarn.hidden = true;
  }
  dlgLevel.textContent = p.level || "?";
  dlgReason.textContent = p.reason ?? "";
  dlgArgs.textContent = JSON.stringify(p.arguments, null, 2);
  dlgAlways.disabled = !p.canAlwaysAllow;
  overlay.hidden = false;
  dlgOnce.focus();
}

function hideDialog(): void {
  overlay.hidden = true;
}

async function answer(decision: string): Promise<void> {
  const p = state.pendingApproval;
  if (!p) { hideDialog(); return; }
  hideDialog();
  state = clearApproval(state);
  render();
  try {
    await api.approval(p.approvalId, decision);
  } catch (e) {
    statusLine.classList.add("error");
    statusLine.textContent = `回复审批失败：${(e as Error).message}`;
  }
}

/* --------------------------------------------------------------- stream */

api.onEvent((ev) => {
  if (ev.event === "hello") return;
  state = reduceEvent(state, ev.event, ev.data ?? {});
  if (ev.event === "approval.request") showDialog();
  if (ev.event === "chat.done" || ev.event === "chat.error") {
    chatId = null;
    hideDialog();
  }
  renderSoon();
});

api.onStatus((s) => {
  stateBadge.textContent = s.state;
  stateBadge.className = `badge ${s.state}`;
  metaLine.textContent = s.backendVersion ? `sidecar ${s.backendVersion}${s.pid ? ` · pid ${s.pid}` : ""}` : "";
  if (s.state === "crashed") {
    statusLine.classList.add("error");
    statusLine.textContent = "sidecar 已停止（连续重启失败）。";
  }
});

/* ---------------------------------------------------------------- send */

function wireMessages(): Array<Record<string, unknown>> {
  const out: Array<Record<string, unknown>> = [];
  for (const m of state.messages) {
    if (m.role === "user") {
      if (m.content) out.push({ role: "user", content: m.content });
    } else if (m.content) {
      out.push({ role: "assistant", content: m.content });
    }
  }
  return out;
}

async function send(): Promise<void> {
  if (state.streaming) return;
  const text = input.value.trim();
  if (!text && pickedImages.length === 0) return;

  state = reduceUserMessage(state, text, pickedImages);
  const messages = wireMessages();
  const images = pickedImages.slice();
  pickedImages = [];
  input.value = "";
  render();

  try {
    const ack = await api.chat({
      messages,
      images,
      useTools: useTools.checked,
      systemPrompt: SYSTEM_PROMPT,
    });
    chatId = ack.chatId;
  } catch (e) {
    state = reduceEvent(state, "chat.error", {
      code: "REQUEST_FAILED",
      message: (e as Error).message,
    });
    render();
  }
}

sendBtn.addEventListener("click", () => { void send(); });
stopBtn.addEventListener("click", () => {
  if (!chatId) return;
  void api.cancel(chatId).catch((e) => {
    statusLine.classList.add("error");
    statusLine.textContent = `取消失败：${(e as Error).message}`;
  });
});
pickBtn.addEventListener("click", () => {
  void api.pickImages().then((paths) => {
    pickedImages = pickedImages.concat(paths);
    render();
  });
});
dlgOnce.addEventListener("click", () => { void answer("allow_once"); });
dlgAlways.addEventListener("click", () => { void answer("allow_always"); });
dlgDeny.addEventListener("click", () => { void answer("deny"); });

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    void send();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
});

void api.getStatus().then((s) => {
  stateBadge.textContent = s.state;
  stateBadge.className = `badge ${s.state}`;
});
render();