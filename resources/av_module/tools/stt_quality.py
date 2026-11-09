"""用 small 模型验证转写质量（首次会从镜像下载 ~480MB）。

用法: python tools/stt_quality.py [模型名]
"""
import os
import sys
import time

MODEL = sys.argv[1] if len(sys.argv) > 1 else "small"
os.environ["AV_STT_MODEL"] = MODEL
os.environ.setdefault("AV_HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from av_core import stt, tts  # noqa: E402

CASES = [
    ("今天下午三点我们在公司三楼会议室开个短会，讨论一下下个版本的发布计划。",
     ["三点", "会议室", "发布"]),
    ("帮我把这条消息转发给张三，顺便问问他周末有没有空一起吃饭。",
     ["转发", "张三", "周末"]),
    ("这个虚拟麦克风的声音很清楚，对方应该能听出来是合成语音吗？",
     ["虚拟麦克风", "清楚", "合成"]),
]

print(f"模型: {MODEL}  镜像: {stt.ENGINE.status()['hf_endpoint']}")
t0 = time.time()
try:
    stt.ENGINE.load()
except Exception as exc:
    print("❌ 加载失败:", exc)
    raise SystemExit(1)
print(f"模型加载完成 {time.time()-t0:.1f}s ({stt.ENGINE.status()['model_path']})")

total_hit, total_kw, total_cer_chars, total_err = 0, 0, 0, 0
for text, kws in CASES:
    res = tts.synthesize(text, engine="edge")
    t1 = time.time()
    tr = stt.ENGINE.transcribe(res.pcm, res.samplerate)
    ms = int((time.time() - t1) * 1000)
    hits = [k for k in kws if k in tr.text]
    total_hit += len(hits)
    total_kw += len(kws)

    # 简易字错率（按字符集合比较，粗估）
    import difflib

    sm = difflib.SequenceMatcher(None, text, tr.text)
    err = sum(len(text[a:b]) for tag, a, b, _, _ in sm.get_opcodes() if tag in ("replace", "delete"))
    total_err += err
    total_cer_chars += len(text)

    print(f"\n原句: {text}")
    print(f"识别: {tr.text}")
    print(f"  {ms}ms  reff={ms/1000:.1f}s 实时  rtf={tr.rtf:.2f}  "
          f"关键词 {len(hits)}/{len(kws)} {hits}  字错率≈{err/max(1,len(text)):.1%}")

print(f"\n汇总: 关键词命中 {total_hit}/{total_kw}，平均字错率≈{total_err/max(1,total_cer_chars):.1%}")
