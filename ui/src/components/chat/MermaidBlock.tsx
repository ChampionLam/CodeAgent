import { useEffect, useRef, useState } from 'react';
import { CodeBlock } from './CodeBlock';
import type { ReactNode } from 'react';
import { MermaidScheduler } from '../../lib/mermaidScheduler';
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
        // 渲染失败时别把 mermaid 那张「Syntax error in text」的错误图塞进页面：
        // 流式期间它会被反复留下，越积越多（真机截图里一次出现 5 个），而且它们
        // 堆在正文后面会把布局顶位移。失败由我们自己的提示行交代就够。
        suppressErrorRendering: true,
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
  const schedulerRef = useRef<MermaidScheduler | null>(null);

  /** 真渲染一次；由调度器在「源码稳定」之后调用（见 mermaidScheduler）。 */
  const renderOnce = (src: string): void => {
    void ensureMermaid()
      .then(m => {
        if (!alive.current) return null;
        // 稳定 id：以前拼了 Date.now()，同一张图每次都被当成新图渲染。
        return m.render(`md-mermaid-${++mermaidSeq}`, src);
      })
      .then(out => {
        if (!alive.current || !out) return;
        setSvg(out.svg);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!alive.current) return;
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg.split('\n')[0].slice(0, 160) || 'Mermaid 语法错误');
      });
  };

  useEffect(() => {
    alive.current = true;
    const sched = new MermaidScheduler(renderOnce);
    schedulerRef.current = sched;
    return () => {
      sched.dispose();
      schedulerRef.current = null;
      alive.current = false;
    };
    // renderOnce 只读写 ref 和 setState，调度器不需要重建。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 流式期间 code 每个 delta 都变：旧写法一进来就 setSvg(null) + 重画，
  // 同一张图被反复抹掉重建 —— 高度塌缩又撑开，视口和光标就被拽回图里
  // （2026-10-14 真机复现）。现在交给调度器：源码稳定才画，且同一段只画一次。
  useEffect(() => {
    schedulerRef.current?.update(code);
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
        {svg && (
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
        {svg && (
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
        <>
          <div ref={stageRef} className="mermaid-block__stage" dangerouslySetInnerHTML={{ __html: svg ?? '' }} />
          {/* 有新源码在流式、或刚渲染失败时，上一张成功的图继续留在页面上：
              抹掉就会高度塌缩，用户正在看的位置会被顶走。失败原因照说，不闷着。 */}
          {error && <div className="mermaid-block__hint">图表渲染失败：{error}</div>}
        </>
      )}
    </div>
  );
}
