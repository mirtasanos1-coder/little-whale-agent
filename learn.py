"""学习模块（v1.1.1）。

为什么单独一个库
----------------
「永久记忆」记的是**关于别人的事实**（他是谁、喜欢什么、约定了什么）。
「学习」记的是**它自己学到的经验**：哪种回法对方笑、什么时候别接话、
群里的黑话/外号指谁、画图怎么画更像。两者混在一张表里会互相污染：
一条「别在群里正经讲道理」写进永久记忆，会被当成"对方的事实"回给本人。

存哪
----
`<DATA_DIR>/learn.db`，独立于 memory.db。这样导出记忆不会把学习清单
带走（用户要的是"记忆备份"，不想要一坨提示词），卸载保留数据时也能分开。

数据形状
--------
一行 = 一条可开关的经验：
    id / created_at / source(auto|manual|draw|preset) / title / content /
    trigger(触发词，可空) / tags / enabled(0/1) / use_count / last_used

怎么进模型
----------
`active_prompt()` 把所有 enabled 的条目拼成一段【学到的经验】，
由 agent.refresh_system_prompt() 追加到系统提示词末尾。
不 enabled 的只在列表里看着，不进提示词 —— 这满足「应用方案后不被清空」。
"""
import json
import os
import re
import sqlite3
import threading
import time

import config as cfg_mod

DB_NAME = "learn.db"
_LOCK = threading.RLock()
_CONN = None

SOURCES = ("auto", "manual", "draw", "preset")

# v1.1.3 任务八：学习类型。key 存库（英文），cn 给界面看。
# cognition/values 这两类会写进「世界观」，所以额外上一道内容闸。
TYPES = {
    "speech":        "说话方式",
    "academic":      "知识",
    "empathy":       "共情方式",
    "micro_emotion": "细微情绪",
    "cognition":     "认知/世界观",
    "values":        "价值观",
    "habit":         "习惯",
    "interpersonal": "人际相处",
    "sensory":       "感官偏好",
}
DEFAULT_TYPE = "speech"
# 这些类型的条目会进角色底色，必须拒绝极端/违法/涉黄涉暴的内容
STRICT_TYPES = ("cognition", "values")

# 明显的红线内容（中文为主，英文只兜最露骨的几个词）
_BANNED_RE = re.compile(
    r"(自杀方法|怎么自杀|杀人|砍人|投毒|制作炸弹|炸药配方|制毒|贩毒|毒品配方|"
    r"强奸|恋童|未成年.{0,4}性|器官买卖|洗钱|恐怖袭击|反人类|"
    r"种族灭绝|屠杀|纳粹万岁|儿童色情|child\s*porn|cp\s*图)", re.I)

