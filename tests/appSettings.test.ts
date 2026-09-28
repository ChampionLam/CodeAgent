import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_APP_SETTINGS, ACCENTS, ACCENT_IDS, THEME_PRESETS,  WALLPAPER_IDS, MSG_SURFACES,
  parseAppSettings, resolveTheme, cssVarsFor, applyAppSettings
} from '../ui/src/lib/appSettings.ts';

test('缺省值：theme=dark、accent=indigo、Enter 发送', () => {
  assert.equal(DEFAULT_APP_SETTINGS.theme, 'dark');
  assert.equal(DEFAULT_APP_SETTINGS.accent, 'indigo');
  assert.equal(DEFAULT_APP_SETTINGS.wallpaper, 'halo');
  // msgSurface 是 2026-09-27 加进外观页的（「放在外观里面设置吧」）
  assert.equal(DEFAULT_APP_SETTINGS.msgSurface, 'wallpaper');   // 缺省＝保持现状观感
  assert.deepEqual(Object.keys(DEFAULT_APP_SETTINGS).sort(), ['accent', 'msgSurface', 'theme', 'wallpaper']);
});

test('parse：非对象 / null / 字符串一律回落缺省', () => {
  for (const bad of [null, undefined, 42, 'x', [], true]) {
    assert.deepEqual(parseAppSettings(bad), DEFAULT_APP_SETTINGS);
  }
});

test('parse：坏字段逐项回落，不牵连其它字段', () => {
  const got = parseAppSettings({ theme: 'neon', accent: 'indigo' });
  assert.equal(got.theme, 'dark');            // 回落
  assert.equal(got.accent, 'indigo');         // 保留
});

test('parse：部分写入的旧对象不会被整份丢掉', () => {
  const got = parseAppSettings({ accent: 'teal' });
  assert.equal(got.accent, 'teal');
  assert.equal(got.theme, 'dark');
});

test('parse：已删掉的老字段（字体 / 字号）不再进对象，也不报错', () => {
  const got = parseAppSettings({ accent: 'rose', uiFont: 'evil', monoFont: 'evil', chatFontSize: 99 });
  assert.equal(got.accent, 'rose');
  assert.deepEqual(Object.keys(got).sort(), ['accent', 'msgSurface', 'theme', 'wallpaper']);
});

test('resolveTheme：system 跟系统，显式模式不受系统影响', () => {
  assert.equal(resolveTheme('system', true), 'dark');
  assert.equal(resolveTheme('system', false), 'light');
  assert.equal(resolveTheme('dark', false), 'dark');
  assert.equal(resolveTheme('light', true), 'light');
});

test('cssVarsFor：强调色五件套，且不再写字体与字号变量', () => {
  const v = cssVarsFor({ ...DEFAULT_APP_SETTINGS, accent: 'teal' });
  assert.equal(v['--accent'], ACCENTS.teal.base);
  assert.equal(v['--accent-hover'], ACCENTS.teal.hover);
  assert.equal(v['--accent-soft'], ACCENTS.teal.soft);
  assert.equal(v['--accent-ring'], ACCENTS.teal.ring);
  assert.equal(v['--accent-text'], ACCENTS.teal.text);
  // 字体/字号已经交给样式表默认值（global.css 里 --chat-font-size: 13.6px）
  assert.equal(v['--font-stack'], undefined);
  assert.equal(v['--font-mono'], undefined);
  assert.equal(v['--chat-font-size'], undefined);
  assert.equal(Object.keys(v).length, 5);
});

