# -*- coding: utf-8 -*-
"""快路由「记录模式」（v1.4.0）—— 说得多的那句话，第二次就直接回，不再花 token。

背景
----
`agent.fast_route()` 原来只有几条死规则（嗯/哦/哈哈、几点了、机器负载），
稍微换个说法就漏，于是每句话都得整包 prompt 走一遍模型 —— 又慢又费钱。

这里补一层「学出来的快路由」：
  * 每次模型正常回复完，把「你那句话 → 我回的那句」记下来（只记短的，默认 ≤30 字）；
  * 同一句话（归一化后：去空白/标点、全角转半角、小写）出现到 `route_memo_min` 次，
    就标记为已学 [learned=1]，下次进来在**第一遍**就命中，不调模型；
  * 回复里凡是带「今天/现在/刚才」这类时间词的，不缓存（明天说同样的话不能回旧日期）；
  * 随时可以在界面上看命中次数、清空。

存储
----
`<数据目录>/route_memo.db`，和主库分开：这里全是可丢的缓存，坏了删掉就行。
"""
import hashlib
import os
import re
import sqlite3
import sys
import threading
import time

_lock = threading.Lock()
_conn = None
_conn_path = ""

# 回复里出现这些词就不缓存（回答跟时间绑定，下次照搬会露馅）
_NO_CACHE_PAT = re.compile(
    r"(今天|明天|昨天|现在|刚刚|刚才|今晚|明早|这周|上周|下周|"
    r"\d{1,2}\s*[:：]\s*\d{2}|\d{4}\s*[-/年]\s*\d{1,2})"
)

_PUNCT = re.compile(r"[\s，。！？、,.!?~～…—\-_:：;；\"'“”‘’()（）\[\]【】]+")


def _data_dir():
    env_dir = os.environ.get("QQ_AGENT_DATA_DIR")
    if env_dir:
        try:
            os.makedirs(env_dir, exist_ok=True)
            return env_dir
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _db_path():
    return os.path.join(_data_dir(), "route_memo.db")


def _conn_get():
    global _conn, _conn_path
    path = _db_path()
    if _conn is not None and _conn_path == path:
        return _conn
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    c = sqlite3.connect(path, timeout=5, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS route_memo (
        key TEXT PRIMARY KEY,
        text TEXT,
        norm TEXT,
        reply TEXT,
        count INTEGER DEFAULT 0,
        hits INTEGER DEFAULT 0,
        learned INTEGER DEFAULT 0,
        last_ts REAL DEFAULT 0
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_route_norm ON route_memo(norm)")
    c.commit()
    _conn, _conn_path = c, path
    return c


def norm(text):
    """归一化：全角转半角、去标点空白、小写。空串返回空串。"""
    t = str(text or "")
    if not t:
        return ""
    out = []
    for ch in t:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    t = "".join(out).lower()
    return _PUNCT.sub("", t)


def _key(text):
    n = norm(text)
    return hashlib.md5(n.encode("utf-8", "ignore")).hexdigest() if n else ""


def _cfg():
    try:
        import config as cfg_mod
        return cfg_mod.load()
    except Exception:
        return {}


def enabled():
    try:
        return bool(_cfg().get("route_memo_enabled", True))
    except Exception:
        return True


def _max_len():
    try:
        return max(4, min(120, int(_cfg().get("route_memo_max_len", 30) or 30)))
    except Exception:
        return 30


def _min_count():
    try:
        return max(1, min(10, int(_cfg().get("route_memo_min", 2) or 2)))
    except Exception:
        return 2


def forget_all():
    """清空（界面上「清空记录」用）。"""
    try:
        with _lock:
            c = _conn_get()
            c.execute("DELETE FROM route_memo")
            c.commit()
        return True
    except Exception:
        return False


def remember(text, reply):
    """记一次「这句话 → 这个回复」。返回累计次数；不该记就返回 0。"""
    try:
        if not enabled():
            return 0
        raw = str(text or "").strip()
        rep = str(reply or "").strip()
        if not raw or not rep:
            return 0
        if len(raw) > _max_len() or len(rep) > 200:
            return 0
        if _NO_CACHE_PAT.search(rep) or _NO_CACHE_PAT.search(raw):
            return 0
        k = _key(raw)
        if not k:
            return 0
        with _lock:
            c = _conn_get()
            now = time.time()
            row = c.execute("SELECT count FROM route_memo WHERE key=?", (k,)).fetchone()
            if row:
                n = int(row[0] or 0) + 1
                c.execute("UPDATE route_memo SET text=?, reply=?, count=?, last_ts=?, "
                          "learned=CASE WHEN ?>=? THEN 1 ELSE learned END WHERE key=?",
                          (raw, rep, n, now, n, _min_count(), k))
            else:
                n = 1
                c.execute("INSERT INTO route_memo(key,text,norm,reply,count,hits,learned,last_ts) "
                          "VALUES(?,?,?,?,?,0,?,?)",
                          (k, raw, norm(raw), rep, n, 1 if n >= _min_count() else 0, now))
            c.commit()
        return n
    except Exception:
        return 0


def lookup(text):
    """这句话学到手了吗？返回要直接发出去的回复；没学到返回空串。"""
    try:
        if not enabled():
            return ""
        raw = str(text or "").strip()
        if not raw or len(raw) > _max_len():
            return ""
        k = _key(raw)
        if not k:
            return ""
        with _lock:
            c = _conn_get()
            row = c.execute("SELECT reply, learned FROM route_memo WHERE key=?", (k,)).fetchone()
            if not row or not int(row[1] or 0):
                return ""
            rep = str(row[0] or "")
            if not rep:
                return ""
            c.execute("UPDATE route_memo SET hits=hits+1, last_ts=? WHERE key=?",
                      (time.time(), k))
            c.commit()
        return rep
    except Exception:
        return ""


def stats(limit=30):
    """给界面看：一共学了多少条、命中最多的几条。"""
    try:
        with _lock:
            c = _conn_get()
            total, learned, hits = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(learned),0), COALESCE(SUM(hits),0) "
                "FROM route_memo").fetchone()
            rows = c.execute(
                "SELECT text, reply, count, hits, learned FROM route_memo "
                "ORDER BY learned DESC, hits DESC, count DESC LIMIT ?",
                (int(limit),)).fetchall()
        items = [{"text": r[0], "reply": r[1], "count": int(r[2] or 0),
                  "hits": int(r[3] or 0), "learned": bool(r[4])} for r in rows]
        return {"ok": True, "enabled": enabled(), "total": int(total or 0),
                "learned": int(learned or 0), "hits": int(hits or 0),
                "min": _min_count(), "max_len": _max_len(), "items": items}
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e), "items": []}
