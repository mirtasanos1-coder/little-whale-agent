# -*- coding: utf-8 -*-
"""模型调用链路（从 agent.py 物理搬出来，逻辑没改）。

管的是「怎么把一段 messages 送出去、拿回正文」：
  · payload 组装（_build_payload / _headers / _chat_url / _unsup / _tkey）
  · 单端点请求与错误分类（_post_chat：超时 / 连不上 / HTTP / 空正文 / 只有思考）
  · 端点冷却（_ep_*，V1.9.24 加的：挂掉的端点别每轮白等 90 秒）
  · 优先级链（_chain_targets / _feature_targets，云端在前、挂了顺延）
  · 多端点重试（_ask_text）与提取专用入口（_extract_text）
  · 用量记录（_record_usage / _usage_*）
  · 两个对外入口 ask_model / ask_model_raw，以及三档优先级队列（_pq_*）

不在这里的（故意留下）：apply_provider / is_reasoning_model / probe_model ——
它们要就地改写 agent 的配置全局与缓存，放在 agent 里当「配置层」更安全；
本模块通过 _sync() 和转发函数使用它们。

跨模块的「活」名字：函数体里写的是 `LM_MODEL` / `mem` / `_post_chat` 这种裸名字，
`_sync()` 在每次调用前把 agent 的最新值灌进本模块（见 core.globals.sync）。
这样既不会吃「导入时的快照」，也让测试/插件对 agent.xxx 的就地替换依然生效。
"""
import json
import os
import re
import threading
import time
import traceback

import requests

import providers as prov_mod
import usage as usage_mod
from core import globals as _g
from core.prompt_builder import build_messages
from core.utils import (  # noqa: F401
    ENABLE_TIME_AWARENESS, MAX_CONTEXT_TOKENS, MIN_KEEP_MESSAGES,
    _switch, clean_reply, estimate_tokens, log, normalize_messages, strip_think,
    time_context,
)
from core.sender import _data_path


def _sync():
    """把 agent 里那些活名字的最新值灌进本模块（配置改了立刻生效）。"""
    _g.sync(_SYNC_NAMES, globals())


_SYNC_NAMES = (
    # ── 活配置（agent._reload_constants 会就地刷新，绝不能吃导入快照）──
    "LM_BASE", "LM_MODEL", "LM_API_KEY", "LM_IS_CLOUD",
    "LM_PROVIDER", "LM_PROVIDER_NAME",
    "LM_MAX_TOKENS", "LM_TEMPERATURE", "LM_TOP_P", "LM_TIMEOUT",
    "LM_REASONING_EFFORT", "LM_REASONING_MAX_TOKENS",
    "REPLY_TIMEOUT", "REPLY_RETRY_ON_TIMEOUT", "SYSTEM_PROMPT",
    # ── 运行时对象（启动后才赋值，重连/重载会换新对象）──
    "mem", "_FG_TS", "_live_cfg",
    # ── 本模块搬出来的那些名字也要跟 agent 对齐 ──
    # 这一条看着多余（两边的值本来就一样），其实是给「就地替换」留的缝：
    # 测试/插件会写 agent._post_chat = 假的，然后调 agent._ask_text；
    # 没有这一条的话 _ask_text 用的是本模块自己那份真的，补丁就不生效了。
    "_post_chat", "_chain_targets", "_ask_text", "_feature_targets", "_extract_text",
    "ask_vision", "ask_model", "ask_model_raw", "_build_payload", "_headers",
    "_unsup", "_tkey", "_tname", "_unsupported", "_EP_COOLDOWN", "_ENABLE_EP_COOLDOWN",
    "_resolve_effort", "_record_usage", "_usage_kind", "_usage_detail",
    "_payload_digest", "_chat_url", "_exc_hint", "_feature_of",
    "ep_cooldown_state", "_ep_cool", "_ep_clear", "_ep_cooling", "_usage_warned_today",
    "_pq_enter", "_pq_exit", "pq_state",
)


def _fwd(name):
    """造一个转发函数：调用 agent 里的同名函数（永远是最新那份）。"""

    def _call(*a, **k):
        return _g.call(name, *a, **k)

    _call.__name__ = name
    _call.__doc__ = "转发到 agent.%s(...)" % name
    return _call


# _prepare_messages / _chat_with_tools 留在 agent（要碰 emotion、工具表等活对象）；
# 其余几个属于提示词与思考拆解，但和 agent 的写库/兜底耦合较紧，先用转发。
_prepare_messages = _fwd("_prepare_messages")
_chat_with_tools = _fwd("_chat_with_tools")
fallback_reply = _fwd("fallback_reply")
refresh_system_prompt = _fwd("refresh_system_prompt")
remember_thinking = _fwd("remember_thinking")
split_thinking = _fwd("split_thinking")
is_reasoning_model = _fwd("is_reasoning_model")
probe_model = _fwd("probe_model")


# ------------------------------------------------------------------
# 搬自 agent.py L1705-1705：_unsupported
# ------------------------------------------------------------------
_unsupported = {}             # {tkey: set(...)}


# ------------------------------------------------------------------
# 搬自 agent.py L1709-1712：_tkey
# ------------------------------------------------------------------
def _tkey(target=None):
    """一个端点的唯一标识，用来分桶缓存"""
    _sync()
    t = target or {}
    return f"{t.get('base') or LM_BASE}|{t.get('model') or LM_MODEL}"


# ------------------------------------------------------------------
# 搬自 agent.py L1715-1718：_unsup
# ------------------------------------------------------------------
def _unsup(target=None):
    """取某个端点"不支持参数"集合（v7：分桶，不再是全局一个 set）"""
    k = _tkey(target)
    return _unsupported.setdefault(k, set())


# ------------------------------------------------------------------
# 搬自 agent.py L1721-1724：_tname
# ------------------------------------------------------------------
def _tname(target=None):
    """端点的可读名字（日志用）"""
    t = target or {}
    return str(t.get("label") or t.get("model") or t.get("base") or "未知端点")


# ------------------------------------------------------------------
# 搬自 agent.py L1735-1735：_EP_COOLDOWN
# ------------------------------------------------------------------
_EP_COOLDOWN = {}                      # tkey -> (过期时间戳, 原因)


