/**
 * 流式期间的 mermaid 渲染调度（2026-10-14 真机回归的护栏）。
 *
 * 真机症状：模型还在吐 ```mermaid 内容时，同一张图被反复抹掉重画 —— 正文高度
 * 塌缩又撑开，用户正在看的位置（视口/光标）被拽回图里；渲染失败那几次 mermaid
 * 还会把「Syntax error in text」错误图留在页面里，越积越多。
 *
 * 口径：源码稳定 RENDER_DEBOUNCE_MS 才渲染；同一段源码只渲染一次；卸载后不再渲染。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { MermaidScheduler, RENDER_DEBOUNCE_MS } from '../ui/src/lib/mermaidScheduler';

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

test('流式：连续的 delta 只在源码稳定后渲染一次', async () => {
  const drawn: string[] = [];
  const sched = new MermaidScheduler((code) => drawn.push(code), 30);
  const chunks = ['graph', 'graph TD;\n', 'graph TD;\nA-->', 'graph TD;\nA-->B',
                  'graph TD;\nA-->B;'];
  for (const chunk of chunks) {
    sched.update(chunk);
    await sleep(5);
  }
  assert.equal(drawn.length, 0, '源码还在变就不该画（旧实现在这里画了 5 次）');
  await sleep(80);
  assert.deepEqual(drawn, ['graph TD;\nA-->B;'], '稳定后只画最后那一版');
  assert.equal(sched.pending, false);
});

test('源码不再变化时，重复 update 不重复渲染', async () => {
  const drawn: string[] = [];
  const sched = new MermaidScheduler((code) => drawn.push(code), 10);
  sched.update('graph TD;\nA-->B;');
  await sleep(40);
  sched.update('graph TD;\nA-->B;');
  await sleep(40);
  assert.equal(drawn.length, 1, '同一段源码渲染一次就够');
});

test('源码又变了才重画一次', async () => {
  const drawn: string[] = [];
  const sched = new MermaidScheduler((code) => drawn.push(code), 10);
  sched.update('graph TD;\nA-->B;');
  await sleep(40);
  sched.update('graph TD;\nA-->C;');
  await sleep(40);
  assert.deepEqual(drawn, ['graph TD;\nA-->B;', 'graph TD;\nA-->C;']);
});

test('卸载后不再渲染（组件已离开 DOM 就别再动它）', async () => {
  const drawn: string[] = [];
  const sched = new MermaidScheduler((code) => drawn.push(code), 30);
  sched.update('graph TD;\nA-->B;');
  sched.dispose();
  await sleep(60);
  assert.deepEqual(drawn, [], 'dispose 之后排队中的渲染必须取消');
});

test('防抖窗口默认可感知（流式 chunk 间隔远小于它）', () => {
  assert.ok(RENDER_DEBOUNCE_MS >= 100 && RENDER_DEBOUNCE_MS <= 600,
            `防抖窗口 ${RENDER_DEBOUNCE_MS}ms 不合理：太短会每帧重画，太长图表出来太慢`);
});