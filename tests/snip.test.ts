/**
 * Geometry tests for the snip (in-app screenshot) feature.
 *
 * These are the only part of the feature that can be proven headless: the
 * windows themselves need a real display, but every coordinate decision
 * (selection -> normalise, clip to the display, bitmap crop) is a
 * pure function in electron/snip.ts. The runtime failure modes we actually hit
 * live here: Windows display scaling (125%/150%), a drag that goes up-left,
 * a selection that touches the screen edge, and a selection off this display
 * the bottom of the screen.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  normalizeRect,
  intersectRects,
  offsetRect,
  toPixelRect,
  overlayBoundsFor,
  type Rect,
} from "../electron/snip";

const display1440: Rect = { x: 0, y: 0, width: 1440, height: 900 };

test("normalizeRect: a drag to the lower right keeps the origin", () => {
  assert.deepEqual(normalizeRect({ x: 10, y: 20 }, { x: 110, y: 220 }), {
    x: 10,
    y: 20,
    width: 100,
    height: 200,
  });
});

test("normalizeRect: a drag up-left normalises instead of going negative", () => {
  assert.deepEqual(normalizeRect({ x: 110, y: 220 }, { x: 10, y: 20 }), {
    x: 10,
    y: 20,
    width: 100,
    height: 200,
  });
});

test("normalizeRect: a click with no movement still yields a 1px rect", () => {
  const r = normalizeRect({ x: 5, y: 5 }, { x: 5, y: 5 });
  assert.equal(r.width, 1);
  assert.equal(r.height, 1);
  assert.ok(r.width > 0 && r.height > 0, "never hand a zero-sized crop to NativeImage");
});

test("normalizeRect: fractional drag points snap to whole pixels", () => {
  assert.deepEqual(normalizeRect({ x: 10.6, y: 20.4 }, { x: 40.2, y: 50.9 }), {
    x: 10,
    y: 20,
    width: 30,
    height: 31,
  });
});

test("intersectRects: overlap, touching edge, and disjoint", () => {
  assert.deepEqual(intersectRects({ x: 0, y: 0, width: 10, height: 10 }, { x: 5, y: 5, width: 10, height: 10 }), {
    x: 5,
    y: 5,
    width: 5,
    height: 5,
  });
  assert.equal(intersectRects({ x: 0, y: 0, width: 10, height: 10 }, { x: 10, y: 0, width: 5, height: 5 }), null);
  assert.equal(intersectRects({ x: 0, y: 0, width: 10, height: 10 }, { x: 40, y: 40, width: 5, height: 5 }), null);
});

test("offsetRect: window-local selection moves into screen coordinates", () => {
  assert.deepEqual(offsetRect({ x: 10, y: 20, width: 30, height: 40 }, 1920, 0), {
    x: 1930,
    y: 20,
    width: 30,
    height: 40,
  });
});

test("toPixelRect: at 100% scaling the crop equals the selection", () => {
  assert.deepEqual(toPixelRect({ x: 100, y: 200, width: 300, height: 400 }, display1440, 1), {
    x: 100,
    y: 200,
    width: 300,
    height: 400,
  });
});

test("toPixelRect: at 125% scaling the crop is the physical-pixel rect", () => {
  // 300 DIP wide at 1.25 is 375 device px; the start is floored and the end
  // ceiled so the crop never loses the edge the user dragged to.
  assert.deepEqual(toPixelRect({ x: 100, y: 200, width: 300, height: 400 }, display1440, 1.25), {
    x: 125,
    y: 250,
    width: 375,
    height: 500,
  });
});

test("toPixelRect: floor/ceil keeps a half-pixel edge that pure round() would drop", () => {
  const r = toPixelRect({ x: 10.4, y: 10.4, width: 3, height: 3 }, display1440, 1.25);
  assert.ok(r);
  assert.equal(r.x, 13); // floor(13.0)
  assert.equal(r.width, 4); // ceil(16.75) - 13
  assert.ok(r.x + r.width >= Math.ceil((10.4 + 3) * 1.25));
});

test("toPixelRect: a selection hanging off the screen edge is clipped, not negative", () => {
  const r = toPixelRect({ x: 1400, y: 880, width: 200, height: 200 }, display1440, 1);
  assert.deepEqual(r, { x: 1400, y: 880, width: 40, height: 20 });
});

test("toPixelRect: a selection fully off this display yields null (no crop at all)", () => {
  assert.equal(toPixelRect({ x: 2000, y: 1000, width: 50, height: 50 }, display1440, 1), null);
});

test("overlayBoundsFor: the overlay covers the whole display, taskbar included", () => {
  assert.deepEqual(overlayBoundsFor({ x: -1920, y: 0, width: 1920, height: 1080 }), {
    x: -1920,
    y: 0,
    width: 1920,
    height: 1080,
  });
});
