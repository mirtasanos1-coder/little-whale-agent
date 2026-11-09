# -*- coding: utf-8 -*-
"""事件与记忆提取（从 agent.py 物理搬出来，逻辑没改）。

这里管的是「把一段对话变成结构化的东西」：
  · 事件/待办：extract_events → _regex_event_fallback → 重试队列（_EV_RETRY*）
  · 会话记忆：extract_and_store → _parse_memory_line / _parse_memory_block
  · 永久记忆：_perm_extract_job → diary.extract_perm_from_dialog（+ 情绪兜底 _emotional_facts）
  · 后台槽位与节流：bg_slot_ok / bg_slot_free / _EXTRACT_LAST / _FG_TS
    （这几样是**线程间共享的锁和计数器**，全项目只有这一份，agent.py 会 import 回去，
      自测里 agent._EXTRACT_LAST.clear() 清的也是这一本账）

调用模型一律走 core.llm_client._extract_text（它自己会顺延端点、记用量）。
"""
import json
import queue
import re
import threading
import time

import diary as diary_mod

from core import globals as _g
from core.llm_client import _extract_text
from core.utils import (  # noqa: F401
    _MEDIA_MARK_RE, _MEDIA_ONLY_HEAD, _MERGE_HEAD, _MERGE_TAIL,
    log, strip_think,
)

_SYNC_NAMES = ('mem', '_live_cfg', 'MEMORY_MODE', 'MEMORY_REPLACE_CONFLICT', '_EMO_FACT_RULES', '_REMIND_RE', '_WEEKDAY_NUM', '_cn_int', '_extract_text')


def _sync():
    """把 agent 里那些活名字的最新值灌进本模块。"""
    _g.sync(_SYNC_NAMES, globals())


# ------------------------------------------------------------------
# 搬自 agent.py L1091-1091：_FG_TS
# ------------------------------------------------------------------
_FG_TS = {"t": 0.0}                      # 最近一次前台 ask_model 的时间


# ------------------------------------------------------------------
# 搬自 agent.py L1092-1092：_BG_SLOT
# ------------------------------------------------------------------
_BG_SLOT = threading.Lock()


# ------------------------------------------------------------------
# 搬自 agent.py L1097-1097：_EV_SLOT
# ------------------------------------------------------------------
_EV_SLOT = threading.Lock()


# ------------------------------------------------------------------
# 搬自 agent.py L1103-1103：_MEM_SLOT
# ------------------------------------------------------------------
_MEM_SLOT = threading.Lock()


# ------------------------------------------------------------------
# 搬自 agent.py L1104-1104：_BG_SLOTS
# ------------------------------------------------------------------
_BG_SLOTS = {"bg": _BG_SLOT, "event": _EV_SLOT, "memory": _MEM_SLOT}


# ------------------------------------------------------------------
# 搬自 agent.py L1105-1105：_SLOT_NAMES
# ------------------------------------------------------------------
_SLOT_NAMES = ("bg", "event", "memory")


# ------------------------------------------------------------------
# 搬自 agent.py L1106-1106：_BG_TLS
# ------------------------------------------------------------------
_BG_TLS = threading.local()              # 谁真正拿到了槽位，才允许释放


# ------------------------------------------------------------------
# 搬自 agent.py L1107-1107：_EXTRACT_LAST
# ------------------------------------------------------------------
_EXTRACT_LAST = {}


# ------------------------------------------------------------------
# 搬自 agent.py L1108-1108：_EXTRACT_MIN_INTERVAL
# ------------------------------------------------------------------
_EXTRACT_MIN_INTERVAL = 45               # 秒


# ------------------------------------------------------------------
# 搬自 agent.py L1109-1109：_BG_QUIET_SEC
# ------------------------------------------------------------------
_BG_QUIET_SEC = 8.0                      # 前台上一次说话后多少秒内不让后台抢模型


# ------------------------------------------------------------------
# 搬自 agent.py L1112-1123：_extract_key
# ------------------------------------------------------------------
def _extract_key(key, ns=""):
    """v1.9.24：节流 key 加功能前缀。

    以前三条链（记忆提取 / 永久记忆提炼 / 事件提取）都用裸 scope 当 key，
    而 _after_reply_job 里它们是**同时**起线程的 —— 谁先抢到谁写 _EXTRACT_LAST，
    另外两个被 45 秒窗口挡掉，等于「永久记忆全天提、会话记忆永远提不到」。
    实测（用户真机库）：perm_memory 29 行、memories 0 行。
    现在各自记账，互不干扰。
    """
    k = str(key or "")
    n = str(ns or "")
    return f"{n}|{k}" if n else k


# ------------------------------------------------------------------
# 搬自 agent.py L1126-1157：bg_slot_ok
# ------------------------------------------------------------------
def bg_slot_ok(key="", min_interval=None, slot="bg", ns=""):
    """后台任务此刻能不能占用模型？

    slot：用哪个槽位。"bg" = 永久记忆提炼（默认，行为跟老版本一致）；
          "event" = 事件提取（v1.1.7 独立槽位，不会被记忆提取挤掉）；
          "memory" = 会话记忆提取（v1.9.24 独立槽位，不再被永久记忆提炼挤掉）。
    ns：  v1.9.24，节流命名空间。传了就跟别的功能分开计 45 秒窗口；
          不传保持老行为（裸 key），兼容既有调用点。
    """
    mi = _EXTRACT_MIN_INTERVAL if min_interval is None else float(min_interval)
    now = time.time()
    if now - float(_FG_TS.get("t") or 0) < _BG_QUIET_SEC:
        return False
    _k = _extract_key(key, ns) if key else ""
    if _k:
        last = float(_EXTRACT_LAST.get(_k) or 0)
        if now - last < mi:
            return False
    slot = str(slot) if str(slot) in _BG_SLOTS else "bg"
    lock = _BG_SLOTS[slot]
    if not lock.acquire(blocking=False):
        return False
    _BG_TLS.held = True                       # 老字段保留，兼容别的代码
    held = getattr(_BG_TLS, "held_slots", None)
    if not isinstance(held, set):
        held = set()
        _BG_TLS.held_slots = held
    held.add(slot)
    _BG_TLS.held_slot = slot
    if _k:
        _EXTRACT_LAST[_k] = now
    return True


