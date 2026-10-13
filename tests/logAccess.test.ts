/**
 * 日志入口（2026-10-14）：目录解析、打开、摘要、侧车日志双写。
 *
 * 用户口径：「程序有问题都可以找到」「界面里要能拿到日志」。这条链上三件事
 * 必须钉死：目录是不是**实际生效**的那个、打开失败有没有原因、侧车崩溃前
 * 最后写了什么。
 *
 * 修复前：electron/logAccess.ts 不存在 —— 界面没有任何「日志在哪、怎么打开、
 * 为什么崩」的入口，侧车的 stderr 只进控制台；LogSink 也没有 tailByPrefix。
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  STDERR_PREFIX,
  buildLogDescribe,
  openLogDir,
  resolveLogDirs,
  resolveSidecarLogDir,
  samePath,
  shapeStderrTail,
  sinkLogger,
} from "../electron/logAccess.ts";
import { LogSink, type LogSinkDeps, type LogLevel } from "../electron/logSink.ts";

const LINUX = "linux" as const;
const WIN = "win32" as const;

/** 两个真实形状的目录：主进程日志走 userData，侧车日志走数据目录。 */
const WIN_DIRS = resolveLogDirs({
  mainLogDir: "C:\\Users\\me\\AppData\\Roaming\\CodeAgent\\logs",
  sidecarDataDir: "C:\\Install\\CodeAgent\\data",
  platform: WIN,
});

/** 双写用例只看内存环，落盘全丢。 */
const NOOP_SINK_DEPS: LogSinkDeps = {
  appendFile: async (): Promise<void> => undefined,
  writeFile: async (): Promise<void> => undefined,
  readFile: async (): Promise<string> => "",
  mkdir: async (): Promise<void> => undefined,
  readdir: async (): Promise<string[]> => [],
  stat: async (): Promise<{ mtimeMs: number; size: number }> => ({ mtimeMs: 0, size: 0 }),
  unlink: async (): Promise<void> => undefined,
};

function memoryLogger(): { seen: string[]; logger: { info: (m: string) => void; warn: (m: string) => void; error: (m: string) => void } } {
  const seen: string[] = [];
  return {
    seen,
    logger: {
      info: (m: string): void => { seen.push("info:" + m); },
      warn: (m: string): void => { seen.push("warn:" + m); },
      error: (m: string): void => { seen.push("error:" + m); },
    },
  };
}

/* ── 目录解析 ────────────────────────────────────────────────────────────── */

test("两个目录：主进程日志是 LogSink 用的那个，侧车日志是 <数据目录>\\logs", () => {
  assert.deepEqual(WIN_DIRS, [
    {
      kind: "main",
      label: "主进程日志",
      path: "C:\\Users\\me\\AppData\\Roaming\\CodeAgent\\logs",
    },
    {
      kind: "sidecar",
      label: "侧车日志（Python）",
      path: "C:\\Install\\CodeAgent\\data\\logs",
    },
  ]);
});

test("侧车目录优先级与 sidecar.py 一致：DESK_AGENT_LOG_DIR > config.logging.logDir > 数据目录\\logs", () => {
  const base = {
    mainLogDir: "/home/me/.config/CodeAgent/logs",
    sidecarDataDir: "/data",
    platform: LINUX,
  };
  assert.equal(
    resolveSidecarLogDir({ ...base, sidecarLogDirOverride: "/var/log/ca", configLogDir: "/cfg/logs" }),
    "/var/log/ca",
  );
  assert.equal(
    resolveSidecarLogDir({ ...base, sidecarLogDirOverride: "   ", configLogDir: "/cfg/logs" }),
    "/cfg/logs",
  );
  assert.equal(resolveSidecarLogDir(base), "/data/logs");
});

test("相对路径按侧车的工作目录展开（sidecar 的 cwd = 仓库根，跟 python 的语义一致）", () => {
  assert.equal(
    resolveSidecarLogDir({
      mainLogDir: "/u/logs", sidecarDataDir: "/d", configLogDir: "logs2", cwd: "/app", platform: LINUX,
    }),
    "/app/logs2",
  );
  assert.equal(
    resolveSidecarLogDir({
      mainLogDir: "C:\\u\\logs", sidecarDataDir: "C:\\d", sidecarLogDirOverride: "mylogs",
      cwd: "C:\\app", platform: WIN,
    }),
    "C:\\app\\mylogs",
  );
});

