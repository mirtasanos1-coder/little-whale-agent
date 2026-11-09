"""语义记忆（v1.1.6 核心任务一）

做法（不装新依赖也能跑）：
- 向量存在记忆库自己的表 `vec_memory` 里（BLOB，float32），跟聊天记录同库同事务。
- 嵌入优先用**你本机的模型服务**：LM Studio 的 `POST /v1/embeddings`、
  Ollama 的 `POST /api/embeddings`；都没有就退回关键词打分（`keyword` 模式）。
- 检索用 numpy 做余弦（几千条内存足够快）；装了 `sqlite-vec` 也仍然走 numpy，
  因为 sqlite-vec 的扩展加载在 Windows 打包版里容易失败，宁可不依赖。

为什么不用 onnxruntime + BGE：那要额外下载 100MB 模型、且打包后要带 onnx 运行时；
本机已经有能出对话的模型服务，借它的 embeddings 端点最省事，失败也有兜底。

对外：
    available()            -> 当前走哪种模式（local/ollama/keyword）
    embed(text)            -> list[float] 或 None
    add(kind, content, ...) -> 写一条
    search(query, k, min_score) -> [{content, score, kind, ...}]
    reindex(...)           -> 把已有聊天记录补进向量库
    stats()                -> 给界面看的统计
"""
import json
import math
import os
import re
import sqlite3
import struct
import sys
import threading
import time

_LOCK = threading.Lock()
_CACHE = {"vecs": None, "stamp": 0.0}
_CACHE_TTL = 20.0


def _cfg(key, default=None):
    try:
        import config
        return config.get(key, default)
    except Exception:
        return default


def _base_url():
    """本机模型服务的根地址（LM Studio 或 Ollama）。"""
    base = str(_cfg("lm_base", "") or "").strip()
    if base:
        return base.rstrip("/")
    eng = str(_cfg("lm_engine", "lmstudio") or "lmstudio")
    if eng == "ollama":
        return "http://127.0.0.1:11434"
    return "http://127.0.0.1:1234/v1"


def _model():
    return str(_cfg("lm_model", "") or "").strip()


_EMB = {"name": "", "at": 0.0}
_LOCAL_RUN = {"tried": False, "fn": None}


def _embedding_model():
    """找一个「能干 embedding 的模型」。

    LM Studio 的 /v1/models 会把已加载的 embedding 模型也列出来（名字里带 embed），
    而 lm_model 是聊天模型，直接拿它调 /v1/embeddings 会被拒。
    所以这里优先挑名字带 embed 的那个，并缓存 10 分钟。
    """
    now = time.time()
    if _EMB["name"] and now - float(_EMB["at"] or 0) < 600:
        return _EMB["name"]
    base = _base_url()
    name = ""
    try:
        import requests
        r = requests.get(base + "/models", timeout=6)
        if r.status_code == 200:
            ids = [str(m.get("id") or "") for m in (r.json().get("data") or [])]
            for i in ids:
                if "embed" in i.lower():
                    name = i
                    break
    except Exception:
        name = ""
    _EMB["name"] = name
    _EMB["at"] = now
    return name


