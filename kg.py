# -*- coding: utf-8 -*-
"""轻量级知识图谱记忆（SQLite + NetworkX）。

为什么要有这个模块
------------------
向量记忆（semantic.py）回答的是"哪几句话和这次的话像"，
永久记忆（perm_memory）回答的是"这个人有哪些事实"。
但真人聊天靠的是**关系**："老板的猫"这件事，藏在
"老板""橘子""猫"三个点之间的关系里，靠一句话相似度是查不出来的。

设计取舍（v1.8.0）
------------------
1.图谱落在**同一个** memory.db里（kg_entities / kg_relations两张表），
不另开库 ——这样备份、时光胶囊、整库迁移都是现成的一整套流程。
2.建表用 PRAGMA user_version做版本闸门：老库首次打开时只加表、不改任何老数据。
3. NetworkX只在**查询时**把边加载进内存，带版本号缓存；写操作 bump版本号让缓存失效。
4.没装 networkx时自动降级成自己写的邻接表 BFS，功能不丢，只是少一点算法。
依赖一个可选库就把记忆功能搞崩，是不能接受的。
5.任何一步失败都返回空结果，绝不抛异常到聊天主流程 ——图谱是增强，不是命脉。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time

#图谱表结构版本（配合 memory.db的 PRAGMA user_version）
KG_SCHEMA_VERSION = 1

_LOCK = threading.RLock()
_CACHE = {"key": "", "graph": None, "backend": "empty", "at":0.0}
_CACHE_TTL = 30.0
#写操作计数器：任何写都 +1，缓存 key一变就重载
_STAMP = {"n":0.0}

#单账号最多加载进内存的边数（防止某人的图大到把内存吃掉）
MAX_EDGES = 4000
#一次多跳检索最多走几跳
MAX_HOPS = 3
MAX_BLOCK_LINES = 14


# ============================================================建表

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS kg_entities(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL,
        norm TEXT NOT NULL,
        etype TEXT DEFAULT '',
        mentions INTEGER DEFAULT 1,
        created_at REAL,
        updated_at REAL)""",
    """CREATE TABLE IF NOT EXISTS kg_relations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL DEFAULT '',
        subject TEXT NOT NULL,
        predicate TEXT NOT NULL,
        object TEXT NOT NULL,
        s_norm TEXT NOT NULL DEFAULT '',
        p_norm TEXT NOT NULL DEFAULT '',
        o_norm TEXT NOT NULL DEFAULT '',
        confidence REAL DEFAULT 1.0,
        source TEXT DEFAULT '',
        perm_id INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        created_at REAL,
        updated_at REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_kge_user ON kg_entities(user_id, norm)",
    "CREATE INDEX IF NOT EXISTS idx_kgr_user ON kg_relations(user_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_kgr_s ON kg_relations(user_id, s_norm)",
    "CREATE INDEX IF NOT EXISTS idx_kgr_o ON kg_relations(user_id, o_norm)",
]


def ensure_schema(conn):
    """确保两张表存在。返回是否新建了东西。

    用 PRAGMA user_version做闸门：已是当前版本的库直接返回，
    老库（user_version=0）走一遍 CREATE IF NOT EXISTS后写回版本号。
    注意：只加表，绝不改动任何老表、老数据。
    """
    if conn is None:
        return False
    try:
        cur = conn.cursor()
        ver = int(cur.execute("PRAGMA user_version").fetchone()[0] or 0)
        if ver >= KG_SCHEMA_VERSION:
            #版本够新也补一遍 IF NOT EXISTS（用户可能手删了表）
            for sql in _SCHEMA:
                try:
                    cur.execute(sql)
                except sqlite3.Error:
                    pass
            conn.commit()
            return False
        for sql in _SCHEMA:
            try:
                cur.execute(sql)
            except sqlite3.Error:
                pass
        try:
            cur.execute("PRAGMA user_version = %d" % KG_SCHEMA_VERSION)
        except sqlite3.Error:
            pass
        conn.commit()
        return True
    except Exception:
        return False


def _conn(mem=None):
    """拿一个连接。优先用传进来的 Memory实例的（它自带 RLock和 WAL）。"""
    if mem is not None and getattr(mem, "conn", None) is not None:
        ensure_schema(mem.conn)
        return mem.conn, False
    import memory as memmod
    m = memmod.Memory()
    return m.conn, True


def _bump():
    with _LOCK:
        _STAMP["n"] = time.time()
        _CACHE["at"] =0.0


# ============================================================归一化

#实体名里这些字眼几乎不承载区分度（"我"和"自己"不该是两个点）
_STOP_ENTITY = "的了是在有和与及也都就还又很非常这那么吗呢吧啊呀哦"


def norm_entity(name):
    """实体名归一化：去空白标点、去停用字、小写、限长。

    "老板的猫" "老板的猫。" "老板的猫"必须落到同一个点上，
    否则图谱会碎成一地孤岛。
    """
    s = str(name or "").strip()
    if not s:
        return ""
    s = s.strip("「」『』\"'“”‘’()（）[]【】<>《》,，.。;；:：!！?？、 \t\r\n")
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
    s = "".join(ch for ch in s if ch not in _STOP_ENTITY)
    return s.lower()[:40]


def norm_predicate(p):
    """谓词归一化。谓词保留原字（要给用户看），这里只做去空白与限长。"""
    s = str(p or "").strip().strip("「」『』\"'“”‘’()（）[]【】")
    s = re.sub(r"\s+", "", s)
    return s[:24]


def _bigrams(s):
    if not s:
        return set()
    if len(s) ==1:
        return {s}
    return {s[i:i +2] for i in range(len(s) -1)}


# ============================================================三元组解析

_TRIPLE_KEYS = ("triples", "三联", "三元组")


def parse_triples(raw):
    """从模型输出里抠出三元组，容忍各种畸形写法。

    支持：
            [{"subject":"我","predicate":"养了","object":"橘子"}]
            [["我","养了","橘子"]]
            {"triples":[...]}
            每行 "我 |养了 |橘子"
    非法条目直接丢掉，绝不抛异常（模型输出是不可信的）。
    """
    out = []
    if raw is None:
        return out
    if isinstance(raw, dict):
        for k in _TRIPLE_KEYS:
            if k in raw:
                return parse_triples(raw.get(k))
        return out
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return out
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
        data = None
        try:
            data = json.loads(s)
        except Exception:
            data = None
        if data is not None:
            return parse_triples(data)
        #退化路径：一行一条 "a | b | c"
        for line in s.splitlines():
            parts = [x.strip() for x in re.split(r"[|\t]+", line) if x.strip()]
            if len(parts) >=3:
                out.append({"subject": parts[0], "predicate": parts[1],
                            "object": parts[2]})
        return _clean_triples(out)
    if isinstance(raw, (list, tuple)):
        for it in raw:
            if isinstance(it, dict):
                sub = (it.get("subject") or it.get("s") or it.get("头")
                        or it.get("from") or "")
                pred = (it.get("predicate") or it.get("p") or it.get("relation")
                        or it.get("谓词") or it.get("rel") or "")
                obj = (it.get("object") or it.get("o") or it.get("尾")
                        or it.get("to") or "")
                if sub and pred and obj:
                    out.append({"subject": sub, "predicate": pred,
                                "object": obj})
                elif it.get("triples"):
                    out.extend(parse_triples(it.get("triples")))
            elif isinstance(it, (list, tuple)) and len(it) >=3:
                out.append({"subject": it[0], "predicate": it[1],
                            "object": it[2]})
        return _clean_triples(out)
    return out


def _clean_triples(items):
    """清洗：归一化、去自环、去重、限长。"""
    seen = set()
    out = []
    for it in items or []:
        try:
            s = str(it.get("subject") or "").strip()
            p = str(it.get("predicate") or "").strip()
            o = str(it.get("object") or "").strip()
        except Exception:
            continue
        if not s or not p or not o:
            continue
        s, o = s[:40], o[:40]
        p = norm_predicate(p)
        if not p:
            continue
        if norm_entity(s) == norm_entity(o):
            continue # "我是我"这种自环没有信息量
        try:
            conf = float(it.get("confidence") or 1.0)
        except (TypeError, ValueError):
            conf = 1.0
        key = (norm_entity(s), p.lower(), norm_entity(o))
        if key in seen:
            continue
        seen.add(key)
        out.append({"subject": s, "predicate": p, "object": o,
                    "confidence": max(0.05, min(1.0, conf))})
        if len(out) >=40:
            break
    return out


# ============================================================写入

def _upsert_entity(c, user_id, name, etype=""):
    n = norm_entity(name)
    if not n:
        return
    now = time.time()
    try:
        row = c.execute("SELECT id, mentions FROM kg_entities "
                        "WHERE user_id=? AND norm=?", (user_id, n)).fetchone()
        if row:
            c.execute("UPDATE kg_entities SET mentions=?, updated_at=?, "
                        "etype=CASE WHEN ?='' THEN etype ELSE ? END WHERE id=?",
                        (int(row[1] or 0) +1, now, str(etype or ""),
                        str(etype or ""), int(row[0])))
        else:
            c.execute("INSERT INTO kg_entities(user_id, name, norm, etype, "
                        "mentions, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                        (user_id, str(name)[:40], n, str(etype or "")[:16],
                        now, now))
    except sqlite3.Error:
        pass


def ingest_triples(mem=None, user_id="", triples=None, source="", perm_id=0):
    """把一批三元组写进图谱。返回写入条数。

    重复的边不新增，只把 confidence略微抬高（同一条关系被反复提到 =更可信）。
    整个函数吞掉所有异常：图谱写失败绝不能影响聊天。
    """
    uid = str(user_id or "")
    items = parse_triples(triples)
    if not items:
        return 0
    conn, owned = _conn(mem)
    n = 0
    try:
        c = conn.cursor()
        with _LOCK:
            for it in items:
                try:
                    s, p, o = it["subject"], it["predicate"], it["object"]
                    sn, pn, on = norm_entity(s), p.lower(), norm_entity(o)
                    if not sn or not pn or not on:
                        continue
                    _upsert_entity(c, uid, s)
                    _upsert_entity(c, uid, o)
                    now = time.time()
                    row = c.execute(
                        "SELECT id, confidence FROM kg_relations WHERE user_id=? "
                        "AND s_norm=? AND p_norm=? AND o_norm=? "
                        "AND status='active' LIMIT 1",
                        (uid, sn, pn, on)).fetchone()
                    if row:
                        conf = min(1.0, float(row[1] or 1.0) +0.1)
                        c.execute("UPDATE kg_relations SET confidence=?, "
                                    "updated_at=?, subject=?, object=? WHERE id=?",
                                    (conf, now, s, o, int(row[0])))
                    else:
                        c.execute(
                            "INSERT INTO kg_relations(user_id, subject, "
                            "predicate, object, s_norm, p_norm, o_norm, "
                            "confidence, source, perm_id, status, created_at, "
                            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'active',?,?)",
                            (uid, s, p, o, sn, pn, on,
                                float(it.get("confidence") or 1.0),
                                str(source or "")[:32], int(perm_id or 0),
                                now, now))
                    n +=1
                except sqlite3.Error:
                    continue
            try:
                conn.commit()
            except sqlite3.Error:
                pass
        if n:
            _bump()
    except Exception:
        pass
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass
    return n


# ============================================================读取 /图

def counts(user_id="", mem=None):
    """返回 (实体数,关系数)。"""
    uid = str(user_id or "")
    conn, owned = _conn(mem)
    try:
        c = conn.cursor()
        if uid:
            e = c.execute("SELECT COUNT(*) FROM kg_entities WHERE user_id=?",
                            (uid,)).fetchone()[0]
            r = c.execute("SELECT COUNT(*) FROM kg_relations WHERE user_id=? "
                            "AND status='active'", (uid,)).fetchone()[0]
        else:
            e = c.execute("SELECT COUNT(*) FROM kg_entities").fetchone()[0]
            r = c.execute("SELECT COUNT(*) FROM kg_relations "
                            "WHERE status='active'").fetchone()[0]
        return int(e or 0), int(r or 0)
    except sqlite3.Error:
        return 0,0
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass


def stats(user_id="", mem=None):
    """给界面用的统计。"""
    e, r = counts(user_id, mem=mem)
    return {"ok": True, "entities": e, "relations": r,
            "networkx": available(), "schema": KG_SCHEMA_VERSION}


def _edges(mem=None, user_id=""):
    """取出边列表 (subject, predicate, object, confidence)。"""
    uid = str(user_id or "")
    conn, owned = _conn(mem)
    try:
        c = conn.cursor()
        if uid:
            c.execute("SELECT subject, predicate, object, confidence "
                        "FROM kg_relations WHERE user_id=? AND status='active' "
                        "ORDER BY confidence DESC, id DESC LIMIT ?",
                        (uid, MAX_EDGES))
        else:
            c.execute("SELECT subject, predicate, object, confidence "
                        "FROM kg_relations WHERE status='active' "
                        "ORDER BY confidence DESC, id DESC LIMIT ?",
                        (MAX_EDGES,))
        return [(str(a or ""), str(b or ""), str(d or ""), float(e or 0))
                for a, b, d, e in c.fetchall()]
    except sqlite3.Error:
        return []
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass


def available():
    """networkx装没装。没装也能用（走降级邻接表）。"""
    try:
        import networkx # noqa: F401
        return True
    except Exception:
        return False


def _cache_key(user_id):
    return "kg|%s|%.3f" % (str(user_id or ""), float(_STAMP["n"]))


def _load_simple(edges):
    """降级实现：纯 dict邻接表。返回 {node: [(other, pred, dir)]}。"""
    adj = {}
    for s, p, o, _c in edges:
        adj.setdefault(s, []).append((o, p, "out"))
        adj.setdefault(o, []).append((s, p, "in"))
    return adj


def load_graph(mem=None, user_id="", force=False):
    """把某个账号的边加载成 networkx图（无 networkx时返回降级结构）。

    返回 (graph, backend)，backend ∈ {"networkx", "simple", "empty"}。
    缓存30秒，写操作会立刻让缓存失效。
    """
    uid = str(user_id or "")
    now = time.time()
    key = _cache_key(uid)
    with _LOCK:
        if (not force and _CACHE.get("key") == key
                and _CACHE.get("graph") is not None
                and (now - float(_CACHE.get("at") or 0)) < _CACHE_TTL):
            return _CACHE["graph"], _CACHE["backend"]

    edges = _edges(mem=mem, user_id=uid)
    if not edges:
        with _LOCK:
            _CACHE.update({"key": key, "graph": None,
                            "backend": "empty", "at": now})
        return None, "empty"

    graph = None
    backend = "simple"
    try:
        import networkx as nx
        g = nx.MultiDiGraph()
        for s, p, o, _c in edges:
            g.add_edge(s, o, key=p, predicate=p)
        graph = g
        backend = "networkx"
    except Exception:
        graph = _load_simple(edges)
        backend = "simple"

    with _LOCK:
        _CACHE.update({"key": key, "graph": graph,
                        "backend": backend, "at": now})
    return graph, backend


def _nodes_of(graph, backend):
    if graph is None:
        return []
    if backend == "networkx":
        try:
            return list(graph.nodes())
        except Exception:
            return []
    return list(graph.keys())


def _neighbors(graph, backend, node):
    """返回 [(other, predicate, direction)]，direction: out/in。"""
    if graph is None:
        return []
    if backend == "networkx":
        out = []
        try:
            for _u, v, k in graph.out_edges(node, keys=True):
                out.append((v, str(k), "out"))
            for u, _v, k in graph.in_edges(node, keys=True):
                out.append((u, str(k), "in"))
        except Exception:
            return []
        return out
    return list(graph.get(node) or [])


def find_seeds(graph, backend, query, limit=6):
    """从这句话里找到能当起点的实体。

    匹配策略（从准到松）：
            1.查询串里直接包含实体名（"老板的猫怎么样" ->老板 /老板的猫）
            2.实体名包含查询串（"橘子" -> "橘子猫"）
            3.2-gram重合度 >=0.34（措辞不同但明显是同一个点）
    长名优先，避免"猫"这种泛词把结果全带偏。
    """
    q = norm_entity(query)
    if not q or graph is None:
        return []
    nodes = _nodes_of(graph, backend)
    hits = []
    for name in nodes:
        nm = norm_entity(name)
        if not nm:
            continue
        score = 0.0
        if nm in q:
            score =1.0 + min(0.5, len(nm) /20.0)
        elif len(q) >=2 and q in nm:
            score = 0.8
        else:
            qb, nb = _bigrams(q), _bigrams(nm)
            if qb and nb:
                jac = len(qb & nb) / float(len(qb | nb) or 1)
                if jac >=0.34:
                    score =0.5 + jac *0.3
        if score >0:
            hits.append((score, name))
    hits.sort(key=lambda x: (-x[0], -len(str(x[1]))))
    return [h[1] for h in hits[:int(limit)]]


def walk(graph, backend, seeds, hops=2, max_lines=MAX_BLOCK_LINES):
    """从种子点做 BFS，收集沿途的关系。

    返回 [{subject, predicate, object, depth}]，浅层优先。
    """
    if graph is None or not seeds:
        return []
    hops = max(1, min(int(hops or 2), MAX_HOPS))
    max_lines = int(max_lines or MAX_BLOCK_LINES)
    seen_nodes = set(str(s) for s in seeds)
    seen_edges = set()
    out = []
    frontier = list(seeds)
    for depth in range(1, hops +1):
        nxt = []
        for node in frontier:
            for other, pred, direction in _neighbors(graph, backend, node):
                if direction == "out":
                    s, o = node, other
                else:
                    s, o = other, node
                ekey = (norm_entity(s), str(pred).lower(), norm_entity(o))
                if ekey in seen_edges:
                    continue
                seen_edges.add(ekey)
                out.append({"subject": s, "predicate": pred, "object": o,
                            "depth": depth})
                if other not in seen_nodes:
                    seen_nodes.add(other)
                    nxt.append(other)
            if len(out) >= max_lines *3:
                break
        frontier = nxt
        if not frontier or len(out) >= max_lines:
            break
    out.sort(key=lambda x: (int(x.get("depth") or 9),
                            -len(str(x.get("subject")))))
    return out[:max_lines]


def seed_by_predicate(mem=None, user_id="", query="", limit=4):
    """兜底找起点：从谓词里找线索。

    「老板那只猫」这种问法里，老板不是一个实体名，而是关系谓词
    「是…的老板」的一部分。光靠实体名匹配会一个种子都找不到，
    所以这里退一步：谓词里 >=2字的词块在问句里出现，就把两端都当种子。
    只在实体匹配全空时才走这条，免得把精度拖坏。
    """
    q = norm_entity(query)
    if not q:
        return []
    out = []
    for s, pr, o, _c in _edges(mem=mem, user_id=user_id):
        chunks = [x for x in re.split(r"[…。的]+", str(pr or ""))
            if len(x) >=2]
        for ch in chunks:
            nc = norm_entity(ch)
            if nc and nc in q:
                out.append(s)
                out.append(o)
                break
        if len(out) >= int(limit) *2:
            break
    seen = set()
    uniq = []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq[:int(limit) *2]


def recall(user_id="", query="", hops=2, limit=MAX_BLOCK_LINES, mem=None):
    """多跳检索主入口。返回 {"ok","hits","seeds","backend","edges"}。"""
    try:
        graph, backend = load_graph(mem=mem, user_id=user_id)
        if backend == "empty":
            return {"ok": True, "hits": [], "seeds": [],
                    "backend": backend, "edges":0}
        seeds = find_seeds(graph, backend, query)
        #再补一轮「谓词线索」：「老板那只猫」里老板不是实体名，
        #而是「是…的老板」这个谓词的一半，光靠实体名会漏掉整条链。
        _more = seed_by_predicate(mem=mem, user_id=user_id,
            query=query)
        _seen = set()
        seeds = [x for x in list(seeds) + list(_more)
           if not (x in _seen or _seen.add(x))]
        hits = walk(graph, backend, seeds, hops=hops, max_lines=limit)
        return {"ok": True, "hits": hits, "seeds": seeds,
                "backend": backend,
                "edges": len(_edges(mem=mem, user_id=user_id))}
    except Exception as e:
        return {"ok": False, "hits": [], "seeds": [], "backend": "error",
                "edges":0, "msg": "%s: %s" % (type(e).__name__, e)}


def format_lines(hits):
    """把关系渲染成人能读的一行行。"""
    lines = []
    for h in hits or []:
        try:
            s = str(h.get("subject") or "").strip()
            p = str(h.get("predicate") or "").strip()
            o = str(h.get("object") or "").strip()
        except Exception:
            continue
        if not s or not p or not o:
            continue
        lines.append("- %s —(%s)→ %s" % (s, p, o))
    return lines


def context_block(user_id="", query="", hops=2, limit=MAX_BLOCK_LINES, mem=None):
    """给 system prompt用的图谱块。没命中就返回空串（不占 token）。"""
    try:
        if not str(user_id or "").strip() or not str(query or "").strip():
            return ""
        res = recall(user_id=user_id, query=query, hops=hops,
                        limit=limit, mem=mem)
        lines = format_lines(res.get("hits"))
        if not lines:
            return ""
        head = ("【关系网】顺着这句话，这些人和事是连在一起的"
                "（可自然引用，别硬塞）：")
        return "\n" + head + "\n" + "\n".join(lines) + "\n"
    except Exception:
        return ""


# ============================================================历史建图

BUILD_PROMPT = """下面是一条关于「{name}」的长期记忆。请把它拆成「主谓宾」三元组。

