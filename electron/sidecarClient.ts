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
  /**
   * Per-request RPC timeout in ms. Default 30000.
   *
   * 必须大于侧车网络请求的 HTTP 超时（15 秒），否则界面拿不到厂商真实错误：
   * 侧车对 /models 这类厂商调用有自己的 HTTP 超时（15s），超了会返回带
   * 状态码/原因的错误响应。如果本 RPC 超时先到，界面只会看到「timed out」，
   * 厂商的真实错误（401/429/网络断开等）被吞掉 —— 这正是 2026-10-13 用户
   * 在「添加模型」对话框里踩到的坑。10s 时代（2026-10 之前）就是这样。
   */
  readonly requestTimeoutMs?: number;
  /** Heartbeat interval in ms. Default 5000. */
  readonly heartbeatIntervalMs?: number;
  /** How long to wait for a pong before declaring the sidecar dead. */
  readonly heartbeatTimeoutMs?: number;
  /** Maximum auto-restarts. Default 3. */
  readonly maxRestarts?: number;
  /** Base backoff between restarts (ms). Default 500; multiplied by 2^n. */
  readonly restartBackoffBaseMs?: number;
  /**
   * (t_5d29bcab) Absolute path of the sidecar script (e.g. <root>/python/
   * sidecar.py). Known up-front by the caller (it built the spawn command);
   * used to verify a leftover pid's command line before killTree()ing it.
   * When omitted, cleanup falls back to the previous pid-only behavior.
   */
  readonly sidecarScriptPath?: string;
}

interface PendingRequest {
  readonly resolve: (value: Record<string, unknown>) => void;
  readonly reject: (reason: Error) => void;
  readonly timer: NodeJS.Timeout;
  readonly method: string;
  /** deps.clock.now() when the request was sent — timeout log diagnostics. */
  readonly sentAt: number;
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
  /** (t_5d29bcab) sidecar script path from opts, for identity-checked kills. */
  private readonly optScriptPath: string | null;
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
  /**
   * Resolver of the in-flight restart-backoff wait, if any. stop() uses it
   * to settle the wait when it clears the timer: without this, the promise
   * inside handleTransportClose would dangle forever (its timer is gone, so
   * nothing ever resolves it) and the async call frame leaks.
   */
  private restartWaitResolve: (() => void) | null = null;
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
  /**
   * REAL sidecar pid reported by the sidecar itself in hello
   * (`{"type":"evt","event":"hello","data":{...,"pid":N}}`). Differs from
   * `transport.pid` when the spawned interpreter is a shim/launcher whose
   * child actually runs sidecar.py (Windows venv python.exe). The pid file
   * and lastWrittenPid must track the process that truly owns sessions.db.
   * (t_5d29bcab)
   */
  private realSidecarPid: number | null = null;
  /**
   * Absolute path of the sidecar script, captured from the transport's spawn
   * options when the transport exposes them. Used to verify a leftover pid's
   * command line really points at OUR sidecar before killTree()ing it.
   * (t_5d29bcab)
   */
  private sidecarScriptPath: string | null = null;

