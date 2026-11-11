#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群商机雷达：本人问「群里最近有什么商机/谁在求购/有什么价格消息」时用，一条命令拿到素材，你再总结。
用法：python3 radar.py [小时数，默认24] [--all] [额外关键词…]
默认只挑像生意的消息（求购、报价、现货、调价、合作…，不分行业），--all 不筛；本人行业的词（如停产、检修、某个品名）作为额外关键词传进来。
输出按群分块的原话（时间、谁说的），本人自己发的不算。你据此给本人列：谁、在哪个群、要什么/卖什么/什么价、
值不值得跟进；只总结，不替本人回复或报价，旧价格只当参考。"""
import re, sys, time
import awx, db

SIGNAL = ("求购|采购|收购|找货|要货|求货|需要|谁有|哪里有|有货|现货|库存|清仓|甩卖|特价|处理|出售|供应|"
          "报价|询价|价格|价位|多少钱|行情|涨|跌|调价|合作|对接|代理|招聘|急")
EACH, PER, TOTAL = 200, 30, 15000   # 单条字数、每群最多条数（留最新的）、总字数

def main():
    args = sys.argv[1:]
    hours = next((float(a) for a in args if re.fullmatch(r"\d+(\.\d+)?", a)), 24)
    extra = [a for a in args if a != "--all" and not re.fullmatch(r"\d+(\.\d+)?", a)]
    pat = None if "--all" in args else re.compile("|".join([SIGNAL] + [re.escape(w) for w in extra]), re.I)
    c = db.conn(); db.init(c); cfg = db.config(c)
    since = time.time() - hours * 3600
    groups = [s for s in awx.api(f"/sessions?since={int(since)}").get("sessions", []) if s["username"].endswith("@chatroom")]
    out, size = [], 0
    for s in groups:
        msgs, before = [], 0
        while True:                       # 往前翻到时间窗口之前
            page = awx.messages(s["username"], 200, before)
            msgs = page + msgs
            if not page or page[0]["time"] < since or len(msgs) >= 2000: break
            before = page[0]["id"]
        lines = []
        for m in msgs:
            if m["time"] < since or m.get("sender_id") == cfg["me_id"]: continue
            if m.get("type") == 1: body = m.get("content") or ""
            elif m.get("type") == 49: body = f"[{m.get('app_name') or '卡片'}：{m.get('content') or ''} {m.get('des') or ''}]"
            else: continue
            if pat and not pat.search(body): continue
            lines.append(f"{time.strftime('%m-%d %H:%M', time.localtime(m['time']))} {m.get('sender') or '某人'}: {body[:EACH]}")
        if not lines: continue
        more, lines = max(0, len(lines) - PER), lines[-PER:]
        name = s.get("name") if s.get("name") not in (None, "", "[群聊]") else s["username"]
        block = f"== {name}（{len(lines) + more} 条" + (f"，只列最新 {PER} 条" if more else "") + "）\n" + "\n".join(lines)
        if size + len(block) > TOTAL:
            out.append(f"…还有群没列（超过 {TOTAL} 字），缩短小时数或加关键词再查"); break
        out.append(block); size += len(block)
    print("\n\n".join(out) if out else f"最近 {hours:g} 小时群里没有像生意的消息")

if __name__ == "__main__":
    main()
