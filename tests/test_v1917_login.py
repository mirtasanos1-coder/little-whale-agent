# -*- coding: utf-8 -*-
"""V1.9.17 自测：QQ 登录补丁。

用户报的是三件事，对应这里的四组断言：
1. 二维码以前 os.startfile 拉系统看图程序，看不出过没过期 —— 现在要能拿到 base64、
   能算出「还剩几秒」、过期要判成过期。
2. 过期了要能换一张（走 NapCat WebUI 的 RefreshQRcode），不能只能重启。
3. 本机登录过好几个号时，要让用户自己选，并且能用本地缓存直接登（不扫码不退 QQ）。
4. WebUI 没起来 / token 不对 / 接口 401 —— 任何一步失败都只能降级成一句人话，
   绝不能让界面因为「拿不到二维码」而崩掉。

跑法：python tests\\test_v1917_login.py
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

TMP = tempfile.mkdtemp(prefix="xy_v1917_")
os.environ["QQ_AGENT_DATA_DIR"] = TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_ui  # noqa: E402
import napcat_mgr  # noqa: E402
import napcat_webui  # noqa: E402

PASS, FAIL = [], []
_REAL_QR_PNG = napcat_mgr.qr_png   # 后面要按安装目录打桩，先留一份真函数


def ok(name, cond, extra=""):
    if cond:
        PASS.append(name)
        print("  [PASS] %s" % name)
    else:
        FAIL.append(name)
        print("  [FAIL] %s %s" % (name, extra))


# ---------------------------------------------------------------- 假 WebUI
TOKEN = "testtoken123"
CRED = "Y3JlZC1mb3ItdGVzdA=="
ST = {
    "auth_calls": 0,
    "other_calls": 0,
    "refresh_calls": 0,
    "reject_left": 0,          # >0 时接着来的 N 个请求都回 401（模拟凭证过期后自愈）
    "status": {"isLogin": False, "isOffline": False, "loginPhase": "waiting_qrcode",
               "qrLoginAccepted": False, "coreReady": False,
               "qrcodeurl": "https://example.invalid/qr1", "loginError": ""},
    "quick": [{"uin": "11111111", "nick": "A"}, {"uin": "22222222", "nick": "B"},
              {"uin": "11111111", "nick": "dup"}],
    "pick_bad": ["33333333"],
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        path = self.path.split("?")[0]
        if path == "/api/auth/login":
            ST["auth_calls"] += 1
            want = hashlib.sha256((TOKEN + ".napcat").encode("utf-8")).hexdigest()
            if body.get("hash") != want:
                return self._send(403, {"code": -1, "message": "token 不对"})
            return self._send(200, {"code": 0, "data": {"Credential": CRED}})
        ST["other_calls"] += 1
        if self.headers.get("Authorization") != "Bearer " + CRED:
            return self._send(401, {"code": -1, "message": "未授权"})
        if ST["reject_left"] > 0:
            ST["reject_left"] -= 1
            return self._send(401, {"code": -1, "message": "凭证过期"})
        if path == "/api/QQLogin/CheckLoginStatus":
            return self._send(200, {"code": 0, "data": ST["status"]})
        if path == "/api/QQLogin/GetQQLoginInfo":
            return self._send(200, {"code": 0, "data": {"uin": "11111111", "nick": "鲸"}})
        if path == "/api/QQLogin/RefreshQRcode":
            ST["refresh_calls"] += 1
            ST["status"]["qrcodeurl"] = "https://example.invalid/qr%d" % (ST["refresh_calls"] + 1)
            ST["status"]["loginError"] = ""
            return self._send(200, {"code": 0, "data": {"qrcodeurl": ST["status"]["qrcodeurl"]}})
        if path in ("/api/QQLogin/GetQuickLoginListNew", "/api/QQLogin/GetQuickLoginList"):
            return self._send(200, {"code": 0, "data": ST["quick"]})
        if path == "/api/QQLogin/SetQuickLogin":
            u = str(body.get("uin") or "")
            if u in ST["pick_bad"]:
                return self._send(200, {"code": -1,
                                        "message": "当前账号(%s)已登录,无法重复登录" % u})
            return self._send(200, {"code": 0, "data": {"uin": u}})
        return self._send(404, {"code": -1, "message": "没有这个接口"})


srv = HTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def _cfg(install_dir=""):
    return {"base": "http://127.0.0.1:%d" % PORT, "port": PORT, "token": TOKEN}


# ================================================================ 1. 鉴权
def t_auth():
    print("\n[1] WebUI 鉴权")
    want = hashlib.sha256((TOKEN + ".napcat").encode("utf-8")).hexdigest()
    ok("password_hash 是 sha256(token+'.napcat')",
       napcat_webui.password_hash(TOKEN) == want, napcat_webui.password_hash(TOKEN))
    ok("空 token 不瞎发请求", napcat_webui.password_hash("") == "")

    napcat_webui.cfg = _cfg
    napcat_webui.reachable = lambda *a, **k: True
    c = napcat_webui.login(force=True)
    ok("拿到凭证", c == CRED, c)
    ok("凭证带 base64 形态", c.startswith("Y3Jl"), c)
    before = ST["auth_calls"]
    for _ in range(3):
        napcat_webui.login()
    ok("凭证被缓存（3 次调用没再登录）", ST["auth_calls"] == before,
       "auth_calls=%d" % ST["auth_calls"])

    # token 写错 → 登录失败但要给人话，不能抛异常
    napcat_webui.cfg = lambda install_dir="": {"base": "http://127.0.0.1:%d" % PORT,
                                              "port": PORT, "token": "wrongtoken"}
    napcat_webui.login(force=True)
    r = napcat_webui.post("/api/QQLogin/CheckLoginStatus", {}, timeout=3)
    ok("错 token 只降级成一句话", r["ok"] is False and bool(r["message"]), r.get("message"))
    napcat_webui.cfg = _cfg


# ================================================================ 2. 状态与换码
def t_status():
    print("\n[2] 登录状态 / 换码")
    r = napcat_webui.status(timeout=3)
    ok("status 通了", r["ok"] and r["reachable"], r.get("msg"))
    ok("state 里能读到 loginPhase", r["phase"] == "waiting_qrcode", r["phase"])
    ok("能读到二维码 URL", r["qr_url"].startswith("https://example.invalid/qr"), r["qr_url"])
    ok("能读到当前登录号", r["uin"] == "11111111", r["uin"])

    ST["status"]["loginError"] = "二维码已过期，请刷新"
    ST["status"]["loginPhase"] = "waiting_qrcode"
    r2 = napcat_webui.status(timeout=3)
    ok("过期信息能传到界面", "过期" in r2["error"], r2["error"])
    ST["status"]["loginError"] = ""

    before = ST["refresh_calls"]
    rf = napcat_webui.refresh_qr(timeout=6)
    ok("换码成功", rf["ok"], rf.get("msg"))
    ok("换码调到了 NapCat", ST["refresh_calls"] == before + 1, ST["refresh_calls"])
    ok("换码返回新 URL", "qr" in rf.get("qr_url", ""), rf.get("qr_url"))


# ================================================================ 3. 多账号
def t_accounts():
    print("\n[3] 多账号：让用户自己选")
    ql = napcat_webui.quick_list(timeout=4)
    ok("能列出本机登录过的号", [a["uin"] for a in ql] == ["11111111", "22222222"],
       ql)
    ok("重复的号去掉了", len(ql) == 2, ql)
    ok("带上了昵称", ql[0].get("nick") == "A", ql[0])

    good = napcat_webui.quick_login("11111111", timeout=6)
    ok("缓存直登成功", good["ok"], good.get("msg"))

    bad = napcat_webui.quick_login("33333333", timeout=6)
    ok("已被占用的号登不上", bad["ok"] is False)
    ok("把 168/重复登录 翻成人话",
       "同一个号不能在两边同时登录" in bad["msg"], bad.get("msg"))

    weird = napcat_webui.quick_login("abc", timeout=6)
    ok("非法 QQ 号本地就拦掉", weird["ok"] is False and "不对" in weird["msg"], weird.get("msg"))


# ================================================================ 4. 二维码图
def t_qr_png():
    print("\n[4] 二维码读成 base64（画在应用内）")
    d = tempfile.mkdtemp(prefix="xy_napcat_")
    cache = os.path.join(d, "napcat", "cache")
    os.makedirs(cache, exist_ok=True)
    png = os.path.join(cache, "qrcode.png")
    fake = b"\x89PNG\r\n\x1a\n" + b"0" * 400
    with open(png, "wb") as f:
        f.write(fake)
    qr = napcat_mgr.qr_png(d)
    ok("读到了二维码", bool(qr) and qr.get("size", 0) > 64, qr.get("path"))
    ok("给了 data URL", str(qr.get("data_url", "")).startswith("data:image/png;base64,"))
    ok("给了指纹（用来判断要不要换图）", len(str(qr.get("sha1", ""))) == 16, qr.get("sha1"))
    ok("刚写出来的码 age 很小", int(qr.get("age", 999)) <= 3, qr.get("age"))

    # 把 mtime 往前拨 200 秒 = 过期
    old = time.time() - 200
    os.utime(png, (old, old))
    qr2 = napcat_mgr.qr_png(d)
    ok("过期能算出来", int(qr2.get("age", 0)) >= 190, qr2.get("age"))
    ok("内容没变指纹就不变（前端不闪）", qr2.get("sha1") == qr.get("sha1"))

    # 换码 → 内容变了 → 指纹要变
    with open(png, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"1" * 400)
    qr3 = napcat_mgr.qr_png(d)
    ok("换码后指纹变了", qr3.get("sha1") != qr.get("sha1"), qr3.get("sha1"))

    ok("没有二维码时返回空", napcat_mgr.qr_png(tempfile.mkdtemp()) == {})
    return d


# ================================================================ 5. 弹窗数据
def t_panel():
    print("\n[5] 弹窗要的一整包数据")
    d = tempfile.mkdtemp(prefix="xy_install_")
    cache = os.path.join(d, "napcat", "cache")
    os.makedirs(cache, exist_ok=True)
    with open(os.path.join(cache, "qrcode.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + b"q" * 600)

    napcat_mgr.probe = lambda *a, **k: {"state": "waiting_scan", "running": True,
                                        "proc_count": 1, "dir": d}
    napcat_mgr.find_install_dir = lambda *a, **k: d
    napcat_mgr.login_info = lambda *a, **k: {}
    napcat_mgr.qr_png = lambda install_dir="", **k: _REAL_QR_PNG(d)
    napcat_mgr.login_candidates = lambda install_dir="", **k: ["11111111", "22222222", "33333333"]
    napcat_mgr.uin_state = lambda *a, **k: {"last": "22222222", "bad": {"33333333": 1},
                                            "ok": {"11111111": 1}}
    napcat_mgr.mark_uin_ok = lambda *a, **k: None

    p = app_ui.qq_login_panel(fresh=True)
    ok("panel 能出来", p.get("ok"), p.get("msg"))
    ok("未登录时是 scan 模式", p.get("mode") == "scan", p.get("mode"))
    ok("带上了二维码", str(p.get("qr", {}).get("data_url", "")).startswith("data:"))
    ok("带上了剩余秒数", 0 < int(p.get("expires_in", 0)) <= app_ui.QR_TTL_SEC,
       p.get("expires_in"))
    uins = [a["uin"] for a in p.get("accounts", [])]
    ok("三个号都列出来了", sorted(uins) == ["11111111", "22222222", "33333333"], uins)
    ok("上回失效的号排在最后", uins[-1] == "33333333", uins)
    ok("缓存能用的号排在最前", bool(p["accounts"][0]["cached"]) and not p["accounts"][0]["bad"],
       p["accounts"][0])
    ok("标记了「上次用的」", any(a["last"] for a in p.get("accounts", [])))
    ok("多个号时提示让用户自己点", any("登录过" in n for n in p.get("notes", [])),
       p.get("notes"))
    ok("报告 WebUI 可用（可自动换码）", p.get("webui", {}).get("reachable") is True)

    # 二维码过期 → 面板要如实说
    old = time.time() - (app_ui.QR_TTL_SEC + 60)
    os.utime(os.path.join(cache, "qrcode.png"), (old, old))
    p2 = app_ui.qq_login_panel(fresh=True)
    ok("过期时 qr_expired=True", p2.get("qr_expired") is True)
    ok("过期时剩余秒数为 0", int(p2.get("expires_in", 1)) == 0, p2.get("expires_in"))
    ok("过期时文案提到换新", "过期" in (p2.get("msg") or ""), p2.get("msg"))

    # 已登录 → 直接说已连上，不再让人扫码
    napcat_mgr.login_info = lambda *a, **k: {"user_id": 11111111, "nickname": "鲸"}
    p3 = app_ui.qq_login_panel(fresh=True)
    ok("已登录时 mode=logged_in", p3.get("mode") == "logged_in", p3.get("mode"))
    ok("已登录时带上 QQ 号", p3.get("uin") == "11111111", p3.get("uin"))
    ok("已登录时文案是「已经连上了」", "已经连上" in (p3.get("msg") or ""), p3.get("msg"))

    # NapCat 没跑 → 让人点「启动并出码」，别丢一张过期图给人扫
    napcat_mgr.login_info = lambda *a, **k: {}
    napcat_mgr.probe = lambda *a, **k: {"state": "down", "running": False,
                                        "proc_count": 0, "dir": d}
    p4 = app_ui.qq_login_panel(fresh=True)
    ok("没跑时 mode=start", p4.get("mode") == "start", p4.get("mode"))
    ok("没跑时提示启动", "启动" in (p4.get("msg") or ""), p4.get("msg"))

    # 同一个号在官方 QQ 里在线 → busy，且不能劝人扫码
    napcat_mgr.probe = lambda *a, **k: {"state": "qq_client_busy", "running": True,
                                        "proc_count": 1, "dir": d,
                                        "msg": "QQ 3280735835 正在官方客户端里登录，同一个号不能两边同时在线。"}
    p5 = app_ui.qq_login_panel(fresh=True)
    ok("被占用时 mode=busy", p5.get("mode") == "busy", p5.get("mode"))
    ok("把占用原因转达给用户", "同一" in (p5.get("msg") or ""), p5.get("msg"))


# ================================================================ 6. 降级
def t_degrade():
    print("\n[6] WebUI 用不了时只能降级，不能崩")
    d = tempfile.mkdtemp(prefix="xy_deg_")
    napcat_mgr.find_install_dir = lambda *a, **k: d
    napcat_webui.reachable = lambda *a, **k: False
    r = app_ui.qq_login_refresh()
    ok("WebUI 没开时换码失败但不抛", r.get("ok") is False and bool(r.get("msg")), r.get("msg"))
    ok("并告诉用户怎么办（重启）", "重启" in (r.get("msg") or ""), r.get("msg"))

    pick = app_ui.qq_login_pick("11111111")
    ok("WebUI 没开时选号失败但不抛", pick.get("ok") is False and bool(pick.get("msg")),
       pick.get("msg"))

    ok("非法 QQ 号不落到后端请求", app_ui.qq_login_pick("")["ok"] is False)

    # 状态接口整个挂掉：status 必须返回 ok=False + 人话
    napcat_webui.reachable = lambda *a, **k: True
    napcat_webui.cfg = lambda install_dir="": {"base": "http://127.0.0.1:9", "port": 9,
                                              "token": TOKEN}
    s = napcat_webui.status(timeout=1.5)
    ok("连不上时不抛异常", s["ok"] is False and bool(s.get("msg")), s.get("msg"))


# ================================================================ 7. 401 重试
def t_401():
    print("\n[7] 凭证过期能自愈")
    napcat_webui.cfg = _cfg
    napcat_webui.reachable = lambda *a, **k: True
    napcat_webui.login(force=True)
    before = ST["auth_calls"]
    # 第一个带凭证的请求回 401（凭证被顶掉/到期），重登后的第二次必须成功
    ST["reject_left"] = 1
    r = napcat_webui.post("/api/QQLogin/CheckLoginStatus", {}, timeout=4)
    ok("401 后自动重新登录并成功", r["ok"] is True, r.get("message"))
    ok("重新登录确实又登了一次", ST["auth_calls"] > before,
       "auth_calls=%d before=%d" % (ST["auth_calls"], before))
    ok("只重试一次，不会无限打转", ST["reject_left"] == 0, ST["reject_left"])


def main():
    print("=" * 60)
    print("V1.9.17 自测：QQ 登录补丁")
    print("数据目录：%s" % TMP)
    print("假 WebUI：http://127.0.0.1:%d" % PORT)
    print("=" * 60)
    for fn in (t_auth, t_status, t_accounts, t_qr_png, t_panel, t_degrade, t_401):
        try:
            fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAIL.append(fn.__name__ + " 抛异常")
    print("\n" + "=" * 60)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  失败：%s" % f)
        return 1
    print("V1.9.17 自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
