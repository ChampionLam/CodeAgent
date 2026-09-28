/**
 * SidecarClient — owns the sidecar process and exposes a tiny RPC surface.
 *
 * Responsibilities:
 *   - Spawn the sidecar via a Transport (stdio for now).
 *   - Instance mutex before every spawn: if the pid file names a LIVE
 *     sidecar this client did not spawn, refuse to start a second writer
 *     for the same data dir (two writers would corrupt sessions.db);
 *     stale/own leftovers are killed and cleaned instead.
 *   - Run the LSP-style handshake (wait for hello, validate protocolVersion).
 *     Handshake timeout is 45 seconds because Python cold-start on Windows
 *     can take that long; UI must show "starting" meanwhile.
 *   - Pair request/response frames by `id` with a configurable timeout.
 *   - Send periodic `ping` heartbeats; if no `pong` arrives in time OR the
 *     transport closes, mark the sidecar as dead and restart.
 *   - Cap restarts (default 3) before giving up; status becomes "crashed"
 *     and we stop trying.
 *   - Surface a status object that the renderer can subscribe to.
 *
 * Design rule: this module never imports `electron`. All side effects go
 * through `deps`, so unit tests run on a headless Linux box.
 */
import { randomUUID } from "crypto";
import {
  ErrorCode,
  EventFrame,
  Frame,
  HelloData,
  PROTOCOL_VERSION,
  RequestFrame,
  ResponseFrame,
  SidecarStatus,
} from "./protocol";
import { encodeFrame } from "./frameCodec";
import { Transport } from "./transport";
import { RuntimeDeps } from "./deps";

/**
 * Stable error code surfaced when the pid file names a live sidecar owned
 * by another process: starting a second writer for the same data dir
 * (sessions.db + breakpoint store) is refused instead.
 */
const SIDECAR_ALREADY_RUNNING = "SIDECAR_ALREADY_RUNNING";

export interface SidecarClientOptions {
  readonly transportFactory: () => Transport;
  readonly deps: RuntimeDeps;
  readonly pidFilePath?: string;
  /** Total handshake timeout in ms. Default 45000 per spec. */
  readonly handshakeTimeoutMs?: number;
  /** Per-request RPC timeout in ms. Default 10000. */
  readonly requestTimeoutMs?: number;
  /** Heartbeat interval in ms. Default 5000. */
  readonly heartbeatIntervalMs?: number;
  /** How long to wait for a pong before declaring the sidecar dead. */
  readonly heartbeatTimeoutMs?: number;
  /** Maximum auto-restarts. Default 3. */
  readonly maxRestarts?: number;
  /** Base backoff between restarts (ms). Default 500; multiplied by 2^n. */
  readonly restartBackoffBaseMs?: number;
}

interface PendingRequest {
  readonly resolve: (value: Record<string, unknown>) => void;
  readonly reject: (reason: Error) => void;
  readonly timer: NodeJS.Timeout;
  readonly method: string;
}

type StatusListener = (status: SidecarStatus) => void;

/** One sidecar event frame, normalized for UI consumption. */
export interface SidecarEvent {
  readonly event: string;
  readonly data: Record<string, unknown>;
}
export type EventListener = (ev: SidecarEvent) => void;

interface SidecarClientResolvedOptions {
  readonly transportFactory: () => Transport;
  readonly deps: RuntimeDeps;
  readonly pidFilePath?: string;
  readonly handshakeTimeoutMs: number;
  readonly requestTimeoutMs: number;
  readonly heartbeatIntervalMs: number;
  readonly heartbeatTimeoutMs: number;
  readonly maxRestarts: number;
  readonly restartBackoffBaseMs: number;
}

