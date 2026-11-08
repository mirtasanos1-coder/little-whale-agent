# -*- coding: utf-8 -*-
"""V1.9.19 自测：唱歌不说技术词 / 不念百科 / 主动问候带上下文。

三个真实故障（用户实机截图）：
  1. 点《告白气球》→ 搜到的是小说简介，55 秒语音在念「阿Q书城言情小说…」；
  2. 语音发完又补一句「唱完了（没接音乐接口，先这么念给你听）」；
  3. 用户说过「已经吃了」，主动问候还问「中午了，吃了吗？」

跑法：python tests\\test_v1919_sing.py
"""
import os
import sys
import json
import time
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["QQ_AGENT_DATA_DIR"] = tempfile.mkdtemp(prefix="xy_v1919_")

import agent
import sing

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra and not cond else ""))


# 用户实机听到的那段（截图 OCR 出来的原文，一字未改）
BOOK_BLOB = """阿Q书城言情小说告白告白内容简介大学时，周京泽和许随是云泥之别，永远不会有交集的两人，一个
随性浪荡，众星捧月，一个乖巧安静，容易被忽略。他在图书馆写完一张又一张试卷时，无意窥见
他与别人的暧昧风月，也目睹过他换了一个又一个的女朋友。一次聚会，许随醉酒，在一场起哄中鼓
起勇气告白，周京泽愣了一下，随即挑起唇角，漫不经心道，不好意思啊，你太乖了。加入书架，开
始阅读。"""

REAL_LYRICS = """塞纳河畔 左岸的咖啡
我手一杯 品尝你的美
留下唇印的嘴
花店玫瑰 名字写错谁
告白气球 风吹到对街
微笑在天上飞
你说你有点难追
想让我知难而退"""


# ==================== 一、歌词：绝不准念百科 ====================
def t_lyrics():
    print("[1] 歌词提取：百科/小说简介必须被判死")
    lines = sing.parse_lyrics(BOOK_BLOB, max_lines=14)
    ok("小说简介整段被丢弃", lines == [], lines[:3])
    ok("简介打分过线", sing._prose_score(BOOK_BLOB) >= 3, sing._prose_score(BOOK_BLOB))

    real = sing.parse_lyrics(REAL_LYRICS, max_lines=14)
    ok("真歌词保留（>=5 行）", len(real) >= 5, len(real))
    ok("真歌词不误判为散文", not sing._looks_like_prose(REAL_LYRICS),
       sing._prose_score(REAL_LYRICS))
    ok("真歌词里没混进「简介」", all("简介" not in x for x in real), real)

    # 搜索结果整段是简介 → lyrics_from_search 必须返回空
    class _FakeCU:
        def __init__(self, payload):
            self.payload = payload

        def tool_web_search(self, q, *a, **k):
            return {"ok": True, "items": [{"title": "告白气球", "snippet": self.payload,
                                           "url": "http://example.invalid/x"}]}

    real_cu = sys.modules.get("computer_use")
    sys.modules["computer_use"] = _FakeCU(BOOK_BLOB)
    try:
        got, note = sing.lyrics_from_search("告白气球", max_lines=12)
        ok("搜到简介时判定没搜到", got == [], got[:3])
        ok("note 说明是简介不是歌词", ("简介" in note or "百科" in note), note)
    finally:
        if real_cu is not None:
            sys.modules["computer_use"] = real_cu

    # 简介里夹带一句「作者：xxx」这种，也不能拿去唱
    ok("带作者行也不念", sing.parse_lyrics("作者：某某某\n" + BOOK_BLOB, 10) == [])


# ==================== 二、唱歌：不发免责声明 ====================
class _Rec(object):
    def __init__(self):
        self.items = []

    def text(self, s):
        self.items.append(("text", str(s)))

    def voice(self, p):
        self.items.append(("voice", str(p)))


def t_sing_send():
    print("[2] 唱歌发送：只发语音 + 一句人话，绝不出现技术词")
    rec = _Rec()
    real_sing = sing.sing
    real_voice = agent.send_voice
    real_send = agent.send_to_session
    real_sleep = time.sleep
    sing.sing = lambda *a, **k: {"ok": True, "path": "C:/tmp/x.mp3", "engine": "read",
                                 "note": "搜到 12 行", "lines": ["a"] * 12}
    agent.send_voice = lambda **k: (rec.voice(k.get("path")), True)[1]
    agent.send_to_session = lambda k, t, **kw: (rec.text(t), True)[1]
    time.sleep = lambda *a, **k: None
    try:
        r = agent._sing_perform("告白气球", "private_1", "private", "1",
                                intro="咳咳，清唱一段送给你~")
    finally:
        sing.sing = real_sing
        agent.send_voice = real_voice
        agent.send_to_session = real_send
        time.sleep = real_sleep

    ok("返回 ok=True", bool(r.get("ok")), r)
    ok("报了唱了几行", int(r.get("lines") or 0) == 12, r)
    kinds = [k for k, _ in rec.items]
    ok("先说话再发语音", kinds == ["text", "voice"], rec.items)
    said = " ".join(t for k, t in rec.items if k == "text")
    for bad in ("接口", "API", "TTS", "朗读", "合成", "念给你听", "没接"):
        ok("发出的话里没有「%s」" % bad, bad not in said, said)
    ok("intro 原样发出", "清唱一段" in said, said)


