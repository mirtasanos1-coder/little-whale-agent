"""全自动验证「UIA 抓不到按钮 → 图像识别兜底点击」这条链路，不需要真的 QQ 来电。

流程：
    1. 起 tools/simulate_call.py（Tk 画的假来电窗口，自绘 UI，UIA 多半看不到按钮）
    2. 读它写下的按钮屏幕坐标 → 从整屏截图里裁出"接听"按钮模板
    3. 用模板在窗口区域里匹配，校验坐标是否对得上（不点击）
    4. 打开 CallWatcher（dry_run=False）→ 等它真的点下去 → 假窗口写 accepted
    5. 收尾：停 watcher、关窗口、删掉测试用的模板文件

用法：
    python tools\\test_rpa_image.py
    python tools\\test_rpa_image.py --keep    # 保留模板文件，方便你接着手工调
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from av_core import config as cfgmod  # noqa: E402
from av_core import rpa as rpamod  # noqa: E402
from av_core import screen as screenmod  # noqa: E402

STATE = ROOT / "runtime" / "sim_call_state.json"
TEMPLATE = ROOT / "rpa" / "templates" / "accept_simtest.png"


def log(msg: str) -> None:
    print(msg, flush=True)


def find_green_button(shot) -> tuple[int, int, int, int] | None:
    """在截图里按颜色找假窗口那个绿色『接听』按钮，返回 (x, y, w, h)。

    用连通域而不是整幅 bbox：屏幕上别处也可能有同色像素，取最大连通块才稳。
    不依赖 Tk 报的坐标（虽然目前看 Tk 报的是物理像素，但别赌这个）。
    """
    import cv2
    import numpy as np

    if shot is None:
        return None
    img = shot.astype("int16")
    r, g, b = img[:, :, 2], img[:, :, 1], img[:, :, 0]   # shot 是 BGR
    mask = (np.abs(r - 0x27) < 22) & (np.abs(g - 0xAE) < 22) & (np.abs(b - 0x60) < 22)
    if int(mask.sum()) < 500:
        return None
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(mask.astype("uint8"), 8)
    best = None
    for i in range(1, n):
        x, y, w, h, area = (int(stats[i, 0]), int(stats[i, 1]), int(stats[i, 2]),
                            int(stats[i, 3]), int(stats[i, 4]))
        if area < 1500 or w < 40 or h < 20:
            continue
        if best is None or area > best[0]:
            best = (area, x, y, w, h)
    if not best:
        return None
    return best[1], best[2], best[3], best[4]


def wait_for(getter, timeout: float, name: str):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            value = getter()
            if value:
                return value
        except Exception:
            pass
        time.sleep(0.25)
    log(f"✘ 等待 {name} 超时（{timeout}s）")
    return None


def read_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="验证 RPA 图像识别兜底")
    ap.add_argument("--keep", action="store_true", help="保留测试模板文件")
    args = ap.parse_args(argv)

    ok_img, why = screenmod.available()
    if not ok_img:
        log(f"✘ 图像依赖不可用：{why}\n  {screenmod.install_hint()}")
        return 1
    if not rpamod.available():
        log(f"✘ UIA 依赖不可用：{rpamod.install_hint()}")
        return 1

    # 清理上次遗留
    global STATE
    STATE = ROOT / "runtime" / f"sim_call_state_{os.getpid()}.json"
    for p in (STATE, TEMPLATE):
        try:
            p.unlink()
        except OSError:
            pass
    TEMPLATE.parent.mkdir(parents=True, exist_ok=True)

    log("① 启动假来电窗口…")
    sim = subprocess.Popen(
        [sys.executable, str(ROOT / "tools" / "simulate_call.py"), "--state", str(STATE)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        state = wait_for(lambda: read_state() if read_state().get("buttons") else None, 25,
                         "假窗口就绪")
        if not state:
            log("  窗口输出：\n" + (sim.stdout.read() if sim.stdout else ""))
            return 1
        rect = state["buttons"]["accept"]
        log(f"   窗口 {state.get('window_rect')} | 接听按钮 {rect}")

        log("② 截屏并按颜色找到『接听』按钮，裁出模板…")
        shot = screenmod.grab()
        if shot is None:
            log("✘ 截屏失败")
            return 1
        btn = find_green_button(shot)
        if not btn:
            log("✘ 截图里没找到绿色按钮（假窗口没在前台？）")
            return 1
        crop_h = int(btn[3])
        crop = btn[0], btn[1], btn[2], max(24, min(crop_h, 90))
        shot_path = ROOT / "runtime" / "sim_shot.png"
        from PIL import Image  # noqa: PLC0415

        Image.fromarray(shot[:, :, ::-1]).save(shot_path)
        saved = screenmod.crop_file(shot_path, TEMPLATE, crop)
        if not saved:
            log("✘ 裁剪失败")
            return 1
        log(f"   按钮区域 {btn} | 模板 -> {saved}")

        log("③ 校验模板匹配坐标（不点击）…")
        tpl = screenmod.load_template(TEMPLATE)
        full = screenmod.grab()
        hits = screenmod.match_all(full, tpl, 0.75, (1.0, 0.85, 1.15), max_hits=3)
        exp_cx, exp_cy = btn[0] + btn[2] // 2, btn[1] + crop[3] // 2
        if not hits:
            log("✘ 整屏匹配不到刚裁的模板（模板太小或纯色？）")
            return 1
        best = hits[0]
        got_cx, got_cy = best["x"] + best["w"] // 2, best["y"] + best["h"] // 2
        log(f"   命中 score={best['score']} 期望中心=({exp_cx},{exp_cy}) "
            f"实测中心=({got_cx},{got_cy})")
        if abs(got_cx - exp_cx) > 6 or abs(got_cy - exp_cy) > 6:
            log("✘ 坐标偏差过大，模板匹配不可靠")
            return 1

        log("④ 打开 CallWatcher（dry_run=False），等它自己点『接听』…")
        cfg = cfgmod.load()
        cfg.rpa.dry_run = False
        cfg.rpa.auto_hangup = False
        cfg.rpa.image_fallback = True
        cfg.rpa.match_threshold = 0.80
        watcher = rpamod.CallWatcher()
        watcher.start()

        def accepted() -> bool:
            return any(e.get("event") == "accepted" for e in read_state().get("events", []))

        got = wait_for(accepted, 25, "自动接听")
        status = watcher.status()
        if not got:
            # 再等一会儿，让轮询有机会真正动手（有的机器首帧截图慢）
            got = wait_for(accepted, 10, "自动接听（补时）")
            status = watcher.status()
        watcher.stop()

        log(f"   模式={status['mode']} 轮询={status['polls']} 点击={status['clicks']} "
            f"图像点击={status['image_clicks']} 状态={status['state']}")
        log(f"   最近匹配={json.dumps(status['image'].get('last_match'), ensure_ascii=False)}")
        if status.get("error"):
            log(f"   错误={status['error']}")
        for entry in status.get("log", [])[-4:]:
            log(f"   日志 {entry}")
        if not got:
            log("✘ 窗口没有收到接听点击")
            log("  排查：窗口是否被别的程序完全遮住？模板阈值太高？"
                "（可先用 python tools\\capture_template.py --test --window 模拟来电）")
            return 1

        if status["mode"] == "image" and status["image_clicks"] >= 1:
            log("✔ PASS：UIA 拿不到按钮 → 图像识别命中 → pyautogui 点击成功")
        elif status["mode"] == "uia":
            log("△ 注意：这个假窗口被 UIA 看到了按钮，走的是 UIA 路径（也算已接听）。"
                "真机自绘 UI 才会走图像路径。")
        else:
            log(f"△ 已点击，但模式是 {status['mode']}，请检查日志")
        return 0 if got else 1
    finally:
        try:
            sim.terminate()
            sim.wait(timeout=5)
        except Exception:
            try:
                sim.kill()
            except Exception:
                pass
        for p in (ROOT / "runtime" / "sim_shot.png",):
            try:
                p.unlink()
            except OSError:
                pass
        if not args.keep:
            try:
                TEMPLATE.unlink()
            except OSError:
                pass
            try:
                STATE.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