# ------------------------------------------------------------------
# 搬自 agent.py L1736-1745：_EP_COOL_SEC
# ------------------------------------------------------------------
_EP_COOL_SEC = {
    "connection": 120.0,               # 连不上：LM Studio 没开 / 端口不通
    "timeout": 120.0,                  # 超时：模型没响应
    "quota": 600.0,                    # 用量闸门：要等第二天或用户改配置
    "http_429": 120.0,                 # 限流
    "http_500": 180.0, "http_502": 180.0,
    "http_503": 180.0, "http_504": 180.0,
    "http_401": 900.0, "http_403": 900.0,   # Key 无效：不会自己好
    "bad_json": 30.0, "bad_shape": 60.0,
}


# ------------------------------------------------------------------
# 搬自 agent.py L1747-1747：_ENABLE_EP_COOLDOWN
# ------------------------------------------------------------------
_ENABLE_EP_COOLDOWN = True


# ------------------------------------------------------------------
# 搬自 agent.py L1750-1757：_ep_cool_sec
# ------------------------------------------------------------------
def _ep_cool_sec(reason):
    """这个失败原因该冷却多久；0 表示不冷却。"""
    r = str(reason or "")
    if r in _EP_COOL_SEC:
        return float(_EP_COOL_SEC[r])
    if r.startswith("http_5"):
        return 180.0
    return 0.0


# ------------------------------------------------------------------
# 搬自 agent.py L1760-1773：_ep_cool
# ------------------------------------------------------------------
def _ep_cool(target, reason):
    """记下某个端点刚失败。冷却期内 _ask_text 不再试它。"""
    if not _ENABLE_EP_COOLDOWN:
        return
    sec = _ep_cool_sec(reason)
    if sec <= 0:
        return
    k = _tkey(target)
    until = time.time() + sec
    old = _EP_COOLDOWN.get(k)
    if old and until <= float(old[0]):
        return
    _EP_COOLDOWN[k] = (until, str(reason))
    log(f"[模型] 端点 {_tname(target)} 失败（{reason}），{int(sec)} 秒内不再重试", "WARN")


# ------------------------------------------------------------------
# 搬自 agent.py L1776-1786：_ep_cooling
# ------------------------------------------------------------------
def _ep_cooling(target):
    """端点是否在冷却中。返回 (是否冷却, 剩余秒数, 原因)。"""
    k = _tkey(target)
    it = _EP_COOLDOWN.get(k)
    if not it:
        return False, 0.0, ""
    left = float(it[0]) - time.time()
    if left <= 0:
        _EP_COOLDOWN.pop(k, None)
        return False, 0.0, ""
    return True, left, str(it[1])


# ------------------------------------------------------------------
# 搬自 agent.py L1789-1794：_ep_clear
# ------------------------------------------------------------------
def _ep_clear(target):
    """端点这次成功了 → 立刻解除冷却。"""
    k = _tkey(target)
    it = _EP_COOLDOWN.pop(k, None)
    if it:
        log(f"[模型] 端点 {_tname(target)} 已恢复，解除冷却", "INFO")


# ------------------------------------------------------------------
# 搬自 agent.py L1797-1807：ep_cooldown_state
# ------------------------------------------------------------------
def ep_cooldown_state():
    """给控制台看的冷却快照：[{端点, 剩余秒, 原因}]"""
    now = time.time()
    out = []
    for k, it in list(_EP_COOLDOWN.items()):
        left = float(it[0]) - now
        if left <= 0:
            _EP_COOLDOWN.pop(k, None)
            continue
        out.append({"endpoint": k, "left_sec": int(left), "reason": it[1]})
    return out


# ------------------------------------------------------------------
# 搬自 agent.py L1814-1814：_EFFORT_ALLOWED
# ------------------------------------------------------------------
_EFFORT_ALLOWED = ("none", "minimal", "low", "medium", "high", "xhigh")


# ------------------------------------------------------------------
# 搬自 agent.py L1817-1835：_resolve_effort
# ------------------------------------------------------------------
def _resolve_effort(effort):
    """把配置里的 reasoning_effort 解析成合法值，或返回 None 表示不带该参数。

    "auto" / "" / 非法值  → "none"（关闭思考，实测 0.7s 出话；不关要 34s）
    "omit" / "off"        → None（完全不发这个字段）
    合法值                → 原样发送
    """
    _sync()
    if effort is None:
        effort = LM_REASONING_EFFORT
    e = str(effort or "").strip().lower()
    if e in ("omit", "off", "none-send", "false"):
        return None
    if e in ("", "auto", "default", "true"):
        return "none"
    if e in _EFFORT_ALLOWED:
        return e
    # 用户手改了 config.json 写了个奇怪的值 → 退回最安全的 none
    log(f"[模型] reasoning_effort 配置值 {effort!r} 不合法，按 none 处理", "WARN")
    return "none"


# ------------------------------------------------------------------
# 搬自 agent.py L1838-1852：_build_payload
# ------------------------------------------------------------------
def _build_payload(messages, max_tokens=None, temperature=None, effort=None,
                   model=None, target=None):
    """按配置拼 payload。effort 传 "omit" 表示不带 reasoning_effort。"""
    _sync()
    payload = {
        "model": model or (target or {}).get("model") or LM_MODEL,
        "messages": messages,
        "max_tokens": int(max_tokens or LM_MAX_TOKENS),
        "temperature": float(LM_TEMPERATURE if temperature is None else temperature),
        "top_p": LM_TOP_P,
        "stream": False,
    }
    resolved = _resolve_effort(effort)
    if resolved and "reasoning_effort" not in _unsup(target):
        payload["reasoning_effort"] = resolved
    return payload


# ------------------------------------------------------------------
# 搬自 agent.py L1855-1867：_headers
# ------------------------------------------------------------------
def _headers(target=None):
    _sync()
    t = target or {}
    api_key = t.get("api_key", LM_API_KEY)
    is_cloud = bool(t.get("is_cloud", LM_IS_CLOUD))
    provider = t.get("provider", LM_PROVIDER)
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    if is_cloud and provider == "openrouter":
        # OpenRouter 建议带这两个头，方便在它的后台区分应用
        h["HTTP-Referer"] = "https://github.com/qq-agent"
        h["X-Title"] = "小鲸鱼 QQ Agent"
    return h


