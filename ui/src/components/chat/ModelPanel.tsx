import { useEffect, useRef, useState } from 'react';
import { useApp } from '../../state/store';
import { Icon } from '../Icon';
import type { RemoteModelPreset } from '../../state/live';
import { CTX_LADDER, formatCtx, windowChoices } from '../../pages/modelDraft';
import { vendorLabel } from '../../state/vendors';
import type { ModelConfig, ThinkingDepth } from '../../types';
import './ModelPanel.css';

/**
 * 窗口读法走 pages/modelDraft.ts 的 formatCtx：厂商报的 1000000 读成 1M，
 * 而不是 976.6k（用户 2026-09-27 报过「1M 的模型看起来只有 128k」这类对不上）。
 * 气泡/底部栏的 token 用量仍用 state/usage.ts 的 fmtTokens，那是个数不是窗口。
 */
const short = (n: number | null | undefined): string => formatCtx(n);

/** 厂商档位的中文写法。没见过的档位名原样显示，不硬套。 */
const DEPTH_LABEL: Record<string, string> = {
  minimal: '最低', low: '低', medium: '中', high: '高', xhigh: '更高', max: '极致'
};

/** 上下文窗口档位（用户 2026-09-27 口径）：64k / 128k / 256k / 512k / 1M，
 *  上限以内铺一遍，外加上限本身。计算在 pages/modelDraft.ts，那边有单测。 */
const LADDER = CTX_LADDER;

/** 目录事实：擅长。目录里没有这条模型就空——不编数。 */
function catalogStrengths(cfg: ModelConfig | undefined, presets: RemoteModelPreset[]): string[] {
  if (!cfg) return [];
  const preset = presets.find(p => p.key === cfg.provider);
  const own = preset?.catalog?.find(c => c.model === cfg.model);
  const anyLine = own ?? presets
    .map(p => p.catalog?.find(c => c.model === cfg.model))
    .find(Boolean);
  return anyLine?.strengths ?? [];
}

/**
 * 模型面板（用户口径 2026-09-26 晚，第三版）：
 * **点开只有模型列表**；鼠标移到哪一行，才在列表左边浮出那一行的详情。
 *
 * - 列表：一行一个模型（名字 + 没配 key 标记 + 右侧窗口数值）。**点名字才是切换模型**；
 *   点完面板不关，选中态跟着变。
 * - 详情：**悬停才出现**，浮在列表左边（不占流、不撑高列表）。鼠标能从列表挪进详情
 *   不闪断（离开延迟 160ms 再收，详情自己 onMouseEnter 取消这个延迟）。高度按窗口
 *   剩余空间钳一下，免得「未配 key」那种多一行的模型把卡片顶出屏幕。
 * - 详情里的思考强度 / 上下文窗口，**对悬停到的哪个模型都可点**（用户口径：非当前
 *   模型也能选上下文，只有点名字才是切换）。值按「会话 × 模型」存，落在那个模型头上，
 *   不写回模型配置；切到该模型时生效。见 state/store.tsx 的 sessionOverrides。
 * - 价格整行已按用户要求去掉。
 * - 底部「配置模型」跳设置页。
 */
