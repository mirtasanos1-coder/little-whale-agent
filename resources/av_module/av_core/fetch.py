"""驱动安装包的自动获取（下载 / 解压 / 校验 / 记账）。

为什么要有这一层
----------------
最终目标是把本模块打包进主控台，所以 `drivers/` 必须能被一条命令填满：
`python drivers/fetch_drivers.py --all`

网络现实（本机实测）
--------------------
* vb-audio.com 直连可用 → VB-Cable 走官方源
* github.com 经常超时 → OBS 虚拟摄像头走 ghproxy 镜像
* 因此每个驱动都给多条 URL，逐个尝试，谁先成功用谁

许可证
------
* VB-Audio Cable：donationware，允许免费个人/商业使用，但**再分发需遵守其条款**
  （打包前请阅读 https://vb-audio.com/Cable/ ，通常建议首次运行时下载）
* OBS Virtual Camera：GPL-2.0（OBS Project 插件）
所以 MANIFEST.json 里记录 license 字段，便于发布时合规核对。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import config as cfgmod
from . import drivers as drvmod
from .winutil import say

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"),
    "Accept": "*/*",
}

GH_MIRRORS = [
    "https://ghproxy.net/{url}",
    "https://gh-proxy.com/{url}",
    "https://mirror.ghproxy.com/{url}",
]

LICENSE = {
    "vbcable": "Donationware (VB-Audio) — 免费使用，再分发前请看 vb-audio.com/Cable/",
    "virtualcam": "GPL-2.0 (OBS Project / obs-virtual-cam)",
}


def candidate_urls(spec: drvmod.DriverSpec) -> list[str]:
    """原始 URL + 针对 github 的镜像变体，去重保序。"""
    out: list[str] = []
    for url in spec.download_urls:
        if url not in out:
            out.append(url)
        if "github.com" in url:
            for tpl in GH_MIRRORS:
                mirrored = tpl.format(url=url)
                if mirrored not in out:
                    out.append(mirrored)
    return out


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, timeout: float = 45.0, retries: int = 2,
             quiet: bool = False) -> dict:
    """流式下载到 dest.part → 改名。返回 {ok,url,path,size,error}。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    last_err = ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                total = int(resp.headers.get("content-length") or 0)
                got = 0
                t0 = time.time()
                with part.open("wb") as fh:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        if not quiet and total > 0:
                            pct = got * 100 // total
                            speed = got / max(0.001, time.time() - t0) / 1048576
                            print(f"\r    {pct:3d}%  {got / 1048576:6.2f}/{total / 1048576:.2f} MB"
                                  f"  {speed:5.1f} MB/s", end="", flush=True)
                if not quiet:
                    print()
            if total and got != total:
                raise IOError(f"下载不完整 {got}/{total}")
            if got < 64 * 1024:
                raise IOError(f"文件太小({got} 字节)，可能被拦截或返回了错误页")
            part.replace(dest)
            return {"ok": True, "url": url, "path": str(dest), "size": got}
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code} {exc.reason}"
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
        try:
            part.unlink(missing_ok=True)
        except Exception:
            pass
        if attempt < retries:
            say(f"    重试 {attempt + 1}/{retries} ... ({last_err})", "yellow")
            time.sleep(1.2)
    return {"ok": False, "url": url, "error": last_err}


