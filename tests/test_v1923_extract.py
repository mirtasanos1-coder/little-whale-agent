# -*- coding: utf-8 -*-
"""V1.9.23 自测：记忆/事件提取要走「和聊天同一条优先级链」。

用户报的现象：聊天已经切到云端（deepseek-v4.1-flash @ https://new.21r.cn/v1），
但后台一直刷
    [ERROR] [模型/记忆提取] 请求超时（90s）@ http://192.168.10.111:1235/v1/chat/completions
也就是说提取还在打本地 LM Studio。

根因：`extract_and_store` / `extract_events` 用的是 `_post_chat(payload)`，
不传 target 就直接打全局 `LM_BASE`（这个还停在本地）；而聊天走的是
`_ask_text(...)` → `_chain_targets()`（云端在前）。

这组测试锁三件事：
  1. `_extract_text()` 打的是链上的第一个端点（云端），不是全局 base；
  2. 第一个端点失败时会顺延到链上的下一个（本地兜底还在）；
  3. 分工（feature_models）里单独配了 memory/event，就用配置的那个。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp(prefix="xy_v1923_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP

import agent  # noqa: E402
import providers as prov_mod  # noqa: E402

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
    if t.get("base") == CLOUD["base"] and REPLY.get("cloud_fail"):
        return None, "timeout", {}
    if t.get("base") == LOCAL["base"]:
        return "", "connection", {}
    return REPLY.get("text", "小鲸鱼: 用户在准备面试"), "ok", {}


def main():
    real = {
        "chain_targets": agent._chain_targets,
        "post_chat": agent._post_chat,
        "feature_target": prov_mod.feature_target,
        "feature_cn": prov_mod.feature_cn,
        "add_fact": agent.mem.add_fact,
        "LM_BASE": agent.LM_BASE,
    }
    facts = []
    try:
        # ---- 准备：链 = [云端, 本地]，全局 base 故意留成本地（复现用户现场）
        agent._chain_targets = lambda: [dict(CLOUD), dict(LOCAL)]
        agent._post_chat = fake_post_chat
        agent.mem.add_fact = lambda scope, who, fact: (facts.append((who, fact)), True)[1]
        prov_mod.feature_target = lambda f, cfg=None: None
        prov_mod.feature_cn = lambda f: f

        print("[1] _feature_targets：没配分工就跟聊天走同一条链")
        CALLS.clear()
        tg = agent._feature_targets("memory")
        ok("返回两个端点", len(tg) == 2, str(tg))
        ok("第一个是云端", tg and tg[0].get("base") == CLOUD["base"], str(tg[0] if tg else None))

        print("[2] _extract_text：实际请求打到云端，不是全局 base")
        CALLS.clear()
        text, reason = agent._extract_text("随便什么问题", 128, tag="/记忆提取",
                                           feature="memory", timeout=90)
        ok("拿到正文", bool(text), repr(text))
        ok("reason=ok", reason == "ok", reason)
        ok("只打了一次", len(CALLS) == 1, str(len(CALLS)))
        ok("打的是云端端点", CALLS and CALLS[0]["target"]["base"] == CLOUD["base"],
           str(CALLS[0]["target"]["base"] if CALLS else None))
        ok("用的云端模型名", CALLS and CALLS[0]["model"] == "cloud-model",
           str(CALLS[0]["model"] if CALLS else None))

        print("[3] 云端挂了 → 顺延到链上的本地（兜底还在）")
        CALLS.clear()
        REPLY["cloud_fail"] = True
        text, reason = agent._extract_text("再来一次", 128, tag="/记忆提取", feature="memory")
        ok("两次尝试", len(CALLS) == 2, str(len(CALLS)))
        ok("第一次云端", CALLS and CALLS[0]["target"]["base"] == CLOUD["base"], "")
        ok("第二次本地", len(CALLS) > 1 and CALLS[1]["target"]["base"] == LOCAL["base"], "")
        ok("全挂了返回空", text == "" and reason in ("connection", "timeout"), f"{text!r} {reason}")
        REPLY["cloud_fail"] = False

        print("[4] 分工里单独配了 memory → 就用配置的那个")
        CALLS.clear()
        prov_mod.feature_target = lambda f, cfg=None: (
            {"kind": "cloud", "provider": "siliconflow", "base": "https://sf.example/v1",
             "model": "Qwen/Qwen2.5-7B-Instruct", "is_cloud": True,
             "label": "硅基流动 / Qwen2.5-7B"} if f == "memory" else None)
        tg = agent._feature_targets("memory")
        ok("只返回配置的那一个", len(tg) == 1, str(tg))
        ok("是 siliconflow", tg and tg[0].get("base") == "https://sf.example/v1", str(tg))
        text, reason = agent._extract_text("分工测试", 128, tag="/记忆提取", feature="memory")
        ok("请求打到分工端点", CALLS and CALLS[0]["target"]["base"] == "https://sf.example/v1",
           str(CALLS[0]["target"]["base"] if CALLS else None))
        ok("用的分工模型名", CALLS and CALLS[0]["model"] == "Qwen/Qwen2.5-7B-Instruct",
           str(CALLS[0]["model"] if CALLS else None))
        prov_mod.feature_target = lambda f, cfg=None: None

        print("[5] extract_and_store 端到端：请求走云端，事实进库")
        # v1.9.24：上一组故意让两个端点都失败，新版的「端点冷却」会把它们记下来，
        # 这里清一下，否则本组会被冷却跳过（那是新功能的正确行为，不是回归）。
        getattr(agent, "_EP_COOLDOWN", {}).clear()
        agent._EXTRACT_LAST.clear()
        agent._FG_TS["t"] = 0.0
        CALLS.clear()
        facts.clear()
        agent.extract_and_store("t:20001", "安瑆.雨", "我明天早上九点要去面试，有点紧张")
        ok("确实发起了模型请求", len(CALLS) >= 1, str(len(CALLS)))
        ok("请求打到云端", CALLS and CALLS[0]["target"]["base"] == CLOUD["base"],
           str(CALLS[0]["target"]["base"] if CALLS else None))
        ok("tag 是记忆提取", CALLS and "/记忆提取" in str(CALLS[0]["tag"]), str(CALLS[0] if CALLS else None))
        ok("事实已入库", len(facts) >= 1, str(facts))

        print("[6] extract_events 端到端：请求同样走云端")
        getattr(agent, "_EP_COOLDOWN", {}).clear()
        agent._EXTRACT_LAST.clear()
        agent._FG_TS["t"] = 0.0
        CALLS.clear()
        REPLY["text"] = '[]'
        agent.EVENT_EXTRACT_ENABLED = True
        agent.extract_events("t:20002", "安瑆.雨", "20002", "明天早上九点我要去面试")
        ok("发起了事件提取请求", len(CALLS) >= 1, str(len(CALLS)))
        ok("事件提取也打云端", CALLS and CALLS[0]["target"]["base"] == CLOUD["base"],
           str(CALLS[0]["target"]["base"] if CALLS else None))
        ok("tag 是事件提取", CALLS and "/事件提取" in str(CALLS[0]["tag"]),
           str(CALLS[0] if CALLS else None))
    finally:
        agent._chain_targets = real["chain_targets"]
        agent._post_chat = real["post_chat"]
        prov_mod.feature_target = real["feature_target"]
        prov_mod.feature_cn = real["feature_cn"]
        agent.mem.add_fact = real["add_fact"]
        agent.LM_BASE = real["LM_BASE"]

    print("-" * 60)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for n, e in FAIL:
            print(f"  ✗ {n}  {e}")
        return 1
    print("V1.9.23 自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
