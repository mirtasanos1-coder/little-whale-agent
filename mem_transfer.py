# -*- coding: utf-8 -*-
"""记忆导入 / 导出 / 定时备份

【做什么】
    1. 把 bot_memory.db 里的记忆类数据导出成一个 JSON 文件（可读、可改、可分享）
    2. 把这种 JSON 重新导回数据库（支持「合并」和「覆盖」两种模式）
    3. 按设置定时自动导出（单位：分钟/小时/天/周/月/年，上限 365 天）

【为什么不用 sqlite 的 .backup】
    .backup 出来是二进制，人看不懂，也没法只挑一部分导入。
    JSON 可以直接改一句话再导回去，还能跨机器/跨版本搬。

【安全】
    - 导出：只读，参数化查询
    - 导入：每个字段都过类型校验和长度上限；SQL 全部参数化；
            覆盖模式会先把整个 db 复制成 .bak-时间戳 再动手
    - 不导出 api_key / 密码类字段（那些在 config.json 里，不属于记忆）
"""
import io
import json
import os
import shutil
import sqlite3
import threading
import time

import config as cfg_mod
import security


def _ndef(v, d):
    """v1.8.8：0 / 0.0 / False 都是合法配置值，只有 None 和空串才回落默认。

    以前到处写 `cfg.get(K) or N`，用户把某项设成 0 会被强行改回默认值 ——
    「主动关心最短间隔 0 分钟」「每日上限 0 条」「生图概率 0」全都会失效。
    """
    if v is None:
        return d
    if isinstance(v, str) and not v.strip():
        return d
    return v



_lock = threading.RLock()
STATE_FILE = "mem_export_state.json"
EXPORT_DIR = "memory_export"
IMPORT_DIR = "memory_import"

# 导出哪些表，以及每张表的「自然键」——合并模式靠它判断是不是同一条
EXPORT_TABLES = {
    "perm_memory": {"key": None, "order": "id"},
    "memories": {"key": ("scope", "person", "fact"), "order": "id"},
    "messages": {"key": ("session_key", "role", "ts"), "order": "id"},
    "chat_log": {"key": ("scope", "person", "role", "created_at"), "order": "id"},
    "summaries": {"key": ("session_key", "ts"), "order": "id"},
    "user_events": {"key": ("user_id", "event_desc", "target_time"), "order": "id"},
    "care_plans": {"key": ("session_key", "plan_at", "reason"), "order": "id"},
    "affinity": {"key": ("user_id",), "order": "user_id"},
    "diary": {"key": ("user_id", "day"), "order": "id"},
    "moment_comments": {"key": ("moment_tid", "commenter_id", "content", "created_at"),
                        "order": "id"},
    "feed_seen": {"key": ("post_key",), "order": "id"},
    # v1.3.0 ⑱：自省与情绪（以前根本导不出去，用户问「自省能不能单独备份」就是卡在这）
    # key 用 None = 导入时按「整行内容完全相同」判重（见 import_data）。
    "inner_thoughts": {"key": None, "order": "id"},
    "think_log": {"key": None, "order": "id"},
    "emotion_log": {"key": None, "order": "id"},
    # v1.8.0：关系网（知识图谱）两张表 ——不登记的话导胶囊会把图丢了
    "kg_entities": {"key": ("user_id", "norm"), "order": "id"},
    "kg_relations": {"key": ("user_id", "s_norm", "p_norm", "o_norm"),
                    "order": "id"},
}

# v1.3.0 ⑱：记忆按「种类」分开导出/导入 —— 用户要的是临时/永久/日记/学习/自省各自独立，
# 而不是永远只有「导出全部」。kind -> (显示名, 表清单)；learn 走独立的 learn.db。
KINDS = {
    "perm":     ("永久记忆", ["perm_memory", "kg_entities", "kg_relations"]),
    "temp":     ("临时记忆（对话与摘要）", ["messages", "chat_log", "summaries", "memories"]),
    "event":    ("事件与关心计划", ["user_events", "care_plans"]),
    "affinity": ("好感度与关系", ["affinity"]),
    "diary":    ("日记", ["diary"]),
    "reflect":  ("自省与情绪", ["inner_thoughts", "think_log", "emotion_log"]),
    "learn":    ("学习清单", None),
}


def kind_info(kind=""):
    k = str(kind or "").strip().lower()
    return KINDS.get(k)


def kind_dir(kind=""):
    d = os.path.join(export_dir(), str(kind or "all"))
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d

# 这些列导入时按整数处理，避免 JSON 里出现字符串把 SQL 搞乱
_INT_COLS = {
    "importance", "use_count", "msgs", "positive", "negative", "status_flag",
    "msg_count", "supersedes", "score", "liked", "reply_count", "depth",
}
_FLOAT_COLS = {"created_at", "last_used", "ts", "target_time", "plan_at",
               "fired_at", "updated_at", "last_decay_day_ts", "time"}
_TEXT_MAX = 8000


