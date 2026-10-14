/**
 * 发送队列（2026-10-14）：模型跑长任务时用户仍然能说话。
 *
 * 修复前的口径：ui/src/components/chat/Composer.tsx:127/181 按 isStreaming
 * 把「发送」闸死 —— 模型一忙，输入框能打字但按回车没反应，用户想补充一句
 * 「顺带把按钮也改了」只能等这轮完，或者点「停止」把整轮作废重来。
 *
 * 现在的口径（用户是主控，AI 只是执行者）：输入框全程可用。忙时这句话进
 * 队列（FIFO），本轮一结束自动送出；「停止」只停当前这轮，不动队列 ——
 * 排队的话还会接着发，用户排队排的不是「等模型」，是「按顺序说完整」。
 *
 * 这里只放纯逻辑（不 import react / 不碰 window，跟 pages/logPanel.ts 同一
 * 套路）：判定、出入队、界面文案。React 接线在 store.tsx / Composer.tsx，
 * 由 ui 的 tsc 兜底。
 */

/** 排在队里的一句话：原话 + 所属会话（自动送出时必须回它自己的会话）。 */
export interface QueuedTurn {
  id: string;
  sessionId: string;
  text: string;
  /** 这句排队话带的附件（图片/文档路径），送出时原样带上。 */
  images?: string[];
}

/**
 * 生成中（isStreaming）输入要发送 → 排队。队列已有存货时，即使当下
 * isStreaming=false（比如刚点过「停止」）也得排：先来后到，新话不许插队。
 */
export function shouldQueueForSend(isStreaming: boolean, queueLength: number): boolean {
  return isStreaming || queueLength > 0;
}

/** 一句话够不够格入队：跟空闲时的发送口径一致（有字或有附件）。 */
export function isSendable(text: string, images?: string[]): boolean {
  return text.trim().length > 0 || (images?.length ?? 0) > 0;
}

/**
 * 从队头取一句。返回 { turn, rest }；空队列返回 null。
 * 不改动传入数组（React 状态不可变，调用方 setState 才能触发重渲染）。
 */
export function dequeue(
  queue: readonly QueuedTurn[]
): { turn: QueuedTurn; rest: QueuedTurn[] } | null {
  if (queue.length === 0) return null;
  const [turn, ...rest] = queue;
  return { turn, rest };
}

/** 按 id 移除一句排队话（用户点「撤销排队」）。找不到时原样返回。 */
export function removeQueued(queue: readonly QueuedTurn[], id: string): QueuedTurn[] {
  const next = queue.filter(t => t.id !== id);
  return next.length === queue.length ? [...queue] : next;
}

/** 排队项 id（React key 用，别拿数组下标当 key）。 */
let seq = 0;
export function nextQueuedId(now: number = Date.now()): string {
  return `q-${now}-${++seq}`;
}

/** 输入框上方那条排队提示（中文）。没排队时返回空串，不占界面。 */
export function queueHint(count: number): string {
  if (count <= 0) return '';
  return count === 1
    ? '已排队 1 条：本轮结束后自动发送'
    : `已排队 ${count} 条：本轮结束后自动发送`;
}
