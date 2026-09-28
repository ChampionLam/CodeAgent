/**
 * 「上次任务被中断 · 继续」这条提示条（2026-09-25 定稿）。
 *
 * 场景：断网拖到放弃、断电、关程序 —— 后端把那一轮存成了断点。这里只在
 * 真的有可续断点时才出现，用户点「继续」才接着跑（绝不自动烧 token）。
 */

import { useApp } from '../../state/store';
import './ResumeBar.css';

export default function ResumeBar() {
  const { pendingRun, resumeRun, dismissPendingRun, isStreaming } = useApp();
  if (!pendingRun || isStreaming) return null;

  const reason = pendingRun.reason || '被中断';
  const when = pendingRun.updated_at || '';
  const rounds = pendingRun.rounds ? `第 ${pendingRun.rounds} 轮` : '';

  return (
    <div className="resumebar" role="status">
      <div className="resumebar__text">
        <span className="resumebar__title">上次任务未完成</span>
        <span className="resumebar__meta">
          {[when, reason, rounds].filter(Boolean).join(' · ')}，进度已保存；继续后将恢复执行，
          已完成的步骤不会重复执行。
        </span>
      </div>
      <div className="resumebar__actions">
        <button className="resumebar__go" onClick={() => void resumeRun()}>
          继续
        </button>
        <button className="resumebar__drop" onClick={() => void dismissPendingRun()}>
          忽略
        </button>
      </div>
    </div>
  );
}