SOURCE_CN = {"auto": "自动学到", "manual": "手写", "draw": "画图学到", "preset": "预设方案"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learn_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    source     TEXT NOT NULL DEFAULT 'manual',
    title      TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL DEFAULT '',
    trigger    TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    use_count  INTEGER NOT NULL DEFAULT 0,
    last_used  REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_learn_enabled ON learn_items(enabled);
"""


def db_path():
    return os.path.join(cfg_mod.DATA_DIR, DB_NAME)


# ==================== v1.1.4：学习安全底线（可关，但要签协议） ====================
AGREEMENT_FILE = "learn_bottom_line_agreement.json"
PROTOCOL_VERSION = "1.1.4"


def _log(msg, level="warn"):
    try:
        import log as _logmod
        getattr(_logmod, level, _logmod.info)(msg)
    except Exception:
        pass


def agreement_path():
    return os.path.join(cfg_mod.DATA_DIR, AGREEMENT_FILE)


def agreement_info():
    """已签署的本地协议内容；没有/读不出返回 {}。"""
    p = agreement_path()
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def bottom_line_on():
    """学习安全底线是否生效。

    用户定的规则：
      1. 默认开启（拦截极端/违法/涉黄涉暴的内容）；
      2. 要关闭必须在本机签署免责协议；
      3. 协议文件被删掉（换机、清数据、手动删）→ 自动恢复开启。
    """
    on = bool(cfg_mod.get("learn_bottom_line", True))
    if on:
        return True
    if agreement_info():
        return False
    try:
        cfg_mod.set_checked("learn_bottom_line", True)
        _log("[学习] 免责协议文件不在了，安全底线已自动恢复为开启")
    except Exception:
        pass
    return True


def sign_agreement(signature=""):
    """签协议：写本地文件 + 把 learn_bottom_line 置 False。返回写进去的内容。"""
    sig = str(signature or "").strip()
    if len(sig) < 2:
        return {"_error": "签名不能空着（至少写 2 个字），这是你自己签的协议"}
    info = {
        "version": PROTOCOL_VERSION,
        "signed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": time.time(),
        "signature": sig[:60],
    }
    try:
        with open(agreement_path(), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"}
    try:
        cfg_mod.set_checked("learn_bottom_line", False)
    except Exception:
        pass
    _log("[学习] 已签署免责协议，安全底线关闭（可随时撤销）", "info")
    return info


def revoke_agreement():
    """撤销：删本地协议 + 立刻把底线恢复开启。"""
    removed = False
    try:
        if os.path.isfile(agreement_path()):
            os.remove(agreement_path())
            removed = True
    except Exception:
        pass
    try:
        cfg_mod.set_checked("learn_bottom_line", True)
    except Exception:
        pass
    _log("[学习] 已撤销免责协议，安全底线恢复开启", "info")
    return removed


def _conn():
    global _CONN
    with _LOCK:
        if _CONN is None:
            d = os.path.dirname(db_path())
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            _CONN = sqlite3.connect(db_path(), check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.executescript(_SCHEMA)
            # v1.1.3：老库补 ltype 列（没有就加，有就忽略）
            try:
                _CONN.execute("ALTER TABLE learn_items ADD COLUMN ltype TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            _CONN.commit()
        return _CONN


def _row(r):
    d = dict(r)
    d["source_cn"] = SOURCE_CN.get(d.get("source") or "", d.get("source") or "")
    d["enabled"] = bool(d.get("enabled"))
    lt = str(d.get("ltype") or "").strip() or DEFAULT_TYPE
    d["ltype"] = lt
    d["ltype_cn"] = TYPES.get(lt, lt)
    return d


def check_type(ltype):
    """把前端传来的类型名收敛成合法 key。"""
    k = str(ltype or "").strip().lower()
    return k if k in TYPES else DEFAULT_TYPE


def add(source="manual", title="", content="", trigger="", tags="", enabled=True, ltype=None):
    """加一条学到的经验，返回 (ok, msg, id)。"""
    try:
        content = str(content or "").strip()[:4000]
        title = str(title or "").strip()[:80]
        lt = check_type(ltype)
        if not content:
            return False, "内容不能为空", 0
        # v1.1.3 任务八：认知/价值观这类会进角色底色的条目，红线内容一律不收
        # v1.1.4：这个拦截受「学习安全底线」开关控制；关掉时只记一条日志放行
        _bl = bottom_line_on()
        if lt in STRICT_TYPES and _BANNED_RE.search(content):
            if not _bl:
                _log(f"[学习] 安全底线已关闭（已签协议），放行红线内容（类型 {lt}）：{content[:60]}")
            else:
                _log(f"[学习] 拒绝写入红线内容（类型 {lt}）：{content[:60]}")
                return False, "这条内容越过红线（极端/违法/涉黄涉暴），不能学进世界观", 0
        if _BANNED_RE.search(content):
            if not _bl:
                _log(f"[学习] 安全底线已关闭（已签协议），放行红线内容：{content[:60]}")
            else:
                return False, "这条内容越过红线，不能学", 0
        if not title:
            title = content.splitlines()[0][:40]
        with _LOCK:
            c = _conn().cursor()
            # 完全一样的内容不重复加（防连点）
            c.execute("SELECT id FROM learn_items WHERE content=?", (content,))
            old = c.fetchone()
            if old:
                return False, "这条已经学过了", int(old[0])
            c.execute("""INSERT INTO learn_items
                         (created_at, source, title, content, trigger, tags, enabled, ltype)
                         VALUES (?,?,?,?,?,?,?,?)""",
                      (time.time(), source if source in SOURCES else "manual",
                       title, content, str(trigger or "")[:80], str(tags or "")[:120],
                       1 if enabled else 0, lt))
            _conn().commit()
            return True, "已加入学习列表", int(c.lastrowid or 0)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", 0


def update(item_id, title=None, content=None, trigger=None, tags=None, enabled=None, ltype=None):
    try:
        sets, args = [], []
        for k, v in (("title", title), ("content", content),
                     ("trigger", trigger), ("tags", tags)):
            if v is not None:
                sets.append(f"{k}=?")
                args.append(str(v).strip()[:4000])
        if ltype is not None:
            sets.append("ltype=?")
            args.append(check_type(ltype))
        if enabled is not None:
            sets.append("enabled=?")
            args.append(1 if enabled else 0)
        if not sets:
            return False, "没有要改的字段"
        args.append(int(item_id))
        with _LOCK:
            c = _conn().cursor()
            c.execute(f"UPDATE learn_items SET {', '.join(sets)} WHERE id=?", args)
            _conn().commit()
            return (c.rowcount > 0), "已保存" if c.rowcount else "没找到这一条"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def delete(item_id):
    try:
        with _LOCK:
            c = _conn().cursor()
            c.execute("DELETE FROM learn_items WHERE id=?", (int(item_id),))
            _conn().commit()
            return (c.rowcount > 0), "已删除" if c.rowcount else "没找到这一条"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def set_enabled(ids, enabled=True):
    """批量启用 / 停用（「应用方案」＝ 把选中的打开）。"""
    try:
        ids = [int(x) for x in (ids or [])]
        if not ids:
            return False, "没选条目"
        q = ",".join("?" * len(ids))
        with _LOCK:
            c = _conn().cursor()
            c.execute(f"UPDATE learn_items SET enabled=? WHERE id IN ({q})",
                      [1 if enabled else 0] + ids)
            _conn().commit()
            n = c.rowcount
        return True, f"已{'应用' if enabled else '停用'} {n} 条"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def list_items(limit=500, only_enabled=False):
    try:
        with _LOCK:
            c = _conn().cursor()
            if only_enabled:
                c.execute("SELECT * FROM learn_items WHERE enabled=1 ORDER BY id DESC LIMIT ?",
                          (int(limit),))
            else:
                c.execute("SELECT * FROM learn_items ORDER BY id DESC LIMIT ?", (int(limit),))
            return [_row(r) for r in c.fetchall()]
    except Exception:
        return []


def stats():
    """学习统计（v1.1.2：多给按来源分布，给可视化用）。"""
    try:
        with _LOCK:
            c = _conn().cursor()
            c.execute("SELECT COUNT(*) FROM learn_items")
            total = int((c.fetchone() or [0])[0] or 0)
            c.execute("SELECT COUNT(*) FROM learn_items WHERE enabled=1")
            on = int((c.fetchone() or [0])[0] or 0)
            by_source = {k: 0 for k in SOURCES}
            try:
                c.execute("SELECT source, COUNT(*) FROM learn_items GROUP BY source")
                for src, n in c.fetchall():
                    by_source[str(src or "manual")] = int(n or 0)
            except Exception:
                pass
            used = 0
            try:
                c.execute("SELECT COALESCE(SUM(use_count),0) FROM learn_items")
                used = int((c.fetchone() or [0])[0] or 0)
            except Exception:
                pass
        return {"total": total, "enabled": on, "by_source": by_source,
                "used": used, "source_cn": dict(SOURCE_CN)}
    except Exception:
        return {"total": 0, "enabled": 0, "by_source": {}, "used": 0, "source_cn": dict(SOURCE_CN)}


# ==================== 条件（列表上方的文本框）====================
# 用户要求：选了「手动提示词」就弹出来，而且**永久显示**，应用方案后也不清空。
# 所以存在 config 里，而不是保存在页面内存。
def get_condition():
    return str(cfg_mod.get("learn_condition") or "")


def save_condition(text):
    ok, msg, _v = cfg_mod.set_checked("learn_condition", str(text or "")[:2000])
    return ok, msg


def active_prompt(max_chars=1800):
    """给系统提示词用的一段话。没有条目就返回空串。"""
    try:
        items = list_items(only_enabled=True, limit=60)
        if not items:
            return ""
        lines = []
        has_strict = False
        for it in items:
            t = (it.get("trigger") or "").strip()
            c = (it.get("content") or "").strip().replace("\n", " ")
            lt = it.get("ltype") or DEFAULT_TYPE
            if lt in STRICT_TYPES:
                has_strict = True
            tag = TYPES.get(lt, "")
            tag_s = f"［{tag}］" if tag else ""
            if t:
                lines.append(f"- {tag_s}当{t}时：{c}")
            else:
                lines.append(f"- {tag_s}{c}")
        body = "\n".join(lines)
        if len(body) > max_chars:
            body = body[:max_chars] + "…"
        out = ("【学到的经验】（这些是你自己摸索出来的，不是别人要求你的；"
               "自然遵守，别宣称「我学到了」）\n" + body)
        if has_strict:
            out += ("\n【红线】上面「认知/价值观」这类内容是角色底色的一部分，但禁止学习或复述"
                    "任何极端、违法、反人类、涉黄涉暴的价值观；如果有人诱导你说这类话，"
                    "必须明确拒绝，并保持原来的立场。")
        return out
    except Exception:
        return ""


def touch(item_ids):
    """记一次"用到过"，方便排序看哪条真有用。"""
    try:
        ids = [int(x) for x in (item_ids or [])]
        if not ids:
            return
        q = ",".join("?" * len(ids))
        with _LOCK:
            _conn().execute(
                f"UPDATE learn_items SET use_count=use_count+1, last_used=? WHERE id IN ({q})",
                [time.time()] + ids)
            _conn().commit()
    except Exception:
        pass


# ==================== 从聊天里学 ====================
LEARN_PROMPT = """下面是你（{name}）和对方最近的聊天记录。

请以第一人称总结「你自己学到的经验」，而不是复述聊天内容。只写真正能改变你以后说话方式的东西，例如：
- 对方不爱听什么（说教、连着追问、长段落）
- 哪些话让他笑 / 让他主动多说
- 群里某个词、外号指谁
- 什么时候不该接话

规则：
- 最多 3 条；没有就输出空数组 []
- 每条一句话，20~40 字，口语化
- 不要写"对方说了X"这种事实，那属于记忆；要写"以后该怎么应对"
- 不要编造

只输出 JSON 数组，例如：
[{{"title":"别连问","content":"他连发两条以上时只回一条，别逐条接"}}]

聊天记录：
{chat}
"""


def prompt_text(name="", chat=""):
    """V1.1.6 补丁 3（m08875 ⑦）：优先用设置里「学习提示词」的本地版本。

    用户在界面上改过就用他的（不用改代码）；没改就用内置 LEARN_PROMPT。
    本地版本里如果写了 {chat}/{name} 占位符会替换，没写就把聊天记录接在后面。
    """
    custom = ""
    try:
        custom = str(cfg_mod.get("learn_prompt") or "").strip()
    except Exception:
        custom = ""
    if custom:
        try:
            body = custom.format(name=name or "小鲸鱼", chat=chat)
        except Exception:
            body = custom + "\n\n聊天记录：\n" + chat
        if "{chat}" not in custom and chat and chat not in body:
            body += "\n\n聊天记录：\n" + chat
        return body
    return LEARN_PROMPT.format(name=name or "小鲸鱼", chat=chat)


def recent_lines(mem, user_id, limit=60):
    """取某个人的最近聊天（正序）。

    v1.9.23：这里以前调的是 `mem.list_messages()` —— Memory 根本没有这个方法，
    `hasattr` 一判 False 就直接吞成空列表，于是「学习」和「每日总结」每一轮都
    拿到空聊天、永远只输出「聊天太少，先多聊几句」。日志里看着一直在跑，
    实际上一次都没真的学过。现在改用 Memory 真正有的 `messages_of()`，
    并兼容 `private_<uid>` 这种带前缀的会话键。
    """
    uid = str(user_id or "").strip()
    if not uid:
        return []
    for key in (f"private_{uid}", uid):
        try:
            rows = mem.messages_of(key, limit=int(limit)) or []
        except Exception:
            rows = []
        if rows:
            return rows
    try:
        return mem.get_recent_messages(f"private_{uid}", turns=max(4, int(limit) // 2)) or []
    except Exception:
        return []


def learn_from_dialog(mem, user_id, name="", model_caller=None, days=1, max_items=3):
    """从最近聊天里提炼经验。需要传 model_caller(prompt) -> 文本。"""
    try:
        if not bool(cfg_mod.get("learn_enabled", True)):
            return {"ok": False, "msg": "学习开关是关的", "added": 0}
        if model_caller is None:
            return {"ok": False, "msg": "没有可用的模型", "added": 0}
        rows = recent_lines(mem, user_id, limit=60)
        chat = "\n".join(f"{r.get('role') or '?'}: {str(r.get('content') or '')[:200]}"
                         for r in rows)[-3000:]
        if len(chat) < 40:
            return {"ok": False, "msg": "聊天太少，先多聊几句", "added": 0}
        text = model_caller(prompt_text(name=name, chat=chat)) or ""
        items = _parse_items(text)[:max_items]
        n = 0
        for it in items:
            ok, _m, _i = add(source="auto", title=it.get("title", ""),
                             content=it.get("content", ""))
            if ok:
                n += 1
        if n:
            try:
                import learn  # noqa
            except Exception:
                pass
        return {"ok": True, "added": n, "msg": f"学到 {n} 条新经验" if n else "这次没什么新东西",
                "items": items}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "added": 0}


def _parse_items(text):
    """从模型输出里抠 JSON 数组，抠不出来就用逐行兜底。"""
    out = []
    s = str(text or "")
    a, b = s.find("["), s.rfind("]")
    if a >= 0 and b > a:
        try:
            arr = json.loads(s[a:b + 1])
            for x in arr:
                if isinstance(x, dict):
                    c = str(x.get("content") or x.get("text") or "").strip()
                    if c:
                        out.append({"title": str(x.get("title") or "")[:40], "content": c[:200]})
            if out:
                return out
        except Exception:
            pass
    for line in s.splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if 8 <= len(line) <= 200 and "：" in line or ":" in line:
            t, _sep, c = line.partition("：") if "：" in line else line.partition(":")
            if c.strip():
                out.append({"title": t.strip()[:40], "content": c.strip()[:200]})
    return out


DAILY_PROMPT = """下面是我和 TA 今天全部的聊天（可能不止一个人）。请做一次「今日全量总结」：
1) 总结今天聊到的事实、约定、没做完的事（只写会长期有用的）；
2) 总结 TA 今天的情绪基调，以及我的回应方式有没有问题；
3) 输出 1~5 条可复用的经验（类型从 cognition/values/habit/interpersonal/empathy 里挑）。
只输出 JSON 数组，元素形如 {"type":"habit","title":"短标题","content":"一句话经验"}。

