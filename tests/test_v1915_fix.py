# -*- coding: utf-8 -*-
"""V1.9.15 自测：连发合并 / 卡片解析 / 不乱画 / 事后自省带真实上文

跑法：python tests\\test_v1915_fix.py
不用 pytest，跑完打小结，失败返回码 1。

对应测试用户反馈的四个「人机感」Bug：
  一、同一个人连发 3 条，只该合并回 1 次（并且群里两个人不能被并成一句）
  二、发卡片 / 视频链接，模型要能看见标题和链接，不能回「没看到图」
  三、随便聊两句、发个视频，绝不能自己开始画图
  四、事后自省要基于最近 3 轮真实对话，觉得没问题就 SKIP，不许硬拼记忆碎片
"""
import json
import os
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="xy_v1915_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP

import agent      # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append((name, extra))
    print(("  [OK]   " if cond else "  [FAIL] ") + name +
          (("   -> " + str(extra)[:300]) if extra and not cond else ""))


def head(t):
    print("")
    print("== " + t + " " + "=" * max(0, 58 - len(t)))


# ==================== 一、卡片 / 链接解析 ====================
def json_card_event(title, url, desc="一段描述"):
    body = {"app": "com.tencent.structmsg",
            "meta": {"news": {"title": title, "desc": desc, "jumpUrl": url}}}
    return {"message": [{"type": "json",
                         "data": {"data": json.dumps(body, ensure_ascii=False)}}]}


def t_cards():
    head("一、卡片 / 链接解析")
    txt = agent.extract_text(json_card_event("猫猫的搞笑视频", "https://b23.tv/abc123"))
    ok("json 卡片：拿到标题", "猫猫的搞笑视频" in txt, txt)
    ok("json 卡片：拿到链接", "b23.tv/abc123" in txt, txt)
    ok("json 卡片：拼成模型看得懂的一句", "用户分享了卡片/视频" in txt, txt)

    # 被 URL 编码 / 转义过的卡片（真机最常见的形态）
    raw = json.dumps({"meta": {"news": {"title": "转发的短视频", "jumpUrl": "https://v.qq.com/x"}}},
                     ensure_ascii=False)
    ev = {"message": [{"type": "json", "data": {"data": urllib.parse.quote(raw)}}]}
    txt = agent.extract_text(ev)
    ok("URL 编码的卡片也能认", "转发的短视频" in txt and "v.qq.com" in txt, txt)

    xml = ("<?xml version='1.0'?><msg serviceID='1'><item><title>群里转的短视频</title>"
           "<summary>笑死</summary><url>https://v.qq.com/xyz</url></item></msg>")
    txt = agent.extract_text({"message": [{"type": "xml", "data": {"data": xml}}]})
    ok("xml 卡片：拿到标题", "群里转的短视频" in txt, txt)
    ok("xml 卡片：拿到链接", "v.qq.com/xyz" in txt, txt)

    cq = "[CQ:json,data=" + urllib.parse.quote(
        json.dumps({"title": "CQ卡片标题", "url": "https://cq.link/1"}, ensure_ascii=False)) + "]"
    txt = agent.extract_text({"message": cq})
    ok("CQ 字符串里的卡片也能认", "CQ卡片标题" in txt, txt)

    ok("纯文字消息没被改坏", agent.extract_text({"message": "你好呀"}) == "你好呀",
       agent.extract_text({"message": "你好呀"}))
    ok("卡片不会退化成一句「没看到」", "没看到" not in txt)


# ==================== 二、防抖 / 连发合并 ====================
def t_debounce():
    head("二、防抖：连发只回一次")
    calls = []

    def fake_pm(ev, mtype, key, sender, text, target, uid):
        calls.append({"key": key, "sender": sender, "text": text, "uid": uid})

    orig_pm, orig_can, orig_delay = agent.process_message, agent.can_reply, agent.merge_delay_sec
    agent.process_message = fake_pm
    agent.can_reply = lambda k: True
    agent.merge_delay_sec = lambda: 0.3
    try:
        def text_ev(s):
            return {"message": [{"type": "text", "data": {"text": s}}]}

        # --- 2.1 同一个人连发 3 条 ---
        for m in ("在吗", "我给你看个东西", "笑死我了"):
            agent._debounce_push("group_1", text_ev(m), "group", "阿杰", m, ("group", 1), 10001)
        time.sleep(1.0)
        ok("连发 3 条 → 只回 1 次", len(calls) == 1, calls)
        if calls:
            t = calls[0]["text"]
            ok("合并后的内容三条都在", all(x in t for x in ("在吗", "我给你看个东西", "笑死我了")), t)
            ok("合并后归属还是同一个人", calls[0]["sender"] == "阿杰" and calls[0]["uid"] == 10001,
               calls[0])

        # --- 2.2 旧定时器迟到不能重复回 ---
        calls.clear()
        bk = agent._debounce_bufkey("group_2", 10002)
        agent._debounce_push("group_2", text_ev("第一句"), "group", "小雨", "第一句",
                             ("group", 2), 10002)
        agent._debounce_push("group_2", text_ev("第二句"), "group", "小雨", "第二句",
                             ("group", 2), 10002)
        agent._debounce_flush(bk, 1)          # 假装第 1 个定时器迟到了
        ok("过期的定时器不会抢先回话", len(calls) == 0, calls)
        time.sleep(1.0)
        ok("新的那一次正常回 1 次", len(calls) == 1, calls)
        if calls:
            ok("迟到的旧定时器没有造成重复", len(calls) == 1 and "第一句" in calls[0]["text"]
               and "第二句" in calls[0]["text"], calls[0]["text"])

        # --- 2.3 群里两个人同时说话，不能并成一句 ---
        calls.clear()
        agent._debounce_push("group_3", text_ev("今天吃啥"), "group", "阿杰", "今天吃啥",
                             ("group", 3), 20001)
        agent._debounce_push("group_3", text_ev("有人在吗"), "group", "小雨", "有人在吗",
                             ("group", 3), 20002)
        time.sleep(1.0)
        ok("群里两个人各回各的（2 次）", len(calls) == 2, calls)
        if len(calls) == 2:
            by_uid = {c["uid"]: c["text"] for c in calls}
            ok("A 的话没被并进 B 的回复", "今天吃啥" in by_uid.get(20001, "")
               and "今天吃啥" not in by_uid.get(20002, ""), by_uid)
    finally:
        agent.process_message, agent.can_reply, agent.merge_delay_sec = orig_pm, orig_can, orig_delay


