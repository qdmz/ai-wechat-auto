#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""值班循环：一直挂在 wxtest 上领任务，手里永远只拿一条。
  python3 watch.py          常驻（已有一份在跑就直接退出，可反复调用来「保活」）
  python3 watch.py setup    一键上岗：停旧值班（run_loop.sh/watch_loop.sh、crontab 里的开班）→ 装 systemd 服务（系统级优先，再试用户级）→ 装不上才装 crontab 保活 → 拉起 → 自检，最后打印平台 hook 要填的那一行
  python3 watch.py cron     只装 crontab 保活（每分钟 + 开机各跑一次 keepalive.py），不靠平台 hook；重复跑只留一份，值班在跑时丢了会自己补回
  python3 watch.py hook     平台 hook 每 10 秒跑这一句：systemd 服务重建丢了就重装 + 保活（up）+ 新任务只打印一次，其余时候什么都不输出；在哪个目录调用都行
  python3 watch.py boot     开机钩子（init.sh）每次沙箱启动跑一次：unit 丢了装回、拉起值班（hook 每次顺手补装开机钩子，不用手动）
  python3 watch.py install  只装 systemd 服务（系统级优先，再试用户级；挂了 5 秒自动拉起、开机自启），装不上会说明
  python3 watch.py up       保活：没在跑就让 systemd 拉，没装服务就脱离当前会话在后台拉起一份（setsid），在跑就什么都不做
  python3 watch.py next     打印最早一条待处理任务（没有就什么都不输出）
  python3 watch.py status   看本机名字、代码指纹和 wxtest 上在线的 agent
  python3 watch.py update   马上从 wxtest 拉它自带的那版 skill 覆盖本地代码（平时落后了 wxtest 会自己派，不用手动）
  python3 watch.py done ID  处理完删掉（带 --task 的任务 act.py 交回后会自动删，alert/摘要等没有 --task 的要手动 done）
  python3 watch.py release ID 原因   处理不了（读不到内容、被平台拦下）：交还给 wxtest，别台马上接；都处理不了 wxtest 告诉本人
