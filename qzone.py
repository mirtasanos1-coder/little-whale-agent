# -*- coding: utf-8 -*-
"""QQ 空间（说说）—— 发说说、收发评论，以及对 NapCat 能力的探测

为什么需要"探测"
----------------
NapCat 各版本实现的 OneBot 扩展接口不一样。我实测过手头这个版本：

    send_qzone_msg        ✅ 有（content 必填，images 可选，返回 tid）
    delete_qzone_msg      ✅ 有（tid 必填）
    get_qzone_msg_list    ❌ 不支持的Api
    get_qzone_msg         ❌ 不支持的Api
    like_qzone            ❌ 不支持的Api
    send_qzone_comment    ❌ 不支持的Api

也就是说：**能发说说，但读不到评论、也回不了评论**。
所以这里不硬编码"支持/不支持"，而是启动时探一次，把结论缓存起来：

    caps() -> {"post": True, "delete": True, "read": False, "reply": False, ...}

读不到评论时自动退回「qzone_inbox/ 收件箱」模式：
用户（或另一个脚本、或手机截图后手动抄一条）往 qzone_inbox/ 里丢一个 json，
后台线程照样能"看到评论并回复"。这样功能是通的，只是采集方式不同。

探测手法：给一个不存在的参数发请求，看返回的 message。
    "不支持的Api xxx"        -> 该接口不存在
    "Schema compilation error" -> 接口存在，只是缺必填参数
"""
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

_lock = threading.RLock()
_cache = {}
_cache_ts = 0.0
_CACHE_TTL = 600  # 探测结论缓存 10 分钟

# v1.9.20：NapCat 的评论接口（send_qzone_comment / reply_qzone_comment）
# 在现网大多数版本里都不存在，调用一次就是一行「不支持的Api」。
# 用户反馈后台被这条错误刷屏、而且回复还会重复 —— 于是整条链路弃用：
# 评论/回评论一律走 QQ 空间的网页协议（qzone_interact.comment，带 @ 昵称格式）。
# 这里不删函数，只让它们变成"立刻返回、不发请求"，避免别处的老调用方崩掉。
COMMENT_VIA_NAPCAT = False
COMMENT_DEPRECATED_MSG = "NapCat 没有评论接口（V1.9.20 起评论统一走空间网页协议）"
# 最近一次网页协议发送成功后拿到的空间评论 id —— 调用方读它来给本地记录补 remote_id。
# 空字符串表示这次没拿到（失败、或旧接口），调用方按老逻辑处理即可。
LAST_COMMENT_ID = ""


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


# ---------------------------------------------------------------------------
# v1.9.9.1：认出「这是我自己」
# 她发的说说会出现在好友动态里，她的评论会出现在评论列表里 —— 不筛掉就会
# 自己给自己点赞、自己评论自己，下一轮又把这条评论当成新评论再回一遍。
# ---------------------------------------------------------------------------
_SELF_CACHE = {"at": 0.0, "uins": set(), "names": set()}


def self_uins(force=False):
    """返回 (自己的 QQ 号集合, 自己的昵称集合)。缓存 60 秒。"""
    now = time.time()
    if (not force) and _SELF_CACHE["at"] and (now - _SELF_CACHE["at"] < 60):
        return set(_SELF_CACHE["uins"]), set(_SELF_CACHE["names"])
    uins, names = set(), set()
    try:
        c = _cfg()
        for k in ("qq_uin", "self_uin", "bot_uin", "my_uin", "owner_uin", "uin"):
            v = str(c.get(k) or "").strip()
            if v.isdigit():
                uins.add(v)
        for k in ("bot_name", "self_name", "bot_nickname", "agent_name", "nickname"):
            v = str(c.get(k) or "").strip()
            if v:
                names.add(v)
    except Exception:
        pass
    try:
        import napcat_mgr
        li = napcat_mgr.login_info(timeout=2) or {}
        for k in ("user_id", "uin", "self_id", "qq"):
            v = str(li.get(k) or "").strip()
            if v.isdigit():
                uins.add(v)
    except Exception:
        pass
    _SELF_CACHE.update({"at": now, "uins": uins, "names": names})
    return uins, names


def is_self(uin="", name=""):
    """这条说说 / 这条评论是不是我自己发的。拿不到号时退回用昵称认。"""
    uid = str(uin or "").strip()
    nm = str(name or "").strip()
    if not uid and not nm:
        return False
    u, n = self_uins()
    if uid and uid in u:
        return True
    if (not uid) and nm and nm in n:
        return True
    return False


def _http(c):
    return str(c.get("napcat_http") or "http://127.0.0.1:3003").strip().rstrip("/")


