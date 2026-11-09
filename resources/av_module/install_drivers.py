#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""虚拟声卡 / 虚拟摄像头：检测 + 下载 + 静默安装（带 UAC 提权与杀软放行指引）。

用法
----
    python install_drivers.py                # 只检测，打印报告 + 给建议
    python install_drivers.py --check        # 同上（脚本化用，不交互）
    python install_drivers.py --download     # 只把安装包下到 drivers/
    python install_drivers.py --install      # 下载(缺则下) + 静默安装，需要时自动提权
    python install_drivers.py --install --yes        # 不询问，全自动（主控台/无人值守用）
    python install_drivers.py --install --only vbcable
    python install_drivers.py --install --force      # 已装也重装（修坏掉的设备）
    python install_drivers.py --json                 # 机器可读输出

关键实现点
----------
1. **检测以“设备是否出现”为准**，不信安装器的退出码（VB-Cable 成功也常返回 1）。
2. 提权用 ShellExecuteExW(runas) **整脚本重入**，全程只弹一次 UAC；
   用户点“否”会被识别为 1223 并给出人话提示，不会死循环。
3. 安装器跑完再轮询枚举 10 秒；仍失败则打印杀软放行指引（红字）。
4. 找不到安装包时自动去下载（vb-audio 官方源 / ghproxy 镜像），
   也可先用 `drivers/fetch_drivers.py` 单独预取。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from av_core import config as cfgmod                 # noqa: E402
from av_core import drivers as drvmod                # noqa: E402
from av_core import fetch as fetchmod                # noqa: E402
from av_core import winutil                          # noqa: E402

CHILD_ENV = "AV_DRIVERS_ELEVATED_CHILD"


# ============================================================ 展示

def print_status(status: dict) -> None:
    w = winutil
    w.banner("虚拟外设驱动状态")
    print(f"  目录      : {status['drivers_dir']}")
    print(f"  管理员    : {'是' if status['admin'] else '否'}")
    av = status.get("security_software") or []
    print(f"  安全软件  : {', '.join(av) if av else '未检测到常见国产安全软件'}")
    print()
    for key, info in status["drivers"].items():
        ok = info["installed"]
        head = "✔ 已安装" if ok else "✘ 未安装"
        color = "green" if ok else "red"
        w.say(f"  {head}  {info['name']}  ({key})", color)
        print(f"        供应商   : {info.get('vendor', '')}")
        print(f"        安装包   : {info.get('installer_path') or '缺失 → 需要下载'}")
        print(f"        下载源   : {', '.join(info.get('download_urls', [])[:2])}")
        if info.get("silent_args"):
            print(f"        静默参数 : {' '.join(info['silent_args'])}")
        sig = info.get("signature")
        if sig:
            flag = {"valid": ("✔ 有效", "green"),
                    "not_signed": ("△ 未签名（不影响安装，但更易被拦截）", "yellow"),
                    "hash_mismatch": ("✘ 哈希不匹配，文件可能被篡改！", "red"),
                    "not_trusted": ("△ 证书链不受信任", "yellow")}.get(
                        sig.get("status", ""), (f"△ {sig.get('status')}", "yellow"))
            w = winutil
            print("        签名     : ", end="")
            w.say(flag[0], flag[1])
            if sig.get("signer"):
                print(f"                   {sig['signer'][:80]}")
        for ev in info.get("evidence", [])[:4]:
            print(f"        · {ev}")
        if info.get("error"):
            print(f"        诊断     : {info['error']}")
        print()


def print_next_steps(status: dict) -> None:
    w = winutil
    ready = status.get("ready")
    w.banner("装好之后：把通话软件指向虚拟设备")
    print("  1) 语音：QQ/微信 设置 → 麦克风 选 [CABLE Output]  ；")
    print("          扬声器 保持真实耳机/音箱（模块用系统回环抓对方声音）")
    print("  2) 视频：QQ/微信 设置 → 摄像头 选 [OBS Virtual Camera]")
    print("  3) 装完/改完设置后 **重启 QQ 或微信**，否则设备列表里看不到新设备")
    print("  4) 启动模块：python av_server.py    然后浏览器打开 http://127.0.0.1:5800")
    print()
    if ready:
        w.say("虚拟设备齐全，可以开始自测了。", "green")
    else:
        w.say("虚拟设备还不全 —— 上面的接口仍可跑 dry-run，但声音/画面不会进对方。", "yellow")


