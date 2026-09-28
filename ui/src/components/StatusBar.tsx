import { useApp } from '../state/store';
import { Icon } from './Icon';
import { fmtTokens, fmtCount, ctxLevel, effectiveWindow } from '../state/usage';
import './StatusBar.css';

/**
 * 底部信息栏。
 *
 * 全部数字来自后端 usage_log 聚合（`usage.summary`），和「用量统计」页同一份
 * 数据。以前这里是编的：窗口写死 128k、token 用「字符数 ÷ 3.2 + 11400」估、
 * 输入输出按 82/18 拆、今日走 mock —— 现在一律不估。
 */
export function StatusBar() {
  const { usage, models, activeModelId, isStreaming, setView, turnOverrides } = useApp();

  const model = models.find(m => m.id === activeModelId) ?? models[0];
  // 上下文窗口跟输入区同一个口径：会话级覆盖优先，否则模型目录默认值
  //（见 state/usage.ts 的 effectiveWindow）。换窗口/换模型，这里的数字立刻跟着变。
  const window = effectiveWindow(model, turnOverrides?.contextWindow);

  // 上下文占用 = 最后一轮真实上报的 prompt token（厂商给的数，不是估的）。
  const used = usage?.session?.lastTurn?.input ?? 0;
  const pct = window > 0 ? Math.min(100, (used / window) * 100) : 0;
  const level = ctxLevel(pct);

  return (
    <footer className="statusbar">
      <div className="statusbar__left">
        <span className={`statusbar__state statusbar__state--${isStreaming ? 'busy' : 'idle'}`}>
          <span className={`statusbar__dot ${isStreaming ? 'pulse-dot' : ''}`} />
          {isStreaming ? '生成中' : '就绪'}
        </span>

        <span className="statusbar__sep" />

        {model?.name && (
          <span className="statusbar__item" title="当前模型">
            <span className="statusbar__model">{model.name}</span>
          </span>
        )}

        <span className="statusbar__item" title="最后一轮的上下文占用 / 当前生效窗口（会话级覆盖优先）">
          <span className="statusbar__label">上下文</span>
          <span className="statusbar__meter">
            <span
              className={`statusbar__meter-fill statusbar__meter-fill--${level}`}
              style={{ width: `${pct.toFixed(1)}%` }}
            />
          </span>
          <span className="mono statusbar__num">
            {fmtCount(used)} / {fmtTokens(window)}
          </span>
          <span className={`statusbar__pct statusbar__pct--${level}`}>{pct.toFixed(0)}%</span>
        </span>
      </div>

      <div className="statusbar__right">
        <span className="statusbar__item" title="本会话累计 token（厂商上报的输入 / 输出）">
          <span className="statusbar__label">本会话</span>
          <span className="mono statusbar__num">
            <span className="statusbar__in">↑{fmtTokens(usage?.session?.input ?? 0)}</span>
            <span className="statusbar__out">↓{fmtTokens(usage?.session?.output ?? 0)}</span>
          </span>
        </span>

        <span className="statusbar__sep" />

        <span className="statusbar__item" title="今日累计（本机时间）">
          <span className="statusbar__label">今日</span>
          <span className="mono statusbar__num">{fmtTokens(usage?.today?.tokens ?? 0)}</span>
        </span>

        <button
          className="statusbar__link"
          onClick={() => setView('usage')}
          title="打开用量统计"
        >
          用量统计
          <Icon.ChevronRight size={12} />
        </button>
      </div>
    </footer>
  );
}