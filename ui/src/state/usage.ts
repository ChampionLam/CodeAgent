/**
 * 用量取数 + 显示口径。底部信息栏和「用量统计」页都从这里拿数——
 * 同一个 RPC（usage.summary）、同一套格式化函数，两边不可能对不上。
 *
 * 数据源是后端 usage_log 表（每轮对话结束写一行，来自厂商返回的 usage），
 * 不是前端按字符数估的。估不出来的地方就空着，不编。
 */
import { getAgent } from './live';

export interface UsageRow {
  model?: string;
  sessionId?: string;
  title?: string;
  updatedAt?: number;
  day?: string;
  input: number;
  output: number;
  cached: number;
  tokens: number;
  calls: number;
}

export interface UsageTotals {
  input: number;
  output: number;
  cached: number;
  tokens: number;
  calls: number;
}

export interface UsageSummaryRemote {
  total: UsageTotals;
  today: UsageTotals & { byModel?: UsageRow[] };
  window: UsageTotals;
  windowDays: number;
  byModel: UsageRow[];
  bySession: UsageRow[];
  byDay: UsageRow[];
  session?: UsageTotals & {
    byModel?: UsageRow[];
    lastTurn?: UsageRow | null;
  };
}

export async function fetchUsageSummary(
  params: { days?: number; sessionId?: string } = {}
): Promise<UsageSummaryRemote | null> {
  const agent = getAgent();
  if (!agent?.usage) return null;
  const r = await agent.usage.summary(params);
  return (r ?? null) as UsageSummaryRemote | null;
}

/**
 * token 按 2 的次方读：65536→64k、262144→256k、1048576→1M。
 * 用户口径，别出现 65.5k 这种十进制换算出来的怪数。
 *
 * **1,000,000 必须读 1M**：这里原来纯按 1024 进制算，100 万的窗口被读成
 * 「976.6k」，底部栏和模型面板的「1M」当场对不上（用户抓出来的）。
 */
export function fmtTokens(n: number | null | undefined): string {
  const v = Math.round(Number(n ?? 0));
  if (!Number.isFinite(v) || v <= 0) return '0';
  if (v >= 1024 ** 3) return trim(v / 1024 ** 3) + 'G';
  if (v % 1_048_576 === 0 && v >= 1_048_576) return `${v / 1_048_576}M`;
  if (v >= 1_000_000) return trim(v / 1_000_000) + 'M';
  if (v >= 1024) return trim(v / 1024) + 'k';
  return String(v);
}

function trim(x: number): string {
  return (Math.round(x * 10) / 10).toFixed(1).replace(/\.0$/, '');
}

export function fmtCount(n: number | null | undefined): string {
  return Number(n ?? 0).toLocaleString('en-US');
}

export type CtxLevel = 'ok' | 'warn' | 'danger';

/**
 * 当前生效的上下文窗口：会话级覆盖优先，否则模型目录默认值。
 * 输入区「上下文窗口」、底部信息栏、用量页、顶部栏共用这一个口径，
 * 免得各读各的（改之前 StatusBar/UsagePage 只看模型默认值，
 * 用户在输入区换成 256k 底部纹丝不动）。
 */
export function effectiveWindow(
  model: { contextWindow?: number } | null | undefined,
  override: number | null | undefined
): number {
  const w = Number(override ?? 0);
  if (Number.isFinite(w) && w > 0) return w;
  const m = Number(model?.contextWindow ?? 0);
  return Number.isFinite(m) && m > 0 ? m : 0;
}

export function ctxLevel(pct: number): CtxLevel {
  if (pct > 80) return 'danger';
  if (pct > 55) return 'warn';
  return 'ok';
}

/**
 * 压缩触发线（契约硬数字，不是猜的）：min(窗口 × 80%, 窗口 − 预留)，
 * 预留 = min(16384, 窗口 × 25%)。窗口 0/缺省时返回 0，界面据此不画这条线。
 */
export function compactTrigger(contextWindow: number | null | undefined): number {
  const w = Number(contextWindow ?? 0);
  if (!Number.isFinite(w) || w <= 0) return 0;
  const reserve = Math.min(16384, Math.round(w * 0.25));
  return Math.min(Math.round(w * 0.8), w - reserve);
}

/** 窗口内每一天都补齐（没数据的日子补 0），不然柱子数会忽多忽少。 */
export function fillDays(byDay: UsageRow[], days: number): UsageRow[] {
  const byDate = new Map(byDay.map(d => [String(d.day), d]));
  const out: UsageRow[] = [];
  const today = new Date();
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today.getTime() - i * 86_400_000);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
      d.getDate()
    ).padStart(2, '0')}`;
    const hit = byDate.get(key);
    out.push(
      hit ?? { day: key, input: 0, output: 0, cached: 0, tokens: 0, calls: 0 }
    );
  }
  return out;
}

/** 短日期：2026-09-25 → 09-25 */
export function shortDay(day: string | undefined): string {
  const s = String(day ?? '');
  return s.length >= 10 ? s.slice(5) : s;
}