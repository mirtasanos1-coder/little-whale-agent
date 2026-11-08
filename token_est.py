# -*- coding: utf-8 -*-
"""token_est.py —— 功能级 token 消耗预估（v1.5.1）

口径说明（每一行都要能被用户看懂）：
- 「单次」= 触发一次大约消耗多少 token（输入 + 输出）。
- 「次数/天」= 按默认设置下的典型频率估的，不是承诺值。
- 所有数字都按「中文字符 ≈ 1.6 token」换算；真实值随模型分词器浮动 ±20%。
- 只统计**会真的发请求**的功能：拆条、贴纸、思考这些纯本地处理记 0。
"""

import os
import re


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



CHARS_PER_TOKEN = 1.6          # 中文经验值


def _tok(chars):
    return int(round(max(0.0, float(chars)) / CHARS_PER_TOKEN))


def _cfg():
    try:
        import config as cfg_mod
        return cfg_mod.load()
    except Exception:
        return {}


def _prompt_chars(name, default=0):
    """从 agent.py 里量真实 prompt 的长度（量不到就用默认）。"""
    try:
        import agent
        v = getattr(agent, name, "")
        return len(v) if isinstance(v, str) and v else default
    except Exception:
        return default


def _on(c, key, default=False):
    v = c.get(key, default)
    if isinstance(v, bool):
        return v
    if v in (None, ""):
        return bool(default)
    return str(v).strip().lower() not in ("0", "false", "off", "no", "关", "关闭")


# 一条聊天回复的固定开销：人设 + 语气 + 记忆注入 + 最近上下文 + 输出
def _chat_per_msg(sys_chars):
    return _tok(sys_chars + 900) + 90


def rows():
    """返回每一行的预估。enabled 是「现在开没开」。"""
    c = _cfg()
    sys_chars = _prompt_chars("SYSTEM_PROMPT", 900)
    out = []

    def add(fid, name, on, unit, per, times, note, group="对话"):
        out.append({
            "id": fid, "name": name, "group": group, "enabled": bool(on),
            "unit": unit, "per": int(per), "times": float(times),
            "per_day": int(round(per * times)), "note": note,
        })

    # —— 对话 ——
    add("chat", "每条回复（含人设+记忆注入）", True, "条",
        _chat_per_msg(sys_chars), float(_ndef(c.get("est_msgs_per_day"), 200)),
        "人设 %d 字 + 记忆/上下文约 900 字 + 回复 90 token；按 %s 条/天估"
        % (sys_chars, _ndef(c.get("est_msgs_per_day"), 200)))

    add("care", "主动关心（timer 触发）", _on(c, "care_plan_enabled", False), "次",
        _tok(700) + 60, float(_ndef(c.get("est_care_per_day"), 6)),
        "每次一次独立请求：判断该不该关心 + 生成一句；不开启则 0")

    add("moment", "自动说说", _on(c, "moment_enabled", False), "条",
        _tok(900) + 150, float(_ndef(c.get("est_moments_per_day"), 2)),
        "选题 + 写正文 + 话题标签，一次约 900 字输入")

    add("introspect", "自省（定时反思）", _on(c, "introspect_enabled", True), "次",
        _tok(1200) + 200, float(_ndef(c.get("est_introspect_per_day"), 3)),
        "把最近对话揉成一段反思，输入最长，输出也不短")

    add("reflect", "事后自省（吵完道歉）", _on(c, "post_reflect_enabled", True), "次",
        _tok(420) + 80, float(_ndef(c.get("est_reflect_per_day"), 1)),
        "只在检测到情绪不对时触发，且 90 分钟冷却；单次很轻")

    add("humanize", "拆条 / 假装打字", _on(c, "humanize_enabled", True), "条",
        0, 0, "纯本地切分与延时，不额外发请求（0 token）")

    add("sticker", "表情包 / 贴纸", True, "次",
        0, 0, "本地随机挑图，不发请求")

    add("route_memo", "高频话记录模式", True, "次",
        0, 0, "命中缓存直接回，反而**省**一次完整请求")

    # —— 生图 / 语音 ——
    add("imagegen", "生图（含 SVG 兜底）", _on(c, "imagegen_enabled", False), "张",
        _tok(200) + (_tok(2600) if str(c.get("imagegen_via") or "auto") in ("svg", "vector") else 0),
        float(_ndef(c.get("est_images_per_day"), 3)),
        "标准生图接口按张计费、token 很少；SVG 走聊天模型，输出要 2000~3000 token", "创作")

    add("tts", "语音合成（edge-tts）", _on(c, "tts_enabled", False), "条",
        0, 0, "走微软免费接口，不消耗大模型 token（自定义音色 / 声音克隆走独立进程，也一样不吃大模型 token）", "创作")

    # —— Agent ——
    tools = 0
    for k in ("agent_web", "agent_code", "agent_keyboard"):
        if _on(c, k, False):
            tools += 1
    steps = int(_ndef(c.get("max_agent_steps"), 10))
    add("agent", "Agent 任务（联网/键鼠/代码）",
        _on(c, "agent_enabled", False) and tools > 0, "次",
        _tok(1300) * min(steps, 4) + 120 * steps, float(_ndef(c.get("est_agent_per_day"), 2)),
        "一次任务最多 %d 步，每步都要带工具说明重发一遍上下文；开 %d 个工具能力" % (steps, tools),
        "Agent")

    add("approval", "Agent 危险动作确认", _on(c, "agent_enabled", False), "次",
        _tok(200), float(_ndef(c.get("est_agent_per_day"), 2)) * 0.5,
        "问你要不要动手的那句话很短", "Agent")

    # —— 记忆（v1.8.2：知识图谱） ——
    kg_on = _on(c, "kg_enabled", True)
    add("kg_extract", "图谱记忆提取（抽关系）", kg_on, "次",
        _tok(600) + 150, float(_ndef(c.get("est_kg_extract_per_day"), 8)),
        "抽永久记忆时顺带拆三元组，在原回复基础上**额外 +150 token**；"
        "每次最多 6 条记忆；关掉「知识图谱」后为 0", "记忆")

    add("kg_inject", "图谱注入（关系网）", kg_on, "次",
        100, float(_ndef(c.get("est_msgs_per_day"), 200)),
        "每次回复把相关关系网拼进提示词，**单次 +100 token**；"
        "没命中关系时是空串、不占 token", "记忆")

    return out


def summary():
    rs = rows()
    on = [r for r in rs if r["enabled"] and r["per_day"] > 0]
    day_on = sum(r["per_day"] for r in on)
    all_day = sum(r["per_day"] for r in rs)
    # 现在开着但没算进 on（per_day=0 的）也要列出来，便于解释
    return {
        "ok": True,
        "rows": rs,
        "chips": CHARS_PER_TOKEN,
        "enabled_day": day_on,
        "enabled_month": day_on * 30,
        "all_day": all_day,
        "all_month": all_day * 30,
        "count_enabled": len(on),
        "count_total": len(rs),
        "note": "按「中文字符 ÷ 1.6 = token」估算，真实值随分词器浮动约 ±20%；"
                "频率可在配置里用 est_* 系列键调整（默认按日常聊天估）。",
    }
