/** 厂商内部 key -> 界面上给人看的品牌名。
 *
 * 为什么要单开一个文件：provider 是**对接线路**的 key（zhipu / minimax-cn /
 * dashscope-bailian …），直接摆到界面上出现过 hg 这种自己编的路由名
 * （用户 2026-09-25 纠正：「我用的百炼的终点」）。命名口径抄 Qoder：写对接方，
 * 不写模型出品方；同一家的不同线路要分得出来（百炼公有云 vs 百炼 MAAS 专属网关）。
 */
export const VENDOR_LABELS: Record<string, string> = {
  'dashscope-bailian': '阿里云百炼',
  'minimax-cn': 'MiniMax 中国',
  'minimax-intl': 'MiniMax 国际',
  deepseek: 'DeepSeek',
  moonshot: 'Kimi',
  zhipu: '智谱',
  siliconflow: 'SiliconFlow',
  openrouter: 'OpenRouter',
  openai: 'OpenAI',
  'ollama-local': 'Ollama 本地',
  custom: '自定义',
};

/** 认不出来的 key 原样返回，不编名字。
 *
 * 传了 baseUrl 时按终点再细分：`.maas.aliyuncs.com` 是百炼的专属网关（MAAS 部署），
 * 跟公有云 compatible-mode 不是一条线，价格/限额都不一样，界面上别混成一个名字。
 */
export function vendorLabel(key?: string | null, baseUrl?: string | null): string {
  const k = String(key ?? '').trim();
  const host = String(baseUrl ?? '');
  if (k === 'dashscope-bailian' && /(^|\.)maas\./i.test(host)) return '阿里云百炼 MAAS';
  return VENDOR_LABELS[k] ?? k;
}
