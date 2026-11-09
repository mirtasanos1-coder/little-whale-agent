# -*- coding: utf-8 -*-
"""V1.5.3 打包版验收：三个 P0 + P2 提速。

用法：python _t_pkg153.py "D:\\临时文件\\_inst153"
数据目录用临时目录（APP_HOME 同级不明显），这里显式指定 QQ_AGENT_DATA_DIR，
避免污染用户真实数据。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

INST = sys.argv[1] if len(sys.argv) > 1 else r"D:\临时文件\_inst153"
EXE = os.path.join(INST, "resources", "backend_server.exe")
DATA = r"D:\临时文件\_d153pkg"
PORT = 5773
BASE = "http://127.0.0.1:%d" % PORT
PORT_FILE = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Temp"),
                         "xiaojingyu-console", "backend.port")

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))


def kill_port(port):
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=20).stdout
    except Exception:
        return
    for ln in out.splitlines():
        if (":%d" % port) in ln and "LISTENING" in ln:
            pid = ln.split()[-1]
            if pid.isdigit() and pid != "0":
                subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                               capture_output=True, timeout=20)


def post(method, *args, timeout=200):
    body = json.dumps({"args": list(args)}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + "/api/" + method, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Origin": "http://tauri.localhost"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"err": str(e)}


def get(path, timeout=30):
    req = urllib.request.Request(BASE + path, headers={"Origin": "http://tauri.localhost"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return 0, {"err": str(e)}


def wait_ready(proc, limit=60):
    t0 = time.time()
    while time.time() - t0 < limit:
        if proc.poll() is not None:
            return False
        st, d = get("/api/health", timeout=3)
        if st == 200 and d.get("ok"):
            return True
        time.sleep(0.3)
    return False


def main():
    print("== 环境 ==")
    ck("打包版 exe 存在", os.path.isfile(EXE), EXE)
    if os.path.isdir(DATA):
        shutil.rmtree(DATA, ignore_errors=True)
    os.makedirs(DATA, exist_ok=True)
    kill_port(PORT)

    # 造一个「很旧 + 超长路径」的假 _MEI，验证清扫不炸
    tdir = os.environ.get("LOCALAPPDATA", r"C:\Temp") + r"\Temp"
    fake = os.path.join(tdir, "_MEI_fake153")
    deep = os.path.join(fake, "中文目录" * 20, "再深一层" * 10)
    try:
        os.makedirs(deep, exist_ok=True)
        open(os.path.join(deep, "x.txt"), "w").write("x")
        old = time.time() - 3600
        os.utime(fake, (old, old))
        made_fake = True
    except Exception as e:
        print("  (造长路径失败，跳过) " + str(e))
        made_fake = False

    env = dict(os.environ)
    env["QQ_AGENT_DATA_DIR"] = DATA
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen([EXE, "--port", str(PORT)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ok = wait_ready(proc)
    ck("后端起来了", ok)
    if not ok:
        try:
            print(proc.stdout.read(2000).decode("utf-8", "replace"))
        except Exception:
            pass
        return

    # ---------- P0-3: 端口文件 ----------
    t0 = time.time()
    while time.time() - t0 < 5 and not os.path.isfile(PORT_FILE):
        time.sleep(0.2)
    pf = {}
    try:
        pf = json.load(open(PORT_FILE, "r", encoding="utf-8"))
    except Exception:
        pass
    ck("端口文件写出来了", bool(pf.get("url")), PORT_FILE)
    ck("端口文件 URL 与本实例一致", str(pf.get("url", "")).endswith(":%d" % PORT), pf.get("url"))
    ck("端口文件带 pid", int(pf.get("pid") or 0) > 0, pf.get("pid"))

    # ---------- P0-1: 未同意协议也能退出/最小化 ----------
    st, d = get("/api/health")
    ver = ((d.get("backend") or {}).get("version"))
    ck("版本 1.5.3", ver == "1.5.3", ver)
    ck("frozen", bool((d.get("backend") or {}).get("frozen")))
    ck("methods 数量", int((d.get("backend") or {}).get("methods") or 0) >= 333,
       (d.get("backend") or {}).get("methods"))
    st, d = post("save_settings", {"guard_enabled": False}, timeout=20)
    ck("未同意协议时业务方法仍 403", st == 403 and d.get("code") == "disclaimer_required",
       (st, d.get("code")))
    st, d = post("win_state", timeout=20)
    ck("未同意协议 win_state 放行", st == 200, st)
    st, d = post("win_min", timeout=20)
    ck("未同意协议 win_min 放行", st == 200, st)

    # ---------- P0-3: _MEI 清扫 ----------
    time.sleep(1.0)
    ck("启动清掉了旧的假 _MEI（含长路径）", (not made_fake) or (not os.path.isdir(fake)),
       fake)
    stack = [n for n in os.listdir(tdir) if ".xjydel.xjydel" in n]
    ck("没有叠后缀垃圾", not stack, stack[:3])

    # ---------- 同意协议后测真链路 ----------
    st, d = post("accept_disclaimer", timeout=20)
    ck("accept_disclaimer", st == 200, st)
    # 注意：Agent 工具受三个开关控制，默认关 —— 必须先开再测，
    # 否则工具直接回「开关是关的」，表现为 0.3 秒空结果（我第一次就吃了这个亏）。
    st, d = post("save_settings", {"agent_web": True, "agent_code": True,
                                   "agent_enabled": True}, timeout=30)
    ck("开 Agent 开关", st == 200, st)

    # P2 搜索
    t0 = time.time()
    st, d = post("agent_test_tool", "web_search", {"query": "今天天气"}, timeout=120)
    dt = time.time() - t0
    pl = d.get("result") if isinstance(d.get("result"), dict) else d
    items = (pl or {}).get("items") or []
    ck("搜索 15 秒内出结果", st == 200 and dt < 15 and len(items) > 0,
       "%.1fs items=%d via=%s" % (dt, len(items), (pl or {}).get("via")))
    if items:
        ttl = str(items[0].get("title") or "")
        ck("标题里没有 HTML 实体残留", ("&" not in ttl and "&#" not in ttl), ttl[:60])

    # P2 open_url
    t0 = time.time()
    st, d = post("agent_test_tool", "open_url", {"url": "https://example.com"}, timeout=120)
    dt = time.time() - t0
    pl = d.get("result") if isinstance(d.get("result"), dict) else d
    via = (pl or {}).get("via")
    ck("open_url 8 秒内读回", st == 200 and dt < 8 and via in ("http", "playwright"),
       "%.1fs via=%s" % (dt, via))

    # 危险动作逐次确认（回归）。客户端超时必须比后端的 120 秒等待更长，
    # 否则会在「还在等确认」时自己先断开（我第一次用 60 秒就误判成失败）。
    rid_box = {}

    def runner():
        rid_box["r"] = post("agent_test_tool", "execute_code",
                            {"code": "print(153)"}, timeout=240)

    import threading
    th = threading.Thread(target=runner, daemon=True)
    th.start()
    rid = ""
    t0 = time.time()
    while time.time() - t0 < 20:
        st, d = post("agent_pending_approval", timeout=20)
        items = ((d.get("items")) or d.get("result", {}).get("items") or [])
        if items:
            rid = items[0].get("rid") or ""
            break
        time.sleep(0.4)
    ck("危险动作挂起等确认", bool(rid), rid)
    if rid:
        st, d = post("agent_answer_approval", rid, True, timeout=60)
        th.join(90)
        _r = (rid_box.get("r") or (0, {}))[1] or {}
        # agent_test_tool 既可能把执行结果放在顶层，也可能包在 result 里
        _rr = _r.get("result") if isinstance(_r.get("result"), dict) else _r
        out = str((_rr or {}).get("output") or (_r or {}).get("output") or "")
        ck("允许后真跑出结果", "153" in out, (out or json.dumps(_r, ensure_ascii=False))[:120])

    # ---------- 胶囊（回归） ----------
    # 必须显式给 path（位置参数，不是字典）：不给的话后端会弹系统文件对话框
    # （无人值守时 15 秒超时失败）。导出是后台任务，等文件头出现即可。
    cap = os.path.join(DATA, "cap153.spark")
    try:
        os.remove(cap)
    except Exception:
        pass
    st, d = post("export_spark", cap, timeout=180)
    head = b""
    t0 = time.time()
    while time.time() - t0 < 90:
        if os.path.isfile(cap):
            head = open(cap, "rb").read(12)
            if head == b"XJY_SPARK_V2":
                break
        time.sleep(0.5)
    ck("胶囊仍是加密格式", head == b"XJY_SPARK_V2", "%r %s" % (head, json.dumps(d, ensure_ascii=False)[:120]))

    # ---------- 公告 ----------
    st, d = post("get_notices", timeout=20)
    ids = [i.get("id") for i in ((d.get("items")) or [])]
    ck("公告只有 v153", ids == ["2026-10-28-v153"], ids)

    # ---------- 退出 ----------
    mei_before = len([n for n in os.listdir(tdir) if n.startswith("_MEI")])
    st, d = post("quit_app", timeout=30)
    rc = None
    t0 = time.time()
    while time.time() - t0 < 20:
        rc = proc.poll()
        if rc is not None:
            break
        time.sleep(0.2)
    ck("quit_app 后进程退出", rc is not None, rc)
    time.sleep(1.0)
    mei_after = len([n for n in os.listdir(tdir) if n.startswith("_MEI")])
    ck("退出后 _MEI 没变多", mei_after <= mei_before, "%d -> %d" % (mei_before, mei_after))
    if proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)

    print("\n== 汇总 ==  %d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    for n in FAIL:
        print("  FAIL: " + n)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
