/**
 * 「长任务期间不能发消息」的接线契约（2026-10-14）。
 *
 * 为什么是源码契约而不是组件测试：这个仓的 React UI 没有测试基建
 * （根目录 node:test 只测 electron/ 和纯逻辑模块，见 logPanelUi.test.ts
 * 的先例 —— 逻辑抽到纯模块、组件只管摆）。排队的行为逻辑已经钉在
 * sendQueue.test.ts；这里再钉住 Composer/store 两处接线，防止将来
 * 回退成「isStreaming 一闸死」。
 *
 * 修复前的红（真实代码形态）：
 *   Composer.tsx:127  if ((!text && images.length === 0) || isStreaming) return;
 *   Composer.tsx:181 const canSend = (…) && !isStreaming;
 *   Composer.tsx:277 isStreaming 时发送按钮被「停止」整个顶掉；
 *   store.tsx      没有任何排队状态。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const composerSrc = readFileSync(join(root, "ui/src/components/chat/Composer.tsx"), "utf8");
const storeSrc = readFileSync(join(root, "ui/src/state/store.tsx"), "utf8");

/* ── Composer：忙时不再闸死，改成排队 ─────────────────────────────────── */

test("submit 的旧闸「|| isStreaming) return」必须拆掉", () => {
  assert.ok(
    !/\|\|\s*isStreaming\s*\)\s*return/.test(composerSrc),
    "Composer.submit 还在用 isStreaming 把发送闸死（2026-10-14 要改成排队）",
  );
});

test("发送判定走 shouldQueueForSend（忙时排队而不是丢掉）", () => {
  assert.match(composerSrc, /shouldQueueForSend\(/);
  assert.match(composerSrc, /enqueueSend\(/);
});

test("canSend 不再看 isStreaming：忙时有内容就能点发送（进队列）", () => {
  assert.ok(composerSrc.includes("const canSend = (value.trim().length > 0 || images.length > 0);"));
});

test("占位文案告诉用户「忙时也能说」（中文）", () => {
  assert.match(composerSrc, /可继续输入/);
  assert.match(composerSrc, /排队/);
});

test("排队的话可见、可撤销（中文文案）", () => {
  assert.match(composerSrc, /queueHint\(/);
  assert.match(composerSrc, /cancelQueuedTurn\(/);
  assert.match(composerSrc, /撤销排队/);
});

test("「停止」能力保留：忙时停止按钮还在，不被发送按钮顶掉", () => {
  assert.match(composerSrc, /stopStreaming/);
  assert.match(composerSrc, /aria-label="停止生成"/);
  // 两键并存：isStreaming 分支渲染停止键，发送键不再被 else 顶掉
  assert.ok(!/\)\s*:\s*\(\s*<button[\s\S]{0,200}composer__send/.test(composerSrc));
});

/* ── store：队列状态 + 本轮结束自动送出 ───────────────────────────────── */

test("store 有发送队列（状态 + 入队 + 撤销）", () => {
  assert.match(storeSrc, /queuedTurns/);
  assert.match(storeSrc, /enqueueSend/);
  assert.match(storeSrc, /cancelQueuedTurn/);
});

test("本轮结束自动送出：按队头自己的会话发（换会话不串门）", () => {
  assert.match(storeSrc, /head\.sessionId/);
  assert.match(storeSrc, /sessionId\?: string/);
});

test("排队期间不落消息流：送出那一刻才像手打的一样出现", () => {
  // 入队只进队列数组；只有 send() 才往 messages 里追加用户消息
  const at = storeSrc.indexOf("enqueueSend = useCallback");
  assert.ok(at >= 0, "store 里没有 enqueueSend");
  const enqueueBlock = storeSrc.slice(at, at + 700);
  assert.ok(!/setMessages/.test(enqueueBlock), "入队不应写消息流（会让人以为已经发出去了）");
});
