# -*- coding: utf-8 -*-
"""纯函数与工具 —— 从 agent.py 原样搬过来的那一部分（V1.9.26 拆分第一步）。

这里放的全是**不碰数据库、不碰网络、不发消息**的东西，所以搬出来最安全：
  · 日志（log）
  · 文本清洗（strip_think / clean_reply / 「记住:」控制行剥离 / normalize_messages）
  · token 估算（estimate_tokens / message_tokens）
  · 时间感知（time_context / time_point_context / 中文钟点换算）
  · 卡片解析（_card_from_json / _card_from_xml / QQ 小程序、B 站分享…）
  · 连发合并的文本归一（_merge_items / _strip_merge_shell）

**纪律**：函数体一行没改，连原始的 agent.py 行号都写在每块前面的注释里，
方便以后对账。唯一的改动是 `AGENT_LOG_FILE` 改成从 config 的数据目录算
（原来它依赖 agent 里重复实现的一份 `_resolve_data_dir()`，逻辑与
`config._data_dir()` 完全一致，这里直接用 config 的，避免两份目录解析漂移）。

**不要**在这个文件里 `import agent` —— agent 会在顶部反向 import 本模块，
那会变成循环引用。真要读 agent 的活配置，用 `core.globals.live("名字")`。
"""
import json
import os
import re
import sys
import threading
import time

import config as cfg_mod

# ------------------------------------------------------------------
# 搬自 agent.py L122-122：MAX_REPLY_CHARS
# ------------------------------------------------------------------
MAX_REPLY_CHARS = 900


# ------------------------------------------------------------------
# 搬自 agent.py L138-138：ENABLE_TIME_AWARENESS
# ------------------------------------------------------------------
ENABLE_TIME_AWARENESS = True


def _switch(name, default):
    """读一个「开关型」常量，优先取 agent 里的当前值。

    为什么不能直接读本模块的同名变量：拆分前这些开关住在 agent.py 里，
    有人（设置页、自测、插件）会写 gent.ENABLE_TIME_AWARENESS = False 来临时关掉它。
    搬出来以后如果只读本模块的副本，那种「运行时改一下」就失效了 —— 行为会变。
    所以开关类一律现取 agent 的值，拿不到才退回本模块的副本。
    """
    try:
        from core import globals as _g
        v = _g.live(name, None)
        return bool(default) if v is None else bool(v)
    except Exception:
        return bool(default)


# ------------------------------------------------------------------
# 搬自 agent.py L184-184：MAX_CONTEXT_TOKENS
# ------------------------------------------------------------------
MAX_CONTEXT_TOKENS = 6000      # 整包 messages 的 token 预算（含 system）


# ------------------------------------------------------------------
# 搬自 agent.py L186-186：RESERVE_OUTPUT_TOKENS
# ------------------------------------------------------------------
RESERVE_OUTPUT_TOKENS = 800    # 给模型回复预留


# ------------------------------------------------------------------
# 搬自 agent.py L187-187：MIN_KEEP_MESSAGES
# ------------------------------------------------------------------
MIN_KEEP_MESSAGES = 2          # 至少保留最近 N 条历史


# ------------------------------------------------------------------
# 搬自 agent.py L1314-1314：WEEKDAYS_CN
# ------------------------------------------------------------------
WEEKDAYS_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


