# -*- coding: utf-8 -*-
"""生图（可选）—— 让支持生图的模型自己画一张，当表情包/说说配图发出去

为什么单独一个模块
------------------
生图不是所有模型都有，接口也不统一。这里只实现最常见的一种：
**OpenAI 兼容的 `POST {base}/images/generations`**（DALL·E、gpt-image-1、
Flux、通义万相、硅基流动、各种中转站基本都是这个形状）。

配置（config.json）
-------------------
    imagegen_enabled   总开关，默认 False
    imagegen_base      服务地址，默认 https://api.openai.com/v1
    imagegen_model     模型名，如 gpt-image-1 / dall-e-3 / flux.1-schnell
    imagegen_api_key   密钥（只存本地）
    imagegen_size      尺寸，如 1024x1024
    imagegen_style     追加到 prompt 后面的风格词
    imagegen_timeout   超时秒数（生图慢，默认 180）
    imagegen_dir       产物目录（默认 generated/）

两条使用路径
------------
1. 聊天里"自己画一张"当表情包（`sticker_gen_enabled`，默认关，概率 `sticker_gen_chance`）
2. 发说说时配图（`moment_image_enabled` + `moment_image_source` 含 generated）

失败绝不影响主流程：所有函数都返回 dict，不抛异常。
"""
import base64
import json
import math
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request

_lock = threading.Lock()
_last_error = ""


def _data_dir():
    env_dir = os.environ.get("QQ_AGENT_DATA_DIR")
    if env_dir:
        try:
            os.makedirs(env_dir, exist_ok=True)
            return env_dir
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DATA_DIR = _data_dir()


def _cfg():
    try:
        import config as cfg_mod
        return cfg_mod.load()
    except Exception:
        return {}


def out_dir():
    c = _cfg()
    name = str(c.get("imagegen_dir") or "generated").strip() or "generated"
    d = name if os.path.isabs(name) else os.path.join(DATA_DIR, name)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


_IMAGE_HINTS = ("image", "img", "dall", "flux", "stable-diffusion", "sd3", "sdxl",
                "kolors", "seedream", "gpt-image", "cogview", "wanx", "doubao-seedream",
                "qwen-image", "hunyuan-image")


def _base(c):
    return str(c.get("imagegen_base") or "").strip().rstrip("/")


def _key(c):
    return str(c.get("imagegen_api_key") or "").strip()


def _chain_entries():
    """把「模型设置 → 优先级列表」里能用的端点捞出来。

    返回 [{base, model, api_key, provider, label, score}]，score 高的先试：
      * 模型名/提供方看着像生图模型的 → 2 分（用户特意放进列表的生图模型优先）
      * 云端 → 1 分（本地 LM Studio 一般不支持 /images/generations）
    """
    try:
        import providers
        items = providers.chain()
    except Exception:
        items = []
    out = []
    for t in items or []:
        try:
            b = str(t.get("base") or "").strip().rstrip("/")
            if not b.startswith("http"):
                continue
            mdl = str(t.get("model") or "").strip()
            pname = str(t.get("provider") or "")
            is_cloud = bool(t.get("is_cloud"))
            blob = (mdl + " " + pname).lower()
            score = 2 if any(h in blob for h in _IMAGE_HINTS) else (1 if is_cloud else 0)
            out.append({"base": b, "model": mdl,
                        "api_key": str(t.get("api_key") or "").strip(),
                        "provider": pname,
                        "label": str(t.get("label") or mdl or pname),
                        "score": score})
        except Exception:
            continue
    out.sort(key=lambda x: -x["score"])
    return out


