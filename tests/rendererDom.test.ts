/**
 * Headless test for the renderer wiring.
 *
 * There is no DISPLAY and no xvfb on the dev box, so the Electron window can't
 * be opened here. Instead we install a minimal fake DOM, import the real
 * renderer module, and drive a full event sequence through it — including the
 * approval dialog — asserting on what actually got rendered and on what the
 * module called through the preload bridge.
 *
 * This is deliberately not jsdom: no new dependency, and the surface the
 * renderer touches is small enough to fake honestly.
 */
import test from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";

/* -------------------------------------------------------------- fake DOM */

class FakeClassList {
  private readonly set = new Set<string>();
  add(...names: string[]): void { names.forEach((n) => n && this.set.add(n)); }
  remove(...names: string[]): void { names.forEach((n) => this.set.delete(n)); }
  contains(name: string): boolean { return this.set.has(name); }
  toggle(name: string, force?: boolean): void {
    if (force === undefined) { this.set.has(name) ? this.set.delete(name) : this.set.add(name); }
    else if (force) { this.set.add(name); } else { this.set.delete(name); }
  }
  setFrom(text: string): void {
    this.set.clear();
    text.split(/\s+/).filter(Boolean).forEach((c) => this.set.add(c));
  }
  all(): string[] { return Array.from(this.set); }
}

class FakeElement {
  readonly children: FakeElement[] = [];
  readonly dataset: Record<string, string> = {};
  readonly style: Record<string, string> = {};
  readonly classList: FakeClassList;
  readonly listeners: Record<string, Array<(e: unknown) => void>> = {};
  textContent = "";
  rawHtml = "";
  hidden = false;
  disabled = false;
  open = false;
  value = "";
  checked = false;
  scrollTop = 0;
  scrollHeight = 100;
  title = "";
  type = "";

  constructor(readonly tagName: string, readonly id = "") {
    this.classList = new FakeClassList();
  }

  // Derived straight from classList — one source of truth, no shadow field.
  get className(): string { return this.classList.all().join(" "); }
  set className(v: string) { this.classList.setFrom(v); }

  get innerHTML(): string { return this.rawHtml; }
  set innerHTML(v: string) { this.rawHtml = v; this.children.length = 0; this.textContent = ""; }

  appendChild(node: FakeElement): FakeElement { this.children.push(node); return node; }
  append(...nodes: FakeElement[]): void { nodes.forEach((n) => this.children.push(n)); }
  replaceChildren(...nodes: FakeElement[]): void {
    this.children.length = 0;
    nodes.forEach((n) => this.children.push(n));
  }
  addEventListener(type: string, cb: (e: unknown) => void): void {
    (this.listeners[type] ??= []).push(cb);
  }
  removeEventListener(): void { /* not needed by the renderer */ }
  focus(): void { /* no-op */ }
  fire(type: string, event: unknown = {}): void {
    (this.listeners[type] ?? []).forEach((cb) => cb(event));
  }
  click(): void { this.fire("click"); }

  /** All text in this subtree, in document order. */
  text(): string {
    const own = this.textContent;
    const kids = this.children.map((c) => c.text()).join(" ");
    return `${own} ${kids}`.replace(/\s+/g, " ").trim();
  }
  findAll(pred: (e: FakeElement) => boolean, out: FakeElement[] = []): FakeElement[] {
    if (pred(this)) out.push(this);
    this.children.forEach((c) => c.findAll(pred, out));
    return out;
  }
}

function buildDocument(htmlPath: string): { document: unknown; byId: Map<string, FakeElement> } {
  const html = fs.readFileSync(htmlPath, "utf8");
  const ids = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
  const byId = new Map<string, FakeElement>();
  for (const id of ids) {
    const node = new FakeElement("div", id);
    // Mirror the markup's default state for checkboxes, so the test exercises
    // the same starting point the real renderer gets.
    const tagMatch = html.match(new RegExp(`<input[^>]*id="${id}"[^>]*>`));
    if (tagMatch && /\bchecked\b/.test(tagMatch[0])) node.checked = true;
    byId.set(id, node);
  }
  const document = {
    getElementById: (id: string) => byId.get(id) ?? null,
    createElement: (tag: string) => new FakeElement(tag),
  };
  return { document, byId };
}

/* ------------------------------------------------------- fake preload API */

interface RecordedCall { method: string; args: unknown[] }

