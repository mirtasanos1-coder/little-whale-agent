# -*- coding: utf-8 -*-
"""电脑使用（Computer Use）—— 让小鲸鱼长出一双「手」，但随时能按停。

设计原则（都很土，但每条都是踩出来的）：
1. **三个开关分开管**：键鼠控制 / 代码执行 / 网络冲浪，用户可以在设置里各自关掉。
   关了就是真的不执行，不是「假装执行完再报成功」。
2. **每一步都记账**：动作、参数、耗时、成功与否、截图路径，全写进
   `<数据目录>/agent.db` 的 `agent_actions` 表，界面上的「Agent 日志」就是读它。
3. **紧急停止**：Ctrl+Alt+K 随时随地能把 pyautogui 掐断。剪断之后
   `_STOP` 一直是置位状态，必须显式 `reset_stop()` 才恢复 —— 免得「一紧张
   点了停止，结果下一个循环又自己动起来」。
4. **失败要能自省**：工具返回的是给模型看的一句话（含错误原因），不是堆栈。

这里不依赖 GUI 线程，也不假设鼠标键盘一定存在：所有 pyautogui 调用都包一层
`_guard()`，出错就降级成一句人话。
"""
from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 基础工具


def _cfg(key: str, default: Any = None) -> Any:
    try:
        import config as _c
        c = _c.load()
        if isinstance(c, dict) and key in c:
            return c[key]
    except Exception:
        pass
    return default


def _data_dir() -> str:
    d = os.environ.get("QQ_AGENT_DATA_DIR", "").strip()
    if d:
        return d
    try:
        import app_ui
        return str(app_ui.DATA_DIR)
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".xiaojingyu")


def _db_path() -> str:
    d = os.path.join(_data_dir(), "agent")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "agent.db")


def shots_dir() -> str:
    d = os.path.join(_data_dir(), "agent_shots")
    os.makedirs(d, exist_ok=True)
    return d


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str, level: str = "INFO") -> None:
    try:
        import agent
        agent.log(msg, level)
    except Exception:
        try:
            print("[%s] %s" % (level, msg))
        except Exception:
            pass


# ---------------------------------------------------------------- 紧急停止


class EmergencyStop(RuntimeError):
    """用户按了紧急停止（Ctrl+Alt+K 或界面按钮）。"""


_STOP = threading.Event()
_STOP_REASON = {"why": "", "at": ""}
_HOTKEY_ON = {"on": False, "err": ""}
_HOTKEY_STOP = threading.Event()


# v1.6.3（QA P2-3）：急停时要通知别人（把 Agent job状态改成 aborted）。
#用回调而不是直接 import agent —— computer_use是底层模块，反过来 import会成环。
#钩子抛异常一律吞掉，绝不能影响急停本身。
_STOP_HOOKS = []


def register_stop_hook(fn):
    """注册一个急停回调 fn(why)；返回注销函数。"""
    if not callable(fn):
        return lambda: None
    _STOP_HOOKS.append(fn)

    def _off():
        try:
            _STOP_HOOKS.remove(fn)
        except ValueError:
            pass
    return _off


def stop_now(why: str = "手动停止") -> Dict[str, Any]:
    """立刻切断：之后任何键鼠/代码动作都会抛 EmergencyStop。"""
    _STOP.set()
    _STOP_REASON["why"] = why
    _STOP_REASON["at"] = _now()
    _log("[Agent] 紧急停止：%s" % why, "WARN")
    # v1.6.3（QA P2-3）：通知订阅者（job→aborted，前端才不会一直显示跑到一半）
    for _h in list(_STOP_HOOKS):
        try:
            _h(why)
        except Exception:
            pass
    try:
        import pyautogui
        # 把「按下的键」松开，否则用户会发现 Ctrl 一直粘着
        for k in ("ctrl", "alt", "shift", "win"):
            try:
                pyautogui.keyUp(k)
            except Exception:
                pass
    except Exception:
        pass
    return {"ok": True, "stopped": True, "reason": why}


def reset_stop() -> Dict[str, Any]:
    _STOP.clear()
    _STOP_REASON["why"] = ""
    _STOP_REASON["at"] = ""
    return {"ok": True, "stopped": False}


def stopped() -> bool:
    return _STOP.is_set()


def _check() -> None:
    if _STOP.is_set():
        raise EmergencyStop(_STOP_REASON["why"] or "已紧急停止")


def _guarded(fn):
    """工具函数统一兜住急停：返回人话，别把异常抛给 ReAct 循环或前端。"""
    def _w(*a, **k):
        try:
            return fn(*a, **k)
        except EmergencyStop as e:
            return {"ok": False,
                    "msg": "已被紧急停止：%s（撤销就点界面上的「继续」或按 Ctrl+Alt+K 之外的方式重置）" % e}
    _w.__name__ = getattr(fn, "__name__", "tool")
    _w.__doc__ = getattr(fn, "__doc__", "")
    return _w


def stop_state() -> Dict[str, Any]:
    return {"stopped": _STOP.is_set(), "reason": _STOP_REASON["why"],
            "at": _STOP_REASON["at"], "hotkey": _HOTKEY_ON["on"],
            "hotkey_err": _HOTKEY_ON["err"]}


def install_hotkey() -> Dict[str, Any]:
    """起一个守护线程轮询 Ctrl+Alt+K（不依赖 keyboard 库，免得要管理员权限）。"""
    if _HOTKEY_ON["on"] or sys.platform != "win32":
        return {"ok": _HOTKEY_ON["on"], "msg": "已是开启状态" if _HOTKEY_ON["on"] else "非 Windows，跳过"}
    try:
        import ctypes
        user32 = ctypes.windll.user32
    except Exception as e:
        _HOTKEY_ON["err"] = str(e)
        return {"ok": False, "msg": "拿不到键盘状态：%s" % e}

    VK_CONTROL, VK_MENU, VK_K = 0x11, 0x12, 0x4B

    def _down(vk: int) -> bool:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)

    _HOTKEY_STOP.clear()

    def _loop() -> None:
        was = False
        while not _HOTKEY_STOP.is_set():
            try:
                now = _down(VK_CONTROL) and _down(VK_MENU) and _down(VK_K)
                if now and not was:
                    stop_now("快捷键 Ctrl+Alt+K")
                was = now
            except Exception:
                pass
            time.sleep(0.06)

    threading.Thread(target=_loop, name="agent-hotkey", daemon=True).start()
    _HOTKEY_ON["on"] = True
    return {"ok": True, "msg": "紧急停止快捷键已就绪：Ctrl+Alt+K"}


def uninstall_hotkey() -> Dict[str, Any]:
    """关掉 Agent 时把热键线程收掉（以前是 while True，永远停不下来）。"""
    if not _HOTKEY_ON["on"]:
        return {"ok": True, "msg": "热键监听本来就没开"}
    try:
        _HOTKEY_STOP.set()
    except Exception:
        pass
    _HOTKEY_ON["on"] = False
    return {"ok": True, "msg": "紧急停止快捷键已停用"}


