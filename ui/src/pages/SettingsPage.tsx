import { useEffect, useState } from 'react';
import { useApp } from '../state/store';
import { fetchRemoteModelIds, fetchRemoteModelsDetailed, fetchVisionModel,
  saveVisionModel } from '../state/live';
import type { RemoteModelMeta } from '../state/live';
import { CTX_LADDER, ctxHintText, formatCtx, followFetchedMeta, followPickedModel } from './modelDraft';
import { VENDOR_LABELS } from '../state/vendors';
import type { ThinkingDepth, ThinkingDisplay } from '../types';
import { Icon } from '../components/Icon';
import './SettingsPage.css';

type Tab = 'models';

/** 一次最多画这么多行，剩下的打字缩小范围（几百上千个模型全画会卡）。 */
const MODEL_LIST_MAX = 60;

/**
 * 模型名的模糊匹配：**子串命中排前面**（越靠前越优先），其次「按字符顺序都能命中」垫底。
 * 都忽略大小写——用户记不清中间那段，只记得开头结尾时也能找到。
 */
function fuzzyScore(hay: string, needle: string): number | null {
  if (!needle) return 0;
  const h = hay.toLowerCase();
  const n = needle.toLowerCase();
  const at = h.indexOf(n);
  if (at >= 0) return at;
  let i = 0;
  for (const ch of n) {
    i = h.indexOf(ch, i);
    if (i < 0) return null;
    i += 1;
  }
  return 10000 + hay.length;
}

function matchModels(ids: string[], q: string): string[] {
  const needle = q.trim();
  if (!needle) return ids;
  return ids
    .map((id, i) => ({ id, score: fuzzyScore(id, needle), i }))
    .filter(r => r.score !== null)
    .sort((a, b) => (a.score as number) - (b.score as number) || a.i - b.i)
    .map(r => r.id);
}

/* ── Models tab ─────────────────────────────────────────────────── */
interface ModelDraft {
  label: string;
  model: string;
  baseUrl: string;
  apiKeyEnv: string;
  provider: string;
  /** Only ever holds what the user just typed. Never prefilled from the backend. */
  secret: string;
  maxTokens: string;
  timeoutSeconds: string;
  vision: boolean;
  /* ── v2 generation params (contract §1.1) ─────────────────────────── */
  /** Token count; validated against 1000..10000000 (MODEL_INVALID on the backend). */
  contextWindow: string;
  /** 'default' | 'on' | 'off' — null / true / false in the payload. */
  thinking: 'default' | 'on' | 'off';
  /** 'default' | 'low' | 'medium' | 'high' — null / level in the payload.
   *  厂商没有深度参数时这里是软引导（只改提示词），界面会写明。 */
  thinkingDepth: 'default' | ThinkingDepth;
  /** Empty string = null (key must not appear in the request body). */
  temperature: string;
  /** Empty string = null. */
  topP: string;
}

const CONTEXT_WINDOW_MIN = 1000;
const CONTEXT_WINDOW_MAX = 10_000_000;

/** 128000 → "128,000" — card meta display only. */
function fmtNum(n: number): string {
  return n.toLocaleString('en-US');
}

function emptyDraft(): ModelDraft {
  return {
    label: '', model: '', baseUrl: '', apiKeyEnv: '', provider: '',
    secret: '', maxTokens: '4096', timeoutSeconds: '180', vision: false,
    contextWindow: '128000', thinking: 'default', thinkingDepth: 'default',
    temperature: '', topP: ''
  };
}

interface ModelFormProps {
  draft: ModelDraft;
  setDraft: (d: ModelDraft) => void;
  presets: ReturnType<typeof useApp>['modelPresets'];
  showsKeyState: boolean;
  keyConfigured: boolean;
  busy: boolean;
  checking: boolean;
  canSkip: boolean;
  // 「用户这一轮自己动过窗口没有」由 ModelsTab 持有：保存（校验并添加）那一步在
  // ModelsTab 里，它要拿这个判断该不该按厂商值纠正窗口，所以标志位提上去。
  ctxTouched: boolean;
  setCtxTouched: (v: boolean) => void;
  onCancel: () => void;
  onSubmit: (skipCheck?: boolean) => void;
}

