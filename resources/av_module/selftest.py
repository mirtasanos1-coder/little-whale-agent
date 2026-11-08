#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""不开 QQ、不开浏览器，命令行里把整条链路自测一遍。

    python selftest.py                 # 环境体检 + 快速自测（推荐先跑这个）
    python selftest.py --speak "你好"   # 只测 TTS → 虚拟麦克风
    python selftest.py --tone          # 只测发声（不联网）
    python selftest.py --listen 6      # 录 6 秒并转写
    python selftest.py --video 5       # 推流 5 秒（没驱动走 dry-run）
    python selftest.py --rpa-scan      # 打印桌面窗口的 UIA 控件树
    python selftest.py --drivers       # 驱动检测报告
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from av_core import audio_io, config as cfgmod, drivers as drvmod, rpa as rpamod  # noqa: E402
from av_core import stt as sttmod, tts as ttsmod                                    # noqa: E402
from av_core.pipeline import AVPipeline                                             # noqa: E402
from av_core.winutil import banner, enable_ansi, is_admin, say                       # noqa: E402


def section(title: str) -> None:
    print()
    banner(title)


def row(k: str, v: str, color: str = "reset") -> None:
    pad = " " * max(1, 26 - len(k) * 2)
    print(f"  {k}{pad}", end="")
    say(str(v), color)


