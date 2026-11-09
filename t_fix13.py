# -*- coding: utf-8 -*-
"""V1.9.13 修正：P1（合并壳误触发图片复用）/ P3-1（只发表情）/ P3-2（情绪判定吃壳）功能验证。"""
import os
import shutil
import sys
import tempfile

ROOT = r"D:\临时文件\小鲸鱼_程序包\小鲸鱼"
sys.path.insert(0, ROOT)

D = os.path.join(tempfile.gettempdir(), "xjy_t_fix13")
shutil.rmtree(D, ignore_errors=True)
os.makedirs(D, exist_ok=True)
os.environ["QQ_AGENT_DATA_DIR"] = D

import agent  # noqa: E402

PASS, FAIL = [], []


def chk(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + (("  <<< " + str(detail)) if (detail and not ok) else ""))


# ---------------- 1. _merge_items ----------------
m = agent._merge_items
two = m([{"text": "你看这个"}, {"text": "笑死我了"}])
chk("两条合并带壳", two.startswith(agent._MERGE_HEAD) and "[消息1：" in two and "[消息2：" in two, two[:60])
single = m([{"text": "就一句"}])
chk("单条不加壳", single == "就一句", single)
only_media = m([{"text": "[表情包：狗头]"}, {"text": "[图片]"}])
chk("只有表情/图片 → 一句说明（P3-1）", only_media.startswith("（对方连着发了几条，没有文字")
    and agent._MERGE_HEAD not in only_media and "狗头" in only_media, only_media[:80])
mixed = m([{"text": "哈哈 [表情包：狗头]"}, {"text": "真的假的"}])
chk("文字+表情仍走合并壳", mixed.startswith(agent._MERGE_HEAD), mixed[:50])

# ---------------- 2. _plain_ask_text ----------------
p = agent._plain_ask_text
chk("剥壳：两条纯文本里没有「图」", "图" not in p(two), p(two))
chk("剥壳：媒体标记被去掉", "[图片]" not in p("[消息1：[图片]]") and "表情包" not in p("[消息1：[表情包：狗头]]"))
chk("问图时仍含「图」", "图" in p(m([{"text": "你看"}, {"text": "刚才那张图怎么样"}])), p(m([{"text": "你看"}, {"text": "刚才那张图怎么样"}])))

# ---------------- 3. 真走 process_message ----------------
CAP = {}


def fake_ask_model(hist, prompt, images=None, **kw):
    CAP["prompt"] = prompt
    CAP["images"] = images or []
    return "好呀"


def fake_send(sk, text, *a, **k):
    CAP.setdefault("sent", []).append(text)
    return True, "", {}


agent.ask_model = fake_ask_model
agent.ENABLE_VISION = True

# 本会话早先发过一张图（QA 复现前提）
agent.sessions.set_images("private_10001", ["data:image/png;base64,AAAA"])

# 3a. 发过图之后，连发两条纯文本 → 绝不能再复用那张图
CAP.clear()
merged_txt = agent._merge_items([{"text": "今天好累啊"}, {"text": "刚下班"}])
agent.process_message({}, "private", "private_10001", "大肥鱼", merged_txt, ("private", "10001"), "10001")
pr = CAP.get("prompt") or ""
chk("发过图后连发纯文本 → 不复用旧图（P1）", not CAP.get("images"), CAP.get("images"))
chk("提示词里没有「指的是你上面看到的那张图片」", "指的是你上面看到的那张图片" not in pr, pr[:120])

# 3b. 真的在问那张图 → 应当复用
CAP.clear()
ask_txt = agent._merge_items([{"text": "你看"}, {"text": "刚才那张图你觉得咋样"}])
agent.process_message({}, "private", "private_10001", "大肥鱼", ask_txt, ("private", "10001"), "10001")
pr2 = CAP.get("prompt") or ""
chk("真的问图 → 复用旧图", bool(CAP.get("images")), CAP.get("images"))
chk("真的问图 → 带「指的是你上面看到的那张图片」", "指的是你上面看到的那张图片" in pr2, pr2[:120])

# ---------------- 4. 情绪判定不吃合并壳（P3-2） ----------------
import emotion_engine  # noqa: E402

SEEN = {}


def fake_analyze(text, *a, **k):
    SEEN["text"] = text
    return {"emotion": "neutral", "weight": 30}


def fake_urgency(text, *a, **k):
    SEEN.setdefault("urg", []).append(text)
    return 0.0


emotion_engine.analyze = fake_analyze
emotion_engine.urgency = fake_urgency
CAP.clear()
agent.process_message({}, "private", "private_10001", "大肥鱼",
                      agent._merge_items([{"text": "在吗"}, {"text": "看看这个"}]), "", "10001")
et = SEEN.get("text") or ""
chk("情绪判定拿到的是用户原话（P3-2）", bool(et) and agent._MERGE_HEAD not in et, et[:80])
chk("紧急度也没吃合并壳", all(agent._MERGE_HEAD not in (x or "") for x in SEEN.get("urg", [])), SEEN.get("urg"))

# ---------------- 5. 静态收尾 ----------------
src = open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read()
chk("asking_about_image 用 _plain_ask_text", "_ask_txt = _plain_ask_text(raw_text)" in src)
chk("_MERGE_TAIL 不再写「里面的图片」", "里面的图片和表情包" not in src)

print("\n=== %d PASS / %d FAIL ===" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    sys.exit(1)
