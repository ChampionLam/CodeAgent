-- 006: 消息挂文件产出（2026-09-27）
--
-- 现场：助手在本机写好文件后，正文里只有一行路径，用户看不到、点不开，
-- 也没法把它当附件用（原话「我让他把文件直接发出来,那对话窗就要能直接把文件
-- 发出来到对话窗里面」）。
--
-- artifacts 存 JSON 数组：[{path, name, size, kind}]，挂在整轮最后一条
-- assistant 消息上。只存元数据，不存文件内容。
ALTER TABLE messages ADD COLUMN artifacts TEXT;