def _targets(c):
    """这次生图要按什么顺序试哪些端点。

    顺序：
      1. 用户显式指定了 imagegen_base + imagegen_model → 只用它（强制指定）
      2. imagegen_use_current 打开 → 优先级列表里的端点（同一条链，失败自动顺延）
      3. 什么配置都没有 → 退回老的四件套默认值（保证不炸）
    """
    base_override = _base(c)
    mdl_override = str(c.get("imagegen_model") or "").strip()
    key = _key(c)
    out = []
    if base_override.startswith("http") and mdl_override:
        out.append({"base": base_override, "model": mdl_override, "api_key": key,
                    "provider": "custom", "label": "自定义 %s" % mdl_override,
                    "score": 99})
    if c.get("imagegen_use_current", True):
        for e in _chain_entries():
            item = dict(e)
            # 只指定了模型名、没指定地址 → 用链上的地址，但换成用户指定的模型
            if mdl_override and not base_override:
                item["model"] = mdl_override
                item["label"] = "%s @ %s" % (mdl_override, item["base"])
            out.append(item)
    if not out:
        out.append({"base": base_override or "https://api.openai.com/v1",
                    "model": mdl_override or "gpt-image-1", "api_key": key,
                    "provider": "custom", "label": "默认 %s" % (mdl_override or "gpt-image-1"),
                    "score": 0})
    # 去重（同 base+model 只留一次）
    seen, uniq = set(), []
    for t in out:
        k = (t["base"], t["model"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    return uniq


def targets_preview():
    """给界面看：现在会用哪个端点生图（不真的发请求）"""
    c = _cfg()
    return _targets(c)


# 这几个 provider 只会聊天/文本嵌入，不能出图（本地 LM Studio / Ollama 同属此类）
_TEXT_ONLY_PROVIDERS = ("local",)
_IMAGE_PROVIDERS = ("openai", "siliconflow", "zhipu", "qwen", "openrouter", "custom")


def available():
    """(ok, msg) —— 界面用它显示"当前能不能生图"。

    v1.1.5：以前只要优先级列表里有模型就说"可用"，于是本地聊天模型也被当成
    能画图，用户点「测试一张」永远失败，还以为是自己没配好。现在把"聊天模型
    不能画图"这件事直接说清楚，并给出该去哪配。
    """
    c = _cfg()
    if not c.get("imagegen_enabled"):
        return False, "生图开关还没打开"
    ts = _targets(c)
    if not ts:
        return False, "没有可用的模型 —— 先去「模型设置」把模型加进优先级列表"
    t = ts[0]
    if not t["model"]:
        return False, "生图用的端点没有模型名，去「模型设置」补一下"

    explicit = bool(_base(c).startswith("http") and str(c.get("imagegen_model") or "").strip())
    prov = str(t.get("provider") or "")
    if explicit:
        if not t["api_key"] and prov == "openai":
            return False, "OpenAI 官方生图要 API Key，先填一下"
        return True, "生图可用：%s（你指定了生图端点）" % t["label"]
    if prov in _TEXT_ONLY_PROVIDERS:
        return False, ("当前生图走的是本地聊天模型（%s），它只会写字、不能画图。"
                       "两种办法：① 在下面填一个支持 /v1/images/generations 的生图接口"
                       "（硅基流动 / OpenAI / 智谱等）并写模型名；"
                       "② 关掉「用当前模型设置」，换成本地生图服务（如 ComfyUI 的兼容代理）。"
                       % t["label"])
    if not t["api_key"]:
        if prov in _IMAGE_PROVIDERS and t["base"].startswith("https://api.openai.com"):
            return False, "OpenAI 官方生图要 API Key，先填一下"
        return True, "生图可用：%s（没填 Key，接口若允许匿名也能试）" % t["label"]
    return True, "生图可用：%s" % t["label"]



def _clean_prompt(prompt, c):
    p = re.sub(r"\s+", " ", str(prompt or "")).strip()
    style = str(c.get("imagegen_style") or "").strip()
    if style and style.lower() not in p.lower():
        p = p + ", " + style
    # 让模型画"头像/表情"而不是画真人照片：加个安全前置
    if p and "no text" not in p.lower():
        p = p + ", no text, no watermark"
    return p[:1200]


def _post_json(url, payload, headers, timeout):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8", "ignore"))


def _download(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "qq-agent/6"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _save_bytes(raw, ext=".png"):
    d = out_dir()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for i in range(50):
        name = "gen_%s_%02d%s" % (stamp, i, ext)
        path = os.path.join(d, name)
        if not os.path.exists(path):
            try:
                with open(path, "wb") as f:
                    f.write(raw)
                return path
            except Exception:
                return ""
    return ""


def _post_images(base, payload, headers, timeout):
    """POST /images/generations，返回 (data, err) —— 不抛异常。

    有的服务不认 response_format，遇到 400/422 就把它去掉再试一次。
    """
    try:
        return _post_json(base + "/images/generations", payload, headers, timeout), ""
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:600]
        except Exception:
            pass
        if e.code in (400, 404, 422) and "response_format" in body:
            p2 = {k: v for k, v in payload.items() if k != "response_format"}
            try:
                return _post_json(base + "/images/generations", p2, headers, timeout), ""
            except urllib.error.HTTPError as e2:
                b2 = ""
                try:
                    b2 = e2.read().decode("utf-8", "ignore")[:600]
                except Exception:
                    pass
                return None, "HTTP %s: %s" % (e2.code, b2 or body)
            except Exception as e2:
                return None, "重试仍失败：%s" % e2
        return None, "HTTP %s: %s" % (e.code, body)
    except Exception as e:
        return None, "连不上：%s" % e


def _post_chat_image(base, payload, headers, timeout):
    """POST /chat/completions 让「能出图的聊天模型」画一张。

    为什么要有这条路：很多中转站/聚合站根本没有 /images/generations
    （一调就 404 "Images API is not supported"），但它们的聊天模型
    支持 OpenAI 的 modalities 出图，图片放在 message.images 里。
    """
    try:
        return _post_json(base + "/chat/completions", payload, headers, timeout), ""
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:600]
        except Exception:
            pass
        # 有的服务不认 modalities，去掉再试一次
        if e.code in (400, 422) and "modalit" in body.lower():
            p2 = {k: v for k, v in payload.items() if k != "modalities"}
            try:
                return _post_json(base + "/chat/completions", p2, headers, timeout), ""
            except urllib.error.HTTPError as e2:
                try:
                    b2 = e2.read().decode("utf-8", "ignore")[:600]
                except Exception:
                    b2 = ""
                return None, "HTTP %s: %s" % (e2.code, b2 or body)
            except Exception as e2:
                return None, "重试仍失败：%s" % e2
        return None, "HTTP %s: %s" % (e.code, body)
    except Exception as e:
        return None, "连不上：%s" % e


_DATA_URI_RE = re.compile(r"data:image/(?:png|jpe?g|webp|gif);base64,([A-Za-z0-9+/=\s]+)")
_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_BARE_IMG_RE = re.compile(r"(https?://[^\s)\"']+\.(?:png|jpe?g|webp|gif)(?:\?[^\s)\"']*)?)", re.I)


def _extract_chat_images(r):
    """从聊天接口的返回里挖出图片：返回 [(kind, payload)]，kind 是 b64/url。"""
    out = []
    try:
        ch = (r or {}).get("choices") or []
        msg = (ch[0] or {}).get("message") or {} if ch else {}
        # 1) OpenAI 官方形状：message.images[].image_url.url
        for it in (msg.get("images") or []):
            u = ""
            if isinstance(it, dict):
                iu = it.get("image_url") or it.get("url") or ""
                u = iu.get("url") if isinstance(iu, dict) else iu
            elif isinstance(it, str):
                u = it
            if u:
                out.append(_classify_image_ref(u))
        # 2) 内容里内联 data URI / markdown 图片
        content = msg.get("content")
        if isinstance(content, list):   # 多段内容
            parts = []
            for seg in content:
                if isinstance(seg, dict):
                    parts.append(str(seg.get("text") or ""))
                    iu = seg.get("image_url") or {}
                    u = iu.get("url") if isinstance(iu, dict) else iu
                    if u:
                        parts.append(str(u))
                else:
                    parts.append(str(seg))
            content = "\n".join(parts)
        content = str(content or "")
        if content:
            for m in _DATA_URI_RE.finditer(content):
                out.append(("b64", re.sub(r"\s+", "", m.group(1))))
            for m in _MD_IMG_RE.finditer(content):
                out.append(_classify_image_ref(m.group(1)))
            for m in _BARE_IMG_RE.finditer(content):
                out.append(_classify_image_ref(m.group(1)))
    except Exception:
        pass
    # 去重去空
    seen, uniq = set(), []
    for kind, val in out:
        if not val:
            continue
        key = (kind, val[:200])
        if key in seen:
            continue
        seen.add(key)
        uniq.append((kind, val))
    return uniq


def _classify_image_ref(u):
    u = str(u or "").strip()
    if u.startswith("data:image"):
        m = _DATA_URI_RE.match(u)
        return ("b64", re.sub(r"\s+", "", m.group(1))) if m else ("", "")
    if u.startswith("http"):
        return ("url", u)
    return ("", "")


def _generate_one_chat(target, prompt, sz, c, timeout, ref=""):
    """走 /chat/completions 出图。返回 dict（形状与 _generate_one 一致）。

    ref: v1.9.15 新增的垫图（data URL 或 http 链接）。给了就把参考图一起发过去，
         让出图模型照着这张脸画，而不是凭空另起一个人。
    """
    global _last_error
    base = str(target.get("base") or "").rstrip("/")
    mdl = str(target.get("model") or "")
    headers = {"Content-Type": "application/json"}
    if target.get("api_key"):
        headers["Authorization"] = "Bearer " + target["api_key"]
    style = str(c.get("imagegen_style") or "").strip()
    ask = prompt if not style else (prompt + "，" + style)
    ask = ("请画一张图（%s）：%s\n只输出图片本身，不要解释。" % (sz, ask))
    content = ask
    if ref:
        content = [
            {"type": "text",
             "text": ask + "\n附带的这张图是这个角色的设定图，"
                          "画面里的人必须和它是同一张脸、同样的发色发型与穿搭。"},
            {"type": "image_url", "image_url": {"url": ref}},
        ]
    payload = {
        "model": mdl,
        "messages": [{"role": "user", "content": content}],
        "modalities": ["image", "text"],
        "stream": False,
    }
    t0 = time.time()
    r, err = _post_chat_image(base, payload, headers, timeout)
    fail = {"ok": False, "path": "", "url": "",
            "msg": err or "生成失败",
            "meta": {"model": mdl, "base": base, "via": "chat",
                     "label": target.get("label", "")}}
    if err:
        _last_error = "%s @ %s（聊天出图）：%s" % (mdl, base, err)
        return fail
    imgs = _extract_chat_images(r)
    if not imgs:
        txt = ""
        try:
            txt = str(((r or {}).get("choices") or [{}])[0].get("message", {}).get("content") or "")[:200]
        except Exception:
            pass
        _last_error = "%s 的聊天返回里没有图片：%s" % (mdl, txt or str(r)[:200])
        fail["msg"] = _last_error
        return fail
    kind, val = imgs[0]
    raw, url = None, ""
    if kind == "b64":
        try:
            raw = base64.b64decode(val)
        except Exception:
            raw = None
    else:
        url = val
        try:
            raw = _download(url, timeout)
        except Exception as e:
            _last_error = "下载生成图失败：%s" % e
            fail["msg"] = _last_error
            fail["url"] = url
            return fail
    if not raw:
        fail["msg"] = "拿到图片数据但解不出来"
        return fail
    path = _save_bytes(raw)
    if not path:
        fail["msg"] = "写文件失败"
        return fail
    meta = {"model": mdl, "base": base, "size": sz, "via": "chat",
            "seconds": round(time.time() - t0, 1),
            "bytes": len(raw), "prompt": prompt[:200]}
    _last_error = ""
    return {"ok": True, "path": path, "url": url,
            "msg": "已生成 %s（%.1fs，%dKB，%s·聊天出图）" % (
                os.path.basename(path), meta["seconds"], len(raw) // 1024,
                target.get("label") or mdl),
            "meta": meta}


# ---------- v1.4.2：SVG 生图（模型直接画矢量图，本地渲染成 PNG） ----------
_SVG_BLOCK_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.S | re.I)

