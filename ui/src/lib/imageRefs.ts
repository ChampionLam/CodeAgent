/**
 * Image / video references in agent text and tool output.
 *
 * Agents report generated media as markdown (`![x](path)`) or, more often, as a
 * bare path in prose or in backticks. All of it should render as a picture (or
 * a player) rather than a string, without touching fenced code blocks.
 *
 * Vector and animated media are covered too: `.svg` files load in an <img>
 * (SMIL/CSS animation still runs, scripts do not), `.gif`/`.webp` animate on
 * their own, and video containers get a <video> element instead.
 */
import { unescapeRef } from '../state/live';

const EXT = 'png|jpe?g|webp|gif|bmp|avif|apng|ico|svg|svgz';
const VIDEO_EXT = 'mp4|webm|mov|m4v|ogv';
const URL_RE = new RegExp(`https?://[^\\s"']+?\\.(?:${EXT})`, 'gi');
const VIDEO_URL_RE = new RegExp(`https?://[^\\s"']+?\\.(?:${VIDEO_EXT})`, 'gi');
// The optional backticks also swallow the ones an agent likes to wrap paths in.
const PATH_RE = new RegExp(
  // 前面的 (?<![\w.]) 拦住「相对路径的尾巴」：generated/gen1.png 里的 /gen1.png
  // 不该被单独当成一条绝对路径（会出现嵌套替换的乱象）。
  "(?<![\\w.])`?(?:[A-Za-z]:[\\\\/]|\\\\\\\\|/)[^\\s\"'`()<>|]+?\\.(?:" + EXT + ")`?",
  'gi',
);
const VIDEO_PATH_RE = new RegExp(
  "(?<![\\w.])`?(?:[A-Za-z]:[\\\\/]|\\\\\\\\|/)[^\\s\"'`()<>|]+?\\.(?:" + VIDEO_EXT + ")`?",
  'gi',
);
// 相对/裸路径：workspace 相对写法（generated/gen1.png）也要认。
// resolveImageSrc 本来就按 workspaceRoot 解析相对路径，而匹配器以前只认绝对路径
// —— 口径不一致的结果是「解析得出来却不渲染」（2026-10-14 用户报图片不显示）。
// 前面的 (?<![\w:\/(\[]) 是为了不咬进 URL 片段和 markdown 链接里已写好的路径
// （https://ex.com/a/b.png 的 a/b.png、![x](generated/y.png) 的 generated/y.png）。
const REL_PATH_RE = new RegExp(
  // 片段只认「像文件名」的字符（字母/数字/._-/汉字），否则前面的中文标点会被一起吞进来
  // —— '生成好了：generated/gen1.png' 曾经整句被当成一条路径。
  // 起始位置排除 [\w.\-] 等文件名内字符：否则绝对路径的「尾巴」会被再咬一口，
  // 图片标记插进路径中间（真机 2026-10-14 出过 D:/ca-![probe.png](v3b/...) 这种垃圾）。
  "(?<![\\w.\\-:\\\\/(\\[])`?(?:\\.{1,2}[\\\\/])?(?:[\\w.\\-\\u4e00-\\u9fa5]+[\\\\/])*[\\w.\\-\\u4e00-\\u9fa5]+?\\.(?:" + EXT + ")`?",
  'gi',
);
const REL_VIDEO_PATH_RE = new RegExp(
  "(?<![\\w.\\-:\\\\/(\\[])`?(?:\\.{1,2}[\\\\/])?(?:[\\w.\\-\\u4e00-\\u9fa5]+[\\\\/])*[\\w.\\-\\u4e00-\\u9fa5]+?\\.(?:" + VIDEO_EXT + ")`?",
  'gi',
);
const SVG_RE = /<svg\b[\s\S]*?<\/svg>/gi;

export const isRemote = (p: string): boolean => /^https?:\/\//i.test(p);
export const isVideoRef = (p: string): boolean =>
  new RegExp(`\\.(?:${VIDEO_EXT})(?:$|[?#])`, 'i').test(p);

export function baseName(p: string): string {
  const parts = p.replace(/[`\\]/g, '/').split('/');
  return parts[parts.length - 1] || p;
}

/** True when a fenced block holds one whole SVG document. */
export function isSvgDoc(text: string): boolean {
  const t = text.trim();
  return /^<svg\b/i.test(t) && /<\/svg>\s*$/i.test(t);
}

/**
 * Inline SVG as a data URL. It is rendered through <img>, which is exactly what
 * keeps model-supplied SVG inert: no script execution, no external fetches.
 */
export function svgDataUrl(svg: string): string {
  // Parentheses must be escaped: an unescaped ")" would end the markdown link
  // destination this URL travels in.
  const encoded = encodeURIComponent(svg.trim()).replace(/\(/g, '%28').replace(/\)/g, '%29');
  return `data:image/svg+xml;utf8,${encoded}`;
}

/** Unique media references found in a blob of text (URLs first, then paths). */
export function imageRefsIn(text: string | undefined): string[] {
  if (!text) return [];
  const t = unescapeRef(text);
  const found = [
    ...(t.match(URL_RE) ?? []),
    ...(t.match(PATH_RE) ?? []),
    ...(t.match(REL_PATH_RE) ?? []),
    ...(t.match(VIDEO_URL_RE) ?? []),
    ...(t.match(VIDEO_PATH_RE) ?? []),
    ...(t.match(REL_VIDEO_PATH_RE) ?? []),
  ];
  return Array.from(new Set(found.map(s => s.replace(/`/g, ''))));
}

/** Rewrite bare media paths as markdown media; fenced code is left alone. */
export function preprocessImages(md: string): string {
  return md
    .split(/(```[\s\S]*?```)/g)
    .map((seg, i) => {
      // Every other segment is inside a fence: leave it for CodeBlock.
      if (i % 2 === 1) return seg;
      const inlined = unescapeRef(seg).replace(SVG_RE, svg => `![svg](${svgDataUrl(svg)})`);
      return inlined
        .replace(VIDEO_PATH_RE, (match, offset: number, whole: string) => {
          const before = whole.slice(Math.max(0, offset - 2), offset);
          if (before.endsWith('](')) return match.replace(/`/g, '');
          return `![${baseName(match)}](${match.replace(/`/g, '')})`;
        })
        .replace(PATH_RE, (match, offset: number, whole: string) => {
          const before = whole.slice(Math.max(0, offset - 2), offset);
          // Already markdown (`](path)`): just unwrap the backticks.
          if (before.endsWith('](')) return match.replace(/`/g, '');
          return `![${baseName(match)}](${match.replace(/`/g, '')})`;
        })
        .replace(REL_PATH_RE, (match, offset: number, whole: string) => {
          const before = whole.slice(Math.max(0, offset - 2), offset);
          if (before.endsWith('](')) return match.replace(/`/g, '');
          return `![${baseName(match)}](${match.replace(/`/g, '')})`;
        })
        .replace(REL_VIDEO_PATH_RE, (match, offset: number, whole: string) => {
          const before = whole.slice(Math.max(0, offset - 2), offset);
          if (before.endsWith('](')) return match.replace(/`/g, '');
          return `![${baseName(match)}](${match.replace(/`/g, '')})`;
        });
    })
    .join('');
}