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
  "`?(?:[A-Za-z]:[\\\\/]|\\\\\\\\|/)[^\\s\"'`()<>|]+?\\.(?:" + EXT + ")`?",
  'gi',
);
const VIDEO_PATH_RE = new RegExp(
  "`?(?:[A-Za-z]:[\\\\/]|\\\\\\\\|/)[^\\s\"'`()<>|]+?\\.(?:" + VIDEO_EXT + ")`?",
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
    ...(t.match(VIDEO_URL_RE) ?? []),
    ...(t.match(VIDEO_PATH_RE) ?? []),
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
        });
    })
    .join('');
}