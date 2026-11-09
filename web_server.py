# -*- coding: utf-8 -*-
"""网页启动版（V1.1.2 #22）——用浏览器开控制台，不用 pywebview 窗口。

用法（源码目录下）：
    python web_server.py                 # 打开 http://127.0.0.1:8765
    python web_server.py --port 9000     # 换端口
    python web_server.py --host 0.0.0.0  # 让局域网别的设备也能开（注意：没有登录，别放到公网）

它做的事很简单：
  1. 起一个本地 HTTP 服务，把 index.html 发出去（注入一小段 shim，把 pywebview 的
     window.pywebview.api 换成 fetch('/rpc')）；
  2. /rpc 收到 {method, args} 就调用 app_ui.Api() 的同名方法，把返回值当 JSON 回。
所以前端代码一行都不用改，网页版和窗口版走的是同一套后端。

限制（写在文档里，也在这里提醒一遍）：
  · 文件选择框、原生目录对话框在网页里用不了 —— 相关接口会返回「网页版不支持」；
  · 网页版默认只听 127.0.0.1，请不要在没有反向代理鉴权的情况下暴露到公网；
  · 托盘、无边框、窗口最小化这些只有窗口版有。
"""

import argparse
import io
import json
import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(BASE_DIR, "index.html")
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}

SHIM = """
<script>
window.__WEB_MODE__ = true;
(function () {
  if (window.pywebview && window.pywebview.api) return;   // 真窗口里已经有桥，不覆盖
  window.pywebview = { api: new Proxy({}, {
    get: function (_t, method) {
      return async function () {
        const args = Array.prototype.slice.call(arguments);
        const r = await fetch('/rpc', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ method: String(method), args: args })
        });
        let j = null;
        try { j = await r.json(); } catch (e) { j = { ok: false, msg: '后端返回的不是 JSON' }; }
        return j;
      };
    }
  }) };
  setTimeout(function () { window.dispatchEvent(new Event('pywebviewready')); }, 30);
})();
</script>
"""

# 这些方法要真窗口（文件框/目录框/托盘），网页里直接给个友好回执
WINDOW_ONLY_HINT = "网页版不支持这个操作，请用窗口版"

# 打包版（frozen）里根本没有 index.html（它在 Tauri 的前端包里，不在 _MEIPASS），
# 所以网页版在分发包里是「口子开着、界面打不开」——QA 终审必修 2 要求直接下架。
FROZEN = bool(getattr(sys, "frozen", False))


