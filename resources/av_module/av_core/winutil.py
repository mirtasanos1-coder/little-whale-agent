"""Windows 平台小工具：管理员权限、UAC 提权、彩色控制台、进程探测。

这里刻意只依赖 ctypes / 标准库，保证 `install_drivers.py` 在**没装任何第三方包**
的干净机器上也能跑（驱动安装往往发生在依赖安装之前）。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterable

IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------- 控制台颜色

_ANSI = {
    "red": "\033[91m",
    "green": "\033[92m",
    "yellow": "\033[93m",
    "blue": "\033[94m",
    "magenta": "\033[95m",
    "cyan": "\033[96m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}
_color_enabled = True


def enable_ansi() -> None:
    """Windows 10+ 控制台默认不解析 ANSI，需要打开 VT 处理。"""
    global _color_enabled
    if not IS_WINDOWS:
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        _color_enabled = False


def clr(text: str, color: str = "reset") -> str:
    if not _color_enabled:
        return text
    return f"{_ANSI.get(color, '')}{text}{_ANSI['reset']}"


def say(text: str = "", color: str = "reset") -> None:
    print(clr(text, color), flush=True)


def banner(title: str) -> None:
    line = "=" * 62
    say(line, "cyan")
    say(f"  {title}", "bold")
    say(line, "cyan")


def fail_help(lines: Iterable[str]) -> None:
    """红字指引：杀软/权限拦截时给人看的东西。"""
    say("", "reset")
    for line in lines:
        say(f"  {line}", "red")
    say("", "reset")


# ---------------------------------------------------------------- 进程/权限


def is_admin() -> bool:
    if not IS_WINDOWS:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_wow64() -> bool:
    """32 位进程跑在 64 位系统上 —— 装 64 位驱动会失败，必须提前拦。"""
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.kernel32.IsWow64Process(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(wintypes.BOOL())
        ))
    except Exception:
        return False


def is_64bit_os() -> bool:
    if not IS_WINDOWS:
        return sys.maxsize > 2**32
    try:
        return bool(ctypes.windll.kernel32.IsWow64Process2 is None) or True
    except Exception:
        return True


def running_processes() -> set[str]:
    """进程名集合（小写，含 .exe）。用 tasklist，避免依赖 psutil。"""
    if not IS_WINDOWS:
        return set()
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            encoding="gbk", errors="ignore",
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        ).stdout
    except Exception:
        return set()
    names: set[str] = set()
    for line in out.splitlines():
        parts = line.split('","')
        if parts and parts[0].startswith('"'):
            names.add(parts[0].strip('"').lower())
    return names


# 会拦截驱动安装的安全软件（命中就给具体指引）
AV_SIGNATURES = {
    "360tray.exe": "360 安全卫士",
    "360sd.exe": "360 杀毒",
    "360safe.exe": "360 安全卫士",
    "zhudongfangyu.exe": "360 主动防御",
    "huorong.exe": "火绒安全",
    "usysdiag.exe": "火绒安全",
    "qqpcmgr.exe": "腾讯电脑管家",
    "kxeetray.exe": "金山毒霸",
    "avp.exe": "卡巴斯基",
    "mcshield.exe": "McAfee",
    "msmpeng.exe": "Windows Defender",
    "esetnod32.exe": "ESET NOD32",
}


def detect_security_software() -> list[str]:
    procs = running_processes()
    hits = [name for exe, name in AV_SIGNATURES.items() if exe in procs]
    # 去重但保留顺序
    seen: list[str] = []
    for h in hits:
        if h not in seen:
            seen.append(h)
    return seen


# ---------------------------------------------------------------- UAC 提权

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_FLAG_NO_UI = 0x00000400
ERROR_CANCELLED = 1223
WAIT_TIMEOUT = 0x00000102


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIcon", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


@dataclass
class ElevationResult:
    ok: bool
    exit_code: int | None
    cancelled: bool
    error: str = ""


def run_elevated_wait(exe: str, args: list[str], timeout: int = 600,
                      cwd: str | None = None, show: bool = True) -> ElevationResult:
    """以管理员身份运行并等待结束（会弹 UAC）。

    返回真实退出码；用户点了“否”时 cancelled=True。
    """
    if not IS_WINDOWS:
        return ElevationResult(False, None, False, "仅支持 Windows")
    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    sei.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
    sei.hwnd = None
    sei.lpVerb = "runas"
    sei.lpFile = str(exe)
    sei.lpParameters = subprocess.list2cmdline([str(a) for a in args]) if args else None
    sei.lpDirectory = cwd or os.path.dirname(str(exe)) or None
    sei.nShow = 1 if show else 0

    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    ok = shell32.ShellExecuteExW(ctypes.byref(sei))
    if not ok:
        err = ctypes.get_last_error() or kernel32.GetLastError()
        if err == ERROR_CANCELLED:
            return ElevationResult(False, None, True, "用户取消了 UAC 提权")
        return ElevationResult(False, None, False, f"ShellExecuteEx 失败 (Win32 {err})")

    handle = sei.hProcess
    try:
        wait_ms = timeout * 1000
        rc = kernel32.WaitForSingleObject(handle, wait_ms)
        if rc == WAIT_TIMEOUT:
            return ElevationResult(False, None, False, f"安装超时（>{timeout}s）")
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return ElevationResult(True, int(code.value), False)
    finally:
        if handle:
            kernel32.CloseHandle(handle)


def relaunch_as_admin(extra_args: list[str] | None = None) -> int:
    """把当前脚本以管理员身份重新拉起（用于 install_drivers.py）。"""
    script = os.path.abspath(sys.argv[0])
    args = list(sys.argv[1:]) if extra_args is None else list(extra_args)
    # 用 python.exe 显式拉起，避免 .py 关联丢失
    params = subprocess.list2cmdline([script] + args)
    res = run_elevated_wait(sys.executable, [script] + args, timeout=3600,
                            cwd=os.path.dirname(script) or None)
    if res.cancelled:
        fail_help([
            "已取消管理员授权（UAC），驱动无法安装。",
            "请重新运行并点击『是』，或右键 install_drivers.py → 以管理员身份运行。",
        ])
        return 1223
    if not res.ok:
        fail_help([f"提权失败: {res.error}"])
        return 1
    return res.exit_code or 0
