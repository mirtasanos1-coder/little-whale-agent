# -*- coding: utf-8 -*-
"""QQ 空间动态互动 —— 读动态 / 点赞 / 评论 / 回复

【为什么不用 NapCat 的接口】
    NapCat 4.18.38 只实现了两个空间接口：
        send_qzone_msg    发说说（可用）
        delete_qzone_msg  删说说（可用）
    其余 15 个（get_qzone_msg_list / like_qzone_msg / send_qzone_comment …）
    全部返回「不支持的Api」。所以点赞、评论这些**没法走 OneBot 接口**。

【所以这里的做法】
    从 NapCat 拿 QQ 登录态（get_cookies?domain=qzone.qq.com，里面有 skey / p_skey），
    自己按 QQ 空间的网页协议发请求。这条链路实测可用：
        读动态  emotion_cgi_msglist_v6      ✅
        点赞/取消 internal_dolike_app       ✅
        发评论  emotion_cgi_re_feeds        ✅
        删评论  emotion_cgi_del_comment_v6  ✅（用于清理测试评论）

【风险与克制】
    · 这是网页协议，QQ 随时可能改；所有函数都不抛异常，失败返回 ok=False。
    · 频率限制很重要：这里内置 sleep + 每秒最多 1 个请求，避免被判定为刷屏。
    · 评论内容会先过 security.scan()，命中提示词注入/SQL 的直接拒绝。
    · 默认「只看不互动」，要互动必须在设置里打开（feed_decide != readonly）。
"""
import json
import random
import re
import threading
import time
import urllib.parse
import urllib.request

import config as cfg_mod

_lock = threading.RLock()
_last_call = {"ts": 0.0}
_sess_cache = {"ts": 0.0, "data": None}
CAP_CACHE = {"ts": 0.0, "data": None}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# ==================== 底层：NapCat + 空间请求 ====================