def _is_executable(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"MZ"
    except Exception:
        return False


def extract_installer(archive: Path, spec: drvmod.DriverSpec, dest_dir: Path) -> list[Path]:
    """从 zip 里挑出安装器（支持嵌套目录），返回解出来的文件列表。"""
    wanted = [n.lower() for n in spec.installer_names if "*" not in n]
    out: list[Path] = []
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            base = info.filename.rsplit("/", 1)[-1]
            low = base.lower()
            if not (low.endswith((".exe", ".msi"))):
                continue
            if wanted and low not in wanted:
                # 通配允许：名字里带关键前缀也收
                if not any(w.split(".")[0][:6] in low for w in wanted):
                    continue
            target = dest_dir / base
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(target)
    return out


def fetch_spec(spec: drvmod.DriverSpec, dest_dir: Path | None = None,
               force: bool = False, quiet: bool = False) -> dict:
    """下载 + （必要时）解压 一个驱动的安装包。已存在则跳过。"""
    dest_dir = dest_dir or cfgmod.DRIVERS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)

    existing = spec.find_installer(dest_dir)
    if existing and not force:
        say(f"[{spec.name}] 安装包已存在: {existing.name}", "green")
        return {"ok": True, "skipped": True, "file": str(existing),
                "sha256": sha256_of(existing),
                "size_mb": round(existing.stat().st_size / 1048576, 2)}

    say(f"[{spec.name}] 开始下载（约 {spec.size_mb or '?'} MB）...", "cyan")
    errors: list[str] = []
    for url in candidate_urls(spec):
        name = url.rsplit("/", 1)[-1].split("?")[0] or "download.bin"
        tmp = dest_dir / (name if name.lower().endswith((".zip", ".exe", ".msi"))
                          else name + ".bin")
        say(f"  源: {url[:110]}", "dim")
        res = download(url, tmp, quiet=quiet)
        if not res["ok"]:
            errors.append(f"{url} -> {res['error']}")
            continue

        if tmp.suffix.lower() == ".zip":
            try:
                files = extract_installer(tmp, spec, dest_dir)
            except zipfile.BadZipFile as exc:
                errors.append(f"{url} -> 不是有效 zip: {exc}")
                tmp.unlink(missing_ok=True)
                continue
            if not files:
                errors.append(f"{url} -> zip 里没找到安装器")
                continue
            picked = spec.find_installer(dest_dir)
            if picked is None:
                errors.append(f"{url} -> 解压后仍未匹配 installer_names")
                continue
            say(f"  解压得到: {', '.join(p.name for p in files)}", "green")
            return {"ok": True, "file": str(picked), "url": url,
                    "sha256": sha256_of(picked),
                    "size_mb": round(picked.stat().st_size / 1048576, 2),
                    "extracted": [str(p) for p in files]}

        if not _is_executable(tmp):
            errors.append(f"{url} -> 下载内容不是 PE 可执行文件（可能是错误页/被拦截）")
            tmp.unlink(missing_ok=True)
            continue
        final = dest_dir / spec.installer_names[0]
        if tmp != final:
            tmp.replace(final)
        say(f"  完成: {final.name} "
            f"({final.stat().st_size / 1048576:.2f} MB) sha256={sha256_of(final)[:16]}…", "green")
        return {"ok": True, "file": str(final), "url": url,
                "sha256": sha256_of(final),
                "size_mb": round(final.stat().st_size / 1048576, 2)}

    say(f"[{spec.name}] 所有下载源都失败。", "red")
    for e in errors:
        say(f"    · {e}", "dim")
    say("  手动方案：浏览器打开 homepage 下载，把安装器放到 drivers/ 目录。", "yellow")
    say(f"  homepage = {spec.homepage}", "yellow")
    return {"ok": False, "errors": errors, "homepage": spec.homepage}


def fetch_all(only: list[str] | None = None, force: bool = False,
              quiet: bool = False) -> dict:
    keys = only or list(drvmod.SPECS.keys())
    results: dict[str, dict] = {}
    for key in keys:
        spec = drvmod.SPECS.get(key)
        if spec is None:
            results[key] = {"ok": False, "error": f"未知驱动: {key}"}
            continue
        results[key] = fetch_spec(spec, force=force, quiet=quiet)
    write_manifest()
    return results


def write_manifest(dest_dir: Path | None = None) -> Path:
    """drivers/MANIFEST.json：记账 + 打包清单（体积/哈希/许可证）。"""
    dest_dir = dest_dir or cfgmod.DRIVERS_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for key, spec in drvmod.SPECS.items():
        f = spec.find_installer(dest_dir)
        items.append({
            "key": key,
            "name": spec.name,
            "kind": spec.kind,
            "vendor": spec.vendor,
            "file": f.name if f else None,
            "size_mb": round(f.stat().st_size / 1048576, 2) if f else 0,
            "sha256": sha256_of(f) if f else None,
            "silent_args": spec.silent_args,
            "homepage": spec.homepage,
            "license": LICENSE.get(key, "见 homepage"),
            "present": bool(f),
        })
    manifest = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_mb": round(sum(i["size_mb"] for i in items), 2),
        "items": items,
        "note": "打包时把 drivers/ 整个目录带上；首次运行由 install_drivers.py 静默安装。",
    }
    path = dest_dir / "MANIFEST.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
