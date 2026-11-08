# -*- coding: utf-8 -*-
"""NapCat（QQ 接入脚本）自动检测与启动

为什么需要这个模块：
    本软件负责"聊天"，但 QQ 的登录与收发消息靠 NapCat 这个独立程序。
    以前用户要自己：装 QQ → 装 NapCat → 配端口 → 手动开。
    这一步劝退了大部分人。现在做成：
      1. 启动时自动探测 3003 端口有没有 NapCat 在跑
      2. 找不到就去找它的安装目录（常见位置 + 用户指定）
      3. 都找不到 → 界面上问一句"要不要我帮你下载安装"

检测策略（config.json 的 napcat_check_mode）：
    install = 只在第一次启动时检测（默认，最省事）
    always  = 每次启动都检测（装好之前想反复确认的用户）
    never   = 完全不检测（老手自己管）

安全约定：
    * 只探测本机端口，绝不外连
    * 下载安装必须经过用户在界面上点同意（napcat_allow_install）
    * 不碰用户的 QQ 账号密码 —— 登录仍在 NapCat 自己的窗口里完成
"""
import base64
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import config as cfg_mod

# v1.8.4：进程清道夫（防孤儿 / 一键清理残留 node）。
# 设计成「可有可无」：模块没打进去也只是少了清理能力，NapCat 照样能起。
try:
	import napcat_proc as _np
except Exception:
	_np = None

# NapCat 的默认端口（NapCat 端默认 3003 HTTP / 3004 WebSocket）
# v1.6.2：默认端口避开 3000/3001 —— 官方 QQ 客户端、很多本地工具的探针
# 都会占这两个端口，用户开着 QQ 时我们连不上、还让他去任务管理器杀进程（实机投诉）。
DEFAULT_HTTP_PORT = 3003
DEFAULT_WS_PORT = 3004

# 常见安装位置（按优先级）
_COMMON_DIRS = [
    r"D:\napcat-fw",
    r"D:\NapCat",
    r"D:\NapCatQQ",
    r"C:\napcat-fw",
    r"C:\NapCat",
    os.path.expanduser(r"~\napcat-fw"),
    os.path.expanduser(r"~\NapCat"),
    os.path.expanduser(r"~\Desktop\napcat-fw"),
    os.path.expanduser(r"~\Downloads\napcat"),
]

# 启动器文件名（不同版本叫法不同）
_LAUNCHERS = ["napiLoader.bat", "napcat.bat", "start.bat", "NapCatWinBootMain.exe",
              "NapCatWinBoot.exe", "napcat.exe"]


# ==================== 端口探测 ====================
def port_open(port=DEFAULT_HTTP_PORT, host="127.0.0.1", timeout=0.6):
    """端口有没有在监听。比 HTTP 请求快，且不受 NapCat 版本影响。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def http_alive(timeout=3):
    """真发一个请求确认"是 NapCat"而不是别的程序占了 3003。

    用 /get_login_info：端口对、但没登录 QQ 时会返回 retcode 非 0，
    端口不对或不是 NapCat 时连不上 / 返回非 JSON。
    """
    base = str(cfg_mod.get("napcat_http") or f"http://127.0.0.1:{DEFAULT_HTTP_PORT}").rstrip("/")
    try:
        req = urllib.request.Request(base + "/get_login_info", method="POST",
                                     data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(4000).decode("utf-8", "replace")
        return {"ok": True, "body": body[:500], "base": base}
    except urllib.error.HTTPError as e:
        # 401/403：端口是 NapCat，但 token 不对 —— 也算"装着且活着"
        return {"ok": e.code in (401, 403), "body": f"HTTP {e.code}", "base": base,
                "auth": True}
    except Exception as e:
        return {"ok": False, "body": f"{type(e).__name__}: {e}", "base": base}


def ws_port_open(timeout=0.6):
    port = DEFAULT_WS_PORT
    try:
        ws = str(cfg_mod.get("napcat_ws") or "")
        if ":" in ws:
            port = int(ws.rsplit(":", 1)[-1].split("/")[0])
    except Exception:
        pass
    return port_open(port, timeout=timeout), port


# ==================== OneBot 配置（v1.1.7 / P1-2）====================
# 为什么要有这一段：
#   以前「下载并安装 NapCat」只是把 zip 解开，什么配置都不写。用户装完之后
#   启动 NapCat、扫码登录，进去发现 3003/3004 一个适配器都没开，本程序连不上，
#   还得自己去 WebUI 里手配一遍 —— 等于白装。
#   现在装完直接生成 config/onebot11.json（模板）和 config/onebot11_<QQ>.json
#   （知道 QQ 时），把 3003 HTTP + 3004 WebSocket 都打开。
#
# 字段是按当前 NapCat（9.9.31 内核 / NapCat 4.x）的实际 schema 抄的：
#   network.{httpServers,httpSseServers,httpClients,websocketServers,
#            websocketClients,plugins} + musicSignUrl/enableLocalFile2Url/
#            parseMultMsg/imageDownloadProxy/timeout
#   少写字段没关系（NapCat 自己会用默认值补齐），多写/写错才会过不了它的校验。
WEBUI_PORT = 6099
WEBHOOK_PORT = 5700  # 小鲸鱼自己的备用事件入口（NapCat 可选往这里推）


def _url_port(url, default):
    """从 http://127.0.0.1:3003 这种串里抠端口，失败用默认值。"""
    try:
        s = str(url or "").strip()
        if not s:
            return int(default)
        tail = s.split("//", 1)[-1].split("/", 1)[0]
        if ":" in tail:
            return int(tail.rsplit(":", 1)[-1])
    except Exception:
        pass
    return int(default)


def onebot_config_dir(install_dir=""):
    """NapCat 真正读配置的目录（找不到安装目录时返回 ""）。

    v1.3.3 修（用户实机踩到）：NapCat Shell 版读的是 `<安装目录>\napcat\config`，
    我们以前把配置写到 `<安装目录>\config` —— 那个目录 NapCat 根本不看。
    结果登录成功后 NapCat 自己生成一份 `onebot11_<QQ>.json`（network 全空），
    HTTP 3003 / WebSocket 3004 一个都不开，界面永远「等扫码」、Agent 永远 10061。
    """
    d = install_dir or find_install_dir()
    if not d:
        return ""
    inner = os.path.join(d, "napcat", "config")
    if os.path.isdir(os.path.join(d, "napcat")):
        return inner
    return os.path.join(d, "config")


def default_onebot_config():
    """本程序需要的 OneBot 配置：3003 HTTP（调 API）+ 3004 WS（收事件）。

    只监听 127.0.0.1：QQ 接入不需要暴露到局域网，少一个被扫的风险。
    不写 httpClients（往 5700 推事件）—— 事件走 WS 已经够了，
    两边同时开会让同一条消息进来两次，回复也重复。
    """
    http_port = _url_port(cfg_mod.get("napcat_http"), DEFAULT_HTTP_PORT)
    ws_port = _url_port(cfg_mod.get("napcat_ws"), DEFAULT_WS_PORT)
    return {
        "network": {
            "httpServers": [{
                "name": "http-server",
                "enable": True,
                "port": http_port,
                "host": "127.0.0.1",
                "enableCors": True,
                "enableWebsocket": False,
                "messagePostFormat": "array",
                "token": "",
                "debug": False,
            }],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [{
                "name": "websocket-server",
                "enable": True,
                "host": "127.0.0.1",
                "port": ws_port,
                "messagePostFormat": "array",
                "reportSelfMessage": False,
                "token": "",
                "enableForcePushEvent": True,
                "debug": False,
                "heartInterval": 30000,
            }],
            "websocketClients": [],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
        "timeout": {
            "baseTimeout": 10000,
            "uploadSpeedKBps": 256,
            "downloadSpeedKBps": 256,
            "maxTimeout": 1800000,
        },
    }


def _enabled_adapters(cfg):
    """数一下配置里已经开着的适配器有几个（用来判断"空配置"）。"""
    net = (cfg or {}).get("network") or {}
    n = 0
    for k in ("httpServers", "httpSseServers", "httpClients",
              "websocketServers", "websocketClients"):
        items = net.get(k) or []
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict) and it.get("enable"):
                n += 1
    return n


