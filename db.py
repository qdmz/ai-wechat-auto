#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQLite 存取：画像、游标、草稿。所有东西一个库文件，不存聊天记录。"""
import sys, json, os, sqlite3, time

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.environ.get("AWX_DB", "store.db"))   # 相对路径按本目录算，不随调用者的当前目录变（否则会在别处建空库）
FIELDS = ("relation", "risk", "mode", "tone", "style", "address",
          "topics", "sensitive", "lang", "reply_when")
RULES = os.environ.get("AWX_RULES") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")
RULES_MAX = 2000
# settings.json 里能填的项（seed.py 写进 meta）；新增设置要同时加进这里、settings.example.json、docs/settings.md、config()
SETTING_KEYS = ("me", "title", "names", "sign", "voice_sign", "hold", "redlines", "notify_to", "rollout", "me_id", "draft_wait", "alert", "agent", "vision", "recall",
                "handoff_min", "merge_sec", "per_hour", "report_hour", "bot_mute_min", "pending_max_min", "sns", "friend", "card", "digest", "label_top", "label_rel", "label_mine", "pat", "like", "like_every", "catchup_min", "voice_reply", "voice_by", "voice_roles", "wake", "sync_min", "policy")
LOCAL_KEYS = ("agent", "wake", "vision")    # 各机自己的（名字、唤醒命令、模型能不能看图），不同步；其余几台共用，走画像同步
SYSTEM = ("newsapp", "filehelper", "qqmail", "fmessage", "medianote", "floatbottle", "weixin",
          "notifymessage", "brandsessionholder", "*default*")

def conn():
    c = sqlite3.connect(PATH)
    c.row_factory = sqlite3.Row
    return c

