/**
 * logSink — Electron 侧结构化日志 + 崩溃兜底（任务 t_5dd44e42 第 4 条）。
 *
 * 与 Python 的 logging_kit.py 同构但更薄：
 *  - appendFile 到 <userData>/logs/agent-YYYYMMDD.log（JSONL，同格式，
 *    source=electron / ui），写失败静默降级 console（日志永不连坐）。
 *  - 级别：debug/info/warn/error，`logLevel()` 读 env
 *    DESK_AGENT_LOG_LEVEL（临时调试）> 传入 config > 默认 info；运行时
 *    setLevel() 可改（界面/配置入口在 main 里接）。
 *  - uncaughtException / unhandledRejection 落 crash-<ts>.log 纯文本
 *    （traceback=stack + 最近 50 条内存缓冲）。
 *  - 按天切文件；启动时清 retentionDays 前的旧文件（默认 7 天）。
 *
 * 纯 Node 依赖（fs/path/os），不 import electron —— 可在 headless Linux
 * 单测（对齐 deps.ts 的设计规则）。
 */
import { appendFile, mkdir, readdir, readFile, stat, unlink, writeFile } from "fs/promises";
import * as path from "path";

export type LogLevel = "debug" | "info" | "warn" | "error";
const ORDER: Record<LogLevel, number> = { debug: 0, info: 1, warn: 2, error: 3 };

export interface LogRecord {
  ts: string;
  level: LogLevel;
  source: string;
  msg: string;
  run_id?: string;
  session_id?: string;
  data?: unknown;
}

/** 依赖注入面（测试可换假 fs）。 */
export interface LogSinkDeps {
  readonly appendFile: (p: string, s: string) => Promise<void>;
  readonly writeFile: (p: string, s: string) => Promise<void>;
  readonly readFile: (p: string) => Promise<string>;
  readonly mkdir: (p: string) => Promise<void>;
  readdir: (p: string) => Promise<string[]>;
  stat: (p: string) => Promise<{ mtimeMs: number; size: number }>;
  unlink: (p: string) => Promise<void>;
}

export const realLogDeps: LogSinkDeps = {
  appendFile: (p, s) => appendFile(p, s, "utf8"),
  writeFile: async (p, s) => void (await writeFile(p, s, "utf8")),
  readFile: (p) => readFile(p, "utf8"),
  mkdir: async (p) => void (await mkdir(p, { recursive: true })),
  readdir: (p) => readdir(p),
  stat: async (p) => stat(p),
  unlink: (p: string) => unlink(p).then(() => undefined),
};

const RETENTION_DAYS_DEFAULT = 7;
const CRASH_KEEP = 10;
const TAIL_LINES = 50;
const MAX_DATA_JSON = 2048;

function isoNow(): string {
  return new Date().toISOString();
}

