# -*- coding: utf-8 -*-
"""Agent 总闸门（一级警告 + 服务启停）。

为什么单独一个模块：Agent 能开浏览器、动键鼠、跑代码，关掉的代价最高，
所以「能不能用」这件事不该散落在 index.html 或 app_ui 里，得有一个地方
既管前端弹窗文案，也管后端 10 秒冷静期的校验，还负责把相关服务收干净。

流程：
    前端 agent_gate_issue()  → 拿到 token + 风险清单 + 最少等待秒数
    用户看满 10 秒、勾选同意    → agent_enable(token)
    后端 verify(token) 通过   → 写 agent_enabled=True，返回 ok

关掉（agent_disable）不需要 token —— 关是安全方向，永远秒关。
"""
from __future__ import annotations

import json
import secrets
import hashlib
import hmac
import io
import os
import secrets
import threading
import time
from typing import Any, Dict, List

MIN_WAIT = 10.0          # 用户必须看满这么多秒才能点「同意开启」
TOKEN_TTL = 600.0        # 挑战令牌 10 分钟内有效

TITLE = "启用之前，请先读完这段话"

RISKS: List[Dict[str, str]] = [
    {"t": "它会在你的电脑上动真格",
     "d": "开启后，Agent 可以自己打开浏览器搜索、点鼠标、敲键盘、在沙箱里跑代码。"
          "这些动作发生在你的真实桌面上，不是演示。"},
    {"t": "它可能看到你的隐私",
     "d": "搜索和截图会经过模型接口。屏幕上如果正开着聊天窗口、网银、邮箱，"
          "内容有可能被一起截进画面。用之前先把手头的敏感窗口关掉。"},
    {"t": "它可能误操作",
     "d": "鼠标点错位置、把内容输进别的窗口、重复提交表单，都是真实会发生的事。"
          "危险动作默认会先问你（涉及键鼠、代码、打电话），但请把它当成「实习生」，不要当成「保险」。"},
    {"t": "它会花你的 token",
     "d": "每一步都要调一次模型。一个任务走 10 步，就是 10 次请求。"
          "着急的时候记得在 Agent 页点红色「紧急停止」，或按 Ctrl+Alt+K。"},
    {"t": "关掉很容易",
     "d": "任何时候把「启用 Agent」关掉，相关服务会立刻停（浏览器、热键监听、动作日志写入）。"
          "关掉之后它不会在后台偷偷干活。"},
]

_LOCK = threading.Lock()
_USED = set()


# ---------------- 配置读写 ----------------

def enabled() -> bool:
    """总开关。没显式打开就是关的（默认值也在 config.DEFAULTS 里）。"""
    try:
        import config as _cfg
        return bool(_cfg.get("agent_enabled", False))
    except Exception:
        return False


def set_enabled(on: bool) -> None:
    try:
        import config as _cfg
        # 走 set_checked：它会写盘并刷新内存缓存（早先手搓 load/save 会被缓存盖掉，
        # 表现就是「点开开关、界面显示已启用、过两秒又变回关闭」）
        _cfg.set_checked("agent_enabled", bool(on))
    except Exception:
        pass


# ---------------- 10 秒冷却挑战 ----------------

def _secret() -> bytes:
    """每个数据目录一个密钥（第一次用的时候生成并落盘）。"""
    try:
        import config as _cfg
        base = str(_cfg.DATA_DIR)
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "agent_gate.key")
    try:
        if os.path.exists(path):
            return io.open(path, "rb").read()
        s = secrets.token_bytes(32)
        os.makedirs(base, exist_ok=True)
        io.open(path, "wb").write(s)
        return s
    except Exception:
        return b"xiaojingyu-agent-gate-fallback"


def _sign(ts: float) -> str:
    msg = ("%.3f" % ts).encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()[:32]


def issue() -> Dict[str, Any]:
    """给前端一份挑战：风险清单 + 令牌 + 最少等待秒数。

    令牌是无状态的（时间戳 + 签名），后端重启、多进程都不会丢 —— 早先用内存字典存，
    结果「点了确认提示令牌已过期」，一查是 backend 进程被重拉过。
    """
    ts = time.time()
    tok = "%s.%s" % (("%.3f" % ts), _sign(ts))
    return {"ok": True, "token": tok, "wait": MIN_WAIT, "title": TITLE,
            "risks": list(RISKS), "ttl": TOKEN_TTL,
            "enabled": enabled()}


