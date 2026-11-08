# -*- coding: utf-8 -*-
"""记忆重编译 —— 把旧格式/旧数据重新整理成当前结构

为什么需要这个模块：
    记忆的存储结构一直在变：
      v1  只有 memories(scope, person, fact) 一张"事实"表
      v4  新增 perm_memory + diary（绑定账号才写永久记忆）
      v5  perm_memory 加 status / supersedes / norm_key，开始支持"新事实顶掉旧事实"

    老用户的库里因此会留下三类问题，靠日常代码修不了，必须专门跑一遍：
      1. norm_key 是 NULL —— v5 之前写入的条目没有归一化键，冲突检测对它们失效，
         所以用户cǎi "改了纪念日但旧记录还在" 的时候，历史数据永远修不好。
      2. 同一件事有两条以上"都活着"的重复 —— 旧版本没有 supersede 机制。
      3. memories 表里的老事实从没进过 perm_memory —— 老用户绑定账号后发现
         "永久记忆是空的"，但 memory.db 里明明有数据。

    这个模块就是干这三件事，全部可预览（dry_run）后再真改。

安全设计：
    * 默认 dry_run=True，界面上先点「预览」看会改什么，再点「执行」
    * 真执行前自动把 memory.db 复制一份到 memory_backup/（可回滚）
    * 只做"补键 / 标废弃 / 搬数据"，不删除任何行 —— superseded 只是打个标记
"""
import os
import shutil
import sys
import time

# ==================== 预览 ====================
def analyze(mem):
    """扫描数据库，报告需要重编译的东西。不改任何数据。

    返回 {
      ok, perm_total, perm_missing_norm, perm_dup_groups, perm_dup_extra,
      legacy_facts, legacy_bound_facts, users, msg, samples
    }
    """
    out = {
        "ok": True,
        "perm_total": 0,
        "perm_missing_norm": 0,
        "perm_dup_groups": 0,
        "perm_dup_extra": 0,
        "legacy_facts": 0,
        "legacy_bound_facts": 0,
        "users": [],
        "samples": [],
        "msg": "",
    }
    try:
        import diary as diary_mod
        bound = set(diary_mod.bound_accounts())
    except Exception:
        bound = set()

    try:
        with mem.lock:
            c = mem.conn.cursor()
            # ① 永久记忆总数 / 缺 norm_key 的
            c.execute("SELECT COUNT(*) FROM perm_memory")
            out["perm_total"] = int(c.fetchone()[0] or 0)
            try:
                c.execute("SELECT COUNT(*) FROM perm_memory "
                          "WHERE norm_key IS NULL OR norm_key=''")
                out["perm_missing_norm"] = int(c.fetchone()[0] or 0)
            except Exception:
                out["perm_missing_norm"] = out["perm_total"]

            # ② 每个用户的重复组（含 norm_key 为空的老数据）
            try:
                c.execute("""SELECT id, user_id, content, norm_key, status
                             FROM perm_memory WHERE status='active'""")
                rows = c.fetchall()
                groups = {}
                for pid, uid, content, nk, _st in rows:
                    key = str(nk or "").strip() or mem.norm_fact(content)
                    if not key:
                        continue
                    groups.setdefault((str(uid), key), []).append(int(pid))
                for (uid, nk), ids in sorted(groups.items(),
                                             key=lambda kv: -len(kv[1])):
                    if len(ids) < 2:
                        continue
                    out["perm_dup_groups"] += 1
                    out["perm_dup_extra"] += len(ids) - 1
                    if len(out["samples"]) < 8:
                        out["samples"].append({
                            "user_id": uid, "norm_key": nk,
                            "count": len(ids), "ids": sorted(ids),
                        })
            except Exception:
                pass

            # ③ 老 memories 表
            try:
                c.execute("SELECT scope, COUNT(*) FROM memories GROUP BY scope")
                rows = c.fetchall()
                out["legacy_facts"] = sum(int(n or 0) for _, n in rows)
                uid_map = {}
                for scope, n in rows:
                    uid = _scope_to_uid(scope)
                    if not uid:
                        continue
                    uid_map[uid] = uid_map.get(uid, 0) + int(n or 0)
                out["users"] = [{"user_id": u, "legacy_facts": n,
                                 "bound": u in bound}
                                for u, n in sorted(uid_map.items())]
                out["legacy_bound_facts"] = sum(
                    u["legacy_facts"] for u in out["users"] if u["bound"])
            except Exception:
                pass
    except Exception as e:
        return {"ok": False, "msg": f"扫描失败：{type(e).__name__}: {e}", **out}

    parts = []
    if out["perm_missing_norm"]:
        parts.append(f"{out['perm_missing_norm']}/{out['perm_total']} 条永久记忆缺归一化键")
    if out["perm_dup_groups"]:
        parts.append(f"{out['perm_dup_groups']} 组重复（多的 {out['perm_dup_extra']} 条该标废弃）")
    if out["legacy_bound_facts"]:
        parts.append(f"{out['legacy_bound_facts']} 条老事实可以搬进永久记忆")
    if not parts:
        out["msg"] = "记忆结构已经是最新的，不需要重编译"
    else:
        out["msg"] = "；".join(parts)
    return out