def _nap(ep, data=None, timeout=15):
    """调一次 NapCat HTTP 接口"""
    cfg = cfg_mod.load()
    base = str(cfg.get("napcat_http") or "http://127.0.0.1:3003").rstrip("/")
    if not base.startswith("http"):
        base = "http://" + base
    body = json.dumps(data or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + "/" + ep, data=body,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def gtk(skey):
    """QQ 空间的 g_tk：skey 的经典哈希"""
    h = 5381
    for ch in str(skey or ""):
        h += (h << 5) + ord(ch)
    return str(h & 0x7FFFFFFF)


def _pick(cookie, name):
    m = re.search(r"(?:^|;\s*)" + re.escape(name) + r"=([^;]+)", cookie or "")
    return m.group(1) if m else ""


def session(force=False):
    """拿一次登录态：{uin, name, skey, p_skey, gtk, cookie}

    缓存 5 分钟（cookie 变了会自动重抓），失败返回 {"ok": False, "msg": ...}
    """
    now = time.time()
    with _lock:
        if not force and _sess_cache["data"] and now - _sess_cache["ts"] < 300:
            return _sess_cache["data"]
    try:
        ck = _nap("get_cookies", {"domain": "qzone.qq.com"})
        cookie = str(((ck.get("data") or {}).get("cookies")) or "")
        if not cookie:
            return {"ok": False, "msg": f"NapCat 没返回 cookie：{ck.get('message') or ck}"}
        info = _nap("get_login_info")
        uin = str(((info.get("data") or {}).get("user_id")) or "")
        name = str(((info.get("data") or {}).get("nickname")) or "")
        skey = _pick(cookie, "skey")
        pskey = _pick(cookie, "p_skey")
        out = {"ok": True, "uin": uin, "name": name, "cookie": cookie,
               "skey": skey, "p_skey": pskey, "gtk": gtk(skey or pskey)}
        with _lock:
            _sess_cache.update(ts=now, data=out)
        return out
    except Exception as e:
        return {"ok": False, "msg": f"取登录态失败：{type(e).__name__}: {e}"}


def _throttle(min_gap=0.8):
    """全局限速：两次请求之间至少隔 min_gap 秒（防被风控）"""
    with _lock:
        gap = time.time() - _last_call["ts"]
        if gap < min_gap:
            time.sleep(min_gap - gap + random.uniform(0, 0.25))
        _last_call["ts"] = time.time()


def _strip_jsonp(text):
    """空间接口的返回有四种形态，全都要认：

        1) 纯 JSON                     {"code":0,...}
        2) JSONP 包装                  _Callback({"code":0,...})
        3) 伪装成 HTML 的 JSONP        <script>frameElement.callback({"ret":0,"msg":"succ"});</script>
        4) 空 body                     （请求太快被限流，交给上层重试）
    """
    s = (text or "").strip()
    if not s:
        return None
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except Exception:
            pass
    # 2/3：从整段文本里抠出第一个 {...}（或 [...]）
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = s.find(opener), s.rfind(closer)
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    return None


def _http(url, data=None, sess=None, timeout=25, retries=2, min_gap=0.8):
    """发一个空间请求，自动带 cookie/referer、去 JSONP、失败重试。"""
    sess = sess or session()
    if not sess.get("ok"):
        return {"ok": False, "msg": sess.get("msg") or "没有登录态"}
    uin = sess["uin"]
    headers = {
        "Cookie": sess["cookie"],
        "Referer": f"https://user.qzone.qq.com/{uin}",
        "User-Agent": UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["Origin"] = "https://user.qzone.qq.com"
    last = ""
    for i in range(max(0, retries) + 1):
        _throttle(min_gap)
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "ignore")
            js = _strip_jsonp(raw)
            if js is not None:
                return {"ok": True, "data": js, "raw": raw[:4000]}
            last = f"返回无法解析（{len(raw)} 字节）：{raw[:120]!r}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.2 + i * 1.5)
    return {"ok": False, "msg": last}


# ==================== 读动态 ====================

def _ok_of(payload):
    """判定空间写操作是否成功。

    不同接口的成功标记不一样，实测见过这几种：
        {"code":0,"message":"succ"}
        {"ret":0,"msg":"succ"}
        {"data":{"code":0}}
    所以三个都认。返回 (ok, code, msg)
    """
    if not isinstance(payload, dict):
        return False, None, ""
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    code = body.get("code", payload.get("code"))
    ret = body.get("ret", payload.get("ret"))
    msg = str(body.get("message") or body.get("msg")
              or payload.get("message") or payload.get("msg") or "")
    ok = (str(code) == "0") or (str(ret) == "0") or ("succ" in msg.lower())
    return ok, (code if code is not None else ret), msg


def _to_ts(v, dflt=0):
    """把各种写法的时间换成秒级时间戳（v1.8.11）。

    QQ 空间几套接口对同一个字段的写法不统一：有纯数字时间戳、有数字字符串、
    也有 "2026-10-31 19:06:54" / "10月31日 19:06" 这种给人看的字符串。
    老代码一律 `int(x)`，碰上可读字符串就抛
    ValueError: invalid literal for int() with base 10
    —— 一条评论就能把整次「读评论」打断（表现就是「读评论一直失败」）。
    这里一律容错，认不出来就返回默认值，绝不抛。
    """
    if v is None or v == "":
        return dflt
    if isinstance(v, bool):
        return dflt
    if isinstance(v, (int, float)):
        return int(v)
    t = str(v).strip()
    if not t:
        return dflt
    if t.isdigit():
        try:
            return int(t)
        except Exception:
            return dflt
    # 带毫秒的数字字符串
    m = re.fullmatch(r"\d{9,13}(?:\.\d+)?", t)
    if m:
        try:
            return int(float(t))
        except Exception:
            return dflt
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(t, fmt)))
        except Exception:
            pass
    now = time.time()
    m = re.match(r"^\s*(\d+)\s*(秒|分钟|分|小时|天|周)前", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        step = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600,
                "天": 86400, "周": 604800}.get(unit, 60)
        return int(now - n * step)
    m = re.match(r"^\s*(\d{1,2})月(\d{1,2})日\s*(\d{1,2})?[:：]?(\d{2})?", t)
    if m:
        try:
            lt = time.localtime(now)
            hh = int(m.group(3) or 0)
            mm = int(m.group(4) or 0)
            return int(time.mktime((lt.tm_year, int(m.group(1)), int(m.group(2)),
                                    hh, mm, 0, 0, 0, -1)))
        except Exception:
            return dflt
    return dflt


