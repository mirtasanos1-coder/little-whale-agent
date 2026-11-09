# -*- coding: utf-8 -*-
"""在聊天里跟用户要授权（而不是弹控制台窗口）。

场景：Agent 想干一件危险的事（动键鼠、跑代码、打电话、改设置），但它人在 QQ 里，
用户可能压根没开控制台。于是让它先发一句话问用户，用户回「同意」就放行。

数据落在 <DATA_DIR>/approvals/approvals.json（很小，够用；不需要 SQLite）。

对外：
    request(kind, detail, session="", ttl=900)  发起一次请求 → {ok,id,prompt}
    pending(session="")                          还有哪些在等
    resolve(id, text)                            按用户回的话决定通过/拒绝
    match_reply(text, session="")                把用户一句闲聊对上唯一一条待批
    clear()                                      清空（关 Agent 时用）
    ask_prompt(req)                              拼成要发给用户的那句话
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List

_LOCK = threading.RLock()
_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}

YES_PAT = re.compile(r"(同意|批准|允许|可以|行|好的|好|ok|okay|yes|开吧|开|来吧|嗯|准了|去做)", re.I)
NO_PAT = re.compile(r"(不同意|不行|不要|不用|不许|拒绝|取消|算了|别|不了|no|停)", re.I)

MAX_KEEP = 40


def _path() -> str:
    try:
        import config as _cfg
        base = getattr(_cfg, "DATA_DIR", "") or os.getcwd()
    except Exception:
        base = os.getcwd()
    d = os.path.join(base, "approvals")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "approvals.json")


def _load() -> List[Dict[str, Any]]:
    now = time.time()
    if _CACHE.get("data") is not None and now - float(_CACHE.get("ts") or 0) < 1.0:
        return [dict(x) for x in _CACHE["data"]]
    p = _path()
    try:
        data = json.loads(io.open(p, encoding="utf-8").read() or "[]")
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []
    _CACHE["data"] = data
    _CACHE["ts"] = now
    return [dict(x) for x in data]


def _save(items: List[Dict[str, Any]]) -> None:
    items = items[-MAX_KEEP:]
    try:
        io.open(_path(), "w", encoding="utf-8", newline="\n").write(
            json.dumps(items, ensure_ascii=False, indent=1))
    except Exception:
        pass
    _CACHE["data"] = items
    _CACHE["ts"] = time.time()


def _alive(it: Dict[str, Any]) -> bool:
    return it.get("status") == "pending" and float(it.get("expire") or 0) > time.time()


def ask_prompt(req: Dict[str, Any]) -> str:
    kind = req.get("kind") or "操作"
    detail = str(req.get("detail") or "").strip()
    return ("我想做一件事：%s。\n%s\n\n你要是同意，就回我一句「同意」；不同意就回「不用」。\n"
            "（%d 分钟内有效）" % (kind, detail, int((float(req.get("expire") or 0) - time.time()) // 60))).strip()


def request(kind: str, detail: str = "", session: str = "", ttl: float = 900.0) -> Dict[str, Any]:
    items = _load()
    # 同一个 session 里已经有待批的，就不再叠加（避免刷屏）
    for it in items:
        if _alive(it) and it.get("session") == session and it.get("kind") == kind:
            return {"ok": True, "id": it["id"], "prompt": ask_prompt(it), "reused": True}
    rid = uuid.uuid4().hex[:10]
    now = time.time()
    it = {"id": rid, "ts": now, "expire": now + float(ttl or 900), "session": session,
          "kind": kind, "detail": str(detail or ""), "status": "pending"}
    items.append(it)
    _save(items)
    return {"ok": True, "id": rid, "prompt": ask_prompt(it), "reused": False}


def pending(session: str = "") -> List[Dict[str, Any]]:
    return [it for it in _load() if _alive(it) and (not session or it.get("session") == session)]


def _decide(text: str) -> str:
    t = str(text or "").strip()
    if not t:
        return "none"
    if NO_PAT.search(t):
        return "no"
    if YES_PAT.search(t):
        return "yes"
    return "none"


def resolve(rid: str, text: str) -> Dict[str, Any]:
    items = _load()
    verdict = _decide(text)
    for it in items:
        if it.get("id") != rid:
            continue
        if not _alive(it):
            return {"ok": False, "msg": "这条请求已经失效了"}
        if verdict == "yes":
            it["status"] = "approved"
            it["resolved"] = time.time()
            it["answer"] = str(text or "")[:60]
            _save(items)
            return {"ok": True, "approved": True, "kind": it.get("kind"), "detail": it.get("detail"), "msg": "用户同意了"}
        if verdict == "no":
            it["status"] = "denied"
            it["resolved"] = time.time()
            it["answer"] = str(text or "")[:60]
            _save(items)
            return {"ok": True, "approved": False, "msg": "用户拒绝了"}
        return {"ok": False, "msg": "没看懂这句是不是同意，再问一次"}
    return {"ok": False, "msg": "找不到这条请求"}


def match_reply(text: str, session: str = "") -> Dict[str, Any]:
    """用户随口回一句，若只有一条待批且判得出意图，就把它结掉。"""
    p = pending(session)
    if not p:
        return {"ok": False, "msg": "没有等你确认的事"}
    verdict = _decide(text)
    if verdict == "none":
        return {"ok": False, "msg": "这句不像答复"}
    if len(p) > 1:
        # 多条时只认最新那条，避免歧义
        p = [max(p, key=lambda x: float(x.get("ts") or 0))]
    return resolve(p[0]["id"], text)


def clear() -> Dict[str, Any]:
    n = len([it for it in _load() if _alive(it)])
    _save([it for it in _load() if not _alive(it)])
    return {"ok": True, "msg": "清掉 %d 条待授权" % n}


def recent(limit: int = 20) -> List[Dict[str, Any]]:
    items = _load()
    items.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
    return items[:max(1, int(limit or 20))]
