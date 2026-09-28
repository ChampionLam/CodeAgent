/**
 * StdioTransport — Transport backed by a child process's stdin/stdout.
 *
 * R1 enforcement: we never write anything to stdout except frames, so we
 * forward stderr to log listeners unchanged.
 *
 * The decoder is held across chunks so sticky/half packets work naturally.
 */
import { Writable } from "stream";
import { Frame } from "./protocol";
import { encodeFrame, FrameDecoder } from "./frameCodec";
import {
  CloseListener,
  FrameListener,
  LogListener,
  Transport,
} from "./transport";
import { SpawnOptions, SpawnedProcess, Spawner } from "./deps";

export interface StdioTransportOptions {
  readonly command: string;
  readonly args: readonly string[];
  readonly cwd?: string;
  readonly env?: NodeJS.ProcessEnv;
  readonly windowsHide: boolean;
  readonly spawner: Spawner;
  /**
   * If true (default), creates the child as a process-group leader on POSIX
   * so tree-kill via `process.kill(-pid)` works. No-op on Windows.
   */
  readonly detached?: boolean;
}

export class StdioTransport implements Transport {
  private readonly opts: StdioTransportOptions;
  private readonly frameListeners = new Set<FrameListener>();
  private readonly closeListeners = new Set<CloseListener>();
  private readonly logListeners = new Set<LogListener>();
  private readonly decoder = new FrameDecoder();
  private proc: SpawnedProcess | null = null;
  private _pid: number | null = null;
  private started = false;
  private closed = false;
  private _exitCode: number | null = null;
  private _exitSignal: NodeJS.Signals | null = null;
  private startResolve: (() => void) | null = null;
  private startReject: ((e: Error) => void) | null = null;

  constructor(opts: StdioTransportOptions) {
    this.opts = opts;
  }

  start(): Promise<void> {
    if (this.started) {
      return Promise.reject(new Error("StdioTransport already started"));
    }
    this.started = true;
    return new Promise<void>((resolve, reject) => {
      this.startResolve = resolve;
      this.startReject = reject;

      const spawnOpts: SpawnOptions = {
        command: this.opts.command,
        args: this.opts.args,
        cwd: this.opts.cwd,
        env: this.opts.env,
        windowsHide: this.opts.windowsHide,
      };
      let proc: SpawnedProcess;
      try {
        proc = this.opts.spawner.spawn(spawnOpts);
      } catch (e) {
        reject(e as Error);
        return;
      }
      this.proc = proc;
      this._pid = proc.pid;

      proc.on("exit", (code, signal) => {
        this._exitCode = code;
        this._exitSignal = signal;
        this.closed = true;
        for (const l of this.closeListeners) l(code, signal);
      });
      proc.on("error", (err) => {
        this.closed = true;
        if (this.startReject) {
          this.startReject(err);
          this.startResolve = null;
          this.startReject = null;
        }
        for (const l of this.closeListeners) l(null, null);
      });

      // Wire stdout -> decoder -> frame listeners. We treat ANY non-frame
      // bytes that arrive on stdout as a fatal protocol violation (R1).
      const stdout = proc.stdout;
      if (stdout) {
        stdout.on("data", (chunk: Buffer) => {
          try {
            for (const frame of this.decoder.push(chunk)) {
              for (const l of this.frameListeners) l(frame);
            }
          } catch (err) {
            this.fail((err as Error).message);
          }
        });
        stdout.on("end", () => {
          // EOF on stdout = sidecar died. Close listeners will fire on exit.
          // We don't fire them here; the `exit` handler is canonical.
        });
      }

      const stderr = proc.stderr;
      if (stderr) {
        stderr.on("data", (chunk: Buffer) => {
          const text = chunk.toString("utf-8");
          for (const line of splitLogLines(text)) {
            for (const l of this.logListeners) l("stderr", line);
          }
        });
      }

      // Process spawned successfully (even if the user has not received
      // the hello event yet). startResolve() is called by StdioTransport
      // as soon as spawn() returned a PID; readiness is the client's job.
      if (this.startResolve) {
        this.startResolve();
        this.startResolve = null;
        this.startReject = null;
      }
    });
  }

  send(frame: Frame): void {
    if (this.closed) throw new Error("transport is closed");
    if (!this.proc) throw new Error("transport not started");
    const stdin = this.proc.stdin as Writable | null;
    if (!stdin) throw new Error("child has no stdin pipe");
    const bytes = encodeFrame(frame);
    stdin.write(bytes);
  }

  onFrame(listener: FrameListener): () => void {
    this.frameListeners.add(listener);
    return () => this.frameListeners.delete(listener);
  }

  onClose(listener: CloseListener): () => void {
    this.closeListeners.add(listener);
    return () => { this.closeListeners.delete(listener); };
  }

  onLog(listener: LogListener): () => void {
    this.logListeners.add(listener);
    return () => this.logListeners.delete(listener);
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    // We intentionally don't kill the process tree here — that's the
    // client's responsibility via KillTree. Just close the pipes.
    const proc = this.proc;
    if (!proc) return;
    try { proc.stdin?.end(); } catch { /* ignore */ }
    try { proc.stdout?.destroy(); } catch { /* ignore */ }
    try { proc.stderr?.destroy(); } catch { /* ignore */ }
  }

  get pid(): number | null {
    return this._pid;
  }

  get exitCode(): number | null {
    return this._exitCode;
  }

  get exitSignal(): NodeJS.Signals | null {
    return this._exitSignal;
  }

  private fail(reason: string): void {
    if (this.closed) return;
    this.closed = true;
    if (this.startReject) {
      this.startReject(new Error(reason));
      this.startResolve = null;
      this.startReject = null;
    }
    for (const l of this.closeListeners) l(null, null);
  }
}

function splitLogLines(text: string): string[] {
  // Strip a trailing newline so we don't emit an empty line at end of buffer.
  const trimmed = text.endsWith("\n") ? text.slice(0, -1) : text;
  if (trimmed.length === 0) return [];
  return trimmed.split(/\r?\n/);
}