# -*- coding: utf-8 -*-
"""用量统计与保护 —— 防止云端 API 账单失控

为什么必须有这个：
    接云端 API 后，一次"全重试失败"可能连发 4 个请求；
    后台还有补回、说说、关怀、记忆提炼、日记 5 条线程在调模型。
    不设闸门的话，模型出错时会被重试逻辑放大成成百上千次调用。

三层保护：
    1. 单次调用前 precheck() —— 日用量超限直接拒绝，不发请求
    2. 每次调用后 record() —— 把 token 数记进当日账本（本地 SQLite）
    3. 本地模型也记，但默认不计入额度（免费，只用来做统计对比）

账本存在 memory.db 的 usage_log 表，跨重启累积。
"""
import datetime
import os
import threading
import time

import config as cfg_mod


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

# 用途中文名（界面「按用途」和「明细」两处都用这张表）
KIND_CN = {
    "chat": "聊天回复",
    "memory": "记忆提炼",
    "moment": "发说说",
    "diary": "写日记",
    "catchup": "离线补回",
    "summary": "滚动摘要",
    "image": "生图",
    "plan": "主动关心",
    "comment": "评论回复",
    "extract": "事件提取",
    "diary_extract": "日记提炼",
    "other": "其他",
}


def kind_cn(kind):
    return KIND_CN.get(str(kind or "").strip().lower(), str(kind or "其他"))


def _today():
    return datetime.date.today().strftime("%Y-%m-%d")


def _ensure_table(mem):
    """usage_log 表（跟着 memory.db 走，卸载时跟着保留）"""
    with mem.lock:
        c = mem.conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS usage_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL,
            ts REAL NOT NULL,
            provider TEXT,
            model TEXT,
            kind TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            is_cloud INTEGER DEFAULT 0,
            ok INTEGER DEFAULT 1,
            detail TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_log(day, is_cloud)")
        # v6：老库补 detail 列（这一笔是干什么花掉的）
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(usage_log)")]
            if cols and "detail" not in cols:
                c.execute("ALTER TABLE usage_log ADD COLUMN detail TEXT")
        except Exception:
            pass
        mem.conn.commit()


def _pick_tokens(u, names):
    """从一个 usage 字典里按别名顺序取整数（v1.1.7 / P0-3）。

    为什么需要：OpenAI 官方字段是 prompt_tokens / completion_tokens，
    但不少网关（含 QA 报告里那家）返回的是 input_tokens / output_tokens，
    有的 Gemini/Anthropic 兼容层还叫 promptTokenCount / candidatesTokenCount。
    老代码只认 prompt_tokens → 云端用量永远记 0 → 20 万 token 的日上限等于没有。
    """
    if not isinstance(u, dict):
        return 0
    for n in names:
        if n in u:
            try:
                v = int(float(u.get(n) or 0))
            except Exception:
                continue
            if v > 0:
                return v
    return 0


_PROMPT_KEYS = ("prompt_tokens", "input_tokens", "promptTokenCount",
                "prompt_token_count", "inputTokenCount", "prompt_eval_count")
_COMPLETION_KEYS = ("completion_tokens", "output_tokens", "candidatesTokenCount",
                    "completion_token_count", "outputTokenCount", "eval_count")
_TOTAL_KEYS = ("total_tokens", "totalTokenCount", "total_token_count")