# ------------------------------------------------------------------
# 搬自 agent.py L1317-1364：time_context
# ------------------------------------------------------------------
def time_context():
    """生成当前时间上下文，注入到每次对话

    v1.1.1：用户反馈「13:02 发早上好，机器人只回『早啊』，根本没意识到时间不对」。
    所以这里不止报个时间，还要把「这个点通常在干嘛」和「对方说的和现在对不上就点出来」
    一起写进去，模型才知道该接什么话。
    """
    now = time.localtime()
    hour = now.tm_hour
    if 5 <= hour < 9:
        period, doing = "清晨", "刚起床/在洗漱/出门路上"
    elif 9 <= hour < 12:
        period, doing = "上午", "在上课或上班"
    elif 12 <= hour < 14:
        period, doing = "中午", "刚吃完饭、在午休或摸鱼"
    elif 14 <= hour < 18:
        period, doing = "下午", "在忙、快下班/放学了"
    elif 18 <= hour < 23:
        period, doing = "晚上", "吃完饭、在休息"
    else:
        period, doing = "深夜", "该睡了/在熬夜"

    weekend = now.tm_wday >= 5
    day_kind = "周末" if weekend else "工作日"
    meal = ""
    if 6 <= hour < 9:
        meal = "早饭时间"
    elif 11 <= hour < 13:
        meal = "午饭时间"
    elif 17 <= hour < 19:
        meal = "晚饭时间"

    tips = [
        f"现在是{day_kind}{period}（{doing}）",
        "深夜（0-5点）对方还没睡，可以顺口问一句怎么还没睡；" if hour < 5 else "",
        "早上可以问早安、问吃没吃；" if 5 <= hour < 10 else "",
        f"现在是{meal}，可以自然提一句吃了什么；" if meal else "",
        "不要每句话都报时间，只在合适的时候顺带提一下；",
        ("如果对方说的话和现在的时间对不上（比如中午/下午说「早上好」、白天说「晚安」），"
         "要自然地点出来（刚睡醒？通宵了？），别顺着装傻，也别生硬地纠正；"
         "这就是「活人感」：先接住对方，再带一句时间上的观察。"),
    ]
    extra = "".join(t for t in tips if t)
    return (
        f"\n\n【当前时间】{now.tm_year}年{now.tm_mon}月{now.tm_mday}日 "
        f"{WEEKDAYS_CN[now.tm_wday]} {hour:02d}:{now.tm_min:02d}（{period}）\n"
        f"{extra}"
    )


# ------------------------------------------------------------------
# 搬自 agent.py L1374-1377：_HOUR_CN
# ------------------------------------------------------------------
_HOUR_CN = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
            "〇": 0, "０": 0, "１": 1, "２": 2, "３": 3, "４": 4, "５": 5,
            "６": 6, "７": 7, "８": 8, "９": 9}


# ------------------------------------------------------------------
# 搬自 agent.py L1380-1404：_norm_hour
# ------------------------------------------------------------------
def _norm_hour(tok):
    """把「4」「04」「四」「十」这类钟点转成 0-23 的整数；转不了返回 None。"""
    s = str(tok or "").strip()
    if not s:
        return None
    if s.isdigit():
        v = int(s)
        return v if 0 <= v <= 23 else None
    if s in _HOUR_CN:
        return _HOUR_CN[s]
    # 「十一点」「十二点」「二十三点」这种
    m = re.match(r"^([一二两三四五六七八九十]{1,3})$", s)
    if m:
        t = m.group(1)
        if t == "十":
            return 10
        if len(t) == 2 and t[0] == "十":          # 十一 ~ 十九
            return 10 + _HOUR_CN.get(t[1], 0)
        if len(t) == 3 and t[1] == "十":          # 二十一 ~ 二十三
            a, b = _HOUR_CN.get(t[0]), _HOUR_CN.get(t[2])
            if a is not None and b is not None:
                return a * 10 + b
        if len(t) == 1:
            return _HOUR_CN.get(t)
    return None


# ------------------------------------------------------------------
# 搬自 agent.py L1408-1413：_TP_RE
# ------------------------------------------------------------------
_TP_RE = re.compile(
    r"(上午|早上|早晨|凌晨|中午|下午|傍晚|晚上|夜里|今晚|今早|明早|明天|后天|昨天|今天|周[一二三四五六日天]|星期[一二三四五六日天])?"
    r"\s*(\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*"
    r"(?:点|电|時|时|:|：)\s*"
    r"(\d{1,2}|[一二三四五六七八九十]{1,2})?\s*(分|半)?"
)


