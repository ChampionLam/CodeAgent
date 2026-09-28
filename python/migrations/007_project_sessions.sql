-- 007: 项目会话（2026-09-28）
--
-- 用户口径：「本质上还是个会话，只不过会加一些特性」——项目会话就是带
-- 额外属性的 session，不另起一套项目实体。所以全部落在 sessions 表上：
--
--   * kind：'chat'（默认，存量行自动归到这里）| 'project'
--   * project_name：项目名（界面侧的可读名，可与 title 不同）
--   * workspace_root：项目资料目录（工具的找/存文件基准），NULL = 未设置
--   * project_skills：JSON 数组，挂载的技能名，如 ["spec-kit","excel"]
--   * project_files：JSON 数组，拖进项目的资料文件元数据
--     [{name, path, size, mime}]
--
-- 只存元数据不存文件本体；skills/files 两个 JSON 列读回时按「坏了当空」
-- 处理（sessionstore._loads 的口径）。spec-kit 之类只是「可以被挂上的技能
-- 集」，代码里没有任何特殊逻辑。
ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat';
ALTER TABLE sessions ADD COLUMN project_name TEXT;
ALTER TABLE sessions ADD COLUMN workspace_root TEXT;
ALTER TABLE sessions ADD COLUMN project_skills TEXT;
ALTER TABLE sessions ADD COLUMN project_files TEXT;
