#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动画像：把某人的历史聊天交给 Muse 总结成画像，结果用 act.py 写回。只存结论，聊天用完即丢。
用法：python3 persona.py next [N]   # 输出最多 N 个（默认 5）待画像任务的 JSON（最近 30 天活跃的前 100 个私聊里没画过或超 7 天的；群不画像）
Muse 对每个任务写出 {"profile": {...}}，再 echo 这段 JSON | python3 act.py <chat>。"""
import json, sys, time
import awx, db

DAYS, TOP, HISTORY, STALE = 30, 100, 200, 7 * 86400

# 画像字段、打标签、写备注怎么写在 SKILL.md「写回复的规矩」，任务包里只放候选标签等事实（命令口吻的说明会被平台安全检查拦）。

LABEL_EVERY = 3 * 86400    # 同一个没标签/没备注的人最多 3 天问 AI 一次补不补

def label_opts(cfg, existing, must=False):
    """还没标签的人：三层候选（大类、关系、补充）。must=True 是聊满 50 条的打标签任务，大类必须给。"""
    rel, mine, top = cfg["label_rel"], set(cfg["label_mine"]), cfg["label_top"]
    o = {"must": must, "extra": [x for x in existing if x not in rel and x not in mine and x not in top][:40]}
    if top: o["top"] = dict(top)
    if rel: o["rel"] = rel
    if mine: o["owner_only"] = sorted(mine)
    return o

def label_task(c, cfg, t):
    """wxtest 派来的 kind=label：这个好友聊满 50 条还没标签，读历史给 TA 打（至少一个大类）。不用打了返回 None。"""
    chat = t["chat"]
    if not db.labels_on(cfg) or db.skip_chat(chat) or db.is_group(chat): return None
    if awx.api("/labels?user=" + awx.quote(chat)).get("labels"): return None     # 派出后本人自己打了
    h = history(c, chat)
    if not h: return None
    return {"kind": "打标签", "chat": chat, "alias": (t.get("info") or {}).get("name") or chat, "title": cfg["title"],
            "ask_label": label_opts(cfg, awx.all_labels(), must=True),
            "history": h, "act": f"python3 act.py {chat} --task {t['id']}"}

def stale(prof):
    return time.time() - (prof.get("profiled_at") or 0) > STALE

def history(c, chat):
    """画像用的历史对话文本；聊得太少（文字少于 5 条）返回空。"""
    msgs = awx.messages(chat, HISTORY)
    if sum(m.get("type") == 1 for m in msgs) < 5:
        return ""
    cfg = db.config(c)
    return awx.transcript(msgs, cfg["me_id"], cfg["me"] or cfg["title"])[-12000:]

def task(c, chat, alias):
    h = history(c, chat)
    if not h:                                # 画不出来：记下时间，按兜底（高风险）处理
        db.save_profile(c, chat, alias, {})
        return None
    return {"kind": "画像", "chat": chat, "alias": alias, "title": db.config(c)["title"],
            "history": h}

def main():
    if sys.argv[1:2] != ["next"]:
        sys.exit(__doc__)
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    c = db.conn(); db.init(c)
    since, out = time.time() - DAYS * 86400, []
    for s in [s for s in awx.api("/sessions").get("sessions", [])
              if s["ts"] >= since and not db.skip_chat(s["username"]) and not db.is_group(s["username"])][:TOP]:
        if len(out) >= n:
            break
        if stale(db.profile(c, s["username"])):
            t = task(c, s["username"], s.get("name"))
            if t and db.config(c)["label_rel"] and not awx.api("/labels?user=" + awx.quote(s["username"])).get("labels"):
                t["ask_label"] = label_opts(db.config(c), awx.all_labels())
            if t:
                out.append(t)
    print(json.dumps({"tasks": out}, ensure_ascii=False))

if __name__ == "__main__":
    main()