# ---------------------------------------------------------------- 动作账本

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL,
    args TEXT,
    result TEXT,
    ok INTEGER DEFAULT 0,
    ms INTEGER DEFAULT 0,
    screenshot TEXT,
    session TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_ts ON agent_actions(ts);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    for stmt in _SCHEMA.split(";"):
        s = stmt.strip()
        if s:
            c.execute(s)
    c.commit()
    return c


def log_action(kind: str, action: str, args: Any = None, ok: bool = True,
               ms: int = 0, result: str = "", screenshot: str = "",
               session: str = "") -> int:
    try:
        c = _conn()
        cur = c.execute(
            "INSERT INTO agent_actions(ts,kind,action,args,result,ok,ms,screenshot,session)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (_now(), kind, action, json.dumps(args, ensure_ascii=False)[:2000],
             str(result)[:2000], 1 if ok else 0, int(ms), screenshot or "", session or ""))
        c.commit()
        rid = int(cur.lastrowid or 0)
        c.close()
        return rid
    except Exception as e:
        _log("[Agent] 记账失败：%s" % e, "WARN")
        return 0


def recent_actions(limit: int = 60, kind: str = "") -> List[Dict[str, Any]]:
    try:
        c = _conn()
        if kind:
            rows = c.execute(
                "SELECT id,ts,kind,action,args,result,ok,ms,screenshot FROM agent_actions"
                " WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, int(limit))).fetchall()
        else:
            rows = c.execute(
                "SELECT id,ts,kind,action,args,result,ok,ms,screenshot FROM agent_actions"
                " ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        c.close()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            a = json.loads(r[4]) if r[4] else None
        except Exception:
            a = r[4]
        out.append({"id": r[0], "ts": r[1], "kind": r[2], "action": r[3],
                    "args": a, "result": r[5], "ok": bool(r[6]), "ms": r[7],
                    "screenshot": r[8]})
    return out


def clear_actions() -> Dict[str, Any]:
    try:
        c = _conn()
        c.execute("DELETE FROM agent_actions")
        c.commit()
        c.close()
        return {"ok": True, "msg": "Agent 日志已清空"}
    except Exception as e:
        return {"ok": False, "msg": "清空失败：%s" % e}


def action_count() -> int:
    try:
        c = _conn()
        n = c.execute("SELECT COUNT(*) FROM agent_actions").fetchone()[0]
        c.close()
        return int(n or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------- 开关与截图


def _on(key: str, default: bool = False) -> bool:
    v = _cfg(key, default)
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(v)


def shoot(tag: str = "shot") -> str:
    """截一张全屏图（只在键鼠控制开启时真正执行）。返回路径，失败返回空串。"""
    if not _on("agent_keyboard", False):
        return ""
    try:
        _check()
        import pyautogui
        p = os.path.join(shots_dir(), "%s_%s.png" % (time.strftime("%Y%m%d_%H%M%S"), re.sub(r"[^\w\-]+", "_", tag)[:24]))
        pyautogui.screenshot().save(p)
        return p
    except EmergencyStop:
        raise
    except Exception as e:
        _log("[Agent] 截图失败：%s" % e, "WARN")
        return ""


def status() -> Dict[str, Any]:
    try:
        import pyautogui
        size = pyautogui.size()
        screen = [int(size.width), int(size.height)]
        pg_ok = True
    except Exception:
        screen, pg_ok = [], False
    return {
        "keyboard": _on("agent_keyboard", False),
        "web": _on("agent_web", False),
        "code": _on("agent_code", False),
        "auto": _on("agent_auto", False),
        "max_steps": int(_cfg("max_agent_steps", 10) or 10),
        "approval": _on("human_approval", True),
        "pyautogui": pg_ok,
        "screen": screen,
        "shots": len(os.listdir(shots_dir())) if os.path.isdir(shots_dir()) else 0,
        "actions": action_count(),
        "playwright": _playwright_ok(),
        "stop": stop_state(),
    }


# ---------------------------------------------------------------- 网络冲浪

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


_PW_STATE = {"checked": False, "ok": False, "why": ""}


def _playwright_ok() -> bool:
    """Playwright 到底能不能用（含 C 扩展）。

    v1.5.2（QA No-Go 修复三）：以前只 `import playwright` 就算「能用」，
    但打包版常常是「包在、greenlet._greenlet 没打进去」—— 一调用就抛
    `ModuleNotFoundError`，表现成「Agent 搜天气直接崩」。
    这里把 sync_api 和 greenlet._greenlet 一起试，结果缓存起来，
    失败原因也记下来给用户看。
    """
    if _PW_STATE["checked"]:
        return bool(_PW_STATE["ok"])
    why = ""
    ok = False
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        import greenlet._greenlet  # noqa: F401
        ok = True
    except Exception as e:
        why = "%s: %s" % (type(e).__name__, e)
    _PW_STATE.update({"checked": True, "ok": ok, "why": why})
    return ok


def _pw_why() -> str:
    _playwright_ok()
    return _PW_STATE.get("why") or ""


def _html_to_text(html: str) -> str:
    import html as _html

    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    # v1.5.3（QA P2-1）：必须走 html.unescape()。
    # 以前只手换了 &nbsp; / &amp; / &lt; / &gt; / &quot; 这 5 个，
    # 而 Bing 结果页里到处是 `&#0183;`、`&ensp;`、`&#x27;`、`&amp;quot;` 这类实体，
    # 于是模型拿到的标题是「&#0183; 原来你也在这里」—— 读起来像乱码，
    # 用户看到的就是「搜出来的东西不对劲」。
    try:
        # 解两次：页面里常见的 `&amp;quot;` 是「转义过的转义」，
        # 只解一次会留下字符串 `&quot;`，看起来像乱码。
        html = _html.unescape(_html.unescape(html))
    except Exception:
        pass
    # unescape 之后还剩的（不合法实体 / 被转义两次的）统一清掉，别留在标题里
    html = re.sub(r"&#x?[0-9a-fA-F]{1,8};?", " ", html)
    # 全角空格、零宽字符、行/段分隔符 → 普通空格，否则前端和 token 都难受
    html = re.sub(r"[\u2000-\u200f\u2028-\u202f\u205f\u3000\ufeff]", " ", html)
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n{2,}", "\n", html)).strip()


def _http_get(url: str, timeout: int = 15) -> str:
    import ssl
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    })

    def _read(ctx=None):
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as f:
            return f.read()

    try:
        raw = _read()
    except Exception as e:
        # 打包版的 Python 可能拿不到系统根证书（实测这台机器读 example.com 就报
        # CERTIFICATE_VERIFY_FAILED）。这里只是「读公开网页正文」，不发隐私、
        # 不收证书，所以退回「不校验证书」再试一次 —— 总比整个功能不可用强。
        if "CERTIFICATE_VERIFY_FAILED" not in str(e) and "certificate verify" not in str(e):
            raise
        raw = _read(ssl._create_unverified_context())
    try:
        return raw.decode("utf-8")
    except Exception:
        return raw.decode("gbk", "ignore")


# v1.8.1（QA 遗留 P1）：搜索结果里混进来的「备案号 / ICP / 许可证 / Copyright」
# 一行里所有二元组都能跟新闻查询撞上，在相关度打分里能拿高分，冒充成新闻标题。
# 标题命中就直接丢掉，别让它进模型。
_TITLE_BLACKLIST = re.compile(
    r"备案|ICP|许可证|公网安备|增值电信|Copyright|©", re.I)


def _bad_title(title) -> bool:
    t = str(title or "").strip()
    if not t:
        return True
    return bool(_TITLE_BLACKLIST.search(t))


def _http_parse(html: str, engine: str, limit: int) -> List[Dict[str, Any]]:
    """从搜索结果页 HTML 里抠 {title,url,snippet}。"""
    import urllib.parse
    items: List[Dict[str, Any]] = []

    def _clean(u):
        u = (u or "").strip()
        if u.startswith("//"):
            u = "https:" + u
        if u.startswith("/l/?uddg=") or u.startswith("https://duckduckgo.com/l/?uddg="):
            try:
                q = urllib.parse.urlparse(u).query
                u = urllib.parse.unquote(urllib.parse.parse_qs(q).get("uddg", [""])[0] or u)
            except Exception:
                pass
        return u

    if engine in ("bing", "cnbing"):
        for m in re.finditer(r'(?is)<a[^>]+href="(https?://[^"]+)"[^>]*>(.{4,120}?)</a>', html):
            href = _clean(m.group(1))
            title = _html_to_text(m.group(2))
            snip = ""   # v1.8.1：这个正则只有 2 个分组，原来的 m.group(3) 必抛 IndexError
            snip = re.sub(r"^(Bing|网页|快照)\s*", "", snip)[:220]
            if title and href.startswith("http") and not _bad_title(title):
                items.append({"title": title[:120], "url": href, "snippet": snip})
            if len(items) >= limit:
                break
    else:   # ddg
        for m in re.finditer(r'(?is)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html):
            href = _clean(m.group(1))
            title = _html_to_text(m.group(2))
            if title and href.startswith("http") and not _bad_title(title):
                items.append({"title": title[:120], "url": href, "snippet": ""})
            if len(items) >= limit:
                break
    return items


# ============ v1.6.3（QA P1-4）：新闻源 +相关度阈值 ============
# QA现象：搜「今天的热点新闻」返回日历网/黄历/历史上的今天/南山天气/百度百科「今天」。
#根因：①什么查询都先走通用 Bing，没有新闻场景兜底；
#②抓到什么算什么，没有相关度门槛，垃圾也当成功喂给模型。
_NEWS_HINT = re.compile(r'新闻|热点|头条|要闻|快讯|最新消息|发生了什么|时事|热搜|突发事件|今日发生|今天发生|最近发生')

#打分前先去掉的口水词（不承载「在查什么」的信息）
_STOP_WORDS = (
    "今天", "今日", "现在", "帮我", "搜一下",
    "搜索", "查一下", "一下", "最近", "有哪些",
    "什么", "怎么样", "吗", "呢",
)


def _is_news_query(q) -> bool:
    """这句查询是不是在要「新闻/热点」（而不是查资料）。"""
    return bool(_NEWS_HINT.search(str(q or "")))


def _norm_query(t) -> str:
    """去掉标点与口水词，只留「在查什么」。"""
    t = re.sub(r"[\\s\\W_]+", "", str(t or ""))
    for w in _STOP_WORDS:
        t = t.replace(w, "")
    return t


def _news_topic(query) -> str:
    """去掉「新闻/热点」这类泛词后剩下的具体话题（没有就返回空）。"""
    t = _norm_query(query)
    for w in ("新闻", "热点", "头条", "要闻", "快讯", "最新消息",
        "时事", "热搜", "突发事件", "今日发生", "今天发生", "最近发生",
        "发生了什么"):
        t = t.replace(w, "")
    t = t.strip("的了吧呢啊")
    return t if len(t) >=2 else ""


def _news_sources(query) -> list:
    """新闻场景专用源（只放新闻频道，不放通用搜索）。"""
    import urllib.parse as _up
    q = _up.quote(_norm_query(query) or str(query or ""))
    return [
        ("cnbing-news",
            "https://cn.bing.com/news/search?q=%s&qft=interval%%3d%%227%%22"),
        ("baidu-news", "https://news.baidu.com/ns?word=%s&tn=news&rtt=1"),
        ("sogou-news", "https://news.sogou.com/news?query=%s"),
    ]


def _http_parse_news(html: str, limit: int) -> list:
    """从新闻页里抠 {title,url,snippet}：不依赖某家 DOM，只认「链接 +像标题的文字」。"""
    items = []
    seen = set()
    for m in re.finditer(r'(?is)<a[^>]+href="(https?://[^"]+)"[^>]*>(.{4,120}?)</a>', html):
        href = m.group(1)
        title = _html_to_text(m.group(2)).strip()
        if len(title) < 6 or href in seen:
            continue
        if _bad_title(title) or re.search(r"(登录|注册|首页|客户端|下载|更多|下一页|百度首页|帮助)", title):
            continue
        seen.add(href)
        items.append({"title": title[:120], "url": href, "snippet": ""})
        if len(items) >= limit * 2:
            break
    return items


def _relevance_score(query, item) -> float:
    """查询词和一条结果的贴合度（0~1）：二元组重合，标题权重0.7、摘要0.3。"""
    q = _norm_query(query)
    # v1.8.1（QA 遗留 P1）：q 为空说明这句没剩下任何实词，
    # 此时任何结果都跟它「无关」，不许当成满分放行。
    if not q:
        return 0.0
    qg = {q[i:i + 2] for i in range(len(q) - 1)} or {q}

    def _hit(txt):
        t = _norm_query(txt)
        if not t:
            return 0.0
        g = {t[i:i + 2] for i in range(len(t) - 1)} or {t}
        return len(qg & g) / float(max(1, len(qg)))

    title = _hit(item.get("title"))
    snip = _hit(item.get("snippet"))
    return round(max(title,0.7 * title + 0.3 * snip),3)


def _filter_relevant(query, items, min_score: float = 0.10):
    """按相关度过滤，返回 (保留的,丢掉的条数)。"""
    keep, drop = [],0
    for it in (items or []):
        sc = _relevance_score(query, it)
        it["relevance"] = sc
        if sc >= min_score:
            keep.append(it)
        else:
            drop += 1
    keep.sort(key=lambda d: d.get("relevance",0), reverse=True)
    return keep, drop


def _search_fallback(query: str, limit: int = 5, why: str = "") -> Dict[str, Any]:
    """没有 Playwright 时的退路：直接抓搜索结果页 HTML（多引擎**并发**试）。

    v1.5.2：以前只抓 Bing 一家，且**抓不到也回 ok:True**（items 是空数组），
    模型拿到空结果就开始编 —— QA 说的「返回垃圾数据」。
    收尾时又发现串行跑三个引擎会拖到 50 秒以上，改成并发抢第一个有结果的。
    """
    import urllib.parse
    q = urllib.parse.quote(query)
    engines = [
        ("bing", "https://www.bing.com/search?q=%s&setlang=zh-CN" % q),
        ("cnbing", "https://cn.bing.com/search?q=%s" % q),
        ("ddg", "https://html.duckduckgo.com/html/?q=%s" % q),
    ]
    why = why or "没走浏览器"
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        box: List[Dict[str, Any]] = []
        errs: List[str] = []
        with ThreadPoolExecutor(max_workers=len(engines)) as ex:
            futs = {ex.submit(_http_get, u, 6): n for n, u in engines}
            try:
                for fut in as_completed(futs, timeout=9):
                    name = futs[fut]
                    try:
                        html = fut.result()
                        items = _http_parse(html, name, limit)
                    except Exception as e:
                        errs.append("%s: %s" % (name, e))
                        continue
                    if items:
                        box.append({"ok": True, "via": "http:" + name, "query": query,
                                    "items": items, "fallback": True,
                                    "msg": "用 HTTP 抓到了 %d 条结果（%s；%s）"
                                           % (len(items), name, why)})
                        break
                    errs.append("%s: 页面打开了但没解析到结果" % name)
            except Exception as _te:
                # 超时了也别白跑：把已经回来的再看一遍
                errs.append("有引擎太慢（%s）" % type(_te).__name__)
                for fut, name in futs.items():
                    if fut.done() and not fut.cancelled() and not box:
                        try:
                            items = _http_parse(fut.result(), name, limit)
                        except Exception:
                            continue
                        if items:
                            box.append({"ok": True, "via": "http:" + name, "query": query,
                                        "items": items, "fallback": True,
                                        "msg": "用 HTTP 抓到了 %d 条结果（%s；%s）"
                                               % (len(items), name, why)})
                            break
        if box:
            return box[0]
        last = "；".join(errs[-2:]) if errs else "未知原因"
    except Exception as e:
        last = "并发抓取出错：%s" % e
    return {"ok": False, "via": "http", "query": query, "items": [],
            "fallback": True,
            "msg": "搜索没成功（HTTP 兜底也失败：%s）。可以先把「允许联网」关掉，"
                   "或者直接告诉我你已知的信息" % (last or "未知原因")}


_BROWSER_SPEC = [None]   # 记住第一次能用的 launch 参数，免得每次都重试


# v1.6.3（QA P1-4）：新闻场景专用抓取（不走通用搜索，避免抓回黄历/天气/百科）。
def _news_search(query: str, limit: int =5) -> Dict[str, Any]:
    import urllib.parse as _up
    errs = []
    for name, tmpl in _news_sources(query):
        try:
            html = _http_get(tmpl % _up.quote(_norm_query(query) or query), timeout=8)
        except Exception as e:
            errs.append("%s: %s" % (name, e))
            continue
        items = _http_parse_news(html, limit)
        if not items:
            errs.append("%s:页面里没解析到新闻条目" % name)
            continue
        _topic = _news_topic(query)
        if _topic:
            keep, drop = _filter_relevant(_topic, items)
        else:
            #纯泛查询（「今天的热点新闻」）：新闻源给什么就收什么
            keep, drop = list(items),0
        if keep:
            return {
                "ok": True, "via": "news:" + name,
                "query": query, "items": keep[:limit], "dropped": drop,
                "msg": "从 %s抓到 %d条相关新闻（另有 %d条不相关已丢弃）"
                    % (name, min(len(keep), limit), drop),
            }
        errs.append("%s: %d条全都不相关" % (name, len(items)))
    return {"ok": False, "via": "news", "query": query,
        "items": [], "msg": "新闻源都没抓到相关结果（%s）"
        % ("；".join(errs[-2:]) or "未知原因")}


def _pw_timeout_ms(env_key: str, default_ms: int) -> int:
    """读一个超时配置（毫秒）。环境变量优先，写错了就用默认值，别崩。"""
    try:
        v = int(float(os.environ.get(env_key, default_ms)))
    except Exception:
        return int(default_ms)
    return max(1000, min(v,120000))


def _launch_browser(p):
    """开一个浏览器：优先 Playwright 自带的 Chromium，没装就借本机 Edge / Chrome。

    打包版不会把 Chromium 塞进安装包（多 150MB），而普通 Windows 一定有 Edge，
    所以按 chromium → msedge → chrome 的顺序试，谁都开不了才算真失败。
    """
    import os as _os
    if os.environ.get("XJY_BROWSER_CHANNEL"):
        cands = [{"channel": os.environ["XJY_BROWSER_CHANNEL"]}]
    else:
        cands = [{}, {"channel": "msedge"}, {"channel": "chrome"}]
    if _BROWSER_SPEC[0] is not None:
        cands = [_BROWSER_SPEC[0]] + [c for c in cands if c != _BROWSER_SPEC[0]]
    last = None
    for spec in cands:
        try:
            b = p.chromium.launch(headless=True, **spec)
            _BROWSER_SPEC[0] = spec
            return b
        except Exception as e:
            last = e
            continue
    raise RuntimeError("开不了浏览器（Chromium / Edge / Chrome 都试过了）：%s" % last)


def _pw_search_once(url: str, limit: int, engine: str = "bing",
           budget_ms: int =0) -> Tuple[List[Dict[str, Any]], str]:
    """用 Chromium 打开一个搜索结果页，把标题/链接/摘要抠出来。

    返回 (items, 截图路径)。不覆盖 UA —— 这是踩过的坑：带桌面 Chrome 的 UA 时
    Bing 会返回另一种版式，`li.b_algo` 一条都匹配不上，看起来像「浏览器打不开」。
    """
    from playwright.sync_api import sync_playwright
    items: List[Dict[str, Any]] = []
    with sync_playwright() as p:
        b = _launch_browser(p)
        try:
            pg = b.new_page(locale="zh-CN")
            # v1.6.4（QA P1-3）：原来写死8秒，慢网/首包慢必断。改成：
            #超时放宽到 env可调（默认25秒），并且超时后再看一眼页面是不是其实已经开了；
            #真不行才翻倍重试一次。
            _to = _pw_timeout_ms("XJY_SEARCH_GOTO_MS",25000)
            if budget_ms and budget_ms >0:
                # v1.6.4：总预算还剩多少就只给多少，别让一个慢站把整个搜索拖死
                _to = max(3000, min(_to, int(budget_ms)))
            _err = None
            for _try_to in (_to, min(_to *2,60000)):
                try:
                    pg.goto(url, timeout=_try_to, wait_until="domcontentloaded")
                    _err = None
                    break
                except Exception as _ge: # noqa: BLE001
                    _err = _ge
                    try:
                        if pg.url and pg.url != "about:blank":
                            #页面已经跳过去了，只是某个子资源拖住了，直接往下解析
                            _err = None
                            break
                    except Exception:
                        pass
            if _err is not None:
                raise _err
            pg.wait_for_timeout(_pw_timeout_ms("XJY_SEARCH_SETTLE_MS",800))
            if engine == "ddg":
                for el in pg.query_selector_all(".result")[:limit]:
                    a = el.query_selector("a.result__a")
                    if not a:
                        continue
                    title = (a.inner_text() or "").strip()
                    href = a.get_attribute("href") or ""
                    sn = el.query_selector(".result__snippet")
                    snip = (sn.inner_text() if sn else "").strip()
                    if title and href.startswith("http"):
                        items.append({"title": title[:120], "url": href, "snippet": snip[:220]})
            else:
                for el in pg.query_selector_all("li.b_algo")[:limit]:
                    a = el.query_selector("h2 a")
                    if not a:
                        continue
                    title = (a.inner_text() or "").strip()
                    href = a.get_attribute("href") or ""
                    sn = el.query_selector("p")
                    snip = (sn.inner_text() if sn else "").strip()
                    if title and href.startswith("http"):
                        items.append({"title": title[:120], "url": href, "snippet": snip[:220]})
            shot = ""
            if items:
                try:
                    shot = os.path.join(shots_dir(), "web_%s_%s.png" % (
                        re.sub(r"\W+", "_", engine)[:10], time.strftime("%Y%m%d_%H%M%S")))
                    pg.screenshot(path=shot)
                except Exception:
                    shot = ""
            return items, shot
        finally:
            try:
                b.close()
            except Exception:
                pass


@_guarded
def tool_web_search(query: str, limit: int = 5, session: str = "") -> Dict[str, Any]:
    """搜索引擎。优先 Playwright headless（能拿到现代页面），没有就退回 HTTP。"""
    t0 = time.time()
    if not _on("agent_web", False):
        r = {"ok": False, "msg": "网络冲浪开关是关的（设置 → Agent 设置 → 允许联网）"}
        log_action("web", "web_search", {"query": query}, False, 0, r["msg"], session=session)
        return r
    query = (query or "").strip()
    if not query:
        return {"ok": False, "msg": "要搜什么？给我一句话"}
    _check()
    res: Dict[str, Any] = {"ok": False, "msg": "没搜成"}

    # ---- 第一步：HTTP 并发抓（快；绝大多数查询 2~4 秒就有结果）
    # v1.6.3（QA P1-4）：要的是新闻就走新闻源 ——别拿黄历/天气/百科充数
    if _is_news_query(query):
        try:
            _nr = _news_search(query, limit)
        except Exception as _e:
            _nr = {"ok": False, "msg": "新闻抓取出错：%s" % _e}
        if _nr.get("ok") and _nr.get("items"):
            res = _nr
            log_action("web", "web_search", {"query": query, "via": _nr.get("via")}, True, int((time.time() - t0) *1000), _nr.get("msg", ""), "", session=session)
            return _nr
    try:
        fb = _search_fallback(query, limit, why="先走 HTTP")
        if fb.get("ok") and fb.get("items"):
            res = fb
    except Exception as e:
        res = {"ok": False, "msg": "HTTP 抓取失败：%s" % e, "via": "http"}

    # ----第二步：HTTP没拿到，才上浏览器（慢，给个总预算）
    if not res.get("ok") and _playwright_ok():
        # v1.6.4（QA P1-3）：原来总预算16秒、单个goto8秒，慢网必然全军覆没。
        #现在：总预算默认40秒，每个站按「剩余预算」动态分配，出结果就收工。
        try:
            _budget = float(os.environ.get("XJY_SEARCH_BUDGET", "40"))
        except Exception:
            _budget =40.0
        _budget = max(10.0, min(_budget,120.0))
        _deadline = time.time() + _budget
        _bw_err = ""
        engines = [
            ("bing", "https://www.bing.com/search?q=%s&setlang=zh-CN"),
            ("cnbing", "https://cn.bing.com/search?q=%s"),
            ("ddg", "https://html.duckduckgo.com/html/?q=%s"),
        ]
        for _i, (ename, tmpl) in enumerate(engines):
            _left = _deadline - time.time()
            if _left <=3.0:
                _bw_err = _bw_err or ("浏览器太慢（%d秒内没搜完）" % int(_budget))
                break
            #剩下的站平分剩余时间，每个站至少6秒（不然还没加载就被砍）
            _share = max(6000, int(_left *1000 / max(1, len(engines) - _i)))
            try:
                import urllib.parse as _up
                items, shot = _pw_search_once(tmpl % _up.quote(query), limit, ename,
                    budget_ms=_share)
            except EmergencyStop:
                raise
            except Exception as e:
                _bw_err = "浏览器打不开：%s" % e
                continue
            if items:
                res = {"ok": True, "via": "playwright:" + ename, "query": query,
                    "items": items, "screenshot": shot,
                    "msg": "在浏览器里搜到 %d条结果（%s）" % (len(items), ename)}
                break
            _bw_err = "浏览器把页面打开了，但没解析到结果标题"
        if not res.get("ok"):
            why = _bw_err or _pw_why() or "浏览器没搜到"
            res = {"ok": False, "via": "playwright", "query": query, "items": [],
                "msg": "没搜到结果（%s）" % why}

    # v1.6.3（QA P1-4）：通用路径也过一道相关度门槛 ——
    #宁可老实说「没搜到靠谱结果」，也别把黄历/天气塞给模型强行编答案。
    if res.get("ok") and res.get("items"):
        _keep, _drop = _filter_relevant(query, res.get("items"))
        res["items"] = _keep
        res["dropped"] = _drop
        if not _keep:
            _note = ("（抓到 %d条，全都跟问题对不上，已全部丢弃）" % _drop) if _drop else ""
            res = {"ok": False, "via": res.get("via"),
              "query": query, "items": [], "dropped": _drop,
              "msg": "没搜到靠谱结果" + _note}
    ms = int((time.time() - t0) * 1000)
    log_action("web", "web_search", {"query": query, "via": res.get("via")},
               bool(res.get("ok")), ms, res.get("msg", ""),
               res.get("screenshot", ""), session=session)
    return res


@_guarded
def tool_open_url(url: str, wait_ms: int = 1200, session: str = "") -> Dict[str, Any]:
    """打开一个网页，抓标题/正文摘要/截图（用来做「真的去浏览一下」的验证）。"""
    t0 = time.time()
    if not _on("agent_web", False):
        return {"ok": False, "msg": "网络冲浪开关是关的"}
    if not re.match(r"^https?://", url or ""):
        return {"ok": False, "msg": "网址要以 http:// 或 https:// 开头"}
    _check()

    # ---- V1.9.10：视频平台走专用解析 ----
    # B 站/抖音/YouTube 的页面基本是空壳（正文靠 JS 渲染），光读 HTML 常常一个字都没有。
    # 这里先用平台接口 / og 标签拿到标题、UP 主、简介、封面，比"打开网页"有用得多。
    try:
        import videocatch as _vc
        if _vc.detect(url)[0]:
            _vi = _vc.fetch(url, timeout=10)
            if _vi.get("ok"):
                ms = int((time.time() - t0) * 1000)
                r = {"ok": True, "title": _vi.get("title", ""),
                     "text": _vc.describe_for_prompt(_vi, "网页"),
                     "screenshot": "", "url": _vi.get("url") or url,
                     "via": "video", "video": _vi, "platform": _vi.get("platform"),
                     "cover": _vi.get("cover", ""),
                     "msg": _vi.get("msg") or ("视频解析成功（%d 毫秒）" % ms)}
                try:
                    log_action("web", "open_url", {"url": url, "via": "video"}, True, ms,
                               r["msg"], "", session=session)
                except Exception:
                    pass
                return r
    except Exception as _e_v:
        try:
            log("  [open_url] 视频解析失败，改走普通网页：%s" % _e_v, "DEBUG")
        except Exception:
            pass

    # ---- 第一步（v1.5.3 / QA P2-2）：先用 HTTP 直读，1~2 秒出正文。
    # 以前不管什么站都先起 Playwright：冷启动 3~8 秒 + goto 25 秒超时，
    # 用户体感就是「点一下卡半天」，QA 记成「响应迟钝」。
    # 要强制走浏览器（比如就是要截图）设 XJY_OPEN_URL_BROWSER=1。
    http_err = ""
    if os.environ.get("XJY_OPEN_URL_BROWSER", "") != "1":
        try:
            html = _http_get(url, timeout=8)
            title = ""
            m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
            if m:
                title = _html_to_text(m.group(1))[:120]
            text = _html_to_text(html)
            # 80 字就够了：example.com 这种极简页也有 140 字；
            # 低于 80 字的八成是 JS 壳子或拦截页，那才值得掏浏览器。
            if len(text) >= 80:
                ms = int((time.time() - t0) * 1000)
                r = {"ok": True, "title": title, "text": text[:1500], "screenshot": "",
                     "url": url, "via": "http",
                     "msg": "页面读完了（HTTP 直读 %d 毫秒）：%s" % (ms, title[:60] or url)}
                log_action("web", "open_url", {"url": url, "via": "http"}, True, ms,
                           r["msg"], "", session=session)
                return r
            http_err = "HTTP 只读到 %d 个字（大概是 JS 渲染的站）" % len(text)
        except Exception as e:
            http_err = "HTTP 读取失败：%s" % e

    if not _playwright_ok():
        # 没浏览器也要能「读一下这个网页」，别让功能整个不可用
        try:
            html = _http_get(url, timeout=10)
            title = ""
            m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
            if m:
                title = _html_to_text(m.group(1))[:120]
            text = _html_to_text(html)[:1500]
            ms = int((time.time() - t0) * 1000)
            r = {"ok": bool(text), "title": title, "text": text, "screenshot": "",
                 "url": url, "via": "http",
                 "msg": ("没走浏览器（%s），用 HTTP 读到了正文：%s"
                         % (_pw_why() or "Playwright 不可用", title[:60] or url))
                        if text else "这个页面用 HTTP 也没读到正文（%s）" % (http_err or "空页面")}
            log_action("web", "open_url", {"url": url, "via": "http"}, bool(r["ok"]),
                       ms, r["msg"], "", session=session)
            return r
        except Exception as e:
            r = {"ok": False, "msg": "打不开网页：%s（浏览器也不可用：%s）"
                                     % (e, _pw_why() or "未知")}
            log_action("web", "open_url", {"url": url}, False, 0, r["msg"], session=session)
            return r
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = _launch_browser(p)
            pg = b.new_page(user_agent=_UA, locale="zh-CN")
            # 超时 25 秒 → 12 秒：慢站宁可早点失败，也别让用户干等
            pg.goto(url, timeout=12000, wait_until="domcontentloaded")
            pg.wait_for_timeout(int(wait_ms))
            title = pg.title() or ""
            text = (pg.inner_text("body") or "")[:1500]
            shot = os.path.join(shots_dir(), "page_%s.png" % time.strftime("%Y%m%d_%H%M%S"))
            try:
                pg.screenshot(path=shot, full_page=False)
            except Exception:
                shot = ""
            b.close()
        ms = int((time.time() - t0) * 1000)
        r = {"ok": True, "title": title, "text": text, "screenshot": shot, "url": url,
             "via": "playwright",
             "msg": "页面打开了（浏览器渲染 %d 毫秒）：%s" % (ms, title[:60] or url)}
        log_action("web", "open_url", {"url": url, "via": "playwright"}, True, ms,
                   r["msg"], shot, session=session)
        return r
    except EmergencyStop:
        raise
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        r = {"ok": False, "msg": "打开失败：%s%s" % (e, ("（%s）" % http_err) if http_err else "")}
        log_action("web", "open_url", {"url": url}, False, ms, r["msg"], session=session)
        return r


# ---------------------------------------------------------------- 玩（键鼠）


@_guarded
def tool_play_game(action: str = "screenshot", **kw) -> Dict[str, Any]:
    """键鼠动作：click / move / type / key / scroll / screenshot。

    每次动作前都问一句 _check()：紧急停止之后一个键都不会再按。
    """
    t0 = time.time()
    if not _on("agent_keyboard", False):
        return {"ok": False, "msg": "键鼠控制是关的（设置 → Agent 设置 → 允许控制键鼠）"}
    _check()
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
    except Exception as e:
        return {"ok": False, "msg": "这台机器没有可用的图形界面：%s" % e}
    act = (action or "screenshot").lower()
    msg = ""
    shot = ""
    try:
        if act in ("click", "tap"):
            x, y = int(kw.get("x", 0)), int(kw.get("y", 0))
            if not (0 <= x < 10000 and 0 <= y < 10000):
                return {"ok": False, "msg": "坐标不合法"}
            _check()
            pyautogui.moveTo(x, y, duration=0.15)
            pyautogui.click()
            msg = "点了 (%d, %d)" % (x, y)
        elif act == "move":
            x, y = int(kw.get("x", 0)), int(kw.get("y", 0))
            _check()
            pyautogui.moveTo(x, y, duration=0.2)
            msg = "鼠标移到 (%d, %d)" % (x, y)
        elif act == "type":
            text = str(kw.get("text", ""))
            if len(text) > 2000:
                return {"ok": False, "msg": "一次输入太长了（上限 2000 字）"}
            _check()
            pyautogui.typewrite(text, interval=0.02) if text.isascii() else _paste_text(text)
            msg = "输入了 %d 个字" % len(text)
        elif act == "key":
            keys = str(kw.get("keys", "")).strip()
            if not keys:
                return {"ok": False, "msg": "要按哪个键？比如 enter / ctrl+a"}
            _check()
            if "+" in keys:
                pyautogui.hotkey(*[k.strip() for k in keys.split("+") if k.strip()])
            else:
                pyautogui.press(keys)
            msg = "按了 %s" % keys
        elif act == "scroll":
            _check()
            pyautogui.scroll(int(kw.get("amount", -400)))
            msg = "滚动了 %s" % kw.get("amount", -400)
        elif act == "screenshot":
            msg = "截了张图"
        else:
            return {"ok": False, "msg": "不认识的动作：%s" % act}
        shot = shoot(act) if act != "screenshot" else shoot("screen")
        if act == "screenshot" and not shot:
            return {"ok": False, "msg": "截图失败"}
        ms = int((time.time() - t0) * 1000)
        log_action("keyboard", act, kw, True, ms, msg, shot, session=str(kw.get("session", "")))
        return {"ok": True, "msg": msg, "screenshot": shot}
    except EmergencyStop:
        raise
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        log_action("keyboard", act, kw, False, ms, str(e), session=str(kw.get("session", "")))
        return {"ok": False, "msg": "动作没做成：%s" % e}


def _paste_text(text: str) -> None:
    """中文没法用 typewrite 打，走剪贴板 + Ctrl+V。"""
    import ctypes
    import pyautogui
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, (len(text) + 1) * 2)
    p = kernel32.GlobalLock(ctypes.c_void_p(h))
    ctypes.memmove(ctypes.c_void_p(p), text.encode("utf-16-le"), len(text) * 2 + 2)
    kernel32.GlobalUnlock(ctypes.c_void_p(h))
    if user32.OpenClipboard(None):
        user32.EmptyClipboard()
        user32.SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(h))
        user32.CloseClipboard()
    pyautogui.hotkey("ctrl", "v")


