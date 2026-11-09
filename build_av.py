"""把「音视频接入模块」编译成 av_server.exe。

V1.3.2起：用 PyInstaller打成单文件 exe（fastapi / uvicorn / edge-tts /音频 /
视频 / RPA /语音识别），放进 resources/av_module/av_server.exe，
av_bridge.py优先拉起它，找不到 exe才退回 python av_server.py（开发者本机用）。

用法
----
    python build_av.py #默认：带语音识别（faster-whisper），不带 opencv
    python build_av.py --with-cv #额外带 opencv（图像兜底点按），体积 +110MB
    python build_av.py --lite #最省：不带语音识别，也不带 opencv
    python build_av.py --clean #先删 build/ dist/

产物
----
    resources/av_module/av_server.exe
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
AV_SRC = HERE / "resources" / "av_module"
AV_OUT = HERE / "resources" / "av_module" / "av_server.exe"

#这些包全部是「懒加载」的：av_core里在函数内部 import，
# PyInstaller静态分析看不到，必须显式告诉它。
HIDDEN_BASE = [
    "av_core", "av_core.config", "av_core.drivers", "av_core.events",
    "av_core.fetch", "av_core.pipeline", "av_core.rpa", "av_core.screen",
    "av_core.stt", "av_core.tts", "av_core.video", "av_core.winutil",
    "av_core.audio_io",
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "fastapi", "starlette", "pydantic", "anyio",
    "sounddevice", "soundcard", "miniaudio", "edge_tts", "edge_tts.communicate",
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
    "pyvirtualcam", "uiautomation", "comtypes", "pyautogui",
    "requests", "websockets", "aiohttp",
]

HIDDEN_STT = [
    "faster_whisper", "ctranslate2", "tokenizers", "numpy",
    "onnxruntime", "onnxruntime.capi.onnxruntime_pybind11_state",
]

HIDDEN_CV = ["cv2", "numpy"]

EXCLUDES = [
    "matplotlib", "pandas", "scipy", "notebook", "IPython", "pytest",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "torch", "tensorflow",
    "setuptools._distutils", "tkinter", "distutils",
]

# v1.6.3修（QA P1-2：STT默认配置下必失败）：
# faster_whisper的 VAD用 onnxruntime加载它自己包里的 assets/silero_vad_v6.onnx。
#以前 PyInstaller只收了 faster_whisper的「代码」，assets目录没进去 ——
#打包后第一次说话，VAD找不到模型文件，卡113秒再抛 NoSuchFile。
#这里把 assets下的数据文件显式收进来，落到 _MEIPASS/faster_whisper/assets/。
STT_ASSET_PACKAGE = "faster_whisper"
STT_ASSET_INCLUDES = ["assets/*"]


def _collect_stt_assets():
    """返回 [(源文件,包内目标目录)]，给 PyInstaller的 --add-data用。"""
    try:
        from PyInstaller.utils.hooks import collect_data_files
    except Exception as e: #没装 PyInstaller
        print("拿不到 PyInstaller的 collect_data_files（%s），STT assets可能漏收" % e)
        return []
    try:
        files = collect_data_files(STT_ASSET_PACKAGE, includes=STT_ASSET_INCLUDES)
    except Exception as e:
        print("收集 %s的数据文件失败：%s" % (STT_ASSET_PACKAGE, e))
        return []
    pairs = []
    for src, dest in files:
        pairs.append((str(src), dest or ("%s/assets" % STT_ASSET_PACKAGE)))
    return pairs


def _check_stt_assets(pairs) -> bool:
    """确认 VAD模型真的收进来了；没有就大声说出来，别又打出一个坏包。"""
    names = [os.path.basename(src) for src, _ in pairs]
    onnx = [n for n in names if n.endswith(".onnx")]
    if not pairs:
        print("!!没收集到 faster_whisper的任何数据文件 ——打出来的包 STT会失败")
        return False
    if not onnx:
        print("!! faster_whisper里没找到 .onnx（silero_vad_v6.onnx缺失），"
            "STT打包后会退化成「无 VAD」模式")
        return False
    print("STT资源：%d个文件，含 onnx模型 %s"
        % (len(pairs), "、".join(onnx)))
    return True


def _newest_mtime() -> float:
    """源码 +静态资源 +模板图里最新的一个 mtime（用来判断要不要重打）。

    v1.6.3（QA P1-3）：模板图是 .png不是 .py。以前只比 *.py的 mtime，
    往 rpa/templates里补了接听/挂断按钮模板也会被判成「已是最新」而跳过重打，
    打出来的包里永远没有模板。
    """
    stamps = []
    for p in AV_SRC.rglob("*.py"):
        try:
            stamps.append(p.stat().st_mtime)
        except OSError:
            pass
    for _dir in ("static", "assets", "rpa", "drivers"):
        _p = AV_SRC / _dir
        if not _p.is_dir():
            continue
        for q in _p.rglob("*"):
            try:
                if q.is_file():
                    stamps.append(q.stat().st_mtime)
            except OSError:
                pass
    return max(stamps) if stamps else 0


def build(with_stt: bool, with_cv: bool, clean: bool) -> int:
    if not (AV_SRC / "av_server.py").is_file():
        print("找不到 %s" % (AV_SRC / "av_server.py"))
        return 2

    #已经打过、且比所有输入都新 →直接跳过（打一次要2~3分钟，别每次都等）
    if not clean and AV_OUT.is_file():
        newest = _newest_mtime()
        if AV_OUT.stat().st_mtime >= newest:
            print("av_server.exe已是最新（%.1f MB），跳过重打；要强制重打加 --clean"
                % (AV_OUT.stat().st_size / 1048576))
            return 0

    if clean:
        for d in ("build", "dist"):
            p = AV_SRC / d
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                print("已清理 %s" % p)

    hidden = list(HIDDEN_BASE)
    if with_stt:
        hidden += HIDDEN_STT
    if with_cv:
        hidden += HIDDEN_CV

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", "av_server",
        "--distpath", str(AV_SRC / "dist"),
        "--workpath", str(AV_SRC / "build"),
        "--specpath", str(AV_SRC / "build"),
        #静态资源必须跟着走（config.STATIC_DIR = _MEIPASS/static）
        "--add-data", "%s;static" % (AV_SRC / "static"),
        "--add-data", "%s;assets" % (AV_SRC / "assets"),
        # v1.6.3（QA P1-3）：rpa/templates里的接听/挂断模板图跟着走
        "--add-data", "%s;rpa" % (AV_SRC / "rpa"),
        "--add-data", "%s;drivers" % (AV_SRC / "drivers"),
        "--console",
    ]
    for m in hidden:
        cmd += ["--hidden-import", m]

    # v1.6.3（QA P1-2）：把 faster_whisper/assets/*（含 silero_vad_v6.onnx）打进去。
    #每个文件单独 --add-data到 faster_whisper/assets/：faster_whisper内部是用
    # __file__拼 assets路径的，所以包内目录名必须一模一样。
    if with_stt:
        _stt_assets = _collect_stt_assets()
        _check_stt_assets(_stt_assets)
        for _src, _dest in _stt_assets:
            cmd += ["--add-data", "%s;%s" % (_src, _dest)]

    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    cmd.append(str(AV_SRC / "av_server.py"))

    print("开始打包音视频模块（STT=%s, cv2=%s）…" % (with_stt, with_cv))
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    rc = subprocess.call(cmd, cwd=str(AV_SRC), env=env)
    if rc !=0:
        print("PyInstaller失败（退出码 %s）" % rc)
        return rc

    built = AV_SRC / "dist" / "av_server.exe"
    if not built.is_file():
        print("没找到产物 %s" % built)
        return 3
    shutil.copy2(built, AV_OUT)
    mb = AV_OUT.stat().st_size / 1048576
    print("产物：%s（%.1f MB）" % (AV_OUT, mb))
    #构建中间件（145MB的 build/dist）拷完就删，免得被打进安装包
    for d in ("dist", "build"):
        shutil.rmtree(AV_SRC / d, ignore_errors=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lite", action="store_true", help="不带语音识别、不带 opencv（最小）")
    ap.add_argument("--with-cv", action="store_true", help="额外带 opencv（图像兜底点按）")
    ap.add_argument("--clean", action="store_true", help="先清 build/dist")
    a = ap.parse_args()
    with_stt = not a.lite
    with_cv = bool(a.with_cv) and not a.lite
    raise SystemExit(build(with_stt, with_cv, a.clean))


if __name__ == "__main__":
    main()
