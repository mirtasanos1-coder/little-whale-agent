"""STT：faster-whisper（CTranslate2）懒加载版。

体积控制策略
------------
* faster-whisper 是本项目最重的依赖（ctranslate2 + tokenizers + onnxruntime），
  因此**不做模块级 import**：没装也能启动服务、能发声、能推流，只是听写接口返回
  明确的安装指引。
* 模型默认 small/int8（约 480MB 内存、中文可用）；想更快用 base，想更准用 medium。
* 模型文件落在 runtime/models，方便跟主程序一起打包/离线分发。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import config as cfgmod
from .audio_io import rms, resample

_WHISPER_SR = 16000

_onnx_cache: bool | None = None


def onnx_available() -> bool:
    """faster-whisper 的内置 VAD 过滤要用 onnxruntime。

    打包版（av_server.exe）为了体积把 onnxruntime 排除了，所以这里必须能优雅降级：
    VAD 只是"更省算力的静音剔除"，没有它照样能转写，只是静音段可能出幻觉，
    由 _is_prompt_echo / 最短长度过滤兜底。
    """
    global _onnx_cache
    if _onnx_cache is None:
        try:
            import onnxruntime  # noqa: F401
            _onnx_cache = True
        except Exception:
            _onnx_cache = False
    return _onnx_cache


@dataclass
class Transcript:
    text: str
    language: str = ""
    seconds: float = 0.0
    segments: list = field(default_factory=list)
    engine: str = "faster-whisper"
    model: str = ""
    rtf: float = 0.0            # 实时率：耗时 / 音频时长
    cached_model: bool = True

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "language": self.language,
            "audio_seconds": round(self.seconds, 2),
            "rtf": round(self.rtf, 3),
            "segments": self.segments,
            "engine": self.engine,
            "model": self.model,
        }


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def install_hint() -> str:
    return (
        "未安装 faster-whisper。执行: pip install faster-whisper "
        "（首次运行会自动下载模型到 runtime/models，small 约 480MB）"
    )


# ==================== v1.6.3：快速失败预检（QA P1-2） ====================
# QA实测：默认配置下 POST /api/av/listen卡113秒再抛
# NoSuchFile: .../faster_whisper/assets/silero_vad_v6.onnx
#两个原因叠在一起：① faster_whisper的 assets没进包（VAD加载不到模型）
#②首次运行才去下480MB模型（边下边等）。
#现在的规矩：这两件事都先在本机文件系统上判掉（毫秒级），
#缺 VAD →自动关掉静音检测继续转写；缺模型 →立刻回明确提示并在后台下。
_VAD_ASSET_NAME = "silero_vad_v6.onnx"
_VAD_ASSET_CACHE = {"checked": False, "path": None}
_MODEL_LOCAL = {"checked": False, "ok": False, "key": None, "dir": ""}
_WARM = {"started": False, "err": ""}


def vad_asset_path():
    """faster_whisper自带的 silero VAD模型路径；找不到返回 None。"""
    if _VAD_ASSET_CACHE["checked"]:
        return _VAD_ASSET_CACHE["path"]
    _VAD_ASSET_CACHE["checked"] = True
    p = None
    try:
        import os.path as _op
        import faster_whisper as _fw
        base = _op.dirname(getattr(_fw, "__file__", "") or "")
        cand = _op.join(base, "assets", _VAD_ASSET_NAME)
        if base and _op.isfile(cand):
            p = cand
    except Exception:
        p = None
    _VAD_ASSET_CACHE["path"] = p
    return p


def vad_ready() -> bool:
    """VAD能不能用：既要 onnxruntime，也要那个 .onnx真在包里。"""
    return bool(onnx_available() and vad_asset_path())


def vad_missing_reason() -> str:
    """VAD不可用的原因（给用户看的一句话）；能用则返回空串。"""
    if not onnx_available():
        return "缺少语音检测依赖 onnxruntime"
    if not vad_asset_path():
        return "缺少语音检测模型（%s）" % _VAD_ASSET_NAME
    return ""


def model_local_ready(cfg=None) -> bool:
    """模型文件是不是已经在本地（不用联网也能加载）。"""
    cfg = cfg or cfgmod.load().stt
    key = (str(cfg.model or ""), str(cfg.download_root or ""))
    if _MODEL_LOCAL["checked"] and _MODEL_LOCAL["key"] == key:
        return bool(_MODEL_LOCAL["ok"])
    ok, root = False, ""
    try:
        import os.path as _op
        import glob as _g
        root = str(cfg.download_root or "") or str(cfgmod.RUNTIME_DIR / "models")
        local = _op.expanduser(str(cfg.model or ""))
        if _op.isfile(_op.join(local, "model.bin")):
            ok = True
        elif _op.isdir(root):
            ok = bool(_g.glob(_op.join(root, "models--*", "snapshots", "*", "model.bin")))
    except Exception:
        ok = False
    _MODEL_LOCAL.update({"checked": True, "ok": bool(ok), "key": key, "dir": root})
    return bool(ok)


def warmup_start() -> dict:
    """后台把模型加载好（必要时下载）；接口侧**不等**它。"""
    if ENGINE.loaded:
        return {"ok": True, "msg": "模型已就绪", "started": False}
    if _WARM["started"] and ENGINE.loading:
        return {"ok": True, "msg": "正在后台准备模型", "started": False}
    
    def _work():
        try:
            ENGINE.load()
        except Exception as exc: # noqa: BLE001
            _WARM["err"] = "%s: %s" % (type(exc).__name__, exc)
            try:
                print("[stt]后台加载模型失败：%s" % _WARM["err"])
            except Exception:
                pass
    try:
        import threading as _th
        _WARM["started"] = True
        _th.Thread(target=_work, name="av-stt-warmup", daemon=True).start()
    except Exception as exc: # noqa: BLE001
        return {"ok": False, "msg": "起不来预热线程：%s" % exc, "started": False}
    return {"ok": True, "msg": "已在后台开始准备模型", "started": True}


def preflight(need_vad: bool = False):
    """毫秒级自检：当前能不能转写？返回 (ok,错误说明,是否强制关 VAD)。

    三条路，全部只看本地文件：
    ① faster_whisper没装 → ok=False（给安装指引）
    ②模型没下到本地 → ok=False，顺手在后台开始下，**不等**
    ③模型有、VAD缺 → ok=True但要关掉 vad_filter（照样能转写）
    """
    if not available():
        return False, install_hint(), True
    if not ENGINE.loaded and not model_local_ready():
        warmup_start()
        return False, ("语音模型还没下载好（约480MB），已在后台开始准备，"
            "稍等一会儿再说一遍就行。"), True
    if need_vad and not vad_ready():
        return True, vad_missing_reason(), True
    return True, "", False


class WhisperEngine:
    """全局单例：模型只加载一次，多请求排队推理。"""

    def __init__(self) -> None:
        self._model = None
        self._key: tuple | None = None
        self._lock = threading.RLock()
        self._infer_lock = threading.Lock()
        self._loading = False
        self._error: str | None = None
        self._vad_warned = False # v1.6.3：缺 VAD只提示一次
        self._model_path: str | None = None
        self._endpoint: str | None = None

    # -- 状态 --
    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def loading(self) -> bool:
        return self._loading

    @property
    def error(self) -> str | None:
        return self._error

    def status(self) -> dict:
        cfg = cfgmod.load().stt
        return {
            "available": available(),
            "loaded": self.loaded,
            "loading": self._loading,
            "model": cfg.model,
            "device": cfg.device,
            "compute_type": cfg.compute_type,
            "language": cfg.language,
            "model_path": self._model_path,
            "hf_endpoint": self._endpoint or cfg.hf_endpoint,
            "vad": vad_ready(),
            "vad_asset": vad_asset_path(),
            "vad_reason": vad_missing_reason(),
            "model_local": model_local_ready(),
            "error": self._error,
            "hint": "" if available() else install_hint(),
        }

    # -- 加载 --
    def _download_root(self) -> str:
        root = cfgmod.load().stt.download_root
        if root:
            p = Path(root)
        else:
            p = cfgmod.RUNTIME_DIR / "models"
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def load(self) -> None:
        cfg = cfgmod.load().stt
        key = (cfg.model, cfg.device, cfg.compute_type, cfg.hf_endpoint)
        with self._lock:
            if self._model is not None and self._key == key:
                return
            if not available():
                self._error = install_hint()
                raise RuntimeError(self._error)
            self._loading = True
            self._error = None
            try:
                from faster_whisper import WhisperModel

                root = self._download_root()
                endpoint = self._apply_hf_endpoint(cfg.hf_endpoint)
                self._endpoint = endpoint
                try:
                    model = WhisperModel(
                        cfg.model, device=cfg.device, compute_type=cfg.compute_type,
                        download_root=root,
                    )
                except Exception as first_exc:  # noqa: BLE001
                    # 镜像不通时再退回官方源试一次（有梯子/海外机器的情况）
                    fallback = "https://huggingface.co"
                    if endpoint.rstrip("/") == fallback:
                        raise
                    self._apply_hf_endpoint(fallback)
                    self._endpoint = fallback
                    try:
                        model = WhisperModel(
                            cfg.model, device=cfg.device, compute_type=cfg.compute_type,
                            download_root=root,
                        )
                    except Exception as second_exc:  # noqa: BLE001
                        raise RuntimeError(
                            f"模型下载失败（镜像 {endpoint} 与官方源均不可用）。"
                            f"可手动下载 {cfg.model} 放到 {root}，"
                            f"或用 AV_HF_ENDPOINT 指定可用镜像。"
                            f" 镜像错误: {type(first_exc).__name__}: {first_exc}"
                            f" | 官方源错误: {type(second_exc).__name__}: {second_exc}"
                        ) from second_exc
                self._model = model
                self._key = key
                self._model_path = root
            except Exception as exc:  # noqa: BLE001
                self._error = str(exc) if isinstance(exc, RuntimeError) else \
                    f"加载模型失败: {type(exc).__name__}: {exc}"
                raise
            finally:
                self._loading = False

    @staticmethod
    def _apply_hf_endpoint(endpoint: str | None) -> str:
        """设置 HuggingFace 下载源。

        huggingface_hub 在 import 时把 ENDPOINT 固化进常量，所以除了写环境变量，
        还要热改一下已加载模块里的常量，否则镜像设置不生效。
        """
        target = (endpoint or "").strip() or os.environ.get("HF_ENDPOINT", "").strip() \
            or "https://hf-mirror.com"
        os.environ["HF_ENDPOINT"] = target
        # 新版 huggingface_hub 会用 Xet 存储（cas-server.xethub.hf.co）传大文件，
        # 镜像站点没有对应的鉴权令牌，会直接 401 —— 关掉 Xet 走普通 HTTP 下载。
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        try:
            import huggingface_hub.constants as hfconst

            hfconst.ENDPOINT = target
        except Exception:  # noqa: BLE001
            pass
        return target

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._key = None

    # -- 推理 --
    def transcribe(self, pcm: np.ndarray, samplerate: int,
                   language: str | None = None, initial_prompt: str | None = None,
                   beam_size: int | None = None,
                   vad_filter: bool | None = None) -> Transcript:
        cfg = cfgmod.load().stt
        # v1.6.3（QA P1-2）：先毫秒级自检，缺资产立刻退，绝不再卡113秒
        _want_vad = cfg.vad_filter if vad_filter is None else bool(vad_filter)
        _ok, _why, _no_vad = preflight(need_vad=_want_vad)
        if not _ok:
            self._error = _why
            raise RuntimeError(_why)
        if _no_vad:
            vad_filter = False
            if _why and not self._vad_warned:
                self._vad_warned = True
                self._error = _why + "（已自动关闭静音检测，转写仍可用）"
        self.load()
        audio = pcm.astype(np.float32, copy=False)
        if samplerate != _WHISPER_SR:
            audio = resample(audio, samplerate, _WHISPER_SR)
        if audio.size == 0:
            return Transcript(text="", language=cfg.language, model=cfg.model)
        audio = self._auto_gain(audio)
        seconds = audio.size / _WHISPER_SR

        want_vad = cfg.vad_filter if vad_filter is None else bool(vad_filter)
        if want_vad and not vad_ready():
            want_vad = False            # 打包版没带 onnxruntime → 纯转写，不会报错
        kwargs = dict(
            language=language if language is not None else cfg.language,
            beam_size=beam_size if beam_size is not None else cfg.beam_size,
            vad_filter=want_vad,
            condition_on_previous_text=False,
        )
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt
        elif str(kwargs["language"] or "").lower().startswith("zh"):
            kwargs["initial_prompt"] = ZH_PROMPT
        if kwargs["vad_filter"]:
            kwargs["vad_parameters"] = dict(min_silence_duration_ms=400,
                                            speech_pad_ms=120)

        t0 = time.time()
        with self._infer_lock:
            segments, info = self._model.transcribe(audio, **kwargs)  # type: ignore[union-attr]
            seg_list = []
            texts = []
            for seg in segments:
                seg_list.append({
                    "start": round(float(seg.start), 2),
                    "end": round(float(seg.end), 2),
                    "text": seg.text.strip(),
                    "no_speech_prob": round(float(getattr(seg, "no_speech_prob", 0.0)), 3),
                })
                if seg.text.strip():
                    texts.append(seg.text.strip())
        elapsed = time.time() - t0
        return Transcript(
            text="".join(texts).strip(),
            language=getattr(info, "language", cfg.language) or cfg.language,
            seconds=seconds,
            segments=seg_list,
            model=cfg.model,
            rtf=elapsed / max(seconds, 1e-6),
        )

    @staticmethod
    def _auto_gain(audio: np.ndarray, target: float = 0.5, max_gain: float = 120.0) -> np.ndarray:
        """录音电平太低时抬一下音量（很多笔记本/通话软件电平峰值只有 0.02），
        whisper 对轻声更敏感、更准。只放大不衰减，且静音不放大，避免吼出底噪。

        实测本机 WASAPI 回环峰值只有 0.006~0.02（系统音量偏低），
        所以 max_gain 给到 120（≈ +41dB），否则 small 模型几乎全是空结果。
        """
        if audio.size == 0:
            return audio
        peak = float(np.max(np.abs(audio)))
        if peak < 1e-4 or peak >= 0.1:
            return audio
        return np.clip(audio * min(max_gain, target / peak), -1.0, 1.0)


ENGINE = WhisperEngine()


def transcribe(pcm: np.ndarray, samplerate: int, **kwargs) -> Transcript:
    return ENGINE.transcribe(pcm, samplerate, **kwargs)


# ---------------------------------------------------------------- 流式听写

#: 给 whisper 的中文提示：不给提示时它经常吐繁体、也不加标点。
#: 注意：窗口里几乎全是静音时，模型会**复述这句提示**，所以下面有专门的过滤。
ZH_PROMPT = "以下是简体中文的普通话内容。"

_PUNCT = "，。！？、；：“”‘’（）【】…—,.!?;:\"'()<>《》 \n\r\t"


def _norm_text(s: str) -> str:
    return "".join(ch for ch in s if ch not in _PUNCT).lower()


def _is_prompt_echo(text: str) -> bool:
    """这段文字是不是 initial_prompt 被复述（静音窗口的典型幻觉）。"""
    t, p = _norm_text(text), _norm_text(ZH_PROMPT)
    if not t or not p:
        return False
    return t in p or p in t


_SENTENCE_END = "。！？!?…；;.~～”’）)"


def _is_sentence_end(text: str) -> bool:
    s = (text or "").strip()
    return bool(s) and s[-1] in _SENTENCE_END


def _trim_overlap(prev: str, cur: str) -> str:
    """去掉 cur 开头与 prev 结尾重复的部分（0.5s 重叠窗口的副产物）。"""
    a, b = _norm_text(prev), _norm_text(cur)
    if not a or not b:
        return cur
    if b == a:
        return ""
    best = 0
    for k in range(min(len(a), len(b)), 0, -1):
        if a.endswith(b[:k]):
            best = k
            break
    if not best:
        return cur
    removed, cut = 0, 0
    for i, ch in enumerate(cur):
        if ch not in _PUNCT:
            removed += 1
        if removed == best:
            cut = i + 1
            break
    else:
        return ""
    return cur[cut:].lstrip(_PUNCT).strip()


def _find_cut(audio: np.ndarray, sr: int, min_rms: float,
              min_len: float = 1.8, min_silence: float = 0.25,
              max_len: float = 4.0) -> int | None:
    """在音频里找"合适切句点"的下标，找不到返回 None。

    规则：以 30ms 帧算 RMS，找长度 ≥ min_silence（默认 0.25s）的连续静音段，
    切在它之后；有声长度不足 min_len 不切（攒着），超过 max_len 强制切。
    默认 max_len=4.0 即"对讲机模式"：再长的连续说话也每 4 秒出一次结果。
    """
    if audio.size == 0:
        return None
    total = audio.size / float(sr)
    if total < min_len:
        return None
    frame = max(1, int(0.03 * sr))
    frames = int(audio.size // frame)
    if frames < 2:
        return None
    block = audio[: frames * frame].astype(np.float32).reshape(frames, frame)
    levels = np.sqrt(np.mean(block * block, axis=1))
    silent = levels < max(min_rms, 1e-6)
    need = max(1, int(min_silence / 0.03))
    run = 0
    for i in range(frames):
        if bool(silent[i]):
            run += 1
            continue
        if run >= need and (i * frame) / float(sr) >= min_len:
            return i * frame           # 静音结束、下一句开始处
        run = 0
    if run >= need and (frames * frame) / float(sr) >= min_len:
        return frames * frame          # 缓冲末尾就是静音
    if total >= max_len:
        # 一直没静音 → 强制切。别硬切在 max_len 上（容易切在字中间，
        # 后半句开头还会被模型脑补出"在这一步""在这一会"之类的填充词），
        # 改成在最后 1.2s 里挑能量最低的那一帧（音节缝隙）下刀。
        lo = max(0, int((max_len - 1.2) * sr))
        hi = min(len(audio), int(max_len * sr))
        if hi - lo > int(0.2 * sr):
            seg = audio[lo:hi]
            n = len(seg) // frame
            if n >= 2:
                blocks = seg[: n * frame].reshape(n, frame)
                eng = np.sqrt(np.mean(blocks * blocks, axis=1))
                k = int(np.argmin(eng))
                # 只有"明显比周围安静"的缝隙才值得下刀（能量低于均值 60%），
                # 否则宁可硬切在 max_len——随便挑个低点会切在字的中间。
                if float(eng[k]) <= 0.6 * float(np.mean(eng)) + 1e-9:
                    return lo + k * frame
        return int(max_len * sr)
    return None


class StreamTranscriber:
    """从 StreamRecorder 持续取音频 -> 按静音分句 -> 转写 -> 回调。

    分句策略（对讲机式）：优先切在 ≥0.25s 的静音处；不足 min_len 继续攒；
    连续说话每 `window`（默认 4s）强制切一刀，保证 4s 内必出一次结果。
    """

    def __init__(self, recorder, on_segment: Callable[[dict], None],
                 window: float | None = None, language: str | None = None,
                 min_rms: float | None = None, min_silence: float | None = None):
        cfg = cfgmod.load()
        self.recorder = recorder
        self.on_segment = on_segment
        self.window = float(window or cfg.stt.stream_window_s)
        self.language = language
        # 静音判定的两个阈值都可在 config.json 里改：
        #   audio.silence_rms     低于它算静音（回环电平很低，默认 0.0010）
        #   audio.silence_hang_ms 静音持续多久算"一句话说完"（默认 250ms）
        self.min_rms = float(min_rms if min_rms is not None else cfg.audio.silence_rms)
        self.min_silence = float(
            min_silence if min_silence is not None
            else max(0.10, cfg.audio.silence_hang_ms / 1000.0)
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._carry = np.zeros(0, dtype=np.float32)
        self._last_text = ""
        self._pending = ""
        self.segments: list[dict] = []
        self.error: str | None = None
        self.processed_seconds = 0.0

    def _run(self) -> None:
        sr = self.recorder.samplerate
        # 对讲机模式：无论有没有停顿，每 window 秒必须切一刀出结果
        max_len = max(2.0, self.window)
        try:
            while not self._stop.is_set():
                time.sleep(0.2)
                new = self.recorder.drain()
                if new.size:
                    self._carry = np.concatenate([self._carry, new])
                # 在"静音处"切句，而不是按固定时长硬切：硬切会把一句话劈两半，
                # 出现「帮我把这条消息转。」+「将写给张三」这种碎片。
                cut = _find_cut(self._carry, sr, self.min_rms,
                                min_len=max(1.0, min(2.0, self.window * 0.5)),
                                min_silence=self.min_silence,
                                max_len=max_len)
                if cut is None:
                    continue
                chunk = self._carry[:cut]
                self._carry = self._carry[cut:]
                self.processed_seconds += chunk.size / sr
                if rms(chunk) < self.min_rms:
                    continue
                try:
                    tr = ENGINE.transcribe(chunk, sr, language=self.language)
                except Exception as exc:  # noqa: BLE001
                    self.error = f"{type(exc).__name__}: {exc}"
                    time.sleep(1.0)
                    continue
                text = tr.text.strip()
                if not text:
                    continue
                # whisper 在"窗口里几乎全是静音"时会复述 initial_prompt，必须过滤掉
                if _is_prompt_echo(text):
                    continue
                text = _trim_overlap(self._last_text, text)
                if len(_norm_text(text)) < 2:
                    continue
                self._last_text = text
                event = {
                    "text": text,
                    "language": tr.language,
                    "seconds": round(chunk.size / sr, 2),
                    "ts": time.time(),
                }
                self.segments.append(event)
                try:
                    self.on_segment(event)
                except Exception:
                    pass
        finally:
            self._flush_pending()
            self._stop.set()

    def _flush_pending(self) -> None:
        """收尾时把攒着的半句也吐出来，避免最后一句丢掉。"""
        text, self._pending = self._pending, ""
        text = (text or "").strip()
        if not text:
            return
        event = {"text": text, "language": self.language, "seconds": 0.0,
                 "ts": time.time(), "final": True}
        self.segments.append(event)
        try:
            self.on_segment(event)
        except Exception:
            pass

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="av-stt-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())
