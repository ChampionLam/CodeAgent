-- 005: 思考通道独立落库（2026-09-24）
--
-- 现场：assistant 的 content 里带着 <thinking>…</thinking> 原文 ——
-- reasoning_split 把思考发给了界面（所以当场看是对的），但落库和回灌模型用的
-- 还是原始 delta，于是界面一重载，整段思考就当作正文显示出来了。
--
-- reasoning 列把思考单独存：正文只留答案，思考进折叠块，重载也不丢、不混。
ALTER TABLE messages ADD COLUMN reasoning TEXT;