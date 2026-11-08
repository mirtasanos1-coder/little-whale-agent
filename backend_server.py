# -*- coding: utf-8 -*-
"""小鲸鱼 Agent 后端服务（V1.2.0，无头 API）

设计目标
--------
1. **界面与业务彻底分开**：界面交给 Tauri（Rust + Web），Python 只做 HTTP/WS 服务。
   本文件不含任何窗口代码，服务器上也能跑（headless）。
2. **一个路由映射全部业务**：`app_ui.Api` 里的 271 个方法不重写，统一走
   `POST /api/{method}`；返回值保持老约定 `{"ok": bool, "msg": str, ...}`，
   老前端几乎不用改（配合 Tauri 前端的兼容层即可）。
3. **任何单个方法报错都不许带崩服务**：全部包 try-except，异常转成
   `{"ok": false, "msg": ...}` 并写进 agent.log。
4. **端口自适应**：从 5700 起探测；5700 是 Agent 的 OneBot 备用 webhook，
   所以会被跳过（正常拿到 5701）。启动时打印
   `[backend] listening on http://127.0.0.1:<port>`，Tauri 的 Rust 端抓这行即可。
5. **日志实时推送**：`ws://127.0.0.1:<port>/ws/logs` 增量 tail agent.log，
   支持断线重连（带 pos 续传）、日志轮转、从末尾 N 字节开始。

启动
----
    python backend_server.py                 # 默认 127.0.0.1 + 自动端口
    python backend_server.py --port 5701     # 指定端口
    python backend_server.py --host 0.0.0.0  # 局域网可见（默认只本机）
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import inspect
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _resolve_data_dir() -> str:
    """先算好数据目录，再 import 任何业务模块。

    V1.2.2 修：Tauri 打包后 backend_server.exe 在 <安装目录>\\resources\\ 里，
    sys.executable 的目录就是 resources —— config/memory/personas/i18n 这些模块
    各自按 sys.executable 算 DATA_DIR，于是配置、记忆库、人设全被写进
    resources\\（随程序一起被卸载/覆盖）。这里在导入前先把 QQ_AGENT_DATA_DIR
    钉成安装目录，后面所有模块都会走环境变量这条优先级最高的分支。
    """
    env_dir = (os.environ.get("QQ_AGENT_DATA_DIR") or "").strip()
    if env_dir:
        return env_dir
    if getattr(sys, "frozen", False):
        d = os.path.dirname(os.path.abspath(sys.executable))
        if os.path.basename(d).lower() == "resources":
            up = os.path.dirname(d)
            if os.path.isdir(up):
                return up
        return d
    return BASE_DIR


os.environ.setdefault("QQ_AGENT_DATA_DIR", _resolve_data_dir())

# 业务实现全部在 app_ui.py（V1.2.0 起它是纯无头模块，没有 pywebview）
import app_ui  # noqa: E402

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse  # noqa: E402

# V1.3.2（BUG-02）：版本号统一到 version.py，别再各写各的
try:
    from version import APP_VERSION, RELEASE_VERSION, APP_VERSION_TAG
except Exception:                      # 打包漏了 version.py 也不能起不来
    APP_VERSION = "1.3.3"
    RELEASE_VERSION = "1.3.3"
    APP_VERSION_TAG = "测试版"

# ==================== 端口 ====================
PORT_PREFERRED = 5700          # 从 5700 开始找
PORT_SCAN_SPAN = 40            # 最多往后找 40 个
# 5700 必须留给 Agent 的 OneBot 备用 webhook（agent.py: WEBHOOK_PORT = 5700），
# 后端服务抢了它，QQ 消息就进不来了 —— 所以这里硬保留。
# 5709 是 Agent 的单实例锁端口（agent.py: INSTANCE_LOCK_PORT），抢了它
# Agent 会误以为「已有另一个实例在跑」然后自己退出，也必须保留。
RESERVED_PORTS = {5700, 5709}


def _bind_port(port: int, host: str) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt":
            # Windows 上避免 TIME_WAIT 误判
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def find_free_port(preferred: int = PORT_PREFERRED, host: str = "127.0.0.1",
                   span: int = PORT_SCAN_SPAN,
                   reserved: Optional[set] = None) -> int:
    """从 preferred 起找一个能 bind 的端口（跳过保留位）。

    全都被占了就让系统随便给一个（0 端口），保证服务总能起来。
    """
    reserved = set(reserved if reserved is not None else RESERVED_PORTS)
    for p in range(int(preferred), int(preferred) + max(1, int(span))):
        if p in reserved:
            continue
        if _bind_port(p, host):
            return p
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, 0))
        return int(s.getsockname()[1])
    finally:
        try:
            s.close()
        except Exception:
            pass


# ==================== runtime/ 目录（前端把下载物都放这里）====================
RUNTIME_DIR = os.path.join(app_ui.DATA_DIR, "runtime")
RUNTIME_NAPCAT = os.path.join(RUNTIME_DIR, "napcat")
RUNTIME_CACHE = os.path.join(RUNTIME_DIR, "cache")
RUNTIME_MODELS = os.path.join(RUNTIME_DIR, "models")
RUNTIME_PY = os.path.join(RUNTIME_DIR, "python")


def _ensure_runtime_dirs() -> List[str]:
    out = []
    for d in (RUNTIME_DIR, RUNTIME_NAPCAT, RUNTIME_CACHE, RUNTIME_MODELS):
        try:
            os.makedirs(d, exist_ok=True)
            out.append(d)
        except Exception:
            pass
    return out


def tts_model_dir(mkdir: bool = True) -> str:
    """语音模型/缓存放哪。

    v1.2.1：默认跟着数据目录走（打包版 = 安装目录\\runtime\\models\\tts），
    不往 C 盘用户目录里塞。用户嫌 D 盘满/想放 E 盘，可以在「设置 → 语音」
    里改 tts_model_dir，这里就认那个值。
    """
    try:
        import config as _cfg
        d = str(_cfg.get("tts_model_dir", "") or "").strip().strip('"')
    except Exception:
        d = ""
    if not d:
        d = os.path.join(RUNTIME_MODELS, "tts")
    if mkdir:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    return d


# ==================== 通用方法分发 ====================
_API_LOCK = threading.Lock()
_API_SINGLETON = None

# 不对外开放的名字：内部方法、以及容易把服务自己弄死的东西
_DENY_METHODS = {"main", "_run_agent_mode", "restart_app", "_hard_exit"}

# 破坏性方法：不可逆、或者会把用户数据/程序本身干掉。
# 光靠「只监听 127.0.0.1」不够 —— 用户浏览器里随便一个网页都能往本机端口发 POST。
#
# v1.0.0-Beta 安全整改（QA BUG-04）：以前是「后端随机生成一个长期口令，放 /api/health 里明文下发」，
# 只要任意本机网页读到 health（CORS 白名单过宽就读得到）就能拿着口令去卸载程序。
# 现在改成**带外一次性票据**：前端先 POST /api/confirm/ticket {method} 换一张 30 秒一次性票据，
# 真正调用时带 X-Confirm-Ticket: <nonce>；health 里不再下发任何口令。
# 再配合「非 GET 必须 application/json + Origin 只认 Tauri 自己 + 禁止 cross-site」，把 CSRF 面收掉。
_DANGER_METHODS = {
    "run_uninstall",          # 卸载程序
    "apply_update",           # 应用更新（覆盖自身文件）
    "rollback_update",        # 回滚更新
    "memory_rebuild_run",     # 重建全部记忆
    "do_memory_import",       # 导入记忆（覆盖写）
    "do_memory_import_dir",
    "memory_import",
    "import_memory",
    "import_diary_backup",
}

# GET 分发只允许「纯读」方法（QA V1.3.2 终审 · 必修 1，P0）：
# 以前 GET /api/{method} 能带完整参数执行**任何**方法，于是恶意网页里一个
# <img src="http://127.0.0.1:5701/api/save_settings?args=[...]"> 就能改配置、设暗号、
# 把 cloud_base 指向攻击者、甚至停掉 Agent —— 而且浏览器发图片请求时根本不会带 Origin，
# 三道新防线（Origin / Sec-Fetch-Site / JSON）全都在 POST 分支里，一个都拦不到。
# 现在：只读方法（get_*/list_*/has_*/is_*/ping/health/methods/...）允许 GET，
# 其余一律 405。写操作必须走 POST + application/json（会触发预检，换来 Origin 校验）。
_READ_ONLY_METHODS = {
    "ping", "health", "methods", "web_state",
    "get_web_state", "confirm_ticket",
    # v1.5.2：界面轮询「有没有危险动作在等我确认」——纯读
    "agent_pending_approval",
}
_READ_ONLY_PREFIXES = ("get_", "list_", "has_", "is_", "count_")


def _is_read_only_method(name: str) -> bool:
    """这个方法能不能被 GET 调用？只认纯读语义。"""
    m = str(name or "").strip()
    if not m:
        return False
    if m in _READ_ONLY_METHODS:
        return True
    return m.startswith(_READ_ONLY_PREFIXES)


def api_instance():
    """全局唯一的 Api 实例（懒创建，线程安全）。

    为什么复用：Api 内部会缓存日志 offset、进程句柄、配置对象；
    每次请求新建一个会让「启动 Agent / 看日志 / 停止 Agent」互相看不见状态。
    """
    global _API_SINGLETON
    with _API_LOCK:
        if _API_SINGLETON is None:
            _API_SINGLETON = app_ui.Api()
        return _API_SINGLETON


def list_methods() -> List[str]:
    try:
        inst = api_instance()
    except Exception:
        return []
    names = []
    for n in dir(inst):
        if n.startswith("_"):
            continue
        try:
            v = getattr(inst, n)
        except Exception:
            continue
        if callable(v):
            names.append(n)
    return sorted(names)


def _bind_args(fn, payload: Any) -> Tuple[list, dict]:
    """把 HTTP body 变成 (args, kwargs)。

    老前端是 `window.pywebview.api.xxx(a, b)` 这种**位置参数**调用，
    兼容层多半会原样塞进 body，所以这里三种形式都认：
      - {"args": [...], "kwargs": {...}}   —— 显式写法
      - [...]                              —— 纯位置参数
      - {"key": value, ...}                —— 名字对得上就用关键字，对不上按顺序当位置参数
      - "字符串"/数字                       —— 单参数
    """
    try:
        sig = inspect.signature(fn)
        params = {k: v for k, v in sig.parameters.items()
                  if v.kind in (v.POSITIONAL_OR_KEYWORD, v.KEYWORD_ONLY)}
    except (TypeError, ValueError):
        params = {}

    if isinstance(payload, dict):
        if "args" in payload or "kwargs" in payload:
            a = payload.get("args") or []
            k = payload.get("kwargs") or {}
            if not isinstance(a, (list, tuple)):
                a = [a]
            if not isinstance(k, dict):
                k = {}
            return list(a), dict(k)
        if "value" in payload and len(payload) == 1:
            return [payload["value"]], {}
        if params and all(kk in params for kk in payload.keys()):
            return [], dict(payload)
        if not params:
            return [], {}
        return list(payload.values()), {}
    if isinstance(payload, (list, tuple)):
        return list(payload), {}
    if payload is None:
        return [], {}
    return [payload], {}


# v1.9.4（抢修三）：沙箱里只读。下面这些方法碰的是屏幕、QQ、文件系统，
# v1.9.5（P0）：拦截逻辑在 call_method() 里（下面这行注释以前跟着代码一起被
# 误插到了 _bind_args 的 return 之后，等于没拦；已挪正）。

# v1.9.8-Beta：小批量对外测试期间，视频/推流相关接口整体停用（前端也遮了，这里是防绕过的兜底）
_UNDER_MAINTENANCE = (
    "av_stream_start", "av_stream_stop", "av_stream_image", "av_stream_caption",
    "av_video_start", "av_video_stop", "av_video_status", "av_camera_start",
)
_MAINT_MSG = "视频功能正在升级，暂时关闭，请期待后续更新。"
_LIMITED_BLOCK = (
    "napcat_", "rpa_", "screen_", "click_", "computer_", "browser_",
    "chatgpt_web", "qzone_", "moment_", "selfmod_", "sing_",
    "shell_", "run_", "open_", "install_", "uninstall_",
)


def _limited_block(name: str):
    """受限模式下要不要拦这个方法；返回 None = 放行。

    v1.9.5：改成 fail-closed —— 环境检测自己出错时，宁可按“受限”处理，
    也不能放行（以前 import/检测一抛异常就 return None，等于把门开着）。
    """
    low = str(name or "").lower()
    try:
        if _is_read_only_method(low):
            return None
    except Exception:
        pass
    try:
        import sandbox as _sb
        if not _sb.limited():
            return None
    except Exception as e:
        _log("[受限] 环境检测失败（%s: %s），按受限处理" % (type(e).__name__, e), "WARN")
        return {"ok": False, "code": "env_abnormal",
                "msg": "环境检测异常", "limited": True}
    for pre in _LIMITED_BLOCK:
        if low.startswith(pre):
            return {"ok": False, "code": "env_abnormal",
                    "msg": "当前环境异常，这一步已跳过（受限模式）。",
                    "limited": True}
    return None


def call_method(name: str, payload: Any = None, confirm: bool = False) -> Dict[str, Any]:
    """调用 Api 的一个方法，永远返回 dict（绝不抛异常）。

    confirm=True 表示调用方出示了有效的一次性票据（X-Confirm-Ticket，见 _DANGER_METHODS）。
    """
    if not name or name.startswith("_") or name in _DENY_METHODS:
        return {"ok": False, "msg": f"不允许调用: {name}"}
    if name in _DANGER_METHODS and not confirm:
        _log(f"[backend] 拒绝未确认的破坏性调用: {name}", "WARN")
        return {"ok": False, "danger": name,
                "msg": f"{name} 是不可逆操作，需要先在界面上确认（一次性票据），"
                       f"别用浏览器地址栏直接敲"}
    # v1.9.5（P0）：沙箱受限闸门。必须在这里（getattr 之前）——
    # 上一版这四行被误插进 _bind_args 的 return 之后，是死代码，实测
    # XJY_FORCE_VM=1 时危险接口照样执行。
    _lim = _limited_block(name)
    if _lim is not None:
        _log("[受限] 沙箱环境，已跳过：%s" % name, "WARN")
        return _lim

    # v1.9.8-Beta：视频功能维护中（返回结构化错误，前端/Agent 都能识别）
    if name in _UNDER_MAINTENANCE:
        _log("[维护] 视频相关调用已拦截：%s" % name, "WARN")
        return {"ok": False, "code": "under_maintenance", "msg": _MAINT_MSG, "method": name}
    try:
        inst = api_instance()
    except Exception as e:
        return {"ok": False, "msg": f"Api 初始化失败: {type(e).__name__}: {e}"}
    fn = getattr(inst, name, None)
    if fn is None or not callable(fn):
        return {"ok": False, "msg": f"后端缺少方法: {name}", "method": name}

    args, kwargs = _bind_args(fn, payload)
    try:
        res = fn(*args, **kwargs)
    except TypeError as e:
        # 参数对不上（老前端传错/少传）→ 退回无参调用，尽量别让界面卡住
        try:
            res = fn()
        except Exception:
            _log(f"[backend] {name} 参数不匹配: {type(e).__name__}: {e}", "ERROR")
            return {"ok": False, "msg": f"{name} 参数不匹配: {e}", "method": name}
    except Exception as e:
        _log(f"[backend] {name} 异常: {type(e).__name__}: {e}", "ERROR")
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "method": name,
                "trace": traceback.format_exc(limit=3)}

    if isinstance(res, dict):
        out = dict(res)
        out.setdefault("ok", True)
        return out
    return {"ok": True, "data": res}


def _log(line: str, level: str = "INFO") -> None:
    try:
        app_ui._append_log(line, level)
    except Exception:
        pass


# ==================== 插件 / 环境探测 ====================
def _napcat_status() -> Dict[str, Any]:
    """NapCat（QQ 接入端）状态：装没装、跑没跑、配置写了没。"""
    out = {"key": "napcat", "name": "NapCat（QQ 接入端）", "installed": False,
           "running": False, "dir": "", "launcher": "", "ws_port": 0,
           "msg": "", "ok": False, "config_ok": False, "webui": ""}
    try:
        import napcat_mgr
        p = napcat_mgr.probe(deep=False) or {}
        out.update({
            "ok": True,
            "running": bool(p.get("running")),
            "http_ok": bool(p.get("http_ok")),
            "ws_ok": bool(p.get("ws_ok")),
            "ws_port": p.get("ws_port") or 0,
            "dir": p.get("dir") or "",
            "launcher": p.get("launcher") or "",
            "msg": p.get("msg") or "",
        })
        out["installed"] = bool(p.get("dir") or p.get("launcher"))
        try:
            d = p.get("dir") or napcat_mgr.find_install_dir() or ""
            if d:
                cdir = napcat_mgr.onebot_config_dir(d)
                if os.path.isdir(cdir):
                    files = [f for f in os.listdir(cdir) if f.startswith("onebot11")]
                    out["config_ok"] = bool(files)
                    out["config_files"] = sorted(files)[:6]
                wi = napcat_mgr.webui_info(d)
                out["webui"] = wi.get("url", "")
        except Exception:
            pass
    except Exception as e:
        out["msg"] = f"检测失败: {type(e).__name__}: {e}"
    # v1.2.1：把「下不动怎么办」的两条路也一起报给前端，前端不用再写死链接
    try:
        import napcat_mgr
        out["official_page"] = getattr(napcat_mgr, "NAPCAT_RELEASES_PAGE", "")
        out["mirrors"] = [m[0] for m in getattr(napcat_mgr, "NAPCAT_MIRRORS", [])]
        out["can_import_zip"] = True
        # v1.2.2：随程序带的离线包（有它就不用联网，也不再需要镜像）
        bi = napcat_mgr.bundled_info()
        out["bundled"] = bool(bi.get("ok"))
        out["bundled_text"] = bi.get("text") or ""
        out["bundled_version"] = bi.get("version") or ""
        out["can_install_offline"] = bool(bi.get("ok"))
    except Exception:
        pass
    return out


_EXT_PROBE_CACHE: Dict[str, Any] = {}


def _probe_edge_tts(py: str, timeout: float = 12.0):
    """问另一个解释器「你能不能 import edge_tts」。结果缓存 30 秒。

    只为了把话说准：pip 装成功 ≠ 当前进程能用（打包版与它不是一个解释器）。
    """
    if not py:
        return False, ""
    key = os.path.abspath(py)
    hit = _EXT_PROBE_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < 30:
        return hit[1], hit[2]
    ok, ver = False, ""
    try:
        flags = 0
        if os.name == "nt":
            flags = 0x08000000  # CREATE_NO_WINDOW
        p = subprocess.run(
            [py, "-c", "import edge_tts;print(getattr(edge_tts,'__version__','') or 'ok')"],
            capture_output=True, timeout=timeout, creationflags=flags)
        if p.returncode == 0:
            ok = True
            ver = (p.stdout or b"").decode("utf-8", "replace").strip()
    except Exception:
        ok = False
    _EXT_PROBE_CACHE[key] = (now, ok, ver)
    return ok, ver


def _tts_status() -> Dict[str, Any]:
    """TTS（edge-tts 语音引擎）状态。"""
    out = {"key": "tts", "name": "语音引擎（edge-tts）", "installed": False,
           "enabled": False, "version": "", "msg": "", "pip": "", "hint": "",
           "can_auto_install": False, "pip_ok": False, "bundled": False}
    try:
        spec = importlib.util.find_spec("edge_tts")
        out["installed"] = spec is not None
        if spec is not None:
            try:
                import edge_tts  # noqa: F401
                out["version"] = str(getattr(edge_tts, "__version__", "") or "")
            except Exception:
                out["version"] = ""
        try:
            import config as _cfg
            out["enabled"] = bool(_cfg.get("tts_enabled", False))
        except Exception:
            out["enabled"] = False
        out["pip"] = _pip_python() or ""
        frozen = bool(getattr(sys, "frozen", False))
        # 打包版（frozen）用的是内置解释器：pip 装到别的 python 里它 import 不到，
        # 所以打包版只能「内置」，不能靠向导 pip 装。这里把两种情形分开说清楚，
        # 免得出现「pip 说装完了但仍然未安装」的鬼打墙。
        ext_ok, ext_ver = (False, "")
        if not out["installed"] and out["pip"]:
            ext_ok, ext_ver = _probe_edge_tts(out["pip"])
        out["can_auto_install"] = bool(not frozen and not out["installed"])
        out["pip_ok"] = bool(ext_ok)
        if not out["installed"]:
            if frozen:
                out["hint"] = "打包版用的是内置解释器，需要重新打包时把语音引擎一起打进去（打包后端.bat 已默认包含）"
                out["msg"] = "打包版没内置语音引擎（文字聊天不受影响）"
                if ext_ok:
                    out["hint"] = ("检测到你本机 Python 里已经装了 edge-tts（%s），"
                                   "但打包版用的是自带的解释器，需要重新打包才生效" % (ext_ver or "版本未知"))
            elif ext_ok:
                out["hint"] = "本机 Python 里已经有 edge-tts（%s），重启后端即可" % (ext_ver or "版本未知")
                out["msg"] = "edge-tts 已装在本机 Python，重启后端后生效"
            else:
                out["hint"] = "点「一键安装」会自动 pip install edge-tts；或者自己装好后重启后端"
                out["msg"] = "没装 edge-tts（语音回复不可用，文字聊天不受影响）"
        else:
            # v1.2.1：打包版能 import 到 edge_tts = 已经内置进 exe 了。
            # 这时要明确告诉用户「不用装、不联网、不写 C 盘」，
            # 免得他去点「一键安装」再被 pip 装到 AppData 里。
            out["bundled"] = bool(frozen)
            out["can_auto_install"] = False
            if out["bundled"]:
                out["msg"] = ("edge-tts 已内置（%s），不用安装，也不占系统环境"
                              % (out["version"] or "版本未知"))
                out["hint"] = "打包时已经把它打进程序里了，开箱即用"
            else:
                out["msg"] = "edge-tts 已安装" + (f"（{out['version']}）" if out["version"] else "")
    except Exception as e:
        out["msg"] = f"检测失败: {type(e).__name__}: {e}"
    return out


def _pip_python() -> str:
    """找一个能 `python -m pip` 的解释器。

    优先 `runtime/python/python.exe`（便携版，打包后也能装包），
    其次系统 python / py 启动器，最后才是当前解释器。
    """
    cands = []
    for name in ("python.exe", "python3.exe"):
        p = os.path.join(RUNTIME_PY, name)
        if os.path.isfile(p):
            cands.append(p)
    for name in ("python", "python3"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    if not getattr(sys, "frozen", False):
        cands.append(sys.executable)
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return ""


def _node_status() -> Dict[str, Any]:
    """Node（NapCat 纯 shell 版需要）。

    除了 PATH，还要看几个常见安装位置 —— 从桌面图标（资源管理器）启动的程序，
    继承的 PATH 常常没有 nodejs，用户明明装了却报「没找到 node」。
    """
    p = shutil.which("node") or ""
    if not p:
        cands = []
        for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            root = os.environ.get(key) or ""
            if root:
                cands.append(os.path.join(root, "nodejs", "node.exe"))
        local = os.environ.get("LOCALAPPDATA") or ""
        if local:
            cands.append(os.path.join(local, "Programs", "nodejs", "node.exe"))
        cands.append(os.path.join(RUNTIME_DIR, "node", "node.exe"))
        for c in cands:
            if os.path.isfile(c):
                p = c
                break
    ver = ""
    if p:
        try:
            r = subprocess.run([p, "-v"], capture_output=True, text=True, timeout=2.5,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ver = (r.stdout or "").strip()
        except Exception:
            ver = ""
    return {"key": "node", "name": "Node.js（NapCat 依赖）", "installed": bool(p),
            "path": p, "version": ver,
            "msg": ("Node " + ver) if p else "没找到 node（NapCat 纯 shell 版需要它）"}


# ==================== v1.8.7（QA P0）：插件探测缓存 ====================
# 为什么要有它：/api/health 以前每次都现查 NapCat / edge-tts / Node，
# 没装 NapCat 的机器上一次要 2 秒多。桌面壳拿 /api/ping 探活之后这已经不致命，
# 但设置页 / 首页 / 外部监控都还在轮询 /api/health，现查就是白烧 CPU。
# 现在：进程内缓存 60 秒，后台线程每 45 秒刷一次；请求只读缓存，永不阻塞。
_HEALTH_CACHE: Dict[str, Any] = {}
_HEALTH_TS = 0.0
_HEALTH_LOCK = threading.Lock()
_HEALTH_BUSY = {"v": False}
_HEALTH_TTL = 60.0


def _probe_plugins() -> Dict[str, Any]:
    return {"napcat": _napcat_status(), "tts": _tts_status(), "node": _node_status()}


def _refresh_plugins():
    """（后台线程用）重新探一遍三个插件，结果放缓存。"""
    global _HEALTH_TS
    try:
        data = _probe_plugins()
        with _HEALTH_LOCK:
            _HEALTH_CACHE.clear()
            _HEALTH_CACHE.update(data)
            _HEALTH_TS = time.time()
        return data
    except Exception as e:
        return {"_err": "%s: %s" % (type(e).__name__, e)}


def _health_plugins(force: bool = False) -> Dict[str, Any]:
    """拿插件状态：有缓存就直接给（几十微秒），过期就丢给后台线程去刷。"""
    now = time.time()
    with _HEALTH_LOCK:
        have = bool(_HEALTH_CACHE)
        fresh = have and (now - _HEALTH_TS) < _HEALTH_TTL
        snap = dict(_HEALTH_CACHE)
    if fresh and not force:
        return snap
    if not have:
        # 冷启动这一次也不许阻塞（QA 要求 < 200ms）：先给空，让前端显示「检测中…」，
        # 背地里立刻起线程去探，下一次请求就有真数据。
        with _HEALTH_LOCK:
            busy0 = bool(_HEALTH_BUSY["v"])
            if not busy0:
                _HEALTH_BUSY["v"] = True
        if not busy0:
            def _cold():
                try:
                    _refresh_plugins()
                finally:
                    with _HEALTH_LOCK:
                        _HEALTH_BUSY["v"] = False
            try:
                threading.Thread(target=_cold, daemon=True).start()
            except Exception:
                with _HEALTH_LOCK:
                    _HEALTH_BUSY["v"] = False
        return {}
    # 有缓存但过期了：先给旧的，后台去刷（用户不会因为探测慢而卡住）
    with _HEALTH_LOCK:
        busy = bool(_HEALTH_BUSY["v"])
        if not busy:
            _HEALTH_BUSY["v"] = True
    if not busy:
        def _bg():
            try:
                _refresh_plugins()
            finally:
                with _HEALTH_LOCK:
                    _HEALTH_BUSY["v"] = False
        try:
            threading.Thread(target=_bg, daemon=True).start()
        except Exception:
            with _HEALTH_LOCK:
                _HEALTH_BUSY["v"] = False
    return snap


def _health_warmer():
    """后台预热线程：每 45 秒刷一次，保证 /api/health 永远是缓存命中。"""
    while True:
        try:
            _refresh_plugins()
        except Exception:
            pass
        time.sleep(45.0)


def start_health_warmer():
    """服务起来之前先叫它一声：第一次请求就不会撞上冷缓存。"""
    try:
        threading.Thread(target=_health_warmer, daemon=True).start()
    except Exception:
        pass




def _env_status() -> Dict[str, Any]:
    """给界面和冒烟看的：当前是不是沙箱受限模式。"""
    try:
        import sandbox as _sb
        return _sb.status()
    except Exception as e:
        return {"vm": False, "limited": False, "why": str(e)}


def health_payload() -> Dict[str, Any]:
    """健康检查：后端状态 + 当前模型 + 本地插件状态。

    结构固定，前端可以照着渲染；任何一段探测失败都只是把该段标成异常，
    不会让整个 /api/health 500。
    """
    st: Dict[str, Any] = {}
    try:
        st = app_ui.get_status() or {}
    except Exception as e:
        st = {"ok": False, "msg": f"{type(e).__name__}: {e}"}

    plugins = _health_plugins()
    if not plugins:
        plugins = {"napcat": {"key": "napcat", "name": "NapCat（QQ 接入端）",
                              "installed": False, "msg": "检测中…"},
                          "tts": {"key": "tts", "name": "语音引擎（edge-tts）",
                                 "installed": False, "msg": "检测中…"},
                          "node": {"key": "node", "name": "Node.js（NapCat 依赖）",
                                  "installed": False, "msg": "检测中…"}}
    missing = [p["name"] for p in plugins.values()
               if not p.get("installed") and p.get("key") in ("napcat", "tts")]

    model = {
        "provider": st.get("provider", ""),
        "provider_name": st.get("provider_name", ""),
        "model": st.get("model", ""),
        "base": st.get("lm_base", ""),
        "is_cloud": bool(st.get("is_cloud")),
        "ready": bool(st.get("model")),
    }

    return {
        "ok": True,
        "backend": {
            "status": "running",
            "env": _env_status(),
            "version": APP_VERSION,
            "host": SERVER["host"],
            "port": SERVER["port"],
            "url": f"http://{SERVER['host']}:{SERVER['port']}",
            "pid": os.getpid(),
            "python": sys.version.split()[0],
            "frozen": bool(getattr(sys, "frozen", False)),
            "data_dir": app_ui.DATA_DIR,
            "runtime_dir": RUNTIME_DIR,
            "log_file": app_ui.LOG_FILE,
            "uptime_sec": round(time.time() - SERVER["started"], 1),
            "methods": len(_METHODS_CACHE) if _METHODS_CACHE else 0,
            # QAv1.0.0-Beta BUG-04：这里**不再下发任何口令**。
            # 破坏性操作要用 /api/confirm/ticket 换一次性票据（30 秒、用完即焚）。
            "confirm_via": "ticket",
            "security": {
                "origin_lock": True,
                "json_only": True,
                "disclaimer_gate": _disclaimer_ok(),
            },
        },
        "agent": {
            "running": bool(st.get("running")),
            "pid": st.get("pid"),
            "webhook": "http://127.0.0.1:5700",
        },
        "app": {
            "name": st.get("title", "小鲸鱼 Agent 控制台"),
            "version": st.get("version", ""),
            "version_tag": st.get("version_tag", ""),
            "subtitle": st.get("subtitle", ""),
            "ui": "tauri",
        },
        "model": model,
        "plugins": plugins,
        "needs_setup": bool(missing),
        "setup_missing": missing,
        "ts": int(time.time()),
    }


# ==================== 长任务（下载 / pip install）====================
_JOBS: Dict[str, Dict[str, Any]] = {}
_JOB_LOCK = threading.Lock()
_JOB_SEQ = {"n": 0}


def _new_job(kind: str, title: str) -> Dict[str, Any]:
    with _JOB_LOCK:
        _JOB_SEQ["n"] += 1
        jid = f"{kind}-{int(time.time())}-{_JOB_SEQ['n']}"
        job = {"id": jid, "kind": kind, "title": title, "status": "pending",
               "pct": 0, "msg": "排队中", "lines": [], "ok": None,
               "started": time.time(), "finished": 0}
        _JOBS[jid] = job
        return job


def _job_log(job: Dict[str, Any], msg: str, pct: Optional[int] = None) -> None:
    msg = str(msg or "").strip()
    if not msg:
        return
    job["msg"] = msg
    job["lines"] = (job.get("lines") or [])[-199:] + [msg]
    if pct is not None:
        try:
            job["pct"] = max(0, min(100, int(pct)))
        except Exception:
            pass
    _log(f"[backend] {job['title']}：{msg}")


def _run_job(job: Dict[str, Any], fn) -> None:
    def _wrap():
        job["status"] = "running"
        try:
            r = fn(job)
            if isinstance(r, dict) and "ok" in r:
                job["ok"] = bool(r.get("ok"))
                job["result"] = r
            else:
                job["ok"] = True
                job["result"] = r
            job["status"] = "done"
            job["pct"] = 100
        except Exception as e:
            job["ok"] = False
            job["status"] = "failed"
            job["result"] = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
            _job_log(job, f"失败：{type(e).__name__}: {e}")
        finally:
            job["finished"] = time.time()
    threading.Thread(target=_wrap, name=f"job-{job['id']}", daemon=True).start()


def get_job(jid: str) -> Dict[str, Any]:
    with _JOB_LOCK:
        j = _JOBS.get(str(jid))
        return dict(j) if j else {}


def _pct_from_msg(msg: Any, lo: int = 5, hi: int = 92) -> Optional[int]:
    """从「正在下载 xxx：12.3 / 40.0 MB（31%）」这种文案里抠百分比。

    NapCat 的下载回调只给字符串（老接口），但向导要画进度条，
    所以在这里把 0~100% 映射到 lo~hi 区间的整体进度。
    """
    mt = re.search(r"[（(]\s*(\d{1,3})\s*[%％]", str(msg or ""))
    if not mt:
        return None
    try:
        p = int(mt.group(1))
    except Exception:
        return None
    p = max(0, min(100, p))
    return int(lo + p * (hi - lo) / 100)


# ==================== FastAPI ====================
SERVER = {"host": "127.0.0.1", "port": 0, "started": time.time()}
_METHODS_CACHE: List[str] = []

# ---- 一次性确认票据（QA BUG-04）----
# nonce -> {"method": 方法名, "exp": 过期时间戳, "origin": 来源, "ip": 客户端地址}
# 30 秒过期、用完即焚；health 里不下发任何口令。
_TICKETS: Dict[str, Dict[str, Any]] = {}
_TICKET_TTL = 30.0


def _prune_tickets():
    now = time.time()
    for k in [k for k, v in list(_TICKETS.items()) if float(v.get("exp") or 0) < now]:
        _TICKETS.pop(k, None)


def _new_ticket(method: str, origin: str = "", ip: str = "") -> str:
    _prune_tickets()
    nonce = secrets.token_urlsafe(24)
    _TICKETS[nonce] = {"method": str(method or ""), "exp": time.time() + _TICKET_TTL,
                       "origin": str(origin or ""), "ip": str(ip or "")}
    return nonce


def _consume_ticket(nonce: str, method: str, origin: str = "", ip: str = "") -> bool:
    """核对并使用票据：一次性、30 秒内、必须同一个方法/来源。"""
    nonce = str(nonce or "")
    if not nonce:
        return False
    rec = _TICKETS.pop(nonce, None)   # 无论成败都作废，防止重放
    if not rec:
        return False
    try:
        if float(rec.get("exp") or 0) < time.time():
            return False
        if str(rec.get("method") or "") != str(method or ""):
            return False
        o_rec = str(rec.get("origin") or "")
        if o_rec and origin and o_rec != origin:
            return False
        ip_rec = str(rec.get("ip") or "")
        if ip_rec and ip and ip_rec != ip:
            return False
        return True
    except Exception:
        return False


# 跨域白名单：**精确匹配**，不再有 `localhost:<任意端口>` 的通配。
# QA BUG-04：以前写 `http://(localhost|127\.0\.0\.1)(:\d+)?`，等于给本机所有开发服务器
# 和任意 XSS 页面开了后门 —— 它们能读 /api/health、能发 POST。
# 现在只认三种：
#   1) Tauri 自己的自定义 scheme（Windows 上是 http://tauri.localhost）；
#   2) 网页版控制台自己那一个端口（8765，`启动网页版.bat` 用的就是它）——
#      精确到端口，仍然挡掉 8080 / 3000 / 任意别的本地页面。
_ORIGIN_RE = (r"^(tauri://localhost|https?://tauri\.localhost|"
              r"https?://(127\.0\.0\.1|localhost):8765)$")


def _origin_allowed(origin: str) -> bool:
    o = str(origin or "").strip()
    if not o or o == "null":
        return False
    if re.match(_ORIGIN_RE, o) is not None:
        return True
    # P1-1（QA 终审）：网页版端口可以在设置里改（web_port），白名单要跟着它走，
    # 否则 start_web(9000) 之后页面所有 POST 都是 403。仍然**精确到端口**，
    # 只认「本机 + 当前配置的那个端口」，不给其它本地页面开口子。
    m = re.match(r"^https?://(127\.0\.0\.1|localhost):(\d+)$", o)
    if not m:
        return False
    try:
        import config as _cfgmod
        want = int(_cfgmod.get("web_port") or 8765)
    except Exception:
        want = 8765
    return int(m.group(2)) == want


# 免责协议闸门（QA BUG-03）：没同意之前，除「读类 + 协议 + 环境向导 + 探活」外一律 403。
# 这里用前缀放行 get_*：界面刚启动要靠它渲染设置/语言/公告，读操作不构成风险；
# 会改数据、起进程、导出、卸载的一律挡住。
# V1.5.3（QA P0-1）：**退出也必须放行**。
# 以前没同意协议时点窗口的 ✕ 会被这里 403 挡回去，界面卡在协议页、进程还在，
# 用户只能去任务管理器杀 —— 「我不同意，我也得有权利关掉你的软件」。
# quit_app / win_close / win_min / win_state 都是纯窗口动作，不碰数据，放行没有风险。
_DISCLAIMER_FREE = {
    "get_disclaimer", "accept_disclaimer", "get_about",
    "ping", "health", "methods", "confirm_ticket",
    "quit_app", "win_close", "win_min", "win_state",
    # v1.9.7（P0）：完整性判定是只读动作，必须在同意协议**之前**就能拿到结论。
    # 以前它被 403 挡掉，前端拿到的是错误对象、`tampered` 是 undefined，
    # 于是首屏的篡改遮罩永远不触发（复测报告 4.2）—— 弹窗挡了一次，问题只是推迟。
    "verify_package",
}


def _disclaimer_ok() -> bool:
    try:
        import config as _cfgmod
        return bool(_cfgmod.load().get("disclaimer_accepted", False))
    except Exception:
        return True    # 配置读不出来时别把整个软件锁死


def _disclaimer_block(method: str):
    """返回 403 响应；放行时返回 None。"""
    if _disclaimer_ok():
        return None
    m = str(method or "")
    if m in _DISCLAIMER_FREE or m.startswith("get_"):
        return None
    _log(f"[backend] 免责协议未接受，拒绝业务调用: {m}", "WARN")
    return JSONResponse(status_code=403, content={
        "ok": False, "code": "disclaimer_required",
        "msg": "还没同意「免责与使用声明」。先在上面点『同意并继续』，再使用这个功能。",
    })


def _running_job(kind: str):
    """有没有同类任务正在跑（同一个安装动作别开两份）。"""
    for j in list(_JOBS.values()):
        try:
            if j.get("kind") == kind and j.get("status") in ("pending", "running"):
                return j
        except Exception:
            continue
    return None


def _auto_unpack_napcat() -> Optional[Dict[str, Any]]:
    """首次启动时把随包的 NapCat 离线包展开好（V1.2.2）。

    装包里带了 118MB 的离线包，用户不必再等一小时的下载 ——
    这里在后台默默解压，向导一打开就是「已安装」。
    已经装过的用户不受影响（有目录就直接返回）。

    V1.3.1：改成返回 job（没有解压任务就返回 None），
    好让后面的 _auto_launch_napcat 能等它解压完再拉起 NapCat。
    """
    try:
        import napcat_mgr
        bi = napcat_mgr.bundled_info()
        if not bi.get("ok"):
            return None
        p = napcat_mgr.probe(deep=False) or {}
        if p.get("dir") or p.get("launcher"):
            return None
        if _running_job("napcat"):
            return None
        job = _new_job("napcat", "展开内置 NapCat（首次启动，约 20 秒）")

        def _work(j):
            _job_log(j, f"{bi.get('text')}：{bi.get('path') or ''}", 4)
            _job_log(j, "不需要联网，正在解压到运行目录…", 8)
            ok, msg, d = napcat_mgr.install_from_bundle(
                RUNTIME_NAPCAT,
                progress=lambda m: _job_log(j, str(m), _pct_from_msg(m, 8, 92)))
            if ok:
                _job_log(j, msg, 100)
                return {"ok": True, "msg": msg, "dir": d}
            _job_log(j, f"内置包没展开成功：{msg}", 100)
            return {"ok": False, "msg": msg, "dir": d}
        _run_job(job, _work)
        _log("[backend] 检测到内置 NapCat 离线包，正在后台展开（不需要联网）")
        return job
    except Exception as e:
        _log(f"[backend] 展开内置 NapCat 出错：{type(e).__name__}: {e}")
        return None


def _auto_start_av() -> None:
    """启动时自动拉起音视频微服务（V1.0.0-Beta：不再要求用户手动起）。

    故意放在最后、且整段包 try：微服务缺依赖、端口被占、python 找不到，
    都只是少一个功能，不能影响文字聊天。

    V1.0.0-Beta 又加了一层：真正的拉起放到**后台 daemon 线程**里做 ——
    `start()` 会等最多 25 秒（首次加载音频设备更慢），放在启动流程里会把
    「自动工作 / 首启向导」的节奏一起拖慢。线程里失败也只写日志。
    """
    def _worker() -> None:
        try:
            import av_bridge
            r = av_bridge.BRIDGE.start_if_enabled()
            if r.get("skipped"):
                if r.get("no_module"):
                    _log("[backend] 没有音视频模块（可选功能），跳过自动启动")
                return
            if r.get("ok"):
                _m = str(r.get("msg", "") or "")
                if "端口" not in _m:
                    _m = f"{_m}（端口 {av_bridge.BRIDGE.port}）"
                _log(f"[backend] 音视频模块：{_m}")
                try:
                    import config as _cfg
                    if _cfg.get("av_rpa_on_start", False):
                        rr = av_bridge.BRIDGE.call("POST", "/api/av/rpa/start",
                                                   {"dry_run": False}, timeout=20.0)
                        _log(f"[backend] 音视频 RPA：{rr.get('msg', '')}")
                except Exception as e:
                    _log(f"[backend] 音视频 RPA 自启失败：{type(e).__name__}: {e}")
            else:
                _log(f"[backend] 音视频模块没起来：{r.get('msg', '')} {r.get('hint', '')}", "WARN")
        except Exception as e:
            _log(f"[backend] 音视频模块启动跳过：{type(e).__name__}: {e}")

    try:
        import threading as _th
        _th.Thread(target=_worker, name="av-autostart", daemon=True).start()
    except Exception as e:
        _log(f"[backend] 音视频自动启动线程没起来：{type(e).__name__}: {e}")


def _av_shutdown() -> None:
    try:
        import av_bridge
        av_bridge.BRIDGE.shutdown()
        _log("[backend] 音视频模块已收尾（只关本控制台拉起的那个）")
    except Exception:
        pass


def _startup_work() -> None:
    # v1.9.4（抢修四②）：配置里还留着 xor1: 的，立刻换成 DPAPI。
    # xor1 的盐是程序里的公开常量，等于没加密；DPAPI 绑当前用户，换机就解不开。
    try:
        import security as _secd
        _mg = _secd.migrate_xor1_config()
        if _mg.get("moved"):
            _log("[安全] " + str(_mg.get("msg")))
    except Exception as _e:
        _log("[安全] 旧密文迁移跳过：%s" % _e, "DEBUG")
    global _METHODS_CACHE
    _ensure_runtime_dirs()
    try:
        _METHODS_CACHE = list_methods()
    except Exception:
        _METHODS_CACHE = []
    _log(f"[backend] v{APP_VERSION} 已启动：http://{SERVER['host']}:{SERVER['port']}"
         f"（{len(_METHODS_CACHE)} 个方法可用）")
    # v1.0.0-Beta 安全整改（QA BUG-05）：老版本留下的明文 API Key 就地加密，
    # 顺手把「本机明文 sk-」这件事收口（只改自己机器上的 config.json，不动用户别的东西）。
    try:
        mig = app_ui.security_migrate_plaintext_keys()
        if mig.get("migrated"):
            _log(f"[backend] 安全自检：{mig.get('msg')}", "WARN")
    except Exception as e:
        _log(f"[backend] 明文 Key 迁移失败（不影响使用）：{type(e).__name__}: {e}", "WARN")
    # v1.2.1：被开机自启拉起时，顺手把 Agent 跑起来，
    # 用户开机后就该「已经在收消息」，而不是还等他点一下启动。
    # v1.3.0：两种「拉起 Agent」的来源要分开看 ——
    #   1) 开机自启（--autostart）：用户要的就是开机就干活，无条件启动
    #   2) 普通双击打开软件：默认**不**启动（agent_auto_start=False），
    #      否则等于「打开软件就自动用你的 QQ 开始说话」，这是个风险点。
    _boot = (os.environ.get("QQ_AGENT_AUTOSTART") or "").strip().lower() in ("1", "true", "yes")
    try:
        import config as _cfg
        _auto = bool(_cfg.get("agent_auto_start", False))
    except Exception:
        _auto = False
    if _boot or _auto:
        try:
            r = app_ui.start_agent()
            _why = "开机自启" if _boot else "设置里开了「启动后自动工作」"
            _log(f"[backend] {_why}：Agent 已自动启动（{r.get('msg', '') if isinstance(r, dict) else r}）")
        except Exception as e:
            _log(f"[backend] 自动启动 Agent 失败：{type(e).__name__}: {e}")
    else:
        _log("[backend] 已就绪，等你点「开始」。"
             "（想让软件一打开就自动工作，去 设置 → 通用 打开「启动后自动工作」）")
    # v1.2.2：随包带了 NapCat 离线包，第一次启动就在后台展开好
    _first_unpack = _auto_unpack_napcat()
    # v1.3.2：NapCat 没跑就自动拉起来（用户要求：没登录就拉起让他登）
    # v1.3.1 修：解压是后台线程，紧接着 probe 肯定还看不到目录 —— 等到解压完了再拉，
    # 否则首次启动「自动拉起」永远被静默跳过（实测：日志里根本没有 [NapCat] 那行）。
    _auto_launch_napcat(wait_unpack=_first_unpack)
    # v1.3.0：音视频通话微服务（默认关，设置里打开才拉）
    _auto_start_av()


def _auto_launch_napcat(wait_unpack=None) -> None:
    """启动时看一眼 NapCat：没登录就拉起来，界面里再引导扫码。

    只在「装了 + 没在跑 + 用户没关这个开关」时动手，绝不重复拉起。
    wait_unpack 是 _auto_unpack_napcat() 起的那个 job（dict 或 None）：
    首次启动要先等它把 118MB 离线包展开完，否则 probe 看不到目录、白等一场。
    """
    try:
        import config as _cfg
        if not bool(_cfg.get("napcat_auto_launch", True)):
            return
        import napcat_mgr
        if wait_unpack:
            jid = (wait_unpack or {}).get("id") or ""
            deadline = time.time() + 180
            while jid and time.time() < deadline:
                j = get_job(jid) or {}
                if str(j.get("status")) in ("done", "failed"):
                    break
                time.sleep(0.5)
        p = napcat_mgr.probe(deep=True)
        if not p.get("dir") or p.get("running"):
            return
        ok, msg = napcat_mgr.launch(p.get("dir", ""), wait=20)
        _log(f"[NapCat] 启动时发现没在跑，已自动拉起：{msg if not ok else '等扫码登录'}")
    except Exception as e:
        _log(f"[NapCat] 自动拉起失败：{type(e).__name__}: {e}", "WARN")


@asynccontextmanager
async def _lifespan(_app):
    # 用 lifespan 而不是已废弃的 @app.on_event("startup")
    #
    # v1.3.0 修（很关键）：以前这里是 `await asyncio.to_thread(_startup_work)` ——
    # uvicorn 要等 lifespan 跑完才开始接请求，而 _startup_work 里要做
    # 「探 NapCat 端口（带超时）→ 解 118MB 内置离线包 → 看有没有开机自启」，
    # 机器慢的时候要几十秒。这几十秒里前端所有请求都连不上：
    # 首启就表现为「请稍等噢」转半天，免责协议被前端当成「后端说不用看」直接跳过。
    # 现在改成起个后台线程去做，接口立刻可用；前端首屏只要 1~2 秒就有响应。
    import threading as _th
    _th.Thread(target=_startup_work, name="backend-startup", daemon=True).start()
    yield
    try:
        await asyncio.to_thread(app_ui.stop_agent)
    except Exception:
        pass
    try:
        await asyncio.to_thread(_av_shutdown)
    except Exception:
        pass


app = FastAPI(title="小鲸鱼 Agent 后端", version=APP_VERSION,
              description="无头 API：界面由 Tauri 提供，这里只管业务。",
              lifespan=_lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_ORIGIN_RE,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- v1.9.0：强制 API Token ----
# 以前只有用户手动设 QQ_AGENT_API_TOKEN 才校验，默认等于没开 —— 本机任何进程
# （包括浏览器里一个跑歪了的页面脚本，只要绕过 Origin 判据）都能直接调后端。
# 现在：壳 spawn 后端时生成随机 Token 传进环境；直接跑 exe / 网页版启动器时，
# 后端自己生成一份写到 <数据目录>/runtime/api_token.txt，别的程序读不到就不算数。
_TOKEN_FILE = os.path.join(os.environ.get("QQ_AGENT_DATA_DIR") or
                           os.path.join(os.path.expanduser("~"), ".qq_agent"),
                           "runtime", "api_token.txt")


def _load_or_make_token():
    """环境变量优先；没有就生成一份并落盘（0600 尽力而为）。"""
    env = (os.environ.get("QQ_AGENT_API_TOKEN") or "").strip()
    if env:
        return env, "env"
    try:
        if os.path.isfile(_TOKEN_FILE):
            old = io.open(_TOKEN_FILE, encoding="utf-8").read().strip()
            if len(old) >= 16:
                return old, "file"
    except Exception:
        pass
    tok = secrets.token_urlsafe(32)
    try:
        d = os.path.dirname(_TOKEN_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        with io.open(_TOKEN_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(tok)
        try:
            os.chmod(_TOKEN_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        _log("[backend] Token 落盘失败（仅本次有效）：%s" % e, "WARN")
    return tok, "generated"


API_TOKEN, API_TOKEN_SRC = _load_or_make_token()
# 不需要 Token 的路径：探活、健康检查、免责声明状态（前端首屏就要，且都不改东西）
# v1.9.1：/api/confirm/ticket 从白名单里拿掉 —— 「给危险动作发通行证」的
# 接口本身不能免鉴权，否则安全边界被拆成两半（复测报告 F-06 遗留）。
_TOKEN_FREE = ("/api/ping", "/api/health", "/api/get_disclaimer",
               "/api/accept_disclaimer")


def _token_guard(request: Request):
    """非只读方法必须带 X-API-Token。只读 GET 放行（它们也过 Origin 判据）。"""
    if not API_TOKEN:
        return None
    path = (request.url.path or "").rstrip("/")
    if path in _TOKEN_FREE:
        return None
    if request.method == "GET" and path.startswith("/api/"):
        # 只读 GET 允许不带 Token：网页版/诊断工具都靠它，且不带任何写操作。
        # 真正危险的是 POST（所有写操作只能走 POST）。
        return None
    got = (request.headers.get("x-api-token") or "").strip()
    if got != API_TOKEN:
        _log("[backend] 拒绝无 Token 的写请求: %s %s" % (request.method, path), "WARN")
        return JSONResponse(status_code=401,
                            content={"ok": False, "code": "token_required",
                                     "msg": "缺少或错误的 X-API-Token（v1.9.0 起写操作必须带）"})
    return None


def _client_ip(request: Request) -> str:
    try:
        return str(request.client.host or "") if request.client else ""
    except Exception:
        return ""


def _confirm_ok(request: Request, method: str = "") -> bool:
    """破坏性方法的一次性票据校验（QA BUG-04：不再有长期口令，也不再从 health 下发）。"""
    return _consume_ticket(request.headers.get("x-confirm-ticket") or "",
                           method or "",
                           request.headers.get("origin") or "",
                           _client_ip(request))


@app.middleware("http")
async def _security_mw(request: Request, call_next):
    """统一入口防线（QA BUG-04 + V1.3.2 终审必修 1）：

    1) **来源校验对所有方法生效**，真正的判据是 Origin（浏览器改不了它）：
       · 带 Origin 且不在白名单 → 403（网页 / 第三方站点 / curl 伪造成 evil.com）
       · 不带 Origin 却标了 `Sec-Fetch-Site: cross-site` → 403
         （浏览器发跨站请求一定带 Origin；只标头不带 Origin 的是脚本伪装，
           QA 复现命令就是这种）
       · 带 Origin 且在白名单（Tauri）→ 放行，**哪怕请求被标成 cross-site**：
         前端页面在 `tauri.localhost`、后端在 `127.0.0.1:57xx`，按「站点」算就是跨站，
         这是自家正常流量。（V1.3.2 抢修第一版把这行写成了「见 cross-site 就拒」，
          结果安装版的界面所有 `GET /api/get_*` 全 403、用户看到「连不上后端」。）
       以前的漏洞是这两条写在「非 GET」分支里，而浏览器
       `<img src=".../api/save_settings?args=...">` 正是 GET 且不带 Origin，
       于是三道新防线对 GET 完全失效（QA 实测 5 个写操作全 200）。
    2) 非 GET/HEAD/OPTIONS 必须 `Content-Type: application/json` —— 浏览器发 text/plain
       的「简单请求」不触发预检，是 CSRF 的经典口子；
    3) 读方法与写方法的区分在分发路由里（`_is_read_only_method`）：GET 只放只读方法，
       其余 405，见 `api_dispatch_get`。
    """
    try:
        # ---- 第 1 条：来源校验，GET / POST 一视同仁 ----
        origin = request.headers.get("origin") or ""
        site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if origin:
            if not _origin_allowed(origin):
                _log(f"[backend] 拒绝非白名单 Origin: {origin} -> {request.url.path}", "WARN")
                return JSONResponse(status_code=403, content={
                    "ok": False, "code": "bad_origin",
                    "msg": "这个来源不允许访问本机后端"})
        elif site == "cross-site":
            # 浏览器发跨站请求一定会带 Origin；没有 Origin 还标 cross-site = 脚本伪造
            _log(f"[backend] 拒绝 cross-site 请求（无 Origin）: {request.method} "
                 f"{request.url.path}", "WARN")
            return JSONResponse(status_code=403, content={
                "ok": False, "code": "cross_site",
                "msg": "拒绝来自其它站点的请求"})
        # ---- 第 2 条：写请求必须是 JSON ----
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                _log(f"[backend] 拒绝非 JSON 请求: {request.method} {request.url.path} "
                     f"(content-type={ctype or '空'})", "WARN")
                return JSONResponse(status_code=415, content={
                    "ok": False, "code": "bad_content_type",
                    "msg": "请求必须是 application/json（安全策略）"})
    except Exception as e:
        _log(f"[backend] 入口校验异常（放行）: {type(e).__name__}: {e}", "WARN")
    return await call_next(request)


# ---- 探活 ----
@app.get("/api/health")
def api_health():
    try:
        return health_payload()
    except Exception as e:
        return JSONResponse(status_code=200, content={
            "ok": False, "msg": f"{type(e).__name__}: {e}", "ts": int(time.time())})


@app.get("/api/ping")
def api_ping():
    return {"ok": True, "pong": True, "ts": int(time.time()), "version": APP_VERSION}


@app.get("/api/methods")
def api_methods():
    global _METHODS_CACHE
    if not _METHODS_CACHE:
        _METHODS_CACHE = list_methods()
    return {"ok": True, "count": len(_METHODS_CACHE), "methods": _METHODS_CACHE}


# ---- 环境 / 依赖安装向导（双入口机制的后端一半）----
@app.get("/api/env/check")
def api_env_check():
    """向导用：缺什么、怎么补、能不能一键装。"""
    plugins = {"napcat": _napcat_status(), "tts": _tts_status(), "node": _node_status()}
    mirrors, official = [], "https://github.com/NapNeko/NapCatQQ/releases"
    try:
        import napcat_mgr
        mirrors = [m[0] for m in getattr(napcat_mgr, "NAPCAT_MIRRORS", [])]
        official = getattr(napcat_mgr, "NAPCAT_RELEASES_PAGE", official)
    except Exception:
        pass
    items = []
    for key in ("napcat", "tts"):
        p = dict(plugins[key])
        if key == "tts":
            # _tts_status 自己算过（打包版不能在线装 / 内置版不用装），别在这里覆盖掉
            p["can_auto_install"] = (bool(p.get("can_auto_install"))
                                     and bool(_pip_python())
                                     and not bool(p.get("bundled")))
            p["manual_hint"] = ("打包版：用 打包后端.bat 重新打包（内置 edge-tts）；"
                                "源码运行：点「一键安装」，或命令行 pip install edge-tts")
            p["model_dir"] = tts_model_dir(mkdir=False)
        else:
            p["can_auto_install"] = bool(plugins["node"].get("installed")) or bool(p.get("bundled"))
            p["manual_hint"] = (
                "官方下载：%s（选带 Node 的 Windows 免安装包，"
                "解压到 runtime/napcat，或直接把路径填进来）" % official)
            # v1.2.1：国内网络连不上 GitHub 时用下面两条路
            p["official_page"] = official
            p["mirrors"] = mirrors
            p["can_import_zip"] = True
            p["import_hint"] = "自己用浏览器下好 zip，点「手动导入安装包」选它，不用联网也不走代理"
            # v1.2.2：随程序带的离线包，有它就连网都不用
            p["can_install_offline"] = bool(p.get("bundled"))
            p["bundled_text"] = p.get("bundled_text") or ""
        items.append(p)
    return {
        "ok": True,
        "needs_setup": any(not i.get("installed") for i in items),
        # 和 /api/health 的 setup_missing 保持一致，前端/脚本都只认这一份
        "setup_missing": [i.get("key") for i in items if not i.get("installed")],
        "items": items,
        "node": plugins["node"],
        "runtime_dir": RUNTIME_DIR,
        "napcat_default_dir": RUNTIME_NAPCAT,
        "python_for_pip": _pip_python(),
        "tts_model_dir": tts_model_dir(mkdir=False),
        "check_ts": int(time.time()),
    }


@app.get("/api/env/paths")
def api_env_paths():
    return {"ok": True, "runtime_dir": RUNTIME_DIR, "napcat_dir": RUNTIME_NAPCAT,
            "cache_dir": RUNTIME_CACHE, "models_dir": RUNTIME_MODELS,
            "tts_model_dir": tts_model_dir(mkdir=True),
            "data_dir": app_ui.DATA_DIR, "log_file": app_ui.LOG_FILE}


@app.post("/api/env/install_tts")
def api_env_install_tts(request: Request):
    """后台 pip install edge-tts（打包版会优先用 runtime/python 便携解释器）。"""
    bad = _token_guard(request)
    if bad is not None:
        return bad
    if getattr(sys, "frozen", False):
        # 打包版跑在 PyInstaller 自带的解释器里：pip 装到系统 python 里没用，
        # 与其让用户等三分钟再看到「未安装」，不如直接说清楚。
        return {"ok": False,
                "msg": "打包版不能在线安装语音引擎（它用的是自带解释器）。"
                       "请用 打包后端.bat 重新打包（默认已包含 edge-tts），"
                       "或改用源码方式运行后端。"}
    py = _pip_python()
    if not py:
        return {"ok": False, "msg": "找不到可用的 Python 解释器，装不了 edge-tts。"
                                    "请自己执行 pip install edge-tts，或放一份便携 Python 到 "
                                    f"{RUNTIME_PY}"}
    job = _new_job("tts", "安装 edge-tts")

    def _work(j):
        _job_log(j, f"用 {py} 安装 edge-tts…", 10)
        cmd = [py, "-m", "pip", "install", "edge-tts", "--disable-pip-version-check",
               "--no-input"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             creationflags=flags)
        lines = []
        for line in (p.stdout or []):
            line = (line or "").rstrip()
            if not line:
                continue
            lines.append(line)
            if len(lines) % 3 == 0:
                _job_log(j, line[:200], min(90, 10 + len(lines)))
        p.wait(timeout=600)
        if p.returncode != 0:
            tail = " / ".join(lines[-3:]) or f"退出码 {p.returncode}"
            _job_log(j, f"pip 失败（{p.returncode}）：{tail}", 100)
            return {"ok": False, "msg": f"pip 安装失败：{tail}"}
        _job_log(j, "安装完成，正在校验…", 95)
        inst = bool(importlib.util.find_spec("edge_tts"))
        _job_log(j, "edge-tts 已可用" if inst else "pip 说装完了但 import 不到（重启后端再看）",
                 100)
        return {"ok": True, "installed": inst,
                "msg": "edge-tts 装好了" if inst else "装完了，重启后端生效"}
    _run_job(job, _work)
    return {"ok": True, "job": job["id"], "msg": "已开始安装 edge-tts"}


@app.post("/api/env/install_napcat")
async def api_env_install_napcat(request: Request):
    """后台下载并安装 NapCat（解压到 runtime/napcat），装完自动写 onebot11 配置。"""
    bad = _token_guard(request)
    if bad is not None:
        return bad
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    target = str(body.get("dir") or body.get("target_dir") or RUNTIME_NAPCAT).strip()
    mirror = str(body.get("mirror") or "").strip()
    # source: "bundle"=只用内置包 / "download"=强制在线下载 / 空=有内置包就用内置包
    source = str(body.get("source") or "").strip().lower()
    try:
        import napcat_mgr as _nm
        _bundled = _nm.bundled_info()
    except Exception:
        _bundled = {"ok": False}
    use_bundle = bool(_bundled.get("ok")) and source != "download"
    # 首次启动会自动展开内置包，这时候用户又点了一次「一键安装」——
    # 直接把在跑的那个任务还给他，别开两份解压往同一个目录写
    busy = _running_job("napcat")
    if busy:
        return {"ok": True, "job": busy["id"], "busy": True, "dir": target,
                "msg": "已经在装了，跟着这个进度看就行"}
    job = _new_job("napcat", "安装 NapCat（内置离线包）" if use_bundle
                   else "下载安装 NapCat")

    def _work(j):
        import napcat_mgr

        def _finish(d, msg):
            """装完的收尾：写 onebot 配置 + 报 WebUI 地址。"""
            res = {"ok": True, "msg": msg, "dir": d}
            try:
                cfg = napcat_mgr.write_onebot_config(d)
                res["config"] = cfg.get("msg", "")
                res["paths"] = cfg.get("paths", [])
                _job_log(j, f"配置：{res['config']}", 94)
            except Exception as e:
                _job_log(j, f"写配置出错（不影响使用）：{type(e).__name__}: {e}", 94)
            try:
                wi = napcat_mgr.webui_info(d)
                res["webui"] = wi.get("url", "")
                res["webui_token"] = wi.get("token", "")
                _job_log(j, "下一步：启动 NapCat → 打开 WebUI → 手机 QQ 扫码登录", 98)
            except Exception:
                pass
            _job_log(j, msg, 100)
            return res

        _job_log(j, f"安装目录：{target}", 3)

        # ① 内置离线包（V1.2.2）：随程序发布，不用联网，十几秒解压完
        if use_bundle:
            _job_log(j, f"{_bundled.get('text') or '内置离线包'}，解压中（不需要联网）…", 6)
            ok, msg, d = napcat_mgr.install_from_bundle(
                target, progress=lambda m: _job_log(j, str(m), _pct_from_msg(m, 8, 90)))
            if ok:
                return _finish(d, msg)
            _job_log(j, f"内置包没装成（{msg}），改用在线下载…", 10)

        if mirror:
            try:
                app_ui._cfg_mod.set_checked("napcat_mirror", mirror)
                _job_log(j, f"下载源：{mirror}（失败会自动换别的源）", 4)
            except Exception:
                pass
        else:
            _job_log(j, "下载源：自动（官方优先，连不上自动换国内镜像）", 4)
        ok, msg, d = napcat_mgr.download_and_install(
            target, progress=lambda m: _job_log(j, str(m), _pct_from_msg(m)))
        if not ok:
            _job_log(j, f"安装失败：{msg}", 100)
            # 把「下一步怎么办」也写进日志，前端能直接照着显示
            return {"ok": False, "msg": msg, "dir": d, "can_import": True,
                    "official_page": getattr(napcat_mgr, "NAPCAT_RELEASES_PAGE", "")}
        return _finish(d, msg)
    _run_job(job, _work)
    return {"ok": True, "job": job["id"],
            "msg": "已开始安装内置 NapCat" if use_bundle else "已开始下载安装 NapCat",
            "dir": target}


@app.post("/api/env/import_napcat")
async def api_env_import_napcat(request: Request):
    """入口 C（终极兜底）：用户自己在浏览器下好 zip，这里导入。

    完全不需要联网，也不碰代理 —— 国内没梯子时这条路必定能用。
    """
    bad = _token_guard(request)
    if bad is not None:
        return bad
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    zip_path = str(body.get("zip") or body.get("path") or "").strip().strip('"')
    target = str(body.get("dir") or body.get("target_dir") or RUNTIME_NAPCAT).strip()
    if not zip_path:
        return {"ok": False, "msg": "没拿到 zip 路径（先选文件）"}
    if not os.path.isfile(zip_path):
        return {"ok": False, "msg": f"找不到这个文件：{zip_path}"}
    if not zip_path.lower().endswith(".zip"):
        return {"ok": False, "msg": "只支持 .zip 压缩包（NapCat 官方发的就是 zip）"}

    job = _new_job("napcat_import", "导入本地安装包")

    def _work(j):
        import napcat_mgr
        _job_log(j, f"压缩包：{zip_path}", 4)
        _job_log(j, f"安装目录：{target}", 6)
        _job_log(j, "正在检查包里是不是 NapCat…", 15)
        ok, msg, d = napcat_mgr.install_from_zip(
            zip_path, target,
            progress=lambda m: _job_log(j, str(m), _pct_from_msg(m, 20, 92)))
        if not ok:
            _job_log(j, f"导入失败：{msg}", 100)
            return {"ok": False, "msg": msg, "dir": d}
        res = {"ok": True, "msg": msg, "dir": d}
        try:
            wi = napcat_mgr.webui_info(d)
            res["webui"] = wi.get("url", "")
            res["webui_token"] = wi.get("token", "")
        except Exception:
            pass
        _job_log(j, "下一步：启动 NapCat → 打开 WebUI → 手机 QQ 扫码登录", 98)
        _job_log(j, msg, 100)
        return res
    _run_job(job, _work)
    return {"ok": True, "job": job["id"], "msg": "已开始导入本地压缩包", "dir": target}


@app.post("/api/env/set_napcat_dir")
async def api_env_set_napcat_dir(request: Request):
    """入口 B：用户自己装好了，把路径填进来（会校验里面有没有 napcat.bat 之类）。"""
    bad = _token_guard(request)
    if bad is not None:
        return bad
    try:
        body = await request.json()
    except Exception:
        body = {}
    d = str((body or {}).get("dir") or "").strip() if isinstance(body, dict) else ""
    if not d or not os.path.isdir(d):
        return {"ok": False, "msg": f"目录不存在：{d or '（空）'}"}
    try:
        import napcat_mgr
        launcher = napcat_mgr.find_launcher(d)
        if not launcher and not napcat_mgr._looks_like_napcat(d):
            return {"ok": False, "msg": "这个目录里没找到 NapCat 的启动脚本（napcat.bat / NapCatWinBootMain.exe）"}
        ok, msg, _v = app_ui._cfg_mod.set_checked("napcat_dir", d)
        cfg = napcat_mgr.write_onebot_config(d)
        _log(f"[backend] 手动指定 NapCat 目录：{d}")
        return {"ok": bool(ok), "msg": f"已设为 {d}；{cfg.get('msg', '')}", "dir": d,
                "launcher": launcher}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


@app.get("/api/env/job/{jid}")
def api_env_job(jid: str):
    j = get_job(jid)
    if not j:
        return {"ok": False, "msg": f"没有这个任务: {jid}"}
    return {"ok": True, **j}


@app.get("/api/env/jobs")
def api_env_jobs():
    with _JOB_LOCK:
        items = sorted(_JOBS.values(), key=lambda x: x.get("started", 0), reverse=True)[:20]
    return {"ok": True, "items": [dict(i) for i in items]}


# ---- 日志 ----
def _read_new(path: str, pos: int) -> Tuple[str, int]:
    """读 pos 之后的新内容；文件被轮转/截断（变小）时从头读。"""
    if not os.path.exists(path):
        return "", pos
    try:
        size = os.path.getsize(path)
        if size < pos:
            pos = 0
        if size == pos:
            return "", pos
        with open(path, "rb") as f:
            f.seek(pos)
            data = f.read(512 * 1024)
            return data.decode("utf-8", "replace"), f.tell()
    except Exception:
        return "", pos


@app.get("/api/logs/tail")
def api_logs_tail(lines: int = 200, path: str = ""):
    """轮询版日志（WebSocket 用不了时的兜底）。"""
    p = path or app_ui.LOG_FILE
    n = max(1, min(2000, int(lines or 200)))
    try:
        size = os.path.getsize(p)
        back = min(size, max(4096, n * 400))
        with open(p, "rb") as f:
            f.seek(size - back)
            text = f.read().decode("utf-8", "replace")
        rows = text.splitlines()[-n:]
        # pos = 当前文件末尾偏移，前端拿它可以接着走 /ws/logs?tail=0 续传
        return {"ok": True, "lines": rows, "size": size, "pos": size, "file": p}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "lines": [],
                "size": 0, "pos": 0, "file": p}


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    """增量推送 agent.log。

    连接参数：
      ?tail=8000        从末尾往回读多少字节作为「历史」（默认 8000）
      ?from_start=1     从头读（调试用）
      ?path=...         换一个日志文件（默认 agent.log）
    """
    await ws.accept()
    q = ws.query_params
    path = q.get("path") or app_ui.LOG_FILE
    try:
        tail = int(q.get("tail") or 8000)
    except Exception:
        tail = 8000
    from_start = str(q.get("from_start") or "") in ("1", "true", "yes", "on")
    try:
        size = os.path.getsize(path) if os.path.exists(path) else 0
    except Exception:
        size = 0
    pos = 0 if from_start else max(0, size - max(0, tail))

    try:
        await ws.send_json({"type": "hello", "file": path, "pos": pos,
                            "version": APP_VERSION, "ts": time.time()})
    except Exception:
        return

    try:
        while True:
            text, pos = await asyncio.to_thread(_read_new, path, pos)
            if text:
                rows = text.splitlines()
                if rows:
                    await ws.send_json({"type": "log", "lines": rows,
                                        "pos": pos, "ts": time.time()})
            else:
                await asyncio.sleep(0.4)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await ws.send_json({"type": "error", "msg": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ---- 首页：给浏览器/调试用的状态页 ----
@app.get("/", response_class=HTMLResponse)
def index():
    h = health_payload()
    plugins = h.get("plugins", {})
    rows = "".join(
        f"<tr><td>{p.get('name','')}</td><td>{'✅ 可用' if p.get('installed') else '❌ 缺失'}"
        f"</td><td>{p.get('msg','')}</td></tr>"
        for p in plugins.values())
    return f"""<!doctype html><meta charset="utf-8">