# ------------------------------------------------------------------
# 搬自 agent.py L1416-1552：time_point_context
# ------------------------------------------------------------------
def time_point_context(user_text):
    """从用户这句话里挑出钟点，算成「今天是几号星期几、那个点过了没有」。

    只在真的提到钟点时才返回内容，否则返回空串（不污染 prompt、不破坏缓存）。
    """
    if not _switch("ENABLE_TIME_AWARENESS", ENABLE_TIME_AWARENESS):
        return ""
    txt = str(user_text or "")
    if not txt or len(txt) > 2000:
        return ""
    try:
        hits = list(_TP_RE.finditer(txt))
    except Exception:
        return ""
    if not hits:
        return ""

    now = time.localtime()
    now_min = now.tm_hour * 60 + now.tm_min
    today = f"{now.tm_year}年{now.tm_mon}月{now.tm_mday}日"

    def _day_kind(c):
        if c in ("明天", "明早"):
            return "明天"
        if c == "后天":
            return "后天"
        if c == "昨天":
            return "昨天"
        return "今天"

    def _rel(tgt):
        """相对现在（分钟数）的说法"""
        diff = now_min - tgt
        if diff > 0:
            hh, mm = divmod(diff, 60)
            return f"已经过去 {hh} 小时 {mm} 分钟" if hh else f"已经过去 {mm} 分钟"
        if diff == 0:
            return "就是现在"
        left = -diff
        hh, mm = divmod(left, 60)
        return f"还有 {hh} 小时 {mm} 分钟才到"

    lines = []
    seen = set()
    for m in hits[:3]:
        prefix, htok, mtok, munit = m.group(1) or "", m.group(2), m.group(3), m.group(4)
        base = _norm_hour(htok)
        if base is None:
            continue
        minute = 0
        if munit == "半":
            minute = 30
        elif mtok:
            try:
                minute = int(mtok) if str(mtok).isdigit() else _HOUR_CN.get(mtok, 0)
            except Exception:
                minute = 0
        if minute > 59:
            minute = 0
        quoted = m.group(0).strip()
        kind = _day_kind(prefix)

        # v1.9.24：没讲上午/下午的钟点是有歧义的 —— 用户说「我四电下课」，
        # 现在 20:17，那到底指今天凌晨 04:00（过了 16 小时）还是下午 16:00（过了 4 小时）？
        # 以前这里默认读成 04:00，越说越错。现在两个都算出来、把更像的那个写在前面，
        # 让模型按语境挑，而不是被我们误导。
        explicit = prefix in ("上午", "早上", "早晨", "凌晨", "中午", "下午",
                             "傍晚", "晚上", "夜里", "今晚", "今早", "明早")
        if explicit:
            hour = base
            if prefix in ("下午", "傍晚", "晚上", "夜里", "今晚") and hour < 12:
                hour += 12
            if prefix in ("凌晨", "夜里") and hour == 12:
                hour = 0
            if hour > 23:
                continue
            cands = [(hour, minute)]
        elif base == 12:
            cands = [(12, minute), (0, minute)]
        elif base <= 11:
            cands = [(base, minute), (base + 12, minute)]
        else:
            cands = [(base, minute)]
        cands = [c for c in cands if c not in seen]
        if not cands:
            continue

        if len(cands) == 1:
            primary = cands[0]
        elif kind == "今天":
            # 偏向「已经过去的那个」（聊天多是说刚发生的事），同类再比谁离现在近
            primary = min(cands, key=lambda c: (0 if c[0] * 60 + c[1] <= now_min else 1,
                                                abs(now_min - (c[0] * 60 + c[1]))))
        else:
            primary = cands[0]        # 明天/昨天这类，按「上午」读最常见（明天九点考试）
        seen.add(primary)

        h0, m0 = primary
        alts = [c for c in cands if c != primary]
        if kind == "今天":
            if not alts:
                diff = now_min - (h0 * 60 + m0)
                if diff > 0:
                    lines.append(
                        f'- 对方说的「{quoted}」= {today} {h0:02d}:{m0:02d}，'
                        f"是**今天**、而且{_rel(h0 * 60 + m0)}了 —— "
                        f"别说成以后的事，也别问「到时候怎样」。"
                    )
                elif diff == 0:
                    lines.append(f'- 对方说的「{quoted}」= 就是现在（{h0:02d}:{m0:02d}）。')
                else:
                    lines.append(
                        f'- 对方说的「{quoted}」= {today} {h0:02d}:{m0:02d}，'
                        f"是**今天**，{_rel(h0 * 60 + m0)}。"
                    )
            else:
                alt_txt = "；".join(
                    f"也可能指今天 {c[0]:02d}:{c[1]:02d}（{_rel(c[0] * 60 + c[1])}）" for c in alts)
                lines.append(
                    f'- 对方说的「{quoted}」= 钟点 {htok} 点，没讲上午还是下午。'
                    f"按现在（{now.tm_hour:02d}:{now.tm_min:02d}）最可能是今天 "
                    f"{h0:02d}:{m0:02d}（{_rel(h0 * 60 + m0)}），{alt_txt}。"
                    f"用最可能的那个回答，别自己另推。"
                )
        else:
            tail = f"（今天{now.tm_mon}月{now.tm_mday}日，{WEEKDAYS_CN[now.tm_wday]}）"
            if not alts:
                lines.append(f'- 对方说的「{quoted}」= {kind} {h0:02d}:{m0:02d}{tail}。')
            else:
                alt_txt = "；".join(f"也可能指 {kind} {c[0]:02d}:{c[1]:02d}" for c in alts)
                lines.append(f'- 对方说的「{quoted}」= {kind} {h0:02d}:{m0:02d}{tail}，'
                             f"没说上午下午，按一般作息是这个点；{alt_txt}。")

    if not lines:
        return ""
    return ("\n\n【对方话里的时间点】已经把上面这句话里的钟点换算好了，"
            "直接用这个结论回答，不要自己重新推：\n" + "\n".join(lines))


