/**
 * 日志入口（2026-10-14）：把「日志在哪、怎么打开、为什么崩」交给界面。
 *
 * 用户口径：「程序有问题都可以找到」+「界面里要能拿到日志」。这条链上四件事
 * 都在这个模块里，全是纯逻辑（只用 path，不 import electron），所以能在
 * headless Linux 上跑单测 —— 跟 logSink.ts / deps.ts 同一条设计规则。
 *
 *   resolveLogDirs / resolveSidecarLogDir  实际生效的日志目录，不是自己拼一个
 *   openLogDir                             用系统文件管理器打开（shell.openPath，可注入假 shell）
 *   shapeStderrTail / buildLogDescribe     界面要的载荷（目录 + 侧车状态 + 输出摘要）
 *   sinkLogger                             侧车客户端日志双写（控制台 + LogSink）
 */

import * as path from "path";
import type { Logger } from "./deps";

/** 侧车 stderr 行的前缀（sidecarClient.ts 给每行加的，界面按它挑行）。 */
export const STDERR_PREFIX = "[sidecar stderr]";

/** 界面一次最多显示几行侧车输出、每行多少字（摘要，不整段灌界面）。 */
export const STDERR_SHOW_LIMIT = 8;
export const STDERR_LINE_MAX = 200;
/** 主进程内存环里能翻到多少行（够覆盖一次崩溃前后的输出）。 */
export const STDERR_SCAN_LIMIT = 50;

export type LogDirKind = "main" | "sidecar";

export interface LogDirEntry {
  kind: LogDirKind;
  /** 界面上的中文标签。 */
  label: string;
  /** 绝对路径 —— 直接显示给用户、也让他复制给别人。 */
  path: string;
}

/** 主进程日志 / 侧车日志分两处时的标签。 */
const LABEL_MAIN = "主进程日志";
const LABEL_SIDECAR = "侧车日志（Python）";
/** 两侧实际写同一个目录时（-DataDir 指到了 userData）只给一条，别摆两个同地址按钮。 */
const LABEL_SHARED = "日志目录（主进程与侧车共用）";

// ── 目录解析 ────────────────────────────────────────────────────────────
// 「以实际生效的路径为准，不要自己拼」：主进程那份直接取 LogSink 构造时用的目录；
// 侧车那份按 python/sidecar.py + logging_kit 的优先级算，主进程把同样的 env 交给
// 子进程，所以这里算出来的就是它真写的那个地方。

export interface ResolveLogDirsInput {
  /** 主进程 LogSink 实际使用的目录（main.ts 里取自 sink.logDir，不重算）。 */
  mainLogDir: string;
  /** 交给子进程的 DESK_AGENT_DATA_DIR（bootstrap 里那个值）。 */
  sidecarDataDir: string;
  /** DESK_AGENT_LOG_DIR —— 侧车最高优先级（logging_kit 的 log_dir_override）。 */
  sidecarLogDirOverride?: string | null;
  /** config.json 的 logging.logDir（第二优先级）。 */
  configLogDir?: string | null;
  /** 侧车的工作目录（相对路径按它展开；sidecar 的 cwd = 仓库根）。 */
  cwd?: string | null;
  platform?: NodeJS.Platform;
}

function nonEmpty(raw: unknown): string | null {
  const s = typeof raw === "string" ? raw.trim() : "";
  return s.length ? s : null;
}

function pathMod(platform: NodeJS.Platform): typeof path.posix {
  return platform === "win32" ? path.win32 : path.posix;
}

function absolutize(p: string, cwd: string, platform: NodeJS.Platform): string {
  const mod = pathMod(platform);
  return mod.isAbsolute(p) ? p : mod.resolve(cwd, p);
}

/** 侧车日志目录（与 sidecar.py 的 _configure_logging 同优先级）。 */
export function resolveSidecarLogDir(input: ResolveLogDirsInput): string {
  const platform = input.platform ?? process.platform;
  const cwd = nonEmpty(input.cwd) ?? process.cwd();
  const override = nonEmpty(input.sidecarLogDirOverride);
  if (override) return absolutize(override, cwd, platform);
  const fromConfig = nonEmpty(input.configLogDir);
  if (fromConfig) return absolutize(fromConfig, cwd, platform);
  return pathMod(platform).join(input.sidecarDataDir, "logs");
}

