"""emotion_engine.py — 情感中枢（v1.1.3 任务四）

把「这句话有多急、有多重、该用什么姿态回」交给一个统一的地方算，而不是散落在
各个 if 里。三件事：

1. 预设：常规细腻型 / 直男理性型 / 傲娇型。不同预设给模型的判断口径不同。
2. 打分：返回 {"emotion", "weight" 0~100, "suggestion"}。weight 决定
   「先回谁」——私聊说「我失恋了」(90) 应该排在群里「哈哈哈」(10) 前面。
3. 降级：调模型超过 timeout（默认 3 秒）或解析失败，**静默**退回本地词表，
   绝不因为情感分析把正常回复卡住或搞挂。

所有决策都记在内存环形日志里（最近 200 条），前端「情感中枢」页直接读。
"""

import json
import re
import threading
import time
from collections import deque

# ---------------- 预设 ----------------
PRESETS = {
    "normal": {
        "cn": "常规细腻型",
        "prompt": (
            "你是一个很会读空气的人。判断对方这句话的情绪，越细越好：\n"
            "- 表面情绪（开心/难过/生气/焦虑/疲惫/平静/兴奋/委屈/暧昧）\n"
            "- 强度 0~100（越急、越重、越需要立刻回应就越高）\n"
            "- 回复姿态建议（一句话，具体到语气，不要空话）\n"
        ),
    },
    "rational": {
        "cn": "直男理性型",
        "prompt": (
            "你是偏理性的判断器，不解读潜台词，只看字面需求：\n"
            "- 情绪（正向/中性/负向 + 一句话理由）\n"
            "- 强度 0~100（事情本身多重就多高，不看语气词）\n"
            "- 回复建议（直接给结论式的一句话，别绕）\n"
        ),
    },
    "tsundere": {
        "cn": "傲娇型",
        "prompt": (
            "你表面嘴硬、其实很在意对方。判断情绪时也带点别扭：\n"
            "- 情绪（用带点傲娇的词：嘴硬心软/装不在意/其实很急/有点吃醋…）\n"
            "- 强度 0~100（越在意越高）\n"
            "- 回复建议（先怼半句，再露一点关心）\n"
        ),
    },
}
DEFAULT_PRESET = "normal"

_PROMPT_FMT = (
    "{preset}\n"
    "只输出一个 JSON，不要代码块、不要解释：\n"
    '{{"emotion": "≤6 字", "weight": 0~100 的整数, "suggestion": "≤40 字"}}\n'
    "对方说的话：「{text}」"
)

# ---------------- 本地兜底词表 ----------------
# (正则, 情绪, 权重, 姿态建议)
_LOCAL_RULES = [
    (r"(失恋|分手了|离婚|被甩|出轨)", "情伤", 92, "先接住情绪，别急着讲道理"),
    (r"(难过|好丧|低落|想哭|哭了一|委屈|心酸|难受|撑不住|emo了)", "低落", 74,
     "先承认他不好受，再问一句怎么了"),
    (r"(我好笨|我不行|没用|废物|好失败|比不上)", "自我否定", 76,
     "先否掉这个评价，再给一件他做得到的小事"),
    (r"(滚|闭嘴|烦不烦|讨厌你|傻逼|有病)", "被骂", 66,
     "别对骂也别装委屈，淡淡接一句就停"),
    (r"(我妈|我爸|家里人).{0,6}(住院|走了|去世|手术)", "家里出事", 95, "认真点，少表情包"),
    (r"(自杀|不想活|活不下去|想死)", "危险信号", 98, "立刻认真回应，必要时劝他找人帮忙"),
    (r"(救命|急死|快点|赶紧|在不在|在线等)", "着急", 86, "短句、直接给答案"),
    (r"(加班|通宵|睡不着|失眠|好累|累死|很累|太累|累得|累坏|疲惫|熬不住|(?<![积牵拖连])累)", "疲惫", 64,
     "别贫，温和一点"),
    (r"(生气|气死|烦死|无语|服了|吐了)", "不满", 72, "先顺着说，不要辩解"),
    (r"(考试|面试|答辩|体检|报告|结果).{0,8}(怎么办|好怕|紧张|担心)", "焦虑", 78, "给具体建议，别说空话"),
    (r"(哈哈|嘿嘿|笑死|太好笑|乐死|开心|高兴|太好了|好耶|爽|舒服)", "开心", 34, "跟着乐，可以接梗"),
    (r"(谢谢|多谢|感谢|辛苦了)", "感谢", 40, "不客套，轻轻接一句"),
    (r"(喜欢你|想你|抱抱|亲亲|宝贝|老婆|老公)", "亲昵", 70, "关系够近才接，别过度"),
    # v1.1.3 阶段一：暧昧/撩拨也要能被判出来（用户反馈「发暧昧信息无法触发情感判定」）
    (r"(暧昧|撩|心动|脸红|心跳|想你了|想抱你|想亲你|亲我|抱我|陪我睡)", "暧昧", 82,
     "语气软一点，接住这层意思但别越过界"),
    (r"(宝贝|亲爱的|乖乖|小可爱|我的人)", "亲昵", 74, "关系够近才接，别过度"),
    (r"(再见|晚安|睡了|下线)", "收尾", 30, "简短道别，别硬聊"),
    (r"(在吗|在不在|有人吗)", "探路", 55, "先应一声，等他说事"),
    (r"(为什么|怎么办|如何|能不能|可不可以|帮我)", "求助", 68, "给步骤，不要长篇"),
    (r"(你真棒|厉害|牛啊|好强|做得不错|夸你)", "被夸", 44, "轻松收下，别假谦虚"),
    (r"(陪我|别走|不要不理我|等等我)", "撒娇", 60, "语气软一点，给对方确定的回应"),
]
_COMPILED = [(re.compile(p), e, w, s) for p, e, w, s in _LOCAL_RULES]