# ---------------------------------------------------------------- 执行代码

_ALLOWED_MODULES = {"math", "random", "json", "re", "time", "datetime", "statistics", "itertools", "collections"}
_BANNED_NAMES = {"eval", "exec", "compile", "open", "input", "__import__", "globals", "locals",
                 "vars", "getattr", "setattr", "delattr", "breakpoint", "help", "exit", "quit"}
_BANNED_ATTRS = {"__globals__", "__subclasses__", "__builtins__", "__class__", "__code__",
                 "__closure__", "__loader__", "__mro__", "__dict__", "__getattribute__", "__init_subclass__"}
_BANNED_NODES = (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith,
                 ast.Try, ast.Lambda, ast.ClassDef)


class _SafeVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.errors: List[str] = []

    def visit_Import(self, node) -> None:  # noqa: N802
        for a in node.names:
            if a.name.split(".")[0] not in _ALLOWED_MODULES:
                self.errors.append("不许 import %s" % a.name)

    def visit_ImportFrom(self, node) -> None:  # noqa: N802
        if (node.module or "").split(".")[0] not in _ALLOWED_MODULES:
            self.errors.append("不许 from %s import" % node.module)

    def visit_Name(self, node) -> None:  # noqa: N802
        if node.id in _BANNED_NAMES:
            self.errors.append("不许用 %s" % node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node) -> None:  # noqa: N802
        if node.attr in _BANNED_ATTRS or node.attr.startswith("__"):
            self.errors.append("不许访问 %s" % node.attr)
        self.generic_visit(node)

    def visit_While(self, node) -> None:  # noqa: N802
        self.errors.append("循环请用 for（while 容易写死循环）")