test('每个强调色都有完整五件套，id 数量与表一致', () => {
  for (const id of ACCENT_IDS) {
    const a = ACCENTS[id];
    assert.ok(a.name && a.base && a.hover && a.soft && a.ring && a.text, id);
    assert.match(a.base, /^#[0-9a-f]{6}$/);
    assert.ok(a.light && a.light.base && a.light.hover && a.light.soft && a.light.ring, id);
  }
  assert.equal(ACCENT_IDS.length, 6);
});

test('cssVarsFor：浅色主题下强调色换成深色一套（白字才够对比）', () => {
  const dark = cssVarsFor({ ...DEFAULT_APP_SETTINGS, accent: 'teal' }, 'dark');
  const light = cssVarsFor({ ...DEFAULT_APP_SETTINGS, accent: 'teal' }, 'light');
  assert.equal(dark['--accent'], ACCENTS.teal.base);
  assert.equal(light['--accent'], ACCENTS.teal.light.base);
  assert.notEqual(light['--accent'], dark['--accent']);
  assert.equal(light['--accent-text'], ACCENTS.teal.light.base);
  assert.equal(dark['--accent-text'], ACCENTS.teal.text);
});

test('每个强调色的浅色版都比深色版深（白字对比度更高）', () => {
  const relLum = (hex: string) => {
    const h = hex.replace('#', '');
    const [r, g, b] = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16) / 255)
      .map(v => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  for (const id of ACCENT_IDS) {
    assert.ok(relLum(ACCENTS[id].light.base) < relLum(ACCENTS[id].base), id + ' 的浅色版应更深');
  }
});

test('cssVarsFor：默认主题是深色（不传第二个参数时行为不变）', () => {
  assert.equal(cssVarsFor(DEFAULT_APP_SETTINGS)['--accent'], ACCENTS.indigo.base);
});

test('主题卡：一张卡 = 深浅 + 背景，深浅两档都要有纯光晕版', () => {
  assert.ok(THEME_PRESETS.length >= 5, '卡数 = ' + THEME_PRESETS.length);
  const combos = new Set<string>();
  for (const p of THEME_PRESETS) {
    assert.ok(p.name.length > 0 && p.hint.length > 0, p.id);
    assert.ok(['dark', 'light'].includes(p.mode), p.id + ' 的 mode 必须是已解析的深浅');
    assert.ok(WALLPAPER_IDS.includes(p.wallpaper), p.id + ' 的 wallpaper 非法');
    assert.ok(ACCENT_IDS.includes(p.accent), p.id + ' 的 accent 非法');
    const key = p.mode + '/' + p.wallpaper;
    assert.ok(!combos.has(key), '同一 (深浅, 背景) 组合出现两次: ' + key);
    combos.add(key);
  }
  // 两张纯光晕卡（深浅各一）+ 至少一张真壁纸卡
  assert.ok(THEME_PRESETS.some(p => p.mode === 'dark' && p.wallpaper === 'halo'), '缺深色光晕卡');
  assert.ok(THEME_PRESETS.some(p => p.mode === 'light' && p.wallpaper === 'halo'), '缺浅色光晕卡');
  assert.ok(THEME_PRESETS.filter(p => p.wallpaper !== 'halo').length >= 3, '真壁纸卡不少于 3 张');
});

test('主题卡：每张卡的预览都能画出强调色变量（深浅按卡自带）', () => {
  for (const p of THEME_PRESETS) {
    {
      const theme = p.mode;
      const v = cssVarsFor(DEFAULT_APP_SETTINGS, theme);
      assert.ok(v['--accent'] && v['--accent-hover'] && v['--accent-soft'] && v['--accent-ring'], p.id);
      assert.match(v['--accent'], /^#[0-9a-f]{6}$/, p.id + ' 的预览底色应是六位十六进制');
    }
  }
});

// ---- 消息底纹（外观页，2026-09-27 用户口径「放在外观里面设置吧」）----------

test('消息底纹：只有两个选项，缺省是「透出壁纸」', () => {
  assert.deepEqual(MSG_SURFACES.map(m => m.id), ['wallpaper', 'solid']);
  assert.equal(DEFAULT_APP_SETTINGS.msgSurface, 'wallpaper');
});

test('消息底纹：认得的值保留，奇怪的值回落缺省', () => {
  assert.equal(parseAppSettings({ msgSurface: 'solid' }).msgSurface, 'solid');
  for (const bad of ['translucent', '', 1, null, {}, []]) {
    assert.equal(parseAppSettings({ msgSurface: bad }).msgSurface, 'wallpaper');
  }
});

test('消息底纹：旧存档（没有这个键）不会被判坏', () => {
  const old = { theme: 'light', accent: 'teal', wallpaper: 'snow' };
  const got = parseAppSettings(old);
  assert.equal(got.wallpaper, 'snow');
  assert.equal(got.msgSurface, 'wallpaper');
});

test('parse：theme=light 时其它字段照样保留（回归）', () => {
  const got = parseAppSettings({ theme: 'light', accent: 'blue', wallpaper: 'aurora', msgSurface: 'solid' });
  assert.deepEqual(got, { theme: 'light', accent: 'blue', wallpaper: 'aurora', msgSurface: 'solid' });
});


test('applyAppSettings：把消息底纹写到 <html> 的 data-msg-surface 上（CSS 靠它覆盖）', () => {
  // 假 root（node 里没有 DOM，照 cssVarsFor 测试的老办法）
  const root: any = { dataset: {} as Record<string, string>, style: { setProperty() {} }, };
  applyAppSettings({ theme: 'dark', accent: 'indigo', wallpaper: 'snow', msgSurface: 'solid' }, root, false);
  assert.equal(root.dataset.msgSurface, 'solid');
  assert.equal(root.dataset.theme, 'dark');

  applyAppSettings({ ...DEFAULT_APP_SETTINGS }, root, false);
  assert.equal(root.dataset.msgSurface, 'wallpaper');
});
