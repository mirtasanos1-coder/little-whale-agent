"""虚拟摄像头推流：pyvirtualcam + Pillow 动态帧。

能力
----
* 推静态人设图，带 Ken Burns 缓慢推拉（看起来不像一张死图）
* 呼吸/脉冲动效、字幕叠加
* 没有虚拟摄像头时**降级为 dry-run**：照样按帧率生成画面并输出预览 JPEG，
  这样测试 UI 在没有装驱动时也能验证“画面长什么样”

为什么只支持 OBS 虚拟摄像头：pyvirtualcam 在 Windows 上的后端要求
DirectShow 滤镜（OBS Virtual Camera）。Unity Capture 不是它的后端。
"""

from __future__ import annotations

import io
import math
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import config as cfgmod
from .events import BUS

_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        try:
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def placeholder_image(text: str = "AI", size: tuple[int, int] = (1280, 720)) -> Image.Image:
    """没有素材时生成一张人设卡，保证推流立刻可用（深底 + 顶部柔光 + 居中字标）。"""
    w, h = size
    # 竖向渐变（numpy 一次算完，比逐行画线快）
    ramp = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    top = np.array([13, 16, 27], dtype=np.float32)
    bottom = np.array([31, 29, 52], dtype=np.float32)
    col = (top * (1 - ramp) + bottom * ramp).astype(np.uint8)          # (h,3)
    arr = np.repeat(col[:, None, :], w, axis=1)                        # (h,w,3)
    # 顶部柔光
    yy, xx = np.mgrid[0:h, 0:w]
    dx = (xx - w * 0.5) / (w * 0.62)
    dy = (yy - h * 0.02) / (h * 0.85)
    glow = np.clip(1.0 - np.sqrt(dx * dx + dy * dy), 0.0, 1.0) ** 2.2
    arr = np.clip(arr.astype(np.float32) +
                  glow[..., None] * np.array([46, 74, 148], dtype=np.float32),
                  0, 255).astype(np.uint8)
    img = Image.fromarray(arr, "RGB")

    draw = ImageDraw.Draw(img)
    font = _load_font(int(h * 0.26))
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (w - tw) / 2 - bbox[0]
    y = h * 0.30 - th / 2 - bbox[1]
    # 外圈光环
    r = max(tw, th) * 0.95
    cx, cy = w / 2, h * 0.30
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(96, 130, 210), width=2)
    draw.text((x, y), text, font=font, fill=(238, 244, 255))

    font2 = _load_font(max(14, int(h * 0.042)))
    sub = "虚拟摄像头测试画面"
    b2 = draw.textbbox((0, 0), sub, font=font2)
    draw.text(((w - (b2[2] - b2[0])) / 2, cy + r + h * 0.045), sub,
              font=font2, fill=(168, 182, 210))
    font3 = _load_font(max(12, int(h * 0.026)))
    tag = "AV Module · Virtual Camera · :5800"
    draw.text((w * 0.035, h - h * 0.075), tag, font=font3, fill=(120, 134, 162))
    return img


def _cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """等比铺满并居中裁剪。"""
    w, h = size
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    new = (max(1, int(iw * scale)), max(1, int(ih * scale)))
    img = img.resize(new, Image.LANCZOS)
    left = (new[0] - w) // 2
    top = (new[1] - h) // 2
    return img.crop((left, top, left + w, top + h))


