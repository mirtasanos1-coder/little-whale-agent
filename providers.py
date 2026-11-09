# -*- coding: utf-8 -*-
"""模型提供方（Provider）预设表 —— 本地 LM Studio 与云端 API 统一抽象

为什么需要这个模块：
    原来 agent.py 里只有 LM Studio 一种接法（http://127.0.0.1:1234/v1）。
    但很多用户电脑跑不动 35B 模型，想直接用云端 API（DeepSeek / 通义 / 智谱…）。
    这些服务的接口都是 OpenAI 兼容格式，只有 base_url 和默认模型名不同，
    所以抽成一张表，界面下拉选一下就能切换。

安全约定：
    1. api_key 只写进本地 config.json，绝不进开源包（build.py 的
       NEVER_OVERWRITE / SOURCE_NAME_EXCLUDE 已排除 config.json）。
    2. 本模块不打印 key 全文，日志里一律打码（sk-1234****abcd）。
    3. base 地址必须 http(s)，不允许 file:// 之类。
"""
import json
import os
import re

import config as cfg_mod

# ==================== 预设表 ====================
# base    : OpenAI 兼容的 /v1 根地址（agent.py 会拼 /chat/completions）
# models  : 推荐模型（界面上给下拉候选，用户也可以自己填）
# key_url : 去哪申请 API Key（界面上的「去申请 Key」按钮直接打开）
# needs_key: 需要 API Key 吗
# 说明：models 只是"候选"，随时会过时；界面上点「拉取」会用服务端真实列表覆盖。
PROVIDERS = {
    "local": {
        "name": "本地 LM Studio",
        "base": "http://127.0.0.1:1234/v1",
        "models": [],
        "needs_key": False,
        "key_url": "https://lmstudio.ai/",
        "note": "免费、离线、隐私最好。需要电脑能跑得动模型。",
    },
    "ollama": {
        "name": "本地 Ollama",
        "base": "http://127.0.0.1:11434/v1",
        "models": [],
        "needs_key": False,
        "key_url": "https://ollama.com/download",
        "note": "免费、离线。装完 Ollama 后 ollama pull 一个模型就能用。"
    },
    "deepseek": {
        "name": "DeepSeek（官方）",
        "base": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "needs_key": True,
        "key_url": "https://platform.deepseek.com/api_keys",
        "note": "便宜，中文好。deepseek-chat 日常聊天够用。",
    },
    "openai": {
        "name": "OpenAI",
        "base": "https://api.openai.com/v1",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1", "gpt-4.1-nano"],
        "needs_key": True,
        "key_url": "https://platform.openai.com/api-keys",
        "note": "国内需要代理；贵，但稳定。",
    },
    "moonshot": {
        "name": "月之暗面 Kimi",
        "base": "https://api.moonshot.cn/v1",
        "models": ["kimi-latest", "moonshot-v1-8k", "moonshot-v1-32k",
                   "moonshot-v1-128k", "kimi-k2-0905-preview"],
        "needs_key": True,
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "note": "长上下文，中文自然。",
    },
    "zhipu": {
        "name": "智谱 GLM",
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus", "glm-4-long"],
        "needs_key": True,
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "note": "glm-4-flash 有免费额度，适合先试。",
    },
    "qwen": {
        "name": "阿里通义千问",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-turbo", "qwen-plus", "qwen-max", "qwen-long"],
        "needs_key": True,
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
        "note": "国内直连快，qwen-turbo 很便宜。",
    },
    "openrouter": {
        "name": "OpenRouter（聚合）",
        "base": "https://openrouter.ai/api/v1",
        "models": ["deepseek/deepseek-chat", "anthropic/claude-3.5-sonnet",
                   "google/gemini-flash-1.5", "meta-llama/llama-3.3-70b-instruct"],
        "needs_key": True,
        "key_url": "https://openrouter.ai/keys",
        "note": "一个 key 用很多家模型，按量付费。",
    },
    "siliconflow": {
        "name": "硅基流动 SiliconFlow",
        "base": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-7B-Instruct",
                   "Qwen/Qwen2.5-72B-Instruct"],
        "needs_key": True,
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "note": "国内聚合，有免费小模型额度。",
    },
    "custom": {
        "name": "自定义（OpenAI 兼容）",
        "base": "",
        "models": [],
        "needs_key": True,
        "key_url": "",
        "note": "任何 OpenAI 兼容接口：填 base_url（含 /v1）+ 模型名 + key。",
    },
}