def _is_mine(uin="", sess=None):
    """v1.9.9.1：这条动态/评论是不是我自己发的（网页协议这条路也要拦）。"""
    me = str((sess or {}).get("uin") or "")
    if not me:
        try:
            me = str((session() or {}).get("uin") or "")
        except Exception:
            me = ""
    uid = str(uin or "").strip()
    return bool(me) and bool(uid) and (uid == me)


def _norm_feed(it):
    """把空间返回的一条动态整理成统一结构"""
    conlist = it.get("conlist") or []
    text = ""
    if isinstance(conlist, list) and conlist:
        text = " ".join(str(c.get("con") or "") for c in conlist
                        if isinstance(c, dict))
    if not text:
        text = str(it.get("content") or "")
    imgs = []
    for c in (conlist if isinstance(conlist, list) else []):
        if isinstance(c, dict) and c.get("type") == 1:
            for pic in (c.get("conlist") or []):
                u = pic.get("url1") or pic.get("url2") or pic.get("url")
                if u:
                    imgs.append(str(u))
    return {
        "tid": str(it.get("tid") or ""),
        "uin": str(it.get("uin") or ""),
        "name": str(it.get("name") or ""),
        "content": re.sub(r"\s+", " ", text).strip(),
        "created_time": _to_ts(it.get("created_time")),
        "time_text": time.strftime("%Y-%m-%d %H:%M",
                                   time.localtime(_to_ts(it.get("created_time")))),
        "cmtnum": _to_ts(it.get("cmtnum")),
        "fwdnum": _to_ts(it.get("fwdnum")),
        "like_count": len(((it.get("likes") or {}).get("likes") or [])
                          if isinstance(it.get("likes"), dict) else []),
        "liked": bool(((it.get("likes") or {}).get("self_liked"))
                      if isinstance(it.get("likes"), dict) else False),
        "images": imgs[:9],
        "secret": _to_ts(it.get("secret")),
    }


def list_feeds(uin="", num=20, pos=0, sess=None):
    """读某个人的动态列表（uin 空 = 自己）。

    返回 {"ok":True,"feeds":[...],"total":n} 或 {"ok":False,"msg":..}
    """
    sess = sess or session()
    if not sess.get("ok"):
        return {"ok": False, "msg": sess.get("msg") or "没有登录态"}
    target = str(uin or sess["uin"])
    url = ("https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/"
           "emotion_cgi_msglist_v6?uin={uin}&ftype=0&sort=0&pos={pos}&num={num}"
           "&replynum=6&g_tk={gtk}&code_version=1&format=json"
           "&need_private_comment=1").format(
        uin=target, pos=int(pos), num=max(1, min(int(num or 20), 40)),
        gtk=sess["gtk"])
    r = _http(url, None, sess)
    if not r.get("ok"):
        return {"ok": False, "msg": r.get("msg") or "读动态失败"}
    d = r["data"] or {}
    code = d.get("code")
    msgs = d.get("msglist") or []
    if code not in (0, None) and not msgs:
        return {"ok": False, "msg": f"空间返回 code={code} message={d.get('message')}"}
    return {"ok": True, "feeds": [_norm_feed(x) for x in msgs if isinstance(x, dict)],
            "total": int(d.get("total") or len(msgs)), "uin": target,
            "self": target == str(sess["uin"])}


def friends(limit=200):
    """好友列表（读好友动态要挨个查，所以按账号取）"""
    try:
        r = _nap("get_friend_list")
        data = r.get("data") or []
        out = []
        for f in data:
            if not isinstance(f, dict):
                continue
            uid = str(f.get("user_id") or "")
            if uid:
                out.append({"user_id": uid,
                            "nickname": str(f.get("nickname") or ""),
                            "remark": str(f.get("remark") or "")})
            if len(out) >= limit:
                break
        return {"ok": True, "friends": out}
    except Exception as e:
        return {"ok": False, "msg": f"{type(e).__name__}: {e}", "friends": []}