def login_info(timeout=3):
    """问一下正在跑的 NapCat：登的是哪个 QQ。没登录/没跑返回 {}。"""
    base = str(cfg_mod.get("napcat_http") or f"http://127.0.0.1:{DEFAULT_HTTP_PORT}").rstrip("/")
    try:
        req = urllib.request.Request(base + "/get_login_info", method="POST",
                                     data=b"{}",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(4000).decode("utf-8", "replace")
        data = __import__("json").loads(body)
        uid = str(((data or {}).get("data") or {}).get("user_id") or "")
        return {"user_id": uid} if uid else {}
    except Exception:
        return {}


def guess_qq(install_dir=""):
    """猜本机的 QQ 号（用于生成 onebot11_<QQ>.json），猜不到返回 ""。

    顺序：正在跑且已登录的 NapCat → 日记里绑定的账号 → 已有的配置文件名。
    """
    uid = str((login_info() or {}).get("user_id") or "").strip()
    if uid.isdigit():
        return uid
    try:
        import diary
        for x in (diary.bound_accounts() or []):
            s = str(x).strip()
            if s.isdigit():
                return s
    except Exception:
        pass
    cdir = onebot_config_dir(install_dir)
    if cdir:
        for p in sorted(glob.glob(os.path.join(cdir, "onebot11_*.json"))):
            s = os.path.basename(p)[len("onebot11_"):-len(".json")]
            if s.isdigit():
                return s
    return ""


def webui_info(install_dir=""):
    """NapCat WebUI 的地址和 token（扫码登录用），读不到就用默认值。"""
    cdir = onebot_config_dir(install_dir)
    host, port, token = "127.0.0.1", WEBUI_PORT, ""
    p = os.path.join(cdir, "webui.json") if cdir else ""
    if p and os.path.isfile(p):
        try:
            cfg = __import__("json").loads(open(p, "r", encoding="utf-8").read())
            port = int(cfg.get("port") or WEBUI_PORT)
            token = str(cfg.get("token") or "")
            h = str(cfg.get("host") or "")
            if h and h not in ("::", "0.0.0.0", "::1"):
                host = h
        except Exception:
            pass
    return {"url": f"http://{host}:{port}/webui", "port": port, "token": token}


def write_onebot_config(install_dir="", qq="", overwrite=False):
    """写 OneBot 配置，返回 {"ok","msg","path","paths","qq","webui","token"}。

    规则（宁可少写，也不砸用户已有配置）：
      * config/onebot11.json —— 永远写（NapCat 登录后会拿它当模板，
        复制成 onebot11_<QQ>.json）
      * config/onebot11_<QQ>.json —— 知道 QQ 时才写，优先写这个
      * 文件已存在：能解析、且里面已经开着别的适配器 → 一律不动；
        是空配置（一个适配器都没开）→ 只补缺失的 network 段，其余字段保留
      * overwrite=True 才真的覆盖（界面上那个「重新写入配置」按钮用）
    """
    d = install_dir or find_install_dir()
    out = {"ok": False, "msg": "", "path": "", "paths": [], "qq": "", "webui": "", "token": ""}
    if not d:
        out["msg"] = "还没找到 NapCat 目录，装好之后再点一次"
        return out
    cdir = onebot_config_dir(d)
    try:
        os.makedirs(cdir, exist_ok=True)
    except Exception as e:
        out["msg"] = f"建 config 目录失败：{e}"
        return out

    qq = str(qq or "").strip() or guess_qq(d)
    out["qq"] = qq if str(qq).isdigit() else ""
    want = default_onebot_config()

    targets = []
    if str(qq).isdigit():
        targets.append(os.path.join(cdir, "onebot11_%s.json" % qq))
    targets.append(os.path.join(cdir, "onebot11.json"))
    # 账号配置（NapCat 登录后自动生成的那些）也要补：它们 network 全空时
    # NapCat 就一个适配器都不开，这是「扫了码还是连不上」的根因。
    try:
        for _p in sorted(glob.glob(os.path.join(cdir, "onebot11_*.json"))):
            if _p not in targets:
                targets.append(_p)
    except Exception:
        pass

    written, merged, kept = [], [], []
    for p in targets:
        cur = None
        if os.path.isfile(p) and not overwrite:
            try:
                cur = __import__("json").loads(open(p, "r", encoding="utf-8").read())
            except Exception:
                kept.append(p)
                continue
            if _enabled_adapters(cur) > 0:
                kept.append(p)
                continue
        try:
            if cur is None:
                payload = want
            else:
                payload = cur
                net = payload.get("network")
                if not isinstance(net, dict):
                    net = {}
                    payload["network"] = net
                for k, v in want["network"].items():
                    if not net.get(k):
                        net[k] = v
                for k, v in want.items():
                    if k != "network" and k not in payload:
                        payload[k] = v
            with open(p, "w", encoding="utf-8") as f:
                __import__("json").dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            out["msg"] = f"写配置失败：{type(e).__name__}: {e}"
            return out
        (merged if cur is not None else written).append(p)

    # ok 的含义是"配置已经可用"：刚写的、补全的、或者本来就配好的都算；
    # 一个能用的文件都没有才返回 False。
    out["ok"] = bool(written or merged or kept)

    out["paths"] = written + merged
    out["path"] = out["paths"][0] if out["paths"] else (kept[0] if kept else "")
    wi = webui_info(d)
    out["webui"], out["token"] = wi["url"], wi["token"]

    parts = []
    if written:
        parts.append("已生成 " + "、".join(os.path.basename(x) for x in written))
    if merged:
        parts.append("已补全空配置 " + "、".join(os.path.basename(x) for x in merged))
    if kept:
        parts.append("已存在且有用，没动 " + "、".join(os.path.basename(x) for x in kept))
    if not parts:
        parts.append("没写任何配置")
    if not str(qq).isdigit():
        parts.append("（还不知道你的 QQ —— 登录后本程序会自动补 onebot11_<QQ>.json）")
    out["msg"] = "；".join(parts)
    return out


def account_uins(install_dir=""):
    """NapCat 登录成功后会写 `onebot11_<QQ>.json`，文件名就是 QQ 号。"""
    cdir = onebot_config_dir(install_dir)
    out = []
    if not cdir:
        return out
    try:
        for p in sorted(glob.glob(os.path.join(cdir, "onebot11_*.json"))):
            s = os.path.basename(p)[len("onebot11_"):-len(".json")]
            if s.isdigit():
                out.append(s)
    except Exception:
        pass
    return out


def quick_login_uins(install_dir=""):
	"""本机能「快速登录」的 QQ 号候选（v1.8.4）。

	NapCat 每次启动都会在日志里打一段「可用于快速登录 of QQ：」+ 编号名单，
	那名单就是本机登录过、本地缓存还在的号 —— 照着它带 `-q <QQ>` 启动就能免扫码。
	以前只认 `onebot11_<QQ>.json`（那是**登录成功之后**才写的），所以第一次永远是空的，
	于是「本机 QQ 已登录点一下就接管」实际从没带上 -q，每次都退回扫码。

	顺序：日志名单（第一个通常是最近在用的号）→ onebot11_<QQ>.json → 猜出来的号。
	"""
	out = []
	p = launch_log_path()
	if p and os.path.isfile(p):
		try:
			with open(p, "rb") as f:
				text = f.read().decode("utf-8", "ignore")
		except Exception:
			text = ""
		idx = text.rfind("可用于快速登录")
		if idx >= 0:
			for line in text[idx:].splitlines()[1:16]:
				m = re.match(r"\s*\d+[.、]\s*(\d{5,12})\b", line)
				if m:
					if m.group(1) not in out:
						out.append(m.group(1))
				elif out:
					break
	try:
		for u in (account_uins(install_dir) or []):
			if u not in out:
				out.append(u)
	except Exception:
		pass
	try:
		g = guess_qq(install_dir)
	except Exception:
		g = ""
	if g and g not in out:
		out.append(g)
	return out


# ==== v1.9.14：登录号选择 / 失效记忆 / 好好关 QQ ====
_BAD_TTL = 1800
# v1.9.18：被官方 QQ 客户端占着的号，记半小时。半小时内不拿它当第一选择。
_CONFLICT_TTL = 1800


def _uin_state_path():
    p = launch_log_path()
    base = os.path.dirname(p) if p else os.getcwd()
    return os.path.join(base, "_uin_state.json")


def uin_state():
    """记下「哪个号上次登上了」「哪个号登录态失效了」。"""
    try:
        with open(_uin_state_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _uin_state_save(st):
    p = _uin_state_path()
    try:
        b = os.path.dirname(p)
        if b:
            os.makedirs(b, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass


def mark_uin_ok(uin):
    """这个号刚刚登录成功 —— 下次优先用它。"""
    u = str(uin or "").strip()
    if not u:
        return
    st = uin_state()
    st.setdefault("ok", {})[u] = int(time.time())
    st.setdefault("bad", {}).pop(u, None)
    st.setdefault("conflict", {}).pop(u, None)
    st["last"] = u
    _uin_state_save(st)


def mark_uin_conflict(uin, why=""):
    """这个号正被本机官方 QQ 客户端占着（v1.9.18）。

    官方 QQ 在线时，NapCat 拿同一个号去快速登录必然被拒（日志：「当前账号(…)已登录,
    无法重复登录」）。以前我们照样把它排在第一个候选、撞一次墙就退回「请你去退出 QQ」，
    用户点多少次都登不上。现在记住这个号被占着，半小时内不再拿它当第一选择 ——
    直接去试另一个号（或者干脆不带 -q 出码，让人扫别的号）。
    """
    u = str(uin or "").strip()
    if not u:
        return
    st = uin_state()
    st.setdefault("conflict", {})[u] = int(time.time())
    if why:
        st.setdefault("conflict_why", {})[u] = str(why)[:120]
    _uin_state_save(st)


def conflict_uins():
    """`{uin: ts}`：最近半小时内被官方 QQ 占着的号。"""
    try:
        st = uin_state()
    except Exception:
        return {}
    c = st.get("conflict") or {}
    now = time.time()
    out = {}
    for u, t in c.items():
        try:
            if now - float(t or 0) <= _CONFLICT_TTL:
                out[str(u)] = t
        except Exception:
            continue
    return out


def mark_uin_bad(uin, why=""):
    """这个号的本地登录态失效了 —— 半小时内别再拿它去撞，越撞越容易被风控。"""
    u = str(uin or "").strip()
    if not u:
        return
    st = uin_state()
    st.setdefault("bad", {})[u] = int(time.time())
    if str(st.get("last") or "") == u:
        st.pop("last", None)
    if why:
        st.setdefault("why", {})[u] = str(why)[:120]
    _uin_state_save(st)


def bootstrap_bad_from_log():
    """新装 / 刚升级上来时 `_uin_state.json` 还是空的。

    NapCat 的「可用于快速登录」名单是按号排的，不按「谁的登录态还有效」，
    所以第一次点「启动 NapCat」很可能又拿个死号去撞，撞出一张二维码 ——
    用户被迫扫码，反复扫码正是被风控的原因。

    这里把日志里已经报过「登录态已失效」的号先记成坏号，让它们排到后面去。
    只跑一次（bad 表非空就跳过）；这个号以后真登上了，mark_uin_ok 会把它清掉。
    """
    try:
        st = uin_state()
    except Exception:
        return []
    if st.get("bad"):
        return []
    p = launch_log_path()
    if not p:
        return []
    try:
        if not os.path.isfile(p):
            return []
        with open(p, "rb") as f:
            text = f.read().decode("utf-8", "ignore")
    except Exception:
        return []
    cur = ""
    dead = []
    for ln in text.splitlines()[-4000:]:
        mm = re.search(r"正在快速登录\s+(\d{5,12})", ln)
        if mm:
            cur = mm.group(1)
            continue
        mm = re.search(r"快速登录错误[：:]\s*(.+)", ln)
        if mm and ("失效" in mm.group(1) or "重新登录" in mm.group(1)):
            if cur and cur not in dead:
                dead.append(cur)
            cur = ""
    for u in dead:
        try:
            mark_uin_bad(u, "日志里报过登录态已失效")
        except Exception:
            pass
    return dead


def login_candidates(install_dir=""):
    """按「最可能一次登上」的顺序给出候选号。
    
    上次登录成功的号 → 本机 QQ 正在用的号 → NapCat 记过的号 → 日志里的快速登录名单。
    登录态失效的号压到后面（30 分钟内不优先），免得每次都拿个死号去撞风控。
    """
    d = install_dir or find_install_dir()
    try:
        bootstrap_bad_from_log()
    except Exception:
        pass
    st = uin_state()
    bad = st.get("bad") or {}
    now = time.time()
    out = []

    def add(u):
        u = str(u or "").strip()
        if u and u.isdigit() and 5 <= len(u) <= 12 and u not in out:
            out.append(u)

    add(st.get("last"))
    try:
        add(guess_qq(d))
    except Exception:
        pass
    for fn in (account_uins, quick_login_uins):
        try:
            for u in fn(d):
                add(u)
        except Exception:
            pass
    def ok(u):
        t = int(bad.get(u) or 0)
        return (not t) or (now - t > _BAD_TTL)

    fresh = [u for u in out if ok(u)]
    # v1.9.18：官方 QQ 开着时，被它占着的号排到最后 —— 拿它去快速登录必然撞「已登录」。
    # 只在 QQ 真的开着时才这样排：QQ 退了之后这些号可能正好是最省事的那个。
    _clash = set()
    try:
        if qq_client_procs():
            _clash = set(conflict_uins())
            # v1.9.25：日志里那条「已登录,无法重复登录」未必出现（实测 5 次启动一次都没有），
            # 所以再加一条硬依据 —— 看官方 QQ 的数据目录还在写哪几个号（=它正用着）。
            # 被它正用着的号排到最后：拿同一个号去快速登录，Worker 刚登上就被踢。
            try:
                _clash |= set(qq_client_uins())
            except Exception:
                pass
    except Exception:
        _clash = set()
    if _clash:
        fresh = [u for u in fresh if u not in _clash] + [u for u in fresh if u in _clash]
        _tail = [u for u in out if (u not in fresh)]
        return fresh + _tail
    # 失效号压在后面，但仍然保留 —— 万一它自己恢复了还能试一次
    return fresh + [u for u in out if u not in fresh]

def detect_login(install_dir="", timeout=3):
    """判断「到底登没登进去」。返回 {logged_in, user_id, via, msg}。

    三条依据，从硬到软：
      1. HTTP 3003 通 + get_login_info 有 QQ 号 → 真登录了、接口也活着；
      2. 有 `onebot11_<QQ>.json` 且进程在跑 → NapCat 登过（登录成功才会写这个文件），
         但 OneBot 接口没开起来（配置是空的 / 写错目录）—— 需要补配置重启；
      3. 其余 → 未登录。
    """
    d = install_dir or find_install_dir()
    out = {"logged_in": False, "user_id": "", "via": "", "msg": "", "dir": d}
    li = login_info(timeout=timeout) or {}
    if li.get("user_id"):
        out.update(logged_in=True, user_id=str(li["user_id"]), via="http",
                   msg="已登录：QQ %s（接口正常）" % li["user_id"])
        return out
    uins = account_uins(d)
    try:
        pids = running_pids(d)
    except Exception:
        pids = []
    if uins and pids:
        out.update(logged_in=True, user_id=uins[0], via="config",
                   msg="NapCat 已登录（QQ %s），但 OneBot 的 HTTP/WS 没开起来 —— 配置是空的" % uins[0])
    elif uins:
        out.update(logged_in=False, user_id=uins[0], via="config-dead",
                   msg="这个账号（QQ %s）以前登录过，但 NapCat 现在没在跑" % uins[0])
    else:
        out.update(msg="还没登录：进程在不在？点「扫码登录」用手机 QQ 扫一下")
    return out


def detect_and_enable(install_dir="", wait=8.0):
    """「检测 QQ 登录」按钮：登了就保证 NapCat 的 3003/3004 开起来。

    流程：检测登录 → 已登录但接口没开 → 补齐 onebot 配置 → 重启 NapCat → 复探。
    """
    d = install_dir or find_install_dir()
    out = {"ok": False, "logged_in": False, "state": "", "msg": "", "fixed": False,
           "user_id": "", "restarted": False, "config": {}, "probe": {}}
    info = detect_login(d)
    out["user_id"] = info.get("user_id", "")
    out["logged_in"] = bool(info.get("logged_in"))
    if not d:
        out["msg"] = "还没找到 NapCat 目录"
        return out
    if not info.get("logged_in"):
        out["msg"] = info.get("msg") or "还没登录"
        out["state"] = (probe().get("state") or "")
        return out
    if info.get("via") == "http":
        p = probe()
        out.update(ok=True, state=p.get("state", ""), msg=str(info.get("msg")),
                   probe=p)
        return out

    cfg = write_onebot_config(d, qq=info.get("user_id", "")) or {}
    out["config"] = cfg
    out["fixed"] = bool(cfg.get("ok"))
    out["msg"] = ("NapCat 其实已经登录了（QQ %s），是 OneBot 配置没写对位置，已补齐：%s。"
                  % (info.get("user_id", ""), cfg.get("msg") or "") +
                  "正在重启 NapCat 让它生效…")
    try:
        launch(d, restart=True)
        out["restarted"] = True
    except Exception as e:
        out["msg"] += " 重启失败：%s: %s" % (type(e).__name__, e)
    t0 = time.time()
    while time.time() - t0 < wait:
        if port_open(DEFAULT_HTTP_PORT):
            break
        time.sleep(0.5)
    p = probe()
    out["probe"] = p
    out["state"] = p.get("state", "")
    out["ok"] = bool(p.get("running"))
    if out["ok"]:
        out["msg"] = "好了：QQ %s 已登录，HTTP %s、WebSocket %s 都通了。" % (
            info.get("user_id", ""), DEFAULT_HTTP_PORT, p.get("ws_port") or 3004)
    else:
        out["msg"] = ("已经补齐配置并重启了 NapCat，但 %s 还没通（当前状态：%s）。"
                      "等 10 秒再点一次「检测 QQ 登录」；还不行就去 NapCat 的 WebUI → 网络配置看一眼。"
                      % (DEFAULT_HTTP_PORT, p.get("state") or "未知"))
    return out


# ==================== 找安装目录 ====================
def _looks_like_napcat(path):
    if not path or not os.path.isdir(path):
        return False
    try:
        names = set(os.listdir(path))
    except OSError:
        return False
    return any(n in names for n in _LAUNCHERS)


def own_install_candidates():
    """本程序自己会装的几个位置（数据目录优先，其次是 exe 旁边）。

    安装包把 NapCat 解到 <数据目录>\runtime\napcat；绿色版数据目录就是程序目录，
    所以两个都要看。返回去重后的目录列表（不保证存在）。
    """
    out = []
    try:
        base = str(getattr(cfg_mod, "DATA_DIR", "") or "")
    except Exception:
        base = ""
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    except Exception:
        exe_dir = ""
    for b in (base, exe_dir):
        if not b:
            continue
        for sub in (os.path.join("runtime", "napcat"), "napcat"):
            p = os.path.join(b, sub)
            if p not in out:
                out.append(p)
    return out


def find_install_dir():
    """按优先级找 NapCat 安装目录，找不到返回 ""。"""
    # ① 用户显式配置的
    user_dir = str(cfg_mod.get("napcat_dir") or "").strip()
    if user_dir and _looks_like_napcat(user_dir):
        return user_dir

    # ①.5 本程序自己装的（安装包/绿色版都放在 <数据目录>\runtime\napcat）
    #      以前漏了这一条：绿色版 config.json 里 napcat_dir 是空串，
    #      于是 find_install_dir() 返回 ""，launch("") 直接抛 WinError 123，
    #      界面显示「没装 NapCat」—— 明明包就在旁边。
    for d in own_install_candidates():
        if _looks_like_napcat(d):
            return d

    # ② 常见位置
    for d in _COMMON_DIRS:
        if _looks_like_napcat(d):
            return d

    # ③ 在几个盘符根目录浅扫（只扫一层，避免卡死）
    for drive in ("D:", "C:", "E:"):
        try:
            for name in os.listdir(drive + os.sep):
                low = name.lower()
                if "napcat" in low:
                    p = os.path.join(drive + os.sep, name)
                    if _looks_like_napcat(p):
                        return p
        except OSError:
            continue

    # ④ 桌面 / 下载里的 zip 解压目录
    for base in (os.path.expanduser("~/Desktop"), os.path.expanduser("~/Downloads")):
        try:
            for name in os.listdir(base):
                if "napcat" in name.lower():
                    p = os.path.join(base, name)
                    if _looks_like_napcat(p):
                        return p
        except OSError:
            continue
    return ""


def find_launcher(install_dir=""):
    """在安装目录里找启动器，返回完整路径或 ""。"""
    d = install_dir or find_install_dir()
    if not d:
        return ""
    # 优先 .bat（能带工作目录），其次 exe
    for name in _LAUNCHERS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    try:
        for name in sorted(os.listdir(d)):
            if name.lower().endswith((".bat", ".exe")) and "napcat" in name.lower():
                return os.path.join(d, name)
    except OSError:
        pass
    return ""


# ==================== 状态 ====================
def _proc_age_sec(pid):
    """进程已经活了多久（秒）。查不到返回 -1（调用方要当成「不知道」，别猜）。"""
    try:
        import psutil
        return max(0.0, time.time() - psutil.Process(int(pid)).create_time())
    except Exception:
        pass
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue).StartTime"],
            capture_output=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = (ps.stdout or b"").decode("utf-8", "replace").strip()
        if not raw:
            return -1
        import datetime as _dt
        for fmt in ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                t = _dt.datetime.strptime(raw.split(".")[0].strip(), fmt).timestamp()
                return max(0.0, time.time() - t)
            except Exception:
                continue
    except Exception:
        pass
    return -1


def _qr_fresh(install_dir="", fresh_sec=180):
    """刚生成的二维码才有意义：返回路径，太旧/没有就返回 ""。"""
    p = qr_path(install_dir)
    if not p:
        return ""
    try:
        if time.time() - os.path.getmtime(p) <= float(fresh_sec):
            return p
    except OSError:
        return ""
    return ""


def stale_pids(install_dir="", min_age=90.0, qr_grace=900.0):
    """判成「假死」的 NapCat 进程。

    四个条件全中才算（宁可放过，也不误杀正在扫码的那个）：
      ① 命令行确实是 NapCat（running_pids 已保证）；
      ② 3003（HTTP）没开 —— 它压根没在提供服务；
      ③ 没有「新鲜的二维码」—— 没二维码说明根本没走到登录页；
      ④ 进程活过 min_age 秒 —— 别把正在启动的当死的。
    在等扫码的正常实例一定有一张刚生成的 qrcode.png，所以不会被清掉。
    """
    d = install_dir or find_install_dir()
    pids = running_pids(d)
    if not pids:
        return []
    if port_open(DEFAULT_HTTP_PORT):
        return []
    if _qr_fresh(d, qr_grace):
        return []
    dead = []
    for pid in pids:
        age = _proc_age_sec(pid)
        if age >= 0 and age >= float(min_age):
            dead.append(pid)
    return dead


def probe(deep=True, timeout=0.1):
    """一次探完整的 NapCat 状态，给界面/启动自检用。

    v1.3.3 改（用户实机踩坑）：以前「进程在」就含糊地说「已在跑」，
    可他实机是「进程在、3003/3004 全不通」—— 说「在跑」等于骗人。
    现在把「端口通不通」和「进程在不在」分开报，并给出 state 供界面分档显示。

    state 取值：
      running         HTTP 3003 通 + WS 通，真在服务
      no_ws           HTTP 通但 WebSocket 连不上（收不到 QQ 消息就是这个）
      port_taken      3003 被别的程序占了，而且不像 NapCat
      zombie          进程在，但端口全关、也没二维码 = 假死（可自动清理）
      waiting_scan    进程在 + 有新鲜二维码 = 正常等扫码
      process_no_port 进程在，端口全关、没二维码，但刚起来（再等等）
      not_running     装了但没跑
      not_installed   没装

    返回 {
      ok, state, running, http_ok, http_port, ws_ok, ws_port,
      pids, proc_count, stale, qr, qr_fresh, dir, launcher, mode,
      allow_install, msg
    }
    """
    # v1.8.7（QA P0）：本地回环的端口探测 0.6s 太奢侈 —— 没装 NapCat 时
    # /api/health 里两个探测就吃掉 1.2s。0.1s 对 127.0.0.1 完全够用。
    mode = str(cfg_mod.get("napcat_check_mode") or "install").strip().lower()
    allow_install = bool(cfg_mod.get("napcat_allow_install", True))

    http_up = port_open(DEFAULT_HTTP_PORT, timeout=timeout)
    ws_up, ws_port = ws_port_open(timeout)
    detail = http_alive() if (deep and http_up) else {"ok": http_up, "body": ""}
    running = bool(http_up and detail.get("ok"))

    d = find_install_dir()
    launcher = find_launcher(d)
    shell = is_shell_build(d) if d else False
    qq_up = qq_running() if (d and not running) else False
    try:
        pids = running_pids(d)
    except Exception:
        pids = []
    qr_now = qr_path(d) if d else ""
    qr_new = _qr_fresh(d) if d else ""
    try:
        stale = stale_pids(d) if (pids and not http_up) else []
    except Exception:
        stale = []
    pid_txt = "、".join(str(p) for p in pids) if pids else ""

    # v1.3.3 补丁：端口「通」不代表是 NapCat 在服务 —— 别的程序（比如 QA 的 mock）
    # 占着 3003 也会回 200，界面就会说「已经在跑了，3003 端口也通」，用户怎么点都登不上。
    owner = {}
    if http_up:
        try:
            owner = port_owner(DEFAULT_HTTP_PORT)
        except Exception:
            owner = {}
    # 注意：不能加 "and not pids" —— NapCat 进程在、3003 却被别的程序占着，
    # 正是最该报「端口被占」的情况（实机就是这样：进程起来了但绑不上端口）。
    foreign = bool(owner) and not owner.get("mine")
    if foreign:
        running = False

    if foreign:
        state = "port_taken"
        o_name = owner.get("name") or "未知程序"
        o_path = owner.get("exe") or owner.get("cmd") or ""
        # v1.6.2：不再指挥用户去任务管理器杀进程（实机投诉「那弄这个的意义在哪」）。
        # 占了我们端口的可能是用户正开着的软件，绝不自动关；只如实说清并给出退路。
        msg = (f"{DEFAULT_HTTP_PORT} 端口现在被另一个程序占着（{o_name}）。\n"
               + (f"它是：{o_path}\n" if o_path else "")
               + "我不动它 —— 那可能是你正在用的软件。如果它自己会关，过会儿点「重新检测」就好；\n"
               + "要马上用，也可以把这个软件关掉，或者把 NapCat 的 HTTP/WS 端口改到别的号。")
    elif running and ws_up:
        state = "running"
        msg = (f"NapCat 正在运行：HTTP {DEFAULT_HTTP_PORT} 通，"
               f"WebSocket {ws_port} 也通，QQ 接入正常")
    elif running and not ws_up:
        state = "no_ws"
        msg = (f"NapCat 在跑、HTTP {DEFAULT_HTTP_PORT} 通，但 WebSocket {ws_port} 连不上 —— "
               "QQ 消息收不到就是这个原因。去 NapCat 的 WebUI →「网络配置 → WebSocket 服务端」"
               f"确认已启用、端口是 {ws_port}，改完点「重新检测」。")
    elif http_up:
        state = "port_taken"
        msg = (f"{DEFAULT_HTTP_PORT} 端口上有别的程序在应答，但它不像 NapCat。\n"
               "我不动它；过会儿点「重新检测」，或者把 NapCat 的端口改成别的号。")
    elif pids and stale:
        state = "zombie"
        dead_txt = "、".join(str(p) for p in stale) or pid_txt
        msg = (f"NapCat 进程假死（PID {dead_txt}）：{DEFAULT_HTTP_PORT}/{ws_port} 都没开、"
               "也没生成二维码，连不上不是你的错。\n"
               "点「重新检测」会自动清掉它并重开一个。")
    elif pids and qr_now:
        state = "waiting_scan"
        msg = ("NapCat 已启动，正在等你扫码登录（没登录前 3003/3004 不会开，这是正常的）。\n"
               + (f"二维码：{qr_now}" if qr_new else
                  f"二维码：{qr_now}（这张是旧的，可能已过期，点「重新检测」会重开一个）"))
    elif pids:
        state = "process_no_port"
        msg = (f"NapCat 进程在（PID {pid_txt}），但 {DEFAULT_HTTP_PORT}/{ws_port} 还没开、"
               "也还没出二维码 —— 可能刚起来。等 10 秒点「重新检测」；"
               "一直这样就点「重新检测」，它会清掉重开。")
    elif d and shell:
        state = "not_running"
        msg = "找到 NapCat（自带 QQ 内核的 Shell 版），但没在运行，点『启动 NapCat』即可"
    elif d:
        state = "not_running"
        msg = f"找到 NapCat，但没在运行：{d}"
    else:
        state = "not_installed"
        msg = "没找到 NapCat（QQ 接入脚本），装了才能连 QQ"

    # ---- v1.4.1：本机 QQ 客户端占着号 / 账号被风控 ----
    last_err = {}
    qq_cli = []
    try:
        last_err = launch_log_errors()
    except Exception:
        last_err = {}
    try:
        qq_cli = qq_client_procs()
    except Exception:
        qq_cli = []
    if not running and not foreign:
        _ec = str((last_err or {}).get("code") or "")
        _uin = str((last_err or {}).get("uin") or "")
        if _ec == "risk":
            state = "risk_limited"
            msg = ("你的 QQ 号被腾讯风控了（错误码 168：「账号近期存在安全风险，部分功能使用受限」）。\n"
                   "先在手机 QQ 上按提示做完安全验证；恢复之前扫码和快速登录都会失败 —— "
                   "先别反复重试，越试锁得越久。")
        elif _ec == "qq_conflict":
            # v1.9.18：不再把「QQ 占着号」说成必须接管（那会掉线）。
            # 现在启动时会自动跳过被占着的号，只有绕不开时才提示用户二选一。
            state = "not_running"
            msg = (f"官方 QQ 正开着{('（QQ ' + _uin + '）') if _uin else ''}，占着一个本地登录号。\n"
                   "点『启动 NapCat』会自动挑另一个没被占着的号直接登录；"
                   "都占着就直接出二维码，扫另一个号就行。")
        elif qq_cli and state in ("not_running", "waiting_scan", "process_no_port"):
            state = "not_running"
            msg = ("找到 NapCat，没在运行 —— 你的官方 QQ 正开着。\n"
                   "点『启动 NapCat』会自动挑一个没被它占着的号登录，不用退 QQ；"
                   "实在绕不开才需要二选一（弹窗里会给你按钮）。")

    return {
        "ok": True,
        "state": state,
        "running": running,
        "http_ok": http_up,
        "http_port": DEFAULT_HTTP_PORT,
        "http_detail": detail.get("body", "")[:300],
        "ws_ok": ws_up,
        "ws_port": ws_port,
        "pids": pids,
        "proc_count": len(pids),
        "stale": stale,
        "dir": d,
        "launcher": launcher,
        "mode": mode,
        "allow_install": allow_install,
        "msg": msg,
        "shell_build": shell,
        "qq_running": qq_up,
        # 内嵌版才需要「强制再开一个 QQ」的二次确认；Shell 版用不着
        "can_force": bool(d and not shell),
        "launch_log": launch_log_path(),
        "qr": qr_now,
        "qr_fresh": qr_new,
        "port_owner": owner,
        "qq_client": qq_cli,
        "last_login_error": last_err,
    }


def recheck(install_dir="", clean_stale=True):
    """「重新检测」按钮走这里：先探一遍，发现假死/重复进程就清掉，再探一遍。

    用户要求（实机反馈）：「重新检测」得能自己把假死的 node.exe 收掉，
    不然他得自己开任务管理器。清理只针对 stale_pids() 那四个条件全中的进程，
    正在等扫码的实例（有新鲜二维码）不会被碰。
    """
    d = install_dir or find_install_dir()
    try:
        first = probe()
    except Exception as e:
        return {"ok": False, "state": "error",
                "msg": f"检测失败：{type(e).__name__}: {e}"}
    killed, kmsg = 0, ""
    if clean_stale:
        try:
            stale = stale_pids(d)
            dup = len(running_pids(d)) > 2
            if stale or dup:
                killed, kmsg = stop_all(d)
        except Exception as e:
            kmsg = f"{type(e).__name__}: {e}"
    try:
        if killed or first.get("state") in ("zombie", "process_no_port"):
            out = dict(probe())
        else:
            out = dict(first)
    except Exception:
        out = dict(first)
    out["cleaned"] = killed
    out["clean_msg"] = kmsg
    out["previous_state"] = first.get("state", "")
    if killed:
        out["msg"] = (f"清掉了 {killed} 个假死/重复的 NapCat 进程（{kmsg}）。\n"
                      + str(out.get("msg") or ""))
    elif first.get("state") == "port_taken":
        out["msg"] = (str(first.get("msg") or "") +
                      "\n（没有自动去关它 —— 那可能是你正开着的软件，关不关你决定。）")
    elif first.get("state") in ("zombie", "process_no_port"):
        out["msg"] = (str(first.get("msg") or "") +
                      "\n（没敢自动清：①②③④ 四条没全中。可以把上面的 PID 手动结束掉再点「启动 NapCat」。）")
    return out


def should_check_on_start():
    """本次启动要不要做 NapCat 检测（按 napcat_check_mode）。"""
    mode = str(cfg_mod.get("napcat_check_mode") or "install").strip().lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    # install：只看"是不是第一次" —— 用 config 里的标记
    try:
        if cfg_mod.get("napcat_checked_once", False):
            return False
    except Exception:
        pass
    return True


def mark_checked():
    """记下"已经检测过了"，install 模式以后就不再多问。"""
    try:
        cfg_mod.set_checked("napcat_checked_once", True)
    except Exception:
        pass


# ==================== 启动 ====================
def qq_running():
    """QQ.exe 是不是已经开着了（v1.1.2：一键启动前先看一眼，别把 QQ 开成两个）。"""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq QQ.exe"],
                             capture_output=True, text=True, timeout=8,
                             creationflags=flags).stdout or ""
        return "QQ.exe" in out
    except Exception:
        return False


# ==================== v1.4.1：本机 QQ 客户端占着号 ====================
# 用户实机踩到：他自己开着 QQ 客户端（同一个号），NapCat 走 -q 快速登录直接被
# 「当前账号(…)已登录,无法重复登录」拒掉，退回扫码又被风控码 168 拦下 ——
# 界面永远「等你扫码」，怎么扫都白搭，越扫码越容易被风控。
# 这里负责三件事：认出「那个 QQ.exe 不是 NapCat 生的」、从启动日志读出这两类错误、
# 以及帮用户先把 QQ 客户端关掉（再快速登录，不扫码）。
def _cim_procs(name):
    """用 CIM 拿进程（pid / 父 pid / 路径 / 命令行）。失败返回 []。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
          "Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine | "
          "ConvertTo-Json -Compress" % name)
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = (r.stdout or b"").decode("utf-8", "ignore").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        out = []
        for it in data:
            out.append({"pid": int(it.get("ProcessId") or 0),
                        "ppid": int(it.get("ParentProcessId") or 0),
                        "exe": it.get("ExecutablePath") or "",
                        "cmd": it.get("CommandLine") or ""})
        return out
    except Exception:
        return []


def qq_client_procs():
    """本机 QQ 客户端进程（排除 NapCat 自己拉起来的那个 QQ 内核）。"""
    try:
        np_pids = set(int(r.get("pid") or 0) for r in napcat_procs())
    except Exception:
        np_pids = set()
    out = []
    for it in _cim_procs("QQ.exe"):
        if it.get("ppid") in np_pids:
            continue                       # NapCat 自己的 QQ 内核，不算「用户开着 QQ」
        if "napcat" in (it.get("cmd") or "").lower():
            continue
        out.append(it)
    return out


_QQ_ACTIVE_CACHE = {"t": 0.0, "uins": []}


def _documents_dirs():
    """可能的「文档」目录（官方 QQ 的 Tencent Files 就在下面）。

    v1.9.25：**必须问注册表**。用户这台机器的「文档」被移到 `D:\\Documents` 了，
    而 `~\\Documents` / `~\\OneDrive\\文档` 都不存在 —— 只猜路径会一个都找不到，
    于是「哪个号被官方 QQ 占着」永远推不出来（就是本次 bug 的根因之一）。
    注册表 `HKCU\\...\\User Shell Folders` 的 `Personal` 才是权威位置。
    """
    cands = []
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        for name in ("Personal", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"):
            try:
                v, _t = winreg.QueryValueEx(key, name)
                if v:
                    cands.append(os.path.expandvars(str(v)))
            except Exception:
                continue
        try:
            winreg.CloseKey(key)
        except Exception:
            pass
    except Exception:
        pass
    home = os.path.expanduser("~")
    cands += [
        os.path.join(home, "Documents"),
        os.path.join(home, "OneDrive", "Documents"),
        os.path.join(home, "OneDrive", "文档"),
        os.path.join(home, "文档"),
    ]
    try:
        up = os.environ.get("USERPROFILE") or ""
        if up:
            cands.append(os.path.join(up, "Documents"))
    except Exception:
        pass
    out = []
    for c in cands:
        if c and os.path.isdir(c) and c not in out:
            out.append(c)
    return out


def qq_client_uins(max_age=1800, use_cache=True):
    """官方 QQ 客户端**正在用**哪几个号（v1.9.25）。

    用户实机踩到：官方 QQ 登着某个号开着，我们拿同一个号去 `-q` 快速登录，
    NapCat 日志里「Worker进程已登录成功」紧接着就是「Worker进程退出，退出码: 1」
    —— 号被官方 QQ 顶下线了，3003 端口刚起来又没。更要命的是这种情形
    **日志里不一定有**「当前账号(…)已登录,无法重复登录」那行（实测 5 次启动一次都没有），
    所以只靠日志认不出「哪个号被占着」。

    这里换个更硬的依据：官方 QQ 只要在某个号上跑着，就会不停写
    `文档\\Tencent Files\\<QQ>\\nt_qq\\nt_db\\*.db-wal`。所以「数据目录最近还在被写」
    就等于「这个号官方 QQ 正在用」。

    最多扫 max_age 秒内还在写的那几个号。默认缓存 30 秒 —— 这个函数会被
    login_candidates() 和 launch() 各调一次，别把磁盘扫两遍。
    """
    now = time.time()
    if use_cache and (now - float(_QQ_ACTIVE_CACHE.get("t") or 0)) < 30:
        return list(_QQ_ACTIVE_CACHE.get("uins") or [])
    out = []
    try:
        root = ""
        for doc in _documents_dirs():
            p = os.path.join(doc, "Tencent Files")
            if os.path.isdir(p):
                root = p
                break
        if root:
            for name in os.listdir(root):
                u = str(name or "").strip()
                if not (u.isdigit() and 5 <= len(u) <= 12):
                    continue
                d = os.path.join(root, u)
                if not os.path.isdir(d):
                    continue
                newest = _newest_mtime(d)
                if newest and (now - newest) <= float(max_age):
                    out.append(u)
    except Exception:
        out = out or []
    _QQ_ACTIVE_CACHE["t"] = now
    _QQ_ACTIVE_CACHE["uins"] = list(out)
    return out


def _newest_mtime(d, budget=400):
    """目录里最近被写过的时间（只看最热的几个子目录，别把整个 QQ 目录扫一遍）。

    官方 QQ 的号目录动辄几万个文件，全量 walk 会卡住界面。这里只探
    `nt_qq\\nt_db`（登录态就落在这儿的 sqlite 上）和目录本身，够用了。
    """
    best = 0.0
    try:
        best = float(os.path.getmtime(d))
    except OSError:
        best = 0.0
    for rel in (os.path.join("nt_qq", "nt_db"), "nt_db", "nt_qq"):
        p = os.path.join(d, rel)
        if not os.path.isdir(p):
            continue
        try:
            best = max(best, float(os.path.getmtime(p)))
        except OSError:
            pass
        try:
            for n in os.listdir(p)[:200]:
                fp = os.path.join(p, n)
                if not n.endswith((".db-wal", ".db-shm", ".db")):
                    continue
                try:
                    best = max(best, float(os.path.getmtime(fp)))
                except OSError:
                    pass
        except OSError:
            pass
    return best


def _log_size():
    """启动日志现在多大（用来只扫「本次启动之后」写出来的内容）。"""
    p = launch_log_path()
    if not p:
        return 0
    try:
        return int(os.path.getsize(p))
    except OSError:
        return 0


def launch_log_errors(lines=800, after=None):
    """从启动日志里读「最近一次」登录错误。

    v1.9.25（用户实机踩到，这次是主因）：以前这里**无条件读日志尾部 800 行**，
    也就是把「过去几天的历史」当成了「刚刚发生的事」。用户机器上尾部 800 行里
    躺着 11-05 的一条旧 `token_dead`（`登录态已失效，请重新登录。`），于是：
      ① 等待循环第一圈就拿到它 → 立刻 break → 对外报「20 秒内 3003 端口没起来」，
         可日志里明明写着 3003 已经 `Start On 127.0.0.1:3003`；
      ② 顺手把 3280735835 记进 `bad`（它这轮根本没被试过）；
      ③ 同一个窗口里真正有用的 `qq_conflict` 因为「只返回最后一条」被丢掉。
    现在支持 `after=<字节偏移>`：只看这个位置之后写出来的日志，即**本次启动**。

    返回 {} 或 {"code": …, "uin": "…", "msg": "…", "raw": "…"}
      qq_conflict    = 本机 QQ 客户端已经登录同一个号，NapCat 无法重复登录
      account_kicked = 快速登录**成功了**，可紧接着 Worker 就被踢下线（退出码 1）。
                       多半也是被官方 QQ 顶掉的 —— 同一个号不能两边同时在线。
                       「已登录,无法重复登录」只是撞号的一种表现，这是另一种更安静的形态。
      risk           = 腾讯风控（serverErrorCode 168），要用手机 QQ 做安全验证
      token_dead     = 这个号的本地登录态失效了，要重新登录一次
    """
    p = launch_log_path()
    if not p or not os.path.isfile(p):
        return {}
    try:
        with open(p, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8", "ignore")
    except Exception:
        return {}
    if after is not None:
        try:
            text = raw[int(after):].decode("utf-8", "ignore")
        except Exception:
            text = ""
    else:
        text = "\n".join(text.splitlines()[-int(lines):])
    tail = text
    hits = []
    for m in re.finditer(r"当前账号\((\d{5,12})\)\s*已登录[，,]?\s*无法重复登录", tail):
        hits.append((m.start(), {"code": "qq_conflict", "uin": m.group(1),
                                 "msg": "本机 QQ 客户端已经登录了这个号，NapCat 无法重复登录",
                                 "raw": m.group(0)[:200]}))
    for m in re.finditer(r'serverErrorCode\"\s*:\s*168', tail):
        seg = tail[max(0, m.start() - 200): m.start() + 260]
        um = re.search(r'\"account\"\s*:\s*\"(\d{5,12})\"', seg)
        hits.append((m.start(), {"code": "risk", "uin": (um.group(1) if um else ""),
                                 "msg": "账号被腾讯风控（错误码 168）",
                                 "raw": seg.strip()[:300]}))
    for m in re.finditer(r"快速登录错误[：:]\s*(.+)", tail):
        msg = m.group(1).strip()
        if ("失效" in msg) or ("重新登录" in msg):
            seg = tail[max(0, m.start() - 400): m.start()]
            um = re.findall(r"正在快速登录\s+(\d{5,12})", seg)
            hits.append((m.start(), {"code": "token_dead",
                "uin": (um[-1] if um else ""),
                "msg": "这个号的本地登录态失效了，要重新登录一次",
                "raw": msg[:200]}))
    # v1.9.25：「登进去了又被踢」—— 只看「最近一次快速登录」之后是不是先成功再退。
    # 必须成对出现才算撞号：光有 Worker 退出（比如第一次没带 -q、或配置写错）不算。
    for m in re.finditer(r"Worker进程退出[，,]?\s*退出码[：:]?\s*(\d+)", tail):
        seg = tail[:m.start()]
        i_login = seg.rfind("正在快速登录")
        after_login = seg[i_login:] if i_login >= 0 else seg
        if "Worker进程已登录成功" not in after_login:
            continue
        um = re.findall(r"正在快速登录\s+(\d{5,12})", after_login)
        hits.append((m.start(), {"code": "account_kicked",
            "uin": (um[-1] if um else ""),
            "msg": "这个号刚快速登录成功就被顶下线了（多半官方 QQ 正用它）",
            "raw": tail[max(0, m.start() - 260): m.start() + 200].strip()[:300]}))
    if not hits:
        return {}
    hits.sort(key=lambda x: x[0])
    return hits[-1][1]


def quit_qq_client(wait=6.0, force=False):
    """把本机 QQ 客户端关掉（只关用户自己开的那些）。返回 (关掉个数, 说明)。
    
    v1.9.14：**先好好说，再强杀**。以前上来就 taskkill /F，QQ 来不及把登录态落盘，
    NapCat 随后快速登录就回「登录态已失效，请重新登录」→ 只能扫码 → 反复登录被风控。
    现在先发一次正常退出（taskkill 不带 /F，等于点窗口的 ✕），等它自己退；
    只有 force=True（用户明确点「这次帮我退出 QQ」）且等不到时，才补 /F。
    """
    procs = qq_client_procs()
    if not procs:
        return 0, "没发现开着的 QQ 客户端"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    pids = [int(it.get("pid") or 0) for it in procs if it.get("pid")]
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                capture_output=True, timeout=15, creationflags=flags)
        except Exception:
            pass
    deadline = time.time() + float(max(3.0, min(wait, 8.0)))
    while time.time() < deadline:
        if not qq_client_procs():
            break
        time.sleep(0.4)
    hard = 0
    if qq_client_procs() and force:
        for it in qq_client_procs():
            pid = int(it.get("pid") or 0)
            if not pid:
                continue
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=15, creationflags=flags)
                hard += 1
            except Exception:
                pass
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not qq_client_procs():
                break
            time.sleep(0.4)
    left = qq_client_procs()
    killed = max(0, len(pids) - len(left))
    if left:
        return killed, ("关掉了 %d 个 QQ 进程，但还剩 %d 个没退（多半在托盘里）——"
            "右键托盘图标选「退出」再点一次" % (killed, len(left)))
    tail = "（正常的，登录态还在）" if not hard else "（其中 %d 个是强制结束的）" % hard
    return killed, "QQ 客户端已退出（%d 个进程）%s" % (killed, tail)


def launch_log_path():
    """启动日志放数据目录，出问题直接让用户打开看。"""
    try:
        return os.path.join(str(cfg_mod.DATA_DIR), "napcat_launch.log")
    except Exception:
        return ""


def launch_log_tail(lines=12):
    p = launch_log_path()
    if not p:
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            buf = f.readlines()[-int(lines):]
        return "".join(buf).strip()
    except Exception:
        return ""


def qr_path(install_dir=""):
    """NapCat 登录二维码图片（没出二维码就返回 ""）。"""
    d = install_dir or find_install_dir()
    if not d:
        return ""
    for rel in (os.path.join("napcat", "cache", "qrcode.png"),
                os.path.join("cache", "qrcode.png"), "qrcode.png"):
        p = os.path.join(d, rel)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 64:
                return p
        except OSError:
            pass
    return ""


def qr_png(install_dir="", max_bytes=3 * 1024 * 1024):
    """二维码图片的「给界面用」形态（v1.9.17）。

    NapCat 只把二维码写成 cache\\qrcode.png，WebUI 接口给的是一串 URL，不是图。
    以前我们是 os.startfile 让系统看图程序打开这张 png —— 用户看不出它过没过期。
    现在读成 base64 直接画在我们自己的弹窗里，同时给出：

        mtime  图片最后一次被改写的时间（NapCat 换码会重写这个文件）
        age    距今多少秒 —— 界面拿它做倒计时，过期就自动换
        sha1   内容指纹 —— 前端发现变了就换图，没变就不重绘（不闪）

    返回 {} 表示「还没有二维码」。
    """
    p = qr_path(install_dir)
    if not p:
        return {}
    try:
        st = os.stat(p)
        size = int(st.st_size)
        if size <= 64 or size > max_bytes:
            return {}
        with open(p, "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    try:
        b64 = base64.b64encode(raw).decode("ascii")
    except Exception:
        return {}
    now = time.time()
    age = max(0, int(now - st.st_mtime))
    return {
        "path": p,
        "size": size,
        "mtime": int(st.st_mtime),
        "age": age,
        "sha1": hashlib.sha1(raw).hexdigest()[:16],
        "b64": b64,
        "data_url": "data:image/png;base64," + b64,
    }


def _bundled_node(d):
    """Shell 包里自带的 node.exe（有它就不用管系统装没装 Node）。"""
    for rel in ("node.exe", os.path.join("nodejs", "node.exe"),
                os.path.join("runtime", "node.exe"), os.path.join("napcat", "node.exe")):
        p = os.path.join(d, rel)
        if os.path.isfile(p):
            return p
    return ""


def _napcat_script(d):
    """NapCat 的入口脚本。

    V1.3.0 修正：Shell 包里正确的入口是根目录的 index.js ——
    它会先把 NAPCAT_WRAPPER_PATH / NAPCAT_QQ_PACKAGE_INFO_PATH /
    NAPCAT_QQ_VERSION_CONFIG_PATH / NAPCAT_DISABLE_PIPE 设好再 import
    napcat.mjs。之前直接跑 napcat/napcat.mjs，缺了这些环境变量，
    NapCat 就会去猜 `resources\\app\\versions\\9.9.15-28131\\package.json`
    并当场崩溃（用户看到的「点启动没反应」就是这个）。
    """
    for rel in ("index.js", os.path.join("napcat", "napcat.mjs"),
                "napcat.mjs", os.path.join("napcat", "index.js")):
        p = os.path.join(d, rel)
        if os.path.isfile(p):
            return p
    return ""


def _shell_env(d):
    """跑 napcat.mjs 时需要的环境变量（与包内 index.js 保持一致）。"""
    env = os.environ.copy()
    for key, name in (("NAPCAT_WRAPPER_PATH", "wrapper.node"),
                      ("NAPCAT_QQ_PACKAGE_INFO_PATH", "package.json"),
                      ("NAPCAT_QQ_VERSION_CONFIG_PATH", "config.json")):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            env[key] = p
    env.setdefault("NAPCAT_DISABLE_PIPE", "1")
    return env


def is_shell_build(install_dir=""):
    """是不是「自带 QQ 内核」的 Shell/Node 版。

    V1.3.0：这个判断很关键 —— Shell 版不需要本机 QQ 在跑，
    而 V1.1.2 那条「QQ 已开着就先别启动」的守卫只适用于 QQ 内嵌版。
    之前不分版本一律拦，结果就是「找得到 NapCat，点启动永远没反应」。
    """
    d = install_dir or find_install_dir()
    if not d:
        return False
    if _napcat_script(d):
        return True
    try:
        with open(os.path.join(d, "package.json"), "r", encoding="utf-8", errors="ignore") as f:
            if "isPureShell" in f.read():
                return True
    except Exception:
        pass
    return False


def napcat_procs():
    """列出正在跑的 NapCat 进程（node 跑 index.js / napcat.mjs 的那些）。

    v1.3.2 加这个的原因：以前 launch() 只管起、不管「已经起着」，
    用户每点一次启动就多开一对进程，几十个实例抢 3003/3004，
    互相把对方顶掉 —— 表现就是「QQ 怎么都登不上」。
    """
    out = []
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        for pr in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cl = " ".join(pr.info.get("cmdline") or []).lower()
                if "napcat" not in cl:
                    continue
                if ("index.js" not in cl) and ("napcat.mjs" not in cl):
                    continue
                out.append({"pid": pr.info.get("pid"), "cmd": cl[:400]})
            except Exception:
                continue
        return out
    # 没 psutil 时的退路：用 CIM 查（Windows）
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = (ps.stdout or b"").decode("utf-8", "replace").strip() or "[]"
        import json as _json
        rows = _json.loads(raw)
        if isinstance(rows, dict):
            rows = [rows]
        for r in rows:
            cl = str(r.get("CommandLine") or "").lower()
            if "napcat" in cl and ("index.js" in cl or "napcat.mjs" in cl):
                out.append({"pid": r.get("ProcessId"), "cmd": cl[:400]})
    except Exception:
        pass
    return out


def running_pids(install_dir=""):
    """只留「跟我们这个安装目录有关」的那些 NapCat 进程。"""
    d = os.path.normcase(str(install_dir or "")).replace("/", "\\")
    rows = []
    for r in napcat_procs():
        if d and d not in str(r.get("cmd") or ""):
            continue
        rows.append(r.get("pid"))
    return [p for p in rows if p]



def _listen_pid(port):
    """谁在 LISTEN 这个端口（Windows netstat -ano）。拿不到就返回 0。"""
    try:
        ps = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=10,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in (ps.stdout or b"").decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].endswith(":" + str(port)):
                    try:
                        return int(parts[4])
                    except ValueError:
                        pass
    except Exception:
        pass
    return 0


def port_owner(port):
    """看这个端口到底是谁在监听，以及「是不是我们自己的 NapCat」。

    v1.3.3 补丁（用户实机踩坑）：光看「3003 通不通」会被别的程序骗过去 ——
    QA 留在机器上的 mock（python mock_napcat.py）占着 3003、还照着 NapCat 的
    接口回了 200，于是界面说「NapCat 已经在跑了，3003 端口也通」，用户扫不到码、
    Agent 一直 10061。这里把「谁在监听」查出来，不是我们的就明说。

    返回 {pid, name, exe, cmd, mine}，查不到返回 {}。
    """
    pid = _listen_pid(port)
    if not pid:
        return {}
    info = {"pid": pid, "name": "", "exe": "", "cmd": "", "mine": False}
    try:
        import psutil
        pr = psutil.Process(pid)
        try:
            info["name"] = pr.name()
        except Exception:
            pass
        try:
            info["exe"] = pr.exe()
        except Exception:
            pass
        try:
            info["cmd"] = " ".join(pr.cmdline())[:300]
        except Exception:
            pass
    except Exception:
        pass
    try:
        d = find_install_dir()
        info["mine"] = pid in running_pids(d)
    except Exception:
        info["mine"] = False
    if not info["mine"]:
        # 同一个 NapCat 可能不是从当前记的安装目录起的（用户换过目录 / 数据重装）。
        # 命令行走的是 index.js 且路径带 napcat，那就还是 NapCat 自己，别误报「被别的程序占着」。
        low = (info.get("cmd") or "").lower()
        if "index.js" in low and "napcat" in low:
            info["mine"] = True
    return info


def stop_all(install_dir="", wait=3.0):
    """把所有 NapCat 进程收掉（清场用）。返回 (杀掉的个数, 说明)。"""
    pids = running_pids(install_dir) if install_dir else [r.get("pid") for r in napcat_procs()]
    pids = [p for p in pids if p]
    if not pids:
        return 0, "没有在跑的 NapCat"
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    deadline = time.time() + max(0.5, float(wait or 0))
    while time.time() < deadline:
        still = running_pids(install_dir) if install_dir else [r.get("pid") for r in napcat_procs()]
        if not still:
            break
        time.sleep(0.3)
    left = running_pids(install_dir) if install_dir else [r.get("pid") for r in napcat_procs()]
    return len(pids), (f"已结束 {len(pids)} 个旧 NapCat 进程" if not left
                       else f"已结束 {len(pids)} 个，还剩 {len(left)} 个没退掉")


def _spawn(args, **kw):
	"""v1.8.4：拉 NapCat 进程统一走这里。

	为什么：Windows 不会因为父进程死了就回收子进程 —— 主程序崩一次、
	或者被任务管理器结束一次，就留一对 node 孤儿；用户实机积了几十个，
	互相抢 3003/3004，表现是「QQ 怎么都登不上」。
	这里把子进程挂进 KILL_ON_JOB_CLOSE 的 Job 对象：主进程一死，
	Windows 内核直接把 job 里的进程收掉，不依赖任何一方「记得清理」。
	"""
	p = subprocess.Popen(args, **kw)
	try:
		if _np is not None:
			_np.attach_job(p)
	except Exception:
		pass
	return p


def launch(install_dir="", wait=0, allow_second_qq=False, restart=False, close_qq=False, takeover=False):
    """启动 NapCat。

    wait > 0 时会等最多 wait 秒，直到 3003 端口起来。
    返回 (ok, msg)。

    v1.3.0 改动：
      · Shell/Node 自带内核版**不再**被「QQ 已开着」拦住（那是内嵌版的规矩）；
      · 优先用包里的 node.exe 直接跑 napcat\\napcat.mjs，绕开 .bat 的编码/工作目录坑；
      · 启动过程写 napcat_launch.log，失败时把日志尾巴带回来给人看。

    v1.3.2 改动（关键）：
      · **已经起着就不再起第二个**（以前每点一次就多开一对，几十个实例抢端口，
        二维码指向的是被顶掉的旧实例 —— 「QQ 永远登不上」就是这么来的）；
      · 发现旧实例（含重复的、别处装的）先清场再起一个干净的；
      · restart=True 强制清场重启。
    """
    d = (install_dir or "").strip() or find_install_dir()
    if not d:
        return False, ("没找到 NapCat 的安装目录。点『下载 NapCat』装一个，"
                       "或在设置 → QQ 接入里手动指定目录。")
    launcher = find_launcher(d)
    if not launcher and not _napcat_script(d):
        return False, ("没找到 NapCat 的启动器。请先在「设置 → QQ 接入」里指定目录，"
                       "或点『下载 NapCat』让程序帮你装。")

    # v1.8.4：接管 = 用户明确点了「启动 NapCat」，授权我们先退官方 QQ 再快速登录。
    # 这个标记必须在下面所有「QQ 开着就拦人」的判断之前算出来，否则接管模式
    # 照样会被 v1.6.2 那句「我不会去关你的 QQ」拦掉。
    _take = bool(close_qq or takeover)

    # ---- v1.9.18：本机 QQ 开着就不再「拒绝启动」----
    # v1.6.2 起这里是硬拦截：一检测到 QQ.exe 就 return False，让用户自己去退出官方 QQ。
    # 实机结果是用户点几次「启动 NapCat」几次都拉不起来 —— 他本来就想一边挂着 QQ 一边
    # 让小鲸鱼在线，而「先退掉你的 QQ」恰恰是他明确说过的下策。
    # 现在改成：照常启动，靠 conflict 记录绕开被 QQ 占着的号；真绕不开再如实说明，
    # 并把「退 QQ 并接管」做成弹窗里的一个按钮，而不是让他自己去任务管理器里折腾。
    _qq_open = False
    _qq_cli = []
    try:
        _qq_cli = qq_client_procs()
        _qq_open = bool(_qq_cli)
    except Exception:
        _qq_cli, _qq_open = [], False
    if _qq_open:
        try:
            with open(launch_log_path() or os.devnull, "ab") as _f:
                _f.write(("\n===== %s 本机 QQ 客户端开着（%d 个进程）—— 照常启动，"
                          "自动避开被它占着的号 =====\n"
                          % (time.strftime("%Y-%m-%d %H:%M:%S"), len(_qq_cli)))
                         .encode("utf-8", "ignore"))
        except Exception:
            pass

    # ---- v1.3.2：先看看是不是已经跑着了 ----
    mine = running_pids(d)
    others = [r.get("pid") for r in napcat_procs() if r.get("pid") not in mine]
    alive = port_open(DEFAULT_HTTP_PORT)
    # v1.8.4：接管时，跑着但没登上（3003 不通）的 NapCat 属于「卡住的旧实例」——
    # 直接清掉重开。否则会在下面回一句「正在等你扫码」，用户点了半天什么也没发生，
    # 而且那张二维码可能早就过期了。
    if _take and mine and not alive:
        restart = True
    # v1.3.3 补丁：3003 通，但监听的不是我们的 NapCat → 直说，别回「已经在跑了」
    if alive:
        try:
            _owner = port_owner(DEFAULT_HTTP_PORT)
        except Exception:
            _owner = {}
        if _owner and not _owner.get("mine") and not mine:
            _p = _owner.get("exe") or _owner.get("cmd") or ""
            return False, (f"端口 {DEFAULT_HTTP_PORT} 被别的程序占着（{_owner.get('name') or '未知程序'}，"
                           f"PID {_owner.get('pid')}），NapCat 绑不上端口，扫了码也连不上。\n"
                           + (f"它在这里：{_p}\n" if _p else "")
                           + "我不动它 —— 那可能是你正在用的软件。如果它自己会关，"
                             "过会儿点「重新检测」就好；要马上用，也可以把这个软件关掉，"
                             "或者把 NapCat 的端口改到别的号。")

    # v1.3.3（用户实机反馈）：进程在、端口全关、也没有二维码 = 假死。
    # 老逻辑这时只回一句「已经在跑，等你扫码」，用户对着一个死进程永远等不到二维码。
    if mine and not restart and not alive:
        try:
            dead = stale_pids(d)
        except Exception:
            dead = []
        if dead:
            killed, kmsg = stop_all(d)
            try:
                with open(launch_log_path() or os.devnull, "ab") as f:
                    f.write((f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 清理假死进程："
                             f"{kmsg}（PID {'、'.join(map(str, dead))}）=====\n")
                            .encode("utf-8", "ignore"))
            except Exception:
                pass
            mine = running_pids(d)

    if mine and not restart:
        qr = qr_path(d)
        qr_new = _qr_fresh(d)
        extra = ""
        if others:
            extra = f"\n另外还有 {len(others)} 个别的目录里的 NapCat 在跑（可能是以前装的），建议清掉。"
        if alive:
            return True, "NapCat 已经在跑了，3003 端口也通，不用再启动。" + extra
        if qr:
            return True, ("NapCat 已经在跑，正在等你扫码登录。\n"
                          + (f"二维码图片：{qr}\n" if qr_new else
                             f"二维码图片：{qr}（这张是旧的，可能已过期）\n")
                          + "扫完码 3003 端口会自动起来。"
                          "如果二维码打不开或一直扫不上，点「重新检测」会清掉进程重开一个。" + extra)
        return False, ("NapCat 进程在（PID " + "、".join(str(p) for p in mine) +
                       f"），但 {DEFAULT_HTTP_PORT} 没开、也没有二维码 —— 它已经假死了。\n"
                       "点「重新检测」会自动清掉并重开；也可以手动结束上面的 PID 再点「启动 NapCat」。")

    # 旧实例（重复的 / 别的目录的）先收掉，否则抢端口
    if mine or others:
        killed, kmsg = stop_all("")
        try:
            with open(launch_log_path() or os.devnull, "ab") as f:
                f.write((f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                         f"清场：{kmsg}（自己 {len(mine)} + 别的 {len(others)}）=====\n")
                        .encode("utf-8", "ignore"))
        except Exception:
            pass

    # v1.4.1 / v1.8.3：用户要求「已经登录的 QQ 直接连」—— 本机 QQ 客户端占着号时，
    # NapCat 的快速登录会被「已登录,无法重复登录」拒掉，所以接管模式下先把 QQ 关掉。
    quit_msg = ""
    _take = bool(close_qq or takeover)
    # v1.9.14：**默认不动你的 QQ**。只有两种情况才退：
    #   ① close_qq=True —— 用户明确点了「这次帮我退出 QQ」；
    #   ② takeover=True 且设置里关掉了「不动我的 QQ」。
    _keep_qq = True
    try:
        _keep_qq = bool(cfg_mod.get("napcat_keep_qq", True))
    except Exception:
        _keep_qq = True
    _do_quit = bool(close_qq) or (bool(takeover) and not _keep_qq)
    if _do_quit and qq_client_procs():
        try:
            _killed, quit_msg = quit_qq_client(force=bool(close_qq))
        except Exception as e:
            quit_msg = f"退出 QQ 失败：{type(e).__name__}: {e}"
        try:
            with open(launch_log_path() or os.devnull, "ab") as f:
                f.write((f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                         f"{'自动接管·' if takeover else ''}退出 QQ 客户端：{quit_msg} =====\n").encode("utf-8", "ignore"))
        except Exception:
            pass

    # v1.8.3：接管模式下 QQ 没关干净就别硬上，直接说清楚
    if _do_quit and takeover and qq_running():
        return False, ("已经帮你退出官方 QQ，但还有 QQ 进程没关掉（可能被别的东西占着）。\n在任务管理器里结束 QQ 之后再点一次就行。"
                    "（不想退 QQ 的话，去设置里把「不动我的 QQ」打开。）")
    shell = is_shell_build(d)
    # v1.6.2：以前这里是「QQ 开着就拒绝启动」，用户被卡住还被要求去杀进程。
    # 现在：QQ 在跑且我们连不上时**不去动 QQ**、也不报错式拦截，只返回一句温和的说明
    # （界面照常显示「启动 NapCat」按钮，用户退出 QQ 后再点）。
    if (not allow_second_qq and not shell and not _take
            and not port_open(DEFAULT_HTTP_PORT) and qq_running()):
        return False, ("检测到官方 QQ 正开着。点「启动并出码」会先用没被它占着的号登录，"
                    "都占着就直接出二维码，你扫另一个号就行；"
                    "想让小鲸鱼用你现在这个号，点弹窗里的「退出我的 QQ 并接管」。")

    node = _bundled_node(d)
    script = _napcat_script(d)
    log_p = launch_log_path()
    out = None
    _q = ""
    _t_launch = time.time()
    # v1.9.25：记下「本次启动」在日志里从哪个字节开始。下面所有错误判定都只看这之后
    # 写出来的内容 —— 否则会把几天前的旧错误当成本次结论（用户实机就是这么被坑的）。
    _log_off = _log_size()

    def _qrp():
        """只认这次启动之后写出来的二维码。

        v1.9.18：以前这里直接 qr_path(d) —— 目录里留着上一次的旧码时，哪怕
        3003 根本没起来、快速登录其实成功了，也会被判成「在等扫码」，把旧码当新码给用户看。
        """
        p = qr_path(d)
        if not p:
            return ""
        try:
            if os.path.getmtime(p) >= _t_launch - 5:
                return p
        except OSError:
            return ""
        return ""

    try:
        if log_p:
            try:
                with open(log_p, "ab") as f:
                    f.write(("\n===== %s 启动 NapCat =====\n"
                             % time.strftime("%Y-%m-%d %H:%M:%S")).encode("utf-8", "ignore"))
            except Exception:
                pass
            out = open(log_p, "ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        low = (launcher or "").lower()
        if shell and node and script:
            # v1.3.3：知道 QQ 号就带 -q 走「快速登录」—— NapCat 支持直接从本地缓存登录，
            # 不用每次重启都重新扫码（用户实机踩到：我们一重启，登录态就没了，得再扫一次）。
            args = [node, script]
            try:
                _cands = login_candidates(d)
                if _qq_open and not _do_quit:
                    # v1.9.18：被官方 QQ 占着的号直接跳过；全被占着就干脆不带 -q，
                    # 让 NapCat 直接出码 —— 用户扫另一个号，比撞一次墙再失败强。
                    # v1.9.25：再加上「数据目录还在被写」的号 —— 官方 QQ 占着它时，
                    # 日志里那句「已登录,无法重复登录」不一定出现，但号一定会被顶。
                    _clash_set = set(conflict_uins())
                    try:
                        _clash_set |= set(qq_client_uins())
                    except Exception:
                        pass
                    _cands = [u for u in _cands if u not in _clash_set]
                _q = _cands[0] if _cands else ""
            except Exception:
                _q = ""
            if _q:
                args += ["-q", _q]
            _spawn(args, cwd=d, stdin=subprocess.DEVNULL,
                             env=_shell_env(d),
                             stdout=out or subprocess.DEVNULL,
                             stderr=subprocess.STDOUT if out else subprocess.DEVNULL,
                             creationflags=flags)
            started = f"{os.path.basename(node)} {os.path.basename(script)}" + (f" -q {_q}" if _q else "")
        elif low.endswith(".bat"):
            # 老版本：交给 cmd 起，工作目录必须设成安装目录
            _spawn(["cmd.exe", "/c", "call", launcher], cwd=d,
                             stdin=subprocess.DEVNULL,
                             stdout=out or subprocess.DEVNULL,
                             stderr=subprocess.STDOUT if out else subprocess.DEVNULL,
                             creationflags=flags)
            started = launcher
        else:
            _spawn([launcher], cwd=d, stdin=subprocess.DEVNULL,
                             stdout=out or subprocess.DEVNULL,
                             stderr=subprocess.STDOUT if out else subprocess.DEVNULL,
                             creationflags=flags)
            started = launcher
    except Exception as e:
        return False, f"启动失败：{type(e).__name__}: {e}"
    finally:
        try:
            if out:
                out.close()
        except Exception:
            pass

    if wait:
        deadline = time.time() + int(wait)
        qr = ""
        last_err = {}
        # v1.9.25：被踢下线的号 —— 等完这轮后不再拿它们去试，直接换号。
        _kicked = set()
        while time.time() < deadline:
            if port_open(DEFAULT_HTTP_PORT):
                try:
                    mark_uin_ok(_q)
                except Exception:
                    pass
                if _q:
                    return True, (f"已连上：用本地缓存直接登录（QQ {_q}），没扫码。"
                                 + ("（启动前退掉了官方 QQ，登录态是保的）" if _do_quit else "（你的 QQ 没动）"))
                return True, f"NapCat 已启动（{started}）"
            try:
                last_err = launch_log_errors(after=_log_off)
            except Exception:
                last_err = {}
            _uc_now = str((last_err or {}).get("code") or "")
            # v1.9.25：**不再一看到 token_dead 就 break**。
            # 以前这里 break 得太早：日志尾部翻出一条几天前的旧 token_dead 就跳出循环，
            # 连 3003 会不会起来都没等，对外报「20 秒内端口没起来」——而其实早就起来了。
            # 现在只有「风控」才立刻停（越试越锁），token_dead 记完坏号继续等：
            # NapCat 会自己重试，端口说不定下一秒就通。
            if _uc_now == "risk":
                break
            if _uc_now == "token_dead":
                try:
                    mark_uin_bad(str((last_err or {}).get("uin") or _q), "登录态已失效")
                except Exception:
                    pass
            if _uc_now == "account_kicked":
                # 快速登录成功又被踢 = 这个号被官方 QQ 占着。记到内存里，
                # 等下换号时排除掉；同时记进 conflict，半小时内不再优先选它。
                _ku = str((last_err or {}).get("uin") or _q)
                if _ku:
                    _kicked.add(_ku)
                    try:
                        mark_uin_conflict(_ku, "本机 QQ 客户端正用着这个号")
                    except Exception:
                        pass
            if not qr:
                qr = _qrp()
            time.sleep(1.0)
        _uc = str((last_err or {}).get("code") or "")
        _uin = str((last_err or {}).get("uin") or "")
        if _uc == "token_dead":
            try:
                mark_uin_bad(_uin or _q, "登录态已失效")
            except Exception:
                pass
        if _uc == "risk":
            try:
                stop_all(d, wait=2.0)
            except Exception:
                pass
            return False, (
                "你的 QQ 号被腾讯风控了（错误码 168），NapCat 连登录都被拒。\n"
                "先去手机 QQ 按提示做完安全验证；恢复之前扫码和快速登录都不行，"
                "也别反复试 —— 越试锁得越久。")
        if _uc in ("qq_conflict", "account_kicked"):
            # v1.9.18：撞上「已登录,无法重复登录」不再劝退用户，而是把这个号记成
            # 「被官方 QQ 占着」，交给下面那段「换下一个本地账号」继续试 ——
            # 只要还有另一个登录态有效的号，小鲸鱼就能直接连上，用户什么都不用退。
            # v1.9.25：account_kicked（登进去又被踢）走同一条路。
            try:
                mark_uin_conflict(_uin or _q, "本机 QQ 客户端正用着这个号")
            except Exception:
                pass
        # v1.9.14：快速登录失败（登录态失效）时**立刻换下一个本地账号**，
        # 不要停在二维码上 —— 反复出码、反复扫码正是被风控的原因。
        # v1.9.18：被官方 QQ 占着的号（qq_conflict）也走这条换号路径。
        if _q and not port_open(DEFAULT_HTTP_PORT):
            try:
                _clash_now = set(conflict_uins())
                _clash_now |= set(_kicked)
                try:
                    _clash_now |= set(qq_client_uins())
                except Exception:
                    pass
                _rest = [x for x in login_candidates(d) if x != _q and x not in _clash_now]
            except Exception:
                _rest = []
            for _q2 in _rest[:2]:
                try:
                    stop_all(d, wait=2.0)
                except Exception:
                    pass
                try:
                    _off2 = _log_size()
                except Exception:
                    _off2 = None
                try:
                    _spawn([node, script, "-q", _q2], cwd=d,
                        stdin=subprocess.DEVNULL, env=_shell_env(d),
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:
                    continue
                _dl2 = time.time() + float(min(20, max(8, int(wait))))
                _ok2 = False
                while time.time() < _dl2:
                    if port_open(DEFAULT_HTTP_PORT):
                        _ok2 = True
                        break
                    time.sleep(1.0)
                if _ok2:
                    try:
                        mark_uin_ok(_q2)
                    except Exception:
                        pass
                    return True, (f"已连上：换了个本地账号直接登录（QQ {_q2}），没扫码。"
                        + ("（启动前退掉了官方 QQ）" if _do_quit else "（你的 QQ 没动）"))
                try:
                    _e2 = launch_log_errors(after=_off2) or {}
                except Exception:
                    _e2 = {}
                _c2 = str(_e2.get("code") or "")
                if _c2 == "token_dead":
                    try:
                        mark_uin_bad(str(_e2.get("uin") or _q2), "登录态已失效")
                    except Exception:
                        pass
                    continue
                if _c2 in ("qq_conflict", "account_kicked"):
                    # 这个号也被官方 QQ 占着（或刚登上就被顶）—— 记下来，继续试下一个
                    try:
                        mark_uin_conflict(str(_e2.get("uin") or _q2), "本机 QQ 客户端正用着这个号")
                    except Exception:
                        pass
                    continue
                break
            qr = _qrp()
        # 端口没起来但已经出二维码 = 起来了，只是在等扫码
        if qr:
            _why = ""
            if _do_quit:
                _why = ("\n（启动前退掉了官方 QQ，但本地登录态没能直接用 —— "
                    "多半是 QQ 换过版本或缓存过期了。扫这一次之后就能免扫。）")
            elif _qq_open:
                _why = ("\n（你的官方 QQ 正开着 —— 同一个号不能两边同时在线，"
                    "所以这张码是给你扫另一个号的。想让小鲸鱼用你现在这个号，"
                    "点弹窗里的「退出我的 QQ 并接管」；不想退就换一个号扫。）")
            elif _keep_qq:
                _why = ("\n（本地登录态没能直接用 —— 登录过的那几个号都失效了。"
                    "扫这一次之后就能免扫。）")
            return True, (
                "NapCat 已经跑起来了，现在等扫码登录。\n"
                    + (f"二维码图片：{qr}\n" if qr else "")
                    + "点下面的按钮就能打开它；扫完码 3003 端口会自动起来。" + _why)
        if started.endswith(tuple("0123456789")) and "-q" in started:
            # 带 -q 没起来：账号缓存可能失效了，改用扫码模式再试一次
            try:
                stop_all(d, wait=2.0)
                _spawn([os.path.join(d, "node.exe") if os.path.isfile(os.path.join(d, "node.exe")) else "node",
                                  script], cwd=d, stdin=subprocess.DEVNULL,
                                 env=_shell_env(d),
                                 stdout=out or subprocess.DEVNULL,
                                 stderr=subprocess.STDOUT if out else subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                time.sleep(6)
                if _qrp():
                    _hint = ("（提示：连续多次扫码容易被腾讯风控，能快速登录时别扫码）")
                    if _qq_open and not _do_quit:
                        _hint = ("（你的官方 QQ 占着刚才那个号 —— 这张码是给另一个号用的。"
                                 "想让小鲸鱼用你现在开着的那个号，点弹窗里的「退出我的 QQ 并接管」。）")
                    return True, ("自动登录没成功，已经改成等你扫码。\n"
                                  f"二维码图片：{_qrp()}\n"
                                  "用手机 QQ 扫一下就好。\n" + _hint)
            except Exception:
                pass
        tail = launch_log_tail(8)
        msg = (f"已经尝试启动（{started}），但 {int(wait)} 秒内 3003 端口没起来。\n"
               "常见原因：① 杀毒软件拦了 node.exe；② 3003/3004 端口被别的程序占了。")
        if _qq_open and not _do_quit:
            msg += ("\n③ 你的官方 QQ 正开着，本地缓存里那几个号都被它占着 —— "
                    "扫另一张码登别的号，或者点弹窗里的「退出我的 QQ 并接管」。")
        if tail:
            msg += "\n\n启动日志最后几行：\n" + tail
        return False, msg
    return True, f"已尝试启动：{started}"


# ==================== 下载安装（需用户同意）====================
# v1.2.1：国内直连 GitHub 十次有九次卡在 0%。
# 所以做成「官方优先 → 镜像降级 → 本地导入」三级方案：
#   1) 先走官方 GitHub；
#   2) 官方连不上/超时就换公共加速镜像（ghfast / gh-proxy / ghproxy 等）；
#   3) 全失败就明确告诉用户浏览器去下 zip，再用「手动导入安装包」。
# 镜像名单会变（哪天某个站就关了），所以集中放在这里，改起来是一行的事。
NAPCAT_RELEASES_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
NAPCAT_RELEASES_PAGE = "https://github.com/NapNeko/NapCatQQ/releases/latest"

# {url} 会被替换成真实地址；顺序即优先级
NAPCAT_MIRRORS = [
    ("官方 GitHub", "{url}"),
    ("ghfast.top", "https://ghfast.top/{url}"),
    ("gh-proxy.com", "https://gh-proxy.com/{url}"),
    ("ghproxy.net", "https://ghproxy.net/{url}"),
    ("moeyy.xyz", "https://github.moeyy.xyz/{url}"),
]

_CONNECT_TIMEOUT = 20      # 连不上就赶紧放弃，用户在盯着进度条
_READ_TIMEOUT = 180


def _p(progress, msg):
    """安全地报个进度（回调里抛异常不能影响下载）。"""
    if not progress:
        return
    try:
        progress(str(msg))
    except Exception:
        pass


def mirror_candidates(url, prefer=""):
    """把真实下载地址展开成 [(源名, 该源的地址)]，首选那个排最前。

    prefer 来自配置 napcat_mirror（向导里「下载源」选的那个），
    留空=按默认顺序（官方优先）。
    """
    items = list(NAPCAT_MIRRORS)
    pref = str(prefer or "").strip()
    if pref:
        items.sort(key=lambda it: 0 if it[0] == pref else 1)
    out = []
    for name, tpl in items:
        try:
            out.append((name, tpl.format(url=url)))
        except Exception:
            continue
    return out


def _fetch_release_json():
    """取最新 release 的 JSON。官方 API 不通时试着走镜像（有的镜像也代理 API）。"""
    errors = []
    for name, real in mirror_candidates(NAPCAT_RELEASES_API):
        try:
            req = urllib.request.Request(
                real, headers={"User-Agent": "qq-agent/5.0", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as r:
                raw = r.read(2_000_000).decode("utf-8", "replace")
            data = __import__("json").loads(raw)
            if isinstance(data, dict) and (data.get("assets") or data.get("tag_name")):
                return data, name, ""
            errors.append(f"{name}: 返回里没有 release 信息")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {e}")
    return None, "", "；".join(errors[-2:])


def find_release_asset():
    """查 NapCat 最新 release 里的 Windows 包下载地址。

    返回 {"ok","tag","url","name","size","msg","source"}。
    失败时 ok=False 且 msg 说明原因（没网 / 被墙 / API 限流）。
    """
    data, src, err = _fetch_release_json()
    if not data:
        return {"ok": False,
                "msg": f"查不到 NapCat 版本信息（{err}）。国内网络常常连不上 GitHub API —— "
                       f"你可以用浏览器打开 {NAPCAT_RELEASES_PAGE} 手动下载 zip，"
                       f"然后在向导里点『手动导入安装包』。"}

    tag = data.get("tag_name") or ""
    assets = data.get("assets") or []
    # 优先选带 Windows / NapCat.Shell 的 zip
    cand = []
    for a in assets:
        name = str(a.get("name") or "")
        low = name.lower()
        if not low.endswith(".zip"):
            continue
        score = 0
        if "napcat" in low:
            score += 2
        if "shell" in low or "win" in low:
            score += 2
        # 只认自带 Node 的那个（NapCat.Shell.Windows.Node.zip）：
        # OneKey 包只有几百 KB、要求机器上装了 QQ，Framework 包同理 —— 都会让用户卡住
        if name == WIN_NODE_ASSET:
            score += 10
        elif "onekey" in low or "framework" in low:
            score -= 5
        cand.append((score, name, a))
    if not cand:
        return {"ok": False, "msg": f"最新版本 {tag} 里没找到 Windows zip 包"}
    cand.sort(key=lambda x: -x[0])
    best = cand[0][2]
    return {
        "ok": True,
        "tag": tag,
        "name": str(best.get("name") or ""),
        "url": str(best.get("browser_download_url") or ""),
        "size": int(best.get("size") or 0),
        "source": src,
        "msg": f"最新版本 {tag}：{best.get('name')}",
    }


def download_file(url, dest, progress=None, label="", expect_size=0):
    """把 url 下到 dest。返回 (ok, msg, 收到字节数)。

    失败要快：连不上 20 秒放弃；镜像返回 200 但内容是网页（限流页）也算失败，
    否则会把几百字节的 HTML 当 zip 存下来，解压时才报"不是压缩包"，更难懂。
    """
    got = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "qq-agent/5.0"})
        with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as r:
            ctype = str(r.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                return False, "这个源返回的是网页（多半是镜像限流或地址失效）", 0
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except Exception:
                total = 0
            if not total:
                try:
                    total = int(expect_size or 0)
                except Exception:
                    total = 0
            last_pct = -1
            first = True
            with open(dest, "wb") as f:
                while True:
                    try:
                        chunk = r.read(1024 * 256)
                    except socket.timeout:
                        return False, f"下载中途超时（已收 {got / 1048576:.1f} MB）", got
                    if not chunk:
                        break
                    if first:
                        first = False
                        if chunk[:1] == b"<":
                            return False, "下回来的不是压缩包（源返回了网页）", got
                    f.write(chunk)
                    got += len(chunk)
                    pct = int(got * 100 / total) if total else 0
                    if pct // 2 != last_pct // 2 or (total and got >= total):
                        last_pct = pct
                        if total:
                            _p(progress, f"正在下载 {label}：{got / 1048576:.1f} / "
                                         f"{total / 1048576:.1f} MB（{pct}%）")
                        else:
                            _p(progress, f"正在下载 {label}：已收 {got / 1048576:.1f} MB")
    except socket.timeout:
        return False, "连接超时（这个源在国内基本连不上）", got
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", got
    if got < 1024:
        return False, f"只收到 {got} 字节，源有问题", got
    return True, f"已下载 {got / 1048576:.1f} MB", got


def zip_has_launcher(zip_path):
    """不解压先看包里有没有 NapCat 的启动器（防止用户选错 zip）。"""
    try:
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                base = os.path.basename(name.replace("\\", "/"))
                if base in _LAUNCHERS:
                    return True
    except Exception:
        return False
    return False


def extract_zip_to(zip_path, dest, progress=None):
    """解压到 dest（带 zip 逃逸防护）。返回 (ok, msg)。

    内置包有一百多 MB、七百多个文件，解压要十几秒 —— 有 progress 就报进度，
    否则用户会以为界面卡住了。
    """
    try:
        import zipfile
        root = os.path.abspath(dest)
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            for name in names:
                target = os.path.abspath(os.path.join(dest, name))
                if not target.startswith(root + os.sep) and target != root:
                    return False, f"压缩包内有非法路径：{name}"
            total = max(1, len(names))
            step = max(1, total // 20)
            for i, name in enumerate(names):
                zf.extract(name, dest)
                if progress and (i % step == 0 or i == total - 1):
                    pct = int((i + 1) * 100 / total)
                    _p(progress, f"正在解压… {i + 1}/{total} 个文件（{pct}%）")
    except Exception as e:
        return False, f"解压失败：{type(e).__name__}: {e}"
    return True, ""


def _finish_install(dest, progress=None, source=""):
    """解压完的收尾：认目录 → 记配置 → 写 OneBot 配置 → 拼消息。"""
    # 解压后可能多一层目录（GitHub 包常常是 NapCat.Shell.xxx/ 里再放文件）
    d = dest
    if not _looks_like_napcat(d):
        try:
            for name in os.listdir(dest):
                p = os.path.join(dest, name)
                if _looks_like_napcat(p):
                    d = p
                    break
        except OSError:
            pass

    try:
        cfg_mod.set_checked("napcat_dir", d)
    except Exception:
        pass

    where = f"（来自 {source}）" if source else ""
    if _looks_like_napcat(d):
        _p(progress, "已经在写了 OneBot 配置…")
        try:
            cb = write_onebot_config(d)
            extra = cb.get("msg") or ""
        except Exception as e:
            extra = f"写配置失败（不影响使用，可在 WebUI 里手配）：{type(e).__name__}: {e}"
        msg = f"NapCat 已装到 {d}{where}，现在可以点『启动 NapCat』"
        if extra:
            msg += f"；{extra}"
        return True, msg, d

    return True, (f"已解压到 {d}{where}，但没认出启动器。请手动打开该目录，"
                  f"运行里面的启动脚本，并在设置里指定这个目录。"), d


def download_and_install(target_dir="", progress=None):
    """下载并解压 NapCat 到目标目录（多源自动降级）。

    必须由用户显式点同意后调用（napcat_allow_install）。
    返回 (ok, msg, install_dir)。
    """
    if not bool(cfg_mod.get("napcat_allow_install", True)):
        return False, "已关闭「允许本软件下载安装 NapCat」，请手动安装后在设置里指定目录", ""

    info = find_release_asset()
    if not info.get("ok"):
        return False, info.get("msg", "查不到下载地址"), ""

    dest = target_dir or os.path.join(str(cfg_mod.DATA_DIR), "napcat")
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as e:
        return False, f"建目录失败：{e}", ""

    zip_path = os.path.join(dest, info["name"] or "napcat.zip")
    size_txt = f"约 {info['size'] / 1048576:.1f} MB" if info.get("size") else "大小未知"
    prefer = str(cfg_mod.get("napcat_mirror") or "")
    cands = mirror_candidates(info["url"], prefer)

    ok_dl = False
    last_err = ""
    used = ""
    for idx, (name, real) in enumerate(cands):
        tag = "" if idx == 0 else f"（第 {idx + 1} 个源）"
        _p(progress, f"正在从「{name}」下载 {info['name']}{tag}，{size_txt}…")
        ok, msg, got = download_file(real, zip_path, progress, name, info.get("size") or 0)
        if ok:
            used = name
            ok_dl = True
            _p(progress, f"「{name}」下载完成（{got / 1048576:.1f} MB），正在解压…")
            break
        last_err = f"「{name}」{msg}"
        _p(progress, f"「{name}」没成功（{msg}），换下一个源…")
        try:
            os.remove(zip_path)
        except OSError:
            pass

    if not ok_dl:
        return False, (
            f"所有下载源都没成功（最后一次：{last_err}）。\n"
            f"这是国内网络的常见情况 —— GitHub 经常连不上，跟你的电脑没关系。\n"
            f"办法：用浏览器打开 {NAPCAT_RELEASES_PAGE} ，下载结尾是 .zip 的 Windows 包，"
            f"然后回到向导点『手动导入安装包』把它选进来（本程序会自动解压、写配置）。"), ""

    ok, msg = extract_zip_to(zip_path, dest)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    if not ok:
        return False, msg, ""
    return _finish_install(dest, progress, source=used)


def install_from_zip(zip_path, target_dir="", progress=None, source="本地文件"):
    """从用户自己下载的 zip 安装（终极兜底，不需要联网）。

    返回 (ok, msg, install_dir)。
    """
    zp = str(zip_path or "").strip().strip('"')
    if not zp or not os.path.isfile(zp):
        return False, f"找不到这个文件：{zp or '(空)'}", ""
    if not zip_has_launcher(zp):
        return False, ("这个压缩包里没看到 NapCat 的启动器（napcat.bat / NapCatWinBootMain.exe）。"
                       "确认一下是不是下错了包 —— 要 NapCatQQ 的 Windows Shell zip。"), ""

    dest = target_dir or os.path.join(str(cfg_mod.DATA_DIR), "napcat")
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as e:
        return False, f"建目录失败：{e}", ""

    _p(progress, f"正在解压 {os.path.basename(zp)} …")
    ok, msg = extract_zip_to(zp, dest, progress)
    if not ok:
        return False, msg, ""
    return _finish_install(dest, progress, source=source)


def save_uploaded_zip(data, filename="napcat.zip", target_dir=""):
    """把前端/接口传上来的 zip 字节存成文件，再调 install_from_zip。

    给「拖拽导入」或以后走 HTTP 上传留的口子，返回 (ok, msg, dir)。
    """
    dest = target_dir or os.path.join(str(cfg_mod.DATA_DIR), "napcat")
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception as e:
        return False, f"建目录失败：{e}", ""
    tmp = os.path.join(dest, "_import_" + (os.path.basename(str(filename or "napcat.zip")) or "napcat.zip"))
    try:
        with open(tmp, "wb") as f:
            f.write(data)
    except Exception as e:
        return False, f"存文件失败：{type(e).__name__}: {e}", ""
    try:
        return install_from_zip(tmp, target_dir=dest)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def open_dir(install_dir=""):
    """在资源管理器里打开 NapCat 目录（找不到就打开数据目录）

    走 app_ui.shell_open：它会自己拉起 explorer.exe 并把窗口提到最前
    （os.startfile 拉起来的窗口会被本程序挡在后面，用户以为没反应）。
    这里用延迟 import，避免 app_ui 和 napcat_mgr 循环依赖。
    """
    d = install_dir or find_install_dir()
    if not d:
        d = str(cfg_mod.DATA_DIR)
    try:
        import app_ui
        r = app_ui.shell_open(d)
        return bool(r.get("ok")), (r.get("msg") or d)
    except Exception:
        pass
    try:
        os.startfile(d)  # noqa: S606 - Windows 专用
        return True, d
    except Exception as e:
        return False, f"打不开目录：{e}"


# ==================== 内置离线包（V1.2.2）====================
#
# 国内不翻墙从 GitHub 下 NapCat（118MB）实测要一个多小时，还会断在半路。
# 所以安装包里直接带一份：首次安装变成「解压本地文件」，几秒搞定，全程不联网。
# 构建期由 build_napcat.py 把它放进 desktop/src-tauri/resources/napcat_bundle.zip。

BUNDLED_NAME = "napcat_bundle.zip"
WIN_NODE_ASSET = "NapCat.Shell.Windows.Node.zip"   # 自带 Node 的免安装版
_BUNDLE_MIN = 20 * 1024 * 1024                     # 小于这个数肯定不对


def _bundle_search_paths():
    """按优先级列出可能放离线包的位置。"""
    out = []
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        out.append(os.path.join(exe_dir, BUNDLED_NAME))                 # 绿色版
        out.append(os.path.join(exe_dir, "resources", BUNDLED_NAME))    # 安装版（Tauri resources）
    except Exception:
        pass
    try:
        out.append(os.path.join(str(cfg_mod.DATA_DIR), BUNDLED_NAME))
        out.append(os.path.join(str(cfg_mod.DATA_DIR), "runtime", BUNDLED_NAME))
    except Exception:
        pass
    try:
        # 源码运行：项目根目录
        out.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), BUNDLED_NAME))
    except Exception:
        pass
    return out


def bundled_zip():
    """找到随程序发布的离线包就返回路径，否则空串。"""
    for p in _bundle_search_paths():
        try:
            if p and os.path.isfile(p) and os.path.getsize(p) >= _BUNDLE_MIN:
                return p
        except Exception:
            continue
    return ""


def _bundle_version(zip_path):
    """从包里的 package.json 里读版本号（读不到就算了）。"""
    try:
        import json
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            for n in zf.namelist():
                low = n.lower()
                if low.endswith("package.json") and low.count("/") <= 1:
                    try:
                        d = json.loads(zf.read(n).decode("utf-8", "replace") or "{}")
                    except Exception:
                        continue
                    v = str(d.get("version") or "").strip()
                    if v:
                        return v
    except Exception:
        pass
    return ""


def bundled_info():
    """离线包信息，给界面/接口用。"""
    p = bundled_zip()
    if not p:
        return {"ok": False, "path": "", "size": 0, "version": "", "text": ""}
    try:
        size = os.path.getsize(p)
    except Exception:
        size = 0
    ver = _bundle_version(p)
    mb = size / 1048576.0
    # 版本号是包里 package.json 的 QQ 版本（9.9.31-49738），界面上写清「QQ」以免被当成 NapCat 版本
    text = f"{mb:.0f} MB" + (f"（QQ {ver}）" if ver else "")
    return {"ok": True, "path": p, "size": size, "version": ver, "text": text}


def install_from_bundle(target_dir="", progress=None):
    """展开随程序发布的离线包（不联网，几秒到二十几秒）。

    返回 (ok, msg, install_dir)，跟 download_and_install / install_from_zip 一致。
    """
    bi = bundled_info()
    if not bi.get("ok"):
        return False, "这一版没带内置离线包。请用「在线下载」或「手动导入安装包」。", ""
    _p(progress, f"正在解压内置离线包（{bi.get('text') or '随程序自带'}），不用联网…")
    dest = target_dir or os.path.join(str(cfg_mod.DATA_DIR), "napcat")
    ok, msg, d = install_from_zip(bi["path"], target_dir=dest, progress=progress,
                                  source="内置离线包")
    if ok:
        try:
            cfg_mod.set_checked("napcat_bundle_version", bi.get("version") or "")
        except Exception:
            pass
    return ok, msg, d


def latest_release(tag=""):
    """查某个 tag（默认最新）的 Windows Node 包地址。构建脚本用。

    返回 {"ok","tag","name","url","size","source","msg"}。
    """
    if tag:
        url = ("https://github.com/NapNeko/NapCatQQ/releases/download/"
               f"{tag}/{WIN_NODE_ASSET}")
        return {"ok": True, "tag": tag, "name": WIN_NODE_ASSET, "url": url,
                "size": 0, "source": "指定版本", "msg": f"指定版本 {tag}"}
    return find_release_asset()


def download_official_zip(dest, url="", progress=None, expect_size=0):
    """把官方包下到 dest（多源降级）。构建脚本用。

    返回 (ok, msg, dest)。
    """
    asset_url = url or (find_release_asset().get("url") or "")
    if not asset_url:
        return False, "拿不到下载地址（GitHub API 不通且镜像也没帮上忙）", ""
    dirname = os.path.dirname(os.path.abspath(dest))
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    prefer = str(cfg_mod.get("napcat_mirror") or "")
    last = ""
    for name, real in mirror_candidates(asset_url, prefer):
        _p(progress, f"正在从「{name}」下载 {WIN_NODE_ASSET}…")
        ok, msg, got = download_file(real, dest, progress, name, expect_size)
        if ok:
            _p(progress, f"「{name}」下载完成（{got / 1048576:.1f} MB）")
            return True, f"{name}：{got / 1048576:.1f} MB", dest
        last = f"「{name}」{msg}"
        _p(progress, f"「{name}」没成功（{msg}），换下一个源…")
        try:
            os.remove(dest)
        except OSError:
            pass
    return False, f"所有下载源都没成功（最后一次：{last}）", ""


if __name__ == "__main__":
    import io
    import json
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
