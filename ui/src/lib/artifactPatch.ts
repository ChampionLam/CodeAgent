import type { Message } from '../types';

/**
 * 把本轮产出文件贴到「最后一条 assistant 消息」上（2026-10-15）。
 *
 * 原来只贴「正在流式的那条」：可是 chat.artifacts 是在落库之后才推的，
 * 那时 chat.done 已经到了、流式标记已经被清掉，patch 直接落空 ——
 * 症状是同一会话里只有最后那一轮的文件卡实时可见，更早的轮次要刷新才出来
 * （真机实测：实时 1 张、刷新后 3 张）。
 *
 * 规则：优先按 event 里带的 messageId 命中；没有就找列表里最后一条 assistant。
 * 找不到就原样返回（不制造假消息）。
 */
export function applyArtifacts(
  list: Message[],
  artifacts: Message['artifacts'],
  messageId?: string
): Message[] {
  if (!artifacts || !artifacts.length) return list;
  let idx = -1;
  if (messageId) {
    idx = list.findIndex(m => m.id === messageId);
  }
  if (idx === -1) {
    for (let i = list.length - 1; i >= 0; i -= 1) {
      if (list[i]?.role === 'assistant') { idx = i; break; }
    }
  }
  if (idx === -1) return list;
  const next = [...list];
  next[idx] = { ...next[idx], artifacts };
  return next;
}