# ------------------------------------------------------------------
# 搬自 agent.py L1906-1916：_FEATURE_HINTS
# ------------------------------------------------------------------
_FEATURE_HINTS = (
    ("记忆提取", "memory"), ("永久记忆", "memory"),
    ("事件提取", "event"), ("待办", "event"),
    ("写日记", "diary"), ("日记", "diary"),
    ("学习", "learn"), ("动态", "qzone"), ("说说", "qzone"),
    ("评论", "qzone"), ("补回", "makeup"), ("冒泡", "chat"),
    ("画图", "draw"), ("生图", "draw"),
    ("启动检测", "check"), ("连通性", "check"), ("测试", "check"),
    ("滚动摘要", "summary"), ("摘要", "summary"),
    ("主动", "proactive"), ("关心", "proactive"), ("问候", "proactive"),
)


# ------------------------------------------------------------------
# 搬自 agent.py L1919-1928：_feature_of
# ------------------------------------------------------------------
def _feature_of(detail="", tag=""):
    """按记账用的 detail/tag 猜这是哪个功能的调用。

    这样不用去改几十个调用点——只要记账时写了用途，就能按功能分工选模型。
    """
    text = f"{detail or ''} {tag or ''}"
    for kw, feat in _FEATURE_HINTS:
        if kw and kw in text:
            return feat
    return "chat"


# ------------------------------------------------------------------
# 搬自 agent.py L1931-1952：_chat_url
# ------------------------------------------------------------------
def _chat_url(base):
    """把 base 拼成 chat/completions 的最终地址，返回 (url, 说明)。

    v1.3.3 修（用户实机报 [Errno 22] 时怀疑双斜杠）：统一在这一处拼，
    并把规整过程返回给调用方写进日志 —— 出问题时能一眼看到到底打了哪个地址。
      https://api.21r.cn/v1/   → https://api.21r.cn/v1/chat/completions
      https://api.21r.cn//v1// → https://api.21r.cn/v1/chat/completions（中间双斜杠折叠）
      https://api.21r.cn       → https://api.21r.cn/chat/completions（不带 /v1，照原样）
    """
    raw = str(base or "").strip()
    norm = raw.rstrip("/")
    m = re.match(r"^(https?://)(.*)$", norm, re.I)
    if m:
        norm = m.group(1) + re.sub(r"/{2,}", "/", m.group(2))
    url = f"{norm}/chat/completions"
    note = ""
    if raw and raw != norm:
        note = f"（base 规整：{raw} → {norm}）"
    tail = url.split("://", 1)[-1]
    if "//" in tail:
        note += "（警告：最终 URL 中间仍有双斜杠）"
    return url, note


# ------------------------------------------------------------------
# 搬自 agent.py L1955-1970：_exc_hint
# ------------------------------------------------------------------
def _exc_hint(e, url=""):
    """把底层异常翻成一句人话（用户要求：别只甩一个 [Errno 22]）。"""
    errno = getattr(e, "errno", None)
    if isinstance(e, OSError) and errno == 22:
        return ("Windows 报 [Errno 22] Invalid argument：这不是网络问题，通常是"
                "「控制台输出管道已关闭」（GUI 关掉了日志的 stdout），也可能是 URL 里 "
                f"有非法字符。最终请求地址是：{url}")
    if isinstance(e, requests.exceptions.InvalidURL):
        return f"URL 不合法：{url}（检查 base 有没有多/少 /v1，或粘进了空格、中文）"
    if isinstance(e, requests.exceptions.MissingSchema):
        return f"base 少了 http:// 或 https://：{url}"
    if isinstance(e, requests.exceptions.SSLError):
        return "TLS/证书握手失败：确认是 https 且证书链正常（公司代理/抓包工具会拦）"
    if isinstance(e, UnicodeError):
        return "base 或 Key 里有非法字符（常见是从网页复制带进了零宽字符/中文标点）"
    return ""


