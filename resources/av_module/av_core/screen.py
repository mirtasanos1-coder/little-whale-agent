"""屏幕截图 + 模板匹配 + 模拟点击 —— RPA 的"图像识别兜底"。

为什么需要它：QQ NT / 微信新版大量使用自绘 UI（Chrome_WidgetWin），UIA 只能看到
窗口本身、看不到"接听/挂断"按钮（`controls=0`）。这时改用最原始也最可靠的办法：
截一张屏幕 → 和预先截好的按钮小图做模板匹配 → 命中就 `pyautogui.click(x, y)`。

三个坑（都已处理）：

1. **DPI 缩放**：python 进程默认不是 DPI aware，UIA 给的坐标和屏幕真实像素会差一个
   缩放比（125% 下点偏 1/5），所以模块导入时就调用 `ensure_dpi_aware()`，
   让 UIA 坐标、截图坐标、pyautogui 坐标统一成物理像素。
2. **多显示器**：截图用 `PIL.ImageGrab.grab(all_screens=True)`，覆盖整个虚拟桌面。
3. **重复点击**：同一位置连点会变成"接听→挂断→接听"，所以调用方（rpa.py）必须带
   冷却时间；本模块只负责"匹配"和"点一下"。

依赖（都可选，缺了 `available()` 会返回 False 并给出原因）：
    pip install pyautogui opencv-python pillow
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from pathlib import Path

import numpy as np

_DPI_DONE = False
_cv2 = None
_pyautogui = None
_import_error = ""


def ensure_dpi_aware() -> bool:
    """把当前进程标记成 per-monitor DPI aware（幂等）。"""
    global _DPI_DONE
    if _DPI_DONE:
        return True
    try:
        # Windows 8.1+：2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        _DPI_DONE = True
        return True
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        _DPI_DONE = True
        return True
    except Exception:
        return False


ensure_dpi_aware()


def _load_deps() -> None:
    global _cv2, _pyautogui, _import_error
    if _cv2 is not None and _pyautogui is not None:
        return
    try:
        import cv2  # type: ignore

        _cv2 = cv2
    except Exception as exc:  # noqa: BLE001
        _import_error = f"opencv-python 导入失败: {type(exc).__name__}: {exc}"
        return
    try:
        # pyautogui 在没装 X/显示的环境会抛异常，必须先探一下
        import pyautogui  # type: ignore

        _ = pyautogui.size()
        _pyautogui = pyautogui
    except Exception as exc:  # noqa: BLE001
        _import_error = f"pyautogui 导入失败: {type(exc).__name__}: {exc}"
        return
    _import_error = ""


def available() -> tuple[bool, str]:
    """(能否用图像兜底, 不能用的原因)"""
    _load_deps()
    if _cv2 is None or _pyautogui is None:
        return False, _import_error or "缺少 opencv-python / pyautogui"
    return True, ""


def install_hint() -> str:
    return "未安装图像识别依赖。执行: pip install pyautogui opencv-python"


def screen_size() -> tuple[int, int]:
    """物理像素下的虚拟桌面尺寸。"""
    _load_deps()
    if _pyautogui is not None:
        try:
            return int(_pyautogui.size()[0]), int(_pyautogui.size()[1])
        except Exception:
            pass
    try:
        u = ctypes.windll.user32
        return int(u.GetSystemMetrics(78)), int(u.GetSystemMetrics(79))  # SM_CXVIRTUALSCREEN/Y
    except Exception:
        return 0, 0


def grab(region: tuple[int, int, int, int] | None = None) -> np.ndarray | None:
    """截屏 -> BGR ndarray。region=(left, top, width, height)，None 表示全屏。"""
    try:
        from PIL import ImageGrab  # type: ignore

        if region is None:
            img = ImageGrab.grab(all_screens=True)
        else:
            left, top, w, h = (int(v) for v in region)
            if w <= 0 or h <= 0:
                return None
            img = ImageGrab.grab(bbox=(left, top, left + w, top + h), all_screens=True)
        arr = np.array(img)
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, axis=-1)
        return arr[:, :, ::-1].copy()  # RGB -> BGR，copy 保证连续内存
    except Exception:
        return None


def load_template(path: str | Path) -> np.ndarray | None:
    """读入模板图片（BGR）。也接受中文路径（先用 PIL 读，再转 ndarray）。"""
    try:
        from PIL import Image  # type: ignore

        with Image.open(str(path)) as im:
            arr = np.array(im.convert("RGB"))
        return arr[:, :, ::-1].copy()
    except Exception:
        return None




# ---------------------------------------------------------------- v1.6.4匹配增强
# QA P1-1：接听按钮命中率低。三个原因一起改：
#1)原来只搜1.0/0.85/1.15三档缩放，模板可能是从别的分辨率（甚至手机截图）截来的，
#差20%就整张图对不上；现在按0.45~2.3的对数档粗搜 +全分辨率复核。
#2)灰度 NCC在纯色/大色块上会飘，加颜色校验：命中区域的平均 HSV和模板差太多就扣分。
#3) QQ/微信来电是「纯色圆底 +白色图标」，直接按颜色找圆形按钮，比模板匹配稳得多，
#模板没命中时用它兜底（接听=绿，挂断=红）。

_GREEN_RANGE = ((35,70,60), (85,255,255))
_RED_RANGES = (((0,60,60), (10,255,255)), ((170,60,60), (180,255,255)))


def _mean_hsv(bgr: np.ndarray) -> tuple[float, float, float] | None:
    """区域平均 HSV（OpenCV的 H是0~179的环形标度）。"""
    if _cv2 is None or bgr is None or getattr(bgr, "size",0) ==0:
        return None
    try:
        hsv = _cv2.cvtColor(bgr, _cv2.COLOR_BGR2HSV)
        return (float(hsv[:, :,0].mean()), float(hsv[:, :,1].mean()), float(hsv[:, :,2].mean()))
    except Exception:
        return None


def _hsv_close(a, b, dh: float =24.0, ds: float =75.0, dv: float =85.0) -> bool:
    """颜色是否接近。色相按环形算距离（179和0只差1）。"""
    if not a or not b:
        return True
    d0 = abs(a[0] - b[0])
    d0 = min(d0,180.0 - d0)
    return d0 <= dh and abs(a[1] - b[1]) <= ds and abs(a[2] - b[2]) <= dv


def _shrink(gray: np.ndarray, max_side: int =1100):
    """粗搜前把截图等比缩小，省时间。返回 (小图,系数)。"""
    if _cv2 is None:
        return gray,1.0
    h, w = gray.shape[:2]
    m = max(h, w)
    if m <= max_side:
        return gray,1.0
    f = float(max_side) / float(m)
    return _cv2.resize(gray, None, fx=f, fy=f, interpolation=_cv2.INTER_AREA), f


def _scale_grid(base) -> list[float]:
    """把用户配置的 scale和常用档合并成一个宽一点的搜索网格。"""
    grid = set()
    for s in tuple(base or (1.0,)):
        try:
            s = float(s)
        except Exception:
            continue
        if s >0:
            grid.add(round(s,3))
    for s in (0.45,0.55,0.65,0.75,0.85,1.0,1.15,1.3,1.5,1.75,2.0,2.3):
        grid.add(s)
    return sorted(grid)


def _nms(hits: list[dict], ratio: float =0.55) -> list[dict]:
    """按中心距离去重：同一个按钮在不同 scale会重复命中。"""
    picked: list[dict] = []
    for h in sorted(hits, key=lambda d: d["score"], reverse=True):
        cx = h["x"] + h["w"] /2.0
        cy = h["y"] + h["h"] /2.0
        dup = False
        for p in picked:
            px = p["x"] + p["w"] /2.0
            py = p["y"] + p["h"] /2.0
            if abs(cx - px) < max(h["w"], p["w"]) * ratio and abs(cy - py) < max(h["h"], p["h"]) * ratio:
                dup = True
                break
        if not dup:
            picked.append(h)
    return picked


def find_color_button(screen: np.ndarray, kind: str = "accept",
                    min_px: int =400, max_px: int =200000) -> dict | None:
    """按颜色找「圆底按钮」——QQ/微信来电的接听是绿圆、挂断是红圆。

    模板匹配在纯色圆底上容易飘（圆里只有几笔白线条），但颜色本身很难搞错，
    所以拿它当兜底：命中返回 {x,y,w,h,click_x,click_y,score,how}（坐标为传入图内坐标）。
    """
    _load_deps()
    if _cv2 is None or screen is None or getattr(screen, "size",0) ==0:
        return None
    cv2 = _cv2
    try:
        hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
    except Exception:
        return None
    if kind == "hangup":
        lo, hi = _RED_RANGES[0]
        mask = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
        lo, hi = _RED_RANGES[1]
        mask2 = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
        mask = cv2.bitwise_or(mask, mask2)
    else:
        lo, hi = _GREEN_RANGE
        mask = cv2.inRange(hsv, np.array(lo, np.uint8), np.array(hi, np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5,5), np.uint8))
    try:
        n, _lab, stats, cents = cv2.connectedComponentsWithStats(mask,8)
    except Exception:
        return None
    best: dict | None = None
    for i in range(1, int(n)):
        x, y, w, h, area = [int(v) for v in stats[i]]
        if area < int(min_px) or area > int(max_px):
            continue
        if w <14 or h <14:
            continue
        ratio = float(w) / float(h)
        if ratio <0.62 or ratio >1.62:
            continue
        fill = float(area) / float(w * h)
        if fill <0.45: #圆大约0.785，方1.0，这里把细条/边框滤掉
            continue
        cx = int(round(float(cents[i][0])))
        cy = int(round(float(cents[i][1])))
        score =0.55 + min(0.30, fill *0.30)
        cand = {"score": round(score,4), "x": x, "y": y,
                "w": w, "h": h, "scale":1.0,
                "click_x": cx, "click_y": cy, "how": "color"}
        if best is None or cand["score"] > best["score"]:
            best = cand
    return best


def match_all(screen: np.ndarray, template: np.ndarray, threshold: float =0.8,
                scales: tuple[float, ...] = (1.0,), max_hits: int =5) -> list[dict]:
    """在 screen里找 template，返回按分数降序的命中点 [{score,x,y,w,h,scale}]。

    坐标以 screen左上角为原点（调用方若截的是窗口区域，需要自己加窗口偏移）。
    v1.6.4：两段式搜索（缩图粗搜定缩放 →原图复核），加颜色一致性校验，
    这样模板和实际按钮尺寸差20%、或背景换色都不会整片漏掉。
    """
    _load_deps()
    if _cv2 is None or screen is None or template is None:
        return []
    if getattr(screen, "size",0) ==0 or getattr(template, "size",0) ==0:
        return []
    cv2 = _cv2
    thr = float(threshold)
    if screen.ndim ==3:
        hay = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
    else:
        hay = screen
    tpl_hsv = _mean_hsv(template) if template.ndim ==3 else None
    small, f = _shrink(hay)

    # ——第一段：缩图上把所有缩放档都粗扫一遍，只挑出最像的几个 scale
    coarse: list[tuple[float, float]] = []
    for sc in _scale_grid(scales):
        tw = int(round(template.shape[1] * sc * f))
        th = int(round(template.shape[0] * sc * f))
        if tw <8 or th <8 or tw >= small.shape[1] or th >= small.shape[0]:
            continue
        tpl_s = cv2.resize(template, (tw, th), interpolation=cv2.INTER_AREA)
        gt = cv2.cvtColor(tpl_s, cv2.COLOR_BGR2GRAY) if tpl_s.ndim ==3 else tpl_s
        if float(gt.std()) <6.0: #纯色模板做 NCC没有意义，只会到处乱命中
            continue
        res = cv2.matchTemplate(small, gt, cv2.TM_CCOEFF_NORMED)
        res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
        if res.size:
            coarse.append((float(res.max()), float(sc)))
    if not coarse:
        return []
    coarse.sort(reverse=True)
    cand_scales = [sc for (_s, sc) in coarse[:4]]

    # ——第二段：原图上按挑出来的 scale精确找，并逐个做颜色校验
    hits: list[dict] = []
    floor_score = max(0.30, thr -0.06)
    for sc in cand_scales:
        tw = int(round(template.shape[1] * sc))
        th = int(round(template.shape[0] * sc))
        if tw <8 or th <8 or tw >= hay.shape[1] or th >= hay.shape[0]:
            continue
        tpl_s = cv2.resize(template, (tw, th), interpolation=cv2.INTER_AREA)
        gt = cv2.cvtColor(tpl_s, cv2.COLOR_BGR2GRAY) if tpl_s.ndim ==3 else tpl_s
        res = cv2.matchTemplate(hay, gt, cv2.TM_CCOEFF_NORMED)
        res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
        ys, xs = np.where(res >= floor_score)
        if ys.size ==0:
            continue
        #每个 scale只取分数最高的40个点，避免在纹理背景上算几万个
        order = np.argsort(res[ys, xs])[::-1][:40]
        for i in order.tolist():
            y, x = int(ys[i]), int(xs[i])
            score = float(res[y, x])
            if tpl_hsv is not None and getattr(screen, "ndim",2) ==3:
                patch = screen[y:y + th, x:x + tw]
                if not _hsv_close(_mean_hsv(patch), tpl_hsv):
                    score -=0.20 #颜色对不上，基本可以断定不是同一个按钮
            hits.append({"score": round(score,4), "x": x, "y": y,
                    "w": int(tw), "h": int(th), "scale": float(sc)})
    if not hits:
        return []
    picked = _nms(hits)
    return [h for h in picked if h["score"] >= thr -0.18][:int(max_hits)]


def click(x: int, y: int, double: bool = False) -> dict:
    """在屏幕绝对坐标点一下。返回 {ok, x, y, error}。"""
    _load_deps()
    if _pyautogui is None:
        return {"ok": False, "x": x, "y": y, "error": _import_error or "pyautogui 不可用"}
    try:
        _pyautogui.moveTo(int(x), int(y), duration=0.05)
        _pyautogui.click(clicks=2 if double else 1, interval=0.05)
        return {"ok": True, "x": int(x), "y": int(y), "error": None}
    except Exception as exc:  # noqa: BLE001
        # pyautogui 的 FAILSAFE（鼠标甩到左上角）也会走这里
        return {"ok": False, "x": int(x), "y": int(y),
                "error": f"{type(exc).__name__}: {exc}"}


def move_to(x: int, y: int) -> bool:
    _load_deps()
    if _pyautogui is None:
        return False
    try:
        _pyautogui.moveTo(int(x), int(y), duration=0.05)
        return True
    except Exception:
        return False


class TemplateStore:
    """管理 rpa/templates 下的按钮模板。

    文件名约定：`accept_*.png` = 接听按钮，`hangup_*.png` = 挂断按钮。
    """

    ACTIONS = ("accept", "hangup")

    def __init__(self, directory: str | Path, threshold: float = 0.8,
                 scales: tuple[float, ...] | None = (1.0, 0.85, 1.15),
                 use_color: bool = True):
        self.dir = Path(directory)
        self.threshold = float(threshold)
        self.scales = tuple(scales or (1.0,))
        # v1.6.4：模板没命中时用颜色找圆按钮兜底（接听绿 /挂断红）
        self.use_color = bool(use_color)
        self._cache: dict[str, np.ndarray] = {}
        self._mtime: dict[str, float] = {}

    def files(self) -> dict[str, list[Path]]:
        out: dict[str, list[Path]] = {a: [] for a in self.ACTIONS}
        if not self.dir.is_dir():
            return out
        for p in sorted(self.dir.glob("*.png")):
            key = p.name.split("_")[0].lower()
            if key in out:
                out[key].append(p)
        return out

    def _load(self, path: Path) -> np.ndarray | None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        key = str(path)
        if key in self._cache and abs(self._mtime.get(key, 0.0) - mtime) < 1e-6:
            return self._cache[key]
        img = load_template(path)
        if img is not None:
            self._cache[key] = img
            self._mtime[key] = mtime
        return img

    def describe(self) -> dict:
        files = self.files()
        return {
            "dir": str(self.dir),
            "threshold": self.threshold,
            "scales": list(self.scales),
            "color_fallback": bool(self.use_color),
            "accept": [p.name for p in files["accept"]],
            "hangup": [p.name for p in files["hangup"]],
            "ready_accept": bool(files["accept"]),
            "ready_hangup": bool(files["hangup"]),
            # 只要有一种模板就能工作：接听只需要接听按钮，挂断只需要挂断按钮
            "ready": bool(files["accept"] or files["hangup"]),
        }

    def match(self, screen: np.ndarray, actions: tuple[str, ...] = ACTIONS) -> dict | None:
        """在整屏截图里找指定动作的按钮，返回最佳命中（带屏幕绝对坐标）。

        v1.6.4：模板全都没命中时，再按颜色找「绿圆/红圆」兜底（QQ/微信来电就是这种形状），
        并用 use_color: False让调用方可以关掉。
        """
        files = self.files()
        best: dict | None = None
        for action in actions:
            for path in files.get(action, []):
                tpl = self._load(path)
                if tpl is None:
                    continue
                for hit in match_all(screen, tpl, self.threshold, self.scales, max_hits=3):
                    cand = {**hit, "action": action, "template": path.name}
                    if best is None or cand["score"] > best["score"]:
                        best = cand
        if best is not None and best["score"] >= self.threshold:
            return best
        if self.use_color:
            for action in actions:
                cand = find_color_button(screen, action)
                if not cand:
                    continue
                cand = {**cand, "action": action, "template": "color:" + action}
                if best is None or cand["score"] > best["score"]:
                    best = cand
        return best


    def match_in_region(self, region: tuple[int, int, int, int],
                        actions: tuple[str, ...] = ACTIONS) -> dict | None:
        """只截指定窗口区域，返回的坐标已换算成屏幕绝对坐标。"""
        shot = grab(region)
        if shot is None:
            return None
        hit = self.match(shot, actions)
        if hit:
            hit["x"] += int(region[0])
            hit["y"] += int(region[1])
            hit["click_x"] = hit["x"] + hit["w"] // 2
            hit["click_y"] = hit["y"] + hit["h"] // 2
        return hit


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """整个虚拟桌面（多显示器合并）的 (left, top, width, height)，物理像素。"""
    try:
        u = ctypes.windll.user32
        left = int(u.GetSystemMetrics(76))    # SM_XVIRTUALSCREEN
        top = int(u.GetSystemMetrics(77))     # SM_YVIRTUALSCREEN
        w = int(u.GetSystemMetrics(78))       # SM_CXVIRTUALSCREEN
        h = int(u.GetSystemMetrics(79))       # SM_CYVIRTUALSCREEN
        if w > 0 and h > 0:
            return left, top, w, h
    except Exception:
        pass
    sw, sh = screen_size()
    return 0, 0, sw, sh


def window_rect_physical(hwnd: int) -> tuple[int, int, int, int] | None:
    """取窗口在屏幕上的物理像素矩形 (left, top, width, height)。

    会过滤掉"最小化/隐藏/尺寸异常/完全在屏幕外"的窗口——最小化的窗口
    GetWindowRect 会返回 (-32000, -32000, ...)，拿它去截图只能得到黑块，
    进而让模板匹配在垃圾图上乱命中。
    """
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        u = ctypes.windll.user32
        hwnd = int(hwnd)
        if not hwnd or not u.IsWindow(hwnd):
            return None
        if u.IsIconic(hwnd) or not u.IsWindowVisible(hwnd):
            return None
        r = RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        w = r.right - r.left
        h = r.bottom - r.top
        if w < 80 or h < 40:
            return None
        if r.left <= -30000 or r.top <= -30000:
            return None
        vl, vt, vw, vh = virtual_screen_rect()
        if vw <= 0 or vh <= 0:
            return None
        if (r.right <= vl or r.bottom <= vt
                or r.left >= vl + vw or r.top >= vt + vh):
            return None                       # 完全在屏幕外
        left = max(r.left, vl)
        top = max(r.top, vt)
        right = min(r.right, vl + vw)
        bottom = min(r.bottom, vt + vh)
        if right - left < 80 or bottom - top < 40:
            return None
        return int(left), int(top), int(right - left), int(bottom - top)
    except Exception:
        return None


def screenshot_file(path: str | Path, region: tuple[int, int, int, int] | None = None) -> str | None:
    """把截图存成 PNG，返回路径。"""
    try:
        from PIL import Image, ImageGrab  # type: ignore

        if region is None:
            img = ImageGrab.grab(all_screens=True)
        else:
            left, top, w, h = (int(v) for v in region)
            img = ImageGrab.grab(bbox=(left, top, left + w, top + h), all_screens=True)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(p)
        return str(p)
    except Exception:
        return None


def crop_file(src: str | Path, dest: str | Path,
              region: tuple[int, int, int, int]) -> str | None:
    """从已保存的截图里裁一块出来当模板。region 相对 src 左上角。"""
    try:
        from PIL import Image  # type: ignore

        with Image.open(str(src)) as im:
            left, top, w, h = (int(v) for v in region)
            box = (left, top, left + w, top + h)
            if box[2] > im.width or box[3] > im.height:
                return None
            crop = im.crop(box)
        p = Path(dest)
        p.parent.mkdir(parents=True, exist_ok=True)
        crop.save(p)
        return str(p)
    except Exception:
        return None


def benchmark_match(rounds: int = 3) -> dict:
    """自测：截一次屏，随便裁一块当模板，再把它找回来（验证 opencv/DPI 链路）。"""
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why}
    shot = grab()
    if shot is None:
        return {"ok": False, "error": "截屏失败（会话被锁屏或没有桌面权限？）"}
    h, w = shot.shape[:2]
    box_h, box_w = min(120, h // 3), min(220, w // 4)
    y0, x0 = max(10, h // 2 - box_h // 2), max(10, w // 2 - box_w // 2)
    tpl = shot[y0:y0 + box_h, x0:x0 + box_w].copy()
    t0 = time.time()
    hits = []
    for _ in range(max(1, rounds)):
        hits = match_all(shot, tpl, 0.9, (1.0,))
    dt = (time.time() - t0) / max(1, rounds)
    top = hits[0] if hits else None
    return {
        "ok": bool(top),
        "screen": [int(w), int(h)],
        "expected": [int(x0), int(y0)],
        "found": [top["x"], top["y"]] if top else None,
        "score": top["score"] if top else None,
        "ms": round(dt * 1000, 1),
    }
