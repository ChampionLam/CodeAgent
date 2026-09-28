/**
 * LSP-style frame codec.
 *
 * Wire format:
 *     Content-Length: <N>\r\n\r\n<JSON body, N UTF-8 bytes>
 *
 * This codec handles:
 *   * sticky packets: a single buffer may contain multiple back-to-back frames
 *   * half packets:   a frame may arrive in arbitrarily small chunks; the
 *                     codec retains partial state until the body is complete
 *   * UTF-8 multibyte boundaries: the decoder is `TextDecoder` with
 *     `fatal: true` so a split multi-byte char throws instead of being
 *     silently mangled. We only decode the body after reading all N bytes,
 *     so the boundary risk is purely defensive (a future chunked-read API
 *     would need it).
 *   * 5MB+ payloads:  no implicit cap; we trust Content-Length up to
 *                     MAX_FRAME_BYTES (64 MiB) which matches the Python side.
 */
import { Frame, ResponseFrame } from "./protocol";

const HEADER_TERMINATOR = Buffer.from("\r\n\r\n", "ascii");
const MAX_FRAME_BYTES = 64 * 1024 * 1024; // 64 MiB; matches Python sidecar limit
const MAX_HEADER_BYTES = 1024;

const utf8Decoder = new TextDecoder("utf-8", { fatal: true });

/**
 * Encode a frame into LSP wire format. UTF-8 length is the byte length of the
 * serialized JSON, NOT the character count.
 */
export function encodeFrame(frame: Frame): Buffer {
  // Stringify ourselves so we know the byte length precisely.
  const body = Buffer.from(JSON.stringify(frame), "utf-8");
  const header = Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, "ascii");
  return Buffer.concat([header, body]);
}

/**
 * Streaming frame decoder. Holds a partial buffer across `push` calls so the
 * caller can feed arbitrary byte chunks (e.g. raw chunks from a stream).
 */
export class FrameDecoder {
  private buffer: Buffer = Buffer.alloc(0);
  /** Filled-in once the header terminator is seen. */
  private expectedBodyBytes: number | null = null;

  /**
   * Feed raw bytes into the decoder and yield every complete frame found.
   * @throws if the frame is malformed (bad header, oversized, bad UTF-8,
   *         non-JSON object).
   */
  *push(chunk: Buffer): Generator<Frame> {
    if (chunk.length === 0) return;
    this.buffer = this.buffer.length === 0 ? chunk : Buffer.concat([this.buffer, chunk]);

    // Loop while we can extract at least one complete header.
    while (true) {
      // Still looking for the header terminator.
      if (this.expectedBodyBytes === null) {
        const terminatorIdx = this.buffer.indexOf(HEADER_TERMINATOR);
        if (terminatorIdx < 0) {
          if (this.buffer.length > MAX_HEADER_BYTES) {
            throw new Error(`frame header exceeds ${MAX_HEADER_BYTES} bytes`);
          }
          return;
        }
        const headerText = this.buffer.subarray(0, terminatorIdx).toString("ascii");
        const lengthMatch = /^Content-Length:\s*(\d+)\s*$/im.exec(headerText);
        if (!lengthMatch) {
          throw new Error(`missing or malformed Content-Length in header: ${JSON.stringify(headerText)}`);
        }
        const n = Number.parseInt(lengthMatch[1]!, 10);
        if (!Number.isFinite(n) || n < 0 || n > MAX_FRAME_BYTES) {
          throw new Error(`Content-Length out of range: ${n}`);
        }
        // Drop the header from the buffer.
        this.buffer = this.buffer.subarray(terminatorIdx + HEADER_TERMINATOR.length);
        this.expectedBodyBytes = n;
      }

      // Now try to read the body.
      const need = this.expectedBodyBytes;
      if (this.buffer.length < need) {
        // Half packet; wait for more.
        return;
      }
      const bodyBytes = this.buffer.subarray(0, need);
      this.buffer = this.buffer.subarray(need);
      this.expectedBodyBytes = null;

      // Decode UTF-8 with fatal mode so partial codepoints throw instead
      // of being silently turned into replacement characters.
      const text = utf8Decoder.decode(bodyBytes);
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch (err) {
        throw new Error(`JSON decode failed: ${(err as Error).message}`);
      }
      if (!isFrameObject(parsed)) {
        throw new Error(`decoded payload is not a frame object: ${typeof parsed}`);
      }
      yield parsed;
    }
  }

  /** Number of bytes currently buffered (for tests/diagnostics). */
  get pendingBytes(): number {
    return this.buffer.length;
  }

  /**
   * Signal that the input stream has ended. Any bytes still buffered
   * constitute a truncated frame and cause an explicit error rather than
   * being silently dropped — we prefer loud failure at EOF.
   *
   * @throws if a partial frame (incomplete body) is still buffered.
   */
  end(): void {
    if (this.expectedBodyBytes !== null) {
      const need = this.expectedBodyBytes;
      const have = this.buffer.length;
      this.expectedBodyBytes = null;
      this.buffer = Buffer.alloc(0);
      throw new Error(
        `truncated frame at end of stream: expected ${need} body bytes, got ${have}`,
      );
    }
    if (this.buffer.length > 0) {
      const leftover = this.buffer.length;
      this.buffer = Buffer.alloc(0);
      throw new Error(
        `trailing bytes after end of stream: ${leftover} byte(s) without a header`,
      );
    }
  }
}

function isFrameObject(value: unknown): value is Frame {
  if (typeof value !== "object" || value === null) return false;
  const t = (value as { type?: unknown }).type;
  return t === "req" || t === "res" || t === "evt";
}

/**
 * Convenience: type-guard a parsed object as a response frame.
 */
export function isResponseFrame(value: Frame): value is ResponseFrame {
  return value.type === "res";
}

/**
 * Convenience: type-guard a parsed object as a request frame.
 */
export function isRequestFrame(value: Frame): boolean {
  return value.type === "req";
}

/**
 * Convenience: type-guard a parsed object as an event frame.
 */
export function isEventFrame(value: Frame): boolean {
  return value.type === "evt";
}