def embed(text):
    """把一句话变成向量。失败返回 None（调用方自己退回关键词）。

    v1.1.6 ⑬：如果用户自己放了本地 BGE 模型（models/bge-small-zh），
    优先用它（离线、不占模型服务）；否则走 LM Studio / Ollama 的 embeddings。
    """
    t = str(text or "").strip()
    if not t:
        return None
    t = t[:1200]
    global _LOCAL_RUN
    if _LOCAL_RUN["tried"] is False:
        _LOCAL_RUN["tried"] = True
        try:
            _LOCAL_RUN["fn"] = _local_bge()
        except Exception:
            _LOCAL_RUN["fn"] = None
    if _LOCAL_RUN.get("fn"):
        try:
            v = _LOCAL_RUN["fn"](t)
            if v:
                return v
        except Exception:
            pass
    base = _base_url()
    eng = str(_cfg("lm_engine", "lmstudio") or "lmstudio")
    try:
        import requests
    except Exception:
        return None
    model = _embedding_model()
    # ① LM Studio / OpenAI 兼容
    try:
        payload = {"input": t}
        if model:
            payload["model"] = model
        r = requests.post(base + "/embeddings", json=payload, timeout=12)
        if r.status_code == 200:
            d = r.json()
            data = d.get("data") or []
            if data and data[0].get("embedding"):
                return [float(x) for x in data[0]["embedding"]]
    except Exception:
        pass
    # ①b 没有专门的 embedding 模型时，最后再拿聊天模型试一次（有的服务会自动路由）
    _chat_model = _model()
    if _chat_model and _chat_model != model:
        try:
            r = requests.post(base + "/embeddings",
                              json={"input": t, "model": _chat_model}, timeout=12)
            if r.status_code == 200:
                d = r.json()
                data = d.get("data") or []
                if data and data[0].get("embedding"):
                    return [float(x) for x in data[0]["embedding"]]
        except Exception:
            pass
    # ② Ollama
    try:
        ollama = base if "11434" in base else "http://127.0.0.1:11434"
        payload = {"prompt": t}
        if model:
            payload["model"] = model
        r = requests.post(ollama.rstrip("/") + "/api/embeddings", json=payload, timeout=12)
        if r.status_code == 200:
            d = r.json()
            if d.get("embedding"):
                return [float(x) for x in d["embedding"]]
    except Exception:
        pass
    return None


def _conn():
    import memory
    m = memory.Memory()
    return m.conn


_ACCEL = {"tried": False, "ok": False, "msg": ""}


def accel():
    """v1.1.6 ⑬：可选 sqlite-vec 加速（装了就用 SQL 排序，没装照样能跑）。

    用户点名要 sqlite-vec + onnxruntime + BGE。做法是「能用就用、不能用无声退化」：
      - sqlite-vec 装了 → 向量排序交给 SQL（几万条也快）；
      - 没装 → 纯 Python 余弦，结果完全一样，只是慢一点；
      - onnxruntime / 本地 BGE 模型目录存在时才会被 _local_bge() 用到。
    整个过程不抛异常、不阻塞，失败只记一句原因。
    """
    if _ACCEL["tried"]:
        return _ACCEL["ok"], _ACCEL["msg"]
    _ACCEL["tried"] = True
    try:
        import sqlite_vec
        conn = _conn()
        try:
            conn.enable_load_extension(True)
        except Exception:
            pass
        sqlite_vec.load(conn)
        try:
            conn.enable_load_extension(False)
        except Exception:
            pass
        conn.execute(
            "SELECT vec_distance_cosine(vec_f32('[1.0,0.0]'), vec_f32('[1.0,0.0]'))"
        ).fetchone()
        _ACCEL["ok"] = True
        _ACCEL["msg"] = "sqlite-vec 已启用（向量排序走 SQL）"
    except Exception as e:
        _ACCEL["ok"] = False
        _ACCEL["msg"] = (f"没用上 sqlite-vec，改走纯 Python 余弦"
                         f"（功能一样，数据多了稍慢）：{type(e).__name__}")
    return _ACCEL["ok"], _ACCEL["msg"]


_BGE_CACHE = {"sess": None, "tok": None, "path": "", "err": ""}


def _bge_files(base):
    """在目录里找出 model.onnx 与 tokenizer.json（兼容 onnx/ 子目录和 model_quantized 命名）。"""
    cands = [
        os.path.join(base, "model.onnx"),
        os.path.join(base, "onnx", "model.onnx"),
        os.path.join(base, "onnx", "model_quantized.onnx"),
        os.path.join(base, "model_quantized.onnx"),
    ]
    onnx_path = next((p for p in cands if os.path.isfile(p)), "")
    tok_path = os.path.join(base, "tokenizer.json")
    return onnx_path, (tok_path if os.path.isfile(tok_path) else "")


