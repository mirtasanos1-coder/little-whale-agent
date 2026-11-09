# -*- coding: utf-8 -*-
"""情感感知与管理（v1.1.1）

不是「大模型再猜一遍情绪」，而是一层轻量、可解释的规则：
词表 + 标点 + 表情 + 强度副词，给出一句话的情绪倾向，
再给提示词/好感度/记忆一个可落地的信号。

对外：
    analyze(text)        -> {"mood","score","intensity","tags","hint"}
    mood_line(text)      -> 直接能塞进提示词的一行中文（情绪不明显时返回 ""）
    affinity_delta(text) -> 这一句对好感度的增减建议（-2 ~ +3）
    summary(items)       -> 一批文本的总体情绪

设计原则：
1. 宁可不判，也别乱判：命中词太少就返回「平静」，hint 给空串；
2. 负面优先：一句话里既有「开心」又有「烦」，按负面算（人更在意坏消息）；
3. 不引入任何依赖，纯标准库，方便打包。
"""
import re

MOODS = ("开心", "平静", "低落", "生气", "焦虑", "兴奋")

# 词表：每个情绪一组词，命中就记分。词尽量短，避免误伤（比如「笑」不能单用）
_LEX = {
    "开心": ("开心", "高兴", "哈哈", "嘿嘿", "舒服", "爽", "美滋滋", "太好了",
             "谢谢", "喜欢", "好耶", "笑死", "逗", "可爱", "棒"),
    "低落": ("难过", "难受", "不开心", "失落", "委屈", "想哭", "哭了", "累",
             "疲倦", "没意思", "无聊", "emo", "算了", "没劲", "倒霉"),
    "生气": ("烦", "气死", "讨厌", "滚", "闭嘴", "无语", "离谱", "凭什么",
             "忍不了", "别烦我", "过分", "恶心", "草"),
    "焦虑": ("紧张", "害怕", "担心", "焦虑", "睡不着", "怎么办", "来不及",
             "慌", "压力", "考试", "面试", "deadline", "赶"),
    "兴奋": ("激动", "兴奋", "赢了", "太强了", "牛", "绝了", "冲", "期待",
             "等不及", "终于"),
}

# 强度副词/符号：放大或收窄
_STRONG = ("特别", "非常", "超级", "巨", "极", "太", "真的", "真的真的", "!!", "！！")
_WEAK = ("有点", "稍微", "还行", "还好", "一点点")

_EMOJI_MAP = {
    "😂": "开心", "😄": "开心", "😊": "开心", "🥰": "开心", "😭": "低落",
    "😢": "低落", "😞": "低落", "😡": "生气", "😤": "生气", "😰": "焦虑",
    "😨": "焦虑", "😳": "焦虑", "🤩": "兴奋", "🔥": "兴奋", "💢": "生气",
}


def _hits(text, words):
    return sum(1 for w in words if w and w in text)


def analyze(text):
    """判一句话的情绪。返回 dict，字段固定，永不抛异常。"""
    raw = str(text or "")
    if not raw.strip():
        return {"mood": "平静", "score": 0, "intensity": 0.0, "tags": [], "hint": ""}

    low = raw.lower()
    scores = {m: _hits(low, ws) for m, ws in _LEX.items()}
    for ch, mood in _EMOJI_MAP.items():
        if ch in raw:
            scores[mood] = scores.get(mood, 0) + 1

    # 标点：!!! 放大，…… 表示欲言又止（偏低落）
    strong = _hits(low, _STRONG) + min(3, raw.count("!") + raw.count("！"))
    weak = _hits(low, _WEAK)
    if "……" in raw or "..." in raw:
        scores["低落"] = scores.get("低落", 0) + 1

    # 负面优先
    order = ("生气", "焦虑", "低落", "兴奋", "开心")
    mood, best = "平静", 0
    for m in order:
        if scores.get(m, 0) > best:
            mood, best = m, scores[m]

    if best == 0:
        return {"mood": "平静", "score": 0, "intensity": 0.0, "tags": [], "hint": ""}

    intensity = min(1.0, 0.25 * best + 0.15 * strong - 0.1 * weak)
    intensity = max(0.1, round(intensity, 2))

    tags = []
    if len(raw) > 60:
        tags.append("长文")
    if re.search(r"[?？]{1,}", raw):
        tags.append("在问")
    if raw.count("!") + raw.count("！") >= 2:
        tags.append("情绪重")

    hints = {
        "开心": "对方心情不错，可以顺着一起高兴，别泼冷水",
        "低落": "对方状态不太好，先接住情绪、少讲道理，别急着给方案",
        "生气": "对方在气头上，先认下情绪、别对线，别用说教口吻",
        "焦虑": "对方有点慌，给一句稳的、具体的，别空泛安慰",
        "兴奋": "对方很兴奋，接住这个劲儿，别冷淡回应",
    }
    hint = hints.get(mood, "")
    if intensity >= 0.7 and hint:
        hint += "（情绪比较明显，只回一到两句就够）"

    return {"mood": mood, "score": best, "intensity": intensity,
            "tags": tags, "hint": hint}


