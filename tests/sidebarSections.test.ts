import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PROJECT_PREVIEW_N,
  splitSidebarSessions,
  slicePreviewProjects,
  readSidebarSections,
  writeSidebarSections,
} from '../ui/src/sidebarSections';
import type { Session } from '../ui/src/types';

/** 2026-10-12 用户需求：左栏拆「会话/项目」两区，项目区短且可折叠。 */

const s = (id: string, kind?: 'chat' | 'project'): Session => ({
  id, title: `t-${id}`, createdAt: 1, updatedAt: 1, modelId: 'm', ...(kind ? { kind } : {}),
});

test('按 kind 拆两区：project 归项目，chat 与缺省 kind 都归会话', () => {
  const out = splitSidebarSessions([s('a'), s('b', 'project'), s('c', 'chat'), s('d', 'project')]);
  assert.deepEqual(out.chats.map(x => x.id), ['a', 'c']);
  assert.deepEqual(out.projects.map(x => x.id), ['b', 'd']);
});

test('kind 未知值（脏数据）也按普通会话处理，不丢行', () => {
  const dirty = s('x');
  (dirty as unknown as Record<string, unknown>).kind = 'weird';
  const out = splitSidebarSessions([dirty]);
  assert.equal(out.projects.length, 0);
  assert.equal(out.chats.length, 1);
});

test('项目区截断：只保留前 N 条，N=5', () => {
  assert.equal(PROJECT_PREVIEW_N, 5);
  const many = Array.from({ length: 9 }, (_, i) => s(`p${i}`, 'project'));
  assert.equal(slicePreviewProjects(many).length, 5);
  assert.equal(slicePreviewProjects(many.slice(0, 3)).length, 3);
});

// localStorage 在 node 里不存在 → read/write 必须静默回退默认值，不能炸。
test('localStorage 不可用时折叠偏好读写不炸、默认展开', () => {
  assert.equal(typeof globalThis.localStorage, 'undefined');
  const v = readSidebarSections();
  assert.deepEqual(v, { projectCollapsed: false });
  assert.doesNotThrow(() => writeSidebarSections({ projectCollapsed: true }));
});

test('有 localStorage 时折叠状态写进读出', () => {
  const store = new Map<string, string>();
  (globalThis as Record<string, unknown>).localStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => { store.set(k, v); },
  };
  try {
    assert.deepEqual(readSidebarSections(), { projectCollapsed: false });
    writeSidebarSections({ projectCollapsed: true });
    assert.deepEqual(readSidebarSections(), { projectCollapsed: true });
    // 损坏载荷 → 回默认，不抛
    store.set('sidebar.sections.v1', '{oops');
    assert.deepEqual(readSidebarSections(), { projectCollapsed: false });
  } finally {
    delete (globalThis as Record<string, unknown>).localStorage;
  }
});

/** 2026-10-12 用户点名：项目分区要在会话分区**上面**。
 *  渲染顺序写在 JSX 里，node:test 没有 DOM 环境，退而求其次：
 *  按源码里两个分区 <section> 出现的先后顺序断言（位置对调会红）。 */
test('Sidebar 渲染顺序：项目分区 <section> 在会话分区 <section> 之前', async () => {
  const { readFile } = await import('node:fs/promises');
  const { fileURLToPath } = await import('node:url');
  const src = await readFile(
    fileURLToPath(new URL('../ui/src/components/Sidebar.tsx', import.meta.url)),
    'utf8',
  );
  const projectsAt = src.indexOf('aria-label="项目"');
  const chatsAt = src.indexOf('aria-label="会话"');
  assert.ok(projectsAt > 0, '找不到项目分区 <section aria-label="项目">');
  assert.ok(chatsAt > projectsAt, '会话分区必须排在项目分区之后（项目在上、会话在下）');
  // 空态判定与两个分区条件渲染共用，位置不受影响；折叠持久化语义不随位置变。
  assert.ok(src.includes("localStorage") || src.includes('readSidebarSections'));
});
