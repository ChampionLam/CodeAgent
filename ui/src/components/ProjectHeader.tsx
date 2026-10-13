/**
 * 会话头部项目信息条（项目会话契约 v1 §3.2.4）。
 *
 * - 只在 kind=project 的会话上渲染；普通会话头部一字不变（TopBar 不挂它）。
 * - 展示：项目名 / 项目目录（挂了才显示）/ 已挂技能 chips。
 * - 点击「编辑」进入编辑态：项目名、目录两行输入 + 技能多选（数据源同
 *   新建项目弹窗：window.agent.skills()）。保存 → sessions.updateProject
 *   （局部更新，只传改过的键）；取消则回到展示态。
 * - 老 preload 没有 updateProject 时 store 会退回乐观更新，界面照常。
 */
import { useEffect, useRef, useState } from 'react';
import { useApp } from '../state/store';
import { getAgent } from '../state/live';
import type { Session, SkillInfo } from '../types';
import { Icon } from './Icon';
import './TopBar.css';

const ERR_BRIDGE = '未连接本地服务，技能列表不可用。';
const ERR_FETCH = '技能列表加载失败，请稍后重试。';

function baseOf(p: string): string {
  const norm = p.replace(/\\/g, '/').replace(/\/+$/, '');
  const i = norm.lastIndexOf('/');
  return i >= 0 ? norm.slice(i + 1) : norm;
}

export function ProjectHeader({ session }: { session: Session }) {
  const [editing, setEditing] = useState(false);

  if (editing) {
    return <ProjectHeaderEditor session={session} onDone={() => setEditing(false)} />;
  }

  const skills = session.projectSkills ?? [];
  const root = session.workspaceRoot;

  return (
    <div className="projhead">
      <span className="projhead__name" title={session.projectName ?? undefined}>
        <Icon.Folder size={12} className="projhead__icon" />
        {session.projectName ?? session.title}
      </span>
      {root && (
        <span className="projhead__root" title={root}>
          {baseOf(root)}
        </span>
      )}
      {skills.length > 0 ? (
        <span className="projhead__skills" title={`已挂技能：${skills.join('、')}`}>
          {skills.map(s => (
            <span key={s} className="projhead__chip">{s}</span>
          ))}
        </span>
      ) : (
        <span className="projhead__noskills">全部技能</span>
      )}
      <button
        className="projhead__edit"
        onClick={() => setEditing(true)}
        title="编辑项目信息"
        aria-label="编辑项目信息"
      >
        <Icon.Pencil size={12} />
      </button>
    </div>
  );
}

function ProjectHeaderEditor({
  session,
  onDone
}: {
  session: Session;
  onDone: () => void;
}) {
  const { updateProject } = useApp();
  const [name, setName] = useState(session.projectName ?? '');
  const [root, setRoot] = useState(session.workspaceRoot ?? '');
  const [picked, setPicked] = useState<string[]>(session.projectSkills ?? []);
  const [skills, setSkills] = useState<SkillInfo[] | null>(null);
  const [skillsMsg, setSkillsMsg] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  // 打开编辑才拉一次技能表；没有 bridge / 失败时给提示，多选区退化为已选名单。
  useEffect(() => {
    const agent = getAgent();
    if (!agent?.skills) {
      setSkillsMsg(ERR_BRIDGE);
      return;
    }
    let cancelled = false;
    agent.skills()
      .then(r => {
        if (cancelled) return;
        setSkills(r?.skills ?? []);
      })
      .catch(() => {
        if (cancelled) return;
        setSkillsMsg(ERR_FETCH);
      });
    return () => { cancelled = true; };
  }, []);

  const save = async () => {
    const cleanName = name.trim();
    if (!cleanName) {
      setError('项目名不能为空');
      nameRef.current?.focus();
      return;
    }
    // 只传要改的键（契约 §1.2 局部更新）：跟当前值比出差异。
    const patch: { projectName?: string; workspaceRoot?: string; projectSkills?: string[] } = {};
    if (cleanName !== (session.projectName ?? '')) patch.projectName = cleanName;
    if (root.trim() !== (session.workspaceRoot ?? '')) patch.workspaceRoot = root.trim();
    if (picked.join('\u0000') !== (session.projectSkills ?? []).join('\u0000')) {
      patch.projectSkills = picked.slice();
    }
    setSaving(true);
    try {
      await updateProject(session.id, patch);
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSaving(false);
    }
  };

  const toggle = (skillName: string) => {
    setPicked(prev =>
      prev.includes(skillName) ? prev.filter(x => x !== skillName) : [...prev, skillName]
    );
  };

  return (
    <div className="projhead projhead--editing" role="dialog" aria-label="编辑项目信息">
      <span className="projhead__field">
        <span className="projhead__label">名称</span>
        <input
          ref={nameRef}
          value={name}
          onChange={e => { setName(e.target.value); if (error) setError(''); }}
          placeholder="项目名"
          aria-label="项目名"
          maxLength={80}
        />
      </span>
      <span className="projhead__field">
        <span className="projhead__label">目录</span>
        <input
          value={root}
          onChange={e => { setRoot(e.target.value); if (error) setError(''); }}
          placeholder="绝对路径，留空跟随全局"
          aria-label="项目目录"
          spellCheck={false}
        />
      </span>
      <span className="projhead__field projhead__field--skills">
        <span className="projhead__label">技能</span>
        {skills && skills.length > 0 ? (
          <span className="projhead__skillpick">
            {skills.map(s => {
              const on = picked.includes(s.name);
              return (
                <button
                  key={s.name}
                  type="button"
                  className={`projhead__pick ${on ? 'is-on' : ''}`}
                  title={s.description || s.name}
                  onClick={() => toggle(s.name)}
                >
                  {s.name}
                </button>
              );
            })}
          </span>
        ) : (
          <span className="projhead__skillmsg">{skillsMsg || '暂无已加载技能。'}</span>
        )}
      </span>
      {error && <span className="projhead__error" role="alert">{error}</span>}
      <span className="projhead__actions">
        <button className="projhead__cancel" onClick={onDone} disabled={saving}>取消</button>
        <button className="projhead__save" onClick={() => void save()} disabled={saving}>
          {saving ? '保存中…' : '保存'}
        </button>
      </span>
    </div>
  );
}