# v1.8.2：本地提供方（免费、不要 Key、算「本地」这一档）
# 判断本地/云端一律用这一对，别写成 == "local" —— 那样 Ollama 会被当成云端。
LOCAL_KEYS = ("local", "ollama")

DEFAULT_PROVIDER = "local"


def mask_key(key):
    """日志/界面用的打码。绝不返回完整 key。"""
    s = str(key or "")
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:6]}****{s[-4:]}"


def list_features():
    """给界面用：所有功能 + 当前分工。"""
    fm = feature_models()
    out = []
    for k, cn, desc in FEATURES:
        one = fm.get(k) or {}
        out.append({
            "key": k, "name": cn, "desc": desc,
            "provider": str(one.get("provider") or ""),
            "base": str(one.get("base") or ""),
            "model": str(one.get("model") or ""),
            "has_key": bool(dec_key(str(one.get("api_key") or ""))),
            "custom": bool(str(one.get("model") or "").strip()),
        })
    return out


def list_providers():
    """给界面用的预设列表（不含任何 key）"""
    out = []
    for key, p in PROVIDERS.items():
        out.append({
            "key": key,
            "name": p["name"],
            "base": p["base"],
            "preset_base": p["base"],
            "models": list(p["models"]),
            "preset_model": (p["models"][0] if p["models"] else ""),
            "needs_key": bool(p["needs_key"]),
            "is_cloud": key not in LOCAL_KEYS,
            "note": p["note"],
            "key_url": p.get("key_url", ""),
        })
    return out


def get_provider(key):
    """取一个预设，找不到返回 local（永不返回 None，避免上层 None 崩）"""
    k = str(key or "").strip().lower()
    if k in PROVIDERS:
        return {"key": k, "is_cloud": k not in LOCAL_KEYS, **PROVIDERS[k]}
    return {"key": DEFAULT_PROVIDER, "is_cloud": False,
            **PROVIDERS[DEFAULT_PROVIDER]}


def _valid_base(url):
    """base 必须是 http(s) 且带主机名"""
    s = str(url or "").strip()
    if not re.match(r"^https?://[^\s/]+", s, flags=re.I):
        return False
    return True


# ==================== v1.1.0：API Key 加密存储 ====================
# 以前 key 是明文躺在 config.json 里的，任何人打开那个文件就能拿走。
# 现在默认用系统级 DPAPI 加密（只有当前 Windows 用户能解开），
# 老配置里的明文不用改——is_encrypted() 认不出来就当明文用，兼容旧版本。
def enc_key(plain):
    """把明文 key 加密成可落盘的字符串（配置关了加密就原样返回）"""
    s = str(plain or "").strip()
    if not s:
        return ""
    try:
        if not cfg_mod.load().get("api_key_encrypt", True):
            return s
        import security
        return security.encrypt_secret(s)
    except Exception:
        return s


def dec_key(stored):
    """读出可用的明文 key（认不出的就当明文，避免老配置失效）"""
    s = str(stored or "")
    if not s:
        return ""
    try:
        import security
        if security.is_encrypted(s):
            got = security.decrypt_secret(s)
            if got:
                return got
            # 解密不出来（换电脑/换用户）就只能当没有，别把密文当 key 发出去
            return ""
    except Exception:
        pass
    return s



def local_engine(cfg=None):
    """当前选的本地引擎：lmstudio / ollama（认不出的按 LM Studio 处理）"""
    c = cfg if isinstance(cfg, dict) else cfg_mod.load()
    e = str(c.get("lm_engine") or "lmstudio").strip().lower()
    return e if e in cfg_mod.LOCAL_ENGINES else "lmstudio"


def local_default_base(cfg=None):
    """该引擎的默认 OpenAI 兼容地址（LM Studio 1234 / Ollama 11434）"""
    return cfg_mod.LOCAL_ENGINE_DEFAULT_BASE[local_engine(cfg)]


