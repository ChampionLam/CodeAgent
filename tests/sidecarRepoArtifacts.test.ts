/**
 * 历史消息必须带出「附件」（2026-10-14 真机）。
 *
 * 生图/生视频的产物已经落库（messages.artifacts）也已经在协议里回传
 * （sidecar._message_wire），但界面刷新后还是不显示 —— 断在这层的映射：
 * MessageRow 没有 artifacts 字段，映射时被丢掉；而且「没有正文的行直接
 * 不渲染」这条会把只有附件的行也一起扔掉。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { SidecarConversationRepo } from '../ui/src/state/sidecarRepo';

function repoWith(rows: unknown[]) {
  const bridge = {
    list: async () => ({ sessions: [] }),
    create: async () => ({ id: 's', title: 't' }),
    rename: async () => ({}),
    remove: async () => ({}),
    messages: async () => ({ messages: rows }),
    search: async () => ({ hits: [] }),
  };
  return new SidecarConversationRepo(bridge as never);
}

test('历史里带附件的 assistant 消息不能把附件丢掉', async () => {
  const art = [{ path: 'D:/ws/generated/g.png', name: 'g.png', size: 123, kind: 'image' }];
  const repo = repoWith([{ id: 'm1', role: 'assistant', content: '图生成好了。', artifacts: art }]);
  const msgs = await repo.loadMessages('s');
  assert.equal(msgs.length, 1);
  assert.deepEqual((msgs[0] as { artifacts?: unknown }).artifacts, art);
});

test('只有附件、没有正文的行也要保留', async () => {
  const art = [{ path: 'D:/ws/generated/v.mp4', name: 'v.mp4', size: 9, kind: 'other' }];
  const repo = repoWith([{ id: 'm2', role: 'assistant', content: '', artifacts: art }]);
  const msgs = await repo.loadMessages('s');
  assert.equal(msgs.length, 1);
  assert.equal((msgs[0] as { artifacts?: Array<{ name: string }> }).artifacts?.[0]?.name, 'v.mp4');
});
