#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回复规则：该不该回、整理这个人的资料、按「风险 × 意图」决定直发/草稿/不回。
所有人名、称呼、备注、套话、红线都来自 cfg（db.config，安装时问用户填的）。"""
from collections import Counter
import awx


# 保密、群聊公共场合、禁区、意图分类、输出格式这些固定规矩写在 SKILL.md「写回复的规矩」，不进任务包：
# 任务包里一大段命令口吻的说明会被 Muse 平台安全检查当成外部内容夹带指令拦下（11-05 对照试验：带说明 4/4 拦，不带 0/9）。

# 画像风险 × 消息意图 → 动作：auto 直发 / hold 回套话并通知 / draft 存草稿并通知 / notify 不回只通知
TABLE = {   # 默认风险表；本人可在 settings.json 的 policy 改单个格子
    "low":  {"chat": "auto",  "tech": "auto", "price": "hold", "business": "hold",  "sensitive": "notify"},
    "mid":  {"chat": "auto",  "tech": "auto", "price": "hold", "business": "draft", "sensitive": "notify"},
    "high": {"chat": "draft", "tech": "auto", "price": "hold", "business": "draft", "sensitive": "notify"},
}

def _owner(m, cfg):
    """本人亲自发的：本人号上、不是 wxtest 那台发的（消息不带 ai；wxtest 按微信库来源字段标，不看内容）。"""
    return m.get("sender_id") == cfg["me_id"] and not m.get("ai")

SPAM = 3                         # 同一句出现 3 次及以上算刷屏（是否叫醒 AI 由 wxtest 判；这里只在喂给 AI 时合成一条）

def _k(m):
    return m.get("sender_id"), m.get("content")

def squash(msgs):
    """喂给 AI 前把刷屏的同一句合成一条（留最后一次，注明刷了几次）。"""
    cnt = Counter(_k(m) for m in msgs if m.get("type") == 1)
    seen, out = set(), []
    for m in reversed(msgs):
        k = _k(m)
        if m.get("type") == 1 and cnt[k] >= SPAM:
            if k in seen:
                continue
            seen.add(k); m = dict(m, content=f"{m.get('content')}（同一句刷了 {cnt[k]} 次）")
        out.append(m)
    return out[::-1]

def paid(new, cfg):
    """别人给本人的钱：转账（待收款那条，wxtest 会自动收）和红包（本人自己在手机上领），返回说明列表。"""
    out = []
    for m in new:
        if m.get("sender_id") == cfg["me_id"]:
            continue
        if m.get("app") == 2000 and (m.get("pay") or {}).get("sub") == 1:
            out.append(f"转账{(m.get('pay') or {}).get('amount') or ''}，已自动收下")
        elif m.get("app") == 2001:
            out.append("红包，本人会自己领")
    return out

def videos(new, cfg):
    """对方这批新发的视频和视频号分享（消息 id）。"""
    return [m["id"] for m in new if m.get("sender_id") != cfg["me_id"] and (m.get("type") == 43 or (m.get("type") == 49 and m.get("finder")))]

def should_reply(prof, new, cfg, called):
    """不回的人/群、本人在聊、群里没叫到本人，wxtest 派任务前已经挡掉了（只有一套，这里不再判）；called 是 wxtest 算好的。"""
    if not new:
        return False, "无新消息"
    if new[-1].get("sender_id") == cfg["me_id"]:
        return False, "最后一条是自己发的"
    if all(m.get("type") == 10000 for m in new):
        return False, "只有拍一拍/系统消息"
    content = (1, 3, 50) if cfg.get("vision") else (1, 50)   # 能看图时图片也算正经内容；文件、转发的聊天记录、没接到的电话总算
    if not prof["is_group"] and paid(new, cfg):          # 别人单聊转钱给本人：一定要回（道谢+接个话题）
        return True, "ok"
    if not prof["is_group"] and videos(new, cfg):       # 单聊发视频：先看懂再聊两句（本人 11-03 定，群里不做）
        return True, "ok"
    if all(m.get("type") not in content and m.get("app") not in (6, 19) for m in new) and prof.get("risk") != "low" and not called:
        return False, "只有图片/表情"
    return True, "ok"

def person(prof, recent, cfg):
    """回复任务里「这个人」的资料（只写事实，不写命令）和最近 30 条上下文。千人千面就在这里。"""
    mine = [m.get("content") for m in recent if _owner(m, cfg) and m.get("type") == 1][-10:]   # 只学本人亲口说的
    p = {"name": prof.get("alias") or prof["chat"], "relation": prof.get("relation") or "未知",
         "tone": prof.get("tone") or "简洁、礼貌、口语，不要客套"}
    for k in ("address", "style", "topics", "sensitive", "rules"):
        if prof.get(k):
            p[k] = prof[k]
    if mine:
        p["owner_said"] = mine
    return p, awx.transcript(squash(recent[-30:]), cfg["me_id"], cfg["me"] or cfg["title"])

INTENTS = ("chat", "tech", "price", "business", "sensitive")
ALIAS = {"闲聊": "chat", "寒暄": "chat", "调侃": "chat", "技术": "tech", "专业": "tech", "常识": "tech",
         "价格": "price", "问价": "price", "报价": "price", "正事": "business", "业务": "business",
         "敏感": "sensitive", "看不懂": "unclear", "不清楚": "unclear", "拿不准": "unclear"}

def intent_of(x):
    """AI 偶尔把意图写成中文（「技术」「闲聊」）：换回英文代码；认不出的原样返回，由 decide 转告本人。"""
    s = str(x or "").strip().lower()
    return ALIAS.get(s, s)

def decide(prof, intent, reply, cfg):
    """返回 (动作, 正文)。动作：auto / draft / notify / skip。unclear 和认不出的意图转告本人，绝不悄悄丢掉。"""
    intent = intent_of(intent)
    if intent not in INTENTS:
        if intent != "unclear":
            print(f"[intent] 认不出的意图「{intent[:20]}」，没回，转告本人", flush=True)
        return "notify", ""
    risk = prof.get("risk") if prof.get("risk") in TABLE else "high"
    act = cfg.get("policy", {}).get((risk, intent)) or TABLE[risk][intent]   # 本人在 policy 里改过的格子优先
    if act == "hold":
        return ("auto" if cfg["rollout"] == "auto" else "draft"), cfg["hold"]
    if act == "notify":
        return "notify", ""
    if not reply:
        return "skip", ""
    if act == "auto" and (prof.get("mode") == "draft" or cfg["rollout"] != "auto"):
        act = "draft"
    return act, reply
