#!/usr/bin/env python
"""模拟音视频通话微服务（独立端口 5800）。

    python av_server.py                # 默认 127.0.0.1:5800
    python av_server.py --rpa          # 同时启动 QQ/微信 自动接听
    python av_server.py --port 5800 --open

设计约束
--------
* 不 import 主控台任何模块；只通过 HTTP(JSON) 交互
* 重依赖（faster-whisper / pyvirtualcam / uiautomation）全部懒加载，
  缺哪个只影响对应接口，服务始终能起来
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent))

from av_core import config as cfgmod          # noqa: E402
from av_core import drivers as drvmod         # noqa: E402
from av_core import rpa as rpamod             # noqa: E402
from av_core import stt as sttmod             # noqa: E402
from av_core import tts as ttsmod             # noqa: E402
from av_core.events import BUS                # noqa: E402
from av_core.pipeline import AVPipeline       # noqa: E402

LOG = logging.getLogger("av")
VERSION = "1.0.0"

PIPE = AVPipeline()
RPA = rpamod.CallWatcher()


# ============================================================ 生命周期

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfgmod.ensure_dirs()
    cfg = cfgmod.load()
    LOG.info("音视频模块启动: http://%s:%s  (token=%s)",
             cfg.server.host, cfg.server.port, "on" if cfg.server.token else "off")
    BUS.publish("server", action="boot", port=cfg.server.port, version=VERSION)
    try:
        yield
    finally:
        LOG.info("正在关闭 ...")
        try:
            PIPE.stop_stream_listen()
        except Exception:
            pass
        try:
            PIPE.stop_speaking()
        except Exception:
            pass
        try:
            PIPE.cam.stop()
        except Exception:
            pass
        try:
            RPA.stop()
        except Exception:
            pass
        BUS.publish("server", action="shutdown")


app = FastAPI(
    title="模拟音视频通话模块",
    description="虚拟声卡 + 虚拟摄像头 + RPA，让 AI 在 QQ/微信里通话",
    version=VERSION,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================ 鉴权 / 错误

@app.middleware("http")
async def token_guard(request: Request, call_next):
    cfg = cfgmod.load()
    path = request.url.path
    if cfg.server.token and path.startswith("/api/av"):
        supplied = (request.headers.get("x-av-token")
                    or request.query_params.get("token") or "")
        if supplied != cfg.server.token:
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["x-av-elapsed-ms"] = f"{(time.perf_counter() - start) * 1000:.0f}"
    response.headers["x-av-version"] = VERSION
    return response


def _fail(exc: Exception, hint: str = "") -> JSONResponse:
    LOG.exception("接口异常: %s", exc)
    return JSONResponse(
        {"ok": False, "error": f"{type(exc).__name__}: {exc}", "hint": hint},
        status_code=500,
    )


# ============================================================ 基础

@app.get("/", include_in_schema=False)
async def index():
    page = cfgmod.STATIC_DIR / "index.html"
    if not page.is_file():
        return JSONResponse({"ok": True, "service": "av-module", "version": VERSION,
                             "docs": "/docs", "ui": "static/index.html 缺失"})
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/api/av/health")
async def health():
    return {"ok": True, "service": "av-module", "version": VERSION, "ts": time.time()}


@app.get("/api/av/status")
async def status():
    return await asyncio.to_thread(PIPE.status)


@app.get("/api/av/config")
async def get_config():
    return {"ok": True, "config": cfgmod.load().to_dict()}


@app.post("/api/av/config")
async def patch_config(payload: dict = Body(...)):
    """部分更新配置。接受三种写法：
       {"audio": {"out_gain": 0.8}}            # 推荐：段名 → 字段
       {"config": {"audio": {...}}}            # 兼容包装
       {"section": "audio", "values": {...}}   # 扁平辅助形式
    """
    cfg = cfgmod.load()
    body: Any = payload.get("config", payload)
    section = payload.get("section")
    values = payload.get("values")
    if isinstance(section, str) and isinstance(values, dict):
        body = {section: values}
    elif isinstance(section, dict) and payload.get("values") is None:
        body = section
    try:
        changed = cfg.patch(body)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, f"配置无效: {exc}")
    cfgmod.save(cfg)
    BUS.publish("config", action="patch", changed=changed)
    return {"ok": True, "changed": changed, "config": cfg.to_dict()}


# ============================================================ 设备

@app.get("/api/av/devices")
async def devices():
    from av_core import audio_io

    try:
        info = await asyncio.to_thread(audio_io.describe_devices)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, "声卡枚举失败：确认已安装 sounddevice/soundcard 且有可用音频设备")
    return {"ok": True, **info}


@app.get("/api/av/voices")
async def voices(locale: str = "zh"):
    data = await asyncio.to_thread(ttsmod.list_edge_voices, locale)
    return {"ok": True, "voices": data, "count": len(data)}


# ============================================================ 自定义音色（v1.8.0）

def _voice_dir():
    d = cfgmod.DATA_ROOT / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_ref_audio(b64: str, name: str) -> str:
    """把前端上传的参考音频存进数据目录，返回绝对路径。"""
    import base64
    import re
    raw = (b64 or "").strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    blob = base64.b64decode(raw)
    if not blob:
        raise ValueError("音频是空的")
    if len(blob) > 30 * 1024 * 1024:
        raise ValueError("音频太大（超过 30MB），截一段 5~15 秒的就够了")
    safe = re.sub(r"[^\w.\-]+", "_", os.path.basename(name or "ref.wav"), flags=re.UNICODE)[:60]
    if "." not in safe:
        safe = (safe or "ref") + ".wav"
    path = _voice_dir() / safe
    path.write_bytes(blob)
    return str(path)


@app.get("/api/av/tts/custom")
async def tts_custom_get():
    info = await asyncio.to_thread(ttsmod.custom_status)
    return {"ok": True, **info}


@app.post("/api/av/tts/custom")
async def tts_custom_set(payload: dict = Body(default={})):
    """改自定义音色配置；可选把参考音频一起传上来（base64）。"""
    cfg = cfgmod.load()
    f: dict = {}
    ref_b64 = str(payload.get("ref_audio_b64") or "")
    if ref_b64:
        try:
            f["custom_ref_audio"] = _save_ref_audio(ref_b64, str(payload.get("ref_audio_name") or "ref.wav"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, "参考音频保存失败：%s" % exc)
    if payload.get("clear_ref"):
        f["custom_ref_audio"] = ""
    _SIMPLE = {
        "enabled": ("custom_enabled", bool),
        "url": ("custom_url", str),
        "format": ("custom_format", str),
        "ref_text": ("custom_ref_text", str),
        "voice_id": ("custom_voice_id", str),
        "model": ("custom_model", str),
        "token": ("custom_token", str),
        "lang": ("custom_lang", str),
        "voice_name": ("custom_voice_name", str),
        "timeout": ("custom_timeout", float),
    }
    for k, (col, cast) in _SIMPLE.items():
        if k not in payload:
            continue
        v = payload.get(k)
        try:
            f[col] = cast(v)
        except Exception:  # noqa: BLE001
            continue
    fmt = str(f.get("custom_format") or cfg.tts.custom_format or "").lower()
    if fmt and fmt not in ttsmod.CUSTOM_FORMATS:
        raise HTTPException(400, "不支持的自定义音色格式：%s（可选 %s）"
                            % (fmt, " / ".join(ttsmod.CUSTOM_FORMATS)))
    if f:
        try:
            changed = cfg.patch({"tts": f})
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(400, "配置无效：%s" % exc)
        cfgmod.save(cfg)
        BUS.publish("config", action="patch", changed=changed)
    info = await asyncio.to_thread(ttsmod.custom_status)
    return {"ok": True, **info}


@app.post("/api/av/tts/custom/test")
async def tts_custom_test(payload: dict = Body(default={})):
    """试一句，让用户在接进打电话之前就听见效果。"""
    text = str(payload.get("text") or "").strip() or "这是一句试听，听听像不像我。"
    try:
        res = await asyncio.to_thread(
            PIPE.speak, text, None, None, None, None, "custom_voice",
            bool(payload.get("block", True)), None,
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, "自定义音色不可用：检查服务是否启动、地址填对没、参考音频还在不在")
    info = await asyncio.to_thread(ttsmod.custom_status)
    if res.get("engine") != "custom_voice":
        res["note"] = "自定义音色没成功，已经自动退回 %s，所以这次听到的不是克隆的声音。" % res.get("engine")
    res.update({"custom": info})
    return res


# ============================================================ 模块一：语音

@app.post("/api/av/speak")
async def speak(payload: dict = Body(...)):
    """文字 -> TTS -> 直接播进虚拟声卡（= 对方听见 AI 说话）。"""
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    try:
        result = await asyncio.to_thread(
            PIPE.speak,
            text,
            payload.get("voice"),
            payload.get("rate"),
            payload.get("volume"),
            payload.get("pitch"),
            payload.get("engine"),
            bool(payload.get("block", True)),
            payload.get("interrupt"),
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, "TTS 失败：检查网络（edge-tts 需联网）或改用 engine=sapi")
    return result


@app.post("/api/av/speak/stop")
async def speak_stop():
    return await asyncio.to_thread(PIPE.stop_speaking)


@app.post("/api/av/test_tone")
async def test_tone(payload: dict = Body(default={})):
    """不依赖 TTS/网络的发声自测：把“叮咚”送进虚拟声卡。"""
    try:
        return await asyncio.to_thread(
            PIPE.test_tone,
            float(payload.get("freq", 660.0)),
            float(payload.get("seconds", 1.5)),
        )
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, "播放失败：检查音频输出设备/独占模式")


@app.post("/api/av/listen")
async def listen(payload: dict = Body(default={})):
    """录一段（系统回环或指定设备）-> faster-whisper转文字。"""
    # v1.6.4（QA P1-2）：录音失败不再抛500。设备被独占/驱动不兼容是常见情况
    #（WDM-KS error -9999），属于可恢复，返回200 + ok=False让前端展示原因。
    from av_core import audio_io # noqa: PLC0415
    try:
        res = await asyncio.to_thread(
            PIPE.listen,
            payload.get("seconds",5.0),
            payload.get("mode"),
            payload.get("device"),
            payload.get("language"),
            payload.get("prompt"),
        )
        if isinstance(res, dict):
            try:
                res["note"] = audio_io.last_note()
            except Exception:
                pass
        return res
    except Exception as exc: # noqa: BLE001
        LOG.exception("录音接口异常: %s", exc)
        note = ""
        try:
            note = audio_io.last_note()
        except Exception:
            pass
        return {
            "ok": False,
            "error": type(exc).__name__ + ": " + str(exc),
            "note": note,
            "hint": "录音设备打不开。常见原因：" + "1)该设备被别的程序独占（QQ/微信/浏览器占着麦）；" + "2)设备是 WDM-KS/专业声卡驱动，不接受当前采样率或双声道；3)设备已拔出。" + "可以去「设置 →音视频」换一个录音设备，或改成录系统回环。",
        }


@app.post("/api/av/listen/start")
async def listen_start(payload: dict = Body(default={})):
    return await asyncio.to_thread(
        PIPE.start_stream_listen, payload.get("language"), payload.get("window")
    )


@app.post("/api/av/listen/stop")
async def listen_stop():
    return await asyncio.to_thread(PIPE.stop_stream_listen)


@app.get("/api/av/listen/status")
async def listen_status():
    return {"ok": True, **PIPE.stream_listen_status()}


# ============================================================ 模块二：视频

def _image_from_payload(payload: dict) -> Any:
    for key in ("path", "image", "url", "file", "base64", "data_uri"):
        if payload.get(key):
            value = payload[key]
            if key == "base64" and not str(value).startswith("data:"):
                return f"data:image/png;base64,{value}"
            return value
    return None


@app.post("/api/av/stream_image")
async def stream_image(payload: dict = Body(...)):
    """切换推流图片（本地路径 / http URL / base64 / data URI）。"""
    src = _image_from_payload(payload)
    if src is None:
        raise HTTPException(400, "需要 path / url / base64 之一")
    cam = PIPE.cam
    try:
        if payload.get("caption") is not None:
            cam.set_caption(str(payload["caption"])) if cam.running else None
        if cam.running:
            result = await asyncio.to_thread(cam.set_image, src)
        else:
            result = await asyncio.to_thread(
                cam.start, src, payload.get("effect")
            )
        if payload.get("caption") is not None:
            cam.set_caption(str(payload["caption"]))
        if payload.get("effect"):
            result = cam.status()
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except (OSError, ValueError) as exc:
        raise HTTPException(400, f"图片无法解析（{type(exc).__name__}: {exc}）")
    except Exception as exc:  # noqa: BLE001
        return _fail(exc, "推流失败：确认图片可读；视频链路需要 OBS Virtual Camera")
    return {"ok": True, "video": result}


@app.post("/api/av/stream/start")
async def stream_start(payload: dict = Body(default={})):
    src = _image_from_payload(payload)
    try:
        result = await asyncio.to_thread(PIPE.cam.start, src, payload.get("effect"))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except (OSError, ValueError) as exc:
        raise HTTPException(400, f"图片无法解析（{type(exc).__name__}: {exc}）")
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)
    return {"ok": True, "video": result}


@app.post("/api/av/stream/stop")
async def stream_stop():
    return {"ok": True, "video": await asyncio.to_thread(PIPE.cam.stop)}


@app.post("/api/av/stream/caption")
async def stream_caption(payload: dict = Body(default={})):
    return {"ok": True, "video": await asyncio.to_thread(
        PIPE.cam.set_caption, str(payload.get("text", "")))}


@app.get("/api/av/preview")
@app.get("/api/av/preview.jpg")
async def preview():
    data = await asyncio.to_thread(PIPE.cam.preview_jpeg)
    return Response(content=data, media_type="image/jpeg",
                    headers={"cache-control": "no-store"})


# ============================================================ 模块零：驱动

@app.post("/api/av/drivers/check")
async def drivers_check(payload: dict = Body(default={})):
    return await asyncio.to_thread(drvmod.status_all)


@app.post("/api/av/drivers/install")
async def drivers_install(payload: dict = Body(default={})):
    cfg = cfgmod.load()
    if not cfg.drivers.allow_remote_install:
        raise HTTPException(
            403,
            "远程安装驱动默认关闭（安全考虑）。请在终端执行: "
            "python install_drivers.py --yes   或设置 drivers.allow_remote_install=true",
        )
    key = payload.get("driver")
    keys = [key] if key else list(drvmod.SPECS.keys())
    results = {}
    for k in keys:
        spec = drvmod.SPECS.get(k)
        if not spec:
            raise HTTPException(400, f"未知驱动: {k}")
        results[k] = await asyncio.to_thread(
            drvmod.install_driver, spec, cfgmod.DRIVERS_DIR,
            cfg.drivers.install_timeout, bool(payload.get("force")), True,
        )
    return {"ok": True, "results": results}


# ============================================================ 模块三：RPA

@app.post("/api/av/rpa/start")
async def rpa_start(payload: dict = Body(default={})):
    if "dry_run" in payload:
        cfg = cfgmod.load()
        cfg.rpa.dry_run = bool(payload["dry_run"])
        cfgmod.save(cfg)
    result = await asyncio.to_thread(RPA.start)
    if not rpamod.available():
        return JSONResponse({"ok": False, "rpa": result}, status_code=503)
    return {"ok": True, "rpa": result}


@app.post("/api/av/rpa/stop")
async def rpa_stop():
    return {"ok": True, "rpa": await asyncio.to_thread(RPA.stop)}


@app.get("/api/av/rpa/status")
async def rpa_status():
    return {"ok": True, "rpa": RPA.status()}


@app.get("/api/av/rpa/scan")
async def rpa_scan(depth: int = 5, limit: int = 40):
    return await asyncio.to_thread(RPA.scan, depth, limit)


@app.get("/api/av/rpa/templates")
async def rpa_templates():
    """图像兜底用的按钮模板状态（目录、已放模板、阈值、屏幕尺寸）。"""
    return {"ok": True, **await asyncio.to_thread(RPA.templates)}


@app.post("/api/av/rpa/test_match")
async def rpa_test_match(payload: dict = Body(default={})):
    """只做模板匹配、不点击；用来验证模板图与坐标对不对。"""
    keyword = (payload or {}).get("keyword") or None
    res = await asyncio.to_thread(RPA.test_match, keyword)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@app.post("/api/av/rpa/snapshot")
async def rpa_snapshot(payload: dict = Body(default={})):
    """截图存到 rpa/templates/_snapshot.png，方便裁模板。"""
    keyword = (payload or {}).get("keyword") or None
    res = await asyncio.to_thread(RPA.snapshot, keyword)
    return JSONResponse(res, status_code=200 if res.get("ok") else 500)


@app.post("/api/av/rpa/hangup")
async def rpa_hangup(payload: dict = Body(default={})):
    """主动挂断（给主控台用）：UIA 找挂断按钮，找不到就走图像识别。"""
    res = await asyncio.to_thread(RPA.hangup_now, bool((payload or {}).get("force", False)))
    return JSONResponse(res, status_code=200 if res.get("ok") else 409)


@app.post("/api/av/rpa/call")
async def rpa_call(payload: dict = Body(default={})):
    """主动拨打（v1.4.0）：搜人 → 打开会话 → 点「语音/视频通话」。

    调用方负责护栏（时段、频率、对方在不在线），这里只管「手」。
    """
    body = payload or {}
    kind = str(body.get("kind") or "voice")
    name = str(body.get("name") or "")
    try:
        res = await asyncio.to_thread(RPA.call_now, kind, name)
    except Exception as e:
        # 不能让它变成裸 500：调用方（qq_call）要能拿到人话
        res = {"ok": False, "msg": "拨号时出错：%s" % e}
    return JSONResponse(res, status_code=200 if res.get("ok") else 409)


# ============================================================ 事件

@app.get("/api/av/events")
async def events(since: int = 0, limit: int = 100):
    return {"ok": True, "events": BUS.recent(limit=limit, since=since),
            "last_seq": BUS.last_seq}


@app.get("/api/av/events/stream")
async def events_stream(request: Request):
    """SSE：实时推送 TTS / 转写 / 推流 / RPA 事件。"""

    async def gen():
        seq = 0
        # 先补发最近的 20 条，客户端不至于空白
        for item in BUS.recent(20):
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            seq = max(seq, item["seq"])
        idle = 0
        while True:
            if await request.is_disconnected():
                break
            items = await BUS.wait_after(seq, timeout=1.0)
            if items:
                idle = 0
                for item in items:
                    seq = max(seq, item["seq"])
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            else:
                idle += 1
                if idle % 15 == 0:
                    yield ": ping\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no",
                 "connection": "keep-alive"},
    )


# ============================================================ 静态资源

if cfgmod.STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(cfgmod.STATIC_DIR)), name="static")
if cfgmod.ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(cfgmod.ASSETS_DIR)), name="assets")


# ============================================================ CLI

def _ensure_console() -> None:
    """无控制台 exe（--noconsole）里 sys.stdout/stderr 是 None。

    print() 和 logging 的 StreamHandler 都会当场炸掉，所以先挂到一个日志文件上，
    出问题还能去 %LOCALAPPDATA%\小鲸鱼\av_module\runtime\av_server_console.log 看。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        cfgmod.ensure_dirs()
        log_path = cfgmod.RUNTIME_DIR / "av_server_console.log"
        fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        import io as _io
        fh = _io.TextIOWrapper(_io.BytesIO(), encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh


def main() -> int:
    _ensure_console()
    parser = argparse.ArgumentParser(description="模拟音视频通话模块 (端口 5800)")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--rpa", action="store_true", help="启动时同时开启自动接听")
    parser.add_argument("--open", action="store_true", help="启动后打开测试 UI")
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--log-level", default=None)
    args = parser.parse_args()

    cfgmod.ensure_dirs()
    cfg = cfgmod.load()
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port
    level = (args.log_level or cfg.server.log_level or "info").upper()

    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # uvicorn 自己也会打日志，避免重复
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    url = f"http://{host}:{port}"
    print("=" * 66)
    print("  模拟音视频通话模块  v" + VERSION)
    print(f"  测试 UI     : {url}/")
    print(f"  接口文档    : {url}/docs")
    print("  驱动自检    : 控制台【音视频】页 → 检查驱动（或 python install_drivers.py --check）")
    print(f"  独立端口    : {port}（不占用主控台端口）")
    print("=" * 66)

    if args.rpa:
        RPA.start()
    if args.open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    uvicorn.run(
        "av_server:app" if args.reload else app,
        host=host, port=port, reload=args.reload, log_level=level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
