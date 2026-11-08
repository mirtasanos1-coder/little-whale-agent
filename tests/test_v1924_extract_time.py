# -*- coding: utf-8 -*-
"""V1.9.24 自测：记忆提取修好 + 时间点换算。

用户报的两件事（m02998）：
  「还是有点小问题，记忆还是无法提取，马上修；还有优化一下上下文，
    我说4电下课一直好像不知道几点，让它能随时知道时间」

取证结论（真机库+日志）：
  1. `memories` 表 0 行、`perm_memory` 表 29 行 —— 同一个模型、同一段对话，
     永久记忆那条链能提出来、会话记忆这条链一条都提不出来。
     三个原因叠加：
       a. `EXTRACT_PROMPT` 只有 208 字符，明写「闲聊、玩笑、情绪宣泄都不算」，
          而用户和好友聊的全是情绪/关系/调侃 → 模型规规矩矩答「无」
          （usage_log 里 completion_tokens=1）。
       b. `extract_and_store` 只会 `for line in text.splitlines()`，模型一旦
          给 JSON / 编号列表就一条都解析不出（perm 链用的是宽容的
          `_extract_json_array`）。
       c. 三条链共用 `_EXTRACT_LAST[scope]` 和 `_BG_SLOT`，同时起线程互相饿死。
  2. 云端 503 + 本地超时，两个端点都不通，但 `_ask_text` 每轮从头重试整条链
     → 每轮白等 90 秒（记忆）+ 120 秒（事件）。
  3. 时间**本来就注入**了（`time_context()` 在最后一条 user 消息里），
     缺的是「我四电下课」这种钟点的换算 —— 模型只看到「现在是 18:51」，
     得自己推「4 点」是今天已经过去的 16:00 还是明天。

这组测试锁六件事：
  [1] 端点冷却：挂过的端点进冷却，`_ask_text` 跳过它；全冷却直接返回，不再空等
  [2] 节流命名空间：memory / perm / event 各记各的 45 秒窗口，不再互相饿死
  [3] 槽位分离：会话记忆提取有独立槽位，不被永久记忆提炼堵死
  [4] `_parse_memory_block` 宽容解析：JSON 数组 / 编号列表 / 分类标签 / 「无」
  [5] `time_point_context`：钟点换算（「我四电下课」「今晚8点」「明天9点」）
  [6] `EXTRACT_PROMPT` 放宽：情绪/关系/承诺必须记一条；端到端能入库
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="xy_v1924_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP

import agent  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append((name, extra))
        print(f"  [FAIL] {name}" + (f"  ← {extra}" if extra else ""))


CLOUD = {"kind": "cloud", "provider": "custom", "base": "https://cloud.example/v1",
         "model": "cloud-model", "api_key": "sk-x", "is_cloud": True,
         "label": "云端 / cloud-model"}
LOCAL = {"kind": "local", "provider": "local", "base": "http://127.0.0.1:1234/v1",
         "model": "local-model", "api_key": "", "is_cloud": False,
         "label": "本地 / local-model"}

CALLS = []
REPLY = {}


def fake_post_chat(payload, timeout=None, tag="", target=None, detail="", feature=""):
    CALLS.append({"tag": tag, "target": target, "model": payload.get("model")})
    t = target or {}
    fail = REPLY.get("fail", {})
    if t.get("base") in fail:
        return None, fail[t["base"]], {}
    return REPLY.get("text", "- 记住: （安瑆.雨）明天早上九点要去面试"), "ok", {}


def _clear_cooldown():
    agent._EP_COOLDOWN.clear()


def main():
    real = {
        "chain_targets": agent._chain_targets,
        "post_chat": agent._post_chat,
        "add_fact": agent.mem.add_fact,
        "ENABLE": agent._ENABLE_EP_COOLDOWN,
    }
    facts = []
    try:
        agent._chain_targets = lambda: [dict(CLOUD), dict(LOCAL)]
        agent._post_chat = fake_post_chat
        agent.mem.add_fact = lambda scope, who, fact: (facts.append((who, fact)), True)[1]

        # ================= [1] 端点冷却 =================
        print("[1] 端点冷却：挂过的端点不再每轮重试")
        _clear_cooldown()
        CALLS.clear()
        agent._ep_cool(CLOUD, "timeout")
        cooling, left, why = agent._ep_cooling(CLOUD)
        ok("冷却中", cooling, str((cooling, left, why)))
        ok("剩余时间 > 0", left > 0, str(left))
        ok("原因记下了", why == "timeout", why)
        ok("另一个端点不受影响", not agent._ep_cooling(LOCAL)[0], "")

        CALLS.clear()
        text, reason = agent._ask_text([{"role": "user", "content": "x"}], 64,
                                      tag="/记忆提取", targets=[dict(CLOUD), dict(LOCAL)],
                                      raw=True)
        ok("跳过了冷却端点，只打了 1 次", len(CALLS) == 1, str(len(CALLS)))
        ok("打的是没冷却的那个", CALLS and CALLS[0]["target"]["base"] == LOCAL["base"], "")
        ok("拿到了正文", bool(text), repr(text))

        CALLS.clear()
        _clear_cooldown()
        agent._ep_cool(CLOUD, "timeout")
        agent._ep_cool(LOCAL, "connection")
        text, reason = agent._ask_text([{"role": "user", "content": "x"}], 64,
                                       tag="/记忆提取", targets=[dict(CLOUD), dict(LOCAL)])
        ok("全冷却 → 一次请求都不发", len(CALLS) == 0, str(len(CALLS)))
        ok("reason=cooldown", reason == "cooldown", reason)
        snap = agent.ep_cooldown_state()
        ok("冷却快照有两条", len(snap) == 2, str(snap))

        _clear_cooldown()
        agent._ep_cool(CLOUD, "timeout")
        ok("成功即解除冷却", (agent._ep_clear(CLOUD), agent._ep_cooling(CLOUD)[0] == False)[1], "")
        ok("不冷却的原因不算冷却", agent._ep_cool_sec("empty") == 0.0,
           str(agent._ep_cool_sec("empty")))
        ok("503 冷却 180 秒", agent._ep_cool_sec("http_503") == 180.0,
           str(agent._ep_cool_sec("http_503")))

        # 失败自动进冷却（_ask_text 内部）
        _clear_cooldown()
        CALLS.clear()
        REPLY["fail"] = {CLOUD["base"]: "http_503"}
        text, reason = agent._ask_text([{"role": "user", "content": "x"}], 64,
                                       tag="/记忆提取", targets=[dict(CLOUD), dict(LOCAL)])
        ok("失败后端点被自动冷却", agent._ep_cooling(CLOUD)[0], "")
        CALLS.clear()
        agent._ask_text([{"role": "user", "content": "x"}], 64,
                        tag="/记忆提取", targets=[dict(CLOUD), dict(LOCAL)])
        ok("下一轮直接跳过（不再付 503 的学费）",
           CALLS and CALLS[0]["target"]["base"] == LOCAL["base"],
           str(CALLS[0]["target"]["base"] if CALLS else None))
        REPLY["fail"] = {}
        _clear_cooldown()

        # ================= [2] 节流命名空间 =================
        print("[2] 节流命名空间：三条链各记各的 45 秒窗口")
        agent._EXTRACT_LAST.clear()
        agent._FG_TS["t"] = 0.0
        ok("memory 先拿到", agent.bg_slot_ok("s1", ns="memory", slot="memory"), "")
        agent.bg_slot_free("memory")
        ok("同 key 同 ns 被 45 秒窗口挡住",
           not agent.bg_slot_ok("s1", ns="memory", slot="memory"), "")
        ok("同 key 换 ns（perm）能拿到",
           agent.bg_slot_ok("s1", ns="perm"), "")
        agent.bg_slot_free("bg")

        print("[2b] 老行为不变：不传 ns 仍然是裸 key")
        agent._EXTRACT_LAST.clear()
        ok("裸 key 先拿到", agent.bg_slot_ok("s2"), "")
        agent.bg_slot_free("bg")
        ok("裸 key 第二次被挡", not agent.bg_slot_ok("s2"), "")
        ok("加了 ns 之后不受裸 key 影响",
           agent.bg_slot_ok("s2", ns="memory", slot="memory"), "")
        agent.bg_slot_free("memory")

        # ================= [3] 槽位分离 =================
        print("[3] 槽位分离：会话记忆不再被永久记忆提炼堵死")
        agent._EXTRACT_LAST.clear()
        agent._FG_TS["t"] = 0.0
        ok("perm 占住 bg 槽", agent.bg_slot_ok("s3", ns="perm"), "")
        ok("memory 槽位独立，照样能拿",
           agent.bg_slot_ok("s3", ns="memory", slot="memory"), "")
        agent.bg_slot_free("memory")
        agent.bg_slot_free("bg")
        ok("'memory' 在 _BG_SLOTS 里", "memory" in agent._BG_SLOTS, str(list(agent._BG_SLOTS)))

        # ================= [4] 宽容解析 =================
        print("[4] _parse_memory_block 宽容解析")
        D = "安瑆.雨"

        r = agent._parse_memory_block(
            '[{"person": "安瑆.雨", "content": "明天早上九点要去面试"}]', D)
        ok("JSON 数组（dict）能解析", len(r) == 1 and r[0][0] == D and "面试" in r[0][1], str(r))

        r = agent._parse_memory_block('```json\n["他喜欢喝冰美式"]\n```', D)
        ok("带代码块的 JSON 字符串数组能解析",
           len(r) == 1 and r[0][0] == D and "冰美式" in r[0][1], str(r))

        r = agent._parse_memory_block("1. 记住: 他喜欢喝冰美式\n2. （安瑆.雨）在准备面试", D)
        ok("编号列表能解析（旧逻辑一条都提不出）", len(r) == 2, str(r))
        ok("序号没被当成事实的一部分", r and not r[0][1].startswith("1"), str(r))

        r = agent._parse_memory_block("[work] 他下午三点在上课", D)
        ok("分类方括号能剥掉", len(r) == 1 and r[0][1].startswith("他下午三点"), str(r))

        r = agent._parse_memory_block("① 他养了一只橘猫", D)
        ok("圈号能剥掉", len(r) == 1 and r[0][1].startswith("他养了"), str(r))

        r = agent._parse_memory_block("- 记住: （阿哲）养了一只橘猫", D)
        ok("老格式仍然认得，人名取括号里的", r and r[0][0] == "阿哲", str(r))

        r = agent._parse_memory_block("无", D)
        ok("「无」解析成空", r == [], str(r))

        r = agent._parse_memory_block("", D)
        ok("空串解析成空", r == [], str(r))

        r = agent._parse_memory_block("他每天12:30上课", D)
        ok("正文里的 12:30 不被当成序号切开",
           r and r[0][1] == "他每天12:30上课", str(r))

        r = agent._parse_memory_block("他今天很难过，说这段时间一直失眠", D)
        ok("整段兜底：没分行也收", len(r) == 1 and "失眠" in r[0][1], str(r))

        print("[4b] clean_reply 的「记住:」剥离不能吃掉提取结果（最隐蔽的第四条原因）")
        _txt = "- 记住: （安瑆.雨）明天早上九点要去面试"
        ok("clean_reply 确实会把这行剥成空串（复现旧 bug）",
           agent.clean_reply(_txt).strip() == "", repr(agent.clean_reply(_txt)))
        CALLS.clear()
        REPLY["text"] = _txt
        text, _r = agent._extract_text("随便", 128, tag="/记忆提取", feature="memory")
        ok("_extract_text 拿到的是原始行（raw=True）", "记住" in text, repr(text))
        ok("原始行还能解析出事实", agent._parse_memory_block(text, D) != [],
           str(agent._parse_memory_block(text, D)))

        # ================= [5] 时间点换算 =================
        print("[5] time_point_context：钟点换算")
        hour_now = agent.time.localtime().tm_hour

        tp = agent.time_point_context("我四电下课")
        ok("「我四电下课」认出来（「电」也算钟点）", bool(tp), repr(tp))
        ok("两个可能的钟点都给了（04:00 / 16:00）",
           "04:00" in tp and "16:00" in tp, repr(tp))
        ok("明确说了「没讲上午还是下午」", "没讲上午还是下午" in tp, repr(tp))
        ok("给了最可能的那个并要求照它答", "最可能" in tp and "别自己另推" in tp, repr(tp))
        ok("带结论前缀", "对方话里的时间点" in tp, repr(tp)[:60])

        tp = agent.time_point_context("我下午四电下课")
        ok("讲清了「下午」→ 单一时点 16:00，不再有歧义",
           "16:00" in tp and "04:00" not in tp, repr(tp))

        tp = agent.time_point_context("今晚8点面试")
        ok("「今晚8点」→ 20:00", "20:00" in tp, repr(tp))
        ok("讲清了就不给第二种解释", "没讲上午还是下午" not in tp, repr(tp))

        tp = agent.time_point_context("明天9点要去面试")
        ok("「明天9点」→ 明天 09:00", "明天" in tp and "09:00" in tp, repr(tp))

        tp = agent.time_point_context("下午三点半开会")
        ok("「下午三点半」→ 15:30", "15:30" in tp, repr(tp))

        tp = agent.time_point_context("今天吃了什么好呢")
        ok("没提钟点 → 空串（不污染 prompt）", tp == "", repr(tp))

        tp = agent.time_point_context("")
        ok("空文本 → 空串", tp == "", repr(tp))

        old = agent.ENABLE_TIME_AWARENESS
        agent.ENABLE_TIME_AWARENESS = False
        tp = agent.time_point_context("我四电下课")
        agent.ENABLE_TIME_AWARENESS = old
        ok("时间感知关掉后不注入", tp == "", repr(tp))

        ok("_norm_hour 认「四」「十一」「23」",
           agent._norm_hour("四") == 4 and agent._norm_hour("十一") == 11
           and agent._norm_hour("23") == 23 and agent._norm_hour("不是") is None,
           str([agent._norm_hour(x) for x in ("四", "十一", "23", "不是")]))

        # ================= [6] prompt 放宽 + 端到端 =================
        print("[6] EXTRACT_PROMPT 放宽 + 端到端入库")
        ok("prompt 里有强制条款", "必须" in agent.EXTRACT_PROMPT, "")
        ok("prompt 覆盖情绪状态", "情绪状态" in agent.EXTRACT_PROMPT, "")
        ok("prompt 覆盖承诺约定", "承诺约定" in agent.EXTRACT_PROMPT, "")
        ok("prompt 不再说「情绪宣泄都不算」",
           "情绪宣泄都不算" not in agent.EXTRACT_PROMPT, "")
        ok("prompt 比旧版大得多（旧版 208 字符）",
           len(agent.EXTRACT_PROMPT) > 500, str(len(agent.EXTRACT_PROMPT)))

        _clear_cooldown()
        agent._EXTRACT_LAST.clear()
        agent._FG_TS["t"] = 0.0
        CALLS.clear()
        facts.clear()
        REPLY["text"] = '[{"content": "他明天早上九点要去面试，有点紧张"}]'
        agent.extract_and_store("t:30001", "安瑆.雨", "我明天早上九点要去面试，有点紧张")
        ok("发起了模型请求", len(CALLS) >= 1, str(len(CALLS)))
        ok("请求打到云端第一个端点", CALLS and CALLS[0]["target"]["base"] == CLOUD["base"],
           str(CALLS[0]["target"]["base"] if CALLS else None))
        ok("JSON 结果成功入库", len(facts) == 1, str(facts))
        ok("入库的是原话要点", facts and "面试" in facts[0][1], str(facts))

        REPLY["text"] = "无"
        agent._EXTRACT_LAST.clear()
        agent._FG_TS["t"] = 0.0
        facts.clear()
        agent.extract_and_store("t:30002", "安瑆.雨", "哈哈哈哈")
        ok("模型说「无」时不入库", facts == [], str(facts))
    finally:
        agent._chain_targets = real["chain_targets"]
        agent._post_chat = real["post_chat"]
        agent.mem.add_fact = real["add_fact"]
        agent._ENABLE_EP_COOLDOWN = real["ENABLE"]
        _clear_cooldown()

    print("-" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n, e in FAIL:
            print(f"  ✗ {n}  {e}")
        return 1
    print("V1.9.24 自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
