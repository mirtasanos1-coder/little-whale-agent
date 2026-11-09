# -*- coding: utf-8 -*-
"""V1.6.2 打包版验收（接口层）。用法： python t3.py <安装目录> [端口]"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

INST = sys.argv[1] if len(sys.argv) > 1 else r"D:\临时文件\_inst162"
PORT = sys.argv[2] if len(sys.argv) > 2 else "5701"
BASE = "http://127.0.0.1:%s" % PORT
ORIGIN = {"Origin": "http://tauri.localhost"}
DATA = os.path.join(INST, "_data")

ok = fail = 0


def chk(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print("  OK   %s %s" % (name, extra))
    else:
        fail += 1
        print("  FAIL %s %s" % (name, extra))


def post(method, *args, timeout=180, base=None):
    body = json.dumps({"args": list(args)}).encode()
    req = urllib.request.Request((base or BASE) + "/api/" + method, data=body,
                                 headers={"Content-Type": "application/json", **ORIGIN})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get(path, timeout=30, base=None):
    req = urllib.request.Request((base or BASE) + path, headers=ORIGIN)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def kill_port(port):
    p = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    for line in (p.stdout or "").splitlines():
        if "LISTENING" in line and (":%s " % port) in line:
            pid = line.split()[-1]
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
            time.sleep(0.5)


exe = os.path.join(INST, r"resources\backend_server.exe")
chk("后端 exe 存在", os.path.exists(exe), exe)
if not os.path.exists(exe):
    sys.exit(1)

# 先跑一次：确认老配置迁移
os.makedirs(DATA, exist_ok=True)
cfg_p = os.path.join(DATA, "config.json")
old = {"napcat_http": "http://127.0.0.1:3000", "napcat_ws": "ws://127.0.0.1:3001"}
if not os.path.exists(cfg_p):
    json.dump(old, open(cfg_p, "w", encoding="utf-8"), ensure_ascii=False)

kill_port(PORT)
env = dict(os.environ)
env["QQ_AGENT_DATA_DIR"] = DATA
proc = subprocess.Popen([exe, "--port", PORT], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print("   backend pid", proc.pid, "port", PORT)

ready = False
for _ in range(120):
    try:
        h = get("/api/health", timeout=3)
        if h.get("ok") is not False:
            ready = True
            break
    except Exception:
        time.sleep(0.5)
chk("后端起来了", ready)
if not ready:
    sys.exit(1)

print("== 1. 版本与冻结状态")
h = get("/api/health")
b = h.get("backend", {})
print("   version=%s frozen=%s methods=%s" % (b.get("version"), b.get("frozen"), b.get("methods")))
chk("version = 1.6.2", b.get("version") == "1.6.2", str(b.get("version")))
chk("frozen = True", b.get("frozen") is True)
chk("methods >= 330", int(b.get("methods") or 0) >= 330, str(b.get("methods")))
post("accept_disclaimer")

print("== 2. 端口迁移（老配置 :3000/:3001 -> :3003/:3004）")
c = post("get_config")
cfg = c.get("config") or c
nh = str(cfg.get("napcat_http") or "")
nw = str(cfg.get("napcat_ws") or "")
print("   napcat_http=", nh, " napcat_ws=", nw)
chk("napcat_http 指向 3003", nh.rstrip("/").endswith(":3003"), nh)
chk("napcat_ws 指向 3004", nw.rstrip("/").endswith(":3004"), nw)
raw = json.load(open(os.path.join(DATA, "config.json"), encoding="utf-8"))
chk("落盘的 config.json 也迁移了",
    str(raw.get("napcat_http") or "").endswith(":3003") and str(raw.get("napcat_ws") or "").endswith(":3004"),
    "%s | %s" % (raw.get("napcat_http"), raw.get("napcat_ws")))

print("== 3. NapCat 常量与登录态文案")
st = post("napcat_login_state")
print("   state=%s running=%s installed=%s" % (st.get("state"), st.get("running"), st.get("installed")))
msg = st.get("msg") or ""
chk("文案里没有「任务管理器」", "任务管理器" not in msg, msg[:60])
chk("不会出现「我帮你关 QQ」", "帮你关" not in msg)
chk("state 是已知档位",
    st.get("state") in ("down", "not_running", "no_login", "waiting_scan", "qq_client_busy",
                        "risk_limited", "port_taken", "not_installed", "running", "no_ws",
                        "process_no_port", "zombie"), str(st.get("state")))

print("== 4. 开关/主题相关接口")
w = post("win_state")
print("   win_state:", {k: w.get(k) for k in ("frameless", "decorations", "tray_running")})
chk("win_state 可调", w.get("ok") is True)
r = post("set_frameless", False)
if r.get("ok") is False and w.get("decorations") is None:
    # 这里单独跑的是后端 exe（没有 Tauri 壳），改窗口边框需要 Rust 侧的命令，
    # 界面里的开关由 t4.py 在真实壳里验；此处按「跳过」处理而不是判失败。
    print("  SKIP set_frameless（本次没有壳，窗口边框交给 t4.py 验）")
else:
    chk("set_frameless(False) 成功", r.get("ok") is not False, str(r.get("ok")))
    w2 = post("win_state")
    chk("窗口真的变成有边框", w2.get("frameless") is False and w2.get("decorations") is True,
        "%s/%s" % (w2.get("frameless"), w2.get("decorations")))
    r = post("set_frameless", True)
    w3 = post("win_state")
    chk("再切回无边框", w3.get("frameless") is True and w3.get("decorations") is False,
        "%s/%s" % (w3.get("frameless"), w3.get("decorations")))
th = post("apply_theme", "sakura")
chk("樱花主题返回 css", bool(th.get("css")) and th.get("dark") is False, "dark=%s" % th.get("dark"))
post("apply_theme", "dark")

print("== 5. 公告")
n = post("get_notices")
items = n.get("items") or n.get("notices") or []
ids = [x.get("id") for x in items]
print("   notices:", ids)
chk("只有 v162 一条", ids == ["2026-10-28-v162"], str(ids))

print("== 6. 退出")
post("quit_app")
gone = False
for _ in range(40):
    if proc.poll() is not None:
        gone = True
        break
    time.sleep(0.5)
chk("quit_app 后进程退出", gone, "rc=%s" % proc.poll())

print("\n结果: OK=%d FAIL=%d" % (ok, fail))
sys.exit(0 if fail == 0 else 1)