def data_dir():
    env = (os.environ.get("QQ_AGENT_DATA_DIR") or "").strip()
    if env:
        return env
    return os.path.dirname(os.path.abspath(__file__))


def export_dir():
    """备份/导出文件放在哪儿。

    v1.3.2（QA BUG-10）：以前写死常量 `memory_export`，而 app_ui 那边的
    「备份列表 / 打开备份目录」读的是配置 `memory_export_dir`（默认
    `memory_backup`）—— 一个写 A、一个读 B，「点备份成功、列表永远空」。
    现在统一：配置项优先（新名 `memory_backup_dir` 优先，老名 `memory_export_dir`
    继续认，方便老用户平滑升级），都没配才用 EXPORT_DIR。
    """
    name = ""
    try:
        import config as _cfg_mod
        name = (str(_cfg_mod.get("memory_backup_dir") or "").strip()
                or str(_cfg_mod.get("memory_export_dir") or "").strip())
    except Exception:
        name = ""
    d = os.path.join(data_dir(), name or EXPORT_DIR)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _state_path():
    return os.path.join(data_dir(), STATE_FILE)


def _read_state():
    try:
        with open(_state_path(), "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_state(d):
    try:
        p = _state_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        pass


# ==================== 导出 ====================

def _connect(mem=None):
    """拿一个 sqlite 连接。优先用传进来的 Memory 实例的连接"""
    if mem is not None and getattr(mem, "conn", None) is not None:
        return mem.conn, False
    import memory as memmod
    m = memmod.Memory()
    return m.conn, True


def export_data(mem=None, tables=None, limit_per_table=0):
    """把记忆导成一个 dict（不落盘）"""
    conn, owned = _connect(mem)
    out = {
        "format": "xiaojingyu-memory",
        "format_version": 1,
        "app_version": getattr(cfg_mod, "APP_VERSION", ""),
        "exported_at": time.time(),
        "exported_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tables": {},
        "counts": {},
    }
    want = list(tables or EXPORT_TABLES.keys())
    try:
        for t in want:
            if t not in EXPORT_TABLES:
                continue
            order = EXPORT_TABLES[t]["order"]
            sql = f"SELECT * FROM {t}"          # 表名来自白名单常量，不是用户输入
            if limit_per_table and int(limit_per_table) > 0:
                sql += f" ORDER BY {order} DESC LIMIT {int(limit_per_table)}"
            else:
                sql += f" ORDER BY {order} ASC"
            try:
                cur = conn.execute(sql)
            except sqlite3.Error:
                # 有些表没有 order 指定的列（比如自省表），退化成不排序再试一次
                try:
                    cur = conn.execute(f"SELECT * FROM {t}")
                except sqlite3.Error:
                    out["tables"][t] = []
                    out["counts"][t] = 0
                    continue
            try:
                cols = [d[0] for d in (cur.description or [])]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            except sqlite3.Error:
                rows = []
            out["tables"][t] = rows
            out["counts"][t] = len(rows)
    finally:
        if owned:
            try:
                conn.close()
            except Exception:
                pass
    out["total"] = sum(out["counts"].values())
    return out


def export_to_file(mem=None, path="", tables=None, limit_per_table=0):
    data = export_data(mem=mem, tables=tables, limit_per_table=limit_per_table)
    if not path:
        fn = "记忆备份_" + time.strftime("%Y%m%d_%H%M%S") + ".json"
        path = os.path.join(export_dir(), fn)
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        size = os.path.getsize(path)
        return {
            "ok": True, "path": path, "size": size,
            "total": data["total"], "counts": data["counts"],
            "msg": f"已导出 {data['total']} 条到 {os.path.basename(path)}（{size/1024:.1f}KB）",
        }
    except Exception as e:
        return {"ok": False, "msg": f"导出失败：{e}", "path": path}


def export_to_text(mem=None, path="", limit=2000):
    """导成给人看的 txt（按人分组，方便直接读）"""
    data = export_data(mem=mem)
    if not path:
        path = os.path.join(export_dir(),
                            "记忆可读版_" + time.strftime("%Y%m%d_%H%M%S") + ".txt")
    lines = [f"小鲸鱼记忆导出（可读版） {data['exported_text']}", ""]
    pm = data["tables"].get("perm_memory") or []
    by_user = {}
    for r in pm:
        by_user.setdefault(str(r.get("user_id") or "?"), []).append(r)
    for uid, rows in by_user.items():
        lines.append(f"===== 账号 {uid} （{len(rows)} 条长期记忆） =====")
        for r in rows[:limit]:
            st = "" if (r.get("status") or "active") == "active" else f"[{r.get('status')}]"
            lines.append(f"  · {st}[{r.get('category') or 'other'}] {r.get('content') or ''}")
        lines.append("")
    ev = data["tables"].get("user_events") or []
    if ev:
        lines.append(f"===== 事件（{len(ev)} 条） =====")
        for r in ev[:limit]:
            t = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(r.get("target_time") or 0)))
            lines.append(f"  · [{r.get('status')}] {t} {r.get('event_desc') or ''}")
        lines.append("")
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return {"ok": True, "path": path, "size": os.path.getsize(path),
                "msg": f"已导出可读版到 {os.path.basename(path)}"}
    except Exception as e:
        return {"ok": False, "msg": f"导出失败：{e}"}


