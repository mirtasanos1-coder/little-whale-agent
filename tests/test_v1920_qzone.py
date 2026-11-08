# -*- coding: utf-8 -*-
"""V1.9.20 自测：说说评论链路重构

用户报的三个问题（一一对应下面四组断言）：
  1. NapCat 没有 reply_qzone_comment，后台一直报「不支持的Api」→ t_napcat_off
  2. 评论被重复回复（接口失败没标记，下一轮又抓一遍）→ t_deliver / t_fail
  3. 回复光秃秃一句，没有「回复 @某人」→ t_format

跑法：python tests\test_v1920_qzone.py
（纯脚本，不需要 pytest；全程打桩，不会真的发评论、也不碰网络）
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="xy_v1920_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP

import agent            # noqa: E402
import memory           # noqa: E402
import qzone            # noqa: E402
import qzone_interact   # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  [OK]   " + name)
    else:
        FAIL.append(name + ("  << " + str(extra) if extra else ""))
        print("  [FAIL] " + name + (("  << " + str(extra)) if extra else ""))


class Recorder(object):
    """记录调用、按预设返回，用来替掉真正的网页协议。"""

    def __init__(self, result=(True, "评论已发出（网页协议）")):
        self.calls = []
        self.result = result

    def __call__(self, tid, text, reply_comment_id="", timeout=30):
        self.calls.append({"tid": tid, "text": text,
                           "reply_comment_id": reply_comment_id})
        return self.result

    def qi(self, tid, uin="", text="", sess=None, reply_comment_id=""):
        self.calls.append({"tid": tid, "text": text,
                           "reply_comment_id": reply_comment_id})
        okk = bool(self.result[0])
        return {"ok": okk, "msg": self.result[1] if len(self.result) > 1 else ""}


def fresh_mem():
    m = memory.Memory()
    agent.mem = m
    agent._CMT_SENT.clear()
    return m


def insert_comment(m, content="哈哈这条太好笑了", who="阿伟", uid="10086",
                   tid="TID_1", remote_id="8801"):
    cid, is_new = m.add_comment(content=content, commenter_id=uid,
                                commenter_name=who, moment_tid=tid,
                                moment_text="今天天气不错", source="test",
                                remote_id=remote_id)
    return cid, is_new


# ---------------------------------------------------------------------------
def t_napcat_off():
    print("\n[1] NapCat 评论接口彻底弃用（不再有「不支持的Api」）")
    probed = []

    def fake_probe(action):
        probed.append(action)
        return False, "不支持的Api " + action

    def boom(*a, **k):
        raise AssertionError("不应该再调用 NapCat 的 call()")

    old_probe, old_call = qzone._probe_action, qzone.call
    qzone._probe_action, qzone.call = fake_probe, boom
    rec = Recorder()
    old_comment = qzone_interact.comment
    qzone_interact.comment = rec.qi
    try:
        qzone.clear_caps()
        c = qzone.caps()
        ok("caps().reply 判为不可用", c.get("reply") is False, c.get("reply"))
        ok("reply_detail 说明已弃用",
           str(c.get("reply_detail") or "") == qzone.COMMENT_DEPRECATED_MSG,
           c.get("reply_detail"))
        ok("comment_via 标成网页协议", c.get("comment_via") == "qzone_web", c)
        ok("不再探 send_qzone_comment", "send_qzone_comment" not in probed, probed)
        ok("不再探 reply_qzone_comment", "reply_qzone_comment" not in probed, probed)

        okk, msg = qzone.reply_comment("TID_X", "CMT_X", "在的在的")
        ok("reply_comment 改走网页协议且成功", okk is True, msg)
        it = rec.calls[-1] if rec.calls else {}
        ok("网页协议拿到的是空间评论 id", it.get("reply_comment_id") == "CMT_X", it)
        ok("正文原样传到网页协议", it.get("text") == "在的在的", it)
        ok("NapCat 的 call() 一次都没被调用", True)
    finally:
        qzone._probe_action, qzone.call = old_probe, old_call
        qzone_interact.comment = old_comment
        qzone.clear_caps()


# ---------------------------------------------------------------------------
def t_format():
    print("\n[2] 回复正文强制带「回复 @昵称 ：」")
    ok("标准格式", agent._comment_at_text("阿伟", "哈哈哈") == "回复 @阿伟 ：哈哈哈",
       agent._comment_at_text("阿伟", "哈哈哈"))
    ok("昵称自带 @ 不会变成两个",
       agent._comment_at_text("@阿伟", "哈哈哈") == "回复 @阿伟 ：哈哈哈",
       agent._comment_at_text("@阿伟", "哈哈哈"))
    ok("空正文返回空串", agent._comment_at_text("阿伟", "   ") == "")
    ok("没昵称时不留空括号", agent._comment_at_text("", "就一句") == "就一句")
    long_body = "啊" * 500
    t = agent._comment_at_text("阿伟", long_body)
    ok("超长正文被截到 300 字以内", len(t) <= 300, len(t))
    ok("截断后仍以称呼开头", t.startswith("回复 @阿伟 ："), t[:20])
    who = agent._comment_who({"commenter_name": "阿伟", "commenter_id": "10086"})
    ok("显示名优先用昵称", who == "阿伟", who)
    who2 = agent._comment_who({"commenter_name": "", "commenter_id": "10086"})
    ok("没昵称就用 QQ 号", who2 == "10086", who2)


# ---------------------------------------------------------------------------
def t_db():
    print("\n[3] 数据库：remote_id 入库 + 按它去重 + 能查状态")
    m = fresh_mem()
    cid, is_new = insert_comment(m, remote_id="8801")
    ok("首次入库是新的", is_new is True)
    cid2, is_new2 = insert_comment(m, content="改了内容也要拦", remote_id="8801")
    ok("同一条说说同一个 remote_id 不算新", is_new2 is False)
    ok("返回的是已有的那条 id", int(cid2) == int(cid), (cid, cid2))
    pend = m.get_pending_comments(limit=10)
    ok("待回复列表带 remote_id",
       bool(pend) and pend[0].get("remote_id") == "8801", pend[:1])
    row = m.get_comment(cid)
    ok("get_comment 能查到状态", row.get("status") == "pending", row)
    m.mark_comment(cid, m.COMMENT_REPLIED, "回复 @阿伟 ：哈哈")
    ok("标记后状态变 replied", m.get_comment(cid).get("status") == "replied")
    ok("标记后不再出现在待回复里",
       all(int(x["id"]) != int(cid) for x in m.get_pending_comments(limit=10)))
    m.close()


# ---------------------------------------------------------------------------
def t_deliver():
    print("\n[4] 发送：走网页协议 + 带 @ 格式 + 只发一次")
    m = fresh_mem()
    cid0, _ = insert_comment(m, tid="TID_DELIVER", remote_id="D8801")
    rec = Recorder()
    old = qzone.comment_web
    qzone.comment_web = rec
    old_text = agent._comment_reply_text
    agent._comment_reply_text = lambda *a, **k: "哈哈哈笑死"
    try:
        sent, skipped = agent._reply_pending_comments(limit=5)
        ok("第一轮发出 1 条", sent == 1, (sent, skipped))
        ok("只调用了一次网页协议", len(rec.calls) == 1, rec.calls)
        body = rec.calls[0]["text"] if rec.calls else ""
        ok("正文带「回复 @阿伟 ：」", body.startswith("回复 @阿伟 ："), body)
        ok("正文里不含接口/API 之类的词",
           not any(w in body for w in ("接口", "API", "NapCat")), body)
        ok("回复挂到空间评论 id 上",
           rec.calls[0]["reply_comment_id"] == "D8801", rec.calls)
        pend = m.get_pending_comments(limit=5)
        ok("发完就标记了 replied（不在待回复里）", not pend, pend)

        # 第二轮：没有任何可回的
        need, _sk = agent._reply_pending_comments(limit=5)
        ok("第二轮不再发", need == 0, need)
        ok("网页协议总共还是只调用一次", len(rec.calls) == 1, len(rec.calls))

        # 把状态强行改回 pending，模拟「接口其实没标记成功」的旧 bug：
        # 这时只靠数据库状态拦不住，得靠 5 分钟内存缓存 + 发送前查库双重把关。
        m.mark_comment(cid0, m.COMMENT_PENDING, "")
        need2, _sk2 = agent._reply_pending_comments(limit=5)
        ok("状态被误改回 pending 时，5 分钟缓存拦住重发", len(rec.calls) == 1
           and need2 == 0, (need2, len(rec.calls), len(rec.calls)))
        # 收尾：把这条恢复成已回，别漏给下一组
        m.mark_comment(cid0, m.COMMENT_REPLIED, "回复 @阿伟 ：哈哈哈笑死")
    finally:
        qzone.comment_web = old
        agent._comment_reply_text = old_text
        m.close()


# ---------------------------------------------------------------------------
def t_fail_marks():
    print("\n[5] 接口报错也要标记，绝不下一轮重发")
    m = fresh_mem()
    cid, _ = insert_comment(m, content="你说说看", who="小张", uid="",
                            tid="TID_2", remote_id="9901")
    rec = Recorder(result=(False, "emotion_cgi_re_feeds 返回 code=-10000"))
    old = qzone.comment_web
    qzone.comment_web = rec
    old_text = agent._comment_reply_text
    agent._comment_reply_text = lambda *a, **k: "行那我再说一遍"
    try:
        agent._reply_pending_comments(limit=5)
        row = m.get_comment(cid)
        ok("报错后状态依然是 replied（防死循环）",
           row.get("status") == "replied", row.get("status"))
        ok("确实尝试发送过", len(rec.calls) == 1, rec.calls)
        agent._reply_pending_comments(limit=5)
        ok("下一轮不再重发", len(rec.calls) == 1, len(rec.calls))
    finally:
        qzone.comment_web = old
        agent._comment_reply_text = old_text
        m.close()


# ---------------------------------------------------------------------------
def t_precheck():
    print("\n[6] 发送前先查状态：已回过的一律跳过")
    m = fresh_mem()
    cid, _ = insert_comment(m, content="在吗", who="老王", tid="TID_3", remote_id="7701")
    m.mark_comment(cid, m.COMMENT_REPLIED, "回复 @老王 ：在")
    rec = Recorder()
    old = qzone.comment_web
    qzone.comment_web = rec
    try:
        c = m.get_comment(cid)
        okk, how, sent = agent._deliver_comment_reply_ex(c, "又被叫了一次")
        ok("已 replied 的不会再发", okk is False and sent is False, (okk, how, sent))
        ok("网页协议没有被调用", len(rec.calls) == 0, rec.calls)
    finally:
        qzone.comment_web = old
        m.close()


def t_remote_back():
    print("\n[7] 发出去之后把空间评论 id 记回来（remote_id 回填）")
    m = fresh_mem()
    cid, _ = insert_comment(m, content="你这个真好看", who="阿伟", tid="TID_R",
                            remote_id="R111")

    def fake_qi(tid, uin="", text="", sess=None, reply_comment_id=""):
        return {"ok": True, "msg": "评论成功", "comment_id": "S8888"}

    old_qi = qzone_interact.comment
    old_text = agent._comment_reply_text
    qzone_interact.comment = fake_qi
    agent._comment_reply_text = lambda *a, **k: "是吧我也觉得"
    try:
        okk, msg = qzone.comment_web("TID_R", "回复 @阿伟 ：是吧")
        ok("comment_web 成功", okk is True, msg)
        ok("LAST_COMMENT_ID 拿到空间评论 id",
           qzone.LAST_COMMENT_ID == "S8888", qzone.LAST_COMMENT_ID)
        qzone.LAST_COMMENT_ID = ""

        sent, _sk = agent._reply_pending_comments(limit=5)
        ok("发出 1 条", sent == 1, sent)
        c = m.conn.cursor()
        c.execute("""SELECT remote_id, content FROM moment_comments
                     WHERE direction='out' ORDER BY id DESC LIMIT 1""")
        row = c.fetchone() or ("", "")
        ok("我们自己那条评论回填了 remote_id", row[0] == "S8888", row)
        ok("回填的正文是带称呼的版本",
           str(row[1]).startswith("回复 @阿伟 ："), row[1])
    finally:
        qzone_interact.comment = old_qi
        agent._comment_reply_text = old_text
        m.close()

    m2 = memory.Memory()
    rid, _ = insert_comment(m2, tid="TID_F", remote_id="F555")
    hit = m2.find_comment_by_remote("TID_F", "F555")
    ok("find_comment_by_remote 能按空间 id 找到",
       hit.get("id") == int(rid), hit)
    ok("remote_id 为空时返回空字典",
       m2.find_comment_by_remote("TID_F", "") == {})
    m2.close()


def main():
    print("=" * 60)
    print("V1.9.20 自测：说说评论链路（NapCat 接口弃用 / @ 格式 / 防重复）")
    print("=" * 60)
    t_napcat_off()
    t_format()
    t_db()
    t_deliver()
    t_fail_marks()
    t_precheck()
    t_remote_back()
    print("\n" + "-" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAILED: " + f)
        return 1
    print("V1.9.20 自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
