/** 「从列表里挑了一个模型」之后，草稿该怎么跟着变 —— 纯逻辑，带单测。
 *
 *  2026-09-27 用户报：「我刚添加的模型，支持 1M 的，现在只有最大 128」。
 *  原因是挑模型时有两样东西没跟着走：
 *    - 显示名只在空着时才取，于是留着上一个模型的名字（配置里 model 是 A、label 是 B）；
 *    - 上下文窗口一直用厂商模板带的默认值，而这个模型的真实窗口厂商明明在同一次
 *      /models 响应里给了（context_length），后端早先把它丢掉了。
 *  现在的口径：
 *    1) 显示名：空着、或还等于上一个模型名（说明是自动带出来的）→ 跟着新模型走；
 *       用户自己起过名 → 不动。
 *    2) 上下文窗口：厂商给了这个模型的窗口 → 带出来（用户自己改过就不动）；
 *       厂商没给 → 保持现状，界面上按 CTX_LADDER 让用户挑。
 */

/** 厂商一次拉取里带的、每个模型自己的数字。查不到就没有这一项（不编数）。 */
export interface ModelMeta {
  contextLength?: number | null;
  maxCompletionTokens?: number | null;
}

/** 拉不到真实窗口时的兜底阶梯（用户 2026-09-27 口径：64/128/256/512/1M）。 */
export const CTX_LADDER = [65536, 131072, 262144, 524288, 1048576];

/** 窗口数值读成人话：1M / 128k / 64k。
 *  厂商报的 1000000 会先贴到阶梯上（差 5% 以内算同一档），免得显示成 976.6k */
export function formatCtx(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n <= 0) return '—';
  const rung = CTX_LADDER.find(v => Math.abs(n - v) / v <= 0.05);
  const v = rung ?? n;
  const trim = (x: number) => (Number.isInteger(x) ? String(x) : x.toFixed(1));
  if (v >= 1048576) return `${trim(v / 1048576)}M`;
  if (v >= 1024) return `${trim(v / 1024)}k`;
  return String(v);
}

export interface DraftLike {
  model: string;
  label: string;
  contextWindow: string;
}

/** 挑中 id 之后要改的草稿字段（其余字段保持不动）。 */
export function followPickedModel(
  draft: DraftLike,
  id: string,
  meta: Record<string, ModelMeta> | null | undefined,
  ctxTouched: boolean
): Partial<DraftLike> {
  const out: Partial<DraftLike> = { model: id };
  if (!draft.label || draft.label === draft.model) out.label = id;
  const ctx = meta?.[id]?.contextLength;
  if (!ctxTouched && typeof ctx === 'number' && ctx > 0) out.contextWindow = String(ctx);
  return out;
}

/** 「上下文窗口」这格下面的说明文案（纯逻辑，带单测）。
 *  三种情况分开说清楚，免得出现「说明写 128k、格子填 262144」这种自相矛盾：
 *  1) 厂商给了窗口、格子里的值跟它一致 → 说这是自动带出来的；
 *  2) 厂商给了、格子里的值不一样（用户自己改的）→ 两个值都摆出来；
 *  3) 厂商没给 → 明说按阶梯挑。
 */
export function ctxHintText(value: string, vendorCtx: number | null | undefined): string {
  const has = typeof vendorCtx === 'number' && Number.isFinite(vendorCtx) && vendorCtx > 0;
  if (!has) return '厂商没给这个模型的窗口，按 64k / 128k / 256k / 512k / 1M 挑一个，也可以直接填。';
  const cur = Number(value);
  if (Number.isFinite(cur) && cur > 0 && cur !== vendorCtx) {
    return `厂商窗口 ${formatCtx(vendorCtx)}（${vendorCtx}）；当前填的是 ${formatCtx(cur)}（${cur}）。`;
  }
  return `厂商给的窗口：${formatCtx(vendorCtx)}（${vendorCtx}）——自动带出来的，压缩触发线据此计算，可以改。`;
}

/** 面板/表单里「上下文窗口」的档位：整条阶梯照给（64k / 128k / 256k / 512k / 1M），
 *  传进来的那个窗口值本身不在阶梯里就补在末尾。
 *
 *  早先是拿「模型配置里那条自己的窗口」当上限封顶的，结果配置错一次就把面板也锁死：
 *  用户 2026-09-27 那条 1M 模型配置里残留 131072，面板就只给 64k / 128k 两个档，
 *  看着像「面板不支持 1M」。面板里的值本来就是**会话级覆盖**（不写回配置），
 *  所以不再拿配置封顶——用户明确要的就是这条阶梯。 */
export function windowChoices(max?: number | null): number[] {
  const out = [...CTX_LADDER];
  // 上限不在阶梯上才补在末尾。**按显示读数去重**：配置里存 1000000（厂商报的 1M）
  // 时，它和阶梯里的 1M 档读出来都是 1M，补上去面板里就会出现两个「1M」
  // （用户 2026-09-27 报「怎么有两个1M」）。
  if (max != null && max > 0 && !out.some(v => formatCtx(v) === formatCtx(max))) out.push(max);
  return out;
}

/** 拉完列表之后要改的草稿字段：厂商认识这个模型、格子里的窗口又和厂商给的不一样，
 *  就用厂商的（用户这一轮自己改过就不动）。
 *
 *  补这条是因为「重新配置」这条路：编辑已有模型时表单里已经填着模型名，不会经过
 *  「从列表里挑」那一步，窗口就一直留着厂商模板的默认值（用户 2026-09-27 报：
 *  「我重新配置怎么只有这两个档位」——配置里那条始终是 131072）。 */
export function followFetchedMeta(
  draft: DraftLike,
  meta: Record<string, ModelMeta> | null | undefined,
  ctxTouched: boolean
): Partial<DraftLike> {
  if (!draft.model || ctxTouched) return {};
  const ctx = meta?.[draft.model]?.contextLength;
  if (typeof ctx !== 'number' || ctx <= 0) return {};
  return String(ctx) === draft.contextWindow ? {} : { contextWindow: String(ctx) };
}
