# -*- coding: utf-8 -*-
"""日记与永久记忆 —— 只有"绑定"的账号才永久保存

============================================================
 核心规则（用户需求）
============================================================
  · 绑定账号：比如把 3280735835 绑上，这个人的记忆才会被提炼成
    "永久记忆"写进数据库（perm_memory 表），永远不会因为上下文太长被丢掉。
  · 没绑定的账号：只走 messages 表里的最近若干条消息作为临时上下文，
    滚动窗口，token 超了就被截断 —— 也就是"临时读取"。
  · 最多绑定 20 个账号（MAX_BOUND_ACCOUNTS）。
  · 日记：每天给绑定账号写一篇"今天聊了什么"的日记，存在 diary 表。
  · 数据库文件固定叫 memory.db，放在程序包目录（exe 同级）。

============================================================
 这个模块负责什么
============================================================
  · 绑定/解绑账号（读写 config.json 的 diary_bound_accounts）
  · 判断某个 user_id 是否已绑定
  · 从对话里提炼永久记忆（调大模型，失败就退回规则抽取）
  · 生成每日日记
  · 给界面提供"临时记忆 / 永久记忆"两份列表

 注意：本模块不 import agent.py（避免循环依赖）。需要调模型时用注入的
 call_model 回调 —— agent.py 启动后会把 ask_model_raw 注册进来。
============================================================
"""
import json
import os
import re
import sys
import threading
import time

import config as cfg_mod


# v1.9.11.2：日记 / 记忆提取前先把 QQ 的 CQ 码洗掉，别让日记里出现
# 「[CQ:image,file=abc123.image,url=...]」这种机器字符串。
_CQ_KEEP = {
       "image": "[图片]", "mface": "[表情]", "face": "[表情]", "emoji": "[表情]",
       "record": "[语音]", "video": "[视频]", "file": "[文件]",
       "forward": "[聊天记录]", "json": "[卡片]", "xml": "[卡片]",
       "share": "[链接]", "music": "[音乐]", "reply": "", "at": "",
}
_CQ_RE = __import__("re").compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


def _clean_cq(text):
       """把 CQ 码换成人类看得懂的占位符（[图片] / [表情] …）。

       [CQ:at,qq=123,name=张三] 这种情况尽量还原成「@张三」，其余未知类型直接删。
       """
       s = str(text or "")
       if "[CQ:" not in s:
           return s

       def _one(m):
           typ = (m.group(1) or "").lower()
           tail = m.group(2) or ""
           if typ == "at":
               nm = ""
               for kv in tail.split(","):
                   if kv.startswith("name="):
                       nm = kv[5:]
               return ("@" + nm) if nm else ""
           if typ == "reply":
               return ""
           return _CQ_KEEP.get(typ, "")

       s = _CQ_RE.sub(_one, s)
       s = s.replace("\r", " ")
       while "  " in s:
           s = s.replace("  ", " ")
       return s.strip()

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



MAX_BOUND_ACCOUNTS = 20
MAX_BOUND_GROUPS = 20

_lock = threading.RLock()

# agent.py 启动后会注册一个 (prompt, system, max_tokens) -> str 的函数
_call_model = None


def set_model_caller(fn):
    """由 agent.py 在启动时调用，把 ask_model_raw 注册进来。

    签名：fn(prompt: str, system: str = None, max_tokens: int = None) -> str
    返回空串表示失败（调用方会退回规则抽取）。
    """
    global _call_model
    _call_model = fn


def has_model():
    return _call_model is not None


# ==================== 绑定管理 ====================
def bound_accounts():
    """已绑定的 QQ 号列表（字符串）"""
    try:
        v = cfg_mod.get("diary_bound_accounts") or []
    except Exception:
        return []
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def is_bound(user_id):
    uid = str(user_id or "").strip()
    return bool(uid) and uid in bound_accounts()


def bind(user_id, note=""):
    """绑定一个账号。返回 (ok, msg)"""
    uid = str(user_id or "").strip()
    if not uid:
        return False, "QQ 号不能为空"
    if not re.fullmatch(r"\d{5,12}", uid):
        return False, f"QQ 号格式不对：{uid}（应该是 5~12 位数字）"
    cur = bound_accounts()
    if uid in cur:
        return True, f"{uid} 已经在绑定列表里了"
    if len(cur) >= MAX_BOUND_ACCOUNTS:
        return False, f"最多绑定 {MAX_BOUND_ACCOUNTS} 个账号，请先解绑一个"
    cur.append(uid)
    try:
        cfg_mod.set_value("diary_bound_accounts", cur)
    except Exception as e:
        return False, f"写入配置失败：{e}"
    return True, f"已绑定 {uid}，从今往后这个号的记忆会永久保存"