def list_exports():
    d = export_dir()
    out = []
    try:
        for fn in os.listdir(d):
            if not fn.lower().endswith((".json", ".txt")):
                continue
            p = os.path.join(d, fn)
            st = os.stat(p)
            out.append({"name": fn, "path": p, "size": st.st_size,
                        "size_text": f"{st.st_size/1024:.1f}KB",
                        "time_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                        "mtime": st.st_mtime,
                        "kind": "json" if fn.lower().endswith(".json") else "txt"})
    except Exception:
        return []
    out.sort(key=lambda x: -x["mtime"])
    return out[:200]


# ==================== 导入 ====================

def import_dir():
    """「自动导入」监听的文件夹：把备份文件丢进去，最多一分钟就会被合并进库。"""
    d = os.path.join(data_dir(), IMPORT_DIR)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _pending_imports():
    """目录里待导入的文件（按修改时间从旧到新，先丢进来的先导）"""
    d = import_dir()
    out = []
    try:
        for fn in os.listdir(d):
            p = os.path.join(d, fn)
            if not os.path.isfile(p):
                continue
            if not fn.lower().endswith((".json", ".jsonl", ".txt", ".zip")):
                continue
            out.append((os.stat(p).st_mtime, p, fn))
    except Exception:
        return []
    out.sort()
    return [(p, fn) for _t, p, fn in out]


def list_import_dir():
    """给界面看：目录位置 + 里面几个文件在等"""
    return {"dir": import_dir(), "pending": [fn for _p, fn in _pending_imports()]}


