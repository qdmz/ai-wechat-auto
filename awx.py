#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共：ai-wechat-auto 接口、防重发的发送、拼对话文本。
wxtest地址和令牌：环境变量 AWX_BASE、AWX_TOKEN 优先，没有就读本目录 awx.env（600 权限、不进 git），
这样 agent 重启或忘了上下文也不断线。"""
import gzip, hashlib, http.client, json, os, re, shutil, socket, sqlite3, subprocess, sys, time, uuid, zlib, urllib.error, urllib.parse, urllib.request
from urllib.parse import quote

def _creds():
    f = os.path.join(os.path.dirname(os.path.abspath(__file__)), "awx.env")
    kv = {}
    if os.path.exists(f):
        for line in open(f, encoding="utf-8"):
            k, _, v = line.strip().removeprefix("export ").partition("=")
            if k and v: kv[k] = v.strip().strip("'\"")
    b, t = os.environ.get("AWX_BASE") or kv.get("AWX_BASE"), os.environ.get("AWX_TOKEN") or kv.get("AWX_TOKEN")
    if not (b and t):
        raise SystemExit("没有wxtest地址/令牌：问用户要，写进 awx.env（AWX_BASE=… 和 AWX_TOKEN=… 两行，chmod 600）")
    return b.rstrip("/"), t

BASE, TOKEN = _creds()
def _agent():
    """本 agent 的名字：settings 里填的 agent，没填用主机名。wxtest 按它区分任务在谁手里。"""
    try:
        r = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.environ.get("AWX_DB", "store.db"))).execute("SELECT v FROM meta WHERE k='agent'").fetchone()
    except sqlite3.Error:
        r = None
    return r[0] if r and r[0] else ""   # 名字只由本人在 settings.json 起；没填就空着，不领任务（wxtest 会提醒本人）

def _meta_once(k, make):
    """本机固定的值（实例号）：第一次生成存进 meta，之后一直用它。"""
    try:
        c = sqlite3.connect(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.environ.get("AWX_DB", "store.db")))
        c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        if r and r[0]: return r[0]
        v = make(); c.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v)); c.commit(); return v
    except sqlite3.Error as e:
        print(f"[awx] 读写 meta.{k} 失败：{e}", file=sys.stderr, flush=True); return ""

def _build(here=None):
    """skill 指纹：顶层 .py、schema.sql、SKILL.md、settings.example.json 和 docs/*.md 的内容摘要（和 app/skillpkg 的 full 一样）。
    同一个提交 = 同一个指纹，wxtest 拿它比是否落后，只改文档也算；styles/、rules/ 不算（各机会自己加）。"""
    here, h = here or os.path.dirname(os.path.abspath(__file__)), hashlib.sha1()
    top = [x for x in os.listdir(here) if x.endswith(".py") or x in ("schema.sql", "SKILL.md", "settings.example.json")]
    try: docs = ["docs/" + x for x in os.listdir(os.path.join(here, "docs")) if x.endswith(".md")]
    except OSError: docs = []
    for f in sorted(x for x in top + docs if os.path.isfile(os.path.join(here, x))):
        try: h.update(f.encode() + b"\0" + open(os.path.join(here, f), "rb").read())
        except OSError as e: print(f"[awx] 算代码指纹读 {f} 失败：{e}", file=sys.stderr, flush=True)
    return h.hexdigest()[:8]

AGENT, BUILD = _agent(), _build()
IID = _meta_once("agent_iid", lambda: uuid.uuid4().hex[:8])
# 自带 User-Agent：wxtest 走 Cloudflare 隧道时，CF 会按 Python-urllib 默认 UA 直接回 403（error code 1010）
H = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json", "X-Agent": quote(AGENT), "User-Agent": "Mozilla/5.0 awx",
     "X-Agent-Id": IID, "X-Build": BUILD, "X-Host": quote(socket.gethostname())}

UNREAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".skill_unread.json")   # 升级改了说明、AI 还没读：skill升级 任务原样存这（tick 写，watch 读过就删）

_stale = {"t": 0, "v": False}
def stale():
    """这个进程跑的代码已经不是磁盘上的了（升级换了文件、本进程还没退）：报 X-Stale，wxtest 当收尾中的旧进程，不当没退。5 秒算一次。"""
    if time.time() - _stale["t"] > 5:
        _stale.update(t=time.time(), v=_build() != BUILD)
    return _stale["v"]

def bye():
    """旧进程退出前报到（X-Bye）：wxtest 马上不再把这个指纹算在报，面板清掉。报不上不要紧（3 分钟没再出现也会清）。"""
    try: api("/status", timeout=10, tries=2, extra={"X-Bye": "1"}); return True
    except Exception as e:
        print(f"[awx] 退出报到没报上：{e}", file=sys.stderr, flush=True); return False

def unread():
    """没读的说明版本（报给 wxtest 的 X-Unread；空 = 都读过）。"""
    try: return str(json.load(open(UNREAD, encoding="utf-8")).get("build") or "?")[:16]
    except FileNotFoundError: return ""
    except (OSError, ValueError) as e: print(f"[awx] 读 {UNREAD} 失败：{e}", file=sys.stderr, flush=True); return "?"

PIECE = 1400   # 回包压缩后超过这么大就让 wxtest 切块（Muse 沙箱出口传途中随时断，11-03 实测 1.5KB 八九成能过、7KB 以上全断）
PIECE_WAIT = 6   # 一块 1.4KB 正常半秒内到；但 24 路同时新建连接时 Muse 出口建连可能要好几秒，2 秒太紧（11-03 0ac3045 实测块全卡在 3 秒超时），回到 6 秒
PIECE_PAR = 4    # 取块几路并行：Muse 出口上同时开 24 路每块都要约 6 秒，单路复用连接每块 0.2 秒（11-03 实测）

def _fetch(path, timeout, data, hdr, curl):
    """发一次请求，返回 (状态码, 响应头(小写键), 原始字节)；回包没收全就抛错。curl 走同一出口，有的沙箱只认它。"""
    if curl:
        import tempfile
        with tempfile.NamedTemporaryFile() as hf:
            args = sum((["-H", f"{k}: {v}"] for k, v in hdr.items()), []) + (["--data-binary", "@-"] if data is not None else [])
            r = subprocess.run(["curl", "-sS", "-m", str(timeout), "-D", hf.name, *args, BASE + path],
                               input=data, capture_output=True, timeout=timeout + 5)
            if r.returncode: raise OSError(r.stderr.decode(errors="replace").strip())
            head = open(hf.name, encoding="latin-1").read().strip().split("\r\n\r\n")[-1].splitlines()
        hd = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in head[1:])}
        return int(head[0].split()[1]), hd, r.stdout
    req = urllib.request.Request(BASE + path, data=data, headers=hdr, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()

import threading
_tl = threading.local()

def _keep(path, hdr):
    """每个取块线程留一条长连接（不用每块都重新握手，Muse 出口一次握手要一秒左右）；断了就关掉，下次重连。走环境变量里的代理（同 urllib）。"""
    u = urllib.parse.urlsplit(BASE)
    target = u.path.rstrip("/") + path
    c = getattr(_tl, "c", None)
    if c is None:
        px = urllib.request.getproxies().get(u.scheme)
        if px and not urllib.request.proxy_bypass(u.hostname):
            p = urllib.parse.urlsplit(px if "://" in px else "http://" + px)
            if u.scheme == "https":   # CONNECT 隧道，再在里面做 TLS
                c = http.client.HTTPSConnection(p.hostname, p.port or 80, timeout=PIECE_WAIT)
                auth = None
                if p.username:
                    import base64
                    auth = {"Proxy-Authorization": "Basic " + base64.b64encode(f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}".encode()).decode()}
                c.set_tunnel(u.hostname, u.port or 443, headers=auth)
            else:
                c = http.client.HTTPConnection(p.hostname, p.port or 80, timeout=PIECE_WAIT); c.absolute = True
        else:
            c = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.hostname, u.port, timeout=PIECE_WAIT)
        _tl.c = c
    try:
        c.request("GET", BASE + path if getattr(c, "absolute", False) else target, headers=hdr)
        r = c.getresponse()
        return r.status, r.read()
    except BaseException:
        c.close(); _tl.c = None; raise

def _piece(pid, i, hdr):
    """取切块回包的第 i 块：每块自己重试，长连接为主、单数次换 curl。块小，PIECE_WAIT 秒没动静就当断了；头两次断了立刻重取，之后才稍等。"""
    curl = shutil.which("curl")
    for t in range(8):
        try:
            p = f"/piece?id={pid}&i={i}"
            code, b = (_fetch(p, PIECE_WAIT + 1, None, hdr, True)[::2] if t % 2 and curl else _keep(p, hdr))
            if code == 404: raise OSError(f"块 {i} 已过期")   # 外层整条重来（POST 带同一编号，wxtest 回上次结果不重做）
            if code == 200: return b
        except (http.client.HTTPException, OSError, EOFError, subprocess.TimeoutExpired) as e:
            if t == 7 or "已过期" in str(e): raise OSError(f"块 {i} 没取到：{str(e)[:60]}")
        if t >= 2: time.sleep(0.3 * (t - 1))
    raise OSError(f"块 {i} 没取到")

_POOL = [None, 0]
def _pool():
    """取块线程池常驻：线程不换，各自的长连接下次调用接着用。"""
    if _POOL[1] != PIECE_PAR:
        from concurrent.futures import ThreadPoolExecutor
        _POOL[:] = [ThreadPoolExecutor(PIECE_PAR, thread_name_prefix="awx-piece"), PIECE_PAR]
    return _POOL[0]

def _decode(path, code, hd, raw, hdr):
    """拼块（有的话）、核对、解压、解析；wxtest 回的错误码照 urllib 抛 HTTPError（e.read() 读得到 JSON）。"""
    enc = hd.get("content-encoding", "")
    if hd.get("x-pieces"):
        n, pid = int(hd["x-pieces"]), hd["x-piece-id"]
        raw += b"".join(_pool().map(lambda i: _piece(pid, i, hdr), range(1, n)))
        if hashlib.sha256(raw).hexdigest()[:16] != hd.get("x-piece-sha"): raise OSError("拼起来的回包校验不对")
        code, enc = int(hd.get("x-code", code)), hd.get("x-piece-enc", "")
    if enc == "gzip": raw = gzip.decompress(raw)
    if code >= 400:
        import io
        raise urllib.error.HTTPError(BASE + path, code, raw.decode(errors="replace")[:200], hd, io.BytesIO(raw))
    return json.loads(raw)

def api(path, body=None, timeout=30, tries=6, waits=20, extra=None):
    """调 wxtest。要 gzip 回包，压缩后还大就让 wxtest 切块（X-Piece），首块随回、其余 PIECE_PAR 路并行取（每路一条长连接），断了只重取那块。
    任何网络错误（建隧道被掐、截断、断连、超时）都退避重试，有 curl 时单双次轮流用 urllib 和 curl；wxtest 回的 HTTP 错误码不重试。
    POST 和领任务（/next_task）每次调用带一个编号 X-Req-Id，重试用同一个（领到的回包被掐，重领拿回同一条，不会白占 5 分钟）：wxtest 做过的直接回上次结果、不会做两遍（发消息、交结论都能放心重交）；
    还在做回 {"pending": true}，等一下用同一编号再问（最多 waits 次）；wxtest 重载丢了半截回 409 lost，由调用方查明再定。"""
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    hdr = {**H, "Accept-Encoding": "gzip", "X-Piece": str(PIECE), "X-Unread": unread(), **({"X-Req-Id": uuid.uuid4().hex} if data or path.startswith("/next_task") else {}),
           **({"X-Stale": "1"} if stale() else {}), **(extra or {})}
    curl, i = shutil.which("curl"), 0
    while True:
        try:
            r = _decode(path, *_fetch(path, timeout, data, hdr, bool(i % 2 and curl)), hdr)
            if data and isinstance(r, dict) and r.get("pending") is True and waits > 0:
                waits -= 1; print(f"[awx] {path} wxtest 还在做，稍后用同一编号再问", file=sys.stderr, flush=True)
                time.sleep(3); continue
            return r
        except urllib.error.HTTPError as e:
            if e.code == 403 and i % 2 == 0 and curl and i < tries - 1:   # urllib 被出口直接挡掉，请求没到 wxtest
                i += 1; continue
            raise
        except (http.client.HTTPException, OSError, EOFError, zlib.error, subprocess.TimeoutExpired, ValueError) as e:   # ValueError：截断的 JSON；EOFError/zlib：截断的 gzip
            if i >= tries - 1: raise
            print(f"[awx] {path} 第 {i + 1} 次没收全，重试：{str(e)[:80]}", file=sys.stderr, flush=True)
            time.sleep(i); i += 1   # 头一次断了立刻重来

def task_check(tid):
    """这条任务还算不算本机的：{mine, status, agent}；连不上或 wxtest 太旧（没这个接口）回 None，调用方照旧往下做。"""
    try:
        r = api(f"/task?id={urllib.parse.quote(tid)}", timeout=15, tries=2)
        return r if isinstance(r, dict) and "mine" in r else None
    except Exception as e:
        print(f"[task] 查 {tid} 状态失败：{str(e)[:80]}", file=sys.stderr, flush=True)

def task_done(tid, body):
    """把任务结论交回 wxtest；租约已被收回（409）或网络错误都不要紧，wxtest 会再派。
    交上回 {"ok": true}；409 回 wxtest 的说明（含 status、agent：已被谁处理）；网络错误回 None。"""
    try:
        return _post(f"/task/{tid}", body, 20)
    except Exception as e:
        print(f"[task] {tid} {e}", file=sys.stderr, flush=True)

def task_release(tid, why, retry=False):
    """处理不了就交还给 wxtest，别台马上接（retry：值班发现没人动它，自动交还，只按次数算放弃）。
    交上回 {"ok": true, "status": "RELEASED"}；409 回现状；网络错误回 None（wxtest 5 分钟后自己收回）。"""
    try:
        return _post(f"/task/{tid}", {"release": why, **({"retry": True} if retry else {})}, 20)
    except Exception as e:
        print(f"[task] 交还 {tid} 失败：{e}", file=sys.stderr, flush=True)

def messages(chat, limit=30, before=0):
    """最近 limit 条；before 给消息 id 就取它之前的（往前翻；迁移来的老记录 id 是负数）。"""
    return api(f"/messages?chat={urllib.parse.quote(chat)}&limit={limit}&before={before}").get("messages", [])

def find_chat(name):
    """微信号原样返回；名字在会话列表（备注/昵称/群名）里找：完全一样的优先，否则包含这几个字的，唯一才算，多个或没有抛 LookupError 列出来。"""
    if name.startswith(("wxid_", "gh_")) or name.endswith("@chatroom") or name == "filehelper": return name
    ss = api("/sessions").get("sessions", [])
    hit = [s for s in ss if s.get("name") == name or s["username"] == name] or [s for s in ss if name in (s.get("name") or "")]
    if len(hit) == 1: return hit[0]["username"]
    if not hit and name.isascii() and " " not in name: return name   # 老式微信号（不带 wxid_）
    if not hit: raise LookupError(f"会话列表里没有「{name}」，换个叫法或直接给微信号")
    raise LookupError(f"「{name}」不止一个，写微信号：" + "；".join(f"{s.get('name')}={s['username']}" for s in hit[:10]))

def search(words, chat="", limit=20):
    """搜聊天记录：words 空格分开的关键词都要有；chat 空就搜所有会话。每条带前后两条上下文。"""
    return api(f"/search?q={urllib.parse.quote(words)}&chat={urllib.parse.quote(chat)}&limit={limit}", timeout=60)

def earlier(chat, recent, me_id, me, n=150, total=5000):
    """context 之前更早的聊天（本人以前怎么跟这个人聊、事情的来龙去脉），拼成文本；没有返回空。"""
    if not recent:
        return ""
    try:
        msgs = messages(chat, n, min(m["id"] for m in recent))
    except Exception:
        return ""
    return transcript(msgs, me_id, me, total=total) if msgs else ""

def moments(user, days=30, n=5):
    """这个人最近 days 天的朋友圈（有文字或链接的，新的在前），一行一条；没有返回空。"""
    try: fs = api(f"/moments?user={quote(user)}&limit={n}").get("feeds", [])
    except Exception: return ""
    cut, out = time.time() - days * 86400, []
    for f in fs:
        if f.get("time", 0) < cut or not (f.get("text") or f.get("title")): continue
        extra = (f"（分享：{f['title']}）" if f.get("title") else "") + (f"[{f['media']}张图/视频]" if f.get("media") else "")
        out.append(f"{time.strftime('%m-%d', time.localtime(f['time']))} {(f.get('text') or '')[:200]}{extra}")
    return "\n".join(out)

def all_labels():
    """本人微信里现有的标签名（有人的）；拿不到返回空。"""
    try: return [x["name"] for x in api("/labels").get("labels", [])]
    except Exception: return []

def ai_done(kind="label"):
    """AI 打过标签（kind=label）/写过备注（kind=remark）的人（username 集合）。现在又没有了 = 本人清掉过，AI 别再弄。拿不到返回 None。"""
    try: return {x["user"] for x in api("/labels?ai_since=1").get("ai", []) if x.get(kind) not in (None, "")}
    except Exception: return None

def set_labels(user, add=(), remove=(), ai=False, wait=0, remark=None):
    """打/移出标签、改备注（remark，"" = 删掉）（wxtest 在界面上操作，会同步到手机）。
    ai=True：只给还没标签/还没备注的人弄，并记进 AI 记录（日报核对）。"""
    it = {"user": user, "add": list(add), "remove": list(remove)}
    if remark is not None: it["remark"] = remark
    if ai: it.update(only_if_none=True, only_if_empty=True, by="ai")
    return call("/labels/set", {"items": [it]}, wait)

def call(path, body, wait=300):
    """慢操作（发送、语音转文字、朋友圈）走wxtest异步任务：提交立刻返回，再挂起等结果（每次最多 15 秒，做完立刻返回）。
    agent 和微信一般不在一台机器，外网代理约 30 秒就断，所以不同步干等。wait=0 只提交不等（通知用）。
    同一个 job id 断网重提交不会重做；查到 lost（做到一半wxtest重载了）、unknown 或超时就返回 None，由调用方查消息确认。"""
    body, end = dict(body, job=uuid.uuid4().hex), time.time() + wait
    call.last = None
    r = None
    for _ in range(4):   # 提交：网络抖动就用同一个 id 重提交
        try:
            r = _post(path, body, 20); break
        except Exception:
            time.sleep(3)
    if r is None or "state" not in r or not wait:
        return r
    return _job(body["job"], r, end)

def _job(jid, r, end):
    while r.get("state") == "running" and time.time() < end:
        try:
            r = api(f"/job?id={jid}&wait=15", timeout=25)
        except Exception:
            time.sleep(2)
    call.last = jid
    return r.get("result") if r.get("state") == "done" else None

def voice_text(msgs):
    """语音换成文字消息（正文前加 [语音]）：wxtest 派任务前已转好（voice_text），这里不再自己转；没转出的保持 [语音] 占位。"""
    for m in msgs:
        t = m.get("voice_text") if m.get("type") == 34 else None
        if t:
            m["type"], m["content"] = 1, "[语音] " + t

def _post(path, body, timeout):   # 409 等错误码也带 JSON（中止原因），照样读出来
    try:
        return api(path, body, timeout)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except ValueError:
            raise e

def sent(chat, text, me_id, n=10):
    return any(m.get("sender_id") == me_id and (m.get("content") or "").endswith(text) for m in messages(chat, n))   # wxtest可能在前面补了备注

def forward(chat, ids, to, merge=None, wait=300):
    """用微信自己的「转发」把 chat 里的这几条转到 to（username 或备注/群名）。merge 不给：一条直接转，多条合并成聊天记录卡片。
    返回 (ok, 状态)。拿不到结果不重转（wxtest 10 分钟内同一批也只转一次）。"""
    if not ids: return False, "没有要转的消息"
    r = call("/forward", {"chat": chat, "ids": list(ids), "to": to, "merge": len(ids) > 1 if merge is None else bool(merge), "allow_group": True}, wait)
    return (bool(r.get("ok")), r.get("status") or r.get("error")) if r else (False, "没拿到转发结果，没重转")

def send(chat, text, me_id, group=False, allow_draft=None, reply_to=None, wait=300):
    """发一次，绝不盲目重发：拿不到结果（断网、wxtest重载）就查消息确认发没发出。
    reply_to：回的是哪条消息；wxtest发现这条之后已有人回了（别的 agent 先回了，或本人亲自接话）就不发，返回 SKIP。
    wait=0：只提交不等结果（通知用，不耽误主流程）。
    返回 (ok, 状态, 输入框里挡路的草稿或 None)。"""
    body = {"to": chat, "text": text, "allow_group": group}
    if reply_to is not None:
        body["reply_to"], body["me"] = reply_to, me_id
    if allow_draft is not None:
        body["allow_draft"] = allow_draft
    r = call("/send", body, wait)
    if not wait:
        return bool(r), "submitted", None
    if r and "rate-limit" in str(r.get("status")):   # 服务端：全局两次至少隔 15 秒、同一个人至少隔 20 秒，状态里带剩余秒数
        m = re.search(r"\(([\d.]+)s left\)", str(r.get("status")))
        time.sleep(float(m.group(1)) + 0.5 if m else 21)
        r = call("/send", body)
    if not r and getattr(call, "last", None):   # 没拿到结果：同一个任务号再问一次（不会重发）
        r = _job(call.last, {"state": "running"}, time.time() + 60)
    if r:
        return bool(r.get("ok")), r.get("status") or r.get("error"), r.get("draft")
    seen = False
    for delay in (0, 60):   # 还没有：可能是慢发迟到落库，隔一分钟再看一次；仍没看到也不补发，交本人定
        time.sleep(delay)
        try:
            seen = sent(chat, text, me_id, 10)
        except Exception:
            seen = False
        if seen: break
    return seen, "没拿到发送结果；查消息" + ("已发出" if seen else "没看到（没补发，免得重复）"), None

IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "img_cache")
IMG_TTL = 3600          # 图片缓存保留 1 小时，过期自动删（不长期留别人的图）

def _img_clean():
    now = time.time()
    try:
        for f in os.listdir(IMG_DIR):
            p = os.path.join(IMG_DIR, f)
            if now - os.path.getmtime(p) > IMG_TTL:
                os.remove(p)
    except OSError:
        pass

def image(chat, mid, thumb=True):
    """取一条图片消息解密后的图，存到 img_cache/ 返回本地路径；拿不到返回 None。
    wxtest负责解密；thumb=True 取缩略图（看懂内容足够、省 token），要高清传 False。
    图没下载到本地（404）、首次派生密钥中（503）、网络断都当拿不到，不影响文字回复。
    返回文件路径而不是 base64：任务 JSON 保持小，Muse 用自己的看图能力打开文件。"""
    import hashlib
    os.makedirs(IMG_DIR, mode=0o700, exist_ok=True)
    _img_clean()
    name = hashlib.md5(f"{chat}:{mid}:{int(thumb)}".encode()).hexdigest()
    return _img_get(name, f"/image?chat={chat}&id={mid}&thumb={1 if thumb else 0}&b64=1")

def moment_image(tid, i=0, thumb=False):
    """朋友圈 tid 的第 i 张图（从 0 数；视频是封面），wxtest 下载解密，存到 img_cache/ 返回本地路径；拿不到返回 None。"""
    import hashlib
    os.makedirs(IMG_DIR, mode=0o700, exist_ok=True)
    _img_clean()
    return _img_get(hashlib.md5(f"sns:{tid}:{i}:{int(thumb)}".encode()).hexdigest(),
                    f"/moments/image?tid={tid}&i={int(i)}&thumb={1 if thumb else 0}&b64=1")

def moment_images(feed, limit=3):
    """一条朋友圈里的图（视频取封面），最多 limit 张：[路径]；拿不到的跳过。"""
    if feed.get("kind") not in ("image", "video", "finder"): return []
    return [p for p in (moment_image(feed["tid"], i) for i in range(min(int(feed.get("media") or 0), limit))) if p]

def _img_get(name, path):
    """按 name 查本地缓存，没有就调 wxtest 的 b64 接口取图存下来，返回路径；失败返回 None。"""
    import base64
    for ext in ("jpg", "png", "gif", "webp", "bmp"):
        p = os.path.join(IMG_DIR, f"{name}.{ext}")
        if os.path.exists(p):
            return p
    try:
        r = api(path, timeout=60)
        data, mime = base64.b64decode(r["b64"]), r["mime"]
    except Exception:
        return None
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp"}.get(mime, "jpg")
    p = os.path.join(IMG_DIR, f"{name}.{ext}")
    tmp = p + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    return p

def file(chat, mid):
    """取一条收到的文件，原样存到 img_cache/（保留原文件名和后缀，1 小时后自动删），返回 (本地路径, 原因)。
    没下载到本地、太大、网络断都返回 (None, 原因)，不影响文字回复。解析由 Muse 自己打开文件来做。"""
    import base64, hashlib
    os.makedirs(IMG_DIR, mode=0o700, exist_ok=True)
    _img_clean()
    tag = hashlib.md5(f"{chat}:{mid}".encode()).hexdigest()[:8]
    for f in os.listdir(IMG_DIR):
        if f.startswith(tag + "_"):
            return os.path.join(IMG_DIR, f), ""
    for i in range(4):                       # 刚收到的文件电脑上可能还在下载，等几秒再取
        try:
            r = api(f"/file?chat={urllib.parse.quote(chat)}&id={mid}&b64=1", timeout=120)
            data = base64.b64decode(r["b64"]); break
        except urllib.error.HTTPError as e:
            try: err = json.load(e).get("error", "")
            except ValueError: err = ""
            if err == "not downloaded" and i < 3:
                time.sleep(5); continue
            return None, {"not downloaded": "电脑上还没下载到这个文件", "too large": "文件太大"}.get(err) or f"取文件失败（{e.code} {err}）"
        except Exception as e:
            return None, f"取文件失败（{e}）"
    p = os.path.join(IMG_DIR, f"{tag}_{os.path.basename(r.get('name') or 'file')}")
    with open(os.open(p + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
        f.write(data)
    os.replace(p + ".tmp", p)
    return p, ""

def attach_files(msgs, chat, limit=2):
    """新消息里的文件：[{id, name, path 或 error}]，最新的优先，最多 limit 个。"""
    out = []
    for m in reversed(msgs):
        if m.get("type") != 49 or m.get("app") != 6:
            continue
        p, why = file(chat, m["id"])
        out.append({"id": m["id"], "name": m.get("content") or "", **({"path": p} if p else {"error": why})})
        if len(out) >= limit:
            break
    return list(reversed(out))

def attach_images(msgs, chat, limit=3):
    """新消息里的图片和表情：[{id, path}]（表情加 emoji: true），最新的优先，最多 limit 张（防刷屏/省 token）。
    表情由 wxtest 转成 jpg：动图是按顺序抽几帧拼成的一张宫格。"""
    out = []
    for m in reversed(msgs):
        if m.get("type") not in (3, 47):
            continue
        p = image(chat, m["id"])
        if p:
            out.append({"id": m["id"], "path": p, **({"emoji": True} if m.get("type") == 47 else {})})
        if len(out) >= limit:
            break
    return list(reversed(out))

def transcript(msgs, me_id, me, each=500, total=3000, ids=False):
    """把消息拼成对话文本（ids=True 每行前面带消息 id，给本人指令里查消息、转发用）：只要文字，图片/表情等写成占位。单条超 each 字截断，总长超 total 只留最近的（防刷长文烧 token）。
    本人号上 AI 发的（带 ai）写成「<me>的助理」，不和本人亲口说的混在一起（画像、学说话风格都不能拿 AI 的话当本人的）。"""
    tag = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]", 49: "[卡片]"}
    out = []
    for m in msgs:
        who = (f"{me}的助理" if m.get("ai") else me) if m.get("sender_id") == me_id else (m.get("sender") or "对方")
        t = m.get("type")
        body = (m.get("content", "") if t == 1 else f"[图片#{m.get('id')}]" if t == 3
                else f"[表情#{m.get('id')}" + (f"：{m['content']}" if m.get("content") else "") + "]" if t == 47 else tag.get(t, "[其他]"))
        if t == 10000:
            body = f"[系统：{m.get('content') or ''}]"
        elif t == 50:                                   # 通话：「[通话] 通话时长 00:15」「[通话] 已取消」
            body = m.get("content") or "[通话]"
        elif t == 48:
            lc = m.get("location") or {}
            body = f"[位置：{m.get('content') or ''}" + (f"（坐标 {lc['lat']},{lc['lng']}）" if lc.get("poi") or lc.get("label") else "") + "]"
        elif t == 42:
            body = f"[名片：{m.get('content') or ''}]"
        elif t == 49 and m.get("app") == 19 and m.get("record"):    # 合并转发：展开每条
            body = f"[转发的聊天记录：{m.get('content') or ''}]" + "".join(
                f"\n  | {r['sender']}（{r['time']}）：{r['text'][:200]}" for r in m["record"])
        elif t == 43:                                   # 视频：带 id，要懂内容跑 awx.py video
            body = f"[视频#{m.get('id')}]"
        elif t == 49 and m.get("finder"):
            body = f"[视频号#{m.get('id')}：{m['finder'].get('title') or ''}]"
        elif t == 49 and m.get("app") == 6:
            body = f"[文件#{m.get('id')}：{m.get('content') or ''}]"
        elif t == 49 and m.get("app_name"):              # 卡片：链接/文件/小程序/转账…带标题
            body = f"[{m['app_name']}：{(m.get('des') if m.get('app') in (2000, 2001) else m.get('content')) or m.get('des') or ''}]"
        if m.get("quote"):                             # 引用回复：先写被引用的那句
            qt = m["quote"]
            qw = (f"{me}的助理" if qt.get("ai") else me) if qt.get("sender_id") == me_id else qt.get("sender") or "某人"
            body = f"（引用 {qw}：{qt.get('text', '')[:100]}）" + body
        lim = each * 4 if m.get("record") else each      # 转发的聊天记录放宽
        if len(body) > lim:
            body = body[:lim] + "…（太长，已截断）"
        out.append((f"#{m.get('id')} " if ids else "") + f"{who}: {body}")
    text = "\n".join(out)
    return text if len(text) <= total else "…（更早的省略）\n" + text[-total:].split("\n", 1)[-1]


def cli_articles(words):
    """收到的公众号文章：标题/公众号里含全部关键词的，最新在前。"""
    for a in api("/articles?limit=200").get("articles", []):
        s = a["title"] + " " + (a.get("account_name") or "")
        if all(w in s for w in words):
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(a['time']))}  {a.get('account_name')}  {a['title']}\n  {a['url']}")

def cli_article(url):
    """公众号文章正文（纯文本；正文里的图片表格看不到）。"""
    d = api("/article?url=" + quote(url))
    print(d.get("title", ""), "|", d.get("account_name", ""), "\n", d.get("text") or d.get("error") or "（没取到正文）")

USAGE = ("用法：python3 awx.py search <关键词，空格分开=都要有> [聊天的微信号]   每条前面是消息 id\n"
         "      python3 awx.py chat <微信号或名字> [条数]  某个聊天最近的消息（默认 30 条，每条前面是消息 id）\n"
         "      python3 awx.py forward <来源微信号或名字> <转到哪：微信号/备注/群名> <消息id…> [--one]\n"
         "                           用微信自己的转发转出去；多条默认合并成聊天记录卡片，--one 逐条转；只在本人亲口让转时用\n"
         "      python3 awx.py articles [关键词…]   列收到的公众号文章（含链接）\n"
         "      python3 awx.py article <链接>        读文章正文\n"
         "      python3 awx.py speak <微信号> <内容>  发语音条（wxtest 转成语音（本人克隆声），结尾念设置 voice_sign）；只在本人亲口让发时用\n"
         "      python3 awx.py video <微信号> <消息id> [问题]  看懂视频（含视频号分享），几十秒到几分钟\n"
         "      python3 awx.py voices  已有哪些声音（克隆：文件传输助手发录音+「克隆声音 本人/新名字」，wxtest 自己做）")

def _video_busy(chat):
    """看视频要几十秒到几分钟：告诉值班手里这条在处理（刷新 inbox/.<ID>.act），别 180 秒交还、别当过期丢掉。"""
    try:
        import watch
        for f in watch.pending():
            if chat in open(f, encoding="utf-8").read(): watch.begin(os.path.basename(f)[:-5])
    except Exception as e: print(f"[video] 标记处理中没成：{type(e).__name__}: {e}", file=sys.stderr)

def video_answer(p, tag, busy=lambda: None):
    """/video/ask 或 /moments/video/ask：202 就接着等（最多 15 分钟），打印答案；没人声把画面存成图、打印目录。"""
    end = time.time() + 900
    try:
        while True:
            busy()
            if "state" not in (r := api(p, timeout=70)) or time.time() >= end: break
        busy()   # 等完再刷一次：看图、写回复还要一会
    except urllib.error.HTTPError as e:
        sys.exit(f"没看成：{e.read().decode(errors='replace') if e.fp else e.msg}")
    if "state" in r: sys.exit("还没处理完，过几分钟再跑一次")
    if r.get("speech") is False:   # 没人声：画面存成图，Muse 自己看图脑补
        import base64, tempfile
        d = tempfile.mkdtemp(prefix=f"awx-video-{tag}-")
        for f in r.get("frames", []):
            t = int(f["t"]); open(os.path.join(d, f"{t // 60:02d}m{t % 60:02d}s.jpg"), "wb").write(base64.b64decode(f["b64"]))
        print(f"这条视频没人声，时长 {r.get('duration')} 秒，按时间截了 {len(r.get('frames', []))} 张画面（文件名=第几分几秒），自己看图判断讲了什么：{d}")
    else: print(r.get("answer"))

if __name__ == "__main__":     # 给 Muse 用的命令行：查更早的聊天、看公众号文章
    cmd = sys.argv[1:2]
    if cmd == ["articles"]:
        cli_articles(sys.argv[2:]); sys.exit()
    if cmd == ["article"] and len(sys.argv) > 2:
        cli_article(sys.argv[2]); sys.exit()
    if cmd == ["video"] and len(sys.argv) > 3:   # wxtest 取视频交给 NotebookLM，处理中（202）就接着等
        video_answer(f"/video/ask?chat={quote(sys.argv[2])}&id={sys.argv[3]}&q={quote(' '.join(sys.argv[4:]))}&wait=55",
                     sys.argv[3], lambda: _video_busy(sys.argv[2])); sys.exit()
    if cmd == ["voices"]:
        print(api("/voices")); sys.exit()
    if cmd == ["speak"] and len(sys.argv) > 3:
        import db, voices
        u, cfg = sys.argv[2], db.config(db.conn())
        try: tags = [] if u.endswith("@chatroom") else api("/labels?user=" + quote(u)).get("labels", [])
        except Exception: tags = []
        v = "default" if u.endswith("@chatroom") else (voices.pick(cfg, u, None, tags) or "default")   # 按人选声音，同自动回复
        r = call("/speak", {"to": u, "text": " ".join(sys.argv[3:]), "allow_group": u.endswith("@chatroom"),
                            "voice": v, "tail": voices.tail(cfg, v)}, 300) or {}
        print("发出了" if r.get("ok") else f"没发成：{r.get('status') or r.get('error') or '没结果，去聊天里看一眼'}"); sys.exit()
    if cmd in (["chat"], ["forward"]) and len(sys.argv) > 2 + (cmd == ["forward"]) * 2:
        import db
        c = db.conn(); db.init(c); cfg = db.config(c)
        try: u = find_chat(sys.argv[2])
        except LookupError as e: sys.exit(str(e))
        if cmd == ["chat"]:
            n = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else 30
            print(f"== {u}"); print(transcript(messages(u, min(n, 200)), cfg["me_id"], cfg["me"] or cfg["title"], total=20000, ids=True)); sys.exit()
        nums = [a for a in sys.argv[4:] if a != "--one"]
        if not nums or not all(re.fullmatch(r"-?\d+", a) for a in nums): sys.exit("消息 id 要写数字（awx.py chat 看到的 # 后面那个）")
        ok, st = forward(u, [int(a) for a in nums], sys.argv[3], False if "--one" in sys.argv else None)
        print(("转出去了：" if ok else "没转成：") + str(st)); sys.exit()
    if cmd != ["search"] or len(sys.argv) < 3:
        sys.exit(USAGE)
    import db
    c = db.conn(); db.init(c); cfg = db.config(c)
    r = search(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
    for h in r.get("results", []):
        print(f"== {h['chat_name']}（{h['chat']}，{time.strftime('%Y-%m-%d %H:%M', time.localtime(h['time']))}）")
        print(transcript(h["before"] + [h] + h["after"], cfg["me_id"], cfg["me"] or cfg["title"], total=1500, ids=True))
    print(f"共 {r.get('count', 0)} 条" + ("，只列最新的" if r.get("count", 0) > len(r.get("results", [])) else ""))