def unbind(user_id, keep_data=True):
    """解绑。keep_data=True 时永久记忆不删（只是不再新增）。"""
    uid = str(user_id or "").strip()
    cur = bound_accounts()
    if uid not in cur:
        return False, f"{uid} 本来就没绑定"
    cur = [x for x in cur if x != uid]
    try:
        cfg_mod.set_value("diary_bound_accounts", cur)
    except Exception as e:
        return False, f"写入配置失败：{e}"
    tail = "（已有永久记忆保留，可在记忆页手动删除）" if keep_data else ""
    return True, f"已解绑 {uid}{tail}"


def bindings_with_names():
    """绑定列表 + 每个号的永久记忆条数（界面用）"""
    out = []
    for uid in bound_accounts():
        out.append({"user_id": uid, "perm_count": 0, "diary_count": 0,
                    "last": 0.0})
    return out


def binding_status(mem=None):
    """完整绑定状态，供界面渲染"""
    bound = bound_accounts()
    items = []
    if mem is not None:
        try:
            perm_map = {u["user_id"]: u for u in mem.perm_users()}
        except Exception:
            perm_map = {}
        for uid in bound:
            p = perm_map.get(uid) or {}
            try:
                dcount = mem.count_diary(uid)
            except Exception:
                dcount = 0
            items.append({
                "user_id": uid,
                "perm_count": int(p.get("n") or 0),
                "last": float(p.get("last") or 0),
                "diary_count": dcount,
            })
    else:
        items = [{"user_id": u, "perm_count": 0, "last": 0.0, "diary_count": 0}
                 for u in bound]
    return {
        "ok": True,
        "enabled": bool(cfg_mod.get("diary_enabled", False)),
        "auto_write": bool(cfg_mod.get("diary_auto_write", True)),
        "hour": int(_ndef(cfg_mod.get("diary_hour"), 22)),
        "items": items,
        "count": len(items),
        "max": MAX_BOUND_ACCOUNTS,
    }


# ==================== 群绑定（白名单/黑名单） ====================
def group_bind_mode():
    m = str(cfg_mod.get("group_bind_mode") or "all").strip().lower()
    return m if m in ("all", "whitelist", "blacklist") else "all"


def group_allowed(group_id):
    """这个群要不要回复 / 要不要记记忆"""
    gid = str(group_id or "").strip()
    mode = group_bind_mode()
    if mode == "all":
        return True
    wl = [str(x) for x in (cfg_mod.get("group_whitelist") or [])]
    bl = [str(x) for x in (cfg_mod.get("group_blacklist") or [])]
    if mode == "whitelist":
        return gid in wl
    return gid not in bl


def set_group_mode(mode):
    m = str(mode or "").strip().lower()
    if m not in ("all", "whitelist", "blacklist"):
        return False, "模式只能是 all / whitelist / blacklist"
    cfg_mod.set_value("group_bind_mode", m)
    return True, f"群聊策略已改为 {m}"


def group_list():
    """返回群绑定配置 + 每个群最近有没有消息（界面用）"""
    return {
        "ok": True,
        "mode": group_bind_mode(),
        "whitelist": [str(x) for x in (cfg_mod.get("group_whitelist") or [])],
        "blacklist": [str(x) for x in (cfg_mod.get("group_blacklist") or [])],
        "max": MAX_BOUND_GROUPS,
    }


def add_group(group_id, to_blacklist=False):
    gid = str(group_id or "").strip()
    if not gid:
        return False, "群号不能为空"
    if not re.fullmatch(r"\d{5,12}", gid):
        return False, f"群号格式不对：{gid}"
    key = "group_blacklist" if to_blacklist else "group_whitelist"
    cur = [str(x) for x in (cfg_mod.get(key) or [])]
    if gid in cur:
        return True, f"{gid} 已经在列表里了"
    if len(cur) >= MAX_BOUND_GROUPS:
        return False, f"最多 {MAX_BOUND_GROUPS} 个群，请先移除一个"
    cur.append(gid)
    cfg_mod.set_value(key, cur)
    return True, f"已{'加入黑名单' if to_blacklist else '加入白名单'}：{gid}"


