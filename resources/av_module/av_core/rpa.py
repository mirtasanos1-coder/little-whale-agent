"""RPA：盯住 QQ / 微信，来电自动接听；两条路可热切换。

为什么两条路
------------
1. **UI Automation（首选）**：拿到的是控件树，按钮有名字可匹配，不受分辨率、主题、
   DPI 影响，也不动鼠标。前提是被控软件真的把 UI 暴露给了 UIA：
   微信 4.x（Qt）一般可以；QQ NT（Electron）多数版本能拿到窗口但**子控件几乎为空**。
2. **图像识别（兜底）**：UIA 探到 0 个按钮时，截窗口区域 → 与预存的按钮小图做模板
   匹配（`opencv`）→ 命中就用 `pyautogui.click(x, y)` 点下去。缺点是依赖模板图、
   受主题/分辨率影响；优点是**自绘 UI 也能点**。

切换是每轮自动判断的（`rpa.image_fallback`），不需要重启服务：
某一轮 UIA 找到"接听/挂断"就用 UIA，找不到且存在候选窗口就走图像匹配。

安全设计（重要）
----------------
* 只处理标题/类名命中 `rpa.window_keywords` 的窗口，别的地方一律不点。
* **自动挂断默认关闭**（`rpa.auto_hangup=false`）：接听成功后再点"挂断"就把通话掐了，
  所以挂断只在显式开启时才动，并且接听后 `hangup_grace_s`（默认 15s）内绝不点。
* 同一个按钮有冷却时间（接听 5s / 挂断 8s），防止连点成"接听→挂断→接听"。
* `dry_run=True` 时只记录"本来要点哪"，绝不真点（第一次接入一定先用它跑一遍）。

诊断
----
* `scan()`：dump 所有窗口的控件树 + 命中词，用来确认 UIA 到底能不能取到按钮。
* `test_match()`：只做模板匹配**不点击**，返回命中分数与坐标，用来调模板图。
* `snapshot()`：把当前屏幕/窗口截图存盘，方便你裁模板。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as cfgmod
from . import screen as screenmod
from .events import BUS

BUTTON_TYPES = {
    "ButtonControl", "TextControl", "ImageControl", "PaneControl",
    "HyperlinkControl", "CustomControl", "GroupControl", "SplitButtonControl",
}


_last_import_error: str | None = None


def available() -> bool:
    global _last_import_error
    try:
        import uiautomation  # noqa: F401

        _last_import_error = None
        return True
    except Exception as exc:  # noqa: BLE001
        _last_import_error = f"{type(exc).__name__}: {exc}"
        return False


def install_hint() -> str:
    if _last_import_error:
        return (f"uiautomation 导入失败（{_last_import_error}）。"
                f"若确实没装: pip install uiautomation comtypes")
    return "未安装 uiautomation。执行: pip install uiautomation comtypes"


def image_available() -> tuple[bool, str]:
    return screenmod.available()


def image_install_hint() -> str:
    return screenmod.install_hint()


def template_root() -> Path:
    """模板目录的绝对路径（config 里是相对项目根写的）。"""
    cfg = cfgmod.load()
    p = Path(cfg.rpa.template_dir)
    return p if p.is_absolute() else (cfgmod.ROOT / p)


@dataclass
class RpaEvent:
    ts: float
    action: str
    window: str
    control: str
    state: str
    clicked: bool
    ok: bool = True
    error: str = ""


class CallWatcher:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state = "idle"          # idle | ringing | in_call
        self._mode = "none"           # none | uia | image
        self._error: str | None = None
        self._log: list[dict] = []
        self._last_click: dict[tuple, float] = {}
        self._current_window = ""
        self._polls = 0
        self._clicks = 0
        self._image_clicks = 0
        self._accepted_at = 0.0
        self._last_match: dict | None = None
        self._last_scan: list[dict] = []
        self._store: screenmod.TemplateStore | None = None
        self._store_key: tuple | None = None

    # ------------------------------------------------------------ 生命周期
    def start(self) -> dict:
        cfg = cfgmod.load().rpa
        if not available():
            self._error = install_hint()
            return self.status()
        with self._lock:
            if self.running:
                return self.status()
            self._stop.clear()
            self._error = None
            self._thread = threading.Thread(target=self._run, name="av-rpa", daemon=True)
            self._thread.start()
        BUS.publish("rpa", action="start", dry_run=cfg.dry_run,
                    image_fallback=cfg.image_fallback)
        return self.status()

    def stop(self) -> dict:
        self._stop.set()
        t = self._thread
        if t:
            t.join(timeout=4.0)
        with self._lock:
            self._thread = None
            self._state = "idle"
            self._mode = "none"
        BUS.publish("rpa", action="stop")
        return self.status()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    # ------------------------------------------------------------ 主循环
    def _run(self) -> None:
        try:
            import uiautomation as auto
        except Exception as exc:  # noqa: BLE001
            self._error = f"{install_hint()} ({type(exc).__name__}: {exc})"
            return
        try:
            auto.SetGlobalSearchTimeout(0.6)
            if hasattr(auto, "InitializeUIAutomationInCurrentThread"):
                auto.InitializeUIAutomationInCurrentThread()
        except Exception:
            pass

        cfg = cfgmod.load()
        interval = max(0.2, float(cfg.rpa.poll_interval))
        while not self._stop.is_set():
            try:
                self._poll(auto)
            except Exception as exc:  # noqa: BLE001
                self._error = f"轮询异常: {type(exc).__name__}: {exc}"
            self._polls += 1
            self._stop.wait(interval)
        try:
            if hasattr(auto, "UninitializeUIAutomationInCurrentThread"):
                auto.UninitializeUIAutomationInCurrentThread()
        except Exception:
            pass

    def _candidate_windows(self, auto) -> list:
        root = auto.GetRootControl()
        wins = []
        try:
            children = root.GetChildren()
        except Exception:
            return []
        for w in children:
            try:
                name = (w.Name or "").strip()
                cls = (w.ClassName or "").strip()
            except Exception:
                continue
            if not name:
                continue
            wins.append((w, name, cls))
        return wins

    def _match_window(self, name: str, cls: str) -> bool:
        cfg = cfgmod.load().rpa
        blob = f"{name} {cls}".lower()
        return any(k.lower() in blob for k in cfg.window_keywords)

    def _walk(self, ctrl, depth: int, out: list, max_depth: int = 7) -> None:
        if depth > max_depth:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            try:
                if not c.Name:
                    continue
                ctype = c.ControlTypeName
                if ctype in BUTTON_TYPES:
                    out.append(c)
                self._walk(c, depth + 1, out, max_depth)
            except Exception:
                continue

    def _find_label(self, controls: list, labels: list[str]) -> Any | None:
        """精确名优先，其次去空格包含匹配。"""
        lowered = {lbl.lower(): lbl for lbl in labels}
        exact = []
        loose = []
        for c in controls:
            try:
                name = (c.Name or "").strip()
            except Exception:
                continue
            low = name.lower().replace(" ", "")
            if low in lowered:
                exact.append(c)
            elif any(low == lbl.lower().replace(" ", "") for lbl in labels):
                exact.append(c)
            elif any(lbl.lower().replace(" ", "") in low for lbl in labels) and len(low) <= 12:
                loose.append(c)
        picked = exact or loose
        return picked[0] if picked else None

    def _invoke(self, ctrl) -> tuple[bool, str]:
        """优先用 UIA 的 Invoke 模式（不动鼠标），退化为鼠标点击。"""
        try:
            pat = ctrl.GetInvokePattern()
            if pat:
                pat.Invoke()
                return True, "invoke"
        except Exception:
            pass
        try:
            pat = ctrl.GetLegacyIAccessiblePattern()
            if pat:
                pat.DoDefaultAction()
                return True, "legacy"
        except Exception:
            pass
        try:
            ctrl.Click(simulateMove=False)
            return True, "click"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    def _cooldown_ok(self, key: tuple, seconds: float = 5.0) -> bool:
        now = time.time()
        last = self._last_click.get(key, 0.0)
        if now - last < seconds:
            return False
        self._last_click[key] = now
        return True

    def _record(self, action: str, window: str, control: str, state: str,
                clicked: bool, ok: bool = True, error: str = "", **extra) -> None:
        cfg = cfgmod.load().rpa
        ev = {
            "ts": round(time.time(), 3),
            "action": action,
            "window": window[:80],
            "control": control[:40],
            "state": state,
            "clicked": clicked,
            "ok": ok,
            "error": error[:200],
            **extra,
        }
        with self._lock:
            self._log.append(ev)
            if len(self._log) > max(20, cfg.log_size):
                self._log = self._log[-cfg.log_size:]
        BUS.publish("rpa", **ev)

    def _set_state(self, new: str) -> None:
        prev = self._state
        self._state = new
        if new != prev:
            BUS.publish("rpa", action="state", state=new, previous=prev)

    # ------------------------------------------------------------ 图像兜底
    def _template_store(self, cfg) -> screenmod.TemplateStore:
        root = template_root()
        col = bool(getattr(cfg, "color_fallback", True))
        key = (str(root), float(cfg.match_threshold), tuple(cfg.match_scales or [1.0]), col)
        if self._store is None or self._store_key != key:
            self._store = screenmod.TemplateStore(root, cfg.match_threshold, key[2], use_color=col)
            self._store_key = key
        return self._store


    @staticmethod
    def _window_hwnd(win) -> int:
        try:
            return int(getattr(win, "NativeWindowHandle", 0) or 0)
        except Exception:
            return 0

    def _window_rect(self, win) -> tuple[int, int, int, int] | None:
        """窗口在屏幕上的物理像素矩形。UIA 拿不到就用 Win32；最小化的窗口返回 None。"""
        hwnd = self._window_hwnd(win)
        if hwnd:
            rect = screenmod.window_rect_physical(hwnd)
            if rect:
                return rect
        try:
            r = win.BoundingRectangle
            w, h = int(r.right - r.left), int(r.bottom - r.top)
            if w < 80 or h < 40 or int(r.left) <= -30000 or int(r.top) <= -30000:
                return None                     # 最小化/隐藏窗口
            return int(r.left), int(r.top), w, h
        except Exception:
            return None

    def _poll_image(self, cfg, matched: list) -> bool:
        """UIA 拿不到按钮时走这里。返回 True 表示这一轮用了图像识别。"""
        ok, why = screenmod.available()
        if not ok:
            if self._mode != "image-missing":
                self._mode = "image-missing"
                self._error = f"UIA 未取到按钮，且图像兜底不可用：{why}。{screenmod.install_hint()}"
            return False
        store = self._template_store(cfg)
        info = store.describe()
        col_ok = bool(getattr(cfg, "color_fallback", True))
        if not info["ready"] and not col_ok:
            if self._mode != "image-no-template":
                self._mode = "image-no-template"
                self._error = (
                    "UIA未取到按钮，已尝试图像兜底，但 " + str(info.get("dir")) + 
                    "里没有模板图（放 accept_*.png接听 / hangup_*.png挂断）。"
                    "采集方法见 AV_DOC.md §5.4")
            return True
        self._mode = "image"
        if info["ready"]:
            actions = tuple(a for a in ("accept","hangup") if info.get("ready_" + a))
        else:
            #没有模板图也能干活：直接按颜色找绿圆（接听）/红圆（挂断）
            actions = ("accept","hangup")
        for win, wname, _cls in matched[:3]:
            rect = self._window_rect(win)
            if not rect:
                continue
            hit = store.match_in_region(rect, actions)
            if not hit:
                continue
            self._last_match = {
                "window": wname[:60],
                "action": hit["action"],
                "template": hit["template"],
                "score": hit["score"],
                "x": hit["click_x"], "y": hit["click_y"],
                "ts": round(time.time(), 3),
            }
            action = hit["action"]
            if action == "accept":
                if self._state == "in_call":
                    continue
                self._set_state("ringing")
                ckey = ("image-accept", wname, hit["click_x"] // 40, hit["click_y"] // 40)
                if not self._cooldown_ok(ckey, float(cfg.accept_cooldown_s)):
                    continue
                if cfg.dry_run:
                    self._record("accept", wname, f"image:{hit['template']}", "ringing",
                                 clicked=False, mode="image", score=hit["score"])
                    continue
                res = screenmod.click(hit["click_x"], hit["click_y"])
                self._clicks += 1
                self._image_clicks += 1
                self._accepted_at = time.time()
                self._set_state("in_call")
                self._record("accept", wname, f"image:{hit['template']}", "ringing",
                             clicked=bool(res["ok"]), ok=bool(res["ok"]),
                             error=res.get("error") or "", mode="image",
                             score=hit["score"])
                BUS.publish("rpa", action="accepted", window=wname, how="image",
                            ok=bool(res["ok"]), score=hit["score"])
                return True
            # —— 挂断 ——
            self._set_state("in_call")
            if not cfg.auto_hangup:
                continue
            if time.time() - self._accepted_at < float(cfg.hangup_grace_s):
                continue
            ckey = ("image-hangup", wname, hit["click_x"] // 40, hit["click_y"] // 40)
            if not self._cooldown_ok(ckey, float(cfg.hangup_cooldown_s)):
                continue
            if cfg.dry_run:
                self._record("hangup", wname, f"image:{hit['template']}", "in_call",
                             clicked=False, mode="image", score=hit["score"])
                continue
            res = screenmod.click(hit["click_x"], hit["click_y"])
            self._clicks += 1
            self._image_clicks += 1
            self._record("hangup", wname, f"image:{hit['template']}", "in_call",
                         clicked=bool(res["ok"]), ok=bool(res["ok"]),
                         error=res.get("error") or "", mode="image", score=hit["score"])
            BUS.publish("rpa", action="hungup", window=wname, how="image",
                        ok=bool(res["ok"]), score=hit["score"])
            return True
        return True

    # ------------------------------------------------------------ 轮询
    def _poll(self, auto) -> None:
        cfg = cfgmod.load().rpa
        candidates = self._candidate_windows(auto)
        matched = [(w, n, c) for (w, n, c) in candidates if self._match_window(n, c)]

        accepts: list[tuple] = []
        hangups: list[tuple] = []
        for win, wname, _cls in matched:
            controls: list = []
            self._walk(win, 0, controls)
            acc = self._find_label(controls, cfg.accept_labels)
            hang = self._find_label(controls, cfg.hangup_labels)
            if acc is not None:
                accepts.append((win, wname, acc))
            if hang is not None:
                hangups.append((win, wname, hang))

        if accepts or hangups:
            self._mode = "uia"
            self._set_state("ringing" if accepts else "in_call")
        elif matched and cfg.image_fallback:
            # UIA 看不到按钮：交给图像识别（自绘 UI 的唯一出路）
            if not self._poll_image(cfg, matched):
                self._set_state("idle")
        else:
            if self._mode in ("uia", "image", "image-missing", "image-no-template"):
                self._mode = "none"
            self._set_state("idle")

        # —— 接听（UIA 路径）——
        for win, wname, ctrl in accepts:
            label = (ctrl.Name or "").strip()
            if self._state == "in_call" and not accepts:
                continue
            key = ("accept", label, self._window_hwnd(win))
            if not self._cooldown_ok(key, float(cfg.accept_cooldown_s)):
                continue
            if cfg.dry_run:
                self._record("accept", wname, label, "ringing", clicked=False, mode="uia")
                continue
            ok, how = self._invoke(ctrl)
            self._clicks += 1
            self._accepted_at = time.time()
            self._record("accept", wname, label, "ringing", clicked=ok, ok=ok, mode="uia")
            BUS.publish("rpa", action="accepted", window=wname, how=how, ok=ok)

        # —— 挂断（UIA 路径，默认关闭）——
        if cfg.auto_hangup:
            for win, wname, ctrl in hangups:
                if accepts:
                    continue          # 同一轮既看到接听又看到挂断，先接听
                if time.time() - self._accepted_at < float(cfg.hangup_grace_s):
                    continue
                label = (ctrl.Name or "").strip()
                key = ("hangup", label, self._window_hwnd(win))
                if not self._cooldown_ok(key, float(cfg.hangup_cooldown_s)):
                    continue
                if cfg.dry_run:
                    self._record("hangup", wname, label, "in_call", clicked=False, mode="uia")
                    continue
                ok, how = self._invoke(ctrl)
                self._clicks += 1
                self._record("hangup", wname, label, "in_call", clicked=ok, ok=ok, mode="uia")
                BUS.publish("rpa", action="hungup", window=wname, how=how, ok=ok)

    # ------------------------------------------------------------ 主动挂断
    def hangup_now(self, force: bool = False) -> dict:
        """显式挂断（主控台调用）：先 UIA，再图像。force=True 时忽略接听宽限期。"""
        cfg = cfgmod.load().rpa
        if cfg.dry_run and not force:
            return {"ok": False, "error": "rpa.dry_run=true，不会真点；传 force=true 可强制执行"}
        try:
            import uiautomation as auto
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        auto.SetGlobalSearchTimeout(0.4)
        matched = [(w, n, c) for (w, n, c) in self._candidate_windows(auto)
                   if self._match_window(n, c)]
        if not matched:
            return {"ok": False, "error": "没有找到 QQ/微信 通话窗口"}

        # 1) UIA
        for win, wname, _cls in matched:
            controls: list = []
            self._walk(win, 0, controls)
            ctrl = self._find_label(controls, cfg.hangup_labels)
            if ctrl is None:
                continue
            label = (ctrl.Name or "").strip()
            if cfg.dry_run:
                self._record("hangup", wname, label, "in_call", clicked=False,
                             mode="uia", manual=True)
                self._set_state("idle")
                return {"ok": True, "clicked": False, "how": "uia(dry-run)",
                        "window": wname, "control": label}
            ok, how = self._invoke(ctrl)
            self._clicks += 1
            self._record("hangup", wname, label, "in_call", clicked=ok, ok=ok,
                         mode="uia", manual=True)
            if ok:
                self._set_state("idle")
            return {"ok": ok, "clicked": ok, "how": f"uia:{how}",
                    "window": wname, "control": label}

        # 2) 图像兜底
        ok_img, why = screenmod.available()
        if not ok_img:
            return {"ok": False, "error": f"UIA 没找到挂断按钮，图像兜底不可用：{why}"}
        store = self._template_store(cfg)
        if not store.describe()["ready"]:
            return {"ok": False, "error": "UIA 没找到挂断按钮，且没有 hangup 模板图"}
        for win, wname, _cls in matched[:3]:
            rect = self._window_rect(win)
            if not rect:
                continue
            hit = store.match_in_region(rect, ("hangup",))
            if not hit:
                continue
            if cfg.dry_run and not force:
                self._record("hangup", wname, f"image:{hit['template']}", "in_call",
                             clicked=False, mode="image", score=hit["score"], manual=True)
                return {"ok": True, "clicked": False, "how": "image(dry-run)",
                        "window": wname, "x": hit["click_x"], "y": hit["click_y"],
                        "score": hit["score"]}
            res = screenmod.click(hit["click_x"], hit["click_y"])
            self._clicks += 1
            self._image_clicks += 1
            self._record("hangup", wname, f"image:{hit['template']}", "in_call",
                         clicked=bool(res["ok"]), ok=bool(res["ok"]),
                         error=res.get("error") or "", mode="image",
                         score=hit["score"], manual=True)
            if res["ok"]:
                self._set_state("idle")
            return {"ok": bool(res["ok"]), "clicked": bool(res["ok"]), "how": "image",
                    "window": wname, "x": hit["click_x"], "y": hit["click_y"],
                    "score": hit["score"], "error": res.get("error")}
        return {"ok": False, "error": "窗口里既没找到挂断按钮，也没匹配到 hangup 模板"}

    def call_now(self, kind: str = "voice", name: str = "") -> dict:
        """主动拨打（v1.4.0）：在 QQ 里搜人 → 打开会话 → 点「语音/视频通话」。

        这条链路跟自动接听是两件事：接听是「别人打进来我们点接」，
        拨出去是「我们主动找人」。所以它不依赖 watch 线程在跑，
        但一定要 QQ 已经登录、窗口在。
        """
        cfg = cfgmod.load().rpa
        kind = (kind or "voice").lower()
        labels = cfg.video_labels if kind == "video" else cfg.voice_labels
        try:
            import uiautomation as auto
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "steps": [], "error": f"{type(exc).__name__}: {exc}"}

        auto.SetGlobalSearchTimeout(0.6)
        steps: list = []
        wins = [w for (w, _n, _c) in self._candidate_windows(auto)]
        if not wins:
            return {"ok": False, "steps": steps, "error": "没找到 QQ 窗口（QQ 得先登录）"}
        main = None
        for w in wins:
            if "QQ" in (w.Name or ""):
                main = w
                break
        if main is None:
            main = wins[0]
        steps.append(("window", main.Name or ""))
        try:
            main.SetActive()
            time.sleep(0.25)
        except Exception:
            pass

        if name:
            ctrls: list = []
            self._walk(main, 0, ctrls, max_depth=8)
            box = None
            for c in ctrls:
                aid = (getattr(c, "AutomationId", "") or "").lower()
                nm = (getattr(c, "Name", "") or "")
                ctype = getattr(c, "ControlTypeName", "") or ""
                if ctype == "EditControl" and ("搜索" in nm or "search" in aid or "search" in nm.lower()):
                    box = c
                    break
            if box is None:
                return {"ok": False, "steps": steps, "error": "QQ 窗口里找不到搜索框"}
            try:
                box.Click()
                time.sleep(0.2)
                box.SendKeys("{Ctrl}a{Delete}" + str(name) + "{Enter}")
                steps.append(("search", str(name)))
                time.sleep(1.6)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "steps": steps, "error": f"搜索「{name}」失败：{exc}"}

        ctrls = []
        self._walk(main, 0, ctrls, max_depth=8)
        ctrl = self._find_label(ctrls, labels)
        if ctrl is None:
            return {"ok": False, "steps": steps,
                    "error": f"没找到「{'视频' if kind == 'video' else '语音'}通话」按钮"
                             "（QQ 版本不同按钮名字也会变，可以在 av_module/config.json 的 "
                             "rpa.voice_labels / video_labels 里补）"}
        label = (ctrl.Name or "").strip()
        steps.append(("button", label))
        if cfg.dry_run:
            self._record("call", main.Name or "", label, "idle", clicked=False,
                         mode="uia", manual=True)
            return {"ok": True, "clicked": False, "dry_run": True, "steps": steps,
                    "how": "uia(dry-run)", "control": label}
        ok, how = self._invoke(ctrl)
        self._clicks += 1
        self._record("call", main.Name or "", label, "dialing", clicked=ok, ok=ok,
                     mode="uia", manual=True)
        if ok:
            self._set_state("dialing")
        return {"ok": bool(ok), "clicked": bool(ok), "steps": steps,
                "how": f"uia:{how}", "control": label, "kind": kind}

    # ------------------------------------------------------------ 诊断
    def templates(self) -> dict:
        cfg = cfgmod.load().rpa
        ok, why = screenmod.available()
        store = self._template_store(cfg)
        info = store.describe()
        info.update({
            "available": ok,
            "error": why,
            "hint": "" if ok else screenmod.install_hint(),
            "image_fallback": cfg.image_fallback,
            "auto_hangup": cfg.auto_hangup,
            "screen": list(screenmod.screen_size()),
            "last_match": self._last_match,
        })
        return info

    def test_match(self, keyword: str | None = None, limit: int = 5) -> dict:
        """只做模板匹配、**不点击**。用来验证模板图能不能认出来。"""
        if not available():
            return {"ok": False, "error": install_hint()}
        ok, why = screenmod.available()
        if not ok:
            return {"ok": False, "error": why, "hint": screenmod.install_hint()}
        cfg = cfgmod.load().rpa
        store = self._template_store(cfg)
        info = store.describe()
        col_ok = bool(getattr(cfg, "color_fallback", True))
        if not info["ready"] and not col_ok:
            _d = str(info.get("dir", ""))
            _msg = (_d + "里没有模板图：放一张接听按钮 accept_*.png就能测接听，"
                "挂断同理放 hangup_*.png。")
            return {"ok": False, "templates": info, "error": _msg,
            "hint": "采集模板：python tools/capture_template.py --crop accept --region L,T,W,H"}
        if not info["ready"]:
            info = {**info, "note": "没有模板图，本次只测颜色兜底（绿圆=接听 /红圆=挂断）"}
        try:
            import uiautomation as auto

            auto.SetGlobalSearchTimeout(0.4)
            candidates = self._candidate_windows(auto)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        results: list[dict] = []
        for win, name, cls in candidates:
            if keyword and keyword.lower() not in f"{name} {cls}".lower():
                continue
            if not keyword and not self._match_window(name, cls):
                continue
            rect = self._window_rect(win)
            if not rect:
                continue
            for action in ("accept", "hangup"):
                hit = store.match_in_region(rect, (action,))
                if hit:
                    results.append({"window": name[:60], "rect": list(rect), **hit})
            if len(results) >= limit:
                break
        results.sort(key=lambda d: d["score"], reverse=True)
        return {
            "ok": True,
            "templates": info,
            "threshold": cfg.match_threshold,
            "hits": results[:limit],
            "note": "只匹配未点击；确认坐标对得上再把 rpa.dry_run 关掉",
        }

    def snapshot(self, keyword: str | None = None) -> dict:
        """截图存盘，方便裁模板。优先截命中关键词的窗口，否则整屏。"""
        ok, why = screenmod.available()
        if not ok:
            return {"ok": False, "error": why, "hint": screenmod.install_hint()}
        out_dir = template_root()
        out_dir.mkdir(parents=True, exist_ok=True)
        region = None
        window = ""
        if available():
            try:
                import uiautomation as auto

                auto.SetGlobalSearchTimeout(0.4)
                for win, name, cls in self._candidate_windows(auto):
                    if keyword and keyword.lower() not in f"{name} {cls}".lower():
                        continue
                    if not keyword and not self._match_window(name, cls):
                        continue
                    region = self._window_rect(win)
                    window = name
                    if region:
                        break
            except Exception:
                pass
        path = out_dir / "_snapshot.png"
        saved = screenmod.screenshot_file(path, region)
        if not saved:
            return {"ok": False, "error": "截图失败（锁屏或无桌面权限？）"}
        return {
            "ok": True,
            "file": saved,
            "window": window,
            "region": list(region) if region else None,
            "full_screen": region is None,
            "next": ("整个虚拟桌面（没有找到可截的 QQ/微信 窗口，或窗口最小化了）"
                     if region is None else
                     f"用 python tools\\capture_template.py --crop accept --region L,T,W,H 裁出按钮"),
        }

    def scan(self, max_depth: int = 5, limit: int = 60) -> dict:
        """把当前桌面窗口 + 疑似通话控件打出来，供人工调词。"""
        if not available():
            return {"ok": False, "error": install_hint()}
        try:
            import uiautomation as auto
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        out: list[dict] = []
        errors: list[str] = []
        try:
            auto.SetGlobalSearchTimeout(0.4)
            for w, name, cls in self._candidate_windows(auto):
                entry: dict = {"name": name[:80], "class": cls[:60], "controls": []}
                try:
                    controls: list = []
                    self._walk(w, 0, controls, max_depth=max_depth)
                    for c in controls[:limit]:
                        try:
                            entry["controls"].append({
                                "name": (c.Name or "")[:40],
                                "type": c.ControlTypeName,
                                "automation_id": getattr(c, "AutomationId", "") or "",
                            })
                        except Exception:
                            continue
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                rect = self._window_rect(w)
                if rect:
                    entry["rect"] = list(rect)
                    entry["hwnd"] = self._window_hwnd(w)
                if entry["controls"] or self._match_window(name, cls):
                    out.append(entry)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")
        cfg = cfgmod.load().rpa
        self._last_scan = out
        img_ok, img_why = screenmod.available()
        store = self._template_store(cfg)
        return {
            "ok": True,
            "windows": out[:40],
            "matched_labels": {
                "accept": self._hits(out, cfg.accept_labels),
                "hangup": self._hits(out, cfg.hangup_labels),
            },
            "image": {
                "available": img_ok,
                "error": img_why,
                "templates": store.describe(),
                "mode": self._mode,
                "note": "窗口 controls=0 时看这里：模板齐了就自动走图像识别",
            },
            "errors": errors,
        }

    @staticmethod
    def _hits(windows: list[dict], labels: list[str]) -> list[str]:
        low = [l.lower() for l in labels]
        found: list[str] = []
        for w in windows:
            for c in w.get("controls", []):
                n = (c.get("name") or "").lower()
                if any(l in n for l in low):
                    found.append(f"{w['name']} :: {c['name']}")
        return found[:10]

    # ------------------------------------------------------------ 状态
    def status(self) -> dict:
        cfg = cfgmod.load().rpa
        img_ok, img_why = screenmod.available()
        with self._lock:
            store_info: dict = {}
            try:
                store_info = self._template_store(cfg).describe()
            except Exception:
                store_info = {}
            return {
                "available": available(),
                "running": self.running,
                "state": self._state,
                "mode": self._mode,
                "dry_run": cfg.dry_run,
                "polls": self._polls,
                "clicks": self._clicks,
                "image_clicks": self._image_clicks,
                "window_keywords": cfg.window_keywords,
                "accept_labels": cfg.accept_labels,
                "hangup_labels": cfg.hangup_labels,
                "image": {
                    "available": img_ok,
                    "error": img_why,
                    "fallback": cfg.image_fallback,
                    "auto_hangup": cfg.auto_hangup,
                    "threshold": cfg.match_threshold,
                    "templates": store_info,
                    "last_match": self._last_match,
                },
                "error": self._error,
                "log": self._log[-20:],
                "hint": "" if available() else install_hint(),
            }