# ------------------------------------------------------------------
# 搬自 agent.py L1160-1178：bg_slot_free
# ------------------------------------------------------------------
def bg_slot_free(slot=None):
    """只有真正拿到槽位的线程才释放（否则会把别人的锁放掉）"""
    held = getattr(_BG_TLS, "held_slots", None)
    if not isinstance(held, set):
        held = set()
    if slot is None:
        # 老调用点（没传 slot）：放掉这个线程手上所有槽位
        names = list(held) or []
    else:
        names = [str(slot) if str(slot) in _BG_SLOTS else "bg"]
    for name in names:
        if name in held:
            held.discard(name)
            try:
                _BG_SLOTS.get(name, _BG_SLOT).release()
            except Exception:
                pass
    if not held:
        _BG_TLS.held = False


# ------------------------------------------------------------------
# 搬自 agent.py L1359-1359：EVENT_EXTRACT_ENABLED
# ------------------------------------------------------------------
EVENT_EXTRACT_ENABLED = True


# ------------------------------------------------------------------
# 搬自 agent.py L1360-1360：EVENT_EXTRACT_MIN_LEN
# ------------------------------------------------------------------
EVENT_EXTRACT_MIN_LEN = 4          # 太短的消息不提取


# ------------------------------------------------------------------
# 搬自 agent.py L1363-1363：EXTRACT_BUDGET
# ------------------------------------------------------------------
EXTRACT_BUDGET = 512


# ------------------------------------------------------------------
# 搬自 agent.py L1364-1364：SUMMARY_BUDGET
# ------------------------------------------------------------------
SUMMARY_BUDGET = 400


# ------------------------------------------------------------------
# 搬自 agent.py L2788-2811：EXTRACT_PROMPT
# ------------------------------------------------------------------
EXTRACT_PROMPT = """以下是 QQ 聊天里的一段对话。
{dialog}

请判断里面有没有值得长期记住的关于「某个人」的新信息。

【什么算】只要是这个人身上稳定的、以后聊天还用得上的信息都算：
- 基本事实：考试、生日、喜好、职业、学校、专业、生病、搬家、养宠物、家人
- 情绪状态：难过、失恋、崩溃、焦虑、失眠、累、开心、生气、害怕
- 关系变化：告白、在一起、分手、吵架、和好、表白被拒、暧昧、想念
- 承诺约定：说好了、答应你、周末陪你、下次一起去、别告诉别人
- 强偏好和雷区：最讨厌、绝不能提、最怕、一听到就烦、最喜欢
- 说话习惯和口头禅：怎么称呼对方、爱用的昵称、常开的玩笑

★ 只要对话里出现上面任何一类，**必须**至少记一条。不许因为"这不算客观事实"
  或者"只是情绪"就跳过 —— 情绪、关系、约定同样是关于这个人的重要信息。
  真的什么都没有（纯属"嗯""哈哈""好的"这种）才输出：无。

要求：
- 每条一行，格式：`- 记住: （人名）事实`
- 事实要具体、带上原话里的关键信息，别写"他很喜欢聊天"这种没信息量的
- 最多 3 条，挑最重要的
- 没有值得记的就输出：无

只输出行，不要解释："""


# ------------------------------------------------------------------
# 搬自 agent.py L2815-2848：EVENT_EXTRACT_PROMPT
# ------------------------------------------------------------------
EVENT_EXTRACT_PROMPT = """下面是一段 QQ 聊天记录。请找出其中【带明确时间点的个人计划/事件】，
例如"明天9点考试""下周三去北京出差""今晚8点面试""周六下午体检"。

对话：
{dialog}

当前时间是：{now}

要求：
1. 只输出 JSON 数组，不要任何解释、不要 markdown 代码块。
2. 每项格式：
   {{"desc": "简短事件描述（不超过30字）", "time": "YYYY-MM-DD HH:MM",
     "care": true, "care_min": 120, "care_topic": "想问的一句话（不超过20字）"}}
   desc 用第三人称客观描述，**去掉"我""你"等人称代词**，例如
   原文"我明天9点考试" → desc 写"考试"；"我下周去北京出差" → desc 写"去北京出差"。
3. time 必须是未来时间；"明天"相对今天推算，"下周三"推算到下一个周三。
   "一会儿""等下""马上"这种很近的事，按当前时间加 20~60 分钟推算。
4. care / care_min / care_topic 三个字段是"要不要主动关心"：
   - care: 这件事值不值得过一会儿主动问一句（考试、面试、体检、下课、开会、
     见客户、看病、出远门 → true；吃饭、睡觉这种日常 → false）
   - care_min: 事件之后多少分钟关心比较自然（考试约 120，面试约 60，
     下课约 10，体检约 180）
   - care_topic: 关心时想说的那句话的方向，例如"考得怎么样""面试顺利吗"
   不确定就整组省略，不要瞎填。
5. 没有明确时间点的闲聊、情绪、玩笑一律不要提取。
   "以后""有空""最近""每天""过几天"这类模糊说法**不算时间点**，不要提取。
6. 没有可提取的就输出：[]
7. 同一件事只输出一次，不要换个说法重复。最多输出 3 项。
8. desc 要让人一眼看懂是什么事，不要写成"事情""那个""安排"这种空话；
   也不要带修饰词（"重要的""令人紧张的"）。
9. care=true 的前提是：这件事**有结果可问**（考完了吗、面试过没过、到家没）。
   如果只是"要去做某事"且没什么好问的（比如"我明天去上班"），care 填 false。

输出："""


# ------------------------------------------------------------------
# 搬自 agent.py L2851-2871：_parse_event_time
# ------------------------------------------------------------------
def _parse_event_time(text, now=None):
    """把模型给的 'YYYY-MM-DD HH:MM' 解析成时间戳；失败返回 0"""
    if not text:
        return 0
    text = str(text).strip()
    now = time.time() if now is None else now
    fmts = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M",
            "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S.000"]
    for fmt in fmts:
        try:
            return time.mktime(time.strptime(text, fmt))
        except Exception:
            continue
    # 只有日期：当天 09:00
    for fmt in ["%Y-%m-%d", "%Y/%m/%d"]:
        try:
            t = time.mktime(time.strptime(text, fmt))
            return t + 9 * 3600
        except Exception:
            continue
    return 0