export class SidecarClient {
  private readonly opts: SidecarClientResolvedOptions;
  private readonly deps: RuntimeDeps;
  private transport: Transport | null = null;
  private readonly pending = new Map<string, PendingRequest>();
  private readonly statusListeners = new Set<StatusListener>();
  private readonly eventListeners = new Set<EventListener>();
  private status: SidecarStatus = {
    state: "stopped",
    restartAttempts: 0,
    startedAt: 0,
  };
  private handshakeTimer: NodeJS.Timeout | null = null;
  private heartbeatTimer: number | null = null;
  private heartbeatWatchdog: NodeJS.Timeout | null = null;
  private restartTimer: NodeJS.Timeout | null = null;
  private stopping = false;
  private restartCount = 0;
  private lastPingSent = 0;
  /**
   * Sidecar pid this client itself last wrote to the pid file (null once
   * removed). The pre-spawn guard uses it to tell "our own leftover or hung
   * sidecar" (kill + respawn) apart from "a sidecar owned by somebody else"
   * (refuse to spawn a second writer).
   */
  private lastWrittenPid: number | null = null;
  /** Latest hello data received from the sidecar; null until handshake completes. */
  private helloData: HelloData | null = null;

  constructor(opts: SidecarClientOptions) {
    this.opts = {
      // Merge user options over defaults. The explicit `??` reads are the
      // whole point: every tunable (handshake timeout, restart budget,
      // backoff base, ...) must respect what the caller passed. Hard-coding
      // the defaults here would silently ignore test overrides (and any
      // production wiring that deviates from the spec defaults).
      handshakeTimeoutMs: opts.handshakeTimeoutMs ?? 45_000,
      requestTimeoutMs: opts.requestTimeoutMs ?? 10_000,
      heartbeatIntervalMs: opts.heartbeatIntervalMs ?? 5_000,
      heartbeatTimeoutMs: opts.heartbeatTimeoutMs ?? 8_000,
      maxRestarts: opts.maxRestarts ?? 3,
      restartBackoffBaseMs: opts.restartBackoffBaseMs ?? 500,
      transportFactory: opts.transportFactory,
      deps: opts.deps,
      pidFilePath: opts.pidFilePath,
    };
    this.deps = opts.deps;
  }

  /* ---------------------------------------------------------- */
  /* Lifecycle                                                  */
  /* ---------------------------------------------------------- */

  /**
   * Spawn + handshake. Returns when the sidecar is `ready` (hello received,
   * protocolVersion OK) or rejects on handshake failure.
   *
   * Re-arm semantics: after a previous run has reached the terminal state
   * ("stopped" via `stop()` or "crashed" via max-restarts), a fresh user
   * `start()` clears the old transport, resets the restart counter, and
   * runs the full spawn pipeline again.
   */
  async start(): Promise<void> {
    // Re-arm from terminal states (crashed, stopped) so a user can retry
    // manually. We close the previous transport so it doesn't keep timers
    // alive; it's then nulled out so the new run replaces it cleanly.
    if (this.transport) {
      if (this.status.state === "crashed" || this.status.state === "stopped") {
        try { await this.transport.close(); } catch { /* ignore */ }
        this.transport = null;
      } else {
        throw new Error("SidecarClient already started");
      }
    }
    this.stopping = false;
    // A fresh start() (whether by the user or by the first attempt) re-arms
    // the restart budget. Internal restart loops explicitly call
    // `restartTransport()` which does NOT reset, so consecutive rapid
    // failures still push restartCount up toward `maxRestarts`.
    this.restartCount = 0;
    this.deps.logger.info(`start: pidFilePath=${this.opts.pidFilePath ?? "<none>"}`);

    // R4a + instance mutex: verify no live sidecar owns the data dir and
    // clean any leftover PID file before spawn.
    await this.cleanupLeftoverPid();

    this.updateStatus({ state: "starting", startedAt: this.deps.clock.now() });

    this.transport = this.opts.transportFactory();
    this.transport.onFrame((f) => this.onFrame(f));
    this.transport.onLog((ch, line) => {
      // Sidecar logs go to stderr only; surface them via the same logger.
      if (ch === "stderr") this.deps.logger.info(`[sidecar stderr] ${line}`);
    });
    this.transport.onClose((code, signal) => {
      this.handleTransportClose(code, signal).catch((e) => {
        this.deps.logger.error(`handleTransportClose error: ${(e as Error).message}`);
      });
    });

    try {
      await this.transport.start();
    } catch (e) {
      // Handshake timer, if any, must not be left running.
      this.cancelHandshakeTimer();
      this.transport = null;
      this.failWith({ code: ErrorCode.SIDECAR_DEAD, message: `spawn failed: ${(e as Error).message}` });
      throw e;
    }
    this.deps.logger.info(`spawned sidecar pid=${this.transport.pid}`);

    // Drive the handshake.
    try {
      await this.waitForHello();
    } catch (e) {
      // The transport may already be dead — try to kill the tree. Timer is
      // always cleared inside waitForHello (either by the timeout firing or
      // by the cancel paths on resolve / version-mismatch / close).
      await this.killCurrentTree();
      throw e;
    }

    // Handshake OK: begin heartbeats and announce ready.
    this.startHeartbeat();
    this.updateStatus({ state: "ready" });
    this.writePidFile();
  }

