# -*- coding: utf-8 -*-
"""时光胶囊（`.spark`）—— 把「她是谁」整个打包，换台电脑还能接着聊。

设计原则
--------
1. **本质是 ZIP**：后缀 `.spark`，里面全是明文 JSON + 文件，改名 `.zip` 就能打开看，
   以后想开源格式也不用解密。
2. **只装「人」不装「程序」**：人设卡、形象图、永久记忆/实体关系、潜意识反思、
   好感度与伴侣关系、TTS 与常用配置 —— 不装日志、不装缓存、不装模型文件。
3. **两种回家方式**：
   - `replace`（直接搬家）：先备份现库，再清空同名表写入胶囊内容；
   - `merge`（唤醒融合）：保留现有的一切，只把胶囊里没有的记忆追加进来。
4. **失败不炸**：所有入口都返回 dict + 人话 msg；损坏的胶囊只会得到
   「胶囊已损坏，解压失败」这种提示，主程序照常跑。

进度模型
--------
导出/导入都在后台线程跑，`_JOB` 里存 {state, pct, msg, result}，前端每 400ms 轮询
`spark_job()`。之所以不用 `/api/env/jobs`：那条链路是给「装环境」用的，
这里要额外回传路径和统计，单独一条更清楚。
"""
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile

FORMAT = "xiaojingyu-spark"
# v1.8.0：胶囊升到 v4 ——多了「关系网」（kg_entities / kg_relations两张表）。
#读 v1..v3的老胶囊照样能装：导入只认文件里真实存在的表，
#没有 kg_*就是空转，不报错（见 mem_transfer.import_data）。
FORMAT_VERSION = 4
# v1.9.4：旧版无口令胶囊的截止版本。到期之后 v2 只能"导入一次就强制重导出"，
# 再往后就不再支持无口令导入了。写进代码里而不是只在公告里喊，才不会忘。
V2_SUNSET_VERSION = "1.10.0"
MANIFEST = "manifest.json"
EXT = ".spark"

# 进胶囊的配置前缀（换电脑后「习惯」不该丢）
CONFIG_PREFIXES = ("tts_", "affinity_", "humanize_", "quote_",
                   "care_plan_", "memory_", "draw_", "moment_", "route_memo_",
                   "imagegen_", "web_search", "game_")
# 永远不装进胶囊的配置（跟这台机器绑定）
CONFIG_BLOCK = ("data_dir", "web_port", "codeword", "codeword_token",
                "provider_keys", "cloud_api_key", "imagegen_api_key")

_JOB = {"state": "idle", "pct": 0, "msg": "", "kind": "", "result": None,
        "started": 0, "finished": 0}
_JOB_LOCK = threading.Lock()


# ---------------------------------------------------------------- 基础工具

def data_dir():
    env = os.environ.get("QQ_AGENT_DATA_DIR")
    if env:
        try:
            os.makedirs(env, exist_ok=True)
            return env
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = data_dir()


def _cfg_mod():
    import config as m
    return m


def _mem_mod():
    import memory as m
    return m


def _job_set(**kw):
    with _JOB_LOCK:
        _JOB.update(kw)


def _job_begin(kind):
    _job_set(state="running", pct=0, msg="准备中…", kind=kind, result=None,
             started=time.time(), finished=0)


def _job_progress(pct, msg):
    p = max(0, min(100, int(pct)))
    _job_set(pct=p, msg=str(msg or ""))


def _job_done(result, kind=""):
    _job_set(state="done" if result.get("ok") else "error",
             pct=100 if result.get("ok") else _JOB.get("pct", 0),
             msg=str(result.get("msg") or ""), result=result,
             finished=time.time(), kind=kind or _JOB.get("kind", ""))


def job_status():
    """给前端轮询：现在到哪一步了 / 上次结果是什么。"""
    with _JOB_LOCK:
        d = dict(_JOB)
    d["running"] = d.get("state") == "running"
    return d


def _human_size(n):
    """字节数 → 人话（小于 1MB 显示 KB，别出现「0.0 MB」）。"""
    try:
        n = float(n or 0)
    except Exception:
        return "0 B"
    if n >= 1024 * 1024:
        return "%.2f MB" % (n / 1024.0 / 1024.0)
    if n >= 1024:
        return "%.0f KB" % (n / 1024.0)
    return "%d B" % int(n)