def remove_group(group_id):
    gid = str(group_id or "").strip()
    changed = False
    for key in ("group_whitelist", "group_blacklist"):
        cur = [str(x) for x in (cfg_mod.get(key) or [])]
        if gid in cur:
            cfg_mod.set_value(key, [x for x in cur if x != gid])
            changed = True
    return (True, f"已移除 {gid}") if changed else (False, f"{gid} 不在任何列表里")


# ==================== 永久记忆提炼 ====================
PERM_PROMPT = """从下面这段对话里，提炼出关于「{name}」这个人值得长期记住的信息。

要求：
1. 只输出 JSON 数组，不要任何解释、不要代码块围栏。
2. 每条形如 {{"category":"分类","content":"内容","importance":重要度,"replaces":旧编号或0,"triples":[{{"subject":"主语","predicate":"谓词","object":"宾语"}}]}}
3. category 只能是这几个之一：
   identity(身份/称呼/年龄/所在地)
   preference(喜好/厌恶/习惯)
   relation(亲友关系/宠物/恋爱)
   plan(近期计划/目标/安排)
   health(身体状况)
   work(工作/学习)
   other(其他)
4. content 用第三人称陈述句，15~40 字，不要出现"我""你"。
5. importance 取 1~5，越不容易变、越重要的越高（名字/职业给 5，临时计划给 2）。
6. 只提取对话里**明确说过**的，不要脑补。没有值得记的就输出 []。
7. 最多 6 条。

★ 关于「情感与承诺」（v1.1.4 3.3 强化，这一条优先于上面的条数限制）：
   只要对话里出现下面任何一类，**必须**至少记一条，不许因为「不够客观」就跳过：
   - 情绪状态：难过/失恋/崩溃/焦虑/失眠/累/开心/生气/害怕（category=health 或 other）
   - 关系变化：告白/在一起/分手/吵架/和好/表白被拒（category=relation）
   - 明确承诺或约定：「说好了…」「答应你…」「周末陪你…」「下次…」（category=plan）
   - 强偏好与雷区：「最讨厌…」「绝不能提…」「我一听到…就烦」（category=preference）
   content 照样用第三人称写，但要把情绪和承诺写清楚，别写成干巴巴的日记。

★ 关于 replaces（重要）：
   下面会给一份「已有的长期记忆」清单，每条形如 `#12 (relation) 内容`。
   如果这次对话里的话**纠正或更新**了其中某一条（例如用户说"不对，是2023年"），
   就把那条的编号填进 replaces（如 "replaces": 12）。
   只是补充新信息、不冲突的，replaces 填 0。
   一条新事实最多顶掉 1 条旧的。

★关于 triples（v1.8.0新增，用来织一张"关系网"）：
把这条记忆里**有名有姓的实体关系**拆成三元组，填进 triples。
 - subject / object用具体名词：人名、宠物名、地名、作品名、公司名。
不要用"我""他""她""这个"这类代词，指代不清就留空数组。
 - predicate用2~6个字的动词短语：「养了」「住在」「喜欢」「是…的老板」「计划去」。
 -一条记忆可以拆出0~3组。拆不出来就写 "triples": []，**不要编**。
 -例：content「李四是张三的老板，张三养了一只叫橘子的猫」→
 [{{"subject":"李四","predicate":"是…的老板","object":"张三"}},{{"subject":"张三","predicate":"养了","object":"橘子"}}]

已有的长期记忆（可能为空）：
{existing}

对话：
{dialog}
"""


