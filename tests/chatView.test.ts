/**
 * Tests for chatView: the event-stream → render-state reducer.
 *
 * These run headless (no DOM, no sidecar). The point of this module existing at
 * all is that the interesting logic is testable without a display.
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  clearApproval,
  initialChatView,
  lastAssistant,
  reduceEvent,
  reduceUserMessage,
  type ChatViewState,
} from "../electron/chatView";

function started(): ChatViewState {
  return reduceUserMessage(initialChatView(), "你好");
}

/* ------------------------------------------------------------------ init */

test("initial state is empty and idle", () => {
  const s = initialChatView();
  assert.deepEqual(s.messages, []);
  assert.equal(s.streaming, false);
  assert.equal(s.pendingApproval, null);
  assert.equal(s.lastError, null);
  assert.deepEqual(s.stats, { rounds: 0, toolCallsRun: 0 });
});

/* --------------------------------------------------------- user message */

test("sending a message appends the user message plus an empty assistant", () => {
  const s = started();
  assert.equal(s.messages.length, 2);
  assert.equal(s.messages[0].role, "user");
  assert.equal(s.messages[0].content, "你好");
  assert.equal(s.messages[1].role, "assistant");
  assert.equal(s.messages[1].content, "");
  assert.equal(s.messages[1].streaming, true);
  assert.equal(s.streaming, true);
});

test("附件不再往正文里拼备注（2026-09-27 用户口径「这里的附带一个文件不要了」）", () => {
  const s = reduceUserMessage(initialChatView(), "看这个", ["/tmp/a.png", "/tmp/b.png"]);
  assert.match(s.messages[0].content, /看这个/);
  // 负向断言：正文保持用户原话，附件靠消息下方的卡展示
  assert.doesNotMatch(s.messages[0].content, /张图片/);
  assert.doesNotMatch(s.messages[0].content, /附带/);
  assert.equal(s.messages[0].content, "看这个");
});

test("reduceUserMessage does not mutate the previous state", () => {
  const first = started();
  const snapshot = JSON.stringify(first);
  const second = reduceUserMessage(first, "再来一句");
  assert.equal(JSON.stringify(first), snapshot);
  assert.equal(second.messages.length, 4);
});

/* ------------------------------------------------------- two channels */

test("reasoning and content land in separate channels, in order", () => {
  let s = started();
  s = reduceEvent(s, "chat.reasoning", { text: "先想" });
  s = reduceEvent(s, "chat.delta", { text: "你好" });
  s = reduceEvent(s, "chat.reasoning", { text: "再想" });
  s = reduceEvent(s, "chat.delta", { text: "世界" });
  const a = lastAssistant(s);
  assert.ok(a);
  assert.equal(a.reasoning, "先想再想");
  assert.equal(a.content, "你好世界");
});

test("empty text is ignored rather than appended", () => {
  let s = started();
  s = reduceEvent(s, "chat.delta", { text: "" });
  assert.equal(lastAssistant(s)?.content, "");
});

test("non-string text does not throw and does not corrupt state", () => {
  let s = started();
  const before = JSON.stringify(s);
  s = reduceEvent(s, "chat.delta", { text: 42 });
  assert.equal(JSON.stringify(s), before);
});

/* --------------------------------------------------------- tool calls */

test("tool.call without approval starts as running", () => {
  let s = started();
  s = reduceEvent(s, "tool.call", {
    id: "c1", name: "read_file", arguments: { path: "/w/a.txt" },
    level: "L0", requiresApproval: false, canAlwaysAllow: false, reason: "L0 auto-allow",
  });
  const call = lastAssistant(s)?.toolCalls[0];
  assert.ok(call);
  assert.equal(call.status, "running");
  assert.equal(call.name, "read_file");
  assert.deepEqual(call.arguments, { path: "/w/a.txt" });
  assert.equal(call.level, "L0");
});

test("tool.call needing approval starts as awaiting-approval", () => {
  let s = started();
  s = reduceEvent(s, "tool.call", {
    id: "c1", name: "write_file", arguments: {}, level: "L1",
    requiresApproval: true, canAlwaysAllow: true, reason: "classified as L1",
  });
  assert.equal(lastAssistant(s)?.toolCalls[0].status, "awaiting-approval");
});