def _scope_to_uid(scope):
    """memories.scope 形如 'private_3280735835' / 'group_274632022' / 'R'
    只有私聊的 scope 能映射回 QQ 号（永久记忆是账号级的）。"""
    s = str(scope or "")
    if s.startswith("private_"):
        rest = s[len("private_"):].strip()
        return rest if rest.isdigit() else ""
    return ""


# ==================== 执行 ====================
def rebuild(mem, dry_run=True, db_path=None, fill_norm=True,
            dedupe=True, migrate_legacy=True):
    """执行重编译。

    参数：
      dry_run          True 只报告不改（默认）
      fill_norm        给缺 norm_key 的条目补键
      dedupe           把同组重复里"旧的"标成 superseded（id 最大的那条留 active）
      migrate_legacy   把 memories 表里私聊账号的老事实搬进 perm_memory

    返回 {"ok","dry_run","filled","deduped","migrated","backup","msg","details"}
    """
    try:
        import diary as diary_mod
        bound = set(diary_mod.bound_accounts())
    except Exception:
        bound = set()

    res = {"ok": True, "dry_run": bool(dry_run), "filled": 0, "deduped": 0,
           "migrated": 0, "backup": "", "msg": "", "details": []}

    # ① 先备份（真执行才备份）
    if not dry_run:
        try:
            res["backup"] = backup_db(mem, db_path)
        except Exception as e:
            return {"ok": False, "msg": f"备份失败，已中止（不会改数据）：{e}", **res}

    # ② 补 norm_key
    if fill_norm:
        res["filled"] = _fill_norm_keys(mem, dry_run)
        if res["filled"]:
            res["details"].append(f"补归一化键 {res['filled']} 条")

    # ③ 去重：同 user_id + norm_key，保留 id 最大的，其余标 superseded
    if dedupe:
        res["deduped"] = _dedupe(mem, dry_run)
        if res["deduped"]:
            res["details"].append(f"标废弃重复 {res['deduped']} 条")

    # ④ 搬老 memories → perm_memory（只搬绑定账号）
    if migrate_legacy:
        res["migrated"] = _migrate_legacy(mem, bound, dry_run)
        if res["migrated"]:
            res["details"].append(f"从老表搬入永久记忆 {res['migrated']} 条")

    if not res["details"]:
        res["msg"] = "没有需要重编译的内容"
    else:
        res["msg"] = ("预览（未改动）：" if dry_run else "已完成：") + "；".join(res["details"])
        if res["backup"]:
            res["msg"] += f"（备份：{os.path.basename(res['backup'])}）"
    return res


def _fill_norm_keys(mem, dry_run):
    n = 0
    with mem.lock:
        c = mem.conn.cursor()
        try:
            c.execute("""SELECT id, content FROM perm_memory
                         WHERE norm_key IS NULL OR norm_key=''""")
            rows = c.fetchall()
        except Exception:
            return 0
        for pid, content in rows:
            nk = mem.norm_fact(content)
            n += 1
            if dry_run:
                continue
            try:
                c.execute("UPDATE perm_memory SET norm_key=?, updated_at=? WHERE id=?",
                          (nk, time.time(), pid))
            except Exception:
                pass
        if not dry_run:
            mem.conn.commit()
    return n


