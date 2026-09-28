/**
 * 附件类型判定与文案（界面层/状态层共用）。
 *
 * 2026-09-26 起「附件」不再只有图片：文档（PDF/Word/Excel/PPT/ODT/RTF/EPUB/ipynb）
 * 与纯文本（日志/配置/源码/CSV —— 对应 sidecar 的 STDLIB_TEXT_EXTENSIONS）都算附件。
 * 三者必须分开渲染：图片画缩略图，其它画文件图标；
 * 一律用 <img> 去加载日志会得到裂图（图标裂开 + 看着不居中）。
 */

export type AttachmentKind = 'image' | 'document' | 'text';

/** 图片：能画缩略图的格式。 */
export const IMAGE_EXT = 'png|jpe?g|gif|webp';
/** 文档：sidecar 走提取层（anydoc / pymupdf / openpyxl / python-docx）。 */
export const DOC_EXT =
  'pdf|docx?m?|pptx?m?|ppsx?m?|pot|xls[bm]?|odt|ods|odp|rtf|epub|ipynb';
/** 纯文本：sidecar 直接按文本读（UTF-8/GBK/UTF-16），不需要转换库。 */
export const TEXT_EXT =
  'txt|text|log|out|err|trace|md|markdown|rst|json|jsonl|ndjson|' +
  'csv|tsv|yaml|yml|toml|ini|cfg|conf|properties|xml|html|htm|css|sql|' +
  'py|pyi|sh|bash|zsh|bat|cmd|ps1|psm1|js|mjs|cjs|ts|tsx|jsx|java|kt|go|rs|' +
  'c|h|cc|cpp|hpp|cs|rb|php|lua|r|swift|scala|vue|svelte|diff|patch';

const IMAGE_RE = new RegExp(`\\.(?:${IMAGE_EXT})$`, 'i');
const DOC_RE = new RegExp(`\\.(?:${DOC_EXT})$`, 'i');
const TEXT_RE = new RegExp(`\\.(?:${TEXT_EXT})$`, 'i');

/** 拖入 / 选中 / 粘贴时要不要收这个文件。 */
export const ATTACH_RE = new RegExp(`\\.(?:${IMAGE_EXT}|${DOC_EXT}|${TEXT_EXT})$`, 'i');

/** 文件选择器的 accept 清单：图片 + 可提取文字的文档 + 纯文本。 */
export const ATTACH_ACCEPT =
  'image/png,image/jpeg,image/gif,image/webp,' +
  'application/pdf,application/rtf,application/epub+zip,' +
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document,' +
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,' +
  'application/vnd.openxmlformats-officedocument.presentationml.presentation,' +
  'application/vnd.oasis.opendocument.text,' +
  'application/vnd.oasis.opendocument.spreadsheetml.sheet,' +
  'application/vnd.oasis.opendocument.presentation,' +
  '.pdf,.doc,.docm,.ppt,.pps,.pot,.pptx,.pptm,.ppsx,.ppsm,.xls,.xlsm,.xlsb,' +
  '.odt,.ods,.odp,.rtf,.epub,.ipynb,' +
  '.log,.txt,.md,.markdown,.json,.jsonl,.csv,.tsv,.yaml,.yml,.toml,.ini,.cfg,' +
  '.conf,.xml,.html,.sql,.py,.js,.mjs,.ts,.tsx,.sh,.bash,.bat,.cmd,.ps1,.diff,.patch';

/** 认不出的后缀返回 null（调用方自己决定收不收）。 */
export function attachmentKind(p: string): AttachmentKind | null {
  if (IMAGE_RE.test(p)) return 'image';
  if (DOC_RE.test(p)) return 'document';
  if (TEXT_RE.test(p)) return 'text';
  return null;
}

/** 能不能画缩略图。 */
export const isImagePath = (p: string): boolean => attachmentKind(p) === 'image';

/** 是不是「要读内容」的附件（文档 + 纯文本），发送提示用。 */
export const isDocPath = (p: string): boolean => {
  const k = attachmentKind(p);
  return k === 'document' || k === 'text';
};

/**
 * 附件摘要，写进用户消息体（见 Contract v4 §reduceUserMessage）。
 * 老版本一律写「N 张图片」，拖进日志也这么写 —— 名不副实，这里按类型分别计数。
 */
export function attachmentSummary(paths: string[]): string {
  const count = { image: 0, document: 0, text: 0, other: 0 };
  for (const p of paths) count[attachmentKind(p) ?? 'other'] += 1;
  const parts: string[] = [];
  if (count.image) parts.push(`${count.image} 张图片`);
  if (count.document) parts.push(`${count.document} 个文档`);
  if (count.text) parts.push(`${count.text} 个文本文件`);
  if (count.other) parts.push(`${count.other} 个文件`);
  return parts.join(' + ');
}