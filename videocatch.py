# -*- coding: utf-8 -*-
"""videocatch.py —— 视频链接解析（V1.9.10）

给「用户丢一个 B 站/抖音/YouTube 链接」和「她自己主动分享视频」两条路用：

    fetch(url)                 -> 解析一个视频：标题 / 封面 / 简介 / 作者 / 时长
    cover_data_url(img, ...)   -> 把封面下载成 data URL，喂给视觉模型
    describe_for_prompt(info)  -> 拼成给模型的提示文案
    find_urls(text)            -> 从一句话里挑出视频链接
    hot_bilibili(limit)        -> B 站热门列表（她主动分享时用）

设计上的两条硬规矩：
  1. **绝不让抓取失败拖垮聊天**：所有函数最坏返回空结果，不抛异常；
  2. **不假装成功**：抓不到标题就说抓不到，不要把 URL 当标题。
"""
import base64
import json
import os
import re
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 平台识别：返回 (平台名, 视频 id 或 "")
_PATTERNS = [
    ("bilibili", re.compile(r"(?:bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)|b23\.tv/([0-9A-Za-z]+))", re.I)),
    ("douyin", re.compile(r"(?:douyin\.com/video/(\d+)|v\.douyin\.com/([0-9A-Za-z_-]+))", re.I)),
    ("youtube", re.compile(r"(?:youtube\.com/(?:watch\?v=|shorts/)([0-9A-Za-z_-]{6,})|youtu\.be/([0-9A-Za-z_-]{6,}))", re.I)),
]

_URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]]+", re.I)