<title>小鲸鱼后端 v{APP_VERSION}</title>
<style>body{{font-family:system-ui,'Segoe UI',sans-serif;background:#0f172a;color:#e2e8f0;
padding:32px;line-height:1.7}}code{{background:#1e293b;padding:2px 6px;border-radius:4px}}
table{{border-collapse:collapse;margin:12px 0}}td,th{{border:1px solid #334155;padding:6px 12px}}
a{{color:#7dd3fc}}</style>
<h1>🐳 小鲸鱼 Agent 后端 v{APP_VERSION}</h1>
<p>界面在 Tauri 桌面端。这里是纯 API 服务，自带文档：
<a href="/docs">/docs</a> ｜ <a href="/api/health">/api/health</a> ｜
<a href="/api/methods">/api/methods</a></p>
<table><tr><th>组件</th><th>状态</th><th>说明</th></tr>{rows}</table>
<p>模型：<code>{h['model'].get('model') or '（未配置）'}</code>　
后端：<code>{h['backend']['url']}</code>　进程 PID：<code>{h['backend']['pid']}</code></p>
<p>日志 WebSocket：<code>ws://{SERVER['host']}:{SERVER['port']}/ws/logs</code></p>"""


# ---- 一次性确认票据：破坏性操作先来这里换票（30 秒、一次性）----
@app.post("/api/confirm/ticket")
async def api_confirm_ticket(request: Request):
    """给「卸载/更新/导入覆盖」这类操作发一张一次性票据。

    QA BUG-04 的落地：票据不再是长期口令、也不从 health 下发；
    只发给通过入口校验（Tauri origin + application/json）的调用方，
    并且绑定方法名 + 来源 Origin + 客户端地址，30 秒过期、用完即焚。
    """
    bad = _token_guard(request)
    if bad is not None:
        return bad
    try:
        raw = await request.body()
        data = json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    method = str(data.get("method") or "").strip()
    if method not in _DANGER_METHODS:
        return {"ok": False, "code": "not_danger",
                "msg": f"{method or '（空）'} 不是破坏性操作，不需要票据"}
    nonce = _new_ticket(method, request.headers.get("origin") or "", _client_ip(request))
    _log(f"[backend] 已签发一次性票据: {method}", "INFO")
    return {"ok": True, "nonce": nonce, "method": method,
            "expires_in": int(_TICKET_TTL),
            "header": "X-Confirm-Ticket"}


# ---- 通用分发：必须放在最后，否则会把 /api/health 之类全吃掉 ----
@app.post("/api/{method}")
async def api_dispatch_post(method: str, request: Request):
    bad = _token_guard(request)
    if bad is not None:
        return bad
    blocked = _disclaimer_block(method)
    if blocked is not None:
        return blocked
    try:
        raw = await request.body()
        payload: Any = None
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                payload = raw.decode("utf-8", "replace")
        # QA 终审：破坏性调用缺票据以前返回 **HTTP 200 + {"ok":false,"danger":...}**，
        # QA 用 curl 看状态码会误判成"通过了"。现在按 HTTP 语义回 403，
        # body 结构不变（前端还是能拿到 danger/msg）。
        confirmed = _confirm_ok(request, method)
        if method in _DANGER_METHODS and not confirmed:
            _log(f"[backend] 拒绝未确认的破坏性调用（403）: {method}", "WARN")
            return JSONResponse(status_code=403, content={
                "ok": False, "code": "ticket_required", "danger": method, "method": method,
                "msg": f"{method} 是不可逆操作，需要先在界面上确认（一次性票据），"
                       f"别用浏览器地址栏直接敲"})
        return await asyncio.to_thread(call_method, method, payload,
                                       confirmed)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "method": method}


@app.get("/api/{method}")
async def api_dispatch_get(method: str, request: Request):
    """GET 只为浏览器里手点「查一眼」方便：只放纯读方法。

    QA V1.3.2 终审必修 1（P0）：以前这里能执行任意方法，
    `GET /api/save_settings?args=[{"guard_enabled":false}]` 这种直接生效。
    现在非只读方法一律 405 —— 写操作只能走 POST（要 application/json + Origin 校验）。
    """
    if not _is_read_only_method(method):
        _log(f"[backend] 拒绝 GET 调写方法: {method}", "WARN")
        return JSONResponse(status_code=405, content={
            "ok": False, "code": "method_not_allowed", "method": method,
            "msg": f"{method} 不能通过 GET 调用（只允许只读方法）；写操作请用 POST + JSON"})
    bad = _token_guard(request)
    if bad is not None:
        return bad
    blocked = _disclaimer_block(method)
    if blocked is not None:
        return blocked
    try:
        params = dict(request.query_params)
        payload: Any = None
        if params:
            if "args" in params:
                try:
                    payload = {"args": json.loads(params["args"])}
                except Exception:
                    payload = None
            if payload is None:
                payload = params
        # GET 永远不带确认票据：地址栏点一下就能卸载的日子结束了
        return await asyncio.to_thread(call_method, method, payload)
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "method": method}


# ==================== 启动 ====================
def build_server(host: str = "127.0.0.1", port: int = 0) -> int:
    """决定端口并记到 SERVER 里。port=0 表示自动探测。"""
    SERVER["host"] = host or "127.0.0.1"
    _prune_tickets()
    if port and int(port) > 0:
        SERVER["port"] = int(port)
    else:
        SERVER["port"] = find_free_port(PORT_PREFERRED, SERVER["host"],
                                        PORT_SCAN_SPAN, RESERVED_PORTS)
    return SERVER["port"]


# ---------------------------------------------------------------- 单实例
# v1.5.1（P1-3）：同一个数据目录只允许一个后端。
# 以前连开两次「小鲸鱼.exe」会各起一套后端、同时写同一个记忆库
# （5701 被占就自动换端口，所以表面上"没问题"），写库会互相踩。
_LOCK_FH = {"fh": None, "path": ""}


def _acquire_instance_lock():
    """对 <数据目录>\.console.lock 加独占锁。返回 (ok, 人话)。"""
    import os as _os
    path = _os.path.join(app_ui.DATA_DIR, ".console.lock")
    try:
        _os.makedirs(app_ui.DATA_DIR, exist_ok=True)
        fh = open(path, "a+b")
    except Exception as e:
        return True, "锁文件建不出来（%s），跳过单实例检查" % e
    try:
        import msvcrt
        fh.seek(0)
        fh.write(b"0")
        fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        _LOCK_FH["fh"] = fh
        _LOCK_FH["path"] = path
        return True, ""
    except Exception:
        try:
            fh.close()
        except Exception:
            pass
        return False, ("另一个小鲸鱼控制台正占着这个数据目录（%s）。\n"
                       "请先关掉那个窗口；确实需要同时开两个，"
                       "给它们各自指定不同的 QQ_AGENT_DATA_DIR。"
                       % app_ui.DATA_DIR)


def _watch_parent(pid):
    """父进程（桌面壳）没了就自杀 —— 效果等同 Windows Job Object 的
    KILL_ON_JOB_CLOSE：壳被强杀时后端不会变成孤儿继续写库。"""
    try:
        import psutil
        gone = lambda: not psutil.pid_exists(pid)  # noqa: E731
    except Exception:
        gone = None
    if gone is None:
        return

    def _run():
        import time as _t
        while True:
            _t.sleep(2.0)
            try:
                if gone():
                    try:
                        _log("[backend] 桌面壳已退出（父进程 %d 没了），后端跟着退出" % pid)
                    except Exception:
                        pass
                    try:
                        app_ui.stop_agent()
                    except Exception:
                        pass
                    import os as _os
                    _os._exit(0)
            except Exception:
                pass

    import threading as _th
    _th.Thread(target=_run, daemon=True, name="parent-watchdog").start()


def _long_path(p: str) -> str:
    """V1.5.3（QA P0-3）：给 Windows 路径加 `\\\\?\\` 前缀绕过 260 字符限制。

    实测残骸长这样：`C:\\Users\\x\\AppData\\Local\\Temp\\_MEI123456\\...\\_MEI123456\\...`
    —— PyInstaller 套娃之后轻松破 260，普通 `shutil.rmtree` 直接
    「文件名或扩展名太长」失败，目录就永远躺在那儿。
    """
    try:
        p = os.path.abspath(p)
    except Exception:
        return p
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC" + p[1:]
    return "\\\\?\\" + p


def _rmtree_long(p: str) -> bool:
    """删目录（长路径、只读文件也尽力删）。删干净返回 True。"""
    try:
        import stat

        def _onerr(func, path, exc):
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                pass

        target = _long_path(p) if os.name == "nt" else p
        if os.path.isdir(target):
            shutil.rmtree(target, onerror=_onerr)
        return not os.path.isdir(target)
    except Exception:
        return False


def _safe_print(*parts, **kw):
    """V1.5.3（QA P0-2）：stdout 被壳接管时 `print()` 会抛 `OSError [Errno 22]`。

    以前 main() 里那句「给 Rust 打印端口」的 print 没有包，一旦句柄非法
    整个进程秒退 —— 而单实例锁还没释放，用户再双击就是「点了没反应」。
    写日志失败不该等于程序崩溃：这里吞掉，返回 False 让调用方知道没打出去。
    """
    try:
        print(*parts, **kw)
        return True
    except Exception:
        return False


def _port_file_path() -> str:
    base = (os.environ.get("LOCALAPPDATA") or "").strip() or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "xiaojingyu-console", "backend.port")


def _write_port_file(url: str) -> bool:
    """把真实端口落一份到磁盘。

    桌面壳读同一个文件（`%LOCALAPPDATA%\\xiaojingyu-console\\backend.port`）：
    万一 stdout 打印失败（杀软接管管道之类），壳照样能拿到地址接上，
    不会出现「服务其实起来了，界面却一直转圈」。
    """
    try:
        p = _port_file_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"url": url, "pid": os.getpid(), "ts": time.time()}, f)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def _sweep_mei(min_age_sec=60.0):
    """v1.5.2（QA No-Go 修复一）：清理 %TEMP% 里被强杀留下的 PyInstaller 解包目录。

    onefile 的 exe 会把自身解到 `%TEMP%\\_MEIxxxxxx`，只有引导进程看到子进程正常结束
    才会删；桌面端 TerminateProcess 一砍，几百 MB 就永久留在 C 盘（QA 实测一次开两个 700MB）。
    这里在启动时做「先改名、再删除」：改名能成功说明没有进程占着它，才真删。
    """
    removed = 0
    try:
        import shutil
        import tempfile
        import time as _t
        # 注意：tempfile.gettempdir() 和 PyInstaller 实际解包的目录**不一定是同一个**
        # （实测：gettempdir() 是 D:\Temp，而 onefile 解在了 %LOCALAPPDATA%\Temp）。
        # 所以这里把几个候选目录全扫一遍。
        cands = []
        for _p in (tempfile.gettempdir(), os.environ.get("TEMP"), os.environ.get("TMP"),
                   os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp"),
                   os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Temp")):
            if _p and os.path.isdir(_p):
                _n = os.path.normcase(os.path.abspath(_p))
                if _n not in [os.path.normcase(c) for c in cands]:
                    cands.append(os.path.abspath(_p))
        mine = (getattr(sys, "_MEIPASS", "") or "").rstrip("\\/")
        now = _t.time()
        seen = set()
        for tmp in cands:
            try:
                names = os.listdir(tmp)
            except Exception:
                continue
            for name in names:
                if not name.lower().startswith("_mei"):
                    continue
                p = os.path.join(tmp, name)
                key = os.path.normcase(p)
                if key in seen or not os.path.isdir(p):
                    continue
                seen.add(key)
                if mine and key.startswith(os.path.normcase(mine)):
                    continue      # 自己正在用的，绝对不能碰
                try:
                    if now - os.path.getmtime(p) < min_age_sec:
                        continue  # 太新，可能是刚起来的另一个实例
                except Exception:
                    continue
                # 名字里已经带 .xjydel 的，是老版本一轮轮叠出来的残骸。
                # v1.5.3：**只删不改名**，绝不再往后加后缀（那是当初越删越长的根因）。
                if ".xjydel" in name.lower():
                    if _rmtree_long(p):
                        removed += 1
                    else:
                        _safe_print("[backend] 跳过删不掉的残留解包目录（下次再试）：%s"
                                    % name[:120], flush=True)
                    continue

                trashed = p + ".xjydel"
                if os.path.isdir(trashed):
                    _rmtree_long(trashed)     # 顺手尝试清一遍，失败也不管
                try:
                    os.rename(p, trashed)      # 被占用会抛异常 → 说明还活着
                except Exception:
                    continue
                if _rmtree_long(trashed):
                    removed += 1
                else:
                    _safe_print("[backend] 解包目录删不掉（长路径或仍被占用）：%s"
                                % trashed, flush=True)
        if removed:
            _safe_print("[backend] 启动清理：删掉 %d 个残留解包目录（_MEI*）" % removed, flush=True)
    except Exception:
        pass
    return removed


def _verify_delta_dir(dirpath):
    """v1.9.1：校验增量包 —— 清单的 Ed25519 签名 + 每个 payload 文件的 sha256。

    为什么要有它：ps1 里的 sha256 只能证明「包没下坏」，证明不了「包是我们发的」。
    清单带签名之后，改任何一个字节都得有发布私钥才能重签。
    返回 (ok, msg)。
    """
    import base64
    import hashlib
    import json as _json
    if not dirpath or not os.path.isdir(dirpath):
        return False, "目录不存在：%s" % dirpath
    mp = os.path.join(dirpath, "delta_manifest.json")
    if not os.path.isfile(mp):
        return False, "没找到 delta_manifest.json"
    try:
        with open(mp, "r", encoding="utf-8") as f:
            man = _json.load(f)
    except Exception as e:
        return False, "清单读不出来：%s" % e
    sig_b64 = str(man.pop("sig", "") or "")
    if not sig_b64:
        return False, "清单里没有签名（sig）—— 不是官方增量包"
    text = _json.dumps(man, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"))
    try:
        import signing as _sg
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey)
        pub = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(_sg.PUBLIC_KEY_B64))
        pub.verify(base64.b64decode(sig_b64), text.encode("utf-8"))
    except Exception as e:
        return False, "签名校验失败（%s）—— 包被改过或不是官方发的" % type(e).__name__
    bad = []
    items = man.get("files") or []
    for it in items:
        rel = str(it.get("path") or "")
        want = str(it.get("sha256") or "").lower()
        if not rel or not want or ".." in rel:
            bad.append(rel or "?（清单条目不合法）")
            continue
        p = os.path.join(dirpath, "payload", rel.replace("/", os.sep))
        if not os.path.isfile(p):
            bad.append(rel + "（缺文件）")
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b)
        if h.hexdigest() != want:
            bad.append(rel + "（哈希不对）")
    # v1.9.3：脚本自身的哈希也进了清单（受签名保护）。删掉 ps1 里的
    # 验签段想蒙混过关，这里再过一遍。
    scripts = man.get("scripts") or {}
    if not isinstance(scripts, dict) or not scripts:
        return False, "清单里没有脚本哈希（scripts）—— 不是官方增量包"
    for _name, _want in scripts.items():
        _p = os.path.join(dirpath, str(_name))
        if not os.path.isfile(_p):
            return False, "包里缺 %s" % _name
        _h = hashlib.sha256()
        with open(_p, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                _h.update(b)
        if _h.hexdigest() != str(_want).lower():
            return False, ("%s 被改过（哈希不对）—— 不能运行改过的更新脚本"
                           % _name)

    # V1.9.7.1：增量包里还带着「安装版布局」用的那份清单（写进 resources\）。
    # 它决定装过安装版的用户更新完还能不能通过启动自检，必须一起验 ——
    # 不能只靠 ps1 自己比一遍（ps1 本身也可能被换成旧版）。
    _lay = man.get("install_layout") or {}
    if isinstance(_lay, dict):
        for it in (_lay.get("files") or []):
            rel = str(it.get("path") or "")
            want = str(it.get("sha256") or "").lower()
            if not rel or not want or ".." in rel:
                bad.append(rel or "?（安装版清单条目不合法）")
                continue
            p = os.path.join(dirpath, "payload", rel.replace("/", os.sep))
            if not os.path.isfile(p):
                bad.append(rel + "（缺文件）")
                continue
            h = hashlib.sha256()
            with open(p, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
            if h.hexdigest() != want:
                bad.append(rel + "（哈希不对）")

    if bad:
        return False, "payload 校验失败：%s" % "、".join(bad[:5])
    return True, "签名与 %d 个文件全部通过" % len(items)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="小鲸鱼 Agent 后端（无头 API 服务）",
        epilog="示例：python backend_server.py --port 5701")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，只本机）")
    ap.add_argument("--port", type=int, default=0,
                    help="监听端口（默认 0 = 从 5700 起自动探测，跳过 Agent webhook 占用的 5700）")
    ap.add_argument("--log-level", default="info", help="uvicorn 日志级别（默认 info）")
    ap.add_argument("--quiet", action="store_true", help="不打印启动横幅")
    ap.add_argument("--parent-pid", type=int, default=0,
                    help="桌面壳进程号：它退出时后端跟着退出（防孤儿）")
    a, unknown = ap.parse_known_args(argv)
    if unknown:
        _safe_print("[backend] 忽略未识别参数：%s" % " ".join(unknown))

    try:
        import uvicorn
    except Exception as e:
        _safe_print("[backend] 缺少 uvicorn：pip install fastapi uvicorn（%s）" % e)
        return 3

    _ensure_runtime_dirs()
    try:
        _sweep_mei()
    except Exception:
        pass
    _ok, _why = _acquire_instance_lock()
    if not _ok:
        _safe_print("[backend] " + _why, flush=True)
        return 4
    if int(a.parent_pid or 0) > 0:
        _watch_parent(int(a.parent_pid))
    # v1.8.7（QA P0）：先把插件探测预热起来，用户点开界面时 /api/health
    # 已经是缓存命中，不会再因为查 NapCat / Node 而慢到 2 秒。
    start_health_warmer()
    port = build_server(a.host, a.port)
    url = f"http://{SERVER['host']}:{SERVER['port']}"

    # V1.5.3：先把端口落盘，再打印。顺序很重要 —— 打印可能抛 OSError，
    # 但只要文件写了，桌面壳就能接上（它两个来源都看）。
    _write_port_file(url)

    if not a.quiet:
        # 这一行是 Rust 端解析端口的契约，别改格式
        _safe_print(f"[backend] listening on {url}", flush=True)
        _safe_print(f"[backend] health: {url}/api/health　logs: ws://{SERVER['host']}:"
                    f"{SERVER['port']}/ws/logs", flush=True)
        try:
            _safe_print(f"[backend] data={app_ui.DATA_DIR} runtime={RUNTIME_DIR}", flush=True)
        except Exception:
            pass

    try:
        uvicorn.run(app, host=SERVER["host"], port=SERVER["port"],
                    log_level=str(a.log_level or "info"), access_log=False)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            app_ui.stop_agent()
        except Exception:
            pass
        _log("[backend] 服务已退出")
    return 0


if __name__ == "__main__":
    if "--clean-napcat-nodes" in sys.argv[1:]:
        # v1.8.4：UAC 提权入口 —— 前端点「以管理员身份清理」时，
        # 我们用 ShellExecuteW(runas) 拉起「本程序 --clean-napcat-nodes」，
        # 它只做一件事：精确杀掉命令行里带 napcat 的 node.exe，然后退出。
        # 这里不启服务、不建窗口，连实例锁都不抢（否则抢不到会误报「已经在跑」）。
        try:
            import napcat_proc
            sys.exit(napcat_proc.main(sys.argv[1:]))
        except SystemExit:
            raise
        except Exception as _e:
            _safe_print("[backend] 提权清理失败：%s: %s" % (type(_e).__name__, _e))
            sys.exit(5)
    if "--verify-delta" in sys.argv[1:]:
        # v1.9.1：给 apply_delta.ps1 用的验签入口。PowerShell 没有原生 Ed25519，
        # 所以借这个 exe —— 它自己刚被壳验过，是可信的那一个。
        _i = sys.argv.index("--verify-delta")
        _d = sys.argv[_i + 1] if len(sys.argv) > _i + 1 else ""
        try:
            _ok, _msg = _verify_delta_dir(_d)
        except Exception as _e:
            _ok, _msg = False, "校验异常：%s: %s" % (type(_e).__name__, _e)
        _safe_print("[delta] " + _msg)
        sys.exit(0 if _ok else 4)
    if "--agent" in sys.argv[1:]:
        # v1.2.1 修：打包后 app_ui.start_agent() 用 [sys.executable, "--agent"] 拉起 Agent，
        # 而 sys.executable 现在是 backend_server.exe —— 以前这里没接住 --agent，
        # 结果「启动 Agent」等于又起了一个后端（还带着自动启动），能无限自我复制。
        app_ui._run_agent_mode()
        sys.exit(0)
    sys.exit(main())
