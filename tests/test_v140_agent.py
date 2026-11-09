# -*- coding: utf-8 -*-
"""V1.4.0 Agent 全链路自测（真的开浏览器，真的走 ReAct 循环）。

跑法：python tests\\test_v140_agent.py
它会：
1. 起一个本地假模型（OpenAI 兼容），第 1 轮回 ACTION: web_search，第 2 轮回 FINAL；
2. 把 Agent 参数写进配置再读回来，确认落盘；
3. 让 agent.run_agent_task 真的去跑 —— 里面 web_search 用 Playwright 真开 Chromium；
4. 检查 agent_actions 有没有记账、紧急停止是不是真的能掐断。
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="xy_agent_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP

import config                      # noqa: E402
import computer_use as cu          # noqa: E402
import agent                       # noqa: E402

PASS, FAIL = [], []
ROUNDS = {"n": 0}
AGENT_STATE = {"acted": False}


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append((name, extra))
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra and not cond else ""))


class MockLM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "ignore")
        ROUNDS["n"] += 1
        # 按内容判别，而不是按「第几次请求」—— providers 会先发一次预热请求，
        # 用计数器会把 ACTION 喂给预热请求，循环就 0 步直接 FINAL 了。
        try:
            msgs = json.loads(body or "{}").get("messages") or []
            prompt = "\n".join((m.get("content") or "") for m in msgs)
        except Exception:
            prompt = ""
        if "步数用完了" in prompt or "只输出一行" in prompt and "FINAL" in prompt and "ACTION" not in prompt:
            text = "FINAL: 我查到北京今天多云，最高 24 度。"
        elif "现在输出下一步" in prompt or "ACTION" in prompt:
            if not AGENT_STATE["acted"]:
                AGENT_STATE["acted"] = True
                text = 'ACTION: web_search {"query": "今天天气 北京", "limit": 3}'
            else:
                text = "FINAL: 我查到北京今天多云，最高 24 度。"
        else:
            text = "（预热）你好"
        data = {"id": "mock", "object": "chat.completion",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main():
    print("数据目录：", TMP)
    srv = HTTPServer(("127.0.0.1", 0), MockLM)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    c = config.load()
    c.update({
        "lm_base": "http://127.0.0.1:%d/v1" % port,
        "lm_model": "mock-lm",
        "lm_api_key": "",
        "lm_fb_base": "", "lm_fb_model": "",
        "lm_primary": "local",
        "provider_keys": {},
        "agent_web": True,
        "agent_keyboard": False,
        "agent_code": True,
        "agent_auto": True,
        "human_approval": False,
        "max_agent_steps": 5,
        "agent_call_enabled": False,
        "agent_call_start_hour": 9,
        "agent_call_end_hour": 22,
        "agent_call_max_per_hour": 1,
    })
    config.save(c)

    print("\n一、参数落盘")
    c2 = config.load()
    for k, v in (("agent_web", True), ("agent_code", True), ("agent_auto", True),
                 ("human_approval", False), ("max_agent_steps", 5),
                 ("agent_call_enabled", False), ("agent_call_max_per_hour", 1)):
        ok("config.%s 读回是 %r" % (k, v), c2.get(k) == v, c2.get(k))
    ok("max_agent_steps 超范围会被夹住",
       config.set_checked("max_agent_steps", 999)[2] == 50,
       config.set_checked("max_agent_steps", 999))
    config.set_checked("max_agent_steps", 5)
    ok("agent_keyboard 默认关（不会自己动鼠标）", c2.get("agent_keyboard") is False, c2.get("agent_keyboard"))

    print("\n二、工具开关：关了就是真不执行")
    c3 = config.load()
    c3["agent_web"] = False
    config.save(c3)
    r = cu.tool_web_search("今天天气")
    ok("网络关着时 web_search 拒绝执行", r.get("ok") is False and "关" in r.get("msg", ""), r.get("msg"))
    c3 = config.load()
    c3["agent_web"] = True
    config.save(c3)

    print("\n三、ReAct 全链路（模型自己决定调浏览器）")
    t0 = time.time()
    res = agent.run_agent_task("用浏览器搜一下今天天气")
    used = time.time() - t0
    print("  用时 %.1fs，走了 %d 步" % (used, len(res.get("steps") or [])))
    ok("任务跑出结论", bool(res.get("answer")), res.get("answer"))
    ok("结论不是兜底话术", ("多云" in (res.get("answer") or "")) or ("北京" in (res.get("answer") or "")),
       res.get("answer"))
    steps = res.get("steps") or []
    ok("至少走了一步", len(steps) >= 1, len(steps))
    first = steps[0] if steps else {}
    ok("第一步调的是 web_search", first.get("tool") == "web_search", first.get("tool"))
    # v1.5.2 收尾：搜索改成「先 HTTP 并发抢，拿不到才上浏览器」——
    # 浏览器冷启动要十几秒，用户等不起。这里只要求「真拿到了结果且不太慢」。
    _items = int(first.get("items") or 0)
    ok("web_search 真拿到结果（浏览器或 HTTP 都行，20 秒内）",
       _items > 0 and used < 20,
       "items=%d via=%s used=%.1fs msg=%s"
       % (_items, first.get("via"), used, str(first.get("msg"))[:80]))
    acts = cu.recent_actions(limit=20)
    ok("动作账本有记录（>=3 条：开始/步骤/结束）", len(acts) >= 3, len(acts))
    ok("账本里有 web_search", any(a.get("action") == "web_search" for a in acts),
       [a.get("action") for a in acts])
    ok("账本里有 task_done", any(a.get("action") == "task_done" for a in acts),
       [a.get("action") for a in acts])

    print("\n四、代码沙箱")
    r = cu.tool_execute_code("print(sum(i*i for i in range(5)))")
    ok("正常代码能跑", r.get("ok") and "30" in (r.get("output") or ""), r)
    r = cu.tool_execute_code("import os\nprint(os.listdir('.'))")
    ok("import os 被拦下", r.get("ok") is False and "不许" in (r.get("msg") or ""), r.get("msg"))
    r = cu.tool_execute_code("open('x.txt','w')")
    ok("open() 被拦下", r.get("ok") is False, r.get("msg"))

    print("\n五、紧急停止")
    cu.reset_stop()
    cu.stop_now("自测")
    r = cu.tool_execute_code("print(1)")
    ok("急停之后代码不让跑", r.get("ok") is False and "停止" in (r.get("msg") or ""), r.get("msg"))
    ok("急停状态可查", cu.status()["stop"]["stopped"] is True, cu.status()["stop"])
    cu.reset_stop()
    r = cu.tool_execute_code("print(2)")
    ok("恢复之后又能跑", r.get("ok") is True, r.get("msg"))

    srv.shutdown()
    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for n, e in FAIL:
            print("  未过：%s -> %s" % (n, e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
