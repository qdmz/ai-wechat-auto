#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Muse 循环跑：挂在 wxtest 上领任务，没活时什么都不输出（Muse 直接下一轮，不用动脑）。
wxtest 已把微信里的事实整理好做成任务：合并连发、群里有没有叫到本人、本人在不在聊、语音转好文字、刷屏不叫。
这里只做千人千面的那部分：按画像和规则决定回不回、整理资料；要回就输出 {"tasks": [...]}（只有事实字段，写法在 SKILL.md），
Muse 写出 JSON 后执行任务里的 act 命令；不回就当场交回。设置一变就把只读过滤名单推给 wxtest。
平时由 watch.py 循环调用。后台心跳 hb.py 自动拉起，只防掉线：单条领走 5 分钟没交 wxtest 照样收回；多个 agent 同时跑时 wxtest 按租约一条只派给一个。
文件传输助手里本人发的管理命令、别人撤回消息、朋友圈评论、微信掉线/恢复、每晚日报也都是 wxtest 派的任务（掉线卡住它自己重启微信）；
这里定措辞，掉线告警按 settings.json 的 alert 走 Bark/邮件/webhook/Muse。wxtest 本身连不上时这里自己报。"""
import json, os, sys, time
import alert, awx, db, mcp, persona, psync, reply_rules as rr, skillup, sns, voices

WAIT = 18                 # 没活干时挂在 wxtest 上等任务的秒数（来活立刻返回）
ALERT_EVERY = 1800        # wxtest 一直连不上时，每 30 分钟再提醒一次
DUP_WAIT = 60             # 重名被停派时隔多久再问（改了名就能接着领）
API_DOWN = 120            # 连不上 wxtest 多久才报（防网络抖动误报）
RISK_LOGOUTS = 3          # 24 小时内掉线达到这个次数，恢复时提醒疑似登录风控
NOTES = []                # 这条任务要发给本人的通知，交任务时一起交给 wxtest 发

def say(cfg, text):
    print("[notify]", text, file=sys.stderr, flush=True)   # 标准输出只能有任务 JSON
    if cfg["notify_to"]:
        NOTES.append(text)

def notes():
    return {"notify": "\n".join(NOTES)} if NOTES else {}

def _muse_alert(text, qr=""):
    """微信里发不出的掉线通知：Muse 用自己和本人的聊天渠道转告（怎么做见 SKILL.md 常驻第 6 条）。"""
    return {"kind": "alert", "alert": text, **({"qr": qr} if qr else {})}

def reach(c, err=None):
    """wxtest 本身连不上（机器停了、外网地址断了）：它报不了，只能这边按 alert 设置报。返回要 Muse 转告的任务或 None。"""
    now, since = int(time.time()), int(db.meta(c, "api_down") or 0)
    if err is None:
        if since:
            if db.meta(c, "api_alert"):
                ext = [x for x in alert.channels(db.meta(c, "alert")) if x != "muse"]
                if ext:
                    alert.send(",".join(ext), "微信接口已恢复", f"又连上 wxtest 了（断了约 {(now - since) // 60 + 1} 分钟）。")
            db.meta_set(c, "api_down", ""); db.meta_set(c, "api_alert", "")
        return None
    if not since:
        db.meta_set(c, "api_down", str(now)); return None
    if now - since < API_DOWN or now - int(db.meta(c, "api_alert") or 0) < ALERT_EVERY:
        return None
    db.meta_set(c, "api_alert", str(now))
    text = (f"连不上 wxtest（{err}），已有约 {(now - since) // 60 + 1} 分钟，期间 AI 不回任何消息。"
            "可能是 wxtest 那台机器停了或外网地址断了。")
    muse, _ = alert.send(db.meta(c, "alert"), "连不上微信接口", text)
    return _muse_alert(text) if muse else None

def dup_name(c, e):
    """本机名字和别台重了（wxtest 只给先来的派任务）：每 ALERT_EVERY 告诉本人一次去改名。返回要 Muse 转告的任务或 None。"""
    now = int(time.time())
    if now - int(db.meta(c, "dup_alert") or 0) < ALERT_EVERY: return None
    db.meta_set(c, "dup_alert", str(now))
    try: why = json.loads(e.read()).get("why") or ""
    except (ValueError, AttributeError, OSError) as x:
        print(f"[task] 读重名说明失败：{x}", file=sys.stderr); why = ""
    text = why or "本机 agent 名字和另一台重了，wxtest 已停止给本机派任务。请在 settings.json 改 agent（每台不同），再跑 seed.py"
    muse, _ = alert.send(db.meta(c, "alert"), "agent 名字重了", text)
    return _muse_alert(text) if muse else None

def _left(c, pushed):
    """alert 设置里 wxtest 还没推过的渠道（它推过的 serverchan/bark/webhook 不重复推）。"""
    return ",".join(x for x in alert.channels(db.meta(c, "alert")) if x.partition(":")[0] not in pushed)

def login_task(c, cfg, t):
    """wxtest 派来的掉线（alert）/恢复（up）任务。info.pushed = wxtest 已推过的渠道（phone 是控制面板绑的手机推送）。"""
    i = t.get("info") or {}
    pushed = set(i.get("pushed") or [])
    if t["kind"] == "alert" and i.get("text"):   # wxtest 自己的故障（卡死自动重载等），不是掉线
        muse, _ = alert.send(db.meta(c, "alert"), "微信助理出故障", i["text"])
        awx.task_done(t["id"], {"result": {"action": "alert"}})
        return _muse_alert(i["text"]) if muse else None
    if t["kind"] == "alert":
        qr, down = f"{awx.BASE}/qr?token={awx.TOKEN}", "微信掉线了，需要扫码或在手机上确认登录"
        text = f"{down}，已有约 {i.get('mins', 1)} 分钟，期间 AI 不回任何消息。"
        left = _left(c, pushed)
        muse = bool(left) and alert.send(left, "微信掉线了", text + "打开链接扫码：", qr)[0]
        awx.task_done(t["id"], {"result": {"action": "alert"}})
        return _muse_alert(text, qr) if muse else None
    msg = f"微信已恢复（断了约 {i.get('mins', 1)} 分钟），AI 已接回。"
    if i.get("logouts_24h", 0) >= RISK_LOGOUTS:          # 一天内频繁掉线：疑似登录风控，提醒本人
        msg += (f" 注意：最近 24 小时掉线 {i['logouts_24h']} 次，疑似登录风控。"
                "建议别频繁切设备登录，或开 WX_PROXY 走固定住宅出口 IP。")
    ext = [x for x in alert.channels(_left(c, pushed)) if x != "muse"]   # 剩空串时 channels 给 muse，过滤后为空
    if ext:
        alert.send(",".join(ext), "微信已恢复", msg)
    if "phone" not in pushed: say(cfg, msg)       # 手机已推过就不再发通知（发通知也会推手机，重复）
    awx.task_done(t["id"], {"result": {"action": "notify"}, **notes()})
    return None

def _resolve(cfg, token):
    """把命令里的名字/备注对到一个会话 username；对不上或多个都返回提示。"""
    token = token.strip()
    if not token:
        return None, "没说是谁"
    try:
        ss = awx.api("/sessions").get("sessions", [])
    except Exception as e:
        return None, f"读会话失败（{e}）"
    me = cfg["me_id"]
    hit = [s for s in ss if s["username"] != me and token in (s.get("name") or "")]
    if not hit:                                   # 名字对不上就按 username 直接给
        hit = [s for s in ss if s["username"] == token]
    if not hit:
        return None, f"没找到「{token}」"
    if len(hit) > 1:
        return None, f"「{token}」对上了 {len(hit)} 个：" + "、".join((s.get("name") or s["username"]) for s in hit[:5]) + "，说全一点"
    return hit[0], (hit[0].get("name") or hit[0]["username"])

# 命令 → (profile 字段, 值, 回执动词)。群命令另算。
_CMDS = [("不回", "mode", "off"), ("拉黑", "mode", "off"), ("别回", "mode", "off"),
         ("草稿", "mode", "draft"), ("直发", "mode", "auto"), ("恢复回复", "mode", "auto")]
_GRP = [("只回@", "reply_when", "mention"), ("全回", "reply_when", "all"), ("群不回", "reply_when", "never")]

# 菜单：发「菜单」弹出编号，回数字；要名字的再回名字，等于发「前缀+名字」。
_MENU = [("AI 暂停（全部不回）", "AI暂停"), ("AI 恢复", "AI恢复"), ("某人不回", "不回"), ("某人只存草稿", "草稿"),
         ("某人直接回", "直发"), ("解封某人", "解封"), ("某群全回", "全回"), ("某群只回@", "只回@"), ("某群不回", "群不回"),
         ("AI 接管等待时间（你在聊时）", "接管")]
HANDOFF_MAX = 240

def handoff_help(cfg):
    return (f"接管等待，现在是 {cfg['handoff_min']} 分钟：\n"
            f"你自己正在某个聊天里聊着时，对方再发消息，你 {cfg['handoff_min']} 分钟内没回，AI 才替你接着回；这期间对方的消息不会丢。\n"
            "想 AI 晚点插嘴就调大，想对方少等就调小。很久没在那个聊天说话时不受影响，AI 照常马上回。")
MENU_TTL = 600
GUIDE_WORDS = ("教程", "使用教程", "使用说明", "说明书", "文档", "guide")
MENU_WORDS = ("菜单", "命令", "指令", "帮助", "用法", "怎么用", "功能", "help", "menu", "?", "？")   # 不分大小写
GUIDE_PIECE = 1500        # 教程按段落拼成每条不超过这么多字，分几条发（wxtest 全局两条至少隔 15 秒）

def guide_pieces(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "guide.md")
    out = []
    for part in open(path, encoding="utf-8").read().strip().split("\n\n"):
        if out and len(out[-1]) + 2 + len(part) <= GUIDE_PIECE: out[-1] += "\n\n" + part
        else: out.append(part)
    return out

def guide(cfg):
    """把 docs/guide.md 分几条发到文件传输助手（命令就是在那里发的）。"""
    try: ps = guide_pieces()
    except OSError as e:
        print(f"[教程] 读 docs/guide.md 失败：{e}", file=sys.stderr, flush=True); say(cfg, f"教程文件读不到（{e}），请让助理检查 skill"); return
    for i, p in enumerate(ps, 1):
        ok, st, _ = awx.send("filehelper", p, cfg["me_id"])
        print(f"[教程] 第 {i}/{len(ps)} 条 {'已发' if ok else '没发出：' + str(st)[:60]}", file=sys.stderr, flush=True)
        if not ok:
            say(cfg, f"教程第 {i} 条没发出（{str(st)[:40]}），稍后再发「教程」"); return

def _menu(c, cfg, t):
    """处理菜单交互，返回要执行的命令文本（None=这条已处理完或不是菜单）。"""
    st = db.meta_json(c, "menu", {})
    fresh = time.time() - st.get("t", 0) < MENU_TTL
    if t.lower() in MENU_WORDS:
        db.meta_set(c, "menu", json.dumps({"t": int(time.time())}))
        say(cfg, "回数字选命令：\n" + "\n".join(f"{i}. {x}" for i, (x, _) in enumerate(_MENU, 1)) + "\n完整使用说明发「教程」")
        return None
    known = [x for _, x in _MENU] + [x for x, _, _ in _CMDS] + ["AI", "标签", "拍", "备注", "语音", "退回画像", "接管", *GUIDE_WORDS]
    if fresh and st.get("pre") == "接管 " and t.isdigit():                  # 等分钟数
        db.meta_set(c, "menu", json.dumps({"t": int(time.time())}))
        return "接管 " + t
    if fresh and st.get("pre") and not t.isdigit() and not any(t.upper().startswith(k) for k in known):   # 等名字
        db.meta_set(c, "menu", json.dumps({"t": int(time.time())}))   # 菜单还有效，可接着回下一个数字
        return st["pre"] + t
    if fresh and t.isdigit() and 1 <= int(t) <= len(_MENU):
        label, cmd = _MENU[int(t) - 1]
        if cmd in ("AI暂停", "AI恢复"):
            db.meta_set(c, "menu", json.dumps({"t": int(time.time())}))
            return cmd
        if cmd == "接管":
            db.meta_set(c, "menu", json.dumps({"t": int(time.time()), "pre": "接管 "}))
            say(cfg, handoff_help(cfg) + f"\n回分钟数改（1~{HANDOFF_MAX}），如 30")
            return None
        db.meta_set(c, "menu", json.dumps({"t": int(time.time()), "pre": cmd}))
        say(cfg, f"「{label}」：回{'群' if '群' in label else ''}名字（备注或昵称里的几个字就行）")
        return None
    if t.isdigit() and len(t) <= 2:
        say(cfg, "菜单过期了，先发「菜单」再回数字")
        return None
    return t

def commands(c, cfg, lead=True):
    """文件传输助手里本人发的管理命令：AI暂停/恢复（全局），以及按人/按群改回复规则；发「菜单」可按数字选。"""
    last, _ = db.cursor_get(c, "filehelper")
    msgs = [m for m in awx.messages("filehelper", 10) if m["id"] > (last or 0)]
    if not msgs:
        return
    top = max(m["id"] for m in msgs)
    for m in msgs:
        t = (m.get("content") or "").strip()
        if m.get("sender_id") != cfg["me_id"] or m.get("ai") or m.get("type") != 1 or not t:   # AI 发的（通知、回话）不是命令
            continue
        t = _menu(c, cfg, t)
        if not t:
            continue
        if t.lower() in GUIDE_WORDS:               # 使用教程：只让回话的那个 agent 发
            if lead: guide(cfg)
            continue
        if t[:2].upper() == "AI":                  # ai恢复 / Ai暂停 都认
            t = "AI" + t[2:].strip()
        if t in ("AI暂停", "AI恢复"):
            db.meta_set(c, "paused", "1" if t == "AI暂停" else "")
            say(cfg, "AI 已" + ("暂停" if t == "AI暂停" else "恢复"))
            continue
        if t.startswith("接管"):                   # 接管 30：本人在聊时对方再说话，几分钟没回 AI 才接（共用设置 handoff_min）
            n = t[2:].strip().replace("分钟", "").replace("分", "")
            if not n:
                say(cfg, handoff_help(cfg) + f"\n改：发「接管 30」（1~{HANDOFF_MAX} 分钟）"); continue
            if not n.isdigit() or not 1 <= int(n) <= HANDOFF_MAX:
                say(cfg, f"格式：接管 分钟数（1~{HANDOFF_MAX}），如「接管 30」"); continue
            if lead:                               # 只让回话的那台改，别台走设置同步
                psync.set_mine(c, "handoff_min", n, lambda x: print(x, file=sys.stderr, flush=True))
                cfg["handoff_min"] = int(n)
                say(cfg, f"好的，接管等待改成 {n} 分钟：你在聊时对方再发消息，你 {n} 分钟没回 AI 才接")
            continue
        done = False
        for pre, field, val in _GRP:   # 群命令优先（含 @）
            if t.startswith(pre):
                s, who = _resolve(cfg, t[len(pre):])
                if not s:
                    say(cfg, who); done = True; break
                if not db.is_group(s["username"]):
                    say(cfg, f"「{who}」不是群"); done = True; break
                db.set_rule(c, s["username"], who, field, val)
                say(cfg, f"好的，群「{who}」改成{pre}")
                done = True; break
        if done:
            continue
        if t.startswith("标签"):   # 标签 名字 X：X 是关系标签就换掉原来的关系（补充的留着），别的就加上；-X 去掉 X；无 = 全去掉
            parts = t[2:].split()
            if len(parts) < 2:
                say(cfg, "格式：标签 名字 新标签（如「标签 张三 客户」；「-客户」去掉一个，「无」全去掉）"); continue
            s, who = _resolve(cfg, " ".join(parts[:-1]))
            if not s or db.is_group(s["username"]):
                say(cfg, who if not s else f"「{who}」是群，群没有标签"); continue
            if not lead:                          # 多个 agent 只让回话的那个去动微信
                continue
            x = parts[-1]
            try: old = awx.api("/labels?user=" + awx.quote(s["username"])).get("labels", [])
            except Exception: old = []
            if x in ("无", "删", "去掉"):
                add, rm = [], old
            elif x[0] in "-－":
                add, rm = [], [x[1:]]
            else:
                top = db.label_parent(cfg, x) or (x if x in cfg["label_top"] else "")   # 换关系/大类：同层旧的去掉，大类跟着关系走
                add = [y for y in (top, x) if y]
                rm = [o for o in old if o not in add and ((x in cfg["label_rel"] and o in cfg["label_rel"])
                                                         or (top and o in cfg["label_top"])
                                                         or (x in cfg["label_top"] and o in cfg["label_rel"] and db.label_parent(cfg, o) not in ("", x)))]
            r = awx.set_labels(s["username"], add, rm, wait=180) or {}
            now = next((x.get("labels") for x in r.get("results", []) if "labels" in x), None)
            say(cfg, f"「{who}」的标签现在是：{'、'.join(now) or '无'}" if now is not None else f"「{who}」改标签没成功，稍后再试")
            continue
        if t[:3] in ("备注 ", "备注　"):   # 备注 名字 新备注：改这个人的微信备注；「无」= 删掉（要带空格，免得备忘被当命令）
            parts = t[2:].split()
            if len(parts) < 2:
                say(cfg, "格式：备注 名字 新备注（如「备注 张三 张总-某某公司」；「无」删掉备注）"); continue
            s, who = _resolve(cfg, " ".join(parts[:-1]))
            if not s or db.is_group(s["username"]):
                say(cfg, who if not s else f"「{who}」是群，这里只改好友备注"); continue
            if not lead:
                continue
            x = "" if parts[-1] in ("无", "删", "去掉") else parts[-1]
            r = awx.set_labels(s["username"], remark=x, wait=180) or {}
            res = next((y for y in r.get("results", []) if "remark" in y), None)
            ok = bool(res) and res.get("remark") == x
            say(cfg, f"「{who}」的备注现在是：{x or '无'}" if ok else f"「{who}」改备注没成功，稍后再试")
            continue
        if t[:3] in ("语音 ", "语音　"):   # 语音 名字 内容：转成语音发给这个人（群也行），声音按人选（voices.py）
            parts = t[3:].split(None, 1)
            if len(parts) < 2:
                say(cfg, "格式：语音 名字 内容（如「语音 张三 货明天上午发出」）"); continue
            s, who = _resolve(cfg, parts[0])
            if not s:
                say(cfg, who); continue
            if not lead:
                continue
            u = s["username"]
            try: tags = [] if db.is_group(u) else awx.api("/labels?user=" + awx.quote(u)).get("labels", [])
            except Exception: tags = []
            v = "default" if db.is_group(u) else (voices.pick(cfg, u, s.get("name"), tags) or "default")   # 本人让发也按人选声音
            r = awx.call("/speak", {"to": u, "text": parts[1], "allow_group": db.is_group(u), "voice": v, "tail": voices.tail(cfg, v)}, 300) or {}
            say(cfg, f"语音发给「{who}」了" if r.get("ok") else f"语音发给「{who}」没成功：{r.get('status') or r.get('error') or '没结果'}")
            continue
        if t[:2] in ("拍 ", "拍　") and t[2:].strip():   # 拍 名字：拍一拍这个人（只单聊）；要带空格，免得「拍照…」这种备忘被当命令
            s, who = _resolve(cfg, t[1:].strip())
            if not s or db.is_group(s["username"]):
                say(cfg, who if not s else "群里不拍，只拍单聊"); continue
            if not lead:
                continue
            r = awx.call("/pat", {"chat": s["username"]}, 120) or {}
            say(cfg, f"拍了拍「{who}」" if r.get("ok") else f"拍「{who}」没成功：{r.get('status') or r.get('error') or '没结果'}")
            continue
        if t.startswith("退回画像"):   # 退回画像 名字：AI 记错了，退回上一版（wxtest 每条留现版和上一版），所有 agent 马上跟着改
            s, who = _resolve(cfg, t[4:])
            if not s:
                say(cfg, who); continue
            if not lead:
                continue
            r = awx._post("/profiles/rollback", {"key": "p:" + s["username"]}, 20) or {}
            say(cfg, f"「{who}」的画像已退回上一版，各 agent 马上跟着改" if r.get("ok") else f"「{who}」没退回：{r.get('error') or '没结果'}")
            continue
        if t.startswith("解封"):
            s, who = _resolve(cfg, t[2:])
            if not s:
                say(cfg, who)
            else:
                db.unset_rule(c, s["username"], "mode")
                awx.call("/unmute", {"chat": s["username"]}, 30)   # wxtest 那边的机器人对聊静默也解除
                say(cfg, f"好的，已解封「{who}」")
            continue
        for pre, field, val in _CMDS:
            if t.startswith(pre):
                s, who = _resolve(cfg, t[len(pre):])
                if not s:
                    say(cfg, who)
                else:
                    db.set_rule(c, s["username"], who, field, val)
                    say(cfg, f"好的，{pre}「{who}」")
                break
    db.cursor_set(c, "filehelper", top)

def chat_name(c, chat, sessions=None):
    """会话名：先看库里的备注/昵称（seed 导入过），没有才去查会话列表。"""
    row = c.execute("SELECT alias FROM profile WHERE chat=?", (chat,)).fetchone()
    if row and row["alias"] and row["alias"] != chat:
        return row["alias"]
    try:
        ss = sessions if sessions is not None else awx.api("/sessions").get("sessions", [])
    except Exception:
        ss = []
    name = next((x.get("name") for x in ss if x["username"] == chat and x.get("name")), None)
    if name:                                   # 记进库，下次不用再查（同 seed.py 导入会话）
        c.execute("INSERT OR IGNORE INTO profile(chat,alias,is_group,updated) VALUES(?,?,?,?)",
                  (chat, name, int(db.is_group(chat)), int(time.time())))
        c.execute("UPDATE profile SET alias=? WHERE chat=? AND (alias IS NULL OR alias='' OR alias=chat)", (name, chat))
        c.commit()
    return name or chat

def digest_task(c, cfg):
    """设置了 digest（要每天摘要的群）：取这些群过去 24 小时的聊天，交给 Muse 写摘要发给本人；没有就返回 None。"""
    if not cfg["digest"] or not cfg["notify_to"]:
        return None
    try:
        groups = [s for s in awx.api("/sessions").get("sessions", []) if db.is_group(s["username"])]
    except Exception as e:
        print(f"[digest] {e}", file=sys.stderr); return None
    since, parts, seen = time.time() - 86400, [], set()
    for key in cfg["digest"]:
        for s in [s for s in groups if s["username"] == key or key in (s.get("name") or "")][:3]:
            if s["username"] in seen:
                continue
            seen.add(s["username"])
            try:
                msgs = [m for m in awx.messages(s["username"], 200) if m.get("time", 0) >= since]
            except Exception as e:
                print(f"[摘要] 读 {s.get('name') or s['username']} 失败：{e}", file=sys.stderr, flush=True)
                parts.append(f"【{s.get('name') or s['username']}】消息读取失败，今天没摘要"); continue
            if msgs:
                parts.append(f"【{s.get('name') or s['username']}】{len(msgs)} 条\n"
                             + awx.transcript(msgs, cfg["me_id"], cfg["me"] or cfg["title"], each=300, total=6000))
    if not parts:
        return None
    return {"kind": "群摘要", "chat": cfg["notify_to"], "alias": "群摘要", "title": cfg["title"], "context": "\n\n".join(parts),
            "act": f"python3 say.py {cfg['notify_to']}"}

def owner_task(cfg, t):
    """本人在自己跟自己的聊天里发的话（wxtest 已认过：本人手机发的文字，不是 AI 备注或通知开头）：原话 + 自聊最近几条，交给 Muse 照办。"""
    me, sg = cfg["me_id"], (cfg["sign"] or "").strip()
    mine = lambda m: (m.get("type") == 1 and m.get("sender_id") == me and not m.get("ai") and (m.get("content") or "").strip()
                      and not m["content"].strip().startswith(("【助理通知】",) + ((sg,) if sg else ())))
    msgs = t.get("messages") or []
    said = [m["content"].strip() for m in msgs if t["from_id"] <= m["id"] <= t["upto"] and mine(m)]
    if not said:
        return None
    earlier = [m for m in msgs if m["id"] < t["from_id"]][-10:]
    tools = mcp.brief()
    return {"kind": "本人指令", "chat": me, "said": "\n".join(said), **({"tools": tools} if tools else {}),
            "earlier": awx.transcript(earlier, me, cfg["me"] or cfg["title"], each=300, total=2000) if earlier else "",
            "act": f"python3 say.py {me}"}

def _remember(c, chat, recent, cfg, voice=None, ask=(), new=()):
    """回的是哪条、对方说了什么、要不要用语音回（用谁的声音）、这次对方新发的是哪几条（要转发时转这些），留给 act.py，它就不用再读一遍消息。"""
    rid = next((m["id"] for m in reversed(recent) if m.get("sender_id") != cfg["me_id"]), None)
    said = next(((m.get("content") or "")[:60] if m.get("type") == 1 else "[非文字]"
                 for m in reversed(recent) if m["id"] == rid), "")
    qs = [[m["id"], (m.get("content") or "")[:200]] for m in recent[-30:]          # AI 想引用某句时，act.py 按原话在这里找 id
          if m.get("type") == 1 and not m.get("ai") and (m.get("content") or "").strip()]
    fw = [[m["id"], ((m.get("content") if m.get("type") == 1 else m.get("voice_text")) or "")[:200]]   # 要转发时 AI 可从中挑几条（act.pick_ids）
          for m in new if m.get("sender_id") != cfg["me_id"] and not m.get("ai") and m.get("type") not in (10000, 50)]
    db.meta_set(c, "task:" + chat, json.dumps({"rid": rid, "said": said, "voice": voice, "ask": list(ask), "qs": qs, "fw": fw}, ensure_ascii=False))

def _build(c, chat, name, cfg, recent, new, tags=(), remark=None, called=False):
    # 机器人对聊、刷屏、勿回、拍一拍、本人在聊、群里没叫到本人：wxtest 派任务前已经处理掉了
    prof = db.profile(c, chat)
    awx.voice_text(recent)                                   # 语音 wxtest 派任务前已转好文字
    ok, why = rr.should_reply(prof, new, cfg, called)
    need = not prof["is_group"] and persona.stale(prof)      # 群不画像，省 token
    if not ok and not (need and why == "只有图片/表情"):
        print(f"[skip] {name}: {why}", file=sys.stderr)
        return None
    if tags:                                                 # 本人给 TA 打的微信标签（wxtest 随任务带来）
        prof = dict(prof, rules=db.rules(chat, prof.get("alias"), tags))
        if "VIP" in tags:                                    # VIP：正事一律只存草稿/转告本人
            prof["risk"] = "high"
    who, context = rr.person(prof, recent, cfg)
    # 任务包只放事实，怎么用这些字段写在 SKILL.md「写回复的规矩」（命令口吻的说明放进包里会被平台安全检查拦）
    t = {"kind": "群聊" if prof["is_group"] else "私聊", "chat": chat, "alias": name, "title": cfg["title"],
         "person": who, "context": context, "act": f"python3 act.py {chat}" + ("" if ok else " --media")}
    if prof["is_group"]:
        t["my_names"] = cfg["names"]
    if cfg["redlines"]:
        t["redlines"] = cfg["redlines"]
    if ok and (tools := mcp.brief()):        # 本人接的外部工具：能查到更准答案的事先查再回
        t["tools"] = tools
    if ok:                                  # 更早的聊天：照本人以前怎么跟 TA 聊来回，知道前因后果
        e = awx.earlier(chat, recent, cfg["me_id"], cfg["me"] or cfg["title"], n=60 if prof["is_group"] else 150)
        if e:
            t["earlier"] = e
    money = [] if prof["is_group"] else rr.paid(new, cfg)
    if money:                               # 本人定：别人转钱一定要回，道谢再接个话题
        t["money"] = money
    if tags:                                # 本人亲手打的微信标签
        t["tags"] = list(tags)
        if cfg["label_top"]: t["label_top"] = list(cfg["label_top"])
        if cfg["label_rel"]: t["label_rel"] = cfg["label_rel"]
    gt = {m.get("sender") or m["sender_id"]: m["sender_labels"] for m in recent if m.get("sender_labels") and m.get("sender_id") != cfg["me_id"]}
    if prof["is_group"] and gt:             # 群里各人的标签：回谁就按谁的身份拿捏语气
        t["group_tags"] = gt
    feeds = "" if prof["is_group"] or not (ok or need) else awx.moments(chat)
    if ok and feeds:                        # 对方最近的朋友圈：知道 TA 在忙什么
        t["moments"] = feeds
    if ok and cfg.get("vision"):            # 只有确定要回的才取图，不为每张图唤醒/花 token
        imgs = awx.attach_images(new, chat)
        if imgs:
            t["images"] = imgs
    last = next((m for m in reversed(new) if m.get("sender_id") != cfg["me_id"]), {})
    if ok and cfg["voice_reply"] and not prof["is_group"] and (last.get("content") or "").startswith("[语音]"):
        t["voice"] = voices.pick(cfg, chat, prof.get("alias"), tags)   # 对方发语音就回语音：用谁的声音、按谁的风格写
        if t["voice"]: t["spoken"] = voices.brief(cfg, t["voice"])   # 有这个字段（哪怕是空的）= 回复会念成语音
    vids = [] if prof["is_group"] or not ok else rr.videos(new, cfg)
    if vids:                                # 单聊发视频（含视频号）：先听完再聊内容（本人 11-03 定，群里不做）
        t["videos"] = [{"id": i, "cmd": f"python3 awx.py video {chat} {i}"} for i in vids[-2:]]
    if ok:                                  # 对方发了文件：取原文件给 Muse 自己打开读
        files = awx.attach_files(new, chat)
        if files:
            t["files"] = files
    lab = (db.labels_on(cfg) and not tags and not prof["is_group"] and (ok or need)
           and time.time() - float(db.meta(c, "labelask:" + chat) or 0) > persona.LABEL_EVERY)
    if lab:                                 # 本人清掉过 AI 打的标签：这个人 AI 不再打
        done = awx.ai_done("label")
        lab = done is not None and chat not in done
    rmk = (remark == "" and not prof["is_group"] and (ok or need)          # 还没备注（remark=None 是旧 wxtest 没带，不问）
           and time.time() - float(db.meta(c, "remarkask:" + chat) or 0) > persona.LABEL_EVERY)
    if rmk:                                 # 本人删掉过 AI 写的备注：这个人 AI 不再写
        done = awx.ai_done("remark")
        rmk = done is not None and chat not in done
    p = persona.task(c, chat, name) if need else None
    if (lab or rmk) and (p or ok):          # 顺带问一句：看得出就补，不强制
        if lab: t["ask_label"] = persona.label_opts(cfg, awx.all_labels())
        if rmk: t["ask_remark"] = True
        t["_ask"] = [k for k, y in (("labelask", lab), ("remarkask", rmk)) if y]   # 结论交上了 act 才记「问过」，任务过期下次照问
    if p:                                   # 还没画像（或过期）：先读 history 画像
        t["history"] = p["history"]
        if feeds and not ok: t["moments"] = feeds
    return t

FILTER_EVERY = 600       # 过滤名单没变也每 10 分钟重推一次（wxtest 重装过也能补上）

def push_filter(c, cfg):
    """把「肯定不用叫醒 AI」的名单推给 wxtest（只读副本，设置本身只在这里）。"""
    rows = c.execute("SELECT chat, mode, reply_when FROM profile WHERE chat != '*default*'").fetchall()
    f = {"me": cfg["me_id"], "sign": cfg["sign"], "voice_sign": cfg["voice_sign"], "paused": bool(db.meta(c, "paused")),
         "mute": sorted(r["chat"] for r in rows if r["mode"] == "off" and not db.is_group(r["chat"])),
         "mute_groups": sorted(r["chat"] for r in rows if (r["reply_when"] == "never" or r["mode"] == "off") and db.is_group(r["chat"])),
         "group_all": sorted(r["chat"] for r in rows if r["reply_when"] == "all" and db.is_group(r["chat"])),
         "names": cfg["names"], "merge": cfg["merge_sec"], "per_hour": cfg["per_hour"], "pat": cfg["pat"], "card": cfg["card"], "bot_mute_min": cfg["bot_mute_min"], "handoff_min": cfg["handoff_min"],
         "notify_to": cfg["notify_to"], "recall": cfg["recall"], "pending_max_min": cfg["pending_max_min"],
         "draft_wait": cfg["draft_wait"], "report_hour": cfg["report_hour"], "tz": 8 * 3600,
         "sns": cfg["sns"], "friend": cfg["friend"], "like": cfg["like"], "like_every": cfg["like_every"], "catchup_min": cfg["catchup_min"],
         "sync_min": cfg["sync_min"], "autolabel": db.labels_on(cfg), "pver": int(db.meta(c, "psync_cursor") or 0), "pepoch": db.meta(c, "psync_epoch")}
    h = json.dumps(f, ensure_ascii=False, sort_keys=True)
    if h == db.meta(c, "filter") and time.time() - int(db.meta(c, "filter_at") or 0) < FILTER_EVERY:
        return
    try:
        r = awx.api("/filter", dict(f, version=int(time.time() * 1000)), timeout=15)
        if not r.get("ok"): print(f"[filter] wxtest 没收：{r.get('error')}（画像还没同步到最新，同步后会重推）", file=sys.stderr)
        db.meta_set(c, "filter", h); db.meta_set(c, "filter_at", str(int(time.time())))
    except Exception as e:
        print(f"[filter] {e}", file=sys.stderr)

def heartbeat():
    """后台心跳没在跑就拉起来（hb.py 自己判断重复）。"""
    import os, subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        pid = int(open(os.path.join(here, "hb.pid")).read()); os.kill(pid, 0); return
    except (OSError, ValueError):
        pass
    subprocess.Popen([sys.executable, os.path.join(here, "hb.py")], cwd=here, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def run(c, cfg):
    """领一条任务：不回就当场交回，要回就交给 Muse 写，act.py 定结论后交回，发送由 wxtest 负责。"""
    db.meta_set(c, "tick_at", str(int(time.time())))
    heartbeat()
    push_filter(c, cfg)
    try:
        t = awx.api(f"/next_task?wait={WAIT}&ctx=30&sync={int(cfg['sync_min'] > 0)}", timeout=WAIT + 12).get("task")
    except Exception as e:
        print(f"[task] {e}", file=sys.stderr)
        if getattr(e, "code", 0) == 409 and "dup_name" in str(getattr(e, "msg", "")):   # wxtest 确认本机和别台重名，停派任务
            reach(c); a = dup_name(c, e)
            if a: print(json.dumps({"tasks": [a]}, ensure_ascii=False))
            time.sleep(DUP_WAIT); return
        a = reach(c, e)
        if a:
            print(json.dumps({"tasks": [a]}, ensure_ascii=False))
        time.sleep(3); return
    reach(c)
    if not t:
        return
    kind, info = t["kind"], t.get("info") or {}
    if kind == "sync":                         # wxtest 轮到本机对齐画像：几秒的事，不用叫醒 Muse
        r = psync.run(c, t["id"])
        if r is not None: push_filter(c, cfg)  # 对齐完马上推一次名单（带新的版本号，别台的旧名单就不收了）
        awx.task_done(t["id"], {"result": r} if r is not None else {"skip": "同步失败"}); return
    if kind == "update":                       # wxtest 发现本机 skill 落后：拉它自带的那版（本人 11-03 授权，不用叫醒 Muse）
        try: r = skillup.run(info.get("build", ""))
        except Exception as e: r = {"ok": False, "why": f"{type(e).__name__}: {e}"}
        print(f"[update] {r if not r.get('ok') else {k: r[k] for k in ('from', 'build')}}", file=sys.stderr)
        note, task = skillup.brief(r, agent=awx.AGENT) if r.get("ok") else (None, None)
        if task:                             # 先记下「说明没读」再交：交的时候被收回（409）也不丢叫醒（实测出现过：交的时候被收回，没叫醒）
            skillup.mark_unread(task); print(json.dumps({"tasks": [task]}, ensure_ascii=False))
        if note: say(cfg, note)              # 本人 11-09：升级了要告诉本人，也要告诉 AI 改了什么（随交结论带给 wxtest 发）
        r.pop("bak", None)
        try: awx.task_done(t["id"], {"result": r, **notes()} if r.get("ok") else {"skip": str(r.get("why", ""))[:200]})
        except Exception as e: print(f"[update] 交升级结果失败（文件已换好）：{e}", file=sys.stderr)
        return
    if kind in ("alert", "up"):
        a = login_task(c, cfg, t)
        if a:
            print(json.dumps({"tasks": [a]}, ensure_ascii=False))
        return
    if kind == "daily":                        # 日报数字 wxtest 自己报，这边只写群摘要（要读懂内容）
        d = digest_task(c, cfg)
        awx.task_done(t["id"], {"result": {"action": "none"}})
        if d:
            print(json.dumps({"tasks": [d]}, ensure_ascii=False))
        return
    if kind == "cmd":                          # 每个 agent 都照做（各自的库都要改），只有 reply 那份回话
        if db.cursor_get(c, "filehelper")[0] is None:     # 新装：只认这条及以后的命令，别把以前的旧命令再执行一遍
            db.cursor_set(c, "filehelper", t["from_id"] - 1)
        commands(c, cfg if info.get("reply") else dict(cfg, notify_to=""), bool(info.get("reply")))
        awx.task_done(t["id"], {"result": {"action": "cmd"}, **notes()}); return
    if kind == "label":                        # 好友聊满 50 条还没标签（wxtest 数的）：交给 Muse 读懂了打
        try: task = persona.label_task(c, cfg, t)
        except Exception as e:
            print(f"[label] {t['chat']}: {e}", file=sys.stderr); task = None
        if task: print(json.dumps({"tasks": [task]}, ensure_ascii=False))
        else: awx.task_done(t["id"], {"skip": "不用打了"})
        return
    if kind == "sns":                          # 有人在朋友圈评论/回复本人：按设置 sns 通知本人或交给 Muse 回
        task = sns.task(c, cfg, t, say)
        if task:
            print(json.dumps({"tasks": [task]}, ensure_ascii=False))
        else:
            awx.task_done(t["id"], {"result": {"action": "notify" if NOTES else "none"}, **notes()})
        return
    if kind == "owner":                        # 本人自聊发的话 = 最高指令：交给 Muse 照办，回话用 act 发回自聊
        task = owner_task(cfg, t)
        awx.task_done(t["id"], {"result": {"action": "owner" if task else "none"}})
        if task:
            print(json.dumps({"tasks": [task]}, ensure_ascii=False))
        return
    if kind != "msg":                          # 旧版 wxtest 派来的撤回/好友等通知：新版 wxtest 自己发，这里不用管
        awx.task_done(t["id"], {"result": {"action": "none"}}); return
    chat = t["chat"]
    recent = [m for m in t.get("messages") or []          # 本人号上的收款/退还回执（wxtest 自动收转账产生）不算本人说话
              if not ((m.get("pay") or {}).get("sub") in (3, 4) and m.get("sender_id") == cfg["me_id"])]
    name = chat_name(c, chat, [{"username": chat, "name": t.get("name")}])
    new = [m for m in recent if m["id"] >= t["from_id"]]
    if recent:
        db.cursor_set(c, chat, max(m["id"] for m in recent))
    task = None
    if new and not db.skip_chat(chat) and not db.meta(c, "paused"):
        task = _build(c, chat, name, cfg, recent, new, t.get("labels") or [], t.get("remark"), bool(t.get("called")))
    if not task:
        awx.task_done(t["id"], {"result": {"action": "none"}, **notes()} if NOTES else {"skip": "不用回"})
        return
    _remember(c, chat, recent, cfg, task.pop("voice", None), task.pop("_ask", ()), new)
    task["act"] += f" --task {t['id']}"
    print(json.dumps({"tasks": [task]}, ensure_ascii=False))

def main():
    c = db.conn(); db.init(c)
    cfg = db.config(c)
    if not (cfg["me_id"] and cfg["title"]):
        raise SystemExit("还没设置本人信息，先按 docs/install.md 问用户并跑 seed.py")
    if not awx.AGENT:
        try: awx.api("/heartbeat", {}, timeout=15)          # wxtest 收到没名字的心跳会发文件传输助手提醒本人
        except Exception as e: print(f"[tick] 告诉 wxtest 没名字没成：{e}", file=sys.stderr)
        raise SystemExit("settings.json 里没填 agent（这台的名字）：问用户起一个（几台各不相同），写进去再跑 seed.py；没名字不领任务")
    run(c, cfg)

if __name__ == "__main__":
    main()
