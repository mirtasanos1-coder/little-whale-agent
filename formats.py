# -*- coding: utf-8 -*-
"""记忆导入/导出格式适配 —— 让别的软件的记忆能搬进来，也能搬出去

为什么要这一层
--------------
v5 的导出只有自家 JSON 一种格式。真到"我以前用别的机器人，想把记忆搬过来"的时候，
对方导出的可能是 CSV、Markdown、JSONL，甚至就是一段纯文本。硬要求用户手改格式
不现实，所以这里做一层适配：

    别的格式 --parse--> 统一中间结构 --apply--> 本软件的 perm_memory

反向也一样：`export()` 可以导出成本软件格式（留档/再导入），也可以导出成
CSV / Markdown / JSONL / TXT（给别人看、给表格打开）。

统一的中间结构
--------------
```python
{
  "perm": [{"user_id": "...", "category": "other", "content": "...",
            "importance": 3, "source": "import"}],
  "diary": [{"user_id": "...", "day": "2026-10-22", "title": "", "content": "...",
             "mood": ""}],
  "events": [{"user_id": "...", "event_desc": "...", "target_time": 1234567890.0}],
  "messages": [{"session_key": "...", "role": "user", "content": "...", "ts": 0.0}],
}
```

导入之后建议跑一次 `mem_rebuild.rebuild()`（界面上的「重编译」），
补 norm_key、清重复、迁移老 facts —— 界面上导入按钮会自动提示。
"""
import csv
import io
import json
import re
import time

# 支持的格式清单（界面下拉直接用这个）
FORMATS = [
    {"id": "qq-agent", "name": "小鲸鱼 JSON（本软件格式）", "ext": ".json",
     "kind": "all", "desc": "全量：永久记忆 + 日记 + 待办事件。"},
    {"id": "generic-json", "name": "通用 JSON（数组 / items）", "ext": ".json",
     "kind": "all", "desc": "别的软件常见的 [{user_id, content, category}]。"},
    {"id": "jsonl", "name": "JSONL（一行一条，聊天记录也可）", "ext": ".jsonl",
     "kind": "all", "desc": "聊天记录会被当成临时上下文导入。"},
    {"id": "csv", "name": "CSV 表格", "ext": ".csv",
     "kind": "memory", "desc": "表头 user_id,category,content,importance（也认中文表头）。"},
    {"id": "markdown", "name": "Markdown", "ext": ".md",
     "kind": "memory", "desc": "## 账号 分节，下面 - 一条记忆。"},
    {"id": "txt", "name": "纯文本（一行一条）", "ext": ".txt",
     "kind": "memory", "desc": "每行一条记忆，全部归到指定的账号下。"},
]
FORMAT_IDS = tuple(f["id"] for f in FORMATS)

# 分类归一：别的软件叫法五花八门，全部映射到我们的 7 类
_CAT_ALIASES = {
    "identity": ("identity", "身份", "个人", "basic", "profile", "info", "资料"),
    "preference": ("preference", "prefer", "like", "喜好", "偏好", "爱好", "喜欢"),
    "relation": ("relation", "relationship", "family", "关系", "家人", "情感", "恋爱"),
    "plan": ("plan", "todo", "schedule", "计划", "安排", "待办", "日程"),
    "health": ("health", "medical", "身体", "健康", "医疗", "病历"),
    "work": ("work", "job", "career", "工作", "职业", "公司", "学业"),
    "other": ("other", "misc", "general", "其他", "其它", "杂项", ""),
}
_CAT_LOOKUP = {}
for _canon, _aliases in _CAT_ALIASES.items():
    for _a in _aliases:
        _CAT_LOOKUP[_a] = _canon


def _norm_cat(value, default="other"):
    s = str(value or "").strip().lower()
    if not s:
        return default
    if s in _CAT_LOOKUP:
        return _CAT_LOOKUP[s]
    for k, v in _CAT_LOOKUP.items():
        if k and k in s:
            return v
    return default


def _to_ts(value, default=0.0):
    """尽量把各种时间写法变成 epoch 秒。"""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        v = float(value)
        # 毫秒时间戳
        return v / 1000.0 if v > 1e11 else v
    s = str(value).strip()
    if not s:
        return default
    if re.fullmatch(r"\d{10,14}", s):
        v = float(s)
        return v / 1000.0 if v > 1e11 else v
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return time.mktime(time.strptime(s[:len(fmt) + 2].strip(), fmt))
        except Exception:
            continue
    return default


