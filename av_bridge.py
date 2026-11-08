# -*- coding: utf-8 -*-
"""音视频通话微服务桥（AV Bridge，v1.3.0 新增）

作用
----
独立开发的音视频模块（`音视频接入模块/av_server.py`，FastAPI，端口 5800）不归属
本控制台，这里只做三件事：

1. **拉起/停止**：用 subprocess 把 `av_server.py` 单独拉起来；控制台退出时只关
   自己拉起的那个进程（外面手动开的不会被误杀）。
2. **转发**：把控制台前端的 `call('av_xxx')` 转成对 5800 的 HTTP 请求，
   统一超时、统一错误格式，前端不用关心端口和 URL。
3. **优雅降级**：模块没装/没启动/报错时，一律返回
   `{"ok": false, "msg": "音视频模块未启动", "av_down": true, "hint": ...}`，
   绝不抛异常、绝不影响文字聊天。

设计约束
--------
* 不爽的地方都写在注释里，别在这里加第三方依赖 —— 只用标准库（urllib）。
* 只监听 127.0.0.1；不往公网发任何东西。
* 任何失败都必须是一条能看懂的中文 msg，而不是 traceback。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 音视频模块默认端口（它自己的 config.json 里也是 5800，改这里要两边一起改）
DEFAULT_PORT = 5800
# 拉起后等它自己把 FastAPI 起起来的上限
#   av_server.exe 是 PyInstaller onefile，第一次启动要把内部依赖解包到临时目录，
#   冷机实测 6~15 秒，所以给 60 秒（目录模式一般 2 秒内就好）
START_TIMEOUT = 60.0
# 普通转发超时：/status 要枚举音频设备，实测 3.6 秒（首次还要初始化 CFFI）
DEFAULT_TIMEOUT = 30.0
# 说话/听写这种「本来就慢」的接口单独放宽
LONG_TIMEOUT = 180.0

# 存放 av_server 的目录候选（用户改过 config.av_module_dir 时优先用那个）
# 两种形态都认：源码目录（av_server.py + av_core/）和打包目录（av_server.exe）
_DIR_CANDIDATES = (
    os.path.join("音视频接入模块",),
    os.path.join("av_module",),
    os.path.join("av"),
    # v1.3.2：随包分发的模块放在 resources/av_module（安装版/绿色版都在 exe 旁边）
    os.path.join("resources", "av_module"),
)

_IS_WIN = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _has_server(d: str) -> bool:
    """这个目录能不能当音视频模块用（打包版或源码版）。"""
    return (os.path.isfile(os.path.join(d, "av_server.exe"))
            or os.path.isfile(os.path.join(d, "av_server.py"))
            or os.path.isfile(os.path.join(d, "av_server", "av_server.exe")))


class AVUnavailable(RuntimeError):
    """音视频模块不可用（没装 / 没起 / 端口不通）。"""


def _cfg_get(key: str, default: Any = None) -> Any:
    try:
        import config as _cfg
        return _cfg.get(key, default)
    except Exception:
        return default


def _data_dir() -> str:
    try:
        import app_ui
        return str(app_ui.DATA_DIR)
    except Exception:
        return os.environ.get("QQ_AGENT_DATA_DIR") or BASE_DIR


def _port_free(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex((host, int(port))) != 0
    except Exception:
        return True
    finally:
        try:
            s.close()
        except Exception:
            pass


class AVBridge:
    """音视频微服务的生命周期 + 转发。全进程共用一个实例（BRIDGE）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._log_path = ""
        self._started_at = 0.0
        self._last_error = ""

    # ---------------- 配置 ----------------
    @property
    def port(self) -> int:
        v = _cfg_get("av_module_port", DEFAULT_PORT)
        try:
            p = int(v)
        except Exception:
            p = DEFAULT_PORT
        return p if 1024 <= p <= 65535 else DEFAULT_PORT

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def module_dir(self) -> str:
        """音视频模块根目录（里面有 av_server.exe 或 av_server.py）。找不到返回空串。"""
        raw = str(_cfg_get("av_module_dir", "") or "").strip().strip('"')
        cands = []
        if raw:
            cands.append(raw)
        env = (os.environ.get("AV_MODULE_DIR") or "").strip()
        if env:
            cands.append(env)
        root = _data_dir()
        # v1.6.1：打包后 `BASE_DIR` 是 PyInstaller 的 _MEIPASS，不是安装目录 ——
        # `<安装目录>\resources\av_module` 靠它找不到（server_exe() 另有一套按
        # sys.executable 的搜索，所以能起来，但 module_dir() 空着会让状态页报
        # 「没找到音视频模块」、start_if_enabled() 也直接跳过）。补上 exe 目录。
        exe_dir = ""
        try:
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        except Exception:
            exe_dir = ""
        bases = [root]
        if exe_dir:
            bases.append(exe_dir)
        bases.append(BASE_DIR)
        for base in bases:
            for name in _DIR_CANDIDATES:
                cands.append(os.path.join(base, name))
        # 同级目录（开发时两个项目并排放）
        cands.append(os.path.join(os.path.dirname(root), "音视频接入模块"))
        cands.append(os.path.join(os.path.dirname(BASE_DIR), "音视频接入模块"))
        # PyInstaller onefile：模块被解包到 _MEIPASS/av_module
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            cands.append(os.path.join(meipass, "av_module"))
        seen = set()
        for d in cands:
            d = os.path.abspath(d)
            if d in seen:
                continue
            seen.add(d)
            if _has_server(d):
                return d
        return ""

    def server_exe(self) -> str:
        """av_server.exe 的绝对路径（没有就返回空串）。

        搜索顺序（都是「用户显式指定」优先）：
          1. config.av_module_dir / AV_MODULE_DIR 里
          2. 控制台 exe 旁边的 resources/av_module/（安装版、绿色版都在这）
          3. 数据目录下的同名候选（开发机）
          4. PyInstaller _MEIPASS/av_module/（老版本把模块塞进 backend exe 的兜底）
        目录模式（--onedir）的产物是 av_server/av_server.exe，也认。
        """
        cands = []
        raw = str(_cfg_get("av_module_dir", "") or "").strip().strip('"')
        if raw:
            cands.append(raw)
        env = (os.environ.get("AV_MODULE_DIR") or "").strip()
        if env:
            cands.append(env)

        # 控制台自身所在目录（打包版就是 backend_server.exe 的目录）
        near = []
        if getattr(sys, "frozen", False):
            near.append(os.path.dirname(os.path.abspath(sys.executable)))
        near.append(BASE_DIR)
        root = _data_dir()
        for base in list(near) + [root]:
            for name in _DIR_CANDIDATES:
                cands.append(os.path.join(base, name))
            cands.append(os.path.join(base, "av_module"))
            cands.append(base)                       # exe 直接和 backend 并排

        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            cands.append(os.path.join(meipass, "av_module"))

        seen = set()
        for d in cands:
            try:
                d = os.path.abspath(d)
            except Exception:
                continue
            if d in seen:
                continue
            seen.add(d)
            for exe in (os.path.join(d, "av_server.exe"),
                        os.path.join(d, "av_server", "av_server.exe")):
                if os.path.isfile(exe):
                    return exe
        return ""

    def _launcher(self) -> str:
        """'exe' / 'python' / ''（都没有）。"""
        if self.server_exe():
            return "exe"
        if self.module_dir() and self._python():
            return "python"
        return ""

    def _python(self) -> str:
        """跑 av_server.py 的解释器。

        源码版直接用当前解释器；打包版（backend_server.exe）的 sys.executable
        是 exe 本身，跑不了脚本 —— 这时必须让用户在设置里填 av_python。
        """
        raw = str(_cfg_get("av_python", "") or "").strip().strip('"')
        if raw and os.path.isfile(raw):
            return raw
        env = (os.environ.get("AV_MODULE_PYTHON") or "").strip()
        if env and os.path.isfile(env):
            return env
        if not getattr(sys, "frozen", False):
            return sys.executable or "python"
        for name in ("python", "python3", "py"):
            p = shutil.which(name)
            if p:
                return p
        return ""

    def _log_file(self) -> str:
        d = os.path.join(_data_dir(), "runtime")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return os.path.join(d, "av_module.log")

    # ---------------- 探活 ----------------
    def health(self, timeout: float = 2.5) -> Optional[Dict[str, Any]]:
        try:
            return self._request("GET", "/api/av/health", None, timeout=timeout)
        except Exception:
            return None

    def running(self) -> bool:
        h = self.health()
        return bool(h and h.get("ok"))

    def status_snapshot(self) -> Dict[str, Any]:
        """给前端用的一站式状态（模块自身没起也不报错）。"""
        out: Dict[str, Any] = {
            "ok": True,
            "module": "av",
            "enabled": bool(_cfg_get("av_module_enabled", False)),
            "dir": self.module_dir(),
            "port": self.port,
            "base": self.base_url,
            "python": self._python(),
            "exe": self.server_exe(),
            "launcher": self._launcher(),
            "managed": bool(self._proc and self._proc.poll() is None),
            "running": False,
            "uptime": 0.0,
            "last_error": self._last_error,
            "hint": "",
            "remote": {},
            "log": [],
            "msg": "",
        }
        h = self.health()
        if h and h.get("ok"):
            out["running"] = True
            out["uptime"] = round(time.time() - self._started_at, 1) if self._started_at else 0.0
            out["remote"] = h
            return out
        if not out["dir"]:
            out["hint"] = ("没找到音视频模块（av_server.exe / av_server.py 都没有）。"
                           "安装版应自带 resources\\av_module\\av_server.exe；"
                           "源码运行时请放到控制台同级目录，或在设置里指 av_module_dir。")
        elif not out["python"]:
            out["hint"] = ("打包版需要用 python 跑 av_server.py，但没找到解释器。"
                           "请在设置里填 av_python（例如 C:\\Python311\\python.exe）。")
        else:
            out["hint"] = "模块已就绪但没在运行，点「启动音视频模块」即可。"
        out["msg"] = out["hint"]
        try:
            _txt = self._tail_log(self._log_file(), 12) or ""
            out["log"] = [ln for ln in _txt.splitlines() if ln.strip()]
        except Exception:
            out["log"] = []
        return out

    # ---------------- HTTP ----------------
    def _request(self, method: str, path: str, body: Any = None,
                 timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                txt = raw.decode("utf-8", "replace")
                if not txt.strip():
                    return {"ok": True, "status": resp.status}
                try:
                    out = json.loads(txt)
                except Exception:
                    return {"ok": True, "status": resp.status, "text": txt[:2000]}
                if isinstance(out, dict):
                    out.setdefault("ok", resp.status < 400)
                    return out
                return {"ok": True, "status": resp.status, "data": out}
        except urllib.error.HTTPError as e:
            # 400/403/409 这类是模块的业务性拒绝，把它的 msg 原样带回去
            try:
                payload = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                payload.setdefault("ok", False)
                payload.setdefault("msg", f"HTTP {e.code}")
                return payload
            return {"ok": False, "msg": f"HTTP {e.code}: {e.reason}"}
        except urllib.error.URLError as e:
            raise AVUnavailable(str(getattr(e, "reason", e))) from e
        except Exception as e:
            raise AVUnavailable(f"{type(e).__name__}: {e}") from e

    def call(self, method: str, path: str, body: Any = None,
             timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """所有转发都走这里：模块不可用时给出统一的中文降级结果。"""
        try:
            out = self._request(method, path, body, timeout=timeout)
            out.setdefault("av_up", True)
            return out
        except AVUnavailable as e:
            self._last_error = str(e)
            return self.unavailable(str(e))
        except Exception as e:  # pragma: no cover - 兜底
            self._last_error = f"{type(e).__name__}: {e}"
            return self.unavailable(self._last_error)

    def unavailable(self, why: str = "") -> Dict[str, Any]:
        s = self.status_snapshot()
        msg = "音视频模块未启动"
        if not s["dir"]:
            msg = "音视频模块未安装（找不到 av_server.py）"
        return {
            "ok": False,
            "av_down": True,
            "av_up": False,
            "msg": msg,
            "why": why,
            "hint": s.get("hint") or "点「音视频通话」页里的「启动音视频模块」。",
            "port": self.port,
            "dir": s["dir"],
        }

    # ---------------- 启停 ----------------
    def start(self, wait: float = START_TIMEOUT) -> Dict[str, Any]:
        # V1.0.0-Beta 修：以前整只 start() 都在 self._lock 里，包括最后「最多等 25 秒
        # 探活」的循环 —— 结果 shutdown() 拿不到锁，控制台退出时会被拖住 25 秒，
        # 等不及就被强杀，子进程成了孤儿（实测：5811 上的 av_server 退出后还在跑）。
        # 现在只把「决策 + spawn」放在锁里，探活循环放到锁外。
        with self._lock:
            if self.health():
                return {"ok": True, "msg": "音视频模块已经在运行",
                        "running": True, "managed": bool(self._proc and self._proc.poll() is None),
                        "port": self.port}
            exe = self.server_exe()
            d = os.path.dirname(exe) if exe else self.module_dir()
            py = ""
            if not exe:
                if not d:
                    return {"ok": False,
                            "msg": "找不到音视频模块（av_server.exe 或 av_server.py 都没有）",
                            "hint": "安装版应该自带 resources\\av_module\\av_server.exe；"
                                    "源码运行请先 pip install -r requirements.txt，"
                                    "或在设置里把 av_module_dir 指过去。", "dir": ""}
                py = self._python()
                if not py:
                    return {"ok": False, "msg": "没找到 python 解释器，无法拉起音视频模块",
                            "hint": "安装包里的 av_server.exe 丢了？重新安装或把 "
                                    "av_module_dir 指向带 av_server.exe 的目录。"}
            if not _port_free(self.port) and not self.health():
                return {"ok": False, "msg": f"端口 {self.port} 被别的程序占着",
                        "hint": f"netstat -ano | findstr :{self.port} 看看是谁；"
                                f"或在设置里换 av_module_port。"}
            log_path = self._log_file()
            try:
                lf = open(log_path, "a", encoding="utf-8", errors="replace")
            except Exception:
                lf = subprocess.DEVNULL
            env = dict(os.environ)
            env.setdefault("PYTHONIOENCODING", "utf-8")
            env.setdefault("PYTHONUNBUFFERED", "1")
            # 打包版把配置/模型/缓存放到控制台数据目录下，绿色版换机也能整包带走
            env.setdefault("AV_MODULE_HOME", os.path.join(_data_dir(), "av_module"))
            if exe:
                args = [exe, "--host", "127.0.0.1", "--port", str(self.port)]
            else:
                args = [py, "-X", "utf8", "av_server.py", "--port", str(self.port)]
            flags = 0
            if _IS_WIN:
                flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
            try:
                proc = subprocess.Popen(
                    args, cwd=d, env=env, stdin=subprocess.DEVNULL,
                    stdout=lf, stderr=subprocess.STDOUT,
                    creationflags=flags, close_fds=True,
                )
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                return {"ok": False, "msg": f"拉起音视频模块失败：{e}",
                        "hint": ("确认 av_server.exe 没被杀软隔离（右键→属性→解除锁定），"
                                 if exe else
                                 "确认 av_python 指向的解释器装过 requirements.txt。")}
            finally:
                try:
                    if lf not in (subprocess.DEVNULL, None):
                        lf.close()  # 子进程已经拿到句柄，父进程这份可以关
                except Exception:
                    pass
            self._proc = proc
            self._started_at = time.time()
            self._log_path = log_path
            self._last_error = ""
            # 让子进程跟着父进程死（父被强杀也不留孤儿）
            _assign_killjob(proc)
        # ---- 锁外探活（可以慢，但不能堵住 shutdown）----
        if True:
            deadline = time.time() + max(3.0, wait)
            while time.time() < deadline:
                if proc.poll() is not None:
                    tail = self._tail_log(log_path, 12)
                    self._last_error = f"进程启动后立即退出（code={proc.returncode}）"
                    return {"ok": False, "msg": self._last_error, "exit_code": proc.returncode,
                            "hint": "看下面的日志尾巴（多半是依赖没装）。", "log": tail,
                            "log_path": log_path}
                if self.health(timeout=1.5):
                    how = "exe" if exe else "python"
                    return {"ok": True,
                            "msg": f"音视频模块已启动（端口 {self.port}，{how}）",
                            "running": True, "managed": True, "pid": proc.pid,
                            "launcher": how, "port": self.port, "dir": d,
                            "log_path": log_path}
                time.sleep(0.4)
            self._last_error = f"启动超时：{int(max(3.0, wait))} 秒内 {self.port} 没响应"
            return {"ok": False, "msg": self._last_error,
                    "hint": "首次启动要加载音频设备，慢一点正常；再等几秒点「刷新」看看，"
                            "还不行就看日志尾巴。",
                    "log": self._tail_log(log_path, 15), "log_path": log_path}

    def stop(self, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                pid = proc.pid
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=6)
                    except Exception:
                        self._kill_tree(pid)
                except Exception as e:
                    self._last_error = f"{type(e).__name__}: {e}"
                self._proc = None
                self._started_at = 0.0
                # onefile 的 av_server.exe 是「引导进程 + 真正服务进程」两层：
                # terminate() 只结束引导进程，真正监听端口的是它的子进程，
                # 有时要好几秒才退（uvicorn 优雅收尾），端口一直被占着。
                # 所以：先给它 2 秒；还活着就直接按端口找到监听进程，整棵树强杀；
                # 再最多等 6 秒确认端口真的释放。
                alive = False
                for _ in range(5):
                    alive = self.running()
                    if not alive:
                        break
                    time.sleep(0.4)
                if alive:
                    for victim in {pid, self._pid_on_port(self.port)}:
                        if not victim:
                            continue
                        try:
                            self._kill_tree(victim)
                        except Exception:
                            pass
                    for _ in range(15):
                        alive = self.running()
                        if not alive:
                            break
                        time.sleep(0.4)
                return {"ok": True, "msg": "音视频模块已停止（本控制台拉起的那个）",
                        "running": alive}
            self._proc = None
            if not self.running():
                return {"ok": True, "msg": "音视频模块本来就没在运行", "running": False}
            if not force:
                return {"ok": False, "msg": "5800 上的音视频服务不是本控制台启动的，已跳过",
                        "hint": "确认要关就给 force=true（会按端口找进程并结束它）。",
                        "running": True}
            pid = self._pid_on_port(self.port)
            if not pid:
                return {"ok": False, "msg": f"找不到监听 {self.port} 的进程", "running": True}
            try:
                self._kill_tree(pid)
            except Exception as e:
                return {"ok": False, "msg": f"结束进程 {pid} 失败：{e}", "running": True}
            time.sleep(0.6)
            return {"ok": True, "msg": f"已结束端口 {self.port} 上的进程 {pid}",
                    "running": self.running()}

    def _kill_tree(self, pid: int) -> None:
        if _IS_WIN:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                           capture_output=True, timeout=15,
                           creationflags=_CREATE_NO_WINDOW)
        else:
            os.kill(int(pid), 15)

    @staticmethod
    def _pid_on_port(port: int) -> int:
        """netstat 找监听端口的 PID（只在本机用，够用且不用装 psutil）。"""
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, timeout=15,
                                 creationflags=_CREATE_NO_WINDOW if _IS_WIN else 0)
            txt = out.stdout.decode("utf-8", "replace")
        except Exception:
            return 0
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" \
                    and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                try:
                    return int(parts[4])
                except Exception:
                    continue
        return 0

    @staticmethod
    def _tail_log(path: str, lines: int = 15) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                rows = f.read().splitlines()
            return "\n".join(rows[-max(1, lines):])
        except Exception:
            return ""

    def tail_log(self, lines: int = 60) -> Dict[str, Any]:
        p = self._log_path or self._log_file()
        return {"ok": True, "path": p, "text": self._tail_log(p, lines)}

    def start_if_enabled(self) -> Dict[str, Any]:
        """后端启动时调用：只在**用户确实开着**音视频模块时才拉。

        v1.6.1（QA P0-1 修复）：以前这里只看 `av_auto_launch`（默认 true），
        用户把「音视频模块」关掉之后，后端启动时照样把 av_server 拉起来 ——
        关了还在后台跑、还占着 5800，而且拉起来之后又反手把
        `av_module_enabled` 写成 true（用户关的开关被改回来了）。

        现在的判据（三条都满足才拉）：
        1. `av_module_enabled` 为真（用户主动开的；这是唯一的总闸）；
        2. `av_auto_launch` 为真（允许控制台启动时自动带起来）；
        3. 能找到 `av_server.py`。

        失败一律只返回结果、不抛异常：少一个功能，不能影响文字聊天。
        """
        if not bool(_cfg_get("av_module_enabled", False)):
            return {"ok": True, "running": False, "skipped": True,
                    "msg": "音视频模块没开（av_module_enabled=false），不启动"}
        if not bool(_cfg_get("av_auto_launch", False)):
            return {"ok": True, "running": False, "skipped": True,
                    "msg": "音视频模块没开自动启动（av_auto_launch=false）"}
        try:
            d = self.module_dir()
        except Exception:
            d = ""
        if not d:
            return {"ok": True, "skipped": True, "no_module": True,
                    "msg": "没找到音视频模块目录（av_server.py），跳过（不影响其他功能）"}
        return self.start()

    def shutdown(self) -> None:
        """控制台退出时收尾：只关自己拉起的那只。"""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    self._kill_tree(proc.pid)
            except Exception:
                pass



