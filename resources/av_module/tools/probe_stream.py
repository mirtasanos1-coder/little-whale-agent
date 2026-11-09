"""探针：流式听写一轮完整回归（TTS 连播若干句 → 静音切句 → 看片段是否完整）。

用法：
    python tools/probe_stream.py            # 默认 3 句逗号连写长句（考验长句切分）
    python tools/probe_stream.py --short    # 6 句短句，考验短句是否被截断

前提：av_server 已在 5800 端口运行（`python av_server.py --port 5800`）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5800"

LONG = [
    "你好，我是人工智能助手，现在开始测试虚拟麦克风。",
    "今天下午三点我们在三楼会议室开会，讨论下个版本的发布计划。",
    "帮我把这条消息转发给张三，顺便问问他周末有没有空。",
]
SHORT = [
    "你好，我是人工智能助手。",
    "现在开始测试虚拟麦克风。",
    "今天下午三点我们在三楼会议室开会。",
    "讨论下个版本的发布计划。",
    "帮我把这条消息转发给张三。",
    "顺便问问他周末有没有空。",
]


def call(path, body=None, timeout=180):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"content-type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    sentences = SHORT if "--short" in sys.argv else LONG
    call("/api/av/listen/stop", {})
    st = call("/api/av/listen/start", {"language": "zh"})[1]
    print("start:", st.get("ok"), "| model_loaded:", st.get("model_loaded"))
    if not st.get("ok"):
        print("启动失败:", st)
        return 1

    def play():
        for s in sentences:
            call("/api/av/speak", {"text": s, "engine": "sapi"}, timeout=120)
            time.sleep(0.9)

    threading.Thread(target=play, daemon=True).start()

    seen: list[dict] = []
    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(1.5)
        segs = call("/api/av/listen/status")[1].get("segments") or []
        if len(segs) > len(seen):
            for s in segs[len(seen):]:
                print(f"  [{s.get('seconds')}s] {s.get('text')}")
            seen = segs
        if len(seen) >= len(sentences) and time.time() - t0 > 20:
            break

    print(f"片段: {len(seen)} / 句数: {len(sentences)} | 用时: {round(time.time() - t0, 1)}s")
    call("/api/av/listen/stop", {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