test("approval.request sets pendingApproval", () => {
  let s = started();
  s = reduceEvent(s, "approval.request", {
    approvalId: "apr_1", toolCallId: "c1", name: "write_file",
    arguments: { path: "/w/a.txt" }, level: "L1", reason: "classified as L1",
    canAlwaysAllow: true,
  });
  assert.ok(s.pendingApproval);
  assert.equal(s.pendingApproval.approvalId, "apr_1");
  assert.equal(s.pendingApproval.toolCallId, "c1");
  assert.equal(s.pendingApproval.level, "L1");
  assert.equal(s.pendingApproval.canAlwaysAllow, true);
});

test("tool.result ok resolves the call and clears the approval", () => {
  let s = started();
  s = reduceEvent(s, "tool.call", {
    id: "c1", name: "write_file", arguments: {}, level: "L1",
    requiresApproval: true, canAlwaysAllow: true, reason: "x",
  });
  s = reduceEvent(s, "approval.request", {
    approvalId: "apr_1", toolCallId: "c1", name: "write_file", arguments: {},
    level: "L1", reason: "x", canAlwaysAllow: true,
  });
  s = reduceEvent(s, "tool.result", {
    id: "c1", name: "write_file", ok: true, content: "Wrote 7 bytes",
    errorCode: null, errorMessage: null, durationMs: 3, decision: "allow_once",
  });
  const call = lastAssistant(s)?.toolCalls[0];
  assert.equal(call?.status, "ok");
  assert.equal(call?.decision, "allow_once");
  assert.equal(call?.content, "Wrote 7 bytes");
  assert.equal(call?.durationMs, 3);
  assert.equal(s.pendingApproval, null);
});

test("tool.result with deny marks the call denied", () => {
  let s = started();
  s = reduceEvent(s, "tool.call", {
    id: "c1", name: "write_file", arguments: {}, level: "L1",
    requiresApproval: true, canAlwaysAllow: true, reason: "x",
  });
  s = reduceEvent(s, "tool.result", {
    id: "c1", name: "write_file", ok: false, content: "用户拒绝了这次工具调用",
    errorCode: "DENIED", errorMessage: "x", durationMs: 0, decision: "deny",
  });
  const call = lastAssistant(s)?.toolCalls[0];
  assert.equal(call?.status, "denied");
  assert.equal(call?.errorCode, "DENIED");
});

test("tool.result with failure marks the call error", () => {
  let s = started();
  s = reduceEvent(s, "tool.call", {
    id: "c1", name: "run_shell", arguments: {}, level: "L2",
    requiresApproval: false, canAlwaysAllow: false, reason: "x",
  });
  s = reduceEvent(s, "tool.result", {
    id: "c1", name: "run_shell", ok: false, content: "exit code 3",
    errorCode: "NONZERO_EXIT", errorMessage: "exit 3", durationMs: 12, decision: "allow_once",
  });
  assert.equal(lastAssistant(s)?.toolCalls[0].status, "error");
});

test("results for an unknown tool id leave other calls untouched", () => {
  let s = started();
  s = reduceEvent(s, "tool.call", {
    id: "c1", name: "read_file", arguments: {}, level: "L0",
    requiresApproval: false, canAlwaysAllow: false, reason: "x",
  });
  s = reduceEvent(s, "tool.result", { id: "nope", name: "read_file", ok: true, decision: "allow_once" });
  assert.equal(lastAssistant(s)?.toolCalls[0].status, "running");
});

test("multiple tool calls are kept in order with their own statuses", () => {
  let s = started();
  for (const id of ["c1", "c2"]) {
    s = reduceEvent(s, "tool.call", {
      id, name: "read_file", arguments: {}, level: "L0",
      requiresApproval: false, canAlwaysAllow: false, reason: "x",
    });
  }
  s = reduceEvent(s, "tool.result", { id: "c2", name: "read_file", ok: false, decision: "allow_once" });
  const calls = lastAssistant(s)?.toolCalls ?? [];
  assert.deepEqual(calls.map((c) => c.status), ["running", "error"]);
});

