import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import { remarkCite } from './remarkCite';
import remarkBreaks from 'remark-breaks';
import rehypeHighlight from 'rehype-highlight';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';
import { CodeBlock } from './CodeBlock';
import { MermaidBlock } from './MermaidBlock';
import { ChartBlock, parseChartOption } from './ChartBlock';
import { resolveImageSrc } from '../../state/live';
import { baseName, imageRefsIn, isSvgDoc, preprocessImages, svgDataUrl } from '../../lib/imageRefs';
import { MediaRef } from './MediaRef';
import './MarkdownView.css';

/** Allow data:/file:/http(s); the only protocol worth refusing is javascript:. */
const urlTransform = (url: string): string => (/^\s*javascript:/i.test(url) ? '' : url);

interface Props {
  content: string;
  streaming?: boolean;
  /** User bubbles: tighter typography, no streaming cursor. */
  variant?: 'assistant' | 'user';
}

/** Flatten React children to plain text (used to inspect a fenced block). */
function flattenText(node: unknown): string {
  if (node === null || node === undefined || node === false) return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(flattenText).join('');
  const el = node as { props?: { children?: unknown } };
  return el.props ? flattenText(el.props.children) : '';
}

/**
 * A fenced block whose entire content is one image reference. Models happily
 * wrap a freshly written PNG path in ``` fences, which renders as code text
 * instead of a picture; treat that case as an image.
 */
function loneImageRef(text: string): string | null {
  const refs = imageRefsIn(text);
  if (refs.length !== 1) return null;
  const stripped = text
    .trim()
    .replace(/^!\[[^\]]*\]\(/, '')
    .replace(/\)$/, '')
    .replace(/^`+|`+$/g, '')
    .trim();
  if (!stripped) return null;
  return stripped === refs[0] || stripped === refs[0].replace(/\\/g, '/') ? refs[0] : null;
}

/** Pull the language name out of a `pre > code.language-xxx` tree. */
function fenceLang(children: unknown): string {
  const arr = Array.isArray(children) ? children : [children];
  const first = arr.find(c => c && typeof c === 'object' && 'props' in (c as object));
  const cls = ((first as { props?: { className?: string } })?.props?.className) ?? '';
  const m = /language-([\w+-]+)/.exec(cls);
  return m ? m[1].toLowerCase() : '';
}

/** Cite-shaped link text: [1] / [1,2] / [1-3] / [1, 3-5]. */
const CITE_TEXT = /^\s*\d+(?:\s*[,，]\s*\d+)*(?:\s*[-–~]\s*\d+)?\s*$/;

/**
 * Footnote-style reference definitions at the end of a message:
 *   [1]: https://example.com  "title"
 * are consumed here and folded into a map, so a bare [1] in the text can be
 * turned into a cite chip even when the model wrote it as plain markdown
 * reference links.
 */
/**
 * 模型习惯把整段回答（或其中一段）再套一层 `````markdown` 围栏，里面才是它自己
 * 的 ```mermaid / ```echarts / ```json。那层壳对用户毫无意义，而且会把里面的围栏
 * 整段吞成一个代码块（2026-09-26 真机实测：mermaid 因此不渲染）。
 * 所以只拆 markdown / md 标签的围栏，让里面的围栏按正常 markdown 解析；
 * 真正贴代码的围栏一个字都不动。流式中间态（还没闭合）原样留着，别提前动它。
 */
function unwrapMarkdownFences(md: string): string {
  const lines = md.split('\n');
  const out: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const open = /^\s{0,3}(`{3,})\s*(?:markdown|md)\s*$/i.exec(lines[i]);
    if (!open) {
      out.push(lines[i]);
      continue;
    }
    const fenceLen = open[1].length;
    const close = new RegExp('^\\s{0,3}`{' + fenceLen + ',}\\s*$');
    let j = i + 1;
    const body: string[] = [];
    for (; j < lines.length; j++) {
      if (close.test(lines[j])) break;
      body.push(lines[j]);
    }
    if (j >= lines.length) {
      out.push(lines[i]);
      continue;
    }
    out.push(...body);
    i = j;
  }
  return out.join('\n');
}

function extractRefDefs(md: string): { text: string; defs: Map<string, { url: string; title?: string }> } {
  const defs = new Map<string, { url: string; title?: string }>();
  const re = /^\s{0,3}\[(\d+)\]:\s*(\S+)(?:\s+(?:"([^"]*)"|'([^']*)'|\(([^)]*)\)))?\s*$/gm;
  let out = md.replace(re, (_m, id: string, url: string, dq?: string, sq?: string, par?: string) => {
    defs.set(id, { url, title: dq ?? sq ?? par });
    return '';
  });
  // 参考列表写法（模型最爱这么写）：1. [标题](url) / 1. 标题 (url) / 1. 标题：url
  // 这些行原样留在正文里，只是顺便把「编号 → URL」记下来，好让正文里的 [1] 点得出去。
  const lists = [
    /^\s{0,3}(\d{1,3})\s*[.、)]\s*\[([^\]]+)\]\((\S+?)\)\s*$/gm,
    /^\s{0,3}(\d{1,3})\s*[.、)]\s*([^\n(]{1,80}?)\s*[（(]\s*(https?:\/\/\S+?)\s*[)）]\s*$/gm,
    /^\s{0,3}(\d{1,3})\s*[.、)]\s*(?:\[([^\]]+)\]\s*)?[：:—-]?\s*(https?:\/\/\S+)\s*$/gm,
  ];
  for (const lre of lists) {
    for (const m of md.matchAll(lre)) {
      if (!defs.has(m[1])) defs.set(m[1], { url: m[3], title: m[2] || undefined });
    }
  }
  return { text: out, defs };
}