# v1.1.2 任务一：紧急度。1.0 = 立刻秒回，0.0 = 纯闲聊可以慢一点。
# 只做加法不做减法：情绪重 + 求救词 + 负面情绪 = 高紧急。
_URGENT_WORDS = ("救命", "救我", "急", "快点", "快回", "在吗", "撑不住",
                 "崩溃", "想死", "受不了", "哭", "睡不着", "怎么办")
_URGENT_HARD = ("救命", "救我", "撑不住", "崩溃", "想死", "受不了", "救救我")


def urgency(text):
    """0.0~1.0 的紧急度；判不出来给 0.0，永不抛异常。"""
    try:
        raw = str(text or "")
        r = analyze(raw)
        u = 0.0
        if r["mood"] in ("低落", "生气", "焦虑"):
            u += 0.45 + 0.35 * float(r["intensity"] or 0)
        elif r["mood"] in ("兴奋", "开心"):
            u += 0.15 * float(r["intensity"] or 0)
        low = raw.lower()
        hit = sum(1 for w in _URGENT_WORDS if w and w in low)
        u += min(0.6, 0.3 * hit)
        if hit:
            u = max(u, 0.35)
        # 「救命/崩溃/想死/撑不住」这种，一句就够判定成紧急（0.85 → 秒回）
        if any(w and w in low for w in _URGENT_HARD) or hit >= 2:
            u = max(u, 0.85)
        if raw.count("!") + raw.count("！") >= 2:
            u += 0.1
        if "在吗" in low or "在不在" in low:
            u += 0.15
        return round(max(0.0, min(1.0, u)), 2)
    except Exception:
        return 0.0


def mood_line(text):
    """能直接塞进提示词的一行；情绪不明显就返回空串（不占篇幅）。"""
    r = analyze(text)
    if not r["hint"]:
        return ""
    return f"【对方情绪】{r['mood']}（强度 {int(r['intensity'] * 100)}%）—— {r['hint']}"


_GOOD = ("谢谢", "辛苦了", "厉害", "喜欢你", "抱抱", "可爱", "好听", "贴心")
_BAD = ("滚", "闭嘴", "烦死", "讨厌你", "别说", "不想理")


def affinity_delta(text):
    """这句话该加还是该减好感度（现有好感度只在「说好听话」时加，这里补负面）。"""
    low = str(text or "").lower()
    d = 0
    if any(w in low for w in _GOOD):
        d += 2
    if any(w in low for w in _BAD):
        d -= 2
    return max(-2, min(3, d))


def summary(items):
    """一批消息的总体情绪（给「活人感」页或日记用）。"""
    total = {}
    for it in items or []:
        r = analyze(it)
        if r["mood"] != "平静":
            total[r["mood"]] = total.get(r["mood"], 0) + r["intensity"]
    if not total:
        return {"mood": "平静", "counts": {}}
    mood = max(total, key=total.get)
    return {"mood": mood, "counts": {k: round(v, 2) for k, v in total.items()}}


if __name__ == "__main__":
    for s in ("今天好累啊，什么都不想干", "哈哈哈绝了这也太强了！！",
              "烦死了你怎么什么都问", "明天面试有点紧张，睡不着"):
        print(s, "->", analyze(s))