def read_friend_feeds(max_friends=6, num=6, sess=None):
    """轮询读好友动态（每轮只查几个好友，避免请求过多被风控）"""
    sess = sess or session()
    if not sess.get("ok"):
        return {"ok": False, "msg": sess.get("msg") or "没有登录态"}
    fr = friends()
    all_feeds = []
    _mine = 0
    for f in fr.get("friends", [])[:max_friends]:
        r = list_feeds(f["user_id"], num=num, sess=sess)
        if r.get("ok"):
            for it in r["feeds"]:
                if _is_mine(it.get("uin") or f.get("user_id"), sess):
                    _mine += 1
                    continue
                it["author_id"] = f["user_id"]
                it["author_name"] = it["name"] or f.get("remark") or f.get("nickname") or ""
                all_feeds.append(it)
    return {"ok": True, "feeds": all_feeds,
            "skipped_self": _mine,
            "checked_friends": min(max_friends, len(fr.get("friends", [])))}


# ==================== 点赞 ====================

def _curkey(unikey, sess):
    """点赞接口需要 curkey：先问一次 get_like_list_app"""
    url = ("https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/"
           "get_like_list_app?unikey={u}&fupdate=1&g_tk={g}&format=json").format(
        u=urllib.parse.quote(unikey, safe=""), g=sess["gtk"])
    r = _http(url, None, sess)
    if not r.get("ok"):
        return ""
    d = r["data"] or {}
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    return str((data or {}).get("curkey") or "")


def like(tid, uin="", unlike=False, sess=None, _retried=False):
    """给一条动态点赞（unlike=True 取消）。

    返回 {"ok":True,"msg":"已点赞"} / {"ok":False,"msg":...}
    """
    sess = sess or session()
    if not sess.get("ok"):
        return {"ok": False, "msg": sess.get("msg") or "没有登录态"}
    tid = str(tid or "").strip()
    if not tid:
        return {"ok": False, "msg": "缺少 tid"}
    owner = str(uin or sess["uin"])
    unikey = f"http://user.qzone.qq.com/{owner}/mood/{tid}"
    curkey = _curkey(unikey, sess)
    url = ("https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/"
           "internal_dolike_app?g_tk={g}").format(g=sess["gtk"])
    form = {
        "qzonetoken": "", "g_tk": sess["gtk"], "unikey": unikey,
        "curkey": curkey, "appid": "311", "typeid": "0", "fid": tid,
        "from": "1", "opr_type": "unlike" if unlike else "like",
        "format": "json",
    }
    r = _http(url, form, sess)
    if not r.get("ok"):
        return {"ok": False, "msg": r.get("msg") or "点赞请求失败"}
    d = r["data"] or {}
    ok, code, msg = _ok_of(d)
    if not ok and not _retried:
        # 偶尔第一次会因为 curkey 为空被拒，重试一次基本就过
        return like(tid, uin, unlike, sess, _retried=True)
    return {"ok": ok, "msg": ("已取消赞" if unlike else "已点赞") if ok
            else f"未成功（code={code} {msg}）", "raw": r.get("raw", "")[:300]}


# ==================== 评论 / 回复 ====================

