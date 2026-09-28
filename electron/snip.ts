/**
 * 截图功能的几何纯函数（无 Electron 依赖，headless 可测）。
 *
 * 坐标系约定：
 *   - screen.* / display.bounds / 遮罩窗口位置：全部是 DIP（逻辑像素），
 *     Electron 的 BrowserWindow setBounds 全按 DIP 算，这一层不掺物理像素。
 *   - desktopCapturer 抓出来的 NativeImage 是物理像素位图，所以裁切发生在
 *     「位图像素」坐标系：选择框 DIP × scaleFactor 之后才对得上位图。
 *   转换全部收口在这一个文件里，别处不许自己乘 scaleFactor。
 */

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Pt {
  x: number;
  y: number;
}

/**
 * 拖拽的两个端点 -> 规整矩形（宽高非负、整数）。
 * 用户可能往左上拖：端点谁小取谁，尺寸取差的绝对值。
 */
export function normalizeRect(a: Pt, b: Pt): Rect {
  const x = Math.min(a.x, b.x);
  const y = Math.min(a.y, b.y);
  return {
    x: Math.floor(x),
    y: Math.floor(y),
    width: Math.max(1, Math.round(Math.abs(b.x - a.x))),
    height: Math.max(1, Math.round(Math.abs(b.y - a.y))),
  };
}

/** 两个矩形的交集；不相交返回 null。 */
export function intersectRects(a: Rect, b: Rect): Rect | null {
  const x = Math.max(a.x, b.x);
  const y = Math.max(a.y, b.y);
  const right = Math.min(a.x + a.width, b.x + b.width);
  const bottom = Math.min(a.y + a.height, b.y + b.height);
  if (right <= x || bottom <= y) return null;
  return { x, y, width: right - x, height: bottom - y };
}

/** r 平移 offset（遮罩窗口内的局部坐标 -> 屏幕全局坐标）。 */
export function offsetRect(r: Rect, dx: number, dy: number): Rect {
  return { x: r.x + dx, y: r.y + dy, width: r.width, height: r.height };
}

/**
 * 选区（屏幕 DIP）-> 位图像素裁切框。
 * 用 Math.floor 起点 + Math.ceil 终点，避免浮点 scale 下把边线裁掉半像素。
 * 越界部分先与屏幕 bounds 求交（多显示器下选区只落在这一块屏幕里，
 * 因为遮罩窗口就贴着这块屏幕建，但求交后这里才敢说「不会拿到负坐标」）。
 */
export function toPixelRect(sel: Rect, display: Rect, scaleFactor: number): Rect | null {
  const clipped = intersectRects(sel, display);
  if (!clipped) return null;
  const x0 = Math.floor(clipped.x * scaleFactor);
  const y0 = Math.floor(clipped.y * scaleFactor);
  const x1 = Math.ceil((clipped.x + clipped.width) * scaleFactor);
  const y1 = Math.ceil((clipped.y + clipped.height) * scaleFactor);
  const w = x1 - x0;
  const h = y1 - y0;
  if (w < 1 || h < 1) return null;
  return { x: x0, y: y0, width: w, height: h };
}

/**
 * 遮罩窗口的 bounds：给光标所在屏幕盖一层「全屏遮罩」。
 * 直接用 display.bounds（DIP），Windows 下 bounds 含任务栏 —— 盖得住整块屏。
 */
export function overlayBoundsFor(display: Rect): Rect {
  return { ...display };
}