聊天记录：
{chat}
"""


def summarize_day(mem, model_caller=None, days=1, max_items=5):
    """v1.1.4 3.4：每日全量总结（把所有绑定账号今天的聊天一起看）。"""
    try:
        if not bool(cfg_mod.get("learn_enabled", True)):
            return {"ok": False, "msg": "学习开关是关的", "added": 0}
        if model_caller is None:
            return {"ok": False, "msg": "没有可用的模型", "added": 0}
        try:
            import diary as _d
            uids = _d.bound_accounts()
        except Exception:
            uids = []
        if not uids:
            return {"ok": False, "msg": "还没有绑定账号", "added": 0}
        lines = []
        for uid in uids:
            for r in recent_lines(mem, uid, limit=120) or []:
                c = str(r.get("content") or "").strip().replace("\n", " ")
                if c:
                    lines.append(("我：" if r.get("role") == "assistant" else "TA：") + c[:160])
        # v1.9.23：群聊也算「今天聊过的」——以前只读私聊，群里聊一天也总结不出来。
        try:
            gkeys = [str(s.get("session_key") or "") for s in (mem.session_stats() or [])]
            for sk in [k for k in gkeys if k.startswith("group_")][:8]:
                for r in (mem.messages_of(sk, limit=80) or []):
                    c = str(r.get("content") or "").strip().replace("\n", " ")
                    if c:
                        lines.append(("[群]" + ("我：" if r.get("role") == "assistant" else "TA：")) + c[:120])
        except Exception:
            pass
        chat = "\n".join(lines)[-6000:]
        if len(chat) < 200:
            return {"ok": False, "msg": "今天聊得不多，不总结", "added": 0}
        # v1.9.23：DAILY_PROMPT 里有 {"type":"habit",...} 这种 JSON 示例，
        # 用 .format() 会被当成占位符 → KeyError: '"type"'，每日总结一直静默失败。
        text = model_caller(DAILY_PROMPT.replace("{chat}", chat)) or ""
        items = _parse_items(text)[:max_items]
        n = 0
        for it in items:
            ok, _m, _i = add(source="auto", title=it.get("title", ""),
                             content=it.get("content", ""), ltype=it.get("type"))
            if ok:
                n += 1
        return {"ok": True, "added": n, "items": items,
                "msg": ("今日总结：学到 %d 条" % n) if n else "今天没什么新东西"}
    except Exception as e:
        return {"ok": False, "msg": "%s: %s" % (type(e).__name__, e), "added": 0}