def _dedupe(mem, dry_run):
    """同 user_id + 归一化键 且都 active 的，保留 id 最大那条。

    关键：不能只按 norm_key 列分组 —— 旧数据的 norm_key 是 NULL，
    那样"补键"和"去重"就得跑两遍才见效，预览时还会报 0（用户以为没重复）。
    这里把 norm_key 为空的现场算一遍，预览和执行看到的是同一份结果。
    """
    rows = []
    with mem.lock:
        c = mem.conn.cursor()
        try:
            c.execute("""SELECT id, user_id, content, norm_key
                         FROM perm_memory WHERE status='active'""")
            rows = c.fetchall()
        except Exception:
            return 0

    groups = {}   # (user_id, key) -> [ids...]
    for pid, uid, content, nk in rows:
        key = str(nk or "").strip() or mem.norm_fact(content)
        if not key:
            continue
        groups.setdefault((str(uid), key), []).append(int(pid))

    n = 0
    with mem.lock:
        c = mem.conn.cursor()
        for (_uid, _key), ids in groups.items():
            if len(ids) < 2:
                continue
            ids.sort()
            keep_id = ids[-1]
            for old_id in ids[:-1]:
                n += 1
                if dry_run:
                    continue
                try:
                    c.execute("""UPDATE perm_memory
                                 SET status='superseded', supersedes=?, updated_at=?
                                 WHERE id=?""", (keep_id, time.time(), old_id))
                except Exception:
                    pass
        if not dry_run:
            mem.conn.commit()
    return n


def _migrate_legacy(mem, bound, dry_run):
    """把 memories 表里私聊账号的事实搬进 perm_memory。

    只搬绑定账号 —— 没绑定的账号按设计就不该有永久记忆。
    """
    if not bound:
        return 0
    n = 0
    with mem.lock:
        c = mem.conn.cursor()
        try:
            c.execute("""SELECT scope, person, fact FROM memories
                         WHERE scope LIKE 'private_%' ORDER BY id ASC""")
            rows = c.fetchall()
        except Exception:
            return 0
    for scope, person, fact in rows:
        uid = _scope_to_uid(scope)
        if not uid or uid not in bound:
            continue
        content = str(fact or "").strip()
        if len(content) < 2:
            continue
        if dry_run:
            n += 1
            continue
        try:
            added_id, is_new, _sup = mem.add_perm(
                uid, content, category="other", importance=3,
                source=f"legacy:{person or ''}", replace_conflict=True)
            if is_new:
                n += 1
        except Exception:
            pass
    return n


# ==================== 备份与回滚 ====================
def backup_db(mem, db_path=None):
    """把 memory.db 复制到 memory_backup/，返回备份路径。"""
    try:
        import config as cfg_mod
        src = db_path or getattr(mem, "db_path", "") or ""
        if not src or not os.path.exists(src):
            return ""
        folder = str(cfg_mod.get("memory_export_dir") or "memory_backup")
        outdir = folder if os.path.isabs(folder) else os.path.join(
            str(cfg_mod.DATA_DIR), folder)
        os.makedirs(outdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(outdir, f"memory_before_rebuild_{stamp}.db")
        # 用 SQLite 自己的 backup API 更安全（关掉 WAL 也能拿到一致快照）
        try:
            import sqlite3
            s = sqlite3.connect(src)
            d = sqlite3.connect(dst)
            with d:
                s.backup(d)
            s.close()
            d.close()
        except Exception:
            shutil.copy2(src, dst)
        return dst
    except Exception:
        return ""


def list_db_backups():
    """列出 memory_backup/ 下的 .db 备份"""
    try:
        import config as cfg_mod
        folder = str(cfg_mod.get("memory_export_dir") or "memory_backup")
        outdir = folder if os.path.isabs(folder) else os.path.join(
            str(cfg_mod.DATA_DIR), folder)
        if not os.path.isdir(outdir):
            return []
        out = []
        for name in os.listdir(outdir):
            if not name.lower().endswith(".db"):
                continue
            p = os.path.join(outdir, name)
            try:
                st = os.stat(p)
                out.append({"name": name, "path": p, "size": st.st_size,
                            "mtime": st.st_mtime,
                            "mtime_text": time.strftime("%Y-%m-%d %H:%M",
                                                        time.localtime(st.st_mtime))})
            except OSError:
                continue
        out.sort(key=lambda x: -x["mtime"])
        return out
    except Exception:
        return []


def restore_backup(name, mem=None):
    """把一个备份覆盖回 memory.db。

    安全做法：先把当前库再存一份 `.pre_restore_<时间>`，覆盖失败还能退回。
    返回 {"ok","msg","restored","safety"}
    """
    try:
        import sqlite3

        import config as cfg_mod
        folder = str(cfg_mod.get("memory_export_dir") or "memory_backup")
        outdir = folder if os.path.isabs(folder) else os.path.join(
            str(cfg_mod.DATA_DIR), folder)
        src = name if os.path.isabs(name) else os.path.join(outdir, str(name or ""))
        if not os.path.exists(src):
            return {"ok": False, "msg": "备份文件不存在"}
        db = getattr(mem, "db_path", "") or ""
        if not db:
            return {"ok": False, "msg": "找不到当前数据库路径"}
        # 1) 先给当前库买保险
        safety = os.path.join(outdir, "memory_before_restore_" +
                              time.strftime("%Y%m%d_%H%M%S") + ".db")
        try:
            os.makedirs(outdir, exist_ok=True)
            s = sqlite3.connect(db)
            d = sqlite3.connect(safety)
            with d:
                s.backup(d)
            s.close()
            d.close()
        except Exception:
            try:
                shutil.copy2(db, safety)
            except Exception:
                safety = ""
        # 2) 用备份覆盖（同样走 SQLite backup API，避免留下半截文件）
        try:
            s = sqlite3.connect(src)
            d = sqlite3.connect(db)
            with d:
                s.backup(d)
            s.close()
            d.close()
        except Exception as e:
            return {"ok": False, "msg": f"恢复失败：{type(e).__name__}: {e}",
                    "safety": safety}
        return {"ok": True,
                "msg": f"已恢复 {os.path.basename(src)}（原库另存为 {os.path.basename(safety)}）",
                "restored": os.path.basename(src), "safety": safety}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}"}


