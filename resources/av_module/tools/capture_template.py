"""采集/验证 RPA 按钮模板（图像识别兜底用）。不需要启动服务。

用法：
    # 看看现有模板与依赖状态
    python tools\\capture_template.py --list

    # 截一张（优先 QQ/微信窗口，找不到就整屏），存成 rpa/templates/_snapshot.png
    python tools\\capture_template.py --snapshot --window QQ

    # 从上一步的截图里裁出按钮（坐标相对截图左上角）
    python tools\\capture_template.py --crop accept --region 320,180,64,64
    python tools\\capture_template.py --crop hangup --region 420,180,64,64

    # 验证：在当前屏幕上匹配（只匹配不点击），打印分数与坐标
    python tools\\capture_template.py --test --window QQ

    # 链路自检：截屏 → 裁一块 → 再找回来（不涉及 QQ）
    python tools\\capture_template.py --bench
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from av_core import config as cfgmod  # noqa: E402
from av_core import rpa as rpamod  # noqa: E402
from av_core import screen as screenmod  # noqa: E402


def _parse_region(text: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in text.replace("，", ",").split(",") if p.strip()]
    if len(parts) != 4:
        raise SystemExit("--region 需要 4 个数字: 左,上,宽,高（例: 320,180,64,64）")
    return tuple(int(float(p)) for p in parts)  # type: ignore[return-value]


def cmd_list() -> int:
    cfg = cfgmod.load()
    watcher = rpamod.CallWatcher()
    info = watcher.templates()
    print("模板目录:", info["dir"])
    print("接听:", info["accept"] or "（空）")
    print("挂断:", info["hangup"] or "（空）")
    print("就绪:", "是" if info["ready"] else "否 —— 见 rpa/templates/README.md")
    print("阈值:", info["threshold"], "| 缩放:", info["scales"])
    print("opencv/pyautogui:", "可用" if info["available"] else f"不可用（{info['error']}）")
    print("屏幕(物理像素):", info["screen"])
    print("rpa.image_fallback:", cfg.rpa.image_fallback, "| auto_hangup:", cfg.rpa.auto_hangup)
    if not info["available"]:
        print("\n" + screenmod.install_hint())
        return 1
    return 0


def cmd_snapshot(keyword: str | None) -> int:
    res = rpamod.CallWatcher().snapshot(keyword)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


def cmd_crop(action: str, region_text: str) -> int:
    root = rpamod.template_root()
    src = root / "_snapshot.png"
    if not src.is_file():
        raise SystemExit(f"找不到 {src}，先跑 --snapshot")
    action = action.strip().lower()
    if action not in ("accept", "hangup"):
        raise SystemExit("--crop 的动作只能是 accept 或 hangup")
    region = _parse_region(region_text)
    dest = root / f"{action}.png"
    saved = screenmod.crop_file(src, dest, region)
    if not saved:
        raise SystemExit(f"裁剪失败：region={region} 超出截图范围")
    print("已保存:", saved)

    # 立刻验证：把裁出来的模板放回原图里找一遍
    shot = screenmod.load_template(src)
    tpl = screenmod.load_template(saved)
    if shot is not None and tpl is not None:
        hits = screenmod.match_all(shot, tpl, 0.6, (1.0,), max_hits=1)
        if hits:
            print(f"自检：在截图里找到自己 score={hits[0]['score']} "
                  f"期望=({region[0]},{region[1]}) 实际=({hits[0]['x']},{hits[0]['y']})")
        else:
            print("自检：没能在截图里找到刚裁的图（裁得太小或是纯色块？）")
    return 0


def cmd_test(keyword: str | None) -> int:
    res = rpamod.CallWatcher().test_match(keyword)
    if not res.get("ok"):
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 1
    print("模板:", json.dumps(res["templates"], ensure_ascii=False))
    print("阈值:", res["threshold"], "| 命中:", len(res["hits"]))
    for h in res["hits"]:
        print(f"  {h['action']:<7} score={h['score']:<6} 点击({h['x']},{h['y']}) "
              f"窗口={h['window']!r} 模板={h['template']}")
    if not res["hits"]:
        print("没命中：把 rpa.match_threshold 降到 0.7 再试，或重新截一张更干净的按钮图")
    return 0


def cmd_bench() -> int:
    res = screenmod.benchmark_match(3)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("ok"):
        print(f"\n自检通过：屏幕 {res['screen']}，模板匹配 {res['ms']}ms/次")
        return 0
    print("\n自检失败，见上面的 error")
    return 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="RPA 按钮模板采集/验证")
    ap.add_argument("--list", action="store_true", help="列出模板与依赖状态")
    ap.add_argument("--snapshot", action="store_true", help="截图存到 rpa/templates/_snapshot.png")
    ap.add_argument("--window", default=None, help="窗口名关键词（QQ、微信…），默认用配置里的词表")
    ap.add_argument("--crop", default=None, choices=["accept", "hangup"], help="从截图裁剪模板")
    ap.add_argument("--region", default=None, help="裁剪区域 左,上,宽,高（相对截图）")
    ap.add_argument("--test", action="store_true", help="在当前屏幕上匹配（不点击）")
    ap.add_argument("--bench", action="store_true", help="截屏+匹配链路自检")
    args = ap.parse_args(argv)

    if args.list:
        return cmd_list()
    if args.snapshot:
        return cmd_snapshot(args.window)
    if args.crop:
        if not args.region:
            raise SystemExit("--crop 必须配 --region")
        return cmd_crop(args.crop, args.region)
    if args.test:
        return cmd_test(args.window)
    if args.bench:
        return cmd_bench()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
