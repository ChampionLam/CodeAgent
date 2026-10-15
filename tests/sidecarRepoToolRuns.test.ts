/**
 * 历史消息要还原「工具卡」（2026-10-14）。
 *
 * 后端早就在 sessions.messages 里一起回传 toolCalls 行（状态/结果/耗时），
 * 界面这边从来没读这个字段 —— 所以对话里完全看不到工具执行过程，
 * 模型调了什么、成没成、报什么错，只有日志里有。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { SidecarConversationRepo } from '../ui/src/state/sidecarRepo';

function repoWith(rows: unknown[], calls: unknown[]) {
  const bridge = {
    list: async () => ({ sessions: [] }),
    create: async () => ({ id: 's', title: 't' }),
    rename: async () => ({}),
    remove: async () => ({}),
    messages: async () => ({ messages: rows, toolCalls: calls }),
    search: async () => ({ hits: [] }),
  };
  return new SidecarConversationRepo(bridge as never);
}

const ROWS = [
  { id: 'm1', role: 'assistant', content: '' },
  { id: 'm2', role: 'assistant', content: '图生成好了。' },
];

test('成功/失败的调用都挂到对应消息上，状态映射正确', async () => {
  const calls = [
    { id: 't1', message_id: 'm1', tool_name: 'run_shell',
      arguments_json: '{"command":"dir"}', result_json: '{"content":"ok"}',
      decision: 'allow', duration_ms: 120, started_at: '2026-10-14 10:00:00' },
    { id: 't2', message_id: 'm2', tool_name: 'image_generate',
      arguments_json: '{"prompt":"cat"}', error_json: '{"code":"AUTH","message":"401"}',
      decision: 'allow', duration_ms: 300, started_at: '2026-10-14 10:00:01' },
  ];
  const msgs = await repoWith(ROWS, calls).loadMessages('s');
  const byId = new Map(msgs.map(m => [m.id, m]));
  const first = (byId.get('m1') as { toolCalls?: Array<{ name: string; status: string; args: Record<string, unknown> }> }).toolCalls;
  const second = (byId.get('m2') as { toolCalls?: Array<{ name: string; status: string; args: Record<string, unknown> }> }).toolCalls;
  assert.equal(first?.length, 1);
  assert.equal(first?.[0]?.name, 'run_shell');
  assert.equal(first?.[0]?.status, 'success');
  assert.deepEqual(first?.[0]?.args, { command: 'dir' });
  assert.equal(second?.[0]?.name, 'image_generate');
  assert.equal(second?.[0]?.status, 'failed');
  assert.deepEqual(second?.[0]?.args, { prompt: 'cat' });
});

test('没有工具调用的消息不带 toolCalls 键', async () => {
  const msgs = await repoWith(ROWS, []).loadMessages('s');
  assert.equal((msgs[0] as { toolCalls?: unknown }).toolCalls, undefined);
});

test('被拒绝的调用显示为 rejected', async () => {
  const calls = [{ id: 't3', message_id: 'm1', tool_name: 'run_shell',
                   arguments_json: '{}', decision: 'deny', duration_ms: 2,
                   started_at: '2026-10-14 10:00:00' }];
  const msgs = await repoWith(ROWS, calls).loadMessages('s');
  const tc = (msgs[0] as { toolCalls?: Array<{ status: string }> }).toolCalls;
  assert.equal(tc?.[0]?.status, 'rejected');
});
