-- 联系人画像 + 运行状态，全放一个 SQLite 文件（默认 store.db）。不存聊天记录。
-- 特殊一行 chat='*default*' 是兜底画像；某人没填的字段就用它。
CREATE TABLE IF NOT EXISTS profile(
  chat        TEXT PRIMARY KEY,     -- username / wxid / xxx@chatroom
  alias       TEXT,
  relation    TEXT,                 -- 家人/朋友/同行/客户/供应商/员工/陌生人
  risk        TEXT,                 -- low / mid / high，决定哪些能直发
  mode        TEXT,                 -- auto 按风险表 / draft 全存草稿 / off 不回；空=跟随 *default*
  tone        TEXT,                 -- 口吻
  style       TEXT,                 -- 本人对 TA 的说话习惯：句长、口头禅、表情
  address     TEXT,                 -- 称呼对方
  topics      TEXT,                 -- 常聊话题
  sensitive   TEXT,                 -- 敏感点
  lang        TEXT,
  reply_when  TEXT,                 -- 群：mention 只回被@ / always / never
  rules       TEXT,                 -- 已废弃：专属规则改放 rules/<微信号>.md，旧值启动时自动搬过去
  locked      TEXT,                 -- 你手动改过、自动画像不许覆盖的字段，逗号分隔，如 'mode,risk'
  is_group    INTEGER DEFAULT 0,
  profiled_at INTEGER,              -- 上次自动画像时间
  updated     INTEGER
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);   -- 用户设置（settings.json 写入）、paused、计数
CREATE TABLE IF NOT EXISTS cursor(chat TEXT PRIMARY KEY, last_id INTEGER, ts INTEGER);  -- 已处理到的消息 id、会话时间戳
CREATE TABLE IF NOT EXISTS draft(               -- 只存“还没发”的回复，发/删后清掉
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT, alias TEXT,
  draft TEXT, ts INTEGER, sent INTEGER DEFAULT 0);