@_guarded
def tool_execute_code(code: str, session: str = "") -> Dict[str, Any]:
    """在沙箱里跑一小段 Python：只放行数学/字符串/列表这类纯计算。"""
    t0 = time.time()
    if not _on("agent_code", False):
        return {"ok": False, "msg": "代码执行是关的（设置 → Agent 设置 → 允许执行代码）"}
    code = (code or "").strip()
    if not code:
        return {"ok": False, "msg": "要跑什么代码？"}
    if len(code) > 4000:
        return {"ok": False, "msg": "代码太长了（上限 4000 字符）"}
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as e:
        r = {"ok": False, "msg": "语法错误：%s（第 %s 行）" % (e.msg, e.lineno)}
        log_action("code", "execute", {"code": code[:400]}, False, 0, r["msg"], session=session)
        return r
    vis = _SafeVisitor()
    vis.visit(tree)
    if vis.errors:
        msg = "不让执行：" + "；".join(dict.fromkeys(vis.errors))[:300]
        log_action("code", "execute", {"code": code[:400]}, False, 0, msg, session=session)
        return {"ok": False, "msg": msg}
    _check()
    buf = io.StringIO()
    safe_builtins = {
        "print": print, "len": len, "range": range, "sum": sum, "min": min, "max": max,
        "abs": abs, "round": round, "sorted": sorted, "enumerate": enumerate, "zip": zip,
        "list": list, "dict": dict, "set": set, "tuple": tuple, "str": str, "int": int,
        "float": float, "bool": bool, "isinstance": isinstance, "repr": repr, "map": map,
        "filter": filter, "any": any, "all": all, "pow": pow, "divmod": divmod,
    }
    try:
        import math as _m
        import random as _r
        import json as _j
        import re as _re
        import statistics as _st
        import itertools as _it
        import collections as _co
        import datetime as _dt
        env = {"__builtins__": safe_builtins, "math": _m, "random": _r, "json": _j,
               "re": _re, "statistics": _st, "itertools": _it, "collections": _co,
               "datetime": _dt, "time": time}
        with redirect_stdout(buf):
            exec(compile(tree, "<agent_code>", "exec"), env, env)  # noqa: S102
        out = buf.getvalue().strip()
        ms = int((time.time() - t0) * 1000)
        r = {"ok": True, "output": out[:4000], "msg": ("输出 %d 字符" % len(out)) if out else "跑完了，没有输出"}
        log_action("code", "execute", {"code": code[:400]}, True, ms, (out[:800] or r["msg"]), session=session)
        return r
    except EmergencyStop:
        raise
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        tail = traceback.format_exc(limit=2).strip().splitlines()[-1]
        r = {"ok": False, "msg": "运行出错：%s" % tail, "output": buf.getvalue()[:1000]}
        log_action("code", "execute", {"code": code[:400]}, False, ms, r["msg"], session=session)
        return r


