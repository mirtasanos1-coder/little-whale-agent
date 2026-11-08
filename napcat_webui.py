# -*- coding: utf-8 -*-
"""NapCat WebUI 登录接口（v1.9.17）

为什么要有这个模块
------------------
v1.9.16 之前，「扫码登录」就是 `os.startfile(cache\\qrcode.png)` —— 拉一个系统看图
程序把 png 打开。用户看不到这张码是不是已经过期，过期了也不知道，只能「扫了没反应 →
关掉 → 重新启动」反复折腾，而反复扫码正是被腾讯风控的主要原因。

其实 NapCat 自带 WebUI（默认 6099）已经把登录状态全部暴露出来了，只是我们一直没用：
    POST /api/auth/login                {"hash": sha256(token + ".napcat")}
                                        -> {"code":0,"data":{"Credential":"<base64>"}}
    POST /api/QQLogin/CheckLoginStatus  -> {isLogin, isOffline, loginPhase,
                                            qrLoginAccepted, coreReady,
                                            qrcodeurl, loginError}
    POST /api/QQLogin/RefreshQRcode     -> 强制换一张码（NapCat 内部最多等 10 秒）
    POST /api/QQLogin/GetQuickLoginList -> 本机登录过、本地缓存还在的号
    POST /api/QQLogin/SetQuickLogin     {"uin": "..."} -> 用本地缓存直接登录
    POST /api/QQLogin/GetQQLoginInfo    -> 当前登录的号 / 昵称

有了这些：
  · 二维码可以画在我们自己的窗口里（png 从 cache 读成 base64）；
  · 过期能被发现（loginError 会写「二维码已过期，请刷新」），也能主动换新码；
  · 本机登录过好几个号时，可以让用户自己挑一个（SetQuickLogin），而不是我们猜。

原则
----
薄客户端 + 全部降级。WebUI 没开、token 不对、接口改版、网络卡住 —— 任何一步失败都
只是返回 ok=False 和一句人话，绝不让界面因为「拿不到二维码」而崩掉。看门狗线程不会
因为这里抛异常而死掉。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request

# 登录凭证默认有效 1 小时（NapCat 侧 validateCredentialWithinOneHour）。
# 我们提前 15 分钟就换一张，免得用到期边缘反复 401。
_CRED_TTL = 45 * 60
# 登录接口有速率限制（webui.json 的 loginRate，默认 10 次/分钟/IP），
# 所以凭证必须缓存，不能每个请求都登一次。
_LOGIN_MIN_GAP = 3.0

_lock = threading.Lock()
_cred_cache = {"key": "", "cred": "", "ts": 0.0, "fail_ts": 0.0, "fail_msg": ""}


# ==================== 基础 ====================


def _mgr():
    """延迟导入：napcat_mgr 很重（会摸进程、端口），循环导入也受不了。"""
    import napcat_mgr

    return napcat_mgr


def cfg(install_dir=""):
    """WebUI 的 base_url / port / token。读不到就返回默认值（不抛异常）。"""
    try:
        wi = _mgr().webui_info(install_dir)
    except Exception:
        wi = {}
    port = int((wi or {}).get("port") or 6099)
    token = str((wi or {}).get("token") or "")
    host = "127.0.0.1"
    return {"base": "http://%s:%d" % (host, port), "port": port, "token": token}


def password_hash(token):
    """NapCat 的密码 hash 约定：sha256(token + ".napcat") 的十六进制。

    注意这不是我们发明的：napcat.mjs 里 generatePasswordHash 就是这一行，
    /api/auth/login 拿它跟 webui.json 里的 token 比对。
    """
    t = str(token or "")
    if not t:
        return ""
    return hashlib.sha256((t + ".napcat").encode("utf-8")).hexdigest()


def _http_post(url, body, headers=None, timeout=6.0):
    """POST JSON，返回 (status_code, dict)。网络层异常统一抛给调用方处理。"""
    data = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=float(timeout)) as r:
        raw = r.read(200000).decode("utf-8", "replace")
        code = int(getattr(r, "status", 200) or 200)
    try:
        return code, (json.loads(raw) if raw else {})
    except Exception:
        return code, {"_raw": raw[:400]}


def reachable(install_dir="", timeout=1.2):
    """WebUI 端口通不通（不登录，纯 TCP）。用于「要不要走 WebUI 这条路」的判断。"""
    c = cfg(install_dir)
    try:
        return bool(_mgr().port_open(c["port"], timeout=timeout))
    except Exception:
        return False


# ==================== 鉴权 ====================


def login(install_dir="", force=False, timeout=6.0):
    """拿一张登录凭证（base64 Credential）。失败返回 ""。

    凭证缓存 45 分钟；失败后 30 秒内不再重试 —— 登录接口有 rate limit，
    界面每 1.5 秒轮询一次，不缓存的话一次连点就把自己锁死了。
    """
    c = cfg(install_dir)
    if not c["token"]:
        return ""
    key = "%s|%s" % (c["base"], c["token"])
    now = time.time()
    with _lock:
        if (not force and _cred_cache["key"] == key and _cred_cache["cred"]
                and now - _cred_cache["ts"] < _CRED_TTL):
            return _cred_cache["cred"]
        if not force and _cred_cache["key"] == key and now - _cred_cache["fail_ts"] < 30:
            return ""
    body = {"hash": password_hash(c["token"])}
    try:
        code, data = _http_post(c["base"] + "/api/auth/login", body, timeout=timeout)
        cred = str(((data or {}).get("data") or {}).get("Credential") or "")
        if code == 200 and (data or {}).get("code") == 0 and cred:
            with _lock:
                _cred_cache.update(key=key, cred=cred, ts=time.time(),
                                   fail_ts=0.0, fail_msg="")
            return cred
        msg = str((data or {}).get("message") or "登录 WebUI 失败")
        with _lock:
            _cred_cache.update(key=key, cred="", fail_ts=time.time(), fail_msg=msg[:160])
        return ""
    except Exception as e:
        with _lock:
            _cred_cache.update(key=key, cred="", fail_ts=time.time(),
                               fail_msg="%s: %s" % (type(e).__name__, e))
        return ""


def post(path, body=None, install_dir="", timeout=6.0, need_auth=True, retry=True):
    """调一个 WebUI 接口。返回 {"ok","code","data","message"}，永不抛异常。"""
    c = cfg(install_dir)
    out = {"ok": False, "code": -1, "data": None, "message": ""}
    if not c["base"]:
        out["message"] = "WebUI 地址未知"
        return out
    headers = {}
    if need_auth:
        cred = login(install_dir, timeout=timeout)
        if not cred:
            with _lock:
                out["message"] = _cred_cache.get("fail_msg") or "拿不到 WebUI 凭证"
            return out
        headers["Authorization"] = "Bearer " + cred
    try:
        code, data = _http_post(c["base"] + path, body, headers=headers, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(4000).decode("utf-8", "replace")
            data = json.loads(raw) if raw else {}
        except Exception:
            data = {}
        code = int(getattr(e, "code", 0) or 0)
    except Exception as e:
        out["message"] = "%s: %s" % (type(e).__name__, e)
        return out
    if code == 401 and need_auth and retry:
        # 凭证可能过期/被顶掉：强制换一张再来一次（只重试一次，避免死循环）
        login(install_dir, force=True, timeout=timeout)
        return post(path, body, install_dir=install_dir, timeout=timeout,
                    need_auth=need_auth, retry=False)
    out["code"] = (data or {}).get("code", 0 if code == 200 else -1)
    out["data"] = (data or {}).get("data")
    out["message"] = str((data or {}).get("message") or "")
    out["ok"] = bool(code == 200 and out["code"] == 0)
    if not out["ok"] and not out["message"]:
        out["message"] = "HTTP %s" % code
    return out


# ==================== 登录状态 ====================


def status(install_dir="", timeout=2.5):
    """问 NapCat 现在登录到哪一步了。

    返回 {"ok","reachable","is_login","offline","phase","error","qr_url","uin","msg"}
      phase: waiting_qrcode / qrcode_scanned / initializing / ready / reconnecting / offline
      error: 例如「二维码已过期，请刷新」「当前账号(xxx)已登录,无法重复登录」
    """
    out = {"ok": False, "reachable": False, "is_login": False, "offline": False,
           "phase": "", "error": "", "qr_url": "", "uin": "", "msg": ""}
    if not reachable(install_dir):
        out["msg"] = "NapCat 的 WebUI 端口没开"
        return out
    out["reachable"] = True
    r = post("/api/QQLogin/CheckLoginStatus", {}, install_dir=install_dir, timeout=timeout)
    if not r["ok"]:
        out["msg"] = r["message"] or "WebUI 没返回登录状态"
        return out
    d = r["data"] or {}
    out.update(ok=True, is_login=bool(d.get("isLogin")),
               offline=bool(d.get("isOffline")),
               phase=str(d.get("loginPhase") or ""),
               error=str(d.get("loginError") or ""),
               qr_url=str(d.get("qrcodeurl") or ""),
               core_ready=bool(d.get("coreReady")))
    try:
        info = post("/api/QQLogin/GetQQLoginInfo", {}, install_dir=install_dir, timeout=timeout)
        dd = (info or {}).get("data") or {}
        if dd.get("uin"):
            out["uin"] = str(dd.get("uin"))
            out["nick"] = str(dd.get("nick") or "")
    except Exception:
        pass
    return out


def quick_list(install_dir="", timeout=4.0):
    """本机登录过的号（NapCat 记着的快速登录名单）。返回 [{"uin","nick"}]。"""
    out = []
    r = post("/api/QQLogin/GetQuickLoginListNew", {}, install_dir=install_dir, timeout=timeout)
    if not r["ok"]:
        r = post("/api/QQLogin/GetQuickLoginList", {}, install_dir=install_dir, timeout=timeout)
    d = (r or {}).get("data")
    if isinstance(d, list):
        for it in d:
            if isinstance(it, dict):
                u = str(it.get("uin") or it.get("uid") or "").strip()
                if u.isdigit():
                    out.append({"uin": u, "nick": str(it.get("nick") or it.get("nickName") or "")})
            elif isinstance(it, (str, int)):
                s = str(it).strip()
                if s.isdigit():
                    out.append({"uin": s, "nick": ""})
    elif isinstance(d, dict):
        for k, v in d.items():
            s = str(v if not isinstance(v, dict) else (v.get("uin") or k)).strip()
            if s.isdigit():
                out.append({"uin": s, "nick": str(v.get("nick") or "") if isinstance(v, dict) else ""})
    # 去重，保持顺序
    seen, uniq = set(), []
    for a in out:
        if a["uin"] not in seen:
            seen.add(a["uin"])
            uniq.append(a)
    return uniq


def quick_login(uin, install_dir="", timeout=20.0):
    """让 NapCat 用本地缓存直接登这个号（不用扫码、不用重启进程）。

    这是「本机登录过好几个 QQ，让用户自己选」的落地方式：调用前不需要停 NapCat，
    也不需要退出官方 QQ —— 成功与否由 NapCat 判断，失败会回一句人话
    （最常见的是「当前账号已登录,无法重复登录」）。
    """
    u = str(uin or "").strip()
    if not (u.isdigit() and 5 <= len(u) <= 12):
        return {"ok": False, "msg": "QQ 号不对：%s" % (uin or "(空)")}
    r = post("/api/QQLogin/SetQuickLogin", {"uin": u}, install_dir=install_dir, timeout=timeout)
    if r["ok"]:
        return {"ok": True, "msg": "已让 NapCat 用缓存登录 QQ %s，等它连上就行" % u, "uin": u}
    msg = r["message"] or "快速登录失败"
    if "已登录" in msg and "重复" in msg:
        msg = ("QQ %s 正在官方客户端里在线，同一个号不能在两边同时登录。\n"
               "要么在官方 QQ 里退出登录，要么扫码换一个号。" % u)
    return {"ok": False, "msg": msg, "uin": u}


def refresh_qr(install_dir="", timeout=14.0):
    """让 NapCat 换一张新二维码（内置最多等 10 秒，这里给 14 秒余量）。"""
    r = post("/api/QQLogin/RefreshQRcode", {}, install_dir=install_dir, timeout=timeout)
    if r["ok"]:
        url = str(((r.get("data") or {}) if isinstance(r.get("data"), dict) else {}).get("qrcodeurl") or "")
        return {"ok": True, "msg": "已换一张新二维码", "qr_url": url,
                "restarting": bool((r.get("data") or {}).get("restarting"))}
    return {"ok": False, "msg": r["message"] or "换码失败"}


def set_auto_login(uin, install_dir=""):
    """把某个号记成 NapCat 的自动登录账号（下次启动直接用它）。"""
    r = post("/api/QQLogin/SetQuickLoginQQ", {"uin": str(uin or "")},
             install_dir=install_dir, timeout=6.0)
    return {"ok": bool(r["ok"]), "msg": r["message"] or ("已记住 QQ %s" % uin)}
