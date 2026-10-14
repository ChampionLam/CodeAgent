/**
 * desktop hub 的发送韧性（2026-10-14 用户报「Desktop hub 功能使用不了，无法发送消息」）。
 *
 * hub 是独立 HTML 窗体（electron/hub.html，不是 React 应用），所以这里沿用
 * logPanelUi / composerQueueWiring 的做法：钉住源码接线，防止回退。
 *
 * 修复前的两处硬伤：
 *   1. send() 里 `await window.hub.ask(...)` 没有 try/catch —— 只要抛一次错
 *      （侧车没起来 / 模型没配 / 网络超时），state.streaming 永远停在 true，
 *      之后每次回车/点发送都被开头那句守卫吃掉 → 整个 hub 从此发不出消息；
 *   2. state.chatId 是在 ask 的 ack 之后才赋值的，而事件过滤器要求
 *      `p.chatId === state.chatId` —— 首个事件先到就被丢掉，最后 chat.done 也收不到，
 *      streaming 同样卡死。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = readFileSync(join(root, "electron/hub.html"), "utf8");

function sendBlock(): string {
  const at = src.indexOf("async function send()");
  assert.ok(at >= 0, "hub.html 里没有 send()");
  return src.slice(at, at + 1500);
}

test("ask 失败不能把输入锁死：try/catch + 复位 streaming", () => {
  const block = sendBlock();
  assert.match(block, /try\s*\{[\s\S]*hub\.ask\(/, "hub.ask 必须包在 try 里");
  assert.match(block, /catch\s*\(/, "必须有 catch");
  const catchBlock = block.slice(block.indexOf("catch"));
  assert.match(catchBlock, /state\.streaming\s*=\s*false/,
               "失败后必须复位 streaming，否则之后永远发不出去");
});

test("失败要当面告诉用户，不能静默卡住", () => {
  assert.match(src, /发送失败：/);
});

test("失败后不丢用户已经打进去的字", () => {
  const block = sendBlock();
  const catchBlock = block.slice(block.indexOf("catch"));
  assert.match(catchBlock, /state\.text\s*=\s*text/);
});

test("首个事件早于 ack 到达时不能丢（chatId 竞态）", () => {
  const at = src.indexOf("window.hub.onEvent");
  assert.ok(at >= 0, "hub.html 里没有 onEvent 接线");
  const tail = src.slice(at, at + 900);
  assert.ok(
    !/if\s*\(\s*!state\.chatId\s*\|\|\s*p\.chatId\s*!==\s*state\.chatId\s*\)\s*return/.test(tail),
    "旧的过滤条件会把 ack 之前的事件全丢掉（chat.done 收不到 → 卡死）",
  );
  assert.match(tail, /state\.chatId\s*=\s*String\(p\.chatId\)/,
               "chatId 还空着时应就地认领这条事件");
});

test("重新唤起 hub 时解掉卡在半路的 streaming", () => {
  const at = src.indexOf("window.hub.onShown");
  assert.ok(at >= 0);
  assert.match(src.slice(at, at + 300),
               /if\s*\(state\.streaming && !state\.chatId\)\s*state\.streaming = false/);
});
test("hub 发请求必须带上模型（否则落到空白 model 段，新装实例直接 CONFIG_ERROR）", () => {
  const main = readFileSync(join(root, "electron/main.ts"), "utf8");
  const at = main.indexOf('ipcMain.handle("hub:ask"');
  assert.ok(at >= 0, "main.ts 里没有 hub:ask");
  const block = main.slice(at, at + 1200);
  assert.match(block, /hubModelPick\(/, "hub:ask 必须用 hubModelPick() 解析当前模型");
  assert.match(block, /modelId\s*\?\s*\{\s*modelId\s*\}/, "解析出来的 id 必须以 modelId 传进 chat 请求");
  // models.list 的形状是 items + activeId/defaultId —— 没有 isDefault 字段，认错键就等于没传。
  const pick = main.slice(main.indexOf("async function hubModelPick"), main.indexOf("async function hubModelPick") + 1200);
  assert.match(pick, /items/, "models.list 的行在 items 里");
  assert.match(pick, /activeId|defaultId/, "默认模型要看 activeId/defaultId");
});