def _guard_rpc(handler):
    """`/rpc` 的入口校验（QA V1.3.2 终审 · 必修 2，P0）。

    以前 `do_POST` 收到 {method,args} 就直接 `getattr(api, method)(*args)`：
    没有 Origin 校验、不看 Content-Type、不认危险方法、不要票据。
    任意网页只要 POST 到 127.0.0.1:8765/rpc 就能跑 `run_uninstall` /
    `memory_rebuild_run` / `set_codeword` —— 把一次性票据体系整个旁路。
    这里把 backend_server 的那套校验复用过来。

    返回 (code, obj) 表示拒绝；返回 None 表示放行。
    """
    # 1) Content-Type 必须是 JSON（text/plain 是 CSRF 的经典简单请求）
    ctype = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return 415, {"ok": False, "code": "bad_content_type",
                     "msg": "网页版只接受 application/json（安全策略）"}
    # 2) Origin 只认本机网页版自己这个端口（和 Tauri 壳）
    origin = (handler.headers.get("Origin") or "").strip()
    if origin:
        host = (handler.headers.get("Host") or "").strip().lower()
        ok = False
        for prefix in ("http://", "https://"):
            if origin.lower() in (prefix + host, prefix + "tauri.localhost"):
                ok = True
                break
        if not ok:
            return 403, {"ok": False, "code": "bad_origin",
                         "msg": "这个来源不允许调用网页版接口"}
    # 3) 没有 Origin 却标了 cross-site → 脚本伪装（浏览器跨站请求一定带 Origin）。
    #    注意顺序：**先看 Origin 再判 cross-site** —— 页面从 127.0.0.1:8765 发到
    #    同一端口的 POST 是 same-origin，不会被这条误伤。
    elif (handler.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
        return 403, {"ok": False, "code": "cross_site",
                     "msg": "拒绝来自其它站点的请求"}
    return None


def _danger_check(method, handler):
    """破坏性方法必须出示一次性票据（和 backend_server 共用同一套票据）。"""
    try:
        import backend_server as bs
    except Exception as e:
        return 403, {"ok": False, "code": "no_guard",
                     "msg": "安全模块没加载起来，拒绝执行破坏性操作：%s" % e}
    if method not in getattr(bs, "_DANGER_METHODS", set()):
        return None
    nonce = (handler.headers.get("X-Confirm-Ticket") or "").strip()
    origin = (handler.headers.get("Origin") or "").strip()
    ip = handler.client_address[0] if handler.client_address else ""
    if not nonce or not bs._consume_ticket(nonce, method, origin, ip):
        return 403, {"ok": False, "code": "ticket_required", "danger": method,
                     "msg": "网页版不支持破坏性操作（需要窗口版的一次性票据）"}
    return None


def _api():
    import app_ui
    return app_ui.Api()


class Handler(BaseHTTPRequestHandler):
    server_version = "WhaleAgentWeb/1.1.2"

    def log_message(self, fmt, *args):   # 安静点，别把每条请求都刷到控制台
        try:
            if str(args[1] if len(args) > 1 else "") not in ("200", "204", "304"):
                sys.stderr.write("[web] %s %s\n" % (args[0], args[1] if len(args) > 1 else ""))
        except Exception:
            pass

    # ---------- 小工具 ----------
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    # ---------- GET ----------
    def do_GET(self):
        path = (self.path or "/").split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with io.open(INDEX, "r", encoding="utf-8") as f:
                    html = f.read()
            except Exception as e:
                self._send(500, f"读不到 index.html：{e}", "text/plain; charset=utf-8")
                return
            if "</head>" in html:
                html = html.replace("</head>", SHIM + "</head>", 1)
            else:
                html = SHIM + html
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/health":
            self._json(200, {"ok": True, "mode": "web", "version": _version()})
            return
        # 静态文件：只允许白名单目录，防目录穿越
        rel = path.lstrip("/")
        allowed = ("icons/", "langs/", "assets/", "static/")
        if rel.startswith(allowed) and ".." not in rel:
            full = os.path.abspath(os.path.join(BASE_DIR, rel))
            if full.startswith(os.path.abspath(BASE_DIR)) and os.path.isfile(full):
                ext = os.path.splitext(full)[1].lower()
                try:
                    with open(full, "rb") as f:
                        data = f.read()
                except Exception as e:
                    self._send(500, str(e), "text/plain; charset=utf-8")
                    return
                self._send(200, data, MIME.get(ext, "application/octet-stream"))
                return
        self._send(404, "not found", "text/plain; charset=utf-8")

    # ---------- POST /rpc ----------
    def do_POST(self):
        if (self.path or "").split("?")[0] != "/rpc":
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        # 入口校验（QA 终审必修 2）：Origin / Content-Type / Sec-Fetch-Site
        bad = _guard_rpc(self)
        if bad is not None:
            self._json(bad[0], bad[1])
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b"{}"
            req = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._json(400, {"ok": False, "msg": "请求体不是 JSON：%s" % e})
            return
        method = str(req.get("method") or "").strip()
        args = req.get("args") if isinstance(req.get("args"), list) else []
        if not method or method.startswith("_") or method in ("main", "Api"):
            self._json(400, {"ok": False, "msg": "方法名不合法"})
            return
        # 破坏性方法要票据（QA 终审必修 2）：没有有效票据一律 403
        bad = _danger_check(method, self)
        if bad is not None:
            sys.stderr.write("[web] 拒绝破坏性方法: %s\n" % method)
            self._json(bad[0], bad[1])
            return
        try:
            api = self.server.api  # 复用同一个 Api 实例（它本身无状态）
            fn = getattr(api, method, None)
            if not callable(fn):
                self._json(200, {"ok": False, "msg": "后端缺少方法: %s" % method})
                return
            try:
                out = fn(*args)
            except TypeError as e:
                self._json(200, {"ok": False, "msg": "参数对不上（%s）：%s" % (method, e)})
                return
            if not isinstance(out, (dict, list, str, int, float, bool, type(None))):
                out = str(out)
            self._json(200, out)
        except Exception as e:
            # 窗口类接口在网页里必然失败，这里给能看懂的话
            msg = "%s: %s" % (type(e).__name__, e)
            if "webview" in msg.lower() or "Window" in msg:
                msg = WINDOW_ONLY_HINT + "（%s）" % msg
            self._json(200, {"ok": False, "msg": msg})


def _version():
    try:
        import config
        return getattr(config, "APP_VERSION", "")
    except Exception:
        return ""


def run(host="127.0.0.1", port=8765, open_browser=True):
    if FROZEN:
        # QA 终审必修 2：分发包里没有 index.html（它在 Tauri 的前端包里），
        # 网页版只会 500，而 /rpc 却照旧能打 —— 宁可这条路直接不存在。
        raise SystemExit("打包版不支持网页版：界面在安装包的前端资源里，"
                         "网页版只对源码运行有效。请直接用窗口版。")
    if not os.path.isfile(INDEX):
        raise SystemExit("找不到 index.html：%s" % INDEX)
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.api = _api()
    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "::") else host, port)
    print("=" * 60)
    print("小鲸鱼 Agent 控制台 · 网页版 v%s" % (_version() or "?"))
    print("打开：%s" % url)
    print("停止：Ctrl+C")
    print("提醒：网页版只有聊天/记忆/设置这些托管功能；文件选择框、托盘、无边框窗口只有窗口版有。")
    print("=" * 60)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        try:
            srv.server_close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="小鲸鱼控制台 · 网页启动版")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认只本机）")
    ap.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    # v1.1.7 修（P1-1）：exe 的 `小鲸鱼.exe --web` 会走到这里，但以前这个
    # 解析器不认 --web，argparse 直接 exit(2)：
    #   "error: unrecognized arguments: --web"
    # 于是 README / 使用说明里承诺的网页版入口等于不存在。这里把 --web /
    # --webui 显式收下（它们只是"走这条路"的标记，不用额外做事）。
    ap.add_argument("--web", "--webui", dest="web", action="store_true",
                    help="以网页版方式启动（兼容 小鲸鱼.exe --web，默认行为）")
    # 用 parse_known_args：窗口版将来再加参数也不会把网页版带崩，
    # 认不出来的只提示一句，不 exit(2)。
    a, unknown = ap.parse_known_args()
    if unknown:
        print("提示：忽略未识别的参数 %s" % " ".join(unknown))
    run(a.host, a.port, not a.no_browser)



if __name__ == "__main__":
    main()