def _clean_fact(text, limit=300):
    s = str(text or "").strip()
    s = re.sub(r"^[-*·•\d]+[.)、]?\s*", "", s)          # 去掉列表符号
    s = re.sub(r"^\[[^\]]{1,12}\]\s*", "", s)            # 去掉 [分类] 前缀
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit]
    return s


def _empty():
    return {"perm": [], "diary": [], "events": [], "messages": []}


# ---------------------------------------------------------------------------
# 识别
# ---------------------------------------------------------------------------
_RECORD_KEYS = ("content", "内容", "fact", "事实", "memory", "记忆", "text", "text_raw")
_USER_KEYS = ("user_id", "userid", "uid", "qq", "账号", "qq号", "扣扣")


def _looks_like_record(d):
    """单个 JSON 对象是不是"一条记忆"（别的软件常导出这个形状）。"""
    if not isinstance(d, dict):
        return False
    return any(k in d for k in _RECORD_KEYS)


def detect(text):
    """猜格式。猜不出来返回 None。"""
    s = (text or "").strip()
    if not s:
        return None
    if s.startswith("{") or s.startswith("["):
        try:
            data = json.loads(s)
        except Exception:
            data = None
        if isinstance(data, dict):
            if data.get("format") == "qq-agent-memory" or "perm" in data:
                return "qq-agent"
            if isinstance(data.get("items"), list):
                return "generic-json"
            if _looks_like_record(data):
                return "generic-json"
        if isinstance(data, list):
            return "generic-json"
        # JSON 整体解析失败 —— 大概率是 JSONL（一行一条）
        lines = [ln for ln in s.splitlines() if ln.strip()]
        if lines:
            ok = 0
            for ln in lines[:20]:
                try:
                    json.loads(ln)
                    ok += 1
                except Exception:
                    break
            if ok and ok == len(lines[:20]):
                return "jsonl"
        return None
    lines = [ln for ln in s.splitlines() if ln.strip()]
    if not lines:
        return None
    if sum(1 for ln in lines[:20] if ln.lstrip().startswith("#")) >= 1:
        return "markdown"
    head = lines[0].lower()
    if ("," in head or "\t" in head or "，" in head) and any(
            k in head for k in ("user", "账号", "qq", "content", "内容", "fact", "记忆")):
        return "csv"
    if len(lines) > 1:
        return "txt"
    return None


# ---------------------------------------------------------------------------
# 解析：任何格式 -> 统一中间结构
# ---------------------------------------------------------------------------
def parse(text, fmt=None, user_id=""):
    """解析文本，返回 (ok, parsed_or_msg, fmt_used)。"""
    text = text or ""
    fmt = (fmt or "").strip().lower() or None
    if fmt in ("auto", "", None):
        fmt = detect(text)
    if fmt not in FORMAT_IDS:
        return False, "识别不出格式，请手动选一个（支持：%s）" % "、".join(FORMAT_IDS), fmt
    try:
        fn = {
            "qq-agent": _parse_native,
            "generic-json": _parse_generic_json,
            "jsonl": _parse_jsonl,
            "csv": _parse_csv,
            "markdown": _parse_markdown,
            "txt": _parse_txt,
        }[fmt]
        ok, data = fn(text, user_id)
        return ok, data, fmt
    except Exception as e:
        return False, "解析失败：%s" % e, fmt


def _parse_native(text, user_id=""):
    data = json.loads(text)
    if not isinstance(data, dict):
        return False, "不是小鲸鱼的 JSON"
    if data.get("format") != "qq-agent-memory" and "perm" not in data:
        return False, "缺少 format 标记，也不像小鲸鱼导出的"
    out = _empty()
    for it in data.get("perm") or []:
        if not isinstance(it, dict):
            continue
        content = _clean_fact(it.get("content"))
        if not content:
            continue
        out["perm"].append({
            "user_id": str(it.get("user_id") or user_id or "").strip(),
            "category": _norm_cat(it.get("category")),
            "content": content,
            "importance": _clamp_int(it.get("importance"), 1, 5, 3),
            "source": "import",
        })
    for it in data.get("diary") or []:
        if not isinstance(it, dict):
            continue
        content = str(it.get("content") or "").strip()
        if not content:
            continue
        out["diary"].append({
            "user_id": str(it.get("user_id") or user_id or "").strip(),
            "day": str(it.get("day") or time.strftime("%Y-%m-%d"))[:10],
            "title": str(it.get("title") or ""),
            "content": content,
            "mood": str(it.get("mood") or ""),
            "msg_count": _clamp_int(it.get("msg_count"), 0, 100000, 0),
        })
    for it in data.get("events") or []:
        if not isinstance(it, dict):
            continue
        desc = _clean_fact(it.get("event_desc") or it.get("desc"))
        if not desc:
            continue
        out["events"].append({
            "user_id": str(it.get("user_id") or user_id or "").strip(),
            "event_desc": desc,
            "target_time": _to_ts(it.get("target_time") or it.get("time")),
        })
    return True, out