test("clearApproval drops the pending dialog", () => {
  let s = started();
  s = reduceEvent(s, "approval.request", {
    approvalId: "apr_1", toolCallId: "c1", name: "write_file",
    arguments: {}, level: "L1", reason: "x", canAlwaysAllow: true,
  });
  s = clearApproval(s);
  assert.equal(s.pendingApproval, null);
});

/* ------------------------------------------------------------ terminal */

test("chat.done finishes the message and records stats", () => {
  let s = started();
  s = reduceEvent(s, "chat.delta", { text: "好了" });
  s = reduceEvent(s, "chat.done", {
    finishReason: "stop", usage: { total_tokens: 42 }, rounds: 3, toolCallsRun: 2,
  });
  const a = lastAssistant(s);
  assert.ok(a);
  assert.equal(a.streaming, false);
  assert.equal(a.finishReason, "stop");
  assert.deepEqual(a.usage, { total_tokens: 42 });
  assert.equal(s.streaming, false);
  assert.deepEqual(s.stats, { rounds: 3, toolCallsRun: 2 });
  assert.equal(s.pendingApproval, null);
});

test("chat.done with missing stats falls back to zero", () => {
  let s = started();
  s = reduceEvent(s, "chat.done", { finishReason: "stop" });
  assert.deepEqual(s.stats, { rounds: 0, toolCallsRun: 0 });
});

test("chat.error records the error on message and state", () => {
  let s = started();
  s = reduceEvent(s, "chat.error", { code: "LOOP_LIMIT", message: "已达最大循环次数 20" });
  const a = lastAssistant(s);
  assert.equal(a?.streaming, false);
  assert.deepEqual(a?.error, { code: "LOOP_LIMIT", message: "已达最大循环次数 20" });
  assert.deepEqual(s.lastError, { code: "LOOP_LIMIT", message: "已达最大循环次数 20" });
  assert.equal(s.streaming, false);
});

/* ------------------------------------------------------------- defensive */

test("events arriving before any assistant message still land safely", () => {
  let s = initialChatView();
  s = reduceEvent(s, "chat.delta", { text: "抢先一步" });
  assert.equal(s.messages.length, 1);
  assert.equal(s.messages[0].role, "assistant");
  assert.equal(s.messages[0].content, "抢先一步");
});

test("unknown events are passed through untouched", () => {
  const s = started();
  const before = JSON.stringify(s);
  const after = reduceEvent(s, "hello", { protocolVersion: 1 });
  assert.equal(JSON.stringify(after), before);
  const after2 = reduceEvent(s, "something.new", { whatever: true });
  assert.equal(JSON.stringify(after2), before);
});

test("event data is not mutated", () => {
  let s = started();
  const data: Record<string, unknown> = { text: "abc" };
  const snapshot = JSON.stringify(data);
  s = reduceEvent(s, "chat.delta", data);
  assert.equal(JSON.stringify(data), snapshot);
});

test("a full happy-path sequence ends in a coherent state", () => {
  let s = reduceUserMessage(initialChatView(), "读一下文件");
  s = reduceEvent(s, "chat.reasoning", { text: "我得先读文件" });
  s = reduceEvent(s, "tool.call", {
    id: "c1", name: "read_file", arguments: { path: "/w/a.txt" }, level: "L0",
    requiresApproval: false, canAlwaysAllow: false, reason: "L0 auto-allow",
  });
  s = reduceEvent(s, "tool.result", {
    id: "c1", name: "read_file", ok: true, content: "hello 世界",
    errorCode: null, errorMessage: null, durationMs: 1, decision: "allow_once",
  });
  s = reduceEvent(s, "chat.delta", { text: "内容是 hello 世界" });
  s = reduceEvent(s, "chat.done", {
    finishReason: "stop", usage: { total_tokens: 120 }, rounds: 2, toolCallsRun: 1,
  });

  assert.equal(s.messages.length, 2);
  const a = s.messages[1];
  assert.equal(a.reasoning, "我得先读文件");
  assert.equal(a.content, "内容是 hello 世界");
  assert.equal(a.toolCalls.length, 1);
  assert.equal(a.toolCalls[0].status, "ok");
  assert.equal(a.streaming, false);
  assert.equal(s.streaming, false);
  assert.equal(s.pendingApproval, null);
  assert.equal(s.lastError, null);
  assert.deepEqual(s.stats, { rounds: 2, toolCallsRun: 1 });
});