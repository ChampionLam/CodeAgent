/**
 * All injectable dependencies for the SidecarClient and its transports.
 *
 * Production code (electron/main.ts) wires these to Node's child_process,
 * fs, and process.platform. Tests pass fakes.
 *
 * Why a bag of deps rather than direct imports? Two reasons:
 *   1. The Electron main module pulls in `electron` itself, which cannot
 *      load on a headless machine. By funneling all native side effects
 *      through this deps object, the *rest* of the codebase is pure and
 *      unit-testable on a Linux box with no DISPLAY.
 *   2. Tests can inject a FakeChildProcess / FakeFS / FakeClock and verify
 *      behaviors that would otherwise require a real spawn + tree-kill.
 */

import { ChildProcess } from "child_process";
import { Readable, Writable } from "stream";

export interface SpawnOptions {
  readonly command: string;
  readonly args: readonly string[];
  readonly cwd?: string;
  readonly env?: NodeJS.ProcessEnv;
  readonly windowsHide: boolean;
}

export interface SpawnedProcess {
  readonly pid: number;
  readonly stdin: Writable | null;
  readonly stdout: Readable | null;
  readonly stderr: Readable | null;
  on(event: "exit", listener: (code: number | null, signal: NodeJS.Signals | null) => void): this;
  on(event: "error", listener: (err: Error) => void): this;
  once(event: "exit", listener: (code: number | null, signal: NodeJS.Signals | null) => void): this;
  once(event: "error", listener: (err: Error) => void): this;
  kill(signal?: NodeJS.Signals | number): boolean;
}

export interface Spawner {
  spawn(opts: SpawnOptions): SpawnedProcess;
}

/**
 * Kill a process tree rooted at pid. On Windows this issues `taskkill /T /F`.
 * On POSIX it sends SIGTERM/SIGKILL to the negative-pid process group.
 *
 * Resolves once the operation has been *dispatched*; callers don't wait
 * for the child to actually die — that is reported via the child's `exit`
 * event.
 *
 * Modeled as a flat callable (not a nested object) so call sites read as
 * `deps.killTree(pid)` and tests can inject either a plain function or a
 * wrapper carrying bookkeeping state via `Object.assign`.
 */
export type KillTree = (pid: number) => Promise<void> | void;

export interface FileSystem {
  exists(path: string): boolean;
  readFile(path: string): string | null;
  writeFile(path: string, content: string): void;
  unlink(path: string): void;
}

export interface Clock {
  now(): number;
  setTimeout(fn: () => void, ms: number): NodeJS.Timeout;
  clearTimeout(handle: NodeJS.Timeout): void;
  setInterval(fn: () => void, ms: number): number;
  clearInterval(handle: number): void;
}

export interface Logger {
  info(msg: string): void;
  warn(msg: string): void;
  error(msg: string): void;
}

export interface RuntimeDeps {
  readonly spawner: Spawner;
  readonly killTree: KillTree;
  readonly fs: FileSystem;
  readonly clock: Clock;
  readonly logger: Logger;
  readonly platform: NodeJS.Platform;
}

/* ------------------------------------------------------------------ */
/* Real implementations. They are the defaults injected into the main  */
/* entrypoint; tests can replace any subset.                            */
/* ------------------------------------------------------------------ */

import { spawn as cpSpawn } from "child_process";

export const realSpawner: Spawner = {
  spawn(opts): SpawnedProcess {
    const cp: ChildProcess = cpSpawn(opts.command, [...opts.args], {
      cwd: opts.cwd,
      env: opts.env,
      windowsHide: opts.windowsHide,
      stdio: ["pipe", "pipe", "pipe"],
    });
    return cp as unknown as SpawnedProcess;
  },
};

export const realKillTree: KillTree = async (pid: number): Promise<void> => {
  if (process.platform === "win32") {
    const { spawn } = await import("child_process");
    await new Promise<void>((resolve) => {
      const tk = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true });
      tk.on("exit", () => resolve());
      tk.on("error", () => resolve());
    });
    return;
  }
  // POSIX: signal the process group created via detached: true upstream.
  try {
    process.kill(-pid, "SIGTERM");
  } catch {
    try { process.kill(pid, "SIGTERM"); } catch { /* already dead */ }
  }
  // Hard-kill fallback after 1 s. We do NOT await — caller continues.
  setTimeout(() => {
    try { process.kill(-pid, "SIGKILL"); } catch { /* ignore */ }
  }, 1000).unref();
};

export const realFs: FileSystem = {
  exists(p) {
    try { return require("fs").existsSync(p); } catch { return false; }
  },
  readFile(p) {
    try { return require("fs").readFileSync(p, "utf-8"); } catch { return null; }
  },
  writeFile(p, c) {
    try { require("fs").writeFileSync(p, c); } catch { /* ignore */ }
  },
  unlink(p) {
    try { require("fs").unlinkSync(p); } catch { /* ignore */ }
  },
};

export const realClock: Clock = {
  now: () => Date.now(),
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (h) => clearTimeout(h),
  // Node's setInterval returns a Timeout object, but the Clock interface
  // (and the FakeClock in tests) exposes a numeric handle. Cast to keep
  // the contract uniform across real + fake clocks.
  setInterval: (fn, ms) => setInterval(fn, ms) as unknown as number,
  clearInterval: (h) => clearInterval(h as unknown as NodeJS.Timeout),
};

export const consoleLogger: Logger = {
  info: (m) => console.log(`[sidecar-client] ${m}`),
  warn: (m) => console.warn(`[sidecar-client] ${m}`),
  error: (m) => console.error(`[sidecar-client] ${m}`),
};

/** Build the default dependency bundle for production. */
export function defaultDeps(): RuntimeDeps {
  return {
    spawner: realSpawner,
    killTree: realKillTree,
    fs: realFs,
    clock: realClock,
    logger: consoleLogger,
    platform: process.platform,
  };
}