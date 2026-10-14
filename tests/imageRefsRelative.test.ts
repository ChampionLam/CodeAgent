/**
 * 相对路径的媒体引用（2026-10-14，用户报「图片不能直接输出」）。
 *
 * 真机验证结论：渲染链本身是通的 —— 正文里给绝对路径、给 ![](绝对路径) 都能出图
 * （隔离实例 + 桩模型实测，img 的 naturalWidth > 0）。真正断在两处口径不一致：
 *   * 工具卡把产物媒体渲染在「展开详情」里面（用户看不到）；
 *   * 匹配器 imageRefsIn 只认绝对路径，而解析器 resolveImageSrc 本来就按
 *     workspaceRoot 解析相对路径 —— 相对路径因此永远走不到渲染。
 * 这里锁住第二处的口径。
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { imageRefsIn, preprocessImages } from '../ui/src/lib/imageRefs';

test('workspace 相对路径也算媒体引用', () => {
  assert.deepEqual(imageRefsIn('生成好了：generated/gen1.png'), ['generated/gen1.png']);
  assert.deepEqual(imageRefsIn('./out/a.jpg'), ['./out/a.jpg']);
  assert.deepEqual(imageRefsIn('out/deep/nested/a.webp'), ['out/deep/nested/a.webp']);
});

test('正文里的相对路径会转成 markdown 图片', () => {
  const out = preprocessImages('看图 generated/gen1.png 就行');
  assert.match(out, /!\[gen1\.png\]\(generated\/gen1\.png\)/);
});

test('不咬 URL 里的路径片段', () => {
  const refs = imageRefsIn('https://ex.com/a/b.png');
  assert.ok(!refs.includes('a/b.png'), 'URL 里的片段不该被当成 workspace 相对路径');
});

test('非媒体扩展名一动不动', () => {
  assert.deepEqual(imageRefsIn('见 notes.md 和 main.py 和 build/out.txt'), []);
});

test('围栏代码块里的路径不转成图片', () => {
  const out = preprocessImages('```\ngenerated/x.png\n```');
  assert.ok(!out.includes('!['), '代码块里的路径交给代码块渲染');
});

test('绝对路径不会被相对路径规则再咬一口（真机 2026-10-14 出过嵌套替换）', () => {
  const out = preprocessImages('绝对路径：D:\\work\\out\\probe.png');
  assert.equal((out.match(/!\[/g) || []).length, 1, '只该包一层图片标记');
  assert.ok(!out.includes('ca-!['), '不能把图片标记插进路径中间');
  assert.ok(out.includes('![probe.png](D:\\work\\out\\probe.png)'), out);
});

test('带反引号的相对路径也能认（模型爱这么写）', () => {
  assert.deepEqual(imageRefsIn('结果见 `generated/gen2.png`'), ['generated/gen2.png']);
});