def _human(e):
    """把异常翻成人话（界面上直接显示这句）。

    v1.9.0：带「口令」的 SparkCryptoError 必须原样透出 —— 「没给口令 / 口令不对」
    和「文件被改过」是两件事，混成一句「胶囊已损坏」只会让用户白折腾。
    """
    s = "%s: %s" % (type(e).__name__, e)
    low = str(e).lower()
    if isinstance(e, zipfile.BadZipFile) or "not a zip file" in low:
        return "胶囊已损坏或不是本软件生成的（解不开）"
    if "SparkCryptoError" in s and "口令" in s:
        return str(e)
    if "SparkCryptoError" in s or "校验失败" in s or "文件头不对" in s:
        return "胶囊已损坏或不是本软件生成的（校验失败）"
    if isinstance(e, json.JSONDecodeError):
        return "胶囊里的 manifest.json 坏了，读不出元数据"
    if "no space" in low or "errno 28" in low:
        return "磁盘满了，写不进去"
    if "permission" in low or "errno 13" in low:
        return "没有权限写这个位置，换一个目录试试"
    return s


# ---------------------------------------------------------------- 统计

def _table_exists(conn, name):
    try:
        r = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                         (name,)).fetchone()
        return bool(r)
    except Exception:
        return False


def _count(conn, table, where=""):
    try:
        sql = "SELECT COUNT(*) FROM %s" % table
        if where:
            sql += " WHERE " + where
        return int((conn.execute(sql).fetchone() or [0])[0] or 0)
    except Exception:
        return 0


def collect_stats():
    """当前能打包多少东西 —— 界面上「包含 1240 条回忆、3 张形象图」就靠这个。"""
    out = {"ok": True, "version": "", "memory": {}, "personas": 0,
           "faces": 0, "config_keys": 0, "habit": 0, "desc": ""}
    try:
        cfg = _cfg_mod()
        out["version"] = str(getattr(cfg, "APP_VERSION", ""))
    except Exception:
        pass
    try:
        mem = _mem_mod().Memory()
        conn = mem.conn
        tables = {}
        for t in ("perm_memory", "messages", "chat_log", "summaries", "memories",
                  "affinity", "inner_thoughts", "think_log", "emotion_log",
                  "diary", "user_events", "care_plans"):
            if _table_exists(conn, t):
                tables[t] = _count(conn, t)
        out["memory"] = tables
        out["memories"] = int(tables.get("perm_memory", 0) + tables.get("memories", 0))
        out["affinity"] = int(tables.get("affinity", 0))
        out["thoughts"] = int(tables.get("inner_thoughts", 0))
        try:
            conn.close()
        except Exception:
            pass
    except Exception as e:
        out["ok"] = False
        out["msg"] = "读记忆库失败：%s" % _human(e)
    try:
        import personas_store as ps
        cards = ps.list_cards() or []
        out["personas"] = len(cards)
        faces = 0
        for c in cards:
            try:
                faces += len(ps.list_faces(c.get("key")) or [])
            except Exception:
                pass
        out["faces"] = faces
    except Exception:
        pass
    try:
        c = _cfg_mod().load()
        out["config_keys"] = len([k for k in c if _cfg_pick(k)])
        out["habit"] = out["config_keys"]
    except Exception:
        pass
    out["desc"] = ("包含 %d 条回忆、%d 张形象图、%d 项习惯"
                   % (out.get("memories", 0), out.get("faces", 0),
                      out.get("habit", 0)))
    return out


def _cfg_pick(k):
    k = str(k or "")
    if not k or k in CONFIG_BLOCK:
        return False
    # v1.5.1（P1-2）：Agent 的"动手"开关绝不随胶囊走 ——
    # 否则别人发来一个胶囊，用户点一下就静默开了键鼠/代码/联网。
    if k.startswith("agent_"):
        return False
    return any(k.startswith(p) for p in CONFIG_PREFIXES)


# v1.5.1：胶囊里出现这些键，导入时强制重置为关
AGENT_DANGER_KEYS = ("agent_enabled", "agent_keyboard", "agent_code",
                     "agent_web", "agent_auto")