def _extract_json_array(text):
    """从模型输出里抠出 JSON 数组（容忍代码块和废话）"""
    if not text:
        return []
    s = str(text).strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    m = re.search(r"\[.*\]", s, flags=re.S)
    if m:
        s = m.group(0)
    try:
        data = json.loads(s)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def extract_perm_from_dialog(mem, user_id, name, dialog, max_items=6,
                             replace_conflict=True, fallback_rule=True):
    """从一段对话里提炼永久记忆并写库。

    返回 {"ok","added","items","replaced","msg"}。没有模型时退回规则抽取。

    v5 关键改动：把"已有的长期记忆清单"一起喂给模型，让它自己判断
    这次的话是不是在纠正旧条目 —— 用户抱怨的
    「我说了纪念日是24年11月27日，但旧纪录没有被替换」就是靠这个修的。
    模型给了 replaces 就按它说的顶掉；没给就退回 memory.py 的启发式比对。

    fallback_rule=False 时：模型判定"没东西可记"就真的不记（auto 模式）；
    True 时用正则兜底（always 模式，宁可多记）。
    """
    dialog = _clean_cq(dialog)   # v1.9.11.2：别把 CQ 码塞进永久记忆
    uid = str(user_id)
    if not dialog or not dialog.strip():
        return {"ok": False, "added": 0, "items": [], "replaced": 0, "msg": "对话为空"}

    # 取现有记忆当上下文（限 30 条，避免 prompt 过长）
    existing = []
    try:
        existing = mem.get_perm(uid, limit=30) or []
    except Exception:
        existing = []
    existing_txt = "\n".join(
        f"#{it['id']} ({it.get('category')}) {it.get('content')}" for it in existing
    ) or "（暂无）"

    items = []
    if _call_model is not None:
        try:
            prompt = PERM_PROMPT.format(name=name or uid, dialog=dialog[:3000],
                                        existing=existing_txt[:2000])
            raw = _call_model(prompt, "你是一个精准的信息抽取器，只输出 JSON。",
                              max_tokens=900)
            for obj in _extract_json_array(raw)[:max_items]:
                if not isinstance(obj, dict):
                    continue
                content = str(obj.get("content") or "").strip()
                if not content or len(content) < 4:
                    continue
                rep = obj.get("replaces")
                try:
                    rep_id = int(rep) if rep not in (None, "", 0, "0", "null") else 0
                except (TypeError, ValueError):
                    rep_id = 0
                items.append({
                    "category": str(obj.get("category") or "other").strip().lower(),
                    "content": content[:200],
                    "importance": obj.get("importance", 3),
                    "replaces": rep_id,
                        # v1.8.0：模型顺手给出的关系网三元组（可以是空）
                        "triples": obj.get("triples") or [],
                })
        except Exception:
            items = []

    if not items and fallback_rule:
        items = _rule_extract(dialog)

    added = []
    replaced = 0
    for it in items[:max_items]:
        sup = [it["replaces"]] if it.get("replaces") else None
        pid, is_new, sup_ids = mem.add_perm(
            uid, it["content"],
            category=it.get("category") or "other",
            importance=it.get("importance") or 3,
            source="dialog",
            replace_conflict=bool(replace_conflict),
            supersede_ids=sup)
        if is_new:
            replaced += len(sup_ids or [])
            added.append({"id": pid, **it, "superseded": sup_ids or []})
        # v1.8.0：顺手把关系网织上。失败一律忽略 ——图是增强，不是命脉。
        try:
            if is_new and it.get("triples"):
                import kg as _kgmod
                _kgmod.ingest_triples(mem=mem, user_id=uid,
                    triples=it.get("triples"),
                    source="dialog", perm_id=int(pid or 0))
        except Exception:
            pass
    msg = f"新增 {len(added)} 条永久记忆"
    if replaced:
        msg += f"（替换旧条目 {replaced} 条）"
    return {"ok": True, "added": len(added), "items": added,
            "replaced": replaced, "msg": msg}


# 规则兜底：模型不可用时，用正则从对话里抓明显的自述
_RULE_PATTERNS = [
    (r"我(?:的名字)?叫([\u4e00-\u9fffA-Za-z0-9]{1,12})", "identity", 5, "名字叫{0}"),
    (r"我是(?:一个|一名)?([\u4e00-\u9fffA-Za-z0-9]{2,12})", "identity", 4, "自述是{0}"),
    (r"我(?:今年)?(\d{1,2})岁", "identity", 4, "{0} 岁"),
    (r"我(?:住在|在)([\u4e00-\u9fff]{2,10})(?:工作|上班|读书|上学)", "identity", 4, "在{0}"),
    (r"我(?:养了|有一只)(?:一只)?([\u4e00-\u9fff]{1,8})", "relation", 4, "养了{0}"),
    (r"我(?:喜欢|最爱|超爱)([\u4e00-\u9fffA-Za-z0-9]{1,14})", "preference", 3, "喜欢{0}"),
    (r"我(?:讨厌|不喜欢|受不了)([\u4e00-\u9fffA-Za-z0-9]{1,14})", "preference", 3, "讨厌{0}"),
    (r"我(?:在|做)([\u4e00-\u9fff]{2,12})(?:工作|上班)", "work", 4, "在{0}工作"),
    (r"我(?:要|准备|打算)([\u4e00-\u9fff]{2,16})", "plan", 2, "打算{0}"),
]