# ---------------------------------------------------------------- 工具清单


# 允许 Agent 代用户改的控制台设置（白名单：只动「行为开关」，不碰密钥、路径、账号）
_SETTABLE: Dict[str, tuple] = {
    "draw_enabled": ("画图开关", bool),
    "imagegen_via": ("出图方式（auto/images/chat/svg）", str),
    "humanize_enabled": ("假装打字", bool),
    "humanize_split": ("长回复拆条", bool),
    "quote_enabled": ("引用回复", bool),
    "care_plan_enabled": ("主动关心", bool),
    "moment_enabled": ("自动说说", bool),
    "affinity_enabled": ("好感度", bool),
    "tts_enabled": ("语音消息", bool),
}


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in ("1", "true", "on", "yes", "y", "开", "打开", "开启", "启用", "是", "好")


@_guarded
def tool_set_config(key: str = "", value: Any = None, session: str = "") -> Dict[str, Any]:
    """帮用户改控制台里的行为开关（比如「把生图打开」）。只认白名单里的键。"""
    k = str(key or "").strip()
    if k not in _SETTABLE:
        return {"ok": False, "msg": "这个设置我不能改：%s。能改的有：%s" % (
            k or "(空)", "、".join(sorted(_SETTABLE)))}
    label, kind = _SETTABLE[k]
    try:
        import config as _cfg_mod
        if kind is bool:
            v = _truthy(value)
            _cfg_mod.set_checked(k, v)
            shown = "打开" if v else "关掉"
        else:
            v = str(value or "").strip()
            _cfg_mod.set_checked(k, v)
            shown = v
        log_action("config", "set", {k: value}, True, 0,
                   "%s → %s" % (label, shown), session=session)
        _log("[Agent] 改了设置：%s = %s" % (label, shown))
        return {"ok": True, "msg": "已经帮你把「%s」%s了" % (label, shown), "key": k, "value": v}
    except Exception as e:
        log_action("config", "set", {k: value}, False, 0, str(e), session=session)
        return {"ok": False, "msg": "改不了：%s" % e}


