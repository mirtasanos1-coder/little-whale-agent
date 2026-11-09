# -*- coding: utf-8 -*-
# 打包版 UI 冒烟 + 「退出真的退」：5 大类 + 二级页签 + quit_app 关壳
import json
import os
import subprocess
import time
import urllib.request
import websocket

APP = r"D:\临时文件\_inst160\小鲸鱼.exe"
ok = bad = 0


def chk(n, c, e=""):
    global ok, bad
    if c:
        ok += 1
        print("  OK   %s %s" % (n, e))
    else:
        bad += 1
        print("  FAIL %s %s" % (n, e))


subprocess.run(["taskkill", "/IM", "小鲸鱼.exe", "/F"], capture_output=True)
time.sleep(1.2)
env_cmd = ("$env:QQ_AGENT_DATA_DIR='D:\\临时文件\\_d160ui';"
           "$env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS='--remote-debugging-port=9223 --remote-allow-origins=*';"
           "$env:WEBVIEW2_USER_DATA_FOLDER='D:\\临时文件\\_wv160';"
           "Start-Process '%s'" % APP)
subprocess.run(["powershell", "-NoProfile", "-Command", env_cmd], capture_output=True)

lst = None
for _ in range(60):
    try:
        lst = json.loads(urllib.request.urlopen("http://127.0.0.1:9223/json/list", timeout=3).read().decode())
        if lst:
            break
    except Exception:
        pass
    time.sleep(1.0)
if not lst:
    raise SystemExit("CDP 没起来")
ws = websocket.create_connection([x for x in lst if x.get("type") == "page"][0]["webSocketDebuggerUrl"],
                                timeout=120, suppress_origin=True)
_id = [0]


def ev(expr, timeout=60):
    _id[0] += 1
    ws.send(json.dumps({"id": _id[0], "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
    end = time.time() + timeout
    while time.time() < end:
        try:
            m = json.loads(ws.recv())
        except Exception:
            continue
        if m.get("id") == _id[0]:
            r = m.get("result", {})
            if r.get("exceptionDetails"):
                return {"__exc": str(r["exceptionDetails"].get("exception", {}).get("description"))[:200]}
            return r.get("result", {}).get("value")
    return None


for _ in range(60):
    if ev("!!(window.showGroup && window.switchTab)"):
        break
    time.sleep(0.5)

print("[1] 结构")
chk("5 个大类按钮", ev("Array.from(document.querySelectorAll('#tabNav .grp-btn')).map(b=>b.dataset.group)")
    == ["console", "model", "soul", "lab", "settings"])
chk("5 个页面容器", ev("document.querySelectorAll('main > .page').length") == 5,
    str(ev("document.querySelectorAll('main > .page').length")))
chk("soul 子页 6", ev("document.querySelectorAll('.subnav[data-subnav=\"soul\"] .sub-btn').length") == 6)
chk("lab 子页 4", ev("document.querySelectorAll('.subnav[data-subnav=\"lab\"] .sub-btn').length") == 4)
chk("settings 子页 2", ev("document.querySelectorAll('.subnav[data-subnav=\"settings\"] .sub-btn').length") == 2)

print("[2] 大类切换")
for g in ("console", "model", "soul", "lab", "settings"):
    ev("window.showGroup('%s'); 1" % g)
    time.sleep(0.45)
    r = ev("""(() => { const pg=document.getElementById('page-%s');
      return {shown: !!pg && !pg.classList.contains('hidden'),
              docOvf: document.documentElement.scrollWidth > window.innerWidth + 2}; })()""" % g)
    chk("大类 %s" % g, bool(r) and r.get("shown") is True, str(r))

print("[3] 子页逐个切")
for tab in ("persona", "memory", "emotion", "affinity", "learn", "introspect",
            "av", "agent", "alive", "draw", "settings", "admin", "model", "console"):
    ev("window.switchTab('%s'); 1" % tab)
    time.sleep(0.35)
    r = ev("""(() => { const s=document.getElementById('tab-%s');
      return {cur: window.__curTab, vis: !!s && getComputedStyle(s).display !== 'none',
              hOvf: !!s && s.scrollWidth > s.clientWidth + 2}; })()""" % tab)
    chk("子页 %s" % tab, bool(r) and r.get("cur") == tab and r.get("vis") is True and r.get("hOvf") is False, str(r))

print("[4] 退出：点确认后壳必须真退")
ev("(async () => { try { await window.pywebview.api.accept_disclaimer(true); } catch(e) {} return 1; })()")
time.sleep(0.8)
ev("(async () => { try { return await window.pywebview.api.quit_app(); } catch(e) { return String(e); } })()")
gone = False
t0 = time.time()
while time.time() - t0 < 25:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq 小鲸鱼.exe"], capture_output=True).stdout.decode("utf-8", "ignore")
    if "小鲸鱼.exe" not in out:
        gone = True
        break
    time.sleep(1.0)
chk("quit_app 后壳退出", gone, "%.1fs" % (time.time() - t0))

print("\n通过 %d / 失败 %d" % (ok, bad))
