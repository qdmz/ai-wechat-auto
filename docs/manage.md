# 管理与排查（要调规则、查问题时才读）

## 用 SQL 调画像
```sql
-- 看画像
SELECT alias, relation, risk, tone FROM profile WHERE profiled_at IS NOT NULL ORDER BY risk;
-- 某人改成低风险，并锁住不让自动画像改回去
UPDATE profile SET risk='low', locked='risk' WHERE alias='某个损友';
-- 供应商统一只存草稿
UPDATE profile SET mode='draft', locked='mode' WHERE relation='供应商';
-- 某个群只在被@时回 / 某人不回
UPDATE profile SET reply_when='mention' WHERE chat='123456@chatroom';
UPDATE profile SET mode='off', locked='mode' WHERE alias='XXX';
-- 看没发的草稿
SELECT * FROM draft WHERE sent=0;
```

## 文件
- `rules/`     每人一个专属规则文件（`_example.md` 是写法示例），隐私不提交
- `docs/`      部署、管理、接口说明，按需读
- `settings.example.json` 用户要填的资料模板（复制成 `settings.json`），每项的用途和场景见 `docs/settings.md`
- `schema.sql`  库结构：`profile`（画像）、`meta`（用户设置、paused、计数）、`cursor`（游标）、`draft`（草稿）
- `db.py`      SQLite 存取
- `awx.py`     微信接口、防重发的发送、通知
- `seed.py`    读 settings.json 写库、认出本人 username、导入所有会话
- `alert.py`   掉线告警：按 `alert` 发 Bark/邮件/webhook（`python3 alert.py test` 测试）
- `persona.py` 批量画像任务
- `reply_rules.py` 拼说明、风险表（本人在聊、群里叫没叫到本人由 wxtest 判）
- `watch.py`   值班循环：一直领任务、手里只拿一条，写进 inbox/ 等 Muse 处理（next / done；install 装 systemd 服务，cron 装 crontab 保活，up 给 hook 保活）
- `mcp.py`      外部工具（MCP）：列出 / 看说明 / 调用本人在控制面板接的 MCP（远程地址或 wxtest 里的本机程序），经 wxtest 转调，令牌不在本机
- `sysd.py`     systemd 保活：系统级优先、再试用户级；hook 每 10 秒查一次，重建丢了 unit 就重装；代理变量写进 .proxy.env 给服务读
- `boothook.py` 开机钩子兜底（只在 Muse/hatch 沙箱）：沙箱每次启动跑一次 `watch.py boot`；init.sh 已有内容不覆盖，只加一行；hook 每次顺手补装
- `keepalive.py` crontab 每分钟/开机调的保活脚本：值班没在跑就拉起；值班里每 10 分钟自检，cron 守护没跑就起、两行丢了就补
- `tick.py`    领一条（watch.py 循环调用）：挂在 wxtest 上领任务，要回就输出任务，没活什么都不输出；设置变了推过滤名单
- `hb.py`      后台心跳，`tick.py` 自动拉起，只防掉线（单条 5 分钟没交照样收回），Muse 停了 2 分钟自己退出
- `act.py`     执行 Muse 写的 JSON：存画像、按风险表定直发/草稿/通知，结论交回 wxtest 去发
- `say.py`     本人让代发时用：一条命令找人+发送+等结果
- `radar.py`   群商机雷达：本人问群里最近有什么求购/现货/行情时，一条命令拿到各群相关原话
- `sns.py`     朋友圈：评论任务怎么处理（通知/AI 回复），以及本人让发、看、回、删朋友圈时的一条命令