def tool_change_avatar(path: str, session: str = "") -> Dict[str, Any]:
    """v1.8.12：换 QQ 头像（走 NapCat 的 set_qq_avatar）。"""
    try:
        import selfmod
        r = selfmod.set_avatar(path)
    except Exception as e:
        r = {"ok": False, "msg": "换头像失败：%s" % e}
    log_action("avatar", "set", {"path": str(path)[:60]}, bool(r.get("ok")),
        0, r.get("msg", ""), session=session)
    return r


def tool_sing(name: str, session: str = "") -> Dict[str, Any]:
    """v1.8.12：真唱。合成就返回文件路径，交给上层发语音。"""
    try:
        import sing as _s
        r = _s.sing(str(name or ""))
    except Exception as e:
        r = {"ok": False, "note": "唱歌模块不可用：%s" % e, "path": ""}
    return {"ok": bool(r.get("ok")), "msg": r.get("note") or "",
        "path": r.get("path") or "", "engine": r.get("engine") or ""}


def tool_update_profile(avatar_path: str = "", nickname: str = "",
                        secret: bool = False, session: str = "") -> Dict[str, Any]:
    """v1.9.10：换头像 / 改昵称（24 小时冷却；secret=True 时"悄悄换"）。

    secret 只是**通用能力**：谁用、怎么用，由模型按当前人设与三观自行决定 ——
    有的性格会「明面上拒绝、私下照办」，也有的会直接换或直接拒绝；
    代码和提示词都不预设哪种人设该用它。返回值里只陈述事实「对方还不知道」。
    """
    try:
        import selfmod as _sm
        r = _sm.update_profile(avatar_path=avatar_path, nickname=nickname,
                               secret=bool(secret))
    except Exception as e:
        r = {"ok": False, "msg": "换资料失败：%s" % e, "secret": bool(secret)}
    try:
        log_action("avatar", "update_profile",
                   {"secret": bool(secret), "kind": r.get("kind") or ""},
                   bool(r.get("ok")), 0, r.get("msg", ""), session=session)
    except Exception:
        pass
    out = {"ok": bool(r.get("ok")), "msg": r.get("msg") or "",
           "kind": r.get("kind") or "", "secret": bool(r.get("secret"))}
    if r.get("cooldown"):
        out["cooldown"] = int(r["cooldown"])
    if r.get("note"):
        # 给模型看的那句话（不要给用户看）
        out["status"] = "success"
        out["note"] = r["note"]
    return out


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "web_search",
        "when": "需要查实时信息（天气、新闻、价格、资料）时",
        "args": {"query": "要搜的关键词", "limit": "可选，默认 5"},
        "fn": lambda a, s="": tool_web_search(a.get("query", ""), int(a.get("limit", 5) or 5), session=s),
    },
    {
        "name": "open_url",
        "when": "需要打开某个具体网页看看内容时",
        "args": {"url": "http(s):// 开头的完整网址"},
        "fn": lambda a, s="": tool_open_url(a.get("url", ""), session=s),
    },
    {
        "name": "play_game",
        "when": "需要动鼠标键盘（点按、输入、滚动、截图）时",
        "args": {"action": "click|move|type|key|scroll|screenshot", "x": "坐标", "y": "坐标",
                 "text": "要输入的字", "keys": "要按的键", "amount": "滚动量"},
        "fn": lambda a, s="": tool_play_game(session=s, **a),
    },
    {
        "name": "execute_code",
        "when": "需要算数、处理文本、跑一小段 Python 时",
        "args": {"code": "python 代码，最后用 print 输出结果"},
        "fn": lambda a, s="": tool_execute_code(a.get("code", ""), session=s),
    },
    {
        "name": "set_config",
        "when": "用户让你打开/关掉控制台里的某个功能（生图、自动说说、主动关心、语音…）时",
        "args": {"key": "设置名（如 draw_enabled）", "value": "打开/关掉 或具体值"},
        "fn": lambda a, s="": tool_set_config(a.get("key", ""), a.get("value"), session=s),
    },
    {
        "name": "change_avatar",
        "when": "用户明确让你换 QQ 头像、并且已经有一张图片的本地路径时",
        "args": {"path": "图片的本地绝对路径（png/jpg，小于 3MB）"},
        "fn": lambda a, s="": tool_change_avatar(a.get("path", ""), session=s),
    },
    {
        "name": "sing",
        "when": "用户点歌、让你唱一段时（会真合成一段音频发出去）",
        "args": {"name": "歌名，或直接给歌词第一句"}, 
        "fn": lambda a, s="": tool_sing(a.get("name", ""), session=s),
    },
    {
        "name": "update_profile",
        "when": ("用户让你换 QQ 头像或改昵称时。换不换、怎么回，按你的人设与三观自行决定："
                 "可以直接换，可以直接拒绝；也可以选「不想明面上答应、但心里想换」这种演法 —— "
                 "嘴上按人设拒绝，同时把 secret 设成 true（悄悄换，对方不会收到提示）"),
        "args": {"avatar_path": "头像图片：用户发来的图（data URL）或本地绝对路径",
                 "nickname": "要改成的新昵称",
                 "secret": "true=悄悄换（对方不会收到提示；是否这样做由你的人设决定），false/不填=正常换"},
        "fn": lambda a, s="": tool_update_profile(
            a.get("avatar_path", ""), a.get("nickname", ""),
            bool(a.get("secret")), session=s),
    },

]