function ModelForm({ draft, setDraft, presets, showsKeyState, keyConfigured, busy, checking, canSkip, ctxTouched, setCtxTouched, onCancel, onSubmit }: ModelFormProps) {
  // 用户不该手打这些：厂商选好 → baseUrl / 环境变量名 / 上下文窗口 自动带出；
  // 模型名从厂商的 /models 拉回来让用户挑。超时、温度这些收进「高级设置」折叠，
  // 平时不用看。
  const [ids, setIds] = useState<string[]>([]);
  const [fetching, setFetching] = useState(false);
  const [fetchErr, setFetchErr] = useState('');
  const [advOpen, setAdvOpen] = useState(false);
  // 厂商一次拉取里带的「每个模型的真实窗口」，用来自动填「上下文窗口」那格
  const [modelMeta, setModelMeta] = useState<Record<string, RemoteModelMeta>>({});

  // 深度只有两种落法：厂商级参数（硬）或提示词一行（软）。界面必须说实话，
  // 不能让用户以为所有厂商都能硬调深度。
  const depthHint = (() => {
    if (draft.thinking === 'off') {
      return '思考已关闭，深度不生效';
    }
    const p = presets.find(x => x.key === draft.provider);
    if (!p || p.thinkingSupported === false) {
      return '该厂商未适配：不会传任何深度参数。';
    }
    if ((p.thinkingDepthLevels ?? []).length > 0) {
      return `厂商级深度参数（${(p.thinkingDepthLevels ?? []).join(' / ')}）：直接写进请求体。`;
    }
    return '厂商无深度参数：只在提示词里加一行（不保证效果）';
  })();

  const applyPreset = (key: string) => {
    const p = presets.find(x => x.key === key);
    if (!p) { setDraft({ ...draft, provider: key }); return; }
    setDraft({
      ...draft,
      provider: key,
      baseUrl: p.baseUrl || draft.baseUrl,
      apiKeyEnv: p.keyEnvHint || draft.apiKeyEnv,
      // 模板里带着该厂商的典型窗口，免得用户去查文档
      contextWindow: p.contextWindow ? String(p.contextWindow) : draft.contextWindow
    });
    setIds([]);
    setFetchErr('');
    // 换了厂商，上一家的窗口事实和「用户改过没有」都重置
    setModelMeta({});
    setCtxTouched(false);
  };
  const active = presets.find(p => p.key === draft.provider);
  // 「这格的值是哪来的」写清楚：厂商给的走自动带出，拉不到就照阶梯挑。
  const vendorCtx = draft.model ? modelMeta[draft.model]?.contextLength : undefined;
  const ctxHint = ctxHintText(draft.contextWindow, vendorCtx);

  const doFetch = async () => {
    setFetching(true);
    setFetchErr('');
    try {
      const { ids: list, meta } = await fetchRemoteModelsDetailed({
        provider: draft.provider,
        baseUrl: draft.baseUrl,
        apiKeyEnv: draft.apiKeyEnv,
        // 用户刚粘的 key 也行，不必先保存；后端只用它发请求，不落库不回显
        apiKey: draft.secret || undefined
      });
      setIds(list);
      setModelMeta(meta);
      if (list.length && !draft.model) {
        setDraft({ ...draft, ...followPickedModel(draft, list[0], meta, ctxTouched) });
      } else {
        // 编辑已有模型时表单里已经填着模型名，不会走「挑一个」那一步；
        // 这里按厂商值把窗口填对（用户这一轮自己改过就不动）。
        setDraft({ ...draft, ...followFetchedMeta(draft, meta, ctxTouched) });
      }
    } catch (e) {
      setIds([]);
      setFetchErr(e instanceof Error ? e.message : '获取模型列表失败');
    } finally {
      setFetching(false);
    }
  };

  const pickModel = (id: string) => {
    // 显示名和上下文窗口都跟着挑中的模型走：新加的 1M 模型被存成 128k，
    // 就是这两样没跟着换（用户 2026-09-27）。纯逻辑在 modelDraft.ts，有单测。
    setDraft({ ...draft, ...followPickedModel(draft, id, modelMeta, ctxTouched) });
  };

  // 可搜索下拉：拿到列表后也**不锁死选择**，输入框一直能自己改（用户 2026-09-27 口径）
  const [listOpen, setListOpen] = useState(false);
  const [hi, setHi] = useState(0);
  const [flipUp, setFlipUp] = useState(false);
  const [listMaxH, setListMaxH] = useState(240);
  const matched = matchModels(ids, draft.model);
  const shown = matched.slice(0, MODEL_LIST_MAX);

  /** 展开前量一下空间：下面不够就往上翻，高度也按可用空间收。
   *  弹窗正文是 overflow-y: auto，画到外面会被裁掉、点不到（踩过一次）。 */
  const openList = (el: HTMLElement) => {
    if (!ids.length) { setListOpen(false); return; }
    const r = el.getBoundingClientRect();
    const below = window.innerHeight - r.bottom - 16;
    const above = r.top - 16;
    const up = below < 200 && above > below;
    setFlipUp(up);
    setListMaxH(Math.max(120, Math.min(260, up ? above : below)));
    setHi(0);
    setListOpen(true);
  };

  const onModelKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Escape') { setListOpen(false); return; }
    if (!listOpen || !shown.length) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); setHi(h => Math.min(h + 1, shown.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setHi(h => Math.max(h - 1, 0)); }
    else if (e.key === 'Enter') { e.preventDefault(); pickModel(shown[hi]); setListOpen(false); }
  };

  return (
    <>
      <div className="mform">
        <label className="field mform__wide">
          <span className="field__label">对接方式</span>
          <select className="input" value={draft.provider} onChange={e => applyPreset(e.target.value)}>
            <option value="">选择对接方式…</option>
            {presets.map(p => (
              <option key={p.key} value={p.key}>
                {/* 厂商名用中文口径（state/vendors.ts 是单一来源，模型列表也用它）；
                    表里认不出来的 key 才回落到后端给的英文 label。 */}
                {VENDOR_LABELS[p.key] ?? p.label}
                {p.needsKey === false ? '（无需 key）' : ''}
              </option>
            ))}
          </select>
          <span className="field__hint">
            {active?.notes ?? '选择对接方式后，请求地址与 key 变量名将自动填充。'}
          </span>
        </label>

        <label className="field mform__wide">
          <span className="field__label">API Key</span>
          <input
            className="input"
            type="password"
            autoComplete="off"
            value={draft.secret}
            placeholder={showsKeyState && keyConfigured ? '已配置，留空则不修改' : '粘贴后仅写入 .env'}
            onChange={e => setDraft({ ...draft, secret: e.target.value })}
          />
          <span className="field__hint">
            仅写入 .env，界面不回读、不显示、不写入日志。未保存也可点击下方「获取模型列表」。
          </span>
        </label>

        <div className="field mform__wide">
          <span className="field__label">模型</span>
          <div className="mform__row">
            {/* 一直是可输入的输入框：列表只当建议，模型名随时能自己填、自己改。
                原来拿到列表后会换成原生 select——几百个模型翻不动，还不让手填。 */}
            <div className="mform__combo">
              <input
                className="input mono"
                role="combobox"
                aria-expanded={listOpen && ids.length > 0}
                aria-autocomplete="list"
                value={draft.model}
                placeholder={ids.length ? '搜索模型名，或直接填写' : '可先在右侧获取，或直接填写模型名'}
                onFocus={e => openList(e.currentTarget)}
                onBlur={() => window.setTimeout(() => setListOpen(false), 120)}
                onKeyDown={onModelKey}
                onChange={e => {
                  setDraft({ ...draft, model: e.target.value, label: draft.label || e.target.value });
                  setHi(0);
                  // 选过一次/按过回车之后接着改，也要能继续给建议
                  setListOpen(true);
                }}
              />
              {listOpen && ids.length > 0 && (
                <div
                  className={`mform__combo-list${flipUp ? ' is-up' : ''}`}
                  style={{ maxHeight: listMaxH }}
                  role="listbox"
                >
                  {shown.length === 0 && (
                    <div className="mform__combo-empty">列表里没有匹配的——直接用现在填的名字就行</div>
                  )}
                  {shown.map((id, i) => (
                    <button
                      key={id}
                      type="button"
                      role="option"
                      aria-selected={id === draft.model}
                      className={`mform__combo-item${i === hi ? ' is-hi' : ''}${id === draft.model ? ' is-sel' : ''}`}
                      onMouseDown={e => { e.preventDefault(); pickModel(id); setListOpen(false); }}
                      onMouseEnter={() => setHi(i)}
                    >
                      <span className="mform__combo-name">{id}</span>
                      {/* 厂商给了窗口就顺手写在右边，挑的时候能看见 */}
                      {modelMeta[id]?.contextLength ? (
                        <span className="mform__combo-ctx">{formatCtx(modelMeta[id].contextLength)}</span>
                      ) : null}
                    </button>
                  ))}
                  {matched.length > shown.length && (
                    <div className="mform__combo-more">还有 {matched.length - shown.length} 个，继续打字缩小范围</div>
                  )}
                </div>
              )}
            </div>
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              onClick={doFetch}
              disabled={fetching || busy}
            >
              {fetching ? '获取中…' : ids.length ? '重新获取' : '获取模型列表'}
            </button>
          </div>
          <span className="field__hint">
            {fetchErr
              ? fetchErr
              : ids.length
                ? `已获取 ${ids.length} 个模型，可搜索选择，也可以直接填名字；选中后窗口会自动带出来。`
                : '从厂商接口获取可用模型；也可以跳过获取，直接填模型名。'}
          </span>
        </div>

        <div className="mform__wide">
          <button
            type="button"
            className="mform__adv-toggle"
            onClick={() => setAdvOpen(v => !v)}
            aria-expanded={advOpen}
          >
            <Icon.ChevronDown size={13} />
            <span>高级设置</span>
            <span className="mform__adv-note">
              {advOpen ? '收起' : `一般无需填写：请求地址${draft.baseUrl ? '（已自动填充）' : ''}、窗口、超时、温度等`}
            </span>
          </button>
        </div>

        {advOpen && (
          <>
            <label className="field mform__wide">
              <span className="field__label">请求地址 Base URL</span>
              <input
                className="input mono"
                value={draft.baseUrl}
                placeholder="https://api.example.com/v1"
                onChange={e => setDraft({ ...draft, baseUrl: e.target.value })}
              />
              <span className="field__hint">
                按对接方式自动填充；仅自建/中转地址需要修改。请勿包含 /chat/completions。
              </span>
            </label>

            <label className="field">
              <span className="field__label">Key 环境变量名</span>
              <input
                className="input mono"
                value={draft.apiKeyEnv}
                placeholder="例：DEEPSEEK_API_KEY"
                onChange={e => setDraft({ ...draft, apiKeyEnv: e.target.value })}
              />
              <span className="field__hint">.env 里的变量名，一般无需改</span>
            </label>

            <label className="field">
              <span className="field__label">最大输出 token</span>
              <input
                className="input"
                value={draft.maxTokens}
                onChange={e => setDraft({ ...draft, maxTokens: e.target.value })}
              />
              <span className="field__hint">思考与正文共用此预算</span>
            </label>

            <div className="field">
              <span className="field__label">上下文窗口（token）</span>
              <input
                className="input mono"
                inputMode="numeric"
                value={draft.contextWindow}
                placeholder="128000"
                onChange={e => {
                  setCtxTouched(true);
                  setDraft({ ...draft, contextWindow: e.target.value });
                }}
              />
              {/* 厂商给了这个模型的窗口就自动带出来；拉不到才让用户按阶梯挑
                  （用户 2026-09-27 口径：64 / 128 / 256 / 512 / 1M）。
                  无论哪种情况，这格都能自己填。 */}
              <div className="mform__ctx-chips">
                {CTX_LADDER.map(v => (
                  <button
                    key={v}
                    type="button"
                    className={`mform__ctx-chip${Number(draft.contextWindow) === v ? ' is-on' : ''}`}
                    onClick={() => {
                      setCtxTouched(true);
                      setDraft({ ...draft, contextWindow: String(v) });
                    }}
                  >
                    {formatCtx(v)}
                  </button>
                ))}
              </div>
              <span className="field__hint">{ctxHint}</span>
            </div>

            <label className="field">
              <span className="field__label">请求超时（秒）</span>
              <input
                className="input"
                value={draft.timeoutSeconds}
                onChange={e => setDraft({ ...draft, timeoutSeconds: e.target.value })}
              />
              <span className="field__hint">默认 180。</span>
            </label>

            <label className="field">
              <span className="field__label">温度 temperature</span>
              <input
                className="input mono"
                inputMode="decimal"
                value={draft.temperature}
                placeholder="留空 = 不传"
                onChange={e => setDraft({ ...draft, temperature: e.target.value })}
              />
              <span className="field__hint">采样温度，0–2。留空则请求体不带该参数。</span>
            </label>

            <label className="field">
              <span className="field__label">核采样 top_p</span>
              <input
                className="input mono"
                inputMode="decimal"
                value={draft.topP}
                placeholder="留空 = 不传"
                onChange={e => setDraft({ ...draft, topP: e.target.value })}
              />
              <span className="field__hint">核采样阈值，0–1。留空则请求体不带该参数。</span>
            </label>

            <label className="field">
              <span className="field__label">思考模式</span>
              <select
                className="input"
                value={draft.thinking}
                onChange={e => setDraft({
                  ...draft,
                  thinking: e.target.value === 'on' || e.target.value === 'off'
                    ? e.target.value
                    : 'default'
                })}
              >
                <option value="default">跟随服务端默认（不传参）</option>
                <option value="on">强制开启</option>
                <option value="off">强制关闭</option>
              </select>
              <span className="field__hint">
                {active?.thinkingSupported === false ? '该厂商未适配思考开关' : '请求体不带思考参数'}
              </span>
            </label>

            <label className="field">
              <span className="field__label">思考深度</span>
              <select
                className="input"
                value={draft.thinkingDepth}
                disabled={draft.thinking === 'off'}
                onChange={e => setDraft({
                  ...draft,
                  thinkingDepth: (e.target.value === 'low' || e.target.value === 'medium'
                    || e.target.value === 'high') ? e.target.value : 'default'
                })}
              >
                <option value="default">不干预（不传深度参数）</option>
                <option value="low">轻（low）</option>
                <option value="medium">中（medium）</option>
                <option value="high">深（high）</option>
              </select>
              <span className="field__hint">{depthHint}</span>
            </label>

            <label className="field">
              <span className="field__label">图片输入</span>
              <select
                className="input"
                value={draft.vision ? 'yes' : 'no'}
                onChange={e => setDraft({ ...draft, vision: e.target.value === 'yes' })}
              >
                <option value="no">不支持</option>
                <option value="yes">支持（多模态）</option>
              </select>
              <span className="field__hint">只有声明支持图片，附件才会作为图片发送给模型。</span>
            </label>
          </>
        )}
      </div>

      <div className="mcard__edit-foot">
        {canSkip && (
          <button className="btn btn--ghost btn--sm" onClick={() => onSubmit(true)} disabled={busy || checking}>
            跳过校验，直接添加
          </button>
        )}
        <button className="btn btn--ghost btn--sm" onClick={onCancel} disabled={busy || checking}>取消</button>
        <button className="btn btn--primary btn--sm" onClick={() => onSubmit()} disabled={busy || checking}>
          {checking ? '校验中…' : busy ? '保存中…' : '校验并添加模型'}
        </button>
      </div>
    </>
  );
}