# ==================== 三、不该画的时候绝不画 ====================
def t_no_draw():
    head("三、不乱画")
    ok("随便聊天不会自动画（maybe_draw 已废）",
       agent.maybe_draw("private_1", "你看这个视频 https://x.y/z", "哈哈") is False)

    src = open(os.path.join(HERE, "agent.py"), encoding="utf-8").read()
    ok("没有「if \"画\" in text」这类硬编码触发", 'if "画" in' not in src)
    ok("没有「想换头像」这类固定词判断", '"想换头像"' not in src and '"你看这个" in' not in src)
    ok("老的画图关键词正则已删除", "画一张|画个|看看你" not in src)

    # 没有形象设定时，自画像工具必须老实承认没画，而不是说「已经发过去了」
    made = {"n": 0}
    import imagegen
    orig_gen = imagegen.generate

    def fake_gen(*a, **kw):
        made["n"] += 1
        return {"ok": False, "msg": "测试里不真画"}

    imagegen.generate = fake_gen
    try:
        r = json.loads(agent._run_chat_tool("draw_self_portrait", {}, session_key="private_9"))
    finally:
        imagegen.generate = orig_gen
    ok("没有形象设定时不硬画", made["n"] == 0, made)
    ok("工具如实回报失败", r.get("ok") is False, r)
    ok("结果里说明了原因（没有形象图/外貌）",
       ("形象图" in r.get("result", "")) or ("外貌" in r.get("result", "")), r)


# ==================== 四、事后自省必须带真实上文 ====================
def t_reflect():
    head("四、事后自省")
    key = "private_10001"
    agent.sessions.add(key, "user", "我今天面试挂了")
    agent.sessions.add(key, "assistant", "啊？怎么会")
    agent.sessions.add(key, "user", "一进去就紧张，话都说不利索")
    agent.sessions.add(key, "assistant", "那确实挺难受的")

    seen = {}
    orig = agent.ask_model_raw

    def fake_raw(prompt, system=None, **kw):
        seen["prompt"] = prompt
        seen["system"] = system
        return "刚那句有点冲，我不是想怼你。"

    agent.ask_model_raw = fake_raw
    try:
        line = agent.post_reflect_apology("你根本不懂", "……行吧", "阿杰", key=key)
    finally:
        agent.ask_model_raw = orig

    ok("prompt 是字符串（不是消息列表）", isinstance(seen.get("prompt"), str),
       type(seen.get("prompt")))
    ok("system 是字符串（不是消息列表）", isinstance(seen.get("system"), str),
       type(seen.get("system")))
    p = seen.get("prompt") or ""
    ok("带上了最近几轮的真实对话", "面试挂了" in p and "话都说不利索" in p, p[:400])
    ok("有【上文内容】标记", "【上文内容】" in p, p[:120])
    ok("明确要求没问题就 SKIP", "SKIP" in p)
    ok("有补话就返回补话", "冲" in line, line)

    # 觉得没问题 → SKIP → 什么都不发
    def fake_skip(prompt, system=None, **kw):
        return "SKIP"

    agent.ask_model_raw = fake_skip
    try:
        line2 = agent.post_reflect_apology("嗯嗯", "好呀", "阿杰", key=key)
    finally:
        agent.ask_model_raw = orig
    ok("模型说 SKIP 时一个字都不补", line2 == "", line2)

    # 反机器人规则必须真的进了 system prompt
    rules = agent.ANTI_BOT_RULES
    ok("反机器人规则：禁止硬拼记忆碎片", "记忆碎片" in rules)
    ok("反机器人规则：禁止客服腔", "客服" in rules and "好的呢" in rules)
    ok("反机器人规则：宁可少说绝不胡编", "胡编" in rules)


# ==================== 调度 ====================
def main():
    only = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    groups = [("card", t_cards), ("debounce", t_debounce),
              ("draw", t_no_draw), ("reflect", t_reflect)]
    for name, fn in groups:
        if only and only not in name:
            continue
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append((name + " 崩溃", str(e)))

    print("")
    print("=" * 62)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for n, extra in FAIL:
            print("  [FAIL] " + n + (("  -> " + str(extra)[:200]) if extra else ""))
        return 1
    print("V1.9.15 自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