def check_env() -> dict:
    section("环境体检")
    problems: list[str] = []
    ok = True

    deps = [
        ("numpy", "numpy"), ("sounddevice", "sounddevice"), ("soundcard", "soundcard"),
        ("PIL", "pillow"), ("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
        ("edge_tts", "edge-tts"), ("miniaudio", "miniaudio"),
    ]
    for mod, pkg in deps:
        try:
            __import__(mod)
            row(mod, "ok", "green")
        except Exception as exc:  # noqa: BLE001
            row(mod, f"缺失 ({type(exc).__name__})  pip install {pkg}", "red")
            problems.append(pkg)
            ok = False

    optional = [
        ("faster_whisper", "faster-whisper", "语音转文字"),
        ("pyvirtualcam", "pyvirtualcam", "虚拟摄像头推流"),
        ("uiautomation", "uiautomation comtypes", "自动接听"),
    ]
    for mod, pkg, why in optional:
        try:
            __import__(mod)
            row(mod, f"ok  ({why})", "green")
        except Exception:  # noqa: BLE001
            row(mod, f"未装 — {why}不可用：pip install {pkg}", "yellow")

    row("管理员权限", "是" if is_admin() else "否（安装驱动时需要提权）",
        "green" if is_admin() else "yellow")
    row("Python", sys.version.split()[0])
    return {"ok": ok, "problems": problems}


def check_devices(as_info: bool = True) -> dict:
    section("音频设备 / 虚拟声卡")
    try:
        info = audio_io.describe_devices()
    except Exception as exc:  # noqa: BLE001
        say(f"  枚举失败: {type(exc).__name__}: {exc}", "red")
        return {}
    vc = info.get("virtual_cable", {})
    pb = vc.get("playback_found") or []
    rc = vc.get("recording_found") or []
    row("CABLE Input(播放)", "✔ " + " | ".join(pb) if pb else "✘ 未找到", "green" if pb else "red")
    row("CABLE Output(录音)", "✔ " + " | ".join(rc) if rc else "✘ 未找到", "green" if rc else "red")
    lo = info.get("loopback_sources") or []
    row("回环源", " | ".join(x.get("name", "") for x in lo) or "无", "green" if lo else "yellow")
    row("输出设备数", str(len(info.get("output") or [])))
    row("输入设备数", str(len(info.get("input") or [])))
    if as_info:
        for x in (info.get("output") or []):
            print(f"      [out {x.get('index')}] {x.get('name')}")
        for x in (info.get("input") or []):
            print(f"      [in  {x.get('index')}] {x.get('name')}")
    if not pb:
        say("  ⚠ 没有虚拟声卡：发声只会从扬声器外放，QQ 里听不到。"
            "请跑 python install_drivers.py --install", "yellow")
    return info


def check_drivers() -> dict:
    section("虚拟驱动状态")
    st = drvmod.status_all(cfgmod.DRIVERS_DIR)
    for key, info in st["drivers"].items():
        color = "green" if info["installed"] else "red"
        row(info["name"][:24],
            ("✔ 已装  " if info["installed"] else "✘ 未装  ") +
            (f"安装包: {Path(info['installer_path']).name}"
             if info.get("installer_path") else "安装包缺失"), color)
    if st.get("security_software"):
        row("安全软件", ", ".join(st["security_software"]), "yellow")
    if not st.get("ready"):
        say("  → 安装: python install_drivers.py --install", "yellow")
    return st


def test_speak(text: str, engine: str | None = None) -> dict:
    section(f"TTS → 虚拟麦克风: {text[:24]}")
    pipe = AVPipeline()
    tgt = pipe.output_target()
    row("输出目标", tgt["resolved_name"] or "(系统默认)", "green" if tgt["virtual_cable"] else "yellow")
    t0 = time.time()
    try:
        res = pipe.speak(text, engine=engine)
    except Exception as exc:  # noqa: BLE001
        say(f"  失败: {type(exc).__name__}: {exc}", "red")
        traceback.print_exc()
        return {"ok": False}
    row("引擎/音色", f"{res['engine']} / {res['voice']}", "green")
    row("音频时长", f"{res['audio_seconds']}s（合成 {res['synth_ms']}ms）")
    row("播放耗时", f"{int((time.time() - t0) * 1000)}ms")
    row("进入虚拟声卡", "是（对方能听到）" if res["played"]["virtual_cable"] else "否（只在本机响）",
        "green" if res["played"]["virtual_cable"] else "yellow")
    if res["file"]:
        row("缓存文件", res["file"])
    return res


def test_tone(freq: float = 660.0, secs: float = 1.5) -> dict:
    section("发声自测（不需要网络/TTS）")
    pipe = AVPipeline()
    try:
        res = pipe.test_tone(freq, secs)
    except Exception as exc:  # noqa: BLE001
        say(f"  失败: {type(exc).__name__}: {exc}", "red")
        return {"ok": False}
    row("播放设备", res["played"]["device"], "green")
    row("进虚拟声卡", "是" if res["played"]["virtual_cable"] else "否（降级）",
        "green" if res["played"]["virtual_cable"] else "yellow")
    return res


def test_listen(secs: float = 5.0, mode: str | None = None) -> dict:
    section(f"录音 {secs}s → Whisper 转写")
    if not sttmod.available():
        say(f"  {sttmod.install_hint()}", "red")
        return {"ok": False}
    say("  请现在对着麦克风/扬声器说话 ...", "yellow")
    pipe = AVPipeline()
    res = pipe.listen(secs, mode=mode)
    if not res.get("ok"):
        row("结果", res.get("error", "失败"), "red")
        if res.get("hint"):
            row("提示", res["hint"], "yellow")
        return res
    row("音频/rms", f"{res['audio_seconds']}s / {res['rms']}" +
        ("  (静音)" if res["silent"] else ""), "yellow" if res["silent"] else "green")
    row("转写", res["text"] or "（空）", "green" if res["text"] else "yellow")
    row("rtf", str(res["rtf"]))
    return res


def test_video(secs: float = 5.0, image: str | None = None, effect: str | None = None) -> dict:
    section(f"推流 {secs}s → 虚拟摄像头")
    pipe = AVPipeline()
    st = pipe.cam.start(image, effect)
    row("摄像头", "已打开" if st["camera_open"] else "未打开（dry-run）",
        "green" if st["camera_open"] else "yellow")
    row("后端/分辨率", f"{st['backend']} / {st['resolution']}")
    time.sleep(max(1.0, secs))
    st = pipe.cam.status()
    row("实测帧率", f"{st['fps_actual']} fps（目标 {st['fps_target']}）",
        "green" if st["fps_actual"] > 1 else "yellow")
    preview = cfgmod.RUNTIME_DIR / "preview.jpg"
    try:
        preview.write_bytes(pipe.cam.preview_jpeg())
        row("预览图", str(preview), "green")
    except Exception as exc:  # noqa: BLE001
        row("预览图", f"生成失败: {exc}", "red")
    pipe.cam.stop()
    return st


def test_rpa_scan(depth: int = 4) -> dict:
    section("UIA 窗口扫描（用于调接听/挂断词）")
    if not rpamod.available():
        say(f"  {rpamod.install_hint()}", "red")
        return {"ok": False}
    res = rpamod.CallWatcher().scan(max_depth=depth, limit=25)
    if not res.get("ok"):
        say(f"  {res.get('error')}", "red")
        return res
    wins = res.get("windows") or []
    if not wins:
        say("  没扫到窗口。先打开 QQ/微信 再来。", "yellow")
        return res
    for w in wins[:12]:
        print(f"  [窗口] {w['name']}   ({w.get('class', '')})")
        for c in (w.get("controls") or [])[:8]:
            print(f"        · {c['type']:<18} {c['name'] or c['automation_id']}")
    hits = res.get("matched_labels") or {}
    row("命中接听词", " | ".join(hits.get("accept") or []) or "无",
        "green" if hits.get("accept") else "yellow")
    row("命中挂断词", " | ".join(hits.get("hangup") or []) or "无",
        "green" if hits.get("hangup") else "yellow")
    return res


def quick_all() -> int:
    cfgmod.ensure_dirs()
    env = check_env()
    dev = check_devices(as_info=False)
    drv = check_drivers()
    tts = ttsmod.edge_available()
    row("edge-tts", "可用" if tts else "不可用（网络/版本问题，会回退 SAPI）",
        "green" if tts else "yellow")
    row("STT 模型", sttmod.ENGINE.status().get("model", "?"))
    row("配置文件", str(cfgmod.CONFIG_PATH))
    row("运行目录", str(cfgmod.RUNTIME_DIR))
    print()
    banner("结论")
    pb = (dev.get("virtual_cable") or {}).get("playback_found") or []
    if pb:
        say("  虚拟声卡就绪 → 可以跑 python av_server.py 并在 QQ 里选 CABLE Output 当麦克风", "green")
    else:
        say("  虚拟声卡缺失 → 先装驱动: python install_drivers.py --install", "yellow")
    say("  下一步：", "cyan")
    print("      python av_server.py --open        # 打开图形测试台")
    print("      python selftest.py --tone         # 听一下有没有声音")
    print("      python selftest.py --listen 6     # 测录音+转写")
    print("      python selftest.py --rpa-scan     # 看 QQ/微信 的控件名")
    return 0 if env["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="音视频模块命令行自测")
    ap.add_argument("--speak", metavar="TEXT", help="TTS 一段话并播放")
    ap.add_argument("--engine", choices=["edge", "sapi"], help="TTS 引擎")
    ap.add_argument("--tone", action="store_true", help="播放测试音")
    ap.add_argument("--listen", type=float, metavar="SEC", help="录音并转写")
    ap.add_argument("--mode", choices=["loopback", "device"], help="录音模式")
    ap.add_argument("--video", type=float, metavar="SEC", help="推流指定秒数")
    ap.add_argument("--image", help="推流用的图片路径")
    ap.add_argument("--effect", help="推流动效 kenburns/pulse/still")
    ap.add_argument("--rpa-scan", action="store_true", help="扫描窗口控件")
    ap.add_argument("--drivers", action="store_true", help="驱动检测")
    ap.add_argument("--env", action="store_true", help="只做环境体检")
    args = ap.parse_args(argv)

    enable_ansi()
    cfgmod.ensure_dirs()
    did = False
    if args.env:
        return 0 if check_env()["ok"] else 1
    if args.drivers:
        check_drivers()
        did = True
    if args.rpa_scan:
        test_rpa_scan()
        did = True
    if args.tone:
        test_tone()
        did = True
    if args.speak:
        test_speak(args.speak, args.engine)
        did = True
    if args.listen:
        test_listen(args.listen, args.mode)
        did = True
    if args.video:
        test_video(args.video, args.image, args.effect)
        did = True
    if did:
        return 0
    return quick_all()


if __name__ == "__main__":
    raise SystemExit(main())