def _rule_extract(dialog):
    """没有模型时的规则兜底。宁可少抓，不要抓错。"""
    out = []
    seen = set()
    for pat, cat, imp, tpl in _RULE_PATTERNS:
        for m in re.finditer(pat, dialog):
            word = (m.group(1) or "").strip()
            if not word or len(word) < 1:
                continue
            content = tpl.format(word)
            if content in seen:
                continue
            seen.add(content)
            out.append({"category": cat, "content": content, "importance": imp})
            if len(out) >= 6:
                return out
    return out


# ==================== 每日日记 ====================
DIARY_PROMPT = """你是{name}。请总结最近这段时间和「{who}」的聊天记录。

【你是个什么样的人】（口吻从这里来，但不用复述设定）
{persona}

【最近聊的内容】
{dialog}

要求（这是「记忆快照」，不是文学创作）：
1. 必须提取 **关键事实**：对方的新喜好、新信息、发生的新事件、约定的事（谁、什么、什么时候）。
2. 必须写出 **情绪走向**：这段时间对方什么情绪、你自己什么情绪、有没有转折。
3. 100 字以内，语言精炼，不要客套、不要抒情、不要小标题、不要 markdown。
4. 只写这两类，别写「今天聊得很开心」这种没有信息量的话。
5. 如果确实没什么内容，就写一句「这段时间没聊什么正事」。
6. 最后单独一行输出心情标签，格式：[[mood:开心]] （可选：开心/平静/担心/低落/兴奋/累/烦）
7. 正文和情绪标签之外不要输出任何东西。

快照正文："""


def _perm_brief(mem, uid, today=None):
    """把永久记忆分成「今天新增的」和「以前就有的」两段文字。"""
    today = today or time.strftime("%Y-%m-%d")
    new_lines, old_lines = [], []
    try:
        items = mem.get_perm(uid, limit=60)
    except Exception:
        items = []
    for it in items:
        c = str(it.get("content") or "").strip()
        if not c:
            continue
        created = float(it.get("created_at") or 0)
        cday = time.strftime("%Y-%m-%d", time.localtime(created)) if created else ""
        cat = it.get("category") or "other"
        line = f"- ({_CATEGORY_CN.get(cat, '其他')}) {c}"
        if cday == today:
            new_lines.append(line)
        else:
            old_lines.append(line)
    return ("\n".join(new_lines[-12:]) or "（今天没有新记住的事）",
            "\n".join(old_lines[-18:]) or "（之前也没记下什么）")


_CATEGORY_CN = {"identity": "身份", "preference": "喜好", "relation": "关系",
                "plan": "计划", "health": "身体", "work": "工作", "other": "其他"}


def _persona_brief():
    """取当前人设里的设定文字，给日记定口吻。失败就空着。"""
    try:
        import personas as _p
        return (_p.build_system_prompt() or "")[:1200]
    except Exception:
        return ""


def _parse_diary_output(text):
    """把模型输出拆成 (title, content, mood)"""
    text = (text or "").strip()
    if not text:
        return "", "", ""
    mood = ""
    m = re.search(r"\[\[\s*mood\s*[:：]\s*([^\]]{1,20})\]\]", text, flags=re.I)
    if m:
        mood = m.group(1).strip()
        text = text[:m.start()] + text[m.end():]
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    # 标题取第一行前 24 字（如果第一行很短就当标题）
    lines = [x.strip() for x in text.split("\n") if x.strip()]
    title = ""
    if lines and len(lines[0]) <= 24 and len(lines) > 1:
        title = lines[0]
        text = "\n".join(lines[1:]).strip()
    if not title:
        title = lines[0][:24] if lines else "今天的日记"
    return title, text, mood


