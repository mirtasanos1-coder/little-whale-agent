# -*- coding: utf-8 -*-
"""主动打电话（v1.4.0）：护栏在这里，手在音视频模块里。

分工要说清楚，不然以后谁也搞不懂为什么「它半夜打电话」：
  - **这个文件管「该不该打」**：开关、时段、一小时几次、对方在不在线。
  - **`resources/av_module` 管「怎么打」**：UIAutomation 去 QQ 里点
    「语音通话 / 视频通话」，那部分代码跑在 av_server.exe 里。

护栏都是「宁可少打一通，也不半夜惊动人」的写法：
  - `agent_call_enabled` 默认关；
  - 只在 `agent_call_start_hour` ~ `agent_call_end_hour` 之间（支持跨零点）；
  - 一小时内最多 `agent_call_max_per_hour` 次（默认 1）；
  - 打之前先看在线状态；拿不到在线状态时**放行但记日志**（QQ 接口大多数
    拿不到在线状态，硬拦就等于永远打不出去）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

_LOCK = threading.RLock()


# ---------------------------------------------------------------- 配置 / 状态


def _cfg(key: str, default: Any = None) -> Any:
    try:
        import config as _c
        c = _c.load()
        if isinstance(c, dict) and key in c:
            return c[key]
    except Exception:
        pass
    return default


def _on(key: str, default: bool = False) -> bool:
    v = _cfg(key, default)
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


def _data_dir() -> str:
    d = os.environ.get("QQ_AGENT_DATA_DIR", "").strip()
    if d:
        return d
    try:
        import app_ui
        return str(app_ui.DATA_DIR)
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".xiaojingyu")


def _state_path() -> str:
    d = os.path.join(_data_dir(), "call")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "call_state.json")


def _load_state() -> Dict[str, Any]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(d: Dict[str, Any]) -> None:
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _log(msg: str, level: str = "INFO") -> None:
    try:
        import agent
        agent.log(msg, level)
    except Exception:
        try:
            print("[%s] %s" % (level, msg))
        except Exception:
            pass


def _hour_window_ok() -> tuple:
    """当前时间在不在允许的时段里。支持 22→9 这种跨零点。"""
    start = int(_cfg("agent_call_start_hour", 9) or 9)
    end = int(_cfg("agent_call_end_hour", 22) or 22)
    h = datetime.now().hour
    if start == end:
        # 「9 点到 9 点」按「只在那一小时」算；想要全天就把两个数拉开（0 到 23）
        ok = h == start
    elif start < end:
        ok = start <= h < end
    else:
        ok = h >= start or h < end
    return ok, start, end


def _calls_in_last_hour() -> List[float]:
    st = _load_state()
    now = time.time()
    ts = [float(x) for x in (st.get("calls") or []) if isinstance(x, (int, float, str))]
    keep = [t for t in ts if now - t < 3600]
    return keep


def guard(kind: str = "voice") -> Dict[str, Any]:
    """护栏总检查。返回 {ok, why}。"""
    if not _on("agent_call_enabled", False):
        return {"ok": False, "why": "主动打电话的总开关是关的（设置 → Agent 设置 → 允许它给你打电话）",
                "code": "disabled"}
    ok, start, end = _hour_window_ok()
    if not ok:
        return {"ok": False, "code": "hours",
                "why": "现在不在允许打电话的时段（%02d:00–%02d:00）" % (start, end)}
    maxn = int(_cfg("agent_call_max_per_hour", 1) or 1)
    used = _calls_in_last_hour()
    if len(used) >= maxn:
        return {"ok": False, "code": "quota",
                "why": "这一小时已经打过 %d 通了（上限 %d）" % (len(used), maxn)}
    return {"ok": True, "why": ""}


def state() -> Dict[str, Any]:
    ok, start, end = _hour_window_ok()
    used = _calls_in_last_hour()
    st = _load_state()
    return {
        "enabled": _on("agent_call_enabled", False),
        "window": [start, end],
        "in_window": ok,
        "used_last_hour": len(used),
        "max_per_hour": int(_cfg("agent_call_max_per_hour", 1) or 1),
        "last_call_at": st.get("last_call_at") or "",
        "last_target": st.get("last_target") or "",
        "autocallback": _on("agent_call_autocallback", False),
        "check_online": _on("agent_call_check_online", True),
    }


def _mark_called(target: str, kind: str) -> None:
    with _LOCK:
        st = _load_state()
        ts = [float(x) for x in (st.get("calls") or []) if isinstance(x, (int, float, str))]
        now = time.time()
        ts = [t for t in ts if now - t < 3600] + [now]
        st["calls"] = ts
        st["last_call_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st["last_target"] = target
        st["last_kind"] = kind
        _save_state(st)


# ---------------------------------------------------------------- 在线状态


def _friend_entry(name_or_uin: str) -> Optional[Dict[str, Any]]:
    """在好友列表里找人（名字或 QQ 号都能匹配）。"""
    try:
        import agent
        r = agent.napcat("get_friend_list", timeout=6)
    except Exception:
        return None
    data = r.get("data") if isinstance(r, dict) else None
    if not isinstance(data, list):
        return None
    key = (name_or_uin or "").strip()
    if not key:
        return None
    for f in data:
        if not isinstance(f, dict):
            continue
        uid = str(f.get("user_id") or "")
        names = [str(f.get("remark") or ""), str(f.get("nickname") or "")]
        if key == uid or key in names:
            return f
    return None


def online_check(name_or_uin: str) -> Dict[str, Any]:
    """尽量查在线状态。查不到就老实说查不到，不假装在线。"""
    f = _friend_entry(name_or_uin)
    if not f:
        return {"ok": True, "known": False, "msg": "好友列表里没找到「%s」，按未知在线状态处理" % name_or_uin}
    for k in ("online", "is_online", "status"):
        if k in f:
            v = f.get(k)
            online = bool(v) if not isinstance(v, str) else v.strip().lower() in ("1", "true", "online")
            return {"ok": online, "known": True, "raw": v,
                    "msg": "对方%s在线" % ("" if online else "不")}
    return {"ok": True, "known": False, "friend": {"user_id": f.get("user_id"),
                                                   "nickname": f.get("nickname"),
                                                   "remark": f.get("remark")},
            "msg": "QQ 接口没给在线状态，按未知处理"}


# ---------------------------------------------------------------- 拨打 / 挂断


def _bridge():
    import av_bridge
    return av_bridge.BRIDGE


def ensure_av() -> Dict[str, Any]:
    """点「打语音」时微服务必须真的起来 —— 不再回一句「没启动」就完事。"""
    b = _bridge()
    try:
        if b.running():
            return {"ok": True, "started": False, "msg": "音视频模块已在运行"}
    except Exception:
        pass
    r = b.start()
    ok = bool(r.get("ok"))
    return {"ok": ok, "started": ok,
            "msg": (r.get("msg") or ("音视频模块已启动" if ok else "音视频模块起不来"))}


def dial(kind: str = "voice", name: str = "", session: str = "",
         force: bool = False) -> Dict[str, Any]:
    """主动打一通。force=True 只跳过护栏里的「开关」以外的软限制？不 —— 谁都跳过不了时段。"""
    import computer_use as cu
    t0 = time.time()
    kind = (kind or "voice").lower()
    if kind not in ("voice", "video"):
        return {"ok": False, "msg": "只会打语音（voice）和视频（video）"}
    name = (name or "").strip()
    if not name:
        return {"ok": False, "msg": "要打给谁？给个名字或 QQ 号"}

    g = guard(kind)
    if not g["ok"]:
        cu.log_action("call", "dial_blocked", {"kind": kind, "name": name, "why": g["why"]},
                      False, 0, g["why"], session=session)
        return {"ok": False, "msg": g["why"], "blocked": g.get("code", "")}

    if _on("agent_call_check_online", True):
        oc = online_check(name)
        if oc.get("known") and not oc.get("ok"):
            msg = "没打：%s" % oc.get("msg", "对方不在线")
            cu.log_action("call", "dial_blocked", {"kind": kind, "name": name, "why": msg},
                          False, 0, msg, session=session)
            return {"ok": False, "msg": msg, "blocked": "offline"}

    av = ensure_av()
    if not av["ok"]:
        cu.log_action("call", "dial_failed", {"kind": kind, "name": name},
                      False, int((time.time() - t0) * 1000), av["msg"], session=session)
        return {"ok": False, "msg": av["msg"] + "（到【音视频】页点「启动微服务」看看报什么错）"}

    try:
        r = _bridge().call("POST", "/api/av/rpa/call", {"kind": kind, "name": name}, timeout=30)
    except Exception as e:
        r = {"ok": False, "msg": "调用音视频模块出错：%s" % e}
    ms = int((time.time() - t0) * 1000)
    if r.get("ok"):
        _mark_called(name, kind)
        msg = "拨出去了：%s（%s）" % (name, "视频" if kind == "video" else "语音")
        cu.log_action("call", "dial", {"kind": kind, "name": name}, True, ms, msg, session=session)
        _log("[打电话] %s" % msg, "WARN")
        return {"ok": True, "msg": msg, "detail": r, "state": state()}
    msg = "没拨出去：%s" % (r.get("error") or r.get("msg") or "未知原因")
    cu.log_action("call", "dial_failed", {"kind": kind, "name": name, "detail": r},
                  False, ms, msg, session=session)
    return {"ok": False, "msg": msg, "detail": r}


def hangup(force: bool = True, session: str = "") -> Dict[str, Any]:
    import computer_use as cu
    t0 = time.time()
    av = ensure_av()
    if not av["ok"]:
        return {"ok": False, "msg": av["msg"]}
    try:
        r = _bridge().call("POST", "/api/av/rpa/hangup", {"force": bool(force)}, timeout=20)
    except Exception as e:
        r = {"ok": False, "error": str(e)}
    ms = int((time.time() - t0) * 1000)
    ok = bool(r.get("ok"))
    msg = "挂断了" if ok else ("挂断失败：%s" % (r.get("error") or r.get("msg") or "未知原因"))
    cu.log_action("call", "hangup", {"force": bool(force)}, ok, ms, msg, session=session)
    return {"ok": ok, "msg": msg, "detail": r}


# ---------------------------------------------------------------- 自动回拨


def on_incoming(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """音视频模块推来事件时的钩子：对方没接到的来电，可以自动回拨。

    注意：只认「未接来电」这一类（`kind` 里有 unincoming / missed），
    正常接起来的通话不重拨，否则会变成互相拨打。
    """
    if not _on("agent_call_autocallback", False):
        return None
    kindtxt = str((event or {}).get("kind") or "")
    if not re.search(r"(missed|unanswered|未接)", kindtxt, re.I):
        return None
    peer = str((event or {}).get("peer") or (event or {}).get("name") or "").strip()
    if not peer:
        return None
    _log("[打电话] 有未接来电（%s），按设置回拨" % peer, "WARN")
    return dial("voice", peer, session="autocallback")


TOOLS = [
    {
        "name": "voice_call",
        "when": "你想给对方打个语音电话（只有在对方明确希望你打、或任务要求时）",
        "args": {"name": "对方的备注名或 QQ 号"},
        "fn": lambda a, s="": dial("voice", a.get("name", ""), session=s),
    },
    {
        "name": "video_call",
        "when": "你想给对方打个视频电话",
        "args": {"name": "对方的备注名或 QQ 号"},
        "fn": lambda a, s="": dial("video", a.get("name", ""), session=s),
    },
    {
        "name": "hangup_call",
        "when": "需要挂断当前通话时",
        "args": {},
        "fn": lambda a, s="": hangup(True, session=s),
    },
]


def tool_names() -> List[str]:
    return [t["name"] for t in TOOLS]


def find_tool(name: str) -> Optional[Dict[str, Any]]:
    n = (name or "").strip().lower()
    for t in TOOLS:
        if t["name"] == n:
            return t
    return None