# ----------------------------------------------------------------
# 日志文件位置（原来在 agent.py L1572-1582，紧挨着 _resolve_data_dir）
# agent 里那份 DATA_DIR 是 _resolve_data_dir() 算的，和 config._data_dir() 逻辑逐行相同
# （环境变量 QQ_AGENT_DATA_DIR > 打包态 exe 同级 > 脚本目录），所以这里直接用 config 的，
# 免得将来只改一处、日志写去别的目录。
# ----------------------------------------------------------------
AGENT_LOG_FILE = os.path.join(cfg_mod.DATA_DIR, "agent.log")
_log_lock = threading.Lock()
# 控制台管道还活着吗（GUI 关掉输出管道后会变 False）。只服务 log()，所以跟着搬过来。
_CONSOLE_ALIVE = True


# 搬自 agent.py L1582-1611：log
# ------------------------------------------------------------------
def log(msg, level="INFO"):
    """格式化写日志：控制台 + agent.log，带时间戳。绝不因编码/管道问题打断主流程。

    v1.3.3 修（用户实机反馈）：GUI 把 stdio 管道关掉之后，print(flush=True) 会抛
    OSError: [Errno 22] Invalid argument（Windows 上写一个读端已关闭的管道）。
    老代码只捕获 Unicode 系列，于是第一条日志就把异常抛给调用方 ——
    「启动顺序判定 → 出话」那一步就显示「出错：[Errno 22] Invalid argument」，
    看着像网络/URL 问题，其实是控制台没了。现在第一次写失败就把
    _CONSOLE_ALIVE 置 False，之后只写文件，log() 永不抛。
    """
    global _CONSOLE_ALIVE
    line = time.strftime("[%Y-%m-%d %H:%M:%S] ") + f"[{level}] " + str(msg)
    if _CONSOLE_ALIVE:
        try:
            print(line, flush=True)
        except (UnicodeEncodeError, UnicodeError):
            try:
                enc = sys.stdout.encoding or "utf-8"
                print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)
            except Exception:
                _CONSOLE_ALIVE = False
        except Exception:
            # 管道断了（Errno 22 等）—— 之后只写文件，不再碰 stdout
            _CONSOLE_ALIVE = False
    try:
        with _log_lock:
            with open(AGENT_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------
# 搬自 agent.py L1696-1703：strip_think
# ------------------------------------------------------------------
def strip_think(text):
    """移除思考块"""
    if not text:
        return ""
    text = re.sub(r"<\|?think\|?>.*?</\|?think\|?>", "", text, flags=re.S | re.I)
    text = re.sub(r"^\s*Thinking Process:.*?(?=\n\n|$)", "", text, flags=re.S | re.I)
    text = re.sub(r"<\|?im_end\|?>|<\|?im_start\|?>", "", text)
    return text.strip()


# ------------------------------------------------------------------
# 搬自 agent.py L1710-1710：_REMEMBER_RE
# ------------------------------------------------------------------
_REMEMBER_RE = re.compile(r"^[ \t]*[-*·•]?[ \t]*记住[ \t]*[:：][ \t]*(.+?)[ \t]*$", re.M)


# ------------------------------------------------------------------
# 搬自 agent.py L1712-1712：_TLS
# ------------------------------------------------------------------
_TLS = threading.local()


# ------------------------------------------------------------------
# 搬自 agent.py L1715-1720：_remember_begin
# ------------------------------------------------------------------
def _remember_begin():
    """在「准备请求模型」时调用：清空本线程的「记住:」收集槽。"""
    try:
        _TLS.remember = []
    except Exception:
        pass


# ------------------------------------------------------------------
# 搬自 agent.py L1723-1730：_remember_take
# ------------------------------------------------------------------
def _remember_take():
    """取走本线程收集到的「记住:」条目（取完就清，避免复用上一次的）。"""
    try:
        items = getattr(_TLS, "remember", None) or []
        _TLS.remember = []
        return [str(x).strip() for x in items if str(x).strip()]
    except Exception:
        return []


# ------------------------------------------------------------------
# 搬自 agent.py L1733-1754：_split_remember
# ------------------------------------------------------------------
def _split_remember(text):
    """把「记住: xxx」这类控制行从正文里剥出来，返回 (正文, [条目...])。"""
    if not text or "记住" not in text:
        return text, []
    items = []

    def _take(m):
        try:
            items.append(m.group(1).strip())
        except Exception:
            pass
        return ""

    cleaned = _REMEMBER_RE.sub(_take, text)
    if not items:
        return text, []
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    sink = getattr(_TLS, "remember", None)
    if isinstance(sink, list):
        sink.extend(items)
    return cleaned, items


# ------------------------------------------------------------------
# 搬自 agent.py L1757-1774：clean_reply
# ------------------------------------------------------------------
def clean_reply(text):
    text = strip_think(text)
    text = re.sub(r"```[a-zA-Z]*\n?", "", text)
    # v1.3.2（QA BUG-08）：先剥「记住:」控制行，再走后面那些格式清理
    # v1.9.24 提醒：这一步会把「- 记住: （人名）事实」整行挖空 —— 而记忆提取的
    # prompt 恰恰要求模型输出这个格式。所以结构化提取绝不能走 clean_reply，
    # 必须走 _ask_text(raw=True)（只 strip_think）。
    text, _rmb = _split_remember(text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"^\s*(小鲸鱼|鲸鲸)\s*[:：]\s*", "", text)
    text = text.strip()
    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS].rstrip() + "……"
    return text


