# -*- coding: utf-8 -*-
"""虚拟化 / 云沙箱自检（V1.9.4 抢修三）。

要防的不是普通用户，是「把程序丢进自动化脱壳沙箱里跑一遍」这种分析方式：
沙箱里没有真实网卡，BIOS 字符串也带着 VMware / VirtualBox / QEMU 的味道。
命中之后不崩溃、不报错，而是进入**受限模式** —— 敏感动作不执行、
返回看上去正常的失败，界面上只说一句「环境异常」。

为什么值得做：程序里有 RPA 点屏幕、接管 QQ、虚拟驱动这类敏感权限，
在沙箱里被自动跑一遍最容易让人摸清调用链。代价是极少数真跑在虚拟机里的
用户会受影响，所以留了 XJY_ALLOW_VM 这个后门（也方便我们自己测）。
"""
import os
import re
import sys

# 常见虚拟化平台 / 云厂商的网卡 MAC 前缀（OUI）
VM_OUI = {
    "00:05:69": "VMware", "00:0c:29": "VMware", "00:1c:14": "VMware",
    "00:50:56": "VMware", "08:00:27": "VirtualBox", "0a:00:27": "VirtualBox",
    "00:15:5d": "Hyper-V", "00:03:ff": "Hyper-V",
    "00:16:3e": "Xen", "00:1c:42": "Parallels",
    "52:54:00": "QEMU/KVM", "00:0f:4b": "Virtual Iron",
    "00:21:f6": "Oracle VM", "00:14:4f": "Oracle VM",
    "02:42:ac": "Docker", "02:42:0a": "Docker",
}

# BIOS / 主板型号里的虚拟化痕迹
VM_HINTS = ("vmware", "virtualbox", "vbox", "qemu", "kvm", "xen",
            "hyper-v", "virtual hd", "virtual machine", "bochs",
            "parallels", "innotek", "openstack", "alibaba cloud",
            "amazon ec2", "google compute")

_CACHE = {}


def _macs():
    """取本机网卡 MAC（只靠标准库）。"""
    out = []
    try:
        import uuid
        n = uuid.getnode()
        if n:
            h = "%012x" % n
            out.append(":".join(h[i:i + 2] for i in range(0, 12, 2)))
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(["getmac", "/fo", "csv", "/nh"],
                           capture_output=True, timeout=6,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        txt = (r.stdout or b"").decode("utf-8", "ignore")
        for m in re.findall(r"([0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){5})", txt):
            mac = m.replace("-", ":").lower()
            if mac not in out:
                out.append(mac)
    except Exception:
        pass
    return out


def _board_hint():
    """主板 / BIOS 信息里有没有虚拟化字样。"""
    blobs = []
    try:
        import subprocess
        for cmd in (["wmic", "computersystem", "get", "model,manufacturer", "/value"],
                    ["wmic", "bios", "get", "serialnumber,manufacturer", "/value"]):
            r = subprocess.run(cmd, capture_output=True, timeout=6,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            blobs.append((r.stdout or b"").decode("utf-8", "ignore"))
    except Exception:
        pass
    text = " ".join(blobs).lower()
    for h in VM_HINTS:
        if h in text:
            return h
    return ""


def detect(force=False):
    """返回 {vm, vendor, macs, hint, why}；一次进程只算一次。"""
    if _CACHE and not force:
        return dict(_CACHE)
    macs = _macs()
    vendor = ""
    hit_mac = ""
    real = []
    for mac in macs:
        pre = mac[:8].lower()
        if pre in VM_OUI:
            if not vendor:
                vendor = VM_OUI[pre]
                hit_mac = mac
        else:
            real.append(mac)
    hint = "" if vendor else _board_hint()
    why = []
    if vendor:
        why.append("网卡 MAC %s 属于 %s" % (hit_mac, vendor))
    if hint:
        why.append("主板/BIOS 信息里出现 %s" % hint)
    # 关键判据：装了 VMware/VirtualBox 的**普通电脑**也会多出一块虚拟网卡，
    # 只要还有一块真实网卡就不能判成沙箱 —— 否则一半用户会被误伤。
    # 真正的沙箱里通常只剩虚拟网卡（或者网卡信息根本读不到）。
    is_vm = bool(hint) or (bool(vendor) and not real)
    # 自测用：XJY_FORCE_VM=1 直接当沙箱（只影响本进程）
    if str(os.environ.get("XJY_FORCE_VM", "")).strip():
        is_vm = True
        why.append("XJY_FORCE_VM 强制命中")
    out = {"vm": is_vm,
           "vendor": vendor or hint,
           "macs": macs[:6],
           "hint": hint,
           "why": "；".join(why)}
    _CACHE.clear()
    _CACHE.update(out)
    return dict(out)


def allow_vm():
    """测试 / 自用后门：设了 XJY_ALLOW_VM 就当普通机器。

    v1.9.5（N-16）：**只在源码态生效**。打包发行版（sys.frozen）里一律忽略
    这个变量 —— 否则分析者往启动参数里加一个环境变量就把沙箱受限全解除了，
    等于没做。
    """
    if getattr(sys, "frozen", False):
        return False
    return bool(str(os.environ.get("XJY_ALLOW_VM", "")).strip())


def limited():
    """要不要进受限模式（沙箱 + 没开后门）。"""
    if allow_vm():
        return False
    if str(os.environ.get("XJY_FORCE_VM", "")).strip():
        return True
    try:
        return bool(detect().get("vm"))
    except Exception:
        return False


def status():
    d = detect()
    d["limited"] = limited()
    d["frozen"] = bool(getattr(sys, "frozen", False))
    d["macs"] = (d.get("macs") or [])[:3]
    return d
