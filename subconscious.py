# -*- coding: utf-8 -*-
"""潜意识 / 睡眠反思（v1.1.2 任务二）

白天聊过的东西，人在睡前会「过一遍」。这个模块就是干这个：
每天凌晨（默认 3 点）把昨天聊过的内容交给模型，让它写 3 条
「自我反思」或「对你的新认识」，存进 memory.inner_thoughts。

这些内容只进系统提示词的**背景段**（【你的潜意识】），
明确要求：只作为背景，不许直接念出来——否则就变成机器人复读日记。

对外：
    due(mem)                 -> 今天该不该反思（bool）
    daily_reflection(mem)    -> {ok, msg, added, thoughts}
    run_loop(mem, log, ask)  -> 后台线程：到点反思 + 顺手做记忆衰退
    decay_memories(mem)      -> 把很久没动的永久记忆压成模糊记忆

设计原则：
1. 全都 try/except：反思失败绝不影响聊天；
2. 昨天没聊过就不反思（省一次模型调用）；
3. 一天最多一次（用 inner_thoughts.day 判重）。
"""
import datetime
import threading
import time

import config as cfg_mod


def _ndef(v, d):
    """v1.8.8：0 / 0.0 / False 都是合法配置值，只有 None 和空串才回落默认。

    以前到处写 `cfg.get(K) or N`，用户把某项设成 0 会被强行改回默认值 ——
    「主动关心最短间隔 0 分钟」「每日上限 0 条」「生图概率 0」全都会失效。
    """
    if v is None:
        return d
    if isinstance(v, str) and not v.strip():
        return d
    return v



REFLECT_PROMPT = """你在给一个 QQ 聊天机器人写「睡前自我反思」。

下面是它昨天和用户/群友的真实聊天记录（格式：会话 | 角色 | 内容）：

{dialog}

请以**第一人称**写 {count} 条它自己的心里话，要求：
1. 每条一句话，20~40 字，口语，别用书面总结腔；
2. 内容要来自上面的聊天：对某个人的新认识、发现自己说话的问题、下次想怎么做；
3. 不要评价「对话流畅度」这种空话，要具体（提到人名/话题/情绪）；
4. 不要复述原话，不要说「我作为 AI」；
5. 严格输出 JSON 数组，例如 ["…","…","…"]，不要任何解释。

开始："""

DECAY_PROMPT = """把下面这条长期记忆压缩成一句「模糊记忆」，只保留最有辨识度的部分。

原记忆：{text}

要求：{n} 字以内，口语，像人回忆很久以前的事；不要加引号、不要解释。"""


def _today():
    return datetime.date.today().isoformat()


def _yesterday():
    return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


def due(mem):
    """今天还没反思过、并且昨天有聊天记录 → True。"""
    try:
        if not bool(cfg_mod.get("subconscious_enabled", True)):
            return False
        if mem is None:
            return False
        if mem.has_thought_for_day(_today()):
            return False
        start = time.time() - 36 * 3600.0   # 过去 36 小时（跨夜也够）
        return len(mem.messages_since(start, limit=5)) > 0
    except Exception:
        return False


def _parse_thoughts(text):
    """从模型输出里抠出 3 条。模型爱乱加 ```json 或解释，所以又能用正则兜底。"""
    import json
    import re
    raw = str(text or "").strip()
    if not raw:
        return []
    cand = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    out = []
    try:
        data = json.loads(cand)
        if isinstance(data, list):
            out = [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        m = re.search(r"\[[\s\S]*\]", cand)
        if m:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, list):
                    out = [str(x).strip() for x in data if str(x).strip()]
            except Exception:
                out = []
    if not out:
        # 最后兜底：按行取，去掉序号和引号
        for line in cand.splitlines():
            s = line.strip().strip(",[]\"' ")
            s = re.sub(r"^[-*\d.、）)\s]+", "", s)
            if 4 <= len(s) <= 80:
                out.append(s)
    return [x for x in out if 4 <= len(x) <= 200][:8]


def _cfg_int(key, default, lo=1, hi=200):
    try:
        v = int(cfg_mod.get(key, default) or default)
    except Exception:
        v = default
    return max(lo, min(hi, v))


def reflection_count():
    """每次写几条（默认 3，可在「自省」页改）。"""
    return _cfg_int("subconscious_count", 3, 1, 8)


def reflection_time():
    """反思时间 -> (时, 分)。老配置只有 consciousness_hour，分钟默认 0。"""
    h = _cfg_int("subconscious_hour", 3, 0, 23)
    m = _cfg_int("subconscious_minute", 0, 0, 59)
    return h, m


