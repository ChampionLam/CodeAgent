// remark 插件：把正文里带来源定义的 [n] 标记换成链接节点，交给 MarkdownView 的
// a 组件渲染成可点落的上标芯片。
//
// 口径（2026-09-26 用户钦定）：[1] 当可点链接、指向来源 URL。
//
// 只认「有来源定义」的 [n]：文末 [1]: url 或参考列表 1. [标题](url) 都算。
// 没有定义的一律保持原样 —— 免得把 a[1] 这种数组下标误判成引用。

export interface CiteDef {
  url: string;
  title?: string;
}

interface Node {
  type: string;
  value?: string;
  url?: string;
  title?: string;
  children?: Node[];
}

/** [1] / [1,2] / [1, 3-5] —— 只要整组都有定义才转换，有一个没定义就整组不动。 */
const MARK = /\[(\d+(?:\s*[,，]\s*\d+(?:\s*[-–~]\s*\d+)?)*)\]/;
const RANGE = /^(\d+)\s*[-–~]\s*(\d+)$/;

function resolve(id: string, defs: Map<string, CiteDef>): CiteDef | undefined {
  const direct = defs.get(id);
  if (direct) return direct;
  const r = RANGE.exec(id);
  return r ? defs.get(r[1]) : undefined;
}

/** 把一段文字拆成 [纯文本, 链接, 纯文本...]；没有任何标记命中就返回 null（不动它）。 */
function convert(value: string, defs: Map<string, CiteDef>): Node[] | null {
  if (!value.includes('[')) return null;
  const parts: Node[] = [];
  let rest = value;
  let changed = false;
  while (rest) {
    const m = MARK.exec(rest);
    if (!m) {
      parts.push({ type: 'text', value: rest });
      break;
    }
    const before = rest.slice(0, m.index);
    if (before) parts.push({ type: 'text', value: before });
    const ids = m[1].split(/[,，]/).map((s) => s.trim());
    const hits = ids.map((id) => resolve(id, defs));
    if (hits.every(Boolean)) {
      changed = true;
      const first = hits[0] as CiteDef;
      parts.push({
        type: 'link',
        url: first.url,
        title: first.title,
        children: [{ type: 'text', value: ids.join(',') }],
      });
    } else {
      parts.push({ type: 'text', value: m[0] });
    }
    rest = rest.slice(m.index + m[0].length);
  }
  return changed ? parts : null;
}

function walk(node: Node, defs: Map<string, CiteDef>, inLink: boolean): void {
  if (!node.children?.length) return;
  const out: Node[] = [];
  for (const child of node.children) {
    if (child.type === 'text' && !inLink) {
      const conv = convert(child.value ?? '', defs);
      if (conv) {
        out.push(...conv);
        continue;
      }
    }
    walk(child, defs, inLink || child.type === 'link');
    out.push(child);
  }
  node.children = out;
}

export function remarkCite(defs: Map<string, CiteDef>) {
  return (tree: Node) => {
    if (defs.size > 0) walk(tree, defs, false);
  };
}