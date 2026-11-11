"""本人在自聊让你改说明（SKILL.md、docs/*.md）时用：推给 wxtest，本人给验证码才生效，生效后别的 agent 跟着换（本人 11-09 定）。
用法：python3 skillpush.py <相对路径> <改好的全文文件> [<相对路径> <文件> ...]   推上去（wxtest 发验证码给本人）
      python3 skillpush.py --code <6 位码>                                        本人在自聊告诉你码后交上去，对了才生效
      python3 skillpush.py --undo                                                 撤回最近一次（同样要码）
只收 SKILL.md、docs/*.md；代码、settings、rules 不走这里。本目录里的原文件别直接改（会被升级盖回去），先复制到别处改好再推；
生效后这里自动把本机那份换上。码 5 分钟过期，过期就重新推。"""
import os, sys, json, hashlib, urllib.error
import awx

HERE = os.path.dirname(os.path.abspath(__file__))
WAIT = os.path.join(HERE, ".skillpush.json")    # 挂着等码的那件（只记最后一件）

def sha(text):
    return hashlib.sha1((text or "").encode()).hexdigest()[:12]

def _read(p):
    try: return open(p, encoding="utf-8").read()
    except FileNotFoundError: return ""

def _call(path, body):
    try: return awx.api(path, body, timeout=60)
    except urllib.error.HTTPError as e:
        try: return {"error": json.loads(e.read() or b"{}").get("error") or f"HTTP {e.code}"}
        except ValueError: return {"error": f"HTTP {e.code}"}

def _write(rel, text):
    p = os.path.join(HERE, rel); tmp = p + ".push.tmp"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(tmp, "w", encoding="utf-8").write(text); os.replace(tmp, p)

def _asked(r, files):
    if r.get("error"): print(f"没推成：{r['error']}"); return 1
    if r.get("ok"): print("内容和现在一样，没改"); return 0
    json.dump({"id": r["wait_code"], "files": files}, open(WAIT, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"已推上去，还没生效：验证码已发给本人（5 分钟内有效）。本人可在控制面板输码，或在自聊告诉你，你再跑 python3 skillpush.py --code <码>")
    return 0

def main(a):
    if a[:1] == ["--code"] and len(a) == 2:
        try: w = json.load(open(WAIT, encoding="utf-8"))
        except (OSError, ValueError): print("没有挂着等码的改动，先推"); return 1
        r = _call("/skill/patch/confirm", {"id": w["id"], "code": a[1]})
        if not r.get("ok"): print(f"没生效：{r.get('error')}"); return 1
        for rel in r["files"]:
            if rel in w["files"]: _write(rel, w["files"][rel])
        os.remove(WAIT)
        print(f"已生效：{'、'.join(r['files'])}，别的 agent 会跟着换")
        return 0
    if a == ["--undo"]: return _asked(_call("/skill/patch/undo", {}), {})
    if not a or len(a) % 2: print(__doc__); return 2
    files, texts = {}, {}
    for rel, src in zip(a[::2], a[1::2]):
        try: texts[rel] = open(src, encoding="utf-8").read()
        except OSError as e: print(f"读不了 {src}：{e}"); return 1
        files[rel] = {"old": sha(_read(os.path.join(HERE, rel))), "text": texts[rel]}
    return _asked(_call("/skill/patch", {"files": files}), texts)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