# ------------------------------------------------------------------
# 搬自 agent.py L2895-3019：_regex_event_fallback
# ------------------------------------------------------------------
def _regex_event_fallback(raw_text, now=None):
    """v1.1.7 修（P0-1）：模型没提取出事件时，用规则兜底落一条 user_events。

    为什么需要：QA 报告里 user_events 恒为 0。拆完独立槽位之后还得防第二种情况
    ——模型返回空（推理模型烧 token / 网关抽风）。这时候不能装作"记下了"，
    而是用正则把「日期 + 几点 + 干什么」抠出来，能认出来就落一条真的；
    认不出来就返回 (None, 0)，绝不写假事件。

    返回 (desc, target_ts)；认不出返回 (None, 0)。
    """
    _sync()
    now = time.time() if now is None else float(now)
    t = str(raw_text or "").strip()
    if not t:
        return (None, 0)
    spans = []
    lt = time.localtime(now)
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    day_ts = None
    default_hour = 9

    # ① 绝对日期：5月20日 / 5月20号
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t)
    if m:
        mo, dy = int(m.group(1)), int(m.group(2))
        try:
            ts = time.mktime((lt.tm_year, mo, dy, 0, 0, 0, 0, 0, -1))
            if ts < base - 86400:      # 说的大概是明年
                ts = time.mktime((lt.tm_year + 1, mo, dy, 0, 0, 0, 0, 0, -1))
            day_ts = ts
            spans.append(m.group(0))
        except Exception:
            return (None, 0)

    # ② 周几：周五 / 星期五 / 礼拜天 / 下周三
    if day_ts is None:
        m = re.search(r"(下下|下)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", t)
        if m:
            wd = _WEEKDAY_NUM.get(m.group(2))
            if wd is not None:
                today_wd = (int(time.strftime("%w", time.localtime(now))) + 6) % 7  # 周一=0
                delta = (wd - today_wd) % 7
                if delta == 0:
                    delta = 7
                if m.group(1):
                    delta += 7 * (2 if m.group(1) == "下下" else 1)
                day_ts = base + delta * 86400
                spans.append(m.group(0))

    # ③ 相对日：大后天 / 后天 / 明天 / 明早 / 今晚 / 今天
    rel_w = ""
    if day_ts is None:
        m = re.search(r"(大后天|后天|明天|明日|明早|明晚|今晚|今天|今日)", t)
        if m:
            w = m.group(1)
            rel_w = w
            off = {"大后天": 3, "后天": 2, "明天": 1, "明日": 1, "明早": 1,
                   "明晚": 1, "今晚": 0, "今天": 0, "今日": 0}[w]
            day_ts = base + off * 86400
            if w in ("明早", "明晚", "今晚"):
                default_hour = {"明早": 7, "明晚": 20, "今晚": 20}[w]
            spans.append(m.group(0))

    if day_ts is None:
        return (None, 0)          # 没有日期线索 → 不瞎猜

    # ④ 具体钟点：9:30 / 9点 / 九点半
    hour, minute = default_hour, 0
    hour_set = False
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            hour, minute, hour_set = hh, mm, True
            spans.append(m.group(0))
    if not hour_set:
        m = re.search(r"(\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*点\s*(半|(\d{1,2})\s*分?)?", t)
        if m:
            hh = _cn_int(m.group(1))
            if 0 <= hh <= 24:
                hour = 0 if hh == 24 else hh
                if m.group(2) == "半":
                    minute = 30
                elif m.group(3):
                    minute = max(0, min(59, _cn_int(m.group(3))))
                hour_set = True
                spans.append(m.group(0))

    # ⑤ 时段词：下午 3 点 → 15 点
    m = re.search(r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)", t)
    if m:
        w = m.group(1)
        if w in ("下午", "傍晚", "晚上", "夜里") and 0 < hour < 12:
            hour += 12
        elif w in ("晚上", "夜里") and hour == 12:
            hour = 24                 # 晚上12点 = 次日零点
        elif w == "中午" and 0 < hour < 11:
            hour = 12
        elif w == "凌晨" and hour == 12:
            hour = 0
        spans.append(m.group(0))
    elif rel_w in ("今晚", "明晚") and hour == 12:
        hour = 24                     # 「今晚12点前交作业」指的是零点，不是中午

    ts = day_ts + hour * 3600 + minute * 60
    if ts <= now + 60:
        return (None, 0)          # 算出来是过去 → 认错时间了，宁可不记

    # ⑥ 描述 = 原句去掉时间表达
    desc = t
    for sp in sorted(set(spans), key=lambda x: -len(x)):
        desc = desc.replace(sp, " ")
    desc = re.sub(r"[\r\n\t]+", " ", desc)
    desc = re.sub(r"[，,。.！!？?、；;：:~～…]+", " ", desc)
    desc = re.sub(r"\s+", " ", desc).strip()
    # 去掉时间词之后常剩下的虚词/动词开头（「要到」「到」「前」这类），连剥两遍
    for _ in range(2):
        desc = re.sub(r"^(?:我们|咱们|我|你|他|她|要|得|会|还|就|再|先|"
                      r"大概|可能|应该|到|在|的|是|前|后|和|跟|把|被|给)+", "", desc)
        desc = desc.strip(" ，,。.！!？?、；;：:~～…-")
    desc = desc[:40].strip(" ，,。.！!？?、；;：:~～…-")
    if len(desc) < 2:
        return (None, 0)
    if re.fullmatch(r"(?:事情|事|那个|那个事|安排|计划|东西|啥|什么|时间|点|时候)+", desc):
        return (None, 0)
    return (desc, ts)