# ---------------- v1.8.12：否定与程度 ----------------
# 用户反馈「我不难过」也被判成难过 —— 必须看关键词前面有没有否定词。
# 只往前看 4 个字（「我不太难过」/「我才不难过」/「没有很难过」都够）。
# 「特别」「分别」「个别」里的「别」不是否定词，所以「别」要加个前置排除
_NEG_RE = re.compile(r"(不|没|没有|没多|(?<![特分类个告区派识])别|甭|勿|并不|才不"
                     r"|一点不|一点也不|根本没)[^，。！？,.!?]{0,3}$")

# 程度副词：强化的往上加，弱化的往下减
_DEG_HIGH = re.compile(r"(非常|特别|超级|极其|极度|太|超|巨|贼|真的要|难受得要死|死了)")
_DEG_MID = re.compile(r"(很|挺|蛮|好|真|相当|实在)")
_DEG_LOW = re.compile(r"(有点|有一点|稍微|略微|有些|一点点|些许|不太)")

# 这些情绪被否定时，实际是「在否认」，不要顺着往下引
_NEGATIVE_EMOS = ("低落", "自我否定", "情伤", "焦虑", "疲惫", "不满", "被骂", "危险信号", "家里出事")

_LOG = deque(maxlen=200)
_LOG_LOCK = threading.Lock()


def presets():
    """给前端：预设清单 [{"key","cn"}]。"""
    return [{"key": k, "cn": v["cn"]} for k, v in PRESETS.items()]


def preset_cn(key):
    return (PRESETS.get(str(key or "")) or PRESETS[DEFAULT_PRESET])["cn"]


