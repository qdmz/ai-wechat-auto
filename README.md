# ai-wechat-auto 微信助手

让 AI（Muse 等 agent）当你的微信私人助理：读消息、给每个联系人自动画像，按画像和专属规则用不同口吻回复；按「风险 × 意图」决定直发、存草稿还是只通知本人；本人一开口，AI 立刻让位。

> 本仓库是**公开版**，只包含**值班助理层**的代码与文档，不包含任何私人备份数据：没有真实的设置文件、API 令牌、聊天数据库，也没有针对具体联系人/群的专属规则——仓库里的配置全是示例（`settings.example.json`、`awx.env.example`、`rules/_example.md`），部署时填你自己的。
>
> 微信客户端层是第三方作者 caichengle666 的容器镜像。本仓库默认通过 docker-compose 拉取**我们自己备份的该镜像副本** `ghcr.io/qdmz/ai-wechat-auto-bin`（与上游原镜像内容一致，备份是为了上游变动或不可用时仍能部署），不包含镜像本身的任何内容。

## 架构（三层）

```
微信客户端层（容器 wxtest）        值班系统（本仓库）              AI 会话
ghcr.io/qdmz/               ┌─> watch.py 每 10 秒领任务 ──>  你的 AI 平台
ai-wechat-auto-bin          │   tick.py 筛选/拼任务            （读 SKILL.md
（备份镜像，源自上游          │   act.py 按风险表执行发送           当值班规则）
 caichengle666）            │   persona.py 自动画像
  · Linux 微信本体           └── store.db 画像与设置（本机数据，
  · 本地库读取 + HTTP API         不进仓库）
  · 面板 /panel、扫码 /qr
```

- **客户端层**：跑官方 Linux 微信，扫码登录；对外提供 HTTP API（消息、会话、发送、语音、朋友圈等），只绑 `127.0.0.1:18500`。
- **值班系统**：纯 Python 标准库脚本，无第三方依赖。负责领任务、按风险筛选、整理上下文、执行发送与通知。
- **AI 会话**：大脑是 AI 平台本身，不接外部大模型；`SKILL.md` 是给 AI 的完整值班规则，脚本负责把任务整理成结构化材料交给它判断。

## 部署说明

前置：一台常开的 Linux 机器（x86_64）、Docker（含 compose 插件）、Python 3.10+，以及一个能跑 shell、能加载 `SKILL.md` 的 AI 平台。

```bash
git clone https://github.com/qdmz/ai-wechat-auto.git
cd ai-wechat-auto
bash install.sh
```

`install.sh` 会：检查环境 → 拉取镜像并 `docker compose up -d` → 生成 `awx.env`（600 权限）和 `settings.json` 初稿 → 打印后续步骤。然后按提示人工完成：

1. 取令牌：`docker exec ai-wechat-auto cat /data/wxauto/api_token`，填进 `awx.env`。
2. 扫码登录：另一块屏幕打开 `http://127.0.0.1:18500/qr?token=<令牌>`，手机微信扫码。控制面板在 `http://127.0.0.1:18500/panel`。
3. 填 `settings.json`：照 `settings.example.json` 里的中文说明填，`me`（你的名字）、`title`（别人怎么称呼你）、`agent`（这台 agent 的名字）、`redlines`（AI 绝不能碰的事）必填。
4. 初始化：`python3 seed.py`（认出本人微信号、导入会话、写入设置）。
5. 启动值班：`python3 watch.py` 前台跑，或用 `systemd/awx-watch.service` 模板装成系统服务常驻。
6. 在 AI 平台里加载本目录的 `SKILL.md`，值班就开始工作了。

### 镜像说明（默认用备份镜像）

- 默认镜像：`ghcr.io/qdmz/ai-wechat-auto-bin:latest`，是我们对上游第三方镜像的原样备份。可用标签：`latest` / `20261110`（当前版）、`v1.5.0`（旧版）；换版本改 `docker-compose.yml` 的 `image:` 一行即可。
- 拉不到备份镜像时（网络不通、仓库权限未放行等），把 `image:` 换成上游原地址 `ghcr.io/caichengle666/ai-wechat-auto-bin:latest` 再 `docker compose pull`，内容与功能完全一致。
- 数据卷 `wxdata` 存微信登录态与令牌：卷还在，重建容器不用重新扫码；**这个卷是你自己的私人数据，不要打包分享给任何人**。

