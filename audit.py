"""签署审计留痕（V1.7.0）

需求：强模态签署弹窗点「同意并继续」后，必须把这条同意**持久化到数据库**，
记录至少包含：用户 ID、条款版本号、同意时间、设备 MAC / Machine ID。

设计要点
--------
* 独立 SQLite 库 `audit/ui_audit.db`，不塞进 config.json
  （config 里只有一堆标量开关，塞进去既不好查也不好统计）。
* 只追加、不修改：每行带 `prev_hash` + `hash`，形成哈希链，
  事后改任何一行都会让后面的链对不上 —— 这就是「留痕」的意义。
* 设备指纹用 `uuid.getnode()`（网卡 MAC，取不到时 Python 会给随机值并置
  multicast 位，这里如实记录 `mac_ok` 标记，不假装拿到了）。
* 所有函数都不抛异常，失败返回 {"ok": False, "msg": ...}，前端据此停在弹窗里重试。
"""
import hashlib
import json
import os
import platform
import sqlite3
import threading
import time
import uuid

try:
    import config as _cfg_mod
    DATA_DIR = _cfg_mod.DATA_DIR
except Exception:  # pragma: no cover - 独立运行时兜底
    import sys
    DATA_DIR = (os.path.dirname(os.path.abspath(sys.executable))
                if getattr(sys, "frozen", False)
                else os.path.dirname(os.path.abspath(__file__)))
    _cfg_mod = None

_LOCK = threading.RLock()
_MACHINE_CACHE = {}


# ---------------------------------------------------------------- 路径
def audit_dir():
    d = os.path.join(DATA_DIR, "audit")
    os.makedirs(d, exist_ok=True)
    return d


def db_path():
    return os.path.join(audit_dir(), "ui_audit.db")


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 设备指纹
def machine_id():
    """稳定的设备指纹：sha256(节点 + 机器名 + 平台)，16 位十六进制。"""
    if _MACHINE_CACHE.get("id"):
        return _MACHINE_CACHE["id"]
    node = uuid.getnode()
    mac_ok = not bool((node >> 40) & 0x01)  # 组播位置 1 说明是随机生成的
    mac = ":".join(("%012X" % node)[i:i + 2] for i in range(0, 12, 2))
    raw = "|".join([
        str(node),
        platform.node() or "",
        platform.machine() or "",
        platform.system() or "",
    ])
    mid = hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:16]
    _MACHINE_CACHE.update({"id": mid, "mac": mac, "mac_ok": mac_ok,
                           "node": str(node)})
    return mid


def device_info():
    machine_id()
    return {
        "machine_id": _MACHINE_CACHE.get("id", ""),
        "mac": _MACHINE_CACHE.get("mac", ""),
        "mac_ok": bool(_MACHINE_CACHE.get("mac_ok")),
        "host": platform.node() or "",
        "platform": (platform.system() or "") + " " + (platform.release() or ""),
    }


def user_id():
    """本地用户标识：优先 config 里的 user_id，否则退到设备指纹。"""
    try:
        if _cfg_mod is not None:
            v = _cfg_mod.get("user_id", "") or ""
            if str(v).strip():
                return str(v).strip()
    except Exception:
        pass
    return "local-" + machine_id()


