/**
 * 设置页「日志」区的界面口径（2026-10-14）。
 *
 * 用户口径：「程序有问题都可以找到」。所以失败时这块必须说清三件事：什么状态、
 * 什么原因（lastError / 重启了几次）、侧车最后几行说了什么 —— 不许只留一句
 * 「未就绪」让用户对着空下拉干瞪眼。
 *
 * 纯逻辑（不 import react / 不碰 window），组件只管摆，单测直接盯这些函数。
 */
import type { LogDirEntry, LogNotice, LogSidecarState } from '../state/live';

/** 与主进程/侧车同口径的前缀（electron/logAccess.ts 的 STDERR_PREFIX，两侧有单测对齐）。 */
export const STDERR_PREFIX = '[sidecar stderr]';
/** 界面一次显示几行、每行多少字（摘要，不整段灌界面）。 */
export const STDERR_SHOW_LIMIT = 8;
export const STDERR_LINE_MAX = 200;

/**
 * 侧车不是 ready 时的一句话原因；正常/启动中返回空串（没事别吓人）。
 */
export function sidecarFailure(sidecar: LogSidecarState | null): string {
  if (!sidecar) return '侧车状态未知（主进程还没起来）。';
  const { state, lastError } = sidecar;
  if (state === 'ready' || state === 'starting') return '';
  const why = lastError ? `${lastError.code}：${lastError.message}` : '没记下原因';
  const attempts = Number(sidecar.restartAttempts) > 0 ? Number(sidecar.restartAttempts) : 0;
  switch (state) {
    case 'crashed':
      return attempts > 0
        ? `侧车已崩溃：${why}（重启 ${attempts} 次都没起来）。`
        : `侧车已崩溃：${why}。`;
    case 'restarting':
      return `侧车正在重启（第 ${attempts || 1} 次）：${why}。`;
    case 'version-mismatch':
      return `侧车与界面版本不匹配：${why}。请把界面和 sidecar 一起更新。`;
    case 'stopped':
      return '侧车已停止（正常退出就是这样；如果不是你关的，看下面的日志）。';
    default:
      return `侧车未就绪（${state}）：${why}。`;
  }
}

/**
 * 侧车输出摘要：剥前缀、去空行、只留最近 N 行、单行超长截断。
 * 主进程通常已经剥过一次；这里再兜一层，老版本 preload 直传的也能看。
 */
export function stderrExcerpt(
  lines: readonly string[],
  limit = STDERR_SHOW_LIMIT,
  maxChars = STDERR_LINE_MAX,
): string[] {
  const out: string[] = [];
  for (const raw of lines) {
    let s = String(raw ?? '').replace(/\s+$/, '');
    if (s.startsWith(STDERR_PREFIX)) s = s.slice(STDERR_PREFIX.length).trim();
    if (!s) continue;
    out.push(s.length > maxChars ? s.slice(0, maxChars) + '…' : s);
  }
  return limit > 0 ? out.slice(-limit) : [];
}

/** 目录这一段的小标题：两个目录说「各写各的」，共用一处说「都在这里」。 */
export function dirSubtitle(dirs: readonly LogDirEntry[]): string {
  if (dirs.length === 0) return '日志目录由主进程决定，现在还没拿到。';
  if (dirs.length === 1) return '主进程与侧车写在同一个目录 —— 点「打开日志目录」就能看到。';
  return '程序日志分两个目录：主进程一份，侧车（Python）一份。排障时两个都看，或把路径复制给别人。';
}

/** 目录还没生成时的提示（第一次写日志才建，别让用户以为路径写错了）。 */
export function missingDirHint(_entry: LogDirEntry): string {
  return '这个目录还没生成 —— 第一次写日志时会自动建出来，点「打开日志目录」也会先建好。';
}

/** 打开失败的原因翻成人话（unknown-dir 不是系统错误）。 */
export function formatLogOpenError(reason: string): string {
  const r = String(reason ?? '').trim();
  if (!r) return '';
  if (r === 'unknown-dir') return '主进程不认识这个日志目录（界面与主进程版本可能不一致）。';
  if (r === 'bad-kind') return '日志目录类型不对。';
  if (r === 'empty-path') return '日志目录路径是空的，没得打开。';
  return `打开失败：${r}`;
}

/* ── 模型接口报错：也落在日志区 ─────────────────────────────────────────
 * 「添加模型 → 对接方式下拉为空」这类失败，用户以前只看到界面空着。模型调用
 * 的报错发生在设置页各处（获取列表 / 校验 / 保存 / 列表刷新），统一报到这里，
 * 日志区摆一条，原因不用翻控制台。 */

let notice: LogNotice | null = null;
const listeners = new Set<() => void>();

/** 记一条模型接口报错并通知日志区。空消息忽略（别记一句空话）。 */
export function reportInterfaceError(label: string, message: string, at: number = Date.now()): void {
  const msg = String(message ?? '').trim();
  if (!msg) return;
  notice = { label: String(label ?? '模型接口'), message: msg, at };
  for (const cb of Array.from(listeners)) {
    try {
      cb();
    } catch {
      // 一个订阅者坏掉不影响别人（也不影响报错本身）
    }
  }
}

export function readInterfaceError(): LogNotice | null {
  return notice;
}

export function subscribeInterfaceError(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

/** 清掉（测试与「换了会话/环境」时用）。 */
export function resetInterfaceError(): void {
  notice = null;
}

/** 日志区那一行的文案；没有报错时返回空串（不画这一块）。 */
export function formatInterfaceError(n: LogNotice | null): string {
  if (!n) return '';
  const hhmm = new Date(n.at).toTimeString().slice(0, 5);
  return `模型接口报错（${hhmm}）：${n.label}：${n.message}`;
}