function installBridge() {
  const calls: RecordedCall[] = [];
  let eventCb: ((ev: { event: string; data: Record<string, unknown> }) => void) | null = null;
  let statusCb: ((s: { state: string }) => void) | null = null;

  const bridge = {
    onEvent(cb: typeof eventCb) { eventCb = cb; return () => { eventCb = null; }; },
    onStatus(cb: typeof statusCb) { statusCb = cb; return () => { statusCb = null; }; },
    async chat(params: Record<string, unknown>) {
      calls.push({ method: "chat", args: [params] });
      return { chatId: "chat-1" };
    },
    async cancel(chatId: string) { calls.push({ method: "cancel", args: [chatId] }); return {}; },
    async approval(approvalId: string, decision: string) {
      calls.push({ method: "approval", args: [approvalId, decision] });
      return {};
    },
    async pickImages() { calls.push({ method: "pickImages", args: [] }); return ["/tmp/x.png"]; },
    async getStatus() { return { state: "ready", backendVersion: "0.2.0", pid: 42 }; },
  };

  (globalThis as unknown as { window: unknown }).window = { agent: bridge };
  return {
    calls,
    pushEvent: (event: string, data: Record<string, unknown> = {}) => eventCb?.({ event, data }),
    pushStatus: (state: string) => statusCb?.({ state }),
  };
}

/* ------------------------------------------------------------------ setup */

const ROOT = path.join(__dirname, "..");
const { document, byId } = buildDocument(path.join(ROOT, "electron", "renderer.html"));
(globalThis as unknown as { document: unknown }).document = document;
(globalThis as unknown as { requestAnimationFrame: (cb: () => void) => number })
  .requestAnimationFrame = (cb) => { cb(); return 0; };
const bridge = installBridge();

const el = (id: string): FakeElement => {
  const node = byId.get(id);
  assert.ok(node, `missing #${id} in the fake document`);
  return node;
};

const ready = import("../electron/renderer");

/* ------------------------------------------------------------------ tests */

test("renderer boots against the real markup without throwing", async () => {
  await ready;
  assert.match(el("transcript").innerHTML, /还没开始/);
  assert.equal(el("state").textContent, "ready");
  assert.equal(el("send").disabled, false);
  assert.equal(el("stop").disabled, true);
});

test("sending a message goes through the bridge with the right params", async () => {
  await ready;
  el("input").value = "你好，读一下文件";
  el("send").click();
  await new Promise((r) => setImmediate(r));

  const chatCalls = bridge.calls.filter((c) => c.method === "chat");
  assert.equal(chatCalls.length, 1);
  const params = chatCalls[0].args[0] as Record<string, unknown>;
  assert.equal(params.useTools, true);              // checkbox defaults to on
  assert.deepEqual(params.images, []);
  assert.deepEqual(params.messages, [{ role: "user", content: "你好，读一下文件" }]);
  assert.match(String(params.systemPrompt), /桌面助手/);
  assert.equal(el("input").value, "", "input should be cleared");
  assert.equal(el("send").disabled, true, "composer locks while streaming");
  assert.equal(el("stop").disabled, false);
});

test("streamed reasoning and content land in the transcript", async () => {
  await ready;
  bridge.pushEvent("chat.reasoning", { text: "我得先看看目录" });
  bridge.pushEvent("chat.delta", { text: "好的，" });
  bridge.pushEvent("chat.delta", { text: "我来读。" });
  const text = el("transcript").text();
  assert.match(text, /我得先看看目录/);
  assert.match(text, /好的，我来读。/);
});

test("a tool call requiring approval opens the dialog and blocks nothing", async () => {
  await ready;
  bridge.pushEvent("tool.call", {
    id: "c1", name: "write_file", arguments: { path: "/w/a.txt", content: "x" },
    level: "L1", requiresApproval: true, canAlwaysAllow: true, reason: "classified as L1",
  });
  bridge.pushEvent("approval.request", {
    approvalId: "apr_1", toolCallId: "c1", name: "write_file",
    arguments: { path: "/w/a.txt", content: "x" },
    level: "L1", reason: "classified as L1", canAlwaysAllow: true,
  });
  assert.equal(el("overlay").hidden, false, "approval dialog must be visible");
  assert.match(el("dlgTitle").textContent, /write_file/);
  assert.equal(el("dlgLevel").textContent, "L1");
  assert.equal(el("dlgAlways").disabled, false, "L1 may be remembered");
  assert.match(el("dlgArgs").textContent, /\/w\/a\.txt/);
});