def verify(token: str) -> Dict[str, Any]:
    """令牌必须存在、没用过、并且距离签发已经满 MIN_WAIT 秒。"""
    tok = str(token or "").strip()
    if not tok:
        return {"ok": False, "msg": "缺少令牌：请从弹窗里的按钮重新走一遍"}
    now = time.time()
    # 令牌形如 "1761.234.<签名>"：时间戳本身带小数点，只能从右边切一刀
    if "." not in tok:
        return {"ok": False, "msg": "令牌格式不对，请重新点「启用 Agent」"}
    ts_s, sign = tok.rsplit(".", 1)
    try:
        ts = float(ts_s)
    except Exception:
        return {"ok": False, "msg": "令牌格式不对，请重新点「启用 Agent」"}
    if not hmac.compare_digest(sign, _sign(ts)):
        return {"ok": False, "msg": "令牌对不上（可能是改过 JS 想绕开等待），请重新点「启用 Agent」"}
    if now - ts > TOKEN_TTL:
        return {"ok": False, "msg": "令牌已过期（超过 10 分钟），请重新点「启用 Agent」"}
    waited = now - ts
    if waited < MIN_WAIT - 0.05:
        left = MIN_WAIT - waited
        return {"ok": False, "msg": "还要再等 %.1f 秒（说明要看满 10 秒）" % left,
                "left": round(left, 1)}
    with _LOCK:
        if tok in _USED:
            return {"ok": False, "msg": "这个令牌已经用过了，请重新点「启用 Agent」"}
        _USED.add(tok)
        if len(_USED) > 200:
            _USED.clear()
    return {"ok": True, "msg": "校验通过"}


def consume_and_enable(token: str) -> Dict[str, Any]:
    v = verify(token)
    if not v.get("ok"):
        return v
    set_enabled(True)
    started = start_services()
    return {"ok": True, "msg": "Agent 已启用（%s）" % ("服务已就绪" if started.get("ok") else started.get("msg", "")),
            "services": started}


def disable() -> Dict[str, Any]:
    set_enabled(False)
    stopped = stop_services()
    return {"ok": True, "msg": "Agent 已关闭，相关服务已停", "services": stopped}


# ---------------- 服务启停 ----------------

def start_services() -> Dict[str, Any]:
    """只有真启用时才拉起：热键监听 +（可选）执行器状态。浏览器按需再起。"""
    if not enabled():
        return {"ok": False, "msg": "总开关是关的"}
    out = {"ok": True, "msg": "", "items": []}
    try:
        import computer_use as cu
        r = cu.install_hotkey()
        out["items"].append({"name": "hotkey", "ok": bool(r.get("ok")), "msg": r.get("msg", "")})
        if not r.get("ok"):
            out["msg"] = r.get("msg", "") or "热键监听没起来"
    except Exception as e:
        out["ok"] = False
        out["msg"] = "热键监听启动失败：%s" % e
    return out


def stop_services() -> Dict[str, Any]:
    out = {"ok": True, "msg": "", "items": []}
    # 1) 热键监听
    try:
        import computer_use as cu
        out["items"].append({"name": "hotkey", "ok": True, "msg": cu.uninstall_hotkey().get("msg", "")})
    except Exception as e:
        out["items"].append({"name": "hotkey", "ok": False, "msg": str(e)})
    # 2) 正在跑的任务：请求停
    try:
        import computer_use as cu
        cu.stop_now("关闭 Agent")
    except Exception:
        pass
    # 3) QQ 通话护栏的计数不重置，但把待授权队列清掉
    try:
        import qq_approve
        out["items"].append({"name": "approvals", "ok": True, "msg": qq_approve.clear().get("msg", "")})
    except Exception:
        pass
    return out


def status() -> Dict[str, Any]:
    try:
        import computer_use as cu
        hk = bool(cu._HOTKEY_ON.get("on"))
    except Exception:
        hk = False
    return {"ok": True, "enabled": enabled(), "hotkey": hk, "min_wait": MIN_WAIT,
            "title": TITLE, "risks": list(RISKS)}