## 配置说明

| 文件 | 内容 |
|---|---|
| `awx.env` | `AWX_BASE`（客户端 API 地址，本机为 `http://127.0.0.1:18500`）、`AWX_TOKEN`（API 令牌）。600 权限，**绝不进 git** |
| `settings.json` | 全部行为设置：身份与称呼、红线、直发/草稿模式、通知去向、语音、各自动化开关。逐项说明见 `settings.example.json` 与 `docs/settings.md` |
| `rules/` | 按会话的专属规则：`rules/<微信号或群号>.md`，另有 `_default.md` 通用规则与 `标签-<标签名>.md` 分组规则；写法见 `rules/_example.md`。改完立刻生效 |
| `styles/` | 各声音的说话风格卡（`styles/<声音名>.md`），仓库只带 `default.md` |
| `store.db` | 画像、设置、草稿的本机数据库，运行时自动生成，**绝不进 git** |

## 功能清单

- **自动回复与风险分级**：意图分闲聊/技术/价格/正事/敏感/看不懂六类，叠加联系人风险等级（low/mid/high）成风险表，逐格决定直发、存草稿或只通知；拿不准就不回。
- **草稿 / 直发双模式**：`rollout=draft` 试运行时全部先存草稿，本人确认后再放开直发；本人正在聊的会话 AI 自动让位（接管延时可设）。
- **群聊 @ 规则**：群里只在被 @ 或被叫到名字时才回复（逐群可设 全回 / 只回@ / 不回），群不画像，另有群聊摘要、群日报等。
- **自动画像**：读每人最近聊天总结成画像（关系、风险、口吻、称呼、敏感点），只存结论；配合微信标签分组与 `rules/` 专属规则分人回复。
- **语音**：对方发语音自动转文字；AI 可回语音——Fish Audio（可克隆本人声音、多声音）优先，通用 OpenAI 兼容语音接口次之，Cloudflare Workers AI 兜底；按标签/按人指定声音与是否回语音。
- **图片与视频**：能看图（需视觉模型开关）、能按要求生成图片/视频并作为真实媒体消息发送。
- **防撤回与补收**：对方撤回的消息会把原文通知本人；掉线/重启期间晚到的消息进「补收」汇总，不自动乱回。
- **日常自动化**：好友申请（自动通过/通知/忽略）、名片通知、拍一拍回拍、朋友圈点赞与评论处理（通知或以助理身份回闲聊）、每天定时日报。
- **多台 agent 协同**：画像与设置可在多台 agent 间同步（psync）；值班脚本自带 systemd/cron 双路保活与掉线多通道告警（Bark / Server酱 / 邮件 / webhook）。
- **受控自更新**：值班代码可由客户端下发更新（带指纹核对与备份回滚）；说明文档的修改走验证码确认。

细节看 `docs/`：`guide.md` 使用指南、`settings.md` 设置全解、`api.md` 客户端 API、`manage.md` 日常管理、`voice.md` 语音、`migration.md` 聊天记录迁移。

## 安全提醒（务必先读）

- **令牌等于微信控制权**：`awx.env`、`settings.json`、`store.db` 只放本机、权限收紧，仓库已用 `.gitignore` 排除，**永远不要手动提交它们**。本公开仓库从未包含维护者自己的这类文件；你部署后产生的文件同样只属于你自己。
- 本项目仅供**个人学习交流**。微信自动化属于非官方用法，存在掉线、功能限制乃至封号的风险，**风险自担**；请勿用于群发营销、骚扰或任何违法违规用途。
- 同一个微信同时只能登录一处 Linux 微信；换机器部署后旧机器会掉线。
- AI 回复默认带助理备注（可在设置里关），请对联系人保持坦白：这是 AI 助理在回复。

## 致谢与上下游

- 微信客户端层原作者：caichengle666 的 `ai-wechat-auto`（上游镜像 `ghcr.io/caichengle666/ai-wechat-auto-bin`）；本仓库默认使用的 `ghcr.io/qdmz/ai-wechat-auto-bin` 是该镜像的备份副本，本仓库的值班系统通过其 HTTP API 工作。
- 值班系统的设计目标是「别发错」：看不懂、拿不准就不回；正事永远先通知本人。