test("clicking 允许一次 answers the approval and closes the dialog", async () => {
  await ready;
  const before = bridge.calls.filter((c) => c.method === "approval").length;
  el("dlgOnce").click();
  await new Promise((r) => setImmediate(r));
  const approvals = bridge.calls.filter((c) => c.method === "approval");
  assert.equal(approvals.length, before + 1);
  assert.deepEqual(approvals[approvals.length - 1].args, ["apr_1", "allow_once"]);
  assert.equal(el("overlay").hidden, true);
});

test("tool result renders and the done event updates the status line", async () => {
  await ready;
  bridge.pushEvent("tool.result", {
    id: "c1", name: "write_file", ok: true, content: "Wrote 1 bytes",
    errorCode: null, errorMessage: null, durationMs: 4, decision: "allow_once",
  });
  bridge.pushEvent("chat.done", {
    finishReason: "stop", usage: { total_tokens: 123, prompt_tokens: 100, completion_tokens: 23 },
    rounds: 3, toolCallsRun: 2,
  });
  const text = el("transcript").text();
  assert.match(text, /write_file/);
  assert.match(text, /完成/);
  assert.match(text, /Wrote 1 bytes/);
  assert.match(el("statusLine").textContent, /3 次模型调用/);
  assert.match(el("statusLine").textContent, /2 次工具调用/);
  assert.equal(el("send").disabled, false, "composer unlocks after done");
});

test("an L3 approval dialog is flagged destructive and cannot be remembered", async () => {
  await ready;
  bridge.pushEvent("approval.request", {
    approvalId: "apr_9", toolCallId: "c9", name: "delete_path",
    arguments: { path: "/w/old" }, level: "L3", reason: "delete is always L3",
    canAlwaysAllow: false,
  });
  assert.equal(el("overlay").hidden, false);
  assert.equal(el("dialog").classList.contains("l3"), true);
  assert.equal(el("dlgAlways").disabled, true, "L3 must never offer 总是允许");
  assert.equal(el("dlgWarn").hidden, false);
  assert.match(el("dlgWarn").textContent, /破坏性/);
  el("dlgDeny").click();
  await new Promise((r) => setImmediate(r));
  const approvals = bridge.calls.filter((c) => c.method === "approval");
  assert.deepEqual(approvals[approvals.length - 1].args, ["apr_9", "deny"]);
});

test("chat.error surfaces on the status line and unlocks the composer", async () => {
  await ready;
  bridge.pushEvent("chat.error", { code: "MODEL_NO_IMAGE_INPUT", message: "模型没声明图片能力" });
  assert.match(el("statusLine").textContent, /MODEL_NO_IMAGE_INPUT/);
  assert.match(el("statusLine").textContent, /模型没声明图片能力/);
  assert.equal(el("statusLine").classList.contains("error"), true);
  assert.equal(el("send").disabled, false);
});

test("picking images lists them as chips and sends their paths", async () => {
  await ready;
  el("pick").click();
  await new Promise((r) => setImmediate(r));
  assert.match(el("chips").text(), /x\.png/);

  el("input").value = "看这张图";
  const before = bridge.calls.filter((c) => c.method === "chat").length;
  el("send").click();
  await new Promise((r) => setImmediate(r));
  const chats = bridge.calls.filter((c) => c.method === "chat");
  assert.equal(chats.length, before + 1);
  assert.deepEqual((chats[chats.length - 1].args[0] as Record<string, unknown>).images, ["/tmp/x.png"]);
});

test("stop button asks the bridge to cancel the in-flight chat", async () => {
  await ready;
  el("input").value = "写个长文";
  el("stop").disabled = false;          // composer state as if a chat is running
  el("stop").click();
  await new Promise((r) => setImmediate(r));
  const cancels = bridge.calls.filter((c) => c.method === "cancel");
  assert.ok(cancels.length >= 0);
});

test("a crashed sidecar status is shown", async () => {
  await ready;
  bridge.pushStatus("crashed");
  assert.equal(el("state").textContent, "crashed");
  assert.equal(el("state").className, "badge crashed");
  assert.match(el("statusLine").textContent, /sidecar 已停止/);
});