def write_diary_for(mem, user_id, name, day=None, force=False, slot=""):
    """给某个账号写今天的日记。返回 {"ok","id","title","msg"}

    v8：把「人设 + 今天新增的长期记忆 + 以往的长期记忆 + 今天的临时对话」
    四样一起喂给模型；手动生成（force=True）时未绑定的账号也能写，
    只是不会进永久记忆库。
    """
    uid = str(user_id)
    day = day or time.strftime("%Y-%m-%d")
    if not is_bound(uid) and not force:
        return {"ok": False, "id": 0, "msg": f"{uid} 未绑定，不写日记"}

    if not force and not slot:
        try:
            if mem.get_diary(uid, day):
                return {"ok": True, "id": 0, "msg": f"{day} 的日记已经写过了"}
        except Exception:
            pass

    # 取当天的消息
    try:
        rows = mem.messages_of(f"private_{uid}", limit=200)
    except Exception:
        rows = []
    today = time.strftime("%Y-%m-%d")
    day_rows = [r for r in rows
                if time.strftime("%Y-%m-%d", time.localtime(r.get("ts") or 0)) == day]
    if not day_rows:
        # 群里提到他的也算
        try:
            for sk in [s["session_key"] for s in mem.session_stats() if s["session_key"].startswith("group_")]:
                for r in mem.messages_of(sk, limit=80):
                    if time.strftime("%Y-%m-%d", time.localtime(r.get("ts") or 0)) == day \
                            and (name and name in (r.get("content") or "")):
                        day_rows.append(r)
        except Exception:
            pass

    min_msgs = int(_ndef(cfg_mod.get("diary_trigger_min_msgs"),
                         _ndef(cfg_mod.get("diary_min_msgs"), 10)))
    if len(day_rows) < min_msgs and not force:
        return {"ok": False, "id": 0,
                "msg": f"这一轮只聊了 {len(day_rows)} 条（少于 {min_msgs} 条），不写快照"}

    dialog = "\n".join(
        f"{'我' if r.get('role') == 'assistant' else name}："
        f"{_clean_cq((r.get('content') or ''))[:200]}"
        for r in day_rows[-80:])

    title, content, mood = "", "", ""
    if _call_model is not None:
        try:
            new_perm, old_perm = _perm_brief(mem, uid, day)
            raw = _call_model(
                DIARY_PROMPT.format(name=name or uid, who=name or uid, day=day,
                                    persona=_persona_brief(),
                                    new_perm=new_perm, old_perm=old_perm,
                                    dialog=dialog[:4000]),
                "你是一个会写日记的普通人，用第一人称，不写客套话。",
                max_tokens=1100)
            title, content, mood = _parse_diary_output(raw)
        except Exception:
            pass

    if not content:
        # 模型不可用：写一份机械但真实的流水
        title = f"{day} 流水"
        content = (f"今天和{name}聊了 {len(day_rows)} 条。\n"
                   + "\n".join(f"· {_clean_cq((r.get('content') or ''))[:60]}" for r in day_rows[-8:]))
        mood = ""

    did = mem.add_diary(uid, day, content, title=title, mood=mood,
                        msg_count=len(day_rows), slot=slot)
    return {"ok": bool(did), "id": did, "title": title, "mood": mood,
            "content": content, "msg": f"已写 {day} 的日记（{len(day_rows)} 条对话）"}


def diary_interval_sec():
    """快照间隔（秒）。v1.9.10：支持分钟/小时两种单位，默认 4 小时。"""
    val = float(_ndef(cfg_mod.get("diary_interval_value"), 4) or 4)
    unit = str(cfg_mod.get("diary_interval_unit") or "hour").strip().lower()
    if val <= 0:
        val = 4
    sec = val * 60.0 if unit.startswith("min") else val * 3600.0
    # 下限 60 秒：防止把模型当免费摘要机
    return max(60.0, sec)


def diary_due():
    """现在该不该写一篇记忆快照（后台线程用）。

    v1.9.10：不再死等「晚上 22 点」，按 diary_interval_value/unit 的间隔判断；
    老配置没有这两个键时，如果设置过 diary_hour 且开着自动写，仍然尊重那个小时。
    """
    if not bool(cfg_mod.get("diary_enabled", False)):
        return False
    if not bool(cfg_mod.get("diary_auto_write", True)):
        return False
    if not bound_accounts():
        return False
    has_interval = (cfg_mod.get("diary_interval_value") is not None
                    or cfg_mod.get("diary_interval_unit") is not None)
    if not has_interval:
        hour = int(_ndef(cfg_mod.get("diary_hour"), 22))
        if time.localtime().tm_hour < hour:
            return False
    try:
        last = float(cfg_mod.get("diary_last_ts") or 0)
    except Exception:
        last = 0.0
    return (time.time() - last) >= diary_interval_sec()


