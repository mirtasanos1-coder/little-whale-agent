# -*- coding: utf-8 -*-
"""小鲸鱼 Agent 后端服务层（v1.2.0 起无头，界面在 Tauri）

功能（全部通过 backend_server.py 暴露成 HTTP / WebSocket）：
  - 控制台：一键启动/停止后台 agent 子进程，实时滚动日志
  - 模型切换：列出已加载的模型，一键切换并重启 Agent
  - 人设编辑：读取/覆写 personas.json（纯 JSON，不再写 Python 代码），保存后自动重启
  - 记忆查看：表格展示 messages 与 user_events

设计要点：
  1. 打包成 exe 后 sys.executable 就是本程序，无法再 `python agent.py`。
     所以启动子进程时用 `本程序 --agent` 自举：app_ui.py 检测到 --agent
     参数就转而执行 agent.main()，这样打包后依然只有一个 exe。
  2. 日志通过增量 offset 读取，避免每次全量传输。
  3. v1.2.0：本文件里**不再有任何 pywebview / 窗口 / 托盘代码**，
     窗口与进程生命周期交给 Tauri（Rust）。直接 `python app_ui.py`
     等于启动后端服务（`backend_server.main()`）。
  3. 所有 API 都返回 JSON 可序列化的 dict，绝不向 JS 抛异常。
  4. 人设用 personas.json，前端直接编辑 JSON，Python 只做校验与落盘。
"""
import base64
import json
import os
import re
import socket
import subprocess
import sys
import queue as _queue
import threading
import time
import traceback


def _ndef(v, d):
    """v1.8.8：0 / 0.0 / False 都是合法配置值，只有 None 和空串才回落默认。

    以前到处写 `cfg.get(K) or N`，用户把某项设成 0 会被强行改回默认值 ——
    「主动关心最短间隔 0 分钟」「每日上限 0 条」「生图概率 0」全都会失效。
    """
    if v is None:
        return d
    if isinstance(v, str) and not v.strip():
        return d
    return v



BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 打包成 exe 后，__file__ 指向 _internal/ 临时目录，
# 但用户期望日志/数据库/人设都在 exe 旁边。所以显式告诉 agent.py 用哪个数据目录。
IS_FROZEN = bool(getattr(sys, "frozen", False))
if IS_FROZEN:
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
    # v1.2.1：Tauri 把后端放在 <安装目录>\resources\ 下，而用户期望数据在安装目录本身。
    # 不往上跳一层的话，配置/记忆/日志会全跑进 resources 里，看着很怪，卸载也可能被一起删。
    _up = os.path.dirname(DATA_DIR)
    if os.path.basename(DATA_DIR).lower() == "resources" and os.path.isdir(_up):
        DATA_DIR = _up
else:
    DATA_DIR = BASE_DIR
# 允许外部强制指定（调试/多实例）
DATA_DIR = os.environ.get("QQ_AGENT_DATA_DIR") or DATA_DIR
# v1.2.2：把算好的数据目录回写进环境变量，config/memory/personas 等模块
# 都按它（优先级最高）取 DATA_DIR —— 否则它们各按 sys.executable 算，
# Tauri 布局下会算成 <安装目录>\resources\，跟这里的 DATA_DIR 不一致。
os.environ.setdefault("QQ_AGENT_DATA_DIR", DATA_DIR)

# v1.5.2（QA No-Go 修复一）：退出标志。
# 以前 quit_app()/win_close() 直接往 `_QUIT["flag"]` 里写，但这个字典从来没被定义过 →
# 点「退出」抛 NameError 被外层 except 吞掉，界面只看到一句「NameError」，退出逻辑也没走完。
_QUIT = {"flag": False}

AGENT_PY = os.path.join(BASE_DIR, "agent.py")
CONFIG_JSON = os.path.join(DATA_DIR, "config.json")
PERSONAS_JSON = os.path.join(DATA_DIR, "personas.json")
LOG_FILE = os.path.join(DATA_DIR, "agent.log")

# v7：记忆数据库单独放进 memory/ 子目录（用户提的"记忆模块的文件夹在哪"）
# 搬库逻辑统一放在 memory.py 里（memory.memory_dir() 会顺手迁移旧库），
# 这里只负责把路径对上，避免两处各写一份迁移代码。
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
DB_FILE = os.path.join(MEMORY_DIR, "memory.db")
LEGACY_DB = os.path.join(DATA_DIR, "memory.db")


def _ensure_memory_dir():
    try:
        import memory as _mem
        _mem.memory_dir(DATA_DIR)
    except Exception:
        try:
            os.makedirs(MEMORY_DIR, exist_ok=True)
        except OSError:
            pass


_ensure_memory_dir()

# agent.py 的备用 webhook 端口（用来热切换模型等）
AGENT_WEBHOOK = "http://127.0.0.1:5700"

# ==================== v1.2.0：无头化（去掉 pywebview / 窗口 / 托盘）====================
# 重构说明（Tauri 前后端分离）
# --------------------------
# 这个文件以前既是「桌面窗口」又是「API 实现」：main() 里 create_window() + webview.start()
# 把界面开起来，Api 类的方法通过 js_api 直接暴露给页面。
#
# V1.2.0 起界面交给 Tauri（Rust + Web 前端），Python 只做**无头 API 服务**：
#   - 窗口/托盘/WebView2 相关代码（_set_window、apply_dark_titlebar、apply_window_mode、
#     enforce_window_mode、start_tray/stop_tray、_activation_ping/_activation_serve、
#     main()、P0-5 的白屏看门狗）全部删除 —— 那些是 pywebview 时代的东西，
#     Tauri 用系统原生窗口，白屏问题从根上不存在了。
#   - `Api` 类原样保留：它是纯业务门面（271 个方法），backend_server.py 用
#     `POST /api/{method}` 把它整体映射成 HTTP，一个方法都不用重写。
#   - `win_min / win_toggle_max / win_close / tray_start / win_resize` 这几个
#     「操作窗口」的方法还在（前端可能仍在调），但现在只返回一句说明，
#     真正的窗口操作由 Tauri 的 Rust 端接管。
#   - 文件选择框从 pywebview 换成 tkinter（同一台机器上的原生对话框），
#     前端也可以直接把路径当参数传进来。
_UI_START_TS = time.time()

# 兼容字段：老代码里 `_WIN.get("ref")` 到处查，现在恒为 None（无窗口）。
_WIN = {"ref": None}
_NO_WINDOW_MSG = "V1.2.0 起窗口由 Tauri 控制，这个接口已停用"


# v1.3.2（QA BUG-11）：原生对话框必须带超时。
# 以前 `_pick_file()` 直接在请求线程里调 tkinter —— 无人值守（QA 脚本、无桌面会话）
# 时对话框弹不出来也不会报错，**直接把请求线程挂死 180 秒**，接口表现为永久超时。
# 现在所有原生对话框都丢进子线程，主线程 `join(timeout)`，超时就返回明确错误，
# 请求立刻返回「对话框打不开，请手动指定路径」。
_DIALOG_TIMEOUT = 15.0


_DIALOG_TIMEOUT = 15.0

# v1.5.1（P1-1）：原生对话框的单例宿主。
# tkinter 只允许在**建 root 的那个线程**里跑事件循环；以前每次调用都新开一个
# 线程 + 新建 tk.Tk()，超时后旧线程还活着，第二次调用就在另一个线程里再建
# root → RuntimeError + 进程级崩溃。现在只有一个常驻宿主线程。
_DLG_HOST = {"ready": False, "dead": False, "busy": False, "root": None}
_DLG_QUEUE = _queue.Queue()
_DLG_LOCK = threading.Lock()
_DLG_BOOT = threading.Lock()


def _dlg_host_loop():
    import tkinter as tk
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        _DLG_HOST["root"] = root
        _DLG_HOST["ready"] = True
    except Exception as e:            # 没有桌面会话（无人值守）也会走到这
        _DLG_HOST["dead"] = True
        _append_log(f"[UI] 原生对话框不可用（{type(e).__name__}: {e}），"
                    f"可以由界面直接把路径传进来", "WARN")
        return

    def _poll():
        try:
            job = _DLG_QUEUE.get_nowait()
        except _queue.Empty:
            if not _DLG_HOST["dead"]:
                root.after(40, _poll)
            return
        fn, box = job
        # 上一个已经放弃了就直接扔掉，别再弹第二个
        if box.get("abandoned"):
            box["done"] = True
            box["value"] = ""
            if not _DLG_HOST["dead"]:
                root.after(40, _poll)
            return
        try:
            box["value"] = fn(root) or ""
        except Exception as e:        # noqa: BLE001
            box["err"] = e
        finally:
            box["done"] = True
            box["ev"].set()
            _DLG_HOST["busy"] = False
        if not _DLG_HOST["dead"]:
            root.after(40, _poll)

    try:
        root.after(40, _poll)
        root.mainloop()
    except Exception:
        pass
    finally:
        _DLG_HOST["dead"] = True
        _DLG_HOST["ready"] = False


def _dlg_ensure_host(wait=4.0):
    """确保宿主线程在跑；返回是否可用。"""
    if _DLG_HOST["dead"]:
        return False
    if _DLG_HOST["ready"]:
        return True
    with _DLG_BOOT:
        if _DLG_HOST["ready"]:
            return True
        if not _DLG_HOST.get("thread"):
            th = threading.Thread(target=_dlg_host_loop, daemon=True,
                                  name="tk-dialog-host")
            _DLG_HOST["thread"] = th
            th.start()
    end = time.time() + max(0.5, float(wait or 4.0))
    while time.time() < end:
        if _DLG_HOST["ready"]:
            return True
        if _DLG_HOST["dead"]:
            return False
        time.sleep(0.05)
    return False


def _close_own_dialogs(wait=3.0):
    """把本进程还开着的原生对话框（窗口类 #32770）关掉，并等它退干净。

    v1.6.1（QA P1-3）：tkinter 的 filedialog 是模态的 —— 用户不点的话，
    宿主线程会永远卡在 mainloop 里，`_DLG_HOST["busy"]` 一直是 True，
    之后再点「选择文件」只会回「没选文件」，永远弹不出来。
    这里用 PostMessage(WM_CLOSE) 把那几个窗口关掉，让宿主线程回到 _poll，
    busy 自然复位；实在还不退就手动复位标记（请求会排队，不会开两个）。
    """
    n = 0
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        WM_CLOSE = 0x0010
        pid = os.getpid()
        found = []

        def _cb(hwnd, _lp):
            try:
                p = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if int(p.value) != pid:
                    return True
                buf = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(hwnd, buf, 64)
                if buf.value == "#32770" and user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                              wintypes.LPARAM)(_cb), 0)
        for h in found:
            user32.PostMessageW(wintypes.HWND(h), WM_CLOSE, 0, 0)
        n = len(found)
        end = time.time() + max(0.5, float(wait or 3.0))
        while time.time() < end and _DLG_HOST["busy"]:
            time.sleep(0.05)
    except Exception as e:                     # noqa: BLE001
        _append_log(f"[UI] 关掉卡住的对话框时出错（{type(e).__name__}: {e}）", "WARN")
    if _DLG_HOST["busy"]:
        # 窗口没关掉也要让用户能再点（请求进队列，等前一个退掉自动弹）
        _DLG_HOST["busy"] = False
        _append_log("[UI] 对话框还没退干净，先把忙标记复位（下次点击会排队打开）", "WARN")
    return n


def _run_dialog(fn, timeout=_DIALOG_TIMEOUT):
    """把对话框任务丢给常驻宿主线程，最多等 timeout 秒。

    返回 (状态, 结果)，状态 ∈ {"ok","cancel","timeout","error","busy"}。
    """
    if not _dlg_ensure_host():
        return "error", ""
    if not _DLG_LOCK.acquire(timeout=2.0):
        return "busy", ""
    try:
        if _DLG_HOST["busy"]:
            _append_log("[UI] 上一个对话框还开着，这次直接返回；"
                        "先把手上的窗口关掉", "WARN")
            return "busy", ""
        box = {"value": "", "done": False, "err": None,
               "abandoned": False, "ev": threading.Event()}
        _DLG_HOST["busy"] = True
        _DLG_QUEUE.put((fn, box))
        if not box["ev"].wait(max(1.0, float(timeout or _DIALOG_TIMEOUT))):
            box["abandoned"] = True
            # v1.6.1（QA P1-3）：超时不能只「放弃」，必须把还开着的窗口关掉并
            # 复位 busy —— 否则下一次点「选择文件」永远回「没选文件」。
            n_closed = _close_own_dialogs()
            _append_log(f"[UI] 文件对话框 {float(timeout):.0f} 秒没有响应，"
                        f"已关掉 {n_closed} 个卡住的窗口（再点一次就能重新弹出）", "WARN")
            return "timeout", ""
        if box["err"] is not None:
            e = box["err"]
            _DLG_HOST["busy"] = False
            _append_log(f"[UI] 文件对话框打不开（{type(e).__name__}: {e}）；"
                        f"可以让界面直接把路径传进来", "WARN")
            return "error", ""
        val = str(box["value"] or "")
        return ("ok", val) if val else ("cancel", "")
    finally:
        _DLG_LOCK.release()


def _pick_file_ex(save=False, default_name="", ext="json", timeout=_DIALOG_TIMEOUT):
    """同 _pick_file，但把「取消 / 超时 / 出错 / 上一个还开着」区分开返回。"""
    exts = ([ext] if isinstance(ext, str) else list(ext or []))
    exts = [str(x).strip(".") for x in exts if str(x).strip(".")]
    label = " / ".join("." + x for x in exts).upper() if exts else ""
    pats = " ".join("*." + x for x in exts) if exts else ""
    types = [("%s 文件" % label, pats)] if exts else []

    def _do(root):
        from tkinter import filedialog
        if save:
            return filedialog.asksaveasfilename(
                parent=root, title="保存到…", initialfile=default_name or "",
                defaultextension=("." + str(ext)) if isinstance(ext, str) and ext else "",
                filetypes=types)
        return filedialog.askopenfilename(parent=root, title="选择文件",
                                          filetypes=types)

    return _run_dialog(_do, timeout=timeout)


def _pick_file(save=False, default_name="", ext="json", timeout=_DIALOG_TIMEOUT):
    """弹系统文件对话框，返回选中路径（取消/超时/失败都返回 ""）。"""
    _st, _path = _pick_file_ex(save=save, default_name=default_name, ext=ext,
                               timeout=timeout)
    return _path


def _pick_dir(title="选择目录", timeout=_DIALOG_TIMEOUT):
    """选目录（走常驻宿主，带超时）。"""
    def _do(root):
        from tkinter import filedialog
        return filedialog.askdirectory(parent=root, title=title)

    _st, _p = _run_dialog(_do, timeout=timeout)
    return _p

def _icon_dir():
    return os.path.join(BASE_DIR, "icons")


def _icon_path(name):
    p = os.path.join(_icon_dir(), name)
    return p if os.path.exists(p) else ""


# 配置中心（放在这里统一 import，别在函数里各写一份 import config）
import config as _cfg_mod


# ==================== 打开目录 / 文件（v6 重写）====================
# 为什么不用 os.startfile
# ----------------------
# 实测（_probe_open.py / _probe_open2.py）：Python 侧 os.startfile 返回成功、
# 前端 toast 也显示"已打开"，但用户看到的却是"点了没反应"。
# 原因有两个叠加：
#   1. pywebview 的窗口持有前台焦点，资源管理器窗口被拉起来后落在背后；
#   2. Windows 11 默认"在同一窗口打开文件夹"，于是新目录直接塞进一个本来
#      就在最小化/背后的资源管理器窗口里 —— 完全没有"弹出来"的视觉反馈。
# 所以这里改成：自己启动 explorer.exe，然后主动找到那个窗口把它提到最前。
def _explorer_windows():
    """返回 {hwnd: 窗口标题}，只列可见的顶层窗口。"""
    out = {}
    if os.name != "nt":
        return out
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out[int(hwnd)] = buf.value
            except Exception:
                pass
            return True

        user32.EnumWindows(proto(_cb), 0)
    except Exception:
        pass
    return out


def _focus_window(hwnd, tries=3):
    """把某个窗口恢复并提到最前。"""
    if os.name != "nt" or not hwnd:
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)          # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        # 前台锁：先假装按一下 Alt，这是最省事且有效的绕过方式
        user32.keybd_event(0x12, 0, 0, 0)   # ALT down
        user32.keybd_event(0x12, 0, 2, 0)   # ALT up
        for _ in range(tries):
            if user32.SetForegroundWindow(hwnd):
                return True
            time.sleep(0.15)
        return bool(user32.GetForegroundWindow() == hwnd)
    except Exception:
        return False


def _focus_explorer_for(path, before=None, wait_sec=3.0):
    """在 before 之后新出现的窗口里，找标题包含目录名的那个，提到最前。

    返回 (found_hwnd, title)。找不到返回 (0, "")。
    """
    if os.name != "nt":
        return 0, ""
    before = before or {}
    base = os.path.basename(os.path.normpath(str(path))) or str(path)
    base_low = base.lower()
    deadline = time.time() + max(0.5, wait_sec)
    while time.time() < deadline:
        wins = _explorer_windows()
        # 优先：新出现的、标题里带目录名的
        for hwnd, title in wins.items():
            if hwnd in before:
                continue
            t = (title or "").lower()
            if base_low and base_low in t:
                _focus_window(hwnd)
                return hwnd, title
        # 其次：任何标题里带目录名的（Windows 11 复用了老窗口的情况）
        for hwnd, title in wins.items():
            t = (title or "").lower()
            if base_low and base_low in t:
                _focus_window(hwnd)
                return hwnd, title
        time.sleep(0.2)
    return 0, ""


def shell_open(path, wait_sec=3.0):
    """用资源管理器打开目录/文件，并尽量把窗口提到最前。

    返回 {"ok": bool, "msg": str, "path": str, "focused": bool}
    """
    p = os.path.abspath(str(path or ""))
    if not p:
        return {"ok": False, "msg": "路径是空的", "path": "", "focused": False}
    if not os.path.exists(p):
        try:
            os.makedirs(p, exist_ok=True)
        except Exception:
            pass
    if os.name != "nt":
        try:
            subprocess.Popen(["xdg-open", p])
            return {"ok": True, "msg": "已打开 " + p, "path": p, "focused": False}
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}",
                    "path": p, "focused": False}

    before = _explorer_windows()
    errs = []
    launched = False
    # 方式 1：直接给 explorer.exe 路径（可控，且能观察到新窗口）
    try:
        subprocess.Popen(["explorer.exe", p], close_fds=True)
        launched = True
    except Exception as e:
        errs.append(f"explorer.exe: {e}")
    # 方式 2：os.startfile 兜底（走 shell 默认关联）
    if not launched:
        try:
            os.startfile(p)   # noqa: S606
            launched = True
        except Exception as e:
            errs.append(f"startfile: {e}")
    # 方式 3：cmd start 兜底
    if not launched:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", p],
                             shell=False, close_fds=True)
            launched = True
        except Exception as e:
            errs.append(f"cmd start: {e}")
    if not launched:
        return {"ok": False, "msg": "打开失败：" + "；".join(errs) or "未知原因",
                "path": p, "focused": False}

    hwnd, title = _focus_explorer_for(p, before, wait_sec=wait_sec)
    if hwnd:
        return {"ok": True, "msg": f"已打开 {p}", "path": p, "focused": True,
                "title": title}
    return {"ok": True,
            "msg": f"已打开 {p}（窗口可能被本程序挡在后面，"
                   f"看任务栏的资源管理器）",
            "path": p, "focused": False}


def _open_result(path, label):
    """所有 open_* API 的统一返回：{ok, msg, path, focused}。"""
    r = shell_open(path)
    if r.get("ok"):
        return {"ok": True, "msg": f"{label}：{r.get('path') or path}",
                "path": r.get("path") or path,
                "focused": bool(r.get("focused"))}
    return {"ok": False, "msg": f"{label}失败：{r.get('msg') or '未知原因'}",
            "path": r.get("path") or str(path), "focused": False}


def _img_data_url(path, max_bytes=6 * 1024 * 1024):
    """把小图读成 data URL 给前端 <img> 用（人设图缩略图）。

    超过 max_bytes 直接返回 ""，免得一条消息塞进几十 MB 把 webview 卡死。
    """
    try:
        p = str(path or "").strip()
        if not p or not os.path.isfile(p):
            return ""
        if os.path.getsize(p) > max_bytes:
            return ""
        ext = os.path.splitext(p)[1].lower().lstrip(".")
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                "webp": "webp", "gif": "gif"}.get(ext, "")
        if not mime:
            return ""
        with open(p, "rb") as f:
            raw = f.read()
        return f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""



# ==================== v1.1.0：打开外部链接 ====================
# 用途：模型设置里「去申请 Key」「去官网下载 LM Studio」这类按钮。
# 只放行 http(s)：避免有人在自定义提供方里塞 file:// / javascript: 之类的伪协议。
_URL_SCHEMES = ("http://", "https://")


def shell_open_url(url):
    """用系统默认浏览器打开一个 http(s) 链接。返回 {ok, msg, url}。"""
    u = str(url or "").strip()
    if not u:
        return {"ok": False, "msg": "链接是空的", "url": ""}
    if not u.lower().startswith(_URL_SCHEMES):
        return {"ok": False, "msg": "只允许打开 http(s) 链接", "url": u}
    try:
        if os.name == "nt":
            os.startfile(u)          # noqa: S606 - Windows 走默认浏览器
        else:
            import webbrowser
            webbrowser.open(u)
        return {"ok": True, "msg": "已在浏览器打开", "url": u}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "url": u}


# 打包成 --windowed 后 stdout 为 None，任何 print 都会炸，这里兜住
try:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
except Exception:
    pass

IS_FROZEN = bool(getattr(sys, "frozen", False))

_proc = None
_proc_lock = threading.Lock()


# ==================== 进程管理 ====================
def _agent_cmd():
    """构造启动 agent 的命令行。打包态用自举，源码态用 python agent.py。"""
    if IS_FROZEN:
        return [sys.executable, "--agent"]
    py = sys.executable or "python"
    return [py, AGENT_PY]


def _spawn_flags():
    """Windows 下隐藏子进程黑窗口，并允许后续整树 kill。"""
    if os.name != "nt":
        return 0
    flags = 0
    flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags


def _is_running():
    global _proc
    with _proc_lock:
        if _proc is None:
            return False
        if _proc.poll() is None:
            return True
        _proc = None
        return False


def _agent_boot_err_path():
    try:
        return os.path.join(DATA_DIR, "agent_boot.err.log")
    except Exception:
        return ""


def _read_tail(path, limit=600):
    """读日志尾巴（给「启动失败」提示用）。"""
    try:
        if not path or not os.path.isfile(path):
            return ""
        with open(path, "rb") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4096))
                data = f.read()
            except Exception:
                data = f.read()
        txt = data.decode("utf-8", "replace").strip()
        lines = [x for x in txt.splitlines() if x.strip()]
        return "\n".join(lines[-6:])[:limit]
    except Exception:
        return ""


def start_agent():
    global _proc
    try:
        with _proc_lock:
            if _proc is not None and _proc.poll() is None:
                return {"ok": True, "msg": f"Agent 已在运行中（PID {_proc.pid}）", "pid": _proc.pid}
            cmd = _agent_cmd()
            env = os.environ.copy()
            # 把数据目录显式传给子进程：打包态下 __file__ 在 _internal，
            # 子进程必须知道"用户可见目录"才能把 log/db 写在 exe 旁边。
            env["QQ_AGENT_DATA_DIR"] = DATA_DIR
            # QA 终审 P1-2：stderr 落到文件里，启动失败时能把真实原因（端口被占等）
            # 一并告诉用户，而不是「已启动（PID xxx）」然后什么也没发生。
            err_path = _agent_boot_err_path()
            err_f = subprocess.DEVNULL
            if err_path:
                try:
                    err_f = open(err_path, "wb")
                except Exception:
                    err_f = subprocess.DEVNULL
            _proc = subprocess.Popen(
                cmd,
                cwd=DATA_DIR,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                creationflags=_spawn_flags(),
            )
            pid = _proc.pid
        # 探活（QA 终审 P1-2）：只拿到 PID 就报成功 = 假成功。
        # Agent 现在会因为「5709 端口被残留进程占着 / 缺依赖」在几百毫秒内退出，
        # 以前用户点了「启动 Agent」看到「已启动」，其实什么都没起来。
        deadline = time.time() + 1.5
        while time.time() < deadline and _proc.poll() is None:
            time.sleep(0.1)
        rc = _proc.poll()
        if rc is not None:
            tail = _read_tail(err_path) or _read_tail(os.path.join(DATA_DIR, "agent.log"))
            with _proc_lock:
                _proc = None
            _append_log(f"[UI] Agent 启动后立刻退出（exit {rc}）：{tail or '无输出'}", "ERROR")
            hint = ""
            if "5709" in tail or "address already in use" in tail.lower() or "10048" in tail:
                hint = "（端口 5709 被占用：多半是上次的 Agent 没退干净，先点「停止」或等几秒再试）"
            msg = f"Agent 启动失败：进程立刻退出（exit {rc}）{hint}"
            if tail:
                msg += "\n" + tail.splitlines()[-1][:200]
            return {"ok": False, "msg": msg, "pid": pid, "exit_code": rc}
        _append_log(f"[UI] 启动 Agent 子进程 PID={pid} cmd={' '.join(cmd)}")
        _track_child(pid)
        return {"ok": True, "msg": f"Agent 已启动（PID {pid}）", "pid": pid}
    except Exception as e:
        _append_log(f"[UI] 启动失败: {e}")
        return {"ok": False, "msg": f"启动失败: {e}"}


def stop_agent():
    global _proc
    try:
        with _proc_lock:
            if _proc is None or _proc.poll() is not None:
                _proc = None
                return {"ok": True, "msg": "Agent 当前未运行"}
            pid = _proc.pid
            # Windows 上 agent 可能还有子线程/子进程，用 taskkill /T 整树清理
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   capture_output=True, timeout=10)
                except Exception:
                    _proc.terminate()
            else:
                _proc.terminate()
            try:
                _proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    _proc.kill()
                except Exception:
                    pass
            _proc = None
        _append_log(f"[UI] 已停止 Agent 子进程 PID={pid}")
        return {"ok": True, "msg": f"Agent 已停止（PID {pid}）"}
    except Exception as e:
        _append_log(f"[UI] 停止失败: {e}")
        return {"ok": False, "msg": f"停止失败: {e}"}



def restart_agent(wait=1.0):
       """v1.9.11.2：真的重启 Agent 子进程。

       换人设 / 改人设都必须换进程 —— Agent 的 SYSTEM_PROMPT 在进程启动时
       生成，光改 config.json 不会影响已经在跑的那个进程。
       返回 {"ok","msg","pid","restarted"}；restarted=True 表示这次真的重启了。
       """
       global _proc
       try:
           running = _proc is not None and _proc.poll() is None
           if running:
               st = stop_agent()
               if not st.get("ok"):
                   return {"ok": False, "msg": "重启失败：旧进程没停下来（%s）" % st.get("msg"),
                           "pid": 0, "restarted": False}
               try:
                   time.sleep(max(0.2, float(wait or 1.0)))
               except Exception:
                   pass
           r = start_agent()
           r = dict(r or {})
           r["restarted"] = bool(running)
           if r.get("ok"):
               if running:
                   r["msg"] = "Agent 已重启（PID %s），新的人设已生效" % r.get("pid")
               else:
                   r["msg"] = "Agent 已启动（PID %s）" % r.get("pid")
           _append_log("[UI] restart_agent: running=%s ok=%s" % (running, r.get("ok")))
           return r
       except Exception as e:
           _append_log("[UI] 重启 Agent 失败: %s" % e, "ERROR")
           return {"ok": False, "msg": "重启 Agent 失败: %s" % e, "pid": 0, "restarted": False}

# ==================== v1.1.3：重启自身（任务一「等不及了，即刻重启！」） ====================
# 只收自己拉起来的进程：Agent 子进程、本程序启动的 LM Studio / Ollama、托盘。
# 用户自己开的 QQ / LM Studio 不在其中 —— 重启自己的控制台不该顺手关别人的东西。
_CHILD_PIDS = []


def _base_dir():
    """程序所在目录（打包态=exe 所在目录；源码态=本文件所在目录）。

    v1.1.3 修：`_relaunch_cmd()`、`system_selfcheck()`、目录选择等好几处都在调
    这个函数，但它从来没被定义过 —— 一点「等不及了，立刻重启」就抛
    `NameError: name '_base_dir' is not defined`，前端再把异常逐条弹出来。
    """
    try:
        if IS_FROZEN:
            return os.path.dirname(os.path.abspath(sys.executable))
    except Exception:
        pass
    return BASE_DIR


def _track_child(pid):
    """登记本程序拉起的子进程，重启/退出时按 PID 整树收掉。"""
    try:
        pid = int(pid)
    except Exception:
        return
    if pid and pid not in _CHILD_PIDS:
        _CHILD_PIDS.append(pid)


def _kill_tracked():
    gone = []
    for pid in list(_CHILD_PIDS):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=8)
            else:
                import signal as _sig
                os.kill(pid, _sig.SIGTERM)
            gone.append(pid)
        except Exception:
            pass
    _CHILD_PIDS.clear()
    return gone


def _relaunch_cmd():
    """拼重启自己用的命令行：(argv, cwd)。

    打包态 sys.executable 就是小鲸鱼.exe，直接再执行一次；
    源码态用 python app_ui.py（没有 app_ui.py 就退 run.py）。

    v1.1.4 补：把窗口模式用参数带过去（--frameless / --no-frameless）。
    以前只靠 config.json，一旦写盘不是最新的（或者被老进程覆盖），
    就会出现「重启了但无边框没生效」。参数是这条链路的最终裁决。
    """
    base = _base_dir()
    try:
        want_fl = bool(_cfg_mod.get("window_frameless", True))
    except Exception:
        want_fl = True
    flag = ["--frameless"] if want_fl else ["--no-frameless"]
    if getattr(sys, "frozen", False):
        return [sys.executable] + flag, base
    entry = os.path.join(base, "app_ui.py")
    if not os.path.exists(entry):
        entry = os.path.join(base, "run.py")
    return [sys.executable, entry] + flag, base


def _hard_exit():
    """退干净：只杀自己，不要再 /T。

    v1.1.4 修：以前是 `taskkill /PID 自己 /T /F`，会把「刚拉起来的新实例」
    一起杀掉（新实例是本进程的子进程）→ 用户看到的是「重启后程序彻底没了」。
    现在新实例用 cmd start 脱离进程树，这里只收自己。
    """
    try:
        _append_log("[UI] 退出进程（不再用 taskkill /T，避免误杀重启后的新实例）")
    except Exception:
        pass
    os._exit(0)


def _spawn_detached(argv, cwd):
    """拉起一个不属于本进程树的新实例。

    用 `cmd /c start "" <argv>`：start 会立刻返回、新进程挂到 explorer 下，
    之后杀本进程的进程树也带不走它。这是 Windows 上最稳的「自己重启自己」。
    同时带一个环境变量 QQ_AGENT_RESTART=1：新实例看到它就知道「我是重启来的」，
    不要去做单实例探测（那时老实例还没退，端口还占着，探测会误判成重复启动而退出）。
    """
    env = dict(os.environ)
    env["QQ_AGENT_RESTART"] = "1"
    if os.name == "nt":
        cmd = ["cmd", "/c", "start", "", ] + [str(a) for a in argv]
        subprocess.Popen(cmd, cwd=cwd, close_fds=True, env=env,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    else:
        subprocess.Popen([str(a) for a in argv], cwd=cwd, close_fds=True, env=env,
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)


def restart_app():
    """彻底结束子进程并重新拉起自己（前端确认后调用）。"""
    try:
        try:
            stop_agent()
        except Exception:
            pass
        gone = _kill_tracked()
        argv, cwd = _relaunch_cmd()
        _spawn_detached(argv, cwd)
        _append_log(f"[UI] 重启：已拉起 {' '.join(argv)}；收掉旧子进程 {gone or '无'}；0.8s 后退出")
        threading.Timer(0.8, _hard_exit).start()
        return {"ok": True, "msg": "正在重启…"}
    except Exception as e:
        _append_log(f"[UI] 重启失败: {e}")
        return {"ok": False, "msg": f"重启失败: {type(e).__name__}: {e}"}


# ==================== v1.1.4：单实例锁 ====================
# 以前同一个人开两个控制台，叉掉一个另一个还驻留，托盘点着还有两个小鲸鱼。
# 做法：第一个实例在 127.0.0.1 的固定端口上监听；后开的实例连上发一句 show，
# 然后自己退出 —— 效果就是「不会开出第二个窗口，而是把已经在的那个叫到前面」。
_ACT_HOST = "127.0.0.1"
_ACT_PORT = 59321


# ---- v1.9.0：发布包完整性校验（Ed25519 签名清单） ----
# v1.9.8：从「只算一次」改成「按包状态指纹缓存」。
# 老做法把第一次的结果永久留在内存里：进程起来之后有人换掉
# resources\backend_server.exe，前端刷新一万次也还是「没问题」——
# 运行期篡改等于完全不报（QA 复现）。现在每次进来先做一次 stat 指纹,
# 清单 / 签名 / 清单里每个文件的 (大小, mtime) 只要变一位，就重新真验一遍。
_SIG_CACHE = {"key": None, "r": None}


def _pkg_stamp():
    """包状态指纹：只 stat，不读内容（4 秒一轮的轮询也不心疼）。

    拿不到就返回 None —— 那种情况下宁可不走缓存，每次真验，也不能
    因为「指纹算不出来」而给出「缓存命中、没问题」的假结论。
    """
    try:
        import signing as _sg
        base = _sg.find_package_root()
        if not base:
            return None
        man = None
        for _c in (os.path.join(base, "manifest.sha256"),
                   os.path.join(base, "resources", "manifest.sha256")):
            if os.path.isfile(_c):
                man = _c
                break
        watch = []
        for _p in (man,
                   os.path.join(base, "manifest.sig"),
                   os.path.join(base, "resources", "manifest.sig")):
            if _p:
                watch.append(_p)
        if man:
            try:
                with open(man, "r", encoding="utf-8", errors="replace") as _f:
                    for _h, _rel in _sg._parse_manifest(_f.read()):
                        watch.append(os.path.join(base, _rel.replace("/", os.sep)))
            except Exception:
                # 清单读不动就当指纹无效，强制每次真验
                return None
        parts = []
        for _p in watch:
            try:
                _st = os.stat(_p)
                parts.append((_p.lower(), _st.st_size, int(_st.st_mtime)))
            except OSError:
                parts.append((_p.lower(), -1, 0))
        return tuple(parts)
    except Exception:
        return None


def verify_package(force=False):
    """校验程序目录里的 manifest.sha256 / manifest.sig。

    返回 {ok, signed, tampered, changed, missing, msg, algo}
    tampered=True 表示「签名对不上」——这是有人在伪造，前端必须拦住。
    只是没有清单（自己从源码编译）不算错，signed=False 而已。

    v1.9.8：包被动过（大小或 mtime 变了）就自动重算，force=True 强制重算。
    """
    global _SIG_CACHE
    key = _pkg_stamp()
    if (not force) and key is not None and _SIG_CACHE.get("key") == key \
            and _SIG_CACHE.get("r") is not None:
        return _SIG_CACHE["r"]
    try:
        import signing
        r = signing.status()
    except Exception as e:
        r = {"ok": True, "signed": False, "tampered": False, "changed": [],
             "missing": [], "msg": "签名模块不可用，跳过校验：%s" % e,
             "algo": "ed25519"}
    _SIG_CACHE = {"key": key, "r": r}
    return r


def get_status():
    """返回运行状态与关键路径，供前端初始化。"""
    try:
        import config as cfg_mod
        running = _is_running()
        pid = _proc.pid if (running and _proc) else None
        cfg = _load_config()
        try:
            ctx = _provider_ctx()
            cur_model, cur_base = ctx["model"], ctx["base"]
            cur_provider, cur_is_cloud = ctx["provider"], ctx["is_cloud"]
            cur_pname = ctx["name"]
        except Exception:
            cur_model = cfg.get("lm_model") or ""
            cur_base = cfg.get("lm_base") or ""
            cur_provider, cur_is_cloud, cur_pname = "local", False, "本地 LM Studio"
        return {
            "ok": True,
            "running": running,
            "pid": pid,
            "base_dir": DATA_DIR,
            "frozen": IS_FROZEN,
            "log_file": LOG_FILE,
            "db_file": DB_FILE,
            "config_file": CONFIG_JSON,
            "personas_file": PERSONAS_JSON,
            "model": cur_model,
            "lm_base": cur_base,
            "provider": cur_provider,
            "provider_name": cur_pname,
            "is_cloud": cur_is_cloud,
            # v1.1.0：左上角小字统一成「正式版 · v1.1.0」，程序目录只在「关于」里出现
            "version": getattr(cfg_mod, "APP_VERSION", "1.1.0"),
            "version_tag": getattr(cfg_mod, "APP_VERSION_TAG", "正式版"),
            "subtitle": getattr(cfg_mod, "APP_SUBTITLE", "正式版 · v1.1.0"),
            "author": getattr(cfg_mod, "APP_AUTHOR", "Ruao"),
            # v1.9.0：包完整性（tampered=True 时前端要拦住）
            "package": verify_package(),
            # v1.1.1：无边框窗口 / 托盘 / 鲸鱼图标
            "frameless": bool(cfg.get("window_frameless", True)),
            "tray_enabled": bool(cfg.get("tray_enabled", True)),
            "title": getattr(cfg_mod, "APP_NAME", "小鲸鱼 Agent 控制台"),
            "icon": bool(_icon_path("whale.png")),
            "log_size": os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0,
        }
    except Exception as e:
        return {"ok": False, "running": False, "msg": str(e)}


# ==================== 日志 ====================
def _append_log(line, level="INFO"):
    """UI 自己的动作也写进同一个日志文件，保证时间线完整。

    v1.1.5.1：支持第二参数（INFO/WARN/ERROR）。之前全仓有 11 处调用都传了
    level，而函数只收 1 个参数 → 那些地方直接抛 TypeError（例如
    apply_window_mode 最后一行），无边框/拖盘/文件对话框出错时反而把正常
    流程打断，且错误日志一个字都没写进去。
    """
    try:
        stamp = time.strftime("[%Y-%m-%d %H:%M:%S] ")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(stamp + f"[{str(level or 'INFO').upper()}] " + str(line) + "\n")
    except Exception:
        pass


def get_perf():
    """v1.1.6（任务九）：性能监控面板的数据。

    全部只读，拿不到就明确写「需要 psutil」，不编数字。
    返回 {ok, ui:{cpu,mem_mb,threads}, agent:{...}, queue:{...}, db:{...}, deps:{...}}
    """
    out = {"ok": True, "ui": {}, "agent": {}, "queue": {}, "db": {}, "deps": {},
           "uptime": int(time.time() - _UI_START_TS)}
    try:
        import psutil
        _has = True
    except Exception:
        psutil = None
        _has = False
    out["deps"]["psutil"] = _has
    me = os.getpid()
    try:
        p = psutil.Process(me) if _has else None
        if p is not None:
            mi = p.memory_info()
            out["ui"] = {"pid": me,
                         "cpu": round(p.cpu_percent(interval=0.12), 1),
                         "mem_mb": round(mi.rss / 1048576.0, 1),
                         "threads": p.num_threads()}
        else:
            out["ui"] = {"pid": me, "cpu": None, "mem_mb": None,
                         "threads": threading.active_count()}
    except Exception as e:
        out["ui"] = {"pid": me, "err": str(e)[:120]}
    # 子进程（Agent / NapCat / LM Studio 如果是我们起的）
    try:
        _kids = []
        if _has:
            for ch in psutil.Process(me).children(recursive=True):
                try:
                    _kids.append({"pid": ch.pid, "name": ch.name()[:40],
                                  "mem_mb": round(ch.memory_info().rss / 1048576.0, 1),
                                  "cpu": round(ch.cpu_percent(interval=0), 1)})
                except Exception:
                    continue
        out["agent"]["children"] = sorted(_kids, key=lambda x: -(x.get("mem_mb") or 0))[:12]
        out["agent"]["running"] = bool(_is_running()) if "_is_running" in globals() else None
    except Exception as e:
        out["agent"]["err"] = str(e)[:120]
    # 优先级队列：Agent 是另一个进程，读不到它的内存 —— 它每秒把
    # pq_state() 落到 <数据目录>/pq_stats.json，这里读那个文件。
    # （v1.8.11：老代码打 http://127.0.0.1:<agent_port>/api/pq，但全项目
    #   没有一处实现过这个路由，所以「请求队列」那一格永远是「—」。）
    try:
        import json as _json
        _pqf = os.path.join(DATA_DIR, "pq_stats.json")
        if os.path.exists(_pqf):
            with open(_pqf, "r", encoding="utf-8") as _f:
                _d = _json.load(_f) or {}
            _age = time.time() - float(_d.get("at") or 0)
            out["queue"] = {"fg": int(_d.get("fg") or 0),
                              "bg_run": int(_d.get("bg_run") or 0),
                              "bg_wait": int(_d.get("bg_wait") or 0),
                              "threads": int(_d.get("threads") or 0),
                              "enabled": bool(_d.get("enabled", True)),
                              "age": int(_age), "stale": _age > 120}
    except Exception as e:
        out["queue"] = {"err": str(e)[:80]}
    # 数据库体积与行数（诊断慢的关键：表越大越慢）
    # v1.8.11：老代码 getattr(_cfg_mod, "DB_FILE", "") 永远是空串
    #（DB_FILE 是 app_ui 自己的模块级常量，不在 config 里），所以
    #「数据库」那一格也永远「—」；count_messages() 还必须带 session_key，
    #无参调用直接 TypeError 被吞。这里改成模块级 DB_FILE + 只读直查。
    try:
        dbf = DB_FILE
        if dbf and os.path.exists(dbf):
            out["db"]["file"] = os.path.basename(dbf)
            out["db"]["mb"] = round(os.path.getsize(dbf) / 1048576.0, 2)
            import sqlite3 as _sq
            _con = _sq.connect("file:" + dbf.replace("\\", "/") + "?mode=ro",
                                  uri=True, timeout=2.0)
            try:
                _cur = _con.cursor()
                for _tb, _kk in (("messages", "messages"), ("perm_memory", "perm")):
                    try:
                        _cur.execute("SELECT COUNT(*) FROM " + _tb)
                        out["db"][_kk] = int(_cur.fetchone()[0])
                    except Exception:
                        out["db"][_kk] = None
            finally:
                _con.close()
        else:
            out["db"]["err"] = "找不到数据库文件"
    except Exception as e:
        out["db"]["err"] = str(e)[:120]
    return out


def get_introspection():
    """v1.1.6（任务十）：自省 / 梦境页的数据。

    三块：潜意识日志（每天 3 条自我反思）、深度思考存档（think_log）、
    记忆衰减状态（多少条老记忆、开关与天数）。全部只读。
    """
    out = {"ok": True, "thoughts": [], "thinking": [], "decay": {}}
    try:
        import memory as _mem_mod
        m = _mem_mod.Memory()
    except Exception as e:
        return {"ok": False, "msg": f"读记忆库失败：{e}"}
    try:
        out["thoughts"] = m.list_thoughts(80) if hasattr(m, "list_thoughts") else []
    except Exception as e:
        out["thoughts_err"] = str(e)[:120]
    try:
        rows = m.recent_thinking(40) or []
        out["thinking"] = [{"user": (r[0] if len(r) > 0 else ""),
                            "thinking": (r[1] if len(r) > 1 else ""),
                            "reply": (r[2] if len(r) > 2 else ""),
                            "time": (r[3] if len(r) > 3 else 0)} for r in rows]
    except Exception as e:
        out["thinking_err"] = str(e)[:120]
    try:
        days = int(_ndef(_cfg_mod.get("memory_decay_days"), 180))
        enabled = bool(_cfg_mod.get("memory_decay_enabled"))
        old = m.old_perm_memories(days, limit=500) if hasattr(m, "old_perm_memories") else []
        out["decay"] = {"enabled": enabled, "days": days,
                        "old": len(old or []),
                        "max": int(_ndef(_cfg_mod.get("memory_decay_max"), 10)),
                        "len": int(_ndef(_cfg_mod.get("memory_decay_len"), 15)),
                        "sub_enabled": bool(_cfg_mod.get("subconscious_enabled")),
                        "sub_hour": int(_ndef(_cfg_mod.get("subconscious_hour"), 3)),
                        "sub_minute": int(_cfg_mod.get("subconscious_minute") or 0),
                        "sub_count": int(_ndef(_cfg_mod.get("subconscious_count"), 3)),
                        "sub_last_day": (out["thoughts"][0].get("day") if out.get("thoughts") else ""),
                        "thought_total": len(out.get("thoughts") or [])}
    except Exception as e:
        out["decay"] = {"err": str(e)[:120]}
    try:
        out["emotion"] = m.emotion_stats(7) if hasattr(m, "emotion_stats") else {}
    except Exception:
        out["emotion"] = {}
    return out


def _reflect_ask():
    """给 subconscious 用的模型调用（和 agent 里潜意识线程用的是同一条路）。

    agent 模块 import 期间不起线程，只有 main() 才起，所以这里 import 是安全的。
    """
    import agent as _ag
    return lambda p: _ag.ask_model_raw(p, system=_ag.SYSTEM_PROMPT,
                                       max_tokens=500, temperature=0.5)


def run_reflect_now(force=True, count=None):
    """立刻反思一次（v1.3.0 ③：用户在「自省」页点一下就能写，不必等到点）。"""
    try:
        import subconscious
        import memory as _mem_mod
        m = _mem_mod.Memory()
        r = subconscious.daily_reflection(m, ask=_reflect_ask(), log=_append_log,
                                          force=bool(force), count=count)
        if isinstance(r, dict) and r.get("ok"):
            n = int(r.get("added") or 0)
            msg = "它写了 %d 条心里话" % n if n else (r.get("msg") or "这次没写新的")
            return {"ok": True, "msg": msg, "added": n,
                    "thoughts": r.get("thoughts") or []}
        return {"ok": False, "msg": (r or {}).get("msg") or "反思失败"}
    except Exception as e:
        return {"ok": False, "msg": f"反思失败：{e}"}


def run_decay_now(max_items=None):
    """立刻跑一次记忆衰减（把超过天数的长期记忆压成一句话）。"""
    try:
        import subconscious
        import memory as _mem_mod
        m = _mem_mod.Memory()
        r = subconscious.decay_memories(m, ask=_reflect_ask(), log=_append_log,
                                        max_items=max_items)
        if not isinstance(r, dict):
            r = {"ok": True, "changed": int(r or 0)}
        if r.get("ok") is False:
            return {"ok": False, "msg": r.get("msg") or "衰减失败"}
        n = int(r.get("changed") or 0)
        if r.get("msg") == "没开":
            return {"ok": False, "msg": "记忆衰减现在是关着的，先打开开关再试"}
        return {"ok": True, "msg": f"衰减完成：压缩了 {n} 条老记忆", "n": n}
    except Exception as e:
        return {"ok": False, "msg": f"衰减失败：{e}"}



# ==================== V1.0.0-Beta 任务五：半自动更新 ====================
# 只做两件事：① 问 GitHub Releases 有没有新版本；② 把下载页丢给系统浏览器。
# 不自动覆盖文件 —— Tauri 是编译产物，偷偷换 jar 那套不适用，预览版够用就行。
def check_release():
    """检查 GitHub Releases 最新版本（失败只返回 ok=False + 人话原因）。"""
    try:
        import updater
        return updater.latest_release()
    except Exception as e:
        return {"ok": False, "configured": False, "has_new": False,
                "msg": f"更新模块不可用：{e}", "current": "", "html_url": "", "assets": []}


def open_release_page(url=""):
    """在系统浏览器里打开 Release 下载页（默认用配置里的仓库）。"""
    u = str(url or "").strip()
    if not u:
        try:
            import updater
            u = updater.releases_url()
        except Exception:
            u = ""
    if not u:
        return {"ok": False, "msg": "还没填仓库（设置 → 在线更新 → 仓库），填完这里才能打开发布页"}
    if not re.match(r"^https://", u, re.I):
        return {"ok": False, "msg": "只允许 https 链接"}
    try:
        shell_open_url(u)
        return {"ok": True, "msg": f"已在浏览器打开：{u}", "url": u}
    except Exception as e:
        return {"ok": False, "msg": f"打开失败：{e}", "url": u}

def get_semantic():
    """v1.1.6 核心任务一：语义记忆状态（不联网不发请求，只报配置与条数）。"""
    try:
        import semantic
        st = semantic.stats()
        st.update(_embed_backend_info())
        # V1.0.0-Beta 任务二：把「热记忆缓存」开关一起报给界面
        try:
            import agent as _ag
            st["hot_cache"] = _ag.hot_mem_state()
        except Exception:
            st["hot_cache"] = {"enabled": bool(_cfg_mod.get("memory_cache_enabled", True)),
                               "size": 0, "max": 3, "keys": []}
        st["cache_enabled"] = bool(st["hot_cache"].get("enabled"))
        return st
    except Exception as e:
        return {"ok": False, "msg": f"语义模块不可用：{e}", "total": 0, "with_vec": 0}


_EMBED_BACKEND_CN = {
    "auto": "自动（有本地模型就本地）",
    "onnx": "只用本地 BGE",
    "server": "只用模型服务",
    "keyword": "只用关键词",
}


def _embed_backend_info():
    """v1.1.6 补丁 5：当前向量后端 + 实际生效情况（给记忆页显示用）。"""
    b = str(_cfg_mod.get("embed_backend") or "auto").strip().lower()
    if b not in _EMBED_BACKEND_CN:
        b = "auto"
    out = {"backend": b, "cn": _EMBED_BACKEND_CN[b], "local_bge": False, "accel": None, "dim": 0}
    try:
        import semantic
        st = semantic.stats()
        out.update({
            "local_bge": bool(st.get("local_bge")),
            "accel": st.get("accel"),
            "dim": st.get("dim") or 0,
            "mode": st.get("mode"),
        })
    except Exception:
        pass
    return out


def save_embed_backend(backend="auto"):
    """v1.1.6 补丁 5：切换向量后端（auto / onnx / server / keyword）。"""
    b = str(backend or "auto").strip().lower()
    if b not in ("auto", "onnx", "server", "keyword"):
        return {"ok": False, "msg": "后端只能是 auto / onnx / server / keyword"}
    _cfg_mod.set_checked("embed_backend", b)
    try:
        import semantic
        st = semantic.stats()
        return {"ok": True, "msg": f"已切到「{_embed_backend_info().get('cn')}」", "backend": b, "stats": st}
    except Exception as e:
        return {"ok": True, "msg": f"已保存（语义模块暂不可用：{e}）", "backend": b}


def semantic_probe(query="", k=5):
    """界面上试一条召回：输入一句话，看能召回什么（不写库）。"""
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "msg": "先输入一句话", "hits": []}
    try:
        import semantic
        hits = semantic.search(q, k=int(k or 5), min_score=0.0)
        return {"ok": True, "mode": semantic.available(), "hits": hits}
    except Exception as e:
        return {"ok": False, "msg": f"检索失败：{e}", "hits": []}


def semantic_reindex(limit=800):
    """补索引 + 重建维度不对的向量（换了 embedding 模型后点这个）。"""
    try:
        import semantic
        fixed = semantic.rebuild_vectors(limit=max(int(limit or 800), 3000))
        n = semantic.reindex(limit=int(limit or 800))
        msg = f"补索引完成：新写入 {n} 条"
        if fixed:
            msg += f"，重建向量 {fixed} 条"
        return {"ok": True, "msg": msg, "n": n, "fixed": fixed, "stats": semantic.stats()}
    except Exception as e:
        return {"ok": False, "msg": f"重建失败：{e}"}


def chat_probe(text="", timeout=120):
    """v1.1.6 任务二：控制台「快速对话测试」。

    直接问一次模型（走当前优先级链），拿来验证「改了配置到底出不出话」。

    v1.3.2（QA P2）：模型不可用时以前会拿到兜底话术「脑子卡壳了……」却仍回
    ok=True，用户会误判「连通正常」。现在不请求兜底话术（allow_fallback=False），
    拿不到真回复就返回 ok=False + fallback=True + 一句人话。
    返回 {ok, reply, model, ms, msg, fallback}；失败不抛异常。
    """
    q = str(text or "").strip()
    if not q:
        return {"ok": False, "fallback": False, "msg": "先说一句想测什么"}
    t0 = time.time()
    try:
        import agent as ag
        reply = ag.ask_model([], q, allow_fallback=False,
                             detail="控制台 · 快速对话测试")
        ms = int((time.time() - t0) * 1000)
        if not reply:
            return {"ok": False, "fallback": True, "ms": ms,
                    "reply": "",
                    "msg": "模型未连通，当前为降级回复（真实对话会走兜底话术，不代表模型好了）。"
                           "去「模型设置」看服务是否在跑、端口对不对，或看控制台日志里的失败原因。"}
        model = ""
        try:
            import providers as _pv
            ch = _pv.chain()
            model = (ch[0].get("model") if ch else "") or ""
        except Exception:
            model = ""
        return {"ok": True, "fallback": False, "reply": str(reply), "model": model, "ms": ms}
    except Exception as e:
        return {"ok": False, "fallback": True,
                "msg": f"模型未连通，当前为降级回复（{e}）。"
                       "去「模型设置」看服务是否在跑、端口对不对，或看控制台日志里的失败原因。",
                "ms": int((time.time() - t0) * 1000)}


def get_logs(offset=0, max_bytes=200000):
    """增量读取日志。

    offset: 上次读到的字节位置；0 表示从尾部取最近 max_bytes。
    返回 {"ok","text","offset","size","reset"}
    """
    try:
        if not os.path.exists(LOG_FILE):
            return {"ok": True, "text": "", "offset": 0, "size": 0, "reset": True}
        size = os.path.getsize(LOG_FILE)
        offset = int(offset or 0)
        reset = False
        if offset <= 0 or offset > size:
            # 首次读取或日志被截断/轮转：从尾部取一段
            start = max(0, size - max_bytes)
            reset = True
        else:
            start = offset
        if start == size:
            return {"ok": True, "text": "", "offset": size, "size": size, "reset": False}
        with open(LOG_FILE, "rb") as f:
            f.seek(start)
            raw = f.read(min(max_bytes, size - start))
        text = raw.decode("utf-8", errors="replace")
        if reset and start > 0:
            # 从中间截断时丢掉半行
            nl = text.find("\n")
            if nl >= 0:
                text = text[nl + 1:]
        return {"ok": True, "text": text, "offset": start + len(raw),
                "size": size, "reset": reset}
    except Exception as e:
        return {"ok": False, "text": f"读取日志失败: {e}", "offset": 0, "size": 0, "reset": False}


def clear_logs():
    try:
        open(LOG_FILE, "w", encoding="utf-8").close()
        return {"ok": True, "msg": "日志已清空"}
    except Exception as e:
        return {"ok": False, "msg": f"清空失败: {e}"}


# ==================== 人设（JSON） ====================
def _read_personas_raw():
    """读 personas.json 原文"""
    if not os.path.exists(PERSONAS_JSON):
        # 用 personas.py 的兜底生成一份
        try:
            import personas as pm
            pm.ensure_file()
        except Exception:
            pass
    if not os.path.exists(PERSONAS_JSON):
        return None
    with open(PERSONAS_JSON, "r", encoding="utf-8") as f:
        return f.read()


def load_personas():
    """读取 personas.json，返回原文 + 解析后的结构，供前端渲染"""
    try:
        raw = _read_personas_raw()
        if raw is None:
            return {"ok": False, "content": "", "msg": "personas.json 不存在且无法生成"}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return {"ok": False, "content": raw,
                    "msg": f"JSON 解析失败（第 {e.lineno} 行第 {e.colno} 列）：{e.msg}"}
        personas = data.get("personas") or {}
        items = []
        for key, val in personas.items():
            if not isinstance(val, dict):
                continue
            items.append({
                "key": key,
                "name": val.get("name") or key,
                "persona_len": len(val.get("persona") or ""),
                "rules_len": len(val.get("rules") or ""),
                "active": key == data.get("active"),
            })
        return {"ok": True, "content": raw, "path": PERSONAS_JSON,
                "active": data.get("active"), "items": items,
                "size": len(raw)}
    except Exception as e:
        return {"ok": False, "content": "", "msg": f"读取失败: {e}"}


def save_personas(content, restart=True):
    """校验 JSON → 备份 → 写盘 →（可选）重启 Agent"""
    try:
        if not content or not str(content).strip():
            return {"ok": False, "msg": "内容为空"}
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            msg = f"JSON 语法错误（第 {e.lineno} 行第 {e.colno} 列）：{e.msg}"
            _append_log(f"[UI] 人设保存被拒绝: {msg}")
            return {"ok": False, "msg": msg}

        # 复用 personas.py 的校验逻辑，保证 UI 与 Agent 判定一致
        try:
            import personas as pm
            ok, msg = pm.validate(data)
        except Exception as e:
            return {"ok": False, "msg": f"校验器不可用: {e}"}
        if not ok:
            _append_log(f"[UI] 人设保存被拒绝: {msg}")
            return {"ok": False, "msg": msg}

        # 备份
        try:
            if os.path.exists(PERSONAS_JSON):
                bak = PERSONAS_JSON + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
                with open(PERSONAS_JSON, "r", encoding="utf-8") as f:
                    old = f.read()
                with open(bak, "w", encoding="utf-8") as f:
                    f.write(old)
        except Exception:
            pass

        # 原子写
        tmp = PERSONAS_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PERSONAS_JSON)
        _append_log(f"[UI] personas.json 已更新（active={data.get('active')}）")

        restarted = False
        if restart:
            stop_agent()
            time.sleep(0.6)
            r = start_agent()
            restarted = bool(r.get("ok"))
            _append_log(f"[UI] 人设已保存，Agent 重启: {r.get('msg')}")

        return {"ok": True,
                "msg": "人设已保存" + ("，并已重启 Agent" if restarted else ""),
                "restarted": restarted, "active": data.get("active")}
    except Exception as e:
        return {"ok": False, "msg": f"保存失败: {e}"}


def set_active_persona(key):
    """只切换当前人设，不编辑内容"""
    try:
        raw = _read_personas_raw()
        if raw is None:
            return {"ok": False, "msg": "personas.json 不存在"}
        data = json.loads(raw)
        personas = data.get("personas") or {}
        if key not in personas:
            return {"ok": False, "msg": f"人设 {key!r} 不存在，可选：{list(personas)}"}
        data["active"] = key
        tmp = PERSONAS_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, PERSONAS_JSON)
        _append_log(f"[UI] 切换人设为 {key}")
        return {"ok": True, "msg": f"已切换到 {personas[key].get('name') or key}",
                "active": key}
    except Exception as e:
        return {"ok": False, "msg": f"切换失败: {e}"}


# ==================== 模型切换 ====================
def _load_config():
    try:
        import config as cfg_mod
        return cfg_mod.load()
    except Exception:
        try:
            with open(CONFIG_JSON, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def _save_config(cfg):
    try:
        import config as cfg_mod
        cfg_mod.save(cfg)
        return True
    except Exception:
        try:
            tmp = CONFIG_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_JSON)
            return True
        except Exception:
            return False


def _friendly_net_error(base, exc, provider_name=""):
    """把 requests 的长串异常翻译成用户能照着做的话。

    本地 LM Studio 和云端 API 的失败原因完全不同，必须分开说，
    否则用云端的人会看到"LM Studio 没启动"这种驴唇不对马嘴的提示。
    """
    name = type(exc).__name__
    text = str(exc)
    who = (provider_name or "").strip()
    is_cloud = who and "本地" not in who
    if "ConnectionError" in name or "NewConnectionError" in text or "refused" in text.lower():
        if is_cloud:
            return (f"连不上 {who}（{base}）。\n"
                    f"原因：服务地址填错了，或这台电脑没网 / 被防火墙拦了。\n"
                    f"解决：① 检查「服务地址」是否和提供方文档一致（通常以 /v1 结尾）；\n"
                    f"      ② 浏览器打开 {base}/models 看能不能出 JSON。")
        return (f"连不上 LM Studio 服务（{base}）。\n"
                f"原因：这个端口上没有程序在监听 —— 通常是 LM Studio 开着但本地服务器没启动。\n"
                f"解决：① 点上面的「启动 LM Studio 服务」按钮；或\n"
                f"      ② 打开 LM Studio → 左侧 Developer 标签 → 打开 Start Server（端口填 1234）。")
    if "Timeout" in name:
        if is_cloud:
            return (f"连接 {who} 超时（{base}）。\n"
                    f"原因：网络慢或被拦。可以用「用量看板」旁的超时设置调大一点。")
        return (f"连接 LM Studio 超时（{base}）。\n"
                f"原因：地址可能不对（比如填了别的机器的 IP 但没通），或 LM Studio 卡死了。\n"
                f"解决：确认服务地址，必要时重启 LM Studio。")
    return f"连接 {'云端 ' + who if is_cloud else 'LM Studio'} 出错（{base}）：{name}: {text[:300]}"


def start_lm_server(port=1234):
    """尝试用 LM Studio 自带的 lms CLI 启动本地服务器。

    只在"程序开着但服务器没起"这个最常见场景下有用。
    """
    import glob
    import subprocess

    candidates = []
    home = os.path.expanduser("~")
    candidates.append(os.path.join(home, ".lmstudio", "bin", "lms.exe"))
    for pat in (r"D:\LM\LM Studio\resources\app\.webpack\lms.exe",
                r"C:\Program Files\LM Studio\resources\app\.webpack\lms.exe"):
        candidates.append(pat)
    try:
        for base in (os.environ.get("LOCALAPPDATA", ""),):
            if base:
                candidates.extend(glob.glob(os.path.join(base, "**", "lms.exe"), recursive=True)[:3])
    except Exception:
        pass

    exe = next((p for p in candidates if p and os.path.exists(p)), None)
    if not exe:
        return {"ok": False,
                "msg": "没找到 LM Studio 的 lms.exe。请手动打开 LM Studio → Developer → Start Server。"}
    try:
        r = subprocess.run([exe, "server", "start", "--port", str(port)],
                           capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace")
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        time.sleep(1.5)
        # 用真实请求验证，别只信 CLI 的输出
        import requests
        try:
            h = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=6)
            if h.status_code == 200:
                _append_log(f"[UI] 已启动 LM Studio 服务（端口 {port}）")
                return {"ok": True, "msg": f"LM Studio 服务已启动（端口 {port}），点「刷新列表」即可。"}
        except Exception:
            pass
        return {"ok": False, "msg": f"命令执行了但端口 {port} 仍无响应：{out[:300]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "msg": "启动超时（90s）。请手动在 LM Studio 里点 Start Server。"}
    except Exception as e:
        return {"ok": False, "msg": f"启动失败：{type(e).__name__}: {e}"}


def _find_ollama():
    """找 ollama.exe：PATH → 用户安装目录 → 系统安装目录。"""
    import glob
    import shutil
    p = shutil.which("ollama")
    if p and os.path.exists(p):
        return p
    cands = []
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("PROGRAMFILES", ""),
                 os.environ.get("PROGRAMFILES(X86)", "")):
        if base:
            cands.append(os.path.join(base, "Programs", "Ollama", "ollama.exe"))
            cands.append(os.path.join(base, "Ollama", "ollama.exe"))
    for p in cands:
        if p and os.path.exists(p):
            return p
    try:
        hits = glob.glob(os.path.join(os.environ.get("LOCALAPPDATA", ""), "**", "ollama.exe"),
                         recursive=True)
        if hits:
            return hits[0]
    except Exception:
        pass
    return ""


def start_ollama_server(port=11434):
    """启动 Ollama 服务（ollama serve），并用真实请求验证端口起来了。"""
    import subprocess
    exe = _find_ollama()
    if not exe:
        return {"ok": False,
                "msg": "没找到 ollama.exe。请先安装 Ollama（https://ollama.com/download），装完它会常驻后台。"}
    # 已经开着就别重复起
    import requests
    try:
        h = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=3)
        if h.status_code in (200, 401):
            _append_log("[UI] Ollama 服务已在运行")
            return {"ok": True, "msg": f"Ollama 服务已经在运行（端口 {port}）"}
    except Exception:
        pass
    try:
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        subprocess.Popen([exe, "serve"], **kwargs)
    except Exception as e:
        return {"ok": False, "msg": f"启动失败：{type(e).__name__}: {e}"}
    for _ in range(20):
        time.sleep(1.0)
        try:
            h = requests.get(f"http://127.0.0.1:{port}/v1/models", timeout=3)
            if h.status_code in (200, 401):
                _append_log(f"[UI] 已启动 Ollama 服务（端口 {port}）")
                return {"ok": True, "msg": f"Ollama 服务已启动（端口 {port}），点「拉取」即可看到模型。"}
        except Exception:
            continue
    return {"ok": False,
            "msg": "ollama serve 已执行，但 20 秒内 11434 端口没响应。请手动在终端跑 `ollama serve` 看看报错。"}


def start_local_server(engine=""):
    """按引擎启动本地服务：LM Studio → lms CLI；Ollama → ollama serve。"""
    e = str(engine or "").strip().lower()
    if e not in ("lmstudio", "ollama"):
        try:
            import providers as prov
            e = prov.local_engine()
        except Exception:
            e = "lmstudio"
    if e == "ollama":
        return start_ollama_server(11434)
    return start_lm_server(1234)


def release_vram():
    """尽量把本地模型从显存里卸掉（v1.1.2 用户要的「一键释放」）。

    老实说：LM Studio / Ollama 都没有一个"通用 HTTP 卸载"接口，能做的只有调它们的
    命令行客户端（`lms unload --all` / `ollama stop <model>`）。找不到 CLI 就明说，
    不要假装成功。云端 API 没有显存可释放，直接给提示。
    """
    import shutil
    import subprocess
    try:
        import providers as prov
        eng = prov.local_engine()
        res = prov.resolve()
    except Exception:
        eng, res = "lmstudio", {}
    model = str(res.get("model") or "")
    if not model:
        model = str((__import__("config").load().get("lm_model") or "")).strip()

    def _run(cmd):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=25,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            return p.returncode == 0, out[:400]
        except FileNotFoundError:
            return None, "没找到命令"
        except Exception as e:
            return False, str(e)

    tried = []
    if eng == "ollama":
        exe = shutil.which("ollama")
        if not exe:
            try:
                exe = _find_ollama()
            except Exception:
                exe = None
        if not exe:
            return {"ok": False, "msg": "没找到 ollama 命令，装到 PATH 里才能一键卸载（手动：ollama stop 模型名）"}
        ok, out = _run([exe, "stop", model] if model else [exe, "ps"])
        tried.append(f"ollama stop {model}: {out}")
        if ok:
            return {"ok": True, "msg": f"已让 ollama 卸载「{model or '当前模型'}」，显存应该回来了", "out": tried}
        return {"ok": False, "msg": "执行失败：" + out, "out": tried}

    exe = shutil.which("lms")
    if not exe:
        return {"ok": False,
                "msg": "没找到 lms 命令（LM Studio 的 CLI）。手动办法：LM Studio 里点模型旁的 Eject，或者在设置里把模型卸载",
                "out": tried}
    ok, out = _run([exe, "unload", "--all"])
    tried.append("lms unload --all: " + out)
    if not ok:
        ok2, out2 = _run([exe, "unload", model] if model else [exe, "ps"])
        tried.append(f"lms unload {model}: {out2}")
        if not ok2:
            return {"ok": False, "msg": "执行失败：" + (out2 or out), "out": tried}
    return {"ok": True, "msg": "已让 LM Studio 卸载本地模型，显存应该回来了", "out": tried}


def _provider_ctx():
    """当前生效的提供方参数（本地 / 云端都走这里，别各写一份）。"""
    import providers as prov
    res = prov.resolve()
    return {
        "res": res,
        "provider": res.get("provider") or "local",
        "name": res.get("provider_name") or "本地 LM Studio",
        "base": (res.get("base") or "").rstrip("/"),
        "model": res.get("model") or "",
        "api_key": res.get("api_key") or "",
        "is_cloud": bool(res.get("is_cloud")),
        "key_base": "cloud_base" if res.get("is_cloud") else "lm_base",
        "key_model": "cloud_model" if res.get("is_cloud") else "lm_model",
    }


def list_models(base="", api_key="", provider=""):
    """列出当前提供方可用的模型 + 现在选中的那个。

    以前这里写死读 lm_base（本地 LM Studio），选了云端也照样去敲 127.0.0.1:1234，
    于是用云端的人看到"LM Studio 未启用"——这是错的。现在按提供方走。

    base / api_key / provider 是可选覆盖：界面上刚填完还没点「保存并切换」时，
    可以直接拿输入框里的值去试连，不用先写盘。
    """
    ctx = _provider_ctx()
    base = (str(base or "").strip() or ctx["base"]).rstrip("/")
    current, name = ctx["model"], ctx["name"]
    api_key = str(api_key or "").strip() or ctx["api_key"]
    is_cloud = ctx["is_cloud"]
    eff_provider = ctx["provider"]
    if str(provider or "").strip() and str(provider).strip().lower() != ctx["provider"]:
        # 前端在看另一个提供方（还没保存），按它算云端/本地
        try:
            import providers as _p
            pr = _p.get_provider(provider)
            is_cloud = bool(pr.get("is_cloud"))
            name = pr.get("name") or name
            eff_provider = pr.get("key") or eff_provider
        except Exception:
            pass
    result = {"ok": False, "models": [], "current": current, "base": base,
              "msg": "", "provider": eff_provider, "provider_name": name,
              "is_cloud": is_cloud}
    if is_cloud and not api_key:
        preset_name = name
        result["msg"] = (f"{preset_name} 需要 API Key"
                         if not ctx["res"].get("ok")
                         else (ctx["res"].get("msg") or f"{preset_name} 需要 API Key"))
        result["needs_setup"] = True
        return result
    if not base:
        result["msg"] = "还没填服务地址"
        result["needs_setup"] = True
        return result
    try:
        import requests
        headers = {}
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        r = requests.get(f"{base}/models", timeout=12, headers=headers)
        if r.status_code == 401:
            result["msg"] = (f"{name} 返回 401：API Key 不对或没填。"
                             f"（在「模型来源」里重新填一次 Key 再保存）")
            result["auth_failed"] = True
            return result
        if r.status_code != 200:
            result["msg"] = f"{name} 返回 HTTP {r.status_code}：{(r.text or '')[:200]}"
            return result
        data = r.json()
        ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
        chat_ids = [i for i in ids if "embed" not in i.lower()]
        result["models"] = chat_ids
        result["all_ids"] = ids
        result["ok"] = True
        if not chat_ids:
            result["msg"] = ("云端没有返回可用模型，手动填模型名也行"
                             if ctx["is_cloud"] else
                             "LM Studio 里没有可聊天的模型，请先加载一个")
        elif current not in chat_ids:
            result["msg"] = f"当前配置的 {current or '(空)'} 不在可用列表里，请选一个"
        else:
            result["msg"] = "OK"
        return result
    except Exception as e:
        result["msg"] = _friendly_net_error(base, e, name)
        result["offline"] = True
        return result


def set_model(model, restart=False, kind=""):
    """切换模型并写入 config.json。

    kind="local"/"cloud" 时强制写对应的键；不传则按当前提供方猜。
    v1.1.0 修复：以前在「API 已启用」时保存本地模型，会把本地模型名写进
    cloud_model（云端卡里出现本地模型名），现在由前端明确指定 kind。
    """
    try:
        model = str(model or "").strip()
        if not model:
            return {"ok": False, "msg": "模型名不能为空"}
        ctx = _provider_ctx()
        k = str(kind or "").strip().lower()
        key = ("lm_model" if k == "local" else "cloud_model") if k in ("local", "cloud") \
            else ctx["key_model"]
        ok, msg, _c = _cfg_mod.set_checked(key, model)
        if not ok:
            return {"ok": False, "msg": msg}
        _append_log(f"[UI] 模型切换（{key}）-> {model}")
        restarted = False
        if restart:
            stop_agent()
            time.sleep(0.6)
            r = start_agent()
            restarted = bool(r.get("ok"))
        return {"ok": True, "msg": f"已切换到 {model}" + ("，Agent 已重启" if restarted else ""),
                "model": model, "restarted": restarted, "key": key}
    except Exception as e:
        return {"ok": False, "msg": f"切换失败: {e}"}


def set_model_base(base, model=None, kind=""):
    """切换服务地址（本地换端口 / 云端换地址都用这个）。

    kind="local"/"cloud" 时强制写对应的键，避免「API 已启用时保存本地地址」
    把 lm_base 写进 cloud_base（v1.1.0 修复）。
    """
    try:
        base = str(base or "").strip().rstrip("/")
        if not base.startswith("http"):
            return {"ok": False, "msg": "地址必须以 http:// 或 https:// 开头"}
        ctx = _provider_ctx()
        k = str(kind or "").strip().lower()
        key_base = ("lm_base" if k == "local" else "cloud_base") if k in ("local", "cloud") \
            else ctx["key_base"]
        key_model = ("lm_model" if k == "local" else "cloud_model") if k in ("local", "cloud") \
            else ctx["key_model"]
        ok, msg, _c = _cfg_mod.set_checked(key_base, base)
        if not ok:
            return {"ok": False, "msg": msg}
        if model:
            _cfg_mod.set_checked(key_model, str(model).strip())
        _append_log(f"[UI] 服务地址改为 {base}（{ctx['name']}）")
        return {"ok": True, "msg": f"地址已保存：{base}", "key": key_base}
    except Exception as e:
        return {"ok": False, "msg": f"保存失败: {e}"}


def test_model():
    """真实发一条测试请求，判断当前提供方到底能不能出正文。

    这个按钮专治"加载了但不回复"：推理模型 max_tokens 太小会返回空。
    """
    ctx = _provider_ctx()
    try:
        import agent as ag
        # agent 里的 LM_BASE/LM_MODEL 是导入时算的，切过提供方就必须重新算一次
        try:
            ag.apply_provider(verbose=False)
        except Exception:
            pass
        info = ag.probe_model()
        detail = info.get("detail", "")
        if not info.get("ok") and ("ConnectionError" in detail or "连接" in detail):
            detail = _friendly_net_error(ctx["base"], Exception(detail), ctx["name"])
        return {"ok": bool(info.get("ok")),
                "detail": detail,
                "model": info.get("model"),
                "provider": ctx["provider"],
                "provider_name": ctx["name"],
                "is_cloud": ctx["is_cloud"],
                "is_reasoning": info.get("is_reasoning"),
                "effort_supported": info.get("effort_supported"),
                "effort_sent": info.get("effort_sent"),
                "available": info.get("available", [])}
    except Exception as e:
        return {"ok": False, "detail": f"测试失败: {type(e).__name__}: {e}"}


# v1.5.1（P1-4）：敏感键统一判定。
# 以前只有 cloud_api_key / codeword_token / provider_keys 三个硬编码名字，
# 新加的 imagegen_api_key 就漏成了明文（QA 抓到的安全红线）。
_SECRET_EXACT = {"provider_keys", "codeword", "codeword_token",
                 "cloud_api_key", "imagegen_api_key"}
_SECRET_SUFFIX = ("_api_key", "_apikey", "_key", "_token", "_secret",
                  "_password", "_credential")


def _is_secret_key(k):
    """这个配置键的值是不是密钥（需要加密 + 不回显 + 日志打码）。"""
    kl = str(k or "").strip().lower()
    if not kl:
        return False
    if kl in _SECRET_EXACT:
        return True
    # 注意：不能用"包含"判定 —— max_tokens / max_context_tokens 里也有 token
    return kl.endswith(_SECRET_SUFFIX)


def _encrypt_if_plain(value):
    """明文就加密；空值 / 已加密 / 全星号原样返回。"""
    try:
        import security as _sec
    except Exception:
        return value
    sv = str(value or "").strip()
    if not sv or sv == "***" or _sec.is_encrypted(sv):
        return value
    try:
        return _sec.encrypt_secret(sv)
    except Exception:
        return value


def get_config():
    """把当前配置回显给前端（**敏感项一律不出去**）"""
    try:
        cfg = _load_config()
        # P3-4（QA 终审）：以前只过滤 k 里带 "key" 的，
        # `codeword_token` / 未来的 secret 类字段就会以 `dpapi:...` 密文回显。
        # 注意不能按子串过滤 —— `max_tokens`/`max_context_tokens` 里也有 "token"，
        # 一律滤掉会把设置页的数值项弄没。改成「精确名单 + 后缀」。
        safe = {}
        for k, v in cfg.items():
            if _is_secret_key(k):
                continue
            safe[k] = v
        return {"ok": True, "config": safe, "path": CONFIG_JSON}
    except Exception as e:
        return {"ok": False, "config": {}, "msg": str(e)}


# 允许前端改写的数值键（白名单，避免前端塞进奇怪的东西）
_EDITABLE_NUM = {
    "max_tokens": (64, 32000, int),
    "reasoning_max_tokens": (512, 32000, int),
    "temperature": (0.0, 2.0, float),
    "top_p": (0.0, 1.0, float),
    "max_context_tokens": (1000, 200000, int),
    "request_timeout": (10, 3600, int),
}


def save_config(cfg_in):
    """保存参数（白名单 + 范围校验）"""
    try:
        if not isinstance(cfg_in, dict):
            return {"ok": False, "msg": "参数格式错误"}
        cfg = _load_config()
        changed = []
        for key, val in cfg_in.items():
            if key in _EDITABLE_NUM:
                lo, hi, cast = _EDITABLE_NUM[key]
                try:
                    v = cast(val)
                except Exception:
                    return {"ok": False, "msg": f"{key} 不是合法数字：{val!r}"}
                v = max(lo, min(hi, v))
                if cfg.get(key) != v:
                    changed.append(f"{key}: {cfg.get(key)} -> {v}")
                cfg[key] = v
            elif key == "reasoning_effort":
                v = str(val or "auto").strip().lower()
                if v not in ("auto", "omit", "off", "none", "minimal",
                             "low", "medium", "high", "xhigh"):
                    return {"ok": False, "msg": f"reasoning_effort 取值非法：{v}"}
                if cfg.get(key) != v:
                    changed.append(f"reasoning_effort: {cfg.get(key)} -> {v}")
                cfg[key] = v
            elif key in ("lm_base", "lm_model", "cloud_base", "cloud_model"):
                v = str(val or "").strip()
                if v and cfg.get(key) != v:
                    changed.append(f"{key}: {cfg.get(key)} -> {v}")
                if v:
                    cfg[key] = v
            # 其他键一律忽略
        if not _save_config(cfg):
            return {"ok": False, "msg": "写入 config.json 失败"}
        if changed:
            _append_log("[UI] 配置更新：" + "；".join(changed))
        return {"ok": True,
                "msg": "参数已保存" + (f"（{len(changed)} 项变更）" if changed else "（无变化）"),
                "changed": changed}
    except Exception as e:
        return {"ok": False, "msg": f"保存失败: {e}"}


# ==================== 记忆查看 ====================
def get_memory_data(msg_limit=60, event_limit=60, session_key="", user_id=""):
    """查询数据库，返回消息与事件（JSON 友好）

    v1.1.2：加了 session_key —— 记忆页下拉选了某个会话时，只取那个会话的消息
    （最多 msg_limit 条），不然永远只能看到"最近 60 条"里恰好有的那部分。
    v1.8.4：加了 user_id —— 记忆页切到某个账号时只取那个账号的。
    """
    try:
        from memory import Memory
        m = Memory(DB_FILE)
        try:
            messages = []
            try:
                if session_key:
                    rows = m.recent_rows(str(session_key), int(msg_limit))
                elif user_id:
                    # v1.8.4：只看这个账号的私聊记录（群聊不按账号分，切账号时不混进来）
                    with m.lock:
                        c = m.conn.cursor()
                        c.execute("""SELECT session_key, role, content, ts FROM messages
                                      WHERE session_key=? ORDER BY id DESC LIMIT ?""",
                                   ("private_" + str(user_id), int(msg_limit)))
                        rows = [{"session_key": r[0], "role": r[1], "content": r[2],
                                 "ts": float(r[3] or 0)} for r in c.fetchall()]
                else:
                    rows = m.recent_messages_all(int(msg_limit))
            except Exception:
                c = m.conn.cursor()
                c.execute("""SELECT session_key, role, content, ts FROM messages
                             ORDER BY id DESC LIMIT ?""", (int(msg_limit),))
                rows = [{"session_key": r[0], "role": r[1], "content": r[2], "ts": r[3]}
                        for r in c.fetchall()]
            for r in rows:
                if isinstance(r, dict):
                    item = r
                else:
                    item = {"session_key": r[0], "role": r[1], "content": r[2], "ts": r[3]}
                item["ts_text"] = time.strftime("%m-%d %H:%M:%S",
                                                time.localtime(item.get("ts") or 0))
                messages.append(item)

            events = m.list_events(limit=int(event_limit))
            if user_id:
                # v1.8.4：事件也按账号过滤（待办事件本来就带 user_id）
                events = [e for e in events
                           if str(e.get("user_id") or "") == str(user_id)]
            for ev in events:
                ev["target_text"] = time.strftime("%m-%d %H:%M",
                                                  time.localtime(ev.get("target_time") or 0))
                ev["created_text"] = time.strftime("%m-%d %H:%M",
                                                   time.localtime(ev.get("created_at") or 0))

            stats = m.stats()
            if user_id:
                # v1.8.4：切到某个账号时，统计条也跟着只算这个账号
                try:
                    stats = dict(stats or {})
                    stats["messages"] = sum(
                        s.get("n", 0) for s in m.session_stats()
                        if str(s.get("session_key") or "") == "private_" + str(user_id))
                    stats["perm_memory"] = m.count_perm(str(user_id))
                    stats["diary"] = m.count_diary(str(user_id))
                    stats["pending_events"] = len(
                        [e for e in m.list_events(limit=500, status="pending")
                          if str(e.get("user_id") or "") == str(user_id)])
                except Exception:
                    pass
            return {"ok": True, "messages": messages, "events": events, "stats": stats}
        finally:
            m.close()
    except Exception as e:
        return {"ok": False, "messages": [], "events": [], "stats": {},
                "msg": f"读取数据库失败: {e}"}


def delete_event(event_id):
    """把事件标记为已取消（不物理删除，便于回溯）"""
    try:
        from memory import Memory
        m = Memory(DB_FILE)
        try:
            ok = m.cancel_event(int(event_id))
            return {"ok": bool(ok), "msg": "已取消该事件" if ok else "事件不存在"}
        finally:
            m.close()
    except Exception as e:
        return {"ok": False, "msg": f"操作失败: {e}"}


# ==================== 表情包管理 ====================
STICKER_DIR = os.path.join(DATA_DIR, "stickers")


def get_stickers():
    """列出表情包目录里的图片与标签统计"""
    try:
        import stickers as st
        d = STICKER_DIR
        st.ensure_dir(d)
        info = st.stats(d)
        items = info["items"]
        # WebView2 默认禁止 file:// 页面加载其它 file:// 资源（跨源），
        # 所以缩略图直接用 base64 data URL 内联。27 张 8KB 的图总共 ~200KB，可接受。
        import base64
        for it in items:
            p = os.path.join(d, it["name"])
            try:
                sz = os.path.getsize(p)
                if sz <= 400 * 1024:
                    with open(p, "rb") as f:
                        raw = f.read()
                    ext = os.path.splitext(it["name"])[1].lower().lstrip(".")
                    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                            "gif": "gif", "webp": "webp", "bmp": "bmp"}.get(ext, "png")
                    it["url"] = f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii")
                else:
                    it["url"] = ""
            except Exception:
                it["url"] = ""
        return {"ok": True, "dir": info["dir"], "count": info["count"],
                "by_tag": info["by_tag"], "items": items,
                "emoji_sets": {k: v[:4] for k, v in st.EMOJI_SETS.items()}}
    except Exception as e:
        return {"ok": False, "msg": f"读取表情包失败: {e}",
                "dir": STICKER_DIR, "count": 0, "by_tag": {}, "items": []}


def open_sticker_dir():
    """打开表情包目录（用户往里丢图片）"""
    try:
        import stickers as st
        st.ensure_dir(STICKER_DIR)
    except Exception:
        pass
    return _open_result(STICKER_DIR, "已打开表情包目录")


def test_sticker(text="好耶", to_self=False):
    """测试表情包匹配：给出文本，看会挑中哪张（不发送）"""
    try:
        import stickers as st
        st.ensure_dir(STICKER_DIR)
        emo = st.detect_emotions(text)
        pick = st.pick_sticker(text=text, cfg_dir=STICKER_DIR)
        emoji = st.decorate_emoji(text, chance=1.0)
        return {"ok": True,
                "text": text,
                "emotions": emo,
                "sticker": pick["name"] if pick else None,
                "sticker_tags": (pick or {}).get("tags") or [],
                "with_emoji": emoji}
    except Exception as e:
        return {"ok": False, "msg": f"测试失败: {e}"}


# ==================== 说说状态 ====================
def get_moment_state():
    """读取 agent_state.json 里的说说配额与发布记录"""
    try:
        path = os.path.join(DATA_DIR, "agent_state.json")
        data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        m = data.get("moment") or {}
        cfg = _load_config()
        today = time.strftime("%Y-%m-%d")
        logs = []
        for it in (m.get("log") or [])[-20:]:
            logs.append({
                "ts_text": time.strftime("%m-%d %H:%M",
                                         time.localtime(it.get("ts") or 0)),
                "text": it.get("text") or "",
            })
        last_at = int(m.get("last_at") or 0)
        return {
            "ok": True,
            "enabled": bool(cfg.get("moment_enabled", True)),
            "date": m.get("date") or today,
            "quota": int(m.get("quota") or 0),
            "count": int(m.get("count") or 0),
            "remaining": max(0, int(m.get("quota") or 0) - int(m.get("count") or 0)),
            "last_at_text": time.strftime("%m-%d %H:%M", time.localtime(last_at)) if last_at else "还没发过",
            "logs": logs,
            "range": [int(_ndef(cfg.get("moment_min_per_day"), 1)),
                      int(_ndef(cfg.get("moment_max_per_day"), 10))],
            "hours": cfg.get("moment_hours") or [8, 23],
        }
    except Exception as e:
        return {"ok": False, "msg": f"读取说说状态失败: {e}", "logs": []}


def post_moment_now(text=""):
    """手动发一条说说（测试用）。text 为空则让模型生成。"""
    try:
        import config as cfg_mod
        cfg = cfg_mod.load()
        base = str(cfg.get("lm_base") or "").rstrip("/")
        if text and text.strip():
            content = text.strip()[:300]
            source = "手动"
        else:
            # 走 agent 的生成逻辑（会读配置里的模型）
            import agent
            # v1.4.0：手动点「立即发一条」= 用户已经决定要发，强制生成正文，不再 SKIP
            content = (agent.moment_compose(force=True) or "").strip()
            source = "模型生成"
            if not content:
                _append_log("[UI] 手动发说说：模型没给出正文，已改成手填提示")
                return {"ok": False,
                        "msg": "模型这次没写出正文（连强制模式都空），"
                               "你在右边输入框里写一句、再点立即发一条就行"}
        import requests
        r = requests.post(f"http://127.0.0.1:3003/send_qzone_msg",
                          json={"content": content}, timeout=60)
        try:
            data = r.json()
        except Exception:
            data = {}
        if data.get("status") == "ok":
            _append_log(f"[UI] 手动发说说成功（{source}）：{content[:60]}")
            return {"ok": True, "msg": "说说已发布", "content": content,
                    "tid": (data.get("data") or {}).get("tid") or ""}
        msg = str(data.get("message") or r.text)[:300]
        _append_log(f"[UI] 手动发说说失败：{msg}")
        if "ENOTFOUND" in msg or "getaddrinfo" in msg:
            msg += ("\n（NapCat 进程 DNS 解析失败，请检查 NapCat 所在环境能否访问 "
                    "user.qzone.qq.com；宿主机能解析不代表 NapCat 能）")
        return {"ok": False, "msg": msg, "content": content}
    except Exception as e:
        return {"ok": False, "msg": f"发说说异常: {type(e).__name__}: {e}"}


# ==================== v4：设置（主题 / 自启 / 日记 / 群绑定 / 图标）====================
# ==================== v1.1.0：人设卡片 ====================

def _store_personas():
    import personas_store
    return personas_store


def get_persona_cards():
    try:
        st = _store_personas()
        st.migrate_from_legacy()
        return {"ok": True, "items": st.list_cards(), "dir": st.dir_path(),
                "active": st.active_key(), "active_name": st.active_name()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def save_persona_order(keys):
    """v1.1.2 #34：人设卡拖动后保存顺序。"""
    try:
        st = _store_personas()
        st.migrate_from_legacy()
        ok, msg = st.save_order(keys)
        out = get_persona_cards()
        out["ok"] = bool(ok)
        out["msg"] = msg
        return out
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


# ==================== v1.1.4：人设图（形象参考） ====================
def persona_faces(key=""):
    """某个人设已上传的形象图，顺手带上描述是否已生成。"""
    try:
        st = _store_personas()
        st.migrate_from_legacy()
        k = str(key or "").strip() or st.active_key()
        faces = st.list_faces(k)
        items = []
        for f in faces:
            items.append({"name": f["name"], "size": f["size"],
                          "url": _img_data_url(f["path"])})
        return {"ok": True, "key": k, "items": items,
                "dir": st.faces_dir(k), "desc": st.face_desc(k),
                "summary": st.face_summary(k)}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def add_persona_face(key=""):
    """选一张图加进人设（可多选时会连点几次）。"""
    try:
        st = _store_personas()
        st.migrate_from_legacy()
        k = str(key or "").strip() or st.active_key()
        # v1.2.0：改用 tkinter 多选（原来是 pywebview 的窗口对话框）
        try:
            import tkinter as _tk
            from tkinter import filedialog as _fd
            _root = _tk.Tk()
            _root.withdraw()
            _root.attributes("-topmost", True)
            try:
                _sel = _fd.askopenfilenames(
                    title="选择人设图片（可多选）",
                    filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.gif"),
                               ("所有文件", "*.*")])
            finally:
                try:
                    _root.destroy()
                except Exception:
                    pass
            paths = list(_sel or [])
        except Exception:
            paths = None
        if not paths:
            return {"ok": False, "msg": "没选图", "canceled": True}
        if isinstance(paths, str):
            paths = [paths]
        added, errs = [], []
        for p in list(paths)[:8]:
            ok, msg, nm = st.save_face(k, p)
            if ok:
                added.append(nm)
            else:
                errs.append(msg)
        out = persona_faces(k)
        out["msg"] = (f"已加 {len(added)} 张" if added else "没加成") + \
            (f"；{errs[0]}" if errs else "")
        out["ok"] = bool(added)
        return out
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def delete_persona_face(key, name):
    try:
        st = _store_personas()
        ok, msg = st.delete_face(key, name)
        out = persona_faces(key)
        out["ok"] = ok
        out["msg"] = msg
        return out
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def open_persona_faces(key=""):
    try:
        st = _store_personas()
        k = str(key or "").strip() or st.active_key()
        return _open_result(st.faces_dir(k), "人设图目录")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _face_image_url(path):
    """v1.1.6 ⑫：把形象图转成能给模型看的 data URL；太大/格式怪就先缩一遍。

    以前只试一次 _img_data_url，超过上限或 webp/gif 直接返回 None，
    用户点了「识别长相」只看到「这张图读不出来」，其实图是好的。
    """
    url = _img_data_url(path)
    if url:
        return url
    try:
        from PIL import Image
        import io as _io
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((1024, 1024))
            buf = _io.BytesIO()
            im.save(buf, format="JPEG", quality=88)
        import base64 as _b64
        return "data:image/jpeg;base64," + _b64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        _append_log(f"[人设图] 缩图失败：{e}", "WARN")
        return ""


def describe_persona_face(key=""):
    """让模型看一眼形象图，把「长什么样」存成人设的一部分。

    画图时把这句塞进 prompt，比每次重新读图省钱，也比纯文字猜得准。
    v1.1.6 ⑫：看图失败不再直接报错，而是退化成「按人设文字描一遍」，
    并把原因写清楚（是没配视觉模型、还是图读不出来）。
    """
    try:
        import agent as ag
        st = _store_personas()
        st.migrate_from_legacy()
        k = str(key or "").strip() or st.active_key()
        faces = st.list_faces(k)
        if not faces:
            return {"ok": False, "msg": "还没有人设图，先上传一张"}
        path = faces[0]["path"]
        url = _face_image_url(path)
        prompt = ("看这张角色形象图，用 60~120 字、客观描述她的长相："
                  "发色发长、发型、眼睛颜色、脸型、肤色、大致年龄感、服装风格、气质。"
                  "只描述看得见的，别编故事，别加评价，别用「她看起来」这类开头。")
        txt, why = "", "没有可用路径"
        if url:
            try:
                txt, why = ag.ask_vision(prompt, url, max_tokens=400, tag="/人设图")
            except Exception as e:
                why = f"{type(e).__name__}: {e}"
            txt = str(txt or "").strip()
        if txt:
            st.set_face_desc(k, txt)
            out = persona_faces(k)
            out["ok"] = True
            out["desc_src"] = "vision"
            out["msg"] = "形象描述已生成（用的是看图模型），画图时会照着这个来"
            return out

        # ---------- 降级：用卡片里的文字信息拼一段，至少画图时有东西可用 ----------
        card = st.get_card(k) or {}
        bits = []
        for field, label in (("gender", "性别"), ("age", "年龄感"), ("identity", "身份"),
                             ("style", "说话/穿搭风格"), ("personality", "性格")):
            v = str(card.get(field) or "").strip()
            if v:
                bits.append(f"{label}：{v}")
        name = str(card.get("name") or card.get("nickname") or "").strip()
        head = f"形象以用户上传的人设图为准（{name or '当前人设'}）"
        detail = "；".join(bits) if bits else "具体长相未描述，按上传的形象图来"
        fallback = f"{head}。{detail}。画图时不要自行改变发色、发型和服装主体。"
        st.set_face_desc(k, fallback)
        out = persona_faces(k)
        out["ok"] = True
        out["desc_src"] = "fallback"
        reason_cn = {
            "no_vision_model": "当前模型不支持看图",
            "http_400": "模型拒收图片（多半不支持视觉）",
            "empty": "模型没返回内容",
        }.get(str(why), str(why))
        out["msg"] = (f"看图没成功（{reason_cn}），已先按人设文字生成一份代替描述。"
                      f"想要更准：到「模型设置 → 按功能分工 → 看图识图」选一个支持视觉的模型再点一次。")
        out["hint"] = "建议配置「看图识图」分工（如 gpt-4o-mini / glm-4v / qwen-vl 这类）"
        return out
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}



def get_persona_card(key):
    try:
        st = _store_personas()
        st.migrate_from_legacy()
        c = st.get_card(key)
        if not c:
            return {"ok": False, "msg": "人设不存在"}
        card = {k: v for k, v in c.items() if not k.startswith("_")}
        card["active"] = (c["key"] == st.active_key())
        return {"ok": True, "card": card}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


PERSONA_MAX_CHARS = 2000


def save_persona_card(key, data):
    try:
        st = _store_personas()
        k = st._safe_key(key or (data or {}).get("key") or "persona")
        # v1.5.1：字数上限在后端也要兜住（前端能绕过去，直接调接口就超了）
        _note = ""
        if isinstance(data, dict):
            _p = str(data.get("persona") or "")
            if len(_p) > PERSONA_MAX_CHARS:
                _note = "（正文超过 %d 字，已截断）" % PERSONA_MAX_CHARS
                data = dict(data)
                data["persona"] = _p[:PERSONA_MAX_CHARS]
        ok, msg = st.save_card(k, data or {})
        if ok and _note:
            msg = (msg or "已保存") + _note
        if ok:
            _append_log(f"[人设] 保存卡片 {k}（{len((data or {}).get('persona') or '')} 字）")
            try:
                import agent as agent_mod
                agent_mod.refresh_system_prompt()
            except Exception:
                pass
        return {"ok": ok, "msg": msg, "key": k}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def delete_persona_card(key):
    try:
        ok, msg = _store_personas().delete_card(key)
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def duplicate_persona_card(key, new_key="", new_name=""):
    try:
        st = _store_personas()
        ok, msg, nk = st.duplicate_card(key, new_key, new_name)
        return {"ok": ok, "msg": msg, "key": nk or ""}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def activate_persona(key):
    try:
        ok, msg = _store_personas().set_active(key)
        if ok:
            _append_log(f"[人设] 切换到 {key}")
            try:
                import agent as agent_mod
                agent_mod.refresh_system_prompt()
            except Exception:
                pass
            return {"ok": True, "msg": msg, "need_restart": _is_running()}
        return {"ok": False, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def export_persona_card(key):
    try:
        st = _store_personas()
        c = st.get_card(key)
        name = ((c or {}).get("name") or key) + ".json"
        path = _pick_file(save=True, default_name=name)
        if not path:
            return {"ok": False, "msg": "已取消"}
        ok, msg = st.export_card(key, path)
        return {"ok": ok, "msg": msg, "path": path}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def import_persona_card():
    try:
        path = _pick_file()
        if not path:
            return {"ok": False, "msg": "已取消"}
        ok, msg = _store_personas().import_card(path)
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v1.1.0：记忆导入 / 导出 ====================

def _mt():
    import mem_transfer
    return mem_transfer


def get_memory_exports():
    try:
        mt = _mt()
        due, nxt, desc = mt.auto_export_due()
        try:
            import config as cfg_mod
            enabled = bool(cfg_mod.get("memory_export_enabled", False))
            every = int(_ndef(cfg_mod.get("memory_export_every"), 1))
            unit = str(cfg_mod.get("memory_export_unit") or "day")
            units = [{"value": v, "label": cfg_mod.EXPORT_UNIT_CN.get(v, v)}
                     for v in cfg_mod.EXPORT_UNITS]
        except Exception:
            enabled, every, unit, units = False, 1, "day", []
        imp = {}
        try:
            imp = mt.list_import_dir()
        except Exception:
            imp = {"dir": "", "pending": []}
        return {"ok": True, "items": mt.list_exports(), "dir": mt.export_dir(),
                "import_dir": imp.get("dir") or "", "import_pending": imp.get("pending") or [],
                "enabled": enabled, "due": bool(due),
                "next_ts": nxt, "desc": desc, "every": every, "unit": unit, "units": units,
                "next_text": (time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt))
                              if nxt else "—")}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def do_memory_export(kind="json", path=""):
    try:
        mt = _mt()
        mem = _mem_conn()
        k = str(kind or "json").lower()
        if k == "txt":
            r = mt.export_to_text(mem=mem)
        elif k == "db":
            # v1.3.0 ⑱：整库原样备份（二进制，无损）
            r = mt.export_to_db()
        else:
            r = mt.export_to_file(mem=mem)
        if r.get("ok"):
            _append_log(f"[记忆] {r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def export_memory_kind(kind="perm", fmt="json", path=""):
    """v1.3.0 ⑱：按种类导出（永久/临时/事件/好感/日记/自省/学习）"""
    try:
        mt = _mt()
        mem = _mem_conn()
        r = mt.export_kind(mem=mem, kind=kind, fmt=fmt)
        if r.get("ok"):
            _append_log(f"[记忆] {r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def import_memory_kind(kind="perm", path="", mode="merge"):
    """v1.3.0 ⑱：按种类导入（合并模式会按内容判重，重复导入不会翻倍）"""
    try:
        if not path:
            path = _pick_file(ext=("json", "db"))
            if not path:
                return {"ok": False, "msg": "已取消"}
        mt = _mt()
        mem = _mem_conn()
        r = mt.import_kind(mem, kind=kind, path=path, mode=mode)
        if r.get("ok"):
            _append_log(f"[记忆] {r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def memory_kinds():
    """给前端画「分开的导入导出」用：每种记忆的名字、目录、已有几个备份"""
    try:
        mt = _mt()
        out = []
        for k, v in (mt.KINDS or {}).items():
            files = mt.list_kind_exports(k)
            out.append({"key": k, "name": v[0],
                        "dir": mt.kind_dir(k),
                        "files": len(files),
                        "latest": (files[0]["name"] if files else "")})
        return {"ok": True, "kinds": out}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "kinds": []}


def pick_memory_bundle():
    try:
        # v1.3.0 ⑱：JSON 备份和整库 .db 都能选
        path = _pick_file(ext=("json", "db"))
        if not path:
            return {"ok": False, "msg": "已取消"}
        return {"ok": True, "path": path}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def preview_memory_bundle(path):
    try:
        if str(path).lower().endswith(".db"):
            return _mt().preview_db(path)
        return _mt().preview_bundle(path)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def do_memory_import(path="", mode="merge"):
    try:
        if not path:
            path = _pick_file()
            if not path:
                return {"ok": False, "msg": "已取消"}
        mem = _mem_conn()
        # v1.3.0 ⑱：.db 走整库读取 + 同一套合并逻辑
        if str(path).lower().endswith(".db"):
            r = _mt().import_from_db(mem, path, mode=mode)
        else:
            r = _mt().import_from_file(mem, path, mode=mode)
        if not r.get("ok"):
            # v1.1.4 2.2：认不出来的格式 → 让模型把它翻译成标准结构再导一次
            try:
                r2 = _mt().import_with_llm(
                    mem, path, mode=mode,
                    model_ask=lambda p: ag.ask_model_raw(
                        p, max_tokens=1600, temperature=0.2, tag="/导入解析"))
                if r2.get("ok"):
                    _append_log(f"[记忆] 模型解析导入：{r2.get('msg')}")
                    return r2
                r = {"ok": False, "msg": "%s（模型解析也没成功：%s）"
                                     % (r.get("msg"), r2.get("msg"))}
            except Exception as e2:
                r = {"ok": False, "msg": "%s（模型解析异常：%s）" % (r.get("msg"), e2)}
        if r.get("ok"):
            _append_log(f"[记忆] 导入：{r.get('msg')}（模式 {r.get('mode')}）")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _spark():
    import spark as sp
    return sp


def spark_stats():
    """时光胶囊卡片顶上的统计（多少条回忆、几张形象图、几项习惯）。"""
    try:
        r = _spark().collect_stats()
        try:
            # v1.5.2：把「允许导入旧版明文胶囊」的开关状态一起带回去，界面据此回显
            r["spark_allow_plain"] = bool(_cfg_mod.get("spark_allow_plain", False))
        except Exception:
            r["spark_allow_plain"] = False
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def spark_pick(password=""):
    """选一个 .spark 文件并先读 manifest —— 让用户在覆盖/融合之间做决定前，
    先看到「这是谁、什么时候导的、装了多少东西」。

    v1.9.0：口令加密的胶囊要先给口令才读得出来，所以这里也接 password。
    """
    try:
        path = _pick_file(ext="spark")
        if not path:
            return {"ok": False, "msg": "已取消"}
        password = password or ""
        try:
            need = bool(_spark().needs_password(path))
        except Exception:
            need = False
        if need and not password:
            return {"ok": False, "need_password": True, "path": path,
                    "msg": "这个胶囊是用口令加密的，请先输入导出口令"}
        r = _spark().preview_spark(path, password=password)
        if not r.get("ok"):
            msg = r.get("msg") or "胶囊读不出来"
            return {"ok": False, "msg": msg, "need_password": ("口令" in msg)}
        r["path"] = path
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def spark_needs_password(path=""):
    """胶囊要不要口令（前端据此决定先弹口令框）。"""
    try:
        import spark as _sp
        return {"ok": True, "need": bool(_sp.needs_password(path))}
    except Exception as e:
        return {"ok": False, "need": False, "msg": str(e)[:120]}


def export_spark(path="", password=""):
    """生成时光胶囊：先弹保存框（除非调用方给了 path），再丢后台打包。"""
    try:
        if not path:
            default = "时光胶囊_" + time.strftime("%Y%m%d_%H%M%S") + ".spark"
            st, joined = _pick_file_ex(save=True, default_name=default, ext="spark")
            if st == "timeout":
                return {"ok": False, "msg": "文件对话框没响应（15 秒超时），请重试或让界面传 path 参数"}
            if st != "ok" or not joined:
                return {"ok": False, "msg": "已取消"}
            path = joined
        r = _spark().start_export(path=path, password=password)
        if r.get("ok"):
            _append_log(f"[时光胶囊] 开始打包 → {path}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def import_spark(path="", mode="merge", password=""):
    """唤醒胶囊。mode=replace 直接搬家（先备份现库）/ merge 唤醒融合。"""
    try:
        if not path:
            path = _pick_file(ext="spark")
            if not path:
                return {"ok": False, "msg": "已取消"}
        man = _spark().preview_spark(path, password=password)
        if not man.get("ok"):
            msg = man.get("msg") or "胶囊读不出来"
            # v1.9.0：口令没给 / 给错了，要把「需要口令」这件事说清楚，
            # 而不是丢一句「校验失败」让用户以为文件坏了。
            if "口令" in msg:
                return {"ok": False, "need_password": True, "msg": msg}
            return {"ok": False, "msg": msg}
        r = _spark().start_import(path=path, mode=mode, password=password)
        if r.get("ok"):
            _append_log(f"[时光胶囊] 开始唤醒（{mode}）← {path}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def spark_job():
    """前端每 400ms 轮询：进度、当前在做什么、上次结果。"""
    try:
        return _spark().job_status()
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def do_memory_import_dir():
    """把 memory_import/ 里丢进去的备份文件全部合并导入（「放进去就自动导入」的手动版）"""
    try:
        mt = _mt()
        mem = _mem_conn()
        r = mt.scan_import_dir(mem=mem, log=_append_log)
        if r.get("ok_count"):
            _append_log(f"[记忆] 从导入目录合并了 {r.get('ok_count')} 个文件")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_memory_import_dir():
    """打开「丢进去就自动导入」的目录（memory_import/）"""
    try:
        indir = _mt().import_dir()
        os.makedirs(indir, exist_ok=True)
    except Exception:
        indir = DATA_DIR
    return _open_result(indir, "已打开导入目录")


def save_memory_export_schedule(every=1, unit="day", enabled=None):
    try:
        import config as cfg_mod
        every = int(float(every or 1))
        unit = str(unit or "day")
        secs, desc = cfg_mod.export_interval_seconds(every, unit)
        ok1, m1, v1 = cfg_mod.set_checked("memory_export_every", every)
        ok2, m2, v2 = cfg_mod.set_checked("memory_export_unit", unit)
        ok3 = True
        if enabled is not None:
            ok3, m3, v3 = cfg_mod.set_checked("memory_export_enabled", bool(enabled))
        # 单位改了但间隔没变时也要重新算「下一次」，否则上次的时间戳会让新周期立刻触发
        mt = _mt()
        st = mt._read_state()
        st["last_ts"] = 0
        mt._write_state(st)
        tail = "" if enabled is None else ("，自动导出已开启" if enabled else "，自动导出已关闭")
        return {"ok": ok1 and ok2 and ok3,
                "msg": f"已设为{desc}（{secs} 秒）{tail}",
                "seconds": secs, "desc": desc, "enabled": enabled}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def get_export_units():
    try:
        import config as cfg_mod
        return {"ok": True,
                "units": [{"key": u, "cn": cfg_mod.EXPORT_UNIT_CN.get(u, u)}
                          for u in cfg_mod.EXPORT_UNITS],
                "max_days": cfg_mod.EXPORT_MAX_DAYS}
    except Exception as e:
        return {"ok": False, "msg": str(e), "units": []}


# ==================== v1.1.1：日记自动备份 / 记忆体检 ====================

def get_diary_backup():
    try:
        import config as cfg_mod
        mt = _mt()
        st = mt.diary_backup_status()
        st["units"] = [{"value": u, "label": cfg_mod.EXPORT_UNIT_CN.get(u, u)}
                       for u in cfg_mod.EXPORT_UNITS]
        st["items"] = mt.list_diary_backups(limit=40)
        return {"ok": True, **st}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": [], "units": []}


def export_diary_backup():
    try:
        r = _mt().export_diary(mem=_mem_conn())
        if r.get("ok"):
            _append_log(f"[日记备份] {r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def pick_diary_dir():
    try:
        d = _pick_dir()
        if not d:
            return {"ok": False, "msg": "已取消"}
        ok, msg, _v = _cfg_mod.set_checked("diary_export_dir", d)
        return {"ok": bool(ok), "msg": f"备份目录：{d}" if ok else msg, "dir": d}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def pick_dir(title="选择目录"):
    """让用户选一个目录，只返回路径、不写配置（v1.2.0 向导用）。

    Tauri 端优先用自己的对话框；这个接口是给「没有 Tauri（浏览器调试、
    老网页版）」的场景兜底 —— 走 tkinter，弹在 Python 进程上。
    """
    try:
        d = _pick_dir(title or "选择目录")
        if not d:
            return {"ok": False, "msg": "已取消", "dir": ""}
        return {"ok": True, "msg": d, "dir": d}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "dir": ""}


def open_diary_backup_dir():
    try:
        return _open_result(_mt().diary_dir(), "日记备份目录")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def import_diary_backup():
    try:
        r = _mt().import_diary_from_dir(mem=_mem_conn(), log=_append_log)
        if r.get("ok"):
            _append_log(f"[日记备份] {r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def save_diary_export_schedule(every=1, unit="day", enabled=None, folder=""):
    try:
        import config as cfg_mod
        every = int(float(every or 1))
        unit = str(unit or "day")
        secs, desc = cfg_mod.export_interval_seconds(every, unit)
        ok1, m1, _v = cfg_mod.set_checked("diary_export_every", every)
        ok2, m2, _v = cfg_mod.set_checked("diary_export_unit", unit)
        ok3 = True
        if enabled is not None:
            ok3, m3, _v = cfg_mod.set_checked("diary_export_enabled", bool(enabled))
        if folder:
            cfg_mod.set_checked("diary_export_dir", str(folder))
        try:
            mt = _mt()
            st = mt._read_state()
            st["diary_last_ts"] = 0     # 改了间隔就重新计时，别立刻触发
            mt._write_state(st)
        except Exception:
            pass
        tail = "" if enabled is None else ("，自动备份已开启" if enabled else "，自动备份已关闭")
        return {"ok": ok1 and ok2 and ok3,
                "msg": f"日记备份：每 {every} {cfg_mod.EXPORT_UNIT_CN.get(unit, unit)}"
                       f"（{secs} 秒，最多保留 {cfg_mod.EXPORT_MAX_DAYS} 天）{tail}",
                "seconds": secs, "desc": desc, "enabled": enabled}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def get_memory_health():
    """记忆页「运行记忆 / 体检」：条数、库大小、近似重复条目（按 norm_key）。"""
    try:
        import os as _os
        mem = _mem_conn()
        st = {}
        try:
            st = mem.stats()
        except Exception:
            st = {}
        size = 0
        try:
            db = getattr(mem, "db_path", "")
            if db and _os.path.exists(db):
                size = _os.path.getsize(db)
        except Exception:
            size = 0
        dups = []
        try:
            c = mem.conn.cursor()
            c.execute("""SELECT norm_key, COUNT(*) AS n, GROUP_CONCAT(id), MAX(content)
                         FROM perm_memory GROUP BY norm_key HAVING n > 1
                         ORDER BY n DESC LIMIT 20""")
            for nk, n, ids, content in c.fetchall():
                dups.append({"norm_key": nk, "count": int(n or 0),
                             "ids": str(ids or ""), "content": str(content or "")[:80]})
            c.execute("SELECT COUNT(*) FROM perm_memory")
            perm_total = int((c.fetchone() or [0])[0] or 0)
        except Exception:
            perm_total = int(st.get("perm_memory") or 0)
        return {"ok": True, "stats": st, "db_bytes": size, "perm_total": perm_total,
                "dup_count": len(dups), "dups": dups}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "dups": []}


def backup_memory_now():
    """右上「备份」按钮：直接导一份 JSON 到备份目录。"""
    try:
        r = _mt().export_to_file(mem=_mem_conn())
        if r.get("ok"):
            _append_log(f"[记忆] {r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v1.1.1：学习模块 ====================

def learn_state():
    try:
        import learn
        import config as cfg_mod
        return {"ok": True,
                "items": learn.list_items(limit=300),
                "condition": learn.get_condition(),
                "mode": str(cfg_mod.get("learn_mode") or "auto"),
                "enabled": bool(cfg_mod.get("learn_enabled", True)),
                "auto": bool(cfg_mod.get("learn_auto", True)),
                "hour": int(_ndef(cfg_mod.get("learn_hour"), 22)),
                "stats": learn.stats()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def learn_chart():
    """把学习情况画成一张 PNG（用 Pillow 直接画，不占模型 token）。

    v1.1.2 需求原话是「学习模块可视化……用 Python 画，省 token」——所以这里
    既不让模型写图表代码，也不调前端图表库，后端画好、前端显示。
    返回 {ok, path, data(带前缀的 data URL), stats}
    """
    try:
        import io as _io
        import base64
        import learn
        from PIL import Image, ImageDraw

        st = learn.stats() or {}
        by = st.get("by_source") or {}
        cn = st.get("source_cn") or {}
        order = ["auto", "manual", "draw", "preset"]
        vals = [int(by.get(k, 0)) for k in order]
        labels = [f"{k} {cn.get(k, k)}" for k in order]
        vmax = max(vals + [1])

        W, H = 560, 260
        img = Image.new("RGB", (W, H), (15, 23, 42))
        d = ImageDraw.Draw(img)
        d.text((16, 12), "learn items by source (v1.1.2)", fill=(148, 163, 184))
        d.text((16, 32), f"total {st.get('total', 0)}  enabled {st.get('enabled', 0)}  used {st.get('used', 0)}",
               fill=(226, 232, 240))
        left, bottom, top = 16, H - 44, 60
        bar_w = 96
        gap = 26
        for i, (lab, v) in enumerate(zip(labels, vals)):
            x0 = left + i * (bar_w + gap)
            h = int((bottom - top) * (v / float(vmax))) if vmax else 0
            y0 = bottom - h
            col = (56, 189, 248) if i % 2 == 0 else (52, 211, 153)
            d.rectangle([x0, y0, x0 + bar_w, bottom], fill=col)
            d.text((x0 + 4, y0 - 14 if y0 > top + 14 else y0 + 4), str(v), fill=(226, 232, 240))
            d.text((x0 + 2, bottom + 8), lab[:16], fill=(148, 163, 184))
        d.line([left, bottom, W - 16, bottom], fill=(71, 85, 105))

        p = os.path.join(DATA_DIR, "learn_chart.png")
        os.makedirs(DATA_DIR, exist_ok=True)
        img.save(p, "PNG")
        buf = _io.BytesIO()
        img.save(buf, "PNG")
        data = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        return {"ok": True, "path": p, "data": data, "stats": st}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def predict_emotion(text="", deep=False):
    """v1.1.2 #15：情感预测栏用。

    先本地词表判（emotion.analyze，不花 token）；deep=True 时再让模型
    结合「人设 + 这句话」判一次，能读出潜台词（比如「今天被领导骂了」→ 低落 + 需要安慰）。
    返回 {ok, mood, mood_cn, score, intensity, tags, hint, advice, persona, deep}
    """
    try:
        import emotion as emo
        txt = str(text or "").strip()
        if not txt:
            return {"ok": False, "msg": "先说一句话，我才好判断"}
        a = emo.analyze(txt) or {}
        mood = str(a.get("mood") or "平静")
        inten = float(a.get("intensity") or 0)
        pct = int(round(inten * 100))
        advice_map = {
            "低落": "先接住情绪，别急着讲道理，也别问「怎么了」三连；顺着她的话往下接一句就够。",
            "生气": "先别辩解，先认下她的感受；句子短一点，别加解释性长句。",
            "焦虑": "给具体、可执行的一句安慰，别空话「没事的」；可以问一个很小的问题帮她落地。",
            "开心": "跟着她的劲高兴就行，可以接梗、可以夸张一点，别泼冷水。",
            "兴奋": "语气跟上去，短句连发也行；别用「冷静一下」这类话。",
            "平静": "正常聊，别硬煽情，也别突然深沉。",
        }
        advice = advice_map.get(mood, "正常接话就行，别过度解读。")
        if pct >= 70:
            advice += "（情绪比较重，回复尽量短、先接情绪。）"
        persona = ""
        try:
            import personas_store as _ps
            import config as _cfg
            pkey = str(_cfg.get("active_persona") or "")
            card = _ps.get_card(pkey) if pkey else None
            if card:
                persona = str(card.get("name") or card.get("nickname") or pkey)
        except Exception:
            persona = ""
        out = {"ok": True, "mood": mood, "mood_cn": mood, "score": a.get("score"),
               "intensity": inten, "intensity_pct": pct, "tags": a.get("tags") or [],
               "hint": a.get("hint") or "", "advice": advice, "persona": persona,
               "deep": False}
        if not deep:
            return out
        # 深度判：让模型结合人设读潜台词（会花一点 token，用户自己点才走）
        try:
            import agent as _ag
            import json as _json
            import re as _re
            _sys = str(getattr(_ag, "SYSTEM_PROMPT", "") or "")[:700]
            _prompt = (
                "你是情感分析器。结合下面的人设，判断对方这句话的真实情绪与潜台词。\n"
                f"【人设节选】{_sys}\n"
                f"【对方的话】{txt}\n"
                '只输出一行 JSON，不要解释、不要代码块：\n'
                '{"mood":"开心|平静|低落|生气|焦虑|兴奋","intensity":0.0-1.0,'
                '"hidden":"一句话潜台词","advice":"一句话回复建议"}'
            )
            raw = _ag.ask_model_raw(_prompt, max_tokens=260, temperature=0.2) or ""
            m = _re.search(r"\{[\s\S]*\}", raw)
            data = _json.loads(m.group(0)) if m else {}
            if isinstance(data, dict) and data.get("mood"):
                out["mood"] = str(data.get("mood"))[:6]
                out["mood_cn"] = out["mood"]
                try:
                    out["intensity"] = max(0.0, min(1.0, float(data.get("intensity") or inten)))
                except (TypeError, ValueError):
                    pass
                out["intensity_pct"] = int(round(float(out["intensity"]) * 100))
                if data.get("hidden"):
                    out["hidden"] = str(data.get("hidden"))[:80]
                if data.get("advice"):
                    out["advice"] = str(data.get("advice"))[:160]
                out["deep"] = True
                out["source"] = "model"
        except Exception as _e:
            out["deep"] = False
            out["msg"] = f"模型深判没成功（用本地结果）：{type(_e).__name__}"
        return out
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def learn_save(item_id=0, title="", content="", trigger="", tags="", enabled=True, ltype=""):
    try:
        import learn
        if int(item_id or 0) > 0:
            ok, msg = learn.update(int(item_id), title=title, content=content,
                                   trigger=trigger, tags=tags, enabled=bool(enabled),
                                   ltype=ltype or None)
        else:
            ok, msg, _i = learn.add(source="manual", title=title, content=content,
                                    trigger=trigger, tags=tags, enabled=bool(enabled),
                                    ltype=ltype or None)
        if ok:
            _append_log(f"[学习] {msg}")
            try:
                import agent
                agent.refresh_system_prompt()
            except Exception:
                pass
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def learn_delete(item_id):
    try:
        import learn
        ok, msg = learn.delete(item_id)
        if ok:
            try:
                import agent
                agent.refresh_system_prompt()
            except Exception:
                pass
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def learn_apply(ids, enabled=True):
    try:
        import learn
        ok, msg = learn.set_enabled(ids, enabled)
        if ok:
            try:
                import agent
                agent.refresh_system_prompt()
            except Exception:
                pass
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def learn_save_condition(text=""):
    try:
        import learn
        ok, msg = learn.save_condition(text)
        try:
            import agent
            agent.refresh_system_prompt()
        except Exception:
            pass
        return {"ok": bool(ok), "msg": "条件已保存（永久生效，应用方案也不会清空）" if ok else msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def learn_set_options(enabled=None, mode=None, auto=None, hour=None):
    try:
        import config as cfg_mod
        out = []
        if enabled is not None:
            ok, msg, _v = cfg_mod.set_checked("learn_enabled", bool(enabled))
            out.append("学习已开启" if enabled else "学习已关闭")
        if mode is not None:
            ok, msg, _v = cfg_mod.set_checked("learn_mode", str(mode))
            out.append(f"学习方式：{'模型自主' if mode == 'auto' else '手动条件'}")
        if auto is not None:
            ok, msg, _v = cfg_mod.set_checked("learn_auto", bool(auto))
            out.append("自动学习已开启" if auto else "自动学习已关闭")
        if hour is not None:
            ok, msg, _v = cfg_mod.set_checked("learn_hour", int(hour))
            out.append(f"每天 {int(hour)}:00 学一次")
        try:
            import agent
            agent.refresh_system_prompt()
        except Exception:
            pass
        return {"ok": True, "msg": "；".join(out) or "没有改动"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def learn_bottom_status():
    """学习安全底线状态（v1.1.4）：开关是否生效 + 协议是否已签。"""
    try:
        import learn as learn_mod
        import config as cfg_mod
        info = learn_mod.agreement_info()
        return {
            "ok": True,
            "enabled": bool(learn_mod.bottom_line_on()),
            "raw": bool(cfg_mod.get("learn_bottom_line", True)),
            "signed": bool(info),
            "signed_at": info.get("signed_at", ""),
            "signature": info.get("signature", ""),
            "path": learn_mod.agreement_path(),
        }
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def learn_bottom_sign(signature=""):
    """签署免责协议（前端等 10 秒后才调）→ 关闭底线。"""
    try:
        import learn as learn_mod
        info = learn_mod.sign_agreement(signature)
        if info.get("_error"):
            return {"ok": False, "msg": info["_error"]}
        return {"ok": True, "msg": "已签署，安全底线已关闭（可随时在设置里撤销）",
                "signed_at": info.get("signed_at", ""),
                "path": learn_mod.agreement_path()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def learn_bottom_revoke():
    """撤销协议 → 删本地文件 + 底线立刻恢复开启。"""
    try:
        import learn as learn_mod
        removed = learn_mod.revoke_agreement()
        return {"ok": True, "msg": "协议已撤销，安全底线已恢复开启" if removed else "协议本来就不在，底线已恢复开启"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def learn_run_now(user_id=""):
    """手动触发一次学习（用当前模型）。"""
    try:
        import learn
        import agent as agent_mod
        import config as cfg_mod
        uid = str(user_id or "").strip()
        if not uid:
            try:
                import diary
                acc = diary.bound_accounts()
                uid = str(acc[0]) if acc else ""
            except Exception:
                uid = ""
        if not uid:
            return {"ok": False, "msg": "先绑定一个账号（设置 → 日记与永久记忆）再学", "added": 0}
        cond = (learn.get_condition() or "").strip()

        def _caller(prompt):
            if cond:
                prompt = prompt + f"\n\n额外条件（下面这些是主人特别要求的方向）：\n{cond}"
            return agent_mod.ask_model_raw(prompt, system=agent_mod.SYSTEM_PROMPT,
                                           max_tokens=500, temperature=0.5)

        r = learn.learn_from_dialog(_mem_conn(), uid, name=uid, model_caller=_caller)
        if r.get("added"):
            try:
                agent_mod.refresh_system_prompt()
            except Exception:
                pass
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "added": 0}


# ==================== v1.1.1：待办事件 ====================

def _parse_when(text):
    """把「2026-10-24 09:00」/「+2h」/「明天 9点」解析成时间戳。失败返回 0。"""
    import time as _t
    import re as _re
    s = str(text or "").strip()
    if not s:
        return 0
    m = _re.match(r"^\+(\d+(?:\.\d+)?)\s*([smhd]|天|小时|分钟|秒)$", s, re.I)
    if m:
        n = float(m.group(1))
        unit = m.group(2).lower()
        mult = {"s": 1, "秒": 1, "m": 60, "分钟": 60, "h": 3600, "小时": 3600,
                "d": 86400, "天": 86400}.get(unit, 3600)
        return _t.time() + n * mult
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y/%m/%d %H:%M", "%m-%d %H:%M"):
        try:
            st = _t.strptime(s, fmt)
            if fmt == "%m-%d %H:%M":
                st = st.replace(year=_t.localtime().tm_year)
            return _t.mktime(st)
        except Exception:
            continue
    m = _re.match(r"^(\d{1,2})[:：](\d{2})$", s)
    if m:
        now = _t.localtime()
        st = _t.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                        int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
        if st < _t.time():
            st += 86400
        return st
    return 0


def todo_state():
    try:
        mem = _mem_conn()
        st = mem.stats()
        return {"ok": True, "items": mem.list_events(limit=100),
                "pending": int(st.get("pending_events") or 0)}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def todo_save(event_id=0, desc="", when="", user_id=""):
    try:
        import time as _t
        mem = _mem_conn()
        desc = str(desc or "").strip()
        if not desc:
            return {"ok": False, "msg": "要写点什么到时候提醒"}
        ts = _parse_when(when)
        if not ts:
            return {"ok": False, "msg": "时间看不懂，可以写 2026-10-24 09:00、+2h、明天写成 09:00"}
        if int(event_id or 0) > 0:
            ok = mem.update_event(int(event_id), event_desc=desc, target_time=ts)
            return {"ok": bool(ok), "msg": "已更新"}
        uid = str(user_id or "").strip() or "self"
        eid = mem.add_event(user_id=uid, event_desc=desc, target_time=ts, dedup=False)
        return {"ok": bool(eid), "msg": "已加上" if eid else "加不上（参数不对）"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def todo_delete(event_id):
    try:
        ok = _mem_conn().delete_event(int(event_id))
        return {"ok": bool(ok), "msg": "已删除" if ok else "没找到"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def todo_done(event_id):
    try:
        ok = _mem_conn().mark_event_done(int(event_id))
        return {"ok": bool(ok), "msg": "已标记完成" if ok else "没找到"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v1.1.1：管理员控制台 ====================

def admin_state():
    try:
        import admin
        return admin.state()
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def admin_verify(text=""):
    try:
        import admin
        ok, msg = admin.verify(text)
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def admin_save(prompt=None, bypass=None):
    try:
        import admin
        r = admin.save(prompt=prompt, bypass=bypass)
        try:
            import agent
            agent.refresh_system_prompt()
        except Exception:
            pass
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def admin_diagnostics():
    try:
        import admin
        import agent
        tail = ""
        try:
            tail = agent.tail_log(30) if hasattr(agent, "tail_log") else ""
        except Exception:
            tail = ""
        return {"ok": True, "text": admin.diagnostics(mem=_mem_conn(), log_tail=tail)}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "text": ""}


def admin_export_diagnostics():
    try:
        import admin
        r = admin.export_diagnostics(mem=_mem_conn())
        if r.get("ok"):
            _append_log(f"[管理] {r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v1.1.0：用量明细导出 / 导入 ====================

_LAST_USAGE_EXPORT = {"path": ""}


def open_usage_report_dir():
    """v1.1.4 4.3：打开最近一次导出所在的目录（没有就打开数据目录）。"""
    p = str(_LAST_USAGE_EXPORT.get("path") or "")
    if p and os.path.exists(p):
        return _open_result(os.path.dirname(p) or p, "用量导出目录")
    try:
        d = cfg_mod.data_dir()
    except Exception:
        d = os.path.dirname(os.path.abspath(__file__))
    return _open_result(d, "数据目录")


def export_usage_report(fmt="csv", path=""):
    try:
        import usage as usage_mod
        # v1.3.2（QA BUG-11）：界面（Tauri）可以直接把保存路径传进来，
        # 后端就不弹原生对话框了 —— 无人值守环境再也不会挂住请求。
        path = str(path or "").strip()
        if not path:
            _st, path = _pick_file_ex(save=True,
                                      default_name="用量统计." + str(fmt or "csv"))
            if _st == "timeout":
                return {"ok": False, "msg": "对话框打不开（可能是无人值守环境），"
                                            "请让界面传 path 参数，或手动指定保存路径"}
            if not path:
                return {"ok": False, "msg": "已取消"}
        r = usage_mod.export_report(_mem_conn(), path, fmt=fmt)
        if r.get("ok"):
            _append_log(f"[用量] {r.get('msg')}")
            _LAST_USAGE_EXPORT["path"] = path
        return {**r, "path": path}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def import_usage_report(path=""):
    try:
        import usage as usage_mod
        path = str(path or "").strip()
        if not path:
            _st, path = _pick_file_ex()
            if _st == "timeout":
                return {"ok": False, "msg": "对话框打不开（可能是无人值守环境），请让界面传 path 参数"}
            if not path:
                return {"ok": False, "msg": "已取消"}
        r = usage_mod.import_report(_mem_conn(), path)
        if r.get("ok"):
            _append_log(f"[用量] {r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v1.1.0：动态（说说）读写 ====================

def _qz():
    import qzone_interact
    return qzone_interact


def qzone_caps(force=False, write_test=False):
    try:
        return _qz().capabilities(force=bool(force), write_test=bool(write_test))
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def qzone_feeds(uin="", num=20):
    try:
        return _qz().list_feeds(uin=uin, num=int(num or 20))
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def qzone_friend_feeds(max_friends=6, num=6):
    try:
        return _qz().read_friend_feeds(max_friends=int(max_friends or 6), num=int(num or 6))
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def qzone_like(tid, uin="", unlike=False):
    try:
        r = _qz().like(tid, uin=uin, unlike=bool(unlike))
        if r.get("ok"):
            _append_log(f"[动态] {'取消赞' if unlike else '点赞'} tid={tid}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def qzone_comment(tid, uin="", text=""):
    try:
        r = _qz().comment(tid, uin=uin, text=text or "")
        if r.get("ok"):
            _append_log(f"[动态] 评论 tid={tid}：{(text or '')[:30]}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def qzone_comments(tid, uin=""):
    try:
        return _qz().comment_list(tid, uin=uin)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


# ==================== v1.1.0：关于 / 免责 / 暗号 / 完整性 ====================

def get_about():
    """关于框：版本、路径、规模、完整性（不暴露任何密钥）"""
    try:
        import config as cfg_mod
        info = {
            "ok": True,
            "name": getattr(cfg_mod, "APP_NAME", "小鲸鱼 Agent 控制台"),
            "version": getattr(cfg_mod, "APP_VERSION", ""),
            "version_tag": getattr(cfg_mod, "APP_VERSION_TAG", ""),
            "subtitle": getattr(cfg_mod, "APP_SUBTITLE", ""),
            "author": getattr(cfg_mod, "APP_AUTHOR", ""),
            "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False)),
            "base_dir": BASE_DIR,
            "data_dir": DATA_DIR,
            "config_path": CONFIG_JSON,
            "pid": os.getpid(),
            "started_text": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(_UI_START_TS)),
        }
        # v1.2.0：界面换成 Tauri，不再有 pywebview
        info["webview"] = ""
        info["ui"] = "tauri"
        try:
            mem = _mem_conn()
            c = {}
            for t in ("messages", "perm_memory", "user_events", "affinity", "diary"):
                try:
                    c[t] = mem.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                except Exception:
                    c[t] = 0
            info["counts"] = c
            info["db_size"] = _file_size(DB_FILE)
        except Exception:
            info["counts"] = {}
        try:
            info["personas"] = len(_store_personas().list_cards())
        except Exception:
            info["personas"] = 0
        info["agent_running"] = _is_running()
        return info
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def get_integrity():
    """文件完整性校验（manifest.sha256 存在才有意义）"""
    try:
        import security
        import config as cfg_mod
        cfg = cfg_mod.load()
        if not cfg.get("integrity_check", True):
            return {"ok": True, "skipped": True, "msg": "完整性校验已在设置里关闭"}
        mf = os.path.join(BASE_DIR, str(cfg.get("integrity_manifest") or "manifest.sha256"))
        names = _manifest_names()
        r = security.verify_manifest(BASE_DIR, mf, names)
        r["manifest"] = mf
        r["msg"] = ("没有生成清单（源码运行属正常）" if r.get("no_manifest")
                    else (f"校验通过：{r.get('checked')} 个文件" if r.get("ok")
                          else f"校验失败：缺 {len(r.get('missing') or [])} 个、被改 {len(r.get('bad') or [])} 个"))
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _manifest_names():
    out = []
    try:
        for fn in os.listdir(BASE_DIR):
            if fn.endswith((".py", ".html")) and not fn.startswith("_"):
                out.append(fn)
    except Exception:
        pass
    return sorted(out)


def accept_disclaimer():
    try:
        import config as cfg_mod
        cfg_mod.set_checked("disclaimer_accepted", True)
        cfg_mod.set_checked("disclaimer_version",
                            getattr(cfg_mod, "APP_VERSION", "1.1.0"))
        _append_log("[UI] 用户已接受免责与使用声明")
        # V1.7.0：除了 config 里的布尔标记，再往审计库落一条不可改的留痕
        try:
            import audit
            audit.accept({
                "agreement": "disclaimer",
                "version": str(getattr(cfg_mod, "APP_VERSION", "1.1.0")),
                "title": "免责与使用声明",
            })
        except Exception as _e:
            _append_log("[审计] 免责协议留痕失败: %s" % _e)
        return {"ok": True, "msg": "已同意"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def get_disclaimer():
    try:
        import config as cfg_mod
        cfg = cfg_mod.load()
        cur = str(getattr(cfg_mod, "APP_VERSION", ""))
        return {
            "ok": True,
            "accepted": bool(cfg.get("disclaimer_accepted", False)),
            "accepted_version": str(cfg.get("disclaimer_version") or ""),
            "version": cur,
            "need_show": (not bool(cfg.get("disclaimer_accepted", False)))
                          or str(cfg.get("disclaimer_version") or "") != cur,
            "text": _DISCLAIMER_TEXT,
        }
    except Exception as e:
        return {"ok": False, "msg": str(e), "need_show": False, "text": ""}


# ==================== V1.7.0：签署审计留痕 ====================
# 注意签名都带 `self=None`：这些函数挂在 Api 类上之后会变成绑定方法，
# 后端 _bind_args 用 inspect.signature(bound) 取参数名，少写 self 会把
# 前端传的 payload 挤成第二个位置参数而报 TypeError（老坑，perf_ack 也踩过）。
def _audit_pick_payload(args, kwargs):
    """从各种调用形态里捞出 payload 字典。"""
    for a in args:
        if isinstance(a, dict):
            return a
    p = kwargs.get("payload")
    if isinstance(p, dict):
        return p
    return {}


def audit_sign_accept(self=None, payload=None, *args, **kwargs):
    """强模态签署点「同意并继续」后落库（协议、版本、时间、用户、设备指纹）。"""
    try:
        data = payload if isinstance(payload, dict) else None
        if data is None:
            data = _audit_pick_payload(args, kwargs)
        import audit
        r = audit.accept(data or {})
        if r.get("ok"):
            try:
                _append_log("[审计] 已记录签署：%s v%s（记录 #%s）"
                            % (r.get("agreement"), r.get("version"), r.get("id")))
            except Exception:
                pass
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def audit_sign_state(self=None, agreement="", *args, **kwargs):
    try:
        import audit
        return audit.state(agreement or "")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "signed": False}


def audit_sign_list(self=None, limit=50, *args, **kwargs):
    try:
        import audit
        return audit.recent(limit)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "rows": []}


def audit_sign_verify(self=None, *args, **kwargs):
    try:
        import audit
        return audit.verify()
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def audit_device_info(self=None, *args, **kwargs):
    try:
        import audit
        return {"ok": True, **audit.device_info()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


_DISCLAIMER_TEXT = """使用前请读完（就这几条）

1. 本软件是一个本地运行的 QQ 聊天机器人，会读取你配置的 QQ 号收到的消息，
   并把聊天内容存到本机数据库（memory/memory.db）里，用于生成回复和记忆。
   所有数据都在你自己的电脑上，不会上传到任何服务器（除非你在设置里
   自己填了云端 API）。

2. 主动发言、自动点赞、自动评论等功能会以你的 QQ 身份对外发出内容。
   请自己确认这些行为符合你所在群/好友的期待，因为这属于「你的账号在说话」。

3. 模型生成的内容可能出错、跑题或冒犯人。本软件不做任何内容担保，
   出了问题（包括但不限于社交尴尬、被封号、数据丢失）由使用者自行承担。

4. 请勿把本软件用于骚扰、刷屏、诈骗、批量养号等用途。

5. 密码、API Key 存在本机 config.json 里，默认用系统级加密（DPAPI）保护，
   但请仍然保管好你的电脑本身——能拿到你电脑的人就能拿到这些。

点「我同意」表示你读完了上面的话，并同意自己承担使用后果。
"""


def set_codeword(token, scope="all"):
    """设置管理员暗号。token 为空 = 关闭；只存加密后的密文。

    v1.0.0-Beta 安全整改（QA BUG-06）：
      1) 响应里**绝不回显明文**（以前 `masked` 字段把整串明文带回，只加了 8 个星号前缀）；
      2) 最短 8 位（暗号一旦泄露 = 注入防护全废，太短的没意义）；
      3) 拒绝纯数字 / 纯生日这类可穷举的暗号。
    """
    try:
        import config as cfg_mod
        import security
        token = str(token or "").strip()
        scope = str(scope or "all")
        if scope not in getattr(cfg_mod, "CODEWORD_SCOPES", ("all",)):
            scope = "all"
        if not token:
            cfg_mod.set_checked("codeword_enabled", False)
            cfg_mod.set_value("codeword_token", "")
            return {"ok": True, "msg": "已关闭管理员暗号"}
        if len(token) < 8:
            return {"ok": False, "msg": "暗号至少 8 个字（太短容易被猜）"}
        if len(token) > 64:
            return {"ok": False, "msg": "暗号太长了（最多 64 字）"}
        if token.isdigit():
            return {"ok": False, "msg": "暗号不能是纯数字（生日/手机号这种几秒就能试出来）"}
        if re.fullmatch(r"(19|20)\d{2}[-/.]?\d{2}[-/.]?\d{2}", token):
            return {"ok": False, "msg": "暗号不能是纯日期"}
        enc = security.encrypt_secret(token)
        cfg_mod.set_value("codeword_token", enc)
        cfg_mod.set_value("codeword_scope", scope)
        cfg_mod.set_checked("codeword_enabled", True)
        # 换了暗号就把爆破计数清零（否则用户改完还得先等冷却）
        try:
            cfg_mod.set_value("admin_fail_count", 0)
            cfg_mod.set_value("admin_fail_last", 0.0)
            cfg_mod.set_value("admin_lock_until", 0.0)
        except Exception:
            pass
        try:
            import agent as agent_mod
            agent_mod.refresh_codeword()
        except Exception:
            pass
        _append_log(f"[安全] 管理员暗号已设置（范围 {scope}，长度 {len(token)}，"
                    f"密文后端 {security.crypto_backend()}）")
        return {"ok": True, "msg": "暗号已保存（已加密，界面不会再显示它）",
                "backend": security.crypto_backend()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def clear_codeword():
    try:
        import config as cfg_mod
        cfg_mod.set_checked("codeword_enabled", False)
        cfg_mod.set_value("codeword_token", "")
        try:
            import agent as agent_mod
            agent_mod.refresh_codeword()
        except Exception:
            pass
        return {"ok": True, "msg": "已关闭"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


CODEWORD_ALERT_FILE = os.path.join(DATA_DIR, "codeword_alert.json")


def get_codeword_alert():
    """前端每 2 秒轮询一次：Agent 收到暗号会写这个文件，读到就在界面上弹 10 秒强提醒"""
    try:
        if not os.path.exists(CODEWORD_ALERT_FILE):
            return {"ok": True, "show": False}
        with open(CODEWORD_ALERT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        now = time.time()
        until = float(d.get("until") or 0)
        if until <= now:
            return {"ok": True, "show": False, "expired": True}
        return {"ok": True, "show": True, "ts": float(d.get("ts") or 0),
                "remaining": round(until - now, 1),
                "name": d.get("name") or "", "user_id": d.get("user_id") or "",
                "type": d.get("type") or "", "text": d.get("text") or "收到管理员暗号"}
    except Exception as e:
        return {"ok": False, "show": False, "msg": f"{type(e).__name__}: {e}"}


def test_codeword(text):
    try:
        import agent as agent_mod
        r = agent_mod.check_codeword(str(text or ""))
        return {"ok": True, "matched": bool(r.get("matched")),
                "msg": r.get("msg") or ("暗号正确" if r.get("matched") else "不是暗号")}
    except Exception as e:
        return {"ok": False, "matched": False, "msg": f"{type(e).__name__}: {e}"}


def _file_size(path):
    try:
        n = os.path.getsize(path)
        return f"{n/1048576:.2f}MB" if n > 1048576 else f"{n/1024:.1f}KB"
    except Exception:
        return ""


def _personas_dir():
    """人设卡片目录（v1.1.0 起一人设一文件）"""
    try:
        import personas_store
        return personas_store.dir_path()
    except Exception:
        return os.path.join(DATA_DIR, "personas")


def _mem_export_dir():
    try:
        import mem_transfer
        return mem_transfer.export_dir()
    except Exception:
        return os.path.join(DATA_DIR, "memory_export")


def get_settings():
    """设置 Tab 的全部数据"""
    try:
        import config as cfg_mod
        import theme as theme_mod
        import autostart_mgr
        import diary as diary_mod
        import notice as notice_mod
        import updater as updater_mod

        cfg = cfg_mod.load()
        themes = theme_mod.list_themes()
        cur = theme_mod.current()
        asst = autostart_mgr.status()
        bst = diary_mod.binding_status(_memory())
        glst = diary_mod.group_list()
        ust = updater_mod.status()

        return {
            "ok": True,
            "theme": {
                "current": cur.get("key"),
                "current_name": cur.get("name"),
                "dark": cur.get("dark"),
                "items": themes,
                "dir": theme_mod.THEME_DIR,
                "css": theme_mod.css_vars(),
                "effects": cur.get("effects") or {"snow": 0, "edge": False},
            },
            "autostart": asst,
            "diary": bst,
            "groups": glst,
            "notice": {
                "enabled": bool(cfg.get("notice_enabled", True)),
                "popup": bool(cfg.get("notice_popup", True)),
                "center": bool(cfg.get("notice_center", True)),
                "count": notice_mod.count_unread(),
                "remote_enabled": bool(cfg.get("notice_remote_enabled", False)),
                "remote_url": str(cfg.get("notice_remote_url") or ""),
                # v1.1.2 #23：公告热推送（定时轮询）
                "poll_enabled": bool(cfg.get("notice_poll_enabled", True)),
                "poll_sec": int(_ndef(cfg.get("notice_poll_sec"), 60)),
            },
            "save_group_memory": bool(cfg.get("save_group_memory", False)),
            # v1.3.0：启动后是否自动开始工作（默认关，避免"打开软件就自动用你的号说话"）
            "agent_auto_start": bool(cfg.get("agent_auto_start", False)),
            # v1.3.0：性能模式（低配电脑减负）
            "perf_mode": bool(cfg.get("perf_mode", False)),
            "perf_auto_refresh": bool(cfg.get("perf_auto_refresh", True)),
            "perf_mode_asked": bool(cfg.get("perf_mode_asked", False)),
            "memory": {
                "mode": str(cfg.get("memory_mode") or "auto"),
                "replace_conflict": bool(cfg.get("memory_replace_conflict", True)),
                "auto_export": bool(cfg.get("memory_auto_export", False)),
                "auto_export_days": int(_ndef(cfg.get("memory_auto_export_days"), 7)),
                "export_dir": _backup_dir(),
            },
            "update": {**ust, "enabled": bool(cfg.get("update_enabled", False)),
                       "repo": cfg.get("update_repo") or "",
                       "branch": cfg.get("update_branch") or "main"},
            "care": {
                "enabled": bool(cfg.get("care_plan_enabled", True)),
                "source": str(cfg.get("care_plan_source") or "model"),
                "hours": list(cfg.get("care_plan_hours") or [8, 23]),
                "max_per_day": int(_ndef(cfg.get("care_plan_max_per_day"), 6)),
                "min_gap_min": int(_ndef(cfg.get("care_plan_min_gap_min"), 25)),
                "respect_silence": bool(cfg.get("care_plan_respect_silence", True)),
                # v1.8.10：对方没回就追问（界面在「主动关心计划」卡片里）
                "followup_enabled": bool(cfg.get("followup_enabled", True)),
                "followup_gap_min": int(_ndef(cfg.get("followup_gap_min"), 8)),
                "followup_max": int(_ndef(cfg.get("followup_max"), 3)),
                "followup_window_min": int(_ndef(cfg.get("followup_window_min"), 180)),
            "care_idle_enabled": bool(cfg.get("care_idle_enabled", True)),
            "care_idle_silence_hours": float(_ndef(cfg.get("care_idle_silence_hours"), 2.0)),
            # v1.8.12：静默门槛改成「数量 + 单位」，真值是秒
            "care_idle_silence_sec": int(_ndef(cfg.get("care_idle_silence_sec"), 1800)),
            "care_idle_silence_unit": str(cfg.get("care_idle_silence_unit") or "minute"),
            "care_trigger_by_emotion": bool(cfg.get("care_trigger_by_emotion", True)),
            # v1.8.12：真唱 + 自我形象（设置页要回填这些）
            "sing_enabled": bool(cfg.get("sing_enabled", True)),
            "sing_engine": str(cfg.get("sing_engine", "read") or "read"),
            "sing_cloud_url": str(cfg.get("sing_cloud_url", "") or ""),
            "sing_cloud_format": str(cfg.get("sing_cloud_format", "suno") or "suno"),
            "sing_style": str(cfg.get("sing_style", "") or ""),
            "sing_svc_url": str(cfg.get("sing_svc_url", "") or ""),
            "sing_max_lines": int(_ndef(cfg.get("sing_max_lines"), 12)),
            "sing_polyphone": str(cfg.get("sing_polyphone", "homophone") or "homophone"),
            "self_appearance": str(cfg.get("self_appearance", "") or ""),
            # v1.9.15：外貌特征（自画像/换形象时会拼进生图提示词）
            "appearance_desc": str(cfg.get("appearance_desc", "") or ""),
            "sticker_steal_enabled": bool(cfg.get("sticker_steal_enabled", True)),
            "avatar_auto_change": bool(cfg.get("avatar_auto_change", False)),
                "pending": (bst.get("pending_plans") if isinstance(bst, dict) else 0) or 0,
            },
            "imagegen": {
                "enabled": bool(cfg.get("imagegen_enabled", False)),
                "sticker_gen_enabled": bool(cfg.get("sticker_gen_enabled", False)),
                "sticker_gen_chance": float(cfg.get("sticker_gen_chance") or 0),
            },
            "icon": _icon_info(),
            # v8
            "checkin": {
                "mode": str(cfg.get("checkin_silence_mode") or "fixed"),
                "hours": float(cfg.get("checkin_silence_hours") or 0),
                "poll_sec": int(_ndef(cfg.get("checkin_poll_sec"), 30)),
                "care_plan_use_silence": bool(cfg.get("care_plan_use_silence", False)),
                # v1.1.1：主动消息的额外要求（拼进问候/查岗/冒泡的提示词）
                "prompt": str(cfg.get("proactive_prompt") or ""),
            },
            "affinity": {
                "enabled": bool(cfg.get("affinity_enabled", False)),
                "gain_msg": int(_ndef(cfg.get("affinity_gain_per_msg"), 1)),
                "gain_positive": int(_ndef(cfg.get("affinity_gain_positive"), 3)),
                "decay": int(_ndef(cfg.get("affinity_decay_per_day"), 1)),
                "max": int(cfg.get("affinity_max") or 1000),
                "show_score": bool(cfg.get("affinity_show_score", True)),
            },
            "ui": {
                "panel_alpha": int(_ndef(cfg.get("ui_panel_alpha"), 86)),
                "notice_stack": bool(cfg.get("notice_stack", True)),
                # v1.1.2 #27：字体与字号
                "font_scale": int(cfg.get("ui_font_scale") or 100),
                "font_family": str(cfg.get("ui_font_family") or ""),
                "accent": str(cfg.get("ui_accent") or ""),
                # v1.1.2 #25：排版（卡片顺序 + 预设）
                "card_order": cfg.get("ui_card_order") or {},
                "layout_presets": cfg.get("ui_layout_presets") or {},
                "card_drag": bool(cfg.get("ui_card_drag", False)),
            "lock_layout": bool(cfg.get("lock_layout", False)),
                # v1.1.2：文字能不能被选中 / 卡片能不能拖大小
                "text_select": bool(cfg.get("ui_text_select", False)),
                "card_resize": bool(cfg.get("ui_card_resize", False)),
                # v1.8.4：自定义鼠标指针（纯 CSS cursor + PNG，不走 JS 跟随）
                "custom_cursor": bool(cfg.get("custom_cursor_enabled", True)),
            },
            "moment_quota": {
                "min": int(_ndef(cfg.get("moment_min_per_day"), 1)),
                "max": int(_ndef(cfg.get("moment_max_per_day"), 10)),
                "hard": int(getattr(cfg_mod, "MOMENT_MAX_HARD", 50)),
                "warn": int(getattr(cfg_mod, "MOMENT_MAX_WARN", 30)),
            },
            "diary_hour": int(_ndef(cfg.get("diary_hour"), 23)),
            # v1.9.10：记忆快照间隔 + 视频相关开关
            "diary_interval_value": float(_ndef(cfg.get("diary_interval_value"), 4)),
            "diary_interval_unit": str(cfg.get("diary_interval_unit") or "hour"),
            "diary_trigger_min_msgs": int(_ndef(cfg.get("diary_trigger_min_msgs"), 10)),
            "video_parse_enabled": bool(cfg.get("video_parse_enabled", True)),
            "agent_share_video_enabled": bool(cfg.get("agent_share_video_enabled", True)),
            "merge_enabled": bool(cfg.get("merge_enabled", True)),
        "chat_tools_enabled": bool(cfg.get("chat_tools_enabled", True)),
            "merge_delay_sec": float(cfg.get("merge_delay_sec", 4.5) or 4.5),
            "share_video_gap_min": int(_ndef(cfg.get("share_video_gap_min"), 90)),
            # v1.1.0
            "effects": {
                "enabled": bool(cfg.get("effects_enabled", True)),
                "speed": float(_ndef(cfg.get("effects_speed"), 1.0)),
                "glow": int(cfg.get("effects_glow") if cfg.get("effects_glow") is not None else 55),
                "hover": bool(cfg.get("effects_hover", True)),
                "page": bool(cfg.get("effects_page", True)),
                "ripple": bool(cfg.get("effects_ripple", True)),
                "snow_depth": int(cfg.get("snow_depth") if cfg.get("snow_depth") is not None else 55),
                # v1.1.5：卡片积雪 / 飘雪分开控制
                "card_snow": bool(cfg.get("card_snow_enabled", True)),
        "bg_effect": str(cfg.get("bg_effect") or ""),
        "card_effect": str(cfg.get("card_effect") or "snow"),
                "flakes": bool(cfg.get("snow_flakes_enabled", True)),
            },
            # v1.1.1：窗口外观与托盘
            "window": {
                "frameless": bool(cfg.get("window_frameless", True)),
                "tray": bool(cfg.get("tray_enabled", True)),
                "icon": bool(_icon_path("whale.png")),
            },
            "guard": {
                "enabled": bool(cfg.get("guard_enabled", True)),
                "sql": bool(cfg.get("guard_sql", True)),
                "prompt": bool(cfg.get("guard_prompt", True)),
                "command": bool(cfg.get("guard_command", True)),
                "block": bool(cfg.get("guard_block", True)),
            },
            "codeword": {
                "enabled": bool(cfg.get("codeword_enabled", False)),
                "scope": str(cfg.get("codeword_scope") or "all"),
                "has_token": bool(cfg.get("codeword_token") or ""),
                "scopes": list(getattr(cfg_mod, "CODEWORD_SCOPES", ())),
            },
            "appinfo": {
                "name": getattr(cfg_mod, "APP_NAME", ""),
                "version": getattr(cfg_mod, "APP_VERSION", ""),
                "version_tag": getattr(cfg_mod, "APP_VERSION_TAG", ""),
                "subtitle": getattr(cfg_mod, "APP_SUBTITLE", ""),
                "author": getattr(cfg_mod, "APP_AUTHOR", ""),
                "disclaimer_accepted": bool(cfg.get("disclaimer_accepted", False)),
                "disclaimer_version": str(cfg.get("disclaimer_version") or ""),
                # v1.1.0：程序目录从左上角挪到这里
                "base_dir": BASE_DIR,
                "data_dir": DATA_DIR,
                "db_file": DB_FILE,
            },
            "memory_export": {
                "every": int(_ndef(cfg.get("memory_export_every"), 1)),
                "unit": str(cfg.get("memory_export_unit") or "day"),
                "units": list(getattr(cfg_mod, "EXPORT_UNITS", ())),
                "max_days": int(getattr(cfg_mod, "EXPORT_MAX_DAYS", 365)),
                "dir": _mem_export_dir(),
            },
            "personas_dir": _personas_dir(),
            "config_path": CONFIG_JSON,
        }
    except Exception as e:
        return {"ok": False, "msg": f"读取设置失败: {type(e).__name__}: {e}",
                "trace": traceback.format_exc()[-800:]}


def _memory():
    """打开一个 Memory 实例（每个 API 调用独立连接，避免线程问题）"""
    from memory import Memory
    return Memory(DB_FILE)


# ---------- 主题 ----------
def list_themes():
    try:
        import theme as theme_mod
        cur = theme_mod.current()
        return {"ok": True, "items": theme_mod.list_themes(),
                "current": cur.get("key"), "current_name": cur.get("name"),
                "dir": theme_mod.THEME_DIR, "css": theme_mod.css_vars(),
                "effects": cur.get("effects") or {"snow": 0, "edge": False}}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def apply_theme(key):
    try:
        import theme as theme_mod
        ok, msg = theme_mod.set_theme(key)
        if not ok:
            return {"ok": False, "msg": msg}
        t = theme_mod.current()
        return {"ok": True, "msg": msg, "css": theme_mod.css_vars(),
                "effects": t.get("effects") or {"snow": 0, "edge": False},
                "key": t.get("key"), "name": t.get("name"), "dark": t.get("dark")}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_theme_dir():
    try:
        import theme as theme_mod
        theme_mod.ensure_dir()
    except Exception:
        pass
    return _open_result(theme_mod.THEME_DIR, "已打开主题目录")


# ---------- 开机自启动 ----------
def get_autostart():
    try:
        import autostart_mgr
        return {"ok": True, **autostart_mgr.status()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def set_autostart(enabled):
    try:
        import autostart_mgr
        import config as cfg_mod
        ok, msg = autostart_mgr.apply(bool(enabled))
        if ok:
            cfg_mod.set_value("autostart", bool(enabled))
        _append_log(f"[UI] 开机自启动 → {'开' if enabled else '关'}：{msg}")
        return {"ok": ok, "msg": msg, **autostart_mgr.status()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- 日记 / 绑定 ----------
def get_diary():
    try:
        import diary as diary_mod
        m = _memory()
        try:
            st = diary_mod.binding_status(m)
            items = m.list_diary(limit=60)
            for d in items:
                d["created_text"] = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(d.get("created_at") or 0))
        finally:
            m.close()
        return {"ok": True, **st, "diaries": items}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def bind_account(user_id, note=""):
    try:
        import diary as diary_mod
        import config as cfg_mod
        ok, msg = diary_mod.bind(str(user_id).strip(), note)
        if ok:
            cfg_mod.set_value("diary_bound_accounts", diary_mod.bound_accounts())
        m = _memory()
        try:
            st = diary_mod.binding_status(m)
        finally:
            m.close()
        st.pop("ok", None)      # 别让子状态里的 ok 覆盖本次操作结果
        st.update({"ok": bool(ok), "msg": msg})
        return st
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def unbind_account(user_id, keep_data=True):
    try:
        import diary as diary_mod
        import config as cfg_mod
        ok, msg = diary_mod.unbind(str(user_id).strip(), keep_data=bool(keep_data))
        if ok:
            cfg_mod.set_value("diary_bound_accounts", diary_mod.bound_accounts())
        m = _memory()
        try:
            st = diary_mod.binding_status(m)
        finally:
            m.close()
        st.pop("ok", None)
        st.update({"ok": bool(ok), "msg": msg})
        return st
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def write_diary_now(user_id=""):
    """手动写日记（测试/补写）"""
    try:
        import diary as diary_mod
        m = _memory()
        try:
            uids = [str(user_id)] if user_id else diary_mod.bound_accounts()
            if not uids:
                return {"ok": False, "msg": "还没有绑定任何账号（先绑定才写日记）"}
            out = []
            for uid in uids:
                # QA P2-2：以前取「最后一条消息的正文前 12 字」当称呼，
                # 于是「今天和<模型说的那句话>聊了 8 条」。messages 表本来就没有昵称列，
                # 正确来源是好感度里的 name（记忆页的账号胶囊用的也是它）。
                name = uid
                try:
                    _aff = m.get_affinity(uid) or {}
                    name = str(_aff.get("name") or "").strip() or uid
                except Exception:
                    name = uid
                res = diary_mod.write_diary_for(m, uid, name, force=True)
                out.append({"user_id": uid, **res})
            _append_log(f"[UI] 手动写日记：{len(out)} 个账号")
            return {"ok": True, "items": out,
                    "msg": f"已处理 {len(out)} 个账号"}
        finally:
            m.close()
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- 群绑定 ----------
def _group_payload(ok=True, msg=""):
    """group_list() 自带 ok=True，展开时会把操作结果盖掉，这里显式收尾"""
    import diary as diary_mod
    g = diary_mod.group_list()
    g.pop("ok", None)
    g.update({"ok": bool(ok), "msg": msg})
    return g


def get_groups():
    try:
        return _group_payload(True, "")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def set_group_mode(mode):
    try:
        import diary as diary_mod
        ok, msg = diary_mod.set_group_mode(mode)
        return _group_payload(ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def add_group(group_id, to_blacklist=False):
    try:
        import diary as diary_mod
        ok, msg = diary_mod.add_group(str(group_id).strip(), bool(to_blacklist))
        return _group_payload(ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def remove_group(group_id):
    try:
        import diary as diary_mod
        ok, msg = diary_mod.remove_group(str(group_id).strip())
        return _group_payload(ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- 记忆总览（v4：临时 + 永久 + 日记）----------
def memory_accounts():
	"""v1.8.4：有哪些账号留下了记忆，给「记忆查看」页的账号胶囊用。

	数据从五张表里凑：永久记忆 / 日记 / 临时对话（private_<QQ>）/ 好感度 / 关系网。
	没有记忆痕迹的账号不出现（用户要求「无记录的不显示」）。
	返回 [{user_id, nickname, perm, diary, msgs, affinity, kg, last}]
	"""
	acc = {}

	def _slot(uid):
		u = str(uid or "").strip()
		if not u:
			return None
		d = acc.get(u)
		if d is None:
			d = {"user_id": u, "nickname": "", "perm": 0, "diary": 0,
			     "msgs": 0, "affinity": 0, "kg": 0, "last": 0.0}
			acc[u] = d
		return d

	try:
		from memory import Memory
		m = Memory(DB_FILE)
	except Exception as e:
		return {"ok": False, "accounts": [], "msg": f"打不开记忆库：{e}"}

	try:
		# 永久记忆
		for p in (m.perm_users() or []):
			d = _slot(p.get("user_id"))
			if d:
				d["perm"] = int(p.get("n") or 0)
				d["last"] = max(d["last"], float(p.get("last") or 0))
		# 日记
		try:
			for r in m.conn.execute(
					"SELECT user_id, COUNT(*), MAX(created_at) FROM diary GROUP BY user_id"):
				d = _slot(r[0])
				if d:
					d["diary"] = int(r[1] or 0)
					d["last"] = max(d["last"], float(r[2] or 0))
		except Exception:
			pass
		# 临时对话（私聊会话名就是 private_<QQ>）
		try:
			for s in (m.session_stats() or []):
				sk = str(s.get("session_key") or "")
				if not sk.startswith("private_"):
					continue
				d = _slot(sk.split("_", 1)[1])
				if d:
					d["msgs"] = int(s.get("n") or 0)
					d["last"] = max(d["last"], float(s.get("last") or 0))
		except Exception:
			pass
		# 好感度（顺便取昵称）
		try:
			for a in (m.list_affinity(limit=200) or []):
				d = _slot(a.get("user_id"))
				if d:
					d["affinity"] = int(a.get("score") or 0)
					if not d["nickname"]:
						d["nickname"] = str(a.get("name") or "")
					d["last"] = max(d["last"], float(a.get("updated_at") or 0))
		except Exception:
			pass
		# 关系网
		try:
			for r in m.conn.execute(
					"SELECT user_id, COUNT(*) FROM kg_entities GROUP BY user_id"):
				d = _slot(r[0])
				if d:
					d["kg"] = int(r[1] or 0)
		except Exception:
			pass
	finally:
		try:
			m.close()
		except Exception:
			pass

	out = sorted(acc.values(), key=lambda x: (-x["last"], x["user_id"]))
	# 绑定了但一条记忆都没有的账号也列出来（不然用户不知道该怎么切过去）
	try:
		import diary as _dm
		have = {x["user_id"] for x in out}
		for uid in (_dm.bound_accounts() or []):
			if str(uid) not in have:
				out.append({"user_id": str(uid), "nickname": "", "perm": 0,
				            "diary": 0, "msgs": 0, "affinity": 0, "kg": 0,
				            "last": 0.0, "empty": True})
	except Exception:
		pass
	total = {"user_id": "", "nickname": "全部账号",
	         "perm": sum(x["perm"] for x in out),
	         "diary": sum(x["diary"] for x in out),
	         "msgs": sum(x["msgs"] for x in out), "affinity": 0,
	         "kg": sum(x["kg"] for x in out), "last": 0.0}
	return {"ok": True, "accounts": out, "total": total}



def get_memory_overview(user_id=""):
    try:
        import diary as diary_mod
        import memory as mem_mod
        m = _memory()
        try:
            ov = diary_mod.memory_overview(m)
            # v7：把"临时记忆"做成可按会话挑的列表，界面才能勾选转永久
            try:
                sessions = m.session_stats()
            except Exception:
                sessions = []
            for s in sessions:
                sk = s.get("session_key") or ""
                s["label"] = sk
                s["time_text"] = time.strftime(
                    "%m-%d %H:%M", time.localtime(float(s.get("last") or 0)))
                peers = getattr(mem_mod, "_peer_names", {})
                if sk.startswith("private_"):
                    uid = sk.split("_", 1)[1]
                    s["kind"] = "private"
                    s["uid"] = uid
                    s["label"] = f"私聊 {uid}"
                elif sk.startswith("group_"):
                    gid = sk.split("_", 1)[1]
                    s["kind"] = "group"
                    s["uid"] = gid
                    s["label"] = f"群 {gid}"
                else:
                    s["kind"] = "other"
                    s["uid"] = ""
            ov["sessions"] = sessions
            if user_id:
                # v1.8.4：切到某个账号时，临时记忆只留这个人的私聊会话
                want = "private_" + str(user_id)
                ov["sessions"] = [s for s in sessions
                                    if str(s.get("session_key") or "") == want]
            ov["memory_dir"] = mem_mod.memory_dir(DATA_DIR)
            ov["db_path"] = getattr(m, "db_path", DB_FILE)
            ov["perm_total"] = m.count_perm(None, include_superseded=True)
            ov["perm_active"] = m.count_perm(None)
            if user_id:
                # v1.8.4：永久记忆 / 日记 / 计数全都只算这个账号
                uid_s = str(user_id)
                for _k in ("perm", "diary"):
                    _sec = ov.get(_k) or {}
                    _sec["items"] = [x for x in (_sec.get("items") or [])
                                           if str(x.get("user_id") or "") == uid_s]
                    _sec["count"] = len(_sec["items"])
                    ov[_k] = _sec
                ov["perm_total"] = m.count_perm(uid_s, include_superseded=True)
                ov["perm_active"] = m.count_perm(uid_s)
            ov["user_id"] = str(user_id or "")
        finally:
            m.close()
        return {"ok": True, **ov}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------------- v1.8.1：关系网（知识图谱）给界面用 ----------------
def _kg_ensure_model():
    """V1.8.2：在 GUI 后端进程里把 diary 的模型回调补注册一次。

    diary._call_model 只在 Agent 子进程里注册（agent.py 启动时调
    set_model_caller），所以控制台自己点「立即补建」永远报「还没配好模型」。
    这里按需补一次；注册不上就返回 False，不假装能建。
    """
    try:
        import diary as _dm
    except Exception:
        return False
    if _dm.has_model():
        return True
    try:
        import agent as _ag
        _dm.set_model_caller(
            lambda p, system=None, max_tokens=None:
            _ag.ask_model_raw(p, system=system, max_tokens=max_tokens,
                              effort="none", tag="/图谱"))
    except Exception:
        return False
    return bool(_dm.has_model())


def _kg_uid() -> str:
    """当前正在用的账号。图谱按账号分，界面一次只看一个人。"""
    try:
        import diary as _dm
        accs = _dm.bound_accounts() or []
    except Exception:
        accs = []
    if accs:
        return str(accs[0] or "")
    try:
        import config as _cm
        return str(_cm.get("last_uid", "") or "")
    except Exception:
        return ""


def kg_overview(self=None, user_id="", limit=120):
    """关系网子页要的全部数据：统计 + 边列表（已经排好序）。

    只读，不建图。数据库不存在或没装 networkx 都返回空表，不报错。
    """
    out = {
        "ok": True, "user_id": "", "entities": 0, "relations": 0,
        "networkx": False, "edges": [], "hint": "",
    }
    try:
        import kg as _kg
    except Exception as e:
        out["hint"] = "图谱模块没加载：%s" % e
        return out
    uid = str(user_id or "").strip() or _kg_uid()
    out["user_id"] = uid
    m = _memory()
    try:
        st = _kg.stats(uid, mem=m) or {}
        out["entities"] = int(st.get("entities") or 0)
        out["relations"] = int(st.get("relations") or 0)
        out["networkx"] = bool(st.get("networkx"))
        n = max(1, min(int(limit or 120), 500))
        rows = _kg._edges(mem=m, user_id=uid)[:n]
        out["edges"] = [
            {"subject": a, "predicate": b, "object": c,
            "confidence": round(float(d or 0), 2)}
            for a, b, c, d in rows
        ]
        if not out["relations"]:
            out["hint"] = ("还没有关系。聊几轮让它抽，或者点「立即补建」"
            "把以前存下的永久记忆一次性织一遍。"
            if m.count_perm(uid) else
            "这个账号还没有永久记忆，图谱暂时没东西可织。"
        )
    except Exception as e:
        out["ok"] = False
        out["hint"] = "%s: %s" % (type(e).__name__, e)
    finally:
        try:
            m.close()
        except Exception:
            pass
    return out


def kg_build_now(self=None, user_id="", limit=40):
    """手动「立即补建」：把还没织过的永久记忆抽一批出来建图。

    后台那个线程一天只走一批；用户点了按钮就当场跑一批，最多 50 条，
    免得点了没反应。没有可用模型就直接说清楚，不假装在建。
    """
    try:
        import kg as _kg
        import diary as _dm
    except Exception as e:
        return {"ok": False, "msg": "图谱模块没加载：%s" % e}
    if not _kg.available():
        return {"ok": False,
            "msg": "没装 networkx，建不了图（pip install networkx）"}
    if not _kg_ensure_model():
        return {"ok": False,
            "msg": "还没配好模型，现在拆不出关系。先去「设置 →模型」配一个。"}
    uid = str(user_id or "").strip() or _kg_uid()
    if not uid:
        return {"ok": False, "msg": "还没绑定账号，不知道给谁织网。"}
    n = max(1, min(int(limit or 40), 50))
    m = _memory()
    try:
        r = _kg.build_from_perm(m, uid, limit=n,
                    model_ask=_dm._call_model)
        st = _kg.stats(uid, mem=m) or {}
        r["stats"] = {"entities": int(st.get("entities") or 0),
                        "relations": int(st.get("relations") or 0)}
        r["user_id"] = uid
        return r
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            m.close()
        except Exception:
            pass


def delete_perm(perm_id):
    try:
        m = _memory()
        try:
            ok = m.delete_perm(int(perm_id))
        finally:
            m.close()
        return {"ok": ok, "msg": "已删除" if ok else "没找到这条记忆"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def clear_perm(user_id):
    try:
        m = _memory()
        try:
            n = m.clear_perm(str(user_id))
        finally:
            m.close()
        return {"ok": True, "msg": f"已清空 {n} 条永久记忆", "count": n}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_db_dir():
    return _open_result(DATA_DIR, "已打开数据目录")


# ---------- 公告栏（开发者功能）----------
def get_notices():
    try:
        import notice as notice_mod
        return {"ok": True, **notice_mod.active_notices()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def refresh_notices(url=""):
    """v1.1.2 #23：手动/定时拉一次远端公告（热推送）。"""
    try:
        import notice as notice_mod
        r = notice_mod.refresh_remote(url)
        if r.get("ok"):
            _append_log(f"[公告] {r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "new": 0}


def dismiss_notice(notice_id):
    try:
        import notice as notice_mod
        ok = notice_mod.dismiss(str(notice_id))
        return {"ok": ok, **notice_mod.active_notices()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- 在线更新 ----------
def get_update_status():
    try:
        import updater as updater_mod
        return {"ok": True, **updater_mod.status()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def check_update():
    try:
        import updater as updater_mod
        return updater_mod.check()
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def apply_update(restart=True):
    try:
        import updater as updater_mod
        cb = (lambda: stop_agent() is not None and start_agent().get("ok")) if restart else None
        res = updater_mod.apply_update(restart_cb=cb)
        _append_log(f"[UI] 在线更新：{res.get('msg')}")
        return res
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def apply_update_package(path="", restart=True):
    """v1.8.5：应用本地的「增量更新包」——只写入新增/改动过的文件。

    和「立即更新」的区别：不走网络，包由用户自己选（发布时生成的小鲸鱼_Vx_增量包.zip）。
    """
    try:
        import updater as updater_mod
        cb = (lambda: stop_agent() is not None and start_agent().get("ok")) if restart else None
        res = updater_mod.apply_package(path, restart_cb=cb)
        _append_log("[UI] 增量更新：" + str(res.get("msg")))
        return res
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}



def rollback_update(stamp=""):
    try:
        import updater as updater_mod
        res = updater_mod.rollback(stamp or None)
        _append_log(f"[UI] 回滚：{res.get('msg')}")
        return res
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- 图标自定义 ----------
ICON_DIR = os.path.join(DATA_DIR, "icons")
ICON_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".svg")


def _icon_info():
    """扫描 icons/ 目录，返回可用图标列表 + 当前选中的那个"""
    try:
        os.makedirs(ICON_DIR, exist_ok=True)
        items = []
        for name in sorted(os.listdir(ICON_DIR)):
            if not name.lower().endswith(ICON_EXTS):
                continue
            p = os.path.join(ICON_DIR, name)
            if not os.path.isfile(p):
                continue
            url = ""
            try:
                import base64
                if os.path.getsize(p) <= 400 * 1024:
                    ext = os.path.splitext(name)[1].lower().lstrip(".")
                    mime = {"jpg": "jpeg", "svg": "svg+xml"}.get(ext, ext)
                    with open(p, "rb") as f:
                        url = f"data:image/{mime};base64," + base64.b64encode(
                            f.read()).decode()
            except Exception:
                url = ""
            items.append({"name": name, "url": url,
                          "size": os.path.getsize(p)})
        import config as cfg_mod
        cur = str(cfg_mod.get("icon_file") or "")
        return {"ok": True, "dir": ICON_DIR, "items": items,
                "current": cur, "count": len(items)}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": [],
                "dir": ICON_DIR, "current": "", "count": 0}


def get_icons():
    return _icon_info()


def set_icon(name):
    try:
        import config as cfg_mod
        info = _icon_info()
        names = [x["name"] for x in info["items"]]
        name = str(name or "").strip()
        if name and name not in names:
            return {"ok": False, "msg": f"没有这个图标文件：{name}"}
        cfg_mod.set_value("icon_file", name)
        _append_log(f"[UI] 图标 → {name or '默认'}")
        return {"ok": True, "msg": f"图标已切换为 {name or '默认'}", **_icon_info()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_icon_dir():
    try:
        os.makedirs(ICON_DIR, exist_ok=True)
        # 放一份说明
        txt = os.path.join(ICON_DIR, "怎么换图标.txt")
        if not os.path.exists(txt):
            with open(txt, "w", encoding="utf-8") as f:
                f.write(
                    "把 png / jpg / webp / ico 图片丢进这个目录，\n"
                    "然后到界面「设置 → 外观 → 自定义图标」里点刷新并选中它。\n"
                    "建议尺寸 128x128 或 256x256，正方形，透明背景更好看。\n")
    except Exception:
        pass
    return _open_result(ICON_DIR, "已打开图标目录")


# ---------- 统一设置保存 ----------
def _apply_switch_lifecycle(patch):
    """开关一改就动真格：说说线程拉起/停掉，音视频微服务起/停。

    v1.6.1（QA P0-2）：以前这些开关只写进 config.json，后台线程照旧跑 ——
    用户关了「自动说说」，日志里连一行提示都没有。
    返回一句人话进保存结果，没动就返回空串。
    """
    done = []
    # 说说是 Agent **进程**里的线程，控制台这边改配置动不到它 ——
    # Agent 自己每 5 秒对一次账（agent.reconcile_switches），这里只记一笔。
    for _k, _lab in (("moment_enabled", "说说"), ("moment_comment_enabled", "评论")):
        if _k in patch:
            _state = "开" if _on(patch.get(_k)) else "关"
            _append_log(f"[{_lab}] 开关已改成{_state}，Agent 会在 5 秒内跟上")
            done.append(f"{_lab}开关→{_state}")
    if ("av_module_enabled" in patch) or ("av_auto_launch" in patch):
        try:
            import config as _cfg2
            want = bool(_cfg2.get("av_module_enabled", False))
            auto = bool(_cfg2.get("av_auto_launch", False))
            br = _av().BRIDGE
            running = bool((br.status_snapshot() or {}).get("running"))
            if not want and running:
                r = br.stop(True)
                _append_log(f"[音视频] 开关关了，停掉微服务：{r.get('msg', '')}")
                done.append("音视频微服务已停")
            elif want and auto and not running:
                r = br.start()
                _append_log(f"[音视频] 开关打开，拉起微服务：{r.get('msg', '')}")
                done.append("音视频微服务已起" if r.get("ok") else "音视频微服务没起来")
        except Exception as e:                 # noqa: BLE001
            _append_log(f"[音视频] 开关联动失败（{type(e).__name__}: {e}）", "WARN")
    return "；".join(done)


def save_settings(patch):
    """批量保存设置。patch 是 dict，只处理认识的键。"""
    try:
        import config as cfg_mod
        if not isinstance(patch, dict):
            return {"ok": False, "msg": "参数必须是对象"}
        saved, rejected = [], []
        # v1.5.1（P1-4）：凡 _is_secret_key() 为真的键，写盘前一律加密、日志一律打码
        for k, v in patch.items():
            _v = v
            if _is_secret_key(k) and k != "provider_keys":
                try:
                    import security as _sec
                    _sv = str(_v or "").strip()
                    if _sv and not _sec.is_encrypted(_sv):
                        _v = _sec.encrypt_secret(_sv)
                except Exception:
                    pass
            elif k == "provider_keys":
                # QA 终审必修 3（P1）：`provider_keys` 是多厂商 Key 表
                # （{"deepseek": "sk-..."}），以前整体明文落盘 —— 这一条比
                # cloud_api_key 还该加密，因为它一次带好几把 Key。
                # 做法：**每个 value 单独加密**，字典结构不变（providers.dec_key
                # 读的时候本来就会认 dpapi: 前缀），所以不用动读取方。
                try:
                    import security as _sec
                    if isinstance(_v, dict):
                        _enc = {}
                        for _pk, _pv in _v.items():
                            _ps = str(_pv or "").strip()
                            if _ps and _ps != "***" and not _sec.is_encrypted(_ps):
                                _ps = _sec.encrypt_secret(_ps)
                            _enc[str(_pk)] = _ps
                        _v = _enc
                except Exception:
                    pass
            ok, msg, coerced = cfg_mod.set_checked(k, _v)
            if ok:
                if _is_secret_key(k):
                    saved.append(f"{k}=***")
                else:
                    saved.append(f"{k}={coerced}")
            else:
                rejected.append(f"{k}: {msg}")
        # 自启动要额外落到注册表
        if "autostart" in patch:
            try:
                import autostart_mgr
                autostart_mgr.apply(bool(patch["autostart"]))
            except Exception:
                pass
        # v1.2.1：语音模型目录改了就把目录建出来（用户填 D:\xxx 时别等到用时才报错）
        if "tts_model_dir" in patch:
            try:
                _d = str(cfg_mod.get("tts_model_dir", "") or "").strip().strip('"')
                if _d:
                    os.makedirs(_d, exist_ok=True)
                    _append_log(f"[设置] 语音模型目录已就绪：{_d}")
                else:
                    _append_log("[设置] 语音模型目录留空 → 跟随数据目录")
            except Exception as e:
                rejected.append(f"tts_model_dir: 目录建不出来（{type(e).__name__}: {e}）")
        # v1.6.1：开关不是写进 json 就算数，得真的把线程/进程拉起来或停掉
        try:
            _life = _apply_switch_lifecycle(patch)
            if _life:
                saved.append(_life)
        except Exception as e:                 # noqa: BLE001
            _append_log(f"[UI] 开关联动失败（{type(e).__name__}: {e}）", "WARN")
        _append_log(f"[UI] 保存设置：{', '.join(saved) or '无'}")
        return {"ok": not rejected,
                "msg": ("已保存 " + ", ".join(saved)) if not rejected
                       else ("部分失败：" + "；".join(rejected)),
                "saved": saved, "rejected": rejected}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v5：模型提供方 / 用量看板 / 日记编辑 / 记忆备份 ====================
# ==================== NapCat / 通知中心 ====================

def _mem_conn():
    """每次新建一个 Memory 连接。

    为什么不复用全局 mem：pywebview 的 js_api 方法可能在不同线程里被调用，
    sqlite3 的连接默认不允许跨线程共享。
    """
    from memory import Memory
    return Memory(DB_FILE)


def get_providers():
    """模型提供方预设列表 + 当前选择（不含任何 key 明文）。"""
    try:
        import providers as prov_mod
        import config as cfg_mod
        cur = prov_mod.resolve()
        cfg = cfg_mod.load()
        return {
            "ok": True,
            "items": prov_mod.list_providers(),
            "current": cur["provider"],
            "current_name": cur["provider_name"],
            "is_cloud": cur["is_cloud"],
            "base": cur["base"],
            "model": cur["model"],
            "masked_key": cur["masked_key"],
            "needs_key": cur["needs_key"],
            "resolved_ok": cur["ok"],
            "resolved_msg": cur["msg"],
            # 前端直接用 resolved.base / resolved.model 回填输入框
            # （曾经漏了这一层，前端读 r.resolved 拿到 undefined → 输入框一直空着）
            "resolved": cur,
            # 本地 / 云端各自的原始值，方便前端回填输入框
            "lm_base": cfg.get("lm_base") or "",
            "lm_model": cfg.get("lm_model") or "",
            "cloud_base": cfg.get("cloud_base") or "",
            "cloud_model": cfg.get("cloud_model") or "",
        }
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}",
                "items": [], "current": "local"}


def set_provider(pkey, base="", model="", api_key=None):
    """切换模型提供方（本地 LM Studio / 云端 API）。

    切完如果 Agent 正在跑，会让它热切换（不重启进程）。
    """
    try:
        import providers as prov_mod
        ok, msg = prov_mod.set_provider(pkey, base=base, model=model, api_key=api_key)
        if not ok:
            return {"ok": False, "msg": msg}
        _append_log(f"[UI] 模型提供方：{msg}")
        # 热切换：Agent 在跑就通知它重读配置
        if _is_running():
            try:
                import urllib.request
                import json as _json
                req = urllib.request.Request(
                    f"{AGENT_WEBHOOK}/reload_model",
                    data=_json.dumps({}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST")
                urllib.request.urlopen(req, timeout=4).read()
                msg += "（Agent 已热切换）"
            except Exception:
                msg += "（Agent 没在跑，下次启动生效）"
        return {"ok": True, "msg": msg, "provider": pkey}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def set_cloud_key(api_key):
    """单独设置云端 API Key（不切换提供方）。

    v1.0.0-Beta 安全整改（QA BUG-05）：落盘前必须加密。
    `cloud_api_key` 以前是明文写进 config.json 的 —— 和 security.py 自己的注释
    「API key 不能明文躺在 config.json 里」直接打架，也和 provider_keys（早就加密）不一致。
    """
    try:
        import config as cfg_mod
        import security
        raw = str(api_key or "").strip()
        if raw and security.is_encrypted(raw):
            enc = raw                      # 前端把密文原样传回时别再套一层
        else:
            enc = security.encrypt_secret(raw) if raw else ""
        ok, msg, _ = cfg_mod.set_checked("cloud_api_key", enc)
        if not ok:
            return {"ok": False, "msg": msg}
        if raw:
            _append_log(f"[安全] 云端 API Key 已加密保存"
                        f"（打码 {security.mask_secret(enc) if enc else ''}，"
                        f"后端 {security.crypto_backend()}）")
            return {"ok": True, "msg": "API Key 已加密保存（只有本机、只有这个用户能解开）"}
        return {"ok": True, "msg": "API Key 已清空"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def security_migrate_plaintext_keys():
    """启动自检（QA BUG-05）：config.json 里如果还躺着明文 sk- Key，就地加密并 WARN。

    老版本升级上来的用户 config.json 里一定有明文；这一步让升级后自动收口。
    """
    out = {"ok": True, "migrated": [], "msg": ""}
    try:
        import config as cfg_mod
        import security
        cfg = cfg_mod.load()
        changed = []
        cur = str(cfg.get("cloud_api_key") or "").strip()
        if cur and not security.is_encrypted(cur):
            ok, _msg, _v = cfg_mod.set_checked("cloud_api_key", security.encrypt_secret(cur))
            if ok:
                changed.append("cloud_api_key")
        keys = cfg.get("provider_keys") or {}
        if isinstance(keys, dict):
            dirty = {}
            for k, v in keys.items():
                sv = str(v or "")
                if sv and not security.is_encrypted(sv):
                    dirty[k] = security.encrypt_secret(sv)
            if dirty:
                newkeys = dict(keys)
                newkeys.update(dirty)
                ok, _msg, _v = cfg_mod.set_checked("provider_keys", newkeys)
                if ok:
                    changed.extend([f"provider_keys.{k}" for k in dirty])
        # v1.5.1（P1-4）：不止那两个老键 —— 凡 _is_secret_key() 的明文都收口，
        # 顺手统计"还剩多少明文"，有就 WARN（QA 要求启动自检扫描）。
        left = []
        for k, v in list(cfg.items()):
            if not _is_secret_key(k) or k == "provider_keys":
                continue
            sv = str(v or "").strip()
            if not sv or sv == "***":
                continue
            if security.is_encrypted(sv):
                continue
            ok, _msg, _v = cfg_mod.set_checked(k, security.encrypt_secret(sv))
            if ok:
                changed.append(k)
            else:
                left.append(k)
        out["plaintext_left"] = left
        if left:
            _append_log("[安全] 这些配置项还是明文密钥、没能自动加密："
                        + ", ".join(left), "WARN")
        out["migrated"] = changed
        if changed:
            out["msg"] = "已把明文 Key 加密：" + ", ".join(changed)
            _append_log(f"[安全] {out['msg']}（旧版本留下的明文）", "WARN")
        return out
    except Exception as e:
        out.update(ok=False, msg=f"{type(e).__name__}: {e}")
        return out


def get_usage():
    """用量看板数据：今日云端/本地、最近 7 天、按用途拆分、上限与保护状态。"""
    try:
        import usage as usage_mod
        m = _mem_conn()
        try:
            data = usage_mod.dashboard(m)
        finally:
            try:
                m.close()
            except Exception:
                pass
        return data
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}",
                "cloud": {"total": 0, "calls": 0}, "local": {"total": 0, "calls": 0},
                "limit": 0, "percent": 0, "days": [], "kinds": []}


def clear_usage():
    """清空今日账本（换 key 后重新计数）"""
    try:
        import usage as usage_mod
        m = _mem_conn()
        try:
            n = usage_mod.clear_today(m)
        finally:
            try:
                m.close()
            except Exception:
                pass
        return {"ok": True, "msg": f"已清空今日 {n} 条用量记录"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- 日记：自由编辑 ----------
def save_diary(user_id, day, content, title="", mood=""):
    """手动编辑/覆写某天的日记（用户要求「日记可以自由编辑」）。"""
    try:
        from memory import Memory
        uid = str(user_id or "").strip()
        d = str(day or "").strip()
        c = str(content or "").strip()
        if not uid or not d:
            return {"ok": False, "msg": "账号和日期都不能为空"}
        if not c:
            return {"ok": False, "msg": "日记内容不能为空"}
        m = _mem_conn()
        try:
            did = m.add_diary(uid, d, c[:8000],
                              title=str(title or "")[:80],
                              mood=str(mood or "")[:20])
        finally:
            try:
                m.close()
            except Exception:
                pass
        _append_log(f"[UI] 保存日记：{uid} {d} #{did}")
        return {"ok": True, "msg": f"已保存 {d} 的日记", "id": did}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def delete_diary(diary_id):
    try:
        m = _mem_conn()
        try:
            ok = m.delete_diary(int(diary_id))
        finally:
            try:
                m.close()
            except Exception:
                pass
        return {"ok": bool(ok), "msg": "已删除" if ok else "没找到这篇日记"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def regenerate_diary(user_id, day=""):
    """让模型重新生成某天的日记（覆盖旧的）。"""
    try:
        import diary as diary_mod
        uid = str(user_id or "").strip()
        if not uid:
            return {"ok": False, "msg": "先选一个账号"}
        m = _mem_conn()
        try:
            res = diary_mod.write_diary_for(m, uid, _peer_name(uid),
                                            day=(day or None), force=True)
        finally:
            try:
                m.close()
            except Exception:
                pass
        return res
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _peer_name(uid):
    """从 agent_state 里捞昵称（捞不到就空串）"""
    try:
        import json as _json
        p = os.path.join(DATA_DIR, "agent_state.json")
        if not os.path.exists(p):
            return ""
        with open(p, "r", encoding="utf-8") as f:
            st = _json.load(f)
        names = st.get("peer_names") or {}
        return str(names.get(str(uid)) or "")
    except Exception:
        return ""


# ---------- 记忆导出 / 导入 ----------
def export_memory(user_id="", include_diary=True, include_events=False, to_file=True):
    """导出记忆成 JSON 文件（存到 memory_backup/）。to_file=False 时只返回数据。"""
    try:
        import config as cfg_mod
        uid = str(user_id or "").strip()
        m = _mem_conn()
        try:
            data = m.export_memory(uid or None,
                                   include_diary=bool(include_diary),
                                   include_events=bool(include_events))
        finally:
            try:
                m.close()
            except Exception:
                pass
        if not to_file:
            return {"ok": True, "data": data,
                    "msg": f"共 {len(data['perm'])} 条永久记忆，{len(data['diary'])} 篇日记"}

        outdir = _backup_dir()
        os.makedirs(outdir, exist_ok=True)
        fname = (f"memory_{uid or 'all'}_"
                 + time.strftime("%Y%m%d_%H%M%S") + ".json")
        path = os.path.join(outdir, fname)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _append_log(f"[UI] 导出记忆：{path}")
        return {"ok": True, "path": path, "dir": outdir,
                "perm": len(data["perm"]), "diary": len(data["diary"]),
                "msg": (f"已导出 {len(data['perm'])} 条永久记忆、"
                        f"{len(data['diary'])} 篇日记 → {fname}")}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _backup_dir():
    """备份/导出目录（唯一真相：`mem_transfer.export_dir()`）。

    v1.3.2（QA BUG-10）：这里以前各自读 `memory_export_dir` 配置再拼 DATA_DIR，
    而写备份的那条路径走的是 `mem_transfer.export_dir()` 的写死常量 —— 于是
    「点立即备份成功、备份列表永远空」。现在读写都归一到同一个函数。
    """
    try:
        import mem_transfer as _mt_mod
        return _mt_mod.export_dir()
    except Exception:
        folder = "memory_backup"
        return folder if os.path.isabs(folder) else os.path.join(DATA_DIR, folder)


def open_memory_backup_dir():
    try:
        outdir = _backup_dir()
        os.makedirs(outdir, exist_ok=True)
    except Exception:
        pass
    return _open_result(outdir, "已打开备份目录")


def open_memory_export_dir(kind=""):
    """打开记忆导出目录（v1.1.0）；v1.3.0 ⑱ 支持按种类打开各自的子目录"""
    try:
        outdir = _mt().kind_dir(kind) if kind else _mt().export_dir()
        os.makedirs(outdir, exist_ok=True)
    except Exception:
        outdir = DATA_DIR
    return _open_result(outdir, "已打开导出目录" if not kind else "已打开这类记忆的导出目录")


def list_memory_backups():
    """列出备份目录下的备份文件（按时间倒序）。v1.3.2：目录与「立即备份」对齐。"""
    try:
        outdir = _backup_dir()
        if not os.path.isdir(outdir):
            return {"ok": True, "items": [], "dir": outdir}
        items = []
        for name in os.listdir(outdir):
            if not name.lower().endswith(".json"):
                continue
            p = os.path.join(outdir, name)
            try:
                st = os.stat(p)
                items.append({"name": name, "path": p, "size": st.st_size,
                              "mtime": st.st_mtime,
                              "mtime_text": time.strftime("%Y-%m-%d %H:%M",
                                                          time.localtime(st.st_mtime))})
            except OSError:
                continue
        items.sort(key=lambda x: -x["mtime"])
        return {"ok": True, "items": items, "dir": outdir}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def import_memory(path="", data=None, user_id="", mode="merge"):
    """把备份文件（或直接传 data）导入成永久记忆。

    mode='merge' 只补缺；mode='replace' 先清空该账号再写。
    用户要求「记忆可以选择是否导出记忆，而将他载入成永久记忆，方便备份记忆」
    —— replace 就是"载入成永久记忆"那条路。
    """
    try:
        uid = str(user_id or "").strip()
        payload = data
        if payload is None:
            p = str(path or "").strip()
            if not p or not os.path.isfile(p):
                return {"ok": False, "msg": f"找不到备份文件：{path}"}
            with open(p, "r", encoding="utf-8") as f:
                payload = json.load(f)
        m = _mem_conn()
        try:
            res = m.import_memory(payload, user_id=(uid or None), mode=mode)
        finally:
            try:
                m.close()
            except Exception:
                pass
        _append_log(f"[UI] 导入记忆：{res.get('msg')}")
        return res
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- NapCat ----------
def get_napcat():
    try:
        import napcat_mgr
        d = napcat_mgr.probe()
        # v1.3.1：把 Agent 真正在连的 WS 地址带出去，界面上才能改（连不上多半是端口对不上）
        try:
            import config as _c
            d["ws_url"] = str(_c.get("napcat_ws") or "ws://127.0.0.1:3004")
        except Exception:
            d["ws_url"] = "ws://127.0.0.1:3004"
        # v1.9.14：界面上的「不动我的 QQ」开关
        try:
            d["keep_qq"] = bool(_c.get("napcat_keep_qq", True))
        except Exception:
            d["keep_qq"] = True
        # v1.3.1：把「登没登、登的是谁」一起带出去（界面据此决定按钮和提示）
        try:
            ls = napcat_login_state()
            d["login_state"] = ls.get("state", "")
            d["logged_in"] = bool(ls.get("logged_in"))
            d["login_qq"] = str(ls.get("user_id") or "")
            d["login_msg"] = ls.get("msg", "")
            if ls.get("qr"):
                d["qr"] = ls["qr"]
        except Exception:
            d["logged_in"] = False
        return d
    except Exception as e:
        return {"ok": False, "running": False, "msg": f"{type(e).__name__}: {e}"}


def napcat_recheck(wait=8):
    """「重新检测」按钮：先清掉假死/重复的 NapCat 进程，再重新探一遍。

    v1.3.3 增（用户实机：进程假死时反复提示「已经在跑」，点「重新检测」也不动，
    只能自己开任务管理器杀 node.exe）。这里调 napcat_mgr.recheck()，
    它只在「端口全关 + 没有新鲜二维码 + 活过 90 秒」时才敢清，正在等扫码的不会误杀。
    """
    try:
        import napcat_mgr
        r = napcat_mgr.recheck(clean_stale=True)
    except Exception as e:
        return {"ok": False, "state": "error",
                "msg": f"重新检测失败：{type(e).__name__}: {e}"}
    try:
        d = get_napcat()
        d.update({k: r.get(k) for k in
                  ("cleaned", "clean_msg", "previous_state", "state", "stale")})
        if r.get("cleaned"):
            d["msg"] = r.get("msg") or d.get("msg", "")
        return d
    except Exception:
        return r


def napcat_login_state(launch_if_down=False, wait=25):
    """V1.3.1：一眼看清「NapCat 到底登没登」。

    三种状态：没跑 / 跑了但没登录 / 已登录（返回 QQ 号）。
    launch_if_down=True 时，如果没跑就顺手拉起来（用户要求：没登录就拉起让他登）。
    """
    try:
        import napcat_mgr
    except Exception as e:
        return {"ok": False, "state": "error", "msg": f"{type(e).__name__}: {e}"}
    out = {"ok": True, "state": "down", "running": False, "logged_in": False,
           "user_id": "", "qr": "", "msg": "", "launched": False}
    try:
        p = napcat_mgr.probe()
    except Exception:
        p = {}
    out["running"] = bool(p.get("running"))
    out["installed"] = str(p.get("state") or "") != "not_installed"
    out["dir"] = p.get("dir", "")
    # v1.3.2：进程在 ≠ 端口通。NapCat 扫完码才开 3003/3004，
    # 所以「进程活着但端口没开」必须算「等扫码」，不能算「没在跑」。
    try:
        pids = napcat_mgr.running_pids(out["dir"])
    except Exception:
        pids = []
    out["proc_count"] = len(pids)
    li = {}
    try:
        li = napcat_mgr.login_info() or {}
    except Exception:
        li = {}
    # NapCat 登录成功才会写 onebot11_<QQ>.json —— 有这个文件基本就能确定登过
    try:
        _uins = napcat_mgr.account_uins(out["dir"])
    except Exception:
        _uins = []
    out["account_hint"] = _uins[0] if _uins else ""
    if li.get("user_id"):
        out.update(state="logged_in", logged_in=True, user_id=str(li["user_id"]),
                   running=True, msg=f"已登录：QQ {li['user_id']}")
        return out
    # 端口被别人占着 → 直接把话说清楚，不要因为「进程不在」就退化成 no_login
    if str(p.get("state") or "") == "port_taken" and not li.get("user_id"):
        owner = p.get("port_owner") or {}
        out.update(state="port_taken", qr="", running=False,
                   msg=(p.get("msg") or "3003 端口被别的程序占着，NapCat 起不来。"),
                   port_owner=owner)
        return out
    # v1.4.1：本机 QQ 客户端占着同一个号 / 账号被风控 —— 这两种情况必须单独说，
    # 否则用户对着「等你扫码」一直扫，越扫越容易被风控。
    _pstate0 = str(p.get("state") or "")
    if _pstate0 in ("qq_client_busy", "risk_limited"):
        _qr = ""
        try:
            _qr = p.get("qr") or napcat_mgr.qr_path(p.get("dir", "")) or ""
        except Exception:
            _qr = ""
        out.update(state=_pstate0, qr=_qr, running=bool(out["proc_count"]),
                   msg=(p.get("msg") or ""),
                   qq_client=p.get("qq_client") or [],
                   last_login_error=p.get("last_login_error") or {})
        return out
    if p.get("running") or out["proc_count"]:
        qr = ""
        try:
            qr = p.get("qr") or napcat_mgr.qr_path(p.get("dir", "")) or ""
        except Exception:
            qr = ""
        if out["proc_count"] > 2:
            out.update(state="no_login", qr=qr, running=True,
                       msg=(f"检测到 {out['proc_count']} 个 NapCat 进程（重复开了），"
                            "它们会抢端口导致扫了码也登不上。点「检测并连接」会自动清场重开一个。"))
            return out
        # v1.3.3（用户实机反馈）：进程在 ≠ 能连。老代码这里一句「已经在跑，但还没有登录」
        # 糊过去，用户对着一个端口全关的死进程反复扫码，永远连不上。
        # 现在按 probe 的 state 分开说，每种情况都给出下一步该点哪儿。
        pstate = str(p.get("state") or "")
        ws_p = p.get("ws_port") or 3001
        if pstate == "port_taken":
            owner = p.get("port_owner") or {}
            out.update(state="port_taken", qr="", running=False,
                       msg=(p.get("msg") or
                            ("3003 端口被别的程序占着，NapCat 起不来。")))
            if owner:
                out["port_owner"] = owner
        elif pstate == "no_ws":
            out.update(state="no_ws", qr=qr, running=True,
                       msg=("NapCat 进程在跑、3003 端口也通，但 WebSocket 连不上 —— "
                            "QQ 消息收不到就是这个原因。去 NapCat 的 WebUI →「网络配置 → "
                            f"WebSocket 服务端」确认已启用、端口是 {ws_p}，改完点「重新检测」。"))
        elif pstate == "zombie":
            out.update(state="zombie", qr="", running=False,
                       msg=(p.get("msg") or
                            "NapCat 进程假死了（端口全关、也没有二维码）。点「重新检测」会自动清掉重开。"))
        elif pstate == "waiting_scan":
            out.update(state="no_login", qr=qr, running=True,
                       msg=("NapCat 已启动，正在等你扫码登录（没登录前 3003/3004 不会开，这是正常的）。"
                            "点「扫码登录」把二维码打开，用手机 QQ 扫一下就登上了。"))
        elif pstate == "process_no_port":
            out.update(state="no_login", qr=qr, running=True,
                       msg="NapCat 进程刚起来，端口还没开、二维码也还没出 —— 等 5~10 秒再点「重新检测」。")
        else:
            out.update(state="no_login", qr=qr, running=True,
                       msg=(p.get("msg") or "NapCat 已经在跑，但还没有 QQ 登录。"))
        return out
    # 没跑
    if launch_if_down:
        try:
            # 有重复实例时强制清场重开（否则扫了码也登不上）
            restart = out["proc_count"] > 2
            if restart:
                _append_log(f"[NapCat] 发现 {out['proc_count']} 个重复进程，先清场再启动")
            ok, msg = napcat_mgr.launch(p.get("dir", ""), wait=wait, restart=restart)
            out["launched"] = bool(ok)
            out["launch_msg"] = msg
            li2 = {}
            try:
                li2 = napcat_mgr.login_info() or {}
            except Exception:
                li2 = {}
            if li2.get("user_id"):
                out.update(state="logged_in", logged_in=True, running=True,
                           user_id=str(li2["user_id"]), msg=f"已登录：QQ {li2['user_id']}")
                return out
            qr = ""
            try:
                qr = napcat_mgr.qr_path(p.get("dir", "")) or ""
            except Exception:
                qr = ""
            if not ok:
                # v1.3.2：拉起失败就必须如实说失败 —— 以前不管成不成都说「已经拉起来了」，
                # 用户看到这句就去扫码，扫了当然没用（这就是「永远登不上」的一半原因）。
                out.update(state="down", running=False, launched=False,
                           msg=(msg or "NapCat 没起来。"))
                return out
            out.update(state="no_login", running=True, qr=qr,
                       msg=("已经把 NapCat 拉起来了。点「扫码登录」打开二维码，"
                            "用手机 QQ 扫一下；扫完这里会自动变成「已登录」。"))
            return out
        except Exception as e:
            out.update(state="down", msg=f"NapCat 没在跑，自动拉起失败：{type(e).__name__}: {e}")
            return out
    out["msg"] = "NapCat 没在跑。点「启动 NapCat」把它拉起来，然后扫码登录。"
    return out


def test_napcat_ws(url=""):
    """V1.3.1：点一下就知道「WS 连不上」到底卡在哪一步。

    只做一件事：按 Agent 用的同一个地址去连一次，把结果翻成人话。
    不碰 NapCat 的配置、不改任何东西，连完立刻断开。
    """
    try:
        import config as _c
        u = str(url or "").strip() or str(_c.get("napcat_ws") or "ws://127.0.0.1:3004")
    except Exception:
        u = str(url or "").strip() or "ws://127.0.0.1:3004"
    if not u.startswith(("ws://", "wss://")):
        return {"ok": False, "url": u, "msg": f"这个地址不像 WebSocket：{u}（要以 ws:// 或 wss:// 开头）"}
    try:
        import websocket
    except Exception as e:
        return {"ok": False, "url": u, "msg": f"本机没有 websocket 模块，测试不了：{e}"}
    try:
        ws = websocket.create_connection(u, timeout=5)
    except Exception as e:
        name = type(e).__name__
        text = str(e)
        low = (name + " " + text).lower()
        if "refused" in low or "10061" in low:
            msg = ("连不上：这个端口没人监听。多半是 NapCat 没启动（或还没扫码登录完），"
                   "先去上面点「启动 NapCat」，等它显示已登录再测。")
        elif "timeout" in low or "timed out" in low:
            msg = ("连不上：等超时了。可能 NapCat 正在启动/登录，也可能被防火墙拦了。"
                   "过 10 秒再测一次；还不行就看 NapCat 日志。")
        elif "403" in low or "401" in low or "handshake" in low:
            msg = ("被拒绝了：NapCat 里的 WebSocket 服务端可能没启用，或者它的 token 和这里不一致。"
                   "去「WebUI → 网络配置 → WebSocket 服务端」看一眼。")
        else:
            msg = f"连不上（{name}）：{text}"
        return {"ok": False, "url": u, "msg": msg}
    try:
        ws.settimeout(3)
        got = ""
        try:
            first = ws.recv()
            if first:
                got = "而且已经收到事件了，说明链路是通的"
        except Exception:
            got = "但 3 秒内没收到事件（NapCat 可能还没登录，登录后才会推消息）"
        msg = f"连上了：{u} 端口是通的{got}。"
        return {"ok": True, "url": u, "msg": msg}
    finally:
        try:
            ws.close()
        except Exception:
            pass


def napcat_quit_qq_and_connect(wait=35):
    """v1.8.3：「重试连接」/「启动 NapCat」——用户点了就**接管**。

    历史：v1.4.1 会 taskkill 掉 QQ 再快速登录，v1.6.2 改成「绝不碰 QQ、只让人手动退出」，
    结果用户实机投诉「都已经登录了为什么不直接连」。v1.8.3 回到接管，但只在这两个
    **用户主动点**的入口里做：
      1. 探一遍状态（风控直接如实回，不硬上）；
      2. 需要接管时先 `quit_qq_client()` 退出官方 QQ，再用本地缓存快速登录（不用扫码）；
      3. 退不干净就明说「还有 QQ 进程没关掉」，不假装连上了。

    页面加载时的自动拉起走 `napcat_login_state()`，那条路对 qq_client_busy 提前 return，
    所以**不会**在用户没点的情况下把 QQ 关掉。
    返回 {ok, msg, state, user_id, logged_in}
    """
    try:
        import napcat_mgr
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}
    out = {"ok": False, "msg": "", "state": "", "killed": 0, "killed_msg": ""}
    try:
        p = napcat_mgr.probe()
    except Exception as e:
        return {"ok": False, "msg": f"检测失败：{type(e).__name__}: {e}"}
    out["state"] = str(p.get("state") or "")
    d = p.get("dir") or napcat_mgr.find_install_dir()
    if not d:
        out["msg"] = "还没装 NapCat，先去「QQ 接入」里装好再来。"
        return out

    # 已经连上了？直接读登录态，什么都不用做。
    try:
        li = napcat_mgr.login_info() or {}
    except Exception:
        li = {}
    if li.get("user_id"):
        out.update(ok=True, logged_in=True, user_id=str(li["user_id"]),
                   msg=f"已经连上了：QQ {li['user_id']} 在线。")
        return out

    st = out["state"]
    if st == "risk_limited":
        out["msg"] = ((p.get("msg") or "")
                      + "\n\n（风控没过之前点了也没用，先别急着反复试。）")
        return out
    if st == "qq_client_busy":
        # v1.8.3：本机 QQ 已登录 —— 用户点了就接管（先退官方 QQ，再快速登录）
        ok, msg = False, ""
        try:
            # v1.9.14：这是用户明确点「接管/这次帮我退出 QQ」的入口 → 允许正常退出 QQ
            ok, msg = napcat_mgr.launch(d, wait=int(wait), restart=False, close_qq=True,
                                    takeover=True)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
        out["ok"] = bool(ok)
        out["msg"] = (msg if ok else
                          "想直接接管，但没能退出官方 QQ（可能被别的程序占着）。\n"
                          "在任务管理器里结束 QQ 之后再点一次「启动 NapCat」就行。")
        try:
            _append_log("[NapCat] 接管连接：" + str(out.get("msg") or "")[:200])
        except Exception:
            pass
        return out

    ok, msg = False, ""
    try:
        ok, msg = napcat_mgr.launch(d, wait=int(wait), restart=False, close_qq=False,
                            takeover=True)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
    out["ok"] = bool(ok)
    out["msg"] = msg
    try:
        li2 = napcat_mgr.login_info() or {}
    except Exception:
        li2 = {}
    if li2.get("user_id"):
        out.update(ok=True, logged_in=True, user_id=str(li2["user_id"]),
                   msg=f"连上了：QQ {li2['user_id']} 已登录，不用扫码。")
    try:
        _append_log("[NapCat] 重试连接：" + str(out.get("msg") or "")[:200])
    except Exception:
        pass
    return out


def start_napcat(wait=25, force=False):
    try:
        import napcat_mgr
        # v1.8.3：用户点「启动 NapCat」就是授权接管 —— 本机 QQ 已登录时先退官方 QQ，
        # 再用本地缓存快速登录（不用扫码）。这一步只在用户主动点击时发生：
        # 页面加载的自动拉起走 napcat_login_state()，那条路对 qq_client_busy 提前 return。
        # v1.3.2：force 语义从「允许再开一个」改成「清场重开一个」
        # （以前 force 会真的多开一对进程，几十个实例抢 TCP 端口 → 扫码也登不上）
        try:
            import config as _cfg
            _keep = bool(_cfg.get("napcat_keep_qq", True))
        except Exception:
            _keep = True
        # v1.9.14：默认不动用户正在用的 QQ（关了才按接管处理）
        ok, msg = napcat_mgr.launch(wait=int(wait), allow_second_qq=bool(force),
                                    restart=bool(force), close_qq=False, takeover=bool(not _keep))
        _append_log(f"[UI] 启动 NapCat：{msg}")
        return {"ok": ok, "msg": msg, "log": napcat_mgr.launch_log_path()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def napcat_log(lines=40):
    """NapCat 启动日志（排查「点了没反应」用）。"""
    try:
        import napcat_mgr
        return {"ok": True, "path": napcat_mgr.launch_log_path(),
                "text": napcat_mgr.launch_log_tail(int(lines))}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_napcat_qr():
    """打开 NapCat 的登录二维码（v1.9.17 起：画在应用内的弹窗里，不再拉系统看图程序）。

    老实现是 os.startfile(png) —— 拉一个外部应用打开图片。用户看不出这张码过没过期，
    过期了只能「关掉、重新启动」反复折腾，而反复扫码正是被风控的原因。
    现在把 png 读成 base64 交给前端自己画（见 qq_login_panel），这里只返回数据。
    """
    try:
        import napcat_mgr
        qr = napcat_mgr.qr_png() or {}
        if not qr:
            return {"ok": False, "msg": "还没有二维码：先点『启动 NapCat』，等它把二维码画出来",
                    "qr": {}}
        return {"ok": True, "msg": "二维码已取到", "qr": qr, "inline": True}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "qr": {}}


# ==================== v1.9.17：扫码登录弹窗 ====================
# 二维码多久算过期。NapCat 的码大概两分半失效，但它自己发现失效时会自动换一张
# （onQRCodeSessionFailed → getQRCodePicture），所以这里留点余量；到点还没扫就直接
# 替用户换新的 —— 让用户自己判断「这码还行不行」本来就是不可能的。
QR_TTL_SEC = 150
_LOGIN_PANEL_CACHE = {"ts": 0.0, "data": None}
_QR_REFRESH_GUARD = {"ts": 0.0, "msg": ""}


def _webui_mod():
    try:
        import napcat_webui
        return napcat_webui
    except Exception:
        return None


def qq_login_panel(start=False, fresh=False):
    """「扫码登录」弹窗要的全部数据（一次取全，前端只负责画）。

    返回：
        ok        False = 连 NapCat 模块都没加载起来
        mode      logged_in 已连上 / scan 要扫码 / quick 有缓存可直登 /
                  start NapCat 没在跑 / wait 还在出码 / busy 被占用或风控
        qr        {data_url, age, expires_in, sha1, path} —— 直接塞 <img src>
        accounts  [{uin,last,ok,bad,cached,label}] —— 本机登录过的号，让用户自己挑
        webui     {reachable,port,phase,error} —— 决定能不能自动换码
        msg       一句人话，前端直接显示
    """
    if not start and not fresh:
        try:
            now = time.time()
            if _LOGIN_PANEL_CACHE["data"] is not None and now - _LOGIN_PANEL_CACHE["ts"] < 1.0:
                return _LOGIN_PANEL_CACHE["data"]
        except Exception:
            pass
    out = {"ok": True, "mode": "", "logged_in": False, "uin": "", "nick": "",
           "msg": "", "qr": {}, "expires_in": 0, "qr_expired": False,
           "accounts": [], "notes": [], "dir": "", "degraded": False,
           "qq_open": False, "qq_pids": [], "conflict": [], "can_takeover": False,
           "hint": "",
           "webui": {"reachable": False, "port": 0, "phase": "", "error": ""},
           "napcat": {"state": "", "running": False, "proc_count": 0}}
    try:
        import napcat_mgr
    except Exception as e:
        out.update(ok=False, mode="start",
                   msg=f"NapCat 模块没加载起来：{type(e).__name__}: {e}")
        return out
    webui = _webui_mod()

    if start:
        try:
            r = start_napcat(wait=20, force=False) or {}
            if r.get("msg"):
                out["notes"].append(str(r["msg"])[:400])
        except Exception as e:
            out["notes"].append(f"启动 NapCat 失败：{type(e).__name__}: {e}")

    # probe 的 deep 模式要翻进程表；轮询时用浅探测，别把界面拖住
    try:
        p = napcat_mgr.probe(deep=False) or {}
    except Exception as e:
        p = {"state": "down", "msg": f"{type(e).__name__}: {e}"}
    d = p.get("dir") or ""
    if not d:
        try:
            d = napcat_mgr.find_install_dir() or ""
        except Exception:
            d = ""
    out["dir"] = d
    out["napcat"] = {"state": str(p.get("state") or ""),
                     "running": bool(p.get("running")),
                     "proc_count": int(p.get("proc_count") or 0)}

    # ---------- 1.5 本机官方 QQ 开着吗（v1.9.18） ----------
    # 用户实机：一边挂着 QQ 一边点「启动 NapCat」，以前必定被一句「请先退出你的 QQ」顶回来，
    # 点几次都拉不起来。现在照常启动、自动换号，把「退 QQ」变成弹窗里的一个按钮，
    # 并且把「哪个号正被你的 QQ 占着」直接标在账号上，用户一眼就知道为什么它登不上。
    _qq_cli, _conf = [], {}
    try:
        _qq_cli = napcat_mgr.qq_client_procs() or []
    except Exception:
        _qq_cli = []
    try:
        _conf = napcat_mgr.conflict_uins() or {}
    except Exception:
        _conf = {}
    out["qq_open"] = bool(_qq_cli)
    out["qq_pids"] = [int(x.get("pid") or 0) for x in _qq_cli if x.get("pid")]
    out["conflict"] = [str(u) for u in _conf.keys()]
    out["can_takeover"] = bool(_qq_cli)

    # ---------- 1. 到底登没登录（硬依据：3003 的 get_login_info） ----------
    li = {}
    try:
        li = napcat_mgr.login_info(timeout=2) or {}
    except Exception:
        li = {}
    if li.get("user_id"):
        _nick = str(li.get("nickname") or "")
        out.update(mode="logged_in", logged_in=True, uin=str(li["user_id"]), nick=_nick,
                   msg="已经连上了：QQ %s%s" % (li["user_id"], ("（" + _nick + "）") if _nick else ""))
        try:
            napcat_mgr.mark_uin_ok(str(li["user_id"]))
        except Exception:
            pass
        _LOGIN_PANEL_CACHE.update(ts=time.time(), data=out)
        return out

    # ---------- 2. WebUI 状态（能不能自动换码 / NapCat 自己怎么说） ----------
    st = {}
    if webui and d:
        try:
            st = webui.status(d) or {}
        except Exception as e:
            st = {"ok": False, "reachable": False, "msg": f"{type(e).__name__}: {e}"}
        try:
            c = webui.cfg(d) or {}
        except Exception:
            c = {}
        out["webui"] = {"reachable": bool(st.get("reachable")), "ok": bool(st.get("ok")),
                        "port": int(c.get("port") or 0),
                        "phase": str(st.get("phase") or ""),
                        "error": str(st.get("error") or "")}
    else:
        out["degraded"] = True

    # ---------- 3. 二维码 ----------
    qr = {}
    try:
        qr = napcat_mgr.qr_png(d) or {}
    except Exception:
        qr = {}
    if qr:
        age = int(qr.get("age") or 0)
        qr["expires_in"] = max(0, QR_TTL_SEC - age)
        out["qr_expired"] = age >= QR_TTL_SEC
        out["qr"] = qr
        out["expires_in"] = qr["expires_in"]

    # ---------- 4. 本机登录过的号（让用户自己挑，而不是我们猜） ----------
    cands, wu = [], []
    try:
        cands = [str(x) for x in (napcat_mgr.login_candidates(d) or [])]
    except Exception:
        cands = []
    if webui and out["webui"].get("reachable"):
        try:
            wu = [a.get("uin") for a in (webui.quick_list(d) or []) if a.get("uin")]
        except Exception:
            wu = []
    try:
        ust = napcat_mgr.uin_state() or {}
    except Exception:
        ust = {}
    bad = ust.get("bad") or {}
    okmap = ust.get("ok") or {}
    last = str(ust.get("last") or "")
    seen, accts = set(), []
    for u in list(wu) + list(cands):
        if u in seen:
            continue
        seen.add(u)
        lab = []
        if u == last:
            lab.append("上次用的")
        if u in okmap:
            lab.append("登过")
        if u in wu:
            lab.append("缓存可用")
        if u in bad:
            lab.append("上回失效")
        if u in _conf:
            lab.append("你的 QQ 正用着")
        if not lab:
            lab.append("本机登录过")
        accts.append({"uin": u, "last": (u == last), "ok": (u in okmap),
                      "bad": (u in bad), "cached": (u in wu),
                      "occupied": (u in _conf), "label": "·".join(lab)})
    # 能用的排前面：缓存可用且没被占 → 一般 → 失效/被官方 QQ 占着
    accts.sort(key=lambda a: (0 if (a["cached"] and not a["bad"] and not a["occupied"])
                              else (2 if (a["bad"] or a["occupied"]) else 1),
                              0 if a["last"] else 1))
    out["accounts"] = accts

    # ---------- 5. 定模式 + 说人话 ----------
    rstate = str(p.get("state") or "")
    if rstate in ("qq_client_busy", "risk_limited", "port_taken"):
        # 这三种情况扫多少次都没用，必须先说清楚，否则用户越扫越容易被风控
        out["mode"] = "busy"
        out["msg"] = str(p.get("msg") or "")
    elif not out["napcat"]["running"]:
        out["mode"] = "start"
        out["msg"] = "NapCat 没在跑。点「启动并出码」，起来之后二维码会直接出现在这里。"
    elif qr:
        out["mode"] = "scan"
        if out["qr_expired"]:
            out["msg"] = "上一张二维码过期了，正在换新的…"
        elif out["webui"].get("reachable"):
            out["msg"] = "用手机 QQ 扫下面这个码。过期会自动换一张，不用重新启动。"
        else:
            out["msg"] = ("用手机 QQ 扫下面这个码。NapCat 的 WebUI 没开，这里没法替你换码 —— "
                          "过期了就点「重新出码」。")
    else:
        out["mode"] = "wait"
        out["msg"] = "NapCat 起来了，正在向 QQ 要二维码，一般 5~10 秒就出来。"

    # v1.9.18：官方 QQ 开着时把话说在前面，别让用户以为是坏了
    if out.get("qq_open") and out["mode"] != "logged_in":
        _occ = [u for u in out.get("conflict") or []]
        out["hint"] = ("你的官方 QQ 正开着 —— 同一个号不能两边同时在线。"
                       "想用小鲸鱼连你现在这个号，点「退出我的 QQ 并接管」；"
                       "不想退，就点下面另一个缓存可用的号，或直接扫这张码登别的号。")
        if _occ:
            out["notes"].append("被你的 QQ 占着的号：" + "、".join(_occ) + "（这几个现在登不上）")

    if len(accts) >= 2 and out["mode"] in ("scan", "wait", "start"):
        out["notes"].append("这台电脑登录过 %d 个 QQ。缓存还能用的，点号就行，不用扫码。"
                            % len(accts))
    if out["webui"].get("error"):
        out["notes"].append("NapCat 说：" + out["webui"]["error"])
    _LOGIN_PANEL_CACHE.update(ts=time.time(), data=out)
    return out


def qq_login_poll():
    """弹窗每 1.5 秒来一次（只读，不触发任何动作）。"""
    return qq_login_panel(fresh=True)


def qq_login_refresh():
    """「重新出码」/ 倒计时归零时调：让 NapCat 换一张新二维码。

    NapCat 的 /api/QQLogin/RefreshQRcode 内部最多等 10 秒，所以这里可能阻塞十几秒 ——
    只在用户点按钮、或前端倒计时归零时调用，绝不在轮询路径上。
    """
    out = {"ok": False, "msg": "", "panel": None}
    webui = _webui_mod()
    try:
        import napcat_mgr
        d = napcat_mgr.find_install_dir() or ""
    except Exception:
        d = ""
    if not webui:
        out["msg"] = "这个版本没有 WebUI 客户端，只能重启 NapCat 换码"
        return out
    if not d:
        out["msg"] = "还没找到 NapCat 装在哪儿"
        return out
    if not webui.reachable(d):
        out["msg"] = ("NapCat 的 WebUI 端口没开（可能还没起来，或者 6099 被别的程序占了）。"
                      "这种情况没法单独换码，点「重启并出码」重来一次。")
        return out
    r = webui.refresh_qr(d) or {}
    _LOGIN_PANEL_CACHE["ts"] = 0.0
    out["ok"] = bool(r.get("ok"))
    out["msg"] = str(r.get("msg") or "")
    out["panel"] = qq_login_panel(fresh=True)
    try:
        _append_log("[NapCat] 换二维码：" + str(out["msg"])[:160])
    except Exception:
        pass
    return out


def qq_login_pick(uin=""):
    """用户点了某个 QQ 号 → 让 NapCat 用本地缓存直接登录它。

    这是「登录过好几个号、让用户自己选」的落地：不重启 NapCat、不退出官方 QQ、不用扫码。
    成不成由 NapCat 判断，我们只把它的回答翻成人话。
    """
    u = str(uin or "").strip()
    out = {"ok": False, "msg": "", "panel": None}
    if not (u.isdigit() and 5 <= len(u) <= 12):
        out["msg"] = "QQ 号看着不对：%s" % (uin or "(空)")
        return out
    webui = _webui_mod()
    try:
        import napcat_mgr
        d = napcat_mgr.find_install_dir() or ""
    except Exception:
        d = ""
    if not webui or not d:
        out["msg"] = "没法用缓存直登（缺少 WebUI 客户端或找不到 NapCat）"
        return out
    if not webui.reachable(d):
        out["msg"] = ("NapCat 的 WebUI 端口没开，没法让它用缓存登录。"
                      "先点「启动并出码」，或者重新检测一下连接。")
        return out
    r = webui.quick_login(u, d) or {}
    try:
        _append_log("[NapCat] 选号直登 %s：%s" % (u, str(r.get("msg") or "")[:160]))
    except Exception:
        pass
    if not r.get("ok"):
        out["msg"] = str(r.get("msg") or "快速登录失败")
        out["panel"] = qq_login_panel(fresh=True)
        return out
    # 给它一点时间连上（要重建连接、拉好友列表），最多等 15 秒
    t0 = time.time()
    uin_ok = ""
    while time.time() - t0 < 15:
        try:
            li = napcat_mgr.login_info(timeout=2) or {}
        except Exception:
            li = {}
        if li.get("user_id"):
            uin_ok = str(li["user_id"])
            break
        time.sleep(1.0)
    _LOGIN_PANEL_CACHE["ts"] = 0.0
    out["panel"] = qq_login_panel(fresh=True)
    if uin_ok:
        try:
            napcat_mgr.mark_uin_ok(uin_ok)
        except Exception:
            pass
        out["ok"] = True
        out["msg"] = "连上了：QQ %s 已登录，不用扫码。" % uin_ok
    else:
        out["ok"] = False
        out["msg"] = ("已经让 NapCat 用缓存登录 QQ %s，但 15 秒内没等到它连上。\n"
                      "如果它还在转，等一会儿点「刷新状态」；一直不行就扫码登录（那个一定行）。" % u)
    return out


def qq_login_start():
    """「启动并出码」：拉起 NapCat，再把弹窗要的数据整包给回去。"""
    return qq_login_panel(start=True, fresh=True)


def qq_login_takeover():
    """「退出我的 QQ 并接管」—— 用户明确点了才会退 QQ，绝不自动做。

    v1.9.18：以前是默认路径（点启动就想退 QQ），被用户否掉了。
    现在只有这个按钮会退，退完立刻用本地缓存登录同一个号，不用扫码。
    """
    out = {"ok": False, "msg": "", "panel": None}
    try:
        import napcat_mgr
        d = napcat_mgr.find_install_dir() or ""
    except Exception as e:
        out["msg"] = f"NapCat 模块没加载起来：{type(e).__name__}: {e}"
        return out
    _pid = []
    try:
        _pid = [int(x.get("pid") or 0) for x in (napcat_mgr.qq_client_procs() or [])]
    except Exception:
        _pid = []
    try:
        _append_log("[NapCat] 用户点了「退出我的 QQ 并接管」，QQ 进程数=%d" % len(_pid))
    except Exception:
        pass
    r = {}
    try:
        r = napcat_mgr.launch(d, restart=True, close_qq=True, wait=40) or (False, "")
    except Exception as e:
        r = (False, f"{type(e).__name__}: {e}")
    ok, msg = (r if isinstance(r, tuple) else (bool(getattr(r, "ok", False)), str(r)))
    _LOGIN_PANEL_CACHE["ts"] = 0.0
    out["panel"] = qq_login_panel(fresh=True)
    out["ok"] = bool(ok)
    out["msg"] = str(msg or "")
    if not ok:
        out["msg"] = ("退出 QQ 之后还是没连上：\n" + out["msg"] +
                      "\n（这种情况多半是登录态被 QQ 校验作废了，扫一次码最稳。）")
    return out


def open_napcat_dir():
    try:
        import napcat_mgr
        d = napcat_mgr.find_install_dir() or DATA_DIR
        return _open_result(d, "已打开 NapCat 目录")
    except Exception:
        return _open_result(DATA_DIR, "已打开 NapCat 目录")


def check_napcat_release():
    """只查最新版本（不下载），让用户决定要不要装"""
    try:
        import napcat_mgr
        info = napcat_mgr.find_release_asset()
        return info
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def install_napcat(target_dir=""):
    """下载并解压 NapCat（用户必须已经在界面上同意）

    v1.1.7 修（P1-2）：装完顺手把 OneBot 配置写好，并把 WebUI 地址/token
    一起带回去 —— 前端拿这个弹窗引导用户去扫码登录（以前装完什么都不给，
    用户停在"装好了，然后呢"）。
    """
    try:
        import napcat_mgr
        ok, msg, d = napcat_mgr.download_and_install(target_dir)
        _append_log(f"[UI] 安装 NapCat：{msg}")
        out = {"ok": ok, "msg": msg, "dir": d}
        try:
            wi = napcat_mgr.webui_info(d)
            out["webui"] = wi.get("url", "")
            out["webui_token"] = wi.get("token", "")
            out["config"] = napcat_mgr.write_onebot_config(d).get("msg", "")
            out["need_login"] = True
        except Exception:
            pass
        return out
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def write_napcat_config(qq=""):
    """手动（重新）写一次 NapCat 的 OneBot 配置。

    给「一键修复」用：配置被用户改坏、或者装的时候还不知道 QQ，
    登录之后点一下就能补上 onebot11_<QQ>.json。
    """
    try:
        import napcat_mgr
        r = napcat_mgr.write_onebot_config("", str(qq or "").strip())
        _append_log(f"[UI] 写 NapCat 配置：{r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}



# ---------- 通知中心 ----------
def get_notice_center():
    try:
        import notice as notice_mod
        return notice_mod.center()
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}",
                "items": [], "unread": 0}


def get_notice_popup():
    try:
        import notice as notice_mod
        return notice_mod.popup_pending()
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def ack_notice_popup(ids=None):
    try:
        import notice as notice_mod
        if not ids:
            pend = notice_mod.popup_pending().get("items") or []
            ids = [it["id"] for it in pend]
        ok, msg = notice_mod.mark_popup_shown(ids)
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def mark_notice_read(notice_id):
    try:
        import notice as notice_mod
        ok, msg = notice_mod.mark_read(notice_id)
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def mark_all_notices_read():
    try:
        import notice as notice_mod
        ok, msg = notice_mod.mark_all_read()
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- 卸载：保留还是删除数据 ----------
def get_uninstall_info():
    """给"卸载时要不要保留记忆"的对话框提供事实数据。"""
    try:
        import config as cfg_mod
        keep = bool(cfg_mod.get("keep_data_on_uninstall", True))
        files = []
        for name in ("config.json", "personas.json", "memory.db",
                     "agent_state.json", "agent.log"):
            p = os.path.join(DATA_DIR, name)
            if os.path.exists(p):
                try:
                    files.append({"name": name, "size": os.path.getsize(p)})
                except OSError:
                    files.append({"name": name, "size": 0})
        return {"ok": True, "keep_data": keep, "data_dir": DATA_DIR,
                "files": files,
                "msg": ("卸载后会保留配置与记忆（只删程序文件）" if keep
                        else "卸载后会连配置与记忆一起删除")}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def set_keep_data(keep):
    """设置卸载时是否保留数据（卸载向导会问一次）"""
    try:
        import config as cfg_mod
        ok, msg, _ = cfg_mod.set_checked("keep_data_on_uninstall", bool(keep))
        return {"ok": ok, "msg": msg if ok else msg, "keep": bool(keep)}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- 卸载程序（v8） ----------
def get_uninstall_plan():
    """卸载会删什么、留什么。"""
    try:
        import uninstall
        import config as cfg_mod
        p = uninstall.plan_info()
        p["keep_data"] = bool(cfg_mod.get("keep_data_on_uninstall", True))
        return p
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}",
                "keep": [], "remove": [], "remove_count": 0}


def run_uninstall(keep_data=True):
    """真卸载。返回 ok 后前端应当立刻关窗口（进程退出后 bat 才会动手）。"""
    try:
        import uninstall
        ok, msg = uninstall.run(bool(keep_data))
        _append_log(f"[UI] 卸载：keep_data={bool(keep_data)} → {msg}")
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _quit_soon(delay=0.8):
    """v1.5.2：让本进程主动退出。

    为什么要主动退出：打包成 onefile 的 exe 会解出 `%TEMP%\\_MEIxxxxxx`，
    只有「子进程正常结束」时 PyInstaller 的引导进程才会把那个目录删掉。
    以前是桌面端 TerminateProcess 直接砍，引导进程也跟着没了 → 每开一次留一份
    几百 MB 的 _MEI，几天就把 C 盘塞满（QA No-Go 第 1 条）。
    所以这里先回一句"正在退出"，再延时 os._exit：引导进程看到子进程结束就会自清理。
    """
    def _boom():
        try:
            time.sleep(max(0.05, float(delay)))
        except Exception:
            pass
        try:
            _append_log("[UI] 后端主动退出（引导进程会清理临时目录）")
        except Exception:
            pass
        try:
            os._exit(0)
        except Exception:
            pass

    try:
        threading.Thread(target=_boom, name="quit-soon", daemon=True).start()
    except Exception:
        pass


def quit_app():
    """v1.2.0：请求退出整个程序。

    以前是销毁 pywebview 窗口；现在界面在 Tauri 里，这里负责「停掉 Agent 子进程 + 自己退出」，
    再由 Rust 端结束窗口进程。主动退出是为了让 PyInstaller 引导进程清掉 %TEMP% 里的解包目录。
    """
    try:
        _QUIT["flag"] = True
        try:
            stop_agent()
        except Exception:
            pass
        try:
            import av_bridge
            av_bridge.stop()
        except Exception:
            pass
        _append_log("[UI] 收到退出请求：已停止 Agent 子进程，本服务即将退出")
        _quit_soon(0.8)
        return {"ok": True, "msg": "正在退出…"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- 好感度看板（v8） ----------
_AFF_LEVELS = ((0, "stranger", "陌生人"), (10, "acquaintance", "认识的人"),
               (30, "familiar", "熟人"), (60, "friend", "朋友"),
               (120, "close_friend", "好朋友"), (200, "confidant", "知己"),
               (350, "special", "特别的人"))


def _aff_levels():
    """v1.3.0 ⑳：以 Memory 那边为准（用户可自定义），拿不到就用内置表。"""
    try:
        import memory as mem_mod
        lv = mem_mod.Memory.affinity_levels()
        if lv:
            return tuple(lv)
    except Exception:
        pass
    return _AFF_LEVELS


def _aff_level(score):
    key, cn = "stranger", "陌生人"
    for lo, k, c in _aff_levels():
        if score >= lo:
            key, cn = k, c
    return key, cn


def get_affinity_board(limit=200):
    """好感度看板：每个人多少分、什么关系、聊过几次。"""
    try:
        import config as cfg_mod
        import agent as ag
        m = _mem_conn()
        try:
            rows = m.list_affinity(int(limit))
        finally:
            m.close()
        items = []
        for r in rows:
            sc = int(r.get("score") or 0)
            _k, cn = _aff_level(sc)
            items.append({
                "user_id": str(r.get("user_id") or ""),
                "name": str(r.get("name") or "") or str(r.get("user_id") or ""),
                "score": sc,
                "level": _k,
                "level_cn": cn,
                "msgs": int(r.get("msgs") or 0),
                "positive": int(r.get("positive") or 0),
                "negative": int(r.get("negative") or 0),
                "updated_at": r.get("updated_at") or 0,
            })
        items.sort(key=lambda x: -x["score"])
        return {
            "ok": True,
            "enabled": bool(cfg_mod.get("affinity_enabled", False)),
            "show_score": bool(cfg_mod.get("affinity_show_score", True)),
            "items": items,
            "count": len(items),
            "levels": [{"key": k, "cn": c, "min": lo} for lo, k, c in _aff_levels()],
            "levels_custom": bool(cfg_mod.get("affinity_levels")),
            "max": int(cfg_mod.get("affinity_max") or 1000),
            "tone": getattr(ag, "_AFFINITY_TONE", {}) or {},
        }
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def reset_affinity(user_id=""):
    try:
        m = _mem_conn()
        try:
            n = m.reset_affinity(str(user_id or "") or None)
        finally:
            m.close()
        return _with(get_affinity_board(), True,
                     ("已重置 %d 个账号的好感度" % n) if n else "没有可重置的记录")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def set_affinity_score(user_id, score):
    try:
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            return {"ok": False, "msg": f"QQ 号格式不对：{user_id}"}
        m = _mem_conn()
        try:
            ok = m.set_affinity(uid, int(score))
        finally:
            m.close()
        return _with(get_affinity_board(), bool(ok),
                     "已把 %s 的好感度设为 %d" % (uid, int(score)))
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- v1.1.4 阶段三：伴侣关系（跟好感度分表，互不覆盖） ----------
def get_romance_list():
    """好感度页/伴侣页用：关系等级表 + 已设置的伴侣列表。"""
    try:
        import memory as mem_mod
        levels = [{"key": k, "cn": cn} for k, cn in mem_mod.Memory.ROMANCE_LEVELS]
        m = _mem_conn()
        try:
            rows = m.list_romance(100)
        finally:
            m.close()
        return {"ok": True, "levels": levels, "rows": rows}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def get_romance(user_id=""):
    try:
        m = _mem_conn()
        try:
            return {"ok": True, "item": m.get_romance(str(user_id or "").strip())}
        finally:
            m.close()
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def set_romance(user_id, level="", note=None, auto=0):
    """设置某个人的伴侣关系。level 为空表示只改备注。

    auto=1：让分数自动管（之后随聊天升降）；auto=0：用户手动钉死。"""
    try:
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            return {"ok": False, "msg": f"QQ 号格式不对：{user_id}"}
        m = _mem_conn()
        try:
            ok, msg, item = m.set_romance(uid, level or None, note, auto=auto)
        finally:
            m.close()
        out = get_romance_list()
        out.update({"ok": bool(ok), "msg": msg, "item": item})
        return out
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def delete_romance(user_id=""):
    """删除一条伴侣关系（回到「朋友」，不动好感度分数）。"""
    try:
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            return {"ok": False, "msg": f"QQ 号格式不对：{user_id}"}
        m = _mem_conn()
        try:
            ok, msg = m.delete_romance(uid)
        finally:
            m.close()
        out = get_romance_list()
        out.update({"ok": bool(ok), "msg": msg})
        return out
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ---------- 记忆重编译 ----------
def memory_rebuild_preview():
    """预览重编译会改动什么（不改数据）"""
    try:
        import mem_rebuild
        m = _mem_conn()
        try:
            a = mem_rebuild.analyze(m)
            r = mem_rebuild.rebuild(m, dry_run=True)
        finally:
            try:
                m.close()
            except Exception:
                pass
        return {"ok": True, "analyze": a, "plan": r,
                "msg": a.get("msg", "") + ("；" + r["msg"] if r.get("details") else "")}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def memory_rebuild_run(fill_norm=True, dedupe=True, migrate_legacy=True):
    """真执行重编译（会先备份 memory.db）"""
    try:
        import mem_rebuild
        m = _mem_conn()
        try:
            r = mem_rebuild.rebuild(m, dry_run=False,
                                    fill_norm=bool(fill_norm),
                                    dedupe=bool(dedupe),
                                    migrate_legacy=bool(migrate_legacy))
        finally:
            try:
                m.close()
            except Exception:
                pass
        _append_log(f"[UI] 记忆重编译：{r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def list_db_backups():
    try:
        import mem_rebuild
        return {"ok": True, "items": mem_rebuild.list_db_backups()}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def memory_verify():
    """v1.1.0：记忆一致性体检"""
    try:
        import mem_rebuild
        r = mem_rebuild.verify(_mem_conn())
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": [], "total": 0}


def memory_restore_backup(name):
    """v1.1.0：把某个备份恢复回 memory.db"""
    try:
        import mem_rebuild
        if not str(name or "").strip():
            return {"ok": False, "msg": "没指定备份文件"}
        r = mem_rebuild.restore_backup(name, mem=_mem_conn())
        if r.get("ok"):
            _append_log(f"[记忆] {r.get('msg')}")
        return r
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v6：背景自定义 ====================
BG_DIR = os.path.join(DATA_DIR, "backgrounds")
BG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")

_BG_README = """背景图片目录
============

把想当背景的图片丢进这个目录（png / jpg / webp / bmp / gif），
然后到界面「设置 → 外观 → 背景」里点刷新并选中它。

建议
----
* 尺寸 1920x1080 以上，横图
* 太亮/太花的图会影响文字辨识度，可以在界面里调「遮罩浓度」
* 想放视频不行，只支持静态图和 gif

删掉图片后记得在界面点一下刷新。
"""


def _bg_items():
    """扫描 backgrounds/，返回 [{name,url,size}]。缩略图用 base64 内联。"""
    try:
        os.makedirs(BG_DIR, exist_ok=True)
        txt = os.path.join(BG_DIR, "怎么用.txt")
        if not os.path.exists(txt):
            try:
                with open(txt, "w", encoding="utf-8") as f:
                    f.write(_BG_README)
            except Exception:
                pass
        items = []
        for name in sorted(os.listdir(BG_DIR)):
            if not name.lower().endswith(BG_EXTS):
                continue
            p = os.path.join(BG_DIR, name)
            if not os.path.isfile(p):
                continue
            url = ""
            try:
                import base64
                # 背景图可能很大，只在 1.2MB 以内才内联预览
                if os.path.getsize(p) <= 1200 * 1024:
                    ext = os.path.splitext(name)[1].lower().lstrip(".")
                    mime = {"jpg": "jpeg"}.get(ext, ext)
                    with open(p, "rb") as f:
                        url = f"data:image/{mime};base64," + base64.b64encode(
                            f.read()).decode()
            except Exception:
                url = ""
            items.append({"name": name, "url": url,
                          "size": os.path.getsize(p)})
        return items
    except Exception:
        return []


def get_bg_data(name):
    """把某张背景图整张转成 data URL。

    为什么不放在 get_backgrounds() 里一起返回：
    手机拍的壁纸经常 3~8MB，base64 之后 5~11MB，每次进设置页都传一遍会很慢。
    所以列表接口只给 ≤1.2MB 的缩略图，真正要铺满窗口时再单独要一次。
    """
    try:
        name = os.path.basename(str(name or "").strip())
        if not name:
            return {"ok": False, "msg": "没有指定背景", "url": ""}
        p = os.path.join(BG_DIR, name)
        if not os.path.isfile(p):
            return {"ok": False, "msg": f"没有这个背景文件：{name}", "url": ""}
        size = os.path.getsize(p)
        if size > 25 * 1024 * 1024:
            return {"ok": False,
                    "msg": f"这张图太大了（{size / 1024 / 1024:.1f}MB），换一张小于 25MB 的",
                    "url": ""}
        import base64
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        mime = {"jpg": "jpeg"}.get(ext, ext)
        with open(p, "rb") as f:
            raw = f.read()
        url = f"data:image/{mime};base64," + base64.b64encode(raw).decode()
        return {"ok": True, "url": url, "size": size, "name": name,
                "msg": f"已加载 {name}（{size / 1024:.0f}KB）"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "url": ""}


def get_backgrounds():
    """背景列表 + 当前设置。"""
    try:
        import config as cfg_mod
        items = _bg_items()
        cur = str(cfg_mod.get("bg_file") or "")
        cur_url = ""
        for it in items:
            if it["name"] == cur:
                cur_url = it["url"]
                break
        return {
            "ok": True, "dir": BG_DIR, "items": items, "count": len(items),
            "current": cur, "current_url": cur_url,
            "dim": int(cfg_mod.get("bg_dim") or 0),
            "blur": int(cfg_mod.get("bg_blur") or 0),
            "fit": str(cfg_mod.get("bg_fit") or "cover"),
            "fits": ["cover", "contain", "repeat"],
        }
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": [],
                "dir": BG_DIR, "current": "", "count": 0, "current_url": ""}


def set_background(name):
    """选一张背景。name 空串 = 不用背景。"""
    try:
        import config as cfg_mod
        info = get_backgrounds()
        names = [x["name"] for x in info["items"]]
        name = str(name or "").strip()
        if name and name not in names:
            return {"ok": False, "msg": f"没有这个背景文件：{name}"}
        cfg_mod.set_value("bg_file", name)
        _append_log(f"[UI] 背景 → {name or '无'}")
        return _with(get_backgrounds(), True, f"背景已切换为 {name or '无'}")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def save_bg_options(dim=0, blur=0, fit="cover"):
    """保存背景的遮罩浓度 / 模糊 / 平铺方式。

    数值越界自动夹到合法区间（不报错，界面滑条本来就夹过）；
    只有 fit 写错这种"真填错了"才提示，而且不影响其它两项保存。
    """
    try:
        import config as cfg_mod
        for k, v in (("bg_dim", dim), ("bg_blur", blur)):
            ok, msg, _c = cfg_mod.set_checked(k, v)
            if not ok:
                _append_log(f"[UI] 背景参数 {k} 被拒：{msg}", "WARN")
        note = ""
        ok, msg, _c = cfg_mod.set_checked("bg_fit", fit)
        if not ok:
            note = "（填充方式没改：" + msg + "）"
        _append_log(f"[UI] 背景参数 dim={dim} blur={blur} fit={fit}")
        return _with(get_backgrounds(), True, "背景参数已保存" + note)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_bg_dir():
    try:
        os.makedirs(BG_DIR, exist_ok=True)
        txt = os.path.join(BG_DIR, "怎么用.txt")
        if not os.path.exists(txt):
            with open(txt, "w", encoding="utf-8") as f:
                f.write(_BG_README)
    except Exception:
        pass
    return _open_result(BG_DIR, "已打开背景目录")


# ==================== v6：多语言 ====================
def get_langs():
    """可选语言列表 + 当前语言的词条 + 缺词统计。"""
    try:
        import i18n
        i18n.ensure_dir()
        cur = i18n.current()
        data = i18n.export(cur)
        langs = i18n.available()
        return {
            "ok": True, "current": cur, "items": langs,
            "dict": (data or {}).get("dict", {}),
            "count": len(data.get("items") or []),
            "missing": i18n.missing_keys(cur),
            "base_count": (data or {}).get("base_count", 0),
            "dir": os.path.join(DATA_DIR, "langs"),
        }
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}",
                "items": [], "dict": {}, "current": "zh-CN", "missing": []}


def get_ui_lang(code=""):
    """当前语言的「整句翻译表」：{中文原文: 译文}。

    前端拿到后在 DOM 里做文本替换 —— 这样不用给每个元素加 data-i18n，
    也不用改 HTML 结构，外挂一个 json 就能加语言。

    code 可传具体语言码（切换语言时前端要立刻要新表，见 applyUiLang），
    不传就用当前生效的语言。
    """
    try:
        import i18n
        i18n.ensure_dir()
        code = (code or "").strip() or i18n.current()
        ui = i18n.ui_dict(code) if code != i18n.DEFAULT_LANG else {}
        # v1.8.16：非中文却拿到空表时，以前是「ok: True + ui: {}」——
        # 前端 applyUiLang 里 `if (!n) return;` 静默什么都不做，用户看到
        # 「切了英语还是全中文」却没有任何提示。现在明确报错，让界面能说清原因。
        if code != i18n.DEFAULT_LANG and not ui:
            return {"ok": False, "code": code, "ui": {}, "count": 0,
                    "is_default": False,
                    "msg": ("没有 %s 的界面词表。安装包里应该自带 langs/%s.json；"
                            "如果是自己删掉了，删掉数据目录下的 langs/ 再重启会自动重建。"
                            % (code, code))}
        return {"ok": True, "code": code, "ui": ui, "count": len(ui),
                "is_default": code == i18n.DEFAULT_LANG}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}",
                "code": "zh-CN", "ui": {}, "count": 0, "is_default": True}


def set_lang(code):
    try:
        import i18n
        i18n.ensure_dir()
        ok, msg = i18n.set_lang(code)
        _append_log(f"[UI] 语言 → {code}")
        return _with(get_langs(), ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_lang_dir():
    d = os.path.join(DATA_DIR, "langs")
    try:
        import i18n
        d = i18n.ensure_dir() or d
    except Exception:
        pass
    return _open_result(d, "已打开语言目录")


# ==================== v6：记忆多格式导入导出 ====================
def list_memory_formats():
    try:
        import formats
        return {"ok": True, "items": formats.list_formats(),
                "current": str(_load_config().get("memory_format") or "qq-agent")}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def memory_export(fmt="qq-agent", user_id="", to_file=True):
    """按指定格式导出记忆。fmt 见 formats.FORMATS。"""
    try:
        import formats
        m = _mem_conn()
        try:
            ok, text, msg = formats.export(
                m, fmt, user_id=str(user_id or "") or None, include_diary=True)
        finally:
            try:
                m.close()
            except Exception:
                pass
        if not ok:
            return {"ok": False, "msg": msg}
        out = ""
        if to_file:
            root = _backup_dir()
            os.makedirs(root, exist_ok=True)
            name = formats.guess_filename(fmt, str(user_id or "") or None)
            out = os.path.join(root, name)
            with open(out, "w", encoding="utf-8-sig" if fmt == "csv" else "utf-8") as f:
                f.write(text)
            _append_log(f"[UI] 记忆已导出（{fmt}）→ {out}")
        return {"ok": True, "msg": msg or f"已导出（{fmt}）", "fmt": fmt,
                "path": out, "chars": len(text),
                "preview": text[:1500]}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def memory_import(path="", content="", fmt="", user_id="", mode="merge"):
    """从任意格式的文件 / 文本导入记忆。

    path 优先；没给 path 就用 content 文本。fmt 空则自动识别。
    """
    try:
        import formats
        text = ""
        src = ""
        if path:
            p = str(path)
            if not os.path.isabs(p):
                p = os.path.join(DATA_DIR, p)
            if not os.path.isfile(p):
                return {"ok": False, "msg": f"文件不存在：{p}"}
            raw = open(p, "rb").read()
            for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
                try:
                    text = raw.decode(enc)
                    break
                except Exception:
                    continue
            src = os.path.basename(p)
        elif content:
            text = str(content)
            src = "粘贴的文本"
        else:
            return {"ok": False, "msg": "没有可导入的内容"}
        f = str(fmt or "").strip() or None
        ok, data, used = formats.parse(text, f, str(user_id or ""))
        if not ok:
            return {"ok": False, "msg": f"解析失败：{data}"}
        m = _mem_conn()
        try:
            r = formats.apply(m, data, user_id=str(user_id or ""),
                              replace_conflict=True)
        finally:
            try:
                m.close()
            except Exception:
                pass
        _append_log(f"[UI] 记忆导入（{used}）{src}：{r.get('msg')}")
        return {"ok": bool(r.get("ok")), "msg": r.get("msg", ""),
                "fmt": used, "source": src, "detail": r}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v6：主动关心计划 ====================
def _with(base, ok=True, msg="", **extra):
    """把 get_plans()/get_moment_status() 这类"顺便带一份列表"的结果合并进来。

    注意：base 里也带 ok / msg，直接 **base 会把本次操作的失败结果盖成成功，
    所以这里先把 base 的 ok/msg 剔掉，再由本次操作决定。
    """
    out = {}
    if isinstance(base, dict):
        out = {k: v for k, v in base.items() if k not in ("ok", "msg")}
    out.update(extra)
    out["ok"] = bool(ok)
    out["msg"] = str(msg or "")
    return out


def _wrap(fn, *args, **kwargs):
    """v1.1.2：#6494 那批 Api 方法用到的统一包一层。

    前端拿到的永远是 {ok, msg, ...}，后台抛异常也不会让整个桥接调用报
    "name '_wrap' is not defined" 这种前端看不懂的错。插件/功能分工这类
    「读一下配置再返回」的接口都走这里。
    """
    try:
        r = fn(*args, **kwargs)
    except Exception as e:
        import traceback
        try:
            _append_log("[桥接] %s 失败：%s: %s" % (getattr(fn, "__name__", fn),
                                                type(e).__name__, e))
        except Exception:
            pass
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e),
                "trace": traceback.format_exc()[-800:]}
    if isinstance(r, dict):
        r.setdefault("ok", True)
        r.setdefault("msg", "")
        return r
    return {"ok": True, "msg": "", "data": r}


def get_plans(status="", limit=80):
    """关心计划列表 + 今日用量。"""
    try:
        import config as cfg_mod
        m = _mem_conn()
        try:
            items = m.list_plans(limit=int(limit), status=(status or None))
            used = m.count_plans_today()
            pend = m.count_plans(m.PLAN_PENDING)
        finally:
            try:
                m.close()
            except Exception:
                pass
        for it in items:
            ts = it.get("plan_at") or 0
            it["plan_at_text"] = time.strftime(
                "%m-%d %H:%M", time.localtime(ts)) if ts else ""
            it["overdue"] = bool(ts and ts <= time.time()
                                 and it.get("status") == "pending")
        return {"ok": True, "items": items, "today": used, "pending": pend,
                "enabled": bool(cfg_mod.get("care_plan_enabled", True)),
                "source": str(cfg_mod.get("care_plan_source") or "model"),
                "max_per_day": int(_ndef(cfg_mod.get("care_plan_max_per_day"), 6)),
                "hours": list(cfg_mod.get("care_plan_hours") or [8, 23])}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def add_plan(user_id, session_key, plan_at_text, reason="", topic=""):
    """手动加一条关心计划。plan_at_text 支持 'YYYY-MM-DD HH:MM' 或 'HH:MM'（今天/明天）。"""
    try:
        uid = str(user_id or "").strip()
        if not re.fullmatch(r"\d{5,12}", uid):
            return {"ok": False, "msg": f"QQ 号格式不对：{uid}（应该是 5~12 位数字）"}
        sk = str(session_key or "").strip() or f"private_{uid}"
        ts = _parse_plan_time(plan_at_text)
        if ts <= 0:
            return {"ok": False, "msg": f"时间看不懂：{plan_at_text}（写成 2026-10-23 15:00 或 15:00）"}
        if ts <= time.time():
            return {"ok": False, "msg": "时间已经过去了，请写未来的时间"}
        m = _mem_conn()
        try:
            pid, is_new = m.add_plan(user_id=uid, session_key=sk, plan_at=ts,
                                     reason=reason or "", topic=topic or "",
                                     source="manual", dedup_min=1)
        finally:
            try:
                m.close()
            except Exception:
                pass
        if not is_new:
            return {"ok": False, "msg": "已经有一条几乎一样的计划了"}
        _append_log(f"[UI] 关心计划 #{pid} {time.strftime('%m-%d %H:%M', time.localtime(ts))} {reason}")
        return _with(get_plans(), True, "已加入关心计划", id=pid)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _parse_plan_time(text):
    """解析计划时间。

    支持：
      +30 / +30m / +30min / +30分钟   → 30 分钟后
      +2h / +2小时 / +2时             → 2 小时后
      +1d / +1天 / +1日               → 1 天后
      2026-10-23 09:00 / 10-23 09:00  → 指定时刻
      09:00                            → 今天（已过则明天）
    """
    s = str(text or "").strip()
    if not s:
        return 0
    m = re.fullmatch(r"\+?\s*(\d{1,4})\s*(m|min|分钟|分|h|hr|hour|小时|时|d|day|天|日)?", s, flags=re.I)
    if m:
        n = int(m.group(1))
        unit = (m.group(2) or "m").lower()
        if unit in ("h", "hr", "hour", "小时", "时"):
            return time.time() + n * 3600
        if unit in ("d", "day", "天", "日"):
            return time.time() + n * 86400
        if unit in ("m", "min", "分钟", "分"):
            return time.time() + n * 60
        return time.time() + n
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M",
                "%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            t = time.mktime(time.strptime(s, fmt))
            if fmt in ("%Y-%m-%d", "%Y/%m/%d"):
                t += 9 * 3600
            if fmt in ("%m-%d %H:%M",):
                lt = time.localtime(t)
                now = time.localtime()
                t = time.mktime((now.tm_year, lt.tm_mon, lt.tm_mday,
                                 lt.tm_hour, lt.tm_min, 0, 0, 0, -1))
            return t
        except Exception:
            continue
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            lt = time.strptime(s, fmt)
            now = time.localtime()
            t = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                             lt.tm_hour, lt.tm_min, 0, 0, 0, -1))
            if t <= time.time():
                t += 86400   # 今天过了就算明天
            return t
        except Exception:
            continue
    return 0


def update_plan(plan_id, plan_at_text="", reason=None, topic=None):
    try:
        ts = _parse_plan_time(plan_at_text) if plan_at_text else 0
        if plan_at_text and ts <= 0:
            return {"ok": False, "msg": f"时间看不懂：{plan_at_text}"}
        m = _mem_conn()
        try:
            ok, msg = m.update_plan(int(plan_id), plan_at=(ts or None),
                                    reason=reason, topic=topic)
        finally:
            try:
                m.close()
            except Exception:
                pass
        return _with(get_plans(), ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def delete_plan(plan_id):
    try:
        m = _mem_conn()
        try:
            ok = m.delete_plan(int(plan_id))
        finally:
            try:
                m.close()
            except Exception:
                pass
        return _with(get_plans(), bool(ok), "已删除" if ok else "没找到这条计划")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def cancel_plan(plan_id):
    try:
        m = _mem_conn()
        try:
            ok = m.cancel_plan(int(plan_id))
        finally:
            try:
                m.close()
            except Exception:
                pass
        return _with(get_plans(), bool(ok), "已取消" if ok else "没找到这条计划")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v6：说说状态 / 配图 / 评论 ====================

# ==================== v1.6.0：功能开启的性能预警 ====================
# 用户要求（阶段三）：开这些「重型功能」时先弹一个温和的提示，确认后才允许启动对应服务。
# 后端是契约方：没确认就一律返回 need_ack，前端拿到后再弹窗，用户点「知道了」→ perf_ack → 重试。
PERF_LABEL = {
    "av": "音视频通话",
    "moment": "自动说说",
    "embed": "本地向量记忆",
}


def perf_ack_msg(kind):
    lab = PERF_LABEL.get(str(kind or ""), kind)
    return ("此功能开启后会启动【%s】后台服务，可能会占用一些性能。"
            "若您的电脑配置有限，建议先关闭其他无关程序噢~" % lab)


def perf_acked(kind):
    try:
        return bool(_cfg_mod.get("perf_ack_%s" % kind, False))
    except Exception:
        return False


def perf_ack(*_a, **kw):
    """
    既能被直接调用 perf_ack("av", True)，也能被当成 Api 的方法调用
    api.perf_ack("av", True)（那样第一个位置参数会是 self）。
    """
    kind, ok = _perf_ack_args(*_a, **kw)
    return _perf_ack(kind, ok)


def _perf_ack_args(*a, **kw):
    vals = [x for x in a if isinstance(x, (str, bool, int)) or x is None]
    kind = kw.get("kind", vals[0] if vals else "")
    ok = kw.get("ok", vals[1] if len(vals) > 1 else True)
    return kind, ok


def _perf_ack(kind="", ok=True):
    """用户点了「知道了」之后调用（ok=False 表示这次先不开）。"""
    k = str(kind or "").strip()
    if k not in PERF_LABEL:
        return {"ok": False, "msg": "未知的功能：%s" % k}
    if ok:
        try:
            _cfg_mod.set_checked("perf_ack_%s" % k, True)
        except Exception as e:
            return {"ok": False, "msg": "记不住这次确认：%s" % e}
    try:
        _append_log("[性能预警] %s%s" % (PERF_LABEL[k], "已确认开启" if ok else "这次先不开"))
    except Exception:
        pass
    return {"ok": True, "kind": k, "acked": perf_acked(k),
            "msg": "知道了，这就开启「%s」" % PERF_LABEL[k] if ok else "好的，先不开"}


def _perf_need_ack(patch):
    """从一次 save_settings 的补丁里判断是否需要先确认。

    v1.6.0：只在「本来是关的、现在要打开」时问；已经是开的还问一次很烦人。
    """
    try:
        p = patch or {}
        if p.get("moment_enabled") is True and not perf_acked("moment"):
            if not bool(_cfg_mod.get("moment_enabled", False)):
                return "moment"
    except Exception:
        pass
    return ""



def get_moment_status():
    """说说的完整状态：能力探测、配额、配图目录、评论收件箱。"""
    try:
        import qzone
        import config as cfg_mod
        st = qzone.status()
        info = _read_state_file()
        mo = (info.get("moment") or {})
        st.update({
            "ok": True,
            "quota": int(mo.get("quota") or 0),
            # v1.4.0：界面上「0 / ?」就是因为只给了 quota 没给上限
            "quota_min": int(_ndef(cfg_mod.get("moment_min_per_day"), 1)),
            "quota_max": int(_ndef(cfg_mod.get("moment_max_per_day"), 10)),
            "quota_range": [int(_ndef(cfg_mod.get("moment_min_per_day"), 1)),
                            int(_ndef(cfg_mod.get("moment_max_per_day"), 10))],
            "count": int(mo.get("count") or 0),
            "date": str(mo.get("date") or ""),
            "last_tid": str(mo.get("last_tid") or ""),
            "last_text": str(mo.get("last_text") or ""),
            "logs": (mo.get("log") or [])[-12:],
            "moment_images": [
                {"name": os.path.basename(p), "path": p, "size": os.path.getsize(p)}
                for p in qzone.list_images(qzone.image_dir("moment"))
            ],
            "imagegen_enabled": bool(cfg_mod.get("imagegen_enabled")),
            "imagegen_ok": False,
            "image_source": str(cfg_mod.get("moment_image_source") or "mix"),
            "preset_count": len(qzone.list_images(qzone.image_dir("moment"))),
        })
        try:
            import stickers as stk
            st["sticker_count"] = stk.count_stickers()
        except Exception:
            st["sticker_count"] = 0
        try:
            import imagegen
            st["generated_count"] = len(imagegen.list_generated(limit=999))
        except Exception:
            st["generated_count"] = 0
        try:
            import imagegen
            iok, imsg = imagegen.available()
            st["imagegen_ok"] = iok
            st["imagegen_msg"] = imsg
        except Exception:
            pass
        m = _mem_conn()
        try:
            st["comments"] = m.list_comments(limit=40)
            st["comment_pending"] = m.count_comments(m.COMMENT_PENDING)
        finally:
            try:
                m.close()
            except Exception:
                pass
        return st
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _read_state_file():
    try:
        p = os.path.join(DATA_DIR, "agent_state.json")
        if not os.path.isfile(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def open_moment_image_dir():
    try:
        import qzone
        d = qzone.image_dir("moment")
        txt = os.path.join(d, "怎么用.txt")
        if not os.path.exists(txt):
            with open(txt, "w", encoding="utf-8") as f:
                f.write("把想当说说配图预先放这里（png/jpg/webp）。\n"
                        "设置 → 说说 里把「发图」打开、来源选 preset 或 mix，\n"
                        "发说说时会从这里随机挑一张。\n")
    except Exception:
        pass
    return _open_result(d, "已打开配图目录")


def open_comment_inbox():
    d = os.path.join(DATA_DIR, "qzone_inbox")
    try:
        import qzone
        d = qzone.ensure_inbox() or d
    except Exception:
        pass
    return _open_result(d, "已打开评论收件箱")


def reply_comment_now(comment_id):
    """立刻给一条待回复评论生成并送出回复。

    v1.9.20：走 agent 的新链路（网页协议 + 「回复 @昵称 ：」格式 + 三重去重），
    并且只要请求发出去就标记 replied —— 手动回过的，后台轮询不会再回一遍。
    """
    try:
        import qzone
        import agent as agent_mod
        m = _mem_conn()
        try:
            cur = m.get_comment(comment_id) if hasattr(m, "get_comment") else {}
            if not cur:
                pend = [c for c in m.get_pending_comments(limit=20)
                        if int(c.get("id") or 0) == int(comment_id)]
                cur = pend[0] if pend else {}
            if not cur:
                return {"ok": False, "msg": "找不到这条评论"}
            if str(cur.get("status") or "") == "replied":
                return {"ok": False, "msg": "这条已经回过了"}
            c = cur
            text = agent_mod._comment_reply_text(
                c.get("moment_text"), c.get("commenter_name"), c.get("content"))
            if not text:
                return {"ok": False, "msg": "模型没生成出回复（可能模型不可用）"}
            ok, how, sent = agent_mod._deliver_comment_reply_ex(c, text)
            shown = agent_mod._comment_at_text(
                agent_mod._comment_who(c), text) or text
            if sent:
                m.mark_comment(c["id"], m.COMMENT_REPLIED, text)
            return _with(get_moment_status(), ok,
                         ("已回复（%s）：%s" % (how, shown)) if ok
                         else (("没发出去，但已标记不再重试：%s" % how) if sent
                               else f"没发：{how}"))
        finally:
            try:
                m.close()
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def drain_comment_inbox():
    """手动扫一次评论收件箱，把 json 收进数据库。"""
    try:
        import qzone
        ok, items, msg = qzone.drain_inbox()
        n = 0
        m = _mem_conn()
        try:
            for it in items:
                _cid, is_new = m.add_comment(
                    content=it.get("content", ""),
                    commenter_id=it.get("commenter_id", ""),
                    commenter_name=it.get("commenter_name", ""),
                    moment_tid=it.get("moment_tid", ""),
                    moment_text=it.get("moment_text", ""), source="inbox",
                    remote_id=it.get("remote_id", ""))
                if is_new:
                    n += 1
        finally:
            try:
                m.close()
            except Exception:
                pass
        return _with(get_moment_status(), True, f"{msg}，其中新增 {n} 条")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def delete_comment(comment_id):
    try:
        m = _mem_conn()
        try:
            ok = m.delete_comment(int(comment_id))
        finally:
            try:
                m.close()
            except Exception:
                pass
        return _with(get_moment_status(), bool(ok), "已删除" if ok else "没找到这条评论")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def post_moment_rich(text="", use_images=True):
    """发说说（v6：可带图）。text 空则由模型生成。"""
    try:
        import qzone
        import config as cfg_mod
        content = str(text or "").strip()
        if not content:
            import agent as agent_mod
            # v1.4.0：手动触发一律强制生成（force=True），被 SKIP 挡掉就是按钮坏了
            content = (agent_mod.moment_compose(force=True) or "").strip()
            if not content:
                return {"ok": False,
                        "msg": "模型没写出正文，你在输入框里写一句再发就行"}
        imgs = []
        if use_images and bool(cfg_mod.get("moment_image_enabled")):
            try:
                import agent as agent_mod
                imgs = agent_mod._moment_images(content)
            except Exception:
                imgs = []
        r = qzone.post(content, imgs)
        if r.get("ok"):
            _append_log(f"[UI] 手动发说说（配图 {len(imgs)} 张）：{content[:60]}")
        return _with(get_moment_status(), bool(r.get("ok")), r.get("msg", ""),
                     tid=r.get("tid", ""), content=content, images=len(imgs))
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v6：生图 ====================
def get_imagegen_status():
    try:
        import imagegen
        st = imagegen.status()
        try:
            st["via"] = str(_cfg_mod.get("imagegen_via") or "auto")
        except Exception:
            st["via"] = "auto"
        st["generated"] = [
            {"name": os.path.basename(p), "path": p, "size": os.path.getsize(p)}
            for p in (imagegen.list_generated() or [])
        ][:24]
        return st
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "generated": []}


# ==================== v1.1.1：画图（最大 720p） ====================
# 用户要的：有个画图界面，最大画面 720p；还能让模型自己决定画不画、画完发给谁。
DRAW_SIZES = [
    ("1280x720", "横屏 720p（1280×720）"),
    ("720x1280", "竖屏 720p（720×1280）"),
    ("1024x1024", "方图 1024×1024"),
    ("960x540", "横屏 540p（960×540）"),
    ("540x960", "竖屏 540p（540×960）"),
]


def _draw_max_side():
    try:
        return max(256, min(2048, int(_cfg_mod.get("draw_max_side", 1280) or 1280)))
    except Exception:
        return 1280


def _draw_size(size=""):
    """把 "1280x720" 之类的字符串夹到上限内，返回 (w, h)。"""
    s = str(size or "1280x720").lower().replace("×", "x").replace("*", "x")
    try:
        w, h = [int(x) for x in s.split("x")[:2]]
    except Exception:
        w, h = 1280, 720
    m = _draw_max_side()
    if max(w, h) > m:
        k = m / float(max(w, h))
        w, h = int(w * k), int(h * k)
    # 对齐到 8 的倍数，本地模型对尺寸敏感
    w, h = max(64, w // 8 * 8), max(64, h // 8 * 8)
    return w, h


def get_draw_status():
    """画图页的初始数据。"""
    try:
        import imagegen
        ok, msg = imagegen.available()
        try:
            out = imagegen.out_dir()
        except Exception:
            out = ""
        items = []
        for p in (imagegen.list_generated(limit=60) or []):
            try:
                it = {"name": os.path.basename(p), "path": p,
                      "size": os.path.getsize(p),
                      "mtime": int(os.path.getmtime(p))}
                # 缩略图：小图直接内联成 data URL，前端就不用再绕一层本地服务
                if len(items) < 12 and it["size"] <= 4 * 1024 * 1024:
                    with open(p, "rb") as fh:
                        raw = fh.read()
                    ext = os.path.splitext(p)[1].lower().lstrip(".")
                    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
                            "webp": "webp", "gif": "gif"}.get(ext, "png")
                    it["data"] = f"data:image/{mime};base64," + base64.b64encode(raw).decode("ascii")
                items.append(it)
            except Exception:
                pass
        return {
            "ok": True,
            "enabled": bool(_cfg_mod.get("draw_enabled", True)),
            "auto": bool(_cfg_mod.get("draw_auto", False)),
            "chance": float(_ndef(_cfg_mod.get("draw_chance", 0.15), 0.15)),
            "max_side": _draw_max_side(),
            "learn": bool(_cfg_mod.get("draw_learn", True)),
            "sizes": [{"value": v, "label": l} for v, l in DRAW_SIZES],
            "via": str(_cfg_mod.get("imagegen_via") or "auto"),
            "available": ok,
            "msg": msg,
            "out_dir": out,
            "generated": items,
        }
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "generated": []}


def draw_now(prompt="", size="1280x720", model=""):
    """画一张。返回 {ok,path,name,msg}。失败不抛异常。"""
    try:
        if not bool(_cfg_mod.get("draw_enabled", True)):
            return {"ok": False, "msg": "画图功能关着（设置里打开「画图」）"}
        p = str(prompt or "").strip()
        if not p:
            return {"ok": False, "msg": "写点想画什么"}
        w, h = _draw_size(size)
        import imagegen
        r = imagegen.generate(p, size=f"{w}x{h}", model=model or None,
                              tag="画图 " + p[:20])
        if not r.get("ok"):
            # v1.6.0：程序生图已删除（不再让模型写 Pillow 代码）。
            # 失败就说清楚，想「不靠生图接口也能画」请用 SVG 生图。
            _why = str(r.get("msg") or "")[:120]
            return {"ok": False,
                    "msg": f"没画成（{_why}）。不想依赖生图接口的话，用『SVG 生图』。"}
        path = r.get("path") or ""
        # v1.1.1：学会一点就往「学习」里记一条（像小孩学画，一点点积累）
        if path and bool(_cfg_mod.get("draw_learn", True)):
            try:
                import learn
                learn.add(source="draw", title="学会画：" + p[:24],
                          content=f"用户让我画「{p}」，用 {w}x{h} 画出来了。"
                                  f"以后遇到类似的可以这样构图：{p[:60]}",
                          trigger="用户说想画东西", tags="画图")
            except Exception:
                pass
        _append_log(f"[画图] {w}x{h} {p[:40]} -> {os.path.basename(path)}")
        return {"ok": True, "path": path, "name": os.path.basename(path),
                "w": w, "h": h, "msg": "画好了"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def draw_svg(prompt="", size="1280x720", model=""):
    """v1.4.2：SVG 生图 —— 让模型直接写矢量图，本地渲染成 PNG。
    和「画一张」的区别：完全不依赖生图 API，聊天模型能写字就能画。"""
    try:
        if not bool(_cfg_mod.get("draw_enabled", True)):
            return {"ok": False, "msg": "画图功能关着（设置里打开「画图」）"}
        p = str(prompt or "").strip()
        if not p:
            return {"ok": False, "msg": "写点想画什么"}
        w, h = _draw_size(size)
        import imagegen
        r = imagegen.svg_draw(p, size="%dx%d" % (w, h), model=model or "")
        if not r.get("ok"):
            return {"ok": False, "msg": r.get("msg") or "没画出来",
                    "path": r.get("path") or ""}
        path = r.get("path") or ""
        if path and bool(_cfg_mod.get("draw_learn", True)):
            try:
                import learn
                learn.add(source="draw", title="学会用 SVG 画：" + p[:20],
                          content=("用 SVG 画了「%s」，%dx%d。这条路只要聊天模型能写字就行，"
                                   "不需要生图接口。" % (p, w, h)),
                          trigger="用户说想画东西", tags="画图,SVG")
            except Exception:
                pass
        _append_log("[SVG 画图] %dx%d %s -> %s" % (w, h, p[:40],
                                                   os.path.basename(path)))
        return {"ok": True, "path": path, "name": os.path.basename(path),
                "w": w, "h": h, "svg": (r.get("meta") or {}).get("svg", ""),
                "msg": r.get("msg") or "画好了（SVG 矢量画的）"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def delete_draw(path):
    """删掉一张生成出来的图（测试图、画歪的都能删，只删画图目录里的）。"""
    try:
        p = os.path.abspath(str(path or ""))
        import imagegen
        out = os.path.abspath(imagegen.out_dir())
        if not p.startswith(out + os.sep):
            return {"ok": False, "msg": "只能删画图目录里的文件"}
        if not os.path.exists(p):
            return {"ok": False, "msg": "文件已经不在了"}
        os.remove(p)
        return {"ok": True, "msg": "已删除 " + os.path.basename(p)}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def save_canvas(data_url, name=""):
    """v1.1.2 #9：手动画布存图。前端传 data:image/png;base64,...，这里落盘到画图目录。"""
    try:
        s = str(data_url or "")
        if "," in s and s.lstrip().lower().startswith("data:"):
            s = s.split(",", 1)[1]
        if not s:
            return {"ok": False, "msg": "画布是空的"}
        raw = base64.b64decode(s)
        if len(raw) < 100:
            return {"ok": False, "msg": "画布是空的"}
        if len(raw) > 12 * 1024 * 1024:
            return {"ok": False, "msg": "图太大了"}
        import imagegen
        path = imagegen._save_bytes(raw, ".png")
        if not path:
            return {"ok": False, "msg": "保存失败"}
        if bool(_cfg_mod.get("draw_learn", True)):
            try:
                import learn
                learn.add(source="draw", title="自己手画了一张",
                          content="用户在画布上手动画了一张图，已存进画图目录。",
                          trigger="手动画布", tags="画图,手绘")
            except Exception:
                pass
        _append_log(f"[手动画布] 存了一张：{os.path.basename(path)}")
        return {"ok": True, "path": path, "name": os.path.basename(path), "msg": "存好了"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_draw_dir():
    try:
        import imagegen
        return _open_result(imagegen.out_dir(), "已打开画图目录")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v1.1.1：启动顺序检测（先 QQ → 再模型 → 再出话/出图/读图/动态） ====================
_CHECK_TRACES = []   # 检测过程产生的临时痕迹（图片、学习条目、用量记录），供「清理测试数据」用


def _chk_qq():
    """① QQ 接入服务：Agent 进程起着 + NapCat 的 ws 端口能连上。"""
    try:
        st = get_status() or {}
        running = bool(st.get("running"))
        ws_url = str(_cfg_mod.get("napcat_ws") or "ws://127.0.0.1:3004")
        reach = False
        detail = ""
        try:
            import websocket
            ws = websocket.create_connection(ws_url, timeout=3)
            ws.close()
            reach = True
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"[:120]
        if running and reach:
            return True, "Agent 在跑，NapCat 端口也通", ws_url
        if not running and reach:
            return False, "NapCat 端口通，但 Agent 没启动（点「启动 Agent」）", ws_url
        if running and not reach:
            return False, "Agent 在跑，但 NapCat 连不上", ws_url + " · " + detail
        return False, "Agent 没启动，NapCat 也没连上", ws_url + " · " + detail
    except Exception as e:
        return False, f"检测出错：{e}", ""


def _chk_model():
    """② 模型服务：优先级链里至少有一个能用的（Key 齐、地址对）。"""
    try:
        import providers as p
        ch = p.chain()
        if not ch:
            return False, "优先级列表是空的（模型设置里加一个）", ""
        bad = [t for t in ch if t.get("missing_key")]
        first = ch[0]
        if bad:
            return False, "有 %d 条缺 Key：%s" % (len(bad), "、".join(t["label"] for t in bad[:2])), first["label"]
        return True, "第 1 顺位：%s（共 %d 条）" % (first["label"], len(ch)), first["label"]
    except Exception as e:
        return False, f"检测出错：{e}", ""


def _chk_talk(text="说一句「我上线了」，五个字以内"):
    """③ 出话：真让模型回一句，验证不是只有配置而是真能出话。

    v1.3.3 修（用户实机）：以前这里 `except Exception as e: return False, f"出错：{e}"`，
    界面上就只剩一句 `[Errno 22] Invalid argument`，既看不出是哪个地址、
    也看不出是网络还是控制台管道问题。现在走 agent.probe_utterance()，
    失败时把「最终 URL / base / 模型 / 原因 / 原始异常 / traceback / 人话提示」一起带回来。
    """
    try:
        import agent
        r = agent.probe_utterance(text)
        ok_ = bool(r.get("ok"))
        msg = str(r.get("msg") or "")
        detail = str(r.get("detail") or "")
        if ok_:
            return True, "模型回了：%s" % msg[:40].replace("\n", " "), msg[:120]
        # 失败：把诊断细节拼成一段能看懂的话（界面会显示在下面那行小字里）
        bits = [msg]
        if detail:
            bits.append(detail)
        _u = str(r.get("url") or "")
        if _u and _u not in "\n".join(bits):
            bits.append("最终 URL：" + _u)
        if r.get("model") and str(r["model"]) not in "\n".join(bits):
            bits.append("模型：" + str(r["model"]))
        if r.get("traceback") and not detail:
            bits.append(str(r["traceback"])[-600:])
        return False, "\n".join(b for b in bits if b)[:1200], ""
    except Exception as e:
        import traceback as _tb
        return False, f"出错：{type(e).__name__}: {e}\n{_tb.format_exc()[-600:]}", ""


def _chk_draw():
    """④ 出图：生图/画图是否配好（不发请求、不烧钱，只看可用性）。"""
    try:
        if not bool(_cfg_mod.get("draw_enabled", True)):
            return False, "画图功能关着（设置里打开）", ""
        import imagegen
        ok_, msg = imagegen.available()
        return bool(ok_), ("能画：" if ok_ else "不能画：") + str(msg)[:100], ""
    except Exception as e:
        return False, f"检测出错：{e}", ""


def _chk_vision():
    """⑤ 读图：视觉开关 + 当前模型支持。"""
    try:
        on = bool(_cfg_mod.get("enable_vision", True))
        if not on:
            return False, "读图关着（config: enable_vision）", ""
        import providers as p
        ch = p.chain()
        if not ch:
            return False, "还没配模型，读图也没法测", ""
        return True, "读图开着；用 %s 处理图片" % ch[0]["label"], ch[0]["label"]
    except Exception as e:
        return False, f"检测出错：{e}", ""


def _chk_feed():
    """⑥ 动态（QQ 空间）：能不能读、能不能点赞评论。"""
    try:
        if not bool(_cfg_mod.get("feed_enabled", True)):
            return False, "动态功能关着（配置 feed_enabled）", ""
        import qzone_interact as qi
        caps = qi.capabilities() if hasattr(qi, "capabilities") else {}
        if isinstance(caps, dict) and caps.get("ok") is False:
            return False, "读不到静态数据：" + str(caps.get("msg", ""))[:100], ""
        return True, "动态接口可用（读/赞/评都有实现）", ""
    except Exception as e:
        return False, f"检测出错：{e}", ""


def run_startup_checks(deep=True):
    """按 `boot_check_order` 跑一遍：QQ → 模型 → 出话 → 出图 → 读图 → 动态。

    v1.1.3：顺序从配置读（前端可拖拽/上下移），不再写死。
    deep=False 时「出话」只查配置、不真调模型（省时间/省钱）。
    """
    step_map = {
        "qq": _chk_qq,
        "model": _chk_model,
        "chat": _chk_talk,
        "image_out": _chk_draw,
        "image_in": _chk_vision,
        "qzone": _chk_feed,
    }
    names = dict(getattr(_cfg_mod, "BOOT_STEPS", {}) or {})
    order = []
    try:
        order = [str(k) for k in (_cfg_mod.get("boot_check_order") or []) if str(k).strip()]
    except Exception:
        order = []
    order = [k for k in order if k in step_map]
    for k in step_map:
        if k not in order:
            order.append(k)
    out = []
    for i, key in enumerate(order):
        fn = step_map[key]
        name = f"{i + 1}. {names.get(key) or key}"
        if key == "chat" and not deep:
            out.append({"key": key, "name": name, "ok": True,
                        "msg": "跳过（只查配置模式，不调模型）", "detail": ""})
            continue
        try:
            ok_, msg, detail = fn()
        except Exception as e:
            ok_, msg, detail = False, f"检测出错：{e}", ""
        out.append({"key": key, "name": name, "ok": bool(ok_), "msg": msg, "detail": detail})
    first_bad = next((s for s in out if not s["ok"]), None)
    if first_bad:
        summary = "卡在「%s」：%s" % (first_bad["name"], first_bad["msg"])
    else:
        summary = "全部通过，可以开始聊了"
    return {"ok": all(s["ok"] for s in out), "steps": out, "summary": summary,
            "pass_count": sum(1 for s in out if s["ok"]), "total": len(out),
            "order": order}


def get_boot_order():
    """v1.1.3：启动判定顺序（前端渲染序号 + 拖拽用）。"""
    names = dict(getattr(_cfg_mod, "BOOT_STEPS", {}) or {})
    try:
        cur = [str(k) for k in (_cfg_mod.get("boot_check_order") or []) if str(k).strip()]
    except Exception:
        cur = []
    cur = [k for k in cur if k in names]
    for k in names:
        if k not in cur:
            cur.append(k)
    return {"ok": True, "order": cur, "steps": [{"key": k, "name": names.get(k, k)} for k in cur]}


def save_boot_order(order):
    """保存启动判定顺序（只认认识的步骤，缺的自动补后面）。"""
    if not isinstance(order, (list, tuple)):
        return {"ok": False, "msg": "order 需要数组"}
    ok, msg, v = _cfg_mod.set_checked("boot_check_order", list(order))
    return {"ok": bool(ok), "msg": msg, "order": v or []}


def get_startup_order():
    """v1.1.2 #32：启动检测顺序（没存过就给默认顺序）。"""
    names = {"qq": "QQ 接入服务", "model": "模型服务", "talk": "出话测试",
             "draw": "出图", "vision": "读图", "feed": "动态读写"}
    try:
        cur = [str(k) for k in (_cfg_mod.get("startup_order") or []) if str(k).strip()]
    except Exception:
        cur = []
    cur = [k for k in cur if k in names]
    for k in names:
        if k not in cur:
            cur.append(k)
    return {"ok": True, "order": cur,
            "items": [{"key": k, "name": names[k]} for k in cur]}


def save_startup_order(keys=None):
    """保存启动检测顺序（只收认识的 key）。"""
    names = {"qq", "model", "talk", "draw", "vision", "feed"}
    clean = []
    for k in (keys or []):
        k = str(k).strip()
        if k in names and k not in clean:
            clean.append(k)
    try:
        ok, msg, _v = _cfg_mod.set_checked("startup_order", clean)
        return {"ok": bool(ok), "msg": msg or f"顺序已保存（{len(clean)} 项）"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def cleanup_check_traces():
    """清掉检测过程中留下的东西：测试图、测试学习条目、测试用量记录。"""
    removed = {"file": 0, "learn": 0, "usage": 0}
    try:
        import imagegen
        out = os.path.abspath(imagegen.out_dir())
        for p in (imagegen.list_generated(limit=200) or []):
            try:
                if os.path.basename(p).startswith(("测试", "startup", "画图 测试")) and \
                        os.path.abspath(p).startswith(out + os.sep):
                    os.remove(p)
                    removed["file"] += 1
            except Exception:
                pass
    except Exception:
        pass
    try:
        import learn
        for it in (learn.list_items(limit=500) or []):
            t = str(it.get("title") or "")
            if t.startswith(("测试", "启动检测")):
                if learn.delete(it["id"]).get("ok"):
                    removed["learn"] += 1
    except Exception:
        pass
    try:
        import usage
        m = _mem_conn()
        try:
            n = usage.delete_by_detail(m, "启动检测")
        finally:
            try:
                m.close()
            except Exception:
                pass
        removed["usage"] = int(n or 0)
    except Exception:
        pass
    msg = "清掉了 %d 张测试图、%d 条学习记录、%d 条用量记录" % (
        removed["file"], removed["learn"], removed["usage"])
    return {"ok": True, "msg": msg, "removed": removed}


# ==================== v1.1.3 任务十三：系统自检（一键体检） ====================
def system_selfcheck():
    """把「数据库 / API / 依赖库 / 关键文件」逐个查一遍，输出一份给人看的报告。

    只做只读检查：不写库、不发消息、不调模型（调用模型的那套在「启动顺序判定」里）。
    每一项都是独立的 try，任何一项炸掉都不影响其余项。
    """
    items = []

    def _add(name, ok, msg, hint=""):
        items.append({"name": name, "ok": bool(ok), "msg": str(msg)[:300], "hint": str(hint)[:300]})

    # ① 数据库
    try:
        m = _mem_conn()
        try:
            n_msg = 0
            n_perm = 0
            try:
                n_msg = int(m.count_messages() or 0)
            except Exception:
                pass
            try:
                n_perm = int(m.get_perm_count() or 0)
            except Exception:
                pass
            _add("数据库（记忆）", True, f"可读写；会话消息 {n_msg} 条，永久记忆 {n_perm} 条")
        finally:
            try:
                m.close()
            except Exception:
                pass
    except Exception as e:
        _add("数据库（记忆）", False, f"{type(e).__name__}: {e}", "检查 data/memory.db 是否被占用或损坏")

    # ② 模型 API 配置
    try:
        import providers as _pv
        cur = _pv.resolve()
        if cur.get("ok"):
            _add("模型配置", True, f"{cur.get('label') or cur.get('model')}（{'云端' if cur.get('is_cloud') else '本地'}）")
        else:
            _add("模型配置", False, cur.get("missing_key") or cur.get("msg") or "配置不完整",
                 "去「模型设置」填地址/选模型；云端还要填 Key")
    except Exception as e:
        _add("模型配置", False, f"{type(e).__name__}: {e}")

    # ③ 模型端口（只测本地监听，不发请求）
    try:
        import providers as _pv
        cur = _pv.resolve()
        base = str(cur.get("base") or "")
        if base:
            alive = _endpoint_alive(base)
            _add("模型服务端口", alive, ("端口在监听" if alive else "端口没开"),
                 "" if alive else "本地模型要先启动（LM Studio/Ollama），云端忽略这条")
    except Exception as e:
        _add("模型服务端口", False, f"{type(e).__name__}: {e}")

    # ④ NapCat / QQ 接入
    # v1.3.2（QA BUG-12）：以前这里用 probe(deep=False) 只看 3003/3004 通不通，
    # 于是「已装好、进程在跑、只是还没扫码（端口没开）」被判定成「没找到 NapCat」，
    # 自检于是天天红灯。现在跟「QQ 登录状态」复用同一套判据：装了就算 ok，
    # 没在跑/没登录只在 msg 里说清楚，hint 才给可操作建议。
    try:
        import napcat_mgr
        pr = napcat_mgr.probe(deep=True)
        st = str(pr.get("state") or "")
        installed = (st != "not_installed")
        procs = int(pr.get("proc_count") or 0)
        if procs > 2:
            _add("QQ 接入（NapCat）", False,
                 f"检测到 {procs} 个 NapCat 进程（重复开了），它们会抢端口",
                 "设置 → QQ 接入 点「重新检测」，会自动清掉多余实例")
        elif st == "running":
            _add("QQ 接入（NapCat）", True, "在跑，HTTP 与 WebSocket 都通（QQ 已登录）")
        elif st == "no_ws":
            # v1.3.3（用户实机）：3000 通、WS 不通时以前算「绿灯」，可消息根本收不到
            _add("QQ 接入（NapCat）", False,
                 "NapCat 在跑、3000 通，但 WebSocket 连不上 —— QQ 消息收不到",
                 "NapCat WebUI →「网络配置 → WebSocket 服务端」确认已启用，"
                 "端口和「设置 → QQ 接入」里的 WS 地址一致")
        elif st == "zombie":
            _add("QQ 接入（NapCat）", False,
                 "NapCat 进程假死（端口全关、也没有二维码）",
                 "设置 → QQ 接入 点「重新检测」，会自动清掉并重开")
        elif st in ("waiting_scan", "process_no_port"):
            _add("QQ 接入（NapCat）", True,
                 "已启动，正在等扫码登录（扫码后端口才会开）"
                 if st == "waiting_scan" else "刚启动，端口还没开（等几秒再检测）",
                 "扫完码 3003/3004 会自动起来")
        elif st == "port_taken":
            _add("QQ 接入（NapCat）", False, "3003 端口被别的程序占用了",
                 "关掉占端口的程序，或给 NapCat 换端口")
        elif installed:
            _add("QQ 接入（NapCat）", True,
                 f"已安装（{pr.get('dir') or '目录已识别'}），当前没在运行",
                 "要用 QQ 就点「启动 NapCat」并扫码登录；只聊天可以不管")
        else:
            _add("QQ 接入（NapCat）", False, "没找到 NapCat",
                 "设置 → QQ 接入 里指定目录，或点一键安装（内置离线包）")
    except Exception as e:
        _add("QQ 接入（NapCat）", False, f"{type(e).__name__}: {e}")

    # ⑤ 依赖库
    # v1.3.2（QA BUG-12）：pystray / webview 是 V1.1.x 那套 pywebview 桌面壳的依赖，
    # V1.2.0 起界面换成 Tauri、托盘由 Rust 管，这两个包本来就不该再检查 ——
    # 以前把它们算进「必须齐全」，打包版一开自检就是红灯。
    deps = [("requests", "HTTP 请求"), ("websocket", "NapCat 长连接"),
            ("PIL", "图片处理"), ("psutil", "硬件快路")]
    optional_deps = [("pystray", "托盘"), ("webview", "旧版桌面界面")]
    missing = []
    for mod, why in deps:
        try:
            __import__(mod)
        except Exception:
            missing.append(f"{mod}（{why}）")
    opt_missing = []
    for mod, why in optional_deps:
        try:
            __import__(mod)
        except Exception:
            opt_missing.append(f"{mod}（{why}）")
    if missing:
        _add("依赖库", False, "缺 " + "、".join(missing),
             "缺的库打包版一般自带；源码版 pip install 一下")
    elif opt_missing:
        _add("依赖库", True,
             f"必需齐全（{len(deps)} 项）；可选未装：{'、'.join(opt_missing)}",
             "可选库只跟老版 pywebview 桌面壳有关，Tauri 版用不到，忽略即可")
    else:
        _add("依赖库", True, "齐全（含可选项）")

    # ⑥ 关键文件
    try:
        import personas_store
        cards = personas_store.list_cards() or []
        name = str(personas_store.active_name() or "").strip()
        _add("人设文件", bool(cards),
             f"共 {len(cards)} 张人设卡，当前：{name or '默认'}",
             "" if cards else "人设库是空的，去「人设编辑」新建一个")
    except Exception as e:
        _add("人设文件", False, f"{type(e).__name__}: {e}")

    # ⑦ 学习 / 插件
    try:
        import learn as _learn
        st = _learn.stats()
        _add("学习库", True, f"{st.get('total', 0)} 条（启用 {st.get('enabled', 0)}）")
    except Exception as e:
        _add("学习库", False, f"{type(e).__name__}: {e}")
    try:
        import plugins as _pl
        lst = _pl.list_plugins()
        on = sum(1 for x in lst if x.get("enabled"))
        _add("插件", True, f"{on}/{len(lst)} 个启用")
    except Exception as e:
        _add("插件", False, f"{type(e).__name__}: {e}")

    bad = [x for x in items if not x["ok"]]
    lines = []
    for i, x in enumerate(items, 1):
        lines.append(f"{i}. {'✅' if x['ok'] else '❌'} {x['name']}：{x['msg']}"
                     + (f"  ⤷ {x['hint']}" if x["hint"] and not x["ok"] else ""))
    report = "\n".join(lines)
    report += "\n\n" + ("全部正常。" if not bad else f"有 {len(bad)} 项要处理（上面标 ❌ 的）。")
    return {"ok": not bad, "items": items, "report": report,
            "pass_count": len(items) - len(bad), "total": len(items)}


# ==================== v1.1.3 任务四：情感中枢 ====================
def emotion_center():
    """给「情感中枢」页：预设、开关、超时、最近的决策日志。"""
    try:
        import emotion_engine as ee
    except Exception as e:
        return {"ok": False, "msg": f"emotion_engine 载入失败：{e}"}
    cfg = _cfg_mod.load()
    custom = cfg.get("emotion_preset_prompts") or {}
    if not isinstance(custom, dict):
        custom = {}
    prompts = {}
    for p in ee.presets():
        eff, is_custom = ee.preset_prompt(p["key"])
        prompts[p["key"]] = {"text": eff, "custom": bool(is_custom)}
    return {
        "ok": True,
        "enabled": bool(cfg.get("emotion_engine_enabled", True)),
        # v1.8.12（任务三）：仅本地 / 仅大模型 / 本地+大模型
        "mode": str(cfg.get("emotion_mode") or "both"),
        "modes": [{"key": "local", "cn": "仅本地（极速）"},
            {"key": "model", "cn": "仅大模型（深判）"},
            {"key": "both", "cn": "本地+大模型（平衡）"}],
        "profile_enabled": bool(cfg.get("emotion_profile_enabled", True)),
        "preset": str(cfg.get("emotion_presets") or ee.DEFAULT_PRESET),
        "presets": ee.presets(),
        "timeout": cfg.get("emotion_engine_timeout", 0.9),
        "log": ee.log_rows(limit=80),
        "stats": ee.stats(),
        "preset_cn": ee.preset_cn(cfg.get("emotion_presets")),
        # V1.1.6 补丁 3（m08875 ⑦）：预设口径本地可改
        "preset_prompts": prompts,
    }


def emotion_profile(user_id=""):
    """看某个人的情感画像（设置页「情感中枢」里能翻）。"""
    try:
        import emoprofile as ep
    except Exception as e:
        return {"ok": False, "msg": f"emoprofile 载入失败：{e}"}
    uid = str(user_id or "").strip()
    if not uid:
        return {"ok": True, "profiles": ep.all_profiles(50), "profile": {}}
    return {"ok": True, "profile": ep.load(uid), "user_id": uid}


def emotion_profile_save(user_id="", traits=None, landmines=None, expressions=None,
                         notes=None, mood_bias=None):
    """手工校正（用户说「她理解错了」时可以改）。"""
    try:
        import emoprofile as ep
    except Exception as e:
        return {"ok": False, "msg": f"emoprofile 载入失败：{e}"}
    uid = str(user_id or "").strip()
    if not uid:
        return {"ok": False, "msg": "先说是哪个人（user_id）"}
    patch = {}
    for k, v in (("traits", traits), ("landmines", landmines),
                 ("expressions", expressions), ("notes", notes), ("mood_bias", mood_bias)):
        if v is not None:
            patch[k] = str(v)
    r = ep.save(uid, patch)
    r["profile"] = ep.load(uid)
    return r


def emotion_profile_clear(user_id=""):
    try:
        import emoprofile as ep
    except Exception as e:
        return {"ok": False, "msg": f"emoprofile 载入失败：{e}"}
    return ep.clear(user_id)


def emotion_profile_reflect(user_id=""):
    """现在就让模型看一眼聊天记录，提炼一次画像。"""
    try:
        import emoprofile as ep
    except Exception as e:
        return {"ok": False, "msg": f"emoprofile 载入失败：{e}"}
    try:
        _kg_ensure_model()
    except Exception:
        pass
    ask = _profile_ask
    uid = str(user_id or "").strip()
    mem_obj = None
    try:
        import memory as _mem_mod
        mem_obj = _mem_mod.Memory()
    except Exception as e:
        return {"ok": False, "msg": f"记忆库打不开：{e}"}
    if uid:
        r = ep.build_profile(mem_obj, uid, ask)
        r["profile"] = ep.load(uid)
        return r
    r = ep.reflect_today(mem_obj, ask=ask, log=lambda m, lv="INFO": None)
    return r


def _profile_ask(prompt):
    """给 emoprofile 用的模型调用（不抛异常，失败返回空串）。"""
    try:
        import diary as _dm
        if getattr(_dm, "has_model", lambda: False)():
            return _dm._call_model(prompt, system=None, max_tokens=600) or ""
    except Exception:
        pass
    try:
        import agent as _ag
        return _ag.ask_model_raw(prompt, max_tokens=600, temperature=0.4,
                                 effort="none", tag="/情感画像") or ""
    except Exception:
        return ""


def save_emotion_center(preset=None, enabled=None, timeout=None, prompt_text=None,
            mode=None, profile_enabled=None):
    """改预设 / 开关 / 超时 / 当前预设的判断口径。传 None 表示不动。"""
    patch = {}
    try:
        if preset is not None:
            import emotion_engine as ee
            keys = [p["key"] for p in ee.presets()]
            p = str(preset or "").strip().lower()
            if p not in keys:
                return {"ok": False, "msg": f"预设只能是 {'/'.join(keys)}"}
            patch["emotion_presets"] = p
        if enabled is not None:
            patch["emotion_engine_enabled"] = bool(enabled)
        if mode is not None:
            _m = str(mode or "").strip().lower()
            if _m not in ("local", "model", "both"):
                return {"ok": False, "msg": "mode 只能是 local / model / both"}
            patch["emotion_mode"] = _m
        if profile_enabled is not None:
            patch["emotion_profile_enabled"] = bool(profile_enabled)
        if timeout is not None:
            try:
                t = float(timeout)
            except Exception:
                return {"ok": False, "msg": "超时得是数字"}
            patch["emotion_engine_timeout"] = max(0.2, min(10.0, t))
        if prompt_text is not None:
            # 只改「当前选中的预设」的口径；传空串 = 恢复内置
            import emotion_engine as ee
            cur = str(patch.get("emotion_presets")
                      or _cfg_mod.get("emotion_presets") or ee.DEFAULT_PRESET).strip().lower()
            saved = dict(_cfg_mod.get("emotion_preset_prompts") or {})
            text = str(prompt_text or "").strip()
            if text:
                saved[cur] = text
            else:
                saved.pop(cur, None)
            patch["emotion_preset_prompts"] = saved
        if not patch:
            return {"ok": False, "msg": "没有要改的东西"}
        for k, v in patch.items():
            ok, msg, _v = _cfg_mod.set_checked(k, v)
            if not ok:
                return {"ok": False, "msg": msg}
        return {"ok": True, "msg": "已保存（下一句话就按新设置走）", "patch": patch}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def get_learn_prompt():
    """V1.1.6 补丁 3（m08875 ⑦）：看学习提炼用的提示词（含是否自定义）。"""
    try:
        import learn as learn_mod
        eff = learn_mod.prompt_text(name="小鲸鱼", chat="（这里会放最近的聊天记录）")
        custom = str(_cfg_mod.get("learn_prompt") or "").strip()
        return {"ok": True, "text": eff, "custom": bool(custom),
                "raw": custom, "builtin": bool(not custom)}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def save_learn_prompt(text=""):
    ok, msg, _v = _cfg_mod.set_checked("learn_prompt", str(text or ""))
    return {"ok": bool(ok), "msg": msg}


# ==================== v1.1.5.1：情感判断记录（跨进程可见） ====================
def clear_emotion_log():
    """清空「情感中枢」的判断记录（表里的，不是内存）。"""
    try:
        import emotion_engine as ee
        ee.clear_log()
        return {"ok": True, "msg": "记录已清空"}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v1.1.2 #22：网页启动版（在窗口里一键开/关） ====================
_WEB = {"srv": None, "thread": None, "url": ""}


def web_state():
    """网页版状态：开关、端口、当前地址、是否在跑。"""
    import config as _cfg_mod
    try:
        port = int(_cfg_mod.get("web_port") or 8765)
    except Exception:
        port = 8765
    running = _WEB.get("srv") is not None
    url = _WEB.get("url") or ""
    if not url:
        url = "http://127.0.0.1:%d/" % port
    return {
        "ok": True,
        "enabled": bool(_cfg_mod.get("web_enabled", False)),
        "port": port,
        "running": running,
        "url": url,
        # QA 终审必修 2：分发包里网页版已下架（没有 index.html，只剩个能被
        # 任意网页调用的 /rpc 口子）。前端按这个字段把整块设置藏起来。
        "supported": not IS_FROZEN,
        "msg": ("网页版正在运行，浏览器打开上面的地址就行"
                if running else
                ("打包版不支持网页版：界面在安装包的前端资源里，请用窗口版"
                 if IS_FROZEN else
                 "网页版没开。点了「开启网页版」就能用浏览器打开")),
    }


def start_web(port=0):
    """起网页版（后台线程，不挡窗口）。返回 {ok,msg,url}。"""
    if IS_FROZEN:
        # QA 终审必修 2：打包版直接拒绝，别让这个口子在分发包里活着
        return {"ok": False, "url": "",
                "msg": "打包版不支持网页版（界面在安装包的前端资源里，网页版只对源码运行有效）；请用窗口版"}
    if _WEB.get("srv") is not None:
        return {"ok": True, "msg": "网页版已经在跑了", "url": _WEB.get("url") or ""}
    import config as _cfg_mod
    try:
        if port:
            port = max(1024, min(65535, int(port)))
            _cfg_mod.set_checked("web_port", port)
        else:
            port = int(_cfg_mod.get("web_port") or 8765)
    except Exception:
        port = 8765
    try:
        import web_server as ws
        srv = ws.ThreadingHTTPServer(("127.0.0.1", port), ws.Handler)
        srv.api = ws._api()
        th = threading.Thread(target=srv.serve_forever, daemon=True, name="web-console")
        th.start()
        _WEB["srv"], _WEB["thread"] = srv, th
        _WEB["url"] = "http://127.0.0.1:%d/" % port
        try:
            _cfg_mod.set_checked("web_enabled", True)
        except Exception:
            pass
        return {"ok": True, "msg": "网页版已开启（只有本机能访问）", "url": _WEB["url"]}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "url": ""}


def stop_web():
    srv = _WEB.get("srv")
    if srv is None:
        return {"ok": True, "msg": "网页版本来就没开"}
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:
        pass
    _WEB["srv"], _WEB["thread"], _WEB["url"] = None, None, ""
    try:
        import config as _cfg_mod
        _cfg_mod.set_checked("web_enabled", False)
    except Exception:
        pass
    return {"ok": True, "msg": "网页版已关闭"}


def open_web():
    st = web_state()
    url = st.get("url") or ""
    if not url:
        return {"ok": False, "msg": "还没有地址"}
    return shell_open_url(url)


def _endpoint_alive(base, timeout=0.6):
    """base 形如 http://127.0.0.1:1234/v1 —— 只看那个端口有没有人在听。

    v1.1.3：一键启动前先静默判断，已经在跑就别重复拉（重复拉 LM Studio
    会多开一个实例，抢显存、抢端口）。
    """
    try:
        from urllib.parse import urlparse
        import socket as _sk
        u = urlparse(str(base or ""))
        host = u.hostname or "127.0.0.1"
        port = int(u.port or (443 if u.scheme == "https" else 80))
        with _sk.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def start_everything():
    """v1.1.1：一键把前置都拉起来 —— NapCat（QQ 接入）→ 模型服务 → Agent。

    顺序是有讲究的：Agent 一起来就会连 NapCat 的 ws，模型服务早于 Agent
    起好，第一条消息才不至于撞上「模型地址还没监听」。
    每一步失败都不中断，最后一次性把结果告诉你哪里没起来。
    """
    steps = []

    def _step(name, fn):
        try:
            r = fn()
            if isinstance(r, tuple) and len(r) == 2:
                ok, msg = bool(r[0]), str(r[1] or "")
            elif isinstance(r, dict):
                ok, msg = bool(r.get("ok", True)), str(r.get("msg") or "")
            else:
                ok, msg = bool(r), ""
            steps.append({"name": name, "ok": ok,
                          "msg": msg[:160] or ("已启动" if ok else "没起来")})
            return ok
        except Exception as e:
            steps.append({"name": name, "ok": False, "msg": f"{type(e).__name__}: {e}"[:160]})
            return False

    # ① NapCat / QQ 接入
    def _nap():
        try:
            import napcat_mgr
            pr = napcat_mgr.probe(deep=True)
            if pr.get("http_alive") or pr.get("ws_open"):
                _append_log("[启动判定] QQ已运行，跳过启动")
                return {"ok": True, "msg": "NapCat 已经在跑（跳过启动）"}
            r = napcat_mgr.launch(wait=12)
            # napcat_mgr.launch 返回的是 (ok, msg) 元组，不是 dict —— 这里必须分开处理
            if isinstance(r, tuple) and len(r) == 2:
                ok, msg = bool(r[0]), str(r[1] or "")
                if ok:
                    return {"ok": True, "msg": msg}
                return {"ok": False, "msg": msg or "NapCat 没起来"}
            if isinstance(r, dict) and r.get("ok"):
                return r
            import agent as _a
            h = _a.start_napcat(wait=12) if hasattr(_a, "start_napcat") else None
            if isinstance(h, dict) and h.get("ok"):
                return h
            return {"ok": False, "msg": (r or {}).get("msg") if isinstance(r, dict) else "没找到 NapCat，可能还没装"}
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}

    _step("QQ 接入（NapCat）", _nap)

    # ② 模型服务（本地才需要拉；云端 API 不用）
    def _model():
        try:
            cfg = _cfg_mod.load()
            eng = str(cfg.get("lm_engine") or "lmstudio")
            if not bool(cfg.get("local_enabled", True)):
                return {"ok": True, "msg": "本地模型关着，直接用云端 API"}
            base = str(cfg.get("lm_base") or "").strip()
            if not base:
                try:
                    import providers as _pv
                    base = str(_pv.resolve().get("base") or "")
                except Exception:
                    base = ""
            if base and _endpoint_alive(base):
                _append_log(f"[启动判定] 模型服务已在监听 {base}，跳过启动")
                return {"ok": True, "msg": "模型服务已在监听（跳过启动）"}
            return start_local_server(eng)
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}

    _step("模型服务（本地）", _model)

    def _agent_step():
        if _is_running():
            _append_log("[启动判定] Agent 已在运行，跳过启动")
            return {"ok": True, "msg": "Agent 已在运行（跳过启动）"}
        return start_agent()

    _step("Agent", _agent_step)
    bad = [s for s in steps if not s["ok"]]
    return {"ok": not bad, "steps": steps,
            "msg": "都起来了" if not bad else "有 %d 步没起来：%s" % (
                len(bad), "、".join(s["name"] for s in bad))}


_IMAGEGEN_ALIAS = {    # 前端习惯用短键，config.set_checked 只认长键 —— 这里做一次翻译。
    # 曾经把这张表弄丢过，结果「启用生图」开关点了完全没反应（静默全部失败）。
    "enabled": "imagegen_enabled",
    "use_current": "imagegen_use_current",
    "base": "imagegen_base",
    "model": "imagegen_model",
    "api_key": "imagegen_api_key",
    "key": "imagegen_api_key",
    "size": "imagegen_size",
    "style": "imagegen_style",
    "timeout": "imagegen_timeout",
    "via": "imagegen_via",
}


# ==================== v1.8.12：真唱 / 自画像 / 表情包 / 头像 ====================
def sing_status():
    """唱歌引擎现在配成什么样、能不能用。"""
    try:
        import sing as _s
        d = _s.sing_status()
        d["ok"] = True
        return d
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}


def save_sing(patch):
    """保存唱歌相关设置（sing_engine / sing_cloud_url / ...）。"""
    try:
        import config as cfg_mod
        patch = patch or {}
        if not isinstance(patch, dict):
            return {"ok": False, "msg": "参数格式不对"}
        ok_all, bad, changed = True, [], []
        for k, v in patch.items():
            ok, msg, _c = cfg_mod.set_checked(str(k), v)
            if not ok:
                ok_all = False
                bad.append(msg)
            else:
                changed.append(str(k))
        d = sing_status()
        d["ok"] = bool(ok_all and d.get("ok", True))
        d["msg"] = ("；".join(bad) if bad else
            ("已保存：%s" % "、".join(changed) if changed else "没有要保存的项"))
        return d
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}


_SING_DEMO = ["你行不行啊", "成长这件事急不来", "音乐响起来的时候"]


def sing_test(name="", lines=None):
    """试唱一段给设置页试听。给了 lines 就用它（不联网），否则按歌名搜。"""
    try:
        import sing as _s
        use = [str(x) for x in (lines or []) if str(x).strip()]
        if not use and not str(name or "").strip():
            use = list(_SING_DEMO)
        # v1.8.15：这里问的是「Agent 能不能联网搜索」，键是 agent_web。
        # 以前误用了 web_enabled（那是网页版服务的开关），用户开了「网上冲浪」
        # 试唱仍然被拦，而关着网页服务的人反而能搜。
        if not use and not bool(_cfg_mod.get("agent_web", True)):
            return {"ok": False,
                    "msg": "网络冲浪关着，搜不了歌词。给几行歌词，或去设置里打开「网上冲浪」再试",
                    "path": "", "engine": "", "lines": [], "fix": []}
        r = _s.sing(str(name or ""), lines=use or None)
        return {"ok": bool(r.get("ok")), "msg": r.get("msg") or r.get("note") or "",
            "path": r.get("path") or "", "engine": r.get("engine") or "",
            "lines": r.get("lines") or [], "fix": r.get("fix") or []}
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e), "path": ""}


def polyphone_preview(text=""):
    """设置页用：看一眼多音字会被怎么改。"""
    try:
        import sing as _s
        fixed, hits = _s.polyphone_fix(str(text or ""))
        return {"ok": True, "text": fixed, "hits": hits, "rules": len(_s.POLY_RULES)}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def draw_self(extra=""):
    """画一张自画像（用 appearance_desc / self_appearance 的描述）。

    v1.9.15：这里 strict=False —— 是用户自己在界面上点「画」的，允许按内置默认描述出图；
    聊天里由模型自己调工具时才是 strict（没有形象依据就不许凭空画一个人）。
    """
    try:
        import selfmod
        return selfmod.draw_self(extra, strict=False)
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e), "path": ""}


def set_avatar(path=""):
    """换 QQ 头像。不传路径就弹文件选择框。"""
    try:
        import selfmod
        if not str(path or "").strip():
            _st, _p = _pick_file_ex(save=False, exts=["png", "jpg", "jpeg"])
            if not _p:
                return {"ok": False, "msg": "没选图片"}
            path = _p
        return selfmod.set_avatar(path)
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}


def avatar_status():
    try:
        import selfmod
        return selfmod.avatar_status()
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def pick_voice_file():
    """挑一个参考音频。"""
    try:
        _st, _p = _pick_file_ex(save=False, exts=["wav", "mp3", "m4a", "flac", "ogg"])
        return {"ok": bool(_p), "path": _p or "", "msg": "" if _p else "没选文件"}
    except Exception as e:
        return {"ok": False, "path": "", "msg": str(e)}


def upload_voice(path="", name=""):
    """把参考音频存进 runtime/voices/（自定义音色要用）。"""
    try:
        import selfmod
        if not str(path or "").strip():
            _r = pick_voice_file()
            if not _r.get("ok"):
                return {"ok": False, "msg": _r.get("msg") or "没选文件"}
            path = _r["path"]
        r = selfmod.save_voice(path, name)
        if r.get("ok"):
            r.update(selfmod.list_voices())
        return r
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}


def list_voices():
    try:
        import selfmod
        return selfmod.list_voices()
    except Exception as e:
        return {"ok": False, "msg": str(e), "items": []}


def sticker_stats():
    """表情包现在有多少张（偷来的也在里面）。"""
    try:
        import selfmod
        return {"ok": True, "count": selfmod.sticker_count(), "dir": selfmod.sticker_dir()}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def save_imagegen(patch):
    """保存生图相关配置。"""
    try:
        import config as cfg_mod
        patch = patch or {}
        if not isinstance(patch, dict):
            return {"ok": False, "msg": "参数格式不对"}
        ok_all, bad, changed = True, [], []
        for k, v in patch.items():
            key = _IMAGEGEN_ALIAS.get(str(k), str(k))
            ok, msg, _c = cfg_mod.set_checked(key, v)
            if not ok:
                ok_all = False
                bad.append(msg)
            else:
                changed.append(key)
        if not changed and not bad:
            return _with(get_imagegen_status(), False, "没有要保存的项")
        tail = "；".join(bad) if bad else "生图设置已保存（%s）" % "、".join(changed)
        # v1.1.5：用户点一下开关之后，最想知道的是「现在到底能不能出图」，
        # 只说「已保存」等于没说 —— 把可用性结论直接拼在消息里。
        if "imagegen_enabled" in changed:
            try:
                import imagegen as _ig
                ok_ig, msg_ig = _ig.available()
                tail += ("　现在就能出图：" + msg_ig) if ok_ig else ("　但还不能出图：" + msg_ig)
            except Exception as _e:
                tail += "　（可用性检查失败：%s）" % _e
        return _with(get_imagegen_status(), ok_all, tail)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def test_imagegen(prompt="一只橘猫在键盘上睡觉，卡通风格"):
    """试着生成一张，验证 key/模型/地址是否可用。"""
    try:
        import imagegen
        ok, msg = imagegen.available()
        if not ok:
            return _with(get_imagegen_status(), False, msg)
        r = imagegen.generate(str(prompt or "一只橘猫"), tag="测试生图")
        return _with(get_imagegen_status(), bool(r.get("ok")),
                     r.get("msg", ""), path=r.get("path", ""))
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_generated_dir():
    d = os.path.join(DATA_DIR, "generated")
    try:
        import imagegen
        d = imagegen.out_dir() or d
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return _open_result(d, "已打开生图目录")


def gen_sticker_now(text="生气"):
    """手动生一张表情包（验证用）。"""
    try:
        import agent as agent_mod
        import imagegen
        ok, msg = imagegen.available()
        if not ok:
            return {"ok": False, "msg": msg}
        emos = []
        try:
            import stickers
            emos = stickers.detect_emotions(str(text or ""))
        except Exception:
            pass
        prompt = imagegen.sticker_prompt(emos, agent_mod.active_name(), extra=str(text))
        r = imagegen.generate(prompt, tag="表情包 " + "/".join(emos)[:20])
        return _with(get_imagegen_status(), bool(r.get("ok")), r.get("msg", ""),
                     path=r.get("path", ""), emotions=emos)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v6：用量明细 ====================
def get_usage_details(limit=80, kind="", day=""):
    try:
        import usage
        m = _mem_conn()
        try:
            items = usage.recent_details(m, limit=int(limit),
                                         day=(day or None),
                                         kind=(kind or None))
        finally:
            try:
                m.close()
            except Exception:
                pass
        return {"ok": True, "items": items}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


# ==================== v7：模型设置（本地/云端双开关 + 优先级链） ====================
def _prov():
    import providers
    return providers


def get_plugins():
    """插件清单：内置能力 + 用户插件（附加载错误）。"""
    try:
        import plugins as plug_mod
        items = plug_mod.list_plugins()
        return {"ok": True, "items": items, "dir": plug_mod.plugin_dir(),
                "count": len(items), "user_count": sum(1 for x in items if not x.get("builtin"))}
    except Exception as e:
        return {"ok": False, "msg": f"读取插件失败：{e}", "items": [], "dir": ""}


def set_plugin_enabled(name="", on=True):
    """开关一个插件。"""
    try:
        import plugins as plug_mod
        ok, msg = plug_mod.set_enabled(name, bool(on))
        return {"ok": ok, "msg": msg, "items": plug_mod.list_plugins()}
    except Exception as e:
        return {"ok": False, "msg": f"操作失败：{e}", "items": []}


def open_plugin_dir():
    """打开用户插件目录。"""
    try:
        import plugins as plug_mod
        return _open_result(plug_mod.plugin_dir(), "已打开插件目录")
    except Exception as e:
        return {"ok": False, "msg": f"打不开：{e}"}


def run_plugin(name=""):
    """跑一次用户插件的 run()。"""
    try:
        import plugins as plug_mod
        ok, msg = plug_mod.run_plugin(name)
        return {"ok": ok, "msg": msg}
    except Exception as e:
        return {"ok": False, "msg": f"跑失败：{e}"}


def reload_plugins():
    """重新扫描插件目录（新放的不用重启）。"""
    try:
        import plugins as plug_mod
        items = plug_mod.reload_plugins()
        done = plug_mod.setup_all()
        msg = f"重新扫描完成，共 {len(items)} 项"
        if done:
            msg += f"；已载入 {len(done)} 个"
        return {"ok": True, "msg": msg, "items": items}
    except Exception as e:
        return {"ok": False, "msg": f"扫描失败：{e}", "items": []}


def get_feature_models():
    """按功能分工：功能清单 + 当前配置 + 可选提供方。"""
    import providers as prov_mod
    try:
        feats = prov_mod.list_features()
        provs = prov_mod.list_providers()
    except Exception as e:
        return {"ok": False, "msg": f"读取失败：{e}", "features": [], "providers": []}
    return {
        "ok": True,
        "features": feats,
        "providers": provs,
        "summary": prov_mod.feature_summary(),
        "msg": prov_mod.feature_summary(),
    }


def save_feature_model(feature="", provider="", base="", model="", api_key="", clear=False):
    """保存/清除某个功能的专用模型。"""
    import providers as prov_mod
    ok, msg = prov_mod.set_feature_model(
        feature, provider=provider, base=base, model=model,
        api_key=(None if not str(api_key or "").strip() else api_key),
        clear=bool(clear))
    if ok:
        try:
            import agent as agent_mod
            agent_mod.refresh_system_prompt()
        except Exception:
            pass
        try:
            _append_log(f"[分工] {msg}")
        except Exception:
            pass
    return {"ok": ok, "msg": msg, "features": (get_feature_models().get("features") or [])}


def get_model_settings():
    """模型设置页要的全部数据。"""
    try:
        P = _prov()
        cfg = _cfg_mod.load()
        ch = P.raw_chain()
        eff = []
        try:
            eff = [t.get("label") for t in P.chain()]
        except Exception:
            pass
        eng = P.local_engine(cfg)
        return {
            "ok": True,
            "local_enabled": bool(cfg.get("local_enabled", True)),
            "api_enabled": bool(cfg.get("api_enabled", False)),
            "failover": bool(cfg.get("model_failover", True)),
            "max_chain": getattr(P, "MAX_CHAIN", 10),
            "current_model": (P.resolve() or {}).get("model", ""),
            "local": {
                "base": str(cfg.get("lm_base") or ""),
                "model": str(cfg.get("lm_model") or ""),
                # v1.1.0：本地引擎（LM Studio / Ollama），按钮文案跟着变
                "engine": eng,
                "engine_cn": _cfg_mod.LOCAL_ENGINE_CN.get(eng, "LM Studio"),
                "default_base": P.local_default_base(cfg),
                "engines": [{"key": k, "name": _cfg_mod.LOCAL_ENGINE_CN.get(k, k),
                             "base": _cfg_mod.LOCAL_ENGINE_DEFAULT_BASE.get(k, "")}
                            for k in _cfg_mod.LOCAL_ENGINES],
            },
            "api": {
                "provider": str(cfg.get("lm_provider") or "local"),
                "base": str(cfg.get("cloud_base") or ""),
                "model": str(cfg.get("cloud_model") or ""),
                "key_masked": P.mask_key(cfg.get("cloud_api_key") or ""),
                "has_key": bool(cfg.get("cloud_api_key") or ""),
            },
            "chain": ch,
            "chain_effective": eff,
            "summary": P.chain_summary(),
            "providers": P.list_providers(),
        }
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "chain": [],
                "chain_effective": [], "providers": []}


def set_local_enabled(enabled):
    ok, msg, val = _cfg_mod.set_checked("local_enabled", enabled)
    return _with({}, ok, ("本地模型已" + ("启用" if val else "停用")) if ok else msg,
                 enabled=bool(val))


def set_api_enabled(enabled):
    ok, msg, val = _cfg_mod.set_checked("api_enabled", enabled)
    return _with({}, ok, ("API 模型已" + ("启用" if val else "停用")) if ok else msg,
                 enabled=bool(val))


def set_model_failover(enabled):
    ok, msg, val = _cfg_mod.set_checked("model_failover", enabled)
    return _with({}, ok, ("已开启失败顺延" if val else "已关闭失败顺延（只用第一个）")
                 if ok else msg, enabled=bool(val))


def set_provider_key(provider, api_key):
    """把某个提供方的 Key 存进 config.provider_keys（切来切去不用重填）。"""
    try:
        pkey = str(provider or "").strip().lower()
        if not pkey:
            return {"ok": False, "msg": "没指定提供方"}
        cfg = _cfg_mod.load()
        keys = dict(cfg.get("provider_keys") or {})
        P = _prov()
        if api_key:
            # v1.1.0：落盘前先加密（DPAPI，只有本机能解开）
            keys[pkey] = P.enc_key(api_key)
        else:
            keys.pop(pkey, None)
        ok, msg, _v = _cfg_mod.set_checked("provider_keys", keys)
        return _with({}, ok, ("已保存 Key（打码：" + P.mask_key(api_key) + "）")
                     if ok and api_key else ("已清除该提供方的 Key" if ok else msg))
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def set_local_engine(engine, base=""):
    """v1.1.0：切换本地引擎（LM Studio / Ollama），默认地址与按钮文案一起变。"""
    try:
        ok, msg = _prov().set_local_engine(engine, base)
        return _with(get_model_settings(), ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def _list_models_at(base, api_key="", is_cloud=False, provider="", timeout=8):
    """在指定地址拉 /models（本地云端共用；不做任何字段判断）。"""
    import requests
    url = f"{str(base).rstrip('/')}/models"
    h = {}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    try:
        r = requests.get(url, headers=h, timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        if is_cloud:
            return {"ok": False, "models": [],
                    "msg": f"连不上 {base}（{type(e).__name__}）。检查服务地址和网络。"}
        return {"ok": False, "models": [],
                "msg": (f"连不上本地服务（{base}）。LM Studio 开着的话，"
                        f"点下面的「启动 LM Studio 服务」；或者把「API 模型」开关打开改用云端。")}
    except requests.exceptions.Timeout:
        return {"ok": False, "models": [], "msg": f"{base} 响应超时（{timeout}s）"}
    except Exception as e:
        return {"ok": False, "models": [], "msg": f"{type(e).__name__}: {e}"}

    if r.status_code in (401, 403):
        return {"ok": False, "models": [], "auth_failed": True,
                "msg": "返回 401：API Key 不对或没填。（在这个卡片的 Key 输入框里重新填一次再拉取）"}
    if r.status_code != 200:
        return {"ok": False, "models": [],
                "msg": f"HTTP {r.status_code}：{(r.text or '')[:200]}"}
    try:
        data = r.json()
    except Exception:
        return {"ok": False, "models": [], "msg": "返回不是合法 JSON（地址可能不对）"}
    ids = []
    for m in (data.get("data") or data.get("models") or []):
        mid = m.get("id") or m.get("name") if isinstance(m, dict) else str(m)
        if mid:
            ids.append(str(mid))
    # 嵌入模型不能聊天，过滤掉（但保留原名列表给用户看）
    chat = [x for x in ids if "embed" not in x.lower()]
    return {"ok": True, "models": chat, "all_ids": ids,
            "msg": (f"发现 {len(chat)} 个可用模型" if chat else "连上了，但没有可用的聊天模型"),
            "base": base}


def list_local_models():
    """只拉本地 LM Studio 的模型列表（不动云端设置）。"""
    try:
        cfg = _cfg_mod.load()
        base = str(cfg.get("lm_base") or "http://127.0.0.1:1234/v1")
        r = _list_models_at(base, "", is_cloud=False, provider="local")
        r["base"] = base
        return r
    except Exception as e:
        return {"ok": False, "models": [], "msg": f"{type(e).__name__}: {e}"}


def list_cloud_models(provider="", base="", api_key=""):
    """只拉云端提供方的模型列表。api_key 留空则用已存的。"""
    try:
        P = _prov()
        cfg = _cfg_mod.load()
        pkey = str(provider or cfg.get("lm_provider") or "").strip().lower()
        preset = P.get_provider(pkey)
        b = str(base or "").strip() or str(cfg.get("cloud_base") or "").strip() or preset["base"]
        keys = cfg.get("provider_keys") or {}
        k = (str(api_key or "").strip()
             or P.dec_key(keys.get(pkey))
             or P.dec_key(cfg.get("cloud_api_key")))
        if preset["needs_key"] and not k:
            return {"ok": False, "models": [], "need_key": True,
                    "msg": f"{preset['name']} 需要 API Key：先在下面填 Key，再点拉取"}
        r = _list_models_at(b, k, is_cloud=True, provider=pkey)
        r["provider"] = pkey
        r["provider_name"] = preset["name"]
        r["base"] = b
        return r
    except Exception as e:
        return {"ok": False, "models": [], "msg": f"{type(e).__name__}: {e}"}


def add_model_to_chain(kind, provider="", base="", model="", api_key="", front=False):
    try:
        ok, msg = _prov().add_to_chain(kind, provider=provider, base=base,
                                       model=model, api_key=api_key,
                                       front=bool(front))
        return _with(get_model_settings(), ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def update_model_in_chain(index, base="", model="", api_key=""):
    """v1.1.0：编辑优先级列表里的一项（改错字 / 换模型），不用删了重填。"""
    try:
        ok, msg = _prov().update_in_chain(index, base=base, model=model, api_key=api_key)
        return _with(get_model_settings(), ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def remove_model_from_chain(index):
    try:
        ok, msg = _prov().remove_from_chain(index)
        return _with(get_model_settings(), ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def move_model_in_chain(frm, to):
    try:
        ok, msg = _prov().move_in_chain(frm, to)
        return _with(get_model_settings(), ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def clear_model_chain():
    try:
        ok, msg = _prov().clear_chain()
        return _with(get_model_settings(), ok, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v7：主动回复（user_events）管理 ====================
def get_events(status="", limit=100):
    try:
        m = _mem_conn()
        try:
            items = m.list_events(limit=int(limit), status=(status or None))
            # 补一个"还有多久 / 逾期多久"的人话，界面直接显示
            now = time.time()
            for it in items:
                try:
                    tt = float(it.get("target_time") or 0)
                except (TypeError, ValueError):
                    tt = 0
                it["target_text"] = time.strftime("%m-%d %H:%M", time.localtime(tt)) if tt else ""
                it["delta_min"] = int((tt - now) / 60)
            pend = m.count_events("pending") if hasattr(m, "count_events") else 0
        finally:
            try:
                m.close()
            except Exception:
                pass
        return {"ok": True, "items": items, "count": len(items), "pending": pend}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def add_event_manual(user_id, desc, when_text, session_key=""):
    """手动加一条"到时主动问他"的事件。同一套 _parse_plan_time。"""
    try:
        desc = str(desc or "").strip()
        if not desc:
            return {"ok": False, "msg": "先写一句要问什么（比如「考得怎么样」）"}
        ts = _parse_plan_time(str(when_text or ""))
        if ts <= 0:
            return {"ok": False, "msg": f"时间看不懂：{when_text}（可以写 +30m / 21:30 / 明天 09:00）"}
        uid = re.sub(r"\D", "", str(user_id or ""))
        if not uid:
            return {"ok": False, "msg": "要填一个 QQ 号（问谁）"}
        key = session_key or f"private_{uid}"
        m = _mem_conn()
        try:
            eid = m.add_event(uid, desc, ts, session_key=key, dedup=False)
        finally:
            try:
                m.close()
            except Exception:
                pass
        return _with(get_events(), bool(eid),
                     "已加入主动回复列表" if eid else "没写进去（可能是重复事件）")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def update_event_event(event_id, desc=None, when_text=None):
    try:
        ts = None
        if when_text:
            ts = _parse_plan_time(str(when_text))
            if ts <= 0:
                return {"ok": False,
                        "msg": f"时间看不懂：{when_text}（可以写 +30m / 21:30 / 明天 09:00）"}
        m = _mem_conn()
        try:
            # 直接改表，避免再加一层 memory 方法
            sets, args = [], []
            if desc is not None and str(desc).strip():
                sets.append("event_desc=?")
                args.append(str(desc).strip())
            if ts:
                sets.append("target_time=?")
                args.append(float(ts))
            if not sets:
                return {"ok": False, "msg": "没有要改的内容"}
            args.append(int(event_id))
            c = m.conn.cursor()
            c.execute(f"UPDATE user_events SET {', '.join(sets)} WHERE id=?", args)
            m.conn.commit()
            n = c.rowcount
        finally:
            try:
                m.close()
            except Exception:
                pass
        return _with(get_events(), n > 0, "已保存" if n > 0 else "没找到这条")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def delete_event_item(event_id):
    try:
        m = _mem_conn()
        try:
            c = m.conn.cursor()
            c.execute("DELETE FROM user_events WHERE id=?", (int(event_id),))
            m.conn.commit()
            n = c.rowcount
        finally:
            try:
                m.close()
            except Exception:
                pass
        return _with(get_events(), n > 0, "已删除" if n > 0 else "没找到这条")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def cancel_event_item(event_id):
    try:
        m = _mem_conn()
        try:
            n = m.cancel_event(int(event_id))
        finally:
            try:
                m.close()
            except Exception:
                pass
        return _with(get_events(), bool(n),
                     "已取消（不再主动问）" if n else "没找到这条")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


# ==================== v7：临时 ⇄ 永久 互转 ====================
def get_sessions(limit=40):
    """最近有消息的会话列表（记忆页「临时记忆」左上角的会话选择器）。

    v1.1.2 新增：以前只能看最后一个会话，群里的聊天记录翻不到，
    用户需求「临时记忆里要能看到它最近在群里说了什么」。
    """
    import diary as diary_mod
    try:
        m = _mem_conn()
        try:
            rows = m.list_sessions(limit=int(limit))
        finally:
            try:
                m.close()
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "msg": f"读取会话失败：{e}", "items": []}
    bound_uids, bound_gids = set(), set()
    try:
        bound_uids = {str(x) for x in (diary_mod.bound_accounts() or [])}
    except Exception:
        pass
    try:
        gl = diary_mod.group_list() or {}
        bound_gids = {str(x) for x in (gl.get("whitelist") or [])}
    except Exception:
        pass
    out = []
    for r in rows:
        key = str(r.get("session_key") or "")
        num = key.split("_", 1)[-1] if "_" in key else key
        if key.startswith("group_"):
            kind, label = "群聊", f"群 {num}"
            bound = num in bound_gids
        elif key.startswith("private_"):
            kind, label = "私聊", f"QQ {num}"
            bound = num in bound_uids
        else:
            kind, label = "会话", num
            bound = False
        out.append({
            "key": key, "kind": kind, "label": label, "num": num,
            "bound": bound, "count": int(r.get("count") or 0),
            "last": time.strftime("%m-%d %H:%M",
                                  time.localtime(float(r.get("last_ts") or 0))),
        })
    return {"ok": True, "items": out, "msg": f"{len(out)} 个会话"}


def get_temp_messages(session_key, limit=40):
    """临时记忆列表（某会话最近的对话，可勾选转成永久）。"""
    try:
        m = _mem_conn()
        try:
            items = m.get_recent_messages(str(session_key), turns=int(limit))
        finally:
            try:
                m.close()
            except Exception:
                pass
        now = time.time()
        out = []
        for i, it in enumerate(items):
            out.append({
                "idx": i,
                "role": it.get("role") or "",
                "content": it.get("content") or "",
                "ts": float(it.get("ts") or 0),
                "time_text": time.strftime("%m-%d %H:%M",
                                           time.localtime(float(it.get("ts") or now))),
            })
        return {"ok": True, "items": out, "count": len(out), "session_key": session_key}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def promote_to_perm(user_id, texts=None, session_key="", from_message_idxs=None):
    """把选中的临时内容转成永久记忆。

    两种入口：
      ① 直接给 texts（界面里勾了几条对话，前端把正文传上来）
      ② 给 session_key + from_message_idxs（由后端自己去库里取，更准）
    """
    try:
        uid = re.sub(r"\D", "", str(user_id or ""))
        if not uid:
            return {"ok": False, "msg": "先在「永久记忆」里绑定一个 QQ 号"}
        items = list(texts or [])
        if not items and session_key and from_message_idxs:
            m = _mem_conn()
            try:
                rows = m.get_recent_messages(str(session_key), turns=200)
            finally:
                try:
                    m.close()
                except Exception:
                    pass
            for i in from_message_idxs:
                try:
                    row = rows[int(i)]
                except (IndexError, TypeError, ValueError):
                    continue
                if row.get("role") == "user" and row.get("content"):
                    items.append(str(row["content"]))
        if not items:
            return {"ok": False, "msg": "没有选中任何内容"}
        m = _mem_conn()
        try:
            ok, added, replaced, msg = m.promote_texts(uid, items)
        finally:
            try:
                m.close()
            except Exception:
                pass
        return {"ok": bool(ok), "msg": msg, "added": added, "replaced": replaced}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def demote_to_temp(user_id, perm_ids, session_key=""):
    """把选中的永久记忆转成临时记忆（不再长期注入，但还留在最近上下文里）。"""
    try:
        uid = re.sub(r"\D", "", str(user_id or ""))
        if not uid:
            return {"ok": False, "msg": "没有指定账号"}
        if not perm_ids:
            return {"ok": False, "msg": "没有选中任何记忆"}
        m = _mem_conn()
        try:
            ok, n, msg = m.demote_to_temp(uid, perm_ids,
                                          session_key or f"private_{uid}")
        finally:
            try:
                m.close()
            except Exception:
                pass
        return {"ok": bool(ok), "msg": msg, "count": n}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_memory_dir():
    return _open_result(MEMORY_DIR, "记忆目录")


# ==================== v7：浏览动态（FeedBridge） ====================
def get_feed_state():
    """动态浏览模块的状态 + 历史。"""
    try:
        cfg = _cfg_mod.load()
        caps = {}
        try:
            import qzone
            caps = qzone.caps()
        except Exception:
            pass
        m = _mem_conn()
        try:
            items = m.list_feed(limit=60)
            liked = m.count_feed("liked")
            replied = m.count_feed("replied")
            total = m.count_feed()
        finally:
            try:
                m.close()
            except Exception:
                pass
        return {"ok": True, "items": items, "total": total, "liked": liked,
                "replied": replied,
                "enabled": bool(cfg.get("feed_enabled", False)),
                "decide": str(cfg.get("feed_decide") or "model"),
                "poll_min": int(_ndef(cfg.get("feed_poll_min"), 30)),
                "max_per_round": int(_ndef(cfg.get("feed_max_per_round"), 3)),
                "comment_reply": bool(cfg.get("feed_comment_reply", True)),
                "like_enabled": bool(cfg.get("feed_like_enabled", True)),
                "comment_enabled": bool(cfg.get("feed_comment_enabled", True)),
                "reply_enabled": bool(cfg.get("feed_reply_enabled", True)),
                "reply_check_min": int(_ndef(cfg.get("feed_reply_check_min"), 10)),
                "web_ok": _qzone_web_ok(),
                "caps": caps,
                # v1.9.20：评论/读评论都改走空间网页协议了，NapCat 那边两个能力都是 False，
                # 所以「能不能读」要把网页协议也算进去，否则界面会一直显示「读不到」。
                "readable": bool((caps or {}).get("read") or (caps or {}).get("reply")
                                 or _qzone_web_ok())}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": []}


def _qzone_web_ok():
    """空间网页协议（qzone_interact）登录态在不在——这才是能点赞/评论的那条路。

    注意 `qzone_interact.capabilities()` 会真的去打接口（还要点赞再取消），
    这里只要判断"有没有登录态"，所以只调 `session()`。
    """
    try:
        import qzone_interact as qi
        s = qi.session()
        return bool(s.get("ok") and s.get("uin"))
    except Exception:
        return False


def feed_reply_now():
    """手动回一轮「别人在我动态下的评论」。"""
    try:
        import agent as ag
        n, msg = ag.feed_comment_round(force=True)
        return _with(get_feed_state(), n > 0, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def delete_feed_item(feed_id):
    try:
        m = _mem_conn()
        try:
            ok = m.delete_feed(int(feed_id))
        finally:
            try:
                m.close()
            except Exception:
                pass
        return _with(get_feed_state(), bool(ok), "已删除" if ok else "没找到这条")
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def feed_run_now():
    """手动触发一次"去看一眼别人动态"。"""
    try:
        import agent as ag
        n, msg = ag.feed_round(force=True)
        return _with(get_feed_state(), n > 0, msg)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def open_feed_inbox():
    try:
        import qzone
        qzone.ensure_inbox()
        d = qzone.inbox_dir()
    except Exception:
        d = os.path.join(DATA_DIR, "feed_inbox")
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return _open_result(d, "动态收件箱")


# ==================== JS API 门面 ====================
class Api:
    """暴露给前端的全部方法。所有方法都必须返回可 JSON 序列化的 dict。"""

    # --- 进程 ---
    def start_agent(self):
        return start_agent()

    def stop_agent(self):
        return stop_agent()

    def restart_agent(self, wait=1.0):
        return restart_agent(wait)

    def restart_app(self):
        return restart_app()

    def get_status(self):
        return get_status()

    # --- 日志 ---
    def chat_probe(self, text="", timeout=120):
        return chat_probe(text, timeout)

    def get_logs(self, offset=0):
        return get_logs(offset)

    def clear_logs(self):
        return clear_logs()

    # --- 人设 ---
    def load_personas(self):
        return load_personas()

    def save_personas(self, content, restart=True):
        return save_personas(content, restart=bool(restart))

    def set_active_persona(self, key):
        return set_active_persona(key)

    # --- 模型 ---
    def list_models(self, base="", api_key="", provider=""):
        return list_models(base, api_key, provider)

    def set_model(self, model, restart=False, kind=""):
        return set_model(model, restart=bool(restart), kind=kind)

    def set_model_base(self, base, model=None, kind=""):
        return set_model_base(base, model, kind)

    def test_model(self):
        return test_model()

    def start_lm_server(self):
        return start_lm_server()

    def start_local_server(self, engine=""):
        return start_local_server(engine)

    def release_vram(self):
        return release_vram()

    def set_local_engine(self, engine, base=""):
        return set_local_engine(engine, base)

    def get_config(self):
        return get_config()

    def save_config(self, cfg):
        return save_config(cfg)

    # --- 记忆 ---
    def get_memory_data(self, msg_limit=60, event_limit=60, session_key="", user_id=""):
        return get_memory_data(msg_limit, event_limit, session_key, user_id)

    def delete_event(self, event_id):
        return delete_event(event_id)

    # --- 表情包 / 说说（活人感） ---
    def get_stickers(self):
        return get_stickers()

    def open_sticker_dir(self):
        return open_sticker_dir()

    def test_sticker(self, text="好耶"):
        return test_sticker(text)

    def get_moment_state(self):
        return get_moment_state()

    def post_moment_now(self, text=""):
        return post_moment_now(text)

    # --- v4 设置 ---
    def get_settings(self):
        return get_settings()

    def save_settings(self, patch):
        # v1.6.0：开「自动说说」这种会起后台服务的开关前，先确认性能影响
        try:
            _ack = _perf_need_ack(patch)
            if _ack:
                return {"ok": False, "need_ack": _ack, "msg": perf_ack_msg(_ack)}
        except Exception:
            pass
        return save_settings(patch)

    # --- v1.1.0：人设卡片 ---
    def get_persona_cards(self):
        return get_persona_cards()

    def get_persona_card(self, key):
        return get_persona_card(key)

    def save_persona_order(self, keys):
        return save_persona_order(keys)

    def save_persona_card(self, key, data):
        return save_persona_card(key, data)

    def persona_faces(self, key=""):
        return persona_faces(key)

    def add_persona_face(self, key=""):
        return add_persona_face(key)

    def delete_persona_face(self, key, name):
        return delete_persona_face(key, name)

    def open_persona_faces(self, key=""):
        return open_persona_faces(key)

    def describe_persona_face(self, key=""):
        return describe_persona_face(key)

    def delete_persona_card(self, key):
        return delete_persona_card(key)

    def duplicate_persona_card(self, key, new_key="", new_name=""):
        return duplicate_persona_card(key, new_key, new_name)

    def activate_persona(self, key):
        return activate_persona(key)

    def export_persona_card(self, key):
        return export_persona_card(key)

    def import_persona_card(self):
        return import_persona_card()

    # --- v1.1.0：记忆导入导出 ---
    def get_memory_exports(self):
        return get_memory_exports()

    def do_memory_export(self, kind="json", path=""):
        return do_memory_export(kind, path)

    def export_memory_kind(self, kind="perm", fmt="json", path=""):
        return export_memory_kind(kind, fmt, path)

    def import_memory_kind(self, kind="perm", path="", mode="merge"):
        return import_memory_kind(kind, path, mode)

    def memory_kinds(self):
        return memory_kinds()

    def pick_memory_bundle(self):
        return pick_memory_bundle()

    def preview_memory_bundle(self, path):
        return preview_memory_bundle(path)

    def do_memory_import(self, path="", mode="merge"):
        return do_memory_import(path, mode)

    # --- v1.4.0：时光胶囊（.spark） ---
    def spark_stats(self):
        return spark_stats()

    def verify_package(self, force=False):
        return verify_package(force)

    def spark_pick(self, password=""):
        return spark_pick(password)

    def spark_needs_password(self, path=""):
        return spark_needs_password(path)

    def export_spark(self, path="", password=""):
        return export_spark(path, password)

    def import_spark(self, path="", mode="merge", password=""):
        return import_spark(path, mode, password)

    def spark_job(self):
        return spark_job()

    # --- v1.1.0：「丢进目录就自动导入」 ---
    def do_memory_import_dir(self):
        return do_memory_import_dir()

    def open_memory_import_dir(self):
        return open_memory_import_dir()

    def save_memory_export_schedule(self, every=1, unit="day", enabled=None):
        return save_memory_export_schedule(every, unit, enabled)

    # --- v1.1.1：日记自动备份 / 记忆体检 ---
    def get_diary_backup(self):
        return get_diary_backup()

    def export_diary_backup(self):
        return export_diary_backup()

    def pick_diary_dir(self):
        return pick_diary_dir()

    def pick_dir(self, title="选择目录"):
        """v1.2.0 依赖安装向导：让用户挑一个目录（只返回路径，不改配置）。"""
        return pick_dir(title)

    def pick_file(self, title="选择文件", ext="zip"):
        """v1.2.1 向导兜底：让用户挑一个本地文件（只返回路径，不动任何配置）。

        NapCat 装不上时，用户自己去浏览器下 zip，再从这里选进来。
        """
        try:
            p = _pick_file(save=False, ext=str(ext or "").strip(".") or "zip")
            if not p:
                return {"ok": False, "msg": "没选文件（或对话框打不开，可以手动填路径）"}
            return {"ok": True, "path": p, "msg": os.path.basename(p)}
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}

    def open_diary_backup_dir(self):
        return open_diary_backup_dir()

    def import_diary_backup(self):
        return import_diary_backup()

    def save_diary_export_schedule(self, every=1, unit="day", enabled=None, folder=""):
        return save_diary_export_schedule(every, unit, enabled, folder)

    def get_memory_health(self):
        return get_memory_health()

    def backup_memory_now(self):
        return backup_memory_now()

    # --- v1.1.1：学习 ---
    def learn_state(self):
        return learn_state()

    def learn_chart(self):
        return learn_chart()

    def predict_emotion(self, text="", deep=False):
        return predict_emotion(text, bool(deep))

    def learn_save(self, item_id=0, title="", content="", trigger="", tags="", enabled=True, ltype=""):
        return learn_save(item_id, title, content, trigger, tags, enabled, ltype)

    def learn_delete(self, item_id):
        return learn_delete(item_id)

    def learn_apply(self, ids=None, enabled=True):
        return learn_apply(ids or [], enabled)

    def learn_save_condition(self, text=""):
        return learn_save_condition(text)

    def learn_set_options(self, enabled=None, mode=None, auto=None, hour=None):
        return learn_set_options(enabled, mode, auto, hour)

    def learn_bottom_status(self):
        return learn_bottom_status()

    def learn_bottom_sign(self, signature=""):
        return learn_bottom_sign(signature)

    def learn_bottom_revoke(self):
        return learn_bottom_revoke()

    def learn_run_now(self, user_id=""):
        return learn_run_now(user_id)

    # --- v1.1.1：待办 ---
    def todo_state(self):
        return todo_state()

    def todo_save(self, event_id=0, desc="", when="", user_id=""):
        return todo_save(event_id, desc, when, user_id)

    def todo_delete(self, event_id):
        return todo_delete(event_id)

    def todo_done(self, event_id):
        return todo_done(event_id)

    # --- v1.1.1：管理员控制台 ---
    def admin_state(self):
        return admin_state()

    def admin_verify(self, text=""):
        return admin_verify(text)

    def admin_save(self, prompt=None, bypass=None):
        return admin_save(prompt, bypass)

    def admin_diagnostics(self):
        return admin_diagnostics()

    def admin_export_diagnostics(self):
        return admin_export_diagnostics()

    def get_export_units(self):
        return get_export_units()

    # --- v1.1.0：用量导出导入 ---
    def export_usage_report(self, fmt="csv", path=""):
        return export_usage_report(fmt, path)

    def open_usage_report_dir(self):
        return open_usage_report_dir()

    def import_usage_report(self, path=""):
        return import_usage_report(path)

    # --- v1.1.0：动态读写 ---
    def qzone_caps(self, force=False, write_test=False):
        return qzone_caps(bool(force), bool(write_test))

    def qzone_feeds(self, uin="", num=20):
        return qzone_feeds(uin, num)

    def qzone_friend_feeds(self, max_friends=6, num=6):
        return qzone_friend_feeds(max_friends, num)

    def qzone_like(self, tid, uin="", unlike=False):
        return qzone_like(tid, uin, bool(unlike))

    def qzone_comment(self, tid, uin="", text=""):
        return qzone_comment(tid, uin, text)

    def qzone_comments(self, tid, uin=""):
        return qzone_comments(tid, uin)

    # --- v1.1.0：关于 / 免责 / 暗号 / 完整性 ---
    def get_about(self):
        return get_about()

    def get_integrity(self):
        return get_integrity()

    def get_disclaimer(self):
        return get_disclaimer()

    def accept_disclaimer(self, *_a, **_k):
        # V1.7.0：签署弹窗会把 payload 一起带过来，这里吃掉多余参数
        return accept_disclaimer()

    def set_codeword(self, token, scope="all"):
        return set_codeword(token, scope)

    def clear_codeword(self):
        return clear_codeword()

    def test_codeword(self, text):
        return test_codeword(text)

    def get_codeword_alert(self):
        return get_codeword_alert()

    def list_themes(self):
        return list_themes()

    def apply_theme(self, key):
        return apply_theme(key)

    def open_theme_dir(self):
        return open_theme_dir()

    def get_autostart(self):
        return get_autostart()

    def set_autostart(self, enabled):
        return set_autostart(enabled)

    def get_diary(self):
        return get_diary()

    def bind_account(self, user_id, note=""):
        return bind_account(user_id, note)

    def unbind_account(self, user_id, keep_data=True):
        return unbind_account(user_id, keep_data=bool(keep_data))

    def write_diary_now(self, user_id=""):
        return write_diary_now(user_id)

    def get_groups(self):
        return get_groups()

    def set_group_mode(self, mode):
        return set_group_mode(mode)

    def add_group(self, group_id, to_blacklist=False):
        return add_group(group_id, to_blacklist=bool(to_blacklist))

    def remove_group(self, group_id):
        return remove_group(group_id)

    def get_memory_overview(self, user_id=""):
        # v1.8.4：user_id 非空时只返回这个账号的永久记忆 / 日记 / 会话
        return get_memory_overview(user_id)

    def memory_accounts(self):
        """v1.8.4：记忆页账号胶囊的数据源（只读）。"""
        return memory_accounts()

    def delete_perm(self, perm_id):
        return delete_perm(perm_id)

    def clear_perm(self, user_id):
        return clear_perm(user_id)

    def open_db_dir(self):
        return open_db_dir()

    def open_url(self, url=""):
        """v1.1.0：模型设置里的「去申请 Key / 去官网下载」按钮。"""
        try:
            return shell_open_url(url)
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}

    # ===== v1.2.0：窗口操作改由 Tauri 负责，这里只做兼容占位 =====
    def win_min(self):
        return {"ok": True, "headless": True, "msg": _NO_WINDOW_MSG}

    def win_toggle_max(self):
        return {"ok": True, "headless": True, "maximized": False,
                "msg": _NO_WINDOW_MSG}

    def win_close(self):
        """v1.2.0：真正结束进程由 Tauri 的 Rust 端负责（会一并杀掉本服务）。"""
        _QUIT["flag"] = True
        return {"ok": True, "headless": True, "msg": "请由桌面端关闭窗口"}

    def tray_start(self):
        return {"ok": False, "headless": True, "running": False,
                "msg": "V1.2.0 起托盘由 Tauri 提供（TrayIcon）"}

    # ===== v1.1.1：画图 =====
    def get_draw_status(self):
        return get_draw_status()

    def draw_now(self, prompt="", size="1280x720", model=""):
        return draw_now(prompt, size, model)

    def draw_svg(self, prompt="", size="1280x720", model=""):
        return draw_svg(prompt, size, model)

    def save_canvas(self, data_url="", name=""):
        return save_canvas(data_url, name)

    def delete_draw(self, path=""):
        return delete_draw(path)

    def open_draw_dir(self):
        return open_draw_dir()

    # ===== v1.1.1：启动顺序检测 =====
    def run_startup_checks(self, deep=True):
        return run_startup_checks(deep)

    def get_startup_order(self):
        return get_startup_order()

    def save_startup_order(self, keys=None):
        return save_startup_order(keys)

    # v1.1.3：启动判定顺序（boot_check_order）
    def get_boot_order(self):
        return get_boot_order()

    def save_boot_order(self, order=None):
        return save_boot_order(order)

    def cleanup_check_traces(self):
        return cleanup_check_traces()

    # v1.1.3 任务十三：系统自检
    def system_selfcheck(self):
        return system_selfcheck()

    def get_perf(self):
        return get_perf()

    def get_introspection(self):
        return get_introspection()

    def check_release(self):
        return check_release()

    def open_release_page(self, url=""):
        return open_release_page(url)

    def get_semantic(self):
        return get_semantic()

    def semantic_probe(self, query="", k=5):
        return semantic_probe(query, k)

    def save_embed_backend(self, backend="auto"):
        # v1.6.0：本地向量模型会吃内存/CPU，先过性能预警
        _bk = str(backend or "auto").strip().lower()
        if _bk not in ("none", "off", "keyword") and not perf_acked("embed"):
            return {"ok": False, "need_ack": "embed", "msg": perf_ack_msg("embed")}
        return save_embed_backend(backend)

    def semantic_reindex(self, limit=800):
        return semantic_reindex(limit)

    def run_decay_now(self, max_items=None):
        return run_decay_now(max_items)

    def run_reflect_now(self, force=True, count=None):
        return run_reflect_now(force, count)

    # v1.1.3 任务四：情感中枢
    def emotion_center(self):
        return emotion_center()

    def save_emotion_center(self, preset=None, enabled=None, timeout=None, prompt_text=None,
                            mode=None, profile_enabled=None):
        # v1.8.13：一律关键字传参。以前是位置传参，签名一改（少了 preset）
        # 就会整体错位 —— 用户看到的是「整页保存全废」的 NameError。
        return save_emotion_center(preset=preset, enabled=enabled, timeout=timeout,
                                   prompt_text=prompt_text, mode=mode,
                                   profile_enabled=profile_enabled)

    # V1.1.6 补丁 3（m08875 ⑦）：学习提示词本地可改
    def get_learn_prompt(self):
        return get_learn_prompt()

    def save_learn_prompt(self, text=""):
        return save_learn_prompt(text)

    def clear_emotion_log(self):
        return clear_emotion_log()

    # ===== v1.1.1：一键启动 =====
    def start_everything(self):
        return start_everything()

    # v1.1.2 #22：网页启动版
    def web_state(self):
        return web_state()

    def start_web(self, port=0):
        return start_web(port)

    def stop_web(self):
        return stop_web()

    def open_web(self):
        return open_web()

    def win_state(self):
        """v1.2.0：窗口由 Tauri 管理，这里只报告配置值与「无头」事实。"""
        try:
            cfg_fl = bool(_cfg_mod.get("window_frameless", True))
            try:
                sub = (get_status() or {}).get("subtitle", "")
            except Exception:
                sub = ""
            return {
                "ok": True,
                "headless": True,
                "frameless": cfg_fl,
                "frameless_cfg": cfg_fl,
                "frameless_real": None,
                "tray_enabled": bool(_cfg_mod.get("tray_enabled", True)),
                "tray_running": False,
                "icon": bool(_icon_path("whale.png")),
                "subtitle": sub,
                "msg": "窗口状态由 Tauri 端维护",
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def win_resize(self, w=0, h=0):
        """v1.2.0：窗口尺寸由 Tauri 端 set_size 处理，这里保留接口但不报错。"""
        try:
            w, h = int(float(w)), int(float(h))
        except (TypeError, ValueError):
            return {"ok": False, "msg": "尺寸不对"}
        return {"ok": True, "headless": True, "w": w, "h": h,
                "msg": _NO_WINDOW_MSG}

    def get_notices(self):
        return get_notices()

    def refresh_notices(self, url=""):
        return refresh_notices(url)

    def dismiss_notice(self, notice_id):
        return dismiss_notice(notice_id)

    def get_update_status(self):
        return get_update_status()

    def check_update(self):
        return check_update()

    def apply_update(self, restart=True):
        return apply_update(restart=bool(restart))

    def rollback_update(self, stamp=""):
        return rollback_update(stamp)
    def apply_update_package(self, path="", restart=True):
        return apply_update_package(path, restart=bool(restart))


    def get_icons(self):
        return get_icons()

    def set_icon(self, name):
        return set_icon(name)

    def open_icon_dir(self):
        return open_icon_dir()

    # --- v5：模型提供方 / 云端 API ---
    def get_providers(self):
        return get_providers()

    def set_provider(self, pkey, base="", model="", api_key=None):
        return set_provider(pkey, base, model, api_key)

    def set_cloud_key(self, api_key):
        return set_cloud_key(api_key)

    # --- v5：用量看板 ---
    def get_usage(self):
        return get_usage()

    def clear_usage(self):
        return clear_usage()

    # --- v5：日记编辑 ---
    def save_diary(self, user_id, day, content, title="", mood=""):
        return save_diary(user_id, day, content, title, mood)

    def delete_diary(self, diary_id):
        return delete_diary(diary_id)

    def regenerate_diary(self, user_id, day=""):
        return regenerate_diary(user_id, day)

    # --- v5：记忆导出 / 导入 ---
    def export_memory(self, user_id="", include_diary=True,
                      include_events=False, to_file=True):
        return export_memory(user_id, include_diary, include_events, bool(to_file))

    def import_memory(self, path="", data=None, user_id="", mode="merge"):
        return import_memory(path, data, user_id, mode)

    def list_memory_backups(self):
        return list_memory_backups()

    def open_memory_export_dir(self, kind=""):
        return open_memory_export_dir(kind)

    def open_memory_backup_dir(self):
        return open_memory_backup_dir()

    # --- v5：NapCat ---
    def get_napcat(self):
        return get_napcat()

    def napcat_recheck(self, wait=8):
        return napcat_recheck(wait)

    def start_napcat(self, wait=25, force=False):
        return start_napcat(wait, force)

    def napcat_log(self, lines=40):
        return napcat_log(lines)

    def open_napcat_qr(self):
        return open_napcat_qr()

    # --- v1.9.17：应用内扫码登录弹窗 ---
    def qq_login_panel(self, start=False):
        return qq_login_panel(bool(start))

    def qq_login_poll(self):
        return qq_login_poll()

    def qq_login_refresh(self):
        return qq_login_refresh()

    def qq_login_pick(self, uin=""):
        return qq_login_pick(uin)

    def qq_login_start(self):
        return qq_login_start()

    def qq_login_takeover(self):
        return qq_login_takeover()

    def test_napcat_ws(self, url=""):
        return test_napcat_ws(url)

    def napcat_detect_login(self):
        """V1.3.3：检测 QQ 到底登没登；登了就补齐 OneBot 配置并重启 NapCat，让 3003/3004 起来。

        用户实机反馈：扫描明明成功了，界面还是「等扫码」——根因是 NapCat 读的配置在
        `<安装目录>\napcat\config`，而我们把模板写到了 `<安装目录>\config`，于是
        NapCat 自己生成一份 network 全空的账号配置，HTTP/WS 一个都不开。
        """
        try:
            import napcat_mgr
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}
        try:
            r = napcat_mgr.detect_and_enable()
        except Exception as e:
            return {"ok": False, "msg": f"{type(e).__name__}: {e}"}
        try:
            _append_log("NapCat 检测登录：" + str(r.get("msg") or ""))
        except Exception:
            pass
        return r

    def napcat_quit_qq_and_connect(self, wait=35):
        return napcat_quit_qq_and_connect(wait)

    def napcat_login_state(self, launch_if_down=False, wait=25):
        return napcat_login_state(launch_if_down, wait)

    def open_napcat_dir(self):
        return open_napcat_dir()

    def check_napcat_release(self):
        return check_napcat_release()

    def install_napcat(self, target_dir=""):
        return install_napcat(target_dir)

    def write_napcat_config(self, qq=""):
        return write_napcat_config(qq)

    # --- v5：通知中心 ---
    def get_notice_center(self):
        return get_notice_center()

    def get_notice_popup(self):
        return get_notice_popup()

    def ack_notice_popup(self, ids=None):
        return ack_notice_popup(ids)

    def mark_notice_read(self, notice_id):
        return mark_notice_read(notice_id)

    def mark_all_notices_read(self):
        return mark_all_notices_read()

    # --- v5：记忆重编译 ---
    def memory_rebuild_preview(self):
        return memory_rebuild_preview()

    def memory_rebuild_run(self, fill_norm=True, dedupe=True, migrate_legacy=True):
        return memory_rebuild_run(bool(fill_norm), bool(dedupe), bool(migrate_legacy))

    def list_db_backups(self):
        return list_db_backups()

    def memory_verify(self):
        return memory_verify()

    def memory_restore_backup(self, name):
        return memory_restore_backup(name)

    # --- v5：卸载 ---
    def get_uninstall_info(self):
        return get_uninstall_info()

    # --- v8：卸载程序 / 好感度看板 ---
    def get_uninstall_plan(self):
        return get_uninstall_plan()

    def run_uninstall(self, keep_data=True):
        return run_uninstall(bool(keep_data))

    def quit_app(self):
        return quit_app()

    def get_affinity_board(self, limit=200):
        return get_affinity_board(limit)

    def reset_affinity(self, user_id=""):
        return reset_affinity(user_id)

    def set_affinity_score(self, user_id, score):
        return set_affinity_score(user_id, score)

    # --- v1.1.4 阶段三：伴侣关系 ---
    def get_romance_list(self):
        return get_romance_list()

    def get_romance(self, user_id=""):
        return get_romance(user_id)

    def set_romance(self, user_id, level="", note=None, auto=0):
        return set_romance(user_id, level, note, auto)

    def delete_romance(self, user_id=""):
        return _wrap(lambda: delete_romance(user_id))

    def set_keep_data(self, keep):
        return set_keep_data(keep)

    # ==================== v7 ====================
    # --- 模型设置（双开关 + 优先级链）---
    def get_plugins(self):
        """插件清单。"""
        return _wrap(get_plugins)

    def set_plugin_enabled(self, name="", on=True):
        """开关插件。"""
        return _wrap(lambda: set_plugin_enabled(name, on))

    def open_plugin_dir(self):
        """打开插件目录。"""
        return _wrap(open_plugin_dir)

    def run_plugin(self, name=""):
        """跑一次插件。"""
        return _wrap(lambda: run_plugin(name))

    def reload_plugins(self):
        """重新扫描插件。"""
        return _wrap(reload_plugins)

    def get_feature_models(self):
        """按功能分工的模型清单。"""
        return _wrap(get_feature_models)

    def save_feature_model(self, feature="", provider="", base="", model="",
                           api_key="", clear=False):
        """保存某个功能的专用模型。"""
        return _wrap(lambda: save_feature_model(feature, provider, base, model, api_key, clear))

    def get_model_settings(self):
        return get_model_settings()

    def set_local_enabled(self, enabled):
        return set_local_enabled(enabled)

    def set_api_enabled(self, enabled):
        return set_api_enabled(enabled)

    def set_model_failover(self, enabled):
        return set_model_failover(enabled)

    def set_provider_key(self, provider, api_key):
        return set_provider_key(provider, api_key)

    def list_local_models(self):
        return list_local_models()

    def list_cloud_models(self, provider="", base="", api_key=""):
        return list_cloud_models(provider, base, api_key)

    def add_model_to_chain(self, kind, provider="", base="", model="", api_key="", front=False):
        return add_model_to_chain(kind, provider, base, model, api_key, front)

    def update_model_in_chain(self, index, base="", model="", api_key=""):
        return update_model_in_chain(index, base, model, api_key)

    def remove_model_from_chain(self, index):
        return remove_model_from_chain(index)

    def move_model_in_chain(self, frm, to):
        return move_model_in_chain(frm, to)

    def clear_model_chain(self):
        return clear_model_chain()

    # --- 主动回复（user_events）---
    def get_events(self, status="", limit=100):
        return get_events(status, limit)

    def add_event_manual(self, user_id, desc, when_text, session_key=""):
        return add_event_manual(user_id, desc, when_text, session_key)

    def update_event_event(self, event_id, desc=None, when_text=None):
        return update_event_event(event_id, desc, when_text)

    def delete_event_item(self, event_id):
        return delete_event_item(event_id)

    def cancel_event_item(self, event_id):
        return cancel_event_item(event_id)

    # --- 临时 ⇄ 永久 ---
    def get_sessions(self, limit=40):
        return get_sessions(limit)

    def get_temp_messages(self, session_key, limit=40):
        return get_temp_messages(session_key, limit)

    def promote_to_perm(self, user_id, texts=None, session_key="", from_message_idxs=None):
        return promote_to_perm(user_id, texts, session_key, from_message_idxs)

    def demote_to_temp(self, user_id, perm_ids, session_key=""):
        return demote_to_temp(user_id, perm_ids, session_key)

    def open_memory_dir(self):
        return open_memory_dir()

    # --- 浏览动态 ---
    def get_feed_state(self):
        return get_feed_state()

    def delete_feed_item(self, feed_id):
        return delete_feed_item(feed_id)

    def feed_run_now(self):
        return feed_run_now()

    def feed_reply_now(self):
        return feed_reply_now()

    def open_feed_inbox(self):
        return open_feed_inbox()

    # ==================== v6 ====================
    # --- 背景 ---
    def get_backgrounds(self):
        return get_backgrounds()

    def get_bg_data(self, name):
        return get_bg_data(name)

    def set_background(self, name):
        return set_background(name)

    def save_bg_options(self, dim=0, blur=0, fit="cover"):
        return save_bg_options(dim, blur, fit)

    def open_bg_dir(self):
        return open_bg_dir()

    # --- 语言 ---
    def get_langs(self):
        return get_langs()

    def get_ui_lang(self, code=""):
        return get_ui_lang(code)

    def set_lang(self, code):
        return set_lang(code)

    def open_lang_dir(self):
        return open_lang_dir()

    # --- 记忆多格式 ---
    def list_memory_formats(self):
        return list_memory_formats()

    def memory_export(self, fmt="qq-agent", user_id="", to_file=True):
        return memory_export(fmt, user_id, bool(to_file))

    def memory_import(self, path="", content="", fmt="", user_id="", mode="merge"):
        return memory_import(path, content, fmt, user_id, mode)

    # --- 关心计划 ---
    def get_plans(self, status="", limit=80):
        return get_plans(status, limit)

    def add_plan(self, user_id, session_key, plan_at_text, reason="", topic=""):
        return add_plan(user_id, session_key, plan_at_text, reason, topic)

    def update_plan(self, plan_id, plan_at_text="", reason=None, topic=None):
        return update_plan(plan_id, plan_at_text, reason, topic)

    def delete_plan(self, plan_id):
        return delete_plan(plan_id)

    def cancel_plan(self, plan_id):
        return cancel_plan(plan_id)

    # --- 说说 ---
    def get_moment_status(self):
        return get_moment_status()

    def post_moment_rich(self, text="", use_images=True):
        return post_moment_rich(text, bool(use_images))

    def open_moment_image_dir(self):
        return open_moment_image_dir()

    def open_comment_inbox(self):
        return open_comment_inbox()

    def reply_comment_now(self, comment_id):
        return reply_comment_now(comment_id)

    def drain_comment_inbox(self):
        return drain_comment_inbox()

    def delete_comment(self, comment_id):
        return delete_comment(comment_id)

    # --- 生图 ---
    def get_imagegen_status(self):
        return get_imagegen_status()
    # v1.8.12：情感画像
    def emotion_profile(self, user_id=""):
        return emotion_profile(user_id)
    def emotion_profile_save(self, user_id="", traits=None, landmines=None,
            expressions=None, notes=None, mood_bias=None):
        return emotion_profile_save(user_id, traits, landmines, expressions,
            notes, mood_bias)
    def emotion_profile_clear(self, user_id=""):
        return emotion_profile_clear(user_id)
    def emotion_profile_reflect(self, user_id=""):
        return emotion_profile_reflect(user_id)

    def sing_status(self):
        return sing_status()

    def save_sing(self, patch=None):
        return save_sing(patch)

    def sing_test(self, name="", lines=None):
        return sing_test(name, lines)

    def polyphone_preview(self, text=""):
        return polyphone_preview(text)

    def draw_self(self, extra=""):
        return draw_self(extra)

    def set_avatar(self, path=""):
        return set_avatar(path)

    def avatar_status(self):
        return avatar_status()

    def pick_voice_file(self):
        return pick_voice_file()

    def upload_voice(self, path="", name=""):
        return upload_voice(path, name)

    def list_voices(self):
        return list_voices()

    def sticker_stats(self):
        return sticker_stats()

    def save_imagegen(self, patch):
        return save_imagegen(patch)

    def test_imagegen(self, prompt="一只橘猫在键盘上睡觉，卡通风格"):
        return test_imagegen(prompt)

    def open_generated_dir(self):
        return open_generated_dir()

    def gen_sticker_now(self, text="生气"):
        return gen_sticker_now(text)

    # --- 用量明细 ---
    def get_usage_details(self, limit=80, kind="", day=""):
        return get_usage_details(limit, kind, day)

    # --- 其他 ---
    def open_data_dir(self):
        """在资源管理器里打开数据目录"""
        return _open_result(DATA_DIR, "已打开目录")


# ==================== 自举：打包后 --agent 走 Agent 分支 ====================
# ==================== v1.3.0：音视频通话模块（独立微服务，端口 5800）====================
# 音视频功能是另一个人独立开发的独立进程（av_server.py，FastAPI，5800）。
# 本文件只做三件事：转发、降级、记录。真正的逻辑一行都不在这里。
#
# 为什么把这些方法写在类外面再挂上去：class Api 有 900 多行、层层嵌套，
# 往里塞容易改坏别人的东西；挂在一个独立区块里，出问题一眼能定位。
# 前端调用方式不变：call('av_status') / call('av_speak', 文本)。
def _av():
    """懒加载桥接模块：微服务装没装都不影响控制台启动。"""
    import av_bridge
    return av_bridge


def _av_err(e):
    return {"ok": False, "msg": f"音视频模块出错：{type(e).__name__}: {e}"}


def av_status(self):
    """音视频模块的「壳」状态：启用了没、跑没跑、端口、目录、python。"""
    try:
        return _av().BRIDGE.status_snapshot()
    except Exception as e:
        return _av_err(e)


def av_start(self):
    """拉起 av_server.py（已跑着就直接返回）。成功顺手把 av_module_enabled 记成 true，
    这样下次开控制台会自动拉起来。"""
    # v1.6.0：重型功能要先过性能预警（用户点过「知道了」才放行）
    if not perf_acked("av"):
        return {"ok": False, "need_ack": "av", "msg": perf_ack_msg("av")}
    try:
        r = _av().BRIDGE.start()
        if r.get("ok"):
            try:
                import config as cfg_mod
                if not cfg_mod.get("av_module_enabled", False):
                    cfg_mod.set_checked("av_module_enabled", True)
            except Exception:
                pass
            _append_log(f"[音视频] {r.get('msg', '')}")
        return r
    except Exception as e:
        return _av_err(e)


def _on(v):
    """把 true/1/"on"/"开" 这类都当关开处理。"""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y", "开", "开启", "是")
    return bool(v)


def _port_busy(port, timeout=0.35):
    """本机端口有没有人在听（用来证明「关了之后 5800 真的放开了」）。"""
    import socket as _socket
    s = _socket.socket()
    s.settimeout(float(timeout))
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def av_set_enabled(self, want=False, force=True):
    """v1.6.1（QA P0-1）：音视频的「开关」= 真起真停。

    以前前端只调 save_settings 写配置，关了开关 av_server.exe 照样在后台跑、
    5800 也照样占着。现在一个入口做完三件事：
      1. 写 av_module_enabled（用户意图，唯一的真相）；
      2. 关 → 停微服务；开 → 过性能预警后启动；
      3. 回报端口有没有真的放开，前端据此显示「已释放 / 仍被占用」。
    """
    on = _on(want)
    try:
        import config as cfg_mod
        ok, msg, _c = cfg_mod.set_checked("av_module_enabled", on)
        if not ok:
            return {"ok": False, "msg": f"保存开关失败：{msg}"}
    except Exception as e:                     # noqa: BLE001
        return {"ok": False, "msg": f"保存开关失败：{type(e).__name__}: {e}"}
    try:
        port = int((cfg_mod.get("av_module_port", 5800) or 5800))
    except Exception:
        port = 5800
    out = {"ok": True, "enabled": on, "running": False, "port": port,
           "port_free": not _port_busy(port)}
    if not on:
        try:
            snap = _av().BRIDGE.status_snapshot() or {}
            if snap.get("running"):
                r = _av().BRIDGE.stop(bool(force))
                out["msg"] = r.get("msg") or "已停止"
            else:
                out["msg"] = "音视频模块已关（本来就没在跑）"
        except Exception as e:                 # noqa: BLE001
            _append_log(f"[音视频] 关的时候出错（{type(e).__name__}: {e}）", "WARN")
            out["msg"] = f"关了开关，但停服务时出错（{type(e).__name__}）"
        out["running"] = False
        out["port_free"] = not _port_busy(port)
        _append_log(f"[音视频] 开关关闭：{out['msg']}；"
                    f"{port} {'已释放' if out['port_free'] else '仍被占用'}")
        return out
    if not perf_acked("av"):
        out.update({"need_ack": "av", "msg": perf_ack_msg("av")})
        return out
    try:
        r = _av().BRIDGE.start()
        out["msg"] = r.get("msg") or ""
        out["already"] = bool(r.get("already"))
        out["ok"] = bool(r.get("ok", True))
        out["hint"] = r.get("hint") or ""
        out["running"] = bool((_av().BRIDGE.status_snapshot() or {}).get("running"))
    except Exception as e:                     # noqa: BLE001
        return {"ok": False, "enabled": on, "port": port,
                "msg": f"起的时候出错：{type(e).__name__}: {e}"}
    _append_log(f"[音视频] 开关打开：{out['msg']}")
    return out


def av_stop(self, force=False):
    """只关本控制台拉起的那个进程；外部起的要 force=True 才会按端口找 PID 结束。"""
    try:
        r = _av().BRIDGE.stop(bool(force))
        _append_log(f"[音视频] {r.get('msg', '')}")
        return r
    except Exception as e:
        return _av_err(e)


def av_log(self, lines=40):
    """微服务的 stdout 尾巴（启动失败时最有用的东西）。"""
    try:
        return _av().BRIDGE.tail_log(int(lines or 40))
    except Exception as e:
        return _av_err(e)


def av_speak(self, text="", voice="", engine="", block=True):
    """TTS → 播到虚拟麦克风（CABLE Input）。block=True 时等播完再返回。"""
    try:
        return _av().speak(str(text or ""), str(voice or ""), str(engine or ""),
                           bool(block))
    except Exception as e:
        return _av_err(e)


def av_speak_stop(self):
    try:
        return _av().BRIDGE.call("POST", "/api/av/speak/stop", {}, timeout=15.0)
    except Exception as e:
        return _av_err(e)


def av_listen(self, seconds=6.0, mode="loopback", language=""):
    """录一段再转文字（阻塞到录完）。mode: loopback=抓系统声音 / device=抓虚拟线输入。"""
    try:
        return _av().listen(float(seconds or 6.0), str(mode or "loopback"),
                            str(language or ""))
    except Exception as e:
        return _av_err(e)


def av_listen_start(self, mode="loopback", language=""):
    """开连续听写：后台按静音切句，片段从 av_events 里出来。"""
    try:
        body = {"mode": str(mode or "loopback")}
        if language:
            body["language"] = str(language)
        return _av().BRIDGE.call("POST", "/api/av/listen/start", body)
    except Exception as e:
        return _av_err(e)


def av_listen_stop(self):
    try:
        return _av().BRIDGE.call("POST", "/api/av/listen/stop", {})
    except Exception as e:
        return _av_err(e)


def av_listen_status(self):
    try:
        return _av().BRIDGE.call("GET", "/api/av/listen/status", None, timeout=15.0)
    except Exception as e:
        return _av_err(e)


def av_remote_status(self):
    """微服务自己的完整状态（设备 / TTS / STT / 视频），面板上那些指示器用它。"""
    try:
        return _av().BRIDGE.call("GET", "/api/av/status", None, timeout=30.0)
    except Exception as e:
        return _av_err(e)


# ==================== v1.8.0：自定义音色（声音克隆）====================
def av_tts_custom(self=None, payload=None, **_kw):
    """查/改自定义音色配置。payload里的 ref_audio_b64 会被后端存成参考音频。"""
    try:
        import av_bridge as _ab
        if payload is None or payload == "":
            return _ab.tts_custom_get()
        if not isinstance(payload, dict):
            return {"ok": False, "msg": "参数格式不对"}
        return _ab.tts_custom_set(payload)
    except Exception as e:
        return _av_err(e)


def av_tts_custom_test(self=None, text="", block=True, **_kw):
    """试一句自定义音色；不成功会自动退回 edge，并在 note里说明。"""
    try:
        import av_bridge as _ab
        return _ab.tts_custom_test(str(text or ""), bool(block))
    except Exception as e:
        return _av_err(e)


def av_voices(self, locale="zh"):
    """可用音色列表（edge-tts）。"""
    try:
        return _av().BRIDGE.call("GET", f"/api/av/voices?locale={locale or 'zh'}",
                                 None, timeout=30.0)
    except Exception as e:
        return _av_err(e)


def av_devices(self):
    """音频设备 / 回环源（排障用：看 CABLE Input 到底装没装）。"""
    try:
        return _av().BRIDGE.call("GET", "/api/av/devices", None, timeout=25.0)
    except Exception as e:
        return _av_err(e)


def av_drivers(self):
    """虚拟声卡 / 虚拟摄像头的安装状态（VB-Cable、OBS Virtual Camera）。"""
    try:
        return _av().BRIDGE.call("POST", "/api/av/drivers/check", {}, timeout=90.0)
    except Exception as e:
        return _av_err(e)


# v1.9.8-Beta：视频相关接口统一停用（V1.9.9 再开）
_MAINT_VIDEO = {
    "ok": False,
    "code": "under_maintenance",
    "msg": "视频功能正在升级，暂时关闭，请期待后续更新。",
}

def av_stream_start(self, image="placeholder", effect=""):
    """开始推流到虚拟摄像头（没装驱动会自动进演练模式，只生成预览帧）。"""
    # v1.9.8-Beta：维护期一律拒绝（防 Agent / RPA / 脚本绕过界面直接调）
    return dict(_MAINT_VIDEO)
    try:
        body = {"image": str(image or "placeholder")}
        if effect:
            body["effect"] = str(effect)
        return _av().BRIDGE.call("POST", "/api/av/stream/start", body)
    except Exception as e:
        return _av_err(e)


def av_stream_image(self, image="", effect=""):
    """换一张推流画面：png/jpg 路径、http URL、dataURL、base64 都行。"""
    # v1.9.8-Beta：维护期一律拒绝（防 Agent / RPA / 脚本绕过界面直接调）
    return dict(_MAINT_VIDEO)
    try:
        body = {"image": str(image or "")}
        if effect:
            body["effect"] = str(effect)
        return _av().BRIDGE.call("POST", "/api/av/stream_image", body)
    except Exception as e:
        return _av_err(e)


def av_stream_stop(self):
    # v1.9.8-Beta：维护期一律拒绝（防 Agent / RPA / 脚本绕过界面直接调）
    return dict(_MAINT_VIDEO)
    try:
        return _av().BRIDGE.call("POST", "/api/av/stream/stop", {})
    except Exception as e:
        return _av_err(e)


def av_stream_caption(self, text=""):
    """给推流画面叠一行字幕（空串 = 清掉）。"""
    # v1.9.8-Beta：维护期一律拒绝（防 Agent / RPA / 脚本绕过界面直接调）
    return dict(_MAINT_VIDEO)
    try:
        return _av().BRIDGE.call("POST", "/api/av/stream/caption", {"text": str(text or "")})
    except Exception as e:
        return _av_err(e)


def av_preview(self):
    """当前推流帧（dataURL），面板里 <img> 直接用，前端不用知道 5800 这个端口。"""
    try:
        return _av().preview_data_url()
    except Exception as e:
        return _av_err(e)


def av_rpa_start(self, dry_run=True):
    """开启自动接听。dry_run=True 只记录不点鼠标（默认就是演练）。"""
    try:
        return _av().BRIDGE.call("POST", "/api/av/rpa/start",
                                 {"dry_run": bool(dry_run)}, timeout=30.0)
    except Exception as e:
        return _av_err(e)


def av_rpa_stop(self):
    try:
        return _av().BRIDGE.call("POST", "/api/av/rpa/stop", {}, timeout=20.0)
    except Exception as e:
        return _av_err(e)


def av_rpa_templates(self):
    """查接听/挂断模板在不在（前端据此提示「请先截取接听按钮模板」）。"""
    try:
        return _av().BRIDGE.call("GET", "/api/av/rpa/templates", None, timeout=20.0)
    except Exception as e:
        return _av_err(e)


def av_rpa_status(self):
    try:
        return _av().BRIDGE.call("GET", "/api/av/rpa/status", None, timeout=20.0)
    except Exception as e:
        return _av_err(e)


def av_rpa_scan(self, depth=3, limit=40):
    """扫一遍窗口和控件（调 accept_labels 用；QQ/微信 Electron 层级浅是正常的）。"""
    try:
        return _av().BRIDGE.call(
            "GET", f"/api/av/rpa/scan?depth={int(depth)}&limit={int(limit)}",
            None, timeout=40.0)
    except Exception as e:
        return _av_err(e)


def av_events(self, since=0, limit=50):
    """事件游标：transcript（听写片段）/ rpa（接听动作）/ audio（告警）/ video。"""
    try:
        return _av().BRIDGE.call(
            "GET", f"/api/av/events?since={int(since or 0)}&limit={int(limit or 50)}",
            None, timeout=15.0)
    except Exception as e:
        return _av_err(e)


# ---------------------------------------------------------------- v1.5.0：Agent 总闸门
def agent_gate_status(self):
    """总闸门状态（含要不要在 Tab 里显示 Agent）。"""
    try:
        import agent_gate
        return agent_gate.status()
    except Exception as e:
        return {"ok": False, "msg": "读不到 Agent 开关：%s" % e}


def agent_gate_issue(self):
    """点「启用 Agent」时先领一张警告票（前端要倒数 10 秒才允许确认）。"""
    try:
        import agent_gate
        info = agent_gate.issue()
        info["ok"] = True
        return info
    except Exception as e:
        return {"ok": False, "msg": "拿不到警告说明：%s" % e}


def agent_gate_enable(self, token=""):
    """读完 10 秒并勾选后调用：真正打开 Agent，并把服务起起来。"""
    if isinstance(token, dict):
        token = token.get("token") or ""
    try:
        import agent_gate
        r = agent_gate.consume_and_enable(str(token or ""))
        _append_log("[Agent] 总开关：%s" % ("已开启" if r.get("ok") else r.get("msg", "")))
        return r
    except Exception as e:
        return {"ok": False, "msg": "开启失败：%s" % e}


def agent_gate_disable(self):
    """关掉 Agent：停掉键鼠热键、急停、清掉待授权。"""
    try:
        import agent_gate
        r = agent_gate.disable()
        _append_log("[Agent] 总开关：已关闭")
        return r
    except Exception as e:
        return {"ok": False, "msg": "关闭失败：%s" % e}


def agent_share(self, action_id=0):
    """把一条动作记录用人话播报出来（界面「让他说说」按钮）。"""
    try:
        import agent as _agent
        return _agent.agent_share_action(int(action_id or 0))
    except Exception as e:
        return {"ok": False, "msg": "播报失败：%s" % e}


# ---------------------------------------------------------------- v1.4.0：Agent
_AGENT_JOB = {"running": False, "state": "idle", "task": "", "answer": "",
              "msg": "", "pct": 0, "steps": [], "started": 0, "stopped": False}


def agent_status(self):
    """Agent 面板一次拿全：总闸门、三个开关、屏幕、工具、打电话护栏、最近任务。"""
    try:
        import agent_gate
        import computer_use as cu
        import qq_call
        st = cu.status()
        st["gate"] = agent_gate.status()
        st["call"] = qq_call.state()
        st["tools"] = [{"name": t["name"], "when": t["when"], "args": t["args"]}
                       for t in (cu.TOOLS + qq_call.TOOLS)]
        job = dict(_AGENT_JOB)
        job["steps"] = list(_AGENT_JOB.get("steps") or [])[-20:]
        # v1.6.3（QA P1-5）：聊天走的是独立 Agent进程，它的 job落在一个共享快照里；
        #本地没在跑任务时就读快照，前端【Agent日志】才有东西看。
        if not job.get("running"):
            try:
                import agent as _agent_mod
                _snap = _agent_mod.job_snapshot() or {}
                if _snap and str(_snap.get("task") or "").strip():
                    job.update({k: _snap.get(k) for k in
                        ("running", "state", "task", "answer", "msg", "pct", "stopped")
                        if k in _snap})
                    job["steps"] = list(_snap.get("steps") or [])[-20:]
                    job["from"] = "agent"
            except Exception:
                pass
        # v1.6.3（QA P2-3）：急停之后别再显示 running/pct19 ——就地改成 aborted
        try:
            if job.get("running") and cu.stopped():
                job["running"] = False
                job["state"] = "aborted"
                job["stopped"] = True
                job["pct"] =100
                job["msg"] = job.get("msg") or "已被紧急停止"
        except Exception:
            pass
        st["job"] = job
        return st
    except Exception as e:
        return {"ok": False, "msg": "读不到 Agent 状态：%s" % e}


def agent_run_task(self, task="", approve=False):
    """从界面手动让 Agent 干一件事（后台线程跑，前端轮询 agent_task_status）。

    v1.5.2：`approve` 参数保留只为兼容老调用方，**已经不再有任何「免确认」捷径** ——
    危险动作一律走 approval_bus 逐个确认（QA No-Go 修复四）。
    """
    import threading
    task = str(task or "").strip()
    if not task:
        return {"ok": False, "msg": "先写一句要它做什么"}
    # v1.6.0：总开关没开就直接说清楚，别「点了显示开始了、日志里却什么都没干」
    try:
        import agent_gate as _gate
        if not _gate.enabled():
            return {"ok": False,
                    "msg": "Agent 总开关还关着 —— 先去「系统设置 → Agent」读一遍风险说明再打开，"
                           "它才敢动你的鼠标键盘。"}
    except Exception:
        pass
    if _AGENT_JOB.get("running"):
        return {"ok": False, "msg": "上一个任务还没做完"}
    import agent as _agent

    _AGENT_JOB.update({"running": True, "state": "running", "task": task, "answer": "",
                       "msg": "开始", "pct": 0, "steps": [], "started": time.time(),
                       "stopped": False})
    _agent.APPROVAL_HOOK = None

    def _run():
        try:
            def _on_step(d):
                _AGENT_JOB["steps"] = (list(_AGENT_JOB.get("steps") or []) + [d])[-50:]
                _AGENT_JOB["msg"] = ("%s %s" % (d.get("tool", ""), d.get("msg", "")))[:200]
                _AGENT_JOB["pct"] = min(95, 10 + 9 * len(_AGENT_JOB["steps"]))
            r = _agent.run_agent_task(task, session="ui", on_step=_on_step)
            _AGENT_JOB.update({"answer": r.get("answer") or "",
                               "msg": "完成" if r.get("ok") else (r.get("error") or "没做成"),
                               "pct": 100, "stopped": bool(r.get("stopped"))})
        except Exception as e:
            _AGENT_JOB.update({"msg": "执行出错：%s" % e, "pct": 100})
        finally:
            try:
                import approval_bus as _ab
                _ab.cancel_all("任务已结束")
            except Exception:
                pass
            _AGENT_JOB["running"] = False
            _AGENT_JOB["state"] = "done"
            _agent.APPROVAL_HOOK = None

    threading.Thread(target=_run, name="agent-task", daemon=True).start()
    return {"ok": True, "msg": "开始了，日志在【Agent 日志】里"}


def agent_task_status(self):
    job = dict(_AGENT_JOB)
    job["steps"] = list(job.get("steps") or [])[-20:]
    return job


def agent_actions(self, limit=60, kind=""):
    try:
        import computer_use as cu
        return {"ok": True, "actions": cu.recent_actions(int(limit or 60), kind or "")}
    except Exception as e:
        return {"ok": False, "msg": "读日志失败：%s" % e}


def agent_clear_log(self):
    try:
        import computer_use as cu
        return cu.clear_actions()
    except Exception as e:
        return {"ok": False, "msg": "清空失败：%s" % e}


def agent_shot(self, path=""):
    """把截图读成 data URL 给前端显示（只允许读 agent_shots 目录里的文件）。"""
    try:
        import base64
        import computer_use as cu
        p = str(path or "")
        root = cu.shots_dir()
        if not p or not os.path.isfile(p) or not os.path.abspath(p).startswith(os.path.abspath(root)):
            return {"ok": False, "msg": "没有这张图"}
        with open(p, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        return {"ok": True, "url": "data:image/png;base64," + data, "path": p}
    except Exception as e:
        return {"ok": False, "msg": "读图失败：%s" % e}


def agent_emergency_stop(self):
    try:
        import computer_use as cu
        return cu.stop_now("界面上的紧急停止")
    except Exception as e:
        return {"ok": False, "msg": "停止失败：%s" % e}


def agent_reset_stop(self):
    try:
        import computer_use as cu
        return cu.reset_stop()
    except Exception as e:
        return {"ok": False, "msg": "恢复失败：%s" % e}


def agent_test_tool(self, tool="", args=None):
    """试一下某个工具（界面上「试一试」按钮，方便排障，不用真的等聊天）。"""
    try:
        import agent as _agent
        import computer_use as cu
        if _agent._live_cfg("human_approval", True) and str(tool) in _agent.AGENT_DANGER_TOOLS:
            cu.log_action("agent", "try_tool", {"tool": tool, "args": args}, True, 0, "界面手动试")
        return _agent._agent_exec(tool, args or {}, "ui-test")
    except Exception as e:
        return {"ok": False, "msg": "试不动：%s" % e}


def agent_call_now(self, kind="voice", name=""):
    try:
        import qq_call
        return qq_call.dial(kind, name, session="ui")
    except Exception as e:
        return {"ok": False, "msg": "打不出去：%s" % e}


def agent_hangup(self):
    try:
        import qq_call
        return qq_call.hangup(True, session="ui")
    except Exception as e:
        return {"ok": False, "msg": "挂不断：%s" % e}


def agent_call_state(self):
    try:
        import qq_call
        return qq_call.state()
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def agent_pending_approval(self):
    """v1.5.2：界面轮询「有没有危险动作在等我确认」。"""
    try:
        import approval_bus as ab
        return ab.pending()
    except Exception as e:
        return {"ok": False, "items": [], "count": 0, "msg": "读不到待确认：%s" % e}


def agent_answer_approval(self, rid="", allow=False):
    """v1.5.2：用户点【允许执行】/【拒绝并停止】。"""
    try:
        import approval_bus as ab
        def _t(v):
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on", "allow")
            return bool(v)
        r = ab.answer(str(rid or ""), _t(allow))
        _append_log("[Agent] 危险动作确认：%s → %s" % (rid, "允许" if _t(allow) else "拒绝"))
        return r
    except Exception as e:
        return {"ok": False, "msg": "提交确认失败：%s" % e}


for _ag_name, _ag_fn in (
    ("agent_status", agent_status), ("agent_run_task", agent_run_task),
    ("agent_task_status", agent_task_status), ("agent_actions", agent_actions),
    ("agent_clear_log", agent_clear_log), ("agent_shot", agent_shot),
    ("agent_emergency_stop", agent_emergency_stop), ("agent_reset_stop", agent_reset_stop),
    ("agent_test_tool", agent_test_tool), ("agent_call_now", agent_call_now),
    ("agent_hangup", agent_hangup), ("agent_call_state", agent_call_state),
    ("agent_gate_status", agent_gate_status), ("agent_gate_issue", agent_gate_issue),
    ("agent_gate_enable", agent_gate_enable), ("agent_gate_disable", agent_gate_disable),
    ("agent_share", agent_share),
    ("agent_pending_approval", agent_pending_approval),
    ("agent_answer_approval", agent_answer_approval),
):

    setattr(Api, _ag_name, _ag_fn)

setattr(Api, "perf_ack", perf_ack)  # v1.6.0 性能预警确认（签名已兼容 self）



# ==================== v1.5.1：token 消耗预估 ====================
def get_token_estimate(*_ignored, **_kw):
    """每一项功能开/关时的 token 开销 + 现在综合起来一天大约多少。

    v1.6.0：默认关闭 —— 关着的时候连 token_est 都不 import，
    统计逻辑完全不参与运行（不是「算了再隐藏」）。
    """
    try:
        if not bool(_cfg_mod.get("token_est_enabled", False)):
            return {"ok": False, "disabled": True,
                    "msg": "消耗预估默认关着；想看得去设置里打开『消耗预估』"}
        import token_est
        r = token_est.summary()
        try:
            _append_log("[消耗预估] 当前开启 %d/%d 项，约 %d token/天"
                        % (r.get("count_enabled", 0), r.get("count_total", 0),
                           r.get("enabled_day", 0)))
        except Exception:
            pass
        return r
    except Exception as e:
        try:
            _append_log("[消耗预估] 失败: %s" % e)
        except Exception:
            pass
        return {"ok": False, "msg": "预估失败：%s" % e, "rows": [],
                "enabled_day": 0, "enabled_month": 0, "all_day": 0, "all_month": 0,
                "count_enabled": 0, "count_total": 0, "chips": 1.6, "note": ""}

for _av_name, _av_fn in (
    ("av_status", av_status), ("av_start", av_start), ("av_stop", av_stop),
    # v1.6.1（QA P0-1）：开关即生命周期，前端只调这一个
    ("av_set_enabled", av_set_enabled),
    ("av_log", av_log), ("av_speak", av_speak), ("av_speak_stop", av_speak_stop),
    ("av_listen", av_listen), ("av_listen_start", av_listen_start),
    ("av_listen_stop", av_listen_stop), ("av_listen_status", av_listen_status),
    ("av_remote_status", av_remote_status), ("av_voices", av_voices),
    ("av_devices", av_devices), ("av_drivers", av_drivers),
    ("av_stream_start", av_stream_start), ("av_stream_image", av_stream_image),
    ("av_stream_stop", av_stream_stop), ("av_stream_caption", av_stream_caption),
    ("av_preview", av_preview), ("av_rpa_start", av_rpa_start),
    ("av_rpa_stop", av_rpa_stop), ("av_rpa_status", av_rpa_status),
    ("av_rpa_scan", av_rpa_scan), ("av_events", av_events),
    # v1.6.3（QA P1-3）：查模板在不在，前端据此提示「请先截取接听按钮模板」
    ("av_rpa_templates", av_rpa_templates),
    # V1.7.0：签署审计留痕（强模态签署 → 数据库，不可改的哈希链）
    ("audit_sign_accept", audit_sign_accept),
    ("audit_sign_state", audit_sign_state),
    ("audit_sign_list", audit_sign_list),
    ("audit_sign_verify", audit_sign_verify),
    ("audit_device_info", audit_device_info),
    # V1.8.0：自定义音色（声音克隆），失败在后端静默降级 edge-tts
    ("av_tts_custom", av_tts_custom),
    ("av_tts_custom_test", av_tts_custom_test),
    ("kg_overview", kg_overview),
    ("kg_build_now", kg_build_now),
):
    setattr(Api, _av_name, _av_fn)

setattr(Api, "get_token_estimate", get_token_estimate)



# ==================== v1.8.4：进程清道夫（运行环境维护）====================
# 用户实机反馈：软件用完退出后，后台会残留一堆 node.exe（NapCat 的进程），
# 越积越多，最后几十个实例一起抢 3003/3004，表现就是「QQ 怎么都登不上」。
#
# 三条防线（从根上治，不是事后擦了就算）：
#   1) napcat_proc.attach_job()  —— 拉起的 node 全挂进 KILL_ON_JOB_CLOSE 的
#      Job 对象，主程序一死内核自动回收，不依赖「记得清理」；
#   2) 这里的 kill_nodes_force() —— 给「已经残留在机器上的」兜底清理；
#   3) 退出时由 Tauri 壳先请求一次 kill_nodes_force，再停后端（lib.rs）。
#
# 安全约定（用户明确要求）：**绝不 taskkill /IM node.exe**。
# 只杀命令行/路径里带 napcat 的那些，判据见 napcat_proc.explain()。
def _nproc():
	"""懒加载进程清道夫模块（没打进去也只是少了清理能力）。"""
	import napcat_proc
	return napcat_proc


def _napcat_dir():
	try:
		import napcat_mgr
		return napcat_mgr.find_install_dir() or ""
	except Exception:
		return ""


def napcat_node_status(self=None):
	"""看一眼本机有哪些 node.exe、哪些是我们的 NapCat（只读）。"""
	try:
		np = _nproc()
		sc = np.scan_napcat_nodes(_napcat_dir())
		return {
			"ok": True,
			"total": sc["total"],
			"ours": [{"pid": x["pid"], "why": x["why"], "cmd": (x["cmd"] or "")[:200]} for x in sc["ours"]],
			"kept": [{"pid": x["pid"], "cmd": (x["cmd"] or "")[:200]} for x in sc["kept"]],
			"denied": [{"pid": x["pid"]} for x in sc["denied"]],
			"job": np.job_status(),
			"psutil": bool(sc.get("psutil", True)),
		}
	except Exception as e:
		return {"ok": False, "msg": "扫描 node 进程失败：%s: %s" % (type(e).__name__, e)}


def kill_nodes_force(self=None, wait=2.5):
	"""一键清理残留的 NapCat 进程（别的 node 一个都不碰）。

	返回里带 need_admin：普通权限杀不掉时前端要弹「以管理员身份清理」。
	"""
	try:
		np = _nproc()
	except Exception as e:
		return {"ok": False, "msg": "清理模块没装进来：%s: %s" % (type(e).__name__, e)}
	try:
		r = np.kill_all_napcat_nodes(_napcat_dir(), wait=wait)
	except Exception as e:
		return {"ok": False, "msg": "清理失败：%s: %s" % (type(e).__name__, e)}
	try:
		_append_log("[清道夫] " + str(r.get("msg") or ""))
	except Exception:
		pass
	return r


def kill_nodes_elevated(self=None):
	"""普通权限关不掉时，请求 UAC 提权再清一遍（用户点「以管理员身份清理」走这里）。"""
	try:
		np = _nproc()
		ok, msg = np.elevate_cleanup()
		try:
			_append_log("[清道夫] 提权清理：" + str(msg))
		except Exception:
			pass
		return {"ok": bool(ok), "msg": msg}
	except Exception as e:
		return {"ok": False, "msg": "提权失败：%s: %s" % (type(e).__name__, e)}


def napcat_restart_now(self=None, wait=30):
	"""立刻重启 NapCat：先清干净，再重新拉一个（能快速登录就直接连）。"""
	try:
		import napcat_mgr
	except Exception as e:
		return {"ok": False, "msg": "napcat_mgr 不可用：%s" % e}
	d = _napcat_dir()
	if not d:
		return {"ok": False, "msg": "还没找到 NapCat 目录。先在「QQ 接入」卡片里安装或指定目录。"}
	cleaned = ""
	try:
		np = _nproc()
		r = np.kill_all_napcat_nodes(d, wait=2.0)
		cleaned = str(r.get("msg") or "")
	except Exception:
		try:
			_c, cleaned = napcat_mgr.stop_all(d, wait=2.0)
		except Exception:
			cleaned = ""
	try:
		ok, msg = napcat_mgr.launch(d, wait=int(wait), restart=True)
	except Exception as e:
		return {"ok": False, "msg": "重启失败：%s: %s" % (type(e).__name__, e), "cleaned": cleaned}
	try:
		_append_log("[NapCat] 重启：%s" % msg)
	except Exception:
		pass
	return {"ok": bool(ok), "msg": msg, "cleaned": cleaned}

# v1.8.4：进程清道夫（运行环境维护）—— 挂到 Api 上给前端 call() 用
setattr(Api, "napcat_node_status", napcat_node_status)
setattr(Api, "kill_nodes_force", kill_nodes_force)
setattr(Api, "kill_nodes_elevated", kill_nodes_elevated)
setattr(Api, "napcat_restart_now", napcat_restart_now)


# v1.9.8：修复工具的临时残渣清理。
# 恢复官方安装.ps1 每跑一次会在 %TEMP% 留下 xjy-repair-<时间戳>（解包出来的
# 几百兆）和 xjy-self-data-<时间戳>，QA 实测攒到 1.64 GB 把磁盘撑满。
# 脚本那边已经改成跑完必删；这里再给用户一个手动入口，顺手扫掉历史残渣。
_TMP_JUNK_PREFIXES = ("xjy-repair-", "xjy-self-data-", "xjy-inst-check-")


def _scan_tmp_junk():
	"""%TEMP% 下我们自己留下的目录/文件，返回 [(path, bytes)]。"""
	import tempfile
	out = []
	try:
		root = tempfile.gettempdir()
		names = os.listdir(root)
	except Exception:
		return out
	for n in names:
		low = n.lower()
		hit = (low.startswith("xjy-repair-") or low.startswith("xjy-self-data-")
		       or low.startswith("xjy-inst-check-")
		       or (low.startswith("xjy-self-") and low.endswith(".txt")))
		if not hit:
			continue
		p = os.path.join(root, n)
		size = 0
		try:
			if os.path.isdir(p):
				for dp, _dn, fns in os.walk(p):
					for fn in fns:
						try:
							size += os.path.getsize(os.path.join(dp, fn))
						except OSError:
							pass
			else:
				size = os.path.getsize(p)
		except OSError:
			size = 0
		out.append((p, size))
	return out


def repair_temp_status(self=None):
	"""设置页显示：修复工具在临时目录里还剩多少东西。"""
	items = _scan_tmp_junk()
	total = sum(s for _p, s in items)
	return {"ok": True, "count": len(items), "bytes": total,
	        "mb": round(total / 1048576.0, 1),
	        "names": [os.path.basename(p) for p, _s in items][:20]}


def clean_repair_temp(self=None):
	"""删掉 %TEMP% 下的 xjy-repair-* / xjy-self-data-* / xjy-self-*.txt。"""
	import shutil
	items = _scan_tmp_junk()
	if not items:
		return {"ok": True, "msg": "没有需要清理的临时文件", "freed_mb": 0.0,
		        "count": 0, "left": []}
	freed = 0
	left = []
	for p, s in items:
		try:
			if os.path.isdir(p):
				shutil.rmtree(p, ignore_errors=True)
			else:
				os.remove(p)
		except Exception:
			pass
		if os.path.exists(p):
			left.append(os.path.basename(p))
		else:
			freed += s
	n = len(items) - len(left)
	msg = "已清理 %d 个临时文件/目录，释放 %.1f MB" % (n, freed / 1048576.0)
	if left:
		msg += "；还有 %d 个删不掉（可能有程序占着）：%s" % (len(left), "、".join(left[:3]))
	try:
		_append_log("[安全] " + msg)
	except Exception:
		pass
	return {"ok": True, "msg": msg, "freed_mb": round(freed / 1048576.0, 1),
	        "count": n, "left": left}


setattr(Api, "repair_temp_status", repair_temp_status)
setattr(Api, "clean_repair_temp", clean_repair_temp)






# v1.9.4（抢修二②）：改注入防护开关的唯一入口。
# 普通 save_settings 对 guard_* 的改动会被 config.save() 退回（防「直接调接口一关了之」），
# 真要关必须走这里：先写审计（时间/改了什么/谁改的），再落盘。
def set_guard(self=None, values=None, note=""):
	"""payload = {"values": {guard_enabled: false, ...}, "note": "为什么关"}

	pywebview 那条路会把整个 dict 塞进第一个位置参数（也就是 self），
	HTTP 那条路按关键字传（values/note）—— 两种都认，免得像 V1.8.2 那样
	因为签名对不上就静默失效。
	"""
	try:
		import config as _gcfg
		p = self if isinstance(self, dict) else {}
		vals = p.get("values") if isinstance(p.get("values"), dict) else None
		if vals is None and isinstance(values, dict) and isinstance(values.get("values"), dict):
			# pywebview 走位置参数时整个 payload 会落到 values 上（self 已被绑定吃掉）
			vals = values.get("values")
			note = values.get("note") or note
		if vals is None and isinstance(values, dict):
			vals = values
		if vals is None and p:
			vals = {k: v for k, v in p.items() if isinstance(v, bool)}
		nt = p.get("note") or note or "界面修改"
		clean = {}
		for k, v in (vals or {}).items():
			if k in _gcfg._GUARD_KEYS:
				clean[k] = bool(v)
		if not clean:
			return {"ok": False, "msg": "没有要改的防护项"}
		r = _gcfg.guard_apply(clean, note=str(nt)[:200], actor="ui")
		return {"ok": bool(r.get("ok", True)), "changed": r.get("changed") or {},
		        "msg": r.get("msg") or "防护设置已更新（已记入审计日志）"}
	except Exception as e:
		return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
setattr(Api, "set_guard", set_guard)
def _run_agent_mode():
    """被 start_agent() 以 `本程序 --agent` 拉起时，直接跑 Agent 主循环。"""
    try:
        sys.argv = [AGENT_PY]
        import agent
        agent.main()
    except Exception:
        try:
            with open(os.path.join(BASE_DIR, "agent_crash.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] --agent 模式崩溃\n"))
                f.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        raise


def main(argv=None):
    """v1.2.0：无头入口 —— 只启 HTTP/WS 服务，不再创建窗口。

    保留这个名字是为了兼容老调用（`python app_ui.py`、打包后双击 exe）：
    真正的实现在 backend_server.py（FastAPI）。
    """
    import backend_server
    return backend_server.main(argv)


if __name__ == "__main__":
    if "--agent" in sys.argv:
        # 打包后自举 Agent 子进程：`本程序 --agent`（逻辑没变）
        _run_agent_mode()
    else:
        # v1.2.0：--web / --webui 这些历史参数直接忽略 —— 后端本身就是网页/HTTP 服务
        main()
