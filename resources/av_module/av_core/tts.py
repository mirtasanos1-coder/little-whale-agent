"""TTS：edge-tts 在线合成（默认） + Windows SAPI 离线兜底。

输出统一为 mono float32 PCM，交给 audio_io.Player 推入虚拟声卡。
MP3 解码顺序：miniaudio（纯 wheel，无需 ffmpeg） → ffmpeg 命令行。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config as cfgmod

_TTS_LOCK = threading.Lock()


@dataclass
class TTSResult:
    pcm: np.ndarray
    samplerate: int
    engine: str
    voice: str = ""
    cached: bool = False
    file: str | None = None
    seconds: float = 0.0
    meta: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "engine": self.engine,
            "voice": self.voice,
            "samplerate": self.samplerate,
            "seconds": round(float(self.seconds), 3),
            "cached": self.cached,
            "file": self.file,
        }


# ---------------------------------------------------------------- 解码

def decode_to_pcm(data: bytes) -> tuple[np.ndarray, int]:
    """MP3/WAV bytes -> (mono float32, samplerate)。"""
    # 1) miniaudio
    try:
        import miniaudio  # type: ignore

        decoded = miniaudio.decode(data)
        samples = np.frombuffer(decoded.samples, dtype=np.int16).astype(np.float32) / 32768.0
        if decoded.nchannels > 1:
            samples = samples.reshape(-1, decoded.nchannels).mean(axis=1)
        return samples.astype(np.float32), int(decoded.sample_rate)
    except Exception:
        pass

    # 2) ffmpeg 兜底（用户机器上常常有）
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            out = subprocess.run(
                [ffmpeg, "-v", "quiet", "-i", tmp, "-f", "f32le",
                 "-acodec", "pcm_f32le", "-ac", "1", "-ar", "24000", "-"],
                capture_output=True, timeout=60,
            )
            if out.returncode == 0 and out.stdout:
                pcm = np.frombuffer(out.stdout, dtype=np.float32).copy()
                return pcm, 24000
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    raise RuntimeError(
        "无法解码 TTS 音频：请安装 miniaudio（pip install miniaudio）或 ffmpeg"
    )


def write_wav(path: Path, pcm: np.ndarray, samplerate: int) -> Path:
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(pcm, -1.0, 1.0)
    ints = (data * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(samplerate))
        w.writeframes(ints.tobytes())
    return path


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    import wave

    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        raw = w.readframes(n)
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


# ---------------------------------------------------------------- edge-tts

def edge_available() -> bool:
    try:
        import edge_tts  # noqa: F401

        return True
    except Exception:
        return False


async def _edge_synth_async(text: str, voice: str, rate: str, volume: str,
                            pitch: str, timeout: float) -> bytes:
    import edge_tts

    kwargs = dict(rate=rate, volume=volume)
    try:
        communicate = edge_tts.Communicate(text, voice, pitch=pitch, **kwargs)
    except TypeError:  # 老版本没有 pitch
        communicate = edge_tts.Communicate(text, voice, **kwargs)
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            chunks.append(chunk["data"])
    if not chunks:
        raise RuntimeError("edge-tts 没有返回音频（网络或文本为空？）")
    return b"".join(chunks)


def synth_edge(text: str, voice: str, rate: str = "+0%", volume: str = "+0%",
               pitch: str = "+0Hz", timeout: float = 25.0) -> bytes:
    """在独立事件循环里跑 edge-tts（调用方通常在线程池里）。"""
    async def runner() -> bytes:
        return await asyncio.wait_for(
            _edge_synth_async(text, voice, rate, volume, pitch, timeout),
            timeout=timeout,
        )

    try:
        return asyncio.run(runner())
    except RuntimeError as exc:
        if "asyncio.run() cannot be called" in str(exc):
            # 已在事件循环里：换个线程跑
            box: dict = {}

            def worker() -> None:
                try:
                    box["data"] = asyncio.run(runner())
                except Exception as e:  # noqa: BLE001
                    box["err"] = e

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            t.join(timeout + 5)
            if "err" in box:
                raise box["err"]
            return box.get("data", b"")
        raise


# ---------------------------------------------------------------- SAPI 兜底

def sapi_available() -> bool:
    if os.name != "nt":
        return False
    try:
        import comtypes.client  # noqa: F401

        return True
    except Exception:
        return False


def synth_sapi(text: str, rate: int = 0) -> tuple[np.ndarray, int]:
    """用 Windows 内置语音合成器，直接输出 WAV。完全离线。"""
    import comtypes.client  # type: ignore

    tmp = Path(tempfile.mkdtemp(prefix="av_sapi_")) / "sapi.wav"
    voice = comtypes.client.CreateObject("SAPI.SpVoice")
    stream = comtypes.client.CreateObject("SAPI.SpFileStream")
    SSFMCreateForWrite = 3
    stream.Open(str(tmp), SSFMCreateForWrite, False)
    old = voice.AudioOutputStream
    try:
        voice.AudioOutputStream = stream
        try:
            voice.Rate = int(rate)
        except Exception:
            pass
        voice.Speak(text)
    finally:
        try:
            voice.AudioOutputStream = old
        except Exception:
            pass
        stream.Close()
    pcm, sr = read_wav(tmp)
    try:
        tmp.unlink()
        tmp.parent.rmdir()
    except OSError:
        pass
    return pcm, sr


# ---------------------------------------------------------------- 对外入口

def sapi_voice_name() -> str:
    """SAPI 默认音色的描述文字（取不到返回空串），用于状态回报。"""
    try:
        import comtypes.client  # type: ignore

        v = comtypes.client.CreateObject("SAPI.SpVoice")
        return str(v.GetVoices().Item(0).GetDescription() or "")
    except Exception:
        return ""


def cache_dir() -> Path:
    d = cfgmod.RUNTIME_DIR / "tts_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_key(text, engine, voice, rate, volume, pitch, extra=""):
    raw = json.dumps([engine, voice, rate, volume, pitch, extra, text],
         ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

# ============================================================自定义音色（v1.8.0）
#用一句"参考音频"去驱动一个声音克隆服务（GPT-SoVITS / CosyVoice / Fish-Speech /
#任何兼容 OpenAI /audio/speech的服务）。
#
#设计底线：这东西是"加分项"，不是"命脉"。
#没配、没启动、超时、返回乱七八糟 ——一律当没这回事，
#静默退回 edge-tts，用户听到的只是声音变了，而不是"说话坏了"。
CUSTOM_FORMATS = ("gpt_sovits", "cosyvoice", "fish", "openai", "simple")


def custom_configured(cfg=None) -> bool:
    """配好了没有（不联网、不试连，只看配置齐不齐）。"""
    try:
        cfg = cfg or cfgmod.load()
        t = cfg.tts
        if not bool(getattr(t, "custom_enabled", False)):
            return False
        if not str(getattr(t, "custom_url", "") or "").strip():
            return False
        fmt = str(getattr(t, "custom_format", "gpt_sovits") or "").strip().lower()
        if fmt in ("gpt_sovits", "cosyvoice") and not _ref_audio_path(t):
            return False
        return True
    except Exception:
        return False


def _ref_audio_path(tts_cfg) -> str:
    raw = str(getattr(tts_cfg, "custom_ref_audio", "") or "").strip()
    if not raw:
        return ""
    try:
        p = Path(raw)
        if not p.is_absolute():
            p = Path(cfgmod.DATA_ROOT) / p
        return str(p) if p.is_file() else ""
    except Exception:
        return ""


def custom_fingerprint(cfg=None) -> str:
    """把"影响音色"的配置揉成一个短串，进缓存 key。

    换了参考音频/换了服务地址必须让旧缓存失效 ——否则用户换了声音，
    听到的还是上一次那个，会以为没生效。
    """
    try:
        cfg = cfg or cfgmod.load()
        t = cfg.tts
        ref = _ref_audio_path(t)
        stamp = ""
        if ref:
            try:
                stamp = str(int(os.path.getmtime(ref)))
            except Exception:
                stamp = "0"
        parts = [str(getattr(t, "custom_url", "")),
                str(getattr(t, "custom_format", "")),
                str(getattr(t, "custom_voice_id", "")),
                str(getattr(t, "custom_ref_text", "")),
                ref, stamp]
        raw = "\x1f".join(parts)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    except Exception:
        return "nocfg"


def _custom_body(text: str, t, fmt: str) -> dict:
    """按目标服务的习惯拼请求体。只做最常见的几种，其它一律 simple。"""
    ref = _ref_audio_path(t)
    ref_text = str(getattr(t, "custom_ref_text", "") or "")
    vid = str(getattr(t, "custom_voice_id", "") or "")
    lang = str(getattr(t, "custom_lang", "zh") or "zh")
    if fmt == "gpt_sovits":
        return {"text": text, "text_lang": lang,
                "ref_audio_path": ref, "prompt_text": ref_text,
                "prompt_lang": lang, "media_type": "wav",
                "streaming_mode": False}
    if fmt == "cosyvoice":
        return {"text": text, "prompt_audio": ref, "prompt_text": ref_text,
                "voice_id": vid}
    if fmt == "fish":
        return {"text": text, "reference_id": vid, "format": "wav"}
    if fmt == "openai":
        return {"model": str(getattr(t, "custom_model", "tts-1") or "tts-1"),
                "input": text, "voice": vid or "alloy", "response_format": "wav"}
    return {"text": text, "ref_audio": ref, "ref_text": ref_text,
            "voice_id": vid, "format": "wav"}


def _custom_pick_audio(obj, raw: bytes) -> bytes:
    """服务返回的可能是二进制音频，也可能是 JSON里塞 base64。都接住。"""
    if raw[:2] == b"PK" or raw[:4] == b"RIFF" or raw[:3] == b"ID3":
        return raw
    try:
        js = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return raw
    if not isinstance(js, dict):
        return raw
    for k in ("audio", "data", "audio_base64", "wav", "result"):
        v = js.get(k)
        if isinstance(v, str) and len(v) >64:
            try:
                return base64.b64decode(v)
            except Exception:
                continue
    raise RuntimeError("自定义音色服务没返回音频（%s）"
            % str(js.get("msg") or js.get("error") or "空响应")[:80])


def synth_custom(text: str, t=None, timeout: float | None = None) -> tuple:
    """把 text交给自定义音色服务，拿回 (pcm, samplerate)。失败就抛，由调用方降级。"""
    import urllib.request
    cfg = cfgmod.load()
    t = t or cfg.tts
    url = str(getattr(t, "custom_url", "") or "").strip()
    if not url:
        raise RuntimeError("没配自定义音色服务地址")
    fmt = str(getattr(t, "custom_format", "gpt_sovits") or "").strip().lower()
    if fmt not in CUSTOM_FORMATS:
        fmt = "simple"
    body = json.dumps(_custom_body(text, t, fmt),
            ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8",
                "Accept": "audio/*, application/json"}
    tok = str(getattr(t, "custom_token", "") or "").strip()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    to = float(timeout or getattr(t, "custom_timeout",30.0) or 30.0)
    to = max(5.0, min(to,120.0))
    with urllib.request.urlopen(req, timeout=to) as resp:
        raw = resp.read()
    if not raw:
        raise RuntimeError("自定义音色服务返回空")
    blob = _custom_pick_audio(None, raw)
    pcm, sr = decode_to_pcm(blob)
    if getattr(pcm, "size",0) ==0:
        raise RuntimeError("自定义音色合成结果为空")
    return pcm, sr


def custom_status(cfg=None) -> dict:
    """给前端看的：配了没、缺什么。"""
    try:
        cfg = cfg or cfgmod.load()
        t = cfg.tts
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}
    ref = _ref_audio_path(t)
    ok = custom_configured(cfg)
    why = ""
    if not bool(getattr(t, "custom_enabled", False)):
        why = "自定义音色没打开"
    elif not str(getattr(t, "custom_url", "") or "").strip():
        why = "还没填服务地址"
    elif str(getattr(t, "custom_format", "")) in ("gpt_sovits", "cosyvoice") and not ref:
        why = "还没上传参考音频（或文件已丢失）"
    return {
        "ok": True, "ready": ok, "reason": why,
        "url": str(getattr(t, "custom_url", "")),
        "format": str(getattr(t, "custom_format", "")),
        "ref_audio": ref,
        "ref_audio_name": os.path.basename(ref) if ref else "",
        "ref_text": str(getattr(t, "custom_ref_text", "")),
        "voice_id": str(getattr(t, "custom_voice_id", "")),
        "voice_name": str(getattr(t, "custom_voice_name", "")),
        "enabled": bool(getattr(t, "custom_enabled", False)),
        "timeout": float(getattr(t, "custom_timeout", 30.0) or 30.0),
    }

def _cfp(cfg) -> str:
    """只有真配了自定义音色，才让指纹进缓存 key。没配就是老行为，不破坏老缓存。"""
    try:
        return custom_fingerprint(cfg) if custom_configured(cfg) else ""
    except Exception:
        return ""


def synthesize(text: str, voice: str | None = None, rate: str | None = None,
               volume: str | None = None, pitch: str | None = None,
               engine: str | None = None, use_cache: bool = True,
               keep_file: bool = True) -> TTSResult:
    """文字 -> PCM。顺序：自定义音色 -> edge -> sapi，逐个降级。"""
    cfg = cfgmod.load()
    text = (text or "").strip()
    if not text:
        raise ValueError("text 不能为空")
    if len(text) > 4000:
        text = text[:4000]

    engine = (engine or cfg.tts.engine or "auto").lower()
    voice = voice or cfg.tts.voice
    rate = rate if rate is not None else cfg.tts.rate
    volume = volume if volume is not None else cfg.tts.volume
    pitch = pitch if pitch is not None else cfg.tts.pitch

    key = _cache_key(text, engine, voice, rate, volume, pitch,
                _cfp(cfg))
    wav_path = cache_dir() / f"{key}.wav"
    if use_cache and wav_path.is_file():
        try:
            pcm, sr = read_wav(wav_path)
            return TTSResult(pcm=pcm, samplerate=sr, engine=f"{engine}(cache)",
                             voice=voice, cached=True,
                             file=str(wav_path) if keep_file else None,
                             seconds=len(pcm) / sr)
        except Exception:
            pass

    errors: list[str] = []
    # v1.8.0：配了自定义音色就它先上；它挂了（超时/服务没开/返回垃圾）
    #自动往后退到 edge -> sapi。用户只会觉得"声音变了"，不会觉得"说不出话"。
    if engine == "sapi":
        order = ["sapi"]
    elif engine in ("custom_voice", "auto") and custom_configured(cfg):
        order = ["custom_voice", "edge", "sapi"]
    else:
        order = ["edge", "sapi"]

    with _TTS_LOCK:
        for eng in order:
            try:
                if eng == "custom_voice":
                    pcm, sr = synth_custom(text, cfg.tts)
                elif eng == "edge":
                    if not edge_available():
                        raise RuntimeError("未安装 edge-tts")
                    blob = synth_edge(text, voice, rate, volume, pitch, cfg.tts.timeout)
                    pcm, sr = decode_to_pcm(blob)
                elif eng == "sapi":
                    if not sapi_available():
                        raise RuntimeError("SAPI 不可用（需要 comtypes，仅 Windows）")
                    pcm, sr = synth_sapi(text)
                else:
                    raise RuntimeError(f"未知 TTS 引擎: {eng}")

                if pcm.size == 0:
                    raise RuntimeError("合成结果为空")
                wav = write_wav(wav_path, pcm, sr)
                # 用 SAPI 时不要把 edge 音色名回报给调用方，否则状态里会显示错音色
                if eng == "edge":
                    voice_label = voice
                elif eng == "custom_voice":
                    voice_label = (str(getattr(cfg.tts, "custom_voice_name", "")
                                   or "") or "自定义音色")
                else:
                    voice_label = sapi_voice_name() or "SAPI默认音色"
                return TTSResult(
                    pcm=pcm, samplerate=sr, engine=eng, voice=voice_label,
                    cached=False, file=str(wav) if keep_file else None,
                    seconds=len(pcm) / sr,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{eng}: {type(exc).__name__}: {exc}")

    raise RuntimeError("所有 TTS 引擎都失败了 -> " + " | ".join(errors))


def list_edge_voices(locale: str = "zh") -> list[dict]:
    """列出可用中文音色（需联网，失败返回空）。"""
    try:
        import edge_tts

        async def runner():
            return await edge_tts.list_voices()

        voices = asyncio.run(runner())
    except Exception:
        return []
    out = []
    for v in voices:
        short = v.get("ShortName", "")
        if locale and locale.lower() not in short.lower():
            continue
        out.append({
            "name": short,
            "gender": v.get("Gender"),
            "locale": short.split("-")[0] if short else "",
            "friendly": short.split("-")[-1].replace("Neural", "") if short else "",
        })
    return out
