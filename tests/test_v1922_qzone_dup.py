# -*- coding: utf-8 -*-
"""V1.9.22 自测：空间评论不再「每 10 分钟重复回一遍」。

用户现场：同一条评论被反复回复（截图里 10:46 / 11:07 / 11:17 / 11:28 / 11:38
每隔约 10 分钟两条）。根因两条：

  1. `_feed_state()` 用的是 `load_state().setdefault("feed", …)`，而 load_state /
     save_state 都不带 "feed" —— 每轮都是全新的空状态，`replied` 和 `reply_at`
     存了等于没存，于是同一批评论每轮都被当成「没回过」。
  2. 「动态回评论」和「评论循环」读的是同一批网页评论，各发一遍。

本测试盯三件事：
  A. feed 状态真落盘（agent_state.json 里有 feed.replied）；
  B. 评论循环开着时，动态循环只入库不发送；
  C. 一轮发过之后，再跑一轮（含重启式重新加载状态）一律 0 发送。

跑法：python tests\\test_v1922_qzone_dup.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="xy_v1922_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP

import agent  # noqa: E402
import memory as memmod  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  [OK]   " + name)
    else:
        FAIL.append(name)
        print("  [FAIL] " + name + (("  <- " + str(extra)) if extra else ""))


# ---------------- 打桩：qzone_interact + 发送 + 生成 ----------------

class FakeQi(object):
    """假的 qzone_interact：固定返回一条别人的评论。"""

    def __init__(self):
        self.comments = []
        self.calls = 0

    def session(self):
        return {"ok": True, "uin": "2145216414", "nickname": "小鲸鱼", "gtk": "1"}

    def list_feeds(self, uin="", num=5, pos=0):
        return {"ok": True, "feeds": [{"tid": "T1", "content": "晚饭吃大白饭",
                                       "cmtnum": len(self.comments)}]}

    def comment_list(self, tid, uin="", sess=None, include_mine=False):
        self.calls += 1
        return {"ok": True, "comments": list(self.comments)}


QI = FakeQi()
SENT = []


def _fake_comment_web(tid, text, reply_comment_id="", timeout=30):
    SENT.append({"tid": tid, "text": text, "reply_comment_id": reply_comment_id})
    agent.qzone_mod.LAST_COMMENT_ID = "S%d" % len(SENT)
    return True, "评论成功"


def _fake_ask_model_raw(prompt, **kw):
    return "哈哈哈被你抓到了"


agent._qi = lambda: QI
agent.qzone_mod.comment_web = _fake_comment_web
agent.qzone_mod.LAST_COMMENT_ID = ""
agent.ask_model_raw = _fake_ask_model_raw
agent._feed_flag = lambda key, default=True: True
agent._cmt_sent_recent  # 用真的，不清
agent._CMT_SENT.clear()


def set_comment_loop(on):
    agent._live_cfg = lambda k, d=None: (bool(on) if k == "moment_comment_enabled" else d)


def state_feed():
    try:
        with open(os.path.join(TMP, "agent_state.json"), encoding="utf-8") as f:
            return json.load(f).get("feed") or {}
    except Exception:
        return {}


# ---------------- A. 评论循环开着：动态循环只入库 ----------------

def t_ingest_only():
    print("[1] 评论循环开着时，动态循环只入库不发送")
    set_comment_loop(True)
    QI.comments = [{"comment_id": "CM1", "uin": "3280735835", "name": "安瑆.雨",
                    "content": "猪"}]
    SENT[:] = []
    n, msg = agent.feed_comment_round(force=True)
    ok("动态循环没发（0 条）", n == 0, n)
    ok("comment_web 一次没调用", len(SENT) == 0, SENT)
    pend = [c for c in agent.mem.get_pending_comments(limit=20)
            if c["content"] == "猪"]
    ok("评论已灌进库待回复", len(pend) == 1, len(pend))
    ok("入库带了 remote_id", bool(pend and pend[0].get("remote_id") == "CM1"),
       pend[0].get("remote_id") if pend else "")
    ok("feed.replied 记住了这条", "CM1" in (state_feed().get("replied") or []),
       state_feed())


def t_comment_loop_once():
    print("[2] 评论循环回一次，之后不再回")
    n = 0
    sent, skipped = agent._reply_pending_comments(limit=5)
    ok("这一轮发了 1 条", sent == 1, (sent, skipped))
    ok("正文带「回复 @安瑆.雨 ：」",
       bool(SENT) and SENT[0]["text"].startswith("回复 @安瑆.雨 ："), SENT[:1])
    ok("挂到了对方评论下（reply_comment_id）",
       bool(SENT) and SENT[0]["reply_comment_id"] == "CM1", SENT[:1])
    pend = [c for c in agent.mem.get_pending_comments(limit=20) if c["content"] == "猪"]
    ok("回复后不再是 pending", len(pend) == 0, len(pend))
    SENT[:] = []
    sent2, _ = agent._reply_pending_comments(limit=5)
    ok("第二轮 0 发送", sent2 == 0, sent2)


def t_dynamic_round_again():
    print("[3] 再跑动态循环：还是 0 发送")
    set_comment_loop(True)
    SENT[:] = []
    n, _ = agent.feed_comment_round(force=True)
    ok("动态循环 0 发送", n == 0 and not SENT, (n, SENT))
    pend = [c for c in agent.mem.get_pending_comments(limit=20) if c["content"] == "猪"]
    ok("也没有重新入库成 pending", len(pend) == 0, len(pend))


# ---------------- B. 评论循环关着：动态循环自己发，但只发一次 ----------------

def t_dynamic_only_once():
    print("[4] 评论循环关着：动态循环自己发，但只发一次")
    set_comment_loop(False)
    QI.comments = [{"comment_id": "CM9", "uin": "3280735835", "name": "安瑆.雨",
                    "content": "断片了"}]
    SENT[:] = []
    n1, _ = agent.feed_comment_round(force=True)
    ok("第一轮发了 1 条", n1 == 1, n1)
    ok("内容带称呼", bool(SENT) and SENT[0]["text"].startswith("回复 @安瑆.雨 ："), SENT[:1])
    n2, _ = agent.feed_comment_round(force=True)
    ok("第二轮 0 发送（状态记住了）", n2 == 0, n2)
    ok("总发送次数仍是 1", len(SENT) == 1, len(SENT))
    ok("feed.replied 里有 CM9", "CM9" in (state_feed().get("replied") or []),
       state_feed().get("replied"))


def t_reload_state():
    print("[5] 重启式重载状态：replied / reply_at 都还在")
    st = agent.load_state()
    feed = st.get("feed") or {}
    ok("load_state 带 feed", bool(feed), feed)
    ok("replied 跨进程可读", "CM9" in [str(x) for x in (feed.get("replied") or [])], feed)
    ok("reply_at 已记录", float(feed.get("reply_at") or 0) > 0, feed.get("reply_at"))


# ---------------- C. 自己发的回复不能被当成新评论 ----------------

def t_skip_echo():
    print("[6] 读回来的「回复 @…」不算新评论")
    set_comment_loop(False)
    QI.comments = [{"comment_id": "CM10", "uin": "", "name": "",
                    "content": "回复 @安瑆.雨 ：我是鲸鱼嘛"}]
    SENT[:] = []
    n, _ = agent.feed_comment_round(force=True)
    ok("没回自己的回复", n == 0 and not SENT, (n, SENT))


def main():
    print("=" * 60)
    print("V1.9.22 自测：空间评论去重")
    print("=" * 60)
    t_ingest_only()
    t_comment_loop_once()
    t_dynamic_round_again()
    t_dynamic_only_once()
    t_reload_state()
    t_skip_echo()
    print("-" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败：" + "、".join(FAIL))
        return 1
    print("V1.9.22 自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