def scan_import_dir(mem, log=None):
    """把 memory_import/ 里的备份文件挨个合并导入，成功的挪进 done/，失败的进 failed/。

    为什么用「合并」而不是「覆盖」：
      自动导入是无人值守的，覆盖模式一旦导错一个空文件就会把用户的记忆清空；
      合并只会「多出一些记忆」或者「什么都没变」，错了也能接受。
      想覆盖还是用手动那边的按钮，并且有确认框。
    """
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass

    items = _pending_imports()
    if not items:
        return {"ok": True, "total": 0, "ok_count": 0, "bad_count": 0,
                "items": [], "msg": "没有待导入的文件"}
    base = import_dir()
    done = os.path.join(base, "done")
    failed = os.path.join(base, "failed")
    res = {"ok": True, "total": len(items), "ok_count": 0, "bad_count": 0, "items": []}
    for path, fn in items:
        try:
            r = import_from_file(mem, path, mode="merge")
        except Exception as e:
            r = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
        ok = bool(r.get("ok"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target_dir = done if ok else failed
        try:
            os.makedirs(target_dir, exist_ok=True)
            os.replace(path, os.path.join(target_dir, f"{stamp}_{fn}"))
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
        if ok:
            res["ok_count"] += 1
            _log(f"[记忆导入] {fn} 合并完成：{r.get('msg')}")
        else:
            res["bad_count"] += 1
            _log(f"[记忆导入] {fn} 导入失败：{r.get('msg')}")
        res["items"].append({"name": fn, "ok": ok, "msg": r.get("msg") or ""})
    res["msg"] = f"共 {res['total']} 个文件，成功 {res['ok_count']}，失败 {res['bad_count']}"
    return res


def _clean_value(col, val):
    """按列名把值洗成安全类型，超长截断，杜绝注入"""
    if val is None:
        return None
    if col in _INT_COLS:
        try:
            return int(float(val))
        except (TypeError, ValueError):
            return 0
    if col in _FLOAT_COLS:
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0
    if isinstance(val, (dict, list)):
        s = json.dumps(val, ensure_ascii=False)
    else:
        s = str(val)
    return s[:_TEXT_MAX]


def read_bundle(path):
    """读一个导出文件，返回 (data, err)"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        return None, f"读不了这个文件：{e}"
    if not isinstance(data, dict):
        return None, "文件格式不对（不是 JSON 对象）"
    if data.get("format") not in ("xiaojingyu-memory", "xiaojingyu-memory-kind"):
        return None, "这不是小鲸鱼的记忆备份文件"
    tabs = data.get("tables")
    if not isinstance(tabs, dict):
        return None, "文件里没有 tables 字段"
    return data, ""


def _backup_db():
    """覆盖导入前先整库备份（出事了能回退）"""
    import memory as memmod
    src = os.path.join(data_dir(), getattr(memmod, "DB_NAME", "bot_memory.db"))
    if not os.path.exists(src):
        return ""
    dst = src + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copy2(src, dst)
        return dst
    except Exception:
        return ""


def import_data(mem, data, mode="merge"):
    """导入。mode: merge=同键跳过 / replace=先清空再导入"""
    conn = mem.conn if getattr(mem, "conn", None) else None
    if conn is None:
        return {"ok": False, "msg": "数据库没打开"}
    mode = "replace" if str(mode).lower() == "replace" else "merge"
    backup = ""
    if mode == "replace":
        backup = _backup_db()
    stats = {}
    with _lock:
        try:
            for t, rows in (data.get("tables") or {}).items():
                if t not in EXPORT_TABLES or not isinstance(rows, list):
                    continue
                cols_exist = [d[1] for d in conn.execute(f"PRAGMA table_info({t})").fetchall()]
                if not cols_exist:
                    continue
                if mode == "replace":
                    conn.execute(f"DELETE FROM {t}")
                keyset = EXPORT_TABLES[t]["key"]
                ins = skip = 0
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    use = {k: _clean_value(k, v) for k, v in row.items() if k in cols_exist}
                    # id 交给数据库自己分配（合并模式下保留 id 会撞主键）
                    if mode == "merge":
                        use.pop("id", None)
                        if keyset:
                            where = " AND ".join(f"{k} IS ?" for k in keyset if k in cols_exist)
                            if where:
                                got = conn.execute(
                                    f"SELECT 1 FROM {t} WHERE {where} LIMIT 1",
                                    tuple(use.get(k) for k in keyset if k in cols_exist)).fetchone()
                                if got:
                                    skip += 1
                                    continue
                        elif EXPORT_TABLES[t].get("key", "x") is None and use:
                            # v1.3.0 ⑱：这类表没有自然键，就按「所有字段都相同」判重，
                            # 免得同一份自省备份导两次库里翻倍。
                            where = " AND ".join(f"{k} IS ?" for k in use)
                            got = conn.execute(f"SELECT 1 FROM {t} WHERE {where} LIMIT 1",
                                               tuple(use.values())).fetchone()
                            if got:
                                skip += 1
                                continue
                    if not use:
                        continue
                    cols = list(use.keys())
                    sql = (f"INSERT INTO {t} ({','.join(cols)}) "
                           f"VALUES ({','.join('?' for _ in cols)})")
                    conn.execute(sql, [use[c] for c in cols])
                    ins += 1
                stats[t] = {"insert": ins, "skip": skip}
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return {"ok": False, "msg": f"导入失败（已回滚）：{e}", "stats": stats}
    total_ins = sum(v["insert"] for v in stats.values())
    return {
        "ok": True, "stats": stats, "insert": total_ins, "mode": mode, "backup": backup,
        "msg": f"导入完成：新增 {total_ins} 条" + (f"（原库已备份为 {os.path.basename(backup)}）" if backup else ""),
    }


LLM_IMPORT_PROMPT = """下面是一段来历不明的记忆数据（可能是 JSON、CSV、纯文本或聊天记录导出）。
请把它转成标准结构，**只输出 JSON**，不要解释：
{"messages":[{"user_id":"QQ号","role":"user","content":"原话","ts":0}],
 "affinity":[{"user_id":"QQ号","score":0}],
 "events":[{"user_id":"QQ号","what":"要做的事","target_time":0}]}

规则：
- role 只能是 user（对方说的）或 assistant（我说的话）；
- user_id 不知道就留空字符串；ts 是秒级时间戳，不知道写 0；
- 只搬原文，**不要编造**内容，不要总结；
- 没有的类别给空数组。

原始数据：
{raw}
"""


def _read_any_text(path, limit=14000):
    """读任意文本文件（utf-8 / utf-8-sig / gbk 依次试），截断到 limit 字。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "utf-16"):
        try:
            with io.open(path, "r", encoding=enc, errors="strict") as f:
                return f.read()[:limit]
        except (UnicodeDecodeError, UnicodeError, LookupError):
            continue
        except Exception:
            break
    try:
        with io.open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[:limit]
    except Exception as e:
        return ""


def import_with_llm(mem, path, mode="merge", model_ask=None):
    """v1.1.4 2.2：认不出的格式，交给大模型转成标准结构再导。

    为什么要这一步：用户手里可能是别的工具导的 CSV、聊天截图 OCR 出来的 txt、
    或者是某个旧版本自己拼的 JSON。硬编码解析器永远追不上，不如让模型做一次翻译。
    注意：模型只负责「搬」，不负责「编」——prompt 里写死了不许总结、不许编造。
    """
    if model_ask is None:
        return {"ok": False, "msg": "这段数据认不出来，需要接一个可用的模型来解析"}
    raw = _read_any_text(path)
    if len(raw.strip()) < 20:
        return {"ok": False, "msg": "这个文件几乎是空的，没什么可解析的"}
    try:
        text = model_ask(LLM_IMPORT_PROMPT.format(raw=raw)) or ""
    except Exception as e:
        return {"ok": False, "msg": "模型解析失败：%s" % e}
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return {"ok": False, "msg": "模型没给出可用的 JSON"}
    try:
        obj = json.loads(text[a:b + 1])
    except Exception:
        return {"ok": False, "msg": "模型给的 JSON 解析不了"}
    tables = {}
    if isinstance(obj.get("messages"), list):
        tables["messages"] = [r for r in obj["messages"] if isinstance(r, dict) and r.get("content")]
    if isinstance(obj.get("affinity"), list):
        tables["affinity"] = [r for r in obj["affinity"] if isinstance(r, dict) and r.get("user_id")]
    if isinstance(obj.get("events"), list):
        tables["user_events"] = [r for r in obj["events"] if isinstance(r, dict) and r.get("what")]
    if not tables:
        return {"ok": False, "msg": "模型没从中找到可导入的消息/好感度/待办"}
    res = import_data(mem, {"tables": tables}, mode=mode)
    if res.get("ok"):
        res["msg"] = "模型解析导入：" + str(res.get("msg") or "")
        res["parsed_by"] = "llm"
    return res


def import_from_file(mem, path, mode="merge", model_ask=None):
    data, err = read_bundle(path)
    if err:
        return {"ok": False, "msg": err}
    return import_data(mem, data, mode=mode)


def preview_bundle(path):
    """只看看里面有啥，不导入"""
    data, err = read_bundle(path)
    if err:
        return {"ok": False, "msg": err}
    return {"ok": True, "exported_text": data.get("exported_text") or "",
            "app_version": data.get("app_version") or "",
            "counts": data.get("counts") or {}, "total": data.get("total") or 0}


# ==================== v1.3.0 ⑱：整库（.db）导出 / 导入 ====================
# 为什么之前只给 JSON：JSON 能改、能挑着导。但用户要「原样备份」的时候，
# 二进制整库才是无损的（表结构、索引、以后新增的字段全在里面）。
# 现在两条路都给：JSON = 可读可改；.db = 原样搬家。


def db_path():
    """主数据库文件（跟 memory.py 保持一致）。"""
    try:
        import memory as memmod
        fn = getattr(memmod, "_default_db_path", None)
        if callable(fn):
            return fn()
        return os.path.join(data_dir(), getattr(memmod, "DB_NAME", "memory.db"))
    except Exception:
        return os.path.join(data_dir(), "memory.db")


def export_to_db(path="", progress=None):
    """整库导出（用 sqlite 的 backup API，带 -wal 也能拿到一致快照）"""
    src = db_path()
    if not os.path.exists(src):
        return {"ok": False, "msg": "还没有数据库文件 —— 先让 Agent 跑一次再导出"}
    if not path:
        path = os.path.join(export_dir(),
                            "记忆整库_" + time.strftime("%Y%m%d_%H%M%S") + ".db")
    try:
        if os.path.abspath(path) == os.path.abspath(src):
            return {"ok": False, "msg": "不能覆盖正在用的库，换个文件名"}
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        s = sqlite3.connect(src, timeout=10)
        try:
            t = sqlite3.connect(path)
            try:
                with t:
                    s.backup(t)
            finally:
                t.close()
        finally:
            s.close()
        size = os.path.getsize(path)
        return {"ok": True, "path": path, "size": size,
                "msg": "已导出整库 %s（%.2f MB）" % (os.path.basename(path), size / 1048576.0)}
    except Exception as e:
        return {"ok": False, "msg": "导出数据库失败：%s" % e}


def db_to_bundle(path):
    """把一个 .db 读成和 JSON 备份同形状的结构（缺表就跳过，缺列也跳过）"""
    tables, counts = {}, {}
    src = sqlite3.connect("file:%s?mode=ro" % str(path).replace("\\", "/"), uri=True)
    try:
        have = {r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for t, meta in EXPORT_TABLES.items():
            if t not in have:
                continue
            order = (meta or {}).get("order") or "rowid"
            try:
                cur = src.execute("SELECT * FROM %s ORDER BY %s" % (t, order))
            except Exception:
                cur = src.execute("SELECT * FROM %s" % t)
            cols = [d[0] for d in (cur.description or [])]
            rows = []
            for r in cur.fetchall():
                rows.append({cols[i]: r[i] for i in range(len(cols))})
            tables[t] = rows
            counts[t] = len(rows)
    finally:
        src.close()
    total = sum(counts.values())
    return {
        "format": "xiaojingyu-memory",
        "app_version": "db-import",
        "exported_at": time.time(),
        "exported_text": "从数据库 %s 导入（%d 条）" % (os.path.basename(path), total),
        "tables": tables, "counts": counts, "total": total,
    }


def preview_db(path):
    try:
        data = db_to_bundle(path)
        return {"ok": True, "exported_text": data["exported_text"],
                "app_version": "db", "counts": data["counts"], "total": data["total"]}
    except Exception as e:
        return {"ok": False, "msg": "读不了这个数据库：%s" % e}


def import_from_db(mem, path, mode="merge"):
    """从 .db 导入（先读成内存里的表，再走和 JSON 完全相同的合并逻辑）"""
    try:
        data = db_to_bundle(path)
    except Exception as e:
        return {"ok": False, "msg": "读不了这个数据库：%s" % e}
    if not data.get("total"):
        return {"ok": False, "msg": "这个库里没读到记忆数据（是不是选错文件了？）"}
    res = import_data(mem, data, mode=mode)
    if res.get("ok"):
        res["msg"] = "（来自 .db）" + str(res.get("msg") or "")
    return res


# ==================== 定时自动导出 ====================

# ==================== v1.3.0 ⑱：按种类导出 / 导入 ====================
# 用户原话：「临时记忆/永久记忆/日记/学习/自省的导入导出、备份防重复检查、
# 打开目录要各自分开」——所以这里每种记忆有自己的目录、自己的按钮，
# 合并导入时按自然键（或整行内容）判重，重复导入不会让库里翻倍。

def _kind_bundle(mem, kind):
    """把某一类记忆读成一个 bundle（learn 走独立的 learn.db）"""
    info = kind_info(kind)
    if not info:
        return None
    name, tables = info
    if kind == "learn":
        rows = []
        try:
            import learn as _learn
            p = _learn.db_path()
            if os.path.isfile(p):
                c = sqlite3.connect(p)
                try:
                    cur = c.execute("SELECT * FROM learn_items")
                    cols = [d[0] for d in (cur.description or [])]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                finally:
                    c.close()
        except Exception:
            rows = []
        data = {"format": "xiaojingyu-memory-kind", "format_version": 1,
                "kind": "learn", "kind_name": name,
                "app_version": getattr(cfg_mod, "APP_VERSION", ""),
                "exported_at": time.time(),
                "exported_text": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tables": {"learn_items": rows},
                "counts": {"learn_items": len(rows)},
                "total": len(rows)}
        return data
    data = export_data(mem=mem, tables=tables)
    data["format"] = "xiaojingyu-memory-kind"
    data["kind"] = kind
    data["kind_name"] = name
    return data


def _kind_text(data):
    """给人看的 TXT 版：一条一条列出来，不是 JSON"""
    lines = ["小鲸鱼记忆备份 · %s" % (data.get("kind_name") or data.get("kind") or ""),
             "导出时间：%s" % data.get("exported_text", ""),
             ""]
    for t, rows in (data.get("tables") or {}).items():
        lines.append("===== %s（%d 条）=====" % (t, len(rows or [])))
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            bits = []
            for k, v in r.items():
                if k in ("id",):
                    continue
                if v in (None, "", 0):
                    continue
                if isinstance(v, float):
                    if k in ("ts", "created_at", "target_time", "plan_at", "exported_at"):
                        try:
                            v = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(v)))
                        except Exception:
                            pass
                bits.append("%s=%s" % (k, v))
            lines.append("- " + " | ".join(bits))
        lines.append("")
    return "\n".join(lines)


