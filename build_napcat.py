# -*- coding: utf-8 -*-
"""把 NapCat 离线包打进安装包（构建期跑一次，最终用户就不用下载了）。

用法：
    python build_napcat.py              # 有缓存就用缓存，没有才下载
    python build_napcat.py --force      # 强制重新下载
    python build_napcat.py --offline    # 只用缓存，绝不联网（断网构建）
    python build_napcat.py --tag v4.18.41   # 指定版本

产物：
    build_napcat/NapCat.Shell.Windows.Node.zip        （缓存，别提交）
    desktop/src-tauri/resources/napcat_bundle.zip     （会被 Tauri 打进安装包）

为什么要内置：
    国内不翻墙下 118MB 的 GitHub 直链，实测要一个多小时（还会断）。
    内置之后首次启动是「解压本地文件」，几秒完成；
    向导里的「一键自动安装」直接走内置包，网络只作为兜底。

后端怎么找到它（napcat_mgr.bundled_zip）：
    <安装目录>/resources/napcat_bundle.zip      ← 安装版
    <安装目录>/napcat_bundle.zip                ← 绿色版
"""
import argparse
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "build_napcat")
RES_DIR = os.path.join(HERE, "desktop", "src-tauri", "resources")
ASSET = "NapCat.Shell.Windows.Node.zip"      # 自带 Node 的免安装版，开箱即用


def log(msg):
    print("[napcat] " + str(msg), flush=True)


def human(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "?"


def _cached_path():
    return os.path.join(CACHE_DIR, ASSET)


def _cached_ok(path):
    """缓存文件能用吗：存在、够大、里面确实有 NapCat 启动器。"""
    if not os.path.isfile(path):
        return False
    size = os.path.getsize(path)
    if size < 20 * 1024 * 1024:
        log("缓存文件太小（%s），当作无效" % human(size))
        return False
    try:
        import napcat_mgr
        if not napcat_mgr.zip_has_launcher(path):
            log("缓存文件里没找到 NapCat 启动器，当作无效")
            return False
    except Exception as e:
        log("校验缓存时出错（继续用）：%s" % e)
    return True


def download(tag="", force=False):
    """下载离线包到缓存目录，失败返回空串。

    走 napcat_mgr 的三级降级（官方 → 国内镜像），构建机有网就行；
    下载进度按 5% 打一行，别让人以为卡死了。
    """
    dst = _cached_path()
    os.makedirs(CACHE_DIR, exist_ok=True)
    if not force and _cached_ok(dst):
        log("用缓存：%s（%s）" % (dst, human(os.path.getsize(dst))))
        return dst

    try:
        import napcat_mgr
    except Exception as e:
        log("导入 napcat_mgr 失败：%s" % e)
        return ""

    url = tag_url(tag)
    if not url:
        return ""
    log("开始下载：%s" % url)
    last = [-1]

    def prog(msg):
        # napcat_mgr 给的文案里带百分比，我们每 5% 才打一行，免得刷屏
        s = str(msg)
        pct = -1
        try:
            import re
            m = re.search(r"[（(]\s*(\d{1,3})\s*[%％]", s)
            if m:
                pct = int(m.group(1))
        except Exception:
            pass
        if pct < 0 or pct // 5 != last[0] // 5:
            last[0] = pct if pct >= 0 else last[0]
            log(s)

    ok, msg, got = napcat_mgr.download_official_zip(dst, url, progress=prog)
    if not ok or not got or not os.path.isfile(dst):
        log("下载失败：%s" % msg)
        return ""
    log("下载完成：%s（%s）" % (dst, human(os.path.getsize(dst))))
    return dst


def tag_url(tag=""):
    """拿到某个 tag（默认最新）的 Windows Node 包直链。"""
    try:
        import napcat_mgr
        info = napcat_mgr.latest_release(tag=tag) if tag else napcat_mgr.latest_release()
        if info:
            return info.get("url") or ""
    except Exception as e:
        log("查最新版本失败：%s" % e)
    if tag:
        return ("https://github.com/NapNeko/NapCatQQ/releases/download/%s/%s" % (tag, ASSET))
    return ""


def publish(zip_path):
    """把离线包放进 Tauri 的 resources 目录（会被打进安装包）。"""
    if not zip_path or not os.path.isfile(zip_path):
        return ""
    os.makedirs(RES_DIR, exist_ok=True)
    dst = os.path.join(RES_DIR, "napcat_bundle.zip")
    src_size = os.path.getsize(zip_path)
    if os.path.isfile(dst) and os.path.getsize(dst) == src_size:
        log("resources 里已经是最新的（%s）" % human(src_size))
        return dst
    log("复制到 %s ..." % dst)
    shutil.copyfile(zip_path, dst)
    log("完成：%s（%s）" % (dst, human(os.path.getsize(dst))))
    return dst


def main(argv=None):
    ap = argparse.ArgumentParser(description="把 NapCat 离线包打进安装包")
    ap.add_argument("--force", action="store_true", help="强制重新下载")
    ap.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    ap.add_argument("--tag", default="", help="指定 NapCat 版本，如 v4.18.41")
    a = ap.parse_args(argv)

    t0 = time.time()
    zip_path = _cached_path()
    if a.offline:
        if not _cached_ok(zip_path):
            log("离线模式下没有可用缓存：%s" % zip_path)
            return 2
        log("用缓存：%s" % zip_path)
    else:
        zip_path = download(tag=a.tag, force=a.force)
        if not zip_path:
            log("拿不到离线包 —— 安装包会退回「联网下载」模式（能用，但国内很慢）")
            return 1

    got = publish(zip_path)
    if not got:
        return 3
    log("总耗时 %.1f 秒" % (time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