function dayStamp(d = new Date()): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}${m}${day}`;
}

function normalizeLevel(raw: string | undefined): LogLevel | null {
  if (!raw) return null;
  const lv = raw.trim().toLowerCase() === "warning" ? "warn" : raw.trim().toLowerCase();
  return (lv in ORDER ? lv : null) as LogLevel | null;
}

export class LogSink {
  private level: LogLevel;
  private readonly ring: LogRecord[] = [];
  private pending: Promise<unknown> = Promise.resolve();
  private swept = false;

  constructor(
    private readonly logDir: string,
    deps: LogSinkDeps,
    level: LogLevel = "info",
    private readonly retentionDays = RETENTION_DAYS_DEFAULT,
  ) {
    this.level = level;
    this.deps = deps;
  }
  private readonly deps: LogSinkDeps;

  static resolveLevel(envLevel: string | undefined, configLevel?: unknown): LogLevel {
    return (
      normalizeLevel(envLevel) ??
      normalizeLevel(typeof configLevel === "string" ? configLevel : undefined) ??
      "info"
    );
  }

  setLevel(level: string): boolean {
    const lv = normalizeLevel(level);
    if (!lv) return false;
    this.level = lv;
    return true;
  }

  currentLevel(): LogLevel {
    return this.level;
  }

  log(level: string, msg: string, opts: { source?: string; runId?: string; sessionId?: string; data?: unknown } = {}): void {
    const lv = normalizeLevel(level) ?? "info";
    const rec: LogRecord = {
      ts: isoNow(),
      level: lv,
      source: opts.source ?? "electron",
      msg: String(msg ?? ""),
    };
    if (opts.runId) rec.run_id = String(opts.runId);
    if (opts.sessionId) rec.session_id = String(opts.sessionId);
    if (opts.data !== undefined) rec.data = truncateData(opts.data);
    this.ring.push(rec);
    if (this.ring.length > 2000) this.ring.shift();
    if (ORDER[lv] < ORDER[this.level]) return;
    // 串行化写，避免并发 append 交错；任何失败静默吞。
    this.pending = this.pending.then(() => this.writeLine(rec)).catch(() => undefined);
  }

  debug(msg: string, opts?: Parameters<LogSink["log"]>[2]): void { this.log("debug", msg, opts); }
  info(msg: string, opts?: Parameters<LogSink["log"]>[2]): void { this.log("info", msg, opts); }
  warn(msg: string, opts?: Parameters<LogSink["log"]>[2]): void { this.log("warn", msg, opts); }
  error(msg: string, opts?: Parameters<LogSink["log"]>[2]): void { this.log("error", msg, opts); }

  private async writeLine(rec: LogRecord): Promise<void> {
    await this.deps.mkdir(this.logDir).catch(() => undefined);
    if (!this.swept) {
      this.swept = true;
      await this.sweepStale().catch(() => undefined);
    }
    const file = path.join(this.logDir, `agent-${dayStamp()}.log`);
    await this.deps.appendFile(file, JSON.stringify(rec) + "\n");
  }

  /** 串行写入 flush（测试/关程序前用）。 */
  async flush(): Promise<void> {
    await this.pending;
  }

  private async sweepStale(): Promise<void> {
    const cutoff = Date.now() - this.retentionDays * 86400_000;
    const names = await this.deps.readdir(this.logDir);
    const crashes: [string, number][] = [];
    for (const n of names) {
      const p = path.join(this.logDir, n);
      if (n.startsWith("agent-")) {
        const st = await this.deps.stat(p).catch(() => null);
        if (st && st.mtimeMs < cutoff) await this.deps.unlink(p).catch(() => undefined);
      } else if (n.startsWith("crash-")) {
        const m = /crash-(\d{8}-\d{6})\.log$/.exec(n);
        crashes.push([p, m ? Number(m[1].replace(/-/g, "")) : 0]);
      }
    }
    crashes.sort((a, b) => a[1] - b[1]);
    while (crashes.length > CRASH_KEEP) {
      const [p] = crashes.shift()!;
      await this.deps.unlink(p).catch(() => undefined);
    }
  }

  /** 崩溃兜底：traceback/堆栈 + 最近 N 条日志写 crash-<ts>.log。返回路径。 */
  async crashDump(title: string, stack: string, opts: { source?: string; runId?: string } = {}): Promise<string | null> {
    const ts = dayStamp() + "-" + new Date().toTimeString().slice(0, 8).replace(/:/g, "");
    const lines: string[] = [
      `# CodeAgent crash ${isoNow()}`,
      `# source=${opts.source ?? "electron"} run_id=${opts.runId ?? "-"} session_id=-`,
      `# title: ${title}`,
      "",
      stack || "(no stack)",
      "",
      `---- recent log tail (${Math.min(this.ring.length, TAIL_LINES)} entries) ----`,
      ...this.ring.slice(-TAIL_LINES).map((r) => JSON.stringify(r)),
    ];
    const body = lines.join("\n") + "\n";
    try {
      await this.deps.mkdir(this.logDir);
      const p = path.join(this.logDir, `crash-${ts}.log`);
      await this.deps.writeFile(p, body);
      this.log("error", `crash: ${title}`, { source: opts.source ?? "electron", data: { stack: stack?.slice(0, MAX_DATA_JSON) } });
      return p;
    } catch {
      console.error("[logSink] log dir unwritable; crash to stderr:\n" + body);
      return null;
    }
  }
}

function truncateData(data: unknown): unknown {
  let s: string;
  try {
    s = JSON.stringify(data) ?? "null";
  } catch {
    return { _unserializable: true };
  }
  return s.length <= MAX_DATA_JSON ? data : { _truncated: true, head: s.slice(0, MAX_DATA_JSON) };
}

/**
 * 挂进程崩溃兜底（main.ts 里调用一次）。
 * 返回的 handlers 不自动接管——由调用方决定 uncaughtException 后的退出策略
 * （对齐设计文档：崩了要让 sidecar 自动重启逻辑接管，main 不无限自救）。
 */
export function crashGuards(
  sink: LogSink,
  onCrash: (title: string, err: Error) => void = () => undefined,
): () => void {
  const handle = (origin: string) => (err: unknown) => {
    const e = err instanceof Error ? err : new Error(String(err));
    void sink.crashDump(`uncaught (${origin}): ${e.name}`, e.stack ?? String(e)).finally(() =>
      onCrash(`uncaught (${origin}): ${e.name}`, e),
    );
  };
  const onUncaught = handle("uncaughtException");
  const onRejection = handle("unhandledRejection");
  process.on("uncaughtException", onUncaught);
  process.on("unhandledRejection", onRejection);
  return () => {
    process.off("uncaughtException", onUncaught);
    process.off("unhandledRejection", onRejection);
  };
}