def analyze_local(text):
    """不调模型，纯本地词表。永远秒回，永远有结果。

    v1.8.12：加了否定词与程度副词。
      「我不难过」→ 不再判成低落（改判「否认情绪」）；
      「非常难过」比「难过」权重更高；「稍微有点难过」低一些。
    """
    raw = str(text or "")
    best = None          # (emo, w, sug)
    neg_hit = None       # 被否定的那条（留着当兜底）
    for rx, emo, w, sug in _COMPILED:
        m = rx.search(raw)
        if not m:
            continue
        # 关键词前面 4 个字里有否定词 → 这条不算数
        head = raw[max(0, m.start() - 5):m.start()]
        if _NEG_RE.search(head):
            # 「别难过」「甭担心」是对方在劝人，不是他自己难过
            if re.search(r"(别|甭|勿)[^，。]{0,2}$", head):
                return {"emotion": "安慰", "weight": 48,
                        "suggestion": "对方在安慰你，别顺势继续诉苦，反过来接住他这句",
                        "src": "local"}
            if neg_hit is None or w > neg_hit[1]:
                neg_hit = (emo, w, sug)
            continue
        w2 = float(w)
        if _DEG_HIGH.search(raw):
            w2 += 14
        elif _DEG_MID.search(raw):
            w2 += 5
        elif _DEG_LOW.search(raw):
            w2 -= 12
        if best is None or w2 > best[1]:
            best = (emo, int(w2), sug)
    if best is None and neg_hit is not None:
        # 全都带否定：「我不难过，就是有点累」这种，给一句「别硬往难受上引」
        return {"emotion": "否认情绪", "weight": 34,
                "suggestion": "对方在否认自己的情绪，别追着问「你是不是不开心」，轻轻接住就好",
                "src": "local"}
    if best is None:
        # 没有任何命中：长度短、带语气词算轻；问句稍高
        w = 30
        if re.search(r"[?？]", raw):
            w = 45
        if len(raw) <= 4:
            w = 25
        emo = "平静"
        sug = "正常接话就行"
        return {"emotion": emo, "weight": w, "suggestion": sug, "src": "local"}
    # 感叹号/连续问号会加急
    w = best[1]
    w = min(100, w + 4 * raw.count("！") + 4 * raw.count("!"))
    w = min(100, w + 3 * raw.count("？"))
    return {"emotion": best[0], "weight": int(w), "suggestion": best[2], "src": "local"}


def _parse_json(text):
    """模型可能加 ```json 包裹或前后废话，尽量把 JSON 抠出来。"""
    s = str(text or "").strip()
    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    try:
        d = json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except Exception:
            return None
    if not isinstance(d, dict):
        return None
    return d


def preset_prompt(preset=None):
    """V1.1.6 补丁 3（m08875 ⑦）：预设的判断口径优先用设置里存的那份。

    用户在「情感中枢」改过某个预设的口径，就以他写的为准；没改过的仍用内置。
    读配置失败时静默退回内置，不影响回复。
    """
    key = str(preset or DEFAULT_PRESET)
    spec = PRESETS.get(key) or PRESETS[DEFAULT_PRESET]
    base = spec["prompt"]
    try:
        import config as _cfg_mod
        custom = (_cfg_mod.get("emotion_preset_prompts") or {}).get(key) or ""
        custom = str(custom).strip()
        if custom:
            return custom, True
    except Exception:
        pass
    return base, False


def analyze(text, preset=None, timeout=3.0, ask=None):
    """情感分析：先本地秒出，再在 timeout 内等模型给出更细的结果。

    ask: 注入的模型调用函数（agent.ask_model_raw），为 None 就只用本地词表。
    返回 {"emotion","weight","suggestion","src"}，src ∈ {"model","local"}。
    任何异常都不抛给调用方 —— 回复链路不能被情感分析搞挂。
    """
    base = analyze_local(text)
    # v1.8.12（任务三）：三档由 emotion_mode 决定
    #   local  ＝ 只用词表（0 延迟，不烧 token）
    #   model  ＝ 只信大模型（模型没给就退回词表，绝不空手）
    #   both   ＝ 现在这样：先用词表兜底，模型在 timeout 内给了就更细
    _mode = "both"
    try:
        import config as _cfg
        _mode = str(_cfg.get("emotion_mode", "both") or "both").lower()
    except Exception:
        _mode = "both"
    if _mode not in ("local", "model", "both"):
        _mode = "both"
    if _mode == "local" or ask is None:
        base["mode"] = _mode
        return base
    try:
        key = str(preset or DEFAULT_PRESET)
        _pp, _custom = preset_prompt(key)
        spec = PRESETS.get(key) or PRESETS[DEFAULT_PRESET]
        prompt = _PROMPT_FMT.format(preset=_pp, text=str(text or "")[:400])
        box = {}

        def _worker():
            try:
                box["r"] = ask(prompt, max_tokens=160, temperature=0.3,
                               effort="none", tag="/情感")
            except Exception as e:
                box["e"] = e

        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        th.join(max(0.2, float(timeout)))
        if th.is_alive() or "r" not in box or not box["r"]:
            base["mode"] = _mode
            return base
        d = _parse_json(box["r"])
        if not d:
            base["mode"] = _mode
            return base
        emo = str(d.get("emotion") or base["emotion"]).strip()[:12]
        try:
            w = int(round(float(d.get("weight"))))
        except Exception:
            w = base["weight"]
        w = max(0, min(100, w))
        sug = str(d.get("suggestion") or base["suggestion"]).strip()[:60]
        if not emo or not sug:
            base["mode"] = _mode
            return base
        return {"emotion": emo, "weight": w, "suggestion": sug, "src": "model",
                "mode": _mode}
    except Exception:
        base["mode"] = _mode
        return base


