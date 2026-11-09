# -*- coding: utf-8 -*-
# V1.6.0 打包版验收：版本/方法/公告/消耗预估/性能提醒/Agent 闸门
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

INST = r"D:\临时文件\_inst160"
DATA = r"D:\临时文件\_d160"
EXE = os.path.join(INST, "resources", "backend_server.exe")
BASE = "http://127.0.0.1:5781"
H = {"Content-Type": "application/json", "Origin": "http://tauri.localhost"}

ok = bad = 0


def chk(name, cond, extra=""):
    global ok, bad
    if cond:
        ok += 1
        print("  OK   %s %s" % (name, extra))
    else:
        bad += 1
        print("  FAIL %s %s" % (name, extra))


def post(method, *args, timeout=90):
    body = json.dumps({"args": list(args)}).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/" + method, data=body, headers=H, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"http": e.code}
    except Exception as e:
        return {"err": str(e)}


def get(path, timeout=20):
    req = urllib.request.Request(BASE + path, headers={"Origin": "http://tauri.localhost"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"err": str(e)}


def cfg():
    c = post("get_config")
    return c.get("config") or c


def kill_port(port):
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=25).stdout.decode("utf-8", "ignore")
    except Exception:
        return
    for line in out.splitlines():
        if (":%d" % port) in line and "LISTENING" in line.upper():
            pid = line.split()[-1]
            if pid.isdigit():
                subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, timeout=25)


print("[0] 环境")
if not os.path.exists(EXE):
    raise SystemExit("没装包：%s" % EXE)
shutil.rmtree(DATA, ignore_errors=True)
os.makedirs(DATA, exist_ok=True)
kill_port(5781)
env = dict(os.environ)
env["QQ_AGENT_DATA_DIR"] = DATA
p = subprocess.Popen([EXE, "--port", "5781"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
h = {}
end = time.time() + 90
while time.time() < end:
    h = get("/api/health", 5)
    if h.get("ok"):
        break
    time.sleep(1.0)
chk("后端起来了", bool(h), str(h.get("backend", {}).get("version")))

print("[1] 版本 / 方法数 / 公告")
b = h.get("backend", {})
chk("version=1.6.0", b.get("version") == "1.6.0", str(b.get("version")))
chk("frozen=true", b.get("frozen") is True)
ms = (get("/api/methods") or {}).get("methods") or []
chk("methods >= 332", len(ms) >= 332, "count=%d" % len(ms))
chk("没有 draw_program", "draw_program" not in ms)
chk("有 perf_ack", "perf_ack" in ms)
post("accept_disclaimer", True)
time.sleep(0.4)
nt = post("get_notices")
items = (nt or {}).get("items") or (nt or {}).get("notices") or []
chk("公告只有 v160", [x.get("id") for x in items if isinstance(x, dict)] == ["2026-10-28-v160"])

print("[2] 消耗预估默认关")
te = post("get_token_estimate")
chk("disabled=true", te.get("disabled") is True, str(te.get("msg"))[:36])
chk("没返回 rows", not te.get("rows"))
chk("token_est_enabled=false", cfg().get("token_est_enabled") is False)
post("save_settings", {"token_est_enabled": True})
te2 = post("get_token_estimate")
chk("打开后能算", te2.get("ok") is True and len(te2.get("rows") or []) == 12, "rows=%d" % len(te2.get("rows") or []))
post("save_settings", {"token_est_enabled": False})

print("[3] 性能提醒闸门")
post("save_settings", {"moment_enabled": False})
time.sleep(0.3)
r = post("save_settings", {"moment_enabled": True})
chk("关→开 要确认", r.get("need_ack") == "moment", json.dumps(r, ensure_ascii=False)[:90])
chk("提示文案含【自动说说】", "【自动说说】" in (r.get("msg") or ""), (r.get("msg") or "")[:40])
chk("被拦下时没落盘", cfg().get("moment_enabled") is False, str(cfg().get("moment_enabled")))
chk("perf_ack ok", post("perf_ack", "moment", True).get("ok") is True)
r2 = post("save_settings", {"moment_enabled": True})
chk("确认后保存成功", r2.get("ok") is True and not r2.get("need_ack"), json.dumps(r2, ensure_ascii=False)[:70])
c = cfg()
chk("moment_enabled=true", c.get("moment_enabled") is True)
chk("perf_ack_moment=true", c.get("perf_ack_moment") is True)
post("perf_ack", "moment", False)
r3 = post("save_settings", {"moment_enabled": True})
chk("已经开着 → 不再问", r3.get("ok") is True and not r3.get("need_ack"), json.dumps(r3, ensure_ascii=False)[:70])

print("[4] 关掉功能不需要确认")
chk("关开关不弹窗", post("save_settings", {"agent_web": False}).get("ok") is True)

print("[5] 闸门都在")
g = post("agent_gate_status")
chk("Agent 总闸默认关", g.get("enabled") is False)
chk("风险 5 条", len(g.get("risks") or []) == 5, str(len(g.get("risks") or [])))
r4 = post("agent_run_task", "帮我查天气")
chk("Agent 没开 → 立刻拒绝", r4.get("ok") is False and "总开关" in (r4.get("msg") or ""),
    json.dumps(r4, ensure_ascii=False)[:90])

print("[6] 退出")
post("quit_app")
t0 = time.time()
while time.time() - t0 < 15:
    if p.poll() is not None:
        break
    time.sleep(0.3)
chk("quit_app 后后端退出", p.poll() is not None, "rc=%s %.1fs" % (p.poll(), time.time() - t0))

print("\n通过 %d / 失败 %d" % (ok, bad))
