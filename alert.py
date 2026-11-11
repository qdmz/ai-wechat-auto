#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""掉线告警：微信掉线时微信里发不出通知，改走用户自己选的渠道。
渠道写在 settings.json 的 alert 里，可以填多个（逗号分隔或写成列表）：
  serverchan:<Server酱 SendKey>  推到微信服务号（免费版每天 5 条）
  bark:<Bark 推送地址，如 https://api.day.app/你的key>
  mail:<收件邮箱>        发件 SMTP 用环境变量 SMTP_HOST、SMTP_PORT（默认 465）、SMTP_USER、SMTP_PASS
  webhook:<网址>         POST JSON {"title","text","url"}，ntfy、自建机器人中转等都能接
  muse                   让 Muse 用它自己和本人的聊天渠道转告
不填默认 muse。
用法：python3 alert.py test   发一条测试，看各渠道通不通"""
import json, os, re, smtplib, sys, urllib.parse, urllib.request
from email.mime.text import MIMEText
from email.header import Header

def channels(spec):
    return [x.strip() for x in (spec or "muse").replace("，", ",").replace("\n", ",").split(",") if x.strip()]

def _post(url, body):
    req = urllib.request.Request(url, json.dumps(body, ensure_ascii=False).encode(),
                                 {"Content-Type": "application/json; charset=utf-8"})
    urllib.request.urlopen(req, timeout=15).read()

def _sc(key, title, text):
    """Server酱：SCT 开头走 sctapi；Server酱³ 的 sctp<数字>t… 走自己的子域名。回的 code 不是 0 算失败。"""
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,80}", key): raise ValueError("SendKey 格式不对")
    m = re.match(r"sctp(\d+)t", key)
    url = f"https://{m.group(1)}.push.ft07.com/send/{key}.send" if m else f"https://sctapi.ftqq.com/{key}.send"
    data = urllib.parse.urlencode({"title": title[:32], "desp": text}).encode()
    r = json.loads(urllib.request.urlopen(urllib.request.Request(url, data), timeout=15).read(4096) or b"{}")
    if r.get("code") not in (0, None): raise RuntimeError(f"Server酱回 {r.get('code')}：{str(r.get('message') or '')[:40]}")

def _mail(to, title, text):
    miss = [k for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS") if not os.environ.get(k)]
    if miss:
        raise RuntimeError("缺少环境变量 " + "、".join(miss))
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT") or 465)
    user, pw = os.environ["SMTP_USER"], os.environ["SMTP_PASS"]
    m = MIMEText(text, "plain", "utf-8")
    m["Subject"], m["From"], m["To"] = Header(title, "utf-8"), user, to
    s = smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)
    try:
        if port != 465:
            s.starttls()
        s.login(user, pw); s.sendmail(user, [to], m.as_string())
    finally:
        s.quit()

def send(spec, title, text, url=""):
    """发到所有外部渠道。返回 (是否要 Muse 转告, 失败列表)；外部渠道全失败也交给 Muse 兜底。"""
    chs, ok, bad = channels(spec), 0, []
    for ch in chs:
        kind, _, arg = ch.partition(":")
        try:
            if kind == "serverchan":
                _sc(arg, title, text + (f"\n\n{url}" if url else ""))
            elif kind == "bark":
                _post(arg.rstrip("/"), {"title": title, "body": text, "url": url, "group": "微信助理"})
            elif kind == "mail":
                _mail(arg, title, text + (f"\n\n{url}" if url else ""))
            elif kind == "webhook":
                _post(arg, {"title": title, "text": text, "url": url})
            elif kind == "muse":
                continue
            else:
                raise ValueError("不认识的渠道")
            ok += 1
        except Exception as e:
            bad.append(f"{kind}: {e}")
            print(f"[alert] {kind} 失败：{e}", file=sys.stderr)
    return ("muse" in chs or not ok), bad

if __name__ == "__main__" and sys.argv[1:] == ["test"]:
    import db
    c = db.conn(); db.init(c)
    muse, bad = send(db.meta(c, "alert"), "微信助理测试", "这是一条测试告警，收到说明渠道通了。")
    print("失败：" + "；".join(bad) if bad else "外部渠道都发出去了", "（还需要 Muse 转告）" if muse else "")