  constructor(opts: SidecarClientOptions) {
    this.opts = {
      // Merge user options over defaults. The explicit `??` reads are the
      // whole point: every tunable (handshake timeout, restart budget,
      // backoff base, ...) must respect what the caller passed. Hard-coding
      // the defaults here would silently ignore test overrides (and any
      // production wiring that deviates from the spec defaults).
      handshakeTimeoutMs: opts.handshakeTimeoutMs ?? 45_000,
      // 必须大于侧车网络请求的 HTTP 超时（15 秒），否则界面拿不到厂商真实错误。
      // 详见 SidecarClientOptions.requestTimeoutMs 的注释。
      requestTimeoutMs: opts.requestTimeoutMs ?? 30_000,
      heartbeatIntervalMs: opts.heartbeatIntervalMs ?? 5_000,
      heartbeatTimeoutMs: opts.heartbeatTimeoutMs ?? 8_000,
      maxRestarts: opts.maxRestarts ?? 3,
      restartBackoffBaseMs: opts.restartBackoffBaseMs ?? 500,
      transportFactory: opts.transportFactory,
      deps: opts.deps,
      pidFilePath: opts.pidFilePath,
    };
    this.deps = opts.deps;
    this.optScriptPath =
      typeof opts.sidecarScriptPath === "string" && opts.sidecarScriptPath.length
        ? opts.sidecarScriptPath
        : null;
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
    this.realSidecarPid = null;
    this.sidecarScriptPath = this.optScriptPath;
    this.deps.logger.info(`start: pidFilePath=${this.opts.pidFilePath ?? "<none>"}`);

    // R4a + instance mutex: verify no live sidecar owns the data dir and
    // clean any leftover PID file before spawn.
    await this.cleanupLeftoverPid();

    this.updateStatus({ state: "starting", startedAt: this.deps.clock.now() });

    this.transport = this.opts.transportFactory();
    this.captureScriptPath();
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
    // Restart racing a terminal state: if the client was stopped (or gave up
    // after max-restarts) while this restart was in flight, do NOT spawn a
    // new sidecar — it would have no owner and would outlive the host
    // process (its open stdio pipes keep the event loop alive; t_5d29bcab).
    if (this.stopping) {
      this.deps.logger.info("restartTransport aborted: stopping is set");
      return;
    }
    if (this.status.state === "stopped" || this.status.state === "crashed") {
      this.deps.logger.info(
        `restartTransport aborted: client is ${this.status.state}`,
      );
      return;
    }
    if (this.transport) {
      try { await this.transport.close(); } catch { /* ignore */ }
      this.transport = null;
    }
    // Run start() but bypass the re-arm guard (we already cleared transport)
    // and the user-facing restartCount reset.
    if (this.transport) throw new Error("SidecarClient already started");
    this.stopping = false;
    this.realSidecarPid = null;
    this.sidecarScriptPath = this.optScriptPath;
    this.deps.logger.info(`restart: pidFilePath=${this.opts.pidFilePath ?? "<none>"}`);

    await this.cleanupLeftoverPid();

    this.updateStatus({ state: "starting", startedAt: this.deps.clock.now() });

    this.transport = this.opts.transportFactory();
    this.captureScriptPath();
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
    if (this.restartWaitResolve) {
      // Settle the dangling backoff wait so handleTransportClose's async
      // frame completes (its `stopping` check then aborts the respawn).
      const r = this.restartWaitResolve;
      this.restartWaitResolve = null;
      r();
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
    opts?: { id?: string; timeoutMs?: number },
  ): Promise<Record<string, unknown>> {
    if (!this.transport || this.status.state !== "ready") {
      // 把「为什么不 ready」带上：用户实际踩到的是侧车 crashed（重启 3 次
      // 全秒死），旧消息只有 "sidecar not ready: crashed" 一句，什么线索
      // 都没有。lastError 里有 exit code / 具体失败原因，带上它，界面才
      // 显示得出来（2026-10-13）。
      const last = this.status.lastError;
      const why = last ? ` (${last.code}: ${last.message})` : "";
      throw new SidecarRpcError(
        ErrorCode.SIDECAR_DEAD,
        `sidecar not ready: ${this.status.state}${why}`,
      );
    }
    // Caller-supplied id lets the UI correlate a long-lived stream (e.g. a
    // chat) with the request that started it, so `chat.cancel` can name it.
    const id = opts?.id ?? randomUUID();
    // Per-call timeout: network-shaped RPCs (models.fetch) pass an explicit
    // longer value; local RPCs (tens of ms) keep the default. Must stay > the
    // sidecar's own HTTP timeout (15s) so the vendor's real error wins.
    const timeoutMs = opts?.timeoutMs && opts.timeoutMs > 0 ? opts.timeoutMs : this.opts.requestTimeoutMs;
    const frame: RequestFrame = {
      type: "req",
      id,
      method,
      params: { ...params, protocolVersion: PROTOCOL_VERSION },
    };
    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = this.deps.clock.setTimeout(() => {
        // pending 数取删自己**之前**的值：读者要的是"包括这个超时请求
        // 在内，当时有几个在飞"；其它还在等的请求另列在 still waiting 里。
        const pendingCount = this.pending.size;
        this.pending.delete(id);
        // 抛出之前必须留日志（2026-10-13 用户明确要求「程序有问题都可以找
        // 到」）：方法名、等了多久、当时还有几个请求在飞、侧车什么状态。
        // 之前这条路径完全静默，超时了界面只有一句 timed out，日志里什么
        // 都查不到。pending 数先删再读（把自己从计数里去掉），读者要的是
        // 「除了它还有谁在等」。
        const waited = this.deps.clock.now() - sentAt;
        this.deps.logger.warn(
          `RPC timeout: request '${method}' timed out after ${timeoutMs}ms ` +
          `(waited ${waited}ms); pending=${pendingCount} (incl. this one), ` +
          `sidecar=${this.status.state}` +
          (this.pending.size ? `, still waiting: ${Array.from(this.pending.values()).map(p => p.method).join(",")}` : ""),
        );
        reject(new SidecarRpcError(ErrorCode.TIMEOUT, `request '${method}' timed out`));
      }, timeoutMs);
      const sentAt = this.deps.clock.now();
      this.pending.set(id, { resolve, reject, timer, method, sentAt });
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
        // t_5d29bcab: the hello pid is the REAL sidecar process (the
        // transport pid may only be a shim/launcher). Track it for the pid
        // file and ownership checks.
        this.realSidecarPid = Number.isFinite(data.pid) ? data.pid : null;
        this.updateStatus({
          backendVersion: data.backendVersion,
          protocolVersion: data.protocolVersion,
          pid: data.pid,
          capabilities: data.capabilities,
        });
        resolve(data);
      });
      // t_5d29bcab: remember the REAL sidecar pid from hello. The transport
      // pid can be a venv shim/launcher whose child actually runs sidecar.py
      // (Windows: .venv\Scripts\python.exe is a launcher); the pid file and
      // lastWrittenPid must track the process that truly owns sessions.db,
      // else leftover cleanup kills the shim and orphans the real sidecar.

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
        // 「pong 没到」不等于侧车死了：一个慢请求（如 models.fetch 在飞）
        // 把 stdio 响应拖住时心跳也会缺。把在飞请求列出来，一眼能分清
        // 「慢请求」还是「真死」（2026-10-13）。
        const inflight = this.pending.size > 1
          ? `; ${this.pending.size - 1} other request(s) in flight: ${
              Array.from(this.pending.values())
                .filter(p => p.method !== "ping")
                .map(p => `${p.method}(${this.deps.clock.now() - p.sentAt}ms)`)
                .join(", ")}`
          : "";
        this.deps.logger.warn(
          `heartbeat pong missing (${sinceLast}ms); declaring dead${inflight}`,
        );
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
      this.restartWaitResolve = r;
      this.restartTimer = this.deps.clock.setTimeout(r, backoff);
    });
    this.restartWaitResolve = null;
    // Terminal states (stopped by user, crashed after max-restarts) must
    // abort the restart loop: respawning now would create a sidecar nobody
    // owns — its open stdio pipes keep the host process alive and the
    // leftover process outlives the app (t_5d29bcab hang).
    if (this.stopping) return;
    if (this.status.state === "stopped" || this.status.state === "crashed") {
      this.deps.logger.info(
        `restart aborted: client is ${this.status.state}; not respawning`,
      );
      return;
    }
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
    // t_5d29bcab: if hello reported a DIFFERENT real sidecar pid (shim
    // scenario), tree-killing the shim alone can leave the real sidecar
    // alive as an orphan. Kill the real pid's tree too — and if we have the
    // script path, kill precisely by command-line match instead of pid.
    const real = this.realSidecarPid;
    if (real !== null && real !== pid) {
      if (this.sidecarScriptPath) {
        await this.killSidecarByCommandLine(real);
      } else {
        await this.deps.killTree(real);
      }
    }
  }

  /**
   * (t_5d29bcab) Kill the tree rooted at `pid`, but ONLY after verifying via
   * the process command line that it really is our sidecar script. Returns
   * true if killed, false if skipped. Identity mismatch => never kill.
   */
  private async killSidecarByCommandLine(pid: number): Promise<boolean> {
    const script = this.sidecarScriptPath;
    if (!script) {
      await this.deps.killTree(pid);
      return true;
    }
    let cmdline: string | null = null;
    try {
      cmdline = await this.deps.readProcessCommandLine(pid);
    } catch {
      cmdline = null;
    }
    if (!cmdline) {
      // Process gone (or unreadable): nothing to kill. Do NOT fall back to
      // killing by pid — an unreadable-but-alive pid we can't identify is
      // exactly the "shoot blind" failure mode we are removing.
      return false;
    }
    if (!this.isOurSidecarCommandLine(cmdline)) {
      this.deps.logger.warn(
        `pid ${pid} alive but command line does not match our sidecar script; not killing: ${cmdline}`,
      );
      return false;
    }
    try {
      await this.deps.killTree(pid);
      return true;
    } catch (e) {
      this.deps.logger.warn(`killTree(${pid}) failed: ${(e as Error).message}`);
      return false;
    }
  }

  /**
   * (t_5d29bcab) Does this command line run OUR sidecar script? Compare on
   * normalized backslashes and case (Windows paths are case-insensitive).
   */
  private isOurSidecarCommandLine(cmdline: string): boolean {
    const script = this.sidecarScriptPath;
    if (!script) return false;
    const norm = (s: string) => s.replace(/\\/g, "/").toLowerCase();
    const want = norm(script);
    const got = norm(cmdline);
    if (got.includes(want)) return true;
    // Quoted variants: \"C:\...\sidecar.py\" -> the escaped quote inside the
    // normalized string collapses to "...sidecar.py" which the includes()
    // above already catches; the case below only guards unquoted paths
    // containing the script as the tail argument.
    return false;
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

  /**
   * (t_5d29bcab) Remember the sidecar script path exposed by the transport
   * (stdio transports know their spawn args; others return undefined and
   * identity-checked kills gracefully degrade to pid-based behavior).
   */
  private captureScriptPath(): void {
    const sp = this.transport?.scriptPath ?? this.optScriptPath;
    this.sidecarScriptPath = typeof sp === "string" && sp.length ? sp : null;
  }

  private writePidFile(): void {
    if (!this.opts.pidFilePath) return;
    // t_5d29bcab: prefer the REAL sidecar pid reported in hello. The
    // transport pid can be a venv shim/launcher on Windows whose child
    // actually runs sidecar.py; recording the shim's pid means later
    // cleanup kills (or misses) the shim and orphans the real sidecar —
    // the root cause of duplicate sidecars fighting over sessions.db.
    const pid = this.realSidecarPid ?? this.transport?.pid ?? null;
    if (pid === null) return;
    try {
      this.deps.fs.writeFile(this.opts.pidFilePath, String(pid));
      this.lastWrittenPid = pid;
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
   *   1. Instance mutex — the pid file names a process that is still ALIVE,
   *      unreadable (identity cannot be established), and that this client
   *      did not spawn. Two sidecars writing the same sessions.db /
   *      breakpoint store would corrupt them, and stdio gives us no way to
   *      attach to the existing process, so the only safe move is to fail
   *      loudly: status becomes "crashed" with a clear error and start()
   *      rejects. Never spawn a second writer silently.
   *      (t_5d29bcab: when the command line IS readable we no longer guess —
   *      a foreign cmdline means "not a sidecar", remove the file and move
   *      on; an OUR-script cmdline means a leftover sidecar, kill it.)
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
    // t_5d29bcab: verify identity BEFORE both the refusal and the kill. A
    // pid file can record the venv shim pid (legacy writePidFile bug) or a
    // recycled pid belonging to some unrelated process. If the pid is alive
    // and we know our script path, read its command line: foreign -> remove
    // the pid file only (never kill, never refuse); our script -> kill.
    if (this.isPidAlive(pid) && pid !== process.pid && this.sidecarScriptPath) {
      let cmdline: string | null = null;
      try {
        cmdline = await this.deps.readProcessCommandLine(pid);
      } catch {
        cmdline = null;
      }
      if (cmdline !== null) {
        if (!this.isOurSidecarCommandLine(cmdline)) {
          this.deps.logger.warn(
            `leftover pid=${pid} is alive but not our sidecar (cmdline: ${cmdline}); ` +
            `removing pid file only, not killing`,
          );
          fs.unlink(this.opts.pidFilePath);
          return;
        }
        // Reads as OUR sidecar: a leftover/orphaned sidecar (possibly the
        // real one behind a shim pid the file used to record). Kill it —
        // including the tree, which catches a still-live shim parent — and
        // unlink. This branch intentionally bypasses the foreign-live
        // refusal below: it IS a sidecar, but OUR leftover, so respawn.
        this.deps.logger.warn(`found leftover sidecar pid=${pid} (cmdline matched); killing tree`);
        try {
          await this.deps.killTree(pid);
        } catch (e) {
          this.deps.logger.warn(`killTree(${pid}) failed: ${(e as Error).message}`);
        }
        fs.unlink(this.opts.pidFilePath);
        return;
      }
      // cmdline unreadable but pid alive: fall through to the conservative
      // refusal below — we cannot prove it is safe to spawn.
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