def set_local_engine(engine, base=""):
    """切换本地引擎，顺手把默认地址换过去。返回 (ok, msg)。"""
    e = str(engine or "").strip().lower()
    if e not in cfg_mod.LOCAL_ENGINES:
        return False, f"本地引擎只能取 {cfg_mod.LOCAL_ENGINES}"
    cfg = cfg_mod.load()
    cfg["lm_engine"] = e
    b = str(base or "").strip()
    if b and _valid_base(b):
        cfg["lm_base"] = b.rstrip("/")
    else:
        cfg["lm_base"] = cfg_mod.LOCAL_ENGINE_DEFAULT_BASE[e]
    cfg_mod.save(cfg)
    return True, f"本地引擎已切换为 {cfg_mod.LOCAL_ENGINE_CN[e]}（地址 {cfg['lm_base']}）"


def resolve():
    """把 config.json 里的设置解析成实际要用的连接参数。

    返回 {
      provider, provider_name, base, model, api_key, masked_key,
      is_cloud, needs_key, ok, msg
    }
    云端但没填 key / base 非法时 ok=False，msg 说明原因。
    """
    cfg = cfg_mod.load()
    pkey = str(cfg.get("lm_provider") or DEFAULT_PROVIDER).strip().lower()
    preset = get_provider(pkey)

    if preset["key"] in LOCAL_KEYS:
        # v1.8.2：选了 Ollama 但配置里还是 LM Studio 的地址时，按引擎给默认值
        _eng = "ollama" if preset["key"] == "ollama" else local_engine(cfg)
        base = str(cfg.get("lm_base") or
                   cfg_mod.LOCAL_ENGINE_DEFAULT_BASE.get(_eng, local_default_base(cfg))).strip()
        model = str(cfg.get("lm_model") or "").strip()
        api_key = dec_key(cfg.get("lm_api_key"))
    else:
        base = str(cfg.get("cloud_base") or preset["base"]).strip()
        model = str(cfg.get("cloud_model") or "").strip()
        api_key = dec_key(cfg.get("cloud_api_key"))
        if not model and preset["models"]:
            model = preset["models"][0]

    is_cloud = preset["key"] not in LOCAL_KEYS
    result = {
        "provider": preset["key"],
        "provider_name": preset["name"],
        "base": base,
        "model": model,
        "api_key": api_key,
        "masked_key": mask_key(api_key),
        "is_cloud": is_cloud,
        "needs_key": bool(preset["needs_key"]),
        "ok": True,
        "msg": "",
    }

    if not _valid_base(base):
        result.update(ok=False, msg=f"服务地址不合法：{base!r}（要以 http:// 或 https:// 开头）")
        return result
    if not model:
        result.update(ok=False, msg="还没选模型（设置 → 模型设置 里选一个，或手动填模型名）")
        return result
    if preset["needs_key"] and not api_key:
        result.update(ok=False, msg=f"{preset['name']} 需要 API Key（设置 → 模型设置 里填）")
        return result
    return result


def set_provider(pkey, base="", model="", api_key=None):
    """切换提供方并落盘。api_key=None 表示"不动原来的 key"。

    返回 (ok, msg)。
    """
    k = str(pkey or "").strip().lower()
    if k not in PROVIDERS:
        return False, f"没有这个提供方：{pkey}（可选 {', '.join(PROVIDERS)}）"
    preset = PROVIDERS[k]

    cfg = cfg_mod.load()
    cfg["lm_provider"] = k

    if k in LOCAL_KEYS:
        # v1.8.2：选 Ollama 时默认地址是 :11434，选 LM Studio 是 :1234
        # 选了哪家就把本地引擎一起切过去，避免地址和引擎对不上
        want_eng = "ollama" if k == "ollama" else "lmstudio"
        cfg["lm_engine"] = want_eng
        b = str(base or "").strip()
        if b:
            if not _valid_base(b):
                return False, f"服务地址不合法：{b!r}"
            cfg["lm_base"] = b.rstrip("/")
        else:
            cfg["lm_base"] = cfg_mod.LOCAL_ENGINE_DEFAULT_BASE[want_eng]
        if model:
            cfg["lm_model"] = str(model).strip()
        if api_key is not None:
            cfg["lm_api_key"] = enc_key(api_key)
    else:
        # 云端：base 为空时用预设，模型为空时用预设第一个
        b = str(base or "").strip() or preset["base"]
        if not _valid_base(b):
            return False, f"服务地址不合法：{b!r}"
        cfg["cloud_base"] = b
        m = str(model or "").strip() or (preset["models"][0] if preset["models"] else "")
        cfg["cloud_model"] = m
        if api_key is not None:
            cfg["cloud_api_key"] = enc_key(api_key)

    cfg_mod.save(cfg)
    tail = "（本地）" if k in LOCAL_KEYS else "（云端）"
    return True, f"已切换到 {preset['name']}{tail}"