def _find_comment_id(obj, depth=0):
    """v1.9.22：从回包里挖出刚发的评论 id（各版本字段名/层级都不一样）。"""
    if depth > 4:
        return ""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in ("commentid", "comment_id") and isinstance(v, (str, int)):
                if str(v).strip():
                    return str(v)
        for v in obj.values():
            r = _find_comment_id(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            r = _find_comment_id(v, depth + 1)
            if r:
                return r
    return ""


def comment(tid, uin="", text="", sess=None, reply_comment_id=""):
    """在某条动态下评论（reply_comment_id 非空时是回复别人的评论）。"""
    import security
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "msg": "评论内容为空"}
    if len(text) > 300:
        text = text[:300]
    scan = security.scan(text)
    if scan["risk"] >= 2:
        return {"ok": False, "msg": "评论内容命中安全拦截（SQL/命令注入）"}
    sess = sess or session()
    if not sess.get("ok"):
        return {"ok": False, "msg": sess.get("msg") or "没有登录态"}
    tid = str(tid or "").strip()
    if not tid:
        return {"ok": False, "msg": "缺少 tid"}
    owner = str(uin or sess["uin"])
    url = ("https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/"
           "emotion_cgi_re_feeds?g_tk={g}").format(g=sess["gtk"])
    form = {
        "topicId": f"{owner}_{tid}",
        "feedsType": "100",
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "plat": "qzone",
        "source": "ic",
        "hostUin": owner,
        "isSignIn": "",
        "appid": "311",
        "content": text,
        "format": "json",
        "qzonetoken": "",
    }
    if reply_comment_id:
        form["commentId"] = str(reply_comment_id)
    r = _http(url, form, sess)
    if not r.get("ok"):
        return {"ok": False, "msg": r.get("msg") or "评论请求失败"}
    d = r["data"] or {}
    ok, code, msg = _ok_of(d)
    body = d.get("data") if isinstance(d.get("data"), dict) else d
    cid = str(body.get("commentId") or body.get("commentid") or "")
    if ok and not cid:
        cid = _find_comment_id(d)
    # v1.9.22：多数版本回包只有 code=0，一个字都不提评论 id。没有 id 就没法认
    # 「这条是我刚发的」—— 回声判定、跨循环去重全靠它。发完回读一次评论列表，
    # 按内容找回自己那条（取最后一条同文的）。一次额外的读，只在拿不到 id 时做。
    if ok and not cid:
        try:
            cl = comment_list(tid, uin, sess, include_mine=True) or {}
            cand = [x for x in (cl.get("comments") or [])
                    if str(x.get("content") or "").strip() == text]
            if cand:
                cid = str(cand[-1].get("comment_id") or "")
        except Exception:
            pass
    return {"ok": ok, "msg": "评论成功" if ok else f"评论失败（code={code} {msg}）",
            "comment_id": cid, "raw": r.get("raw", "")[:300]}


def delete_comment(tid, uin="", comment_id="", sess=None):
    """删掉自己发错的评论（清理用）"""
    sess = sess or session()
    if not sess.get("ok"):
        return {"ok": False, "msg": sess.get("msg") or "没有登录态"}
    owner = str(uin or sess["uin"])
    url = ("https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/"
           "emotion_cgi_del_comment_v6?g_tk={g}").format(g=sess["gtk"])
    form = {"topicId": f"{owner}_{tid}", "commentId": str(comment_id),
            "inCharset": "utf-8", "outCharset": "utf-8", "format": "json",
            "plat": "qzone", "hostUin": owner, "appid": "311"}
    r = _http(url, form, sess)
    if not r.get("ok"):
        return {"ok": False, "msg": r.get("msg") or "删评论请求失败"}
    d = r["data"] or {}
    ok, code, msg = _ok_of(d)
    return {"ok": ok, "msg": "已删除评论" if ok else f"删除失败（code={code} {msg}）",
            "raw": r.get("raw", "")[:200]}


def comment_list(tid, uin="", sess=None, include_mine=False):
    """读某条动态的评论（用于"回评论"）。

    include_mine=True 时连自己发的也返回 —— v1.9.22 新增，用于发完评论后
    按内容找回刚发那条的 commentid（回包不给 id 时的兜底）。
    """
    sess = sess or session()
    if not sess.get("ok"):
        return {"ok": False, "msg": sess.get("msg") or "没有登录态", "comments": []}
    owner = str(uin or sess["uin"])
    url = ("https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/"
           "emotion_cgi_msgdetail_v6?uin={o}&tid={t}&not_trunc_con=1&g_tk={g}"
           "&format=json").format(o=owner, t=str(tid), g=sess["gtk"])
    r = _http(url, None, sess)
    if not r.get("ok"):
        return {"ok": False, "msg": r.get("msg") or "读评论失败", "comments": []}
    d = r["data"] or {}
    out = []
    for c in (d.get("commentlist") or []):
        if not isinstance(c, dict):
            continue
        if not include_mine and _is_mine(c.get("uin"), sess):
            continue
        out.append({"comment_id": str(c.get("commentid") or c.get("tid") or ""),
                    "uin": str(c.get("uin") or ""),
                    "name": str(c.get("name") or ""),
                    "content": re.sub(r"\s+", " ", str(c.get("content") or "")),
                    "time": _to_ts(c.get("createTime2") or c.get("created_time"))})
    return {"ok": True, "comments": out, "total": _to_ts(d.get("cmtnum"), len(out))}


# ==================== 能力自检（集成前先跑这个） ====================

