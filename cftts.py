#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令给语音加 Cloudflare 兜底（Fish 接口不通时顶上；免费额度够日常用）：
在用户的 Cloudflare 账号部署 Worker「wxtts」（Workers AI MeloTTS 中文），随机生成密钥，再告诉 wxtest（POST /tts）。
用法：CF_TOKEN=<用户给的 API 令牌> python3 cftts.py      令牌怎么拿见 docs/install.md「语音」；令牌只用这一次，别存。"""
import json, os, secrets, sys, urllib.request, urllib.error
import awx

API = "https://api.cloudflare.com/client/v4"
TOKEN = os.environ.get("CF_TOKEN") or sys.exit("先设环境变量 CF_TOKEN（用户给的 Cloudflare API 令牌）")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "tts-worker.js")

def cf(method, path, body=None, ctype="application/json"):
    req = urllib.request.Request(API + path, body, {"Authorization": "Bearer " + TOKEN, "Content-Type": ctype}, method=method)
    try: r = json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e: r = json.load(e)
    if not r.get("success"): sys.exit(f"Cloudflare 报错（{path}）：{r.get('errors')}——多半是令牌权限不够，按 install.md 重建令牌")
    return r["result"]

acct = cf("GET", "/accounts")[0]["id"]
key = secrets.token_urlsafe(24)
meta = {"main_module": "worker.js", "compatibility_date": "2024-09-01",
        "bindings": [{"type": "ai", "name": "AI"}, {"type": "secret_text", "name": "KEY", "text": key}]}
b = "----wxtts" + secrets.token_hex(8)
body = (f'--{b}\r\nContent-Disposition: form-data; name="metadata"\r\nContent-Type: application/json\r\n\r\n{json.dumps(meta)}\r\n'
        f'--{b}\r\nContent-Disposition: form-data; name="worker.js"; filename="worker.js"\r\nContent-Type: application/javascript+module\r\n\r\n'
        + open(SRC, encoding="utf-8").read() + f"\r\n--{b}--\r\n").encode()
cf("PUT", f"/accounts/{acct}/workers/scripts/wxtts", body, "multipart/form-data; boundary=" + b)
cf("POST", f"/accounts/{acct}/workers/scripts/wxtts/subdomain", json.dumps({"enabled": True}).encode())
sub = cf("GET", f"/accounts/{acct}/workers/subdomain")["subdomain"]
url = f"https://wxtts.{sub}.workers.dev"
print(json.dumps(awx.api("/tts", {"url": url, "key": key}), ensure_ascii=False))
print("好了：", url, "（新 Worker 的地址要等一两分钟才通；之后让本人在文件传输助手发「试听」听不到也不影响，Fish 正常时不会用它）")