def _parse_generic_json(text, user_id=""):
    data = json.loads(text)
    if isinstance(data, dict) and _looks_like_record(data):
        items = [data]          # 单个对象也当成只有一条
    else:
        items = data if isinstance(data, list) else (data.get("items") or data.get("memories") or [])
    if not isinstance(items, list):
        return False, "找不到记忆数组"
    out = _empty()
    for it in items:
        if isinstance(it, str):
            content = _clean_fact(it)
            if content:
                out["perm"].append({"user_id": str(user_id), "category": "other",
                                    "content": content, "importance": 3,
                                    "source": "import"})
            continue
        if not isinstance(it, dict):
            continue
        content = _clean_fact(it.get("content") or it.get("fact") or it.get("text")
                              or it.get("memory") or it.get("value"))
        if not content:
            continue
        uid = str(it.get("user_id") or it.get("qq") or it.get("uid")
                  or it.get("account") or user_id or "").strip()
        out["perm"].append({
            "user_id": uid,
            "category": _norm_cat(it.get("category") or it.get("type") or it.get("kind")),
            "content": content,
            "importance": _clamp_int(it.get("importance") or it.get("weight"), 1, 5, 3),
            "source": "import",
        })
    if not out["perm"]:
        return False, "这个 JSON 里没找到能用的记忆条目"
    return True, out


def _parse_jsonl(text, user_id=""):
    out = _empty()
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            it = json.loads(ln)
        except Exception:
            continue
        if not isinstance(it, dict):
            continue
        role = str(it.get("role") or it.get("type") or "").lower()
        content = str(it.get("content") or it.get("text") or "").strip()
        if not content:
            continue
        uid = str(it.get("user_id") or it.get("qq") or user_id or "").strip()
        if role in ("user", "assistant", "system"):
            # 聊天记录 → 临时上下文
            sk = it.get("session_key") or ("private_" + uid if uid else "")
            if not sk:
                continue
            out["messages"].append({
                "session_key": str(sk),
                "role": "user" if role == "user" else "assistant",
                "content": content[:2000],
                "ts": _to_ts(it.get("ts") or it.get("time")),
            })
            continue
        fact = _clean_fact(content)
        if fact:
            out["perm"].append({
                "user_id": uid,
                "category": _norm_cat(it.get("category") or it.get("kind")),
                "content": fact,
                "importance": _clamp_int(it.get("importance"), 1, 5, 3),
                "source": "import",
            })
    if not out["perm"] and not out["messages"]:
        return False, "这份 JSONL 里没有可导入的内容"
    return True, out


def _parse_csv(text, user_id=""):
    text = text.lstrip("\ufeff")
    sample = text[:2048]
    delim = ","
    for cand in (",", "\t", ";", "，"):
        if sample.count(cand) > sample.count(delim):
            delim = cand
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    if not reader.fieldnames:
        return False, "CSV 没有表头"
    fields = {str(f or "").strip().lower(): str(f or "").strip()
              for f in reader.fieldnames}

    def pick(*names):
        for n in names:
            if n in fields:
                return fields[n]
        return None

    f_content = pick("content", "fact", "text", "memory", "内容", "记忆", "事实")
    if not f_content:
        return False, "CSV 里找不到内容列（content / 内容 / 记忆）"
    f_user = pick("user_id", "uid", "qq", "account", "账号", "qq号")
    f_cat = pick("category", "type", "kind", "分类", "类型")
    f_imp = pick("importance", "weight", "score", "重要度", "权重")
    out = _empty()
    for row in reader:
        content = _clean_fact(row.get(f_content))
        if not content:
            continue
        uid = str((row.get(f_user) if f_user else "") or user_id or "").strip()
        out["perm"].append({
            "user_id": uid,
            "category": _norm_cat(row.get(f_cat) if f_cat else ""),
            "content": content,
            "importance": _clamp_int(row.get(f_imp) if f_imp else 3, 1, 5, 3),
            "source": "import",
        })
    if not out["perm"]:
        return False, "CSV 解析出来是空的"
    return True, out


