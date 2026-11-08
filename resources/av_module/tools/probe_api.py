"""回归3：真实图片推流、并发说话、fps、错误码。"""
import json
import threading
import time
import urllib.request

from PIL import Image, ImageDraw

BASE = "http://127.0.0.1:5800"


def call(path, body=None, timeout=120):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"content-type": "application/json"},
                                 method="GET" if body is None else "POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        code = r.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        code = exc.code
    ms = int((time.time() - t0) * 1000)
    try:
        out = json.loads(raw)
    except Exception:
        out = {"_bin": len(raw)}
    return code, out, ms


for _ in range(40):
    try:
        call("/api/av/health", timeout=3)
        break
    except Exception:
        time.sleep(1)

# 造一张 512x512 的“人设图”
img = Image.new("RGB", (512, 512), (40, 30, 70))
d = ImageDraw.Draw(img)
d.ellipse([96, 120, 416, 440], fill=(240, 200, 170))
d.ellipse([176, 220, 216, 260], fill=(30, 30, 40))
d.ellipse([296, 220, 336, 260], fill=(30, 30, 40))
d.arc([206, 280, 306, 360], 20, 160, fill=(120, 60, 60), width=8)
d.text((150, 460), "SAMPLE AVATAR", fill=(255, 255, 255))
img.save("assets/sample_avatar.png")
print("sample saved")

print("== start with sample avatar")
code, out, ms = call("/api/av/stream/start", {"image": "assets/sample_avatar.png", "effect": "kenburns"})
v = out.get("video", {})
print(f"  {code} {ms}ms dry_run={v.get('dry_run')} frames={v.get('frames')} image={v.get('image')}")

time.sleep(2.0)
code, out, ms = call("/api/av/status")
v = out["video"]
print(f"== fps_actual={v['fps_actual']} frames={v['frames']} target={v['fps_target']}")

print("== stream_image 切到 placeholder")
code, out, ms = call("/api/av/stream_image", {"image": "placeholder"})
print(f"  {code} image={out.get('video', {}).get('image')}")

print("== 坏 base64 -> 期望 400")
bad = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
code, out, ms = call("/api/av/stream_image", {"image": bad})
print(f"  {code} {str(out)[:160]}")

print("== 并发：说话期间 health/status 是否被阻塞")
res = {}


def long_speak():
    code, out, ms = call("/api/av/speak", {"text": "这是一段比较长的播报内容，用来测试服务在播放期间是否还能响应其他请求，"
                                                  "顺便验证打断与状态查询功能是否正常。", "engine": "edge"}, timeout=180)
    res["speak"] = (code, ms, bool(out.get("ok")))


t = threading.Thread(target=long_speak)
t.start()
time.sleep(1.2)
lat = []
for _ in range(4):
    c, o, m = call("/api/av/health", timeout=10)
    lat.append(m)
    time.sleep(0.3)
print("  health 延迟(ms):", lat)
c, o, m = call("/api/av/status", timeout=15)
print("  status 延迟:", m, "speaking?", o.get("audio", {}).get("speaking", "?"))
print("== 打断")
c, o, m = call("/api/av/speak/stop")
print("  stop ->", c, str(o)[:120])
t.join(timeout=60)
print("  speak 结果:", res.get("speak"))

print("== 立即再次说话（打断语义）")
code, out, ms = call("/api/av/speak", {"text": "第二句。", "engine": "sapi"}, timeout=60)
print(f"  {code} {ms}ms audio={out.get('audio_seconds')}s played={out.get('played', {}).get('played')}")

call("/api/av/stream/stop", {})
print("done")