def current_summary():
    """给界面顶部 chip / 状态栏用的一句话"""
    r = resolve()
    if not r["ok"]:
        return f"{r['provider_name']} · 未配置"
    if r["is_cloud"]:
        return f"{r['provider_name']} · {r['model']}"
    return r["model"] or r["provider_name"]


# ==================== v7：模型优先级链（失败顺延） ====================
MAX_CHAIN = 10

# v1.1.2：可以单独指定模型的「功能」
# 不在这张表里的调用一律走全局模型（或优先级链）
FEATURES = (
    ("chat", "聊天回复", "私聊/群聊回话"),
    ("makeup", "补回消息", "上线后补回漏掉的消息"),
    ("proactive", "主动消息", "主动问候/查岗/关心"),
    ("memory", "记忆提取", "从对话里抽永久记忆"),
    ("event", "待办提取", "从对话里抽待办"),
    ("diary", "写日记", "每天总结成日记"),
    ("learn", "学习提炼", "从对话里提炼经验"),
    ("qzone", "动态互动", "读动态/点赞/评论/回复"),
    ("draw", "画图判断", "判断这条要不要画、画什么"),
    ("vision", "看图识图", "人设图长相描述、看图说话（要选支持视觉的模型）"),
    ("check", "测试检测", "启动顺序判定/模型测试"),
    ("summary", "滚动摘要", "长上下文滚动摘要"),
)
FEATURE_KEYS = tuple(k for k, _cn, _d in FEATURES)


def feature_models(cfg=None):
    cfg = cfg or cfg_mod.load()
    raw = cfg.get("feature_models") or {}
    return raw if isinstance(raw, dict) else {}


def feature_cn(feature):
    for k, cn, _d in FEATURES:
        if k == feature:
            return cn
    return feature or "未指定"


def feature_target(feature, cfg=None):
    """这个功能单独配的模型；没配返回 None（调用方回退全局）。"""
    cfg = cfg or cfg_mod.load()
    fm = feature_models(cfg)
    one = fm.get(str(feature or "").strip())
    if not isinstance(one, dict) or not str(one.get("model") or "").strip():
        return None
    provider = str(one.get("provider") or "custom").strip()
    p = PROVIDERS.get(provider) or PROVIDERS[DEFAULT_PROVIDER]
    base = str(one.get("base") or "").strip() or p.get("base") or ""
    model = str(one.get("model") or "").strip()
    if not base.startswith(("http://", "https://")):
        return None
    api_key = dec_key(str(one.get("api_key") or ""))
    is_cloud = provider not in LOCAL_KEYS
    if is_cloud and not api_key:
        api_key = dec_key((cfg.get("provider_keys") or {}).get(provider) or "") \
            or dec_key(str(cfg.get("cloud_api_key") or ""))
    return {
        "ok": True,
        "kind": "cloud" if is_cloud else "local",
        "provider": provider,
        "provider_name": p.get("name") or provider,
        "base": base.rstrip("/"),
        "model": model,
        "api_key": api_key,
        "is_cloud": is_cloud,
        "needs_key": bool(p.get("needs_key", True)) and is_cloud,
        "missing_key": bool(p.get("needs_key", True)) and is_cloud and not api_key,
        "label": f"{p.get('name') or provider} / {model}",
        "feature": feature,
    }