def _parse_markdown(text, user_id=""):
    out = _empty()
    cur_user = str(user_id or "").strip()
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^#{1,6}\s*(.+?)\s*$", line)
        if m:
            title = m.group(1)
            title = re.sub(r"^(记忆|memory|账号|user)[:：]\s*", "", title, flags=re.I)
            if re.fullmatch(r"\d{5,12}", title.strip()):
                cur_user = title.strip()
            continue
        m = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if not m:
            continue
        content = _clean_fact(m.group(1))
        if not content:
            continue
        out["perm"].append({"user_id": cur_user, "category": _norm_cat(content[:2]),
                            "content": content, "importance": 3,
                            "source": "import"})
    if not out["perm"]:
        return False, "Markdown 里没找到 - 开头的记忆条目"
    return True, out


def _parse_txt(text, user_id=""):
    out = _empty()
    for raw in (text or "").splitlines():
        content = _clean_fact(raw)
        if len(content) < 2:
            continue
        out["perm"].append({"user_id": str(user_id or "").strip(),
                            "category": "other", "content": content,
                            "importance": 3, "source": "import"})
    if not out["perm"]:
        return False, "这个文本里没有内容"
    return True, out


def _clamp_int(value, lo, hi, default):
    try:
        v = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def export(mem, fmt="qq-agent", user_id=None, include_diary=True):
    """把库里的记忆导出成指定格式，返回 (ok, text, msg)。"""
    fmt = (fmt or "qq-agent").strip().lower()
    if fmt not in FORMAT_IDS:
        return False, "", "不支持的导出格式：%s" % fmt
    uid = str(user_id or "").strip() or None
    try:
        perm = mem.get_perm_list(limit=5000, include_superseded=False) if not uid \
            else mem.get_perm(uid, limit=5000)
    except Exception as e:
        return False, "", "读数据库失败：%s" % e
    if uid:
        perm = [dict(x, user_id=uid) for x in perm]
    diary = []
    if include_diary:
        try:
            diary = mem.list_diary(uid, limit=1000)
        except Exception:
            diary = []

    if fmt == "qq-agent":
        payload = {
            "format": "qq-agent-memory",
            "version": 2,
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "app": "小鲸鱼 Agent",
            "perm": [{"user_id": str(x.get("user_id") or ""),
                      "category": x.get("category") or "other",
                      "content": x.get("content") or "",
                      "importance": x.get("importance") or 3,
                      "created_at": x.get("created_at") or 0} for x in perm],
            "diary": [{"user_id": str(x.get("user_id") or ""),
                       "day": x.get("day") or "",
                       "title": x.get("title") or "",
                       "content": x.get("content") or "",
                       "mood": x.get("mood") or ""} for x in diary],
        }
        return True, json.dumps(payload, ensure_ascii=False, indent=2), \
            "已导出 %d 条永久记忆、%d 篇日记" % (len(perm), len(diary))

    if fmt == "generic-json":
        arr = [{"user_id": str(x.get("user_id") or ""),
                "category": x.get("category") or "other",
                "content": x.get("content") or "",
                "importance": x.get("importance") or 3} for x in perm]
        return True, json.dumps(arr, ensure_ascii=False, indent=2), \
            "已导出 %d 条（通用 JSON 数组）" % len(arr)

    if fmt == "jsonl":
        buf = []
        for x in perm:
            buf.append(json.dumps({
                "user_id": str(x.get("user_id") or ""),
                "category": x.get("category") or "other",
                "content": x.get("content") or "",
                "importance": x.get("importance") or 3,
            }, ensure_ascii=False))
        return True, "\n".join(buf) + ("\n" if buf else ""), \
            "已导出 %d 行 JSONL" % len(buf)

    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["user_id", "category", "content", "importance"])
        for x in perm:
            w.writerow([str(x.get("user_id") or ""), x.get("category") or "other",
                        x.get("content") or "", x.get("importance") or 3])
        return True, out.getvalue(), "已导出 %d 行 CSV（Excel 可直接打开）" % len(perm)

    if fmt == "markdown":
        lines = ["# 小鲸鱼记忆导出", "",
                 "导出时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"), ""]
        by_user = {}
        for x in perm:
            by_user.setdefault(str(x.get("user_id") or ""), []).append(x)
        for uid_key, items in by_user.items():
            lines.append("## %s" % (uid_key or "未指定账号"))
            lines.append("")
            for x in items:
                lines.append("- [%s] %s" % (x.get("category") or "other",
                                            x.get("content") or ""))
            lines.append("")
        return True, "\n".join(lines), "已导出 %d 条（Markdown）" % len(perm)

    # txt
    lines = [str(x.get("content") or "") for x in perm if x.get("content")]
    return True, "\n".join(lines) + ("\n" if lines else ""), \
        "已导出 %d 行纯文本" % len(lines)