# ------------------------------------------------------------------
# 搬自 agent.py L1783-1783：_CJK_RE
# ------------------------------------------------------------------
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


# ------------------------------------------------------------------
# 搬自 agent.py L1786-1796：estimate_tokens
# ------------------------------------------------------------------
def estimate_tokens(text):
    """粗略估算 token 数：中日韩字符按 1 token，其余按 4 字符 1 token。"""
    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int(cjk + other / 4) + 1


# ------------------------------------------------------------------
# 搬自 agent.py L1799-1814：message_tokens
# ------------------------------------------------------------------
def message_tokens(msg):
    """单条消息的 token 估算（图片按固定值计，避免 base64 撑爆估算）"""
    content = msg.get("content")
    if isinstance(content, str):
        return estimate_tokens(content) + 4
    if isinstance(content, list):
        total = 4
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                total += estimate_tokens(part.get("text", ""))
            elif part.get("type") == "image_url":
                total += 400  # 图片按固定 token 预算计
        return total
    return 4


# ------------------------------------------------------------------
# 搬自 agent.py L1818-1822：_as_content_list
# ------------------------------------------------------------------
def _as_content_list(content):
    """把 content 统一成 list[part]，保留多模态结构"""
    if isinstance(content, list):
        return list(content)
    return [{"type": "text", "text": "" if content is None else str(content)}]


# ------------------------------------------------------------------
# 搬自 agent.py L1825-1840：_merge_contents
# ------------------------------------------------------------------
def _merge_contents(prev, cur):
    """合并两条同 role 消息的 content。

    - 两条都是纯文本：换行拼接
    - 任一条是 list：转成 list 后拼接 part，保持图文结构不被破坏
    """
    if isinstance(prev, str) and isinstance(cur, str):
        return (prev + "\n" + cur).strip()
    parts = _as_content_list(prev) + _as_content_list(cur)
    # 去掉空文本 part
    cleaned = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text" and not (p.get("text") or "").strip():
            continue
        cleaned.append(p)
    return cleaned if cleaned else [{"type": "text", "text": ""}]


# ------------------------------------------------------------------
# 搬自 agent.py L1843-1857：_content_is_empty
# ------------------------------------------------------------------
def _content_is_empty(content):
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text" and (p.get("text") or "").strip():
                return False
            if p.get("type") == "image_url":
                return False
        return True
    return False