def set_feature_model(feature, provider="", base="", model="", api_key=None, clear=False):
    """设置/清除某个功能的专用模型。返回 (ok, msg)。"""
    feature = str(feature or "").strip()
    if feature not in FEATURE_KEYS:
        return False, f"不认识的功能：{feature}"
    cfg = cfg_mod.load()
    fm = dict(feature_models(cfg))
    if clear:
        fm.pop(feature, None)
        ok, msg, _v = cfg_mod.set_checked("feature_models", fm)
        return (True, f"{feature_cn(feature)} 改回用全局模型") if ok else (False, msg)
    provider = str(provider or "").strip()
    if provider not in PROVIDERS:
        return False, f"不认识的提供方：{provider}"
    base = str(base or "").strip() or str(PROVIDERS[provider].get("base") or "")
    if not base.startswith(("http://", "https://")):
        return False, f"地址不合法：{base}"
    model = str(model or "").strip()
    if not model:
        return False, "还没填模型名"
    if PROVIDERS[provider].get("needs_key", True) and provider not in LOCAL_KEYS and api_key is None:
        old = dec_key(str((fm.get(feature) or {}).get("api_key") or ""))
        if not old and not dec_key(str(cfg.get("cloud_api_key") or "")):
            return False, "这个提供方要 Key，先填 Key（或去 API 卡填一次）"
    entry = {"provider": provider, "base": base, "model": model}
    if api_key is None:
        entry["api_key"] = str((fm.get(feature) or {}).get("api_key") or "")
    else:
        entry["api_key"] = enc_key(str(api_key)) if str(api_key).strip() else ""
    fm[feature] = entry
    ok, msg, _v = cfg_mod.set_checked("feature_models", fm)
    return (True, f"{feature_cn(feature)} 用 {PROVIDERS[provider]['name']} / {model}") if ok else (False, msg)


def feature_summary():
    fm = feature_models()
    if not fm:
        return "全部功能共用全局模型"
    return "、".join(f"{feature_cn(k)}→{v.get('model')}" for k, v in fm.items())



def _norm_entry(item):
    """把一条链项目规整成标准结构（不写盘）"""
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "").strip().lower()
    if kind not in ("local", "cloud"):
        return None
    provider = str(item.get("provider") or ("local" if kind == "local" else "custom")).strip().lower()
    if provider not in PROVIDERS:
        provider = "local" if kind == "local" else "custom"
    return {
        "kind": kind,
        "provider": provider,
        "base": str(item.get("base") or "").strip(),
        "model": str(item.get("model") or "").strip(),
        "api_key": str(item.get("api_key") or "").strip(),
    }


def _entry_target(entry, cfg=None):
    """把一条链项解析成"真的能发请求"的参数，附带 ok/msg。"""
    cfg = cfg if cfg is not None else cfg_mod.load()
    preset = get_provider(entry["provider"])
    kind = entry["kind"]
    if kind == "local":
        # v1.8.2：Ollama有自己的默认端口，别被 lm_base（LM Studio的 :1234）带跑
        if preset["key"] == "ollama" and not str(entry["base"] or "").strip():
            base = str(preset["base"]).strip()
        else:
            base = entry["base"] or str(cfg.get("lm_base") or preset["base"]).strip()
        model = entry["model"] or str(cfg.get("lm_model") or "").strip()
        key = dec_key(entry["api_key"]) or dec_key(cfg.get("lm_api_key"))
        is_cloud = False
    else:
        base = entry["base"] or str(preset["base"]).strip()
        model = entry["model"] or (preset["models"][0] if preset["models"] else "")
        keys = cfg.get("provider_keys") or {}
        key = (dec_key(entry["api_key"])
               or dec_key(keys.get(entry["provider"]))
               or dec_key(cfg.get("cloud_api_key")))
        is_cloud = True

    ok, msg = True, ""
    if not _valid_base(base):
        ok, msg = False, f"服务地址不合法：{base!r}"
    elif not model:
        ok, msg = False, "还没选模型"
    elif is_cloud and preset["needs_key"] and not key:
        ok, msg = False, f"{preset['name']} 需要 API Key"

    return {
        "kind": kind,
        "provider": preset["key"],
        "provider_name": preset["name"],
        "base": base,
        "model": model,
        "api_key": key,
        "masked_key": mask_key(key),
        "is_cloud": is_cloud,
        # needs_key = 这个提供方本来就要不要 Key（本地永远不要）
        # missing_key = 这一条现在缺不缺 Key（界面只对云端显示"缺 Key"）
        "needs_key": bool(is_cloud and preset["needs_key"]),
        "missing_key": bool(is_cloud and preset["needs_key"] and not key),
        "ok": ok,
        "msg": msg,
        "label": (f"{preset['name']} / {model}" if model else f"{preset['name']} / 未选模型"),
    }