def plan_hint(decision, preset=None):
    """把「情绪 + 权重 + 预设」折成一句可直接执行的回法（给 prompt 用）。

    用户反馈「判断太笼统，要根据模型性格和当前场景给最优回复方案」：
    光有「焦虑 78」没用，得告诉模型「先干嘛、几句话、什么语气」。
    """
    d = decision or {}
    emo = str(d.get("emotion") or "").strip()
    try:
        w = int(d.get("weight") or 0)
    except Exception:
        w = 0
    sug = str(d.get("suggestion") or "").strip()
    style = {"normal": "语气自然、像熟人", "rational": "先给结论再补一句理由",
             "tsundere": "先嘴硬半句，再露一点关心"}.get(
        str(preset or DEFAULT_PRESET), "语气自然")
    if w >= 85:
        move = "先一句话把人接住（别问「怎么了」），再给一个立刻能做的动作，2 句以内"
    elif w >= 70:
        move = "先顺着情绪应一句，再回应他说的内容，3 句以内"
    elif w >= 50:
        move = "接住话头，就事论事，别展开"
    else:
        move = "正常接话，短一点"
    out = f"对方情绪偏{emo or '平稳'}（强度{w}）；{move}；{style}"
    if sug:
        out += f"；姿态建议：{sug}"
    return out


def log_decision(text, decision, action="", scope=""):
    """记一条决策日志：内存（给同进程）+ SQLite（给控制台进程）。

    为什么两处都写：Agent 由 app_ui 用 subprocess 拉起，是两个进程。
    只写内存时，控制台「情感中枢」永远是 0 条 —— 这是 v1.1.5.1 的真 bug。
    """
    try:
        d = decision or {}
        with _LOG_LOCK:
            _LOG.append({
                "ts": time.time(),
                "time": time.strftime("%H:%M:%S"),
                "text": str(text or "")[:60],
                "emotion": d.get("emotion"),
                "weight": d.get("weight"),
                "suggestion": d.get("suggestion"),
                "src": d.get("src"),
                "action": str(action or "")[:40],
                "scope": str(scope or "")[:24],
            })
    except Exception:
        pass
    try:
        db = _db()
        if db is not None:
            db.log_emotion(scope, text, (decision or {}).get("emotion"),
                           (decision or {}).get("weight"),
                           (decision or {}).get("suggestion"),
                           (decision or {}).get("src") or "local", action)
    except Exception:
        pass


_DB = {}


def _db():
    """懒加载本库，失败返回 None（绝不影响回复）。"""
    if "m" in _DB:
        return _DB["m"]
    try:
        import memory as _mem_mod
        _DB["m"] = _mem_mod.Memory()
    except Exception:
        _DB["m"] = None
    return _DB["m"]


def log_rows(limit=60):
    db = _db()
    if db is not None:
        try:
            rows = db.list_emotion_log(limit)
            if rows:
                return rows
        except Exception:
            pass
    with _LOG_LOCK:
        return list(_LOG)[-int(limit):][::-1]


def stats(days=7):
    db = _db()
    if db is not None:
        try:
            st = db.emotion_stats(days)
            if st.get("total"):
                return st
        except Exception:
            pass
    with _LOG_LOCK:
        rows = list(_LOG)
    n = len(rows)
    by_model = sum(1 for r in rows if r.get("src") == "model")
    hi = sum(1 for r in rows if (r.get("weight") or 0) >= 80)
    avg = int(sum(r.get("weight") or 0 for r in rows) / n) if n else 0
    return {"total": n, "by_model": by_model, "by_local": n - by_model,
            "high": hi, "avg_weight": avg, "days": int(days)}


def clear_log():
    db = _db()
    if db is not None:
        try:
            db.clear_emotion_log()
        except Exception:
            pass
    with _LOG_LOCK:
        _LOG.clear()
    return True
