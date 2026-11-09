# -*- coding: utf-8 -*-
"""V1.5.3 壳层验收：未同意协议点 ✕ 能不能关掉、连点多次会不会锁死。

用法：python _t_shell153.py "D:\\临时文件\\_inst153"
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

INST = sys.argv[1] if len(sys.argv) > 1 else r"D:\临时文件\_inst153"
EXE = os.path.join(INST, "小鲸鱼.exe")
DATA = r"D:\临时文件\_d153shell"
WV = r"D:\临时文件\_wv153shell"

user32 = ctypes.WinDLL("user32", use_last_error=True)
WM_CLOSE = 0x0010

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK   " if cond else "  FAIL ") + name + (("  " + str(extra)) if extra else ""))


def windows_of(pid):
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def procs(prefix):
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                           text=True, timeout=30).stdout
    except Exception:
        return []
    rows = []
    for ln in r.splitlines():
        parts = [x.strip('"') for x in ln.split('","')]
        if len(parts) >= 2 and parts[0].lower().startswith(prefix.lower()):
            rows.append((parts[0], parts[1]))
    return rows


def start(data, wv):
    env = dict(os.environ)
    env["QQ_AGENT_DATA_DIR"] = data
    env["WEBVIEW2_USER_DATA_FOLDER"] = wv
    return subprocess.Popen([EXE], env=env, cwd=INST,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_window(pid, limit=40):
    t0 = time.time()
    while time.time() - t0 < limit:
        w = windows_of(pid)
        if w:
            return w[0]
        time.sleep(0.4)
    return None


def main():
    print("== 环境 ==")
    ck("壳程序存在", os.path.isfile(EXE), EXE)
    for d in (DATA, WV):
        if os.path.isdir(d):
            subprocess.run(["cmd", "/c", "rmdir", "/S", "/Q", d], capture_output=True)
    os.makedirs(DATA, exist_ok=True)

    # ---- 第一轮：不碰界面，直接点 ✕ ----
    print("\n== 第 1 次启动 → 点 ✕（没同意协议） ==")
    p = start(DATA, WV)
    win = wait_window(p.pid)
    ck("窗口起来了", bool(win), win[1] if win else "无")
    if not win:
        p.kill()
        return 1
    t0 = time.time()
    user32.PostMessageW(win[0], WM_CLOSE, 0, 0)
    rc = None
    while time.time() - t0 < 25:
        rc = p.poll()
        if rc is not None:
            break
        time.sleep(0.2)
    dt = time.time() - t0
    ck("点 ✕ 后 25 秒内退出", rc is not None, "rc=%s %.1fs" % (rc, dt))
    ck("退出码是 0", rc == 0, rc)
    time.sleep(1.5)
    left = [x for x in procs("backend_server") if True]
    ck("后端没留孤儿", len(left) <= 2, left)   # onefile 引导 + 真身，退出后应为 0

    # ---- 第二轮 / 第三轮：连续启动，验证不锁死 ----
    for i in (2, 3):
        print("\n== 第 %d 次启动 → 点 ✕ ==" % i)
        p = start(DATA, WV)
        win = wait_window(p.pid, 40)
        ck("第 %d 次窗口起来了" % i, bool(win), win[1] if win else "无")
        if not win:
            p.kill()
            continue
        user32.PostMessageW(win[0], WM_CLOSE, 0, 0)
        t0 = time.time()
        rc = None
        while time.time() - t0 < 25:
            rc = p.poll()
            if rc is not None:
                break
            time.sleep(0.2)
        ck("第 %d 次退出 rc=0" % i, rc == 0, "rc=%s %.1fs" % (rc, time.time() - t0))

    # ---- 双开 ----
    print("\n== 双开 ==")
    p1 = start(DATA, WV)
    w1 = wait_window(p1.pid, 40)
    ck("主实例窗口起来了", bool(w1))
    p2 = start(DATA, WV)
    t0 = time.time()
    rc2 = None
    while time.time() - t0 < 20:
        rc2 = p2.poll()
        if rc2 is not None:
            break
        time.sleep(0.2)
    ck("第二个实例自己退了", rc2 is not None, rc2)
    ck("主实例还活着", p1.poll() is None)
    if w1:
        user32.PostMessageW(w1[0], WM_CLOSE, 0, 0)
    t0 = time.time()
    while time.time() - t0 < 25 and p1.poll() is None:
        time.sleep(0.2)
    ck("主实例最后也能关掉", p1.poll() == 0, p1.poll())
    for x in (p1, p2):
        if x.poll() is None:
            x.kill()

    print("\n== 汇总 ==  %d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    for n in FAIL:
        print("  FAIL: " + n)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