def daily_reflection(mem, ask=None, log=None, force=False, count=None):
    """做一次反思。ask(prompt) -> str，由 agent 传进来（避免循环 import）。

    force=True 时忽略「今天已经反思过」的限制（用户在界面点「现在反思一次」）。
    count 为空时取配置 subconscious_count。
    """
    def _log(msg, level="INFO"):
        if callable(log):
            try:
                log(msg, level)
            except Exception:
                pass
    try:
        if mem is None or not callable(ask):
            return {"ok": False, "msg": "缺参数", "added": 0, "thoughts": []}
        if not force and mem.has_thought_for_day(_today()):
            return {"ok": True, "msg": "今天已经反思过了", "added": 0, "thoughts": []}
        try:
            n = int(count if count is not None else reflection_count())
        except Exception:
            n = 3
        n = max(1, min(8, n))
        start = time.time() - 36 * 3600.0
        rows = mem.messages_since(start, limit=160)
        if not rows:
            return {"ok": True, "msg": "昨天没聊过，跳过", "added": 0, "thoughts": []}
        lines = []
        for key, role, content in rows[-120:]:
            who = "我" if role == "assistant" else "对方"
            text = str(content or "").replace("\n", " ")[:120]
            if text:
                lines.append(f"{key} | {who} | {text}")
        prompt = REFLECT_PROMPT.format(dialog="\n".join(lines), count=n)
        raw = ask(prompt) or ""
        thoughts = _parse_thoughts(raw)
        if not thoughts:
            _log("[潜意识] 模型没给出可用内容", "WARN")
            return {"ok": False, "msg": "模型没按格式回", "added": 0, "thoughts": []}
        day = _today()
        added = 0
        for t in thoughts[:n]:
            if mem.add_thought(t, day):
                added += 1
        _log(f"[潜意识] 记下 {added} 条自我反思")
        # v1.8.12（任务四）：同一次反思里顺手更新「用户情感画像」
        try:
            import emoprofile as _ep
            _pr = _ep.reflect_today(mem, ask=ask, log=log)
            if _pr.get("count"):
                _log("[情感画像] " + str(_pr.get("msg") or ""))
        except Exception as _epx:
            _log(f"[情感画像] 提炼跳过：{_epx}", "DEBUG")
        return {"ok": True, "msg": f"记下 {added} 条", "added": added, "thoughts": thoughts}
    except Exception as e:
        _log(f"[潜意识] 失败：{e}", "WARN")
        return {"ok": False, "msg": str(e), "added": 0, "thoughts": []}


def decay_memories(mem, ask=None, log=None, max_items=None, max_len=None):
    """记忆衰退：把很久没动的永久记忆压成模糊记忆（默认关）。

    max_items / max_len 为空时取配置 memory_decay_max / memory_decay_len。
    """
    def _log(msg, level="INFO"):
        if callable(log):
            try:
                log(msg, level)
            except Exception:
                pass
    try:
        if not bool(cfg_mod.get("memory_decay_enabled", False)):
            return {"ok": True, "msg": "没开", "changed": 0}
        if mem is None or not callable(ask):
            return {"ok": False, "msg": "缺参数", "changed": 0}
        days = int(_ndef(cfg_mod.get("memory_decay_days", 180), 180))
        try:
            lim = int(max_items if max_items is not None else _ndef(cfg_mod.get("memory_decay_max", 10), 10))
        except Exception:
            lim = 10
        lim = max(1, min(200, lim))
        try:
            mlen = int(max_len if max_len is not None else _ndef(cfg_mod.get("memory_decay_len", 15), 15))
        except Exception:
            mlen = 15
        mlen = max(6, min(60, mlen))
        rows = mem.old_perm_memories(days, limit=lim)
        changed = 0
        for mem_id, _uid, content in rows:
            try:
                short = str(ask(DECAY_PROMPT.format(text=str(content)[:300], n=mlen)) or "").strip()
                short = short.strip("「」\"' ")[:max(mlen, 8)]
                if short and len(short) < len(str(content)):
                    if mem.update_perm_content(mem_id, short):
                        changed += 1
            except Exception:
                continue
        if changed:
            _log(f"[记忆衰退] 压模糊了 {changed} 条")
        return {"ok": True, "msg": f"处理 {changed} 条", "changed": changed}
    except Exception as e:
        return {"ok": False, "msg": str(e), "changed": 0}


def run_loop(mem, ask=None, log=None, stop=None):
    """后台线程：每 10 分钟醒一次，到点就反思 + 顺手做记忆衰退。"""
    while True:
        try:
            if stop is not None and stop():
                return
            hour, minute = reflection_time()
            now = datetime.datetime.now()
            if (now.hour == hour and now.minute >= minute) and bool(cfg_mod.get("subconscious_enabled", True)):
                if due(mem):
                    daily_reflection(mem, ask=ask, log=log)
                decay_memories(mem, ask=ask, log=log)
        except Exception:
            pass
        time.sleep(600)


def start(mem, ask=None, log=None, stop=None):
    """起线程（agent 启动时调一次）。"""
    try:
        t = threading.Thread(target=run_loop, args=(mem, ask, log, stop), daemon=True)
        t.start()
        return t
    except Exception:
        return None
