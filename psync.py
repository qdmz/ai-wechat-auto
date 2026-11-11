#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多 agent 画像同步：干活只读本地 store.db 和 rules/；wxtest 逐台下放 kind=sync 任务，领到才对齐（tick.py 调 run()）。
- 每条（画像 p:<微信号>、规则文件 r:<文件名>、共用设置 s:settings）记着上次对齐的版本号和内容指纹（本地表 psync）；指纹变了 = 本机改过，推上去带 base。
- wxtest 合并后把游标之后的所有改动（含刚推的合并结果）发回来，照写本地：别台改的进来，本机改的拿到合并后的样子。
- 共用设置：meta 里 settings.json 的项，除 agent/wake/vision（各机自留）；别台改的写进本机 meta 和 settings.json。
- 本地删了规则文件 / 画像行 → 推删除；别台删的跟着删。
- 即时同步：本机有任何改动（AI 学到的、本人改的、rules/ 文件、删除）watch.py 发现后请 wxtest 马上轮一次，wxtest 收下后别台全部马上对齐；掉线回来的先对齐再干活。
- wxtest 的库丢了（epoch 变了）：本地全量回传，不删本地。新 agent 第一次（或本地库丢了）：先拉 wxtest 已有的、以那边为准，再推那边没有的。
命令：python3 psync.py              请 wxtest 马上安排一次同步（本人刚让你改了谁的规则时用）
      python3 psync.py history 名字   看这人画像最近几版（谁改的）；history r:文件名.md 看规则文件
      python3 psync.py rollback 名字 [版本]   退回上一版（或指定版），所有 agent 马上跟着改
      python3 psync.py join 这台的名字   已有别的 agent 在用同一个微信时，新装这台用它：从 wxtest 拿共用设置写 settings.json，只用起名字，不用再问一遍"""
import hashlib, json, os, sys, time
import db

BATCH = 300                # 一次最多推几条（新 agent 全量回传时分批）
SCHEMA = 1                 # 画像格式号：加/删画像字段时 +1
HASH_SKIP = ("updated", "profiled_at", "chat")   # 只是时间戳，不算改了内容
SETTINGS = "s:settings"

def _init(c):
    c.execute("CREATE TABLE IF NOT EXISTS psync(key TEXT PRIMARY KEY, ver INTEGER, hash TEXT)")

def _h(key, data):
    if key.startswith("r:"): return hashlib.sha1((data.get("text") or "").encode()).hexdigest()
    return hashlib.sha1(json.dumps({k: v for k, v in data.items() if k not in HASH_SKIP}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def _rule_files():
    if not os.path.isdir(db.RULES): return []
    return [f for f in sorted(os.listdir(db.RULES)) if f.endswith(".md") and not f.startswith("_example") and os.path.isfile(os.path.join(db.RULES, f))]

def local(c):
    """本地所有画像行和规则文件：{key: data}。"""
    out = {"p:" + r["chat"]: {k: r[k] for k in r.keys() if k not in ("rules", "updated")} for r in c.execute("SELECT * FROM profile")}
    for f in _rule_files():
        out["r:" + f] = {"text": open(os.path.join(db.RULES, f), encoding="utf-8").read()[:4000]}
    st = {k: v for k in db.SETTING_KEYS if k not in db.LOCAL_KEYS and (v := db.meta(c, k, None)) is not None}
    if st: out[SETTINGS] = st
    return out

def _now_hash(c, key):
    """这一条本地现在的指纹（不存在是 DEL）：同步途中本机又改了它就能看出来。"""
    if key.startswith("r:"):
        p = os.path.join(db.RULES, os.path.basename(key[2:]))
        if not os.path.isfile(p): return "DEL"
        return _h(key, {"text": open(p, encoding="utf-8").read()[:4000]})
    if key == SETTINGS:
        st = {k: v for k in db.SETTING_KEYS if k not in db.LOCAL_KEYS and (v := db.meta(c, k, None)) is not None}
        return _h(key, st) if st else "DEL"
    r = c.execute("SELECT * FROM profile WHERE chat=?", (key[2:],)).fetchone()
    return _h(key, {k: r[k] for k in r.keys() if k not in ("rules", "updated")}) if r else "DEL"

def _settings_file():
    return os.environ.get("AWX_SETTINGS") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

def _apply_settings(c, data, log, who="别台"):
    """别台改的共用设置：写进本机 meta，同时改 settings.json（不然本机下次跑 seed.py 会把旧值又推回去）。各机自留的项不动。"""
    st = {k: str(v) for k, v in data.items() if k in db.SETTING_KEYS and k not in db.LOCAL_KEYS}
    for k, v in st.items(): db.meta_set(c, k, v)
    f = _settings_file()
    if not os.path.exists(f): return
    try:
        raw = json.load(open(f, encoding="utf-8"))
        norm = lambda x: ",".join(x) if isinstance(x, list) else str(x)
        ch = {k: v for k, v in st.items() if k not in raw or norm(raw[k]).strip() != v}
        if not ch: return
        for k, v in ch.items(): raw[k] = [x for x in v.split(",") if x] if isinstance(raw.get(k), list) else v
        tmp = f + ".sync"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh: json.dump(raw, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, f)
        log(f"[psync] {who}改了设置 {','.join(sorted(ch))}，本机 settings.json 已跟着改")
    except (OSError, ValueError) as e:
        log(f"[psync] 改 settings.json 失败（meta 已改，跑 seed.py 前先手动对一下）：{e}")

def set_mine(c, key, val, log):
    """本人在文件传输助手改的共用设置：写 meta 和 settings.json，马上同步给别台（wxtest 下一轮 /filter 拿到）。"""
    _apply_settings(c, {key: val}, log, who="本人")
    db.meta_set(c, "psync_urgent", "1")

def _extra(c, it):
    """拉下来写进本机后，本机还有没有那边空着的值。"""
    if it.get("deleted"): return False
    mine, theirs = local(c).get(it["key"]) or {}, it.get("data") or {}
    return any(v not in (None, "") and theirs.get(k) in (None, "") for k, v in mine.items() if k not in HASH_SKIP and k != "locked")

def dirty(c):
    """本机改过、还没推的：[{key, base, data} 或 {key, base, deleted}]。"""
    _init(c)
    seen = {r["key"]: (r["ver"], r["hash"]) for r in c.execute("SELECT * FROM psync")}
    cur = local(c)
    out = [{"key": k, "base": seen.get(k, (0, ""))[0], "data": d} for k, d in cur.items() if seen.get(k, (0, ""))[1] != _h(k, d)]
    out += [{"key": k, "base": v, "deleted": True} for k, (v, h) in seen.items() if k not in cur and h != "DEL"]
    return out

def urgent(c):
    """有改动还没推（即时同步）：AI 学到的、本人改的（db.set_rule 记 psync_urgent）、规则文件、删除，都算。"""
    return db.meta(c, "psync_urgent") == "1" or bool(dirty(c))

def signature(c):
    """没推的改动是哪一批（看同一批是不是一直推不上去）。"""
    return _h("dirty", {x["key"]: x.get("data") or "DEL" for x in dirty(c)})

def _apply(c, it, log=lambda m: print(m, file=sys.stderr, flush=True)):
    """别台（或合并后）的一条写进本地。返回写完后的指纹（删了是 DEL）。"""
    key, data = it["key"], it.get("data") or {}
    if key.startswith("r:"):
        p = os.path.join(db.RULES, os.path.basename(key[2:]))
        if not p.endswith(".md"): raise ValueError(f"规则文件名不对：{key[:40]}")
        if it.get("deleted"):
            if os.path.exists(p): os.remove(p)
            return "DEL"
        os.makedirs(db.RULES, exist_ok=True)
        tmp = p + ".sync"; open(tmp, "w", encoding="utf-8").write(data.get("text") or ""); os.replace(tmp, p)
        return _h(key, {"text": (data.get("text") or "")[:4000]})
    if key == SETTINGS:
        if not it.get("deleted"): _apply_settings(c, data, log)
        st = {k: v for k in db.SETTING_KEYS if k not in db.LOCAL_KEYS and (v := db.meta(c, k, None)) is not None}
        return _h(key, st) if st else "DEL"
    chat = key[2:]
    if it.get("deleted"):
        c.execute("DELETE FROM profile WHERE chat=?", (chat,)); return "DEL"
    cols = [r[1] for r in c.execute("PRAGMA table_info(profile)")]
    row = {k: v for k, v in data.items() if k in cols and k not in ("chat", "rules", "updated")}   # 不认识的字段（别台 skill 更新）本地不存，wxtest 那边留着
    old = c.execute("SELECT * FROM profile WHERE chat=?", (chat,)).fetchone()
    if old:
        sets = ",".join(f"{k}=?" for k in row)
        c.execute(f"UPDATE profile SET {sets + ',' if sets else ''}updated=? WHERE chat=?", (*row.values(), int(time.time()), chat))
    else:
        ks = ["chat", "updated", *row]
        c.execute(f"INSERT INTO profile({','.join(ks)}) VALUES({','.join('?' * len(ks))})", (chat, int(time.time()), *row.values()))
    r = c.execute("SELECT * FROM profile WHERE chat=?", (chat,)).fetchone()
    return _h(key, {k: r[k] for k in r.keys() if k not in ("rules", "updated")})

def run(c, task, log=lambda m: print(m, file=sys.stderr, flush=True)):
    """做一次同步（领到 kind=sync 任务时）。返回 {"pushed", "got", "conflicts"}；失败返回 None（只记日志，wxtest 过会儿再派）。"""
    import awx                        # 放这里：没令牌时 import 就退出，别连累 watch.py
    _init(c)
    t0, pushed, got, conflicts, resets = time.time(), 0, 0, 0, 0
    epoch, cursor = db.meta(c, "psync_epoch"), int(db.meta(c, "psync_cursor") or 0)
    todo, urg = dirty(c), urgent(c)
    first = not epoch and not c.execute("SELECT 1 FROM psync LIMIT 1").fetchone()
    if first: todo = []   # 新 agent 第一次：先把 wxtest 上已有的拉下来（本机装好时生成的初始画像以那边为准），再推那边没有的，免得几十条「冲突」
    start = {k: _h(k, d) for k, d in local(c).items()}     # 开始时本地每条的指纹：途中本机又改了的不拿别台的覆盖
    low, raced = None, 0                                    # low：写失败的最小版本，游标不越过它（下次重拉）
    try:
        while True:
            items, todo = todo[:BATCH], todo[BATCH:]
            r = awx.api("/profiles", {"task": task, "epoch": epoch, "cursor": cursor, "schema": SCHEMA, "urgent": urg, "items": items}, timeout=30)
            if r.get("reset"):
                resets += 1
                if resets > 1: raise RuntimeError("wxtest 连续要求全量回传，先停下")
                log(f"[psync] wxtest 的画像库{'是新建的（旧库丢了）' if r.get('lost') else '和本机记的对不上'}：本机全量回传，不删本地")
                c.execute("DELETE FROM psync"); c.commit()
                epoch, cursor, todo = r.get("epoch") or "", 0, dirty(c)
                continue
            pushed += r.get("took", 0); conflicts += len(r.get("conflicts") or [])
            for x in r.get("conflicts") or []: log(f"[psync] 冲突 {str(x.get('key'))[:40]} 字段 {','.join(x.get('fields') or [])}（已按规则合并，wxtest 留了历史可退回）")
            for it in r.get("items") or []:
                try:
                    k = it["key"]
                    if _now_hash(c, k) != start.get(k, "DEL"):   # 同步途中本机又改了这条：不覆盖，下一轮带旧 base 推上去由 wxtest 合并
                        raced += 1; log(f"[psync] {str(k)[:40]} 同步途中本机又改了，先不覆盖，马上再同步一次"); continue
                    h = _apply(c, it, log); got += 1
                    if first and _extra(c, it): h = ""            # 第一次对齐：本机有、那边空着的字段还要推上去（标成改过）
                    c.execute("INSERT OR REPLACE INTO psync VALUES(?,?,?)", (k, int(it["ver"]), h))
                    start[k] = h
                except Exception as e:
                    v = int(it.get("ver") or 0); low = v if low is None else min(low, v)
                    log(f"[psync] 写 {str(it.get('key'))[:40]} 失败（第 {v} 版，下次重拉）：{e}")
            c.commit()
            epoch, cursor = r.get("epoch") or epoch, int(r.get("cursor") or cursor)
            db.meta_set(c, "psync_epoch", epoch); db.meta_set(c, "psync_cursor", str(cursor))
            if int(r.get("schema") or 0) > SCHEMA: log(f"[psync] 别的 agent 的画像格式更新（{r.get('schema')} > {SCHEMA}），本机 skill 该升级了；不认识的字段 wxtest 原样留着")
            if not r.get("more") and not todo:
                if not first: break
                first, todo = False, dirty(c)
                if got: log(f"[psync] 第一次对齐：已有的 {got} 条以 wxtest 为准写进本机，再推本机独有的 {len(todo)} 条")
                if not todo: break
    except BaseException as e:            # 连不上 / 没令牌（SystemExit）/ 任务被收回（409）都只记
        log(f"[psync] 同步失败（{time.time() - t0:.1f}s）：{e}"); return None
    if low is not None and low - 1 < cursor:
        cursor = max(low - 1, 0); db.meta_set(c, "psync_cursor", str(cursor))
    db.meta_set(c, "psync_at", str(int(time.time())))
    if urg: db.meta_set(c, "psync_urgent", "")   # 途中又改的那条本身就算没推（dirty），watch.py 会马上再请；写失败的不催，下次照常同步时重拉
    log(f"[psync] 对齐到第 {cursor} 版：推 {pushed} 收 {got} 冲突 {conflicts}" + (f" 途中又改 {raced}" if raced else "") + (f" 写失败从第 {low} 版重拉" if low is not None else "") + f"（{time.time() - t0:.1f}s）")   # 每次都记：光看日志就知道成没成
    return {"pushed": pushed, "got": got, "conflicts": conflicts}

def request(c=None, log=lambda m: print(m, file=sys.stderr, flush=True)):
    """请 wxtest 马上给本机下放一次同步。成功返回 True。"""
    try:
        import awx
        awx.api("/profiles/urgent", {}, timeout=15); return True
    except BaseException as e:
        log(f"[psync] 请求马上同步没成功（下次再试）：{e}"); return False

def join(name, path=None):
    """新加一台：共用设置抄 wxtest 上最新一版（别台填过的），agent 写这台的名字，wake/vision 用示例默认。返回写了几项；wxtest 上没有就 0（第一台，照常问用户）。"""
    import awx
    path = path or _settings_file()
    here = os.path.dirname(os.path.abspath(__file__))
    raw = json.load(open(os.path.join(here, "settings.example.json"), encoding="utf-8"))
    if os.path.exists(path):      # 只是 cp 了示例、名字和称呼还没填的可以盖；填过的不动
        try: old = json.load(open(path, encoding="utf-8"))
        except ValueError: old = {}
        if any(old.get(k) not in (None, "", raw.get(k)) for k in ("me", "title")): raise SystemExit(f"{path} 已经填过本人名字/称呼，不覆盖；要重来先删掉它")
    vs = awx.api("/profiles/history?key=" + awx.quote(SETTINGS)).get("versions", [])
    cur = next((v["data"] for v in vs if not v["deleted"]), None)
    if not cur: return 0
    for k, v in cur.items():
        if k in db.SETTING_KEYS and k not in db.LOCAL_KEYS: raw[k] = str(v)
    raw["agent"] = name
    if os.path.exists(path): os.remove(path)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as fh: json.dump(raw, fh, ensure_ascii=False, indent=2)
    return len(cur)

def _key(c, name):
    if name.startswith(("p:", "r:", "s:")): return name
    r = c.execute("SELECT chat FROM profile WHERE chat=? OR alias=?", (name, name)).fetchone()
    return "p:" + (r["chat"] if r else name)

def main(a):
    if a[:1] == ["join"]:
        if len(a) < 2 or not a[1].strip(): sys.exit("要给这台起名字：python3 psync.py join 名字（问用户，别和已有的重名）")
        import awx
        names = [x.get("agent") for x in awx.api("/status").get("agents", [])]
        if a[1] in names and a[1] != awx.AGENT: sys.exit(f"「{a[1]}」已经有一台在用了（在线：{'、'.join(names)}），换一个名字")
        n = join(a[1].strip())
        print(f"已从 wxtest 拿到别台的 {n} 项共用设置写进 settings.json，名字 {a[1]}。不用再问用户设置，直接做 docs/install.md 第三步（seed.py）" if n
              else "wxtest 上还没有共用设置（这是第一台），回到第二步照常问用户")
        return
    c = db.conn(); db.init(c)
    if not a:
        print("已请 wxtest 马上安排同步" if request(c) else "没请成，看上面的原因")
    elif a[0] == "history" and len(a) > 1:
        import awx
        k = _key(c, a[1])
        for v in awx.api("/profiles/history?key=" + awx.quote(k)).get("versions", []):
            d = v["data"]; s = d.get("text", "")[:60] if k.startswith("r:") else "，".join(f"{x}={d[x]}" for x in db.FIELDS if d.get(x))[:120]
            print(f"第 {v['ver']} 版  {time.strftime('%m-%d %H:%M', time.localtime(v['at']))}  {v['by']}  {'（已删）' if v['deleted'] else s}")
    elif a[0] == "rollback" and len(a) > 1:
        import awx
        r = awx._post("/profiles/rollback", {"key": _key(c, a[1]), **({"ver": int(a[2])} if len(a) > 2 else {})}, 20)
        print(f"已退回到第 {r['from']} 版，所有在线 agent 马上跟着改" if r.get("ok") else f"没退回：{r.get('error')}")
    else:
        sys.exit(__doc__)

if __name__ == "__main__":
    main(sys.argv[1:])
