import { useEffect, useMemo, useRef, useState } from 'react';
import { CodeBlock } from './CodeBlock';
import type { ReactNode } from 'react';
import { pageBackground, saveDataUrl, stamp } from './download';
import './ChartBlock.css';

interface Props {
  code: string;
  /** Original fence children, replayed in the fallback code block. */
  children?: ReactNode;
}

/**
 * Strip every function-valued (or otherwise non-JSON) field from a parsed
 * ECharts option: only plain data survives — ECharts configs are never
 * executed, they are only *rendered*.
 */
function sanitize(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sanitize);
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      // typeof 'function' is impossible after JSON.parse, but crafted keys
      // like "series[0].onClick" style names hold no power either — dropping
      // any non-plain value keeps the surface minimal.
      if (typeof v === 'function') continue;
      out[k] = sanitize(v);
    }
    return out;
  }
  return value;
}

/** JSON option that clearly looks like an ECharts config, not a random object. */
export function parseChartOption(code: string): { ok: true; option: Record<string, unknown> } | { ok: false; error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(code);
  } catch {
    return { ok: false, error: '不是合法的 JSON' };
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, error: '顶层必须是 ECharts option 对象' };
  }
  const opt = parsed as Record<string, unknown>;
  if (!('series' in opt) || !Array.isArray(opt.series) || opt.series.length === 0) {
    return { ok: false, error: '缺少 series 数组' };
  }
  // 判据（2026-09-26 用户口径）：图表要有坐标轴；饼图/关系图/仪表盘/雷达这类
  // 天生无轴的图单独放行，其余没有 xAxis/yAxis 的一律当普通 JSON，免得误伤数据。
  const AXISLESS = ['pie', 'graph', 'gauge', 'radar'];
  const allAxisless = opt.series.every(
    (s) => s && typeof s === 'object' && AXISLESS.includes(String((s as { type?: unknown }).type)),
  );
  if (!('xAxis' in opt) && !('yAxis' in opt) && !allAxisless) {
    return { ok: false, error: '缺少坐标轴（xAxis/yAxis）' };
  }
  return { ok: true, option: sanitize(opt) as Record<string, unknown> };
}

type EChartsCore = typeof import('echarts/core');

/**
 * ```echarts / ```chart / chart-shaped ```json fence → live ECharts canvas.
 * echarts/core only, with the handful of components we actually render.
 */
export function ChartBlock({ code, children }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);
  const parsed = useMemo(() => parseChartOption(code), [code]);

  useEffect(() => {
    if (!parsed.ok) return;
    let disposed = false;
    let chart: import('echarts/core').ECharts | null = null;
    let cleanup: (() => void) | undefined;

    import('echarts/core')
      .then(async (echarts: EChartsCore) => {
        const [
          { BarChart, LineChart, PieChart },
          { GridComponent, TooltipComponent, LegendComponent, TitleComponent, DatasetComponent },
          { CanvasRenderer },
        ] = await Promise.all([
          import('echarts/charts'),
          import('echarts/components'),
          import('echarts/renderers'),
        ]);
        if (disposed || !hostRef.current) return;
        echarts.use([
          BarChart,
          LineChart,
          PieChart,
          GridComponent,
          TooltipComponent,
          LegendComponent,
          TitleComponent,
          DatasetComponent,
          CanvasRenderer,
        ]);

        // Dark theme read from the app's own CSS variables, so the chart
        // follows the interface instead of hardcoding colours.
        const css = getComputedStyle(document.documentElement);
        const v = (name: string, fallback: string): string =>
          (css.getPropertyValue(name) || '').trim() || fallback;
        const baseOption = {
          backgroundColor: 'transparent',
          textStyle: { color: v('--text-secondary', '#a8a8b0') },
          color: ['#6366f1', '#8b8ef8', '#4ade80', '#fbbf24', '#f87171', '#22d3ee'],
        };

        chart = echarts.init(hostRef.current, null, { renderer: 'canvas' });
        chart.setOption({ ...baseOption, ...parsed.option } as Parameters<typeof chart.setOption>[0]);

        const axisStyle = {
          axisLabel: { color: v('--text-muted', '#6f6f79') },
          axisLine: { lineStyle: { color: v('--border-strong', '#36363c') } },
          splitLine: { lineStyle: { color: v('--border-subtle', '#222226') } },
        };
        // Merge axis colours without clobbering user-provided sub-styles.
        for (const key of ['xAxis', 'yAxis'] as const) {
          const ax = (parsed.option as Record<string, unknown>)[key];
          if (Array.isArray(ax)) {
            chart?.setOption({
              [key]: ax.map((a: unknown) => ({ ...(a as object), ...axisStyle })),
            } as Parameters<typeof chart.setOption>[0]);
          }
        }

        // Redraw on container resize (window, sidebar collapse, …).
        const ro = new ResizeObserver(() => chart?.resize());
        ro.observe(hostRef.current);
        cleanup = () => ro.disconnect();
      })
      .catch(() => {
        /* lazy import failure leaves the code block below in place */
      });

    return () => {
      disposed = true;
      cleanup?.();
      chart?.dispose();
      chart = null;
    };
  }, [parsed]);

  /** 导出 PNG：铺一层页面底色再合成，避免导出带透明通道。 */
  const downloadPng = async (): Promise<void> => {
    const src = hostRef.current?.querySelector('canvas');
    if (!src) return;
    const out = document.createElement('canvas');
    out.width = src.width;
    out.height = src.height;
    const ctx = out.getContext('2d');
    if (!ctx) return;
    ctx.fillStyle = pageBackground();
    ctx.fillRect(0, 0, out.width, out.height);
    ctx.drawImage(src, 0, 0);
    await saveDataUrl(`图表-${stamp()}.png`, out.toDataURL('image/png'));
  };

  /** 复制图表配置（清洗过的 option JSON），方便贴回代码里改。 */
  const copyOption = async (): Promise<void> => {
    const text = JSON.stringify((parsed as { option?: unknown }).option ?? {}, null, 2);
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch { /* 剪贴板不可用时静默 */ }
      ta.remove();
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1600);
  };

  if (!parsed.ok) {
    return (
      <>
        <CodeBlock>{children}</CodeBlock>
        <div className="chart-block__hint">未能渲染为图表：{parsed.error}</div>
      </>
    );
  }

  return (
    <div className="chart-block">
      <div className="chart-block__bar">
        <span className="chart-block__label">图表</span>
        <div className="chart-block__acts">
          <button type="button" className="chart-block__act" title="下载 PNG（2 倍分辨率）" onClick={downloadPng}>
            下载
          </button>
          <button
            type="button"
            className={`chart-block__act ${copied ? 'is-copied' : ''}`}
            title="复制图表配置（JSON）"
            onClick={copyOption}
          >
            {copied ? '已复制' : '复制配置'}
          </button>
        </div>
      </div>
      <div ref={hostRef} className="chart-block__host" />
    </div>
  );
}
