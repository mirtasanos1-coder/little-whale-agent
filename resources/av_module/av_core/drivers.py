"""虚拟驱动：检测 / 静默安装 / 验证 / 诊断建议。

两个驱动
--------
1. VB-Audio Virtual Cable  —— 虚拟声卡（模块一的物理基础）
     · 安装后出现两块设备：播放端 "CABLE Input"、录音端 "CABLE Output"
     · 把 TTS 播到 "CABLE Input"，QQ 的“麦克风”选 "CABLE Output" 即可听见 AI

2. OBS Virtual Camera      —— 虚拟摄像头（模块二的物理基础）
     · 只装 DirectShow 滤镜，不需要装整个 OBS Studio，也不需要 OBS 在运行
     · pyvirtualcam 的 windows 后端直接向这个滤镜推帧

本模块**只用标准库**（winreg / ctypes / subprocess），
因为驱动安装通常发生在 `pip install` 之前，甚至可能在裸机 PE 环境里跑。
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import winutil
from .winutil import clr, say

CREATE_NO_WINDOW = 0x08000000


# ============================================================ 驱动清单

@dataclass
class DriverSpec:
    key: str
    name: str
    kind: str                      # audio | camera
    vendor: str
    # 安装包在 drivers/ 下的候选文件名（按优先级）
    installer_names: list[str]
    silent_args: list[str]
    download_urls: list[str]
    homepage: str
    # 检测到这些关键字即认为设备已存在
    device_keywords: list[str]
    exit_code_note: str = ""
    size_mb: float = 0.0
    extra: dict = field(default_factory=dict)

    def find_installer(self, drivers_dir: Path) -> Path | None:
        for name in self.installer_names:
            if "*" in name or name.lower().endswith((".zip", ".7z", ".rar")):
                continue        # 通配符/压缩包不是可执行安装器
            p = drivers_dir / name
            if p.is_file() and p.stat().st_size > 64 * 1024:
                return p
        # 兜底：按关键字在 drivers/ 里扫
        if drivers_dir.is_dir():
            keys = [n.split(".")[0].lower()[:8] for n in self.installer_names]
            for p in drivers_dir.iterdir():
                if p.suffix.lower() in (".exe", ".msi") and p.is_file():
                    if any(k in p.name.lower() for k in keys):
                        return p
        return None


VB_CABLE = DriverSpec(
    key="vbcable",
    name="VB-Audio Virtual Cable",
    kind="audio",
    vendor="VB-Audio (Vincent Burel)",
    installer_names=[
        "VBCABLE_Setup_x64.exe",
        "VBCABLE_Setup.exe",
        "VBCABLE_Driver_Pack*.zip",
    ],
    silent_args=["-i", "-h"],       # -i 安装 / -h 静默（不打弹窗）
    download_urls=[
        "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip",
        "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack43.zip",
        "https://vb-audio.com/Cable/VBCABLE_Driver_Pack43.zip",
    ],
    homepage="https://vb-audio.com/Cable/",
    device_keywords=["cable input", "cable output", "vb-audio", "vb audio"],
    exit_code_note="VB-Cable 安装器即使成功也常返回 1，必须以“设备是否出现”为准。",
    size_mb=1.6,
    extra={
        "render_name": "CABLE Input",
        "capture_name": "CABLE Output",
        "reboot": "安装后若设备未立即出现，注销/重启一次即可（无需重装）。",
    },
)

OBS_VIRTUAL_CAM = DriverSpec(
    key="virtualcam",
    name="OBS Virtual Camera",
    kind="camera",
    vendor="OBS Project",
    installer_names=[
        "obs-virtualcam-2.0.5-Windows-installer.exe",
        "obs-virtualcam-*-Windows-installer.exe",
        "OBS-Studio-*-Windows-Installer.exe",
        "OBS-Studio-*-Windows-x64-Installer.exe",
        "UnityCaptureInstaller.exe",
    ],
    silent_args=["/S"],             # NSIS 静默
    download_urls=[
        "https://github.com/Fenrirthviti/obs-virtual-cam/releases/download/2.0.5/obs-virtualcam-2.0.5-Windows-installer.exe",
        "https://ghproxy.net/https://github.com/Fenrirthviti/obs-virtual-cam/releases/download/2.0.5/obs-virtualcam-2.0.5-Windows-installer.exe",
        "https://mirror.ghproxy.com/https://github.com/Fenrirthviti/obs-virtual-cam/releases/download/2.0.5/obs-virtualcam-2.0.5-Windows-installer.exe",
        "https://obsproject.com/download",
    ],
    homepage="https://obsproject.com/",
    device_keywords=["obs virtual camera", "obs-camera", "obs virtual", "virtual camera",
                     "unity capture", "unitycapture", "akvcam"],
    exit_code_note="NSIS 安装器返回 0 表示成功；返回 2/3 多为被安全软件拦截。",
    size_mb=1.1,
    extra={
        "clsid": "{A3FCE0F5-3493-419F-958A-ABA1250EC20B}",  # OBS 虚拟摄像头 DirectShow 滤镜
        "reboot": "装完无需重启，但正在运行的会议/聊天软件需重启才能看到新摄像头。",
    },
)

SPECS: dict[str, DriverSpec] = {"vbcable": VB_CABLE, "virtualcam": OBS_VIRTUAL_CAM}


# ============================================================ 设备枚举（纯注册表）

_MMDEVICES = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
_FRIENDLY = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
_DEVICEDESC = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
_STATE = "DeviceState"     # 1 = 已启用


def _reg_str(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        for enc in ("utf-16-le", "mbcs", "utf-8"):
            try:
                return value.decode(enc).strip("\x00")
            except Exception:
                continue
        return value.decode("utf-8", "ignore").strip("\x00")
    return str(value)


def mmdevices_names(kind: str = "Render") -> list[dict]:
    """枚举 Windows 音频端点（Render=播放, Capture=录音）。纯 winreg，无依赖。"""
    result: list[dict] = []
    try:
        import winreg
    except Exception:
        return result
    base = f"{_MMDEVICES}\\{kind}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return result
    try:
        i = 0
        while True:
            try:
                guid = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                sub = winreg.OpenKey(root, f"{guid}\\Properties")
            except OSError:
                continue
            friendly = desc = ""
            try:
                for val_name in (_FRIENDLY, _DEVICEDESC):
                    try:
                        v, _t = winreg.QueryValueEx(sub, val_name)
                        text = _reg_str(v)
                        if val_name == _FRIENDLY:
                            friendly = text
                        else:
                            desc = text
                    except OSError:
                        pass
            finally:
                sub.Close()
            state = None
            try:
                key = winreg.OpenKey(root, guid)
                try:
                    state = int(winreg.QueryValueEx(key, _STATE)[0])
                finally:
                    key.Close()
            except OSError:
                pass
            if friendly or desc:
                result.append({
                    "name": friendly or desc,
                    "description": desc,
                    "id": guid,
                    "state": state,
                    "active": state in (None, 1),
                    "kind": kind,
                })
    finally:
        root.Close()
    return result


def pnp_devices(classes: list[str]) -> list[dict]:
    """PnP 设备列表（摄像头类用这个最准）。失败返回空。"""
    if not winutil.IS_WINDOWS:
        return []
    class_arg = ",".join(classes)
    cmd = (
        f"Get-PnpDevice -Class {class_arg} -ErrorAction SilentlyContinue | "
        "Select-Object Status,FriendlyName,Class,InstanceId | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", cmd],
            capture_output=True, text=True, timeout=25,
            encoding="utf-8", errors="ignore",
            creationflags=CREATE_NO_WINDOW,
        ).stdout.strip()
        if not out:
            return []
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        return [d for d in data if isinstance(d, dict)]
    except Exception:
        return []


# ============================================================ 检测

def detect(spec: DriverSpec) -> dict:
    """返回 {"installed": bool, "evidence": [...], "devices": [...], "hint": str}"""
    evidence: list[str] = []
    devices: list[dict] = []

    if spec.kind == "audio":
        render = mmdevices_names("Render")
        capture = mmdevices_names("Capture")
        for d in render + capture:
            blob = f"{d['name']} {d['description']}".lower()
            if any(k in blob for k in spec.device_keywords):
                devices.append(d)
                evidence.append(f"[MMDevices] {d['kind']}: {d['name']} (state={d['state']})")
        # 交叉验证：sounddevice 视角（装了包才用）
        try:
            import sounddevice as sd  # type: ignore

            for idx, dev in enumerate(sd.query_devices()):
                blob = str(dev.get("name", "")).lower()
                if any(k in blob for k in spec.device_keywords):
                    evidence.append(
                        f"[PortAudio#{idx}] {dev['name']} "
                        f"in={dev['max_input_channels']} out={dev['max_output_channels']}"
                    )
        except Exception:
            pass
    else:
        # 1) PnP 摄像头类
        pnp = pnp_devices(["Camera", "Image", "Media"])
        for d in pnp:
            name = (d.get("FriendlyName") or "").lower()
            if any(k in name for k in spec.device_keywords):
                devices.append({"name": d.get("FriendlyName"), "status": d.get("Status"),
                                "instance": d.get("InstanceId")})
                evidence.append(f"[PnP] {d.get('FriendlyName')} ({d.get('Status')})")
        # 2) DirectShow 滤镜 CLSID
        clsid = spec.extra.get("clsid")
        if clsid:
            try:
                import winreg

                for hive, flag in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ),
                                   (winreg.HKEY_LOCAL_MACHINE,
                                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY)):
                    try:
                        k = winreg.OpenKey(hive, rf"SOFTWARE\Classes\CLSID\{clsid}", 0, flag)
                        k.Close()
                        evidence.append(f"[Registry] CLSID {clsid} 已注册")
                        break
                    except OSError:
                        continue
            except Exception:
                pass
        # 3) pyvirtualcam 实测（最权威）
        probe = probe_pyvirtualcam(spec)
        if probe.get("available"):
            evidence.append(f"[pyvirtualcam] 后端可用: {probe.get('backend')}")
            devices.append({"name": probe.get("device"), "backend": probe.get("backend")})

    installed = bool(evidence) and (bool(devices) or spec.kind == "camera")
    return {
        "key": spec.key,
        "name": spec.name,
        "kind": spec.kind,
        "installed": installed,
        "evidence": evidence,
        "devices": devices,
        "installer": None,   # 由调用方补
        "hint": spec.extra.get("reboot", ""),
    }


def probe_pyvirtualcam(spec: DriverSpec | None = None) -> dict:
    """创建并立即关闭一次虚拟摄像头，确认后端真的能用。"""
    try:
        import pyvirtualcam  # type: ignore
    except Exception as exc:
        return {"available": False, "error": f"pyvirtualcam 未安装: {exc}"}
    backends = ["obs"]
    if spec is not None and spec.extra.get("backend"):
        backends.insert(0, spec.extra["backend"])
    last = ""
    for backend in backends:
        try:
            cam = pyvirtualcam.Camera(width=640, height=360, fps=5, backend=backend)
            try:
                import numpy as np

                cam.send(np.zeros((360, 640, 3), dtype=np.uint8))
            except Exception:
                pass
            device = getattr(cam, "device", backend)
            cam.close()
            return {"available": True, "backend": backend, "device": device}
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    return {"available": False, "error": last or "未找到虚拟摄像头后端"}


# ============================================================ 安装

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_signature(path: Path) -> dict:
    """Authenticode 签名检查（不阻断，只告警）。"""
    if not winutil.IS_WINDOWS:
        return {"status": "unknown", "signer": None}
    cmd = (
        f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
        "$s | Select-Object Status,@{n='Signer';e={$_.SignerCertificate.Subject}} | "
        "ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", cmd],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="ignore", creationflags=CREATE_NO_WINDOW,
        ).stdout.strip()
        data = json.loads(out) if out else {}
        if isinstance(data, list):        # 某些 PowerShell 版本会包成数组
            data = data[0] if data else {}
        raw = str(data.get("Status") if data.get("Status") is not None
                  else (data.get("status") if data.get("status") is not None else "Unknown")).strip()
        signer = data.get("Signer") or data.get("signer")
        # 注意：Windows PowerShell 5.1 的 ConvertTo-Json 会把 SignatureStatus 枚举序列化成数字
        # 0=Valid 1=UnknownError 2=NotSigned 3=HashMismatch 4=NotTrusted 5=NotSupportedFileFormat
        friendly = {
            "valid": "valid", "0": "valid",
            "unknownerror": "unknown", "1": "unknown",
            "notsigned": "not_signed", "2": "not_signed",
            "hashmismatch": "hash_mismatch", "3": "hash_mismatch",
            "nottrusted": "not_trusted", "4": "not_trusted",
            "notsupportedfileformat": "not_supported", "5": "not_supported",
            "incompatible": "incompatible", "6": "incompatible",
        }.get(raw.lower(), "unknown")
        return {"status": friendly, "raw_status": raw,
                "signer": (signer or "")[:160] or None,
                "ok": friendly == "valid"}
    except Exception as exc:
        return {"status": "unknown", "signer": None, "error": str(exc), "ok": False}


AV_BLOCK_HINT = [
    "驱动安装被拦截了 —— 这是杀毒/安全软件的主动防御，不是脚本的问题。",
    "请按顺序处理：",
    "  1) 看屏幕右下角是否有 360/火绒/电脑管家 的弹窗，选择『允许本次操作』『信任该程序』；",
    "  2) 没有弹窗就临时退出这些安全软件（退出，不是关闭主界面），再重跑本脚本；",
    "  3) Windows Defender 用户：『病毒和威胁防护 → 排除项』把本项目目录加入排除；",
    "  4) 仍然失败：右键 install_drivers.py → 以管理员身份运行，全程点『是』；",
    "  5) 公司电脑可能受组策略限制，需要 IT 放行未签名/驱动安装。",
]


def install_driver(spec: DriverSpec, drivers_dir: Path, timeout: int = 300,
                   force: bool = False, elevate: bool = True) -> dict:
    """安装单个驱动。返回结构化结果（不抛异常）。"""
    say(f"\n[{spec.name}] 检测中 ...", "cyan")
    st = detect(spec)
    st["installer"] = None
    if st["installed"] and not force:
        say(f"[{spec.name}] 已安装，跳过。", "green")
        for ev in st["evidence"][:4]:
            say(f"    · {ev}", "dim")
        st["action"] = "already_installed"
        return st

    installer = spec.find_installer(drivers_dir)
    if installer is None:
        st["action"] = "installer_missing"
        st["error"] = (
            f"在 {drivers_dir} 里找不到安装包（期望: {', '.join(spec.installer_names)}）"
        )
        say(f"[{spec.name}] 缺少安装包。", "yellow")
        say(f"    先执行:  python drivers/fetch_drivers.py --only {spec.key}", "yellow")
        say(f"    或手动下载: {spec.homepage}  放到 {drivers_dir}", "yellow")
        return st

    st["installer"] = str(installer)
    sig = check_signature(installer)
    st["signature"] = sig
    sig_status = str(sig.get("status", "")).lower()
    if sig_status == "valid":
        say(f"[{spec.name}] 签名有效: {sig.get('signer') or '(未提供主体)'}", "dim")
    else:
        note = {
            "not_signed": "安装包未签名（VB-Audio 部分版本如此），静默安装时杀软更容易拦截",
            "not_trusted": "证书链不受信任",
            "hash_mismatch": "签名哈希不匹配 —— 文件可能被篡改或下载损坏！",
            "unknown": "没取到签名信息（无 PowerShell 或文件被占用），跳过校验",
        }.get(sig_status, f"未知状态: {sig_status}")
        say(f"[{spec.name}] 签名提示：{note}", "yellow")
        st["signature_note"] = note

    if not winutil.is_admin():
        if not elevate:
            st["action"] = "needs_admin"
            st["error"] = "需要管理员权限"
            say(f"[{spec.name}] 需要管理员权限（UAC）。", "yellow")
            return st
        # 单驱动提权：用 runas 拉起安装器本身
        say(f"[{spec.name}] 请求管理员权限 ...", "yellow")
        res = winutil.run_elevated_wait(
            str(installer), spec.silent_args, timeout=timeout,
            cwd=str(installer.parent),
        )
        st["elevated"] = True
        st["exit_code"] = res.exit_code
        if res.cancelled:
            st["action"] = "cancelled"
            st["error"] = "用户取消 UAC"
            winutil.fail_help(["已取消管理员授权，驱动未安装。"])
            return st
        if not res.ok:
            st["action"] = "elevate_failed"
            st["error"] = res.error
            return st
    else:
        say(f"[{spec.name}] 以管理员身份静默安装: {installer.name} "
            f"{' '.join(spec.silent_args)}", "cyan")
        try:
            proc = subprocess.run(
                [str(installer)] + spec.silent_args,
                cwd=str(installer.parent), timeout=timeout,
                capture_output=True, text=True, errors="ignore",
                creationflags=CREATE_NO_WINDOW,
            )
            st["exit_code"] = proc.returncode
            st["stdout_tail"] = (proc.stdout or "")[-2000:]
            st["stderr_tail"] = (proc.stderr or "")[-2000:]
        except subprocess.TimeoutExpired:
            st["action"] = "timeout"
            st["error"] = f"安装超时（>{timeout}s）"
            return st
        except Exception as exc:
            st["action"] = "exec_failed"
            st["error"] = f"{type(exc).__name__}: {exc}"
            return st

    # 安装器跑完不代表装上了：等一会儿，用设备是否出现来判定
    say(f"[{spec.name}] 等待设备枚举 ...", "dim")
    after = None
    for _ in range(10):
        time.sleep(1.0)
        after = detect(spec)
        if after["installed"]:
            break
    st["after"] = after
    if after and after["installed"]:
        st["action"] = "installed"
        st["installed"] = True
        say(f"[{spec.name}] 安装成功。", "green")
        for ev in after["evidence"][:6]:
            say(f"    · {ev}", "green")
        return st

    # ---- 失败诊断 ----
    av = winutil.detect_security_software()
    st["action"] = "verify_failed"
    st["installed"] = False
    st["security_software"] = av
    st["error"] = "安装器已运行，但系统里没有出现对应设备"
    say(f"[{spec.name}] 安装器执行完毕，但设备未出现。", "yellow")
    if st.get("exit_code") not in (None, 0):
        say(f"    退出码 = {st['exit_code']}  ({spec.exit_code_note})", "yellow")
    if av:
        say(f"    检测到安全软件在运行: {', '.join(av)}", "yellow")
    winutil.fail_help(AV_BLOCK_HINT)
    say(f"    {spec.extra.get('reboot', '')}", "yellow")
    return st


def status_all(drivers_dir: Path | None = None, with_signature: bool = False) -> dict:
    drivers_dir = drivers_dir or Path(__file__).resolve().parent.parent / "drivers"
    out: dict[str, Any] = {
        "admin": winutil.is_admin(),
        "is_windows": winutil.IS_WINDOWS,
        "security_software": winutil.detect_security_software(),
        "drivers_dir": str(drivers_dir),
        "drivers": {},
    }
    for key, spec in SPECS.items():
        st = detect(spec)
        inst = spec.find_installer(drivers_dir)
        st["installer_present"] = inst is not None
        st["installer_path"] = str(inst) if inst else None
        st["installer_size_mb"] = round(inst.stat().st_size / 1048576, 2) if inst else 0
        st["download_urls"] = spec.download_urls
        st["silent_args"] = spec.silent_args
        st["vendor"] = spec.vendor
        if inst is not None and with_signature:
            st["signature"] = check_signature(inst)
        out["drivers"][key] = st
    out["ready"] = all(d["installed"] for d in out["drivers"].values())
    return out
