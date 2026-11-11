#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""心跳：tick.py 自动在后台拉起，每 20 秒告诉 wxtest「我还活着」，续上本 agent 手里所有任务，顺带报收件箱里的任务号，wxtest 回已不归本机的就当场叫停（只防「掉线」：单条领走 5 分钟没交，wxtest 照样收回）。
tick.py/watch.py 超过 2 分钟没动静（Muse 停了）或 skill 代码换了就自己退出，wxtest 60 秒收不到心跳就把任务收回给别的 agent。"""
import os, sys, time
import awx, db, watch

EVERY, IDLE = 20, 120
PID = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hb.pid")

def alive():
    try:
        pid = int(open(PID).read()); os.kill(pid, 0); return pid != os.getpid()
    except (OSError, ValueError):
        return False

def main():
    if alive() or not awx.AGENT:             # 没名字的 agent 不续心跳（tick 会提醒本人起名）
        return
    open(PID, "w").write(str(os.getpid()))
    c = db.conn()
    while time.time() - int(db.meta(c, "tick_at") or 0) < IDLE:
        if awx._build() != awx.BUILD:          # skill 升级了：退出，tick 下一轮用新代码拉起（不然旧心跳一直报旧指纹）
            print("[hb] skill 代码换了，退出让 tick 用新代码重拉", file=sys.stderr, flush=True); break
        try:
            hold = [os.path.basename(p)[:-5] for p in watch.pending()]
            r = awx.api("/heartbeat", {"hold": hold} if hold else {}, timeout=15)
            for g in (r.get("gone") or []) if isinstance(r, dict) else []:   # 本人接管 / 别台接手 / 已作废：当场叫停，处理员不用白跑
                watch.stop(g.get("id"), g.get("why"))
        except Exception as e:
            print(f"[hb] {e}", file=sys.stderr, flush=True)
        time.sleep(EVERY)
    try: os.remove(PID)
    except OSError: pass

if __name__ == "__main__":
    main()
