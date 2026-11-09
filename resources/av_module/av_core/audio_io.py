"""音频 IO：设备枚举 / 播放到“虚拟麦克风” / 从“系统回环”录音。

两个通道的物理含义
------------------
写（AI 说话）:  TTS 的 PCM ──渲染──▶ 播放设备 "CABLE Input"
                                    └─ VB-Cable 内部环回 ─▶ 录音设备 "CABLE Output"
                                                              └─ QQ 里把“麦克风”选它，对方就听见 AI

读（听对方）  :  对方声音从 QQ 播到系统默认扬声器 ──WASAPI Loopback──▶ 本模块录音
                  （不需要改 QQ 的输出设备，也不会制造回声）

如果用户愿意，也可以用 `input_mode="device"` 直接录 "CABLE Output"
（需要把 QQ 的扬声器设到 CABLE Input，是另一套走法）。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np


class AudioError(RuntimeError):
    pass


# ---------------------------------------------------------------- COM 初始化
# soundcard 的 WASAPI 实现要调 COM。主线程往往已经被别的库（comtypes/COM 组件）
# 初始化过，但 uvicorn 的线程池、我们自己的录音线程都没有 —— 直接用会报
# `RuntimeError: Error 0x800401f0`（CO_E_NOTINITIALIZED）。
_com_state = threading.local()


def com_initialize() -> None:
    """在当前线程初始化 COM（幂等，可反复调用）。"""
    if getattr(_com_state, "done", False):
        return
    _com_state.done = True
    try:
        import ctypes

        hr = ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        # S_OK(0) / S_FALSE(1, 已初始化) 都算成功；其它值不阻断（交给 soundcard 报错）
        _com_state.hr = int(hr)
    except Exception:  # 非 Windows 或 ole32 不可用：忽略
        _com_state.hr = None


# ---------------------------------------------------------------- 工具函数

def rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def resample(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """线性插值重采样。语音够用，且零依赖（不必装 scipy/librosa）。"""
    if sr_from == sr_to or x.size == 0:
        return x.astype(np.float32, copy=False)
    n_out = max(1, int(round(len(x) * sr_to / sr_from)))
    src = np.arange(len(x), dtype=np.float64)
    dst = np.linspace(0.0, len(x) - 1.0, n_out)
    return np.interp(dst, src, x.astype(np.float64)).astype(np.float32)


def to_channels(x: np.ndarray, channels: int) -> np.ndarray:
    """单声道 -> 多声道（复制）；多声道 -> 单声道（平均）。"""
    if x.ndim == 1:
        if channels <= 1:
            return x.reshape(-1, 1)
        return np.repeat(x.reshape(-1, 1), channels, axis=1)
    if x.shape[1] == channels:
        return x
    if channels == 1:
        return x.mean(axis=1, keepdims=True)
    mix = x.mean(axis=1, keepdims=True)
    return np.repeat(mix, channels, axis=1)


def mono(x: np.ndarray) -> np.ndarray:
    if x.ndim == 2:
        return x.mean(axis=1)
    return x


def peak_normalize(x: np.ndarray, target: float = 0.85) -> np.ndarray:
    p = float(np.max(np.abs(x))) if x.size else 0.0
    if p < 1e-6:
        return x
    return (x * (target / p)).astype(np.float32)


def soft_clip(x: np.ndarray) -> np.ndarray:
    return np.clip(x, -1.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------- 设备枚举

def _sd():
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise AudioError(
            "缺少 sounddevice。请执行:  pip install sounddevice"
        ) from exc
    return sd


def list_devices() -> list[dict]:
    """PortAudio 设备表（播放/录音都在这）。"""
    sd = _sd()
    out: list[dict] = []
    try:
        hostapis = sd.query_hostapis()
    except Exception:
        hostapis = []
    for idx, dev in enumerate(sd.query_devices()):
        api = ""
        try:
            api = hostapis[dev["hostapi"]]["name"]
        except Exception:
            pass
        out.append({
            "index": idx,
            "name": dev["name"],
            "hostapi": api,
            "max_input_channels": dev["max_input_channels"],
            "max_output_channels": dev["max_output_channels"],
            "default_samplerate": dev["default_samplerate"],
            "is_default_output": idx == sd.default.device[1],
            "is_default_input": idx == sd.default.device[0],
        })
    return out


def list_loopback_sources() -> list[dict]:
    """WASAPI 回环源 = 每块扬声器都能被“录”一份。"""
    try:
        import soundcard as sc  # type: ignore
    except Exception:
        return []
    com_initialize()
    out: list[dict] = []
    try:
        for spk in sc.all_speakers():
            try:
                mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
                loop_name = getattr(mic, "name", f"{spk.name} [Loopback]")
            except Exception:
                loop_name = f"{spk.name} [Loopback]"
            out.append({"speaker": spk.name, "loopback_name": loop_name,
                        "is_default": spk.name == getattr(sc.default_speaker(), "name", "")})
    except Exception:
        return out
    return out


def describe_devices() -> dict:
    """给 UI/接口用的全量视图，并标出关键设备是否就绪。"""
    devices = list_devices()
    loop = list_loopback_sources()
    try:
        from . import config as cfgmod

        cfg = cfgmod.load()
        out_pat = cfg.audio.output_device
        in_pat = cfg.audio.input_device
    except Exception:
        out_pat, in_pat = "CABLE Input", "CABLE Output"

    def has(pat: str) -> list[int]:
        if not pat:
            return []
        low = pat.lower()
        return [d["index"] for d in devices if low in d["name"].lower()]

    virtual_out = [d for d in devices
                   if d["max_output_channels"] > 0
                   and any(k in d["name"].lower() for k in ("cable input", "vb-audio"))]
    virtual_in = [d for d in devices
                  if d["max_input_channels"] > 0
                  and any(k in d["name"].lower() for k in ("cable output", "vb-audio"))]
    return {
        "devices": devices,
        "loopback_sources": loop,
        "virtual_cable": {
            "playback_found": bool(virtual_out),
            "recording_found": bool(virtual_in),
            "playback": [d["name"] for d in virtual_out],
            "recording": [d["name"] for d in virtual_in],
        },
        "configured": {
            "output_device": out_pat,
            "output_match": has(out_pat),
            "input_device": in_pat,
            "input_match": has(in_pat),
        },
    }


def find_device(pattern: str, want_input: bool) -> int | None:
    """按名字子串找设备；优先 WASAPI。空 pattern 返回 None（=系统默认）。"""
    if not pattern:
        return None
    sd = _sd()
    low = pattern.strip().lower()
    candidates: list[tuple[int, dict]] = []
    for idx, dev in enumerate(sd.query_devices()):
        ch = dev["max_input_channels"] if want_input else dev["max_output_channels"]
        if ch <= 0:
            continue
        if low in dev["name"].lower():
            candidates.append((idx, dev))
    if not candidates:
        return None
    # 优先 WASAPI（虚拟声卡在 WASAPI 下最稳，MME 会重采样且延迟高）
    def score(item: tuple[int, dict]) -> int:
        idx, dev = item
        try:
            api = sd.query_hostapis(dev["hostapi"])["name"].lower()
        except Exception:
            api = ""
        return 0 if "wasapi" in api else 1

    candidates.sort(key=score)
    return candidates[0][0]


def resolve_output(device: str | int | None) -> int | None:
    if isinstance(device, int):
        return device
    return find_device(device or "", want_input=False)


def resolve_input(device: str | int | None) -> int | None:
    if isinstance(device, int):
        return device
    return find_device(device or "", want_input=True)


def device_label(idx: int | None) -> str:
    sd = _sd()
    if idx is None:
        try:
            return f"系统默认输出 (#{sd.default.device[1]})"
        except Exception:
            return "系统默认输出"
    try:
        return f"{sd.query_devices(idx)['name']} (#{idx})"
    except Exception:
        return f"#{idx}"


# ---------------------------------------------------------------- 播放器

class Player:
    """把 PCM 推到指定播放设备；支持随时打断（新的 TTS 抢话）。

    用常驻 OutputStream + 内部缓冲，避免 sd.play() 的全局状态互相踩。
    """

    def __init__(self, device: int | None = None, samplerate: int = 48000,
                 channels: int = 2, gain: float = 1.0):
        self.device = device
        self.samplerate = int(samplerate)
        self.channels = int(channels)
        self.gain = float(gain)
        self._sd = _sd()
        self._stream = None
        self._buf: np.ndarray = np.zeros((0, self.channels), dtype=np.float32)
        self._lock = threading.Lock()
        self._gen = 0                 # 每次打断 +1，用来让 play() 提前返回
        self._done = threading.Event()
        self._done.set()
        self._frames_written = 0
        self._last_error: str | None = None

    # -- 内部 --
    def _open(self) -> None:
        if self._stream is not None:
            return
        kwargs: dict[str, Any] = dict(
            samplerate=self.samplerate, channels=self.channels,
            dtype="float32", device=self.device, blocksize=0,
        )
        try:
            self._sd.check_output_settings(**kwargs)
        except Exception:
            # 设备不支持 48k/双声道：退到设备默认值
            try:
                info = self._sd.query_devices(self.device)
                self.samplerate = int(info["default_samplerate"]) or 44100
                kwargs["samplerate"] = self.samplerate
            except Exception:
                pass
        self._stream = self._sd.OutputStream(callback=self._callback, **kwargs)
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status):  # noqa: ANN001
        with self._lock:
            n = min(frames, self._buf.shape[0])
            if n > 0:
                outdata[:n] = self._buf[:n]
                self._buf = self._buf[n:]
            if n < frames:
                outdata[n:] = 0.0
                if self._buf.shape[0] == 0:
                    self._done.set()

    # -- 对外 --
    def play(self, pcm: np.ndarray, samplerate: int, block: bool = True) -> dict:
        """pcm: mono float32（任意采样率）。返回播放统计。"""
        if pcm.size == 0:
            return {"played": False, "reason": "empty"}
        self._open()
        data = resample(mono(pcm), int(samplerate), self.samplerate)
        data = soft_clip(data * self.gain)
        block_data = to_channels(data, self.channels)
        with self._lock:
            self._gen += 1
            my_gen = self._gen
            self._buf = np.zeros((0, self.channels), dtype=np.float32)  # 打断旧内容
            self._buf = block_data.copy()
            self._done.clear()
            self._frames_written = block_data.shape[0]
        if not block:
            return {"played": True, "seconds": len(data) / self.samplerate, "interrupted": False}
        # 等缓冲播完（或用 _gen 变化 / stop() 提前退出）
        deadline = time.time() + len(data) / self.samplerate + 5.0
        while time.time() < deadline:
            with self._lock:
                empty = self._buf.shape[0] == 0
                gen = self._gen
            if gen != my_gen:
                return {"played": True, "seconds": len(data) / self.samplerate,
                        "interrupted": True}
            if empty:
                time.sleep(0.03)
                with self._lock:
                    if self._buf.shape[0] == 0:
                        break
            else:
                time.sleep(0.01)
        return {"played": True, "seconds": len(data) / self.samplerate,
                "interrupted": False, "device": device_label(self.device),
                "samplerate": self.samplerate}

    def stop(self) -> None:
        with self._lock:
            self._gen += 1
            self._buf = np.zeros((0, self.channels), dtype=np.float32)
            self._done.set()

    def close(self) -> None:
        self.stop()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._buf.shape[0] > 0


# ---------------------------------------------------------------- 录音

def _loopback_mic(pattern: str = ""):
    """拿到系统扬声器的 WASAPI 回环"麦克风"。

    顺序很重要：先 import soundcard（它 import 时会自己 CoInitializeEx），
    再补一次本线程的 COM 初始化（幂等）。反过来会让 soundcard 拿到 S_FALSE。
    """
    import soundcard as sc  # type: ignore

    com_initialize()

    if pattern:
        low = pattern.lower()
        for spk in sc.all_speakers():
            if low in spk.name.lower():
                return sc.get_microphone(id=str(spk.name), include_loopback=True)
    spk = sc.default_speaker()
    if spk is None:
        raise AudioError("找不到系统默认播放设备，无法回环录音")
    return sc.get_microphone(id=str(spk.name), include_loopback=True)


# ============ v1.6.4（QA P1-2）：录音设备打不开时自动降级 ============
# QA实测：mode=device指定设备时抛 PortAudioError [Windows WDM-KS error -9999]
#整个 listen接口直接500。WDM-KS对独占/采样率/通道数极其挑剔，
#而 MME几乎什么都能开（只是延迟高）。按 WDM-KS → WASAPI → MME逐级降级。
_FALLBACK_HOST = {}


def _hostapi_name(sd, dev_idx):
    "设备所属 hostapi名字（小写），拿不到返回空串。"
    try:
        apis = sd.query_hostapis()
        d = sd.query_devices(int(dev_idx))
        return str(apis[int(d["hostapi"])]["name"] or "").lower()
    except Exception:
        return ""


def _same_dev_in_host(sd, dev_idx, want):
    "在同名设备的其它 hostapi里找等价输入设备；找不到返回 None。"
    try:
        cur = sd.query_devices(int(dev_idx))
        apis = sd.query_hostapis()
        target = str(cur.get("name") or "").strip().lower()
        if not target:
            return None
        for j, d in enumerate(sd.query_devices()):
            if int(d.get("max_input_channels") or 0) <=0:
                continue
            if str(d.get("name") or "").strip().lower() != target:
                continue
            api = str(apis[int(d["hostapi"])]["name"] or "").lower()
            if want and want in api:
                return j
    except Exception:
        pass
    return None


def _open_input(sd, dev_idx, channels, samplerate):
    "开录音流；失败就换 hostapi重试。返回 (stream,设备号,说明)。"
    attempts = []
    first = _FALLBACK_HOST.get(dev_idx)
    plan = []
    if first:
        plan.append((dev_idx, first))
    for want in ("wdm-ks", "wasapi", "mme", ""):
        if first and want == first:
            continue
        plan.append((dev_idx, want))
        alt = _same_dev_in_host(sd, dev_idx, want)
        if alt is not None and alt != dev_idx:
            plan.append((alt, want))
    for cand, want in plan:
        try:
            st = sd.InputStream(device=cand, channels=int(channels),
                samplerate=int(samplerate), dtype="float32")
            st.start()
            if want:
                _FALLBACK_HOST[dev_idx] = want
            note = "" if cand == dev_idx else ("已换到同名的其它 hostapi设备 #" + str(cand))
            return st, cand, note
        except Exception as exc: # noqa: BLE001
            attempts.append((want or "?") + ": " + str(exc)[:80])
    raise AudioError("录音流打不开（WDM-KS/WASAPI/MME都试过了）：" + 
        ("；".join(attempts[-3:]) or "未知"))


# v1.6.4：上一次录音的降级说明（"已换到别的 hostapi"之类），给上层提示用
_LAST_NOTE = []


def last_note():
    """取走上一次录音的降级说明（取完清空）。"""
    if _LAST_NOTE:
        n = "；".join(_LAST_NOTE)
        del _LAST_NOTE[:]
        return n
    return ""


def _warn_if_silent(audio, mode, dev):
    """录到全0 /全静音时留个note，别让上层以为"录成功了但对方没说话"。"""
    try:
        if audio is None or len(audio) ==0:
            _LAST_NOTE.append("录到0帧")
            return
        lv = float(np.max(np.abs(audio)))
        if lv <1e-6:
            _LAST_NOTE.append(
                "全程电平为0（" + ("麦克风" if mode == "device" else "系统声音")
                + " / " + str(dev) + "）：可能没声音、设备静音、或回环源选错了")
    except Exception:
        pass


def record_seconds(seconds: float, mode: str = "loopback",
                   device: str | int | None = None, samplerate: int = 48000,
                   channels: int = 2, loopback_device: str = "",
                   stop_event: threading.Event | None = None,
                   on_progress: Callable[[float], None] | None = None) -> np.ndarray:
    """录一段音频，返回 mono float32。"""
    seconds = max(0.2, min(float(seconds), 600.0))
    stop_event = stop_event or threading.Event()

    if mode == "device":
        sd = _sd()
        idx = resolve_input(device)
        need = int(seconds * samplerate)
        chunks = []
        got =0
        # v1.6.4：交给 _open_input，它会按 WDM-KS -> WASAPI -> MME降级重试
        stream, _used, _note = _open_input(sd, idx, channels, samplerate)
        if _note:
            _LAST_NOTE.append(_note)
        try:
            while got < need and not stop_event.is_set():
                n = min(2048, need - got)
                data, _overflowed = stream.read(n)
                chunks.append(np.array(data, dtype=np.float32))
                got += n
                if on_progress:
                    on_progress(got / max(1, need))
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        out = mono(np.concatenate(chunks, axis=0))
        _warn_if_silent(out, "device", _used)
        return out
    elif mode == "loopback":
        import soundcard as sc  # type: ignore

        mic = _loopback_mic(loopback_device)
        chunk = 4800
        want = int(seconds * samplerate)
        frames: list[np.ndarray] = []
        got = 0
        with mic.recorder(samplerate=samplerate, channels=channels) as rec:
            while got < want and not stop_event.is_set():
                data = rec.record(numframes=min(chunk, want - got))
                frames.append(np.array(data, dtype=np.float32))
                got += data.shape[0]
                if on_progress:
                    on_progress(got / max(1, want))
        if not frames:
            return np.zeros(0, dtype=np.float32)
        out = mono(np.concatenate(frames, axis=0))
        _warn_if_silent(out, "loopback", loopback_device or "系统默认扬声器")
        return out
    else:
        raise AudioError(f"未知录音模式: {mode}")


class StreamRecorder:
    """持续录音 + 内部缓冲，供“流式听写”和电平表用。"""

    def __init__(self, mode: str = "loopback", device: str | int | None = None,
                 samplerate: int = 48000, channels: int = 2,
                 loopback_device: str = "", max_seconds: float = 300.0):
        self.mode = mode
        self.device = device
        self.samplerate = int(samplerate)
        self.channels = int(channels)
        self.loopback_device = loopback_device
        self.max_samples = int(max_seconds * samplerate)
        self._buf = np.zeros(0, dtype=np.float32)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self.total_samples = 0
        self._level = 0.0

    # -- 采集线程 --
    def _run(self) -> None:
        # 注意：不要在这里先 com_initialize()！soundcard 在 import 时会自己调
        # CoInitializeEx(MTA)，若我们抢先初始化，它拿到 S_FALSE(1) 就当成错误抛
        # `RuntimeError: Error 0x100000001`。`_loopback_mic()` 里的顺序是
        # “先 import soundcard，再补 COM 初始化”，这里只管调它即可。
        try:
            if self.mode == "device":
                sd = _sd()
                idx = resolve_input(self.device)
                # v1.6.4：和 record_seconds一样走 hostapi降级，别让 WDM-KS把整条流打死
                stream, _u, _n = _open_input(sd, idx, self.channels, self.samplerate)
                if _n:
                    _LAST_NOTE.append(_n)
                try:
                    while not self._stop.is_set():
                        data, _ = stream.read(1024)
                        self._push(np.array(data, dtype=np.float32))
                finally:
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:
                        pass
            else:
                mic = _loopback_mic(self.loopback_device)
                with mic.recorder(samplerate=self.samplerate,
                                  channels=self.channels) as recc:
                    while not self._stop.is_set():
                        data = recc.record(numframes=2400)
                        self._push(np.array(data, dtype=np.float32))
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            self._stop.set()

    def _push(self, block: np.ndarray) -> None:
        m = mono(block)
        self._level = rms(m[-2400:]) if m.size else 0.0
        with self._lock:
            self._buf = np.concatenate([self._buf, m])
            if self._buf.size > self.max_samples:
                self._buf = self._buf[-self.max_samples:]
            self.total_samples += m.size

    # -- 对外 --
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, name="av-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._thread = None

    def read_all(self, clear: bool = False) -> np.ndarray:
        with self._lock:
            data = self._buf.copy()
            if clear:
                self._buf = np.zeros(0, dtype=np.float32)
        return data

    def drain(self, max_seconds: float | None = None) -> np.ndarray:
        """取走并清空缓冲（按时间切片用一个窗口的尾巴）。"""
        with self._lock:
            data = self._buf
            self._buf = np.zeros(0, dtype=np.float32)
        if max_seconds is not None:
            n = int(max_seconds * self.samplerate)
            if data.size > n:
                data = data[-n:]
        return data

    @property
    def level(self) -> float:
        return round(float(self._level), 4)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def seconds_buffered(self) -> float:
        with self._lock:
            return round(self._buf.size / self.samplerate, 2)


# ---------------------------------------------------------------- 测试音

def make_tone(freq: float = 440.0, seconds: float = 1.5, samplerate: int = 24000,
              fade: float = 0.02) -> np.ndarray:
    n = int(seconds * samplerate)
    t = np.arange(n, dtype=np.float32) / samplerate
    # 双音 + 淡入淡出，听起来像“叮—咚”，便于人耳确认路由对不对
    wave = 0.5 * np.sin(2 * math.pi * freq * t) + 0.3 * np.sin(2 * math.pi * freq * 1.5 * t)
    env = np.ones(n, dtype=np.float32)
    f = max(1, int(fade * samplerate))
    env[:f] = np.linspace(0, 1, f)
    env[-f:] = np.linspace(1, 0, f)
    return (wave * env * 0.6).astype(np.float32)


def make_counting_voice(samplerate: int = 24000) -> np.ndarray:
    """无 TTS 时的兜底测试音：滴答声列，用于验证“对方能听见”。"""
    out = []
    for i in range(3):
        out.append(make_tone(660 + 110 * i, 0.35, samplerate))
        out.append(np.zeros(int(0.15 * samplerate), dtype=np.float32))
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)
