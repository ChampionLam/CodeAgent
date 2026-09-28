import { useEffect, useRef, useState } from 'react';
import { CodeBlock } from './CodeBlock';
import type { ReactNode } from 'react';
import { inlineSvgStyles, pageBackground, saveDataUrl, stamp, svgToPng } from './download';
import './MermaidBlock.css';

interface Props {
  code: string;
  /** Original fence children, replayed when rendering fails or the user wants the source. */
  children?: ReactNode;
}

/** mermaid is only pulled in when a diagram actually shows up. */
let mermaidReady: Promise<typeof import('mermaid').default> | null = null;
let mermaidSeq = 0;

async function ensureMermaid() {
  if (!mermaidReady) {
    mermaidReady = import('mermaid').then(mod => {
      mod.default.initialize({
        startOnLoad: false,
        // strict: no click callbacks, no HTML labels — model-provided diagrams
        // must stay inert. Never switch this to 'loose'.
        securityLevel: 'strict',
        theme: 'dark',
        fontFamily: 'inherit',
        themeVariables: {
          background: 'transparent',
          primaryColor: '#1d1d22',
          primaryTextColor: '#e8e8ea',
          primaryBorderColor: '#6366f1',
          lineColor: '#a8a8b0',
          secondaryColor: '#24242a',
          tertiaryColor: '#1a1a1e',
        },
      });
      return mod.default;
    });
  }
  return mermaidReady;
}

/**
 * ```mermaid fence → rendered SVG (default) with a source/preview toggle.
 * Parse failures fall back to the plain code block plus a one-line hint,
 * so one bad diagram never takes the whole message down.
 */
export function MermaidBlock({ code, children }: Props) {
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<'preview' | 'source'>('preview');
  const alive = useRef(true);
  const stageRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    alive.current = true;
    let cancelled = false;
    setSvg(null);
    setError(null);
    ensureMermaid()
      .then(m => {
        if (cancelled) return;
        // Streaming: a half-written diagram would throw here; that is fine, it
        // just retries on the next chunk once the source settles.
        return m.render(`md-mermaid-${Date.now()}-${mermaidSeq++}`, code);
      })
      .then(out => {
        if (!cancelled && alive.current && out) setSvg(out.svg);
      })
      .catch((err: unknown) => {
        if (cancelled || !alive.current) return;
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg.split('\n')[0].slice(0, 160) || 'Mermaid 语法错误');
      });
    return () => {
      cancelled = true;
      alive.current = false;
    };
  }, [code]);

  /** 导出用：取当前 SVG 并内联计算样式，另存出去离线打开也不掉色。 */
  const exportable = (): { w: number; h: number; text: string } | null => {
    const el = stageRef.current?.querySelector('svg');
    if (!el) return null;
    const box = el.getBoundingClientRect();
    const vb = el.viewBox?.baseVal;
    const w = Math.round(vb && vb.width ? vb.width : box.width || 800);
    const h = Math.round(vb && vb.height ? vb.height : box.height || 600);
    const clone = el.cloneNode(true) as SVGSVGElement;
    inlineSvgStyles(el, clone);
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    clone.setAttribute('width', String(w));
    clone.setAttribute('height', String(h));
    return { w, h, text: new XMLSerializer().serializeToString(clone) };
  };

  const downloadSvg = async (): Promise<void> => {
    const out = exportable();
    if (!out) return;
    await saveDataUrl(`图表-${stamp()}.svg`, 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(out.text));
  };

  const downloadPng = async (): Promise<void> => {
    const out = exportable();
    if (!out) return;
    const png = await svgToPng(out.text, out.w, out.h, pageBackground());
    if (png) await saveDataUrl(`图表-${stamp()}.png`, png);
  };

  const showFallback = mode === 'source' || (mode === 'preview' && !svg);
  return (
    <div className="mermaid-block">
      <div className="mermaid-block__bar">
        <span className="mermaid-block__label">Mermaid</span>
        {svg && !error && (
          <div className="mermaid-block__toggle" role="tablist">
            <button
              className={mode === 'preview' ? 'is-active' : ''}
              onClick={() => setMode('preview')}
              role="tab"
              aria-selected={mode === 'preview'}
            >
              预览
            </button>
            <button
              className={mode === 'source' ? 'is-active' : ''}
              onClick={() => setMode('source')}
              role="tab"
              aria-selected={mode === 'source'}
            >
              源码
            </button>
          </div>
        )}
        {svg && !error && (
          <div className="mermaid-block__acts">
            <button type="button" className="mermaid-block__act" title="下载 SVG（矢量，可无损放大）" onClick={downloadSvg}>
              SVG
            </button>
            <button type="button" className="mermaid-block__act" title="下载 PNG（2 倍分辨率）" onClick={downloadPng}>
              PNG
            </button>
          </div>
        )}
      </div>
      {showFallback ? (
        <>
          <CodeBlock>{children}</CodeBlock>
          {error && <div className="mermaid-block__hint">图表渲染失败：{error}</div>}
        </>
      ) : (
        // Rendered via innerHTML of mermaid's own SVG output (strict mode
        // sanitises it; no script execution is possible).
        <div ref={stageRef} className="mermaid-block__stage" dangerouslySetInnerHTML={{ __html: svg ?? '' }} />
      )}
    </div>
  );
}
