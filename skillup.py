"""从 wxtest 拉它镜像自带的那版 skill，覆盖本目录（wxtest 发现本机落后时派 kind=update，tick.py 领到就做；手动：python3 watch.py update）。
本人 11-03 授权：这是唯一允许自动改 skill 代码的路子。只换仓库里的代码和文档；settings.json、awx.env、store.db、rules/<人>.md 等本机数据一律不动。
步骤：先全写进临时目录核对指纹、全部 .py 能编译，再把旧文件备份到 .bak/<时间>/（留 3 份）逐个替换；替换后指纹不对就整份恢复。"""
import json, os, sys, time, shutil, tempfile, difflib
import awx

HERE = os.path.dirname(os.path.abspath(__file__))
KEEP = 3        # 备份留几份（.bak/<时间>-<旧指纹>/）
DATA = ("settings.json", "awx.env", "store.db", "hb.pid", "watch.lock", "watch.log", "watch.log.1")

def _ok(rel):
    """只认仓库跟踪的那几类：顶层 .py/SKILL.md/schema.sql/settings.example.json、docs/ styles/ 下的 .md、rules/_example.md。"""
    if not rel or rel.startswith(("/", ".")) or "\\" in rel or ".." in rel.split("/") or os.path.basename(rel) in DATA: return False
    parts = rel.split("/")
    if len(parts) == 1: return rel.endswith(".py") or rel in ("SKILL.md", "schema.sql", "settings.example.json")
    return (len(parts) == 2 and parts[0] in ("docs", "styles") and rel.endswith(".md")) or rel == "rules/_example.md"

def _code(d):
    return sorted(f for f in os.listdir(d) if f.endswith(".py") or f in ("schema.sql", "SKILL.md"))

def run(want="", here=None, api=None):
    """返回 {"ok", "build", "from", "changed", "removed"} 或 {"ok": False, "why"}。"""
    here, api = here or HERE, api or awx.api
    old = awx._build(here)
    b = api("/skill", timeout=120)
    build, files = b.get("full") or b.get("build") or "", b.get("files") or {}   # full 含文档（新版 wxtest）；旧版只有 build
    if not build or not files: return {"ok": False, "why": "wxtest 没有自带 skill（旧镜像）"}
    if want and want not in (build, b.get("build")): print(f"[update] 派单时目标 {want}，wxtest 现在自带 {build}，按现在的来", file=sys.stderr)
    if build == old: return {"ok": True, "build": build, "from": old, "changed": [], "removed": []}
    bad = [r for r in files if not _ok(r)]
    if bad: return {"ok": False, "why": f"包里有不该有的路径：{bad[:3]}"}
    tmp = tempfile.mkdtemp(prefix=".skillup-", dir=here)
    try:
        for rel, text in files.items():
            p = os.path.join(tmp, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="") as f: f.write(text)
        if awx._build(tmp) != build: return {"ok": False, "why": f"拉下来的指纹 {awx._build(tmp)} 和 wxtest 说的 {build} 不一样，没换"}
        for rel in files:
            if rel.endswith(".py"):
                try: compile(files[rel], rel, "exec")
                except (SyntaxError, ValueError) as e: return {"ok": False, "why": f"{rel} 编译不过，没换：{str(e)[:120]}"}
        changed = [r for r in sorted(files) if not os.path.exists(os.path.join(here, r)) or open(os.path.join(here, r), encoding="utf-8", errors="replace").read() != files[r]]
        removed = [f for f in _code(here) if f not in files]   # 上游删掉的顶层代码（不删就会算进指纹、对不上）
        bak = os.path.join(here, ".bak", time.strftime("%Y%m%d-%H%M%S") + f"-{old}")
        added = []
        for rel in changed + removed:
            src = os.path.join(here, rel)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(os.path.join(bak, rel)), exist_ok=True); shutil.copy2(src, os.path.join(bak, rel))
            elif rel in files: added.append(rel)
        for rel in changed:
            dst = os.path.join(here, rel); os.makedirs(os.path.dirname(dst), exist_ok=True); os.replace(os.path.join(tmp, rel), dst)
        for rel in removed: os.remove(os.path.join(here, rel))
        now = awx._build(here)
        if now != build:                                       # 替换途中出了岔子：整份恢复
            _restore(here, bak, added)
            return {"ok": False, "why": f"换完指纹是 {now} 不是 {build}，已恢复原样（备份 {os.path.basename(bak)}）"}
        _prune(os.path.join(here, ".bak"))
        print(f"[update] skill {old} → {build}：改 {len(changed)} 个、删 {len(removed)} 个，旧文件备份在 .bak/{os.path.basename(bak)}", file=sys.stderr)
        return {"ok": True, "build": build, "from": old, "changed": changed, "removed": removed, "bak": bak}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