# ---------------- Windows：让子进程跟着父进程一起死 ----------------
# 为什么需要：控制台被强杀（Tauri 关窗走 `taskkill /T /F`，或任务管理器直接结束）
# 时，Python 的 finally 根本不会执行 —— 实测 av_server 会变成孤儿进程，还占着 5800。
# 解法是把它放进一个「句柄关闭就杀光成员」的 Job Object：
# 父进程一消失，句柄自动关闭，Windows 内核替我们把子进程收掉。
_JOB_HANDLE = None  # 必须一直持有：句柄一关，成员进程立刻被杀


def _assign_killjob(proc) -> bool:
    """把刚 spawn 的子进程塞进 kill-on-close 的 Job（仅 Windows，失败就算了）。"""
    global _JOB_HANDLE
    if not _IS_WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes as wt

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                        ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong),
                        ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong),
                        ("OtherTransferCount", ctypes.c_ulonglong)]

        class BASIC_LIMITS(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wt.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wt.DWORD),
                        ("SchedulingClass", wt.DWORD)]

        class EXT_LIMITS(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMITS),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if _JOB_HANDLE is None:
            h = k32.CreateJobObjectW(None, None)
            if not h:
                return False
            info = EXT_LIMITS()
            info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            ok = k32.SetInformationJobObject(
                wt.HANDLE(h), 9, ctypes.byref(info), ctypes.sizeof(info))
            if not ok:
                k32.CloseHandle(wt.HANDLE(h))
                return False
            _JOB_HANDLE = h
        # 拿子进程的句柄（Popen 在 Windows 上持的是 Win32 句柄）
        hproc = wt.HANDLE(int(proc._handle))  # noqa: SLF001 - 故意的，标准做法
        return bool(k32.AssignProcessToJobObject(wt.HANDLE(_JOB_HANDLE), hproc))
    except Exception:
        return False