def verify(mem):
    """一致性体检：把"看起来不对但不会崩"的数据找出来。

    查五样：
      1. 没有 session_key 的消息（老版本遗留）
      2. 长期记忆里指向不存在的账号（孤儿）
      3. 同一账号里 spec 完全重复的长期记忆
      4. 到点了但还没处理的事件（status=pending 且 target_time 已过很久）
      5. 只有 assistant 没有 user 的会话（模型自言自语）
    """
    out = {"ok": True, "items": [], "total": 0}
    try:
        with mem.lock:
            c = mem.conn.cursor()

            def _q(sql, args=()):
                try:
                    return list(c.execute(sql, args))
                except Exception:
                    return []

            checks = []
            r1 = _q("SELECT COUNT(*) FROM messages WHERE session_key IS NULL OR session_key=''")
            if r1 and r1[0][0]:
                checks.append(("消息没有会话键", int(r1[0][0]),
                               "这些消息永远不会被任何会话读到（可以清掉）"))
            r2 = _q("SELECT COUNT(*) FROM perm_memory WHERE user_id IS NULL OR user_id=''")
            if r2 and r2[0][0]:
                checks.append(("长期记忆没有归属账号", int(r2[0][0]),
                               "导入/迁移时可能丢了 user_id"))
            r3 = _q("SELECT spec, COUNT(*) n FROM perm_memory WHERE status='active' "
                    "GROUP BY user_id, spec HAVING n>1 LIMIT 20")
            if r3:
                checks.append(("长期记忆有重复事实", len(r3),
                               "同一个人重复记住了同一件事，可用「重编译」去重"))
            r4 = _q("SELECT COUNT(*) FROM user_events WHERE status='pending' "
                    "AND target_time < ?", (time.time() - 7 * 86400,))
            if r4 and r4[0][0]:
                checks.append(("超过 7 天没处理的旧事件", int(r4[0][0]),
                               "多半是当时 Agent 没在跑，可以标成 done 或删掉"))
            r5 = _q("SELECT session_key, SUM(role='user') u, SUM(role='assistant') a "
                    "FROM messages GROUP BY session_key HAVING u=0 AND a>0 LIMIT 20")
            if r5:
                checks.append(("只有模型自言自语的会话", len(r5),
                               "这些会话里没有用户消息，可能是旧 bug 留下的"))
            out["items"] = [{"name": n, "count": k, "hint": h} for n, k, h in checks]
            out["total"] = sum(x["count"] for x in out["items"])
            out["msg"] = ("没发现问题，数据很干净" if not out["items"]
                          else f"发现 {len(out['items'])} 类问题，共 {out['total']} 处")
        return out
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "items": [], "total": 0}


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    from memory import Memory
    m = Memory()
    print("=== 预览 ===")
    import json
    print(json.dumps(analyze(m), ensure_ascii=False, indent=2))
    print("=== 试运行 ===")
    print(json.dumps(rebuild(m, dry_run=True), ensure_ascii=False, indent=2))
    m.close()