def record(mem, provider, model, usage, kind="chat", is_cloud=False, ok=True,
           detail=""):
    """记一笔用量。usage 是 OpenAI 格式的 usage 字典（可能为空）。

    detail 是这句人话："私聊回复 安瑆.雨" / "日记 2026-10-22" / "说说"，
    给界面「最近明细」用 —— 光看 kind 只能知道"发了说说"，
    看 detail 才知道"发的哪条、回的是谁"。

    返回本笔实际计入的 total_tokens（没有 usage 时按 0 记，仍留一条痕迹）。
    """
    if mem is None:
        return 0
    try:
        _ensure_table(mem)
        u = usage or {}
        pt = _pick_tokens(u, _PROMPT_KEYS)
        ct = _pick_tokens(u, _COMPLETION_KEYS)
        tt = _pick_tokens(u, _TOTAL_KEYS) or (pt + ct)

        with mem.lock:
            c = mem.conn.cursor()
            c.execute("""INSERT INTO usage_log
                         (day, ts, provider, model, kind, prompt_tokens,
                          completion_tokens, total_tokens, is_cloud, ok, detail)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (_today(), time.time(), str(provider or ""), str(model or ""),
                       str(kind or "chat"), pt, ct, tt, 1 if is_cloud else 0,
                       1 if ok else 0, str(detail or "")[:200]))
            mem.conn.commit()
        return tt
    except Exception:
        return 0


def set_detail(mem, kind, detail):
    """给今天最近一条某用途的记录补上明细（生图这类先写后补的场景用）。"""
    if mem is None:
        return False
    try:
        _ensure_table(mem)
        with mem.lock:
            c = mem.conn.cursor()
            c.execute("""UPDATE usage_log SET detail=?
                         WHERE id = (SELECT id FROM usage_log
                                     WHERE day=? AND kind=?
                                     ORDER BY id DESC LIMIT 1)""",
                      (str(detail or "")[:200], _today(), str(kind or "")))
            mem.conn.commit()
            return c.rowcount > 0
    except Exception:
        return False



def today_usage(mem, is_cloud=None):
    """今日汇总：{day, calls, prompt, completion, total}"""
    out = {"day": _today(), "calls": 0, "prompt": 0, "completion": 0, "total": 0}
    if mem is None:
        return out
    try:
        _ensure_table(mem)
        with mem.lock:
            c = mem.conn.cursor()
            sql = ("SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), "
                   "COALESCE(SUM(completion_tokens),0), COALESCE(SUM(total_tokens),0) "
                   "FROM usage_log WHERE day=?")
            args = [_today()]
            if is_cloud is not None:
                sql += " AND is_cloud=?"
                args.append(1 if is_cloud else 0)
            c.execute(sql, args)
            row = c.fetchone()
        if row:
            out.update(calls=int(row[0] or 0), prompt=int(row[1] or 0),
                       completion=int(row[2] or 0), total=int(row[3] or 0))
    except Exception:
        pass
    return out


def recent_days(mem, days=7):
    """最近 N 天的按天汇总（含云端/本地拆分），给看板画柱状图"""
    out = []
    if mem is None:
        return out
    try:
        _ensure_table(mem)
        with mem.lock:
            c = mem.conn.cursor()
            c.execute("""SELECT day, is_cloud, COUNT(*), COALESCE(SUM(total_tokens),0)
                         FROM usage_log
                         WHERE day >= date('now', ?)
                         GROUP BY day, is_cloud ORDER BY day ASC""",
                      (f"-{int(days) - 1} days",))
            rows = c.fetchall()
    except Exception:
        return out
    bucket = {}
    for day, is_cloud, n, tt in rows:
        b = bucket.setdefault(str(day), {"day": str(day), "cloud": 0,
                                         "local": 0, "cloud_calls": 0,
                                         "local_calls": 0})
        if int(is_cloud or 0):
            b["cloud"] = int(tt or 0)
            b["cloud_calls"] = int(n or 0)
        else:
            b["local"] = int(tt or 0)
            b["local_calls"] = int(n or 0)
    for d in sorted(bucket):
        out.append(bucket[d])
    return out


def by_kind(mem, day=None):
    """今日按用途拆分（聊天/说说/记忆/日记/补回…），看哪块最费"""
    out = []
    if mem is None:
        return out
    try:
        _ensure_table(mem)
        with mem.lock:
            c = mem.conn.cursor()
            c.execute("""SELECT kind, COUNT(*), COALESCE(SUM(total_tokens),0)
                         FROM usage_log WHERE day=?
                         GROUP BY kind ORDER BY 3 DESC""", (day or _today(),))
            for kind, n, tt in c.fetchall():
                k = str(kind or "chat")
                out.append({"kind": k, "kind_cn": kind_cn(k),
                            "calls": int(n or 0), "tokens": int(tt or 0)})
    except Exception:
        pass
    return out


def recent_details(mem, limit=60, day=None, kind=None):
    """最近 N 条用量明细 —— 让用户看得懂"这些 token 花在哪了"。

    每行：{ts, time, kind, kind_cn, model, tokens, detail, ok, is_cloud}
    """
    out = []
    if mem is None:
        return out
    try:
        _ensure_table(mem)
        with mem.lock:
            c = mem.conn.cursor()
            sql = ("SELECT ts, kind, model, total_tokens, COALESCE(detail,''), "
                   "ok, is_cloud FROM usage_log WHERE 1=1")
            args = []
            if day:
                sql += " AND day=?"
                args.append(day)
            if kind:
                sql += " AND kind=?"
                args.append(kind)
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(int(limit))
            c.execute(sql, args)
            rows = c.fetchall()
    except Exception:
        return out
    for ts, kd, model, tt, detail, ok, is_cloud in rows:
        d = str(detail or "").strip()
        kcn = kind_cn(kd)
        out.append({
            "ts": float(ts or 0),
            "time": datetime.datetime.fromtimestamp(float(ts or 0)).strftime("%H:%M:%S"),
            "kind": str(kd or "chat"),
            "kind_cn": kcn,
            "model": str(model or ""),
            "tokens": int(tt or 0),
            "detail": d,
            # v1.1.0：老记录（v6 之前没有 detail 列）不再显示空白，
            # 用用途 + 通道兜一句人话，免得用户以为是 bug
            "detail_text": d or f"{kcn} · {'云端' if is_cloud else '本地'}（这笔没记细节）",
            "ok": bool(ok),
            "is_cloud": bool(is_cloud),
        })
    return out



def precheck(mem, is_cloud):
    """发请求前的闸门。

    返回 (allowed, msg)。
      本地模型：永远允许（免费，只记账）
      云端模型：日用量 >= 上限 且 保护开启 → 拒绝
    """
    if not is_cloud:
        return True, ""
    cfg = cfg_mod.load()
    if not bool(cfg.get("usage_guard_enabled", True)):
        return True, ""
    limit = int(cfg.get("usage_daily_limit") or 0)
    if limit <= 0:
        return True, ""
    used = today_usage(mem, is_cloud=True)["total"]
    if used >= limit:
        return False, (f"今日云端用量已达上限（{used:,} / {limit:,} tokens），"
                       f"为防账单失控已暂停调用。可在「设置 → 用量看板」里调高上限。")
    return True, ""


def warn_if_needed(mem, is_cloud):
    """用到 warn_percent% 时返回一句提醒（调用方决定 log 还是弹窗）"""
    if not is_cloud:
        return ""
    cfg = cfg_mod.load()
    limit = int(cfg.get("usage_daily_limit") or 0)
    if limit <= 0:
        return ""
    pct = int(_ndef(cfg.get("usage_warn_percent"), 80))
    used = today_usage(mem, is_cloud=True)["total"]
    ratio = used * 100.0 / limit
    if ratio >= pct:
        return (f"今日云端用量已用 {ratio:.0f}%（{used:,} / {limit:,} tokens），"
                f"注意账单。")
    return ""


def dashboard(mem):
    """给界面看板的完整数据（一次取完，避免前端多次调用）"""
    cfg = cfg_mod.load()
    limit = int(cfg.get("usage_daily_limit") or 0)
    cloud = today_usage(mem, is_cloud=True)
    local = today_usage(mem, is_cloud=False)
    pct = round(cloud["total"] * 100.0 / limit, 1) if limit > 0 else 0.0
    return {
        "ok": True,
        "day": _today(),
        "cloud": cloud,
        "local": local,
        "limit": limit,
        "percent": pct,
        "guard": bool(cfg.get("usage_guard_enabled", True)),
        "warn_percent": int(_ndef(cfg.get("usage_warn_percent"), 80)),
        "days": recent_days(mem, 7),
        "kinds": by_kind(mem),
        "details": recent_details(mem, limit=40),
        "kinds_all": len(KIND_CN),
        "msg": (f"今日云端 {cloud['total']:,} tokens（{pct}% of {limit:,}）"
                if limit > 0 else f"今日云端 {cloud['total']:,} tokens（未设上限）"),
    }


def clear_today(mem):
    """清空今日账本（用户手动重置，比如换了 key 重新计数）"""
    if mem is None:
        return 0
    try:
        _ensure_table(mem)
        with mem.lock:
            c = mem.conn.cursor()
            c.execute("DELETE FROM usage_log WHERE day=?", (_today(),))
            mem.conn.commit()
            return c.rowcount
    except Exception:
        return 0


def delete_by_detail(mem, text):
    """按「这一笔做了什么」里的关键字删记录（v1.1.1：清理检测/测试留下的账）。

    只删 detail 里含该关键字的行，正常聊天账一条都不动。
    """
    if mem is None:
        return 0
    try:
        _ensure_table(mem)
        with mem.lock:
            c = mem.conn.cursor()
            c.execute("DELETE FROM usage_log WHERE detail LIKE ?", ("%" + str(text) + "%",))
            mem.conn.commit()
            return c.rowcount
    except Exception:
        return 0


# ==================== v1.1.0：用量报表导出 / 导入 ====================
# 为什么要有这个：账本只在本机 memory.db 里，换机器、换库、给客服看，
# 都需要一个能带走的文件。CSV 用 UTF-8 BOM（Excel 双击不乱码），
# TXT 是给人看的分组报表，JSON 是给程序再导入用的。
_EXPORT_COLS = ["day", "ts", "provider", "model", "kind", "kind_cn",
                "prompt_tokens", "completion_tokens", "total_tokens",
                "is_cloud", "ok", "detail"]


def export_report(mem, path, fmt="csv"):
    """导出全部用量。返回 {"ok","msg","count","path"}"""
    if mem is None:
        return {"ok": False, "msg": "数据库没打开"}
    fmt = str(fmt or "csv").strip().lower()
    if fmt not in ("csv", "txt", "json"):
        fmt = "csv"
    try:
        _ensure_table(mem)
        with mem.lock:
            rows = list(mem.conn.execute(
                "SELECT day, ts, provider, model, kind, prompt_tokens, completion_tokens,"
                " total_tokens, is_cloud, ok, detail FROM usage_log ORDER BY day, id"))
    except Exception as e:
        return {"ok": False, "msg": f"读账本失败：{type(e).__name__}: {e}"}
    if not rows:
        return {"ok": False, "msg": "账本是空的，没东西可导"}
    recs = []
    for r in rows:
        recs.append({
            "day": r[0] or "", "ts": float(r[1] or 0), "provider": r[2] or "",
            "model": r[3] or "", "kind": r[4] or "", "kind_cn": kind_cn(r[4] or ""),
            "prompt_tokens": int(r[5] or 0), "completion_tokens": int(r[6] or 0),
            "total_tokens": int(r[7] or 0), "is_cloud": int(r[8] or 0),
            "ok": int(r[9] if r[9] is not None else 1), "detail": r[10] or "",
        })
    try:
        if fmt == "csv":
            import csv
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=_EXPORT_COLS)
                w.writeheader()
                for x in recs:
                    w.writerow({k: x.get(k, "") for k in _EXPORT_COLS})
        elif fmt == "json":
            import json as _json
            with open(path, "w", encoding="utf-8") as f:
                _json.dump({"format": "xiaojingyu-usage", "format_version": 1,
                            "exported_at": datetime.datetime.now().isoformat(" ", "seconds"),
                            "count": len(recs), "items": recs},
                           f, ensure_ascii=False, indent=2)
        else:
            by_day = {}
            for x in recs:
                by_day.setdefault(x["day"], []).append(x)
            lines = ["小鲸鱼 Agent —— 用量报表",
                     "导出时间：" + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     f"共 {len(recs)} 笔", ""]
            for day in sorted(by_day):
                items = by_day[day]
                tot = sum(x["total_tokens"] for x in items)
                cl = sum(x["total_tokens"] for x in items if x["is_cloud"])
                lines.append(f"== {day} ==  合计 {tot:,} tokens（云端 {cl:,}）")
                kinds = {}
                for x in items:
                    kinds[x["kind_cn"]] = kinds.get(x["kind_cn"], 0) + x["total_tokens"]
                for k, v in sorted(kinds.items(), key=lambda t: -t[1]):
                    lines.append(f"   {k}：{v:,}")
                for x in items:
                    t = datetime.datetime.fromtimestamp(x["ts"]).strftime("%H:%M:%S")
                    lines.append(f"     {t}  {x['kind_cn']:<6} {x['model'] or '-':<24} "
                                 f"{x['total_tokens']:>7}  {x['detail']}")
                lines.append("")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
    except Exception as e:
        return {"ok": False, "msg": f"写文件失败：{type(e).__name__}: {e}"}
    return {"ok": True, "msg": f"已导出 {len(recs)} 笔用量到 {os.path.basename(path)}",
            "count": len(recs), "path": path, "format": fmt}


def import_report(mem, path):
    """从导出的 csv/json 再导回来（按 建议去重：同 day+ts+model+kind+total 视为同一笔）"""
    if mem is None:
        return {"ok": False, "msg": "数据库没打开"}
    if not path or not os.path.exists(path):
        return {"ok": False, "msg": "文件不存在"}
    recs = []
    try:
        if path.lower().endswith(".json"):
            import json as _json
            with open(path, "r", encoding="utf-8-sig") as f:
                data = _json.load(f)
            if isinstance(data, dict):
                recs = data.get("items") or []
            elif isinstance(data, list):
                recs = data
        else:
            import csv
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                recs = list(csv.DictReader(f))
    except Exception as e:
        return {"ok": False, "msg": f"读文件失败：{type(e).__name__}: {e}"}
    if not recs:
        return {"ok": False, "msg": "文件里没有数据"}
    try:
        _ensure_table(mem)
    except Exception as e:
        return {"ok": False, "msg": f"建表失败：{e}"}
    ins = skip = bad = 0
    with mem.lock:
        c = mem.conn.cursor()
        for r in recs:
            try:
                day = str(r.get("day") or "")[:10]
                ts = float(r.get("ts") or 0)
                if not day or ts <= 0:
                    bad += 1
                    continue
                provider = str(r.get("provider") or "")[:40]
                model = str(r.get("model") or "")[:80]
                kind = str(r.get("kind") or "chat")[:24]
                total = max(0, int(float(r.get("total_tokens") or 0)))
                dup = c.execute(
                    "SELECT 1 FROM usage_log WHERE day=? AND ABS(ts-?)<1 AND model=? "
                    "AND kind=? AND total_tokens=? LIMIT 1",
                    (day, ts, model, kind, total)).fetchone()
                if dup:
                    skip += 1
                    continue
                c.execute(
                    "INSERT INTO usage_log(day, ts, provider, model, kind, prompt_tokens,"
                    " completion_tokens, total_tokens, is_cloud, ok, detail)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (day, ts, provider, model, kind,
                     max(0, int(float(r.get("prompt_tokens") or 0))),
                     max(0, int(float(r.get("completion_tokens") or 0))),
                     total,
                     1 if str(r.get("is_cloud") or "0") in ("1", "true", "True") else 0,
                     1 if str(r.get("ok") if r.get("ok") is not None else "1")
                     not in ("0", "false", "False") else 0,
                     str(r.get("detail") or "")[:200]))
                ins += 1
            except Exception:
                bad += 1
        mem.conn.commit()
    return {"ok": True,
            "msg": f"导入完成：新增 {ins} 笔，重复跳过 {skip} 笔"
                   + (f"，格式不对 {bad} 笔" if bad else ""),
            "inserted": ins, "skipped": skip, "bad": bad}