def call(action, params=None, timeout=45):
    """裸调 NapCat 的一个 action，返回 dict（失败也返回 dict，不抛）。"""
    c = _cfg()
    url = _http(c) + "/" + str(action).lstrip("/")
    body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return json.loads(raw.decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:
            return {"status": "failed", "message": "HTTP %s" % e.code}
    except Exception as e:
        return {"status": "failed", "message": "连不上 NapCat：%s" % e}


def _msg_of(resp):
    if not isinstance(resp, dict):
        return ""
    return str(resp.get("message") or resp.get("wording") or "")


def _unsupported(resp):
    return "不支持的Api" in _msg_of(resp) or "unsupported" in _msg_of(resp).lower()


def _probe_action(action, need_param_hint=True):
    """探测一个 action 存不存在。见模块开头的说明。"""
    resp = call(action, {}, timeout=8)
    m = _msg_of(resp)
    if _unsupported(resp):
        return False, m
    if "Schema compilation error" in m or "required property" in m:
        return True, "接口存在（缺必填参数）"
    if str(resp.get("status")) == "ok":
        return True, "接口存在"
    # V1.9.9.3：以前这里无条件 return True —— 调用超时 / 返回空 dict 都会被当成
    # 「接口存在」，于是 read_comments 走了 NapCat 分支、真调的时候才报
    # 「不支持的Api get_qzone_msg_list」，收件箱兜底那条路永远走不到。
    if not m:
        return False, "探测没有答复（当作不支持）"
    return False, m


def clear_caps():
    """把能力探测缓存清掉（V1.9.9.3：实际调用发现不支持时要立刻失效）。"""
    global _cache, _cache_ts
    with _lock:
        _cache = None
        _cache_ts = 0.0


def caps(force=False):
    """探测 NapCat 的 QQ 空间能力。结果缓存 10 分钟。

    v1.9.20：comment（发评论/回评论）这一项**不再探测、直接判不可用**。
    实测多个 NapCat 版本（含 2.9.x）根本没有 send_qzone_comment / reply_qzone_comment，
    以前这里每 10 分钟探一次、每次回复再调一次，后台日志就被「不支持的Api」刷满。
    现在评论一律走空间网页协议（qzone_interact.comment），NapCat 只负责发说说/删除。
    """
    global _cache, _cache_ts
    with _lock:
        if not force and _cache and (time.time() - _cache_ts) < _CACHE_TTL:
            return dict(_cache)
    out = {}
    for key, action in (("post", "send_qzone_msg"),
                        ("delete", "delete_qzone_msg"),
                        ("read", "get_qzone_msg_list"),
                        ("like", "like_qzone")):
        ok, detail = _probe_action(action)
        out[key] = ok
        out[key + "_detail"] = detail
    out["reply"] = False
    out["reply_action"] = ""
    out["reply_detail"] = COMMENT_DEPRECATED_MSG
    out["comment_via"] = "qzone_web"
    out["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    out["ok"] = True
    with _lock:
        _cache = dict(out)
        _cache_ts = time.time()
    return out


# ---------------------------------------------------------------------------
# 浏览好友动态 / 点赞 / 发评论（v7）
# 现实情况：绝大多数 NapCat 版本没有"好友动态列表"接口，这里一律先探测，
# 不支持就返回 ok=False，调用方改走 feed_inbox/ 收件箱，绝不假装成功。
# ---------------------------------------------------------------------------
def read_feed(limit=20, timeout=30):
    """读好友动态列表（也读自己空间的留言）。返回 (ok, items, msg)。"""
    c = caps()
    if not c.get("read"):
        return False, [], "这个 NapCat 不支持读说说/动态（不支持的Api get_qzone_msg_list）"
    for action in ("get_qzone_msg_list", "get_qzone_feed"):
        resp = call(action, {"pos": 0, "num": int(limit)}, timeout=timeout)
        if _unsupported(resp):
            continue
        if str(resp.get("status")) != "ok":
            return False, [], _msg_of(resp) or "读取失败"
        data = resp.get("data")
        rows = []
        if isinstance(data, dict):
            rows = data.get("msglist") or data.get("list") or data.get("data") or []
        elif isinstance(data, list):
            rows = data
        items = []
        _self_n = 0
        for r in rows:
            if not isinstance(r, dict):
                continue
            content = str(r.get("content") or r.get("text") or "").strip()
            if not content:
                continue
            if is_self(r.get("uin") or r.get("user_id") or "",
                       r.get("name") or r.get("nickname") or ""):
                _self_n += 1
                continue
            items.append({
                "post_key": str(r.get("tid") or r.get("id") or content[:24]),
                "author_id": str(r.get("uin") or r.get("user_id") or ""),
                "author_name": str(r.get("name") or r.get("nickname") or "一个好友"),
                "content": content,
                "images": r.get("images") or r.get("pic") or "",
                "source": "napcat",
            })
        _tail = f"（跳过自己发的 %d 条）" % _self_n if _self_n else ""
        return True, items[:limit], f"拿到 {len(items)} 条" + _tail
    # V1.9.9.3：探测和实际调用经常对不上（不同 NapCat 版本差异很大），
    # 这里把缓存清掉，让下一轮重新探，并给调用方一个明确的「没有接口」而不是异常。
    clear_caps()
    return False, [], "这个 NapCat 没有可用的动态列表接口（改走收件箱）"


def like(tid, timeout=30):
    """给一条说说点赞。"""
    if not caps().get("like"):
        return False, "这个 NapCat 不支持点赞（不支持的Api like_qzone）"
    resp = call("like_qzone", {"tid": str(tid), "type": 0}, timeout=timeout)
    if str(resp.get("status")) == "ok":
        return True, "已点赞"
    return False, _msg_of(resp) or "点赞失败"


def post_comment(tid, text, timeout=30):
    """在别人的动态下留一条评论（v1.9.20：改走空间网页协议）。"""
    return comment_web(tid, text)


def comment_web(tid, text, reply_comment_id="", timeout=30):
    """用 QQ 空间网页协议发/回评论 —— 现在唯一可用的评论通路。

    返回 (ok, msg)。reply_comment_id 非空表示"回复某条评论"，
    这样网页端会把这条挂在对方那条下面，而不是孤零零一句。
    """
    import qzone_interact as qi
    global LAST_COMMENT_ID
    LAST_COMMENT_ID = ""
    tid = str(tid or "").strip()
    if not tid:
        return False, "没有说说 id，发不了评论"
    body = str(text or "").strip()
    if not body:
        return False, "评论内容为空"
    try:
        r = qi.comment(tid, "", body, reply_comment_id=str(reply_comment_id or ""))
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    if isinstance(r, dict) and r.get("ok"):
        LAST_COMMENT_ID = str(r.get("comment_id") or "")
        return True, "评论已发出（网页协议）"
    msg = ""
    if isinstance(r, dict):
        msg = str(r.get("msg") or r.get("message") or "")
    return False, msg or "网页协议发评论失败"



# ---------------------------------------------------------------------------
# 发说说
# ---------------------------------------------------------------------------
def post(content, images=None, timeout=90):
    """发一条说说。返回 {ok, tid, msg}。

    images 支持三种写法（实测 NapCat 都收）：
        ["file:///D:/.../a.png"]  本地路径（推荐，省内存）
        ["D:/.../a.png"]          裸路径
        ["base64://iVBOR..."]     base64
    """
    text = str(content or "").strip()
    if not text and not images:
        return {"ok": False, "tid": "", "msg": "内容不能为空"}
    payload = {"content": text}
    if images:
        payload["images"] = list(images)[:9]
    resp = call("send_qzone_msg", payload, timeout=timeout)
    data = resp.get("data") or {}
    tid = ""
    if isinstance(data, dict):
        tid = str(data.get("tid") or "")
    ok = str(resp.get("status")) == "ok" and bool(tid)
    return {
        "ok": ok,
        "tid": tid,
        "msg": ("已发说说（tid=%s）" % tid) if ok else
               ("发说说失败：%s" % (_msg_of(resp) or resp)),
    }


def delete(tid, timeout=30):
    tid = str(tid or "").strip()
    if not tid:
        return False, "没给 tid"
    resp = call("delete_qzone_msg", {"tid": tid}, timeout=timeout)
    ok = str(resp.get("status")) == "ok"
    return ok, ("已删除" if ok else ("删除失败：%s" % (_msg_of(resp) or resp)))


# ---------------------------------------------------------------------------
# 评论：读 / 回
# ---------------------------------------------------------------------------
def read_comments(tid="", timeout=30):
    """读评论。能用接口就调接口，否则读 qzone_inbox/ 收件箱。

    返回 (ok, list, msg)。list 每项：
        {moment_tid, moment_text, commenter_id, commenter_name, content, source}
    """
    c = _cfg()
    cap = caps()
    if cap.get("read"):
        params = {"pos": 0, "count": 20}
        if tid:
            params["tid"] = tid
        resp = call("get_qzone_msg_list", params, timeout=timeout)
        if str(resp.get("status")) == "ok":
            items = resp.get("data") or []
            out = []
            _cm_self = 0
            for it in items if isinstance(items, list) else []:
                if not isinstance(it, dict):
                    continue
                for cm in (it.get("comments") or it.get("commentList") or []):
                    if not isinstance(cm, dict):
                        continue
                    if is_self(cm.get("uin") or cm.get("user_id") or "",
                               cm.get("name") or cm.get("nickname") or ""):
                        _cm_self += 1
                        continue
                    out.append({
                        "moment_tid": str(it.get("tid") or tid or ""),
                        "moment_text": str(it.get("content") or "")[:1000],
                        "commenter_id": str(cm.get("uin") or cm.get("user_id") or ""),
                        "commenter_name": str(cm.get("name") or cm.get("nickname") or ""),
                        "content": str(cm.get("content") or "").strip(),
                        "source": "napcat",
                        "remote_id": str(cm.get("commentid") or cm.get("comment_id") or ""),
                    })
            _tail = ("（跳过自己的 %d 条）" % _cm_self) if _cm_self else ""
            return True, out, "从 NapCat 读到 %d 条评论" % len(out) + _tail
        _m = _msg_of(resp) or str(resp)
        if _unsupported(resp):
            # V1.9.9.3：这个 NapCat 根本没有读评论的接口 —— 以前直接返回失败，
            # 表现为「现在又读不了评论了」。改成先清缓存，再走收件箱。
            clear_caps()
            ok2, items2, msg2 = drain_inbox()
            if ok2 and items2:
                return True, items2, "这个 NapCat 没有读评论的接口，改用收件箱" + (
                    ("：" + msg2) if msg2 else "")
            return True, [], "这个 NapCat 没有读评论的接口，收件箱里暂时没有新评论"
        return False, [], "读评论失败：%s" % _m
    # 收件箱模式
    ok, items, msg = drain_inbox()
    return ok, items, msg


def reply_comment(tid, comment_id, text, timeout=30):
    """在评论区回一句（v1.9.20：改走空间网页协议）。

    这里的 comment_id 必须是**空间那边的评论 id**（网页协议里的 commentid），
    不是我们数据库的自增 id —— 以前的实现把库内 id 传给 NapCat，
    就算接口存在也挂不到对方那条评论下面。
    """
    return comment_web(tid, text, reply_comment_id=comment_id)


# ---------------------------------------------------------------------------
# 收件箱（读不到评论时的兜底采集方式）
# ---------------------------------------------------------------------------
def inbox_dir():
    c = _cfg()
    name = str(c.get("moment_comment_inbox") or "qzone_inbox").strip() or "qzone_inbox"
    d = name if os.path.isabs(name) else os.path.join(DATA_DIR, name)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


_INBOX_README = """QQ 空间评论收件箱
==================

为什么有这个文件夹
------------------
NapCat 有些版本**没有**读说说评论的接口（get_qzone_msg_list / get_qzone_msg），
本软件拿不到评论，就没法"看到你在说说下面说了什么"。
这个文件夹是兜底采集通道：你把评论按下面的格式写成 json 丢进来，
后台线程会当成真评论处理（生成回复、发出去，普通模式下回复走评论或私聊）。

怎么用
------
1. 在本文件夹里新建一个 .json 文件，比如 001.json
2. 内容写（单个对象或数组都行）：

{
  "tid": "9e67dd7f6d9cd96a20d60a00",
  "moment": "今天风好大，人也跟着清醒了",
  "user_id": "3280735835",
  "name": "安瑆.雨",
  "content": "你在干啥啊"
}

   数组写法：
[
  {"user_id": "3280735835", "name": "安瑆.雨", "content": "你在干啥啊"},
  {"user_id": "2453335323", "name": "喵星人", "content": "哈哈这个表情绝了"}
]

3. 保存。后台线程每 N 分钟扫一次（设置 → 说说 里能看间隔）。
4. 处理过的文件会被移动到 processed/ 子目录，不会重复处理。

字段说明
--------
  tid      可选，说说 id（要用评论区回复时最好带上）
  moment   可选，说说原文（模型回复时会参考）
  user_id  必填，评论者 QQ 号（用来私聊回复、写进记忆）
  name     可选，评论者昵称
  content  必填，评论内容

注意
----
* 只在「设置 → 说说评论」打开时才会处理。
* 回复方式见 moment_comment_deliver：comment = 评论区回，private = 私聊对方。
  如果 NapCat 不支持发评论，comment 模式会自动改成私聊。
"""


def ensure_inbox():
    d = inbox_dir()
    readme = os.path.join(d, "怎么用.txt")
    if not os.path.exists(readme):
        try:
            with open(readme, "w", encoding="utf-8") as f:
                f.write(_INBOX_README)
        except Exception:
            pass
    return d


def _norm_comment(it, fallback_tid=""):
    if not isinstance(it, dict):
        return None
    content = str(it.get("content") or it.get("text") or "").strip()
    if not content:
        return None
    if is_self(it.get("user_id") or it.get("qq") or it.get("uin") or "",
               it.get("name") or it.get("nickname") or ""):
        return None
    return {
        "moment_tid": str(it.get("tid") or it.get("moment_tid") or fallback_tid or ""),
        "moment_text": str(it.get("moment") or it.get("moment_text") or "")[:1000],
        "commenter_id": str(it.get("user_id") or it.get("qq") or it.get("uin") or ""),
        "commenter_name": str(it.get("name") or it.get("nickname") or ""),
        "content": content[:800],
        "source": "inbox",
    }


def drain_inbox(move=True):
    """把收件箱里的 json 读出来并（默认）移到 processed/。"""
    d = ensure_inbox()
    out = []
    files = []
    try:
        files = [f for f in sorted(os.listdir(d)) if f.lower().endswith(".json")]
    except Exception:
        files = []
    if not files:
        return True, [], "收件箱是空的"
    proc = os.path.join(d, "processed")
    try:
        os.makedirs(proc, exist_ok=True)
    except Exception:
        pass
    for fn in files:
        path = os.path.join(d, fn)
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            c = _norm_comment(it)
            if c:
                out.append(c)
        if move:
            try:
                stamp = time.strftime("%H%M%S")
                shutil.move(path, os.path.join(proc, stamp + "_" + fn))
            except Exception:
                try:
                    os.remove(path)
                except Exception:
                    pass
    return True, out, "从收件箱读到 %d 条评论" % len(out)


def inbox_count():
    d = inbox_dir()
    try:
        return len([f for f in os.listdir(d) if f.lower().endswith(".json")])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 图片工具
# ---------------------------------------------------------------------------
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def to_image_uri(path, prefer_file=True):
    """本地图片 -> NapCat 认的写法。

    v1.1.6 修：以前默认「小图塞 base64://」，但 NapCat 对 base64:// 的支持看版本，
    有些版本会直接回「base64 报错 / 图片解析失败」；而控制台和 NapCat 本来就在同一台机器上，
    `file:///绝对路径` 又快又稳。现在**默认一律 file:///**，只有明确 prefer_file=False
    且文件 <=1MB 时才用 base64（老的调用方不受影响）。
    """
    try:
        p = os.path.abspath(path)
        if not os.path.isfile(p):
            return ""
        size = os.path.getsize(p)
        if size > 20 * 1024 * 1024:
            return ""            # 超过 20MB 哪个通道都别发，交上层说人话
        if not prefer_file and size <= 1 * 1024 * 1024:
            import base64
            with open(p, "rb") as f:
                return "base64://" + base64.b64encode(f.read()).decode("ascii")
        return "file:///" + p.replace("\\", "/")
    except Exception:
        return ""


def list_images(dir_path):
    """列出一个目录里的图片（按文件名排序）。"""
    out = []
    if not dir_path or not os.path.isdir(dir_path):
        return out
    for fn in sorted(os.listdir(dir_path)):
        p = os.path.join(dir_path, fn)
        if os.path.isfile(p) and os.path.splitext(fn)[1].lower() in _IMG_EXTS:
            out.append(p)
    return out


def image_dir(kind="moment"):
    """predefined 图片目录：moment_images/ 或 generated/。"""
    c = _cfg()
    if kind == "generated":
        name = str(c.get("imagegen_dir") or "generated")
    else:
        name = str(c.get("moment_image_dir") or "moment_images")
    d = name if os.path.isabs(name) else os.path.join(DATA_DIR, name)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def status():
    """给界面看的综合状态。"""
    c = _cfg()
    cap = caps()
    return {
        "ok": True,
        "enabled": bool(c.get("moment_enabled")),
        "decide": str(c.get("moment_decide") or "model"),
        "image_enabled": bool(c.get("moment_image_enabled")),
        "comment_enabled": bool(c.get("moment_comment_enabled")),
        "comment_deliver": str(c.get("moment_comment_deliver") or "comment"),
        "caps": {k: v for k, v in cap.items() if not k.endswith("_detail")},
        "caps_detail": {k: v for k, v in cap.items() if k.endswith("_detail")},
        "inbox_dir": inbox_dir(),
        "inbox_count": inbox_count(),
        "image_dir": image_dir("moment"),
        "image_count": len(list_images(image_dir("moment"))),
    }


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(json.dumps(status(), ensure_ascii=False, indent=2))
