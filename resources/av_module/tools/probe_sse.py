"""回归4：SSE 事件流、字幕接口、事件序号连续性。

需要服务已在 5800 端口运行：
    python tools/probe_sse.py
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:5800"


def call(path, body=None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"content-type": "application/json"},
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


print("== 订阅 SSE")
stream = urllib.request.urlopen(BASE + "/api/av/events/stream", timeout=25)
got = []


def pump(seconds):
    end = time.time() + seconds
    while time.time() < end and len(got) < 25:
        line = stream.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if text.startswith("data:"):
            payload = text[5:].strip()
            if payload:
                try:
                    got.append(json.loads(payload))
                except Exception:
                    got.append({"raw": payload})


pump(1.5)
print("  补发历史事件:", len(got))

print("== 触发字幕 + 切图")
call("/api/av/stream/start", {"image": "placeholder", "effect": "pulse"})
call("/api/av/stream/caption", {"text": "AI 正在思考：这句话是字幕"})
call("/api/av/stream_image", {"image": "assets/sample_avatar.png"})
print("  caption 状态:", call("/api/av/status")["video"].get("caption"))
pump(3.0)

kinds = {}
for e in got:
    kinds[e.get("kind")] = kinds.get(e.get("kind"), 0) + 1
seqs = [e["seq"] for e in got if "seq" in e]
print("  收到事件:", len(got), "类型分布:", kinds)
print("  seq 单调递增:", all(b > a for a, b in zip(seqs, seqs[1:])) if len(seqs) > 1 else "样本不足")

print("== 轮询接口与 SSE 序号是否一致")
poll = call("/api/av/events?since=0&limit=5")
print("  /events 最新:", [e["seq"] for e in poll.get("events", [])][:5],
      "| SSE 最新:", seqs[-1] if seqs else None)

call("/api/av/stream/stop", {})
print("done")
