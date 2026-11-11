#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本人让你代发一条消息时用，一条命令搞定：按名字找会话 → 发送 → 等结果。别自己一步步查状态、找人、翻消息。
用法：python3 say.py <备注/昵称里的几个字 或 微信号> [--group]，要发的内容从标准输入给（heredoc，可多行）。
备注由wxtest自动加。发群要加 --group。"""
import json, sys
import awx, db

def unwrap(text):
    """误把交给 act 的 JSON（{"reply": ...}）整段喂进来：只取 reply，免得把花括号和 \\n 原样发出去。"""
    if text.startswith("{"):
        try: d = json.loads(text)
        except ValueError: return text
        if isinstance(d, dict) and isinstance(d.get("reply"), str) and d["reply"].strip(): return d["reply"].strip()
    return text

def main():
    args = [a for a in sys.argv[1:] if a != "--group"]
    if len(args) != 1:
        raise SystemExit(__doc__)
    text = unwrap(sys.stdin.read().strip())
    if not text:
        raise SystemExit("内容是空的")
    token = args[0]
    c = db.conn(); db.init(c)
    me = db.config(c)["me_id"]
    ss = awx.api("/sessions").get("sessions", [])
    hit = [s for s in ss if s["username"] == token] or ([{"username": me, "name": "本人自聊"}] if token == me else
                                                        [s for s in ss if token in (s.get("name") or "")])
    if len(hit) != 1:
        names = "、".join((s.get("name") or s["username"]) for s in hit[:5])
        raise SystemExit(f"「{token}」对上了 {len(hit)} 个" + (f"：{names}" if names else "") + "，说全一点或给微信号")
    chat, name = hit[0]["username"], hit[0].get("name") or hit[0]["username"]
    if db.is_group(chat) and "--group" not in sys.argv:
        raise SystemExit(f"「{name}」是群，确认要发就加 --group")
    if chat == me and awx.AGENT:                       # 回本人自聊：带上是哪台 agent 回的（代码加，不靠 AI 记得）
        text = f"〔{awx.AGENT}〕{text}"
    ok, st, draft = awx.send(chat, text, me, db.is_group(chat))
    print(f"{'已发出' if ok else '没发出'}：{name}（{st}）" + (f"，输入框里有别人的字：{draft}" if draft else ""))
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
