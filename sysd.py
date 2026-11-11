# -*- coding: utf-8 -*-
"""systemd 保活：值班交给 systemd（死了 5 秒拉回、开机自启）。能装系统级就装系统级（要 root），不行再试用户级。
11-06 Muse 主力机实测：没有用户级总线（systemctl --user：No medium found），系统级可用；删 unit 模拟重建，约 63 秒恢复。
沙箱重建会丢 /etc/systemd/system 下的 unit：平台 hook 每 10 秒跑 watch.py hook 时调 heal()，装过（有 .systemd_on）而 unit 不在，
就照本目录重写、启用、拉起（失败 10 分钟才再试一次）。
代理变量只在平台 hook / 会话的环境里有，服务拿不到：heal() 顺手写进 .proxy.env（600），服务经 EnvironmentFile 读，下次重启跟上。平台每次跑 hook 都换一份凭据（旧的仍能用），值变了最多 10 分钟刷一次、不记日志。
只写日志，不往 stdout 输出（hook 有输出才叫醒 AI，不花 token）。"""
import os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "awx-watch.service"
SYS_DIR = "/etc/systemd/system"
MARK = os.path.join(HERE, ".systemd_on")      # 内容是 system / user；没装过的机器 heal 什么都不做
TRY = os.path.join(HERE, ".systemd_try")      # 重装失败的时间（mtime），RETRY 秒内不再试，免得每 10 秒刷一行日志
ENVF = os.path.join(HERE, ".proxy.env")
PROXY = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")
RETRY = 600
ENV_AGE = 600
LOGF = os.path.join(HERE, "watch.log")

def log(m):
    """直接追加进 watch.log：heal 在平台 hook 进程里跑，stderr 进的是 hook 的日志，不在 watch.log（11-06 真机测重建时 grep 不到）。"""
    try:
        with open(LOGF, "a") as f: f.write(f"{time.strftime('%H:%M:%S')} {m}\n")
    except OSError: print(time.strftime("%H:%M:%S"), m, file=sys.stderr, flush=True)

def system_ok():
    return os.geteuid() == 0 and os.path.isdir("/run/systemd/system")

def path(mode):
    return os.path.join(SYS_DIR if mode == "system" else os.path.expanduser("~/.config/systemd/user"), NAME)

def unit(mode):
    lg = os.path.join(HERE, "watch.log")
    return ("[Unit]\nDescription=ai-wechat-auto watch loop (muse-skill/watch.py)\n"
            "After=network-online.target\nWants=network-online.target\nStartLimitIntervalSec=0\n\n"
            f"[Service]\nType=simple\nWorkingDirectory={HERE}\nEnvironment=AWX_SYSTEMD=1\nEnvironmentFile=-{ENVF}\n"
            f"ExecStart={sys.executable} {os.path.join(HERE, 'watch.py')}\n"
            f"Restart=always\nRestartSec=5\nTimeoutStopSec=20\nStandardOutput=append:{lg}\nStandardError=append:{lg}\n\n"
            f"[Install]\nWantedBy={'multi-user.target' if mode == 'system' else 'default.target'}\n")

def ctl(mode, *a):
    """跑 systemctl，返回出错原因（成功返回空串）。"""
    cmd = ["systemctl"] + (["--user"] if mode == "user" else []) + list(a)
    try: r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e: return f"{' '.join(cmd)}：{e}"
    return f"{' '.join(cmd)}：{(r.stderr or r.stdout).strip()[:200]}" if r.returncode else ""

def write(mode):
    """写 unit、reload、enable --now。返回出错原因（成功空串）。脱管那份值班在跑也没事：服务那份会等它让位（watch.loop）。"""
    p = path(mode)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f: f.write(unit(mode))
        os.chmod(p, 0o644)                    # hook 环境 umask 007，不改是 660
    except OSError as e: return f"写 {p}：{e}"
    return ctl(mode, "daemon-reload") or ctl(mode, "enable", "--now", NAME)

def refresh_env():
    """把当前环境里的代理变量写进 .proxy.env（600，只在变了时写；值不进日志）。"""
    body = "".join(f"{v}={os.environ[v]}\n" for v in PROXY if os.environ.get(v))
    try: old = open(ENVF).read()
    except FileNotFoundError: old = ""
    except OSError as e: log(f"[sysd] 读 .proxy.env 没成：{e}"); return
    if old == body: return
    names = lambda t: sorted(l.split("=", 1)[0] for l in t.splitlines() if "=" in l)
    same = names(old) == names(body)
    # Muse 平台每次跑 hook 都发一份新代理凭据（11-06 备用机实测约 10 秒一换），旧的照样能用：
    # 只是值变了就最多 ENV_AGE 秒刷一次、不记日志；变量增减才马上写并记一行（只记名字，值不进日志）
    try:
        if same and time.time() - os.path.getmtime(ENVF) < ENV_AGE: return
    except OSError: pass
    try:
        tmp = ENVF + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f: f.write(body)
        os.replace(tmp, ENVF)
        if not same: log(f"[sysd] 代理变量有增减（{','.join(names(body)) or '无'}），已更新 .proxy.env（服务下次重启生效）")
    except OSError as e: log(f"[sysd] 写 .proxy.env 没成：{e}")

def mode():
    """装过就返回 system / user，没装过返回空串。"""
    try: return open(MARK).read().strip() or "system"
    except FileNotFoundError: return ""

def install():
    """setup / `watch.py install`：返回 (成没成, 给人看的一句话)。"""
    refresh_env()                              # 先写代理，服务第一次起来就连得上
    why = []
    for m in (["system"] if system_ok() else []) + ["user"]:
        err = write(m)
        if err: why.append(err); continue
        with open(MARK, "w") as f: f.write(m)
        if m == "user":
            try: subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as e: log(f"[sysd] enable-linger 没做成（不影响已在跑的服务）：{e}")
        return True, (f"已装好{'系统级' if m == 'system' else '用户级'} systemd 服务 {NAME}（死了 5 秒自动拉起、开机自启；"
                      "沙箱重建丢了，平台 hook 跑 watch.py hook 时会自动重装）。")
    return False, f"systemd 服务没装上（{'；'.join(why)}）。"

def heal():
    """平台 hook 每 10 秒调：装过的机器刷新代理文件，unit 不在（沙箱重建）就重装拉起。"""
    m = mode()
    if not m: return
    refresh_env()
    if os.path.exists(path(m)): return
    try:
        if time.time() - os.path.getmtime(TRY) < RETRY: return
    except OSError: pass
    err = write(m)
    if not err:
        log(f"[sysd] {NAME} 不在了（沙箱重建？），已照本目录重装并拉起")
        try: os.remove(TRY)
        except FileNotFoundError: pass
        return
    open(TRY, "w").close()
    log(f"[sysd] {NAME} 不在了，重装没成（{err}），{RETRY} 秒后再试；这段时间保活靠 hook 的 up")

def start():
    """装过且 unit 在：让 systemd 拉起（RestartSec 的空档里 hook 先到也不脱管另起一份）。返回是否成了。"""
    m = mode()
    if not m or not os.path.exists(path(m)): return False
    err = ctl(m, "start", NAME)
    if err: log(f"[sysd] 服务拉不起：{err}，改为脱管拉起")
    return not err