def export_kind(mem=None, kind="", fmt="json"):
    """导出某一类记忆。fmt: json / txt"""
    info = kind_info(kind)
    if not info:
        return {"ok": False, "msg": "不认识的记忆种类：%s" % kind}
    name = info[0]
    fmt = "txt" if str(fmt).lower() in ("txt", "text") else "json"
    try:
        data = _kind_bundle(mem, kind)
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
    if not data:
        return {"ok": False, "msg": "这段时间没读到内容"}
    d = kind_dir(kind)
    fn = "%s_%s.%s" % (name.replace("（", "_").replace("）", "").replace("/", "_"),
                       time.strftime("%Y%m%d_%H%M%S"), fmt)
    path = os.path.join(d, fn)
    try:
        if fmt == "txt":
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(_kind_text(data))
        else:
            with io.open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(data, ensure_ascii=False, indent=1))
    except Exception as e:
        return {"ok": False, "msg": "写文件失败：%s" % e}
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    return {"ok": True, "path": path, "size": size,
            "msg": "%s 导出完成：%d 条 -> %s" % (name, data.get("total", 0), os.path.basename(path))}


def list_kind_exports(kind=""):
    d = kind_dir(kind)
    out = []
    try:
        for fn in os.listdir(d):
            p = os.path.join(d, fn)
            if not os.path.isfile(p):
                continue
            try:
                st = os.stat(p)
            except Exception:
                continue
            out.append({"name": fn, "path": p, "size": st.st_size, "mtime": st.st_mtime})
    except Exception:
        pass
    out.sort(key=lambda x: x.get("mtime") or 0, reverse=True)
    return out