CAP = 8000   # 给 AI 的改动原文最多这么多字，超了只列文件名，让它自己去读全文

def _read(p):
    try: return open(p, encoding="utf-8", errors="replace").read()
    except OSError: return ""

def brief(r, here=None, agent=""):
    """升级成功后：给本人的一句通知 + 要叫醒 AI 的任务（只放事实字段；文档没变返回 None）。
    文档 = SKILL.md、docs/ styles/ 下的 .md、rules/_example.md：AI 会话里读的是旧版，要它重读；只改代码下一轮自动生效，不叫醒。"""
    here = here or HERE
    ch, rm = r.get("changed") or [], r.get("removed") or []
    if not ch and not rm: return None, None
    docs = [f for f in ch + rm if f.endswith(".md")]
    code = [f for f in ch + rm if f not in docs]
    names = "、".join((ch + rm)[:6]) + (f" 等 {len(ch) + len(rm)} 个" if len(ch) + len(rm) > 6 else "")
    note = f"{agent or '本机'} skill 已升级（{r.get('from')} → {r.get('build')}），改了 {names}" + ("；已叫醒 AI 重读改过的说明。" if docs else "；只改代码，AI 不用重读。")
    if not docs: return note, None
    diff, cut = [], False
    for f in docs:
        old, new = _read(os.path.join(r.get("bak") or "", f)), _read(os.path.join(here, f))
        d = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"旧/{f}", f"新/{f}", n=1))
        if sum(map(len, diff)) + len(d) > CAP: cut = True; continue
        diff.append(d)
    task = {"kind": "skill升级", "from": r.get("from"), "build": r.get("build"), "docs": docs, "code": code,
            "diff": "".join(diff), "diff_cut": cut}
    return note, task

def mark_unread(task, here=None):
    """记下这次说明改动还没读（awx.UNREAD）：每个请求报 X-Unread，wxtest 10 分钟没读告诉本人；
    AI 处理掉 skill升级 任务（watch done）就删；任务被值班丢掉（超时）的话，下一条任务附上改动原文（watch.next）。"""
    p = os.path.join(here or HERE, os.path.basename(awx.UNREAD))
    try:
        with open(p + ".tmp", "w", encoding="utf-8") as f: json.dump(task, f, ensure_ascii=False)
        os.replace(p + ".tmp", p)
    except OSError as e: print(f"[update] 记「说明没读」失败：{e}", file=sys.stderr)

def _restore(here, bak, added):
    for root, _, fs in os.walk(bak):
        for f in fs:
            src = os.path.join(root, f); shutil.copy2(src, os.path.join(here, os.path.relpath(src, bak)))
    for rel in added:
        try: os.remove(os.path.join(here, rel))
        except OSError as e: print(f"[update] 恢复时删 {rel} 失败：{e}", file=sys.stderr)

def _prune(d):
    try:
        for x in sorted(os.listdir(d))[:-KEEP]: shutil.rmtree(os.path.join(d, x), ignore_errors=True)
    except OSError as e: print(f"[update] 清旧备份失败：{e}", file=sys.stderr)
