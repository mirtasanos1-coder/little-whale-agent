"""回归5：流式听写（listen/start|status|stop）与 RPA（rpa/start|status|stop）。

需要服务已在 5800 端口运行；首次会把 whisper 模型加载进内存（CPU 约 1 分钟）。
    python tools/probe_listen.py
"""
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:5800"


def call(path, body=None, timeout=180):
    """body=None -> GET；{} -> POST（想 POST 空体就传 {}）。"""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"content-type": "application/json"},
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except Exception:
            return exc.code, {"_raw": raw.decode("utf-8", "replace")}


def show(label, code, out, keys):
    print(f"  {label:16s} {code}", {k: out.get(k) for k in keys if k in out} or out)


print("== 阻塞式听写 /listen（4 秒）")
code, out = call("/api/av/listen", {"seconds": 4, "mode": "loopback"}, timeout=300)
print("  ", code, {k: out.get(k) for k in ("ok", "text", "audio_seconds", "rtf", "error")})

print("\n== 流式听写 /listen/start")
code, out = call("/api/av/listen/start", {"language": "zh", "mode": "loopback"})
show("start", code, out, ("ok", "running", "mode", "device", "model_loaded", "error"))

# 让系统真的发出点声音（回环才录得到东西）：模型加载期间先垫一句话
import threading  # noqa: E402


def say_later():
    time.sleep(1.0)
    for text in ("现在开始测试流式听写功能。",
                 "今天下午三点我们在三楼会议室开会。",
                 "请问你周末有时间一起吃饭吗？"):
        try:
            call("/api/av/speak", {"text": text, "engine": "sapi"}, timeout=120)
        except Exception as exc:  # noqa: BLE001
            print("   speak 失败:", exc)
        time.sleep(6.0)


threading.Thread(target=say_later, daemon=True).start()

t0 = time.time()
last = {}
for _ in range(22):
    time.sleep(4)
    code, st = call("/api/av/listen/status")
    last = st
    print(f"  +{int(time.time() - t0):3d}s running={st.get('running')} level={st.get('level'):.4f} "
          f"buffered={st.get('buffered_seconds')} segs={st.get('segment_count')} "
          f"model_loaded={st.get('model_loaded')} err={str(st.get('error'))[:50]}")
    if st.get("segment_count"):
        break

code, out = call("/api/av/listen/stop", {})
show("stop", code, out, ("ok", "running"))
segs = last.get("segments") or []
print("   片段:", [s.get("text") for s in segs][:3])

print("\n== RPA dry-run")
code, out = call("/api/av/rpa/start", {"dry_run": True})
print("  start", code, {k: out.get("rpa", {}).get(k) for k in ("running", "state", "dry_run")})
time.sleep(3)
code, st = call("/api/av/rpa/status")
r = st.get("rpa", {})
print("  status", code, "state=", r.get("state"), "polls=", r.get("polls"),
      "clicks=", r.get("clicks"), "err=", r.get("error"))
code, out = call("/api/av/rpa/stop", {})
print("  stop", code, {k: out.get("rpa", {}).get(k) for k in ("running", "state")})
print("done")