def _bge_candidates():
    """本地 BGE 模型可能出现的所有位置（V1.0.0-Beta 打包后就不止一个了）。

    顺序 = 用户显式配置 → 安装目录/resources/models → 安装目录/models
          → PyInstaller 解包目录(_MEIPASS)/models → 仓库 models（源码运行）
    打包版把模型塞进了 exe 里，运行时在 `_MEIPASS\\models\\bge-small-zh`；
    绿色版也可能被人手动放在 `resources\\models` 旁边。
    """
    out = []
    p = str(_cfg("embed_model_dir", "") or "").strip()
    if p:
        out.append(p)
    name = "bge-small-zh"
    roots = []
    try:
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            roots += [os.path.join(exe_dir, "resources", "models"),
                      os.path.join(exe_dir, "models")]
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(os.path.join(meipass, "models"))
    except Exception:
        pass
    try:
        roots.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"))
    except Exception:
        pass
    for r in roots:
        out.append(os.path.join(r, name))
    # 去重但保序
    seen, uniq = set(), []
    for d in out:
        k = os.path.normcase(d)
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


def _bge_dir():
    """本地 BGE 模型目录（按 _bge_candidates 的顺序找第一个真的存在模型文件的）。"""
    try:
        for d in _bge_candidates():
            if any(_bge_files(d)):
                return d
    except Exception:
        pass
    c = _bge_candidates()
    return c[0] if c else ""


_WARN_ONCE = {"msg": ""}


def _warn_once(reason: str, level: str = "warning") -> None:
    """本地向量模型不可用时只提示一次，绝不重复刷日志、绝不抛异常。"""
    reason = str(reason or "")
    if not reason or _WARN_ONCE["msg"] == reason:
        return
    _WARN_ONCE["msg"] = reason
    line = f"[语义记忆] 本地向量模型没启用，改用关键词+重要度+时间衰减检索：{reason}"
    try:
        import logging
        logging.getLogger("xiaojingyu.semantic").warning(line)
    except Exception:
        pass
    try:
        print(line, file=sys.stderr)
    except Exception:
        pass


def _local_bge():
    """v1.1.6 ⑬：本地 BGE 向量化（onnxruntime + tokenizers）。

    目录结构（下载脚本 _dl_bge.py 会放好）：
        models/bge-small-zh/model.onnx
        models/bge-small-zh/tokenizer.json
    任何一步失败都返回 None 并记下原因，调用方继续用服务端 embeddings；
    服务端也没有就退关键词 —— 全程不抛异常、不影响聊天。
    """
    backend = str(_cfg("embed_backend", "auto") or "auto").strip().lower()
    if backend not in ("auto", "onnx"):
        _BGE_CACHE["err"] = "embed_backend=%s（没走本地 ONNX）" % backend
        if backend not in ("keyword", "off", "none"):
            _warn_once(_BGE_CACHE["err"])
        return None
    try:
        if not any(_bge_files(_bge_dir())):
            _BGE_CACHE["err"] = "没有本地模型（%s）" % _bge_dir()
            _warn_once(_BGE_CACHE["err"])
            return None
    except Exception:
        _BGE_CACHE["err"] = "模型目录不可读（%s）" % _bge_dir()
        _warn_once(_BGE_CACHE["err"])
        return None
    if _BGE_CACHE.get("sess") is not None:
        return _BGE_CACHE["sess"]
    try:
        import json as _json

        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        base = _bge_dir()
        onnx_path, tok_path = _bge_files(base)
        if not (onnx_path and tok_path):
            _BGE_CACHE["err"] = "缺文件（model.onnx / tokenizer.json）"
            return None
        tok = Tokenizer.from_file(tok_path)
        try:
            tok.enable_truncation(max_length=512)
            tok.enable_padding(pad_id=0, pad_token="[PAD]")
        except Exception:
            pass
        sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        in_names = [i.name for i in sess.get_inputs()]
        dim = 512
        try:
            cfg_path = os.path.join(base, "config.json")
            if os.path.exists(cfg_path):
                dim = int(_json.load(open(cfg_path, "r", encoding="utf-8")).get("hidden_size") or 512)
        except Exception:
            pass

        def _run(text):
            enc = tok.encode(str(text)[:2000])
            ids = np.array([enc.ids], dtype=np.int64)
            mask = np.array([enc.attention_mask], dtype=np.int64)
            feeds = {}
            for nm in in_names:
                if nm == "input_ids":
                    feeds[nm] = ids
                elif nm == "attention_mask":
                    feeds[nm] = mask
                elif nm == "token_type_ids":
                    feeds[nm] = np.zeros_like(ids)
            out = sess.run(None, feeds)[0]
            # mean pooling（带 attention mask）
            m3 = mask[..., None].astype("float32")
            v = (out * m3).sum(axis=1) / np.clip(m3.sum(axis=1), 1e-9, None)
            v = v[0].astype("float32")
            n = float(np.linalg.norm(v)) or 1.0
            return [float(x) for x in (v / n)]

        _BGE_CACHE.update({"sess": _run, "tok": tok, "path": base, "err": ""})
        return _run
    except Exception as e:
        _BGE_CACHE["err"] = "%s: %s" % (type(e).__name__, str(e)[:80])
        return None