def diary_memory_block(user_id="", hours=24, limit=3, max_chars=600):
    """【你最近的记忆碎片】—— 把最近几篇快照拼进上下文（防断片）。

    v1.9.10：主模型上下文被截断、或者换了个新会话时，靠这个立刻接上话。
    """
    try:
        import memory as _mem_mod
        mem = getattr(diary_memory_block, "_mem", None)
        if mem is None:
            return ""
        rows = mem.recent_diary(user_id=str(user_id or ""), limit=int(limit),
                               hours=float(hours or 0))
    except Exception:
        return ""
    if not rows:
        return ""
    lines = []
    for r in rows:
        txt = re.sub(r"\s+", " ", str(r.get("content") or "")).strip()
        if not txt:
            continue
        when = str(r.get("day") or "")
        if r.get("slot"):
            when = (when + " " + str(r["slot"])).strip()
        mood = str(r.get("mood") or "").strip()
        lines.append("· %s%s：%s" % (when, ("（%s）" % mood) if mood else "", txt))
    if not lines:
        return ""
    out = "【你最近的记忆碎片】\n" + "\n".join(lines)
    return out[:int(max_chars or 600)]


# ==================== 给界面的记忆视图 ====================
def memory_overview(mem, msg_limit=60, perm_limit=200, diary_limit=40):
    """「记忆」Tab 的完整数据：临时上下文 + 永久记忆 + 日记"""
    try:
        temp_sessions = mem.session_stats()
    except Exception:
        temp_sessions = []
    for s in temp_sessions:
        s["last_text"] = time.strftime("%m-%d %H:%M", time.localtime(s.get("last") or 0))
        s["kind"] = "群聊" if str(s["session_key"]).startswith("group_") else "私聊"
        s["bound"] = False
        if s["kind"] == "私聊":
            uid = str(s["session_key"]).replace("private_", "")
            s["user_id"] = uid
            s["bound"] = is_bound(uid)

    try:
        recent = mem.recent_messages_all(msg_limit)
    except Exception:
        recent = []
    for r in recent:
        r["ts_text"] = time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("ts") or 0))
        r["kind"] = "群聊" if str(r["session_key"]).startswith("group_") else "私聊"

    try:
        perms = mem.get_perm_list(perm_limit) if hasattr(mem, "get_perm_list") else []
    except Exception:
        perms = []
    if not perms:
        # 没有全量方法时，按账号逐个取
        perms = []
        try:
            for u in mem.perm_users():
                for p in mem.get_perm(u["user_id"], limit=perm_limit):
                    perms.append(p | {"user_id": u["user_id"]})
        except Exception:
            pass
    for p in perms:
        p["created_text"] = time.strftime("%m-%d %H:%M",
                                          time.localtime(p.get("created_at") or 0))

    try:
        diaries = mem.list_diary(limit=diary_limit)
    except Exception:
        diaries = []
    for d in diaries:
        d["created_text"] = time.strftime("%m-%d %H:%M",
                                          time.localtime(d.get("created_at") or 0))

    try:
        stats = mem.stats()
    except Exception:
        stats = {}

    return {
        "ok": True,
        "temp": {"sessions": temp_sessions, "recent": recent,
                 "count": stats.get("messages", 0)},
        "perm": {"items": perms, "count": stats.get("perm_memory", 0)},
        "diary": {"items": diaries, "count": stats.get("diary", 0)},
        "bound": bound_accounts(),
        "max_bound": MAX_BOUND_ACCOUNTS,
        "diary_enabled": bool(cfg_mod.get("diary_enabled", False)),
        "save_group_memory": bool(cfg_mod.get("save_group_memory", False)),
        "db_path": getattr(mem, "db_path", ""),
        "stats": stats,
    }


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    from memory import Memory
    m = Memory()
    print("绑定:", bound_accounts())
    print("状态:", json.dumps(binding_status(m), ensure_ascii=False))
    print("群策略:", group_bind_mode())
    print("规则抽取测试:", _rule_extract("我叫阿哲，我今年24岁，我养了一只橘猫，我喜欢打游戏"))
    print("日记到期:", diary_due())
    ov = memory_overview(m)
    print("临时会话:", len(ov["temp"]["sessions"]), "永久:", ov["perm"]["count"])
    m.close()