/**
 * Markdown renderer for assistant (and user) output.
 * - GFM (tables, task lists, strikethrough, footnotes)
 * - highlight.js syntax highlighting via rehype-highlight
 * - KaTeX math: inline $...$ and block $$...$$
 * - ```mermaid / ```echarts / ```chart (and chart-shaped ```json) fences
 * - [1] cite chips rendered as superscript links
 * - code blocks render through <CodeBlock/> for chrome (lang badge + copy)
 */
export function MarkdownView({ content, streaming, variant = 'assistant' }: Props) {
  // Bare image paths become real pictures so generated files are visible.
  const prepped = unwrapMarkdownFences(preprocessImages(content));
  const { text, defs } = extractRefDefs(prepped);
  // While streaming, an unclosed $$ block would render as a broken formula:
  // pad it so KaTeX sees a closed (if empty) expression until more text lands.
  const body = streaming ? padOpenMath(text) : text;

  return (
    <div className={`md md--${variant}`}>
      <ReactMarkdown
        remarkPlugins={[
          remarkBreaks,
          remarkGfm,
          [remarkMath, { singleDollarTextMath: true }],
          [remarkCite, defs],
        ]}
        rehypePlugins={[
          [rehypeHighlight, { detect: true, ignoreMissing: true }],
          [rehypeKatex, { throwOnError: false, errorColor: 'var(--accent)', strict: 'ignore' }],
        ]}
        // react-markdown strips data: URLs by default, which would blank out an
        // inline SVG; only javascript: is worth blocking here.
        urlTransform={urlTransform}
        components={{
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          pre: ({ children }: any) => {
            const text2 = flattenText(children);
            const lang = fenceLang(children);
            // A fence holding one SVG document or one image path: show the
            // picture, keep the code (models like to hand back a bare path).
            if (isSvgDoc(text2)) {
              return (
                <>
                  <img className="md-image" src={svgDataUrl(text2)} alt="inline svg" />
                  <CodeBlock>{children}</CodeBlock>
                </>
              );
            }
            const ref = loneImageRef(text2);
            if (ref && resolveImageSrc(ref)) {
              return (
                <>
                  <MediaRef value={ref} />
                  <CodeBlock>{children}</CodeBlock>
                </>
              );
            }
            // Language-routed fences.
            if (lang === 'mermaid') return <MermaidBlock code={text2}>{children}</MermaidBlock>;
            if (lang === 'echarts' || lang === 'chart') return <ChartBlock code={text2}>{children}</ChartBlock>;
            if (lang === 'json') {
              const parsed = parseChartOption(text2);
              if (parsed.ok) return <ChartBlock code={text2}>{children}</ChartBlock>;
              return <CodeBlock>{children}</CodeBlock>;
            }
            return <CodeBlock>{children}</CodeBlock>;
          },
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          img: ({ src, alt }: any) => <MediaRef value={typeof src === 'string' ? src : ''} fallback={alt} />,
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          code: ({ className, children, ...rest }: any) => {
            const isBlock = /language-/.test(className ?? '');
            if (isBlock) {
              return <code className={className} {...rest}>{children}</code>;
            }
            return <code className="md-inline-code" {...rest}>{children}</code>;
          },
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          a: ({ href, children, ...rest }: any) => {
            const label = flattenText(children);
            // [1](url) / [1,2](url) / [1-3](url) → superscript cite chip.
            if (CITE_TEXT.test(label) && typeof href === 'string' && href) {
              return (
                <a
                  className="md-cite"
                  href={href}
                  title={href}
                  target="_blank"
                  rel="noreferrer noopener"
                  onClick={openInSystemBrowser}
                >
                  {label}
                </a>
              );
            }
            // Bare [n] whose definition sits at the message foot.
            const m = /^\s*\[(\d+)\]\s*$/.exec(label);
            const def = m ? defs.get(m[1]) : undefined;
            if (def) {
              return (
                <a
                  className="md-cite"
                  href={def.url}
                  title={def.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  onClick={openInSystemBrowser}
                >
                  {m![1]}
                </a>
              );
            }
            return (
              <a
                {...rest}
                href={href}
                target="_blank"
                rel="noreferrer noopener"
                onClick={openInSystemBrowser}
              >
                {children}
              </a>
            );
          },
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          table: ({ children }: any) => (
            <div className="md-table-wrap"><table>{children}</table></div>
          )
        }}
      >
        {body}
      </ReactMarkdown>
      {streaming && <span className="streaming-cursor" aria-hidden />}
    </div>
  );
}

/**
 * Keep half-written math from breaking the render while a message streams:
 * if the number of $$ delimiters is odd, append a closing one; a trailing
 * unclosed single $ is dropped so prose like "价格是 5$" keeps flowing.
 */
function padOpenMath(md: string): string {
  const withoutFences = md.split(/(```[\s\S]*?```)/g).filter((_, i) => i % 2 === 0).join('');
  const dollars = (withoutFences.match(/\$\$/g) ?? []).length;
  let out = md;
  if (dollars % 2 === 1) out += '\n$$';
  // Trailing lone $ right before the end would open an inline formula that
  // never closes — strip just that one.
  out = out.replace(/\$(?!\$)[^\n$]*$/, s => s.replace(/\$$/, ''));
  return out;
}

/**
 * External links must leave the Electron window, not navigate it. The preload
 * bridge exposes `openExternal`; in a plain browser there is nothing to do
 * (target=_blank already handles it) and the default click runs.
 */
function openInSystemBrowser(e: React.MouseEvent<HTMLAnchorElement>) {
  const agent = (window as unknown as { agent?: { openExternal?: (url: string) => void } }).agent;
  if (agent?.openExternal) {
    e.preventDefault();
    agent.openExternal(e.currentTarget.href);
  }
}