export function ModelPanel({ onClose }: { onClose: () => void }) {
  const {
    models, activeModelId, setActiveModel, sessionOverrides, setModelOverride,
    modelPresets, setView
  } = useApp();

  const [row, setRow] = useState<'think' | 'window' | null>(null);
  /** 小窗体装不下就往上翻。 */
  const [flip, setFlip] = useState(false);
  /** 当前悬停的模型：它决定左边那块详情显示什么；没人悬停就不显示详情。 */
  const [hoverId, setHoverId] = useState<string | null>(null);
  /** 详情浮层默认贴列表左边；窗口很窄、左边塞不下才翻到右边。 */
  const [flyRight, setFlyRight] = useState(false);
  /** 详情的高度上限（按窗口剩余空间算），免得顶出屏幕。 */
  const [maxH, setMaxH] = useState<number | null>(null);
  const closeTimer = useRef<number | null>(null);
  const graceRef = useRef<((e: MouseEvent) => void) | null>(null);

  useEffect(() => () => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    dropGrace();
  }, []);

  /**
   * 选完 / 收起小窗体那一下：小窗体是卡片的子节点，它被卸载时浏览器会重算指针下的
   * 元素，给卡片补发一次**假 mouseleave**——照单全收就把整块卡片收掉（用户反馈
   * 「点开再点合，直接整个弹窗都没了」）。所以这里挂一个一次性 mousemove：
   * 鼠标**真的**移出面板才收，还在面板里就继续留着。
   */
  const dropGrace = () => {
    if (graceRef.current) {
      document.removeEventListener('mousemove', graceRef.current, true);
      graceRef.current = null;
    }
  };
  const keepUntilMove = () => {
    cancelClose();
    dropGrace();
    const onMove = (e: MouseEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && typeof t.closest === 'function' && t.closest('.mpanel')) return;
      dropGrace();
      setHoverId(null);
    };
    graceRef.current = onMove;
    document.addEventListener('mousemove', onMove, true);
  };

  const cancelClose = () => {
    if (closeTimer.current) { window.clearTimeout(closeTimer.current); closeTimer.current = null; }
  };
  /** 鼠标现在在详情卡片里吗（二级菜单浮在卡片外面，得靠它判断）。 */
  const inDetail = useRef(false);
  /**
   * 离开列表/详情后延迟收起：给鼠标挪进详情留时间，不然一移就闪没。
   * **二级菜单开着就不收**：那个小菜单按设计浮在卡片外面（`right: calc(100% + 8px)`），
   * 鼠标去点它必然先离开卡片——不收的话卡片会带着菜单一起消失，点都点不到。
   */
  const scheduleClose = () => {
    cancelClose();
    // 小窗体开着、或刚选完还在保护期里，都不收
    if (row !== null || graceRef.current) return;
    closeTimer.current = window.setTimeout(() => setHoverId(null), 160);
  };
  const hoverIn = (id: string, el: HTMLElement) => {
    cancelClose();
    dropGrace();
    inDetail.current = false;
    setHoverId(id);
    const wrap = el.closest('.mpanel') as HTMLElement | null;
    const box = wrap?.getBoundingClientRect();
    if (box) {
      setFlyRight(box.left - 252 < 8);
      // 卡片往上长，可用空间是「列表下沿往上直到窗口顶」
      setMaxH(Math.max(180, Math.round(box.bottom - 10)));
    }
  };

  const shown = hoverId ? (models.find(m => m.id === hoverId) ?? null) : null;
  /** 这个模型头上的会话级覆盖（没设过就是空）。 */
  const ov = (shown ? sessionOverrides[shown.id] : undefined) ?? {};
  /** 列表右侧那排窗口数值：设过覆盖就显示覆盖值，否则显示模型自己配的。 */
  const rowWindow = (m: ModelConfig) =>
    (sessionOverrides[m.id]?.contextWindow ?? m.contextWindow) ?? null;

  // 详情里的一切都按**悬停到的那个模型**算（用户口径：非当前模型也能设）
  const thinking = shown
    ? (ov.thinking !== undefined ? ov.thinking : (shown.thinking ?? null))
    : null;
  const depth = shown
    ? (ov.thinkingDepth !== undefined ? ov.thinkingDepth : (shown.thinkingDepth ?? null))
    : null;
  const levels = (shown?.thinkingDepthLevels ?? []).filter(Boolean) as ThinkingDepth[];
  const supportsDepth = levels.length > 0;
  /** 这条线路有没有真正的思考开关；未适配就整行写成静态说明。 */
  const thinkAdapted = shown?.thinkingAdapted !== false;
  const effectiveWindow = shown ? (ov.contextWindow ?? shown.contextWindow ?? null) : null;

  const thinkValue = thinking === false
    ? '不思考'
    : supportsDepth
      ? `思考 ${depth ? (DEPTH_LABEL[depth] ?? depth) : '中'}`
      : '思考';

  /** 小窗体大概多高：一项按 26px 估，只用来决定往上翻还是往下翻。 */
  const estPopHeight = (which: 'think' | 'window') => {
    const n = which === 'think'
      ? (supportsDepth ? levels.length + 1 : 2)
      : windowChoices(shown?.contextWindow).length + 1;
    return n * 26 + 12;
  };

  /** 点行开/收旁边的小窗体；装不下就往上翻（别顶出窗口底）。 */
  const toggle = (which: 'think' | 'window', el: HTMLElement | null) => {
    if (row === which) { setRow(null); keepUntilMove(); return; }
    const top = el?.getBoundingClientRect().top ?? 0;
    setFlip(top + estPopHeight(which) > window.innerHeight - 8);
    setRow(which);
  };

  /** 选完就把二级弹窗收起来（用户口径：点完选项框要关掉）；值挂在这个模型头上。 */
  const pick = (patch: Parameters<typeof setModelOverride>[1]) => {
    if (shown) setModelOverride(shown.id, patch);
    setRow(null);
    keepUntilMove();
  };

  return (
    <div className="mpanel" role="group" aria-label="模型选择">
      {/* 列表：点开面板只看得到它；点名字 = 切换模型 */}
      <div className="mpanel__list">
        {models.length === 0 && (
          <div className="mpanel__empty">尚未配置模型，请在设置中添加</div>
        )}
        {models.map(m => (
          <button
            key={m.id}
            type="button"
            className={`mpanel__item ${m.id === activeModelId ? 'is-active' : ''}`}
            onClick={() => { setActiveModel(m.id); setRow(null); }}
            onMouseEnter={e => hoverIn(m.id, e.currentTarget)}
            onMouseLeave={scheduleClose}
            onFocus={e => hoverIn(m.id, e.currentTarget)}
            onBlur={scheduleClose}
          >
            <span className="mpanel__item-name">{m.name}</span>
            {m.hasKey === false && <span className="mpanel__tag">未配 key</span>}
            <span className="mpanel__item-num mono">{short(rowWindow(m))}</span>
          </button>
        ))}
        <button
          type="button"
          className="mpanel__add"
          onClick={() => { setView('settings'); onClose(); }}
        >
          <Icon.Cog size={12} />
          <span>配置模型</span>
        </button>
      </div>

      {/* 详情：悬停才出现，浮在列表左边，不占流 */}
      {shown && (
        <div
          className={`mpanel__detail ${flyRight ? 'is-right' : ''}`}
          style={maxH ? { maxHeight: `${maxH}px` } : undefined}
          onMouseEnter={() => { inDetail.current = true; cancelClose(); }}
          onMouseLeave={() => { inDetail.current = false; scheduleClose(); }}
        >
          <div className="mpanel__title">{shown.name}</div>
          <div className="mpanel__desc">
            {catalogStrengths(shown, modelPresets).length
              ? catalogStrengths(shown, modelPresets).join(' · ')
              : shown.model}
          </div>
          {shown.hasKey === false && (
            <div className="mpanel__warn">
              <Icon.Warning size={12} />
              <span>该模型未配置 key，配置后方可使用</span>
            </div>
          )}

          <div className="mpanel__rows">
            {/* 思考强度：这条线路没有可用旋钮时不藏行，写成静态说明——
                藏起来用户会以为「思考深度没设置」（2026-09-25 反馈）。 */}
            {!thinkAdapted && (
              <div
                className="mpanel__row mpanel__row--static"
                title={
                  '该线路的思考开关由厂商侧固定为开启，且无可调深度参数：实测 '
                  + 'enable_thinking=false 被拒（400 restricted to True），'
                  + 'thinking_budget 可提交但不生效。请求中不发送任何思考键。'
                }
              >
                <span className="mpanel__row-label">思考强度</span>
                <span className="mpanel__row-value">固定开启</span>
              </div>
            )}
            {thinkAdapted && (
              <div className="mpanel__rowwrap">
                <button
                  type="button"
                  className={`mpanel__row ${row === 'think' ? 'is-open' : ''}`}
                  onClick={e => toggle('think', e.currentTarget)}
                  aria-expanded={row === 'think'}
                >
                  <span className="mpanel__row-label">思考强度</span>
                  <span className="mpanel__row-value">
                    {thinkValue}
                    <Icon.ChevronRight size={10} />
                  </span>
                </button>
                {row === 'think' && (
                  <div className={`mpanel__pop ${flip ? 'is-up' : ''}`} role="menu">
                    {/* 厂商侧「始终思考」的模型（glm-5.3：目录 thinking.off=null）不发
                        「不思考」——发了就是 400，摆出来是骗人。 */}
                    {shown.thinkingCanDisable !== false && (
                      <button
                        type="button"
                        className={`mpanel__choice ${thinking === false ? 'is-active' : ''}`}
                        onClick={() => pick({ thinking: false })}
                      >
                        不思考
                        {thinking === false && <Icon.Check size={11} />}
                      </button>
                    )}
                    {supportsDepth ? levels.map(l => (
                      <button
                        key={l}
                        type="button"
                        className={`mpanel__choice ${thinking !== false && depth === l ? 'is-active' : ''}`}
                        onClick={() => pick({ thinking: true, thinkingDepth: l })}
                      >
                        {DEPTH_LABEL[l] ?? l}
                        {thinking !== false && depth === l && <Icon.Check size={11} />}
                      </button>
                    )) : (
                      <button
                        type="button"
                        className={`mpanel__choice ${thinking === true ? 'is-active' : ''}`}
                        onClick={() => pick({ thinking: true })}
                      >
                        思考
                        {thinking === true && <Icon.Check size={11} />}
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}

            <div className="mpanel__rowwrap">
              <button
                type="button"
                className={`mpanel__row ${row === 'window' ? 'is-open' : ''}`}
                onClick={e => toggle('window', e.currentTarget)}
                aria-expanded={row === 'window'}
              >
                <span className="mpanel__row-label">上下文窗口</span>
                <span className="mpanel__row-value">
                  {effectiveWindow ? short(effectiveWindow) : '—'}
                  <Icon.ChevronRight size={10} />
                </span>
              </button>
              {row === 'window' && (
                <div className={`mpanel__pop ${flip ? 'is-up' : ''}`} role="menu">
                  {/* 没有「默认」这一项（用户 2026-09-27 口径：不要默认字样，默认是哪个
                      就直接指向哪个）。当前生效的那一档（会话覆盖 > 模型配置值）直接标出来，
                      点它就等于把会话窗口设成这个值。 */}
                  {windowChoices(shown.contextWindow).map(w => {
                    const active = effectiveWindow != null && short(effectiveWindow) === short(w);
                    return (
                      <button
                        key={w}
                        type="button"
                        className={`mpanel__choice ${active ? 'is-active' : ''}`}
                        onClick={() => pick({ contextWindow: w })}
                      >
                        {short(w)}
                        {active && <Icon.Check size={11} />}
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          <div className="mpanel__foot mono">
            {vendorLabel(shown.provider, shown.baseUrl) || '—'} · {shown.model}
          </div>
        </div>
      )}
    </div>
  );
}
