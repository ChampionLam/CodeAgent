/**
 * Shared types and constants for the desk-agent frame protocol.
 *
 * This file is the single source of truth for the LSP-style wire format
 * enforced on both sides of the stdio pipe. Bumping PROTOCOL_VERSION
 * requires coordinated change in python/sidecar.py.
 */

export const PROTOCOL_VERSION = 1 as const;

/**
 * A frame is one of three LSP-style messages.
 *
 * The protocol does NOT define a `type: "evt"`-style request envelope for
 * outbound notifications; they are top-level objects with `type: "evt"`.
 */
export type Frame =
  | RequestFrame
  | ResponseFrame
  | EventFrame;

export interface RequestFrame {
  readonly type: "req";
  readonly id: string;
  readonly method: string;
  readonly params: Readonly<Record<string, unknown>>;
}

export interface ResponseFrame {
  readonly type: "res";
  readonly id: string;
  readonly ok: boolean;
  readonly result?: Readonly<Record<string, unknown>>;
  readonly error?: { readonly code: string; readonly message: string };
}

export interface EventFrame {
  readonly type: "evt";
  readonly event: string;
  readonly data: Readonly<Record<string, unknown>>;
}

export type HelloData = {
  readonly protocolVersion: number;
  readonly backendVersion: string;
  readonly pid: number;
  readonly capabilities: readonly string[];
};

/**
 * Error codes returned by the sidecar. Centralized so callers don't
 * pattern-match on free-form strings.
 */
export const ErrorCode = {
  VERSION_MISMATCH: "VERSION_MISMATCH",
  BAD_REQUEST: "BAD_REQUEST",
  BAD_METHOD: "BAD_METHOD",
  TIMEOUT: "TIMEOUT",
  SIDECAR_DEAD: "SIDECAR_DEAD",
  PROTOCOL_ERROR: "PROTOCOL_ERROR",
} as const;
export type ErrorCode = (typeof ErrorCode)[keyof typeof ErrorCode];

/**
 * Possible state values for the SidecarClient. Mirrors what the renderer
 * UI displays.
 */
export type SidecarState =
  | "starting"
  | "ready"
  | "crashed"
  | "restarting"
  | "version-mismatch"
  | "stopped";

export interface SidecarStatus {
  readonly state: SidecarState;
  readonly backendVersion?: string;
  readonly protocolVersion?: number;
  readonly pid?: number;
  readonly capabilities?: readonly string[];
  readonly lastError?: { readonly code: string; readonly message: string };
  readonly restartAttempts: number;
  readonly startedAt: number;
}