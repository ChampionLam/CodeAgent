import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CTX_LADDER, ctxHintText, formatCtx, followFetchedMeta, followPickedModel, windowChoices } from '../ui/src/pages/modelDraft';

/** 用户 2026-09-27：新加的 1M 模型被存成 128k。
 *  挑模型之后显示名和上下文窗口必须跟着走，用户自己改过的不能被覆盖。 */

const draft = (over: Partial<{ model: string; label: string; contextWindow: string }> = {}) => ({
  model: '', label: '', contextWindow: '128000', ...over
});

test('阶梯就是用户点名的那五档', () => {
  assert.deepEqual(CTX_LADDER, [65536, 131072, 262144, 524288, 1048576]);
  assert.deepEqual(CTX_LADDER.map(formatCtx), ['64k', '128k', '256k', '512k', '1M']);
});

test('挑中模型：窗口按厂商给的带出来', () => {
  const meta = { 'nvidia/nemotron-3-ultra-550b-a55b:free': { contextLength: 1000000 } };
  assert.deepEqual(
    followPickedModel(draft({ model: 'aion-labs/aion-2.0', label: 'aion-labs/aion-2.0' }),
      'nvidia/nemotron-3-ultra-550b-a55b:free', meta, false),
    { model: 'nvidia/nemotron-3-ultra-550b-a55b:free', label: 'nvidia/nemotron-3-ultra-550b-a55b:free', contextWindow: '1000000' });
});

test('厂商没给窗口：不动用户眼前的值，等他自己按阶梯挑', () => {
  const out = followPickedModel(draft({ model: 'x', label: 'x' }), 'y', {}, false);
  assert.equal(out.model, 'y');
  assert.equal(out.contextWindow, undefined);
});

test('用户自己改过窗口就不许被覆盖', () => {
  const meta = { y: { contextLength: 1000000 } };
  const out = followPickedModel(draft({ model: 'x', label: 'x', contextWindow: '65536' }), 'y', meta, true);
  assert.equal(out.contextWindow, undefined);
});

test('用户起过的显示名不动', () => {
  const out = followPickedModel(draft({ model: 'x', label: '我的模型' }), 'y', {}, false);
  assert.equal(out.label, undefined);
});

test('显示名是自动带出来的（等于上一个模型名）就跟着换', () => {
  assert.equal(followPickedModel(draft({ model: 'x', label: 'x' }), 'y', {}, false).label, 'y');
  assert.equal(followPickedModel(draft({ model: 'x', label: '' }), 'y', {}, false).label, 'y');
});

test('窗口读数：厂商报的 1000000 读成 1M，不是 976.6k', () => {
  assert.equal(formatCtx(1000000), '1M');
  assert.equal(formatCtx(131072), '128k');
  assert.equal(formatCtx(128000), '128k');
  assert.equal(formatCtx(2000000), '1.9M');
  assert.equal(formatCtx(null), '—');
  assert.equal(formatCtx(0), '—');
});

test('窗口说明文案：跟厂商一致 / 用户改过 / 厂商没给，三种都说清', () => {
  assert.match(ctxHintText('1000000', 1000000), /厂商给的窗口：1M（1000000）/);
  assert.match(ctxHintText('131072', 1000000), /厂商窗口 1M（1000000）；当前填的是 128k（131072）/);
  assert.match(ctxHintText('131072', undefined), /厂商没给这个模型的窗口/);
  assert.match(ctxHintText('', null), /厂商没给这个模型的窗口/);
});

test('面板档位：整条阶梯照给，不在阶梯上的窗口值补在末尾（不拿配置封顶）', () => {
  assert.deepEqual(windowChoices(131072), [...CTX_LADDER]);
  // 1000000 和阶梯里的 1M 档读数一样（formatCtx 带 5% 吸附）→ 不重复补
  assert.deepEqual(windowChoices(1000000), [...CTX_LADDER]);
  assert.deepEqual(windowChoices(null), [...CTX_LADDER]);
  assert.deepEqual(windowChoices(40000), [...CTX_LADDER, 40000]);
});

test('拉完列表：按厂商值把窗口填对；用户改过或厂商没给就不动', () => {
  const d = { model: 'a/b:free', label: 'a/b:free', contextWindow: '131072' };
  assert.deepEqual(followFetchedMeta(d, { 'a/b:free': { contextLength: 1000000 } }, false), { contextWindow: '1000000' });
  assert.deepEqual(followFetchedMeta(d, { 'a/b:free': { contextLength: 1000000 } }, true), {});
  assert.deepEqual(followFetchedMeta(d, { 'a/b:free': {} }, false), {});
  assert.deepEqual(followFetchedMeta(d, {}, false), {});
  assert.deepEqual(followFetchedMeta({ ...d, model: '' }, { 'a/b:free': { contextLength: 1000000 } }, false), {});
  assert.deepEqual(followFetchedMeta({ ...d, contextWindow: '1000000' }, { 'a/b:free': { contextLength: 1000000 } }, false), {});
});

test('档位不重复补：上限和某一档显示同一个读数时只留那一档（用户 2026-09-27 报「怎么有两个1M」）', () => {
  const out = windowChoices(1000000);
  assert.deepEqual(out, [...CTX_LADDER]);
  assert.equal(new Set(out.map(formatCtx)).size, out.length);
  const out3 = windowChoices(3 * 1024 * 1024);
  assert.equal(out3.length, CTX_LADDER.length + 1);
  assert.equal(out3[out3.length - 1], 3 * 1024 * 1024);
  assert.equal(new Set(out3.map(formatCtx)).size, out3.length);
});