# ------------------------------------------------------------------
# 搬自 agent.py L1860-1933：normalize_messages
# ------------------------------------------------------------------
def normalize_messages(messages, max_tokens=MAX_CONTEXT_TOKENS):
    """把 messages 洗成 LM Studio 一定接受的结构。

    规则：
    1. 丢弃非法 role、空 content
    2. system 消息全部提到最前面
    3. 连续相同 role 自动合并（绝不出现两条连续 user / assistant）
    4. 首条非 system 必须是 user（丢弃开头的 assistant）
    5. 超出 token 预算时，从最旧的历史开始丢，保证最近对话完整
    """
    systems, convo = [], []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role not in ("system", "user", "assistant"):
            continue
        content = m.get("content")
        if _content_is_empty(content):
            continue
        item = {"role": role, "content": content}
        if role == "system":
            systems.append(item)
        else:
            convo.append(item)

    # 合并连续相同 role
    merged = []
    for item in convo:
        if merged and merged[-1]["role"] == item["role"]:
            merged[-1]["content"] = _merge_contents(merged[-1]["content"], item["content"])
        else:
            merged.append(item)

    # 首条必须是 user：丢掉开头的 assistant
    while merged and merged[0]["role"] != "user":
        dropped = merged.pop(0)
        log(f"[消息清洗] 丢弃开头的 {dropped['role']} 消息", "WARN")

    # 多 system 合并成一条，减少无谓 token
    sys_tokens = sum(message_tokens(s) for s in systems)
    sys_msgs = []
    if systems:
        texts = [s["content"] for s in systems if isinstance(s["content"], str)]
        if len(texts) == len(systems):
            sys_msgs = [{"role": "system", "content": "\n\n".join(texts)}]
        else:
            sys_msgs = systems

    # token 预算：先保证 system + 最近对话
    budget = max_tokens - RESERVE_OUTPUT_TOKENS
    kept = []
    used = sum(message_tokens(s) for s in sys_msgs)
    for item in reversed(merged):
        t = message_tokens(item)
        if used + t > budget and len(kept) >= MIN_KEEP_MESSAGES:
            break
        kept.append(item)
        used += t
    kept.reverse()

    # 截断后首条仍必须是 user（可能砍掉了配对的 user）
    while kept and kept[0]["role"] != "user":
        kept.pop(0)

    # 截断可能又产生连续同 role，再合一次
    final = []
    for item in kept:
        if final and final[-1]["role"] == item["role"]:
            final[-1]["content"] = _merge_contents(final[-1]["content"], item["content"])
        else:
            final.append(item)

    return sys_msgs + final, used + sys_tokens


# ------------------------------------------------------------------
# 搬自 agent.py L6567-6567：_MERGE_HEAD
# ------------------------------------------------------------------
_MERGE_HEAD = "用户连续发送了多条消息，请综合这些内容统一回复："


# ------------------------------------------------------------------
# 搬自 agent.py L6568-6569：_MERGE_TAIL
# ------------------------------------------------------------------
_MERGE_TAIL = ("（提示：上面附带的图片和表情包是语气的补充 —— 调侃、无语、赞同之类，"
               "请自然地接住这个情绪，不要当成新话题重新展开。）")


# ------------------------------------------------------------------
# 搬自 agent.py L6570-6570：_MEDIA_MARK_RE
# ------------------------------------------------------------------
_MEDIA_MARK_RE = re.compile(r"\[(?:图片|表情包[^\]]*)\]")


# ------------------------------------------------------------------
# 搬自 agent.py L6571-6571：_MERGE_EMPTY
# ------------------------------------------------------------------
_MERGE_EMPTY = "（对方连着发了几条，但都是表情或者图片，没有文字。）"


# ------------------------------------------------------------------
# 搬自 agent.py L6572-6573：_MEDIA_ONLY
# ------------------------------------------------------------------
_MEDIA_ONLY = ("（对方连着发了几条，没有文字，只有：%s）"
               "请就这些内容自然地回应一下，不要当成新话题展开。")


# ------------------------------------------------------------------
# 搬自 agent.py L6574-6574：_MEDIA_ONLY_HEAD
# ------------------------------------------------------------------
_MEDIA_ONLY_HEAD = "（对方连着发了几条，没有文字"


# ------------------------------------------------------------------
# 搬自 agent.py L6598-6604：_media_marks
# ------------------------------------------------------------------
def _media_marks(item):
    """一条消息里的媒体标记（[图片] / [表情包：xx]）。"""
    t = (item.get("text") or "")
    out = []
    for m in _MEDIA_MARK_RE.finditer(t):
        out.append(m.group(0))
    return out