  /**
   * Internal-only restart path used by `handleTransportClose`. Closes the
   * previous transport and re-runs `start()` WITHOUT resetting
   * `restartCount`, so consecutive failures can still drive us to the
   * max-restarts ceiling.
   */
  private async restartTransport(): Promise<void> {
    if (this.transport) {
      try { await this.transport.close(); } catch { /* ignore */ }
      this.transport = null;
    }
    // Run start() but bypass the re-arm guard (we already cleared transport)
    // and the user-facing restartCount reset.
    if (this.transport) throw new Error("SidecarClient already started");
    this.stopping = false;
    this.deps.logger.info(`restart: pidFilePath=${this.opts.pidFilePath ?? "<none>"}`);

    await this.cleanupLeftoverPid();

    this.updateStatus({ state: "starting", startedAt: this.deps.clock.now() });

    this.transport = this.opts.transportFactory();
    this.transport.onFrame((f) => this.onFrame(f));
    this.transport.onLog((ch, line) => {
      if (ch === "stderr") this.deps.logger.info(`[sidecar stderr] ${line}`);
    });
    this.transport.onClose((code, signal) => {
      this.handleTransportClose(code, signal).catch((e) => {
        this.deps.logger.error(`handleTransportClose error: ${(e as Error).message}`);
      });
    });

    try {
      await this.transport.start();
    } catch (e) {
      this.cancelHandshakeTimer();
      this.transport = null;
      this.failWith({ code: ErrorCode.SIDECAR_DEAD, message: `spawn failed: ${(e as Error).message}` });
      throw e;
    }
    this.deps.logger.info(`respawned sidecar pid=${this.transport.pid}`);

    try {
      await this.waitForHello();
    } catch (e) {
      await this.killCurrentTree();
      throw e;
    }

    this.startHeartbeat();
    this.updateStatus({ state: "ready" });
    this.writePidFile();
  }

