import { useState } from 'react';
import type { ToolCall, ToolStatus } from '../../types';
import { Icon, ToolGlyph } from '../Icon';
import { resolveImageSrc } from '../../state/live';
import { imageRefsIn } from '../../lib/imageRefs';
import { MediaRef } from '../chat/MediaRef';
import './ToolCallCard.css';

const STATUS_META: Record<ToolStatus, { label: string; chip: string }> = {
  running:  { label: '执行中',  chip: 'chip--accent' },
  awaiting: { label: '待确认',  chip: 'chip--warn' },
  success:  { label: '已完成',  chip: 'chip--success' },
  failed:   { label: '失败',    chip: 'chip--danger' },
  rejected: { label: '已拒绝',  chip: 'chip--neutral' }
};

// 人话工具名：卡片上只写用户能看懂的用途，不出现 run_shell / grep 这种
// 内部名。等级标签（L0–L3）分级整体下线，界面上不再显示。
const TOOL_LABELS: Record<string, string> = {
  run_shell: '执行命令',
  read_file: '读文件',
  write_file: '写文件',
  edit_file: '改文件',
  delete_path: '删文件',
  list_dir: '看目录',
  search_files: '搜文件',
  grep: '搜文件',
  glob: '找文件',
  web_fetch: '抓网页',
  web_search: '联网搜索',
  read_image: '看图片',
  vision_analyze: '看图片',
  image_generate: '生成图片',
  video_generate: '生成视频',
  read_skill: '读文档',
  list_skills: '列文档',
  save_rule: '存规则',
  remove_rule: '删规则'
};

function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name;
}

function fmtDuration(start: number, end?: number) {
  if (!end) return null;
  const ms = end - start;
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m${Math.round((ms % 60_000) / 1000)}s`;
}

export function ToolCallCard({ call, defaultOpen = false }: { call: ToolCall; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const meta = STATUS_META[call.status];
  const Glyph = ToolGlyph[call.name as keyof typeof ToolGlyph] ?? ToolGlyph.default;
  const duration = fmtDuration(call.startedAt, call.endedAt);
  const isLive = call.status === 'running' || call.status === 'awaiting';

  return (
    <div className={`toolcard toolcard--${call.status} ${open ? 'is-open' : ''}`}>
      <button
        className="toolcard__head"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
      >
        <span className="toolcard__glyph">
          <Glyph size={14} />
          {isLive && <span className="toolcard__glyph-ring" />}
        </span>

        <span className="toolcard__summary">
          <span className="toolcard__summary-text">{toolLabel(call.name)}</span>
        </span>

        <span className="toolcard__badges">
          <span className={`chip ${meta.chip}`}>
            {isLive && <span className="toolcard__spin" aria-hidden />}
            {meta.label}
          </span>
          {duration && <span className="toolcard__dur mono">{duration}</span>}
        </span>

        <span className={`toolcard__chevron ${open ? 'is-open' : ''}`}>
          <Icon.ChevronDown size={13} />
        </span>
      </button>

      {isLive && <span className="toolcard__progress shimmer" aria-hidden />}

      {open && (
        <div className="toolcard__body">
          {call.result && (
            <div className="toolcard__section">
              <div className="toolcard__section-label">
                {call.status === 'failed' ? '错误输出' : '输出'}
              </div>
              <pre
                className={`toolcard__result mono ${call.status === 'failed' ? 'is-error' : ''}`}
              >
                {call.result}
              </pre>
              {(() => {
                // A tool that produced media says so as a path — show it.
                const refs = imageRefsIn(call.result).filter(r => resolveImageSrc(r));
                if (!refs.length) return null;
                return (
                  <div className="toolcard__images">
                    {refs.slice(0, 6).map(p => (
                      <MediaRef key={p} value={p} className="toolcard__image" />
                    ))}
                  </div>
                );
              })()}
            </div>
          )}

          {!call.result && isLive && (
            <div className="toolcard__section">
              <div className="toolcard__section-label">输出</div>
              <div className="toolcard__pending">
                <span className="toolcard__pending-bar" />
                <span className="toolcard__pending-bar" />
                <span className="toolcard__pending-bar" />
                <span className="toolcard__pending-text">等待工具返回…</span>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}