test("两个目录实际是同一个时只给一条 —— 别让界面摆两个开同一个地方的按钮", () => {
  const dirs = resolveLogDirs({
    mainLogDir: "C:\\App\\data\\logs",
    sidecarDataDir: "C:\\App\\data",
    platform: WIN,
  });
  assert.equal(dirs.length, 1);
  assert.match(dirs[0].label, /共用/);
  assert.equal(dirs[0].path, "C:\\App\\data\\logs");
});

test("samePath：Windows 不分大小写、斜杠与结尾分隔符；Linux 区分大小写", () => {
  assert.equal(samePath("C:\\A\\logs", "c:/a/logs/", WIN), true);
  assert.equal(samePath("C:\\A\\logs", "C:\\A\\logs2", WIN), false);
  assert.equal(samePath("/a/logs/", "/a/logs", LINUX), true);
  assert.equal(samePath("/a/logs", "/A/logs", LINUX), false);
});

/* ── 打开日志目录（注入假 shell）──────────────────────────────────────────── */

function fakeShell(error = ""): { calls: string[]; deps: { openPath: (p: string) => Promise<string> } } {
  const calls: string[] = [];
  return {
    calls,
    deps: {
      openPath: async (p: string): Promise<string> => {
        calls.push(p);
        return error;
      },
    },
  };
}

test("打开日志目录：走 shell.openPath，成功把真实路径回给界面", async () => {
  const sh = fakeShell();
  const r = await openLogDir(WIN_DIRS, "sidecar", sh.deps);
  assert.deepEqual(r, { ok: true, path: "C:\\Install\\CodeAgent\\data\\logs" });
  assert.deepEqual(sh.calls, ["C:\\Install\\CodeAgent\\data\\logs"]);
});

test("打开日志目录：系统报错时原样带出原因（不是静默失败）", async () => {
  const sh = fakeShell("Failed to open path");
  const r = await openLogDir(WIN_DIRS, "main", sh.deps);
  assert.equal(r.ok, false);
  assert.equal(r.reason, "Failed to open path");
});

test("打开日志目录：kind 不认识就不开 —— 渲染层递不进来任意路径", async () => {
  const sh = fakeShell();
  const r = await openLogDir(WIN_DIRS, "C:\\Windows" as never, sh.deps);
  assert.equal(r.ok, false);
  assert.equal(r.reason, "unknown-dir");
  assert.deepEqual(sh.calls, []);
});

test("目录还没生成：先建出来再打开（否则文件管理器直接报错）", async () => {
  const sh = fakeShell();
  const made: string[] = [];
  const r = await openLogDir(WIN_DIRS, "sidecar", {
    openPath: sh.deps.openPath,
    exists: () => false,
    mkdir: (d: string) => made.push(d),
  });
  assert.equal(r.ok, true);
  assert.deepEqual(made, ["C:\\Install\\CodeAgent\\data\\logs"]);
});

test("建目录失败不挡打开：openPath 的原因照样回给界面", async () => {
  const sh = fakeShell("EACCES");
  const r = await openLogDir(WIN_DIRS, "main", {
    openPath: sh.deps.openPath,
    exists: () => false,
    mkdir: () => { throw new Error("EPERM"); },
  });
  assert.equal(r.ok, false);
  assert.equal(r.reason, "EACCES");
});

test("两个 kind 共用一个目录时，两个都能打开它（不留死按钮）", async () => {
  const dirs = resolveLogDirs({ mainLogDir: "/d/logs", sidecarDataDir: "/d", platform: LINUX });
  const sh = fakeShell();
  assert.equal((await openLogDir(dirs, "sidecar", sh.deps)).path, "/d/logs");
  assert.equal(sh.calls.length, 1);
});

/* ── 侧车输出摘要 ────────────────────────────────────────────────────────── */

test("摘要只认 [sidecar stderr] 行，别的日志不混进「侧车输出」块", () => {
  assert.deepEqual(
    shapeStderrTail(["RPC timeout: request 'models.fetch'", `${STDERR_PREFIX} boom`]),
    ["boom"],
  );
});