def _allow_plain_capsule():
    """v1.5.2：是否允许导入「旧版未加密的明文 zip 胶囊」。

    默认关（安全）；用户在设置里打开 `spark_allow_plain` 或设了环境变量才放行，
    导入成功后会顺手把内容重新加密导出成新格式，不让老数据丢。
    """
    if os.environ.get("XJY_ALLOW_LEGACY_SPARK") == "1":
        return True
    try:
        import config
        return bool(config.get("spark_allow_plain", False))
    except Exception:
        return False


def needs_password(path):
    """这个胶囊要不要口令（v1.9.0 的 v3 格式要）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except Exception:
        return False
    try:
        import spark_crypto as sc
        return bool(sc.needs_password(head))
    except Exception:
        return False


def _open_capsule(path, info=None, password=None):
    """打开胶囊 → 内存里的 BytesIO。

    - v1.5.2 新格式（XJY_SPARK_V2）：按文件头里记录的 KDF 解密；
    - v1.5.1 旧格式（XJY_SPARK_V1，带机器特征）：也能解，info 里标 legacy；
    - v1.5.0 及更早的明文 zip：默认拒绝，开了 `spark_allow_plain` 才放行。
    """
    with open(path, "rb") as f:
        blob = f.read()
    try:
        import spark_crypto as sc
    except Exception:
        sc = None
    if sc is not None and sc.is_capsule(blob):
        data, meta = sc.decrypt(blob, want_info=True, password=password)
        if info is not None:
            info.update(meta)
        return io.BytesIO(data)
    if blob[:2] == b"PK":
        # 明文 zip 胶囊（v1.5.0 及更早）—— 谁能解压就能改，默认拒绝；
        # 老用户可以在设置里打开「允许导入旧版明文胶囊」把数据迁过来。
        if _allow_plain_capsule():
            if info is not None:
                info.update({"format": "plain", "legacy": True, "plain": True})
            return io.BytesIO(blob)
        raise zipfile.BadZipFile("这是旧版未加密的胶囊，已不再支持（可在设置里打开「允许导入旧版明文胶囊」）")
    raise zipfile.BadZipFile("不是本软件生成的胶囊")


# ---------------------------------------------------------------- 导出

def _write_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _collect(mem, workdir, progress=None):
    """把要装的东西摊到临时目录里，返回 counts。"""
    counts = {"personas": 0, "faces": 0, "tables": {}, "config": 0}

    def step(pct, msg):
        if progress:
            try:
                progress(pct, msg)
            except Exception:
                pass

    # 1) 记忆（表数据）
    step(8, "正在读取记忆库…")
    import mem_transfer
    data = mem_transfer.export_data(mem=mem)
    tables = data.get("tables") or {}
    _write_json(os.path.join(workdir, "memory.json"), data)
    counts["tables"] = {k: len(v) for k, v in tables.items()}

    # 2) 人设卡 + 形象图
    step(35, "正在打包人设卡…")
    try:
        import personas_store as ps
        cards = ps.list_cards() or []
        full_cards = []
        for c in cards:
            key = str(c.get("key") or "")
            try:
                full_cards.append(ps.get_card(key) or c)
            except Exception:
                full_cards.append(c)
        _write_json(os.path.join(workdir, "personas.json"),
                    {"active": ps.active_key(), "cards": full_cards})
        pdir = os.path.join(workdir, "personas")
        os.makedirs(pdir, exist_ok=True)
        for c in cards:
            key = str(c.get("key") or "")
            if not key:
                continue
            try:
                full = ps.get_card(key) or c
                _write_json(os.path.join(pdir, key + ".json"), full)
                counts["personas"] += 1
            except Exception:
                pass
        # 形象图：persona_faces/<key>/<file>
        step(50, "正在打包形象图…")
        for c in cards:
            key = str(c.get("key") or "")
            if not key:
                continue
            try:
                files = ps.list_faces(key) or []
            except Exception:
                files = []
            for f in files:
                src = f.get("path") if isinstance(f, dict) else str(f)
                if not src or not os.path.isfile(src):
                    continue
                dst = os.path.join(workdir, "personas_avatars", key,
                                   os.path.basename(src))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                counts["faces"] += 1
    except Exception as e:
        _job_progress(52, "人设卡打包出问题（跳过）：%s" % _human(e))

    # 3) 配置（TTS / 好感度 / Agent 习惯）
    step(65, "正在收拾习惯与语音参数…")
    try:
        cfg = _cfg_mod().load()
        picked = {k: v for k, v in cfg.items() if _cfg_pick(k)}
        counts["config"] = len(picked)
        _write_json(os.path.join(workdir, "config.json"), picked)
    except Exception as e:
        _job_progress(68, "配置打包出问题（跳过）：%s" % _human(e))

    return counts, data


def export_spark(path="", progress=None, mem=None, password=""):
    """把当前一切打成 `.spark`。返回 {ok, path, size, counts, msg}。

    v1.9.0：必须给口令（至少 6 位）。口令只有用户自己知道，程序里不再存任何
    能单独解开胶囊的密钥材料。
    """
    workdir = ""
    try:
        if progress:
            progress(2, "准备临时目录…")
        mem = mem or _mem_mod().Memory()
        workdir = tempfile.mkdtemp(prefix="spark_")
        counts, data = _collect(mem, workdir, progress)

        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "app_version": str(getattr(_cfg_mod(), "APP_VERSION", "")),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "created_ts": time.time(),
            "counts": {
                "personas": counts["personas"],
                "faces": counts["faces"],
                "config": counts["config"],
                "tables": counts["tables"],
                "total_rows": int(data.get("total") or 0),
                "kg_entities": int((counts["tables"] or {}).get("kg_entities") or 0),
                "kg_relations": int((counts["tables"] or {}).get("kg_relations") or 0),
            },
        }
        manifest["summary"] = (
            "包含 %d 条回忆、%d 张形象图、%d 项习惯、%d 张人设卡"
            % (sum(int(v or 0) for v in (counts["tables"] or {}).values()),
               counts["faces"], counts["config"], counts["personas"]))
        _write_json(os.path.join(workdir, MANIFEST), manifest)
        with io.open(os.path.join(workdir, "README.txt"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write("这是「小鲸鱼」的时光胶囊（.spark）。\n\n")
            f.write("它本质是个 ZIP 包：把后缀改成 .zip 就能用任何解压软件打开，\n")
            f.write("里面是明文 JSON 和图片 —— 我们不放加密，也不藏着。\n\n")
            f.write("恢复方式：小鲸鱼 → 设置 → 时光胶囊 → 唤醒胶囊，选中这个文件。\n")
            f.write("导出于 %s\n" % manifest["created_at"])

        if not path:
            fn = "时光胶囊_" + time.strftime("%Y%m%d_%H%M%S") + EXT
            path = os.path.join(DATA_DIR, fn)
        path = os.path.abspath(path)
        if not path.lower().endswith(EXT):
            path += EXT
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

        if progress:
            progress(80, "正在压缩…")
        tmp_zip = path + ".part"
        buf = io.BytesIO()
        try:
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as z:
                for root, _dirs, files in os.walk(workdir):
                    for fn in files:
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, workdir).replace("\\", "/")
                        z.write(full, rel)
            if progress:
                progress(90, "正在加密…")
            import spark_crypto as _sc
            # v1.9.0：改成口令加密。没有口令就直接抛错（check_password 会给人话），
            # 绝不再用写死在源码里的密钥材料「假加密」。
            blob = _sc.encrypt_with_password(buf.getvalue(), password)
            with open(tmp_zip, "wb") as f:
                f.write(blob)
            os.replace(tmp_zip, path)
        finally:
            try:
                if os.path.exists(tmp_zip):
                    os.remove(tmp_zip)
            except Exception:
                pass

        size = os.path.getsize(path)
        if progress:
            progress(100, "完成")
        return {"ok": True, "path": path, "size": size, "counts": counts,
                "manifest": manifest,
                "msg": "时光胶囊已生成：%s（%s，用口令加密）。"
                       "这个口令别忘了 —— 忘了就打不开了。" % (
                           os.path.basename(path), _human_size(size))}
    except Exception as e:
        return {"ok": False, "path": path, "msg": "生成胶囊失败：%s" % _human(e)}
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------- 导入

def read_manifest(path, password=None):
    """只读 manifest，用来「先看看这胶囊是什么」。"""
    try:
        with zipfile.ZipFile(_open_capsule(path, password=password)) as z:
            names = z.namelist()
            if MANIFEST not in names:
                return {"ok": False,
                        "msg": "这不是小鲸鱼的时光胶囊（缺少 manifest.json）"}
            raw = z.read(MANIFEST)
        m = json.loads(raw.decode("utf-8"))
        if str(m.get("format") or "") != FORMAT:
            return {"ok": False,
                    "msg": "这个文件的标记是「%s」，不是时光胶囊" % (m.get("format") or "空")}
        ver = int(m.get("format_version") or 0)
        if ver > FORMAT_VERSION:
            return {"ok": False,
                    "msg": "胶囊是更新版本的小鲸鱼导出的（格式 v%d，本机只认 v%d），"
                           "请先升级程序" % (ver, FORMAT_VERSION)}
        m["ok"] = True
        return m
    except Exception as e:
        return {"ok": False, "msg": _human(e)}


def preview_spark(path, password=None):
    m = read_manifest(path, password=password)
    if not m.get("ok"):
        return m
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    return {"ok": True, "manifest": m, "size": size,
            "summary": m.get("summary") or "",
            "created_at": m.get("created_at") or "",
            "app_version": m.get("app_version") or "",
            "msg": "胶囊完好：%s（导出于 %s，%s 版本）" % (
                m.get("summary") or "", m.get("created_at") or "未知",
                m.get("app_version") or "未知")}


def _restore_personas(workdir, mode):
    """人设卡 + 形象图写入。返回 (写入张数, 跳过张数, 图张数)。"""
    import personas_store as ps
    written = skipped = faces = 0
    failed = []
    pj = os.path.join(workdir, "personas.json")
    cards = []
    active = ""
    if os.path.isfile(pj):
        try:
            obj = json.load(io.open(pj, encoding="utf-8"))
            cards = obj.get("cards") or []
            active = str(obj.get("active") or "")
        except Exception:
            cards = []
    if not cards:
        pdir = os.path.join(workdir, "personas")
        if os.path.isdir(pdir):
            for fn in os.listdir(pdir):
                if not fn.lower().endswith(".json"):
                    continue
                try:
                    cards.append(json.load(io.open(os.path.join(pdir, fn),
                                                   encoding="utf-8")))
                except Exception:
                    pass
    # personas.json 里存的是「列表用的简卡」（没有 persona 正文），
    # 真正能写回的是 personas/<key>.json —— 所以有全卡就用全卡。
    full = {}
    pdir = os.path.join(workdir, "personas")
    if os.path.isdir(pdir):
        for fn in os.listdir(pdir):
            if not fn.lower().endswith(".json"):
                continue
            try:
                c = json.load(io.open(os.path.join(pdir, fn), encoding="utf-8"))
                k = str(c.get("key") or fn[:-5])
                c["key"] = k
                full[k] = c
            except Exception:
                pass
    if full:
        cards = [full.get(str(c.get("key") or ""), c) for c in cards]
        have = {str(c.get("key") or "") for c in cards}
        cards += [c for k, c in full.items() if k not in have]
    existing = set()
    try:
        existing = {str(c.get("key") or "") for c in (ps.list_cards() or [])}
    except Exception:
        pass
    for c in cards:
        if not isinstance(c, dict):
            continue
        key = str(c.get("key") or "").strip()
        if not key:
            continue
        if mode == "merge" and key in existing:
            skipped += 1
            continue
        try:
            # 覆盖模式也要先删同 key，否则 save_card 可能跟旧卡合并出怪结果
            if mode == "replace" and key in existing:
                try:
                    ps.delete_card(key)
                except Exception:
                    pass
            rr = ps.save_card(key, c, make_active=False)
            # save_card 返回 (ok, msg) —— 校验不过（比如正文太短）要能看见
            if isinstance(rr, tuple) and not rr[0]:
                failed.append("%s：%s" % (key, rr[1]))
                continue
            written += 1
        except Exception as e:
            failed.append("%s：%s" % (key, e))
    # 形象图
    addir = os.path.join(workdir, "personas_avatars")
    if os.path.isdir(addir):
        for key in os.listdir(addir):
            kd = os.path.join(addir, key)
            if not os.path.isdir(kd):
                continue
            for fn in os.listdir(kd):
                src = os.path.join(kd, fn)
                if not os.path.isfile(src):
                    continue
                try:
                    if mode == "merge":
                        have = {os.path.basename((f.get("path") if isinstance(f, dict)
                                                  else str(f)) or "")
                                for f in (ps.list_faces(key) or [])}
                        if fn in have:
                            continue
                    ps.save_face(key, src)
                    faces += 1
                except Exception:
                    pass
    if mode == "replace" and active:
        try:
            ps.set_active(active, quiet=True)
        except Exception:
            pass
    if failed:
        _job_progress(_JOB.get("pct", 60),
                      "有 %d 张人设卡没还原（%s）" % (len(failed), "；".join(failed[:3])))
    return written, skipped, faces, failed


def import_spark(path, mode="merge", progress=None, password=None):
    """把胶囊装回来。mode: replace=完全覆盖 / merge=保留现有、只追加。"""
    workdir = ""
    try:
        mode = "replace" if str(mode or "").lower() == "replace" else "merge"
        if not path or not os.path.isfile(path):
            return {"ok": False, "msg": "找不到这个胶囊文件：%s" % (path or "（空）")}
        if progress:
            progress(2, "正在校验胶囊…")
        man = read_manifest(path, password=password)
        if not man.get("ok"):
            return {"ok": False, "msg": man.get("msg") or "胶囊读不出来"}

        workdir = tempfile.mkdtemp(prefix="spark_in_")
        if progress:
            progress(10, "正在解压…")
        cinfo = {}
        try:
            with zipfile.ZipFile(_open_capsule(path, info=cinfo,
                                              password=password)) as z:
                z.extractall(workdir)
        except Exception as e:
            return {"ok": False, "msg": _human(e)}

        # v1.9.2：这条胶囊有没有口令保护？没有的话导入成功后必须醒目提示 ——
        # v2 及更早的胶囊用的是写死的密钥，谁拿到文件就等于拿到全部记忆。
        try:
            _no_pwd = not needs_password(path)
        except Exception:
            _no_pwd = False

        import mem_transfer
        import memory as mem_mod
        mem = mem_mod.Memory()

        # 覆盖模式：先给现库拍一份备份（导错了还能退回来）
        backup = ""
        if mode == "replace":
            if progress:
                progress(18, "正在备份现有记忆…")
            try:
                backup = mem_transfer.export_to_file(
                    mem=mem, limit_per_table=0).get("path") or ""
            except Exception:
                backup = ""

        stats = {"tables": {}, "inserted": 0, "skipped": 0,
                 "personas": 0, "personas_skipped": 0, "faces": 0, "config": 0}

        mj = os.path.join(workdir, "memory.json")
        if os.path.isfile(mj):
            if progress:
                progress(30, "正在写回记忆…")
            try:
                data = json.load(io.open(mj, encoding="utf-8"))
            except Exception as e:
                return {"ok": False, "msg": "记忆文件坏了：%s" % _human(e)}
            r = mem_transfer.import_data(mem, data, mode=mode)
            if not r.get("ok"):
                return {"ok": False, "msg": "写记忆失败：%s" % (r.get("msg") or "")}
            stats["tables"] = r.get("stats") or r.get("counts") or {}
            # v1.8.0：老胶囊（v1~v3）没有 kg_*两张表，这里显式兜底一次，
            #把图补进来；新胶囊走上面的 import_data已经带进来了，这里是幂等的。
            try:
                import kg as _kgmod
                _kgrows = {
                    "kg_entities": (data.get("tables") or {}).get("kg_entities") or [],
                    "kg_relations": (data.get("tables") or {}).get("kg_relations") or [],
                }
                if _kgrows["kg_relations"]:
                    _kr = _kgmod.import_rows(mem, _kgrows)
                    stats["kg"] = _kr
                else:
                    #老胶囊里没有图数据，如实报一句就行（不报错，也不假装成功）
                    stats["kg"] = {"ok": True, "entities":0, "relations":0,
                        "msg": "胶囊里没有关系网数据（老版本胶囊）"}
            except Exception as _kge:
                stats["kg"] = {"ok": False, "msg": _human(_kge)}

        if progress:
            progress(58, "正在还原人设卡…")
        try:
            w, sk, fc, pfail = _restore_personas(workdir, mode)
            stats["personas"], stats["personas_skipped"], stats["faces"] = w, sk, fc
            stats["personas_failed"] = pfail
        except Exception as e:
            _job_progress(60, "人设卡还原出问题（继续）：%s" % _human(e))

        if progress:
            progress(74, "正在恢复习惯与语音参数…")
        cj = os.path.join(workdir, "config.json")
        if os.path.isfile(cj) and mode == "replace":
            try:
                obj = json.load(io.open(cj, encoding="utf-8"))
                if isinstance(obj, dict):
                    cfgm = _cfg_mod()
                    cur = cfgm.load()
                    n = 0
                    for k, v in obj.items():
                        if not _cfg_pick(k):
                            continue
                        cur[k] = v
                        n += 1
                    # v1.5.1（P1-2）：导入胶囊一律把 Agent 的动手开关重置为关，
                    # 用户要开就自己走 10 秒警告闸门。
                    forced = 0
                    for _k in AGENT_DANGER_KEYS:
                        try:
                            if cur.get(_k):
                                forced += 1
                            cur[_k] = False
                        except Exception:
                            pass
                    cfgm.save(cur)
                    stats["config"] = n
                    stats["agent_forced_off"] = forced
            except Exception as e:
                _job_progress(78, "配置恢复出问题（继续）：%s" % _human(e))

        if progress:
            progress(92, "收尾…")
        try:
            mem.conn.commit()
        except Exception:
            pass
        try:
            mem.conn.close()
        except Exception:
            pass

        rows = sum(int(v or 0) for v in (man.get("counts", {}).get("tables") or {}).values())
        msg = ("胶囊已%s：记忆记录 %d 条（%d 张表）、人设卡 %d 张、"
               "形象图 %d 张、习惯 %d 项"
               % ("搬过来" if mode == "replace" else "融合进来",
                  rows, len(man.get("counts", {}).get("tables") or {}),
                  stats["personas"], stats["faces"], stats["config"]))
        if backup:
            msg += "；导入前的旧记忆已备份到 %s" % os.path.basename(backup)
        pfail = stats.get("personas_failed") or []
        if pfail:
            msg += "（有 %d 张人设卡因校验没过没写进来：%s）" % (
                len(pfail), "；".join(pfail[:2]))
        # v1.5.2：旧格式（带机器特征）与明文胶囊，导入后提示重新导出成新格式
        if cinfo.get("plain"):
            msg += "。这条是旧版未加密胶囊，建议现在重新导出一次（新胶囊已加密）"
        elif cinfo.get("legacy"):
            msg += "。这条胶囊是旧格式（带机器特征），已成功迁入；建议重新导出一次"
        elif cinfo.get("format") == "v2":
            msg += "（胶囊格式 v2，换电脑也能打开）"
        if _no_pwd:
            msg += "。⚠️ 这是旧版无口令胶囊，已为你解开"
        return {"ok": True, "mode": mode, "msg": msg, "stats": stats,
                "backup": backup, "manifest": man, "capsule": cinfo,
                "need_reexport": bool(_no_pwd),
                "sunset": V2_SUNSET_VERSION if _no_pwd else "",
                "warn": ("这是旧版无口令胶囊，已为您解开。"
                         "请立即用密码重新导出一份，并删除原文件！"
                         "从 V%s 起将不再支持无口令胶囊导入。" % V2_SUNSET_VERSION) if _no_pwd else ""}
    except Exception as e:
        return {"ok": False, "msg": "唤醒胶囊失败：%s" % _human(e)}
    finally:
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------- 后台任务

def start_export(path="", progress=None, password=""):
    """后台导出（前端轮询 spark_job）。返回是否成功启动。"""
    if _JOB.get("state") == "running":
        return {"ok": False, "msg": "上一件事还没做完，等它跑完再点"}

    def run():
        _job_begin("export")
        r = export_spark(path=path, progress=_job_progress, password=password)
        _job_done(r, "export")

    _job_begin("export")
    threading.Thread(target=run, daemon=True, name="spark-export").start()
    return {"ok": True, "msg": "开始打包"}


def start_import(path, mode="merge", password=""):
    if _JOB.get("state") == "running":
        return {"ok": False, "msg": "上一件事还没做完，等它跑完再点"}

    def run():
        _job_begin("import")
        r = import_spark(path=path, mode=mode, progress=_job_progress, password=password)
        _job_done(r, "import")

    _job_begin("import")
    threading.Thread(target=run, daemon=True, name="spark-import").start()
    return {"ok": True, "msg": "开始唤醒"}
