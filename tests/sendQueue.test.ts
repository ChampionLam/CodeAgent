/**
 * 生成中继续说话：发送队列的口径（2026-10-14 用户诉求「长任务期间也要能发消息」）。
 *
 * 修复前：ui/src/components/chat/Composer.tsx:127/181 用 isStreaming 把发送闸死，
 * 模型做长任务时用户打完字按回车没反应，只能干等或「停止」重来。
 * 修复后：输入框全程可用 —— 忙时这句话进队列（FIFO，记住所属会话），
 * 本轮一结束自动送出；「停止」只停当前这轮，不动队列。
 *
 * 这里盯的是 ui/src/state/sendQueue.ts 的纯逻辑（不 import react / 不碰
 * window，跟 pages/logPanel.ts 同一套路）；React 接线由 ui 的 tsc 兜底。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  dequeue,
  isSendable,
  nextQueuedId,
  queueHint,
  removeQueued,
  shouldQueueForSend,
  type QueuedTurn,
} from "../ui/src/state/sendQueue.ts";

function turn(id: string, text: string, sessionId = "s-1"): QueuedTurn {
  return { id, sessionId, text };
}

/* ── 入队判定：什么时候直接发、什么时候排队 ─────────────────────────── */

test("正在生成时输入要排队（修复前这条路径是被直接禁用的）", () => {
  assert.equal(shouldQueueForSend(true, 0), true);
});

test("空闲且队列空：直接发送，不排队", () => {
  assert.equal(shouldQueueForSend(false, 0), false);
});

test("「停止」之后队列还有存货：新输入照样排队，先来后到不插队", () => {
  assert.equal(shouldQueueForSend(false, 2), true);
});

test("空话（没字没附件）不排队", () => {
  assert.equal(isSendable(""), false);
  assert.equal(isSendable("   "), false);
  assert.equal(isSendable("看看", undefined), true);
  assert.equal(isSendable("", ["/tmp/a.png"]), true);
});

/* ── FIFO 出队 ────────────────────────────────────────────────────────── */

test("出队按先后顺序：先排的先发，发完的不再出现", () => {
  const q = [turn("q-1", "第一条"), turn("q-2", "第二条"), turn("q-3", "第三条")];
  const a = dequeue(q);
  const b = dequeue(a!.rest);
  const c = dequeue(b!.rest);
  assert.equal(a!.turn.text, "第一条");
  assert.equal(b!.turn.text, "第二条");
  assert.equal(c!.turn.text, "第三条");
  assert.equal(dequeue(c!.rest), null);
});

test("dequeue / removeQueued 不改动传入的队列（React 状态不可变）", () => {
  const q = [turn("q-1", "a"), turn("q-2", "b")];
  const snapshot = JSON.stringify(q);
  dequeue(q);
  removeQueued(q, "q-1");
  assert.equal(JSON.stringify(q), snapshot);
});

test("取消排队按 id 精确移除；取消不存在的 id 队列原样", () => {
  const q = [turn("q-1", "a"), turn("q-2", "b"), turn("q-3", "c")];
  assert.deepEqual(removeQueued(q, "q-2").map(t => t.id), ["q-1", "q-3"]);
  assert.deepEqual(removeQueued(q, "nope"), q);
});

/* ── 排队项自带会话：换会话后自动发送不能串门 ─────────────────────────── */

test("排队项记住自己属于哪个会话，自动发送时按它自己的会话走", () => {
  const q = [turn("q-1", "把按钮改成蓝色", "s-A"), turn("q-2", "顺带看下日志", "s-B")];
  const a = dequeue(q)!;
  const b = dequeue(a.rest)!;
  assert.equal(a.turn.sessionId, "s-A");
  assert.equal(b.turn.sessionId, "s-B");
});

/* ── 界面文案（中文） ─────────────────────────────────────────────────── */

test("排队提示是中文、条数对；没排队时不占地方", () => {
  assert.equal(queueHint(0), "");
  assert.equal(queueHint(1), "已排队 1 条：本轮结束后自动发送");
  assert.match(queueHint(3), /已排队 3 条/);
  assert.match(queueHint(3), /本轮结束后/);
});

test("排队 id 唯一且带前缀（React key 稳定）", () => {
  assert.match(nextQueuedId(1), /^q-/);
  assert.notEqual(nextQueuedId(1), nextQueuedId(2));
});