# ------------------------------------------------------------------
# 搬自 agent.py L6607-6635：_merge_items
# ------------------------------------------------------------------
def _merge_items(items):
    """多条合并成一段；只有一条纯文本时不加壳，保持平时的说话方式。"""
    rows = []
    plain = []
    marks_all = []
    for it in items:
        t = (it.get("text") or "").strip()
        mk = _media_marks(it) or list(it.get("marks") or [])
        if mk:
            marks_all.extend(mk)
        if t:
            rows.append(t)
            if _MEDIA_MARK_RE.sub("", t).strip():
                plain.append(_MEDIA_MARK_RE.sub("", t).strip())
    if not rows:
        return _MERGE_EMPTY
    if not plain and marks_all:
        # v1.9.13 修正（QA P3-1）：只发了表情/图片时，别再套合并壳，给一句看得懂的说明
        return _MEDIA_ONLY % "、".join(marks_all[:6])
    # v1.9.13 二次修补（QA）：只有一条消息（哪怕带图带表情）也不套壳 —— 说「多条」是错的
    if len(items) <= 1 and rows:
        return rows[0]
    if len(rows) == 1 and not any((it.get("marks") for it in items)):
        return rows[0]
    lines = [_MERGE_HEAD]
    for i, t in enumerate(rows, 1):
        lines.append("[消息%d：%s]" % (i, t))
    lines.append(_MERGE_TAIL)
    return "\n".join(lines)


# ------------------------------------------------------------------
# 搬自 agent.py L6638-6657：_strip_merge_shell
# ------------------------------------------------------------------
def _strip_merge_shell(text):
    """把合并壳剥掉，只留下用户真正说的话（含多条，用 / 连接）。"""
    s = (text or "").strip()
    if s.startswith(_MEDIA_ONLY_HEAD):
        # 纯媒体模板里没有用户原话；判「是不是在问图」时必须是空
        return ""
    if not s.startswith(_MERGE_HEAD[:12]):
        return s
    lines = [l for l in s.splitlines() if l.strip()]
    keep = []
    for l in lines[1:]:
        l = l.strip()
        if l.startswith(_MERGE_TAIL[:8]):
            continue
        m = re.match(r"^\[消息(\d+)：([\s\S]*)\]$", l)
        if m:
            keep.append(m.group(2).strip())
        elif l:
            keep.append(l)
    return " / ".join([x for x in keep if x])


# ------------------------------------------------------------------
# 搬自 agent.py L6905-6905：_CARD_TITLE_KEYS
# ------------------------------------------------------------------
_CARD_TITLE_KEYS = ("title", "prompt_title", "promptTitle")


# ------------------------------------------------------------------
# 搬自 agent.py L6906-6906：_CARD_DESC_KEYS
# ------------------------------------------------------------------
_CARD_DESC_KEYS = ("prompt", "desc", "summary", "brief", "content", "description")


# ------------------------------------------------------------------
# 搬自 agent.py L6907-6907：_CARD_URL_KEYS
# ------------------------------------------------------------------
_CARD_URL_KEYS = ("jumpUrl", "jump_url", "jumpurl", "qqdocurl", "url", "link")


# ------------------------------------------------------------------
# 搬自 agent.py L6908-6908：_CARD_TAG_RE
# ------------------------------------------------------------------
_CARD_TAG_RE = re.compile(r"<[^>]+>")


# ------------------------------------------------------------------
# 搬自 agent.py L6911-6916：_html_unescape
# ------------------------------------------------------------------
def _html_unescape(s):
    try:
        import html as _html
        return _html.unescape(str(s or ""))
    except Exception:
        return str(s or "")


# ------------------------------------------------------------------
# 搬自 agent.py L6919-6924：_unquote_plus
# ------------------------------------------------------------------
def _unquote_plus(s):
    try:
        import urllib.parse as _up
        return _up.unquote(_html_unescape(s))
    except Exception:
        return _html_unescape(s)


# ------------------------------------------------------------------
# 搬自 agent.py L6927-6945：_walk_get
# ------------------------------------------------------------------
def _walk_get(obj, names, depth=0):
    """在任意嵌套的卡片结构里，按 names 的优先顺序找第一个非空字符串。"""
    if depth > 8:
        return ""
    if isinstance(obj, dict):
        for n in names:
            v = obj.get(n)
            if isinstance(v, str) and v.strip():
                return _html_unescape(v).strip()
        for v in obj.values():
            r = _walk_get(v, names, depth + 1)
            if r:
                return r
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            r = _walk_get(v, names, depth + 1)
            if r:
                return r
    return ""


# ------------------------------------------------------------------
# 搬自 agent.py L6948-6968：_loads_loose
# ------------------------------------------------------------------
def _loads_loose(s):
    """尽量把一段字符串解析成 dict（QQ 卡片里的 JSON 常被转义 / URL 编码过）。"""
    t = str(s or "").strip()
    if not t:
        return {}
    for cand in (t, _unquote_plus(t), _unquote_plus(t).replace("&#44;", ",")):
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            d = json.loads(t[i:j + 1])
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return {}