def t_sing_fail():
    print("[3] 搜不到歌词：不许硬念，交给模型说人话")
    sent = _Rec()
    real_sing = sing.sing
    real_send = agent.send_to_session
    sing.sing = lambda *a, **k: {"ok": False, "path": "", "engine": "read",
                                 "note": "搜到的是简介/百科，不是歌词", "lines": []}
    agent.send_to_session = lambda k, t, **kw: (sent.text(t), True)[1]
    try:
        r = agent._sing_perform("告白气球", "private_1", "private", "1")
        res = json.loads(agent._run_chat_tool("sing_song", {"song": "告白气球"},
                                              session_key="private_1"))
    finally:
        sing.sing = real_sing
        agent.send_to_session = real_send
    ok("返回 ok=False", not r.get("ok"), r)
    ok("失败时不自己发消息（交给模型）", sent.items == [], sent.items)
    ok("工具结果 ok=False", res.get("ok") is False, res)
    ok("工具结果叫模型别硬念", "别硬念" in res.get("result", ""), res.get("result"))


def t_sing_tool_ok():
    print("[4] 唱成功后：工具结果引导她求夸奖，且只许一句")
    real_sing = sing.sing
    real_voice = agent.send_voice
    real_send = agent.send_to_session
    sing.sing = lambda *a, **k: {"ok": True, "path": "C:/tmp/x.mp3", "engine": "read",
                                 "note": "", "lines": ["a"] * 8}
    agent.send_voice = lambda **k: True
    agent.send_to_session = lambda k, t, **kw: True
    try:
        res = json.loads(agent._run_chat_tool(
            "sing_song", {"song": "告白气球", "intro": "清唱一段"},
            session_key="private_1"))
    finally:
        sing.sing = real_sing
        agent.send_voice = real_voice
        agent.send_to_session = real_send
    body = res.get("result", "")
    ok("ok=True", res.get("ok") is True, res)
    ok("提示求夸奖", "没跑调" in body, body)
    ok("提示只补一句", "只说一句" in body, body)
    ok("提示别提接口", "不要提接口" in body, body)


# ==================== 三、主动问候：必须带上下文 ====================
def t_greeting():
    print("[5] 主动问候：刚聊过「吃了」就不许再问「吃了吗」")
    hist = [{"role": "user", "content": "家人：我吃过饭了", "ts": 1},
            {"role": "assistant", "content": "好，那不催你了", "ts": 2}]
    real_recent = agent._recent_dialog
    real_ask = agent.ask_model
    agent._recent_dialog = lambda sk, turns=4: (hist, "对方：我吃过饭了\n你：好，那不催你了")

    calls = {"n": 0}
    seen_prompts = []

    def fake_ask(h, prompt, **kw):
        calls["n"] += 1
        seen_prompts.append(prompt)
        if calls["n"] == 1:
            return "中午了，吃了吗？"
        return "那你下午忙啥呢"

    agent.ask_model = fake_ask
    try:
        txt = agent._greeting_text("12:00", "private_1")
    finally:
        agent._recent_dialog = real_recent
        agent.ask_model = real_ask

    ok("送进去的 prompt 带最近对话", "我吃过饭了" in seen_prompts[0], seen_prompts[:1])
    ok("prompt 明确要求别重复", "重复" in seen_prompts[0], seen_prompts[:1])
    ok("重复的那句被拦下重写", calls["n"] == 2, calls)
    ok("最终没问吃了吗", "吃" not in txt, txt)
    ok("最终结果不是空", bool(txt.strip()), txt)


def t_greeting_fallback():
    print("[6] 两次都重复 → 用泛化话，也不重复问")
    hist = [{"role": "user", "content": "我吃过了", "ts": 1}]
    real_recent = agent._recent_dialog
    real_ask = agent.ask_model
    agent._recent_dialog = lambda sk, turns=4: (hist, "对方：我吃过了")
    agent.ask_model = lambda h, p, **kw: "吃饭了吗？"
    try:
        txt = agent._greeting_text("12:00", "private_1")
    finally:
        agent._recent_dialog = real_recent
        agent.ask_model = real_ask
    ok("兜底也不问吃了吗", "吃" not in txt, txt)
    ok("兜底话非空", bool(txt.strip()), txt)

    # 没上下文时照常走
    agent._recent_dialog = lambda sk, turns=4: ([], "")
    agent.ask_model = lambda h, p, **kw: "在干嘛呢"
    try:
        txt2 = agent._greeting_text("18:00", "private_1")
    finally:
        agent._recent_dialog = real_recent
        agent.ask_model = real_ask
    ok("没有历史时正常问候", txt2 == "在干嘛呢", txt2)


def t_checkin_ctx():
    print("[7] 查岗问句也带上下文")
    real_recent = agent._recent_dialog
    real_ask = agent.ask_model
    got = {}

    def fake_ask(h, p, **kw):
        got["p"] = p
        return "面试结果出了吗"

    agent._recent_dialog = lambda sk, turns=3: ([{"role": "user", "content": "明天面试"}],
                                                "对方：明天面试")
    agent.ask_model = fake_ask
    try:
        txt = agent._event_checkin_text("明天面试", 3.0, "private_1")
    finally:
        agent._recent_dialog = real_recent
        agent.ask_model = real_ask
    ok("查岗 prompt 带记录", "明天面试" in got.get("p", ""), got.get("p", "")[:80])
    ok("查岗结果原样返回", txt == "面试结果出了吗", txt)


def main():
    print("=" * 60)
    print("V1.9.19 自测：唱歌 / 歌词 / 主动问候")
    print("=" * 60)
    t_lyrics()
    t_sing_send()
    t_sing_fail()
    t_sing_tool_ok()
    t_greeting()
    t_greeting_fallback()
    t_checkin_ctx()
    print("-" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for n in FAIL:
            print("  失败：" + n)
        return 1
    print("V1.9.19 自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
