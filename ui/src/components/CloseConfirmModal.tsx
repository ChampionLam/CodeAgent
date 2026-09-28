/**
 * 关程序但还有任务在跑时问一句（2026-09-25 定稿）。
 *
 * 主进程在 before-quit 里发现 `run.active` 非空就会发 close:confirm 过来；
 * 这里给三个选择，选完用 closeChoice 回话。渲染进程要是没答（卡死），主进程
 * 4 秒后按「中断并保存断点」退出 —— 绝不把用户卡在关不掉的窗口里。
 */

import { useEffect, useState } from 'react';
import { getAgent } from '../state/live';
import type { RunInfo } from '../state/live';
import './CloseConfirmModal.css';

export default function CloseConfirmModal() {
  const [runs, setRuns] = useState<RunInfo[] | null>(null);

  useEffect(() => {
    const agent = getAgent();
    if (!agent?.onCloseConfirm) return;
    return agent.onCloseConfirm(payload => {
      setRuns(Array.isArray(payload?.runs) ? payload.runs : []);
    });
  }, []);

  if (!runs) return null;

  const answer = (choice: 'wait' | 'interrupt' | 'cancel') => {
    setRuns(null);
    getAgent()?.closeChoice?.(choice);
  };

  const count = runs.length;
  const first = runs[0] || {};
  const where = first.rounds ? `第 ${first.rounds} 轮` : '';

  return (
    <div className="closemodal__mask" role="dialog" aria-modal="true">
      <div className="closemodal">
        <div className="closemodal__title">有任务正在执行</div>
        <div className="closemodal__body">
          {count > 1 ? `有 ${count} 个任务在执行` : '有 1 个任务在执行'}
          {where ? `（${where}）` : ''}，此时关闭将中断该任务。
          进度将保存为断点，下次打开后可点击「继续」恢复执行；已完成的步骤不会重复执行。
        </div>
        <div className="closemodal__actions">
          <button className="closemodal__btn" onClick={() => answer('wait')}>
            等待完成后再退出
          </button>
          <button className="closemodal__btn closemodal__btn--danger" onClick={() => answer('interrupt')}>
            中断并保存断点退出
          </button>
          <button className="closemodal__btn closemodal__btn--ghost" onClick={() => answer('cancel')}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}