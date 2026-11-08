# -*- coding: utf-8 -*-
"""V1.9.16 自测：QA 终审提的三个 P3 尾巴。

1. 内部提示词污染长期记忆 —— 落库/写历史必须是原话，不能带「[消息1：…]」合并壳。
2. 自省发言人前缀重复 —— 历史里已有「阿杰：」就别再拼一次。
3. B 站小程序卡片标题 —— 优先读 desc，别把「哔哩哔哩」当视频标题。

跑法：python tests\\test_v1916_fix.py
"""
import json
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="xy_v1916_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s %s" % (name, extra))


# ---------------------------------------------------------------- 1. 记忆污染
class StubMem(object):
    """记下所有 add_message / log_message 的入参，别的调用一律返回空。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _f(*a, **k):
            if name in ("add_message", "log_message"):
                self.calls.append((name,) + tuple(a))
            if name == "count_messages":
                return 0
            if name == "get_earliest_messages":
                return []
            return ""
        return _f

    def user_texts(self):
        return [c[3] for c in self.calls if c[0] == "add_message" and c[2] == "user"]


def t_memory_text():
    print("\n[1] 记忆里只留原话")
    merged = agent._merge_items([{"text": "在吗"}, {"text": "今天面试挂了"}])
    ok("合并壳确实存在（前提）", "[消息1" in merged, repr(merged[:40]))
    clean = agent._memory_text(merged)
    ok("剥壳后是第一句", clean.splitlines()[0] == "在吗", repr(clean))
    ok("剥壳后多句用换行", clean == "在吗\n今天面试挂了", repr(clean))
    ok("剥壳后没有[消息N]", "[消息" not in clean)
    ok("剥壳后没有包装词", "连续发送" not in clean)

    media = agent._merge_items([{"text": "[图片]", "marks": ["[图片]"]},
                                {"text": "[表情包：狗头]", "marks": ["[表情包：狗头]"]}])
    mclean = agent._memory_text(media)
    ok("纯媒体合并 → 只留 [图片] 标记", "[图片]" in mclean and "连续发送" not in mclean,
       repr(mclean))

    plain = "你好呀，今天好累"
    ok("普通消息原样返回", agent._memory_text(plain) == plain)
    ok("空串不炸", agent._memory_text("") == "" and agent._memory_text(None) == "")

    # 用户在一条消息里自己换了行：别把「[消息2：第二行」当成正文留在记忆里
    multi = agent._merge_items([{"text": "第一句"}, {"text": "第二行\n第三行"}])
    mclean2 = agent._memory_text(multi)
    ok("多行消息不被截断", "第二行" in mclean2 and "第三行" in mclean2, repr(mclean2))
    ok("多行消息不留壳", "[消息" not in mclean2, repr(mclean2))

    # 端到端：走一遍 process_message，看真正写库/写历史的到底是什么
    stub = StubMem()
    agent.mem = stub
    agent.MEMORY_ENABLED = True
    agent.ask_model = lambda *a, **k: "嗯嗯，我在听"
    agent.set_typing = lambda *a, **k: None
    agent.human_delay = lambda *a, **k: None
    agent.send_private_human = lambda *a, **k: None
    agent.send_group_human = lambda *a, **k: None
    agent.maybe_send_sticker = lambda *a, **k: None
    agent.quote_target = lambda *a, **k: None
    agent.sessions = agent.SessionStore()

    ev = {"message_id": 1, "user_id": 10001, "message": []}
    agent.process_message(ev, "private", "private_10001", "阿杰", merged,
                          ("private", 10001), 10001)
    time.sleep(0.8)

    hist = agent.sessions.get("private_10001") or []
    hist_user = [m for m in hist if m.get("role") == "user"]
    ok("历史里写进了 user 消息", len(hist_user) == 1, repr(hist[:2]))
    if hist_user:
        h = hist_user[0].get("content") or ""
        ok("历史没有合并壳", "[消息" not in h and "连续发送" not in h, repr(h))
        ok("历史保留说话人前缀", h.startswith("阿杰："), repr(h))
    db_users = stub.user_texts()
    ok("落库有 user 消息", len(db_users) >= 1, repr(db_users))
    for t in db_users:
        ok("落库内容不含[消息N]：%s" % t[:12], "[消息" not in str(t), repr(t))
        ok("落库内容不含包装词", "连续发送" not in str(t), repr(t))


# ------------------------------------------------------------ 2. 自省前缀重复
def t_reflect_prefix():
    print("\n[2] 自省不重复前缀")
    agent.sessions = agent.SessionStore()
    agent.sessions.add("private_20002", "user", "阿杰：我今天面试挂了")
    agent.sessions.add("private_20002", "assistant", "你：啊？怎么会")
    ctx = agent._reflect_context("private_20002", "阿杰")
    ok("上文取到了两行", len(ctx.splitlines()) == 2, repr(ctx))
    ok("用户行没双前缀", "阿杰：阿杰：" not in ctx, repr(ctx))
    ok("助手行没双前缀", "你：你：" not in ctx, repr(ctx))
    ok("用户行仍是带前缀的", "阿杰：我今天面试挂了" in ctx, repr(ctx))

    # 没前缀的历史要补上前缀
    agent.sessions = agent.SessionStore()
    agent.sessions.add("private_20003", "user", "我今天面试挂了")
    ctx2 = agent._reflect_context("private_20003", "小美")
    ok("无前缀时补上昵称", ctx2.startswith("小美："), repr(ctx2))

    # 直接检查发给模型的那段 prompt
    seen = {}

    def _fake_raw(prompt, **k):
        seen["prompt"] = prompt
        return "SKIP"

    agent.sessions = agent.SessionStore()
    agent.sessions.add("private_20004", "user", "阿杰：我今天面试挂了")
    agent.sessions.add("private_20004", "assistant", "你：啊？怎么会")
    old = agent.ask_model_raw
    agent.ask_model_raw = _fake_raw
    try:
        r = agent.post_reflect_apology("我今天面试挂了", "啊？怎么会",
                                       sender_name="阿杰", key="private_20004")
    finally:
        agent.ask_model_raw = old
    p = seen.get("prompt", "")
    ok("自省 prompt 带【上文内容】", "【上文内容】" in p, repr(p[:60]))
    ok("自省 prompt 无双前缀", "阿杰：阿杰：" not in p, repr(p[:200]))
    ok("SKIP 时不补话", not (r or "").strip(), repr(r))


# ------------------------------------------------------------ 3. B站卡片标题
def _card_json(obj):
    return {"type": "json", "data": {"data": json.dumps(obj, ensure_ascii=False)}}


def t_card_bili():
    print("\n[3] B 站卡片读真标题")
    bili = {
        "app": "com.tencent.miniapp_01",
        "meta": {"detail_1": {"appid": "1109937557", "title": "哔哩哔哩",
                              "desc": "这只猫居然会开门，笑死我了",
                              "qqdocurl": "https://b23.tv/abc123",
                              "preview": "https://i0.hdslb.com/x.jpg"}},
    }
    txt = agent.card_segment_text(_card_json(bili))
    ok("卡片文本格式正确", txt.startswith("[用户分享了卡片/视频："), repr(txt))
    ok("用的是 desc 真标题", "这只猫居然会开门" in txt, repr(txt))
    ok("不再只显示平台名", not txt.startswith("[用户分享了卡片/视频：哔哩哔哩，"), repr(txt))
    ok("链接保留", "https://b23.tv/abc123" in txt, repr(txt))

    # desc 里带「哔哩哔哩 - 」前缀时要剥掉
    bili2 = {"meta": {"detail_1": {"title": "哔哩哔哩",
                                   "desc": "哔哩哔哩 - 三分钟学会红烧肉",
                                   "qqdocurl": "https://b23.tv/x"}}}
    t2 = agent.card_segment_text(_card_json(bili2))
    ok("剥掉平台名前缀", "三分钟学会红烧肉" in t2 and "- 三分钟" not in t2, repr(t2))

    # 普通新闻卡片：本来就有真标题，别被 desc 顶掉
    news = {"app": "com.tencent.news", "meta": {"news": {
        "title": "台风明天登陆", "desc": "气象台已经发布黄色预警",
        "jumpUrl": "https://news.qq.com/a/1"}}}
    t3 = agent.card_segment_text(_card_json(news))
    ok("普通卡片标题不被顶掉", "台风明天登陆" in t3, repr(t3))
    ok("普通卡片链接保留", "news.qq.com" in t3, repr(t3))

    # 平台名判定本身
    ok("哔哩哔哩是平台名", agent._is_platform_name("哔哩哔哩"))
    ok("B站是平台名", agent._is_platform_name("B站"))
    ok("真标题不是平台名", not agent._is_platform_name("这只猫居然会开门，笑死我了"))


def main():
    print("=" * 60)
    print("小鲸鱼 V1.9.16 自测（QA P3 三条）")
    print("=" * 60)
    t_memory_text()
    t_reflect_prefix()
    t_card_bili()
    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for n in FAIL:
            print("  失败：%s" % n)
        print("V1.9.16 自测未通过")
        return 1
    print("V1.9.16 自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
