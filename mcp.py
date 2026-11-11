#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""外部工具（MCP）：本人在控制面板「外部工具」接的远程 MCP 服务（可以有多个），经 wxtest 转调（令牌只在 wxtest，这里看不到也不用填）。
  python3 mcp.py                                列出接了哪些服务和各自干什么
  python3 mcp.py 服务名                          看这个服务的使用说明和能用的工具（名字、参数、说明；〔改〕会改东西，〔删〕会删改）
  python3 mcp.py 服务名 工具名 '{"参数":"值"}'      调一个工具，打印结果（JSON）
  python3 mcp.py 服务名 工具名 <<'EOF' … EOF       参数长或带引号时用 heredoc 喂 JSON
什么时候用、哪些事不许用它做，看 SKILL.md「外部工具」。"""
import json, os, sys, tempfile, time
import awx

CACHE, TTL = os.path.join(tempfile.gettempdir(), f"awx-tools-{os.getuid()}.json"), 600

def brief():
    """接了哪些服务、各自干什么（「名字：说明」列表，一个服务一行，工具再多也只一行），给任务包的 tools 字段用。缓存 10 分钟，省得每条任务都问 wxtest；问不到就当没接，不耽误回消息。"""
    try:
        v = json.load(open(CACHE, encoding="utf-8"))
        if time.time() - v["at"] < TTL: return v["tools"]
    except (OSError, ValueError, KeyError, TypeError): pass
    try: s = awx.api("/mcp", timeout=10, tries=1).get("servers") or []
    except Exception as e: print(f"[mcp] 取外部工具清单失败：{e!r}", file=sys.stderr); return []
    tools = [f"{x['name']}：{x['note']}" if x.get("note") else f"{x['name']}：{x['intro']}" if x.get("intro") else x["name"]
             for x in s if not x.get("off")]          # 本人写的说明优先，没写用服务自带开篇说明第一句，都没有只给名字
    try:
        with open(CACHE + ".tmp", "w", encoding="utf-8") as f: json.dump({"at": time.time(), "tools": tools}, f, ensure_ascii=False)
        os.replace(CACHE + ".tmp", CACHE)
    except OSError as e: print(f"[mcp] 清单缓存写不进：{e!r}", file=sys.stderr)
    return tools

MARK = {"read": "", "write": "〔改〕", "danger": "〔删〕", "": ""}

def main(a):
    if not a:
        s = awx.api("/mcp", timeout=20).get("servers") or []
        if not s: print("还没接外部工具（本人在控制面板「外部工具」里加）"); return 0
        for x in s: print(f"{x['name']}：{x.get('note') or x['url']}")
        print("\n看某个服务的工具：python3 mcp.py 服务名")
        return 0
    if len(a) == 1:
        r = awx.api("/mcp?server=" + awx.quote(a[0]), timeout=60)
        if r.get("error"): print(r["error"]); return 1
        if r.get("note"): print(f"【{a[0]}】{r['note']}")
        if r.get("instructions"): print("【使用说明】\n" + r["instructions"] + "\n")
        for t in r.get("tools") or []:
            props = (t.get("input") or {}).get("properties") or {}
            need = set((t.get("input") or {}).get("required") or [])
            args = "，".join(f"{k}{'*' if k in need else ''}" for k in props)
            print(f"- {MARK.get(t.get('kind') or '', '')}{t['name']}（{args or '无参数'}）：{t.get('description', '')}")
        if not r.get("tools"): print("（没有能用的工具）")
        return 0
    raw = a[2] if len(a) > 2 else ("" if sys.stdin.isatty() else sys.stdin.read())
    try: args = json.loads(raw) if raw.strip() else {}
    except ValueError as e: print(f"参数不是 JSON：{e}"); return 1
    if not isinstance(args, dict): print("参数要是 JSON 对象 {…}"); return 1
    r = awx.call("/mcp/call", {"server": a[0], "tool": a[1], "args": args}, wait=120)
    if r is None: print("没拿到结果（网络断了或 wxtest 重载）：会改东西的工具先查清楚做没做成，别直接重调"); return 1
    r.pop("http", None)
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return 0 if not r.get("error") and not r.get("is_error") else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