  async stop(): Promise<void> {
    this.stopping = true;
    this.cancelHandshakeTimer();
    this.stopHeartbeat();
    if (this.restartTimer) {
      this.deps.clock.clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    // Reject all pending requests.
    for (const [, p] of this.pending) {
      this.deps.clock.clearTimeout(p.timer);
      p.reject(new SidecarRpcError(ErrorCode.SIDECAR_DEAD, "client stopping"));
    }
    this.pending.clear();
    await this.killCurrentTree();
    if (this.transport) await this.transport.close();
    this.transport = null;
    this.updateStatus({ state: "stopped" });
    this.removePidFile();
  }

  /* ---------------------------------------------------------- */
  /* Public status API                                          */
  /* ---------------------------------------------------------- */

  onStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener);
    listener(this.status); // emit current state immediately
    return () => this.statusListeners.delete(listener);
  }

  getStatus(): SidecarStatus {
    return this.status;
  }

  /**
   * Subscribe to every `evt` frame the sidecar pushes (chat.*, tool.*,
   * approval.request, hello, ...). Returns an unsubscribe function.
   *
   * A throwing listener must never take the client down: each listener is
   * called inside its own try/catch and a failure is logged, not propagated.
   */
  onEvent(listener: EventListener): () => void {
    this.eventListeners.add(listener);
    return () => {
      this.eventListeners.delete(listener);
    };
  }

  /**
   * The hello payload the sidecar sent at handshake. Null until the
   * handshake succeeds. Useful for diagnostics and tests.
   */
  getHelloData(): HelloData | null {
    return this.helloData;
  }

  /* ---------------------------------------------------------- */
  /* RPC                                                        */
  /* ---------------------------------------------------------- */

  async request(
    method: string,
    params: Record<string, unknown> = {},
    opts?: { id?: string },
  ): Promise<Record<string, unknown>> {
    if (!this.transport || this.status.state !== "ready") {
      throw new SidecarRpcError(ErrorCode.SIDECAR_DEAD, `sidecar not ready: ${this.status.state}`);
    }
    // Caller-supplied id lets the UI correlate a long-lived stream (e.g. a
    // chat) with the request that started it, so `chat.cancel` can name it.
    const id = opts?.id ?? randomUUID();
    const frame: RequestFrame = {
      type: "req",
      id,
      method,
      params: { ...params, protocolVersion: PROTOCOL_VERSION },
    };
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = this.deps.clock.setTimeout(() => {
        this.pending.delete(id);
        reject(new SidecarRpcError(ErrorCode.TIMEOUT, `request '${method}' timed out`));
      }, this.opts.requestTimeoutMs);
      this.pending.set(id, { resolve, reject, timer, method });
      try {
        this.transport!.send(frame);
      } catch (e) {
        this.deps.clock.clearTimeout(timer);
        this.pending.delete(id);
        reject(e);
      }
    });
  }

  /* ---------------------------------------------------------- */
  /* Internals: handshake                                       */
  /* ---------------------------------------------------------- */

  private waitForHello(): Promise<HelloData> {
    return new Promise<HelloData>((resolve, reject) => {
      // Handshake timer is routed through deps.clock so tests can advance
      // virtual time with `clock.tick(ms)` instead of waiting on real
      // wall-clock seconds. The default deps bundle uses realClock which
      // delegates to Node's setTimeout, preserving the 45s production
      // behavior; tests inject a FakeClock.
      this.handshakeTimer = this.deps.clock.setTimeout(() => {
        this.handshakeTimer = null;
        this.updateStatus({
          state: "crashed",
          lastError: {
            code: ErrorCode.PROTOCOL_ERROR,
            message: `handshake timed out after ${this.opts.handshakeTimeoutMs}ms`,
          },
        });
        reject(new SidecarRpcError(
          ErrorCode.PROTOCOL_ERROR,
          `handshake timed out after ${this.opts.handshakeTimeoutMs}ms`,
        ));
      }, this.opts.handshakeTimeoutMs);

      // The hello arrives as an unsolicited `evt hello` frame.
      const offFrame = this.transport!.onFrame((f) => {
        if (f.type !== "evt" || f.event !== "hello") return;
        const data = f.data as unknown as HelloData;
        if (typeof data !== "object" || data === null) {
          this.cancelHandshakeTimer();
          offFrame();
          reject(new SidecarRpcError(ErrorCode.PROTOCOL_ERROR, "hello data is not an object"));
          return;
        }
        if (data.protocolVersion !== PROTOCOL_VERSION) {
          this.cancelHandshakeTimer();
          offFrame();
          this.updateStatus({
            state: "version-mismatch",
            lastError: {
              code: ErrorCode.VERSION_MISMATCH,
              message: `client=${PROTOCOL_VERSION}, sidecar=${data.protocolVersion}`,
            },
          });
          reject(new SidecarRpcError(
            ErrorCode.VERSION_MISMATCH,
            `client=${PROTOCOL_VERSION}, sidecar=${data.protocolVersion}`,
          ));
          return;
        }
        this.helloData = data;
        this.cancelHandshakeTimer();
        offFrame();
        this.updateStatus({
          backendVersion: data.backendVersion,
          protocolVersion: data.protocolVersion,
          pid: data.pid,
          capabilities: data.capabilities,
        });
        resolve(data);
      });

      // Also fail fast if the transport dies during handshake.
      const offClose = this.transport!.onClose(() => {
        this.cancelHandshakeTimer();
        offFrame();
        reject(new SidecarRpcError(ErrorCode.SIDECAR_DEAD, "transport closed during handshake"));
      });
      // Ensure we tear down close listener on resolve too.
      const origResolve = resolve;
      // (the listener is no-op once resolved because resolve is final;
      // leaving onClose in place is harmless — no events fire after close.)
      void origResolve;
      void offClose;
    });
  }

  private cancelHandshakeTimer(): void {
    if (this.handshakeTimer) {
      // Routed through deps.clock so the cancel matches wherever the
      // timer was scheduled — real setTimeout in production, fake in tests.
      this.deps.clock.clearTimeout(this.handshakeTimer);
      this.handshakeTimer = null;
    }
  }

  /* ---------------------------------------------------------- */
  /* Internals: heartbeat                                       */
  /* ---------------------------------------------------------- */

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = this.deps.clock.setInterval(() => {
      this.fireHeartbeat().catch((e) => {
        this.deps.logger.warn(`heartbeat error: ${(e as Error).message}`);
      });
    }, this.opts.heartbeatIntervalMs);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      this.deps.clock.clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
    if (this.heartbeatWatchdog) {
      this.deps.clock.clearTimeout(this.heartbeatWatchdog);
      this.heartbeatWatchdog = null;
    }
  }

  private async fireHeartbeat(): Promise<void> {
    if (!this.transport || this.status.state !== "ready") return;
    try {
      this.lastPingSent = this.deps.clock.now();
      // We don't await; the response is matched in onFrame.
      const pongPromise = this.request("ping", {});
      pongPromise.catch(() => { /* surfaced via state machine */ });
      this.heartbeatWatchdog = this.deps.clock.setTimeout(() => {
        this.heartbeatWatchdog = null;
        const sinceLast = this.deps.clock.now() - this.lastPingSent;
        this.deps.logger.warn(`heartbeat pong missing (${sinceLast}ms); declaring dead`);
        this.handleTransportClose(null, null).catch(() => { /* root */ });
      }, this.opts.heartbeatTimeoutMs);
      // Reset watchdog when pong resolves.
      pongPromise.finally(() => {
        if (this.heartbeatWatchdog) {
          this.deps.clock.clearTimeout(this.heartbeatWatchdog);
          this.heartbeatWatchdog = null;
        }
      });
    } catch (e) {
      this.deps.logger.warn(`heartbeat send error: ${(e as Error).message}`);
    }
  }

  /* ---------------------------------------------------------- */
  /* Internals: frame dispatch                                  */
  /* ---------------------------------------------------------- */

  private onFrame(frame: Frame): void {
    if (frame.type === "res") {
      const res = frame as ResponseFrame;
      const p = this.pending.get(res.id);
      if (!p) {
        this.deps.logger.warn(`orphan response id=${res.id}`);
        return;
      }
      this.pending.delete(res.id);
      this.deps.clock.clearTimeout(p.timer);
      if (res.ok) {
        p.resolve((res.result ?? {}) as Record<string, unknown>);
      } else {
        const code = (res.error?.code ?? ErrorCode.BAD_REQUEST) as string;
        const message = res.error?.message ?? "unknown error";
        p.reject(new SidecarRpcError(code, message));
      }
      return;
    }
    // Event frames: normalize and hand to subscribers. The hello frame is
    // also one of these (the handshake has its own listener), so we do not
    // special-case it here — subscribers see everything, in arrival order.
    if (frame.type === "evt") {
      const ev = frame as EventFrame;
      const data = (ev.data ?? {}) as Record<string, unknown>;
      for (const listener of Array.from(this.eventListeners)) {
        try {
          listener({ event: ev.event, data });
        } catch (e) {
          this.deps.logger.warn(`event listener threw on '${ev.event}': ${(e as Error).message}`);
        }
      }
      return;
    }
  }

  /* ---------------------------------------------------------- */
  /* Internals: death + restart                                 */
  /* ---------------------------------------------------------- */

  private async handleTransportClose(code: number | null, signal: NodeJS.Signals | null): Promise<void> {
    if (this.stopping) return;
    this.deps.logger.warn(`sidecar transport closed code=${code} signal=${signal}`);
    this.stopHeartbeat();
    // Reject everything in-flight.
    for (const [, p] of this.pending) {
      this.deps.clock.clearTimeout(p.timer);
      p.reject(new SidecarRpcError(ErrorCode.SIDECAR_DEAD, "sidecar exited"));
    }
    this.pending.clear();

    if (this.restartCount >= this.opts.maxRestarts) {
      this.deps.logger.error(`max restarts (${this.opts.maxRestarts}) exhausted; giving up`);
      this.failWith({ code: ErrorCode.SIDECAR_DEAD, message: `max restarts exhausted` });
      return;
    }
    this.restartCount += 1;
    const backoff = this.opts.restartBackoffBaseMs * Math.pow(2, this.restartCount - 1);
    this.updateStatus({
      state: "restarting",
      restartAttempts: this.restartCount,
      lastError: { code: ErrorCode.SIDECAR_DEAD, message: `exited code=${code} signal=${signal}` },
    });
    this.deps.logger.info(`restarting in ${backoff}ms (attempt ${this.restartCount})`);
    await new Promise<void>((r) => {
      this.restartTimer = this.deps.clock.setTimeout(r, backoff);
    });
    if (this.stopping) return;
    try {
      // restartTransport() preserves restartCount so consecutive failures
      // accumulate toward maxRestarts. A reset would defeat the cap.
      await this.restartTransport();
    } catch (e) {
      this.deps.logger.error(`restart failed: ${(e as Error).message}`);
      // Loop will trigger via handleTransportClose once the new process dies.
    }
  }

  private async killCurrentTree(): Promise<void> {
    if (!this.transport || !this.transport.pid) return;
    const pid = this.transport.pid;
    await this.deps.killTree(pid);
  }

  /* ---------------------------------------------------------- */
  /* Internals: status + pid file                               */
  /* ---------------------------------------------------------- */

  private updateStatus(patch: Partial<SidecarStatus>): void {
    const prev = this.status;
    const next = { ...prev, ...patch };
    const stateChanged = next.state !== prev.state;
    this.status = next;
    // Only notify status listeners on state transitions. Field-only
    // patches (e.g. hello-data arriving mid-handshake) keep the state
    // stable — surfacing those as separate listener invocations would
    // emit duplicate "starting" events and force UI to re-render for no
    // visible change. UI code that needs hello fields can read
    // getStatus() / getHelloData().
    if (stateChanged) {
      for (const l of this.statusListeners) l(this.status);
    }
  }

  private failWith(err: { code: string; message: string }): void {
    this.updateStatus({ state: "crashed", lastError: err });
  }

  private writePidFile(): void {
    if (!this.opts.pidFilePath || !this.transport?.pid) return;
    try {
      this.deps.fs.writeFile(this.opts.pidFilePath, String(this.transport.pid));
      this.lastWrittenPid = this.transport.pid;
    } catch (e) {
      this.deps.logger.warn(`writePidFile failed: ${(e as Error).message}`);
    }
  }

  private removePidFile(): void {
    if (!this.opts.pidFilePath) return;
    // Forget ownership first, so a later start() in this process cannot
    // mistake a foreign pid for "ours" after the file is gone.
    this.lastWrittenPid = null;
    try { this.deps.fs.unlink(this.opts.pidFilePath); } catch { /* ignore */ }
  }

  /**
   * R4a + instance mutex, both applied before any spawn:
   *
   *   1. Instance mutex — the pid file names a process that is still ALIVE
   *      and that this client did not spawn (another app instance, or a
   *      sidecar orphaned by a hard kill). Two sidecars writing the same
   *      sessions.db / breakpoint store would corrupt them, and stdio gives
   *      us no way to attach to the existing process, so the only safe move
   *      is to fail loudly: status becomes "crashed" with a clear error and
   *      start() rejects. Never spawn a second writer silently.
   *   2. Leftover cleanup — otherwise the recorded pid is stale or our own
   *      (e.g. a hung sidecar being restarted): kill its tree and unlink so
   *      the fresh spawn starts from a clean slate.
   */
  private async cleanupLeftoverPid(): Promise<void> {
    if (!this.opts.pidFilePath) return;
    const fs = this.deps.fs;
    if (!fs.exists(this.opts.pidFilePath)) return;
    const content = fs.readFile(this.opts.pidFilePath);
    const pid = Number.parseInt(content ?? "", 10);
    if (!Number.isFinite(pid)) {
      fs.unlink(this.opts.pidFilePath);
      return;
    }
    if (this.isForeignLivePid(pid)) {
      const message =
        `another sidecar (pid=${pid}) is still running and owns ` +
        `${this.opts.pidFilePath}; refusing to start a second writer for the ` +
        `same data dir — stop that process (or delete the pid file) first`;
      this.deps.logger.warn(message);
      this.failWith({ code: SIDECAR_ALREADY_RUNNING, message });
      throw new SidecarRpcError(SIDECAR_ALREADY_RUNNING, message);
    }
    if (pid === process.pid) {
      // The pid file records the sidecar child, never this process, so a
      // self-referencing entry is garbage data: discard it without ever
      // signalling ourselves.
      this.deps.logger.warn(`pid file records our own pid=${pid}; discarding without kill`);
      fs.unlink(this.opts.pidFilePath);
      return;
    }
    this.deps.logger.warn(`found leftover pid file with pid=${pid}; killing tree`);
    try {
      await this.deps.killTree(pid);
    } catch (e) {
      this.deps.logger.warn(`killTree(${pid}) failed: ${(e as Error).message}`);
    }
    fs.unlink(this.opts.pidFilePath);
  }

  /**
   * True when `pid` is alive AND is not a process this client owns: not our
   * own process id (a probe against self trivially succeeds) and not the
   * sidecar pid we ourselves last wrote (that one is handled as a leftover
   * by cleanupLeftoverPid, which kills + respawns it).
   */
  private isForeignLivePid(pid: number): boolean {
    if (pid === process.pid) return false;
    if (this.lastWrittenPid !== null && pid === this.lastWrittenPid) return false;
    return this.isPidAlive(pid);
  }

  /**
   * Existence probe without delivering a real signal: process.kill(pid, 0)
   * only fails when the process is gone (ESRCH). EPERM means the process
   * exists but we may not signal it — count that as alive.
   */
  private isPidAlive(pid: number): boolean {
    if (!Number.isFinite(pid) || pid <= 0) return false;
    try {
      process.kill(pid, 0);
      return true;
    } catch (e) {
      return (e as NodeJS.ErrnoException)?.code === "EPERM";
    }
  }
}

/**
 * Domain error type for RPC failures. Codes are stable strings.
 */
export class SidecarRpcError extends Error {
  readonly code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
    this.name = "SidecarRpcError";
  }
}

// Re-exported for tests / convenience.
export { encodeFrame };