/** 两个路径是不是同一个地方（Windows 不分大小写、斜杠混用、结尾分隔符）。 */
export function samePath(a: string, b: string, platform: NodeJS.Platform = process.platform): boolean {
  const norm = (raw: string): string => {
    let s = String(raw ?? "");
    s = platform === "win32" ? s.replace(/\//g, "\\").toLowerCase() : s.replace(/\\/g, "/");
    return s.replace(/[\\/]+$/, "");
  };
  return norm(a) === norm(b);
}

/** 界面要展示的日志目录（同一个地方时只给一条）。 */
export function resolveLogDirs(input: ResolveLogDirsInput): LogDirEntry[] {
  const platform = input.platform ?? process.platform;
  const main: LogDirEntry = { kind: "main", label: LABEL_MAIN, path: input.mainLogDir };
  const sidecarPath = resolveSidecarLogDir(input);
  if (samePath(main.path, sidecarPath, platform)) {
    return [{ kind: "main", label: LABEL_SHARED, path: main.path }];
  }
  return [main, { kind: "sidecar", label: LABEL_SIDECAR, path: sidecarPath }];
}

// ── 打开日志目录 ────────────────────────────────────────────────────────

/** 注入面：主进程传 shell.openPath / fs；测试传假 shell。 */
export interface OpenLogDirDeps {
  /** 系统文件管理器打开目录。成功返回空串，失败返回错误描述（Electron shell.openPath 的契约）。 */
  openPath: (target: string) => Promise<string>;
  /** 目录是否已存在（缺省当作存在，不建目录）。 */
  exists?: (dir: string) => boolean;
  /** 目录不存在时先建出来（可选）；失败不阻断打开 —— openPath 的原因更重要。 */
  mkdir?: (dir: string) => void;
}

export interface OpenLogDirResult {
  ok: boolean;
  path?: string;
  reason?: string;
}

/**
 * 打开其中一个日志目录。kind 只认 "main" / "sidecar" —— 路径由主进程自己解析，
 * 渲染层递不进任意路径。
 */
export async function openLogDir(
  dirs: readonly LogDirEntry[],
  kind: LogDirKind,
  deps: OpenLogDirDeps,
): Promise<OpenLogDirResult> {
  let target = dirs.find((d) => d.kind === kind);
  // 共用目录时只有一条（kind=main）：两个按钮都得能用，不留死按钮。
  if (!target && dirs.length === 1) target = dirs[0];
  if (!target) return { ok: false, reason: "unknown-dir" };
  const dir = String(target.path ?? "").trim();
  if (!dir) return { ok: false, reason: "empty-path" };
  try {
    if (deps.exists && !deps.exists(dir)) deps.mkdir?.(dir);
  } catch {
    // 建不出来不挡打开：让 shell.openPath 把真实原因说出来。
  }
  try {
    const err = await deps.openPath(dir);
    return err ? { ok: false, path: dir, reason: String(err) } : { ok: true, path: dir };
  } catch (e) {
    return { ok: false, path: dir, reason: e instanceof Error ? e.message : String(e) };
  }
}

// ── 载荷 ────────────────────────────────────────────────────────────────

export interface StderrTailOptions {
  limit?: number;
  maxChars?: number;
}

/**
 * 侧车输出摘要：只认 [sidecar stderr] 行（别的日志别混进这一块）、剥前缀、
 * 去空行、只留最近 limit 条、单行超长截断。
 */
export function shapeStderrTail(lines: readonly string[], opts: StderrTailOptions = {}): string[] {
  const limit = opts.limit ?? STDERR_SHOW_LIMIT;
  const maxChars = opts.maxChars ?? STDERR_LINE_MAX;
  const out: string[] = [];
  for (const raw of lines) {
    const line = String(raw ?? "").replace(/\s+$/, "");
    if (!line.startsWith(STDERR_PREFIX)) continue;
    const body = line.slice(STDERR_PREFIX.length).trim();
    if (!body) continue;
    out.push(body.length > maxChars ? body.slice(0, maxChars) + "…" : body);
  }
  return limit > 0 ? out.slice(-limit) : [];
}

export interface LogDescribeStatus {
  state?: string;
  restartAttempts?: number;
  lastError?: { code?: string; message?: string } | null;
}

export interface BuildLogDescribeInput {
  dirs: readonly LogDirEntry[];
  /** 目录是否已生成（界面据此提示「第一次写日志时才建」）。 */
  exists?: (dir: string) => boolean;
  /** 侧车状态；client 还没起来就给 null。 */
  status?: LogDescribeStatus | null;
  /** 主进程日志里的 [sidecar stderr] 行（logSink.tailByPrefix 的结果）。 */
  stderrLines?: readonly string[];
  stderrLimit?: number;
  stderrMaxChars?: number;
}

export interface LogDescribe {
  dirs: Array<LogDirEntry & { exists: boolean }>;
  sidecar: {
    state: string;
    restartAttempts: number;
    lastError: { code: string; message: string } | null;
  } | null;
  stderrTail: string[];
}

/** 设置页日志区的一整份载荷：目录 + 侧车状态 + 输出摘要。 */
export function buildLogDescribe(input: BuildLogDescribeInput): LogDescribe {
  const exists = input.exists ?? ((): boolean => false);
  const status = input.status ?? null;
  const last = status?.lastError ?? null;
  return {
    dirs: input.dirs.map((d) => {
      let ok = false;
      try {
        ok = exists(d.path);
      } catch {
        ok = false; // 查不到就当没生成（界面只少一句提示，不该整个区报错）
      }
      return { ...d, exists: ok };
    }),
    sidecar: status
      ? {
          state: String(status.state ?? "unknown"),
          restartAttempts: Number.isFinite(status.restartAttempts) ? Number(status.restartAttempts) : 0,
          lastError: last && (last.code || last.message)
            ? { code: String(last.code ?? ""), message: String(last.message ?? "") }
            : null,
        }
      : null,
    stderrTail: shapeStderrTail(input.stderrLines ?? [], {
      limit: input.stderrLimit,
      maxChars: input.stderrMaxChars,
    }),
  };
}

// ── 侧车日志双写 ────────────────────────────────────────────────────────

/** LogSink 里被用到的部分（测试可换假 sink）。 */
export interface SinkLike {
  log(level: string, msg: string, opts?: { source?: string; data?: unknown }): void;
}

/**
 * 侧车客户端的日志原来只进控制台（[sidecar stderr] / RPC 超时 / 心跳判死全在
 * console 里），界面查不到。包一层再写进 LogSink：既落盘（agent-YYYYMMDD.log），
 * 又能被设置页的日志区取到。sink 抛错绝不能连坐原日志通道。
 */
export function sinkLogger(base: Logger, sink: SinkLike): Logger {
  const to = (level: "info" | "warn" | "error") => (msg: string): void => {
    base[level](msg);
    try {
      sink.log(level, msg, { source: "electron" });
    } catch {
      // 日志永不连坐。
    }
  };
  return { info: to("info"), warn: to("warn"), error: to("error") };
}