# ------------------------------------------------------------------
# 搬自 agent.py L3022-3035：_extract_json_array
# ------------------------------------------------------------------
def _extract_json_array(text):
    """从模型输出里抠出 JSON 数组，容忍前后废话和代码块"""
    if not text:
        return []
    text = strip_think(text)
    text = re.sub(r"```(?:json)?", "", text).replace("```", "")
    m = re.search(r"\[.*\]", text, flags=re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except Exception:
        return []


# ------------------------------------------------------------------
# 搬自 agent.py L3042-3042：_EV_RETRY
# ------------------------------------------------------------------
_EV_RETRY = queue.Queue(maxsize=200)


# ------------------------------------------------------------------
# 搬自 agent.py L3043-3043：_EV_RETRY_TH
# ------------------------------------------------------------------
_EV_RETRY_TH = {"th": None}


# ------------------------------------------------------------------
# 搬自 agent.py L3044-3044：_EV_RETRY_SEEN
# ------------------------------------------------------------------
_EV_RETRY_SEEN = {}


# ------------------------------------------------------------------
# 搬自 agent.py L3047-3064：_enqueue_event_retry
# ------------------------------------------------------------------
def _enqueue_event_retry(scope, sender_name, user_id, raw_text, delay=10.0):
    """把没抢到槽位的提取任务排进延迟队列（默认10秒后再跑）。"""
    _sync()
    key = (str(scope or ""), str(raw_text or "")[:80])
    now = time.time()
    if now - float(_EV_RETRY_SEEN.get(key) or 0) < 120:
        return False #同一句话2分钟内只排一次，别堆爆
    _EV_RETRY_SEEN[key] = now
    if len(_EV_RETRY_SEEN) > 500:
        _EV_RETRY_SEEN.clear()
    try:
        _EV_RETRY.put_nowait({"due": now + float(delay), "scope": scope,
            "sender_name": sender_name, "user_id": user_id,
            "raw_text": raw_text})
        _ensure_ev_retry_thread()
        return True
    except Exception as e: # noqa: BLE001
        log(f" [事件]排不进重试队列（{e}），这条跳过", "DEBUG")
        return False


# ------------------------------------------------------------------
# 搬自 agent.py L3067-3073：_ensure_ev_retry_thread
# ------------------------------------------------------------------
def _ensure_ev_retry_thread():
    _sync()
    th = _EV_RETRY_TH.get("th")
    if th and th.is_alive():
        return
    _EV_RETRY_TH["th"] = threading.Thread(
        target=_ev_retry_worker, name="ev-retry", daemon=True)
    _EV_RETRY_TH["th"].start()


# ------------------------------------------------------------------
# 搬自 agent.py L3076-3098：_ev_retry_worker
# ------------------------------------------------------------------
def _ev_retry_worker():
    """扫描延迟队列：到点就重跑一次 extract_events（_retry=1防无限循环）。"""
    _sync()
    while True:
        try:
            item = _EV_RETRY.get(timeout=1.5)
        except Exception:
            continue
        try:
            due = float(item.get("due") or 0)
            wait = due - time.time()
            if wait > 0:
                time.sleep(min(wait,5.0))
                if due > time.time() + 0.5:
                    try:
                        _EV_RETRY.put_nowait(item)
                    except Exception:
                        pass
                    continue
            log(" [事件]延迟重试：现在再提取一次", "INFO")
            extract_events(item.get("scope"), item.get("sender_name"),
                item.get("user_id"), item.get("raw_text"), _retry=1)
        except Exception as e: # noqa: BLE001
            log(f" [事件]重试出错: {e}", "DEBUG")


# ------------------------------------------------------------------
# 搬自 agent.py L3101-3285：extract_events
# ------------------------------------------------------------------
def extract_events(scope, sender_name, user_id, raw_text, _retry=0):
    """后台线程：从用户这句话里提取带时间点的事件，写进 user_events"""
    _sync()
    if not EVENT_EXTRACT_ENABLED:
        return
    # v1.1.2：总开关 + 「只手动加」直接不提取（设置页可改，改完立刻生效）
    if not bool(_live_cfg("event_extract_enabled", True)):
        return
    if str(_live_cfg("event_source", "model") or "model") == "manual":
        return
    if not raw_text or len(raw_text) < EVENT_EXTRACT_MIN_LEN:
        return
    # 快速预筛：没有任何时间线索就不打扰模型
    if not re.search(r"(明天|后天|大后天|今天|今晚|明早|下周|下个?周|周[一二三四五六日天]|"
                     r"星期[一二三四五六日天]|早上|上午|中午|下午|晚上|点|号|日|月)", raw_text):
        return
    # v1.1.7 修（P0-1）：独立槽位 + 抢不到时最多等 5 秒再试，不再静默 return。
    # 老行为：记忆提取占着 _BG_SLOT 时这里直接 return，连一行日志都没有，
    # 用户看到的就是"user_events 一片空白"。
    # v1.6.3修（QA P1-1：事件提取100%失效）
    #根因：前台刚说完话有 _BG_QUIET_SEC（8秒）静默期，而这里原来最多只等5秒，
    #必然扑空 → user_events恒为0。现在分三层兜底：
    #①先试一次；②按静默期剩余时间等到期（≥12秒）；③还拿不到就排进延迟队列，
    #10秒后重试一次，绝不静默丢。
    _now0 = time.time()
    _last_ev = float(_EXTRACT_LAST.get(scope) or 0)
    _quiet_left = max(0.0, _BG_QUIET_SEC - (_now0 - float(_FG_TS.get("t") or 0)))
    #窗口 =静默期剩余 +余量，且不少于12秒（QA要求）
    _may_wait = (max(12.0, _quiet_left + 4.0) if (_retry or (_now0 - _last_ev) >= 30) else 0.0)
    _got = bg_slot_ok(scope, 30, slot="event", ns="event")
    _t0 = time.time()
    while not _got and _may_wait > 0 and (time.time() - _t0) < _may_wait:
        time.sleep(0.5)
        _got = bg_slot_ok(scope, 30, slot="event", ns="event")
    if not _got:
        if _may_wait <= 0:
            _gap = int(max(0,30 - (_now0 - _last_ev)))
            log(f" [事件]同一会话 {_gap}秒内刚提取过，这条跳过", "INFO")
        else:
            log(f" [事件]等了 {int(time.time() - _t0)}秒槽位仍没空出来"
                "（前台刚聊完的静默期 /另一个提取任务在忙），排队10秒后重试", "INFO")
            _enqueue_event_retry(scope, sender_name, user_id, raw_text)
        return

    def _fallback(reason_text=""):
        """模型没给出结果时用规则兜底，返回是否落成功（v1.1.7 / P0-1）。

        宁可返回 False，也不写一条时间错的事件 —— 写错的查岗比不查岗更尬。
        """
        try:
            _d, _ts = _regex_event_fallback(raw_text)
            if not _d or not _ts:
                log(f"  [事件] 模型没提取出来（{reason_text or '空'}），规则兜底也没认出时间点，"
                    f"这条不记（不会假装记下了）", "INFO")
                return False
            _eid = mem.add_event(user_id=user_id, event_desc=_d,
                             target_time=_ts, session_key=scope,
                             ignore_silence=(1 if _REMIND_RE.search(raw_text or "") else 0))
            if _eid:
                _when = time.strftime("%m-%d %H:%M", time.localtime(_ts))
                log(f"  [事件] #{_eid} {_when} {_d}（模型没提取出来，用规则兜底记下了）", "INFO")
                return True
            log(f"  [事件] 规则兜底认出的和已有事件重复，不重复记：{_d}", "INFO")
        except Exception as e:
            log(f"  [事件] 规则兜底失败: {e}", "DEBUG")
        return False

    try:
        now_str = time.strftime("%Y-%m-%d %H:%M %A")
        # v8：把最近几条对话一起喂进去。只看当前这一句经常漏掉"他刚说周六体检，
        # 然后回了个'对'"这种情况；带上前文识别率明显更高。
        ctx_lines = []
        try:
            hist = mem.get_recent_messages(scope, turns=4) if scope else []
            for m in hist[-5:]:
                role = "我" if m.get("role") == "assistant" else sender_name
                c = str(m.get("content") or "").strip()
                if c:
                    ctx_lines.append(f"{role}：{c[:120]}")
        except Exception:
            ctx_lines = []
        if not ctx_lines or not ctx_lines[-1].endswith(raw_text[:120]):
            ctx_lines.append(f"{sender_name}：{raw_text}")
        dialog = "\n".join(ctx_lines)
        # v1.1.2：用户可以在界面上写「额外提示词」，告诉模型什么该记、什么别记
        _ev_prompt = EVENT_EXTRACT_PROMPT.format(dialog=dialog, now=now_str)
        try:
            _extra = str(_live_cfg("event_prompt", "") or "").strip()
        except Exception:
            _extra = ""
        if _extra:
            _ev_prompt += "\n\n【用户额外要求（优先于上面的默认规则）】\n" + _extra[:600]
        # ★ 关键：这类"提取结构化信息"的任务不需要思考，必须显式关掉。
        #   之前用 effort="omit"（让服务端用默认值），推理模型会先想几百字，
        #   把 max_tokens 吃光 → 正文为空 → 事件永远提取不到。
        # v1.9.23：事件提取也走「和聊天同一条优先级链」（云端在前就先云端），
        # 分工里单独配了 event 才用它。以前这里直接打全局 base，
        # 用户切云端后必然超时 → user_events 一直是空的。
        text, reason = _extract_text(_ev_prompt, EXTRACT_BUDGET, temperature=0.1,
                                     tag="/事件提取", feature="event", timeout=120)
        if not text:
            log(f"  [事件] 模型没给出结果（{reason}），改走规则兜底", "INFO")
            _fallback(reason)
            return
        items = _extract_json_array(text)
        if not items:
            log("  [事件] 模型返回的不是能解析的 JSON 数组，改走规则兜底", "INFO")
            _fallback("不是 JSON")
            return
        _stored = 0
        for it in items[:3]:
            if not isinstance(it, dict):
                continue
            desc = str(it.get("desc") or "").strip()
            # 去掉人称代词，避免查岗话术变成"对了，我明天9点考试——后来怎么样啦？"
            desc = re.sub(r"^[我你他她]\s*", "", desc).strip()
            desc = re.sub(r"(要|会|得)$", "", desc).strip()
            # v8：过滤空话
            if not desc or len(desc) > 40:
                continue
            if re.fullmatch(r"(事情|事|那个|那个事|安排|计划|东西|啥|什么)+", desc):
                log(f"  [事件] 丢弃空泛描述：{desc!r}", "DEBUG")
                continue
            ts = _parse_event_time(it.get("time"))
            now = time.time()
            # v8：时间必须落在 [1 分钟后, 60 天后]，否则模型算错了会立刻触发查岗
            if ts <= now + 60:
                log(f"  [事件] 丢弃时间不合法（{desc}，{it.get('time')}）", "DEBUG")
                continue
            if ts > now + 60 * 86400:
                log(f"  [事件] 丢弃太远的时间（{desc}，{it.get('time')}）", "DEBUG")
                continue
            # v1.9.9：这句是「叫我/提醒我」这种明确的约定吗？是就别让静默挡住
            _ign = 1 if _REMIND_RE.search(raw_text or "") else 0
            _pri = 1 if (it.get("remind") or it.get("priority") or _ign) else 0
            if _ign:
                log("  [事件] 这句是明确要求提醒，标记 ignore_silence=1：" + desc, "INFO")
            eid = mem.add_event(user_id=user_id, event_desc=desc,
                    target_time=ts, session_key=scope,
                    ignore_silence=_ign, priority=_pri)
            if eid:
                _stored += 1
                when = time.strftime("%m-%d %H:%M", time.localtime(ts))
                log(f"  [事件] #{eid} {when} {desc}", "INFO")
            # ---- v6：顺手排一条"主动关心"计划（模型判断值不值得问一句）----
            if (bool(_live_cfg("care_plan_enabled", True))
                    and str(_live_cfg("care_plan_source", "model") or "") in ("model", "both")):
                try:
                    if bool(it.get("care")):
                        mins = it.get("care_min")
                        try:
                            mins = int(mins)
                        except Exception:
                            mins = 60
                        mins = max(1, min(12 * 60, mins))
                        topic = str(it.get("care_topic") or "").strip()[:40]
                        if not topic:
                            # 模型没给问法就按事件推一句，别让它空着
                            topic = f"{desc}怎么样了"
                            log(f"  [关心计划] 模型没给问法，按事件推：{topic}", "DEBUG")
                        plan_at = ts + mins * 60
                        if plan_at <= time.time() + 30:
                            log(f"  [关心计划] 时间太近（{desc}），跳过", "DEBUG")
                            continue
                        pid, is_new = mem.add_plan(
                            user_id=user_id, session_key=scope,
                            plan_at=plan_at,
                            reason=desc, topic=topic, source="model",
                            extra=json.dumps({"event_id": eid}, ensure_ascii=False),
                            ignore_silence=_ign, priority=_pri,
                        )
                        if is_new:
                            at = time.strftime("%m-%d %H:%M",
                                               time.localtime(ts + mins * 60))
                            log(f"  [关心计划] #{pid} {at} {desc}"
                                + (f" / {topic}" if topic else ""), "INFO")
                except Exception as e:
                    log(f"  [关心计划] 写入失败（忽略）: {e}", "DEBUG")
        if not _stored:
            # v1.1.7：模型给了 JSON 但全被规则筛掉，也试着兜一条，别静默
            _fallback("全部被规则筛掉")
    except Exception as e:
        log(f"  [事件] 提取失败: {e}", "ERROR")
        _fallback(f"异常 {type(e).__name__}")
    finally:
        bg_slot_free(slot="event")


# ------------------------------------------------------------------
# 搬自 agent.py L3288-3339：_parse_memory_line
# ------------------------------------------------------------------
def _parse_memory_line(line, default_person):
    """解析一行记忆提取结果，返回 (person, fact) 或 None。

    模型不一定严格按格式输出，实测过这些形态：
        - 记住: （阿哲）养了一只橘猫          ← 标准
        - 记住: 阿哲养了一只橘猫              ← 忘了括号（旧正则会把整句当人名）
        - 阿哲：养了一只橘猫                  ← 用了冒号
        - - 记住: 他喜欢喝冰美式
        - 1. 记住: 他喜欢喝冰美式             ← v1.9.24：带序号（实测模型爱这么写）
        - [work] 他下午三点在上课             ← v1.9.24：带分类方括号
    """
    if not line:
        return None
    s = line.strip()
    # 去掉列表符号
    s = re.sub(r"^[-*•·]\s*", "", s).strip()
    # v1.9.24：去掉序号（"1." "1、" "1)" "（1）" "①"）—— 模型经常自动编号，
    # 老逻辑不认，`1. 记住: …` 会连序号带正文一起被当成事实，人名也丢了。
    # 注意分开写：不能写成 `^\d{1,2}[.:：)]`，那样会把正文开头的「12:30 上课」切成「30 上课」。
    s = re.sub(r"^[（(]\s*\d{1,2}\s*[）)]\s*[.、．:：)]?\s*", "", s).strip()
    s = re.sub(r"^\d{1,2}\s*[.、．]\s*", "", s).strip()
    s = re.sub(r"^\d{1,2}\s*[)）]\s*", "", s).strip()
    s = re.sub(r"^\d{1,2}\s*[:：](?!\d)\s*", "", s).strip()
    s = re.sub(r"^[①②③④⑤⑥⑦⑧⑨⑩]\s*", "", s).strip()
    # v1.9.24：去掉分类标签，如 "[work]" "[preference]" "【情绪】"
    s = re.sub(r"^[\[【][^\]】]{1,12}[\]】]\s*", "", s).strip()
    # 去掉「记住:」前缀
    s = re.sub(r"^记住\s*[:：]\s*", "", s).strip()
    if not s or s in ("无", "無", "None", "none", "没有", "暂无"):
        return None

    # ① 标准格式：（人名）事实
    m = re.match(r"^[（(]([^）)]{1,20})[）)]\s*(.+)$", s)
    if m:
        person, fact = m.group(1).strip(), m.group(2).strip()
        return (person or default_person, fact) if fact else None

    # ② 冒号格式：人名：事实（人名要短、且不能含数字，避免把整句切开）
    # v1.9.24：以前是 `[^：:]{1,12}`，「他每天12:30上课」会被切成人名「他每天12」+ 事实「30上课」。
    m = re.match(r"^([^：:\d]{1,12})[：:]\s*(.+)$", s)
    if m:
        person, fact = m.group(1).strip(), m.group(2).strip()
        # "他/她/我" 这种代词统一归到默认人名
        if person in ("他", "她", "我", "对方", "用户"):
            person = default_person
        if fact and len(fact) >= 2:
            return (person, fact)

    # ③ 没有括号也没有冒号 → 整句都是事实，人默认用 sender
    if len(s) >= 4:
        return (default_person, s)
    return None


# ------------------------------------------------------------------
# 搬自 agent.py L3342-3406：_parse_memory_block
# ------------------------------------------------------------------
def _parse_memory_block(text, default_person):
    """把模型返回的**整段**结果解析成 [(person, fact), ...]。

    v1.9.24：以前 extract_and_store 只会 `for line in text.splitlines()` +
    `_parse_memory_line`。实测模型不老实按行输出时会**一条都提不出来** ——
    真机上「memories 表 0 行、perm_memory 29 行」就是这么来的：
    perm 那条链用的是 `_extract_json_array`（剥代码块 + 抠 [.*]），宽容得多；
    会话记忆这条链只会逐行，模型一旦给 JSON / 编号列表就全丢。

    现在三级兜底，任何一级命中就用：
      ① JSON 数组：`[{"content": "…", "person": "…"}, …]` 或 `["…", "…"]`
         —— 直接复用 `_extract_json_array` 那套剥壳逻辑，跟永久记忆链一致
      ② 逐行文本（老行为，可能带序号 / 分类标签，交给 _parse_memory_line 清洗）
      ③ 整段当一条（万一模型没分行，也别浪费）
    """
    raw = str(text or "").strip()
    if not raw:
        return []

    # ① 先按 JSON 数组试（跟永久记忆链同款的宽容解析）
    try:
        items = _extract_json_array(raw)
    except Exception:
        items = []
    out = []
    if items:
        for it in items:
            if isinstance(it, dict):
                fact = str(it.get("content") or it.get("fact")
                           or it.get("text") or it.get("memory") or "").strip()
                who = str(it.get("person") or it.get("name")
                          or it.get("who") or "").strip() or default_person
            else:
                fact, who = str(it or "").strip(), default_person
            if not fact:
                continue
            # JSON 里的字符串也过一遍清洗（可能自带 "- 记住:" 或序号）；
            # 把人名当默认值传进去，这样 fact 自己带「阿哲：…」时仍以它为准
            p = _parse_memory_line(fact, who)
            if p:
                out.append((p[0] or who, p[1]))
            elif len(fact) >= 4:
                out.append((who, fact))
        if out:
            return out

    # ② 逐行（老行为）
    for line in raw.splitlines():
        s = line.strip()
        if not s or s in ("无", "無", "None", "none", "-", "*", "[]", "[ ]"):
            continue
        if s in ("[", "]", "{", "}", "```", "```json"):
            continue
        parsed = _parse_memory_line(s, default_person)
        if parsed and parsed[1] and parsed[1] != "无":
            out.append(parsed)
    if out:
        return out

    # ③ 整段兜底：模型完全没分行（只在有明显信息量时收，避免把「无」记进去）
    if "\n" not in raw and 4 <= len(raw) <= 120:
        p = _parse_memory_line(raw, default_person)
        if p and p[1] and p[1] != "无":
            return [p]
    return []


# ------------------------------------------------------------------
# 搬自 agent.py L3409-3453：extract_and_store
# ------------------------------------------------------------------
def extract_and_store(scope, sender_name, raw_text):
    """后台线程：让模型从对话里挑值得记的事实，存进 SQLite"""
    _sync()
    if not raw_text or len(raw_text) < 4:
        return
    # v1.1.2：前台在说话时让路；同一会话 45 秒内只提炼一次
    # v1.9.24：ns="memory" + slot="memory" —— 以前跟永久记忆提炼共用一个 45 秒窗口、
    # 还共用 _BG_SLOT，两条链同时起线程、谁先抢到谁把另一条整个堵死，
    # 会话记忆长期被饿死（真机：memories 0 行）。
    if not bg_slot_ok(scope, ns="memory", slot="memory"):
        return
    try:
        # v1.1.2：把最近几轮也带上，单句「好累啊」提炼不出东西，带上上下文才能
        dialog = f"{sender_name}：{raw_text}"
        try:
            _ctx = mem.get_recent_messages(scope, turns=3)
            if _ctx:
                _lines = [f"{'小鲸鱼' if r.get('role') == 'assistant' else sender_name}："
                          f"{(r.get('content') or '')[:80]}" for r in _ctx[-6:]]
                if _lines:
                    dialog = "\n".join(_lines)
        except Exception:
            pass
        # ★ 同上：记忆提取是结构化抽取任务，必须关掉思考，
        #   否则推理模型把 token 全花在 reasoning_content 上，正文为空。
        # v1.9.23：关键是这一行。以前是 _post_chat(全局 base) —— 用户把聊天切到
        # 云端后，全局还指着本地 LM Studio（192.168.x.x:1234），每轮都超时 90 秒，
        # 记忆一条也提不出来。现在跟聊天一样走优先级链，云端在前就用云端。
        text, reason = _extract_text(EXTRACT_PROMPT.format(dialog=dialog),
                                     EXTRACT_BUDGET, temperature=0.2,
                                     tag="/记忆提取", feature="memory", timeout=90)
        if not text:
            log(f"  [记忆] 模型没给出结果（{reason}），本轮跳过", "DEBUG")
            return
        stored = 0
        # v1.9.24：整段宽容解析（JSON 数组 / 逐行 / 整段兜底），不再只认 `- 记住:`
        for who, fact in _parse_memory_block(text, sender_name):
            if mem.add_fact(scope, who, fact):
                stored += 1
                log(f"  [记忆] {who}: {fact}", "INFO")
        if not stored:
            log("  [记忆] 本轮没有值得长期记住的内容", "DEBUG")
    except Exception as e:
        log(f"  [记忆] 提取失败: {e}", "ERROR")
    finally:
        bg_slot_free()


# ------------------------------------------------------------------
# 搬自 agent.py L3466-3497：_emotional_facts
# ------------------------------------------------------------------
def _emotional_facts(dialog):
    """v1.1.4 3.3：永久记忆的「情感兜底」。

    模型没抽出东西时，用规则把明显带情绪/关系的句子捞出来。
    宁可记多一条，也别让「我失恋了」这种话一句都不进长期记忆。
    """
    _sync()
    import re as _re
    out = []
    seen = set()
    for line in str(dialog or "").splitlines():
        t = line.split("：", 1)[-1].strip()
        if not t or len(t) < 4:
            continue
        for pat, cat, imp in _EMO_FACT_RULES:
            m = _re.search(pat, t)
            if not m:
                continue
            if cat == "like":
                content = "喜欢" + t[m.end():][:30].strip(" 。，！？,.!?")
            elif cat == "avoid":
                content = "不喜欢" + t[m.end():][:30].strip(" 。，！？,.!?")
            elif cat == "emotion":
                content = t[:60]
            else:
                content = t[:60]
            content = content.strip(" 。，！？,.!?")
            if len(content) < 3 or content in seen:
                continue
            seen.add(content)
            out.append({"category": cat, "content": content, "importance": imp})
            break
    return out[:4]


# ------------------------------------------------------------------
# 搬自 agent.py L3500-3583：_perm_extract_job
# ------------------------------------------------------------------
def _perm_extract_job(scope, sender_name, user_id):
    """后台线程：给「绑定过的账号」提炼永久记忆。

    用户需求：「绑定后记忆才会永久存储，其余的通过 QQ 聊天记录临时读取」。
    所以这里第一件事就是判断有没有绑定 —— 没绑定直接返回，一条都不写。

    v5：三档记忆策略
      temp   → 直接返回，一个字都不写（只靠 messages 表做临时上下文）
      auto   → 让模型自己判断有没有值得记的（模型说没有就不记）
      always → 每轮都提炼；模型没给出结果时退回正则规则兜底，尽量不漏
    """
    # v1.1.2：前台在说话时让路，别跟聊天抢模型
    # v1.9.24：ns="perm" —— 跟会话记忆链（ns="memory"）分开记 45 秒窗口
    _sync()
    if not bg_slot_ok(scope, ns="perm"):
        return
    try:
        uid = str(user_id or "").strip()
        if not uid or not diary_mod.is_bound(uid):
            return
        if MEMORY_MODE == "temp":
            log(f"  [永久记忆] 记忆策略=temp，跳过（不写永久记忆）", "DEBUG")
            return
        # 取这个会话最近的对话（含机器人回复，便于提炼上下文）
        rows = mem.get_recent_messages(scope, turns=12)
        if not rows:
            return
        dialog = "\n".join(
            f"{'我' if r.get('role') == 'assistant' else sender_name}："
            f"{(r.get('content') or '')[:200]}"
            for r in rows)
        res = diary_mod.extract_perm_from_dialog(
            mem, uid, sender_name, dialog,
            replace_conflict=MEMORY_REPLACE_CONFLICT,
            # auto 模式下模型说没有就是没有；always 模式允许规则兜底
            fallback_rule=(MEMORY_MODE == "always"))
        if res.get("added"):
            for it in res.get("items") or []:
                sup = it.get("superseded") or []
                tail = f"（替换旧 #{sup[0]}）" if sup else ""
                log(f"  [永久记忆] {sender_name}: [{it.get('category')}] "
                    f"{it.get('content')}{tail}", "INFO")
        else:
            log(f"  [永久记忆] 本轮没有新增（{res.get('msg')}）", "DEBUG")
            # v1.1.4 3.3：模型没抽到东西时，用规则兜住明显的情感/关系事实
            _facts = _emotional_facts(dialog)
            if _facts:
                _added = 0
                for _f in _facts:
                    try:
                        _r = mem.add_perm(uid, _f["content"], category=_f["category"],
                                          importance=int(_f.get("importance") or 3),
                                          source="情感兜底")
                        if _r:
                            _added += 1
                            log(f"  [永久记忆·兜底] {sender_name}: [{_f['category']}] "
                                f"{_f['content']}", "INFO")
                    except Exception as _e2:
                        log(f"  [永久记忆·兜底] 写入失败: {_e2}", "WARN")
                if _added:
                    log(f"  [永久记忆·兜底] 规则命中共写入 {_added} 条", "DEBUG")
        # v1.1.4 3.3：模型即使抽到了东西，情感/关系类事实也再兜一遍。
        # 原因：模型倾向于漏掉「我最近很累」「说好了周末去」这种没情绪词的承诺，
        # 而这些东西恰恰是最该长期记住的。兜底只补「模型没写过的」，最多 2 条。
        try:
            _known = [str(it.get("content") or "").strip() for it in (res.get("items") or [])]
            _extra = 0
            for _f in _emotional_facts(dialog):
                if _extra >= 2:
                    break
                _c = _f["content"]
                if any(_c and _c[:12] in k for k in _known):
                    continue
                _r = mem.add_perm(uid, _c, category=_f["category"],
                                  importance=int(_f.get("importance") or 3),
                                  source="情感兜底")
                if _r:
                    _extra += 1
                    log(f"  [永久记忆·补兜] {sender_name}: [{_f['category']}] {_c}", "INFO")
        except Exception as _e3:
            log(f"  [永久记忆·补兜] 跳过：{_e3}", "DEBUG")
    except Exception as e:
        log(f"  [永久记忆] 提炼失败: {e}", "ERROR")
    finally:
        bg_slot_free()


# ------------------------------------------------------------------
# 搬自 agent.py L4175-4216：_memory_text
# ------------------------------------------------------------------
def _memory_text(text):
    """「发给模型的那一版」→「用户真正说的那一版」，专门用来落库 / 写对话历史。

    v1.9.16（QA P3-1，导致她越聊越傻的那条）：
      以前 process_message 直接把 raw_text 写进 sessions 和长期记忆，而 raw_text 里
      带着合并壳「用户连续发送了多条消息… / [消息1：在吗] / [消息2：…]」。
      于是下一轮她从自己的历史里读到这些包装词，不但学不到「对方怎么说话」，
      还会跟着用这种腔调 —— 越聊越像机器。现在：
        · 发给模型的那一次请求：照旧带合并壳（她要看得懂「一个人连发了几条」）；
        · 写进 sessions / 长期记忆：只留原话，多句用换行拼成「第一句\\n第二句」，
          表情包图片保留成「[图片]」「[表情包：xx]」。
    """
    s = (text or "").strip()
    if not s:
        return ""
    if s.startswith(_MEDIA_ONLY_HEAD):
        # 纯表情/图片的合并模板，正文里没有原话，只把媒体标记抠出来
        marks = _MEDIA_MARK_RE.findall(s)
        return " ".join(marks)
    if not s.startswith(_MERGE_HEAD[:12]):
        return s
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    body_lines = []
    for l in lines[1:]:
        if l.startswith(_MERGE_TAIL[:8]):
            continue
        body_lines.append(l)
    body = "\n".join(body_lines)
    # 别按行切：用户自己在 QQ 里换行，或者一句话里带回车，按行切会把
    # 「[消息1：第一行」当成正文留下 —— 所以按「[消息N：」标记整段切。
    parts = re.split(r"\[消息\d+：", body)
    items = []
    if len(parts) > 1:
        for c in parts[1:]:
            c = c.strip()
            if c.endswith("]"):
                c = c[:-1].strip()
            if c:
                items.append(c)
        if items:
            return "\n".join(items)
    return body or s




# ---------------------------------------------------------------
# 说明：这里没有 `import agent`；agent 需要的那几个名字由 agent.py 反向 import。
# ---------------------------------------------------------------