# ---------------------------------------------------------------- 基础工具
def _get(url, timeout=8, referer="", raw=False):
    """GET 一次。raw=True 返回 bytes。失败抛异常（由上层兜）。"""
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if raw:
        return data
    for enc in ("utf-8", "gbk", "ignore"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return ""


def _meta(html):
    """从 HTML 里抠 og:title / og:image / og:description。"""
    out = {}

    def pick(pat):
        m = re.search(pat, html or "", re.I | re.S)
        return m.group(1).strip() if m else ""

    out["title"] = (pick(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)')
                    or pick(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:title["\']')
                    or pick(r"<title[^>]*>(.*?)</title>"))
    out["cover"] = (pick(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']*)')
                    or pick(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:image["\']'))
    out["desc"] = (pick(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)')
                   or pick(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)'))
    for k in list(out):
        v = out[k]
        v = re.sub(r"\s+", " ", v or "").strip()
        v = (v.replace("&amp;", "&").replace("&quot;", '"')
              .replace("&#39;", "'").replace("&lt;", "<").replace("&gt;", ">"))
        out[k] = v
    if out["title"].lower().endswith("_哔哩哔哩_bilibili"):
        out["title"] = out["title"][:-len("_哔哩哔哩_bilibili")].strip()
    return out


def detect(url):
    """判断是不是视频链接。返回 (平台, vid)；不是视频返回 ("", "")。"""
    u = str(url or "")
    for name, pat in _PATTERNS:
        m = pat.search(u)
        if m:
            vid = next((g for g in m.groups() if g), "")
            return name, vid
    return "", ""


def find_urls(text, limit=2):
    """从一段话里挑出视频链接（去重，最多 limit 条）。"""
    out = []
    for u in _URL_RE.findall(str(text or "")):
        u = u.rstrip(".,;!?)）]】")
        if detect(u)[0] and u not in out:
            out.append(u)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 各平台
def _code_ok(js):
    """B 站接口的 code 判定。

    坑：`int(js.get("code") or -1) != 0` 在 code=0 时会算成 int(-1)，
    因为 0 是 falsy —— 直接把「成功」判成失败，页面上就是「解析不出来」。
    """
    c = js.get("code")
    try:
        return int(c) == 0
    except Exception:
        return str(c) == "0"


def _bili_view(bvid):
    """B 站官方 view 接口（无需登录，比抠网页靠谱得多）。"""
    url = "https://api.bilibili.com/x/web-interface/view?bvid=%s" % bvid
    js = json.loads(_get(url, timeout=8))
    if not _code_ok(js):
        return {}
    d = js.get("data") or {}
    return {
        "title": str(d.get("title") or ""),
        "desc": str(d.get("desc") or ""),
        "cover": str(d.get("pic") or ""),
        "author": str((d.get("owner") or {}).get("name") or ""),
        "duration": int(d.get("duration") or 0),
        "plays": int((d.get("stat") or {}).get("view") or 0),
        "url": "https://www.bilibili.com/video/%s" % (d.get("bvid") or bvid),
    }


def _ytdlp(url, timeout=25):
    """有 yt-dlp 就顺手拿字幕/长简介；没装就跳过（绝不为它装依赖）。"""
    try:
        import yt_dlp  # noqa: F401
    except Exception:
        return {}
    try:
        from yt_dlp import YoutubeDL
    except Exception:
        return {}
    opt = {"quiet": True, "no_warnings": True, "skip_download": True,
           "socket_timeout": timeout, "noplaylist": True}
    try:
        with YoutubeDL(opt) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except Exception:
        return {}
    out = {}
    for k in ("title", "description", "uploader", "duration"):
        v = info.get(k)
        if v:
            out["title" if k == "title" else k] = v
    if info.get("thumbnail"):
        out["cover"] = info["thumbnail"]
    # 字幕：优先中文，最多取 20 行
    subs = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for src in (subs, auto):
        for key in list(src.keys()):
            if not key.lower().startswith(("zh", "ai-zh", "en")):
                continue
            ent = src.get(key) or []
            u = (ent[0] or {}).get("url") if ent else ""
            if not u:
                continue
            try:
                vtt = _get(u, timeout=8)
                lines = [re.sub(r"<[^>]+>", "", x).strip()
                         for x in vtt.splitlines()]
                lines = [x for x in lines
                         if x and not x.startswith(("WEBVTT", "NOTE", "Kind:", "Language:"))
                         and "-->" not in x and not re.match(r"^\d+$", x)]
                # 去掉重复的连续字幕行
                dedup, last = [], ""
                for x in lines:
                    if x != last:
                        dedup.append(x)
                        last = x
                if dedup:
                    out["subtitle"] = " ".join(dedup[:20])[:600]
                    break
            except Exception:
                continue
        if out.get("subtitle"):
            break
    return out


def fetch(url, timeout=10, with_subtitle=False):
    """解析一个视频链接。

    返回 {"ok", "platform", "url", "title", "cover", "desc", "author",
          "duration", "plays", "subtitle", "msg"}
    抓不到标题时 ok=False（不假装成功）。
    """
    t0 = time.time()
    url = str(url or "").strip()
    platform, vid = detect(url)
    out = {"ok": False, "platform": platform, "url": url, "title": "", "cover": "",
           "desc": "", "author": "", "duration": 0, "plays": 0, "subtitle": "",
           "msg": "", "ms": 0}

    # 短链先展开，拿到真实地址
    if platform and not re.search(r"/(video|watch|shorts)/|youtu\.be/", url, re.I):
        try:
            real = _get(url, timeout=timeout)  # 只为触发跳转
            del real
        except Exception:
            pass
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                url = r.geturl() or url
                out["url"] = url
        except Exception:
            pass
        platform2, vid2 = detect(url)
        if platform2:
            platform, vid = platform2, vid2 or vid
            out["platform"] = platform

    # 0) v1.9.10：视频平台先走 yt-dlp（最稳，官方接口变了也不受影响）
    #    只在这几个站试，别对普通网页乱跑（yt-dlp 会读几十秒）
    if platform in ("bilibili", "youtube", "douyin"):
        try:
            extra = _ytdlp(url)
        except Exception as e:
            extra = None
            out["msg"] = "yt-dlp 没读到：%s" % e
        if extra:
            out["title"] = str(extra.get("title") or "")[:200] or out["title"]
            out["cover"] = out["cover"] or (extra.get("cover") or "")
            desc = str(extra.get("description") or "")
            if desc and len(desc) > len(out["desc"]):
                out["desc"] = desc[:600]
            out["author"] = out["author"] or str(extra.get("uploader") or "")
            sub = str(extra.get("subtitle") or "")
            if sub:
                out["subtitle"] = sub[:1200]
            if out["title"]:
                out["ok"] = True
                out["via"] = (out.get("via") or "") + "ytdlp"

    # 1) B 站官方接口（yt-dlp 已经成功就不再打扰它 —— 上次这里无条件覆盖 via，
    #    结果明明走的是 yt-dlp，日志却写 bilibili_api，排查时被误导过一次）
    if not out["ok"] and platform == "bilibili" and vid.upper().startswith("BV"):
        try:
            info = _bili_view(vid)
            if info.get("title"):
                out.update({k: info.get(k, out.get(k)) for k in
                            ("title", "desc", "cover", "author", "duration", "plays", "url")})
                out["ok"] = True
                out["via"] = "bilibili_api"
        except Exception as e:
            out["msg"] = "B 站接口没读到：%s" % e

    # 2) 网页 og 标签兜底（抖音/YouTube/短链都走这条）
    if not out["ok"]:
        try:
            html = _get(url, timeout=timeout, referer=url)
            m = _meta(html)
            if m.get("title"):
                out["title"] = m["title"][:200]
                out["cover"] = m.get("cover", "")
                out["desc"] = m.get("desc", "")[:600]
                out["ok"] = True
                out["via"] = "og"
        except Exception as e:
            out["msg"] = (out["msg"] + "；" if out["msg"] else "") + "网页没读到：%s" % e

    # 3) yt-dlp（可选，能拿字幕）
    if with_subtitle and platform in ("bilibili", "youtube", "douyin"):
        extra = _ytdlp(url)
        if extra:
            for k in ("title", "cover", "subtitle"):
                if extra.get(k) and not out.get(k):
                    out[k] = extra[k]
            if extra.get("description") and len(str(extra["description"])) > len(out["desc"]):
                out["desc"] = str(extra["description"])[:600]
            if extra.get("uploader") and not out["author"]:
                out["author"] = str(extra["uploader"])
            if not out["ok"] and out["title"]:
                out["ok"] = True
            out["via"] = (out.get("via") or "") + "+ytdlp"

    out["ms"] = int((time.time() - t0) * 1000)
    if out["ok"]:
        out["msg"] = "视频解析成功（%s，%d 毫秒）" % (out.get("via") or "?", out["ms"])
    else:
        out["msg"] = out["msg"] or "没抓到视频标题"
        out["title"] = ""
    return out


# ---------------------------------------------------------------- 封面
def cover_data_url(img_url, max_bytes=1500000, timeout=10):
    """下载封面 → data URL（可直接喂视觉模型）。失败返回 ""。"""
    u = str(img_url or "").strip()
    if not u.startswith("http"):
        return ""
    try:
        data = _get(u, timeout=timeout, raw=True, referer="https://www.bilibili.com/")
    except Exception:
        return ""
    if not data or len(data) > max_bytes:
        return ""
    head = data[:12]
    if head.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif head.startswith(b"\x89PNG"):
        mime = "image/png"
    elif head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    elif head[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    else:
        return ""
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


# ---------------------------------------------------------------- 文案 / 列表
def describe_for_prompt(info, who="对方"):
    """把解析结果拼成给模型看的一段说明（她会据此"接着聊"）。"""
    if not info or not info.get("ok"):
        return ""
    bits = ["【%s分享了一个视频】" % who,
            "标题：%s" % (info.get("title") or "（没抓到）")]
    if info.get("author"):
        bits.append("UP 主：%s" % info["author"])
    if info.get("duration"):
        bits.append("时长：%s" % _fmt_dur(info["duration"]))
    if info.get("plays"):
        bits.append("播放：%s" % _fmt_num(info["plays"]))
    if info.get("desc"):
        bits.append("简介：%s" % str(info["desc"])[:300])
    if info.get("subtitle"):
        bits.append("字幕节选：%s" % str(info["subtitle"])[:300])
    bits.append("链接：%s" % (info.get("url") or ""))
    bits.append("（封面图已一并发给你，可以看着封面聊）" if info.get("cover") else "")
    return "\n".join(x for x in bits if x)


def _fmt_dur(sec):
    try:
        sec = int(sec)
    except Exception:
        return ""
    if sec <= 0:
        return ""
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


def _fmt_num(n):
    try:
        n = int(n)
    except Exception:
        return ""
    if n >= 100000000:
        return "%.1f 亿" % (n / 100000000.0)
    if n >= 10000:
        return "%.1f 万" % (n / 10000.0)
    return str(n)


def hot_bilibili(limit=20, timeout=10):
    """B 站热门（不需要登录）。失败返回 []。"""
    out = []
    for api in ("https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all",
                "https://api.bilibili.com/x/web-interface/popular?ps=%d&pn=1" % max(5, limit)):
        try:
            js = json.loads(_get(api, timeout=timeout))
            if not _code_ok(js):
                continue
            data = js.get("data") or {}
            lst = data.get("list") if isinstance(data, dict) else None
            if not lst and isinstance(data, dict):
                lst = (data.get("data") or {}).get("list") if isinstance(data.get("data"), dict) else None
            for it in (lst or [])[:limit]:
                t = str(it.get("title") or "").strip()
                bv = str(it.get("bvid") or "")
                if not t or not bv:
                    continue
                out.append({
                    "title": t,
                    "url": "https://www.bilibili.com/video/%s" % bv,
                    "cover": str(it.get("pic") or ""),
                    "author": str((it.get("owner") or {}).get("name") or ""),
                    "desc": str(it.get("desc") or "")[:300],
                    "duration": int(it.get("duration") or 0),
                    "plays": int((it.get("stat") or {}).get("view") or 0),
                    "ok": True,
                })
            if out:
                break
        except Exception:
            continue
    return out


if __name__ == "__main__":
    import sys

    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if not arg:
        print("用法: python videocatch.py <视频链接>")
        sys.exit(2)
    r = fetch(arg, with_subtitle=True)
    print(json.dumps({k: (v[:120] if isinstance(v, str) else v) for k, v in r.items()},
                     ensure_ascii=False, indent=2))
    if r.get("cover"):
        d = cover_data_url(r["cover"])
        print("cover data url 长度:", len(d))
    print("---- 提示文案 ----")
    print(describe_for_prompt(r, "用户"))