# ------------------------------------------------------------------
# 搬自 agent.py L1973-2116：_post_chat
# ------------------------------------------------------------------
def _post_chat(payload, timeout=None, tag="", target=None, detail="", feature=""):
    """发一次请求。

    返回 (text, reason, meta)：
      text   : 正文，失败为 None
      reason : "ok" / "http_400" / "timeout" / "empty" / "reasoning_only" / "quota" ...
      meta   : {"finish": ..., "reasoning_len": ..., "content_len": ..., "usage": ...}

    对推理模型特别处理：
      这个模型（如 qwen3.5 系列）默认把内容写进 reasoning_content，
      content 为空且 finish_reason=length，说明 max_tokens 全被思考吃掉了。
      这种情况返回 reason="reasoning_only"，由上层用更大 max_tokens 重试。

    v5：云端 API 调用前先过用量闸门，调用后记账。
    v7：target 指定具体端点（优先级链里的某一项）；不传就用当前全局配置。
    """
    _sync()
    t = dict(target or {})
    _feat = feature or _feature_of(detail, tag)
    if not t:
        try:
            _ft = prov_mod.feature_target(_feat)
        except Exception:
            _ft = None
        if _ft:
            t = _ft
            if _ft.get("model"):
                payload["model"] = _ft["model"]
            log(f"[分工] {prov_mod.feature_cn(_feat)} 用 {_ft['label']}", "DEBUG")
    base = t.get("base") or LM_BASE
    is_cloud = bool(t.get("is_cloud", LM_IS_CLOUD))
    # v1.1.7 修（P0-4：双斜杠 404）；v1.3.3 抽成 _chat_url()，规整说明进日志
    base = str(base or "").strip().rstrip("/")
    url, _url_note = _chat_url(base)
    if _url_note:
        log(f"[模型{tag}] 地址{_url_note}，最终请求 {url}", "DEBUG")
    timeout = timeout or LM_TIMEOUT
    n_msg = len(payload.get("messages") or [])

    # ① 用量闸门：云端超限直接拒绝，不发请求（防止账单失控）
    try:
        allowed, why = usage_mod.precheck(mem, is_cloud)
    except Exception:
        allowed, why = True, ""
    if not allowed:
        log(f"[用量{tag}] {why}", "ERROR")
        return None, "quota", {"body": why}

    try:
        r = requests.post(url, json=payload, headers=_headers(t), timeout=timeout)
    except requests.exceptions.Timeout:
        log(f"[模型{tag}] 请求超时（{timeout}s）@ {url}", "ERROR")
        _record_usage(payload, {}, ok=False, tag=tag, target=t or None, detail=detail)
        return None, "timeout", {}
    except requests.exceptions.ConnectionError as e:
        hint = "" if is_cloud else " —— LM Studio 没开？"
        log(f"[模型{tag}] 连不上 {url}{hint}({e})", "ERROR")
        _record_usage(payload, {}, ok=False, tag=tag, target=t or None, detail=detail)
        return None, "connection", {}
    except Exception as e:
        tb = traceback.format_exc()
        _errno_bit = f"；errno={getattr(e, 'errno', None)}" if getattr(e, "errno", None) else ""
        log(f"[模型{tag}] 请求异常: {type(e).__name__}: {e} @ {url}{_errno_bit}", "ERROR")
        log(f"[模型{tag}] 最终 URL = {url}；base = {base}；"
            f"payload={_payload_digest(payload)}", "ERROR")
        log(f"[模型{tag}] traceback:\n{tb}", "DEBUG")
        hint = _exc_hint(e, url)
        if hint:
            log(f"[模型{tag}] {hint}", "ERROR")
        _record_usage(payload, {}, ok=False, tag=tag, target=t or None, detail=detail)
        return None, "exception", {"url": url, "base": base,
                                   "exc": f"{type(e).__name__}: {e}",
                                   "traceback": tb, "hint": hint}

    if r.status_code != 200:
        body = (r.text or "")[:4000]
        log(f"[模型{tag}] HTTP {r.status_code} @ {url} —— messages={n_msg} "
            f"payload={_payload_digest(payload)}", "ERROR")
        log(f"[模型{tag}] response.text = {body}", "ERROR")
        # 云端常见的错误给出中文提示
        low = body.lower()
        if is_cloud:
            if r.status_code in (401, 403):
                log(f"[模型{tag}] API Key 无效或没权限，检查「设置 → 模型设置」里的 Key", "ERROR")
            elif r.status_code == 402 or "insufficient" in low or "balance" in low:
                log(f"[模型{tag}] 云端账户余额不足", "ERROR")
            elif r.status_code == 429:
                log(f"[模型{tag}] 触发云端限流（429），等一会儿再试", "ERROR")
        # 400 且提到了 reasoning_effort / 未知字段 → 记下来，后续不再带
        if r.status_code == 400 and "reasoning_effort" in payload:
            if ("reasoning_effort" in low or "unknown" in low or "unrecognized" in low
                    or "invalid" in low or "extra" in low):
                _unsup(target).add("reasoning_effort")
                log(f"[模型] {t.get('model') or LM_MODEL} 不认 reasoning_effort，"
                    f"本端点不再发送该参数", "WARN")
        _record_usage(payload, {}, ok=False, tag=tag, target=t or None, detail=detail)
        return None, f"http_{r.status_code}", {"body": body}

    try:
        data = r.json()
    except Exception as e:
        log(f"[模型{tag}] 返回不是合法 JSON: {e}；body={(r.text or '')[:1000]}", "ERROR")
        return None, "bad_json", {}

    try:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
    except Exception:
        log(f"[模型{tag}] 响应结构异常: {str(data)[:1000]}", "ERROR")
        return None, "bad_shape", {}

    finish = choice.get("finish_reason")
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    usage = data.get("usage") or {}
    # v1.9.15：模型可能不写话、而是要求调工具（tool_calls）。
    # 这里把原始结构带出去，由 _chat_with_tools() 执行完再喂回来。
    tool_calls = msg.get("tool_calls") or []
    meta = {"finish": finish, "reasoning_len": len(reasoning),
            "content_len": len(content), "usage": usage,
            "tool_calls": tool_calls}

    # 记账（云端/本地都记，看板里分开统计）
    _record_usage(payload, usage, ok=bool(content), tag=tag, target=target, detail=detail)

    if content:
        return content, "ok", meta

    # v1.9.15：正文为空但要求调工具 —— 不是失败，交给上层执行
    if tool_calls:
        log(f"[模型{tag}] 要求调用工具："
            + ", ".join(str((c.get("function") or {}).get("name")) for c in tool_calls),
            "DEBUG")
        return None, "tool_calls", meta

    # 正文为空：分三种情况
    if reasoning:
        # ① 有思考没正文 → 典型推理模型，且 max_tokens 被吃光
        log(f"[模型{tag}] 正文为空但思考有 {len(reasoning)} 字，finish={finish}"
            f"（推理模型吃光了 max_tokens）", "WARN")
        return None, "reasoning_only", meta
    # ② 完全空
    log(f"[模型{tag}] 返回空 content；finish_reason={finish}；msg_keys={list(msg.keys())}",
        "WARN")
    return None, "empty", meta


# ------------------------------------------------------------------
# 搬自 agent.py L2119-2134：_record_usage
# ------------------------------------------------------------------
def _record_usage(payload, usage, ok=True, tag="", target=None, detail=""):
    """把一次调用的 token 用量记进账本，并在接近上限时提醒一次。"""
    _sync()
    t = target or {}
    try:
        usage_mod.record(mem,
                         t.get("provider") or LM_PROVIDER,
                         t.get("model") or payload.get("model") or LM_MODEL,
                         usage,
                         kind=_usage_kind(tag),
                         is_cloud=bool(t.get("is_cloud", LM_IS_CLOUD)), ok=ok,
                         detail=_usage_detail(tag, t, detail))
        warn = usage_mod.warn_if_needed(mem, bool(t.get("is_cloud", LM_IS_CLOUD)))
        if warn:
            _usage_warned_today(warn)
    except Exception as e:
        log(f"[用量] 记账失败（忽略）: {e}", "DEBUG")


# ------------------------------------------------------------------
# 搬自 agent.py L2140-2161：_USAGE_TAG_CN
# ------------------------------------------------------------------
_USAGE_TAG_CN = {
    "": "正常调用",
    "/加大token": "加大 max_tokens 重试",
    "/无effort重试": "去掉 reasoning_effort 重试",
    "/无图重试": "去掉图片重试",
    "/短上下文重试": "缩短上下文重试",
    "/事件提取": "提取日程/事件",
    "/事件提取-加大": "提取日程/事件（加大）",
    "/记忆提取": "提炼永久记忆",
    "/记忆提取-加大": "提炼永久记忆（加大）",
    "/滚动摘要": "压缩滚动摘要",
    "/滚动摘要-加大": "压缩滚动摘要（加大）",
    "/主动回复决策": "判断要不要主动开口",
    "/探测": "连通性探测",
    "/探测-无effort": "连通性探测（不带 effort）",
    "/裸调用": "内部裸调用",
    "/永久记忆": "提炼永久记忆",
    "/说说": "写说说",
    "/评论回复": "回复别人的评论",
    "/动态": "读好友动态并决定要不要互动",
    "/主动关心": "主动关心",
}


