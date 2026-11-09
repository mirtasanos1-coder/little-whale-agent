# -*- coding: utf-8 -*-
"""危险动作的「逐次确认」通道（v1.5.2，QA No-Go 修复四）。

背景：以前 Agent 的危险动作靠界面上一个全局开关 `agentApprove` 授权一次就全放开，
等于「点一次同意，之后干什么都行」。QA 要求改成**每一个危险动作单独确认**：

1. 后端执行到危险动作时**挂起**（阻塞等用户点），不是先做再问；
2. 前端通过轮询 `/api/agent_pending_approval` 拿到待确认事件
   （`{"type": "danger_approval", "action": ..., "args": ...}`）；
3. 用户点【允许执行】→ 放行；点【拒绝并停止】→ 抛 `DangerDenied`，
   整条任务立刻终止（和紧急停止一样的语义）；
4. 没人理它（超时）→ 按「拒绝」处理，绝不偷偷执行。

这个模块只做状态，不 import agent（避免循环依赖）。
"""
import json
import threading
import time

# 等用户点按钮的最长时间（秒）；超时算拒绝
DEFAULT_TIMEOUT = 120.0


class DangerDenied(Exception):
    """用户拒绝 / 超时 —— 上层应当立刻终止这条任务。"""


_LOCK = threading.Lock()
_PENDING = {}          # rid -> {"rid","action","args","session","ts","event","decision"}
_SEQ = [0]


def _new_id(action):
    with _LOCK:
        _SEQ[0] += 1
        return "%d-%s-%d" % (int(time.time()), (action or "act")[:12], _SEQ[0])


def _brief(args):
    try:
        s = json.dumps(args or {}, ensure_ascii=False)
    except Exception:
        s = str(args)
    return s[:600]


def request(action, args=None, session="", timeout=DEFAULT_TIMEOUT):
    """挂起，等界面点【允许执行】/【拒绝并停止】。

    返回 {"ok": True, "msg": "用户已允许"}；
    被拒绝或超时抛 DangerDenied（调用方必须让它终止整个任务）。
    """
    rid = _new_id(action)
    ev = threading.Event()
    item = {"rid": rid, "action": (action or "")[:40], "args": args or {},
            "args_text": _brief(args), "session": session or "",
            "ts": time.time(), "event": ev, "decision": ""}
    with _LOCK:
        _PENDING[rid] = item
    try:
        ev.wait(max(1.0, float(timeout or DEFAULT_TIMEOUT)))
    finally:
        with _LOCK:
            item = _PENDING.pop(rid, item)
    decision = item.get("decision") or ""
    if decision == "allow":
        return {"ok": True, "rid": rid, "msg": "用户已允许执行 %s" % (action or "")}
    if decision == "deny":
        raise DangerDenied("用户拒绝了这个动作（%s），已停止" % (action or ""))
    raise DangerDenied("等确认超时（%.0f 秒没人点），按拒绝处理，已停止" % float(timeout or DEFAULT_TIMEOUT))


def answer(rid, allow):
    """界面点按钮时调：allow=True 放行，False 拒绝并停止。"""
    rid = str(rid or "")
    with _LOCK:
        item = _PENDING.get(rid)
        if not item:
            return {"ok": False, "msg": "这个确认已经过期了（可能自己超时了）"}
        item["decision"] = "allow" if allow else "deny"
        ev = item.get("event")
    try:
        if ev is not None:
            ev.set()
    except Exception:
        pass
    return {"ok": True, "msg": "已允许" if allow else "已拒绝并停止"}


def pending():
    """给界面轮询：当前正在等确认的事件列表（不含 Event 对象）。"""
    now = time.time()
    out = []
    with _LOCK:
        for rid, it in list(_PENDING.items()):
            out.append({"type": "danger_approval", "rid": rid,
                        "action": it.get("action"), "args": it.get("args_text"),
                        "session": it.get("session"),
                        "waited": round(now - float(it.get("ts") or now), 1)})
    return {"ok": True, "items": out, "count": len(out)}


def cancel_all(msg="任务已经结束了"):
    """任务作废/急停时，把还挂着的确认全部按拒绝收掉，别让线程一直吊着。"""
    with _LOCK:
        items = list(_PENDING.items())
    for _rid, it in items:
        it["decision"] = "deny"
        try:
            it["event"].set()
        except Exception:
            pass
    return {"ok": True, "msg": msg, "count": len(items)}
