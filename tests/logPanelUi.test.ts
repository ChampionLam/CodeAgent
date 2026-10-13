/**
 * 设置页「日志」区的界面口径（2026-10-14）。
 *
 * 用户原话：「程序有问题都可以找到」。所以失败时这里必须说清三件事：
 * 什么状态、什么原因、侧车最后几行说了什么 —— 不许只留一句「未就绪」。
 *
 * 修复前：ui/src/pages/logPanel.ts 不存在，设置页没有任何日志区。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  dirSubtitle,
  formatInterfaceError,
  formatLogOpenError,
  missingDirHint,
  readInterfaceError,
  reportInterfaceError,
  resetInterfaceError,
  sidecarFailure,
  stderrExcerpt,
  subscribeInterfaceError,
} from "../ui/src/pages/logPanel.ts";
import type { LogDirEntry, LogSidecarState } from "../ui/src/state/live.ts";

const ready: LogSidecarState = { state: "ready", restartAttempts: 0, lastError: null };
const crashed: LogSidecarState = {
  state: "crashed",
  restartAttempts: 3,
  lastError: { code: "SIDECAR_DEAD", message: "max restarts exhausted" },
};

/* ── 失败原因是人话 ──────────────────────────────────────────────────────── */

test("侧车正常/启动中：日志区不喊故障（没事别吓人）", () => {
  assert.equal(sidecarFailure(ready), "");
  assert.equal(sidecarFailure({ state: "starting", restartAttempts: 0, lastError: null }), "");
});

test("侧车 crashed：崩溃原因 + 重启次数都要写出来", () => {
  const s = sidecarFailure(crashed);
  assert.match(s, /崩溃/);
  assert.match(s, /SIDECAR_DEAD：max restarts exhausted/);
  assert.match(s, /重启 3 次/);
});

test("侧车 crashed 但状态里没有 lastError：明说没记下原因，不编一个", () => {
  const s = sidecarFailure({ state: "crashed", restartAttempts: 0, lastError: null });
  assert.match(s, /没记下原因/);
  assert.ok(!/重启/.test(s), s);
});

test("状态整个拿不到（主进程还没起来）时也说得明白", () => {
  assert.match(sidecarFailure(null), /未知/);
});

test("重启中 / 版本不匹配 / 已停止各有各的说法", () => {
  assert.match(
    sidecarFailure({ state: "restarting", restartAttempts: 1, lastError: { code: "SIDECAR_DEAD", message: "exited code=1" } }),
    /重启/,
  );
  assert.match(
    sidecarFailure({ state: "version-mismatch", restartAttempts: 0, lastError: { code: "VERSION_MISMATCH", message: "client=1, sidecar=2" } }),
    /版本/,
  );
  assert.match(sidecarFailure({ state: "stopped", restartAttempts: 0, lastError: null }), /停止/);
});

/* ── stderr 摘要 ────────────────────────────────────────────────────────── */

test("stderr 摘要：最多 8 行、单行 200 字、剥掉前缀（摘要不整段灌界面）", () => {
  const lines = Array.from({ length: 20 }, (_, i) => `[sidecar stderr] line-${i}`);
  const out = stderrExcerpt(lines);
  assert.equal(out.length, 8);
  assert.deepEqual(out[0], "line-12");
  assert.deepEqual(out[7], "line-19");
  const [long] = stderrExcerpt([`[sidecar stderr] ${"y".repeat(400)}`]);
  assert.equal(long.length, 201);
  assert.ok(long.endsWith("…"));
});

test("stderr 摘要：空行丢掉，空输入得空数组", () => {
  assert.deepEqual(stderrExcerpt([]), []);
  assert.deepEqual(stderrExcerpt(["[sidecar stderr]   "]), []);
});

/* ── 目录说明 ──────────────────────────────────────────────────────────── */

const twoDirs: LogDirEntry[] = [
  { kind: "main", label: "主进程日志", path: "C:\\u\\logs", exists: true },
  { kind: "sidecar", label: "侧车日志（Python）", path: "C:\\d\\logs", exists: true },
];

test("目录副标题：两处目录说「各写各的」，共用一处说「都在这里」", () => {
  assert.match(dirSubtitle(twoDirs), /两个目录/);
  assert.match(dirSubtitle([{ kind: "main", label: "日志目录（主进程与侧车共用）", path: "/d/logs", exists: true }]), /同一个目录/);
});

test("目录还没生成时给一句说明（第一次写日志时才建）", () => {
  const hint = missingDirHint({ kind: "sidecar", label: "侧车日志（Python）", path: "C:\\d\\logs", exists: false });
  assert.match(hint, /还没生成/);
  assert.match(hint, /第一次写日志/);
});

test("打开失败的原因翻成人话：unknown-dir 不是系统错误", () => {
  assert.match(formatLogOpenError("unknown-dir"), /不认识/);
  assert.match(formatLogOpenError("Failed to open path"), /Failed to open path/);
  assert.equal(formatLogOpenError(""), "");
});

/* ── 模型接口报错也要落在日志区 ─────────────────────────────────────────── */

test("模型接口报错会被记住并通知日志区", () => {
  resetInterfaceError();
  assert.equal(readInterfaceError(), null);
  let hits = 0;
  const off = subscribeInterfaceError(() => { hits += 1; });
  reportInterfaceError("获取模型列表", "401 unauthorized");
  assert.equal(hits, 1);
  const notice = readInterfaceError();
  assert.ok(notice);
  assert.equal(notice.label, "获取模型列表");
  assert.equal(notice.message, "401 unauthorized");
  assert.match(formatInterfaceError(notice), /获取模型列表：401 unauthorized/);
  off();
  reportInterfaceError("保存模型", "MODEL_INVALID");
  assert.equal(hits, 1, "退订后不再收通知");
  resetInterfaceError();
});

test("没有报错时日志区不画这一块", () => {
  resetInterfaceError();
  assert.equal(formatInterfaceError(null), "");
  assert.equal(formatInterfaceError(readInterfaceError()), "");
});