def raw_chain():
    """配置里存的原始链（key 打码，给界面看）

    注意：这里**绝对不能**把明文 api_key 返回给界面/日志，
    真实 key 只在 chain() 里给 agent 用。曾经漏了这一层，
    结果界面上能抓到完整 sk-xxx。
    """
    cfg = cfg_mod.load()
    items = cfg.get("model_chain") or []
    out = []
    for i, it in enumerate(items):
        e = _norm_entry(it)
        if not e:
            continue
        t = _entry_target(e, cfg)
        t = dict(t)
        t["has_key"] = bool(t.get("api_key"))
        t["api_key"] = ""            # ← 打码：明文一律不出去
        t["index"] = i
        out.append(t)
    return out


def chain():
    """真正要用的优先级列表（已按开关过滤 + 已解析）。

    - model_chain 为空时，回退到老的单模型设置（保证升级不炸）
    - local_enabled=False 时去掉所有本地项；api_enabled=False 时去掉所有云端项
    """
    cfg = cfg_mod.load()
    local_on = bool(cfg.get("local_enabled", True))
    api_on = bool(cfg.get("api_enabled", False))

    items = []
    for it in (cfg.get("model_chain") or []):
        e = _norm_entry(it)
        if not e:
            continue
        if e["kind"] == "local" and not local_on:
            continue
        if e["kind"] == "cloud" and not api_on:
            continue
        items.append(e)

    # 老的安装没有 model_chain → 用当前单模型设置兜底
    if not items:
        r = resolve()
        if r["ok"]:
            kind = "cloud" if r["is_cloud"] else "local"
            if (kind == "local" and local_on) or (kind == "cloud" and api_on) or not (
                    local_on or api_on):
                items.append({
                    "kind": kind, "provider": r["provider"], "base": r["base"],
                    "model": r["model"], "api_key": r["api_key"],
                })
        elif local_on:
            items.append({
                "kind": "local", "provider": "local", "base": "",
                "model": str(cfg.get("lm_model") or "").strip(), "api_key": "",
            })

    if not bool(cfg.get("model_failover", True)):
        items = items[:1]

    out = []
    for e in items:
        t = _entry_target(e, cfg)
        if t["ok"]:
            out.append(t)
    return out[:MAX_CHAIN]


def chain_summary():
    """一行摘要，给启动日志/状态栏"""
    ch = chain()
    if not ch:
        return "没有可用模型（两个开关都关了，或配置不完整）"
    if len(ch) == 1:
        return ch[0]["label"]
    return " → ".join(t["label"] for t in ch[:3]) + (f" 等 {len(ch)} 个" if len(ch) > 3 else "")


def _save_chain(items):
    ok, msg, _v = cfg_mod.set_checked("model_chain", items)
    return ok, msg


