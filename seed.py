#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""初始化：读用户自己填的 settings.json 写进库，认出本人微信 username，导入所有会话。
改了 settings.json 再跑一次即可更新设置（不会动画像）。
用法：cp settings.example.json settings.json，用户填好后 python3 seed.py（路径可用 AWX_SETTINGS 改）"""
import collections, json, os, time
import awx, db

KEYS = db.SETTING_KEYS

def guess_me_id(sessions):
    """本人 username：在多个私聊里都出现的发送者就是自己。"""
    cnt = collections.Counter()
    for s in [s for s in sessions if not db.is_group(s["username"]) and not db.skip_chat(s["username"])][:15]:
        cnt.update({m["sender_id"] for m in awx.messages(s["username"], 30)
                    if m.get("sender_id") and m["sender_id"] != s["username"]})
    top = cnt.most_common(1)
    return top[0][0] if top and top[0][1] >= 2 else ""

def load_settings():
    """读用户填的 settings.json；没改动的示例说明当“没填”（不写库，用默认）。"""
    path = os.environ.get("AWX_SETTINGS", "settings.json")
    if not os.path.exists(path):
        raise SystemExit(f"没有 {path}：先 cp settings.example.json {path}，让用户自己填")
    here = os.path.dirname(os.path.abspath(__file__))
    example = json.load(open(os.path.join(here, "settings.example.json"), encoding="utf-8"))
    raw = json.load(open(path, encoding="utf-8"))
    st = {k: (",".join(raw[k]) if isinstance(raw[k], list) else str(raw[k])).strip()
          for k in KEYS if k in raw and raw[k] != example.get(k)}
    if not (st.get("me") and st.get("title")):
        raise SystemExit("settings.json 里 me（名字）和 title（别人怎么称呼你）必须由用户填写")
    if not st.get("agent"):
        raise SystemExit("settings.json 里 agent（这台 agent 的名字，几台各不相同）必须由用户起，wxtest 不替你取")
    if st.get("rollout", "auto") not in ("auto", "draft"):
        raise SystemExit("rollout 只能是 auto 或 draft")
    if st.get("sns", "notify") not in ("notify", "reply", "off"):
        raise SystemExit("sns 只能是 notify、reply 或 off")
    if st.get("friend", "notify") not in ("accept", "notify", "off"):
        raise SystemExit("friend 只能是 accept、notify 或 off")
    if st.get("pat", "back") not in ("back", "off"):
        raise SystemExit("pat 只能是 back 或 off")
    if st.get("card", "notify") not in ("notify", "off"):
        raise SystemExit("card 只能是 notify 或 off")
    return st

def main():
    st = load_settings()
    c = db.conn(); db.init(c)
    sessions = awx.api("/sessions").get("sessions", [])
    st["me_id"] = st.get("me_id") or db.meta(c, "me_id") or guess_me_id(sessions)
    if not st["me_id"]:
        raise SystemExit("认不出本人微信 username，请让用户在 settings.json 填 me_id")
    if any(db.meta(c, k, None) != v for k, v in st.items() if k not in db.LOCAL_KEYS):
        db.meta_set(c, "psync_urgent", "1")     # 本人改了共用设置：马上同步给别的 agent
    for k, v in st.items():
        db.meta_set(c, k, v)
    c.execute("""INSERT OR IGNORE INTO profile(chat,alias,mode,risk,tone,lang,reply_when,updated)
                 VALUES('*default*','兜底','auto','mid','简洁、礼貌、口语，不要客套','中文','mention',?)""",
              (int(time.time()),))
    n = 0
    for s in sessions:
        u = s["username"]
        if not db.skip_chat(u):
            c.execute("INSERT OR IGNORE INTO profile(chat,alias,is_group,updated) VALUES(?,?,?,?)",
                      (u, s.get("name") or u, int(db.is_group(u)), int(time.time())))
            n += 1
    c.commit()
    cfg = db.config(c)
    print(f"本人 {cfg['me']}（{cfg['me_id']}），称呼 {cfg['title']}，备注 {cfg['sign']}，"
          f"模式 {cfg['rollout']}，通知发给 {cfg['notify_to'] or '（只打日志）'}，"
          f"掉线告警走 {db.meta(c, 'alert') or 'muse'}；导入 {n} 个会话。")
    if cfg["policy"]:
        print("风险表改过的格子：" + "，".join(f"{r}.{i}={a}" for (r, i), a in cfg["policy"].items()))
    print(f"红线：{cfg['redlines']}" if cfg["redlines"] else
          "红线还没填：AI 回复时没有红线。按 docs/settings.md 问用户，用户不写就按通用模板写一份给 TA 确认，再跑一次 seed.py。")

if __name__ == "__main__":
    main()
