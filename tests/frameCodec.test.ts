/**
 * Tests for the LSP-style frame codec.
 *
 * Covers the five required scenarios:
 *   1. plain single-frame round-trip
 *   2. sticky packets (multiple frames in one chunk)
 *   3. half packets (frame split across many tiny reads, including a split
 *      between header terminator and body)
 *   4. UTF-8 multi-byte boundaries (Chinese + emoji body, split mid-char)
 *   5. 5 MiB large payload
 */
import test from "node:test";
import assert from "node:assert/strict";
import { Buffer } from "node:buffer";
import { encodeFrame, FrameDecoder } from "../electron/frameCodec";
import {
  PROTOCOL_VERSION,
  type Frame,
} from "../electron/protocol";

function makeReq(id: string, method: string, params: Record<string, unknown> = {}): Frame {
  return { type: "req", id, method, params: { protocolVersion: PROTOCOL_VERSION, ...params } };
}

test("round-trip a single frame", () => {
  const frame = makeReq("a", "ping");
  const enc = encodeFrame(frame);
  const dec = new FrameDecoder();
  const frames = [...dec.push(enc)];
  assert.equal(frames.length, 1);
  assert.deepEqual(frames[0], frame);
});

test("sticky packets: two frames in one chunk", () => {
  const f1 = makeReq("a", "ping");
  const f2 = makeReq("b", "info");
  const enc = Buffer.concat([encodeFrame(f1), encodeFrame(f2)]);
  const dec = new FrameDecoder();
  const frames = [...dec.push(enc)];
  assert.equal(frames.length, 2);
  assert.deepEqual(frames[0], f1);
  assert.deepEqual(frames[1], f2);
  assert.equal(dec.pendingBytes, 0);
});

test("half packets: byte-by-byte", () => {
  const frame = makeReq("a", "ping", { nested: { x: 1, y: [1, 2, 3] } });
  const enc = encodeFrame(frame);
  const dec = new FrameDecoder();
  let out: Frame[] = [];
  for (const ch of enc) {
    out = [...dec.push(Buffer.from([ch]))];
  }
  assert.equal(out.length, 1);
  assert.deepEqual(out[0], frame);
  assert.equal(dec.pendingBytes, 0);
});

test("half packets: split between header terminator and body", () => {
  const frame = makeReq("split", "uuid");
  const enc = encodeFrame(frame);
  const headerEnd = enc.indexOf(Buffer.from("\r\n\r\n")) + 4;
  const partA = enc.subarray(0, headerEnd);
  const partB = enc.subarray(headerEnd);
  const dec = new FrameDecoder();
  assert.deepEqual([...dec.push(partA)], []);
  assert.deepEqual([...dec.push(partB)], [frame]);
});

test("sticky after partial: partial + remainder-of-two -> two frames", () => {
  const f1 = makeReq("a", "ping");
  const f2 = makeReq("b", "info");
  const all = Buffer.concat([encodeFrame(f1), encodeFrame(f2)]);
  const dec = new FrameDecoder();
  // Feed half of f1 then everything else.
  const enc1 = encodeFrame(f1);
  const cut = Math.floor(enc1.length / 2);
  const collected = [...dec.push(enc1.subarray(0, cut))];
  assert.deepEqual(collected, []);
  const collected2 = [...dec.push(Buffer.concat([enc1.subarray(cut), all.subarray(enc1.length)]))];
  assert.equal(collected2.length, 2);
  assert.deepEqual(collected2[0], f1);
  assert.deepEqual(collected2[1], f2);
});

test("UTF-8 multi-byte: Chinese + emoji body, round-trip", () => {
  const frame = makeReq("utf", "echo", {
    text: "你好，desk-agent 🦀🚀 emoji split",
  });
  const enc = encodeFrame(frame);
  const dec = new FrameDecoder();
  const frames = [...dec.push(enc)];
  assert.equal(frames.length, 1);
  const data = frames[0] as { type: "req"; params: Record<string, unknown> };
  assert.equal(data.params.text, "你好，desk-agent 🦀🚀 emoji split");
});

test("UTF-8 split: feed the body byte-by-byte across reads", () => {
  const frame = makeReq("utf-split", "echo", { text: "中文 emoji 🦀" });
  const enc = encodeFrame(frame);
  const dec = new FrameDecoder();
  // Feed header in one shot, body one byte at a time.
  const headerEnd = enc.indexOf(Buffer.from("\r\n\r\n")) + 4;
  [...dec.push(enc.subarray(0, headerEnd))];
  const body = enc.subarray(headerEnd);
  let out: Frame[] = [];
  for (const ch of body) {
    out = [...dec.push(Buffer.from([ch]))];
  }
  assert.equal(out.length, 1);
  assert.equal(
    (out[0] as { params: Record<string, unknown> }).params.text,
    "中文 emoji 🦀",
  );
});

test("5 MiB payload round-trip", () => {
  const big = "x".repeat(5 * 1024 * 1024);
  const frame = makeReq("big", "upload", { data: big });
  const enc = encodeFrame(frame);
  // Verify header advertises the actual UTF-8 byte length of the JSON body
  // (NOT 5 MiB itself — JSON.stringify adds envelope bytes).
  const headerEnd = enc.indexOf(Buffer.from("\r\n\r\n"));
  const header = enc.subarray(0, headerEnd).toString("ascii");
  const m = /^Content-Length:\s*(\d+)\s*$/.exec(header);
  assert.ok(m, `expected Content-Length header, got ${JSON.stringify(header)}`);
  const advertised = Number.parseInt(m![1]!, 10);
  assert.equal(advertised, enc.length - (headerEnd + 4));
  // And it must comfortably exceed 5 MiB (the payload itself).
  assert.ok(advertised > 5 * 1024 * 1024);
  const dec = new FrameDecoder();
  const frames = [...dec.push(enc)];
  assert.equal(frames.length, 1);
  const data = (frames[0] as { params: Record<string, unknown> }).params;
  assert.equal((data.data as string).length, 5 * 1024 * 1024);
});

test("malformed Content-Length throws", () => {
  const dec = new FrameDecoder();
  assert.throws(() => {
    [...dec.push(Buffer.from("Content-Length: notanumber\r\n\r\n{}"))];
  }, /Content-Length/);
});

test("truncated body throws", () => {
  const dec = new FrameDecoder();
  // Header says 100 bytes, body is only 5. A streaming decoder must buffer
  // and wait on partial input — throwing here would be wrong. Only at the
  // end of the stream do we declare truncation as an error.
  const buf = Buffer.from(`Content-Length: 100\r\n\r\nhello`, "ascii");
  assert.deepEqual([...dec.push(buf)], []);
  assert.throws(() => dec.end(), /truncated frame/);
});

test("non-object payload throws", () => {
  const enc = Buffer.from(`Content-Length: 3\r\n\r\n123`, "ascii");
  const dec = new FrameDecoder();
  assert.throws(() => {
    [...dec.push(enc)];
  }, /not a frame object/);
});