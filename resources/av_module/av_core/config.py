"""配置：config.json 持久化 + AV_* 环境变量覆盖。

设计原则
--------
* 所有默认值都“能猜对”：默认把虚拟声卡当麦克风，默认用系统回环录音。
* 配置项扁平的两层结构（section.key），方便 HTTP PATCH 部分更新。
* 设备用“名字子串”而不是索引，因为 Windows 设备索引每次插拔都会变。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

def _is_frozen() -> bool:
    """是不是 PyInstaller 打出来的 exe。"""
    return bool(getattr(sys, "frozen", False))


# 代码 / 静态资源根目录
#   源码运行：项目根
#   PyInstaller 打包后：临时解包目录（sys._MEIPASS），退出就删
ROOT = Path(__file__).resolve().parent.parent

# 可写数据根目录
#   打包后 ROOT 是临时目录，配置 / TTS 缓存 / whisper 模型 / 模板图必须落到这里，
#   否则关掉服务就全丢了（而且一次几 GB 的模型每次启动都要重下）。
if _is_frozen():
    _home = (os.environ.get("AV_MODULE_HOME") or "").strip().strip('"')
    if not _home:
        _home = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "小鲸鱼", "av_module",
        )
    DATA_ROOT = Path(_home)
    try:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
    except Exception:                     # 目录被占用/无权限时退到系统临时目录
        DATA_ROOT = Path(os.environ.get("TEMP") or ".") / "xhy_av_module"
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
else:
    DATA_ROOT = ROOT

CONFIG_PATH = DATA_ROOT / "config.json"
STATIC_DIR = ROOT / "static"
DRIVERS_DIR = ROOT / "drivers"
ASSETS_DIR = ROOT / "assets"
RUNTIME_DIR = DATA_ROOT / "runtime"
TEMPLATE_DIR = DATA_ROOT / "rpa" / "templates"
BUNDLED_TEMPLATE_DIR = ROOT / "rpa" / "templates"


def _env(name: str, default: Any) -> Any:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on", "y")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 5800
    # 可选共享密钥：非空时所有 /api 请求需带 X-AV-Token
    token: str = ""
    log_level: str = "info"


@dataclass
class AudioConfig:
    # —— 输出（TTS -> 虚拟麦克风）——
    # 渲染到这块“播放设备”的声音，会出现在虚拟声卡的录音端，即对方听到的“我”
    output_device: str = "CABLE Input"
    # 没有虚拟声卡时的兜底播放设备（空 = 系统默认）
    fallback_output_device: str = ""
    out_sample_rate: int = 48000
    out_channels: int = 2
    out_gain: float = 1.0

    # —— 输入（对方声音 -> STT）——
    # loopback = 抓系统回环（推荐，无需改对方设备的输出路由）
    # device   = 抓指定录音设备（如 CABLE Output / 立体声混音）
    input_mode: str = "loopback"
    input_device: str = "CABLE Output"
    loopback_device: str = ""          # 空 = 跟随系统默认播放设备
    in_sample_rate: int = 48000
    in_channels: int = 2

    # —— 录音/切分 ——
    frame_ms: int = 20                 # 播放/录音块大小
    max_listen_seconds: int = 120
    silence_rms: float = 0.0010        # 低于此 RMS 视为静音（流式分段用；实测回环电平很低）
    silence_hang_ms: int = 250         # 连续静音这么久就切一段（越小响应越快、越容易切碎）


@dataclass
class TTSConfig:
    engine: str = "edge"               # edge | sapi | auto
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"
    timeout: float = 20.0
    # 语音被“打断”时的行为：True 表示新一句话直接打断上一句
    interrupt: bool = True
    # ============自定义音色（v1.8.0）============
    #用一段参考音频驱动本地的声音克隆服务（GPT-SoVITS / CosyVoice / Fish-Speech，
    #或者任何接受 JSON、回音频的 HTTP服务）。
    #设计底线：它挂了不吭声，静默退回 edge-tts，绝不让"说话"这件事坏掉。
    custom_enabled: bool = False #总开关（关=完全走老的 edge-tts）
    custom_url: str = "" #服务地址，如 http://127.0.0.1:9880/tts
    # gpt_sovits | cosyvoice | fish | openai | simple
    custom_format: str = "gpt_sovits"
    custom_ref_audio: str = "" #参考音频路径（相对路径按数据目录算）
    custom_ref_text: str = "" #参考音频里说的那句话（GPT-SoVITS必填）
    custom_voice_id: str = "" #声音ID/模型名（fish/openai用）
    custom_model: str = "tts-1" #兼容 OpenAI时的模型名
    custom_token: str = "" #可选 Bearer token
    custom_lang: str = "zh"
    custom_voice_name: str = "" #只用于界面显示"
    custom_timeout: float =30.0 #服务慢就慢，30秒够克隆一句话了


@dataclass
class STTConfig:
    engine: str = "faster-whisper"
    model: str = "small"               # tiny/base/small/medium（base 以上中文才靠谱）
    device: str = "cpu"                # cpu | cuda
    compute_type: str = "int8"         # int8 / int8_float16 / float16
    language: str = "zh"
    beam_size: int = 1
    vad_filter: bool = True
    download_root: str = ""            # 空 = runtime/models
    hf_endpoint: str = "https://hf-mirror.com"   # 模型下载源（官方源在国内基本不通）
    stream_window_s: float = 4.0     # 流式听写窗口（秒）；3s 太短容易截断整句       # 流式听写窗口


@dataclass
class VideoConfig:
    width: int = 1280
    height: int = 720
    fps: int = 24
    backend: str = "obs"               # pyvirtualcam 后端：obs / unitycapture / akvcam
    device: int = 0
    effect: str = "kenburns"           # still | kenburns | pulse
    zoom: float = 1.06
    # 绿幕底色（部分客户端需要非透明帧）
    background: str = "#101018"


@dataclass
class RPAConfig:
    # v1.9.8-Beta：小批量测试期间默认关闭（自动接管鼠标键盘容易被杀软误报）
    enabled: bool = False
    dry_run: bool = False
    poll_interval: float = 0.8
    # 只在这些窗口上找按钮，避免误点别的程序
    window_keywords: list = field(default_factory=lambda: [
        "QQ", "微信", "WeChat", "通话", "Call", "Voice", "Video",
    ])
    accept_labels: list = field(default_factory=lambda: [
        "接听", "接听电话", "接听通话", "接受", "Accept", "Answer", "Accept call",
    ])
    hangup_labels: list = field(default_factory=lambda: [
        "挂断", "挂断电话", "结束通话", "拒绝", "Hang up", "Hangup", "End call", "Decline",
    ])
    # v1.4.0：主动拨打时找按钮用的标签（QQ 里是「语音通话」「视频通话」）
    voice_labels: list = field(default_factory=lambda: [
        "语音通话", "语音", "打电话", "Voice call", "Voice", "Call",
    ])
    video_labels: list = field(default_factory=lambda: [
        "视频通话", "视频", "Video call", "Video",
    ])
    # 接听后静音原设备的麦克风（避免真实麦克风抢麦）——交给系统，不强制
    log_size: int = 200

    # —— 图像识别兜底（UIA 拿不到按钮时用模板匹配 + 鼠标点击）——
    image_fallback: bool = True        # UIA 探到 0 个控件时自动切图像识别
    template_dir: str = "rpa/templates"  # 按钮模板目录（accept_*.png / hangup_*.png）
    # v1.6.4（QA P1-1）：原来0.80太严，接听按钮差一点点就全军覆没。
    #灰度 NCC在纯色圆按钮上本来就偏低，0.72配合颜色校验才不会误判。
    match_threshold: float =0.72
    #模板和实际按钮的尺寸常差一截（不同分辨率/DPI/QQ版本），多给几档缩放
    match_scales: list = field(default_factory=lambda: [1.0,0.85,1.15,0.7,1.3,1.5,0.6,1.8])
    #模板都没命中时，按颜色找「绿圆=接听 /红圆=挂断」兜底
    color_fallback: bool = True
    # 自动挂断默认关闭：接听成功后再"挂断"就会把通话掐了，交给调用方决定
    auto_hangup: bool = False
    hangup_grace_s: float = 15.0       # 接听后这么久内绝不点挂断
    accept_cooldown_s: float = 5.0     # 同一按钮最小点击间隔
    hangup_cooldown_s: float = 8.0


@dataclass
class DriverConfig:
    # 允许服务端触发驱动安装（默认关闭，避免被远程点装驱动）
    allow_remote_install: bool = False
    vbcable_installer: str = "VBCABLE_Setup_x64.exe"
    vbcable_args: list = field(default_factory=lambda: ["-i", "-h"])
    virtualcam_installer: str = "obs-virtualcam-2.0.5-Windows-installer.exe"
    virtualcam_args: list = field(default_factory=lambda: ["/S"])
    install_timeout: int = 300


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    video: VideoConfig = field(default_factory=VideoConfig)
    rpa: RPAConfig = field(default_factory=RPAConfig)
    drivers: DriverConfig = field(default_factory=DriverConfig)

    # ---------- 序列化 ----------
    def to_dict(self) -> dict:
        return asdict(self)

    def section(self, name: str) -> Any:
        if not hasattr(self, name):
            raise KeyError(f"未知配置段: {name}")
        return getattr(self, name)

    def patch(self, data: dict) -> dict:
        """部分更新：{"audio": {"output_device": "..."}, "server": {"port": 5800}}"""
        changed: dict = {}
        for section, values in (data or {}).items():
            if not isinstance(values, dict):
                raise ValueError(f"配置段 {section} 必须是对象")
            if not hasattr(self, section):
                raise KeyError(f"未知配置段: {section}")
            target = getattr(self, section)
            for key, value in values.items():
                if not hasattr(target, key):
                    raise KeyError(f"未知配置项: {section}.{key}")
                cur = getattr(target, key)
                if isinstance(cur, bool) and not isinstance(value, bool):
                    value = str(value).lower() in ("1", "true", "yes", "on")
                elif isinstance(cur, int) and not isinstance(cur, bool):
                    value = int(value)
                elif isinstance(cur, float):
                    value = float(value)
                elif isinstance(cur, list) and isinstance(value, str):
                    value = [v.strip() for v in value.split(",") if v.strip()]
                if cur != value:
                    setattr(target, key, value)
                    changed[f"{section}.{key}"] = value
        return changed


_lock = threading.RLock()
_config: AppConfig | None = None


def _apply_env(cfg: AppConfig) -> AppConfig:
    s, a, t, st, v, r, d = (
        cfg.server, cfg.audio, cfg.tts, cfg.stt, cfg.video, cfg.rpa, cfg.drivers,
    )
    s.host = _env("AV_HOST", s.host)
    s.port = int(_env("AV_PORT", s.port))
    s.token = _env("AV_TOKEN", s.token)
    s.log_level = _env("AV_LOG_LEVEL", s.log_level)

    a.output_device = _env("AV_OUTPUT_DEVICE", a.output_device)
    a.fallback_output_device = _env("AV_FALLBACK_OUTPUT_DEVICE", a.fallback_output_device)
    a.input_mode = _env("AV_INPUT_MODE", a.input_mode)
    a.input_device = _env("AV_INPUT_DEVICE", a.input_device)
    a.loopback_device = _env("AV_LOOPBACK_DEVICE", a.loopback_device)
    a.out_sample_rate = int(_env("AV_OUT_SR", a.out_sample_rate))
    a.in_sample_rate = int(_env("AV_IN_SR", a.in_sample_rate))
    a.out_gain = float(_env("AV_OUT_GAIN", a.out_gain))

    t.engine = _env("AV_TTS_ENGINE", t.engine)
    t.voice = _env("AV_TTS_VOICE", t.voice)
    t.rate = _env("AV_TTS_RATE", t.rate)
    t.volume = _env("AV_TTS_VOLUME", t.volume)

    st.model = _env("AV_STT_MODEL", st.model)
    st.device = _env("AV_STT_DEVICE", st.device)
    st.compute_type = _env("AV_STT_COMPUTE", st.compute_type)
    st.language = _env("AV_STT_LANG", st.language)
    st.download_root = _env("AV_STT_ROOT", st.download_root)
    st.hf_endpoint = _env("AV_HF_ENDPOINT", st.hf_endpoint)

    v.width = int(_env("AV_VIDEO_WIDTH", v.width))
    v.height = int(_env("AV_VIDEO_HEIGHT", v.height))
    v.fps = int(_env("AV_VIDEO_FPS", v.fps))
    v.backend = _env("AV_VIDEO_BACKEND", v.backend)
    v.effect = _env("AV_VIDEO_EFFECT", v.effect)

    r.enabled = _env("AV_RPA_ENABLED", r.enabled)
    r.dry_run = _env("AV_RPA_DRY_RUN", r.dry_run)
    r.image_fallback = _env("AV_RPA_IMAGE_FALLBACK", r.image_fallback)
    r.auto_hangup = _env("AV_RPA_AUTO_HANGUP", r.auto_hangup)
    r.template_dir = _env("AV_RPA_TEMPLATE_DIR", r.template_dir)
    r.match_threshold = float(_env("AV_RPA_THRESHOLD", r.match_threshold))

    d.allow_remote_install = _env("AV_ALLOW_REMOTE_INSTALL", d.allow_remote_install)
    return cfg


def load(path: Path | None = None) -> AppConfig:
    """读取配置（带缓存）。文件不存在则用默认值。"""
    global _config
    with _lock:
        if _config is not None and path is None:
            return _config
        p = Path(path) if path else CONFIG_PATH
        cfg = AppConfig()
        if p.exists():
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
                for section, values in raw.items():
                    if hasattr(cfg, section) and isinstance(values, dict):
                        target = getattr(cfg, section)
                        for k, val in values.items():
                            if hasattr(target, k):
                                setattr(target, k, val)
            except Exception as exc:  # 配置坏了不能拖垮服务
                print(f"[config] 读取 {p} 失败，使用默认配置: {exc}")
        cfg = _apply_env(cfg)
        if _is_frozen():
            # 打包后 ROOT 在临时目录里，模板图/截图要落到可写目录
            tpl = str(cfg.rpa.template_dir or "").strip()
            if not tpl or not os.path.isabs(tpl):
                cfg.rpa.template_dir = str(TEMPLATE_DIR)
        if path is None:
            _config = cfg
        return cfg


def save(cfg: AppConfig | None = None, path: Path | None = None) -> Path:
    global _config
    with _lock:
        cfg = cfg or load()
        p = Path(path) if path else CONFIG_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if path is None:
            _config = cfg
        return p


def ensure_dirs() -> None:
    for d in (STATIC_DIR, DRIVERS_DIR, ASSETS_DIR, RUNTIME_DIR, TEMPLATE_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    # 打包版：把随包带的示例模板复制到可写目录（不覆盖用户自己的）
    try:
        if BUNDLED_TEMPLATE_DIR.is_dir() and BUNDLED_TEMPLATE_DIR != TEMPLATE_DIR:
            for f in BUNDLED_TEMPLATE_DIR.glob("*.png"):
                dst = TEMPLATE_DIR / f.name
                if not dst.exists():
                    shutil.copy2(f, dst)
    except Exception:
        pass