# ------------------------------------------------------------------
# 搬自 agent.py L2164-2184：_usage_detail
# ------------------------------------------------------------------
def _usage_detail(tag, target=None, extra=""):
    """拼「这一笔做了什么」：用途 + 具体对象 + （重试方式）"""
    t = target or {}
    kind = _usage_kind(tag)
    base = {
        "chat": "聊天回复", "moment": "说说", "memory": "记忆提炼",
        "diary": "写日记", "proactive": "主动消息", "probe": "连通性测试",
    }.get(kind, "模型调用")
    bits = [base]
    ex = str(extra or "").strip()
    if ex:
        bits.append(ex)
    tag_s = str(tag or "")
    if tag_s and tag_s in _USAGE_TAG_CN:
        bits.append(_USAGE_TAG_CN[tag_s])
    elif tag_s:
        bits.append(tag_s.lstrip("/"))
    label = str(t.get("label") or "").strip()
    if label and label not in bits:
        bits.append(label)
    return " · ".join(bits)[:200]


# ------------------------------------------------------------------
# 搬自 agent.py L2187-2187：_usage_warn_cache
# ------------------------------------------------------------------
_usage_warn_cache = {"day": "", "sent": False}


# ------------------------------------------------------------------
# 搬自 agent.py L2190-2196：_usage_warned_today
# ------------------------------------------------------------------
def _usage_warned_today(msg):
    """同一天只提醒一次，避免每轮都刷日志"""
    _sync()
    today = time.strftime("%Y-%m-%d")
    if _usage_warn_cache.get("day") == today and _usage_warn_cache.get("sent"):
        return
    _usage_warn_cache.update(day=today, sent=True)
    log(f"[用量] {msg}", "WARN")


# ------------------------------------------------------------------
# 搬自 agent.py L2199-2212：_usage_kind
# ------------------------------------------------------------------
def _usage_kind(tag):
    """把调用点的 tag 归到看板的用途分类"""
    t = str(tag or "")
    if "说说" in t or "moment" in t.lower():
        return "moment"
    if "记忆" in t or "perm" in t.lower() or "抽取" in t:
        return "memory"
    if "日记" in t or "diary" in t.lower():
        return "diary"
    if "问候" in t or "查岗" in t or "补回" in t or "冒泡" in t:
        return "proactive"
    if "探测" in t:
        return "probe"
    return "chat"


# ------------------------------------------------------------------
# 搬自 agent.py L2215-2228：_payload_digest
# ------------------------------------------------------------------
def _payload_digest(payload):
    """给日志用的 payload 摘要：不打印 base64，只给结构"""
    msgs = payload.get("messages") or []
    digest = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, list):
            kinds = ",".join(p.get("type", "?") for p in c if isinstance(p, dict))
            digest.append(f"{m.get('role')}[{kinds}]")
        else:
            digest.append(f"{m.get('role')}({len(str(c or ''))}字)")
    return (f"model={payload.get('model')} n={len(msgs)} "
            f"max_tokens={payload.get('max_tokens')} "
            f"effort={payload.get('reasoning_effort', '-')} " + " -> ".join(digest))


# ------------------------------------------------------------------
# 搬自 agent.py L2315-2333：_chain_targets
# ------------------------------------------------------------------
def _chain_targets():
    """取优先级链；读不到就用当前单模型兜底（保证永远至少一个可试端点）。"""
    _sync()
    try:
        ch = prov_mod.chain()
    except Exception as e:
        log(f"[模型] 读取优先级链失败（回落单模型）：{e}", "WARN")
        ch = []
    if not ch:
        ch = [{
            "kind": "cloud" if LM_IS_CLOUD else "local",
            "provider": LM_PROVIDER,
            "provider_name": LM_PROVIDER_NAME,
            "base": LM_BASE,
            "model": LM_MODEL,
            "api_key": LM_API_KEY,
            "is_cloud": LM_IS_CLOUD,
            "label": f"{LM_PROVIDER_NAME} / {LM_MODEL}",
        }]
    return ch


