import { test } from 'node:test';
import assert from 'node:assert/strict';
import { extractLocalFiles, fileNameOf, kindOf } from '../ui/src/lib/localFileLinks.ts';

test('标记从正文里摘掉，文件抽出来', () => {
  const r = extractLocalFiles('已写入 D:\\desk-agent\\workspace\\a.py，内容 ok\n\n[[file: D:\\desk-agent\\workspace\\a.py]]');
  assert.equal(r.paths.length, 1);
  assert.equal(r.paths[0], 'D:\\desk-agent\\workspace\\a.py');
  assert.ok(!r.text.includes('[[file'), r.text);
  assert.ok(r.text.includes('已写入'), r.text);
});

test('同一个文件出现两次只留一个', () => {
  const r = extractLocalFiles('[[file: D:\\x\\a.txt]] 和 [[file: D:\\x\\a.txt]]');
  assert.equal(r.paths.length, 1);
});

test('多个文件按出现顺序', () => {
  const r = extractLocalFiles('[[file: D:\\x\\a.py]] 中间 [[file: D:\\x\\b.md]]');
  assert.deepEqual(r.paths, ['D:\\x\\a.py', 'D:\\x\\b.md']);
});

test('裸路径不抽、也不变链接（用户要求：别把所有本地文件都转）', () => {
  const r = extractLocalFiles('文件在 C:\\Users\\me\\aiohttp_demo.py 里面');
  assert.deepEqual(r.paths, []);
  assert.equal(r.text, '文件在 C:\\Users\\me\\aiohttp_demo.py 里面');
});

test('代码块里的标记不动', () => {
  const fenced = extractLocalFiles('```\n[[file: D:\\x\\a.py]]\n```');
  assert.deepEqual(fenced.paths, []);
  assert.ok(fenced.text.includes('[[file'), fenced.text);
  const inline = extractLocalFiles('写法是 `[[file: 路径]]` 这样');
  assert.deepEqual(inline.paths, []);
});

test('没有标记时原样返回', () => {
  const s = '这一轮只做一件事';
  assert.deepEqual(extractLocalFiles(s), { text: s, paths: [] });
});

test('文件名与类型', () => {
  assert.equal(fileNameOf('D:\\a\\b.md'), 'b.md');
  assert.equal(kindOf('D:\\a\\x.py'), 'code');
  assert.equal(kindOf('D:\\a\\x.png'), 'image');
  assert.equal(kindOf('D:\\a\\x.txt'), 'text');
  assert.equal(kindOf('D:\\a\\x.zip'), 'file');
});
