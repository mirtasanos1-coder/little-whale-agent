# -*- coding: utf-8 -*-
"""V1.9.23 自测（二）：学习真的能学到了 + 防抖默认 4.5 秒。

背景（用户 2026-11-05 反馈）：
  1）「学习一直都没有触发过」—— 日志里每轮都在跑，但永远输出「聊天太少，先多聊几句」。
     真因是 learn_from_dialog() 去调 mem.list_messages()，Memory 根本没这个方法，
     hasattr 一判 False 就吞成空列表。
  2）「把防抖从 2.5 调到 4.5 秒」。

跑法：python tests\\test_v1923_learn.py
"""
import io
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix="xy_v1923b_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  [OK]   " + name)
    else:
        FAIL.append(name)
        print("  [FAIL] " + name + ("  <" + str(extra) + ">" if extra else ""))


# ---------------------------------------------------------------- 桩
class FakeMem:
    """只实现 learn 真正会用到的那几个方法（messages_of / session_stats）。"""

    def __init__(self, private_rows, group_rows=None, key_prefix="private_"):
        self.private_rows = private_rows
        self.group_rows = group_rows or {}
        self.key_prefix = key_prefix
        self.asked = []

    def messages_of(self, session_key, limit=40):
        self.asked.append(session_key)
        if str(session_key).startswith("group_"):
            return list(self.group_rows.get(session_key, []))[:limit]
        if str(session_key).startswith(self.key_prefix) or str(session_key).isdigit():
            return list(self.private_rows)[:limit]
        return []

    def session_stats(self):
        out = [{"session_key": "private_20001", "n": 20, "ts": 1.0}]
        for k, v in self.group_rows.items():
            out.append({"session_key": k, "n": len(v), "ts": 1.0})
        return out

    def get_recent_messages(self, session_key, turns=8):
        return list(self.private_rows)[: int(turns) * 2]


ROWS = [
    {"role": "user", "content": "你别老是一次回我三条，看着像机器人"},
    {"role": "assistant", "content": "行，那我以后就回一条"},
    {"role": "user", "content": "还有别连着问我三个问题，烦"},
    {"role": "assistant", "content": "记住了"},
]

GOOD_JSON = ('[{"title":"别连发","content":"他连发两句以上时只回一条，别逐条接"},'
             '{"title":"别追问","content":"一轮里最多问一个问题，别连着追"}]')


import learn as L  # noqa: E402


def t_recent_lines():
    print("[1] 取聊天：真的能从 Memory 里读到")
    m = FakeMem(ROWS)
    rows = L.recent_lines(m, "20001", limit=60)
    ok("recent_lines 拿到 4 行", len(rows) == 4, len(rows))
    ok("优先用 private_ 前缀", "private_20001" in m.asked, m.asked)

    m2 = FakeMem(ROWS, key_prefix="weird_")
    ok("裸 uid 会话键也兼容", len(L.recent_lines(m2, "20001")) == 4)

    ok("空 uid 返回空", L.recent_lines(FakeMem(ROWS), "") == [])

    class Broken:
        def messages_of(self, *a, **k):
            raise RuntimeError("库坏了")

        def get_recent_messages(self, *a, **k):
            raise RuntimeError("也坏了")

    ok("库异常不抛", L.recent_lines(Broken(), "20001") == [])


def t_learn_runs():
    print("[2] 学习：有聊天就真的学，不再永远「聊天太少」")
    src = io.open(os.path.join(ROOT, "learn.py"), encoding="utf-8").read()
    ok("不再调用 mem.list_messages", "= mem.list_messages" not in src)

    m = FakeMem(ROWS)
    seen = {}

    def caller(prompt):
        seen["prompt"] = prompt
        return GOOD_JSON

    r = L.learn_from_dialog(m, "20001", name="安瑆.雨", model_caller=caller)
    ok("ok=True", r.get("ok") is True, r.get("msg"))
    ok("学到条目", int(r.get("added") or 0) >= 1, r)
    ok("prompt 里带了原话", ("回我三条" in seen.get("prompt", "")) or ("连着问" in seen.get("prompt", "")))
    ok("prompt 里带对方称呼", "安瑆.雨" in seen.get("prompt", ""))
    ok("这次不是「聊天太少」", "聊天太少" not in str(r.get("msg")), r.get("msg"))

    stat = L.stats()
    ok("学习库真的多了一行", int(stat.get("total") or 0) >= 1, stat.get("total"))


def t_learn_too_short():
    print("[3] 学习：真的没聊天时，仍然老实说「太少」")
    m = FakeMem([{"role": "user", "content": "嗯"}])
    r = L.learn_from_dialog(m, "20001", model_caller=lambda p: GOOD_JSON)
    ok("ok=False", r.get("ok") is False, r)
    ok("提示聊天太少", "太少" in str(r.get("msg")), r.get("msg"))


def t_summary_reads_group():
    print("[4] 每日总结：私聊 + 群聊都能读到")
    rows = [{"role": "user", "content": "今天面试完了，感觉还行，就是有点累"}] * 6
    g = [{"role": "user", "content": "群里聊了点有的没的"}] * 6
    m = FakeMem(rows, group_rows={"group_1": g})

    import diary as _diary
    _orig = _diary.bound_accounts
    _diary.bound_accounts = lambda: ["20001"]      # 临时库没有绑定账号，桩一下
    got = {}

    def caller(prompt):
        got["prompt"] = prompt
        return '[{"type":"habit","title":"累的时候别追问","content":"他说累就少问两句"}]'

    try:
        r = L.summarize_day(m, model_caller=caller)
    finally:
        _diary.bound_accounts = _orig
    ok("ok=True", r.get("ok") is True, r.get("msg"))
    ok("prompt 里有群聊", "[群]" in got.get("prompt", ""), got.get("prompt", "")[:80])
    ok("不是「今天聊得不多」", "不多" not in str(r.get("msg")), r.get("msg"))


def t_debounce_default():
    print("[5] 防抖默认值 4.5 秒")
    import config as C
    ok("config 默认 4.5", float(C.DEFAULTS.get("merge_delay_sec")) == 4.5,
       C.DEFAULTS.get("merge_delay_sec"))
    rule = C._NUM_RULES["merge_delay_sec"]
    ok("范围仍然夹住 4.5", float(rule[1]) <= 4.5 <= float(rule[2]), rule)
    import agent as A
    d = A.merge_delay_sec()
    ok("运行时读到 4.5", abs(float(d) - 4.5) < 0.01, d)
    src = io.open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read()
    ok("代码里默认值已改", '_live_cfg("merge_delay_sec", 4.5)' in src)


def main():
    print("=" * 60)
    print("V1.9.23 自测（二）：学习触发 / 防抖 4.5 秒")
    print("=" * 60)
    t_recent_lines()
    t_learn_runs()
    t_learn_too_short()
    t_summary_reads_group()
    t_debounce_default()
    print("-" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：" + "、".join(FAIL))
        return 1
    print("V1.9.23 自测（二）全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