def red_block_hint() -> None:
    winutil.fail_help(drvmod.AV_BLOCK_HINT)


# ============================================================ 提权

def need_elevation(missing: list[str]) -> bool:
    return bool(missing) and winutil.IS_WINDOWS and not winutil.is_admin()


def relaunch_elevated(argv: list[str]) -> int:
    w = winutil
    w.say("\n驱动安装需要管理员权限（UAC）。", "yellow")
    w.say("马上会弹出一个『用户账户控制』窗口 —— 请点『是』。", "yellow")
    os.environ[CHILD_ENV] = "1"
    extra = [a for a in argv]
    return winutil.relaunch_as_admin(extra)


# ============================================================ 主流程

def cmd_check(status: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print_status(status)
        missing = [k for k, v in status["drivers"].items() if not v["installed"]]
        if missing:
            w = winutil
            w.say(f"缺少 {len(missing)} 个虚拟设备: {', '.join(missing)}", "yellow")
            print("  补齐命令：python install_drivers.py --install        （会自动提权）")
            if status.get("security_software"):
                print()
                w.say("检测到安全软件在运行，安装时请留意它的弹窗并选择『允许』。", "yellow")
        print_next_steps(status)
    return 0 if status.get("ready") else 1


def cmd_download(keys: list[str], force: bool, as_json: bool) -> int:
    w = winutil
    w.banner("下载驱动安装包 → drivers/")
    cfgmod.ensure_dirs()
    results = fetchmod.fetch_all(only=keys, force=force)
    ok = all(r.get("ok") for r in results.values())
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print()
        for key, res in results.items():
            if res.get("ok"):
                w.say(f"  ✔ {key}: {res.get('file')}  ({res.get('size_mb')} MB)", "green")
            else:
                w.say(f"  ✘ {key}: 下载失败", "red")
                for e in (res.get("errors") or [])[:4]:
                    print(f"      · {e}")
                if res.get("homepage"):
                    print(f"      手动下载: {res['homepage']}")
        mf = cfgmod.DRIVERS_DIR / "MANIFEST.json"
        print(f"\n  清单已写入: {mf}")
    return 0 if ok else 2


def cmd_install(keys: list[str], force: bool, yes: bool, as_json: bool,
                argv: list[str], no_elevate: bool = False) -> int:
    w = winutil
    status = drvmod.status_all(cfgmod.DRIVERS_DIR)

    missing = [k for k in keys if not status["drivers"][k]["installed"]]
    if not missing and not force:
        if as_json:
            print(json.dumps({"ok": True, "action": "nothing_to_do", "status": status},
                             ensure_ascii=False, indent=2))
        else:
            w.banner("全部虚拟设备已就绪")
            w.say("无需安装。要强制重装请加 --force。", "green")
            print_next_steps(status)
        return 0

    # —— 先补齐安装包（不需要管理员） ——
    need_dl = []
    for k in missing or keys:
        if drvmod.SPECS[k].find_installer(cfgmod.DRIVERS_DIR) is None:
            need_dl.append(k)
    if need_dl:
        w.banner("缺少安装包，先下载")
        dl = fetchmod.fetch_all(only=need_dl)
        if not all(r.get("ok") for r in dl.values()):
            w.say("\n有安装包没下下来。请按上面的提示手动下载后重跑本脚本。", "red")
            if as_json:
                print(json.dumps({"ok": False, "download": dl}, ensure_ascii=False, indent=2))
            return 2
        status = drvmod.status_all(cfgmod.DRIVERS_DIR)
        missing = [k for k in keys if not status["drivers"][k]["installed"]]

    # —— 提权（整脚本重入，只弹一次 UAC） ——
    if need_elevation(missing) and not force and not no_elevate:
        if not yes:
            try:
                ans = input("需要管理员权限安装驱动，现在提权？[Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans not in ("", "y", "yes"):
                w.say("已跳过安装。之后可右键『以管理员身份运行』重试。", "yellow")
                return 3
        # 避免在提权失败的非管理员环境里无限重入
        if os.environ.get(CHILD_ENV) == "1":
            w.say("提权后仍未获得管理员权限（可能被策略拦截）。", "red")
            red_block_hint()
            return 4
        code = relaunch_elevated(argv)
        return code

    # —— 逐个安装 ——
    w.banner("静默安装")
    results: dict[str, dict] = {}
    for key in (missing or keys):
        spec = drvmod.SPECS[key]
        results[key] = drvmod.install_driver(
            spec, cfgmod.DRIVERS_DIR, cfgmod.load().drivers.install_timeout,
            force=force, elevate=(not winutil.is_admin()) and not no_elevate,
        )

    final = drvmod.status_all(cfgmod.DRIVERS_DIR)
    ok = all(final["drivers"][k]["installed"] for k in keys)
    fetchmod.write_manifest()

    if as_json:
        print(json.dumps({"ok": ok, "results": results, "status": final},
                         ensure_ascii=False, indent=2))
    else:
        w.banner("结果")
        for key in keys:
            info = final["drivers"][key]
            if info["installed"]:
                w.say(f"  ✔ {info['name']} 已就绪", "green")
            else:
                w.say(f"  ✘ {info['name']} 仍未安装", "red")
                if info.get("error"):
                    print(f"      {info['error']}")
                for ev in (info.get("after") or {}).get("evidence", [])[:3]:
                    print(f"      · {ev}")
        if not ok:
            actions = {k: str(v.get("action") or "") for k, v in results.items()}
            needs_admin = [k for k, a in actions.items() if a == "needs_admin"]
            blocked = [k for k, a in actions.items()
                       if a in ("verify_failed", "elevate_failed", "exec_failed", "timeout")]
            if needs_admin and not blocked:
                w.say("\n失败原因：缺少管理员权限（不是被杀软拦截）。", "yellow")
                w.say("  重跑并允许 UAC：python install_drivers.py --install --yes", "yellow")
                w.say("  或右键本脚本 →『以管理员身份运行』，在弹窗里点『是』。", "yellow")
            if blocked:
                red_block_hint()
            for key in keys:
                spec = drvmod.SPECS[key]
                if spec.extra.get("reboot") and not final["drivers"][key]["installed"]:
                    w.say(f"  {spec.name}: {spec.extra['reboot']}", "yellow")
        print_next_steps(final)
    return 0 if ok else 1


def interactive_menu(argv: list[str]) -> int:
    w = winutil
    status = drvmod.status_all(cfgmod.DRIVERS_DIR)
    print_status(status)
    if status.get("ready"):
        print_next_steps(status)
        return 0
    print("  可选操作：")
    print("    [1] 下载并安装缺失的驱动（推荐，会提权）")
    print("    [2] 只下载安装包到 drivers/（不提权）")
    print("    [3] 退出")
    try:
        choice = input("\n请选择 [1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        return 0
    if choice == "2":
        return cmd_download(list(drvmod.SPECS.keys()), False, False)
    if choice == "1":
        missing = [k for k, v in status["drivers"].items() if not v["installed"]]
        return cmd_install(missing or list(drvmod.SPECS.keys()), False, False, False,
                           argv + ["--install", "--yes"])
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="虚拟声卡/虚拟摄像头 检测、下载、静默安装",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true", help="只检测（默认行为）")
    parser.add_argument("--download", action="store_true", help="只下载安装包到 drivers/")
    parser.add_argument("--install", action="store_true", help="下载(缺则下)+安装")
    parser.add_argument("--only", default="", help="只处理指定驱动，逗号分隔: vbcable,virtualcam")
    parser.add_argument("--force", action="store_true", help="已安装也重装")
    parser.add_argument("--yes", "-y", action="store_true", help="不询问，全自动")
    parser.add_argument("--json", action="store_true", help="JSON 输出（供主控台解析）")
    parser.add_argument("--no-elevate", action="store_true", help="不提权（仅诊断用）")
    args = parser.parse_args(argv)

    winutil.enable_ansi()
    cfgmod.ensure_dirs()

    keys = [k.strip() for k in args.only.split(",") if k.strip()] or list(drvmod.SPECS.keys())
    bad = [k for k in keys if k not in drvmod.SPECS]
    if bad:
        winutil.say(f"未知驱动: {', '.join(bad)}  可选: {', '.join(drvmod.SPECS)}", "red")
        return 64

    if args.download:
        return cmd_download(keys, args.force, args.json)
    if args.install:
        return cmd_install(keys, args.force, args.yes, args.json, argv,
                           no_elevate=args.no_elevate)
    if args.check or args.json or not sys.stdin.isatty():
        return cmd_check(drvmod.status_all(cfgmod.DRIVERS_DIR, with_signature=True), args.json)
    return interactive_menu(argv)


if __name__ == "__main__":
    raise SystemExit(main())
