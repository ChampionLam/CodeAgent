import { test } from "node:test";
import assert from "node:assert/strict";
import { LogSink, crashGuards, type LogSinkDeps } from "../electron/logSink.ts";
import * as os from "os";
import * as fs from "fs";
import * as path from "path";

function sandboxDeps(): { deps: LogSinkDeps; dir: string } {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "logsink-"));
  const deps: LogSinkDeps = {
    appendFile: async (p, s) => void fs.appendFileSync(p, s),
    writeFile: async (p, s) => void fs.writeFileSync(p, s),
    readFile: async (p) => fs.readFileSync(p, "utf8"),
    mkdir: async (p) => void fs.mkdirSync(p, { recursive: true }),
    readdir: async (p) => fs.readdirSync(p),
    stat: async (p) => {
      const st = fs.statSync(p);
      return { mtimeMs: st.mtimeMs, size: st.size };
    },
    unlink: async (p) => void fs.unlinkSync(p),
  };
  return { deps, dir };
}

function todayFile(dir: string): string {
  const d = new Date();
  const stamp = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
  return path.join(dir, `agent-${stamp}.log`);
}

test("record fields structured (ts/level/source/run_id/session_id/data)", async () => {
  const { deps, dir } = sandboxDeps();
  const sink = new LogSink(dir, deps, "debug");
  sink.log("info", "hello", { source: "ui", runId: "r1", sessionId: "s1", data: { tool: "read_file" } });
  await sink.flush();
  const rec = JSON.parse(fs.readFileSync(todayFile(dir), "utf8").trim());
  assert.equal(rec.level, "info");
  assert.equal(rec.source, "ui");
  assert.equal(rec.run_id, "r1");
  assert.equal(rec.session_id, "s1");
  assert.deepEqual(rec.data, { tool: "read_file" });
  assert.match(rec.ts, /^\d{4}-\d{2}-\d{2}T.*Z$/);
});

test("level filtering + runtime setLevel", async () => {
  const { deps, dir } = sandboxDeps();
  const sink = new LogSink(dir, deps, "info");
  sink.debug("hidden");
  sink.info("kept");
  assert.equal(sink.setLevel("warn"), true);
  sink.info("also-hidden");
  sink.warn("kept-warn");
  assert.equal(sink.setLevel("bogus"), false);
  await sink.flush();
  const recs = fs.readFileSync(todayFile(dir), "utf8").trim().split("\n").map((l) => JSON.parse(l));
  assert.deepEqual(recs.map((r) => r.level), ["info", "warn"]);
});

test("crashDump writes file with stack + tail, and error record", async () => {
  const { deps, dir } = sandboxDeps();
  const sink = new LogSink(dir, deps, "info");
  sink.info("before crash");
  const p = await sink.crashDump("test crash", "Error: Boom\n    at x (y.ts:1)");
  assert.ok(p && fs.existsSync(p));
  const body = fs.readFileSync(p!, "utf8");
  assert.ok(body.includes("Error: Boom"));
  assert.ok(body.includes("recent log tail"));
  assert.ok(body.includes("before crash"));
  await sink.flush();
  const recs = fs.readFileSync(todayFile(dir), "utf8").trim().split("\n").map((l) => JSON.parse(l));
  assert.ok(recs.some((r) => r.level === "error" && r.msg.includes("crash")));
});

test("log never throws when dir unwritable", async () => {
  const { deps, dir } = sandboxDeps();
  const blocked = path.join(dir, "blocked");
  fs.writeFileSync(blocked, "i am a file");
  const sink = new LogSink(path.join(blocked, "logs"), deps, "info");
  sink.info("must not throw"); // silent
  const p = await sink.crashDump("t", "stack");
  assert.equal(p, null); // falls back to console (already spammed); no throw
});

test("retention sweep removes old files, caps crashes", async () => {
  const { deps, dir } = sandboxDeps();
  fs.mkdirSync(dir, { recursive: true });
  const old = path.join(dir, "agent-19990101.log");
  fs.writeFileSync(old, "stale\n");
  fs.utimesSync(old, new Date(Date.now() - 30 * 86400_000), new Date(Date.now() - 30 * 86400_000));
  for (let i = 0; i < 15; i++) fs.writeFileSync(path.join(dir, `crash-20250101-${String(i).padStart(6, "0")}.log`), "c");
  const sink = new LogSink(dir, deps, "info");
  sink.info("trigger sweep");
  await sink.flush();
  assert.ok(!fs.existsSync(old));
  const crashes = fs.readdirSync(dir).filter((n) => n.startsWith("crash-"));
  assert.ok(crashes.length <= 10);
});

test("crashGuards dumps on unhandled rejection callback", async () => {
  const { deps, dir } = sandboxDeps();
  const sink = new LogSink(dir, deps, "info");
  // 捕获注册的监听器而不是真触发 process 级事件（node test runner 会把
  // 真 unhandledRejection 当测试失败）。这仍验证 crashGuards 的接线逻辑。
  const handlers = new Map<string, (err: unknown) => void>();
  const origOn = process.on.bind(process);
  const origOff = process.off.bind(process);
  (process as unknown as { on: (n: string, f: (e: unknown) => void) => typeof process }).on =
    (name: string, f: (e: unknown) => void) => { handlers.set(name, f); return process as unknown as typeof import("process"); };
  (process as unknown as { off: (n: string, f: unknown) => typeof process }).off = () => process as unknown as typeof import("process");
  try {
    let fired = false;
    const off = crashGuards(sink, () => { fired = true; });
    handlers.get("unhandledRejection")!(new Error("rejected-xyz"));
    for (let i = 0; i < 50 && !fired; i++) await new Promise((r) => setTimeout(r, 10));
    off();
    assert.ok(fired);
    await sink.flush();
    const crashes = fs.readdirSync(dir).filter((n) => n.startsWith("crash-"));
    assert.equal(crashes.length, 1);
    assert.ok(fs.readFileSync(path.join(dir, crashes[0]), "utf8").includes("rejected-xyz"));
  } finally {
    (process as unknown as { on: typeof origOn }).on = origOn;
    (process as unknown as { off: typeof origOff }).off = origOff;
  }
});

test("resolveLevel: env > config > default; warning alias", () => {
  assert.equal(LogSink.resolveLevel("debug", "warn"), "debug");
  assert.equal(LogSink.resolveLevel(undefined, "warn"), "warn");
  assert.equal(LogSink.resolveLevel(undefined, undefined), "info");
  assert.equal(LogSink.resolveLevel("WARNING"), "warn");
  assert.equal(LogSink.resolveLevel(undefined, "garbage"), "info");
});