# ---------------------------------------------------------------------------
# 导入（写入数据库）
# ---------------------------------------------------------------------------
def apply(mem, parsed, user_id="", replace_conflict=True, import_messages=True):
    """把 parse() 出来的中间结构写进库。返回统计 dict。"""
    res = {"ok": True, "perm_added": 0, "perm_superseded": 0,
           "diary_added": 0, "msg_added": 0, "skipped": 0, "msg": ""}
    if not isinstance(parsed, dict):
        res.update(ok=False, msg="解析结果为空")
        return res
    fallback_uid = str(user_id or "").strip()
    for it in parsed.get("perm") or []:
        uid = str(it.get("user_id") or fallback_uid or "").strip()
        if not uid or not re.fullmatch(r"\d{5,12}", uid):
            res["skipped"] += 1
            continue
        content = _clean_fact(it.get("content"))
        if not content:
            res["skipped"] += 1
            continue
        try:
            _pid, is_new, superseded = mem.add_perm(
                uid, content,
                category=_norm_cat(it.get("category")),
                importance=_clamp_int(it.get("importance"), 1, 5, 3),
                source=it.get("source") or "import",
                replace_conflict=bool(replace_conflict))
        except Exception:
            res["skipped"] += 1
            continue
        if is_new:
            res["perm_added"] += 1
        if superseded:
            res["perm_superseded"] += len(superseded)
    for it in parsed.get("diary") or []:
        uid = str(it.get("user_id") or fallback_uid or "").strip()
        content = str(it.get("content") or "").strip()
        if not uid or not content:
            res["skipped"] += 1
            continue
        try:
            mem.add_diary(uid, it.get("day") or time.strftime("%Y-%m-%d"), content,
                          title=it.get("title") or "", mood=it.get("mood") or "",
                          msg_count=_clamp_int(it.get("msg_count"), 0, 100000, 0))
            res["diary_added"] += 1
        except Exception:
            res["skipped"] += 1
    for it in parsed.get("events") or []:
        uid = str(it.get("user_id") or fallback_uid or "").strip()
        if not uid or not it.get("event_desc"):
            continue
        try:
            mem.add_event(uid, it["event_desc"], it.get("target_time") or time.time())
        except Exception:
            pass
    if import_messages:
        for it in parsed.get("messages") or []:
            sk = str(it.get("session_key") or "").strip()
            if not sk:
                continue
            try:
                mem.add_message(sk, it.get("role") or "user", it.get("content") or "")
                res["msg_added"] += 1
            except Exception:
                pass
    parts = []
    if res["perm_added"]:
        parts.append("%d 条永久记忆" % res["perm_added"])
    if res["perm_superseded"]:
        parts.append("顶掉 %d 条旧记忆" % res["perm_superseded"])
    if res["diary_added"]:
        parts.append("%d 篇日记" % res["diary_added"])
    if res["msg_added"]:
        parts.append("%d 条临时上下文" % res["msg_added"])
    if res["skipped"]:
        parts.append("跳过 %d 条（账号不合法或内容为空）" % res["skipped"])
    res["msg"] = ("导入完成：" + "、".join(parts)) if parts else "没有可导入的内容"
    if not parts:
        res["ok"] = False
    return res


def guess_filename(fmt, uid=None):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    ext = next((f["ext"] for f in FORMATS if f["id"] == fmt), ".txt")
    tail = ("_" + str(uid)) if uid else ""
    return "memory_export%s_%s%s" % (tail, stamp, ext)


def list_formats():
    return [dict(f) for f in FORMATS]


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    for f in FORMATS:
        print(f["id"], f["name"], f["kind"])
    print("detect json:", detect('[{"user_id":"3280735835","content":"喜欢打游戏"}]'))
    print("detect csv:\n ", detect("user_id,category,content\n3280735835,preference,喜欢打游戏"))
    print("detect md:", detect("## 3280735835\n- 喜欢打游戏\n- [work] 在做机器人"))
    ok, data, fmt = parse("user_id,category,content,importance\n"
                          "3280735835,preference,喜欢打游戏,4\n"
                          "3280735835,work,在做QQ机器人,5\n")
    print("csv ->", ok, fmt, data["perm"])
