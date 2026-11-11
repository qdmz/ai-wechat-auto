# -*- coding: utf-8 -*-
"""crontab 保活（同飞书桥）：系统 cron 每分钟、开机各跑一次本脚本（python3 keepalive.py），值班没在跑就拉起，在跑什么都不做。
纯本机脚本，不靠平台 hook，不叫醒 AI，不花 token。用 .py 不用 .sh：skill 自动更新只收 .py/文档，.sh 会让老版本的更新整包被拒。
补平台 hook 停摆的空档（实测：停 hook 杀值班，11 秒被 cron 拉回）。
自愈：沙箱重建后 cron 守护不起、crontab（在 /var）会丢；值班一跑起来就 heal()，装过的（有 .cron_on）补回来。
只用本机已有的 cron（systemctl start cron 或直接跑 cron），不自动装软件。"""
import glob, os, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
MARK = os.path.join(HERE, ".cron_on")        # 装过：重建丢了要补回；没装过的机器 heal 什么都不做
TAG = "# awx-watch-cron"
HEAL_EVERY = 600

def log(m):
    print(time.strftime("%H:%M:%S"), m, file=sys.stderr, flush=True)

def _run(cmd, **k):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10, **k)

def daemon_alive():
    """cron 守护进程在不在跑（不在跑，crontab 写了也不会执行）。"""
    for d in glob.glob("/proc/[0-9]*"):
        try: name = open(f"{d}/comm").read().strip()
        except OSError: continue
        if name.startswith("cron"): return True                     # cron / crond（busybox 的也叫 crond）
    return False

def start_daemon():
    """cron 装着但没跑（Muse 开机进最小 target，cron.service enabled 也不起）：先 systemctl，不行直接跑。返回是否起来了。"""
    if daemon_alive(): return True
    for cmd in (["systemctl", "start", "cron"], ["systemctl", "start", "crond"], ["cron"], ["crond"]):
        if not shutil.which(cmd[0]): continue
        try: r = _run(cmd)
        except (OSError, subprocess.SubprocessError) as e: log(f"[keepalive] {' '.join(cmd)} 没成：{e}"); continue
        if r.returncode == 0:
            time.sleep(1)
            if daemon_alive(): log(f"[keepalive] cron 守护没在跑，已用 {' '.join(cmd)} 起来"); return True
        else: log(f"[keepalive] {' '.join(cmd)} 退出码 {r.returncode}：{(r.stderr or r.stdout).strip()[:120]}")
    return False

def lines():
    cmd = f"cd {HERE} && {sys.executable} {os.path.abspath(__file__)} >> {os.path.join(HERE, 'watch.log')} 2>&1 {TAG}"
    return [f"* * * * * {cmd}", f"@reboot {cmd}"]

def table():
    """当前 crontab 的行；没有 crontab（-l 退出码 1）当空表。没有 crontab 命令抛 OSError。"""
    r = _run(["crontab", "-l"])
    return r.stdout.splitlines() if r.returncode == 0 else []

def write(old):
    keep = [l for l in old if TAG not in l]
    _run(["crontab", "-"], input="\n".join(keep + lines()) + "\n", check=True)

def install():
    """setup / `watch.py cron`：写两行（重复装只留一份，别的定时任务不动）、起守护、记 .cron_on。返回给人看的一句话。"""
    if not shutil.which("crontab"):
        return "本机没有 cron（crontab 命令不在），保活只靠平台 hook；要更稳就先装 cron（Ubuntu：apt install cron）再跑 python3 watch.py cron。"
    try: write(table())
    except (OSError, subprocess.SubprocessError) as e:
        log(f"[keepalive] crontab 写不进：{e}")
        return f"crontab 保活没装上（{str(e)[:120]}），保活只靠平台 hook。"
    open(MARK, "w").close()
    if not start_daemon():
        log("[keepalive] crontab 已写，但 cron 守护起不来")
        return "crontab 保活已写，但 cron 守护起不来，写了不会执行；保活只靠平台 hook。"
    return "crontab 保活已装（每分钟查一次、开机自动拉起，不靠平台 hook）。"

_last = [0.0]
def heal():
    """值班循环里每 HEAL_EVERY 秒调一次：装过的机器，守护没跑就起、两行丢了就补。只写日志，不输出到 stdout（hook 有输出才叫醒 AI）。"""
    if time.time() - _last[0] < HEAL_EVERY or not shutil.which("crontab"): return
    _last[0] = time.time()
    try:
        old = table()
        if not os.path.exists(MARK):
            if not any(TAG in l for l in old): return                   # 没装过的机器不动
            open(MARK, "w").close()                                     # 旧版装的（没留标记）：补上标记，以后重建也能补
        if lines() != [l for l in old if TAG in l]:
            write(old); log("[keepalive] crontab 保活行不在或过时（沙箱重建？），已补回")
    except (OSError, subprocess.SubprocessError) as e:
        log(f"[keepalive] 补 crontab 没成：{e}")
    if not start_daemon(): log("[keepalive] cron 守护起不来，这段时间保活只靠平台 hook")

if __name__ == "__main__":       # crontab 调的就是这里
    import watch
    watch.up()