def import_kind(mem, kind="", path="", mode="merge"):
    """导入某一类记忆。merge=重复的跳过；replace=先清空这类数据再导入"""
    info = kind_info(kind)
    if not info:
        return {"ok": False, "msg": "不认识的记忆种类：%s" % kind}
    name = info[0]
    path = str(path or "").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "msg": "找不到这个文件：%s" % path}
    if str(path).lower().endswith(".db"):
        data = db_to_bundle(path)
        err = ""
    else:
        data, err = read_bundle(path)
    if not data or not isinstance(data, dict) or not data.get("tables"):
        return {"ok": False, "msg": err or "这个文件里没读到记忆数据"}
    if kind == "learn":
        return _import_learn(mem, data, mode=mode)
    tables = info[1] or []
    keep = {t: (data.get("tables") or {}).get(t) or [] for t in tables}
    if not any(keep.values()):
        return {"ok": False, "msg": "这个文件里没有「%s」的数据" % name}
    r = import_data(mem, {"tables": keep}, mode=mode)
    if r.get("ok"):
        r["msg"] = "「%s」%s" % (name, r.get("msg"))
    return r


def _import_learn(mem, data, mode="merge"):
    """学习清单在 learn.db 里，单独处理（按 source+title+content 判重）"""
    rows = (data.get("tables") or {}).get("learn_items") or []
    if not rows:
        return {"ok": False, "msg": "这个文件里没有学习清单"}
    try:
        import learn as _learn
        p = _learn.db_path()
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        if mode == "replace":
            _backup_db()
        # 全新的 learn.db 里还没有表：先让 learn 模块自己建一次
        if not os.path.isfile(p) or os.path.getsize(p) < 1024:
            try:
                _c0 = _learn._conn()
                _c0.close()
            except Exception:
                pass
        c = sqlite3.connect(p)
        ins = skip = 0
        try:
            cols_all = [d[1] for d in c.execute("PRAGMA table_info(learn_items)").fetchall()]
            if mode == "replace":
                c.execute("DELETE FROM learn_items")
            for row in rows:
                if not isinstance(row, dict):
                    continue
                use = {k: _clean_value(k, v) for k, v in row.items() if k in cols_all}
                use.pop("id", None)
                if not use:
                    continue
                if mode != "replace":
                    got = c.execute(
                        "SELECT 1 FROM learn_items WHERE source IS ? AND title IS ? AND content IS ? LIMIT 1",
                        (use.get("source"), use.get("title"), use.get("content"))).fetchone()
                    if got:
                        skip += 1
                        continue
                cols = list(use.keys())
                c.execute("INSERT INTO learn_items (%s) VALUES (%s)"
                          % (",".join(cols), ",".join("?" for _ in cols)),
                          [use[x] for x in cols])
                ins += 1
            c.commit()
        finally:
            c.close()
    except Exception as e:
        return {"ok": False, "msg": "导入学习清单失败：%s" % e}
    return {"ok": True, "insert": ins, "skip": skip, "mode": mode,
            "msg": "「学习清单」导入完成：新增 %d 条，跳过重复 %d 条" % (ins, skip)}


