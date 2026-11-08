"""编排层：把 TTS / 播放 / 录音 / STT / 推流 组装成对外的“通话动作”。

服务器只做参数校验和并发调度，真正的能力都在这里，
所以 `selftest.py` 可以完全不启 HTTP 就测完整条链路。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np

from . import audio_io, config as cfgmod, stt as sttmod, tts as ttsmod, video as videomod
from .audio_io import AudioError
from .events import BUS


class AVPipeline:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._player: audio_io.Player | None = None
        self._player_key: tuple | None = None
        self._recorder: audio_io.StreamRecorder | None = None
        self._transcriber: sttmod.StreamTranscriber | None = None
        self.cam = videomod.VirtualCam()
        self._speak_count = 0
        self._last_speak: dict | None = None
        self._listen_count = 0
        self._last_listen: dict | None = None

    # ------------------------------------------------------------ 播放
    def _player_for_config(self) -> audio_io.Player:
        cfg = cfgmod.load().audio
        idx = audio_io.resolve_output(cfg.output_device)
        used_fallback = False
        if idx is None:
            # 没装虚拟声卡：退到 fallback 或系统默认，保证“能听到”以便自测
            idx = audio_io.resolve_output(cfg.fallback_output_device or "")
            used_fallback = True
        if used_fallback:
            BUS.publish("audio", action="warn",
                        message=f"未找到输出设备『{cfg.output_device}』，已退到系统默认输出；"
                                f"对方将听不到声音（声音进不了虚拟声卡）。")
        key = (idx, cfg.out_sample_rate, cfg.out_channels, cfg.out_gain)
        with self._lock:
            if self._player is None or self._player_key != key:
                if self._player is not None:
                    self._player.close()
                self._player = audio_io.Player(
                    device=idx, samplerate=cfg.out_sample_rate,
                    channels=cfg.out_channels, gain=cfg.out_gain,
                )
                self._player_key = key
            return self._player

    def output_target(self) -> dict:
        cfg = cfgmod.load().audio
        idx = audio_io.resolve_output(cfg.output_device)
        virtual = idx is not None
        return {
            "configured": cfg.output_device,
            "resolved_index": idx,
            "resolved_name": audio_io.device_label(idx) if virtual else None,
            "virtual_cable": virtual,
            "note": "声音已进入虚拟声卡" if virtual else
                    f"未找到『{cfg.output_device}』，当前会从系统扬声器直接外放",
        }

    def play_pcm(self, pcm: np.ndarray, samplerate: int, block: bool = True) -> dict:
        player = self._player_for_config()
        res = player.play(pcm, samplerate, block=block)
        res["device"] = audio_io.device_label(player.device)
        res["virtual_cable"] = audio_io.resolve_output(
            cfgmod.load().audio.output_device) is not None
        return res

    def stop_speaking(self) -> dict:
        with self._lock:
            player = self._player
        if player:
            player.stop()
        BUS.publish("audio", action="speak_stop")
        return {"ok": True}

    def speak(self, text: str, voice: str | None = None, rate: str | None = None,
              volume: str | None = None, pitch: str | None = None,
              engine: str | None = None, block: bool = True,
              interrupt: bool | None = None) -> dict:
        cfg = cfgmod.load()
        if interrupt is None:
            interrupt = cfg.tts.interrupt
        with self._lock:
            player = self._player_for_config() if interrupt else None
        if interrupt and player is not None:
            player.stop()   # 打断上一句，避免两句话叠在一起

        t0 = time.time()
        result = ttsmod.synthesize(text, voice=voice, rate=rate, volume=volume,
                                   pitch=pitch, engine=engine)
        synth_ms = int((time.time() - t0) * 1000)
        BUS.publish("audio", action="tts", engine=result.engine,
                    seconds=round(result.seconds, 2), text=text[:60],
                    cached=result.cached, synth_ms=synth_ms)
        played = self.play_pcm(result.pcm, result.samplerate, block=block)
        out = {
            "ok": True,
            "text": text,
            "engine": result.engine,
            "voice": result.voice,
            "audio_seconds": round(result.seconds, 2),
            "synth_ms": synth_ms,
            "cached": result.cached,
            "file": result.file,
            "played": played,
            "total_ms": int((time.time() - t0) * 1000),
        }
        with self._lock:
            self._speak_count += 1
            self._last_speak = out
        BUS.publish("audio", action="speak_done", seconds=out["audio_seconds"],
                    dialog=bool(played.get("virtual_cable")))
        return out

    def play_file(self, path: str | Path, block: bool = True) -> dict:
        p = Path(path)
        if not p.is_absolute():
            p = cfgmod.ROOT / p
        if not p.is_file():
            raise FileNotFoundError(f"音频文件不存在: {p}")
        pcm, sr = ttsmod.read_wav(p)
        res = self.play_pcm(pcm, sr, block=block)
        return {"ok": True, "file": str(p), "audio_seconds": round(len(pcm) / sr, 2),
                "played": res}

    def test_tone(self, freq: float = 660.0, seconds: float = 1.5) -> dict:
        pcm = audio_io.make_tone(freq, max(0.3, min(10.0, seconds)), 24000)
        res = self.play_pcm(pcm, 24000)
        BUS.publish("audio", action="tone", freq=freq, seconds=seconds)
        return {"ok": True, "freq": freq, "seconds": seconds, "played": res}

    # ------------------------------------------------------------ 录音/转写
    def _recording_args(self) -> dict:
        cfg = cfgmod.load().audio
        return dict(mode=cfg.input_mode, device=cfg.input_device,
                    samplerate=cfg.in_sample_rate, channels=cfg.in_channels,
                    loopback_device=cfg.loopback_device)

    def listen(self, seconds: float | None = None, mode: str | None = None,
               device: str | int | None = None, language: str | None = None,
               prompt: str | None = None) -> dict:
        cfg = cfgmod.load()
        seconds = float(seconds or 5.0)
        seconds = max(0.5, min(seconds, float(cfg.audio.max_listen_seconds)))
        args = self._recording_args()
        if mode:
            args["mode"] = mode
        if device:
            args["device"] = device
        BUS.publish("audio", action="listen_start", seconds=seconds, mode=args["mode"])

        pcm = audio_io.record_seconds(
            seconds, stop_event=None,
            mode=args["mode"], device=args["device"],
            samplerate=args["samplerate"], channels=args["channels"],
            loopback_device=args["loopback_device"],
        )
        level = audio_io.rms(pcm)
        t0 = time.time()
        try:
            tr = sttmod.transcribe(pcm, args["samplerate"], language=language,
                                   initial_prompt=prompt)
        except Exception as exc:  # noqa: BLE001
            out = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "audio_seconds": round(pcm.size / args["samplerate"], 2),
                "rms": round(level, 4),
                "hint": "" if sttmod.available() else sttmod.install_hint(),
                "elapsed_ms": int((time.time() - t0) * 1000),
            }
            BUS.publish("audio", action="listen_error", error=out["error"])
            with self._lock:
                self._last_listen = out
            return out
        out = {
            "ok": True,
            "text": tr.text,
            "language": tr.language,
            "audio_seconds": round(pcm.size / args["samplerate"], 2),
            "rms": round(level, 4),
            "silent": level < cfg.audio.silence_rms,
            "segments": tr.segments,
            "rtf": round(tr.rtf, 3),
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
        with self._lock:
            self._listen_count += 1
            self._last_listen = out
        BUS.publish("audio", action="listen_done", text=tr.text[:80],
                    rms=out["rms"], audio_seconds=out["audio_seconds"])
        return out

    def start_stream_listen(self, language: str | None = None,
                            window: float | None = None) -> dict:
        cfg = cfgmod.load()
        if not sttmod.available():
            return {"ok": False, "error": sttmod.install_hint()}
        with self._lock:
            if self._recorder and self._recorder.running:
                return {"ok": True, "already": True, **self.stream_listen_status()}
            args = self._recording_args()
            rec = audio_io.StreamRecorder(
                mode=args["mode"], device=args["device"],
                samplerate=args["samplerate"], channels=args["channels"],
                loopback_device=args["loopback_device"],
                max_seconds=max(60.0, float(cfg.audio.max_listen_seconds) * 3),
            )
            self._recorder = rec

            def on_segment(ev: dict) -> None:
                BUS.publish("transcript", **ev)

            self._transcriber = sttmod.StreamTranscriber(
                rec, on_segment, window=window, language=language,
            )
            rec.start()
            self._transcriber.start()
        # 预热模型，避免第一句等 20 秒
        threading.Thread(target=self._warm_model, daemon=True).start()
        BUS.publish("audio", action="stream_listen_start", mode=args["mode"])
        return {"ok": True, **self.stream_listen_status()}

    @staticmethod
    def _warm_model() -> None:
        try:
            sttmod.ENGINE.load()
        except Exception:
            pass

    def stop_stream_listen(self) -> dict:
        with self._lock:
            tr, rec = self._transcriber, self._recorder
            self._transcriber = None
            self._recorder = None
        if tr:
            tr.stop()
        if rec:
            rec.stop()
        BUS.publish("audio", action="stream_listen_stop")
        return {"ok": True}

    def stream_listen_status(self) -> dict:
        with self._lock:
            tr, rec = self._transcriber, self._recorder
        segs = list(tr.segments) if tr else []
        return {
            "running": bool(tr and tr.running),
            "level": rec.level if rec else 0.0,
            "buffered_seconds": rec.seconds_buffered if rec else 0.0,
            "segment_count": len(segs),
            "segments": segs[-20:],
            "mode": rec.mode if rec else None,
            "device": (rec.device if rec else None),
            "model_loaded": sttmod.ENGINE.loaded,
            "error": (tr.error if tr else None) or (rec.error if rec else None),
        }

    # ------------------------------------------------------------ 汇总
    def status(self) -> dict:
        cfg = cfgmod.load()
        with self._lock:
            last_speak = self._last_speak
            last_listen = self._last_listen
            speaks = self._speak_count
            listens = self._listen_count
        try:
            devices = audio_io.describe_devices()
        except Exception as exc:  # noqa: BLE001
            devices = {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "server": {"port": cfg.server.port, "host": cfg.server.host,
                       "version": "1.0.0", "ts": time.time()},
            "audio": {
                "output": self.output_target(),
                "speaking": bool(self._player and self._player.busy),
                "input_mode": cfg.audio.input_mode,
                "input_device": cfg.audio.input_device,
                "loopback_device": cfg.audio.loopback_device or "(系统默认播放设备)",
                "virtual_cable": devices.get("virtual_cable", {}),
                "speaks": speaks,
                "listens": listens,
                "last_speak": last_speak,
                "last_listen": last_listen,
            },
            "stream_listen": self.stream_listen_status(),
            "tts": {"engine": cfg.tts.engine, "voice": cfg.tts.voice,
                    "edge_available": ttsmod.edge_available(),
                    "sapi_available": ttsmod.sapi_available()},
            "stt": sttmod.ENGINE.status(),
            "video": self.cam.status(),
        }