BRIDGE = AVBridge()


# ---------------- 便捷转发（backend_server 的路由直接用） ----------------
def speak(text: str, voice: str = "", engine: str = "", block: bool = True) -> Dict[str, Any]:
    body: Dict[str, Any] = {"text": text, "block": bool(block)}
    if voice:
        body["voice"] = voice
    if engine:
        body["engine"] = engine
    return BRIDGE.call("POST", "/api/av/speak", body, timeout=LONG_TIMEOUT)


def listen(seconds: float = 6.0, mode: str = "loopback", language: str = "") -> Dict[str, Any]:
    body: Dict[str, Any] = {"seconds": float(seconds), "mode": mode}
    if language:
        body["language"] = language
    return BRIDGE.call("POST", "/api/av/listen", body, timeout=LONG_TIMEOUT)


# ---- v1.8.0：自定义音色（声音克隆）----
def tts_custom_get() -> Dict[str, Any]:
    """查自定义音色配好没、缺什么。"""
    return BRIDGE.call("GET", "/api/av/tts/custom", None, timeout=DEFAULT_TIMEOUT)


def tts_custom_set(payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """改自定义音色配置；payload里可以带 ref_audio_b64 直接上传参考音频。"""
    return BRIDGE.call("POST", "/api/av/tts/custom", payload or {},
                         timeout=LONG_TIMEOUT)


def tts_custom_test(text: str = "", block: bool = True) -> Dict[str, Any]:
    """试听一句：走 custom_voice，失败会自动退回 edge并带 note。"""
    body: Dict[str, Any] = {"block": bool(block)}
    if text:
        body["text"] = text
    return BRIDGE.call("POST", "/api/av/tts/custom/test", body,
                         timeout=LONG_TIMEOUT)


def preview_data_url() -> Dict[str, Any]:
    """把 5800 的 /api/av/preview.jpg 变成 dataURL，前端 <img src> 直接用。

    为什么不直接让 <img> 指 5800：前端在 Tauri 里跑，还要兼容网页版，
    走同源转发最省事（也顺便躲开浏览器对 localhost 跨端口的怪癖）。
    """
    import base64
    url = BRIDGE.base_url + "/api/av/preview.jpg"
    try:
        req = urllib.request.Request(url, headers={"Accept": "image/jpeg"})
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            raw = resp.read()
        if not raw:
            return {"ok": False, "msg": "预览图是空的（推流没开？）", "av_up": True}
        return {"ok": True, "av_up": True,
                "data": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
                "bytes": len(raw), "ts": time.time()}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "av_up": True, "msg": "还没有预览图，先点「开始推流」"}
        return {"ok": False, "av_up": True, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return BRIDGE.unavailable(f"{type(e).__name__}: {e}")