def capabilities(force=False, write_test=False):
    """逐项测试空间能力，结果缓存 10 分钟。

    返回 {"ok":True,"read":bool,"like":bool,"comment":bool,"detail":[...]}
    write_test=True 时会在自己的一条动态上真的发一条评论再删掉（会留痕迹）。
    """
    now = time.time()
    with _lock:
        if not force and CAP_CACHE["data"] and now - CAP_CACHE["ts"] < 600:
            return CAP_CACHE["data"]
    out = {"ok": False, "read": False, "like": False, "comment": False,
           "uin": "", "detail": []}
    sess = session(force=True)
    if not sess.get("ok"):
        out["detail"].append({"name": "登录态", "ok": False,
                              "msg": sess.get("msg") or "取不到 cookie"})
        with _lock:
            CAP_CACHE.update(ts=now, data=out)
        return out
    out["uin"] = sess["uin"]
    out["detail"].append({"name": "登录态", "ok": True,
                          "msg": f"uin={sess['uin']} {sess['name']}"})

    r = list_feeds(sess=sess, num=3)
    out["read"] = bool(r.get("ok"))
    out["detail"].append({"name": "读动态", "ok": out["read"],
                          "msg": r.get("msg") or f"读到 {len(r.get('feeds') or [])} 条"})
    tid = (r.get("feeds") or [{}])[0].get("tid") if r.get("ok") and r.get("feeds") else ""

    if tid:
        # 点赞→取消，成对做，不改变最终状态
        a = like(tid, sess=sess)
        time.sleep(1.5)
        b = like(tid, sess=sess, unlike=True) if a.get("ok") else {"ok": False, "msg": "-"}
        out["like"] = bool(a.get("ok"))
        out["detail"].append({"name": "点赞", "ok": out["like"],
                              "msg": f"赞:{a.get('msg')} / 取消:{b.get('msg')}"})
        if write_test:
            c = comment(tid, text="[自检] 评论通道测试，稍后自动删除", sess=sess)
            out["comment"] = bool(c.get("ok"))
            out["detail"].append({"name": "评论", "ok": out["comment"],
                                  "msg": c.get("msg") or ""})
            if c.get("ok") and c.get("comment_id"):
                time.sleep(1.5)
                delete_comment(tid, comment_id=c["comment_id"], sess=sess)
                out["detail"].append({"name": "删评论", "ok": True, "msg": "已清理"})
        else:
            out["detail"].append({"name": "评论", "ok": None, "msg": "未测试（write_test=False）"})
    else:
        out["detail"].append({"name": "点赞/评论", "ok": False, "msg": "没有可用动态，跳过"})

    out["ok"] = out["read"]
    with _lock:
        CAP_CACHE.update(ts=now, data=out)
    return out


# ==================== 命令行 ====================

def _main(argv):
    import sys
    cmd = (argv[1] if len(argv) > 1 else "--test").strip()
    if cmd == "--test":
        cap = capabilities(force=True, write_test="--write" in argv)
        print(f"登录态 uin={cap['uin']}")
        for d in cap["detail"]:
            flag = "OK " if d["ok"] else ("-- " if d["ok"] is None else "FAIL")
            print(f"  [{flag}] {d['name']}: {d['msg']}")
        print("结论：读动态", "可用" if cap["read"] else "不可用",
              "| 点赞", "可用" if cap["like"] else "不可用",
              "| 评论", ("可用" if cap["comment"] else "未验证"))
        return 0
    if cmd == "--list":
        uin = argv[2] if len(argv) > 2 else ""
        r = list_feeds(uin, num=10)
        if not r.get("ok"):
            print("失败：", r.get("msg"))
            return 1
        for f in r["feeds"]:
            print(f"[{f['time_text']}] {f['name']}({f['uin']}) tid={f['tid']} "
                  f"赞{f['like_count']} 评{f['cmtnum']}\n    {f['content'][:80]}")
        return 0
    if cmd == "--like":
        print(like(argv[2], argv[3] if len(argv) > 3 else ""))
        return 0
    if cmd == "--comment":
        print(comment(argv[2], argv[3] if len(argv) > 3 else "", " ".join(argv[4:])))
        return 0
    print("用法：python qzone_interact.py [--test [--write]] [--list [uin]] "
          "[--like <tid> [uin]] [--comment <tid> <uin> <内容>]")
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv))