# ------------------------------------------------------------------
# 搬自 agent.py L6971-6972：_PLATFORM_NAMES
# ------------------------------------------------------------------
_PLATFORM_NAMES = ("哔哩哔哩", "bilibili", "哔哩", "b站", "腾讯视频", "抖音", "快手",
                   "小红书", "微博", "知乎", "微信", "qq", "qq音乐", "网易云音乐")


# ------------------------------------------------------------------
# 搬自 agent.py L6975-6983：_is_platform_name
# ------------------------------------------------------------------
def _is_platform_name(t):
    """这个「标题」其实是不是平台名（哔哩哔哩之类）？是的话它不能当内容标题。"""
    s = str(t or "").strip().lower()
    if not s or len(s) > 12:
        return False
    for p in _PLATFORM_NAMES:
        if s == p or s == p + "小程序" or s == p + "视频" or s == p + "网页":
            return True
    return False


# ------------------------------------------------------------------
# 搬自 agent.py L6986-7029：_card_from_json
# ------------------------------------------------------------------
def _card_from_json(raw):
    """JSON 卡片 → (标题, 描述, 链接)；拿不到就返回三个空串。"""
    obj = {}
    if isinstance(raw, dict):
        _d = raw.get("data")
        if isinstance(_d, dict):
            obj = _d
        elif isinstance(_d, str):
            obj = _loads_loose(_d)
        if not obj:
            obj = raw
    else:
        obj = _loads_loose(raw)
    if not obj:
        return "", "", ""
    _meta = obj.get("meta") if isinstance(obj, dict) else None
    _detail = {}
    if isinstance(_meta, dict):
        for _k in ("detail_1", "detail_2", "news", "video", "music", "doc"):
            if isinstance(_meta.get(_k), dict):
                _detail = _meta.get(_k)
                break
    title = _walk_get(obj, _CARD_TITLE_KEYS)
    desc = _walk_get(obj, _CARD_DESC_KEYS)
    url = _walk_get(obj, _CARD_URL_KEYS)
    # v1.9.16（QA P3-3）：B 站 / 小程序卡片的 title 往往只是平台名（「哔哩哔哩」），
    # 真正的视频标题在 detail_1.desc 里。以前直接把平台名当标题，模型只能以为
    # 对方分享了一整站 bilibili，完全聊不到那个视频上。
    if isinstance(_detail, dict):
        _dt = _html_unescape(str(_detail.get("title") or "")).strip()
        _dd = _html_unescape(str(_detail.get("desc") or "")).strip()
        if _dd:
            _dd = re.sub(r"^\s*(哔哩哔哩|bilibili|B站|哔哩)\s*[-—:|·、]?\s*",
                         "", _dd, flags=re.I).strip()
        if _dd and (not title or _is_platform_name(title)):
            if not desc or desc == title:
                desc = title if (title and not _is_platform_name(title)) else desc
            title = _dd
        elif _dt and (not title or _is_platform_name(title)):
            title = _dt
    # 兜底：任何卡片的标题只要是个平台名，就用描述顶上
    if _is_platform_name(title) and (desc or "").strip():
        title = desc.strip()
    return title, desc, url


# ------------------------------------------------------------------
# 搬自 agent.py L7032-7057：_card_from_xml
# ------------------------------------------------------------------
def _card_from_xml(raw):
    """XML 卡片 → (标题, 描述, 链接)。"""
    s = raw.get("data") if isinstance(raw, dict) else raw
    s = _unquote_plus(str(s or ""))
    if not s:
        return "", "", ""
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", s, re.S | re.I)
    if m:
        title = _CARD_TAG_RE.sub("", m.group(1)).strip()
    desc = ""
    m = re.search(r"<summary[^>]*>(.*?)</summary>", s, re.S | re.I)
    if m:
        desc = _CARD_TAG_RE.sub("", m.group(1)).strip()
    url = ""
    for _pat in (r"<url[^>]*>(.*?)</url>", r'\burl\s*=\s*"([^"]+)"', r"\burl\s*=\s*'([^']+)'"):
        m = re.search(_pat, s, re.S | re.I)
        if m and m.group(1).strip():
            url = m.group(1).strip()
            break
    if not title:
        # 兜底：<item><title>.. 之类的变体，取第一段 CDATA/文本
        m = re.search(r"<item[^>]*>(.*?)</item>", s, re.S | re.I)
        if m:
            title = _CARD_TAG_RE.sub(" ", m.group(1)).strip()[:60]
    return title, desc, url