_SVG_ASK = (
    "请画一张 SVG 插画，主题是：{prompt}\n"
    "只输出 SVG 代码本身，不要任何解释、不要 markdown 代码块标记。\n"
    "硬性要求：\n"
    '1) 根节点写成 <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
    'width="{w}" height="{h}">；\n'
    "2) 不要 <script>、不要 <foreignObject>、不要外链图片或字体；\n"
    "3) 用矩形/圆形/路径/渐变/文字把画面铺满，主体明确、配色协调、有明暗层次；\n"
    "4) 画面里不要写大段文字，能看懂的图形优先。"
)


def _extract_svg(text):
    """从模型回复里抠出 <svg>…</svg>（容忍 ```svg 围栏和前后废话）。"""
    if not text:
        return ""
    m = _SVG_BLOCK_RE.search(str(text))
    if not m:
        return ""
    svg = m.group(0).strip()
    low = svg.lower()
    if "<svg" not in low or "</svg>" not in low:
        return ""
    if "<script" in low:
        return ""
    return svg


def _svg_to_png(svg, w, h):
    """用 Playwright 把 SVG 渲染成 PNG；装不了浏览器就退 Edge/Chrome。"""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return "", "没装 playwright：%s" % e
    s = str(svg or "")
    head = s.split(">", 1)[0]
    if "xmlns" not in head:
        s = s.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    html = ("<!doctype html><html><head><meta charset='utf-8'>"
            "<style>html,body{margin:0;padding:0;background:#0b1020;overflow:hidden}"
            "svg{display:block;width:%dpx;height:%dpx}</style></head><body>%s</body></html>"
            % (int(w), int(h), s))
    try:
        with sync_playwright() as pw:
            try:
                import computer_use as _cu
                b = _cu._launch_browser(pw)
            except Exception:
                b = pw.chromium.launch(headless=True)
            pg = b.new_page(viewport={"width": int(w), "height": int(h)})
            try:
                pg.set_content(html, wait_until="load")
                raw = pg.screenshot(type="png")
            finally:
                try:
                    pg.close()
                except Exception:
                    pass
                try:
                    b.close()
                except Exception:
                    pass
        path = _save_bytes(raw)
        return path, ("" if path else "写文件失败")
    except Exception as e:
        return "", "%s: %s" % (type(e).__name__, e)


