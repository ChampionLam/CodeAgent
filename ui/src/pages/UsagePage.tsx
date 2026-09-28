import { useEffect, useMemo, useState } from 'react';
import { useApp } from '../state/store';
import { Icon } from '../components/Icon';
import {
  fmtTokens, fmtCount, ctxLevel,
  compactTrigger, fillDays, shortDay, effectiveWindow
} from '../state/usage';
import './UsagePage.css';

type Range = 7 | 30;

/**
 * 用量统计。
 *
 * 口径：全部读后端 usage.summary（usage_log 表，一轮对话一行，token 来自
 * 厂商返回的 usage）。以前这页是纯 mock：假的「较上周 +12.4%」、写死的
 * 上下文行（11,400 / 68,200）。现在只显示库里真有的东西，取不到就不显示，
 * 不用估算值填。
 */
export function UsagePage() {
  const { usage, models, activeModelId, setView, refreshUsage, turnOverrides } = useApp();
  const [range, setRange] = useState<Range>(7);

  // 切窗口要重新取数：界面渲染的也是后端回来的 windowDays，不自己算。
  useEffect(() => {
    void refreshUsage(undefined, range);
  }, [range, refreshUsage]);

  const days = usage?.windowDays ?? range;
  const byDay = useMemo(() => fillDays(usage?.byDay ?? [], days), [usage?.byDay, days]);
  const byModel = usage?.byModel ?? [];
  const bySession = usage?.bySession ?? [];

  const maxTokens = Math.max(1, ...byDay.map(d => d.tokens));
  const windowTokens = usage?.window?.tokens ?? 0;
  const windowCalls = usage?.window?.calls ?? 0;
  const avgPerDay = days > 0 ? Math.round(windowTokens / days) : 0;
  const maxModelTokens = Math.max(1, ...byModel.map(m => m.tokens));
  const modelTokens = byModel.reduce((a, r) => a + r.tokens, 0);

  const model = models.find(m => m.id === activeModelId) ?? models[0];
  // 上下文压力跟输入区/底部栏同一个口径：会话级覆盖优先，否则模型目录默认值。
  const ctxWindow = effectiveWindow(model, turnOverrides?.contextWindow);
  const ctxUsed = usage?.session?.lastTurn?.input ?? 0;
  const trigger = compactTrigger(ctxWindow);

  const ctxRows = [
    { label: '当前会话', used: ctxUsed, limit: ctxWindow },
    { label: '压缩触发线', used: trigger, limit: ctxWindow }
  ].filter(r => r.limit > 0);

  return (
    <div className="page">
      <header className="page__head">
        <div className="page__head-row">
          <div>
            <h1 className="page__title">用量统计</h1>
          </div>
          <div className="seg">
            {([7, 30] as Range[]).map(r => (
              <button
                key={r}
                className={`seg__item ${range === r ? 'is-active' : ''}`}
                onClick={() => setRange(r)}
              >
                {r === 7 ? '最近 7 天' : '最近 30 天'}
              </button>
            ))}
            {/* 返回按钮由顶部那行统一提供，这里不再重复（2026-09-27）。 */}
          </div>
        </div>
      </header>

      {/* Stat cards */}
      <div className="stats">
        <div className="stat">
          <div className="stat__label">
            <Icon.ChartBar size={13} />
            最近 {days} 天 Token
          </div>
          <div className="stat__value mono">{fmtCount(windowTokens)}</div>
          <div className="stat__foot">
            <span className="stat__hint">
              平均 {fmtTokens(avgPerDay)} / 天 · {fmtCount(windowCalls)} 次调用
            </span>
          </div>
        </div>

        <div className="stat">
          <div className="stat__label">
            <Icon.Bolt size={13} />
            本会话 Token
          </div>
          <div className="stat__value mono">{fmtCount(usage?.session?.tokens ?? 0)}</div>
          <div className="stat__foot">
            <span className="stat__hint">
              输入 {fmtTokens(usage?.session?.input ?? 0)} · 输出 {fmtTokens(usage?.session?.output ?? 0)}
            </span>
          </div>
        </div>

        <div className="stat">
          <div className="stat__label">
            <Icon.Message size={13} />
            会话数
          </div>
          <div className="stat__value mono">{fmtCount(bySession.length)}</div>
          <div className="stat__foot">
            <span className="stat__hint">
              {byModel.length ? `涉及 ${byModel.length} 个模型` : '该时间范围内暂无记录'}
            </span>
          </div>
        </div>

        <div className="stat">
          <div className="stat__label">
            <Icon.Sparkles size={13} />
            今日
          </div>
          <div className="stat__value mono">{fmtCount(usage?.today?.tokens ?? 0)}</div>
          <div className="stat__foot">
            <span className="stat__hint">
              {fmtCount(usage?.today?.calls ?? 0)} 次调用
            </span>
          </div>
        </div>
      </div>

      {/* Daily chart */}
      <section className="card ucard">
        <div className="card__head">
          <div className="card__title">
            每日 Token 消耗
            <span className="card__sub">单位：tokens</span>
          </div>
          <div className="ucard__legend">
            <span className="ucard__legend-item">
              <span className="ucard__legend-swatch ucard__legend-swatch--today" />
              今日
            </span>
            <span className="ucard__legend-item">
              <span className="ucard__legend-swatch" />
              过往
            </span>
          </div>
        </div>
        <div className="card__body">
          {windowTokens === 0 ? (
            <p className="ucard__note">该时间范围内暂无调用记录。</p>
          ) : (
            <div className="chart">
              {byDay.map((d, i) => {
                const pct = (d.tokens / maxTokens) * 100;
                const isToday = i === byDay.length - 1;
                return (
                  <div className="chart__col" key={d.day ?? i}>
                    <div className="chart__val mono">{d.tokens ? fmtTokens(d.tokens) : ''}</div>
                    <div className="chart__track">
                      <div
                        className={`chart__bar ${isToday ? 'is-today' : ''}`}
                        style={{ height: `${pct}%` }}
                        title={`${d.day} · ${fmtCount(d.tokens)} tokens · ${d.calls} 次调用`}
                      />
                    </div>
                    <div className="chart__label mono">{shortDay(d.day)}</div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </section>

      {/* Per-model breakdown */}
      <section className="card ucard">
        <div className="card__head">
          <div className="card__title">
            模型分布
            <span className="card__sub">按模型统计调用次数与 token</span>
          </div>
        </div>
        <div className="card__body">
          {byModel.length === 0 ? (
            <p className="ucard__note">该时间范围内暂无按模型统计的记录。</p>
          ) : (
            <div className="modeltable">
              <div className="modeltable__head">
                <span>模型</span>
                <span className="modeltable__num">调用</span>
                <span className="modeltable__num">Token</span>
                <span className="modeltable__share">占比</span>
              </div>
              {byModel.map(row => {
                const share = modelTokens ? (row.tokens / modelTokens) * 100 : 0;
                const local = models.find(m => m.model === row.model);
                return (
                  <div className="modeltable__row" key={row.model}>
                    <div className="modelcell">
                      <span className="modelcell__icon">
                        <Icon.Sparkles size={12} />
                      </span>
                      <span className="modelcell__text">
                        <span className="modelcell__name">{local?.name ?? row.model}</span>
                        <span className="modelcell__id mono">{row.model}</span>
                      </span>
                    </div>
                    <span className="modeltable__num mono">{fmtCount(row.calls)}</span>
                    <span className="modeltable__num mono">{fmtTokens(row.tokens)}</span>
                    <span className="modeltable__share">
                      <span className="modeltable__bar">
                        <span
                          className="modeltable__bar-fill"
                          style={{ width: `${(row.tokens / maxModelTokens) * 100}%` }}
                        />
                      </span>
                      <span className="modeltable__pct mono">{share.toFixed(0)}%</span>
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </section>

      {/* Context pressure */}
      <section className="card ucard">
        <div className="card__head">
          <div className="card__title">
            上下文压力
            <span className="card__sub">
              窗口 {fmtTokens(ctxWindow)}（{model?.name ?? '当前模型'}，会话级覆盖优先）
            </span>
          </div>
        </div>
        <div className="card__body">
          {ctxRows.length === 0 ? (
            <p className="ucard__note">当前模型未设置上下文窗口。</p>
          ) : (
            <div className="ctxbars">
              {ctxRows.map(row => {
                const pct = (row.used / row.limit) * 100;
                const level = ctxLevel(pct);
                return (
                  <div className="ctxrow" key={row.label}>
                    <span className="ctxrow__label">{row.label}</span>
                    <span className="ctxrow__track">
                      <span
                        className={`ctxrow__fill ctxrow__fill--${level}`}
                        style={{ width: `${Math.min(100, pct).toFixed(1)}%` }}
                      />
                    </span>
                    <span className="ctxrow__num mono">
                      {fmtTokens(row.used)} / {fmtTokens(row.limit)}
                    </span>
                    <span className={`ctxrow__pct mono ctxrow__pct--${level}`}>
                      {pct.toFixed(0)}%
                    </span>
                  </div>
                );
              })}
            </div>
          )}
          <p className="ucard__note">
            上下文超过触发线（min(窗口 × 80%, 窗口 − 预留)）时 Agent 会自动摘要历史消息，
            摘要过程本身也会消耗 token。
          </p>
          <p className="ucard__note">
            口径说明：token 为厂商上报的真实用量；上下文占用取最后一轮的输入 token。
          </p>
        </div>
      </section>
    </div>
  );
}