要求：
1.只输出 JSON数组，不要解释、不要代码块围栏。
2.每条形如 {{"subject":"主语","predicate":"谓词","object":"宾语"}}
3.主语/宾语用具体的名词（人、物、地方、组织、作品），不要用"我""他"这类代词，
指代不清就换成这条记忆里出现的名字。
4.谓词用2~6个字的动词短语，比如「养了」「住在」「喜欢」「是…的老板」「计划去」。
5.拆不出来就输出 [] ——宁可不拆，也不要编。

记忆内容：
{content}
"""


def extract_from_text(text, name="", model_ask=None):
    """让模型把一段文字拆成三元组。model_ask(prompt, system, max_tokens) -> str"""
    if not model_ask or not str(text or "").strip():
        return []
    try:
        prompt = BUILD_PROMPT.format(name=name or "这个人",
                                        content=str(text)[:1200])
        raw = model_ask(prompt, "你是一个把句子拆成三元组的信息抽取器，"
                        "只输出 JSON。",400)
        return parse_triples(raw)
    except Exception:
        return []


def build_from_perm(mem=None, user_id="", limit=30, model_ask=None,
                    progress=None, min_importance=1, skip_done=True):
    """历史建图：从老的 perm_memory里抽一批出来织网。

    返回 {"ok","scanned","added","skipped","msg"}。

    「慢慢织网」的落点就在这里：每次只处理 limit条（默认30，被
    agent.py的后台线程限制在20~50之间），处理过的打标，下次接着往后走。
    """
    uid = str(user_id or "")
    out = {"ok": True, "scanned":0, "added":0, "skipped":0, "msg": ""}
    if not uid or not model_ask:
        out["msg"] = "没有账号或没有模型"
        return out
    conn, owned = _conn(mem)
    try:
        c = conn.cursor()
        c.execute("SELECT id, content, category, importance FROM perm_memory "
                    "WHERE user_id=? AND status='active' AND importance>=? "
                    "ORDER BY id ASC", (uid, int(min_importance or 1)))
        rows = c.fetchall()
        done = set()
        try:
            for r in c.execute("SELECT DISTINCT perm_id FROM kg_relations "
                                "WHERE user_id=? AND perm_id>0", (uid,)):
                done.add(int(r[0] or 0))
        except sqlite3.Error:
            pass
        todo = [r for r in rows if int(r[0]) not in done]
        if not todo:
            out["msg"] = "所有永久记忆都已织进图里"
            return out
        for pid, content, _category, _imp in todo:
            if out["scanned"] >= int(limit):
                break
            out["scanned"] +=1
            if skip_done and int(pid) in done:
                out["skipped"] +=1
                continue
            triples = extract_from_text(content, name=uid,
                                        model_ask=model_ask)
            if not triples:
                #拆不出来也记一笔，免得每次启动都重试同一条
                try:
                    with _LOCK:
                        c.execute(
                            "INSERT INTO kg_relations(user_id, subject, "
                            "predicate, object, s_norm, p_norm, o_norm, "
                            "confidence, source, perm_id, status, created_at, "
                            "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (uid, "", "", "", "", "", "",0.0, "perm_skip",
                                int(pid), "skipped", time.time(), time.time()))
                        conn.commit()
                except sqlite3.Error:
                    pass
                out["skipped"] +=1
                continue
            n = ingest_triples(mem=mem, user_id=uid, triples=triples,
                                source="perm", perm_id=int(pid))
            out["added"] += n
            if n ==0:
                out["skipped"] +=1
            if progress:
                try:
                    progress(out["scanned"], out["added"])
                except Exception:
                    pass
        out["msg"] = "扫了 %d条，新增 %d条关系" % (out["scanned"],
                                                    out["added"])
        return out
    except Exception as e:
        out["ok"] = False
        out["msg"] = "%s: %s" % (type(e).__name__, e)
        return out
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass


# ============================================================导出 /导入

def export_rows(mem=None, user_id=""):
    """导出图谱数据（给时光胶囊 /备份用）。"""
    uid = str(user_id or "")
    conn, owned = _conn(mem)
    try:
        c = conn.cursor()
        if uid:
            ents = c.execute(
                "SELECT user_id, name, norm, etype, mentions, created_at "
                "FROM kg_entities WHERE user_id=? ORDER BY id", (uid,)).fetchall()
            rels = c.execute(
                "SELECT user_id, subject, predicate, object, confidence, "
                "source, perm_id, status, created_at FROM kg_relations "
                "WHERE user_id=? ORDER BY id", (uid,)).fetchall()
        else:
            ents = c.execute(
                "SELECT user_id, name, norm, etype, mentions, created_at "
                "FROM kg_entities ORDER BY id").fetchall()
            rels = c.execute(
                "SELECT user_id, subject, predicate, object, confidence, "
                "source, perm_id, status, created_at FROM kg_relations "
                "ORDER BY id").fetchall()
        return (
            [{"user_id": r[0], "name": r[1], "norm": r[2], "etype": r[3],
                "mentions": int(r[4] or 1), "created_at": float(r[5] or 0)}
                for r in ents],
            [{"user_id": r[0], "subject": r[1], "predicate": r[2],
                "object": r[3], "confidence": float(r[4] or 1.0),
                "source": r[5], "perm_id": int(r[6] or 0),
                "status": r[7] or "active", "created_at": float(r[8] or 0)}
                for r in rels],
        )
    except sqlite3.Error:
        return [], []
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass


def import_rows(mem=None, rows=None, user_id=None):
    """导入图谱数据。rows是 {"kg_entities": [...], "kg_relations": [...]}。

    老版本胶囊里没有这两个键 ->传空就是空转，不会报错（这是安全兜底）。
    """
    out = {"ok": True, "entities":0, "relations":0}
    if not isinstance(rows, dict):
        return out
    uid_force = str(user_id) if user_id else ""
    conn, owned = _conn(mem)
    try:
        for it in (rows.get("kg_entities") or []):
            if not isinstance(it, dict):
                continue
            uid = uid_force or str(it.get("user_id") or "")
            if not uid:
                continue
            try:
                with _LOCK:
                    c = conn.cursor()
                    _upsert_entity(c, uid, it.get("name") or it.get("norm") or "",
                                    it.get("etype") or "")
                    conn.commit()
                out["entities"] +=1
            except Exception:
                continue
        if out["entities"]:
            _bump()
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass
    by_user = {}
    for it in (rows.get("kg_relations") or []):
        if not isinstance(it, dict):
            continue
        uid = uid_force or str(it.get("user_id") or "")
        if not uid:
            continue
        by_user.setdefault(uid, []).append({
            "subject": it.get("subject"), "predicate": it.get("predicate"),
            "object": it.get("object"),
            "confidence": it.get("confidence")})
    for uid, items in by_user.items():
        try:
            out["relations"] += ingest_triples(mem=mem, user_id=uid,
                                                triples=items, source="import")
        except Exception:
            continue
    return out


def clear_user(user_id, mem=None):
    """删掉某人的图谱（跟 clear_perm配套用）。"""
    uid = str(user_id or "")
    if not uid:
        return 0
    conn, owned = _conn(mem)
    try:
        with _LOCK:
            c = conn.cursor()
            c.execute("DELETE FROM kg_relations WHERE user_id=?", (uid,))
            c.execute("DELETE FROM kg_entities WHERE user_id=?", (uid,))
            conn.commit()
        _bump()
        return 1
    except sqlite3.Error:
        return 0
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
 import io
 import sys
 sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
  errors="replace")
 print("networkx:", available())
 m = __import__("memory").Memory()
 print("schema:", ensure_schema(m.conn))
 clear_user("__kgtest__", mem=m)
 n = ingest_triples(mem=m, user_id="__kgtest__", triples=[
  ["老板", "送了", "张三"],
  ["张三", "养了", "橘子"],
  ["橘子", "是一只", "猫"],
  ["李四", "是", "张三的老板"],
 ], source="test")
 print("ingested:", n)
 print("counts:", counts("__kgtest__", mem=m))
 r1 = recall(user_id="__kgtest__", query="张三最近怎么样", hops=1, mem=m)
 print("hops1 seeds:", r1.get("seeds"), "hits:", len(r1.get("hits") or []))
 r3 = recall(user_id="__kgtest__", query="老板那只猫", hops=3, mem=m)
 print("hops3 seeds:", r3.get("seeds"))
 rows = [(h["depth"], h["subject"], h["predicate"], h["object"])
  for h in (r3.get("hits") or [])]
 for d, s, pp, o in rows:
  print(" depth", d, s, "-(", pp, ")-> ", o)
 print("block:", context_block(user_id="__kgtest__", query="老板那只猫",
  hops=3, mem=m))
 print("stats:", stats("__kgtest__", mem=m))
