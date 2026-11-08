"""STT 端到端自检：TTS 生成语音 -> 系统回环录制 -> faster-whisper 转写。

默认用 tiny 模型（体积小、下载快），验证链路通了再换 small。
用法:
    python tools/stt_probe.py            # 离线合成 + 回环录制 + 转写
    python tools/stt_probe.py --direct   # 直接把 TTS PCM 送进 whisper（不经过扬声器）
"""
import os
import sys
import time

os.environ.setdefault("AV_STT_MODEL", "tiny")
os.environ.setdefault("AV_HF_ENDPOINT", "https://hf-mirror.com")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from av_core import audio_io, stt, tts  # noqa: E402

TEXT = "你好，我是人工智能助手，现在正在测试语音识别链路。"
direct = "--direct" in sys.argv

print(f"[1/4] 合成语音（edge-tts）: {TEXT}")
t0 = time.time()
res = tts.synthesize(TEXT, engine="edge")
print(f"      {res.seconds:.2f}s 音频, {len(res.pcm)} 采样, {res.samplerate}Hz, "
      f"合成 {int((time.time()-t0)*1000)}ms, engine={res.engine}")

if direct:
    print("[2/4] 跳过回环，直接转写")
    pcm, sr = res.pcm, res.samplerate
else:
    print("[2/4] 打开系统回环录音，同时播放到默认扬声器")
    rec = audio_io.StreamRecorder(mode="loopback", samplerate=48000, channels=2,
                                  max_seconds=30)
    rec.start()
    time.sleep(0.4)
    player = audio_io.Player()
    player.play(res.pcm, res.samplerate)
    time.sleep(0.4)
    pcm_raw = rec.read_all()
    rec.stop()
    print(f"      录到 {len(pcm_raw)} 采样 ({len(pcm_raw)/48000:.2f}s), 峰值 {np.max(np.abs(pcm_raw)):.3f}")
    pcm, sr = pcm_raw, 48000

print(f"[3/4] faster-whisper 转写（模型 {stt.ENGINE.status()['model']}，镜像 "
      f"{stt.ENGINE.status()['hf_endpoint']}）")
t0 = time.time()
try:
    tr = stt.ENGINE.transcribe(pcm, sr)
except Exception as exc:
    print("      ❌ 失败:", type(exc).__name__, exc)
    print("      hint:", stt.install_hint())
    raise SystemExit(1)

print(f"[4/4] 耗时 {int((time.time()-t0)*1000)}ms  rtf={tr.rtf}  lang={tr.language}")
print("      识别结果:", repr(tr.text))
for seg in tr.segments[:4]:
    print(f"        [{seg.get('start'):.2f}-{seg.get('end'):.2f}] {seg.get('text')}")

hit = sum(1 for ch in ("测试", "语音", "助手", "识别") if ch in tr.text)
print(f"      关键词命中 {hit}/4 -> {'✅ 链路可用' if hit >= 2 else '⚠ 识别结果偏差较大'}")