test("摘要取最近 N 条（正序）、单行超长截断", () => {
  const lines = Array.from({ length: 12 }, (_, i) => `${STDERR_PREFIX} line-${i}`);
  assert.deepEqual(shapeStderrTail(lines, { limit: 3 }), ["line-9", "line-10", "line-11"]);
  const [one] = shapeStderrTail([`${STDERR_PREFIX} ` + "x".repeat(500)], { limit: 1, maxChars: 40 });
  assert.equal(one.length, 41);
  assert.ok(one.endsWith("…"), one);
});

test("摘要：空输入与纯空白行得到空数组（界面不画空块）", () => {
  assert.deepEqual(shapeStderrTail([]), []);
  assert.deepEqual(shapeStderrTail([`${STDERR_PREFIX}    `]), []);
});

/* ── 载荷 ──────────────────────────────────────────────────────────────── */

test("日志区载荷：目录 + 是否已生成 + 侧车状态 + 摘要", () => {
  const payload = buildLogDescribe({
    dirs: WIN_DIRS,
    exists: (d: string): boolean => d.includes("Roaming"),
    status: {
      state: "crashed",
      restartAttempts: 3,
      lastError: { code: "SIDECAR_DEAD", message: "max restarts exhausted" },
    },
    stderrLines: [`${STDERR_PREFIX} Traceback (most recent call last):`, `${STDERR_PREFIX} ValueError: boom`],
  });
  assert.equal(payload.dirs.length, 2);
  assert.equal(payload.dirs[0].exists, true);
  assert.equal(payload.dirs[1].exists, false);
  assert.deepEqual(payload.sidecar, {
    state: "crashed",
    restartAttempts: 3,
    lastError: { code: "SIDECAR_DEAD", message: "max restarts exhausted" },
  });
  assert.deepEqual(payload.stderrTail, ["Traceback (most recent call last):", "ValueError: boom"]);
});

test("日志区载荷：客户端还没起来（状态为空）也不崩，lastError 归一成 null", () => {
  const p = buildLogDescribe({ dirs: WIN_DIRS, exists: () => false, status: null, stderrLines: [] });
  assert.equal(p.sidecar, null);
  assert.deepEqual(p.stderrTail, []);
});

test("日志区载荷：状态字段缺省补齐（restartAttempts 缺省 0）", () => {
  const p = buildLogDescribe({ dirs: WIN_DIRS, exists: () => false, status: { state: "ready" }, stderrLines: [] });
  assert.deepEqual(p.sidecar, { state: "ready", restartAttempts: 0, lastError: null });
});

/* ── 侧车日志双写 ────────────────────────────────────────────────────────── */

test("侧车日志双写：stderr 行既进控制台也进主进程日志（以前只进控制台，界面查不到）", () => {
  const { seen, logger: base } = memoryLogger();
  const sink = new LogSink("/tmp/unused-logAccess", NOOP_SINK_DEPS, "info");
  const logger = sinkLogger(base, sink);
  logger.info(`${STDERR_PREFIX} Traceback (most recent call last):`);
  logger.warn("RPC timeout: request 'models.fetch' timed out");
  assert.deepEqual(seen, [
    `info:${STDERR_PREFIX} Traceback (most recent call last):`,
    "warn:RPC timeout: request 'models.fetch' timed out",
  ]);
  assert.deepEqual(sink.tailByPrefix(STDERR_PREFIX, 5), [`${STDERR_PREFIX} Traceback (most recent call last):`]);
  assert.deepEqual(sink.tailByPrefix("RPC timeout", 5), ["RPC timeout: request 'models.fetch' timed out"]);
});

test("侧车日志双写：落盘通道抛错也不影响原日志（日志永不连坐）", () => {
  const { seen, logger: base } = memoryLogger();
  const broken = { log: (): void => { throw new Error("sink down"); } };
  const logger = sinkLogger(base, broken as unknown as LogSink);
  logger.info("still delivered");
  assert.deepEqual(seen, ["info:still delivered"]);
});

test("侧车日志双写：级别原样透传（warn/error 不降成 info）", () => {
  const levels: string[] = [];
  const sink = { log: (lv: string): void => { levels.push(lv); } };
  const logger = sinkLogger(
    { info: (): void => undefined, warn: (): void => undefined, error: (): void => undefined },
    sink as unknown as LogSink,
  );
  logger.info("a");
  logger.warn("b");
  logger.error("c");
  assert.deepEqual(levels, ["info", "warn", "error"] satisfies LogLevel[]);
});