def tool_names() -> List[str]:
    return [t["name"] for t in TOOLS]


def find_tool(name: str) -> Optional[Dict[str, Any]]:
    n = (name or "").strip().lower()
    for t in TOOLS:
        if t["name"] == n:
            return t
    return None


def run_tool(name: str, args: Dict[str, Any], session: str = "") -> Dict[str, Any]:
    t = find_tool(name)
    if not t:
        return {"ok": False, "msg": "没有这个工具：%s（现有：%s）" % (name, "、".join(tool_names()))}
    try:
        return t["fn"](args or {}, s=session)
    except EmergencyStop as e:
        return {"ok": False, "msg": "已被紧急停止：%s" % e}
    except Exception as e:
        return {"ok": False, "msg": "工具执行失败：%s" % e}


def tools_prompt() -> str:
    """给模型看的工具说明书（塞进 system prompt）。"""
    lines = ["你可以使用下面这些工具。需要动手时，**只输出一行**：", "",
             'ACTION: 工具名 {"参数": "值"}', "",
             "拿到结果后我会把 OBSERVATION 给你。做完就输出 FINAL: 你的最终回答。", ""]
    for t in TOOLS:
        lines.append("- %s：%s；参数：%s" % (
            t["name"], t["when"], json.dumps(t["args"], ensure_ascii=False)))
    return "\n".join(lines)
