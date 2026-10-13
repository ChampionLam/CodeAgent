/**
 * 日志入口的接线契约（2026-10-14）。
 *
 * electron/main.ts 不能 import（它 import electron），所以主进程那半边用
 * 「读源码核对通道名 + 纯逻辑单测」两段拼起来验：这里盯通道名、preload 的暴露、
 * 界面上的按钮文案，以及「没引入新的运行时依赖」。
 *
 * 修复前：这三处都没有日志相关的东西 —— 界面里没有入口，preload 没有声明。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { STDERR_PREFIX as MAIN_PREFIX } from '../electron/logAccess.ts';
import { STDERR_PREFIX as UI_PREFIX } from '../ui/src/pages/logPanel.ts';

const REPO = path.resolve(__dirname, '..');
const read = (rel: string): string => fs.readFileSync(path.join(REPO, rel), 'utf-8');
const MAIN = read('electron/main.ts');
const PRELOAD = read('electron/preload.ts');
const SETTINGS = read('ui/src/pages/SettingsPage.tsx');
const PANEL = read('ui/src/pages/LogsPanel.tsx');
const PKG = JSON.parse(read('package.json')) as Record<string, unknown>;

test('两侧的 [sidecar stderr] 前缀同口径（主进程挑行、界面剥前缀靠它对齐）', () => {
  assert.equal(MAIN_PREFIX, UI_PREFIX);
  assert.equal(MAIN_PREFIX, '[sidecar stderr]');
});

test('主进程注册两个日志通道（沿用 agent:xxx 风格），打开走 shell.openPath', () => {
  assert.match(MAIN, /ipcMain\.handle\("agent:logDescribe"/);
  assert.match(MAIN, /ipcMain\.handle\("agent:logOpen"/);
  // 打开日志目录必须用系统文件管理器（shell.openPath），不是自绘窗口/外链
  assert.match(MAIN, /openPath:\s*\(target: string\)\s*=>\s*shell\.openPath\(target\)/);
  // 目录以实际生效的为准：主进程那份取自 LogSink，不另拼一个
  assert.match(MAIN, /mainLogDir:\s*logSink\(\)\.logDir/);
  // 侧车日志双写：stderr 行要进主进程日志，界面才取得到
  assert.match(MAIN, /sinkLogger\(/);
});

test('preload 声明了 logs.describe / logs.open（渲染层只走白名单）', () => {
  assert.match(PRELOAD, /logs:\s*\{/);
  assert.match(PRELOAD, /describe\(\): Promise<LogDescribeLike>/);
  assert.match(PRELOAD, /async open\(kind: "main" \| "sidecar"\)/);
  // 通道名不能只活在 preload 里：主进程那边必须有同名的 handle
  for (const channel of ['agent:logDescribe', 'agent:logOpen']) {
    assert.ok(MAIN.includes(`"${channel}"`), `main.ts 缺少 ${channel}`);
    assert.ok(PRELOAD.includes(`"${channel}"`), `preload.ts 缺少 ${channel}`);
  }
  assert.match(PRELOAD, /export interface LogDescribeLike/);
});

test('设置页挂上了日志区，两个入口的按钮文案在界面上', () => {
  assert.match(SETTINGS, /<LogsPanel \/>/);
  assert.match(PANEL, /打开日志目录/);
  assert.match(PANEL, /复制目录路径/);
  // 复制走剪贴板，复制的是绝对路径本身
  assert.match(PANEL, /navigator\.clipboard\?\.writeText\(text\)/);
  // 显示给用户看的路径直接来自主进程给的 dirs，不许在界面里拼
  assert.match(PANEL, /\{d\.path\}/);
  // 失败原因（lastError / 重启次数 / 侧车输出）在同一个区域露面
  assert.match(PANEL, /sidecarFailure\(/);
  assert.match(PANEL, /stderrExcerpt\(/);
});

test('模型接口报错也会落到日志区（不是只在表单里闪一下）', () => {
  for (const label of ['获取模型列表', '校验模型', '保存模型', '读取模型列表']) {
    assert.ok(SETTINGS.includes(`reportInterfaceError('${label}'`), `SettingsPage 未上报：${label}`);
  }
});

test('没有引入新的运行时依赖（只有 devDependencies）', () => {
  assert.equal(PKG.dependencies, undefined, '根 package.json 不该出现 dependencies');
  assert.ok(PKG.devDependencies, 'devDependencies 应保持原样');
});