def add_to_chain(kind, provider="", base="", model="", api_key="", front=False):
    """往优先级列表加一条。返回 (ok, msg)。

    front=True 时插到第 1 位（「保存并加入优先级」= 立刻生效的那个）。
    """
    kind = str(kind or "").strip().lower()
    if kind not in ("local", "cloud"):
        return False, "kind 只能是 local 或 cloud"
    cfg = cfg_mod.load()
    items = list(cfg.get("model_chain") or [])
    e = _norm_entry({"kind": kind, "provider": provider, "base": base,
                     "model": model, "api_key": enc_key(api_key)})
    if not e:
        return False, "参数不对"
    t = _entry_target(e, cfg)
    if not t["model"]:
        return False, "还没选模型"
    if not t["ok"]:
        return False, t["msg"]
    # 完全一样的（kind+provider+model+base）不重复加，但要按 front 挪位置
    for idx, old in enumerate(items):
        o = _norm_entry(old)
        if o and o["kind"] == e["kind"] and o["provider"] == e["provider"] \
                and o["model"] == e["model"] and o["base"] == e["base"]:
            if front and idx != 0:
                items.insert(0, items.pop(idx))
                ok, msg = _save_chain(items)
                return (True, f"{t['label']} 已在列表里，已提到第 1 位并生效") if ok else (False, msg)
            return False, f"{t['label']} 已经在列表里了"
    if len(items) >= MAX_CHAIN:
        return False, f"最多 {MAX_CHAIN} 个，先删一个再加"
    if front:
        items.insert(0, e)
    else:
        items.append(e)
    ok, msg = _save_chain(items)
    if not ok:
        return False, msg
    # 加完顺手把对应开关打开，否则列表里加了却不生效
    if kind == "local" and not cfg.get("local_enabled", True):
        cfg_mod.set_checked("local_enabled", True)
    if kind == "cloud" and not cfg.get("api_enabled", False):
        cfg_mod.set_checked("api_enabled", True)
    # 让「单模型设置」也跟上，避免列表清空后回退到旧模型
    try:
        if kind == "local":
            cfg_mod.set_checked("lm_model", e["model"])
            if e["base"]:
                cfg_mod.set_checked("lm_base", e["base"])
        else:
            cfg_mod.set_checked("lm_provider", e["provider"])
            cfg_mod.set_checked("cloud_model", e["model"])
            if e["base"]:
                cfg_mod.set_checked("cloud_base", e["base"])
    except Exception:
        pass
    return True, (f"已加入优先级第 1 位并生效：{t['label']}" if front
                  else f"已加入优先级列表：{t['label']}")


def update_in_chain(index, base=None, model=None, api_key=None):
    """编辑优先级列表里的某一项（改错字用，不用删了重填）。返回 (ok, msg)。"""
    cfg = cfg_mod.load()
    items = list(cfg.get("model_chain") or [])
    try:
        i = int(index)
    except (TypeError, ValueError):
        return False, "下标不对"
    if not (0 <= i < len(items)):
        return False, f"没有第 {i + 1} 项"
    e = _norm_entry(items[i])
    if not e:
        return False, "这一项格式坏了，删掉重加吧"
    if base is not None and str(base).strip():
        e["base"] = str(base).strip().rstrip("/")
    if model is not None and str(model).strip():
        e["model"] = str(model).strip()
    if api_key:
        e["api_key"] = enc_key(str(api_key).strip())
    t = _entry_target(e, cfg)
    if not t["model"]:
        return False, "模型名不能为空"
    if not _valid_base(t["base"]):
        return False, f"服务地址不合法：{t['base']!r}"
    if t["missing_key"]:
        return False, f"{t['provider_name']} 需要 API Key"
    items[i] = e
    ok, msg = _save_chain(items)
    return (True, f"已更新第 {i + 1} 项：{t['label']}") if ok else (False, msg)


def remove_from_chain(index):
    """按下标删一条。返回 (ok, msg)。"""
    cfg = cfg_mod.load()
    items = list(cfg.get("model_chain") or [])
    try:
        i = int(index)
    except (TypeError, ValueError):
        return False, "下标不对"
    if i < 0 or i >= len(items):
        return False, f"没有第 {i + 1} 项"
    gone = _norm_entry(items.pop(i))
    ok, msg = _save_chain(items)
    if not ok:
        return False, msg
    label = gone["model"] if gone else "该项"
    return True, f"已移除 {label}"


def move_in_chain(frm, to):
    """拖动排序：把第 frm 项移到第 to 位。返回 (ok, msg)。"""
    cfg = cfg_mod.load()
    items = list(cfg.get("model_chain") or [])
    try:
        a, b = int(frm), int(to)
    except (TypeError, ValueError):
        return False, "下标不对"
    if not (0 <= a < len(items)):
        return False, f"没有第 {a + 1} 项"
    b = max(0, min(len(items) - 1, b))
    if a == b:
        return True, "位置没变"
    items.insert(b, items.pop(a))
    ok, msg = _save_chain(items)
    return (True, f"已移到第 {b + 1} 位") if ok else (False, msg)


def clear_chain():
    ok, msg = _save_chain([])
    return (True, "已清空优先级列表（会回退到「保存并切换」的那个模型）") if ok else (False, msg)


if __name__ == "__main__":
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print("providers:", ", ".join(PROVIDERS))
    print(json.dumps(resolve(), ensure_ascii=False, indent=2))