def auto_export_due():
    """按设置判断现在该不该自动导出，返回 (due, 下次时间戳, 说明)"""
    cfg = cfg_mod.load()
    if not bool(cfg.get("memory_export_enabled", False)):
        return False, 0, "未开启"
    every = int(_ndef(cfg.get("memory_export_every"), 1))
    unit = str(cfg.get("memory_export_unit") or "day")
    secs, desc = cfg_mod.export_interval_seconds(every, unit)
    if secs <= 0:
        return False, 0, "未开启"
    st = _read_state()
    last = float(st.get("last_ts") or 0)
    now = time.time()
    due = (now - last) >= secs
    return due, (last + secs), desc


def auto_export_tick(mem=None, log=None):
    """给后台线程每轮调一次。到点了就导出一份，并只保留最近 30 份"""
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass
    due, next_ts, desc = auto_export_due()
    if not due:
        return {"ok": True, "did": False, "next_ts": next_ts, "desc": desc}
    res = export_to_file(mem=mem)
    if res.get("ok"):
        st = _read_state()
        st["last_ts"] = time.time()
        st["last_path"] = res.get("path")
        st["desc"] = desc
        _write_state(st)
        _prune(keep=30)
        _log(f"[记忆备份] 自动导出完成：{res.get('msg')}（周期 {desc}）")
    else:
        _log(f"[记忆备份] 自动导出失败：{res.get('msg')}")
    return {"ok": res.get("ok"), "did": True, "msg": res.get("msg"),
            "path": res.get("path"), "desc": desc}


# ==================== 日记自动备份（和记忆备份同一套单位换算） ====================

DIARY_DIR = "diary_backup"


def diary_dir():
    """日记备份目录：设置了就用设置里的，没设置就用 data/diary_backup"""
    custom = str(cfg_mod.get("diary_export_dir") or "").strip()
    d = custom or os.path.join(data_dir(), DIARY_DIR)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def export_diary(mem=None, path=""):
    """只把 diary 表导成一份 JSON（和整库备份同格式，可以互相导入）"""
    if not path:
        path = os.path.join(diary_dir(), "日记备份_" + time.strftime("%Y%m%d_%H%M%S") + ".json")
    return export_to_file(mem=mem, path=path, tables=["diary"])