# ---------------------------------------------------------------- 建表
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ui_sign_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    agreement   TEXT    NOT NULL,
    version     TEXT    NOT NULL,
    title       TEXT,
    user_id     TEXT,
    machine_id  TEXT,
    mac         TEXT,
    accepted_at TEXT,
    signature   TEXT,
    client      TEXT,
    prev_hash   TEXT,
    hash        TEXT,
    algo        TEXT
);
CREATE INDEX IF NOT EXISTS idx_ui_sign_audit_ag ON ui_sign_audit(agreement, id DESC);
CREATE INDEX IF NOT EXISTS idx_ui_sign_audit_ts ON ui_sign_audit(ts);
CREATE TABLE IF NOT EXISTS ui_sign_audit_seal (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    n    INTEGER NOT NULL,
    tail TEXT,
    ts   TEXT
);
"""


def _conn():
    c = sqlite3.connect(db_path(), timeout=10)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    for stmt in _SCHEMA.strip().split(";"):
        s = stmt.strip()
        if s:
            c.execute(s)
    # v1.8.1：老库没有 algo 列，补一个（不动已有数据）
    try:
        cols = [r[1] for r in c.execute(
            "PRAGMA table_info(ui_sign_audit)").fetchall()]
        if "algo" not in cols:
            c.execute("ALTER TABLE ui_sign_audit ADD COLUMN algo TEXT")
    except Exception:
        pass
    c.commit()
    return c


DIGEST_ALGO = "v181"


def _digest_v1(prev_hash, ts, agreement, version, uid, mid, accepted_at):
    """老算法（V1.7.0 ~ V1.8.0）：只算 7 个字段。留着重算老记录用。"""
    raw = "\x1f".join([prev_hash or "", ts or "", agreement or "", version or "",
        uid or "", mid or "", accepted_at or ""])
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def _digest(prev_hash, ts, agreement, version, uid, mid, accepted_at,
        title="", mac="", signature="", client=""):
    """v1.8.1：标题、MAC、签名、客户端指纹全纳入，改动任意一项都能查出来。"""
    raw = "\x1f".join([prev_hash or "", ts or "", agreement or "", version or "",
        uid or "", mid or "", accepted_at or "",
        title or "", mac or "", signature or "", client or ""])
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()



def _seal_update(c):
    """尾部封印：记下当前行数和最后一条的哈希。

    只有行数+尾哈希一起存，才能在「整条链算下来都是对的、但末尾几行被删了」
    的时候报出来。调用方负责 commit。
    """
    row = c.execute("SELECT COUNT(*) AS n, MAX(id) AS m"
                 " FROM ui_sign_audit").fetchone()
    n = int((row["n"] if row else 0) or 0)
    tail = ""
    if n:
        last = c.execute("SELECT hash FROM ui_sign_audit"
               " ORDER BY id DESC LIMIT 1").fetchone()
        tail = (last["hash"] if last else "") or ""
    c.execute("INSERT INTO ui_sign_audit_seal (id, n, tail, ts)"
              " VALUES (1,?,?,?)"
              " ON CONFLICT(id) DO UPDATE SET n=excluded.n,"
              "  tail=excluded.tail, ts=excluded.ts",
        (n, tail, _now()))
    c.commit()



# ---------------------------------------------------------------- 写入
def accept(payload=None):
    """记录一次「同意」。payload 来自前端 UI.showSignDialog。"""
    payload = payload or {}
    agreement = str(payload.get("agreement") or "").strip()
    version = str(payload.get("version") or "").strip()
    if not agreement:
        return {"ok": False, "msg": "缺少协议标识（agreement）"}
    if not version:
        return {"ok": False, "msg": "缺少条款版本号（version）"}

    title = str(payload.get("title") or "")
    signature = str(payload.get("signature") or "")
    accepted_at = str(payload.get("accepted_at") or "")
    if not accepted_at:
        accepted_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    client = payload.get("client") or {}
    try:
        client_s = json.dumps(client, ensure_ascii=False)[:2000]
    except Exception:
        client_s = "{}"

    info = device_info()
    uid = str(payload.get("user_id") or "").strip() or user_id()
    mid = info["machine_id"]

    with _LOCK:
        try:
            c = _conn()
        except Exception as e:
            return {"ok": False, "msg": "审计库打不开：%s" % e}
        try:
            row = c.execute(
                "SELECT hash FROM ui_sign_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev_hash = (row["hash"] if row else "") or ""
            ts = _now()
            h = _digest(prev_hash, ts, agreement, version, uid, mid, accepted_at,
                       title=title, mac=info["mac"],
                       signature=signature, client=client_s)
            cur = c.execute(
                "INSERT INTO ui_sign_audit"
                " (ts, agreement, version, title, user_id, machine_id, mac,"
                "  accepted_at, signature, client, prev_hash, hash, algo)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, agreement, version, title, uid, mid, info["mac"],
                 accepted_at, signature, client_s, prev_hash, h, DIGEST_ALGO))
            c.commit()
            _seal_update(c)
            rid = int(cur.lastrowid or 0)
            return {"ok": True, "id": rid, "at": ts, "hash": h,
                    "agreement": agreement, "version": version,
                    "user_id": uid, "machine_id": mid, "mac": info["mac"]}
        except Exception as e:
            return {"ok": False, "msg": "写审计记录失败：%s" % e}
        finally:
            try:
                c.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 查询
def state(agreement=""):
    """某个协议签没签过、签的是哪个版本。"""
    agreement = str(agreement or "").strip()
    try:
        c = _conn()
    except Exception as e:
        return {"ok": False, "msg": "审计库打不开：%s" % e}
    try:
        if agreement:
            row = c.execute(
                "SELECT id, ts, version, accepted_at, user_id, machine_id"
                " FROM ui_sign_audit WHERE agreement=? ORDER BY id DESC LIMIT 1",
                (agreement,)).fetchone()
        else:
            row = None
        if not row:
            return {"ok": True, "signed": False, "agreement": agreement}
        return {"ok": True, "signed": True, "agreement": agreement,
                "id": row["id"], "version": row["version"], "at": row["ts"],
                "accepted_at": row["accepted_at"],
                "user_id": row["user_id"], "machine_id": row["machine_id"]}
    except Exception as e:
        return {"ok": False, "msg": "查审计记录失败：%s" % e}
    finally:
        try:
            c.close()
        except Exception:
            pass


def recent(limit=50):
    """最近的签署记录，供设置页展示。"""
    try:
        n = max(1, min(int(limit or 50), 500))
    except Exception:
        n = 50
    try:
        c = _conn()
    except Exception as e:
        return {"ok": False, "msg": "审计库打不开：%s" % e, "rows": []}
    try:
        rows = c.execute(
            "SELECT id, ts, agreement, version, title, user_id, machine_id, mac,"
            " accepted_at, signature FROM ui_sign_audit ORDER BY id DESC LIMIT ?",
            (n,)).fetchall()
        return {"ok": True, "rows": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        return {"ok": False, "msg": "查审计记录失败：%s" % e, "rows": []}
    finally:
        try:
            c.close()
        except Exception:
            pass


def verify():
    """校验哈希链是否被改过 + 尾部行数锚点。

    两件事分开查：
     ① 逐行复算：老记录用 v1 算法（只有 7 个字段），新记录用 v181（11 个字段），
        所以升级前的数据不会因为换了算法就集体变「被篡改」。
     ② 尾部锚点：整条链算下来都对、但末尾几行被删掉的情况下，①是查不出来的
        —— 必须靠 ui_sign_audit_seal 里另存的行数 n 和尾哈希 tail 才能发现。
    """
    try:
        c = _conn()
    except Exception as e:
        return {"ok": False, "msg": "审计库打不开：%s" % e}
    try:
        rows = c.execute(
            "SELECT id, ts, agreement, version, title, user_id, machine_id,"
            " mac, accepted_at, signature, client, prev_hash, hash, algo"
            " FROM ui_sign_audit ORDER BY id ASC"
        ).fetchall()
        prev = ""
        bad = []
        for r in rows:
            if (r["algo"] or "") == DIGEST_ALGO:
                want = _digest(prev, r["ts"], r["agreement"], r["version"],
                             r["user_id"], r["machine_id"], r["accepted_at"],
                             title=r["title"] or "", mac=r["mac"] or "",
                             signature=r["signature"] or "", client=r["client"] or "")
            else:
                want = _digest_v1(prev, r["ts"], r["agreement"], r["version"],
                          r["user_id"], r["machine_id"], r["accepted_at"])
            if (r["prev_hash"] or "") != prev or (r["hash"] or "") != want:
                bad.append(r["id"])
            prev = r["hash"] or ""

        # v1.8.1：尾部锚点 —— 防「链本身都对、末尾被删了几行」
        seal = None
        tail_ok = True
        tail_msg = ""
        try:
            seal = c.execute(
                "SELECT n, tail FROM ui_sign_audit_seal WHERE id=1"
            ).fetchone()
        except Exception:
            seal = None
        if seal:
            want_n = int(seal["n"] or 0)
            want_tail = (seal["tail"] or "")
            if want_n != len(rows):
                tail_ok = False
                tail_msg = ("记录条数对不上：应有 %d 条，实际 %d 条"
                          % (want_n, len(rows)))
            elif want_tail != (prev or ""):
                tail_ok = False
                tail_msg = "最后一条的哈希对不上（末尾被改过）"
        else:
            tail_msg = "还没有封印（首次写入后建立）"
        return {
            "ok": True,
            "total": len(rows),
            "broken": bad,
            "intact": (not bad),
            "tail_ok": tail_ok,
            "tail_msg": tail_msg,
            "algo": DIGEST_ALGO,
        }
    except Exception as e:
        return {"ok": False, "msg": "校验失败：%s" % e}
    finally:
        try:
            c.close()
        except Exception:
            pass


if __name__ == "__main__":
    print(json.dumps(device_info(), ensure_ascii=False, indent=2))
    print(json.dumps(accept({"agreement": "_selftest", "version": "0.0.1",
                             "title": "自测"}), ensure_ascii=False))
    print(json.dumps(state("_selftest"), ensure_ascii=False))
    print(json.dumps(verify(), ensure_ascii=False))
