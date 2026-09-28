/**
 * Transport abstraction.
 *
 * SidecarClient talks to a Transport, never directly to a child process.
 * The stdio implementation is the first one; future HTTP / WebSocket
 * transports can implement the same surface so callers don't have to change.
 */
import { Frame } from "./protocol";

export type FrameListener = (frame: Frame) => void;
export type CloseListener = (code: number | null, signal: NodeJS.Signals | null) => void;
export type LogListener = (channel: "stdout" | "stderr", line: string) => void;

export interface Transport {
  /** Start the transport. Idempotent in practice but undefined if called twice. */
  start(): Promise<void>;
  /** Send a single frame. Throws if the underlying stream is closed. */
  send(frame: Frame): void;
  /** Register a callback for incoming frames. Returns an unsubscribe fn. */
  onFrame(listener: FrameListener): () => void;
  /** Register a callback for close events. Returns an unsubscribe fn. */
  onClose(listener: CloseListener): () => void;
  /** Optional: register a callback for raw log lines (stderr / raw stdout). */
  onLog(listener: LogListener): () => void;
  /** Close the transport. Must be idempotent. */
  close(): Promise<void>;
  /** Current PID, if known. Null until started. */
  readonly pid: number | null;
}