def init(c):
    with open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8") as f:
        c.executescript(f.read())
    if "ts" not in {r["name"] for r in c.execute("PRAGMA table_info(cursor)")}:
        c.execute("ALTER TABLE cursor ADD COLUMN ts INTEGER")
    c.execute("DROP TABLE IF EXISTS pending")      # 发不出去的重试已交给 wxtest
    have = {r["name"] for r in c.execute("PRAGMA table_info(profile)")}
    for col in ("risk", "style", "topics", "sensitive", "locked", "profiled_at"):
        if col not in have:   # 旧库补列
            c.execute(f"ALTER TABLE profile ADD COLUMN {col} {'INTEGER' if col == 'profiled_at' else 'TEXT'}")
    for r in c.execute("SELECT chat, rules FROM profile WHERE rules IS NOT NULL AND rules != ''").fetchall():
        p = rule_path(r["chat"])     # 旧库里的专属规则搬到规则文件，库里不再存
        if not os.path.exists(p):
            os.makedirs(RULES, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(r["rules"].strip() + "\n")
        c.execute("UPDATE profile SET rules=NULL WHERE chat=?", (r["chat"],))
    c.commit()

def rule_path(name):
    return os.path.join(RULES, name.replace("/", "_").replace("\\", "_") + ".md")

def _read(name):
    with open(rule_path(name), encoding="utf-8") as f:
        return f.read().strip()

def rules(chat, alias=None, labels=()):
    """这个人的专属规则文件 rules/<微信号>.md，没有就找 rules/<备注名>.md，都没有用 rules/_default.md；再接上对所有人都适用的 rules/_all.md；
    本人在微信里给 TA 打了标签的，再接上 rules/标签-<标签名>.md（同一类客户共用，专属规则优先）。
    回谁只读谁的那一份；用户自己写，自动画像不碰。"""
    own = next((_read(n) for n in (chat, alias, "_default") if n and os.path.isfile(rule_path(n))), "")
    parts = [own] if own else []
    if os.path.isfile(rule_path("_all")) and (common := _read("_all")):   # 本人的自定义规则：对所有人都适用，排在专属规则后面（冲突以专属为准）
        parts.append("（本人对所有人的通用规则，和上面冲突时以上面为准）\n" + common)
    for lb in labels:
        if os.path.isfile(rule_path("标签-" + lb)):
            parts.append(f"（微信标签「{lb}」这类客户的共用规则，和上面冲突时以上面为准）\n" + _read("标签-" + lb))
    return "\n\n".join(parts)[:RULES_MAX]

def skip_chat(chat):
    return chat.startswith("gh_") or chat in SYSTEM or chat.endswith("@placeholder_foldgroup")

def is_group(chat):
    return chat.endswith("@chatroom") or chat.endswith("@openim")

def meta(c, k, default=""):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default   # 存了空串就是空（如 notify_to 设空=不发通知）

def _list(v): return [n.strip() for n in v.replace("，", ",").split(",") if n.strip()]

def _tree(v):
    """label_top「大类=关系,关系;大类=…」→ {大类: [关系…]}；只写大类（「工作,亲友」）也行。"""
    v, out = v.replace("；", ";"), {}
    for part in (v.split(";") if ";" in v or "=" in v else _list(v)):
        top, _, kids = part.partition("=")
        if top.strip(): out[top.strip()] = _list(kids)
    return out

def label_parent(cfg, rel):
    """关系标签属于哪个大类（没有返回空串）。"""
    return next((t for t, kids in cfg["label_top"].items() if rel in kids), "")

def labels_on(cfg):
    """本人设了关系标签（大类或关系）= AI 可以给人打标签。"""
    return bool(cfg["label_top"] or cfg["label_rel"])

def config(c):
    """安装时问用户得来的设置，全在 meta 里；没填的按称呼推出默认值。代码里不写死任何人名。"""
    g = lambda k, dft="": meta(c, k) or dft          # 空串也当没填，用默认
    off = lambda v: "" if v.strip() in ("无", "不加", "none") else v   # 填「无」= 不加
    title = g("title") or g("me")
    num = lambda k, dft: int(g(k)) if g(k).isdigit() else dft
    names = [n.strip() for n in g("names").replace("，", ",").split(",") if n.strip()]
    tt = lambda v: v.replace("<称呼>", title)        # 控制面板里写的「<称呼>」换成本人称呼
    return {
        "me": g("me"), "me_id": g("me_id"), "title": title,
        "names": sorted(set(names + [g("me"), title]) - {""}),     # 群里算“叫到我”的名字
        "sign": off(tt(g("sign", f"【{title}的私人助理·自动回复】"))),
        "voice_sign": off(tt(g("voice_sign"))),   # 没填 = 不念
        "hold": tt(g("hold", f"这事我做不了主，等{title}本人回你。")),
        "redlines": g("redlines"),                       # 用户自己填，空 = 不加这一行
        "notify_to": meta(c, "notify_to", "filehelper"),   # 只有它允许设成空串（=只打日志）
        "rollout": g("rollout", "auto"),
        # 用户可调的时长/数量（含义和场景见 docs/settings.md），没填或填错用默认
        "draft_wait": num("draft_wait", 3), "handoff_min": num("handoff_min", 5),
        "merge_sec": num("merge_sec", 8), "per_hour": num("per_hour", 40), "report_hour": num("report_hour", 21),
        "bot_mute_min": num("bot_mute_min", 60), "pending_max_min": num("pending_max_min", 5),
        "vision": g("vision", "off").lower() in ("on", "1", "true", "yes"),   # Muse 的模型能看图才开
        "recall": g("recall", "on").lower() not in ("off", "0", "false", "no"),        # 别人撤回时把原文告诉本人（默认开）
        "sns": g("sns", "notify") if g("sns", "notify") in ("notify", "reply", "off") else "notify",   # 朋友圈评论怎么处理
        "friend": g("friend", "notify") if g("friend", "notify") in ("accept", "notify", "off") else "notify",  # 有人加好友：自动通过/告诉本人/不管
        "wake": g("wake"),              # 值班循环领到任务后立刻跑的命令（叫醒 Muse），空 = 靠 hook 定时看
        "sync_min": num("sync_min", 30),  # 多台 agent 时，wxtest 每隔多少分钟（空闲时）给本机下放一次画像同步；0 = 不同步
        "digest": _list(g("digest")),   # 每天日报时要摘要的群（群名里的字）
        "label_top": _tree(g("label_top")),    # 第一层大类 {大类: [下面的关系]}，聊满 50 条的人至少打一个大类；本人自己定
        "label_rel": list(dict.fromkeys(_list(g("label_rel")) + [k for v in _tree(g("label_top")).values() for k in v])),   # 第二层关系（每人一个，AI 只从这里选）；空 = AI 不给人打标签
        "label_mine": _list(g("label_mine")),  # 只由本人打的标签（AI 不碰）；本人自己填
        "card": g("card", "notify") if g("card", "notify") in ("notify", "off") else "notify",        # 收到名片告不告诉本人
        "pat": "off" if g("pat", "back") == "off" else "back",       # 别人单聊拍了拍本人：回拍
        "like": [] if g("like") == "off" else _list(g("like")),   # 给打了这些标签的人的新朋友圈点赞（off = 不点）
        "like_every": num("like_every", 30),                          # 多少分钟刷一次朋友圈找新动态
        "voice_reply": g("voice_reply", "on").lower() not in ("off", "0", "false", "no"),   # 对方发语音就回语音（默认开）
        "voice_by": _list(g("voice_by")),                     # 标签=声音，没列的用本人声音 default；本人自己填
        "voice_roles": _list(g("voice_roles")),      # 非本人声音=人名:昵称，结尾自报「私人助手昵称」；本人自己登记
        "catchup_min": num("catchup_min", 30),                        # 晚到多少分钟的消息算补收（不回、只汇总通知本人）
        "policy": _policy(g("policy")),                              # 改风险表的格子「high.chat=auto,mid.business=hold」，没写的用默认表
    }

POLICY_ACTS = ("auto", "draft", "hold", "notify")
POLICY_INTENTS = ("chat", "tech", "price", "business", "sensitive")

def _policy(v):
    """「风险.意图=动作」逗号分隔 → {(风险, 意图): 动作}；写错的格子跳过并打日志。"""
    out = {}
    for item in _list(v):
        k, _, act = item.partition("=")
        risk, _, intent = k.strip().partition(".")
        if risk in ("low", "mid", "high") and intent in POLICY_INTENTS and act.strip() in POLICY_ACTS:
            out[(risk, intent)] = act.strip()
        else:
            print(f"[settings] policy 这一格写错了，跳过：{item[:40]}", file=sys.stderr)
    return out

def meta_set(c, k, v):
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    c.commit()

def profile(c, chat):
    """合并 *default* 与该 chat 的行，空字段用默认兜底。"""
    d = c.execute("SELECT * FROM profile WHERE chat='*default*'").fetchone()
    hit = c.execute("SELECT * FROM profile WHERE chat=?", (chat,)).fetchone()
    base, own = dict(d) if d else {}, dict(hit) if hit else {}
    prof = {k: own.get(k) or base.get(k) for k in FIELDS}
    prof.update(chat=chat, alias=own.get("alias"), is_group=is_group(chat),
                profiled_at=own.get("profiled_at") or 0, rules=rules(chat, own.get("alias")))
    prof["mode"] = prof["mode"] or "auto"
    prof["risk"] = own.get("risk") or "mid"         # 没画像按 mid：闲聊照样按以往聊天风格直回（自报 AI），正事/敏感仍转本人
    if prof["is_group"]:
        prof["reply_when"] = prof["reply_when"] or "mention"
    return prof

def save_profile(c, chat, alias, data):
    """自动画像写库：locked 里的字段不覆盖。"""
    row = c.execute("SELECT locked FROM profile WHERE chat=?", (chat,)).fetchone()
    locked = {x.strip() for x in ((row and row["locked"]) or "").split(",") if x.strip()}
    now = int(time.time())
    c.execute("INSERT OR IGNORE INTO profile(chat,alias,is_group,updated) VALUES(?,?,?,?)",
              (chat, alias, int(is_group(chat)), now))
    vals = {k: str(v)[:300] for k, v in data.items() if k in FIELDS and k not in locked and v not in (None, "")}
    sets = "".join(f",{k}=?" for k in vals)
    c.execute(f"UPDATE profile SET profiled_at=?,updated=?{sets} WHERE chat=?", (now, now, *vals.values(), chat))
    c.commit()

def set_rule(c, chat, alias, field, value):
    """文件传输助手命令改规则：写一个 profile 字段并锁住（locked），自动画像不会覆盖。"""
    now = int(time.time())
    c.execute("INSERT OR IGNORE INTO profile(chat,alias,is_group,updated) VALUES(?,?,?,?)",
              (chat, alias, int(is_group(chat)), now))
    row = c.execute("SELECT locked FROM profile WHERE chat=?", (chat,)).fetchone()
    locked = {x.strip() for x in ((row and row["locked"]) or "").split(",") if x.strip()} | {field}
    c.execute(f"UPDATE profile SET {field}=?, locked=?, updated=? WHERE chat=?",
              (value, ",".join(sorted(locked)), now, chat))
    c.commit()
    meta_set(c, "psync_urgent", "1")   # 本人亲口改的：多台 agent 时马上同步给别台（watch.py 看到会请 wxtest 安排）

def unset_rule(c, chat, field):
    """解封：字段清空回到默认，并解锁让自动画像能再写。"""
    row = c.execute("SELECT locked FROM profile WHERE chat=?", (chat,)).fetchone()
    if not row:
        return
    locked = {x.strip() for x in (row["locked"] or "").split(",") if x.strip()} - {field}
    c.execute(f"UPDATE profile SET {field}=NULL, locked=?, updated=? WHERE chat=?",
              (",".join(sorted(locked)), int(time.time()), chat))
    c.commit()
    meta_set(c, "psync_urgent", "1")

def cursor_get(c, chat):
    """返回 (last_id, ts)，没有记录就是 (None, None)。"""
    r = c.execute("SELECT last_id, ts FROM cursor WHERE chat=?", (chat,)).fetchone()
    return (r["last_id"], r["ts"]) if r else (None, None)

def cursor_set(c, chat, last_id=None, ts=None):
    c.execute("INSERT INTO cursor(chat,last_id,ts) VALUES(?,?,?) ON CONFLICT(chat) DO UPDATE SET "
              "last_id=COALESCE(excluded.last_id,last_id), ts=COALESCE(excluded.ts,ts)", (chat, last_id, ts))
    c.commit()

def meta_json(c, k, default):
    try:
        return json.loads(meta(c, k)) if meta(c, k) else default
    except ValueError:
        return default

def add_draft(c, chat, alias, text):
    c.execute("INSERT INTO draft(chat,alias,draft,ts) VALUES(?,?,?,?)",
              (chat, alias, text, int(time.time())))
    c.commit()
