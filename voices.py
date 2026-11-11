# -*- coding: utf-8 -*-
"""千人千面的语音：对方发语音时回复转成语音，用谁的声音按人定，AI 按那个人的说话风格写，结尾自报身份。
选声音：rules/ 里这个人的文件写「声音：小美」（声音名/人名/昵称，写「声音：文字」= 只回文字）> 设置 voice_by（标签=声音）> default（本人克隆声）。
不是本人的声音必须在设置 voice_roles 里登记（声音=人名:昵称），结尾强制念「我是<称呼>私人助手<昵称>」；没登记的一律用 default。"""
import os, re
import db

def _pairs(items):
    return dict(x.split("=", 1) for x in items if "=" in x)

def roles(cfg):
    """{声音: (人名, 昵称)}"""
    return {k.strip(): tuple((v.split(":", 1) + [v])[:2]) for k, v in _pairs(cfg["voice_roles"]).items()}

def pick(cfg, chat, alias=None, tags=()):
    own = db.rules(chat, alias)
    m = re.search(r"声音\s*[:：]\s*([\w-]+)", own)           # 可写声音名，也可写人名或昵称（中文名、昵称）
    alias = {n.strip(): k for k, r in roles(cfg).items() for n in (k, *r)}
    alias.update({x: "default" for x in ("default", "本人", "我", cfg["title"], cfg.get("me") or "default")})
    alias.update({x: None for x in ("文字", "无", "不用", "off")})   # 这个人发语音也只回文字
    by = {k.strip(): v.strip() for k, v in _pairs(cfg["voice_by"]).items()}
    v = m.group(1) if m else next((by[t] for t in tags if t in by), "default")
    return alias.get(v, "default")

def tail(cfg, v):
    r = roles(cfg).get(v)
    return f"我是{cfg['title']}私人助手{r[1].strip()}。" if r else None   # None = wxtest 用设置 voice_sign

def _card(v):
    """styles/<声音名>.md：这个声音的说话风格卡（仓库只带本人 default；别的声音本人描述后 Muse 新建）。"""
    try: return open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "styles", os.path.basename(v) + ".md"), encoding="utf-8").read().strip()
    except OSError: return ""

def brief(cfg, v):
    """回复要念成语音时给 Muse 的资料：用谁的声音、以什么身份、风格卡（写法见 SKILL.md「写回复的规矩」）。"""
    r, out = roles(cfg).get(v), {}
    if r:
        out.update(speaker=r[0].strip(), as_name=r[1].strip())
    if c := _card(v):
        out["card"] = c.replace("<称呼>", cfg["title"])
    return out