class FrameRenderer:
    """在图源上施加速度很慢的动效，逐帧输出 RGB ndarray。"""

    def __init__(self, image: Image.Image, width: int, height: int,
                 effect: str = "kenburns", zoom: float = 1.06,
                 background: str = "#101018"):
        self.width = width
        self.height = height
        self.effect = effect
        self.zoom = max(1.0, min(1.6, float(zoom)))
        self.background = background
        self.caption = ""
        self._t0 = time.time()
        self._font = _load_font(max(14, height // 26))
        self.set_image(image)

    def set_image(self, image: Image.Image) -> None:
        if image.mode != "RGB":
            image = image.convert("RGB")
        self.base = _cover(image, (int(self.width * self.zoom), int(self.height * self.zoom)))
        self._still = _cover(image, (self.width, self.height))

    def set_caption(self, text: str) -> None:
        self.caption = (text or "")[:80]

    def frame(self, t: float | None = None) -> np.ndarray:
        t = self._t0 if t is None else t
        el = time.time() - t
        if self.effect == "still" or self.zoom <= 1.001:
            img = self._still.copy()
        elif self.effect == "pulse":
            # 小幅推拉 + 亮度呼吸
            k = 1.0 + 0.5 * (0.5 + 0.5 * math.sin(el * 0.9)) * (self.zoom - 1.0)
            w = int(self.width * k)
            h = int(self.height * k)
            img = _cover(self.base, (w, h))
            left = (w - self.width) // 2
            top = (h - self.height) // 2
            img = img.crop((left, top, left + self.width, top + self.height))
            arr = np.asarray(img, dtype=np.int16)
            boost = 1.0 + 0.05 * math.sin(el * 1.7)
            arr = np.clip(arr * boost, 0, 255).astype(np.uint8)
            img = Image.fromarray(arr)
        else:  # kenburns: 8 秒一个来回的缓慢平移
            span = 0.5 - 0.5 * math.cos(el * (2 * math.pi / 18.0))
            max_dx = self.base.width - self.width
            max_dy = self.base.height - self.height
            dx = int(max_dx * (0.2 + 0.6 * span))
            dy = int(max_dy * (0.8 - 0.6 * span))
            img = self.base.crop((dx, dy, dx + self.width, dy + self.height))

        if self.caption:
            img = img.copy()
            draw = ImageDraw.Draw(img, "RGBA")
            bbox = draw.textbbox((0, 0), self.caption, font=self._font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            pad = 14
            y = self.height - th - pad * 3
            x = max(pad, (self.width - tw) // 2)
            draw.rounded_rectangle(
                [x - pad, y - pad, x + tw + pad, y + th + pad],
                radius=10, fill=(0, 0, 0, 130),
            )
            draw.text((x, y), self.caption, font=self._font, fill=(255, 255, 255, 235))
        return np.asarray(img, dtype=np.uint8)


class VirtualCam:
    """推流线程 + 状态机。所有公开方法线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cam = None
        self._renderer: FrameRenderer | None = None
        self._backend = ""
        self._dry_run = False
        self._error: str | None = None
        self._error_detail = ""
        self._error_hint = ""
        self._opened = threading.Event()
        self._fps_actual = 0.0
        self._frames = 0
        self._started_at = 0.0
        self._preview: bytes = b""
        self._preview_at = 0.0
        self._image_desc = ""
        self._fps_cfg = 24

    # -------------------------------------------------- 图源
    def load_image(self, src: str | bytes | Path | Image.Image,
                   base_dir: Path | None = None) -> Image.Image:
        """支持本地路径 / http(s) URL / base64 / data URI / PIL.Image。"""
        if isinstance(src, Image.Image):
            return src
        if isinstance(src, (bytes, bytearray)):
            return Image.open(io.BytesIO(src))
        s = str(src).strip()
        if not s:
            raise ValueError("图片地址为空")
        if s.startswith("data:"):
            import base64

            head, _, payload = s.partition(",")
            raw = base64.b64decode(payload)
            return Image.open(io.BytesIO(raw))
        if s.startswith("http://") or s.startswith("https://"):
            req = urllib.request.Request(s, headers={"User-Agent": "av-module/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return Image.open(io.BytesIO(resp.read()))
        p = Path(s)
        if not p.is_absolute() and base_dir is not None:
            p = (base_dir / s).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"图片不存在: {p}")
        return Image.open(p)

    # -------------------------------------------------- 控制
    def start(self, image: str | bytes | Image.Image | None = None,
              effect: str | None = None) -> dict:
        cfg = cfgmod.load().video
        with self._lock:
            if self.running:
                if image is not None:
                    self._set_image_locked(image)
                if effect:
                    self._renderer.effect = effect if self._renderer else cfg.effect
                return self.status()
            img = self._resolve_image(image)
            self._fps_cfg = max(5, min(60, int(cfg.fps)))
            self._renderer = FrameRenderer(
                img, cfg.width, cfg.height,
                effect=effect or cfg.effect, zoom=cfg.zoom, background=cfg.background,
            )
            self._stop.clear()
            self._opened.clear()
            self._error = None
            self._error_detail = ""
            self._error_hint = ""
            self._frames = 0
            self._started_at = time.time()
            self._thread = threading.Thread(target=self._run, name="av-virtualcam", daemon=True)
            self._thread.start()
        # 等摄像头打开结果，避免接口返回“还没开始就成功”的状态
        self._opened.wait(4.0)
        BUS.publish("video", action="start", effect=effect or cfg.effect,
                    resolution=f"{cfg.width}x{cfg.height}", fps=self._fps_cfg)
        return self.status()

    def _resolve_image(self, image) -> Image.Image:
        cfg = cfgmod.load().video
        keyword = str(image).strip().lower() if isinstance(image, str) else ""
        if image is None or keyword in ("placeholder", "testcard", "test", "default", "none"):
            # 优先 assets/default_avatar.png，其次 assets/ 里第一张图
            adir = cfgmod.ASSETS_DIR
            for name in ("default_avatar.png", "default_avatar.jpg"):
                p = adir / name
                if p.is_file():
                    try:
                        self._image_desc = p.name
                        return Image.open(p).convert("RGB")
                    except Exception:
                        pass
            if adir.is_dir():
                for p in sorted(adir.iterdir()):
                    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                        try:
                            self._image_desc = p.name
                            return Image.open(p).convert("RGB")
                        except Exception:
                            continue
            self._image_desc = "placeholder"
            return placeholder_image("AI", (cfg.width, cfg.height))
        img = self.load_image(image, base_dir=cfgmod.ROOT)
        self._image_desc = str(image)[:200] if not isinstance(image, Image.Image) else "memory"
        return img

    def _set_image_locked(self, image) -> None:
        img = self._resolve_image(image)
        if self._renderer:
            self._renderer.set_image(img)
        else:
            self._renderer = FrameRenderer(img, cfgmod.load().video.width,
                                           cfgmod.load().video.height)

    def set_image(self, image) -> dict:
        with self._lock:
            if not self.running:
                return self.start(image=image)
            self._set_image_locked(image)
        BUS.publish("video", action="image", image=self._image_desc)
        return self.status()

    def set_caption(self, text: str) -> dict:
        with self._lock:
            if self._renderer:
                self._renderer.set_caption(text)
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._stop.set()
            thread = self._thread
        if thread:
            thread.join(timeout=3.0)
        with self._lock:
            self._thread = None
            self._close_cam_locked()
        BUS.publish("video", action="stop")
        return self.status()

    def _close_cam_locked(self) -> None:
        cam, self._cam = self._cam, None
        if cam is not None:
            try:
                cam.close()
            except Exception:
                pass

    # -------------------------------------------------- 线程
    def _open_camera(self):
        cfg = cfgmod.load().video
        try:
            import pyvirtualcam
        except Exception as exc:  # noqa: BLE001
            self._fail_camera(
                "未安装 pyvirtualcam，已降级为 dry-run",
                f"pip install pyvirtualcam  （{type(exc).__name__}: {exc}）",
            )
            return None

        base = dict(width=cfg.width, height=cfg.height, fps=self._fps_cfg,
                    fmt=pyvirtualcam.PixelFormat.RGB)
        backend = (cfg.backend or "").strip().lower()
        attempts: list[dict] = []
        if backend in ("unitycapture", "akvcam"):
            attempts.append(dict(base, backend=backend, device=cfg.device))
            attempts.append(dict(base, backend=backend))
        elif backend:
            # 注意：obs 后端不接受 device 关键字（nanobind 会报
            # "incompatible constructor arguments"），所以先不带
            attempts.append(dict(base, backend=backend))
        attempts.append(dict(base))          # 交给 pyvirtualcam 自己挑

        errors: list[str] = []
        for kwargs in attempts:
            try:
                cam = pyvirtualcam.Camera(**kwargs)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{kwargs.get('backend', 'auto')} → {type(exc).__name__}: {exc}")
                continue
            self._backend = str(getattr(cam, "backend", kwargs.get("backend", "auto")))
            self._dry_run = False
            self._error = None
            BUS.publish("video", action="camera_open", backend=self._backend,
                        device=str(getattr(cam, "device", "")),
                        resolution=f"{cfg.width}x{cfg.height}")
            return cam

        first = errors[0] if errors else "未知错误"
        self._fail_camera(
            "打不开虚拟摄像头，已降级为 dry-run（只生成预览帧）",
            " | ".join(errors[:3]),
            hint="先运行 python install_drivers.py --install 安装 OBS Virtual Camera；"
                 "安装后需重启本服务与 QQ/微信。",
            short_err=first,
        )
        return None

    def _fail_camera(self, short: str, detail: str, hint: str = "",
                     short_err: str = "") -> None:
        self._dry_run = True
        self._error = short
        self._error_detail = detail
        self._error_hint = hint
        BUS.publish("video", action="camera_missing", error=(short_err or detail)[:400],
                    detail=detail[:1200], hint=hint, dry_run=True)

    def _run(self) -> None:
        cam = self._open_camera()
        self._opened.set()
        with self._lock:
            self._cam = cam
        period = 1.0 / max(1, self._fps_cfg)
        t_next = time.perf_counter()
        last_fps_t = time.time()
        frames_since = 0
        while not self._stop.is_set():
            try:
                with self._lock:
                    renderer = self._renderer
                if renderer is None:
                    break
                frame = renderer.frame()
                if cam is not None:
                    cam.send(frame)
                    cam.sleep_until_next_frame()
                else:
                    # dry-run：按帧率节流 + 生成预览
                    now = time.time()
                    if now - self._preview_at > 0.3:
                        self._preview = self._encode_preview(frame)
                        self._preview_at = now
                    t_next += period
                    delay = t_next - time.perf_counter()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        t_next = time.perf_counter()
                self._frames += 1
                frames_since += 1
                if time.time() - last_fps_t >= 1.0:
                    self._fps_actual = frames_since / (time.time() - last_fps_t)
                    frames_since = 0
                    last_fps_t = time.time()
            except Exception as exc:  # noqa: BLE001
                self._error = f"推流异常: {type(exc).__name__}: {exc}"
                BUS.publish("video", action="error", error=self._error)
                time.sleep(0.5)
        with self._lock:
            self._close_cam_locked()

    @staticmethod
    def _encode_jpeg(frame: np.ndarray, quality: int = 80) -> bytes:
        buf = io.BytesIO()
        Image.fromarray(frame).save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    @staticmethod
    def _encode_preview(frame: np.ndarray, max_width: int = 960, quality: int = 72) -> bytes:
        """预览用：缩到 ≤960 宽再编码，省 CPU（推流本身仍是全分辨率）。"""
        img = Image.fromarray(frame)
        if img.width > max_width:
            height = max(1, int(img.height * max_width / img.width))
            img = img.resize((max_width, height), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    # -------------------------------------------------- 状态
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def preview_jpeg(self) -> bytes:
        with self._lock:
            if self._preview:
                return self._preview
            renderer = self._renderer
        if renderer is None:
            cfg = cfgmod.load().video
            return self._encode_preview(np.asarray(placeholder_image("AI", (cfg.width, cfg.height))))
        return self._encode_preview(renderer.frame())

    def status(self) -> dict:
        cfg = cfgmod.load().video
        with self._lock:
            return {
                "running": self.running,
                "dry_run": self._dry_run,
                "backend": self._backend or cfg.backend,
                "camera_open": self._cam is not None,
                "effect": self._renderer.effect if self._renderer else cfg.effect,
                "image": self._image_desc,
                "caption": self._renderer.caption if self._renderer else "",
                "resolution": f"{cfg.width}x{cfg.height}",
                "fps_target": self._fps_cfg,
                "fps_actual": round(self._fps_actual, 1),
                "frames": self._frames,
                "uptime": round(time.time() - self._started_at, 1) if self._started_at else 0,
                "error": self._error,
                "error_detail": self._error_detail[:600],
                "error_hint": self._error_hint,
            }
