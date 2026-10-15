/**
 * 实时附件要贴到「最后一条 assistant 消息」上，而不是只贴流式那条（2026-10-15）。
 *
 * 真机症状：一个会话里走了四轮（生图/生视频/两个图表），只看到最后那一轮的
 * 文件卡实时出现，更早的轮次要刷新才补出来（实时 1 张、刷新后 3 张）。
 * 根因：chat.artifacts 在落库后才推，那时 chat.done 已到、流式标记已清，
 * 原来那种「只贴正在流式的那条」的写法直接落空。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { applyArtifacts } from '../ui/src/lib/artifactPatch';
import { kindOf, mediaKindOf } from '../ui/src/lib/localFileLinks';

const ART = [{ path: 'D:/ws/generated/g.png', name: 'g.png', size: 10, kind: 'image' as const }];

test('贴给最后一条 assistant 消息（哪怕它已经不在流式）', () => {
  const list = [
    { id: 'u1', role: 'user' as const, content: '生成一张图', createdAt: 1 },
    { id: 'a1', role: 'assistant' as const, content: '', createdAt: 2 },
  ];
  const out = applyArtifacts(list as never, ART as never);
  assert.equal(out[1].artifacts?.length, 1);
  assert.equal(out[0].artifacts, undefined);
});

test('后面的 user 消息不会被误挂', () => {
  const list = [
    { id: 'a1', role: 'assistant' as const, content: '图生成好了', createdAt: 2 },
    { id: 'u2', role: 'user' as const, content: '再来一张', createdAt: 3 },
  ];
  const out = applyArtifacts(list as never, ART as never);
  assert.equal(out[0].artifacts?.length, 1);
  assert.equal(out[1].artifacts, undefined);
});

test('给了 messageId 就按 id 命中', () => {
  const list = [
    { id: 'a1', role: 'assistant' as const, content: '早的那条', createdAt: 1 },
    { id: 'a2', role: 'assistant' as const, content: '晚的那条', createdAt: 2 },
  ];
  const out = applyArtifacts(list as never, ART as never, 'a1');
  assert.equal(out[0].artifacts?.length, 1);
  assert.equal(out[1].artifacts, undefined);
});

test('没有 assistant 消息就原样返回，不造假消息', () => {
  const list = [{ id: 'u1', role: 'user' as const, content: '你好', createdAt: 1 }];
  const out = applyArtifacts(list as never, ART as never);
  assert.equal(out.length, 1);
  assert.equal(out[0].artifacts, undefined);
});

test('空附件不改动列表', () => {
  const list = [{ id: 'a1', role: 'assistant' as const, content: 'x', createdAt: 1 }];
  assert.equal(applyArtifacts(list as never, [] as never), list);
});

test('老附件行（kind=other）也能按扩展名认出视频', () => {
  // 2026-10-15 之前落库的行 kind 是 'other'，只认 kind 会退化成文件图标。
  assert.equal(mediaKindOf({ kind: 'other', path: 'D:/ws/gen/old.mp4' }), 'video');
  assert.equal(mediaKindOf({ kind: 'video', path: 'x.bin' }), 'video');
  assert.equal(mediaKindOf({ kind: '', path: 'a.png' }), 'image');
  assert.equal(mediaKindOf({ kind: 'doc', path: 'a.pdf' }), null);
});

test('视频/音频各自成类（卡上要出播放器）', () => {
  assert.equal(kindOf('no-time.mp4'), 'video');
  assert.equal(kindOf('a.MOV'), 'video');
  assert.equal(kindOf('voice.mp3'), 'audio');
  assert.equal(kindOf('shot.png'), 'image');
});