领到任务写进 inbox/<ID>.json，设置了 wake 就马上跑它叫醒 Muse，然后不再领下一条，直到 inbox 空了；等待期间续着 tick_at，后台心跳不会退出。
wxtest 规定单条领走 5 分钟没交就收回，所以处理要快；处理不了也别拖，交 {} 或 done 掉，好让别的 agent 接。"""
import fcntl, glob, json, os, re, subprocess, sys, time
import boothook, db, keepalive, psync, sysd

HERE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(HERE, "inbox")
GIVEBACK = 180      # 叫醒后这么久处理员还没跑 act（没起来、被平台拦下、只汇报没交）：交还给 wxtest 让别台马上接，不干等 5 分钟收回
                    # （hook 同一条只叫醒一次：重复叫醒会把平台唤醒放大、处理员排队几小时；第二次叫醒实测也没用）
STALE = 330          # 交还没交上（连不上 wxtest）且超过 wxtest 的单条上限（300 秒）：已被收回改派，本地这份作废，丢掉接着领
VIDEO_MAX, VIDEO_GRACE = 930, 180   # 处理员在等看懂视频（awx.py video 每轮刷新 .act，wxtest 那边同样放宽到 900 秒）：过期时限放宽

def log(m):
    print(time.strftime("%H:%M:%S"), m, file=sys.stderr, flush=True)

def pending():
    return sorted(glob.glob(os.path.join(INBOX, "*.json")), key=os.path.getmtime)

def save(out):
    m = re.search(r"--task ([\w-]+)", out)
    tid = m.group(1) if m else f"n{int(time.time() * 1000)}"
    tmp = os.path.join(INBOX, f".{tid}.tmp")
    open(tmp, "w", encoding="utf-8").write(out)
    path = os.path.join(INBOX, f"{tid}.json")
    os.replace(tmp, path)
    log(f"[watch] 领到 {tid}")
    wake(tid, path)

def wake(tid, path):
    """设置 wake 填了就马上叫醒 Muse；失败只记日志，hook 定时看 next 兜底。"""
    cmd = db.config(db.conn())["wake"]
    if not cmd:
        return
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10,
                           env=dict(os.environ, AWX_TASK_ID=tid, AWX_TASK_FILE=path))
        if r.returncode: log(f"[wake] 退出码 {r.returncode}：{(r.stderr or r.stdout).strip()[:200]}")
        else: mark(tid)                        # 叫醒过了，hook 不再重复叫
    except Exception as e:
        log(f"[wake] {e}")

def shown(tid):   # inbox/.<ID>.shown：已经叫醒过几次（内容）、最后一次什么时候（mtime）
    return os.path.join(INBOX, f".{tid}.shown")

def mark(tid):
    p = shown(tid)
    try: n = int(open(p).read() or 0)
    except (OSError, ValueError): n = 0
    open(p, "w").write(str(n + 1))

def started(tid):  # inbox/.<ID>.act：act.py 已经开始处理这条（发过稍等、在查资料），值班不交还
    return os.path.join(INBOX, f".{tid}.act")

def begin(tid):
    try: open(started(tid), "w").close()
    except OSError as e: log(f"[watch] 记不下 {tid} 已开始：{e}")

def busy(tid):   # .act 最近刷新过（awx.py video 还在等视频，或刚等完）
    try: return time.time() - os.path.getmtime(started(tid)) < VIDEO_GRACE
    except OSError: return False

def release(tid, why, retry=False):
    """交还 wxtest 并从收件箱删掉。返回 wxtest 的回复（None = 没交上）。"""
    tid = os.path.basename(tid).removesuffix(".json")
    try: import awx
    except SystemExit: return None
    r = awx.task_release(tid, why, retry)
    if r is not None: done(tid)                # 交上了或已不是本机的（409）都删；连不上留着，STALE 兜底
    return r

def gone(tid):    # inbox/.<ID>.gone：wxtest 说这条已不归本机（本人接管、别台接手），内容是原因；act.py 看到就打印原因收手
    return os.path.join(INBOX, f".{tid}.gone")

def stop(tid, why):
    """心跳得知这条已不归本机：从收件箱拿掉（hook 不再叫醒、值班接着领），原因留给还在跑的处理员看。"""
    tid = os.path.basename(str(tid or "")).removesuffix(".json")
    if not tid or not os.path.exists(os.path.join(INBOX, f"{tid}.json")): return
    try: open(gone(tid), "w", encoding="utf-8").write(why or "已不归本机")
    except OSError as e: log(f"[watch] 记不下 {tid} 作废原因：{e}")
    for p in (os.path.join(INBOX, f"{tid}.json"), shown(tid)):
        try: os.remove(p)
        except OSError: pass
    log(f"[watch] {tid} 已不归本机，叫停：{why}")

UNREAD = os.path.join(HERE, ".skill_unread.json")   # 同 awx.UNREAD：升级改了说明、AI 还没读（tick 写）

def _kind(path):
    """收件箱里这条是什么任务（tick 打印的是 {"tasks": [...]}）。"""
    try: d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError): return None
    ts = d.get("tasks") if isinstance(d, dict) else None
    return (ts[0].get("kind") if ts and isinstance(ts[0], dict) else None) or (d.get("kind") if isinstance(d, dict) else None)

def _via():   # 改动原文附在哪条任务里给过处理员
    try: return json.load(open(UNREAD, encoding="utf-8")).get("via")
    except (OSError, ValueError): return None

def read_ok(why):
    try: os.remove(UNREAD); log(f"[watch] 新说明已读（{why}）")
    except FileNotFoundError: pass
    except OSError as e: log(f"[watch] 删 {UNREAD} 失败：{e}")

def done(tid, read=True):
    """read=False：值班超时丢掉的，不算处理员读过。"""
    tid = os.path.basename(tid).removesuffix(".json")
    if not read: pass
    elif _kind(os.path.join(INBOX, f"{tid}.json")) == "skill升级": read_ok("skill升级 已处理")
    elif _via() == tid: read_ok(f"附了改动原文的 {tid} 已处理")
    for p in (os.path.join(INBOX, f"{tid}.json"), shown(tid), started(tid), gone(tid)):
        try: os.remove(p)
        except OSError: pass

LOCK = os.path.join(HERE, "watch.lock")
YIELD = os.path.join(HERE, "watch.yield")   # systemd 那份在等锁：里面是它的进程号，脱管那份做完这轮就让位
SYSTEMD = bool(os.environ.get("AWX_SYSTEMD"))   # 本进程是 awx-watch 服务拉起的（unit 里设的，别用 INVOCATION_ID：平台 hook 引擎自己也在 systemd 下）
LOGF, LOG_MAX = os.path.join(HERE, "watch.log"), 1 << 20   # 满 1MB 轮转，留一份 watch.log.1

def trim_log(path=None, limit=LOG_MAX):
    """watch.log 超过上限：整份挪成 .1（旧的 .1 覆盖），原文件清零。写的人（systemd append、up 的 "a"）都是追加模式，
    清零后接着从头写，不用重开文件。返回是否轮转了。"""
    path = path or LOGF
    try:
        if os.path.getsize(path) <= limit: return False
        with open(path, "rb") as src, open(path + ".1", "wb") as dst:
            while True:
                b = src.read(1 << 16)
                if not b: break
                dst.write(b)
        os.truncate(path, 0)
        log(f"[watch] watch.log 超过 {limit >> 20}MB，旧内容挪到 watch.log.1")
        return True
    except FileNotFoundError: return False
    except OSError as e: log(f"[watch] watch.log 轮转没成：{e}"); return False

def yield_asked():
    """systemd 那份在等锁（YIELD 里的进程号还活着）。留下的旧文件不算，免得脱管那份一起来就退。"""
    try: pid = int(open(YIELD).read().strip() or 0)
    except (OSError, ValueError): return False
    return pid > 0 and os.path.exists(f"/proc/{pid}")

def running():
    """已有一份值班在跑（它拿着 watch.lock）。"""
    with open(LOCK, "a") as f:
        try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError: return True
        fcntl.flock(f, fcntl.LOCK_UN); return False

def up():
    """hook 保活：装了 systemd 服务就让 systemd 拉；否则不挂在调用它的会话上拉起（会话结束、超时被平台清进程树时值班不跟着死）。"""
    if running() or sysd.start(): return
    log_f = open(LOGF, "a")
    subprocess.Popen([sys.executable, os.path.abspath(__file__)], cwd=HERE, stdin=subprocess.DEVNULL,
                     stdout=log_f, stderr=log_f, start_new_session=True)
    log_f.write(f"{time.strftime('%H:%M:%S')} [watch] up：没在跑，已在后台拉起\n"); log_f.close()   # 写进 watch.log：hook 里跑时 stderr 不在这

OLD = ("run_loop.sh", "watch_loop.sh")   # 旧值班脚本（cron 开班 / 会话里循环），setup 时停掉

def stop_old():
    """停掉旧式值班进程，删掉 crontab 里开班的行。返回做了什么（给 setup 打印）。"""
    did = []
    for d in glob.glob("/proc/[0-9]*"):
        try: cmd = open(f"{d}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except OSError: continue
        if any(o in cmd for o in OLD) and int(d[6:]) != os.getpid():
            try: os.kill(int(d[6:]), 15); did.append(f"停了旧进程 {d[6:]}：{cmd.strip()[:80]}")
            except OSError as e: did.append(f"停不了旧进程 {d[6:]}：{e}")
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            keep = [l for l in r.stdout.splitlines() if not any(o in l for o in OLD + ("tick.py",))]
            if len(keep) < len(r.stdout.splitlines()):
                subprocess.run(["crontab", "-"], input="\n".join(keep) + "\n", text=True, timeout=10, check=True)
                did.append(f"删了 crontab 里 {len(r.stdout.splitlines()) - len(keep)} 行开班")
    except (OSError, subprocess.SubprocessError) as e:
        log(f"[setup] 查 crontab 没做成：{e}")
    return did

def status_line():
    try:
        import awx
        st = awx.api("/status", timeout=15)
        return f"本机 {awx.AGENT or '（没填名字，不会领任务）'}，代码指纹 {awx.BUILD}；wxtest 登录 {st.get('logged_in')}，在线 agent：" + "，".join(f"{a.get('agent')}({a.get('hb_ago')}秒)" for a in st.get("agents") or [])
    except BaseException as e:            # awx 缺令牌是 SystemExit，也只报不退
        return f"连不上 wxtest：{e}"

def setup():
    for d in stop_old(): print(d)
    systemd, msg = sysd.install(); print(msg)
    if (b := boothook.ensure()): print(b)
    crontab = "有 systemd 托管，不装 crontab。" if systemd else keepalive.install(); print(crontab)
    up()
    for _ in range(10):
        if running(): break
        time.sleep(1)
    ok = running()
    print(("值班已在跑。" if ok else "值班没起来，看 watch.log。") + status_line())
    print("画像同步：" + ("已请 wxtest 马上安排一次（第一次是全量）" if psync.request(db.conn(), log) else "没请成，wxtest 按 sync_min 会自己安排"))
    print(f"还差平台上一步（代码做不了）：建一个每 10 秒的定时 hook，执行\n  {sys.executable} {os.path.abspath(__file__)} hook\n"
          "没有输出就什么都不做；有输出就是一条任务（JSON），叫醒处理员马上处理。"
          + ("" if systemd or crontab.startswith("crontab 保活已装") else "（本机 systemd 和 cron 都用不上，保活全靠这个 hook。）")
          + ("\n平台上另建的值班保活（别的 hook、systemd 服务、watchdog 脚本）请删掉，hook 只留上面这一个。" if systemd else "")
          + "\n平台自带的定时开班（例如每 N 分钟跑旧值班脚本）请删掉，不要再用"
          + ("。" if systemd else "；上面那两行 crontab 保活是本程序装的，留着。"))
    return 0 if ok else 1

def loop():
    lock = open(LOCK, "w")
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:                             # 已有一份在跑
        if not SYSTEMD: return
        with open(YIELD, "w") as f: f.write(str(os.getpid()))
        log("[watch] systemd 接管：等脱管那份做完手上这轮再让位")
        fcntl.flock(lock, fcntl.LOCK_EX)
    if SYSTEMD:
        try: os.remove(YIELD)
        except FileNotFoundError: pass
    os.makedirs(INBOX, exist_ok=True)
    c = db.conn()
    log("[watch] 开始值班")
    try: import awx                             # 没令牌时 awx import 就退出：那就不查升级，值班照跑
    except SystemExit: awx = None; log("[watch] awx 起不来（没令牌？），不查代码升级")
    build, trimmed = awx and awx.BUILD, 0
    while True:
        if time.time() - trimmed > 60: trimmed = time.time(); trim_log()
        if not SYSTEMD and yield_asked():
            log("[watch] systemd 那份在等，让位退出"); return
        keepalive.heal()                        # 装过 crontab 保活的：重建后守护没起、行丢了就补回
        if awx and not pending() and awx._build() != build:   # skill 升级了：手里没活马上退（每轮都查，旧代码不再发请求），hook 10 秒内用新代码拉起
            log(f"[watch] skill 代码换了（{build} → {awx._build()}），退出让 hook 用新代码重拉")
            if hasattr(awx, "bye"): awx.bye()             # 报到：wxtest 面板马上清掉这个旧指纹
            return
        try:
            step(c)
        except Exception as e:                  # 什么错都不许把值班循环弄死
            log(f"[watch] {e}"); time.sleep(5)

_sync_try = {"at": 0, "sig": None, "stuck": 0}
URGENT_EVERY, STUCK_WAIT = 10, 600
def maybe_urgent(c):
    """手里没任务时：本机画像/规则/设置有没推的改动就请 wxtest 马上给本机下放同步（即时同步），最多 URGENT_EVERY 秒请一次。
    别台改的不用请：wxtest 收下后会给其余在线的全部下放。防死循环：同一批改动请过、也同步过一轮还在（推不上去），
    退到 STUCK_WAIT 秒才再请，并记日志让人查。"""
    now = time.time()
    if now - _sync_try["at"] < (STUCK_WAIT if _sync_try["stuck"] else URGENT_EVERY) or db.config(c)["sync_min"] <= 0: return
    if not psync.urgent(c): _sync_try.update(sig=None, stuck=0); return
    sig = psync.signature(c)
    synced_since = int(db.meta(c, "psync_at") or 0) > _sync_try["at"] > 0
    if sig == _sync_try["sig"] and synced_since:
        if not _sync_try["stuck"]: log(f"[watch] 同步过一轮这批改动还没推上去（{sig[:8]}），{STUCK_WAIT} 秒后再请；查 watch.log 里 [psync] 那行")
        _sync_try["stuck"] = 1
    else: _sync_try["stuck"] = 0
    _sync_try.update(at=now, sig=sig)
    psync.request(c, log)

_noname = [0]
def step(c):
    if not db.meta(c, "agent"):                 # 没名字不领任务；10 分钟跑一次 tick，让 wxtest 发文件传输助手提醒本人
        if time.time() - _noname[0] > 600:
            _noname[0] = time.time()
            log("[watch] settings.json 里没填 agent（这台的名字），不领任务：问用户起一个（几台各不相同），写进去再跑 seed.py")
            try: subprocess.run([sys.executable, os.path.join(HERE, "tick.py")], cwd=HERE, capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as e: log(f"[watch] 提醒没名字没成：{e}")
        time.sleep(30); return
    p = pending()
    if p:
        tid, ago = os.path.basename(p[0])[:-5], time.time() - os.path.getmtime(p[0])
        if ago > GIVEBACK and not os.path.exists(started(tid)) and "--task" in open(p[0], encoding="utf-8").read():
            r = release(tid, f"处理员 {GIVEBACK} 秒没动它", retry=True)
            if r is not None:
                log(f"[watch] {tid} 叫醒 {GIVEBACK} 秒处理员还没跑 act，已交还 wxtest 让别台接（{r.get('status') or r.get('error')}）；请查处理员为什么没处理")
                return
        if ago > STALE and not (busy(tid) and ago < VIDEO_MAX):
            log(f"[watch] {os.path.basename(p[0])} 超过 {STALE} 秒没处理完（wxtest 已收回改派），丢弃；请查处理员为什么没处理")
            done(p[0], read=False); return
        db.meta_set(c, "tick_at", str(int(time.time())))   # 手里有活，心跳别退
        time.sleep(2); return
    maybe_urgent(c)
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "tick.py")], cwd=HERE,
                           capture_output=True, text=True, timeout=400)   # 别杀太早：awx 失败会重试多轮，半路杀掉领到的任务就白占 5 分钟
    except subprocess.TimeoutExpired:
        log("[watch] tick.py 400 秒没返回，重来"); return
    if r.stderr.strip(): sys.stderr.write(r.stderr); sys.stderr.flush()
    if r.stdout.strip(): save(r.stdout.strip())
    if r.returncode: log(f"[watch] tick.py 退出码 {r.returncode}"); time.sleep(5)

def main_next(hook=False):
    p = pending()
    if p and hook:                             # hook：同一条只给一次（GIVEBACK 秒没人动值班自己交还）
        tid = os.path.basename(p[0])[:-5]
        if os.path.exists(shown(tid)): return
        mark(tid)
    if p:
        tid = os.path.basename(p[0])[:-5]
        t = {"id": tid, **json.load(open(p[0], encoding="utf-8"))}
        if not any(_kind(x) == "skill升级" for x in p):   # skill升级 被丢了还没读：改动原文附在这条里
            try:
                u = json.load(open(UNREAD, encoding="utf-8"))
                t["skill升级"] = {k: v for k, v in u.items() if k not in ("kind", "via")}
                if u.get("via") != tid:        # 这条处理完（done）才算读过
                    with open(UNREAD + ".tmp", "w", encoding="utf-8") as f: json.dump(dict(u, via=tid), f, ensure_ascii=False)
                    os.replace(UNREAD + ".tmp", UNREAD)
            except FileNotFoundError: pass
            except (OSError, ValueError) as e: log(f"[watch] 读 {UNREAD} 失败：{e}")
        print(json.dumps(t, ensure_ascii=False))   # 只放事实：必须以 act 收尾等规矩在 SKILL.md（命令口吻进输出会被平台安全检查拦）

def main():
    a = sys.argv[1:]
    if not a:
        return loop()
    if a[0] == "next":
        main_next()
    elif a[0] == "done" and len(a) > 1:
        done(a[1])
    elif a[0] == "release" and len(a) > 1:
        r = release(a[1], " ".join(a[2:]) or "处理不了")
        print(json.dumps(r, ensure_ascii=False) if r is not None else "没交上（连不上 wxtest），5 分钟后 wxtest 自己收回改派")
    elif a[0] == "up":
        up()
    elif a[0] == "hook":
        sysd.heal(); boothook.ensure(); up(); main_next(hook=True)
    elif a[0] == "boot":                      # 开机钩子（init.sh）每次沙箱启动跑一次：unit 丢了装回、拉起值班
        log("[boot] 沙箱启动，开机钩子拉值班"); sysd.heal(); up()
    elif a[0] == "setup":
        sys.exit(setup())
    elif a[0] == "cron":
        print(keepalive.install())
    elif a[0] == "install":
        ok, msg = sysd.install(); print(msg); sys.exit(0 if ok else 1)
    elif a[0] == "status":
        print(status_line())
    elif a[0] == "update":
        import skillup
        r = skillup.run(); print(json.dumps(r, ensure_ascii=False)); sys.exit(0 if r.get("ok") else 1)
    else:
        sys.exit(__doc__)

if __name__ == "__main__":
    main()