# ------------------------------------------------------------------
# 搬自 agent.py L2336-2411：_ask_text
# ------------------------------------------------------------------
def _ask_text(messages, budget, temperature=None, effort="none", tag="/裸调用",
              allow_fallback=False, targets=None, timeout=None, respect_cooldown=True,
              raw=False):
    """把一组 messages 沿优先级链依次尝试，返回 (text, reason)。

    v7 核心：第 1 个端点彻底失败（连不上/超时/400/空正文）就顺延第 2 个，
    全部失败才返回 ("", 最后一个 reason)。

    v1.1.6 ⑫：targets 可显式指定端点列表（看图这类需要特定模型的功能用）。

    v1.9.24：respect_cooldown=True 时跳过「刚挂过、还在冷却期」的端点。
    这是用户实机上「每轮白等 90 秒 × 2」的直接解药 —— 端点挂了就记着，
    别每回合重新交一次学费。聊天主路径 ask_model 不走这里，所以用户
    正在等的那句回复永远会真的去试一次。
    """
    _sync()
    targets = list(targets) if targets else _chain_targets()
    if respect_cooldown and _ENABLE_EP_COOLDOWN and targets:
        alive = [t for t in targets if not _ep_cooling(t)[0]]
        if not alive:
            _left_info = []
            for t in targets:
                _cool, _left, _why = _ep_cooling(t)
                _left_info.append(f"{_tname(t)}（{_why}，还要 {int(_left)}s）")
            log(f"[模型{tag}] 全部 {len(targets)} 个端点都在冷却中，本次跳过："
                + "、".join(_left_info), "INFO")
            return "", "cooldown"
        if len(alive) < len(targets):
            _skipped = [t for t in targets if _ep_cooling(t)[0]]
            log(f"[模型{tag}] 跳过 {len(_skipped)} 个冷却中的端点："
                + "、".join(_tname(t) for t in _skipped), "DEBUG")
            targets = alive
    last_reason = "unknown"
    for idx, t in enumerate(targets):
        name = t.get("label") or t.get("model")
        t_budget = budget
        if is_reasoning_model(t):
            t_budget = max(int(budget), LM_REASONING_MAX_TOKENS)
        payload = _build_payload(messages, max_tokens=t_budget,
                                 temperature=temperature, effort=effort, target=t)
        text, reason, meta = _post_chat(payload, tag=tag, target=t, timeout=timeout)
        last_reason = reason

        # 推理模型吃光 token → 加倍重试
        if not text and reason == "reasoning_only":
            bigger = min(t_budget * 4, 16000)
            log(f"[模型{tag}] {name} 推理模型 token 不足，{t_budget} → {bigger} 重试", "WARN")
            payload2 = _build_payload(messages, max_tokens=bigger,
                                      temperature=temperature, effort="omit", target=t)
            text, reason, meta = _post_chat(payload2, tag=tag + "-加大", target=t,
                                           timeout=timeout)
            last_reason = reason

        # 400 且不认 reasoning_effort → 去掉参数重试
        if not text and reason == "http_400" and "reasoning_effort" in payload:
            log(f"[模型{tag}] {name} 带 reasoning_effort 报 400，去掉该参数重试", "WARN")
            _unsup(t).add("reasoning_effort")
            payload3 = _build_payload(messages, max_tokens=t_budget, effort="omit", target=t)
            text, reason, meta = _post_chat(payload3, tag=tag + "-无effort", target=t,
                                           timeout=timeout)
            last_reason = reason

        if text:
            _ep_clear(t)
            if idx > 0:
                log(f"[模型{tag}] 顺延到第 {idx + 1} 个端点 {name} 成功")
            return (strip_think(text).strip() if raw else clean_reply(text)), reason

        # v1.9.24：把这个端点的失败记下来，冷却期内不再重试
        _ep_cool(t, reason)

        if idx < len(targets) - 1:
            log(f"[模型{tag}] {name} 失败（{reason}），顺延下一个"
                f"（{idx + 2}/{len(targets)}）", "WARN")
        else:
            log(f"[模型{tag}] 最后一个端点 {name} 也失败（{reason}）", "WARN")
    return "", last_reason


# ------------------------------------------------------------------
# 搬自 agent.py L2414-2435：_feature_targets
# ------------------------------------------------------------------
def _feature_targets(feature=""):
    """v1.9.23：非聊天类任务（记忆/事件提取、摘要…）该打哪个端点。

    以前这些任务直接打全局单模型（`LM_BASE`）。用户把聊天切到云端之后，
    全局设置还停在本地 LM Studio 的地址上，于是日志里全是
    「[模型/记忆提取] 请求超时（90s）@ http://192.168.x.x:1234/v1/...」——
    聊得好好的，记忆一条都提取不出来。

    现在的规则（用户要的）：分工里单独配了这个功能就用它；没配就跟聊天
    走同一条优先级链（云端在前就先云端）。链也是空的才退回全局单模型。
    """
    _sync()
    tg = []
    if feature:
        try:
            import providers as _prov
            t = _prov.feature_target(feature)
            if isinstance(t, dict) and str(t.get("model") or "").strip():
                tg = [t]
                log(f"[分工] {_prov.feature_cn(feature)} 用 {t.get('label') or t.get('model')}", "DEBUG")
        except Exception as e:
            log(f"[分工] 读「{feature}」的专用模型失败（回退主链）：{e}", "DEBUG")
    return tg or _chain_targets()


# ------------------------------------------------------------------
# 搬自 agent.py L2438-2452：_extract_text
# ------------------------------------------------------------------
def _extract_text(prompt, budget, temperature=0.2, effort="none", tag="/提取",
                  feature="", timeout=None):
    """结构化抽取类调用统一入口：沿优先级链试，返回 (text, reason)。

    v1.9.23 修 P0：以前用 `_post_chat` 直接打全局 base，云端用户必然超时。
    现在和聊天一样走链，第 1 个端点失败会顺延到下一个。

    v1.9.24 修 P0（最隐蔽的那个）：raw=True 绕开 clean_reply 的「记住:」控制行剥离。
    以前这里返回的是 clean_reply(text)，而 clean_reply 会把 `- 记住: （人名）事实`
    整行挖掉（那是给聊天回复去控制行的），于是记忆提取**永远拿到空串** →
    extract_and_store 走 `if not text: return` → 真机 memories 表 0 行。
    """
    _sync()
    return _ask_text([{"role": "user", "content": prompt}], budget,
                     temperature=temperature, effort=effort, tag=tag,
                     targets=_feature_targets(feature), timeout=timeout, raw=True)


# ------------------------------------------------------------------
# 搬自 agent.py L2455-2475：ask_vision
# ------------------------------------------------------------------
def ask_vision(prompt, url, max_tokens=400, tag="/看图"):
    """v1.1.6 ⑫：看图专用调用，返回 (text, reason)。

    优先用「模型设置 → 按功能分工 → 看图识图」里指定的模型；没配就走主链，
    主链里没有视觉模型时调用方会收到空串（再降级成文字描述）。
    """
    _sync()
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": str(prompt or "")},
        {"type": "image_url", "image_url": {"url": str(url or "")}},
    ]}]
    tg = None
    try:
        import providers as prov_mod
        t = prov_mod.feature_target("vision")
        if isinstance(t, dict) and str(t.get("model") or "").strip():
            tg = [t]
            log(f"[看图] 用「看图识图」分工指定的模型：{t.get('label') or t.get('model')}", "DEBUG")
    except Exception as e:
        log(f"[看图] 读「看图识图」分工失败：{e}", "DEBUG")
    text, reason = _ask_text(msgs, max_tokens, effort="none", tag=tag, targets=tg)
    return (text or ""), (reason or "unknown")




