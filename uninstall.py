# -*- coding: utf-8 -*-
"""卸载程序（v8）

为什么不能自己删自己：
    程序正在运行的时候，exe 和 _internal 里的 dll 都被系统锁着，
    直接删会失败（Windows 会报"文件正在被另一个程序使用"）。
    所以做法是：写一个临时 .bat，让它等几秒，等本进程真的退出之后再动手。

保留什么：
    用户选的"保留配置和记忆"= 只删程序文件，下面这些目录/文件留下：
        memory/            记忆库（memory.db + exports + backups）
        config.json        所有设置
        personas.json      人设
        stickers/ icons/ backgrounds/ themes/ langs/   用户放进去的东西
        moment_images/ generated/ qzone_inbox/ feed_inbox/    自己攒的素材
        memory_backup/     记忆备份
    不保留 = 整个目录一起删掉。
"""

import os
import sys
import time
import tempfile
import subprocess

# 卸载时无论如何都留下的东西（用户在设置里勾了"保留"才留）
KEEP_NAMES = (
    "memory", "config.json", "personas.json",
    "stickers", "icons", "backgrounds", "themes", "langs",
    "moment_images", "generated", "qzone_inbox", "feed_inbox",
    "memory_backup",
)


def _data_dir():
    """和 config.py 保持一致的数据目录规则。"""
    d = os.environ.get("QQ_AGENT_DATA_DIR", "").strip()
    if d:
        return os.path.abspath(d)
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def program_dir():
    """程序所在目录（打包版 = exe 同级；源码版 = 项目根）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _dir_size(path):
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return total


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%d B" % n


def plan_info():
    """给界面看的：会删什么、留什么。"""
    d = program_dir()
    keep, remove = [], []
    try:
        names = sorted(os.listdir(d))
    except Exception:
        names = []
    for n in names:
        full = os.path.join(d, n)
        if n in KEEP_NAMES:
            keep.append({
                "name": n,
                "size": _human(_dir_size(full) if os.path.isdir(full) else
                               (os.path.getsize(full) if os.path.isfile(full) else 0)),
            })
        else:
            remove.append({
                "name": n,
                "size": _human(_dir_size(full) if os.path.isdir(full) else
                               (os.path.getsize(full) if os.path.isfile(full) else 0)),
            })
    return {
        "ok": True,
        "dir": d,
        "data_dir": _data_dir(),
        "is_frozen": bool(getattr(sys, "frozen", False)),
        "keep": keep,
        "remove": remove,
        "remove_count": len(remove),
        "note": ("卸载后这些会留下：memory/（记忆库）、config.json（设置）、"
                 "personas.json（人设）以及你自己放进去的表情包/图标/背景。"
                 "下次装回来直接就能接着用。"),
    }


def _ps_quote(s):
    return "'" + str(s).replace("'", "''") + "'"


def _build_batch(keep_data, d, wait_sec=4):
    """生成等待-删除用的 .bat（用 PowerShell 过滤，比 for 循环可靠）。"""
    keep_list = ",".join(_ps_quote(x) for x in KEEP_NAMES)
    lines = [
        "@echo off",
        "chcp 65001 >nul 2>&1",
        "timeout /t %d /nobreak >nul 2>&1" % int(wait_sec),
    ]
    if keep_data:
        ps = (
            "$k=@(%s);"
            "Get-ChildItem -LiteralPath %s -Force -ErrorAction SilentlyContinue | "
            "Where-Object { $k -notcontains $_.Name } | "
            "Remove-Item -Recurse -Force -ErrorAction SilentlyContinue"
        ) % (keep_list, _ps_quote(d))
        lines.append('powershell -NoProfile -ExecutionPolicy Bypass -Command "%s"' % ps)
    else:
        lines.append('powershell -NoProfile -ExecutionPolicy Bypass -Command '
                     '"Remove-Item -LiteralPath %s -Recurse -Force '
                     '-ErrorAction SilentlyContinue"' % _ps_quote(d))
    # 自删
    lines.append('del /f /q "%%~f0" >nul 2>&1')
    return "\r\n".join(lines) + "\r\n"


def make_batch(keep_data=True, wait_sec=4, path=None):
    """只写 .bat 不执行（测试用 / 手工卸载用）。返回 bat 路径。"""
    d = program_dir()
    bat = path or os.path.join(tempfile.gettempdir(), "小鲸鱼_uninstall.bat")
    with open(bat, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(_build_batch(keep_data, d, wait_sec))
    return bat


def run(keep_data=True, wait_sec=4):
    """启动卸载。返回 (ok, msg)。调用方拿到 ok 后应当立刻退出程序。"""
    if not getattr(sys, "frozen", False):
        # 源码版不允许真删 —— 删了就找不回来了
        return False, ("这是源码版（python 直接跑的），不会真的删文件。"
                       "打包版（小鲸鱼.exe）里这个按钮才会真卸载。")
    try:
        bat = make_batch(keep_data, wait_sec)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | \
                    getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen(["cmd.exe", "/c", bat],
                         creationflags=flags, close_fds=True,
                         cwd=tempfile.gettempdir())
        keep_txt = "保留记忆和配置（只删程序）" if keep_data else "全部删除"
        return True, "正在卸载（%s）…程序会自动退出，稍等几秒就干净了。" % keep_txt
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


if __name__ == "__main__":
    import json
    print(json.dumps(plan_info(), ensure_ascii=False, indent=2))