def _generate_one_svg(target, prompt, sz, c, timeout):
    """让模型写 SVG，再本地渲染成 PNG。返回结构跟其它出图方式一致。"""
    global _last_error
    base = str(target.get("base") or "").rstrip("/")
    mdl = str(target.get("model") or "")
    try:
        w, h = [int(x) for x in str(sz or "1024x1024").lower().split("x")[:2]]
    except Exception:
        w, h = 1024, 1024
    t0 = time.time()
    ask = _SVG_ASK.format(prompt=_clean_prompt(prompt, c), w=w, h=h)
    payload = {"model": mdl, "messages": [{"role": "user", "content": ask}],
               "temperature": 0.7, "stream": False}
    headers = {"Content-Type": "application/json"}
    if target.get("api_key"):
        headers["Authorization"] = "Bearer " + target["api_key"]
    r, err = _post_chat_image(base, payload, headers, timeout)
    if err:
        _last_error = "SVG 生图失败：%s @ %s：%s" % (mdl, base, err)
        return {"ok": False, "path": "", "url": "", "msg": _last_error,
                "meta": {"via": "svg", "model": mdl, "base": base}}
    txt = ""
    try:
        ch = ((r or {}).get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        txt = msg.get("content") or msg.get("reasoning_content") or ""
        if isinstance(txt, list):   # 少数网关把 content 拆成段落数组
            txt = "".join([x.get("text", "") if isinstance(x, dict) else str(x) for x in txt])
    except Exception:
        txt = ""
    svg = _extract_svg(txt)
    if not svg:
        _last_error = "模型没给出 SVG 代码（回了 %d 个字）" % len(str(txt))
        return {"ok": False, "path": "", "url": "", "msg": _last_error,
                "meta": {"via": "svg", "model": mdl, "base": base}}

    # 原样留一份 .svg（以后能再编辑/放大），再渲染一份 .png（QQ 里能直接显示）
    svg_path = _save_bytes(svg.encode("utf-8"), ext=".svg")
    png_path, rerr = _svg_to_png(svg, w, h)
    if not png_path:
        _last_error = "SVG 渲染成 PNG 失败：%s" % rerr
        return {"ok": False, "path": svg_path, "url": "", "msg": _last_error,
                "meta": {"via": "svg", "model": mdl, "base": base, "svg": svg_path}}
    secs = round(time.time() - t0, 1)
    _last_error = ""
    return {"ok": True, "path": png_path, "url": "",
            "msg": "已用 SVG 画好 %s（%.1fs，%dx%d，源文件 %s）" % (
                os.path.basename(png_path), secs, w, h, os.path.basename(svg_path or "")),
            "meta": {"via": "svg", "model": mdl, "base": base,
                     "seconds": secs, "svg": svg_path}}


def _generate_one(target, prompt, sz, c, timeout, ref=""):
    """对着一个端点试一次生图，返回 dict；失败时 meta 里带 target 方便排查。

    ref: 垫图（data URL）。给了就优先走聊天出图（只有它能带上参考图）。
    """
    global _last_error
    base = str(target.get("base") or "").rstrip("/")
    mdl = str(target.get("model") or "")
    headers = {"Content-Type": "application/json"}
    if target.get("api_key"):
        headers["Authorization"] = "Bearer " + target["api_key"]
    payload = {"model": mdl, "prompt": prompt, "n": 1, "size": sz,
               "response_format": "b64_json"}
    t0 = time.time()
    via = str(c.get("imagegen_via") or "auto").strip().lower()
    if ref and via in ("auto", "chat"):
        # v1.9.15：有垫图就先试聊天出图 —— 能把参考图一起发过去的那条路。
        # 不成再往下走普通生图（提示词里已经写死了外貌，不至于全无依据）。
        rc = _generate_one_chat(target, prompt, sz, c, timeout, ref=ref)
        if rc.get("ok"):
            return rc
    if via in ("svg", "vector"):
        return _generate_one_svg(target, prompt, sz, c, timeout)
    if via == "chat":
        return _generate_one_chat(target, prompt, sz, c, timeout, ref=ref)
    r, err = _post_images(base, payload, headers, timeout)
    fail = {"ok": False, "path": "", "url": "",
            "msg": err or "生成失败", "meta": {"model": mdl, "base": base,
                                             "label": target.get("label", "")}}
    if err:
        # 端点没有 /images/generations（404/405，或明确说 Images API 不支持）
        # → 换一条路：用聊天接口出图
        low = err.lower()
        if ("404" in low or "405" in low or "not found" in low
                or "images api" in low or "not supported" in low
                or "not implemented" in low):
            log_once = "%s @ %s 没有生图端点，改走聊天出图" % (mdl, base)
            try:
                _last_error = log_once
            except Exception:
                pass
            if via in ("auto", "chat"):
                rc = _generate_one_chat(target, prompt, sz, c, timeout, ref=ref)
                if rc.get("ok"):
                    return rc
                # 最后再试一次 SVG（模型直接写矢量图，不依赖生图 API）
                rs = _generate_one_svg(target, prompt, sz, c, timeout)
                if rs.get("ok"):
                    return rs
                _last_error = "%s；聊天出图也失败：%s；SVG 也失败：%s" % (
                    log_once, rc.get("msg", ""), rs.get("msg", ""))
                fail["msg"] = _last_error
                return fail
        _last_error = "%s @ %s：%s" % (mdl, base, err)
        return fail

    items = (r or {}).get("data") or []
    if not items:
        _last_error = "%s 的返回里没有 data：%s" % (mdl, str(r)[:300])
        fail["msg"] = _last_error
        return fail
    first = items[0] if isinstance(items[0], dict) else {}
    raw = None
    url = str(first.get("url") or "")
    b64 = first.get("b64_json") or first.get("b64") or ""
    if b64:
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raw = None
    if raw is None and url:
        try:
            raw = _download(url, timeout)
        except Exception as e:
            _last_error = "下载生成图失败：%s" % e
            fail["msg"] = _last_error
            fail["url"] = url
            return fail
    if not raw:
        _last_error = "服务既没给 b64_json 也没给 url"
        fail["msg"] = _last_error
        return fail

    path = _save_bytes(raw)
    if not path:
        fail["msg"] = "写文件失败"
        return fail
    meta = {"model": mdl, "base": base, "size": sz,
            "seconds": round(time.time() - t0, 1),
            "bytes": len(raw), "prompt": prompt[:200]}
    _last_error = ""
    return {"ok": True, "path": path, "url": url,
            "msg": "已生成 %s（%.1fs，%dKB，%s）" % (os.path.basename(path),
                                                  meta["seconds"], len(raw) // 1024,
                                                  target.get("label") or mdl),
            "meta": meta}


def generate(prompt, size=None, model=None, tag="", record=True, ref=""):
    """生成一张图，返回 dict：{ok, path, url, msg, meta}

    【v8】不再单独要一套 API —— 默认沿用「模型设置」里当前应用的模型：
    按优先级列表挨个试（生图模型 > 云端 > 本地），第一个成功的就用它。
    绝不抛异常 —— 生图失败就当没这回事，聊天/发说说照常继续。

    ref: v1.9.15 新增。垫图（data URL 或图片链接），画「同一个角色」时必须给，
         免得每次自画像都变成另一个陌生人。
    """
    global _last_error
    c = _cfg()
    ok, msg = available()
    if not ok:
        return {"ok": False, "path": "", "url": "", "msg": msg, "meta": {}}
    sz = str(size or c.get("imagegen_size") or "1024x1024").strip()
    text = _clean_prompt(prompt, c)
    if not text:
        return {"ok": False, "path": "", "url": "", "msg": "提示词是空的", "meta": {}}
    timeout = int(c.get("imagegen_timeout") or 180)

    targets = _targets(c)
    if model:
        # 调用方明确指定模型名 → 优先试它
        targets = [dict(t, model=str(model).strip()) for t in targets[:1]] + targets[1:]
    errors = []
    for i, t in enumerate(targets):
        if not str(t.get("model") or "").strip():
            continue
        r = _generate_one(t, text, sz, c, timeout, ref=ref)
        if r.get("ok"):
            if record:
                _record_usage(r["meta"]["model"], tag or "生图")
            if i > 0:
                r["msg"] += "（第 %d 个端点才成功）" % (i + 1)
            return r
        errors.append("%s：%s" % (t.get("label") or t.get("model"), r.get("msg", "")))
    # v1.5.0（用户第 12 条）：端点全军覆没 → 最后用 SVG 矢量图兜底，
    # 聊天模型能写字就能画，比「一张都出不来」强。
    try:
        _svg_t = next((x for x in targets if str(x.get("model") or "").strip()), None)
        if _svg_t:
            _rs = _generate_one_svg(_svg_t, text, sz, c, timeout)
            if _rs.get("ok"):
                if record:
                    _record_usage(_rs["meta"]["model"], (tag or "生图") + "(SVG 兜底)")
                _rs["msg"] = (_rs.get("msg") or "") + "（生图接口都失败了，改用 SVG 画的）"
                return _rs
            errors.append("SVG 兜底：%s" % _rs.get("msg", ""))
    except Exception as _e_svg:
        errors.append("SVG 兜底异常：%s" % _e_svg)
    _last_error = "所有端点都生不出来：\n" + "\n".join(errors[:4]) if errors else "没有可用端点"
    return {"ok": False, "path": "", "url": "", "msg": _last_error, "meta": {}}



def _record_usage(model, tag):
    """把这次生图记进用量明细（生图一般不计 token，但记次数有参考价值）。"""
    try:
        import memory as mem_mod
        import usage as usage_mod
        m = mem_mod.Memory()
        try:
            usage_mod.record(m, "imagegen", model, {"total_tokens": 0},
                             kind="image", is_cloud=True, ok=True)
            try:
                usage_mod.set_detail(m, "image", tag)
            except Exception:
                pass
        finally:
            m.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 给"表情包 / 说说配图"用的提示词
# ---------------------------------------------------------------------------
_EMOTION_LOOK = {
    "开心": "a cute cartoon whale grinning with sparkles",
    "生气": "a cute cartoon whale puffing steam, angry but adorable",
    "委屈": "a cute cartoon whale with teary eyes, looking wronged",
    "无语": "a cute cartoon whale doing a facepalm, speechless",
    "害羞": "a cute cartoon whale blushing and hiding its face",
    "调皮": "a cute cartoon whale sticking out its tongue playfully",
    "惊讶": "a cute cartoon whale with huge shocked eyes",
    "困": "a cute cartoon whale dozing off with a sleep bubble",
    "爱心": "a cute cartoon whale hugging a big heart",
    "点赞": "a cute cartoon whale giving a thumbs up",
    "吃饭": "a cute cartoon whale holding a bowl of rice happily",
    "打招呼": "a cute cartoon whale waving hello",
}


def sticker_prompt(emotions=None, persona_name="", extra=""):
    """根据情绪拼一个"表情包"提示词。"""
    emo = (emotions or ["开心"])[0]
    look = _EMOTION_LOOK.get(emo, "a cute cartoon whale with a funny expression")
    who = persona_name or "小鲸鱼"
    p = ("%s, sticker style, thick clean outlines, flat pastel colors, "
         "centered, simple plain background, square composition" % look)
    if who and who not in p:
        p = p + ", character is %s" % who
    if extra:
        p = p + ", " + str(extra).strip()[:200]
    return p


def moment_image_prompt(text, persona_name=""):
    """把一条说说正文变成配图提示词（不要文字，只要氛围）。"""
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(body) > 160:
        body = body[:160]
    who = persona_name or "小鲸鱼"
    return ("a soft illustration that matches this diary-like mood: %s. "
            "cozy lighting, gentle colors, no people's faces in detail, "
            "no text, square composition, character is a cute little whale called %s"
            % (body, who))


def last_error():
    return _last_error






def svg_draw(prompt, size="1024x1024", model="", timeout=None):
    """v1.4.2：公开入口 —— 用模型写 SVG、本地渲染成 PNG。

    不依赖 /images/generations，也不要求模型支持 modalities：
    只要聊天接口能回文字，就能画。返回 {ok,path,msg,meta}。
    """
    global _last_error
    c = _cfg()
    p = _clean_prompt(prompt, c)
    sz = str(size or c.get("imagegen_size") or "1024x1024")
    tmo = int(timeout or c.get("imagegen_timeout") or 180)
    targets = _targets(c) or []
    if model:
        _hit = [x for x in targets if str(x.get("model") or "") == str(model)]
        if _hit:
            targets = _hit
        else:
            targets = [{"base": _base(c), "model": str(model),
                        "api_key": _key(c), "label": "指定的模型"}] + targets
    if not targets:
        _last_error = "没有可用的模型端点（先在设置里配好模型）"
        return {"ok": False, "path": "", "msg": _last_error, "meta": {"via": "svg"}}
    last = ""
    for tg in targets:
        r = _generate_one_svg(tg, p, sz, c, tmo)
        if r.get("ok"):
            try:
                _record_usage(str(tg.get("model") or ""), "SVG 生图")
            except Exception:
                pass
            return r
        last = r.get("msg") or ""
    _last_error = last or "SVG 生图失败"
    return {"ok": False, "path": "", "msg": _last_error, "meta": {"via": "svg"}}


def cleanup(keep_days=14):
    """删掉太老的生成图，返回删了几个。"""
    d = out_dir()
    if not os.path.isdir(d):
        return 0
    cutoff = time.time() - max(1, int(keep_days)) * 86400
    n = 0
    for fn in os.listdir(d):
        p = os.path.join(d, fn)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                n += 1
        except Exception:
            pass
    return n


def list_generated(limit=40):
    """返回生成图片的**路径列表**（按时间倒序）。

    注意：早期版本这里返回的是 dict，调用方却当路径用（`os.path.basename(p)`），
    导致画图页整块报 `TypeError: expected str, bytes or os.PathLike object, not dict`。
    现在固定返回路径；需要名字/大小/时间请用 list_generated_info()。
    """
    return [it["path"] for it in list_generated_info(limit=limit)]


def list_generated_info(limit=40):
    """返回 [{name, path, size, mtime}]，同样时间倒序。"""
    d = out_dir()
    if not os.path.isdir(d):
        return []
    files = []
    for fn in os.listdir(d):
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        if os.path.splitext(fn)[1].lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        try:
            st = os.stat(p)
        except Exception:
            continue
        files.append({"name": fn, "path": p, "size": st.st_size, "mtime": st.st_mtime})
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files[:limit]


def status():
    ok, msg = available()
    c = _cfg()
    key = str(c.get("imagegen_api_key") or "")
    masked = ""
    if key:
        masked = (key[:4] + "…" + key[-4:]) if len(key) > 10 else "已设置"
    n = len(list_generated(limit=999))
    ts = _targets(c)
    return {
        "ok": True,
        "enabled": bool(c.get("imagegen_enabled")),
        "available": ok,
        "msg": msg,
        "available_msg": msg,
        # 【v8】默认跟着「模型设置」走，下面这三个只是可选的强制覆盖
        "use_current": bool(c.get("imagegen_use_current", True)),
        "targets": [{"label": t["label"], "base": t["base"], "model": t["model"],
                     "provider": t["provider"], "has_key": bool(t.get("api_key"))}
                    for t in ts],
        "target_first": (ts[0]["label"] if ts else ""),
        "model": str(c.get("imagegen_model") or ""),
        "base": _base(c),
        "size": str(c.get("imagegen_size") or ""),
        "api_key": masked,
        "dir": out_dir(),
        "count": n,
        "generated_count": n,
        "sticker_gen_enabled": bool(c.get("sticker_gen_enabled")),
        "sticker_gen_chance": float(c.get("sticker_gen_chance") or 0),
    }


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(json.dumps(status(), ensure_ascii=False, indent=2))
    print("sticker prompt:", sticker_prompt(["开心"], "小鲸鱼"))
