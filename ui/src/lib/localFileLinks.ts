/**
 * 助手消息里的「交付文件」抽取（2026-09-27 用户要求）。
 *
 * 用户原话：「不是所有的本地文件都转换成这样,你只需要在最后结尾加上这个文件就好」
 * —— 所以这里**不做**正文内联链接：只把 [[file: 路径]] 标记从正文里剥掉，
 * 把文件交给消息末尾的附件卡（打开 / 打开文件夹）去显示，并且去重。
 *
 * 只认标记、不认裸路径：标记是模型明确表示「这个文件是交付物」的方式，
 * 正文里随口提到的路径保持纯文本，不喧宾夺主。
 * 代码块与行内代码里的标记也保持原样（那是示例代码，不是交付物）。
 */
const MARKER_RE = /\[\[\s*file\s*[:：]\s*([^\]]+?)\s*\]\]/g;

export function fileNameOf(path: string): string {
  const parts = path.replace(/\\/g, '/').split('/').filter(Boolean);
  return parts[parts.length - 1] || path;
}

/** 按扩展名猜一个 kind，给附件卡选图标用。 */
export function kindOf(path: string): string {
  const ext = (fileNameOf(path).match(/\.([A-Za-z0-9]+)$/) || [, ''])[1].toLowerCase();
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'].includes(ext)) return 'image';
  if (['py', 'ts', 'tsx', 'js', 'jsx', 'json', 'sh', 'ps1', 'bat', 'cs', 'java', 'go', 'rs', 'sql', 'yml', 'yaml', 'toml', 'html', 'css'].includes(ext)) return 'code';
  if (['md', 'txt', 'log', 'csv', 'tsv', 'ini', 'env'].includes(ext)) return 'text';
  if (['doc', 'docx', 'pdf', 'xls', 'xlsx', 'ppt', 'pptx'].includes(ext)) return 'doc';
  return 'file';
}

/** 只在「非代码」片段里找标记：行内反引号与 ``` 围栏整段跳过。 */
function splitCode(text: string): string[] {
  const parts: string[] = [];
  const re = /(```[\s\S]*?```|`[^`\n]*`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    parts.push(text.slice(last, m.index));
    parts.push(m[0]);
    last = m.index + m[0].length;
  }
  parts.push(text.slice(last));
  return parts;
}

export type ExtractedFiles = { text: string; paths: string[] };

/** 抽取交付文件，并从正文里摘掉标记（返回的 text 就是最终正文）。 */
export function extractLocalFiles(md: string): ExtractedFiles {
  if (!md || !md.includes('[[file')) return { text: md, paths: [] };
  const paths: string[] = [];
  const parts = splitCode(md);
  const out = parts.map((chunk, i) => {
    if (i % 2 === 1) return chunk;                       // 代码原样
    return chunk.replace(MARKER_RE, (_all, raw) => {
      const p = String(raw || '').trim();
      if (p && !paths.includes(p)) paths.push(p);
      return '';
    });
  }).join('');
  // 标记被摘掉后可能留下多余空行，收一下
  const text = out.replace(/[ \t]+$/gm, '').replace(/\n{3,}/g, '\n\n').trimEnd();
  return { text, paths };
}