def ask_model_raw(prompt, system=None, max_tokens=None, temperature=None,
                  effort="none", tag="/裸调用", no_time=False):
    """不带历史、不带记忆规则的轻量调用（用于发说说、生成问候等）。

    和 ask_model 的区别：
      - 只用一句 prompt，不拼历史/记忆/摘要
      - 默认 effort="none"（关思考），这类创作任务不需要推理过程
      - 失败返回 ""（调用方自己决定兜底）
    v7：同样走优先级链（第一个端点挂了自动顺延）。
    """
    _sync()
    sys_p = system if system is not None else SYSTEM_PROMPT
    if not sys_p:
        refresh_system_prompt()
        sys_p = SYSTEM_PROMPT
    # v1.9.11：情感判定这类「只要 JSON 打分」的调用不需要时间戳 ——
    # 带上它 system 每秒都在变，前缀缓存永远命中不了。
    if _switch("ENABLE_TIME_AWARENESS", ENABLE_TIME_AWARENESS) and not no_time:
        sys_p = sys_p + time_context()
    messages = [{"role": "system", "content": sys_p},
                {"role": "user", "content": prompt}]
    messages, _used = normalize_messages(messages, max_tokens=MAX_CONTEXT_TOKENS)

    budget = int(max_tokens or LM_MAX_TOKENS)
    text, reason = _ask_text(messages, budget, temperature=temperature,
                             effort=effort, tag=tag)
    if not text:
        log(f"[模型{tag}] 未取得正文（{reason}）", "WARN")
        return ""
    return text