def _pack(vec):
    return struct.pack("<%df" % len(vec), *[float(x) for x in vec])


def _unpack(blob):
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack("<%df" % n, blob[:n * 4]))


def _cos(a, b):
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = na = nb = 0.0
    for i in range(n):
        x = a[i]
        y = b[i]
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _words(text):
    """中文按「字 bigram」切，英文/数字按整词；这样「我妈怎么样」能撞上「我妈住院了」。"""
    t = str(text or "").lower()
    grams = set()
    han = re.sub(r"[^\u4e00-\u9fa5]", "", t)
    for i in range(len(han) - 1):
        grams.add(han[i:i + 2])
    if len(han) == 1:
        grams.add(han)
    for w in re.findall(r"[a-z0-9]{2,}", t):
        grams.add(w)
    return grams


def _kw_score(q, content):
    """Dice 系数：2*交集/(lenA+lenB)，0~1。"""
    a, b = _words(q), _words(content)
    if not a or not b:
        return 0.0
    return (2.0 * len(a & b)) / (len(a) + len(b))


# v1.9.21（QA P2-1）：1.9.16 只把 messages / sessions / diary 换成了「用户原话」，
# 漏了语义库这一处 —— 连发合并时 raw_text 是
# 「用户连续发送了多条消息，请综合这些内容统一回复：[消息1：在吗]…（提示：…）」
# 这段**只该给模型看一次**的内部包装，却被当原话写进了 vec_memory，
# 之后每次检索到都回喂给模型（占 token，还可能被复读）。
# 写入端已经在 agent.process_message 里修好，这里补一把一次性清扫，删掉历史遗留的行。
_WRAP_MARK = "用户连续发送了多条消息"
_WRAP_MSG = "[消息1："
_PURGE = {"done": False}


def purge_wrapped() -> int:
    """删掉历史上被误存的「连发合并包装文案」行，返回删掉的条数。"""
    try:
        conn = _conn()
        with conn:
            c = conn.cursor()
            c.execute("DELETE FROM vec_memory WHERE content LIKE ? AND content LIKE ?",
                      ("%" + _WRAP_MARK + "%", "%" + _WRAP_MSG + "%"))
            n = int(c.rowcount or 0)
        if n:
            _CACHE["stamp"] = 0.0
            try:
                print(f"[语义记忆] 已清理 {n} 条历史遗留的「连发合并文案」", file=sys.stderr)
            except Exception:
                pass
        return n
    except Exception:
        return 0


def _purge_once() -> int:
    """整个进程只扫一次（第一次写入或检索时），避免每次 add 都做全表 LIKE。"""
    if _PURGE["done"]:
        return 0
    _PURGE["done"] = True
    return purge_wrapped()


