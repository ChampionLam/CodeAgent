/**
 * 日志区要用的 LogSink 取数能力（2026-10-14）。
 *
 * 修复前：LogSink 只写盘 + 内存环自己用（crashDump），外面既拿不到「实际生效的
 * 日志目录」，也拿不到「侧车最后说了什么」。界面上的日志入口因此无处取数。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { LogSink, type LogSinkDeps } from "../electron/logSink.ts";

function sandbox(): { deps: LogSinkDeps; dir: string } {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "logsink-tail-"));
  const deps: LogSinkDeps = {
    appendFile: async (p: string, s: string): Promise<void> => void fs.appendFileSync(p, s),
    writeFile: async (p: string, s: string): Promise<void> => void fs.writeFileSync(p, s),
    readFile: async (p: string): Promise<string> => fs.readFileSync(p, "utf8"),
    mkdir: async (p: string): Promise<void> => void fs.mkdirSync(p, { recursive: true }),
    readdir: async (p: string): Promise<string[]> => fs.readdirSync(p),
    stat: async (p: string): Promise<{ mtimeMs: number; size: number }> => {
      const st = fs.statSync(p);
      return { mtimeMs: st.mtimeMs, size: st.size };
    },
    unlink: async (p: string): Promise<void> => void fs.unlinkSync(p),
  };
  return { deps, dir };
}

function todayFile(dir: string): string {
  const d = new Date();
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  return path.join(dir, `agent-${stamp}.log`);
}

test("logDir 就是实际写入的目录（界面显示的路径不另算一份）", async () => {
  const { deps, dir } = sandbox();
  const logs = path.join(dir, "logs");
  const sink = new LogSink(logs, deps, "info");
  assert.equal(sink.logDir, logs);
  sink.info("x");
  await sink.flush(); // 落盘是串行异步的：不 flush 就看不到文件
  assert.ok(fs.existsSync(todayFile(logs)), "日志确实落在这个目录");
});

test("tailByPrefix：只取指定前缀、按时间正序、取最近 N 条", () => {
  const { deps, dir } = sandbox();
  const sink = new LogSink(dir, deps, "info");
  sink.info("bootstrap: starting sidecar");
  for (let i = 0; i < 5; i++) sink.info(`[sidecar stderr] crash-line-${i}`);
  sink.warn("sidecar transport closed code=1 signal=null");
  sink.info("[sidecar stderr] crash-line-5");
  assert.deepEqual(sink.tailByPrefix("[sidecar stderr]", 3), [
    "[sidecar stderr] crash-line-3",
    "[sidecar stderr] crash-line-4",
    "[sidecar stderr] crash-line-5",
  ]);
  assert.deepEqual(sink.tailByPrefix("sidecar transport closed", 10), ["sidecar transport closed code=1 signal=null"]);
  assert.deepEqual(sink.tailByPrefix("never-logged", 10), []);
  assert.deepEqual(sink.tailByPrefix("[sidecar stderr]", 0), []);
});

test("tailByPrefix 不受落盘级别过滤影响：级别调到 error，侧车崩溃前那些 info 行照样能看见", () => {
  const { deps, dir } = sandbox();
  const sink = new LogSink(dir, deps, "error");
  sink.info("[sidecar stderr] Traceback (most recent call last):");
  sink.info("[sidecar stderr] ImportError: No module named 'mcp'");
  assert.deepEqual(sink.tailByPrefix("[sidecar stderr]", 5), [
    "[sidecar stderr] Traceback (most recent call last):",
    "[sidecar stderr] ImportError: No module named 'mcp'",
  ]);
  // 落盘侧确实被级别挡掉了（这条用例证明两件事互不牵连）
  assert.ok(!fs.existsSync(todayFile(dir)), "低于级别的行不落盘");
});