def ask_model(history, user_text, images=None, scope=None, summary="", perm_user=None, perm_name="",
              allow_fallback=True, detail=""):
    """调用模型（v7：按优先级链依次尝试，失败顺延）。

    返回：字符串。allow_fallback=True 时保证非空（失败返回兜底话术）；
          allow_fallback=False 时失败返回 ""（主动发言场景用，宁可不发也不发傻话）。

    images:    可选，data URL 字符串列表
    scope:     会话标识（记忆按 scope 存取）
    perm_user: 可选，QQ 号；绑定过的账号会注入永久记忆
    allow_fallback: 模型彻底失败时是否用兜底话术顶上
    detail:    v1.1.0，用量明细里那句「这一笔做了什么」（如「私聊 · 安瑆.雨」）
    """
    _sync()
    global LM_REASONING_MAX_TOKENS

    # v1.1.2：告诉后台任务「前台正在/刚说过话」，让它们别抢模型
    _FG_TS["t"] = time.time()

    _dt = str(detail or "").strip()
    if not _dt and scope:
        _dt = "会话 " + str(scope)

    sys_prompt, memory_block, summary, messages = _prepare_messages(
        history, user_text, images, scope, summary, perm_user, perm_name)

    targets = _chain_targets()
    if len(targets) > 1:
        log(f"[模型] 优先级链共 {len(targets)} 个端点："
            + " → ".join(t.get("label") or t.get("model", "?") for t in targets), "DEBUG")

    last_reason = "unknown"
    _started = time.time()
    # v1.1.2：整条回复的总时间预算。以前一次失败就再等 300 秒，用户以为它死了。
    _budget = REPLY_TIMEOUT * (1 + REPLY_RETRY_ON_TIMEOUT)

    def _left():
        return _budget - (time.time() - _started)

    for idx, t in enumerate(targets):
        name = t.get("label") or t.get("model")

        # ★ 关键修复：推理模型需要大得多的 max_tokens，否则正文一个字都出不来
        max_tokens = LM_MAX_TOKENS
        if is_reasoning_model(t):
            max_tokens = LM_REASONING_MAX_TOKENS

        payload = _build_payload(messages, max_tokens=max_tokens, target=t)
        # v1.9.15：主调用走「带工具」通道；模型不认 tools 会自动退回普通调用
        text, reason, meta = _chat_with_tools(payload, messages, t, REPLY_TIMEOUT,
                                              detail=_dt, scope=scope, session_key=scope)
        last_reason = reason

        # ① 推理模型吃光 token → 加倍 max_tokens 重试（本次请求立刻补救）
        if not text and reason == "reasoning_only":
            bigger = min(max_tokens * 3, 16000)
            log(f"[模型] 推理模型 token 不足，max_tokens {max_tokens} → {bigger} 重试", "WARN")
            payload_big = _build_payload(messages, max_tokens=bigger, target=t)
            text, reason, meta = _post_chat(payload_big, timeout=REPLY_TIMEOUT, tag="/加大token",
                                            target=t, detail=_dt)
            last_reason = reason
            if text:
                if bigger > LM_REASONING_MAX_TOKENS:
                    LM_REASONING_MAX_TOKENS = bigger
                    # v1.9.26 拆分：这个「抬高」要写回 agent，
                    # 否则下次 _sync() 会拿 agent 的老值盖掉它。
                    _g.set_live("LM_REASONING_MAX_TOKENS", LM_REASONING_MAX_TOKENS)
                    log(f"[模型] 已记住该模型需要 max_tokens>={bigger}", "INFO")

        # ② 400 且可能是不认 reasoning_effort → 去掉该参数重试
        if not text and reason == "http_400" and "reasoning_effort" in payload:
            log("[模型] 带 reasoning_effort 报 400，去掉该参数重试", "WARN")
            _unsup(t).add("reasoning_effort")
            payload2 = _build_payload(messages, max_tokens=max_tokens, effort="omit", target=t)
            text, reason, meta = _post_chat(payload2, timeout=REPLY_TIMEOUT, tag="/无effort重试",
                                            target=t, detail=_dt)
            last_reason = reason

        # ③ 带图失败 → 去掉图片重试（图片格式/尺寸常触发 400）。
        #    但如果是超时就别折腾了：超时是模型忙/算得慢，去掉图片也一样慢。
        if not text and images and reason != "timeout":
            log("[模型] 带图调用失败，去掉图片重试一次", "WARN")
            msgs_noimg, _ = build_messages(
                sys_prompt, history,
                user_text + "\n（对方发了图片，但你没看到，自然回应即可）",
                images=None, memory_block=memory_block, summary=summary)
            payload3 = _build_payload(msgs_noimg, max_tokens=max_tokens, target=t)
            text, reason, meta = _post_chat(payload3, timeout=REPLY_TIMEOUT, tag="/无图重试",
                                            target=t, detail=_dt)
            last_reason = reason

        # ④ 仍然失败 → 砍一半历史再试（上下文过长的典型症状）。
        #    v1.1.2：① 超时且用户选了「不重试」就跳过；② 必须在总预算里，剩余不到 5 秒不试。
        if not text and reason == "timeout" and REPLY_RETRY_ON_TIMEOUT <= 0:
            log("[模型] 回复超时，且设置里不允许重试 → 直接兜底", "WARN")
        elif (not text and history and reason != "reasoning_only"
                and REPLY_RETRY_ON_TIMEOUT >= 1 and _left() > 5):
            half = max(MIN_KEEP_MESSAGES, len(history) // 2)
            _tmo = max(8, int(min(REPLY_TIMEOUT, _left())))
            log(f"[模型] 二次失败，历史砍到最近 {half} 条重试（超时 {_tmo}s）", "WARN")
            msgs_short, _ = build_messages(sys_prompt, history[-half:], user_text,
                                          images=None, memory_block="", summary="")
            payload4 = _build_payload(msgs_short, max_tokens=max_tokens, target=t)
            text, reason, meta = _post_chat(payload4, timeout=_tmo,
                                            tag="/短上下文重试", target=t, detail=_dt)
            last_reason = reason

        if text:
            if idx > 0:
                log(f"[模型] 顺延到第 {idx + 1} 个端点 {name} 成功")
            think, body = split_thinking(text)
            cleaned = clean_reply(body)
            if think:
                remember_thinking(scope, user_text, think, cleaned)
            if cleaned:
                return cleaned
            log(f"[模型] {name} 清洗后为空，继续下一个", "WARN")
            continue

        if idx < len(targets) - 1:
            log(f"[模型] {name} 失败（{reason}），顺延下一个（{idx + 2}/{len(targets)}）", "WARN")
        else:
            log(f"[模型] 全部 {len(targets)} 个端点都失败（{reason}）", "WARN")

    if not allow_fallback:
        log(f"[模型] 全部重试失败（{last_reason}），本次不发送（调用方允许静默）", "WARN")
        return ""
    fb = fallback_reply()
    log(f"[模型] 全部重试失败（{last_reason}），发兜底话术：{fb}", "ERROR")
    return fb


# ==================== v1.1.5.1：优先级队列（用户点名的核心任务）====================
# 三档，语义固定，接口预留后续加档位：
#   1 = 用户在等回复（最高）——立刻占有模型；
#   2 = 被唤醒后的待办/无归属回复（中）；
#   3 = 主动消息 / 日记 / 学习 / 动态 / 总结（最低）——等前台空闲再跑。
# 目的：后台一多，用户问一句要排队十几秒；现在后台让路。
_PQ = {"fg": 0, "bg_wait": 0, "bg_run": 0, "last_fg": 0.0}
_PQ_LOCK = threading.Condition()
PQ_PRIO_USER, PQ_PRIO_NORMAL, PQ_PRIO_BG = 1, 2, 3


def _pq_enter(prio):
    _sync()
    p = int(prio or PQ_PRIO_NORMAL)
    try:
        if not bool(_live_cfg("priority_queue_enabled", True)):
            return p
    except Exception:
        pass
    with _PQ_LOCK:
        if p >= PQ_PRIO_BG:
            _PQ["bg_wait"] += 1
            t0 = time.time()
            # 前台最多等 25 秒，避免后台任务被无限饿死
            while _PQ["fg"] > 0 and (time.time() - t0) < 25:
                _PQ_LOCK.wait(0.5)
            _PQ["bg_wait"] -= 1
            _PQ["bg_run"] += 1
        elif p <= PQ_PRIO_USER:
            _PQ["fg"] += 1
            _PQ["last_fg"] = time.time()
    # v1.8.12：每次进出队列都刷一次快照，控制台才看得到实时数字
    try:
        pq_state()
    except Exception:
        pass
    return p


def _pq_exit(p):
    p = int(p or PQ_PRIO_NORMAL)
    with _PQ_LOCK:
        if p >= PQ_PRIO_BG:
            _PQ["bg_run"] = max(0, _PQ["bg_run"] - 1)
        elif p <= PQ_PRIO_USER:
            _PQ["fg"] = max(0, _PQ["fg"] - 1)
        _PQ_LOCK.notify_all()
    try:
        pq_state()
    except Exception:
        pass


_PQ_FILE_TS = {"t": 0.0}


def pq_state():
    """给控制台/自检看：现在几档各有几个在跑。

    v1.8.11：顺手落个盘。控制台的「请求队列」那一格以前永远是「—」，
    因为队列计数器只在 Agent 进程的内存里，而 GUI 是另一个进程，读不到。
    """
    _sync()
    with _PQ_LOCK:
        st = {"fg": _PQ["fg"], "bg_wait": _PQ["bg_wait"], "bg_run": _PQ["bg_run"],
                "enabled": bool(_live_cfg("priority_queue_enabled", True)),
                # v1.8.12：控制台要显示「运行线程数」，Agent 的活线程数只有自己知道
                "threads": threading.active_count()}
    try:
        _now = time.time()
        if _now - float(_PQ_FILE_TS.get("t") or 0) >= 1.0:
            _PQ_FILE_TS["t"] = _now
            _p = _data_path("pq_stats.json")
            _d = dict(st)
            _d["at"] = int(_now)
            with open(_p + ".tmp", "w", encoding="utf-8") as _f:
                json.dump(_d, _f, ensure_ascii=False)
            os.replace(_p + ".tmp", _p)
    except Exception:
        pass
    return st


_ask_model_raw_impl = ask_model_raw
_ask_model_impl = ask_model


def ask_model_raw(prompt, system=None, max_tokens=None, temperature=None,
                  effort="none", tag="/裸调用", prio=PQ_PRIO_NORMAL,
                  no_time=False):
    """ask_model_raw 的优先级包装（参数与原来完全一致，多一个 prio）。"""
    _sync()
    p = _pq_enter(prio)
    try:
        return _ask_model_raw_impl(prompt, system=system, max_tokens=max_tokens,
                                   temperature=temperature, effort=effort, tag=tag,
                                   no_time=no_time)
    finally:
        _pq_exit(p)


def ask_model(history, user_text, images=None, scope=None, summary="",
              perm_user=None, perm_name="", allow_fallback=True, detail="",
              prio=PQ_PRIO_USER):
    """ask_model 的优先级包装（默认「用户在等回复」这一档）。"""
    _sync()
    p = _pq_enter(prio)
    try:
        return _ask_model_impl(history, user_text, images=images, scope=scope,
                               summary=summary, perm_user=perm_user,
                               perm_name=perm_name, allow_fallback=allow_fallback,
                               detail=detail)
    finally:
        _pq_exit(p)


# ---------------------------------------------------------------
# 说明：这里没有 `import agent`；需要 agent 的活名字一律走 core.globals。
# ---------------------------------------------------------------