def list_diary_backups(limit=100):
    d = diary_dir()
    out = []
    try:
        names = [n for n in os.listdir(d) if n.lower().endswith((".json", ".jsonl"))]
    except Exception:
        return out
    for n in names:
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except Exception:
            continue
        out.append({"name": n, "path": p, "size": st.st_size,
                    "mtime": st.st_mtime,
                    "mtime_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    out.sort(key=lambda x: -x["mtime"])
    return out[:int(limit)]


def diary_export_due():
    """返回 (due, 下次时间戳, 说明)"""
    cfg = cfg_mod.load()
    if not bool(cfg.get("diary_export_enabled", False)):
        return False, 0, "未开启"
    every = int(_ndef(cfg.get("diary_export_every"), 1))
    unit = str(cfg.get("diary_export_unit") or "day")
    secs, desc = cfg_mod.export_interval_seconds(every, unit)
    if secs <= 0:
        return False, 0, "未开启"
    st = _read_state()
    last = float(st.get("diary_last_ts") or 0)
    now = time.time()
    return (now - last) >= secs, (last + secs), desc


def diary_export_tick(mem=None, log=None):
    """后台每轮调一次：到点就把日记导一份到备份目录"""
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass
    due, next_ts, desc = diary_export_due()
    if not due:
        return {"ok": True, "did": False, "next_ts": next_ts, "desc": desc}
    res = export_diary(mem=mem)
    if res.get("ok"):
        st = _read_state()
        st["diary_last_ts"] = time.time()
        st["diary_last_path"] = res.get("path")
        st["diary_desc"] = desc
        _write_state(st)
        _prune_diary(keep=60)
        _log(f"[日记备份] 自动备份完成：{res.get('msg')}（周期 {desc}）")
    else:
        _log(f"[日记备份] 自动备份失败：{res.get('msg')}")
    return {"ok": res.get("ok"), "did": True, "msg": res.get("msg"), "desc": desc}


def _prune_diary(keep=60):
    for x in list_diary_backups(limit=10 ** 6)[int(keep):]:
        try:
            os.remove(x["path"])
        except Exception:
            pass


def import_diary_from_dir(mem, log=None):
    """把备份文件夹里的日记文件全部合并导入（按 user_id+day 去重）"""
    def _log(msg):
        if log:
            try:
                log(msg)
            except Exception:
                pass
    d = diary_dir()
    files = [x for x in list_diary_backups(limit=10 ** 6)]
    files.sort(key=lambda x: x["mtime"])
    ok = bad = 0
    items = []
    for f in files:
        r = import_from_file(mem, f["path"], mode="merge")
        if r.get("ok"):
            ok += 1
            items.append({"file": f["name"], "ok": True, "msg": r.get("msg")})
        else:
            bad += 1
            items.append({"file": f["name"], "ok": False, "msg": r.get("msg")})
    if ok:
        _log(f"[日记备份] 从 {os.path.basename(d)} 导入了 {ok} 个文件")
    return {"ok": True, "dir": d, "total": len(files), "ok_count": ok,
            "bad_count": bad, "items": items,
            "msg": f"共 {len(files)} 个文件，成功 {ok}，失败 {bad}"}


def diary_backup_status():
    """给界面用：开关/间隔/目录/下次时间/已有几份"""
    cfg = cfg_mod.load()
    enabled = bool(cfg.get("diary_export_enabled", False))
    every = int(_ndef(cfg.get("diary_export_every"), 1))
    unit = str(cfg.get("diary_export_unit") or "day")
    secs, desc = cfg_mod.export_interval_seconds(every, unit)
    due, next_ts, _d = diary_export_due()
    return {
        "enabled": enabled, "every": every, "unit": unit,
        "unit_cn": (cfg_mod.EXPORT_UNIT_CN or {}).get(unit, unit),
        "dir": diary_dir(), "seconds": secs, "desc": desc,
        "next_ts": next_ts if enabled else 0,
        "due": due,
        "files": len(list_diary_backups(limit=10 ** 6)),
    }


def _prune(keep=30):
    """自动导出的文件别无限涨"""
    files = [x for x in list_exports() if x["name"].startswith("记忆备份_")]
    for x in files[keep:]:
        try:
            os.remove(x["path"])
        except Exception:
            pass


def mask_preview(path):
    """列表里显示的脱敏摘要（内容不外露）"""
    r = preview_bundle(path)
    if not r.get("ok"):
        return r.get("msg") or "无法读取"
    c = r.get("counts") or {}
    top = sorted(c.items(), key=lambda kv: -kv[1])[:3]
    return " · ".join(f"{k} {v}" for k, v in top) or "空"


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("导出目录:", export_dir())
    r = export_to_file()
    print("导出:", r.get("msg"))
    print("最近文件:", [x["name"] for x in list_exports()[:3]])
    print("到期检查:", auto_export_due())
