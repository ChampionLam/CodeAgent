/**
 * 图表导出的公共部分：把 data URL 交给主进程弹「另存为」，拿不到 Electron 桥就
 * 退回浏览器下载（纯前端调试时也能用）。
 * 只处理 data: URL —— 主进程侧同样只认 data:，两边都不给任意路径写盘的机会。
 */

type SaveResult = { ok: boolean; path?: string; reason?: string };
type SaveBridge = { saveFile?: (name: string, dataUrl: string) => Promise<SaveResult> };

/** HH-MM-SS，用来给导出文件编号（跟截图附件名一个规矩）。 */
export function stamp(): string {
  const d = new Date();
  const p = (n: number): string => String(n).padStart(2, '0');
  return `${p(d.getHours())}-${p(d.getMinutes())}-${p(d.getSeconds())}`;
}

export async function saveDataUrl(name: string, dataUrl: string): Promise<boolean> {
  const bridge = (window as unknown as { agent?: SaveBridge }).agent;
  if (bridge?.saveFile) {
    const res = await bridge.saveFile(name, dataUrl);
    return Boolean(res && res.ok);
  }
  const a = document.createElement('a');
  a.href = dataUrl;
  a.download = name;
  a.click();
  return true;
}

/**
 * mermaid 的配色来自 CSS 变量，直接序列化出去会变成黑白，所以导出前把计算样式
 * 内联到克隆节点上（只内联跟画图有关的那几项）。
 */
const INLINE_PROPS = [
  'fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'stroke-linecap',
  'font-family', 'font-size', 'font-weight', 'font-style', 'opacity',
  'color', 'text-anchor', 'dominant-baseline',
];

export function inlineSvgStyles(source: SVGSVGElement, clone: SVGSVGElement): void {
  const src: Element[] = [source, ...Array.from(source.querySelectorAll('*'))];
  const dst: Element[] = [clone, ...Array.from(clone.querySelectorAll('*'))];
  for (let i = 0; i < src.length && i < dst.length; i++) {
    const cs = getComputedStyle(src[i]);
    let style = '';
    for (const prop of INLINE_PROPS) {
      const val = cs.getPropertyValue(prop);
      if (val) style += `${prop}:${val};`;
    }
    dst[i].setAttribute('style', style);
  }
}

/** SVG 文本 → PNG data URL（<img> + canvas，2 倍分辨率，字体走系统字体）。 */
export async function svgToPng(
  svgText: string,
  width: number,
  height: number,
  background: string,
): Promise<string | null> {
  const img = new Image();
  const ok = new Promise<boolean>((resolve) => {
    img.onload = () => resolve(true);
    img.onerror = () => resolve(false);
  });
  img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgText);
  if (!(await ok)) return null;
  const cv = document.createElement('canvas');
  cv.width = Math.max(1, Math.round(width * 2));
  cv.height = Math.max(1, Math.round(height * 2));
  const ctx = cv.getContext('2d');
  if (!ctx) return null;
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, cv.width, cv.height);
  ctx.drawImage(img, 0, 0, cv.width, cv.height);
  return cv.toDataURL('image/png');
}

/** 导出铺底用的页面背景色，免得透明 PNG 贴到浅色文档里糊掉。 */
export function pageBackground(): string {
  const root = getComputedStyle(document.documentElement);
  const named = (root.getPropertyValue('--bg-base') || '').trim();
  if (named) return named;
  const body = getComputedStyle(document.body).backgroundColor;
  return body && body !== 'rgba(0, 0, 0, 0)' ? body : '#101014';
}
