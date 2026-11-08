# -*- coding: utf-8 -*-
"""V1.9.21 自测：QA 对 V1.9.20 的两条遗留。

1. P2-1 语义记忆仍存「连发合并包装文案」—— 写入端三处（semantic.add / extract_and_store /
   extract_events）必须吃用户原话，再加一次历史清扫。
2. P3-1 评论降级成私聊时被拆成两条气泡 —— 必须整条发（no_split=True）。

跑法：python tests\\test_v1921_semantic.py
"""
import os
import sys
import tempfile
import time

TMP = tempfile.mkdtemp(prefix="xy_v1921_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent      # noqa: E402
import semantic   # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s %s" % (name, extra))


class StubMem(object):
    """记下 add_message / log_message，其余一律返回空。"""

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
            if name == "get_comment":
                return {}
            return ""
        return _f

    def user_texts(self):
        return [c[3] for c in self.calls if c[0] == "add_message" and c[2] == "user"]


# ------------------------------------------------------- 1. 写入端只存原话
def t_sem_write():
    print("\n[1] 语义库/抽取都只吃用户原话")
    merged = agent._merge_items([{"text": "在吗"}, {"text": "今天面试挂了"},
                                 {"text": "有点丧"}])
    ok("前提：合并壳存在", "[消息1" in merged and "连续发送" in merged, repr(merged[:50]))

    seen = {"sem": [], "store": [], "events": []}

    def _fake_sem_add(kind, content, **kw):
        seen["sem"].append(content)
        return 1

    real_add = semantic.add
    semantic.add = _fake_sem_add
    real_store, real_events = agent.extract_and_store, agent.extract_events
    real_save = agent.MEMORY_AUTO_SAVE
    agent.extract_and_store = lambda key, who, text: seen["store"].append(text)
    agent.extract_events = lambda key, who, uid, text: seen["events"].append(text)
    agent.MEMORY_AUTO_SAVE = True

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

    ev = {"message_id": 7, "user_id": 10001, "message": []}
    agent.process_message(ev, "private", "private_10001", "阿杰", merged,
                          ("private", 10001), 10001)
    time.sleep(1.2)

    semantic.add = real_add
    agent.extract_and_store, agent.extract_events = real_store, real_events
    agent.MEMORY_AUTO_SAVE = real_save

    ok("语义库写了一条", len(seen["sem"]) == 1, repr(seen["sem"]))
    if seen["sem"]:
        body = str(seen["sem"][0])
        ok("语义库没有 [消息N 壳", "[消息" not in body, repr(body))
        ok("语义库没有「连续发送」", "连续发送" not in body, repr(body))
        ok("语义库存的是三句原话", body.replace("\r\n", "\n") == "在吗\n今天面试挂了\n有点丧",
           repr(body))
    ok("extract_and_store 拿到原话", len(seen["store"]) == 1
       and "[消息" not in str(seen["store"][0]), repr(seen["store"]))
    ok("extract_events 拿到原话", len(seen["events"]) == 1
       and "[消息" not in str(seen["events"][0]), repr(seen["events"]))
    for t in stub.user_texts():
        ok("落库也没有壳：%s" % str(t)[:10], "[消息" not in str(t), repr(t))

    # 单条普通消息：不能被 _memory_text 误伤
    seen["sem"].clear()
    semantic.add = _fake_sem_add
    agent.extract_and_store = lambda key, who, text: seen["store"].append(text)
    agent.extract_events = lambda key, who, uid, text: seen["events"].append(text)
    agent.process_message({"message_id": 8, "user_id": 10001, "message": []},
                          "private", "private_10001", "阿杰", "今天有点冷",
                          ("private", 10001), 10001)
    time.sleep(1.0)
    semantic.add = real_add
    agent.extract_and_store, agent.extract_events = real_store, real_events
    ok("普通消息原样进语义库", seen["sem"][:1] == ["今天有点冷"], repr(seen["sem"]))


# ------------------------------------------------------- 2. 历史污染清扫
def t_purge():
    print("\n[2] 历史遗留的合并文案会被清掉")
    conn = semantic._conn()
    with conn:
        c = conn.cursor()
        c.execute("INSERT INTO vec_memory(kind, session_key, user_id, content, vec, dim, "
                  "created_at, meta) VALUES(?,?,?,?,?,?,?,?)",
                  ("chat", "private_1", "1",
                   "用户连续发送了多条消息，请综合这些内容统一回复：\n[消息1：在吗]\n"
                   "[消息2：今天面试挂了]\n（提示：上面附带的图片和表情包是语气的补充）",
                   None, 0, time.time(), ""))
        bad_id = int(c.lastrowid)
        c.execute("INSERT INTO vec_memory(kind, session_key, user_id, content, vec, dim, "
                  "created_at, meta) VALUES(?,?,?,?,?,?,?,?)",
                  ("chat", "private_1", "1", "在吗\n今天面试挂了\n有点丧", None, 0,
                   time.time(), ""))
        good_id = int(c.lastrowid)
        # 用户自己说到「连续发送了多条消息」这几个字（不该被误删）
        c.execute("INSERT INTO vec_memory(kind, session_key, user_id, content, vec, dim, "
                  "created_at, meta) VALUES(?,?,?,?,?,?,?,?)",
                  ("chat", "private_1", "1",
                   "我刚刚连续发送了多条消息你都没回我，是不是卡了", None, 0,
                   time.time(), ""))
        user_id_like = int(c.lastrowid)

    n = semantic.purge_wrapped()
    ok("删掉了 1 条污染行", n == 1, "n=%s" % n)
    c = semantic._conn().cursor()
    c.execute("SELECT id FROM vec_memory WHERE id=?", (bad_id,))
    ok("污染行确实不在了", c.fetchone() is None)
    c.execute("SELECT content FROM vec_memory WHERE id=?", (good_id,))
    row = c.fetchone()
    ok("干净的行还在", bool(row) and "面试挂了" in row[0], repr(row))
    c.execute("SELECT content FROM vec_memory WHERE id=?", (user_id_like,))
    row2 = c.fetchone()
    ok("用户自己说的一句话没被误删（没有 [消息N）", bool(row2), repr(row2))
    ok("再清一次是 0 条", semantic.purge_wrapped() == 0)


# ------------------------------------------------------- 3. 降级私聊整条发
def t_dm_one_bubble():
    print("\n[3] 评论降级私聊只发一条气泡")
    calls = []
    real_qz = agent.qzone_mod.comment_web
    real_dm = agent.send_private_human
    agent.qzone_mod.comment_web = lambda *a, **k: (False, "NapCat 没返回 cookie")
    agent.send_private_human = lambda uid, text, **kw: calls.append((uid, text, kw))
    agent._CMT_SENT.clear()
    try:
        c = {"id": 501, "moment_tid": "TID_Z", "remote_id": "R501",
             "commenter_id": "20002", "commenter_name": "阿伟",
             "moment_text": "今天有点丧", "content": "怎么了"}
        okk, how, attempted = agent._deliver_comment_reply_ex(c, "嗯，我在。")
    finally:
        agent.qzone_mod.comment_web = real_qz
        agent.send_private_human = real_dm
        agent._CMT_SENT.clear()

    ok("降级成功", okk is True and how == "private", "%s %s" % (okk, how))
    ok("只发了一次", len(calls) == 1, repr(calls))
    if calls:
        uid, text, kw = calls[0]
        ok("发给评论者本人", str(uid) == "20002", str(uid))
        ok("要求整条发（no_split=True）", kw.get("no_split") is True, repr(kw))
        ok("一句话说清在哪儿看到的", "偷偷在这儿回你" in text, repr(text[:40]))
        ok("正文接在同一句后面", text.rstrip().endswith("嗯，我在。"), repr(text[-12:]))


def main():
    print("=" * 60)
    print("V1.9.21 自测：语义记忆不吃包装文案 / 降级私聊整条发")
    print("=" * 60)
    t_sem_write()
    t_purge()
    t_dm_one_bubble()
    print("\n" + "-" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败：%s" % f)
        return 1
    print("V1.9.21 自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