def add(kind, content, session_key="", user_id="", meta=""):
    """写一条语义记忆。写不进去（比如库锁了）也不抛，返回 0。"""
    text = str(content or "").strip()
    if not text:
        return 0
    _purge_once()
    vec = embed(text) if bool(_cfg("semantic_enabled", True)) else None
    try:
        conn = _conn()
        with conn:
            c = conn.cursor()
            c.execute("""INSERT INTO vec_memory(kind, session_key, user_id, content, vec, dim, created_at, meta)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (str(kind or "chat")[:32], str(session_key or "")[:120],
                       str(user_id or "")[:32], text[:2000],
                       _pack(vec) if vec else None, len(vec) if vec else 0,
                       time.time(), str(meta or "")[:200]))
            vid = int(c.lastrowid or 0)
        _CACHE["stamp"] = 0.0
        return vid
    except Exception:
        return 0


def _load_all():
    _purge_once()
    with _LOCK:
        if _CACHE["vecs"] is not None and (time.time() - _CACHE["stamp"]) < _CACHE_TTL:
            return _CACHE["vecs"]
        rows = []
        try:
            conn = _conn()
            c = conn.cursor()
            c.execute("SELECT id, kind, session_key, user_id, content, vec, created_at FROM vec_memory "
                      "ORDER BY id DESC LIMIT 20000")
            for r in c.fetchall():
                rows.append({"id": r[0], "kind": r[1], "session_key": r[2], "user_id": r[3],
                             "content": r[4] or "", "vec": _unpack(r[5]), "time": float(r[6] or 0)})
        except Exception:
            rows = []
        _CACHE["vecs"] = rows
        _CACHE["stamp"] = time.time()
        return rows


def search(query, k=5, min_score=0.3):
    """语义召回。没有向量时自动退回关键词，score 含义仍然是 0~1 的相关度。"""
    q = str(query or "").strip()
    if not q:
        return []
    k = max(1, min(50, int(k or 5)))
    try:
        mins = float(min_score)
    except Exception:
        mins = 0.3
    rows = _load_all()
    if not rows:
        return []
    qv = embed(q)
    out = []
    # 向量模式用配置门槛；关键词模式（Dice）天然分低，按 0.4 折算，否则永远召回不到
    thr = mins if qv else max(0.09, mins * 0.3)
    # v1.1.6 ⑬：sqlite-vec 可用时让 SQL 直接算余弦并排序（大库快很多）；
    # 任何异常都静默退回下面的 Python 循环，结果一致。
    if qv:
        try:
            ok_acc, _msg = accel()
            if ok_acc:
                conn = _conn()
                c = conn.cursor()
                c.execute("SELECT id, kind, session_key, user_id, content, created_at,"
                          " vec_distance_cosine(vec, vec_f32(?)) AS d"
                          " FROM vec_memory WHERE vec IS NOT NULL AND length(vec) = ?"
                          " ORDER BY d ASC LIMIT ?",
                          (_pack(qv), max(4, 4 * len(qv)), max(k * 6, 30)))
                out = []
                for rid, kind, sk, uid, content, ts, d in c.fetchall():
                    s = 1.0 - float(d or 0.0)
                    if s >= thr:
                        out.append({"content": content or "", "score": round(s, 4),
                                    "kind": kind, "session_key": sk, "user_id": uid,
                                    "time": float(ts or 0), "id": int(rid or 0)})
                if out:
                    out.sort(key=lambda x: -x["score"])
                    return out[:k]
        except Exception:
            pass
    for r in rows:
        if qv and r["vec"] and len(r["vec"]) == len(qv):
            s = _cos(qv, r["vec"])
        elif qv and r["vec"]:
            # 维度不一致（比如换了 embedding 模型）：老向量不能和新向量比，先按关键词算，
            # 用户点「重建索引」后会被重新向量化。
            s = _kw_score(q, r["content"])
        else:
            s = _kw_score(q, r["content"])   # 关键词模式：Dice 系数本身就是 0~1
        if s >= thr:
            out.append({"content": r["content"], "score": round(s, 4),
                        "kind": r["kind"], "session_key": r["session_key"],
                        "user_id": r["user_id"], "time": r["time"], "id": r["id"]})
    out.sort(key=lambda x: -x["score"])
    return out[:k]


def stats():
    try:
        conn = _conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*), SUM(CASE WHEN vec IS NOT NULL THEN 1 ELSE 0 END) FROM vec_memory")
        total, withvec = c.fetchone()
        c.execute("SELECT dim FROM vec_memory WHERE dim > 0 ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        ok_acc, acc_msg = accel()
        return {"ok": True, "total": int(total or 0), "with_vec": int(withvec or 0),
                "dim": int(row[0]) if row else 0,
                "mode": available(), "enabled": bool(_cfg("semantic_enabled", True)),
                "accel": "sqlite-vec" if ok_acc else "python",
                "accel_msg": acc_msg,
                "local_bge": bool(_LOCAL_RUN.get("fn")),
                "top_k": int(_cfg("semantic_top_k", 4) or 4),
                "min_score": float(_cfg("semantic_min_score", 0.35) or 0.35)}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:160], "total": 0, "with_vec": 0}


def available():
    """不真的联网，只看配置与地址：能连上 embeddings 就 local/ollama，否则 keyword。"""
    qv = None
    try:
        qv = embed("测试")
    except Exception:
        qv = None
    if qv:
        eng = str(_cfg("lm_engine", "lmstudio") or "lmstudio")
        return "ollama" if eng == "ollama" else "local"
    return "keyword"


def reindex(limit=800):
    """把最近的聊天记录补进向量库（只补还没索引过的）。返回补了几条。"""
    try:
        import memory
        m = memory.Memory()
        sess = []
        if hasattr(m, "list_sessions"):
            sess = [s.get("key") for s in (m.list_sessions(40) or []) if s.get("key")]
        rows = []
        for sk in (sess or [""]):
            for r in (m.recent_rows(sk, limit=int(limit or 800)) or []):
                rows.append(r)
    except Exception:
        return 0
    if not rows:
        return 0
    seen = set()
    for r in _load_all():
        if r["kind"] == "chat":
            seen.add((r["session_key"], r["content"][:160]))
    n = 0
    for r in rows:
        try:
            sk, role, content = r.get("session_key", ""), r.get("role", ""), r.get("content", "")
        except Exception:
            continue
        if not content or role != "user":
            continue
        key = (sk, str(content)[:160])
        if key in seen:
            continue
        if add("chat", content, session_key=sk):
            seen.add(key)
            n += 1
            if n >= int(limit or 800):
                break
    stats()
    return n


def rebuild_vectors(limit=5000):
    """把维度不对（换了 embedding 模型）或缺向量的行重新算一遍。返回更新条数。

    只动 vec/dim 两列，不删任何行；embed 不可用（没模型也没服务）时直接返回 0，
    原向量保持不动 —— 不会把已有记忆搞坏。
    """
    probe = embed("向量重建探针")
    if not probe:
        return 0
    dim = len(probe)
    n = 0
    try:
        conn = _conn()
        c = conn.cursor()
        c.execute("SELECT id, content, dim, length(vec) FROM vec_memory ORDER BY id DESC LIMIT ?",
                  (int(limit or 5000),))
        rows = c.fetchall()
    except Exception:
        return 0
    for rid, content, d, blen in rows:
        if not content:
            continue
        if int(d or 0) == dim and (blen or 0) == 4 * dim:
            continue
        v = embed(content)
        if not v:
            continue
        try:
            with conn:
                conn.execute("UPDATE vec_memory SET vec = ?, dim = ? WHERE id = ?",
                             (_pack(v), len(v), int(rid)))
            n += 1
        except Exception:
            continue
    _CACHE["stamp"] = 0.0
    return n


def remember_fact(user_id, content, kind="perm"):
    """永久记忆写进语义库（长期记忆的检索增强）。"""
    return add(kind, content, user_id=str(user_id or ""))


def context_block(query, k=None, min_score=None):
    """给 prompt 用的召回块。没有命中就返回空串（不占 token）。"""
    if not bool(_cfg("semantic_enabled", True)):
        return ""
    k = int(_cfg("semantic_top_k", 4) if k is None else k)
    ms = float(_cfg("semantic_min_score", 0.35) if min_score is None else min_score)
    hits = search(query, k=k, min_score=ms)
    if not hits:
        return ""
    lines = []
    for h in hits:
        lines.append(f"- {h['content'][:160]}")
    return "\n\n【语义记忆（跟这句话最相关的旧记忆）】\n" + "\n".join(lines)
