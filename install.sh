#!/usr/bin/env bash
# ai-wechat-auto 值班系统一键部署：
#   1) 检查 docker / python3
#   2) 用 docker compose 拉起微信客户端容器（默认拉取本项目备份镜像
#      ghcr.io/qdmz/ai-wechat-auto-bin，见 docker-compose.yml 的说明）
#   3) 生成 awx.env 与 settings.json 初稿（已存在的不覆盖）
#   4) 打印后续人工步骤：取令牌 -> 扫码登录 -> 填设置 -> seed.py -> 启动值班
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n== %s ==\n' "$*"; }

say "1/4 检查环境"
command -v docker >/dev/null 2>&1 || { echo "没有 docker：先装 Docker Engine 再重跑。"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "没有 docker compose 插件：先装 docker-compose-plugin 再重跑。"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "没有 python3：值班脚本需要 Python 3.10+。"; exit 1; }

say "2/4 拉取镜像并启动微信客户端容器"
docker compose pull
docker compose up -d

say "3/4 生成本机配置初稿"
if [ ! -f awx.env ]; then
  cp awx.env.example awx.env
  chmod 600 awx.env
  echo "已生成 awx.env（权限 600）：等下把令牌填进 AWX_TOKEN= 后面。"
else
  echo "awx.env 已存在，跳过。"
fi
if [ ! -f settings.json ]; then
  cp settings.example.json settings.json
  echo "已生成 settings.json：把里面带说明文字的占位换成你自己的（至少 me、title、agent、redlines 要填）。"
else
  echo "settings.json 已存在，跳过。"
fi

say "4/4 后续步骤（需要你本人操作）"
cat <<'EOF'
1) 取 API 令牌（容器起来几秒后就有）：
     docker exec ai-wechat-auto cat /data/wxauto/api_token
   把它填进 awx.env 的 AWX_TOKEN= 后面。令牌等于微信控制权，别外传。

2) 扫码登录微信（在另一块屏幕上打开，手机才扫得到）：
     http://127.0.0.1:18500/qr?token=<刚才的令牌>
   也可以打开控制面板 http://127.0.0.1:18500/panel 用远程桌面看微信界面。
   同一个微信同时只能登一处 Linux 微信，换机器部署后旧机器会掉线。

3) 初始化值班数据库（settings.json 填好后）：
     python3 seed.py

4) 启动值班（二选一）：
   - 临时前台跑：python3 watch.py
   - systemd 常驻：把 systemd/awx-watch.service 里的 __INSTALL_DIR__ 换成本目录
     的绝对路径，复制到 /etc/systemd/system/ 后：
       systemctl daemon-reload && systemctl enable --now awx-watch

5) 让你的 AI 平台（如 Muse）加载本目录的 SKILL.md 作为值班规则；
   语音（Fish / Cloudflare 兜底）等进阶配置见 docs/ 目录。
EOF
echo
echo "部署脚本结束。容器状态："
docker compose ps