function ModelsTab() {
  const {
    models, activeModelId, setActiveModel, modelPresets, modelsReady, modelsError,
    refreshModels, saveModel, removeModel, setDefaultModel,
    thinkingDisplay, setThinkingDisplay
  } = useApp();
  // 视觉线路：会话模型不吃图时，图片先交给它读成文字（2026-09-28 用户要求在设置里能改）
  const [vision, setVision] = useState<{ model?: string; provider?: string }>({});
  const [visionBusy, setVisionBusy] = useState(false);
  const [visionErr, setVisionErr] = useState('');
  const [visionHint, setVisionHint] = useState('');
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<ModelDraft>(emptyDraft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // 校验态：主按钮点下去先真问一次厂商 /models，通过才保存（照 Qoder 的「校验并添加」）
  const [checking, setChecking] = useState(false);
  const [canSkip, setCanSkip] = useState(false);
  // 用户这一轮自己动过「上下文窗口」没有（在 ModelForm 里 onChange，状态放在这里，
  // 因为保存那一步在 ModelsTab）。
  const [ctxTouched, setCtxTouched] = useState(false);

  /* 2026-09-26：「添加模型」改成居中弹窗（用户要求：跟模型面板一样是弹窗，不是页内嵌卡片）。
     Esc 关闭；输入框聚焦时也要生效，所以挂 window。 */
  useEffect(() => {
    let alive = true;
    void fetchVisionModel()
      .then(v => { if (alive) setVision(v); })
      .catch(() => { /* 老 sidecar 没有这条线路就不显示 */ });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (editing !== 'new') return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      // 跟全局口径一致：输入框里按第一次 Esc 只退出输入，别把填了一半的表单关掉。
      const el = document.activeElement as HTMLElement | null;
      const typing = !!el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
      if (typing) { el.blur(); return; }   // 第一次只退出输入，再按一次才关表单
      setEditing(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [editing]);

  const startAdd = () => {
    setEditing('new');
    setDraft(emptyDraft());
    setCtxTouched(false);
    setError(null);
    setNotice(null);
  };

  const startEdit = (m: ReturnType<typeof useApp>['models'][number]) => {
    setEditing(m.id);
    // The key is deliberately not prefilled: the renderer does not have it.
    setDraft({
      label: m.name,
      model: m.model,
      baseUrl: m.baseUrl,
      apiKeyEnv: m.apiKeyEnv ?? '',
      provider: m.provider ?? '',
      secret: '',
      maxTokens: String(m.maxTokens ?? 4096),
      timeoutSeconds: String(m.timeoutSeconds ?? 180),
      vision: (m.inputModalities ?? []).includes('image'),
      contextWindow: String(m.contextWindow ?? 128000),
      thinking: m.thinking === true ? 'on' : m.thinking === false ? 'off' : 'default',
      thinkingDepth: (m.thinkingDepth === 'low' || m.thinkingDepth === 'medium'
        || m.thinkingDepth === 'high') ? m.thinkingDepth : 'default',
      temperature: m.temperature == null ? '' : String(m.temperature),
      topP: m.topP == null ? '' : String(m.topP)
    });
    setCtxTouched(false);
    setError(null);
    setNotice(null);
  };

  const submit = async (skipCheck = false) => {
    setError(null);
    setNotice(null);
    // 校验时厂商给的窗口表（走「跳过校验」那条路时为空）
    let checkMeta: Record<string, RemoteModelMeta> = {};
    if (!draft.model.trim() || !draft.baseUrl.trim()) {
      setError('模型名与 Base URL 不能为空。');
      return;
    }
    if (!skipCheck) {
      // 照 Qoder：先校验这把 key 真能问出模型，通过了才落库。这里必须用 detailed 那支——
      // 厂商在同一个响应里就把每个模型的窗口给了，只留 id 的话窗口就没得校验
      // （用户 2026-09-27：「校验并添加模型那里你也还得做一次校验」）。
      // 校验不过不保存，但给一条「跳过校验」的后路——有些自建网关没有 /models 也能用。
      setChecking(true);
      try {
        const { ids: list, meta: fetchedMeta } = await fetchRemoteModelsDetailed({
          provider: draft.provider,
          baseUrl: draft.baseUrl,
          apiKeyEnv: draft.apiKeyEnv,
          apiKey: draft.secret || undefined
        });
        if (!list.length) throw new Error('厂商未返回任何模型');

        checkMeta = fetchedMeta;
        // 顺手把窗口按厂商值填对（用户这一轮自己手改过就不动），界面上直接能看见。
        setDraft(d => ({ ...d, ...followFetchedMeta(d, fetchedMeta, ctxTouched) }));
        setCanSkip(false);
      } catch (e) {
        setError(`校验未通过：${e instanceof Error ? e.message : '无法获取模型列表'}`);
        setCanSkip(true);
        setChecking(false);
        return;
      }
      setChecking(false);
    }
    if (draft.apiKeyEnv.trim() && !/^[A-Z][A-Z0-9_]*$/.test(draft.apiKeyEnv.trim())) {
      setError('Key 环境变量名只能是大写字母、数字、下划线，并且以字母开头。');
      return;
    }
    // ── v2 generation params (§1.1). Mirror the backend's MODEL_INVALID rules
    //    so the message can name the field before the RPC round-trip.
    // 窗口以「校验之后」的值为准：厂商认识这个模型、用户这一轮又没自己改过，就按厂商的存
    // （他手里那个值很可能就是厂商模板的默认值）。
    const saveFollow = followFetchedMeta(draft, checkMeta, ctxTouched);
    const cw = saveFollow.contextWindow ?? draft.contextWindow;
    const cwNum = Number(cw.trim());
    if (!cw.trim() || !Number.isInteger(cwNum) || cwNum < CONTEXT_WINDOW_MIN || cwNum > CONTEXT_WINDOW_MAX) {
      setError(
        `上下文窗口必须是 ${CONTEXT_WINDOW_MIN}–${CONTEXT_WINDOW_MAX} 的整数（MODEL_INVALID：contextWindow）。`
      );
      return;
    }
    const temp = draft.temperature.trim();
    const tempNum = Number(temp);
    if (temp && (!Number.isFinite(tempNum) || tempNum < 0 || tempNum > 2)) {
      setError('温度必须是 0–2 之间的数字（含边界），留空表示不传（MODEL_INVALID：temperature）。');
      return;
    }
    const topPStr = draft.topP.trim();
    const topPNum = Number(topPStr);
    if (topPStr && (!Number.isFinite(topPNum) || topPNum < 0 || topPNum > 1)) {
      setError('top_p 必须是 0–1 之间的数字（含边界），留空表示不传（MODEL_INVALID：topP）。');
      return;
    }
    setBusy(true);
    try {
      const payload: Record<string, unknown> = {
        label: draft.label.trim() || draft.model.trim(),
        provider: draft.provider || 'custom',
        baseUrl: draft.baseUrl.trim(),
        model: draft.model.trim(),
        apiKeyEnv: draft.apiKeyEnv.trim(),
        maxTokens: Number(draft.maxTokens) || undefined,
        timeoutSeconds: Number(draft.timeoutSeconds) || undefined,
        inputModalities: draft.vision ? ['text', 'image'] : ['text'],
        contextWindow: cwNum,
        // Tri-state: null = 不干预（请求体不出现思考键）。
        thinking: draft.thinking === 'on' ? true : draft.thinking === 'off' ? false : null,
        // 深度三态：null = 不传。关掉思考时后端也会丢弃深度键。
        thinkingDepth: draft.thinkingDepth === 'default' ? null : draft.thinkingDepth,
        // 留空 → null（显式回默认/清空）；数值 → 范围已校验。
        temperature: temp ? tempNum : null,
        topP: topPStr ? topPNum : null
      };
      if (editing && editing !== 'new') payload.id = editing;
      if (draft.secret.trim()) payload.apiKey = draft.secret.trim();
      const id = await saveModel(payload);
      // 窗口留一句口信：按厂商值改过要说；用户自己填的与厂商不一致也要说（这就是「校验」）。
      const vendorNow = checkMeta[draft.model.trim()]?.contextLength;
      let ctxNote = '';
      if (saveFollow.contextWindow) {
        ctxNote = `；窗口按厂商值填成 ${formatCtx(cwNum)}（原来填的是 ${draft.contextWindow.trim()}）`;
      } else if (vendorNow && String(vendorNow) !== draft.contextWindow.trim()) {
        ctxNote = `；注意：厂商窗口是 ${formatCtx(vendorNow)}（${vendorNow}），你填的是 ${draft.contextWindow.trim()}`;
      }
      setNotice((editing === 'new' ? `已添加模型：${id}` : `已保存模型：${id}`) + ctxNote);
      setEditing(null);
      setDraft(emptyDraft());
    } catch (e) {
      // Sidecar rejects with MODEL_INVALID + per-field message; surface it raw.
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onRemove = async (id: string) => {
    setError(null);
    setNotice(null);
    try {
      await removeModel(id);
      setNotice(`已删除模型：${id}`);
      if (editing === id) setEditing(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const onDefault = async (id: string) => {
    setError(null);
    setNotice(null);
    try {
      await setDefaultModel(id);
      setNotice(`全局默认已切换为：${id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const editingModel = editing && editing !== 'new' ? models.find(m => m.id === editing) : undefined;
  const defaultName = models.find(m => m.isDefault)?.name;

  return (
    <>
      <section className="card">
        <div className="card__head">
          <div>
            <div className="card__title">思考显示</div>
          </div>
        </div>
        <div className="card__body">
          <label className="field">
            <span className="field__label">对话中思考的显示方式</span>
            <select
              className="input"
              value={thinkingDisplay}
              onChange={e => setThinkingDisplay(e.target.value as ThinkingDisplay)}
            >
              <option value="collapsed">折叠（默认，点击查看）</option>
              <option value="expanded">展开（直接显示）</option>
              <option value="hidden">隐藏正文（仅显示「思考中」指示）</option>
            </select>
            <span className="field__hint">生成时先显示一行「思考中」</span>
          </label>
        </div>
      </section>

      <section className="card">
        <div className="card__head">
          <div>
            <div className="card__title">模型</div>
            <div className="card__sub">
              写入 config.json；Key 只进 .env
              {models.length > 0 ? ` · 共 ${models.length} 个${defaultName ? `，默认：${defaultName}` : ''}` : ''}
            </div>
          </div>
          <div className="card__foot card__foot--inline">
            <button className="btn btn--ghost btn--sm" onClick={() => { void refreshModels(); }}>
              <Icon.Refresh size={13} /> 刷新
            </button>
            <button className="btn btn--primary btn--sm" onClick={startAdd}>
              <Icon.Plus size={13} /> 添加模型
            </button>
          </div>
        </div>

        <div className="card__body">
          {error && <div className="card card--danger"><div className="card__body">{error}</div></div>}
          {notice && <div className="card__sub">{notice}</div>}
          {!modelsReady && !modelsError && <div className="card__sub">正在从 sidecar 读取模型配置…</div>}
          {modelsError && <div className="card__sub">读取失败：{modelsError}</div>}
          {modelsReady && models.length === 0 && (
            <div className="card__sub">尚未配置模型</div>
          )}

          {/* 读图走哪个模型：会话模型没声明吃图时，图片先交给它读成文字再进上下文 */}
          <div className="mcard" style={{ marginBottom: 12 }}>
            <div className="mcard__head">
              <div>
                <div className="card__title">视觉模型</div>
                <div className="card__sub">
                  会话用的模型不吃图时，图片先交给它读成文字再进上下文
                  {vision.model ? ` · 当前：${vision.model}` : ' · 未设置'}
                </div>
              </div>
            </div>
            <div className="mcard__body">
              <select
                className="input"
                disabled={visionBusy}
                value={models.find(m => (m as { model?: string }).model === vision.model)?.id ?? ''}
                onChange={e => {
                  const id = e.target.value;
                  setVisionErr(''); setVisionHint(''); setVisionBusy(true);
                  void saveVisionModel(id)
                    .then(() => fetchVisionModel())
                    .then(v => { setVision(v); setVisionHint('已保存，下一轮带图提问就走它读图'); })
                    .catch(err => setVisionErr(err instanceof Error ? err.message : '保存失败'))
                    .finally(() => setVisionBusy(false));
                }}
              >
                {models.length === 0 && <option value="">（还没有可选模型）</option>}
                {models.map(m => (
                  <option key={m.id} value={m.id}>
                    {(m as { name?: string }).name ?? m.id}
                  </option>
                ))}
              </select>
              <span className="field__hint">
                只有声明支持图片的模型能当视觉模型，没声明的会被拒绝。
              </span>
              {visionErr && <div className="card__sub">{visionErr}</div>}
              {visionHint && <div className="card__sub">{visionHint}</div>}
            </div>
          </div>

          {models.map(m => {
            const isEditing = editing === m.id;
            const isSession = m.id === activeModelId;
            return (
              <div key={m.id} className="mcard">
                <div className="mcard__head">
                  <button
                    className={`mcard__radio ${isSession ? 'is-on' : ''}`}
                    onClick={() => setActiveModel(m.id)}
                    title="当前会话使用该模型"
                    aria-pressed={isSession}
                  >
                    <span className="mcard__radio-dot" />
                  </button>

                  <div className="mcard__title">
                    <span className="mcard__name">{m.name}</span>
                    <span className="mcard__model mono">{m.model}</span>
                  </div>

                  {isSession && <span className="chip chip--success">本会话</span>}
                  {m.isDefault && <span className="chip chip--neutral">全局默认</span>}
                  {m.hasKey === false && <span className="chip chip--accent">未配 key</span>}

                  <div className="mcard__actions">
                    <button className="btn btn--ghost btn--sm" onClick={() => (isEditing ? setEditing(null) : startEdit(m))}>
                      {isEditing ? '收起' : '编辑'}
                    </button>
                    {!m.isDefault && (
                      <button className="btn btn--ghost btn--sm" onClick={() => { void onDefault(m.id); }} disabled={busy}>
                        设为默认
                      </button>
                    )}
                    <button
                      className="mcard__del"
                      onClick={() => { void onRemove(m.id); }}
                      disabled={models.length <= 1}
                      title={models.length <= 1 ? '至少保留一个模型' : '删除模型'}
                    >
                      <Icon.Trash size={13} />
                    </button>
                  </div>
                </div>

                <div className="mcard__meta">
                  <span className="mcard__meta-item">
                    <span className="mcard__meta-key">Base URL</span>
                    <span className="mono">{m.baseUrl}</span>
                  </span>
                  <span className="mcard__meta-item">
                    <span className="mcard__meta-key">Key 变量</span>
                    <span className="mono">{m.apiKeyEnv || '—'}</span>
                  </span>
                  <span className="mcard__meta-item">
                    <span className="mcard__meta-key">状态</span>
                    <span>{m.hasKey === false ? '未配置 key' : '可调用'}</span>
                  </span>
                  {m.maxTokens ? (
                    <span className="mcard__meta-item">
                      <span className="mcard__meta-key">最大输出</span>
                      <span className="mono">{m.maxTokens}</span>
                    </span>
                  ) : null}
                  {m.timeoutSeconds ? (
                    <span className="mcard__meta-item">
                      <span className="mcard__meta-key">超时</span>
                      <span className="mono">{m.timeoutSeconds}s</span>
                    </span>
                  ) : null}
                  <span className="mcard__meta-item">
                    <span className="mcard__meta-key">窗口</span>
                    <span className="mono">{fmtNum(m.contextWindow ?? 128000)}</span>
                  </span>
                  <span className="mcard__meta-item">
                    <span className="mcard__meta-key">思考</span>
                    <span>
                      {m.thinking == null ? '跟随默认'
                        : m.thinking ? '强制开启' : '强制关闭'}
                      {m.thinkingDepth ? ` · 深度${m.thinkingDepth === 'low' ? '轻'
                        : m.thinkingDepth === 'medium' ? '中' : '深'}` : ''}
                      {modelPresets.find(p => p.key === m.provider)?.thinkingSupported === false
                        ? '（该厂商未适配）' : ''}
                    </span>
                  </span>
                  {m.temperature != null && (
                    <span className="mcard__meta-item">
                      <span className="mcard__meta-key">温度</span>
                      <span className="mono">{m.temperature}</span>
                    </span>
                  )}
                  {m.topP != null && (
                    <span className="mcard__meta-item">
                      <span className="mcard__meta-key">top_p</span>
                      <span className="mono">{m.topP}</span>
                    </span>
                  )}
                  <span className="mcard__meta-item">
                    <span className="mcard__meta-key">图片输入</span>
                    <span>{(m.inputModalities ?? []).includes('image') ? '支持' : '不支持'}</span>
                  </span>
                </div>

                {m.maxTokensNote && (
                  <div className="mcard__note">
                    <Icon.Warning size={12} />
                    <span>{m.maxTokensNote}</span>
                  </div>
                )}

                {isEditing && editingModel && (
                  <div className="mcard__edit">
                    <ModelForm
                    ctxTouched={ctxTouched}
                    setCtxTouched={setCtxTouched}
                      draft={draft}
                      setDraft={setDraft}
                      presets={modelPresets}
                      showsKeyState
                      keyConfigured={editingModel.hasKey !== false}
                      busy={busy}
                      checking={checking}
                      canSkip={canSkip}
                      onCancel={() => setEditing(null)}
                      onSubmit={(skipCheck?: boolean) => { void submit(skipCheck); }}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </section>

      {editing === 'new' && (
        <div
          className="addmodel-overlay"
          role="presentation"
          onMouseDown={e => {
            if (e.target === e.currentTarget) setEditing(null);
          }}
        >
          <div className="addmodel-modal" role="dialog" aria-modal="true" aria-label="添加模型">
            <div className="addmodel-modal__head">
              <div>
                <div className="card__title">添加模型</div>
                <div className="card__sub">API Key 可留空，稍后补</div>
              </div>
              <button
                type="button"
                className="addmodel-modal__x"
                onClick={() => setEditing(null)}
                aria-label="关闭"
                title="关闭 (Esc)"
              >
                <Icon.X size={13} />
              </button>
            </div>
            <div className="addmodel-modal__body">
              <ModelForm
                    ctxTouched={ctxTouched}
                    setCtxTouched={setCtxTouched}
                draft={draft}
                setDraft={setDraft}
                presets={modelPresets}
                showsKeyState={false}
                keyConfigured={false}
                busy={busy}
                checking={checking}
                canSkip={canSkip}
                onCancel={() => setEditing(null)}
                onSubmit={(skipCheck?: boolean) => { void submit(skipCheck); }}
              />
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/* ── Permissions tab ───────────────────────────────────────────── */
/* ── Page ───────────────────────────────────────────────────────── */
export function SettingsPage() {
  const [tab, setTab] = useState<Tab>('models');
  const { setView } = useApp();

  return (
    <div className="page">
      {/* 不再画页标题「设置」：左栏已标出当前分区，顶部栏也有设置入口（2026-09-27 用户圈图去掉）。 */}

      <div className="settings-body">
        {/* 只剩一个 tab：不画 tab 头，直接展示内容 */}
        <ModelsTab />
      </div>
    </div>
  );
}